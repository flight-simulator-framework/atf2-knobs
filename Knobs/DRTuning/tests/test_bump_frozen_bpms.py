"""Transient per-channel freezes during timestamp-verified bump acquisition."""
import threading
import unittest
from unittest.mock import patch

import numpy as np

from Knobs.DRTuning.coupling import BumpOptions, CouplingService
from Knobs.DRTuning.tests.test_bump_dispersion import BumpFixture


class FrozenBpmTests(unittest.TestCase):
    def setUp(self):
        self.backend=BumpFixture()
        self.service=CouplingService(self.backend,threading.Event())
        self.options=BumpOptions(settle_s=0.)
        self.events=[]
        self.batch=0

    def provide(self,change):
        def samples(count,cancel,interval):
            self.batch+=1
            for shot in range(count):
                frame=self.backend.orbit()
                # Only Y varies. New timestamps or intensity alone do not
                # establish that the position channel is still updating.
                frame['y']+=1e-9*(shot+1)
                frame['tmit']+=shot
                frame.update(freshness_verified=True,timestamps={'bpm':self.backend.acquisitions})
                change(frame,self.batch,shot)
                yield frame
        self.backend.orbit_samples=samples

    def test_outside_freeze_and_mixed_recovery_batch_do_not_dilute_cod(self):
        g=self.backend.geometry()
        def change(frame,batch,shot):
            if batch==1 or (batch==2 and shot<4):
                frame['x'][2]=8.
                frame['y'][2]=9.
            else:
                frame['x'][2]=.06
            frame['x'][3:]=.012
        self.provide(change)
        baseline=np.zeros(16)
        for _ in range(2):
            observed=self.service.observe(g,baseline,self.options,event=self.events.append)
            self.assertFalse(observed['valid'][2])
            self.assertTrue(np.isnan(observed['outside'][0]))
            self.assertAlmostEqual(observed['outside_rms_mm'],.012)
        observed=self.service.observe(g,baseline,self.options,event=self.events.append)
        self.assertTrue(observed['valid'][2])
        self.assertAlmostEqual(observed['outside_max_mm'],.06)
        transitions=[e for e in self.events if e['kind']=='bpm_health']
        self.assertEqual([e['frozen'] for e in transitions],[['B2'],[]])
        self.assertEqual(transitions[-1]['recovered'],['B2'])

    def test_internal_or_too_few_outside_wait_without_writing(self):
        def change(frame,batch,shot):
            if batch==1:frame['y'][0]=0.
            if batch==2:frame['y'][2:6]=0.
        self.provide(change)
        with patch.object(self.service.cancel,'wait',return_value=False) as wait:
            observation=self.service.observe(self.backend.geometry(),np.zeros(16),self.options)
        self.assertTrue(observation['valid'].all())
        self.assertEqual(self.batch,3)
        self.assertEqual(wait.call_count,2)
        self.assertFalse(self.backend.commands)

    def test_timeout_if_whole_ring_stays_frozen(self):
        self.provide(lambda frame,*_:frame['y'].fill(0.))
        with patch('Knobs.DRTuning.coupling.time.monotonic',side_effect=[0.,0.,91.]),patch.object(self.service.cancel,'wait',return_value=False):
            with self.assertRaisesRegex(ValueError,'90 s'):self.service.orbit(self.options)
        self.assertFalse(self.backend.commands)

    def test_frozen_probe_rows_are_missing_and_baseline_is_filled_only_at_zero(self):
        def change(frame,batch,shot):
            if batch==1:frame['y'][2]=0.
        self.provide(change)
        column,_=self.service.probe_column('H0',0.,.05,self.options,(),0,1,self.events.append)
        self.assertTrue(np.isnan(column[[2,10]]).all())
        self.assertTrue(np.isfinite(column[[3,11]]).all())
        baseline=np.zeros(16);baseline[[2,10]]=np.nan
        g=self.backend.geometry()
        observed=self.service.observe(g,baseline,self.options)
        self.assertTrue(np.isnan(observed['outside'][0]))
        observed=self.service.observe(g,baseline,self.options,at_baseline=True)
        self.assertTrue(np.isfinite(baseline).all())
        self.assertEqual(observed['outside'][0],0.)

    def test_calibration_continues_and_restores_missing_reference_at_original_currents(self):
        def change(frame,batch,shot):
            if batch in (1,2):
                frame['x'][2]=40.
                frame['y'][2]=-50.
        self.provide(change)
        original=self.backend.snapshot()
        result=self.service.calibrate(self.backend.geometry(),self.options,event=self.events.append)
        self.assertTrue(result['calibrated'])
        self.assertTrue(np.isnan(result['response'][[2,10],0]).all())
        np.testing.assert_allclose(result['reference_orbit_mm'][[2,10]],[0.,5.5e-9],atol=1e-12)
        self.assertEqual(self.backend.snapshot(),original)
