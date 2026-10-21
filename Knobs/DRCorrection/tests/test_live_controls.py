"""Offline tests for recovered op-base controls; all PVs are simulated."""
import unittest
from unittest.mock import patch
import numpy as np
from Knobs.DRCorrection.backends import EpicsBackend,profile_issues
from Knobs.DRCorrection.core import CorrectionService,Options,Cancelled
from Knobs.DRCorrection.ramp import DeltaRamp
from test_corrections import fake_profile,FakeEpics


class RampClient(FakeEpics):
    def __init__(self):
        super().__init__();self.calls=[];self.values['rf']=714.
        self.values.update({'RAMP:STATUS_READ':0,'RAMP:HOLD:STATUS_READ':0})
        for offset in range(-10000,10001,1000):self.values['RAMP:'+DeltaRamp.label(offset)+':STATUS_READ']=int(offset==0)
        self.selected=0.;self.fail_once=None
    def caput(self,pv,value,**kwargs):
        self.calls.append((pv,value))
        if pv==self.fail_once:self.fail_once=None;return -1
        if pv=='RAMP:CONTROL_ON_SW':self.values['RAMP:STATUS_READ']=1
        elif pv=='RAMP:CONTROL_OFF_SW':self.values['RAMP:STATUS_READ']=0
        elif pv.startswith('RAMP:') and pv.endswith(':ONOFF_SW'):
            for offset in range(-10000,10001,1000):
                label=DeltaRamp.label(offset);match=pv=='RAMP:'+label+':ONOFF_SW'
                self.values['RAMP:'+label+':STATUS_READ']=int(match)
                if match:self.selected=offset
        else:return super().caput(pv,value,**kwargs)
        self.values['rf']=714.+(self.selected/1e6 if self.values['RAMP:STATUS_READ'] else 0)
        return 1


def ramp_profile():
    p=fake_profile()
    p['rf'].update(mode='delta_ramp',hz_per_unit=1e6,timeout_s=.1,settle_s=0.,enable_settle_s=0.,tolerance_hz=1.,control_scheme_confirmed=True)
    p['rf'].pop('set')
    return p


class RecoveredControlTests(unittest.TestCase):
    def test_enable_delay_handles_controller_not_ready_at_on_status(self):
        for delay in (0.,1.):
            with self.subTest(delay=delay):
                now=[0.]
                class ArmingClient(RampClient):
                    ready_at=float('inf')
                    def caput(self,pv,value,**kwargs):
                        if pv=='RAMP:CONTROL_ON_SW':self.ready_at=now[0]+1.
                        if pv.endswith(':ONOFF_SW') and now[0]<self.ready_at:
                            self.calls.append((pv,value))
                            self.values[pv]=value  # CA put succeeds, action is ignored.
                            return 1
                        return super().caput(pv,value,**kwargs)
                client=ArmingClient();profile=ramp_profile()
                if delay:profile['rf'].pop('enable_settle_s')  # Default is 1 s.
                backend=EpicsBackend(profile,client)
                def sleep(seconds):now[0]+=seconds
                with patch('Knobs.DRCorrection.ramp.time.monotonic',side_effect=lambda:now[0]),patch('Knobs.DRCorrection.ramp.time.sleep',side_effect=sleep):
                    if delay:
                        backend.set_frequency(-1000.)
                        self.assertAlmostEqual(client.values['rf'],713.999)
                        backend.set_frequency(1000.)
                        self.assertAlmostEqual(client.values['rf'],714.001)
                        self.assertEqual(client.calls.count(('RAMP:CONTROL_ON_SW',1)),1)
                    else:
                        with self.assertRaisesRegex(RuntimeError,'timeout: preset MI1') as caught:
                            backend.set_frequency(-1000.)
                        message=str(caught.exception)
                        self.assertIn('before restoration:',message)
                        self.assertIn('RAMP:MI1:ONOFF_SW=1',message)
                        self.assertIn('RAMP:MI1:STATUS_READ=0',message)
                        self.assertIn('RAMP:STATUS_READ=1',message)
                        self.assertIn('rf=714.',message)
                        self.assertEqual(client.values['RAMP:STATUS_READ'],0)

    def test_rf_enable_delay_configuration_is_validated_before_writes(self):
        for value in (-1.,float('nan'),float('inf'),'invalid'):
            client=RampClient();profile=ramp_profile();profile['rf']['enable_settle_s']=value
            with self.assertRaisesRegex(ValueError,'rf.enable_settle_s'):EpicsBackend(profile,client)
            self.assertEqual(client.calls,[])

    def test_failed_restoration_preserves_original_error(self):
        client=RampClient();backend=EpicsBackend(ramp_profile(),client)
        client.fail_once='RAMP:MI1:ONOFF_SW'
        with patch.object(backend.ramp,'restore',side_effect=RuntimeError('restore failed')):
            with self.assertRaisesRegex(RuntimeError,'PV write failed: RAMP:MI1:ONOFF_SW; restoration also failed: restore failed'):
                backend.set_frequency(-1000.)

    def test_diagnostics_read_failure_does_not_write_or_mask_error(self):
        client=RampClient();backend=EpicsBackend(ramp_profile(),client)
        with patch.object(backend.ramp,'get',side_effect=RuntimeError('disconnected')):
            message=backend.ramp.diagnostics(('RAMP:MI1:ONOFF_SW',))
        self.assertIn('RAMP:MI1:ONOFF_SW=unavailable (disconnected)',message)
        self.assertEqual(client.calls,[])

    def test_bpm_sampling_waits_first_and_preserves_mean_spread_and_raw_data(self):
        client=FakeEpics();backend=EpicsBackend(fake_profile(),client);service=CorrectionService(backend)
        original=backend.orbit;reads=[]
        def read():
            reads.append(wait.call_count)
            client.values['x']=[float(2*len(reads)-1),10.]
            client.values['y']=[float(len(reads)),20.]
            return original()
        backend.orbit=read
        with patch.object(service.cancel,'wait',return_value=False) as wait:
            x,y,valid=service._orbit(Options(bpm_samples=3,bpm_interval_s=1.))
        self.assertEqual(reads,[1,2,3])
        self.assertEqual([call.args for call in wait.call_args_list],[(1.,)]*3)
        np.testing.assert_allclose(x,[3.,10.]);np.testing.assert_allclose(y,[2.,20.])
        sample=service.orbit_measurements[0]
        np.testing.assert_allclose(sample['stdx'],[np.std([1.,3.,5.]),0.])
        self.assertEqual(sample['samples']['x'].shape,(3,2))
        self.assertEqual(sample['nshots'],3);self.assertFalse(sample['freshness_verified'])
        self.assertTrue(valid.all())

    def test_cancelling_bpm_sample_wait_restores_probe_without_next_read(self):
        backend=EpicsBackend(fake_profile(),FakeEpics());service=CorrectionService(backend)
        calls=[]
        def wait(seconds):
            calls.append(seconds)
            if len(calls)==4:service.cancel.set();return True
            return False
        with patch.object(service.cancel,'wait',side_effect=wait):
            with self.assertRaises(Cancelled):service.calculate('orbit',Options(bpm_samples=3,bpm_interval_s=1.))
        self.assertEqual(backend.acquisitions,3)
        self.assertEqual(backend.read('correctors',['V'])[0],0.)

    def test_invalid_bpm_sampling_is_rejected_before_reads_or_writes(self):
        client=RampClient();backend=EpicsBackend(ramp_profile(),client);service=CorrectionService(backend)
        for options in (Options(bpm_samples=0),Options(bpm_samples=1.5),Options(bpm_interval_s=-1.)):
            with self.assertRaisesRegex(ValueError,'BPM'):service.calculate('orbit',options)
        self.assertEqual(client.calls,[]);self.assertEqual(backend.acquisitions,0)

    def test_invalid_sample_is_not_hidden_by_averaging(self):
        client=FakeEpics();backend=EpicsBackend(fake_profile(),client);service=CorrectionService(backend)
        original=backend.orbit
        def read():
            result=original()
            if backend.acquisitions==2:result['valid'][0]=False
            return result
        backend.orbit=read
        _,_,valid=service._orbit(Options(bpm_samples=3))
        np.testing.assert_equal(valid,[False,True])

    def test_delayed_bpm_response_waits_after_current_readback(self):
        # The IOC current readback updates immediately, but its BPM waveform
        # still contains the previous orbit until the acquisition delay passes.
        for delay in (0.,5.):
            with self.subTest(delay=delay):
                client=FakeEpics();client.values['y']=[1.,1.]
                profile=fake_profile();profile['magnet_settle_s']=delay
                backend=EpicsBackend(profile,client);service=CorrectionService(backend)
                waits=[]
                def settle(seconds):
                    waits.append((seconds,client.values['v']))
                    self.assertEqual(backend.read('correctors',['V'])[0],client.values['v'])
                    if seconds>=5.:client.values['y']=[1.+2.*client.values['v']]*2
                with patch('Knobs.DRCorrection.backends.time.sleep',side_effect=settle):
                    if delay==0.:
                        with self.assertRaisesRegex(ValueError,'No measured response.*V: 0'):
                            service.calculate('orbit',Options(corrector_probe_A=.01))
                    else:
                        data=service.calculate('orbit',Options(corrector_probe_A=.01))
                        np.testing.assert_allclose(data['response'],[[2.],[2.]])
                        self.assertEqual(waits,[(5.,-.01),(5.,.01),(5.,0.)])
                self.assertEqual(client.values['v'],0.)

    def test_settle_cancellation_restores_before_next_bpm_read(self):
        client=FakeEpics();profile=fake_profile();profile['magnet_settle_s']=5.
        backend=EpicsBackend(profile,client);service=CorrectionService(backend)
        def cancel(_):service.cancel.set()
        with patch('Knobs.DRCorrection.backends.time.sleep',side_effect=cancel) as wait:
            with self.assertRaises(Cancelled):service.calculate('orbit',Options())
        self.assertEqual(backend.acquisitions,1)  # Only the initial orbit was read.
        self.assertEqual(client.values['v'],0.)
        self.assertEqual(wait.call_count,2)  # Probe and restoration settle.

    def test_current_drift_during_wait_is_rejected_and_restored(self):
        client=FakeEpics();profile=fake_profile();profile['magnet_settle_s']=5.
        backend=EpicsBackend(profile,client)
        def drift(_):client.values['v']=.5
        with patch('Knobs.DRCorrection.backends.time.sleep',side_effect=drift):
            with self.assertRaisesRegex(RuntimeError,'changed during settling'):
                backend.write('correctors',['V'],[.01])
        self.assertEqual(client.values['v'],0.)

    def test_settling_default_and_invalid_configuration(self):
        client=FakeEpics();profile=fake_profile();profile.pop('magnet_settle_s')
        backend=EpicsBackend(profile,client)
        with patch('Knobs.DRCorrection.backends.time.sleep') as wait:
            backend.write('correctors',['V'],[.01])
        wait.assert_not_called()
        for invalid in (-1.,float('nan'),float('inf')):
            profile['magnet_settle_s']=invalid
            with self.assertRaisesRegex(ValueError,'magnet_settle_s'):EpicsBackend(profile,client)

    def test_nonzero_but_orthogonal_response_reports_solver_diagnostic(self):
        client=FakeEpics();backend=EpicsBackend(fake_profile(),client)
        service=CorrectionService(backend);options=Options()
        data=service.acquire('orbit',options)
        data['response']=np.array([[-1.],[1.]])
        data['measured']=np.array([[1.,1.]])
        with self.assertRaisesRegex(ValueError,'could not reduce.*V: 1'):
            service.calculate('orbit',options,dataset=data)

    def test_limit_pv_order_and_per_magnet_intersection(self):
        p=fake_profile();c=FakeEpics()
        for name,entry in p['actuators'].items():
            entry.update(lower_limit_pv=name+':parameter.A',upper_limit_pv=name+':parameter.B')
            c.values[name+':parameter.A']=-2.;c.values[name+':parameter.B']=3.
        c.values['H:parameter.B']=10.
        p['skew_limits_A']=None
        self.assertEqual(profile_issues(p),[])
        b=EpicsBackend(p,c);lo,hi=b.limits('correctors',['H','V'])
        np.testing.assert_equal(lo,[-2.,-2.]);np.testing.assert_equal(hi,[5.,3.])
        b.write('skews',['SD'],[2.5])
        with self.assertRaises(ValueError):b.write('skews',['SD'],[3.1])
        # Limit changes are re-read before every write, not cached at startup.
        c.values['SD:parameter.B']=2.
        with self.assertRaises(ValueError):b.write('skews',['SD'],[2.1])
        c.values['SD:parameter.A']=float('nan')
        with self.assertRaisesRegex(ValueError,'limit PV'):b.write('skews',['SD'],[0.])
    def test_delta_ramp_routes_to_controller_and_restores_off(self):
        client=RampClient();backend=EpicsBackend(ramp_profile(),client);before=backend.snapshot()
        backend.set_frequency(4000.)
        self.assertEqual(backend.frequency(),4000.)
        self.assertIn(('RAMP:PL4:ONOFF_SW',1),client.calls)
        self.assertTrue(all(pv.startswith('RAMP:') for pv,_ in client.calls))
        self.assertAlmostEqual(client.values['rf'],714.004)
        backend.restore(before)
        self.assertTrue(backend.same_settings(before,backend.snapshot()))
        self.assertEqual(client.values['RAMP:STATUS_READ'],0)
    def test_frequency_record_processing_and_fresh_get(self):
        client=RampClient();p=ramp_profile();p['rf']['read_process_pv']='FREQUENCY:DR_read.PROC'
        backend=EpicsBackend(p,client);backend.set_frequency(1000.)
        self.assertIn(('FREQUENCY:DR_read.PROC',1),client.calls)
        self.assertFalse(any(pv.endswith('_write') for pv,value in client.calls))
    def test_sub_khz_is_rejected_before_any_write(self):
        client=RampClient();backend=EpicsBackend(ramp_profile(),client)
        with self.assertRaisesRegex(ValueError,'integer kHz'):backend.set_frequency(500.)
        self.assertEqual(client.calls,[])
        with self.assertRaisesRegex(ValueError,'integer kHz'):CorrectionService(backend)._signal('dispersion',Options(frequency_change_khz=1.))
        self.assertEqual(client.calls,[])
    def test_dispersion_pair_restores_controller_and_frequency(self):
        client=RampClient();backend=EpicsBackend(ramp_profile(),client);before=backend.frequency_state()
        CorrectionService(backend)._signal('dispersion',Options(frequency_change_khz=2.))
        self.assertEqual(backend.frequency_state(),before)
        self.assertIn(('RAMP:MI1:ONOFF_SW',1),client.calls);self.assertIn(('RAMP:PL1:ONOFF_SW',1),client.calls)
    def test_dispersion_calc_only_restores_probed_magnets(self):
        # One corrector entails nine orbit reads: nominal and +/- RF, repeated
        # at the two corrector probe settings. Fail/cancel at every read.
        cases=[('success',None)]+[(mode,i) for mode in ('failure','cancel') for i in range(1,10)]
        for mode,at in cases:
            with self.subTest(mode=mode,read=at):
                client=RampClient();backend=EpicsBackend(ramp_profile(),client)
                client.values.update(h=.4,v=.7,s=.2)
                service=CorrectionService(backend);before=backend.snapshot()
                original=backend.orbit
                def orbit():
                    result=original()
                    # An unrelated setting changed elsewhere must not be
                    # overwritten by this scan's cleanup.
                    client.values['h']=1.3
                    if backend.acquisitions==at:
                        if mode=='failure':raise RuntimeError('Injected orbit failure')
                        service.cancel.set()
                    return result
                backend.orbit=orbit
                options=Options(frequency_change_khz=2.,corrector_probe_A=.01)
                if mode=='success':
                    data=service.calculate('dispersion',options)
                    self.assertEqual(data['actuators'],['V'])
                    self.assertEqual(backend.acquisitions,9)
                else:
                    with self.assertRaises(RuntimeError if mode=='failure' else Cancelled):
                        service.calculate('dispersion',options)
                expected=before
                expected['correctors']['H']=1.3
                self.assertTrue(backend.same_settings(expected,backend.snapshot()))
                writes=[(pv,value) for pv,value in client.calls if not pv.startswith('RAMP:')]
                self.assertTrue(all(pv=='v' for pv,value in writes),writes)
                self.assertTrue(all(abs(value-.7)<=.010001 for pv,value in writes),writes)
    def test_failed_ramp_preset_restores_active_previous_preset(self):
        client=RampClient();backend=EpicsBackend(ramp_profile(),client)
        backend.set_frequency(-2000.);before=backend.frequency_state()
        client.fail_once='RAMP:PL4:ONOFF_SW'
        with self.assertRaises(RuntimeError):backend.set_frequency(4000.)
        self.assertEqual(backend.frequency_state(),before)


if __name__=='__main__':unittest.main()
