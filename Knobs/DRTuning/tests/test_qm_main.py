import copy
import importlib.util
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from Knobs.DRTuning.dispersion import DispersionOptions, DispersionService
from Knobs.DRTuning.epics import LiveBackend
from Knobs.DRTuning.hardware import QM_MAIN_CANDIDATES, load_hardware_profile
from Knobs.DRTuning.qm_checkpoint import save_checkpoint
from Knobs.DRTuning.tests.test_bump_dispersion import DispersionFixture


class MainFixture(DispersionFixture):
    def __init__(self):
        super().__init__()
        self.main_quadrupoles = {n: dict(limits_A=[8., 12.]) for n in QM_MAIN_CANDIDATES}
        self.mains = dict.fromkeys(QM_MAIN_CANDIDATES, 10.)

    def quadrupole_current(self, n):
        return self.mains[n] if n in self.mains else super().quadrupole_current(n)

    def validate_quadrupole_targets(self, n, values):
        if n in self.mains:
            if not np.all((np.array(values) >= 8) & (np.array(values) <= 12)):
                raise ValueError('Main current limit')
        else:
            super().validate_quadrupole_targets(n, values)

    def set_quadrupole_current(self, n, value):
        if n in self.mains:
            self.validate_quadrupole_targets(n, [value])
            self.mains[n] = value
            self.commands.append((n, value))
        else:
            super().set_quadrupole_current(n, value)

    def orbit(self):
        frame = super().orbit()
        shifts = [self.mains[n.split('.')[0]] - 10. for n in self.q]
        frame['x'] += self.dr @ shifts * (-self.offset / (self.nominal_frequency_hz * .0021))
        return frame


class MainCoilTests(unittest.TestCase):
    def test_live_main_current_limits_write_and_restore(self):
        profile = load_hardware_profile()
        client = Mock(spec=['caget', 'caput'])
        values = {'QM2R:currentRead': 12., 'QM2R:parameter.A': 10., 'QM2R:parameter.B': 15.}
        client.caget.side_effect = lambda pv, **kw: values[pv]
        def write(pv, value, **kw):
            self.assertEqual(pv, 'QM2R:currentWrite')
            values['QM2R:currentRead'] = value
            return 1
        client.caput.side_effect = write
        b = LiveBackend(profile, client)
        self.assertEqual(tuple(b.main_quadrupoles), QM_MAIN_CANDIDATES)
        with patch('Knobs.DRTuning.epics.time.sleep'):
            b.set_quadrupole_current('QM2R', 12.1)
            self.assertEqual(b.quadrupole_current('QM2R'), 12.1)
            with self.assertRaisesRegex(ValueError, 'supply current limits'):
                b.set_quadrupole_current('QM2R', 16.)
            b.restore_quadrupole_current('QM2R', 12.)
        self.assertEqual(client.caput.call_count, 2)
        self.assertEqual(values['QM2R:currentRead'], 12.)

    def test_main_scan_apply_refit_restore(self):
        b = MainFixture()
        s = DispersionService(b, threading.Event())
        opt = DispersionOptions(coil='main', step_A=.01)
        data = s.calculate(opt)
        self.assertEqual(data['actuators'], list(QM_MAIN_CANDIDATES))
        self.assertEqual(len(data['probe_readbacks']), 8)
        for probe in data['probe_readbacks']:
            np.testing.assert_allclose(probe['requested'], [9.9, 10.1])
        s.validate_checkpoint(data)
        with self.assertRaisesRegex(ValueError, 'selection'):
            s.recalculate(data, DispersionOptions())
        b.mains['QM2R'] += .05
        with self.assertRaisesRegex(ValueError, 'Settings changed'):
            s.apply(data)
        b.mains['QM2R'] = 10.
        s.apply(data)
        second = s.recalculate(data, opt)
        s.apply(second)
        s.reset(second)
        self.assertEqual(set(b.mains.values()), {10.})
        self.assertEqual(set(b.q.values()), {0.})
        self.assertEqual({name for name, _ in b.commands}, set(QM_MAIN_CANDIDATES))

    @unittest.skipUnless(importlib.util.find_spec('RF_Track'), 'native RF-Track unavailable')
    def test_rftrack_shared_main_with_independent_trims(self):
        from Knobs.DRTuning.rftrack import RFTrackBackend
        b = RFTrackBackend()
        members = b.main_quadrupoles['QM2R']['members']
        def strengths():
            return np.array([b.qelements[n].get_K1L(-1299.9999) for n in members])
        b.set_quadrupole_current(members[0], .1)
        saved = b.snapshot()
        fields = strengths()
        original = b.quadrupole_current('QM2R')
        b.set_quadrupole_current('QM2R', original + .1)
        self.assertTrue(np.all(np.abs(strengths() - fields) > 1.e-8))
        self.assertEqual([b.quadrupole_current(n) for n in members], [.1, 0.])
        b.restore(saved)
        np.testing.assert_allclose(strengths(), fields, atol=1.e-12)
        self.assertEqual(b.quadrupole_current('QM2R'), original)

    def test_gui_mode_exclusions_and_checkpoint(self):
        from PyQt6.QtWidgets import QApplication, QFileDialog
        from Knobs.DRTuning.gui import TuningSession
        app = QApplication.instance() or QApplication([])
        window = TuningSession(MainFixture())
        try:
            panel = window.qm_dispersion
            self.assertNotIn('QM2R.1', panel.options().actuators())
            panel.coil.setCurrentText('Main')
            self.assertEqual(panel.excluded.text(), '')
            self.assertEqual(panel.table.rowCount(), 8)
            panel.excluded.setText('QM4R')
            data = panel.service.calculate(panel.options())
            panel.display(data)
            self.assertTrue(panel.set_button.isEnabled())
            panel.coil.setCurrentText('Trim')
            self.assertFalse(panel.set_button.isEnabled())
            self.assertFalse(panel.recalculate_button.isEnabled())
            self.assertIn('QM3R.2', panel.excluded.text())
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'scan.json'
                save_checkpoint(path, data)
                with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(path), 'JSON')):
                    panel.load_scan()
            self.assertEqual(panel.coil.currentText(), 'Main')
            self.assertEqual(panel.excluded.text(), 'QM4R')
            self.assertTrue(panel.set_button.isEnabled())
            mixed = copy.deepcopy(data)
            mixed['options']['coil'] = 'trim'
            with self.assertRaises(ValueError):
                panel.service.validate_checkpoint(mixed)
        finally:
            window.close()
            app.processEvents()
