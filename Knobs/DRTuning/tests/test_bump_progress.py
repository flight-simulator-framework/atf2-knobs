"""Progress follows real acquisition/movement and keeps failure/restoration distinct."""
import threading
import unittest
from unittest.mock import patch
import numpy as np
from Knobs.DRCorrection.core import Cancelled
from Knobs.DRTuning.coupling import CouplingService,BumpOptions
from Knobs.DRTuning.tests.test_bump_dispersion import BumpFixture,DispersionFixture


class BumpProgressTests(unittest.TestCase):
    def setUp(self):
        self.backend=BumpFixture();self.cancel=threading.Event()
        self.service=CouplingService(self.backend,self.cancel);self.options=BumpOptions(settle_s=0.)
    def test_probe_samples_are_actual_counts_and_completion_follows_restore_verification(self):
        events=[];commands_at_complete=[];baseline=self.backend.snapshot()
        def event(value):
            events.append(value)
            if value.get('stage')=='complete':commands_at_complete.append(self.backend.snapshot())
        result=self.service.calibrate(self.backend.geometry(),self.options,event=event)
        probe=[e for e in events if e.get('stage')=='probe' and 'samples' in e]
        self.assertEqual(len(probe),8*2*11)
        for start in range(0,len(probe),11):
            group=probe[start:start+11]
            self.assertEqual([e['samples'] for e in group],list(range(11)))
            self.assertEqual(group[-1]['readback_A'],group[-1]['command_A'])
        closure=[e for e in events if e['kind']=='orbit']
        self.assertEqual([e['amplitude_mm'] for e in closure],[.1,-.1])
        self.assertTrue(all(e['closed'] for e in closure))
        stages=[e['stage'] for e in events if e['kind']=='status']
        self.assertEqual(stages[-3:],['restore','restore_verify','complete'])
        self.assertEqual(commands_at_complete,[baseline]);self.assertTrue(result['calibrated'])
    def test_cancel_during_real_samples_restores_without_reporting_success(self):
        events=[];baseline=self.backend.snapshot()
        def event(value):
            events.append(value)
            if value.get('stage')=='probe' and value.get('samples')==3:self.cancel.set()
        with self.assertRaises(Cancelled):self.service.calibrate(self.backend.geometry(),self.options,event=event)
        self.assertEqual(self.backend.snapshot(),baseline)
        self.assertTrue(any(e.get('stage')=='restore' for e in events))
        self.assertFalse(any(e.get('stage')=='complete' for e in events))

    def test_invalid_updates_replaced_at_same_setting(self):
        events=[]
        orbit=self.backend.orbit
        def intermittent():
            row=orbit()
            shot=self.backend.acquisitions
            row['x'][:]=shot
            if shot in (1,5,12):
                row['valid'][shot%2]=False
                row['x'][:]=999.
            return row
        with patch.object(self.backend,'orbit',side_effect=intermittent):
            position,_=self.service.orbit(self.options,required=[0,1],event=events.append)
        self.assertEqual(self.backend.acquisitions,13)
        self.assertEqual(self.backend.commands,[])
        self.assertEqual([e['samples'] for e in events],[0,3,9])
        np.testing.assert_allclose(position[:8],np.mean([2,3,4,6,7,8,9,10,11,13]))

    def test_persistent_loss_during_probe_names_bpm_and_restores(self):
        initial=self.backend.snapshot()
        orbit=self.backend.orbit
        def missing_at_probe():
            row=orbit()
            if any(self.backend.currents.values()):
                row['tmit'][2]=0.
            return row
        with patch.object(self.backend,'orbit',side_effect=missing_at_probe):
            with self.assertRaisesRegex(ValueError,'after 3 retries: B2'):
                self.service.calibrate(self.backend.geometry(),self.options)
        self.assertEqual(self.backend.acquisitions,14)
        self.assertEqual(self.backend.snapshot(),initial)
        self.assertTrue(self.service.last_result['restored'])
    def test_failed_restoration_is_reported_as_failure(self):
        events=[]
        with patch.object(self.service,'verify_geometry',side_effect=[None,ValueError('readback mismatch')]):
            with self.assertRaisesRegex(ValueError,'readback mismatch'):
                self.service.calibrate(self.backend.geometry(),self.options,event=events.append)
        self.assertTrue(any(e.get('stage')=='restore_verify' for e in events))
        self.assertFalse(any(e.get('stage')=='complete' for e in events))


class BumpProgressGuiTests(unittest.TestCase):
    def test_visible_current_counts_cod_and_failure_context(self):
        from PyQt6.QtWidgets import QApplication
        from Knobs.DRTuning.gui import TuningSession
        app=QApplication.instance() or QApplication([]);window=TuningSession(DispersionFixture())
        try:
            panel=window.coupling_panel
            for count in (0,6,10):
                panel.on_update(dict(kind='status',stage='probe',message='2/5 応答測定 3/8: ZV30R',
                    samples=count,total=10,magnets=['ZV30R'],baseline_A=[.2],command_A=[.25],readback_A=[.2499]))
            self.assertIn('ZV30R',panel.activity_label.text());self.assertIn('+0.2499',panel.activity_currents.text())
            self.assertEqual(panel.acquisition_progress.value(),10)
            panel.on_update(dict(kind='orbit',state=np.array([.001,0,.098,0]),amplitude_mm=.1,iteration=2,
                closed=True,outside_max_mm=.004,outside_rms_mm=.004,outside=np.array([.004,.001])))
            self.assertIn('4.00 µm',panel.closure_status.text());self.assertIn('correction 2',panel.closure_status.text())
            panel.on_update(dict(kind='failure',data=dict(restored=False,restoration_error='supply offline',error='readback failed')))
            self.assertIn('Restoration failed',panel.activity_label.text());self.assertIn('ZV30R',panel.activity_label.text())
            self.assertEqual(panel.acquisition_progress.format(),'Acquisition stopped')
        finally:window.close();app.processEvents()
