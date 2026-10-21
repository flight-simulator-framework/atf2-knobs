import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
import numpy as np
from Knobs.DRTuning.optics import fit_qmod,phase_beta,QModService
from Knobs.DRTuning.acquisition import TBTController
from Knobs.DRTuning.sr_beta import fit_sr_beta
from Knobs.DRCorrection.backends import DemoBackend


class QuadFixture(DemoBackend):
    def __init__(self):
        super().__init__();self.q={'QF1R.1':1.,'QF2R.1':1.,'QM1R.1':1.,'QM1R.2':1.}
        self.quadrupoles={n:dict(family=f) for n,f in zip(self.q,['QF1R','QF2R','QMTRIM1','QMTRIM2'])}
        self.active='QF1R.1';self.fail=False;self.commands=[]
    def quadrupole_current(self,n):return self.q[n]
    def quadrupole_k1l(self,n,value):return value*.01
    def validate_quadrupole_targets(self,n,v):
        if not np.isfinite(v).all() or np.max(abs(np.asarray(v)))>2:raise ValueError('limit')
    def set_quadrupole_current(self,n,v):
        self.validate_quadrupole_targets(n,[v]);self.q[n]=v;self.active=n;self.commands.append((n,v))
    def measure_tunes(self,cancel=None):
        if self.fail:raise RuntimeError('detector failed')
        k=self.quadrupole_k1l(self.active,self.q[self.active]);return dict(tunes=[.2+8*k/(4*np.pi),.4-3*k/(4*np.pi)])


class OpticsTests(unittest.TestCase):
    def test_qmod_fit_and_covariance(self):
        k=np.linspace(-.001,.001,5);q=np.column_stack([.2+8*k/(4*np.pi),.4-3*k/(4*np.pi)])
        r=fit_qmod(k,q);np.testing.assert_allclose(r['beta_m'],[8,3],rtol=1e-9)
        q[-1]=np.nan;r=fit_qmod(k,q,[True,True,True,True,False]);np.testing.assert_allclose(r['beta_m'],[8,3])
        with self.assertRaises(ValueError):fit_qmod(np.zeros(5),np.zeros((5,2)))
        with self.assertRaises(ValueError):fit_qmod(k,q)
    def test_scan_restores_and_limits_precede_writes(self):
        b=QuadFixture();service=QModService(b,threading.Event());r=service.scan('QF1R.1',[.8,1,1.2])
        self.assertTrue(r['restored']);self.assertEqual(b.q['QF1R.1'],1.);np.testing.assert_allclose(r['fit']['beta_m'],[8,3])
        b.commands=[]
        with self.assertRaises(ValueError):service.scan('QF1R.1',[1,2,3])
        self.assertEqual(b.commands,[])
    def test_scan_restores_on_failure_and_cancel(self):
        b=QuadFixture();b.fail=True;cancel=threading.Event();service=QModService(b,cancel)
        with self.assertRaises(RuntimeError):service.scan('QF1R.1',[.8,1,1.2])
        self.assertEqual(b.q['QF1R.1'],1.)
        b.fail=False
        with self.assertRaisesRegex(RuntimeError,'cancelled'):
            service.scan('QF1R.1',[.8,1,1.2],lambda *_:cancel.set())
        self.assertEqual(b.q['QF1R.1'],1.)
    def test_phase_beta_independent_of_amplitude(self):
        beta=7.;alpha=.4;phi=np.array([.2,.8,1.5]);cot=1/np.tan(phi[1:]-phi[0])
        cal=dict(schema='dr-phase-optics-1',source='independent test',planes={'x':[dict(names=['a','b','c'],m11_m12=((cot+alpha)/beta).tolist(),phase_sign=1)]})
        est=dict(names=['a','b','c'],planes=dict(x=dict(valid=True,phase_rad=phi,valid_bpms=[True]*3),y=dict(valid=False)))
        r=phase_beta(est,cal);self.assertAlmostEqual(r['planes']['x'][0]['beta_m'],beta)
        est['planes']['x']['valid_bpms'][1]=False
        self.assertFalse(phase_beta(est,cal)['planes']['x'][0]['valid'])
    def test_sr_fit_recovers_monitor_beta(self):
        names=['a','b','c','d'];drifts=np.array([0.,1.,2.,3.]);beta=4.;alpha=.2;gamma=(1+alpha*alpha)/beta
        measured=beta-2*alpha*drifts+gamma*drifts**2
        c=dict(schema='dr-sr-beta-1',source='independent test',bpms=[dict(name=n,r11=1.,r12_m=d) for n,d in zip(names,drifts)],monitors=[dict(name='SR',r11=1.,r12_m=1.5)])
        r=fit_sr_beta(names,measured,c);self.assertAlmostEqual(r['monitors'][0]['beta_m'],beta-3*alpha+2.25*gamma,places=6)
        c['bpms']=[dict(name=n,r11=1.,r12_m=0.) for n in names]
        with self.assertRaisesRegex(ValueError,'Degenerate'):fit_sr_beta(names,measured,c)


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.values=dict(mode='Repetitive 3Hz',shot=0,turn=1,generation=12);self.commands=[]
        self.config=dict(completion_confirmed=True,generation_pv='generation',mode_pv='mode',shot_mode_pv='shot',turn_pv='turn',snap_pv='snap',timeout_s=.03,arm_settle_s=0.)
        self.complete=True;self.cancel=threading.Event()
    def get(self,pv):return self.values[pv]
    def put(self,pv,v):
        self.commands.append((pv,v));self.values[pv]=v
        if pv=='snap' and self.complete:self.values['generation']+=1
    def reader(self):return dict(turns=list(range(1,17)))
    def check_restored(self):
        self.assertEqual(self.values['mode'],'Repetitive 3Hz');self.assertEqual(self.values['shot'],0);self.assertEqual(self.values['turn'],1)
    def test_fresh_shot_and_restoration(self):
        r=TBTController(self.get,self.put,self.config).acquire(self.reader,start_turn=30)
        self.assertTrue(r['fresh_acquisition']);self.assertEqual(r['turns'][0],30);self.check_restored()
    def test_unknown_completion_never_writes(self):
        self.config['completion_confirmed']=False
        with self.assertRaises(ValueError):TBTController(self.get,self.put,self.config).acquire(self.reader)
        self.assertEqual(self.commands,[])
    def test_timeout_restores_modes(self):
        self.complete=False
        with self.assertRaises(TimeoutError):TBTController(self.get,self.put,self.config).acquire(self.reader)
        self.check_restored()
    def test_generation_change_during_read_rejected(self):
        def mixed():self.values['generation']+=1;return self.reader()
        with self.assertRaisesRegex(RuntimeError,'generation changed'):TBTController(self.get,self.put,self.config).acquire(mixed)
        self.check_restored()
    def test_reader_failure_and_cancel_restore_modes(self):
        def fail():raise RuntimeError('read failure')
        with self.assertRaises(RuntimeError):TBTController(self.get,self.put,self.config).acquire(fail)
        self.check_restored();self.complete=False;self.cancel.set()
        with self.assertRaisesRegex(RuntimeError,'cancelled'):TBTController(self.get,self.put,self.config).acquire(self.reader,cancel=self.cancel)
        self.check_restored()


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def wait(self,window):
        deadline=time.monotonic()+20
        while window.task is not None and window.task.isRunning():
            self.app.processEvents();time.sleep(.01)
            if time.monotonic()>deadline:raise AssertionError('Qt task timeout')
        self.app.processEvents()
    def test_original_qmod_buttons_scan_calculate_save(self):
        from Knobs.DRTuning.gui import TuningSession
        window=TuningSession(QuadFixture());window.show()
        try:
            with tempfile.TemporaryDirectory() as temp:
                for key,p in window.qmod.items():
                    window.tabs.setCurrentWidget(p);p.scan_button.click();self.wait(window)
                    self.assertEqual(len(p.sessions),1,window.messages.toPlainText())
                    p.form.pushButtonCALC.click();p.save(temp)
                    self.assertTrue((Path(temp)/f'qmod_{key}.json').exists())
                    self.assertEqual(float(p.summary.item(0,2).text()),8.)
                    self.assertEqual(p.point_table.rowCount(),5)
                window.tbt.turns.setValue(64);window.tbt.legacy.pushButtonUpdate.click();self.wait(window)
                window.tbt.legacy.pushButtonFFTcalc.click();window.tbt.save(temp)
                self.assertTrue((Path(temp)/'tbt.json').exists());self.assertNotIn('ERROR:',window.messages.toPlainText())
        finally:window.close()
    def test_update_shows_failure_then_success_and_is_read_only_by_default(self):
        from Knobs.DRTuning.gui import TuningSession
        class Backend(DemoBackend):
            label='EPICS test'
            fail=True
            def read_tbt(self,turns,*,cancel=None,progress=lambda *_:None):
                progress(0,'bpm01.WPSH (1/288)')
                if self.fail:raise RuntimeError('bpm01.WPSH: disconnected')
                return self.tbt(turns)
            def tbt_acquire(self,*args,**kwargs):raise AssertionError('Unexpected fresh trigger')
        backend=Backend();window=TuningSession(backend);window.show();window.tabs.setCurrentWidget(window.tbt)
        try:
            self.assertFalse(window.tbt.fresh.isChecked())
            window.tbt.update_button.click();self.wait(window)
            self.assertIn('bpm01.WPSH',window.operation_status.text())
            self.assertIn('Failed',window.tbt.acquisition_status.text())
            self.assertTrue(window.log_toggle.isChecked());self.assertFalse(window.cancel_button.isEnabled())
            backend.fail=False;window.tbt.turns.setValue(64);window.tbt.update_button.click();self.wait(window)
            self.assertIn('complete',window.operation_status.text());self.assertIn('64 turns',window.tbt.acquisition_status.text())
            self.assertTrue(window.tabs.isEnabled());self.assertEqual(len(window.tbt.plot.figure.axes),1)
        finally:window.close()

    def test_update_cancel_is_responsive_and_keeps_previous_waveform(self):
        from Knobs.DRTuning.gui import TuningSession
        class Backend(DemoBackend):
            label='EPICS test'
            def read_tbt(self,turns,*,cancel=None,progress=lambda *_:None):
                progress(5,'bpm02.WPSH')
                if not cancel.wait(5):raise AssertionError('Cancel was not delivered')
                raise RuntimeError('TBT read cancelled')
        b=Backend();window=TuningSession(b);window.show();window.tabs.setCurrentWidget(window.tbt)
        try:
            wave=b.tbt(32);window.tbt.received(wave);window.tbt.update_button.click()
            self.app.processEvents();self.assertTrue(window.cancel_button.isEnabled());self.assertFalse(window.tabs.isEnabled())
            window.cancel_button.click();self.wait(window)
            self.assertIs(window.tbt.wave,wave);self.assertIn('cancelled',window.operation_status.text())
            self.assertTrue(window.tabs.isEnabled())
        finally:
            window.service.cancel.set();self.wait(window);window.close()

    def test_redesigned_corrections_measure_apply_restore_and_small_window(self):
        from Knobs.DRTuning.gui import TuningSession
        window=TuningSession(DemoBackend());window.resize(1024,768);window.show();self.app.processEvents()
        try:
            self.assertLessEqual(window.width(),1024);self.assertLessEqual(window.height(),768)
            baseline=window.backend.snapshot()
            for mode,panel in window.panels.items():
                window.tabs.setCurrentWidget(panel)
                prefix='pushButtonDispersion' if mode=='dispersion' else 'pushButton'
                getattr(panel.form,prefix+'Calc').click();self.wait(window)
                self.assertIsNotNone(panel.data,window.messages.toPlainText())
                getattr(panel.form,prefix+'Set').click();self.wait(window);self.assertTrue(panel.data['applied'])
                getattr(panel.form,prefix+'Reset').click();self.wait(window)
                self.assertEqual(window.backend.snapshot(),baseline)
                self.assertNotIn('Failed',window.operation_status.text())
        finally:window.close()
    def test_launcher_keeps_one_interface_and_switches_tools(self):
        from Knobs.DRTuning.gui import DRTuningWindow
        window=DRTuningWindow();window.interface.setCurrentText('DEMO');window.connect_backend();self.wait(window)
        try:
            self.assertIsNotNone(window.session,window.status.text());backend=window.session.backend
            for key in ('orbit','dispersion','skew','tbt','qf1','qf2','qm1','qm2'):window.open_tool(key);self.app.processEvents()
            self.assertIs(backend,window.session.backend);self.assertFalse(window.interface.isEnabled())
            window.session.close();self.assertIsNotNone(window.session);self.assertTrue(window.disconnect())
            self.assertTrue(window.interface.isEnabled())
        finally:window.close()

    def test_live_launcher_uses_default_without_file_picker_or_pv_access(self):
        from unittest.mock import patch
        from Knobs.DRTuning.gui import DRTuningWindow
        from Knobs.DRTuning.epics import LiveBackend
        from Knobs.DRTuning.hardware import load_hardware_profile
        class NoIO:
            def __getattr__(self,name):raise AssertionError('Unexpected EPICS access: '+name)
        captured=[]
        def offline_backend(profile):
            captured.append(profile)
            return LiveBackend(profile,client=NoIO())
        window=DRTuningWindow();window.interface.setCurrentText('Machine (EPICS)')
        try:
            with patch('Knobs.DRTuning.epics.LiveBackend',side_effect=offline_backend):
                window.connect_backend();self.wait(window)
                self.assertIsNotNone(window.session,window.status.text())
                self.assertEqual(captured[-1],load_hardware_profile())
                self.assertIn('ATF machine defaults',window.status.text())
                self.assertEqual(len(window.session.backend.quadrupoles),100)
                self.assertTrue(window.disconnect())
                with tempfile.TemporaryDirectory() as temp:
                    custom=load_hardware_profile();custom['machine_id']='custom-test'
                    path=Path(temp)/'site.json';path.write_text(json.dumps(custom))
                    window.profile.setText(str(path));window.connect_backend();self.wait(window)
                    self.assertIsNotNone(window.session,window.status.text())
                    self.assertEqual(window.session.backend.identity,'epics:custom-test')
        finally:window.close()

    def test_correction_plot_labels_missing_geometry_as_index(self):
        from Knobs.DRCorrection.gui import Plot
        plot=Plot()
        data=dict(mode='orbit',source='test',names=['MB96R','MB98R'],positions_m=[137.,None],
                  valid=[True,True],signal_labels=['COD'],measured=[[.1,.2]],target=[[0.,0.]])
        try:
            plot.correction(data)
            self.assertEqual(plot.figure.axes[0].get_xlabel(),'BPM index (measurement order)')
            np.testing.assert_equal(plot.figure.axes[0].collections[0].get_offsets()[:,0],[1,2])
            self.assertIsNone(data['positions_m'][1])
        finally:plot.close()


class HardwareDefaultsTests(unittest.TestCase):
    def test_read_tbt_reports_pv_progress_and_cancel_without_writes(self):
        from Knobs.DRTuning.hardware import load_hardware_profile
        from Knobs.DRTuning.epics import LiveBackend
        class NoIO:
            def __getattr__(self,name):raise AssertionError('Unexpected EPICS call '+name)
        b=LiveBackend(load_hardware_profile(),NoIO());calls=[];updates=[]
        def metadata(pv):calls.append(pv);return dict(value=np.ones(32),timestamp=1.)
        b.metadata=metadata
        wave=b.read_tbt(32,progress=lambda n,s:updates.append((n,s)))
        self.assertEqual(len(calls),288);self.assertEqual(updates[-1][0],100)
        self.assertFalse(wave['fresh_acquisition']);self.assertFalse(wave['same_shot_verified'])
        cancel=threading.Event();cancel.set();calls.clear()
        with self.assertRaisesRegex(RuntimeError,'cancelled'):b.read_tbt(32,cancel=cancel)
        self.assertEqual(calls,[])
        b.metadata=lambda pv:dict(value=np.ones(8),timestamp=2.)
        with self.assertRaisesRegex(ValueError,'bpm01.WPSH'):b.read_tbt(32)
    def test_default_reads_full_98_slot_waveform_and_uses_live_limits(self):
        from Knobs.DRTuning.hardware import load_hardware_profile
        from Knobs.DRTuning.epics import LiveBackend
        p=load_hardware_profile();raw=np.zeros((98,10));columns=p['orbit']['columns']
        raw[:,columns['x']]=np.arange(1,99)*1000.
        raw[:,columns['status']]=p['orbit']['valid_status'];raw[:,columns['tmit']]=1.
        values={p['orbit']['packed_pv']:raw.ravel(),p['orbit']['mode_read']:p['orbit']['required_mode']}
        for n in ['ZH1R','SD1R.1']:
            values[p['actuators'][n]['lower_limit_pv']]=-8.
            values[p['actuators'][n]['upper_limit_pv']]=7.
        class ReadOnly:
            def caget(self,pv,**kwargs):return values[pv]
            def caput(self,*args,**kwargs):raise AssertionError('Unexpected write')
        b=LiveBackend(p,client=ReadOnly());r=b.orbit()
        self.assertEqual(r['names'][-2:],['MB97R','MB98R'])
        np.testing.assert_equal(r['x'][-2:],[97.,98.])
        np.testing.assert_equal(b.limits('correctors',['ZH1R']),[[-5.],[5.]])
        np.testing.assert_equal(b.limits('skews',['SD1R.1']),[[-8.],[7.]])

    def test_default_mapping_limits_and_missing_geometry(self):
        from Knobs.DRTuning.hardware import load_hardware_profile
        from Knobs.DRCorrection.backends import profile_issues
        p=load_hardware_profile()
        self.assertEqual(profile_issues(p),[])
        self.assertEqual(p['bpm_names'][-2:],['MB97R','MB98R'])
        self.assertEqual(p['orbit']['row_indices'][-2:],[96,97])
        self.assertEqual(p['positions_m'][-2:],[None,None])
        self.assertEqual(p['tbt']['names'][19],'MB20R')
        self.assertEqual(p['tbt']['names'][-1],'MB96R')
        self.assertEqual(p['rf']['mode'],'delta_ramp')
        self.assertEqual(p['rf']['hz_per_unit'],1e6)
        self.assertEqual(p['quadrupoles']['QM7R.1']['calibration']['main_current_pv'],'QM7AR:internalCurrentWrite')
        for n,a in p['actuators'].items():
            self.assertEqual(a['lower_limit_pv'],n.replace('.','_')+':parameter.A')
            self.assertEqual(a['upper_limit_pv'],n.replace('.','_')+':parameter.B')
        # Opting into missing geometry must not permit infinity or bad arrays.
        p['positions_m'][0]=float('inf')
        self.assertIn('Invalid positions_m array',profile_issues(p))

    def test_missing_override_is_not_replaced_and_defaults_are_independent(self):
        from Knobs.DRTuning.hardware import load_hardware_profile
        p=load_hardware_profile();p['rf']['settle_s']=0.
        self.assertEqual(load_hardware_profile()['rf']['settle_s'],5.)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(FileNotFoundError):load_hardware_profile(Path(temp)/'missing.json')


class ReadbackAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.values={'mode':'Repetitive 3Hz','shot':0,'turn':1};self.event=1.;self.updated=False
        self.config=dict(mode_pv='mode',shot_mode_pv='shot',turn_pv='turn',snap_pv='snap',timeout_s=.03,arm_settle_s=0.,readout_settle_s=0.,stable_read_interval_s=0.)
    def put(self,pv,value):
        self.values[pv]=value
        if pv=='snap':self.updated=True;self.event+=1
    def snapshot(self):
        n=2 if self.updated else 1
        return dict(turns=list(range(1,17))),{'x':dict(timestamp=float(n),sha256=str(n)),'y':dict(timestamp=float(n),sha256=str(n))}
    def controller(self):
        from Knobs.DRTuning.acquisition import ReadbackTBTController
        return ReadbackTBTController(self.values.__getitem__,self.put,lambda:self.event,self.config)
    def test_provisional_readback_checks_do_not_claim_common_shot(self):
        r=self.controller().acquire(self.snapshot)
        self.assertTrue(r['readback_update_verified']);self.assertFalse(r['same_shot_verified']);self.assertFalse(r['fresh_acquisition'])
        self.assertEqual(self.values['mode'],'Repetitive 3Hz');self.assertEqual(self.values['shot'],0)
    def test_stale_and_partially_updated_waveforms_rejected(self):
        for partial in (False,True):
            self.setUp()
            def snapshot():
                wave,evidence=self.snapshot()
                evidence['y']=dict(timestamp=1.,sha256='1')
                if not partial:evidence['x']=dict(timestamp=1.,sha256='1')
                return wave,evidence
            with self.assertRaisesRegex(TimeoutError,r'stale/missing PVs=.*y'):self.controller().acquire(snapshot)
            self.assertEqual(self.values['shot'],0)
    def test_processing_timestamp_alone_not_accepted(self):
        def snapshot():
            wave,evidence=self.snapshot()
            for v in evidence.values():v['sha256']='unchanged'
            return wave,evidence
        with self.assertRaisesRegex(TimeoutError,'content_changed=False'):self.controller().acquire(snapshot)
    def test_cancellation_restores_legacy_modes(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaisesRegex(RuntimeError,'cancelled'):self.controller().acquire(self.snapshot,cancel=cancel)
        self.assertEqual(self.values['mode'],'Repetitive 3Hz')


class OrbitModeAcquisitionTests(unittest.TestCase):
    def setup_live(self,original=2):
        from Knobs.DRTuning.epics import LiveBackend
        b=LiveBackend.__new__(LiveBackend)
        config=dict(mode='readback_checks',mode_pv='crate-mode',shot_mode_pv='shot',
                    turn_pv='turn',snap_pv='snap',event_pv='event',timeout_s=.03,
                    arm_settle_s=0.,readout_settle_s=0.,stable_read_interval_s=0.,
                    orbit_mode_pv='orbit-mode',orbit_mode_settle_s=0.,orbit_mode_timeout_s=.03)
        b.profile={'tbt':dict(mapping_confirmed=True,acquisition=config)}
        values={'orbit-mode':original,'crate-mode':'Repetitive 3Hz','shot':0,'turn':99,'event':1.}
        commands=[]
        def put(pv,value):
            commands.append((pv,value));values[pv]=value
            if pv=='snap':values['event']+=1
        b.get=values.__getitem__;b.put=put;b.metadata=lambda pv:dict(timestamp=values[pv])
        def snapshot(turns,**kwargs):
            self.assertEqual(values['orbit-mode'],0)
            n=values['event']
            return dict(turns=list(range(1,turns+1))),{'bpm01.WPSH':dict(timestamp=n,sha256=str(n))}
        b.tbt_snapshot=snapshot
        return b,values,commands
    def test_new_shot_restores_each_original_mode(self):
        for original in (0,1,2,3):
            with self.subTest(original=original):
                b,values,commands=self.setup_live(original)
                r=b.tbt_acquire(16,start_turn=21)
                self.assertEqual(values['orbit-mode'],original)
                self.assertEqual((values['crate-mode'],values['shot'],values['turn']),('Repetitive 3Hz',0,99))
                self.assertEqual(r['turns'][0],21);self.assertTrue(r['orbit_mode_control']['restored'])
                if original:
                    self.assertEqual(commands[0],('orbit-mode',0));self.assertEqual(commands[-1],('orbit-mode',original))
                else:self.assertNotIn('orbit-mode',[pv for pv,_ in commands])
    def test_waveform_timeout_restores_orbit_and_crate_modes(self):
        b,values,commands=self.setup_live()
        b.tbt_snapshot=lambda turns,**kwargs:(dict(turns=list(range(1,turns+1))),{'bpm02.WPSV':dict(timestamp=1.,sha256='old')})
        with self.assertRaisesRegex(TimeoutError,'bpm02.WPSV'):b.tbt_acquire(16)
        self.assertEqual(values['orbit-mode'],2);self.assertEqual(values['shot'],0);self.assertEqual(values['turn'],99)
        self.assertEqual(commands[-1],('orbit-mode',2))
    def test_mode_readback_mismatch_never_triggers_shot(self):
        b,values,commands=self.setup_live()
        original_put=b.put
        def put(pv,value):
            if pv=='orbit-mode' and value==0:commands.append((pv,value))
            else:original_put(pv,value)
        b.put=put
        with self.assertRaisesRegex(TimeoutError,'orbit-mode: expected 0, read 2'):b.tbt_acquire(16)
        self.assertEqual(commands,[('orbit-mode',0),('orbit-mode',2)])
    def test_cancellation_after_mode_write_still_restores(self):
        b,values,commands=self.setup_live(3);cancel=threading.Event();original_put=b.put
        def put(pv,value):
            original_put(pv,value)
            if pv=='orbit-mode' and value==0:cancel.set()
        b.put=put
        with self.assertRaisesRegex(RuntimeError,'cancelled'):b.tbt_acquire(16,cancel=cancel)
        self.assertEqual(values['orbit-mode'],3);self.assertNotIn('snap',[pv for pv,_ in commands])
    def test_failed_write_may_have_applied_and_is_restored(self):
        b,values,commands=self.setup_live();original_put=b.put
        def put(pv,value):
            original_put(pv,value)
            if pv=='orbit-mode' and value==0:raise RuntimeError('write reply lost')
        b.put=put
        with self.assertRaisesRegex(RuntimeError,'write reply lost'):b.tbt_acquire(16)
        self.assertEqual(values['orbit-mode'],2);self.assertNotIn('snap',[pv for pv,_ in commands])
    def test_restoration_failure_is_not_reported_as_success(self):
        b,values,commands=self.setup_live();original_put=b.put
        def put(pv,value):
            if pv=='orbit-mode' and value==2:return  # simulate accepted but ineffective restore
            original_put(pv,value)
        b.put=put
        with self.assertRaisesRegex(RuntimeError,'ORBIT_MODE restoration failed'):b.tbt_acquire(16)
        self.assertEqual(values['orbit-mode'],0)
    def test_completion_counter_path_also_selects_and_restores_orbit_mode(self):
        b,values,commands=self.setup_live(1);a=b.profile['tbt']['acquisition']
        a.update(mode='completion',completion_confirmed=True,generation_pv='event')
        def read(turns,**kwargs):
            self.assertEqual(values['orbit-mode'],0);return dict(turns=list(range(1,turns+1)))
        b.read_tbt=read;r=b.tbt_acquire(16)
        self.assertTrue(r['orbit_mode_control']['restored']);self.assertEqual(values['orbit-mode'],1)


class LiveQuadTests(unittest.TestCase):
    def setup_backend(self):
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'DRCorrection/tests'))
        from test_corrections import fake_profile,FakeEpics
        from Knobs.DRTuning.epics import LiveBackend
        p=fake_profile();p['quadrupoles']={'Q':dict(family='QF1R',mapping_confirmed=True,set='Q',read='Q',lower_limit_pv='low',upper_limit_pv='high',settle_s=0.,calibration=dict(kind='polynomial',confirmed=True,coefficients=[0.,.01]))}
        client=FakeEpics();client.values.update(Q=0.,low=-.3,high=.3)
        return LiveBackend(p,client),client
    def test_live_quad_limits_and_stale_settings_include_quads(self):
        b,c=self.setup_backend();before=b.snapshot();b.set_quadrupole_current('Q',.2)
        self.assertFalse(b.same_settings(before,b.snapshot()));b.restore(before);self.assertTrue(b.same_settings(before,b.snapshot()))
        with self.assertRaises(ValueError):b.set_quadrupole_current('Q',.4)
        self.assertEqual(c.values['Q'],0.)
    def test_unknown_tbt_mapping_prevents_first_scan_write(self):
        b,c=self.setup_backend()
        with self.assertRaises(ValueError):QModService(b,threading.Event()).scan('Q',[-.1,0,.1])
        self.assertEqual(c.values['Q'],0.)
    def test_field_pv_check_keeps_explicit_units(self):
        b,c=self.setup_backend();c.values['BL']=2.
        b.quadrupoles['Q']['field_check']=dict(read_pv='BL',k1l_per_unit=.001,tolerance_per_m=1e-9)
        self.assertTrue(b.quadrupole_field_check('Q',.2)['within_tolerance'])
        self.assertFalse(b.quadrupole_field_check('Q',.1)['within_tolerance'])

class TunePVTests(unittest.TestCase):
    def test_tune_values_may_stay_equal_but_timestamps_must_advance(self):
        b,_=LiveQuadTests().setup_backend();b.profile['qmod_tunes']=dict(mode='pvs',x='tx',y='ty',timeout_s=.03,max_timestamp_skew_s=.01)
        calls=[]
        def metadata(pv):
            calls.append(pv);return dict(value=.2 if pv=='tx' else .4,timestamp=1. if len(calls)<=2 else 2.)
        b.metadata=metadata;r=b.measure_tunes();self.assertEqual(r['tunes'],[.2,.4]);self.assertFalse(r['same_shot_verified'])
    def test_stale_tune_values_rejected(self):
        b,_=LiveQuadTests().setup_backend();b.profile['qmod_tunes']=dict(mode='pvs',x='tx',y='ty',timeout_s=.03,max_timestamp_skew_s=.01)
        b.metadata=lambda pv:dict(value=.2,timestamp=1.)
        with self.assertRaises(TimeoutError):b.measure_tunes()
    def test_unconfigured_quads_do_not_break_other_tools(self):
        b,c=LiveQuadTests().setup_backend()
        from Knobs.DRTuning.epics import LiveBackend
        p=copy.deepcopy(b.profile);p['quadrupoles']['unknown']=dict(mapping_confirmed=False)
        backend=LiveBackend(p,c);self.assertNotIn('unknown',backend.quadrupoles);backend.snapshot()

if __name__=='__main__':unittest.main()
