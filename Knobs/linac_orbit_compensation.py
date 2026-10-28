"""Bounded, measured-response restoration of the gun-section reference orbit.

Positions are mm, currents A. All hardware access is supplied by the caller.
"""
import numpy as np

BPMS = ("ML8L", "ML10L")
CORRECTORS = ("ZH1L", "ZH2L", "ZV1L", "ZV2L")
THRESHOLD_MM = 0.1
PROBE_A = 0.05
MAX_STEP_A = 0.1
MAX_EXCURSION_A = 0.5
MAX_ITERATIONS = 5


def mean_orbit(data):
    """Require three valid samples for both BPMs; do not average away failures."""
    if data.get("position_unit", "mm") != "mm":
        raise ValueError("Orbit compensation requires BPM positions in mm")
    names = list(data.get("names", []))
    if any(names.count(name) != 1 for name in BPMS):
        raise ValueError("Orbit compensation requires ML8L and ML10L")
    indices = [names.index(name) for name in BPMS]
    values = []
    for key in ("x", "y", "tmit"):
        array = np.asarray(data.get(key, []), float)
        if array.shape != (3, len(names)):
            raise ValueError(f"Orbit compensation requires exactly 3 {key} samples per BPM")
        selected = array[:, indices]
        if not np.isfinite(selected).all() or (key == "tmit" and np.any(selected <= 0)):
            raise ValueError(f"Invalid BPM {key} samples; compensation stopped")
        if key != "tmit":
            values.extend(selected.mean(axis=0))
    if "status" in data:
        status = np.asarray(data["status"])
        if status.shape != (3, len(names)) or not np.all(status[:, indices] == 1):
            raise ValueError("Invalid BPM status; compensation stopped")
    return np.asarray(values, float)  # x8, x10, y8, y10


class OrbitCompensation:
    def __init__(self, *, measure, read, write, checkpoint, limits, report):
        self.measure, self.read, self.write = measure, read, write
        self.checkpoint = checkpoint
        self.limits = np.asarray([limits[name] for name in CORRECTORS], float)
        if self.limits.shape != (4, 2) or not np.isfinite(self.limits).all() or np.any(self.limits[:, 0] >= self.limits[:, 1]):
            raise ValueError("Verified current limits required for all four gun correctors")
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
                           samples_per_mean=3, position_unit="mm", bpm_order=list(BPMS), coordinate_order=["x8", "x10", "y8", "y10"],
                           corrector_order=list(CORRECTORS), threshold_mm=THRESHOLD_MM,
                           probe_A=PROBE_A, max_step_A=MAX_STEP_A, max_excursion_A=MAX_EXCURSION_A,
                           max_iterations=MAX_ITERATIONS, probes=[], iterations=[], status="reference_captured")

    def correct(self):
        self.checkpoint()
        orbit = self._sample()
        drift = orbit - self.reference
        active = []
        if np.max(abs(drift[:2])) >= THRESHOLD_MM - 1e-12:
            active.extend((0, 1))
        if np.max(abs(drift[2:])) >= THRESHOLD_MM - 1e-12:
            active.extend((2, 3))
        self.report.update(after_scan_mm=orbit.tolist(), planes=("HV" if len(active) == 4 else "H" if active == [0, 1] else "V" if active else "none"))
        if not active:
            self.report.update(status="within_tolerance", final_mm=orbit.tolist())
            return False
        active = np.asarray(active, int)
        inactive = np.asarray([i for i in range(4) if i not in active], int)
        current = np.asarray(self.read(), float)
        if not np.allclose(current, self.origin, rtol=0, atol=0.001):
            raise ValueError("Gun corrector currents changed since reference acquisition")
        lower = np.maximum(self.limits[:, 0], self.origin - MAX_EXCURSION_A)
        upper = np.minimum(self.limits[:, 1], self.origin + MAX_EXCURSION_A)
        applied = False
        for iteration in range(MAX_ITERATIONS):
            self.checkpoint()  # Pause only with probes restored and accepted settings applied.
            response = np.zeros((len(active), len(active)))
            for col, index in enumerate(active):
                self.checkpoint()
                low, high = max(lower[index], current[index] - PROBE_A), min(upper[index], current[index] + PROBE_A)
                if high - low < 0.001:
                    raise ValueError(f"No probe headroom for {CORRECTORS[index]}")
                samples = []
                try:
                    for value in (low, high):
                        self.write({CORRECTORS[index]: float(value)}, restore=False)
                        measured = self._sample()
                        samples.append(measured)
                        self.report["probes"].append(dict(iteration=iteration + 1, corrector=CORRECTORS[index],
                                                          current_A=float(value), orbit_mm=measured.tolist()))
                finally:
                    self.write({CORRECTORS[index]: float(current[index])}, restore=True)
                response[:, col] = (samples[1][active] - samples[0][active]) / (high - low)
            u, singular, vt = np.linalg.svd(response, full_matrices=False)
            if not np.isfinite(singular).all() or singular[0] < 1e-9 or singular[-1] <= singular[0] * 0.01:
                self.report["response_mm_per_A"] = response.tolist()
                raise ValueError("Gun orbit response is singular/ill-conditioned; no correction applied")
            step = -0.7 * (vt.T @ ((u.T @ drift[active]) / singular))
            step *= min(1., MAX_STEP_A / max(float(np.max(abs(step))), 1e-12))
            trial = current.copy()
            trial[active] = np.clip(current[active] + step, lower[active], upper[active])
            if np.max(abs(trial - current)) < 1e-5:
                self.report["status"] = "current_limit"
                break
            accepted = False
            try:
                self.write({CORRECTORS[i]: float(trial[i]) for i in active}, restore=False)
                measured = self._sample()
                residual = measured - self.reference
                accepted = bool(np.linalg.norm(residual[active]) < np.linalg.norm(drift[active])
                                and (not len(inactive) or np.all(abs(residual[inactive]) < THRESHOLD_MM - 1e-12)))
                self.report["iterations"].append(dict(iteration=iteration + 1, response_mm_per_A=response.tolist(),
                    singular_values=singular.tolist(), currents_A=trial.tolist(), orbit_mm=measured.tolist(), accepted=accepted))
            finally:
                if not accepted:
                    self.write({CORRECTORS[i]: float(current[i]) for i in active}, restore=True)
            if not accepted:
                self.report["status"] = "no_improvement"
                break
            applied = True
            current, orbit, drift = trial, measured, residual
            if np.all(abs(drift) < THRESHOLD_MM - 1e-12):
                self.report.update(status="corrected", final_mm=orbit.tolist(), final_currents_A=current.tolist())
                return applied
        self.report.update(status=self.report.get("status") if self.report.get("status") in {"no_improvement", "current_limit"} else "iteration_limit",
                           final_mm=orbit.tolist(), final_currents_A=current.tolist())
        raise RuntimeError("Gun orbit compensation did not reach <100 um at both BPMs; retained last accepted currents")
