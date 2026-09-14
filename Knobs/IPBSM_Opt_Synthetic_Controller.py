# -*- coding: utf-8 -*-
"""
IPBSM_Opt_Synthetic_Controller.py
Creates synthetic single-peak Gaussian "IPBSM modulation" measurements with noise.

- Linear mode: independent (diagonal covariance)
- Nonlinear mode: weak correlations by default (full covariance)

The "true" peak:
- center ~ 0.4 (random per run, each dimension)
- sigma ~ 0.5 (random per dimension)
- amplitude max = 0.8
- measurement error ~ 0.01
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional
import numpy as np

from IPBSM_Opt import BaseIPBSMController

@dataclass
class SyntheticGaussianSpec:
    params: List[str]
    mu: np.ndarray          # (d,)
    cov: np.ndarray         # (d,d)
    amp: float = 0.8
    meas_sigma: float = 0.01

def make_random_spec(
    params: List[str],
    seed: int = 123,
    mu_center: float = 0.4,
    mu_jitter: float = 0.25,
    sigma_center: float = 0.5,
    sigma_jitter: float = 0.15,
    weak_corr: float = 0.2,
    correlated: bool = True,
    amp: float = 0.8,
    meas_sigma: float = 0.01,
) -> SyntheticGaussianSpec:
    rng = np.random.default_rng(seed)
    d = len(params)

    mu = mu_center + mu_jitter * rng.uniform(-1.0, 1.0, size=d)
    sig = np.maximum(0.20, sigma_center + sigma_jitter * rng.uniform(-1.0, 1.0, size=d))

    if not correlated or d == 1:
        cov = np.diag(sig**2)
    else:
        cov = np.diag(sig**2)
        # Add weak correlations
        for i in range(d):
            for j in range(i+1, d):
                rho = weak_corr * rng.uniform(-1.0, 1.0)
                cov[i, j] = rho * sig[i] * sig[j]
                cov[j, i] = cov[i, j]

        # Make sure PD (add jitter if needed)
        try:
            ev = np.linalg.eigvalsh(cov)
            if np.min(ev) <= 1e-8:
                cov = cov + (abs(np.min(ev)) + 1e-6) * np.eye(d)
        except Exception:
            cov = np.diag(sig**2)

    return SyntheticGaussianSpec(params=params, mu=mu, cov=cov, amp=amp, meas_sigma=meas_sigma)

class SyntheticGaussianIPBSMController(BaseIPBSMController):
    """
    A controller that mimics machine behavior:
      apply_knobs -> internal state updated
      get_ipbsm -> returns noisy modulation
    """
    def __init__(self, spec: SyntheticGaussianSpec, seed: int = 999):
        self.spec = spec
        self.state = {p: 0.0 for p in spec.params}
        self.rng = np.random.default_rng(seed)

        try:
            self.Q = np.linalg.inv(self.spec.cov)
        except Exception:
            self.Q = np.eye(len(self.spec.params))

    def apply_knobs(self, knob_values: Dict[str, float]) -> None:
        for k, v in knob_values.items():
            if k in self.state:
                self.state[k] = float(v)

    def get_ipbsm(self) -> Tuple[float, float]:
        x = np.array([self.state[p] for p in self.spec.params], float)
        dx = (x - self.spec.mu).reshape(-1, 1)
        y_true = self.spec.amp * float(np.exp(-0.5 * (dx.T @ self.Q @ dx)))
        y_meas = y_true + float(self.rng.normal(0.0, self.spec.meas_sigma))
        # enforce physical bounds for this test model: 0 <= modulation <= amp
        y_meas = float(np.clip(y_meas, 0.0, self.spec.amp))
        return y_meas, float(self.spec.meas_sigma)

    def set_magnet_current(self, name: str, value: float) -> None:
        # Not used in synthetic mode; knobs use apply_knobs
        self.apply_knobs({name: value})

    def set_magnet_position(self, name: str, value: float) -> None:
        # Not used in synthetic mode
        self.apply_knobs({name: value})
