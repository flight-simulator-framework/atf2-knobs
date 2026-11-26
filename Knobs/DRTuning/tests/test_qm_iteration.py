"""Reuse measured QM responses across verified corrections, without new probes."""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np

from Knobs.DRCorrection.core import serial
from Knobs.DRTuning.dispersion import DispersionService,DispersionOptions,QM_CANDIDATES
from Knobs.DRTuning.tests.test_bump_dispersion import DispersionFixture


class QMIterationTests(unittest.TestCase):
    def setUp(self):
        self.b=DispersionFixture();self.s=DispersionService(self.b,threading.Event())
        self.opt=DispersionOptions(probe_A=.01,use_probe_presets=False,adaptive_probe=False,
                                   regularization=.3,fixed_actuators=(),measure_tunes=True)

    def test_qm_cod_change_rows_only_use_the_six_selected_monitors(self):
        from Knobs.DRTuning.dispersion import qm_objective,straight_bpms
        names=[f'dr:MB{i}R' for i in range(1,97)]
        before=np.arange(4*96).reshape(4,96).astype(float)
        data=dict(names=names,full_measured=before,straight=straight_bpms(names))
        valid=np.ones(96,bool)
        target,weights,_=qm_objective(data,self.opt,valid)
        for row in weights[2:]:
            np.testing.assert_array_equal(np.flatnonzero(row)+1,[19,22,23,24,65,66])
        np.testing.assert_array_equal(target[2:],before[2:])

    def test_repeated_apply_uses_measured_residual_and_currents_without_rescanning(self):
        original=self.b.snapshot();data=self.s.calculate(self.opt);response=data['response'].copy()
        norms=[np.linalg.norm(data['full_measured'][0])]
        for iteration in range(3):
            self.s.apply(data)
            norms.append(np.linalg.norm(data['full_after'][0]))
            self.assertTrue(data['applied']);self.assertLess(norms[-1],norms[-2])
            applied=copy.deepcopy(data);currents=self.b.snapshot()
            with ExitStack() as stack:
                for method in ('orbit','snapshot','quadrupole_current','set_quadrupole_current',
                               'measure_tunes','set_frequency'):
                    stack.enter_context(patch.object(self.b,method,side_effect=AssertionError(method)))
                data=self.s.recalculate(data,self.opt)
            self.assertEqual(data['iteration_index'],iteration+1)
            self.assertEqual(len(data['iteration_history']),iteration+1)
            self.assertEqual(data['baseline'],currents)
            np.testing.assert_array_equal(data['response'],response)
            np.testing.assert_array_equal(data['full_measured'],applied['full_after'])
            np.testing.assert_array_equal(data['centers'],applied['applied_currents_A'])
            np.testing.assert_array_equal(data['initial_tunes'],applied['after_tunes'])
            np.testing.assert_allclose(data['full_predicted'],
                applied['full_after']+(response@data['delta']).reshape(applied['full_after'].shape))
            self.assertTrue(applied['applied']);self.assertFalse(data['applied'])
        # Restore remains available even when the next proposal has only been prepared.
        self.s.reset(data);self.assertEqual(self.b.snapshot(),original)
        self.assertNotIn('delta',data)
        reset_fit=self.s.recalculate(data,self.opt)
        self.assertEqual(reset_fit['baseline'],original)
        self.assertEqual(reset_fit['iteration_index'],0)

    def test_saved_matrix_on_current_beam_uses_live_limits_masks_and_restore_origin(self):
        self.opt.measure_tunes=False
        name=QM_CANDIDATES[0]
        self.opt.fixed_actuators=QM_CANDIDATES[1:]
        self.b.d0=-self.b.dr[:,0]
        source=self.s.calculate(self.opt,group_rf=True)
        self.s.apply(source)
        source=self.s.recalculate(source,self.opt)
        self.s.apply(source)
        saved=json.loads(json.dumps(serial(source),allow_nan=False))
        self.b.q[name]=.25
        self.b.offset=300.
        self.b.d0=-1.5*self.b.dr[:,0]
        self.b.quadrupoles[name]['limits_A']=[-.1,.7]
        original=self.b.snapshot()
        lost,missing=np.flatnonzero(source['straight'])[:2]
        saved['iteration_origin']['acquisition_valid'][lost]=False
        orbit=self.b.orbit
        def dropping():
            frame=orbit();frame['valid'][missing]=False;return frame
        self.b.commands.clear();before=self.b.acquisitions
        with patch.object(self.b,'orbit',side_effect=dropping),patch.object(self.b,'set_frequency',wraps=self.b.set_frequency) as rf,patch.object(self.s.reader.cancel,'wait',return_value=False) as wait:
            result=self.s.reuse_on_current_beam(saved,self.opt)
        self.assertEqual([c.args[0] for c in rf.call_args_list],[-1700.,2300.,300.])
        wait.assert_called_once_with(3.)
        self.assertEqual(self.b.acquisitions-before,40)
        self.assertEqual(self.b.commands,[])
        self.assertEqual(result['baseline'],original)
        self.assertEqual(result['centers'][0],.25)
        self.assertEqual(result['response_origin']['baseline']['quadrupoles'][name],0.)
        self.assertNotIn('iteration_history',result)
        self.assertTrue(saved['applied']);self.assertFalse(result['applied'])
        self.assertFalse(result['valid'][lost]);self.assertFalse(result['valid'][missing])
        valid=result['valid']
        np.testing.assert_allclose(result['full_measured'][0,valid],-1.25*self.b.dr[valid,0])
        np.testing.assert_array_equal(result['response'],source['response'])
        self.assertAlmostEqual(result['delta'][0],.45)
        self.s.apply(result)
        self.assertAlmostEqual(self.b.q[name],.7)
        again=self.s.recalculate(result,self.opt)
        self.s.reset(again)
        self.assertEqual(self.b.snapshot(),original)
        fresh=self.s.reuse_on_current_beam(again,self.opt)
        self.assertFalse(fresh['valid'][lost]);self.assertTrue(fresh['valid'][missing])
        self.s.validate_checkpoint(json.loads(json.dumps(serial(fresh),allow_nan=False)))

    def test_current_baseline_detects_incompatible_file_and_changes_during_or_after_read(self):
        self.opt.measure_tunes=False
        source=self.s.calculate(self.opt,group_rf=True)
        source['profile_signature']='different'
        with patch.object(self.b,'set_frequency',side_effect=AssertionError),self.assertRaisesRegex(ValueError,'profile'):
            self.s.reuse_on_current_beam(source,self.opt)
        source['profile_signature']=self.s.profile_signature()
        n=source['actuators'][0]
        def drift(opt):
            self.b.q[n]+=.2
            return signal(opt)
        signal=self.s.signal
        with patch.object(self.s,'signal',side_effect=drift),self.assertRaisesRegex(ValueError,'settings changed'):
            self.s.reuse_on_current_beam(source,self.opt)
        proposal=self.s.reuse_on_current_beam(source,self.opt)
        self.b.q[n]+=.01
        self.b.commands.clear()
        with self.assertRaisesRegex(ValueError,'Settings changed'):self.s.apply(proposal)
        self.assertEqual(self.b.commands,[])

    def test_arc_objective_uses_physical_mb_parity_and_refits_without_beam(self):
        from Knobs.DRTuning.dispersion import arc_bpms
        names=['MB0R','dr:MB1R','MB18R','MB19R','MB31R','MB32R','MB65R',
               'MB66R','MB80R','MB81R','MB96R','MB97R','other']
        self.assertEqual(np.flatnonzero(arc_bpms(names,'odd')).tolist(),[1,6,9])
        self.assertEqual(np.flatnonzero(arc_bpms(names,'even')).tolist(),[2,5,10])
        self.opt.fixed_actuators=QM_CANDIDATES[1:];self.opt.measure_tunes=False
        odd=arc_bpms(self.b.bpm_names,'odd');even=arc_bpms(self.b.bpm_names,'even')
        self.b.dr[odd|even,0]=1.
        self.b.d0[odd]=.4;self.b.d0[even]=-.3
        self.opt.regularization=0.
        data=self.s.calculate(self.opt,group_rf=True)
        self.opt.arc_weight=1.;self.opt.dispersion_weight=0.;self.opt.orbit_weight=0.
        self.opt.excluded_bpms=(self.b.bpm_names[0],)
        with patch.object(self.b,'orbit',side_effect=AssertionError),patch.object(self.b,'set_frequency',side_effect=AssertionError):
            first=self.s.recalculate(data,self.opt)
            self.assertAlmostEqual(first['delta'][0],-.4)
            self.assertNotIn(self.b.bpm_names[0],first['arc_bpms'])
            self.opt.arc_parity='even'
            second=self.s.recalculate(data,self.opt)
            self.assertAlmostEqual(second['delta'][0],.3)
            self.assertEqual(set(second['arc_bpms']),set(np.asarray(self.b.bpm_names)[even]))
        self.s.apply(second)
        self.assertTrue(second['applied'])
        np.testing.assert_allclose(second['full_after'][0,even],0.,atol=1e-10)
        self.opt.excluded_bpms=tuple(np.asarray(self.b.bpm_names)[even])
        with self.assertRaisesRegex(ValueError,'No valid even arc'):self.s.recalculate(data,self.opt)

    def test_three_amp_steps_accumulate_until_the_hardware_limit(self):
        name=QM_CANDIDATES[0]
        self.b.quadrupoles[name]['limits_A']=[-5.,5.]
        def validate(n,values):
            lo,hi=self.b.quadrupoles[n]['limits_A']
            if np.any(np.asarray(values)<lo) or np.any(np.asarray(values)>hi):raise ValueError('QM limits')
        self.b.validate_quadrupole_targets=validate
        self.b.d0=-6.*self.b.dr[:,0]
        self.opt.fixed_actuators=QM_CANDIDATES[1:];self.opt.measure_tunes=False
        data=self.s.calculate(self.opt);self.s.apply(data)
        self.assertAlmostEqual(self.b.q[name],3.)
        data=self.s.recalculate(data,self.opt)
        self.assertAlmostEqual(data['centers'][0]+data['delta'][0],5.)
        self.assertLessEqual(abs(data['delta'][0]),3.+1.e-9)
        self.s.apply(data);self.assertAlmostEqual(self.b.q[name],5.)
        data=self.s.recalculate(data,self.opt)
        self.assertLess(abs(data['delta'][0]),1.e-8)
        self.assertTrue(all(abs(value)<=5.+1.e-9 for _,value in self.b.commands))

    def test_apply_rejects_excessive_delta_before_writes(self):
        data=self.s.calculate(self.opt);data['delta'][0]=self.opt.step_A+.1
        self.b.commands=[];reads=self.b.acquisitions
        with self.assertRaisesRegex(ValueError,'per-Apply current change limit'):self.s.apply(data)
        self.assertEqual(self.b.commands,[]);self.assertEqual(self.b.acquisitions,reads)

    def test_external_changes_block_next_apply_before_any_write(self):
        data=self.s.calculate(self.opt);self.s.apply(data)
        data=self.s.recalculate(data,self.opt)
        for change in ('qm','rf','corrector'):
            with self.subTest(change=change):
                baseline=self.b.snapshot()
                if change=='qm':self.b.q[QM_CANDIDATES[0]]+=.01
                elif change=='rf':self.b.offset+=100.
                else:self.b.currents[next(iter(self.b.currents))]+=.01
                self.b.commands=[];reads=self.b.acquisitions
                with self.assertRaisesRegex(ValueError,'Settings changed'):self.s.apply(data)
                self.assertEqual(self.b.commands,[]);self.assertEqual(self.b.acquisitions,reads)
                self.b.q=baseline['quadrupoles'];self.b.currents=baseline['correctors'];self.b.offset=baseline['frequency']

    def test_failed_next_apply_restores_previous_success_and_reset_restores_origin(self):
        original=self.b.snapshot();data=self.s.calculate(self.opt);self.s.apply(data)
        previous=self.b.snapshot();data=self.s.recalculate(data,self.opt);self.b.fail=True
        with self.assertRaisesRegex(RuntimeError,'Tune read failed'):self.s.apply(data)
        self.assertEqual(self.b.snapshot(),previous);self.assertTrue(data['restored'])
        self.assertFalse(data['applied'])
        self.b.fail=False
        data=self.s.recalculate(data,self.opt);self.s.apply(data)
        self.s.reset(data);self.assertEqual(self.b.snapshot(),original)

    def test_saved_applied_result_and_legacy_readbacks_can_continue(self):
        data=self.s.calculate(self.opt);self.s.apply(data)
        expected=self.s.recalculate(data,self.opt)
        for legacy in (False,True):
            with self.subTest(legacy=legacy):
                loaded=json.loads(json.dumps(serial(data),allow_nan=False))
                if legacy:loaded.pop('applied_baseline')
                result=self.s.recalculate(loaded,self.opt)
                np.testing.assert_allclose(result['delta'],expected['delta'])
                self.assertEqual(result['baseline'],self.b.snapshot())
        missing=copy.deepcopy(data);missing.pop('applied_currents_A')
        with self.assertRaisesRegex(ValueError,'Missing verified'):self.s.recalculate(missing,self.opt)

    def test_iteration_masks_unmeasured_after_bpms_and_does_not_reenable_them(self):
        data=self.s.calculate(self.opt)
        j=int(np.flatnonzero(data['straight'])[0]);self.opt.excluded_bpms=(data['names'][j],)
        data=self.s.recalculate(data,self.opt)
        original=self.b.orbit
        def invalid():
            frame=original();frame['valid'][j]=False;return frame
        with patch.object(self.b,'orbit',side_effect=invalid):self.s.apply(data)
        self.assertFalse(data['after_valid'][j]);self.assertTrue(data['acquisition_valid'][j])
        self.opt.excluded_bpms=();next_step=self.s.recalculate(data,self.opt)
        self.assertFalse(next_step['valid'][j]);self.assertTrue(next_step['acquisition_valid'][j])
        again=self.s.recalculate(next_step,self.opt);self.assertFalse(again['valid'][j])


class QMIterationGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])

    def test_select_matrix_then_recalculate_from_current_beam(self):
        from PyQt6.QtWidgets import QFileDialog
        from Knobs.DRTuning.gui import TuningSession
        from Knobs.DRTuning.qm_checkpoint import save_checkpoint
        w=TuningSession(DispersionFixture())
        try:
            panel=w.qm_dispersion;opt=panel.options()
            self.assertFalse(panel.recalculate_button.isEnabled())
            source=panel.service.calculate(opt,group_rf=True)
            with tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'saved.json';save_checkpoint(path,source)
                original=path.read_bytes()
                with patch.object(QFileDialog,'getOpenFileName',return_value=(str(path),'JSON')):
                    with patch.object(w,'start',side_effect=lambda work,done,**kw:done(work(lambda *_:None))):panel.load_scan()
                self.assertIsNone(panel.result)
                self.assertFalse(panel.set_button.isEnabled())
                self.assertIn("QM trim",panel.matrix_label.text())
                self.assertTrue(panel.recalculate_button.isEnabled())
                w.backend.q[source['actuators'][0]]=.14
                w.backend.d0*=2.
                w.backend.commands.clear()
                with patch.object(w,'start',side_effect=lambda action,done,**_:done(action(lambda *_:None))):
                    panel.recalculate_button.click()
                self.assertEqual(w.backend.commands,[])
                self.assertTrue(panel.set_button.isEnabled())
                self.assertIs(panel.plot.data,panel.result)
                self.assertEqual(panel.table.item(0,1).text(),'0.14000')
                self.assertFalse(np.allclose(panel.result['full_measured'],source['full_measured']))
                self.assertEqual(path.read_bytes(),original)
        finally:w.close();self.app.processEvents()

    def test_update_without_matrix_then_recalculate_reads_limits_and_changed_machine(self):
        from PyQt6.QtWidgets import QFileDialog
        from Knobs.DRTuning.gui import TuningSession
        from Knobs.DRTuning.qm_checkpoint import save_checkpoint
        b=DispersionFixture();w=TuningSession(b)
        try:
            panel=w.qm_dispersion
            original=panel.service.calculate(panel.options(),group_rf=True)
            n=original['actuators'][0];b.commands.clear()
            def run(work,done,**kw):done(work(lambda *_:None))
            with tempfile.TemporaryDirectory() as folder,patch.object(w,'start',side_effect=run):
                path=Path(folder)/'qm_scan_trim_test.checkpoint.json'
                save_checkpoint(path,original)
                before=b.acquisitions
                panel.update_dispersion_button.click()
                self.assertIsNone(panel.result);self.assertFalse(panel.recalculate_button.isEnabled())
                self.assertEqual(b.acquisitions-before,30)
                with patch.object(QFileDialog,'getOpenFileName',return_value=(str(path),'')) as dialog:
                    panel.load_button.click()
                self.assertIn('qm_scan_',dialog.call_args.args[3])
                self.assertNotIn('orbit_measurements',panel.matrix)
                self.assertNotIn('delta',panel.matrix)
                self.assertNotIn('baseline',panel.matrix)
                before=b.acquisitions
                b.quadrupoles[n]['limits_A']=[-.16,.17]
                panel.recalculate_button.click()
                self.assertEqual(b.acquisitions,before)
                np.testing.assert_allclose(panel.result['limits'][:,0],[-.16,.17])
                b.q[n]=.01
                panel.recalculate_button.click()
                self.assertEqual(b.acquisitions-before,30)
                self.assertAlmostEqual(panel.result['centers'][0],.01)
                np.testing.assert_equal(panel.service.matrix_from_scan(panel.result)['response'],panel.matrix['response'])
                self.assertEqual(b.commands,[])
                panel.rf.setValue(2.)
                before=b.acquisitions
                panel.recalculate_button.click()
                self.assertEqual(b.acquisitions-before,30)
                self.assertAlmostEqual(panel.result['centers'][0],.01)
                # Filename filters are also backed by content checks.
                fake=Path(folder)/'qm_dispersion_trim_other.json'
                fake.write_text(json.dumps(dict(schema='dr-correction-1',mode='dispersion')))
                selected=panel.matrix
                with patch.object(QFileDialog,'getOpenFileName',return_value=(str(fake),'')),self.assertRaisesRegex(ValueError,'QM trim or main'):
                    panel.load_scan()
                self.assertIs(panel.matrix,selected)
                with patch.object(QFileDialog,'getOpenFileName',return_value=(str(path),'')),self.assertRaisesRegex(ValueError,'schema'):
                    w.panels['dispersion'].load_measured_response()
        finally:w.close();self.app.processEvents()

    def test_legacy_proposal_requires_refit_for_new_qm_weights(self):
        from Knobs.DRTuning.gui import TuningSession
        window=TuningSession(DispersionFixture())
        try:
            panel=window.qm_dispersion
            self.assertEqual((panel.dispersion_weight.value(),panel.orbit_weight.value(),panel.arc_weight.value()),(1.,.1,0.))
            old=DispersionOptions.from_saved({})
            self.assertEqual((old.dispersion_weight,old.orbit_weight,old.arc_weight),(.05,1.,0.))
            options=panel.options();options.dispersion_weight=old.dispersion_weight;options.orbit_weight=old.orbit_weight
            saved=panel.service.calculate(options,group_rf=True)
            for key in ('orbit_weight','arc_weight','arc_parity'):saved['options'].pop(key)
            panel.display(saved)
            self.assertFalse(panel.set_button.isEnabled())
            panel.dispersion_weight.setValue(.05)
            self.assertFalse(panel.proposal_matches_options(saved))
            panel.orbit_weight.setValue(1.)
            self.assertTrue(panel.proposal_matches_options(saved))
            panel.orbit_weight.setValue(.1);panel.dispersion_weight.setValue(1.)
            with patch.object(window.backend,'orbit',side_effect=AssertionError),patch.object(window.backend,'set_frequency',side_effect=AssertionError):
                panel.display(panel.service.recalculate(saved,panel.options()))
            self.assertTrue(panel.set_button.isEnabled())
            self.assertEqual(panel.result['options']['orbit_weight'],.1)
        finally:window.close();self.app.processEvents()

    def test_apply_recalculate_apply_saves_both_iterations_and_can_reload(self):
        from Knobs.DRTuning.gui import TuningSession
        from Knobs.DRTuning.qm_checkpoint import load_checkpoint
        w=TuningSession(DispersionFixture())
        try:
            panel=w.qm_dispersion;panel.ridge.setValue(.3)
            panel.display(panel.service.calculate(panel.options()))
            with tempfile.TemporaryDirectory() as folder:
                panel.autosave_directory=Path(folder)
                with patch.object(w,'start',side_effect=lambda action,done,**_:done(action(lambda *_:None))):
                    panel.apply()
                    self.assertTrue(panel.recalculate_button.isEnabled());self.assertFalse(panel.set_button.isEnabled())
                    previous=panel.result;reads=w.backend.acquisitions;commands=list(w.backend.commands)
                    panel.recalculate()
                    self.assertTrue(panel.set_button.isEnabled());self.assertTrue(panel.reset_button.isEnabled())
                    self.assertEqual(w.backend.acquisitions,reads);self.assertEqual(w.backend.commands,commands)
                    np.testing.assert_allclose(panel.result['full_measured'],previous['full_after'])
                    panel.apply()
                files=sorted(Path(folder).glob('*.json'));self.assertEqual(len(files),2)
                saved=panel.service.validate_checkpoint(load_checkpoint(files[-1]))
                self.assertTrue(saved['applied']);self.assertEqual(saved['iteration_index'],1)
                self.assertEqual(len(saved['iteration_history']),1)
                self.assertEqual(len(list(Path(folder).glob('*.png'))),2)
                panel.display(saved);panel.service.recalculate(saved,panel.options())
        finally:w.close();self.app.processEvents()
