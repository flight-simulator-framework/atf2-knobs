"""Measured range search: shifted full width, half-width fallback, and hard failures."""
import copy
import threading
import unittest
from unittest.mock import patch
import numpy as np

from Knobs.DRCorrection.core import Cancelled
from Knobs.DRTuning.coupling import BumpOptions,CouplingService,BumpRangeUnavailable
from Knobs.DRTuning.tests.test_bump_dispersion import BumpFixture


class AdaptiveBumpRangeTests(unittest.TestCase):
    def setUp(self):
        self.b=BumpFixture();self.cancel=threading.Event();self.s=CouplingService(self.b,self.cancel)
        self.opt=BumpOptions(settle_s=0.)
        self.g=self.s.calibrate(self.b.geometry(),self.opt)
        self.baseline=self.b.snapshot();self.b.commands=[]

    def current_window(self,low,high):
        # The linear fixture has eight independent local/orbit constraints.
        # Bound each current so its calibrated knob can reach this interval.
        k=np.asarray(self.g['knob_A_per_mm']);ends=np.array([low*k,high*k])
        self.g['limits']=[ends.min(axis=0)-1.e-8,ends.max(axis=0)+1.e-8]

    def test_shifted_full_width_respects_currents_and_fits_beyond_original_half_range(self):
        self.current_window(-.25,.65);self.b.minimum=.5;events=[]
        result=self.s.scan(self.g,self.opt,event=events.append)
        np.testing.assert_allclose(result['scan_range']['bounds_mm'],[-.2,.6])
        self.assertEqual(result['scan_range']['width_mm'],.8)
        self.assertTrue(result['scan_range']['shifted']);self.assertFalse(result['scan_range']['fallback'])
        self.assertAlmostEqual(result['final_amplitude_mm'],.5,places=5)
        np.testing.assert_allclose(result['refinement_mm'],[.4,.6],atol=1.e-6)
        self.assertEqual(sum(e['kind']=='sample' for e in events),80)
        self.assertTrue(any(e['kind']=='range' for e in events))
        lo,hi=np.asarray(self.g['limits'])
        for _,names,values in self.b.commands:
            indices=[self.g['actuators'].index(n) for n in names]
            self.assertTrue(np.all(np.asarray(values)>=lo[indices]-1.e-9))
            self.assertTrue(np.all(np.asarray(values)<=hi[indices]+1.e-9))
        self.s.reset(result);self.assertEqual(self.b.snapshot(),self.baseline)

    def test_cod_limited_range_falls_back_to_total_width_point_four(self):
        close=self.s.close_at
        def limited(*args):
            observed=close(*args)
            if abs(args[4])>.200001:raise BumpRangeUnavailable('Measured COD cannot close at this amplitude')
            return observed
        with patch.object(self.s,'close_at',side_effect=limited):result=self.s.scan(self.g,self.opt)
        np.testing.assert_allclose(result['scan_range']['bounds_mm'],[-.2,.2])
        self.assertEqual(result['scan_range']['width_mm'],.4)
        self.assertTrue(result['scan_range']['fallback']);self.assertFalse(result['scan_range']['shifted'])
        np.testing.assert_allclose(result['refinement_mm'],[.03,.2],atol=1.e-6)
        self.assertAlmostEqual(result['final_amplitude_mm'],.13,places=5)
        amplitudes=[check['amplitude_mm'] for check in result['range_checks']]
        self.assertEqual(len(amplitudes),len(set(amplitudes)))
        self.assertTrue(any(not check['success'] for check in result['range_checks']))
        self.assertLessEqual(max(abs(a) for a in amplitudes),.4)

    def test_half_width_can_also_shift(self):
        self.current_window(-.01,.41);self.b.minimum=.25
        result=self.s.scan(self.g,self.opt)
        np.testing.assert_allclose(result['scan_range']['bounds_mm'],[0.,.4])
        self.assertTrue(result['scan_range']['fallback']);self.assertTrue(result['scan_range']['shifted'])
        self.assertAlmostEqual(result['final_amplitude_mm'],.25,places=5)

    def test_supply_aligned_window_between_center_grid_steps_keeps_full_width(self):
        self.current_window(-.35,.45);self.b.minimum=.3
        result=self.s.scan(self.g,self.opt)
        np.testing.assert_allclose(result['scan_range']['bounds_mm'],[-.35,.45],atol=1.e-6)
        self.assertFalse(result['scan_range']['fallback']);self.assertEqual(result['scan_range']['width_mm'],.8)
        self.assertAlmostEqual(result['final_amplitude_mm'],.3,places=5)

    def test_no_feasible_half_width_restores_and_never_reads_xsr(self):
        self.current_window(-.15,.15)
        with patch.object(self.b,'sample_xsr',side_effect=AssertionError('XSR before feasible range')):
            with self.assertRaisesRegex(BumpRangeUnavailable,'No closed scan window'):self.s.scan(self.g,self.opt)
        self.assertEqual(self.b.snapshot(),self.baseline);self.assertTrue(self.s.last_result['restored'])
        self.assertFalse(self.s.last_result['applied']);self.assertEqual(self.s.last_result['points'],[])

    def test_measurement_or_hardware_faults_stop_without_range_fallback(self):
        for exc in (TimeoutError('stale acquisition'),ValueError('Required bump BPM lost validity'),RuntimeError('readback mismatch')):
            with self.subTest(exc=str(exc)):
                with patch.object(self.s,'close_at',side_effect=exc) as close:
                    with self.assertRaises(type(exc)):self.s.scan(self.g,self.opt)
                self.assertEqual(close.call_count,1)
                self.assertEqual(len(self.s.last_result['range_attempts']),1)
                self.assertEqual(self.b.snapshot(),self.baseline)

    def test_cancel_during_range_checks_restores_before_any_xsr(self):
        def event(value):
            if value['kind']=='orbit':self.cancel.set()
        with patch.object(self.b,'sample_xsr',side_effect=AssertionError('XSR after cancellation')):
            with self.assertRaises(Cancelled):self.s.scan(self.g,self.opt,event=event)
        self.assertEqual(self.b.snapshot(),self.baseline);self.assertTrue(self.s.last_result['restored'])

    def test_changed_cod_after_preflight_still_stops_measurement(self):
        original=self.s.select_scan_range
        def drift(*args):
            selected=original(*args)
            self.b.nonlinear=1000.
            return selected
        with patch.object(self.s,'select_scan_range',side_effect=drift):
            with self.assertRaises(ValueError):self.s.scan(self.g,self.opt)
        self.assertEqual(self.b.snapshot(),self.baseline);self.assertFalse(self.s.last_result['applied'])

    def test_selection_can_use_calibration_range_if_full_symmetric_range_is_infeasible(self):
        from types import SimpleNamespace
        from Knobs.DRCorrection.core import Options
        model=SimpleNamespace(bpm_names=self.b.bpm_names)
        candidate=copy.deepcopy(self.b.geometry());candidate['model_response']=self.b.response.copy()
        with patch.object(self.s.reader.model_manager,'prepare',return_value=model),\
             patch('Knobs.DRTuning.coupling.select_geometry',side_effect=[
                 ValueError('No observable, bounded local bump geometry: full range unavailable'),candidate]) as select:
            geometry=self.s.prepare('SF1R.1',self.opt,Options())
        self.assertEqual([c.kwargs['amplitude_mm'] for c in select.call_args_list],[.4,.1])
        self.assertEqual(geometry['selection_half_range_mm'],.1)
        self.assertEqual(self.b.commands,[])


class BumpRangeGuiTests(unittest.TestCase):
    def test_bump_tune_matching_is_optional_and_independent(self):
        from PyQt6.QtWidgets import QApplication
        from Knobs.DRTuning.gui import TuningSession
        from Knobs.DRTuning.tests.test_bump_dispersion import DispersionFixture
        app=QApplication.instance() or QApplication([]);window=TuningSession(DispersionFixture())
        try:
            panel=window.coupling_panel;shared=window.panels['dispersion'].options
            shared.model_match_tunes=True;shared.fixed_actuators=('unmapped',)
            with patch.object(window,'start') as start,patch.object(panel.service,'prepare') as prepare:
                panel.prepare();start.call_args.args[0](lambda *_:None)
                options=prepare.call_args.args[2]
                self.assertFalse(options.model_match_tunes)
                self.assertEqual(options.fixed_actuators,('unmapped',))
                self.assertTrue(shared.model_match_tunes)
                panel.geometry={};panel.calibration={};panel.scan_button.setEnabled(True)
                panel.match_tunes.setChecked(True)
                self.assertIsNone(panel.geometry);self.assertIsNone(panel.calibration)
                self.assertFalse(panel.scan_button.isEnabled())
                panel.prepare();start.call_args.args[0](lambda *_:None)
                self.assertTrue(prepare.call_args.args[2].model_match_tunes)
        finally:window.close();app.processEvents()

    def test_full_width_control_and_selected_range_are_explicit(self):
        from PyQt6.QtWidgets import QApplication
        from Knobs.DRTuning.gui import TuningSession
        from Knobs.DRTuning.tests.test_bump_dispersion import DispersionFixture
        app=QApplication.instance() or QApplication([]);window=TuningSession(DispersionFixture())
        try:
            panel=window.coupling_panel
            self.assertEqual(panel.amplitude.value(),.8);self.assertEqual(panel.options().amplitude_mm,.4)
            self.assertEqual(panel.step.value(),4.)
            panel.on_update(dict(kind='range',scan_range=dict(bounds_mm=[-.2,.6],width_mm=.8,fallback=False)))
            self.assertIn('-0.200 to +0.600',panel.range_status.text())
            panel.on_update(dict(kind='range',scan_range=dict(bounds_mm=[-.2,.2],width_mm=.4,fallback=True)))
            self.assertIn('width 0.400',panel.range_status.text());self.assertIn('reduced',panel.range_status.text())
        finally:window.close();app.processEvents()
