"""Offline tests of observable/model separation, bounds and stale inputs."""
import copy
import json
import unittest
from unittest.mock import patch
import numpy as np
from Knobs.DRCorrection.backends import DemoBackend
from Knobs.DRCorrection.core import CorrectionService,Options
from Knobs.DRTuning.model import ModelManager,normal_modes,verify_inputs,digest
from Knobs.DRTuning.sr_beta import fit_sr_beta
from Knobs.DRTuning.model_validation import compare_optics,compare_response


class CountedBackend(DemoBackend):
    def __init__(self):super().__init__();self.commands=[]
    def write(self,kind,names,values):
        self.commands.append((kind,list(names),list(values)));super().write(kind,names,values)
    def set_frequency(self,value):
        self.commands.append(('rf',value));super().set_frequency(value)


class ModelRoutingTests(unittest.TestCase):
    def setUp(self):
        self.machine=CountedBackend();self.service=CorrectionService(self.machine)
        self.model=DemoBackend();self.model.match_report={'model_only':True}
        self.manager=ModelManager(self.machine);self.service._model_manager=self.manager
        def prepare(opt,cancel,progress):
            self.manager.baseline=self.machine.snapshot();self.manager.settings={'raw_pvs':{}}
            return self.model
        self.manager.prepare=prepare
        self.opt=Options(response_source='rftrack',probe1=self.machine.hcorrectors[0],probe2=self.machine.hcorrectors[4])
    def test_cod_model_response_never_scans_machine_and_set_remeasures(self):
        data=self.service.calculate('orbit',self.opt)
        self.assertEqual(self.machine.commands,[]);self.assertEqual(self.machine.acquisitions,1)
        self.assertEqual(data['source'],'RFTrack model response + measured signal')
        self.service.apply(data)
        np.testing.assert_allclose(data['after'],data['predicted'],atol=1.e-10)
        self.service.reset()
    def test_dispersion_only_one_real_rf_pair(self):
        data=self.service.calculate('dispersion',self.opt)
        self.assertEqual([c[0] for c in self.machine.commands],['rf','rf','rf'])
        self.assertEqual(self.machine.acquisitions,3)
        self.assertEqual(len(data['orbit_measurements']),3)
    def test_skew_only_two_horizontal_diagnostic_probes(self):
        self.service.calculate('skew',self.opt)
        self.assertEqual(len(self.machine.commands),6)
        self.assertTrue(all(c[0]=='correctors' for c in self.machine.commands))
        self.assertEqual(self.machine.acquisitions,4)
    def test_unstable_model_preflight_never_moves_machine(self):
        self.model.orbit=lambda:(_ for _ in ()).throw(ValueError('unstable model'))
        with self.assertRaisesRegex(ValueError,'unstable'):self.service.calculate('dispersion',self.opt)
        self.assertEqual(self.machine.commands,[])
    def test_tune_or_main_supply_change_blocks_set(self):
        data=self.service.calculate('orbit',self.opt);data['model_settings']['raw_pvs']={'main:currentRead':1.}
        self.machine.get=lambda pv:2.
        with self.assertRaisesRegex(ValueError,'Model input changed'):self.service.apply(data)
        self.assertEqual(self.machine.commands,[])
    def test_model_measurement_failure_restores_rf(self):
        original=self.machine.orbit
        def fail():
            if self.machine.acquisitions==1:raise RuntimeError('no beam')
            return original()
        self.machine.orbit=fail
        with self.assertRaisesRegex(RuntimeError,'no beam'):self.service.calculate('dispersion',self.opt)
        self.assertEqual(self.machine.offset,0.)
    def test_extraction_soft_constraint_changes_bounded_solution(self):
        base=CorrectionService(self.machine).calculate('orbit',Options())
        n=len(base['actuators']);constrained=copy.deepcopy(base)
        constrained['constraints']=dict(matrix=np.eye(n),residual=np.zeros(n),weight=1.e5,labels=list(base['actuators']))
        result=self.service.calculate('orbit',Options(),dataset=constrained)
        self.assertLess(np.linalg.norm(result['delta']),np.linalg.norm(base['delta'])*.001)
        self.assertIn('constraints',result['solver']);self.assertIn('predicted_change',result['constraints'])
    def test_subset_bpm_proposal_applies_with_matching_shape(self):
        data=self.service.calculate('orbit',self.opt);n=len(data['names'])
        for key in ('names','positions_m','valid'):data[key]=np.asarray(data[key])[1:].tolist()
        for key in ('measured','target','predicted','fitted'):data[key]=np.asarray(data[key])[:,1:]
        data['response']=data['response'][1:]
        for key in ('full_measured','full_predicted','full_fitted'):data[key]=np.asarray(data[key])[:,1:]
        data['full_response']=np.asarray(data['full_response']).reshape(2,n,-1)[:,1:].reshape(2*(n-1),-1)
        self.service.apply(data)
        self.assertEqual(data['after'].shape,(1,n-1));self.assertEqual(data['full_after'].shape,(2,n-1))


class ModelMathTests(unittest.TestCase):
    def test_modes_preserve_tune_above_half_integer(self):
        matrix=np.zeros((4,4))
        for sl,tune,beta in [(slice(0,2),.2,4.),(slice(2,4),.585,2.)]:
            c=np.cos(2*np.pi*tune);s=np.sin(2*np.pi*tune)
            matrix[sl,sl]=[[c,beta*s],[-s/beta,c]]
        tunes,modes=normal_modes(matrix);np.testing.assert_allclose(tunes,[.2,.585])
        np.testing.assert_allclose([abs(modes[0,0])**2,abs(modes[2,1])**2],[4,2])
        matrix[0,0]=10.
        with self.assertRaisesRegex(ValueError,'Unstable'):normal_modes(matrix)
    def test_quad_average_beta_fit_uses_integrated_transport(self):
        beta=4.;alpha=.3;gamma=(1+alpha**2)/beta;names=['q1','q2','q3','q4'];rows=[];values=[]
        for n,s in zip(names,[0.,1.,2.,3.]):
            basis=[1.,-2*s,s*s+.25/12]
            rows.append(dict(name=n,beta_transport=basis));values.append(np.dot(basis,[beta,alpha,gamma]))
        cal=dict(schema='dr-sr-beta-1',source='drift-averaged independent test',bpms=rows,monitors=[dict(name='SR',r11=1.,r12_m=1.5)])
        fit=fit_sr_beta(names,values,cal)
        self.assertAlmostEqual(fit['monitors'][0]['beta_m'],beta-3*alpha+2.25*gamma,places=6)
    def test_reference_comparison_requires_names_units_and_provenance(self):
        model=dict(settings={'a':1},optics=dict(tunes=[.2,.585],rows={'MB1R':dict(s_m=1.,beta_m=[4.,2.])}))
        ref=dict(schema='dr-optics-reference-1',source='independent SAD',settings_sha256=digest(model['settings']),tunes=[.2,.585],rows=model['optics']['rows'])
        result=compare_optics(model,ref);self.assertTrue(result['same_input_hash_verified'])
        np.testing.assert_array_equal(result['beta_relative_rms'],[0,0])
        ref['settings_sha256']='bad'
        with self.assertRaisesRegex(ValueError,'hash'):compare_optics(model,ref)
    def test_response_comparison_aligns_names(self):
        d=dict(mode='orbit',actuator_unit='A',names=['a','b'],actuators=['x','y'],measured=[[0,0]],response=np.array([[1,2],[3,4]]),delta=[.1,.2])
        r=dict(schema='dr-response-reference-1',source='SAD export',mode='orbit',actuator_unit='A',signal_units='mm',names=['b','a'],actuators=['y','x'],response=[[4,3],[2,1]],delta=[.2,.1])
        report=compare_response(d,r);self.assertEqual(report['matrix_relative_error'],0.)
        np.testing.assert_array_equal(report['proposal_delta_difference'],[0,0])
        r['actuator_unit']='rad'
        with self.assertRaisesRegex(ValueError,'units'):compare_response(d,r)


class ModelGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def test_model_navigation_and_response_selection_no_pv_access(self):
        from Knobs.DRTuning.gui import TuningSession
        b=CountedBackend();window=TuningSession(b)
        try:
            window.tool_actions['model'].trigger()
            self.assertIs(window.tabs.currentWidget(),window.model_panel)
            panel=window.panels['dispersion'];panel.response_source_box.setCurrentIndex(1)
            self.assertEqual(panel.options.response_source,'rftrack')
            self.assertIn('one RF scan pair',panel.selection_label.text())
            self.assertEqual(b.commands,[]);self.assertEqual(b.acquisitions,0)
            window.model_panel.fields['tune_x'].setText('CUSTOM:TUNE:X')
            window.model_panel.inputs_changed()
            self.assertEqual(b.model_config['tune_pvs'][0],'CUSTOM:TUNE:X')
            self.assertIsNone(window.service.model_manager.model)
        finally:window.close();window.deleteLater();self.app.processEvents()


if __name__=='__main__':unittest.main()
