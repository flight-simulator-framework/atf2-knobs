import copy
import json
import tempfile
from pathlib import Path
import unittest
import numpy as np
from Knobs.DRCorrection.backends import DemoBackend,EpicsBackend,HERE,profile_issues
from Knobs.DRCorrection.core import CorrectionService,Options,Cancelled,save_session,load_session,export_curves
from Knobs.DRCorrection.dataio import parse_sad_orbits,aligned_difference,fit_sr,load_tbt_text
from Knobs.DRCorrection.tbt import analyze_tbt


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.b=DemoBackend();self.s=CorrectionService(self.b)
        self.o=Options(probe1=self.b.hcorrectors[0],probe2=self.b.hcorrectors[6])
    def test_ported_proposals_apply_and_reset(self):
        baseline=self.b.snapshot()
        for mode in ('orbit','dispersion','skew'):
            with self.subTest(mode=mode):
                d=self.s.calculate(mode,self.o)
                self.assertIn('ATFDRRingCorrection.propose_',d['solver'])
                self.assertEqual(baseline,self.b.snapshot())
                self.assertLess(np.linalg.norm(d['predicted']),np.linalg.norm(d['measured']))
                self.s.apply(d);np.testing.assert_allclose(d['after'],d['predicted'],atol=1e-10)
                np.testing.assert_allclose(d['full_after'],d['full_predicted'],atol=1e-10)
                with self.assertRaisesRegex(ValueError,'Already applied'):self.s.apply(d)
                self.s.reset();self.assertEqual(baseline,self.b.snapshot())
    def test_stale_proposal_and_absolute_limits(self):
        d=self.s.calculate('orbit',self.o)
        self.b.write('correctors',[self.b.vcorrectors[0]],[.01])
        with self.assertRaisesRegex(ValueError,'Settings changed'):self.s.apply(d)
        self.b.write('correctors',[self.b.vcorrectors[0]],[5.])
        d=self.s.calculate('orbit',self.o)
        self.assertIn('Asymmetric',d['solver'])
        self.assertLessEqual(max(d['centers']+d['delta']),5.)
        with self.assertRaises(ValueError):self.b.write('correctors',[self.b.vcorrectors[0]],[5.01])
    def test_cancel_restores_every_scan(self):
        baseline=self.b.snapshot()
        for mode in ('orbit','dispersion','skew'):
            with self.subTest(mode=mode),self.assertRaises(Cancelled):
                self.s.calculate(mode,self.o,lambda *_:self.s.cancel.set())
            self.assertEqual(baseline,self.b.snapshot())
    def test_read_failure_restores_rf_and_correctors(self):
        baseline=self.b.snapshot();original=self.b.orbit
        def failing():
            result=original()
            if self.b.acquisitions==5:raise RuntimeError('Injected acquisition failure')
            return result
        self.b.orbit=failing
        with self.assertRaisesRegex(RuntimeError,'Injected'):self.s.calculate('dispersion',self.o)
        self.assertEqual(baseline,self.b.snapshot())
    def test_failed_set_rolls_back_and_does_not_push_undo(self):
        d=self.s.calculate('skew',self.o);baseline=self.b.snapshot()
        def fail():raise RuntimeError('Lost beam')
        self.b.orbit=fail
        with self.assertRaisesRegex(RuntimeError,'Lost beam'):self.s.apply(d)
        self.assertEqual(baseline,self.b.snapshot());self.assertFalse(self.s.undo);self.assertFalse(d['applied'])
    def test_files_are_offline_and_plane_checked(self):
        d=self.s.calculate('dispersion',self.o);count=self.b.acquisitions
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'disp.json';save_session(path,d);export_curves(Path(root)/'disp.csv',d)
            loaded=load_session(path);recalculated=self.s.calculate('dispersion',self.o,dataset=loaded)
            np.testing.assert_allclose(recalculated['delta'],d['delta'])
            self.assertEqual(count,self.b.acquisitions)
            self.o.plane='x'
            with self.assertRaisesRegex(ValueError,'same H/V'):self.s.calculate('dispersion',self.o,dataset=loaded)
    def test_recalculation_discards_old_after_measurement(self):
        data=self.s.calculate('orbit',self.o);self.s.apply(data)
        recalculated=self.s.calculate('orbit',self.o,dataset=data)
        self.assertNotIn('after',recalculated);self.assertNotIn('full_after',recalculated)
    def test_exclusions_and_invalid_readings(self):
        self.o.excluded_bpms=(self.b.bpm_names[0],)
        self.o.fixed_actuators=tuple(self.b.vcorrectors[2:])
        d=self.s.calculate('orbit',self.o)
        self.assertFalse(d['valid'][0]);self.assertEqual(d['actuators'],self.b.vcorrectors[:2])
    def test_invalid_options_rejected_before_scan(self):
        self.o.svd_cut=float('nan')
        with self.assertRaises(ValueError):self.s.calculate('orbit',self.o)
        self.assertEqual(self.b.acquisitions,0)
    def test_sr_fit_recovers_synthetic_dispersion(self):
        data=self.s.calculate('dispersion',self.o);result=fit_sr(data,self.b.sr_calibration())
        self.assertLess(result['planes']['x']['rms_residual_mm'],1e-8)
        self.assertLess(result['planes']['y']['rms_residual_mm'],1e-8)
        bad=self.b.sr_calibration();bad['planes']['x']['bpm_response']=np.ones((96,8))
        with self.assertRaisesRegex(ValueError,'rank deficient'):fit_sr(data,bad)


class DataTests(unittest.TestCase):
    def test_sad_parser_units_mapping_and_reject_expression(self):
        text='ATFData={{ATFBPM -> {{"M.1",1.2e-3,-2*^-4,7},{"M.2",0,0,9}}}};'
        data=parse_sad_orbits(text,{'M.1':'MB1R','M.2':'MB2R'})[0]
        np.testing.assert_allclose(data['x'],[1.2,0]);np.testing.assert_allclose(data['y'],[-.2,0])
        with self.assertRaisesRegex(ValueError,'mapping'):parse_sad_orbits(text,{})
        with self.assertRaises(ValueError):parse_sad_orbits(text.replace('1.2e-3','Run["anything"]'),{'M.1':'MB1R','M.2':'MB2R'})
    def test_difference_aligns_by_name(self):
        a=dict(names=['a','b'],x=[1,2],y=[3,4],tmit=[1,1],valid=[True,True],position_unit='mm')
        b=dict(names=['b','a'],x=[4,3],y=[6,5],tmit=[1,1],valid=[True,True],position_unit='mm')
        d=aligned_difference(a,b);np.testing.assert_allclose(d['x'],[2,2]);np.testing.assert_allclose(d['y'],[2,2])
    def test_tbt_tunes_and_relative_beta(self):
        data=DemoBackend().tbt(128);result=analyze_tbt(data,start_turn=3)
        for axis,tune in [('x',.205),('y',.415)]:
            plane=result['planes'][axis];self.assertTrue(plane['valid']);self.assertAlmostEqual(plane['tune'],tune,3);self.assertIsNone(plane['beta_m'])
        with tempfile.TemporaryDirectory() as root:
            paths={key:Path(root)/(key+'.txt') for key in ('x','y','tmit')}
            for key,path in paths.items():np.savetxt(path,data[key])
            loaded=load_tbt_text(paths,data['names'],mm_per_unit=1.,noise_mm=.005)
            np.testing.assert_allclose(loaded['x'],data['x'])
            for key,path in paths.items():
                path.write_text(' '.join(f'bpm{i+1:02d}_'+('wint' if key=='tmit' else key) for i in range(96))+'\n'+path.read_text())
            loaded=load_tbt_text(paths,data['names'],mm_per_unit=1.,noise_mm=.005)
            np.testing.assert_allclose(loaded['y'],data['y'])


class FakeEpics:
    def __init__(self):self.values={'h':0.,'v':0.,'s':0.,'rf':714e6,'x':[0.,0.],'y':[0.,0.],'q':[1.,1.]};self.fail=None
    def caget(self,pv,**_):return self.values[pv]
    def caput(self,pv,value,**_):
        if pv==self.fail:self.fail=None;return -1
        self.values[pv]=value;return 1


def fake_profile():
    return dict(machine_id='TEST',bpm_mapping_confirmed=True,bpm_names=['b1','b2'],positions_m=[0.,1.],
        hcorrectors=['H'],vcorrectors=['V'],skews=['SD'],nominal_frequency_hz=714e6,
        actuators={n:dict(set=p,read=p) for n,p in [('H','h'),('V','v'),('SD','s')]},
        orbit=dict(x='x',y='y',tmit='q',mm_per_unit=1.),rf=dict(set='rf',read='rf',hz_per_unit=1.,settle_s=0.,tolerance_hz=1.),
        target_dispersion_mm=dict(x=[100.,100.],y=[0.,0.]),skew_limits_A=[-1.,1.],readback_timeout_s=.01,current_tolerance_A=.001,magnet_settle_s=0.)


class EpicsTests(unittest.TestCase):
    def test_incomplete_profile_never_connects(self):
        example=json.loads((HERE/'hardware_profile.example.json').read_text())
        self.assertTrue(profile_issues(example))
        with self.assertRaisesRegex(ValueError,'Incomplete hardware profile'):EpicsBackend(example)
    def test_write_readback_and_rollback_fake_only(self):
        client=FakeEpics();b=EpicsBackend(fake_profile(),client)
        original=b.snapshot();b.write('correctors',['H'],[.2]);self.assertAlmostEqual(b.read('correctors',['H'])[0],.2)
        b.restore(original);self.assertEqual(b.snapshot(),original)
        client.fail='v'
        with self.assertRaises(RuntimeError):b.write('correctors',['H','V'],[.2,.3])
        self.assertEqual(b.snapshot(),original)
        with self.assertRaises(ValueError):b.write('correctors',['H'],[5.1])
        b.set_frequency(500.);self.assertEqual(b.frequency(),500.);b.restore(original)
    def test_nan_readback_rejected(self):
        client=FakeEpics();b=EpicsBackend(fake_profile(),client)
        original=client.caget
        client.caget=lambda pv,**kwargs:float('nan') if pv=='h' else original(pv,**kwargs)
        with self.assertRaises(RuntimeError):b.write('correctors',['H'],[.1])
    def test_bad_profile_shape(self):
        p=fake_profile();p['positions_m']=[0.];self.assertTrue(profile_issues(p))
    def test_packed_existing_interface_mapping(self):
        p=fake_profile();p['orbit']=dict(packed_pv='packed',row_width=10,row_indices=[0,2],columns=dict(status=0,x=1,y=2,tmit=3),valid_status=1,mm_per_unit=.001)
        c=FakeEpics();c.values['packed']=[1,100,200,3,0,0,0,0,0,0, 1,400,500,6,0,0,0,0,0,0, 0,700,800,9,0,0,0,0,0,0]
        b=EpicsBackend(p,c);orbit=b.orbit()
        np.testing.assert_allclose(orbit['x'],[.1,.7]);np.testing.assert_array_equal(orbit['valid'],[True,False])
    def test_tbt_per_bpm_channels_and_unit_conversion(self):
        p=fake_profile();p['tbt']=dict(layout='per_bpm',mapping_confirmed=True,names=['one','two'],x=['x1','x2'],y=['y1','y2'],tmit=['q1','q2'],mm_per_unit=.001,noise_mm=.005)
        c=FakeEpics()
        for k in ('x1','x2','y1','y2','q1','q2'):c.values[k]=np.arange(32)+1.
        wave=EpicsBackend(p,c).tbt(16)
        self.assertEqual(wave['names'],['one','two']);self.assertEqual(wave['x'].shape,(16,2));self.assertEqual(wave['x'][0,0],.001)
        p['tbt']['mapping_confirmed']=False
        with self.assertRaisesRegex(ValueError,'Confirm TBT'):EpicsBackend(p,c).tbt(16)


if __name__=='__main__':unittest.main()
