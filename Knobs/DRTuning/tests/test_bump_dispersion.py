"""Offline acquisition, measured closure, bounded correction and rollback tests."""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
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
from Knobs.DRTuning.qm_checkpoint import load_checkpoint,save_checkpoint


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
        b=object.__new__(LiveBackend);b.profile={};seen=[]
        reads=[];elapsed=0.;cancel=threading.Event()
        def wait(seconds):
            nonlocal elapsed
            elapsed+=seconds
            return False
        def metadata(pv):
            reads.append(elapsed)
            return dict(value=12.,timestamp=6*(elapsed//6))
        b.metadata=metadata
        with patch.object(cancel,'wait',side_effect=wait),patch('Knobs.DRTuning.fresh.time.monotonic',side_effect=lambda:elapsed):
            result=b.sample_xsr(cancel,callback=seen.append)
        self.assertEqual(reads,list(range(61)))
        self.assertEqual(result['pv'],'XSR:PROJ_V_SIGMA');self.assertEqual(result['count'],10)
        self.assertEqual(len(seen),10);self.assertTrue(result['update_verified'])
        self.assertEqual(result['timestamps'],list(range(6,61,6)))
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
        bpm_counts_at_xsr_end=[]
        def event(value):
            events.append(value)
            if value['kind']=='sample' and value['xsr']['count']==10:
                bpm_counts_at_xsr_end.append(self.b.acquisitions)
            if value['kind']=='point':
                self.assertEqual(self.b.acquisitions,bpm_counts_at_xsr_end[-1])
                self.assertTrue(value['point']['orbit']['closed'])
        result=self.s.scan(g,self.o,event=event)
        self.assertTrue(result['applied']);self.assertAlmostEqual(result['final_amplitude_mm'],.13,places=5)
        np.testing.assert_allclose(result['refinement_mm'],[.03,.23],atol=1.e-6)
        self.assertEqual(len(result['points']),8);self.assertEqual(len(result['fits']),2)
        self.assertEqual(sum(e['kind']=='sample' for e in events),80)
        self.assertEqual(self.b.acquisitions,bpm_counts_at_xsr_end[-1])
        self.assertLess(result['final']['orbit']['outside_max_mm'],1.e-6)
        np.testing.assert_allclose(self.b.vector()[[0,1,8,9]],[0,0,.13,0],atol=1.e-6)
        self.s.reset(result);self.assertEqual(self.b.snapshot(),initial)
    def test_leakage_from_nonlinearity_is_refined_without_losing_amplitude(self):
        self.b.nonlinear=1.
        g=self.s.calibrate(self.b.geometry(),self.o);events=[]
        result=self.s.scan(g,self.o,event=events.append)
        self.assertTrue(any(e['kind']=='orbit' and e['iteration']>0 for e in events))
        self.assertLess(result['final']['orbit']['outside_max_mm'],.01)
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
    def test_prepare_passes_shared_exclusions_to_preset_binding_without_model(self):
        candidate=self.b.geometry();options=Options(excluded_bpms=('B3',))
        with patch('Knobs.DRTuning.model.ModelManager.prepare',side_effect=AssertionError('No RFTrack')) as model,\
             patch('Knobs.DRTuning.coupling.preset_geometry',return_value=candidate) as preset:
            result=self.s.prepare('SF1R.1',self.o,options)
        model.assert_not_called();self.assertIs(preset.call_args.args[4],options)
        self.assertEqual(result['bpm_names'],self.b.bpm_names);self.assertEqual(self.b.commands,[])


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
        self.o=DispersionOptions(probe_A=.01,use_probe_presets=False,regularization=0.,fixed_actuators=(),measure_tunes=True,adaptive_probe=False)
    def test_requested_defaults_skip_tunes_through_calc_set_reset_and_exclude_qm2(self):
        opt=DispersionOptions();initial=self.b.snapshot()
        self.assertEqual((opt.probe_A,opt.step_A,opt.frequency_change_khz),(.1,3.,4.))
        self.assertTrue(opt.adaptive_probe);self.assertEqual(opt.step_A,3.)
        self.assertEqual(opt.target_snr,5.);self.assertFalse(opt.constrain_cod)
        self.assertEqual(opt.fixed_actuators,('QM2R.1','QM2R.2','QM3R.2'))
        with patch.object(self.b,'measure_tunes',side_effect=AssertionError('Tune read must be skipped')) as tunes,\
             patch.object(self.b,'validate_tune_measurement',create=True,side_effect=AssertionError('Tune preflight must be skipped')) as validate,\
             patch.object(self.b,'set_frequency',wraps=self.b.set_frequency) as frequency:
            data=self.s.calculate(opt)
            self.assertEqual(self.b.snapshot(),initial);self.assertEqual(len(data['actuators']),13)
            self.assertEqual(len(data['orbit_measurements']),81)
            for key in ('initial_tunes','tune_response','predicted_tune_change'):self.assertIsNone(data[key])
            self.assertEqual(data['scan_tunes'],[]);self.assertIn('skipped',data['solver'])
            expected_widths=[.16,.34,.13,.62,.50,.28,.16,.22,.18,.47,.28,.20,.29]
            self.assertTrue(opt.use_probe_presets);self.assertFalse(opt.characterize_first_scan)
            for probe,width in zip(data['probe_readbacks'],expected_widths):
                np.testing.assert_allclose(probe['requested'],[-width,width])
            self.s.apply(data);self.assertTrue(data['applied']);self.assertFalse(data['tune_check_performed'])
            self.assertIsNone(data['after_tunes']);self.s.reset(data)
            tunes.assert_not_called();validate.assert_not_called()
            self.assertEqual({call.args[0] for call in frequency.call_args_list},{-2000.,0.,2000.})
        self.assertEqual(self.b.snapshot(),initial)
        self.assertTrue(set(n for n,_ in self.b.commands).isdisjoint(opt.fixed_actuators))
    def test_skipping_tunes_retains_cod_verification_and_restoration(self):
        data=self.s.calculate(DispersionOptions(constrain_cod=True));initial=self.b.snapshot();original=self.b.orbit
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
    def test_recalculate_changes_bpm_mask_without_any_machine_io(self):
        data=self.s.calculate(self.o);before=copy.deepcopy(data)
        j=int(np.flatnonzero(data['straight'])[0]);self.o.excluded_bpms=(data['names'][j],)
        # Corrupt one measured BPM to make exclusion materially change the fit.
        data['full_measured'][0,j]+=10.
        with ExitStack() as stack:
            for method in ('orbit','snapshot','same_settings','quadrupole_current','set_quadrupole_current',
                           'validate_mode','validate_quadrupole_targets','measure_tunes','set_frequency'):
                stack.enter_context(patch.object(self.b,method,side_effect=AssertionError('Unexpected machine IO: '+method)))
            result=self.s.recalculate(data,self.o)
        self.assertFalse(result['valid'][j]);self.assertTrue(data['valid'][j])
        self.assertTrue(result['response_reused']);self.assertEqual(result['recalculation_count'],1)
        self.assertEqual(result['baseline'],before['baseline']);np.testing.assert_equal(result['centers'],before['centers'])
        self.assertEqual(result['acquisition_options']['excluded_bpms'],())
        self.assertEqual(len(result['orbit_measurements']),len(before['orbit_measurements']))
        self.assertLess(np.linalg.norm(result['full_predicted'][0,result['valid']]),
                        np.linalg.norm(result['full_measured'][0,result['valid']])*.001)
        self.assertIs(self.s.last_result,result)
    def test_recalculation_can_reinclude_initially_excluded_but_never_invalid_bpm(self):
        good,bad=np.flatnonzero(straight_bpms(self.b.bpm_names))[:2]
        self.o.excluded_bpms=(self.b.bpm_names[good],)
        original=self.b.orbit
        def invalid():
            result=original();result['valid'][bad]=False;return result
        with patch.object(self.b,'orbit',side_effect=invalid):data=self.s.calculate(self.o)
        self.assertFalse(data['valid'][good]);self.assertTrue(data['acquisition_valid'][good])
        self.o.excluded_bpms=();result=self.s.recalculate(data,self.o)
        self.assertTrue(result['valid'][good]);self.assertFalse(result['valid'][bad])
        self.assertFalse(data['valid'][good]);self.assertEqual(result['acquisition_options']['excluded_bpms'],(self.b.bpm_names[good],))
    def test_recalculate_rejects_changed_measurement_conditions_and_uses_applied_baseline(self):
        data=self.s.calculate(self.o)
        for key,value,message in [('frequency_change_khz',2.,'RF / momentum'),
                                  ('momentum_compaction',.003,'RF / momentum'),
                                  ('fixed_actuators',('QM2R.1',),'QM selection')]:
            opt=copy.deepcopy(self.o);setattr(opt,key,value)
            with self.assertRaisesRegex(ValueError,message):self.s.recalculate(data,opt)
        self.s.apply(data)
        next_step=self.s.recalculate(data,self.o)
        np.testing.assert_allclose(next_step['full_measured'],data['full_after'])
        self.assertEqual(next_step['baseline'],self.b.snapshot())
        self.s.reset(data);result=self.s.recalculate(data,self.o)
        self.assertNotIn('full_after',result);self.assertFalse(result['applied'])
        self.s.apply(result);self.assertTrue(result['applied']);self.assertNotIn('restored',result)
    def test_apply_recalculated_proposal_ignores_excluded_bpm_cod_outlier(self):
        data=self.s.calculate(self.o);j=int(np.flatnonzero(data['straight'])[0])
        self.o.excluded_bpms=(data['names'][j],);result=self.s.recalculate(data,self.o)
        original=self.b.orbit
        def shifted():
            frame=original();frame['x'][j]+=.5;return frame
        with patch.object(self.b,'orbit',side_effect=shifted):self.s.apply(result)
        self.assertTrue(result['applied']);self.assertIs(self.s.last_result,result)
        self.assertGreater(abs(result['full_after'][2,j]-result['full_measured'][2,j]),self.o.orbit_tolerance_mm)
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
        self.assertEqual(self.s.last_result['validity_history'][0]['common_straight'],0)
    def test_common_bpm_failure_reports_accumulated_loss_and_restores_probe(self):
        initial=self.b.snapshot();straight=np.flatnonzero(straight_bpms(self.b.bpm_names))
        first,second=np.array_split(straight,2);original=self.b.orbit;progress=[]
        def alternating():
            frame=original();current=self.b.q['QM1R.1']
            if current<0:frame['valid'][first]=False
            if current>0:frame['valid'][second]=False
            return frame
        with patch.object(self.b,'orbit',side_effect=alternating):
            with self.assertRaisesRegex(ValueError,'Lost all common valid') as failure:
                self.s.calculate(self.o,lambda _,message:progress.append(message))
        data=self.s.last_result;report=data['validity_history'][-1]
        self.assertEqual(self.b.snapshot(),initial);self.assertNotIn('response',data)
        self.assertIn('QM1R.1 probe +0.01 A',str(failure.exception))
        self.assertGreater(report['current_straight'],0);self.assertGreater(report['previous_common'],0)
        self.assertEqual(report['common_straight'],0);self.assertFalse(np.any(data['valid']&data['straight']))
        self.assertEqual(report['lost_bpms'],[self.b.bpm_names[i] for i in second])
        self.assertEqual(len(report['settings']),3);self.assertIn('other/status invalid=',str(failure.exception))
        self.assertTrue(any('BPM validity:' in message for message in progress))
    def test_single_invalid_update_is_omitted_and_scan_continues(self):
        initial=self.b.snapshot();region=straight_bpms(self.b.bpm_names);original=self.b.orbit
        def dropout():
            frame=original()
            if self.b.acquisitions==31:frame['tmit'][region]=0.
            return frame
        with patch.object(self.b,'orbit',side_effect=dropout):
            data=self.s.calculate(self.o)
        report=data['validity_history'][1];settings=report['settings']
        self.assertEqual(self.b.snapshot(),initial)
        self.assertEqual(settings[0]['valid_straight'],int(region.sum()))
        self.assertEqual(settings[0]['per_sample_valid_straight'],[0]+[int(region.sum())]*9)
        self.assertEqual(settings[0]['invalid_intensity'],int(region.sum()))
        self.assertEqual(settings[1]['valid_straight'],int(region.sum()))
        self.assertEqual(settings[2]['valid_straight'],int(region.sum()))
        self.assertEqual(settings[0]['valid_sample_count'],[9]*int(region.sum()))
        self.assertEqual(settings[0]['max_invalid_streak'],[1]*int(region.sum()))
        self.assertEqual(len(data['orbit_measurements']),99)
        self.assertTrue(data['valid'][region].all());self.assertTrue(np.isfinite(data['response']).all())
    def test_five_consecutive_invalid_updates_stop_scan_and_restore(self):
        initial=self.b.snapshot();region=straight_bpms(self.b.bpm_names);original=self.b.orbit
        def dropout():
            frame=original()
            if 32<=self.b.acquisitions<=36:frame['tmit'][region]=0.
            return frame
        with patch.object(self.b,'orbit',side_effect=dropout):
            with self.assertRaisesRegex(ValueError,'5 consecutive invalid'):self.s.calculate(self.o)
        self.assertEqual(self.b.snapshot(),initial)
        setting=self.s.last_result['validity_history'][-1]['settings'][0]
        self.assertEqual(setting['valid_straight'],0)
        self.assertEqual(setting['max_invalid_streak'],[5]*int(region.sum()))
    def read_pattern(self,reader,pattern):
        frames=[]
        for i,good in enumerate(pattern):
            frame=self.b.orbit();frame['x'][0]=float(i) if good else (np.nan if i%2 else 1.e6)
            frame['valid'][0]=good;frames.append(frame)
        with patch.object(self.b,'orbit',side_effect=frames):reader._orbit(Options(bpm_samples=len(pattern)))
        return reader.orbit_measurements[-1]
    def test_four_invalid_updates_and_scattered_failures_use_only_good_values(self):
        for pattern in ([False]*4+[True]*6,[False,True]*5):
            self.s.reader.reset_sampling();frame=self.read_pattern(self.s.reader,pattern)
            values=np.flatnonzero(pattern)
            self.assertTrue(frame['valid'][0]);self.assertEqual(frame['valid_sample_count'][0],len(values))
            self.assertAlmostEqual(frame['x'][0],np.mean(values));self.assertAlmostEqual(frame['stdx'][0],np.std(values))
            self.assertEqual(frame['nshots'],10)
    def test_invalid_streak_crosses_rf_batches_and_resets_for_new_acquisition(self):
        reader=self.s.reader;reader.reset_sampling()
        first=self.read_pattern(reader,[True]*6+[False]*4)
        self.assertTrue(first['valid'][0]);self.assertEqual(first['trailing_invalid_streak'][0],4)
        second=self.read_pattern(reader,[False]+[True]*9)
        self.assertFalse(second['valid'][0]);self.assertEqual(second['max_invalid_streak'][0],5)
        # A good update ends the streak; a later isolated failure is accepted.
        third=self.read_pattern(reader,[False]+[True]*9)
        self.assertTrue(third['valid'][0]);self.assertEqual(third['max_invalid_streak'][0],1)
        self.read_pattern(reader,[True]*6+[False]*4);reader.reset_sampling()
        fresh=self.read_pattern(reader,[False]+[True]*9)
        self.assertTrue(fresh['valid'][0]);self.assertEqual(fresh['max_invalid_streak'][0],1)
    def test_other_correction_readers_keep_strict_validity(self):
        reader=CorrectionService(self.b)
        frame=self.read_pattern(reader,[True]*9+[False])
        self.assertFalse(frame['valid'][0])
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
    def test_qm_apply_button_converts_numpy_decisions_to_python_bool(self):
        from Knobs.DRTuning.gui import TuningSession
        w=TuningSession(DispersionFixture())
        try:
            panel=w.qm_dispersion;data=panel.service.calculate(panel.options())
            self.assertIsInstance(np.any(abs(data['delta'])>1.e-12),np.bool_)
            with patch.object(panel.set_button,'setEnabled',wraps=panel.set_button.setEnabled) as enabled:
                panel.display(data);self.assertTrue(panel.set_button.isEnabled())
                data['delta']=np.zeros_like(data['delta'])
                panel.display(data);self.assertFalse(panel.set_button.isEnabled())
                data['delta'][0]=.01;data['applied']=np.bool_(True)
                panel.display(data);self.assertFalse(panel.set_button.isEnabled())
            self.assertEqual([call.args[0] for call in enabled.call_args_list],[True,False,False])
            self.assertTrue(all(type(call.args[0]) is bool for call in enabled.call_args_list))
        finally:w.close();self.app.processEvents()
    def test_bpm_selection_invalidates_apply_and_recalculate_reuses_measurement(self):
        from Knobs.DRTuning.gui import TuningSession
        from Knobs.DRCorrection.selection import SelectionDialog
        from PyQt6.QtCore import Qt
        w=TuningSession(DispersionFixture())
        try:
            panel=w.qm_dispersion;data=panel.service.calculate(panel.options());panel.display(data)
            self.assertTrue(panel.set_button.isEnabled());self.assertTrue(panel.recalculate_button.isEnabled())
            dialog=SelectionDialog(w);j=int(np.flatnonzero(data['straight'])[0])
            dialog.bpms.items.item(j).setCheckState(Qt.CheckState.Unchecked);dialog.apply_to(w)
            self.assertFalse(panel.set_button.isEnabled());self.assertTrue(panel.recalculate_button.isEnabled())
            with patch.object(w,'error') as error,patch.object(panel.service,'apply') as apply:
                panel.apply();error.assert_called_once();apply.assert_not_called()
            acquisitions=w.backend.acquisitions;commands=list(w.backend.commands)
            with patch.object(w,'start',side_effect=lambda action,done,**_:done(action(lambda *_:None))):panel.recalculate()
            self.assertTrue(panel.set_button.isEnabled());self.assertFalse(panel.result['valid'][j])
            self.assertEqual(w.backend.acquisitions,acquisitions);self.assertEqual(w.backend.commands,commands)
            dialog.close()
        finally:w.close();self.app.processEvents()
    def test_apply_automatically_saves_measured_json_and_plot_without_dialog(self):
        from Knobs.DRTuning.gui import TuningSession
        w=TuningSession(DispersionFixture())
        try:
            panel=w.qm_dispersion;panel.display(panel.service.calculate(panel.options()))
            with tempfile.TemporaryDirectory() as folder:
                panel.autosave_directory=Path(folder)/'results'
                with patch.object(w,'start',side_effect=lambda action,done,**_:done(action(lambda *_:None))),\
                     patch('Knobs.DRTuning.correction_gui.QFileDialog.getSaveFileName',side_effect=AssertionError('Unexpected dialog')):
                    panel.apply()
                files=list(panel.autosave_directory.glob('*.json'));self.assertEqual(len(files),1)
                saved=json.loads(files[0].read_text());self.assertTrue(saved['applied'])
                np.testing.assert_allclose(saved['full_after'],panel.result['full_after'])
                self.assertEqual(saved['autosave']['json'],str(files[0]))
                self.assertTrue(files[0].with_suffix('.png').read_bytes().startswith(b'\x89PNG'))
                self.assertIn('Auto-saved',panel.save_status.text());self.assertTrue(panel.recalculate_button.isEnabled())
                panel.display(panel.result);self.assertEqual(len(list(panel.autosave_directory.glob('*.json'))),1)
                panel.service.reset(panel.result);panel.display(panel.service.recalculate(panel.result,panel.options()))
                with patch.object(w,'start',side_effect=lambda action,done,**_:done(action(lambda *_:None))):panel.apply()
                self.assertEqual(len(list(panel.autosave_directory.glob('*.json'))),2)
        finally:w.close();self.app.processEvents()
    def test_save_failure_keeps_applied_settings_and_offers_manual_retry(self):
        from Knobs.DRTuning.gui import TuningSession
        w=TuningSession(DispersionFixture())
        try:
            panel=w.qm_dispersion;panel.display(panel.service.calculate(panel.options()))
            with tempfile.TemporaryDirectory() as folder:
                blocked=Path(folder)/'file';blocked.write_text('not a directory');panel.autosave_directory=blocked
                with patch.object(w,'start',side_effect=lambda action,done,**_:done(action(lambda *_:None))),\
                     patch.object(panel.service,'reset') as reset,patch.object(w,'error') as error:
                    panel.apply();reset.assert_not_called();error.assert_not_called()
                self.assertTrue(panel.result['applied']);self.assertNotIn('error',panel.result)
                for n,c,d in zip(panel.result['actuators'],panel.result['centers'],panel.result['delta']):
                    self.assertAlmostEqual(w.backend.q[n],c+d)
                self.assertIn('SET remains applied',panel.save_status.text());self.assertIn('Save data + plot',panel.save_status.text())
        finally:w.close();self.app.processEvents()
    def test_new_navigation_actual_dispersion_and_live_fit_plot(self):
        from Knobs.DRTuning.gui import TuningSession
        b=DispersionFixture();w=TuningSession(b)
        try:
            panel=w.qm_dispersion;defaults=panel.options()
            self.assertEqual((panel.probe.value(),panel.step.value(),panel.rf.value()),(.1,3.,4.))
            from PyQt6.QtWidgets import QCheckBox
            from datetime import datetime
            self.assertEqual(panel.findChildren(QCheckBox),[])
            self.assertTrue(defaults.use_probe_presets);self.assertTrue(defaults.adaptive_probe)
            self.assertTrue(defaults.reuse_probe_widths);self.assertFalse(defaults.characterize_first_scan)
            self.assertFalse(defaults.constrain_cod)
            self.assertEqual(panel.autosave_directory,Path('/atf/data/flight-simulator/DRTuning')/datetime.now().strftime('%Y'))
            self.assertEqual(set(defaults.fixed_actuators),{'QM2R.1','QM2R.2','QM3R.2'})
            self.assertFalse(defaults.measure_tunes)
            data=panel.service.calculate(defaults)
            panel.service.apply(data);panel.display(data)
            self.assertIn('Applied. Dispersion remeasured.',panel.status.text())
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
            self.assertEqual(len(bump.samples),5);self.assertEqual(bump.amplitude.value(),.8);self.assertEqual(bump.refine.value(),.1)
            self.assertEqual(bump.options().amplitude_mm,.4);self.assertEqual(bump.step.value(),4.)
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
