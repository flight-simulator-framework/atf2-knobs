"""Selected-only start snapshots and audited resets, with no machine access."""
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from Knobs import Linac_Opt as opt


class Interface:
    def __init__(self, values):
        self.values = dict(values)
        self.reads = []
        self.writes = []

    def pv_get(self, pv, default=np.nan):
        self.reads.append(pv)
        return self.values.get(pv, default)

    def pv_put_many(self, values):
        self.writes.append(dict(values))
        self.values.update(values)

    def read_current(self, *args, **kwargs):
        raise AssertionError("Must not use the legacy zero fallback for live snapshots")


class InitialRestoreTests(unittest.TestCase):
    def setUp(self):
        sleeper = patch.object(opt.time, "sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def window(self, interface, directory):
        return SimpleNamespace(_ensure_interface=lambda: interface,
                               feedback_panel=SimpleNamespace(active=False),
                               save_path=Path(directory), _append_log_text=Mock(), append_log=Mock())

    def test_selection_overrides_legacy_broad_restore_list(self):
        for key in ("timing", "timing_fine"):
            cfg = {key: True, "restore_pvs": opt.RESTORE_PVS,
                   "developer_actuators": [{"pv_write": "ZH1L:currentWrite"}]}
            self.assertEqual(opt._restore_pvs_for_config(cfg), [opt.TIMING_PV])
        self.assertEqual(opt._restore_pvs_for_config({"restore_pvs": opt.RESTORE_PVS}), [])
        self.assertEqual(opt._restore_pvs_for_config(dict(kly_phase=True,
                         kly_group_1_members=[2, 4], grp_L5_8=False)),
                         ["CM2L:phaseWrite", "CM4L:phaseWrite"])
        for group, count in (("qa", 5), ("qm", 3)):
            self.assertEqual(opt._restore_pvs_for_config({group: True}),
                             [f"{group.upper()}{i}L:currentWrite" for i in range(1, count + 1)])

    def test_selected_correctors_combined_l8_and_gun_auto_restore(self):
        actuators = [{"pv_write": pv} for pv in ("ZH1L:currentWrite", "CM8L:phaseWrite", opt.TIMING_PV)]
        for cfg in (dict(correctors=True, correctors_l8=True, timing_fine=True),
                    dict(run_profile=opt.RUN_PROFILE_DEVELOPER, qa=True)):
            cfg["developer_actuators"] = actuators
            self.assertEqual(set(opt._restore_pvs_for_config(cfg)), {a["pv_write"] for a in actuators})
        cfg = dict(gun_sol_l0_phase=True, gun_orbit_compensation=True)
        self.assertEqual(set(opt._restore_pvs_for_config(cfg)),
                         {s[1] for s in opt.TARGET_VALUE_SPECS["gun_sol_l0"]}
                         | {f"{n}:currentWrite" for n in opt.GUN_CORRECTORS})

    def test_capture_only_selected_exact_setpoints_and_preserve_valid_zero(self):
        for value in (1120.37, 0.):
            interface = Interface({opt.TIMING_PV: value})
            window = self.window(interface, "/unused")
            cfg = dict(timing=True, restore_pvs=opt.RESTORE_PVS)
            with patch.object(opt, "_read_icts_with_retry", return_value=({"L0": 1., "DR": .9, "Ttot": .9}, True, "")):
                origin = opt.MainWindow._capture_machine_origin(window, cfg)
            self.assertEqual(origin["initial_setpoints"], {opt.TIMING_PV: value})
            self.assertEqual(interface.reads, [opt.TIMING_PV])
            self.assertEqual(interface.writes, [])

    def test_capture_unavailable_selected_value_fails_before_objective_or_writes(self):
        for value in (None, np.nan, np.inf, -np.inf):
            interface = Interface({opt.TIMING_PV: value})
            with patch.object(opt, "_read_icts_with_retry") as objective:
                with self.assertRaisesRegex(ValueError, "Cannot read parameter.*after 5 attempts"):
                    opt.MainWindow._capture_machine_origin(self.window(interface, "/unused"), {"timing": True})
            objective.assert_not_called()
            self.assertEqual(interface.writes, [])
            self.assertEqual(interface.reads, [opt.TIMING_PV] * 5)

    def test_initial_capture_recovers_on_last_attempt_without_zero_substitution(self):
        interface = Interface({})
        interface.pv_get = Mock(side_effect=[None, np.nan, ConnectionError("Disconnected"), np.inf, 1120.37])
        log = Mock()
        values = opt._read_initial_setpoints(interface, [opt.TIMING_PV], log_fn=log)
        self.assertEqual(values, {opt.TIMING_PV: 1120.37})
        self.assertEqual(interface.pv_get.call_count, 5)
        self.assertEqual([c.args for c in self.sleep.call_args_list], [(0.5,)] * 4)
        self.assertEqual(sum("[READ RETRY]" in c.args[0] for c in log.call_args_list), 4)
        self.assertIn("[READ RECOVERED]", log.call_args.args[0])
        self.assertEqual(interface.writes, [])

    def test_successful_channels_are_not_reread_and_real_zero_is_valid(self):
        interface = Interface({})
        interface.pv_get = Mock(side_effect=[1120.37, None, 0.])
        values = opt._read_initial_setpoints(interface, [opt.TIMING_PV, "CM8L:phaseWrite"])
        self.assertEqual(values, {opt.TIMING_PV: 1120.37, "CM8L:phaseWrite": 0.})
        self.assertEqual([c.args[0] for c in interface.pv_get.call_args_list],
                         [opt.TIMING_PV, "CM8L:phaseWrite", "CM8L:phaseWrite"])
        self.sleep.assert_called_once_with(.5)

    def test_retry_can_be_stopped_without_further_reads(self):
        interface = Interface({})
        interface.pv_get = Mock(return_value=None)
        checkpoint = Mock(side_effect=[None, InterruptedError("Stop")])
        with self.assertRaises(InterruptedError):
            opt._read_initial_setpoints(interface, [opt.TIMING_PV], checkpoint=checkpoint)
        self.assertEqual(interface.pv_get.call_count, 1)
        self.assertEqual(interface.writes, [])

    def test_scan_readback_retries_same_channel_without_write_pv_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            interface = Interface({"CM2L:phaseWrite": 0.})
            interface.pv_get = Mock(side_effect=[None, 12.345])
            worker = opt.OptimizationWorker({}, directory, interface=interface)
            worker._interface_injected = False
            self.assertEqual(worker._read_current("CM2L:phaseWrite", "CM2L:phaseRead"), 12.5)
            self.assertEqual([c.args[0] for c in interface.pv_get.call_args_list], ["CM2L:phaseRead"] * 2)
            self.assertEqual(interface.writes, [])

    def test_reset_current_read_failure_retries_and_sends_no_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            interface = Interface({})
            origin = dict(config=dict(timing=True), initial_setpoints={opt.TIMING_PV: 1120.37})
            with self.assertRaisesRegex(ValueError, "after 5 attempts"):
                opt.MainWindow._apply_machine_origin(self.window(interface, directory), origin)
            self.assertEqual(interface.reads, [opt.TIMING_PV] * 5)
            self.assertEqual(interface.writes, [])

    def test_reset_ignores_surplus_snapshot_and_logs_actual_restored_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            interface = Interface({opt.TIMING_PV: 1131.57, "CM8L:phaseWrite": 36.5, "QA1L:currentWrite": 2.})
            window = self.window(interface, directory)
            origin = dict(config=dict(timing=True, restore_pvs=opt.RESTORE_PVS),
                          initial_setpoints={opt.TIMING_PV: 1120.37, "CM8L:phaseWrite": 0., "QA1L:currentWrite": 0.})
            restored = opt.MainWindow._apply_machine_origin(window, origin)
            self.assertEqual(restored, [opt.TIMING_PV])
            self.assertEqual(interface.writes, [{opt.TIMING_PV: 1120.37}])
            self.assertEqual(set(interface.reads), {opt.TIMING_PV})
            self.assertEqual(interface.values["CM8L:phaseWrite"], 36.5)
            self.assertEqual(interface.values["QA1L:currentWrite"], 2.)
            records = [json.loads(s) for s in (Path(directory) / "ResetInitial.jsonl").read_text().splitlines()]
            self.assertEqual([r["kind"] for r in records], ["reset_requested", "setpoint_restored", "reset_completed"])
            self.assertEqual(records[1]["before"], 1131.57)
            self.assertEqual(records[1]["target"], 1120.37)
            self.assertEqual(records[-1]["restored_parameters"], [opt.TIMING_PV])
            self.assertTrue(all(r["timestamp"] for r in records))
            self.assertEqual(len({r["reset_id"] for r in records}), 1)

    def test_missing_invalid_or_unscoped_snapshot_writes_nothing(self):
        for cfg, initial in (({}, {opt.TIMING_PV: 1.}), ({"timing": True}, {}),
                             ({"timing": True, "qa": True}, {opt.TIMING_PV: 1.}),
                             ({"timing": True}, {opt.TIMING_PV: np.nan})):
            interface = Interface({opt.TIMING_PV: 2.})
            with self.assertRaises(ValueError):
                opt.MainWindow._apply_machine_origin(self.window(interface, "/unused"),
                                                     dict(config=cfg, initial_setpoints=initial))
            self.assertEqual(interface.writes, [])

    def test_partial_failure_records_completed_and_failed_pvs_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            pvs = ["CM1L:phaseWrite", "CM2L:phaseWrite", "CM3L:phaseWrite"]
            interface = Interface(dict.fromkeys(pvs, 10.))
            real_write = interface.pv_put_many
            def write(values):
                if pvs[1] in values:
                    raise RuntimeError("Disconnected")
                real_write(values)
            interface.pv_put_many = write
            origin = dict(config=dict(kly_phase=True, kly_group_1_members=[1, 2, 3], grp_L5_8=False),
                          initial_setpoints=dict.fromkeys(pvs, 3.))
            with self.assertRaisesRegex(RuntimeError, "Disconnected"):
                opt.MainWindow._apply_machine_origin(self.window(interface, directory), origin)
            self.assertEqual(interface.writes, [{pvs[0]: 3.}])
            records = [json.loads(s) for s in (Path(directory) / "ResetInitial.jsonl").read_text().splitlines()]
            self.assertEqual(records[-1]["kind"], "reset_failed")
            self.assertEqual(records[-1]["pv"], pvs[1])
            self.assertEqual(records[-1]["restored_parameters"], pvs[:1])

    def test_log_open_failure_prevents_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            interface = Interface({opt.TIMING_PV: 2.})
            # Opening a directory as the journal fails before sending a command.
            origin = dict(config=dict(timing=True), initial_setpoints={opt.TIMING_PV: 1.}, reset_log_path=directory)
            with self.assertRaises(OSError):
                opt.MainWindow._apply_machine_origin(self.window(interface, directory), origin)
            self.assertEqual(interface.writes, [])

    def test_unconfirmed_write_is_logged_as_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            interface = Interface({opt.TIMING_PV: 2.})
            interface.pv_put_many = Mock()  # Simulate a silently dropped PUT.
            origin = dict(config=dict(timing=True), initial_setpoints={opt.TIMING_PV: 1.})
            with patch.object(opt.time, "monotonic", side_effect=[0., 3.]):
                with self.assertRaisesRegex(RuntimeError, "could not be confirmed"):
                    opt.MainWindow._apply_machine_origin(self.window(interface, directory), origin)
            records = [json.loads(s) for s in (Path(directory) / "ResetInitial.jsonl").read_text().splitlines()]
            self.assertEqual([r["kind"] for r in records], ["reset_requested", "reset_failed"])
            self.assertEqual(records[-1]["restored_parameters"], [])

    def test_worker_snapshot_uses_same_selected_scope_and_exact_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            pv = "CM2L:phaseWrite"
            interface = Interface({pv: 12.345})
            cfg = dict(kly_phase=True, kly_group_1_members=[2], grp_L5_8=False,
                       restore_pvs=opt.RESTORE_PVS)
            worker = opt.OptimizationWorker(cfg, directory, interface=interface)
            worker._interface_injected = False  # Exercise the production read path with fake I/O.
            worker._measure_objective = Mock(return_value=({}, np.nan, .9, "DR", .9, True, ""))
            worker._dump_initial_snapshot()
            snapshot = json.loads((worker.run_dir / f"InitialSnapshot_{worker.run_tag}.json").read_text())
            self.assertEqual(snapshot["initial_setpoints"], {pv: 12.345})
            self.assertEqual(interface.reads, [pv])
            self.assertEqual(interface.writes, [])


if __name__ == "__main__":
    unittest.main()
