"""Offline feedback control, EPICS protocol and GUI interlock regressions."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from Knobs.linac_feedback import (BeamLoss, OrbitDrift, FeedbackConfig, FeedbackController, RecentResponse,
                                 CURRENT_PVS, PVS, TIMING_PV, PHASE_PV)
from Knobs.linac_feedback_io import FeedbackMachine


class Plant:
    def __init__(self):
        self.values = dict.fromkeys(PVS, 0.)
        self.values[TIMING_PV] = 100.37
        self.target_timing, self.target_phase = 105.97, 0.
        self.reference = np.array([.5, -.2, .7, -.4])
        self.drift = np.zeros(4)
        # Column order: H1, V1, H2, V2. Include cross-plane coupling.
        self.response = np.array([[2., .1, .3, 0.], [.3, 0., 1.5, .1],
                                  [.1, 1.8, 0., .2], [0., .4, .1, 1.6]])
        self.writes = []
        self.loss = 0.
        self.stopped = False
        self.on_write = lambda *_: None
        self.orbit_callback = lambda value: None

    def set_orbit_monitor(self, callback):
        self.orbit_callback = callback

    def read(self, pv):
        return self.values[pv]

    def write(self, values, restore=False):
        if not restore:
            self.checkpoint()
        self.writes.append((dict(values), restore))
        self.values.update(values)
        self.on_write(values, restore)

    def checkpoint(self):
        if self.stopped:
            raise InterruptedError("Stop")

    def orbit(self):
        self.checkpoint()
        return self.reference + self.drift + self.response @ np.array([self.values[pv] for pv in CURRENT_PVS])

    def transmission(self):
        self.checkpoint()
        timing = (self.values[TIMING_PV] - self.target_timing) / 2.8
        phase = (self.values[PHASE_PV] - self.target_phase) / .5
        residual = self.orbit() - self.reference
        self.orbit_callback(self.orbit())
        return max(0., .95 - .001 * timing ** 2 - .003 * phase ** 2 - .1 * np.dot(residual, residual) - self.loss)

    def wait(self, seconds):
        self.checkpoint()


class FeedbackTests(unittest.TestCase):
    def controller(self, plant=None, **options):
        plant = plant or Plant()
        events = []
        config = replace(FeedbackConfig(settle_sec=0, interval_sec=0), **options)
        limits = {pv: (-10000., 10000.) for pv in PVS}
        limits.update({pv: (-.5, .5) for pv in CURRENT_PVS})
        controller = FeedbackController(plant, config, limits, events.append)
        controller.initialize()
        return plant, controller, events

    def test_timing_tracks_moving_local_optimum_on_relative_grid(self):
        plant, controller, events = self.controller()
        controller.cycle()
        self.assertAlmostEqual(plant.values[TIMING_PV], plant.target_timing)
        plant.target_timing += 5.6
        controller.cycle()
        self.assertAlmostEqual(plant.values[TIMING_PV], plant.target_timing)
        plant.target_timing -= 8.4
        controller.cycle()
        self.assertAlmostEqual(plant.values[TIMING_PV], plant.target_timing)
        for values, _ in plant.writes:
            self.assertEqual(set(values), {TIMING_PV})
            self.assertAlmostEqual((values[TIMING_PV] - 100.37) / 2.8,
                                   round((values[TIMING_PV] - 100.37) / 2.8))

    def test_decline_then_orbit_then_timing_then_phase_then_timing(self):
        plant, controller, events = self.controller()
        controller.cycle()
        reference = controller.reference.copy()
        plant.drift = np.array([.26, -.17, .24, -.22])
        plant.target_phase = 2.5
        start = len(events)
        controller.cycle()
        states = [e["state"] for e in events[start:] if e["kind"] == "state"]
        self.assertEqual(states, ["Checking orbit", "Learning corrector response", "Correcting orbit",
                                  "Checking orbit", "Timing after orbit correction",
                                  "Checking transmission after timing", "CM8L phase recovery", "Monitoring"])
        self.assertLess(np.max(abs(plant.orbit() - reference)), .02)
        self.assertAlmostEqual(plant.values[PHASE_PV], 2.5)
        np.testing.assert_array_equal(controller.reference, reference)
        # The next cycle always begins with timing, including after CM8L recovery.
        start = len(events)
        controller.cycle()
        self.assertEqual([e["state"] for e in events[start:] if e["kind"] == "state"][:2], ["Checking orbit", "Timing tracking"])
        previous = dict.fromkeys(CURRENT_PVS, 0.)
        for values, _ in plant.writes:
            for pv, value in values.items():
                if pv in CURRENT_PVS:
                    # Four initial calibration excursions and their undo may
                    # move 0.05 A. All accepted correction steps remain 0.01 A.
                    self.assertLessEqual(abs(value - previous[pv]), .0500000001)
                    previous[pv] = value
        accepted_current = dict.fromkeys(CURRENT_PVS, 0.)
        for event in events:
            if event["kind"] == "orbit_step" and event["accepted"]:
                for pv, value in event["settings"].items():
                    self.assertLessEqual(abs(value - accepted_current[pv]), .0100000001)
                    accepted_current[pv] = value
        self.assertTrue(any(e["kind"] == "response" and e["observations"] > 4 for e in events))

    def test_orbit_threshold_triggers_independently_of_transmission_decline(self):
        for drift, loss, threshold, expected in ((.099, .01, 100, False), (.1, .01, 100, True),
                                                (.12, .01, 150, False), (.12, -.1, 100, True)):
            with self.subTest(drift=drift, loss=loss, threshold=threshold):
                plant, controller, events = self.controller(orbit_threshold_um=threshold)
                controller.cycle()
                plant.drift[0], plant.loss = drift, loss
                # Keep transmission in its physical range in the improving case.
                if loss < 0:
                    plant.loss = -.002
                controller.cycle()
                self.assertEqual(any(e.get("state") == "Learning corrector response" for e in events), expected)

    def test_phase_only_when_below_fixed_initial_transmission(self):
        plant, controller, events = self.controller()
        controller.cycle()
        initial = controller.initial_transmission
        plant.target_phase = -2.5
        controller.cycle()
        self.assertAlmostEqual(plant.values[PHASE_PV], -2.5)
        self.assertEqual(controller.initial_transmission, initial)
        self.assertFalse(any(e.get("state") == "Correcting orbit" for e in events))
        for values, _ in plant.writes:
            if PHASE_PV in values:
                self.assertAlmostEqual(values[PHASE_PV] / .5, round(values[PHASE_PV] / .5))

    def test_stop_and_failure_restore_temporary_timing_phase_and_current(self):
        for pv in (TIMING_PV, PHASE_PV, CURRENT_PVS[0]):
            for stop in (True, False):
                with self.subTest(pv=pv, stop=stop):
                    plant, controller, _ = self.controller()
                    original = dict(plant.values)
                    def fail(values, restore):
                        if pv in values and not restore:
                            if stop:
                                plant.stopped = True
                            else:
                                raise ValueError("Read failure after write")
                    plant.on_write = fail
                    with self.assertRaises((InterruptedError, ValueError)):
                        if pv in CURRENT_PVS:
                            controller.learn_response()
                        else:
                            controller.search(pv, 2.8 if pv == TIMING_PV else .5, "test")
                    self.assertEqual(plant.values, original)
                    self.assertTrue(plant.writes[-1][1])

    def test_bad_correction_is_rolled_back_but_updates_model(self):
        plant, controller, events = self.controller()
        controller.learn_response()
        samples = len(controller.response.samples)
        # Wrong model after an abrupt machine change; trial must be rejected.
        plant.response *= -1
        plant.drift = np.array([.25, .2, .3, .2])
        controller.learn_response = lambda: None
        controller.correct_orbit()
        np.testing.assert_array_equal(controller.currents(), np.zeros(4))
        self.assertGreater(len(controller.response.samples), samples)
        self.assertTrue(any(e["kind"] == "orbit_step" and not e["accepted"] for e in events))

    def test_range_boundary_stops_without_claiming_unbounded_optimum(self):
        plant, controller, _ = self.controller(timing_range_ns=2.8)
        plant.target_timing = 1000.
        # Avoid the zero-transmission plateau for this boundary test.
        plant.transmission = lambda: .5 + (plant.values[TIMING_PV] - 100.37) / 100.
        with self.assertRaisesRegex(RuntimeError, "range boundary"):
            controller.cycle()
        self.assertAlmostEqual(plant.values[TIMING_PV], 103.17)

    def test_recent_response_learns_new_response_and_requires_excitation(self):
        model = RecentResponse()
        dx = np.array([.01, 0., 0., 0.])
        for _ in range(5):
            model.add(dx, 2 * dx)
        self.assertIsNone(model.matrix())
        for _ in range(20):
            for dx in np.eye(4) * .01:
                model.add(dx, 3 * dx)
        np.testing.assert_allclose(model.matrix(), np.eye(4) * 3, atol=1e-4)
        for _ in range(20):
            for dx in np.eye(4) * .01:
                model.add(dx, 5 * dx)
        np.testing.assert_allclose(model.matrix(), np.eye(4) * 5, atol=1e-4)
        self.assertEqual(len(model.samples), 64)

    def test_initial_response_uses_005_then_refresh_uses_001(self):
        plant, controller, _ = self.controller()
        controller.learn_response()
        for values, restore in plant.writes:
            self.assertAlmostEqual(abs(next(iter(values.values()))), 0. if restore else .05)
        plant.writes.clear()
        controller.learn_response()
        for values, restore in plant.writes:
            self.assertAlmostEqual(abs(next(iter(values.values()))), 0. if restore else .01)

    def test_initial_probe_respects_tight_limits(self):
        plant, controller, _ = self.controller(current_range_A=.02)
        controller.learn_response()
        self.assertTrue(all(abs(v) <= .020000001 for values, _ in plant.writes for v in values.values()))

    def test_phase_recovery_uses_five_point_mean_and_absolute_five_point_drop(self):
        for samples, degraded in (([.95] * 4 + [.75], False), ([.90] * 5, False), ([.899] * 5, True)):
            plant = Plant()
            plant.target_timing = plant.values[TIMING_PV]
            plant, controller, events = self.controller(plant)
            original = plant.transmission
            readings = iter(samples)
            def measurement():
                return next(readings) if controller.state == "Checking transmission after timing" else original()
            plant.transmission = measurement
            controller.cycle()
            decision = next(e for e in events if e["kind"] == "degradation_decision")
            self.assertEqual(decision["samples"], samples)
            self.assertAlmostEqual(decision["threshold"], .95 - .05)
            self.assertEqual(decision["degraded"], degraded)
            self.assertEqual(any(e.get("state") == "CM8L phase recovery" for e in events), degraded)

    def test_sustained_improvement_promotes_floor_and_preserves_initial_references(self):
        plant = Plant()
        plant.transmission = lambda: .8
        _, controller, events = self.controller(plant)
        origin, orbit = dict(controller.origin), controller.reference.copy()
        for now, average in ((0., .90), (60., .89), (120., .91)):
            with patch("Knobs.linac_feedback.time.monotonic", return_value=now):
                controller.update_transmission_baseline(average)
            self.assertAlmostEqual(controller.transmission_baseline, .89 if now == 120. else .8)
        self.assertAlmostEqual(controller.recovery_threshold(), .84)
        self.assertEqual(controller.initial_transmission, .8)
        self.assertEqual(controller.origin, origin)
        np.testing.assert_array_equal(controller.reference, orbit)
        event = next(e for e in events if e["kind"] == "baseline_updated")
        self.assertEqual((event["previous"], event["confirmations"], event["sustained_sec"]), (.8, 3, 120.))
        controller.update_transmission_baseline(.7)
        self.assertEqual(controller.transmission_baseline, .89)

    def test_baseline_requires_time_and_confirmations_and_restarts_after_a_dip(self):
        plant = Plant()
        plant.transmission = lambda: .8
        _, controller, events = self.controller(plant)
        for now, value in ((0., .9), (60., .8), (120., .91), (240., .90)):
            with patch("Knobs.linac_feedback.time.monotonic", return_value=now):
                controller.update_transmission_baseline(value)
        self.assertEqual(controller.transmission_baseline, .8)  # only two new confirmations
        with patch("Knobs.linac_feedback.time.monotonic", return_value=241.):
            controller.update_transmission_baseline(.92)
        self.assertEqual(controller.transmission_baseline, .90)
        self.assertTrue(any(e["kind"] == "baseline_pending_reset" for e in events))

    def test_phase_recovery_uses_promoted_baseline(self):
        plant = Plant()
        plant.transmission = lambda: .8
        _, controller, events = self.controller(plant)
        for now in (0., 60., 120.):
            with patch("Knobs.linac_feedback.time.monotonic", return_value=now):
                controller.update_transmission_baseline(.95)
        plant.transmission = lambda: .89
        controller.cycle()
        decision = next(e for e in events if e["kind"] == "degradation_decision")
        self.assertTrue(decision["degraded"])
        self.assertAlmostEqual(decision["threshold"], .90)
        self.assertTrue(any(e.get("state") == "CM8L phase recovery" for e in events))
        self.assertIsNone(controller._baseline_candidate)

    def test_orbit_recovery_cancels_pending_baseline(self):
        plant, controller, events = self.controller()
        controller.update_transmission_baseline(.99)
        plant.drift[0] = .2
        controller.cycle()
        self.assertTrue(any(e["kind"] == "baseline_pending_reset" and
                            e["reason"] == "Orbit recovery required" for e in events))

    def test_baseline_threshold_is_clamped_at_zero(self):
        plant = Plant()
        plant.transmission = lambda: .03
        _, controller, _ = self.controller(plant)
        self.assertEqual(controller.recovery_threshold(), 0.)

    def test_phase_keeps_response_and_refresh_replaces_stale_observations(self):
        plant, controller, events = self.controller()
        controller.learn_response()
        previous = controller.response.snapshot()
        plant.target_phase = .5
        controller.search(PHASE_PV, .5, "Phase")
        self.assertEqual(controller.response.snapshot(), previous)
        self.assertTrue(controller.response_needs_refresh)
        plant.response *= -1
        plant.writes.clear()
        controller.learn_response()
        self.assertFalse(controller.response_needs_refresh)
        self.assertEqual(len(controller.response.samples), 4)
        np.testing.assert_allclose(controller.response.matrix(), plant.response, atol=1e-4)
        for values, restore in plant.writes:
            self.assertAlmostEqual(abs(next(iter(values.values()))), 0. if restore else .01)

    def test_interrupted_response_refresh_retains_previous_model(self):
        plant, controller, _ = self.controller()
        controller.learn_response()
        previous = controller.response.snapshot()
        controller.response_needs_refresh = True
        def fail(values, restore):
            if not restore:
                raise BeamLoss("ICT = 0")
        plant.on_write = fail
        with self.assertRaises(BeamLoss):
            controller.learn_response()
        self.assertEqual(controller.response.snapshot(), previous)
        self.assertTrue(controller.response_needs_refresh)
        np.testing.assert_array_equal(controller.currents(), np.zeros(4))

    def test_response_cache_roundtrip_and_validation(self):
        _, controller, _ = self.controller()
        controller.learn_response()
        data = json.loads(json.dumps(controller.response.snapshot()))
        loaded = RecentResponse.from_snapshot(data)
        np.testing.assert_array_equal(loaded.matrix(), controller.response.matrix())
        for changes in (dict(units="um/A"), dict(samples=[]), dict(correctors=list(reversed(CURRENT_PVS))),
                        dict(samples=[[[.01, 0, 0, 0], [1, 2, 3, 4]]] * 4)):
            with self.assertRaises(ValueError):
                RecentResponse.from_snapshot(dict(data, **changes))

    def test_probe_losses_do_not_pollute_degradation_average(self):
        plant = Plant()
        plant.target_timing = plant.values[TIMING_PV]
        plant.transmission = lambda: .95 if abs(plant.values[TIMING_PV] - 100.37) < .001 else .5
        _, controller, events = self.controller(plant)
        controller.cycle()
        decision = next(e for e in events if e["kind"] == "degradation_decision")
        self.assertEqual(decision["samples"], [.95] * 5)
        self.assertFalse(decision["degraded"])

    def test_orbit_drift_during_timing_interrupts_and_restores_probe(self):
        plant, controller, events = self.controller()
        injected = []
        def drift(values, restore):
            if TIMING_PV in values and not restore and not injected:
                injected.append(True)
                plant.drift[0] = .25
        plant.on_write = drift
        controller.cycle()
        self.assertTrue(any(e["kind"] == "orbit_priority" for e in events))
        # The temporary timing offset was removed before any current excitation.
        first_current = next(i for i, (values, _) in enumerate(plant.writes) if any(p in CURRENT_PVS for p in values))
        self.assertEqual(plant.writes[first_current - 1], ({TIMING_PV: 100.37}, True))
        self.assertLess(np.max(abs(plant.orbit() - controller.reference)), .1)

    def test_temporary_probe_orbit_change_is_confirmed_at_restored_setting(self):
        plant = Plant()
        plant.target_timing = plant.values[TIMING_PV]
        original = plant.orbit
        plant.orbit = lambda: original() + (np.array([.2, 0, 0, 0]) if abs(plant.values[TIMING_PV] - 100.37) > .001 else 0)
        _, controller, events = self.controller(plant)
        controller.cycle()
        self.assertFalse(any(e["kind"] == "orbit_priority" for e in events))
        self.assertTrue(all(set(values) == {TIMING_PV} for values, _ in plant.writes))

    def test_unresolved_orbit_blocks_phase_even_when_transmission_is_low(self):
        plant, controller, events = self.controller()
        plant.response[:] = 0
        plant.drift[0], plant.loss = .3, .1
        controller.cycle()
        self.assertTrue(any(e["kind"] == "phase_blocked" for e in events))
        self.assertTrue(any(e.get("state") == "Timing after orbit correction" for e in events))
        self.assertFalse(any(PHASE_PV in values for values, _ in plant.writes))

    def test_countdown_and_orbit_drift_interrupt_wait(self):
        for drift in (False, True):
            plant, controller, events = self.controller(interval_sec=5.)
            now = [0.]
            def wait(seconds):
                now[0] += seconds
                if drift:
                    plant.drift[0] = .2
                    plant.orbit_callback(plant.orbit())
            plant.wait = wait
            with patch("Knobs.linac_feedback.time.monotonic", side_effect=lambda: now[0]):
                if drift:
                    with self.assertRaises(OrbitDrift):
                        controller.wait_cycle()
                    self.assertLess(now[0], 5.)
                else:
                    controller.wait_cycle()
                    self.assertEqual([e["remaining_sec"] for e in events if e["kind"] == "waiting"], [5, 4, 3, 2, 1, 0])

    def test_linear_intensity_drift_does_not_fake_neighbour_improvement(self):
        plant, controller, _ = self.controller()
        origin = plant.values[TIMING_PV]
        calls = 0
        def drifting_measurement():
            nonlocal calls
            calls += 1
            return .8 - .001 * ((plant.values[TIMING_PV] - origin) / 2.8) ** 2 + .002 * calls
        plant.transmission = drifting_measurement
        controller.search(TIMING_PV, 2.8, "Timing")
        self.assertEqual(plant.values[TIMING_PV], origin)


class MachineTests(unittest.TestCase):
    def machine(self, factory):
        healthy = Mock()
        healthy.get.return_value = 1.
        healthy.get_with_metadata.return_value = dict(value=1., timestamp=1.)
        def channels(name, **kwargs):
            return healthy if name.startswith("BIM:") else factory(name, **kwargs)
        machine = FeedbackMachine(FeedbackConfig(settle_sec=0, sample_timeout_sec=.01),
                                  lambda: False, lambda: None, lambda e: None, channels)
        machine.wait = lambda *args, **kw: None
        return machine

    def test_three_new_timestamps_required_and_stale_records_time_out(self):
        pv = Mock()
        stamps = iter((1, 1, 2, 2, 3, 4))
        pv.get_with_metadata.side_effect = lambda **kw: dict(timestamp=next(stamps), value=1.)
        machine = self.machine(lambda *a, **kw: pv)
        records = machine.fresh_records(("test",))
        self.assertEqual([r["test"]["timestamp"] for r in records], [2, 3, 4])
        pv.get_with_metadata.side_effect = lambda **kw: dict(timestamp=5., value=1.)
        with self.assertRaises(TimeoutError):
            machine.fresh_records(("test",))

    def test_monitor_mapping_units_status_and_exact_three_mean(self):
        from machine_interfaces_atf2.InterfaceATF2_Linac import LINAC_MONITORS
        records = []
        for i in range(3):
            raw = np.zeros((len(LINAC_MONITORS), 20))
            for name, x, y in (("ML8L", 100 + i * 100, -300), ("ML10L", 400, 500 + i * 100)):
                raw[LINAC_MONITORS.index(name), :4] = [1, x, y, 10]
            records.append({"LINAC:monitors": dict(value=raw.ravel(), timestamp=i)})
        machine = self.machine(Mock())
        machine.fresh_records = lambda names: records
        np.testing.assert_allclose(machine.orbit(), [.2, .4, -.3, .6])
        records[0]["LINAC:monitors"]["value"][LINAC_MONITORS.index("ML8L") * 20] = 0
        with self.assertRaisesRegex(ValueError, "Invalid/no-beam"):
            machine.orbit()

    def test_continuous_bpm_monitor_uses_three_distinct_records(self):
        from machine_interfaces_atf2.InterfaceATF2_Linac import LINAC_MONITORS
        raw = np.zeros((len(LINAC_MONITORS), 20))
        raw[:, 0], raw[:, 3] = 1., 1.
        records = []
        for stamp, x in ((1, 0), (2, 100), (2, 100), (3, 200), (4, 300), (4, 300)):
            raw[LINAC_MONITORS.index("ML8L"), 1] = x
            records.append(dict(value=raw.copy().ravel(), timestamp=stamp))
        pv = Mock()
        pv.get_with_metadata.side_effect = records
        machine = self.machine(lambda *a, **kw: pv)
        means = []
        machine.set_orbit_monitor(means.append)
        for _ in range(5):
            machine.check_beam(force=True)
        self.assertEqual(len(means), 2)
        np.testing.assert_allclose([m[0] for m in means], [.1, .2])
        machine._orbit_last_seen = -100.
        with self.assertRaisesRegex(TimeoutError, "stopped updating"):
            machine.poll_orbit()

    def test_transmission_is_ratio_of_three_means(self):
        machine = self.machine(Mock())
        machine.fresh_records = lambda names: [dict(zip(names, [dict(value=u, timestamp=i), dict(value=d, timestamp=i)]))
                                               for i, (u, d) in enumerate(((2, 1), (4, 3), (6, 3)))]
        self.assertAlmostEqual(machine.transmission(), 7 / 12)

    def test_unverified_limits_put_failure_and_readback_failure(self):
        pv = Mock()
        pv.get_ctrlvars.return_value = {}
        machine = self.machine(lambda *a, **kw: pv)
        with self.assertRaisesRegex(ValueError, "verify"):
            machine.limits()
        pv.put.assert_not_called()
        pv.get_ctrlvars.return_value = dict(lower_ctrl_limit=-3., upper_ctrl_limit=3.)
        machine.limits()
        pv.put.return_value = -1
        pv.get.return_value = 0.
        with self.assertRaisesRegex(RuntimeError, "PUT failed"):
            machine.write({CURRENT_PVS[0]: .01})
        pv.put.return_value = 1
        pv.get.return_value = None
        with self.assertRaisesRegex(ValueError, "Disconnected"):
            machine.write({CURRENT_PVS[0]: .01})

    def test_active_feedback_readback_tolerances_at_start_and_after_write(self):
        for name, tolerance in ((CURRENT_PVS[0], .1), (PHASE_PV, 1.), (TIMING_PV, .01)):
            for error, accepted in ((tolerance, True), (tolerance + .001, False)):
                with self.subTest(name=name, error=error):
                    channel = Mock()
                    channel.get_ctrlvars.return_value = dict(lower_ctrl_limit=-3., upper_ctrl_limit=3.)
                    channel.put.return_value = 1
                    machine = self.machine(lambda *a, **kw: channel)
                    machine.limits()
                    machine.check_beam = Mock()
                    channel.get.side_effect = [0., error]
                    if accepted:
                        self.assertEqual(machine.read(name), 0.)
                    else:
                        with self.assertRaisesRegex(ValueError, "Setpoint/readback mismatch"):
                            machine.read(name)
                    channel.get.side_effect = [0., error]
                    with patch("Knobs.linac_feedback_io.time.monotonic", side_effect=[0., 4.]):
                        if accepted:
                            machine.write({name: 0.})
                        else:
                            with self.assertRaisesRegex(RuntimeError, "readback did not reach target"):
                                machine.write({name: 0.})
    def test_logging_failure_does_not_block_probe_restoration(self):
        pv = Mock()
        pv.get_ctrlvars.return_value = dict(lower_ctrl_limit=-3., upper_ctrl_limit=3.)
        pv.put.return_value = 1
        pv.get.return_value = 0.
        machine = self.machine(lambda *a, **kw: pv)
        machine.limits()
        machine.emit = Mock(side_effect=OSError("Disk full"))
        with self.assertRaises(OSError):
            machine.write({CURRENT_PVS[0]: .01})
        pv.put.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "Restored settings"):
            machine.write({CURRENT_PVS[0]: 0.}, restore=True)
        pv.put.assert_called_once_with(0., wait=True, timeout=2.)

    def test_restore_attempts_all_currents_if_one_channel_fails(self):
        pvs = {name: Mock() for name in CURRENT_PVS}
        for pv in pvs.values():
            pv.get_ctrlvars.return_value = dict(lower_ctrl_limit=-3., upper_ctrl_limit=3.)
            pv.put.return_value = 1
        pvs[CURRENT_PVS[0]].put.return_value = -1
        machine = self.machine(lambda name, **kw: pvs[name])
        machine.limits()
        with self.assertRaisesRegex(RuntimeError, "Restoration incomplete"):
            machine.write(dict.fromkeys(CURRENT_PVS, 0.), restore=True)
        self.assertTrue(all(pv.put.call_count == 1 for pv in pvs.values()))

    def test_nonpositive_ict_is_detected_before_averaging_even_with_alarm(self):
        for value in (0., -1.):
            machine = FeedbackMachine(FeedbackConfig(), lambda: False, lambda: None, Mock(),
                                      lambda *a, **kw: Mock(get_with_metadata=Mock(return_value=
                                          dict(value=value, timestamp=1., severity=3))))
            with self.assertRaises(BeamLoss):
                machine.transmission()
            machine.emit.assert_called_once()

    def test_loss_during_orbit_acquisition_and_wait(self):
        for operation in (lambda m: m.orbit(), lambda m: m.wait(.1), lambda m: m.write({TIMING_PV: 1.})):
            machine = FeedbackMachine(FeedbackConfig(), lambda: False, lambda: None, Mock(),
                                      lambda *a, **kw: Mock(get_with_metadata=Mock(return_value=
                                          dict(value=0., timestamp=1.))))
            with self.assertRaises(BeamLoss):
                operation(machine)

    def test_command_audit_contains_before_after_and_confirmation(self):
        pv = Mock()
        pv.get_ctrlvars.return_value = dict(lower_ctrl_limit=-3., upper_ctrl_limit=3.)
        pv.get.side_effect = [0.2, 0.21]
        pv.put.return_value = 1
        machine = self.machine(lambda *a, **kw: pv)
        machine.limits()
        events = []
        machine.emit = events.append
        machine.write({CURRENT_PVS[0]: .21})
        self.assertEqual([e["kind"] for e in events], ["command", "command_result"])
        self.assertEqual(events[0]["previous"], {CURRENT_PVS[0]: .2})
        self.assertEqual(events[1]["readbacks"], {CURRENT_PVS[0]: .21})
        self.assertEqual(events[1]["status"], "confirmed")
        self.assertEqual(events[0]["command_id"], events[1]["command_id"])


_APP = None


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _APP
        from PyQt6.QtWidgets import QApplication
        _APP = QApplication.instance() or QApplication([])

    def test_tab_threshold_and_mutual_exclusion_without_live_access(self):
        from Knobs import Linac_Opt as linac
        with patch.object(linac.MainWindow, "_refresh_current_values"), patch.object(
                linac.MainWindow, "_refresh_developer_live_values"), patch("epics.PV", side_effect=AssertionError("hardware")):
            window = linac.MainWindow()
            window._current_value_timer.stop()
            try:
                panel = window.feedback_panel
                self.assertEqual(window.tabs.tabText(window.tabs.indexOf(panel)), "Active Feedback")
                self.assertEqual(panel.config().orbit_threshold_um, 100.)
                self.assertEqual(panel.config().settle_sec, 2.)
                self.assertEqual(panel.config().degradation_samples, 5)
                self.assertEqual(panel.config().degradation_percent, 5.)
                self.assertEqual(panel.config().baseline_hold_sec, 120.)
                self.assertIn("percentage points", panel.fields["degradation_percent"].toolTip())
                self.assertEqual(panel.start_button.styleSheet(), window.btn_start.styleSheet())
                self.assertEqual(panel.pause_button.styleSheet(), window.btn_stop.styleSheet())
                self.assertTrue(panel.log.isHidden())
                self.assertIn("full feedback cycle", panel.fields["interval_sec"].toolTip())
                self.assertIn("0.1 percentage point", panel.fields["min_gain"].toolTip())
                origin = {pv: 1. for pv in PVS}
                settings = dict(origin, **{TIMING_PV: 6.6, PHASE_PV: 1.5, CURRENT_PVS[0]: 1.02})
                panel.on_event(dict(kind="settings", settings=settings, initial_settings=origin))
                self.assertEqual(panel.parameter_table.item(0, 3).text(), "+5.600")
                self.assertEqual(panel.parameter_table.item(1, 3).text(), "+0.500")
                self.assertEqual(panel.parameter_table.item(2, 3).text(), "+0.020")
                panel.on_event(dict(kind="reference", transmission=.8, orbit_mm=[0.] * 4, settings=origin))
                self.assertAlmostEqual(panel.degradation_threshold, .75)
                panel.on_event(dict(kind="transmission_average", average=.89, samples=5, required=5,
                                    complete=True, threshold=.75))
                panel.on_event(dict(kind="baseline_updated", baseline=.89, threshold=.84))
                self.assertIn("Initial: 0.800000", panel.values.text())
                self.assertIn("Recovery baseline: 0.890000", panel.values.text())
                self.assertIn("Threshold: 0.840000", panel.values.text())
                self.assertNotIn("0.750000", panel.values.text())
                panel.draw_plot()
                lines = {line.get_label(): line for line in panel.axes[0].lines}
                self.assertEqual(lines["Recovery baseline"].get_ydata()[0], .89)
                self.assertEqual(lines["Initial"].get_ydata()[0], .8)
                panel.on_event(dict(kind="waiting", cycle=3, remaining_sec=4.1))
                self.assertIn("Next timing scan in 5 s", panel.status.text())
                self.assertIn("BPM monitoring active", panel.status.text())
                panel.on_event(dict(kind="activity", state="Timing tracking", cycle=3,
                                    detail="Settling after write", remaining_sec=1.2))
                self.assertIn("RUNNING", panel.status.text())
                self.assertIn("1.2 s remaining", panel.status.text())
                panel.fields["orbit_threshold_um"].setValue(150.)
                self.assertEqual(panel.config().orbit_threshold_um, 150.)
                fake = Mock()
                panel.worker = fake
                window._feedback_busy_changed(True)
                self.assertFalse(window.btn_start.isEnabled())
                self.assertFalse(window.reset_initial_btn.isEnabled())
                with patch.object(linac.QMessageBox, "warning") as warning:
                    window.start_optimization()
                    window.start_developer_optimization()
                    self.assertEqual(warning.call_count, 2)
                panel.worker = None
                window._feedback_busy_changed(False)
                window.worker = fake
                with patch.object(linac.QMessageBox, "warning") as warning:
                    panel.start()
                    warning.assert_called_once()
                window.worker = None
                self.assertTrue(window.btn_start.isEnabled())
            finally:
                panel.worker = None
                window.worker = None
                _APP.processEvents()
                window.close()
                _APP.processEvents()

    def test_pause_cancels_pending_baseline_without_lowering_confirmed_baseline(self):
        from Knobs.linac_feedback_gui import FeedbackWorker
        _, controller, events = FeedbackTests().controller()
        controller.update_transmission_baseline(.99)
        baseline = controller.transmission_baseline
        with tempfile.TemporaryDirectory() as directory:
            worker = FeedbackWorker(FeedbackConfig(), directory)
            worker.on_pause = controller.reset_baseline_candidate
            worker.paused.set()
            worker.emit_event = lambda event: worker.paused.clear()
            worker.checkpoint()
        self.assertIsNone(controller._baseline_candidate)
        self.assertEqual(controller.transmission_baseline, baseline)
        self.assertEqual(events[-1]["reason"], "Feedback paused")

    def test_beam_loss_retains_model_and_cancels_pending_baseline(self):
        plant, controller, events = FeedbackTests().controller()
        controller.learn_response()
        previous = controller.response.snapshot()
        def fail_cycle():
            controller.update_transmission_baseline(.99)
            raise BeamLoss("ICT = 0")
        controller.cycle = fail_cycle
        def pause(reason):
            self.assertEqual(controller.response.snapshot(), previous)
            self.assertTrue(controller.response_needs_refresh)
            self.assertIsNone(controller._baseline_candidate)
            raise InterruptedError("test complete")
        with self.assertRaises(InterruptedError):
            controller.run(pause_for_loss=pause)

    def test_response_is_saved_and_loaded_by_a_new_worker(self):
        from Knobs.linac_feedback_gui import FeedbackWorker
        with tempfile.TemporaryDirectory() as directory:
            for number in range(3):
                plant = Plant()
                plant.limits = lambda: {pv: (-1000., 1000.) for pv in PVS}
                plant.close = Mock()
                if number == 2:
                    (Path(directory) / "active_feedback_response.json").write_text("broken cache")
                def exercise(controller, **kwargs):
                    if number == 1:
                        np.testing.assert_allclose(controller.response.matrix(), plant.response, atol=1e-4)
                        self.assertTrue(controller.response_needs_refresh)
                    else:
                        self.assertIsNone(controller.response.matrix())
                    controller.initialize()
                    controller.learn_response()
                with patch.object(FeedbackController, "run", autospec=True, side_effect=exercise):
                    worker = FeedbackWorker(FeedbackConfig(settle_sec=0), directory, lambda *args: plant)
                    worker.run()
                events = [json.loads(line) for line in (worker.run_dir / "events.jsonl").read_text().splitlines()]
                self.assertFalse(any(e["kind"] == "error" for e in events), events)
                probe = next(e["probe_A"] for e in events if e["kind"] == "response_probe_size")
                self.assertEqual(probe, .01 if number == 1 else .05)
                saved = RecentResponse.from_snapshot(json.loads(worker.response_cache.read_text()))
                np.testing.assert_allclose(saved.matrix(), plant.response, atol=1e-4)
                plant.close.assert_called_once()

    def test_worker_journals_reference_commands_and_stop(self):
        from Knobs.linac_feedback_gui import FeedbackWorker
        plant = Plant()
        plant.limits = lambda: {pv: (-1000., 1000.) for pv in PVS}
        plant.close = Mock()
        plant.wait = lambda seconds: (_ for _ in ()).throw(InterruptedError("done"))
        with tempfile.TemporaryDirectory() as directory:
            worker = FeedbackWorker(FeedbackConfig(settle_sec=0), directory, lambda *args: plant)
            worker.run()
            events = [json.loads(line) for line in (worker.run_dir / "events.jsonl").read_text().splitlines()]
            self.assertTrue(any(e["kind"] == "reference" for e in events))
            self.assertTrue(events[-1]["state"].startswith("Stopped"))
            self.assertTrue((worker.run_dir / "config.json").exists())
            plant.close.assert_called_once()

    def test_loss_restores_then_auto_pauses_and_resume_rechecks_beam(self):
        from Knobs.linac_feedback_gui import FeedbackWorker
        for recover in (False, True):
            with self.subTest(recover=recover), tempfile.TemporaryDirectory() as directory:
                plant = Plant()
                plant.limits = lambda: {pv: (-1000., 1000.) for pv in PVS}
                plant.close = Mock()
                plant.wait = lambda seconds: (_ for _ in ()).throw(InterruptedError("done"))
                lost = [False]
                original_transmission = plant.transmission
                def transmission():
                    if lost[0]:
                        raise BeamLoss("DR ICT = 0")
                    return original_transmission()
                plant.transmission = transmission
                def write(values, restore):
                    if not restore:
                        lost[0] = True
                plant.on_write = write
                worker = FeedbackWorker(FeedbackConfig(settle_sec=0), directory, lambda *args: plant)
                first_pause, second_pause = threading.Event(), threading.Event()
                events = []
                emit = worker.emit_event
                def record(event):
                    emit(event)
                    events.append(event)
                    if event["kind"] == "auto_pause":
                        (second_pause if first_pause.is_set() else first_pause).set()
                worker.emit_event = record
                thread = threading.Thread(target=worker.run)
                thread.start()
                try:
                    self.assertTrue(first_pause.wait(3))
                    self.assertTrue(worker.paused.is_set())
                    self.assertAlmostEqual(plant.values[TIMING_PV], 100.37)
                    writes = len(plant.writes)
                    # No automatic recovery just because the beam comes back.
                    if recover:
                        lost[0] = False
                        plant.on_write = lambda *a: None
                    self.assertTrue(thread.is_alive())
                    self.assertEqual(len(plant.writes), writes)
                    worker.paused.clear()  # operator Resume
                    if recover:
                        thread.join(3)
                        self.assertFalse(thread.is_alive())
                        self.assertAlmostEqual(plant.values[TIMING_PV], plant.target_timing)
                    else:
                        self.assertTrue(second_pause.wait(3))
                        self.assertEqual(len(plant.writes), writes)
                    self.assertEqual(sum(e["kind"] == "reference" for e in events), 1)
                finally:
                    worker.stop()
                    thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertTrue((worker.run_dir / "operations.csv").exists())


if __name__ == "__main__":
    unittest.main()
