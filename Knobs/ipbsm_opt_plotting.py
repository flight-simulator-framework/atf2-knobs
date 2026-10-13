from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

try:
    from .ipbsm_opt_math import GPParams, GaussianFitResult, SimpleGP
except ImportError:
    from ipbsm_opt_math import GPParams, GaussianFitResult, SimpleGP


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def _run_file_stamp(out_dir: Path) -> str:
    label = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in out_dir.name).strip("_")
    return label or "run"


def _title_time_stamp(file_stamp: str) -> str:
    parts = str(file_stamp).split("-")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{parts[0]}-{parts[1]}"
    return str(file_stamp)


def _fit_gf_axis_1d(x: np.ndarray, y: np.ndarray):
    x = np.asarray(x, float).reshape(-1)
    y = np.asarray(y, float).reshape(-1)
    if x.size < 3 or y.size < 3:
        return None
    y_clip = np.clip(y, 1e-6, None)
    try:
        a, b, c = np.polyfit(x, np.log(y_clip), deg=2)
    except Exception:
        return None
    if (not np.isfinite(a)) or a >= -1e-12:
        return None
    mu = -b / (2.0 * a)
    sigma2 = -1.0 / (2.0 * a)
    if (not np.isfinite(mu)) or (not np.isfinite(sigma2)) or sigma2 <= 0.0:
        return None
    sigma = float(np.sqrt(sigma2))
    ln_amp = float(c + 0.5 * (mu * mu) / sigma2)
    amp = float(np.exp(np.clip(ln_amp, -50.0, 10.0)))
    yln_hat = a * x * x + b * x + c
    resid_rms = float(np.sqrt(np.mean((np.log(y_clip) - yln_hat) ** 2)))
    return {
        "mu": float(mu),
        "sigma": sigma,
        "amp": amp,
        "residual_rms": resid_rms,
        "n_points": int(x.size),
    }


def build_gf_axiswise_fit(cfg, X: np.ndarray, y: np.ndarray, chosen_by: Optional[List[str]] = None) -> GaussianFitResult:
    params = list(getattr(cfg, "params", []) or [])
    chosen = list(chosen_by) if chosen_by is not None else []
    d = len(params)
    mu = np.array([float(getattr(cfg, "param_origins", {}).get(p, 0.0)) for p in params], float)
    cov = np.eye(d, dtype=float)
    amp_vals = []
    residuals = []
    point_counts = []

    for i, p in enumerate(params):
        idx = [k for k, cb in enumerate(chosen) if isinstance(cb, str) and cb.startswith(f"GF[{p}]")]
        if idx:
            fit_1d = _fit_gf_axis_1d(X[idx, i], y[idx])
        else:
            fit_1d = None
        if fit_1d is None:
            cov[i, i] = max(1e-12, float(getattr(cfg, "init_sigma", {}).get(p, 0.5)) ** 2)
            continue
        mu[i] = float(fit_1d["mu"])
        cov[i, i] = max(1e-12, float(fit_1d["sigma"]) ** 2)
        amp_vals.append(float(fit_1d["amp"]))
        residuals.append(float(fit_1d["residual_rms"]))
        point_counts.append(int(fit_1d["n_points"]))

    amp = float(np.nanmax(np.asarray(amp_vals, float))) if amp_vals else (float(np.max(y)) if np.size(y) else 0.0)
    return GaussianFitResult(
        ok=True,
        mu=mu.tolist(),
        cov=cov.tolist(),
        amp=float(amp),
        ln_amp=float(np.log(max(float(amp), 1e-6))),
        ridge=float(getattr(cfg, "ridge_fit", 1e-4)),
        mode="diag",
        residual_rms=float(np.nanmean(np.asarray(residuals, float))) if residuals else float("nan"),
        n_points=int(np.sum(point_counts)) if point_counts else int(np.size(y)),
    )


def plot_results(
    cfg,
    out_dir: Path,
    X: np.ndarray,
    y: np.ndarray,
    yerr: np.ndarray,
    fit,
    boot: Optional[Dict] = None,
    chosen_by: Optional[List[str]] = None,
    average: Optional[np.ndarray] = None,
    average_pause_ratio: Optional[float] = None,
    average_limit: Optional[float] = None,
    include_1d: bool = True,
    save_prefix: str = "",
    bo_data_only_1d: bool = False,
    discarded_rows: Optional[List[Dict[str, Any]]] = None,
) -> List[Path]:
    import matplotlib.pyplot as plt

    out_dir = ensure_dir(out_dir)
    file_stamp = _run_file_stamp(out_dir)
    title_stamp = _title_time_stamp(file_stamp)
    params = cfg.params
    d = len(params)
    lo = np.array([cfg.bounds[p][0] for p in params], float)
    hi = np.array([cfg.bounds[p][1] for p in params], float)

    mu = np.array(fit.mu, float)
    cov = np.array(fit.cov, float)
    cov = 0.5 * (cov + cov.T)
    amp = float(fit.amp)
    if cfg.expected_y_max is not None:
        amp = min(amp, float(cfg.expected_y_max))

    try:
        Q = np.linalg.inv(cov)
    except Exception:
        Q = np.eye(d)

    def gaussian(x_vec: np.ndarray) -> float:
        dx = np.asarray(x_vec - mu, dtype=float).reshape(-1)
        quad = float(dx @ Q @ dx)
        val = amp * float(np.exp(-0.5 * quad))
        return val

    saved = []
    chosen = list(chosen_by) if chosen_by is not None else None
    discarded = list(discarded_rows) if discarded_rows is not None else []
    discarded_steps: List[int] = []
    discarded_X_rows: List[List[float]] = []
    discarded_y_vals: List[float] = []
    discarded_yerr_vals: List[float] = []
    discarded_avg_vals: List[float] = []
    discarded_chosen: List[str] = []
    for item in discarded:
        x_map = item.get("x", {})
        if not isinstance(x_map, dict):
            continue
        try:
            discarded_steps.append(int(item.get("step", len(discarded_steps) + 1)))
            discarded_X_rows.append([float(x_map.get(p, float("nan"))) for p in params])
            discarded_y_vals.append(float(item.get("y", float("nan"))))
            discarded_yerr_vals.append(float(item.get("y_err", float("nan"))))
            discarded_chosen.append(str(item.get("chosen_by", "")))
            dat = dict(item.get("dat", {}) or {})
            discarded_avg_vals.append(float(dat.get("average", float("nan"))))
        except Exception:
            continue
    discarded_X = np.asarray(discarded_X_rows, float) if discarded_X_rows else np.zeros((0, d), float)
    discarded_y = np.asarray(discarded_y_vals, float) if discarded_y_vals else np.zeros((0,), float)
    discarded_yerr = np.asarray(discarded_yerr_vals, float) if discarded_yerr_vals else np.zeros((0,), float)
    discarded_avg = np.asarray(discarded_avg_vals, float) if discarded_avg_vals else np.zeros((0,), float)
    method_upper = str(getattr(cfg, "method", "")).upper()
    is_sequential = method_upper in {"GF", "SEQUENTIAL"}
    is_bo = method_upper == "BO"

    ard_cfg = getattr(cfg, "gp_ard_length_scales", None)
    if not isinstance(ard_cfg, dict):
        ard_cfg = {}
    gp_length_scales = []
    for p in params:
        try:
            ls_val = float(ard_cfg.get(p, getattr(cfg, "gp_length_scale", 1.0)))
        except Exception:
            ls_val = float(getattr(cfg, "gp_length_scale", 1.0))
        gp_length_scales.append(max(1e-6, ls_val))
    zscan_names = set(str(n) for n in (getattr(cfg, "zscan_axis_names", ["Z scan knob"]) or ["Z scan knob"]))
    z_axes = [idx for idx, p in enumerate(params) if str(p) in zscan_names]
    gp_for_zpair = None
    x_ref = mu.copy()
    if (not is_sequential) and z_axes and (X.ndim == 2) and (X.shape[0] >= 2):
        try:
            best_idx = int(np.nanargmax(y))
            x_ref = np.asarray(X[best_idx], float).copy()
        except Exception:
            x_ref = mu.copy()
        try:
            gp_for_zpair = SimpleGP(GPParams(
                kernel=getattr(cfg, "gp_kernel", "rbf"),
                length_scale=np.asarray(gp_length_scales, float),
                signal_var=float(getattr(cfg, "gp_signal_var", 0.15)),
                noise_var=float(getattr(cfg, "gp_noise_var", 1e-4)),
                zscan_axes=z_axes,
                zscan_kernel="rbf",
            ))
            gp_for_zpair.fit(np.asarray(X, float), np.asarray(y, float).reshape(-1))
        except Exception:
            gp_for_zpair = None

    if y.size > 0:
        fig = plt.figure()
        ax = fig.add_subplot(111)
        xs_eval = np.arange(1, y.size + 1, dtype=int)
        if str(getattr(cfg, "method", "")).upper() == "BO" and chosen is not None and len(chosen) == len(y):
            init_idx = [
                k for k, cb in enumerate(chosen)
                if isinstance(cb, str) and cb.startswith("init_")
            ]
            if init_idx:
                init_last = max(init_idx) + 1
                ax.axvspan(0.5, init_last + 0.5, color="#d8ecff", alpha=0.35, lw=0)
                if init_last < y.size:
                    ax.axvspan(init_last + 0.5, y.size + 0.5, color="#fff0cc", alpha=0.35, lw=0)
                y_top = float(np.nanmax(y)) if np.size(y) else 1.0
                if not np.isfinite(y_top):
                    y_top = 1.0
                ax.text(max(1.0, init_last * 0.5), y_top, "Initial", ha="center", va="bottom", color="#355c7d")
                if init_last < y.size:
                    ax.text((init_last + 1 + y.size) * 0.5, y_top, "BO", ha="center", va="bottom", color="#8a5a12")
        ax.plot(xs_eval, y, marker="o")
        if discarded_steps and discarded_y.size:
            ax.plot(
                np.asarray(discarded_steps, dtype=float),
                discarded_y,
                linestyle="None",
                marker="x",
                color="#dc2626",
                markersize=8,
                markeredgewidth=2.0,
            )
        ax.set_xlabel("Evaluation")
        ax.set_ylabel("IPBSM modulation")
        ax.set_title("Modulation vs evaluation")
        ax.grid(True, alpha=0.3)
        path = out_dir / (f"{save_prefix}{file_stamp}_modulation_vs_evaluation.png" if save_prefix else f"{file_stamp}_modulation_vs_evaluation.png")
        fig.tight_layout()
        fig.savefig(path, dpi=150)
        plt.close(fig)
        saved.append(path)

    avg = np.asarray(average, float).reshape(-1) if average is not None else np.zeros((0,), float)
    if avg.size > 0:
        fig = plt.figure()
        ax = fig.add_subplot(111)
        xs_eval = np.arange(1, avg.size + 1, dtype=int)
        ax.plot(xs_eval, avg, marker="o", color="#2e8b57")
        if discarded_steps and discarded_avg.size:
            mask_disc = np.isfinite(discarded_avg)
            if np.any(mask_disc):
                ax.plot(
                    np.asarray(discarded_steps, dtype=float)[mask_disc],
                    discarded_avg[mask_disc],
                    linestyle="None",
                    marker="x",
                    color="#dc2626",
                    markersize=8,
                    markeredgewidth=2.0,
                )

        lim = float(average_limit) if average_limit is not None else float("nan")
        if not np.isfinite(lim):
            ratio = average_pause_ratio
            if ratio is None:
                ratio = getattr(cfg, "average_pause_ratio", None)
            finite_idx = np.where(np.isfinite(avg))[0]
            if finite_idx.size > 0 and ratio is not None:
                first_avg = float(avg[int(finite_idx[0])])
                lim = float(first_avg) * float(ratio)
        if np.isfinite(lim):
            ax.axhline(lim, color="#8a5a12", linestyle="--", linewidth=1.2, label=f"Pause limit: {lim:.3f}")
            ax.legend(loc="best")

        ax.set_xlabel("Evaluation")
        ax.set_ylabel("IPBSM average")
        ax.set_title("Average vs evaluation")
        ax.grid(True, alpha=0.3)
        path = out_dir / (f"{save_prefix}{file_stamp}_average_vs_evaluation.png" if save_prefix else f"{file_stamp}_average_vs_evaluation.png")
        fig.tight_layout()
        fig.savefig(path, dpi=150)
        plt.close(fig)
        saved.append(path)

    if include_1d:
        for i, p in enumerate(params):
            fig = plt.figure()
            ax = fig.add_subplot(111)
            x_plot = X[:, i]
            y_plot = y
            yerr_plot = yerr
            fit_mu = float(mu[i])
            fit_sigma = math.sqrt(max(cov[i, i], 1e-12))
            fit_amp = amp
            fit_resid = float(fit.residual_rms)
            fit_n = int(fit.n_points)
            fit_curve = None
            show_fit = True
            if is_sequential and chosen is not None and len(chosen) == len(y):
                idx = [k for k, cb in enumerate(chosen) if isinstance(cb, str) and cb.startswith(f"GF[{p}]")]
                if idx:
                    x_plot = X[idx, i]
                    y_plot = y[idx]
                    yerr_plot = yerr[idx]
                else:
                    x_plot = np.zeros((0,), float)
                    y_plot = np.zeros((0,), float)
                    yerr_plot = np.zeros((0,), float)
                gf_fit = _fit_gf_axis_1d(x_plot, y_plot)
                if gf_fit is not None:
                    fit_mu = float(gf_fit["mu"])
                    fit_sigma = float(gf_fit["sigma"])
                    fit_amp = float(gf_fit["amp"])
                    fit_resid = float(gf_fit["residual_rms"])
                    fit_n = int(gf_fit["n_points"])
                    xs = np.linspace(lo[i], hi[i], 250)
                    fit_curve = fit_amp * np.exp(-0.5 * ((xs - fit_mu) / max(fit_sigma, 1e-12)) ** 2)
                else:
                    show_fit = False
            elif bo_data_only_1d and is_bo:
                fit_curve = None
                show_fit = False
            else:
                xs = np.linspace(lo[i], hi[i], 250)
                ys = []
                for x1 in xs:
                    x_vec = mu.copy()
                    x_vec[i] = x1
                    ys.append(gaussian(x_vec))
                fit_curve = np.array(ys, float)

            ax.errorbar(x_plot, y_plot, yerr=yerr_plot, fmt="o", capsize=2, color="black", ecolor="black")
            disc_idx: List[int] = []
            if discarded_y.size:
                if is_sequential and discarded_chosen:
                    disc_idx = [
                        k for k, cb in enumerate(discarded_chosen)
                        if isinstance(cb, str) and cb.startswith(f"GF[{p}]")
                    ]
                else:
                    disc_idx = list(range(discarded_y.size))
            if disc_idx:
                disc_idx_arr = np.asarray(disc_idx, dtype=int)
                ax.plot(
                    discarded_X[disc_idx_arr, i],
                    discarded_y[disc_idx_arr],
                    linestyle="None",
                    marker="x",
                    color="#dc2626",
                    markersize=8,
                    markeredgewidth=2.0,
                )
            if fit_curve is not None:
                ax.plot(xs, fit_curve)
            ax.set_xlabel(p)
            ax.set_ylabel("IPBSM modulation")
            if is_sequential:
                title_main = f"1D axis scan: {p}"
            elif bo_data_only_1d and is_bo:
                title_main = f"1D slice data: {p}" if d >= 2 else f"1D BO data: {p}"
            elif is_bo and d >= 2:
                title_main = f"1D slice fit: {p}"
            elif is_bo:
                title_main = f"1D BO fit: {p}"
            else:
                title_main = f"1D fit: {p}"
            ax.set_title(f"{title_main} [{title_stamp}]")

            mu_err = None
            sigma_err = None
            if boot is not None and (not is_sequential):
                mu_err = float(boot["mu_std"][i])
                var_mean = float(boot["cov_diag_mean"][i])
                var_std = float(boot["cov_diag_std"][i])
                sigma_err = 0.5 * var_std / max(1e-12, math.sqrt(var_mean))

            if show_fit:
                txt_lines = [
                    f"mu[{p}] = {fit_mu:+.4f}" + (f" ± {mu_err:.4f}" if mu_err is not None else ""),
                    f"sigma[{p}] = {fit_sigma:.4f}" + (f" ± {sigma_err:.4f}" if sigma_err is not None else ""),
                    f"amp = {fit_amp:.4f}",
                    f"resid_rms(ln) = {fit_resid:.4f}",
                    f"n = {fit_n}",
                ]
            elif bo_data_only_1d and is_bo:
                txt_lines = [
                    f"n = {len(x_plot)}",
                    "BO live data only (no Gaussian fit)",
                ]
            else:
                txt_lines = [
                    f"n = {len(x_plot)}",
                    "Waiting for >= 3 axis points",
                    "Fit will appear after init0/init+/init-",
                ]

            ax.text(
                0.98, 0.98,
                "\n".join(txt_lines),
                transform=ax.transAxes,
                va="top",
                ha="right",
            )

            name = f"{save_prefix}{file_stamp}_1D_{p}.png" if save_prefix else f"{file_stamp}_1D_{p}.png"
            path = out_dir / name
            fig.tight_layout()
            fig.savefig(path, dpi=150)
            plt.close(fig)
            saved.append(path)

    pairs = []
    if not (bo_data_only_1d and is_bo):
        for i in range(d):
            for j in range(i + 1, d):
                pairs.append((i, j))

    for (i, j) in pairs:
        nx = 120
        ny = 120
        xs = np.linspace(lo[i], hi[i], nx)
        ys = np.linspace(lo[j], hi[j], ny)
        use_gp_surface = (gp_for_zpair is not None) and ((i in z_axes) or (j in z_axes))
        if use_gp_surface:
            XX, YY = np.meshgrid(xs, ys)
            grid = np.repeat(x_ref.reshape(1, -1), repeats=XX.size, axis=0)
            grid[:, i] = XX.reshape(-1)
            grid[:, j] = YY.reshape(-1)
            mu_grid, _ = gp_for_zpair.predict(grid)
            Z = mu_grid.reshape(ny, nx)
        else:
            Z = np.zeros((ny, nx), float)
            for iy, yj in enumerate(ys):
                for ix, xi in enumerate(xs):
                    x_vec = mu.copy()
                    x_vec[i] = xi
                    x_vec[j] = yj
                    Z[iy, ix] = gaussian(x_vec)

        fig = plt.figure()
        ax = fig.add_subplot(111)
        vmin = 0.0
        vmax = float(cfg.expected_y_max) if cfg.expected_y_max is not None else float(np.max(Z) if Z.size else 1.0)
        im = ax.imshow(
            Z,
            origin="lower",
            extent=[lo[i], hi[i], lo[j], hi[j]],
            aspect="auto",
            vmin=vmin,
            vmax=vmax,
        )
        if is_sequential and chosen is not None and len(chosen) == len(y):
            idx_pair = [
                k for k, cb in enumerate(chosen)
                if isinstance(cb, str) and (cb.startswith(f"GF[{params[i]}]") or cb.startswith(f"GF[{params[j]}]"))
            ]
            if idx_pair:
                ax.scatter(X[idx_pair, i], X[idx_pair, j], s=12)
            if discarded_y.size and discarded_chosen:
                disc_idx_pair = [
                    k for k, cb in enumerate(discarded_chosen)
                    if isinstance(cb, str) and (cb.startswith(f"GF[{params[i]}]") or cb.startswith(f"GF[{params[j]}]"))
                ]
                if disc_idx_pair:
                    disc_idx_arr = np.asarray(disc_idx_pair, dtype=int)
                    ax.scatter(
                        discarded_X[disc_idx_arr, i],
                        discarded_X[disc_idx_arr, j],
                        s=36,
                        marker="x",
                        c="#dc2626",
                        linewidths=2.0,
                    )
        else:
            ax.scatter(X[:, i], X[:, j], s=12)
            if discarded_y.size:
                ax.scatter(
                    discarded_X[:, i],
                    discarded_X[:, j],
                    s=36,
                    marker="x",
                    c="#dc2626",
                    linewidths=2.0,
                )
        ax.set_xlabel(params[i])
        ax.set_ylabel(params[j])
        if use_gp_surface:
            ax.set_title(f"2D GP heatmap (Matern32-Z): {params[i]} vs {params[j]}")
            fig.colorbar(im, ax=ax, label="predicted modulation (GP mean)")
        else:
            ax.set_title(f"2D fit heatmap: {params[i]} vs {params[j]}")
            fig.colorbar(im, ax=ax, label="predicted modulation")
        name = f"{save_prefix}{file_stamp}_2D_{params[i]}_{params[j]}.png" if save_prefix else f"{file_stamp}_2D_{params[i]}_{params[j]}.png"
        path = out_dir / name
        fig.tight_layout()
        fig.savefig(path, dpi=150)
        plt.close(fig)
        saved.append(path)

    txt_path = out_dir / (f"{save_prefix}{file_stamp}_fit_params.txt" if save_prefix else f"{file_stamp}_fit_params.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("Gaussian fit parameters (from quadratic ln fit)\n")
        f.write(json.dumps({
            "params": params,
            "mu": mu.tolist(),
            "cov": cov.tolist(),
            "amp": amp,
            "residual_rms_ln": fit.residual_rms,
            "n_points": fit.n_points,
            "mode": fit.mode,
        }, indent=2, ensure_ascii=False))
    saved.append(txt_path)

    return saved


def plot_bo_gp_heatmap(
    cfg,
    out_dir: Path,
    X: np.ndarray,
    y: np.ndarray,
    *,
    save_prefix: str = "",
) -> List[Path]:
    import matplotlib.pyplot as plt

    out_dir = ensure_dir(out_dir)
    file_stamp = _run_file_stamp(out_dir)
    params = list(getattr(cfg, "params", []) or [])
    target_params = ["corrector 1", "Abe chamber"]
    if str(getattr(cfg, "method", "")).upper() != "BO":
        return []
    if params != target_params:
        return []

    X = np.asarray(X, float)
    y = np.asarray(y, float).reshape(-1)
    if X.ndim != 2 or X.shape[0] == 0 or X.shape[1] != 2 or y.size != X.shape[0]:
        return []

    length_scales = []
    for p in params:
        try:
            length_scales.append(float(getattr(cfg, "gp_ard_length_scales", {}).get(p, getattr(cfg, "gp_length_scale", 1.0))))
        except Exception:
            length_scales.append(float(getattr(cfg, "gp_length_scale", 1.0)))
    gp = SimpleGP(GPParams(
        kernel=getattr(cfg, "gp_kernel", "rbf"),
        length_scale=np.asarray(length_scales, float),
        signal_var=float(getattr(cfg, "gp_signal_var", 0.15)),
        noise_var=float(getattr(cfg, "gp_noise_var", 1e-4)),
    ))
    gp.fit(X, y)

    lo = np.array([cfg.bounds[p][0] for p in params], float)
    hi = np.array([cfg.bounds[p][1] for p in params], float)
    nx = 120
    ny = 120
    xs = np.linspace(lo[0], hi[0], nx)
    ys = np.linspace(lo[1], hi[1], ny)
    XX, YY = np.meshgrid(xs, ys)
    grid = np.column_stack([XX.reshape(-1), YY.reshape(-1)])
    mu, std = gp.predict(grid)
    Z_mu = mu.reshape(ny, nx)
    Z_std = std.reshape(ny, nx)

    saved: List[Path] = []

    fig = plt.figure()
    ax = fig.add_subplot(111)
    vmax = float(np.nanmax(Z_mu)) if np.size(Z_mu) else 1.0
    if not np.isfinite(vmax):
        vmax = 1.0
    im = ax.imshow(
        Z_mu,
        origin="lower",
        extent=[lo[0], hi[0], lo[1], hi[1]],
        aspect="auto",
        vmin=float(np.nanmin(Z_mu)) if np.size(Z_mu) else 0.0,
        vmax=vmax,
    )
    scatter = ax.scatter(X[:, 0], X[:, 1], c=y, s=24, edgecolors="white", linewidths=0.5)
    ax.set_xlabel(params[0])
    ax.set_ylabel(params[1])
    ax.set_title("BO GP mean heatmap")
    fig.colorbar(im, ax=ax, label="predicted modulation (GP mean)")
    fig.colorbar(scatter, ax=ax, label="measured modulation")
    path = out_dir / (f"{save_prefix}{file_stamp}_BO_GP_mean_corrector1_abe.png" if save_prefix else f"{file_stamp}_BO_GP_mean_corrector1_abe.png")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    saved.append(path)

    fig = plt.figure()
    ax = fig.add_subplot(111)
    im = ax.imshow(
        Z_std,
        origin="lower",
        extent=[lo[0], hi[0], lo[1], hi[1]],
        aspect="auto",
        vmin=0.0,
    )
    ax.scatter(X[:, 0], X[:, 1], s=20, facecolors="none", edgecolors="white", linewidths=0.7)
    ax.set_xlabel(params[0])
    ax.set_ylabel(params[1])
    ax.set_title("BO GP uncertainty heatmap")
    fig.colorbar(im, ax=ax, label="predicted std (GP)")
    path = out_dir / (f"{save_prefix}{file_stamp}_BO_GP_std_corrector1_abe.png" if save_prefix else f"{file_stamp}_BO_GP_std_corrector1_abe.png")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    saved.append(path)

    return saved
