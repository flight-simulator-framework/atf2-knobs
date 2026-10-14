"""Offline GUI construction regression: never construct a real interface."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication
from Knobs.GUIs import IPBSM_Opt_GUI as gui


class TestGUIStartup(unittest.TestCase):
    def test_settings_construct_without_machine(self):
        app = QApplication.instance() or QApplication([])
        with patch.object(gui, "IPBSMInterface", return_value=object()):
            window = gui.MainWindow()
        try:
            self.assertEqual(window.hyperparameter_mode_box.currentText(), "legacy_fixed")
            self.assertEqual(window.zscan_kernel_box.currentText(), "rbf")
            self.assertEqual(window.zscan_initial_points_box.value(), 3)
            window.hyperparameter_mode_box.setCurrentText("learned_fixed")
            self.assertEqual(window.hyperparameter_mode_box.currentText(), "learned_fixed")
        finally:
            window.close()
            app.processEvents()


if __name__ == "__main__":
    unittest.main()
