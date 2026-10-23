"""Offline desktop geometry checks for the shared optimizer workspace."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication
from Knobs import Linac_Opt as linac
from Knobs.GUIs import IPBSM_Opt_GUI as ipbsm


class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_window(self, module):
        if module is linac:
            with patch.object(linac.MainWindow, "_refresh_current_values"), patch.object(
                    linac.MainWindow, "_refresh_developer_live_values"), patch.object(
                    linac, "DRBPMReader", return_value=Mock()):
                window = linac.MainWindow()
            window._current_value_timer.stop()
            return window
        with patch.object(ipbsm, "IPBSMInterface", return_value=object()):
            return ipbsm.MainWindow()

    def test_graphs_fit_beside_controls_at_laptop_size(self):
        with patch("epics.PV", side_effect=AssertionError("hardware")):
            for module in (linac, ipbsm):
                with self.subTest(module=module.__name__):
                    window = self.make_window(module)
                    try:
                        window.resize(1280, 800)
                        window.show()
                        self.app.processEvents()
                        self.assertEqual((window.width(), window.height()), (1280, 800))
                        canvas = window.eval_canvas if module is linac else window.canvas
                        controls = window.workspace.controls_scroll
                        self.assertGreater(canvas.mapTo(window, QPoint()).x(),
                                           controls.mapTo(window, QPoint()).x() + controls.width())
                        self.assertGreater(canvas.width(), 600)
                        self.assertGreater(canvas.height(), 500)
                        self.assertEqual(controls.horizontalScrollBar().maximum(), 0)
                        log = window.txt_log if module is linac else window.log_box
                        self.assertTrue(log.isHidden())
                        window.workspace.log_toggle.click()
                        self.app.processEvents()
                        self.assertTrue(log.isVisible())
                        window.bo1d_canvas.setVisible(True)
                        self.app.processEvents()
                        self.assertGreaterEqual(canvas.height(), 230)
                        self.assertGreaterEqual(window.bo1d_canvas.height(), 210)
                        self.assertEqual(window.height(), 800)
                        # Adjusting the horizontal divider reallocates plot width.
                        before = canvas.width()
                        window.workspace.splitter.setSizes([400, 840])
                        self.app.processEvents()
                        self.assertGreater(canvas.width(), before)
                    finally:
                        window.close()
                        self.app.processEvents()

    def test_ipbsm_gallery_scales_to_monitor_column(self):
        window = self.make_window(ipbsm)
        try:
            window.resize(1280, 800)
            window.show()
            window.main_view_stack.setCurrentIndex(1)
            self.app.processEvents()
            with tempfile.TemporaryDirectory() as directory:
                paths = []
                for name in ("test_modulation_vs_evaluation.png", "test_average_vs_evaluation.png"):
                    path = Path(directory) / name
                    image = QImage(1800, 600, QImage.Format.Format_RGB32)
                    image.fill(0xFFFFFF)
                    image.save(str(path))
                    paths.append(path)
                window._populate_gallery(paths)
                self.app.processEvents()
                self.assertEqual(window.gallery_grid.columnCount(), 1)
                self.assertEqual(window.gallery_scroll.horizontalScrollBar().maximum(), 0)
                window.workspace.splitter.setSizes([560, 680])
                self.app.processEvents()
                self.assertEqual(window.gallery_scroll.horizontalScrollBar().maximum(), 0)
        finally:
            window.close()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
