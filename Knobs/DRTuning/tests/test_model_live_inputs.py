"""Native calibration with fake EPICS: never connects to the machine."""
import importlib.util
import unittest
from unittest.mock import patch
import copy
import numpy as np
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
        self.values_live=values
        b=self.backend;self.baseline=dict(correctors={n:0. for n in b.hcorrectors+b.vcorrectors},skews={n:0. for n in b.skews},frequency=0.)
    def test_full_coil_import_alias_and_explicit_unmapped_exclusions(self):
        from Knobs.DRTuning.model import capture_settings,OpticsModel
        self.backend.model_config={'preparation':'all_currents'}
        settings=capture_settings(self.backend,Options(excluded_bpms=('MB97R','MB98R'),fixed_actuators=('ZH15R','ZV16R')),self.baseline)
        model=OpticsModel(settings)
        self.assertEqual(len(settings['quadrupoles_k1l']),100)
        self.assertEqual(settings['coils']['QM7AR.1']['hardware_name'],'QM7R.1')
        self.assertEqual(set(settings['unmodelled_fixed_actuators']),{'ZH15R','ZV16R'})
        self.assertEqual(len(model.bpm_names),94)
        self.assertTrue(all(v<=0 for n,v in settings['sextupoles_k2l'].items() if n.startswith('SD1R')))
        np.testing.assert_allclose(model.tunes(),[.0625,.1361],atol=1.e-4)
        self.assertEqual(self.client.writes,[])
    def test_sad_reference_factors_trim_policy_and_absolute_current_centers(self):
        from Knobs.DRTuning.model import capture_settings,OpticsModel
        for n in ('QF1R.1','QM7R.1','QM8R.1'):
            self.values_live[self.backend.quadrupoles[n]['read']]=.03
        self.baseline['correctors']['ZH1R']=2.
        self.baseline['skews'][self.backend.skews[0]]=.1
        settings=capture_settings(self.backend,Options(excluded_bpms=('MB97R','MB98R'),fixed_actuators=('ZH15R','ZV16R')),self.baseline)
        self.assertEqual(settings['preparation'],'sad_cod')
        self.assertFalse(settings['coils']['QF1R.1']['trim_included'])
        self.assertFalse(settings['coils']['QM7AR.1']['trim_included'])
        self.assertTrue(settings['coils']['QM8R.1']['trim_included'])
        self.assertEqual(settings['quadrupole_factors']['QM8R.1'],.9683762232693006)
        model=OpticsModel(settings)
        np.testing.assert_allclose(model.read('correctors',['ZH1R']),[2.])
        orbit=model.orbit();np.testing.assert_allclose(orbit['x'],0.,atol=1.e-8)
        np.testing.assert_allclose(orbit['y'],0.,atol=1.e-8)
        q=model.elements['QM8R.1']
        self.assertAlmostEqual(q.get_K1L(-model.momentum),settings['quadrupoles_k1l']['QM8R.1']*.9683762232693006)
        model.write('correctors',['ZH1R'],[2.01])
        self.assertGreater(np.max(abs(model.orbit()['x'])),1.e-6)
        model.write('correctors',['ZH1R'],[2.])
        # These synthetic main currents were inverted from the uncorrected
        # design, not measured historical operating currents. Do not assert
        # that archived factors reproduce that design's tunes within 5%.
        target=model.tunes()+[.005,-.005]
        report=model.match_tunes(target)
        np.testing.assert_allclose(report['final'],target,atol=1.e-5)
        from Knobs.DRCorrection.core import CorrectionService
        data=CorrectionService(model).acquire('orbit',Options(plane='x',
            fixed_actuators=tuple(n for n in model.hcorrectors if n!='ZH1R')))
        self.assertEqual(data['actuators'],['ZH1R'])
        np.testing.assert_allclose(data['centers'],[2.])
        np.testing.assert_allclose(data['limits'],[[-5.],[5.]])
        self.assertGreater(np.linalg.norm(data['response']),1.e-6)
        np.testing.assert_allclose(model.read('correctors',['ZH1R']),[2.])
        self.assertEqual(self.client.writes,[])
    def test_native_unstable_import_recovers_without_machine_writes(self):
        from Knobs.DRTuning.model import capture_settings,OpticsModel,ModelOpticsError
        # Pin the original recovery fixture to the uncorrected bend reference.
        self.backend.model_config={'bend_k1l':{}}
        settings=capture_settings(self.backend,Options(excluded_bpms=('MB97R','MB98R'),
            fixed_actuators=('ZH15R','ZV16R')),self.baseline)
        model=OpticsModel(settings);target=model.tunes()
        # A 1% QF2 error makes this imported reference vertically unstable.
        # Recovery limits remain relative to this perturbed imported model.
        for n,e in model.elements.items():
            if n.startswith('QF2R.') and type(e).__name__=='Quadrupole':
                e.set_K1(-model.momentum,e.get_K1(-model.momentum)*1.01)
        with self.assertRaises(ModelOpticsError) as caught:model.tunes()
        self.assertGreater(caught.exception.diagnostics['max_modulus_deviation'],.1)
        report=model.match_tunes(target)
        np.testing.assert_allclose(model.tunes(),target,atol=1.e-5)
        self.assertLessEqual(np.max(abs(report['family_fractional_changes'])),.05)
        self.assertGreater(report['initial_stable_point_search']['evaluations'],0)
        self.assertEqual(self.client.writes,[])

    def test_sad_common_ring_initialization_imports_bend_gradients(self):
        from Knobs.DRTuning.model import capture_settings,OpticsModel,preparation_config
        settings=capture_settings(self.backend,Options(excluded_bpms=('MB97R','MB98R'),
            fixed_actuators=('ZH15R','ZV16R')),self.baseline)
        self.assertEqual(len(settings['bend_k1l']),36)
        model=OpticsModel(settings)
        for i in range(1,37):
            expected=-1.17442 if i<=6 else -1.14780
            self.assertAlmostEqual(model.elements[f'BH1R.{i}'].get_K1L(),expected)
        original=copy.deepcopy(settings);original['bend_k1l']={}
        uncorrected=OpticsModel(original)
        self.assertGreater(np.linalg.norm(model.tunes()-uncorrected.tunes()),.01)
        self.backend.model_config={'quadrupole_factors':{}}
        self.assertEqual(len(preparation_config(self.backend)['bend_k1l']),36)
        self.backend.model_config={'bend_k1l':{'BH1R.7':-1.15}}
        self.assertEqual(preparation_config(self.backend)['bend_k1l'],{'BH1R.7':-1.15})
        self.backend.model_config={'preparation':'all_currents'}
        self.assertNotIn('bend_k1l',preparation_config(self.backend))
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
