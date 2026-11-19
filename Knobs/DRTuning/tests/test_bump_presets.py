"""Preset selection, live eligibility and disabled position controls."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from Knobs.DRCorrection.core import Options
from Knobs.DRTuning.bump_maps import select_geometry,BumpGeometryUnavailable
from Knobs.DRTuning.bump_presets import bump_preset,bump_targets
from Knobs.DRTuning.coupling import CouplingService,BumpOptions
from Knobs.DRTuning.tests.test_bump_dispersion import BumpFixture,DispersionFixture


class PresetGeometryTests(unittest.TestCase):
    def setUp(self):
        self.bpms=['left','right','out1','out2','out3','out4']
        self.v=[f'V{i}' for i in range(4)];self.h=[f'H{i}' for i in range(4)]
        self.selected=self.v+self.h;self.centers=np.zeros(8)
        self.lower=np.full(8,-1.);self.upper=np.full(8,1.)
        def drift(s):
            m=np.eye(4);m[0,1]=s;m[2,3]=s;return m
        positions=dict(zip(self.bpms,[49,51,10,20,80,90]))
        positions.update(zip(self.selected,[45,46,54,55,44,43,56,57]))
        rows={n:dict(matrix=drift(s-50),s_m=s) for n,s in positions.items()}
        rows['target']=dict(matrix=np.eye(4),s_m=50)
        self.table=dict(rows=rows,circumference_m=100,one_turn_matrix=np.eye(4),source='test')
        self.response=np.zeros((12,8))
        for i,n in enumerate(self.bpms[:2]):
            self.response[[i,i+6],:4]=rows[n]['matrix'][[0,2]]
        self.model=SimpleNamespace(bpm_names=self.bpms,vcorrectors=self.v,hcorrectors=self.h,
            actuators={},positions_m=np.array([positions[n] for n in self.bpms]),settings={},check_cancel=lambda:None,
            read=lambda _,names:np.array([self.centers[self.selected.index(n)] for n in names]),
            limits=lambda _,names:np.array([[self.lower[self.selected.index(n)] for n in names],
                                          [self.upper[self.selected.index(n)] for n in names]]))
        self.preset=dict(id='test',internal_bpms=self.bpms[:2],actuators=self.selected)
    def select(self,**kwargs):
        with patch('Knobs.DRTuning.bump_maps.periodic_response',return_value=self.response):
            return select_geometry(self.model,'SF1R.1',kwargs.pop('valid',np.ones(6,bool)),
                preferred=kwargs.pop('preferred',self.preset),table=self.table,amplitude_mm=.1,min_width_mm=.4,**kwargs)
    def test_preset_uses_current_centers_and_bounds_and_records_source(self):
        self.centers[2]=.1;self.lower[2]=-.05;self.upper[2]=.55
        g=self.select()
        self.assertEqual(g['selection']['method'],'preset')
        np.testing.assert_allclose(g['centers'],self.centers)
        np.testing.assert_allclose(g['predicted_range_mm'],[-.15,.45],atol=1.e-7)
    def test_missing_preset_device_falls_back_with_reason(self):
        g=self.select(preferred=dict(self.preset,internal_bpms=['absent','right']))
        self.assertEqual(g['selection']['method'],'automatic')
        self.assertIn('absent',g['selection']['preset_rejection'])
    def test_insufficient_current_budget_is_unavailable_even_if_calibration_fits(self):
        self.lower[:]=-.15;self.upper[:]=.15
        with self.assertRaisesRegex(BumpGeometryUnavailable,'Insufficient current budget'):
            self.select()
    def test_lost_bpms_and_fixed_correctors_are_not_used(self):
        with self.assertRaises(BumpGeometryUnavailable):self.select(valid=np.zeros(6,bool))
        with self.assertRaises(BumpGeometryUnavailable):self.select(fixed=('V0',))
    def test_catalog_has_all_68_verified_combinations(self):
        self.assertEqual(len(bump_targets()),68)
        for target in bump_targets():
            p=bump_preset(target)
            self.assertEqual(len(set(p['internal_bpms'])),2)
            self.assertEqual(len(set(p['actuators'])),8)
            self.assertTrue(all(n.startswith('ZV') for n in p['actuators'][:4]))
            self.assertTrue(all(n.startswith('ZH') for n in p['actuators'][4:]))


class PositionEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.backend=BumpFixture();self.service=CouplingService(self.backend,threading.Event())
        self.targets=['SD1R.1','SF1R.1']
        self.model=SimpleNamespace(bpm_names=self.backend.bpm_names,elements=dict.fromkeys(self.targets),settings={})
    def run_check(self,selection):
        with patch.object(self.service.reader.model_manager,'prepare',return_value=self.model),\
             patch('Knobs.DRTuning.coupling.transfer_table',return_value={}) as table,\
             patch('Knobs.DRTuning.coupling.bump_targets',return_value=self.targets),\
             patch.object(self.service,'select',side_effect=selection):
            result=self.service.available_targets(BumpOptions(),Options(model_match_tunes=False))
            table.assert_called_once()
        self.assertEqual(self.backend.commands,[])
        return result
    def test_failed_geometry_disables_only_that_position(self):
        report=self.run_check([BumpGeometryUnavailable('BPMs unavailable'),self.backend.geometry()])
        self.assertFalse(report['rows'][0]['available']);self.assertTrue(report['rows'][1]['available'])
        self.assertEqual(report['rows'][0]['reason'],'BPMs unavailable')
    def test_io_failure_is_not_misreported_as_unavailable_geometry(self):
        with self.assertRaisesRegex(TimeoutError,'offline'):self.run_check(TimeoutError('offline'))
    def test_changed_settings_discard_availability(self):
        def changed(*args):self.backend.offset+=1;return self.backend.geometry()
        with self.assertRaisesRegex(ValueError,'settings changed'):self.run_check(changed)


class PositionEligibilityGuiTests(unittest.TestCase):
    def test_gray_positions_cannot_be_selected_for_preparation_and_can_be_rechecked(self):
        from PyQt6.QtWidgets import QApplication
        from Knobs.DRTuning.gui import TuningSession
        app=QApplication.instance() or QApplication([]);window=TuningSession(DispersionFixture())
        try:
            panel=window.coupling_panel
            self.assertTrue(panel.options().use_presets)
            self.assertIn('Default BPMs:',panel.preset_status.text())
            report=dict(rows=[dict(target='SD1R.1',available=False,reason='Insufficient current budget'),
                             dict(target='SF1R.1',available=True,internal_bpms=['left','right'])])
            panel.positions_checked(report)
            self.assertFalse(panel.target.model().item(0).isEnabled())
            self.assertIn('current budget',panel.target.model().item(0).toolTip())
            self.assertEqual(panel.target.currentText(),'SF1R.1')
            panel.target.setCurrentIndex(0)
            with patch.object(window,'error') as error,patch.object(window,'start') as start:
                panel.prepare();error.assert_called_once();start.assert_not_called()
            panel.step.setValue(3.)
            self.assertIsNone(panel.availability);self.assertTrue(panel.target.model().item(0).isEnabled())
            panel.positions_checked(dict(rows=[]))
            self.assertEqual(panel.target.currentIndex(),-1);self.assertFalse(panel.select_button.isEnabled())
            self.assertEqual(window.backend.commands,[])
        finally:window.close();app.processEvents()
