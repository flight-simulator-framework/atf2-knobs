"""Continuous local transmission tracking; no Qt or hardware dependencies.

Orbit coordinates are [ML8L.x, ML10L.x, ML8L.y, ML10L.y] in mm.
All grids are relative to the initial setpoints (fractional origins preserved).
"""
from collections import deque
from dataclasses import dataclass
from itertools import product
import time

import numpy as np

from Knobs.timing_pv import TIMING_PV

PHASE_PV = "CM8L:phaseWrite"
CORRECTORS = ("ZH1L", "ZV1L", "ZH2L", "ZV2L")
CURRENT_PVS = tuple(f"{name}:currentWrite" for name in CORRECTORS)
PVS = (TIMING_PV, PHASE_PV) + CURRENT_PVS
TIMING_STEP = 2.8
PHASE_STEP = 0.5
CURRENT_STEP = 0.01
INITIAL_PROBE_A = 0.05


class BeamLoss(RuntimeError):
    """A nonpositive ICT measurement: unwind probes, then wait for the operator."""


class OrbitDrift(RuntimeError):
    """Switch to orbit recovery only after a temporary probe has been restored."""


@dataclass(frozen=True)
class FeedbackConfig:
    orbit_threshold_um: float = 100.
    settle_sec: float = 2.
    interval_sec: float = 5.
    sample_timeout_sec: float = 15.
    timing_range_ns: float = 112.
    phase_range_deg: float = 8.
    current_range_A: float = 0.5
    min_gain: float = 0.0  # absolute transmission, not percent; GUI-adjustable
    max_search_steps: int = 100
    max_orbit_steps: int = 50
    downstream: str = "DR"
    degradation_samples: int = 5
    degradation_percent: float = 5.  # absolute percentage points, 5 pp = 0.05
    baseline_hold_sec: float = 120.
    baseline_min_confirmations: int = 3

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
Separate excitation of all four correctors is required before prediction.
"""
    def __init__(self):
        self.samples = deque(maxlen=64)

    def add(self, delta_current, delta_orbit):
        x, y = np.asarray(delta_current, float), np.asarray(delta_orbit, float)
        if x.shape != (4,) or y.shape != (4,) or not np.isfinite(np.r_[x, y]).all():
            raise ValueError("Invalid response observation")
        if np.linalg.norm(x) > 1e-9:
            self.samples.append((x.copy(), y.copy()))

    def matrix(self):
        if len(self.samples) < 4:
            return None
        x, y = (np.array(a) for a in zip(*self.samples))
        weights = 0.9 ** np.arange(len(x) - 1, -1, -1)
        x, y = x * np.sqrt(weights[:, None]), y * np.sqrt(weights[:, None])
        if np.linalg.matrix_rank(x, tol=1e-7) < 4:
            return None
        # Small regularization stabilizes noisy estimates without a model prior.
        return np.linalg.solve(x.T @ x + np.eye(4) * 1e-10, x.T @ y).T

    def snapshot(self):
        return dict(version=1, correctors=list(CURRENT_PVS),
                    coordinates=["ML8L.x", "ML10L.x", "ML8L.y", "ML10L.y"],
                    units="mm/A", samples=[[x.tolist(), y.tolist()] for x, y in self.samples])

    @classmethod
    def from_snapshot(cls, data):
        model = cls()
        expected = model.snapshot()
        if any(data.get(key) != expected[key] for key in ("version", "correctors", "coordinates", "units")):
            raise ValueError("Incompatible response cache")
        if not isinstance(data.get("samples"), list) or not 4 <= len(data["samples"]) <= 64:
            raise ValueError("Invalid response cache observations")
        for x, y in data["samples"]:
            model.add(x, y)
        if model.matrix() is None:
            raise ValueError("Response cache lacks independent corrector observations")
        return model


class FeedbackController:
    """IO supplies read/write, transmission, orbit, wait and checkpoint.

write(..., restore=True) must work after cancellation to remove temporary
probes. checkpoint may pause ONLY when accepted settings are applied.
"""
    def __init__(self, io, config, limits, emit=lambda event: None):
        self.io, self.config, self.emit = io, config, emit
        self.limits = dict(limits)
        self.response = RecentResponse()
        self.response_needs_refresh = False
        self.cycle_number = 0
        self.state = "Idle"
        self.orbit_alert = False
        self.transmission_history = deque(maxlen=config.degradation_samples)
        self._baseline_candidate = None

    def event(self, kind, **values):
        self.emit(dict(kind=kind, state=self.state, cycle=self.cycle_number, **values))

    def set_state(self, state):
        self.state = state
        self.event("state")

    def transmission(self):
        value = float(self.io.transmission())
        if np.isfinite(value) and value <= 0:
            raise BeamLoss(f"Transmission dropped to {value:g}")
        if not np.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("Invalid transmission measurement")
        self.event("transmission", transmission=value, settings=dict(self.current))
        return value

    def orbit(self):
        value = np.asarray(self.io.orbit(), float)
        if value.shape != (4,) or not np.isfinite(value).all():
            raise ValueError("Invalid three-sample orbit mean")
        self.event("orbit", orbit_mm=value.tolist())
        return value

    def outside_orbit_threshold(self, value):
        return bool(np.max(abs(value - self.reference)) >= self.config.orbit_threshold_um / 1000. - 1e-12)

    def monitor_orbit(self, value):
        # Latch alerts from the continuous monitor, including during probes.
        # A fresh measurement at an accepted setting must confirm the alert.
        if self.outside_orbit_threshold(value):
            self.orbit_alert = True

    def check_orbit(self, force=False):
        if force or self.orbit_alert:
            self.orbit_alert = False
            orbit = self.orbit()
            if self.outside_orbit_threshold(orbit):
                raise OrbitDrift("ML8L / ML10L orbit exceeds the drift threshold")
            self.orbit_alert = False

    def settings_event(self):
        self.event("settings", settings=dict(self.current), initial_settings=dict(self.origin),
                   delta={pv: self.current[pv] - self.origin[pv] for pv in PVS})

    def recovery_threshold(self):
        return max(0., self.transmission_baseline - self.config.degradation_percent / 100.)

    def reset_baseline_candidate(self, reason):
        if self._baseline_candidate is not None:
            self._baseline_candidate = None
            self.event("baseline_pending_reset", reason=reason)

    def update_transmission_baseline(self, average):
        """Promote a sustained operating-point floor, never a transient maximum.

Only complete averages at timing optima with a confirmed normal orbit qualify.
The original orbit/current references and initial transmission remain fixed.
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
            previous = self.transmission_baseline
            self.transmission_baseline = candidate["floor"]
            self.event("baseline_updated", previous=previous, baseline=self.transmission_baseline,
                       initial=self.initial_transmission, threshold=self.recovery_threshold(),
                       sustained_sec=duration, confirmations=candidate["confirmations"],
                       settings=dict(self.current), reason="Sustained improvement; minimum confirmed mean")
            self._baseline_candidate = None
        else:
            self.event("baseline_pending", candidate=candidate["floor"], baseline=self.transmission_baseline,
                       remaining_sec=max(0., self.config.baseline_hold_sec - duration),
                       confirmations=candidate["confirmations"], required=self.config.baseline_min_confirmations)

    def initialize(self):
        self.set_state("Capturing reference")
        self.io.checkpoint()
        self.current = {pv: float(self.io.read(pv)) for pv in PVS}
        if not np.isfinite(list(self.current.values())).all():
            raise ValueError("Invalid initial setpoints")
        self.origin = dict(self.current)
        excursions = (self.config.timing_range_ns, self.config.phase_range_deg) + (self.config.current_range_A,) * 4
        for pv, span in zip(PVS, excursions):
            lo, hi = self.limits[pv]
            lo, hi = max(lo, self.origin[pv] - span), min(hi, self.origin[pv] + span)
            if not np.isfinite([lo, hi]).all() or not lo <= self.origin[pv] <= hi or lo >= hi:
                raise ValueError(f"Invalid limits for {pv}")
            self.limits[pv] = (lo, hi)
        self.reference = self.orbit()
        self.initial_transmission = self.transmission()
        if self.initial_transmission <= 0:
            raise ValueError("Establish transmitted beam before starting feedback")
        self.transmission_baseline = self.initial_transmission
        self._baseline_candidate = None
        self.event("reference", orbit_mm=self.reference.tolist(), transmission=self.initial_transmission,
                   settings=self.origin, limits=self.limits,
                   baseline=self.transmission_baseline, degradation_threshold=self.recovery_threshold())
        self.settings_event()
        if hasattr(self.io, "set_orbit_monitor"):
            self.io.set_orbit_monitor(self.monitor_orbit)

    def write(self, values, restore=False):
        for pv, value in values.items():
            lo, hi = self.limits[pv]
            if not np.isfinite(value) or not lo - 1e-9 <= value <= hi + 1e-9:
                raise ValueError(f"Feedback target outside range: {pv}={value}")
        self.io.write(values, restore=restore)
        self.current.update(values)
        self.settings_event()

    def search(self, pv, step, label, guard_orbit=True):
        """Re-measure both neighbours around each accepted centre; no old best.

Bracket each neighbour with centre measurements to reject linear time drift.
Probe order alternates between cycles. No move on ties or insignificant gain.
"""
        self.set_state(label)
        center = self.current[pv]
        lo, hi = self.limits[pv]
        for _ in range(self.config.max_search_steps):
            self.io.checkpoint()
            if guard_orbit:
                self.check_orbit()
            best_gain, best_value = self.config.min_gain, center
            missing_neighbor = False
            order = (-1, 1) if self.cycle_number % 2 else (1, -1)
            for direction in order:
                target = center + direction * step
                if not lo - 1e-9 <= target <= hi + 1e-9:
                    missing_neighbor = True
                    continue
                before = self.transmission()
                try:
                    self.write({pv: target})
                    trial = self.transmission()
                finally:
                    self.write({pv: center}, restore=True)
                after = self.transmission()
                if guard_orbit:
                    self.check_orbit()  # Probe restored; orbit can take priority.
                gain = trial - (before + after) / 2
                self.event("probe", pv=pv, value=target, gain=gain)
                if gain > best_gain + 1e-12:
                    best_gain, best_value = gain, target
            if best_value == center:
                value = self.transmission()
                # A range endpoint cannot establish an unconstrained local optimum.
                if missing_neighbor:
                    self.event("range_limit", pv=pv, value=center, transmission=value)
                    raise RuntimeError(f"{label}: range boundary reached; expand the range and restart")
                self.event("local_peak", pv=pv, value=center, transmission=value)
                return value
            # Confirmation uses a fresh centre measurement; undo a failed move.
            before = self.transmission()
            accepted = False
            try:
                self.write({pv: best_value})
                after = self.transmission()
                accepted = after > before + self.config.min_gain
            finally:
                if not accepted:
                    self.write({pv: center}, restore=True)
            if accepted:
                center = best_value
                self.event("accepted", pv=pv, value=center, transmission=after)
                if pv == PHASE_PV:
                    self.response_needs_refresh = True  # Retain information; refresh before reuse.
                    self.report_response()
            else:
                # A noisy probe is not evidence that a local maximum was reached.
                self.event("retry", message="Unconfirmed neighbour improvement")
        raise RuntimeError(f"{label}: no confirmed local maximum within iteration limit")

    def currents(self):
        return np.array([self.current[pv] for pv in CURRENT_PVS])

    def learn_response(self):
        self.set_state("Learning corrector response")
        probe = INITIAL_PROBE_A if self.response.matrix() is None else CURRENT_STEP
        self.event("response_probe_size", probe_A=probe)
        # Keep the previous model intact if calibration is interrupted. Changed
        # optics need fresh observations so old, larger probes do not dominate.
        refreshed = RecentResponse.from_snapshot(self.response.snapshot()) if (
            self.response.matrix() is not None and not self.response_needs_refresh) else RecentResponse()
        for index, pv in enumerate(CURRENT_PVS):
            self.io.checkpoint()
            current = self.current[pv]
            lo, hi = self.limits[pv]
            # Preserve the 0.01 A grid if a configured limit leaves less room
            # than the initial 0.05 A calibration excursion.
            room = max(hi - current, current - lo)
            amplitude = min(probe, np.floor((room + 1e-9) / CURRENT_STEP) * CURRENT_STEP)
            if amplitude < CURRENT_STEP - 1e-9:
                raise RuntimeError(f"No 0.01 A probe headroom for {pv}")
            delta = amplitude if current + amplitude <= hi + 1e-9 else -amplitude
            if current + delta < lo - 1e-9:
                raise RuntimeError(f"No 0.01 A probe headroom for {pv}")
            before = self.orbit()
            try:
                self.write({pv: current + delta})
                measured = self.orbit()
            finally:
                self.write({pv: current}, restore=True)
            after = self.orbit()
            dx = np.zeros(4)
            dx[index] = delta
            refreshed.add(dx, measured - (before + after) / 2)
        self.response = refreshed
        self.response_needs_refresh = False
        self.report_response()

    def report_response(self):
        matrix = self.response.matrix()
        self.event("response", matrix_mm_per_A=None if matrix is None else matrix.tolist(),
                   observations=len(self.response.samples), correctors=list(CORRECTORS),
                   needs_refresh=self.response_needs_refresh, model=self.response.snapshot())
        return matrix

    def correct_orbit(self):
        # Refresh each independent direction on every correction episode. Recent
        # accepted/rejected moves also refine the weighted response continuously.
        self.learn_response()
        self.set_state("Correcting orbit")
        steps = np.array(list(product((-1, 0, 1), repeat=4)), float) * CURRENT_STEP
        for _ in range(self.config.max_orbit_steps):
            self.io.checkpoint()
            before = self.orbit()
            residual = before - self.reference
            # The GUI threshold triggers correction; the target remains the
            # initial orbit, down to the resolution of the 0.01 A current grid.
            if np.max(abs(residual)) < 1e-9:
                self.event("orbit_done", reason="reference restored")
                return
            matrix = self.response.matrix()
            if matrix is None or np.linalg.norm(matrix) < 1e-9:
                self.event("orbit_done", reason="response not identifiable")
                return
            currents = self.currents()
            bounds = np.array([self.limits[pv] for pv in CURRENT_PVS])
            candidates = currents + steps
            allowed = np.all((candidates >= bounds[:, 0] - 1e-9) & (candidates <= bounds[:, 1] + 1e-9), axis=1)
            norms = np.linalg.norm(residual + steps @ matrix.T, axis=1)
            norms[~allowed] = np.inf
            best = int(np.argmin(norms))
            delta = steps[best]
            if not np.any(delta) or norms[best] >= np.linalg.norm(residual) - 1e-12:
                self.event("orbit_done", reason="no improving 0.01 A move within limits")
                return
            targets = {pv: float(value) for pv, value, d in zip(CURRENT_PVS, candidates[best], delta) if d}
            original = {pv: self.current[pv] for pv in targets}
            accepted = False
            try:
                self.write(targets)
                measured = self.orbit()
                self.response.add(delta, measured - before)
                accepted = np.linalg.norm(measured - self.reference) < np.linalg.norm(residual)
                self.event("orbit_step", settings=targets, orbit_mm=measured.tolist(), accepted=bool(accepted))
            finally:
                if not accepted:
                    self.write(original, restore=True)
            self.report_response()
            if not accepted:
                self.event("orbit_done", reason="unconfirmed correction; restored accepted currents")
                return
        self.event("orbit_done", reason="correction iteration limit")

    def cycle(self):
        self.cycle_number += 1
        timing_label = "Timing tracking"
        for _ in range(3):
            try:
                self.set_state("Checking orbit")
                self.check_orbit(force=True)
                self.search(TIMING_PV, TIMING_STEP, timing_label)
                self.check_orbit(force=True)
                # Compare five new operating-point measurements, never a mix of
                # deliberate probe losses or obsolete timing/corrector settings.
                self.transmission_history.clear()
                self.set_state("Checking transmission after timing")
                threshold = self.recovery_threshold()
                for index in range(self.config.degradation_samples):
                    self.io.checkpoint()
                    self.transmission_history.append(self.transmission())
                    self.check_orbit()
                    self.event("transmission_average", average=float(np.mean(self.transmission_history)),
                               samples=index + 1, required=self.config.degradation_samples,
                               threshold=threshold, complete=index + 1 == self.config.degradation_samples)
                self.check_orbit(force=True)
                average = float(np.mean(self.transmission_history))
                self.update_transmission_baseline(average)
                threshold = self.recovery_threshold()
                degraded = average < threshold - 1e-12
                self.event("degradation_decision", average=average, threshold=threshold, degraded=degraded,
                           samples=list(self.transmission_history), orbit_normal=True,
                           baseline=self.transmission_baseline, initial=self.initial_transmission)
                if degraded:
                    self.search(PHASE_PV, PHASE_STEP, "CM8L phase recovery")
                    self.check_orbit(force=True)
                self.set_state("Monitoring")
                return
            except OrbitDrift as exc:
                self.reset_baseline_candidate("Orbit recovery required")
                self.event("orbit_priority", message=str(exc))
                self.correct_orbit()
                self.orbit_alert = False
                if self.outside_orbit_threshold(self.orbit()):
                    # Still retune timing, but never mistake an unresolved orbit
                    # problem for a reason to scan phase.
                    self.search(TIMING_PV, TIMING_STEP, "Timing after orbit correction", guard_orbit=False)
                    self.event("phase_blocked", reason="Orbit remains outside threshold")
                    self.set_state("Orbit outside threshold; phase recovery blocked")
                    return
                timing_label = "Timing after orbit correction"
        self.set_state("Orbit recovery retry limit; monitoring before next cycle")

    def wait_cycle(self):
        self.set_state("Waiting for next timing scan")
        deadline = time.monotonic() + self.config.interval_sec
        while True:
            self.io.checkpoint()
            self.check_orbit()
            remaining = max(0., deadline - time.monotonic())
            self.event("waiting", remaining_sec=remaining, next_action="Timing scan", bpm_monitoring=True)
            if remaining <= 0:
                self.io.wait(0)
                return
            self.io.wait(min(1., remaining))

    def run(self, pause_for_loss=None):
        initialized = False
        while True:
            try:
                self.io.checkpoint()
                if not initialized:
                    self.initialize()
                    initialized = True
                self.cycle()
                self.wait_cycle()
            except OrbitDrift:
                self.reset_baseline_candidate("Orbit drift during wait")
                self.event("orbit_priority", message="Orbit drift during wait; starting recovery now")
            except BeamLoss as exc:
                # Every probe/trial has already unwound its finally block.
                # Keep the original reference and accepted machine settings;
                # discard the interrupted search and refresh the retained model
                # before using it for another orbit correction.
                if pause_for_loss is None:
                    raise
                self.response_needs_refresh = True
                self.reset_baseline_candidate("Beam loss")
                self.transmission_history.clear()
                self.report_response()
                self.event("beam_loss", message=str(exc), settings=dict(getattr(self, "current", {})))
                pause_for_loss(str(exc))
