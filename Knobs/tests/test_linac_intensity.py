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
                  "objective_bpm_names": ["A", "B"],
                  "developer_actuators": [{"pv_write": "Z:currentWrite"}],
                  "actuator_limits": {"Z:currentWrite": [-.5, .5]}}
        dr = Mock(nsamples=3)
        dr.get_bpms.return_value = data()
        with tempfile.TemporaryDirectory() as directory, patch.object(opt, "InterfaceATF2_LinacBT", return_value=fake), patch.object(opt, "DRBPMReader", return_value=dr):
            worker = opt.OptimizationWorker(config, directory)
            worker._read_icts = Mock(side_effect=AssertionError("Intensity must not require ICT"))
            self.assertEqual(worker._measure_objective()[4], 11.)
            fake.get_bpms.assert_not_called()
            dr.get_bpms.assert_called_once_with()
            worker._read_icts.assert_not_called()
            worker._write_setpoints({"Z:currentWrite": .2})
            fake.pv_put.assert_called_once_with("Z:currentWrite", .2)
            for values in [{"other": 0.}, {"Z:currentWrite": 1.}, {"Z:currentWrite": np.nan}]:
                with self.assertRaises(ValueError):
                    worker._write_setpoints(values)
            self.assertEqual(fake.pv_put.call_count, 1)
            dr.get_bpms.return_value = data(0)
            with self.assertRaises(RuntimeError):
                worker._measure_objective()

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
                cfg = {"run_profile": opt.RUN_PROFILE_MAIN, "mode": mode,
                       "objective_type": opt.DEVELOPER_OBJECTIVE_INTENSITY,
                       "objective_bpm_names": ["A", "B"], "gun_sol_l0_phase": True,
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
                self.assertEqual(set(window.dev_bpm_row_map), set(opt.DR_BPM_NAMES))
                window.dev_bpm_table.item(0, 0).setCheckState(opt.Qt.CheckState.Checked)
                window.dev_objective_box.setCurrentText("BPM intensity")
                self.assertEqual(window._selected_developer_bpms(), ["MB1R"])
                self.assertEqual(window.dev_refresh_btn.maximumWidth(), 240)
                self.assertEqual([window.tabs.tabText(i) for i in range(window.tabs.count())], ["Main", "Config"])
                self.assertIs(window.objective_box, window.dev_objective_box)
                self.assertTrue(window.selection_page.isAncestorOf(window.dev_actuator_table))
                window.chk_correctors.setChecked(True)
                self.assertEqual(set(window.dev_bpm_row_map), set(opt.DR_BPM_NAMES))
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
                    self.assertEqual(cfg["objective_bpm_names"], ["MB1R", "MB2R"])
                    self.assertTrue(cfg["correctors"])
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
                self.assertEqual(len(lower.patches), len(opt.DR_BPM_NAMES))
                self.assertEqual([t.get_text() for t in lower.get_xticklabels()], list(opt.DR_BPM_NAMES))
                self.assertEqual(lower.patches[2].get_height(), 5.)
                self.assertNotEqual(lower.patches[0].get_facecolor(), lower.patches[2].get_facecolor())
                self.assertFalse(lower.get_shared_x_axes().joined(lower, upper))
                self.assertEqual(len(window._plot_widgets[opt.RUN_PROFILE_MAIN]["ax_ttot"].lines), 2)
                window.dev_intensity_plot.addItem("A")
                window.dev_intensity_plot.setCurrentText("A")
                self.assertEqual(len(window._plot_widgets[opt.RUN_PROFILE_MAIN]["ax_ttot"].lines), 1)
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
