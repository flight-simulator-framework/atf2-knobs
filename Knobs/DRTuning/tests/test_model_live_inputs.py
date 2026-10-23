"""Native calibration with fake EPICS: never connects to the machine."""
import importlib.util
import unittest
from unittest.mock import patch
import copy
from Knobs.DRCorrection.core import Options


@unittest.skipUnless(importlib.util.find_spec('RF_Track'),'RFTrack optional native calibration test')
class LiveModelInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from Knobs.DRTuning.hardware import load_hardware_profile
        from Knobs.DRTuning.calibration import MagnetCalibration
        from machine_interfaces_atf2.DR_ATF2.ATF_DR_RFTrack_lattice import build_atf_dr_lattice
        cls.profile=load_hardware_profile();p=cls.profile
        cls.values={'SAD:FILE:DR:ENERGY':1.3,'SAD:DR_TUNE_X':.0625,'SAD:DR_TUNE_Y':.1361,
                    'SAD:SKEWCOR_SDCALIB':.0002,'SAD:SKEWCOR_SFCALIB':.0001}
        cal=MagnetCalibration();ring=build_atf_dr_lattice(momentum_mev_c=1300.)
        for e in ring['*']:
            n=e.get_name()
            if type(e).__name__=='Quadrupole':
                hardware='QM7R.1' if n=='QM7AR.1' else n;c=p['quadrupoles'][hardware]['calibration']
                cls.values[c['main_current_pv']]=cal.mag.k1l_to_current(c['magnet'],e.get_K1L(-1300.),1.3)
            elif type(e).__name__=='Sextupole':
                family=n.split('.')[0]
                cls.values[family+':internalCurrentWrite']=cal.mag.k1l_to_current(family,e.get_K2L(-1300.),1.3)
        for entry in list(p['quadrupoles'].values())+list(p['actuators'].values()):
            cls.values[entry['read']]=0.;cls.values[entry['lower_limit_pv']]=-5.;cls.values[entry['upper_limit_pv']]=5.
    def setUp(self):
        from Knobs.DRTuning.epics import LiveBackend
        values=self.values.copy()
        class Client:
            writes=[]
            def caget(self,pv,**kw):return values[pv]
            def caput(self,*args,**kw):self.writes.append(args);raise AssertionError('Model capture wrote a PV')
        self.client=Client();self.backend=LiveBackend(self.profile,self.client)
        b=self.backend;self.baseline=dict(correctors={n:0. for n in b.hcorrectors+b.vcorrectors},skews={n:0. for n in b.skews},frequency=0.)
    def test_full_coil_import_alias_and_explicit_unmapped_exclusions(self):
        from Knobs.DRTuning.model import capture_settings,OpticsModel
        settings=capture_settings(self.backend,Options(excluded_bpms=('MB97R','MB98R'),fixed_actuators=('ZH15R','ZV16R')),self.baseline)
        model=OpticsModel(settings)
        self.assertEqual(len(settings['quadrupoles_k1l']),100)
        self.assertEqual(settings['coils']['QM7AR.1']['hardware_name'],'QM7R.1')
        self.assertEqual(set(settings['unmodelled_fixed_actuators']),{'ZH15R','ZV16R'})
        self.assertEqual(len(model.bpm_names),94)
        self.assertTrue(all(v<=0 for n,v in settings['sextupoles_k2l'].items() if n.startswith('SD1R')))
        self.assertEqual(self.client.writes,[])
    def test_unknown_active_magnet_fails_without_writing(self):
        from Knobs.DRTuning.model import capture_settings
        with self.assertRaisesRegex(ValueError,'Actuator absent'):
            capture_settings(self.backend,Options(),self.baseline)
        self.assertEqual(self.client.writes,[])
    def test_profile_calibration_library_is_used(self):
        from Knobs.DRTuning.model import capture_settings
        from machine_interfaces_atf2.MagKi import MagKiWrapper
        from Knobs.DRTuning.calibration import build_library
        library=str(build_library());self.backend.quadrupoles=copy.deepcopy(self.backend.quadrupoles)
        self.backend.quadrupoles['QM7R.1']['calibration']['library']=library
        with patch('machine_interfaces_atf2.MagKi.MagKiWrapper',wraps=MagKiWrapper) as wrapper:
            settings=capture_settings(self.backend,Options(excluded_bpms=('MB97R','MB98R'),fixed_actuators=('ZH15R','ZV16R')),self.baseline)
        self.assertIn(unittest.mock.call(library),wrapper.call_args_list)
        self.assertEqual(settings['coils']['QM7AR.1']['library'],library)
        self.assertEqual(self.client.writes,[])


if __name__=='__main__':unittest.main()
