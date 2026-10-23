"""Offline desktop geometry checks for the shared optimizer workspace."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication, QDoubleSpinBox
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

    def test_linac_tabs_remain_navigable_with_run_settings_locked(self):
        window = self.make_window(linac)
        try:
            window.show()
            window.objective_box.setCurrentText("ICT")
            window.cb_downstream_ict.setCurrentText("LNE")
            self.app.processEvents()
            self.assertTrue(window.cb_downstream_ict.isVisible())
            window.objective_box.setCurrentText("BPM intensity")
            self.assertTrue(window.cb_downstream_ict.isHidden())
            window.objective_box.setCurrentText("ICT")
            self.assertEqual(window.cb_downstream_ict.currentText(), "LNE")
            window._set_main_run_controls_enabled(False)
            window._set_developer_run_controls_enabled(False)
            with patch.object(window, "_refresh_current_values") as main_read, patch.object(
                    window, "_refresh_developer_live_values") as developer_read:
                for page in (window.config_tab, window.detail_config_tab, window.main_tab):
                    index = window.tabs.indexOf(page)
                    QTest.mouseClick(window.tabs.tabBar(), Qt.MouseButton.LeftButton,
                                     pos=window.tabs.tabBar().tabRect(index).center())
                    self.app.processEvents()
                    self.assertIs(window.tabs.currentWidget(), page)
                for index in range(window.config_sections.count()):
                    window.tabs.setCurrentWidget(window.config_tab)
                    QTest.mouseClick(window.config_sections.tabBar(), Qt.MouseButton.LeftButton,
                                     pos=window.config_sections.tabBar().tabRect(index).center())
                    self.assertEqual(window.config_sections.currentIndex(), index)
                main_read.assert_not_called()
                developer_read.assert_not_called()
            self.assertFalse(window.cb_downstream_ict.isEnabled())
            self.assertFalse(window.sp_phase_half.isEnabled())
            self.assertFalse(window.group_bo_max_eval_spinboxes["timing"].isEnabled())
            self.assertTrue(window.detail_config_scroll.isEnabled())
            window._set_main_run_controls_enabled(True)
            window._set_developer_run_controls_enabled(True)
            window._update_mode_ui()
            self.assertTrue(window.cb_downstream_ict.isEnabled())
            self.assertTrue(window.sp_phase_half.isEnabled())
        finally:
            window.close()
            self.app.processEvents()

    def test_ipbsm_operator_config_stays_simple_after_loading_and_method_changes(self):
        window = self.make_window(ipbsm)
        try:
            window.show()
            window._set_config_to_ui({"params": ["Ay", "corrector 1"],
                                      "average_pause_ratio": .75})
            self.assertEqual(window._collect_config().params, ["Ay"])
            self.assertFalse(window.knob_checks["corrector 1"].isChecked())
            self.assertFalse(window.corrector_preset_btn.isVisible())
            self.assertFalse(window.knob_checks["corrector 1"].isVisible())
            window.tabs.setCurrentWidget(window.config_tab)
            self.app.processEvents()
            for method in ("Sequential", "BO"):
                window.method_box.setCurrentText(method)
                self.app.processEvents()
                visible_fields = {field for field in window.config_tab.findChildren(QDoubleSpinBox)
                                  if field.isVisible()}
                expected = {window.avg_pause_ratio, window.zscan_sigma_box}
                expected.update(box for name, box in window.sigma_boxes.items()
                                if name not in ipbsm.CORRECTOR_KNOBS)
                self.assertEqual(visible_fields, expected)
                self.assertFalse(window.ei_stop_mode_box.isVisible())
                self.assertFalse(window.bo_max_steps.isVisible())
                self.assertFalse(window.save_cfg_btn.isVisible())
                self.assertEqual(window._collect_config().average_pause_ratio, .75)
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
