"""Local scan budgets and optional single-PNG export, without machine/NAS I/O."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication, QMessageBox
from Knobs import Linac_Opt as opt


class ScanProgressTests(unittest.TestCase):
    def make_worker(self, directory, **config):
        defaults = dict(settle_sec=0, reuse_initial_eval=False, bo_max_evals=4,
                        bo_min_init=2, bo_refine=False, bo_ei_tol=-1, bo_stall_iters=99,
                        gbo_max_evals=4, gbo_min_init=2,
                        gbo_uncertainty_abs_tol=0, gbo_uncertainty_rel_tol=0)
        defaults.update(config)
        interface = Mock()
        interface.read_current.return_value = 0.0
        worker = opt.OptimizationWorker(defaults, directory, interface=interface)
        worker._measure_objective = Mock(return_value=({"Ttot": 0.8, "DR": 0.5}, np.nan, 0.5, "DR", 0.5, True, ""))
        worker._read_icts = Mock(return_value=({"Ttot": 0.8, "DR": 0.5}, True, ""))
        events = []
        worker._emit_progress = events.append
        return worker, events

    def test_group_and_sequential_budget_excludes_final_check(self):
        for method in ("group", "BO", "TERNARY", "LBO"):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as directory:
                worker, events = self.make_worker(directory, seq_method=method, lbo_iters=1, lbo_line_samples=3)
                params = [("A", "CM1L:phaseWrite", 1, 3)]
                if method == "group":
                    worker._group_bo_simultaneous("Example", params)
                elif method == "LBO":
                    worker._lbo_group("Example", params)
                else:
                    worker.perform_1d_scan("A", "CM1L:phaseWrite", np.arange(-3., 4.))
                evaluations = [p for p in events if p["kind"] == "evaluation"]
                budget = 7 if method == "LBO" else 4
                self.assertGreater(len(evaluations), 1)
                for i, event in enumerate(evaluations[:-1], 1):
                    self.assertEqual(event["scan_progress"]["index"], i)
                    self.assertEqual(event["scan_progress"]["max_evals"], budget)
                    self.assertLessEqual(i, budget)
                last = evaluations[-1]["scan_progress"]
                self.assertEqual(last["stage"], "final check")
                self.assertEqual(last["index"], len(evaluations) - 1)

    def test_refinement_reports_extended_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, events = self.make_worker(directory, bo_max_evals=2, bo_refine=True, bo_refine_evals=2)
            worker.perform_1d_scan("QA1L", "QA1L:currentWrite", np.array([-1., 0., 1.]))
            evaluations = [p["scan_progress"] for p in events if p["kind"] == "evaluation"]
            self.assertEqual([p["max_evals"] for p in evaluations], [2, 2, 4, 4, 4])
            self.assertEqual(evaluations[-1]["stage"], "final check")

    def test_resume_initial_design_obeys_remaining_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, events = self.make_worker(directory, bo_max_evals=5, bo_min_init=5)
            worker._resume_seq_rows[("SEQUENTIAL", "", "CM1L:phaseWrite")] = [
                dict(set_value=x, score=0.5, ict={"Ttot": 0.8, "DR": 0.5}, note="BO_EI") for x in (-3., -2., 2., 3.)]
            worker.perform_1d_scan("A", "CM1L:phaseWrite", np.arange(-3., 4.))
            evaluations = [p for p in events if p["kind"] == "evaluation"]
            self.assertEqual(len(evaluations), 2)  # one new point + final check
            self.assertEqual([p["scan_progress"]["index"] for p in evaluations], [5, 5])
            self.assertEqual([p["scan_progress"]["max_evals"] for p in evaluations], [5, 5])


class CompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        with patch.object(opt.MainWindow, "_refresh_current_values"), \
             patch.object(opt.MainWindow, "_refresh_developer_live_values"), \
             patch.object(opt, "DRBPMReader", return_value=Mock()), \
             patch("epics.PV", side_effect=AssertionError("hardware")):
            self.window = opt.MainWindow()
        self.window._current_value_timer.stop()

    def tearDown(self):
        self.window.worker = None
        self.window.close()
        self.app.processEvents()
        self.tmp.cleanup()

    def prepare_run(self, profile):
        w = self.window
        w._run_profile = profile
        w._run_mode = "GROUP_BO"
        csv = self.root / profile / "LiniacOptimization_Log_offline.csv"
        csv.parent.mkdir(exist_ok=True)
        csv.write_text("offline test\n")
        w.current_measurements_csv_by_profile[profile] = csv
        w._on_worker_progress(dict(kind="evaluation", eval_index=12, ttot=0.8,
            downstream_value=0.5, scan_progress=dict(label="QA", index=3, max_evals=20, stage="measuring")))
        self.assertIn("QA | eval=3/20", w._status_labels[profile].text())
        self.assertIn("total=12", w._status_labels[profile].text())
        trace = dict(axis="QA1L", x_grid=[-1, 0, 1], y_mean=[0.1, 0.2, 0.1],
                     y_std=[0.01]*3, acquisition=[0.01]*3, x_obs=[0], y_obs=[0.2])
        w._on_worker_progress(dict(kind="bo1d_trace", trace=trace))
        return csv.parent

    def test_both_profiles_normal_and_save_end_export_one_png(self):
        for profile in (opt.RUN_PROFILE_MAIN, opt.RUN_PROFILE_DEVELOPER):
            for stopped in (False, True):
                with self.subTest(profile=profile, stopped=stopped):
                    run_dir = self.prepare_run(profile)
                    dest = self.root / f"nas-{profile}-{stopped}"
                    dest.mkdir()
                    self.window.worker = Mock(is_running=not stopped, _current_pv_values={})
                    with patch.object(opt, "LINAC_TEMPFILES_DIR", dest), \
                         patch.object(opt.QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as ask, \
                         patch.object(opt.QMessageBox, "warning") as warn, \
                         patch.object(self.window, "_refresh_current_values"):
                        self.window.optimization_finished()
                    ask.assert_called_once()
                    self.assertEqual(ask.call_args.args[2], "Save plots to tempfiles?")
                    warn.assert_not_called()
                    self.assertEqual(len(list(run_dir.glob("*.png"))), 2)
                    exported = list(dest.iterdir())
                    self.assertEqual(len(exported), 1)
                    self.assertFalse(QImage(str(exported[0])).isNull())

    def test_no_and_missing_directory_preserve_normal_plots(self):
        self.prepare_run(opt.RUN_PROFILE_MAIN)
        plots = self.window._save_live_plot_snapshot(opt.RUN_PROFILE_MAIN)
        with patch.object(opt.QMessageBox, "question", return_value=QMessageBox.StandardButton.No), \
             patch.object(opt, "save_plot_montage") as save:
            self.window._offer_tempfiles_export(plots, opt.RUN_PROFILE_MAIN)
        save.assert_not_called()
        with patch.object(opt.QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
             patch.object(opt.QMessageBox, "warning") as warn, \
             patch.object(opt, "LINAC_TEMPFILES_DIR", self.root / "missing"):
            self.window._offer_tempfiles_export(plots, opt.RUN_PROFILE_MAIN)
        warn.assert_called_once()
        self.assertTrue(all(p.exists() for p in plots))
        self.assertFalse((self.root / "missing").exists())

    def test_no_data_produces_no_export_prompt(self):
        with patch.object(opt.QMessageBox, "question") as ask:
            self.window._offer_tempfiles_export(self.window._save_live_plot_snapshot(opt.RUN_PROFILE_MAIN), opt.RUN_PROFILE_MAIN)
        ask.assert_not_called()


if __name__ == "__main__":
    unittest.main()
