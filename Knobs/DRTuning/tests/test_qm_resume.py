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
from Knobs.DRTuning.dispersion import DispersionService,DispersionOptions,QM_CANDIDATES,QM_PROBE_PRESETS_A,probe_pair,quad_limits
from Knobs.DRTuning.qm_checkpoint import load_checkpoint,save_checkpoint
from Knobs.DRTuning.tests.test_bump_dispersion import DispersionFixture

ACTIVE=('QM1R.1','QM3R.1','QM4R.1')


class QMResumeTests(unittest.TestCase):
    def setUp(self):
        self.b=DispersionFixture();self.cancel=threading.Event();self.s=DispersionService(self.b,self.cancel)
        self.opt=DispersionOptions(probe_A=.01,use_probe_presets=False,fixed_actuators=tuple(n for n in QM_CANDIDATES if n not in ACTIVE),adaptive_probe=False)

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

    def test_recalculate_saved_null_bpm_values_matches_in_memory_fit_without_io(self):
        data=self.s.calculate(self.opt);n=len(data['names']);idx=20
        data['valid'][idx]=False;data['acquisition_valid'][idx]=False
        data['full_measured'][:,idx]=np.nan
        data['response'][np.arange(4)*n+idx,:]=np.nan
        expected=self.s.recalculate(data,self.opt)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'null_bpms.json';save_checkpoint(path,data);saved=load_checkpoint(path)
            self.assertIsNone(saved['full_measured'][0][idx])
            commands=list(self.b.commands);reads=self.b.acquisitions
            result=self.s.recalculate(saved,self.opt)
            np.testing.assert_allclose(result['delta'],expected['delta'])
            self.assertFalse(result['valid'][idx]);self.assertEqual(self.b.commands,commands)
            self.assertEqual(self.b.acquisitions,reads)
            # Missing data must still fail if its BPM is marked valid.
            saved['acquisition_valid'][idx]=True
            with self.assertRaisesRegex(ValueError,'Invalid measured QM response'):
                self.s.recalculate(saved,self.opt)

    def test_resume_with_saved_null_bpm_values_retains_committed_mask_and_response(self):
        with tempfile.TemporaryDirectory() as directory:
            saved=self.interrupt_after_first(Path(directory)/'scan.json');n=len(saved['names']);idx=20
            saved['valid'][idx]=False;saved['scan_state']['common_valid'][idx]=False
            for row in saved['full_measured']:row[idx]=None
            for row in np.arange(4)*n+idx:saved['scan_state']['columns'][0][row]=None
            self.cancel.clear();self.b.commands=[]
            result=self.s.resume(saved,self.opt)
            self.assertEqual(result['scan_state']['status'],'complete')
            self.assertFalse(result['valid'][idx]);self.assertIn('delta',result)
            self.assertNotIn(ACTIVE[0],[name for name,_ in self.b.commands])
            self.assertTrue(np.isnan(result['resume_checks'][0]['change_from_previous'][:,idx]).all())

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
        self.opt=DispersionOptions(probe_A=.01,use_probe_presets=False,fixed_actuators=tuple(n for n in QM_CANDIDATES if n not in ACTIVE[:2]),characterize_first_scan=False)
        original=self.b.orbit
        def noisy():
            frame=original();frame['x']+=.0002*(1 if self.b.acquisitions%2 else -1);return frame
        self.b.orbit=noisy

    def test_preset_starts_then_rescales_weak_response_in_same_scan(self):
        self.opt.use_probe_presets=True
        original=self.b.orbit
        def weak():
            frame=original();frame['x']+=.002*(1 if self.b.acquisitions%2 else -1);return frame
        self.b.orbit=weak
        result=self.s.calculate(self.opt)
        for name in result['actuators']:
            attempts=[a for a in result['probe_attempts'] if a['magnet']==name]
            self.assertEqual(attempts[0]['half_span_A'],QM_PROBE_PRESETS_A[name])
            self.assertFalse(attempts[0]['quality']['usable']);self.assertGreater(len(attempts),1)
            self.assertGreater(attempts[1]['half_span_A'],attempts[0]['half_span_A'])
            self.assertTrue(attempts[-1]['quality']['usable'])
        self.assertIn('delta',result)

    def test_preset_invalid_response_shrinks_and_live_limits_clip_each_pair(self):
        self.opt.use_probe_presets=True
        self.b.quadrupoles[ACTIVE[0]]['limits_A']=[-.1,.05]
        original=self.b.orbit
        def invalid():
            frame=original()
            if abs(self.b.q[ACTIVE[0]])>.080001:frame['valid'][20]=False
            return frame
        self.b.orbit=invalid
        result=self.s.calculate(self.opt)
        attempts=[a for a in result['probe_attempts'] if a['magnet']==ACTIVE[0]]
        self.assertEqual([a['half_span_A'] for a in attempts],[.16,.08])
        np.testing.assert_allclose(attempts[0]['requested'],[-.1,.05])
        np.testing.assert_allclose(attempts[1]['requested'],[-.08,.05])
        self.assertTrue(attempts[0]['rejected']);self.assertTrue(attempts[1]['quality']['usable'])
        self.assertTrue(all(-.1<=value<=.05 for n,value in self.b.commands if n==ACTIVE[0]))
        self.assertEqual(self.b.q[ACTIVE[0]],0.)

    def test_new_presets_do_not_reuse_old_uniform_scan_cache(self):
        old=self.s.calculate(self.opt)
        self.opt.use_probe_presets=True
        result=self.s.calculate(self.opt)
        for name in result['actuators']:
            first=next(a for a in result['probe_attempts'] if a['magnet']==name)
            self.assertEqual(first['half_span_A'],QM_PROBE_PRESETS_A[name])
            self.assertNotEqual(first['half_span_A'],old['successful_probe_widths'][name]['width_A'])
        newer=self.s.calculate(self.opt)
        for name in newer['actuators']:
            first=next(a for a in newer['probe_attempts'] if a['magnet']==name)
            self.assertEqual(first['half_span_A'],result['successful_probe_widths'][name]['width_A'])

    def test_probe_grows_in_steps_to_five_sigma_and_restores_each_pair(self):
        initial=self.b.snapshot();result=self.s.calculate(self.opt)
        self.assertEqual(self.b.snapshot(),initial)
        self.assertGreater(len(result['probe_attempts']),len(result['actuators']))
        for name in result['actuators']:
            attempts=[a for a in result['probe_attempts'] if a['magnet']==name]
            self.assertEqual(attempts[0]['half_span_A'],.01)
            for a,b in zip(attempts,attempts[1:]):
                self.assertLessEqual(b['half_span_A'],a['half_span_A']*self.opt.probe_max_growth+1.e-9)
                self.assertAlmostEqual(b['half_span_A']/.01,round(b['half_span_A']/.01))
            self.assertTrue(attempts[-1]['quality']['usable'])
        self.assertEqual(result['probe_attempts'][1]['half_span_A'],.03)
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

    def test_probe_uses_hardware_limits_without_absolute_three_amp_cap(self):
        self.opt.fixed_actuators=tuple(n for n in QM_CANDIDATES if n!=ACTIVE[0])
        self.b.q[ACTIVE[0]]=2.9;self.b.quadrupoles[ACTIVE[0]]['limits_A']=[-5.,5.];self.b.dr[:,0]=0.
        def validate(n,values):
            if np.max(abs(np.asarray(values)))>5:raise ValueError('supply limit')
        with patch.object(self.b,'validate_quadrupole_targets',side_effect=validate):
            with self.assertRaisesRegex(ValueError,'No QM response reaches'):self.s.calculate(self.opt)
        commands=[v for n,v in self.b.commands if n==ACTIVE[0]]
        self.assertAlmostEqual(max(commands),5.);self.assertAlmostEqual(min(commands),-5.)
        self.assertAlmostEqual(self.b.q[ACTIVE[0]],2.9)

    def test_new_invalid_bpm_shrinks_and_does_not_revisit_failed_width(self):
        original=self.b.orbit
        def drops():
            frame=original()
            if abs(self.b.q[ACTIVE[0]])>=.02:frame['valid'][20]=False
            return frame
        self.b.orbit=drops
        result=self.s.calculate(self.opt)
        self.assertEqual(self.b.q[ACTIVE[0]],0.)
        attempts=[a for a in result['probe_attempts'] if a['magnet']==ACTIVE[0]]
        failures=[(i,a) for i,a in enumerate(attempts) if a.get('rejected')]
        self.assertTrue(failures)
        for i,a in failures:
            for later in attempts[i+1:]:self.assertLess(later['half_span_A'],a['half_span_A'])
        self.assertNotIn(ACTIVE[0],result['selected'])

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
        np.testing.assert_allclose(targets,[0.,.01])

    def test_probe_rereads_changed_parameter_limits_and_skips_sub_resolution_range(self):
        self.opt.adaptive_probe=False;original=quad_limits;reads={}
        def limits(backend,name):
            reads[name]=reads.get(name,0)+1
            if name==ACTIVE[0] and reads[name]>1:return 0.,.006
            return original(backend,name)
        with patch('Knobs.DRTuning.dispersion.quad_limits',side_effect=limits):result=self.s.calculate(self.opt)
        self.assertTrue(result['probe_readbacks'][0]['skipped'])
        self.assertFalse(result['response_column_measured'][0])
        self.assertNotIn(ACTIVE[0],[n for n,_ in self.b.commands])

    def test_unrestorable_current_is_skipped_in_scan_solve_apply_and_reset(self):
        self.b.q[ACTIVE[0]]=2.1
        result=self.s.calculate(self.opt)
        self.assertEqual(result['skipped_qms'][0]['magnet'],ACTIVE[0])
        self.assertIn('current=+2.1',result['skipped_qms'][0]['reason'])
        self.assertFalse(result['response_column_measured'][0]);self.assertIsNone(result['probe_quality'][0]['signal_rms'])
        self.assertEqual(result['delta'][0],0.);self.assertNotIn(ACTIVE[0],result['selected'])
        # Fixed-probe refitting must also exclude the unmeasured placeholder.
        self.opt.adaptive_probe=False;result=self.s.recalculate(result,self.opt)
        self.assertNotIn(ACTIVE[0],result['selected'])
        self.s.apply(result);self.s.reset(result)
        self.assertEqual(self.b.q[ACTIVE[0]],2.1)
        self.assertNotIn(ACTIVE[0],[n for n,_ in self.b.commands])

    def test_probe_above_three_amps_stays_centered_and_restores_original_current(self):
        self.opt.adaptive_probe=False
        for center,expected in [(3.2,[3.19,3.21]),(-3.2,[-3.21,-3.19])]:
            with self.subTest(center=center):
                self.b.q[ACTIVE[0]]=center;self.b.quadrupoles[ACTIVE[0]]['limits_A']=[-5.,5.]
                self.b.commands=[]
                def validate(name,values):
                    if np.max(abs(np.asarray(values)))>5:raise ValueError('supply limit')
                with patch.object(self.b,'validate_quadrupole_targets',side_effect=validate):result=self.s.calculate(self.opt)
                currents=[v for name,v in self.b.commands if name==ACTIVE[0]]
                np.testing.assert_allclose(currents,expected+[center])
                self.assertEqual(result['probe_readbacks'][0]['scheme'],'symmetric')
                np.testing.assert_allclose(result['response'][:len(self.b.bpm_names),0],self.b.dr[:,0],atol=1.e-10)
                self.assertEqual(self.b.q[ACTIVE[0]],center)

    def test_narrow_supply_range_skip_is_checkpointed_and_resume_continues_other_qms(self):
        self.opt.adaptive_probe=False
        self.b.q[ACTIVE[0]]=.002;self.b.quadrupoles[ACTIVE[0]]['limits_A']=[0.,.005]
        def cancel_after_skip(_,message):
            if message.startswith('QM dispersion response') and '(1/2)' in message:self.s.reader.cancel.set()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'skip.checkpoint.json'
            with self.assertRaises(Cancelled):self.s.calculate(self.opt,cancel_after_skip,checkpoint_path=path)
            saved=load_checkpoint(path);self.s.validate_checkpoint(saved)
            self.assertEqual(saved['scan_state']['completed'],1)
            self.assertEqual(len(saved['orbit_measurements']),3);self.assertTrue(saved['probe_readbacks'][0]['skipped'])
            resumed=DispersionService(self.b,threading.Event()).resume(saved,self.opt)
            self.assertEqual(resumed['scan_state']['status'],'complete')
            self.assertNotIn(ACTIVE[0],resumed['selected'])
            self.assertNotIn(ACTIVE[0],[n for n,_ in self.b.commands])

    def test_all_ranges_unavailable_produce_no_proposal_and_no_qm_writes(self):
        for name in ACTIVE[:2]:
            self.b.q[name]=.002;self.b.quadrupoles[name]['limits_A']=[0.,.005]
        with self.assertRaisesRegex(ValueError,'No measurable QM response'):self.s.calculate(self.opt)
        self.assertEqual(self.b.commands,[]);self.assertEqual(len(self.s.last_result['skipped_qms']),2)
        self.assertNotIn('delta',self.s.last_result)

    def test_unresolved_readback_span_restores_and_skips_without_dividing_by_zero(self):
        original=self.b.quadrupole_current
        with patch.object(self.b,'quadrupole_current',side_effect=lambda n:0. if n==ACTIVE[0] else original(n)):
            result=self.s.calculate(self.opt)
        self.assertIn('Unresolved actual',result['skipped_qms'][0]['reason'])
        self.assertEqual(self.b.q[ACTIVE[0]],0.)
        self.assertNotIn(ACTIVE[0],result['selected']);self.assertIn(ACTIVE[1],result['selected'])
        self.assertTrue(np.isfinite(result['response']).all())

    def test_default_cod_hard_limit_is_off_during_apply(self):
        result=self.s.calculate(self.opt);original=self.b.orbit
        def shifted():
            frame=original();frame['x']+=.2;return frame
        self.b.orbit=shifted;self.s.apply(result)
        self.assertTrue(result['applied']);self.assertFalse(result['options']['constrain_cod'])
        self.assertGreater(np.max(abs(result['full_after'][2:]-result['full_measured'][2:])),.05)

    def test_invalid_probe_halves_on_hundredth_amp_grid_then_accepts_first_good_pair(self):
        for initial,expected in [(3.,[3.,1.5,.75,.37,.18,.09,.04,.02,.01]),
                                 (1.,[1.,.5,.25,.12,.06,.03,.01]),(.05,[.05,.02,.01])]:
            with self.subTest(initial=initial):
                self.opt.probe_A=initial;self.s.successful_widths={};self.b.commands=[]
                def orbit():
                    frame=DispersionFixture.orbit(self.b)
                    frame['x']+=.00001*(1 if self.b.acquisitions%2 else -1)
                    if abs(self.b.q[ACTIVE[0]])>.010001:frame['valid'][20]=False
                    return frame
                self.b.orbit=orbit
                result=self.s.calculate(self.opt)
                attempts=[a for a in result['probe_attempts'] if a['magnet']==ACTIVE[0]]
                self.assertEqual([a['half_span_A'] for a in attempts],expected)
                self.assertTrue(attempts[-1]['quality']['usable']);self.assertTrue(attempts[-1]['accepted'])
                self.assertTrue(result['valid'][20]);self.assertEqual(self.b.q[ACTIVE[0]],0.)
                currents=[v for n,v in self.b.commands if n==ACTIVE[0]]
                self.assertEqual(currents, [v for h in expected[:-1] for v in (-min(h,2.),0.)]+[-.01,.01,0.])

    def test_invalid_at_minimum_is_skipped_without_sub_resolution_commands(self):
        self.opt.probe_A=.05
        def orbit():
            frame=DispersionFixture.orbit(self.b)
            if self.b.q[ACTIVE[0]]!=0:frame['valid'][20]=False
            return frame
        self.b.orbit=orbit;result=self.s.calculate(self.opt)
        attempts=[a for a in result['probe_attempts'] if a['magnet']==ACTIVE[0]]
        self.assertEqual([a['half_span_A'] for a in attempts],[.05,.02,.01])
        self.assertTrue(result['probe_readbacks'][0]['skipped']);self.assertNotIn(ACTIVE[0],result['selected'])
        self.assertTrue(all(v==0 or abs(v)>=.01 for _,v in self.b.commands))

    def test_cancel_during_shrink_stops_before_retry(self):
        self.opt.probe_A=.05
        original=self.b.orbit
        def orbit():
            frame=original()
            if self.b.q[ACTIVE[0]]!=0:frame['valid'][20]=False
            return frame
        self.b.orbit=orbit
        def progress(_,message):
            if 'rejected at' in message:self.s.reader.cancel.set()
        with self.assertRaises(Cancelled):self.s.calculate(self.opt,progress)
        self.assertEqual(self.b.commands,[(ACTIVE[0],-.05),(ACTIVE[0],0.)])

    def test_restore_failure_is_not_treated_as_a_retriable_invalid_probe(self):
        self.opt.probe_A=.05;original=self.b.orbit;write=self.b.set_quadrupole_current
        def orbit():
            frame=original()
            if self.b.q[ACTIVE[0]]!=0:frame['valid'][20]=False
            return frame
        def failing_restore(n,value):
            if n==ACTIVE[0] and value==0:raise RuntimeError('restore failed')
            return write(n,value)
        self.b.orbit=orbit
        with patch.object(self.b,'set_quadrupole_current',side_effect=failing_restore):
            with self.assertRaisesRegex(RuntimeError,'restore failed'):self.s.calculate(self.opt)
        self.assertEqual(self.b.commands,[(ACTIVE[0],-.05)])
        self.assertIn('restoration_error',self.s.last_result)

    def test_successful_widths_reused_only_with_matching_settings_and_user_option(self):
        first=self.s.calculate(self.opt)
        self.assertGreater(len(first['probe_attempts']),2)
        second=self.s.calculate(self.opt)
        self.assertEqual(len(second['probe_attempts']),2)
        for a in second['probe_attempts']:
            self.assertEqual(a['half_span_A'],first['successful_probe_widths'][a['magnet']]['width_A'])
        self.opt.reuse_probe_widths=False
        fresh=self.s.calculate(self.opt)
        self.assertEqual(fresh['probe_attempts'][0]['half_span_A'],.01);self.assertGreater(len(fresh['probe_attempts']),2)
        self.opt.reuse_probe_widths=True
        self.b.currents[self.b.hcorrectors[0]]+=.01
        changed=self.s.calculate(self.opt)
        self.assertEqual(changed['probe_attempts'][0]['half_span_A'],.01)

    def test_successful_width_cache_round_trips_through_checkpoint(self):
        first=self.s.calculate(self.opt)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'scan.json';save_checkpoint(path,first)
            service=DispersionService(self.b,threading.Event());service.restore_width_cache(load_checkpoint(path))
            result=service.calculate(self.opt)
        self.assertEqual(len(result['probe_attempts']),2)

    def test_first_sweep_measures_once_then_next_sweep_targets_estimated_widths(self):
        self.opt.probe_A=3.;self.opt.characterize_first_scan=True
        first=self.s.calculate(self.opt)
        self.assertEqual(len(first['probe_attempts']),2)
        self.assertTrue(all(a['half_span_A']==3. for a in first['probe_attempts']))
        self.assertTrue(all(e['width_A']<3. for e in first['successful_probe_widths'].values()))
        second=self.s.calculate(self.opt)
        self.assertEqual(len(second['probe_attempts']),2)
        for a in second['probe_attempts']:
            self.assertEqual(a['half_span_A'],first['successful_probe_widths'][a['magnet']]['width_A'])
            self.assertTrue(a['quality']['usable'])
        np.testing.assert_allclose(first['response'],second['response'],atol=1.e-10)

    def test_weak_first_sweep_saves_calibration_without_apply_then_next_sweep_resolves_it(self):
        self.opt.characterize_first_scan=True
        first=self.s.calculate(self.opt)
        self.assertEqual(first['scan_state']['status'],'complete')
        self.assertEqual(len(first['probe_attempts']),2);self.assertNotIn('delta',first)
        self.assertIn('proposal_unavailable_reason',first);self.assertNotIn('error',first)
        self.assertTrue(all(e['width_A']>.01 for e in first['successful_probe_widths'].values()))
        second=self.s.calculate(self.opt)
        self.assertIn('delta',second);self.assertNotIn('proposal_unavailable_reason',second)
        self.assertTrue(all(q['usable'] for q in second['probe_quality']))

    def test_legacy_absolute_cap_is_ignored_and_per_apply_delta_is_limited(self):
        self.opt.fixed_actuators=tuple(n for n in QM_CANDIDATES if n!=ACTIVE[0])
        self.opt.probe_current_limit_A=.3;self.opt.step_A=.1
        for sign in (-1,1):
            with self.subTest(sign=sign):
                self.b.q[ACTIVE[0]]=sign*.8;self.b.d0=-self.b.dr[:,0]*(sign*1.5);self.b.commands=[]
                result=self.s.calculate(self.opt)
                self.assertAlmostEqual(result['centers'][0]+result['delta'][0],sign*.9,places=6)
                self.assertAlmostEqual(abs(result['delta'][0]),self.opt.step_A)
                self.s.apply(result)
                self.assertAlmostEqual(self.b.q[ACTIVE[0]],sign*.9,places=6)
                self.assertTrue(all(abs(v)<=2.+1.e-9 for _,v in self.b.commands))
                self.s.reset(result)


class QMCheckpointGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])

    def test_load_checkpoint_with_unknown_bpm_positions_uses_index_without_changing_data(self):
        from Knobs.DRTuning.gui import TuningSession
        from Knobs.DRTuning.correction_gui import QFileDialog
        window=TuningSession(DispersionFixture());panel=window.qm_dispersion
        try:
            result=panel.service.calculate(panel.options())
            result['positions_m'][-1]=np.nan
            result['valid'][-1]=False
            result['full_measured'][:,-1]=np.nan
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'missing_positions.checkpoint.json'
                save_checkpoint(path,result);original=path.read_bytes()
                for has_proposal in (True,False):
                    if not has_proposal:
                        result.pop('delta');save_checkpoint(path,result);original=path.read_bytes()
                    with patch.object(QFileDialog,'getOpenFileName',return_value=(str(path),'JSON')),patch.object(window,'error') as error:
                        panel.load_scan()
                    error.assert_not_called()
                    self.assertIsNone(panel.result['positions_m'][-1])
                    self.assertIsNone(panel.result['full_measured'][0][-1])
                    self.assertEqual(path.read_bytes(),original)
                    for ax in panel.plot.figure.axes:
                        self.assertEqual(ax.get_xlabel(),'BPM index (measurement order)')
                        np.testing.assert_equal(ax.lines[0].get_xdata(),np.arange(1,len(result['names'])+1))
                    self.assertEqual(panel.set_button.isEnabled(),has_proposal)
        finally:window.close();self.app.processEvents()

    def test_load_resume_buttons_preserve_current_initial_setting_and_save_new_checkpoint(self):
        from Knobs.DRTuning.gui import TuningSession
        from Knobs.DRTuning.correction_gui import QFileDialog
        window=TuningSession(DispersionFixture());panel=window.qm_dispersion
        try:
            self.assertEqual(panel.probe.value(),.1);self.assertEqual(panel.step.value(),3.)
            self.assertFalse(panel.options().constrain_cod)
            with tempfile.TemporaryDirectory() as directory:
                panel.autosave_directory=Path(directory)
                panel.excluded.setText(', '.join(n for n in QM_CANDIDATES if n not in ACTIVE))
                opt=panel.options();opt.probe_A=.05;path=Path(directory)/'old.checkpoint.json'
                def progress(_,message):
                    if message.startswith('QM dispersion response') and '(1/3)' in message:window.service.cancel.set()
                with self.assertRaises(Cancelled):panel.service.calculate(opt,progress,checkpoint_path=path)
                with patch.object(QFileDialog,'getOpenFileName',return_value=(str(path),'JSON')):panel.load_scan()
                self.assertEqual(panel.probe.value(),.1);self.assertTrue(panel.resume_button.isEnabled())
                self.assertFalse(panel.set_button.isEnabled());window.service.cancel.clear()
                with patch.object(window,'start',side_effect=lambda action,done,**_:done(action(lambda *_:None))):panel.resume_scan()
                self.assertFalse(panel.resume_button.isEnabled());self.assertTrue(panel.set_button.isEnabled())
                self.assertEqual(len(list(Path(directory).glob('*.checkpoint.json'))),2)
                self.assertIn('Scan checkpoint: 3/3',panel.save_status.text())
                for probe in panel.result['probe_readbacks'][1:]:
                    width=QM_PROBE_PRESETS_A[probe['magnet']]
                    np.testing.assert_allclose(probe['requested'],[-width,width])
        finally:window.close();self.app.processEvents()

    def test_skipped_qm_is_visible_in_partial_and_complete_tables(self):
        from Knobs.DRTuning.gui import TuningSession
        window=TuningSession(DispersionFixture());panel=window.qm_dispersion
        try:
            window.backend.q[ACTIVE[0]]=.002
            window.backend.quadrupoles[ACTIVE[0]]['limits_A']=[0.,.005]
            result=panel.service.calculate(panel.options());panel.display(result)
            self.assertIn('Skipped QM: '+ACTIVE[0],panel.status.text())
            self.assertTrue(panel.table.item(0,4).text().startswith('Skipped:'))
            self.assertEqual(panel.table.item(0,5).text(),'Not measured')
            partial=copy.deepcopy(result);partial.pop('delta');partial['scan_state']['status']='interrupted'
            panel.display(partial)
            self.assertEqual(panel.table.item(0,1).text(),'Skipped')
        finally:window.close();self.app.processEvents()

    def test_weak_calibration_displays_next_width_and_disables_apply(self):
        from Knobs.DRTuning.gui import TuningSession
        backend=DispersionFixture();original=backend.orbit
        def noisy():
            frame=original();frame['x']+=.0002*(1 if backend.acquisitions%2 else -1);return frame
        backend.orbit=noisy;window=TuningSession(backend);panel=window.qm_dispersion
        try:
            panel.probe.setValue(.01)
            panel.excluded.setText(', '.join(n for n in QM_CANDIDATES if n not in ACTIVE[:2]))
            old_options=panel.options();old_options.use_probe_presets=False;old_options.characterize_first_scan=True
            result=panel.service.calculate(old_options);panel.display(result)
            self.assertFalse(panel.set_button.isEnabled());self.assertFalse(panel.resume_button.isEnabled())
            self.assertIn('S/N below target',panel.status.text())
            self.assertEqual(panel.table.horizontalHeaderItem(3).text(),'Next probe [A]')
            self.assertGreater(float(panel.table.item(0,3).text()),.01)
            panel.target_snr.setValue(1.)
            panel.display(panel.service.recalculate(result,panel.options()))
            self.assertNotIn('proposal_unavailable_reason',panel.result)
            self.assertTrue(panel.set_button.isEnabled())
        finally:window.close();self.app.processEvents()


if __name__=='__main__':unittest.main()
