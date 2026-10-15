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
    def test_l8_phase_uses_search_range_without_ioc_limits(self):
        interface = Mock()
        interface.read_current.return_value = 350.
        window = SimpleNamespace(_ensure_interface=lambda: interface)
        spec = {"name": "L8 phase", "pv_write": "CM8L:phaseWrite", "pv_read": "CM8L:phaseRead",
                "unit": "deg", "step": 1., "half_range": 20.}
        with patch("epics.PV") as pv:
            limits, _ = opt.MainWindow._checked_corrector_ranges(window, [spec])
            self.assertEqual(limits, {"CM8L:phaseWrite": [330., 370.]})
            pv.assert_not_called()
            for value in (float("nan"), float("inf")):
                interface.read_current.return_value = value
                with self.assertRaises(ValueError):
                    opt.MainWindow._checked_corrector_ranges(window, [spec])
            interface.read_current.return_value = 350.
            with self.assertRaises(ValueError):
                opt.MainWindow._checked_corrector_ranges(window, [{**spec, "half_range": float("inf")}])
            pv.return_value.get_ctrlvars.return_value = {}
            for name, unit in (("ZH1L:currentWrite", "A"),):
                with self.assertRaisesRegex(ValueError, "Cannot verify EPICS limits"):
                    opt.MainWindow._checked_corrector_ranges(window, [{**spec, "pv_write": name, "unit": unit}])

    def test_timing_uses_finite_configured_range_without_ioc_limits(self):
        interface = Mock()
        interface.read_current.return_value = 1000.
        window = SimpleNamespace(_ensure_interface=lambda: interface)
        spec = {"name": "Timing", "pv_write": "EVE_LINAC:OUT0:SetData", "pv_read": None,
                "unit": "ns", "step": 11.2, "half_range": 112.}
        with patch("epics.PV") as pv:
            limits, _ = opt.MainWindow._checked_corrector_ranges(window, [spec])
            self.assertEqual(limits, {spec["pv_write"]: [888., 1112.]})
            for value in (float("nan"), float("inf")):
                interface.read_current.return_value = value
                with self.assertRaises(ValueError):
                    opt.MainWindow._checked_corrector_ranges(window, [spec])
            interface.read_current.return_value = 1000.
            for changes in ({"step": 0.}, {"half_range": float("inf")}, {"half_range": 0.}):
                with self.assertRaises(ValueError):
                    opt.MainWindow._checked_corrector_ranges(window, [{**spec, **changes}])
            pv.assert_not_called()
        config = {"correctors": True, "developer_actuators": [spec], "actuator_limits": limits}
        with tempfile.TemporaryDirectory() as directory, patch.object(opt, "InterfaceATF2_LinacBT") as control:
            worker = opt.OptimizationWorker(config, directory)
            with self.assertRaises(ValueError):
                worker._write_setpoints({spec["pv_write"]: 1113.})
            control.return_value.pv_put.assert_not_called()

    def test_initial_bo_background_phases_and_restarts(self):
        from matplotlib.figure import Figure
        from matplotlib.colors import to_rgba
        notes = ["GBO_INIT_CUR|REUSE_INIT", "GBO_INIT_RAND", "GBO_EI", "OPTIMIZED_SET",
                 "BO_INIT", "BO_EI", "BO_REFINE", "TERNARY_EVAL"]
        phases = [opt.evaluation_phase(note) for note in notes]
        self.assertEqual(phases, ["Initial", "Initial", "BO", "", "Initial", "BO", "BO", ""])
        axis = Figure().add_subplot()
        opt.shade_evaluation_phases(axis, list(range(1, 9)), phases)
        self.assertEqual([text.get_text() for text in axis.texts], ["Initial", "BO", "Initial", "BO"])
        self.assertEqual(len(axis.patches), 4)
        self.assertEqual(axis.patches[0].get_facecolor(), to_rgba("#d8ecff", .35))
        self.assertEqual(axis.patches[1].get_facecolor(), to_rgba("#fff0cc", .35))
        for patch, (left, right) in zip(axis.patches, ((.5, 2.5), (2.5, 3.5), (4.5, 5.5), (5.5, 7.5))):
            self.assertEqual(patch.get_x(), left)
            self.assertEqual(patch.get_width(), right - left)

    def test_transmission_and_fixed_initial_mean_current(self):
        first, last = data(), data()
        first["names"] = last["names"] = ["MB1R", "MB2R"]
        first["intensity"] = np.tile([10., 20.], (3, 1))
        last["intensity"] = np.tile([8., 10.], (3, 1))
        reader = Mock(nsamples=1)
        reader.get_bpms.return_value = {"turn_data": {1: first, 10: last}}
        config = {"dr_ratio_turn": 10, "intensity_aggregation": "turn_composite", "dr_current_weight": 2.}
        baseline = {"MB1R": 5., "MB2R": 10.}
        plot = {}
        score, initial, final, snapshot, transmission, normalized = opt.read_turn_objective(
            reader, first["names"], config, baseline, plot_out=plot)
        self.assertEqual(plot["ratios"], {"MB1R": .8, "MB2R": .5})
        self.assertEqual(plot["first"], {"MB1R": 10., "MB2R": 20.})
        self.assertEqual(plot["last"], {"MB1R": 8., "MB2R": 10.})
        reader.get_bpms.assert_called_once()
        self.assertAlmostEqual(transmission, .65)
        self.assertEqual(normalized, 2.)
        self.assertAlmostEqual(score, 4.65)
        self.assertEqual(reader.nsamples, 1)
        config["intensity_aggregation"] = "turn_ratio"
        self.assertAlmostEqual(opt.read_turn_objective(reader, first["names"], config)[0], .65)
        with self.assertRaises(ValueError):
            opt.read_turn_objective(reader, ["MB5L"], config)
        first["intensity"][:, 0] = 0
        first["status"][:, 0] = 0
        last["status"][:, 0] = 0
        result = opt.read_turn_objective(reader, first["names"], config, plot_out=plot)
        self.assertEqual(result[0], .25)  # lost BPM remains in the mean, contributing zero
        self.assertEqual(plot["ratios"]["MB1R"], 0.)
        config["intensity_aggregation"] = "turn_composite"
        result = opt.read_turn_objective(reader, first["names"], config, baseline, check_status=False)
        self.assertAlmostEqual(result[0], .25 + 2 * (10 / 7.5))
        first["intensity"][:] = 0
        self.assertEqual(opt.read_turn_objective(reader, first["names"], config, baseline)[0], 0.)
        with self.assertRaisesRegex(ValueError, "Initial meanCurrent"):
            opt.read_turn_objective(reader, first["names"], config)
        config["intensity_aggregation"] = "turn_ratio"
        self.assertEqual(opt.read_turn_objective(reader, first["names"], config)[0], 0.)
        first["intensity"][:, 0] = np.nan
        with self.assertRaises(ValueError):
            opt.read_turn_objective(reader, first["names"], config, check_status=False)
        first["intensity"][:, 1] = 20.
        first["intensity"][:, 0] = [1, 10, 20]
        with self.assertRaises(ValueError):
            opt.read_turn_objective(reader, first["names"], config, check_status=False)

    def test_transmission_reader_uses_only_endpoints(self):
        module = "machine_interfaces_atf2.InterfaceATF2_DR"
        reader = opt.DRBPMReader(nsamples=3)
        reader.log = lambda message: None
        opt.configure_dr_turns(reader, {"intensity_aggregation": "turn_ratio", "dr_ratio_turn": 10})
        current = [5]
        selector = Mock()
        selector.get.return_value = 5
        selector.get.side_effect = lambda **kwargs: current[0]
        selector.get_ctrlvars.return_value = {}
        def put(value, **kwargs):
            current[0] = value
            return 1
        selector.put.side_effect = put
        def sample(_reader, names):
            return {"names": names, **{key: np.ones((3, len(names))) for key in ("intensity", "status", "x", "y", "tmit")}}
        with patch(module + ".PV", return_value=selector), patch(module + ".time.sleep"), patch(module + ".InterfaceATF2_DR.get_bpms", autospec=True, side_effect=sample) as read:
            result = reader.get_bpms(["MB1R"])
        self.assertEqual(result["turns"], [1, 10])
        self.assertEqual(set(result["turn_data"]), {1, 10})
        self.assertEqual(read.call_count, 2)
        self.assertEqual([call.args[0] for call in selector.put.call_args_list], [1, 10, 5])

    def test_turn_live_plots_components_bars_and_no_pv_reads(self):
        app = QApplication.instance() or QApplication([])
        with patch.object(opt.MainWindow, "_refresh_current_values"):
            window = opt.MainWindow()
            window._current_value_timer.stop()
            try:
                window._run_profile = opt.RUN_PROFILE_MAIN
                window._reset_eval_plot("BPM intensity turn_composite")
                plot = {"first": {"MB1R": 10., "MB2R": 20.},
                        "last": {"MB1R": 8., "MB2R": 10.},
                        "ratios": {"MB1R": .8, "MB2R": .5},
                        "target": 10, "weight": 2., "transmission": .65, "normalized_current": 1.}
                with patch.object(window, "_ensure_interface") as machine, patch.object(window.dr_bpm_reader, "get_bpms") as read:
                    for i, (score, valid) in enumerate(((2.65, True), (2.5, True), (99., False), (2.8, True)), 1):
                        window._on_worker_progress({"kind": "evaluation", "eval_index": i,
                            "evaluation_phase": "Initial" if i <= 2 else "BO",
                            "downstream_value": score, "downstream_label": "BPM intensity turn_composite",
                            "bpm_intensities": plot["last"], "selected_bpms": ["MB1R"],
                            "turn_plot": plot, "measurement_ok": valid})
                    upper, lower = window.ax_ttot, window.ax_downstream
                    self.assertEqual(len(upper.patches), 2)
                    self.assertEqual([text.get_text() for text in upper.texts], ["Initial", "BO"])
                    lines = {line.get_label(): line for line in upper.lines}
                    np.testing.assert_allclose(lines["Best score so far"].get_ydata(), [2.65, 2.65, 2.65, 2.8])
                    np.testing.assert_allclose(lines["Mean transmission"].get_ydata(), [.65, .65, np.nan, .65])
                    self.assertIn("Normalized turn 1 current (unweighted; w=2)", lines)
                    names = list(opt.INTENSITY_PLOT_BPM_NAMES)
                    self.assertAlmostEqual(lower.patches[names.index("MB1R")].get_height(), .8)
                    self.assertTrue(np.isnan(lower.patches[names.index("MB5L")].get_height()))
                    self.assertTrue(np.isnan(lower.patches[names.index("MB3R")].get_height()))
                    window.turn_plot_view.setCurrentIndex(1)
                    self.assertEqual(lower.patches[names.index("MB1R")].get_height(), 10.)
                    self.assertEqual(lower.patches[len(names) + names.index("MB1R")].get_height(), 8.)
                    machine.assert_not_called()
                    read.assert_not_called()
                window._reset_eval_plot("BPM intensity turn_ratio")
                window._on_worker_progress({"kind": "evaluation", "downstream_value": .65,
                    "downstream_label": "BPM intensity turn_ratio", "turn_plot": plot})
                labels = [line.get_label() for line in window.ax_ttot.lines]
                self.assertNotIn("Mean transmission", labels)
                self.assertIn("Score (= mean transmission)", labels)
                window._reset_eval_plot("BPM intensity raw_mean")
                self.assertTrue(window.turn_plot_view.isHidden())
                self.assertEqual(window._plot_state[opt.RUN_PROFILE_MAIN]["turn_plot"], {})
            finally:
                window.close()

    def test_combined_gui_builds_config_before_machine_access(self):
        app = QApplication.instance() or QApplication([])
        with patch.object(opt.MainWindow, "_refresh_current_values"):
            window = opt.MainWindow()
            window._current_value_timer.stop()
            try:
                window.correctors_l8.setChecked(True)
                self.assertEqual(window.chk_correctors.text(), "Correctors & L8")
                self.assertEqual(window.mode_box.currentText(), "GROUP_BO")
                window.dev_actuator_table.item(0, 0).setCheckState(opt.Qt.CheckState.Checked)
                window.objective_box.setCurrentText("BPM intensity")
                window.dev_bpm_table.item(window.dev_bpm_row_map["MB1R"], 0).setCheckState(opt.Qt.CheckState.Checked)
                window.dev_intensity_aggregation.setCurrentIndex(window.dev_intensity_aggregation.findData("turn_composite"))
                self.assertEqual(window.group_bo_max_eval_spinboxes["correctors"].value(), 50)  # D = 1 + 2
                with patch.object(window, "_checked_corrector_ranges", return_value=({}, [])) as checked, patch.object(
                        QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), patch.object(
                        window, "_capture_machine_origin", side_effect=ValueError("offline stop")) as capture, patch.object(QMessageBox, "critical"):
                    window.start_optimization()
                config = capture.call_args.args[0]
                self.assertTrue(config["correctors_l8"])
                self.assertFalse(config["timing"])
                self.assertEqual(config["dr_current_weight"], 1.)
                self.assertEqual(config["intensity_aggregation"], "turn_composite")
                self.assertEqual([a["pv_write"] for a in checked.call_args.args[0]][-2:],
                                 ["CM8L:phaseWrite", "EVE_LINAC:OUT0:SetData"])
                # Reject a mixed-region ratio objective before acquiring any data.
                window.dev_bpm_table.item(window.dev_bpm_row_map["MB5L"], 0).setCheckState(opt.Qt.CheckState.Checked)
                with patch.object(QMessageBox, "warning") as warning, patch.object(window, "_checked_corrector_ranges") as checked:
                    window.start_optimization()
                checked.assert_not_called()
                warning.assert_called_once()
            finally:
                window.close()

    def test_combined_group_has_correctors_phase_and_timing_in_one_call(self):
        actuators = [{"name": "ZH1L", "pv_write": "ZH1L:currentWrite", "step": .01, "half_range": .1},
                     {"name": "L8 phase", "pv_write": "CM8L:phaseWrite", "step": 1., "half_range": 5., "unit": "deg"},
                     {"name": "Timing", "pv_write": "EVE_LINAC:OUT0:SetData", "step": 11.2, "half_range": 112., "unit": "ns"}]
        config = {"mode": "GROUP_BO", "correctors": True, "correctors_l8": True,
                  "developer_actuators": actuators, "actuator_limits": {s["pv_write"]: [-200., 200.] for s in actuators}}
        with tempfile.TemporaryDirectory() as directory, patch.object(opt, "InterfaceATF2_LinacBT"):
            worker = opt.OptimizationWorker(config, directory)
            with patch.object(worker, "_read_current", return_value=0.), patch.object(worker, "_group_bo_simultaneous") as run:
                worker._run_correctors()
                self.assertEqual(run.call_count, 1)
                self.assertEqual([p[1] for p in run.call_args.args[1]], [s["pv_write"] for s in actuators])
            with self.assertRaises(ValueError):
                worker._write_setpoints({"CM8L:phaseWrite": 201.})
            worker.config.update(kly_phase=True, grp_L1_4=False, grp_L5_8=True)
            with patch.object(worker, "_group_bo_simultaneous") as run:
                worker._run_group_bo()
                self.assertNotIn("CM8L:phaseWrite", [p[1] for p in run.call_args.args[1]])

    def test_dr_turn_scan_mean_sum_settle_and_restore(self):
        module = "machine_interfaces_atf2.InterfaceATF2_DR"
        for aggregation, expected, bad_status in (("mean", 20., False), ("sum", 60., False), ("sum", 60., True)):
            with self.subTest(aggregation=aggregation, bad_status=bad_status):
                events, current = [], [7]
                selector, waveform = Mock(), Mock()
                selector.wait_for_connection.return_value = True
                selector.get_ctrlvars.return_value = {"lower_ctrl_limit": 1, "upper_ctrl_limit": 100}
                selector.get.side_effect = lambda **kwargs: current[0]
                def put(value, **kwargs):
                    current[0] = value
                    events.append(("put", value))
                    return 1
                def read(**kwargs):
                    self.assertFalse(kwargs["use_monitor"])
                    events.append(("read", current[0]))
                    a = np.zeros((98, 10))
                    a[:, 0], a[:, 3] = 1, current[0] * 10
                    if bad_status and current[0] == 2:
                        a[0, 0] = 0
                    return a.ravel()
                selector.put.side_effect = put
                waveform.get.side_effect = read
                reader = opt.DRBPMReader(nsamples=3)
                reader.log = lambda message: None
                reader.configure_turn_scan(True, 1, 3, aggregation)
                with patch(module + ".PV", side_effect=lambda name: selector if name == "DRBPM:INJ:TURN_COUNT" else waveform), patch(module + ".time.sleep", side_effect=lambda seconds: events.append(("sleep", seconds))):
                    values = reader.get_bpms(["MB1R", "MB21R"])
                np.testing.assert_equal(values["intensity"], np.full((3, 2), expected))
                if bad_status:
                    with self.assertRaises(ValueError):
                        opt.bpm_intensity_values(values, ["MB1R"])
                    self.assertEqual(opt.bpm_intensity_values(values, ["MB1R"], check_status=False), {"MB1R": expected})
                self.assertEqual(current[0], 7)
                self.assertEqual(values["turns"], [1, 2, 3])
                self.assertEqual(waveform.get.call_count, 9)
                for turn in (1, 2, 3):
                    index = events.index(("put", turn))
                    self.assertEqual(events[index + 1:index + 3], [("sleep", .5), ("read", turn)])

    def test_dr_turn_failure_cancel_and_validation(self):
        module = "machine_interfaces_atf2.InterfaceATF2_DR"
        reader = opt.DRBPMReader(nsamples=3)
        reader.log = lambda message: None
        for args in ((True, 0, 3, "mean"), (True, 3, 1, "mean"), (True, 1, 3, "invalid")):
            with self.assertRaises(ValueError):
                reader.configure_turn_scan(*args)
        for failure in ("put", "cancel", "restore"):
            with self.subTest(failure=failure):
                current, stopped = [7], [False]
                selector = Mock()
                selector.wait_for_connection.return_value = True
                selector.get_ctrlvars.return_value = {}
                selector.get.side_effect = lambda **kwargs: current[0]
                def put(value, **kwargs):
                    if (failure == "put" and value == 1) or (failure == "restore" and value == 7):
                        return -1
                    current[0] = value
                    return 1
                selector.put.side_effect = put
                reader.configure_turn_scan(True, 1, 2, cancelled=lambda: stopped[0])
                def settle(seconds):
                    stopped[0] = True
                with patch(module + ".PV", return_value=selector), patch(module + ".time.sleep", side_effect=settle):
                    with self.assertRaises(InterruptedError if failure == "cancel" else opt.DRBPMTurnControlError):
                        opt.read_bpm_intensities(reader, ["MB1R"])
                # A selector error must not trigger the three measurement retries.
                self.assertEqual(selector.put.call_count, 2)
                if failure != "restore":
                    self.assertEqual(current[0], 7)

        reader.configure_turn_scan(True, 1, 10)
        selector = Mock()
        selector.get.return_value = 7
        selector.get_ctrlvars.return_value = {"lower_ctrl_limit": 1, "upper_ctrl_limit": 5}
        with patch(module + ".PV", return_value=selector):
            with self.assertRaises(opt.DRBPMTurnControlError):
                reader.get_bpms(["MB1R"])
        selector.put.assert_not_called()

    def test_dr_turn_config_ui_and_worker(self):
        app = QApplication.instance() or QApplication([])
        with patch.object(opt.MainWindow, "_refresh_current_values"):
            window = opt.MainWindow()
            window._current_value_timer.stop()
            try:
                self.assertFalse(window.dr_turn_enabled.isChecked())
                self.assertFalse(window.dr_turn_start.isEnabled())
                config = {"dr_turn_scan": True, "dr_turn_start": 2, "dr_turn_end": 10,
                          "dr_turn_aggregation": "sum"}
                window._apply_config_to_ui(config)
                self.assertEqual(window._dr_turn_config_from_ui(), config)
                self.assertTrue(window.dr_turn_start.isEnabled())
                config["objective_type"] = opt.DEVELOPER_OBJECTIVE_INTENSITY
                with tempfile.TemporaryDirectory() as directory, patch.object(opt, "InterfaceATF2_LinacBT"):
                    worker = opt.OptimizationWorker(config, directory)
                    self.assertTrue(worker.dr_bpm_reader.turn_scan_enabled)
                    self.assertEqual(worker.dr_bpm_reader.turn_aggregation, "sum")
                    self.assertEqual(worker.dr_bpm_reader.turn_start, 2)
                    worker.stop()
                    self.assertTrue(worker.dr_bpm_reader.cancelled())
                window._apply_config_to_ui({})
                self.assertFalse(window.dr_turn_enabled.isChecked())
            finally:
                window.close()

    def test_online_gp_likelihood_gradient_and_bounded_fit(self):
        model = opt.CorrectorGPModel([-1., -1.], [1., 1.], [.01, .01])
        x = np.random.default_rng(7).uniform(-1, 1, (24, 2))
        y = 2000 + 300 * np.sin(2 * x[:, 0]) + 70 * x[:, 1] ** 2
        xn = model.transform_inputs(x)
        yn = (y - y.mean()) / y.std()
        theta = model.theta.copy()
        value, gradient = model.negative_log_likelihood(theta, xn, yn)
        numeric = []
        for i in range(len(theta)):
            delta = np.zeros(len(theta))
            delta[i] = 1e-5
            numeric.append((model.negative_log_likelihood(theta + delta, xn, yn)[0]
                            - model.negative_log_likelihood(theta - delta, xn, yn)[0]) / 2e-5)
        np.testing.assert_allclose(gradient, numeric, rtol=1e-3, atol=1e-4)
        model.prepare(x, y)
        self.assertEqual(model.successful_fits, 1)
        self.assertLess(model.negative_log_likelihood(model.theta, xn, yn)[0], value)
        self.assertTrue(np.all(model.theta >= model.bounds[:, 0]))
        self.assertTrue(np.all(model.theta <= model.bounds[:, 1]))
        scaled = opt.CorrectorGPModel([-1., -1.], [1., 1.], [.01, .01])
        scaled.prepare(x, 100 * y + 50000)
        np.testing.assert_allclose(scaled.theta, model.theta, rtol=2e-4, atol=2e-4)

    def test_online_gp_failure_cadence_constant_and_cancel(self):
        model = opt.CorrectorGPModel([0.], [1.], [.01], refit_interval=5)
        x = np.linspace(0, 1, 20)[:, None]
        y = np.sin(x[:, 0] * 3)
        model.prepare(x[:10], y[:10])
        self.assertEqual(model.successful_fits, 1)
        theta, scale, center = model.theta.copy(), model.scale, model.center
        with patch("scipy.optimize.minimize", side_effect=RuntimeError("Synthetic fit failure")) as fit:
            model.prepare(x[:14], y[:14])
            fit.assert_not_called()
            model.prepare(x[:15], y[:15])
            self.assertEqual(fit.call_count, 1)
            model.prepare(x[:19], y[:19])
            self.assertEqual(fit.call_count, 1)
            model.prepare(x, y)
            self.assertEqual(fit.call_count, 2)
        np.testing.assert_equal(model.theta, theta)
        self.assertEqual((model.scale, model.center), (scale, center))
        constant = opt.CorrectorGPModel([0.], [1.], [.01])
        with patch("scipy.optimize.minimize") as fit:
            _, standardized = constant.prepare(x, np.full(20, 1e6))
            fit.assert_not_called()
        self.assertFalse(constant.scale_frozen)
        np.testing.assert_equal(standardized, np.zeros(20))
        for kwargs in ({"cancelled": lambda: True}, {"fit_timeout": 0.}):
            stopped = opt.CorrectorGPModel([0.], [1.], [.01], **kwargs)
            initial = stopped.theta.copy()
            stopped.prepare(x, y)
            np.testing.assert_equal(stopped.theta, initial)
            self.assertEqual(stopped.successful_fits, 0)

    def test_gp_diagonal_prediction_matches_full_covariance(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(opt, "InterfaceATF2_LinacBT"):
            worker = opt.OptimizationWorker({}, directory)
            x = np.array([[0.], [.3], [.8]])
            y = np.array([1., -1., 2.])
            c = np.linspace(0, 1, 9)[:, None]
            length, signal, noise = np.array([.4]), 1.3, .02
            k = worker._kernel_rbf_ard(x, x, length, signal) + (noise ** 2 + 1e-12) * np.eye(3)
            ks = worker._kernel_rbf_ard(x, c, length, signal)
            kss = worker._kernel_rbf_ard(c, c, length, signal)
            mu, std = worker._gp_posterior_nd(x, y, c, length, signal, noise)
            np.testing.assert_allclose(mu, ks.T @ np.linalg.solve(k, y))
            np.testing.assert_allclose(std ** 2, np.diag(kss - ks.T @ np.linalg.solve(k, ks)), atol=1e-10)

    def test_online_corrector_group_uses_scaled_model_but_retains_raw_scores(self):
        sequences = []
        for multiplier, offset in ((1., 0.), (1000., 50000.)):
            rng = np.random.default_rng(42)
            fitted_counts = []
            with tempfile.TemporaryDirectory() as directory, patch.object(opt, "InterfaceATF2_LinacBT"), patch.object(opt.np.random, "default_rng", return_value=rng), patch.object(opt.CorrectorGPModel, "_fit", side_effect=lambda x, y: fitted_counts.append(len(y))):
                worker = opt.OptimizationWorker({"corrector_gp_mode": "online_fit", "corrector_gp_refit_interval": 5,
                                                 "gbo_min_init": 5, "gbo_max_evals_correctors": 16}, directory)
                worker._read_current = Mock(return_value=0.)
                worker._try_reuse_initial_vector = Mock(return_value=None)
                scores, commands = [], []
                def evaluate(**kwargs):
                    value = kwargs["pv_to_value"]["ZH1L:currentWrite"]
                    score = multiplier * (10000 + 2 * value + .1 * value ** 2) + offset
                    commands.append(value)
                    scores.append(score)
                    return SimpleNamespace(score=score, ict={}, bpm_metric=score)
                worker._evaluate_point_multi = Mock(side_effect=evaluate)
                worker._objective_metric_text = Mock(return_value="raw sum")
                worker._gp_posterior_nd = Mock(side_effect=lambda x, y, c, **kwargs: (np.zeros(len(c)), np.ones(len(c))))
                worker._group_bo_simultaneous("Correctors", [("ZH1L", "ZH1L:currentWrite", 1., 10.)])
                self.assertEqual(len(commands), 17)  # 16 search evaluations + best-point measurement
                self.assertEqual(fitted_counts, [5, 10, 15])
                self.assertTrue(all(score > 9000 for score in scores))
                for call in worker._gp_posterior_nd.call_args_list:
                    self.assertLess(np.max(np.abs(call.args[1])), 10.)
                self.assertEqual(commands[-1], commands[int(np.argmax(scores[:-1]))])
                sequences.append(commands)
        self.assertEqual(sequences[0], sequences[1])

    def test_online_gp_stop_during_fit_does_not_write_final_point(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(opt, "InterfaceATF2_LinacBT"):
            worker = opt.OptimizationWorker({"corrector_gp_mode": "online_fit", "gbo_min_init": 5,
                                             "gbo_max_evals_correctors": 10}, directory)
            worker._read_current = Mock(return_value=0.)
            worker._try_reuse_initial_vector = Mock(return_value=None)
            worker._evaluate_point_multi = Mock(side_effect=lambda **kw: SimpleNamespace(
                score=kw["pv_to_value"]["Z:currentWrite"], ict={}, bpm_metric=1.))
            def stop(*args):
                worker.is_running = False
            with patch.object(opt.CorrectorGPModel, "_fit", side_effect=stop):
                worker._group_bo_simultaneous("Correctors", [("Z", "Z:currentWrite", 1., 10.)])
            self.assertEqual(worker._evaluate_point_multi.call_count, 5)
            self.assertTrue(all(call.kwargs["note"].startswith("GBO_INIT")
                                for call in worker._evaluate_point_multi.call_args_list))

    def test_group_budget_formula_and_overrides(self):
        for preset, max_slope, max_offset, init_slope, init_offset in (
                ("quick", 5, 10, 1, 4), ("standard", 10, 20, 2, 8), ("explore", 20, 40, 4, 12)):
            for dim in (1, 4, 8, 32):
                budget = opt._auto_group_bo_budget(dim, preset)
                self.assertEqual(budget["max_evals"], max_slope * dim + max_offset)
                self.assertEqual(budget["min_init"], init_slope * dim + init_offset)
                self.assertEqual(opt._resolve_group_bo_max_evals(
                    {"gbo_budget_preset": preset, "gbo_correctors_auto": True}, "Correctors", dim), budget["max_evals"])
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
                self.assertEqual(window.corrector_gp_mode_box.currentData(), "online_fit")
                self.assertEqual(window.corrector_gp_refit_spin.value(), 5)
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
                preset_box = window.group_bo_budget_preset_box
                preset_box.setCurrentIndex(preset_box.findData("quick"))
                self.assertEqual(spin.value(), 25)
                self.assertEqual(window.group_bo_max_eval_spinboxes["qa"].value(), 35)
                self.assertIn("5D + 10", window.group_bo_budget_help.text())
                self.assertIn("init ≤ 7", window.group_bo_budget_labels["correctors"].text())
                window.dev_actuator_table.item(3, 0).setCheckState(opt.Qt.CheckState.Checked)
                self.assertEqual(spin.value(), 30)
                window.corrector_budget_auto.setChecked(False)
                spin.setValue(77)
                preset_box.setCurrentIndex(preset_box.findData("explore"))
                self.assertTrue(window.corrector_budget_auto.isChecked())
                self.assertEqual(spin.value(), 120)
                self.assertIn("init ≤ 28", window.group_bo_budget_labels["correctors"].text())
                self.assertEqual(window.group_bo_max_eval_spinboxes["timing"].value(), 60)
                window._apply_config_to_ui({"mode": "GROUP_BO", "gbo_budget_preset": "quick",
                                            "gbo_max_evals_correctors": 77, "gbo_correctors_auto": False,
                                            "gbo_max_evals_qa": 69})
                self.assertEqual(preset_box.currentData(), "quick")
                self.assertEqual(spin.value(), 77)
                self.assertEqual(window.group_bo_max_eval_spinboxes["qa"].value(), 69)
                window._apply_config_to_ui({"mode": "GROUP_BO", "gbo_max_evals_qa": 29})
                self.assertEqual(preset_box.currentData(), "standard")
                self.assertEqual(window.group_bo_max_eval_spinboxes["qa"].value(), 29)
                self.assertEqual(spin.value(), 60)
                self.assertEqual(window.corrector_gp_mode_box.currentData(), "fixed")
                self.assertFalse(window.corrector_gp_refit_spin.isEnabled())
                window._apply_config_to_ui({"mode": "GROUP_BO", "corrector_gp_mode": "online_fit",
                                            "corrector_gp_refit_interval": 7})
                self.assertEqual(window.corrector_gp_mode_box.currentData(), "online_fit")
                self.assertEqual(window.corrector_gp_refit_spin.value(), 7)
                self.assertTrue(window.corrector_gp_refit_spin.isEnabled())
            finally:
                window.close()
                app.processEvents()

    def test_group_budget_limit_and_small_grid_exhaustion(self):
        for dimension, half_range, max_evals, expected_count, reason, preset in (
                (1, 1., 30, 3, "candidate_exhausted", "standard"),
                (2, 2., 14, 14, "max_evals_reached(14)", "standard"),
                (2, 4., None, 20, "max_evals_reached(20)", "quick"),
                (2, 4., None, 80, "max_evals_reached(80)", "explore")):
            with self.subTest(dimension=dimension), tempfile.TemporaryDirectory() as directory, patch.object(opt, "InterfaceATF2_LinacBT"):
                config = {"gbo_max_evals_correctors": max_evals, "gbo_budget_preset": preset,
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
                initial_calls = [call for call in calls if call.kwargs["note"].startswith("GBO_INIT")]
                self.assertEqual(len(initial_calls), min(opt._auto_group_bo_budget(dimension, preset)["min_init"], expected_count))
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
                    self.assertEqual(cfg["gbo_budget_preset"], "standard")
                    self.assertEqual(cfg["corrector_gp_mode"], "online_fit")
                    self.assertEqual(cfg["corrector_gp_refit_interval"], 5)
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
