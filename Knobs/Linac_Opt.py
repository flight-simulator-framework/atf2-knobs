# -*- coding: utf-8 -*-
"""
Liniac_Opt.py

Linac auto tuning GUI + worker.

Improvements (2026-01):
- Use BIM:L0_nparticles as upstream and selectable downstream ICT (LN0/DR/GUN/LNE/BTM/BTE) for transmission.
- Log additional ICTs (GUN/LNE/BTM/BTE) at every evaluation point.
- Always wait settle_sec (default 5s) after changing any PV before reading ICTs.
-nparticles Timing scan after klystron phase scan:
    PV: EVE_LINAC:OUT0:SetData  [ns], step=11.2ns, range=±15 steps around current
- Add Group-LBO mode (discrete / quantized to the same step sizes as sequential scans).
  Groups: L1–L4, L5–L8, Timing, QA, QM (only checked groups run, in this order).
"""

import sys
import time
import datetime
import csv
import math
import json
import itertools
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Tuple, Optional

import numpy as np

_KNOBS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _KNOBS_DIR.parent
for _path in (str(_KNOBS_DIR), str(_REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from PyQt6.QtCore import QThread, QTimer, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QCheckBox, QLabel, QFileDialog, QTextEdit, QGroupBox,
    QMessageBox, QDoubleSpinBox, QSpinBox, QComboBox, QTabWidget, QSizePolicy, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QStackedWidget, QScrollArea
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from machine_interfaces_atf2.InterfaceATF2_LinacBT import InterfaceATF2_LinacBT, BT_SEQUENCE
from machine_interfaces_atf2.InterfaceATF2_DR import DRBPMReader, DR_BPM_NAMES, DRBPMTurnControlError
from linac_gp import CorrectorGPModel

LINAC_BT_BPM_NAMES = tuple(dict.fromkeys(name for name in BT_SEQUENCE if not name.lower().startswith("z")))
INTENSITY_BPM_NAMES = tuple(DR_BPM_NAMES) + LINAC_BT_BPM_NAMES
# Display order only: preserve acquisition/selection order and rotate the ring
# so that MB21R is the first DR monitor (wrap to MB1R after MB98R).
INTENSITY_PLOT_BPM_NAMES = (
    LINAC_BT_BPM_NAMES
    + tuple(DR_BPM_NAMES[DR_BPM_NAMES.index("MB21R"):])
    + tuple(DR_BPM_NAMES[:DR_BPM_NAMES.index("MB21R")])
)

FLIGHT_SIMULATOR_DATA_ROOT = Path("/atf/data/flight-simulator")


def default_linacopt_save_dir(year: Optional[str] = None) -> Path:
    year_text = str(year or datetime.datetime.now().strftime("%Y"))
    return FLIGHT_SIMULATOR_DATA_ROOT / "LinacOpt" / year_text


# ----------------------------
# Scan defaults (same values used by Sequential and Group-LBO)
# ----------------------------
PHASE_HALF_RANGE_DEG = 8.0
PHASE_STEP_DEG = 1.0

GUN_PHASE_HALF_RANGE_DEG = 5.0
GUN_PHASE_STEP_DEG = 0.1
L0_PHASE_HALF_RANGE_DEG = 5.0
L0_PHASE_STEP_DEG = 0.1
SOLENOIDE_MIN_A = 80.0
SOLENOIDE_MAX_A = 125.0
SOLENOIDE_STEP_A = 0.5
SOLENOIDE_WRITE_PV = "SOLENOIDE:currentWrite"
SOLENOIDE_READ_PV = "SOLENOIDE:internalCurrentWrite"

QA_HALF_RANGE_A = 1.0
QA_STEP_A = 0.05

QM_HALF_RANGE_A = 2.0
QM_STEP_A = 0.2

TIMING_STEP_NS = 11.2
TIMING_HALF_STEPS = 10  # ±15 steps

SETTLE_SEC_DEFAULT = 5.0
ICT_SAMPLES_DEFAULT = 3
ICT_SAMPLE_INTERVAL_S_DEFAULT = 0.5
ICT_MAX_RETRIES_PER_SAMPLE_DEFAULT = 5
ICT_RETRY_WAIT_S_DEFAULT = 0.5
DEVELOPER_DEFAULT_HALF_RANGE_A = 0.5
DEVELOPER_DEFAULT_STEP_A = 0.05
STEER_CURRENT_MIN_A = -5.0
STEER_CURRENT_MAX_A = 5.0
RUN_PROFILE_MAIN = "MAIN"
RUN_PROFILE_DEVELOPER = "DEVELOPER"
DEVELOPER_OBJECTIVE_ICT = "ICT"
DEVELOPER_OBJECTIVE_BPM = "BPM_SQSUM"
DEVELOPER_OBJECTIVE_INTENSITY = "BPM_INTENSITY"


def evaluation_phase(note):
    tags = str(note or "").split("|")
    if any(tag in ("GBO_INIT_CUR", "GBO_INIT_RAND", "BO_INIT", "REUSE_INIT") for tag in tags):
        return "Initial"
    if any(tag in ("GBO_EI", "BO_EI", "BO_REFINE") for tag in tags):
        return "BO"
    return ""


def shade_evaluation_phases(axis, indices, phases):
    """Shade actual acquisition phases, including restarts for later groups."""
    if len(indices) != len(phases) or len(indices) == 0:
        return
    colors = {"Initial": ("#d8ecff", "#355c7d"), "BO": ("#fff0cc", "#8a5a12")}
    start = 0
    for end in range(1, len(indices) + 1):
        if end < len(indices) and phases[end] == phases[start]:
            continue
        phase = phases[start]
        if phase in colors:
            left = (indices[start - 1] + indices[start]) / 2 if start else indices[0] - .5
            right = (indices[end - 1] + indices[end]) / 2 if end < len(indices) else indices[-1] + .5
            color, text_color = colors[phase]
            axis.axvspan(left, right, color=color, alpha=.35, lw=0, zorder=0)
            axis.text((left + right) / 2, .98, phase, transform=axis.get_xaxis_transform(),
                      ha="center", va="top", fontsize=8, color=text_color)
        start = end


def bpm_intensity_values(bpms, names, min_samples=1, check_status=True):
    """Require all selected data; status validation is optional after preflight."""
    available = list(bpms.get("names", []))
    raw = np.asarray(bpms.get("intensity", []), dtype=float)
    status = np.asarray(bpms.get("status", []), dtype=float) if check_status else None
    if (not names or raw.ndim != 2 or raw.shape[0] < min_samples
            or (check_status and raw.shape != status.shape)):
        raise ValueError("BPM intensity/status data missing or incomplete; update machine-interfaces.")
    result = {}
    for name in names:
        if name not in available:
            raise ValueError(f"Missing selected BPM: {name}")
        i = available.index(name)
        if i >= raw.shape[1] or not np.all(np.isfinite(raw[:, i])):
            raise ValueError(f"Missing or non-finite intensity at {name}")
        if check_status and not np.all(status[:, i] == 1):
            raise ValueError(f"Invalid status at {name}")
        result[name] = float(np.mean(raw[:, i]))
    return result


def bpm_intensity_snapshot(data, check_status=True):
    """Display all monitors, retaining invalid ones as gaps rather than zeros."""
    values = {}
    for name in data.get("names", []):
        try:
            values[name] = bpm_intensity_values(data, [name], check_status=check_status)[name]
        except ValueError:
            values[name] = float("nan")
    return values


def read_bpm_intensities(interface, names, log=lambda message: None, baseline=False, snapshot_out=None,
                         check_status=True):
    error = "No measurement"
    for attempt in range(3):
        try:
            previous_samples = interface.nsamples
            try:
                interface.nsamples = max(3, int(previous_samples))
                data = interface.get_bpms()
                values = bpm_intensity_values(data, names, min_samples=interface.nsamples, check_status=check_status)
            finally:
                interface.nsamples = previous_samples
            if baseline:
                for name, value in values.items():
                    samples = np.asarray(data["intensity"])[:, list(data["names"]).index(name)]
                    if value <= max(1e-12, 5 * float(np.std(samples))):
                        raise ValueError(f"Baseline too small/noisy for relative intensity: {name}; use raw mean.")
            if snapshot_out is not None:
                snapshot_out.clear()
                snapshot_out.update(bpm_intensity_snapshot(data, check_status=check_status))
            return values
        except (DRBPMTurnControlError, InterruptedError):
            raise
        except (ValueError, RuntimeError, TypeError, IndexError) as exc:
            error = str(exc)
            log(f"BPM measurement retry {attempt + 1}/3: {error}")
    raise RuntimeError(f"BPM measurement invalid; optimization stopped: {error}")


def configure_dr_turns(reader, config, cancelled=None):
    reader.configure_orbit_mode(config.get("dr_orbit_mode", 2))
    if config.get("intensity_aggregation") in ("turn_ratio", "turn_composite"):
        reader.configure_turn_scan(True, 1, config.get("dr_ratio_turn", 10), "mean",
                                   cancelled=cancelled, endpoints_only=True)
        return
    reader.configure_turn_scan(
        enabled=bool(config.get("dr_turn_scan", False)),
        start=config.get("dr_turn_start", 1), end=config.get("dr_turn_end", 10),
        aggregation=config.get("dr_turn_aggregation", "mean"), cancelled=cancelled)


def dr_turn_label(config):
    if config.get("intensity_aggregation") in ("turn_ratio", "turn_composite"):
        return f"DR intensity: turn {config.get('dr_ratio_turn', 10)}; objective uses N / 1"
    if not config.get("dr_turn_scan", False):
        return ""
    return (f"DR turns {config.get('dr_turn_start', 1)}–{config.get('dr_turn_end', 10)}: "
            f"{config.get('dr_turn_aggregation', 'mean')}")


def intensity_score(values, baseline, aggregation):
    if not values or not np.all(np.isfinite(list(values.values()))):
        raise ValueError("Empty or non-finite BPM intensity values")
    if aggregation == "raw_mean":
        return float(np.mean(list(values.values())))
    if aggregation == "raw_sum":
        return float(np.sum(list(values.values())))
    if aggregation != "relative_mean":
        raise ValueError("Unknown intensity aggregation")
    # No universal calibrated noise floor exists. Acquisition separately rejects
    # means within five sample standard deviations of zero.
    if any(n not in baseline or not np.isfinite(baseline[n]) or baseline[n] <= 1e-12 for n in values):
        raise ValueError("Relative intensity needs a positive, non-zero baseline for every BPM.")
    return float(np.mean([v / baseline[n] for n, v in values.items()]))


def read_turn_objective(reader, names, config, baseline=None, check_status=True, plot_out=None):
    """Ratio of per-turn sample means, then equal-weight mean across DR BPMs."""
    if not names or any(name not in DR_BPM_NAMES for name in names):
        raise ValueError("Turn transmission objectives require DR BPMs only.")
    target = int(config.get("dr_ratio_turn", 10))
    floor = float(config.get("dr_ratio_floor", 1e-12))
    weight = float(config.get("dr_current_weight", 1.0))
    if target < 2 or not np.isfinite([floor, weight]).all() or floor <= 0 or weight < 0:
        raise ValueError("Require N >= 2, positive intensity floor and non-negative finite weight.")
    previous = reader.nsamples
    try:
        reader.nsamples = max(3, int(previous))
        data = reader.get_bpms()
    finally:
        reader.nsamples = previous
    first_data, last_data = data["turn_data"][1], data["turn_data"][target]
    # Finite low intensity is a measured loss, not a missing measurement.
    first = bpm_intensity_values(first_data, names, min_samples=3, check_status=False)
    last = bpm_intensity_values(last_data, names, min_samples=3, check_status=False)
    lost = {name for name in names if first[name] <= floor}
    valid_names = [name for name in names if name not in lost]
    if check_status and valid_names:
        bpm_intensity_values(first_data, valid_names, min_samples=3, check_status=True)
        bpm_intensity_values(last_data, valid_names, min_samples=3, check_status=True)
    for name in names:
        if name in lost:
            first[name] = 0.
            continue
    transmission = float(np.mean([0. if name in lost else last[name] / first[name] for name in names]))
    initial = first if baseline is None else baseline
    if any(n not in initial or not np.isfinite(initial[n]) or initial[n] < 0 for n in names):
        raise ValueError("Invalid fixed turn 1 baseline; start a fresh run.")
    initial_mean = float(np.mean([initial[n] for n in names]))
    if initial_mean <= floor and config["intensity_aggregation"] == "turn_composite":
        raise ValueError("Initial meanCurrent is zero/too small; cannot normalize. Establish beam or use transmission-only mode.")
    normalized = float(np.mean(list(first.values())) / initial_mean) if initial_mean > floor else float("nan")
    score = transmission + weight * normalized if config["intensity_aggregation"] == "turn_composite" else transmission
    if not np.isfinite(score):
        raise ValueError("Non-finite turn transmission score")
    if plot_out is not None:
        first_snapshot = bpm_intensity_snapshot(first_data, check_status=check_status)
        last_snapshot = bpm_intensity_snapshot(last_data, check_status=check_status)
        raw_first_snapshot = bpm_intensity_snapshot(first_data, check_status=False)
        raw_last_snapshot = bpm_intensity_snapshot(last_data, check_status=False)
        ratios = {}
        for name, value in first_snapshot.items():
            if np.isfinite(raw_first_snapshot.get(name, np.nan)) and raw_first_snapshot[name] <= floor and np.isfinite(raw_last_snapshot.get(name, np.nan)):
                ratios[name] = 0.
                continue
            ratios[name] = (last_snapshot.get(name, np.nan) / value
                            if np.isfinite(value) and value > floor else float("nan"))
        plot_out.clear()
        plot_out.update(first=first_snapshot, last=last_snapshot, ratios=ratios,
                        transmission=transmission, normalized_current=normalized,
                        weight=weight, target=target, aggregation=config["intensity_aggregation"])
    return score, first, last, bpm_intensity_snapshot(last_data, check_status=check_status), transmission, normalized


def intensity_sources(names):
    return [source for source, available in (("LINAC:monitors", LINAC_BT_BPM_NAMES),
                                             ("DR:monitors", DR_BPM_NAMES))
            if any(name in available for name in names)]


def read_selected_bpm_intensities(linac, dr, names, log=lambda message: None,
                                 baseline=False, snapshot_out=None, check_status=True):
    """Read only selected source regions; mixed sources are sampled sequentially.

    Each source supplies three samples at 0.5 s intervals. All selected monitors
    must supply finite data; status is checked at preflight, not during scans.
    """
    unknown = set(names) - set(INTENSITY_BPM_NAMES)
    if not names or unknown:
        raise ValueError(f"Empty or unknown intensity BPM selection: {sorted(unknown)}")
    values, snapshot = {}, {}
    for reader, available in ((linac, LINAC_BT_BPM_NAMES), (dr, DR_BPM_NAMES)):
        selected = [name for name in names if name in available]
        if not selected:
            continue
        previous_interval = getattr(reader, "bpm_sample_interval_s", None)
        reader.bpm_sample_interval_s = 0.5
        try:
            source_snapshot = {}
            values.update(read_bpm_intensities(reader, selected, log, baseline=baseline,
                                               snapshot_out=source_snapshot, check_status=check_status))
            snapshot.update(source_snapshot)
        finally:
            if previous_interval is None:
                del reader.bpm_sample_interval_s
            else:
                reader.bpm_sample_interval_s = previous_interval
    if snapshot_out is not None:
        snapshot_out.clear()
        snapshot_out.update(snapshot)
    return {name: values[name] for name in names}


DEVELOPER_GROUP_NAME = "Correctors"

TARGET_LABELS = {
    "gun_sol_l0": "GUN, Solenoid, L0",
    "kly_phase": "Klystron phase",
    "kly_group_1": "Group 1 (L1-L4)",
    "kly_group_2": "Group 2 (L5-L8)",
    "timing": "Timing",
    "qa": "QA",
    "qm": "QM",
    "correctors": "Correctors",
}
GROUP_BO_MAX_EVAL_SPECS = [
    {"config_id": "gun_sol_l0", "ui_label": TARGET_LABELS["gun_sol_l0"], "runtime_name": TARGET_LABELS["gun_sol_l0"], "dim": 3},
    {"config_id": "kly_group_1", "ui_label": TARGET_LABELS["kly_group_1"], "runtime_name": "L1-4", "dim": 4},
    {"config_id": "kly_group_2", "ui_label": TARGET_LABELS["kly_group_2"], "runtime_name": "L5-8", "dim": 4},
    {"config_id": "timing", "ui_label": TARGET_LABELS["timing"], "runtime_name": TARGET_LABELS["timing"], "dim": 1},
    {"config_id": "qa", "ui_label": TARGET_LABELS["qa"], "runtime_name": TARGET_LABELS["qa"], "dim": 5},
    {"config_id": "qm", "ui_label": TARGET_LABELS["qm"], "runtime_name": TARGET_LABELS["qm"], "dim": 3},
    # Corrector dimension is resolved from the current selection at runtime.
    {"config_id": "correctors", "ui_label": "Correctors", "runtime_name": "Correctors", "dim": 0},
]
GROUP_BO_RUNTIME_TO_SPEC = {
    str(spec["runtime_name"]): spec
    for spec in GROUP_BO_MAX_EVAL_SPECS
}
KLY_GROUP_1_SPECS = [
    (f"L{i}", f"CM{i}L:phaseWrite", f"CM{i}L:phaseRead", "deg")
    for i in range(1, 5)
]
KLY_GROUP_2_SPECS = [
    (f"L{i}", f"CM{i}L:phaseWrite", f"CM{i}L:phaseRead", "deg")
    for i in range(5, 9)
]
TARGET_VALUE_SPECS = {
    "gun_sol_l0": [
        ("GUN", "RFGUN:PHASE_WRITE", None, "deg"),
        ("Solenoid", SOLENOIDE_WRITE_PV, SOLENOIDE_READ_PV, "A"),
        ("L0", "CM0L:phaseWrite", None, "deg"),
    ],
    "kly_group_1": KLY_GROUP_1_SPECS,
    "kly_group_2": KLY_GROUP_2_SPECS,
    "timing": [("Timing", "EVE_LINAC:OUT0:SetData", None, "ns")],
    "qa": [(f"QA{i}L", f"QA{i}L:currentWrite", f"QA{i}L:currentRead", "A") for i in range(1, 6)],
    "qm": [(f"QM{i}L", f"QM{i}L:currentWrite", f"QM{i}L:currentRead", "A") for i in range(1, 4)],
}
RESTORE_PVS = [
                  "RFGUN:PHASE_WRITE",
                  SOLENOIDE_WRITE_PV,
                  "CM0L:phaseWrite",
                  "EVE_LINAC:OUT0:SetData",
              ] + [f"CM{i}L:phaseWrite" for i in range(1, 9)] + [f"QA{i}L:currentWrite" for i in range(1, 6)] + [
                  f"QM{i}L:currentWrite" for i in range(1, 4)
              ]

# Conservative ARD priors obtained from the BO2026 offline fit.  These are
# deliberately not the sharper ternary-only estimates: a production BO run
# needs a global prior that remains resolvable on the hardware grid.
LINAC_LEARNED_LENGTH_SCALES = {
    "RFGUN:PHASE_WRITE": 3.5,
    SOLENOIDE_WRITE_PV.upper(): 12.5,
    "CM0L:PHASEWRITE": 2.5,
    **{f"CM{i}L:PHASEWRITE": value for i, value in enumerate((4.6, 4.7, 4.1, 4.0, 4.6, 4.4, 4.5, 4.5), start=1)},
    "EVE_LINAC:OUT0:SETDATA": 22.4,
    **{f"QA{i}L:CURRENTWRITE": value for i, value in enumerate((0.50, 0.53, 0.44, 0.61, 0.82), start=1)},
    **{f"QM{i}L:CURRENTWRITE": value for i, value in enumerate((1.10, 1.19, 1.08), start=1)},
}
LINAC_HYPERPARAMETER_MODES = ("legacy_fixed", "learned_fixed")
LINAC_LEARNED_PRESET_VERSION = "BO2026-conservative-v1"


def _linac_axis_key(axis_name: str) -> str:
    """Return a stable lookup key for a PV or a human-readable LINAC axis."""
    text = str(axis_name or "").strip().upper().replace(" ", "")
    return text.replace("PHASEREAD", "PHASEWRITE").replace("CURRENTREAD", "CURRENTWRITE")


def resolve_linac_length_scale(
    axis_name: str,
    axis_range: float,
    axis_step: float,
    *,
    mode: str = "legacy_fixed",
    n_step_floor: float = 2.0,
) -> Tuple[float, str]:
    """Resolve a safe fixed GP length scale and record its source.

    Unknown/developer axes intentionally retain the historical range/3 rule.
    """
    safe_range = max(abs(float(axis_range)), abs(float(axis_step)), 1e-6)
    legacy = max(safe_range / 3.0, 1e-6)
    if str(mode).strip().lower() != "learned_fixed":
        return legacy, "legacy_range_over_3"
    learned = LINAC_LEARNED_LENGTH_SCALES.get(_linac_axis_key(axis_name))
    if learned is None:
        return legacy, "legacy_fallback_unknown_axis"
    floor = max(float(n_step_floor) * abs(float(axis_step)), 1e-6)
    return max(float(learned), floor), LINAC_LEARNED_PRESET_VERSION


def _format_machine_value(value: float, unit: str) -> str:
    if not np.isfinite(float(value)):
        return "-"
    unit = str(unit)
    if unit == "deg":
        return f"{float(value):+.1f} {unit}"
    if unit == "ns":
        return f"{float(value):+.1f} {unit}"
    return f"{float(value):.3f} {unit}"


def _format_delta_value(value: float, unit: str) -> str:
    if not np.isfinite(float(value)):
        return "-"
    unit = str(unit)
    if unit == "deg":
        return f"{float(value):+.1f} {unit}"
    if unit == "ns":
        return f"{float(value):+.1f} {unit}"
    return f"{float(value):+.3f} {unit}"


def _format_significant_value(value: float, digits: int = 4) -> str:
    if not np.isfinite(float(value)):
        return "-"
    return f"{float(value):.{max(1, int(digits))}g}"


GROUP_BO_BUDGET_PRESETS = {
    "quick": {"label": "Quick", "max_slope": 5, "max_offset": 10, "init_slope": 1, "init_offset": 4},
    "standard": {"label": "Standard", "max_slope": 10, "max_offset": 20, "init_slope": 2, "init_offset": 8},
    "explore": {"label": "Explore", "max_slope": 20, "max_offset": 40, "init_slope": 4, "init_offset": 12},
}


def _group_bo_preset(preset: str = "standard") -> Dict[str, Any]:
    return GROUP_BO_BUDGET_PRESETS.get(str(preset), GROUP_BO_BUDGET_PRESETS["standard"])


def _auto_group_bo_budget(ndim: int, preset: str = "standard") -> Dict[str, int]:
    dim = max(1, int(ndim))
    # Heuristic evaluation budgets, not convergence guarantees. Keep growing
    # with dimension instead of saturating at the old 18 / 60 point caps.
    spec = _group_bo_preset(preset)
    min_init = spec["init_slope"] * dim + spec["init_offset"]
    max_evals = spec["max_slope"] * dim + spec["max_offset"]
    candidate_pool = min(max(1024, 256 * dim), 4096)
    stall_iters = max(6, min(12, dim + 2))
    return {
        "min_init": int(min_init),
        "max_evals": int(max_evals),
        "candidate_pool": int(candidate_pool),
        "stall_iters": int(stall_iters),
    }


def _recommended_group_bo_max_evals(ndim: int, preset: str = "standard") -> int:
    dim = max(1, int(ndim))
    return int(_auto_group_bo_budget(dim, preset)["max_evals"])


def _group_bo_max_evals_config_key(config_id: str) -> str:
    return f"gbo_max_evals_{str(config_id)}"


def _resolve_group_bo_max_evals(config: Dict[str, Any], group_name: str, ndim: int) -> int:
    fallback = _recommended_group_bo_max_evals(ndim, config.get("gbo_budget_preset", "standard"))
    if group_name == DEVELOPER_GROUP_NAME and config.get("gbo_correctors_auto", False):
        return fallback
    spec = GROUP_BO_RUNTIME_TO_SPEC.get(str(group_name))
    if spec is not None:
        value = config.get(_group_bo_max_evals_config_key(str(spec["config_id"])))
        if value is not None:
            return max(1, int(value))
    value = config.get("gbo_max_evals")
    if value is not None:
        return max(1, int(value))
    return max(1, int(fallback))


def _default_readback_pv(pv_write: str) -> Optional[str]:
    if str(pv_write).strip() == SOLENOIDE_WRITE_PV:
        return SOLENOIDE_READ_PV
    return None


def build_display_value_texts(pv_values: Dict[str, float]) -> Dict[str, str]:
    texts: Dict[str, str] = {}
    for key, specs in TARGET_VALUE_SPECS.items():
        parts = []
        for label, pv_write, _pv_read, unit in specs:
            parts.append(f"{label}={_format_machine_value(pv_values.get(pv_write, float('nan')), unit)}")
        texts[key] = ", ".join(parts)
    return texts


def _main_run_delta_specs(config: Dict[str, Any]) -> List[Tuple[str, str, Optional[str], str]]:
    specs: List[Tuple[str, str, Optional[str], str]] = []
    if bool(config.get("gun_sol_l0_phase", False)):
        specs.extend(TARGET_VALUE_SPECS["gun_sol_l0"])
    if bool(config.get("kly_phase", False)):
        if str(config.get("mode", "")).upper() == "GROUP_BO":
            if bool(config.get("grp_L1_4", False)):
                specs.extend(KLY_GROUP_1_SPECS)
            if bool(config.get("grp_L5_8", False)):
                specs.extend(KLY_GROUP_2_SPECS)
        else:
            specs.extend(KLY_GROUP_1_SPECS)
            specs.extend(KLY_GROUP_2_SPECS)
    if bool(config.get("timing", False)):
        specs.extend(TARGET_VALUE_SPECS["timing"])
    if bool(config.get("qa", False)):
        specs.extend(TARGET_VALUE_SPECS["qa"])
    if bool(config.get("qm", False)):
        specs.extend(TARGET_VALUE_SPECS["qm"])

    deduped: List[Tuple[str, str, Optional[str], str]] = []
    seen_pvs = set()
    for label, pv_write, pv_read, unit in specs:
        if pv_write in seen_pvs:
            continue
        seen_pvs.add(pv_write)
        deduped.append((label, pv_write, pv_read, unit))
    return deduped


def _machine_region(name: str) -> str:
    text = str(name or "").upper()
    if text.endswith("L"):
        return "LINAC"
    if text.endswith("T"):
        return "BT"
    return "OTHER"


def _corrector_plane(name: str) -> str:
    text = str(name or "").lower()
    if text.startswith(("zh", "zx")):
        return "H"
    if text.startswith(("zv", "zy")):
        return "V"
    return "?"


def _developer_actuator_spec(name: str) -> Dict[str, Any]:
    text = str(name)
    return {
        "name": text,
        "label": text,
        "pv_write": f"{text}:currentWrite",
        "pv_read": f"{text}:currentRead",
        "plane": _corrector_plane(text),
        "region": _machine_region(text),
    }


def _trim_centered_bounds(
        center: float,
        half_range: float,
        lower: float,
        upper: float,
) -> Tuple[float, float, bool, float, float]:
    raw_lo = float(center) - max(float(half_range), 0.0)
    raw_hi = float(center) + max(float(half_range), 0.0)
    lo = max(raw_lo, float(lower))
    hi = min(raw_hi, float(upper))
    trimmed = (abs(lo - raw_lo) > 1e-12) or (abs(hi - raw_hi) > 1e-12)
    if hi < lo:
        clipped_center = float(np.clip(float(center), float(lower), float(upper)))
        lo = clipped_center
        hi = clipped_center
        trimmed = True
    return lo, hi, trimmed, raw_lo, raw_hi


def _scan_from_bounds(lo: float, hi: float, step: float) -> np.ndarray:
    lo_f = float(min(lo, hi))
    hi_f = float(max(lo, hi))
    step_f = max(float(step), 1e-12)
    if hi_f - lo_f <= step_f * 1e-9:
        return np.array([lo_f], dtype=float)
    scan = np.arange(lo_f, hi_f + step_f * 0.5, step_f, dtype=float)
    scan = np.unique(np.clip(scan, lo_f, hi_f))
    if scan.size == 0:
        return np.array([lo_f], dtype=float)
    if abs(float(scan[0]) - lo_f) > step_f * 1e-6:
        scan = np.insert(scan, 0, lo_f)
    if abs(float(scan[-1]) - hi_f) > step_f * 1e-6:
        scan = np.append(scan, hi_f)
    return np.unique(scan)


def _normalize_downstream_key(downstream_key: str) -> str:
    key = str(downstream_key or "DR").upper()
    return "L0" if key == "LN0" else key


def _validate_transmission_measurement(
        ict: Dict[str, float],
        downstream_key: str,
) -> Tuple[bool, str]:
    downstream_lookup = _normalize_downstream_key(downstream_key)
    downstream_label = "LN0" if downstream_lookup == "L0" else downstream_lookup
    l0 = float(ict.get("L0", float("nan")))
    downstream = float(ict.get(downstream_lookup, float("nan")))
    ttot = float(ict.get("Ttot", float("nan")))

    if not np.isfinite(l0) or l0 <= 0.0:
        return False, f"invalid L0 ICT: {l0:.6g}"
    if not np.isfinite(downstream) or downstream < 0.0:
        return False, f"invalid {downstream_label} ICT: {downstream:.6g}"
    if not np.isfinite(ttot):
        return False, f"invalid Transmission: Ttot={ttot}"
    if not (0.0 <= ttot <= 1.0):
        return False, (
            f"Transmission out of range: Ttot={ttot:.6f} "
            f"(L0={l0:.6g}, {downstream_label}={downstream:.6g})"
        )
    return True, ""


def _zero_ict_measurement() -> Dict[str, float]:
    return {
        "L0": 0.0,
        "DR": 0.0,
        "GUN": 0.0,
        "LNE": 0.0,
        "BTM": 0.0,
        "BTE": 0.0,
        "Ttot": 0.0,
    }


def _read_icts_with_retry(
        interface: InterfaceATF2_LinacBT,
        downstream_key: str,
        *,
        sample_count: int = ICT_SAMPLES_DEFAULT,
        sample_interval_s: float = ICT_SAMPLE_INTERVAL_S_DEFAULT,
        max_retries_per_sample: int = ICT_MAX_RETRIES_PER_SAMPLE_DEFAULT,
        retry_wait_s: float = ICT_RETRY_WAIT_S_DEFAULT,
        log_fn: Optional[Callable[[str], None]] = None,
) -> Tuple[Dict[str, float], bool, str]:
    valid_samples: List[Dict[str, float]] = []
    samples_needed = max(1, int(sample_count))
    retries_per_sample = max(1, int(max_retries_per_sample))
    normalized_downstream = _normalize_downstream_key(downstream_key)
    last_reason = "unknown ICT read failure"

    for sample_idx in range(1, samples_needed + 1):
        accepted = False
        for attempt in range(1, retries_per_sample + 1):
            ict = interface.read_icts_for_optimizer(
                downstream_key=normalized_downstream,
                dr_samples=1,
                dr_interval_s=0.0,
            )
            valid, reason = _validate_transmission_measurement(ict, downstream_key)
            if valid:
                valid_samples.append(ict)
                accepted = True
                break

            last_reason = reason
            if log_fn is not None:
                msg = (
                    f"[ICT] Rejected shot {sample_idx}/{samples_needed} "
                    f"(attempt {attempt}/{retries_per_sample}): {reason}"
                )
                if attempt < retries_per_sample:
                    msg += " Retrying shot..."
                log_fn(msg)

            if attempt < retries_per_sample:
                time.sleep(max(0.0, float(retry_wait_s)))

        if not accepted:
            final_reason = (
                f"ICT read failed at shot {sample_idx}/{samples_needed} after "
                f"{retries_per_sample} attempts: {last_reason}"
            )
            if log_fn is not None:
                log_fn(final_reason)
                log_fn("[ICT] Falling back to zero Transmission/current score for this evaluation.")
            return _zero_ict_measurement(), False, final_reason

        if sample_idx < samples_needed:
            time.sleep(max(0.0, float(sample_interval_s)))

    out: Dict[str, float] = {}
    value_keys = ("L0", "DR", "GUN", "LNE", "BTM", "BTE")
    for key in value_keys:
        vals = [float(sample.get(key, float("nan"))) for sample in valid_samples]
        out[key] = float(np.nanmean(vals)) if any(np.isfinite(vals)) else float("nan")

    l0 = out.get("L0", float("nan"))
    downstream = out.get(normalized_downstream, float("nan"))
    if np.isfinite(l0) and l0 != 0.0 and np.isfinite(downstream):
        out["Ttot"] = float(downstream / l0)
    else:
        out["Ttot"] = float("nan")
    return out, True, ""


@dataclass
class EvalResult:
    t_iso: str
    device_label: str
    pv_name: str
    set_value: float
    ict: Dict[str, float]  # L0/DR/GUN/LNE/BTM/BTE + Ttot
    score: float
    objective_type: str = DEVELOPER_OBJECTIVE_ICT
    objective_label: str = ""
    objective_value: float = float("nan")
    bpm_metric: float = float("nan")
    measurement_ok: bool = True
    measurement_reason: str = ""
    note: str = ""
    group: str = ""
    mode: str = ""


class OptimizationWorker(QThread):
    log_signal = pyqtSignal(str)
    status_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(dict)
    finished_signal = pyqtSignal()

    def __init__(self, config: dict, save_dir: str | Path):
        super().__init__()
        self.config = config
        self.save_dir = Path(save_dir)
        self.is_running = True
        self._pause_condition = threading.Condition()
        self.pause_requested = False
        self.is_paused = False
        self.run_profile = str(config.get("run_profile", RUN_PROFILE_MAIN)).upper()

        self.settle_sec = float(config.get("settle_sec", SETTLE_SEC_DEFAULT))
        self.score_w_ttot = float(config.get("score_w_ttot", 1.0))
        self.score_w_downstream = float(config.get("score_w_downstream", 1.0))
        self.downstream_ict = str(config.get("downstream_ict", "DR")).upper()
        if self.downstream_ict == "L0":
            self.downstream_ict = "LN0"
        self._downstream_ict_key = "L0" if self.downstream_ict == "LN0" else self.downstream_ict
        if self._downstream_ict_key not in ("L0", "DR", "GUN", "LNE", "BTM", "BTE"):
            self.downstream_ict = "DR"
            self._downstream_ict_key = "DR"
        self.mode = str(config.get("mode", "SEQUENTIAL")).upper()
        self.objective_type = str(config.get("objective_type", DEVELOPER_OBJECTIVE_ICT)).upper()
        if self.objective_type not in (DEVELOPER_OBJECTIVE_ICT, DEVELOPER_OBJECTIVE_BPM, DEVELOPER_OBJECTIVE_INTENSITY):
            self.objective_type = DEVELOPER_OBJECTIVE_ICT
        self.objective_bpm_plane = str(config.get("objective_bpm_plane", "XY")).upper()
        if self.objective_bpm_plane not in ("X", "Y", "XY"):
            self.objective_bpm_plane = "XY"
        self.objective_bpm_names = [str(name) for name in list(config.get("objective_bpm_names", [])) if
                                    str(name).strip()]
        self.intensity_aggregation = str(config.get("intensity_aggregation", "raw_mean"))
        self.intensity_baseline = dict(config.get("intensity_baseline", {}))
        self._last_intensities = {}
        self._last_bpm_snapshot = {}
        self._last_turn_plot = {}
        self.developer_actuators = [dict(item) for item in list(config.get("developer_actuators", [])) if
                                    isinstance(item, dict)]
        self.reuse_initial_eval = bool(config.get("reuse_initial_eval", True))
        self.ict_samples = max(1, int(config.get("ict_samples", config.get("ict_dr_samples", ICT_SAMPLES_DEFAULT))))
        self.ict_sample_interval_s = float(
            config.get("ict_sample_interval_s", config.get("ict_dr_interval_s", ICT_SAMPLE_INTERVAL_S_DEFAULT)))
        self.ict_max_retries_per_sample = max(1, int(config.get("ict_max_retries_per_sample",
                                                                config.get("ict_max_attempts",
                                                                           ICT_MAX_RETRIES_PER_SAMPLE_DEFAULT))))
        self.ict_retry_wait_s = float(config.get("ict_retry_wait_s", ICT_RETRY_WAIT_S_DEFAULT))
        self._initial_snapshot = None
        self._initial_eval_consumed = False
        self._eval_counter = 0
        self._current_pv_values: Dict[str, float] = {}
        self.resume_csv_path = Path(str(config.get("resume_csv_path", "")).strip()).expanduser() if str(
            config.get("resume_csv_path", "")).strip() else None
        self._resume_seq_rows: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
        self._resume_group_rows: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self._resume_discarded_row: Optional[Dict[str, Any]] = None

        # Fixed machine interface for Linac operations
        self.interface = InterfaceATF2_LinacBT(nsamples=1)
        self.dr_bpm_reader = DRBPMReader(nsamples=3) if self.objective_type == DEVELOPER_OBJECTIVE_INTENSITY else None
        if self.dr_bpm_reader is not None:
            configure_dr_turns(self.dr_bpm_reader, config, cancelled=lambda: not self.is_running)

        # Date folder + timestamped run files
        self.run_tag = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = self.save_dir / f"{self.run_tag}_LinacOpt"
        self.csv_path = self.run_dir / f"LiniacOptimization_Log_{self.run_tag}.csv"
        self._init_csv()
        self._load_resume_rows()

    # ----------------------------
    # CSV / logging
    # ----------------------------
    def _init_csv(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with open(self.csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "TimestampISO", "Mode", "Group", "DeviceLabel", "PV",
                "SetValue",
                "ICT_L0", "ICT_DR", "Ttot",
                "ICT_GUN", "ICT_LNE", "ICT_BTM", "ICT_BTE",
                "Score", "ScoreDownstreamICT", "ScoreWeight_Ttot", "ScoreWeight_Downstream",
                "ObjectiveType", "ObjectiveLabel", "ObjectiveValue", "BPMMetric", "Note"
            ])

    def _append_csv(self, r: EvalResult):
        ict = r.ict
        with open(self.csv_path, "a", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                r.t_iso, r.mode, r.group, r.device_label, r.pv_name,
                r.set_value,
                ict.get("L0", float("nan")),
                ict.get("DR", float("nan")),
                ict.get("Ttot", float("nan")),
                ict.get("GUN", float("nan")),
                ict.get("LNE", float("nan")),
                ict.get("BTM", float("nan")),
                ict.get("BTE", float("nan")),
                r.score,
                self.downstream_ict,
                self.score_w_ttot,
                self.score_w_downstream,
                r.objective_type,
                r.objective_label,
                r.objective_value,
                r.bpm_metric,
                r.note
            ])

    def _emit_progress(self, payload: Dict[str, object]):
        self.progress_signal.emit(dict(payload))

    def _emit_bo1d_trace(
        self,
        *,
        axis_name: str,
        x_grid: np.ndarray,
        mu: np.ndarray,
        std: np.ndarray,
        acq: np.ndarray,
        chosen_x: float,
        chosen_acq: float,
        x_obs: np.ndarray,
        y_obs: np.ndarray,
    ) -> None:
        x_arr = np.asarray(x_grid, dtype=float).reshape(-1)
        mu_arr = np.asarray(mu, dtype=float).reshape(-1)
        std_arr = np.asarray(std, dtype=float).reshape(-1)
        acq_arr = np.asarray(acq, dtype=float).reshape(-1)
        x_obs_arr = np.asarray(x_obs, dtype=float).reshape(-1)
        y_obs_arr = np.asarray(y_obs, dtype=float).reshape(-1)
        if x_arr.size == 0 or mu_arr.size != x_arr.size or std_arr.size != x_arr.size or acq_arr.size != x_arr.size:
            return

        note = "BO uses the score shown here."
        if self.objective_type == DEVELOPER_OBJECTIVE_ICT:
            note = (
                f"Score = {self.score_w_ttot:g}*Ttot + "
                f"{self.score_w_downstream:g}*{self.downstream_ict}"
            )

        self._emit_progress({
            "kind": "bo1d_trace",
            "trace": {
                "axis": str(axis_name),
                "x_label": str(axis_name),
                "y_label": "BO score",
                "direction": "maximize",
                "note": note,
                "acquisition_label": "EI",
                "x_grid": x_arr.tolist(),
                "y_mean": mu_arr.tolist(),
                "y_std": std_arr.tolist(),
                "acquisition": acq_arr.tolist(),
                "x_obs": x_obs_arr.tolist(),
                "y_obs": y_obs_arr.tolist(),
                "chosen_x": float(chosen_x),
                "chosen_acq": float(chosen_acq),
            },
        })

    def _emit_group_state(self, group_key: str, state: str):
        self._emit_progress({
            "kind": "group_state",
            "group_key": str(group_key),
            "state": str(state),
        })

    def _emit_developer_actuator_state(self, name: str, state: str):
        self._emit_progress({
            "kind": "developer_actuator_state",
            "name": str(name),
            "state": str(state),
        })

    def _emit_display_values(self):
        self._emit_progress({
            "kind": "current_values",
            "display_values": build_display_value_texts(self._current_pv_values),
        })

    def _record_evaluation(self, r: EvalResult, pv_updates: Optional[Dict[str, float]] = None):
        if pv_updates:
            for pv_name, value in pv_updates.items():
                self._current_pv_values[str(pv_name)] = float(value)
        self._eval_counter += 1
        self._emit_progress({
            "kind": "evaluation",
            "evaluation_phase": evaluation_phase(r.note),
            "eval_index": int(self._eval_counter),
            "group": str(r.group),
            "mode": str(r.mode),
            "score": float(r.score),
            "ttot": float(r.ict.get("Ttot", float("nan"))),
            "downstream_label": str(r.objective_label or self.downstream_ict),
            "downstream_value": float(
                r.objective_value if np.isfinite(r.objective_value) else self._downstream_value(r.ict)),
            "objective_type": str(r.objective_type),
            "bpm_metric": float(r.bpm_metric),
            "bpm_intensities": dict(self._last_intensities),
            "bpm_snapshot": dict(self._last_bpm_snapshot),
            "turn_plot": dict((self._initial_snapshot or {}).get("turn_plot", {})
                              if "REUSE_INIT" in r.note else self._last_turn_plot),
            "selected_bpms": list(self.objective_bpm_names),
            "dr_turn_label": dr_turn_label(self.config) if any(n in DR_BPM_NAMES for n in self.objective_bpm_names) else "",
            "measurement_ok": bool(r.measurement_ok),
            "measurement_reason": str(r.measurement_reason),
            "display_values": build_display_value_texts(self._current_pv_values),
        })
        self._pause_checkpoint()

    def _resume_seq_key(self, mode: str, group: str, pv_name: str) -> Tuple[str, str, str]:
        return (str(mode), str(group), str(pv_name))

    def _resume_group_key(self, mode: str, group: str) -> Tuple[str, str]:
        return (str(mode), str(group))

    def _parse_resume_vector_note(self, note: str) -> Optional[Dict[str, float]]:
        text = str(note or "")
        brace = text.find("{")
        if brace < 0:
            return None
        try:
            raw = json.loads(text[brace:])
        except Exception:
            return None
        out: Dict[str, float] = {}
        try:
            for key, value in dict(raw).items():
                out[str(key)] = float(value)
        except Exception:
            return None
        return out

    def _load_resume_rows(self):
        if self.resume_csv_path is None:
            return
        if not self.resume_csv_path.exists():
            self.log_signal.emit(f"[RESUME] CSV not found: {self.resume_csv_path}")
            return

        parsed_rows: List[Dict[str, Any]] = []
        with open(self.resume_csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for raw in reader:
                if not raw:
                    continue
                try:
                    ict = {
                        "L0": float(raw.get("ICT_L0", "nan")),
                        "DR": float(raw.get("ICT_DR", "nan")),
                        "Ttot": float(raw.get("Ttot", "nan")),
                        "GUN": float(raw.get("ICT_GUN", "nan")),
                        "LNE": float(raw.get("ICT_LNE", "nan")),
                        "BTM": float(raw.get("ICT_BTM", "nan")),
                        "BTE": float(raw.get("ICT_BTE", "nan")),
                    }
                    row = {
                        "t_iso": str(raw.get("TimestampISO", "")),
                        "mode": str(raw.get("Mode", "")),
                        "group": str(raw.get("Group", "")),
                        "device_label": str(raw.get("DeviceLabel", "")),
                        "pv_name": str(raw.get("PV", "")),
                        "set_value": float(raw.get("SetValue", "nan")),
                        "ict": ict,
                        "score": float(raw.get("Score", "nan")),
                        "objective_type": str(raw.get("ObjectiveType", DEVELOPER_OBJECTIVE_ICT)),
                        "objective_label": str(raw.get("ObjectiveLabel", "")),
                        "objective_value": float(raw.get("ObjectiveValue", "nan")),
                        "bpm_metric": float(raw.get("BPMMetric", "nan")),
                        "note": str(raw.get("Note", "")),
                    }
                except Exception:
                    continue

                if row["pv_name"] == "MULTI":
                    vec = self._parse_resume_vector_note(row["note"])
                    if not vec:
                        continue
                    row["vector"] = vec
                parsed_rows.append(row)

        if not parsed_rows:
            self.log_signal.emit(f"[RESUME] No valid resume rows found in {self.resume_csv_path}")
            return

        self._resume_discarded_row = dict(parsed_rows[-1])
        kept_rows = parsed_rows[:-1]
        self._eval_counter = len(kept_rows)

        for row in kept_rows:
            if row["pv_name"] == "MULTI":
                key = self._resume_group_key(row["mode"], row["group"])
                self._resume_group_rows.setdefault(key, []).append(row)
            else:
                key = self._resume_seq_key(row["mode"], row["group"], row["pv_name"])
                self._resume_seq_rows.setdefault(key, []).append(row)

        discarded = self._resume_discarded_row
        discard_desc = (
            f"{discarded.get('mode', '')}/{discarded.get('group', '')}/{discarded.get('device_label', discarded.get('pv_name', ''))}"
            if discarded is not None else "-"
        )
        self.log_signal.emit(
            f"[RESUME] Loaded {len(kept_rows)} previous evaluations from {self.resume_csv_path}. "
            f"Discarded last row for re-measurement: {discard_desc}"
        )

    def _resume_note_is_completed(self, row: Optional[Dict[str, Any]]) -> bool:
        if row is None:
            return False
        note = str(row.get("note", ""))
        return ("OPTIMIZED_SET" in note) or ("RESUME_COMPLETED_SET" in note)

    def _resume_pending_matches_seq(self, mode: str, group: str, pv_name: str) -> bool:
        row = self._resume_discarded_row
        if row is None:
            return False
        if str(row.get("pv_name", "")) == "MULTI":
            return False
        return self._resume_seq_key(row.get("mode", ""), row.get("group", ""), row.get("pv_name", "")) == self._resume_seq_key(mode, group, pv_name)

    def _resume_pending_matches_group(self, mode: str, group: str) -> bool:
        row = self._resume_discarded_row
        if row is None:
            return False
        if str(row.get("pv_name", "")) != "MULTI":
            return False
        return self._resume_group_key(row.get("mode", ""), row.get("group", "")) == self._resume_group_key(mode, group)

    def _consume_resume_pending_seq_row(self, mode: str, group: str, pv_name: str) -> Optional[Dict[str, Any]]:
        if (not self._resume_pending_matches_seq(mode, group, pv_name)) or self._resume_note_is_completed(self._resume_discarded_row):
            return None
        row = dict(self._resume_discarded_row or {})
        self._resume_discarded_row = None
        return row

    def _consume_resume_pending_group_row(self, mode: str, group: str) -> Optional[Dict[str, Any]]:
        if (not self._resume_pending_matches_group(mode, group)) or self._resume_note_is_completed(self._resume_discarded_row):
            return None
        row = dict(self._resume_discarded_row or {})
        self._resume_discarded_row = None
        return row

    def _resume_eval_result(self, row: Dict[str, Any]) -> EvalResult:
        return EvalResult(
            t_iso=str(row.get("t_iso", "")),
            device_label=str(row.get("device_label", "")),
            pv_name=str(row.get("pv_name", "")),
            set_value=float(row.get("set_value", float("nan"))),
            ict=dict(row.get("ict", {})),
            score=float(row.get("score", float("nan"))),
            objective_type=str(row.get("objective_type", DEVELOPER_OBJECTIVE_ICT)),
            objective_label=str(row.get("objective_label", "")),
            objective_value=float(row.get("objective_value", float("nan"))),
            bpm_metric=float(row.get("bpm_metric", float("nan"))),
            note=str(row.get("note", "")),
            group=str(row.get("group", "")),
            mode=str(row.get("mode", "")),
        )

    def _resume_seq_completed_row(self, mode: str, group: str, pv_name: str) -> Optional[Dict[str, Any]]:
        rows = self._resume_seq_rows.get(self._resume_seq_key(mode, group, pv_name), [])
        done_rows = [row for row in rows if self._resume_note_is_completed(row)]
        if done_rows:
            return done_rows[-1]
        if self._resume_pending_matches_seq(mode, group, pv_name) and self._resume_note_is_completed(self._resume_discarded_row):
            return dict(self._resume_discarded_row or {})
        return None

    def _resume_group_completed_row(self, mode: str, group: str) -> Optional[Dict[str, Any]]:
        rows = self._resume_group_rows.get(self._resume_group_key(mode, group), [])
        done_rows = [row for row in rows if self._resume_note_is_completed(row)]
        if done_rows:
            return done_rows[-1]
        if self._resume_pending_matches_group(mode, group) and self._resume_note_is_completed(self._resume_discarded_row):
            return dict(self._resume_discarded_row or {})
        return None

    def _resume_scan_candidates(self, mode: str, group: str, pv_name: str, fallback: np.ndarray) -> np.ndarray:
        rows = self._resume_seq_rows.get(self._resume_seq_key(mode, group, pv_name), [])
        values = sorted({round(float(row.get("set_value", float("nan"))), 10) for row in rows if
                         np.isfinite(float(row.get("set_value", float("nan"))))})
        if len(values) < 2:
            return fallback
        values_arr = np.array(values, dtype=float)
        fallback_diffs = np.diff(np.unique(np.asarray(fallback, dtype=float)))
        step = float(np.median(fallback_diffs[fallback_diffs > 0])) if np.any(fallback_diffs > 0) else float("nan")
        if (not np.isfinite(step)) or step <= 0.0:
            diffs = np.diff(values_arr)
            step = float(np.median(diffs[diffs > 0])) if np.any(diffs > 0) else float("nan")
        if (not np.isfinite(step)) or step <= 0.0:
            return fallback
        rebuilt = np.arange(values_arr[0], values_arr[-1] + step * 0.5, step, dtype=float)
        rebuilt = np.unique(np.round(rebuilt, 10))
        return rebuilt if rebuilt.size else fallback

    def _dump_initial_snapshot(self):
        """Dump initial machine/config snapshot for reproducibility."""
        self._pause_checkpoint()
        snapshot_path = self.run_dir / f"InitialSnapshot_{self.run_tag}.json"
        setpoints: Dict[str, float] = {}
        restore_pvs = [str(pv) for pv in list(self.config.get("restore_pvs", RESTORE_PVS))]
        for pv in restore_pvs:
            setpoints[pv] = float(self._read_current(pv, _default_readback_pv(pv)))

        (
            initial_ict,
            initial_bpm_metric,
            initial_objective_value,
            initial_objective_label,
            initial_score,
            initial_measurement_ok,
            initial_measurement_reason,
        ) = self._measure_objective()

        data = {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "mode": self.mode,
            "config": self.config,
            "initial_setpoints": setpoints,
            "initial_ict": initial_ict,
            "initial_bpm_metric": float(initial_bpm_metric),
            "initial_objective_label": str(initial_objective_label),
            "initial_objective_value": float(initial_objective_value),
            "initial_score": initial_score,
            "initial_measurement_ok": bool(initial_measurement_ok),
            "initial_measurement_reason": str(initial_measurement_reason),
            "turn_plot": dict(self._last_turn_plot),
        }
        with open(snapshot_path, "w") as f:
            json.dump(data, f, indent=2)
        self._initial_snapshot = data
        self._current_pv_values = {str(k): float(v) for k, v in setpoints.items()}
        self._emit_display_values()
        self.log_signal.emit(f"[INIT] Snapshot dumped: {snapshot_path}")
        self._pause_checkpoint()

    def _try_reuse_initial_point(self, device_label: str, pv_name: str, set_value: float,
                                 mode: str, group: str, note: str) -> Optional[EvalResult]:
        if not self.reuse_initial_eval or self._initial_eval_consumed or (self._initial_snapshot is None):
            return None
        init_set = self._initial_snapshot.get("initial_setpoints", {})
        init_ict = self._initial_snapshot.get("initial_ict", {})
        init_bpm_metric = float(self._initial_snapshot.get("initial_bpm_metric", float("nan")))
        init_objective_label = str(self._initial_snapshot.get("initial_objective_label", self._objective_label()))
        init_objective_value = float(self._initial_snapshot.get("initial_objective_value", float("nan")))
        init_score = float(self._initial_snapshot.get("initial_score", -1e30))
        init_measurement_ok = bool(self._initial_snapshot.get("initial_measurement_ok", True))
        init_measurement_reason = str(self._initial_snapshot.get("initial_measurement_reason", ""))
        if pv_name not in init_set:
            return None
        if abs(float(set_value) - float(init_set[pv_name])) > 1e-9:
            return None

        self._initial_eval_consumed = True
        r = EvalResult(
            t_iso=datetime.datetime.now().isoformat(timespec="seconds"),
            device_label=device_label,
            pv_name=pv_name,
            set_value=float(set_value),
            ict=dict(init_ict),
            score=float(init_score),
            objective_type=self.objective_type,
            objective_label=init_objective_label,
            objective_value=init_objective_value,
            bpm_metric=init_bpm_metric,
            measurement_ok=init_measurement_ok,
            measurement_reason=init_measurement_reason,
            note=(
                f"{note}|REUSE_INIT|ICT_INVALID_ZERO: {init_measurement_reason}"
                if ((not init_measurement_ok) and init_measurement_reason)
                else f"{note}|REUSE_INIT"
            ),
            group=group,
            mode=mode,
        )
        self._append_csv(r)
        self._record_evaluation(r, {pv_name: float(set_value)})
        self.log_signal.emit(f"[INIT-REUSE] Reused initial eval for {device_label} ({pv_name}) at x={set_value:.6g}")
        return r

    def _try_reuse_initial_vector(self, labels: List[str], pvs: List[str], x_vec: np.ndarray,
                                  mode: str, group: str, note: str) -> Optional[EvalResult]:
        if not self.reuse_initial_eval or self._initial_eval_consumed or (self._initial_snapshot is None):
            return None
        init_set = self._initial_snapshot.get("initial_setpoints", {})
        init_ict = self._initial_snapshot.get("initial_ict", {})
        init_bpm_metric = float(self._initial_snapshot.get("initial_bpm_metric", float("nan")))
        init_objective_label = str(self._initial_snapshot.get("initial_objective_label", self._objective_label()))
        init_objective_value = float(self._initial_snapshot.get("initial_objective_value", float("nan")))
        init_score = float(self._initial_snapshot.get("initial_score", -1e30))
        init_measurement_ok = bool(self._initial_snapshot.get("initial_measurement_ok", True))
        init_measurement_reason = str(self._initial_snapshot.get("initial_measurement_reason", ""))

        for pv, x in zip(pvs, x_vec):
            if pv not in init_set:
                return None
            if abs(float(x) - float(init_set[pv])) > 1e-9:
                return None

        self._initial_eval_consumed = True
        r = EvalResult(
            t_iso=datetime.datetime.now().isoformat(timespec="seconds"),
            device_label=",".join(labels),
            pv_name=",".join(pvs),
            set_value=float("nan"),
            ict=dict(init_ict),
            score=float(init_score),
            objective_type=self.objective_type,
            objective_label=init_objective_label,
            objective_value=init_objective_value,
            bpm_metric=init_bpm_metric,
            measurement_ok=init_measurement_ok,
            measurement_reason=init_measurement_reason,
            note=(
                f"{note}|REUSE_INIT|ICT_INVALID_ZERO: {init_measurement_reason}"
                if ((not init_measurement_ok) and init_measurement_reason)
                else f"{note}|REUSE_INIT"
            ),
            group=group,
            mode=mode,
        )
        self._append_csv(r)
        self._record_evaluation(r, {pv: float(x) for pv, x in zip(pvs, x_vec)})
        self.log_signal.emit(f"[INIT-REUSE] Reused initial vector eval for group {group}")
        return r

    # ----------------------------
    # Stop
    # ----------------------------
    def stop(self):
        with self._pause_condition:
            self.is_running = False
            self.pause_requested = False
            self._pause_condition.notify_all()
        self.log_signal.emit("!!! Stop Requested !!!")

    def request_pause(self):
        with self._pause_condition:
            if self.is_running:
                self.pause_requested = True
        self.log_signal.emit("[PAUSE] Requested; finish the current evaluation before waiting.")

    def resume(self):
        with self._pause_condition:
            self.pause_requested = False
            self._pause_condition.notify_all()

    def _pause_checkpoint(self):
        with self._pause_condition:
            if self.pause_requested and self.is_running:
                self.is_paused = True
                self.log_signal.emit("[PAUSE] Paused; history, baseline and machine settings retained.")
                self._emit_progress({"kind": "pause_state", "paused": True})
                try:
                    while self.pause_requested and self.is_running:
                        self._pause_condition.wait()
                finally:
                    self.is_paused = False
                if self.is_running:
                    self.log_signal.emit("[RESUME] Continuing the same run.")
                    self._emit_progress({"kind": "pause_state", "paused": False})
            if not self.is_running:
                raise InterruptedError("Optimization stopped")

    # ----------------------------
    # Measurement
    # ----------------------------

    def _read_icts(self) -> Tuple[Dict[str, float], bool, str]:
        return _read_icts_with_retry(self.interface,
            downstream_key=self._downstream_ict_key,
            sample_count=self.ict_samples,
            sample_interval_s=self.ict_sample_interval_s,
            max_retries_per_sample=self.ict_max_retries_per_sample,
            retry_wait_s=self.ict_retry_wait_s,
            log_fn=self.log_signal.emit,
        )

    def _downstream_value(self, ict: Dict[str, float]) -> float:
        return float(ict.get(self._downstream_ict_key, float("nan")))

    def _objective_label(self) -> str:
        if self.objective_type == DEVELOPER_OBJECTIVE_INTENSITY:
            return "BPM intensity " + self.intensity_aggregation
        if self.objective_type == DEVELOPER_OBJECTIVE_BPM:
            return f"BPM {self.objective_bpm_plane} sumsq"
        return str(self.downstream_ict)

    def _read_bpm_metric(self) -> float:
        if self.objective_type != DEVELOPER_OBJECTIVE_BPM:
            return float("nan")
        if not self.objective_bpm_names:
            return float("nan")

        bpms = self.interface.get_bpms()
        bpm_names = [str(name) for name in list(bpms.get("names", []))]
        x_all = np.asarray(bpms.get("x", []), dtype=float)
        y_all = np.asarray(bpms.get("y", []), dtype=float)
        if x_all.ndim == 1:
            x_avg = x_all
        elif x_all.size:
            x_avg = np.nanmean(x_all, axis=0)
        else:
            x_avg = np.array([], dtype=float)
        if y_all.ndim == 1:
            y_avg = y_all
        elif y_all.size:
            y_avg = np.nanmean(y_all, axis=0)
        else:
            y_avg = np.array([], dtype=float)

        index_map = {name: idx for idx, name in enumerate(bpm_names)}
        metric = 0.0
        used = 0
        for bpm_name in self.objective_bpm_names:
            idx = index_map.get(str(bpm_name))
            if idx is None:
                continue
            if self.objective_bpm_plane in ("X", "XY") and idx < x_avg.size and np.isfinite(x_avg[idx]):
                metric += float(x_avg[idx]) ** 2
                used += 1
            if self.objective_bpm_plane in ("Y", "XY") and idx < y_avg.size and np.isfinite(y_avg[idx]):
                metric += float(y_avg[idx]) ** 2
                used += 1
        return float(metric) if used > 0 else float("nan")

    def _measure_objective(self) -> Tuple[Dict[str, float], float, float, str, float, bool, str]:
        if self.objective_type == DEVELOPER_OBJECTIVE_INTENSITY:
            if self.intensity_aggregation in ("turn_ratio", "turn_composite"):
                metric, _, self._last_intensities, self._last_bpm_snapshot, transmission, normalized = read_turn_objective(
                    self.dr_bpm_reader, self.objective_bpm_names, self.config, self.intensity_baseline,
                    check_status=False, plot_out=self._last_turn_plot)
                self.log_signal.emit(f"[DR] mean transmission={transmission:.6g}; normalized turn 1 meanCurrent={normalized:.6g}; score={metric:.6g}")
                return {}, metric, metric, self._objective_label(), metric, True, ""
            self._last_intensities = read_selected_bpm_intensities(
                self.interface, self.dr_bpm_reader, self.objective_bpm_names, self.log_signal.emit,
                snapshot_out=self._last_bpm_snapshot, check_status=False)
            metric = intensity_score(self._last_intensities, self.intensity_baseline, self.intensity_aggregation)
            return {}, metric, metric, self._objective_label(), metric, True, ""
        ict, measurement_ok, measurement_reason = self._read_icts()
        bpm_metric = self._read_bpm_metric()
        label = self._objective_label()
        if self.objective_type == DEVELOPER_OBJECTIVE_BPM:
            objective_value = float(bpm_metric)
        else:
            objective_value = float(self._downstream_value(ict))
        score = self._score(ict, bpm_metric=bpm_metric)
        return ict, float(bpm_metric), float(objective_value), label, float(score), bool(measurement_ok), str(
            measurement_reason)

    def _objective_metric_text(self, ict: Dict[str, float], bpm_metric: float) -> str:
        if self.objective_type in (DEVELOPER_OBJECTIVE_BPM, DEVELOPER_OBJECTIVE_INTENSITY):
            return f"{self._objective_label()}={float(bpm_metric):.6g}"
        return f"{self.downstream_ict}={self._downstream_value(ict):.6g}"

    def _score(self, ict: Dict[str, float], bpm_metric: float = float("nan")) -> float:
        """
        Weighted score:
        score = w_t * Ttot + w_c * ICT_downstream
        where ICT_downstream is LN0/DR/GUN/LNE/BTM/BTE selected by config.
        """
        if self.objective_type == DEVELOPER_OBJECTIVE_INTENSITY:
            if not np.isfinite(bpm_metric):
                raise ValueError("Invalid BPM intensity score")
            return float(bpm_metric)
        if self.objective_type == DEVELOPER_OBJECTIVE_BPM:
            if not np.isfinite(bpm_metric):
                return -1e30
            return -float(bpm_metric)

        T = ict.get("Ttot", float("nan"))
        C = self._downstream_value(ict)

        if not np.isfinite(T) or not np.isfinite(C):
            return -1e30

        return self.score_w_ttot * float(T) + self.score_w_downstream * float(C)

    def _evaluate_point(self, device_label: str, pv_name: str, set_value: float,
                        mode: str, group: str, note: str = "") -> EvalResult:
        self._write_setpoints({pv_name: set_value})

        # settle (requested: after change, wait a few seconds)
        t0 = time.time()
        while self.is_running and (time.time() - t0) < self.settle_sec:
            time.sleep(0.1)

        ict, bpm_metric, objective_value, objective_label, score, measurement_ok, measurement_reason = self._measure_objective()
        full_note = str(note)
        if not measurement_ok:
            invalid_note = f"ICT_INVALID_ZERO: {measurement_reason}"
            full_note = f"{full_note} | {invalid_note}" if full_note else invalid_note

        r = EvalResult(
            t_iso=datetime.datetime.now().isoformat(timespec="seconds"),
            device_label=device_label,
            pv_name=pv_name,
            set_value=float(set_value),
            ict=ict,
            score=float(score),
            objective_type=self.objective_type,
            objective_label=objective_label,
            objective_value=float(objective_value),
            bpm_metric=float(bpm_metric),
            measurement_ok=bool(measurement_ok),
            measurement_reason=str(measurement_reason),
            note=full_note,
            group=group,
            mode=mode,
        )
        self._append_csv(r)
        self._record_evaluation(r, {pv_name: float(set_value)})
        return r

    def _evaluate_point_multi(self, device_label: str, pv_to_value: Dict[str, float],
                              mode: str, group: str, note: str = "") -> EvalResult:
        # Put all PVs first
        self._write_setpoints({k: float(v) for k, v in pv_to_value.items()})

        # Optional fixed wait after finishing all puts (recommended for multi-put updates)
        post_put_wait = float(self.config.get("post_put_wait_sec", 0.0))
        if self.is_running and post_put_wait > 0:
            time.sleep(post_put_wait)

        # Settle once
        t0 = time.time()
        while self.is_running and (time.time() - t0) < self.settle_sec:
            time.sleep(0.1)

        ict, bpm_metric, objective_value, objective_label, score, measurement_ok, measurement_reason = self._measure_objective()

        # Store the vector in note (CSV-friendly)
        try:
            vec_note = json.dumps(pv_to_value, sort_keys=True)
        except Exception:
            vec_note = str(pv_to_value)
        if not measurement_ok:
            vec_note = f"ICT_INVALID_ZERO: {measurement_reason}" + (" | " + vec_note if vec_note else "")

        r = EvalResult(
            t_iso=datetime.datetime.now().isoformat(timespec="seconds"),
            device_label=device_label,
            pv_name="MULTI",
            set_value=float("nan"),
            ict=ict,
            score=float(score),
            objective_type=self.objective_type,
            objective_label=objective_label,
            objective_value=float(objective_value),
            bpm_metric=float(bpm_metric),
            measurement_ok=bool(measurement_ok),
            measurement_reason=str(measurement_reason),
            note=(note + (" | " if note else "") + vec_note),
            group=group,
            mode=mode,
        )
        self._append_csv(r)
        self._record_evaluation(r, {k: float(v) for k, v in pv_to_value.items()})
        return r

    # ----------------------------
    # GP posterior (N-D, RBF kernel with ARD)
    # ----------------------------
    def _kernel_rbf_ard(self, Xa: np.ndarray, Xb: np.ndarray, length_scales: np.ndarray, sigma_f: float) -> np.ndarray:
        # Xa: (N,D), Xb: (M,D)
        Xa = np.asarray(Xa, dtype=float)
        Xb = np.asarray(Xb, dtype=float)
        ls = np.asarray(length_scales, dtype=float)
        ls = np.maximum(ls, 1e-12)
        diff = (Xa[:, None, :] - Xb[None, :, :]) / ls[None, None, :]
        d2 = np.sum(diff * diff, axis=2)
        return (sigma_f ** 2) * np.exp(-0.5 * d2)

    def _gp_posterior_nd(self, X_train: np.ndarray, y_train: np.ndarray, X_test: np.ndarray,
                         length_scales: np.ndarray, sigma_f: float, sigma_n: float) -> Tuple[np.ndarray, np.ndarray]:
        # Standard GP regression posterior for scalar outputs
        X_train = np.asarray(X_train, dtype=float)
        y_train = np.asarray(y_train, dtype=float).reshape(-1)
        X_test = np.asarray(X_test, dtype=float)

        if X_train.ndim != 2 or X_test.ndim != 2:
            raise ValueError("X_train and X_test must be 2D arrays (N,D) and (M,D).")
        if X_train.shape[0] == 0:
            mu = np.zeros((X_test.shape[0],), dtype=float)
            std = np.ones((X_test.shape[0],), dtype=float)
            return mu, std

        K = self._kernel_rbf_ard(X_train, X_train, length_scales, sigma_f) + (sigma_n ** 2) * np.eye(X_train.shape[0])
        Ks = self._kernel_rbf_ard(X_train, X_test, length_scales, sigma_f)

        # Solve K^{-1} y via Cholesky for stability
        try:
            L = np.linalg.cholesky(K + 1e-12 * np.eye(K.shape[0]))
            alpha = np.linalg.solve(L.T, np.linalg.solve(L, y_train))
            mu = Ks.T @ alpha
            v = np.linalg.solve(L, Ks)
            var = sigma_f ** 2 - np.sum(v * v, axis=0)
        except np.linalg.LinAlgError:
            # Fallback: pseudo-inverse
            Kinv = np.linalg.pinv(K)
            mu = Ks.T @ (Kinv @ y_train)
            var = sigma_f ** 2 - np.sum(Ks * (Kinv @ Ks), axis=0)

        var = np.clip(var, 1e-12, None)
        std = np.sqrt(var)
        return mu.reshape(-1), std.reshape(-1)

    # ----------------------------
    # Group BO (simultaneous multi-parameter BO via random candidate EI maximization)
    # ----------------------------
    def _group_bo_simultaneous(self, group_name: str, params: List[tuple]):
        """
        Simultaneous (multi-dimensional) BO for a group:
        - Each iteration proposes a vector of settings for all PVs in the group.
        - Candidate vectors are generated by random sampling within [cur-half, cur+half] per PV,
          then quantized to each PV step.
        - EI is maximized over the random candidate set.
        """
        if not self.is_running or len(params) == 0:
            return
        self._emit_progress({"kind": "bo1d_trace", "trace": {}})

        resume_key = self._resume_group_key("GROUP_BO_SIMUL", group_name)
        resume_rows = self._resume_group_rows.get(resume_key, [])
        resume_done_row = self._resume_group_completed_row("GROUP_BO_SIMUL", group_name)

        # Current baseline
        pvs = []
        steps_list = []
        cur_list = []
        lo_list = []
        hi_list = []
        for p in params:
            label, pv, step, half = p[:4]
            cur_i = float(self._read_current(pv, None))
            lo_i = cur_i - max(float(half), 0.0)
            hi_i = cur_i + max(float(half), 0.0)
            hard_lo_i = None
            hard_hi_i = None
            if len(p) >= 6:
                hard_lo_i = float(p[4])
                hard_hi_i = float(p[5])
                if hard_hi_i < hard_lo_i:
                    hard_lo_i, hard_hi_i = hard_hi_i, hard_lo_i
                lo_i = hard_lo_i
                hi_i = hard_hi_i
            if resume_rows:
                past_vals = []
                for row in resume_rows:
                    vec = row.get("vector", {})
                    if pv in vec:
                        try:
                            past_vals.append(float(vec[pv]))
                        except Exception:
                            pass
                if past_vals:
                    lo_i = min(lo_i, float(np.nanmin(past_vals)))
                    hi_i = max(hi_i, float(np.nanmax(past_vals)))
            if hard_lo_i is not None and hard_hi_i is not None:
                lo_i = max(lo_i, hard_lo_i)
                hi_i = min(hi_i, hard_hi_i)
                if hi_i < lo_i:
                    clipped_cur = float(np.clip(cur_i, hard_lo_i, hard_hi_i))
                    lo_i = clipped_cur
                    hi_i = clipped_cur
            cur_i = float(np.clip(cur_i, lo_i, hi_i))
            pvs.append(pv)
            steps_list.append(max(float(step), 1e-12))
            cur_list.append(cur_i)
            lo_list.append(lo_i)
            hi_list.append(hi_i)

        steps = np.array(steps_list, dtype=float)
        cur = np.array(cur_list, dtype=float)
        lo = np.array(lo_list, dtype=float)
        hi = np.array(hi_list, dtype=float)

        # Config
        budget_preset = self.config.get("gbo_budget_preset", "standard")
        auto_budget = _auto_group_bo_budget(len(params), budget_preset)
        min_init = int(self.config.get("gbo_min_init", auto_budget["min_init"]))
        max_evals = _resolve_group_bo_max_evals(self.config, group_name, len(params))
        min_init = max(1, min(min_init, max_evals))
        cand_pool = int(self.config.get("gbo_candidate_pool", auto_budget["candidate_pool"]))
        ei_tol = float(self.config.get("bo_ei_tol", 0.0))
        xi = float(self.config.get("bo_xi", 0.1))
        sigma_f = float(self.config.get("bo_sigma_f", 1.0))
        sigma_n = float(self.config.get("bo_sigma_n", 1e-2))
        uncertainty_rel_tol = float(self.config.get("gbo_uncertainty_rel_tol", 0.05))
        uncertainty_abs_tol_cfg = self.config.get("gbo_uncertainty_abs_tol", None)

        # Fixed ARD priors.  legacy_fixed preserves the previous range/3
        # behavior exactly; learned_fixed uses the BO2026 conservative preset
        # and never drops below the configured number of hardware steps.
        ranges = np.maximum(hi - lo, steps)
        hp_mode = str(self.config.get("bo_hyperparameter_mode", "legacy_fixed")).lower()
        n_step_floor = float(self.config.get("bo_length_scale_n_step_floor", 2.0))
        resolved_ls = [
            resolve_linac_length_scale(
                pv,
                axis_range=float(ranges[i]),
                axis_step=float(steps[i]),
                mode=hp_mode,
                n_step_floor=n_step_floor,
            )
            for i, pv in enumerate(pvs)
        ]
        ls = np.asarray([item[0] for item in resolved_ls], dtype=float)
        # Allow override
        if "gbo_length_scale_factor" in self.config:
            ls = np.maximum(ls * float(self.config["gbo_length_scale_factor"]), n_step_floor * steps)

        # Only Corrector GroupBO opts into online fitting. Missing config keys
        # preserve historical behavior for existing saved runs.
        online_model = None
        if group_name == DEVELOPER_GROUP_NAME and self.config.get("corrector_gp_mode", "fixed") == "online_fit":
            online_model = CorrectorGPModel(
                lo, hi, steps, refit_interval=self.config.get("corrector_gp_refit_interval", 5),
                log=self.log_signal.emit, cancelled=lambda: not self.is_running, names=pvs)

        rng = np.random.default_rng()
        grid_max_idx = np.maximum(0, np.round((hi - lo) / steps).astype(int))
        grid_axes = [
            np.unique(np.clip(lo[i] + np.arange(grid_max_idx[i] + 1, dtype=float) * steps[i], lo[i], hi[i]))
            for i in range(len(params))
        ]
        full_lattice_limit = int(self.config.get("gbo_full_lattice_limit", 8192))
        total_lattice_points = 1
        for axis in grid_axes:
            total_lattice_points *= max(1, int(axis.size))
        min_init = min(min_init, total_lattice_points)

        X, Y, R = [], [], []

        def quantize_vec(x: np.ndarray) -> np.ndarray:
            q = np.round((x - lo) / steps) * steps + lo
            return np.clip(q, lo, hi)

        def key(v: np.ndarray) -> Tuple[int, ...]:
            q = quantize_vec(np.asarray(v, dtype=float))
            return tuple(np.round((q - lo) / steps).astype(int).tolist())

        def vec_from_key(k: Tuple[int, ...]) -> np.ndarray:
            return np.array(
                [grid_axes[i][min(max(int(k[i]), 0), grid_axes[i].size - 1)] for i in range(len(k))],
                dtype=float,
            )

        def vec_to_dict(x: np.ndarray) -> Dict[str, float]:
            return {pv: float(val) for pv, val in zip(pvs, x)}

        eval_cache: Dict[Tuple[int, ...], EvalResult] = {}

        def eval_vec(x: np.ndarray, note: str = ""):
            xq = quantize_vec(np.asarray(x, dtype=float))
            x_key = key(xq)
            cached = eval_cache.get(x_key)
            if cached is not None:
                self.log_signal.emit(f"[GROUP_BO] Reusing cached lattice point for {group_name}: key={x_key}")
                return cached
            reused = self._try_reuse_initial_vector(labels=[lab for (lab, *_rest) in params], pvs=pvs, x_vec=xq,
                                                    mode="GROUP_BO_SIMUL", group=group_name, note=note)
            if reused is not None:
                X.append(xq.copy())
                Y.append(float(reused.score))
                R.append(reused)
                eval_cache[x_key] = reused
                return reused
            r = self._evaluate_point_multi(
                device_label=f"Group:{group_name}",
                pv_to_value=vec_to_dict(xq),
                mode="GROUP_BO_SIMUL",
                group=group_name,
                note=note
            )
            X.append(xq.copy())
            Y.append(float(r.score))
            R.append(r)
            eval_cache[x_key] = r
            return r

        def current_best():
            idx = int(np.argmax(Y))
            return np.asarray(X[idx], dtype=float), float(Y[idx]), R[idx]

        self.log_signal.emit(f"--- GROUP_BO_SIMUL: {group_name} (D={len(params)}) ---")
        self.log_signal.emit(
            f"[GROUP_BO] {group_name}: lattice_points={total_lattice_points}, "
            f"candidate_mode={'full_lattice' if total_lattice_points <= full_lattice_limit else 'sampled_lattice'}"
        )
        if online_model is None:
            self.log_signal.emit(
                f"[GROUP_BO][GP] mode={hp_mode}, preset={LINAC_LEARNED_PRESET_VERSION if hp_mode == 'learned_fixed' else 'legacy'}, "
                f"length_scales={{" + ", ".join(
                    f"{pv}:{ls_i:.6g} ({source})" for pv, ls_i, (_, source) in zip(pvs, ls, resolved_ls)
                ) + "} | signal_sd=%.6g noise_sd=%.6g" % (sigma_f, sigma_n)
            )
        if online_model is not None:
            self.log_signal.emit(
                f"[Corrector GP] online_fit; refit every {online_model.refit_interval} evaluations; "
                "normalized length bounds=[min(2, max(0.02, 2*step/range)), 2], signal_sd=[0.1,10], noise_sd=[0.001,1]. "
                "EI and uncertainty thresholds use standardized scores; displayed/saved scores stay raw.")
        if resume_done_row is not None:
            vec = resume_done_row.get("vector", {})
            if isinstance(vec, dict):
                x_best = np.array([float(vec.get(pv, np.nan)) for pv in pvs], dtype=float)
                if np.all(np.isfinite(x_best)):
                    if self._resume_pending_matches_group("GROUP_BO_SIMUL", group_name):
                        self._resume_discarded_row = None
                    self.log_signal.emit(
                        f"[RESUME] Group {group_name} already completed previously. Re-applying saved best vector.")
                    _ = self._evaluate_point_multi(
                        device_label=f"Group:{group_name}",
                        pv_to_value=vec_to_dict(quantize_vec(x_best)),
                        mode="GROUP_BO_SIMUL",
                        group=group_name,
                        note="RESUME_COMPLETED_SET"
                    )
                    return

        if resume_rows:
            dedup: Dict[Tuple[int, ...], Dict[str, Any]] = {}
            for row in resume_rows:
                note = str(row.get("note", ""))
                vec = row.get("vector", {})
                if (not isinstance(vec, dict)) or ("OPTIMIZED_SET" in note) or ("RESUME_COMPLETED_SET" in note):
                    continue
                x_vec = np.array([float(vec.get(pv, np.nan)) for pv in pvs], dtype=float)
                if not np.all(np.isfinite(x_vec)):
                    continue
                q_vec = quantize_vec(x_vec)
                key_vec = tuple(np.round((q_vec - lo) / steps).astype(int).tolist())
                prev = dedup.get(key_vec)
                if prev is None or float(row.get("score", float("-inf"))) >= float(prev.get("score", float("-inf"))):
                    dedup[key_vec] = row
            if dedup:
                for row in dedup.values():
                    vec = row["vector"]
                    x_vec = quantize_vec(np.array([float(vec[pv]) for pv in pvs], dtype=float))
                    X.append(x_vec.copy())
                    Y.append(float(row["score"]))
                    resume_r = self._resume_eval_result(row)
                    R.append(resume_r)
                    eval_cache[key(x_vec)] = resume_r
                self.log_signal.emit(
                    f"[RESUME] Warm start for group {group_name}: loaded {len(X)} previous evaluations.")

        resume_pending_row = self._consume_resume_pending_group_row("GROUP_BO_SIMUL", group_name)
        if resume_pending_row is not None:
            vec = resume_pending_row.get("vector", {})
            if isinstance(vec, dict):
                x_resume = quantize_vec(np.array([float(vec.get(pv, np.nan)) for pv in pvs], dtype=float))
                if np.all(np.isfinite(x_resume)):
                    self.log_signal.emit(
                        f"[RESUME] Re-measuring discarded last group point for {group_name} before continuing."
                    )
                    r_resume = self._evaluate_point_multi(
                        device_label=f"Group:{group_name}",
                        pv_to_value=vec_to_dict(x_resume),
                        mode="GROUP_BO_SIMUL",
                        group=group_name,
                        note="RESUME_REMEASURE_LAST",
                    )
                    X.append(x_resume.copy())
                    Y.append(float(r_resume.score))
                    R.append(r_resume)
                    eval_cache[key(x_resume)] = r_resume

        stop_reason = "max_evals_reached"

        def build_candidate_set(X_train: np.ndarray) -> Tuple[np.ndarray, str]:
            seen = {key(x) for x in X_train}
            remaining = max(0, total_lattice_points - len(seen))
            if remaining == 0:
                return np.empty((0, len(params)), dtype=float), "candidate_exhausted"

            if total_lattice_points <= full_lattice_limit:
                cand_keys = [
                    tuple(int(v) for v in idx)
                    for idx in itertools.product(*[range(int(axis.size)) for axis in grid_axes])
                    if tuple(int(v) for v in idx) not in seen
                ]
                if not cand_keys:
                    return np.empty((0, len(params)), dtype=float), "candidate_exhausted"
                return np.vstack([vec_from_key(k) for k in cand_keys]), f"full_lattice({len(cand_keys)})"

            target = min(cand_pool, remaining)
            cand_keys: List[Tuple[int, ...]] = []
            sampled = set()
            attempts = 0
            max_attempts = max(1000, target * 40)
            while len(cand_keys) < target and attempts < max_attempts:
                attempts += 1
                idx = tuple(int(rng.integers(0, int(axis.size))) for axis in grid_axes)
                if idx in seen or idx in sampled:
                    continue
                sampled.add(idx)
                cand_keys.append(idx)
            if not cand_keys:
                return np.empty((0, len(params)), dtype=float), "candidate_sampling_exhausted"
            return np.vstack([vec_from_key(k) for k in cand_keys]), f"sampled_lattice({len(cand_keys)}/{remaining})"

        self.log_signal.emit(
            f"[GROUP_BO] {group_name}: D={len(params)}, min_init={min_init}, "
            f"max_evals={max_evals}, cand_pool={cand_pool}; "
            f"budget_preset={budget_preset}, default max={auto_budget['max_evals']}, initial={auto_budget['min_init']}"
        )

        # Initial design: current + unique random lattice points. Larger
        # budgets must not loop forever when the configured grid is small.
        if not self.is_running:
            return
        if len(X) == 0:
            eval_vec(cur, note="GBO_INIT_CUR")
        while self.is_running and len(X) < min_init and len(X) < max_evals:
            candidates, candidate_mode = build_candidate_set(np.vstack(X))
            if candidates.size == 0:
                stop_reason = candidate_mode
                break
            eval_vec(candidates[int(rng.integers(len(candidates)))], note="GBO_INIT_RAND")

        while self.is_running and len(X) < max_evals and stop_reason == "max_evals_reached":
            X_train = np.vstack(X)
            y_train = np.array(Y, dtype=float)

            C2, cand_mode = build_candidate_set(X_train)
            if C2.size == 0:
                stop_reason = cand_mode
                break

            if online_model is not None:
                model_x, model_y = online_model.prepare(X_train, y_train)
                if not self.is_running:
                    return
                model_ls, model_signal, model_noise = online_model.parameters
                mu, std = self._gp_posterior_nd(
                    model_x, model_y, online_model.transform_inputs(C2),
                    length_scales=model_ls, sigma_f=model_signal, sigma_n=model_noise)
                y_best = float(np.max(model_y))
                # Fixed standardized reference, independent of raw sum/offset.
                score_scale = 1.0
            else:
                mu, std = self._gp_posterior_nd(X_train, y_train, C2, length_scales=ls, sigma_f=sigma_f, sigma_n=sigma_n)
                y_best = float(np.max(y_train))
                score_scale = max(float(np.std(y_train)), float(np.ptp(y_train)) * 0.25, max(abs(y_best), 1e-6))
            uncertainty_abs_tol = (
                float(uncertainty_abs_tol_cfg)
                if uncertainty_abs_tol_cfg is not None
                else max(1e-6, 0.03 * score_scale)
            )
            std_tol = max(uncertainty_abs_tol, uncertainty_rel_tol * max(score_scale, 1.0))
            max_std = float(np.max(std)) if std.size else 0.0
            if (len(X) >= min_init and max_std <= std_tol
                    and (online_model is None or online_model.scale_frozen)):
                stop_reason = (
                    f"uncertainty_small(max_std={max_std:.6g} <= tol={std_tol:.6g}, "
                    f"mode={cand_mode})"
                )
                break

            ei = self._expected_improvement(mu, std, y_best, xi=xi)
            j = int(np.argmax(ei))
            if float(ei[j]) < ei_tol:
                stop_reason = f"ei_small(max_ei={float(ei[j]):.6g} < tol={ei_tol:.6g}, mode={cand_mode})"
                break

            eval_vec(C2[j], note="GBO_EI")

        if online_model is not None and not self.is_running:
            return
        best_x, best_score, best_r = current_best()
        if len(X) >= max_evals and stop_reason == "max_evals_reached":
            stop_reason = f"max_evals_reached({max_evals})"
        self.log_signal.emit(
            f"-> Best for Group {group_name}: score={best_score:.6g} "
            f"Ttot={best_r.ict.get('Ttot', float('nan')):.6f} {self._objective_metric_text(best_r.ict, best_r.bpm_metric)}"
        )
        self.log_signal.emit(f"[GROUP_BO] {group_name}: stop_reason={stop_reason}, evals={len(X)}")
        # Re-apply best settings to be safe (single multi-put)
        _ = self._evaluate_point_multi(
            device_label=f"Group:{group_name}",
            pv_to_value=vec_to_dict(best_x),
            mode="GROUP_BO_SIMUL",
            group=group_name,
            note="OPTIMIZED_SET"
        )

    # ----------------------------
    # Sequential 1D scan
    # ----------------------------

    # ----------------------------
    # Sequential 1D scan (Unimodal Bayesian Optimization on a discrete grid)
    # ----------------------------
    def _rbf_kernel(self, xa: np.ndarray, xb: np.ndarray, length_scale: float, sigma_f: float) -> np.ndarray:
        xa = xa.reshape(-1, 1)
        xb = xb.reshape(-1, 1)
        d2 = (xa - xb.T) ** 2
        return (sigma_f ** 2) * np.exp(-0.5 * d2 / (length_scale ** 2))

    def _gp_posterior(self, x_train: np.ndarray, y_train: np.ndarray, x_test: np.ndarray,
                      length_scale: float, sigma_f: float, sigma_n: float) -> tuple[np.ndarray, np.ndarray]:
        """Simple 1D GP posterior (no external deps). Returns mean, std."""
        if x_train.size == 0:
            return np.zeros_like(x_test, dtype=float), np.ones_like(x_test, dtype=float)

        K = self._rbf_kernel(x_train, x_train, length_scale, sigma_f) + (sigma_n ** 2) * np.eye(len(x_train))
        Ks = self._rbf_kernel(x_train, x_test, length_scale, sigma_f)
        Kss = self._rbf_kernel(x_test, x_test, length_scale, sigma_f) + 1e-12 * np.eye(len(x_test))

        # Cholesky for stability
        try:
            L = np.linalg.cholesky(K)
            alpha = np.linalg.solve(L.T, np.linalg.solve(L, y_train))
            mu = Ks.T @ alpha
            v = np.linalg.solve(L, Ks)
            cov = Kss - v.T @ v
            var = np.clip(np.diag(cov), 1e-12, np.inf)
            return mu.astype(float), np.sqrt(var).astype(float)
        except np.linalg.LinAlgError:
            # Fallback: diagonal posterior
            mu = np.full_like(x_test, float(np.mean(y_train)), dtype=float)
            std = np.full_like(x_test, float(np.std(y_train) + 1.0), dtype=float)
            return mu, std

    def _expected_improvement(self, mu: np.ndarray, std: np.ndarray, y_best: float, xi: float = 0.0) -> np.ndarray:
        """EI for maximization."""
        std = np.maximum(std, 1e-12)
        z = (mu - y_best - xi) / std
        # normal pdf/cdf (avoid scipy)
        pdf = (1.0 / np.sqrt(2.0 * np.pi)) * np.exp(-0.5 * z * z)
        cdf = 0.5 * (1.0 + np.vectorize(math.erf)(z / np.sqrt(2.0)))
        ei = (mu - y_best - xi) * cdf + std * pdf
        ei[std <= 1e-12] = 0.0
        return ei

    def _use_refinement(self, knob_name: str) -> bool:
        name = knob_name.lower()
        return ("qa" in name) or ("qm" in name)

    def perform_1d_scan(self, device_label: str, pv_name: str, scan_values: np.ndarray,
                        group: str = "", mode: str = "SEQUENTIAL"):

        if scan_values.size == 0 or (not self.is_running):
            return

        scan_values = self._resume_scan_candidates(mode, group, pv_name, np.asarray(scan_values, dtype=float))

        # Ensure sorted unique candidates
        candidates = np.unique(np.array(scan_values, dtype=float))
        candidates.sort()

        # Sequential method selection (user request):
        # - In SEQUENTIAL mode, allow choosing BO or discrete ternary search (unimodal assumption).
        # - In GROUP modes, always use BO.
        seq_method = str(self.config.get("seq_method", "BO")).upper()
        method = seq_method if str(mode).upper() == "SEQUENTIAL" else "BO"
        log_method = "TERNARY-1D" if method in ("TERNARY", "TERNARY_SEARCH", "BINARY", "BINARY_SEARCH") else "BO-1D"
        self.log_signal.emit(f"[{log_method}] Optimizing {device_label} ({pv_name}) on {len(candidates)} candidates ...")
        self._emit_progress({"kind": "bo1d_trace", "trace": {}})

        # Config knobs (reasonable defaults)
        min_init = int(self.config.get("bo_min_init", 5))  # initial evaluations
        if str(mode).upper() == "GROUP_BO":
            max_evals = _resolve_group_bo_max_evals(self.config, group or device_label, 1)
        else:
            max_evals = int(self.config.get("bo_max_evals", min(17, len(candidates))))
        ei_tol = float(self.config.get("bo_ei_tol", 1e-6))
        stall_iters = int(self.config.get("bo_stall_iters", 4))
        refine_enabled = bool(self.config.get("bo_refine", True))
        refine_factor = float(self.config.get("bo_refine_factor", 5.0))  # step -> step/refine_factor

        # GP hyperparameters.  The historical default was range/3; retain it
        # under legacy_fixed so loading an older config is behavior-compatible.
        x_range = float(candidates[-1] - candidates[0]) if len(candidates) >= 2 else 1.0
        axis_step = float(np.median(np.diff(candidates))) if len(candidates) >= 2 else x_range
        hp_mode = str(self.config.get("bo_hyperparameter_mode", "legacy_fixed")).lower()
        n_step_floor = float(self.config.get("bo_length_scale_n_step_floor", 2.0))
        length_scale, length_source = resolve_linac_length_scale(
            pv_name,
            axis_range=x_range,
            axis_step=axis_step,
            mode=hp_mode,
            n_step_floor=n_step_floor,
        )
        # Preserve an explicit old-style override, which is useful for an
        # offline comparison and was supported before BO2026.
        if "bo_length_scale" in self.config:
            length_scale = max(float(self.config["bo_length_scale"]), n_step_floor * abs(axis_step))
            length_source = "explicit_config_override"
        sigma_f = float(self.config.get("bo_sigma_f", 1.0))
        sigma_n = float(self.config.get("bo_sigma_n", 1e-2))
        xi = float(self.config.get("bo_xi", 0.0))
        if log_method == "BO-1D":
            self.log_signal.emit(
                f"[BO1D][GP] axis={pv_name}, mode={hp_mode}, length_scale={length_scale:.6g}, "
                f"source={length_source}, signal_sd={sigma_f:.6g}, noise_sd={sigma_n:.6g}"
            )

        X, Y, R = [], [], []  # store evaluated (x, score, EvalResult)

        resume_done_row = self._resume_seq_completed_row(mode, group, pv_name)
        if resume_done_row is not None:
            best_x = float(resume_done_row.get("set_value", float("nan")))
            if np.isfinite(best_x):
                if self._resume_pending_matches_seq(mode, group, pv_name):
                    self._resume_discarded_row = None
                self.log_signal.emit(
                    f"[RESUME] {device_label} already completed previously. Re-applying x={best_x:.6g}.")
                _ = self._evaluate_point(device_label, pv_name, best_x, mode=mode, group=group,
                                         note="RESUME_COMPLETED_SET")
                return

        resume_rows = self._resume_seq_rows.get(self._resume_seq_key(mode, group, pv_name), [])
        if resume_rows:
            dedup: Dict[float, Dict[str, Any]] = {}
            for row in resume_rows:
                note = str(row.get("note", ""))
                x = float(row.get("set_value", float("nan")))
                if (not np.isfinite(x)) or ("OPTIMIZED_SET" in note) or ("RESUME_COMPLETED_SET" in note):
                    continue
                key_x = round(x, 10)
                prev = dedup.get(key_x)
                if prev is None or float(row.get("score", float("-inf"))) >= float(prev.get("score", float("-inf"))):
                    dedup[key_x] = row
            if dedup:
                for _, row in sorted(dedup.items(), key=lambda item: item[0]):
                    X.append(float(row["set_value"]))
                    Y.append(float(row["score"]))
                    R.append(self._resume_eval_result(row))
                self.log_signal.emit(f"[RESUME] Warm start for {device_label}: loaded {len(X)} previous evaluations.")

        resume_pending_row = self._consume_resume_pending_seq_row(mode, group, pv_name)
        if resume_pending_row is not None:
            x_resume = float(resume_pending_row.get("set_value", float("nan")))
            if np.isfinite(x_resume):
                self.log_signal.emit(
                    f"[RESUME] Re-measuring discarded last point for {device_label} before continuing."
                )
                r_resume = self._evaluate_point(
                    device_label,
                    pv_name,
                    x_resume,
                    mode=mode,
                    group=group,
                    note="RESUME_REMEASURE_LAST",
                )
                X.append(float(x_resume))
                Y.append(float(r_resume.score))
                R.append(r_resume)

        def eval_at(x: float, note: str = ""):
            reused = self._try_reuse_initial_point(
                device_label=device_label,
                pv_name=pv_name,
                set_value=float(x),
                mode=mode,
                group=group,
                note=note,
            )
            if reused is not None:
                X.append(float(x))
                Y.append(float(reused.score))
                R.append(reused)
                Ttot = reused.ict.get("Ttot", float("nan"))
                self.log_signal.emit(
                    f"  x={x:.6g}  Ttot={Ttot:.6f}  {self._objective_metric_text(reused.ict, reused.bpm_metric)}  [REUSE_INIT]"
                )
                return reused
            r = self._evaluate_point(device_label, pv_name, float(x), mode=mode, group=group, note=note)
            X.append(float(x))
            Y.append(float(r.score))
            R.append(r)
            Ttot = r.ict.get("Ttot", float("nan"))
            self.log_signal.emit(
                f"  x={x:.6g}  Ttot={Ttot:.6f}  {self._objective_metric_text(r.ict, r.bpm_metric)}"
            )
            return r

        # ------------------------------------------------------------------
        # Discrete ternary search (unimodal) on the candidate grid
        # ------------------------------------------------------------------
        if method in ("TERNARY", "TERNARY_SEARCH", "BINARY", "BINARY_SEARCH"):
            # Cache helper
            def _y_at(xv: float) -> float:
                # Ensure evaluated
                if xv not in set(X):
                    eval_at(xv, note="TERNARY_EVAL")
                # Return stored
                return float(Y[X.index(xv)])

            l = 0
            r = len(candidates) - 1

            # Ternary search loop (discrete)
            while self.is_running and (r - l) > 2 and len(X) < max_evals:
                m1 = l + (r - l) // 3
                m2 = r - (r - l) // 3
                x1 = float(candidates[m1])
                x2 = float(candidates[m2])

                y1 = _y_at(x1)
                if not self.is_running or len(X) >= max_evals:
                    break
                y2 = _y_at(x2)

                # Keep the side containing the higher value (maximize)
                if y1 < y2:
                    l = m1
                else:
                    r = m2

            # Final local exhaustive on the remaining small bracket (and within remaining budget)
            for j in range(l, r + 1):
                if not self.is_running or len(X) >= max_evals:
                    break
                xj = float(candidates[j])
                if xj not in set(X):
                    eval_at(xj, note="TERNARY_FINAL")

            idxb = int(np.argmax(Y))
            best_x, best_score, best_r = float(X[idxb]), float(Y[idxb]), R[idxb]

            # Refinement (step/5) is intentionally NOT used for phase/timing; it is controlled later (QA/QM only).
            # Jump to final set & return.
            self.log_signal.emit(
                f"-> Best for {device_label}: {best_x:.6g}  "
                f"Ttot={best_r.ict.get('Ttot', float('nan')):.6f}  {self._objective_metric_text(best_r.ict, best_r.bpm_metric)}"
            )
            _ = self._evaluate_point(device_label, pv_name, best_x, mode=mode, group=group, note="OPTIMIZED_SET")
            return

        # 1) initial points: endpoints + mid (or fewer if grid is tiny)
        init_x = []
        init_x.append(float(candidates[0]))
        if len(candidates) > 1:
            init_x.append(float(candidates[-1]))
        init_x.append(float(candidates[len(candidates) // 2]))
        init_x = list(dict.fromkeys(init_x))  # unique preserve order

        # If user wants more init, fill evenly
        if min_init > len(init_x) and len(candidates) > len(init_x):
            extra = np.linspace(candidates[0], candidates[-1], min_init)
            extra = [float(candidates[np.argmin(np.abs(candidates - e))]) for e in extra]
            for e in extra:
                if e not in init_x:
                    init_x.append(e)

        # evaluate init
        for x in init_x[:max_evals]:
            if not self.is_running:
                return
            if x in set(X):
                continue
            eval_at(x, note="BO_INIT")

        def current_best():
            idx = int(np.argmax(Y))
            return float(X[idx]), float(Y[idx]), R[idx]

        # 2) BO loop on the full candidate grid
        stall = 0
        prev_best_x = None

        while self.is_running and len(X) < max_evals:
            remaining = np.array([c for c in candidates if c not in set(X)], dtype=float)
            if remaining.size == 0:
                break

            x_train = np.array(X, dtype=float)
            y_train = np.array(Y, dtype=float)
            mu, std = self._gp_posterior(x_train, y_train, remaining, length_scale, sigma_f, sigma_n)
            y_best = float(np.max(y_train))
            ei = self._expected_improvement(mu, std, y_best, xi=xi)
            k = int(np.argmax(ei))
            mu_plot, std_plot = self._gp_posterior(x_train, y_train, candidates, length_scale, sigma_f, sigma_n)
            ei_plot = self._expected_improvement(mu_plot, std_plot, y_best, xi=xi)
            self._emit_bo1d_trace(
                axis_name=device_label,
                x_grid=candidates,
                mu=mu_plot,
                std=std_plot,
                acq=ei_plot,
                chosen_x=float(remaining[k]),
                chosen_acq=float(ei[k]),
                x_obs=x_train,
                y_obs=y_train,
            )
            if float(ei[k]) < ei_tol:
                break

            cand = float(remaining[k])
            eval_at(cand, note="BO_EI")

            bx, by, _ = current_best()
            if prev_best_x is not None and bx == prev_best_x:
                stall += 1
            else:
                stall = 0
                prev_best_x = bx

            if stall >= stall_iters:
                break

        best_x, best_score, best_r = current_best()

        pv_lower = pv_name.lower()
        is_qaqm = ("qa" in pv_lower) or ("qm" in pv_lower)

        if refine_enabled and is_qaqm and len(candidates) >= 2:
            # infer coarse step from candidates (median diff)
            diffs = np.diff(candidates)
            coarse_step = float(np.median(diffs[diffs > 0])) if np.any(diffs > 0) else 0.0

            if coarse_step > 0:
                fine_step = coarse_step / refine_factor

                # Keep refinement global: re-sample the full original range with a finer step.
                lo = float(candidates[0])
                hi = float(candidates[-1])
                fine = np.arange(lo, hi + fine_step * 0.5, fine_step, dtype=float)
                fine = np.unique(np.clip(fine, candidates[0], candidates[-1]))
                fine.sort()

                # allow a few more evals, but keep bounded
                refine_budget = int(self.config.get("bo_refine_evals", min(10, len(fine))))
                max_total = max_evals + refine_budget

                self.log_signal.emit(
                    f"[BO-1D] Refinement: global step {coarse_step:.6g} -> {fine_step:.6g}, range=[{lo:.6g},{hi:.6g}], budget={refine_budget}"
                )

                stall2 = 0
                prev_best_x2 = best_x

                while self.is_running and len(X) < max_total:
                    remaining = np.array([c for c in fine if c not in set(X)], dtype=float)
                    if remaining.size == 0:
                        break

                    x_train = np.array(X, dtype=float)
                    y_train = np.array(Y, dtype=float)
                    mu, std = self._gp_posterior(x_train, y_train, remaining, length_scale=max(fine_step * 3, 1e-6),
                                                 sigma_f=sigma_f, sigma_n=sigma_n)
                    y_best = float(np.max(y_train))
                    ei = self._expected_improvement(mu, std, y_best, xi=xi)
                    k = int(np.argmax(ei))
                    mu_plot, std_plot = self._gp_posterior(
                        x_train, y_train, fine,
                        length_scale=max(fine_step * 3, 1e-6),
                        sigma_f=sigma_f,
                        sigma_n=sigma_n,
                    )
                    ei_plot = self._expected_improvement(mu_plot, std_plot, y_best, xi=xi)
                    self._emit_bo1d_trace(
                        axis_name=device_label,
                        x_grid=fine,
                        mu=mu_plot,
                        std=std_plot,
                        acq=ei_plot,
                        chosen_x=float(remaining[k]),
                        chosen_acq=float(ei[k]),
                        x_obs=x_train,
                        y_obs=y_train,
                    )
                    if float(ei[k]) < ei_tol:
                        break

                    eval_at(float(remaining[k]), note="BO_REFINE")

                    bx, by, _ = current_best()
                    if bx == prev_best_x2:
                        stall2 += 1
                    else:
                        stall2 = 0
                        prev_best_x2 = bx

                    if stall2 >= stall_iters:
                        break

                idxb = int(np.argmax(Y))
            best_x, best_score, best_r = float(X[idxb]), float(Y[idxb]), R[idxb]

        if not self.is_running:
            return

        self.log_signal.emit(
            f"-> Best for {device_label}: {best_x:.6g}  "
            f"Ttot={best_r.ict.get('Ttot', float('nan')):.6f}  {self._objective_metric_text(best_r.ict, best_r.bpm_metric)}"
        )
        _ = self._evaluate_point(device_label, pv_name, best_x, mode=mode, group=group, note="OPTIMIZED_SET")

        # ----------------------------

    # Group-LBO (discrete / quantized)
    # ----------------------------
    def _quantize(self, x: float, x0: float, step: float, lo: float, hi: float) -> float:
        if step <= 0:
            return float(np.clip(x, lo, hi))
        xq = x0 + round((x - x0) / step) * step
        return float(np.clip(xq, lo, hi))

    def _read_current(self, pv_write: str, pv_read: Optional[str] = None) -> float:
        if pv_read is None:
            pv_read = _default_readback_pv(pv_write)
        return self.interface.read_current(pv_write=pv_write, pv_read=pv_read, quantize_phase=True)

    def _lbo_group(self, group_name: str, params: List[tuple]):
        """
        params: list of (label, pv_write, step, half_range) for each dimension
        Optimization:
        - Start at current point x0
        - Repeat a few iterations:
            - Choose a direction (coordinate + random)
            - Sample candidates along that line (discrete/quantized)
            - Move to best candidate
        """
        if not self.is_running:
            return
        self._emit_progress({"kind": "bo1d_trace", "trace": {}})

        # current point
        x0 = []
        bounds = []
        steps = []
        labels = []
        pvs = []
        for p in params:
            label, pv_write, step, half = p[:4]
            cur = self._read_current(pv_write)  # use write as read by default
            lo = cur - half
            hi = cur + half
            if len(p) >= 6:
                lo = float(p[4])
                hi = float(p[5])
                if hi < lo:
                    lo, hi = hi, lo
            cur = float(np.clip(cur, lo, hi))
            x0.append(cur)
            bounds.append((lo, hi))
            steps.append(step)
            labels.append(label)
            pvs.append(pv_write)

        x = np.array(x0, dtype=float)

        # Evaluation helper
        def eval_vec(x_vec: np.ndarray, note: str = "") -> EvalResult:
            xq = np.asarray(x_vec, dtype=float)
            reused = self._try_reuse_initial_vector(labels=labels, pvs=pvs, x_vec=xq, mode="GROUP_LBO",
                                                    group=group_name, note=note)
            if reused is not None:
                self.log_signal.emit(
                    f"[{group_name}] Ttot={reused.ict.get('Ttot', float('nan')):.6f} "
                    f"{self._objective_metric_text(reused.ict, reused.bpm_metric)}  x={np.array2string(xq, precision=6)}  [REUSE_INIT]"
                )
                return reused
            # Apply all PVs (sequential put), then single settle+read (practical)
            # Put all
            self._write_setpoints({pv_name: float(v) for pv_name, v in zip(pvs, x_vec)})
            # settle
            t0 = time.time()
            while self.is_running and (time.time() - t0) < self.settle_sec:
                time.sleep(0.1)
            ict, measurement_ok, measurement_reason = self._read_icts()
            score = self._score(ict)
            r = EvalResult(
                t_iso=datetime.datetime.now().isoformat(timespec="seconds"),
                device_label=",".join(labels),
                pv_name=",".join(pvs),
                set_value=float("nan"),
                ict=ict,
                score=float(score),
                measurement_ok=bool(measurement_ok),
                measurement_reason=str(measurement_reason),
                note=(
                    f"{note} | ICT_INVALID_ZERO: {measurement_reason}"
                    if ((not measurement_ok) and note)
                    else (f"ICT_INVALID_ZERO: {measurement_reason}" if not measurement_ok else note)
                ),
                group=group_name,
                mode="GROUP_LBO",
            )
            # For vector, store set_value as NaN; still log.
            self._append_csv(r)
            self._record_evaluation(r, {pv_name: float(v) for pv_name, v in zip(pvs, x_vec)})
            # log message
            self.log_signal.emit(
                f"[{group_name}] Ttot={ict.get('Ttot', float('nan')):.6f} {self._objective_metric_text(ict, r.bpm_metric)}  x={np.array2string(x_vec, precision=6)}"
            )
            return r

        # initial eval
        best = eval_vec(x, note="LBO_INIT")
        best_x = x.copy()

        # iterations (small fixed number to avoid runaway)
        iters = int(self.config.get("lbo_iters", 4))
        line_samples = int(self.config.get("lbo_line_samples", 11))

        rng = np.random.default_rng()

        def clamp_quantize_vec(x_candidate: np.ndarray) -> np.ndarray:
            y = []
            for i in range(len(x_candidate)):
                lo, hi = bounds[i]
                y.append(self._quantize(float(x_candidate[i]), x0[i], steps[i], lo, hi))
            return np.array(y, dtype=float)

        for it in range(iters):
            if not self.is_running:
                return

            # Choose direction: coordinate directions + occasional random direction
            directions = []
            # coordinate directions
            for i in range(len(x)):
                d = np.zeros_like(x)
                d[i] = 1.0
                directions.append(d)
            # add one random direction
            rd = rng.normal(size=len(x))
            if np.linalg.norm(rd) > 0:
                rd = rd / np.linalg.norm(rd)
            directions.append(rd)

            improved_in_iter = False
            for d in directions:
                if not self.is_running:
                    return

                # sample alpha in [-1,1] then scale by half-range in that direction
                alphas = np.linspace(-1.0, 1.0, line_samples)
                cand = []
                for a in alphas:
                    # propose
                    prop = best_x + a * d
                    # scale to meaningful range per-dimension: use half-range as scale
                    # (so that alpha=1 can reach near bounds depending on direction)
                    scaled = best_x + a * d * np.array([b[1] - b[0] for b in bounds]) * 0.5
                    q = clamp_quantize_vec(scaled)
                    cand.append(q)

                # unique candidates
                uniq = []
                seen = set()
                for v in cand:
                    key = tuple(np.round(v / np.array(steps), 0)) if all(s > 0 for s in steps) else tuple(v)
                    if key in seen:
                        continue
                    seen.add(key)
                    uniq.append(v)

                # evaluate
                for v in uniq:
                    if not self.is_running:
                        return
                    r = eval_vec(v)
                    if r.score > best.score:
                        best = r
                        best_x = v.copy()
                        improved_in_iter = True

            # if no improvement, stop early
            if not improved_in_iter:
                break

        # finalize: set best_x and log
        if self.is_running:
            self._write_setpoints({pv_name: float(v) for pv_name, v in zip(pvs, best_x)})
            t0 = time.time()
            while self.is_running and (time.time() - t0) < self.settle_sec:
                time.sleep(0.1)
            _ = eval_vec(best_x, note="LBO_FINAL")

    # ----------------------------
    # Main run
    # ----------------------------
    def run(self):
        self.log_signal.emit(f"=== Optimization Started (profile={self.run_profile}, mode={self.mode}) ===")
        try:
            self._dump_initial_snapshot()
            if self.run_profile == RUN_PROFILE_DEVELOPER:
                self._run_correctors()
            elif self.mode == "GROUP_LBO":
                self._run_group_lbo()
            elif self.mode == "GROUP_BO":
                self._run_group_bo()
            else:
                self._run_sequential()
            if self.run_profile == RUN_PROFILE_MAIN and self.config.get("correctors") and self.is_running:
                self._emit_group_state("correctors", "running")
                self._run_correctors()
                if self.is_running:
                    self._emit_group_state("correctors", "done")
        except InterruptedError as e:
            if self.is_running:
                self._emit_progress({"kind": "run_error", "message": str(e)})
            else:
                self.log_signal.emit("[STOP] Optimization stopped by user.")
        except Exception as e:
            self._emit_progress({"kind": "run_error", "message": str(e)})
            self.log_signal.emit(f"Error: {str(e)}")
        finally:
            self.log_signal.emit("=== Optimization Finished ===")
            self.finished_signal.emit()

    def _write_setpoints(self, values):
        self._pause_checkpoint()
        if self.run_profile == RUN_PROFILE_DEVELOPER or self.config.get("correctors"):
            selected = {spec["pv_write"] for spec in self.developer_actuators}
            limits = self.config.get("actuator_limits", {})
            corrector_pvs = {f"{name}:currentWrite" for name in BT_SEQUENCE if str(name).lower().startswith("z")}
            for pv, value in values.items():
                if self.run_profile == RUN_PROFILE_MAIN and pv not in corrector_pvs and pv not in selected:
                    continue  # Existing Main groups (RF phases, solenoid, QA/QM).
                if pv not in selected or pv not in limits:
                    raise ValueError(f"Unselected/unverified corrector: {pv}")
                lo, hi = limits[pv]
                if not np.isfinite(value) or not lo <= value <= hi:
                    raise ValueError(f"Corrector command outside verified range: {pv}={value}")
        if len(values) == 1:
            pv, value = next(iter(values.items()))
            self.interface.pv_put(pv, value)
        else:
            self.interface.pv_put_many(values)

    def _run_correctors(self):
        if not self.developer_actuators:
            raise ValueError("Corrector optimization requires at least one selected actuator.")
        if self.objective_type in (DEVELOPER_OBJECTIVE_BPM, DEVELOPER_OBJECTIVE_INTENSITY) and not self.objective_bpm_names:
            raise ValueError("BPM objective requires at least one selected BPM.")

        params: List[Tuple[str, str, float, float]] = []
        for spec in self.developer_actuators:
            label = str(spec.get("label", spec.get("name", "Actuator")))
            pv_write = str(spec.get("pv_write", ""))
            half_range = float(spec.get("half_range", DEVELOPER_DEFAULT_HALF_RANGE_A))
            step = float(spec.get("step", DEVELOPER_DEFAULT_STEP_A))
            if not pv_write:
                continue
            params.append((label, pv_write, step, half_range))

        if not params:
            raise ValueError("Could not build a valid corrector list.")

        self.log_signal.emit(
            f"--- Corrector Run | objective={self.objective_type} | plane={self.objective_bpm_plane} | "
            f"actuators={len(params)} | bpms={len(self.objective_bpm_names)} ---"
        )

        if self.mode == "GROUP_BO":
            bounded_params: List[Tuple[str, str, float, float, float, float]] = []
            for label, pv_write, step, half_range in params:
                cur = self._read_current(pv_write, None)
                lo, hi, trimmed, raw_lo, raw_hi = _trim_centered_bounds(
                    cur,
                    half_range,
                    self.config["actuator_limits"][pv_write][0],
                    self.config["actuator_limits"][pv_write][1],
                )
                if trimmed:
                    self.log_signal.emit(
                        f"[Correctors] {label}: steer range trimmed "
                        f"[{raw_lo:.4f}, {raw_hi:.4f}] -> [{lo:.4f}, {hi:.4f}] A"
                    )
                bounded_params.append((label, pv_write, step, half_range, lo, hi))
                self._emit_developer_actuator_state(label, "running")
            self._group_bo_simultaneous(DEVELOPER_GROUP_NAME, bounded_params)
            if self.is_running:
                for label, _pv_write, _step, _half_range, _lo, _hi in bounded_params:
                    self._emit_developer_actuator_state(label, "done")
            return

        for label, pv_write, step, half_range in params:
            if not self.is_running:
                break
            self._emit_developer_actuator_state(label, "running")
            cur = self._read_current(pv_write, None)
            lo, hi, trimmed, raw_lo, raw_hi = _trim_centered_bounds(
                cur,
                half_range,
                self.config["actuator_limits"][pv_write][0],
                self.config["actuator_limits"][pv_write][1],
            )
            if trimmed:
                self.log_signal.emit(
                    f"[Correctors] {label}: steer range trimmed "
                    f"[{raw_lo:.4f}, {raw_hi:.4f}] -> [{lo:.4f}, {hi:.4f}] A"
                )
            scan = _scan_from_bounds(lo, hi, step)
            self.perform_1d_scan(label, pv_write, scan, group=DEVELOPER_GROUP_NAME, mode="SEQUENTIAL")
            if self.is_running:
                self._emit_developer_actuator_state(label, "done")

    def _run_sequential(self):
        # Scan settings (GUI-configurable)
        gun_phase_half = float(self.config.get("gun_phase_half_range_deg", GUN_PHASE_HALF_RANGE_DEG))
        gun_phase_step = float(self.config.get("gun_phase_step_deg", GUN_PHASE_STEP_DEG))
        sol_min = float(self.config.get("solenoide_min_a", SOLENOIDE_MIN_A))
        sol_max = float(self.config.get("solenoide_max_a", SOLENOIDE_MAX_A))
        sol_step = float(self.config.get("solenoide_step_a", SOLENOIDE_STEP_A))
        l0_phase_half = float(self.config.get("l0_phase_half_range_deg", L0_PHASE_HALF_RANGE_DEG))
        l0_phase_step = float(self.config.get("l0_phase_step_deg", L0_PHASE_STEP_DEG))
        phase_half = float(self.config.get("phase_half_range_deg", PHASE_HALF_RANGE_DEG))
        phase_step = float(self.config.get("phase_step_deg", PHASE_STEP_DEG))
        qa_half = float(self.config.get("qa_half_range_a", QA_HALF_RANGE_A))
        qa_step = float(self.config.get("qa_step_a", QA_STEP_A))
        qm_half = float(self.config.get("qm_half_range_a", QM_HALF_RANGE_A))
        qm_step = float(self.config.get("qm_step_a", QM_STEP_A))
        timing_step = float(self.config.get("timing_step_ns", TIMING_STEP_NS))
        timing_half_steps = int(self.config.get("timing_half_steps", TIMING_HALF_STEPS))
        gun_group_name = TARGET_LABELS["gun_sol_l0"]

        # 1) GUN/SOLENOIDE/L0 mode (head of sequence)
        if self.config.get("gun_sol_l0_phase", False) and self.is_running:
            self._emit_group_state("gun_sol_l0", "running")
            self.log_signal.emit(f"--- {gun_group_name} (GUN -> Solenoid -> L0) ---")
            sol_lo = min(sol_min, sol_max)
            sol_hi = max(sol_min, sol_max)
            specs = [
                ("GUN phase", "RFGUN:PHASE_WRITE", gun_phase_half, gun_phase_step),
                ("Solenoid current", SOLENOIDE_WRITE_PV, None, sol_step),
                ("L0 phase", "CM0L:phaseWrite", l0_phase_half, l0_phase_step),
            ]
            for label, pv_write, half, step in specs:
                if not self.is_running:
                    break
                if "SOLENOIDE" in pv_write:
                    scan = np.arange(sol_lo, sol_hi + step * 0.5, step)
                else:
                    cur = self._read_current(pv_write, None)
                    scan = np.arange(cur - half, cur + half + step, step)
                self.perform_1d_scan(label, pv_write, scan, group=gun_group_name, mode="SEQUENTIAL")
            if self.is_running:
                self._emit_group_state("gun_sol_l0", "done")

        # 2) Klystron phases (1..8)
        if self.config.get("kly_phase", False) and self.is_running:
            self._emit_group_state("kly_phase", "running")
            self.log_signal.emit("--- Klystron Phase (1-8) ---")
            for i in range(1, 9):
                if not self.is_running:
                    break
                pv_write = f"CM{i}L:phaseWrite"
                pv_read = f"CM{i}L:phaseRead"
                cur = self._read_current(pv_write, pv_read)
                scan = np.arange(cur - phase_half, cur + phase_half + phase_step, phase_step)
                self.perform_1d_scan(f"Klystron {i} phase", pv_write, scan, group=f"L{i}", mode="SEQUENTIAL")
            if self.is_running:
                self._emit_group_state("kly_phase", "done")

        # 3) Timing (after phase)
        if self.config.get("timing", False) and self.is_running:
            self._emit_group_state("timing", "running")
            self.log_signal.emit("--- Timing ---")
            pv = "EVE_LINAC:OUT0:SetData"
            cur = self._read_current(pv, None)
            scan = cur + timing_step * np.arange(-timing_half_steps, timing_half_steps + 1, 1)
            self.perform_1d_scan("Timing", pv, scan, group="Timing", mode="SEQUENTIAL")
            if self.is_running:
                self._emit_group_state("timing", "done")

        # 4) QA
        if self.config.get("qa", False) and self.is_running:
            self._emit_group_state("qa", "running")
            self.log_signal.emit("--- QA Magnets (QA1L-QA5L) ---")
            for q in ["QA1L", "QA2L", "QA3L", "QA4L", "QA5L"]:
                if not self.is_running:
                    break
                pv_write = f"{q}:currentWrite"
                # optional read PV name if exists; fallback to write
                pv_read = f"{q}:currentRead"
                cur = self._read_current(pv_write, pv_read)
                scan = np.arange(cur - qa_half, cur + qa_half + qa_step, qa_step)
                self.perform_1d_scan(q, pv_write, scan, group="QA", mode="SEQUENTIAL")
            if self.is_running:
                self._emit_group_state("qa", "done")

        # 5) QM
        if self.config.get("qm", False) and self.is_running:
            self._emit_group_state("qm", "running")
            self.log_signal.emit("--- QM Magnets (QM1L-QM3L) ---")
            for m in ["QM1L", "QM2L", "QM3L"]:
                if not self.is_running:
                    break
                pv_write = f"{m}:currentWrite"
                pv_read = f"{m}:currentRead"
                cur = self._read_current(pv_write, pv_read)
                scan = np.arange(cur - qm_half, cur + qm_half + qm_step, qm_step)
                self.perform_1d_scan(m, pv_write, scan, group="QM", mode="SEQUENTIAL")
            if self.is_running:
                self._emit_group_state("qm", "done")

    def _run_group_lbo(self):
        # Scan settings (GUI-configurable)
        gun_phase_half = float(self.config.get("gun_phase_half_range_deg", GUN_PHASE_HALF_RANGE_DEG))
        gun_phase_step = float(self.config.get("gun_phase_step_deg", GUN_PHASE_STEP_DEG))
        sol_min = float(self.config.get("solenoide_min_a", SOLENOIDE_MIN_A))
        sol_max = float(self.config.get("solenoide_max_a", SOLENOIDE_MAX_A))
        sol_step = float(self.config.get("solenoide_step_a", SOLENOIDE_STEP_A))
        l0_phase_half = float(self.config.get("l0_phase_half_range_deg", L0_PHASE_HALF_RANGE_DEG))
        l0_phase_step = float(self.config.get("l0_phase_step_deg", L0_PHASE_STEP_DEG))
        phase_half = float(self.config.get("phase_half_range_deg", PHASE_HALF_RANGE_DEG))
        phase_step = float(self.config.get("phase_step_deg", PHASE_STEP_DEG))
        qa_half = float(self.config.get("qa_half_range_a", QA_HALF_RANGE_A))
        qa_step = float(self.config.get("qa_step_a", QA_STEP_A))
        qm_half = float(self.config.get("qm_half_range_a", QM_HALF_RANGE_A))
        qm_step = float(self.config.get("qm_step_a", QM_STEP_A))
        timing_step = float(self.config.get("timing_step_ns", TIMING_STEP_NS))
        timing_half_steps = int(self.config.get("timing_half_steps", TIMING_HALF_STEPS))
        # Group order: GUN&SOLENOIDE&L0, L1-4, L5-8, Timing, QA, QM
        if self.config.get("gun_sol_l0_phase", False) and self.is_running:
            sol_lo = min(sol_min, sol_max)
            sol_hi = max(sol_min, sol_max)
            params = [
                ("GUN phase", "RFGUN:PHASE_WRITE", gun_phase_step, gun_phase_half),
                ("SOLENOIDE current", SOLENOIDE_WRITE_PV, sol_step, 0.0, sol_lo, sol_hi),
                ("L0 phase", "CM0L:phaseWrite", l0_phase_step, l0_phase_half),
            ]
            self.log_signal.emit("--- GROUP_LBO: GUN&SOLENOIDE&L0 ---")
            self._lbo_group("GUN&SOLENOIDE&L0", params)

        if self.config.get("kly_phase", False) and self.is_running:
            # L1-4
            if self.config.get("grp_L1_4", True):
                params = []
                for i in range(1, 5):
                    pv_write = f"CM{i}L:phaseWrite"
                    params.append((f"L{i}", pv_write, phase_step, phase_half))
                self.log_signal.emit("--- GROUP_LBO: L1-4 ---")
                self._lbo_group("L1-4", params)

            # L5-8
            if self.is_running and self.config.get("grp_L5_8", True):
                params = []
                for i in range(5, 9):
                    pv_write = f"CM{i}L:phaseWrite"
                    params.append((f"L{i}", pv_write, phase_step, phase_half))
                self.log_signal.emit("--- GROUP_LBO: L5-8 ---")
                self._lbo_group("L5-8", params)

        # Timing
        if self.config.get("timing", False) and self.is_running:
            pv = "EVE_LINAC:OUT0:SetData"
            params = [("Timing", pv, timing_step, timing_step * timing_half_steps)]
            self.log_signal.emit("--- GROUP_LBO: Timing ---")
            self._lbo_group("Timing", params)

        # QA
        if self.config.get("qa", False) and self.is_running:
            params = [(q, f"{q}:currentWrite", qa_step, qa_half) for q in ["QA1L", "QA2L", "QA3L", "QA4L", "QA5L"]]
            self.log_signal.emit("--- GROUP_LBO: QA ---")
            self._lbo_group("QA", params)

        # QM
        if self.config.get("qm", False) and self.is_running:
            params = [(m, f"{m}:currentWrite", qm_step, qm_half) for m in ["QM1L", "QM2L", "QM3L"]]
            self.log_signal.emit("--- GROUP_LBO: QM ---")
            self._lbo_group("QM", params)

    def _run_group_bo(self):
        # Scan settings (GUI-configurable)
        gun_phase_half = float(self.config.get("gun_phase_half_range_deg", GUN_PHASE_HALF_RANGE_DEG))
        gun_phase_step = float(self.config.get("gun_phase_step_deg", GUN_PHASE_STEP_DEG))
        sol_min = float(self.config.get("solenoide_min_a", SOLENOIDE_MIN_A))
        sol_max = float(self.config.get("solenoide_max_a", SOLENOIDE_MAX_A))
        sol_step = float(self.config.get("solenoide_step_a", SOLENOIDE_STEP_A))
        l0_phase_half = float(self.config.get("l0_phase_half_range_deg", L0_PHASE_HALF_RANGE_DEG))
        l0_phase_step = float(self.config.get("l0_phase_step_deg", L0_PHASE_STEP_DEG))
        phase_half = float(self.config.get("phase_half_range_deg", PHASE_HALF_RANGE_DEG))
        phase_step = float(self.config.get("phase_step_deg", PHASE_STEP_DEG))
        qa_half = float(self.config.get("qa_half_range_a", QA_HALF_RANGE_A))
        qa_step = float(self.config.get("qa_step_a", QA_STEP_A))
        qm_half = float(self.config.get("qm_half_range_a", QM_HALF_RANGE_A))
        qm_step = float(self.config.get("qm_step_a", QM_STEP_A))
        timing_step = float(self.config.get("timing_step_ns", TIMING_STEP_NS))
        timing_half_steps = int(self.config.get("timing_half_steps", TIMING_HALF_STEPS))
        gun_group_name = TARGET_LABELS["gun_sol_l0"]

        # Simultaneous Group BO:
        # - For multi-PV groups (L1-4, L5-8, QA, QM), use multi-dimensional BO that proposes a *vector*
        #   and applies all PVs in one shot (single wait + single measurement per iteration).
        # - For single-PV group (Timing), fall back to the 1D scan optimizer.
        self.log_signal.emit("[GROUP_BO] Using simultaneous multi-parameter BO for multi-PV groups.")

        # Group order: GUN&SOLENOIDE&L0, L1-4, L5-8, Timing, QA, QM
        if self.config.get("gun_sol_l0_phase", False) and self.is_running:
            self._emit_group_state("gun_sol_l0", "running")
            sol_lo = min(sol_min, sol_max)
            sol_hi = max(sol_min, sol_max)
            params = [
                ("GUN phase", "RFGUN:PHASE_WRITE", gun_phase_step, gun_phase_half),
                ("Solenoid current", SOLENOIDE_WRITE_PV, sol_step, 0.0, sol_lo, sol_hi),
                ("L0 phase", "CM0L:phaseWrite", l0_phase_step, l0_phase_half),
            ]
            self._group_bo_simultaneous(gun_group_name, params)
            if self.is_running:
                self._emit_group_state("gun_sol_l0", "done")

        if self.config.get("kly_phase", False) and self.is_running:
            self._emit_group_state("kly_phase", "running")
            if self.config.get("grp_L1_4", True):
                self._emit_group_state("kly_group_1", "running")
                params = [(f"L{i}", f"CM{i}L:phaseWrite", phase_step, phase_half) for i in range(1, 5)]
                self._group_bo_simultaneous("L1-4", params)
                if self.is_running:
                    self._emit_group_state("kly_group_1", "done")
            if self.config.get("grp_L5_8", True):
                self._emit_group_state("kly_group_2", "running")
                params = [(f"L{i}", f"CM{i}L:phaseWrite", phase_step, phase_half) for i in range(5, 9)
                          if not (i == 8 and self.config.get("correctors_l8", False))]
                self._group_bo_simultaneous("L5-8", params)
                if self.is_running:
                    self._emit_group_state("kly_group_2", "done")
            if self.is_running:
                self._emit_group_state("kly_phase", "done")

        if self.config.get("timing", False) and self.is_running:
            self._emit_group_state("timing", "running")
            pv = "EVE_LINAC:OUT0:SetData"
            cur = self._read_current(pv, None)
            scan = cur + timing_step * np.arange(-timing_half_steps, timing_half_steps + 1, 1)
            self.perform_1d_scan("Timing", pv, scan, group="Timing", mode="GROUP_BO")
            if self.is_running:
                self._emit_group_state("timing", "done")

        if self.config.get("qa", False) and self.is_running:
            self._emit_group_state("qa", "running")
            params = [(q, f"{q}:currentWrite", qa_step, qa_half) for q in ["QA1L", "QA2L", "QA3L", "QA4L", "QA5L"]]
            self._group_bo_simultaneous("QA", params)
            if self.is_running:
                self._emit_group_state("qa", "done")

        if self.config.get("qm", False) and self.is_running:
            self._emit_group_state("qm", "running")
            params = [(m, f"{m}:currentWrite", qm_step, qm_half) for m in ["QM1L", "QM2L", "QM3L"]]
            self._group_bo_simultaneous("QM", params)
            if self.is_running:
                self._emit_group_state("qm", "done")


# ----------------------------
# GUI
# ----------------------------
class ClickOpenComboBox(QComboBox):
    """Combo box that opens popup when the box body is clicked."""

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self.showPopup()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Linac Optimization GUI")
        self.resize(1180, 900)

        self.worker: Optional[OptimizationWorker] = None
        self.interface: Optional[InterfaceATF2_LinacBT] = None
        self.dr_bpm_reader = DRBPMReader(nsamples=1)
        self.save_path = default_linacopt_save_dir()
        self._run_profile: str = RUN_PROFILE_MAIN
        self.target_checks: Dict[str, QCheckBox] = {}
        self.target_status_labels: Dict[str, QLabel] = {}
        self.target_value_labels: Dict[str, QLabel] = {}
        self.target_states: Dict[str, str] = {}
        self._run_mode: str = ""
        self._run_failed: bool = False
        self._shutdown_in_progress: bool = False
        self._current_value_refresh_failed: bool = False
        self.current_machine_origin: Optional[Dict[str, Any]] = None
        self.current_measurements_csv_by_profile: Dict[str, Optional[Path]] = {
            RUN_PROFILE_MAIN: None,
            RUN_PROFILE_DEVELOPER: None,
        }
        self.resume_snapshot_state: Optional[Dict[str, Any]] = None
        self._plot_state: Dict[str, Dict[str, Any]] = {
            RUN_PROFILE_MAIN: {
                "eval_index": [],
                "ttot": [],
                "metric": [],
                "metric_label": "DR",
                "measurement_ok": [],
                "discarded_eval_index": [],
                "discarded_ttot": [],
                "discarded_metric": [],
                "bo1d_trace": None,
            },
            RUN_PROFILE_DEVELOPER: {
                "eval_index": [],
                "ttot": [],
                "metric": [],
                "metric_label": "DR",
                "measurement_ok": [],
                "discarded_eval_index": [],
                "discarded_ttot": [],
                "discarded_metric": [],
                "bo1d_trace": None,
            },
        }
        self._status_labels: Dict[str, QLabel] = {}
        self._result_labels: Dict[str, QLabel] = {}
        self._log_widgets: Dict[str, QTextEdit] = {}
        self._plot_widgets: Dict[str, Dict[str, Any]] = {}
        self._last_setpoint_values: Dict[str, float] = {}
        self.dev_actuator_row_map: Dict[str, int] = {}
        self.dev_actuator_spinboxes: Dict[str, Dict[str, QDoubleSpinBox]] = {}
        self.dev_bpm_row_map: Dict[str, int] = {}
        self.group_bo_max_eval_spinboxes: Dict[str, QSpinBox] = {}
        self.group_bo_budget_labels: Dict[str, QLabel] = {}

        self._init_ui()
        self._current_value_timer = QTimer(self)
        self._current_value_timer.setInterval(3000)
        self._current_value_timer.timeout.connect(self._auto_refresh_current_values)
        self._current_value_timer.start()
        self._update_mode_ui()
        self._update_developer_mode_ui()
        self._update_developer_objective_ui()
        self._reset_target_states()
        self._refresh_current_values()
        self._set_status("Status: IDLE", state="idle", profile=RUN_PROFILE_MAIN)
        self._set_status("Status: IDLE", state="idle", profile=RUN_PROFILE_DEVELOPER)

    def _create_eval_plot_bundle(self):
        fig = Figure(figsize=(8.8, 5.6))
        ax_ttot = fig.add_subplot(211)
        ax_metric = fig.add_subplot(212)
        fig.subplots_adjust(left=0.10, right=0.98, top=0.92, bottom=0.12, hspace=0.42)
        canvas = FigureCanvas(fig)
        canvas.setMinimumHeight(340)
        canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)
        return fig, canvas, ax_ttot, ax_metric

    def _create_bo1d_plot_bundle(self):
        fig = Figure(figsize=(8.8, 5.0))
        ax_obj = fig.add_subplot(211)
        ax_acq = fig.add_subplot(212, sharex=ax_obj)
        fig.subplots_adjust(left=0.10, right=0.98, top=0.92, bottom=0.12, hspace=0.42)
        canvas = FigureCanvas(fig)
        canvas.setMinimumHeight(280)
        canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)
        canvas.setVisible(False)
        return fig, canvas, ax_obj, ax_acq

    def _init_ui(self):
        cw = QWidget()
        self.setCentralWidget(cw)
        root = QVBoxLayout(cw)

        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(
            "QTabBar::tab { font-size: 16px; font-weight: 600; padding: 8px 18px; min-width: 120px; }"
        )
        root.addWidget(self.tabs)
        self.main_tab = QWidget()
        self.config_tab = QWidget()
        # Retain legacy widget handles for old run logs, but no Developer page.
        self.developer_tab = QWidget(self)
        self.developer_tab.hide()
        self.tabs.addTab(self.main_tab, "Main")
        self.tabs.addTab(self.config_tab, "Config")

        self._build_main_tab()
        self._build_config_tab()
        self._build_developer_tab()
        self.objective_row.addWidget(QLabel("Objective"))
        self.objective_box = self.dev_objective_box
        self.objective_row.addWidget(self.objective_box)
        self.objective_row.addWidget(QLabel("BPM intensity: LINAC / BT / DR"))
        self.objective_row.addStretch(1)
        refresh_row = QHBoxLayout()
        self.dev_refresh_btn.setFixedWidth(240)
        self.dev_refresh_btn.setMinimumHeight(44)
        refresh_row.addWidget(self.dev_refresh_btn)
        refresh_row.addStretch(1)
        self.selection_page.layout().addLayout(refresh_row)
        self.selection_page.layout().addWidget(self.selection_panel, stretch=1)

        self.mode_box.currentTextChanged.connect(self._update_mode_ui)
        self.mode_box.currentTextChanged.connect(self._refresh_current_values)
        self.chk_kly_phase.toggled.connect(self._update_mode_ui)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        for cb in (
                self.chk_gun_sol_l0_phase,
                self.chk_kly_phase,
                self.chk_kly_group1,
                self.chk_kly_group2,
                self.chk_timing,
                self.chk_qa,
                self.chk_qm,
        ):
            cb.toggled.connect(self._refresh_current_values)
        self.dev_mode_box.currentTextChanged.connect(self._update_developer_mode_ui)
        self.dev_objective_box.currentTextChanged.connect(self._update_developer_objective_ui)

    def _build_main_tab(self):
        layout = QVBoxLayout(self.main_tab)
        layout.setSpacing(10)
        self.main_tab.setStyleSheet(
            "QGroupBox { font-size: 19px; font-weight: 700; margin-top: 10px; } "
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; } "
            "QLabel { font-size: 17px; } "
            "QCheckBox { font-size: 19px; font-weight: 500; spacing: 10px; } "
            "QCheckBox[knobCheck=\"true\"] { font-size: 21px; font-weight: 500; } "
            "QCheckBox[knobCheck=\"true\"]:checked { font-size: 24px; font-weight: 800; color: #111827; } "
            "QCheckBox::indicator { width: 22px; height: 22px; } "
            "QTextEdit { font-size: 15px; } "
            "QLineEdit, QComboBox { font-size: 17px; min-height: 34px; }"
        )

        grp_targets = QGroupBox("Knob Groups To Scan")
        layout.addWidget(grp_targets)
        lay_t = QVBoxLayout(grp_targets)
        self.chk_gun_sol_l0_phase = self._add_target_row(
            lay_t, "gun_sol_l0", TARGET_LABELS["gun_sol_l0"], checked=True, prominent=True
        )
        self.chk_kly_phase = self._add_target_row(
            lay_t, "kly_phase", TARGET_LABELS["kly_phase"], checked=True, prominent=True, show_value=False
        )
        self.chk_kly_group1 = self._add_target_row(
            lay_t, "kly_group_1", TARGET_LABELS["kly_group_1"], checked=True, prominent=False, indent=34
        )
        self.chk_kly_group2 = self._add_target_row(
            lay_t, "kly_group_2", TARGET_LABELS["kly_group_2"], checked=True, prominent=False, indent=34
        )
        self.chk_timing = self._add_target_row(lay_t, "timing", TARGET_LABELS["timing"], checked=True, prominent=True)
        self.chk_qa = self._add_target_row(lay_t, "qa", TARGET_LABELS["qa"], checked=True, prominent=True)
        self.chk_qm = self._add_target_row(lay_t, "qm", TARGET_LABELS["qm"], checked=True, prominent=True)
        self.chk_correctors = self._add_target_row(lay_t, "correctors", "Correctors", checked=False,
                                                   prominent=True, show_value=False)
        self.corrector_summary = QLabel("Selected: none (choose in Config)")
        self.corrector_summary.setStyleSheet("font-size: 12px; color: #475569; padding-left: 28px;")
        self.corrector_summary.setWordWrap(True)
        lay_t.addWidget(self.corrector_summary)

        ctrl_group = QGroupBox("Run Control")
        layout.addWidget(ctrl_group)
        ctrl = QVBoxLayout(ctrl_group)
        self.objective_row = QHBoxLayout()
        ctrl.addLayout(self.objective_row)

        quick = QHBoxLayout()
        method_lbl = QLabel("Method")
        method_lbl.setStyleSheet("font-size: 24px; font-weight: 900; color: #111827;")
        quick.addWidget(method_lbl)
        self.mode_box = ClickOpenComboBox()
        self.mode_box.addItems(["SEQUENTIAL", "GROUP_BO"])
        self.mode_box.setCurrentText("SEQUENTIAL")
        self.mode_box.setEditable(True)
        self.mode_box.lineEdit().setReadOnly(True)
        self.mode_box.lineEdit().setAlignment(Qt.AlignmentFlag.AlignCenter)
        for i in range(self.mode_box.count()):
            self.mode_box.setItemData(i, Qt.AlignmentFlag.AlignCenter, Qt.ItemDataRole.TextAlignmentRole)
        self.mode_box.setStyleSheet(
            "QComboBox { font-size: 24px; font-weight: 800; min-height: 44px; padding: 2px 10px; }"
        )
        quick.addWidget(self.mode_box)
        quick.addSpacing(14)
        seq_lbl = QLabel("Sequential Method")
        seq_lbl.setStyleSheet("font-size: 24px; font-weight: 900; color: #111827;")
        quick.addWidget(seq_lbl)
        self.seq_method_box = ClickOpenComboBox()
        self.seq_method_box.addItems(["BO", "TERNARY"])
        self.seq_method_box.setCurrentText("BO")
        self.seq_method_box.setEditable(True)
        self.seq_method_box.lineEdit().setReadOnly(True)
        self.seq_method_box.lineEdit().setAlignment(Qt.AlignmentFlag.AlignCenter)
        for i in range(self.seq_method_box.count()):
            self.seq_method_box.setItemData(i, Qt.AlignmentFlag.AlignCenter, Qt.ItemDataRole.TextAlignmentRole)
        self.seq_method_box.setStyleSheet(
            "QComboBox { font-size: 24px; font-weight: 800; min-height: 44px; padding: 2px 10px; }"
        )
        quick.addWidget(self.seq_method_box)
        quick.addStretch(1)
        ctrl.addLayout(quick)

        btn_row = QHBoxLayout()
        self.btn_start = QPushButton("START")
        self.btn_stop = QPushButton("PAUSE")
        self.btn_end = QPushButton("STOP")
        self.btn_end.setToolTip("End this run (cannot resume in memory). Does not restore initial settings.")
        self.btn_end.setEnabled(False)
        self.btn_end.clicked.connect(self.cancel_optimization)
        self.reset_initial_btn = QPushButton("Reset To Initial")
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self.start_optimization)
        self.btn_stop.clicked.connect(self.stop_optimization)
        self.reset_initial_btn.clicked.connect(self._on_reset_to_initial)
        big_button_css = (
            "QPushButton { font-size: 22px; font-weight: 700; padding: 16px 28px; min-height: 56px; }"
        )
        self.btn_start.setStyleSheet(big_button_css + " QPushButton { background: #1f7a1f; color: white; }")
        self.btn_stop.setStyleSheet(big_button_css + " QPushButton { background: #a32020; color: white; }")
        self.reset_initial_btn.setStyleSheet(
            "QPushButton { font-size: 18px; font-weight: 700; padding: 14px 22px; min-height: 52px; background: #585f66; color: white; }"
        )
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        btn_row.addWidget(self.btn_end)
        btn_row.addWidget(self.reset_initial_btn)
        ctrl.addLayout(btn_row)

        resume_group = QGroupBox("Resume From Interrupted Run")
        layout.addWidget(resume_group)
        resume_layout = QHBoxLayout(resume_group)
        self.resume_file_edit = QLineEdit()
        self.resume_file_edit.setPlaceholderText(
            "Select previous LiniacOptimization_Log_*.csv to continue from saved data")
        self.resume_file_browse_btn = QPushButton("Browse...")
        self.resume_file_clear_btn = QPushButton("Clear")
        self.resume_file_browse_btn.clicked.connect(self._browse_resume_file)
        self.resume_file_clear_btn.clicked.connect(self._clear_resume_file)
        resume_layout.addWidget(self.resume_file_edit, stretch=1)
        resume_layout.addWidget(self.resume_file_browse_btn)
        resume_layout.addWidget(self.resume_file_clear_btn)

        self.status_lbl = QLabel("Status: IDLE")
        self.status_lbl.setObjectName("statusBadge")
        self.status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_lbl.setMinimumHeight(54)
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        ctrl.addWidget(self.status_lbl)
        self._status_labels[RUN_PROFILE_MAIN] = self.status_lbl

        log_group = QGroupBox("Log")
        log_group.setMinimumHeight(520)
        layout.addWidget(log_group, stretch=2)
        log_l = QVBoxLayout(log_group)
        self.turn_plot_view = QComboBox()
        self.turn_plot_view.addItem("BPM transmission N / 1", "ratio")
        self.turn_plot_view.addItem("BPM intensity: turn 1 / turn N", "intensity")
        self.turn_plot_view.setMaximumWidth(340)
        self.turn_plot_view.setToolTip("Display acquired data only; switching does not read PVs.")
        self.turn_plot_view.currentIndexChanged.connect(lambda: self._refresh_eval_plot(RUN_PROFILE_MAIN))
        self.turn_plot_view.hide()
        log_l.addWidget(self.turn_plot_view)
        self.eval_fig, self.eval_canvas, self.ax_ttot, self.ax_downstream = self._create_eval_plot_bundle()
        log_l.addWidget(self.eval_canvas, stretch=3)
        self.bo1d_fig, self.bo1d_canvas, self.bo1d_ax_obj, self.bo1d_ax_acq = self._create_bo1d_plot_bundle()
        log_l.addWidget(self.bo1d_canvas, stretch=2)
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        log_l.addWidget(self.txt_log, stretch=2)
        self._log_widgets[RUN_PROFILE_MAIN] = self.txt_log
        self._plot_widgets[RUN_PROFILE_MAIN] = {
            "fig": self.eval_fig,
            "canvas": self.eval_canvas,
            "ax_ttot": self.ax_ttot,
            "ax_metric": self.ax_downstream,
            "bo1d_fig": self.bo1d_fig,
            "bo1d_canvas": self.bo1d_canvas,
            "bo1d_ax_obj": self.bo1d_ax_obj,
            "bo1d_ax_acq": self.bo1d_ax_acq,
        }
        self._refresh_eval_plot(RUN_PROFILE_MAIN)

        self.result_lbl = QLabel("Result: -")
        self.result_lbl.setObjectName("resultBadge")
        self.result_lbl.setWordWrap(True)
        self.result_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.result_lbl.setStyleSheet(
            "QLabel#resultBadge { "
            "font-size: 18px; font-weight: 700; color: #0f172a; "
            "background: #ecf5ff; border: 2px solid #93c5fd; border-radius: 8px; "
            "padding: 10px 12px; }"
        )
        self.result_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.result_lbl)
        self._result_labels[RUN_PROFILE_MAIN] = self.result_lbl

    def _build_developer_tab(self):
        layout = QVBoxLayout(self.developer_tab)
        layout.setSpacing(10)
        self.developer_tab.setStyleSheet(
            "QGroupBox { font-size: 18px; font-weight: 700; margin-top: 10px; } "
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; } "
            "QLabel { font-size: 15px; color: #111827; } "
            "QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox { font-size: 15px; min-height: 30px; } "
            "QPushButton { font-size: 15px; font-weight: 700; min-height: 32px; } "
            "QTableWidget { font-size: 14px; }"
        )

        ctrl_group = QGroupBox("Developer Run Control")
        layout.addWidget(ctrl_group)
        ctrl = QVBoxLayout(ctrl_group)

        quick = QHBoxLayout()
        quick.addWidget(QLabel("Method"))
        self.dev_mode_box = ClickOpenComboBox()
        self.dev_mode_box.addItems(["SEQUENTIAL", "GROUP_BO"])
        self.dev_mode_box.setCurrentText("SEQUENTIAL")
        self.dev_mode_box.setEditable(True)
        self.dev_mode_box.lineEdit().setReadOnly(True)
        quick.addWidget(self.dev_mode_box)
        quick.addSpacing(12)
        quick.addWidget(QLabel("Sequential Method"))
        self.dev_seq_method_box = ClickOpenComboBox()
        self.dev_seq_method_box.addItems(["BO", "TERNARY"])
        self.dev_seq_method_box.setCurrentText("BO")
        self.dev_seq_method_box.setEditable(True)
        self.dev_seq_method_box.lineEdit().setReadOnly(True)
        quick.addWidget(self.dev_seq_method_box)
        quick.addSpacing(12)
        quick.addWidget(QLabel("Objective"))
        self.dev_objective_box = ClickOpenComboBox()
        self.dev_objective_box.addItems(["ICT", "BPM sumsq", "BPM intensity"])
        self.dev_objective_box.setCurrentText("ICT")
        self.dev_objective_box.setEditable(True)
        self.dev_objective_box.lineEdit().setReadOnly(True)
        quick.addWidget(self.dev_objective_box)
        quick.addStretch(1)
        self.dev_refresh_btn = QPushButton("Refresh currents / BPMs")
        self.dev_refresh_btn.setToolTip("Read display values once. Automatic refresh is off in Developer.")
        self.dev_refresh_btn.clicked.connect(self._refresh_developer_on_request)
        quick.addWidget(self.dev_refresh_btn)
        ctrl.addLayout(quick)

        btn_row = QHBoxLayout()
        self.dev_btn_start = QPushButton("START")
        self.dev_btn_stop = QPushButton("PAUSE")
        self.dev_reset_initial_btn = QPushButton("Reset To Initial")
        self.dev_btn_stop.setEnabled(False)
        self.dev_btn_start.clicked.connect(self.start_developer_optimization)
        self.dev_btn_stop.clicked.connect(self.stop_optimization)
        self.dev_reset_initial_btn.clicked.connect(self._on_reset_to_initial)
        big_button_css = (
            "QPushButton { font-size: 20px; font-weight: 700; padding: 14px 24px; min-height: 52px; }"
        )
        self.dev_btn_start.setStyleSheet(big_button_css + " QPushButton { background: #1f7a1f; color: white; }")
        self.dev_btn_stop.setStyleSheet(big_button_css + " QPushButton { background: #a32020; color: white; }")
        self.dev_reset_initial_btn.setStyleSheet(
            "QPushButton { font-size: 17px; font-weight: 700; padding: 12px 18px; min-height: 48px; background: #585f66; color: white; }"
        )
        btn_row.addWidget(self.dev_btn_start)
        btn_row.addWidget(self.dev_btn_stop)
        btn_row.addWidget(self.dev_reset_initial_btn)
        ctrl.addLayout(btn_row)

        self.dev_status_lbl = QLabel("Status: IDLE")
        self.dev_status_lbl.setObjectName("statusBadge")
        self.dev_status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dev_status_lbl.setMinimumHeight(46)
        self.dev_status_lbl.setWordWrap(True)
        ctrl.addWidget(self.dev_status_lbl)
        self._status_labels[RUN_PROFILE_DEVELOPER] = self.dev_status_lbl

        selection_row = QHBoxLayout()
        self.selection_panel = QWidget()
        self.selection_panel.setLayout(selection_row)
        layout.addWidget(self.selection_panel, stretch=2)

        act_group = QGroupBox("Actuators")
        selection_row.addWidget(act_group, stretch=7)
        act_layout = QVBoxLayout(act_group)

        act_filter = QHBoxLayout()
        act_filter.addWidget(QLabel("Search"))
        self.dev_actuator_search = QLineEdit()
        self.dev_actuator_search.setPlaceholderText("Filter by steer name")
        act_filter.addWidget(self.dev_actuator_search, stretch=1)
        act_filter.addWidget(QLabel("Region"))
        self.dev_actuator_region_box = QComboBox()
        self.dev_actuator_region_box.addItems(["All", "LINAC", "BT"])
        act_filter.addWidget(self.dev_actuator_region_box)
        act_filter.addWidget(QLabel("Plane"))
        self.dev_actuator_plane_box = QComboBox()
        self.dev_actuator_plane_box.addItems(["All", "H", "V"])
        act_filter.addWidget(self.dev_actuator_plane_box)
        self.dev_actuator_selected_only = QCheckBox("Selected only")
        act_filter.addWidget(self.dev_actuator_selected_only)
        act_layout.addLayout(act_filter)

        act_defaults = QHBoxLayout()
        act_defaults.addWidget(QLabel("H half-range"))
        self.dev_h_half = QDoubleSpinBox()
        self.dev_h_half.setDecimals(2)
        self.dev_h_half.setRange(0.0001, 1000.0)
        self.dev_h_half.setValue(DEVELOPER_DEFAULT_HALF_RANGE_A)
        act_defaults.addWidget(self.dev_h_half)
        act_defaults.addWidget(QLabel("A"))
        act_defaults.addSpacing(8)
        act_defaults.addWidget(QLabel("H step"))
        self.dev_h_step = QDoubleSpinBox()
        self.dev_h_step.setDecimals(2)
        self.dev_h_step.setRange(0.0001, 1000.0)
        self.dev_h_step.setValue(DEVELOPER_DEFAULT_STEP_A)
        act_defaults.addWidget(self.dev_h_step)
        act_defaults.addWidget(QLabel("A"))
        act_defaults.addSpacing(16)
        act_defaults.addWidget(QLabel("V half-range"))
        self.dev_v_half = QDoubleSpinBox()
        self.dev_v_half.setDecimals(2)
        self.dev_v_half.setRange(0.0001, 1000.0)
        self.dev_v_half.setValue(DEVELOPER_DEFAULT_HALF_RANGE_A)
        act_defaults.addWidget(self.dev_v_half)
        act_defaults.addWidget(QLabel("A"))
        act_defaults.addSpacing(8)
        act_defaults.addWidget(QLabel("V step"))
        self.dev_v_step = QDoubleSpinBox()
        self.dev_v_step.setDecimals(2)
        self.dev_v_step.setRange(0.0001, 1000.0)
        self.dev_v_step.setValue(DEVELOPER_DEFAULT_STEP_A)
        act_defaults.addWidget(self.dev_v_step)
        act_defaults.addWidget(QLabel("A"))
        act_defaults.addStretch(1)
        act_layout.addLayout(act_defaults)

        act_btns = QHBoxLayout()
        self.dev_actuator_select_visible_btn = QPushButton("Select Visible")
        self.dev_actuator_clear_visible_btn = QPushButton("Clear Visible")
        self.dev_actuator_apply_defaults_btn = QPushButton("Apply Defaults To Visible")
        act_btns.addWidget(self.dev_actuator_select_visible_btn)
        act_btns.addWidget(self.dev_actuator_clear_visible_btn)
        act_btns.addWidget(self.dev_actuator_apply_defaults_btn)
        act_btns.addStretch(1)
        self.dev_actuator_count_lbl = QLabel("Selected: 0")
        act_btns.addWidget(self.dev_actuator_count_lbl)
        act_layout.addLayout(act_btns)

        self.dev_actuator_table = QTableWidget(0, 8)
        self.dev_actuator_table.setHorizontalHeaderLabels(
            ["Use", "Name", "Region", "Plane", "Current [A]", "Half-range [A]", "Step [A]", "Status"]
        )
        self.dev_actuator_table.verticalHeader().setVisible(False)
        self.dev_actuator_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.dev_actuator_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.dev_actuator_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.dev_actuator_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.dev_actuator_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.dev_actuator_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.dev_actuator_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.dev_actuator_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.dev_actuator_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        self.dev_actuator_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        act_layout.addWidget(self.dev_actuator_table, stretch=1)

        objective_group = QGroupBox("Objective")
        selection_row.addWidget(objective_group, stretch=6)
        objective_layout = QVBoxLayout(objective_group)
        self.selection_objective_hint = QLabel("LINAC / BT / DR BPMs can be preselected here. Used when Objective = BPM intensity.")
        self.selection_objective_hint.setWordWrap(True)
        objective_layout.addWidget(self.selection_objective_hint)
        self.dev_objective_stack = QStackedWidget()
        objective_layout.addWidget(self.dev_objective_stack, stretch=1)

        ict_page = QWidget()
        ict_layout = QVBoxLayout(ict_page)
        ict_row = QHBoxLayout()
        ict_row.addWidget(QLabel("w_t (Ttot)"))
        self.dev_sp_score_w_ttot = QDoubleSpinBox()
        self.dev_sp_score_w_ttot.setDecimals(3)
        self.dev_sp_score_w_ttot.setRange(-1000.0, 1000.0)
        self.dev_sp_score_w_ttot.setValue(1.0)
        ict_row.addWidget(self.dev_sp_score_w_ttot)
        ict_row.addSpacing(12)
        ict_row.addWidget(QLabel("w_c (ICT downstream)"))
        self.dev_sp_score_w_downstream = QDoubleSpinBox()
        self.dev_sp_score_w_downstream.setDecimals(3)
        self.dev_sp_score_w_downstream.setRange(-1000.0, 1000.0)
        self.dev_sp_score_w_downstream.setValue(1.0)
        ict_row.addWidget(self.dev_sp_score_w_downstream)
        ict_row.addSpacing(12)
        ict_row.addWidget(QLabel("Downstream ICT"))
        self.dev_downstream_ict_box = QComboBox()
        self.dev_downstream_ict_box.addItems(["DR", "LNE", "BTE", "LN0", "GUN", "BTM"])
        self.dev_downstream_ict_box.setCurrentText("DR")
        ict_row.addWidget(self.dev_downstream_ict_box)
        ict_row.addStretch(1)
        ict_layout.addLayout(ict_row)
        ict_layout.addWidget(QLabel("Score = w_t * Ttot + w_c * selected downstream ICT"))
        ict_layout.addStretch(1)
        self.dev_objective_stack.addWidget(ict_page)

        bpm_page = QWidget()
        bpm_layout = QVBoxLayout(bpm_page)
        bpm_row = QHBoxLayout()
        self.dev_bpm_mode_label = QLabel("Plane")
        bpm_row.addWidget(self.dev_bpm_mode_label)
        self.dev_bpm_plane_box = QComboBox()
        self.dev_bpm_plane_box.addItems(["XY", "X", "Y"])
        self.dev_bpm_plane_box.setCurrentText("XY")
        bpm_row.addWidget(self.dev_bpm_plane_box)
        self.dev_intensity_aggregation = QComboBox()
        self.dev_intensity_aggregation.addItem("Raw mean", "raw_mean")
        self.dev_intensity_aggregation.addItem("Raw sum", "raw_sum")
        self.dev_intensity_aggregation.addItem("Mean / initial baseline", "relative_mean")
        self.dev_intensity_aggregation.addItem("DR transmission: turn N / turn 1", "turn_ratio")
        self.dev_intensity_aggregation.addItem("DR transmission + normalized meanCurrent", "turn_composite")
        self.dev_intensity_aggregation.setToolTip("Maximize the selected BPMs' mean or sum. Relative mode fixes each baseline before the run.")
        bpm_row.addWidget(self.dev_intensity_aggregation)
        bpm_row.addStretch(1)
        bpm_layout.addLayout(bpm_row)
        self.intensity_help = QLabel(
            "Raw mean: average of selected BPM intensities.\n"
            "Raw sum: sum of selected BPM intensities (no normalization).\n"
            "Mean / initial baseline: average of each BPM's current / initial intensity (start = 1).")
        self.intensity_help.setWordWrap(True)
        self.intensity_help.setToolTip(
            "Raw intensities may have different scales across BPMs/regions. "
            "Mean / initial baseline compares fractional changes, not calibrated charge. "
            "Optimization reads selected source regions only; Refresh reads all regions.")
        self.intensity_help.setStyleSheet("font-size: 12px; color: #475569;")
        bpm_layout.addWidget(self.intensity_help)

        self.dr_turn_group = QGroupBox("DR BPM turn selection")
        turn_layout = QVBoxLayout(self.dr_turn_group)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("DR BPM mode"))
        self.dr_orbit_mode = QComboBox()
        for label, value in (("TBT (0)", 0), ("COD (1)", 1), ("Injection (2)", 2), ("Extraction (3)", 3)):
            self.dr_orbit_mode.addItem(label, value)
        self.dr_orbit_mode.setCurrentIndex(2)
        self.dr_orbit_mode.setToolTip("DRBPM:ORBIT_MODE. Applied before DR acquisition or with Apply mode. No PV write on GUI startup; mode is not restored.")
        mode_row.addWidget(self.dr_orbit_mode)
        self.dr_orbit_apply = QPushButton("Apply mode")
        self.dr_orbit_apply.clicked.connect(self._apply_dr_orbit_mode)
        mode_row.addWidget(self.dr_orbit_apply)
        mode_row.addStretch()
        turn_layout.addLayout(mode_row)
        self.dr_turn_enabled = QCheckBox("Scan turns (writes DRBPM:INJ:TURN_COUNT)")
        turn_layout.addWidget(self.dr_turn_enabled)
        turn_row = QHBoxLayout()
        self.dr_turn_start = QSpinBox()
        self.dr_turn_end = QSpinBox()
        for spin in (self.dr_turn_start, self.dr_turn_end):
            spin.setRange(1, 1000000)
        self.dr_turn_end.setValue(10)
        self.dr_turn_start.valueChanged.connect(self.dr_turn_end.setMinimum)
        self.dr_turn_aggregation = QComboBox()
        self.dr_turn_aggregation.addItem("Mean", "mean")
        self.dr_turn_aggregation.addItem("Sum", "sum")
        for label, widget in (("First turn", self.dr_turn_start), ("Last turn (inclusive)", self.dr_turn_end),
                              ("Across turns", self.dr_turn_aggregation)):
            turn_row.addWidget(QLabel(label))
            turn_row.addWidget(widget)
            widget.setEnabled(False)
            self.dr_turn_enabled.toggled.connect(widget.setEnabled)
        turn_layout.addLayout(turn_row)
        turn_help = QLabel("Wait 0.5 s after each turn change, then sample. Optimization: 3 samples/turn "
                           "at 0.5 s intervals (~2 s/turn). Average samples, then Mean/Sum across turns, "
                           "then Aggregate across BPMs. Shared PV stays at the last commanded turn (also on stop/error). "
                           "Refresh also scans when enabled; LINAC/BT are unchanged.")
        turn_help.setWordWrap(True)
        turn_layout.addWidget(turn_help)
        bpm_layout.addWidget(self.dr_turn_group)

        ratio_row = QHBoxLayout()
        self.dr_ratio_turn = QSpinBox()
        self.dr_ratio_turn.setRange(2, 1000000)
        self.dr_ratio_turn.setValue(10)
        self.dr_current_weight = QDoubleSpinBox()
        self.dr_current_weight.setRange(0, 1000)
        self.dr_current_weight.setDecimals(3)
        self.dr_current_weight.setValue(1)
        self.dr_ratio_floor = QDoubleSpinBox()
        self.dr_ratio_floor.setDecimals(12)
        self.dr_ratio_floor.setRange(1e-12, 1e12)
        self.dr_ratio_floor.setValue(1e-12)
        for label, widget in (("Transmission N", self.dr_ratio_turn), ("Current weight w", self.dr_current_weight),
                              ("Min turn 1 intensity", self.dr_ratio_floor)):
            ratio_row.addWidget(QLabel(label))
            ratio_row.addWidget(widget)
        bpm_layout.addLayout(ratio_row)
        ratio_help = QLabel("Transmission modes: DR BPMs only; read turns 1 and N (0.5 s settle each). "
                            "Overrides the turn-range scan above. Score = mean(I_N / I_1), optionally + w × "
                            "mean(I_1) / initial mean(I_1). Fixed baseline; finite turn 1 intensity at/below the minimum "
                            "counts as loss (0 transmission, retained in the BPM mean). No standard-deviation/noise threshold is applied; missing data stops the run. "
                            "Each acquisition writes turn 1 then N; the selector stays at N after completion.")
        ratio_help.setWordWrap(True)
        bpm_layout.addWidget(ratio_help)

        bpm_filter = QHBoxLayout()
        bpm_filter.addWidget(QLabel("Search"))
        self.dev_bpm_search = QLineEdit()
        self.dev_bpm_search.setPlaceholderText("Filter by BPM name")
        bpm_filter.addWidget(self.dev_bpm_search, stretch=1)
        bpm_filter.addWidget(QLabel("Region"))
        self.dev_bpm_region_box = QComboBox()
        self.dev_bpm_region_box.addItems(["All", "LINAC", "BT"])
        bpm_filter.addWidget(self.dev_bpm_region_box)
        self.dev_bpm_selected_only = QCheckBox("Selected only")
        bpm_filter.addWidget(self.dev_bpm_selected_only)
        bpm_layout.addLayout(bpm_filter)

        bpm_btns = QHBoxLayout()
        self.dev_bpm_select_visible_btn = QPushButton("Select Visible")
        self.dev_bpm_clear_visible_btn = QPushButton("Clear Visible")
        bpm_btns.addWidget(self.dev_bpm_select_visible_btn)
        bpm_btns.addWidget(self.dev_bpm_clear_visible_btn)
        bpm_btns.addStretch(1)
        self.dev_bpm_count_lbl = QLabel("Selected BPMs: 0")
        bpm_btns.addWidget(self.dev_bpm_count_lbl)
        bpm_layout.addLayout(bpm_btns)

        self.dev_bpm_table = QTableWidget(0, 5)
        self.dev_bpm_table.setHorizontalHeaderLabels(["Use", "Name", "Region", "Live X [mm]", "Live Y [mm]"])
        self.dev_bpm_table.verticalHeader().setVisible(False)
        self.dev_bpm_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.dev_bpm_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.dev_bpm_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.dev_bpm_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.dev_bpm_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.dev_bpm_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.dev_bpm_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        bpm_layout.addWidget(self.dev_bpm_table, stretch=1)
        self.dev_objective_stack.addWidget(bpm_page)

        log_group = QGroupBox("Developer Log")
        log_group.setMinimumHeight(520)
        layout.addWidget(log_group, stretch=2)
        log_l = QVBoxLayout(log_group)
        self.dev_eval_fig, self.dev_eval_canvas, self.dev_ax_ttot, self.dev_ax_metric = self._create_eval_plot_bundle()
        log_l.addWidget(self.dev_eval_canvas, stretch=3)
        self.dev_bo1d_fig, self.dev_bo1d_canvas, self.dev_bo1d_ax_obj, self.dev_bo1d_ax_acq = self._create_bo1d_plot_bundle()
        log_l.addWidget(self.dev_bo1d_canvas, stretch=2)
        self.dev_txt_log = QTextEdit()
        self.dev_txt_log.setReadOnly(True)
        log_l.addWidget(self.dev_txt_log, stretch=2)
        self._log_widgets[RUN_PROFILE_DEVELOPER] = self.dev_txt_log
        self._plot_widgets[RUN_PROFILE_DEVELOPER] = {
            "fig": self.dev_eval_fig,
            "canvas": self.dev_eval_canvas,
            "ax_ttot": self.dev_ax_ttot,
            "ax_metric": self.dev_ax_metric,
            "bo1d_fig": self.dev_bo1d_fig,
            "bo1d_canvas": self.dev_bo1d_canvas,
            "bo1d_ax_obj": self.dev_bo1d_ax_obj,
            "bo1d_ax_acq": self.dev_bo1d_ax_acq,
        }
        self._refresh_eval_plot(RUN_PROFILE_DEVELOPER)

        self.dev_result_lbl = QLabel("Result: -")
        self.dev_result_lbl.setObjectName("resultBadge")
        self.dev_result_lbl.setWordWrap(True)
        self.dev_result_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.dev_result_lbl.setStyleSheet(
            "QLabel#resultBadge { "
            "font-size: 17px; font-weight: 700; color: #0f172a; "
            "background: #ecf5ff; border: 2px solid #93c5fd; border-radius: 8px; "
            "padding: 10px 12px; }"
        )
        self.dev_result_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.dev_result_lbl)
        self._result_labels[RUN_PROFILE_DEVELOPER] = self.dev_result_lbl

        self._populate_developer_tables()

        self.dev_actuator_search.textChanged.connect(self._apply_developer_actuator_filters)
        self.dev_actuator_region_box.currentTextChanged.connect(self._apply_developer_actuator_filters)
        self.dev_actuator_plane_box.currentTextChanged.connect(self._apply_developer_actuator_filters)
        self.dev_actuator_selected_only.toggled.connect(self._apply_developer_actuator_filters)
        self.dev_actuator_select_visible_btn.clicked.connect(self._select_visible_developer_actuators)
        self.dev_actuator_clear_visible_btn.clicked.connect(self._clear_visible_developer_actuators)
        self.dev_actuator_apply_defaults_btn.clicked.connect(self._apply_default_ranges_to_visible_developer_actuators)

        self.dev_bpm_search.textChanged.connect(self._apply_developer_bpm_filters)
        self.dev_bpm_region_box.currentTextChanged.connect(self._apply_developer_bpm_filters)
        self.dev_bpm_selected_only.toggled.connect(self._apply_developer_bpm_filters)
        self.dev_bpm_select_visible_btn.clicked.connect(self._select_visible_developer_bpms)
        self.dev_bpm_clear_visible_btn.clicked.connect(self._clear_visible_developer_bpms)

        self._update_developer_mode_ui()
        self._update_developer_objective_ui()

    def _build_config_tab(self):
        outer = QVBoxLayout(self.config_tab)
        self.config_sections = QTabWidget()
        outer.addWidget(self.config_sections)
        scan_page = QWidget()
        scan_scroll = QScrollArea()
        scan_scroll.setWidgetResizable(True)
        scan_scroll.setWidget(scan_page)
        self.config_sections.addTab(scan_scroll, "Scan settings")
        self.selection_page = QWidget()
        QVBoxLayout(self.selection_page)
        self.config_sections.addTab(self.selection_page, "Correctors / BPMs")
        layout = QVBoxLayout(scan_page)
        layout.setSpacing(10)
        self.config_tab.setStyleSheet(
            "QGroupBox { font-size: 18px; font-weight: 700; margin-top: 10px; } "
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; } "
            "QLabel { font-size: 16px; color: #111827; } "
            "QDoubleSpinBox, QSpinBox, QComboBox { font-size: 16px; min-height: 32px; } "
            "QPushButton { font-size: 16px; font-weight: 700; min-height: 34px; }"
        )

        grp_scan = QGroupBox("Scan Settings")
        layout.addWidget(grp_scan)
        lay_seq = QVBoxLayout(grp_scan)

        def _row(label: str, w1, w2, unit1: str, unit2: str):
            h = QHBoxLayout()
            h.addWidget(QLabel(label))
            h.addWidget(QLabel("half-range"))
            h.addWidget(w1)
            h.addWidget(QLabel(unit1))
            h.addSpacing(12)
            h.addWidget(QLabel("step"))
            h.addWidget(w2)
            h.addWidget(QLabel(unit2))
            h.addStretch()
            lay_seq.addLayout(h)

        self.sp_gun_phase_half = QDoubleSpinBox()
        self.sp_gun_phase_half.setDecimals(2)
        self.sp_gun_phase_half.setRange(0.0, 360.0)
        self.sp_gun_phase_half.setValue(GUN_PHASE_HALF_RANGE_DEG)
        self.sp_gun_phase_step = QDoubleSpinBox()
        self.sp_gun_phase_step.setDecimals(3)
        self.sp_gun_phase_step.setRange(0.001, 180.0)
        self.sp_gun_phase_step.setValue(GUN_PHASE_STEP_DEG)
        _row("GUN phase", self.sp_gun_phase_half, self.sp_gun_phase_step, "deg", "deg")

        lay_sol = QHBoxLayout()
        lay_sol.addWidget(QLabel("Solenoid current"))
        lay_sol.addWidget(QLabel("min"))
        self.sp_sol_min = QDoubleSpinBox()
        self.sp_sol_min.setDecimals(2)
        self.sp_sol_min.setRange(-1000.0, 1000.0)
        self.sp_sol_min.setValue(SOLENOIDE_MIN_A)
        lay_sol.addWidget(self.sp_sol_min)
        lay_sol.addWidget(QLabel("A"))
        lay_sol.addSpacing(12)
        lay_sol.addWidget(QLabel("max"))
        self.sp_sol_max = QDoubleSpinBox()
        self.sp_sol_max.setDecimals(2)
        self.sp_sol_max.setRange(-1000.0, 1000.0)
        self.sp_sol_max.setValue(SOLENOIDE_MAX_A)
        lay_sol.addWidget(self.sp_sol_max)
        lay_sol.addWidget(QLabel("A"))
        lay_sol.addSpacing(12)
        lay_sol.addWidget(QLabel("step"))
        self.sp_sol_step = QDoubleSpinBox()
        self.sp_sol_step.setDecimals(3)
        self.sp_sol_step.setRange(0.001, 100.0)
        self.sp_sol_step.setValue(SOLENOIDE_STEP_A)
        lay_sol.addWidget(self.sp_sol_step)
        lay_sol.addWidget(QLabel("A"))
        lay_sol.addStretch()
        lay_seq.addLayout(lay_sol)

        self.sp_l0_phase_half = QDoubleSpinBox()
        self.sp_l0_phase_half.setDecimals(2)
        self.sp_l0_phase_half.setRange(0.0, 360.0)
        self.sp_l0_phase_half.setValue(L0_PHASE_HALF_RANGE_DEG)
        self.sp_l0_phase_step = QDoubleSpinBox()
        self.sp_l0_phase_step.setDecimals(3)
        self.sp_l0_phase_step.setRange(0.001, 180.0)
        self.sp_l0_phase_step.setValue(L0_PHASE_STEP_DEG)
        _row("L0 phase", self.sp_l0_phase_half, self.sp_l0_phase_step, "deg", "deg")

        self.sp_phase_half = QDoubleSpinBox()
        self.sp_phase_half.setDecimals(2)
        self.sp_phase_half.setRange(0.0, 360.0)
        self.sp_phase_half.setValue(PHASE_HALF_RANGE_DEG)
        self.sp_phase_step = QDoubleSpinBox()
        self.sp_phase_step.setDecimals(3)
        self.sp_phase_step.setRange(0.001, 180.0)
        self.sp_phase_step.setValue(PHASE_STEP_DEG)
        _row("Klystron phase (L1-L8)", self.sp_phase_half, self.sp_phase_step, "deg", "deg")

        self.sp_qa_half = QDoubleSpinBox()
        self.sp_qa_half.setDecimals(3)
        self.sp_qa_half.setRange(0.0, 50.0)
        self.sp_qa_half.setValue(QA_HALF_RANGE_A)
        self.sp_qa_step = QDoubleSpinBox()
        self.sp_qa_step.setDecimals(4)
        self.sp_qa_step.setRange(0.0001, 10.0)
        self.sp_qa_step.setValue(QA_STEP_A)
        _row("QA magnets", self.sp_qa_half, self.sp_qa_step, "A", "A")

        self.sp_qm_half = QDoubleSpinBox()
        self.sp_qm_half.setDecimals(3)
        self.sp_qm_half.setRange(0.0, 50.0)
        self.sp_qm_half.setValue(QM_HALF_RANGE_A)
        self.sp_qm_step = QDoubleSpinBox()
        self.sp_qm_step.setDecimals(4)
        self.sp_qm_step.setRange(0.0001, 10.0)
        self.sp_qm_step.setValue(QM_STEP_A)
        _row("QM magnets", self.sp_qm_half, self.sp_qm_step, "A", "A")

        lay_timing = QHBoxLayout()
        lay_timing.addWidget(QLabel("Timing"))
        lay_timing.addWidget(QLabel("step"))
        self.sp_timing_step = QDoubleSpinBox()
        self.sp_timing_step.setDecimals(3)
        self.sp_timing_step.setRange(0.001, 1000.0)
        self.sp_timing_step.setValue(TIMING_STEP_NS)
        lay_timing.addWidget(self.sp_timing_step)
        lay_timing.addWidget(QLabel("ns"))
        lay_timing.addSpacing(12)
        lay_timing.addWidget(QLabel("half-steps"))
        self.sp_timing_half_steps = QSpinBox()
        self.sp_timing_half_steps.setRange(1, 200)
        self.sp_timing_half_steps.setValue(TIMING_HALF_STEPS)
        lay_timing.addWidget(self.sp_timing_half_steps)
        lay_timing.addStretch()
        lay_seq.addLayout(lay_timing)

        self.correctors_l8 = QCheckBox("Correctors & L8: include L8 phase + Timing in one Group BO")
        self.correctors_l8.setToolTip("Timing = EVE_LINAC:OUT0:SetData (existing LINAC timing). Uses Timing range/step above. L8 is excluded from the separate L5–L8 group.")
        lay_seq.addWidget(self.correctors_l8)
        self.l8_phase_half = QDoubleSpinBox()
        self.l8_phase_half.setRange(0.001, 180.)
        self.l8_phase_half.setDecimals(3)
        self.l8_phase_half.setValue(PHASE_HALF_RANGE_DEG)
        self.l8_phase_step = QDoubleSpinBox()
        self.l8_phase_step.setRange(0.001, 180.)
        self.l8_phase_step.setDecimals(3)
        self.l8_phase_step.setValue(PHASE_STEP_DEG)
        _row("Correctors & L8 phase", self.l8_phase_half, self.l8_phase_step, "deg", "deg")
        self.correctors_l8.toggled.connect(self._update_correctors_l8_ui)

        grp_score = QGroupBox("Scoring")
        layout.addWidget(grp_score)
        lay_score = QHBoxLayout(grp_score)
        lay_score.addWidget(QLabel("w_t (Ttot)"))
        self.sp_score_w_ttot = QDoubleSpinBox()
        self.sp_score_w_ttot.setDecimals(3)
        self.sp_score_w_ttot.setRange(-1000.0, 1000.0)
        self.sp_score_w_ttot.setValue(1.0)
        lay_score.addWidget(self.sp_score_w_ttot)
        lay_score.addSpacing(12)
        lay_score.addWidget(QLabel("w_c (ICT downstream)"))
        self.sp_score_w_downstream = QDoubleSpinBox()
        self.sp_score_w_downstream.setDecimals(3)
        self.sp_score_w_downstream.setRange(-1000.0, 1000.0)
        self.sp_score_w_downstream.setValue(1.0)
        lay_score.addWidget(self.sp_score_w_downstream)
        lay_score.addSpacing(12)
        lay_score.addWidget(QLabel("Downstream ICT"))
        self.cb_downstream_ict = QComboBox()
        self.cb_downstream_ict.addItems(["DR", "LNE", "BTE", "LN0", "GUN", "BTM"])
        self.cb_downstream_ict.setCurrentText("DR")
        lay_score.addWidget(self.cb_downstream_ict)
        lay_score.addStretch(1)

        self.grp_group_bo_budget = QGroupBox("Group BO Max Evals")
        layout.addWidget(self.grp_group_bo_budget)
        lay_gbo = QGridLayout(self.grp_group_bo_budget)
        lay_gbo.setHorizontalSpacing(18)
        lay_gbo.setVerticalSpacing(8)
        self.group_bo_budget_preset_box = QComboBox()
        for key, preset_spec in GROUP_BO_BUDGET_PRESETS.items():
            self.group_bo_budget_preset_box.addItem(preset_spec["label"], key)
        self.group_bo_budget_preset_box.setCurrentIndex(self.group_bo_budget_preset_box.findData("standard"))
        self.group_bo_budget_preset_box.setToolTip(
            "Changing preset resets ALL group maximum budgets and enables Corrector Auto. "
            "Only evaluation counts change, not scan ranges, steps, GP settings or early stopping.")
        lay_gbo.addWidget(QLabel("Evaluation budget preset"), 0, 0)
        lay_gbo.addWidget(self.group_bo_budget_preset_box, 0, 1, 1, 3)
        self.group_bo_budget_help = QLabel()
        self.group_bo_budget_help.setWordWrap(True)
        lay_gbo.addWidget(self.group_bo_budget_help, 1, 0, 1, 4)
        for idx, spec in enumerate(GROUP_BO_MAX_EVAL_SPECS):
            label = QLabel(f"{spec['ui_label']} (D={spec['dim']})")
            self.group_bo_budget_labels[str(spec["config_id"])] = label
            spin = QSpinBox()
            spin.setRange(1, 1000000)
            spin.setValue(_recommended_group_bo_max_evals(int(spec["dim"])))
            spin.setSuffix(" evals")
            spin.setToolTip("Maximum search evaluations, including initial points. Editable override of the selected preset.")
            self.group_bo_max_eval_spinboxes[str(spec["config_id"])] = spin

            row = 2 + idx // 2
            col = (idx % 2) * 2
            lay_gbo.addWidget(label, row, col)
            lay_gbo.addWidget(spin, row, col + 1)
            spin.valueChanged.connect(self._refresh_group_bo_budget_labels)
        self.corrector_budget_auto = QCheckBox("Correctors: auto max from preset (follow selected count)")
        self.corrector_budget_auto.setChecked(True)
        self.corrector_budget_auto.toggled.connect(self._refresh_group_bo_budget_labels)
        lay_gbo.addWidget(self.corrector_budget_auto, 2 + (len(GROUP_BO_MAX_EVAL_SPECS) + 1) // 2, 0, 1, 4)
        self.group_bo_budget_preset_box.currentIndexChanged.connect(self._apply_group_bo_budget_preset)
        self._refresh_group_bo_budget_labels()

        corrector_gp_group = QGroupBox("Corrector Group BO model")
        layout.addWidget(corrector_gp_group)
        corrector_gp_layout = QGridLayout(corrector_gp_group)
        self.corrector_gp_mode_box = QComboBox()
        self.corrector_gp_mode_box.addItem("Online fit (standardized)", "online_fit")
        self.corrector_gp_mode_box.addItem("Fixed (legacy)", "fixed")
        corrector_gp_layout.addWidget(QLabel("Hyperparameters"), 0, 0)
        corrector_gp_layout.addWidget(self.corrector_gp_mode_box, 0, 1)
        corrector_gp_layout.addWidget(QLabel("Refit every"), 0, 2)
        self.corrector_gp_refit_spin = QSpinBox()
        self.corrector_gp_refit_spin.setRange(1, 1000)
        self.corrector_gp_refit_spin.setValue(5)
        self.corrector_gp_refit_spin.setSuffix(" evaluations")
        corrector_gp_layout.addWidget(self.corrector_gp_refit_spin, 0, 3)
        self.corrector_gp_mode_box.currentIndexChanged.connect(
            lambda: self.corrector_gp_refit_spin.setEnabled(self.corrector_gp_mode_box.currentData() == "online_fit"))
        gp_help = QLabel(
            "Online: fit length scales, signal and noise after initial data, then periodically. "
            "Bounds and a 5 s fit time budget apply; failure keeps previous parameters.\n"
            "Scores are standardized internally (scaling frozen once data varies). Display / CSV remain raw. "
            "Corrector GroupBO only; other groups and Sequential are unchanged.")
        gp_help.setWordWrap(True)
        gp_help.setToolTip("Normalized length scale: max(0.02, 2 steps) to 2 ranges (lower bound capped at 2). "
                           "Signal SD: 0.1–10; noise SD: 0.001–1 in standardized score units. "
                           "No additional machine measurements are made for fitting.")
        corrector_gp_layout.addWidget(gp_help, 1, 0, 1, 4)

        grp_adv = QGroupBox("Advanced")
        layout.addWidget(grp_adv)
        lay_adv = QHBoxLayout(grp_adv)
        lay_adv.addWidget(QLabel("Settle time"))
        self.sp_settle_sec = QDoubleSpinBox()
        self.sp_settle_sec.setDecimals(2)
        self.sp_settle_sec.setRange(0.0, 120.0)
        self.sp_settle_sec.setValue(SETTLE_SEC_DEFAULT)
        lay_adv.addWidget(self.sp_settle_sec)
        lay_adv.addWidget(QLabel("s"))
        lay_adv.addSpacing(18)
        lay_adv.addWidget(QLabel("BO GP preset"))
        self.bo_hyperparameter_mode_box = QComboBox()
        self.bo_hyperparameter_mode_box.addItems(list(LINAC_HYPERPARAMETER_MODES))
        # New runs use the conservative offline-derived prior.  Config files
        # created before this field existed are loaded as legacy_fixed below.
        self.bo_hyperparameter_mode_box.setCurrentText("learned_fixed")
        self.bo_hyperparameter_mode_box.setToolTip(
            "legacy_fixed: historical range/3. learned_fixed: BO2026 conservative ARD priors. "
            "Corrector GroupBO Online fit uses its separate fitted model instead."
        )
        lay_adv.addWidget(self.bo_hyperparameter_mode_box)
        lay_adv.addStretch(1)

        path_group = QGroupBox("Data Save Location")
        layout.addWidget(path_group)
        lay_p = QHBoxLayout(path_group)
        self.lbl_path = QLabel(str(self.save_path))
        self.lbl_path.setStyleSheet(
            "font-size: 18px; color: #0f172a; background: #ecf5ff; border: 2px solid #93c5fd; "
            "border-radius: 8px; padding: 8px 10px;"
        )
        self.btn_browse = QPushButton("Browse...")
        self.btn_browse.clicked.connect(self.browse_folder)
        self.btn_browse.setStyleSheet(
            "QPushButton { font-size: 18px; font-weight: 700; min-height: 46px; "
            "padding: 10px 18px; background: #585f66; color: white; }"
        )
        lay_p.addWidget(self.lbl_path, stretch=1)
        lay_p.addWidget(self.btn_browse)

        layout.addStretch(1)

    def _make_check_item(self, checked: bool = False) -> QTableWidgetItem:
        item = QTableWidgetItem("")
        item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        return item

    def _populate_developer_tables(self):
        self.dev_actuator_table.setRowCount(0)
        self.dev_actuator_row_map.clear()
        self.dev_actuator_spinboxes.clear()
        actuator_names = [name for name in BT_SEQUENCE if str(name).lower().startswith("z")]
        for name in actuator_names:
            spec = _developer_actuator_spec(name)
            row = self.dev_actuator_table.rowCount()
            self.dev_actuator_table.insertRow(row)
            self.dev_actuator_row_map[str(name)] = row
            self.dev_actuator_table.setItem(row, 0, self._make_check_item(False))
            self.dev_actuator_table.setItem(row, 1, QTableWidgetItem(str(name)))
            self.dev_actuator_table.setItem(row, 2, QTableWidgetItem(spec["region"]))
            self.dev_actuator_table.setItem(row, 3, QTableWidgetItem(spec["plane"]))
            self.dev_actuator_table.setItem(row, 4, QTableWidgetItem("-"))
            half_box = QDoubleSpinBox()
            half_box.setDecimals(2)
            half_box.setRange(0.0001, 1000.0)
            half_box.setValue(self.dev_h_half.value() if spec["plane"] == "H" else self.dev_v_half.value())
            step_box = QDoubleSpinBox()
            step_box.setDecimals(2)
            step_box.setRange(0.0001, 1000.0)
            step_box.setValue(self.dev_h_step.value() if spec["plane"] == "H" else self.dev_v_step.value())
            self.dev_actuator_table.setCellWidget(row, 5, half_box)
            self.dev_actuator_table.setCellWidget(row, 6, step_box)
            self.dev_actuator_table.setItem(row, 7, QTableWidgetItem("IDLE"))
            self.dev_actuator_spinboxes[str(name)] = {"half": half_box, "step": step_box}

        self.dev_actuator_table.itemChanged.connect(self._on_developer_actuator_item_changed)
        self.dev_bpm_table.itemChanged.connect(self._on_developer_bpm_item_changed)
        self._populate_developer_bpm_table(False)
        self._apply_developer_actuator_filters()

    def _populate_developer_bpm_table(self, intensity):
        if not hasattr(self, "_bpm_selections"):
            self._bpm_selections = {}
        if hasattr(self, "_bpm_table_is_intensity"):
            self._bpm_selections[self._bpm_table_is_intensity] = set(self._selected_developer_bpms())
        selected = self._bpm_selections.get(intensity, set())
        self.dev_bpm_table.blockSignals(True)
        self.dev_bpm_table.setRowCount(0)
        self.dev_bpm_row_map.clear()
        bpm_names = INTENSITY_BPM_NAMES if intensity else LINAC_BT_BPM_NAMES
        for name in bpm_names:
            region = "DR" if name in DR_BPM_NAMES else _machine_region(name)
            row = self.dev_bpm_table.rowCount()
            self.dev_bpm_table.insertRow(row)
            self.dev_bpm_row_map[str(name)] = row
            self.dev_bpm_table.setItem(row, 0, self._make_check_item(name in selected))
            self.dev_bpm_table.setItem(row, 1, QTableWidgetItem(str(name)))
            self.dev_bpm_table.setItem(row, 2, QTableWidgetItem(region))
            self.dev_bpm_table.setItem(row, 3, QTableWidgetItem("-"))
            self.dev_bpm_table.setItem(row, 4, QTableWidgetItem("-"))

        self.dev_bpm_table.blockSignals(False)
        self.dev_bpm_region_box.blockSignals(True)
        self.dev_bpm_region_box.clear()
        self.dev_bpm_region_box.addItems(["All", "LINAC", "BT", "DR"] if intensity else ["All", "LINAC", "BT"])
        self.dev_bpm_region_box.blockSignals(False)
        self.dev_bpm_search.clear()
        self._bpm_table_is_intensity = intensity
        self._apply_developer_bpm_filters()
        self._refresh_developer_selection_counts()

    def _on_developer_actuator_item_changed(self, item: QTableWidgetItem):
        if item.column() == 0:
            self._refresh_developer_selection_counts()
            if self.dev_actuator_selected_only.isChecked():
                self._apply_developer_actuator_filters()

    def _on_developer_bpm_item_changed(self, item: QTableWidgetItem):
        if item.column() == 0:
            self._refresh_developer_selection_counts()
            if self._bpm_table_is_intensity and (self.worker is None or not self.worker.isRunning()):
                state = self._plot_state.get(RUN_PROFILE_MAIN, {})
                state["selected_bpms"] = self._selected_developer_bpms()
                self._refresh_eval_plot(RUN_PROFILE_MAIN)
            if self.dev_bpm_selected_only.isChecked():
                self._apply_developer_bpm_filters()

    def _apply_group_bo_budget_preset(self, *_args):
        preset = self.group_bo_budget_preset_box.currentData()
        for spec in GROUP_BO_MAX_EVAL_SPECS:
            spin = self.group_bo_max_eval_spinboxes[str(spec["config_id"])]
            spin.blockSignals(True)
            spin.setValue(_recommended_group_bo_max_evals(int(spec["dim"]), preset))
            spin.blockSignals(False)
        self.corrector_budget_auto.setChecked(True)
        self._refresh_group_bo_budget_labels()

    def _refresh_group_bo_budget_labels(self, *_args):
        preset = self.group_bo_budget_preset_box.currentData()
        preset_spec = _group_bo_preset(preset)
        max_formula = f"{preset_spec['max_slope']}D + {preset_spec['max_offset']}"
        init_formula = f"{preset_spec['init_slope']}D + {preset_spec['init_offset']}"
        self.group_bo_budget_help.setText(
            f"Preset max = {max_formula}; initial = {init_formula} (included in max). D = number of variables.\n"
            "Heuristic budget only; early stopping is unchanged. Ranges and steps are unchanged.\n"
            "Initial points are limited by max / available grid points. Final best-point measurement is extra.\n"
            "Timing uses the 1D BO initial design; only its maximum follows the preset.\n"
            "Changing preset replaces manual maximum budgets; edit individual values afterwards if needed.")
        corrector_dim = len(self._selected_developer_actuators()) if hasattr(self, "dev_actuator_table") else 0
        if hasattr(self, "correctors_l8") and self.correctors_l8.isChecked():
            corrector_dim += 2
        for spec in GROUP_BO_MAX_EVAL_SPECS:
            config_id = str(spec["config_id"])
            dim = corrector_dim if config_id == "correctors" else int(spec["dim"])
            spin = self.group_bo_max_eval_spinboxes[config_id]
            if config_id == "correctors":
                auto = self.corrector_budget_auto.isChecked()
                spin.setSpecialValueText("—" if dim == 0 and auto else "")
                if auto:
                    spin.blockSignals(True)
                    spin.setValue(_recommended_group_bo_max_evals(dim, preset) if dim else 1)
                    spin.blockSignals(False)
                spin.setEnabled(dim > 0 and not auto)
            initial = min(_auto_group_bo_budget(dim, preset)["min_init"], spin.value())
            detail = "not selected" if dim == 0 else ("1D BO init" if config_id == "timing" else f"init ≤ {initial}")
            if dim and spin.value() != _recommended_group_bo_max_evals(dim, preset):
                detail += "; manual max"
            self.group_bo_budget_labels[config_id].setText(f"{spec['ui_label']} (D={dim}; {detail})")
            self.group_bo_budget_labels[config_id].setToolTip(
                (f"Preset max: {max_formula}, D={dim}: {_recommended_group_bo_max_evals(dim, preset)}. "
                 "Corrector D is the selected actuator count, not BPM count.")
                if dim else "Select correctors to calculate their evaluation budget.")

    def _refresh_developer_selection_counts(self):
        actuator_count = sum(
            1
            for row in range(self.dev_actuator_table.rowCount())
            if self.dev_actuator_table.item(row, 0).checkState() == Qt.CheckState.Checked
        )
        bpm_count = sum(
            1
            for row in range(self.dev_bpm_table.rowCount())
            if self.dev_bpm_table.item(row, 0).checkState() == Qt.CheckState.Checked
        )
        self.dev_actuator_count_lbl.setText(f"Selected: {actuator_count}")
        selected_names = [self.dev_actuator_table.item(row, 1).text()
                          for row in range(self.dev_actuator_table.rowCount())
                          if self.dev_actuator_table.item(row, 0).checkState() == Qt.CheckState.Checked]
        summary = ", ".join(selected_names) or "none (choose in Config)"
        self.corrector_summary.setText("Selected: " + (summary if len(summary) <= 100 else summary[:97] + "..."))
        self.corrector_summary.setToolTip(summary)
        self.dev_bpm_count_lbl.setText(f"Selected BPMs: {bpm_count}")
        self._refresh_group_bo_budget_labels()

    def _apply_developer_actuator_filters(self, *_args):
        search = self.dev_actuator_search.text().strip().lower()
        region = self.dev_actuator_region_box.currentText()
        plane = self.dev_actuator_plane_box.currentText()
        selected_only = self.dev_actuator_selected_only.isChecked()
        for row in range(self.dev_actuator_table.rowCount()):
            name = self.dev_actuator_table.item(row, 1).text()
            row_region = self.dev_actuator_table.item(row, 2).text()
            row_plane = self.dev_actuator_table.item(row, 3).text()
            checked = self.dev_actuator_table.item(row, 0).checkState() == Qt.CheckState.Checked
            visible = True
            if search and search not in name.lower():
                visible = False
            if region != "All" and row_region != region:
                visible = False
            if plane != "All" and row_plane != plane:
                visible = False
            if selected_only and not checked:
                visible = False
            self.dev_actuator_table.setRowHidden(row, not visible)

    def _apply_developer_bpm_filters(self, *_args):
        search = self.dev_bpm_search.text().strip().lower()
        region = self.dev_bpm_region_box.currentText()
        selected_only = self.dev_bpm_selected_only.isChecked()
        for row in range(self.dev_bpm_table.rowCount()):
            name = self.dev_bpm_table.item(row, 1).text()
            row_region = self.dev_bpm_table.item(row, 2).text()
            checked = self.dev_bpm_table.item(row, 0).checkState() == Qt.CheckState.Checked
            visible = True
            if search and search not in name.lower():
                visible = False
            if region != "All" and row_region != region:
                visible = False
            if selected_only and not checked:
                visible = False
            self.dev_bpm_table.setRowHidden(row, not visible)

    def _set_check_state_for_visible_rows(self, table: QTableWidget, checked: bool):
        table.blockSignals(True)
        for row in range(table.rowCount()):
            if table.isRowHidden(row):
                continue
            table.item(row, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        table.blockSignals(False)
        self._refresh_developer_selection_counts()
        self._apply_developer_actuator_filters()
        self._apply_developer_bpm_filters()

    def _select_visible_developer_actuators(self):
        self._set_check_state_for_visible_rows(self.dev_actuator_table, True)

    def _clear_visible_developer_actuators(self):
        self._set_check_state_for_visible_rows(self.dev_actuator_table, False)

    def _select_visible_developer_bpms(self):
        self._set_check_state_for_visible_rows(self.dev_bpm_table, True)

    def _clear_visible_developer_bpms(self):
        self._set_check_state_for_visible_rows(self.dev_bpm_table, False)

    def _apply_default_ranges_to_visible_developer_actuators(self):
        for row in range(self.dev_actuator_table.rowCount()):
            if self.dev_actuator_table.isRowHidden(row):
                continue
            plane = self.dev_actuator_table.item(row, 3).text()
            name = self.dev_actuator_table.item(row, 1).text()
            boxes = self.dev_actuator_spinboxes.get(name, {})
            if plane == "H":
                boxes.get("half").setValue(self.dev_h_half.value())
                boxes.get("step").setValue(self.dev_h_step.value())
            else:
                boxes.get("half").setValue(self.dev_v_half.value())
                boxes.get("step").setValue(self.dev_v_step.value())

    def _update_developer_mode_ui(self):
        is_seq = (self.dev_mode_box.currentText() == "SEQUENTIAL")
        self.dev_seq_method_box.setEnabled(is_seq)

    def _update_developer_objective_ui(self):
        intensity = self.dev_objective_box.currentText() == "BPM intensity"
        show_intensity = self.dev_objective_box.currentText() != "BPM sumsq"
        if show_intensity != getattr(self, "_bpm_table_is_intensity", False):
            self._populate_developer_bpm_table(show_intensity)
        self.dev_bpm_count_lbl.setToolTip(
            "LINAC / BT: LINAC:monitors; DR: DR:monitors. Selected sources are sampled sequentially, not simultaneously."
            if show_intensity else "LINAC:monitors")
        is_bpm = self.dev_objective_box.currentText() != "ICT"
        self.selection_objective_hint.setVisible(not is_bpm)
        self.dev_objective_stack.setVisible(True)
        self.dev_objective_stack.setCurrentIndex(1)
        self.dev_bpm_plane_box.setVisible(not show_intensity)
        self.dev_bpm_mode_label.setText("Aggregate" if show_intensity else "Plane")
        self.dev_intensity_aggregation.setVisible(show_intensity)
        self.intensity_help.setVisible(show_intensity)
        self.dr_turn_group.setVisible(show_intensity)
        self.dev_bpm_table.setHorizontalHeaderLabels(
            ["Use", "Name", "Region", "Intensity", "Status"] if show_intensity else
            ["Use", "Name", "Region", "Live X [mm]", "Live Y [mm]"])
        # Never relabel stale position numbers as intensity (or vice versa).
        for row in range(self.dev_bpm_table.rowCount()):
            for column in (3, 4):
                self.dev_bpm_table.item(row, column).setText("-")
        state = self._plot_state.get(RUN_PROFILE_MAIN, {})
        if not state.get("eval_index"):
            state["metric_label"] = "BPM intensity score" if intensity else ("BPM sumsq" if is_bpm else "ICT")
            self._refresh_eval_plot(RUN_PROFILE_MAIN)

    def _selected_developer_actuators(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in range(self.dev_actuator_table.rowCount()):
            if self.dev_actuator_table.item(row, 0).checkState() != Qt.CheckState.Checked:
                continue
            name = self.dev_actuator_table.item(row, 1).text()
            boxes = self.dev_actuator_spinboxes.get(name, {})
            base = _developer_actuator_spec(name)
            out.append({
                **base,
                "half_range": float(boxes.get("half").value()),
                "step": float(boxes.get("step").value()),
            })
        return out

    def _selected_developer_bpms(self) -> List[str]:
        names: List[str] = []
        for row in range(self.dev_bpm_table.rowCount()):
            if self.dev_bpm_table.item(row, 0).checkState() == Qt.CheckState.Checked:
                names.append(self.dev_bpm_table.item(row, 1).text())
        return names

    def _set_developer_actuator_status(self, name: str, state: str):
        row = self.dev_actuator_row_map.get(str(name))
        if row is None:
            return
        item = self.dev_actuator_table.item(row, 7)
        if item is not None:
            item.setText(str(state).upper())

    def _reset_developer_actuator_statuses(self):
        for name in list(self.dev_actuator_row_map.keys()):
            self._set_developer_actuator_status(name, "idle")

    def _dr_turn_config_from_ui(self):
        return {"dr_orbit_mode": self.dr_orbit_mode.currentData(), "dr_turn_scan": self.dr_turn_enabled.isChecked(),
                "dr_turn_start": self.dr_turn_start.value(), "dr_turn_end": self.dr_turn_end.value(),
                "dr_turn_aggregation": self.dr_turn_aggregation.currentData()}

    def _apply_dr_orbit_mode(self):
        try:
            self.dr_bpm_reader.configure_orbit_mode(self.dr_orbit_mode.currentData())
            self.dr_bpm_reader.apply_orbit_mode()
            self.append_log(f"[DR BPM] DRBPM:ORBIT_MODE = {self.dr_orbit_mode.currentText()}")
        except Exception as exc:
            QMessageBox.critical(self, "DR BPM mode", str(exc))

    def _transmission_config_from_ui(self):
        return {"dr_ratio_turn": self.dr_ratio_turn.value(), "dr_current_weight": self.dr_current_weight.value(),
                "dr_ratio_floor": self.dr_ratio_floor.value()}

    def _update_correctors_l8_ui(self, checked):
        self.chk_correctors.setText("Correctors & L8" if checked else "Correctors")
        if checked:
            self.chk_correctors.setChecked(True)
            self.mode_box.setCurrentText("GROUP_BO")
        if hasattr(self, "group_bo_budget_help"):
            self._refresh_group_bo_budget_labels()

    def _l8_actuators(self):
        return [
            {"name": "L8 phase", "pv_write": "CM8L:phaseWrite", "pv_read": "CM8L:phaseRead",
             "half_range": self.l8_phase_half.value(), "step": self.l8_phase_step.value(), "unit": "deg"},
            {"name": "Timing", "pv_write": "EVE_LINAC:OUT0:SetData", "pv_read": None,
             "half_range": self.sp_timing_step.value() * self.sp_timing_half_steps.value(),
             "step": self.sp_timing_step.value(), "unit": "ns"}]

    def _refresh_developer_live_values(self):
        try:
            interface = self._ensure_interface()
            correctors = interface.get_correctors()
            corr_names = [str(name) for name in list(correctors.get("names", []))]
            corr_vals = np.asarray(correctors.get("bact", []), dtype=float).reshape(-1)
            corr_map = {
                corr_names[idx]: corr_vals[idx]
                for idx in range(min(len(corr_names), corr_vals.size))
            }
            for name, row in self.dev_actuator_row_map.items():
                item = self.dev_actuator_table.item(row, 4)
                if item is not None:
                    val = corr_map.get(name, float("nan"))
                    item.setText(_format_significant_value(val, digits=4))

            if self._bpm_table_is_intensity:
                refresh_config = {**self._dr_turn_config_from_ui(), **self._transmission_config_from_ui(),
                                  "intensity_aggregation": self.dev_intensity_aggregation.currentData()}
                configure_dr_turns(self.dr_bpm_reader, refresh_config)
                snapshot = {}
                for source, reader in (("LINAC:monitors", interface), ("DR:monitors", self.dr_bpm_reader)):
                    try:
                        data = reader.get_bpms()
                        if source == "DR:monitors" and refresh_config["intensity_aggregation"] in ("turn_ratio", "turn_composite"):
                            data = data["turn_data"][refresh_config["dr_ratio_turn"]]
                        snapshot.update(bpm_intensity_snapshot(data))
                    except Exception as exc:
                        self.append_log(f"[BPM] {source} refresh failed: {exc}")
                state = self._plot_state.get(RUN_PROFILE_MAIN, {})
                state["bpm_snapshot"] = snapshot
                state["dr_turn_label"] = dr_turn_label(refresh_config)
                state["selected_bpms"] = self._selected_developer_bpms()
                self._refresh_eval_plot(RUN_PROFILE_MAIN)
                for name, row in self.dev_bpm_row_map.items():
                    value = snapshot.get(name, float("nan"))
                    self.dev_bpm_table.item(row, 3).setText(f"{value:.5g}" if np.isfinite(value) else "-")
                    self.dev_bpm_table.item(row, 4).setText("VALID" if np.isfinite(value) else "INVALID")
                return
            bpms = interface.get_bpms()
            bpm_names = [str(name) for name in list(bpms.get("names", []))]
            x_all = np.asarray(bpms.get("x", []), dtype=float)
            y_all = np.asarray(bpms.get("y", []), dtype=float)
            if x_all.ndim == 1:
                x_avg = x_all
            elif x_all.size:
                x_avg = np.nanmean(x_all, axis=0)
            else:
                x_avg = np.array([], dtype=float)
            if y_all.ndim == 1:
                y_avg = y_all
            elif y_all.size:
                y_avg = np.nanmean(y_all, axis=0)
            else:
                y_avg = np.array([], dtype=float)
            for idx, name in enumerate(bpm_names):
                row = self.dev_bpm_row_map.get(name)
                if row is None:
                    continue
                x_item = self.dev_bpm_table.item(row, 3)
                y_item = self.dev_bpm_table.item(row, 4)
                x_val = x_avg[idx] if idx < x_avg.size else float("nan")
                y_val = y_avg[idx] if idx < y_avg.size else float("nan")
                if x_item is not None:
                    x_item.setText(f"{x_val:+.4f}" if np.isfinite(x_val) else "-")
                if y_item is not None:
                    y_item.setText(f"{y_val:+.4f}" if np.isfinite(y_val) else "-")
        except Exception as exc:
            self.append_log(f"[Developer] live value refresh failed: {exc}")

    def _compute_bpm_metric_for_config(self, config: Dict[str, Any]) -> float:
        bpm_names = [str(name) for name in list(config.get("objective_bpm_names", [])) if str(name).strip()]
        if not bpm_names:
            return float("nan")
        plane = str(config.get("objective_bpm_plane", "XY")).upper()
        if plane not in ("X", "Y", "XY"):
            plane = "XY"
        interface = self._ensure_interface()
        bpms = interface.get_bpms()
        names = [str(name) for name in list(bpms.get("names", []))]
        x_all = np.asarray(bpms.get("x", []), dtype=float)
        y_all = np.asarray(bpms.get("y", []), dtype=float)
        if x_all.ndim == 1:
            x_avg = x_all
        elif x_all.size:
            x_avg = np.nanmean(x_all, axis=0)
        else:
            x_avg = np.array([], dtype=float)
        if y_all.ndim == 1:
            y_avg = y_all
        elif y_all.size:
            y_avg = np.nanmean(y_all, axis=0)
        else:
            y_avg = np.array([], dtype=float)
        index_map = {name: idx for idx, name in enumerate(names)}
        metric = 0.0
        used = 0
        for bpm_name in bpm_names:
            idx = index_map.get(bpm_name)
            if idx is None:
                continue
            if plane in ("X", "XY") and idx < x_avg.size and np.isfinite(x_avg[idx]):
                metric += float(x_avg[idx]) ** 2
                used += 1
            if plane in ("Y", "XY") and idx < y_avg.size and np.isfinite(y_avg[idx]):
                metric += float(y_avg[idx]) ** 2
                used += 1
        return float(metric) if used > 0 else float("nan")

    def _on_tab_changed(self, _index: int):
        # Tab navigation must not trigger synchronous EPICS reads.
        pass

    def _refresh_developer_on_request(self):
        if self.worker is not None and self.worker.isRunning():
            return
        self.dev_refresh_btn.setEnabled(False)
        self.dev_refresh_btn.setText("Reading...")
        try:
            self._refresh_developer_live_values()
        finally:
            self.dev_refresh_btn.setText("Refresh currents / BPMs")
            self.dev_refresh_btn.setEnabled(True)

    def _add_target_row(
            self,
            parent_layout,
            key: str,
            text: str,
            *,
            checked: bool,
            prominent: bool,
            indent: int = 0,
            show_value: bool = True,
    ):
        row = QHBoxLayout()
        row.setSpacing(10)
        if indent > 0:
            row.addSpacing(indent)

        box = QCheckBox(text)
        box.setChecked(bool(checked))
        if prominent:
            box.setProperty("knobCheck", True)
        else:
            box.setStyleSheet("font-size: 18px; font-weight: 500; color: #334155;")
        row.addWidget(box)

        status_lbl = QLabel("IDLE")
        status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status_lbl.setMinimumWidth(88)
        status_lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        row.addWidget(status_lbl)

        if show_value:
            value_lbl = QLabel("Current: -")
            value_lbl.setWordWrap(True)
            value_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value_lbl.setStyleSheet("font-size: 14px; color: #475569;")
            row.addWidget(value_lbl, stretch=1)
            self.target_value_labels[key] = value_lbl
        else:
            row.addStretch(1)

        parent_layout.addLayout(row)

        self.target_checks[key] = box
        self.target_status_labels[key] = status_lbl
        self._set_target_state(key, "idle")
        return box

    def _set_target_state(self, key: str, state: str):
        lbl = self.target_status_labels.get(key)
        if lbl is None:
            return
        palette = {
            "idle": ("IDLE", "#334155", "#e2e8f0", "#94a3b8"),
            "waiting": ("WAITING", "#1d4ed8", "#dbeafe", "#60a5fa"),
            "running": ("RUNNING", "#166534", "#dcfce7", "#4ade80"),
            "done": ("DONE", "#1e3a8a", "#dbeafe", "#3b82f6"),
            "error": ("ERROR", "#991b1b", "#fee2e2", "#f87171"),
        }
        text, fg, bg, bd = palette.get(str(state).lower(), palette["idle"])
        lbl.setText(text)
        lbl.setStyleSheet(
            "QLabel {"
            f"font-size: 12px; font-weight: 800; color: {fg}; "
            f"background: {bg}; border: 1px solid {bd}; "
            "border-radius: 9px; padding: 5px 8px; }"
        )
        self.target_states[key] = str(state).lower()

    def _reset_target_states(self):
        for key in list(self.target_status_labels.keys()):
            self._set_target_state(key, "idle")

    def _selected_target_keys(self, mode: Optional[str] = None) -> List[str]:
        mode_text = str(mode or self.mode_box.currentText())
        keys: List[str] = []
        if self.chk_gun_sol_l0_phase.isChecked():
            keys.append("gun_sol_l0")
        if self.chk_kly_phase.isChecked():
            keys.append("kly_phase")
            if mode_text == "GROUP_BO":
                if self.chk_kly_group1.isChecked():
                    keys.append("kly_group_1")
                if self.chk_kly_group2.isChecked():
                    keys.append("kly_group_2")
        if self.chk_timing.isChecked():
            keys.append("timing")
        if self.chk_qa.isChecked():
            keys.append("qa")
        if self.chk_qm.isChecked():
            keys.append("qm")
        if self.chk_correctors.isChecked():
            keys.append("correctors")
        return keys

    def _prepare_target_states_for_run(self, mode: str):
        self._reset_target_states()
        for key in self._selected_target_keys(mode):
            self._set_target_state(key, "waiting")

    def _finalize_target_states_after_run(self):
        for key, state in list(self.target_states.items()):
            if state in ("waiting", "running"):
                self._set_target_state(key, "idle")

    def _update_value_labels(self, display_values: Dict[str, str]):
        for key, lbl in self.target_value_labels.items():
            txt = str(display_values.get(key, "")).strip()
            lbl.setText(f"Current: {txt}" if txt else "Current: -")

    def _ensure_interface(self) -> InterfaceATF2_LinacBT:
        if self.interface is None:
            self.interface = InterfaceATF2_LinacBT(nsamples=1)
        return self.interface

    def _refresh_current_values(self, *_args):
        try:
            interface = self._ensure_interface()
            pv_values: Dict[str, float] = {}
            for specs in TARGET_VALUE_SPECS.values():
                for _label, pv_write, pv_read, _unit in specs:
                    if pv_write in pv_values:
                        continue
                    pv_values[pv_write] = float(interface.read_current(pv_write, pv_read))
            self._update_value_labels(build_display_value_texts(pv_values))
            self._current_value_refresh_failed = False
        except Exception as exc:
            self._update_value_labels({})
            if not self._current_value_refresh_failed:
                self.append_log(f"[UI] current value refresh failed: {exc}")
            self._current_value_refresh_failed = True

    def _auto_refresh_current_values(self):
        if self.tabs.currentWidget() is not self.main_tab:
            return
        if self.worker is not None and self.worker.isRunning():
            return
        self._refresh_current_values()

    def _reset_eval_plot(self, metric_label: Optional[str] = None, profile: Optional[str] = None):
        context = str(profile or self._run_profile or RUN_PROFILE_MAIN).upper()
        state = self._plot_state.setdefault(
            context,
            {
                "eval_index": [],
                "ttot": [],
                "metric": [],
                "metric_label": "DR",
                "measurement_ok": [],
                "discarded_eval_index": [],
                "discarded_ttot": [],
                "discarded_metric": [],
                "bo1d_trace": None,
            },
        )
        state["eval_index"] = []
        state["evaluation_phase"] = []
        state["ttot"] = []
        state["metric"] = []
        state["bpm_intensities"] = []
        state["bpm_snapshot"] = {}
        state["turn_plot"] = {}
        state["turn_transmission"] = []
        state["turn_normalized_current"] = []
        state["dr_turn_label"] = ""
        state["selected_bpms"] = self._selected_developer_bpms()
        state["measurement_ok"] = []
        state["discarded_eval_index"] = []
        state["discarded_ttot"] = []
        state["discarded_metric"] = []
        state["bo1d_trace"] = None
        if metric_label is not None:
            state["metric_label"] = str(metric_label)
        self._refresh_eval_plot(context)
        self._refresh_bo1d_plot(context)

    def _refresh_eval_plot(self, profile: Optional[str] = None):
        context = str(profile or RUN_PROFILE_MAIN).upper()
        widgets = self._plot_widgets.get(context)
        state = self._plot_state.get(context)
        if not widgets or not state:
            return

        ax_ttot = widgets["ax_ttot"]
        ax_metric = widgets["ax_metric"]
        ax_ttot.clear()
        ax_metric.clear()

        ax_ttot.set_title("Transmission vs evaluation", pad=10)
        ax_ttot.set_ylabel("Ttot")
        ax_ttot.tick_params(axis="x", labelbottom=False)
        ax_ttot.grid(True, alpha=0.3)

        metric_label = str(state.get("metric_label", "Metric") or "Metric")
        is_intensity = metric_label.startswith("BPM intensity")
        is_turn = metric_label in ("BPM intensity turn_ratio", "BPM intensity turn_composite")
        if context == RUN_PROFILE_MAIN:
            self.turn_plot_view.setVisible(is_turn)
        if is_intensity:
            aggregation = metric_label.removeprefix("BPM intensity").strip()
            objective_label = {"raw_sum": "Raw sum", "raw_mean": "Raw mean",
                               "relative_mean": "Mean / initial baseline"}.get(aggregation, "Score")
            ax_ttot.set_title(f"BPM intensity objective ({objective_label}) vs evaluation")
            ax_ttot.set_ylabel(objective_label)
        if is_turn:
            ax_ttot.set_title("DR objective and components vs evaluation")
            ax_ttot.set_ylabel("Dimensionless")
        ax_metric.set_title(f"{metric_label} vs evaluation", pad=10)
        ax_metric.set_ylabel(metric_label)
        ax_metric.set_xlabel("Evaluation")
        ax_metric.grid(True, alpha=0.3)

        if state["eval_index"]:
            phases = state.get("evaluation_phase", [])
            shade_evaluation_phases(ax_ttot, state["eval_index"], phases)
            if not is_intensity:
                shade_evaluation_phases(ax_metric, state["eval_index"], phases)
            eval_index = np.asarray(state["eval_index"], dtype=float)
            ttot = np.asarray(state["ttot"], dtype=float)
            metric = np.asarray(state["metric"], dtype=float)
            measurement_ok = np.asarray(state.get("measurement_ok", [True] * len(state["eval_index"])), dtype=bool)
            if measurement_ok.size != eval_index.size:
                measurement_ok = np.ones_like(eval_index, dtype=bool)

            upper_values = metric if is_intensity else ttot
            valid_ttot = np.where(measurement_ok, upper_values, np.nan)
            valid_metric = np.where(measurement_ok, metric, np.nan)
            ax_ttot.plot(eval_index, valid_ttot, marker="o", color="#2563eb", linewidth=2.4 if is_turn else 1.8,
                         label="Score (= mean transmission)" if metric_label.endswith("turn_ratio") else "Score")
            if is_turn:
                if metric_label.endswith("turn_composite"):
                    transmission = np.asarray(state.get("turn_transmission", []), dtype=float)
                    if transmission.size == eval_index.size:
                        ax_ttot.plot(eval_index, np.where(measurement_ok, transmission, np.nan),
                                     color="#0f766e", linewidth=1.2, label="Mean transmission")
                normalized = np.asarray(state.get("turn_normalized_current", []), dtype=float)
                if normalized.size == eval_index.size:
                    weight = state.get("turn_plot", {}).get("weight", 1.)
                    label = (f"Normalized turn 1 current (unweighted; w={weight:g})"
                             if metric_label.endswith("turn_composite") else "Normalized turn 1 current (reference)")
                    ax_ttot.plot(eval_index, np.where(measurement_ok, normalized, np.nan),
                                 color="#ea580c", linewidth=1.2, label=label)
                valid_scores = np.where(measurement_ok & np.isfinite(metric), metric, np.nan)
                ax_ttot.plot(eval_index, np.fmax.accumulate(valid_scores), color="#64748b",
                             linestyle="--", linewidth=1.2, label="Best score so far")
                ax_ttot.legend(fontsize=7, loc="best", ncol=2)
            ax_metric.plot(eval_index, valid_metric, marker="o", color="#0f766e", linewidth=1.8)

            invalid_mask = ~measurement_ok
            if np.any(invalid_mask):
                ax_ttot.plot(
                    eval_index[invalid_mask],
                    upper_values[invalid_mask],
                    linestyle="None",
                    marker="x",
                    color="#dc2626",
                    markersize=8,
                    markeredgewidth=2.0,
                )
                ax_metric.plot(
                    eval_index[invalid_mask],
                    metric[invalid_mask],
                    linestyle="None",
                    marker="x",
                    color="#dc2626",
                    markersize=8,
                    markeredgewidth=2.0,
                )

        discarded_eval = np.asarray(state.get("discarded_eval_index", []), dtype=float)
        discarded_ttot = np.asarray(state.get("discarded_ttot", []), dtype=float)
        discarded_metric = np.asarray(state.get("discarded_metric", []), dtype=float)
        if discarded_eval.size > 0:
            ax_ttot.plot(
                discarded_eval,
                discarded_metric if is_intensity else discarded_ttot,
                linestyle="None",
                marker="x",
                color="#dc2626",
                markersize=8,
                markeredgewidth=2.0,
            )
            ax_metric.plot(
                discarded_eval,
                discarded_metric,
                linestyle="None",
                marker="x",
                color="#dc2626",
                markersize=8,
                markeredgewidth=2.0,
            )

        if is_intensity:
            if is_turn:
                ax_ttot.tick_params(axis="x", labelbottom=True)
                self._draw_turn_bpm_plot(ax_metric, state)
                widgets["fig"].subplots_adjust(bottom=.20, hspace=.65)
                widgets["canvas"].draw_idle()
                return
            from matplotlib.patches import Patch
            ax_ttot.tick_params(axis="x", labelbottom=True)
            # The title already identifies evaluation count; leave space for
            # the independent lower plot's title at the default window height.
            ax_ttot.set_xlabel("")
            ax_metric.clear()
            snapshot = state.get("bpm_snapshot", {})
            names = list(INTENSITY_PLOT_BPM_NAMES)
            positions = np.arange(len(names))
            values = np.array([snapshot.get(name, np.nan) for name in names])
            selected_names = set(state.get("selected_bpms", self._selected_developer_bpms()))
            colors = ["#ea580c" if name in selected_names else "#94a3b8" for name in names]
            ax_metric.bar(positions, values, color=colors, width=.8)
            invalid = ~np.isfinite(values)
            if invalid.any():
                ax_metric.scatter(positions[invalid], np.full(invalid.sum(), .025),
                                  transform=ax_metric.get_xaxis_transform(), marker="x", s=12,
                                  color="#64748b", label="No valid data (not zero)")
            ax_metric.set_xticks(positions, names, rotation=90, fontsize=6)
            for tick, name in zip(ax_metric.get_xticklabels(), names):
                if name in selected_names:
                    tick.set_color("#c2410c")
                    tick.set_fontweight("bold")
            ax_metric.set_xlim(-1, len(names))
            turn_label = state.get("dr_turn_label", "")
            ax_metric.set_title("Latest intensity by BPM (sample mean)" + ("\n" + turn_label if turn_label else ""), pad=10)
            ax_metric.set_ylabel("Intensity (raw)")
            ax_metric.set_xlabel("BPM (LINAC / BT / DR from MB21R; unread regions shown as gaps)")
            ax_metric.grid(axis="y", alpha=.3)
            handles, labels = ax_metric.get_legend_handles_labels()
            ax_metric.legend([Patch(color="#ea580c"), Patch(color="#94a3b8")] + handles,
                             ["Selected for optimization", "Not selected"] + labels,
                             fontsize=7, ncol=3, loc="upper right")
            widgets["fig"].subplots_adjust(bottom=.20, hspace=.65)
        else:
            widgets["fig"].subplots_adjust(bottom=.12, hspace=.42)
        widgets["canvas"].draw_idle()

    def _draw_turn_bpm_plot(self, axis, state):
        """Only render cached evaluation data; never access the machine."""
        from matplotlib.patches import Patch
        axis.clear()
        data = state.get("turn_plot", {})
        names = list(INTENSITY_PLOT_BPM_NAMES)
        positions = np.arange(len(names))
        selected = set(state.get("selected_bpms", []))
        colors = ["#ea580c" if name in selected else "#94a3b8" for name in names]
        dr_mask = np.array([name in DR_BPM_NAMES for name in names])
        intensity = self.turn_plot_view.currentData() == "intensity"
        keys = ("first", "last") if intensity else ("ratios",)
        for index, key in enumerate(keys):
            values = np.array([data.get(key, {}).get(name, np.nan) if name in DR_BPM_NAMES else np.nan for name in names])
            offset = (-.21 if index == 0 else .21) if intensity else 0
            axis.bar(positions + offset, values, width=.4 if intensity else .8,
                     color=colors, alpha=.45 if intensity and index == 0 else 1.,
                     hatch="//" if intensity and index == 0 else None)
            missing = dr_mask & ~np.isfinite(values)
            if missing.any():
                axis.scatter(positions[missing] + offset, np.full(missing.sum(), .025),
                             transform=axis.get_xaxis_transform(), marker="x", s=12, color="#64748b")
        if not intensity:
            axis.axhline(1., color="#64748b", linestyle="--", linewidth=1.)
        if (~dr_mask).any():
            axis.axvspan(-.5, np.flatnonzero(~dr_mask)[-1] + .5, color="#e2e8f0", alpha=.5)
        axis.set_xticks(positions, names, rotation=90, fontsize=6)
        for tick, name in zip(axis.get_xticklabels(), names):
            if name in selected:
                tick.set_color("#c2410c")
                tick.set_fontweight("bold")
        axis.set_xlim(-1, len(names))
        target = data.get("target", "N")
        axis.set_title(f"Latest BPM intensities: turn 1 / turn {target}" if intensity else f"Latest BPM transmission: turn {target} / turn 1", pad=10)
        axis.set_ylabel("Intensity (raw)" if intensity else "Transmission N / 1")
        axis.set_xlabel("BPM (LINAC / BT: not applicable; DR from MB21R; × = missing/invalid, not zero)")
        axis.grid(axis="y", alpha=.3)
        handles = [Patch(color="#ea580c", label="Selected"), Patch(color="#94a3b8", label="Not selected")]
        if intensity:
            handles += [Patch(facecolor="#94a3b8", alpha=.45, hatch="//", label="Turn 1"),
                        Patch(facecolor="#94a3b8", label=f"Turn {target}")]
        axis.legend(handles=handles, fontsize=7, ncol=len(handles), loc="upper right")

    def _refresh_bo1d_plot(self, profile: Optional[str] = None):
        context = str(profile or RUN_PROFILE_MAIN).upper()
        widgets = self._plot_widgets.get(context)
        state = self._plot_state.get(context)
        if not widgets or not state:
            return

        canvas = widgets.get("bo1d_canvas")
        ax_obj = widgets.get("bo1d_ax_obj")
        ax_acq = widgets.get("bo1d_ax_acq")
        if canvas is None or ax_obj is None or ax_acq is None:
            return

        ax_obj.clear()
        ax_acq.clear()
        trace = dict(state.get("bo1d_trace") or {})
        visible = bool(trace and trace.get("x_grid"))
        canvas.setVisible(visible)
        if not visible:
            canvas.draw_idle()
            return

        x_grid = np.asarray(trace.get("x_grid", []), dtype=float)
        y_mean = np.asarray(trace.get("y_mean", []), dtype=float)
        y_std = np.asarray(trace.get("y_std", []), dtype=float)
        acq = np.asarray(trace.get("acquisition", []), dtype=float)
        x_obs = np.asarray(trace.get("x_obs", []), dtype=float)
        y_obs = np.asarray(trace.get("y_obs", []), dtype=float)
        chosen_x = float(trace.get("chosen_x", float("nan")))
        chosen_acq = float(trace.get("chosen_acq", float("nan")))
        axis_name = str(trace.get("axis", "Parameter"))
        y_label = str(trace.get("y_label", "Score"))
        acq_label = str(trace.get("acquisition_label", "Acquisition"))
        note = str(trace.get("note", "") or "")

        if x_grid.size and y_mean.size == x_grid.size and y_std.size == x_grid.size:
            lo_band = y_mean - y_std
            hi_band = y_mean + y_std
            band_mask = np.isfinite(x_grid) & np.isfinite(lo_band) & np.isfinite(hi_band)
            if np.any(band_mask):
                ax_obj.fill_between(
                    x_grid[band_mask], lo_band[band_mask], hi_band[band_mask],
                    color="#cfe8ff", alpha=0.65, label="Surrogate ±1σ",
                )
            mean_mask = np.isfinite(x_grid) & np.isfinite(y_mean)
            if np.any(mean_mask):
                ax_obj.plot(
                    x_grid[mean_mask], y_mean[mean_mask],
                    linestyle="--", linewidth=1.8, color="#1d4ed8", label="Surrogate mean",
                )

        obs_mask = np.isfinite(x_obs) & np.isfinite(y_obs)
        if np.any(obs_mask):
            ax_obj.plot(
                x_obs[obs_mask], y_obs[obs_mask],
                linestyle="None", marker="o", markersize=6,
                color="#111827", label="Measured points",
            )

        if np.isfinite(chosen_x):
            ax_obj.axvline(chosen_x, color="#b45309", linestyle=":", linewidth=1.6, label="Chosen x")
            ax_acq.axvline(chosen_x, color="#b45309", linestyle=":", linewidth=1.6)

        acq_mask = np.isfinite(x_grid) & np.isfinite(acq)
        if np.any(acq_mask):
            ax_acq.plot(
                x_grid[acq_mask], acq[acq_mask],
                color="#d97706", linewidth=1.8, label=f"{acq_label} acquisition",
            )
        if np.isfinite(chosen_x) and np.isfinite(chosen_acq):
            ax_acq.plot(
                [chosen_x], [chosen_acq],
                linestyle="None", marker="o", markersize=7,
                color="#92400e", label="Chosen: max acquisition",
            )

        ax_obj.set_title(f"1D BO surrogate for {axis_name}")
        ax_obj.set_ylabel(y_label)
        ax_obj.grid(True, alpha=0.3)
        if note:
            ax_obj.text(
                0.01, 0.98, note,
                transform=ax_obj.transAxes,
                ha="left", va="top", fontsize=8.5, color="#475569",
            )

        ax_acq.set_title("Why this point was chosen")
        ax_acq.set_xlabel(axis_name)
        ax_acq.set_ylabel(acq_label)
        ax_acq.grid(True, alpha=0.3)

        if ax_obj.get_legend_handles_labels()[0]:
            ax_obj.legend(loc="best")
        if ax_acq.get_legend_handles_labels()[0]:
            ax_acq.legend(loc="best")
        canvas.draw_idle()

    def _on_worker_progress(self, payload: dict):
        context = self._run_profile
        kind = str(payload.get("kind", ""))
        if kind == "group_state":
            key = str(payload.get("group_key", ""))
            state = str(payload.get("state", "idle")).lower()
            if context == RUN_PROFILE_MAIN:
                if state == "done":
                    self._set_target_state(key, "done")
                elif state == "running":
                    self._set_target_state(key, "running")
                    label = TARGET_LABELS.get(key, key)
                    if self._run_mode:
                        self._set_status(f"Status: RUNNING {self._run_mode} | {label}", state="running",
                                         profile=context)
                else:
                    self._set_target_state(key, state)
            else:
                if self._run_mode:
                    self._set_status(f"Status: RUNNING {self._run_mode}", state="running", profile=context)
            return

        if kind == "developer_actuator_state":
            self._set_developer_actuator_status(str(payload.get("name", "")), str(payload.get("state", "idle")))
            return

        if kind == "current_values":
            self._update_value_labels(dict(payload.get("display_values") or {}))
            return

        if kind == "run_error":
            self._run_failed = True
            self._set_status("Status: FAILED", state="error", profile=context)
            return

        if kind == "pause_state":
            if self.worker is not None and not self.worker.is_running:
                return  # A queued pause acknowledgement must not undo STOP.
            paused = bool(payload.get("paused"))
            for button in (self.btn_stop, self.dev_btn_stop):
                button.setText("RESUME" if paused else "PAUSE")
                button.setEnabled(True)
            self._set_status("Status: PAUSED — settings retained" if paused else "Status: RUNNING",
                             state="paused" if paused else "running", profile=context)
            return

        if kind == "bo1d_trace":
            state = self._plot_state.setdefault(
                context,
                {
                    "eval_index": [],
                    "ttot": [],
                    "metric": [],
                    "metric_label": "DR",
                    "measurement_ok": [],
                    "discarded_eval_index": [],
                    "discarded_ttot": [],
                    "discarded_metric": [],
                    "bo1d_trace": None,
                },
            )
            state["bo1d_trace"] = dict(payload.get("trace") or {})
            self._refresh_bo1d_plot(context)
            return

        if kind == "evaluation":
            self._update_value_labels(dict(payload.get("display_values") or {}))
            state = self._plot_state.setdefault(
                context,
                {
                    "eval_index": [],
                    "ttot": [],
                    "metric": [],
                    "metric_label": "DR",
                    "measurement_ok": [],
                    "discarded_eval_index": [],
                    "discarded_ttot": [],
                    "discarded_metric": [],
                    "bo1d_trace": None,
                },
            )
            state["eval_index"].append(int(payload.get("eval_index", len(state["eval_index"]) + 1)))
            state.setdefault("evaluation_phase", [""] * (len(state["eval_index"]) - 1)).append(
                str(payload.get("evaluation_phase", "")))
            state["ttot"].append(float(payload.get("ttot", float("nan"))))
            state["metric"].append(float(payload.get("downstream_value", float("nan"))))
            state["measurement_ok"].append(bool(payload.get("measurement_ok", True)))
            state["metric_label"] = str(payload.get("downstream_label", state.get("metric_label", "DR")))
            turn_data = dict(payload.get("turn_plot") or {})
            state["turn_plot"] = turn_data
            for key, field in (("turn_transmission", "transmission"), ("turn_normalized_current", "normalized_current")):
                history = state.setdefault(key, [float("nan")] * (len(state["eval_index"]) - 1))
                history.append(float(turn_data.get(field, float("nan"))))
            if payload.get("bpm_intensities"):
                values = payload["bpm_intensities"]
                state.setdefault("bpm_intensities", []).append(values)
                state["bpm_snapshot"] = dict(payload.get("bpm_snapshot", values))
                state["selected_bpms"] = list(payload.get("selected_bpms", values))
                state["dr_turn_label"] = payload.get("dr_turn_label", "")
                for name, value in values.items():
                    row = self.dev_bpm_row_map.get(name)
                    if row is not None:
                        self.dev_bpm_table.item(row, 3).setText(f"{value:.5g}")
                        self.dev_bpm_table.item(row, 4).setText("READ (status unchecked)")
            self._refresh_eval_plot(context)
            if self._run_mode:
                self._set_status(
                    f"Status: RUNNING {self._run_mode} | eval={state['eval_index'][-1]} | " +
                    (f"Intensity score={state['metric'][-1]:.6g}" if payload.get("bpm_intensities") else f"Ttot={state['ttot'][-1]:.6f}"),
                    state="running",
                    profile=context,
                )

    def _set_status(self, text: str, *, state: str = "info", profile: Optional[str] = None) -> None:
        context = str(profile or RUN_PROFILE_MAIN).upper()
        target = self._status_labels.get(context)
        if target is None:
            return
        palette = {
            "idle": ("#374151", "#e5e7eb", "#9ca3af"),
            "running": ("#14532d", "#dcfce7", "#22c55e"),
            "paused": ("#713f12", "#fef3c7", "#f59e0b"),
            "warning": ("#7c2d12", "#ffedd5", "#fb923c"),
            "error": ("#7f1d1d", "#fee2e2", "#ef4444"),
            "success": ("#1e3a8a", "#dbeafe", "#3b82f6"),
            "info": ("#1f2937", "#e5e7eb", "#9ca3af"),
        }
        fg, bg, bd = palette.get(str(state).lower(), palette["info"])
        target.setText(text)
        target.setStyleSheet(
            "QLabel#statusBadge {"
            f"font-size: 24px; font-weight: 800; color: {fg}; "
            f"background: {bg}; border: 2px solid {bd}; "
            "border-radius: 10px; padding: 8px 14px; }"
        )

    def _update_mode_ui(self):
        is_seq = (self.mode_box.currentText() == "SEQUENTIAL")
        self.seq_method_box.setEnabled(is_seq)
        self.grp_group_bo_budget.setEnabled(not is_seq)
        enable_kly_subgroups = (self.mode_box.currentText() == "GROUP_BO") and self.chk_kly_phase.isChecked()
        self.chk_kly_group1.setEnabled(enable_kly_subgroups)
        self.chk_kly_group2.setEnabled(enable_kly_subgroups)

    def _capture_machine_origin(self, config: Dict[str, Any]) -> Dict[str, Any]:
        interface = self._ensure_interface()
        restore_pvs = [str(pv) for pv in list(config.get("restore_pvs", RESTORE_PVS))]
        setpoints = {
            pv: float(interface.read_current(pv, _default_readback_pv(pv)))
            for pv in restore_pvs
        }
        if config.get("objective_type") == DEVELOPER_OBJECTIVE_INTENSITY:
            configure_dr_turns(self.dr_bpm_reader, config)
            if config["intensity_aggregation"] in ("turn_ratio", "turn_composite"):
                score, first, _, _, transmission, normalized = read_turn_objective(
                    self.dr_bpm_reader, config["objective_bpm_names"], config)
                config["intensity_baseline"] = first
                self.append_log(f"[DR baseline] transmission={transmission:.6g}; normalized meanCurrent={normalized:.6g}")
                return {"config": dict(config), "initial_setpoints": setpoints,
                        "initial_ict": {}, "initial_bpm_metric": score,
                        "initial_objective_value": score, "initial_score": score,
                        "initial_objective_label": "BPM intensity " + config["intensity_aggregation"],
                        "initial_measurement_ok": True, "initial_measurement_reason": ""}
            values = read_selected_bpm_intensities(interface, self.dr_bpm_reader, config["objective_bpm_names"], self.append_log,
                                         baseline=config["intensity_aggregation"] == "relative_mean")
            config["intensity_baseline"] = values
            score = intensity_score(values, values, config["intensity_aggregation"])
            return {"config": dict(config), "initial_setpoints": setpoints,
                    "initial_ict": {}, "initial_bpm_metric": score,
                    "initial_objective_value": score, "initial_score": score,
                    "initial_objective_label": "BPM intensity " + config["intensity_aggregation"],
                    "initial_measurement_ok": True, "initial_measurement_reason": ""}
        initial_ict, initial_measurement_ok, initial_measurement_reason = _read_icts_with_retry(
            interface,
            downstream_key=str(config.get("downstream_ict", "DR")).upper().replace("LN0", "L0"),
            sample_count=max(1, int(config.get("ict_samples", config.get("ict_dr_samples", ICT_SAMPLES_DEFAULT)))),
            sample_interval_s=float(
                config.get("ict_sample_interval_s", config.get("ict_dr_interval_s", ICT_SAMPLE_INTERVAL_S_DEFAULT))),
            max_retries_per_sample=max(1, int(config.get("ict_max_retries_per_sample", config.get("ict_max_attempts",
                                                                                                  ICT_MAX_RETRIES_PER_SAMPLE_DEFAULT)))),
            retry_wait_s=float(config.get("ict_retry_wait_s", ICT_RETRY_WAIT_S_DEFAULT)),
        )
        objective_type = str(config.get("objective_type", DEVELOPER_OBJECTIVE_ICT)).upper()
        objective_label = str(config.get("downstream_ict", "DR")).upper()
        objective_value = float("nan")
        bpm_metric = float("nan")
        if objective_type == DEVELOPER_OBJECTIVE_BPM:
            bpm_metric = self._compute_bpm_metric_for_config(config)
            objective_label = f"BPM {str(config.get('objective_bpm_plane', 'XY')).upper()} sumsq"
            objective_value = float(bpm_metric)
            score = -float(bpm_metric) if np.isfinite(bpm_metric) else -1e30
        else:
            ttot = float(initial_ict.get("Ttot", float("nan")))
            downstream_key = str(config.get("downstream_ict", "DR")).upper()
            downstream_lookup = "L0" if downstream_key == "LN0" else downstream_key
            downstream_value = float(initial_ict.get(downstream_lookup, float("nan")))
            objective_value = float(downstream_value)
            score = float(config.get("score_w_ttot", 1.0)) * ttot + float(
                config.get("score_w_downstream", 1.0)) * downstream_value
        return {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "mode": str(config.get("mode", "")),
            "config": dict(config),
            "initial_setpoints": setpoints,
            "initial_ict": initial_ict,
            "initial_bpm_metric": bpm_metric,
            "initial_objective_label": objective_label,
            "initial_objective_value": objective_value,
            "initial_score": score,
            "initial_measurement_ok": bool(initial_measurement_ok),
            "initial_measurement_reason": str(initial_measurement_reason),
        }

    def _apply_machine_origin(self, origin: Dict[str, Any]):
        setpoints = dict(origin.get("initial_setpoints", {}) or {})
        if not setpoints:
            raise ValueError("No initial setpoints were stored for this run.")
        self._ensure_interface().pv_put_many({str(k): float(v) for k, v in setpoints.items()})

    def _delta_specs_for_origin(self, origin: Dict[str, Any], profile: str) -> List[
        Tuple[str, str, Optional[str], str]]:
        config = dict(origin.get("config", {}) or {})
        context = str(profile or config.get("run_profile", RUN_PROFILE_MAIN)).upper()
        if context == RUN_PROFILE_DEVELOPER:
            specs: List[Tuple[str, str, Optional[str], str]] = []
            for item in list(config.get("developer_actuators", [])):
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label", item.get("name", "Actuator")))
                pv_write = str(item.get("pv_write", "")).strip()
                if not pv_write:
                    continue
                pv_read = str(item.get("pv_read", "")).strip() or None
                unit = str(item.get("unit", "A"))
                specs.append((label, pv_write, pv_read, unit))
            return specs
        specs = _main_run_delta_specs(config)
        specs.extend((item["name"], item["pv_write"], item.get("pv_read"), item.get("unit", "A"))
                     for item in config.get("developer_actuators", []))
        return specs

    def _append_final_delta_summary(self, profile: str):
        origin = self.current_machine_origin
        if not origin:
            return
        specs = self._delta_specs_for_origin(origin, profile)
        if not specs:
            return

        initial_setpoints = {
            str(pv): float(value)
            for pv, value in dict(origin.get("initial_setpoints", {}) or {}).items()
        }
        final_setpoints = {
            str(pv): float(value)
            for pv, value in dict(self._last_setpoint_values or {}).items()
        }
        lines = [
            "[FINAL DELTA] final set - initial set",
            "Device | Initial | Final | Delta",
        ]
        has_row = False
        for label, pv_write, pv_read, unit in specs:
            initial = float(initial_setpoints.get(pv_write, float("nan")))
            final = float(final_setpoints.get(pv_write, float("nan")))
            delta = final - initial if np.isfinite(initial) and np.isfinite(final) else float("nan")
            lines.append(
                f"{label} | {_format_machine_value(initial, unit)} | "
                f"{_format_machine_value(final, unit)} | {_format_delta_value(delta, unit)}"
            )
            has_row = True
        if has_row:
            self._append_log_text("\n".join(lines), profile=profile)

    def _save_live_plot_snapshot(self, profile: str):
        context = str(profile or RUN_PROFILE_MAIN).upper()
        widgets = self._plot_widgets.get(context)
        csv_path = self.current_measurements_csv_by_profile.get(context)
        if not widgets or csv_path is None:
            return
        fig = widgets.get("fig")
        if fig is None or not csv_path.exists():
            return
        run_dir = csv_path.parent
        metric_label = str(self._plot_state.get(context, {}).get("metric_label", "Metric") or "Metric")
        safe_metric = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in metric_label).strip(
            "_") or "Metric"
        stem = csv_path.stem
        run_tag = stem.split("LiniacOptimization_Log_", 1)[-1] if "LiniacOptimization_Log_" in stem else ""
        prefix = f"LivePlot_{run_tag}_{context}" if run_tag else f"LivePlot_{context}"
        out_path = run_dir / f"{prefix}_{safe_metric}.png"
        try:
            fig.savefig(out_path, dpi=180, bbox_inches="tight")
            self._append_log_text(f"Saved live plot: {out_path}", profile=context)
        except Exception as exc:
            self._append_log_text(f"Live plot save failed: {exc}", profile=context)

    def _find_resume_snapshot_file(self, csv_path: Path) -> Optional[Path]:
        csv_path = csv_path.expanduser().resolve()
        stem = csv_path.stem
        tag = stem.split("LiniacOptimization_Log_", 1)[-1] if "LiniacOptimization_Log_" in stem else ""
        parent = csv_path.parent
        if tag:
            candidate = parent / f"InitialSnapshot_{tag}.json"
            if candidate.exists():
                return candidate
        tagged = sorted(parent.glob("InitialSnapshot_*.json"))
        return tagged[-1] if tagged else None

    def _load_resume_snapshot_state(self, csv_path: Path) -> Optional[Dict[str, Any]]:
        snap_path = self._find_resume_snapshot_file(csv_path)
        if snap_path is None:
            return None
        with open(snap_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _resume_plot_metric_value(self, row: Dict[str, Any], fallback_label: Optional[str] = None) -> tuple[str, float]:
        metric_label = str(row.get("ObjectiveLabel", "") or fallback_label or row.get("ScoreDownstreamICT", "Metric"))
        objective_type = str(row.get("ObjectiveType", DEVELOPER_OBJECTIVE_ICT)).upper()
        try:
            metric_value = float(row.get("ObjectiveValue", float("nan")))
        except Exception:
            metric_value = float("nan")
        if objective_type == DEVELOPER_OBJECTIVE_BPM and not np.isfinite(metric_value):
            try:
                metric_value = float(row.get("BPMMetric", float("nan")))
            except Exception:
                metric_value = float("nan")
        if np.isfinite(metric_value):
            return metric_label, metric_value

        downstream_name = str(row.get("ScoreDownstreamICT", metric_label)).upper()
        if downstream_name == "L0":
            downstream_name = "LN0"
        downstream_column = "ICT_L0" if downstream_name == "LN0" else f"ICT_{downstream_name}"
        try:
            metric_value = float(row.get(downstream_column, float("nan")))
        except Exception:
            metric_value = float("nan")
        if not metric_label:
            metric_label = downstream_name or "Metric"
        return metric_label, metric_value

    def _prime_eval_plot_from_resume(self, csv_path: Path, *, profile: str, metric_label: Optional[str] = None) -> None:
        if not csv_path.exists():
            return

        resume_rows: List[Dict[str, Any]] = []
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for raw in reader:
                if not raw:
                    continue
                label, metric_value = self._resume_plot_metric_value(raw, metric_label)
                try:
                    ttot = float(raw.get("Ttot", float("nan")))
                except Exception:
                    ttot = float("nan")
                note = str(raw.get("Note", ""))
                resume_rows.append({
                    "ttot": ttot,
                    "metric": metric_value,
                    "metric_label": label,
                    "measurement_ok": "ICT_INVALID_ZERO" not in note,
                    "evaluation_phase": evaluation_phase(note),
                })

        if not resume_rows:
            return

        kept_rows = resume_rows[:-1]
        discarded_rows = resume_rows[-1:]
        context = str(profile or RUN_PROFILE_MAIN).upper()
        state = self._plot_state.setdefault(
            context,
            {
                "eval_index": [],
                "ttot": [],
                "metric": [],
                "metric_label": "DR",
                "measurement_ok": [],
                "discarded_eval_index": [],
                "discarded_ttot": [],
                "discarded_metric": [],
            },
        )
        state["eval_index"] = [idx for idx in range(1, len(kept_rows) + 1)]
        state["evaluation_phase"] = [row["evaluation_phase"] for row in kept_rows]
        state["ttot"] = [float(row["ttot"]) for row in kept_rows]
        state["metric"] = [float(row["metric"]) for row in kept_rows]
        state["measurement_ok"] = [bool(row["measurement_ok"]) for row in kept_rows]
        if metric_label is not None:
            state["metric_label"] = str(metric_label)
        elif resume_rows:
            state["metric_label"] = str(resume_rows[-1].get("metric_label", state.get("metric_label", "DR")))

        if discarded_rows:
            discarded = discarded_rows[0]
            state["discarded_eval_index"] = [len(kept_rows) + 1]
            state["discarded_ttot"] = [float(discarded.get("ttot", float("nan")))]
            state["discarded_metric"] = [float(discarded.get("metric", float("nan")))]
            self._append_log_text(
                f"[RESUME] Dropped last plot point from {csv_path.name}; it will be re-measured.",
                profile=context,
            )
        else:
            state["discarded_eval_index"] = []
            state["discarded_ttot"] = []
            state["discarded_metric"] = []

        self._refresh_eval_plot(context)

    def _apply_config_to_ui(self, payload: Dict[str, Any]):
        self.dr_orbit_mode.setCurrentIndex(max(0, self.dr_orbit_mode.findData(payload.get("dr_orbit_mode", 2))))
        if "objective_type" in payload:
            self.objective_box.setCurrentText({DEVELOPER_OBJECTIVE_INTENSITY: "BPM intensity",
                                              DEVELOPER_OBJECTIVE_BPM: "BPM sumsq"}.get(payload["objective_type"], "ICT"))
        self.dev_intensity_aggregation.setCurrentIndex(max(0, self.dev_intensity_aggregation.findData(
            payload.get("intensity_aggregation", "raw_mean"))))
        self.dr_ratio_turn.setValue(int(payload.get("dr_ratio_turn", 10)))
        self.dr_current_weight.setValue(float(payload.get("dr_current_weight", 1.)))
        self.dr_ratio_floor.setValue(float(payload.get("dr_ratio_floor", 1e-12)))
        self.correctors_l8.setChecked(bool(payload.get("correctors_l8", False)))
        self.l8_phase_half.setValue(float(payload.get("l8_phase_half", PHASE_HALF_RANGE_DEG)))
        self.l8_phase_step.setValue(float(payload.get("l8_phase_step", PHASE_STEP_DEG)))
        self.dr_turn_enabled.setChecked(bool(payload.get("dr_turn_scan", False)))
        self.dr_turn_start.setValue(int(payload.get("dr_turn_start", 1)))
        self.dr_turn_end.setValue(int(payload.get("dr_turn_end", 10)))
        self.dr_turn_aggregation.setCurrentIndex(max(0, self.dr_turn_aggregation.findData(
            payload.get("dr_turn_aggregation", "mean"))))
        run_profile = str(payload.get("run_profile", RUN_PROFILE_MAIN)).upper()
        mode = str(payload.get("mode", self.mode_box.currentText()))
        if mode not in ("SEQUENTIAL", "GROUP_BO"):
            mode = "SEQUENTIAL"
        self.mode_box.setCurrentText(mode)
        self.seq_method_box.setCurrentText(str(payload.get("seq_method", self.seq_method_box.currentText())))
        self.chk_gun_sol_l0_phase.setChecked(
            bool(payload.get("gun_sol_l0_phase", self.chk_gun_sol_l0_phase.isChecked())))
        self.chk_kly_phase.setChecked(bool(payload.get("kly_phase", self.chk_kly_phase.isChecked())))
        self.chk_kly_group1.setChecked(bool(payload.get("grp_L1_4", self.chk_kly_group1.isChecked())))
        self.chk_kly_group2.setChecked(bool(payload.get("grp_L5_8", self.chk_kly_group2.isChecked())))
        self.chk_timing.setChecked(bool(payload.get("timing", self.chk_timing.isChecked())))
        self.chk_qa.setChecked(bool(payload.get("qa", self.chk_qa.isChecked())))
        self.chk_qm.setChecked(bool(payload.get("qm", self.chk_qm.isChecked())))
        self.sp_settle_sec.setValue(float(payload.get("settle_sec", self.sp_settle_sec.value())))
        # Old saved configurations predate this setting; they must retain the
        # historical effective GP behavior rather than inheriting the GUI's
        # learned-preset default.
        hp_mode = str(payload.get("bo_hyperparameter_mode", "legacy_fixed"))
        if self.bo_hyperparameter_mode_box.findText(hp_mode) >= 0:
            self.bo_hyperparameter_mode_box.setCurrentText(hp_mode)
        self.sp_score_w_ttot.setValue(float(payload.get("score_w_ttot", self.sp_score_w_ttot.value())))
        self.sp_score_w_downstream.setValue(
            float(payload.get("score_w_downstream", self.sp_score_w_downstream.value())))
        downstream = str(payload.get("downstream_ict", self.cb_downstream_ict.currentText()))
        if self.cb_downstream_ict.findText(downstream) >= 0:
            self.cb_downstream_ict.setCurrentText(downstream)
        self.sp_gun_phase_half.setValue(float(payload.get("gun_phase_half_range_deg", self.sp_gun_phase_half.value())))
        self.sp_gun_phase_step.setValue(float(payload.get("gun_phase_step_deg", self.sp_gun_phase_step.value())))
        self.sp_sol_min.setValue(float(payload.get("solenoide_min_a", self.sp_sol_min.value())))
        self.sp_sol_max.setValue(float(payload.get("solenoide_max_a", self.sp_sol_max.value())))
        self.sp_sol_step.setValue(float(payload.get("solenoide_step_a", self.sp_sol_step.value())))
        self.sp_l0_phase_half.setValue(float(payload.get("l0_phase_half_range_deg", self.sp_l0_phase_half.value())))
        self.sp_l0_phase_step.setValue(float(payload.get("l0_phase_step_deg", self.sp_l0_phase_step.value())))
        self.sp_phase_half.setValue(float(payload.get("phase_half_range_deg", self.sp_phase_half.value())))
        self.sp_phase_step.setValue(float(payload.get("phase_step_deg", self.sp_phase_step.value())))
        self.sp_qa_half.setValue(float(payload.get("qa_half_range_a", self.sp_qa_half.value())))
        self.sp_qa_step.setValue(float(payload.get("qa_step_a", self.sp_qa_step.value())))
        self.sp_qm_half.setValue(float(payload.get("qm_half_range_a", self.sp_qm_half.value())))
        self.sp_qm_step.setValue(float(payload.get("qm_step_a", self.sp_qm_step.value())))
        self.sp_timing_step.setValue(float(payload.get("timing_step_ns", self.sp_timing_step.value())))
        self.sp_timing_half_steps.setValue(int(payload.get("timing_half_steps", self.sp_timing_half_steps.value())))
        gp_mode = payload.get("corrector_gp_mode", "fixed")
        gp_index = self.corrector_gp_mode_box.findData(gp_mode)
        self.corrector_gp_mode_box.setCurrentIndex(gp_index if gp_index >= 0 else 1)
        self.corrector_gp_refit_spin.setValue(int(payload.get("corrector_gp_refit_interval", 5)))
        preset = str(payload.get("gbo_budget_preset", "standard"))
        if preset not in GROUP_BO_BUDGET_PRESETS:
            preset = "standard"
        self.group_bo_budget_preset_box.blockSignals(True)
        self.group_bo_budget_preset_box.setCurrentIndex(self.group_bo_budget_preset_box.findData(preset))
        self.group_bo_budget_preset_box.blockSignals(False)
        self.corrector_budget_auto.setChecked(bool(payload.get(
            "gbo_correctors_auto", "gbo_max_evals_correctors" not in payload)))
        for spec in GROUP_BO_MAX_EVAL_SPECS:
            box = self.group_bo_max_eval_spinboxes.get(str(spec["config_id"]))
            if box is None:
                continue
            key = _group_bo_max_evals_config_key(str(spec["config_id"]))
            box.setValue(int(payload.get(key, _recommended_group_bo_max_evals(int(spec["dim"]), preset))))
        self._refresh_group_bo_budget_labels()
        self._update_mode_ui()

        if run_profile == RUN_PROFILE_DEVELOPER:
            self.tabs.setCurrentWidget(self.developer_tab)
            self.dev_mode_box.setCurrentText(mode)
            self.dev_seq_method_box.setCurrentText(
                str(payload.get("seq_method", self.dev_seq_method_box.currentText())))
            objective_type = str(payload.get("objective_type", DEVELOPER_OBJECTIVE_ICT)).upper()
            self.dev_objective_box.setCurrentText("BPM sumsq" if objective_type == DEVELOPER_OBJECTIVE_BPM else "ICT")
            self.dev_sp_score_w_ttot.setValue(float(payload.get("score_w_ttot", self.dev_sp_score_w_ttot.value())))
            self.dev_sp_score_w_downstream.setValue(
                float(payload.get("score_w_downstream", self.dev_sp_score_w_downstream.value())))
            dev_downstream = str(payload.get("downstream_ict", self.dev_downstream_ict_box.currentText()))
            if self.dev_downstream_ict_box.findText(dev_downstream) >= 0:
                self.dev_downstream_ict_box.setCurrentText(dev_downstream)
            self.dev_bpm_plane_box.setCurrentText(
                str(payload.get("objective_bpm_plane", self.dev_bpm_plane_box.currentText())).upper())

            selected_bpm_names = {str(name) for name in list(payload.get("objective_bpm_names", []))}
            self.dev_bpm_table.blockSignals(True)
            for row in range(self.dev_bpm_table.rowCount()):
                name = self.dev_bpm_table.item(row, 1).text()
                self.dev_bpm_table.item(row, 0).setCheckState(
                    Qt.CheckState.Checked if name in selected_bpm_names else Qt.CheckState.Unchecked
                )
            self.dev_bpm_table.blockSignals(False)

            selected_actuators = {
                str(item.get("name", "")): dict(item)
                for item in list(payload.get("developer_actuators", []))
                if isinstance(item, dict)
            }
            self.dev_actuator_table.blockSignals(True)
            for row in range(self.dev_actuator_table.rowCount()):
                name = self.dev_actuator_table.item(row, 1).text()
                cfg = selected_actuators.get(name)
                self.dev_actuator_table.item(row, 0).setCheckState(
                    Qt.CheckState.Checked if cfg is not None else Qt.CheckState.Unchecked
                )
                if cfg is not None:
                    boxes = self.dev_actuator_spinboxes.get(name, {})
                    boxes.get("half").setValue(float(cfg.get("half_range", boxes.get("half").value())))
                    boxes.get("step").setValue(float(cfg.get("step", boxes.get("step").value())))
            self.dev_actuator_table.blockSignals(False)
            self._update_developer_mode_ui()
            self._update_developer_objective_ui()
            self._apply_developer_actuator_filters()
            self._apply_developer_bpm_filters()
            self._refresh_developer_selection_counts()

    def _browse_resume_file(self):
        current = self.resume_file_edit.text().strip() or str(self.save_path.resolve())
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select interrupted Linac log CSV",
            current,
            "CSV (*.csv);;All files (*)",
        )
        if not path:
            return
        self.resume_file_edit.setText(path)
        self.resume_snapshot_state = None
        try:
            snapshot = self._load_resume_snapshot_state(Path(path))
            if snapshot:
                self.resume_snapshot_state = snapshot
                cfg_payload = dict(snapshot.get("config", {}) or {})
                loaded_profile = str(
                    cfg_payload.get("run_profile", RUN_PROFILE_MAIN)).upper() if cfg_payload else RUN_PROFILE_MAIN
                if cfg_payload:
                    self._apply_config_to_ui(cfg_payload)
                self.append_log(f"Loaded resume snapshot from {self._find_resume_snapshot_file(Path(path))}")
                self._set_status(f"Status: resume file loaded -> {path}", state="info", profile=loaded_profile)
            else:
                self._set_status(f"Status: resume file selected -> {path}", state="info", profile=self._run_profile)
        except Exception as exc:
            self.append_log(f"Resume snapshot load failed: {exc}")
            self._set_status(f"Status: resume file selected -> {path}", state="info", profile=self._run_profile)

    def _clear_resume_file(self):
        self.resume_file_edit.clear()
        self.resume_snapshot_state = None

    def _update_result_label(self, text: Optional[str] = None, profile: Optional[str] = None):
        context = str(profile or self._run_profile or RUN_PROFILE_MAIN).upper()
        target = self._result_labels.get(context)
        if target is None:
            return
        if text is not None:
            target.setText(str(text))
            return
        csv_path = self.current_measurements_csv_by_profile.get(context)
        if csv_path is None or (not csv_path.exists()):
            target.setText("Result: -")
            return
        best_row: Optional[Dict[str, Any]] = None
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    score = float(row.get("Score", "nan"))
                except Exception:
                    continue
                if not np.isfinite(score):
                    continue
                if best_row is None or score > float(best_row.get("Score", float("-inf"))):
                    best_row = dict(row)
        if best_row is None:
            target.setText(f"Result: no valid rows yet | file={csv_path}")
            return
        group = str(best_row.get("Group", "-"))
        device = str(best_row.get("DeviceLabel", "-"))
        ttot = float(best_row.get("Ttot", float("nan")))
        score = float(best_row.get("Score", float("nan")))
        objective_label = str(best_row.get("ObjectiveLabel", best_row.get("ScoreDownstreamICT", "Metric")))
        try:
            objective_value = float(best_row.get("ObjectiveValue", float("nan")))
        except Exception:
            objective_value = float("nan")
        downstream_name = str(best_row.get("ScoreDownstreamICT", self._plot_state[context].get("metric_label", "DR")))
        downstream_column = "ICT_L0" if downstream_name == "LN0" else f"ICT_{downstream_name}"
        try:
            downstream_value = float(best_row.get(downstream_column, float("nan")))
        except Exception:
            downstream_value = float("nan")
        if not np.isfinite(objective_value):
            objective_value = downstream_value
        target.setText(
            f"Result: best score={score:.6g}, Ttot={ttot:.6f}, {objective_label}={objective_value:.6g}, "
            f"group={group}, device={device}, file={csv_path}"
        )

    def _on_reset_to_initial(self):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.warning(self, "Busy", "Stop the optimizer before resetting to the initial state.")
            return
        if not self.current_machine_origin:
            QMessageBox.information(
                self,
                "No Initial State",
                "No start-time machine state is stored yet. Press START once before using reset.",
            )
            return
        try:
            self._apply_machine_origin(self.current_machine_origin)
        except Exception as exc:
            self.append_log(f"Reset to initial failed: {exc}")
            QMessageBox.warning(self, "Reset failed", str(exc))
            return
        self._refresh_current_values()
        profile = RUN_PROFILE_DEVELOPER if self.tabs.currentWidget() is self.developer_tab else RUN_PROFILE_MAIN
        self._set_status("Status: restored to initial machine state", state="success", profile=profile)
        self.append_log(
            f"Reset to initial completed: restored {len(self.current_machine_origin.get('initial_setpoints', {}))} channels")

    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Save Directory", str(self.save_path))
        if folder:
            self.save_path = Path(folder)
            self.lbl_path.setText(str(self.save_path))

    def _set_main_run_controls_enabled(self, enabled: bool):
        for w in [
            self.chk_gun_sol_l0_phase, self.chk_kly_phase, self.chk_kly_group1, self.chk_kly_group2,
            self.chk_timing, self.chk_qa, self.chk_qm,
            self.chk_correctors, self.objective_box,
            self.mode_box, self.seq_method_box, self.btn_browse, self.config_tab,
            self.resume_file_edit, self.resume_file_browse_btn, self.resume_file_clear_btn
        ]:
            w.setEnabled(enabled)

    def _set_developer_run_controls_enabled(self, enabled: bool):
        for w in [
            self.dev_mode_box, self.dev_seq_method_box, self.dev_objective_box,
            self.dev_actuator_search, self.dev_actuator_region_box, self.dev_actuator_plane_box,
            self.dev_actuator_selected_only, self.dev_actuator_select_visible_btn,
            self.dev_actuator_clear_visible_btn, self.dev_actuator_apply_defaults_btn,
            self.dev_h_half, self.dev_h_step, self.dev_v_half, self.dev_v_step,
            self.dev_bpm_search, self.dev_bpm_region_box, self.dev_bpm_selected_only,
            self.dev_bpm_select_visible_btn, self.dev_bpm_clear_visible_btn,
            self.dev_bpm_plane_box, self.dev_sp_score_w_ttot, self.dev_sp_score_w_downstream,
            self.dev_intensity_aggregation, self.dr_turn_group, self.dr_ratio_turn, self.dr_current_weight, self.dr_ratio_floor,
            self.correctors_l8, self.l8_phase_half, self.l8_phase_step,
            self.dev_refresh_btn,
            self.dev_downstream_ict_box, self.dev_bpm_table, self.dev_actuator_table,
        ]:
            w.setEnabled(enabled)
        for boxes in self.dev_actuator_spinboxes.values():
            boxes["half"].setEnabled(enabled)
            boxes["step"].setEnabled(enabled)

    def _prepare_developer_statuses_for_run(self):
        self._reset_developer_actuator_statuses()
        for spec in self._selected_developer_actuators():
            self._set_developer_actuator_status(spec["name"], "waiting")

    def _finalize_developer_statuses_after_run(self):
        for spec in self._selected_developer_actuators():
            row = self.dev_actuator_row_map.get(spec["name"])
            if row is None:
                continue
            current = self.dev_actuator_table.item(row, 7).text().strip().upper()
            if current in ("WAITING", "RUNNING"):
                self._set_developer_actuator_status(spec["name"], "idle")

    def _checked_corrector_ranges(self, actuators):
        from epics import PV
        limits, summary = {}, []
        if not actuators:
            return limits, summary
        interface = self._ensure_interface()
        for spec in actuators:
            pv = spec["pv_write"]
            unit = spec.get("unit", "A")
            if pv in ("CM8L:phaseWrite", "EVE_LINAC:OUT0:SetData"):
                # Phase/timing use the user-configured finite, centered search
                # range as the command guard, without requiring IOC bounds.
                lo, hi = -float("inf"), float("inf")
            else:
                ctrl = PV(pv).get_ctrlvars(timeout=1.0) or {}
                lo = float(ctrl.get("lower_ctrl_limit", float("nan")))
                hi = float(ctrl.get("upper_ctrl_limit", float("nan")))
                if not np.isfinite([lo, hi]).all() or lo >= hi:
                    raise ValueError(f"Cannot verify EPICS limits: {pv} [{unit}]. Check IOC configuration before running.")
            if unit == "A":
                lo, hi = max(lo, STEER_CURRENT_MIN_A), min(hi, STEER_CURRENT_MAX_A)
            current = float(interface.read_current(pv, spec["pv_read"]))
            if not np.isfinite(current) or not lo <= current <= hi or lo >= hi:
                raise ValueError(f"Unsafe value/range: {pv}, {current} {unit}, limits [{lo}, {hi}] {unit}")
            if not np.isfinite([spec["step"], spec["half_range"]]).all() or not 0 < spec["step"] <= 2 * spec["half_range"]:
                raise ValueError(f"Invalid step/range: {pv}")
            lower, upper = max(lo, current - spec["half_range"]), min(hi, current + spec["half_range"])
            if not np.isfinite([lower, upper]).all() or lower >= upper:
                raise ValueError(f"Invalid finite search range: {pv}")
            limits[pv] = [lower, upper]
            summary.append(f"{spec['name']}: {current:g} {unit} -> [{lower:g}, {upper:g}] {unit}; step {spec['step']:g} {unit}")
        return limits, summary

    def start_developer_optimization(self):
        mode = str(self.dev_mode_box.currentText())
        objective_key = {"BPM sumsq": DEVELOPER_OBJECTIVE_BPM, "BPM intensity": DEVELOPER_OBJECTIVE_INTENSITY}.get(self.dev_objective_box.currentText(), DEVELOPER_OBJECTIVE_ICT)
        actuators = self._selected_developer_actuators()
        bpm_names = self._selected_developer_bpms()
        resume_path_text = self.resume_file_edit.text().strip()
        if objective_key == DEVELOPER_OBJECTIVE_INTENSITY and resume_path_text:
            QMessageBox.warning(self, "Resume", "Clear Resume CSV for an intensity run; a new fixed baseline is required.")
            return
        if resume_path_text:
            resume_path = Path(resume_path_text).expanduser()
            if not resume_path.exists():
                QMessageBox.warning(self, "Resume file", f"Resume CSV was not found:\n{resume_path}")
                return
        if not actuators:
            QMessageBox.warning(self, "Warning", "Please select at least one steer in Developer tab.")
            return
        if objective_key in (DEVELOPER_OBJECTIVE_BPM, DEVELOPER_OBJECTIVE_INTENSITY) and not bpm_names:
            QMessageBox.warning(self, "Warning", "Please select at least one BPM for the BPM objective.")
            return

        restore_pvs = sorted({str(spec["pv_write"]) for spec in actuators})
        config = {
            "run_profile": RUN_PROFILE_DEVELOPER,
            **self._dr_turn_config_from_ui(),
            **self._transmission_config_from_ui(),
            "mode": mode,
            "corrector_gp_mode": self.corrector_gp_mode_box.currentData(),
            "corrector_gp_refit_interval": self.corrector_gp_refit_spin.value(),
            "gbo_budget_preset": self.group_bo_budget_preset_box.currentData(),
            "gbo_correctors_auto": self.corrector_budget_auto.isChecked(),
            "gbo_max_evals_correctors": self.group_bo_max_eval_spinboxes["correctors"].value(),
            "seq_method": str(self.dev_seq_method_box.currentText()),
            "objective_type": objective_key,
            "intensity_aggregation": self.dev_intensity_aggregation.currentData(),
            "intensity_source": ", ".join(intensity_sources(bpm_names)),
            "objective_bpm_plane": str(self.dev_bpm_plane_box.currentText()),
            "objective_bpm_names": bpm_names,
            "developer_actuators": actuators,
            "score_w_ttot": float(self.dev_sp_score_w_ttot.value()),
            "score_w_downstream": float(self.dev_sp_score_w_downstream.value()),
            "downstream_ict": str(self.dev_downstream_ict_box.currentText()),
            "settle_sec": float(self.sp_settle_sec.value()),
            "bo_hyperparameter_mode": str(self.bo_hyperparameter_mode_box.currentText()),
            "bo_length_scale_n_step_floor": 2.0,
            "restore_pvs": restore_pvs,
            "reuse_initial_eval": True,
            "resume_csv_path": str(Path(resume_path_text).expanduser().resolve()) if resume_path_text else "",
        }
        try:
            config["actuator_limits"], summary = self._checked_corrector_ranges(actuators)
            message = (f"Objective: {self.dev_objective_box.currentText()}\n"
                       f"Correctors: {len(actuators)}; BPMs: {len(bpm_names)}\n"
                       + ("BPMs: " + ", ".join(bpm_names) + "\n" if bpm_names else "")
                       + (f"BPM source: {config['intensity_source']}\nAggregate: {self.dev_intensity_aggregation.currentText()}\n" if objective_key == DEVELOPER_OBJECTIVE_INTENSITY else "")
                       + (dr_turn_label(config) + "; selector PV stays at the last commanded turn.\n" if objective_key == DEVELOPER_OBJECTIVE_INTENSITY and dr_turn_label(config) and any(n in DR_BPM_NAMES for n in bpm_names) else "")
                       + "\n".join(summary) + "\n\nStart optimization? This will change selected corrector currents.")
            if QMessageBox.question(self, "Confirm Developer run", message,
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            self.current_machine_origin = self._capture_machine_origin(config)
        except Exception as exc:
            QMessageBox.critical(self, "Machine readback error", str(exc))
            return

        self._run_profile = RUN_PROFILE_DEVELOPER
        self._run_mode = mode
        self._run_failed = False
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.dev_btn_start.setEnabled(False)
        self.dev_btn_stop.setEnabled(True)
        self.reset_initial_btn.setEnabled(False)
        self.dev_reset_initial_btn.setEnabled(False)
        self.dev_txt_log.clear()
        self._update_result_label("Result: running...", profile=RUN_PROFILE_DEVELOPER)
        self._prepare_developer_statuses_for_run()
        metric_label = str(
            self.dev_downstream_ict_box.currentText()) if objective_key == DEVELOPER_OBJECTIVE_ICT else f"BPM {self.dev_bpm_plane_box.currentText()} sumsq"
        if objective_key == DEVELOPER_OBJECTIVE_INTENSITY:
            metric_label = "BPM intensity " + config["intensity_aggregation"]
        self._reset_eval_plot(metric_label, profile=RUN_PROFILE_DEVELOPER)
        if resume_path_text:
            self._prime_eval_plot_from_resume(
                Path(resume_path_text).expanduser(),
                profile=RUN_PROFILE_DEVELOPER,
                metric_label=metric_label,
            )
        self._set_status(f"Status: RUNNING {mode}", state="running", profile=RUN_PROFILE_DEVELOPER)
        self._set_main_run_controls_enabled(False)
        self._set_developer_run_controls_enabled(False)
        self.resume_file_edit.setEnabled(False)
        self.resume_file_browse_btn.setEnabled(False)
        self.resume_file_clear_btn.setEnabled(False)

        self.worker = OptimizationWorker(config, self.save_path)
        self.current_measurements_csv_by_profile[RUN_PROFILE_DEVELOPER] = Path(self.worker.csv_path)
        self.worker.log_signal.connect(self.append_log)
        self.worker.progress_signal.connect(self._on_worker_progress)
        self.worker.finished_signal.connect(self.optimization_finished)
        self.btn_end.setEnabled(True)
        self.worker.start()

    def start_optimization(self):
        mode = str(self.mode_box.currentText())
        objective = {"BPM intensity": DEVELOPER_OBJECTIVE_INTENSITY,
                     "BPM sumsq": DEVELOPER_OBJECTIVE_BPM}.get(self.objective_box.currentText(), DEVELOPER_OBJECTIVE_ICT)
        actuators = self._selected_developer_actuators() if self.chk_correctors.isChecked() else []
        bpms = self._selected_developer_bpms() if objective != DEVELOPER_OBJECTIVE_ICT else []
        if self.chk_correctors.isChecked() and not actuators:
            QMessageBox.warning(self, "Correctors", "Select correctors in Config > Correctors / BPMs.")
            return
        if self.chk_correctors.isChecked() and self.correctors_l8.isChecked():
            if mode != "GROUP_BO":
                QMessageBox.warning(self, "Correctors & L8", "Select GROUP_BO for simultaneous optimization.")
                return
            actuators += self._l8_actuators()
        if objective == DEVELOPER_OBJECTIVE_INTENSITY and self.dev_intensity_aggregation.currentData() in ("turn_ratio", "turn_composite"):
            if not bpms or any(name not in DR_BPM_NAMES for name in bpms):
                QMessageBox.warning(self, "DR transmission", "Select only DR BPMs for turn transmission objectives.")
                return
        if objective != DEVELOPER_OBJECTIVE_ICT and not bpms:
            QMessageBox.warning(self, "BPMs", "Select target BPMs in Config > Correctors / BPMs.")
            return
        resume_path_text = self.resume_file_edit.text().strip()
        if resume_path_text and (objective != DEVELOPER_OBJECTIVE_ICT or actuators):
            QMessageBox.warning(self, "Resume", "Clear Resume CSV for BPM objectives or corrector runs; use a fresh baseline and verified limits.")
            return
        if resume_path_text:
            resume_path = Path(resume_path_text).expanduser()
            if not resume_path.exists():
                QMessageBox.warning(self, "Resume file", f"Resume CSV was not found:\n{resume_path}")
                return
        config = {
            "mode": mode,
            **self._dr_turn_config_from_ui(),
            **self._transmission_config_from_ui(),
            "correctors_l8": self.correctors_l8.isChecked() and self.chk_correctors.isChecked(),
            "l8_phase_half": self.l8_phase_half.value(), "l8_phase_step": self.l8_phase_step.value(),
            "settle_sec": float(self.sp_settle_sec.value()),
            "bo_hyperparameter_mode": str(self.bo_hyperparameter_mode_box.currentText()),
            "bo_length_scale_n_step_floor": 2.0,
            "score_w_ttot": float(getattr(self, "sp_score_w_ttot").value()),
            "score_w_downstream": float(getattr(self, "sp_score_w_downstream").value()),
            "downstream_ict": str(getattr(self, "cb_downstream_ict").currentText()),
            "lbo_iters": 4,
            "lbo_line_samples": 11,
            "group_bo_passes": 2,
            "group_bo_score_eps": 0.0,
            "corrector_gp_mode": self.corrector_gp_mode_box.currentData(),
            "corrector_gp_refit_interval": self.corrector_gp_refit_spin.value(),
            "gbo_budget_preset": self.group_bo_budget_preset_box.currentData(),
            "gbo_correctors_auto": self.corrector_budget_auto.isChecked(),
            "reuse_initial_eval": True,
            "gun_sol_l0_phase": self.chk_gun_sol_l0_phase.isChecked(),
            "kly_phase": self.chk_kly_phase.isChecked(),
            "timing": self.chk_timing.isChecked() and not (self.correctors_l8.isChecked() and self.chk_correctors.isChecked()),
            "qa": self.chk_qa.isChecked(),
            "qm": self.chk_qm.isChecked(),
            "correctors": self.chk_correctors.isChecked(),
            "developer_actuators": actuators,
            "objective_bpm_names": bpms,
            "objective_bpm_plane": self.dev_bpm_plane_box.currentText(),
            "intensity_aggregation": self.dev_intensity_aggregation.currentData(),
            "intensity_source": ", ".join(intensity_sources(bpms)),
            "grp_L1_4": self.chk_kly_group1.isChecked(),
            "grp_L5_8": self.chk_kly_group2.isChecked(),
            # Sequential method + scan settings
            "seq_method": str(self.seq_method_box.currentText()),
            "gun_phase_half_range_deg": float(getattr(self, "sp_gun_phase_half").value()),
            "gun_phase_step_deg": float(getattr(self, "sp_gun_phase_step").value()),
            "solenoide_min_a": float(getattr(self, "sp_sol_min").value()),
            "solenoide_max_a": float(getattr(self, "sp_sol_max").value()),
            "solenoide_step_a": float(getattr(self, "sp_sol_step").value()),
            "l0_phase_half_range_deg": float(getattr(self, "sp_l0_phase_half").value()),
            "l0_phase_step_deg": float(getattr(self, "sp_l0_phase_step").value()),
            "phase_half_range_deg": float(getattr(self, "sp_phase_half").value()),
            "phase_step_deg": float(getattr(self, "sp_phase_step").value()),
            "qa_half_range_a": float(getattr(self, "sp_qa_half").value()),
            "qa_step_a": float(getattr(self, "sp_qa_step").value()),
            "qm_half_range_a": float(getattr(self, "sp_qm_half").value()),
            "qm_step_a": float(getattr(self, "sp_qm_step").value()),
            "timing_step_ns": float(getattr(self, "sp_timing_step").value()),
            "timing_half_steps": int(getattr(self, "sp_timing_half_steps").value()),
            "run_profile": RUN_PROFILE_MAIN,
            "objective_type": objective,
            "restore_pvs": list(RESTORE_PVS) + [spec["pv_write"] for spec in actuators],
            "resume_csv_path": str(Path(resume_path_text).expanduser().resolve()) if resume_path_text else "",
        }
        for spec in GROUP_BO_MAX_EVAL_SPECS:
            box = self.group_bo_max_eval_spinboxes.get(str(spec["config_id"]))
            if box is None:
                continue
            config[_group_bo_max_evals_config_key(str(spec["config_id"]))] = int(box.value())

        if not (config["gun_sol_l0_phase"] or config["kly_phase"] or config["timing"] or config["qa"] or config["qm"] or config["correctors"]):
            QMessageBox.warning(self, "Warning", "Please select at least one target.")
            return
        if config["mode"] == "GROUP_BO" and config["kly_phase"] and not (config["grp_L1_4"] or config["grp_L5_8"]):
            QMessageBox.warning(self, "Warning", "Please select Group 1 and/or Group 2 for Klystron phase.")
            return
        try:
            config["actuator_limits"], summary = self._checked_corrector_ranges(actuators)
            if objective != DEVELOPER_OBJECTIVE_ICT or actuators:
                groups = [label for key, label in [("gun_sol_l0_phase", "GUN / Solenoid / L0"),
                          ("kly_phase", "Klystron phase"), ("timing", "Timing"), ("qa", "QA"), ("qm", "QM"),
                          ("correctors", "Selected LINAC/BT correctors")] if config[key]]
                message = (f"Objective: {self.objective_box.currentText()}\nTargets: {', '.join(groups)}\n"
                           + (f"BPMs: {', '.join(bpms)}\n" if bpms else "")
                           + (f"Source: {config['intensity_source']}; aggregate: {self.dev_intensity_aggregation.currentText()}\n"
                              if objective == DEVELOPER_OBJECTIVE_INTENSITY else "")
                           + (dr_turn_label(config) + f"; w={config['dr_current_weight']:g}; selector PV stays at the last commanded turn.\n" if objective == DEVELOPER_OBJECTIVE_INTENSITY and dr_turn_label(config) and any(n in DR_BPM_NAMES for n in bpms) else "")
                           + "\n".join(summary) + "\n\nStart? Selected machine settings will change.")
                if QMessageBox.question(self, "Confirm optimization", message,
                                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                        QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                    return
            self.current_machine_origin = self._capture_machine_origin(config)
        except Exception as exc:
            QMessageBox.critical(self, "Machine readback error", str(exc))
            return

        # UI state
        self._run_profile = RUN_PROFILE_MAIN
        self._run_mode = mode
        self._run_failed = False
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.dev_btn_start.setEnabled(False)
        self.dev_btn_stop.setEnabled(True)
        self.reset_initial_btn.setEnabled(False)
        self.dev_reset_initial_btn.setEnabled(False)
        self.txt_log.clear()
        self._update_result_label("Result: running...", profile=RUN_PROFILE_MAIN)
        self._prepare_target_states_for_run(mode)
        metric_label = ("BPM intensity " + config["intensity_aggregation"] if objective == DEVELOPER_OBJECTIVE_INTENSITY else
                        (f"BPM {config['objective_bpm_plane']} sumsq" if objective == DEVELOPER_OBJECTIVE_BPM else config["downstream_ict"]))
        self._reset_eval_plot(metric_label, profile=RUN_PROFILE_MAIN)
        if resume_path_text:
            self._prime_eval_plot_from_resume(
                Path(resume_path_text).expanduser(),
                profile=RUN_PROFILE_MAIN,
                metric_label=config["downstream_ict"],
            )
        self._refresh_current_values()
        self._set_status(f"Status: RUNNING {mode}", state="running", profile=RUN_PROFILE_MAIN)
        self._set_main_run_controls_enabled(False)
        self._set_developer_run_controls_enabled(False)
        self.resume_file_edit.setEnabled(False)
        self.resume_file_browse_btn.setEnabled(False)
        self.resume_file_clear_btn.setEnabled(False)

        self.worker = OptimizationWorker(config, self.save_path)
        self.current_measurements_csv_by_profile[RUN_PROFILE_MAIN] = Path(self.worker.csv_path)
        self.worker.log_signal.connect(self.append_log)
        self.worker.progress_signal.connect(self._on_worker_progress)
        self.worker.finished_signal.connect(self.optimization_finished)
        self.btn_end.setEnabled(True)
        self.worker.start()

    def stop_optimization(self):
        if self.worker and self.worker.isRunning():
            was_paused = self.worker.is_paused
            if was_paused:
                self.worker.resume()
            else:
                self.worker.request_pause()
            self.btn_stop.setEnabled(False)
            self.dev_btn_stop.setEnabled(False)
            self._set_status("Status: resuming" if was_paused else "Status: pause requested — completing evaluation",
                             state="paused", profile=self._run_profile)

    def cancel_optimization(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.btn_stop.setEnabled(False)
            self.dev_btn_stop.setEnabled(False)
            self.btn_end.setEnabled(False)
            self._set_status("Status: stopping", state="paused", profile=self._run_profile)

    def closeEvent(self, event):  # type: ignore[override]
        timer_was_active = self._current_value_timer.isActive()
        self._current_value_timer.stop()
        if self.worker is not None and self.worker.isRunning():
            self._shutdown_in_progress = True
            self.worker.stop()
            if not self.worker.wait(15000):
                self._shutdown_in_progress = False
                if timer_was_active:
                    self._current_value_timer.start()
                QMessageBox.warning(
                    self,
                    "Worker still running",
                    "Optimization is still stopping. Please wait a few seconds and try closing again.",
                )
                event.ignore()
                return
        event.accept()
        super().closeEvent(event)

    def optimization_finished(self):
        finished_profile = self._run_profile
        stopped = self.worker is not None and not self.worker.is_running
        if self.worker is not None:
            self._last_setpoint_values = {
                str(pv): float(value)
                for pv, value in dict(getattr(self.worker, "_current_pv_values", {}) or {}).items()
            }
        self.worker = None
        self.btn_end.setEnabled(False)
        self.btn_stop.setText("PAUSE")
        self.dev_btn_stop.setText("PAUSE")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.dev_btn_start.setEnabled(True)
        self.dev_btn_stop.setEnabled(False)
        self.reset_initial_btn.setEnabled(True)
        self.dev_reset_initial_btn.setEnabled(True)
        self._set_main_run_controls_enabled(True)
        self._set_developer_run_controls_enabled(True)
        self.resume_file_edit.setEnabled(True)
        self.resume_file_browse_btn.setEnabled(True)
        self.resume_file_clear_btn.setEnabled(True)
        self._update_mode_ui()
        self._update_developer_mode_ui()
        self._update_developer_objective_ui()
        if finished_profile == RUN_PROFILE_MAIN:
            self._finalize_target_states_after_run()
        else:
            self._finalize_developer_statuses_after_run()
        if finished_profile == RUN_PROFILE_MAIN:
            self._refresh_current_values()
        self._append_final_delta_summary(finished_profile)
        self._save_live_plot_snapshot(finished_profile)
        self._update_result_label(profile=finished_profile)
        if self._run_failed:
            self._set_status("Status: FAILED", state="error", profile=finished_profile)
        elif stopped:
            self._set_status("Status: STOPPED", state="paused", profile=finished_profile)
        else:
            self._set_status("Status: DONE", state="success", profile=finished_profile)
        self._run_mode = ""
        self._run_profile = RUN_PROFILE_MAIN
        if self._shutdown_in_progress or stopped:
            return
        if self._run_failed:
            QMessageBox.warning(self, "Finished with Error", "Optimization ended with an error. Please check the log.")
        else:
            QMessageBox.information(self, "Finished", "Optimization process finished.")

    def _append_log_text(self, text: str, profile: Optional[str] = None):
        context = str(profile or RUN_PROFILE_MAIN).upper()
        target = self._log_widgets.get(context, self.txt_log)
        target.append(str(text))
        sb = target.verticalScrollBar()
        sb.setValue(sb.maximum())

    def append_log(self, text: str):
        if self.worker is not None and self.worker.isRunning():
            context = self._run_profile
        else:
            context = RUN_PROFILE_DEVELOPER if self.tabs.currentWidget() is self.developer_tab else RUN_PROFILE_MAIN
        self._append_log_text(text, profile=context)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
