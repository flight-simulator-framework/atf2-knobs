"""Real pointer events on both dispersion plots, including during acquisition."""
import threading
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
from matplotlib.backend_bases import MouseEvent
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from Knobs.DRTuning.gui import TuningSession
from Knobs.DRTuning.tests.test_bump_dispersion import DispersionFixture


class DispersionPlotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for(self, condition):
        end = time.monotonic() + 10
        while not condition():
            self.app.processEvents()
            time.sleep(.005)
            self.assertLess(time.monotonic(), end)
        self.app.processEvents()

    def point(self, plot, ax, name, click=False):
        plot.canvas.draw()
        names, coordinates, *_ = plot.points[ax]
        x, y = ax.transData.transform(coordinates[list(names).index(name)])
        kind = 'button_press_event' if click else 'motion_notify_event'
        event = MouseEvent(kind, plot.canvas, x, y, button=1 if click else None)
        plot.canvas.callbacks.process(kind, event)

    def test_vertical_points_remain_editable_during_zv_scan_and_after_set(self):
        from Knobs.DRTuning.tests.test_model import CountedBackend
        from Knobs.DRCorrection.backends import DemoBackend
        from unittest.mock import patch
        machine = CountedBackend()
        model = DemoBackend()
        model.match_report = {}
        with tempfile.TemporaryDirectory() as root:
            window = TuningSession(machine, QSettings(str(Path(root)/'prefs'), QSettings.Format.IniFormat))
            window.show()
            window.open_tool('dispersion')
            panel = window.panels['dispersion']
            manager = window.service.model_manager
            def prepare(opt, cancel, progress):
                manager.baseline = machine.snapshot()
                manager.settings = {'raw_pvs': {}}
                return model
            manager.prepare = prepare
            release = threading.Event()
            try:
                panel.find_button.click()
                self.wait_for(lambda: not window.task.isRunning() and panel.find_button.isEnabled())
                self.assertTrue(panel.measure_button.isEnabled())
                waiting = threading.Event()
                def baseline(data):
                    panel.baseline_observed.emit(data)
                    waiting.set()
                    if not release.wait(10):
                        raise RuntimeError('test worker timed out')
                window.service.dispersion_observed = baseline
                panel.measure_button.click()
                self.wait_for(lambda: waiting.is_set() and 'options' not in panel.plot.data)
                name = machine.bpm_names[42]
                self.point(panel.plot, panel.plot.figure.axes[1], name, click=True)
                release.set()
                self.wait_for(lambda: not window.task.isRunning() and panel.find_button.isEnabled())
                self.assertFalse(window._failed, window.messages.toPlainText())
                self.assertIn(name, panel.data['options']['excluded_bpms'])
                self.assertTrue(panel.apply_button.isEnabled())
                with patch.object(window.service.cancel, 'wait', return_value=False):
                    panel.apply_button.click()
                    self.wait_for(lambda: not window.task.isRunning() and panel.find_button.isEnabled())
                self.assertTrue(panel.data['applied'])
                ax = panel.plot.figure.axes[1]
                for label in ('Measured before','Predicted','Measured after SET'):
                    curve = next(line for line in ax.lines if line.get_label()==label)
                    self.assertEqual(len(curve.get_xdata()), len(machine.bpm_names)-1)
                    self.assertNotIn(machine.positions_m[42], curve.get_xdata())
                    self.assertTrue(np.isfinite(curve.get_ydata()).all())
                self.point(panel.plot, ax, name, click=True)
                self.assertNotIn(name, panel.plot.excluded)
                self.assertIsNotNone(panel.plot.data)
            finally:
                release.set()
                if window.task:
                    window.task.wait(10000)
                window.close()

    def test_toggle_while_measuring_and_overwrite_for_next_session(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / 'bpm.ini')
            def session():
                return TuningSession(DispersionFixture(), QSettings(path, QSettings.Format.IniFormat))
            window = session()
            release = threading.Event()
            try:
                window.show()
                window.open_tool('qm_dispersion')
                panel = window.qm_dispersion
                panel.autosave_directory = Path(root)
                panel.excluded.setText(', '.join(list(window.backend.quadrupoles)[1:]))
                original = panel.service.measure_remaining
                waiting = threading.Event()
                def hold(*args):
                    waiting.set()
                    if not release.wait(10):
                        raise RuntimeError('test worker timed out')
                    return original(*args)
                panel.service.measure_remaining = hold
                panel.calc_button.click()
                self.wait_for(lambda: waiting.is_set() and panel.plot.data is not None)
                self.assertFalse(panel.calc_button.isEnabled())
                self.assertTrue(panel.plot.canvas.isEnabled())
                plot = panel.plot
                ax = plot.figure.axes[0]
                name = 'dr:MB25R'
                self.point(plot, ax, name)
                self.assertIn(name, plot.hovered[1])
                self.assertIn('Dispersion X =', plot.hovered[1])
                self.point(plot, ax, name, click=True)
                self.assertIn(name, plot.excluded)
                grey = next(line for line in plot.figure.axes[0].lines if line.get_label()=='Excluded')
                self.assertEqual(len(grey.get_xdata()), 1)
                save = next(a for a in plot.canvas.toolbar.actions() if a.text()=='Save BPM selection')
                save.trigger()
                release.set()
                self.wait_for(lambda: not window.task.isRunning() and panel.calc_button.isEnabled())
                self.assertFalse(window._failed, window.messages.toPlainText())
                idx = panel.result['names'].index(name)
                self.assertFalse(panel.result['valid'][idx])
                self.assertIn(name, panel.result['options']['excluded_bpms'])
                self.assertNotIn(panel.result['positions_m'][idx], plot.figure.axes[0].lines[0].get_xdata())
                predicted = next(line for line in plot.figure.axes[0].lines if line.get_label()=='Predicted')
                self.assertTrue(np.isfinite(predicted.get_ydata()).all())
                self.point(plot, plot.figure.axes[0], name, click=True)
                self.assertNotIn(name, plot.excluded)
                self.assertFalse(panel.set_button.isEnabled())
                self.assertTrue(panel.recalculate_button.isEnabled())
            finally:
                release.set()
                if window.task:
                    window.task.wait(10000)
                window.close()
            # Unsaved reenabling did not replace the earlier saved exclusion.
            window = session()
            try:
                vertical = window.panels['dispersion']
                self.assertIn(name, vertical.plot.excluded)
                self.assertIn(name, window.qm_dispersion.options().excluded_bpms)
                window.toggle_dispersion_bpm(name)
                window.save_bpm_selection()
            finally:
                window.close()
            window = session()
            try:
                self.assertNotIn(name, window.panels['dispersion'].options.excluded_bpms)
            finally:
                window.close()
