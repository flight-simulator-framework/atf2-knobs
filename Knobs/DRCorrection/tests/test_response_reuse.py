"""Refitting a saved response must not trigger another machine measurement."""
import copy
import unittest
from unittest.mock import patch
import numpy as np
from Knobs.DRCorrection.backends import DemoBackend
from Knobs.DRCorrection.core import CorrectionService,Options


class ResponseReuseTests(unittest.TestCase):
    def setUp(self):
        self.backend=DemoBackend();self.service=CorrectionService(self.backend)
        self.opt=Options(probe1=self.backend.hcorrectors[0],probe2=self.backend.hcorrectors[1])

    def test_probe_target_and_fit_changes_reuse_response_without_machine_access(self):
        for mode in ('orbit','dispersion','skew'):
            with self.subTest(mode=mode):
                data=self.service.calculate(mode,self.opt);opt=copy.deepcopy(self.opt)
                opt.adaptive_target_mm=.025;opt.svd_cut=.1;opt.count_v=3
                opt.dispersion_weight=.2
                with patch.object(self.backend,'write',side_effect=AssertionError('magnet write')),\
                     patch.object(self.backend,'orbit',side_effect=AssertionError('beam read')),\
                     patch.object(self.backend,'set_frequency',side_effect=AssertionError('RF write')):
                    result=self.service.recalculate(data,opt)
                np.testing.assert_array_equal(result['response'],data['response'])
                self.assertEqual(result['acquisition_options']['adaptive_target_mm'],.1)
                self.assertEqual(result['options']['adaptive_target_mm'],.025)
                self.assertTrue(result['reused_response'])

    def test_exclude_then_reinclude_bpm_preserves_acquisition_validity(self):
        data=self.service.calculate('orbit',self.opt)
        opt=copy.deepcopy(self.opt);opt.excluded_bpms=(self.backend.bpm_names[0],)
        excluded=self.service.recalculate(data,opt)
        self.assertFalse(excluded['valid'][0]);self.assertTrue(excluded['acquisition_valid'][0])
        restored=self.service.recalculate(excluded,self.opt)
        np.testing.assert_array_equal(restored['valid'],data['valid'])
        np.testing.assert_allclose(restored['delta'],data['delta'])

    def test_new_unmeasured_correctors_require_scan(self):
        self.opt.fixed_actuators=tuple(self.backend.vcorrectors[2:])
        data=self.service.calculate('orbit',self.opt)
        with self.assertRaisesRegex(ValueError,'No measured columns'):
            self.service.recalculate(data,Options())

    def test_missing_bpm_rows_are_not_silently_ignored(self):
        data=self.service.calculate('orbit',self.opt)
        data['names']=data['names'][1:]
        with self.assertRaisesRegex(ValueError,'No saved response rows for BPMs'):
            self.service.recalculate(data,self.opt)

    def test_other_plane_and_changed_physical_signal_definition_are_rejected(self):
        for mode,key,value in [('orbit','plane','x'),('dispersion','momentum_compaction',.003),
                               ('skew','change1_A',.4)]:
            with self.subTest(mode=mode):
                data=self.service.calculate(mode,self.opt);opt=copy.deepcopy(self.opt);setattr(opt,key,value)
                with self.assertRaises(ValueError):self.service.recalculate(data,opt)

    def test_changed_machine_still_blocks_set_of_recalculated_proposal(self):
        data=self.service.calculate('orbit',self.opt)
        self.backend.write('correctors',[self.backend.hcorrectors[0]],[.02])
        result=self.service.recalculate(data,self.opt)
        with self.assertRaisesRegex(ValueError,'Settings changed since CALC'):
            self.service.apply(result)
