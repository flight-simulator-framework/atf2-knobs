# -*- coding: utf-8 -*-
"""
IPBSM_Opt_run_synthetic_demo.py
Headless demo (no GUI) to verify optimizer runs and produces plots in Data/<year>/<run>/.

Usage:
  python IPBSM_Opt_run_synthetic_demo.py

This uses linear mode + GF by default.
"""

from pathlib import Path
import numpy as np

from IPBSM_Opt import Optimizer, OptimizerConfig, fit_gaussian_from_samples, plot_results, now_tag
from IPBSM_Opt_Synthetic_Controller import make_random_spec, SyntheticGaussianIPBSMController


def build_run_output_dir(base_dir: Path, tag: str, suffix: str) -> Path:
    return base_dir / tag[:4] / f"{tag}-{suffix}"

def main():
    params = ["Ay", "Ey", "Coup2"]
    cfg = OptimizerConfig(
        mode_name="linear",
        method="GF",
        acquisition="UCB",
        params=params,
        bounds={p: (-2.0, 2.0) for p in params},
        init_sigma={p: 0.5 for p in params},
        meas_sigma=0.01,
        expected_y_max=0.8,
        max_steps=50,
        stop_sigma_ratio=0.25,
        seed=123,
        n_init_random=8,
        n_candidates=6000,
        n_bootstrap=40,
        ridge_fit=1e-4,
    )

    spec = make_random_spec(params=params, seed=cfg.seed, correlated=False, amp=0.8, meas_sigma=cfg.meas_sigma)
    ctrl = SyntheticGaussianIPBSMController(spec=spec, seed=cfg.seed+999)

    tag = now_tag()
    out_dir = build_run_output_dir(Path("Data"), tag, suffix=f"demo-{cfg.mode_name}-{cfg.method}")
    opt = Optimizer(ctrl, cfg, out_dir)
    out = opt.run()

    # Load and plot
    import csv
    X, y, yerr = [], [], []
    with open(out_dir/"measurements.csv", "r", encoding="utf-8") as f:
        r = csv.reader(f)
        header = next(r)
        for row in r:
            X.append([float(row[2+i]) for i in range(len(params))])
            y.append(float(row[2+len(params)]))
            yerr.append(float(row[3+len(params)]))
    X = np.array(X, float)
    y = np.array(y, float)
    yerr = np.array(yerr, float)

    fit = fit_gaussian_from_samples(X, y, mode="diag", ridge=cfg.ridge_fit)
    plot_results(cfg, out_dir, X, y, yerr, fit)

    print("Done:", out_dir)
    print("Best:", out["best_y"], out["best_x"])
    print("Fit mu:", out["boot_mu_mean"], "std:", out["boot_mu_std"])

if __name__ == "__main__":
    main()
