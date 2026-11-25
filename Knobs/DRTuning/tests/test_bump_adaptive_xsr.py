"""Adaptive XSR decisions on measured positions and resumed scans."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np

from Knobs.DRTuning.bump_fit import historical_sem_floor, next_xsr_point, size_fit
from Knobs.DRTuning.coupling import BumpOptions, CouplingService
from Knobs.DRTuning.tests.test_bump_dispersion import BumpFixture


class AdaptiveXSRTests(unittest.TestCase):
    def setUp(self):
        self.b=BumpFixture()
        self.s=CouplingService(self.b,threading.Event())
        self.opt=BumpOptions(settle_s=0.)
        self.g=self.s.calibrate(self.b.geometry(),self.opt)

    def test_six_seven_or_eight_points_follow_uncertainty(self):
        for floor,expected in ((.01,6),(.13,7),(.17,8)):
            with self.subTest(floor=floor):
                self.s.xsr_history_floor_um=floor
                result=self.s.scan(self.g,self.opt)
                self.assertEqual(len(result['points']),expected)
                self.assertEqual(result['points'][-1]['phase'],'final verification')
                self.assertAlmostEqual(result['final_amplitude_mm'],.13,places=5)
                self.s.reset(result)

    def test_both_edges_expand_within_one_mm_and_require_measured_bracket(self):
        for minimum in (-.46,.46):
            self.b.minimum=minimum
            result=self.s.scan(self.g,self.opt)
            np.testing.assert_allclose(result['xsr_bounds_mm'],[-.5,.5])
            np.testing.assert_allclose(result['refinement_mm'],[np.sign(minimum)*.5],atol=1e-6)
            self.assertAlmostEqual(result['final_amplitude_mm'],minimum,places=5)
            self.assertTrue(all(p['orbit']['closed'] for p in result['points']))
            self.s.reset(result)

    def test_restarts_at_failed_expansion_without_repeating_coarse_points(self):
        self.b.minimum=.46
        read=self.b.sample_xsr
        calls=[]
        def failed_once(*args,**kwargs):
            calls.append(self.b.vector()[8])
            if len(calls)==6:raise TimeoutError('XSR update')
            return read(*args,**kwargs)
        with patch.object(self.b,'sample_xsr',side_effect=failed_once):
            with self.assertRaises(TimeoutError):self.s.scan(self.g,self.opt)
            saved=self.s.last_result
            result=self.s.scan(self.g,self.opt,resume=saved)
        np.testing.assert_allclose(calls,[-.4,-.2,0,.2,.4,.5,.5,.46],atol=1e-6)
        self.assertTrue(result['applied'])

    def test_flat_scan_skips_after_five_and_restores_verified_currents(self):
        original=self.b.snapshot()
        read=self.b.sample_xsr
        def flat(*args,**kwargs):
            self.b.minimum=self.b.vector()[8]
            return read(*args,**kwargs)
        with patch.object(self.b,'sample_xsr',side_effect=flat):
            result=self.s.scan(self.g,self.opt)
        self.assertIn('Flat XSR',result['skip_reason'])
        self.assertTrue(result['restored']);self.assertFalse(result['applied'])
        self.assertEqual(len(result['points']),5)
        self.assertEqual(self.b.snapshot(),original)

    def test_prior_noise_floor_rejects_other_machines_and_preserves_candidate_order(self):
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root)
            for index,machine in enumerate(['demo']*3+['plant']):
                data=dict(schema='dr-bump-xsr-history-1',machine=machine,median_sem_um=.01,
                          verification_error_um=.22 if machine=='demo' else 7.)
                (folder/f'bump_SD1R.1_20261125_120000_{index}.xsr.json').write_text(json.dumps(data))
            self.assertAlmostEqual(historical_sem_floor(folder,'demo'),1.4826*.22/np.sqrt(2))
            self.assertEqual(historical_sem_floor(folder,'unknown'),0.)
        for minimum in (-.13,.13):
            points=[dict(amplitude_mm=x,requested_amplitude_mm=x,phase='coarse',
                         xsr=dict(mean=np.sqrt(100+300*(x-minimum)**2),sem=.13)) for x in np.linspace(-.4,.4,5)]
            points[2]['xsr']['sem']=0.
            fit=size_fit(points,[-.4,.4],.13)
            candidate,phase=next_xsr_point(fit,points,dict(bounds_mm=[-.4,.4]),.1,[])
            self.assertEqual(phase,'refinement')
            self.assertAlmostEqual(candidate,minimum-np.sign(minimum)*.1)

    def test_export_contains_replayable_points_and_supplies_next_scan_floor(self):
        from PyQt6.QtWidgets import QApplication
        from Knobs.DRTuning.gui import TuningSession
        result=self.s.scan(self.g,self.opt)
        app=QApplication.instance() or QApplication([])
        window=TuningSession(self.b)
        try:
            with tempfile.TemporaryDirectory() as root:
                panel=window.coupling_panel
                panel.tempfiles_directory=Path(root);panel.finish_scan(result)
                path,=Path(root).glob('*.xsr.json')
                data=json.loads(path.read_text())
                self.assertEqual(len(data['points']),6)
                self.assertEqual(len(data['fits'][0]['ci95_mm']),2)
                self.assertTrue(path.with_suffix('').with_suffix('.png').is_file())
                self.assertGreater(historical_sem_floor(Path(root),self.b.identity),0.)
        finally:
            window.close();app.processEvents()
