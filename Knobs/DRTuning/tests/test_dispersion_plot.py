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

    def test_dispersion_rms_uses_same_bpms_and_distinguishes_prediction_from_measurement(self):
        from Knobs.DRTuning.dispersion_plot import dispersion_summary
        names=['MB1R','MB2R','MB19R','MB20R','MB21R']
        before=np.array([12.,42.,4.,50.,80.])
        data=dict(names=names,options=dict(arc_weight=1.,arc_parity='odd'),
                  full_predicted=np.tile(before/2,(4,1)),full_after=np.tile(before/4,(4,1)),
                  after_valid=[True,True,True,True,False])
        valid=np.array([True,True,True,False,True])
        text=dispersion_summary(data,'x',valid,before)
        self.assertIn('Straight ηx RMS · 1 BPM',text)
        self.assertIn('Before 4   |   Predicted 2   |   Measured after 1 · decrease 3 (75.0%)',text)
        self.assertIn('Arc ηx (odd) RMS · 1 BPM',text)
        self.assertIn('Before 12   |   Predicted 6',text)
        data.update(target=[before], restored=True)
        text=dispersion_summary(data,'y',valid,before)
        self.assertIn('Arc ηy − target RMS · 2 BPM',text)
        self.assertIn('Trial (restored)',text)
        self.assertIn('increase',text)
        self.assertNotIn('%',text)
        self.assertIn('no common valid BPMs',dispersion_summary(data,'y',valid & False,before))

    def test_invalid_observations_hidden_and_manual_exclusion_reversible(self):
        from Knobs.DRTuning.dispersion_plot import DispersionPlot
        plot = DispersionPlot()
        self.addCleanup(plot.close)
        names = ['MB1R', 'MB2R', 'MB3R', 'MB4R', 'MB5R']
        observed = dict(names=names, positions_m=[1., 2., 3., 4., np.nan],
                        valid=[True, True, True, False, False],
                        full_measured=np.tile([1., 2., 3., 99999., 0.], (4, 1)),
                        full_signal_labels=['Dispersion X', 'Dispersion Y', 'COD X', 'COD Y'])
        # The live preview uses all BPMs. The nominal response uses mapped rows.
        subset = [2, 1, 3]
        nominal = dict(observed, names=[names[i] for i in subset], bpm_observations=observed,
                       valid=[True, False, False], acquisition_valid=[True, True, False],
                       full_predicted=observed['full_measured'][:, subset])
        plot.bpm_clicked.connect(lambda name: plot.set_excluded(plot.excluded ^ {name}))
        for data in (observed, nominal):
            plot.set_excluded({'MB2R', 'MB4R'})
            plot.correction(data)
            for ax in plot.figure.axes:
                self.assertEqual(ax.get_xlabel(), 's [m]')
                self.assertLess(ax.get_ylim()[1], 10.)
                self.assertEqual(list(plot.points[ax][0]), names[:3])
                grey = next(line for line in ax.lines if line.get_label() == 'Excluded')
                np.testing.assert_equal(grey.get_xdata(), [2.])
            self.point(plot, plot.figure.axes[1], 'MB2R', click=True)
            self.assertEqual(plot.excluded, {'MB4R'})
            self.assertIn(2., plot.figure.axes[1].lines[0].get_ydata())
            plot.set_excluded(())
            for ax in plot.figure.axes:
                self.assertLess(ax.get_ylim()[1], 10.)
                self.assertNotIn('MB4R', plot.points[ax][0])
                self.assertNotIn('Invalid for fit', ax.get_legend_handles_labels()[1])

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
            panel.data_directory=lambda:root
            panel.tempfiles_directory=Path(root)/'tempfiles'
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
                self.assertFalse(panel.apply_button.isEnabled())
                with self.assertRaisesRegex(ValueError, 'Measure the selected ZVs'):
                    window.service.apply(panel.data)
                waiting = threading.Event()
                def baseline(data):
                    panel.baseline_observed.emit(data)
                    waiting.set()
                    if not release.wait(10):
                        raise RuntimeError('test worker timed out')
                window.service.dispersion_observed = baseline
                panel.measure_button.click()
                self.wait_for(lambda: waiting.is_set() and 'options' not in panel.plot.data)
                self.assertTrue(panel.matrix_progress.isVisible())
                self.assertEqual(panel.matrix_progress.shots.value(),10)
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
                exports=list(panel.tempfiles_directory.iterdir())
                self.assertEqual(len(exports),3)
                for stage in ('proposal','matrix','applied'):
                    png,=[path for path in exports if path.name.startswith(f'vertical_dispersion_{stage}_')]
                    self.assertEqual(png.read_bytes()[:8],bytes.fromhex('89504e470d0a1a0a'))
                ax = panel.plot.figure.axes[1]
                for label in ('Measured before','Predicted','Measured after SET'):
                    curve = next(line for line in ax.lines if line.get_label()==label)
                    self.assertEqual(len(curve.get_xdata()), len(machine.bpm_names)-1)
                    self.assertNotIn(machine.positions_m[42], curve.get_xdata())
                    arc={*range(1,19),*range(32,65),*range(81,97)}
                    highlighted=[int(n.split('MB')[1][:-1]) in arc for n in machine.bpm_names if n!=name]
                    np.testing.assert_array_equal(np.isfinite(curve.get_ydata()),highlighted)
                self.assertTrue(any(line.get_alpha()==.28 and np.isfinite(line.get_ydata()).all() for line in ax.lines))
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
                original = window.backend.set_quadrupole_current
                waiting = threading.Event()
                def hold(name, value):
                    # Dispersion becomes available at the positive RF reference.
                    if window.backend.offset > 0 and not waiting.is_set():
                        waiting.set()
                        if not release.wait(10):
                            raise RuntimeError('test worker timed out')
                    return original(name, value)
                window.backend.set_quadrupole_current = hold
                panel.calc_button.click()
                self.wait_for(lambda: waiting.is_set() and panel.plot.data is not None and
                              np.isfinite(panel.plot.data['full_measured'][0]).any())
                self.assertFalse(panel.calc_button.isEnabled())
                self.assertTrue(panel.plot.canvas.isEnabled())
                self.assertTrue(panel.matrix_progress.isVisible())
                self.assertIn('RF +2 kHz',panel.matrix_progress.point.text())
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
