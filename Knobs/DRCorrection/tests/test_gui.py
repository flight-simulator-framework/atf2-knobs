"""Offscreen Qt workflows; no EPICS connections."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
import time
from pathlib import Path
import unittest
from PyQt6.QtWidgets import QApplication
from Knobs.DRCorrection.gui import DRCorrectionWindow
from Knobs.DRCorrection.dialogs import BufferDialog,SRFitDialog


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


if __name__=='__main__':unittest.main()
