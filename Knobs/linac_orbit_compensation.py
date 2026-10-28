"""Bounded, measured-response restoration of the gun-section reference orbit.

Positions are mm, currents A. All hardware access is supplied by the caller.
"""
import json
from pathlib import Path

import numpy as np

from Knobs.linac_feedback import RecentResponse, CURRENT_PVS as FEEDBACK_CURRENT_PVS

BPMS = ("ML8L", "ML10L")
CORRECTORS = ("ZH1L", "ZH2L", "ZV1L", "ZV2L")
THRESHOLD_MM = 0.15
SAMPLES_PER_MEAN = 10
MAX_STEP_A = 0.1
MAX_EXCURSION_A = 0.5
MAX_ITERATIONS = 10


def load_feedback_response(path):
    """Read the shared cache and reorder H1,V1,H2,V2 into H1,H2,V1,V2."""
    path = Path(path)
    try:
        model = RecentResponse.from_snapshot(json.loads(path.read_text()))
        matrix = model.matrix()
        if matrix is None:
            raise ValueError("Incomplete response matrix")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError(f"Auto restore needs a valid Active Feedback response matrix at {path}. "
                         "Use Measure response matrix at Start in Active Feedback first.") from exc
    indices = [FEEDBACK_CURRENT_PVS.index(f"{name}:currentWrite") for name in CORRECTORS]
    return matrix[:, indices], dict(response_cache=str(path), response_measured_at=model.measured_at,
                                    response_frozen=model.frozen)


def mean_orbit(data):
    """Require ten valid samples for both BPMs; do not average away failures."""
    if data.get("position_unit", "mm") != "mm":
        raise ValueError("Orbit compensation requires BPM positions in mm")
    names = list(data.get("names", []))
    if any(names.count(name) != 1 for name in BPMS):
        raise ValueError("Orbit compensation requires ML8L and ML10L")
    indices = [names.index(name) for name in BPMS]
    values = []
    for key in ("x", "y", "tmit"):
        array = np.asarray(data.get(key, []), float)
        if array.shape != (SAMPLES_PER_MEAN, len(names)):
            raise ValueError(f"Orbit compensation requires exactly {SAMPLES_PER_MEAN} {key} samples per BPM")
        selected = array[:, indices]
        if not np.isfinite(selected).all() or (key == "tmit" and np.any(selected <= 0)):
            raise ValueError(f"Invalid BPM {key} samples; compensation stopped")
        if key != "tmit":
            values.extend(selected.mean(axis=0))
    if "status" in data:
        status = np.asarray(data["status"])
        if status.shape != (SAMPLES_PER_MEAN, len(names)) or not np.all(status[:, indices] == 1):
            raise ValueError("Invalid BPM status; compensation stopped")
    return np.asarray(values, float)  # x8, x10, y8, y10


class OrbitCompensation:
    def __init__(self, *, measure, read, write, checkpoint, limits, report, response):
        self.measure, self.read, self.write = measure, read, write
        self.checkpoint = checkpoint
        self.limits = np.asarray([limits[name] for name in CORRECTORS], float)
        if self.limits.shape != (4, 2) or not np.isfinite(self.limits).all() or np.any(self.limits[:, 0] >= self.limits[:, 1]):
            raise ValueError("Verified current limits required for all four gun correctors")
        self.response = np.asarray(response, float).copy()
        if self.response.shape != (4, 4) or not np.isfinite(self.response).all():
            raise ValueError("A valid four-coordinate response matrix is required")
        self.response[:2, 2:] = 0.
        self.response[2:, :2] = 0.
        self.report = report

    def _sample(self):
        orbit = np.asarray(self.measure(), float)
        if orbit.shape != (4,) or not np.isfinite(orbit).all():
            raise ValueError("Invalid four-coordinate orbit measurement")
        return orbit

    def capture(self):
        self.checkpoint()
        self.origin = np.asarray(self.read(), float)
        if self.origin.shape != (4,) or not np.isfinite(self.origin).all() or np.any(self.origin < self.limits[:, 0]) or np.any(self.origin > self.limits[:, 1]):
            raise ValueError("Invalid initial gun corrector currents")
        self.reference = self._sample()
        self.report.update(reference_mm=self.reference.tolist(), initial_currents_A=self.origin.tolist(),
                           samples_per_mean=SAMPLES_PER_MEAN, position_unit="mm", bpm_order=list(BPMS), coordinate_order=["x8", "x10", "y8", "y10"],
                           corrector_order=list(CORRECTORS), threshold_mm=THRESHOLD_MM,
                           response_mm_per_A=self.response.tolist(), max_step_A=MAX_STEP_A, max_excursion_A=MAX_EXCURSION_A,
                           max_iterations=MAX_ITERATIONS, probes=[], iterations=[], status="reference_captured")

    def correct(self):
        self.checkpoint()
        orbit = self._sample()
        drift = orbit - self.reference
        self.report.update(after_scan_mm=orbit.tolist(), planes="none")
        if np.all(abs(drift) < THRESHOLD_MM - 1e-12):
            self.report.update(status="within_tolerance", final_mm=orbit.tolist())
            return False
        current = np.asarray(self.read(), float)
        if not np.allclose(current, self.origin, rtol=0, atol=0.001):
            raise ValueError("Gun corrector currents changed since reference acquisition")
        lower = np.maximum(self.limits[:, 0], self.origin - MAX_EXCURSION_A)
        upper = np.minimum(self.limits[:, 1], self.origin + MAX_EXCURSION_A)
        best_current, best_orbit = current.copy(), orbit.copy()
        best_rms = float(np.sqrt(np.mean(drift ** 2)))
        gain, reason, used_planes = .7, "iteration_limit", set()
        for iteration in range(MAX_ITERATIONS):
            self.checkpoint()
            if iteration:
                orbit = self._sample()
                drift = orbit - self.reference
            rms = float(np.sqrt(np.mean(drift ** 2)))
            if rms < best_rms:
                best_current, best_orbit, best_rms = current.copy(), orbit.copy(), rms
            if np.all(abs(drift) < THRESHOLD_MM - 1e-12):
                self.report.update(status="corrected", final_mm=orbit.tolist(), final_currents_A=current.tolist())
                return bool(np.max(abs(current - self.origin)) > 1e-12)
            active = []
            if np.max(abs(drift[:2])) >= THRESHOLD_MM - 1e-12:
                active.extend((0, 1))
                used_planes.add("H")
            if np.max(abs(drift[2:])) >= THRESHOLD_MM - 1e-12:
                active.extend((2, 3))
                used_planes.add("V")
            self.report["planes"] = "".join(plane for plane in "HV" if plane in used_planes)
            active = np.asarray(active, int)
            response = self.response[np.ix_(active, active)]
            u, singular, vt = np.linalg.svd(response, full_matrices=False)
            if not np.isfinite(singular).all() or singular[0] < 1e-9 or singular[-1] <= singular[0] * 0.01:
                raise ValueError("Stored gun orbit response is singular/ill-conditioned; no correction applied")
            step = -gain * (vt.T @ ((u.T @ drift[active]) / singular))
            step *= min(1., MAX_STEP_A / max(float(np.max(abs(step))), 1e-12))
            trial = current.copy()
            trial[active] = np.clip(current[active] + step, lower[active], upper[active])
            if np.max(abs(trial - current)) < 1e-5:
                reason = "current_limit" if gain == .7 else "no_improvement"
                break
            accepted = False
            try:
                self.write({CORRECTORS[i]: float(trial[i]) for i in active}, restore=False)
                measured = self._sample()
                residual = measured - self.reference
                measured_rms = float(np.sqrt(np.mean(residual ** 2)))
                accepted = measured_rms < rms
                if measured_rms < best_rms:
                    best_current, best_orbit, best_rms = trial.copy(), measured.copy(), measured_rms
                self.report["iterations"].append(dict(iteration=iteration + 1, response_mm_per_A=response.tolist(),
                    active_correctors=[CORRECTORS[i] for i in active], singular_values=singular.tolist(),
                    currents_A=trial.tolist(), orbit_mm=measured.tolist(), rms_um=1000 * measured_rms,
                    accepted=accepted))
            finally:
                if not accepted:
                    self.write({CORRECTORS[i]: float(current[i]) for i in active}, restore=True)
            if accepted:
                current, orbit, drift = trial, measured, residual
                gain = .7
                if np.all(abs(drift) < THRESHOLD_MM - 1e-12):
                    self.report.update(status="corrected", final_mm=orbit.tolist(), final_currents_A=current.tolist())
                    return bool(np.max(abs(current - self.origin)) > 1e-12)
            else:
                # Retry a smaller correction using the same saved response.
                gain *= .5
        # Nonconvergence is a normal completion: return to the best measured
        # four-coordinate RMS, including the starting point, then let Main continue.
        while np.max(abs(best_current - current)) > 1e-12:
            self.checkpoint()
            next_current = current + np.clip(best_current - current, -MAX_STEP_A, MAX_STEP_A)
            self.write({name: float(value) for name, value, previous in zip(CORRECTORS, next_current, current)
                        if abs(value - previous) > 1e-12}, restore=True)
            current = next_current
        final = self._sample()
        self.report.update(status="best_rms_restored", completion_reason=reason,
                           best_rms_um=1000 * best_rms, best_mm=best_orbit.tolist(),
                           final_mm=final.tolist(), final_currents_A=current.tolist(),
                           reached_tolerance=bool(np.all(abs(final - self.reference) < THRESHOLD_MM - 1e-12)))
        return bool(np.max(abs(current - self.origin)) > 1e-12)
