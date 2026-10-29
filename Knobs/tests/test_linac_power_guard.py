"""RF power shot interlock and both pause paths without accelerator access."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import threading
import itertools
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from Knobs.linac_power_guard import KlystronPowerGuard, KlystronPowerLoss, POWER_PVS
from Knobs import Linac_Opt as opt
from Knobs.linac_feedback import BeamLoss, FeedbackController, FeedbackConfig, PVS, TIMING_PV
from Knobs.linac_feedback_io import FeedbackMachine
from Knobs.tests.test_linac_feedback import Plant


def guard():
    result = KlystronPowerGuard(pv_factory=Mock())
    result.started = 100.
    for i, pv in enumerate(POWER_PVS):
        result.update(pvname=pv, value=100. + i, timestamp=101.)
    return result


class PowerGuardTests(unittest.TestCase):
    def test_each_of_nine_channels_trips_at_exactly_half_but_not_above(self):
        for i, pv in enumerate(POWER_PVS):
            with self.subTest(pv=pv):
                monitor = guard()
                monitor.update(pvname=pv, value=(100. + i) * .5001, timestamp=102.)
                monitor.check()
                monitor.update(pvname=pv, value=(100. + i) * .5, timestamp=103.)
                with self.assertRaisesRegex(KlystronPowerLoss, f"Klystron {i} RF power"):
                    monitor.check()

    def test_single_low_shot_is_latched_even_if_it_recovers_before_check(self):
        monitor = guard()
        monitor.update(pvname=POWER_PVS[8], value=0., timestamp=102.)
        monitor.update(pvname=POWER_PVS[8], value=120., timestamp=103.)
        with self.assertRaises(KlystronPowerLoss):
            monitor.check()
        monitor.check()
        self.assertEqual(monitor.baseline[POWER_PVS[8]], 108.)

    def test_cached_and_duplicate_shots_do_not_establish_or_change_baseline(self):
        monitor = KlystronPowerGuard(pv_factory=Mock())
        monitor.started = 100.
        monitor.update(pvname=POWER_PVS[0], value=400., timestamp=99.)
        self.assertFalse(monitor.ready)
        self.assertEqual(monitor.baseline, {})
        monitor.update(pvname=POWER_PVS[0], value=100., timestamp=101.)
        monitor.update(pvname=POWER_PVS[0], value=0., timestamp=101.)
        monitor.check()
        self.assertEqual(monitor.baseline[POWER_PVS[0]], 100.)

    def test_invalid_reads_are_not_replaced_with_zero_and_zero_baseline_pauses(self):
        monitor = KlystronPowerGuard(pv_factory=Mock())
        monitor.started = 100.
        for stamp, value in enumerate((None, float("nan"), float("inf")), 101):
            monitor.update(pvname=POWER_PVS[0], value=value, timestamp=stamp)
        self.assertEqual(monitor.baseline, {})
        monitor.check()
        monitor.update(pvname=POWER_PVS[0], value=0., timestamp=104.)
        with self.assertRaisesRegex(KlystronPowerLoss, "cannot establish"):
            monitor.check()
        self.assertFalse(monitor.ready)

    def test_subscribes_to_all_channels_and_removes_only_own_callbacks(self):
        channels = {}
        def factory(name, **kwargs):
            self.assertTrue(kwargs["auto_monitor"])
            channel = Mock()
            def subscribe(callback, run_now):
                self.assertTrue(run_now)
                # Factories may prefix PV names (e.g. the loopback IOC).
                callback(pvname="TEST:" + name, value=100., timestamp=monitor.started + .1)
                return 7
            channel.add_callback.side_effect = subscribe
            channels[name] = channel
            return channel
        events = []
        monitor = KlystronPowerGuard(factory, events.append)
        monitor.start()
        monitor.check()
        self.assertTrue(monitor.ready)
        self.assertEqual(set(channels), set(POWER_PVS))
        self.assertEqual(len(events), 9)
        monitor.close()
        for channel in channels.values():
            channel.remove_callback.assert_called_once_with(7)
            channel.put.assert_not_called()

    def test_main_blocks_commands_until_operator_resumes_after_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            machine = Mock()
            worker = opt.OptimizationWorker({}, directory, interface=machine)
            worker.power_guard = guard()
            worker.power_guard.update(pvname=POWER_PVS[3], value=20., timestamp=102.)
            paused = threading.Event()
            events = []
            def progress(event):
                events.append(event)
                if event.get("paused"):
                    paused.set()
            worker._emit_progress = progress
            errors = []
            def write():
                try:
                    worker._write_setpoints({TIMING_PV: 100.})
                except Exception as exc:
                    errors.append(exc)
            thread = threading.Thread(target=write)
            thread.start()
            try:
                self.assertTrue(paused.wait(2))
                machine.pv_put.assert_not_called()
                self.assertIn("Klystron 3", worker.pause_reason)
                # Resume while still low must return to the forced pause.
                paused.clear()
                worker.resume()
                self.assertTrue(paused.wait(2))
                machine.pv_put.assert_not_called()
                worker.power_guard.update(pvname=POWER_PVS[3], value=20., timestamp=103.)
                worker.power_guard.update(pvname=POWER_PVS[3], value=103., timestamp=104.)
                self.assertTrue(worker.is_paused)
                machine.pv_put.assert_not_called()
                worker.resume()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                machine.pv_put.assert_called_once_with(TIMING_PV, 100.)
                self.assertEqual(worker.power_guard.baseline[POWER_PVS[3]], 103.)
            finally:
                worker.stop()
                thread.join(2)

    def test_resume_acknowledges_only_recovered_fault_and_rearms_for_new_shots(self):
        monitor = guard()
        pv = POWER_PVS[0]
        monitor.update(pvname=pv, value=50., timestamp=102.)
        with self.assertRaises(KlystronPowerLoss):
            monitor.check()
        monitor.update(pvname=pv, value=20., timestamp=103.)
        monitor.acknowledge()
        with self.assertRaises(KlystronPowerLoss):
            monitor.check()
        monitor.update(pvname=pv, value=20., timestamp=104.)
        monitor.update(pvname=pv, value=100., timestamp=105.)
        monitor.acknowledge()
        monitor.check()
        self.assertEqual(monitor.baseline[pv], 100.)
        monitor.update(pvname=pv, value=50., timestamp=106.)
        with self.assertRaises(KlystronPowerLoss):
            monitor.check()

    def test_main_discards_objective_that_spanned_a_power_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = opt.OptimizationWorker({}, directory, interface=Mock())
            results = []
            def measure():
                results.append(1)
                if len(results) == 1:
                    worker._power_trip_count += 1  # Pause/resume occurred during acquisition.
                    return "interrupted"
                return "fresh"
            worker._measure_objective_once = measure
            self.assertEqual(worker._measure_objective(), "fresh")
            self.assertEqual(len(results), 2)

    def test_closing_main_rf_warning_keeps_pause_until_explicit_resume(self):
        for click_resume in (False, True):
            worker = SimpleNamespace(is_paused=True, is_running=True, pause_reason="Klystron 8 RF power is too low",
                                     resume=Mock(), save_and_end=Mock())
            window = SimpleNamespace(worker=worker)
            with patch.object(opt, "QMessageBox") as factory:
                resume, keep = object(), object()
                box = factory.return_value
                box.addButton.side_effect = [resume, keep]
                box.clickedButton.return_value = resume if click_resume else None
                opt.MainWindow._show_pause_choices(window)
                box.setEscapeButton.assert_called_once_with(keep)
            self.assertEqual(worker.resume.call_count, int(click_resume))
            worker.save_and_end.assert_not_called()

    def test_feedback_power_loss_restores_probe_then_uses_auto_pause_path(self):
        plant = Plant()
        monitor = guard()
        events = []
        controller = FeedbackController(plant, FeedbackConfig(settle_sec=0),
                                        {pv: (-10000., 10000.) for pv in PVS}, events.append)
        original = dict(plant.values)
        measurement = plant.transmission
        def transmission():
            monitor.check()
            return measurement()
        plant.transmission = transmission
        def drop(values, restore):
            if not restore:
                monitor.update(pvname=POWER_PVS[4], value=0., timestamp=102.)
        plant.on_write = drop
        def pause(reason):
            self.assertIn("Klystron 4", reason)
            self.assertEqual(plant.values, original)
            raise InterruptedError("test complete")
        with self.assertRaises(InterruptedError):
            controller.run(pause_for_loss=pause)
        self.assertTrue(any(e["kind"] == "klystron_power_pause" for e in events))
        self.assertTrue(plant.writes[-1][1])

    def test_feedback_checks_power_during_wait_even_without_ict_poll(self):
        machine = FeedbackMachine(FeedbackConfig(settle_sec=0), lambda: False, lambda: None,
                                  lambda event: None, pv_factory=Mock())
        machine.power_guard = guard()
        machine.power_guard.update(pvname=POWER_PVS[0], value=0., timestamp=102.)
        with self.assertRaises(KlystronPowerLoss):
            machine.wait(.1)
        machine.pv_factory.assert_not_called()

    def test_feedback_explicitly_disabled_ict_pause_accepts_zero_transmission(self):
        config = FeedbackConfig(ict_auto_pause=False)
        self.assertFalse(config.ict_auto_pause)
        events = []
        machine = FeedbackMachine(config, lambda: False, lambda: None, events.append, pv_factory=Mock())
        for shot in range(25):
            for pv in machine.ict_pvs:
                machine.check_ict(pv, 0., shot)
        self.assertEqual(events, [])
        self.assertEqual(list(machine._ict_loss_counts.values()), [0, 0])
        up, down = machine.ict_pvs
        machine.fresh_records = lambda names: [{up: dict(value=1., timestamp=i), down: dict(value=0., timestamp=i)}
                                               for i in range(10)]
        self.assertEqual(machine.transmission(), 0.)
        for shot in range(10):
            machine.monitor_transmission(up, dict(value=1., timestamp=shot))
            machine.monitor_transmission(down, dict(value=0., timestamp=shot))
        live = [e for e in events if e["kind"] == "transmission_monitor"]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["transmission"], 0.)
        controller = FeedbackController(machine, config, {pv: (-1000., 1000.) for pv in PVS}, events.append)
        controller.current = dict.fromkeys(PVS, 0.)
        self.assertEqual(controller.transmission(), 0.)
        machine.power_guard = guard()
        machine.power_guard.update(pvname=POWER_PVS[8], value=0., timestamp=102.)
        with self.assertRaises(KlystronPowerLoss):
            machine.check_beam()

    def test_invalid_zero_denominator_times_out_without_ict_auto_pause(self):
        events = []
        stamps = itertools.count()
        factory = lambda *args, **kwargs: Mock(get_with_metadata=lambda **kw: dict(value=0., timestamp=next(stamps)))
        machine = FeedbackMachine(FeedbackConfig(sample_timeout_sec=.01, ict_auto_pause=False), lambda: False, lambda: None,
                                  events.append, pv_factory=factory)
        with self.assertRaises(TimeoutError):
            machine.transmission()
        self.assertFalse(any(e["kind"] in ("auto_pause", "beam_loss_detected", "beam_loss_pending") for e in events))

    def test_default_ict_interlock_trips_on_twentieth_distinct_shot_per_channel(self):
        self.assertTrue(FeedbackConfig().ict_auto_pause)
        for index in (0, 1):
            machine = FeedbackMachine(FeedbackConfig(), lambda: False, lambda: None, Mock(), pv_factory=Mock())
            pv, other = machine.ict_pvs[index], machine.ict_pvs[1 - index]
            for stamp in range(1, 20):
                machine.check_ict(pv, 0., stamp)
                machine.check_ict(pv, -1., stamp)  # Cached shot cannot advance the counter.
                machine.check_ict(other, 1., stamp)
            self.assertEqual(machine._ict_loss_counts[pv], 19)
            self.assertEqual(machine._ict_loss_counts[other], 0)
            with self.assertRaisesRegex(BeamLoss, "20 consecutive shots"):
                machine.check_ict(pv, 0., 20)
            machine.check_ict(pv, 1., 21)
            self.assertEqual(machine._ict_loss_counts[pv], 0)


if __name__ == "__main__":
    unittest.main()
