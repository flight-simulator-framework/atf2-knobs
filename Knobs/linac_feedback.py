"""Continuous local transmission tracking; no Qt or hardware dependencies.

Orbit coordinates are [ML8L.x, ML10L.x, ML8L.y, ML10L.y] in mm.
All grids are relative to the initial setpoints (fractional origins preserved).
"""
from collections import deque
from dataclasses import dataclass
from itertools import product

import numpy as np

from Knobs.timing_pv import TIMING_PV

PHASE_PV = "CM8L:phaseWrite"
CORRECTORS = ("ZH1L", "ZV1L", "ZH2L", "ZV2L")
CURRENT_PVS = tuple(f"{name}:currentWrite" for name in CORRECTORS)
PVS = (TIMING_PV, PHASE_PV) + CURRENT_PVS
TIMING_STEP = 2.8
PHASE_STEP = 0.5
CURRENT_STEP = 0.01


@dataclass(frozen=True)
class FeedbackConfig:
    orbit_threshold_um: float = 100.
    settle_sec: float = 5.
    interval_sec: float = 5.
    sample_timeout_sec: float = 15.
    timing_range_ns: float = 112.
    phase_range_deg: float = 8.
    current_range_A: float = 0.5
    min_gain: float = 0.0  # absolute transmission, not percent; GUI-adjustable
    max_search_steps: int = 100
    max_orbit_steps: int = 50
    downstream: str = "DR"

    def __post_init__(self):
        for name in ("orbit_threshold_um", "sample_timeout_sec", "timing_range_ns",
                     "phase_range_deg", "current_range_A"):
            if not np.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive and finite")
        for name in ("settle_sec", "interval_sec", "min_gain"):
            if not np.isfinite(getattr(self, name)) or getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative and finite")
        if self.timing_range_ns < TIMING_STEP or self.phase_range_deg < PHASE_STEP or self.current_range_A < CURRENT_STEP:
            raise ValueError("Search ranges must allow at least one step")
        if self.max_search_steps < 1 or self.max_orbit_steps < 1:
            raise ValueError("Iteration limits must be positive")
        if self.downstream not in ("DR", "LNE", "BTM", "BTE"):
            raise ValueError("Select DR, LNE, BTM or BTE transmission")


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


class FeedbackController:
    """IO supplies read/write, transmission, orbit, wait and checkpoint.

write(..., restore=True) must work after cancellation to remove temporary
probes. checkpoint may pause ONLY when accepted settings are applied.
"""
    def __init__(self, io, config, limits, emit=lambda event: None):
        self.io, self.config, self.emit = io, config, emit
        self.limits = dict(limits)
        self.response = RecentResponse()
        self.cycle_number = 0
        self.state = "Idle"

    def event(self, kind, **values):
        self.emit(dict(kind=kind, state=self.state, cycle=self.cycle_number, **values))

    def set_state(self, state):
        self.state = state
        self.event("state")

    def transmission(self):
        value = float(self.io.transmission())
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
        self.previous_peak = self.initial_transmission
        self.event("reference", orbit_mm=self.reference.tolist(), transmission=self.initial_transmission,
                   settings=self.origin, limits=self.limits)

    def write(self, values, restore=False):
        for pv, value in values.items():
            lo, hi = self.limits[pv]
            if not np.isfinite(value) or not lo - 1e-9 <= value <= hi + 1e-9:
                raise ValueError(f"Feedback target outside range: {pv}={value}")
        self.io.write(values, restore=restore)
        self.current.update(values)

    def search(self, pv, step, label):
        """Re-measure both neighbours around each accepted centre; no old best.

Bracket each neighbour with centre measurements to reject linear time drift.
Probe order alternates between cycles. No move on ties or insignificant gain.
"""
        self.set_state(label)
        center = self.current[pv]
        lo, hi = self.limits[pv]
        for _ in range(self.config.max_search_steps):
            self.io.checkpoint()
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
                    self.response = RecentResponse()  # New optics; reacquire local response.
                    self.report_response()
            else:
                # A noisy probe is not evidence that a local maximum was reached.
                self.event("retry", message="Unconfirmed neighbour improvement")
        raise RuntimeError(f"{label}: no confirmed local maximum within iteration limit")

    def currents(self):
        return np.array([self.current[pv] for pv in CURRENT_PVS])

    def learn_response(self):
        self.set_state("Learning corrector response")
        for index, pv in enumerate(CURRENT_PVS):
            self.io.checkpoint()
            current = self.current[pv]
            lo, hi = self.limits[pv]
            delta = CURRENT_STEP if current + CURRENT_STEP <= hi + 1e-9 else -CURRENT_STEP
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
            self.response.add(dx, measured - (before + after) / 2)
        self.report_response()

    def report_response(self):
        matrix = self.response.matrix()
        self.event("response", matrix_mm_per_A=None if matrix is None else matrix.tolist(),
                   observations=len(self.response.samples), correctors=list(CORRECTORS))
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
        peak = self.search(TIMING_PV, TIMING_STEP, "Timing tracking")
        decreased = peak < self.previous_peak - self.config.min_gain
        self.event("timing_result", transmission=peak, previous_peak=self.previous_peak, decreased=decreased)
        orbit = self.orbit()
        if decreased and np.max(abs(orbit - self.reference)) >= self.config.orbit_threshold_um / 1000. - 1e-12:
            self.correct_orbit()
            peak = self.search(TIMING_PV, TIMING_STEP, "Timing after orbit correction")
        self.previous_peak = peak
        if peak < self.initial_transmission - self.config.min_gain:
            self.search(PHASE_PV, PHASE_STEP, "CM8L phase recovery")
        self.set_state("Monitoring")

    def run(self):
        self.initialize()
        while True:
            self.io.checkpoint()
            self.cycle()
            self.io.wait(self.config.interval_sec)
