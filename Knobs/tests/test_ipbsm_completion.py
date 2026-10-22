"""Offline completion/export tests: no machine interface or NAS access."""
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication, QMessageBox
from Knobs.GUIs import IPBSM_Opt_GUI as gui
from Knobs.IPBSM_Opt import BaseIPBSMController, Optimizer, OptimizerConfig
from Knobs.ipbsm_opt_plotting import save_plot_montage


class FakeController(BaseIPBSMController):
    def apply_knobs(self, values):
        self.values = values

    def get_ipbsm(self):
        return 0.2, 0.01


def config(**changes):
    settings = dict(mode_name="linear", method="BO", acquisition="EI", params=["Ay"],
                    bounds={"Ay": (-1.0, 1.0)}, init_sigma={"Ay": 0.2},
                    n_init_random=2, bo_max_steps=6, n_bootstrap=0,
                    bo_low_acq_threshold=0.001, bo_low_acq_patience=2)
    settings.update(changes)
    return OptimizerConfig(**settings)


class CompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch.object(gui, "IPBSMInterface", return_value=object()):
            self.window = gui.MainWindow()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.tmp.cleanup()

    def test_custom_ei_roundtrip_and_presets(self):
        w = self.window
        w._set_config_to_ui(asdict(config(bo_low_acq_threshold=0.000789, bo_low_acq_patience=4)))
        self.assertEqual(w.ei_stop_mode_box.currentText(), "Custom")
        loaded = w._collect_config()
        self.assertEqual(loaded.bo_low_acq_threshold, 0.000789)
        self.assertEqual(loaded.bo_low_acq_patience, 4)
        for name, pair in gui.EI_STOP_MODES.items():
            w.ei_stop_mode_box.setCurrentText(name)
            actual = w._collect_config()
            self.assertEqual((actual.bo_low_acq_threshold, actual.bo_low_acq_patience), pair)

    def test_progress_uses_running_budget_for_init_resume_and_measurement(self):
        w = self.window
        w.last_run_cfg = config(bo_max_steps=20)
        w.bo_max_steps.setValue(99)  # Editing next run must not change current budget.
        with patch.object(w, "_refresh_gf_gallery_live"), patch.object(w, "_redraw_live_plot"):
            for phase, step in (("init", 2), ("resume_remeasure", 8), ("measuring", 9), ("acquisition", 8)):
                info = {"phase": phase, "x": {"Ay": 0.0}, "max_acq": 0.002}
                if phase in {"init", "resume_remeasure"}:
                    info.update(y=0.2, best_y=0.2)
                w._on_progress({"step": step, "info": info})
                self.assertIn(f"step={step}/20", w.status_lbl.text())

    def _plots(self):
        paths = []
        for i, color in enumerate(("red", "green", "blue")):
            image = QImage(100, 50, QImage.Format.Format_RGB32)
            image.fill(QColor(color))
            path = self.root / f"{i}.png"
            self.assertTrue(image.save(str(path)))
            paths.append(path)
        return paths

    def test_montage_contains_all_panels_and_only_one_file(self):
        plots = self._plots()
        output = self.root / "export"
        output.mkdir()
        destination = save_plot_montage(plots + [plots[0]], output / "combined.png")
        image = QImage(str(destination))
        self.assertEqual((image.width(), image.height()), (272, 172))
        for x, y, color in ((30, 30, "red"), (155, 30, "green"), (30, 104, "blue")):
            self.assertEqual(image.pixelColor(x, y), QColor(color))
        self.assertEqual(list(output.iterdir()), [destination])

    def test_export_saves_without_confirmation(self):
        plots = self._plots()
        with patch.object(gui, "IPBSM_TEMPFILES_DIR", self.root), \
             patch.object(gui.QMessageBox, "question") as ask, \
             patch.object(gui, "save_plot_montage") as save:
            self.window._save_tempfiles_export(plots, self.root)
        ask.assert_not_called()
        save.assert_called_once()
        self.assertEqual(save.call_args.args[0], plots)
        self.assertEqual(save.call_args.args[1].parent, self.root)

    def test_unavailable_export_keeps_original_plots(self):
        plots = self._plots()
        with patch.object(gui, "IPBSM_TEMPFILES_DIR", self.root / "unmounted"), \
             patch.object(gui.QMessageBox, "warning") as warning:
            self.window._save_tempfiles_export(plots, self.root)
        warning.assert_called_once()
        self.assertTrue(all(p.exists() for p in plots))
        self.assertFalse((self.root / "unmounted").exists())

    def test_normal_and_save_end_completion_generate_one_automatic_png(self):
        for manual_stop in (False, True):
            with self.subTest(manual_stop=manual_stop):
                directory = self.root / str(manual_stop)
                destination = directory / "tempfiles"
                destination.mkdir(parents=True)
                cfg = config(bo_max_steps=2)
                pause = Mock(return_value=False)  # Save and End from pause dialog.
                optimizer = Optimizer(FakeController(), cfg, directory, pause_hook=pause)
                if manual_stop:
                    optimizer.request_manual_pause()
                result = optimizer.run()
                self.assertEqual(result["n_steps"], 1 if manual_stop else 2)
                if manual_stop:
                    self.assertEqual(pause.call_args.args[0]["reason"], "manual_pause")
                self.window.last_run_cfg = cfg
                with patch.object(gui, "IPBSM_TEMPFILES_DIR", destination), \
                     patch.object(gui.QMessageBox, "question") as ask, \
                     patch.object(gui.QMessageBox, "warning") as warning:
                    self.window._on_finished(result)
                ask.assert_not_called()
                warning.assert_not_called()
                self.assertGreater(len(list(directory.glob("*.png"))), 1)
                exported = list(destination.iterdir())
                self.assertEqual(len(exported), 1)
                self.assertEqual(exported[0].suffix, ".png")
                self.assertFalse(QImage(str(exported[0])).isNull())
                self.assertIn(f"step={result['n_steps']}/2", self.window.status_lbl.text())

    def test_save_end_after_first_measurement_error_has_no_fake_plots(self):
        controller = FakeController()
        controller.get_ipbsm = Mock(side_effect=TimeoutError("Remote scan not accepted"))
        cfg = config(n_bootstrap=2)
        pause = Mock(return_value=False)
        optimizer = Optimizer(controller, cfg, self.root, pause_hook=pause)
        result = optimizer.run()
        self.assertEqual(result["n_steps"], 0)
        pause.assert_called_once()
        self.window.last_run_cfg = cfg
        with patch.object(gui.QMessageBox, "question") as ask:
            self.window._on_finished(result)
        ask.assert_not_called()
        self.assertFalse(list(self.root.glob("*.png")))
        self.assertTrue((self.root / "result.json").exists())


class EIStopTests(unittest.TestCase):
    def test_threshold_streak_resets_and_stop_precedes_next_measurement(self):
        # Low, high, low, low: stop on the fourth check, after five evaluations.
        with tempfile.TemporaryDirectory() as directory:
            events = []
            optimizer = Optimizer(FakeController(), config(), Path(directory),
                                  progress_cb=lambda step, info: events.append((step, info)))
            proposals = [(np.array([x]), ei) for x, ei in
                         ((0.4, 0.001), (0.6, 0.1), (0.8, 0.0005), (-0.4, 0.0001))]
            with patch.object(optimizer, "_propose_next_BO", side_effect=proposals):
                result = optimizer.run()
            self.assertEqual(result["n_steps"], 5)
            stop = next(info for _, info in events if info.get("reason") == "low_acquisition_gain")
            self.assertEqual(stop["streak"], 2)
            self.assertEqual(stop["threshold"], 0.001)

    def test_disabled_ei_or_ucb_runs_to_budget(self):
        for acquisition, enabled in (("EI", False), ("UCB", True)):
            with self.subTest(acquisition=acquisition), tempfile.TemporaryDirectory() as directory:
                optimizer = Optimizer(FakeController(), config(acquisition=acquisition, bo_stop_on_low_acq=enabled), Path(directory))
                proposals = [(np.array([x]), 0.0) for x in (0.4, 0.6, 0.8, -0.4)]
                with patch.object(optimizer, "_propose_next_BO", side_effect=proposals):
                    self.assertEqual(optimizer.run()["n_steps"], 6)


if __name__ == "__main__":
    unittest.main()
