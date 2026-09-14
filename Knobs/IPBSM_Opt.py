
from __future__ import annotations

import os
import json
import time
import math
import csv
import datetime as _dt
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Callable

import time
import numpy as np
from Interfaces.ATF2.InterfaceATF2_Ext import InterfaceATF2_Ext
from Interfaces.ATF2.InterfaceATF2_Ext import CurrentDropToZeroError

def now_tag() -> str:
    # Asia/Tokyo not enforced here; GUI passes local time anyway.
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")

def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p

def clamp(x: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    return np.minimum(np.maximum(x, lo), hi)

def normal_pdf(z: np.ndarray) -> np.ndarray:
    return (1.0 / np.sqrt(2.0 * np.pi)) * np.exp(-0.5 * z * z)

def normal_cdf(z: np.ndarray) -> np.ndarray:
    # Using erf; no scipy dependency
    return 0.5 * (1.0 + np.vectorize(math.erf)(z / np.sqrt(2.0)))

# ----------------------------
# Controllers (interfaces)
# ----------------------------

class BaseIPBSMController:
    """
    Abstract controller. Real machine version should talk to EPICS PVs.
    """

    def get_ipbsm(self) -> Tuple[float, float]:
        """Return (modulation, modulation_error)."""
        raise NotImplementedError

    def get_ipbsm_full(self) -> Dict[str, Any]:
        """
        Return full dat-style payload when available.
        Fallback implementation uses get_ipbsm() only.
        """
        y, yerr = self.get_ipbsm()
        return {
            "modulation": float(y),
            "error": abs(float(yerr)),
            "beamsize": float("nan"),
            "ebeamsize": float("nan"),
            "average": float("nan"),
            "phase": float("nan"),
            "filename": "",
            "ict_average": float("nan"),
        }

    def set_magnet_current(self, name: str, values: float) -> None:
        raise NotImplementedError

    def set_magnet_position(self, name: str, values: float) -> None:
        raise NotImplementedError

    def apply_knobs(self, knob_valuess: Dict[str, float]) -> None:
        """
        Default: map knob_valuess to currents with set_magnet_current.
        Override if your machine mapping differs.
        """
        for k, v in knob_valuess.items():
            self.set_magnet_current(k, v)


class IPBSMInterface(InterfaceATF2_Ext):
    """
    Compatibility wrapper using ATF2 Ext interface as the fixed machine interface.
    """
    def __init__(self, nsamples=3):
        super().__init__(nsamples=nsamples)


class EPICSIPBSMController:
    """
    Controller wrapper for real machine.
    Optimizer expects: apply_knobs(dict)->None and get_ipbsm()->(y,yerr)
    """
    def __init__(
        self,
        interface: IPBSMInterface,
        mode_name: str = "linear",
        scan_mode_label: str = "30",
        baseline_state: Optional[Dict[str, Any]] = None,
    ):
        self.interface = interface
        self.mode_name = str(mode_name).lower()
        self.scan_mode_label = str(scan_mode_label or "30")
        self.baseline_state: Dict[str, Any] = {}
        self.linear_base_positions: Dict[str, Dict[str, float]] = {}
        self.nonlinear_base_currents: Dict[str, float] = {}
        self.corrector_base_values: Dict[str, float] = {}
        self.zscan_base_values: Dict[str, float] = {}
        if baseline_state:
            self.set_baseline_state(baseline_state)

    def set_baseline_state(self, baseline_state: Dict[str, Any]) -> None:
        self.baseline_state = dict(baseline_state or {})
        if "scan_mode_label" in self.baseline_state:
            self.scan_mode_label = str(self.baseline_state.get("scan_mode_label", self.scan_mode_label) or self.scan_mode_label)
        self.linear_base_positions = {}
        self.nonlinear_base_currents = {}
        self.corrector_base_values = {}
        self.zscan_base_values = {}

        raw_linear = self.baseline_state.get("linear_base_positions", {})
        if isinstance(raw_linear, dict):
            for mag, item in raw_linear.items():
                if isinstance(item, dict):
                    self.linear_base_positions[str(mag)] = {
                        "x": float(item.get("x", float("nan"))),
                        "y": float(item.get("y", float("nan"))),
                    }

        raw_nonlinear = self.baseline_state.get("nonlinear_base_currents", {})
        if isinstance(raw_nonlinear, dict):
            for mag, val in raw_nonlinear.items():
                self.nonlinear_base_currents[str(mag)] = float(val)

        raw_corrector = self.baseline_state.get("corrector_base_values", {})
        if isinstance(raw_corrector, dict):
            for knob, val in raw_corrector.items():
                self.corrector_base_values[str(knob)] = float(val)

        raw_zscan = self.baseline_state.get("zscan_base_values", {})
        if isinstance(raw_zscan, dict):
            for knob, val in raw_zscan.items():
                self.zscan_base_values[str(knob)] = float(val)

    def ensure_machine_origin(self, params: List[str]) -> Dict[str, Any]:
        if self.linear_base_positions or self.nonlinear_base_currents or self.corrector_base_values or self.zscan_base_values:
            return dict(self.baseline_state)
        baseline_state = self.interface.capture_knob_origin(params, scan_mode_label=self.scan_mode_label)
        self.set_baseline_state(baseline_state)
        return dict(self.baseline_state)

    def _split_knob_payload(self, knob_values: Dict[str, float]) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, float], Dict[str, float]]:
        linear_names = set(self.interface.get_linear_knob_names())
        nonlinear_names = set(self.interface.get_nonlinear_knob_names())
        corrector_names = set(getattr(self.interface, "get_corrector_knob_names", lambda: [])())
        zscan_names = set(getattr(self.interface, "get_zscan_knob_names", lambda: [])())

        linear_payload: Dict[str, float] = {}
        nonlinear_payload: Dict[str, float] = {}
        corrector_payload: Dict[str, float] = {}
        zscan_payload: Dict[str, float] = {}
        for key, value in knob_values.items():
            if key in linear_names:
                linear_payload[key] = float(value)
            elif key in nonlinear_names:
                nonlinear_payload[key] = float(value)
            elif key in corrector_names:
                corrector_payload[key] = float(value)
            elif key in zscan_names:
                zscan_payload[key] = float(value)
            else:
                raise KeyError(f"Unknown knob for EPICS controller: {key}")
        return linear_payload, nonlinear_payload, corrector_payload, zscan_payload

    def _machine_label_linear_x(self, mag: str) -> str:
        return f"{mag}:X"

    def _machine_label_linear_y(self, mag: str) -> str:
        return f"{mag}:Y"

    def _machine_label_current(self, mag: str) -> str:
        return f"{mag}:I"

    def _machine_label_corrector(self, knob: str) -> str:
        return f"{knob}:SET"

    def _machine_label_zscan(self, knob: str) -> str:
        try:
            pvs = self.interface._zscan_pvs_for_mode(self.scan_mode_label)
            axis = str(pvs.get("axis", "")).strip()
            if axis:
                return f"{axis}:Position"
        except Exception:
            pass
        return f"{knob}:Position"

    def describe_machine_setpoint_channels(self, knob_names: List[str]) -> Dict[str, Any]:
        names = [str(k) for k in (knob_names or [])]
        self.ensure_machine_origin(names)
        zero_map = {k: 0.0 for k in names}
        linear_payload, nonlinear_payload, corrector_payload, zscan_payload = self._split_knob_payload(zero_map)

        linear_mags = []
        nonlinear_mags = []
        if hasattr(self.interface, "_linear_magnets_for_knobs"):
            linear_mags = list(getattr(self.interface, "_linear_magnets_for_knobs")(list(linear_payload.keys())))
        if hasattr(self.interface, "_nonlinear_magnets_for_knobs"):
            nonlinear_mags = list(getattr(self.interface, "_nonlinear_magnets_for_knobs")(list(nonlinear_payload.keys())))

        channels: List[str] = []
        initial: Dict[str, float] = {}

        for mag in linear_mags:
            lx = self._machine_label_linear_x(str(mag))
            ly = self._machine_label_linear_y(str(mag))
            base = dict(self.linear_base_positions.get(str(mag), {}))
            channels.extend([lx, ly])
            initial[lx] = float(base.get("x", float("nan")))
            initial[ly] = float(base.get("y", float("nan")))
        for mag in nonlinear_mags:
            li = self._machine_label_current(str(mag))
            channels.append(li)
            initial[li] = float(self.nonlinear_base_currents.get(str(mag), float("nan")))
        for knob in corrector_payload.keys():
            lc = self._machine_label_corrector(str(knob))
            channels.append(lc)
            initial[lc] = float(self.corrector_base_values.get(str(knob), float("nan")))
        for knob in zscan_payload.keys():
            lz = self._machine_label_zscan(str(knob))
            channels.append(lz)
            initial[lz] = float(self.zscan_base_values.get(str(knob), float("nan")))
        return {"channels": channels, "initial": initial}

    def compute_machine_setpoint_values(self, knob_values: Dict[str, float], knob_names: Optional[List[str]] = None) -> Dict[str, float]:
        names = [str(k) for k in ((knob_names or list(knob_values.keys())) or [])]
        self.ensure_machine_origin(names)
        use_values = {str(k): float(knob_values.get(k, 0.0)) for k in names}
        linear_payload, nonlinear_payload, corrector_payload, zscan_payload = self._split_knob_payload(use_values)

        out: Dict[str, float] = {}

        linear_mags = []
        nonlinear_mags = []
        if hasattr(self.interface, "_linear_magnets_for_knobs"):
            linear_mags = list(getattr(self.interface, "_linear_magnets_for_knobs")(list(linear_payload.keys())))
        if hasattr(self.interface, "_nonlinear_magnets_for_knobs"):
            nonlinear_mags = list(getattr(self.interface, "_nonlinear_magnets_for_knobs")(list(nonlinear_payload.keys())))

        linear_delta: Dict[str, Tuple[float, float]] = {}
        nonlinear_delta: Dict[str, float] = {}
        if linear_payload and hasattr(self.interface, "_build_linear_deltas"):
            linear_delta = dict(getattr(self.interface, "_build_linear_deltas")(linear_payload))
        if nonlinear_payload and hasattr(self.interface, "_build_nonlinear_deltas"):
            nonlinear_delta = dict(getattr(self.interface, "_build_nonlinear_deltas")(nonlinear_payload))

        for mag in linear_mags:
            m = str(mag)
            base = dict(self.linear_base_positions.get(m, {}))
            dx, dy = linear_delta.get(m, (0.0, 0.0))
            x0 = float(base.get("x", float("nan")))
            y0 = float(base.get("y", float("nan")))
            out[self._machine_label_linear_x(m)] = float(x0 + float(dx)) if np.isfinite(x0) else float("nan")
            out[self._machine_label_linear_y(m)] = float(y0 + float(dy)) if np.isfinite(y0) else float("nan")

        for mag in nonlinear_mags:
            m = str(mag)
            i0 = float(self.nonlinear_base_currents.get(m, float("nan")))
            di = float(nonlinear_delta.get(m, 0.0))
            out[self._machine_label_current(m)] = float(i0 + di) if np.isfinite(i0) else float("nan")

        for knob, value in corrector_payload.items():
            out[self._machine_label_corrector(str(knob))] = float(value)

        for knob, delta in zscan_payload.items():
            k = str(knob)
            z0 = float(self.zscan_base_values.get(k, float("nan")))
            out[self._machine_label_zscan(k)] = float(z0 + float(delta)) if np.isfinite(z0) else float("nan")

        return out

    def apply_knobs(self, knob_values: Dict[str, float]) -> None:
        if not (self.linear_base_positions or self.nonlinear_base_currents or self.corrector_base_values or self.zscan_base_values):
            self.ensure_machine_origin(list(knob_values.keys()))

        linear_payload, nonlinear_payload, corrector_payload, zscan_payload = self._split_knob_payload(knob_values)

        if linear_payload:
            self.interface.apply_linear_knobs(
                linear_payload,
                base_positions=self.linear_base_positions or None,
            )
        if nonlinear_payload:
            self.interface.apply_nonlinear_knobs(
                nonlinear_payload,
                base_currents=self.nonlinear_base_currents or None,
            )
        if corrector_payload:
            self.interface.apply_corrector_knobs(corrector_payload)
        if zscan_payload:
            self.interface.apply_zscan_knobs(
                zscan_payload,
                scan_mode_label=self.scan_mode_label,
                base_values=self.zscan_base_values or None,
            )

    def get_ipbsm(self) -> Tuple[float, float]:
        return self.interface.get_ipbsm()

    def get_ipbsm_full(self) -> Dict[str, Any]:
        return self.interface.get_ipbsm_full()

from ipbsm_opt_math import (
    GPParams,
    GaussianFitResult,
    SimpleGP,
    acq_ei,
    acq_ucb,
    bootstrap_fit,
    fit_gaussian_from_samples,
)

# ----------------------------
# Optimizer
# ----------------------------

@dataclass
class OptimizerConfig:
    mode_name: str  # "linear", "nonlinear2", "nonlinear4"
    method: str     # "GF", "BO", "LQO", "TRBO"
    acquisition: str  # "UCB" or "EI" (primarily for BO)
    params: List[str]
    bounds: Dict[str, Tuple[float, float]]
    init_sigma: Dict[str, float]
    param_origins: Dict[str, float] = field(default_factory=dict)
    scan_mode_label: str = "30"
    meas_sigma: float = 0.01
    expected_y_max: Optional[float] = None  # e.g. 0.8 for synthetic test
    stop_modulation: Optional[float] = 0.8  # stop immediately if y >= this (GF/BO/LQO)
    knob_step: float = 0.01              # quantization step for ALL knob params (linear/nonlinear)
    param_steps: Dict[str, float] = field(default_factory=dict)  # per-axis quantization step override
    zscan_axis_names: List[str] = field(default_factory=lambda: ["Z scan knob"])
    zscan_method: str = "BO"
    zscan_range: float = 0.0085
    zscan_step: float = 0.001
    gf_weight_peak: float = 1.0          # GF policy weight: peak-seeking (use fitted mu)
    gf_weight_refine: float = 1.0        # GF policy weight: localization / precision improvement
    gf_jitter_frac: float = 0.25         # GF peak-seeking jitter as fraction of sigma
    max_steps: int = 60              # safety cap (mainly for GF axis-sequential mode)
    bo_max_steps: int = 60           # BO/LQO/TRBO step budget
    gf_axis_max_steps: int = 7      # per-axis budget in GF mode
    gf_axis_min_points: int = 3      # per-axis minimum samples before convergence checks
    stop_sigma_ratio: float = 0.20    # GF: stop axis when mu_std <= 1D fitted sigma * ratio
    stop_y_sigma: float = 0.01        # stop when predicted peak y std small (not strict)
    n_init_random: int = 8
    n_candidates: int = 6000
    n_bootstrap: int = 60
    ridge_fit: float = 1e-4
    gp_kernel: str = "matern52"
    gp_length_scale: float = 1.2
    gp_ard_length_scales: Optional[Dict[str, float]] = None
    gp_signal_var: float = 0.15
    gp_noise_var: float = 1e-4
    ucb_beta: float = 2.0
    ei_xi: float = 0.0
    probe_scale: float = 1.0          # for GF probing around peak
    init_strategy: str = "structured"  # "structured" or "random"
    lqo_trust_radius_sigma: float = 1.5
    lqo_min_local_points: int = 12
    lqo_candidates: int = 2000
    bo_stop_on_low_acq: bool = True
    bo_low_acq_threshold: float = 1e-4
    bo_low_acq_patience: int = 2
    average_pause_ratio: float = 0.80  # pause if average < ratio * first average

@dataclass
class StepRecord:
    step: int
    t_iso: str
    x: Dict[str, float]
    y: float
    y_err: float
    chosen_by: str
    dat: Dict[str, Any] = field(default_factory=dict)


DAT_CSV_COLUMNS = [
    "dat_modulation",
    "dat_error",
    "dat_beamsize",
    "dat_ebeamsize",
    "dat_average",
    "dat_phase",
    "dat_filename",
    "dat_ict_average",
]


def _machine_state_col_name(kind: str, channel: str) -> str:
    return f"machine_{kind}[{channel}]"

class StopFlag:
    def __init__(self):
        self._stop = False
    def request_stop(self):
        self._stop = True
    def is_stopped(self) -> bool:
        return self._stop


class GracefulStopRequested(Exception):
    pass

class Optimizer:
    def _is_sequential_method(self) -> bool:
        return str(getattr(self.cfg, "method", "")).upper() in {"GF", "SEQUENTIAL"}

    def _zscan_axis_name_set(self) -> set:
        names = getattr(self.cfg, "zscan_axis_names", None)
        if isinstance(names, (list, tuple, set)) and len(names) > 0:
            return {str(n) for n in names}
        return {"Z scan knob"}

    def _is_zscan_axis(self, axis_name: str) -> bool:
        return str(axis_name) in self._zscan_axis_name_set()

    def _zscan_axis_indices(self) -> List[int]:
        zset = self._zscan_axis_name_set()
        return [i for i, p in enumerate(self.cfg.params) if str(p) in zset]

    def _structured_init_points(self) -> List[np.ndarray]:
        """
        Deterministic-ish initialization around the origin (current knob baseline):
          x0 = 0
          x0 +/- sigma0_i along each axis
        This helps avoid the "all points far from peak => unstable fit" failure.
        """
        d = len(self.cfg.params)
        x0 = np.array([float(self.cfg.param_origins.get(p, 0.0)) for p in self.cfg.params], dtype=float)
        sig0 = np.array([max(1e-6, float(self.cfg.init_sigma.get(p, 0.5))) for p in self.cfg.params], float)
        lo, hi = self._bounds_arrays()
        pts: List[np.ndarray] = []

        def _add_point(xp: np.ndarray) -> None:
            xc = clamp(np.asarray(xp, float), lo, hi)
            for existing in pts:
                if np.linalg.norm(existing - xc) < 1e-12:
                    return
            pts.append(xc)

        _add_point(x0)

        # Ay-Z coupled coarse coverage first so it naturally fits into n_init_random budget.
        ay_idx = next((i for i, name in enumerate(self.cfg.params) if str(name) == "Ay"), None)
        z_idxs = self._zscan_axis_indices()
        if ay_idx is not None and len(z_idxs) > 0:
            ay_center = float(x0[ay_idx])
            ay_sigma = max(1e-6, float(sig0[ay_idx]))
            ay_vals = [ay_center, ay_center + ay_sigma, ay_center - ay_sigma]
            for z_idx in z_idxs:
                z_center = 0.5 * (float(lo[z_idx]) + float(hi[z_idx]))
                z_vals = [z_center, float(hi[z_idx]), float(lo[z_idx])]
                for z_val in z_vals:
                    for ay_val in ay_vals:
                        x_ayz = x0.copy()
                        x_ayz[z_idx] = float(z_val)
                        x_ayz[ay_idx] = float(ay_val)
                        _add_point(x_ayz)

        for i in range(d):
            axis_name = self.cfg.params[i]
            if self._is_zscan_axis(axis_name):
                x_center = x0.copy()
                x_center[i] = 0.5 * (float(lo[i]) + float(hi[i]))
                _add_point(x_center)
                xp = x_center.copy(); xp[i] = float(hi[i])
                xm = x_center.copy(); xm[i] = float(lo[i])
            else:
                xp = x0.copy(); xp[i] += sig0[i]
                xm = x0.copy(); xm[i] -= sig0[i]
            _add_point(xp)
            _add_point(xm)
        return pts

    def __init__(
        self,
        controller: BaseIPBSMController,
        config: OptimizerConfig,
        out_dir: Path,
        progress_cb: Optional[Callable[[int, Dict], None]] = None,
        stop_flag: Optional[StopFlag] = None,
        pause_hook: Optional[Callable[[Dict[str, Any]], bool]] = None,
        warm_start_data: Optional[List[Dict[str, Any]]] = None,
    ):
        self.controller = controller
        self.cfg = config
        self.out_dir = ensure_dir(out_dir)
        self.progress_cb = progress_cb
        self.stop_flag = stop_flag or StopFlag()
        self.pause_hook = pause_hook
        self.rng = np.random.default_rng()
        self._manual_pause_requested = False
        self.run_tag = now_tag()
        self.measurements_csv_path = self.out_dir / f"measurements-{self.run_tag}.csv"
        self.machine_origin_path = self.out_dir / "machine_origin.json"
        self.machine_origin_tagged_path = self.out_dir / f"machine_origin-{self.run_tag}.json"

        self.X = []
        self.y = []
        self.yerr = []
        self.records: List[StepRecord] = []
        self._last_dat: Dict[str, Any] = {}
        self._last_measure_reused: bool = False
        self._last_reuse_from_step: Optional[int] = None
        self._gf_cycle_first_side: Dict[int, str] = {}
        self._average_baseline: Optional[float] = None
        self._low_acq_streak = 0
        self._final_target_x: Dict[str, float] = {}
        self._final_target_y: float = float("nan")
        ratio = float(getattr(self.cfg, "average_pause_ratio", 0.80))
        self._average_pause_ratio = min(1.0, max(0.0, ratio))
        self._machine_state_channels: List[str] = []
        self._machine_state_init_values: Dict[str, float] = {}
        self._init_machine_state_tracking()

        if warm_start_data:
            self._load_warm_start_data(warm_start_data)
        self._save_machine_origin()
        self._save_config()
        if self.records:
            self._rewrite_measurements_csv()

    def request_manual_pause(self) -> None:
        self._manual_pause_requested = True

    def _save_config(self):
        cfg_path = self.out_dir / "config.json"
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(asdict(self.cfg), f, indent=2, ensure_ascii=False)

    def _save_machine_origin(self):
        baseline_state = None
        if hasattr(self.controller, "ensure_machine_origin"):
            try:
                baseline_state = self.controller.ensure_machine_origin(self.cfg.params)
            except Exception:
                baseline_state = None
        if not baseline_state:
            return
        for path in (self.machine_origin_path, self.machine_origin_tagged_path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(baseline_state, f, indent=2, ensure_ascii=False)

    def _step_for_param(self, param_name: str) -> float:
        param_steps = getattr(self.cfg, "param_steps", {}) or {}
        step = float(param_steps.get(param_name, self.cfg.knob_step))
        return max(1e-12, abs(step))

    def _step_array(self) -> np.ndarray:
        return np.array([self._step_for_param(p) for p in self.cfg.params], dtype=float)

    def _quantize_knob(self, v: float, param_name: str) -> float:
        step = self._step_for_param(param_name)
        return round(float(v) / step) * step

    def _quantize_x_vec(self, x_vec: np.ndarray) -> np.ndarray:
        lo, hi = self._bounds_arrays()
        x_arr = np.asarray(x_vec, float)
        x_q = np.array([self._quantize_knob(x_arr[i], self.cfg.params[i]) for i in range(len(self.cfg.params))], float)
        return clamp(x_q, lo, hi)

    def _quantize_x_dict(self, x_map: Dict[str, float]) -> Dict[str, float]:
        return {p: self._quantize_knob(float(x_map[p]), p) for p in self.cfg.params if p in x_map}

    def _init_machine_state_tracking(self) -> None:
        self._machine_state_channels = []
        self._machine_state_init_values = {}

        describe_fn = getattr(self.controller, "describe_machine_setpoint_channels", None)
        if callable(describe_fn):
            try:
                info = describe_fn(list(self.cfg.params))
                channels = list(info.get("channels", [])) if isinstance(info, dict) else []
                initial = dict(info.get("initial", {})) if isinstance(info, dict) else {}
                if channels:
                    self._machine_state_channels = [str(ch) for ch in channels]
                    self._machine_state_init_values = {
                        str(ch): float(initial.get(ch, float("nan"))) for ch in self._machine_state_channels
                    }
                    return
            except Exception:
                pass

        # Fallback for mock controllers: use knob values as synthetic channels.
        self._machine_state_channels = [f"knob:{p}" for p in self.cfg.params]
        self._machine_state_init_values = {
            f"knob:{p}": float(self.cfg.param_origins.get(p, 0.0)) for p in self.cfg.params
        }

    def _machine_values_from_knobs(self, knob_values: Dict[str, float]) -> Dict[str, float]:
        calc_fn = getattr(self.controller, "compute_machine_setpoint_values", None)
        if callable(calc_fn):
            try:
                out = calc_fn(dict(knob_values), knob_names=list(self.cfg.params))
                return {str(k): float(v) for k, v in dict(out or {}).items()}
            except Exception:
                pass
        out: Dict[str, float] = {}
        for p in self.cfg.params:
            out[f"knob:{p}"] = float(knob_values.get(p, self.cfg.param_origins.get(p, 0.0)))
        return out

    def _log_step(self, rec: StepRecord):
        self._attach_machine_state_to_record(rec)
        self.records.append(rec)
        header = (
            ["step", "t_iso"]
            + self.cfg.params
            + ["modulation", "mod_err"]
            + DAT_CSV_COLUMNS
            + self._machine_state_csv_columns()
            + ["chosen_by"]
        )
        row = self._csv_row_for_record(rec)
        csv_path = self.measurements_csv_path
        is_new = not csv_path.exists()
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(header)
            w.writerow(row)
        if self._is_sequential_method():
            axis_name = self._gf_axis_name_from_chosen_by(str(rec.chosen_by))
            if axis_name:
                try:
                    self._save_gf_dat_exports(axis_names=[axis_name])
                except Exception as exc:
                    self._emit(rec.step, {
                        "phase": "warn",
                        "reason": "gf_dat_export_failed",
                        "message": str(exc),
                        "axis": axis_name,
                    })

    def _machine_state_csv_columns(self) -> List[str]:
        cols: List[str] = []
        for ch in self._machine_state_channels:
            cols.append(_machine_state_col_name("init", ch))
            cols.append(_machine_state_col_name("current", ch))
            cols.append(_machine_state_col_name("final", ch))
        return cols

    def _machine_state_maps(self, rec: StepRecord) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, float]]:
        init_vals: Dict[str, float] = dict(self._machine_state_init_values)
        for p in self.cfg.params:
            init_vals.setdefault(f"knob:{p}", float(self.cfg.param_origins.get(p, 0.0)))
        cur_knobs = {
            p: float(self._quantize_knob(rec.x.get(p, self.cfg.param_origins.get(p, 0.0)), p))
            for p in self.cfg.params
        }
        cur_vals: Dict[str, float] = self._machine_values_from_knobs(cur_knobs)
        final_vals: Dict[str, float] = dict(cur_vals)

        if self._final_target_x:
            final_vals = self._machine_values_from_knobs(dict(self._final_target_x))
        elif len(self.X) > 0 and len(self.y) == len(self.X):
            y_arr = np.asarray(self.y, float).reshape(-1)
            finite_idx = np.where(np.isfinite(y_arr))[0]
            if finite_idx.size > 0:
                best_idx = int(finite_idx[int(np.argmax(y_arr[finite_idx]))])
                if 0 <= best_idx < len(self.X):
                    best_x = self._x_dict(self._quantize_x_vec(np.asarray(self.X[best_idx], float)))
                    final_vals = self._machine_values_from_knobs(best_x)

        keys = list(self._machine_state_channels or [])
        if not keys:
            keys = sorted(set(init_vals.keys()) | set(cur_vals.keys()) | set(final_vals.keys()))
            self._machine_state_channels = list(keys)
        init_out = {k: float(init_vals.get(k, float("nan"))) for k in keys}
        cur_out = {k: float(cur_vals.get(k, float("nan"))) for k in keys}
        fin_out = {k: float(final_vals.get(k, float("nan"))) for k in keys}
        return init_out, cur_out, fin_out

    def _attach_machine_state_to_record(self, rec: StepRecord) -> None:
        init_vals, cur_vals, final_vals = self._machine_state_maps(rec)
        if rec.dat is None:
            rec.dat = {}
        rec.dat["machine_init_values"] = dict(init_vals)
        rec.dat["machine_current_values"] = dict(cur_vals)
        rec.dat["machine_final_values"] = dict(final_vals)

    def _machine_state_row_values(self, rec: StepRecord) -> List[Any]:
        dat = dict(getattr(rec, "dat", {}) or {})
        init_vals = dat.get("machine_init_values", {})
        cur_vals = dat.get("machine_current_values", {})
        final_vals = dat.get("machine_final_values", {})

        row: List[Any] = []
        for ch in self._machine_state_channels:
            init_v = init_vals.get(ch, float("nan"))
            cur_v = cur_vals.get(ch, float("nan"))
            final_v = final_vals.get(ch, cur_v)
            row.extend([init_v, cur_v, final_v])
        return row

    def _csv_row_for_record(self, rec: StepRecord) -> List[Any]:
        x_q = [self._quantize_knob(rec.x[p], p) for p in self.cfg.params]
        return (
            [rec.step, rec.t_iso]
            + x_q
            + [
                rec.y,
                rec.y_err,
                rec.dat.get("modulation", float("nan")),
                rec.dat.get("error", float("nan")),
                rec.dat.get("beamsize", float("nan")),
                rec.dat.get("ebeamsize", float("nan")),
                rec.dat.get("average", float("nan")),
                rec.dat.get("phase", float("nan")),
                rec.dat.get("filename", ""),
                rec.dat.get("ict_average", float("nan")),
            ]
            + self._machine_state_row_values(rec)
            + [
                rec.chosen_by,
            ]
        )

    def _rewrite_measurements_csv(self) -> None:
        header = (
            ["step", "t_iso"]
            + self.cfg.params
            + ["modulation", "mod_err"]
            + DAT_CSV_COLUMNS
            + self._machine_state_csv_columns()
            + ["chosen_by"]
        )
        csv_path = self.measurements_csv_path
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            for rec in self.records:
                self._attach_machine_state_to_record(rec)
                w.writerow(self._csv_row_for_record(rec))

    def _format_autoscan_filename(self, rec: StepRecord) -> str:
        t_iso = str(getattr(rec, "t_iso", "") or "")
        digits = "".join(ch for ch in t_iso if ch.isdigit())
        if len(digits) >= 14:
            stamp = f"{digits[:8]}_{digits[8:14]}"
        else:
            stamp = self.run_tag.replace("-", "_")
        return f"AutoScan_{stamp}_s{int(rec.step):03d}.binary"

    def _gf_file_tag(self) -> str:
        digits = "".join(ch for ch in self.run_tag if ch.isdigit())
        if len(digits) >= 14:
            return f"{digits[2:8]}_{digits[8:14]}"
        if len(self.run_tag) >= 2:
            return self.run_tag[2:].replace("-", "_")
        return self.run_tag.replace("-", "_")

    def _gf_axis_name_from_chosen_by(self, chosen_by: str) -> str:
        text = str(chosen_by or "")
        if not text.startswith("GF["):
            return ""
        end = text.find("]")
        if end <= 3:
            return ""
        return text[3:end]

    def _gf_axis_file_label(self, axis_name: str) -> str:
        label = "".join(ch if ch.isalnum() else "_" for ch in str(axis_name)).strip("_")
        return label or "axis"

    def _gf_dat_title_text(self, axis_name: str, mode_label: str) -> str:
        return f"{axis_name} {mode_label} scan [Auto Scan]"

    def _gf_dat_file_stem(self, axis_name: str, mode_label: str) -> str:
        # Keep edge underscores so "[Auto Scan]" naturally becomes "__Auto_Scan_".
        title_text = self._gf_dat_title_text(axis_name=axis_name, mode_label=mode_label)
        stem = "".join(ch if ch.isalnum() else "_" for ch in title_text)
        if any(ch.isalnum() for ch in stem):
            return stem
        return f"{self._gf_axis_file_label(axis_name)}_{mode_label}_scan__Auto_Scan_"

    def _gf_dat_export_paths(self, axis_names: Optional[List[str]] = None) -> List[str]:
        if not self._is_sequential_method():
            return []
        mode_label = str(getattr(self.cfg, "scan_mode_label", "") or "30")
        year = self.run_tag[:4] if len(self.run_tag) >= 4 and self.run_tag[:4].isdigit() else _dt.datetime.now().strftime("%Y")
        export_dir = Path("/atf/data/ipbsm") / year
        target_axes = [a for a in (axis_names or self.cfg.params) if a in self.cfg.params]
        file_tag = self._gf_file_tag()
        return [str(export_dir / f"{self._gf_dat_file_stem(axis_name=axis_name, mode_label=mode_label)}-{file_tag}.dat") for axis_name in target_axes]

    def _save_gf_dat_exports(self, axis_names: Optional[List[str]] = None) -> List[str]:
        if not self._is_sequential_method():
            return []

        mode_label = str(getattr(self.cfg, "scan_mode_label", "") or "30")
        title_mode = mode_label
        year = self.run_tag[:4] if len(self.run_tag) >= 4 and self.run_tag[:4].isdigit() else _dt.datetime.now().strftime("%Y")
        export_dir = ensure_dir(Path("/atf/data/ipbsm/plot") / year)
        saved_paths: List[str] = []

        target_axes = [a for a in (axis_names or self.cfg.params) if a in self.cfg.params]
        for axis_name in target_axes:
            axis_records = [rec for rec in self.records if str(rec.chosen_by).startswith(f"GF[{axis_name}]")]
            if not axis_records:
                continue

            file_tag = self._gf_file_tag()
            title_text = self._gf_dat_title_text(axis_name=axis_name, mode_label=title_mode)
            file_stem = self._gf_dat_file_stem(axis_name=axis_name, mode_label=title_mode)
            out_path = export_dir / f"{file_stem}-{file_tag}.dat"
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("Version 2024A\n")
                f.write("//\n")
                f.write("Fit Gaussian\n")
                f.write("SelectX C\n")
                f.write("selectY D\n")
                f.write("selectYE E\n")
                f.write(f'Title "{title_text}"\n')
                f.write(f'Xlabel "{axis_name}"\n')
                f.write('Ylabel "Modulation"\n')
                f.write("Xauto ON -1.0,1.0\n")
                f.write("Yauto ON -1.0,1.0\n")
                f.write("Header mode,<signal>,X,Modulation,Mod. Error,BeamSize,BS. error,Filename,\n")
                for rec in axis_records:
                    dat = dict(getattr(rec, "dat", {}) or {})
                    signal = float(dat.get("average", float("nan")))
                    x_val = float(rec.x.get(axis_name, float("nan")))
                    mod = float(rec.y)
                    mod_err = float(rec.y_err)
                    beamsize = float(dat.get("beamsize", float("nan")))
                    ebeamsize = float(dat.get("ebeamsize", float("nan")))
                    row_filename = self._format_autoscan_filename(rec)

                    def _fmt(val: float, ndig: int) -> str:
                        if not np.isfinite(val):
                            return ""
                        return f"{val:.{ndig}f}"

                    f.write(
                        "Row unmasked "
                        f"{axis_name},"
                        f"{_fmt(signal, 1)},"
                        f"{_fmt(x_val, 3)},"
                        f"{_fmt(mod, 3)},"
                        f"{_fmt(mod_err, 3)},"
                        f"{_fmt(beamsize, 1)},"
                        f"{_fmt(ebeamsize, 1)},"
                        f"{row_filename},\n"
                    )
                f.write("END\n")
            saved_paths.append(str(out_path))

        return saved_paths

    def _load_warm_start_data(self, rows: List[Dict[str, Any]]) -> None:
        for item in rows:
            x = {p: float(item["x"][p]) for p in self.cfg.params}
            rec = StepRecord(
                step=int(item.get("step", len(self.records) + 1)),
                t_iso=str(item.get("t_iso", "")),
                x=x,
                y=float(item.get("y", float("nan"))),
                y_err=float(item.get("y_err", float("nan"))),
                chosen_by=str(item.get("chosen_by", "warm_start")),
                dat=dict(item.get("dat", {})),
            )
            self.records.append(rec)
            self.X.append(np.array([x[p] for p in self.cfg.params], float))
            self.y.append(float(rec.y))
            self.yerr.append(float(rec.y_err))
            self._last_dat = dict(rec.dat)


    def _emit(self, step: int, info: Dict):
        if self.progress_cb:
            self.progress_cb(step, info)

    def _bounds_arrays(self) -> Tuple[np.ndarray, np.ndarray]:
        lo = np.array([self.cfg.bounds[p][0] for p in self.cfg.params], float)
        hi = np.array([self.cfg.bounds[p][1] for p in self.cfg.params], float)
        return lo, hi

    def _gf_expand_axis_bounds(self, axis_name: str, target_sigma_mult: float) -> Dict[str, float]:
        if axis_name not in self.cfg.params:
            return {"changed": 0.0}
        axis_idx = self.cfg.params.index(axis_name)
        sigma0 = max(1e-6, float(self.cfg.init_sigma.get(axis_name, 0.5)))
        target_span = max(0.0, float(target_sigma_mult)) * sigma0
        cur_lo, cur_hi = self.cfg.bounds.get(axis_name, (float("nan"), float("nan")))
        if (not np.isfinite(cur_lo)) or (not np.isfinite(cur_hi)) or cur_lo >= cur_hi:
            return {"changed": 0.0}
        center = 0.5 * (float(cur_lo) + float(cur_hi))
        cur_span = max(abs(float(cur_hi) - center), abs(center - float(cur_lo)))
        new_span = max(cur_span, target_span)
        if abs(new_span - cur_span) <= 1e-12:
            return {
                "changed": 0.0,
                "old_lo": float(cur_lo),
                "old_hi": float(cur_hi),
                "new_lo": float(cur_lo),
                "new_hi": float(cur_hi),
                "center": float(center),
                "old_span_sigma": float(cur_span / sigma0),
                "new_span_sigma": float(cur_span / sigma0),
            }
        step = self._step_for_param(self.cfg.params[axis_idx])
        new_lo = round((center - new_span) / step) * step
        new_hi = round((center + new_span) / step) * step
        if new_lo >= new_hi:
            new_lo = float(cur_lo)
            new_hi = float(cur_hi)
        self.cfg.bounds[axis_name] = (float(new_lo), float(new_hi))
        return {
            "changed": 1.0,
            "old_lo": float(cur_lo),
            "old_hi": float(cur_hi),
            "new_lo": float(new_lo),
            "new_hi": float(new_hi),
            "center": float(center),
            "old_span_sigma": float(cur_span / sigma0),
            "new_span_sigma": float(max(abs(float(new_hi) - center), abs(center - float(new_lo))) / sigma0),
        }

    def _clamp_with_warn(self, x: np.ndarray, lo: np.ndarray, hi: np.ndarray, *, step: int, context: str, axis: str = "") -> np.ndarray:
        xr = np.asarray(x, float)
        xc = clamp(xr, lo, hi)
        hit = np.where(np.abs(xc - xr) > 1e-12)[0]
        if hit.size > 0:
            hit_params = [self.cfg.params[int(i)] for i in hit.tolist()]
            self._emit(step, {
                "phase": "warn",
                "reason": "bounds_clamp",
                "context": context,
                "axis": axis,
                "hit_params": hit_params,
            })
            print(f"[WARN] bounds_clamp context={context} axis={axis} hit={hit_params}")
        return xc

    def _x_dict(self, x_vec: np.ndarray) -> Dict[str, float]:
        return {p: float(x_vec[i]) for i, p in enumerate(self.cfg.params)}

    def _is_duplicate_quantized_point(self, x_q: np.ndarray) -> bool:
        if len(self.X) == 0:
            return False
        x_q = self._quantize_x_vec(np.asarray(x_q, float))
        X_prev = np.asarray(self.X, float)
        steps = self._step_array()
        X_prev_q = np.round(X_prev / steps.reshape(1, -1)) * steps.reshape(1, -1)
        lo, hi = self._bounds_arrays()
        X_prev_q = clamp(X_prev_q, lo.reshape(1, -1), hi.reshape(1, -1))
        d = np.linalg.norm(X_prev_q - x_q.reshape(1, -1), axis=1)
        return bool(np.any(d < 1e-12))

    def _avoid_duplicate_gf_point(self, x_vec: np.ndarray, axis_idx: int) -> np.ndarray:
        x_q = self._quantize_x_vec(np.asarray(x_vec, float))
        if not self._is_duplicate_quantized_point(x_q):
            return x_q

        lo, hi = self._bounds_arrays()
        step = self._step_for_param(self.cfg.params[axis_idx])
        base = float(x_q[axis_idx])
        max_k = int(np.ceil((hi[axis_idx] - lo[axis_idx]) / step)) + 2

        for k in range(1, max_k + 1):
            for sign in (+1.0, -1.0):
                cand = x_q.copy()
                cand[axis_idx] = base + sign * k * step
                cand = self._quantize_x_vec(cand)
                if abs(float(cand[axis_idx]) - base) < 0.5 * step:
                    continue
                if not self._is_duplicate_quantized_point(cand):
                    return cand
        return x_q

    def _estimate_axis_mu_sigma_1d(self, axis_name: str) -> Optional[Tuple[float, float]]:
        fit_1d = self._fit_gf_axis_1d(axis_name)
        if fit_1d is None:
            return None
        return float(fit_1d["mu"]), float(fit_1d["sigma"])

    def _gf_axis_xy(self, axis_name: str) -> Tuple[np.ndarray, np.ndarray]:
        xs = []
        ys = []
        key = f"GF[{axis_name}]"
        for rec in self.records:
            cb = str(rec.chosen_by)
            if not cb.startswith(key):
                continue
            try:
                xs.append(float(rec.x[axis_name]))
                ys.append(float(rec.y))
            except Exception:
                continue
        return np.asarray(xs, float), np.asarray(ys, float)

    def _fit_gf_axis_1d(self, axis_name: str) -> Optional[Dict[str, float]]:
        x, y = self._gf_axis_xy(axis_name)
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
            "sigma": float(sigma),
            "amp": float(amp),
            "residual_rms": float(resid_rms),
            "n_points": int(x.size),
        }

    def _bootstrap_gf_axis_1d(self, axis_name: str) -> Dict[str, float]:
        x, y = self._gf_axis_xy(axis_name)
        n = int(x.size)
        if n < 3:
            return {
                "mu_mean": float("nan"),
                "mu_std": float("nan"),
                "sigma_mean": float("nan"),
                "sigma_std": float("nan"),
                "ok_count": 0,
            }
        mus = []
        sigmas = []
        for _ in range(max(0, int(self.cfg.n_bootstrap))):
            idx = self.rng.integers(0, n, size=n)
            fit = self._fit_gf_axis_1d_from_xy(x[idx], y[idx])
            if fit is None:
                continue
            mus.append(float(fit["mu"]))
            sigmas.append(float(fit["sigma"]))
        if not mus:
            return {
                "mu_mean": float("nan"),
                "mu_std": float("nan"),
                "sigma_mean": float("nan"),
                "sigma_std": float("nan"),
                "ok_count": 0,
            }
        mu_arr = np.asarray(mus, float)
        sigma_arr = np.asarray(sigmas, float)
        return {
            "mu_mean": float(np.mean(mu_arr)),
            "mu_std": float(np.std(mu_arr, ddof=1)) if mu_arr.size > 1 else 0.0,
            "sigma_mean": float(np.mean(sigma_arr)),
            "sigma_std": float(np.std(sigma_arr, ddof=1)) if sigma_arr.size > 1 else 0.0,
            "ok_count": int(mu_arr.size),
        }

    def _fit_gf_axis_1d_from_xy(self, x: np.ndarray, y: np.ndarray) -> Optional[Dict[str, float]]:
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
            "sigma": float(sigma),
            "amp": float(amp),
            "residual_rms": float(resid_rms),
            "n_points": int(x.size),
        }

    def _gf_recent_axis_sigmas(self, axis_name: str, max_count: int = 2) -> List[float]:
        recs = self._gf_axis_records(axis_name)
        sigmas: List[float] = []
        for end in range(3, len(recs) + 1):
            subset = recs[:end]
            xs = []
            ys = []
            for rec in subset:
                try:
                    xs.append(float(rec.x[axis_name]))
                    ys.append(float(rec.y))
                except Exception:
                    continue
            if len(xs) < 3:
                continue
            x = np.asarray(xs, float)
            y = np.asarray(ys, float)
            y_clip = np.clip(y, 1e-6, None)
            try:
                a, b, _ = np.polyfit(x, np.log(y_clip), deg=2)
            except Exception:
                continue
            if (not np.isfinite(a)) or (a >= -1e-12):
                continue
            sigma2 = -1.0 / (2.0 * a)
            if (not np.isfinite(sigma2)) or sigma2 <= 0.0:
                continue
            sigmas.append(float(np.sqrt(sigma2)))
        if max_count <= 0:
            return sigmas
        return sigmas[-max_count:]

    def _count_axis_side_samples(self, axis_name: str, mu_axis: float) -> Tuple[int, int]:
        plus_cnt = 0
        minus_cnt = 0
        key = f"GF[{axis_name}]"
        for rec in self.records:
            cb = str(rec.chosen_by)
            if not cb.startswith(key):
                continue
            try:
                xv = float(rec.x[axis_name])
            except Exception:
                continue
            if xv > mu_axis:
                plus_cnt += 1
            elif xv < mu_axis:
                minus_cnt += 1
        return plus_cnt, minus_cnt

    def _gf_axis_records(self, axis_name: str) -> List[StepRecord]:
        key = f"GF[{axis_name}]_"
        return [rec for rec in self.records if str(rec.chosen_by).startswith(key)]

    def _gf_axis_tag(self, chosen_by: str, axis_name: str) -> str:
        prefix = f"GF[{axis_name}]_"
        text = str(chosen_by)
        if text.startswith(prefix):
            return text[len(prefix):]
        return text

    def _build_gf_resume_state(self) -> Dict[str, Any]:
        x_fixed = np.array([float(self.cfg.param_origins.get(p, 0.0)) for p in self.cfg.params], dtype=float)
        axis_states: List[Dict[str, Any]] = []
        next_axis_idx = len(self.cfg.params)
        for axis_idx, axis_name in enumerate(self.cfg.params):
            recs = self._gf_axis_records(axis_name)
            tags = [self._gf_axis_tag(rec.chosen_by, axis_name) for rec in recs]
            final_recs = [rec for rec, tag in zip(recs, tags) if tag == "final"]
            done = bool(final_recs)
            if done:
                x_fixed[axis_idx] = float(final_recs[-1].x.get(axis_name, 0.0))
            init_tags_done = {tag for tag in tags if tag in {"init0", "init+", "init-"}}
            loop_tags = [tag for tag in tags if tag not in {"init0", "init+", "init-", "final"}]
            axis_states.append({
                "done": done,
                "init_tags_done": init_tags_done,
                "axis_steps": len(recs),
                "cycle_idx": len(loop_tags),
                "last_y": float(recs[-1].y) if recs else None,
            })
            if (not done) and next_axis_idx == len(self.cfg.params):
                next_axis_idx = axis_idx
        return {
            "x_fixed": x_fixed,
            "axis_states": axis_states,
            "next_axis_idx": next_axis_idx,
        }

    def _measure_at(self, x_vec: np.ndarray, chosen_by: str) -> Tuple[float, float]:
        # Quantize knobs to hardware-like step and clamp
        lo, hi = self._bounds_arrays()
        self._last_measure_reused = False
        self._last_reuse_from_step = None

        xq = self._quantize_x_vec(np.asarray(x_vec, float))

        meas_idx = len(self.X) + 1  # <- これが「今何回目の測定か」
        best_now = float(np.max(self.y)) if self.y else float("nan")
        self._emit(meas_idx, {
            "phase": "measuring",
            "chosen_by": chosen_by,
            "x": self._x_dict(xq),
            "best_y": best_now,
        })

        # Reuse previously measured point (skip PV IO) if exactly same quantized setpoint.
        if len(self.X) > 0:
            X_prev = np.asarray(self.X, float)
            steps = self._step_array()
            X_prev_q = np.round(X_prev / steps.reshape(1, -1)) * steps.reshape(1, -1)
            X_prev_q = clamp(X_prev_q, lo.reshape(1, -1), hi.reshape(1, -1))
            d = np.linalg.norm(X_prev_q - xq.reshape(1, -1), axis=1)
            hit = np.where(d < 1e-12)[0]
            if hit.size > 0:
                j = int(hit[0])
                self._last_measure_reused = True
                self._last_reuse_from_step = int(j + 1)
                y = float(self.y[j])
                yerr = float(self.yerr[j])
                if j < len(self.records):
                    self._last_dat = dict(self.records[j].dat)
                self._emit(meas_idx, {
                    "phase": "reuse",
                    "chosen_by": chosen_by,
                    "x": self._x_dict(xq),
                    "y": y,
                    "y_err": yerr,
                    "best_y": best_now,
                    "reuse_from_step": j + 1,
                })
                print(f"[REUSE] i={meas_idx} by={chosen_by} <- step={j+1} y={y:.6f} err={yerr:.6f}")
                return y, yerr

        while True:
            try:
                self.controller.apply_knobs(self._x_dict(xq))
                break
            except CurrentDropToZeroError as exc:
                payload = {
                    "reason": "current_drop_to_zero",
                    "step": meas_idx,
                    "x": self._x_dict(xq),
                    "magnets": list(getattr(exc, "magnets", [])),
                    "target": dict(getattr(exc, "target", {})),
                    "readback": dict(getattr(exc, "readback", {})),
                    "message": str(exc),
                }
                should_continue = True
                if self.pause_hook is not None:
                    should_continue = bool(self.pause_hook(payload))
                if not should_continue:
                    self.stop_flag.request_stop()
                    raise GracefulStopRequested("current_drop_to_zero") from exc
            except Exception as exc:
                payload = {
                    "reason": "operation_error",
                    "operation": "apply_knobs",
                    "step": meas_idx,
                    "x": self._x_dict(xq),
                    "message": str(exc),
                    "error_type": exc.__class__.__name__,
                }
                should_continue = True
                if self.pause_hook is not None:
                    should_continue = bool(self.pause_hook(payload))
                if not should_continue:
                    self.stop_flag.request_stop()
                    raise GracefulStopRequested("apply_knobs_error") from exc

        while True:
            try:
                dat_raw: Dict[str, Any] = {}
                if hasattr(self.controller, "get_ipbsm_full"):
                    dat_raw = self.controller.get_ipbsm_full()
                else:
                    y0, yerr0 = self.controller.get_ipbsm()
                    dat_raw = {"modulation": y0, "error": yerr0}
                break
            except Exception as exc:
                payload = {
                    "reason": "operation_error",
                    "operation": "get_ipbsm",
                    "step": meas_idx,
                    "x": self._x_dict(xq),
                    "message": str(exc),
                    "error_type": exc.__class__.__name__,
                }
                should_continue = True
                if self.pause_hook is not None:
                    should_continue = bool(self.pause_hook(payload))
                if not should_continue:
                    self.stop_flag.request_stop()
                    raise GracefulStopRequested("get_ipbsm_error") from exc

        def _f(v: Any) -> float:
            try:
                return float(v)
            except Exception:
                return float("nan")

        dat = {
            "modulation": _f(dat_raw.get("modulation", float("nan"))),
            "error": abs(_f(dat_raw.get("error", float("nan")))),
            "beamsize": _f(dat_raw.get("beamsize", float("nan"))),
            "ebeamsize": _f(dat_raw.get("ebeamsize", float("nan"))),
            "average": _f(dat_raw.get("average", float("nan"))),
            "phase": _f(dat_raw.get("phase", float("nan"))),
            "filename": str(dat_raw.get("filename", "")),
            "ict_average": _f(dat_raw.get("ict_average", float("nan"))),
        }
        self._last_dat = dat

        y = float(dat["modulation"])
        yerr = float(dat["error"])

        avg = dat["average"]
        if np.isfinite(avg):
            if self._average_baseline is None:
                self._average_baseline = float(avg)
            else:
                threshold = self._average_pause_ratio * self._average_baseline
                if avg < threshold:
                    payload = {
                        "reason": "average_below_threshold",
                        "step": meas_idx,
                        "average": float(avg),
                        "baseline_average": float(self._average_baseline),
                        "threshold_average": float(threshold),
                        "ratio": float(avg / self._average_baseline) if self._average_baseline else float("nan"),
                    }
                    should_continue = True
                    if self.pause_hook is not None:
                        should_continue = bool(self.pause_hook(payload))
                    if not should_continue:
                        self.stop_flag.request_stop()

        best = max([float(y)] + [float(v) for v in self.y]) if self.y else float(y)
        print(f"[MEAS] i={meas_idx} by={chosen_by}   y={float(y):.6f} ± {float(yerr):.6f}  best={best:.6f}")

        # Manual pause request from GUI: stop only after current measurement has completed.
        if self._manual_pause_requested:
            self._manual_pause_requested = False
            payload = {
                "reason": "manual_pause",
                "step": meas_idx,
                "x": self._x_dict(xq),
                "y": float(y),
                "y_err": float(yerr),
                "best_y": float(best),
            }
            should_continue = True
            if self.pause_hook is not None:
                should_continue = bool(self.pause_hook(payload))
            if not should_continue:
                self.stop_flag.request_stop()

        return float(y), float(yerr)

    def _random_point(self) -> np.ndarray:
        lo, hi = self._bounds_arrays()
        return lo + (hi - lo) * self.rng.random(lo.shape[0])

    def _candidate_points(self, n: int) -> np.ndarray:
        lo, hi = self._bounds_arrays()
        return lo + (hi - lo) * self.rng.random((n, lo.shape[0]))

    def _candidate_points_in_box(self, center: np.ndarray, radius: np.ndarray, n: int) -> np.ndarray:
        lo, hi = self._bounds_arrays()
        center = np.asarray(center, float).reshape(-1)
        radius = np.asarray(radius, float).reshape(-1)
        box_lo = np.maximum(lo, center - radius)
        box_hi = np.minimum(hi, center + radius)
        return box_lo.reshape(1, -1) + (box_hi - box_lo).reshape(1, -1) * self.rng.random((n, lo.shape[0]))

    def _gp_length_scales(self) -> np.ndarray:
        cfg_ls = getattr(self.cfg, "gp_ard_length_scales", None)
        if isinstance(cfg_ls, dict) and cfg_ls:
            vals = [float(cfg_ls.get(p, self.cfg.init_sigma.get(p, self.cfg.gp_length_scale))) for p in self.cfg.params]
            return np.maximum(np.asarray(vals, float), 1e-6)
        if cfg_ls is not None:
            arr = np.asarray(cfg_ls, float).reshape(-1)
            if arr.size == 1:
                return np.full(len(self.cfg.params), float(arr[0]), dtype=float)
            if arr.size == len(self.cfg.params):
                return np.maximum(arr.astype(float), 1e-6)
        vals = [float(self.cfg.init_sigma.get(p, self.cfg.gp_length_scale)) for p in self.cfg.params]
        return np.maximum(np.asarray(vals, float), 1e-6)

    def _fit_and_bootstrap(self) -> Dict:
        mode = "diag" if self.cfg.mode_name == "linear" else "full"
        X = np.asarray(self.X, float)
        y = np.asarray(self.y, float)

        fit = fit_gaussian_from_samples(X, y, mode=mode, ridge=self.cfg.ridge_fit, y_cap=self.cfg.expected_y_max)

        # Stabilize: clamp mu to bounds and covariance diagonal to reasonable range
        lo, hi = self._bounds_arrays()
        mu = np.array(fit.mu, float)
        mu = clamp(mu, lo, hi)
        cov = np.array(fit.cov, float)
        cov = 0.5 * (cov + cov.T)

        sig0 = np.array([max(1e-6, float(self.cfg.init_sigma.get(p, 0.5))) for p in self.cfg.params], float)
        min_sig = np.maximum(0.05, 0.2 * sig0)
        max_sig = np.minimum(5.0, 3.0 * sig0 + 1.0)

        diag = np.diag(cov)
        diag = np.clip(diag, min_sig**2, max_sig**2)
        cov = cov.copy()
        for i in range(len(diag)):
            cov[i, i] = diag[i]

        fit.mu = mu.tolist()
        fit.cov = cov.tolist()

        boot = bootstrap_fit(X, y, mode=mode, ridge=self.cfg.ridge_fit, n_boot=self.cfg.n_bootstrap, rng=self.rng)

        out = {
            "fit": fit,
            "boot": boot,
        }
        # Save summary
        summary = {
            "fit": asdict(fit),
            "boot": {
                "mu_mean": boot["mu_mean"].tolist(),
                "mu_std": boot["mu_std"].tolist(),
                "cov_diag_mean": boot["cov_diag_mean"].tolist(),
                "cov_diag_std": boot["cov_diag_std"].tolist(),
            }
        }
        with open(self.out_dir / "fit_summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        return out

    def _fit_and_bootstrap_gf_1d(self) -> Dict:
        d = len(self.cfg.params)
        mu = np.array([float(self.cfg.param_origins.get(p, 0.0)) for p in self.cfg.params], float)
        cov = np.eye(d, dtype=float)
        mu_std = np.full(d, np.nan, dtype=float)
        cov_diag_std = np.full(d, np.nan, dtype=float)
        amp_vals = []
        residuals = []
        point_counts = []

        for i, axis_name in enumerate(self.cfg.params):
            fit_1d = self._fit_gf_axis_1d(axis_name)
            boot_1d = self._bootstrap_gf_axis_1d(axis_name)
            if fit_1d is not None:
                mu[i] = float(fit_1d["mu"])
                cov[i, i] = max(1e-12, float(fit_1d["sigma"]) ** 2)
                amp_vals.append(float(fit_1d["amp"]))
                residuals.append(float(fit_1d["residual_rms"]))
                point_counts.append(int(fit_1d["n_points"]))
            else:
                cov[i, i] = max(1e-12, float(self.cfg.init_sigma.get(axis_name, 0.5)) ** 2)
            if np.isfinite(float(boot_1d.get("mu_std", float("nan")))):
                mu_std[i] = float(boot_1d["mu_std"])
            if np.isfinite(float(boot_1d.get("sigma_std", float("nan")))):
                sigma_std = float(boot_1d["sigma_std"])
                sigma_mean = float(boot_1d.get("sigma_mean", np.sqrt(cov[i, i])))
                cov_diag_std[i] = 2.0 * max(1e-12, sigma_mean) * sigma_std

        fit = GaussianFitResult(
            ok=True,
            mu=mu.tolist(),
            cov=cov.tolist(),
            amp=float(np.nanmax(np.asarray(amp_vals, float))) if amp_vals else (float(np.max(self.y)) if self.y else 0.0),
            ln_amp=float(np.log(max(float(np.nanmax(np.asarray(amp_vals, float))) if amp_vals else (float(np.max(self.y)) if self.y else 1e-6), 1e-6))),
            ridge=float(self.cfg.ridge_fit),
            mode="diag",
            residual_rms=float(np.nanmean(np.asarray(residuals, float))) if residuals else float("nan"),
            n_points=int(np.sum(point_counts)) if point_counts else len(self.X),
        )
        boot = {
            "mu_mean": mu.copy(),
            "mu_std": mu_std,
            "cov_mean": cov.copy(),
            "cov_diag_mean": np.diag(cov).copy(),
            "cov_diag_std": cov_diag_std,
        }
        summary = {
            "fit": asdict(fit),
            "boot": {
                "mu_mean": boot["mu_mean"].tolist(),
                "mu_std": boot["mu_std"].tolist(),
                "cov_diag_mean": boot["cov_diag_mean"].tolist(),
                "cov_diag_std": boot["cov_diag_std"].tolist(),
            },
            "gf_mode": "axiswise_1d",
        }
        with open(self.out_dir / "fit_summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        return {"fit": fit, "boot": boot}

    def _stop_by_precision(self, boot: Dict) -> bool:
        mu_std = np.asarray(boot["mu_std"], float)
        if not np.all(np.isfinite(mu_std)):
            return False
        cov_diag_mean = np.asarray(boot.get("cov_diag_mean", []), float)
        if cov_diag_mean.size != mu_std.size or not np.all(np.isfinite(cov_diag_mean)):
            return False
        sigma_fit = np.sqrt(np.maximum(cov_diag_mean, 1e-12))
        thr = np.maximum(sigma_fit * float(getattr(self.cfg, "stop_sigma_ratio", 0.20)), 1e-6)
        return bool(np.all(mu_std < thr))

    def _stop_by_modulation(self, y: float) -> bool:
        thr = getattr(self.cfg, 'stop_modulation', None)
        if thr is None:
            return False
        try:
            return bool(np.isfinite(y) and float(y) >= float(thr))
        except Exception:
            return False

    def _propose_next_GF(
        self,
        axis: int,
        cycle_idx: int,
        base_x: Optional[np.ndarray] = None,
        no_fit_sigma_mult: float = 1.0,
    ) -> Tuple[np.ndarray, str]:
        axis = int(axis)
        axis_name = self.cfg.params[axis]
        if base_x is None:
            x_base = np.array([float(self.cfg.param_origins.get(p, 0.0)) for p in self.cfg.params], dtype=float)
        else:
            x_base = np.asarray(base_x, float).copy()
        fit_1d = self._fit_gf_axis_1d(axis_name)
        if fit_1d is not None:
            mu_axis = float(fit_1d["mu"])
            sig_axis = max(1e-6, float(fit_1d["sigma"]))
        else:
            mu_axis = float(x_base[axis])
            sig0 = max(1e-6, float(self.cfg.init_sigma.get(axis_name, 0.5)))
            sig_axis = sig0 * max(1.0, float(no_fit_sigma_mult))

        mode = int(cycle_idx) % 3
        x = x_base.copy()
        if mode == 0:
            x[axis] = float(mu_axis)
            self._gf_cycle_first_side.pop(axis, None)
            tag = "mu"
        elif mode == 1:
            plus_cnt, minus_cnt = self._count_axis_side_samples(axis_name=axis_name, mu_axis=float(mu_axis))
            first_side = "plus" if plus_cnt <= minus_cnt else "minus"
            self._gf_cycle_first_side[axis] = first_side
            if first_side == "plus":
                x[axis] = float(mu_axis) + float(sig_axis)
                tag = "plus_sigma"
            else:
                x[axis] = float(mu_axis) - float(sig_axis)
                tag = "minus_sigma"
        else:
            first_side = self._gf_cycle_first_side.get(axis)
            if first_side == "plus":
                x[axis] = float(mu_axis) - float(sig_axis)
                tag = "minus_sigma"
            elif first_side == "minus":
                x[axis] = float(mu_axis) + float(sig_axis)
                tag = "plus_sigma"
            else:
                plus_cnt, minus_cnt = self._count_axis_side_samples(axis_name=axis_name, mu_axis=float(mu_axis))
                if plus_cnt <= minus_cnt:
                    x[axis] = float(mu_axis) + float(sig_axis)
                    tag = "plus_sigma"
                else:
                    x[axis] = float(mu_axis) - float(sig_axis)
                    tag = "minus_sigma"
        return x, tag

    def _propose_next_zscan_bo1d(
        self,
        axis: int,
        base_x: Optional[np.ndarray] = None,
    ) -> Tuple[np.ndarray, str]:
        axis = int(axis)
        axis_name = self.cfg.params[axis]
        if base_x is None:
            x_base = np.array([float(self.cfg.param_origins.get(p, 0.0)) for p in self.cfg.params], dtype=float)
        else:
            x_base = np.asarray(base_x, float).copy()

        lo, hi = self._bounds_arrays()
        lo_axis = float(lo[axis])
        hi_axis = float(hi[axis])
        axis_recs = self._gf_axis_records(axis_name)
        x_hist = []
        y_hist = []
        for rec in axis_recs:
            try:
                x_hist.append(float(rec.x[axis_name]))
                y_hist.append(float(rec.y))
            except Exception:
                continue

        if len(x_hist) < 2:
            x_fallback, gf_tag = self._propose_next_GF(
                axis=axis,
                cycle_idx=max(0, len(axis_recs)),
                base_x=x_base,
            )
            return x_fallback, f"bo1d_fallback_{gf_tag}"

        X1 = np.asarray(x_hist, float).reshape(-1, 1)
        y1 = np.asarray(y_hist, float)
        axis_ls = max(1e-6, float(self.cfg.init_sigma.get(axis_name, self._step_for_param(axis_name))))
        gp = SimpleGP(GPParams(
            kernel="matern32",
            length_scale=np.array([axis_ls], dtype=float),
            signal_var=self.cfg.gp_signal_var,
            noise_var=self.cfg.gp_noise_var,
        ))
        try:
            gp.fit(X1, y1)
        except Exception:
            x_fallback, gf_tag = self._propose_next_GF(
                axis=axis,
                cycle_idx=max(0, len(axis_recs)),
                base_x=x_base,
            )
            return x_fallback, f"bo1d_fallback_{gf_tag}"

        n_cand = max(200, min(int(self.cfg.n_candidates), 4000))
        cand_rand = self.rng.uniform(low=lo_axis, high=hi_axis, size=n_cand)
        cand_all = np.concatenate([
            cand_rand,
            np.asarray(x_hist, float),
            np.array([lo_axis, hi_axis, float(x_base[axis])], dtype=float),
        ])
        cand_q = np.array([self._quantize_knob(float(v), axis_name) for v in cand_all], dtype=float)
        cand_q = np.clip(cand_q, lo_axis, hi_axis)
        cand = np.unique(cand_q)
        if cand.size == 0:
            x_fallback, gf_tag = self._propose_next_GF(
                axis=axis,
                cycle_idx=max(0, len(axis_recs)),
                base_x=x_base,
            )
            return x_fallback, f"bo1d_fallback_{gf_tag}"

        mu, std = gp.predict(cand.reshape(-1, 1))
        if self.cfg.acquisition.upper() == "EI":
            acq = acq_ei(mu, std, y_best=float(np.max(y1)), xi=self.cfg.ei_xi)
        else:
            acq = acq_ucb(mu, std, beta=self.cfg.ucb_beta)
        best_idx = int(np.argmax(acq))
        x = x_base.copy()
        x[axis] = float(cand[best_idx])
        return x, "bo1d"

    def _propose_next_BO(self) -> Tuple[np.ndarray, float]:
        lo, hi = self._bounds_arrays()
        X = np.asarray(self.X, float)
        y = np.asarray(self.y, float)

        gp = SimpleGP(GPParams(
            kernel=self.cfg.gp_kernel,
            length_scale=self._gp_length_scales(),
            signal_var=self.cfg.gp_signal_var,
            noise_var=self.cfg.gp_noise_var,
            zscan_axes=self._zscan_axis_indices(),
            zscan_kernel="matern32",
        ))
        gp.fit(X, y)

        cand = self._candidate_points(self.cfg.n_candidates)
        mu, std = gp.predict(cand)

        if self.cfg.acquisition.upper() == "EI":
            a = acq_ei(mu, std, y_best=float(np.max(y)), xi=self.cfg.ei_xi)
        else:
            a = acq_ucb(mu, std, beta=self.cfg.ucb_beta)

        x_next = cand[int(np.argmax(a))]
        return clamp(x_next, lo, hi), float(np.max(a))

    def _propose_next_TRBO(self) -> Tuple[np.ndarray, float]:
        lo, hi = self._bounds_arrays()
        X = np.asarray(self.X, float)
        y = np.asarray(self.y, float)
        best_idx = int(np.argmax(y))
        x0 = X[best_idx].copy()
        scales = self._gp_length_scales()
        trust_radius = np.maximum(float(self.cfg.lqo_trust_radius_sigma) * scales, self._step_array())

        gp = SimpleGP(GPParams(
            kernel=self.cfg.gp_kernel,
            length_scale=self._gp_length_scales(),
            signal_var=self.cfg.gp_signal_var,
            noise_var=self.cfg.gp_noise_var,
            zscan_axes=self._zscan_axis_indices(),
            zscan_kernel="matern32",
        ))
        gp.fit(X, y)

        cand = self._candidate_points_in_box(x0, trust_radius, self.cfg.n_candidates)
        cand = np.vstack([x0.reshape(1, -1), cand])
        mu, std = gp.predict(cand)

        if self.cfg.acquisition.upper() == "EI":
            a = acq_ei(mu, std, y_best=float(np.max(y)), xi=self.cfg.ei_xi)
        else:
            a = acq_ucb(mu, std, beta=self.cfg.ucb_beta)

        x_next = cand[int(np.argmax(a))]
        return clamp(x_next, lo, hi), float(np.max(a))

    def _fit_local_quadratic(self, X_local: np.ndarray, y_local: np.ndarray, x_center: np.ndarray, scales: np.ndarray) -> Tuple[np.ndarray, str]:
        U = (np.asarray(X_local, float) - x_center.reshape(1, -1)) / scales.reshape(1, -1)
        n, d = U.shape

        def _design(mode: str) -> np.ndarray:
            cols = [np.ones((n, 1)), U, U * U]
            if mode == "full":
                cross = []
                for i in range(d):
                    for j in range(i + 1, d):
                        cross.append((U[:, i] * U[:, j]).reshape(n, 1))
                if cross:
                    cols.append(np.hstack(cross))
            return np.hstack(cols)

        full_cols = 1 + 2 * d + d * (d - 1) // 2
        mode = "full" if n >= (full_cols + 2) else "diag"
        Phi = _design(mode)
        dist2 = np.sum(U * U, axis=1)
        w = 1.0 / np.maximum(1.0 + dist2, 1e-6)
        W = np.sqrt(w).reshape(-1, 1)
        Phi_w = Phi * W
        y_w = y_local.reshape(-1, 1) * W
        A = Phi_w.T @ Phi_w + float(self.cfg.ridge_fit) * np.eye(Phi.shape[1])
        coef = np.linalg.solve(A, Phi_w.T @ y_w).reshape(-1)
        return coef, mode

    def _predict_local_quadratic(self, coef: np.ndarray, U: np.ndarray, d: int, mode: str) -> np.ndarray:
        idx = 0
        out = np.full(U.shape[0], float(coef[idx])); idx += 1
        out += U @ coef[idx:idx + d]; idx += d
        out += (U * U) @ coef[idx:idx + d]; idx += d
        if mode == "full":
            for i in range(d):
                for j in range(i + 1, d):
                    out += coef[idx] * U[:, i] * U[:, j]
                    idx += 1
        return out

    def _propose_next_LQO(self) -> np.ndarray:
        lo, hi = self._bounds_arrays()
        X = np.asarray(self.X, float)
        y = np.asarray(self.y, float)
        d = X.shape[1]
        best_idx = int(np.argmax(y))
        x0 = X[best_idx].copy()
        scales = self._gp_length_scales()
        trust_radius = np.maximum(float(self.cfg.lqo_trust_radius_sigma) * scales, self._step_array())

        in_box = np.all(np.abs(X - x0.reshape(1, -1)) <= trust_radius.reshape(1, -1), axis=1)
        idx = np.where(in_box)[0]
        min_local = max(int(self.cfg.lqo_min_local_points), 2 * d + 3)
        if idx.size < min_local:
            dist = np.linalg.norm((X - x0.reshape(1, -1)) / scales.reshape(1, -1), axis=1)
            idx = np.argsort(dist)[:min(min_local, len(X))]
        X_local = X[idx]
        y_local = y[idx]

        coef, mode = self._fit_local_quadratic(X_local, y_local, x0, scales)

        n_cand = max(200, min(int(self.cfg.n_candidates), int(self.cfg.lqo_candidates)))
        cand = x0.reshape(1, -1) + self.rng.uniform(
            low=-trust_radius.reshape(1, -1),
            high=trust_radius.reshape(1, -1),
            size=(n_cand, d),
        )
        cand = clamp(cand, lo.reshape(1, -1), hi.reshape(1, -1))
        cand = np.vstack([x0.reshape(1, -1), cand])
        U_cand = (cand - x0.reshape(1, -1)) / scales.reshape(1, -1)
        score = self._predict_local_quadratic(coef, U_cand, d, mode=mode)
        x_next = cand[int(np.argmax(score))]
        return clamp(x_next, lo, hi)

    def run(self) -> Dict:
        lo, hi = self._bounds_arrays()
        method_raw = str(self.cfg.method).upper()
        method = "GF" if method_raw in {"GF", "SEQUENTIAL"} else method_raw
        gf_final_x: Dict[str, float] = {}
        gf_final_y: float = float("nan")
        try:
            # Initialize (structured first, then random fill)
            init_pts = []
            if self.cfg.init_strategy == "structured":
                init_pts = self._structured_init_points()

            # In Sequential mode, do per-axis initial triplets (center,+sigma,-sigma) inside axis loop.
            # In BO/LQO mode, keep legacy behavior: structured up to n_init_random then random fill.
            if method == "GF":
                min_pts = max(1, int(getattr(self.cfg, "gf_axis_min_points", 3)))
                axis_max = max(min_pts, int(getattr(self.cfg, "gf_axis_max_steps", 12)))
                init_target = 0
                total_limit = max(1, len(self.cfg.params) * (axis_max + 1))
            else:
                min_pts = 0
                axis_max = 0
                init_target = int(self.cfg.n_init_random)
                total_limit = max(1, int(getattr(self.cfg, "bo_max_steps", self.cfg.max_steps)))

            for x in init_pts:
                if len(self.X) >= init_target or len(self.X) >= total_limit:
                    break
                if self.stop_flag.is_stopped():
                    break
                y, yerr = self._measure_at(x, chosen_by="init_structured")
                self.X.append(x)
                self.y.append(y)
                self.yerr.append(yerr)
                rec = StepRecord(
                    step=len(self.records) + 1,
                    t_iso=_dt.datetime.now().isoformat(timespec="seconds"),
                    x=self._x_dict(x),
                    y=y,
                    y_err=yerr,
                    chosen_by="init_structured",
                    dat=dict(self._last_dat),
                )
                self._log_step(rec)
                self._emit(len(self.X), {
                    "phase": "init",
                    "x": rec.x,
                    "y": y,
                    "y_err": yerr,
                })
                if method != "GF" and self._stop_by_modulation(y):
                    self.stop_flag.request_stop()
                    self._emit(len(self.X), {"phase": "stop", "reason": "modulation_threshold_hit", "y": y})
                    break

            while method != "GF" and len(self.X) < int(self.cfg.n_init_random) and len(self.X) < total_limit:
                if self.stop_flag.is_stopped():
                    break
                x = self._random_point()
                y, yerr = self._measure_at(x, chosen_by="init_random")
                self.X.append(x)
                self.y.append(y)
                self.yerr.append(yerr)
                rec = StepRecord(
                    step=len(self.records) + 1,
                    t_iso=_dt.datetime.now().isoformat(timespec="seconds"),
                    x=self._x_dict(x),
                    y=y,
                    y_err=yerr,
                    chosen_by="init_random",
                    dat=dict(self._last_dat),
                )
                self._log_step(rec)
                self._emit(len(self.X), {
                    "phase": "init",
                    "x": rec.x,
                    "y": y,
                    "y_err": yerr,
                })
                if method != "GF" and self._stop_by_modulation(y):
                    self.stop_flag.request_stop()
                    self._emit(len(self.X), {"phase": "stop", "reason": "modulation_threshold_hit", "y": y})
                    break

            if method == "GF":
                x_fixed = np.array([float(self.cfg.param_origins.get(p, 0.0)) for p in self.cfg.params], dtype=float)
                gf_resume = self._build_gf_resume_state()
                if self.records:
                    x_fixed = np.asarray(gf_resume["x_fixed"], float)
                for axis_idx, axis_name in enumerate(self.cfg.params):
                    if self.stop_flag.is_stopped() or len(self.X) >= total_limit:
                        break
                    axis_resume = gf_resume["axis_states"][axis_idx] if axis_idx < len(gf_resume["axis_states"]) else {}
                    if bool(axis_resume.get("done", False)):
                        continue

                    x_fixed = clamp(x_fixed, lo, hi)
                    axis_steps = int(axis_resume.get("axis_steps", 0))
                    last_y_axis: Optional[float] = axis_resume.get("last_y", None)
                    cycle_idx = int(axis_resume.get("cycle_idx", 0))
                    init_tags_done = set(axis_resume.get("init_tags_done", set()))
                    gf_no_fit_stage = 0
                    gf_forced_offsets: List[float] = []

                    axis_sigma = max(1e-6, float(self.cfg.init_sigma.get(axis_name, 0.5)))
                    if self._is_zscan_axis(axis_name):
                        # Z-axis init is now also anchored at x_fixed (current axis value).
                        # This allows init0 reuse across axis transitions, then probes +/-half-range from that anchor.
                        z_span = max(1e-6, float(getattr(self.cfg, "zscan_range", axis_sigma)))
                        axis_init_plan = [
                            ("init0", 0.0, False),
                            ("init+", +0.5 * z_span, False),
                            ("init-", -0.5 * z_span, False),
                        ]
                    else:
                        axis_init_plan = [
                            ("init0", 0.0, False),
                            ("init+", +axis_sigma, False),
                            ("init-", -axis_sigma, False),
                        ]
                    for init_tag, axis_val, use_abs in axis_init_plan:
                        if init_tag in init_tags_done:
                            continue
                        if len(self.X) >= total_limit or self.stop_flag.is_stopped():
                            break
                        x_init = x_fixed.copy()
                        if use_abs:
                            x_init[axis_idx] = float(axis_val)
                        else:
                            x_init[axis_idx] = x_init[axis_idx] + float(axis_val)
                        x_init = self._clamp_with_warn(
                            x_init, lo, hi, step=len(self.X) + 1, context=f"gf_axis_init_{init_tag}", axis=axis_name
                        )
                        # Keep init0 at x_fixed so the next axis starts by reusing the previous-axis final point.
                        if init_tag != "init0":
                            x_init = self._avoid_duplicate_gf_point(x_init, axis_idx)

                        y_i, yerr_i = self._measure_at(x_init, chosen_by=f"GF[{axis_name}]_{init_tag}")
                        if self._last_measure_reused:
                            # Reused init points are valid axis samples; log them so 1D GF sees/plots them.
                            last_y_axis = float(y_i)
                            axis_steps += 1
                            x_init_q = self._quantize_x_vec(np.asarray(x_init, float))
                            rec_i = StepRecord(
                                step=len(self.records) + 1,
                                t_iso=_dt.datetime.now().isoformat(timespec="seconds"),
                                x=self._x_dict(x_init_q),
                                y=y_i,
                                y_err=yerr_i,
                                chosen_by=f"GF[{axis_name}]_{init_tag}",
                                dat=dict(self._last_dat),
                            )
                            self._log_step(rec_i)
                            if init_tag == "init0":
                                self._emit(rec_i.step, {
                                    "phase": "reuse",
                                    "reason": "gf_init0_reuse_previous_axis_final",
                                    "axis": axis_name,
                                    "tag": init_tag,
                                    "x": rec_i.x,
                                    "y": y_i,
                                    "y_err": yerr_i,
                                    "reuse_from_step": self._last_reuse_from_step,
                                })
                            else:
                                self._emit(rec_i.step, {
                                    "phase": "warn",
                                    "reason": "gf_init_reused_candidate",
                                    "axis": axis_name,
                                    "tag": init_tag,
                                    "x": rec_i.x,
                                })
                            self._emit(rec_i.step, {
                                "phase": "loop",
                                "chosen_by": rec_i.chosen_by,
                                "axis": axis_name,
                                "x": rec_i.x,
                                "y": y_i,
                                "y_err": yerr_i,
                                "best_y": float(np.max(self.y)) if self.y else float(y_i),
                                "axis_steps": axis_steps,
                                "axis_max": axis_max,
                                "reuse": True,
                                "reuse_from_step": self._last_reuse_from_step,
                            })
                            continue
                        last_y_axis = float(y_i)
                        axis_steps += 1

                        self.X.append(x_init)
                        self.y.append(y_i)
                        self.yerr.append(yerr_i)

                        rec_i = StepRecord(
                            step=len(self.records) + 1,
                            t_iso=_dt.datetime.now().isoformat(timespec="seconds"),
                            x=self._x_dict(x_init),
                            y=y_i,
                            y_err=yerr_i,
                            chosen_by=f"GF[{axis_name}]_{init_tag}",
                            dat=dict(self._last_dat),
                        )
                        self._log_step(rec_i)
                        self._emit(len(self.X), {
                            "phase": "loop",
                            "chosen_by": rec_i.chosen_by,
                            "axis": axis_name,
                            "x": rec_i.x,
                            "y": y_i,
                            "y_err": yerr_i,
                            "best_y": float(np.max(self.y)),
                            "axis_steps": axis_steps,
                            "axis_max": axis_max,
                        })

                    while len(self.X) < total_limit and (not self.stop_flag.is_stopped()):
                        self._emit(len(self.X), {
                            "phase": "model_fit",
                            "method": method,
                            "step_next": len(self.X) + 1,
                        })

                        if axis_steps >= min_pts:
                            axis_fit_1d = self._fit_gf_axis_1d(axis_name=axis_name)
                            axis_boot_1d = self._bootstrap_gf_axis_1d(axis_name=axis_name)
                            if (axis_fit_1d is None) and (not self._is_zscan_axis(axis_name)):
                                if gf_no_fit_stage == 0:
                                    gf_no_fit_stage = 1
                                    gf_forced_offsets = [2.0 * axis_sigma, -2.0 * axis_sigma]
                                    exp_info = self._gf_expand_axis_bounds(axis_name, target_sigma_mult=3.0)
                                    lo, hi = self._bounds_arrays()
                                    expand_text = "range unchanged"
                                    if bool(exp_info.get("changed", 0.0)):
                                        expand_text = (
                                            f"range expanded to [{float(exp_info.get('new_lo', float('nan'))):+.4f}, "
                                            f"{float(exp_info.get('new_hi', float('nan'))):+.4f}] "
                                            f"(~ +/-{float(exp_info.get('new_span_sigma', float('nan'))):.2f}sigma)"
                                        )
                                    self._emit(len(self.X), {
                                        "phase": "warn",
                                        "reason": "gf_fit_unavailable_stage1",
                                        "axis": axis_name,
                                        "message": (
                                            f"GF[{axis_name}] 1D fit unavailable -> next probes are +2sigma and -2sigma, "
                                            f"set search range target to +/-3sigma; {expand_text}"
                                        ),
                                    })
                                elif (gf_no_fit_stage == 1) and (not gf_forced_offsets):
                                    gf_no_fit_stage = 2
                                    gf_forced_offsets = [3.0 * axis_sigma, -3.0 * axis_sigma]
                                    exp_info = self._gf_expand_axis_bounds(axis_name, target_sigma_mult=4.0)
                                    lo, hi = self._bounds_arrays()
                                    expand_text = "range unchanged"
                                    if bool(exp_info.get("changed", 0.0)):
                                        expand_text = (
                                            f"range expanded to [{float(exp_info.get('new_lo', float('nan'))):+.4f}, "
                                            f"{float(exp_info.get('new_hi', float('nan'))):+.4f}] "
                                            f"(~ +/-{float(exp_info.get('new_span_sigma', float('nan'))):.2f}sigma)"
                                        )
                                    self._emit(len(self.X), {
                                        "phase": "warn",
                                        "reason": "gf_fit_unavailable_stage2",
                                        "axis": axis_name,
                                        "message": (
                                            f"GF[{axis_name}] still unfittable after +/-2sigma -> next probes are +3sigma and -3sigma, "
                                            f"set search range target to +/-4sigma; {expand_text}"
                                        ),
                                    })
                            elif axis_fit_1d is not None:
                                gf_no_fit_stage = 0
                                gf_forced_offsets = []
                            axis_mu_1d = float(axis_fit_1d["mu"]) if axis_fit_1d is not None else float("nan")
                            axis_sigma_1d = float(axis_fit_1d["sigma"]) if axis_fit_1d is not None else float("nan")
                            mu_std_axis = float(axis_boot_1d.get("mu_std", float("nan")))
                            axis_sigma_threshold = (
                                max(1e-6, axis_sigma_1d * float(getattr(self.cfg, "stop_sigma_ratio", 0.20)))
                                if np.isfinite(axis_sigma_1d) else float("nan")
                            )
                            cond_mu = bool(
                                np.isfinite(mu_std_axis)
                                and np.isfinite(axis_sigma_threshold)
                                and (mu_std_axis <= axis_sigma_threshold)
                            )
                            cond_mod = bool(last_y_axis is not None and self._stop_by_modulation(float(last_y_axis)))
                            cond_cnt = bool(axis_steps >= axis_max)
                            if cond_mu or cond_mod or cond_cnt:
                                reasons = []
                                if cond_mu:
                                    reasons.append("mu_sigma")
                                if cond_mod:
                                    reasons.append("modulation")
                                if cond_cnt:
                                    reasons.append("axis_step_limit")
                                self._emit(len(self.X), {
                                    "phase": "axis_done",
                                    "axis": axis_name,
                                    "axis_steps": axis_steps,
                                    "axis_max": axis_max,
                                    "reason": ",".join(reasons),
                                    "mu_std_axis": mu_std_axis,
                                    "mu_axis_1d": axis_mu_1d,
                                    "sigma_axis_1d": axis_sigma_1d,
                                    "sigma_axis_threshold": axis_sigma_threshold,
                                })

                                if len(self.X) < total_limit and (not self.stop_flag.is_stopped()):
                                    x_confirm = x_fixed.copy()
                                    final_source = "fit_mu"
                                    if axis_fit_1d is not None:
                                        x_confirm[axis_idx] = float(axis_fit_1d["mu"])
                                    else:
                                        # If 1D fit is unstable, keep coordinate-descent behavior by
                                        # locking this axis at its best measured value rather than
                                        # resetting to origin.
                                        axis_recs = self._gf_axis_records(axis_name)
                                        if axis_recs:
                                            best_axis_rec = max(axis_recs, key=lambda rec: float(rec.y))
                                            x_confirm[axis_idx] = float(best_axis_rec.x.get(axis_name, self.cfg.param_origins.get(axis_name, 0.0)))
                                            final_source = "best_measured_axis"
                                        else:
                                            x_confirm[axis_idx] = float(self.cfg.param_origins.get(axis_name, 0.0))
                                            final_source = "origin_fallback"
                                    x_confirm = clamp(x_confirm, lo, hi)
                                    x_confirm = self._avoid_duplicate_gf_point(x_confirm, axis_idx)

                                    y_c, yerr_c = self._measure_at(x_confirm, chosen_by=f"GF[{axis_name}]_final")
                                    if not self._last_measure_reused:
                                        self.X.append(x_confirm)
                                        self.y.append(y_c)
                                        self.yerr.append(yerr_c)

                                        rec_c = StepRecord(
                                            step=len(self.records) + 1,
                                            t_iso=_dt.datetime.now().isoformat(timespec="seconds"),
                                            x=self._x_dict(x_confirm),
                                            y=y_c,
                                            y_err=yerr_c,
                                            chosen_by=f"GF[{axis_name}]_final",
                                            dat=dict(self._last_dat),
                                        )
                                        self._log_step(rec_c)
                                        self._emit(len(self.X), {
                                            "phase": "axis_finalize",
                                            "chosen_by": rec_c.chosen_by,
                                            "axis": axis_name,
                                            "x": rec_c.x,
                                            "y": y_c,
                                            "y_err": yerr_c,
                                            "best_y": float(np.max(self.y)),
                                            "final_source": final_source,
                                        })
                                    else:
                                        self._emit(len(self.X), {
                                            "phase": "axis_finalize",
                                            "chosen_by": f"GF[{axis_name}]_final",
                                            "axis": axis_name,
                                            "x": self._x_dict(x_confirm),
                                            "y": y_c,
                                            "y_err": yerr_c,
                                            "best_y": float(np.max(self.y)) if self.y else float(y_c),
                                            "final_source": final_source,
                                            "reuse": True,
                                        })
                                    x_fixed[axis_idx] = float(x_confirm[axis_idx])
                                    gf_final_x = self._x_dict(self._quantize_x_vec(np.asarray(x_confirm, float)))
                                    gf_final_y = float(y_c)
                                break

                        used_forced_probe = False
                        if self._is_zscan_axis(axis_name):
                            zscan_method = str(getattr(self.cfg, "zscan_method", "BO") or "BO").upper()
                            if zscan_method == "GF":
                                x_next_raw, phase_tag = self._propose_next_GF(
                                    axis=axis_idx,
                                    cycle_idx=cycle_idx,
                                    base_x=x_fixed,
                                )
                            else:
                                x_next_raw, phase_tag = self._propose_next_zscan_bo1d(
                                    axis=axis_idx,
                                    base_x=x_fixed,
                                )
                        else:
                            if gf_forced_offsets:
                                offset = float(gf_forced_offsets.pop(0))
                                x_next_raw = x_fixed.copy()
                                x_next_raw[axis_idx] = float(x_fixed[axis_idx]) + offset
                                sig_mult = abs(offset) / max(axis_sigma, 1e-12)
                                side = "plus" if offset >= 0.0 else "minus"
                                phase_tag = f"force_{side}{sig_mult:.0f}sigma"
                                used_forced_probe = True
                            else:
                                no_fit_sigma_mult = 1.0
                                if gf_no_fit_stage == 1:
                                    no_fit_sigma_mult = 2.0
                                elif gf_no_fit_stage >= 2:
                                    no_fit_sigma_mult = 3.0
                                x_next_raw, phase_tag = self._propose_next_GF(
                                    axis=axis_idx,
                                    cycle_idx=cycle_idx,
                                    base_x=x_fixed,
                                    no_fit_sigma_mult=no_fit_sigma_mult,
                                )
                        x_next = self._clamp_with_warn(
                            x_next_raw, lo, hi, step=len(self.X) + 1, context=f"gf_propose_{phase_tag}", axis=axis_name
                        )
                        x_next = self._avoid_duplicate_gf_point(x_next, axis_idx)
                        chosen_by = f"GF[{axis_name}]_{phase_tag}"

                        y, yerr = self._measure_at(x_next, chosen_by=chosen_by)
                        if self._last_measure_reused:
                            if not used_forced_probe:
                                cycle_idx += 1
                            self._emit(len(self.X), {
                                "phase": "warn",
                                "reason": "gf_loop_reused_candidate",
                                "axis": axis_name,
                                "chosen_by": chosen_by,
                                "x": self._x_dict(x_next),
                            })
                            continue
                        last_y_axis = float(y)
                        axis_steps += 1
                        if not used_forced_probe:
                            cycle_idx += 1

                        self.X.append(x_next)
                        self.y.append(y)
                        self.yerr.append(yerr)

                        rec = StepRecord(
                            step=len(self.records) + 1,
                            t_iso=_dt.datetime.now().isoformat(timespec="seconds"),
                            x=self._x_dict(x_next),
                            y=y,
                            y_err=yerr,
                            chosen_by=chosen_by,
                            dat=dict(self._last_dat),
                        )
                        self._log_step(rec)

                        self._emit(len(self.X), {
                            "phase": "loop",
                            "chosen_by": chosen_by,
                            "x": rec.x,
                            "y": y,
                            "y_err": yerr,
                            "best_y": float(np.max(self.y)),
                            "axis": axis_name,
                            "axis_steps": axis_steps,
                            "axis_max": axis_max,
                        })
            else:
                while len(self.X) < total_limit and (not self.stop_flag.is_stopped()):
                    X_np = np.asarray(self.X, float)

                    fit_pack = self._fit_and_bootstrap()
                    boot = fit_pack["boot"]

                    if method == "LQO":
                        x_next = self._propose_next_LQO()
                        chosen_by = "LQO"
                    elif method == "TRBO":
                        x_next, max_acq = self._propose_next_TRBO()
                        chosen_by = "TRBO"
                        self._emit(len(self.X), {
                            "phase": "acquisition",
                            "method": method,
                            "step_next": len(self.X) + 1,
                            "max_acq": float(max_acq),
                        })
                    else:
                        x_next, max_acq = self._propose_next_BO()
                        chosen_by = "BO"
                        self._emit(len(self.X), {
                            "phase": "acquisition",
                            "method": method,
                            "step_next": len(self.X) + 1,
                            "max_acq": float(max_acq),
                        })
                        if bool(getattr(self.cfg, "bo_stop_on_low_acq", True)):
                            thr = float(getattr(self.cfg, "bo_low_acq_threshold", 1e-4))
                            if max_acq <= thr:
                                self._low_acq_streak += 1
                            else:
                                self._low_acq_streak = 0
                            patience = max(1, int(getattr(self.cfg, "bo_low_acq_patience", 2)))
                            if self._low_acq_streak >= patience:
                                self._emit(len(self.X), {
                                    "phase": "stop",
                                    "reason": "low_acquisition_gain",
                                    "max_acq": float(max_acq),
                                    "threshold": float(thr),
                                    "streak": int(self._low_acq_streak),
                                })
                                self.stop_flag.request_stop()
                                break

                    if len(self.X) > 0:
                        if np.min(np.linalg.norm(X_np - x_next.reshape(1, -1), axis=1)) < 1e-6:
                            x_next = self._random_point()
                            chosen_by += "+jitter"

                    y, yerr = self._measure_at(x_next, chosen_by=chosen_by)

                    self.X.append(x_next)
                    self.y.append(y)
                    self.yerr.append(yerr)

                    rec = StepRecord(
                        step=len(self.records) + 1,
                        t_iso=_dt.datetime.now().isoformat(timespec="seconds"),
                        x=self._x_dict(x_next),
                        y=y,
                        y_err=yerr,
                        chosen_by=chosen_by,
                        dat=dict(self._last_dat),
                    )
                    self._log_step(rec)

                    self._emit(len(self.X), {
                        "phase": "loop",
                        "chosen_by": chosen_by,
                        "x": rec.x,
                        "y": y,
                        "y_err": yerr,
                        "best_y": float(np.max(self.y)),
                    })
                    if self._stop_by_modulation(y):
                        self.stop_flag.request_stop()
                        self._emit(len(self.X), {"phase": "stop", "reason": "modulation_threshold_hit", "y": y})
                        break
        except GracefulStopRequested:
            pass

        # Final fit
        final_pack = self._fit_and_bootstrap_gf_1d() if method == "GF" else self._fit_and_bootstrap()
        final_fit: GaussianFitResult = final_pack["fit"]
        final_boot = final_pack["boot"]

        best_idx = int(np.argmax(np.asarray(self.y, float))) if self.y else -1
        best_measured_x = self._x_dict(self._quantize_x_vec(np.asarray(self.X[best_idx], float))) if best_idx >= 0 else {}
        best_measured_y = float(self.y[best_idx]) if best_idx >= 0 else float("nan")
        final_x = dict(best_measured_x)
        final_y = float(best_measured_y)
        final_strategy = "best_measured"
        if method == "GF" and gf_final_x:
            final_x = dict(gf_final_x)
            final_y = float(gf_final_y)
            final_strategy = "sequential_axis_final_mu"
        self._final_target_x = dict(final_x)
        self._final_target_y = float(final_y)
        best_x = dict(final_x)
        best_y = float(final_y)
        best_apply_ok = False
        best_apply_error = ""

        if final_x:
            self._emit(len(self.X), {
                "phase": "final_apply",
                "x": final_x,
                "best_y": best_y,
                "final_strategy": final_strategy,
            })
            try:
                self.controller.apply_knobs(final_x)
                best_apply_ok = True
            except Exception as exc:
                best_apply_error = str(exc)
                self._emit(len(self.X), {
                    "phase": "warn",
                    "reason": "final_apply_failed",
                    "message": best_apply_error,
                    "x": final_x,
                    "best_y": best_y,
                    "final_strategy": final_strategy,
                })

        out = {
            "out_dir": str(self.out_dir),
            "measurements_csv": str(self.measurements_csv_path),
            "machine_origin_file": str(self.machine_origin_path),
            "n_steps": len(self.X),
            "best_x": best_x,
            "best_y": best_y,
            "best_measured_x": best_measured_x,
            "best_measured_y": best_measured_y,
            "final_x": final_x,
            "final_y": final_y,
            "final_strategy": final_strategy,
            "best_apply_ok": best_apply_ok,
            "best_apply_error": best_apply_error,
            "fit_mu": final_fit.mu,
            "fit_cov": final_fit.cov,
            "boot_mu_mean": final_boot["mu_mean"].tolist(),
            "boot_mu_std": final_boot["mu_std"].tolist(),
            "fit_mu_err": final_boot["mu_std"][0],
            "fit_sigma_err": 0.5 * final_boot["cov_diag_std"][0] / max(1e-12, np.sqrt(final_boot["cov_diag_mean"][0])),}

        out["gf_scan_dat_files"] = self._gf_dat_export_paths()

        with open(self.out_dir / "result.json", "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)

        return out

from ipbsm_opt_plotting import build_gf_axiswise_fit, plot_bo_gp_heatmap, plot_results
