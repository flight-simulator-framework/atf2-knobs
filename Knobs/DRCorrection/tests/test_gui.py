"""Offscreen Qt workflows; no EPICS connections."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
import time
from pathlib import Path
import unittest
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from Knobs.DRCorrection.gui import DRCorrectionWindow
from Knobs.DRCorrection.dialogs import BufferDialog,SRFitDialog
from Knobs.DRCorrection.selection import SelectionDialog
from Knobs.DRCorrection.backends import DemoBackend


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):self.window=DRCorrectionWindow();self.window.show();self.app.processEvents()
    def tearDown(self):self.wait();self.window.close();self.app.processEvents()
    def wait(self):
        deadline=time.monotonic()+30
        while self.window.task is not None and self.window.task.isRunning():
            self.app.processEvents();time.sleep(.01)
            if time.monotonic()>deadline:raise AssertionError('Qt task timed out')
        self.app.processEvents()
    def test_live_sampling_defaults_and_editable_controls(self):
        from Knobs.DRTuning.presentation import reflow_correction
        backend=DemoBackend();backend.identity='epics:offline-ui-test'
        window=DRCorrectionWindow(backend)
        try:
            for panel in window.panels.values():
                reflow_correction(panel)
                self.assertEqual(panel.options.bpm_samples,3)
                self.assertEqual(panel.options.bpm_interval_s,1.)
                panel.modern_fields['bpm_samples'].setValue(5)
                panel.modern_fields['bpm_interval_s'].setValue(.5)
                self.assertEqual(panel.options.bpm_samples,5)
                self.assertEqual(panel.options.bpm_interval_s,.5)
            self.assertEqual(backend.acquisitions,0)
        finally:window.close()
    def test_calc_set_save_reset_all_panels(self):
        with tempfile.TemporaryDirectory() as root:
            baseline=self.window.backend.snapshot()
            for i,(mode,panel) in enumerate(self.window.panels.items()):
                self.window.tabs.setCurrentIndex(i)
                prefix='pushButtonDispersion' if mode=='dispersion' else 'pushButton'
                getattr(panel.form,prefix+'Calc').click();self.wait()
                self.assertIsNotNone(panel.data,self.window.messages.toPlainText())
                self.assertEqual(len(panel.plot.figure.axes),4 if mode=='dispersion' else 2)
                getattr(panel.form,prefix+'Set').click();self.wait();self.assertTrue(panel.data['applied'])
                panel.save_bundle(root)
                for ext in ('json','csv','png','pdf'):self.assertGreater((Path(root)/(mode+'.'+ext)).stat().st_size,100)
                getattr(panel.form,prefix+'Reset').click();self.wait();self.assertEqual(baseline,self.window.backend.snapshot())
            self.assertNotIn('ERROR:',self.window.messages.toPlainText())
    def test_buffers_sr_and_tbt_workflows(self):
        with tempfile.TemporaryDirectory() as root:
            buffer=BufferDialog(self.window);buffer.show();buffer.capture();self.wait()
            buffer.slot.setCurrentText('B');buffer.capture();self.wait();buffer.subtract();buffer.save(root)
            self.assertTrue((Path(root)/'orbit_buffer.csv').exists())
            panel=self.window.panels['dispersion'];panel.calculate();self.wait()
            sr=SRFitDialog(self.window,panel.data);sr.calculate();self.assertIsNotNone(sr.result);sr.save(root)
            self.assertTrue((Path(root)/'sr_fit.pdf').exists())
            tbt=self.window.tbt;tbt.turns.setValue(128);tbt.acquire();self.wait();tbt.analyze();tbt.save(root)
            self.assertTrue(tbt.estimate['planes']['x']['valid']);self.assertTrue((Path(root)/'tbt.npz').exists())
            buffer.close();sr.close();self.assertNotIn('ERROR:',self.window.messages.toPlainText())

    def test_selection_filter_presets_and_one_magnet_calc(self):
        from Knobs.DRTuning.presentation import reflow_correction
        panel=self.window.panels['dispersion'];reflow_correction(panel)
        self.assertNotIn('momentum_compaction',panel.modern_fields)
        self.assertIn('0.0021',panel.compaction_label.text())
        dialog=SelectionDialog(self.window);dialog.show();self.app.processEvents()
        dialog.presets['V only'].click()
        b=self.window.backend
        checked=lambda group:[i.text() for i in group.rows() if i.checkState()==Qt.CheckState.Checked]
        self.assertEqual(checked(dialog.magnets),b.vcorrectors)
        target=b.vcorrectors[0]
        dialog.magnets.search.setText(target)
        self.assertEqual(checked(dialog.magnets),b.vcorrectors)  # Filter is not selection.
        self.assertIn('11 checked outside filter',dialog.magnets.count.text())
        row=next(i for i in dialog.magnets.rows() if i.text()==target)
        row.setSelected(True);dialog.magnets.actions['Only highlighted'].click()
        self.assertEqual(checked(dialog.magnets),[target])
        dialog.bpms.search.setText('MB1R');dialog.bpms.actions['Uncheck matches'].click()
        self.assertEqual(dialog.bpms.excluded(),(b.bpm_names[0],))
        dialog.apply_to(self.window);dialog.close()
        self.assertIn('scan 1 magnets',panel.selection_label.text())
        self.assertIn('RF scan pairs: 3',panel.selection_label.text())
        panel.calculate();self.wait()
        self.assertIsNotNone(panel.data,self.window.messages.toPlainText())
        self.assertEqual(panel.data['actuators'],[target])
        self.assertFalse(panel.data['valid'][0])
        self.assertEqual(b.acquisitions,9)

    def test_selection_cancel_all_none_and_match_scope(self):
        before=self.window.backend.snapshot()
        dialog=SelectionDialog(self.window)
        dialog.magnets.actions['None'].click()
        self.assertEqual(len(dialog.magnets.excluded()),dialog.magnets.items.count())
        dialog.magnets.search.setText('zv')
        dialog.magnets.actions['Check matches'].click()
        self.assertEqual(set(dialog.magnets.excluded()),set(self.window.backend.hcorrectors+self.window.backend.skews))
        dialog.magnets.actions['All'].click()
        self.assertEqual(dialog.magnets.excluded(),())
        for title,family in [('SD only','SD'),('SF only','SF')]:
            dialog.presets[title].click()
            checked=[i.text() for i in dialog.magnets.rows() if i.checkState()==Qt.CheckState.Checked]
            self.assertTrue(checked);self.assertTrue(all(n.startswith(family) for n in checked))
        dialog.reject()
        for panel in self.window.panels.values():self.assertEqual(panel.options.fixed_actuators,())
        self.assertEqual(before,self.window.backend.snapshot())


if __name__=='__main__':unittest.main()
