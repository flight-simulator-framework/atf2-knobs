"""Offline intensity, bounds and GUI regressions. No real PV access."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np
from PyQt6.QtWidgets import QApplication, QMessageBox
from Knobs import Linac_Opt as opt


def data(status=1):
    return {"names": ["A", "B"], "status": np.full((3, 2), status),
            "intensity": np.array([[2., 20.], [2., 20.], [2., 20.]])}


class IntensityTests(unittest.TestCase):
    def test_group_budget_formula_and_overrides(self):
        for dim in (1, 2, 4, 8, 16, 32, 64):
            budget = opt._auto_group_bo_budget(dim)
            self.assertEqual(budget["min_init"], 2 * dim + 8)
            self.assertEqual(budget["max_evals"], 10 * dim + 20)
            self.assertEqual(opt._resolve_group_bo_max_evals({}, "Correctors", dim), 10 * dim + 20)
        self.assertEqual(opt._resolve_group_bo_max_evals({"gbo_max_evals_correctors": 77}, "Correctors", 8), 77)
        self.assertEqual(opt._resolve_group_bo_max_evals({"gbo_max_evals": 65}, "Correctors", 8), 65)
        self.assertEqual(opt._resolve_group_bo_max_evals(
            {"gbo_max_evals_correctors": 77, "gbo_correctors_auto": True}, "Correctors", 8), 100)
        self.assertEqual(opt._resolve_group_bo_max_evals({"gbo_max_evals_qa": 91}, "QA", 5), 91)

    def test_group_budget_gui_tracks_corrector_count_and_manual_override(self):
        app = QApplication.instance() or QApplication([])
        with patch.object(opt.MainWindow, "_refresh_current_values"):
            window = opt.MainWindow()
            window._current_value_timer.stop()
            try:
                window.mode_box.setCurrentText("GROUP_BO")
                spin = window.group_bo_max_eval_spinboxes["correctors"]
                self.assertTrue(window.corrector_budget_auto.isChecked())
                for row in (0, 1):
                    window.dev_actuator_table.item(row, 0).setCheckState(opt.Qt.CheckState.Checked)
                self.assertEqual(spin.value(), 40)
                self.assertIn("D=2", window.group_bo_budget_labels["correctors"].text())
                self.assertIn("init ≤ 12", window.group_bo_budget_labels["correctors"].text())
                self.assertFalse(spin.isEnabled())
                window.corrector_budget_auto.setChecked(False)
                self.assertTrue(spin.isEnabled())
                spin.setValue(77)
                window.dev_actuator_table.item(2, 0).setCheckState(opt.Qt.CheckState.Checked)
                self.assertEqual(spin.value(), 77)
                self.assertIn("D=3", window.group_bo_budget_labels["correctors"].text())
                spin.setValue(5)
                self.assertIn("init ≤ 5", window.group_bo_budget_labels["correctors"].text())
                window.corrector_budget_auto.setChecked(True)
                self.assertEqual(spin.value(), 50)
                self.assertEqual(window.group_bo_max_eval_spinboxes["qa"].value(), 70)
                self.assertEqual(window.group_bo_max_eval_spinboxes["timing"].value(), 30)
            finally:
                window.close()
                app.processEvents()

    def test_group_budget_limit_and_small_grid_exhaustion(self):
        for dimension, half_range, max_evals, expected_count, reason in (
                (1, 1., 30, 3, "candidate_exhausted"),
                (2, 2., 14, 14, "max_evals_reached(14)")):
            with self.subTest(dimension=dimension), tempfile.TemporaryDirectory() as directory, patch.object(opt, "InterfaceATF2_LinacBT"):
                config = {"gbo_max_evals_correctors": max_evals,
                          "gbo_uncertainty_rel_tol": 0., "gbo_uncertainty_abs_tol": 0.}
                worker = opt.OptimizationWorker(config, directory)
                worker._read_current = Mock(return_value=0.)
                worker._try_reuse_initial_vector = Mock(return_value=None)
                worker._evaluate_point_multi = Mock(return_value=SimpleNamespace(score=1., ict={}, bpm_metric=1.))
                worker._objective_metric_text = Mock(return_value="score=1")
                worker._gp_posterior_nd = Mock(side_effect=lambda x, y, c, **kw: (np.zeros(len(c)), np.ones(len(c))))
                messages = []
                worker.log_signal.connect(messages.append)
                params = [(f"Z{i}", f"Z{i}:currentWrite", 1., half_range) for i in range(dimension)]
                worker._group_bo_simultaneous("Correctors", params)
                calls = worker._evaluate_point_multi.call_args_list
                self.assertEqual(len(calls), expected_count + 1)
                self.assertEqual(calls[-1].kwargs["note"], "OPTIMIZED_SET")
                vectors = [tuple(call.kwargs["pv_to_value"].values()) for call in calls[:-1]]
                self.assertEqual(len(set(vectors)), expected_count)
                self.assertTrue(any(f"stop_reason={reason}" in message for message in messages))

    def test_gp_log_is_only_shown_for_bo_scans(self):
        for method in ("TERNARY", "BO"):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as directory, patch.object(opt, "InterfaceATF2_LinacBT"):
                worker = opt.OptimizationWorker({"seq_method": method, "bo_max_evals": 3,
                                                 "bo_refine": False}, directory)
                messages = []
                worker.log_signal.connect(messages.append)
                worker._try_reuse_initial_point = Mock(return_value=None)
                worker._evaluate_point = Mock(return_value=SimpleNamespace(score=1., ict={"Ttot": 1.}, bpm_metric=1.))
                worker._objective_metric_text = Mock(return_value="score=1")
                worker.perform_1d_scan("L0 phase", "CM0L:phaseWrite", np.array([-1., 0., 1.]))
                self.assertTrue(worker._evaluate_point.called)
                self.assertEqual(any("[BO1D][GP]" in message for message in messages), method == "BO")
                expected_label = "TERNARY-1D" if method == "TERNARY" else "BO-1D"
                self.assertTrue(any(f"[{expected_label}] Optimizing" in message for message in messages))

    def test_intensity_plot_beamline_order_and_dr_wrap(self):
        names = opt.INTENSITY_PLOT_BPM_NAMES
        expected_dr = [f"MB{i}R" for i in list(range(21, 99)) + list(range(1, 21))
                       if i not in (20, 75)]
        self.assertEqual(list(names),
                         [n for n in opt.LINAC_BT_BPM_NAMES if n.endswith("L")]
                         + [n for n in opt.LINAC_BT_BPM_NAMES if n.endswith("T")]
                         + expected_dr)
        self.assertEqual(set(names), set(opt.INTENSITY_BPM_NAMES))
        self.assertEqual(len(names), len(set(names)))

    def test_mixed_sources_sampling_baseline_and_failure(self):
        linac_data, dr_data = data(), data()
        linac_data["names"] = ["MB5L", "ML1T"]
        dr_data["names"] = ["MB1R", "MB2R"]
        linac = SimpleNamespace(nsamples=1, get_bpms=Mock())
        dr = SimpleNamespace(nsamples=1, bpm_sample_interval_s=.5, get_bpms=Mock())
        def read(reader, payload):
            self.assertEqual(reader.nsamples, 3)
            self.assertEqual(reader.bpm_sample_interval_s, .5)
            return payload
        linac.get_bpms.side_effect = lambda: read(linac, linac_data)
        dr.get_bpms.side_effect = lambda: read(dr, dr_data)
        names = ["MB1R", "MB5L", "ML1T"]
        snapshot = {"old": 123.}
        values = opt.read_selected_bpm_intensities(linac, dr, names, baseline=True, snapshot_out=snapshot)
        self.assertEqual(values, {"MB1R": 2., "MB5L": 2., "ML1T": 20.})
        self.assertEqual(list(values), names)
        self.assertEqual(opt.intensity_score(values, values, "relative_mean"), 1.)
        self.assertEqual(opt.intensity_sources(names), ["LINAC:monitors", "DR:monitors"])
        self.assertEqual(set(snapshot), {"MB1R", "MB2R", "MB5L", "ML1T"})
        self.assertEqual((linac.nsamples, dr.nsamples), (1, 1))
        self.assertFalse(hasattr(linac, "bpm_sample_interval_s"))
        dr.get_bpms.reset_mock()
        opt.read_selected_bpm_intensities(linac, dr, ["ML1T"])
        dr.get_bpms.assert_not_called()
        linac_data["status"][:, 1] = 0
        with self.assertRaises(RuntimeError):
            opt.read_selected_bpm_intensities(linac, dr, names)
        linac.get_bpms.reset_mock()
        unchecked = opt.read_selected_bpm_intensities(linac, dr, names, check_status=False, snapshot_out=snapshot)
        self.assertEqual(unchecked, values)
        self.assertEqual(snapshot["ML1T"], 20.)
        linac.get_bpms.assert_called_once_with()
        self.assertEqual(linac.nsamples, 1)
        self.assertFalse(hasattr(linac, "bpm_sample_interval_s"))
        with self.assertRaises(ValueError):
            opt.read_selected_bpm_intensities(linac, dr, ["unknown"])

    def test_linac_bt_waveform_matches_each_name_without_duplicates(self):
        from machine_interfaces_atf2.InterfaceATF2_Linac import InterfaceATF2_Linac, LINAC_MONITORS, _build_beamline
        interface = InterfaceATF2_Linac.__new__(InterfaceATF2_Linac)
        _, interface.bpms, _, interface.bpm_indexes = _build_beamline(opt.BT_SEQUENCE, LINAC_MONITORS)
        interface.nsamples, interface.bpm_sample_interval_s = 3, .5
        self.assertEqual(tuple(interface.bpms), opt.LINAC_BT_BPM_NAMES)
        self.assertEqual(len(interface.bpms), len(set(interface.bpms)))
        raw = np.zeros((len(LINAC_MONITORS), 20))
        raw[:, 0] = 1
        raw[:, 3] = np.arange(len(raw)) + 100
        module = "machine_interfaces_atf2.InterfaceATF2_Linac"
        with patch(module + ".PV") as pv, patch(module + ".time.sleep") as sleep:
            pv.return_value.get.return_value = raw.reshape(-1)
            result = interface.get_bpms()
            pv.assert_called_once_with("LINAC:monitors")
            pv.return_value.put.assert_not_called()
            self.assertEqual([call.args for call in sleep.call_args_list], [(.5,)] * 3)
        for i, name in enumerate(result["names"]):
            np.testing.assert_equal(result["intensity"][:, i], [100 + LINAC_MONITORS.index(name)] * 3)

    def test_gui_mixed_region_selection_and_manual_snapshot(self):
        app = QApplication.instance() or QApplication([])
        with patch.object(opt.MainWindow, "_refresh_current_values"):
            window = opt.MainWindow()
            window._current_value_timer.stop()
            try:
                for region, name in (("LINAC", "MB5L"), ("BT", "ML1T"), ("DR", "MB1R")):
                    window.dev_bpm_region_box.setCurrentText(region)
                    for candidate, row in window.dev_bpm_row_map.items():
                        expected_region = "DR" if candidate in opt.DR_BPM_NAMES else opt._machine_region(candidate)
                        self.assertEqual(window.dev_bpm_table.isRowHidden(row), expected_region != region)
                    window.dev_bpm_table.item(window.dev_bpm_row_map[name], 0).setCheckState(opt.Qt.CheckState.Checked)
                window.objective_box.setCurrentText("BPM intensity")
                selected = {"MB5L", "ML1T", "MB1R"}
                self.assertEqual(set(window._selected_developer_bpms()), selected)
                window.objective_box.setCurrentText("BPM sumsq")
                window.objective_box.setCurrentText("BPM intensity")
                self.assertEqual(set(window._selected_developer_bpms()), selected)
                linac, dr = Mock(nsamples=1), Mock(nsamples=1)
                linac.get_correctors.return_value = {"names": [], "bact": []}
                linac.get_bpms.return_value = data()
                linac.get_bpms.return_value["names"] = ["MB5L", "ML1T"]
                dr.get_bpms.return_value = data()
                dr.get_bpms.return_value["names"] = ["MB1R", "MB2R"]
                window.dr_bpm_reader = dr
                with patch.object(window, "_ensure_interface", return_value=linac):
                    window._refresh_developer_live_values()
                    for name in selected:
                        self.assertEqual(window.dev_bpm_table.item(window.dev_bpm_row_map[name], 4).text(), "VALID")
                    config = {"objective_type": opt.DEVELOPER_OBJECTIVE_INTENSITY,
                              "objective_bpm_names": ["MB5L", "ML1T", "MB1R"],
                              "intensity_aggregation": "relative_mean", "restore_pvs": []}
                    origin = window._capture_machine_origin(config)
                    self.assertEqual(origin["initial_score"], 1.)
                    self.assertEqual(config["intensity_baseline"], {"MB5L": 2., "ML1T": 20., "MB1R": 2.})
                    with tempfile.TemporaryDirectory() as directory, patch.object(opt, "InterfaceATF2_LinacBT", return_value=linac), patch.object(opt, "DRBPMReader", return_value=dr):
                        worker = opt.OptimizationWorker(config, directory)
                        self.assertEqual(worker._measure_objective()[4], 1.)
                        self.assertEqual(set(worker._last_intensities), selected)
                        self.assertIn("MB2R", worker._last_bpm_snapshot)
                    config["intensity_aggregation"] = "raw_sum"
                    origin = window._capture_machine_origin(config)
                    self.assertEqual(origin["initial_score"], 24.)
                    self.assertEqual(origin["initial_objective_label"], "BPM intensity raw_sum")
                    dr.get_bpms.return_value["status"][:] = 0
                    with self.assertRaises(RuntimeError):
                        window._capture_machine_origin(config)
                    dr.get_bpms.side_effect = RuntimeError("Disconnected")
                    window._refresh_developer_live_values()
                    self.assertEqual(window.dev_bpm_table.item(window.dev_bpm_row_map["MB1R"], 4).text(), "INVALID")
                    self.assertEqual(window.dev_bpm_table.item(window.dev_bpm_row_map["ML1T"], 4).text(), "VALID")
                linac.pv_put.assert_not_called()
            finally:
                window.close()
                app.processEvents()

    def test_all_bpm_snapshot_does_not_change_selected_objective(self):
        payload = data()
        payload["status"][:, 1] = 0
        interface = SimpleNamespace(nsamples=3, get_bpms=Mock(return_value=payload))
        snapshot = {}
        self.assertEqual(opt.read_bpm_intensities(interface, ["A"], snapshot_out=snapshot), {"A": 2.})
        interface.get_bpms.assert_called_once_with()
        self.assertEqual(snapshot["A"], 2.)
        self.assertTrue(np.isnan(snapshot["B"]))

    def test_aggregation_and_fixed_baseline(self):
        values = opt.bpm_intensity_values(data(), ["B", "A"], 3)
        self.assertEqual(opt.intensity_score(values, {}, "raw_mean"), 11.)
        self.assertEqual(opt.intensity_score(values, {}, "raw_sum"), 22.)
        self.assertEqual(opt.intensity_score({"A": 0., "B": 0.}, {}, "raw_sum"), 0.)
        for invalid in ({}, {"A": np.nan}, {"A": np.inf}):
            with self.assertRaises(ValueError):
                opt.intensity_score(invalid, {}, "raw_sum")
        baseline = {"A": 1., "B": 20.}
        self.assertEqual(opt.intensity_score(values, baseline, "relative_mean"), 1.5)
        self.assertEqual(baseline, {"A": 1., "B": 20.})
        with self.assertRaises(ValueError):
            opt.intensity_score(values, {"A": 0., "B": 20.}, "relative_mean")

    def test_invalid_is_not_zero_or_silently_excluded(self):
        for payload, names in [(data(0), ["A"]), (data(), ["missing"]), ({}, ["A"])]:
            with self.assertRaises(ValueError):
                opt.bpm_intensity_values(payload, names, 3)
        payload = data()
        payload["intensity"][0, 1] = np.nan
        with self.assertRaises(ValueError):
            opt.bpm_intensity_values(payload, ["A", "B"], 3)
        payload = data()
        payload["intensity"][:] = 0
        self.assertEqual(opt.bpm_intensity_values(payload, ["A"])["A"], 0.)

    def test_retries_and_baseline_validation(self):
        interface = SimpleNamespace(nsamples=1, get_bpms=Mock(side_effect=[data(0), data()]))
        self.assertEqual(opt.read_bpm_intensities(interface, ["A"]), {"A": 2.})
        self.assertEqual(interface.nsamples, 1)
        interface.get_bpms = Mock(return_value=data(0))
        with self.assertRaises(RuntimeError):
            opt.read_bpm_intensities(interface, ["A"])
        self.assertEqual(interface.get_bpms.call_count, 3)
        self.assertEqual(interface.nsamples, 1)
        noisy = data()
        noisy["intensity"][:, 0] = [0., 2., 4.]
        interface.get_bpms = Mock(return_value=noisy)
        with self.assertRaises(RuntimeError):
            opt.read_bpm_intensities(interface, ["A"], baseline=True)

    def test_worker_maximizes_and_guards_writes(self):
        fake = Mock(nsamples=1)
        fake.get_bpms.return_value = data()
        config = {"run_profile": opt.RUN_PROFILE_DEVELOPER,
                  "objective_type": opt.DEVELOPER_OBJECTIVE_INTENSITY,
                  "objective_bpm_names": ["MB1R", "MB2R"],
                  "developer_actuators": [{"pv_write": "Z:currentWrite"}],
                  "actuator_limits": {"Z:currentWrite": [-.5, .5]}}
        dr = Mock(nsamples=3)
        dr.get_bpms.return_value = data()
        dr.get_bpms.return_value["names"] = ["MB1R", "MB2R"]
        with tempfile.TemporaryDirectory() as directory, patch.object(opt, "InterfaceATF2_LinacBT", return_value=fake), patch.object(opt, "DRBPMReader", return_value=dr):
            worker = opt.OptimizationWorker(config, directory)
            worker._read_icts = Mock(side_effect=AssertionError("Intensity must not require ICT"))
            self.assertEqual(worker._measure_objective()[4], 11.)
            fake.get_bpms.assert_not_called()
            dr.get_bpms.assert_called_once_with()
            worker.intensity_aggregation = "raw_sum"
            measurement = worker._measure_objective()
            self.assertEqual(measurement[4], 22.)
            self.assertEqual(measurement[3], "BPM intensity raw_sum")
            worker._read_icts.assert_not_called()
            worker._write_setpoints({"Z:currentWrite": .2})
            fake.pv_put.assert_called_once_with("Z:currentWrite", .2)
            for values in [{"other": 0.}, {"Z:currentWrite": 1.}, {"Z:currentWrite": np.nan}]:
                with self.assertRaises(ValueError):
                    worker._write_setpoints(values)
            self.assertEqual(fake.pv_put.call_count, 1)
            dr.get_bpms.return_value = data(0)
            dr.get_bpms.return_value["names"] = ["MB1R", "MB2R"]
            dr.get_bpms.reset_mock()
            self.assertEqual(worker._measure_objective()[4], 22.)
            dr.get_bpms.assert_called_once_with()
            self.assertEqual(worker._last_bpm_snapshot["MB1R"], 2.)
            dr.get_bpms.return_value["intensity"][0, 0] = np.nan
            with self.assertRaises(RuntimeError):
                worker._measure_objective()

    def test_unchecked_status_still_requires_complete_finite_data(self):
        payload = data(0)
        payload.pop("status")
        self.assertEqual(opt.bpm_intensity_values(payload, ["A", "B"], 3, check_status=False),
                         {"A": 2., "B": 20.})
        with self.assertRaises(ValueError):
            opt.bpm_intensity_values(payload, ["missing"], 3, check_status=False)
        for value in (np.nan, np.inf, -np.inf):
            payload["intensity"][0, 0] = value
            with self.assertRaises(ValueError):
                opt.bpm_intensity_values(payload, ["A", "B"], 3, check_status=False)
        payload["intensity"] = np.zeros((2, 2))
        with self.assertRaises(ValueError):
            opt.bpm_intensity_values(payload, ["A", "B"], 3, check_status=False)

    def test_dr_reader_uses_numbered_slots_and_only_reads_dr(self):
        module = "machine_interfaces_atf2.InterfaceATF2_DR"
        raw = np.zeros((98, 10))
        raw[0, :4] = [1, 1, 2, 10]
        raw[20, :4] = [0, 3, 4, 20]
        with patch(module + ".PV") as pv, patch(module + ".time.sleep"):
            reader = opt.DRBPMReader(nsamples=1)
            pv.assert_not_called()
            pv.return_value.get.return_value = raw.reshape(-1)
            result = reader.get_bpms(["MB21R", "MB1R"])
            pv.assert_called_once_with("DR:monitors")
            pv.return_value.put.assert_not_called()
        np.testing.assert_equal(result["status"], [[0, 1]])
        np.testing.assert_equal(result["intensity"], [[20, 10]])
        self.assertTrue(np.isnan(result["tmit"][0, 0]))
        with self.assertRaises(ValueError):
            reader.get_bpms(["MB1L"])

    def test_main_runs_rf_groups_and_then_selected_correctors_with_dr_objective(self):
        for mode in ("SEQUENTIAL", "GROUP_BO"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory, patch.object(opt, "InterfaceATF2_LinacBT") as control, patch.object(opt, "DRBPMReader") as reader:
                reader.return_value.nsamples = 3
                reader.return_value.get_bpms.return_value = data()
                reader.return_value.get_bpms.return_value["names"] = ["MB1R", "MB2R"]
                cfg = {"run_profile": opt.RUN_PROFILE_MAIN, "mode": mode,
                       "objective_type": opt.DEVELOPER_OBJECTIVE_INTENSITY,
                       "objective_bpm_names": ["MB1R", "MB2R"], "gun_sol_l0_phase": True,
                       "kly_phase": True, "correctors": True,
                       "developer_actuators": [{"name": "ZH1L", "pv_write": "ZH1L:currentWrite", "half_range": .5, "step": .1}],
                       "actuator_limits": {"ZH1L:currentWrite": [-.5, .5]}}
                worker = opt.OptimizationWorker(cfg, directory)
                worker._dump_initial_snapshot = Mock()
                worker._read_current = Mock(return_value=0.)
                seen = []
                def scan(label, pv, *args, **kwargs):
                    seen.append(pv)
                    self.assertEqual(worker._measure_objective()[4], 11.)
                def group(label, params):
                    seen.extend(p[1] for p in params)
                    self.assertEqual(worker._measure_objective()[4], 11.)
                worker.perform_1d_scan = scan
                worker._group_bo_simultaneous = group
                errors = []
                worker.progress_signal.connect(lambda payload: errors.append(payload) if payload.get("kind") == "run_error" else None)
                worker.run()
                self.assertEqual(errors, [])
                for i in range(9):
                    self.assertIn(f"CM{i}L:phaseWrite", seen)
                self.assertEqual(seen[-1], "ZH1L:currentWrite")
                control.return_value.get_bpms.assert_not_called()
                worker._write_setpoints({"CM0L:phaseWrite": 12.})
                control.return_value.pv_put.assert_called_with("CM0L:phaseWrite", 12.)
                with self.assertRaises(ValueError):
                    worker._write_setpoints({"ZH2L:currentWrite": 0.})

    def test_dr_reader_half_second_samples(self):
        module = "machine_interfaces_atf2.InterfaceATF2_DR"
        raw = np.zeros((98, 10))
        raw[0, :4] = [1, 0, 0, 10]
        with patch(module + ".PV") as pv, patch(module + ".time.sleep") as sleep:
            pv.return_value.get.return_value = raw.reshape(-1)
            reader = opt.DRBPMReader(nsamples=3)
            reader.log = lambda message: None
            result = reader.get_bpms(["MB1R"])
            self.assertEqual(result["intensity"].shape, (3, 1))
            self.assertEqual(pv.return_value.get.call_count, 3)
            self.assertEqual([c.args for c in sleep.call_args_list], [(0.5,)] * 3)

    def test_linac_array_retains_status_and_raw_intensity(self):
        from machine_interfaces_atf2.InterfaceATF2_Linac import InterfaceATF2_Linac
        interface = InterfaceATF2_Linac.__new__(InterfaceATF2_Linac)
        interface.nsamples, interface.bpms, interface.bpm_indexes = 1, ["A", "B"], [0, 1]
        raw = np.zeros((2, 20))
        raw[:, 0], raw[:, 3] = [1, 0], [10, 20]
        module = "machine_interfaces_atf2.InterfaceATF2_Linac"
        with patch(module + ".PV") as pv, patch(module + ".time.sleep"):
            pv.return_value.get.return_value = raw.reshape(-1)
            result = interface.get_bpms(["B"])
        np.testing.assert_equal(result["status"], [[0]])
        np.testing.assert_equal(result["intensity"], [[20]])
        np.testing.assert_equal(result["tmit"], [[0]])

    def test_gui_switching_and_confirmation_cancel(self):
        app = QApplication.instance() or QApplication([])
        with patch.object(opt.MainWindow, "_refresh_current_values"), patch.object(opt.MainWindow, "_refresh_developer_live_values"):
            window = opt.MainWindow()
            window._current_value_timer.stop()
            try:
                self.assertEqual(window.objective_box.currentText(), "ICT")
                self.assertFalse(window.dev_objective_stack.isHidden())
                self.assertEqual(set(window.dev_bpm_row_map), set(opt.INTENSITY_BPM_NAMES))
                window.dev_bpm_table.item(0, 0).setCheckState(opt.Qt.CheckState.Checked)
                window.dev_objective_box.setCurrentText("BPM intensity")
                self.assertEqual(window.dev_intensity_aggregation.currentData(), "raw_mean")
                window.dev_intensity_aggregation.setCurrentText("Raw sum")
                self.assertEqual(window.dev_intensity_aggregation.currentData(), "raw_sum")
                self.assertEqual(window._selected_developer_bpms(), ["MB1R"])
                self.assertEqual(window.dev_refresh_btn.maximumWidth(), 240)
                self.assertEqual([window.tabs.tabText(i) for i in range(window.tabs.count())], ["Main", "Config"])
                self.assertIs(window.objective_box, window.dev_objective_box)
                self.assertTrue(window.selection_page.isAncestorOf(window.dev_actuator_table))
                window.chk_correctors.setChecked(True)
                self.assertEqual(set(window.dev_bpm_row_map), set(opt.INTENSITY_BPM_NAMES))
                self.assertTrue(all(not name.endswith("R") for name in window.dev_actuator_row_map))
                self.assertEqual(window.dev_bpm_table.horizontalHeaderItem(3).text(), "Intensity")
                self.assertTrue(window.dev_bpm_plane_box.isHidden())
                window.dev_actuator_table.item(0, 0).setCheckState(opt.Qt.CheckState.Checked)
                window.dev_bpm_table.item(0, 0).setCheckState(opt.Qt.CheckState.Checked)
                window.dev_bpm_table.item(1, 0).setCheckState(opt.Qt.CheckState.Checked)
                self.assertEqual(len(window._selected_developer_bpms()), 2)
                fake = Mock()
                fake.read_current.return_value = 0.
                with patch.object(window, "_ensure_interface", return_value=fake), patch("epics.PV") as pv, patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No), patch.object(window, "_capture_machine_origin") as capture:
                    pv.return_value.get_ctrlvars.return_value = {"lower_ctrl_limit": -1., "upper_ctrl_limit": 1.}
                    window.start_optimization()
                    capture.assert_not_called()
                    fake.pv_put.assert_not_called()
                with patch.object(window, "_ensure_interface", return_value=fake), patch("epics.PV") as pv, patch.object(QMessageBox, "critical") as error, patch.object(QMessageBox, "question") as confirm:
                    pv.return_value.get_ctrlvars.return_value = {}
                    window.start_optimization()
                    error.assert_called_once()
                    confirm.assert_not_called()
                with patch.object(window, "_ensure_interface", return_value=fake), patch("epics.PV") as pv, patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), patch.object(window, "_capture_machine_origin", return_value={}), patch.object(opt, "OptimizationWorker") as worker:
                    pv.return_value.get_ctrlvars.return_value = {"lower_ctrl_limit": -.2, "upper_ctrl_limit": .3}
                    worker.return_value.csv_path = "/tmp/offline-test.csv"
                    window.start_optimization()
                    cfg = worker.call_args.args[0]
                    self.assertEqual(cfg["objective_type"], opt.DEVELOPER_OBJECTIVE_INTENSITY)
                    self.assertEqual(cfg["intensity_source"], "DR:monitors")
                    self.assertEqual(cfg["intensity_aggregation"], "raw_sum")
                    self.assertEqual(cfg["objective_bpm_names"], ["MB1R", "MB2R"])
                    self.assertTrue(cfg["correctors"])
                    self.assertTrue(cfg["gbo_correctors_auto"])
                    self.assertEqual(cfg["gbo_max_evals_correctors"], 30)
                    self.assertEqual(len(cfg["developer_actuators"]), 1)
                    self.assertTrue(cfg["gun_sol_l0_phase"])
                    self.assertTrue(cfg["kly_phase"])
                    self.assertEqual(cfg["run_profile"], opt.RUN_PROFILE_MAIN)
                    self.assertEqual(list(cfg["actuator_limits"].values()), [[-.2, .3]])
                    worker.return_value.start.assert_called_once()
                    window.worker = None
                window._run_profile = opt.RUN_PROFILE_MAIN
                window._on_worker_progress({"kind": "evaluation", "eval_index": 1,
                                            "bpm_intensities": {"A": 2., "B": 20.},
                                            "bpm_snapshot": {"MB1R": 2., "MB2R": 20., "MB3R": 5.},
                                            "selected_bpms": ["MB1R", "MB2R"],
                                            "downstream_value": 11., "downstream_label": "BPM intensity raw_mean"})
                lower = window._plot_widgets[opt.RUN_PROFILE_MAIN]["ax_metric"]
                upper = window._plot_widgets[opt.RUN_PROFILE_MAIN]["ax_ttot"]
                self.assertEqual(len(lower.patches), len(opt.INTENSITY_BPM_NAMES))
                self.assertEqual([t.get_text() for t in lower.get_xticklabels()], list(opt.INTENSITY_PLOT_BPM_NAMES))
                bars = dict(zip(opt.INTENSITY_PLOT_BPM_NAMES, lower.patches))
                self.assertEqual(bars["MB3R"].get_height(), 5.)
                self.assertEqual(bars["MB1R"].get_height(), 2.)
                self.assertNotEqual(bars["MB1R"].get_facecolor(), bars["MB3R"].get_facecolor())
                self.assertFalse(lower.get_shared_x_axes().joined(lower, upper))
                self.assertEqual(len(upper.lines), 1)
                np.testing.assert_equal(upper.lines[0].get_ydata(), [11.])
                for aggregation, score, label in (("raw_sum", 22., "Raw sum"),
                                                   ("raw_mean", 11., "Raw mean"),
                                                   ("relative_mean", 1.5, "Mean / initial baseline")):
                    window._reset_eval_plot("BPM intensity " + aggregation)
                    for evaluation in (1, 2):
                        window._on_worker_progress({"kind": "evaluation", "eval_index": evaluation,
                                                    "ttot": 999.,
                                                    "bpm_intensities": {"MB1R": 2., "MB2R": 20.},
                                                    "downstream_value": score * evaluation,
                                                    "downstream_label": "BPM intensity " + aggregation})
                    self.assertEqual(len(upper.lines), 1)
                    np.testing.assert_equal(upper.lines[0].get_xdata(), [1, 2])
                    np.testing.assert_equal(upper.lines[0].get_ydata(), [score, score * 2])
                    self.assertEqual(upper.get_ylabel(), label)
                    self.assertIn(label, upper.get_title())
                window.dev_objective_box.setCurrentText("BPM sumsq")
                self.assertEqual(window.dev_bpm_table.horizontalHeaderItem(3).text(), "Live X [mm]")
                self.assertNotIn("MB1R", window.dev_bpm_row_map)
                self.assertFalse(window.dev_bpm_plane_box.isHidden())
            finally:
                window.close()
                app.processEvents()

    def test_developer_refresh_is_manual(self):
        app = QApplication.instance() or QApplication([])
        with patch.object(opt.MainWindow, "_refresh_current_values") as main_read, patch.object(opt.MainWindow, "_refresh_developer_live_values") as dev_read:
            window = opt.MainWindow()
            window._current_value_timer.stop()
            try:
                main_read.reset_mock()
                window.tabs.setCurrentWidget(window.config_tab)
                window._auto_refresh_current_values()
                main_read.assert_not_called()
                dev_read.assert_not_called()
                window.dev_refresh_btn.click()
                dev_read.assert_called_once()
                window.worker = Mock()
                window.worker.isRunning.return_value = True
                window.dev_refresh_btn.click()
                self.assertEqual(dev_read.call_count, 1)
                window.worker = None
                window.tabs.setCurrentWidget(window.main_tab)
                window._auto_refresh_current_values()
                main_read.assert_called_once()
            finally:
                window.worker = None
                window.close()
                app.processEvents()


if __name__ == "__main__":
    unittest.main()
