"""Exercise the live RF guard with timestamped simulated EPICS power updates."""
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtWidgets import QApplication
from Knobs.DRCorrection.backends import DemoBackend
from Knobs.DRTuning import power_pause
from Knobs.DRTuning.gui import TuningSession
from Knobs.linac_power_guard import KlystronPowerGuard, POWER_PVS


class PowerChannels:
    def __init__(self):
        self.callbacks = {}
        self.stamp = time.time() + 10

    def channel(self, pv, **options):
        def add(callback, run_now):
            self.callbacks[pv] = callback
            self.update(pv, 100.)
            return 0
        return Mock(add_callback=add, remove_callback=lambda _: self.callbacks.pop(pv))

    def update(self, pv, value):
        self.stamp += 1
        self.callbacks[pv](value=value, timestamp=self.stamp)


class PowerPauseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def until(self, condition):
        end = time.monotonic() + 5
        while not condition():
            self.app.processEvents()
            if time.monotonic() > end:
                self.fail('Timed out waiting for RF pause worker / GUI')
            time.sleep(.005)
        self.app.processEvents()

    def test_popup_recovery_reacquires_and_cancel_restores(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                channels = PowerChannels()
                backend = DemoBackend()
                backend.monitor_klystrons = True
                window = TuningSession(backend)
                window.show()
                reads, results, restored = [], [], []
                cls = power_pause.PowerPause
                def monitor(event, notify):
                    return cls(event, notify, lambda: KlystronPowerGuard(channels.channel))
                def read():
                    reads.append(len(reads) + 1)
                    if len(reads) == 1:
                        channels.update(POWER_PVS[3], 50.)
                    return reads[-1]
                @power_pause.restoration
                def restore():
                    power_pause.checkpoint()
                    restored.append(True)
                def work(progress):
                    try:
                        return power_pause.stable_read(read)
                    finally:
                        restore()
                try:
                    with patch.object(power_pause, 'PowerPause', monitor):
                        window.start(work, results.append)
                    self.until(lambda: window._rf_popup is not None)
                    box = window._rf_popup
                    self.assertIn('Klystron 3', box.text())
                    # Recovery is required even when Resume is explicitly pressed.
                    next(b for b in box.buttons() if b.text() == 'Resume').click()
                    self.until(lambda: box is not window._rf_popup)
                    window._rf_popup.escapeButton().click()
                    self.assertFalse(window._rf_popup.isVisible())
                    if cancel:
                        window.cancel_button.click()
                    else:
                        channels.update(POWER_PVS[3], 100.)
                        self.assertEqual(reads, [1])
                        self.assertTrue(window.task.isRunning())
                        window.pause_button.click()
                    self.until(lambda: not window.task.isRunning() and window.tabs.isEnabled())
                    self.assertEqual(restored, [True])
                    self.assertEqual(results, [] if cancel else [2])
                    self.assertEqual(channels.callbacks, {})
                    self.assertEqual(window._failed, cancel)
                finally:
                    window.service.cancel.set()
                    if window.task:
                        window.task.wait(5000)
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()

    def test_bpm_and_xsr_ten_update_groups_restart_after_a_low_shot(self):
        import numpy as np
        from Knobs.DRCorrection.core import Options
        from Knobs.DRTuning.dispersion import QMOrbitReader
        from Knobs.DRTuning.epics import LiveBackend
        from Knobs.DRTuning.hardware import load_hardware_profile

        for signal in ('BPM', 'XSR'):
            with self.subTest(signal=signal):
                channels = PowerChannels()
                def recover(monitor, reason):
                    if reason:
                        channels.update(POWER_PVS[1], 100.)
                        monitor.resume.set()
                cancel = threading.Event()
                monitor = power_pause.PowerPause(cancel, recover, lambda: KlystronPowerGuard(channels.channel))
                profile = load_hardware_profile()
                client = Mock()
                client.caget.return_value = profile['orbit']['required_mode']
                backend = LiveBackend(profile, client)
                packed = np.zeros((98, 10))
                cols = profile['orbit']['columns']
                packed[:, cols['status']] = profile['orbit']['valid_status']
                packed[:, cols['tmit']] = 1.
                stamp = 0
                def record(**kwargs):
                    nonlocal stamp
                    stamp += 1
                    if stamp == 5:
                        channels.update(POWER_PVS[1], 25.)
                        if signal == 'XSR':
                            raise RuntimeError('No camera data during RF trip')
                    return dict(timestamp=stamp, value=packed.ravel().copy() if signal=='BPM' else stamp)
                client.get_pv.return_value.get_with_metadata.side_effect = record
                if signal == 'BPM':
                    reader = QMOrbitReader(backend)
                    monitor.run(lambda _: reader._orbit(Options(bpm_samples=10)), None)
                    self.assertEqual(len(reader.orbit_measurements), 1)
                    stamps = [item[profile['orbit']['packed_pv']]
                              for item in reader.orbit_measurements[0]['timestamps']]
                else:
                    result = monitor.run(lambda _: backend.sample_xsr(cancel, 10), None)
                    stamps = result['timestamps']
                    self.assertEqual(result['count'], 10)
                self.assertEqual(monitor.generation, 1)
                self.assertEqual(stamps, list(range(stamp-9, stamp+1)))
                self.assertGreater(stamps[0], 5)

    def test_missing_monitor_readings_keep_machine_access_blocked(self):
        guard = Mock(ready=False)
        monitor = power_pause.PowerPause(threading.Event(), Mock(), lambda: guard)
        def attempt(progress):
            with patch.object(power_pause.time, 'monotonic', side_effect=[0., 16.]):
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError, 'No start RF power readings'):
                        power_pause.checkpoint()
        monitor.run(attempt, None)
        guard.start.assert_called_once()
        guard.close.assert_called_once()
