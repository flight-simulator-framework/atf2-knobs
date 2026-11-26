"""Launch and default matrix selection without EPICS communication."""
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from Knobs.DRCorrection.core import Options
from Knobs.DRTuning import __main__ as launcher
from Knobs.DRTuning.dispersion import DispersionOptions
from Knobs.DRTuning.gui import TuningSession
from Knobs.DRTuning.qm_checkpoint import save_checkpoint
from Knobs.DRTuning.tests.test_qm_main import MainFixture


class StartupTests(unittest.TestCase):
    def test_default_entry_opens_epics_session_directly(self):
        with patch.object(launcher,'QApplication') as app,patch.object(launcher,'LiveBackend') as live, \
             patch.object(launcher,'TuningSession') as window,patch.object(launcher,'DRTuningWindow') as chooser:
            self.assertEqual(launcher.main(),app.return_value.exec.return_value)
            chooser.assert_not_called()
            window.assert_called_once_with(live.return_value)
            window.return_value.show.assert_called_once()
            window.return_value.load_latest_matrices.assert_called_once()

    def test_latest_valid_files_only_and_coil_switching(self):
        app=QApplication.instance() or QApplication([])
        b=MainFixture()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            w=TuningSession(b,QSettings(str(root/'prefs.ini'),QSettings.Format.IniFormat))
            self.addCleanup(w.close)
            qm=w.qm_dispersion
            vertical=w.panels['dispersion']
            bump=w.coupling_panel
            folder=root/'2025'
            folder.mkdir()
            qm.autosave_directory=root/'2026'
            vertical.data_directory=lambda:str(root/'2026')
            bump.run_data_directory=root
            for coil in ('trim','main'):
                opt=DispersionOptions(coil=coil)
                data=qm.service.matrix_from_scan(qm.service.calculate(opt))
                data['matrix_measured_at']='2025-04-11T12:30:00+00:00'
                save_checkpoint(folder/f'qm_scan_{coil}_20250411_130000.checkpoint.json',data)
                data['matrix_measured_at']='2025-04-10T12:30:00+00:00'
                save_checkpoint(folder/f'qm_dispersion_{coil}_20250412_130000.json',data)
            (folder/'qm_scan_trim_20250415_160000.json').write_text('{')
            n=len(b.bpm_names)
            mag=b.vcorrectors[0]
            zv=dict(schema='dr-correction-1',mode='dispersion',options=asdict(Options(plane='y')),
                    names=b.bpm_names,positions_m=b.positions_m,actuators=[mag],valid=np.ones(n,bool),
                    response=np.ones((2*n,1)),full_response=np.ones((4*n,1)),full_zv_candidates=[mag],
                    actuator_unit='A',kind='correctors',backend_id=b.identity,delta=[123.],applied=True)
            save_checkpoint(folder/'vertical_dispersion.json',zv)
            save_checkpoint(folder/'vertical_dispersion_newer.json',dict(zv,backend_id='other machine'))
            shared=dict(schema='dr-bump-response-1',machine=b.identity,bpm_names=b.bpm_names,
                        measured_unix_s=1700000000.,columns={mag:dict(response_mm_per_A=np.ones(2*n),
                            readback=dict(magnet=mag,actual=[-.1,.1]))})
            save_checkpoint(folder/'bump_response_20250410_120000.json',shared)
            # Startup and switching coils may inspect files, but never measure or apply.
            with ExitStack() as stack:
                for method in ('write','orbit','snapshot','set_frequency','set_quadrupole_current'):
                    stack.enter_context(patch.object(b,method,side_effect=AssertionError(method)))
                w.load_latest_matrices()
                self.assertTrue(w.task.wait(10000))
                app.processEvents()
                self.assertFalse(w._failed,w.messages.toPlainText())
                self.assertIsNone(vertical.data)
                self.assertTrue(vertical.matrix['matrix_only'])
                self.assertNotIn('delta',vertical.matrix)
                self.assertTrue(vertical.recalculate_button.isEnabled())
                self.assertTrue(bump.use_matrix.isChecked())
                self.assertIs(bump.service.response_matrix,bump.matrix)
                for coil in ('trim','main','trim'):
                    qm.coil.setCurrentIndex(qm.coil.findData(coil))
                    self.assertEqual(qm.matrix_path.name,f'qm_scan_{coil}_20250411_130000.checkpoint.json')
                    self.assertTrue(qm.matrix['matrix_only'])
                    self.assertIsNone(qm.result)
                    self.assertIsNone(qm.observation)
                    self.assertTrue(qm.recalculate_button.isEnabled())
                    self.assertFalse(qm.set_button.isEnabled())
