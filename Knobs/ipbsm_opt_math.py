from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np


@dataclass
class GaussianFitResult:
    ok: bool
    mu: List[float]
    cov: List[List[float]]
    amp: float
    ln_amp: float
    ridge: float
    mode: str  # "diag" or "full"
    residual_rms: float
    n_points: int


def _design_matrix_quadratic(X: np.ndarray, mode: str) -> Tuple[np.ndarray, List[Tuple[str, Tuple[int, int]]]]:
    n, d = X.shape
    cols = []
    meta = []

    cols.append(np.ones((n, 1)))
    meta.append(("c", (-1, -1)))

    cols.append(X)
    for i in range(d):
        meta.append(("b", (i, i)))

    cols.append(X * X)
    for i in range(d):
        meta.append(("d", (i, i)))

    if mode == "full":
        cross_terms = []
        for i in range(d):
            for j in range(i + 1, d):
                cross_terms.append((X[:, i] * X[:, j]).reshape(n, 1))
                meta.append(("e", (i, j)))
        if cross_terms:
            cols.append(np.hstack(cross_terms))

    Phi = np.hstack(cols)
    return Phi, meta


def fit_gaussian_from_samples(
    X: np.ndarray,
    y: np.ndarray,
    mode: str = "diag",
    ridge: float = 1e-6,
    eps_y: float = 1e-6,
    y_cap: Optional[float] = None,
    weighted: bool = True,
) -> GaussianFitResult:
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    n, d = X.shape

    if n < max(6, d + 2):
        return GaussianFitResult(
            ok=False,
            mu=[0.0] * d,
            cov=np.eye(d).tolist(),
            amp=float(np.max(y)) if n else 0.0,
            ln_amp=float(np.log(max(float(np.max(y)) if n else 1e-6, 1e-6))),
            ridge=ridge,
            mode=mode,
            residual_rms=float("inf"),
            n_points=n,
        )

    if y_cap is None:
        y_clip = np.maximum(y, eps_y)
    else:
        y_clip = np.clip(y, eps_y, float(y_cap))
    yln = np.log(y_clip)

    Phi, _ = _design_matrix_quadratic(X, mode=mode)

    if weighted:
        wgt = np.sqrt(y_clip / max(float(np.max(y_clip)), eps_y))
        W = wgt.reshape(-1, 1)
        Phi_w = Phi * W
        yln_w = yln * wgt
        A = Phi_w.T @ Phi_w
        A += ridge * np.eye(A.shape[0])
        w = np.linalg.solve(A, Phi_w.T @ yln_w)
    else:
        A = Phi.T @ Phi
        A += ridge * np.eye(A.shape[0])
        w = np.linalg.solve(A, Phi.T @ yln)

    yln_hat = Phi @ w
    residual = yln - yln_hat
    residual_rms = float(np.sqrt(np.mean(residual**2)))

    idx = 0
    c = float(w[idx]); idx += 1
    b = np.array(w[idx:idx + d], dtype=float); idx += d
    dcoef = np.array(w[idx:idx + d], dtype=float); idx += d

    M = np.zeros((d, d), dtype=float)
    for i in range(d):
        M[i, i] = dcoef[i]
    if mode == "full":
        for i in range(d):
            for j in range(i + 1, d):
                if idx >= len(w):
                    break
                eij = float(w[idx]); idx += 1
                M[i, j] = 0.5 * eij
                M[j, i] = 0.5 * eij

    Q = -2.0 * M
    Q = 0.5 * (Q + Q.T)

    try:
        eigvals, eigvecs = np.linalg.eigh(Q)
        eigvals = np.maximum(eigvals, 1e-6)
        Q_pd = eigvecs @ np.diag(eigvals) @ eigvecs.T
    except Exception:
        Q_pd = np.eye(d)

    if mode == "diag":
        Q_pd = np.diag(np.diag(Q_pd))

    try:
        cov = np.linalg.inv(Q_pd)
        mu = cov @ b
        ln_amp = c + 0.5 * float(mu.T @ Q_pd @ mu)
        ln_amp = max(min(ln_amp, 10.0), -50.0)
        amp = float(np.exp(ln_amp))
    except Exception:
        cov = np.eye(d)
        mu = np.zeros(d)
        ln_amp = float(c)
        amp = float(np.exp(ln_amp))

    return GaussianFitResult(
        ok=True,
        mu=mu.tolist(),
        cov=cov.tolist(),
        amp=amp,
        ln_amp=ln_amp,
        ridge=ridge,
        mode=mode,
        residual_rms=residual_rms,
        n_points=n,
    )


def bootstrap_fit(
    X: np.ndarray,
    y: np.ndarray,
    mode: str,
    ridge: float,
    n_boot: int = 50,
    rng: Optional[np.random.Generator] = None,
) -> Dict[str, np.ndarray]:
    X = np.asarray(X, float)
    y = np.asarray(y, float)
    n, d = X.shape
    rng = rng or np.random.default_rng()

    mu_abs_max = 10.0
    cov_diag_min = 1e-8
    cov_diag_max = 100.0

    mus = []
    covs = []
    ok_count = 0
    for _ in range(max(0, n_boot)):
        idx = rng.integers(0, n, size=n)
        fr = fit_gaussian_from_samples(X[idx], y[idx], mode=mode, ridge=ridge)
        if fr.ok:
            mu_v = np.array(fr.mu, float)
            cov_m = np.array(fr.cov, float)
            diag = np.diag(cov_m) if cov_m.ndim == 2 else np.array([])
            if np.any(~np.isfinite(mu_v)) or np.any(~np.isfinite(cov_m)):
                continue
            if np.any(np.abs(mu_v) > mu_abs_max):
                continue
            if diag.size and (np.any(diag < cov_diag_min) or np.any(diag > cov_diag_max)):
                continue
            mus.append(mu_v)
            covs.append(cov_m)
            ok_count += 1

    if ok_count == 0:
        mu_mean = np.zeros(d)
        mu_std = np.full(d, np.inf)
        cov_mean = np.eye(d)
    else:
        mu_stack = np.vstack(mus)
        cov_stack = np.stack(covs, axis=0)
        mu_mean = np.mean(mu_stack, axis=0)
        mu_std = np.std(mu_stack, axis=0, ddof=1) if ok_count > 1 else np.zeros(d)
        cov_mean = np.mean(cov_stack, axis=0)

    cov_diag = np.diag(cov_mean)
    if ok_count > 1:
        cov_diag_std = np.std(np.stack([np.diag(c) for c in covs], axis=0), axis=0, ddof=1)
    else:
        cov_diag_std = np.zeros(d)

    return {
        "mu_mean": mu_mean,
        "mu_std": mu_std,
        "cov_mean": cov_mean,
        "cov_diag_mean": cov_diag,
        "cov_diag_std": cov_diag_std,
    }


@dataclass
class GPParams:
    kernel: str = "rbf"
    length_scale: float = 1.0
    signal_var: float = 1.0
    noise_var: float = 1e-4
    zscan_axes: Optional[Sequence[int]] = None
    zscan_kernel: str = "rbf"


def _as_ard_length_scales(length_scale, d: int) -> np.ndarray:
    ls = np.asarray(length_scale, float).reshape(-1)
    if ls.size == 0:
        ls = np.ones(d, dtype=float)
    elif ls.size == 1:
        ls = np.full(d, float(ls[0]), dtype=float)
    elif ls.size != d:
        raise ValueError(f"length_scale must have size 1 or {d}, got {ls.size}")
    return np.maximum(ls, 1e-9)


def _pairwise_ard_distance(X1: np.ndarray, X2: np.ndarray, length_scale) -> np.ndarray:
    X1 = np.asarray(X1, float)
    X2 = np.asarray(X2, float)
    ls = _as_ard_length_scales(length_scale, X1.shape[1])
    diff = (X1[:, None, :] - X2[None, :, :]) / ls.reshape(1, 1, -1)
    return np.sqrt(np.maximum(np.sum(diff * diff, axis=2), 0.0))


def rbf_kernel(X1: np.ndarray, X2: np.ndarray, length_scale, signal_var: float) -> np.ndarray:
    r = _pairwise_ard_distance(X1, X2, length_scale)
    return float(signal_var) * np.exp(-0.5 * r * r)


def matern32_kernel(X1: np.ndarray, X2: np.ndarray, length_scale, signal_var: float) -> np.ndarray:
    r = _pairwise_ard_distance(X1, X2, length_scale)
    c = np.sqrt(3.0) * r
    return float(signal_var) * (1.0 + c) * np.exp(-c)


def matern52_kernel(X1: np.ndarray, X2: np.ndarray, length_scale, signal_var: float) -> np.ndarray:
    r = _pairwise_ard_distance(X1, X2, length_scale)
    c = np.sqrt(5.0) * r
    return float(signal_var) * (1.0 + c + (5.0 / 3.0) * r * r) * np.exp(-c)


def _kernel_by_name(X1: np.ndarray, X2: np.ndarray, kernel_name: str, length_scale, signal_var: float) -> np.ndarray:
    kernel = str(kernel_name).strip().lower()
    if kernel in ("rbf", "se", "squared_exponential"):
        return rbf_kernel(X1, X2, length_scale, signal_var)
    if kernel in ("matern32", "matern_32", "mat32", "m32"):
        return matern32_kernel(X1, X2, length_scale, signal_var)
    if kernel in ("matern52", "matern_52", "mat52", "m52"):
        return matern52_kernel(X1, X2, length_scale, signal_var)
    raise ValueError(f"Unknown GP kernel: {kernel_name}")


def _normalize_axis_indices(axes: Optional[Sequence[int]], d: int) -> List[int]:
    if axes is None:
        return []
    out = []
    for a in axes:
        try:
            idx = int(a)
        except Exception:
            continue
        if 0 <= idx < d:
            out.append(idx)
    return sorted(set(out))


def kernel_matrix(X1: np.ndarray, X2: np.ndarray, params: GPParams) -> np.ndarray:
    X1 = np.asarray(X1, float)
    X2 = np.asarray(X2, float)
    d = int(X1.shape[1])
    if X2.shape[1] != d:
        raise ValueError(f"Kernel dimension mismatch: X1 has {d}, X2 has {X2.shape[1]}")

    z_axes = _normalize_axis_indices(getattr(params, "zscan_axes", None), d)
    if len(z_axes) == 0:
        return _kernel_by_name(X1, X2, params.kernel, params.length_scale, params.signal_var)

    ls_all = _as_ard_length_scales(params.length_scale, d)
    z_kernel_name = str(getattr(params, "zscan_kernel", "rbf"))
    non_z_axes = [i for i in range(d) if i not in z_axes]

    K = np.ones((X1.shape[0], X2.shape[0]), dtype=float)
    if len(non_z_axes) > 0:
        K *= _kernel_by_name(
            X1[:, non_z_axes],
            X2[:, non_z_axes],
            params.kernel,
            ls_all[non_z_axes],
            1.0,
        )
    K *= _kernel_by_name(
        X1[:, z_axes],
        X2[:, z_axes],
        z_kernel_name,
        ls_all[z_axes],
        1.0,
    )
    return float(params.signal_var) * K


class SimpleGP:
    def __init__(self, params: GPParams):
        self.params = params
        self.X = None
        self.y = None
        self.L = None
        self.alpha = None

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        X = np.asarray(X, float)
        y = np.asarray(y, float).reshape(-1, 1)
        n = X.shape[0]
        K = kernel_matrix(X, X, self.params)
        K += (float(self.params.noise_var) + 1e-12) * np.eye(n)
        try:
            self.L = np.linalg.cholesky(K)
        except np.linalg.LinAlgError:
            jitter = max(1e-10, 10.0 * float(self.params.noise_var) + 1e-10)
            self.L = np.linalg.cholesky(K + jitter * np.eye(n))
        self.alpha = np.linalg.solve(self.L.T, np.linalg.solve(self.L, y))
        self.X = X
        self.y = y

    def predict(self, Xs: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        Xs = np.asarray(Xs, float)
        if self.X is None:
            mu = np.zeros(Xs.shape[0])
            std = np.ones(Xs.shape[0])
            return mu, std
        Ks = kernel_matrix(self.X, Xs, self.params)
        mu = (Ks.T @ self.alpha).reshape(-1)
        v = np.linalg.solve(self.L, Ks)
        kss = np.diag(kernel_matrix(Xs, Xs, self.params))
        var = np.maximum(kss - np.sum(v * v, axis=0), 1e-12)
        std = np.sqrt(var)
        return mu, std


def normal_pdf(z: np.ndarray) -> np.ndarray:
    return (1.0 / np.sqrt(2.0 * np.pi)) * np.exp(-0.5 * z * z)


def normal_cdf(z: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.vectorize(math.erf)(z / np.sqrt(2.0)))


def acq_ucb(mu: np.ndarray, std: np.ndarray, beta: float) -> np.ndarray:
    return mu + beta * std


def acq_ei(mu: np.ndarray, std: np.ndarray, y_best: float, xi: float = 0.0) -> np.ndarray:
    imp = mu - y_best - xi
    z = imp / np.maximum(std, 1e-12)
    return imp * normal_cdf(z) + std * normal_pdf(z)
