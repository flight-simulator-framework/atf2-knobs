"""Measured scans continue past named readback faults (offline PVs only)."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import numpy as np
from Knobs.DRCorrection.backends import EpicsBackend,MagnetReadbackError
from Knobs.DRCorrection.core import CorrectionService,Options,save_session,load_session
from Knobs.DRTuning.epics import LiveBackend
from test_corrections import FakeEpics,fake_profile


class FaultClient(FakeEpics):
    def __init__(self,fault='missing'):
        super().__init__();self.fault=fault;self.commands=[]
        self.values.update(v2=0.,v3=0.,v_set=0.,quad=0.)
        if fault=='missing':self.values['v']=None
        if fault=='nan':self.values['v']=np.nan
    def caget(self,pv,**kwargs):
        if pv=='y':return np.array([.15,-.1])+self.values['v2']*np.array([1.,-1.])+self.values['v3']*np.array([.5,1.])
        return super().caget(pv,**kwargs)
    def caput(self,pv,value,**kwargs):
        self.commands.append((pv,value))
        if pv=='v_set':
            self.values[pv]=value
            if self.fault=='unrestored':self.values['v']=.02
            elif self.fault=='lost':self.values['v']=None
            elif self.fault=='put':return -1
            elif self.fault not in ('stuck','missing','nan'):self.values['v']=value
            return 1
        return super().caput(pv,value,**kwargs)


def setup(fault='missing',live=False):
    profile=fake_profile();profile['vcorrectors']=['V','V2','V3']
    profile['actuators'].update(V=dict(set='v_set',read='v'),V2=dict(set='v2',read='v2'),V3=dict(set='v3',read='v3'))
    profile['readback_timeout_s']=.001
    if live:profile['quadrupoles']={'Q':dict(read='quad',set='quad',mapping_confirmed=True)}
    client=FaultClient(fault);backend=(LiveBackend if live else EpicsBackend)(profile,client)
    return client,backend,CorrectionService(backend)


class SkipReadbackTests(unittest.TestCase):
    def test_initial_bad_readback_skipped_and_healthy_set_reset_and_save(self):
        for fault in ('missing','nan'):
            with self.subTest(fault=fault):
                client,backend,service=setup(fault)
                data=service.calculate('orbit',Options())
                self.assertEqual(data['actuators'],['V2','V3'])
                self.assertEqual(data['response'].shape,(2,2))
                self.assertEqual(data['ignored_readbacks'],['V'])
                self.assertNotIn('V',data['baseline']['correctors'])
                self.assertEqual(data['scan_errors'][0]['restoration'],'not moved')
                self.assertFalse(data['set_blocked_reason'])
                self.assertNotIn('v_set',[p for p,v in client.commands])
                with tempfile.TemporaryDirectory() as root:
                    path=Path(root)/'scan.json';save_session(path,data)
                    self.assertEqual(load_session(path)['scan_errors'],data['scan_errors'])
                service.apply(data)
                self.assertTrue(data['applied'])
                service.reset()
                self.assertNotIn('v_set',[p for p,v in client.commands])
                self.assertEqual(client.values['v2'],0.)
                self.assertEqual(client.values['v3'],0.)

    def test_snapshot_fault_in_unselected_quad_does_not_stop_measured_scan(self):
        client,backend,service=setup('healthy',live=True);client.values['quad']=None
        data=service.calculate('orbit',Options())
        self.assertEqual(data['ignored_readbacks'],['Q'])
        self.assertEqual(data['actuators'],['V','V2','V3'])
        service.apply(data)
        self.assertNotIn('quad',[p for p,v in client.commands])
        # General/model snapshots still reject missing optics inputs.
        with self.assertRaises(MagnetReadbackError):backend.snapshot()

    def test_timeout_restored_skips_column_and_continues_set(self):
        client,backend,service=setup('stuck');messages=[]
        data=service.calculate('orbit',Options(),lambda _,text:messages.append(text))
        self.assertEqual(data['actuators'],['V2','V3'])
        self.assertEqual(len(data['scan_errors']),1)
        self.assertTrue(data['scan_errors'][0]['restored'])
        self.assertIn('did not settle',data['scan_errors'][0]['error'])
        self.assertTrue(any(m.startswith('SKIP V:') for m in messages))
        self.assertEqual(client.values['v_set'],0.)
        client.commands.clear();service.apply(data);service.reset()
        self.assertNotIn('v_set',[p for p,v in client.commands])

    def test_restoration_unknown_continues_but_blocks_set(self):
        for fault in ('unrestored','lost'):
            with self.subTest(fault=fault):
                client,backend,service=setup(fault)
                data=service.calculate('orbit',Options())
                self.assertEqual(data['actuators'],['V2','V3'])
                self.assertFalse(data['scan_errors'][0]['restored'])
                self.assertTrue(data['set_blocked_reason'])
                # Even when readback disappears, the original command is sent.
                self.assertEqual(client.values['v_set'],0.)
                self.assertIn('v3',[p for p,v in client.commands])
                client.commands.clear()
                with self.assertRaisesRegex(ValueError,'restoration unverified'):service.apply(data)
                self.assertEqual(client.commands,[])

    def test_later_readback_fault_and_stale_settings_still_block_set(self):
        for value in (None,.3):
            with self.subTest(value=value):
                client,backend,service=setup()
                data=service.calculate('orbit',Options());client.values['v2']=value;client.commands.clear()
                with self.assertRaises((MagnetReadbackError,ValueError)):service.apply(data)
                self.assertEqual(client.commands,[])

    def test_global_bpm_and_write_errors_are_not_swallowed(self):
        for fault in ('bpm','put'):
            with self.subTest(fault=fault):
                client,backend,service=setup(fault)
                if fault=='bpm':client.values['q']=[0.,0.]
                with self.assertRaises((ValueError,RuntimeError)):service.calculate('orbit',Options())
                self.assertNotIn('v2',[p for p,v in client.commands])

    def test_all_failed_gives_details_and_no_proposal(self):
        client,backend,service=setup();client.values['v2']=None;client.values['v3']=None
        with self.assertRaisesRegex(RuntimeError,'No usable response magnets') as caught:
            service.calculate('orbit',Options())
        for name in ('V','V2','V3'):self.assertIn(name,str(caught.exception))
        self.assertEqual(len(service.last_scan_errors),3)
        self.assertEqual(client.commands,[])

    def test_skew_excitation_failure_is_not_attributed_to_skew_supply(self):
        client,backend,service=setup('healthy')
        backend.hcorrectors.append('H2')
        backend.mapping['H2']=dict(set='h2',read='h2');client.values['h2']=0.
        original=backend.write
        def write(kind,names,values):
            if names==['H'] and values[0]!=0:raise MagnetReadbackError(['H'],'H diagnostic failed')
            return original(kind,names,values)
        with patch.object(backend,'write',side_effect=write):
            with self.assertRaisesRegex(MagnetReadbackError,'H diagnostic failed'):
                service.calculate('skew',Options(probe1='H',probe2='H2'))
        self.assertEqual(service.last_scan_errors,[])


if __name__=='__main__':unittest.main()
