from __future__ import annotations

import csv
import importlib.util
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

_KNOBS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _KNOBS_DIR.parent
for _path in (str(_KNOBS_DIR), str(_REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from machine_interfaces_atf2.InterfaceATF2_Ext import CurrentDropToZeroError, InterfaceATF2_Ext


def _load_module(module_name: str, module_path: Path):
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(f"Could not load module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_BASE_OPT = _load_module("Knobs.IPBSM_Opt", _KNOBS_DIR / "IPBSM_Opt.py")
_MOTR_MEAS = _load_module(
    "ATF2.mOTR_measurements",
    _KNOBS_DIR / "mOTR_measurements.py",
)

BaseOptimizer = _BASE_OPT.Optimizer
StopFlag = _BASE_OPT.StopFlag
StepRecord = _BASE_OPT.StepRecord
now_tag = _BASE_OPT.now_tag


DEFAULT_MOTR_OUTPUT_BASE_DIR = Path("/atf/data/flight-simulator/mOTR_Opt")
QK_KNOBS = ["QK1X", "QK2X", "QK3X", "QK4X"]
Q_KNOBS = ["QF17X", "QD18X", "QF19X", "QD20X"]
KNOB_ORDER = QK_KNOBS + Q_KNOBS
DEFAULT_HALF_RANGE_A = 1.0
DEFAULT_STEP_A = 0.1
DEFAULT_MOTR_IDS = [0, 1, 2, 3]


def default_output_base_dir() -> Path:
    return DEFAULT_MOTR_OUTPUT_BASE_DIR


def recommended_initial_points(d: int) -> int:
    if d <= 0:
        return 3
    return max(3, 1 + 2 * d)


def recommended_max_steps(d: int, n_init: int) -> int:
    if d <= 0:
        return max(12, n_init + 2)
    if d >= 7:
        return max(48, n_init + 14)
    if d >= 4:
        return max(28, n_init + 10)
    return max(16, n_init + 6)


def recommended_candidate_pool(d: int) -> int:
    if d <= 0:
        return 800
    if d >= 8:
        return 3000
    if d >= 6:
        return 2400
    if d >= 4:
        return 1600
    return 1000


def _safe_float(value: Any, default: float = np.nan) -> float:
    try:
        if value is None:
            return float(default)
        arr = np.asarray(value)
        if arr.size == 0:
            return float(default)
        return float(arr.flat[0])
    except Exception:
        return float(default)


def _nanmean(values: List[float]) -> float:
    arr = np.asarray(values, dtype=float)
    if arr.size == 0 or not np.any(np.isfinite(arr)):
        return float("nan")
    return float(np.nanmean(arr))


def _normalize_objective_source(source: str) -> str:
    text = str(source or "conrad").strip().lower()
    return "kek" if text == "kek" else "conrad"


def _objective_label(source: str) -> str:
    return "KEK" if _normalize_objective_source(source) == "kek" else "Conrad"


def _objective_to_score(objective_value: float) -> float:
    if not np.isfinite(objective_value) or objective_value <= 0:
        return 1e-12
    return float(1.0 / objective_value)


def _score_err_from_objective(objective_value: float, objective_err: float) -> float:
    if not np.isfinite(objective_value) or objective_value <= 0:
        return 0.0
    if not np.isfinite(objective_err) or objective_err < 0:
        return 0.0
    return float(objective_err / (objective_value ** 2))


def _motr_extra_csv_columns(cfg: "OptimizerConfig") -> List[str]:
    cols = [
        "objective_source",
        "plot_both",
        "objective_selected",
        "objective_selected_err",
        "objective_conrad",
        "objective_conrad_err",
        "objective_kek",
        "objective_kek_err",
        "measurement_counter",
        "measurement_timestamp",
        "measurement_file",
    ]
    sample_count = max(1, int(getattr(cfg, "measurement_kek_samples", 3)))
    for otr_id in getattr(cfg, "motr_ids", DEFAULT_MOTR_IDS):
        prefix = f"otr{int(otr_id)}"
        cols.extend(
            [
                f"{prefix}_total_intensity",
                f"{prefix}_conrad_sigma_h_um",
                f"{prefix}_conrad_sigma_v_um",
                f"{prefix}_conrad_sigma13_m2",
                f"{prefix}_kek_size_h_mean",
                f"{prefix}_kek_size_v_mean",
                f"{prefix}_kek_size_h_std",
                f"{prefix}_kek_size_v_std",
                f"{prefix}_kek_center_h",
                f"{prefix}_kek_center_v",
                f"{prefix}_calibration_h_um_per_px",
                f"{prefix}_calibration_v_um_per_px",
                f"{prefix}_baseline_conrad_sigma_v",
                f"{prefix}_baseline_kek_sigma_v",
            ]
        )
        for sample_idx in range(sample_count):
            cols.append(f"{prefix}_kek_size_h_sample{sample_idx + 1}")
            cols.append(f"{prefix}_kek_size_v_sample{sample_idx + 1}")
    return cols


@dataclass
class OptimizerConfig:
    mode_name: str
    method: str
    acquisition: str
    params: List[str]
    bounds: Dict[str, Tuple[float, float]]
    init_sigma: Dict[str, float]
    param_origins: Dict[str, float] = field(default_factory=dict)
    scan_mode_label: str = "mOTR"
    meas_sigma: float = 0.01
    expected_y_max: Optional[float] = None
    stop_modulation: Optional[float] = None
    knob_step: float = DEFAULT_STEP_A
    param_steps: Dict[str, float] = field(default_factory=dict)
    zscan_axis_names: List[str] = field(default_factory=list)
    zscan_method: str = "BO"
    zscan_range: float = 0.0
    zscan_step: float = 0.0
    gf_weight_peak: float = 1.0
    gf_weight_refine: float = 1.0
    gf_jitter_frac: float = 0.25
    max_steps: int = 60
    bo_max_steps: int = 60
    gf_axis_max_steps: int = 7
    gf_axis_min_points: int = 3
    stop_sigma_ratio: float = 0.20
    stop_y_sigma: float = 0.01
    n_init_random: int = 8
    n_candidates: int = 6000
    n_bootstrap: int = 60
    ridge_fit: float = 1e-4
    gp_kernel: str = "rbf"
    gp_length_scale: float = 1.0
    gp_ard_length_scales: Optional[Dict[str, float]] = None
    gp_signal_var: float = 0.15
    gp_noise_var: float = 1e-4
    ucb_beta: float = 2.0
    ei_xi: float = 0.0
    probe_scale: float = 1.0
    init_strategy: str = "structured"
    lqo_trust_radius_sigma: float = 1.5
    lqo_min_local_points: int = 12
    lqo_candidates: int = 2000
    bo_stop_on_low_acq: bool = True
    bo_low_acq_threshold: float = 1e-4
    bo_low_acq_patience: int = 2
    average_pause_ratio: float = 0.80
    objective_source: str = "Conrad"
    plot_both: bool = True
    motr_ids: List[int] = field(default_factory=lambda: list(DEFAULT_MOTR_IDS))
    measurement_min_total_intensity: float = 130000.0
    measurement_max_retries: int = 3
    measurement_background_frames: int = 10
    measurement_beam_frames: int = 5
    measurement_background_wait_sec: float = 1.0
    measurement_beam_wait_sec: float = 3.0
    measurement_select_wait_sec: float = 1.0
    measurement_retract_wait_sec: float = 5.0
    measurement_insert_wait_sec: float = 5.0
    measurement_retry_wait_sec: float = 1.0
    measurement_kek_samples: int = 3
    measurement_kek_sample_interval_sec: float = 1.0


class EPICSmOTRController:
    def __init__(
        self,
        interface: InterfaceATF2_Ext,
        *,
        objective_source: str = "Conrad",
        plot_both: bool = True,
        out_dir: Optional[Path] = None,
        motr_ids: Optional[List[int]] = None,
        baseline_state: Optional[Dict[str, Any]] = None,
        measurement_settings: Optional[Dict[str, Any]] = None,
    ):
        self.interface = interface
        self.objective_source = _normalize_objective_source(objective_source)
        self.plot_both = bool(plot_both)
        self.out_dir = Path(out_dir) if out_dir is not None else None
        self.measurement_output_dir = self.out_dir / "measurement_data" if self.out_dir is not None else None
        if self.measurement_output_dir is not None:
            self.measurement_output_dir.mkdir(parents=True, exist_ok=True)
        self.motr_ids = [int(v) for v in (motr_ids or DEFAULT_MOTR_IDS)]
        self.measurement_settings = dict(measurement_settings or {})
        self.baseline_state: Dict[str, Any] = {}
        self.baseline_currents: Dict[str, float] = {}
        self.resolved_prefixes: Dict[str, str] = {}
        self.measurement_baseline: Dict[str, Dict[int, float]] = {"conrad": {}, "kek": {}}
        self.measurement_counter = 0
        self.latest_measurement: Dict[str, Any] = {}
        if baseline_state:
            self.set_baseline_state(baseline_state)

    def _pv_prefix_candidates(self, name: str) -> List[str]:
        raw = str(name).strip()
        candidates = [raw]
        if raw.startswith("M") and len(raw) > 1:
            candidates.append(raw[1:])
        else:
            candidates.append(f"M{raw}")
        out = []
        for cand in candidates:
            if cand and cand not in out:
                out.append(cand)
        return out

    def _resolve_magnet_prefix(self, name: str) -> str:
        key = str(name)
        if key in self.resolved_prefixes:
            return self.resolved_prefixes[key]

        for cand in self._pv_prefix_candidates(key):
            current_write = self.interface._pv_get(f"{cand}:currentWrite", default=np.nan, timeout=0.7)
            current_read = self.interface._pv_get(f"{cand}:currentRead", default=np.nan, timeout=0.7)
            current_rb = self.interface._pv_get(f"{cand}:current", default=np.nan, timeout=0.7)
            if np.isfinite(current_write) or np.isfinite(current_read) or np.isfinite(current_rb):
                self.resolved_prefixes[key] = cand
                return cand

        self.resolved_prefixes[key] = key
        return key

    def _read_current_value(self, prefix: str) -> float:
        values = [
            self.interface._pv_get(f"{prefix}:currentWrite", default=np.nan, timeout=0.7),
            self.interface._pv_get(f"{prefix}:currentRead", default=np.nan, timeout=0.7),
            self.interface._pv_get(f"{prefix}:current", default=np.nan, timeout=0.7),
        ]
        for value in values:
            if np.isfinite(value):
                return float(value)
        return float("nan")

    def _readback_value(self, prefix: str) -> float:
        values = [
            self.interface._pv_get(f"{prefix}:currentRead", default=np.nan, timeout=0.7),
            self.interface._pv_get(f"{prefix}:current", default=np.nan, timeout=0.7),
            self.interface._pv_get(f"{prefix}:currentWrite", default=np.nan, timeout=0.7),
        ]
        for value in values:
            if np.isfinite(value):
                return float(value)
        return float("nan")

    def set_measurement_baseline(self, baseline: Optional[Dict[str, Any]]) -> None:
        data = {"conrad": {}, "kek": {}}
        if isinstance(baseline, dict):
            for source_key in ("conrad", "kek"):
                raw = baseline.get(source_key, {})
                if isinstance(raw, dict):
                    for otr_key, value in raw.items():
                        try:
                            otr_id = int(otr_key)
                            val = float(value)
                        except Exception:
                            continue
                        if np.isfinite(val) and val > 0:
                            data[source_key][otr_id] = val
        self.measurement_baseline = data

    def get_measurement_baseline(self) -> Dict[str, Dict[int, float]]:
        return {
            "conrad": {int(k): float(v) for k, v in self.measurement_baseline.get("conrad", {}).items()},
            "kek": {int(k): float(v) for k, v in self.measurement_baseline.get("kek", {}).items()},
        }

    def set_baseline_state(self, baseline_state: Dict[str, Any]) -> None:
        self.baseline_state = dict(baseline_state or {})
        self.baseline_currents = {}
        for knob_name, item in dict(self.baseline_state.get("magnet_state", {}) or {}).items():
            if not isinstance(item, dict):
                continue
            resolved_prefix = str(item.get("resolved_prefix", "") or self._resolve_magnet_prefix(knob_name))
            self.resolved_prefixes[str(knob_name)] = resolved_prefix
            base_current = _safe_float(item.get("current_write"), default=np.nan)
            if not np.isfinite(base_current):
                base_current = _safe_float(item.get("current_read"), default=np.nan)
            if not np.isfinite(base_current):
                base_current = _safe_float(item.get("current_rb"), default=np.nan)
            if not np.isfinite(base_current):
                base_current = self._read_current_value(resolved_prefix)
            if not np.isfinite(base_current):
                base_current = 0.0
            self.baseline_currents[str(knob_name)] = float(base_current)

        self.set_measurement_baseline(self.baseline_state.get("measurement_baseline"))

    def ensure_machine_origin(self, params: List[str]) -> Dict[str, Any]:
        if self.baseline_currents:
            return self.export_origin_state()

        magnet_state: Dict[str, Dict[str, float]] = {}
        for name in params:
            resolved_prefix = self._resolve_magnet_prefix(name)
            current_write = self.interface._pv_get(f"{resolved_prefix}:currentWrite", default=np.nan, timeout=0.7)
            current_read = self.interface._pv_get(f"{resolved_prefix}:currentRead", default=np.nan, timeout=0.7)
            current_rb = self.interface._pv_get(f"{resolved_prefix}:current", default=np.nan, timeout=0.7)
            base_current = _safe_float(current_write, default=np.nan)
            if not np.isfinite(base_current):
                base_current = _safe_float(current_read, default=np.nan)
            if not np.isfinite(base_current):
                base_current = _safe_float(current_rb, default=np.nan)
            if not np.isfinite(base_current):
                base_current = 0.0
            magnet_state[str(name)] = {
                "resolved_prefix": resolved_prefix,
                "current_write": _safe_float(current_write),
                "current_read": _safe_float(current_read),
                "current_rb": _safe_float(current_rb),
            }
            self.baseline_currents[str(name)] = float(base_current)

        self.baseline_state = {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "params": [str(p) for p in params],
            "objective_source": _objective_label(self.objective_source),
            "plot_both": bool(self.plot_both),
            "motr_ids": [int(v) for v in self.motr_ids],
            "magnet_state": magnet_state,
            "measurement_baseline": self.get_measurement_baseline(),
        }
        return self.export_origin_state()

    def export_origin_state(self) -> Dict[str, Any]:
        state = dict(self.baseline_state or {})
        magnet_state = dict(state.get("magnet_state", {}) or {})
        for knob_name, base_current in self.baseline_currents.items():
            item = dict(magnet_state.get(knob_name, {}) or {})
            resolved_prefix = str(item.get("resolved_prefix", "") or self._resolve_magnet_prefix(knob_name))
            magnet_state[knob_name] = {
                "resolved_prefix": resolved_prefix,
                "current_write": float(base_current),
                "current_read": float(self.interface._pv_get(f"{resolved_prefix}:currentRead", default=np.nan, timeout=0.7)),
                "current_rb": float(self.interface._pv_get(f"{resolved_prefix}:current", default=np.nan, timeout=0.7)),
            }
        state["magnet_state"] = magnet_state
        state["measurement_baseline"] = self.get_measurement_baseline()
        state["objective_source"] = _objective_label(self.objective_source)
        state["plot_both"] = bool(self.plot_both)
        state["motr_ids"] = [int(v) for v in self.motr_ids]
        return state

    def describe_machine_setpoint_channels(self, knob_names: List[str]) -> Dict[str, Any]:
        self.ensure_machine_origin(knob_names)
        channels = []
        initial = {}
        for name in knob_names:
            resolved_prefix = self._resolve_magnet_prefix(name)
            channel = f"{resolved_prefix}:currentWrite"
            channels.append(channel)
            initial[channel] = float(self.baseline_currents.get(str(name), 0.0))
        return {"channels": channels, "initial": initial}

    def compute_machine_setpoint_values(
        self,
        knob_values: Dict[str, float],
        *,
        knob_names: Optional[List[str]] = None,
    ) -> Dict[str, float]:
        names = [str(v) for v in (knob_names or list(knob_values.keys()))]
        self.ensure_machine_origin(names)
        values: Dict[str, float] = {}
        for name in names:
            resolved_prefix = self._resolve_magnet_prefix(name)
            base_current = float(self.baseline_currents.get(name, 0.0))
            offset = float(knob_values.get(name, 0.0))
            values[f"{resolved_prefix}:currentWrite"] = base_current + offset
        return values

    def apply_knobs(
        self,
        knob_values: Dict[str, float],
        *,
        current_tol: float = 0.05,
        timeout: float = 15.0,
        poll: float = 0.05,
    ) -> None:
        self.ensure_machine_origin(list(knob_values.keys()))
        targets: Dict[str, float] = {}
        prefix_for_knob: Dict[str, str] = {}
        for knob_name, offset in knob_values.items():
            resolved_prefix = self._resolve_magnet_prefix(knob_name)
            prefix_for_knob[str(knob_name)] = resolved_prefix
            target = float(self.baseline_currents.get(str(knob_name), 0.0)) + float(offset)
            targets[str(knob_name)] = target
            self.interface._pv_put(f"{resolved_prefix}:currentWrite", target)

        deadline = time.time() + float(timeout)
        pending = set(targets.keys())
        last_readback: Dict[str, float] = {}
        while pending:
            done = []
            for knob_name in list(pending):
                resolved_prefix = prefix_for_knob[knob_name]
                rb = self._readback_value(resolved_prefix)
                last_readback[knob_name] = float(rb)
                if np.isfinite(rb) and abs(float(rb) - float(targets[knob_name])) <= float(current_tol):
                    done.append(knob_name)
            for knob_name in done:
                pending.remove(knob_name)
            if not pending:
                break
            if time.time() >= deadline:
                zero_drop = [
                    knob_name
                    for knob_name in pending
                    if abs(float(targets[knob_name])) > float(current_tol)
                    and np.isfinite(last_readback.get(knob_name, np.nan))
                    and abs(float(last_readback.get(knob_name, np.nan))) <= float(current_tol)
                ]
                if zero_drop:
                    raise CurrentDropToZeroError(
                        "Current readback dropped near 0 A after quadrupole current apply.",
                        target={name: float(targets[name]) for name in zero_drop},
                        readback={name: float(last_readback.get(name, np.nan)) for name in zero_drop},
                        magnets=list(zero_drop),
                    )
                raise TimeoutError(f"Timed out waiting for current readback. pending={sorted(pending)}")
            time.sleep(float(poll))

    def restore_machine_origin(
        self,
        origin_state: Dict[str, Any],
        *,
        current_tol: float = 0.05,
        timeout: float = 15.0,
        poll: float = 0.05,
    ) -> None:
        state = dict(origin_state or {})
        magnet_state = dict(state.get("magnet_state", {}) or {})
        targets: Dict[str, float] = {}
        prefixes: Dict[str, str] = {}
        for knob_name, item in magnet_state.items():
            if not isinstance(item, dict):
                continue
            resolved_prefix = str(item.get("resolved_prefix", "") or self._resolve_magnet_prefix(knob_name))
            target = _safe_float(item.get("current_write"), default=np.nan)
            if not np.isfinite(target):
                continue
            prefixes[str(knob_name)] = resolved_prefix
            targets[str(knob_name)] = float(target)
            self.interface._pv_put(f"{resolved_prefix}:currentWrite", float(target))

        deadline = time.time() + float(timeout)
        pending = set(targets.keys())
        while pending:
            done = []
            for knob_name in list(pending):
                rb = self._readback_value(prefixes[knob_name])
                if np.isfinite(rb) and abs(float(rb) - float(targets[knob_name])) <= float(current_tol):
                    done.append(knob_name)
            for knob_name in done:
                pending.remove(knob_name)
            if not pending:
                break
            if time.time() >= deadline:
                raise TimeoutError(f"Restore current timeout. pending={sorted(pending)}")
            time.sleep(float(poll))

        self.set_baseline_state(state)

    def _extract_sigma_map(self, measurement_results: Dict[int, Dict[str, Any]], source: str) -> Dict[int, float]:
        norm_source = _normalize_objective_source(source)
        sigma_map: Dict[int, float] = {}
        for otr_id in self.motr_ids:
            result = measurement_results.get(int(otr_id), {})
            if norm_source == "kek":
                sigma_val = _safe_float(result.get("kek", {}).get("size_v_mean"), default=np.nan)
            else:
                sigma_val = _safe_float(result.get("conrad", {}).get("sigma_v_um"), default=np.nan)
            sigma_map[int(otr_id)] = float(sigma_val)
        return sigma_map

    def _maybe_seed_measurement_baseline(self, measurement_results: Dict[int, Dict[str, Any]]) -> None:
        for source in ("conrad", "kek"):
            sigma_map = self._extract_sigma_map(measurement_results, source)
            target = dict(self.measurement_baseline.get(source, {}) or {})
            changed = False
            for otr_id, sigma_val in sigma_map.items():
                if otr_id not in target and np.isfinite(sigma_val) and sigma_val > 0:
                    target[otr_id] = float(sigma_val)
                    changed = True
            if changed:
                self.measurement_baseline[source] = target

    def _objective_from_sigma_map(self, source: str, sigma_map: Dict[int, float]) -> float:
        baseline = dict(self.measurement_baseline.get(_normalize_objective_source(source), {}) or {})
        terms = []
        for otr_id in self.motr_ids:
            current_sigma = float(sigma_map.get(int(otr_id), np.nan))
            init_sigma = float(baseline.get(int(otr_id), np.nan))
            if not np.isfinite(current_sigma) or current_sigma <= 0 or not np.isfinite(init_sigma) or init_sigma <= 0:
                return float("nan")
            terms.append((current_sigma / init_sigma) ** 2)
        return float(np.sqrt(np.sum(terms)))

    def _objective_error(
        self,
        measurement_results: Dict[int, Dict[str, Any]],
        source: str,
        objective_value: float,
    ) -> float:
        norm_source = _normalize_objective_source(source)
        if not np.isfinite(objective_value) or objective_value <= 0:
            return float("nan")
        if norm_source != "kek":
            return 0.0
        baseline = dict(self.measurement_baseline.get("kek", {}) or {})
        variance_terms = []
        for otr_id in self.motr_ids:
            result = measurement_results.get(int(otr_id), {})
            sigma_mean = _safe_float(result.get("kek", {}).get("size_v_mean"), default=np.nan)
            sigma_std = _safe_float(result.get("kek", {}).get("size_v_std"), default=np.nan)
            init_sigma = _safe_float(baseline.get(int(otr_id)), default=np.nan)
            if not np.isfinite(sigma_mean) or sigma_mean <= 0 or not np.isfinite(init_sigma) or init_sigma <= 0:
                return float("nan")
            if not np.isfinite(sigma_std) or sigma_std < 0:
                sigma_std = 0.0
            ratio = float(sigma_mean / init_sigma)
            partial = float(ratio / objective_value)
            ratio_std = float(sigma_std / init_sigma)
            variance_terms.append((partial * ratio_std) ** 2)
        return float(np.sqrt(np.sum(variance_terms)))

    def _save_measurement_file(self, measurement_results: Dict[int, Dict[str, Any]]) -> str:
        if self.measurement_output_dir is None:
            return ""
        filename = f"measurement-{self.measurement_counter:04d}-{now_tag()}.npz"
        path = self.measurement_output_dir / filename
        _MOTR_MEAS.save_measurement_set_npz(str(path), measurement_results)
        return str(path)

    def _build_flat_dat(
        self,
        measurement_results: Dict[int, Dict[str, Any]],
        *,
        measurement_file: str,
        objective_conrad: float,
        objective_conrad_err: float,
        objective_kek: float,
        objective_kek_err: float,
        objective_selected: float,
        objective_selected_err: float,
        average_intensity: float,
    ) -> Dict[str, Any]:
        dat: Dict[str, Any] = {
            "modulation": _objective_to_score(objective_selected),
            "error": _score_err_from_objective(objective_selected, objective_selected_err),
            "beamsize": float(objective_selected),
            "ebeamsize": float(objective_selected_err) if np.isfinite(objective_selected_err) else float("nan"),
            "average": float(average_intensity),
            "phase": 0.0,
            "filename": measurement_file,
            "ict_average": float("nan"),
            "objective_source": _objective_label(self.objective_source),
            "plot_both": int(self.plot_both),
            "objective_selected": float(objective_selected),
            "objective_selected_err": float(objective_selected_err),
            "objective_conrad": float(objective_conrad),
            "objective_conrad_err": float(objective_conrad_err),
            "objective_kek": float(objective_kek),
            "objective_kek_err": float(objective_kek_err),
            "measurement_counter": int(self.measurement_counter),
            "measurement_timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "measurement_file": measurement_file,
        }
        sample_count = max(1, int(self.measurement_settings.get("kek_samples", 3)))
        for otr_id in self.motr_ids:
            prefix = f"otr{int(otr_id)}"
            result = dict(measurement_results.get(int(otr_id), {}) or {})
            conrad = dict(result.get("conrad", {}) or {})
            kek = dict(result.get("kek", {}) or {})
            dat[f"{prefix}_total_intensity"] = float(result.get("total_intensity", np.nan))
            dat[f"{prefix}_conrad_sigma_h_um"] = float(conrad.get("sigma_h_um", np.nan))
            dat[f"{prefix}_conrad_sigma_v_um"] = float(conrad.get("sigma_v_um", np.nan))
            dat[f"{prefix}_conrad_sigma13_m2"] = float(conrad.get("sigma_13_m2", np.nan))
            dat[f"{prefix}_kek_size_h_mean"] = float(kek.get("size_h_mean", np.nan))
            dat[f"{prefix}_kek_size_v_mean"] = float(kek.get("size_v_mean", np.nan))
            dat[f"{prefix}_kek_size_h_std"] = float(kek.get("size_h_std", np.nan))
            dat[f"{prefix}_kek_size_v_std"] = float(kek.get("size_v_std", np.nan))
            dat[f"{prefix}_kek_center_h"] = float(kek.get("center_h", np.nan))
            dat[f"{prefix}_kek_center_v"] = float(kek.get("center_v", np.nan))
            dat[f"{prefix}_calibration_h_um_per_px"] = float(kek.get("calibration_h_um_per_px", np.nan))
            dat[f"{prefix}_calibration_v_um_per_px"] = float(kek.get("calibration_v_um_per_px", np.nan))
            dat[f"{prefix}_baseline_conrad_sigma_v"] = float(
                self.measurement_baseline.get("conrad", {}).get(int(otr_id), np.nan)
            )
            dat[f"{prefix}_baseline_kek_sigma_v"] = float(
                self.measurement_baseline.get("kek", {}).get(int(otr_id), np.nan)
            )
            size_h_samples = np.asarray(kek.get("size_h_samples", []), dtype=float).reshape(-1)
            size_v_samples = np.asarray(kek.get("size_v_samples", []), dtype=float).reshape(-1)
            for sample_idx in range(sample_count):
                h_value = size_h_samples[sample_idx] if sample_idx < size_h_samples.size else float("nan")
                v_value = size_v_samples[sample_idx] if sample_idx < size_v_samples.size else float("nan")
                dat[f"{prefix}_kek_size_h_sample{sample_idx + 1}"] = float(h_value)
                dat[f"{prefix}_kek_size_v_sample{sample_idx + 1}"] = float(v_value)
        return dat

    def get_latest_measurement(self) -> Dict[str, Any]:
        return dict(self.latest_measurement or {})

    def get_ipbsm_full(self) -> Dict[str, Any]:
        self.measurement_counter += 1
        measurement_results = _MOTR_MEAS.measure_motr_set(
            otr_ids=self.motr_ids,
            min_total_intensity=float(self.measurement_settings.get("min_total_intensity", 130000.0)),
            max_retries=int(self.measurement_settings.get("max_retries", 3)),
            background_frames=int(self.measurement_settings.get("background_frames", 10)),
            beam_frames=int(self.measurement_settings.get("beam_frames", 5)),
            background_acquire_wait_sec=float(self.measurement_settings.get("background_wait_sec", 1.0)),
            beam_acquire_wait_sec=float(self.measurement_settings.get("beam_wait_sec", 3.0)),
            select_wait_sec=float(self.measurement_settings.get("select_wait_sec", 1.0)),
            retract_wait_sec=float(self.measurement_settings.get("retract_wait_sec", 5.0)),
            insert_wait_sec=float(self.measurement_settings.get("insert_wait_sec", 5.0)),
            retry_wait_sec=float(self.measurement_settings.get("retry_wait_sec", 1.0)),
            kek_samples=int(self.measurement_settings.get("kek_samples", 3)),
            kek_sample_interval_sec=float(self.measurement_settings.get("kek_sample_interval_sec", 1.0)),
        )

        self._maybe_seed_measurement_baseline(measurement_results)

        sigma_conrad = self._extract_sigma_map(measurement_results, "conrad")
        sigma_kek = self._extract_sigma_map(measurement_results, "kek")
        objective_conrad = self._objective_from_sigma_map("conrad", sigma_conrad)
        objective_kek = self._objective_from_sigma_map("kek", sigma_kek)
        objective_conrad_err = self._objective_error(measurement_results, "conrad", objective_conrad)
        objective_kek_err = self._objective_error(measurement_results, "kek", objective_kek)

        if self.objective_source == "kek":
            objective_selected = float(objective_kek)
            objective_selected_err = float(objective_kek_err)
        else:
            objective_selected = float(objective_conrad)
            objective_selected_err = float(objective_conrad_err)

        if not np.isfinite(objective_selected) or objective_selected <= 0:
            objective_selected = 1e6
            objective_selected_err = 0.0

        average_intensity = _nanmean(
            [float(measurement_results[int(otr_id)]["total_intensity"]) for otr_id in self.motr_ids]
        )
        measurement_file = self._save_measurement_file(measurement_results)
        dat = self._build_flat_dat(
            measurement_results,
            measurement_file=measurement_file,
            objective_conrad=objective_conrad,
            objective_conrad_err=objective_conrad_err,
            objective_kek=objective_kek,
            objective_kek_err=objective_kek_err,
            objective_selected=objective_selected,
            objective_selected_err=objective_selected_err,
            average_intensity=average_intensity,
        )
        self.latest_measurement = {
            "counter": int(self.measurement_counter),
            "objective_source": _objective_label(self.objective_source),
            "objective_selected": float(objective_selected),
            "objective_selected_err": float(objective_selected_err),
            "objective_conrad": float(objective_conrad),
            "objective_conrad_err": float(objective_conrad_err),
            "objective_kek": float(objective_kek),
            "objective_kek_err": float(objective_kek_err),
            "average_intensity": float(average_intensity),
            "measurement_file": measurement_file,
            "measurement_baseline": self.get_measurement_baseline(),
            "results": measurement_results,
            "flat_dat": dict(dat),
        }
        return dat


class MOTROptimizer(BaseOptimizer):
    def _extra_csv_columns(self) -> List[str]:
        return _motr_extra_csv_columns(self.cfg)

    def _bo1d_display_y_label(self) -> str:
        return f"{_objective_label(getattr(self.cfg, 'objective_source', 'Conrad'))} objective"

    def _bo1d_display_direction(self) -> str:
        return "minimize"

    def _bo1d_display_note(self) -> str:
        return "Surrogate mean/std are converted from the BO score used internally: score = 1 / objective."

    def _bo1d_display_values_from_records(self, records: List[StepRecord]) -> np.ndarray:
        values: List[float] = []
        for rec in list(records or []):
            dat = dict(getattr(rec, "dat", {}) or {})
            values.append(_safe_float(dat.get("objective_selected", dat.get("beamsize", np.nan)), default=np.nan))
        return np.asarray(values, dtype=float)

    def _bo1d_model_to_display(self, mu: np.ndarray, std: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        mu_arr = np.asarray(mu, dtype=float)
        std_arr = np.asarray(std, dtype=float)
        safe_mu = np.where(np.isfinite(mu_arr) & (mu_arr > 1e-12), mu_arr, np.nan)
        disp_mean = np.full(mu_arr.shape, np.nan, dtype=float)
        disp_std = np.full(std_arr.shape, np.nan, dtype=float)
        mask = np.isfinite(safe_mu)
        disp_mean[mask] = 1.0 / safe_mu[mask]
        disp_std[mask] = std_arr[mask] / np.maximum(safe_mu[mask] ** 2, 1e-18)
        return disp_mean, disp_std

    def _csv_header(self) -> List[str]:
        return (
            ["step", "t_iso"]
            + self.cfg.params
            + ["modulation", "mod_err"]
            + list(_BASE_OPT.DAT_CSV_COLUMNS)
            + self._extra_csv_columns()
            + self._machine_state_csv_columns()
            + ["chosen_by"]
        )

    def _csv_row_for_record(self, rec: StepRecord) -> List[Any]:
        x_q = [self._quantize_knob(rec.x[p], p) for p in self.cfg.params]
        dat = dict(getattr(rec, "dat", {}) or {})
        return (
            [rec.step, rec.t_iso]
            + x_q
            + [
                rec.y,
                rec.y_err,
                dat.get("modulation", float("nan")),
                dat.get("error", float("nan")),
                dat.get("beamsize", float("nan")),
                dat.get("ebeamsize", float("nan")),
                dat.get("average", float("nan")),
                dat.get("phase", float("nan")),
                dat.get("filename", ""),
                dat.get("ict_average", float("nan")),
            ]
            + [dat.get(col, "") for col in self._extra_csv_columns()]
            + self._machine_state_row_values(rec)
            + [rec.chosen_by]
        )

    def _log_step(self, rec: StepRecord):
        self._attach_machine_state_to_record(rec)
        self.records.append(rec)
        header = self._csv_header()
        row = self._csv_row_for_record(rec)
        csv_path = self.measurements_csv_path
        is_new = not csv_path.exists()
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(header)
            writer.writerow(row)

    def _rewrite_measurements_csv(self) -> None:
        csv_path = self.measurements_csv_path
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(self._csv_header())
            for rec in self.records:
                self._attach_machine_state_to_record(rec)
                writer.writerow(self._csv_row_for_record(rec))

    def _update_machine_origin_payload(self, path: Path) -> None:
        payload: Dict[str, Any] = {}
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception:
                payload = {}
        export_origin = getattr(self.controller, "export_origin_state", None)
        if callable(export_origin):
            try:
                payload = dict(export_origin())
            except Exception:
                pass
        payload["measurement_baseline"] = getattr(self.controller, "get_measurement_baseline", lambda: {})()
        payload["objective_source"] = _objective_label(getattr(self.controller, "objective_source", "conrad"))
        payload["plot_both"] = bool(getattr(self.controller, "plot_both", True))
        payload["motr_ids"] = [int(v) for v in getattr(self.cfg, "motr_ids", DEFAULT_MOTR_IDS)]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    def run(self) -> Dict[str, Any]:
        out = super().run()
        out["objective_source"] = _objective_label(getattr(self.controller, "objective_source", "conrad"))
        out["plot_both"] = bool(getattr(self.controller, "plot_both", True))
        measurement_baseline = getattr(self.controller, "get_measurement_baseline", lambda: {})()
        out["measurement_baseline"] = measurement_baseline
        final_score = _safe_float(out.get("best_y"), default=np.nan)
        out["best_objective_selected"] = float(1.0 / final_score) if np.isfinite(final_score) and final_score > 0 else float("nan")

        for path in (self.machine_origin_path, self.machine_origin_tagged_path):
            try:
                self._update_machine_origin_payload(path)
            except Exception:
                pass

        with open(self.out_dir / "result.json", "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        return out
