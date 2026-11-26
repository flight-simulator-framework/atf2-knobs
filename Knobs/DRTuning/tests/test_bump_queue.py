"""Bump queue sequencing, bounded recovery and per-target exports."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from Knobs.DRCorrection.core import Options
from Knobs.DRTuning.coupling import BumpLeakage, BumpOptions, CouplingService
from Knobs.DRTuning.epics import LiveBackend
from Knobs.DRTuning.gui import TuningSession
from Knobs.DRTuning.tests.test_bump_dispersion import BumpFixture, DispersionFixture
from Knobs.DRTuning.tests.test_bump_presets import PresetBackend


class RecoveryTests(unittest.TestCase):
    def test_discovery_replaces_sparse_first_shot_with_internal_bpms_still_valid(self):
        backend = PresetBackend()
        cancel = threading.Event()
        service = CouplingService(backend,cancel)
        orbit = backend.orbit
        def first_shot_sparse():
            shot = orbit()
            if backend.acquisitions==1:shot['valid'][10:30]=False
            return shot
        with patch.object(backend,'orbit',side_effect=first_shot_sparse), patch.object(cancel,'wait',return_value=False) as wait:
            _,valid=service.orbit(BumpOptions(),required=[0,1])
        wait.assert_called_once_with(3.)
        np.testing.assert_array_equal(valid,backend.valid)
        self.assertEqual(backend.acquisitions,11)

    def test_truncated_live_waveform_is_retried_after_three_seconds(self):
        backend = object.__new__(LiveBackend)
        backend.profile = {'orbit': dict(packed_pv='bpm',row_width=4,row_indices=[0],
            columns=dict(status=0,x=1,y=2,tmit=3),valid_status=1,mm_per_unit=1.)}
        backend.bpm_names = ['MB1R']
        backend.acquisitions = 0
        stamps = iter(range(20))
        def read(_):
            stamp = next(stamps)
            return dict(value=[] if stamp==1 else [1,2.,3.+(stamp%2)*.001,1],timestamp=stamp)
        backend.metadata = read
        cancel = threading.Event()
        with patch.object(cancel,'wait',return_value=False) as wait:
            orbit, valid = CouplingService(backend,cancel).orbit(BumpOptions(),required=[0])
        self.assertIn(((3.,),{}), wait.call_args_list)
        self.assertEqual(backend.acquisitions,10)
        np.testing.assert_allclose(orbit,[2.,3.0005])
        self.assertTrue(valid.all())

    def test_many_missing_waits_three_seconds_before_next_sample(self):
        backend = BumpFixture()
        cancel = threading.Event()
        service = CouplingService(backend, cancel)
        orbit = backend.orbit
        def dropout():
            row = orbit()
            if backend.acquisitions == 4:
                row['valid'][:6] = False
            return row
        with patch.object(backend, 'orbit', side_effect=dropout), patch.object(cancel, 'wait', return_value=False) as wait:
            _, valid = service.orbit(BumpOptions(), required=range(8))
        wait.assert_called_once_with(3.)
        self.assertTrue(valid.all())
        self.assertEqual(backend.acquisitions, 11)
        self.assertFalse(backend.commands)

    def test_xsr_replaces_outlier_invalid_and_timeout(self):
        backend = object.__new__(LiveBackend)
        backend.profile = {}
        def records(values):
            for stamp, value in enumerate(values):
                yield {'XSR:PROJ_V_SIGMA': dict(value=value, timestamp=stamp)}
        def timeout():
            raise TimeoutError('stale XSR')
            yield
        with patch('Knobs.DRTuning.fresh.updates', side_effect=[timeout(), records([1e4, float('nan')] + [12.]*10)]):
            result = backend.sample_xsr(threading.Event())
        np.testing.assert_array_equal(result['values'], [12.]*10)
        self.assertEqual(result['rejected_or_timed_out'], 3)

    def test_scan_resume_retains_completed_xsr_points(self):
        backend = BumpFixture()
        service = CouplingService(backend, threading.Event())
        opt = BumpOptions(settle_s=0.)
        calibration = service.calibrate(backend.geometry(), opt)
        read = backend.sample_xsr
        calls = []
        def intermittent(*args, **kwargs):
            calls.append(backend.vector()[8])
            if len(calls) == 4:
                raise TimeoutError('XSR stopped')
            return read(*args, **kwargs)
        with patch.object(backend, 'sample_xsr', side_effect=intermittent):
            with self.assertRaises(TimeoutError):
                service.scan(calibration, opt)
            saved = service.last_result
            self.assertTrue(saved['restored'])
            self.assertEqual(len(saved['points']), 3)
            result = service.scan(calibration, opt, resume=saved)
        self.assertTrue(result['applied'])
        self.assertEqual(len(calls), 7)
        self.assertAlmostEqual(calls[3], calls[4])

    def test_scan_preserves_xsr_through_two_helpers_and_interrupted_calibration(self):
        backend=PresetBackend()
        service=CouplingService(backend,threading.Event())
        opt=BumpOptions(settle_s=0.)
        g=service.calibrate_target('SF1R.2',opt,Options())
        def vector():
            orbit=backend.response@backend.read('correctors',backend.hcorrectors+backend.vcorrectors)
            return np.r_[np.zeros(8),(g['reconstruction']@orbit)[2]]
        backend.vector=vector
        close=service.close_at
        probe=service.probe_column
        fault=[True]
        def flaky_probe(n,*args,**kwargs):
            if n not in g['actuators'] and fault:
                fault.pop()
                raise TimeoutError('helper probe interrupted')
            return probe(n,*args,**kwargs)
        def leak(geometry,*args):
            if (len(geometry['actuators']),read.call_count) in ((8,3),(9,5)):
                outside=np.zeros(len(g['outside_rows']))
                outside[:len(outside)//2]=np.linspace(.03,.08,len(outside)//2)
                raise BumpLeakage(dict(outside=outside,outside_max_mm=.08,outside_rms_mm=.05))
            return close(geometry,*args)
        with patch.object(backend,'sample_xsr',wraps=backend.sample_xsr) as read, \
             patch.object(service,'close_at',side_effect=leak),patch.object(service,'probe_column',side_effect=flaky_probe):
            with self.assertRaisesRegex(TimeoutError,'helper probe interrupted'):
                service.scan(g,opt)
            partial=service.last_result
            self.assertTrue(partial['restored'])
            self.assertEqual(len(partial['pending_geometry']['actuators']),9)
            self.assertEqual(len(partial['points']),3)
            first=partial['points'][0]
            result=service.scan(g,opt,resume=partial)
        self.assertEqual(read.call_count,6)  # 5 coarse and the verified optimum
        self.assertEqual([len(p['actuators']) for p in result['points']],[8]*3+[9]*2+[10])
        self.assertIs(result['points'][0],first)
        self.assertEqual(result['started_unix_s'],partial['started_unix_s'])
        self.assertTrue(result['applied'])
        self.assertAlmostEqual(result['final_amplitude_mm'],backend.minimum,places=4)

    def test_extra_columns_measured_and_fixed_magnets_excluded(self):
        backend = PresetBackend()
        service = CouplingService(backend, threading.Event())
        opt = BumpOptions(settle_s=0.)
        original = service.prepare('SF1R.2', opt, Options(fixed_actuators=('ZV18R',)))
        saved = backend.snapshot()
        n = len(original['outside_rows'])
        loss = BumpLeakage(dict(outside=np.random.default_rng(27).normal(size=n),
                                outside_max_mm=.06, outside_rms_mm=.04))
        extra = service.extend_geometry(original, opt, loss, lambda _:None)
        extra = service.extend_geometry(extra, opt, loss, lambda _:None)
        self.assertEqual(len(set(extra['actuators'])), 10)
        self.assertNotIn('ZV18R', extra['closure_helpers'])
        self.assertIsNone(service.extend_geometry(extra, opt, loss, lambda _:None))
        g = service.calibrate(extra, opt)
        self.assertTrue(g['calibrated'])
        self.assertEqual(set(g['actuators']), {r['magnet'] for r in g['probe_readbacks']})
        self.assertEqual(backend.snapshot(), saved)

    def test_failed_closure_automatically_recalibrates_with_extra_column(self):
        backend = PresetBackend()
        service = CouplingService(backend, threading.Event())
        opt = BumpOptions(settle_s=0.)
        baseline = backend.snapshot()
        close = service.close_at
        def first_set_leaks(g, response, knob, orbit, amplitude, options, event):
            observed = close(g, response, knob, orbit, amplitude, options, event)
            if len(g['actuators']) == 8:
                leakage = np.linspace(-.08,.08,len(observed['outside']))
                raise BumpLeakage(dict(outside=leakage,outside_max_mm=.08,outside_rms_mm=.05))
            return observed
        with patch.object(service,'close_at',side_effect=first_set_leaks):
            g = service.calibrate_target('SF1R.2', opt, Options())
        self.assertEqual(len(g['closure_helpers']), 1)
        self.assertTrue(g['calibrated'])
        probes = [names[0] for _, names, values in backend.commands if len(names)==1 and values[0]!=0]
        self.assertCountEqual(probes, g['actuators']*2)
        self.assertEqual(backend.snapshot(), baseline)


class QueueGuiTests(unittest.TestCase):
    def test_operator_pause_restores_bump_and_resumes_at_interrupted_xsr_point(self):
        app=QApplication.instance() or QApplication([])
        backend=BumpFixture()
        baseline=backend.snapshot()
        window=TuningSession(backend)
        panel=window.coupling_panel
        panel.rank_started=True
        panel.settle.setValue(0.)
        panel.calibration=panel.service.calibrate(backend.geometry(),panel.options())
        waiting=threading.Event()
        amplitudes=[]
        read=backend.sample_xsr
        def interrupt_second_point(cancel,count,callback):
            amplitudes.append(backend.vector()[8])
            if len(amplitudes)==2:
                waiting.set()
                if not cancel.wait(8):raise AssertionError('Pause did not interrupt XSR')
            return read(cancel,count,callback)
        paused=[]
        def popup(message,resume):
            paused.append((message,backend.snapshot(),len(panel.scan_resume['points'])))
            resume()
        def wait_for(condition):
            deadline=time.monotonic()+12
            while not condition():
                app.processEvents()
                self.assertLess(time.monotonic(),deadline,window.operation_status.text())
                time.sleep(.005)
        try:
            with tempfile.TemporaryDirectory() as folder,patch.object(panel,'recovery',side_effect=popup), \
                 patch.object(backend,'sample_xsr',side_effect=interrupt_second_point):
                panel.tempfiles_directory=Path(folder)/'tempfiles'
                panel.run_data_directory=Path(folder)/'data'
                window.show();window.open_tool('coupling')
                panel.scan()
                wait_for(waiting.is_set)
                self.assertTrue(window.pause_button.isVisible() and window.pause_button.isEnabled())
                window.pause_button.click()
                wait_for(lambda: panel.completed and not window.task.isRunning())
                self.assertEqual(paused[0][1:],(baseline,1))
                self.assertIn('Operator pause',paused[0][0])
                self.assertEqual(len(paused),1)
                self.assertAlmostEqual(amplitudes[1],amplitudes[2])
                self.assertEqual(sum(np.isclose(amplitudes,amplitudes[0])),1)
                self.assertTrue(panel.completed[0]['result']['applied'])
        finally:
            window.service.cancel.set()
            if window.task:window.task.wait(5000)
            window.close();app.processEvents()

    def test_preset_current_limit_skips_before_writing_without_helpers(self):
        from Knobs.DRTuning.bump_presets import bump_preset
        app=QApplication.instance() or QApplication([])
        backend=PresetBackend()
        target='SF1R.2'
        backend.currents[bump_preset(target)['actuators'][0]]=5.
        window=TuningSession(backend);panel=window.coupling_panel
        try:
            with tempfile.TemporaryDirectory() as tmp, patch.object(window,'start',side_effect=lambda work,done,label,**kwargs:done(work(lambda *_:None))), \
                 patch.object(panel,'after_worker',side_effect=lambda run:run()),patch.object(panel,'recovery') as popup:
                panel.run_data_directory=Path(tmp)
                panel.target.clear();panel.target.addItem(target,target)
                panel.queue=[target];panel.running_queue=True
                panel.next_target()
                popup.assert_not_called()
                self.assertEqual(backend.commands,[])
                self.assertFalse(panel.running_queue)
                record,=panel.skipped
                self.assertEqual(record['target'],target)
                self.assertTrue(record['baseline_verified'])
                self.assertIn('current margin',record['reason'])
        finally:window.close();app.processEvents()

    def test_default_fixed_magnets_can_be_reenabled_from_shared_selection(self):
        from Knobs.DRCorrection.selection import SelectionDialog
        app=QApplication.instance() or QApplication([])
        backend=PresetBackend()
        window=TuningSession(backend)
        try:
            selection=SelectionDialog(window)
            excluded={'ZV16R','ZH15R'}
            self.assertEqual(set(selection.magnets.excluded()),excluded)
            panel=window.coupling_panel
            self.assertEqual(set(panel.model_options().fixed_actuators),excluded)
            for item in selection.magnets.rows():
                if item.text()=='ZH15R':item.setCheckState(Qt.CheckState.Checked)
            selection.apply_to(window)
            self.assertEqual(panel.model_options().fixed_actuators,('ZV16R',))
        finally:window.close();app.processEvents()

    def test_exhausted_closure_saves_points_and_runs_next_checked_target(self):
        app=QApplication.instance() or QApplication([])
        backend=DispersionFixture()
        window=TuningSession(backend);panel=window.coupling_panel
        targets=['SF1R.2','SD1R.16']
        def calibrated(target,*args):
            return dict(target=target,actuators=['H1','H2','V1'],centers=[0.]*3,
                        closure_helpers=['H2','V1'],knob_A_per_mm=[1.]*3,
                        baseline=backend.snapshot(),bpm_names=list(backend.bpm_names))
        def scan(g,*args,**kwargs):
            point=dict(amplitude_mm=.1,xsr=dict(mean=12.,sem=.01,count=10,values=[12.]*10))
            if g['target']==targets[0]:
                panel.service.last_result=dict(calibration=g,restored=True,applied=False,points=[point])
                raise BumpLeakage(dict(outside=np.array([.08,0.]),outside_max_mm=.08,outside_rms_mm=.08))
            return dict(calibration=g,applied=True,points=[point],fits=[],final=point,final_amplitude_mm=.1)
        try:
            with tempfile.TemporaryDirectory() as tmp, patch.object(panel.service,'calibrate_target',side_effect=calibrated), \
                 patch.object(panel.service,'scan',side_effect=scan) as scans,patch.object(panel,'recovery') as popup:
                panel.run_data_directory=panel.tempfiles_directory=Path(tmp)
                panel.target.clear()
                for t in targets:panel.target.addItem(t,t)
                panel.queue=targets.copy();panel.running_queue=True
                panel.next_target()
                deadline=time.monotonic()+12.
                while panel.running_queue and time.monotonic()<deadline:
                    app.processEvents();time.sleep(.005)
                self.assertFalse(panel.running_queue)
                popup.assert_not_called()
                self.assertEqual(scans.call_count,2)
                self.assertEqual([r['target'] for r in panel.completed],targets[1:])
                self.assertEqual([r['target'] for r in panel.skipped],targets[:1])
                path,=Path(tmp).glob('*/bump_skipped_*.json')
                saved=json.loads(path.read_text())
                self.assertTrue(saved['baseline_verified']);self.assertEqual(len(saved['result']['points']),1)
                self.assertIn('Outside COD',saved['reason'])
        finally:
            if window.task:window.task.wait(10000)
            window.close();app.processEvents()

    def test_queue_pauses_when_exhausted_but_restoration_is_unverified(self):
        app=QApplication.instance() or QApplication([])
        b=DispersionFixture();window=TuningSession(b);panel=window.coupling_panel
        target='SD1R.2';panel.queue=[target];panel.running_queue=True
        g=dict(target=target,actuators=['H0','V0'],closure_helpers=['H0','V0'],
               baseline=b.snapshot(),bpm_names=list(b.bpm_names))
        def interrupted(_):
            panel.service.last_result=dict(calibration=g,restored=True,points=[])
            raise BumpLeakage(dict(outside=np.array([.06,0.]),outside_max_mm=.06,outside_rms_mm=.06))
        try:
            with patch.object(window,'start',side_effect=lambda work,done,label,**kwargs:done(work(None))), \
                 patch.object(panel.service,'verify_geometry',side_effect=ValueError('readback mismatch')), \
                 patch.object(panel,'after_worker',side_effect=lambda f:f()),patch.object(panel,'recovery') as popup:
                panel.run(interrupted,lambda _:self.fail('unexpected completion'),'scan')
                self.assertEqual(panel.queue,[target]);self.assertFalse(panel.skipped)
                self.assertIn('Baseline verification failed',popup.call_args.args[0])
        finally:window.close();app.processEvents()

    def test_save_and_end_archives_partial_and_pending_targets(self):
        from datetime import date
        app = QApplication.instance() or QApplication([])
        window = TuningSession(DispersionFixture())
        panel = window.coupling_panel
        try:
            with tempfile.TemporaryDirectory() as root, patch.object(QMessageBox,'exec'), patch.object(QMessageBox,'clickedButton',lambda box:next(b for b in box.buttons() if b.text()=='Save and end')):
                panel.run_data_directory=Path(root)
                panel.queue=['SD1R.2'];panel.result={'restored':True,'points':[{'amplitude_mm':.1}]}
                panel.completed=[dict(target='SF1R.1',result={'applied':True})]
                panel.recovery('XSR timeout', lambda:self.fail('Unexpected resume'))
                filename,=(Path(root)/str(date.today().year)).glob('bump_*.json')
                data=json.loads(filename.read_text())
                self.assertEqual(data['pending'],['SD1R.2'])
                self.assertTrue(data['partial']['restored'])
                self.assertTrue(data['completed'][0]['result']['applied'])
                self.assertEqual(panel.queue,[])
        finally:
            window.close();app.processEvents()

    def test_queue_ranks_checks_resumes_and_exports_each_target(self):
        app = QApplication.instance() or QApplication([])
        backend = DispersionFixture()
        backend.unit = lambda _: 'A'
        window = TuningSession(backend)
        panel = window.coupling_panel
        def wait_for(predicate):
            deadline = time.monotonic() + 15
            while not predicate():
                app.processEvents()
                if time.monotonic() > deadline:
                    self.fail(window.messages.toPlainText())
                time.sleep(.005)
        try:
            def calibrate(target, *args):
                return dict(target=target, actuators=['ZV1R'], centers=[0.], knob_A_per_mm=[1.])
            attempts = []
            def scan(g, *args, resume=None):
                target = g['target']
                attempts.append(target)
                if len(attempts) == 2:
                    panel.service.last_result = dict(schema='dr-coupling-bump-1',calibration=g,restored=True,points=[])
                    raise TimeoutError('test interruption')
                if len(attempts) == 3:self.assertIsNotNone(resume)
                p = dict(amplitude_mm=.1, xsr=dict(mean=12.,sem=.01,count=10,values=[12.]*10))
                return dict(calibration=g,points=[p],fits=[],final=p,final_amplitude_mm=.1,final_currents_A=[.1])
            with tempfile.TemporaryDirectory() as root, patch.object(panel.service,'calibrate_target',side_effect=calibrate), patch.object(panel.service,'scan',side_effect=scan), patch.object(QMessageBox,'exec'), patch.object(QMessageBox,'clickedButton',lambda box:next(b for b in box.buttons() if b.text()=='Resume')):
                panel.tempfiles_directory=Path(root)
                window.show()
                window.open_tool('coupling')
                wait_for(lambda: panel.queue_list.count() and window.tabs.isEnabled())
                selected = panel.checked_targets()
                self.assertEqual(len(selected),5)
                for i in range(2,5):
                    panel.queue_list.item(i).setCheckState(Qt.CheckState.Unchecked)
                panel.queue_button.click()
                wait_for(lambda: len(panel.completed)==2 and not panel.running_queue)
                self.assertEqual(attempts,[selected[0],selected[1],selected[1]])
                self.assertEqual(len(list(Path(root).glob('bump_*.png'))),2)
                self.assertIn(selected[1],panel.plot.figure.axes[0].get_title())
        finally:
            if window.task:window.task.wait(10000)
            window.close();app.processEvents()
