"""Live BO cursors: real PNG transforms, resize mapping and measurement lifecycle."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PyQt6.QtWidgets import QApplication, QMessageBox
from Knobs.GUIs import IPBSM_Opt_GUI as gui
from Knobs.tests.test_ipbsm_completion import config


class MeasurementMarkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch.object(gui, "IPBSMInterface", return_value=object()):
            self.window = gui.MainWindow()
        self.directory = tempfile.TemporaryDirectory()
        self.window.last_out_dir = Path(self.directory.name)
        self.window.current_measurements_csv = Path(self.directory.name) / "not_yet_measured.csv"
        self.window.last_run_cfg = config(params=["Ay", "Ey"],
                                         bounds={"Ay": (10., 14.), "Ey": (-2., 2.)},
                                         init_sigma={"Ay": .2, "Ey": .4})
        self.window.resize(1280, 800)
        self.window.show()
        # The very first measurement has no data yet, but needs visible axes.
        self.window._refresh_gf_gallery_live(bo_data_only_1d=True)
        self.window.main_view_stack.setCurrentIndex(1)
        self.app.processEvents()

    def tearDown(self):
        self.window._set_measurement_position({})
        self.window.close()
        self.app.processEvents()
        self.directory.cleanup()

    def measuring(self, **values):
        self.window._on_progress({"step": 1, "info": {"phase": "measuring", "x": values}})
        self.app.processEvents()

    def test_all_axes_mark_first_and_next_position_with_correct_scaled_coordinates(self):
        w = self.window
        self.assertEqual(len(w.gallery_widget.panels), 2)
        self.measuring(Ay=13., Ey=-1.)
        self.assertTrue(w._measurement_blink_timer.isActive())
        for size in ((1280, 800), (1440, 900)):
            w.resize(*size)
            self.app.processEvents()
            for panel in w.gallery_widget.panels:
                axis = panel.axis_metadata["axis"]
                rect, start, end = panel.measurement_line()
                expected_fraction = .75 if axis == "Ay" else .25
                self.assertAlmostEqual((start.x() - rect.left()) / rect.width(), expected_fraction)
                self.assertEqual(start.y(), rect.top())
                self.assertEqual(end.y(), rect.bottom())
                self.assertTrue(panel.rect().contains(start.toPoint()))
                self.assertTrue(panel.rect().contains(end.toPoint()))
        self.measuring(Ay=11., Ey=1.)
        for panel in w.gallery_widget.panels:
            axis = panel.axis_metadata["axis"]
            self.assertEqual(panel.measurement_value, 11. if axis == "Ay" else 1.)
        # GUI blinking changes pixels, never the saved scientific plot.
        original_files = {p: p.read_bytes() for p in w.last_out_dir.glob("*.png")}
        panel = w.gallery_widget.panels[0]
        before = panel.grab().toImage()
        w._blink_measurement_position()
        after = panel.grab().toImage()
        self.assertFalse(before == after)
        self.assertFalse(panel.measurement_visible)
        self.assertEqual(original_files, {p: p.read_bytes() for p in original_files})
        # A refresh of the gallery retains the current measurement cursor.
        w._populate_gallery(list(original_files))
        self.assertTrue(all(p.measurement_value is not None for p in w.gallery_widget.panels))

    def test_completion_reuse_stop_and_failure_clear_cursor(self):
        w = self.window
        with patch.object(w, "_refresh_gf_gallery_live"), patch.object(w, "_redraw_live_plot"):
            for phase in ("init", "loop", "reuse", "discarded_measurement", "model_fit", "stop"):
                self.measuring(Ay=13., Ey=-1.)
                w._on_progress({"step": 1, "info": {"phase": phase}})
                self.assertFalse(w._measurement_blink_timer.isActive())
                self.assertTrue(all(p.measurement_line() is None for p in w.gallery_widget.panels))
        self.measuring(Ay=13., Ey=-1.)
        with patch.object(QMessageBox, "critical"):
            w._on_failed("offline test")
        self.assertFalse(w._measurement_blink_timer.isActive())
        self.measuring(Ay=13., Ey=-1.)
        w.last_out_dir = None
        w._on_finished({})
        self.assertFalse(w._measurement_blink_timer.isActive())

    def test_pause_resume_restarts_at_same_position_and_abort_clears(self):
        w = self.window
        self.measuring(Ay=13., Ey=-1.)
        w._measurement_blink_timer.stop()
        w.worker = Mock()
        try:
            w._resume_measurement_from_pause(True, remeasure_current_point=True)
            self.assertTrue(w._measurement_blink_timer.isActive())
            self.assertEqual(w._measurement_position, {"Ay": 13., "Ey": -1.})
            w.worker.resume_from_pause.assert_called_with(True, remeasure_current_point=True)
            w._resume_measurement_from_pause(False)
            self.assertFalse(w._measurement_blink_timer.isActive())
            self.assertEqual(w._measurement_position, {})
        finally:
            w.worker = None


if __name__ == "__main__":
    unittest.main()
