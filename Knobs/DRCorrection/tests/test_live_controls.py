"""Offline tests for recovered op-base controls; all PVs are simulated."""
import unittest
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
    p['rf'].update(mode='delta_ramp',hz_per_unit=1e6,timeout_s=.1,settle_s=0.,tolerance_hz=1.,control_scheme_confirmed=True)
    p['rf'].pop('set')
    return p


class RecoveredControlTests(unittest.TestCase):
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
