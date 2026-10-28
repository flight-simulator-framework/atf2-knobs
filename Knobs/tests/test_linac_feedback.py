"""Offline feedback control, EPICS protocol and GUI interlock regressions."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
from itertools import product
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


def seed_response(controller, plant):
    """Inject prior measured observations without issuing calibration commands."""
    for delta in np.eye(4) * .01:
        controller.response.add(delta, plant.response @ delta)


class FeedbackTests(unittest.TestCase):
    def controller(self, plant=None, **options):
        plant = plant or Plant()
        events = []
        config = replace(FeedbackConfig(settle_sec=0, interval_sec=0, min_gain=0), **options)
        limits = {pv: (-10000., 10000.) for pv in PVS}
        limits.update({pv: (-.5, .5) for pv in CURRENT_PVS})
        controller = FeedbackController(plant, config, limits, events.append)
        controller.initialize()
        return plant, controller, events

    def test_default_deadband_requires_more_than_005_percentage_point(self):
        for gain, moves in ((.00025, False), (.0005, False), (.001, True)):
            plant = Plant()
            origin = plant.values[TIMING_PV]
            target = origin + 2.8
            plant.transmission = lambda: .9 - gain * ((plant.values[TIMING_PV] - target) / 2.8) ** 2
            _, controller, _ = self.controller(plant, min_gain=FeedbackConfig().min_gain)
            controller.search(TIMING_PV, 2.8, "Timing")
            self.assertAlmostEqual(plant.values[TIMING_PV], target if moves else origin)

    def test_adjustment_selection_only_writes_enabled_targets(self):
        for timing, phase, correctors, drift in product((False, True), repeat=4):
            with self.subTest(timing=timing, phase=phase, correctors=correctors, drift=drift):
                plant, controller, events = self.controller(adjust_timing=timing, adjust_phase=phase,
                                                            adjust_correctors=correctors)
                plant.target_phase, plant.loss = 1.5, .1
                plant.drift[0] = .2 if drift else 0.
                controller.cycle()
                touched = {pv for values, _ in plant.writes for pv in values}
                self.assertEqual(TIMING_PV in touched, timing)
                self.assertEqual(PHASE_PV in touched, phase)
                self.assertEqual(bool(touched.intersection(CURRENT_PVS)), correctors and drift)
                if not correctors:
                    self.assertEqual(len(controller.response.samples), 0)
                if not any((timing, phase, correctors)):
                    self.assertEqual(plant.writes, [])
                    controller.wait_cycle()
                    self.assertEqual(next(e["next_action"] for e in events if e["kind"] == "waiting"), "Feedback cycle")
                if drift and not correctors:
                    decision = next(e for e in events if e["kind"] == "degradation_decision")
                    self.assertFalse(decision["orbit_normal"])

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

    def test_decline_then_timing_then_phase_before_orbit_then_timing(self):
        plant, controller, events = self.controller()
        controller.cycle()
        reference = controller.reference.copy()
        plant.drift = np.array([.26, -.17, .24, -.22])
        plant.target_phase = 2.5
        start = len(events)
        controller.cycle()
        states = [e["state"] for e in events[start:] if e["kind"] == "state"]
        self.assertEqual(states, ["Checking orbit", "Timing tracking",
                                  "Checking transmission after timing", "CM8L phase recovery",
                                  "Timing after phase recovery", "Correcting orbit",
                                  "Timing after orbit correction", "Checking transmission after timing", "Monitoring"])
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
                    self.assertLessEqual(abs(value - previous[pv]), .0050000001)
                    previous[pv] = value
        accepted_current = dict.fromkeys(CURRENT_PVS, 0.)
        for event in events:
            if event["kind"] == "orbit_step" and event["accepted"]:
                for pv, value in event["settings"].items():
                    self.assertLessEqual(abs(value - accepted_current[pv]), .0050000001)
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
                self.assertEqual(any(e.get("state") == "Correcting orbit" for e in events), expected)

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
                            plant.drift[0] = .2
                            controller.correct_orbit()
                        else:
                            controller.search(pv, 2.8 if pv == TIMING_PV else .5, "test")
                    self.assertEqual(plant.values, original)
                    self.assertTrue(plant.writes[-1][1])

    def test_bad_correction_is_rolled_back_but_updates_model(self):
        plant, controller, events = self.controller()
        seed_response(controller, plant)
        samples = len(controller.response.samples)
        # Wrong model after an abrupt machine change; trial must be rejected.
        plant.response *= -1
        plant.drift = np.array([.25, .2, .3, .2])
        controller.correct_orbit()
        self.assertTrue(plant.writes[1][1])
        self.assertTrue(all(value == 0. for value in plant.writes[1][0].values()))
        self.assertLess(np.linalg.norm(plant.orbit() - controller.reference), np.linalg.norm(plant.drift))
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

    def test_no_dedicated_learning_and_improving_first_correction_is_kept(self):
        plant, controller, events = self.controller(max_orbit_steps=1)
        plant.drift[0] = -.2  # First +H1 correction improves the orbit.
        controller.correct_orbit()
        self.assertEqual(plant.writes, [({CURRENT_PVS[0]: .005}, False)])
        self.assertEqual(len(controller.response.samples), 1)
        self.assertEqual([e["state"] for e in events if e["kind"] == "state"][-1], "Correcting orbit")
        self.assertFalse(any(e["kind"] == "response_probe_size" for e in events))
        self.assertTrue(next(e for e in events if e["kind"] == "orbit_step")["accepted"])

    def test_stored_matrix_proposes_correction_without_calibration(self):
        plant, controller, events = self.controller(max_orbit_steps=1)
        seed_response(controller, plant)
        plant.drift = np.array([.2, -.2, .2, -.2])
        controller.correct_orbit()
        steps = [e for e in events if e["kind"] == "orbit_step"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["source"], "response model")
        self.assertTrue(steps[0]["accepted"])
        self.assertEqual(len(plant.writes), 1)

    def test_direct_correction_respects_tight_limits(self):
        plant, controller, _ = self.controller(current_range_A=.02)
        plant.drift[0] = .2
        controller.correct_orbit()
        self.assertTrue(plant.writes)
        self.assertTrue(all(abs(v) <= .020000001 for values, _ in plant.writes for v in values.values()))

    def test_phase_recovery_uses_one_ten_shot_measurement_and_absolute_five_point_drop(self):
        for samples, degraded in (([.95], False), ([.90], False), ([.899], True)):
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

    def test_promoted_orbit_reference_controls_drift_detection_and_correction(self):
        plant = Plant()
        plant.transmission = lambda: .8
        _, controller, events = self.controller(plant)
        initial = controller.initial_orbit.copy()
        origin = dict(controller.origin)
        # Each promotion uses the final fresh ten-shot orbit at that operating
        # point, rather than the initial orbit or a temporary scan probe.
        for start, transmission, displacement in ((0., .9, .06), (180., .95, .18)):
            plant.transmission = lambda: transmission
            plant.drift[:] = [displacement, -displacement / 2, displacement / 3, 0.]
            previous = controller.reference.copy()
            for elapsed in (start, start + 60, start + 120):
                with patch("Knobs.linac_feedback.time.monotonic", return_value=elapsed):
                    controller.evaluate_transmission()
                if elapsed < start + 120:
                    np.testing.assert_array_equal(controller.reference, previous)
            np.testing.assert_allclose(controller.reference, plant.orbit())
            event = [e for e in events if e["kind"] == "baseline_updated"][-1]
            np.testing.assert_allclose(event["orbit_reference_mm"], controller.reference)
            np.testing.assert_array_equal(event["previous_orbit_reference_mm"], previous)
            np.testing.assert_array_equal(event["initial_orbit_mm"], initial)
        np.testing.assert_array_equal(controller.initial_orbit, initial)
        self.assertEqual(controller.origin, origin)
        self.assertEqual(controller.initial_transmission, .8)
        self.assertFalse(controller.outside_orbit_threshold(plant.orbit()))
        self.assertTrue(controller.outside_orbit_threshold(initial))
        target = controller.reference.copy()
        plant.drift[0] += .2
        controller.monitor_orbit(plant.orbit())
        self.assertTrue(controller.orbit_alert)
        seed_response(controller, plant)
        controller.correct_orbit()
        self.assertLess(np.max(abs(plant.orbit() - target)), .01)
        self.assertGreater(np.max(abs(plant.orbit() - initial)), .15)

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

    def test_orbit_recovery_retains_pending_baseline_and_promotes_after_recovery(self):
        plant = Plant()
        plant.transmission = lambda: .8
        _, controller, events = self.controller(plant)
        for now, value in ((0., .91), (60., .90)):
            with patch("Knobs.linac_feedback.time.monotonic", return_value=now):
                controller.update_transmission_baseline(value)
        candidate = dict(controller._baseline_candidate)
        correct_orbit = controller.correct_orbit
        def recover():
            self.assertEqual(controller._baseline_candidate, candidate)
            correct_orbit()
            self.assertEqual(controller._baseline_candidate, candidate)
            self.assertEqual(controller.transmission_baseline, .8)
        controller.correct_orbit = recover
        plant.transmission = lambda: .92
        plant.drift[0] = .2
        with patch("Knobs.linac_feedback.time.monotonic", return_value=120.):
            controller.cycle()
        self.assertTrue(any(e["kind"] == "orbit_recovery" for e in events))
        self.assertFalse(any(e["kind"] == "baseline_pending_reset" for e in events))
        promotion = next(e for e in events if e["kind"] == "baseline_updated")
        self.assertEqual(promotion["confirmations"], 3)
        self.assertEqual(promotion["sustained_sec"], 120.)
        self.assertAlmostEqual(controller.transmission_baseline, .90)
        self.assertAlmostEqual(controller.recovery_threshold(), .85)

    def test_orbit_drift_during_wait_retains_pending_baseline(self):
        _, controller, events = self.controller()
        controller.update_transmission_baseline(.99)
        candidate = dict(controller._baseline_candidate)
        with patch.object(controller, "initialize"), \
             patch.object(controller, "cycle", side_effect=[None, InterruptedError("test complete")]), \
             patch.object(controller, "wait_cycle", side_effect=OrbitDrift("drift during wait")):
            with self.assertRaises(InterruptedError):
                controller.run()
        self.assertEqual(controller._baseline_candidate, candidate)
        self.assertTrue(any(e["kind"] == "orbit_priority" for e in events))
        self.assertFalse(any(e["kind"] == "baseline_pending_reset" for e in events))

    def test_baseline_threshold_is_clamped_at_zero(self):
        plant = Plant()
        plant.transmission = lambda: .03
        _, controller, _ = self.controller(plant)
        self.assertEqual(controller.recovery_threshold(), 0.)

    def test_phase_keeps_response_and_learning_continues_from_corrections(self):
        plant, controller, events = self.controller()
        seed_response(controller, plant)
        previous = controller.response.snapshot()
        plant.target_phase = .5
        controller.search(PHASE_PV, .5, "Phase")
        self.assertEqual(controller.response.snapshot(), previous)
        plant.response *= -1
        plant.drift[0] = .2
        plant.writes.clear()
        controller.correct_orbit()
        self.assertGreater(len(controller.response.samples), 4)
        self.assertLess(np.linalg.norm(plant.orbit() - controller.reference), .2)
        self.assertFalse(any(e["kind"] == "response_probe_size" for e in events))

    def test_interrupted_correction_restores_current_and_retains_previous_model(self):
        plant, controller, _ = self.controller()
        seed_response(controller, plant)
        previous = controller.response.snapshot()
        plant.drift[0] = .2
        def fail(values, restore):
            if not restore:
                raise BeamLoss("ICT = 0")
        plant.on_write = fail
        with self.assertRaises(BeamLoss):
            controller.correct_orbit()
        self.assertEqual(controller.response.snapshot(), previous)
        np.testing.assert_array_equal(controller.currents(), np.zeros(4))

    def test_response_cache_roundtrip_and_validation(self):
        plant, controller, _ = self.controller()
        seed_response(controller, plant)
        data = json.loads(json.dumps(controller.response.snapshot()))
        loaded = RecentResponse.from_snapshot(data)
        np.testing.assert_array_equal(loaded.matrix(), controller.response.matrix())
        for changes in (dict(units="um/A"), dict(samples=[]), dict(correctors=list(reversed(CURRENT_PVS))),
                        dict(samples=[[[.01, 0, 0], [1, 2, 3, 4]]])):
            with self.assertRaises(ValueError):
                RecentResponse.from_snapshot(dict(data, **changes))

    def test_probe_losses_do_not_pollute_degradation_average(self):
        plant = Plant()
        plant.target_timing = plant.values[TIMING_PV]
        plant.transmission = lambda: .95 if abs(plant.values[TIMING_PV] - 100.37) < .001 else .5
        _, controller, events = self.controller(plant)
        controller.cycle()
        decision = next(e for e in events if e["kind"] == "degradation_decision")
        self.assertEqual(decision["samples"], [.95])
        self.assertFalse(decision["degraded"])

    def test_orbit_drift_during_timing_defers_correction_until_scan_finishes(self):
        plant, controller, events = self.controller()
        injected = []
        def drift(values, restore):
            if TIMING_PV in values and not restore and not injected:
                injected.append(True)
                plant.drift[0] = .25
        plant.on_write = drift
        controller.cycle()
        self.assertTrue(any(e["kind"] == "orbit_recovery" for e in events))
        # The temporary timing offset was removed before any current excitation.
        first_current = next(i for i, (values, _) in enumerate(plant.writes) if any(p in CURRENT_PVS for p in values))
        self.assertEqual(plant.writes[first_current - 1], ({TIMING_PV: plant.target_timing}, True))
        decision = next(i for i, e in enumerate(events) if e["kind"] == "degradation_decision")
        correction = next(i for i, e in enumerate(events) if e["kind"] == "orbit_recovery")
        self.assertLess(decision, correction)
        self.assertLess(np.max(abs(plant.orbit() - controller.reference)), .1)

    def test_temporary_probe_orbit_change_is_confirmed_at_restored_setting(self):
        plant = Plant()
        plant.target_timing = plant.values[TIMING_PV]
        original = plant.orbit
        plant.orbit = lambda: original() + (np.array([.2, 0, 0, 0]) if abs(plant.values[TIMING_PV] - 100.37) > .001 else 0)
        _, controller, events = self.controller(plant)
        controller.cycle()
        self.assertFalse(any(e["kind"] == "orbit_recovery" for e in events))
        self.assertTrue(all(set(values) == {TIMING_PV} for values, _ in plant.writes))

    def test_unresolved_orbit_does_not_block_phase_when_transmission_is_low(self):
        plant, controller, events = self.controller()
        plant.response[:] = 0
        plant.drift[0], plant.loss = .3, .1
        plant.target_phase = 1.5
        controller.cycle()
        self.assertFalse(any(e["kind"] == "phase_blocked" for e in events))
        self.assertTrue(any(e.get("state") == "Timing after orbit correction" for e in events))
        self.assertAlmostEqual(plant.values[PHASE_PV], 1.5)
        phase = next(i for i, (values, _) in enumerate(plant.writes) if PHASE_PV in values)
        corrector = next(i for i, (values, _) in enumerate(plant.writes) if any(pv in CURRENT_PVS for pv in values))
        self.assertLess(phase, corrector)
        self.assertTrue(controller.outside_orbit_threshold(plant.orbit()))

    def test_phase_recovery_rechecks_orbit_before_corrector_writes(self):
        plant, controller, events = self.controller()
        original = plant.orbit
        plant.target_phase = 2.5
        plant.orbit = lambda: original() + (np.array([.2, 0., 0., 0.])
                                            if abs(plant.values[PHASE_PV] - 2.5) > .01 else 0.)
        controller.cycle()
        self.assertAlmostEqual(plant.values[PHASE_PV], 2.5)
        self.assertTrue(any(e["kind"] == "phase_priority" for e in events))
        self.assertFalse(any(any(pv in CURRENT_PVS for pv in values) for values, _ in plant.writes))

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

    def test_ten_new_timestamps_required_and_stale_records_time_out(self):
        pv = Mock()
        stamps = iter([1, 1, 2, 2] + list(range(3, 12)))
        pv.get_with_metadata.side_effect = lambda **kw: dict(timestamp=next(stamps), value=1.)
        machine = self.machine(lambda *a, **kw: pv)
        records = machine.fresh_records(("test",))
        self.assertEqual([r["test"]["timestamp"] for r in records], list(range(2, 12)))
        pv.get_with_metadata.side_effect = lambda **kw: dict(timestamp=5., value=1.)
        with self.assertRaises(TimeoutError):
            machine.fresh_records(("test",))

    def test_monitor_mapping_units_status_and_exact_ten_shot_mean(self):
        from machine_interfaces_atf2.InterfaceATF2_Linac import LINAC_MONITORS
        records = []
        for i in range(10):
            raw = np.zeros((len(LINAC_MONITORS), 20))
            for name, x, y in (("ML8L", 100 + i * 100, -300), ("ML10L", 400, 500 + i * 100)):
                raw[LINAC_MONITORS.index(name), :4] = [1, x, y, 10]
            records.append({"LINAC:monitors": dict(value=raw.ravel(), timestamp=i)})
        machine = self.machine(Mock())
        machine.fresh_records = lambda names: records
        np.testing.assert_allclose(machine.orbit(), [.55, .4, -.3, .95])
        records[0]["LINAC:monitors"]["value"][LINAC_MONITORS.index("ML8L") * 20] = 0
        with self.assertRaisesRegex(ValueError, "Invalid/no-beam"):
            machine.orbit()

    def test_continuous_bpm_monitor_uses_latest_ten_distinct_records(self):
        from machine_interfaces_atf2.InterfaceATF2_Linac import LINAC_MONITORS
        raw = np.zeros((len(LINAC_MONITORS), 20))
        raw[:, 0], raw[:, 3] = 1., 1.
        records = []
        for stamp, x in ([(i, (i - 1) * 100) for i in range(1, 11)] + [(10, 900), (11, 1000), (11, 1000)]):
            raw[LINAC_MONITORS.index("ML8L"), 1] = x
            records.append(dict(value=raw.copy().ravel(), timestamp=stamp))
        pv = Mock()
        pv.get_with_metadata.side_effect = records
        machine = self.machine(lambda *a, **kw: pv)
        means = []
        machine.set_orbit_monitor(means.append)
        for _ in range(12):
            machine.check_beam(force=True)
        self.assertEqual(len(means), 2)
        np.testing.assert_allclose([m[0] for m in means], [.45, .55])
        machine._orbit_last_seen = -100.
        with self.assertRaisesRegex(TimeoutError, "stopped updating"):
            machine.poll_orbit()

    def test_transmission_is_ratio_of_ten_shot_means(self):
        machine = self.machine(Mock())
        machine.fresh_records = lambda names: [dict(zip(names, [dict(value=u, timestamp=i), dict(value=d, timestamp=i)]))
                                               for i, (u, d) in enumerate((2 * n, n + 1) for n in range(1, 11))]
        self.assertAlmostEqual(machine.transmission(), 65 / 110)

    def test_live_transmission_uses_ten_distinct_pairs_and_restarts_after_loss(self):
        machine = self.machine(Mock())
        events = []
        machine.emit = events.append
        up, down = machine.ict_pvs
        for stamp in range(1, 11):
            upstream, downstream = 2 * stamp, stamp + 1
            for _ in range(2):  # Cached rereads must not count as new shots.
                machine.monitor_transmission(up, dict(value=upstream, timestamp=stamp))
                machine.monitor_transmission(down, dict(value=downstream, timestamp=stamp))
            if stamp < 10:
                self.assertEqual(events, [])
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[-1]["transmission"], 65 / 110)
        machine.monitor_transmission(up, dict(value=22, timestamp=11))
        self.assertEqual(len(events), 1)
        machine.monitor_transmission(down, dict(value=12, timestamp=11))
        self.assertAlmostEqual(events[-1]["transmission"], 75 / 130)
        machine.monitor_transmission(down, dict(value=0, timestamp=12))
        for stamp in range(13, 22):
            machine.monitor_transmission(up, dict(value=2, timestamp=stamp))
            machine.monitor_transmission(down, dict(value=1, timestamp=stamp))
        self.assertEqual(len(events), 2)
        machine.monitor_transmission(up, dict(value=2, timestamp=22))
        machine.monitor_transmission(down, dict(value=1, timestamp=22))
        self.assertEqual(events[-1]["transmission"], .5)

    def test_live_transmission_updates_during_wait_without_control_measurements(self):
        stamps = {name: iter(range(100)) for name in ("BIM:LN0:nparticles", "BIM:DR:nparticles")}
        def factory(name, **kwargs):
            return Mock(get_with_metadata=lambda **kw: dict(timestamp=next(stamps[name]),
                                                            value=2. if "LN0" in name else 1.))
        events = []
        machine = FeedbackMachine(FeedbackConfig(), lambda: False, lambda: None, events.append, factory)
        machine.transmission = Mock(side_effect=AssertionError("Control measurement must not run"))
        machine.wait(2.2)
        live = [e for e in events if e["kind"] == "transmission_monitor"]
        self.assertGreaterEqual(len(live), 2)
        self.assertTrue(all(e["shots"] == 10 and e["transmission"] == .5 for e in live))
        machine.transmission.assert_not_called()

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
            stamps = iter(range(100))
            machine = FeedbackMachine(FeedbackConfig(), lambda: False, lambda: None, Mock(),
                                      lambda *a, **kw: Mock(get_with_metadata=Mock(side_effect=lambda **kw:
                                          dict(value=value, timestamp=next(stamps), severity=3))))
            with self.assertRaisesRegex(BeamLoss, "10 consecutive"):
                machine.transmission()
            losses = [c.args[0] for c in machine.emit.call_args_list if c.args[0]["pv"] == machine.ict_pvs[0]]
            self.assertEqual([e["consecutive"] for e in losses], list(range(1, 11)))
            self.assertEqual(losses[-1]["kind"], "beam_loss_detected")

    def test_loss_during_orbit_acquisition_and_wait(self):
        for operation in (lambda m: m.orbit(), lambda m: m.wait(.5), lambda m: m.write({TIMING_PV: 1.})):
            stamps = iter(range(100))
            machine = FeedbackMachine(FeedbackConfig(), lambda: False, lambda: None, Mock(),
                                      lambda *a, **kw: Mock(get_with_metadata=Mock(side_effect=lambda **kw:
                                          dict(value=0., timestamp=next(stamps)))))
            with self.assertRaises(BeamLoss):
                operation(machine)

    def test_ict_loss_counts_distinct_shots_per_channel_and_resets_on_recovery(self):
        machine = self.machine(Mock())
        up, down = machine.ict_pvs
        for stamp, value in ((1, 0), (1, 0), (2, -1), (3, 1), (4, 0), (4, 0), (5, 0)):
            machine.check_ict(down, value, stamp)
            machine.check_ict(up, 1., stamp)
        self.assertEqual(machine._ict_loss_counts[down], 2)
        for stamp in range(6, 13):
            machine.check_ict(down, 0., stamp)
            machine.check_ict(down, 0., stamp)  # Cached rereads do not count.
            machine.check_ict(up, 1., stamp)
        self.assertEqual(machine._ict_loss_counts[down], 9)
        with self.assertRaisesRegex(BeamLoss, "10 consecutive"):
            machine.check_ict(down, 0., 13)
        machine.check_ict(down, 1., 14)
        self.assertEqual(machine._ict_loss_counts[down], 0)

    def test_stale_bad_ict_times_out_instead_of_counting_same_shot_repeatedly(self):
        machine = FeedbackMachine(FeedbackConfig(sample_timeout_sec=.01), lambda: False, lambda: None, Mock(),
                                  lambda *a, **kw: Mock(get_with_metadata=Mock(return_value=dict(value=0., timestamp=1.))))
        with self.assertRaises(TimeoutError):
            machine.check_beam(force=True)
        self.assertEqual(list(machine._ict_loss_counts.values()), [1, 1])

    def test_transient_ict_drop_is_excluded_from_transmission_average(self):
        streams = {name: iter(range(100)) for name in ("BIM:LN0:nparticles", "BIM:DR:nparticles")}
        def factory(name, **kwargs):
            def read(**kwargs):
                stamp = next(streams[name])
                # Two bad samples during acquisition, then recovery.
                value = 0. if name == "BIM:DR:nparticles" and stamp in (3, 4) else (2. if "LN0" in name else 1.)
                return dict(value=value, timestamp=stamp)
            return Mock(get_with_metadata=read)
        events = []
        machine = FeedbackMachine(FeedbackConfig(), lambda: False, lambda: None, events.append, factory)
        self.assertEqual(machine.transmission(), .5)
        self.assertFalse(any(e["kind"] == "beam_loss_detected" for e in events))
        sample = next(e for e in events if e["kind"] == "ict_samples")
        self.assertTrue(np.all(np.array(sample["samples"]) > 0))

    def test_bpm_invalid_counts_distinct_shots_and_recovers(self):
        from machine_interfaces_atf2.InterfaceATF2_Linac import LINAC_MONITORS
        machine = self.machine(Mock())
        raw = np.zeros((len(LINAC_MONITORS), 20))
        raw[:, 0], raw[:, 3] = 1., 1.
        for stamp, valid in ((1, False), (1, False), (2, False), (3, True), (4, False), (5, False)):
            raw[LINAC_MONITORS.index("ML8L"), 0] = int(valid)
            self.assertEqual(machine.check_bpm(dict(value=raw.ravel(), timestamp=stamp)), valid)
        self.assertEqual(machine._bpm_invalid_count, 2)
        for stamp in range(6, 11):
            self.assertFalse(machine.check_bpm(dict(value=raw.ravel(), timestamp=stamp)))
        self.assertEqual(machine._bpm_invalid_count, 7)
        raw[:, 0] = 1.
        self.assertTrue(machine.check_bpm(dict(value=raw.ravel(), timestamp=11)))
        self.assertEqual(machine._bpm_invalid_count, 0)

    def test_invalid_bpm_retries_without_pause_during_acquisition_wait_and_before_write(self):
        from machine_interfaces_atf2.InterfaceATF2_Linac import LINAC_MONITORS
        for operation in (lambda m: m.orbit(), lambda m: m.wait(.5), lambda m: m.write({TIMING_PV: 1.})):
            raw = np.zeros((len(LINAC_MONITORS), 20))
            stamps = iter(range(100))
            def reading(**kwargs):
                stamp = next(stamps)
                wave = raw.copy()
                if stamp >= 5:
                    wave[:, 0], wave[:, 3] = 1., 1.
                return dict(value=wave.ravel(), timestamp=stamp)
            pv = Mock(get_with_metadata=reading)
            pv.get.return_value = 1.
            pv.put.return_value = 1
            pv.get_ctrlvars.return_value = dict(lower_ctrl_limit=-3., upper_ctrl_limit=3.)
            machine = self.machine(lambda *a, **kw: pv)
            machine.config = FeedbackConfig(settle_sec=0)
            machine.limits()
            # Use real wait to exercise the guard during cycle/settling waits.
            machine.wait = lambda seconds, **kwargs: FeedbackMachine.wait(machine, seconds, **kwargs)
            machine.set_orbit_monitor(Mock())
            events = []
            machine.emit = events.append
            operation(machine)
            invalids = [e for e in events if e["kind"] == "bpm_invalid"]
            self.assertEqual([e["consecutive"] for e in invalids], [1, 2, 3, 4, 5])
            self.assertEqual(machine._bpm_invalid_count, 0)

    def test_bpm_acquisition_requires_ten_fresh_valid_shots_after_transient_invalid(self):
        from machine_interfaces_atf2.InterfaceATF2_Linac import LINAC_MONITORS
        raw = np.zeros((len(LINAC_MONITORS), 20))
        raw[:, 0], raw[:, 3] = 1., 1.
        stamps = iter([0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
        def read(**kwargs):
            stamp = next(stamps)
            wave = raw.copy()
            wave[LINAC_MONITORS.index("ML10L"), 3] = 0. if stamp == 2 else 1.
            return dict(value=wave.ravel(), timestamp=stamp)
        machine = self.machine(lambda *a, **kw: Mock(get_with_metadata=read))
        events = []
        machine.emit = events.append
        np.testing.assert_array_equal(machine.orbit(), np.zeros(4))
        samples = next(e for e in events if e["kind"] == "orbit_samples")
        self.assertEqual(samples["timestamps"], list(range(3, 13)))

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

    def test_orbit_limits_follow_promotions_without_recentring_plot(self):
        from Knobs.linac_feedback_gui import FeedbackPanel
        panel = FeedbackPanel(lambda: "/tmp", lambda: False)
        initial = np.array([.5, -.2, .7, -.4])
        offset = np.array([.04, -.03, .02, -.01])
        try:
            panel.on_event(dict(kind="reference", transmission=.8, orbit_mm=initial.tolist(),
                                settings=dict.fromkeys(PVS, 0.), elapsed=0.))
            panel.on_event(dict(kind="orbit_monitor", orbit_mm=(initial + offset).tolist(), elapsed=10.))
            for multiplier, baseline in ((1, .9), (2, .95)):
                panel.on_event(dict(kind="baseline_updated", baseline=baseline, threshold=baseline - .05,
                                    orbit_reference_mm=(initial + multiplier * offset).tolist(), elapsed=multiplier * 120.))
                panel.on_event(dict(kind="orbit", orbit_mm=(initial + offset).tolist(), elapsed=multiplier * 120. + 1))
                panel.draw_plot()
                np.testing.assert_array_equal(panel.reference, initial)
                traces = [line for line in panel.axes[1].lines if line.get_label().startswith("ML")]
                limits = [line for line in panel.axes[1].lines if line.get_linestyle() == ":"]
                self.assertEqual(len(traces), 4)
                self.assertEqual(len(limits), 8)
                for i, trace in enumerate(traces):
                    np.testing.assert_allclose(trace.get_ydata(), offset[i] * 1000)
                    for j, sign in enumerate((-1, 1)):
                        line = limits[2 * i + j]
                        np.testing.assert_allclose(line.get_ydata(), multiplier * offset[i] * 1000 + sign * 150)
                        self.assertEqual(line.get_color(), trace.get_color())
        finally:
            _APP.processEvents()
            panel.close()

    def test_tab_threshold_and_mutual_exclusion_without_live_access(self):
        from Knobs import Linac_Opt as linac
        with patch.object(linac.MainWindow, "_refresh_current_values"), patch.object(
                linac.MainWindow, "_refresh_developer_live_values"), patch("epics.PV", side_effect=AssertionError("hardware")):
            window = linac.MainWindow()
            window._current_value_timer.stop()
            try:
                panel = window.feedback_panel
                self.assertEqual([window.tabs.tabText(i) for i in range(window.tabs.count())],
                                 ["Main", "Active Feedback", "Config"])
                self.assertEqual(window.config_sections.tabText(window.config_sections.indexOf(window.feedback_config_tab)),
                                 "Active Feedback")
                self.assertTrue(window.feedback_config_tab.isAncestorOf(panel.settings))
                self.assertFalse(panel.isAncestorOf(panel.settings))
                self.assertGreaterEqual(panel.start_button.minimumHeight(), 56)
                self.assertTrue(all(box.isChecked() for box in panel.target_checks.values()))
                panel.target_checks["adjust_phase"].setChecked(False)
                self.assertFalse(panel.config().adjust_phase)
                self.assertTrue(panel.config().adjust_timing)
                panel.target_checks["adjust_phase"].setChecked(True)
                self.assertEqual(window.tabs.tabText(window.tabs.indexOf(panel)), "Active Feedback")
                self.assertEqual(panel.config().orbit_threshold_um, 150.)
                self.assertEqual(panel.config().settle_sec, 2.)
                self.assertEqual(panel.config().interval_sec, 30.)
                self.assertEqual(panel.config().min_gain, .0005)
                self.assertEqual(panel.config().degradation_samples, 1)
                self.assertEqual(panel.config().degradation_percent, 5.)
                self.assertEqual(panel.config().baseline_hold_sec, 120.)
                self.assertIn("percentage points", panel.fields["degradation_percent"].toolTip())
                self.assertTrue(panel.start_button.styleSheet().startswith(window.btn_start.styleSheet()))
                self.assertTrue(panel.pause_button.styleSheet().startswith(window.btn_stop.styleSheet()))
                self.assertTrue(panel.log.isHidden())
                self.assertIn("full feedback cycle", panel.fields["interval_sec"].toolTip())
                self.assertIn("0.1 percentage point", panel.fields["min_gain"].toolTip())
                origin = {pv: 1. for pv in PVS}
                settings = dict(origin, **{TIMING_PV: 6.6, PHASE_PV: 1.5, CURRENT_PVS[0]: 1.02})
                panel.on_event(dict(kind="settings", settings=settings, initial_settings=origin, elapsed=10.))
                panel.draw_plot()
                self.assertAlmostEqual(panel.parameter_axes[0].lines[0].get_ydata()[-1], 5.6)
                self.assertAlmostEqual(panel.phase_axis.lines[0].get_ydata()[-1], .5)
                self.assertAlmostEqual(panel.parameter_axes[1].lines[0].get_ydata()[-1], .02)
                self.assertEqual(len(panel.parameter_axes[1].lines), 4)
                panel.on_event(dict(kind="settings", settings=origin, initial_settings=origin, elapsed=12.))
                panel.on_event(dict(kind="waiting", remaining_sec=10., cycle=2, elapsed=20.))
                panel.draw_plot()
                np.testing.assert_allclose(panel.parameter_axes[0].lines[0].get_xdata(), [10., 12., 20.])
                np.testing.assert_allclose(panel.parameter_axes[0].lines[0].get_ydata(), [5.6, 0., 0.])
                self.assertEqual(panel.parameter_axes[0].lines[0].get_drawstyle(), "steps-post")
                panel.on_event(dict(kind="reference", transmission=.8, orbit_mm=[0.] * 4, settings=origin))
                self.assertAlmostEqual(panel.degradation_threshold, .75)
                panel.on_event(dict(kind="transmission_average", average=.89, samples=5, required=5,
                                    complete=True, threshold=.75))
                panel.on_event(dict(kind="baseline_updated", baseline=.89, threshold=.84, elapsed=21.))
                self.assertIn("Initial: 0.800000", panel.values.text())
                self.assertIn("Recovery baseline: 0.890000", panel.values.text())
                self.assertIn("Threshold: 0.840000", panel.values.text())
                self.assertNotIn("0.750000", panel.values.text())
                panel.draw_plot()
                lines = {line.get_label(): line for line in panel.axes[0].lines}
                np.testing.assert_allclose(lines["Threshold"].get_xdata(), [0., 21.])
                np.testing.assert_allclose(lines["Threshold"].get_ydata(), [.75, .84])
                self.assertEqual(lines["Threshold"].get_drawstyle(), "steps-post")
                self.assertEqual(lines["Initial"].get_ydata()[0], .8)
                panel.on_event(dict(kind="baseline_updated", baseline=.91, threshold=.86, elapsed=25.))
                panel.on_event(dict(kind="waiting", cycle=3, remaining_sec=4.1, elapsed=30.))
                panel.draw_plot()
                lines = {line.get_label(): line for line in panel.axes[0].lines}
                np.testing.assert_allclose(lines["Threshold"].get_xdata(), [0., 21., 25., 30.])
                np.testing.assert_allclose(lines["Threshold"].get_ydata(), [.75, .84, .86, .86])
                self.assertEqual(lines["Initial"].get_ydata()[0], .8)
                self.assertIn("Next timing scan in 5 s", panel.status.text())
                waiting_status = panel.status.text()
                panel.on_event(dict(kind="activity", state="Timing tracking", cycle=3,
                                    detail="Settling after write", remaining_sec=1.2))
                self.assertEqual(panel.status.text(), waiting_status)
                panel.on_event(dict(kind="state", state="Timing tracking", cycle=4))
                self.assertEqual(panel.status.text(), "RUNNING | Timing scan")
                for detail in ("Reading BPM orbit: 10 / 10 fresh samples", "Reading transmission: 10 / 10 fresh samples"):
                    panel.on_event(dict(kind="activity", detail=detail))
                    self.assertEqual(panel.status.text(), "RUNNING | Timing scan")
                panel.on_event(dict(kind="transmission_monitor", transmission=.91, shots=10, elapsed=42.))
                self.assertEqual(list(panel.points), [])
                panel.on_event(dict(kind="transmission", transmission=.89, settings=origin, elapsed=43.))
                panel.on_event(dict(kind="transmission_monitor", transmission=.92, shots=10, elapsed=44.))
                self.assertEqual(panel.latest_transmission, .89)
                panel.on_event(dict(kind="transmission_average", average=.89, samples=3, required=5,
                                    complete=False, threshold=.84))
                self.assertEqual(panel.status.text(), "RUNNING | Timing scan")
                panel.draw_plot()
                self.assertEqual({line.get_label() for line in panel.axes[0].lines},
                                 {"Measured transmission", "Initial", "Threshold"})
                live_line = next(line for line in panel.axes[0].lines if line.get_label() == "Measured transmission")
                self.assertEqual(list(live_line.get_xdata()), [43.])
                self.assertEqual(list(live_line.get_ydata()), [.89])
                threshold_line = next(line for line in panel.axes[0].lines if line.get_label() == "Threshold")
                self.assertEqual(list(threshold_line.get_xdata()), [43., 44.])
                self.assertEqual(list(threshold_line.get_ydata()), [.86, .86])
                panel.on_event(dict(kind="waiting", remaining_sec=10., elapsed=45.))
                panel.on_event(dict(kind="transmission_monitor", transmission=.93, shots=10, elapsed=46.))
                self.assertEqual(panel.latest_transmission, .93)
                panel.on_event(dict(kind="state", state="Timing tracking", elapsed=47.))
                panel.on_event(dict(kind="transmission_monitor", transmission=.94, shots=10, elapsed=48.))
                self.assertEqual(list(panel.points), [(43., .89), (46., .93)])
                panel.on_event(dict(kind="transmission", transmission=.95, settings=origin, elapsed=49.))
                self.assertEqual(panel.points[-1], (49., .95))
                self.assertEqual(panel.latest_transmission, .95)
                panel.fields["orbit_threshold_um"].setValue(200.)
                self.assertEqual(panel.config().orbit_threshold_um, 200.)
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
                with patch("Knobs.linac_feedback_gui.FeedbackWorker", return_value=Mock()):
                    panel.start()
                    self.assertEqual(list(panel.threshold_history), [])
                    self.assertIsNone(panel.orbit_reference)
                    self.assertFalse(panel.settings.isEnabled())
                    self.assertFalse(panel.targets.isEnabled())
                    self.assertTrue(window.config_sections.isEnabled())
                    panel.finished()
                self.assertTrue(panel.settings.isEnabled())
                self.assertTrue(panel.targets.isEnabled())
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
        seed_response(controller, plant)
        previous = controller.response.snapshot()
        def fail_cycle():
            controller.update_transmission_baseline(.99)
            raise BeamLoss("ICT = 0")
        controller.cycle = fail_cycle
        def pause(reason):
            self.assertEqual(controller.response.snapshot(), previous)
            self.assertIsNone(controller._baseline_candidate)
            raise InterruptedError("test complete")
        with self.assertRaises(InterruptedError):
            controller.run(pause_for_loss=pause)

    def test_partial_response_is_saved_and_loaded_by_a_new_worker(self):
        from Knobs.linac_feedback_gui import FeedbackWorker
        with tempfile.TemporaryDirectory() as directory:
            saved = None
            for number in range(3):
                plant = Plant()
                plant.limits = lambda: {pv: (-1000., 1000.) for pv in PVS}
                plant.close = Mock()
                if number == 2:
                    (Path(directory) / "active_feedback_response.json").write_text("broken cache")
                def exercise(controller, **kwargs):
                    if number == 1:
                        self.assertEqual(controller.response.snapshot(), saved.snapshot())
                    else:
                        self.assertEqual(len(controller.response.samples), 0)
                    controller.initialize()
                    plant.drift[0] = -.2
                    controller.correct_orbit()
                with patch.object(FeedbackController, "run", autospec=True, side_effect=exercise):
                    worker = FeedbackWorker(FeedbackConfig(settle_sec=0, min_gain=0, max_orbit_steps=1),
                                            directory, lambda *args: plant)
                    worker.run()
                events = [json.loads(line) for line in (worker.run_dir / "events.jsonl").read_text().splitlines()]
                self.assertFalse(any(e["kind"] == "error" for e in events), events)
                self.assertFalse(any(e["kind"] == "response_probe_size" for e in events))
                self.assertEqual(plant.writes, [({CURRENT_PVS[0]: .005}, False)])
                saved = RecentResponse.from_snapshot(json.loads(worker.response_cache.read_text()))
                self.assertEqual(len(saved.samples), 2 if number == 1 else 1)
                self.assertIsNone(saved.matrix())  # Incomplete observations still survive restart.
                plant.close.assert_called_once()

    def test_worker_journals_reference_commands_and_stop(self):
        from Knobs.linac_feedback_gui import FeedbackWorker
        plant = Plant()
        plant.limits = lambda: {pv: (-1000., 1000.) for pv in PVS}
        plant.close = Mock()
        plant.wait = lambda seconds: (_ for _ in ()).throw(InterruptedError("done"))
        with tempfile.TemporaryDirectory() as directory:
            worker = FeedbackWorker(FeedbackConfig(settle_sec=0, min_gain=0), directory, lambda *args: plant)
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
                worker = FeedbackWorker(FeedbackConfig(settle_sec=0, min_gain=0), directory, lambda *args: plant)
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
