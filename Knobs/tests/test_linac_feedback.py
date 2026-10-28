"""Offline feedback control, EPICS protocol and GUI interlock regressions."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from Knobs.linac_feedback import (FeedbackConfig, FeedbackController, RecentResponse,
                                 CURRENT_PVS, PVS, TIMING_PV, PHASE_PV)
from Knobs.linac_feedback_io import FeedbackMachine


class Plant:
    def __init__(self):
        self.values = dict.fromkeys(PVS, 0.)
        self.values[TIMING_PV] = 100.37
        self.target_timing, self.target_phase = 105.97, 0.
        self.reference = np.array([.5, -.2, .7, -.4])
        self.drift = np.zeros(4)
        # Column order: H1, V1, H2, V2. Include cross-plane coupling.
        self.response = np.array([[2., .1, .3, 0.], [.3, 0., 1.5, .1],
                                  [.1, 1.8, 0., .2], [0., .4, .1, 1.6]])
        self.writes = []
        self.loss = 0.
        self.stopped = False
        self.on_write = lambda *_: None

    def read(self, pv):
        return self.values[pv]

    def write(self, values, restore=False):
        if not restore:
            self.checkpoint()
        self.writes.append((dict(values), restore))
        self.values.update(values)
        self.on_write(values, restore)

    def checkpoint(self):
        if self.stopped:
            raise InterruptedError("Stop")

    def orbit(self):
        self.checkpoint()
        return self.reference + self.drift + self.response @ np.array([self.values[pv] for pv in CURRENT_PVS])

    def transmission(self):
        self.checkpoint()
        timing = (self.values[TIMING_PV] - self.target_timing) / 2.8
        phase = (self.values[PHASE_PV] - self.target_phase) / .5
        residual = self.orbit() - self.reference
        return max(0., .95 - .001 * timing ** 2 - .003 * phase ** 2 - .1 * np.dot(residual, residual) - self.loss)

    def wait(self, seconds):
        self.checkpoint()


class FeedbackTests(unittest.TestCase):
    def controller(self, plant=None, **options):
        plant = plant or Plant()
        events = []
        config = replace(FeedbackConfig(settle_sec=0, interval_sec=0), **options)
        limits = {pv: (-10000., 10000.) for pv in PVS}
        limits.update({pv: (-.5, .5) for pv in CURRENT_PVS})
        controller = FeedbackController(plant, config, limits, events.append)
        controller.initialize()
        return plant, controller, events

    def test_timing_tracks_moving_local_optimum_on_relative_grid(self):
        plant, controller, events = self.controller()
        controller.cycle()
        self.assertAlmostEqual(plant.values[TIMING_PV], plant.target_timing)
        plant.target_timing += 5.6
        controller.cycle()
        self.assertAlmostEqual(plant.values[TIMING_PV], plant.target_timing)
        plant.target_timing -= 8.4
        controller.cycle()
        self.assertAlmostEqual(plant.values[TIMING_PV], plant.target_timing)
        for values, _ in plant.writes:
            self.assertEqual(set(values), {TIMING_PV})
            self.assertAlmostEqual((values[TIMING_PV] - 100.37) / 2.8,
                                   round((values[TIMING_PV] - 100.37) / 2.8))

    def test_decline_then_orbit_then_timing_then_phase_then_timing(self):
        plant, controller, events = self.controller()
        controller.cycle()
        reference = controller.reference.copy()
        plant.drift = np.array([.26, -.17, .24, -.22])
        plant.target_phase = 1.
        start = len(events)
        controller.cycle()
        states = [e["state"] for e in events[start:] if e["kind"] == "state"]
        self.assertEqual(states, ["Timing tracking", "Learning corrector response", "Correcting orbit",
                                  "Timing after orbit correction", "CM8L phase recovery", "Monitoring"])
        self.assertLess(np.max(abs(plant.orbit() - reference)), .02)
        self.assertAlmostEqual(plant.values[PHASE_PV], 1.)
        np.testing.assert_array_equal(controller.reference, reference)
        # The next cycle always begins with timing, including after CM8L recovery.
        start = len(events)
        controller.cycle()
        self.assertEqual(next(e["state"] for e in events[start:] if e["kind"] == "state"), "Timing tracking")
        previous = dict.fromkeys(CURRENT_PVS, 0.)
        for values, _ in plant.writes:
            for pv, value in values.items():
                if pv in CURRENT_PVS:
                    self.assertLessEqual(abs(value - previous[pv]), .0100000001)
                    previous[pv] = value
        self.assertTrue(any(e["kind"] == "response" and e["observations"] > 4 for e in events))

    def test_orbit_threshold_and_decline_are_both_required(self):
        for drift, loss, threshold, expected in ((.099, .01, 100, False), (.1, .01, 100, True),
                                                (.12, .01, 150, False), (.12, -.1, 100, False)):
            with self.subTest(drift=drift, loss=loss, threshold=threshold):
                plant, controller, events = self.controller(orbit_threshold_um=threshold)
                controller.cycle()
                plant.drift[0], plant.loss = drift, loss
                # Keep transmission in its physical range in the improving case.
                if loss < 0:
                    plant.loss = -.002
                controller.cycle()
                self.assertEqual(any(e.get("state") == "Learning corrector response" for e in events), expected)

    def test_phase_only_when_below_fixed_initial_transmission(self):
        plant, controller, events = self.controller()
        controller.cycle()
        initial = controller.initial_transmission
        plant.target_phase = -1.5
        controller.cycle()
        self.assertAlmostEqual(plant.values[PHASE_PV], -1.5)
        self.assertEqual(controller.initial_transmission, initial)
        self.assertFalse(any(e.get("state") == "Correcting orbit" for e in events))
        for values, _ in plant.writes:
            if PHASE_PV in values:
                self.assertAlmostEqual(values[PHASE_PV] / .5, round(values[PHASE_PV] / .5))

    def test_stop_and_failure_restore_temporary_timing_phase_and_current(self):
        for pv in (TIMING_PV, PHASE_PV, CURRENT_PVS[0]):
            for stop in (True, False):
                with self.subTest(pv=pv, stop=stop):
                    plant, controller, _ = self.controller()
                    original = dict(plant.values)
                    def fail(values, restore):
                        if pv in values and not restore:
                            if stop:
                                plant.stopped = True
                            else:
                                raise ValueError("Read failure after write")
                    plant.on_write = fail
                    with self.assertRaises((InterruptedError, ValueError)):
                        if pv in CURRENT_PVS:
                            controller.learn_response()
                        else:
                            controller.search(pv, 2.8 if pv == TIMING_PV else .5, "test")
                    self.assertEqual(plant.values, original)
                    self.assertTrue(plant.writes[-1][1])

    def test_bad_correction_is_rolled_back_but_updates_model(self):
        plant, controller, events = self.controller()
        controller.learn_response()
        samples = len(controller.response.samples)
        # Wrong model after an abrupt machine change; trial must be rejected.
        plant.response *= -1
        plant.drift = np.array([.25, .2, .3, .2])
        controller.learn_response = lambda: None
        controller.correct_orbit()
        np.testing.assert_array_equal(controller.currents(), np.zeros(4))
        self.assertGreater(len(controller.response.samples), samples)
        self.assertTrue(any(e["kind"] == "orbit_step" and not e["accepted"] for e in events))

    def test_range_boundary_stops_without_claiming_unbounded_optimum(self):
        plant, controller, _ = self.controller(timing_range_ns=2.8)
        plant.target_timing = 1000.
        # Avoid the zero-transmission plateau for this boundary test.
        plant.transmission = lambda: .5 + (plant.values[TIMING_PV] - 100.37) / 100.
        with self.assertRaisesRegex(RuntimeError, "range boundary"):
            controller.cycle()
        self.assertAlmostEqual(plant.values[TIMING_PV], 103.17)

    def test_recent_response_learns_new_response_and_requires_excitation(self):
        model = RecentResponse()
        dx = np.array([.01, 0., 0., 0.])
        for _ in range(5):
            model.add(dx, 2 * dx)
        self.assertIsNone(model.matrix())
        for _ in range(20):
            for dx in np.eye(4) * .01:
                model.add(dx, 3 * dx)
        np.testing.assert_allclose(model.matrix(), np.eye(4) * 3, atol=1e-4)
        for _ in range(20):
            for dx in np.eye(4) * .01:
                model.add(dx, 5 * dx)
        np.testing.assert_allclose(model.matrix(), np.eye(4) * 5, atol=1e-4)
        self.assertEqual(len(model.samples), 64)

    def test_linear_intensity_drift_does_not_fake_neighbour_improvement(self):
        plant, controller, _ = self.controller()
        origin = plant.values[TIMING_PV]
        calls = 0
        def drifting_measurement():
            nonlocal calls
            calls += 1
            return .8 - .001 * ((plant.values[TIMING_PV] - origin) / 2.8) ** 2 + .002 * calls
        plant.transmission = drifting_measurement
        controller.search(TIMING_PV, 2.8, "Timing")
        self.assertEqual(plant.values[TIMING_PV], origin)


class MachineTests(unittest.TestCase):
    def machine(self, factory):
        machine = FeedbackMachine(FeedbackConfig(settle_sec=0, sample_timeout_sec=.01),
                                  lambda: False, lambda: None, lambda e: None, factory)
        machine.wait = lambda *args, **kw: None
        return machine

    def test_three_new_timestamps_required_and_stale_records_time_out(self):
        pv = Mock()
        stamps = iter((1, 1, 2, 2, 3, 4))
        pv.get_with_metadata.side_effect = lambda **kw: dict(timestamp=next(stamps), value=1.)
        machine = self.machine(lambda *a, **kw: pv)
        records = machine.fresh_records(("test",))
        self.assertEqual([r["test"]["timestamp"] for r in records], [2, 3, 4])
        pv.get_with_metadata.side_effect = lambda **kw: dict(timestamp=5., value=1.)
        with self.assertRaises(TimeoutError):
            machine.fresh_records(("test",))

    def test_monitor_mapping_units_status_and_exact_three_mean(self):
        from machine_interfaces_atf2.InterfaceATF2_Linac import LINAC_MONITORS
        records = []
        for i in range(3):
            raw = np.zeros((len(LINAC_MONITORS), 20))
            for name, x, y in (("ML8L", 100 + i * 100, -300), ("ML10L", 400, 500 + i * 100)):
                raw[LINAC_MONITORS.index(name), :4] = [1, x, y, 10]
            records.append({"LINAC:monitors": dict(value=raw.ravel(), timestamp=i)})
        machine = self.machine(Mock())
        machine.fresh_records = lambda names: records
        np.testing.assert_allclose(machine.orbit(), [.2, .4, -.3, .6])
        records[0]["LINAC:monitors"]["value"][LINAC_MONITORS.index("ML8L") * 20] = 0
        with self.assertRaisesRegex(ValueError, "Invalid/no-beam"):
            machine.orbit()

    def test_transmission_is_ratio_of_three_means(self):
        machine = self.machine(Mock())
        machine.fresh_records = lambda names: [dict(zip(names, [dict(value=u, timestamp=i), dict(value=d, timestamp=i)]))
                                               for i, (u, d) in enumerate(((2, 1), (4, 3), (6, 3)))]
        self.assertAlmostEqual(machine.transmission(), 7 / 12)

    def test_unverified_limits_put_failure_and_readback_failure(self):
        pv = Mock()
        pv.get_ctrlvars.return_value = {}
        machine = self.machine(lambda *a, **kw: pv)
        with self.assertRaisesRegex(ValueError, "verify"):
            machine.limits()
        pv.put.assert_not_called()
        pv.get_ctrlvars.return_value = dict(lower_ctrl_limit=-3., upper_ctrl_limit=3.)
        machine.limits()
        pv.put.return_value = -1
        with self.assertRaisesRegex(RuntimeError, "PUT failed"):
            machine.write({CURRENT_PVS[0]: .01})
        pv.put.return_value = 1
        pv.get.return_value = None
        with self.assertRaisesRegex(ValueError, "Disconnected"):
            machine.write({CURRENT_PVS[0]: .01})

    def test_logging_failure_does_not_block_probe_restoration(self):
        pv = Mock()
        pv.get_ctrlvars.return_value = dict(lower_ctrl_limit=-3., upper_ctrl_limit=3.)
        pv.put.return_value = 1
        pv.get.return_value = 0.
        machine = self.machine(lambda *a, **kw: pv)
        machine.limits()
        machine.emit = Mock(side_effect=OSError("Disk full"))
        with self.assertRaises(OSError):
            machine.write({CURRENT_PVS[0]: .01})
        pv.put.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "Restored settings"):
            machine.write({CURRENT_PVS[0]: 0.}, restore=True)
        pv.put.assert_called_once_with(0., wait=True, timeout=2.)

    def test_restore_attempts_all_currents_if_one_channel_fails(self):
        pvs = {name: Mock() for name in CURRENT_PVS}
        for pv in pvs.values():
            pv.get_ctrlvars.return_value = dict(lower_ctrl_limit=-3., upper_ctrl_limit=3.)
            pv.put.return_value = 1
        pvs[CURRENT_PVS[0]].put.return_value = -1
        machine = self.machine(lambda name, **kw: pvs[name])
        machine.limits()
        with self.assertRaisesRegex(RuntimeError, "Restoration incomplete"):
            machine.write(dict.fromkeys(CURRENT_PVS, 0.), restore=True)
        self.assertTrue(all(pv.put.call_count == 1 for pv in pvs.values()))


_APP = None


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        from PyQt6.QtWidgets import QApplication
        _APP = QApplication.instance() or QApplication([])

    def test_tab_threshold_and_mutual_exclusion_without_live_access(self):
        from Knobs import Linac_Opt as linac
        with patch.object(linac.MainWindow, "_refresh_current_values"), patch.object(
                linac.MainWindow, "_refresh_developer_live_values"), patch("epics.PV", side_effect=AssertionError("hardware")):
            window = linac.MainWindow()
            window._current_value_timer.stop()
            try:
                panel = window.feedback_panel
                self.assertEqual(window.tabs.tabText(window.tabs.indexOf(panel)), "Active Feedback")
                self.assertEqual(panel.config().orbit_threshold_um, 100.)
                panel.fields["orbit_threshold_um"].setValue(150.)
                self.assertEqual(panel.config().orbit_threshold_um, 150.)
                fake = Mock()
                panel.worker = fake
                window._feedback_busy_changed(True)
                self.assertFalse(window.btn_start.isEnabled())
                self.assertFalse(window.reset_initial_btn.isEnabled())
                with patch.object(linac.QMessageBox, "warning") as warning:
                    window.start_optimization()
                    window.start_developer_optimization()
                    self.assertEqual(warning.call_count, 2)
                panel.worker = None
                window._feedback_busy_changed(False)
                window.worker = fake
                with patch.object(linac.QMessageBox, "warning") as warning:
                    panel.start()
                    warning.assert_called_once()
                window.worker = None
                self.assertTrue(window.btn_start.isEnabled())
            finally:
                panel.worker = None
                window.worker = None
                _APP.processEvents()
                window.close()
                _APP.processEvents()

    def test_worker_journals_reference_commands_and_stop(self):
        from Knobs.linac_feedback_gui import FeedbackWorker
        plant = Plant()
        plant.limits = lambda: {pv: (-1000., 1000.) for pv in PVS}
        plant.close = Mock()
        plant.wait = lambda seconds: (_ for _ in ()).throw(InterruptedError("done"))
        with tempfile.TemporaryDirectory() as directory:
            worker = FeedbackWorker(FeedbackConfig(settle_sec=0), directory, lambda *args: plant)
            worker.run()
            events = [json.loads(line) for line in (worker.run_dir / "events.jsonl").read_text().splitlines()]
            self.assertTrue(any(e["kind"] == "reference" for e in events))
            self.assertTrue(events[-1]["state"].startswith("Stopped"))
            self.assertTrue((worker.run_dir / "config.json").exists())
            plant.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
