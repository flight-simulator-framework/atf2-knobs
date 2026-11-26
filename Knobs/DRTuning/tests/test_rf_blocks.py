"""RF command counts and recovery through the grouped response acquisition paths."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from Knobs.DRCorrection.backends import DemoBackend
from Knobs.DRCorrection.core import Cancelled,CorrectionService,Options
from Knobs.DRTuning.dispersion import DispersionOptions,DispersionService,QM_CANDIDATES
from Knobs.DRTuning.qm_checkpoint import load_checkpoint
from Knobs.DRTuning.tests.test_bump_dispersion import DispersionFixture


class RFBlockTests(unittest.TestCase):
    def test_qm_trim_matches_serial_matrix_with_three_changes(self):
        b=DispersionFixture();s=DispersionService(b,threading.Event())
        opt=DispersionOptions();baseline=b.snapshot()
        old=s.calculate(opt)
        with patch.object(b,'set_frequency',wraps=b.set_frequency) as rf:
            result=s.calculate(opt,group_rf=True)
        self.assertEqual([args.args[0] for args in rf.call_args_list],[-2000.,2000.,0.])
        self.assertEqual(len(result['actuators']),13)
        for key in ('response','full_measured','delta'):
            np.testing.assert_allclose(result[key],old[key],atol=1.e-10)
        self.assertEqual(b.snapshot(),baseline)
        blocks=result['rf_blocks'][0]['blocks']
        for offset in (0.,-2000.,2000.):
            at_rf=[f for f in result['orbit_measurements'] if f['rf_offset_hz']==offset]
            self.assertEqual(len(at_rf),28)
            self.assertEqual(sum(f['role']=='probe' for f in at_rf),26)
        self.assertEqual([f['role'] for f in result['orbit_measurements']].count('reference end'),3)
        for block in blocks:
            np.testing.assert_allclose(block['reference_drift_rms_mm'],[0.,0.])
        self.assertTrue(all(f['nshots']==10 for f in result['orbit_measurements']))

    def test_dispersion_read_retries_only_invalid_frequency_and_keeps_three_fit_frames(self):
        for plane in ('x','y'):
            for offset in (0.,-2000.,2000.):
                with self.subTest(plane=plane,offset=offset):
                    b=DispersionFixture();s=DispersionService(b,threading.Event())
                    reader=s.reader if plane=='x' else CorrectionService(b)
                    original=b.orbit
                    remaining=10
                    def transient():
                        nonlocal remaining
                        frame=original()
                        if b.offset==offset and remaining:
                            frame['valid'][20]=False;remaining-=1
                        return frame
                    with patch.object(b,'orbit',side_effect=transient),patch.object(reader.cancel,'wait',return_value=False) as wait,patch.object(b,'set_frequency',wraps=b.set_frequency) as rf:
                        if plane=='x':_,valid,_,_=s.signal(DispersionOptions())
                        else:_,valid=reader._signal('dispersion',Options(plane='y',frequency_change_khz=4.,bpm_samples=10))
                    wait.assert_called_once_with(3.)
                    self.assertEqual([c.args[0] for c in rf.call_args_list],[-2000.,2000.,0.])
                    self.assertEqual((b.acquisitions,len(reader.orbit_measurements)),(40,3))
                    self.assertTrue(valid[20])
                    idx=(0.,-2000.,2000.).index(offset)
                    self.assertFalse(reader.orbit_measurements[idx]['retry_of']['valid'][20])

    def test_cancel_during_dispersion_retry_restores_rf_and_excluded_bpm_does_not_retry(self):
        b=DemoBackend();s=CorrectionService(b);original=b.orbit
        opt=Options(plane='y',frequency_change_khz=4.,bpm_samples=10)
        def dropout():
            frame=original()
            if b.offset<0:frame['valid'][20]=False
            return frame
        def cancel(delay):
            self.assertEqual(delay,3.)
            s.cancel.set()
            return True
        with patch.object(b,'orbit',side_effect=dropout),patch.object(s.cancel,'wait',side_effect=cancel):
            with self.assertRaises(Cancelled):s._signal('dispersion',opt)
        self.assertEqual(b.offset,0.)
        s.cancel.clear();s.orbit_measurements=[]
        opt.excluded_bpms=(b.bpm_names[20],)
        with patch.object(b,'orbit',side_effect=dropout),patch.object(s.cancel,'wait',side_effect=AssertionError):
            _,valid=s._signal('dispersion',opt)
        self.assertFalse(valid[20])
        self.assertEqual(len(s.orbit_measurements),3)

    def test_vertical_zv_reference_and_probe_retry_one_missing_bpm(self):
        from Knobs.DRTuning.zv_rf_scan import measure_all_zvs
        b=DemoBackend();reader=CorrectionService(b);original=b.orbit
        n=b.vcorrectors[0]
        opt=Options(plane='y',selection_method='all',frequency_change_khz=4.,bpm_samples=10)
        activity=[]
        reader.matrix_activity=activity.append
        remaining={'reference':10,'probe':10}
        def transient():
            frame=original()
            key='probe' if b.currents[n] else 'reference'
            if b.offset==-2000. and remaining[key]:
                frame['valid'][20]=False;remaining[key]-=1
            return frame
        with patch.object(b,'orbit',side_effect=transient),patch.object(reader.cancel,'wait',return_value=False) as wait:
            data=measure_all_zvs(reader,opt,lambda *_:None)
        self.assertEqual([call.args for call in wait.call_args_list],[(3.,),(3.,)])
        self.assertTrue(data['valid'][20])
        self.assertEqual(len(data['rf_blocks']),1)
        self.assertTrue(any('retry in 3 s' in e['stage'] for e in activity))
        for side in ('Low','High'):
            self.assertTrue(any(n in e['point'] and 'RF -2 kHz' in e['point']
                                and side in e['stage'] and e['shots']==(10,10) for e in activity))
        total=len(data['actuators'])
        self.assertEqual(activity[-1]['columns'],(total,total,0))

    def test_progress_display_failure_preserves_current_and_rf_restoration(self):
        from Knobs.DRTuning.zv_rf_scan import acquire_zvs
        machine=DemoBackend()
        service=CorrectionService(machine)
        original=machine.snapshot()
        def observer(event):
            if machine.offset and event['stage'].startswith('Restoring'):
                raise RuntimeError('display disconnected')
        service.matrix_activity=observer
        options=Options(plane='y',fixed_actuators=tuple(machine.vcorrectors[1:]))
        with self.assertRaisesRegex(RuntimeError,'display disconnected'):
            acquire_zvs(service,options,lambda *_:None)
        self.assertTrue(machine.same_settings(original,machine.snapshot()))

    def test_full_vertical_response_probes_and_restores_only_allowed_ranges(self):
        from Knobs.DRTuning.zv_rf_scan import measure_all_zvs
        b=DemoBackend()
        expected=[f'ZV{i}R' for i in range(1,52) if 1<=i<=8 or 17<=i<=32 or 43<=i<=51]
        b.vcorrectors=[f'ZV{i}R' for i in range(1,52)]
        b.hcorrectors=[f'ZH{i}R' for i in range(1,52)]
        b.currents=dict.fromkeys(b.hcorrectors+b.vcorrectors,0.)
        rng=np.random.default_rng(72)
        for axis in ('x','y'):b.orm[axis]=b.basis@rng.normal(size=(8,51))
        b.dispr=b.basis@rng.normal(size=(8,51))
        s=CorrectionService(b)
        opt=Options(plane='y',selection_method='all',count_v=5,bpm_samples=10,
                    frequency_change_khz=4.,preserve_vertical_dispersion_orbit=True)
        baseline=b.snapshot()
        with patch.object(b,'set_frequency',wraps=b.set_frequency) as rf,patch.object(b,'write',wraps=b.write) as writes:
            data=measure_all_zvs(s,opt,lambda *_:None)
            self.assertEqual(data['actuators'],expected)
            self.assertEqual(data['selected'],expected)
            self.assertEqual(data['response'].shape,(192,33))
            self.assertEqual(b.snapshot(),baseline)
            self.assertEqual(rf.call_count,3)
            before=b.acquisitions
            opt.fixed_actuators=('ZV51R',)
            data=s.recalculate(data,opt)
            self.assertNotIn('ZV51R',data['selected'])
            self.assertEqual(b.acquisitions,before)
            self.assertEqual(rf.call_count,3)
            writes.reset_mock()
            with patch.object(s.cancel,'wait',return_value=False):
                s.apply(data)
            with patch.object(b,'orbit',side_effect=AssertionError),patch.object(b,'set_frequency',side_effect=AssertionError):
                following=s.recalculate(data,opt)
            np.testing.assert_array_equal(following['measured'],data['after'])
            self.assertEqual(following['baseline'],b.snapshot())
            self.assertEqual(following['response_baseline'],baseline)
            np.testing.assert_array_equal(following['response'],data['response'])
            s.reset()
            touched={n for call in writes.call_args_list for n in call.args[1]}
            self.assertTrue(touched)
            self.assertLessEqual(touched,set(expected)-{'ZV51R'})
            self.assertEqual(b.snapshot(),baseline)

    def test_vertical_current_beam_reads_three_settings_and_refits_weights_offline(self):
        from Knobs.DRTuning.zv_rf_scan import measure_all_zvs
        b=DemoBackend();s=CorrectionService(b)
        opt=Options(plane='y',selection_method='all',bpm_samples=10,frequency_change_khz=4.,
                    preserve_vertical_dispersion_orbit=True,dispersion_weight=1.,orbit_weight=0.)
        data=measure_all_zvs(s,opt,lambda *_:None)
        n=data['actuators'][0];b.currents[n]=.2;b.disp0*=.7
        before=b.snapshot();start=b.acquisitions
        with patch.object(b,'write',side_effect=AssertionError),patch.object(b,'set_frequency',wraps=b.set_frequency) as rf:
            current=s.reuse_on_current_beam(data,opt)
            self.assertEqual([c.args[0] for c in rf.call_args_list],[-2000,2000,0])
            self.assertEqual(b.acquisitions-start,30);self.assertEqual(current['baseline'],before)
            np.testing.assert_allclose(current['response'],data['response'])
            self.assertEqual(current['centers'][0],.2)
            opt.orbit_weight=100.
            fit=s.recalculate(current,opt)
            self.assertEqual(b.acquisitions-start,30);self.assertEqual(rf.call_count,3)
            self.assertFalse(np.allclose(fit['delta'],current['delta']))
            np.testing.assert_array_equal(fit['target'][1],current['measured'][1])

    def test_vertical_shortlist_and_recalculate(self):
        b=DemoBackend();s=CorrectionService(b)
        opt=Options(plane='y',bpm_samples=10,frequency_change_khz=4.,count_v=5)
        from dataclasses import asdict
        chosen=b.vcorrectors[:5]
        baseline=b.snapshot()
        proposal=dict(mode='dispersion',options=dict(asdict(opt),response_source='rftrack_nominal'),
                      selected=chosen,backend_id=b.identity,actuators=b.vcorrectors,baseline=baseline)
        old=s.measure_selected_zvs(proposal)
        with patch.object(b,'set_frequency',wraps=b.set_frequency) as rf:
            result=s.measure_selected_zvs(proposal,group_rf=True)
            np.testing.assert_allclose(result['full_response'],old['full_response'],atol=1.e-9)
            np.testing.assert_allclose(result['delta'],old['delta'],atol=1.e-9)
            s.recalculate(result,Options(**result['options']))
            self.assertEqual([args.args[0] for args in rf.call_args_list],[-2000.,2000.,0.])
        self.assertEqual(result['actuators'],chosen)
        self.assertEqual(b.snapshot(),baseline)

    def test_zv_retries_only_weak_dispersion_and_reuses_resolved_cod(self):
        from Knobs.DRTuning.zv_rf_scan import measure_all_zvs
        for resolved in (True,False):
            with self.subTest(nominal_resolved=resolved):
                b=DemoBackend();s=CorrectionService(b);read=b.orbit
                strong,weak=b.vcorrectors[:2]
                b.dispr[:,0]=20.;b.dispr[:,1]=4.
                b.orm['y'][:,0]=.001  # Strong dispersion with very small nominal COD.
                b.orm['y'][:,1]=1. if resolved else 0.
                opt=Options(plane='y',selection_method='all',frequency_change_khz=4.,bpm_samples=10,
                            adaptive_probe=True,fixed_actuators=tuple(b.vcorrectors[2:]))
                baseline=b.snapshot()
                def noisy():
                    frame=read()
                    frame['y']+=.0005*(-1 if b.acquisitions%2 else 1)
                    return frame
                with patch.object(b,'orbit',side_effect=noisy),patch.object(b,'set_frequency',wraps=b.set_frequency) as rf:
                    data=measure_all_zvs(s,opt,lambda *_:None)
                self.assertEqual([r['magnet'] for r in data['probe_history']],[strong,weak,weak])
                self.assertEqual([r['quality']['usable'] for r in data['probe_history']],[True,False,True])
                self.assertEqual(data['probe_history'][-1]['nominal_reused'],resolved)
                offsets=[c.args[0] for c in rf.call_args_list]
                self.assertEqual(offsets,[-2000,2000,-2000,0] if resolved else [-2000,2000,0,-2000,0])
                np.testing.assert_allclose(data['response'][:96],b.dispr[:,:2],atol=1.e-10)
                self.assertEqual(b.snapshot(),baseline)

    def test_only_weak_qm_reprobed(self):
        b=DispersionFixture();s=DispersionService(b,threading.Event())
        strong,weak=QM_CANDIDATES[:2]
        opt=DispersionOptions(use_probe_presets=False,probe_A=.05,
            fixed_actuators=tuple(QM_CANDIDATES[2:]))
        b.dr[:,0]*=10.
        b.dr[:,1]*=.2
        orbit=b.orbit
        def noisy():
            frame=orbit()
            frame['x']+=.1*b.q[weak]+.0002*(-1 if b.acquisitions%2 else 1)
            return frame
        with patch.object(b,'orbit',side_effect=noisy),patch.object(b,'set_frequency',wraps=b.set_frequency) as rf:
            data=s.calculate(opt,group_rf=True)
        rounds=data['rf_blocks']
        self.assertGreater(len(rounds),1)
        self.assertEqual(set(rounds[0]['targets']),{strong,weak})
        self.assertTrue(all(list(r['targets'])==[weak] for r in rounds[1:]))
        sides=[-2000.,2000.]
        for _ in rounds[1:]:sides.append(-sides[-1])
        self.assertEqual([c.args[0] for c in rf.call_args_list],sides+[0.])
        for i,r in enumerate(rounds[1:]):
            self.assertEqual(r['rf_order'],[0,(-1)**i,-(-1)**i])
            self.assertEqual([block['offset_hz'] for block in r['blocks']],[0.,-2000.,2000.])
        frames=data['orbit_measurements']
        self.assertEqual(sum(f['rf_offset_hz']==0 for f in frames),6)
        for r in rounds[1:]:
            self.assertEqual(r['blocks'][0]['pairs'][weak],rounds[0]['blocks'][0]['pairs'][weak])
        np.testing.assert_allclose(data['response'][:len(b.bpm_names),1],b.dr[:,1],atol=1e-9)
        self.assertNotEqual(data['probe_readbacks'][1]['actual_by_rf'][0],data['probe_readbacks'][1]['actual_by_rf'][1])

        opt.constrain_cod=True
        with patch.object(b,'orbit',side_effect=noisy):
            constrained=DispersionService(b,threading.Event()).calculate(opt,group_rf=True)
        off_energy_retries=len(constrained['rf_blocks'])-1
        self.assertGreater(off_energy_retries,0)
        nominal=sum(f['rf_offset_hz']==0 for f in constrained['orbit_measurements'])
        self.assertEqual(nominal,6+4*off_energy_retries)

    def test_qm_transient_partial_loss_retries_only_one_rf_current_point(self):
        b=DispersionFixture();s=DispersionService(b,threading.Event())
        bad=QM_CANDIDATES[0]
        opt=DispersionOptions(fixed_actuators=tuple(QM_CANDIDATES[2:]),use_probe_presets=False,probe_A=.05)
        orbit=b.orbit
        dropped=0
        def noisy():
            nonlocal dropped
            frame=orbit()
            if b.offset==-2000 and b.q[bad]<0 and dropped<10:
                frame['valid'][0]=False
                dropped+=1
            return frame
        with patch.object(b,'orbit',side_effect=noisy),patch.object(s.reader.cancel,'wait',return_value=False) as waits:
            data=s.calculate(opt,group_rf=True)
        self.assertEqual(dropped,10)
        self.assertEqual(len(data['rf_blocks']),1)
        retry=[f for f in data['orbit_measurements'] if f['role']=='probe retry']
        self.assertEqual([(f['magnet'],f['rf_offset_hz'],f['current_A']) for f in retry],[(bad,-2000.,-.05)])
        waits.assert_any_call(3.)
        self.assertEqual(len(data['orbit_measurements']),19)


    def test_mixed_qm_retries_only_shrinking_probe_at_nominal_rf(self):
        b=DispersionFixture();s=DispersionService(b,threading.Event())
        weak,shrinking=QM_CANDIDATES[:2]
        b.dr[:,0]*=.2
        b.dr[:,1]*=10.
        opt=DispersionOptions(use_probe_presets=False,probe_A=.05,fixed_actuators=tuple(QM_CANDIDATES[2:]))
        original=b.orbit
        def orbit():
            frame=original()
            frame['x']+=.1*b.q[weak]+.0002*(-1 if b.acquisitions%2 else 1)
            if b.offset<0 and abs(b.q[shrinking])>.021:frame['valid'][:]=False
            return frame
        messages=[]
        with patch.object(b,'orbit',side_effect=orbit),patch.object(s.reader.cancel,'wait',return_value=False):
            data=s.calculate(opt,lambda pct,msg:messages.append(msg),group_rf=True)
        rounds=data['rf_blocks']
        self.assertEqual(set(rounds[1]['targets']),{weak,shrinking})
        initial,second=[r['blocks'][0]['pairs'] for r in rounds[:2]]
        self.assertEqual(second[weak]['indices'],initial[weak]['indices'])
        self.assertNotEqual(second[shrinking]['indices'],initial[shrinking]['indices'])
        self.assertTrue(any(weak in msg and 'low S/N' in msg for msg in messages))
        self.assertTrue(any(shrinking in msg and 'reduced probe' in msg for msg in messages))
        np.testing.assert_allclose(data['response'][:len(b.bpm_names),:2],b.dr[:,:2],atol=1e-9)

    def test_zv_pause_at_offset_only_waits_at_original_settings(self):
        from Knobs.DRTuning.zv_rf_scan import acquire_zvs
        b=DemoBackend();s=CorrectionService(b);baseline=b.snapshot()
        names=b.vcorrectors[:2];opt=Options(fixed_actuators=tuple(b.vcorrectors[2:]),frequency_change_khz=4.)
        def progress(_,msg):
            if msg.startswith('RF -2 kHz: '+names[0]):s.pause_requested.set()
            elif msg.startswith('PAUSED:'):
                self.assertEqual(b.snapshot(),baseline)
                s.pause_requested.clear()
        with patch.object(b,'set_frequency',wraps=b.set_frequency) as rf:
            acquire_zvs(s,opt,progress)
        self.assertEqual([call.args[0] for call in rf.call_args_list],[-2000.,0.,-2000.,2000.,0.])
        self.assertEqual(b.snapshot(),baseline)

    def test_resuming_weak_first_column_retains_later_strong_column(self):
        b=DispersionFixture();cancel=threading.Event();s=DispersionService(b,cancel)
        weak,strong=QM_CANDIDATES[:2]
        opt=DispersionOptions(use_probe_presets=False,probe_A=.05,fixed_actuators=tuple(QM_CANDIDATES[2:]))
        b.dr[:,0]*=.2;b.dr[:,1]*=10.
        original=b.orbit
        def noisy():
            frame=original()
            frame['x']+=.0002*(-1 if b.acquisitions%2 else 1)
            return frame
        def checkpoint(e):
            if e['kind']=='checkpoint' and e['completed']==1:cancel.set()
        with tempfile.TemporaryDirectory() as folder,patch.object(b,'orbit',side_effect=noisy):
            path=Path(folder)/'scan.json'
            with self.assertRaises(Cancelled):
                s.calculate(opt,event=checkpoint,checkpoint_path=path,group_rf=True)
            saved=load_checkpoint(path)
            self.assertEqual(set(saved['rf_results']),{strong})
            self.assertEqual(set(saved['rf_pending']),{weak})
            b.commands.clear();cancel.clear()
            data=s.resume(saved,opt)
            self.assertEqual(set(n for n,_ in b.commands),{weak})
            self.assertEqual(data['scan_state']['completed'],2)
            np.testing.assert_allclose(data['response'][:,1],saved['rf_results'][strong]['column'])

    def test_main_coils_and_apply_recalculate_roundtrip(self):
        from Knobs.DRTuning.tests.test_qm_main import MainFixture
        b=MainFixture();s=DispersionService(b,threading.Event());opt=DispersionOptions(coil='main')
        with patch.object(b,'set_frequency',wraps=b.set_frequency) as rf:
            data=s.calculate(opt,group_rf=True)
            self.assertEqual(len(data['actuators']),8)
            self.assertEqual(rf.call_count,3)
            s.validate_checkpoint(data)
            s.apply(data)
            self.assertEqual(rf.call_count,6)
            updated=s.recalculate(data,opt)
            self.assertEqual(rf.call_count,6)
            self.assertTrue(updated['iteration_index'])
            s.reset(updated)
        self.assertTrue(all(v==10 for v in b.mains.values()))

    def test_bad_initial_qm_retries_only_that_magnet_at_smaller_width(self):
        b=DispersionFixture();s=DispersionService(b,threading.Event())
        bad,good=QM_CANDIDATES[:2]
        opt=DispersionOptions(fixed_actuators=tuple(QM_CANDIDATES[2:]),use_probe_presets=False,probe_A=.05)
        orbit=b.orbit
        def dropped():
            frame=orbit()
            if abs(b.q[bad])>.021 and b.offset<0:frame['valid'][:]=False
            return frame
        with patch.object(b,'orbit',side_effect=dropped):
            data=s.calculate(opt,group_rf=True)
        self.assertEqual([r['targets'] for r in data['rf_blocks']],[
            {bad:[-.05,.05],good:[-.05,.05]}, {bad:[-.02,.02]}])
        self.assertEqual(data['probe_attempts'][0]['next_width_A'],.02)
        self.assertTrue(data['probe_quality'][0]['usable'])

    def test_spans_are_normalized_at_each_rf(self):
        b=DispersionFixture();s=DispersionService(b,threading.Event())
        n=QM_CANDIDATES[0]
        opt=DispersionOptions(fixed_actuators=tuple(QM_CANDIDATES[1:]),use_probe_presets=False,probe_A=.1)
        old_orbit=b.orbit
        def orbit_with_cod():
            frame=old_orbit()
            frame['x']+=100*b.q[n]
            return frame
        setter=b.set_quadrupole_current
        def write(name,value):
            setter(name,value*(1+b.offset/10000))
        with patch.object(b,'orbit',side_effect=orbit_with_cod),patch.object(b,'set_quadrupole_current',side_effect=write):
            data=s.calculate(opt,group_rf=True)
        np.testing.assert_allclose(data['response'][:len(b.bpm_names),0],b.dr[:,0],atol=1.e-9)
        np.testing.assert_allclose(data['response'][2*len(b.bpm_names):3*len(b.bpm_names),0],100.)

    def test_zv_failed_rf_read_and_current_restore_propagate(self):
        b=DemoBackend();s=CorrectionService(b)
        from Knobs.DRTuning.zv_rf_scan import acquire_zvs
        opt=Options(fixed_actuators=tuple(b.vcorrectors[1:]),frequency_change_khz=4.,bpm_samples=10)
        orbit=b.orbit
        def fail_at_plus_probe():
            if b.offset>0 and b.currents[b.vcorrectors[0]]:raise TimeoutError('BPM timeout')
            return orbit()
        baseline=b.snapshot()
        with patch.object(b,'orbit',side_effect=fail_at_plus_probe),self.assertRaisesRegex(TimeoutError,'BPM timeout'):
            acquire_zvs(s,opt,lambda *_:None)
        self.assertEqual(b.snapshot(),baseline)
        setter=b.write
        def fail_restore(kind,names,values):
            if b.offset<0 and values[0]==0:raise RuntimeError('restore failed')
            setter(kind,names,values)
        with patch.object(b,'write',side_effect=fail_restore),self.assertRaisesRegex(RuntimeError,'restore failed'):
            acquire_zvs(s,opt,lambda *_:None)
        self.assertEqual(b.offset,baseline['frequency'])

    def test_cancel_at_offset_restores_and_resumes_completed_block(self):
        b=DispersionFixture();cancel=threading.Event();s=DispersionService(b,cancel)
        opt=DispersionOptions(fixed_actuators=tuple(QM_CANDIDATES[2:]))
        baseline=b.snapshot()
        def interrupt(_,message):
            if message.startswith('RF -2 kHz: '+QM_CANDIDATES[1]):cancel.set()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'scan.json'
            with self.assertRaises(Cancelled):
                s.calculate(opt,interrupt,group_rf=True,checkpoint_path=path)
            saved=load_checkpoint(path)
        self.assertTrue(saved['restored']);self.assertEqual(b.snapshot(),baseline)
        self.assertEqual(len(saved['rf_round']['blocks']),1)
        self.assertEqual(list(saved['rf_round']['partial']['pairs']),[QM_CANDIDATES[0]])
        cancel.clear()
        corrupt=copy.deepcopy(saved)
        corrupt['rf_round']['targets']['other magnet']=[-.1,.1]
        with patch.object(b,'set_frequency') as rf,self.assertRaisesRegex(ValueError,'Invalid pending RF block targets'):
            s.resume(corrupt,opt)
        rf.assert_not_called()
        writes=[]
        setter=b.set_quadrupole_current
        def record(n,v):
            writes.append((b.offset,n))
            setter(n,v)
        with patch.object(b,'set_quadrupole_current',side_effect=record):
            resumed=s.resume(saved,opt)
        self.assertNotIn(0.,[offset for offset,n in writes])
        self.assertEqual({n for offset,n in writes if offset==-2000},{QM_CANDIDATES[1]})
        expected=s.calculate(opt)
        np.testing.assert_allclose(resumed['response'],expected['response'],atol=1.e-10)
        self.assertEqual(b.snapshot(),baseline)
