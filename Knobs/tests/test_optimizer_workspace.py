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
                        progress = {"percent": 37.5, "completed": 15, "total": 40}
                        if module is linac:
                            window._on_worker_progress({"kind": "overall_progress", "overall_progress": progress})
                        else:
                            window._on_progress({"step": 0, "info": {"phase": "overall_progress", "overall_progress": progress}})
                        self.assertEqual(window.total_progress.value(), 375)
                        self.assertIn("37.5%", window.total_progress.format())
                        self.assertGreater(window.total_progress.y(), window.status_lbl.y() + window.status_lbl.height())
                        self.assertGreaterEqual(window.status_lbl.height(), 48)
                        canvas = window.eval_canvas if module is linac else window.canvas
                        controls = window.workspace.controls_scroll
                        self.assertGreater(canvas.mapTo(window, QPoint()).x(),
                                           controls.mapTo(window, QPoint()).x() + controls.width())
                        self.assertGreater(canvas.width(), 600)
                        self.assertGreater(canvas.height(), 450)
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

    def test_ipbsm_all_gallery_plots_fit_without_scrolling(self):
        window = self.make_window(ipbsm)
        try:
            window.show()
            window.main_view_stack.setCurrentIndex(1)
            with tempfile.TemporaryDirectory() as directory:
                paths = []
                for index in range(12):
                    path = Path(directory) / f"plot_{index}.png"
                    image = QImage(1800, 600 if index < 2 else 1200, QImage.Format.Format_RGB32)
                    image.fill(0xFFFFFF)
                    image.save(str(path))
                    paths.append(path)
                for size in ((1280, 800), (1440, 900)):
                    window.resize(*size)
                    for count in (2, 6, 12, 1):
                        with self.subTest(size=size, count=count):
                            window._populate_gallery(paths[:count], bo_main_layout=True)
                            self.app.processEvents()
                            gallery = window.gallery_widget
                            self.assertEqual(len(gallery.panels), count)
                            self.assertEqual((window.width(), window.height()), size)
                            for panel in gallery.panels:
                                self.assertTrue(panel.isVisible())
                                self.assertTrue(gallery.rect().contains(panel.geometry()))
                                self.assertGreater(panel.width(), 50)
                                self.assertGreater(panel.height(), 50)
                            self.assertGreaterEqual(gallery.columns, 1)
                    window.workspace.splitter.setSizes([560, 680])
                    self.app.processEvents()
                    for panel in window.gallery_widget.panels:
                        self.assertTrue(window.gallery_widget.rect().contains(panel.geometry()))
        finally:
            window.close()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
