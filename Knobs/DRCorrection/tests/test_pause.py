"""Pause real scan transactions against offline backends only."""
import threading
import time
import unittest
from unittest.mock import patch
import numpy as np
from Knobs.DRCorrection.backends import DemoBackend,EpicsBackend
from Knobs.DRCorrection.core import CorrectionService,Options,Cancelled
from test_corrections import FakeEpics,fake_profile


class PauseTests(unittest.TestCase):
    def scan(self,mode='orbit',adaptive=False,at_end=False,action='resume',service=None):
        b=service.backend if service else DemoBackend();s=service or CorrectionService(b)
        options=Options(probe1=b.hcorrectors[0],probe2=b.hcorrectors[min(1,len(b.hcorrectors)-1)],adaptive_probe=adaptive,
                        adaptive_max_pairs=2,adaptive_target_mm=100.)
        names=b.skews if mode=='skew' else b.vcorrectors
        options.fixed_actuators=tuple(names[2:])
        initial=b.snapshot() if service is None else b.measured_snapshot([])
        outcome={};messages=[];requested=False
        def progress(n,text):
            nonlocal requested
            messages.append(text)
            trigger=('response 2/2' in text) if at_end else (': pair 1/' in text)
            if trigger and not requested:requested=True;s.pause_requested.set()
        def work():
            try:outcome['data']=s.calculate(mode,options,progress)
            except Exception as error:outcome['error']=error
        worker=threading.Thread(target=work,daemon=True);worker.start()
        try:
            self.assertTrue(s.paused.wait(5),outcome)
            current=b.snapshot() if service is None else b.measured_snapshot([])
            self.assertEqual(current,initial)  # Including RF and skew probes.
            reads=b.acquisitions
            time.sleep(.12)
            self.assertEqual(b.acquisitions,reads)
            self.assertTrue(worker.is_alive())
            if action=='cancel':s.cancel.set()
            else:
                if action=='change':b.write('correctors',[b.hcorrectors[0]],[.0123])
                s.pause_requested.clear()
            worker.join(5);self.assertFalse(worker.is_alive())
            self.assertFalse(s.paused.is_set())
            if action=='cancel':
                self.assertIsInstance(outcome.get('error'),Cancelled)
                self.assertEqual(b.acquisitions,reads)
            elif action=='change':
                self.assertIn('Machine settings changed',str(outcome.get('error')))
                self.assertEqual(b.read('correctors',[b.hcorrectors[0]])[0],.0123)
                self.assertEqual(b.acquisitions,reads)
            else:
                self.assertNotIn('error',outcome)
                expected=s.calculate(mode,options)
                np.testing.assert_allclose(outcome['data']['response'],expected['response'])
                self.assertTrue(any(m.startswith('RESUMED:') for m in messages))
        finally:
            s.cancel.set();s.pause_requested.clear();worker.join(5)
    def test_pause_restores_each_mode_and_preserves_response(self):
        for mode in ('orbit','dispersion','skew'):
            with self.subTest(mode=mode):self.scan(mode)
    def test_pause_between_adaptive_pairs(self):self.scan(adaptive=True)
    def test_pause_after_last_column(self):self.scan(at_end=True)
    def test_cancel_while_paused_does_not_resume_measurements(self):self.scan(action='cancel')
    def test_changed_settings_on_resume_abort_without_overwriting(self):self.scan(action='change')
    def test_pause_respects_initial_skipped_readbacks(self):
        from test_skip_readbacks import setup
        client,b,s=setup('missing')
        self.scan(service=s)


class NominalTargetTests(unittest.TestCase):
    def test_missing_horizontal_target_is_explicit_and_never_writes(self):
        p=fake_profile();p['target_dispersion_mm']['x']=None
        client=FakeEpics();s=CorrectionService(EpicsBackend(p,client))
        with patch.object(client,'caput',wraps=client.caput) as writes:
            with self.assertRaisesRegex(ValueError,'Nominal horizontal dispersion target is not configured'):
                s.calculate('dispersion',Options(plane='x'))
            writes.assert_not_called()
        np.testing.assert_array_equal(s.backend.target_dispersion('y'),[0.,0.])
    def test_bad_target_reports_shape_and_plane_before_writes(self):
        for value in ([1.], [float('nan'),0.]):
            client=FakeEpics();b=EpicsBackend(fake_profile(),client)
            b.target_dispersion=lambda plane:np.asarray(value)
            with patch.object(client,'caput',wraps=client.caput) as writes:
                with self.assertRaisesRegex(ValueError,'plane=x; expected shape='):
                    CorrectionService(b).calculate('dispersion',Options(plane='x'))
                writes.assert_not_called()
