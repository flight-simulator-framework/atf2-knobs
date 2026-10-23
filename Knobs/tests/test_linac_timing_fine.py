"""Timing carry/borrow and fine-only GUI/worker regressions, without EPICS."""
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PyQt6.QtWidgets import QApplication, QMessageBox
from Knobs import Linac_Opt as opt

_APP = None  # Keep Qt alive when this module shares a process with other GUI tests.


class TimingInterface:
    def __init__(self, coarse=100, fine=18):
        self.values = {opt.TIMING_PV: coarse, opt.TIMING_FINE_PV: fine}
        self.writes = []

    def read_current(self, pv_write, pv_read=None, quantize_phase=True):
        return self.values[pv_write]

    def pv_put_many(self, values):
        self.writes.append(dict(values))
        self.values.update(values)

    def write_setpoints_and_wait(self, values):
        self.pv_put_many(values)


class TimingFineTests(unittest.TestCase):
    def test_nine_relative_points_for_every_fine_digit(self):
        for coarse in (0, 100):
            for fine in range(20):
                with self.subTest(coarse=coarse, fine=fine):
                    points = opt.timing_fine_scan(coarse, fine)
                    self.assertEqual([delta for delta, _ in points], list(range(-20, 21, 5)))
                    self.assertEqual(len({tuple(v.values()) for _, v in points}), 9)
                    for delta, values in points:
                        c, f = values[opt.TIMING_PV], values[opt.TIMING_FINE_PV]
                        self.assertIsInstance(c, int)
                        self.assertIsInstance(f, int)
                        self.assertTrue(0 <= f <= 19)
                        self.assertEqual(20 * c + f - (20 * coarse + fine), delta)
                        self.assertEqual(f % 5, fine % 5)
        self.assertEqual(dict(opt.timing_fine_scan(100, 18))[5],
                         {opt.TIMING_PV: 101, opt.TIMING_FINE_PV: 3})
        self.assertEqual(dict(opt.timing_fine_scan(100, 2))[-5],
                         {opt.TIMING_PV: 99, opt.TIMING_FINE_PV: 17})

    def test_invalid_fine_digits_are_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            interface = TimingInterface()
            worker = opt.OptimizationWorker({}, directory, interface=interface)
            for value in (-1, 20, 1.5, np.nan, np.inf):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        opt.timing_fine_scan(100, value)
                    with self.assertRaises(ValueError):
                        worker._write_setpoints({opt.TIMING_FINE_PV: value})
                    interface.values[opt.TIMING_FINE_PV] = value
                    with self.assertRaises(ValueError):
                        worker._run_timing_fine()
            self.assertEqual(interface.writes, [])

    def test_fine_only_run_all_modes_measures_nine_pairs_and_applies_best(self):
        for mode in ("SEQUENTIAL", "GROUP_BO", "GROUP_LBO"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory, patch.object(
                    opt, "InterfaceATF2_LinacBT", side_effect=AssertionError("hardware")), patch(
                    "epics.PV", side_effect=AssertionError("hardware")):
                interface = TimingInterface()
                config = {"mode": mode, "timing_fine": True, "settle_sec": 0,
                          "restore_pvs": [], "seq_method": "TERNARY"}
                worker = opt.OptimizationWorker(config, directory, interface=interface)
                # Maximum is +2.8 ns from (100, 18), crossing the digit boundary.
                def measure():
                    ticks = 20 * interface.values[opt.TIMING_PV] + interface.values[opt.TIMING_FINE_PV]
                    score = 100. - abs(ticks - 2023)
                    return {"Ttot": 1., "DR": score}, np.nan, score, "DR", score, True, ""
                worker._measure_objective = Mock(side_effect=measure)
                progress = []
                worker._emit_progress = progress.append
                worker.run()
                self.assertFalse([p for p in progress if p["kind"] == "run_error"])
                self.assertEqual(len(interface.writes), 10)  # nine points + best remeasurement
                self.assertEqual({tuple(v.values()) for v in interface.writes[:9]},
                                 {tuple(v.values()) for _, v in opt.timing_fine_scan(100, 18)})
                self.assertEqual(interface.writes[-1], {opt.TIMING_PV: 101, opt.TIMING_FINE_PV: 3})
                self.assertEqual(worker._measure_objective.call_count, 11)  # includes initial snapshot
                with open(worker.run_dir / f"InitialSnapshot_{worker.run_tag}.json") as stream:
                    snapshot = json.load(stream)
                self.assertEqual(snapshot["initial_setpoints"],
                                 {opt.TIMING_PV: 100, opt.TIMING_FINE_PV: 18})
                worker._finish_at_best()
                self.assertEqual(interface.writes[-1], {opt.TIMING_PV: 101, opt.TIMING_FINE_PV: 3})

    def test_fine_immediately_follows_coarse_before_qa_qm_and_correctors(self):
        for mode in ("SEQUENTIAL", "GROUP_BO", "GROUP_LBO"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                worker = opt.OptimizationWorker({"mode": mode, "timing": True,
                    "correctors": True, "timing_fine": True, "qa": True, "qm": True},
                    directory, interface=TimingInterface())
                worker._dump_initial_snapshot = Mock()
                events = []
                current = [100]
                worker._read_current = lambda *args: current[0]
                def record(group):
                    events.append(group)
                    if group == "Timing": current[0] = 110
                worker.perform_1d_scan = lambda *args, **kw: record(kw["group"])
                worker._lbo_group = lambda group, params: record(group)
                worker._group_bo_simultaneous = lambda group, params: record(group)
                worker._run_correctors = lambda: events.append("correctors")
                def fine():
                    events.append("fine")
                    self.assertEqual(worker._read_current(opt.TIMING_PV), 110)
                worker._run_timing_fine = fine
                worker.run()
                self.assertEqual(events[:2], ["Timing", "fine"])
                self.assertGreater(events.index("QA"), events.index("fine"))
                self.assertGreater(events.index("QM"), events.index("fine"))
                self.assertEqual(events[-1], "correctors")
                self.assertEqual(events.count("fine"), 1)

    def test_combined_timing_refined_after_correctors_once(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = opt.OptimizationWorker({"correctors_l8": True, "correctors": True,
                "timing_fine": True}, directory, interface=TimingInterface())
            worker._dump_initial_snapshot = Mock();events = []
            worker._run_correctors = lambda: events.append("combined coarse")
            worker._run_timing_fine = lambda: events.append("fine")
            worker.run()
            self.assertEqual(events, ["combined coarse", "fine"])

    def test_fine_pair_may_carry_past_combined_coarse_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            interface = TimingInterface(coarse=110)
            config = {"correctors": True, "timing_fine": True, "settle_sec": 0,
                      "developer_actuators": [{"pv_write": opt.TIMING_PV}],
                      "actuator_limits": {opt.TIMING_PV: [90, 110]}}
            worker = opt.OptimizationWorker(config, directory, interface=interface)
            worker._measure_objective = lambda: ({}, np.nan, 1., "DR", 1., True, "")
            worker._run_timing_fine()
            self.assertIn({opt.TIMING_PV: 111, opt.TIMING_FINE_PV: 18}, interface.writes)
            # Same score at every point leaves the starting timing selected.
            self.assertEqual(interface.writes[-1], {opt.TIMING_PV: 110, opt.TIMING_FINE_PV: 18})
            with self.assertRaises(ValueError):
                worker._write_setpoints({opt.TIMING_PV: 112, opt.TIMING_FINE_PV: 18})
            with self.assertRaises(ValueError):
                worker._write_setpoints({opt.TIMING_PV: 111})

    def test_invalid_measurements_restore_origin_and_stop_does_not_apply_best(self):
        for stop in (False, True):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as directory:
                interface = TimingInterface()
                worker = opt.OptimizationWorker({"settle_sec": 0}, directory, interface=interface)
                def measure():
                    if stop:
                        worker.stop()
                    return {}, np.nan, 0., "DR", 0., False, "invalid"
                worker._measure_objective = measure
                if stop:
                    with self.assertRaises(InterruptedError):
                        worker._run_timing_fine()
                    self.assertEqual(len(interface.writes), 1)
                else:
                    with self.assertRaisesRegex(ValueError, "no valid measurements"):
                        worker._run_timing_fine()
                    self.assertEqual(interface.writes[-1], {opt.TIMING_PV: 100, opt.TIMING_FINE_PV: 18})

    def test_gui_allows_fine_only_and_round_trips_selection(self):
        global _APP
        _APP = QApplication.instance() or QApplication([])
        with patch.object(opt.MainWindow, "_refresh_current_values"), patch.object(
                opt.MainWindow, "_refresh_developer_live_values"), patch(
                "epics.PV", side_effect=AssertionError("hardware")):
            window = opt.MainWindow()
            window._current_value_timer.stop()
            try:
                self.assertFalse(window.chk_timing_fine.isChecked())
                for box in (window.chk_gun_sol_l0_phase, window.chk_kly_phase, window.chk_timing,
                            window.chk_qa, window.chk_qm, window.chk_correctors):
                    box.setChecked(False)
                window.chk_timing_fine.setChecked(True)
                window.objective_box.setCurrentText("ICT")
                # The parent enables both children without changing their selections.
                window.chk_timing_group.setChecked(False)
                self.assertFalse(window.chk_timing.isEnabled())
                self.assertFalse(window.chk_timing_fine.isEnabled())
                self.assertTrue(window.chk_timing_fine.isChecked())
                self.assertEqual(window._selected_target_keys(), [])
                with patch.object(QMessageBox, "warning") as warning, patch.object(
                        window, "_capture_machine_origin") as capture:
                    window.start_optimization()
                warning.assert_called_once()
                capture.assert_not_called()
                window.chk_timing_group.setChecked(True)
                self.assertTrue(window.chk_timing.isEnabled())
                self.assertTrue(window.chk_timing_fine.isEnabled())
                self.assertEqual(window._selected_target_keys(), ["timing_group", "timing_fine"])
                with patch.object(window, "_capture_machine_origin", side_effect=ValueError("offline stop")) as capture, patch.object(
                        window, "_checked_corrector_ranges", return_value=({}, [])), patch.object(
                        QMessageBox, "critical"), patch.object(QMessageBox, "warning") as warning:
                    window.start_optimization()
                warning.assert_not_called()
                config = capture.call_args.args[0]
                self.assertTrue(config["timing_group"])
                self.assertTrue(config["timing_fine"])
                self.assertFalse(config["timing"])
                self.assertIn(opt.TIMING_FINE_PV, config["restore_pvs"])
                self.assertEqual([s[1] for s in opt._main_run_delta_specs(config)],
                                 [opt.TIMING_PV, opt.TIMING_FINE_PV])
                window._apply_config_to_ui({})
                self.assertFalse(window.chk_timing_fine.isChecked())
                window._apply_config_to_ui(config)
                self.assertEqual(window._selected_target_keys(), ["timing_group", "timing_fine"])
                # Legacy fine-only configs infer the parent selection.
                window._apply_config_to_ui({k: v for k, v in config.items() if k != "timing_group"})
                self.assertTrue(window.chk_timing_group.isChecked())
                window._prepare_target_states_for_run("SEQUENTIAL")
                window._set_target_state("timing_fine", "running")
                self.assertEqual(window.target_states["timing_group"], "running")
                window._set_target_state("timing_fine", "done")
                self.assertEqual(window.target_states["timing_group"], "done")
                window._set_main_run_controls_enabled(False)
                self.assertFalse(window.chk_timing_group.isEnabled())
                self.assertFalse(window.chk_timing_fine.isEnabled())
                self.assertNotIn(opt.TIMING_FINE_PV, opt._restore_pvs_for_config({}))
            finally:
                _APP.processEvents()
                window.close()
                _APP.processEvents()


if __name__ == "__main__":
    unittest.main()
