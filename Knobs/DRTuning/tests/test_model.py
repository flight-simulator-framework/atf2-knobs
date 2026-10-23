"""Offline tests of observable/model separation, bounds and stale inputs."""
import copy
import json
import unittest
from unittest.mock import patch,Mock
import tempfile
from pathlib import Path
import numpy as np
from Knobs.DRCorrection.backends import DemoBackend
from Knobs.DRCorrection.core import CorrectionService,Options
from Knobs.DRTuning.model import ModelManager,normal_modes,verify_inputs,digest,ModelOpticsError,OpticsModel
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
    def test_model_response_does_not_run_adaptive_real_or_virtual_scan(self):
        self.opt.adaptive_probe=True
        data=self.service.calculate('orbit',self.opt)
        self.assertEqual(data['probe_history'],[])
        self.assertEqual(self.machine.commands,[]);self.assertEqual(self.machine.acquisitions,1)
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
    def test_failed_initial_optics_retains_inputs_and_never_writes(self):
        b=CountedBackend();manager=ModelManager(b)
        settings={'raw_pvs':{'main:currentRead':1.},'tunes':[.2,.3],'tune_alias':False}
        fake=Mock();fake.tunes.side_effect=lambda:normal_modes(np.diag([2.,.5,1.,1.]))
        # Even without tune matching, unstable imported optics must not pass.
        with patch('Knobs.DRTuning.model.capture_settings',return_value=settings),patch('Knobs.DRTuning.model.OpticsModel',return_value=fake):
            with self.assertRaisesRegex(ValueError,'initial imported optics stability check'):
                manager.prepare(Options(model_match_tunes=False),lambda:None)
        self.assertEqual(b.commands,[]);self.assertIsNone(manager.model)
        self.assertEqual(manager.last_attempt['settings'],settings)
        self.assertEqual(manager.last_attempt['status'],'failed')
        self.assertEqual(manager.last_attempt['optics_failure']['max_modulus_deviation'],1.)
        from Knobs.DRCorrection.core import serial
        json.dumps(serial(manager.last_attempt),allow_nan=False)
    def test_unstable_tune_trial_backtracks_and_failed_fit_restores_model(self):
        from types import SimpleNamespace
        class Quadrupole:
            def __init__(self):self.value=2.
            def get_K1(self,p):return self.value
            def set_K1(self,p,value):self.value=value
        elements={'QF1R.1':Quadrupole(),'QF2R.1':Quadrupole()}
        def tunes():
            changes=np.array([e.value/2.-1 for e in elements.values()])
            if .003<changes[0]<.006:
                raise ModelOpticsError('Unstable trial',{'max_modulus_deviation':.2})
            return np.array([.2,.3])+changes
        model=SimpleNamespace(elements=elements,momentum=1300.,check_cancel=lambda:None,settings={},tunes=tunes)
        report=OpticsModel.match_tunes(model,[.21,.31])
        np.testing.assert_allclose(report['final'],[.21,.31],atol=1.e-5)
        self.assertGreater(len(report['rejected_unstable_trials']),0)
        for e in elements.values():e.value=2.
        with self.assertRaisesRegex(ValueError,'Tune match failed'):
            OpticsModel.match_tunes(model,[.4,.45])
        self.assertEqual([e.value for e in elements.values()],[2.,2.])
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
    def test_live_defaults_choose_sad_model_and_legacy_counts(self):
        from Knobs.DRTuning.gui import TuningSession
        b=CountedBackend();b.identity='epics:offline-fake'
        window=TuningSession(b)
        try:
            self.assertEqual(window.model_panel.preparation.currentData(),'sad_cod')
            panel=window.panels['orbit'];controls=panel.modern_fields
            self.assertTrue(controls['adaptive_probe'].isChecked())
            self.assertFalse(controls['adaptive_target_mm'].isEnabled())
            panel.response_source_box.setCurrentIndex(0)
            self.assertTrue(controls['adaptive_target_mm'].isEnabled())
            controls['adaptive_target_mm'].setValue(.15)
            controls['adaptive_target_tolerance'].setValue(20.)
            self.assertEqual(panel.options.adaptive_target_mm,.15)
            self.assertEqual(panel.options.adaptive_target_tolerance,.2)
            controls['adaptive_probe'].setChecked(False)
            self.assertFalse(controls['adaptive_target_mm'].isEnabled())
            panel.response_source_box.setCurrentIndex(1)
            for mode,h in [('orbit',5),('dispersion',10)]:
                p=window.panels[mode]
                self.assertEqual(p.options.response_source,'rftrack')
                self.assertEqual((p.options.count_h,p.options.count_v),(h,5))
            window.model_panel.preparation.setCurrentIndex(1)
            self.assertEqual(b.model_config['preparation'],'all_currents')
            self.assertEqual(b.commands,[]);self.assertEqual(b.acquisitions,0)
        finally:window.close();window.deleteLater();self.app.processEvents()
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
            window.service.model_manager.last_attempt={'schema':'dr-model-diagnostic-1','status':'failed','settings':{'raw_pvs':{'test:pv':1.}}}
            with tempfile.TemporaryDirectory() as root:
                path=Path(root)/'diagnostic.json';window.model_panel.save_diagnostics(path)
                self.assertEqual(json.loads(path.read_text())['status'],'failed')
        finally:window.close();window.deleteLater();self.app.processEvents()


if __name__=='__main__':unittest.main()
