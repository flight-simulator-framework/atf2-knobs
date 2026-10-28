"""Offline gun orbit restoration and manual Klystron selection regressions."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PyQt6.QtWidgets import QApplication
from Knobs import Linac_Opt as linac
from Knobs.linac_orbit_compensation import OrbitCompensation, CORRECTORS, mean_orbit


class OrbitPlant:
    def __init__(self):
        self.currents = np.zeros(4)
        self.reference = np.array([1., -2., .5, -.8])
        self.drift = np.zeros(4)
        self.response = np.array([[2., .4, 0., 0.], [.3, 1.5, 0., 0.],
                                  [0., 0., 1.8, .2], [0., 0., .4, 1.6]])
        self.writes = []
        self.fail_measure = False
        self.nsamples = 1

    def orbit(self):
        if self.fail_measure:
            raise ValueError("BPM disconnected")
        return self.reference + self.drift + self.response @ self.currents

    def read(self):
        return self.currents.copy()

    def write(self, values, *, restore=False):
        self.writes.append((dict(values), restore))
        for name, value in values.items():
            self.currents[CORRECTORS.index(name)] = value

    def get_bpms(self):
        orbit = self.orbit()
        # Reverse the two BPMs to verify name-based matching, not slot assumptions.
        return dict(names=["ML10L", "ML8L"], x=np.tile(orbit[[1, 0]], (self.nsamples, 1)),
                    y=np.tile(orbit[[3, 2]], (self.nsamples, 1)), tmit=np.ones((self.nsamples, 2)),
                    status=np.ones((self.nsamples, 2)))

    def read_current(self, pv_write, pv_read=None, quantize_phase=True):
        name = pv_write.split(":")[0]
        return self.currents[CORRECTORS.index(name)] if name in CORRECTORS else 0.

    def pv_put_many(self, values):
        self.write({pv.split(":")[0]: value for pv, value in values.items()})


class CompensationTests(unittest.TestCase):
    def make_correction(self, plant, limits=None, checkpoint=lambda: None):
        report = {}
        correction = OrbitCompensation(measure=plant.orbit, read=plant.read, write=plant.write,
            checkpoint=checkpoint, limits=limits or {name: (-.5, .5) for name in CORRECTORS}, report=report)
        correction.capture()
        return correction, report

    def test_horizontal_vertical_and_coupled_restoration(self):
        for drift, expected in (([.32, -.17, .01, 0], "H"), ([0, .01, .28, -.21], "V"),
                                ([.25, -.19, .29, -.22], "HV")):
            with self.subTest(plane=expected):
                plant = OrbitPlant()
                if expected == "HV":
                    plant.response[0, 2] = .3
                    plant.response[2, 0] = -.2
                correction, report = self.make_correction(plant)
                plant.drift = np.asarray(drift)
                self.assertTrue(correction.correct())
                self.assertEqual(report["planes"], expected)
                self.assertEqual(report["status"], "corrected")
                self.assertTrue(np.all(abs(plant.orbit() - plant.reference) < .1))
                for values, _ in plant.writes:
                    self.assertTrue(all(("H" if name.startswith("ZH") else "V") in expected for name in values))
                    self.assertTrue(all(abs(v) <= .5 for v in values.values()))
                previous = np.zeros(4)
                for iteration in report["iterations"]:
                    trial = np.asarray(iteration["currents_A"])
                    self.assertLessEqual(np.max(abs(trial - previous)), .100000001)
                    previous = trial

    def test_below_threshold_does_not_write_and_exact_threshold_triggers(self):
        plant = OrbitPlant()
        correction, report = self.make_correction(plant)
        plant.drift = np.array([.099, -.05, 0, .099])
        self.assertFalse(correction.correct())
        self.assertEqual(plant.writes, [])
        plant.reference[:] = 0
        correction, report = self.make_correction(plant)
        plant.drift += np.array([.1, 0, 0, 0])
        self.assertTrue(correction.correct())
        self.assertEqual(report["planes"], "H")

    def test_probe_failure_restores_current(self):
        plant = OrbitPlant()
        correction, report = self.make_correction(plant)
        plant.drift[0] = .2
        original = plant.write
        def write(values, *, restore=False):
            original(values, restore=restore)
            plant.fail_measure = not restore
        correction.write = write
        with self.assertRaisesRegex(ValueError, "disconnected"):
            correction.correct()
        np.testing.assert_array_equal(plant.currents, np.zeros(4))
        self.assertTrue(plant.writes[-1][1])

    def test_stop_during_trial_restores_last_accepted_state(self):
        plant = OrbitPlant()
        correction, report = self.make_correction(plant)
        plant.drift[0] = .3
        original = plant.write
        def write(values, *, restore=False):
            original(values, restore=restore)
            if not restore and len(values) > 1:
                raise InterruptedError("Stop")
        correction.write = write
        with self.assertRaises(InterruptedError):
            correction.correct()
        np.testing.assert_array_equal(plant.currents, np.zeros(4))

    def test_singular_response_is_rejected_without_persistent_probe(self):
        plant = OrbitPlant()
        plant.response[:2, :2] = 1.
        correction, _ = self.make_correction(plant)
        plant.drift[:2] = [.3, -.2]
        with self.assertRaisesRegex(ValueError, "singular"):
            correction.correct()
        np.testing.assert_array_equal(plant.currents, np.zeros(4))

    def test_crosstalk_into_untouched_plane_rolls_back(self):
        plant = OrbitPlant()
        plant.response[2, 0] = 50.
        correction, report = self.make_correction(plant)
        plant.drift[0] = .3
        with self.assertRaisesRegex(RuntimeError, "did not reach"):
            correction.correct()
        self.assertEqual(report["status"], "no_improvement")
        np.testing.assert_array_equal(plant.currents, np.zeros(4))
        self.assertFalse(any(name.startswith("ZV") for values, _ in plant.writes for name in values))

    def test_rail_limited_response_uses_available_probe_and_stops_bounded(self):
        plant = OrbitPlant()
        limits = {name: (-.02, .02) for name in CORRECTORS}
        correction, report = self.make_correction(plant, limits)
        plant.drift[0] = 2.
        with self.assertRaisesRegex(RuntimeError, "did not reach"):
            correction.correct()
        self.assertTrue(all(abs(value) <= .02 for values, _ in plant.writes for value in values.values()))
        self.assertLessEqual(len(report["iterations"]), 5)

    def test_three_sample_mean_name_matching_and_invalid_data(self):
        plant = OrbitPlant()
        plant.nsamples = 3
        data = plant.get_bpms()
        data["x"][:, 0] += [-.03, 0, .03]
        np.testing.assert_allclose(mean_orbit(data), plant.reference)
        for key, bad in (("x", np.nan), ("tmit", 0), ("status", 0)):
            with self.subTest(key=key):
                data = plant.get_bpms()
                data[key][1, 0] = bad
                with self.assertRaises(ValueError):
                    mean_orbit(data)
        plant.nsamples = 2
        with self.assertRaisesRegex(ValueError, "exactly 3"):
            mean_orbit(plant.get_bpms())

    def test_worker_brackets_all_gun_scans_and_uses_injected_interface(self):
        for method in ("SEQUENTIAL", "GROUP_BO", "GROUP_LBO"):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as directory:
                plant = OrbitPlant()
                cfg = dict(mode=method, gun_sol_l0_phase=True, gun_orbit_compensation=True, settle_sec=0,
                           gun_orbit_limits={f"{name}:currentWrite": [-.5, .5] for name in CORRECTORS})
                worker = linac.OptimizationWorker(cfg, directory, interface=plant)
                def scan(*args, **kwargs):
                    plant.drift = np.array([.3, -.15, 0, 0])
                with patch("epics.PV", side_effect=AssertionError("hardware access")), \
                     patch.object(worker, "perform_1d_scan", side_effect=scan), \
                     patch.object(worker, "_group_bo_simultaneous", side_effect=scan), \
                     patch.object(worker, "_lbo_group", side_effect=scan), \
                     patch.object(worker, "_evaluate_point_multi") as verification:
                    getattr(worker, {"SEQUENTIAL": "_run_sequential", "GROUP_BO": "_run_group_bo", "GROUP_LBO": "_run_group_lbo"}[method])()
                self.assertEqual(worker._gun_orbit_report["status"], "corrected")
                verification.assert_called_once()
                self.assertEqual(plant.nsamples, 1)
                self.assertTrue(set(f"{n}:currentWrite" for n in CORRECTORS).issubset(linac._restore_pvs_for_config(cfg)))

    def test_worker_saves_reference_response_and_samples_report(self):
        with tempfile.TemporaryDirectory() as directory:
            plant = OrbitPlant()
            cfg = dict(mode="GROUP_BO", gun_sol_l0_phase=True, gun_orbit_compensation=True, settle_sec=0,
                       gun_orbit_limits={f"{name}:currentWrite": [-.5, .5] for name in CORRECTORS})
            worker = linac.OptimizationWorker(cfg, directory, interface=plant)
            events = []
            worker.progress_signal.connect(events.append)
            def scan(*args):
                plant.drift[:2] = [.3, -.15]
            with patch.object(worker, "_dump_initial_snapshot"), patch.object(
                    worker, "_group_bo_simultaneous", side_effect=scan), patch.object(worker, "_evaluate_point_multi"):
                worker.run()
            self.assertFalse(any(e["kind"] == "run_error" for e in events))
            paths = list(Path(directory).rglob("GunOrbitCompensation_*.json"))
            self.assertEqual(len(paths), 1)
            report = json.loads(paths[0].read_text())
            self.assertEqual(report["status"], "corrected")
            self.assertEqual(report["samples_per_mean"], 3)
            self.assertTrue(report["probes"])
            self.assertEqual(np.asarray(report["acquisitions"][0]["x_samples_mm"]).shape, (3, 2))

    def test_worker_option_off_makes_no_orbit_reads_or_corrector_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            plant = OrbitPlant()
            worker = linac.OptimizationWorker(dict(gun_sol_l0_phase=True), directory, interface=plant)
            with patch.object(plant, "get_bpms", side_effect=AssertionError("disabled")), \
                 patch.object(worker, "perform_1d_scan"):
                worker._run_sequential()
            self.assertEqual(plant.writes, [])
            self.assertEqual(worker._gun_orbit_report, {})

    def test_unconfirmed_current_causes_probe_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            plant = OrbitPlant()
            cfg = dict(gun_sol_l0_phase=True, gun_orbit_compensation=True, settle_sec=0,
                       gun_orbit_limits={f"{name}:currentWrite": [-.5, .5] for name in CORRECTORS})
            worker = linac.OptimizationWorker(cfg, directory, interface=plant)
            worker._begin_gun_orbit_compensation()
            plant.drift[0] = .3
            with patch.object(plant, "read_current", return_value=0.):
                with self.assertRaisesRegex(RuntimeError, "readback"):
                    worker._finish_gun_orbit_compensation()
            np.testing.assert_array_equal(plant.currents, np.zeros(4))


class KlystronSelectionTests(unittest.TestCase):
    def test_selection_drives_all_runners_budgets_and_delta_report(self):
        for mode in ("SEQUENTIAL", "GROUP_BO", "GROUP_LBO"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                config = dict(mode=mode, kly_phase=True, kly_group_1_members=[2, 4], kly_group_2_members=[7])
                interface = Mock()
                interface.read_current.return_value = 0.
                worker = linac.OptimizationWorker(config, directory, interface=interface)
                with patch.object(worker, "perform_1d_scan") as sequential, \
                     patch.object(worker, "_group_bo_simultaneous") as group_bo, \
                     patch.object(worker, "_lbo_group") as lbo:
                    getattr(worker, {"SEQUENTIAL": "_run_sequential", "GROUP_BO": "_run_group_bo", "GROUP_LBO": "_run_group_lbo"}[mode])()
                    pvs = ([call.args[1] for call in sequential.call_args_list] if mode == "SEQUENTIAL" else
                           [p[1] for call in (group_bo if mode == "GROUP_BO" else lbo).call_args_list for p in call.args[1]])
                self.assertEqual(pvs, ["CM2L:phaseWrite", "CM4L:phaseWrite", "CM7L:phaseWrite"])
                self.assertEqual([spec[1] for spec in linac._main_run_delta_specs(config)], pvs)
                budgets = linac._run_evaluation_budgets(config)
                if mode == "GROUP_BO":
                    self.assertEqual(budgets, {"L1-4": 40, "L5-8": 30})

    def test_empty_groups_legacy_defaults_and_combined_l8(self):
        config = dict(mode="GROUP_BO", kly_phase=True, kly_group_1_members=[], kly_group_2_members=[8],
                      correctors=True, correctors_l8=True)
        self.assertEqual(linac.selected_klystrons(config, 1), [])
        self.assertEqual(linac.selected_klystrons(config, 2), [])
        self.assertEqual(linac.selected_klystrons({"kly_phase": True}, 1), [1, 2, 3, 4])
        with self.assertRaises(ValueError):
            linac.selected_klystrons({"kly_phase": True, "kly_group_1_members": [8]}, 1)

    def test_gui_membership_budget_and_runtime_lock(self):
        app = QApplication.instance() or QApplication([])
        with patch.object(linac.MainWindow, "_refresh_current_values"), patch.object(
                linac.MainWindow, "_refresh_developer_live_values"), patch.object(linac, "DRBPMReader"):
            window = linac.MainWindow()
        window._current_value_timer.stop()
        try:
            window.mode_box.setCurrentText("GROUP_BO")
            window.kly_member_checks[1].setChecked(False)
            window.kly_member_checks[3].setChecked(False)
            self.assertEqual(linac.selected_klystrons(window._kly_selection_config(), 1), [2, 4])
            self.assertEqual(window.group_bo_max_eval_spinboxes["kly_group_1"].value(), 40)
            window.group_bo_max_eval_spinboxes["kly_group_1"].setValue(99)
            window.kly_member_checks[2].setChecked(False)
            self.assertEqual(window.group_bo_max_eval_spinboxes["kly_group_1"].value(), 99)
            self.assertFalse(window.chk_gun_orbit_compensation.isChecked())
            with patch.object(window, "_refresh_current_values"):
                window._apply_config_to_ui({"mode": "GROUP_BO", "kly_phase": True,
                                           "kly_group_1_members": [1, 3], "kly_group_2_members": [6],
                                           "gun_orbit_compensation": True})
            self.assertEqual(linac.selected_klystrons(window._kly_selection_config(), 1), [1, 3])
            self.assertEqual(linac.selected_klystrons(window._kly_selection_config(), 2), [6])
            self.assertEqual(window.group_bo_max_eval_spinboxes["kly_group_1"].value(), 40)
            self.assertTrue(window.chk_gun_orbit_compensation.isChecked())
            window._set_main_run_controls_enabled(False)
            self.assertTrue(all(not box.isEnabled() for box in window.kly_member_checks.values()))
            self.assertFalse(window.chk_gun_orbit_compensation.isEnabled())
        finally:
            window.close()
            app.processEvents()


if __name__ == "__main__":
    unittest.main()
