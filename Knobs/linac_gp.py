"""Bounded online GP fitting for Corrector GroupBO; no machine access."""
import time

import numpy as np


class CorrectorGPModel:
    """Normalize inputs and freeze output scaling at the first varying dataset.

    Keeping the output transform fixed preserves the meaning of the last good
    hyperparameters when a later fit fails. Raw scores remain with the caller.
    """

    def __init__(self, lower, upper, steps, refit_interval=5, log=lambda text: None,
                 cancelled=lambda: False, fit_timeout=5.0, names=None):
        self.lower = np.asarray(lower, dtype=float)
        self.width = np.maximum(np.asarray(upper, dtype=float) - self.lower, steps)
        self.names = list(names) if names is not None else [str(i) for i in range(len(self.width))]
        if not np.all(np.isfinite(self.width)) or np.any(self.width <= 0):
            raise ValueError("Invalid Corrector GP input ranges")
        length_min = np.minimum(2., np.maximum(.02, 2 * np.asarray(steps) / self.width))
        self.bounds = np.log(np.array(list(zip(length_min, np.full(len(self.width), 2.)))
                                      + [(.1, 10.), (.001, 1.)]))
        self.theta = np.clip(np.log(np.r_[np.full(len(self.width), 1 / 3), 1., .01]),
                             self.bounds[:, 0], self.bounds[:, 1])
        self.refit_interval = max(1, int(refit_interval))
        self.log, self.cancelled = log, cancelled
        self.fit_timeout = float(fit_timeout)
        self.center, self.scale = 0., 1.
        self.scale_frozen = False
        self.last_fit_count = None
        self.successful_fits = 0

    @property
    def parameters(self):
        values = np.exp(self.theta)
        return values[:-2].copy(), float(values[-2]), float(values[-1])

    def transform_inputs(self, values):
        return (np.asarray(values, dtype=float) - self.lower) / self.width

    @staticmethod
    def negative_log_likelihood(theta, x, y):
        """Exact RBF-ARD negative log marginal likelihood and log-gradient."""
        from scipy.linalg import cho_factor, cho_solve

        length, signal, noise = np.exp(theta[:-2]), np.exp(theta[-2]), np.exp(theta[-1])
        delta2 = ((x[:, None, :] - x[None, :, :]) / length) ** 2
        kernel = signal ** 2 * np.exp(-.5 * np.sum(delta2, axis=2))
        covariance = kernel + (noise ** 2 + 1e-10) * np.eye(len(y))
        factor = cho_factor(covariance, lower=True, check_finite=False)
        alpha = cho_solve(factor, y, check_finite=False)
        nll = .5 * float(y @ alpha) + np.log(np.diag(factor[0])).sum() + .5 * len(y) * np.log(2 * np.pi)
        weight = cho_solve(factor, np.eye(len(y)), check_finite=False) - np.outer(alpha, alpha)
        grad_length = .5 * np.einsum("ij,ij,ijd->d", weight, kernel, delta2)
        gradient = np.r_[grad_length, np.sum(weight * kernel), noise ** 2 * np.trace(weight)]
        if not np.isfinite(nll) or not np.all(np.isfinite(gradient)):
            raise ValueError("Non-finite GP likelihood/gradient")
        return float(nll), gradient

    def prepare(self, inputs, scores):
        x = self.transform_inputs(inputs)
        y = np.asarray(scores, dtype=float)
        if x.ndim != 2 or y.ndim != 1 or len(x) != len(y) or not len(y):
            raise ValueError("Incomplete Corrector GP training data")
        if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
            raise ValueError("Non-finite Corrector GP training data")
        if not self.scale_frozen:
            # Difference-first arithmetic reduces cancellation for large offsets.
            offset = y - y[0]
            spread = float(np.std(offset))
            resolution = max(np.finfo(float).tiny, 32 * np.finfo(float).eps * float(np.max(np.abs(y))))
            self.center = float(y[0] + np.mean(offset))
            if spread > resolution:
                self.scale = spread
                self.scale_frozen = True
                self.log(f"[Corrector GP] standardize: center={self.center:.9g}, scale={self.scale:.9g} (fixed for run)")
        standardized = (y - self.center) / self.scale
        if not np.isfinite(self.scale) or not np.all(np.isfinite(standardized)):
            raise ValueError("Corrector GP score standardization overflow")
        enough_data = len(y) >= max(4, x.shape[1] + 2)
        due = self.last_fit_count is None or len(y) - self.last_fit_count >= self.refit_interval
        if enough_data and due:
            self.last_fit_count = len(y)
            if self.scale_frozen:
                self._fit(x, standardized)
            else:
                self.log(f"[Corrector GP] n={len(y)}: constant/unresolved scores; keep prior, defer fit and uncertainty stop")
        return x, standardized

    def _fit(self, x, y):
        started = time.monotonic()
        previous = self.theta.copy()

        def objective(theta):
            if self.cancelled():
                raise InterruptedError("Run stopped")
            if time.monotonic() - started > self.fit_timeout:
                raise TimeoutError("GP fit time budget exceeded")
            return self.negative_log_likelihood(theta, x, y)

        try:
            from scipy.optimize import minimize
            before, _ = objective(previous)
            result = minimize(objective, previous, jac=True, method="L-BFGS-B",
                              bounds=self.bounds, options={"maxiter": 80, "maxfun": 160, "ftol": 1e-7})
            if not result.success:
                raise RuntimeError(str(result.message))
            fitted = np.asarray(result.x, dtype=float)
            if (fitted.shape != previous.shape or not np.all(np.isfinite(fitted))
                    or np.any(fitted < self.bounds[:, 0]) or np.any(fitted > self.bounds[:, 1])):
                raise ValueError("Fitted GP parameters outside bounds")
            after, _ = objective(fitted)
            if after > before + 1e-7:
                raise ValueError("GP fit did not improve likelihood")
            self.theta = fitted.copy()
            self.successful_fits += 1
            length, signal, noise = self.parameters
            self.log(f"[Corrector GP] fit n={len(y)}: NLL={before:.6g}->{after:.6g}; "
                     f"length_A={dict(zip(self.names, (length * self.width).tolist()))}, "
                     f"signal_sd={signal:.6g}, noise_sd={noise:.6g} (standardized)")
        except Exception as exc:
            # No partially fitted parameters are ever applied.
            self.theta = previous
            self.log(f"[Corrector GP] fit skipped/failed at n={len(y)}; keep previous parameters: {exc}")

