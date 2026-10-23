"""Beam-driven probe adjustment, bounds and restoration; no machine access."""
import unittest
import importlib.util
import builtins
import sys
from unittest.mock import patch
import numpy as np
from Knobs.DRCorrection.backends import DemoBackend
from Knobs.DRCorrection.core import CorrectionService,Options,Cancelled
from Knobs.DRCorrection.core import next_excitation_amplitude


class LinearBeam(DemoBackend):
    def __init__(self,gain=1.,readback_gain=1.):
        super().__init__();self.gain=gain;self.readback_gain=readback_gain;self.commands=[]
        self.bpm_names=['b1','b2','ignored'];self.positions_m=np.arange(3.)
        self.hcorrectors=['H1','H2'];self.vcorrectors=['V'];self.skews=['SD1']
        self.currents=dict(H1=0.,H2=0.,V=0.);self.skew_values={'SD1':0.}
        self._target={'x':np.zeros(3),'y':np.zeros(3)}
    def limits(self,kind,names=None):
        return (-5.,5.) if names is None else (np.full(len(names),-5.),np.full(len(names),5.))
    def unit(self,kind):return 'A'
    def target_dispersion(self,plane):return self._target[plane]
    def write(self,kind,names,values):
        self.commands.append((kind,list(names),list(values)))
        super().write(kind,names,np.asarray(values)*self.readback_gain)
    def orbit(self):
        self.acquisitions+=1
        x=.1+self.currents['H1']*self.gain
        y=(.2+self.currents['V']*self.gain
            +self.skew_values['SD1']*(2*self.currents['H1']+3*self.currents['H2'])
            -self.offset/(self.nominal_frequency_hz*.0021)*(3+100*self.currents['V']))
        return dict(names=self.bpm_names,x=np.array([x,x,1.e8*self.currents['H1']]),
            y=np.array([y,y,1.e8*self.currents['V']]),tmit=np.ones(3),valid=np.ones(3,bool),position_unit='mm')


def options(**changes):
    values=dict(adaptive_probe=True,adaptive_target_mm=.1,adaptive_max_pairs=20,
        corrector_probe_A=.01,excluded_bpms=('ignored',),probe1='H1',probe2='H2')
    values.update(changes);return Options(**values)


class AdaptiveProbeTests(unittest.TestCase):
    def load_without_shared_module(self,missing_name='machine_interfaces.Backend.Excitation'):
        from Knobs.DRCorrection import core
        original=builtins.__import__
        def blocked(name,*args,**kwargs):
            if name=='machine_interfaces.Backend.Excitation':
                raise ModuleNotFoundError('Simulated missing module',name=missing_name)
            return original(name,*args,**kwargs)
        spec=importlib.util.spec_from_file_location('Knobs.DRCorrection._compat_core_test',core.__file__)
        module=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{spec.name:module}),patch('builtins.__import__',side_effect=blocked):
            spec.loader.exec_module(module)
        return module

    def test_old_machine_interfaces_runs_adaptive_scan_with_bundled_rule(self):
        module=self.load_without_shared_module();b=LinearBeam()
        data=module.CorrectionService(b).acquire('orbit',options())
        self.assertEqual(data['probe_rule_source'],'bundled SysID compatibility rule')
        self.assertTrue(data['probe_history'][-1]['target_reached'])
        self.assertEqual(b.currents['V'],0.)

    def test_bundled_rule_matches_shared_rule_across_edge_cases(self):
        module=self.load_without_shared_module()
        for current in (1.e-7,.01,.5):
            for observed in (0.,float('nan'),.001,1.):
                for target in (0.,.1):
                    for limit in (0.,.03,.5):
                        self.assertEqual(module.next_excitation_amplitude(current,observed,target,limit),
                                         next_excitation_amplitude(current,observed,target,limit))
        self.assertEqual(module.finite_abs_max([float('nan'),-.5,.1]),.5)

    def test_missing_dependency_inside_shared_module_is_not_hidden(self):
        with self.assertRaises(ModuleNotFoundError) as caught:
            self.load_without_shared_module('some_dependency_inside_excitation')
        self.assertEqual(caught.exception.name,'some_dependency_inside_excitation')

    @unittest.skipUnless(importlib.util.find_spec('RF_Track'),'native RFTrack optional')
    def test_native_rftrack_bpm_feedback_reaches_target_and_restores(self):
        from Knobs.DRTuning.rftrack import RFTrackBackend
        b=RFTrackBackend(seed=32);b.noise_mm=0.;before=b.snapshot()
        selected=b.vcorrectors[0]
        opt=options(corrector_probe_A=.001,adaptive_max_pairs=12,excluded_bpms=(),
            fixed_actuators=tuple(n for n in b.vcorrectors if n!=selected))
        data=CorrectionService(b).acquire('orbit',opt)
        self.assertGreater(len(data['probe_history']),1)
        self.assertTrue(data['probe_history'][-1]['target_reached'])
        self.assertGreater(np.linalg.norm(data['response']),0.)
        self.assertEqual(b.snapshot(),before)

    def test_sysid_growth_shrink_and_zero_response_rules(self):
        self.assertAlmostEqual(next_excitation_amplitude(.01,.01,.1,.5),.018)
        self.assertAlmostEqual(next_excitation_amplitude(.05,1.,.1,.5),.042)
        self.assertAlmostEqual(next_excitation_amplitude(.01,0.,.1,.5),.014)

    def test_grows_to_target_ignores_excluded_bpm_and_uses_final_pair(self):
        b=LinearBeam();service=CorrectionService(b)
        data=service.acquire('orbit',options())
        history=data['probe_history'];self.assertGreater(len(history),1)
        self.assertTrue(history[-1]['target_reached']);self.assertTrue(history[-1]['used_for_response'])
        self.assertTrue(all(not h['used_for_response'] for h in history[:-1]))
        self.assertLess(abs(history[-1]['observed_mm']/.1-1),.25)
        np.testing.assert_allclose(data['response'][:2],[[1.],[1.]])
        self.assertEqual(b.currents['V'],0.)
        self.assertTrue(all(b.commands[i][2]==[0.] for i in range(2,len(b.commands),3)))

    def test_shrinks_oversized_response(self):
        b=LinearBeam(gain=10.)
        data=CorrectionService(b).acquire('orbit',options(corrector_probe_A=.05))
        history=data['probe_history']
        self.assertLess(history[-1]['amplitude'],history[0]['amplitude'])
        self.assertTrue(history[-1]['target_reached']);self.assertEqual(b.currents['V'],0.)

    def test_adaptive_proposal_applies_predicted_correction_and_resets(self):
        b=LinearBeam();service=CorrectionService(b);before=b.snapshot()
        data=service.calculate('orbit',options())
        self.assertEqual(b.snapshot(),before)
        service.apply(data)
        np.testing.assert_allclose(data['after'],data['predicted'],atol=1.e-8)
        self.assertEqual(data['assessment']['rows'][0]['status'],'improved')
        service.reset();self.assertEqual(b.snapshot(),before)

    def test_readback_span_normalizes_response(self):
        b=LinearBeam(readback_gain=.8)
        data=CorrectionService(b).acquire('orbit',options())
        np.testing.assert_allclose(data['response'][:2],[[1.],[1.]],atol=1.e-12)
        last=data['probe_history'][-1]
        self.assertAlmostEqual(last['readback_span'],.8*(last['commanded'][1]-last['commanded'][0]))

    def test_amplitude_and_absolute_supply_limits_are_reported(self):
        b=LinearBeam(gain=.001);b.currents['V']=4.99
        data=CorrectionService(b).acquire('orbit',options(adaptive_max_probe=.03))
        final=data['probe_history'][-1]
        self.assertEqual(final['status'],'amplitude_or_supply_limit');self.assertFalse(final['target_reached'])
        self.assertTrue(all(abs(v)<=5 for _,_,values in b.commands for v in values))
        self.assertLessEqual(final['amplitude'],.03);self.assertEqual(b.currents['V'],4.99)
        np.testing.assert_allclose(data['response'][:2],[[.001],[.001]],atol=1.e-12)

    def test_max_pairs_does_not_claim_target_reached(self):
        b=LinearBeam(gain=.001)
        data=CorrectionService(b).acquire('orbit',options(adaptive_max_pairs=2))
        self.assertEqual(len(data['probe_history']),2)
        self.assertEqual(data['probe_history'][-1]['status'],'max_pairs')
        self.assertFalse(data['probe_history'][-1]['target_reached']);self.assertEqual(b.currents['V'],0.)

    def test_no_current_movement_is_rejected_after_restoring(self):
        b=LinearBeam(readback_gain=0.)
        with self.assertRaisesRegex(ValueError,'no positive readback span'):
            CorrectionService(b).acquire('orbit',options())
        self.assertEqual(b.commands[-1][2],[0.])

    def test_cancel_between_pairs_leaves_original_current(self):
        b=LinearBeam();service=CorrectionService(b)
        def progress(_,message):
            if 'BPM response' in message:service.cancel.set()
        with self.assertRaises(Cancelled):service.acquire('orbit',options(),progress)
        self.assertEqual(len(b.commands),3);self.assertEqual(b.currents['V'],0.)

    def test_invalid_configuration_never_reads_or_writes(self):
        for changes in [dict(adaptive_target_mm=0.),dict(adaptive_max_pairs=1.5),
                        dict(adaptive_target_tolerance=1.),dict(adaptive_max_probe=-1.)]:
            b=LinearBeam()
            with self.assertRaisesRegex(ValueError,'Invalid adaptive'):
                CorrectionService(b).acquire('orbit',options(**changes))
            self.assertEqual(b.commands,[]);self.assertEqual(b.acquisitions,0)

    def test_dispersion_adapts_to_nominal_cod_not_eta(self):
        b=LinearBeam()
        data=CorrectionService(b).acquire('dispersion',options())
        final=data['probe_history'][-1]
        self.assertTrue(final['target_reached'])
        self.assertAlmostEqual(final['observed_mm'],final['readback_span']/2.)
        np.testing.assert_allclose(data['response'][:2],[[100.],[100.]],atol=1.e-8)
        self.assertEqual(b.offset,0.);self.assertEqual(b.currents['V'],0.)

    def test_skew_targets_change_in_vertical_probe_response(self):
        b=LinearBeam()
        data=CorrectionService(b).acquire('skew',options(skew_probe=.05))
        final=data['probe_history'][-1]
        self.assertTrue(final['target_reached'])
        self.assertAlmostEqual(final['observed_mm'],final['amplitude']*.6)
        self.assertEqual(b.skew_values['SD1'],0.)
        self.assertEqual(list(b.currents.values()),[0.,0.,0.])


if __name__=='__main__':unittest.main()
