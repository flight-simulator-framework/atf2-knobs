import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import math
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from Knobs.RandomExcitation.catalog import CATALOG, BY_NAME, ICT_PVS, DR_MONITORS, DR_MODE, DR_TURN
from Knobs.RandomExcitation.core import Config, Runner, inspect
from Knobs.RandomExcitation.transport import DemoTransport, EpicsTransport, MonitorCache
from Knobs.RandomExcitation.gui import MainWindow


class FakePV:
    created = []
    metadata_gets = 0
    puts = []

    def __init__(self, name, **kwargs):
        self.name = name
        self.connected = True
        self.write_access = True
        self.callback = kwargs['callback']
        self.connection_callback = kwargs['connection_callback']
        self.kwargs = kwargs
        self.disconnected = False
        self.created.append(self)
        self.connection_callback(pvname=name, conn=True)
        value = -5 if name.endswith(':parameter.A') else (5 if name.endswith(':parameter.B') else 0.)
        if name == 'EVE_LINAC:OUT0:SetData':
            value = 1120.37
        if name == DR_MONITORS:
            value = np.zeros(980)
        self.callback(pvname=name, value=value, timestamp=time.time())

    def get(self, *args, **kwargs):
        raise AssertionError('Acquisition/display must not issue GET requests')

    def get_ctrlvars(self, **kwargs):
        type(self).metadata_gets += 1
        return {}

    def put(self, value, **kwargs):
        self.puts.append((self.name, value, kwargs))
        self.callback(pvname=self.name, value=value, timestamp=time.time())
        return 1

    def disconnect(self):
        self.disconnected = True


class TransportTests(unittest.TestCase):
    def setUp(self):
        FakePV.created, FakePV.puts, FakePV.metadata_gets = [], [], 0

    def test_persistent_monitors_no_polling_and_no_diagnostic_writes(self):
        b = EpicsTransport(pv_factory=FakePV).connect(timeout=0)
        self.addCleanup(b.close)
        self.assertEqual(len(FakePV.created), len({p.name for p in FakePV.created}))
        self.assertTrue(all(p.kwargs['auto_monitor'] and p.kwargs['form'] == 'time' for p in FakePV.created))
        count = len(FakePV.created)
        self.assertEqual([p.name for p in FakePV.created if p.name.startswith("EVE_LINAC")], ["EVE_LINAC:OUT0:SetData"])
        reads = FakePV.metadata_gets
        for _ in range(20):
            b.snapshot()
            b.history(ICT_PVS['DR'])
            inspect(b, Config())
        self.assertEqual(len(FakePV.created), count)
        self.assertEqual(FakePV.metadata_gets, reads)
        self.assertFalse(FakePV.puts)
        for name in [DR_MODE, DR_TURN, DR_MONITORS, ICT_PVS['DR'], 'QA1L:parameter.A']:
            with self.assertRaises(RuntimeError):
                b.put(name, 1)
        b.put('QA1L:currentWrite', 1.2)
        self.assertEqual(FakePV.puts[0][2], dict(wait=True, timeout=2.0))
        b.close()
        self.assertTrue(all(p.disconnected for p in FakePV.created))

    def test_disconnect_invalidates_cached_measurements_and_history(self):
        b = MonitorCache()
        b.on_value(pvname='test', value=2, timestamp=10)
        self.assertEqual(b.sample('test').scalar(), 2)
        b.on_connection(pvname='test', conn=False)
        self.assertTrue(math.isnan(b.sample('test').scalar()))
        self.assertFalse(b.history('test'))
        b.on_value(pvname='test', value=1, timestamp=11, severity=2)
        self.assertTrue(math.isnan(b.sample('test').scalar()))

    def test_ca_alarm_status_survives_callback_and_limits_use_udf_values(self):
        b = EpicsTransport(pv_factory=FakePV).connect(timeout=0)
        self.addCleanup(b.close)
        for pv, value in [('ZV2L:parameter.A', -5.), ('ZV2L:parameter.B', 5.)]:
            b._pvs[pv].callback(pvname=pv,value=value,timestamp=time.time(),status=17,severity=3)
            self.assertEqual(b.sample(pv).alarm_status,17)
        pv = 'ZV2L:currentWrite'
        b._pvs[pv].callback(pvname=pv,value=.2,timestamp=time.time(),status=17,severity=3)
        ready, errors = inspect(b, Config())
        self.assertIn('ZV2L',ready)
        self.assertEqual((ready['ZV2L'].lower,ready['ZV2L'].upper),(-5.,5.))
        self.assertNotIn('INVALID',ready['ZV2L'].limit_source)
        self.assertEqual(ready['ZV2L'].origin,.2)
        self.assertFalse(FakePV.puts)


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.window = MainWindow(demo=True)
        self.window.resize(1280, 800)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        if self.window.worker and self.window.worker.isRunning():
            self.window.worker.runner.stop()
            self.window.worker.wait(3000)
        self.window.close()
        self.app.processEvents()
        self.tmp.cleanup()

    def pump(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertTrue(predicate())

    def connect_demo(self):
        b = DemoTransport(period=.005).connect()
        ready, errors = inspect(b, self.window.config())
        self.window._connected(b, ready, errors)
        return b

    def test_startup_is_passive_and_dr_controls_opt_in(self):
        w = self.window
        self.assertIsNone(w.transport)
        self.assertFalse(w.start_button.isEnabled())
        self.assertEqual(len(w.selected()), 66)
        self.assertFalse(w.group_checks['dr'].isChecked())
        b = self.connect_demo()
        self.assertFalse(b.commands)
        self.assertTrue(w.start_button.isEnabled())
        w.group_checks['dr'].setChecked(True)
        self.assertEqual(len(w.selected()), 163)
        self.assertFalse(b.commands)

    def test_preview_timer_and_dr_tab_do_not_write_or_connect_more_channels(self):
        b = self.connect_demo()
        for _ in range(10):
            self.window.tabs.setCurrentIndex(2)
            self.window.refresh_live()
        self.assertFalse(b.commands)
        self.assertEqual(len(self.window.dr_plot.curves['X [mm]']), 96)
        self.assertIn('Read only', self.window.dr_info.text())
        self.assertNotEqual(self.window.ict_labels['DR'].text(), '—')

    def test_status_omits_invalid_for_usable_current_configuration(self):
        b = self.connect_demo()
        a = BY_NAME['ZV2L']
        for pv, value in [(a.writes[0], .2), (a.limit_pvs[0], -5.), (a.limit_pvs[1], 5.)]:
            b.on_value(pvname=pv, value=value, status=17, severity=3)
        ready, errors = inspect(b, self.window.config())
        self.window._connected(b, ready, errors)
        row = self.window.row_by_name[a.name]
        self.assertNotIn(a.name, errors)
        status = self.window.table.item(row, 8)
        self.assertEqual(self.window.table.horizontalHeaderItem(8).text(), 'Status')
        self.assertEqual(status.text(), 'connected')
        self.assertEqual(status.foreground().color().name(), '#167b4c')
        self.assertEqual(status.toolTip(), '')
        b.on_connection(pvname=a.read, conn=False)
        self.window.refresh_live()
        self.assertTrue(status.text().startswith('error · '))
        self.assertIn(a.read, status.text())
        self.assertEqual(status.foreground().color().name(), '#b91c1c')
        b.publish(a.read, .2)
        self.window.refresh_live()
        self.assertEqual(status.text(), 'connected')
        self.assertEqual(status.foreground().color().name(), '#167b4c')

    def test_actual_start_button_runs_and_retains_configuration_lock_until_done(self):
        w = self.window
        b = self.connect_demo()
        for key, check in w.group_checks.items():
            check.setChecked(key in ('phase', 'qa'))
        w.output.setText(self.tmp.name)
        w.guard.setValue(0)
        w.samples.setValue(2)
        w.max_points.setValue(4)
        w.start_button.click()
        self.assertFalse(w.start_button.isEnabled())
        self.assertTrue(w.pause_button.isEnabled())
        self.assertFalse(w.control_tabs.isEnabled())
        self.pump(lambda: not w.worker.isRunning())
        self.assertEqual(w.worker.runner.visits, 4)
        self.assertEqual(w.status.text(), 'Complete')
        self.assertTrue(w.start_button.isEnabled())
        self.assertGreater(len(b.commands), 0)
        self.assertTrue(list(Path(self.tmp.name).glob('*/commands.jsonl')))
        self.assertFalse(w.active_names)
        self.assertEqual(w.active_label.text(), 'No active commands')

    def test_current_user_ranges_survive_phase_range_edit(self):
        w = self.window
        self.connect_demo()
        row = w.row_by_name['QA1L']
        w.table.item(row, 4).setText('1.7')
        w.table.item(row, 5).setText('4.3')
        w.phase_range.setValue(60)
        self.assertEqual(w.table.item(row, 4).text(), '1.7')
        self.assertEqual(w.table.item(row, 5).text(), '4.3')
        self.assertEqual(w.table.item(w.row_by_name['L1'], 4).text(), '-60')

    def test_missing_ict_disables_start_without_writing(self):
        b = self.connect_demo()
        b._stop.set()
        b._thread.join()
        b.on_connection(pvname=ICT_PVS['DR'], conn=False)
        self.window.refresh_live()
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertFalse(b.commands)

    def test_pause_button_finishes_measurement_and_resume_remeasures(self):
        w = self.window
        self.connect_demo()
        w.output.setText(self.tmp.name)
        w.guard.setValue(.01)
        w.samples.setValue(10)
        w.max_points.setValue(3)
        entered, release = threading.Event(), threading.Event()
        measure = Runner._measure
        def first_measure(runner, kind, targets):
            if kind == 'initial':
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('Test did not release the first measurement')
            return measure(runner, kind, targets)
        with patch.object(w, 'show_pause_choices') as dialog, patch.object(Runner, '_measure', first_measure):
            w.start_button.click()
            try:
                self.pump(entered.is_set)
                w.pause_button.click()
            finally:
                release.set()
            self.pump(lambda: w.worker.runner.is_paused)
            self.assertEqual(w.worker.runner.visits, 1)
            dialog.assert_called_once()
            w.worker.runner.resume()
            self.pump(lambda: not w.worker.isRunning())
        import json
        records = [json.loads(x) for x in (w.worker.runner.journal.directory/'commands.jsonl').read_text().splitlines()]
        kinds = [x['kind'] for x in records if x['event'] == 'measurement_window']
        self.assertEqual(kinds[1], 'pause_remeasure')

    def test_pause_dialog_close_matches_linacopt_resume(self):
        w = self.window
        runner = Mock(is_paused=True, running=True, best={'score': 2})
        w.worker = Mock(runner=runner)
        with patch.object(QMessageBox, 'exec', return_value=0), patch.object(QMessageBox, 'clickedButton', return_value=None):
            w.show_pause_choices()
        runner.resume.assert_called_once()
        runner.save_and_end.assert_not_called()
        w.worker = None

    def test_restore_button_finishes_measurement_and_reports_initial_restored(self):
        w = self.window
        b = self.connect_demo()
        for key, check in w.group_checks.items():
            check.setChecked(key == 'qa')
        w.output.setText(self.tmp.name)
        w.guard.setValue(0)
        w.samples.setValue(2)
        w.max_points.setValue(20)
        entered, release = threading.Event(), threading.Event()
        measure = Runner._measure
        def held_measure(runner, kind, targets):
            if runner.visits == 1:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('Test did not release measurement')
            return measure(runner, kind, targets)
        with patch.object(Runner, '_measure', held_measure):
            w.start_button.click()
            try:
                self.pump(entered.is_set)
                self.assertTrue(w.restore_button.isEnabled())
                w.restore_button.click()
                self.assertFalse(w.restore_button.isEnabled())
                self.assertTrue(w.stop_button.isEnabled())
            finally:
                release.set()
            self.pump(lambda: not w.worker.isRunning())
        self.assertEqual(w.status.text(), 'Finished · Initial settings restored')
        self.assertEqual(w.worker.runner.visits, 2)
        for p in w.worker.runner.items.values():
            self.assertAlmostEqual(b.sample(p.actuator.writes[0]).scalar(), p.origin)
        self.assertFalse(w.restore_button.isEnabled())

    def test_pause_dialog_can_restore_initial_and_end(self):
        w = self.window
        runner = Mock(is_paused=True, running=True, quality_held=False, best={'score': 2})
        w.worker = Mock(runner=runner)
        def clicked(box):
            return next(b for b in box.buttons() if b.text() == 'Restore initial and End')
        try:
            with patch.object(QMessageBox, 'exec', return_value=0), patch.object(QMessageBox, 'clickedButton', clicked):
                w.show_pause_choices()
            runner.restore_initial_and_end.assert_called_once()
            runner.resume.assert_not_called()
            runner.save_and_end.assert_not_called()
        finally:
            w.worker = None

    def test_progressive_configuration_and_ten_percent_default(self):
        w = self.window
        self.assertEqual(w.config().min_transmission_fraction, .10)
        self.assertEqual(w.config().initial_radius_steps, 1)
        self.assertEqual(w.config().max_step_steps, 1)
        w.ratio_floor.setValue(15)
        w.expand_points.setValue(60)
        w.expand_coverage.setValue(3)
        self.assertEqual(w.config().min_transmission_fraction, .15)
        self.assertEqual(w.config().expansion_points, 60)
        self.assertEqual(w.config().expansion_coverage, 3)

    def test_quality_hold_close_never_resumes_or_writes_best(self):
        w = self.window
        runner = Mock(is_paused=True, quality_held=True, running=True)
        w.worker = Mock(runner=runner)
        with patch.object(QMessageBox, 'exec', return_value=0), patch.object(QMessageBox, 'clickedButton', return_value=None):
            w.show_pause_choices()
        runner.resume.assert_not_called()
        runner.save_and_end.assert_not_called()
        runner.stop.assert_not_called()
        w.worker = None

    def test_quality_recheck_requires_explicit_button(self):
        w = self.window
        runner = Mock(is_paused=True, quality_held=True, running=True)
        w.worker = Mock(runner=runner)
        original = QMessageBox.addButton
        buttons = {}
        def add(box, text, role):
            button = original(box, text, role)
            buttons[text] = button
            return button
        with patch.object(QMessageBox, 'addButton', add), patch.object(QMessageBox, 'exec', return_value=0), \
             patch.object(QMessageBox, 'clickedButton', side_effect=lambda: buttons['Recheck after recovery']):
            w.show_pause_choices()
        runner.resume.assert_called_once()
        runner.save_and_end.assert_not_called()
        self.assertEqual(w.pause_button.text(), 'PAUSE')
        w.worker = None

    def test_pause_dialog_save_and_end_selects_best_action(self):
        w = self.window
        runner = Mock(is_paused=True, running=True, best={'score': 2})
        w.worker = Mock(runner=runner)
        original = QMessageBox.addButton
        buttons = {}
        def add(box, text, role):
            b = original(box, text, role)
            buttons[text] = b
            return b
        with patch.object(QMessageBox, 'addButton', add), patch.object(QMessageBox, 'exec', return_value=0), \
             patch.object(QMessageBox, 'clickedButton', side_effect=lambda: buttons['Save and End']):
            w.show_pause_choices()
        runner.save_and_end.assert_called_once()
        runner.resume.assert_not_called()
        w.worker = None


if __name__ == '__main__':
    unittest.main()
