# -*- coding: utf-8 -*-
"""
IPBSM_Opt_GUI.py
Bayesian-optimization oriented GUI for IPBSM tuning.

Main tab:
- knob selection
- preset buttons
- large START / PAUSE / GET IPBSM buttons
- live modulation plot
- log

Config tab:
- BO / GP / stop settings
- per-knob sigma setup
"""

from __future__ import annotations

import json
import csv
import traceback
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QSpinBox, QDoubleSpinBox,
    QFileDialog, QGroupBox, QMessageBox, QTabWidget, QCheckBox,
    QPlainTextEdit, QLineEdit, QStackedWidget, QScrollArea, QFrame, QSizePolicy,
    QTableWidget, QTableWidgetItem, QHeaderView, QButtonGroup, QRadioButton,
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from IPBSM_Opt import (
    Optimizer, OptimizerConfig, StopFlag,
    build_gf_axiswise_fit, fit_gaussian_from_samples, plot_bo_gp_heatmap, plot_results, now_tag,
    EPICSIPBSMController, DAT_CSV_COLUMNS,
)
from IPBSM_Opt import IPBSMInterface


ZSCAN_DEFAULT_RANGE = 0.0085
ZSCAN_DEFAULT_STEP = 0.001
ZAY_PRESET_INIT_POINTS = 9
ZAY_PRESET_MAX_STEPS = 20
ZSCAN_KNOBS = ["Z scan knob"]
KNOB_ORDER = ["Ay", "Ey", "Coup2", "Y22", "Y24", "Y26", "Y66", "Y44", "Y46", "corrector 1", "Abe chamber"] + ZSCAN_KNOBS
SIGMA_KNOBS = [name for name in KNOB_ORDER if name not in ZSCAN_KNOBS]
LINEAR_KNOBS = ["Ay", "Ey", "Coup2"]
CORRECTOR_KNOBS = ["corrector 1", "Abe chamber"]
NONLINEAR_KNOBS = [
    name
    for name in KNOB_ORDER
    if name not in LINEAR_KNOBS and name not in CORRECTOR_KNOBS and name not in ZSCAN_KNOBS
]
ALL_KNOBS = list(KNOB_ORDER)
SIGMA_PRESET_30 = {
    "Ay": 0.25,
    "Ey": 0.2,
    "Coup2": 0.7,
    "Y22": 1.0,
    "Y24": 3.0,
    "Y26": 1.0,
    "Y66": 0.5,
    "Y44": 0.5,
    "Y46": 0.5,
    "corrector 1": 5.5,
    "Abe chamber": 5.0,
}
SIGMA_MODE_COMMON_KNOBS = {"Y22", "Y24", "Y26", "Y66", "Y44", "Y46"}
SIGMA_MODE_LINEAR_PRESETS = {
    "2-8": {"Ay": 0.7, "Ey": 0.6, "Coup2": 1.0},
    "30": {"Ay": 0.25, "Ey": 0.2, "Coup2": 0.7},
    "174": {"Ay": 0.08, "Ey": 0.06, "Coup2": 0.2},
}
SIGMA_MODE_FACTORS = {
    "2-8": 3.0,
    "30": 1.0,
    "174": 0.25,
}


def sigma_for_mode(mode: str, name: str) -> float:
    mode_key = str(mode)
    base = float(SIGMA_PRESET_30[name])
    if name in SIGMA_MODE_COMMON_KNOBS:
        return base
    linear_map = SIGMA_MODE_LINEAR_PRESETS.get(mode_key, {})
    if name in linear_map:
        return float(linear_map[name])
    factor = float(SIGMA_MODE_FACTORS.get(mode_key, 1.0))
    return base * factor
DEFAULT_SIGMAS = dict(SIGMA_PRESET_30)
for _z_name in ZSCAN_KNOBS:
    DEFAULT_SIGMAS[_z_name] = ZSCAN_DEFAULT_RANGE / 2.0
DEFAULT_ORIGINS = {
    name: 0.0 for name in KNOB_ORDER
}
DEFAULT_ORIGINS["Abe chamber"] = -0.825
DEFAULT_BOUNDS = {
    name: (-2.0 * DEFAULT_SIGMAS[name], 2.0 * DEFAULT_SIGMAS[name])
    for name in KNOB_ORDER
}
DEFAULT_BOUNDS["corrector 1"] = (-6.0, 10.0)
DEFAULT_BOUNDS["Abe chamber"] = (-10.0, 10.0)
EI_STOP_MODES = {
    "Aggressive": (3e-3, 2),
    "Standard": (1e-3, 2),
    "Careful": (3e-4, 3),
}


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
    if d >= 9:
        return 3000
    if d >= 7:
        return 2400
    if d >= 4:
        return 1600
    return 1000


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

    def resume_from_pause(self, should_continue: bool = True) -> None:
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
    """Combo box that opens popup when the box body is clicked."""

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            # Defer popup until release so click-to-select behaves normally.
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
        self.setWindowTitle("IPBSM Bayesian Optimization")

        self.interface = IPBSMInterface()
        self.stop_flag = StopFlag()
        self.worker: Optional[OptimizerWorker] = None
        self.last_out_dir: Optional[Path] = None
        self.last_run_cfg: Optional[OptimizerConfig] = None
        self.current_measurements_csv: Optional[Path] = None
        self.live_eval_index: List[int] = []
        self.live_modulation: List[float] = []
        self.live_best: List[float] = []
        self.live_average: List[float] = []
        self.live_average_limit: Optional[float] = None
        self.live_chosen_by: List[str] = []
        self._last_recommended_n_init = 0
        self._last_recommended_max_steps = 0
        self._last_recommended_candidate_pool = 0
        self.current_machine_origin: Optional[Dict[str, Any]] = None
        self._zscan_display_lock: Optional[Dict[str, Any]] = None
        self._active_scan_knobs: List[str] = []
        self._done_scan_knobs: List[str] = []
        self._run_selected_knobs: List[str] = []
        self._run_state_channels: List[str] = []
        self._run_initial_values: Dict[str, float] = {}
        self._run_current_values: Dict[str, float] = {}
        self._run_final_values: Dict[str, float] = {}
        self._run_best_y: float = float("-inf")
        self._run_controller: Optional[Any] = None
        self._scan_blink_on: bool = False
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
        self._apply_knob_preset(LINEAR_KNOBS)
        self._update_method_visibility()
        self._update_stop_summary()
        self._maybe_apply_recommended_max_steps()
        self._refresh_recommendations()
        self._refresh_selected_knobs_label()
        self._set_active_scan_knobs([])
        self._set_run_knob_state_params([])
        self._refresh_zscan_status_label()
        self._redraw_live_plot()

    def _set_status(self, text: str, *, state: str = "info") -> None:
        if not hasattr(self, "status_lbl"):
            return
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
        self.linear_preset_btn = QPushButton("Linear Knobs")
        self.nonlinear_preset_btn = QPushButton("Nonlinear Knobs")
        self.corrector_preset_btn = QPushButton("Corrector Knobs")
        self.zay_preset_btn = QPushButton("Z and Ay")
        self.selected_knobs_lbl = QLabel("")
        preset_button_css = (
            "QPushButton { font-size: 20px; font-weight: 700; padding: 14px 24px; min-height: 52px; }"
        )
        self.linear_preset_btn.setStyleSheet(
            preset_button_css + " QPushButton { background: #0f5c7a; color: white; }"
        )
        self.nonlinear_preset_btn.setStyleSheet(
            preset_button_css + " QPushButton { background: #9b3d1f; color: white; }"
        )
        self.corrector_preset_btn.setStyleSheet(
            preset_button_css + " QPushButton { background: #5a4aa3; color: white; }"
        )
        self.zay_preset_btn.setStyleSheet(
            preset_button_css + " QPushButton { background: #8a5a12; color: white; }"
        )
        self.selected_knobs_lbl.setStyleSheet("font-size: 24px; font-weight: 800; color: #0f172a;")
        self.selected_knobs_lbl.setVisible(False)
        preset_row.addWidget(self.linear_preset_btn)
        preset_row.addWidget(self.nonlinear_preset_btn)
        preset_row.addWidget(self.corrector_preset_btn)
        preset_row.addWidget(self.zay_preset_btn)
        preset_row.addWidget(self.selected_knobs_lbl, stretch=1)
        knob_layout.addLayout(preset_row)

        self.knob_checks: Dict[str, QCheckBox] = {}

        linear_group = QGroupBox("Linear Knobs")
        linear_layout = QGridLayout(linear_group)
        knob_layout.addWidget(linear_group)
        for idx, name in enumerate(LINEAR_KNOBS):
            box = QCheckBox(name)
            box.setProperty("knobCheck", True)
            box.setProperty("scanActive", False)
            box.setProperty("scanBlink", False)
            box.setProperty("scanDone", False)
            box.setProperty("scanWaiting", False)
            self.knob_checks[name] = box
            linear_layout.addWidget(box, idx // 3, idx % 3)

        nonlinear_group = QGroupBox("Nonlinear Knobs")
        nonlinear_layout = QGridLayout(nonlinear_group)
        knob_layout.addWidget(nonlinear_group)
        for idx, name in enumerate(NONLINEAR_KNOBS):
            box = QCheckBox(name)
            box.setProperty("knobCheck", True)
            box.setProperty("scanActive", False)
            box.setProperty("scanBlink", False)
            box.setProperty("scanDone", False)
            box.setProperty("scanWaiting", False)
            self.knob_checks[name] = box
            nonlinear_layout.addWidget(box, idx // 3, idx % 3)

        corrector_group = QGroupBox("Corrector Knobs")
        corrector_layout = QGridLayout(corrector_group)
        knob_layout.addWidget(corrector_group)
        for idx, name in enumerate(CORRECTOR_KNOBS):
            box = QCheckBox(name)
            box.setProperty("knobCheck", True)
            box.setProperty("scanActive", False)
            box.setProperty("scanBlink", False)
            box.setProperty("scanDone", False)
            box.setProperty("scanWaiting", False)
            self.knob_checks[name] = box
            corrector_layout.addWidget(box, idx // 2, idx % 2)

        zscan_group = QGroupBox("Z Scan Knob")
        zscan_layout = QHBoxLayout(zscan_group)
        knob_layout.addWidget(zscan_group)
        for name in ZSCAN_KNOBS:
            box = QCheckBox(name)
            box.setProperty("knobCheck", True)
            box.setProperty("scanActive", False)
            box.setProperty("scanBlink", False)
            box.setProperty("scanDone", False)
            box.setProperty("scanWaiting", False)
            self.knob_checks[name] = box
            zscan_layout.addWidget(box)
        self.zscan_method_lbl = QLabel("Method")
        self.zscan_method_lbl.setStyleSheet("font-weight: 700; color: #1f2937;")
        zscan_layout.addWidget(self.zscan_method_lbl)
        self.zscan_method_group = QButtonGroup(self)
        self.zscan_method_bo = QRadioButton("BO")
        self.zscan_method_gf = QRadioButton("GF")
        self.zscan_method_group.addButton(self.zscan_method_bo)
        self.zscan_method_group.addButton(self.zscan_method_gf)
        self.zscan_method_bo.setChecked(True)
        zscan_layout.addWidget(self.zscan_method_bo)
        zscan_layout.addWidget(self.zscan_method_gf)
        self.zscan_status_lbl = QLabel("")
        self.zscan_status_lbl.setStyleSheet("color: #3f4a54;")
        zscan_layout.addWidget(self.zscan_status_lbl)
        zscan_layout.addStretch(1)

        ctrl_group = QGroupBox("Run Control")
        layout.addWidget(ctrl_group)
        ctrl_layout = QVBoxLayout(ctrl_group)

        quick_row = QHBoxLayout()
        self.main_method_box = ClickOpenComboBox()
        self.main_method_box.addItems(["BO", "Sequential"])
        self.main_method_box.setEditable(True)
        self.main_method_box.lineEdit().setReadOnly(True)
        self.main_method_box.lineEdit().setAlignment(Qt.AlignmentFlag.AlignCenter)
        for i in range(self.main_method_box.count()):
            self.main_method_box.setItemData(i, Qt.AlignmentFlag.AlignCenter, Qt.ItemDataRole.TextAlignmentRole)
        self.main_method_box.setStyleSheet(
            "QComboBox { font-size: 24px; font-weight: 800; min-height: 44px; padding: 2px 10px; }"
        )
        self.main_sigma_mode_box = ClickOpenComboBox()
        self.main_sigma_mode_box.addItems(["2-8", "30", "174"])
        self.main_sigma_mode_box.setCurrentText("30")
        self.main_sigma_mode_box.setEditable(True)
        self.main_sigma_mode_box.lineEdit().setReadOnly(True)
        self.main_sigma_mode_box.lineEdit().setAlignment(Qt.AlignmentFlag.AlignCenter)
        for i in range(self.main_sigma_mode_box.count()):
            self.main_sigma_mode_box.setItemData(i, Qt.AlignmentFlag.AlignCenter, Qt.ItemDataRole.TextAlignmentRole)
        self.main_sigma_mode_box.setStyleSheet(
            "QComboBox { font-size: 24px; font-weight: 800; min-height: 44px; padding: 2px 10px; }"
        )
        method_lbl = QLabel("Method")
        method_lbl.setStyleSheet("font-size: 24px; font-weight: 900; color: #111827;")
        sigma_lbl = QLabel("Crossing Angle")
        sigma_lbl.setStyleSheet("font-size: 24px; font-weight: 900; color: #111827;")
        quick_row.addWidget(method_lbl)
        quick_row.addWidget(self.main_method_box)
        quick_row.addSpacing(16)
        quick_row.addWidget(sigma_lbl)
        quick_row.addWidget(self.main_sigma_mode_box)
        quick_row.addStretch(1)
        ctrl_layout.addLayout(quick_row)

        button_row = QHBoxLayout()

        self.run_btn = QPushButton("START")
        self.stop_btn = QPushButton("PAUSE")
        self.reset_initial_btn = QPushButton("Reset To Initial")
        self.stop_btn.setEnabled(False)

        big_button_css = (
            "QPushButton { font-size: 22px; font-weight: 700; padding: 16px 28px; min-height: 56px; }"
        )
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

        plot_group = QGroupBox("Main View")
        layout.addWidget(plot_group, stretch=1)
        plot_layout = QVBoxLayout(plot_group)
        self.main_view_stack = QStackedWidget()
        plot_layout.addWidget(self.main_view_stack)

        live_view = QWidget()
        live_layout = QVBoxLayout(live_view)
        live_layout.setContentsMargins(0, 0, 0, 0)
        self.fig = Figure(figsize=(9, 6), tight_layout=True)
        self.canvas = FigureCanvas(self.fig)
        self.ax_live = self.fig.add_subplot(211)
        self.ax_live_avg = self.fig.add_subplot(212, sharex=self.ax_live)
        live_layout.addWidget(self.canvas)
        self.main_view_stack.addWidget(live_view)

        self.gallery_scroll = QScrollArea()
        self.gallery_scroll.setWidgetResizable(True)
        self.gallery_widget = QWidget()
        self.gallery_layout = QVBoxLayout(self.gallery_widget)
        self.gallery_layout.setContentsMargins(0, 0, 0, 0)
        self.gallery_layout.setSpacing(10)
        self.gallery_plots_widget = QWidget()
        self.gallery_grid = QGridLayout(self.gallery_plots_widget)
        self.gallery_grid.setSpacing(8)
        self.gallery_layout.addWidget(self.gallery_plots_widget)
        self.gallery_scroll.setWidget(self.gallery_widget)
        self.main_view_stack.addWidget(self.gallery_scroll)

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
        self.gallery_layout.addWidget(state_group)
        self.gallery_layout.addStretch(1)

    def _build_config_tab(self):
        layout = QVBoxLayout(self.config_tab)
        layout.setSpacing(10)

        cfg_group = QGroupBox("Optimization Settings")
        layout.addWidget(cfg_group)
        form = QFormLayout(cfg_group)
        form.setContentsMargins(14, 12, 14, 12)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(8)
        self.cfg_form = form

        self.method_box = QComboBox()
        self.method_box.addItems(["BO", "Sequential"])
        self.method_box.setCurrentText("BO")

        self.acq_box = QComboBox()
        self.acq_box.addItems(["EI"])
        self.acq_box.setEnabled(False)

        self.kernel_box = QComboBox()
        self.kernel_box.addItems(["matern52", "matern32"])
        self.kernel_box.setCurrentText("matern52")

        self.bounds_sigma_mult = QDoubleSpinBox()
        self.bounds_sigma_mult.setRange(0.5, 10.0)
        self.bounds_sigma_mult.setDecimals(2)
        self.bounds_sigma_mult.setValue(2.0)

        self.knob_step = QDoubleSpinBox()
        self.knob_step.setRange(0.001, 1.0)
        self.knob_step.setDecimals(3)
        self.knob_step.setSingleStep(0.01)
        self.knob_step.setValue(0.01)

        self.zscan_range = QDoubleSpinBox()
        self.zscan_range.setRange(0.0001, 1.0)
        self.zscan_range.setDecimals(4)
        self.zscan_range.setSingleStep(0.0005)
        self.zscan_range.setValue(ZSCAN_DEFAULT_RANGE)

        self.zscan_step = QDoubleSpinBox()
        self.zscan_step.setRange(0.0001, 1.0)
        self.zscan_step.setDecimals(4)
        self.zscan_step.setSingleStep(0.0001)
        self.zscan_step.setValue(ZSCAN_DEFAULT_STEP)

        self.output_dir_edit = QLineEdit(str(Path("Data").resolve()))
        self.output_dir_browse_btn = QPushButton("Browse...")

        self.n_init = QSpinBox()
        self.n_init.setRange(1, 200)
        self.n_init.setValue(recommended_initial_points(len(LINEAR_KNOBS)))

        self.n_init_hint_lbl = QLabel("")
        self.max_steps_hint_lbl = QLabel("")
        self.candidate_pool_hint_lbl = QLabel("")

        self.bo_max_steps = QSpinBox()
        self.bo_max_steps.setRange(1, 999)
        self.bo_max_steps.setValue(60)

        self.gf_axis_max_steps = QSpinBox()
        self.gf_axis_max_steps.setRange(3, 999)
        self.gf_axis_max_steps.setValue(7)

        self.n_cand = QSpinBox()
        self.n_cand.setRange(100, 50000)
        self.n_cand.setValue(6000)

        self.stop_modulation_enabled = QCheckBox("Enable")
        self.stop_modulation_enabled.setChecked(False)
        self.stop_modulation = QDoubleSpinBox()
        self.stop_modulation.setRange(0.0, 1.0)
        self.stop_modulation.setDecimals(4)
        self.stop_modulation.setValue(0.75)
        self.stop_modulation.setEnabled(False)

        self.stop_sigma_ratio = QDoubleSpinBox()
        self.stop_sigma_ratio.setRange(1e-3, 1.0)
        self.stop_sigma_ratio.setDecimals(4)
        self.stop_sigma_ratio.setValue(0.20)

        self.avg_pause_ratio = QDoubleSpinBox()
        self.avg_pause_ratio.setRange(0.0, 1.0)
        self.avg_pause_ratio.setDecimals(3)
        self.avg_pause_ratio.setValue(0.80)

        self.ucb_beta = QDoubleSpinBox()
        self.ucb_beta.setRange(0.0, 50.0)
        self.ucb_beta.setDecimals(3)
        self.ucb_beta.setValue(2.0)

        self.ei_xi = QDoubleSpinBox()
        self.ei_xi.setRange(0.0, 1.0)
        self.ei_xi.setDecimals(4)
        self.ei_xi.setValue(0.0)

        self.gp_sig = QDoubleSpinBox()
        self.gp_sig.setRange(1e-6, 10.0)
        self.gp_sig.setDecimals(4)
        self.gp_sig.setValue(0.15)

        self.gp_noise = QDoubleSpinBox()
        self.gp_noise.setRange(1e-12, 1e-1)
        self.gp_noise.setDecimals(10)
        self.gp_noise.setValue(1e-4)

        self.ei_stop_enabled = QCheckBox("Enable")
        self.ei_stop_enabled.setChecked(True)
        self.ei_stop_mode_box = QComboBox()
        self.ei_stop_mode_box.addItems(list(EI_STOP_MODES.keys()))
        self.ei_stop_mode_box.setCurrentText("Standard")
        self.ei_stop_hint_lbl = QLabel(
            "Aggressive: early stop, Standard: balanced, Careful: keep exploring a bit longer"
        )
        for lbl in (self.n_init_hint_lbl, self.max_steps_hint_lbl, self.candidate_pool_hint_lbl, self.ei_stop_hint_lbl):
            lbl.setStyleSheet("color: #5c6670;")

        form.addRow("Method", self.method_box)
        form.addRow("Acquisition", self.acq_box)
        form.addRow("GP kernel", self.kernel_box)
        form.addRow("Bounds = +/- n sigma", self.bounds_sigma_mult)
        form.addRow("Knob step", self.knob_step)
        out_row = QWidget()
        out_layout = QHBoxLayout(out_row)
        out_layout.setContentsMargins(0, 0, 0, 0)
        out_layout.addWidget(self.output_dir_edit, stretch=1)
        out_layout.addWidget(self.output_dir_browse_btn)
        form.addRow("Save directory", out_row)
        form.addRow("Initial points", self.n_init)
        form.addRow("Recommended n_init", self.n_init_hint_lbl)
        form.addRow("BO max steps", self.bo_max_steps)
        form.addRow("Recommended max steps", self.max_steps_hint_lbl)
        form.addRow("Sequential axis max steps", self.gf_axis_max_steps)
        form.addRow("Candidate pool", self.n_cand)
        form.addRow("Recommended pool", self.candidate_pool_hint_lbl)
        self.stop_mod_row = QWidget()
        stop_layout = QHBoxLayout(self.stop_mod_row)
        stop_layout.setContentsMargins(0, 0, 0, 0)
        stop_layout.addWidget(self.stop_modulation_enabled)
        stop_layout.addWidget(self.stop_modulation)
        form.addRow("Stop modulation", self.stop_mod_row)
        form.addRow("Sequential stop sigma ratio", self.stop_sigma_ratio)
        form.addRow("Average pause ratio", self.avg_pause_ratio)
        form.addRow("UCB beta", self.ucb_beta)
        form.addRow("EI xi", self.ei_xi)
        form.addRow("GP signal variance", self.gp_sig)
        form.addRow("GP noise variance", self.gp_noise)
        self.ei_stop_row = QWidget()
        ei_stop_layout = QHBoxLayout(self.ei_stop_row)
        ei_stop_layout.setContentsMargins(0, 0, 0, 0)
        ei_stop_layout.addWidget(self.ei_stop_enabled)
        ei_stop_layout.addWidget(self.ei_stop_mode_box)
        form.addRow("EI stop rule", self.ei_stop_row)
        form.addRow("EI stop guide", self.ei_stop_hint_lbl)

        zscan_cfg_group = QGroupBox("Z Scan Settings")
        layout.addWidget(zscan_cfg_group)
        zscan_cfg_layout = QHBoxLayout(zscan_cfg_group)
        zscan_cfg_layout.setContentsMargins(14, 12, 14, 12)
        zscan_cfg_layout.addWidget(QLabel("Range (+/-)"))
        zscan_cfg_layout.addWidget(self.zscan_range)
        zscan_cfg_layout.addSpacing(12)
        zscan_cfg_layout.addWidget(QLabel("Step"))
        zscan_cfg_layout.addWidget(self.zscan_step)
        zscan_cfg_layout.addSpacing(16)
        zscan_cfg_hint = QLabel("Visible and editable here for quick tuning.")
        zscan_cfg_hint.setStyleSheet("color: #5c6670;")
        zscan_cfg_layout.addWidget(zscan_cfg_hint, stretch=1)

        sigma_group = QGroupBox("Representative Sigma Per Axis")
        layout.addWidget(sigma_group)
        sigma_layout = QVBoxLayout(sigma_group)
        sigma_layout.setContentsMargins(14, 12, 14, 12)
        sigma_layout.setSpacing(8)
        sigma_mode_row = QHBoxLayout()
        sigma_mode_row.addWidget(QLabel("Mode"))
        self.sigma_mode_box = QComboBox()
        self.sigma_mode_box.addItems(["2-8", "30", "174"])
        self.sigma_mode_box.setCurrentText("30")
        sigma_mode_row.addWidget(self.sigma_mode_box)
        sigma_mode_row.addStretch(1)
        sigma_layout.addLayout(sigma_mode_row)

        sigma_form = QFormLayout()
        sigma_form.setHorizontalSpacing(18)
        sigma_form.setVerticalSpacing(6)
        sigma_layout.addLayout(sigma_form)
        self.sigma_boxes: Dict[str, QDoubleSpinBox] = {}
        for name in SIGMA_KNOBS:
            box = QDoubleSpinBox()
            box.setRange(0.01, 100.0)
            box.setDecimals(3)
            box.setSingleStep(0.01)
            box.setValue(DEFAULT_SIGMAS[name])
            box.setMaximumWidth(120)
            self.sigma_boxes[name] = box
            sigma_form.addRow(f"sigma[{name}]", box)

        corrector_cfg_group = QGroupBox("Corrector Range / Origin")
        layout.addWidget(corrector_cfg_group)
        corrector_cfg_layout = QGridLayout(corrector_cfg_group)
        corrector_cfg_layout.setContentsMargins(14, 12, 14, 12)
        corrector_cfg_layout.setHorizontalSpacing(10)
        corrector_cfg_layout.setVerticalSpacing(8)
        self.origin_boxes: Dict[str, QDoubleSpinBox] = {}
        self.lower_bound_boxes: Dict[str, QDoubleSpinBox] = {}
        self.upper_bound_boxes: Dict[str, QDoubleSpinBox] = {}
        header_style = "font-weight: 600; color: #37414b;"
        corrector_cfg_layout.addWidget(QLabel("Knob"), 0, 0)
        hdr_origin = QLabel("Origin")
        hdr_min = QLabel("Min")
        hdr_max = QLabel("Max")
        hdr_origin.setStyleSheet(header_style)
        hdr_min.setStyleSheet(header_style)
        hdr_max.setStyleSheet(header_style)
        corrector_cfg_layout.addWidget(hdr_origin, 0, 1)
        corrector_cfg_layout.addWidget(hdr_min, 0, 2)
        corrector_cfg_layout.addWidget(hdr_max, 0, 3)

        for row_idx, name in enumerate(CORRECTOR_KNOBS, start=1):
            name_lbl = QLabel(name)
            name_lbl.setStyleSheet("font-weight: 500;")
            corrector_cfg_layout.addWidget(name_lbl, row_idx, 0)

            origin_box = QDoubleSpinBox()
            origin_box.setRange(-1000.0, 1000.0)
            origin_box.setDecimals(3)
            origin_box.setSingleStep(0.01)
            origin_box.setValue(DEFAULT_ORIGINS[name])
            origin_box.setMaximumWidth(120)
            self.origin_boxes[name] = origin_box
            corrector_cfg_layout.addWidget(origin_box, row_idx, 1)

            lo_box = QDoubleSpinBox()
            lo_box.setRange(-1000.0, 1000.0)
            lo_box.setDecimals(3)
            lo_box.setSingleStep(0.01)
            lo_box.setValue(DEFAULT_BOUNDS[name][0])
            lo_box.setMaximumWidth(120)
            self.lower_bound_boxes[name] = lo_box
            corrector_cfg_layout.addWidget(lo_box, row_idx, 2)

            hi_box = QDoubleSpinBox()
            hi_box.setRange(-1000.0, 1000.0)
            hi_box.setDecimals(3)
            hi_box.setSingleStep(0.01)
            hi_box.setValue(DEFAULT_BOUNDS[name][1])
            hi_box.setMaximumWidth(120)
            self.upper_bound_boxes[name] = hi_box
            corrector_cfg_layout.addWidget(hi_box, row_idx, 3)
        corrector_cfg_layout.setColumnStretch(4, 1)

        stop_group = QGroupBox("Stop Summary")
        layout.addWidget(stop_group)
        stop_layout = QVBoxLayout(stop_group)
        stop_layout.setContentsMargins(14, 12, 14, 12)
        self.stop_summary_lbl = QLabel("")
        self.stop_summary_lbl.setWordWrap(True)
        self.stop_summary_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        stop_layout.addWidget(self.stop_summary_lbl)

        cfg_row = QHBoxLayout()
        self.save_cfg_btn = QPushButton("Save config")
        self.load_cfg_btn = QPushButton("Load config")
        cfg_row.addWidget(self.save_cfg_btn)
        cfg_row.addWidget(self.load_cfg_btn)
        cfg_row.addStretch(1)
        layout.addLayout(cfg_row)
        layout.addStretch(1)

    def _connect_signals(self):
        self.linear_preset_btn.clicked.connect(lambda: self._apply_knob_preset(LINEAR_KNOBS))
        self.nonlinear_preset_btn.clicked.connect(lambda: self._apply_knob_preset(NONLINEAR_KNOBS))
        self.corrector_preset_btn.clicked.connect(lambda: self._apply_knob_preset(CORRECTOR_KNOBS))
        self.zay_preset_btn.clicked.connect(self._apply_zay_preset)
        for box in self.knob_checks.values():
            box.toggled.connect(self._on_knob_selection_changed)

        self.method_box.currentTextChanged.connect(self._update_method_visibility)
        self.method_box.currentTextChanged.connect(self._update_stop_summary)
        self.method_box.currentTextChanged.connect(self._sync_main_method_box)
        self.main_method_box.currentTextChanged.connect(self._sync_config_method_box)
        self.bo_max_steps.valueChanged.connect(self._update_stop_summary)
        self.gf_axis_max_steps.valueChanged.connect(self._update_stop_summary)
        self.stop_sigma_ratio.valueChanged.connect(self._update_stop_summary)
        self.stop_modulation.valueChanged.connect(self._update_stop_summary)
        self.stop_modulation_enabled.toggled.connect(self.stop_modulation.setEnabled)
        self.stop_modulation_enabled.toggled.connect(self._update_stop_summary)
        self.n_init.valueChanged.connect(self._refresh_recommendations)
        self.n_init.valueChanged.connect(self._maybe_apply_recommended_max_steps)
        self.ei_stop_enabled.toggled.connect(self._update_stop_summary)
        self.ei_stop_mode_box.currentTextChanged.connect(self._update_stop_summary)

        self.sigma_mode_box.currentTextChanged.connect(self._apply_sigma_mode)
        self.sigma_mode_box.currentTextChanged.connect(self._sync_main_sigma_mode_box)
        self.main_sigma_mode_box.currentTextChanged.connect(self._sync_config_sigma_mode_box)
        self.zscan_method_bo.toggled.connect(self._on_zscan_method_changed)
        self.zscan_method_gf.toggled.connect(self._on_zscan_method_changed)
        self.zscan_range.valueChanged.connect(self._refresh_zscan_status_label)
        self.zscan_step.valueChanged.connect(self._refresh_zscan_status_label)
        self.output_dir_browse_btn.clicked.connect(self._browse_output_dir)
        self.resume_file_browse_btn.clicked.connect(self._browse_resume_file)
        self.resume_file_clear_btn.clicked.connect(lambda: self.resume_file_edit.clear())
        self.run_btn.clicked.connect(self._on_run)
        self.stop_btn.clicked.connect(self._on_stop)
        self.reset_initial_btn.clicked.connect(self._on_reset_to_initial)
        self.save_cfg_btn.clicked.connect(self._on_save_config)
        self.load_cfg_btn.clicked.connect(self._on_load_config)

    def _selected_params(self) -> List[str]:
        return [name for name in KNOB_ORDER if self.knob_checks[name].isChecked()]

    def _apply_knob_preset(self, knob_names: List[str]):
        max_knobs = len(KNOB_ORDER)
        selected = set(knob_names[:max_knobs])
        for name, box in self.knob_checks.items():
            box.blockSignals(True)
            box.setChecked(name in selected)
            box.blockSignals(False)
        self._on_knob_selection_changed()

    def _apply_zay_preset(self):
        self._apply_knob_preset(["Ay"] + ZSCAN_KNOBS)
        self.n_init.setValue(int(ZAY_PRESET_INIT_POINTS))
        self.bo_max_steps.setValue(int(ZAY_PRESET_MAX_STEPS))
        self._update_stop_summary()

    def _on_knob_selection_changed(self):
        params = self._selected_params()
        max_knobs = len(KNOB_ORDER)
        if len(params) > max_knobs:
            keep = set(params[:max_knobs])
            for name, box in self.knob_checks.items():
                box.blockSignals(True)
                box.setChecked(name in keep)
                box.blockSignals(False)
            params = self._selected_params()

        rec = recommended_initial_points(len(params))
        if self.n_init.value() == self._last_recommended_n_init or self._last_recommended_n_init == 0:
            self.n_init.setValue(rec)
        self._last_recommended_n_init = rec
        self._maybe_apply_recommended_max_steps()
        self._refresh_recommendations()
        self._refresh_selected_knobs_label()

    def _refresh_recommendations(self):
        d = len(self._selected_params())
        rec_init = recommended_initial_points(d)
        rec_max = recommended_max_steps(d, int(self.n_init.value()))
        rec_pool = recommended_candidate_pool(d)
        self.n_init_hint_lbl.setText(f"{rec_init} points for {d}D")
        self.max_steps_hint_lbl.setText(f"{rec_max} total steps for {d}D")
        self.candidate_pool_hint_lbl.setText(f"{rec_pool} candidates for {d}D")

    def _maybe_apply_recommended_max_steps(self):
        d = len(self._selected_params())
        rec_max = recommended_max_steps(d, int(self.n_init.value()))
        if self.bo_max_steps.value() == self._last_recommended_max_steps or self._last_recommended_max_steps == 0:
            self.bo_max_steps.setValue(rec_max)
        self._last_recommended_max_steps = rec_max

        rec_pool = recommended_candidate_pool(d)
        if self.n_cand.value() == self._last_recommended_candidate_pool or self._last_recommended_candidate_pool == 0:
            self.n_cand.setValue(rec_pool)
        self._last_recommended_candidate_pool = rec_pool

    def _refresh_selected_knobs_label(self):
        params = self._selected_params()
        if params:
            self.selected_knobs_lbl.setText(f"Selected ({len(params)}/{len(KNOB_ORDER)}): {', '.join(params)}")
        else:
            self.selected_knobs_lbl.setText(f"Selected (0/{len(KNOB_ORDER)}): none")

    def _fmt_knob_value(self, value: Any) -> str:
        try:
            v = float(value)
        except Exception:
            return "-"
        if not np.isfinite(v):
            return "-"
        return f"{v:+.4f}"

    def _set_run_knob_state_params(self, channels: List[str]) -> None:
        if not hasattr(self, "knob_state_table"):
            return
        table = self.knob_state_table
        table.setRowCount(len(channels))
        for row, name in enumerate(channels):
            item_name = QTableWidgetItem(str(name))
            table.setItem(row, 0, item_name)
            for col in (1, 2):
                table.setItem(row, col, QTableWidgetItem("-"))

    def _compute_machine_values_for_display(self, knob_values: Dict[str, float]) -> Dict[str, float]:
        ctrl = self._run_controller
        fn = getattr(ctrl, "compute_machine_setpoint_values", None) if ctrl is not None else None
        if callable(fn):
            try:
                out = fn(dict(knob_values), knob_names=list(self._run_selected_knobs))
                return {str(k): float(v) for k, v in dict(out or {}).items()}
            except Exception:
                pass
        # Fallback: keep knob-level display.
        out: Dict[str, float] = {}
        for p in self._run_selected_knobs:
            out[f"knob:{p}"] = float(knob_values.get(p, self._run_initial_values.get(f"knob:{p}", float("nan"))))
        return out

    def _update_run_knob_state_table(self) -> None:
        if not hasattr(self, "knob_state_table"):
            return
        params = list(self._run_state_channels or [])
        table = self.knob_state_table
        if table.rowCount() != len(params):
            self._set_run_knob_state_params(params)
        for row, name in enumerate(params):
            init_txt = self._fmt_knob_value(self._run_initial_values.get(name, float("nan")))
            cur_txt = self._fmt_knob_value(self._run_current_values.get(name, float("nan")))
            table.setItem(row, 0, QTableWidgetItem(str(name)))
            table.setItem(row, 1, QTableWidgetItem(init_txt))
            table.setItem(row, 2, QTableWidgetItem(cur_txt))

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
        # Show WAITING immediately after START (thread startup can lag before isRunning() becomes True).
        running_now = bool(self.worker is not None)
        selected_set = set(self._run_selected_knobs if self._run_selected_knobs else self._selected_params())
        for name, box in self.knob_checks.items():
            is_active = name in active_set
            # Active state takes precedence over DONE style.
            is_done = (name in done_set) and (not is_active)
            box.setProperty("scanDone", is_done)
            is_waiting = bool(running_now and (name in selected_set) and (not is_active) and (not is_done))
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
        done_set = set(keep)
        for name, box in self.knob_checks.items():
            box.setProperty("scanDone", name in done_set)
        self._refresh_knob_state_labels()

    def _set_form_row_visible(self, form: QFormLayout, field: QWidget, visible: bool) -> None:
        lab = form.labelForField(field)
        if lab is not None:
            lab.setVisible(visible)
        field.setVisible(visible)

    def _is_sequential_method(self, method: str) -> bool:
        return str(method).upper() in {"GF", "SEQUENTIAL"}

    def _get_zscan_method(self) -> str:
        return "GF" if self.zscan_method_gf.isChecked() else "BO"

    def _set_zscan_method(self, method: str) -> None:
        desired = "GF" if str(method).upper() == "GF" else "BO"
        self.zscan_method_bo.blockSignals(True)
        self.zscan_method_gf.blockSignals(True)
        self.zscan_method_bo.setChecked(desired == "BO")
        self.zscan_method_gf.setChecked(desired == "GF")
        self.zscan_method_bo.blockSignals(False)
        self.zscan_method_gf.blockSignals(False)

    def _on_zscan_method_changed(self) -> None:
        if self.method_box.currentText().upper() == "BO":
            self._set_zscan_method("BO")

    def _apply_sigma_mode(self, mode: str):
        for name, box in self.sigma_boxes.items():
            box.setValue(sigma_for_mode(mode, name))

    def _browse_output_dir(self):
        current = self.output_dir_edit.text().strip() or str(Path("Data").resolve())
        path = QFileDialog.getExistingDirectory(self, "Select save directory", current)
        if path:
            self.output_dir_edit.setText(path)

    def _sync_main_method_box(self, method: str):
        self.main_method_box.blockSignals(True)
        self.main_method_box.setCurrentText(method)
        self.main_method_box.blockSignals(False)

    def _sync_config_method_box(self, method: str):
        self.method_box.blockSignals(True)
        self.method_box.setCurrentText(method)
        self.method_box.blockSignals(False)
        self._update_method_visibility()
        self._update_stop_summary()

    def _sync_main_sigma_mode_box(self, mode: str):
        self.main_sigma_mode_box.blockSignals(True)
        self.main_sigma_mode_box.setCurrentText(mode)
        self.main_sigma_mode_box.blockSignals(False)
        self._refresh_zscan_status_label()

    def _sync_config_sigma_mode_box(self, mode: str):
        self.sigma_mode_box.blockSignals(True)
        self.sigma_mode_box.setCurrentText(mode)
        self.sigma_mode_box.blockSignals(False)
        self._apply_sigma_mode(mode)
        self._refresh_zscan_status_label()

    def _refresh_zscan_status_label(self):
        if not hasattr(self, "zscan_status_lbl"):
            return
        mode = str(self.main_sigma_mode_box.currentText() or self.sigma_mode_box.currentText() or "30")

        locked = (
            self._zscan_display_lock
            if (self.worker is not None and self.worker.isRunning() and isinstance(self._zscan_display_lock, dict))
            else None
        )
        if locked is not None:
            axis_name = str(locked.get("axis", "M30LY"))
            pos = float(locked.get("position", float("nan")))
        else:
            axis_name = {"2-8": "M8LY", "30": "M30LY", "174": "M174LY"}.get(mode, "M30LY")
            pos = float("nan")
            try:
                getter = getattr(self.interface, "get_zscan_status", None)
                info = getter(scan_mode_label=mode) if callable(getter) else {}
                pos = float(info.get("position", float("nan")))
            except Exception:
                pos = float("nan")

        pos_txt = f"{pos:+.4f}" if np.isfinite(pos) else "n/a"

        rng = float(self.zscan_range.value()) if hasattr(self, "zscan_range") else float(ZSCAN_DEFAULT_RANGE)
        step = float(self.zscan_step.value()) if hasattr(self, "zscan_step") else float(ZSCAN_DEFAULT_STEP)
        self.zscan_status_lbl.setText(
            f"{axis_name}:({pos_txt})  Range:+/-{rng:.4f}  Step:{step:.4f}"
        )

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
        current = self.resume_file_edit.text().strip() or str(Path(self.output_dir_edit.text().strip() or "Data").resolve())
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
            param_names = header[2:mod_idx]
            dat_col_to_key = {
                "dat_modulation": "modulation",
                "dat_error": "error",
                "dat_beamsize": "beamsize",
                "dat_ebeamsize": "ebeamsize",
                "dat_average": "average",
                "dat_phase": "phase",
                "dat_filename": "filename",
                "dat_ict_average": "ict_average",
            }
            dat_indices = {col: header.index(col) for col in DAT_CSV_COLUMNS if col in header}
            chosen_by_idx = header.index("chosen_by") if "chosen_by" in header else None

            for row in reader:
                if not row or len(row) <= mod_err_idx:
                    continue
                x = {name: float(row[2 + i]) for i, name in enumerate(param_names)}
                dat = {}
                for col_name, idx in dat_indices.items():
                    raw = row[idx]
                    key = dat_col_to_key[col_name]
                    if key == "filename":
                        dat[key] = raw
                    else:
                        try:
                            dat[key] = float(raw)
                        except Exception:
                            dat[key] = float("nan")
                rows_out.append({
                    "step": int(row[0]),
                    "t_iso": str(row[1]),
                    "x": x,
                    "y": float(row[mod_idx]),
                    "y_err": float(row[mod_err_idx]),
                    "chosen_by": str(row[chosen_by_idx]) if chosen_by_idx is not None and chosen_by_idx < len(row) else "warm_start",
                    "dat": dat,
                })
        return rows_out

    def _update_method_visibility(self):
        method = self.method_box.currentText().upper()
        is_gf = self._is_sequential_method(method)
        is_bo_like = (method == "BO")
        is_bo = (method == "BO")
        self._set_form_row_visible(self.cfg_form, self.bounds_sigma_mult, False)
        self._set_form_row_visible(self.cfg_form, self.gf_axis_max_steps, is_gf)
        self._set_form_row_visible(self.cfg_form, self.stop_sigma_ratio, is_gf)
        self._set_form_row_visible(self.cfg_form, self.bo_max_steps, is_bo_like)
        self._set_form_row_visible(self.cfg_form, self.n_cand, False)
        self._set_form_row_visible(self.cfg_form, self.max_steps_hint_lbl, is_bo_like)
        self._set_form_row_visible(self.cfg_form, self.candidate_pool_hint_lbl, False)
        self._set_form_row_visible(self.cfg_form, self.n_init, not is_gf)
        self._set_form_row_visible(self.cfg_form, self.n_init_hint_lbl, not is_gf)
        self._set_form_row_visible(self.cfg_form, self.ucb_beta, False)
        self._set_form_row_visible(self.cfg_form, self.acq_box, is_bo)
        self._set_form_row_visible(self.cfg_form, self.kernel_box, False)
        self._set_form_row_visible(self.cfg_form, self.ei_xi, False)
        self._set_form_row_visible(self.cfg_form, self.gp_sig, False)
        self._set_form_row_visible(self.cfg_form, self.gp_noise, False)
        self._set_form_row_visible(self.cfg_form, self.stop_mod_row, False)
        self._set_form_row_visible(self.cfg_form, self.ei_stop_hint_lbl, is_bo)
        self._set_form_row_visible(self.cfg_form, self.ei_stop_row, is_bo)
        if is_bo:
            self._set_zscan_method("BO")
        zscan_method_enabled = is_gf
        self.zscan_method_lbl.setEnabled(zscan_method_enabled)
        self.zscan_method_bo.setEnabled(zscan_method_enabled)
        self.zscan_method_gf.setEnabled(zscan_method_enabled)
        self.main_view_stack.setCurrentIndex(1 if (is_gf or self._should_show_bo_corrector_heatmap()) else 0)

    def _update_stop_summary(self):
        method = self.method_box.currentText().upper()
        if self._is_sequential_method(method):
            txt = (
                "Sequential axis stop rule:\n"
                f"- each selected axis gets at least 3 points\n"
                f"- stop axis when mu_std <= sigma_fit x {float(self.stop_sigma_ratio.value()):.4g}\n"
                f"- or axis measured points >= {int(self.gf_axis_max_steps.value())} (init0/init+/init- already count as 3)"
            )
        else:
            ei_stop_line = ""
            if method == "BO":
                if self.ei_stop_enabled.isChecked():
                    thr, patience = EI_STOP_MODES[self.ei_stop_mode_box.currentText()]
                    ei_stop_line = (
                        f"- or EI stop mode {self.ei_stop_mode_box.currentText()} "
                        f"(EI max <= {thr:.4g} for {patience} {method} steps)\n"
                    )
                else:
                    ei_stop_line = "- EI-based stop disabled\n"
            txt = (
                "BO stop rule:\n"
                f"- total steps >= {int(self.bo_max_steps.value())}\n"
                f"- or operator / average warning stop\n"
                f"{ei_stop_line}"
            )
        self.stop_summary_lbl.setText(txt)

    def _collect_config(self) -> OptimizerConfig:
        params = self._selected_params()
        if not params:
            raise ValueError("Select at least one knob.")

        zscan_range_val = float(self.zscan_range.value())
        zscan_step_val = float(self.zscan_step.value())
        if zscan_range_val <= 0.0:
            raise ValueError("Z scan range must be positive.")
        if zscan_step_val <= 0.0:
            raise ValueError("Z scan step must be positive.")

        sigma_map = {}
        for p in params:
            if p in self.sigma_boxes:
                sigma_map[p] = float(self.sigma_boxes[p].value())
            elif p in ZSCAN_KNOBS:
                sigma_map[p] = max(zscan_step_val, 0.5 * zscan_range_val)
            else:
                sigma_map[p] = float(DEFAULT_SIGMAS.get(p, 0.5))
        knob_step = float(self.knob_step.value())
        half_range_mult = float(self.bounds_sigma_mult.value())
        origin_map = {}
        bounds = {}
        param_steps = {}
        for p in params:
            axis_step = float(zscan_step_val if p in ZSCAN_KNOBS else knob_step)
            param_steps[p] = axis_step
            if p in CORRECTOR_KNOBS:
                origin_map[p] = float(self.origin_boxes[p].value())
                lo = round(float(self.lower_bound_boxes[p].value()) / axis_step) * axis_step
                hi = round(float(self.upper_bound_boxes[p].value()) / axis_step) * axis_step
            elif p in ZSCAN_KNOBS:
                origin_map[p] = float(DEFAULT_ORIGINS.get(p, 0.0))
                lo = round((-zscan_range_val) / axis_step) * axis_step
                hi = round((+zscan_range_val) / axis_step) * axis_step
            else:
                origin_map[p] = float(DEFAULT_ORIGINS.get(p, 0.0))
                span = half_range_mult * sigma_map[p]
                lo = round((-span) / axis_step) * axis_step
                hi = round((+span) / axis_step) * axis_step
            if lo >= hi:
                raise ValueError(f"Invalid range for {p}: min must be smaller than max.")
            if not (lo <= origin_map[p] <= hi):
                raise ValueError(f"Origin for {p} must be inside its configured range.")
            bounds[p] = (float(lo), float(hi))

        mode_name = "linear" if all(p in LINEAR_KNOBS for p in params) else "custom"

        return OptimizerConfig(
            mode_name=mode_name,
            method=self.method_box.currentText(),
            acquisition=self.acq_box.currentText(),
            params=params,
            bounds=bounds,
            init_sigma=sigma_map,
            param_origins=origin_map,
            scan_mode_label=self.sigma_mode_box.currentText(),
            meas_sigma=0.01,
            expected_y_max=None,
            stop_modulation=None,
            knob_step=knob_step,
            param_steps=param_steps,
            zscan_axis_names=list(ZSCAN_KNOBS),
            zscan_method=("BO" if self.method_box.currentText().upper() == "BO" else self._get_zscan_method()),
            zscan_range=float(zscan_range_val),
            zscan_step=float(zscan_step_val),
            max_steps=int(self.bo_max_steps.value()),
            bo_max_steps=int(self.bo_max_steps.value()),
            gf_axis_max_steps=int(self.gf_axis_max_steps.value()),
            gf_axis_min_points=3,
            stop_sigma_ratio=float(self.stop_sigma_ratio.value()),
            n_init_random=int(self.n_init.value()),
            n_candidates=int(recommended_candidate_pool(len(params))),
            gp_kernel=self.kernel_box.currentText(),
            gp_length_scale=1.0,
            gp_ard_length_scales=sigma_map,
            gp_signal_var=float(self.gp_sig.value()),
            gp_noise_var=float(self.gp_noise.value()),
            ucb_beta=float(self.ucb_beta.value()),
            ei_xi=float(self.ei_xi.value()),
            bo_stop_on_low_acq=bool(self.ei_stop_enabled.isChecked()),
            bo_low_acq_threshold=float(EI_STOP_MODES[self.ei_stop_mode_box.currentText()][0]),
            bo_low_acq_patience=int(EI_STOP_MODES[self.ei_stop_mode_box.currentText()][1]),
            average_pause_ratio=float(self.avg_pause_ratio.value()),
        )

    def _set_config_to_ui(self, cfg: dict):
        params = cfg.get("params", LINEAR_KNOBS)
        self._apply_knob_preset(params)

        method_name = str(cfg.get("method", "BO") or "BO")
        if method_name.upper() == "GF":
            method_name = "Sequential"
        elif method_name.upper() in {"TRBO", "LQO", "LQF"}:
            method_name = "BO"
        self.method_box.setCurrentText(method_name)
        self.kernel_box.setCurrentText(cfg.get("gp_kernel", "matern52"))
        self.acq_box.setCurrentText(cfg.get("acquisition", "EI"))

        init_sigma = cfg.get("init_sigma", {})
        param_origins = cfg.get("param_origins", {})
        bounds = cfg.get("bounds", {})
        for name, box in self.sigma_boxes.items():
            box.setValue(float(init_sigma.get(name, DEFAULT_SIGMAS[name])))
        self._set_zscan_method(str(cfg.get("zscan_method", "BO")))
        self.zscan_range.setValue(float(cfg.get("zscan_range", ZSCAN_DEFAULT_RANGE)))
        self.zscan_step.setValue(float(cfg.get("zscan_step", ZSCAN_DEFAULT_STEP)))
        for name in CORRECTOR_KNOBS:
            self.origin_boxes[name].setValue(float(param_origins.get(name, DEFAULT_ORIGINS[name])))
            bound_pair = bounds.get(name, DEFAULT_BOUNDS[name])
            self.lower_bound_boxes[name].setValue(float(bound_pair[0]))
            self.upper_bound_boxes[name].setValue(float(bound_pair[1]))
        if "scan_mode_label" in cfg:
            self.sigma_mode_box.setCurrentText(str(cfg.get("scan_mode_label", "30")))

        save_dir = cfg.get("output_base_dir")
        if save_dir:
            self.output_dir_edit.setText(str(save_dir))

        if bounds and not param_origins:
            first_key = next(iter(bounds.keys()))
            lo, hi = bounds[first_key]
            sigma = max(float(init_sigma.get(first_key, DEFAULT_SIGMAS.get(first_key, 0.5))), 1e-9)
            mult = max(abs(float(lo)), abs(float(hi))) / sigma
            self.bounds_sigma_mult.setValue(mult)

        self.knob_step.setValue(float(cfg.get("knob_step", 0.01)))
        self.n_init.setValue(int(cfg.get("n_init_random", recommended_initial_points(len(params)))))
        self.bo_max_steps.setValue(int(cfg.get("bo_max_steps", cfg.get("max_steps", 60))))
        self.gf_axis_max_steps.setValue(int(cfg.get("gf_axis_max_steps", 7)))
        self.n_cand.setValue(int(cfg.get("n_candidates", 6000)))
        self._last_recommended_n_init = recommended_initial_points(len(params))
        self._last_recommended_max_steps = recommended_max_steps(len(params), int(self.n_init.value()))
        self._last_recommended_candidate_pool = recommended_candidate_pool(len(params))
        stop_mod = cfg.get("stop_modulation", None)
        self.stop_modulation_enabled.setChecked(stop_mod is not None)
        self.stop_modulation.setValue(float(0.75 if stop_mod is None else stop_mod))
        self.stop_sigma_ratio.setValue(float(cfg.get("stop_sigma_ratio", cfg.get("stop_mu_sigma", 0.20))))
        self.avg_pause_ratio.setValue(float(cfg.get("average_pause_ratio", 0.80)))
        self.ucb_beta.setValue(float(cfg.get("ucb_beta", 2.0)))
        self.ei_xi.setValue(float(cfg.get("ei_xi", 0.0)))
        self.gp_sig.setValue(float(cfg.get("gp_signal_var", 0.15)))
        self.gp_noise.setValue(float(cfg.get("gp_noise_var", 1e-4)))
        self.ei_stop_enabled.setChecked(bool(cfg.get("bo_stop_on_low_acq", True)))
        thr = float(cfg.get("bo_low_acq_threshold", 1e-4))
        patience = int(cfg.get("bo_low_acq_patience", 2))
        matched_mode = "Standard"
        for mode_name, pair in EI_STOP_MODES.items():
            if abs(pair[0] - thr) < 1e-12 and int(pair[1]) == patience:
                matched_mode = mode_name
                break
        self.ei_stop_mode_box.setCurrentText(matched_mode)

        self._on_knob_selection_changed()
        self._sync_sigma_mode_from_values()
        self._update_method_visibility()
        self._update_stop_summary()

    def _sync_sigma_mode_from_values(self):
        current_vals = {name: float(box.value()) for name, box in self.sigma_boxes.items()}
        matched = None
        for mode in SIGMA_MODE_FACTORS.keys():
            target = {name: sigma_for_mode(mode, name) for name in SIGMA_KNOBS}
            if all(abs(current_vals[name] - target[name]) < 1e-9 for name in SIGMA_KNOBS):
                matched = mode
                break
        self.sigma_mode_box.blockSignals(True)
        self.sigma_mode_box.setCurrentText(matched or "30")
        self.sigma_mode_box.blockSignals(False)
        self._sync_main_sigma_mode_box(matched or "30")

    def _make_controller(self, cfg: OptimizerConfig, baseline_state: Optional[Dict[str, Any]] = None):
        return EPICSIPBSMController(
            interface=self.interface,
            mode_name=cfg.mode_name,
            scan_mode_label=cfg.scan_mode_label,
            baseline_state=baseline_state,
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
            self.interface.restore_knob_origin(origin)
        except Exception as e:
            self._append_log(f"Reset to initial failed: {e}")
            QMessageBox.warning(self, "Reset failed", str(e))
            return
        lin_n = len(origin.get("linear_base_positions", {}) or {})
        cur_n = len(origin.get("nonlinear_base_currents", {}) or {})
        cor_n = len(origin.get("corrector_base_values", {}) or {})
        zscan_n = len(origin.get("zscan_base_values", {}) or {})
        self._set_status("Status: restored to initial machine state", state="success")
        self._append_log(
            f"Reset to initial completed: restored {lin_n} linear magnets, {cur_n} nonlinear current channels, "
            f"{cor_n} corrector channels, {zscan_n} z-scan channels"
        )

    def _append_log(self, line: str):
        self.log_box.appendPlainText(str(line))

    def _load_measurements_from_csv(self, csv_path: Path, cfg: OptimizerConfig):
        X, y, yerr, chosen_by, average = [], [], [], [], []
        if not csv_path.exists():
            return (
                np.zeros((0, len(cfg.params)), float),
                np.zeros((0,), float),
                np.zeros((0,), float),
                [],
                np.zeros((0,), float),
            )
        with open(csv_path, "r", encoding="utf-8") as f:
            r = csv.reader(f)
            header = next(r, None)
            if not header:
                return (
                    np.zeros((0, len(cfg.params)), float),
                    np.zeros((0,), float),
                    np.zeros((0,), float),
                    [],
                    np.zeros((0,), float),
                )
            if "modulation" not in header or "mod_err" not in header:
                return (
                    np.zeros((0, len(cfg.params)), float),
                    np.zeros((0,), float),
                    np.zeros((0,), float),
                    [],
                    np.zeros((0,), float),
                )
            mod_idx = header.index("modulation")
            mod_err_idx = header.index("mod_err")
            avg_idx = header.index("dat_average") if "dat_average" in header else None
            chosen_by_idx = header.index("chosen_by") if "chosen_by" in header else None
            csv_params = header[2:mod_idx]
            for row in r:
                if not row or len(row) <= mod_err_idx:
                    continue
                x_map = {}
                for i, name in enumerate(csv_params):
                    col = 2 + i
                    if col < len(row):
                        try:
                            x_map[name] = float(row[col])
                        except Exception:
                            x_map[name] = float("nan")
                X.append([float(x_map.get(p, float("nan"))) for p in cfg.params])
                y.append(float(row[mod_idx]))
                yerr.append(float(row[mod_err_idx]))
                chosen_by.append(
                    str(row[chosen_by_idx])
                    if chosen_by_idx is not None and chosen_by_idx < len(row)
                    else ""
                )
                if avg_idx is not None and avg_idx < len(row):
                    try:
                        average.append(float(row[avg_idx]))
                    except Exception:
                        average.append(float("nan"))
                else:
                    average.append(float("nan"))
        return (
            np.array(X, float) if X else np.zeros((0, len(cfg.params)), float),
            np.array(y, float) if y else np.zeros((0,), float),
            np.array(yerr, float) if yerr else np.zeros((0,), float),
            chosen_by,
            np.array(average, float) if average else np.zeros((0,), float),
        )

    def _lookup_average_from_csv(self, step: int) -> float:
        csv_path = self.current_measurements_csv
        if csv_path is None or (not csv_path.exists()):
            return float("nan")
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                r = csv.reader(f)
                header = next(r, None)
                if not header:
                    return float("nan")
                if "dat_average" not in header:
                    return float("nan")
                avg_idx = header.index("dat_average")
                step_idx = header.index("step") if "step" in header else 0
                latest = float("nan")
                for row in r:
                    if not row or len(row) <= max(avg_idx, step_idx):
                        continue
                    try:
                        latest = float(row[avg_idx])
                    except Exception:
                        latest = float("nan")
                    try:
                        row_step = int(float(row[step_idx]))
                    except Exception:
                        row_step = None
                    if row_step == int(step):
                        return latest
                return latest
        except Exception:
            return float("nan")

    def _should_show_bo_corrector_heatmap(self, cfg: Optional[OptimizerConfig] = None) -> bool:
        cfg = cfg or self.last_run_cfg
        if cfg is None:
            return False
        return str(getattr(cfg, "method", "")).upper() == "BO" and list(getattr(cfg, "params", [])) == CORRECTOR_KNOBS

    def _clear_gallery(self):
        while self.gallery_grid.count():
            item = self.gallery_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _populate_gallery(self, png_paths: List[Path], *, bo_main_layout: bool = False):
        self._clear_gallery()
        if not png_paths:
            return
        cols = 2
        try:
            viewport_w = int(self.gallery_scroll.viewport().width())
            viewport_h = int(self.gallery_scroll.viewport().height())
        except Exception:
            viewport_w = 0
            viewport_h = 0
        if viewport_w <= 240:
            viewport_w = 1100
        if viewport_h <= 180:
            viewport_h = 900
        # Keep plots readable even when additional widgets exist in the same scroll area.
        full_width = max(760, min(1400, viewport_w - 80))
        two_col_width = max(420, min(860, (viewport_w - 110) // 2))
        full_height = max(340, min(760, int(full_width * 0.55)))
        two_col_height = max(260, min(620, int(two_col_width * 0.62)))

        def _add_frame(
            path: Path,
            row: int,
            col: int,
            *,
            col_span: int = 1,
            width: int = 520,
            height: int = 320,
        ) -> None:
            frame = QFrame()
            frame.setFrameShape(QFrame.Shape.StyledPanel)
            v = QVBoxLayout(frame)

            lbl = QLabel(path.name)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            v.addWidget(lbl)

            img = QLabel()
            img.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pix = QPixmap(str(path))
            if not pix.isNull():
                img.setPixmap(
                    pix.scaled(
                        width,
                        height,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            else:
                img.setText("(failed to load)")
            v.addWidget(img)

            self.gallery_grid.addWidget(frame, row, col, 1, col_span)

        if bo_main_layout:
            # BO Main View policy:
            # 1) Modulation/Average first and vertically stacked
            # 2) only 1D plots in a 2-column grid
            # 3) hide any 2D / BO GP heatmap images
            mod_path = None
            avg_path = None
            one_d_paths: List[Path] = []
            for p in png_paths:
                n = p.name.lower()
                if "_2d_" in n or "bo_gp_" in n or "heatmap" in n:
                    continue
                if n.endswith("_modulation_vs_evaluation.png"):
                    mod_path = p
                    continue
                if n.endswith("_average_vs_evaluation.png"):
                    avg_path = p
                    continue
                if "_1d_" in n:
                    one_d_paths.append(p)

            r = 0
            if mod_path is not None:
                _add_frame(mod_path, r, 0, col_span=2, width=full_width, height=full_height)
                r += 1
            if avg_path is not None:
                _add_frame(avg_path, r, 0, col_span=2, width=full_width, height=full_height)
                r += 1

            one_d_paths = sorted(one_d_paths, key=lambda p: str(p))
            c = 0
            for p in one_d_paths:
                _add_frame(p, r, c, width=two_col_width, height=two_col_height)
                c += 1
                if c >= cols:
                    c = 0
                    r += 1
            return

        mod_name = "_modulation_vs_evaluation.png"
        avg_name = "_average_vs_evaluation.png"
        pair_height = max(220, min(480, int(two_col_width * 0.45)))

        r = 0
        c = 0
        i = 0
        while i < len(png_paths):
            path = png_paths[i]
            name = path.name.lower()
            pair_next = False
            if (i + 1) < len(png_paths):
                name_next = png_paths[i + 1].name.lower()
                pair_next = (
                    (name.endswith(mod_name) and name_next.endswith(avg_name))
                    or (name.endswith(avg_name) and name_next.endswith(mod_name))
                )
            if pair_next:
                if c != 0:
                    c = 0
                    r += 1
                _add_frame(path, r, 0, width=two_col_width, height=pair_height)
                _add_frame(png_paths[i + 1], r, 1, width=two_col_width, height=pair_height)
                i += 2
                c = 0
                r += 1
                continue

            _add_frame(path, r, c, width=two_col_width, height=two_col_height)
            i += 1
            c += 1
            if c >= cols:
                c = 0
                r += 1

    def _refresh_gf_gallery_live(self, *, bo_data_only_1d: bool = False):
        cfg = self.last_run_cfg
        csv_path = self.current_measurements_csv
        out_dir = self.last_out_dir
        if cfg is None or out_dir is None or csv_path is None:
            return
        X, y, yerr, chosen_by, average = self._load_measurements_from_csv(csv_path, cfg)
        if y.size == 0:
            return
        if self._is_sequential_method(cfg.method):
            fit = build_gf_axiswise_fit(cfg, X, y, chosen_by=chosen_by)
        else:
            mode_fit = "diag" if all(p in LINEAR_KNOBS for p in cfg.params) else "full"
            fit = fit_gaussian_from_samples(X, y, mode=mode_fit, ridge=cfg.ridge_fit, y_cap=cfg.expected_y_max)
        saved = plot_results(
            cfg=cfg,
            out_dir=out_dir,
            X=X,
            y=y,
            yerr=yerr,
            fit=fit,
            boot=None,
            chosen_by=chosen_by,
            average=average,
            average_pause_ratio=float(getattr(cfg, "average_pause_ratio", 0.80)),
            include_1d=True,
            bo_data_only_1d=bool(bo_data_only_1d),
        )
        png_saved = [p for p in saved if str(p).lower().endswith(".png")]
        self._populate_gallery(
            sorted(png_saved, key=lambda p: str(p)),
            bo_main_layout=bool(bo_data_only_1d and str(getattr(cfg, "method", "")).upper() == "BO"),
        )

    def _refresh_bo_corrector_gallery_live(self):
        cfg = self.last_run_cfg
        csv_path = self.current_measurements_csv
        out_dir = self.last_out_dir
        if cfg is None or out_dir is None or csv_path is None or (not self._should_show_bo_corrector_heatmap(cfg)):
            return
        X, y, _, _, _ = self._load_measurements_from_csv(csv_path, cfg)
        if y.size == 0:
            return
        saved = plot_bo_gp_heatmap(
            cfg=cfg,
            out_dir=out_dir,
            X=X,
            y=y,
        )
        png_saved = [p for p in saved if str(p).lower().endswith(".png")]
        self._populate_gallery(sorted(png_saved, key=lambda p: str(p)))

    def _reset_live_history(self):
        self.live_eval_index = []
        self.live_modulation = []
        self.live_best = []
        self.live_average = []
        self.live_average_limit = None
        self.live_chosen_by = []
        self._redraw_live_plot()

    def _prime_live_history(self, warm_start_rows: List[Dict]) -> None:
        self.live_eval_index = []
        self.live_modulation = []
        self.live_best = []
        self.live_average = []
        self.live_average_limit = None
        self.live_chosen_by = []
        best = None
        ratio = float(getattr(self.last_run_cfg, "average_pause_ratio", self.avg_pause_ratio.value()))
        for idx, item in enumerate(warm_start_rows, start=1):
            y = float(item.get("y", float("nan")))
            if not np.isfinite(y):
                continue
            best = y if best is None else max(best, y)
            step = int(item.get("step", idx))
            self.live_eval_index.append(step)
            self.live_modulation.append(y)
            self.live_best.append(best)
            self.live_chosen_by.append(str(item.get("chosen_by", "warm_start")))
            dat = dict(item.get("dat", {}) or {})
            try:
                avg = float(dat.get("average", float("nan")))
            except Exception:
                avg = float("nan")
            self.live_average.append(avg)
            if self.live_average_limit is None and np.isfinite(avg):
                self.live_average_limit = float(avg) * ratio
        self._redraw_live_plot()

    def _redraw_live_plot(self):
        self.ax_live.clear()
        self.ax_live_avg.clear()
        if self.live_eval_index:
            xs = np.asarray(self.live_eval_index, float)
            ys_mod = np.asarray(self.live_modulation, float)
            chosen = list(self.live_chosen_by)
            method = self.method_box.currentText().upper()
            span_edges = None

            if method == "BO" and len(chosen) == len(xs):
                init_idx = [
                    k for k, cb in enumerate(chosen)
                    if isinstance(cb, str) and cb.startswith("init_")
                ]
                if init_idx:
                    if xs.size >= 2:
                        left_edge = float(xs[0] - 0.5 * (xs[1] - xs[0]))
                        right_edge = float(xs[-1] + 0.5 * (xs[-1] - xs[-2]))
                    else:
                        left_edge = float(xs[0] - 0.5)
                        right_edge = float(xs[0] + 0.5)

                    init_last = max(init_idx)
                    if init_last < xs.size - 1:
                        split_edge = float(0.5 * (xs[init_last] + xs[init_last + 1]))
                    else:
                        split_edge = right_edge
                    span_edges = (left_edge, split_edge, right_edge)

                    self.ax_live.axvspan(left_edge, split_edge, color="#d8ecff", alpha=0.35, lw=0, zorder=0)
                    if split_edge < right_edge:
                        self.ax_live.axvspan(split_edge, right_edge, color="#fff0cc", alpha=0.35, lw=0, zorder=0)

            self.ax_live.plot(self.live_eval_index, self.live_modulation, marker="o", color="#1769aa", zorder=3)
            if span_edges is not None:
                left_edge, split_edge, right_edge = span_edges
                y_top = float(np.nanmax(ys_mod)) if ys_mod.size else 1.0
                if not np.isfinite(y_top):
                    y_top = 1.0
                self.ax_live.text((left_edge + split_edge) * 0.5, y_top, "Initial", ha="center", va="bottom", color="#355c7d")
                if split_edge < right_edge:
                    self.ax_live.text((split_edge + right_edge) * 0.5, y_top, "BO", ha="center", va="bottom", color="#8a5a12")
            ys_avg = np.asarray(self.live_average, float)
            n_avg = min(len(self.live_eval_index), ys_avg.size)
            if n_avg > 0:
                self.ax_live_avg.plot(
                    self.live_eval_index[:n_avg],
                    ys_avg[:n_avg],
                    marker="o",
                    color="#2e8b57",
                )
                lim = float(self.live_average_limit) if self.live_average_limit is not None else float("nan")
                if np.isfinite(lim):
                    self.ax_live_avg.axhline(
                        lim,
                        color="#8a5a12",
                        linestyle="--",
                        linewidth=1.2,
                        label=f"Pause limit: {lim:.3f}",
                    )
                    self.ax_live_avg.legend(loc="best")
        self.ax_live.set_title("IPBSM modulation vs evaluation")
        self.ax_live.set_ylabel("Modulation")
        self.ax_live.grid(True, alpha=0.3)
        self.ax_live_avg.set_title("IPBSM average vs evaluation")
        self.ax_live_avg.set_xlabel("Evaluation")
        self.ax_live_avg.set_ylabel("Average")
        self.ax_live_avg.grid(True, alpha=0.3)
        self.canvas.draw_idle()

    def _on_save_config(self):
        try:
            cfg = self._collect_config()
        except Exception as e:
            QMessageBox.warning(self, "Save config", str(e))
            return
        payload = asdict(cfg)
        payload["output_base_dir"] = self.output_dir_edit.text().strip()
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

    def _build_run_output_dir(self, base_dir: Path, tag: str, suffix: str = "ipbsm-bo") -> Path:
        year_dir = base_dir / tag[:4]
        return year_dir / f"{tag}-{suffix}"

    def _run_output_suffix(self, method: str) -> str:
        return "ipbsm-seq" if self._is_sequential_method(method) else "ipbsm-bo"

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
        output_base_dir = Path(self.output_dir_edit.text().strip() or "Data")
        out_dir = self._build_run_output_dir(
            output_base_dir,
            tag,
            suffix=self._run_output_suffix(cfg.method),
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        self.last_out_dir = out_dir
        self.last_run_cfg = cfg
        self.stop_flag = StopFlag()
        warm_start_rows: List[Dict] = []
        baseline_state: Optional[Dict[str, Any]] = None

        resume_path_text = self.resume_file_edit.text().strip()
        if resume_path_text:
            try:
                resume_path = Path(resume_path_text).expanduser().resolve()
                warm_start_rows = self._load_warm_start_rows(resume_path)
                baseline_state = self._load_resume_origin_state(resume_path)
            except Exception as e:
                QMessageBox.warning(self, "Resume file error", str(e))
                return
            if not warm_start_rows:
                QMessageBox.warning(self, "Resume file error", "Resume CSV did not contain any valid measurements.")
                return
            resume_params = list(warm_start_rows[0]["x"].keys())
            if resume_params != list(cfg.params):
                QMessageBox.warning(
                    self,
                    "Resume file mismatch",
                    "Selected knobs do not match the resume CSV.\n"
                    f"Current: {cfg.params}\n"
                    f"Resume: {resume_params}",
                )
                return
            if isinstance(baseline_state, dict) and "scan_mode_label" in baseline_state:
                resume_mode = str(baseline_state.get("scan_mode_label", ""))
                if resume_mode and resume_mode != str(cfg.scan_mode_label):
                    QMessageBox.warning(
                        self,
                        "Resume file mismatch",
                        "Selected Sigma Mode does not match the resume machine origin.\n"
                        f"Current: {cfg.scan_mode_label}\n"
                        f"Resume: {resume_mode}",
                    )
                    return

        try:
            ctrl = self._make_controller(cfg, baseline_state=baseline_state)
            baseline_state = ctrl.ensure_machine_origin(cfg.params)
            self.current_machine_origin = baseline_state
        except Exception as e:
            QMessageBox.critical(self, "Controller error", str(e))
            return

        lock_axis = {"2-8": "M8LY", "30": "M30LY", "174": "M174LY"}.get(str(cfg.scan_mode_label), "M30LY")
        lock_pos = float("nan")
        try:
            getter = getattr(self.interface, "get_zscan_status", None)
            lock_info = getter(scan_mode_label=cfg.scan_mode_label) if callable(getter) else {}
            lock_axis = str(lock_info.get("axis", lock_axis))
            lock_pos = float(lock_info.get("position", float("nan")))
        except Exception:
            lock_pos = float("nan")
        self._zscan_display_lock = {
            "mode": str(cfg.scan_mode_label),
            "axis": lock_axis,
            "position": lock_pos,
        }
        self._refresh_zscan_status_label()

        opt = Optimizer(
            controller=ctrl,
            config=cfg,
            out_dir=out_dir,
            stop_flag=self.stop_flag,
            warm_start_data=warm_start_rows,
        )
        self.current_measurements_csv = Path(opt.measurements_csv_path)
        self.worker = OptimizerWorker(opt)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.pause_requested.connect(self._on_pause_requested)

        if warm_start_rows:
            self._prime_live_history(warm_start_rows)
        else:
            self._reset_live_history()
        self._clear_gallery()
        self._set_done_scan_knobs([])
        self._run_selected_knobs = list(cfg.params)
        self._run_controller = ctrl
        self._run_state_channels = []
        self._run_initial_values = {}
        describe_fn = getattr(ctrl, "describe_machine_setpoint_channels", None)
        if callable(describe_fn):
            try:
                info = describe_fn(list(cfg.params))
                self._run_state_channels = [str(ch) for ch in list(info.get("channels", []))]
                self._run_initial_values = {str(k): float(v) for k, v in dict(info.get("initial", {})).items()}
            except Exception:
                self._run_state_channels = []
                self._run_initial_values = {}
        if not self._run_state_channels:
            self._run_state_channels = [f"knob:{p}" for p in cfg.params]
            self._run_initial_values = {f"knob:{p}": float(cfg.param_origins.get(p, 0.0)) for p in cfg.params}
        self._run_current_values = dict(self._run_initial_values)
        self._run_final_values = dict(self._run_initial_values)
        self._run_best_y = float("-inf")
        self._set_run_knob_state_params(self._run_state_channels)
        self._update_run_knob_state_table()
        self.result_lbl.setText("Result: running...")
        self.log_box.clear()
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._set_status(
            f"Status: RUNNING {cfg.method} | knobs={', '.join(cfg.params)}",
            state="running",
        )
        if self._is_sequential_method(cfg.method):
            first_axis = [cfg.params[0]] if cfg.params else []
            self._set_active_scan_knobs(first_axis)
        else:
            self._set_active_scan_knobs(list(cfg.params))
        self._append_log(
            f"Run started: method={cfg.method} acquisition={cfg.acquisition} kernel={cfg.gp_kernel} params={cfg.params}"
        )
        self._append_log(
            f"origin={cfg.param_origins} sigma={cfg.init_sigma} bounds={cfg.bounds} n_init={cfg.n_init_random} max_steps={cfg.bo_max_steps}"
        )
        self._append_log(f"Outputs: one timestamped measurements CSV and machine_origin.json will be saved under {out_dir}")
        if warm_start_rows:
            self._append_log(
                f"Warm start: loaded {len(warm_start_rows)} previous measurements from {resume_path_text}"
            )
            last_best = max(float(item.get("y", float("-inf"))) for item in warm_start_rows)
            self._append_log(f"Warm start: best previous modulation={last_best:.6f}")
            if baseline_state:
                self._append_log("Warm start: machine origin snapshot loaded from previous run")
            else:
                self._append_log("Warm start: origin snapshot file was not found, current machine state will be used as origin")
        self.tabs.setCurrentWidget(self.main_tab)
        self.worker.start()

    def _on_stop(self):
        if self.worker is None or (not self.worker.isRunning()):
            return
        self.worker.request_manual_pause()
        self._set_status("Status: pause requested after current measurement", state="paused")

    def _on_pause_requested(self, info: dict):
        reason = str(info.get("reason", ""))
        if reason == "operation_error":
            operation = str(info.get("operation", "IPBSM operation"))
            error_type = str(info.get("error_type", "Error"))
            message = str(info.get("message", ""))
            step = info.get("step", "?")
            x = info.get("x", {})
            x_txt = " ".join(f"{k}={float(v):+.3f}" for k, v in x.items()) if isinstance(x, dict) else ""

            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Operation Error")
            box.setText(
                f"{operation} failed during optimization.\n"
                f"step={step}\n"
                f"type={error_type}\n"
                f"{message}\n\n"
                f"{x_txt}"
            )
            resume_btn = box.addButton("Resume", QMessageBox.ButtonRole.AcceptRole)
            stop_btn = box.addButton("Save and End", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(resume_btn)
            box.exec()

            if self.worker is None:
                return
            if box.clickedButton() is stop_btn:
                self.stop_flag.request_stop()
                self._set_status(f"Status: stop requested after {operation} error", state="warning")
                self.worker.resume_from_pause(False)
            else:
                self._set_status(f"Status: retrying after {operation} error", state="running")
                self.worker.resume_from_pause(True)
            return

        if reason == "current_drop_to_zero":
            magnets = list(info.get("magnets", []))
            target = dict(info.get("target", {}))
            readback = dict(info.get("readback", {}))
            rows = []
            for mag in magnets:
                rows.append(
                    f"{mag}: target={float(target.get(mag, float('nan'))):+.4f} A, "
                    f"readback={float(readback.get(mag, float('nan'))):+.4f} A"
                )
            body = "\n".join(rows) if rows else str(info.get("message", "Current dropped near 0 A."))

            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Current Drop Detected")
            box.setText(
                "One or more nonlinear-magnet currents dropped near 0 A after setting.\n\n"
                f"{body}"
            )
            resume_btn = box.addButton("Resume", QMessageBox.ButtonRole.AcceptRole)
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
                f"y={float(info.get('y', float('nan'))):.6f}\n"
                f"best={float(info.get('best_y', float('nan'))):.6f}"
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
                self._set_status("Status: resumed", state="running")
                self.worker.resume_from_pause(True)
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
            f"threshold={threshold:.6f}"
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
            self._set_status("Status: resumed after warning", state="running")
            self.worker.resume_from_pause(True)

    def _on_progress(self, payload: dict):
        step = int(payload.get("step", 0))
        info = payload.get("info", {})
        phase = str(info.get("phase", ""))
        y = info.get("y", None)
        best_y = info.get("best_y", None)
        chosen_by = str(info.get("chosen_by", ""))
        if phase == "init" and (not chosen_by):
            # Some progress payloads for init points do not include chosen_by.
            # Normalize to an init_* tag so live BO shading can detect init region.
            chosen_by = "init_live"
        x = info.get("x", {})
        x_map = dict(x) if isinstance(x, dict) else {}

        if x_map and self._run_state_channels:
            machine_vals = self._compute_machine_values_for_display(x_map)
            if machine_vals:
                for ch in self._run_state_channels:
                    if ch in machine_vals:
                        self._run_current_values[ch] = float(machine_vals[ch])

        if y is not None and phase in ("init", "loop", "axis_finalize"):
            try:
                avg = float(info.get("average", float("nan")))
            except Exception:
                avg = float("nan")
            if not np.isfinite(avg):
                avg = self._lookup_average_from_csv(step)
            self.live_eval_index.append(step)
            self.live_modulation.append(float(y))
            self.live_best.append(float(best_y) if best_y is not None else float(y))
            self.live_average.append(float(avg))
            self.live_chosen_by.append(chosen_by)
            try:
                threshold_average = float(info.get("threshold_average", float("nan")))
            except Exception:
                threshold_average = float("nan")
            if np.isfinite(threshold_average):
                self.live_average_limit = threshold_average
            elif self.live_average_limit is None and np.isfinite(avg):
                ratio = float(getattr(self.last_run_cfg, "average_pause_ratio", self.avg_pause_ratio.value()))
                self.live_average_limit = float(avg) * ratio
            self._redraw_live_plot()
            try:
                y_now = float(y)
            except Exception:
                y_now = float("nan")
            if np.isfinite(y_now) and x_map:
                if y_now >= float(self._run_best_y):
                    self._run_best_y = y_now
                    machine_best = self._compute_machine_values_for_display(x_map)
                    if machine_best:
                        for ch in self._run_state_channels:
                            if ch in machine_best:
                                self._run_final_values[ch] = float(machine_best[ch])
        if y is not None:
            method_live = str(self.method_box.currentText()).upper()
            if self._is_sequential_method(method_live):
                axis_live = str(info.get("axis", "")).strip() or self._axis_from_chosen_by(chosen_by)
                if axis_live in self.knob_checks:
                    self._set_active_scan_knobs([axis_live])
                self._refresh_gf_gallery_live()
            elif method_live == "BO":
                if self.last_run_cfg is not None:
                    self._set_active_scan_knobs(list(self.last_run_cfg.params))
                self._refresh_gf_gallery_live(bo_data_only_1d=True)
                self.main_view_stack.setCurrentIndex(1)
        if y is not None:
            self._refresh_zscan_status_label()

        pos_txt = ""
        if isinstance(x, dict) and x:
            pos_txt = " ".join(f"{k}={float(v):+.3f}" for k, v in x.items())

        if phase == "model_fit":
            self._append_log(f"step={int(info.get('step_next', step)):02d} fitting surrogate/model")
        elif phase == "acquisition":
            self._append_log(
                f"step={int(info.get('step_next', step)):02d} evaluating acquisition max={float(info.get('max_acq', float('nan'))):.6g}"
            )
        elif phase == "measuring":
            x_txt = " ".join(f"{k}={float(v):+.3f}" for k, v in x.items()) if isinstance(x, dict) else ""
            self._append_log(
                f"step={int(step):02d} setting candidate by={chosen_by} {x_txt}".strip()
            )
        elif phase == "reuse":
            self._append_log(
                f"step={int(step):02d} reusing previous measurement from step={int(info.get('reuse_from_step', 0))}"
            )
        elif phase == "final_apply":
            x_txt = " ".join(f"{k}={float(v):+.3f}" for k, v in x.items()) if isinstance(x, dict) else ""
            final_strategy = str(info.get("final_strategy", "") or "")
            apply_label = "sequential final knobs" if final_strategy == "sequential_axis_final_mu" else "best knobs"
            self._append_log(
                f"final apply: setting {apply_label} best_y={float(info.get('best_y', float('nan'))):.6f} {x_txt}".strip()
            )
            if x_map:
                machine_final = self._compute_machine_values_for_display(x_map)
                if machine_final:
                    for ch in self._run_state_channels:
                        if ch in machine_final:
                            v = float(machine_final[ch])
                            self._run_current_values[ch] = v
                            self._run_final_values[ch] = v

        if y is not None:
            line = (
                f"step={step:02d} phase={phase} by={chosen_by} "
                f"mod={float(y):.6f} best={float(best_y) if best_y is not None else float(y):.6f} {pos_txt}"
            ).strip()
            self._append_log(line)

        if phase == "stop":
            stop_msg = f"stop reason={info.get('reason', '')}"
            if "y" in info:
                stop_msg += f" y={float(info.get('y', float('nan'))):.6f}"
            if "max_acq" in info:
                stop_msg += (
                    f" max_acq={float(info.get('max_acq', float('nan'))):.6g}"
                    f" threshold={float(info.get('threshold', float('nan'))):.6g}"
                    f" streak={int(info.get('streak', 0))}"
                )
            self._append_log(stop_msg)
        elif phase == "axis_done":
            axis_done = str(info.get("axis", "")).strip()
            if axis_done in self.knob_checks:
                done_set = set(self._done_scan_knobs)
                done_set.add(axis_done)
                self._set_done_scan_knobs(list(done_set))
        elif phase == "warn":
            self._append_log(
                f"warning reason={info.get('reason', '')} message={info.get('message', '')}"
            )

        if y is not None and (not self._is_sequential_method(self.method_box.currentText())):
            self._set_status(
                f"Status: RUNNING | step={step} | modulation={float(y):.6f} | best={float(best_y) if best_y is not None else float(y):.6f}",
                state="running",
            )
        if self._run_selected_knobs:
            self._update_run_knob_state_table()

    def _on_failed(self, msg: str):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.worker = None
        self._zscan_display_lock = None
        self._set_active_scan_knobs([])
        self._set_done_scan_knobs([])
        self._run_selected_knobs = []
        self._run_state_channels = []
        self._run_initial_values = {}
        self._run_current_values = {}
        self._run_final_values = {}
        self._run_best_y = float("-inf")
        self._run_controller = None
        self._set_run_knob_state_params([])
        self._set_status("Status: FAILED", state="error")
        self._append_log(msg)
        self._refresh_zscan_status_label()
        QMessageBox.critical(self, "Failed", msg)

    def _on_finished(self, out: dict):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.worker = None
        self._zscan_display_lock = None
        self._set_active_scan_knobs([])

        out_dir = Path(out.get("out_dir", "")) if out.get("out_dir") else self.last_out_dir
        if out_dir is None:
            self._set_status("Status: finished", state="success")
            self.current_measurements_csv = None
            self._run_controller = None
            return

        cfg = self.last_run_cfg or self._collect_config()
        if str(getattr(cfg, "method", "")).upper() == "BO":
            self._set_done_scan_knobs(list(cfg.params))
        self._run_selected_knobs = list(getattr(cfg, "params", []) or [])
        csv_path = Path(out.get("measurements_csv", "")) if out.get("measurements_csv") else (out_dir / "measurements.csv")
        X, y, yerr, chosen_by, average = self._load_measurements_from_csv(csv_path, cfg)

        if self._is_sequential_method(cfg.method):
            fit = build_gf_axiswise_fit(cfg, X, y, chosen_by=chosen_by)
        else:
            mode_fit = "diag" if all(p in LINEAR_KNOBS for p in cfg.params) else "full"
            fit = fit_gaussian_from_samples(X, y, mode=mode_fit, ridge=cfg.ridge_fit, y_cap=cfg.expected_y_max)
        saved = plot_results(
            cfg=cfg,
            out_dir=out_dir,
            X=X,
            y=y,
            yerr=yerr,
            fit=fit,
            boot=None,
            chosen_by=chosen_by,
            average=average,
            average_pause_ratio=float(getattr(cfg, "average_pause_ratio", 0.80)),
            include_1d=True,
            bo_data_only_1d=bool(str(getattr(cfg, "method", "")).upper() == "BO"),
        )
        png_saved = [p for p in saved if str(p).lower().endswith(".png")]
        if self._is_sequential_method(cfg.method):
            self._populate_gallery(sorted(png_saved, key=lambda p: str(p)))
        elif str(getattr(cfg, "method", "")).upper() == "BO":
            self._populate_gallery(sorted(png_saved, key=lambda p: str(p)), bo_main_layout=True)
            self.main_view_stack.setCurrentIndex(1)

        best_x = out.get("best_x", {})
        best_y = float(out.get("best_y", float("nan")))
        final_strategy = str(out.get("final_strategy", "") or "")
        best_apply_ok = bool(out.get("best_apply_ok", False))
        best_apply_error = str(out.get("best_apply_error", ""))
        best_knob_line = ", ".join(
            f"{k}={float(v):+.4f}" if isinstance(v, (int, float)) else f"{k}={v}"
            for k, v in dict(best_x or {}).items()
        ) or "-"
        modulation_label = "Final modulation" if final_strategy == "sequential_axis_final_mu" else "Best modulation"
        knobs_label = "Final knobs" if final_strategy == "sequential_axis_final_mu" else "Best knobs"
        self.result_lbl.setText(
            "Result\n"
            f"{modulation_label}: {best_y:.6f}\n"
            f"{knobs_label}: {best_knob_line}\n"
            f"Saved folder: {Path(out_dir).name} (full path in log)"
        )
        if self._run_state_channels and isinstance(best_x, dict):
            machine_best = self._compute_machine_values_for_display(best_x)
            if machine_best:
                for ch in self._run_state_channels:
                    if ch in machine_best:
                        v = float(machine_best[ch])
                        self._run_current_values[ch] = v
                        self._run_final_values[ch] = v
        self._update_run_knob_state_table()
        self._set_status("Status: finished", state="success")
        self._append_log(f"Finished: {modulation_label.lower()}={best_y:.6f}")
        self._append_log(f"{knobs_label}: {best_x}")
        self._append_log(f"Measurements CSV: {csv_path}")
        gf_dat_files = list(out.get("gf_scan_dat_files", []) or [])
        if gf_dat_files:
            self._append_log(f"Sequential dat exports: {len(gf_dat_files)} file(s)")
            for path in gf_dat_files:
                self._append_log(f"  {path}")
        gf_dat_error = str(out.get("gf_scan_dat_error", "") or "")
        if gf_dat_error:
            self._append_log(f"Sequential dat export failed: {gf_dat_error}")
        if best_apply_ok:
            self._append_log("Best knobs were applied to the machine before exit.")
        elif best_x:
            self._append_log(f"Best knob apply failed: {best_apply_error}")
        self._append_log(f"Saved analysis files: {len(saved)}")
        self.current_measurements_csv = None
        self._run_controller = None
        self._refresh_zscan_status_label()

def main():
    app = QApplication([])
    w = MainWindow()
    w.resize(1280, 920)
    w.show()
    app.exec()


if __name__ == "__main__":
    main()
