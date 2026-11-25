"""Offline tests of observable/model separation, bounds and stale inputs."""
import copy
import json
import unittest
from unittest.mock import patch,Mock
import tempfile
from pathlib import Path
import numpy as np
from Knobs.DRCorrection.backends import DemoBackend
from Knobs.DRCorrection.core import CorrectionService,Options,serial
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
    def test_model_response_shares_pause_controls_without_machine_writes(self):
        messages=[];requested=False
        def progress(n,text):
            nonlocal requested
            messages.append(text)
            if ': pair 1/' in text and not requested:
                requested=True;self.service.pause_requested.set()
            if text.startswith('PAUSED:'):
                self.assertTrue(self.service.paused.is_set())
                self.assertEqual(self.machine.commands,[])
                self.assertEqual(self.machine.acquisitions,0)
                self.service.pause_requested.clear()
        self.service.calculate('orbit',self.opt,progress)
        self.assertTrue(any(m.startswith('PAUSED:') for m in messages))
        self.assertTrue(any(m.startswith('RESUMED:') for m in messages))
        self.assertFalse(self.service.paused.is_set())
    def test_dispersion_only_one_real_rf_pair(self):
        data=self.service.calculate('dispersion',self.opt)
        self.assertEqual([c[0] for c in self.machine.commands],['rf','rf','rf'])
        self.assertEqual(self.machine.acquisitions,3)
        self.assertEqual(len(data['orbit_measurements']),3)

    def test_selected_zvs_only_are_probed_and_applied(self):
        self.opt.count_v=3
        target=np.linspace(-2.,2.,len(self.machine.bpm_names))
        original_target=self.machine.target_dispersion
        self.machine.target_dispersion=lambda plane:target if plane=='y' else original_target(plane)
        self.opt.fixed_actuators=(self.machine.vcorrectors[0],)
        preview=self.service.calculate('dispersion',self.opt)
        np.testing.assert_allclose(preview['target'][0],target)
        names=set(preview['selected'])
        self.assertTrue(0<len(names)<=3)
        self.assertNotIn(self.machine.vcorrectors[0],names)
        self.assertTrue(all(c[0]=='rf' for c in self.machine.commands))
        # Distinguish the independently measured matrix from the screening model.
        self.machine.dispr*=.8
        result=self.service.measure_selected_zvs(preview)
        probed={n for c in self.machine.commands if c[0]=='correctors' for n in c[1]}
        self.assertEqual(probed,names)
        self.assertEqual(set(result['actuators']),names)
        self.assertEqual(result['options']['response_source'],'measured')
        cols=[preview['actuators'].index(n) for n in result['actuators']]
        np.testing.assert_allclose(result['response'][:96],.8*preview['response'][:96,cols],atol=1.e-7)
        self.assertEqual(result['baseline'],self.machine.snapshot())
        self.assertFalse(result['applied'])
        measured=Options(**result['options'])
        result=self.service.recalculate(result,measured)
        self.machine.commands.clear()
        self.service.apply(result)
        self.assertTrue(result['applied'])
        self.assertTrue(all(set(c[1])<=names for c in self.machine.commands if c[0]=='correctors'))

    def test_nominal_zv_selection_bypasses_tune_fit_and_requires_measured_response(self):
        self.opt.response_source='rftrack_nominal'
        self.opt.count_v=3
        self.manager.prepare=Mock(side_effect=AssertionError('No live tune fit expected'))
        self.machine.bpm_names[-1]='unmapped BPM'
        self.opt.fixed_actuators=(self.machine.vcorrectors[0],)
        preview=self.service.calculate('dispersion',self.opt)
        self.assertEqual(preview['reference_matrix']['unmapped_bpms'],['unmapped BPM'])
        self.assertEqual(preview['full_measured'].shape,(4,95))
        self.assertEqual(preview['bpm_observations']['full_measured'].shape,(4,96))
        self.assertEqual(len(preview['selected']),3)
        self.assertNotIn(self.machine.vcorrectors[0],preview['selected'])
        self.assertTrue(all(c[0]=='rf' for c in self.machine.commands))
        with self.assertRaisesRegex(ValueError,'Measure the selected ZVs'):
            self.service.apply(preview)
        measured=self.service.measure_selected_zvs(preview)
        self.assertEqual(set(measured['actuators']),set(preview['selected']))
        self.assertEqual(measured['options']['response_source'],'measured')
        self.assertEqual(measured['reference_matrix'],preview['reference_matrix'])
        self.assertEqual(measured['full_measured'].shape,(4,96))
        self.manager.prepare.assert_not_called()

    def test_stale_zv_selection_stops_before_measuring(self):
        preview=self.service.calculate('dispersion',self.opt)
        self.machine.currents[self.machine.vcorrectors[0]]+=.02
        self.machine.commands.clear()
        with self.assertRaisesRegex(ValueError,'Settings changed'):
            self.service.measure_selected_zvs(preview)
        self.assertEqual(self.machine.commands,[])

    def test_initial_zv_loss_retries_both_endpoints_from_restored_center(self):
        self.opt.count_v=1
        self.opt.response_source='rftrack_nominal'
        self.opt.adaptive_probe=True
        self.opt.bpm_samples=10
        preview=self.service.calculate('dispersion',self.opt)
        name,=preview['selected']
        orbit=self.machine.orbit
        def lost_at_upper_endpoint():
            result=orbit()
            if self.machine.currents[name]>.011:result['valid'][:]=False
            return result
        with patch.object(self.machine,'orbit',side_effect=lost_at_upper_endpoint),patch.object(
                self.service.cancel,'wait',return_value=False) as wait:
            data=self.service.measure_selected_zvs(preview)
        wait.assert_called_once_with(3.)
        commands=[c[2][0] for c in self.machine.commands if c[0]=='correctors']
        np.testing.assert_allclose(commands,[-.05,.05,0.,-.02,.02,0.,-.01,.01,0.])
        history=data['probe_history']
        self.assertEqual([r['used_for_response'] for r in history],[False,False,False,True])
        self.assertEqual([r['next_width_A'] for r in history if r['status']=='no_common_bpm'],[.02,.01])
        self.assertTrue(data['valid'].all())
        j=self.machine.vcorrectors.index(name)
        np.testing.assert_allclose(data['response'][:96,0],self.machine.dispr[:,j],atol=1.e-8)
        self.assertEqual(self.machine.snapshot(),preview['baseline'])

    def test_unmeasurable_zv_at_floor_is_skipped_only_if_center_recovers(self):
        self.opt.count_v=2
        self.opt.response_source='rftrack_nominal'
        preview=self.service.calculate('dispersion',self.opt)
        first,remaining=sorted(preview['selected'],key=self.machine.vcorrectors.index)
        orbit=self.machine.orbit
        def lost_on_probe():
            result=orbit()
            if self.machine.currents[first]:result['tmit'][:]=0.
            return result
        with patch.object(self.machine,'orbit',side_effect=lost_on_probe),patch.object(
                self.service.cancel,'wait',return_value=False):
            data=self.service.measure_selected_zvs(preview)
        self.assertEqual(data['actuators'],[remaining])
        self.assertEqual(data['response'].shape,(192,1))
        self.assertEqual(data['scan_errors'][0]['magnet'],first)
        self.assertEqual([r['amplitude'] for r in data['probe_history'] if r['status']=='no_common_bpm'],[.05,.02,.01])
        self.assertEqual(self.machine.snapshot(),preview['baseline'])

        self.machine.commands.clear()
        def lost_at_center_too():
            result=orbit()
            if any(c[0]=='correctors' for c in self.machine.commands):result['valid'][:]=False
            return result
        with patch.object(self.machine,'orbit',side_effect=lost_at_center_too):
            with self.assertRaisesRegex(ValueError,'BPM coverage unavailable at original current'):
                self.service.measure_selected_zvs(preview)
        self.assertEqual([c[2] for c in self.machine.commands if c[0]=='correctors'],[[-.05],[0.]])
        self.assertEqual(self.machine.snapshot(),preview['baseline'])

    def test_zv_restoration_failure_never_retries(self):
        self.opt.count_v=1
        self.opt.response_source='rftrack_nominal'
        preview=self.service.calculate('dispersion',self.opt)
        name,=preview['selected']
        orbit=self.machine.orbit
        def lost_on_probe():
            result=orbit()
            if self.machine.currents[name]:result['valid'][:]=False
            return result
        with patch.object(self.machine,'orbit',side_effect=lost_on_probe),patch.object(
                self.machine,'restore_probe',create=True,side_effect=RuntimeError('restore failed')):
            with self.assertRaisesRegex(RuntimeError,'Probe restoration also failed'):
                self.service.measure_selected_zvs(preview)
        self.assertEqual([c[2] for c in self.machine.commands if c[0]=='correctors'],[[-.05]])

    def test_later_adaptive_zv_pair_does_not_restart_initial_probe_search(self):
        self.opt.count_v=1
        self.opt.adaptive_probe=True
        self.opt.adaptive_target_mm=10.
        self.opt.response_source='rftrack_nominal'
        preview=self.service.calculate('dispersion',self.opt)
        name,=preview['selected']
        orbit=self.machine.orbit
        def lost_after_growth():
            result=orbit()
            if abs(self.machine.currents[name])>.05:result['valid'][:]=False
            return result
        with patch.object(self.machine,'orbit',side_effect=lost_after_growth),patch.object(
                self.service.cancel,'wait',return_value=False) as wait:
            with self.assertRaisesRegex(ValueError,'no common BPM after retry'):
                self.service.measure_selected_zvs(preview)
        wait.assert_called_once_with(3.)
        commands=[c[2][0] for c in self.machine.commands if c[0]=='correctors']
        self.assertEqual(commands[:3],[-.05,.05,0.])
        self.assertEqual(len(commands),5)
        self.assertEqual(self.machine.snapshot(),preview['baseline'])

    def test_later_zv_endpoint_recovers_on_fresh_rf_triplet_at_same_current(self):
        self.opt.count_v=1
        self.opt.adaptive_probe=True
        self.opt.adaptive_target_mm=10.
        self.opt.adaptive_max_pairs=2
        self.opt.bpm_samples=10
        self.opt.response_source='rftrack_nominal'
        preview=self.service.calculate('dispersion',self.opt)
        name,=preview['selected']
        orbit=self.machine.orbit
        upper_observations=[]
        def first_high_triplet_lost():
            result=orbit()
            if self.machine.currents[name]>.05:
                upper_observations.append(self.machine.offset)
                if len(upper_observations)<=30:
                    result['valid'][:]=False
                    result['y'][:]=99999.
            return result
        def wait_at_restored_rf(seconds):
            self.assertEqual(seconds,3.)
            self.assertEqual(self.machine.offset,0.)
            self.assertGreater(self.machine.currents[name],.05)
            return False
        with patch.object(self.machine,'orbit',side_effect=first_high_triplet_lost),patch.object(
                self.service.cancel,'wait',side_effect=wait_at_restored_rf) as wait:
            data=self.service.measure_selected_zvs(preview)
        wait.assert_called_once_with(3.)
        self.assertEqual(len(upper_observations),60)
        self.assertEqual(upper_observations[:30],upper_observations[30:])
        self.assertEqual(len([c for c in self.machine.commands if c[0]=='correctors']),6)
        self.assertTrue(data['valid'].all())
        self.assertTrue(data['probe_history'][-1]['used_for_response'])
        self.assertEqual(data['probe_history'][-2]['status'],'retry_bpm')
        j=self.machine.vcorrectors.index(name)
        np.testing.assert_allclose(data['response'][:96,0],self.machine.dispr[:,j],atol=1.e-8)
        self.assertEqual(self.machine.snapshot(),preview['baseline'])

    def test_cancel_during_zv_retry_wait_restores_without_reacquiring(self):
        from Knobs.DRCorrection.core import Cancelled
        self.opt.count_v=1
        self.opt.adaptive_probe=True
        self.opt.adaptive_target_mm=10.
        self.opt.response_source='rftrack_nominal'
        preview=self.service.calculate('dispersion',self.opt)
        name,=preview['selected']
        orbit=self.machine.orbit
        observations=[]
        def invalid_at_larger_width():
            result=orbit()
            if abs(self.machine.currents[name])>.05:
                observations.append(self.machine.offset)
                result['valid'][:]=False
            return result
        def cancel(seconds):
            self.service.cancel.set()
            return True
        with patch.object(self.machine,'orbit',side_effect=invalid_at_larger_width),patch.object(
                self.service.cancel,'wait',side_effect=cancel):
            with self.assertRaises(Cancelled):self.service.measure_selected_zvs(preview)
        self.assertEqual(len(observations),3)
        self.assertEqual(self.machine.snapshot(),preview['baseline'])

    def test_nominal_zv_reports_which_rf_setting_lost_bpm_coverage(self):
        self.opt.response_source='rftrack_nominal'
        self.opt.bpm_samples=10
        self.machine.bpm_names[-1]='unmapped BPM'
        self.opt.excluded_bpms=(self.machine.bpm_names[0],)
        orbit=self.machine.orbit
        def interrupted_update():
            result=orbit()
            if self.machine.acquisitions==14:result['tmit'][:]=0.
            return result
        with patch.object(self.machine,'orbit',side_effect=interrupted_update):
            with self.assertRaises(ValueError) as caught:self.service.calculate('dispersion',self.opt)
        message=str(caught.exception)
        self.assertIn('RF baseline: 94 BPMs valid on every update',message)
        self.assertIn('RF minus: 0 BPMs valid on every update; 0–94 per update',message)
        self.assertIn('RF plus: 94 BPMs valid on every update',message)
        self.assertEqual(self.machine.acquisitions,30)
        self.assertEqual(self.machine.frequency(),0.)
        self.assertEqual([c[0] for c in self.machine.commands],['rf','rf','rf'])
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

    def test_vertical_unstable_import_offers_explicit_recovery_before_probe(self):
        b=CountedBackend()
        options=Options(plane='y',response_source='rftrack',model_match_tunes=False)
        settings={'preparation':'sad_cod','tunes':None}
        fake=Mock()
        fake.tunes.side_effect=lambda:normal_modes(np.diag([1.29989,1/1.29989,1.,1.]))
        service=CorrectionService(b)
        with patch('Knobs.DRTuning.model.capture_settings',return_value=settings),patch('Knobs.DRTuning.model.OpticsModel',return_value=fake):
            with self.assertRaises(ValueError) as error:
                service.acquire('dispersion',options)
        for expected in ('0.29989','Current optics + tune fit','Saved nominal RFTrack','Measure selected ZVs'):
            self.assertIn(expected,str(error.exception))
        self.assertEqual(service.model_manager.last_attempt['stage'],'initial imported optics stability check')
        self.assertEqual(b.commands,[])
        fake.match_tunes.assert_not_called()
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
        self.assertIsNone(model.match_report)
        attempt=model.match_attempt
        self.assertEqual(attempt['status'],'failed');self.assertTrue(attempt['model_restored'])
        self.assertTrue(any(attempt['bound_active']))
        self.assertGreater(len(attempt['history']),1)
        self.assertGreater(len(attempt['accepted_points']),1)
        np.testing.assert_allclose(attempt['target'],[.4,.45])
        np.testing.assert_allclose((attempt['final']-attempt['resolved_target']+.5)%1-.5,attempt['residual'])
        json.dumps(serial(attempt),allow_nan=False)
    def test_initial_unstable_sad_reference_recovers_with_absolute_bounds(self):
        from types import SimpleNamespace
        class Quadrupole:
            def __init__(self):self.value=2.
            def get_K1(self,p):return self.value
            def set_K1(self,p,value):self.value=value
        for preset,alias in [('sad_cod',False),('sad_cod',True),('all_currents',False)]:
            with self.subTest(preset=preset,alias=alias):
                elements={'QF1R.1':Quadrupole(),'QF2R.1':Quadrupole()};trials=[]
                def tunes():
                    changes=np.array([e.value/2.-1 for e in elements.values()]);trials.append(changes)
                    if changes[0]<=0:raise ModelOpticsError('Unstable imported model',{'max_modulus_deviation':.162})
                    return np.array([.2,.3])+changes
                model=SimpleNamespace(elements=elements,momentum=1300.,check_cancel=lambda:None,
                    settings={'preparation':preset},tunes=tunes)
                if preset=='all_currents':
                    with self.assertRaises(ModelOpticsError):OpticsModel.match_tunes(model,[.21,.31])
                    self.assertEqual(len(trials),1)
                    self.assertEqual([e.value for e in elements.values()],[2.,2.]);continue
                report=OpticsModel.match_tunes(model,[.79,.69] if alias else [.21,.31],alias=alias)
                np.testing.assert_allclose(report['final'],[.21,.31],atol=1.e-5)
                self.assertLessEqual(np.max(np.abs(trials)),.05)
                self.assertEqual(report['initial_stable_point_search']['initial_optics_failure']['max_modulus_deviation'],.162)
                json.dumps(serial(report),allow_nan=False)

    def test_unrecoverable_seed_and_cancel_restore_model(self):
        from types import SimpleNamespace
        from Knobs.DRCorrection.core import Cancelled
        class Quadrupole:
            def __init__(self):self.value=2.
            def get_K1(self,p):return self.value
            def set_K1(self,p,value):self.value=value
        for cancel in (False,True):
            elements={'QF1R.1':Quadrupole(),'QF2R.1':Quadrupole()};trials=[]
            def tunes():
                trials.append([e.value/2.-1 for e in elements.values()])
                if cancel and len(trials)>3:raise Cancelled('Stopped')
                raise ModelOpticsError('Always unstable',{'max_modulus_deviation':.162})
            model=SimpleNamespace(elements=elements,momentum=1300.,check_cancel=lambda:None,
                settings={'preparation':'sad_cod'},tunes=tunes)
            with self.assertRaisesRegex(Cancelled if cancel else ModelOpticsError,'Stopped' if cancel else 'No stable initial'):
                OpticsModel.match_tunes(model,[.21,.31])
            self.assertEqual([e.value for e in elements.values()],[2.,2.])
            self.assertLessEqual(np.max(np.abs(trials)),.05+1.e-12)

    def test_manager_allows_sad_seed_recovery_only_when_matching_enabled(self):
        for preset,match in [('sad_cod',True),('sad_cod',False),('all_currents',True)]:
            b=CountedBackend();manager=ModelManager(b)
            settings={'raw_pvs':{},'preparation':preset,'tunes':[.2,.3],'tune_alias':False}
            fake=Mock();fake.tunes.side_effect=lambda:normal_modes(np.diag([2.,.5,1.,1.]))
            fake.match_report={'initial_stable_point_search':{'evaluations':8}}
            with patch('Knobs.DRTuning.model.capture_settings',return_value=settings),patch('Knobs.DRTuning.model.OpticsModel',return_value=fake):
                if preset=='sad_cod' and match:
                    self.assertIs(manager.prepare(Options(model_match_tunes=match),lambda:None),fake)
                    fake.match_tunes.assert_called_once_with(settings['tunes'],max_scale=.05,alias=False)
                    self.assertIsNone(manager.last_attempt['initial_tunes'])
                    self.assertIn('initial_optics_failure',manager.last_attempt)
                else:
                    with self.assertRaises(ValueError):manager.prepare(Options(model_match_tunes=match),lambda:None)
                    fake.match_tunes.assert_not_called()
            self.assertEqual(b.commands,[])

    def test_failed_match_history_retained_in_manager_diagnostic(self):
        b=CountedBackend();manager=ModelManager(b)
        settings={'raw_pvs':{},'tunes':[.2,.3],'tune_alias':False}
        fake=Mock();fake.tunes.return_value=np.array([.21,.31])
        fake.match_attempt=dict(status='failed',target=np.array([.2,.3]),
            history=[dict(family_fractional_changes=np.array([.05,0.]),tunes=np.array([.21,.31]))],
            model_restored=True)
        fake.match_tunes.side_effect=ValueError('Tune match failed at bound')
        with patch('Knobs.DRTuning.model.capture_settings',return_value=settings),patch('Knobs.DRTuning.model.OpticsModel',return_value=fake):
            with self.assertRaisesRegex(ValueError,'Tune match failed at bound'):
                manager.prepare(Options(),lambda:None)
        attempt=manager.last_attempt
        self.assertEqual(attempt['tune_match_attempt']['status'],'failed')
        self.assertEqual(len(attempt['tune_match_attempt']['history']),1)
        self.assertIsNot(attempt['tune_match_attempt'],fake.match_attempt)
        self.assertEqual(b.commands,[]);self.assertIsNone(manager.model)
        json.dumps(serial(attempt),allow_nan=False)

    def test_bound_step_uses_remaining_family_and_larger_limit_is_explicit(self):
        from types import SimpleNamespace
        class Quadrupole:
            def __init__(self):self.value=1.
            def get_K1(self,p):return self.value
            def set_K1(self,p,v):self.value=v
        elements={'QF1R.1':Quadrupole(),'QF2R.1':Quadrupole()}
        def tunes():
            x,y=[e.value-1 for e in elements.values()]
            return np.array([.2+x+y,.3+y])
        model=SimpleNamespace(elements=elements,momentum=1300.,check_cancel=lambda:None,settings={},tunes=tunes)
        # Unconstrained Newton points only along QF1. At its bound the
        # feasible QF2 direction still improves the residual.
        with self.assertRaisesRegex(ValueError,'Tune match failed'):
            OpticsModel.match_tunes(model,[.14,.3])
        attempt=model.match_attempt
        np.testing.assert_allclose(attempt['family_fractional_changes'],[-.05,-.005],atol=1.e-6)
        np.testing.assert_allclose(attempt['residual'],[.005,-.005],atol=1.e-6)
        self.assertTrue(attempt['model_restored'])
        report=OpticsModel.match_tunes(model,[.14,.3],max_scale=.10)
        np.testing.assert_allclose(report['final'],[.14,.3],atol=1.e-5)
        self.assertEqual(report['max_scale'],.10)
        self.assertLess(report['family_fractional_changes'][0],-.05)

    def test_configured_model_limit_is_captured_and_used_by_manager(self):
        from Knobs.DRTuning.model import preparation_config
        b=CountedBackend();b.model_config={'tune_match_max_scale':.10}
        self.assertEqual(preparation_config(b)['tune_match_max_scale'],.10)
        manager=ModelManager(b)
        settings={'raw_pvs':{},'tunes':[.2,.3],'tune_alias':False,'tune_match_max_scale':.10}
        fake=Mock();fake.tunes.return_value=np.array([.21,.31]);fake.match_report={}
        with patch('Knobs.DRTuning.model.capture_settings',return_value=settings),patch('Knobs.DRTuning.model.OpticsModel',return_value=fake):
            manager.prepare(Options(),lambda:None)
        fake.match_tunes.assert_called_once_with(settings['tunes'],max_scale=.10,alias=False)
        self.assertEqual(b.commands,[])
        for bound in (0,-.1,.21,float('nan')):
            b.model_config={'tune_match_max_scale':bound}
            with self.assertRaisesRegex(ValueError,'matching limit'):preparation_config(b)

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

    def test_load_legacy_measured_zvs_refits_for_dispersion_without_scanning(self):
        from Knobs.DRCorrection.core import save_session
        from Knobs.DRTuning.gui import TuningSession
        from PyQt6.QtWidgets import QFileDialog
        machine = CountedBackend()
        window = TuningSession(machine)
        try:
            panel = window.panels['dispersion']
            machine.errors['y'][:] = .05
            machine.disp0[:] = .05
            machine.dispr[:] = -1.
            machine.orm['y'][:] = 1.
            opt = copy.deepcopy(panel.options)
            opt.response_source = 'rftrack_nominal'
            opt.count_v = 1
            preview = window.service.calculate('dispersion', opt)
            np.testing.assert_array_equal(preview['target'][1], preview['measured'][1])
            legacy = copy.deepcopy(preview)
            legacy['options'].pop('preserve_vertical_dispersion_orbit')
            legacy['options'].update(dispersion_weight=.05, orbit_weight=1.)
            saved = window.service.measure_selected_zvs(legacy)
            self.assertLess(saved['delta'][0], 0.)
            saved['options'].pop('preserve_vertical_dispersion_orbit')
            saved['acquisition_options'].pop('preserve_vertical_dispersion_orbit')
            reads = machine.acquisitions
            machine.commands.clear()
            with tempfile.TemporaryDirectory() as root:
                path = Path(root) / 'dispersion.json'
                save_session(path, saved)
                def run(action, finished, **kwargs):
                    finished(action(lambda *_: None))
                with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(path), '')), patch.object(window, 'start', run):
                    panel.load_measured_response()
            result = panel.data
            self.assertTrue(panel.apply_button.isEnabled())
            self.assertTrue(result['reused_response'])
            np.testing.assert_array_equal(result['target'][1], saved['measured'][1])
            np.testing.assert_array_equal(result['response'], saved['response'])
            np.testing.assert_allclose(result['delta'], [.05 / (1. + .05**2)], atol=1e-9)
            self.assertEqual(machine.acquisitions, reads)
            self.assertEqual(machine.commands, [])
        finally:
            window.close();window.deleteLater();self.app.processEvents()

    def test_diagnostics_save_latest_bump_or_main_attempt_without_machine_access(self):
        from Knobs.DRTuning.gui import TuningSession
        b=CountedBackend();window=TuningSession(b)
        try:
            main=window.service.model_manager;bump=window.coupling_panel.service.reader.model_manager
            main.last_attempt=dict(captured_unix_s=1,status='ready')
            bump.last_attempt=dict(captured_unix_s=2,status='failed',error='Actuator absent from model')
            with tempfile.TemporaryDirectory() as root:
                path=Path(root)/'diagnostic.json'
                window.model_panel.save_diagnostics(path)
                self.assertEqual(json.loads(path.read_text()),bump.last_attempt)
                main.last_attempt=dict(captured_unix_s=3,status='ready')
                window.model_panel.save_diagnostics(path)
                self.assertEqual(json.loads(path.read_text()),main.last_attempt)
                main.last_attempt=None
                window.model_panel.save_diagnostics(path)
                self.assertEqual(json.loads(path.read_text()),bump.last_attempt)
            self.assertEqual(b.commands,[]);self.assertEqual(b.acquisitions,0)
        finally:window.close();window.deleteLater();self.app.processEvents()
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
            vertical=window.panels['dispersion']
            self.assertEqual(vertical.screening.currentData(),'imported')
            self.assertFalse(vertical.options.model_match_tunes)
            for choice,source,fit in [('imported','rftrack',False),('nominal','rftrack_nominal',False),('fit','rftrack',True)]:
                vertical.screening.setCurrentIndex(vertical.screening.findData(choice))
                self.assertEqual(vertical.options.response_source,source)
                self.assertEqual(vertical.options.model_match_tunes,fit)
            self.assertTrue(panel.options.model_match_tunes)
            window.model_panel.preparation.setCurrentIndex(1)
            self.assertEqual(b.model_config['preparation'],'all_currents')
            self.assertEqual(window.model_panel.match_limit.value(),5.)
            window.model_panel.match_limit.setValue(10.)
            self.assertEqual(b.model_config['tune_match_max_scale'],.10)
            self.assertTrue(all(p.data is None for p in window.panels.values()))
            self.assertEqual(b.commands,[]);self.assertEqual(b.acquisitions,0)
        finally:window.close();window.deleteLater();self.app.processEvents()
    def test_model_navigation_and_response_selection_no_pv_access(self):
        from Knobs.DRTuning.gui import TuningSession
        b=CountedBackend();window=TuningSession(b)
        try:
            window.tool_actions['model'].trigger()
            self.assertIs(window.tabs.currentWidget(),window.model_panel)
            panel=window.panels['dispersion']
            self.assertEqual(panel.options.response_source,'rftrack')
            self.assertEqual(panel.options.plane,'y')
            self.assertFalse(panel.measure_button.isEnabled())
            self.assertEqual(window.tabs.indexOf(window.qm_dispersion),window.tabs.indexOf(panel)+1)
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
