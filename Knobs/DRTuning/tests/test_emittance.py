import json
import math
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock
import numpy as np
from Knobs.DRTuning.emittance import estimate_emittance,read_xsr_sizes,sr_beta_values,sr_dispersion_values,XSR_SIZE_PVS


class EmittanceTests(unittest.TestCase):
    def test_recovers_known_geometric_emittance_and_apparent_value(self):
        # beta=3 m, eps=12 pm, eta=2 mm, dp/p=.001 -> sigma²=40 um².
        r=estimate_emittance(math.sqrt(40),3,2,.001,correct_dispersion=True)
        self.assertAlmostEqual(r['emittance_pm_rad'],12.)
        self.assertAlmostEqual(r['emittance_m_rad'],12.e-12)
        self.assertAlmostEqual(r['apparent_pm_rad'],40/3)
        self.assertEqual(r['dispersive_variance_um2'],4.)
        self.assertEqual(r['resolution_variance_um2'],0.)
        self.assertIsNone(r['uncertainty_pm_rad'])
        apparent=estimate_emittance(math.sqrt(40),3,2,.001)
        self.assertAlmostEqual(apparent['emittance_pm_rad'],40/3)
        self.assertIn('not removed',apparent['estimate_kind'])

    def test_independent_uncertainties_match_numerical_derivatives(self):
        values=dict(sigma_um=8.,beta_m=3.,dispersion_mm=-2.,sigma_delta=.001,resolution_um=1.)
        errors=dict(sigma_um=.1,beta_m=.05,dispersion_mm=.1,sigma_delta=.0001,resolution_um=.05)
        r=estimate_emittance(**values,correct_dispersion=True,uncertainties=errors)
        variance=0.
        for key in values:
            lo=values.copy();hi=values.copy();step=max(abs(values[key])*1.e-5,1.e-10)
            lo[key]-=step;hi[key]+=step
            derivative=(estimate_emittance(**hi,correct_dispersion=True)['emittance_pm_rad']-estimate_emittance(**lo,correct_dispersion=True)['emittance_pm_rad'])/(2*step)
            variance+=(derivative*errors[key])**2
        self.assertAlmostEqual(r['uncertainty_pm_rad'],math.sqrt(variance),places=7)

    def test_nonphysical_inputs_and_unresolved_signal(self):
        for values in ((0,3),(2,0),(float('nan'),3)):
            with self.assertRaises(ValueError):estimate_emittance(*values)
        with self.assertRaisesRegex(ValueError,'positive RMS'):estimate_emittance(2,3,2,0,correct_dispersion=True)
        r=estimate_emittance(1,3,2,.001,correct_dispersion=True)
        self.assertIsNone(r['emittance_pm_rad']);self.assertLess(r['betatron_variance_um2'],0)
        self.assertIn('unresolved',r['status']);json.dumps(r,allow_nan=False)

    def test_read_only_corrected_pvs_no_double_factor(self):
        # No horizontal PV is available: the vertical estimate must still work.
        values={'XSR:PROJ_V_SIGMA':6.};get=Mock(side_effect=values.__getitem__)
        reading=read_xsr_sizes(get)
        self.assertEqual(reading['sigma_um'],dict(y=6.));get.assert_called_once_with('XSR:PROJ_V_SIGMA')
        self.assertTrue(reading['resolution_corrected']);self.assertFalse(reading['fresh_shot_verified'])
        self.assertEqual(estimate_emittance(reading['sigma_um']['y'],3)['emittance_pm_rad'],12.)
        with self.assertRaises(ValueError):read_xsr_sizes(lambda _:0.)

    def test_measured_beta_transport_to_size_estimate(self):
        from Knobs.DRTuning.sr_beta import fit_sr_beta
        beta=4.;alpha=.3;gamma=(1+alpha*alpha)/beta
        names=['q1','q2','q3','q4'];positions=[0.,1.,2.,3.]
        measured=[beta-2*s*alpha+s*s*gamma for s in positions]
        cal=dict(schema='dr-sr-beta-1',source='Independent drift fixture',
                 bpms=[dict(name=n,r11=1.,r12_m=s) for n,s in zip(names,positions)],
                 monitors=[dict(name='SR',r11=1.,r12_m=1.5)])
        fit=fit_sr_beta(names,measured,cal);local=sr_beta_values({'y':fit},'SR')['y']
        true_local=beta-3*alpha+2.25*gamma
        size=math.sqrt(12*true_local+4)
        r=estimate_emittance(size,local,2,.001,correct_dispersion=True)
        self.assertAlmostEqual(r['emittance_pm_rad'],12.,places=5)
        with self.assertRaises(ValueError):sr_beta_values({'y':fit},'SR2')
        disp=dict(monitors=[dict(name='SR2'),dict(name='SR')],planes={'y':dict(monitor_dispersion=[99.,2.])})
        self.assertEqual(sr_dispersion_values(disp,'SR'),{'y':2.})


class EmittanceGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def test_gui_read_fit_estimate_save_and_invalidation(self):
        from Knobs.DRTuning.gui import TuningSession
        from Knobs.DRCorrection.backends import DemoBackend
        backend=DemoBackend();backend.read_xsr_sizes=lambda:read_xsr_sizes({'XSR:PROJ_V_SIGMA':6.}.__getitem__)
        w=TuningSession(backend);w.error=Mock()
        w.start=lambda action,done,**kw:done(action(lambda *_:None))
        try:
            p=w.emittance_panel;w.tool_actions['emittance'].trigger()
            self.assertIs(w.tabs.currentWidget(),p)
            self.assertEqual(set(p.fields),{'y'})
            p.import_beta({'x':None,'y':dict(monitors=[dict(name='SR',beta_m=3.)])})
            p.import_dispersion(dict(monitors=[dict(name='SR')],planes={'x':None,'y':dict(monitor_dispersion=[2.])}))
            p.fields['y']['resolution_um'].setValue(2.);p.read_sizes()
            self.assertEqual(p.fields['y']['resolution_um'].value(),0.)
            p.calculate();self.assertIsNone(p.result);w.error.assert_called_once()
            w.error.reset_mock();p.same_optics.setChecked(True);p.calculate();w.error.assert_not_called()
            self.assertEqual(p.result['planes']['y']['emittance_pm_rad'],12.)
            self.assertEqual(set(p.result['planes']),{'y'})
            self.assertIsNone(p.result['planes']['y']['uncertainty_pm_rad'])
            with tempfile.TemporaryDirectory() as directory:
                p.save(directory);saved=json.loads((Path(directory)/'xsr_emittance.json').read_text())
                self.assertEqual(saved['planes']['y']['emittance_pm_rad'],12.)
                self.assertTrue((Path(directory)/'xsr_emittance.png').exists())
            p.monitor.setCurrentText('SR2');self.assertIsNone(p.result)
            self.assertEqual(p.fields['y']['beta_m'].value(),0.);self.assertFalse(p.same_optics.isChecked())
        finally:w.close();w.deleteLater();self.app.processEvents()


if __name__=='__main__':unittest.main()
