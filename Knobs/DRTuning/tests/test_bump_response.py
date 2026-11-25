"""Plane-specific closure and reuse of shared, measured corrector columns."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
from PyQt6.QtWidgets import QApplication, QFileDialog
from Knobs.DRCorrection.core import Options, serial
from Knobs.DRTuning.bump_presets import ranked_closure_helpers
from Knobs.DRTuning.coupling import BumpOptions, CouplingService
from Knobs.DRTuning.gui import TuningSession
from Knobs.DRTuning.tests.test_bump_dispersion import BumpFixture
from Knobs.DRTuning.tests.test_bump_presets import PresetBackend


class SharedResponseTests(unittest.TestCase):
    def setUp(self):
        self.b=BumpFixture()
        self.s=CouplingService(self.b,threading.Event())
        self.opt=BumpOptions(settle_s=0.)

    def measure(self,options=None,state=None):
        return self.s.measure_response_matrix(self.opt,options or Options(),state if state is not None else {},
                                              lambda *_:None,lambda _:None)

    def test_measured_matrix_roundtrip_and_different_steering_baseline(self):
        matrix=json.loads(json.dumps(serial(self.measure()),allow_nan=False))
        self.assertEqual(set(matrix['columns']),set(self.b.currents))
        for j,(n,entry) in enumerate(matrix['columns'].items()):
            np.testing.assert_allclose(entry['response_mm_per_A'],self.b.response[:,j],atol=1e-12)
        self.b.currents['H0']=.012
        g=self.b.geometry()
        g['centers']=self.b.read('correctors',g['actuators'])
        self.s.use_response_matrix(matrix)
        with patch.object(self.s,'probe_column',side_effect=AssertionError('repeated probe')):
            result=self.s.calibrate(g,self.opt)
        self.assertEqual(result['shared_columns'],g['actuators'])
        self.assertEqual(len(result['calibration_checks']),2)
        self.assertEqual(self.b.snapshot(),g['baseline'])
        self.b.offset=1000.
        with self.assertRaisesRegex(ValueError,'Optics or RF'):
            self.s.calibrate(dict(g,baseline=self.b.snapshot()),self.opt)

    def test_missing_column_and_invalid_required_row_remeasured(self):
        matrix=self.measure(Options(fixed_actuators=('H0',)))
        self.assertNotIn('H0',matrix['columns'])
        matrix['columns']['V2']['response_mm_per_A'][0]=np.nan
        self.s.use_response_matrix(matrix)
        with patch.object(self.s,'probe_column',wraps=self.s.probe_column) as probe:
            result=self.s.calibrate(self.b.geometry(),self.opt)
        self.assertEqual([call.args[0] for call in probe.call_args_list],['H0','V2'])
        self.assertEqual(len(result['shared_columns']),6)

    def test_same_matrix_used_for_two_targets_after_first_steering_change(self):
        b=PresetBackend('SF1R.2')
        b.response+=PresetBackend('SD1R.16').response
        service=CouplingService(b,threading.Event())
        optics={'QF1R':56.}
        b.bump_optics_currents=lambda:dict(optics)
        matrix=service.measure_response_matrix(self.opt,Options(),{},lambda *_:None,lambda _:None)
        service.use_response_matrix(matrix)
        with patch.object(service,'probe_column',side_effect=AssertionError('repeated probe')):
            for target in ('SF1R.2','SD1R.16'):
                calibration=service.calibrate_target(target,self.opt)
                self.assertEqual(calibration['actuators'],calibration['shared_columns'])
                b.write('correctors',calibration['actuators'],calibration['centers']+.01*calibration['knob_A_per_mm'])
        optics['QF1R']+=1.
        with self.assertRaisesRegex(ValueError,'Optics or RF'):service.use_response_matrix(matrix)

    def test_failed_probe_restores_and_resumes_at_same_supply(self):
        state={}
        orbit=self.b.orbit
        def fail_once():
            if self.b.acquisitions==23:raise ValueError('unavailable')
            return orbit()
        original=self.b.snapshot()
        with patch.object(self.b,'orbit',side_effect=fail_once),self.assertRaisesRegex(ValueError,'unavailable'):
            self.measure(state=state)
        self.assertEqual(self.b.snapshot(),original)
        self.assertEqual(list(state['columns']),['H0'])
        with patch.object(self.s,'probe_column',wraps=self.s.probe_column) as probe:
            self.measure(state=state)
        self.assertEqual(probe.call_count,7)
        self.assertEqual(self.b.snapshot(),original)

    def test_helper_uses_only_violating_plane_and_worst_plane_first(self):
        b=PresetBackend()
        g=CouplingService(b,threading.Event()).prepare('SF1R.2',self.opt)
        rng=np.random.default_rng(10)
        shape=np.sign(rng.normal(size=len(g['outside_indices'])))
        # A 21 µm RMS exceeds 20 µm even when combined XY RMS is below it.
        for h,v,expected in ((.021,.019,b.hcorrectors),(.019,.021,b.vcorrectors),
                             (.05,.04,b.hcorrectors),(.04,.05,b.vcorrectors)):
            ranked=ranked_closure_helpers(b,g,np.r_[h*shape,v*shape],self.opt)
            self.assertTrue(ranked)
            self.assertIn(ranked[0],expected)
            if min(h,v)<.020:self.assertTrue(set(ranked)<=set(expected))
        self.assertFalse(ranked_closure_helpers(b,g,np.r_[.01*shape,.01*shape],self.opt))
        residual=np.r_[.045*shape,0*shape]
        candidates=ranked_closure_helpers(b,g,residual,self.opt)
        preferred=candidates[-1]
        col=np.zeros(2*len(b.bpm_names))
        col[g['outside_indices']]=shape
        measured={n:{'response_mm_per_A':col if n==preferred else col*0} for n in candidates}
        self.assertEqual(ranked_closure_helpers(b,g,residual,self.opt,measured),[preferred])

    def test_panel_measure_save_load_and_disable(self):
        app=QApplication.instance() or QApplication([])
        window=TuningSession(self.b)
        panel=window.coupling_panel
        def synchronous(action,done,label):
            done(action(lambda *_:None))
        try:
            with tempfile.TemporaryDirectory() as tmp,patch.object(panel,'run',side_effect=synchronous):
                panel.run_data_directory=Path(tmp)
                panel.settle.setValue(0.)
                panel.measure_matrix()
                path,=Path(tmp).glob('*/bump_response_*.json')
                self.assertTrue(panel.use_matrix.isChecked())
                panel.use_matrix.setChecked(False)
                self.assertIsNone(panel.service.response_matrix)
                with patch.object(QFileDialog,'getOpenFileName',return_value=(str(path),'')):
                    panel.load_matrix()
                self.assertTrue(panel.use_matrix.isChecked())
                self.assertEqual(len(panel.service.response_matrix['columns']),8)
                wrong=copy.deepcopy(panel.matrix);wrong['bpm_names'].reverse()
                with self.assertRaisesRegex(ValueError,'mapping'):panel.service.use_response_matrix(wrong)
        finally:
            window.close();app.processEvents()
