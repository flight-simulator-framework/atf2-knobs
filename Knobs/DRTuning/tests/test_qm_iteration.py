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
