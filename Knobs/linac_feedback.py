"""Continuous local transmission tracking; no Qt or hardware dependencies.

Orbit coordinates are [ML8L.x, ML10L.x, ML8L.y, ML10L.y] in mm.
All grids are relative to the initial setpoints (fractional origins preserved).
"""
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import product
import time

import numpy as np

from Knobs.timing_pv import TIMING_PV
from Knobs.linac_power_guard import KlystronPowerLoss

PHASE_PV = "CM8L:phaseWrite"
CORRECTORS = ("ZH1L", "ZV1L", "ZH2L", "ZV2L")
CURRENT_PVS = tuple(f"{name}:currentWrite" for name in CORRECTORS)
PVS = (TIMING_PV, PHASE_PV) + CURRENT_PVS
TIMING_STEP = 2.8
PHASE_STEP = 0.5
CURRENT_STEP = 0.005
RESPONSE_TARGET_MM = 0.5
RESPONSE_INITIAL_STEP_A = 0.05
# Rows: x8, x10, y8, y10; columns: H1, V1, H2, V2.
RESPONSE_PLANE_MASK = np.array([[1, 0, 1, 0], [1, 0, 1, 0],
                                [0, 1, 0, 1], [0, 1, 0, 1]], dtype=bool)


class BeamLoss(RuntimeError):
    """Confirmed beam loss: unwind probes, then wait for the operator."""


class OrbitDrift(RuntimeError):
    """Switch to orbit recovery only after a temporary probe has been restored."""


class TargetsChanged(RuntimeError):
    """Restart dispatch after applying target selections at a safe boundary."""


@dataclass(frozen=True)
class FeedbackConfig:
    orbit_threshold_um: float = 150.
    settle_sec: float = 2.
    interval_sec: float = 30.
    sample_timeout_sec: float = 15.
    timing_range_ns: float = 112.
    phase_range_deg: float = 8.
    current_range_A: float = 0.5
    min_gain: float = 0.0005  # absolute transmission, 0.05 percentage point; GUI-adjustable
    max_search_steps: int = 100
    max_orbit_steps: int = 50
    downstream: str = "DR"
    degradation_samples: int = 1  # one control measurement = ten fresh ICT shots
    degradation_percent: float = 5.  # absolute percentage points, 5 pp = 0.05
    baseline_hold_sec: float = 120.
    baseline_min_confirmations: int = 3
    adjust_timing: bool = True
    adjust_phase: bool = True
    adjust_correctors: bool = True
    measure_response_on_start: bool = False
    ict_auto_pause: bool = True  # Twenty consecutive nonpositive shots; independent of RF power.

    def __post_init__(self):
        for name in ("orbit_threshold_um", "sample_timeout_sec", "timing_range_ns",
                     "phase_range_deg", "current_range_A"):
            if not np.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive and finite")
        for name in ("settle_sec", "interval_sec", "min_gain", "baseline_hold_sec"):
            if not np.isfinite(getattr(self, name)) or getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative and finite")
        if self.timing_range_ns < TIMING_STEP or self.phase_range_deg < PHASE_STEP or self.current_range_A < CURRENT_STEP:
            raise ValueError("Search ranges must allow at least one step")
        if self.max_search_steps < 1 or self.max_orbit_steps < 1:
            raise ValueError("Iteration limits must be positive")
        if self.downstream not in ("DR", "LNE", "BTM", "BTE"):
            raise ValueError("Select DR, LNE, BTM or BTE transmission")
        if not isinstance(self.degradation_samples, int) or not 1 <= self.degradation_samples <= 100:
            raise ValueError("Transmission average must use 1–100 measurements")
        if not np.isfinite(self.degradation_percent) or not 0 <= self.degradation_percent < 100:
            raise ValueError("Transmission drop threshold must be in [0, 100) percentage points")
        if not isinstance(self.baseline_min_confirmations, int) or self.baseline_min_confirmations < 2:
            raise ValueError("Baseline promotion requires at least two independent confirmations")


class RecentResponse:
    """Exponentially weighted fit to the last 64 measured current/orbit changes.

Only isolated corrector moves enter this fit, never timing or phase changes.
Four independent observed current directions are required before prediction.
"""
    def __init__(self):
        self.samples = deque(maxlen=64)
        self.frozen = False
        self.measured_at = None

    def add(self, delta_current, delta_orbit):
        x, y = np.asarray(delta_current, float), np.asarray(delta_orbit, float)
        if x.shape != (4,) or y.shape != (4,) or not np.isfinite(np.r_[x, y]).all():
            raise ValueError("Invalid response observation")
        if self.frozen:
            return
        if np.linalg.norm(x) > 1e-9:
            self.samples.append((x.copy(), y.copy()))
            self.measured_at = datetime.now().astimezone().isoformat(timespec="seconds")

    def matrix(self):
        if len(self.samples) < 4:
            return None
        x, y = (np.array(a) for a in zip(*self.samples))
        weights = 0.9 ** np.arange(len(x) - 1, -1, -1)
        x, y = x * np.sqrt(weights[:, None]), y * np.sqrt(weights[:, None])
        if np.linalg.matrix_rank(x, tol=1e-7) < 4:
            return None
        # Small regularization stabilizes noisy estimates without a model prior.
        matrix = np.linalg.solve(x.T @ x + np.eye(4) * 1e-10, x.T @ y).T
        return np.where(RESPONSE_PLANE_MASK, matrix, 0.)

    def snapshot(self):
        return dict(version=1, correctors=list(CURRENT_PVS),
                    coordinates=["ML8L.x", "ML10L.x", "ML8L.y", "ML10L.y"],
                    units="mm/A", frozen=self.frozen, measured_at=self.measured_at,
                    samples=[[x.tolist(), y.tolist()] for x, y in self.samples])

    @classmethod
    def from_snapshot(cls, data):
        model = cls()
        expected = model.snapshot()
        if any(data.get(key) != expected[key] for key in ("version", "correctors", "coordinates", "units")):
            raise ValueError("Incompatible response cache")
        if not isinstance(data.get("samples"), list) or not 1 <= len(data["samples"]) <= 64:
            raise ValueError("Invalid response cache observations")
        for x, y in data["samples"]:
            model.add(x, y)
        if not model.samples:
            raise ValueError("Response cache contains no current changes")
        frozen = data.get("frozen", False)
        if not isinstance(frozen, bool) or (frozen and model.matrix() is None):
            raise ValueError("Invalid fixed response cache")
        model.frozen = frozen
        # A cache save/reload is not a measurement. Older caches have no
        # acquisition time; never substitute their continually changing saved_at.
        stamp = data.get("measured_at")
        try:
            datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            stamp = None
        model.measured_at = stamp
        return model


class FeedbackController:
    """IO supplies read/write, transmission, orbit, wait and checkpoint.

write(..., restore=True) must work after cancellation to remove temporary
probes. checkpoint may pause ONLY when accepted settings are applied.
"""
    def __init__(self, io, config, limits, emit=lambda event: None):
        self.io, self.config, self.emit = io, config, emit
        self.machine_limits = dict(limits)
        self.limits = dict(limits)
        self.response = RecentResponse()
        self.cycle_number = 0
        self.state = "Idle"
        self.orbit_alert = False
        self.transmission_history = deque(maxlen=config.degradation_samples)
        self._baseline_candidate = None
        self.target_provider = None
        self.timing_at_optimum = False
        self._interleave_context = None

    def checkpoint(self):
        # Only call at accepted settings, outside a temporary probe's try/finally.
        self.io.checkpoint()
        if self.target_provider is not None:
            selection = self.target_provider()
            changes = {name: bool(selection[name]) for name in
                       ("adjust_timing", "adjust_phase", "adjust_correctors")}
            if any(getattr(self.config, name) != value for name, value in changes.items()):
                self.config = replace(self.config, **changes)
                self._interleave_context = None
                self.event("targets_changed", **changes)
                raise TargetsChanged()

    def wait_for_targets(self):
        if any((self.config.adjust_timing, self.config.adjust_phase, self.config.adjust_correctors)):
            return
        self.set_state("Waiting for adjustment targets")
        while not any((self.config.adjust_timing, self.config.adjust_phase, self.config.adjust_correctors)):
            self.checkpoint()
            # Keep timestamped BPM/ICT monitoring and the ICT loss guard alive.
            self.io.wait(.2)

    def event(self, kind, **values):
        self.emit(dict(kind=kind, state=self.state, cycle=self.cycle_number, **values))

    def set_state(self, state):
        self.state = state
        self.event("state")

    def transmission(self):
        value = float(self.io.transmission())
        if self.config.ict_auto_pause and np.isfinite(value) and value <= 0:
            raise BeamLoss(f"Transmission dropped to {value:g}")
        if not np.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("Invalid transmission measurement")
        self.event("transmission", transmission=value, settings=dict(self.current))
        return value

    def orbit(self):
        value = np.asarray(self.io.orbit(), float)
        if value.shape != (4,) or not np.isfinite(value).all():
            raise ValueError("Invalid ten-shot orbit mean")
        self.latest_orbit = value.copy()
        self.event("orbit", orbit_mm=value.tolist())
        return value

    def outside_orbit_threshold(self, value):
        return bool(np.max(abs(value - self.reference)) >= self.config.orbit_threshold_um / 1000. - 1e-12)

    def monitor_orbit(self, value):
        # Latch alerts from the continuous monitor, including during probes.
        # A fresh measurement at an accepted setting must confirm the alert.
        if self.outside_orbit_threshold(value):
            self.orbit_alert = True

    def check_orbit(self, force=False, interrupt=True):
        if force or self.orbit_alert:
            self.orbit_alert = False
            orbit = self.orbit()
            normal = not self.outside_orbit_threshold(orbit)
            if not normal and self.config.adjust_correctors and interrupt:
                raise OrbitDrift("ML8L / ML10L orbit exceeds the drift threshold")
            self.orbit_alert = False
            return normal
        return True

    def settings_event(self):
        self.event("settings", settings=dict(self.current), initial_settings=dict(self.origin),
                   delta={pv: self.current[pv] - self.origin[pv] for pv in PVS})

    def recovery_threshold(self):
        return max(0., self.transmission_baseline - self.config.degradation_percent / 100.)

    def reset_baseline_candidate(self, reason):
        if self._baseline_candidate is not None:
            self._baseline_candidate = None
            self.event("baseline_pending_reset", reason=reason)

    def update_transmission_baseline(self, average, orbit_mm=None):
        """Promote a sustained operating-point floor, never a transient maximum.

Only complete averages at timing optima with a confirmed normal orbit qualify.
The correction reference follows the orbit at promotion. Initial plot origins
and parameter origins remain fixed.
"""
        if not np.isfinite(average) or not 0 < average <= 1:
            raise ValueError("Invalid transmission baseline observation")
        if average <= self.transmission_baseline + 1e-12:
            self.reset_baseline_candidate("Improvement was not sustained")
            return
        now = time.monotonic()
        if self._baseline_candidate is None:
            self._baseline_candidate = dict(started=now, floor=average, confirmations=0)
        candidate = self._baseline_candidate
        candidate["floor"] = min(candidate["floor"], average)
        candidate["confirmations"] += 1
        duration = max(0., now - candidate["started"])
        if duration >= self.config.baseline_hold_sec and candidate["confirmations"] >= self.config.baseline_min_confirmations:
            new_reference = self.orbit() if orbit_mm is None else np.asarray(orbit_mm, float)
            if new_reference.shape != (4,) or not np.isfinite(new_reference).all():
                raise ValueError("Invalid orbit reference at baseline promotion")
            previous = self.transmission_baseline
            previous_orbit = self.reference.copy()
            self.transmission_baseline = candidate["floor"]
            self.reference = new_reference.copy()
            self.orbit_alert = False
            self.event("baseline_updated", previous=previous, baseline=self.transmission_baseline,
                       initial=self.initial_transmission, threshold=self.recovery_threshold(),
                       orbit_reference_mm=self.reference.tolist(),
                       previous_orbit_reference_mm=previous_orbit.tolist(),
                       initial_orbit_mm=self.initial_orbit.tolist(),
                       sustained_sec=duration, confirmations=candidate["confirmations"],
                       settings=dict(self.current), reason="Sustained improvement; minimum confirmed mean")
            self._baseline_candidate = None
        else:
            self.event("baseline_pending", candidate=candidate["floor"], baseline=self.transmission_baseline,
                       remaining_sec=max(0., self.config.baseline_hold_sec - duration),
                       confirmations=candidate["confirmations"], required=self.config.baseline_min_confirmations)

    def initialize(self, feedback_start=False):
        self.set_state("Capturing feedback reference" if feedback_start else "Capturing reference")
        self.checkpoint()
        self.current = {pv: float(self.io.read(pv)) for pv in PVS}
        if not np.isfinite(list(self.current.values())).all():
            raise ValueError("Invalid initial setpoints")
        origin = dict(self.current)
        limits = {}
        excursions = (self.config.timing_range_ns, self.config.phase_range_deg) + (self.config.current_range_A,) * 4
        for pv, span in zip(PVS, excursions):
            lo, hi = self.machine_limits[pv]
            lo, hi = max(lo, origin[pv] - span), min(hi, origin[pv] + span)
            if not np.isfinite([lo, hi]).all() or not lo <= origin[pv] <= hi or lo >= hi:
                raise ValueError(f"Invalid limits for {pv}")
            limits[pv] = (lo, hi)
        reference = self.orbit()
        transmission = self.transmission()
        if transmission <= 0:
            raise ValueError("Establish transmitted beam before starting feedback")
        # Publish the new reference only after both fresh measurements succeed.
        self.origin, self.limits = origin, limits
        self.reference = reference
        self.initial_orbit = reference.copy()
        self.initial_transmission = transmission
        self.transmission_baseline = self.initial_transmission
        self._baseline_candidate = None
        self.transmission_history.clear()
        self.orbit_alert = False
        self.event("reference", orbit_mm=self.reference.tolist(), transmission=self.initial_transmission,
                   settings=self.origin, limits=self.limits,
                   baseline=self.transmission_baseline, degradation_threshold=self.recovery_threshold(),
                   feedback_start=feedback_start)
        self.settings_event()
        if hasattr(self.io, "set_orbit_monitor"):
            self.io.set_orbit_monitor(self.monitor_orbit)
        if feedback_start and hasattr(self.io, "reset_monitor_batches"):
            self.io.reset_monitor_batches()

    def write(self, values, restore=False):
        for pv, value in values.items():
            enabled = (self.config.adjust_timing if pv == TIMING_PV else
                       self.config.adjust_phase if pv == PHASE_PV else self.config.adjust_correctors)
            if not enabled:
                raise ValueError(f"Feedback adjustment disabled: {pv}")
            lo, hi = self.limits[pv]
            if not np.isfinite(value) or not lo - 1e-9 <= value <= hi + 1e-9:
                raise ValueError(f"Feedback target outside range: {pv}={value}")
        self.io.write(values, restore=restore)
        self.current.update(values)
        self.settings_event()

    def timing_recovery_needed(self, transmission):
        """Check recovery only at an accepted/restored timing setting."""
        orbit_normal = self.check_orbit(force=True, interrupt=False)
        self.checkpoint()
        threshold = self.recovery_threshold()
        phase_needed = self.config.adjust_phase and transmission < threshold - 1e-12
        orbit_needed = self.config.adjust_correctors and not orbit_normal
        if phase_needed or orbit_needed:
            self.event("timing_yield", transmission=transmission, threshold=threshold,
                       orbit_normal=orbit_normal,
                       reason="Phase recovery needed" if phase_needed else "Orbit correction needed")
            return True
        return False

    def _orbit_context_key(self):
        # RMS results from different accepted RF settings are not comparable.
        return (self.cycle_number, self.current[TIMING_PV], self.current[PHASE_PV], *self.reference)

    def orbit_intervention_blocked(self):
        context = self._interleave_context
        return bool(context and context["key"] == self._orbit_context_key() and context.get("blocked"))

    def interleave_orbit(self, label, transmission=None, phase_priority=False):
        """At a completed RF trial boundary, perform at most one accepted step.

Return whether any correction was attempted, so the caller discards its old
RF comparisons. Temporary RF offsets must already be restored or accepted.
"""
        self.checkpoint()
        if not self.config.adjust_correctors:
            return False
        if (phase_priority and self.config.adjust_phase and transmission is not None
                and transmission < self.recovery_threshold() - 1e-12):
            return False
        if not self.orbit_alert and not self.outside_orbit_threshold(self.latest_orbit):
            return False
        if self.check_orbit(force=True, interrupt=False):
            self._interleave_context = None
            return False
        key = self._orbit_context_key()
        if self._interleave_context is None or self._interleave_context["key"] != key:
            self._interleave_context = dict(key=key, attempts=0, steps=0, blocked=False)
        context = self._interleave_context
        if context["blocked"]:
            return False
        if context["steps"] >= self.config.max_orbit_steps:
            context["blocked"] = True
            self.event("orbit_intervention_deferred", reason="Correction step limit at this RF setting")
            return False
        attempts = context["attempts"]
        self.event("orbit_intervention", operation=label, orbit_mm=self.latest_orbit.tolist())
        try:
            result = self.correct_orbit(max_steps=1, context=context)
            if result is False or context["attempts"] == attempts:
                context["blocked"] = True
        finally:
            self.set_state(label)
        return context["attempts"] > attempts

    def search(self, pv, step, label, guard_orbit=True, check_recovery_at_start=True):
        """Walk in one improving direction using independent ten-shot means.

Keep each improvement directly. A failed first step tries the opposite direction;
a decline after an improvement restores the previous best and ends the search.
Orbit intervention happens only at accepted/restored RF settings and starts a
new comparison from a freshly measured current point.
"""
        self.set_state(label)
        center = self.current[pv]
        lo, hi = self.limits[pv]
        timing = pv == TIMING_PV
        direction = -1 if self.cycle_number % 2 else 1
        progressed = opposite_tried = missing_neighbor = False
        if timing:
            self.timing_at_optimum = False
        self.checkpoint()
        score = self.transmission()
        if timing and check_recovery_at_start and self.timing_recovery_needed(score):
            return score
        for _ in range(self.config.max_search_steps):
            self.checkpoint()
            target = center + direction * step
            if not lo - 1e-9 <= target <= hi + 1e-9:
                if not progressed and not opposite_tried:
                    direction *= -1
                    opposite_tried = missing_neighbor = True
                    continue
                self.event("range_limit", pv=pv, value=center, transmission=score)
                raise RuntimeError(f"{label}: range boundary reached; expand the range and restart")
            accepted = False
            try:
                self.write({pv: target})
                measured = self.transmission()
                gain = measured - score
                self.event("probe", pv=pv, value=target, gain=gain,
                           reference_value=center, reference_transmission=score, transmission=measured)
                if gain > self.config.min_gain + 1e-12:
                    self.event("accepted", pv=pv, value=target, transmission=measured)
                    accepted = True
            finally:
                if not accepted:
                    self.write({pv: center}, restore=True)
            if accepted:
                center, score = target, measured
                progressed, missing_neighbor = True, False
                if pv == PHASE_PV:
                    self.report_response()
            else:
                # Rejected trials are undone before reading the retained point,
                # considering recovery, or comparing the opposite direction.
                score = self.transmission()
            self.checkpoint()
            if self.interleave_orbit(label, score, phase_priority=timing and not accepted):
                center = self.current[pv]
                score = self.transmission()
                progressed = opposite_tried = missing_neighbor = False
                self.event("scan_restarted", pv=pv, value=center, transmission=score,
                           reason="Orbit intervention; fresh comparison reference")
                continue
            if accepted:
                continue
            if not progressed and not opposite_tried:
                direction *= -1
                opposite_tried = True
                continue
            if missing_neighbor:
                self.event("range_limit", pv=pv, value=center, transmission=score)
                raise RuntimeError(f"{label}: range boundary reached; expand the range and restart")
            if timing and self.timing_recovery_needed(score):
                return score
            self.event("local_peak", pv=pv, value=center, transmission=score,
                       reason="First non-improving step after ascent" if progressed else "Neither direction improved")
            if timing:
                self.timing_at_optimum = True
            return score
        raise RuntimeError(f"{label}: no confirmed local maximum within iteration limit")

    def currents(self):
        return np.array([self.current[pv] for pv in CURRENT_PVS])

    def report_response(self):
        matrix = self.response.matrix()
        self.event("response", matrix_mm_per_A=None if matrix is None else matrix.tolist(),
                   observations=len(self.response.samples), correctors=list(CORRECTORS),
                   model=self.response.snapshot())
        return matrix

    def measure_response(self):
        """Acquire a fixed matrix from ~500 µm primary-plane excitations.

Each trial is bracketed by fresh ten-shot means at its original setting.
Restore every trial, including failures; commit all four columns atomically.
"""
        if not self.config.adjust_correctors:
            return
        model = RecentResponse()
        for index, pv in enumerate(CURRENT_PVS):
            self.set_state(f"Measuring response: {CORRECTORS[index]}")
            center = self.current[pv]
            lo, hi = self.limits[pv]
            sign = 1. if hi - center >= center - lo else -1.
            room = hi - center if sign > 0 else center - lo
            if room < CURRENT_STEP - 1e-12:
                raise RuntimeError(f"{CORRECTORS[index]}: no room to measure response")
            amplitude = min(RESPONSE_INITIAL_STEP_A, room)
            rows = slice(0, 2) if index % 2 == 0 else slice(2, 4)
            for attempt in range(6):
                self.checkpoint()
                before = self.orbit()
                delta = sign * amplitude
                try:
                    self.write({pv: center + delta})
                    measured = self.orbit()
                finally:
                    self.write({pv: center}, restore=True)
                after = self.orbit()
                self.checkpoint()
                response = measured - (before + after) / 2
                excursion = float(np.max(abs(response[rows])))
                accepted = .8 * RESPONSE_TARGET_MM <= excursion <= 1.2 * RESPONSE_TARGET_MM
                self.event("response_probe", pv=pv, delta_A=delta, attempt=attempt + 1,
                           target_um=1000 * RESPONSE_TARGET_MM, excursion_um=1000 * excursion,
                           before_mm=before.tolist(), measured_mm=measured.tolist(),
                           restored_mm=after.tolist(), accepted=accepted, shots=10)
                if accepted:
                    current_delta = np.zeros(4)
                    current_delta[index] = delta
                    # Cross-plane measurements never enter the calibration fit.
                    response[~RESPONSE_PLANE_MASK[:, index]] = 0.
                    model.add(current_delta, response)
                    break
                factor = np.clip(RESPONSE_TARGET_MM / max(excursion, 1e-12), 1 / 3, 3.)
                next_amplitude = min(room, max(CURRENT_STEP, round(amplitude * factor / CURRENT_STEP) * CURRENT_STEP))
                if abs(next_amplitude - amplitude) < 1e-12 or attempt == 5:
                    raise RuntimeError(f"{CORRECTORS[index]}: cannot obtain a 400–600 µm response within current limits; "
                                       f"last excursion {1000 * excursion:.1f} µm at {amplitude:.4g} A. Previous matrix retained.")
                amplitude = next_amplitude
        model.frozen = True
        model.measured_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.response = model
        self.orbit_alert = False
        if hasattr(self.io, "reset_monitor_batches"):
            self.io.reset_monitor_batches()
        self.event("response_measured", target_um=1000 * RESPONSE_TARGET_MM, shots=10,
                   measured_at=model.measured_at,
                   message="Startup response acquired; cross-plane terms zero; matrix fixed for corrections")
        self.report_response()

    def correct_orbit(self, max_steps=None, context=None):
        """Keep improving corrections, using a fixed or adaptive response model.

A usable stored model proposes the first move. Without one, or if its move
fails, try individual +/-0.005 A corrections. Fixed models are never retrained.
"""
        self.set_state("Correcting orbit")
        best_currents, best_rms = self.currents().copy(), float("inf")
        consecutive_failures = 0
        offset = 0
        if context is not None:
            best_currents = context.get("best_currents", best_currents).copy()
            best_rms = context.get("best_rms", best_rms)
            consecutive_failures = context.get("failures", 0)
            offset = context["steps"]
        steps = np.array(list(product((-1, 0, 1), repeat=4)), float) * CURRENT_STEP
        for iteration in range(self.config.max_orbit_steps if max_steps is None else max_steps):
            self.checkpoint()
            before = self.orbit()
            residual = before - self.reference
            rms = float(np.sqrt(np.mean(residual ** 2)))
            if rms < best_rms:
                best_currents, best_rms = self.currents().copy(), rms
            if context is not None:
                context.update(best_currents=best_currents.copy(), best_rms=best_rms)
            if np.max(abs(residual)) < 1e-9:
                self.event("orbit_done", reason="reference restored")
                return
            currents = self.currents()
            bounds = np.array([self.limits[pv] for pv in CURRENT_PVS])
            proposals = []
            matrix = self.response.matrix()
            if matrix is not None and np.linalg.norm(matrix) >= 1e-9:
                candidates = currents + steps
                allowed = np.all((candidates >= bounds[:, 0] - 1e-9) & (candidates <= bounds[:, 1] + 1e-9), axis=1)
                norms = np.linalg.norm(residual + steps @ matrix.T, axis=1)
                norms[~allowed] = np.inf
                best = int(np.argmin(norms))
                if np.any(steps[best]) and norms[best] < np.linalg.norm(residual) - 1e-12:
                    proposals.append((steps[best], "response model"))
                else:
                    self.event("orbit_done", reason=f"no improving {CURRENT_STEP:g} A move within limits")
                    return
            # Cycle the coordinate priority so one corrector does not consume
            # the whole iteration budget before other orbit components improve.
            for index in range(4):
                direction = np.eye(4)[(offset + iteration + index) % 4]
                for sign in (1., -1.):
                    delta = direction * sign * CURRENT_STEP
                    if not any(np.array_equal(delta, move) for move, _ in proposals):
                        proposals.append((delta, "direct correction"))
            accepted = False
            for proposal_index, (delta, source) in enumerate(proposals):
                candidate = currents + delta
                if np.any(candidate < bounds[:, 0] - 1e-9) or np.any(candidate > bounds[:, 1] + 1e-9):
                    continue
                # A failed trial was restored; remeasure to avoid comparing a
                # later correction against an old orbit during beam drift.
                if proposal_index:
                    self.checkpoint()
                    before = self.orbit()
                    rms = float(np.sqrt(np.mean((before - self.reference) ** 2)))
                    if rms < best_rms:
                        best_currents, best_rms = self.currents().copy(), rms
                targets = {pv: float(value) for pv, value, d in zip(CURRENT_PVS, candidate, delta) if d}
                original = {pv: self.current[pv] for pv in targets}
                accepted = False
                try:
                    self.write(targets)
                    measured = self.orbit()
                    self.response.add(delta, measured - before)
                    rms = float(np.sqrt(np.mean((measured - self.reference) ** 2)))
                    if rms < best_rms:
                        best_currents, best_rms = self.currents().copy(), rms
                    accepted = np.linalg.norm(measured - self.reference) < np.linalg.norm(before - self.reference)
                    consecutive_failures = 0 if accepted else consecutive_failures + 1
                    if context is not None:
                        context.update(attempts=context["attempts"] + 1, failures=consecutive_failures,
                                       best_currents=best_currents.copy(), best_rms=best_rms)
                    self.event("orbit_step", settings=targets, orbit_mm=measured.tolist(),
                               accepted=bool(accepted), source=source, rms_um=1000 * rms,
                               consecutive_failures=consecutive_failures, interleaved=context is not None)
                finally:
                    if not accepted:
                        self.write(original, restore=True)
                self.report_response()
                self.checkpoint()
                if consecutive_failures >= 3:
                    self.set_state("Restoring best orbit")
                    # Return in normal correction-sized steps; beam checks and
                    # cancellation remain active during this deliberate move.
                    while np.max(abs(best_currents - self.currents())) > 1e-12:
                        self.checkpoint()
                        values = self.currents() + np.clip(best_currents - self.currents(), -CURRENT_STEP, CURRENT_STEP)
                        self.write({pv: float(value) for pv, value, current in zip(CURRENT_PVS, values, self.currents())
                                    if abs(value - current) > 1e-12})
                    restored = self.orbit()
                    self.event("orbit_best_restored", settings=dict(zip(CURRENT_PVS, best_currents.tolist())),
                               best_rms_um=1000 * best_rms, orbit_mm=restored.tolist(),
                               reason="Three consecutive unsuccessful correction attempts")
                    return False
                if accepted:
                    if context is not None:
                        context["steps"] += 1
                    break
            if not accepted:
                self.event("orbit_done", reason="no improving correction; accepted currents retained")
                return
        self.event("orbit_done", reason="single correction step complete" if context is not None else "correction iteration limit")

    def evaluate_transmission(self, allow_baseline_update=True):
        """Evaluate settled transmission; orbit drift does not block phase recovery."""
        self.transmission_history.clear()
        self.set_state("Checking transmission after timing")
        threshold = self.recovery_threshold()
        for index in range(self.config.degradation_samples):
            self.checkpoint()
            self.transmission_history.append(self.transmission())
            self.event("transmission_average", average=float(np.mean(self.transmission_history)),
                       samples=index + 1, required=self.config.degradation_samples,
                       threshold=threshold, complete=index + 1 == self.config.degradation_samples)
        orbit_normal = self.check_orbit(force=True, interrupt=False)
        self.checkpoint()
        average = float(np.mean(self.transmission_history))
        if orbit_normal and allow_baseline_update:
            self.update_transmission_baseline(average, self.latest_orbit)
        elif average <= self.transmission_baseline + 1e-12:
            self.reset_baseline_candidate("Improvement was not sustained")
        # Above-baseline observations with a displaced orbit retain the pending
        # candidate but cannot add confirmations or promote it.
        threshold = self.recovery_threshold()
        degraded = average < threshold - 1e-12
        self.event("degradation_decision", average=average, threshold=threshold, degraded=degraded,
                   samples=list(self.transmission_history), orbit_normal=orbit_normal,
                   baseline=self.transmission_baseline, initial=self.initial_transmission)
        return degraded, orbit_normal

    def cycle(self):
        self.cycle_number += 1
        self.set_state("Checking orbit")
        self.check_orbit(force=True, interrupt=False)
        # Timing yields at accepted settings when recovery is needed; confirmed
        # improving timing moves may continue until an improvement fails.
        if self.config.adjust_timing:
            self.search(TIMING_PV, TIMING_STEP, "Timing tracking", guard_orbit=False)
        degraded, orbit_normal = self.evaluate_transmission(
            allow_baseline_update=not self.config.adjust_timing or self.timing_at_optimum)
        if degraded and self.config.adjust_phase:
            self.event("phase_priority", message="Transmission below threshold; phase recovery before orbit correction")
            self.search(PHASE_PV, PHASE_STEP, "CM8L phase recovery", guard_orbit=False)
            if self.config.adjust_timing:
                self.search(TIMING_PV, TIMING_STEP, "Timing after phase recovery", guard_orbit=False,
                            check_recovery_at_start=False)
            # Phase/timing changes may have removed the drift; use a new mean.
            orbit_normal = self.check_orbit(force=True, interrupt=False)
        if not orbit_normal and self.config.adjust_correctors and not self.orbit_intervention_blocked():
            self.event("orbit_recovery", message="Orbit outside threshold after phase decision; correcting orbit")
            self.correct_orbit()
            self.orbit_alert = False
            if self.config.adjust_timing:
                self.search(TIMING_PV, TIMING_STEP, "Timing after orbit correction", guard_orbit=False,
                            check_recovery_at_start=False)
            # Resume baseline confirmation only at a settled operating point.
            # Three failed attempts return directly to timing/phase recovery.
            degraded, _ = self.evaluate_transmission(
                allow_baseline_update=not self.config.adjust_timing or self.timing_at_optimum)
            if degraded and self.config.adjust_phase:
                self.search(PHASE_PV, PHASE_STEP, "CM8L phase recovery", guard_orbit=False)
                if self.config.adjust_timing:
                    self.search(TIMING_PV, TIMING_STEP, "Timing after phase recovery", guard_orbit=False,
                                check_recovery_at_start=False)
        self.set_state("Monitoring")

    def wait_cycle(self):
        next_action = "Timing scan" if self.config.adjust_timing else "Feedback cycle"
        self.set_state("Waiting for next timing scan" if self.config.adjust_timing else "Waiting for next feedback cycle")
        deadline = time.monotonic() + self.config.interval_sec
        while True:
            self.checkpoint()
            self.check_orbit()
            remaining = max(0., deadline - time.monotonic())
            self.event("waiting", remaining_sec=remaining, next_action=next_action, bpm_monitoring=True)
            if remaining <= 0:
                self.io.wait(0)
                return
            self.io.wait(min(1., remaining))

    def run(self, pause_for_loss=None):
        initialized = False
        response_ready = not (self.config.measure_response_on_start and self.config.adjust_correctors)
        feedback_ready = response_ready
        while True:
            try:
                self.checkpoint()
                if not initialized:
                    self.initialize()
                    initialized = True
                self.wait_for_targets()
                if not response_ready:
                    self.measure_response()
                    response_ready = True
                if not feedback_ready:
                    self.initialize(feedback_start=True)
                    feedback_ready = True
                self.cycle()
                self.wait_cycle()
            except TargetsChanged:
                # Temporary probes have already restored their original settings.
                # Redispatch immediately, including during the cycle countdown.
                continue
            except OrbitDrift:
                self.event("orbit_priority", message="Orbit drift during wait; starting cycle with phase priority")
            except (BeamLoss, KlystronPowerLoss) as exc:
                self._interleave_context = None
                # Every probe/trial has already unwound its finally block.
                # Keep the original reference and accepted machine settings;
                # discard the interrupted search; retain response observations
                # and update them only through subsequent correction attempts.
                if pause_for_loss is None:
                    raise
                self.reset_baseline_candidate("RF power loss" if isinstance(exc, KlystronPowerLoss) else "Beam loss")
                self.transmission_history.clear()
                self.report_response()
                self.event("klystron_power_pause" if isinstance(exc, KlystronPowerLoss) else "beam_loss",
                           message=str(exc), settings=dict(getattr(self, "current", {})))
                pause_for_loss(str(exc))
