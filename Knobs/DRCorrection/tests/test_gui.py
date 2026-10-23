"""Offscreen Qt workflows; no EPICS connections."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
import time
from pathlib import Path
import unittest
import numpy as np
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
    def test_ramp_worker_messages_reach_log_and_callback_is_restored(self):
        from Knobs.DRCorrection.ramp import DeltaRamp
        ramp=DeltaRamp(None,None,{})
        previous=ramp.report
        self.window.backend.ramp=ramp
        for fail in (False,True):
            def action(progress):
                ramp.trace('command RAMP:PL1:ONOFF_SW=1')
                if fail:raise RuntimeError('test failure')
            self.window.start(action,lambda result:None)
            self.wait()
            self.assertIs(ramp.report,previous)
        text=self.window.messages.toPlainText()
        self.assertEqual(text.count('DeltaFRamp: command RAMP:PL1:ONOFF_SW=1'),2)
        self.assertIn('test failure',text)
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
    def test_plane_probe_targets_and_cached_responses_survive_option_changes(self):
        for mode in ('orbit','dispersion'):
            panel=self.window.panels[mode]
            panel.set_option('adaptive_target_mm',.03)
            panel.calculate();self.wait();vertical=panel.data['response'].copy()
            panel.set_option('plane','x');self.assertEqual(panel.options.adaptive_target_mm,.1)
            panel.set_option('adaptive_target_mm',.2)
            panel.calculate();self.wait()
            panel.set_option('plane','y');self.assertEqual(panel.options.adaptive_target_mm,.03)
            self.assertTrue(panel.recalculate_button.isEnabled())
            panel.set_option('count_v',3);reads=self.window.backend.acquisitions
            panel.recalculate_button.click();self.wait()
            self.assertEqual(self.window.backend.acquisitions,reads)
            np.testing.assert_array_equal(panel.data['response'],vertical)
            self.assertTrue(panel.data['reused_response'])
            panel.set_option('plane','x');self.assertEqual(panel.options.adaptive_target_mm,.2)
            self.assertTrue(panel.recalculate_button.isEnabled())
    def test_plot_masks_excluded_predictions_but_preserves_large_valid_values(self):
        from Knobs.DRCorrection.core import Options
        panel=self.window.panels['orbit']
        data=self.window.service.calculate('orbit',Options())
        data['valid'][0]=False
        data['full_measured'][:,0]=2000.
        data['full_predicted'][:,0]=3000.
        panel.plot.correction(data)
        for ax in panel.plot.figure.axes:
            self.assertLess(ax.get_ylim()[1],100.)
            prediction=next(line for line in ax.lines if line.get_label()=='Predicted after SET')
            self.assertTrue(np.isnan(prediction.get_ydata()[0]))
        self.assertIn('SET not applied',panel.plot.figure._suptitle.get_text())
        self.assertIn('95/96',panel.plot.figure._suptitle.get_text())
        self.assertEqual(panel.plot.figure.axes[1].get_ylabel(),'COD Y [mm]')
        self.assertTrue(any(line.get_label()=='Target (fit plane)' for line in panel.plot.figure.axes[1].lines))
        data['full_predicted'][1,1]=3000.
        panel.plot.correction(data)
        self.assertGreater(panel.plot.figure.axes[1].get_ylim()[1],3000.)
        # Stored/exportable arrays are never clipped by plotting.
        self.assertEqual(data['full_predicted'][1,0],3000.)
        data['applied']=True;data['full_after']=data['full_measured'].copy()
        data['after_valid']=data['valid'].copy();data['after_valid'][1]=False
        panel.plot.correction(data)
        after=next(line for line in panel.plot.figure.axes[1].lines if line.get_label()=='Measured after SET')
        self.assertTrue(np.isnan(after.get_ydata()[1]))
    def test_skipped_magnets_visible_and_unverified_restoration_blocks_set(self):
        from Knobs.DRCorrection.core import Options
        panel=self.window.panels['orbit']
        data=self.window.service.calculate('orbit',Options())
        data['scan_errors']=[dict(magnet='ZV_BAD',stage='response probe',error='readback timeout',
            restoration='not verified',restored=False)]
        data['set_blocked_reason']='Probe restoration unverified; CALC again'
        panel.display(data)
        self.assertEqual(panel.scan_error_table.rowCount(),1)
        self.assertEqual(panel.scan_error_table.item(0,0).text(),'ZV_BAD')
        self.assertIn('1 magnet errors',panel.proposal_summary.text())
        self.assertIn('SET blocked',panel.proposal_summary.text())
        self.assertFalse(panel.form.pushButtonSet.isEnabled())
        self.assertIn('ZV_BAD',self.window.messages.toPlainText())
        panel.invalidate();self.assertEqual(panel.scan_error_table.rowCount(),0)

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
