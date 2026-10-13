# -*- coding: utf-8 -*-
"""
mOTR_Opt_GUI.py
Optimization GUI for mOTR-based quadrupole tuning.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
import traceback
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

_GUI_DIR = Path(__file__).resolve().parent
_KNOBS_DIR = _GUI_DIR.parent
_REPO_ROOT = _KNOBS_DIR.parent
for _path in (str(_GUI_DIR), str(_KNOBS_DIR), str(_REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from machine_interfaces_atf2.InterfaceATF2_Ext import InterfaceATF2_Ext


def _load_motr_opt_module():
    module_name = "Knobs.mOTR_Opt"
    if module_name in sys.modules:
        return sys.modules[module_name]
    legacy_name = "mOTR_Opt"
    if legacy_name in sys.modules:
        return sys.modules[legacy_name]

    module_path = _KNOBS_DIR / "mOTR_Opt.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(f"Could not load module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.modules.setdefault(legacy_name, module)
    spec.loader.exec_module(module)
    return module


_MOTR_OPT = _load_motr_opt_module()

Optimizer = _MOTR_OPT.MOTROptimizer
OptimizerConfig = _MOTR_OPT.OptimizerConfig
StopFlag = _MOTR_OPT.StopFlag
EPICSmOTRController = _MOTR_OPT.EPICSmOTRController
now_tag = _MOTR_OPT.now_tag
default_output_base_dir = _MOTR_OPT.default_output_base_dir
recommended_initial_points = _MOTR_OPT.recommended_initial_points
recommended_max_steps = _MOTR_OPT.recommended_max_steps
recommended_candidate_pool = _MOTR_OPT.recommended_candidate_pool
KNOB_ORDER = list(_MOTR_OPT.KNOB_ORDER)
QK_KNOBS = list(_MOTR_OPT.QK_KNOBS)
Q_KNOBS = list(_MOTR_OPT.Q_KNOBS)
DEFAULT_MOTR_IDS = list(_MOTR_OPT.DEFAULT_MOTR_IDS)
DEFAULT_HALF_RANGE_A = float(_MOTR_OPT.DEFAULT_HALF_RANGE_A)
DEFAULT_STEP_A = float(_MOTR_OPT.DEFAULT_STEP_A)
DEFAULT_STOP_SIGMA_RATIO = 0.20
DEFAULT_AVERAGE_PAUSE_RATIO = 0.80
DEFAULT_GP_SIGNAL_VAR = 0.15
DEFAULT_GP_NOISE_VAR = 1e-4


METHOD_CHOICES = ["BO", "Sequential"]
OBJECTIVE_CHOICES = ["Conrad", "KEK"]
EI_STOP_MODES = {
    "Aggressive": (3e-3, 2),
    "Standard": (1e-3, 2),
    "Careful": (3e-4, 3),
}


def _is_numeric_header(header_name: str) -> bool:
    text = str(header_name)
    if text in {
        "chosen_by",
        "objective_source",
        "measurement_timestamp",
        "measurement_file",
        "dat_filename",
        "filename",
    }:
        return False
    return not text.startswith("machine_")


def _parse_csv_value(header_name: str, raw_value: str):
    if raw_value == "":
        return float("nan") if _is_numeric_header(header_name) else ""
    if not _is_numeric_header(header_name):
        return raw_value
    try:
        return float(raw_value)
    except Exception:
        return raw_value


class OptimizerWorker(QThread):
    progress = pyqtSignal(dict)
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)
    pause_requested = pyqtSignal(dict)

    def __init__(self, optimizer: Optimizer):
        super().__init__()
        self.optimizer = optimizer
        self._pause_event = threading.Event()
        self._pause_continue = True

    def _pause_hook(self, payload: dict) -> bool:
        self._pause_continue = True
        self._pause_event.clear()
        self.pause_requested.emit(payload)
        self._pause_event.wait()
        return self._pause_continue

    def resume_from_pause(self, should_continue: bool = True, *, remeasure_current_point: bool = False) -> None:
        if should_continue and remeasure_current_point:
            self.optimizer.request_remeasure_current_point()
        self._pause_continue = bool(should_continue)
        self._pause_event.set()

    def request_manual_pause(self) -> None:
        self.optimizer.request_manual_pause()

    def run(self):
        try:
            def cb(step, info):
                self.progress.emit({"step": step, "info": info})
            self.optimizer.progress_cb = cb
            self.optimizer.pause_hook = self._pause_hook
            out = self.optimizer.run()
            self.finished.emit(out)
        except Exception as e:
            self.failed.emit(str(e) + "\n" + traceback.format_exc())


class ClickOpenComboBox(QComboBox):
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
        self.setWindowTitle("mOTR Optimization")

        self.interface = InterfaceATF2_Ext()
        self.stop_flag = StopFlag()
        self.worker: Optional[OptimizerWorker] = None
        self.last_out_dir: Optional[Path] = None
        self.last_run_cfg: Optional[OptimizerConfig] = None
        self.current_measurements_csv: Optional[Path] = None
        self.current_machine_origin: Optional[Dict[str, Any]] = None
        self.current_controller: Optional[EPICSmOTRController] = None

        self.live_rows: List[Dict[str, Any]] = []
        self.discarded_rows: List[Dict[str, Any]] = []
        self.resume_discarded_rows: List[Dict[str, Any]] = []
        self.latest_measurement_summary: Dict[str, Any] = {}
        self.bo1d_trace: Optional[Dict[str, Any]] = None

        self._run_selected_knobs: List[str] = []
        self._run_state_channels: List[str] = []
        self._run_initial_values: Dict[str, float] = {}
        self._run_current_values: Dict[str, float] = {}
        self._run_final_values: Dict[str, float] = {}

        self._last_recommended_n_init = 0
        self._last_recommended_max_steps = 0

        self._active_scan_knobs: List[str] = []
        self._done_scan_knobs: List[str] = []
        self._scan_blink_on = False
        self._scan_blink_timer = QTimer(self)
        self._scan_blink_timer.setInterval(450)
        self._scan_blink_timer.timeout.connect(self._on_scan_blink_timer)

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)

        self.main_tab = QWidget()
        self.config_tab = QWidget()
        self.tabs.addTab(self.main_tab, "Main")
        self.tabs.addTab(self.config_tab, "Config")

        self._build_main_tab()
        self._build_config_tab()
        self._connect_signals()
        self._apply_knob_preset(QK_KNOBS)
        self._refresh_recommendations()
        self._refresh_selected_knobs_label()
        self._set_active_scan_knobs([])
        self._redraw_live_plot()

    def _set_status(self, text: str, *, state: str = "info") -> None:
        state_key = str(state or "info").lower()
        palette = {
            "idle": ("#374151", "#e5e7eb", "#9ca3af"),
            "running": ("#14532d", "#dcfce7", "#22c55e"),
            "paused": ("#713f12", "#fef3c7", "#f59e0b"),
            "warning": ("#7c2d12", "#ffedd5", "#fb923c"),
            "error": ("#7f1d1d", "#fee2e2", "#ef4444"),
            "success": ("#1e3a8a", "#dbeafe", "#3b82f6"),
            "info": ("#1f2937", "#e5e7eb", "#9ca3af"),
        }
        fg, bg, bd = palette.get(state_key, palette["info"])
        self.status_lbl.setText(text)
        self.status_lbl.setStyleSheet(
            "QLabel#statusBadge {"
            f"font-size: 24px; font-weight: 800; color: {fg}; "
            f"background: {bg}; border: 2px solid {bd}; "
            "border-radius: 10px; padding: 8px 14px;"
            "}"
        )

    def _build_main_tab(self):
        layout = QVBoxLayout(self.main_tab)
        self.main_tab.setStyleSheet(
            "QGroupBox { font-size: 19px; font-weight: 700; margin-top: 10px; } "
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; } "
            "QLabel { font-size: 17px; } "
            "QCheckBox { font-size: 19px; font-weight: 500; spacing: 10px; } "
            "QCheckBox[knobCheck=\"true\"] { font-size: 21px; font-weight: 500; } "
            "QCheckBox[knobCheck=\"true\"]:checked { font-size: 24px; font-weight: 800; color: #111827; } "
            "QCheckBox[knobCheck=\"true\"][scanActive=\"true\"] { "
            "background: #fff4ce; border: 1px solid #f59e0b; border-radius: 6px; padding: 2px 8px; } "
            "QCheckBox[knobCheck=\"true\"][scanActive=\"true\"][scanBlink=\"true\"] { "
            "background: #f59e0b; color: #111827; font-weight: 900; } "
            "QCheckBox[knobCheck=\"true\"][scanDone=\"true\"] { "
            "background: #dcfce7; border: 1px solid #22c55e; border-radius: 6px; "
            "padding: 2px 8px; color: #166534; font-weight: 850; } "
            "QCheckBox[knobCheck=\"true\"][scanWaiting=\"true\"] { "
            "background: #e5e7eb; border: 1px solid #9ca3af; border-radius: 6px; "
            "padding: 2px 8px; color: #374151; font-weight: 700; } "
            "QCheckBox::indicator { width: 22px; height: 22px; } "
            "QPlainTextEdit { font-size: 15px; } "
            "QLineEdit, QComboBox { font-size: 17px; min-height: 34px; }"
        )

        knob_group = QGroupBox("Knobs To Scan")
        layout.addWidget(knob_group)
        knob_layout = QVBoxLayout(knob_group)

        preset_row = QHBoxLayout()
        self.qk_preset_btn = QPushButton("QK Series")
        self.q_preset_btn = QPushButton("Q Series")
        self.all_preset_btn = QPushButton("All Knobs")
        self.selected_knobs_lbl = QLabel("")
        preset_button_css = "QPushButton { font-size: 20px; font-weight: 700; padding: 14px 24px; min-height: 52px; }"
        self.qk_preset_btn.setStyleSheet(preset_button_css + " QPushButton { background: #0f5c7a; color: white; }")
        self.q_preset_btn.setStyleSheet(preset_button_css + " QPushButton { background: #9b3d1f; color: white; }")
        self.all_preset_btn.setStyleSheet(preset_button_css + " QPushButton { background: #8a5a12; color: white; }")
        self.selected_knobs_lbl.setStyleSheet("font-size: 24px; font-weight: 800; color: #0f172a;")
        self.selected_knobs_lbl.setVisible(False)
        preset_row.addWidget(self.qk_preset_btn)
        preset_row.addWidget(self.q_preset_btn)
        preset_row.addWidget(self.all_preset_btn)
        preset_row.addWidget(self.selected_knobs_lbl, stretch=1)
        knob_layout.addLayout(preset_row)

        self.knob_checks: Dict[str, QCheckBox] = {}

        qk_group = QGroupBox("QK Series")
        qk_layout = QGridLayout(qk_group)
        knob_layout.addWidget(qk_group)
        for idx, name in enumerate(QK_KNOBS):
            box = QCheckBox(name)
            box.setProperty("knobCheck", True)
            box.setProperty("scanActive", False)
            box.setProperty("scanBlink", False)
            box.setProperty("scanDone", False)
            box.setProperty("scanWaiting", False)
            self.knob_checks[name] = box
            qk_layout.addWidget(box, idx // 4, idx % 4)

        q_group = QGroupBox("Q Series")
        q_layout = QGridLayout(q_group)
        knob_layout.addWidget(q_group)
        for idx, name in enumerate(Q_KNOBS):
            box = QCheckBox(name)
            box.setProperty("knobCheck", True)
            box.setProperty("scanActive", False)
            box.setProperty("scanBlink", False)
            box.setProperty("scanDone", False)
            box.setProperty("scanWaiting", False)
            self.knob_checks[name] = box
            q_layout.addWidget(box, idx // 4, idx % 4)

        ctrl_group = QGroupBox("Run Control")
        layout.addWidget(ctrl_group)
        ctrl_layout = QVBoxLayout(ctrl_group)

        quick_row = QHBoxLayout()
        self.method_box = ClickOpenComboBox()
        self.method_box.addItems(METHOD_CHOICES)
        self.method_box.setCurrentText("BO")
        self.method_box.setEditable(True)
        self.method_box.lineEdit().setReadOnly(True)
        self.method_box.lineEdit().setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.method_box.setStyleSheet("QComboBox { font-size: 24px; font-weight: 800; min-height: 44px; padding: 2px 10px; }")
        self.objective_box = ClickOpenComboBox()
        self.objective_box.addItems(OBJECTIVE_CHOICES)
        self.objective_box.setCurrentText("Conrad")
        self.objective_box.setEditable(True)
        self.objective_box.lineEdit().setReadOnly(True)
        self.objective_box.lineEdit().setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.objective_box.setStyleSheet("QComboBox { font-size: 24px; font-weight: 800; min-height: 44px; padding: 2px 10px; }")
        method_lbl = QLabel("Method")
        method_lbl.setStyleSheet("font-size: 24px; font-weight: 900; color: #111827;")
        objective_lbl = QLabel("Objective")
        objective_lbl.setStyleSheet("font-size: 24px; font-weight: 900; color: #111827;")
        quick_row.addWidget(method_lbl)
        quick_row.addWidget(self.method_box)
        quick_row.addSpacing(16)
        quick_row.addWidget(objective_lbl)
        quick_row.addWidget(self.objective_box)
        quick_row.addStretch(1)
        ctrl_layout.addLayout(quick_row)

        button_row = QHBoxLayout()
        self.run_btn = QPushButton("START")
        self.stop_btn = QPushButton("PAUSE")
        self.reset_initial_btn = QPushButton("Reset To Initial")
        self.stop_btn.setEnabled(False)
        big_button_css = "QPushButton { font-size: 22px; font-weight: 700; padding: 16px 28px; min-height: 56px; }"
        self.run_btn.setStyleSheet(big_button_css + " QPushButton { background: #1f7a1f; color: white; }")
        self.stop_btn.setStyleSheet(big_button_css + " QPushButton { background: #a32020; color: white; }")
        self.reset_initial_btn.setStyleSheet(
            "QPushButton { font-size: 18px; font-weight: 700; padding: 14px 22px; min-height: 52px; background: #585f66; color: white; }"
        )
        button_row.addWidget(self.run_btn)
        button_row.addWidget(self.stop_btn)
        button_row.addWidget(self.reset_initial_btn)
        ctrl_layout.addLayout(button_row)

        resume_group = QGroupBox("Resume From Interrupted Run")
        layout.addWidget(resume_group)
        resume_layout = QHBoxLayout(resume_group)
        self.resume_file_edit = QLineEdit()
        self.resume_file_edit.setPlaceholderText("Select previous measurements.csv to continue from its saved data")
        self.resume_file_browse_btn = QPushButton("Browse...")
        self.resume_file_clear_btn = QPushButton("Clear")
        resume_layout.addWidget(self.resume_file_edit, stretch=1)
        resume_layout.addWidget(self.resume_file_browse_btn)
        resume_layout.addWidget(self.resume_file_clear_btn)

        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("statusBadge")
        self.status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_lbl.setMinimumHeight(54)
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        layout.addWidget(self.status_lbl)
        self._set_status("Status: IDLE", state="idle")

        plot_group = QGroupBox("Live View")
        layout.addWidget(plot_group, stretch=1)
        plot_layout = QVBoxLayout(plot_group)
        self.fig = Figure(figsize=(10, 8), tight_layout=True)
        self.canvas = FigureCanvas(self.fig)
        gs = self.fig.add_gridspec(3, 2, height_ratios=[1.1, 1.0, 1.0])
        self.ax_objective = self.fig.add_subplot(gs[0, :])
        self.ax_images = {
            0: self.fig.add_subplot(gs[1, 0]),
            1: self.fig.add_subplot(gs[1, 1]),
            2: self.fig.add_subplot(gs[2, 0]),
            3: self.fig.add_subplot(gs[2, 1]),
        }
        plot_layout.addWidget(self.canvas)
        self.bo1d_fig = Figure(figsize=(10, 5.2), tight_layout=True)
        self.bo1d_canvas = FigureCanvas(self.bo1d_fig)
        self.bo1d_ax_obj = self.bo1d_fig.add_subplot(211)
        self.bo1d_ax_acq = self.bo1d_fig.add_subplot(212, sharex=self.bo1d_ax_obj)
        self.bo1d_canvas.setVisible(False)
        plot_layout.addWidget(self.bo1d_canvas)

        log_group = QGroupBox("Log")
        layout.addWidget(log_group, stretch=1)
        log_layout = QVBoxLayout(log_group)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        log_layout.addWidget(self.log_box)

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

        state_group = QGroupBox("Machine Setpoint State (Initial / Current)")
        state_layout = QVBoxLayout(state_group)
        self.knob_state_table = QTableWidget(0, 3)
        self.knob_state_table.setHorizontalHeaderLabels(["Channel", "Initial", "Current"])
        self.knob_state_table.verticalHeader().setVisible(False)
        self.knob_state_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.knob_state_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.knob_state_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.knob_state_table.setAlternatingRowColors(True)
        hdr = self.knob_state_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.knob_state_table.setMinimumHeight(160)
        state_layout.addWidget(self.knob_state_table)
        layout.addWidget(state_group)

    def _build_config_tab(self):
        layout = QVBoxLayout(self.config_tab)
        layout.setSpacing(10)

        cfg_group = QGroupBox("Optimization Settings")
        layout.addWidget(cfg_group)
        form = QFormLayout(cfg_group)
        form.setContentsMargins(14, 12, 14, 12)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(8)

        self.kernel_lbl = QLabel("RBF (fixed)")

        self.n_init = QSpinBox()
        self.n_init.setRange(1, 200)
        self.n_init.setValue(recommended_initial_points(len(QK_KNOBS)))
        self.n_init_hint_lbl = QLabel("")
        self.max_steps_hint_lbl = QLabel("")

        self.bo_max_steps = QSpinBox()
        self.bo_max_steps.setRange(1, 999)
        self.bo_max_steps.setValue(60)

        self.gf_axis_max_steps = QSpinBox()
        self.gf_axis_max_steps.setRange(3, 999)
        self.gf_axis_max_steps.setValue(7)

        self.ei_stop_enabled = QCheckBox("Enable")
        self.ei_stop_enabled.setChecked(True)
        self.ei_stop_mode_box = QComboBox()
        self.ei_stop_mode_box.addItems(list(EI_STOP_MODES.keys()))
        self.ei_stop_mode_box.setCurrentText("Standard")
        self.plot_both_check = QCheckBox("Plot Conrad and KEK together")
        self.plot_both_check.setChecked(True)

        for lbl in (self.n_init_hint_lbl, self.max_steps_hint_lbl):
            lbl.setStyleSheet("color: #5c6670;")

        form.addRow("BO kernel", self.kernel_lbl)
        form.addRow("Initial points", self.n_init)
        form.addRow("Recommended n_init", self.n_init_hint_lbl)
        form.addRow("BO max steps", self.bo_max_steps)
        form.addRow("Recommended max steps", self.max_steps_hint_lbl)
        form.addRow("Sequential axis max steps", self.gf_axis_max_steps)

        ei_row = QWidget()
        ei_layout = QHBoxLayout(ei_row)
        ei_layout.setContentsMargins(0, 0, 0, 0)
        ei_layout.addWidget(self.ei_stop_enabled)
        ei_layout.addWidget(self.ei_stop_mode_box)
        form.addRow("EI stop rule", ei_row)
        form.addRow("Plot", self.plot_both_check)

        axis_group = QGroupBox("Per-Axis Range / Step (A, centered on live currentWrite)")
        layout.addWidget(axis_group)
        axis_layout = QGridLayout(axis_group)
        axis_layout.setContentsMargins(14, 12, 14, 12)
        axis_layout.setHorizontalSpacing(10)
        axis_layout.setVerticalSpacing(8)
        axis_layout.addWidget(QLabel("Knob"), 0, 0)
        axis_layout.addWidget(QLabel("Half-Range"), 0, 1)
        axis_layout.addWidget(QLabel("Step"), 0, 2)

        self.range_boxes: Dict[str, QDoubleSpinBox] = {}
        self.step_boxes: Dict[str, QDoubleSpinBox] = {}
        for row_idx, name in enumerate(KNOB_ORDER, start=1):
            axis_layout.addWidget(QLabel(name), row_idx, 0)
            range_box = QDoubleSpinBox()
            range_box.setRange(0.1, 1000.0)
            range_box.setDecimals(3)
            range_box.setSingleStep(0.1)
            range_box.setValue(DEFAULT_HALF_RANGE_A)
            range_box.setMaximumWidth(120)
            self.range_boxes[name] = range_box
            axis_layout.addWidget(range_box, row_idx, 1)

            step_box = QDoubleSpinBox()
            step_box.setRange(0.001, 1000.0)
            step_box.setDecimals(3)
            step_box.setSingleStep(0.1)
            step_box.setValue(DEFAULT_STEP_A)
            step_box.setMaximumWidth(120)
            self.step_boxes[name] = step_box
            axis_layout.addWidget(step_box, row_idx, 2)
        axis_layout.setColumnStretch(3, 1)

        measurement_group = QGroupBox("Measurement Settings")
        layout.addWidget(measurement_group)
        measurement_form = QFormLayout(measurement_group)
        measurement_form.setContentsMargins(14, 12, 14, 12)
        measurement_form.setHorizontalSpacing(18)
        measurement_form.setVerticalSpacing(8)

        self.min_total_intensity = QDoubleSpinBox()
        self.min_total_intensity.setRange(0.0, 1e9)
        self.min_total_intensity.setDecimals(0)
        self.min_total_intensity.setValue(130000.0)

        self.max_retries = QSpinBox()
        self.max_retries.setRange(1, 20)
        self.max_retries.setValue(3)

        self.kek_samples = QSpinBox()
        self.kek_samples.setRange(1, 10)
        self.kek_samples.setValue(3)

        self.kek_sample_interval = QDoubleSpinBox()
        self.kek_sample_interval.setRange(0.0, 30.0)
        self.kek_sample_interval.setDecimals(2)
        self.kek_sample_interval.setValue(1.0)
        self.min_total_intensity.setToolTip("Background subtraction後の総強度がこの値を下回ると、そのmOTRを再撮像します。")
        self.max_retries.setToolTip("総強度不足のときに、そのmOTRを取り直す最大回数です。")
        self.kek_samples.setToolTip("KEK objective用に mOTR:analyzer:size:H/V を何回読むかです。")
        self.kek_sample_interval.setToolTip("KEK analyzer PV を連続取得するときの読み取り間隔です。")

        measurement_form.addRow("Min total intensity", self.min_total_intensity)
        measurement_form.addRow("Max retries", self.max_retries)
        measurement_form.addRow("KEK analyzer samples", self.kek_samples)
        measurement_form.addRow("KEK sample interval [s]", self.kek_sample_interval)

        cfg_row = QHBoxLayout()
        self.save_cfg_btn = QPushButton("Save config")
        self.load_cfg_btn = QPushButton("Load config")
        cfg_row.addWidget(self.save_cfg_btn)
        cfg_row.addWidget(self.load_cfg_btn)
        cfg_row.addStretch(1)
        layout.addLayout(cfg_row)
        layout.addStretch(1)

    def _connect_signals(self):
        self.qk_preset_btn.clicked.connect(lambda: self._apply_knob_preset(QK_KNOBS))
        self.q_preset_btn.clicked.connect(lambda: self._apply_knob_preset(Q_KNOBS))
        self.all_preset_btn.clicked.connect(lambda: self._apply_knob_preset(KNOB_ORDER))
        for box in self.knob_checks.values():
            box.toggled.connect(self._on_knob_selection_changed)

        self.n_init.valueChanged.connect(self._refresh_recommendations)
        self.bo_max_steps.valueChanged.connect(self._redraw_live_plot)
        self.resume_file_browse_btn.clicked.connect(self._browse_resume_file)
        self.resume_file_clear_btn.clicked.connect(lambda: self.resume_file_edit.clear())
        self.run_btn.clicked.connect(self._on_run)
        self.stop_btn.clicked.connect(self._on_stop)
        self.reset_initial_btn.clicked.connect(self._on_reset_to_initial)
        self.save_cfg_btn.clicked.connect(self._on_save_config)
        self.load_cfg_btn.clicked.connect(self._on_load_config)
        self.plot_both_check.toggled.connect(self._redraw_live_plot)
        self.objective_box.currentTextChanged.connect(self._redraw_live_plot)
        self.method_box.currentTextChanged.connect(self._redraw_live_plot)

    def _selected_params(self) -> List[str]:
        return [name for name in KNOB_ORDER if self.knob_checks[name].isChecked()]

    def _apply_knob_preset(self, knob_names: List[str]):
        selected = set(knob_names)
        for name, box in self.knob_checks.items():
            box.blockSignals(True)
            box.setChecked(name in selected)
            box.blockSignals(False)
        self._on_knob_selection_changed()

    def _on_knob_selection_changed(self):
        self._refresh_recommendations()
        self._refresh_selected_knobs_label()
        self._refresh_knob_state_labels()

    def _refresh_recommendations(self):
        d = len(self._selected_params())
        rec_init = recommended_initial_points(d)
        rec_max = recommended_max_steps(d, int(self.n_init.value()))
        self.n_init_hint_lbl.setText(f"{rec_init} points for {d}D")
        self.max_steps_hint_lbl.setText(f"{rec_max} total steps for {d}D")
        if self.n_init.value() == self._last_recommended_n_init or self._last_recommended_n_init == 0:
            self.n_init.setValue(rec_init)
        if self.bo_max_steps.value() == self._last_recommended_max_steps or self._last_recommended_max_steps == 0:
            self.bo_max_steps.setValue(rec_max)
        self._last_recommended_n_init = rec_init
        self._last_recommended_max_steps = rec_max

    def _refresh_selected_knobs_label(self):
        params = self._selected_params()
        if not params:
            self.selected_knobs_lbl.setVisible(False)
            return
        self.selected_knobs_lbl.setVisible(True)
        self.selected_knobs_lbl.setText(f"{len(params)} selected: {', '.join(params)}")

    def _axis_from_chosen_by(self, chosen_by: str) -> str:
        text = str(chosen_by or "")
        if not text.startswith("GF["):
            return ""
        end = text.find("]")
        if end <= 3:
            return ""
        return text[3:end]

    def _on_scan_blink_timer(self):
        if not self._active_scan_knobs:
            return
        self._scan_blink_on = not self._scan_blink_on
        active_set = set(self._active_scan_knobs)
        for name, box in self.knob_checks.items():
            if name in active_set:
                box.setProperty("scanBlink", self._scan_blink_on)
                box.style().unpolish(box)
                box.style().polish(box)
                box.update()

    def _refresh_knob_state_labels(self) -> None:
        active_set = set(self._active_scan_knobs)
        done_set = set(self._done_scan_knobs)
        running_now = bool(self.worker is not None)
        selected_set = set(self._run_selected_knobs if self._run_selected_knobs else self._selected_params())
        for name, box in self.knob_checks.items():
            is_active = name in active_set
            is_done = (name in done_set) and (not is_active)
            is_waiting = bool(running_now and (name in selected_set) and (not is_active) and (not is_done))
            box.setProperty("scanDone", is_done)
            box.setProperty("scanWaiting", is_waiting)
            if is_active:
                tag = "RUNNING"
            elif is_done:
                tag = "DONE!"
            elif is_waiting:
                tag = "WAITING"
            else:
                tag = ""
            box.setText(f"{name}  {tag}" if tag else name)
            box.style().unpolish(box)
            box.style().polish(box)
            box.update()

    def _set_active_scan_knobs(self, knob_names: List[str]) -> None:
        keep = [name for name in KNOB_ORDER if name in set(knob_names) and name in self.knob_checks]
        self._active_scan_knobs = keep
        active_set = set(keep)
        if active_set:
            if not self._scan_blink_timer.isActive():
                self._scan_blink_timer.start()
        else:
            self._scan_blink_timer.stop()
            self._scan_blink_on = False
        for name, box in self.knob_checks.items():
            is_active = name in active_set
            box.setProperty("scanActive", is_active)
            box.setProperty("scanBlink", bool(is_active and self._scan_blink_on))
        self._refresh_knob_state_labels()

    def _set_done_scan_knobs(self, knob_names: List[str]) -> None:
        keep = [name for name in KNOB_ORDER if name in set(knob_names) and name in self.knob_checks]
        self._done_scan_knobs = keep
        self._refresh_knob_state_labels()

    def _find_resume_origin_file(self, csv_path: Path) -> Optional[Path]:
        parent = csv_path.resolve().parent
        direct = parent / "machine_origin.json"
        if direct.exists():
            return direct
        tagged = sorted(parent.glob("machine_origin-*.json"))
        return tagged[-1] if tagged else None

    def _load_resume_origin_state(self, csv_path: Path) -> Optional[Dict[str, Any]]:
        origin_path = self._find_resume_origin_file(csv_path)
        if origin_path is None:
            return None
        with open(origin_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _browse_resume_file(self):
        current_base = str(default_output_base_dir())
        current = self.resume_file_edit.text().strip() or str(Path(current_base).expanduser().resolve())
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select interrupted measurements.csv",
            current,
            "CSV (*.csv);;All files (*)",
        )
        if not path:
            return
        self.resume_file_edit.setText(path)
        cfg_path = Path(path).resolve().parent / "config.json"
        if cfg_path.exists():
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                self._set_config_to_ui(payload)
                self._set_status(f"Status: resume file loaded with config -> {path}", state="info")
                self._append_log(f"Loaded resume config from {cfg_path}")
            except Exception as e:
                self._set_status(f"Status: resume file selected -> {path}", state="info")
                self._append_log(f"Resume config load failed: {e}")
        else:
            self._set_status(f"Status: resume file selected -> {path}", state="info")
        origin_path = self._find_resume_origin_file(Path(path))
        if origin_path is not None:
            try:
                self.current_machine_origin = self._load_resume_origin_state(Path(path))
            except Exception:
                self.current_machine_origin = None
            self._append_log(f"Resume origin found: {origin_path}")
        else:
            self.current_machine_origin = None

    def _load_warm_start_rows(self, csv_path: Path) -> List[Dict]:
        rows_out: List[Dict] = []
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                raise ValueError("Resume CSV is empty.")
            if "modulation" not in header or "mod_err" not in header:
                raise ValueError("Resume CSV does not look like measurements.csv.")

            mod_idx = header.index("modulation")
            mod_err_idx = header.index("mod_err")
            chosen_by_idx = header.index("chosen_by") if "chosen_by" in header else None
            param_names = header[2:mod_idx]
            machine_cols = {name for name in header if name.startswith("machine_")}
            base_dat_map = {
                "dat_modulation": "modulation",
                "dat_error": "error",
                "dat_beamsize": "beamsize",
                "dat_ebeamsize": "ebeamsize",
                "dat_average": "average",
                "dat_phase": "phase",
                "dat_filename": "filename",
                "dat_ict_average": "ict_average",
            }
            for row in reader:
                if not row or len(row) <= mod_err_idx:
                    continue
                x = {name: float(row[2 + i]) for i, name in enumerate(param_names)}
                dat: Dict[str, Any] = {}
                for idx, col_name in enumerate(header):
                    if idx <= mod_err_idx:
                        continue
                    if chosen_by_idx is not None and idx == chosen_by_idx:
                        continue
                    if col_name in machine_cols:
                        continue
                    value = row[idx] if idx < len(row) else ""
                    mapped_name = base_dat_map.get(col_name, col_name)
                    dat[mapped_name] = _parse_csv_value(col_name, value)
                rows_out.append(
                    {
                        "step": int(float(row[0])),
                        "t_iso": str(row[1]),
                        "x": x,
                        "y": float(row[mod_idx]),
                        "y_err": float(row[mod_err_idx]),
                        "chosen_by": str(row[chosen_by_idx]) if chosen_by_idx is not None and chosen_by_idx < len(row) else "warm_start",
                        "dat": dat,
                    }
                )
        return rows_out

    def _split_resume_rows_for_remeasure(self, rows: List[Dict]) -> tuple[List[Dict], List[Dict]]:
        if not rows:
            return [], []
        return list(rows[:-1]), [dict(rows[-1])]

    def _extract_measurement_baseline_from_rows(self, rows: List[Dict]) -> Dict[str, Dict[int, float]]:
        baseline = {"conrad": {}, "kek": {}}
        if not rows:
            return baseline
        first_dat = dict(rows[0].get("dat", {}) or {})
        for otr_id in DEFAULT_MOTR_IDS:
            conrad_key = f"otr{int(otr_id)}_baseline_conrad_sigma_v"
            kek_key = f"otr{int(otr_id)}_baseline_kek_sigma_v"
            try:
                conrad_val = float(first_dat.get(conrad_key, float("nan")))
            except Exception:
                conrad_val = float("nan")
            try:
                kek_val = float(first_dat.get(kek_key, float("nan")))
            except Exception:
                kek_val = float("nan")
            if np.isfinite(conrad_val) and conrad_val > 0:
                baseline["conrad"][int(otr_id)] = float(conrad_val)
            if np.isfinite(kek_val) and kek_val > 0:
                baseline["kek"][int(otr_id)] = float(kek_val)
        return baseline

    def _is_sequential_method(self, method: str) -> bool:
        return str(method).upper() in {"GF", "SEQUENTIAL"}

    def _collect_config(self) -> OptimizerConfig:
        params = self._selected_params()
        if not params:
            raise ValueError("Select at least one knob.")

        sigma_map: Dict[str, float] = {}
        bounds: Dict[str, tuple[float, float]] = {}
        param_steps: Dict[str, float] = {}
        origin_map: Dict[str, float] = {}
        for name in params:
            half_range = float(self.range_boxes[name].value())
            step = float(self.step_boxes[name].value())
            if half_range <= 0:
                raise ValueError(f"Half-range for {name} must be positive.")
            if step <= 0:
                raise ValueError(f"Step for {name} must be positive.")
            origin = 0.0
            lo = round((-half_range) / step) * step
            hi = round((+half_range) / step) * step
            if lo >= hi:
                raise ValueError(f"Invalid range for {name}: min must be smaller than max.")
            origin_map[name] = origin
            bounds[name] = (float(lo), float(hi))
            param_steps[name] = float(step)
            sigma_map[name] = max(float(step), 0.5 * float(half_range))

        d = len(params)
        ei_thr, ei_patience = EI_STOP_MODES[self.ei_stop_mode_box.currentText()]
        return OptimizerConfig(
            mode_name="motr",
            method=self.method_box.currentText(),
            acquisition="EI",
            params=params,
            bounds=bounds,
            init_sigma=sigma_map,
            param_origins=origin_map,
            scan_mode_label="mOTR",
            meas_sigma=0.01,
            expected_y_max=None,
            stop_modulation=None,
            knob_step=DEFAULT_STEP_A,
            param_steps=param_steps,
            zscan_axis_names=[],
            zscan_method="BO",
            zscan_range=0.0,
            zscan_step=0.0,
            max_steps=int(self.bo_max_steps.value()),
            bo_max_steps=int(self.bo_max_steps.value()),
            gf_axis_max_steps=int(self.gf_axis_max_steps.value()),
            gf_axis_min_points=3,
            stop_sigma_ratio=DEFAULT_STOP_SIGMA_RATIO,
            n_init_random=int(self.n_init.value()),
            n_candidates=recommended_candidate_pool(d),
            gp_kernel="rbf",
            gp_length_scale=1.0,
            gp_ard_length_scales=sigma_map,
            gp_signal_var=DEFAULT_GP_SIGNAL_VAR,
            gp_noise_var=DEFAULT_GP_NOISE_VAR,
            ucb_beta=2.0,
            ei_xi=0.0,
            bo_stop_on_low_acq=bool(self.ei_stop_enabled.isChecked()),
            bo_low_acq_threshold=float(ei_thr),
            bo_low_acq_patience=int(ei_patience),
            average_pause_ratio=DEFAULT_AVERAGE_PAUSE_RATIO,
            objective_source=self.objective_box.currentText(),
            plot_both=bool(self.plot_both_check.isChecked()),
            motr_ids=list(DEFAULT_MOTR_IDS),
            measurement_min_total_intensity=float(self.min_total_intensity.value()),
            measurement_max_retries=int(self.max_retries.value()),
            measurement_background_frames=10,
            measurement_beam_frames=5,
            measurement_background_wait_sec=1.0,
            measurement_beam_wait_sec=3.0,
            measurement_select_wait_sec=1.0,
            measurement_retract_wait_sec=5.0,
            measurement_insert_wait_sec=5.0,
            measurement_retry_wait_sec=1.0,
            measurement_kek_samples=int(self.kek_samples.value()),
            measurement_kek_sample_interval_sec=float(self.kek_sample_interval.value()),
        )

    def _set_config_to_ui(self, cfg: dict):
        params = cfg.get("params", QK_KNOBS)
        self._apply_knob_preset(params)
        self.method_box.setCurrentText(str(cfg.get("method", "BO") or "BO"))
        self.objective_box.setCurrentText(str(cfg.get("objective_source", "Conrad") or "Conrad"))
        self.plot_both_check.setChecked(bool(cfg.get("plot_both", True)))
        self.n_init.setValue(int(cfg.get("n_init_random", recommended_initial_points(len(params)))))
        self.bo_max_steps.setValue(int(cfg.get("bo_max_steps", cfg.get("max_steps", 60))))
        self.gf_axis_max_steps.setValue(int(cfg.get("gf_axis_max_steps", 7)))
        self.ei_stop_enabled.setChecked(bool(cfg.get("bo_stop_on_low_acq", True)))
        thr = float(cfg.get("bo_low_acq_threshold", 1e-4))
        patience = int(cfg.get("bo_low_acq_patience", 2))
        matched_mode = "Standard"
        for mode_name, pair in EI_STOP_MODES.items():
            if abs(pair[0] - thr) < 1e-12 and int(pair[1]) == patience:
                matched_mode = mode_name
                break
        self.ei_stop_mode_box.setCurrentText(matched_mode)
        self.min_total_intensity.setValue(float(cfg.get("measurement_min_total_intensity", 130000.0)))
        self.max_retries.setValue(int(cfg.get("measurement_max_retries", 3)))
        self.kek_samples.setValue(int(cfg.get("measurement_kek_samples", 3)))
        self.kek_sample_interval.setValue(float(cfg.get("measurement_kek_sample_interval_sec", 1.0)))

        init_sigma = dict(cfg.get("init_sigma", {}) or {})
        bounds = dict(cfg.get("bounds", {}) or {})
        origins = dict(cfg.get("param_origins", {}) or {})
        steps = dict(cfg.get("param_steps", {}) or {})
        for name in KNOB_ORDER:
            self.step_boxes[name].setValue(float(steps.get(name, DEFAULT_STEP_A)))
            if name in bounds:
                lo, hi = bounds[name]
                origin_ref = float(origins.get(name, 0.0))
                half_range = max(abs(float(hi) - origin_ref), abs(origin_ref - float(lo)))
                self.range_boxes[name].setValue(float(half_range))
            elif name in init_sigma:
                self.range_boxes[name].setValue(max(DEFAULT_HALF_RANGE_A, 2.0 * float(init_sigma[name])))
            else:
                self.range_boxes[name].setValue(DEFAULT_HALF_RANGE_A)

        self._refresh_recommendations()
        self._refresh_selected_knobs_label()
        self._redraw_live_plot()

    def _make_measurement_settings(self, cfg: OptimizerConfig) -> Dict[str, Any]:
        return {
            "min_total_intensity": float(cfg.measurement_min_total_intensity),
            "max_retries": int(cfg.measurement_max_retries),
            "background_frames": int(cfg.measurement_background_frames),
            "beam_frames": int(cfg.measurement_beam_frames),
            "background_wait_sec": float(cfg.measurement_background_wait_sec),
            "beam_wait_sec": float(cfg.measurement_beam_wait_sec),
            "select_wait_sec": float(cfg.measurement_select_wait_sec),
            "retract_wait_sec": float(cfg.measurement_retract_wait_sec),
            "insert_wait_sec": float(cfg.measurement_insert_wait_sec),
            "retry_wait_sec": float(cfg.measurement_retry_wait_sec),
            "kek_samples": int(cfg.measurement_kek_samples),
            "kek_sample_interval_sec": float(cfg.measurement_kek_sample_interval_sec),
        }

    def _make_controller(
        self,
        cfg: OptimizerConfig,
        *,
        out_dir: Path,
        baseline_state: Optional[Dict[str, Any]] = None,
    ) -> EPICSmOTRController:
        return EPICSmOTRController(
            interface=self.interface,
            objective_source=cfg.objective_source,
            plot_both=cfg.plot_both,
            out_dir=out_dir,
            motr_ids=list(cfg.motr_ids),
            baseline_state=baseline_state,
            measurement_settings=self._make_measurement_settings(cfg),
        )

    def _on_reset_to_initial(self):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.warning(self, "Busy", "Stop the optimizer before resetting to the initial state.")
            return
        origin = self.current_machine_origin
        if not origin:
            resume_path_text = self.resume_file_edit.text().strip()
            if resume_path_text:
                try:
                    origin = self._load_resume_origin_state(Path(resume_path_text).expanduser().resolve())
                    self.current_machine_origin = origin
                except Exception as e:
                    QMessageBox.warning(self, "Reset failed", f"Could not load machine origin: {e}")
                    return
        if not origin:
            QMessageBox.information(
                self,
                "No Initial State",
                "No machine origin is loaded yet. Start a run or load a previous resume file first.",
            )
            return
        try:
            controller = self._make_controller(
                self.last_run_cfg or self._collect_config(),
                out_dir=self.last_out_dir or default_output_base_dir(),
                baseline_state=origin,
            )
            controller.restore_machine_origin(origin)
        except Exception as e:
            self._append_log(f"Reset to initial failed: {e}")
            QMessageBox.warning(self, "Reset failed", str(e))
            return
        self._set_status("Status: restored to initial machine state", state="success")
        self._append_log("Reset to initial completed.")

    def _append_log(self, line: str):
        self.log_box.appendPlainText(str(line))

    def _fmt_knob_value(self, value: float) -> str:
        if not np.isfinite(value):
            return "nan"
        return f"{float(value):+.4f}"

    def _set_run_machine_state(self, controller: EPICSmOTRController, cfg: OptimizerConfig):
        info = controller.describe_machine_setpoint_channels(list(cfg.params))
        channels = list(info.get("channels", []))
        initial = dict(info.get("initial", {}))
        self._run_state_channels = channels
        self._run_initial_values = {str(ch): float(initial.get(ch, float("nan"))) for ch in channels}
        self._run_current_values = dict(self._run_initial_values)
        self._run_final_values = dict(self._run_initial_values)
        self._update_run_knob_state_table()

    def _update_run_knob_state_table(self):
        table = self.knob_state_table
        channels = list(self._run_state_channels)
        table.setRowCount(len(channels))
        for row, name in enumerate(channels):
            init_txt = self._fmt_knob_value(self._run_initial_values.get(name, float("nan")))
            cur_txt = self._fmt_knob_value(self._run_current_values.get(name, float("nan")))
            table.setItem(row, 0, QTableWidgetItem(str(name)))
            table.setItem(row, 1, QTableWidgetItem(init_txt))
            table.setItem(row, 2, QTableWidgetItem(cur_txt))

    def _compute_machine_values_for_display(self, x_map: Dict[str, float]) -> Dict[str, float]:
        controller = self.current_controller
        if controller is None:
            return {}
        try:
            return controller.compute_machine_setpoint_values(dict(x_map), knob_names=list(self._run_selected_knobs))
        except Exception:
            return {}

    def _build_run_output_dir(self, base_dir: Path, tag: str, suffix: str = "motr-bo") -> Path:
        year_dir = base_dir / tag[:4]
        return year_dir / f"{tag}-{suffix}"

    def _run_output_suffix(self, method: str) -> str:
        return "motr-seq" if self._is_sequential_method(method) else "motr-bo"

    def _row_from_dat(self, step: int, chosen_by: str, dat: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "step": int(step),
            "chosen_by": str(chosen_by),
            "objective_selected": float(dat.get("objective_selected", float("nan"))),
            "objective_conrad": float(dat.get("objective_conrad", float("nan"))),
            "objective_kek": float(dat.get("objective_kek", float("nan"))),
            "average": float(dat.get("average", float("nan"))),
        }

    def _reset_live_history(self):
        self.live_rows = []
        self.discarded_rows = []
        self.resume_discarded_rows = []
        self.latest_measurement_summary = {}
        self._set_bo1d_trace(None)
        self._redraw_live_plot()

    def _prime_live_history(self, warm_start_rows: List[Dict], discarded_rows: Optional[List[Dict]] = None) -> None:
        self.live_rows = []
        self.discarded_rows = []
        for item in warm_start_rows:
            dat = dict(item.get("dat", {}) or {})
            self.live_rows.append(self._row_from_dat(int(item.get("step", len(self.live_rows) + 1)), str(item.get("chosen_by", "warm_start")), dat))
        for item in list(discarded_rows or []):
            dat = dict(item.get("dat", {}) or {})
            self.discarded_rows.append(self._row_from_dat(int(item.get("step", len(self.discarded_rows) + 1)), str(item.get("chosen_by", "warm_start_discarded")), dat))
        self._set_bo1d_trace(None)
        self._redraw_live_plot()

    def _set_bo1d_trace(self, trace: Optional[Dict[str, Any]]) -> None:
        self.bo1d_trace = dict(trace or {}) if trace else None
        visible = bool(self.bo1d_trace and self.bo1d_trace.get("x_grid"))
        self.bo1d_canvas.setVisible(visible)
        self._redraw_bo1d_plot()

    def _redraw_bo1d_plot(self) -> None:
        self.bo1d_ax_obj.clear()
        self.bo1d_ax_acq.clear()
        trace = dict(self.bo1d_trace or {})
        if not trace:
            self.bo1d_canvas.draw_idle()
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
        y_label = str(trace.get("y_label", "Objective"))
        acq_label = str(trace.get("acquisition_label", "Acquisition"))
        direction = str(trace.get("direction", "maximize")).lower()
        note = str(trace.get("note", "") or "")

        if x_grid.size and y_mean.size == x_grid.size and y_std.size == x_grid.size:
            lo_band = y_mean - y_std
            hi_band = y_mean + y_std
            mask = np.isfinite(x_grid) & np.isfinite(lo_band) & np.isfinite(hi_band)
            if np.any(mask):
                self.bo1d_ax_obj.fill_between(
                    x_grid[mask], lo_band[mask], hi_band[mask],
                    color="#cfe8ff", alpha=0.65, label="Surrogate ±1σ",
                )
            mean_mask = np.isfinite(x_grid) & np.isfinite(y_mean)
            if np.any(mean_mask):
                self.bo1d_ax_obj.plot(
                    x_grid[mean_mask], y_mean[mean_mask],
                    linestyle="--", linewidth=1.8, color="#1d4ed8", label="Surrogate mean",
                )

        obs_mask = np.isfinite(x_obs) & np.isfinite(y_obs)
        if np.any(obs_mask):
            self.bo1d_ax_obj.plot(
                x_obs[obs_mask], y_obs[obs_mask],
                linestyle="None", marker="o", markersize=6,
                color="#111827", label="Measured points",
            )

        if np.isfinite(chosen_x):
            self.bo1d_ax_obj.axvline(chosen_x, color="#b45309", linestyle=":", linewidth=1.6, label="Chosen x")
            self.bo1d_ax_acq.axvline(chosen_x, color="#b45309", linestyle=":", linewidth=1.6)

        acq_mask = np.isfinite(x_grid) & np.isfinite(acq)
        if np.any(acq_mask):
            self.bo1d_ax_acq.plot(
                x_grid[acq_mask], acq[acq_mask],
                color="#d97706", linewidth=1.8, label=f"{acq_label} acquisition",
            )
        if np.isfinite(chosen_x) and np.isfinite(chosen_acq):
            self.bo1d_ax_acq.plot(
                [chosen_x], [chosen_acq],
                linestyle="None", marker="o", markersize=7,
                color="#92400e", label="Chosen: max acquisition",
            )

        goal_text = "higher is better" if direction == "maximize" else "lower is better"
        self.bo1d_ax_obj.set_title(f"1D BO surrogate for {axis_name} ({goal_text})")
        self.bo1d_ax_obj.set_ylabel(y_label)
        self.bo1d_ax_obj.grid(True, alpha=0.3)
        if note:
            self.bo1d_ax_obj.text(
                0.01, 0.98, note,
                transform=self.bo1d_ax_obj.transAxes,
                ha="left", va="top", fontsize=8.5, color="#475569",
            )

        self.bo1d_ax_acq.set_title("Why this point was chosen")
        self.bo1d_ax_acq.set_xlabel(axis_name)
        self.bo1d_ax_acq.set_ylabel(acq_label)
        self.bo1d_ax_acq.grid(True, alpha=0.3)

        if self.bo1d_ax_obj.get_legend_handles_labels()[0]:
            self.bo1d_ax_obj.legend(loc="best")
        if self.bo1d_ax_acq.get_legend_handles_labels()[0]:
            self.bo1d_ax_acq.legend(loc="best")
        self.bo1d_canvas.draw_idle()

    def _refresh_latest_images(self):
        summary = dict(self.latest_measurement_summary or {})
        measurement_results = dict(summary.get("results", {}) or {})
        for otr_id, ax in self.ax_images.items():
            ax.clear()
            result = dict(measurement_results.get(int(otr_id), {}) or {})
            image = result.get("subtracted_image")
            if image is None:
                ax.text(0.5, 0.5, f"mOTR{otr_id}\nNo image", ha="center", va="center", transform=ax.transAxes)
                ax.set_xticks([])
                ax.set_yticks([])
                continue
            arr = np.asarray(image, dtype=float)
            arr[~np.isfinite(arr)] = 0.0
            ax.imshow(arr, cmap="gray", origin="lower")
            conrad = dict(result.get("conrad", {}) or {})
            kek = dict(result.get("kek", {}) or {})
            sigma_conrad = float(conrad.get("sigma_v_um", float("nan")))
            sigma_kek = float(kek.get("size_v_mean", float("nan")))
            ax.set_title(
                f"mOTR{otr_id} | Conrad $\\sigma_y$={sigma_conrad:.3g} | KEK $\\sigma_y$={sigma_kek:.3g}",
                fontsize=9,
            )
            ax.set_xticks([])
            ax.set_yticks([])

    def _redraw_live_plot(self):
        self.ax_objective.clear()
        rows = list(self.live_rows)
        if rows:
            steps = np.asarray([int(row["step"]) for row in rows], dtype=float)
            selected = np.asarray([float(row.get("objective_selected", float("nan"))) for row in rows], dtype=float)
            conrad = np.asarray([float(row.get("objective_conrad", float("nan"))) for row in rows], dtype=float)
            kek = np.asarray([float(row.get("objective_kek", float("nan"))) for row in rows], dtype=float)
            chosen = [str(row.get("chosen_by", "")) for row in rows]
            best_selected = np.minimum.accumulate(np.where(np.isfinite(selected), selected, np.inf))

            if str(self.method_box.currentText()).upper() == "BO":
                init_idx = [idx for idx, cb in enumerate(chosen) if cb.startswith("init")]
                if init_idx:
                    left_edge = float(steps[0] - 0.5)
                    right_edge = float(steps[-1] + 0.5)
                    init_last = max(init_idx)
                    split_edge = float(0.5 * (steps[init_last] + (steps[init_last + 1] if init_last < len(steps) - 1 else steps[init_last] + 1.0)))
                    self.ax_objective.axvspan(left_edge, split_edge, color="#d8ecff", alpha=0.35, lw=0, zorder=0)
                    if split_edge < right_edge:
                        self.ax_objective.axvspan(split_edge, right_edge, color="#fff0cc", alpha=0.35, lw=0, zorder=0)

            self.ax_objective.axhline(2.0, color="#6b7280", linestyle="--", linewidth=1.0, label="Initial objective = 2")
            if self.plot_both_check.isChecked():
                self.ax_objective.plot(steps, conrad, marker="o", color="#1769aa", label="Conrad objective")
                self.ax_objective.plot(steps, kek, marker="o", color="#d97706", label="KEK objective")
                active = str(self.objective_box.currentText()).lower()
                if active == "kek":
                    self.ax_objective.plot(steps, kek, color="#92400e", linewidth=2.4, alpha=0.85)
                else:
                    self.ax_objective.plot(steps, conrad, color="#0f4c81", linewidth=2.4, alpha=0.85)
            else:
                source = str(self.objective_box.currentText()).lower()
                if source == "kek":
                    self.ax_objective.plot(steps, kek, marker="o", color="#d97706", label="KEK objective")
                else:
                    self.ax_objective.plot(steps, conrad, marker="o", color="#1769aa", label="Conrad objective")
            self.ax_objective.plot(steps, best_selected, color="#111827", linewidth=1.6, linestyle=":", label="Best selected objective")

        if self.discarded_rows:
            d_steps = np.asarray([int(row["step"]) for row in self.discarded_rows], dtype=float)
            d_selected = np.asarray([float(row.get("objective_selected", float("nan"))) for row in self.discarded_rows], dtype=float)
            mask = np.isfinite(d_steps) & np.isfinite(d_selected)
            if np.any(mask):
                self.ax_objective.plot(
                    d_steps[mask],
                    d_selected[mask],
                    linestyle="None",
                    marker="x",
                    color="#dc2626",
                    markersize=8,
                    markeredgewidth=2.0,
                    label="Discarded selected objective",
                )

        self.ax_objective.set_title("mOTR objective vs evaluation")
        self.ax_objective.set_xlabel("Evaluation")
        self.ax_objective.set_ylabel("Objective (lower is better)")
        self.ax_objective.grid(True, alpha=0.3)
        handles, labels = self.ax_objective.get_legend_handles_labels()
        if handles:
            self.ax_objective.legend(loc="best")

        self._refresh_latest_images()
        self.canvas.draw_idle()

    def _on_save_config(self):
        try:
            cfg = self._collect_config()
        except Exception as e:
            QMessageBox.warning(self, "Save config", str(e))
            return
        payload = asdict(cfg)
        path, _ = QFileDialog.getSaveFileName(self, "Save config", "", "JSON (*.json)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        self._set_status(f"Status: config saved -> {path}", state="info")

    def _on_load_config(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load config", "", "JSON (*.json)")
        if not path:
            return
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        self._set_config_to_ui(payload)
        self._set_status(f"Status: config loaded -> {path}", state="info")

    def _objective_summary_from_controller(self) -> str:
        summary = dict(self.latest_measurement_summary or {})
        selected = float(summary.get("objective_selected", float("nan")))
        conrad = float(summary.get("objective_conrad", float("nan")))
        kek = float(summary.get("objective_kek", float("nan")))
        return f"selected={selected:.6f} | Conrad={conrad:.6f} | KEK={kek:.6f}"

    def _on_run(self):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.warning(self, "Busy", "Optimizer is running.")
            return

        try:
            cfg = self._collect_config()
        except Exception as e:
            QMessageBox.warning(self, "Invalid config", str(e))
            return

        tag = now_tag()
        output_base_dir = default_output_base_dir().expanduser()
        out_dir = self._build_run_output_dir(output_base_dir, tag, suffix=self._run_output_suffix(cfg.method))
        out_dir.mkdir(parents=True, exist_ok=True)
        self.last_out_dir = out_dir
        self.last_run_cfg = cfg
        self.stop_flag = StopFlag()

        warm_start_rows: List[Dict] = []
        warm_start_rows_all: List[Dict] = []
        resume_discarded_rows: List[Dict] = []
        baseline_state: Optional[Dict[str, Any]] = None

        resume_path_text = self.resume_file_edit.text().strip()
        if resume_path_text:
            try:
                resume_path = Path(resume_path_text).expanduser().resolve()
                warm_start_rows_all = self._load_warm_start_rows(resume_path)
                baseline_state = self._load_resume_origin_state(resume_path)
            except Exception as e:
                QMessageBox.warning(self, "Resume file error", str(e))
                return
            if not warm_start_rows_all:
                QMessageBox.warning(self, "Resume file error", "Resume CSV did not contain any valid measurements.")
                return
            resume_params = list(warm_start_rows_all[0]["x"].keys())
            if resume_params != list(cfg.params):
                QMessageBox.warning(
                    self,
                    "Resume file mismatch",
                    "Selected knobs do not match the resume CSV.\n"
                    f"Current: {cfg.params}\nResume: {resume_params}",
                )
                return
            warm_start_rows, resume_discarded_rows = self._split_resume_rows_for_remeasure(warm_start_rows_all)
            if baseline_state is None:
                baseline_state = {}
            if "measurement_baseline" not in baseline_state:
                baseline_state["measurement_baseline"] = self._extract_measurement_baseline_from_rows(warm_start_rows_all)

        controller = self._make_controller(cfg, out_dir=out_dir, baseline_state=baseline_state)
        self.current_controller = controller
        origin_state = controller.ensure_machine_origin(cfg.params)
        self.current_machine_origin = origin_state
        self._set_run_machine_state(controller, cfg)
        self._run_selected_knobs = list(cfg.params)
        self.current_measurements_csv = None

        opt = Optimizer(
            controller=controller,
            config=cfg,
            out_dir=out_dir,
            stop_flag=self.stop_flag,
            warm_start_data=warm_start_rows,
            resume_pending_row=resume_discarded_rows[0] if resume_discarded_rows else None,
        )
        self.current_measurements_csv = Path(opt.measurements_csv_path)
        self.worker = OptimizerWorker(opt)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.pause_requested.connect(self._on_pause_requested)

        self.resume_discarded_rows = list(resume_discarded_rows)
        if warm_start_rows or resume_discarded_rows:
            self._prime_live_history(warm_start_rows, discarded_rows=resume_discarded_rows)
        else:
            self._reset_live_history()

        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._set_done_scan_knobs([])
        self._set_active_scan_knobs([])
        self._set_status(f"Status: RUNNING {cfg.method} | knobs={', '.join(cfg.params)}", state="running")
        self.result_lbl.setText("Result: running...")
        self._append_log(
            f"Starting {cfg.method} objective={cfg.objective_source} knobs={cfg.params} "
            f"range/step configured per axis | n_init={cfg.n_init_random} max_steps={cfg.bo_max_steps}"
        )
        self.worker.start()
        self._refresh_knob_state_labels()

    def _on_stop(self):
        if self.worker is None:
            return
        self.worker.request_manual_pause()
        self._set_status("Status: pause requested after current measurement", state="paused")

    def _on_pause_requested(self, payload: dict):
        info = dict(payload or {})
        reason = str(info.get("reason", ""))
        op = str(info.get("operation", "operation"))
        summary_text = self._objective_summary_from_controller()

        if reason == "operation_error":
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Operation Error")
            box.setText(
                f"{op} failed during measurement.\n"
                f"step={info.get('step', '?')}\n"
                f"{summary_text}\n\n"
                f"{info.get('message', '')}"
            )
            resume_btn = box.addButton("Retry", QMessageBox.ButtonRole.AcceptRole)
            stop_btn = box.addButton("Save and End", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(resume_btn)
            box.exec()
            if self.worker is None:
                return
            if box.clickedButton() is stop_btn:
                self.stop_flag.request_stop()
                self._set_status(f"Status: stop requested after {op} error", state="warning")
                self.worker.resume_from_pause(False)
            else:
                self._set_status(f"Status: retrying after {op} error", state="running")
                self.worker.resume_from_pause(True)
            return

        if reason == "current_drop_to_zero":
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Current Drop Detected")
            box.setText(
                f"Quadrupole readback dropped near 0 A.\n"
                f"step={info.get('step', '?')}\n"
                f"{summary_text}\n\n"
                f"{info.get('message', '')}"
            )
            resume_btn = box.addButton("Retry", QMessageBox.ButtonRole.AcceptRole)
            stop_btn = box.addButton("Save and End", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(resume_btn)
            box.exec()
            if self.worker is None:
                return
            if box.clickedButton() is stop_btn:
                self.stop_flag.request_stop()
                self._set_status("Status: stop requested after current drop", state="warning")
                self.worker.resume_from_pause(False)
            else:
                self._set_status("Status: retrying after current drop", state="running")
                self.worker.resume_from_pause(True)
            return

        if reason == "manual_pause":
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("Pause Requested")
            box.setText(
                f"Current measurement finished.\n"
                f"step={info.get('step', '?')}\n"
                f"{summary_text}"
            )
            resume_btn = box.addButton("Resume", QMessageBox.ButtonRole.AcceptRole)
            stop_btn = box.addButton("Save and End", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(resume_btn)
            box.exec()
            if self.worker is None:
                return
            if box.clickedButton() is stop_btn:
                self.stop_flag.request_stop()
                self._set_status("Status: stop requested from pause", state="warning")
                self.worker.resume_from_pause(False)
            else:
                self._set_status("Status: re-measuring paused point", state="running")
                self.worker.resume_from_pause(True, remeasure_current_point=True)
            return

        avg = float(info.get("average", float("nan")))
        baseline = float(info.get("baseline_average", float("nan")))
        threshold = float(info.get("threshold_average", float("nan")))
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Average Warning")
        box.setText(
            f"Average dropped below threshold.\n"
            f"step={info.get('step', '?')}\n"
            f"average={avg:.6f}\n"
            f"baseline={baseline:.6f}\n"
            f"threshold={threshold:.6f}\n"
            f"{summary_text}"
        )
        resume_btn = box.addButton("Resume", QMessageBox.ButtonRole.AcceptRole)
        stop_btn = box.addButton("Save and End", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(resume_btn)
        box.exec()
        if self.worker is None:
            return
        if box.clickedButton() is stop_btn:
            self.stop_flag.request_stop()
            self._set_status("Status: stop requested from average warning", state="warning")
            self.worker.resume_from_pause(False)
        else:
            self._set_status("Status: re-measuring warning point", state="running")
            self.worker.resume_from_pause(True, remeasure_current_point=True)

    def _latest_summary_from_controller(self) -> Dict[str, Any]:
        if self.worker is None:
            return dict(self.latest_measurement_summary or {})
        try:
            summary = self.worker.optimizer.controller.get_latest_measurement()
        except Exception:
            summary = {}
        if summary:
            self.latest_measurement_summary = dict(summary)
        return dict(self.latest_measurement_summary or {})

    def _append_live_row_from_summary(self, step: int, chosen_by: str, summary: Dict[str, Any], *, discarded: bool = False):
        row = {
            "step": int(step),
            "chosen_by": str(chosen_by),
            "objective_selected": float(summary.get("objective_selected", float("nan"))),
            "objective_conrad": float(summary.get("objective_conrad", float("nan"))),
            "objective_kek": float(summary.get("objective_kek", float("nan"))),
            "average": float(summary.get("average_intensity", float("nan"))),
        }
        target = self.discarded_rows if discarded else self.live_rows
        if target and int(target[-1]["step"]) == int(step) and str(target[-1]["chosen_by"]) == str(chosen_by):
            target[-1] = row
        else:
            target.append(row)

    def _on_progress(self, payload: dict):
        step = int(payload.get("step", 0))
        info = dict(payload.get("info", {}) or {})
        phase = str(info.get("phase", ""))
        bo1d_trace = dict(info.get("bo1d_trace", {}) or {})
        if bo1d_trace:
            self._set_bo1d_trace(bo1d_trace)
        chosen_by = str(info.get("chosen_by", ""))
        x_map = dict(info.get("x", {}) or {})

        if x_map and self._run_state_channels:
            machine_vals = self._compute_machine_values_for_display(x_map)
            if machine_vals:
                for ch in self._run_state_channels:
                    if ch in machine_vals:
                        self._run_current_values[ch] = float(machine_vals[ch])

        summary = self._latest_summary_from_controller()
        if summary:
            if phase == "discarded_measurement":
                self._append_live_row_from_summary(step, chosen_by, summary, discarded=True)
                self.resume_discarded_rows.append(
                    {
                        "step": int(step),
                        "x": dict(x_map),
                        "y": float(info.get("y", float("nan"))),
                        "y_err": float(info.get("y_err", float("nan"))),
                        "chosen_by": chosen_by,
                        "dat": dict(summary.get("flat_dat", {}) or {}),
                    }
                )
            elif phase in ("init", "loop", "axis_finalize", "resume_remeasure"):
                self._append_live_row_from_summary(step, chosen_by, summary, discarded=False)

        if phase == "model_fit":
            self._append_log(f"step={int(info.get('step_next', step)):02d} fitting surrogate/model")
        elif phase == "acquisition":
            self._append_log(
                f"step={int(info.get('step_next', step)):02d} evaluating acquisition max={float(info.get('max_acq', float('nan'))):.6g}"
            )
        elif phase == "measuring":
            pos_txt = " ".join(f"{k}={float(v):+.3f}" for k, v in x_map.items()) if x_map else ""
            self._append_log(f"step={step:02d} setting candidate by={chosen_by} {pos_txt}".strip())
        elif phase == "reuse":
            self._append_log(f"step={step:02d} reusing previous measurement from step={int(info.get('reuse_from_step', 0))}")
        elif phase == "final_apply":
            pos_txt = " ".join(f"{k}={float(v):+.3f}" for k, v in x_map.items()) if x_map else ""
            self._append_log(
                f"final apply: objective={float(summary.get('objective_selected', float('nan'))):.6f} {pos_txt}".strip()
            )
            if x_map:
                machine_final = self._compute_machine_values_for_display(x_map)
                if machine_final:
                    for ch in self._run_state_channels:
                        if ch in machine_final:
                            value = float(machine_final[ch])
                            self._run_current_values[ch] = value
                            self._run_final_values[ch] = value
        elif phase == "resume_remeasure":
            self._append_log(f"step={step:02d} re-measured discarded resume point by={chosen_by}")
        elif phase == "discarded_measurement":
            self._append_log(f"step={step:02d} discarded measurement by={chosen_by} reason={info.get('reason', '')}")
        elif phase == "warn":
            self._append_log(f"warning reason={info.get('reason', '')} message={info.get('message', '')}")
        elif phase == "stop":
            self._append_log(f"stop reason={info.get('reason', '')}")
        elif phase == "axis_done":
            axis_done = str(info.get("axis", "")).strip()
            if axis_done in self.knob_checks:
                done_set = set(self._done_scan_knobs)
                done_set.add(axis_done)
                self._set_done_scan_knobs(list(done_set))

        if summary and phase in ("init", "loop", "axis_finalize", "resume_remeasure"):
            current_obj = float(summary.get("objective_selected", float("nan")))
            best_obj = float(np.nanmin(np.asarray([row["objective_selected"] for row in self.live_rows], dtype=float)))
            self._set_status(
                f"Status: RUNNING | step={step} | objective={current_obj:.6f} | best={best_obj:.6f}",
                state="running",
            )

        if phase in ("init", "loop", "axis_finalize", "resume_remeasure", "discarded_measurement"):
            method_live = str(self.method_box.currentText()).upper()
            if self._is_sequential_method(method_live):
                axis_live = str(info.get("axis", "")).strip() or self._axis_from_chosen_by(chosen_by)
                if axis_live in self.knob_checks:
                    self._set_active_scan_knobs([axis_live])
            else:
                self._set_active_scan_knobs(list(self.last_run_cfg.params) if self.last_run_cfg is not None else self._selected_params())
            self._redraw_live_plot()

        if self._run_selected_knobs:
            self._update_run_knob_state_table()

    def _on_failed(self, msg: str):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.worker = None
        self.current_controller = None
        self._set_active_scan_knobs([])
        self._set_done_scan_knobs([])
        self._run_selected_knobs = []
        self._run_state_channels = []
        self._run_initial_values = {}
        self._run_current_values = {}
        self._run_final_values = {}
        self._update_run_knob_state_table()
        self._set_status("Status: FAILED", state="error")
        self._append_log(msg)
        QMessageBox.critical(self, "Failed", msg)

    def _save_summary_plots(self, out_dir: Path, rows: List[Dict[str, Any]], cfg: OptimizerConfig):
        if not rows:
            return
        steps = np.asarray([int(row["step"]) for row in rows], dtype=float)
        selected = np.asarray([float(row.get("objective_selected", float("nan"))) for row in rows], dtype=float)
        conrad = np.asarray([float(row.get("objective_conrad", float("nan"))) for row in rows], dtype=float)
        kek = np.asarray([float(row.get("objective_kek", float("nan"))) for row in rows], dtype=float)

        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(9, 5), constrained_layout=True)
        ax.axhline(2.0, color="#6b7280", linestyle="--", linewidth=1.0, label="Initial objective = 2")
        active_source = str(cfg.objective_source).lower()
        if active_source == "kek":
            active_values = kek
            active_label = "KEK objective"
            active_color = "#d97706"
        else:
            active_values = conrad
            active_label = "Conrad objective"
            active_color = "#1769aa"
        ax.plot(steps, active_values, marker="o", color=active_color, label=active_label)
        ax.plot(steps, np.minimum.accumulate(np.where(np.isfinite(selected), selected, np.inf)), color="#111827", linestyle=":", label="Best selected objective")
        ax.set_title("Selected objective vs evaluation")
        ax.set_xlabel("Evaluation")
        ax.set_ylabel("Objective (lower is better)")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best")
        fig.savefig(out_dir / "objective_selected_vs_evaluation.png", dpi=150)
        plt.close(fig)

        if cfg.plot_both:
            fig2, ax2 = plt.subplots(figsize=(9, 5), constrained_layout=True)
            ax2.axhline(2.0, color="#6b7280", linestyle="--", linewidth=1.0, label="Initial objective = 2")
            ax2.plot(steps, conrad, marker="o", color="#1769aa", label="Conrad objective")
            ax2.plot(steps, kek, marker="o", color="#d97706", label="KEK objective")
            ax2.set_title("Conrad vs KEK objective comparison")
            ax2.set_xlabel("Evaluation")
            ax2.set_ylabel("Objective (lower is better)")
            ax2.grid(True, alpha=0.3)
            ax2.legend(loc="best")
            fig2.savefig(out_dir / "objective_compare_vs_evaluation.png", dpi=150)
            plt.close(fig2)

        summary = dict(self.latest_measurement_summary or {})
        measurement_results = dict(summary.get("results", {}) or {})
        if measurement_results:
            fig3, axes = plt.subplots(2, 2, figsize=(10, 8), constrained_layout=True)
            for ax, otr_id in zip(axes.flat, DEFAULT_MOTR_IDS):
                result = dict(measurement_results.get(int(otr_id), {}) or {})
                image = result.get("subtracted_image")
                if image is None:
                    ax.text(0.5, 0.5, f"mOTR{otr_id}\nNo image", ha="center", va="center", transform=ax.transAxes)
                    ax.set_xticks([])
                    ax.set_yticks([])
                    continue
                arr = np.asarray(image, dtype=float)
                arr[~np.isfinite(arr)] = 0.0
                ax.imshow(arr, cmap="gray", origin="lower")
                conrad_res = dict(result.get("conrad", {}) or {})
                kek_res = dict(result.get("kek", {}) or {})
                ax.set_title(
                    f"mOTR{otr_id} | C={float(conrad_res.get('sigma_v_um', float('nan'))):.3g} | "
                    f"K={float(kek_res.get('size_v_mean', float('nan'))):.3g}",
                    fontsize=9,
                )
                ax.set_xticks([])
                ax.set_yticks([])
            fig3.savefig(out_dir / "latest_motr_images.png", dpi=150)
            plt.close(fig3)

    def _on_finished(self, out: dict):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.worker = None
        self._set_active_scan_knobs([])
        cfg = self.last_run_cfg or self._collect_config()
        if str(getattr(cfg, "method", "")).upper() == "BO":
            self._set_done_scan_knobs(list(cfg.params))

        out_dir = Path(out.get("out_dir", "")) if out.get("out_dir") else self.last_out_dir
        if out_dir is None:
            self._set_status("Status: finished", state="success")
            self.current_controller = None
            return

        rows = self._load_warm_start_rows(Path(out.get("measurements_csv", self.current_measurements_csv or "")))
        self._save_summary_plots(out_dir, rows, cfg)

        best_x = dict(out.get("best_x", {}) or {})
        best_obj = float(out.get("best_objective_selected", float("nan")))
        best_knob_line = ", ".join(f"{k}={float(v):+.4f}" for k, v in best_x.items()) or "-"
        self.result_lbl.setText(
            "Result\n"
            f"Objective source: {cfg.objective_source}\n"
            f"Applied objective: {best_obj:.6f}\n"
            f"Applied knobs: {best_knob_line}\n"
            f"Saved folder: {Path(out_dir).name} (full path in log)"
        )

        if self._run_state_channels and best_x:
            machine_best = self._compute_machine_values_for_display(best_x)
            if machine_best:
                for ch in self._run_state_channels:
                    if ch in machine_best:
                        value = float(machine_best[ch])
                        self._run_current_values[ch] = value
                        self._run_final_values[ch] = value
                self._update_run_knob_state_table()

        self._set_status("Status: finished", state="success")
        self._append_log(f"Finished: objective={best_obj:.6f}")
        self._append_log(f"Applied knobs: {best_x}")
        self._append_log(f"Measurements CSV: {out.get('measurements_csv', self.current_measurements_csv)}")
        self._append_log(f"Saved summary plots under: {out_dir}")

        origin_file = out.get("machine_origin_file")
        if origin_file:
            try:
                with open(origin_file, "r", encoding="utf-8") as f:
                    self.current_machine_origin = json.load(f)
            except Exception:
                pass
        self.current_controller = None


def main():
    app = QApplication([])
    w = MainWindow()
    w.resize(1360, 980)
    w.show()
    app.exec()


if __name__ == "__main__":
    main()
