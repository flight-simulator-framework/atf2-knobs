"""Offline interruption/restart, adaptive QM probes and checkpoint durability."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import numpy as np
from Knobs.DRCorrection.core import Cancelled
from Knobs.DRTuning.dispersion import DispersionService,DispersionOptions,QM_CANDIDATES,probe_pair,quad_limits
from Knobs.DRTuning.qm_checkpoint import load_checkpoint,save_checkpoint
from Knobs.DRTuning.tests.test_bump_dispersion import DispersionFixture

ACTIVE=('QM1R.1','QM3R.1','QM4R.1')


class QMResumeTests(unittest.TestCase):
    def setUp(self):
        self.b=DispersionFixture();self.cancel=threading.Event();self.s=DispersionService(self.b,self.cancel)
        self.opt=DispersionOptions(fixed_actuators=tuple(n for n in QM_CANDIDATES if n not in ACTIVE),adaptive_probe=False)

    def interrupt_after_first(self,path):
        def progress(_,message):
            if message.startswith('QM dispersion response') and '(1/3)' in message:self.cancel.set()
        with self.assertRaises(Cancelled):self.s.calculate(self.opt,progress,checkpoint_path=path)
        return load_checkpoint(path)

    def test_restart_reuses_committed_columns_and_uses_new_smaller_probe(self):
        self.opt.probe_A=.1
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'scan.checkpoint.json';saved=self.interrupt_after_first(path);original=path.read_bytes()
            self.assertEqual(saved['scan_state']['completed'],1);self.assertTrue(saved['restored'])
            self.assertEqual(len(saved['probe_readbacks']),1)
            backend=DispersionFixture();service=DispersionService(backend,threading.Event());self.opt.probe_A=.01
            result=service.resume(saved,self.opt,checkpoint_path=Path(directory)/'continued.checkpoint.json')
            self.assertEqual(path.read_bytes(),original)
            self.assertEqual(result['scan_state']['status'],'complete');self.assertFalse(result['applied'])
            self.assertNotIn(ACTIVE[0],[n for n,_ in backend.commands])
            self.assertEqual(set(n for n,_ in backend.commands),set(ACTIVE[1:]))
            self.assertEqual(backend.acquisitions,150)  # 30 baseline updates + two full +/- QM pairs.
            np.testing.assert_allclose(result['response'][:,0],saved['scan_state']['columns'][0])
            np.testing.assert_allclose(result['probe_readbacks'][0]['requested'],[-.1,.1])
            for probe in result['probe_readbacks'][1:]:np.testing.assert_allclose(probe['requested'],[-.01,.01])
            self.assertEqual(len(result['resume_checks']),1)
            self.assertEqual(backend.snapshot(),saved['baseline'])
            service.validate_checkpoint(load_checkpoint(Path(directory)/'continued.checkpoint.json'))

    def test_half_finished_pair_is_repeated_and_failed_mask_is_not_committed(self):
        original=self.s.signal;calls=0
        def interrupted(opt):
            nonlocal calls
            result=original(opt);calls+=1
            if calls==4:  # baseline, first QM -/+, then second QM - only.
                result[1][20]=False;self.cancel.set()
            return result
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'scan.json'
            with patch.object(self.s,'signal',side_effect=interrupted):
                with self.assertRaises(Cancelled):self.s.calculate(self.opt,checkpoint_path=path)
            saved=load_checkpoint(path);self.assertEqual(saved['scan_state']['completed'],1)
            self.assertTrue(saved['scan_state']['common_valid'][20]);self.assertFalse(saved['valid'][20])
            self.assertEqual(len(saved['orbit_measurements']),12)
            self.cancel.clear();self.b.commands=[]
            result=self.s.resume(saved,self.opt)
            currents=[v for n,v in self.b.commands if n==ACTIVE[1]]
            np.testing.assert_allclose(currents,[-.01,.01,0.])
            self.assertTrue(result['valid'][20]);self.assertEqual(len(result['probe_readbacks']),3)

    def test_crash_with_changed_current_or_rf_requires_explicit_restoration(self):
        with tempfile.TemporaryDirectory() as directory:
            saved=self.interrupt_after_first(Path(directory)/'scan.json');self.cancel.clear()
            self.b.q[ACTIVE[1]]=.01;self.b.offset=2000.;self.b.commands=[];reads=self.b.acquisitions
            with self.assertRaisesRegex(ValueError,'Settings differ'):self.s.resume(saved,self.opt)
            self.assertEqual(self.b.commands,[]);self.assertEqual(self.b.acquisitions,reads)
            self.assertEqual(self.b.offset,2000.)
            self.s.reset(saved);self.assertEqual(self.b.snapshot(),saved['baseline'])
            result=self.s.resume(saved,self.opt);self.assertEqual(result['scan_state']['completed'],3)

    def test_resume_rejects_changed_conditions_and_corrupt_columns_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            saved=self.interrupt_after_first(Path(directory)/'scan.json');self.cancel.clear();self.b.commands=[]
            for key,value in [('frequency_change_khz',2.),('measure_tunes',True),('fixed_actuators',())]:
                opt=copy.deepcopy(self.opt);setattr(opt,key,value)
                with self.assertRaises(ValueError):self.s.resume(saved,opt)
            corrupt=copy.deepcopy(saved);corrupt['scan_state']['columns'][0]=[0.]
            with self.assertRaisesRegex(ValueError,'column shape'):self.s.resume(corrupt,self.opt)
            self.assertEqual(self.b.commands,[])

    def test_atomic_save_failure_keeps_previous_checkpoint_and_restores_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'scan.json';saved=self.interrupt_after_first(path);old=path.read_bytes()
            with patch('Knobs.DRTuning.qm_checkpoint.os.replace',side_effect=OSError('disk full')):
                with self.assertRaisesRegex(OSError,'disk full'):save_checkpoint(path,dict(saved,error='new'))
            self.assertEqual(path.read_bytes(),old);self.assertEqual(list(Path(directory).glob('*.tmp')),[])
            self.cancel.clear();self.b.commands=[]
            with patch('Knobs.DRTuning.dispersion.save_checkpoint',side_effect=OSError('disk full')):
                with self.assertRaisesRegex(OSError,'disk full'):self.s.resume(saved,self.opt,checkpoint_path=path)
            self.assertEqual(self.b.commands,[]);self.assertEqual(self.b.snapshot(),saved['baseline'])
            self.assertEqual(load_checkpoint(path)['scan_state']['completed'],1)


class QMAdaptiveTests(unittest.TestCase):
    def setUp(self):
        self.b=DispersionFixture();self.s=DispersionService(self.b,threading.Event())
        self.opt=DispersionOptions(fixed_actuators=tuple(n for n in QM_CANDIDATES if n not in ACTIVE[:2]))
        original=self.b.orbit
        def noisy():
            frame=original();frame['x']+=.0002*(1 if self.b.acquisitions%2 else -1);return frame
        self.b.orbit=noisy

    def test_probe_grows_in_steps_to_five_sigma_and_restores_each_pair(self):
        initial=self.b.snapshot();result=self.s.calculate(self.opt)
        self.assertEqual(self.b.snapshot(),initial)
        self.assertGreater(len(result['probe_attempts']),len(result['actuators']))
        for name in result['actuators']:
            attempts=[a for a in result['probe_attempts'] if a['magnet']==name]
            self.assertEqual(attempts[0]['half_span_A'],.01)
            for a,b in zip(attempts,attempts[1:]):self.assertEqual(b['half_span_A'],a['half_span_A']*2)
            self.assertTrue(attempts[-1]['quality']['usable'])
        self.assertTrue(all(q['snr']>=5 for q in result['probe_quality']))
        for q in result['probe_quality']:
            self.assertAlmostEqual(q['target_change_mm'],5*q['orbit_noise_rms_mm'])
            self.assertAlmostEqual(q['orbit_noise_rms_mm'],2*.0002/3.)
        for i in range(2,len(self.b.commands),3):self.assertEqual(self.b.commands[i][1],0.)

    def test_weak_response_at_attempt_limit_is_excluded_from_solver(self):
        self.b.dr[:,0]=0.;self.opt.maximum_probe_pairs=5
        result=self.s.calculate(self.opt)
        self.assertFalse(result['probe_quality'][0]['usable']);self.assertEqual(result['delta'][0],0.)
        self.assertNotIn(ACTIVE[0],result['selected']);self.assertIn(ACTIVE[1],result['selected'])

    def test_absolute_three_amp_limit_is_not_a_three_amp_delta(self):
        self.opt.fixed_actuators=tuple(n for n in QM_CANDIDATES if n!=ACTIVE[0])
        self.b.q[ACTIVE[0]]=2.9;self.b.quadrupoles[ACTIVE[0]]['limits_A']=[-5.,5.];self.b.dr[:,0]=0.
        def validate(n,values):
            if np.max(abs(np.asarray(values)))>5:raise ValueError('supply limit')
        with patch.object(self.b,'validate_quadrupole_targets',side_effect=validate):
            with self.assertRaisesRegex(ValueError,'No QM response reaches'):self.s.calculate(self.opt)
        commands=[v for n,v in self.b.commands if n==ACTIVE[0]]
        self.assertAlmostEqual(max(commands),3.);self.assertAlmostEqual(min(commands),-3.)
        self.assertAlmostEqual(self.b.q[ACTIVE[0]],2.9)

    def test_new_invalid_bpm_stops_growth_and_restores_current(self):
        original=self.b.orbit
        def drops():
            frame=original()
            if abs(self.b.q[ACTIVE[0]])>=.02:frame['valid'][20]=False
            return frame
        self.b.orbit=drops
        with self.assertRaisesRegex(ValueError,'New invalid BPMs'):self.s.calculate(self.opt)
        self.assertEqual(self.b.q[ACTIVE[0]],0.)
        self.assertLessEqual(max(abs(v) for _,v in self.b.commands),.02)

    def test_one_sided_probe_at_supply_limit_matches_known_response(self):
        self.opt.adaptive_probe=False;self.opt.step_A=.001
        self.b.quadrupoles[ACTIVE[0]]['limits_A']=[0.,2.]
        self.b.q[ACTIVE[1]]=2.
        result=self.s.calculate(self.opt)
        np.testing.assert_allclose(result['probe_readbacks'][0]['requested'],[0.,.01])
        np.testing.assert_allclose(result['probe_readbacks'][1]['requested'],[1.99,2.])
        for q in result['probe_readbacks']:self.assertEqual(q['scheme'],'asymmetric / one-sided')
        n=len(self.b.bpm_names)
        np.testing.assert_allclose(result['response'][:n],self.b.dr[:,[0,2]],atol=1.e-10)
        self.assertTrue(all(v>=0 for name,v in self.b.commands if name==ACTIVE[0]))
        self.assertEqual(self.b.q[ACTIVE[0]],0.);self.assertEqual(self.b.q[ACTIVE[1]],2.)

    def test_parameter_limits_take_precedence_and_probe_clips_both_endpoints(self):
        name=ACTIVE[0]
        self.b.quadrupoles[name].update(lower_limit_pv=name+':parameter.A',upper_limit_pv=name+':parameter.B')
        with patch.object(self.b,'get',create=True,side_effect=lambda pv:0. if pv.endswith('.A') else .015) as get:
            limits=quad_limits(self.b,name)
        self.assertEqual(limits,(0.,.015));self.assertEqual(get.call_count,2)
        targets,_=probe_pair(name,.009,limits,self.opt,.01)
        np.testing.assert_allclose(targets,[0.,.015])

    def test_probe_rereads_changed_parameter_limits_and_uses_clipped_span(self):
        self.opt.adaptive_probe=False;original=quad_limits;reads={}
        def limits(backend,name):
            reads[name]=reads.get(name,0)+1
            if name==ACTIVE[0] and reads[name]>1:return 0.,.006
            return original(backend,name)
        with patch('Knobs.DRTuning.dispersion.quad_limits',side_effect=limits):result=self.s.calculate(self.opt)
        np.testing.assert_allclose(result['probe_readbacks'][0]['requested'],[0.,.006])
        np.testing.assert_allclose(result['response'][:len(self.b.bpm_names),0],self.b.dr[:,0],atol=1.e-10)
        self.assertTrue(all(0<=v<=.006 for name,v in self.b.commands if name==ACTIVE[0]))

    def test_outside_probe_range_error_identifies_magnet_and_limits(self):
        self.b.q[ACTIVE[0]]=2.1
        with self.assertRaisesRegex(ValueError,r'QM1R\.1.*current=\+2.1.*supply='):
            self.s.calculate(self.opt)
        self.assertEqual(self.b.commands,[]);self.assertEqual(self.b.acquisitions,0)

    def test_default_cod_hard_limit_is_off_during_apply(self):
        result=self.s.calculate(self.opt);original=self.b.orbit
        def shifted():
            frame=original();frame['x']+=.2;return frame
        self.b.orbit=shifted;self.s.apply(result)
        self.assertTrue(result['applied']);self.assertFalse(result['options']['constrain_cod'])
        self.assertGreater(np.max(abs(result['full_after'][2:]-result['full_measured'][2:])),.05)


class QMCheckpointGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])

    def test_load_resume_buttons_preserve_small_probe_and_save_new_checkpoint(self):
        from Knobs.DRTuning.gui import TuningSession
        from Knobs.DRTuning.correction_gui import QFileDialog
        window=TuningSession(DispersionFixture());panel=window.qm_dispersion
        try:
            self.assertEqual(panel.probe.value(),.01);self.assertEqual(panel.probe_limit.value(),3.)
            self.assertFalse(panel.constrain_cod.isChecked());self.assertFalse(panel.cod.isEnabled())
            with tempfile.TemporaryDirectory() as directory:
                panel.autosave_directory=Path(directory)
                panel.excluded.setText(', '.join(n for n in QM_CANDIDATES if n not in ACTIVE))
                opt=panel.options();opt.probe_A=.1;path=Path(directory)/'old.checkpoint.json'
                def progress(_,message):
                    if message.startswith('QM dispersion response') and '(1/3)' in message:window.service.cancel.set()
                with self.assertRaises(Cancelled):panel.service.calculate(opt,progress,checkpoint_path=path)
                with patch.object(QFileDialog,'getOpenFileName',return_value=(str(path),'JSON')):panel.load_scan()
                self.assertEqual(panel.probe.value(),.01);self.assertTrue(panel.resume_button.isEnabled())
                self.assertFalse(panel.set_button.isEnabled());window.service.cancel.clear()
                with patch.object(window,'start',side_effect=lambda action,done,**_:done(action(lambda *_:None))):panel.resume_scan()
                self.assertFalse(panel.resume_button.isEnabled());self.assertTrue(panel.set_button.isEnabled())
                self.assertEqual(len(list(Path(directory).glob('*.checkpoint.json'))),2)
                self.assertIn('Scan checkpoint: 3/3',panel.save_status.text())
                for probe in panel.result['probe_readbacks'][1:]:np.testing.assert_allclose(probe['requested'],[-.01,.01])
        finally:window.close();self.app.processEvents()


if __name__=='__main__':unittest.main()
