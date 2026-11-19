"""Offline acquisition, measured closure, bounded correction and rollback tests."""
import copy
import threading
import unittest
from unittest.mock import patch
import numpy as np
from Knobs.DRCorrection.backends import DemoBackend
from Knobs.DRCorrection.core import CorrectionService,Options,Cancelled
from Knobs.DRTuning.fresh import updates,scalar_statistics
from Knobs.DRTuning.coupling import CouplingService,BumpOptions,fit_size
from Knobs.DRTuning.dispersion import DispersionService,DispersionOptions,QM_CANDIDATES,straight_bpms
from Knobs.DRTuning.bump_maps import constrained_step
from Knobs.DRTuning.epics import LiveBackend


class FreshTests(unittest.TestCase):
    def test_repeated_timestamps_do_not_count_and_values_are_the_gated_payload(self):
        stamps=iter([1,1,2,2,3,4,5,6,7,8,9,10,11])
        def metadata(pv):
            t=next(stamps);return dict(value=100+t,timestamp=t)
        frames=list(updates(metadata,['x'],10,threading.Event(),timeout_s=.2))
        self.assertEqual([f['x']['timestamp'] for f in frames],list(range(2,12)))
        self.assertEqual(frames[-1]['x']['value'],111)
    def test_timeout_cancel_and_alarm(self):
        event=threading.Event();read=lambda _:dict(value=5,timestamp=1)
        with self.assertRaises(TimeoutError):list(updates(read,['x'],1,event,timeout_s=.001))
        event.set()
        with self.assertRaises(Cancelled):list(updates(read,['x'],1,event))
        event.clear()
        with self.assertRaises(RuntimeError):list(updates(lambda _:dict(value=5,timestamp=2,severity=2),['x'],1,event))
    def test_live_xsr_is_ten_raw_updates_and_callbacks(self):
        b=object.__new__(LiveBackend);b.profile={};counter=iter(range(30));seen=[]
        b.metadata=lambda pv:dict(value=12.+next(counter)*.01,timestamp=next(counter))
        result=b.sample_xsr(threading.Event(),callback=seen.append)
        self.assertEqual(result['pv'],'XSR:PROJ_V_SIGMA');self.assertEqual(result['count'],10)
        self.assertEqual(len(seen),10);self.assertTrue(result['update_verified'])
        self.assertEqual(len(set(result['timestamps'])),10)
    def test_live_bpm_reads_metadata_payload_without_second_waveform_read(self):
        b=object.__new__(LiveBackend);b.profile={'orbit':dict(packed_pv='bpm',row_width=4,row_indices=[0],
            columns=dict(status=0,x=1,y=2,tmit=3),valid_status=1,mm_per_unit=.001)}
        b.bpm_names=['MB1R'];b.acquisitions=0;counter=iter(range(20))
        def metadata(pv):
            t=next(counter);return dict(value=[1,t*1000,t*2000,1],timestamp=t)
        b.metadata=metadata;b.get=lambda _:self.fail('unexpected duplicate read')
        service=CorrectionService(b);x,y,valid=service._orbit(Options(bpm_samples=10))
        np.testing.assert_allclose(x,[5.5]);np.testing.assert_allclose(y,[11.]);self.assertTrue(valid[0])
        self.assertTrue(service.orbit_measurements[-1]['freshness_verified'])


class BumpFixture(DemoBackend):
    def __init__(self):
        super().__init__();self.bpm_names=[f'B{i}' for i in range(8)];self.positions_m=np.arange(8.)
        self.hcorrectors=[f'H{i}' for i in range(4)];self.vcorrectors=[f'V{i}' for i in range(4)]
        self.currents=dict.fromkeys(self.hcorrectors+self.vcorrectors,0.)
        rng=np.random.default_rng(82);mix=np.eye(8)+rng.normal(0,.15,(8,8))
        self.response=np.zeros((16,8));self.response[[0,1,8,9]]=mix[:4]
        self.response[2:8]=np.column_stack([np.linspace(1,2,6),np.linspace(0,1,6)])@mix[4:6]
        self.response[10:16]=np.column_stack([np.linspace(1,2,6),np.linspace(0,1,6)])@mix[6:8]
        self.reconstruction=np.zeros((4,16));self.reconstruction[:,[0,1,8,9]]=np.eye(4)
        self.nonlinear=0.;self.fail=False;self.minimum=.13;self.commands=[]
    def write(self,kind,names,values):
        self.commands.append((kind,list(names),list(values)));super().write(kind,names,values)
    def vector(self):
        v=self.response@self.read('correctors',self.hcorrectors+self.vcorrectors)
        v[10:16]+=self.nonlinear*v[8]**2
        return v
    def orbit(self):
        v=self.vector();self.acquisitions+=1
        return dict(names=self.bpm_names,x=v[:8],y=v[8:],tmit=np.ones(8),valid=np.ones(8,bool),position_unit='mm')
    def sample_xsr(self,cancel,count=10,callback=lambda *_:None):
        if self.fail:raise TimeoutError('XSR stale')
        mean=np.sqrt(100+300*(self.vector()[8]-self.minimum)**2);values=[]
        for i in range(count):
            if cancel.is_set():raise Cancelled('test cancelled')
            values.append(mean+(.001 if i%2 else -.001))
            result=scalar_statistics(values,list(range(i+1)),'XSR:PROJ_V_SIGMA');callback(result)
        return result
    def geometry(self):
        return dict(target='SF1R.1',actuators=self.hcorrectors+self.vcorrectors,bpm_names=self.bpm_names,
            positions_m=self.positions_m,centers=np.zeros(8),limits=[np.full(8,-5.),np.full(8,5.)],
            reconstruction=self.reconstruction,outside_indices=np.arange(2,8),outside_rows=np.r_[np.arange(2,8),np.arange(10,16)],
            internal_indices=[0,1],internal_bpms=['B0','B1'],baseline=self.snapshot(),model_settings={})


class CouplingTests(unittest.TestCase):
    def setUp(self):
        self.b=BumpFixture();self.cancel=threading.Event();self.s=CouplingService(self.b,self.cancel)
        self.o=BumpOptions(settle_s=0.)
    def test_measured_bump_scan_fit_refine_and_final_set(self):
        initial=self.b.snapshot();g=self.s.calibrate(self.b.geometry(),self.o)
        self.assertEqual(self.b.snapshot(),initial)
        self.assertTrue(g['calibrated']);events=[]
        result=self.s.scan(g,self.o,event=events.append)
        self.assertTrue(result['applied']);self.assertAlmostEqual(result['final_amplitude_mm'],.13,places=5)
        np.testing.assert_allclose(result['refinement_mm'],[.03,.23],atol=1.e-6)
        self.assertEqual(len(result['points']),8);self.assertEqual(len(result['fits']),2)
        self.assertEqual(sum(e['kind']=='sample' for e in events),80)
        self.assertLess(result['final']['orbit_after_xsr']['outside_max_mm'],1.e-6)
        np.testing.assert_allclose(self.b.vector()[[0,1,8,9]],[0,0,.13,0],atol=1.e-6)
        self.s.reset(result);self.assertEqual(self.b.snapshot(),initial)
    def test_leakage_from_nonlinearity_is_refined_without_losing_amplitude(self):
        self.b.nonlinear=1.
        g=self.s.calibrate(self.b.geometry(),self.o);events=[]
        result=self.s.scan(g,self.o,event=events.append)
        self.assertTrue(any(e['kind']=='orbit' and e['iteration']>0 for e in events))
        self.assertLess(result['final']['orbit_after_xsr']['outside_max_mm'],.01)
        self.assertAlmostEqual(self.b.vector()[8],result['final_amplitude_mm'],places=6)
    def test_stale_xsr_and_cancel_restore_all_currents(self):
        for cancel in (False,True):
            with self.subTest(cancel=cancel):
                self.b.fail=False;self.cancel.clear();g=self.s.calibrate(self.b.geometry(),self.o);initial=self.b.snapshot()
                self.b.fail=not cancel
                def event(e):
                    if cancel and e['kind']=='sample':self.cancel.set()
                with self.assertRaises((TimeoutError,Cancelled)):self.s.scan(g,self.o,event=event)
                self.assertEqual(self.b.snapshot(),initial);self.assertTrue(self.s.last_result['restored'])
                self.assertFalse(self.s.last_result['applied'])
    def test_unbracketed_minimum_restores(self):
        g=self.s.calibrate(self.b.geometry(),self.o);initial=self.b.snapshot();self.b.minimum=.8
        with self.assertRaisesRegex(ValueError,'not bracketed'):self.s.scan(g,self.o)
        self.assertEqual(self.b.snapshot(),initial)
    def test_changed_settings_block_scan_before_writes(self):
        g=self.s.calibrate(self.b.geometry(),self.o);self.b.offset=1.;self.b.commands=[]
        with self.assertRaisesRegex(ValueError,'Settings changed'):self.s.scan(g,self.o)
        self.assertEqual(self.b.commands,[])
    def test_rank_absolute_signed_current(self):
        self.b.unit=lambda _:'A';self.b.skew_values['SD1R.1$SKEW']=-2.;self.b.skew_values['SF1R.2$SKEW']=1.
        rows=self.s.ranked_targets();self.assertEqual(rows[0]['name'],'SD1R.1');self.assertEqual(rows[0]['current_A'],-2.)
    def test_infeasible_equality_never_clips(self):
        with self.assertRaisesRegex(ValueError,'cannot be reached'):
            constrained_step(np.eye(2),[0,0],[[1,1]],[3],[-1,-1],[1,1])
    def test_readback_span_not_commanded_span_normalizes_response(self):
        original=self.b.write
        def write(kind,names,values):original(kind,names,np.asarray(values)*.8)
        self.b.write=write;self.b.profile={'current_tolerance_A':.1}
        g=self.s.calibrate(self.b.geometry(),self.o)
        np.testing.assert_allclose(g['response'],self.b.response,atol=1.e-10)
        self.assertAlmostEqual(g['probe_readbacks'][0]['actual'][1],.04)
    def test_restoration_failure_attempts_remaining_supplies_and_is_recorded(self):
        g=self.s.calibrate(self.b.geometry(),self.o);self.b.fail=True;restored=[]
        def restore(kind,n,value):
            restored.append(n)
            if n==g['actuators'][0]:raise RuntimeError('supply offline')
            self.b.write(kind,[n],[value])
        self.b.restore_probe=restore
        with self.assertRaisesRegex(RuntimeError,'restoration incomplete'):self.s.scan(g,self.o)
        self.assertEqual(restored,g['actuators']);self.assertFalse(self.s.last_result['restored'])
        self.assertIn('supply offline',self.s.last_result['restoration_error'])
    def test_flat_size_fit_rejected(self):
        points=[dict(amplitude_mm=a,xsr=dict(mean=10.,sem=.01)) for a in np.linspace(-.4,.4,5)]
        with self.assertRaisesRegex(ValueError,'curvature'):fit_size(points,.4)
    def test_excluded_model_bpms_are_lifted_to_acquisition_order(self):
        from types import SimpleNamespace
        keep=np.array([0,1,2,4,5,6,7]);rows=np.r_[keep,keep+8];g=self.b.geometry()
        g.update(bpm_names=[self.b.bpm_names[i] for i in keep],positions_m=keep.astype(float),
                 reconstruction=self.b.reconstruction[:,rows],model_response=self.b.response[rows],
                 outside_indices=np.arange(2,7),outside_rows=np.r_[np.arange(2,7),np.arange(9,14)])
        model=SimpleNamespace(bpm_names=g['bpm_names'])
        with patch.object(self.s.reader.model_manager,'prepare',return_value=model),patch('Knobs.DRTuning.coupling.select_geometry',return_value=g) as select:
            result=self.s.prepare('SF1R.1',self.o,Options(excluded_bpms=('B3',)))
        self.assertEqual(len(select.call_args.args[2]),7)
        self.assertEqual(result['bpm_names'],self.b.bpm_names)
        np.testing.assert_allclose(result['reconstruction'],self.b.reconstruction)
        self.assertNotIn(3,result['outside_indices']);np.testing.assert_allclose(result['model_response'][3],0.)


class DispersionFixture(DemoBackend):
    def __init__(self):
        super().__init__();self.quadrupoles={n:dict(limits_A=[-2.,2.],family='QMTRIM1' if n.endswith('.1') else 'QMTRIM2') for n in QM_CANDIDATES}
        self.q=dict.fromkeys(QM_CANDIDATES,0.);self.commands=[];self.fail=False
        straight=straight_bpms(self.bpm_names);rng=np.random.default_rng(91)
        self.dr=np.zeros((len(self.bpm_names),16));self.dr[straight]=rng.normal(0,3.,(straight.sum(),16))
        self.d0=self.dr@np.linspace(-.06,.06,16)
    def snapshot(self):
        s=super().snapshot();s['quadrupoles']=self.q.copy();return s
    def quadrupole_current(self,n):return self.q[n]
    def validate_quadrupole_targets(self,n,values):
        if np.max(abs(np.asarray(values)))>2:raise ValueError('QM limits')
    def set_quadrupole_current(self,n,v):
        self.validate_quadrupole_targets(n,[v]);self.q[n]=v;self.commands.append((n,v))
    def measure_tunes(self,cancel=None):
        if self.fail:raise RuntimeError('Tune read failed')
        return dict(tunes=np.array([.2,.4])+np.array([1,-1])*.001*sum(self.q.values()))
    def orbit(self):
        q=np.array(list(self.q.values()));delta=-self.offset/(self.nominal_frequency_hz*.0021)
        self.acquisitions+=1
        return dict(names=self.bpm_names,x=(self.d0+self.dr@q)*delta,y=np.zeros(len(self.bpm_names)),
                    tmit=np.ones(len(self.bpm_names)),valid=np.ones(len(self.bpm_names),bool),position_unit='mm')


class DispersionTests(unittest.TestCase):
    def setUp(self):
        self.b=DispersionFixture();self.cancel=threading.Event();self.s=DispersionService(self.b,self.cancel)
        self.o=DispersionOptions(regularization=0.,fixed_actuators=(),measure_tunes=True,step_A=.1)
    def test_requested_defaults_skip_tunes_through_calc_set_reset_and_exclude_qm2(self):
        opt=DispersionOptions();initial=self.b.snapshot()
        self.assertEqual((opt.probe_A,opt.step_A,opt.frequency_change_khz),(.1,2.,4.))
        self.assertEqual(opt.fixed_actuators,('QM2R.1','QM2R.2'))
        with patch.object(self.b,'measure_tunes',side_effect=AssertionError('Tune read must be skipped')) as tunes,\
             patch.object(self.b,'validate_tune_measurement',create=True,side_effect=AssertionError('Tune preflight must be skipped')) as validate,\
             patch.object(self.b,'set_frequency',wraps=self.b.set_frequency) as frequency:
            data=self.s.calculate(opt)
            self.assertEqual(self.b.snapshot(),initial);self.assertEqual(len(data['actuators']),14)
            self.assertEqual(len(data['orbit_measurements']),87)
            for key in ('initial_tunes','tune_response','predicted_tune_change'):self.assertIsNone(data[key])
            self.assertEqual(data['scan_tunes'],[]);self.assertIn('skipped',data['solver'])
            for probe in data['probe_readbacks']:np.testing.assert_allclose(probe['requested'],[-.1,.1])
            self.s.apply(data);self.assertTrue(data['applied']);self.assertFalse(data['tune_check_performed'])
            self.assertIsNone(data['after_tunes']);self.s.reset(data)
            tunes.assert_not_called();validate.assert_not_called()
            self.assertEqual({call.args[0] for call in frequency.call_args_list},{-2000.,0.,2000.})
        self.assertEqual(self.b.snapshot(),initial)
        self.assertTrue(set(n for n,_ in self.b.commands).isdisjoint(opt.fixed_actuators))
    def test_skipping_tunes_retains_cod_verification_and_restoration(self):
        data=self.s.calculate(DispersionOptions());initial=self.b.snapshot();original=self.b.orbit
        def shifted():
            result=original();result['x']+=.2;return result
        self.b.orbit=shifted
        with self.assertRaisesRegex(ValueError,'COD change exceeds'):self.s.apply(data)
        self.assertEqual(self.b.snapshot(),initial);self.assertTrue(data['restored'])
    def test_enabling_tune_constraints_requires_a_measured_response(self):
        opt=DispersionOptions();data=self.s.calculate(opt);opt.measure_tunes=True
        with self.assertRaisesRegex(ValueError,'No measured tune response'):self.s.solve(data,opt)
    def test_16_qm_measured_response_and_actual_after_plot_data(self):
        initial=self.b.snapshot();data=self.s.calculate(self.o)
        self.assertEqual(self.b.snapshot(),initial);self.assertEqual(len(data['actuators']),16)
        self.assertEqual(len(data['orbit_measurements']),99)
        self.assertTrue(all(f['nshots']==10 for f in data['orbit_measurements']))
        self.s.apply(data);self.assertTrue(data['applied'])
        np.testing.assert_allclose(data['full_after'],data['full_predicted'],atol=1.e-10)
        self.assertLess(np.linalg.norm(data['full_after'][0]),np.linalg.norm(data['full_measured'][0])*.01)
        self.s.reset(data);self.assertEqual(self.b.snapshot(),initial)
    def test_greedy_count_and_constraints(self):
        data=self.s.calculate(self.o);self.o.selection='greedy';self.o.maximum=3;self.o.tune_tolerance=.00001
        self.s.solve(data,self.o);self.assertLessEqual(len(data['selected']),3)
        self.assertLessEqual(np.max(abs(data['predicted_tune_change'])),self.o.tune_tolerance+1.e-10)
        self.assertLessEqual(np.max(abs(data['delta'])),self.o.step_A+1.e-10)
    def test_cancel_during_scan_restores_probe_and_rf(self):
        initial=self.b.snapshot()
        def progress(*_):self.cancel.set()
        with self.assertRaises(Cancelled):self.s.calculate(self.o,progress)
        self.assertEqual(self.b.snapshot(),initial)
    def test_failed_set_remeasurement_restores_qm(self):
        data=self.s.calculate(self.o);initial=self.b.snapshot();self.b.fail=True
        with self.assertRaisesRegex(RuntimeError,'Tune read failed'):self.s.apply(data)
        self.assertEqual(self.b.snapshot(),initial)
    def test_unconfirmed_qm_mapping_fails_before_any_write(self):
        del self.b.quadrupoles['QM2R.1']
        with self.assertRaisesRegex(ValueError,'QM2R.1'):self.s.calculate(self.o)
        self.assertEqual(self.b.commands,[]);self.assertEqual(self.b.acquisitions,0)
    def test_no_beam_prevents_qm_probe(self):
        orbit=self.b.orbit
        def invalid():
            result=orbit();result['valid'][:]=False;return result
        self.b.orbit=invalid
        with self.assertRaisesRegex(ValueError,'No valid straight'):self.s.calculate(self.o)
        self.assertEqual(self.b.commands,[]);self.assertEqual(self.b.offset,0.)
    def test_all_corrector_solver_ignores_greedy_count_and_honors_bounds(self):
        b=DemoBackend();s=CorrectionService(b);o=Options(selection_method='all',count_v=1,step_A=.03)
        data=s.calculate('dispersion',o)
        self.assertEqual(len(data['selected']),len(b.vcorrectors))
        self.assertTrue(np.all(abs(data['delta'])<=.03+1.e-12));self.assertIn('regularized',data['solver'])


class CorrectionGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def test_new_navigation_actual_dispersion_and_live_fit_plot(self):
        from Knobs.DRTuning.gui import TuningSession
        b=DispersionFixture();w=TuningSession(b)
        try:
            panel=w.qm_dispersion;defaults=panel.options()
            self.assertEqual((panel.probe.value(),panel.step.value(),panel.rf.value()),(.1,2.,4.))
            self.assertEqual(set(defaults.fixed_actuators),{'QM2R.1','QM2R.2'})
            self.assertFalse(defaults.measure_tunes);self.assertFalse(panel.tune.isEnabled())
            data=panel.service.calculate(defaults)
            panel.service.apply(data);panel.display(data)
            self.assertIn('tune constraints skipped',panel.status.text())
            panel.measure_tunes.setChecked(True);self.assertTrue(panel.options().measure_tunes);self.assertTrue(panel.tune.isEnabled())
            self.assertEqual(len(panel.plot.figure.axes),4)
            self.assertTrue(all('Measured after SET' in ax.get_legend_handles_labels()[1] for ax in panel.plot.figure.axes))
            w.open_tool('qm_dispersion');self.assertIs(w.tabs.currentWidget(),panel)
            bump=w.coupling_panel;points=[]
            for a in np.linspace(-.4,.4,5):
                xsr=scalar_statistics(np.full(10,np.sqrt(100+300*(a-.13)**2)),range(10),'XSR:PROJ_V_SIGMA')
                bump.on_update(dict(kind='sample',phase='coarse',amplitude_mm=a,xsr=xsr))
                point=dict(amplitude_mm=a,xsr=xsr);points.append(point);bump.on_update(dict(kind='point',point=point))
            fit=fit_size(points,.4);bump.on_update(dict(kind='fit',fit=fit))
            self.assertTrue(any('Fit 1:' in label for label in bump.plot.figure.axes[0].get_legend_handles_labels()[1]))
            self.assertEqual(len(bump.samples),5);self.assertEqual(bump.amplitude.value(),.4);self.assertEqual(bump.refine.value(),.1)
            w.open_tool('coupling');self.assertIs(w.tabs.currentWidget(),bump)
        finally:w.close();self.app.processEvents()


class NativeBumpMapTests(unittest.TestCase):
    def test_periodic_matrix_matches_small_tracked_bump_including_ring_wrap(self):
        try:import RF_Track
        except ImportError:self.skipTest('RFTrack unavailable')
        from Knobs.DRTuning.rftrack import RFTrackBackend
        from Knobs.DRTuning.model import ModelManager
        from Knobs.DRTuning.bump_maps import select_geometry
        b=RFTrackBackend();model=ModelManager(b).prepare(Options(model_match_tunes=False),lambda:None)
        g=select_geometry(model,'SF1R.1',np.ones(len(b.bpm_names),bool))
        base=model.orbit();amplitude=.001
        model.write('correctors',g['actuators'],g['centers']+amplitude*g['model_knob_A_per_mm'])
        after=model.orbit();observed=np.r_[after['x']-base['x'],after['y']-base['y']]
        predicted=g['model_response']@(amplitude*g['model_knob_A_per_mm'])
        np.testing.assert_allclose(observed,predicted,atol=5.e-6)
        np.testing.assert_allclose(g['reconstruction']@observed,[0,0,amplitude,0],atol=3.e-6)
        self.assertLess(np.max(abs(observed[g['outside_rows']])),5.e-6)
        self.assertLessEqual(np.max(abs(g['model_knob_A_per_mm']))*.4,1.+1.e-8)


if __name__=='__main__':unittest.main()
