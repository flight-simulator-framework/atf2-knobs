"""Desktop operator UI, using the same PyQt6 stack as LinacOpt."""
from collections import deque
from datetime import datetime
from pathlib import Path
import math

import numpy as np
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal, QPointF, QRectF
from PyQt6.QtGui import QColor, QPainter, QPen, QPainterPath
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QFormLayout, QLabel, QPushButton, QGroupBox, QCheckBox, QDoubleSpinBox,
    QSpinBox, QComboBox, QLineEdit, QFileDialog, QTabWidget, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QPlainTextEdit, QMessageBox,
)

from Knobs.optimizer_workspace import OptimizerWorkspace, WORKSPACE_STYLE, TAB_STYLE
from .catalog import CATALOG, GROUPS, ICT_PVS, DR_MONITORS, DR_MODE, DR_TURN, DR_BPM_NAMES, XSR_PVS
from .core import Config, Runner, command_value, inspect
from .transport import EpicsTransport, DemoTransport

COLORS = ("#087f8c", "#2463bb", "#7157b4", "#c77814", "#cf5562", "#167b4c")


def number(value, digits=3):
    try:
        return f"{value:.{digits}f}" if math.isfinite(value) else "—"
    except (TypeError, ValueError):
        return "—"


class CurvePlot(QWidget):
    """A bounded, inexpensive plot; updates read only the local monitor cache."""
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.title = title
        self.curves = {}
        self.setMinimumHeight(170)

    def set_curves(self, curves):
        self.curves = curves
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#ffffff"))
        p.setPen(QColor("#243b53"))
        p.drawText(14, 22, self.title)
        area = QRectF(60, 48, max(1, self.width() - 78), max(1, self.height() - 76))
        arrays = [np.asarray(v, dtype=float) for v in self.curves.values()]
        finite = np.concatenate([a[np.isfinite(a)] for a in arrays]) if arrays else np.array([])
        ymin, ymax = (float(np.min(finite)), float(np.max(finite))) if finite.size else (0., 1.)
        pad = max((ymax - ymin) * .12, .01)
        ymin, ymax = ymin - pad, ymax + pad
        p.setPen(QPen(QColor("#e2e8f0"), 1))
        for i in range(4):
            y = area.top() + area.height() * i / 3
            p.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
            p.setPen(QColor("#64748b"))
            p.drawText(QRectF(0, y - 9, 53, 20), Qt.AlignmentFlag.AlignRight,
                       f"{ymax - (ymax-ymin)*i/3:.3g}")
            p.setPen(QPen(QColor("#e2e8f0"), 1))
        if not finite.size:
            p.setPen(QColor("#64748b"))
            p.drawText(area, Qt.AlignmentFlag.AlignCenter, "Waiting for measurements")
        for index, (label, data) in enumerate(self.curves.items()):
            color = QColor(COLORS[index % len(COLORS)])
            p.setPen(QPen(color, 1.7))
            path, started = QPainterPath(), False
            for i, value in enumerate(data):
                if not math.isfinite(value):
                    started = False
                    continue
                point = QPointF(area.left() + area.width() * i / max(1, len(data)-1),
                                area.bottom() - area.height() * (value-ymin)/(ymax-ymin))
                if started:
                    path.lineTo(point)
                else:
                    path.moveTo(point)
                    started = True
            p.drawPath(path)
            p.drawText(65 + index * 78, 40, label)
        p.end()


class ConnectWorker(QThread):
    ready = pyqtSignal(object, object, object)
    failed = pyqtSignal(str)

    def __init__(self, factory, config, parent=None):
        super().__init__(parent)
        self.factory, self.config = factory, config

    def run(self):
        transport = None
        try:
            transport = self.factory()
            transport.connect()
            ready, errors = inspect(transport, self.config)
            self.ready.emit(transport, ready, errors)
        except Exception as exc:
            if transport:
                transport.close()
            self.failed.emit(str(exc))


class RunWorker(QThread):
    update = pyqtSignal(dict)

    def __init__(self, runner, parent=None):
        super().__init__(parent)
        self.runner = runner
        runner.emit = self.update.emit

    def run(self):
        self.runner.run()


class MainWindow(QMainWindow):
    def __init__(self, demo=False, transport_factory=None):
        super().__init__()
        self.demo = demo
        self.factory = transport_factory or (DemoTransport if demo else EpicsTransport)
        self.transport = None
        self.connector = None
        self.worker = None
        self.prepared = {}
        self.errors = {}
        self.active_names = set()
        self._building = True
        self._closing = False
        self._awaiting_close = False
        self._trend = {k: deque(maxlen=240) for k in ICT_PVS}
        self.row_by_name = {}
        self.group_checks = {}
        self.setWindowTitle("Random Excitation — ATF Linac / BT / DR" + (" [DEMO]" if demo else ""))
        self.resize(1400, 900)
        self.setMinimumSize(1040, 690)
        self.setStyleSheet(WORKSPACE_STYLE + "QMainWindow { background: #f3f6fa; }")
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(18, 12, 18, 12)
        self.setCentralWidget(central)
        header = QHBoxLayout()
        title = QLabel("Random Excitation")
        title.setStyleSheet("font-size: 25px; font-weight: 700; color: #12324d;")
        header.addWidget(title)
        header.addStretch()
        mode = QLabel("DEMO · Synthetic signals / no EPICS" if demo else "ATF · EPICS / BigData companion")
        mode.setStyleSheet("background: #e0f2fe; color: #075985; padding: 8px 12px; border-radius: 6px;")
        header.addWidget(mode)
        outer.addLayout(header)
        description = QLabel("Randomized controls, command journal and measurement windows. DR diagnostics are read-only.")
        description.setWordWrap(True)
        outer.addWidget(description)
        self.workspace = OptimizerWorkspace(controls_width=390)
        outer.addWidget(self.workspace, 1)
        self._build_controls()
        self._build_monitor()
        self._building = False
        self.timer = QTimer(self)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self.refresh_live)
        self.timer.start()
        self._update_selected()

    def spin(self, value, minimum, maximum, decimals=0):
        box = QDoubleSpinBox() if decimals else QSpinBox()
        box.setRange(minimum, maximum)
        if decimals:
            box.setDecimals(decimals)
        box.setValue(value)
        box.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        return box

    def _build_controls(self):
        layout = self.workspace.controls_layout
        buttons = QGroupBox("Run control")
        box = QVBoxLayout(buttons)
        self.connect_button = QPushButton("Connect / read current limits")
        self.connect_button.clicked.connect(self.connect_machine)
        box.addWidget(self.connect_button)
        row = QHBoxLayout()
        self.start_button = QPushButton("START")
        self.start_button.setStyleSheet(
            "QPushButton { background: #087f8c; color: white; font-weight: 700; min-height: 35px; }"
            "QPushButton:disabled { background: #edf1f5; color: #94a3b8; }")
        self.start_button.clicked.connect(self.start_run)
        self.pause_button = QPushButton("PAUSE")
        self.pause_button.setMinimumHeight(35)
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self.pause_run)
        row.addWidget(self.start_button)
        row.addWidget(self.pause_button)
        box.addLayout(row)
        self.stop_button = QPushButton("STOP · Keep current settings")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_run)
        box.addWidget(self.stop_button)
        self.selection_label = QLabel()
        self.selection_label.setWordWrap(True)
        box.addWidget(self.selection_label)
        layout.addWidget(buttons)

        self.control_tabs = QTabWidget()
        self.control_tabs.setStyleSheet(TAB_STYLE)
        layout.addWidget(self.control_tabs)
        page, detail = QWidget(), QWidget()
        self.control_tabs.addTab(page, "Setup")
        self.control_tabs.addTab(detail, "Details")
        p = QVBoxLayout(page)
        form = QFormLayout()
        self.hours = self.spin(8, .001, 1000, 3)
        self.max_points = self.spin(0, 0, 10000000)
        self.max_points.setSpecialValueText("Time limit only")
        self.phase_range = self.spin(90, 1, 180)
        self.timing_mode = QComboBox()
        self.timing_mode.addItems(["Coarse: ±112 ns / 11.2 ns", "Fine: ±11.2 ns / 2.8 ns"])
        self.end_action = QComboBox()
        self.end_action.addItems(["Restore initial settings", "Apply best recorded settings", "Keep current settings"])
        for combo in (self.timing_mode, self.end_action):
            combo.setMinimumContentsLength(12)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.score_ict = QComboBox()
        self.score_ict.addItems(list(ICT_PVS))
        self.score_ict.setCurrentText("DR")
        for label, widget in [("Duration [h]", self.hours), ("Max. points", self.max_points),
                              ("Phase ± [deg]", self.phase_range), ("Timing", self.timing_mode),
                              ("On completion", self.end_action), ("Best-point ICT", self.score_ict)]:
            form.addRow(label, widget)
        self.phase_range.valueChanged.connect(self.reinspect)
        self.timing_mode.currentIndexChanged.connect(self.reinspect)
        p.addLayout(form)
        label = QLabel("Phase: 1° grid / Current: 0.1 A grid\nCurrent limits: parameter.A/B.\nEdit Lower / Upper to narrow the range.")
        label.setWordWrap(True)
        p.addWidget(label)
        groups = QGroupBox("Actuator groups")
        group_layout = QVBoxLayout(groups)
        for key, text in GROUPS.items():
            check = QCheckBox(text)
            check.setChecked(key != "dr")
            check.toggled.connect(lambda checked, group=key: self.select_group(group, checked))
            self.group_checks[key] = check
            group_layout.addWidget(check)
        note = QLabel("DR correctors are off by default.\nEnable for a dedicated DR response study.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #64748b;")
        group_layout.addWidget(note)
        p.addWidget(groups)
        p.addStretch()

        d = QFormLayout(detail)
        d.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.samples = self.spin(6, 1, 1000)
        self.guard = self.spin(1, 0, 30, 2)
        self.arrival_timeout = self.spin(15, 1, 120, 1)
        self.measurement_timeout = self.spin(10, 1, 120, 1)
        self.phase_tol = self.spin(.5, .001, 10, 3)
        self.current_tol = self.spin(.05, .0001, 10, 4)
        self.max_changed = self.spin(10, 1, len(CATALOG))
        self.joint_every = self.spin(4, 0, 10000)
        self.reference_every = self.spin(10, 0, 10000)
        self.seed = self.spin(20261026, 0, 2147483647)
        self.save_means = QCheckBox("Save ICT point means too")
        for label, widget in [("Fresh ICT samples", self.samples), ("Arrival guard [s]", self.guard),
                              ("Arrival timeout [s]", self.arrival_timeout), ("Measure timeout [s]", self.measurement_timeout),
                              ("Phase tolerance [deg]", self.phase_tol), ("Current tolerance [A]", self.current_tol),
                              ("Max. controls / point", self.max_changed), ("Joint every N points", self.joint_every),
                              ("Reference every N", self.reference_every), ("Random seed", self.seed),
                              ("Optional summary", self.save_means)]:
            d.addRow(label, widget)
        hint = QLabel("Interval 0 disables it. Duration includes pauses.")
        hint.setWordWrap(True)
        d.addRow(hint)
        notes = QLabel("Pause finishes the current measurement.\nResume remeasures the same point.\nSave and End applies the best recorded state.\n\nGUN/L0: command echo confirmation.\nSolenoid: internalCurrentWrite feedback.")
        notes.setWordWrap(True)
        d.addRow(notes)

        output_box = QGroupBox("Command journal folder")
        out = QVBoxLayout(output_box)
        default = (Path.cwd() / "Analysis" / "RandomExcitationDemo" if self.demo else
                   Path("/mnt/nas1/atf-data/op/Bigdata/commands") / datetime.now().strftime("%Y"))
        self.output = QLineEdit(str(default))
        out.addWidget(self.output)
        browse = QPushButton("Browse folder")
        browse.clicked.connect(self.choose_directory)
        out.addWidget(browse)
        layout.addWidget(output_box)
        self.settings_widgets = [self.control_tabs, self.output, browse]

    def _build_monitor(self):
        layout = self.workspace.monitor_layout
        ribbon = QHBoxLayout()
        self.sections = {}
        for key, title in [("phase", "GUN / RF"), ("linac", "LINAC"), ("bt", "BT"), ("dr", "DR")]:
            label = QLabel(title)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet("background: #e2e8f0; padding: 10px; border-radius: 5px; font-weight: 600;")
            self.sections[key] = label
            ribbon.addWidget(label)
        layout.addLayout(ribbon)
        self.status = QLabel("Connect and inspect ranges to begin")
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(62)
        self.status.setStyleSheet("background: #12324d; color: white; border-radius: 7px; padding: 10px; font-size: 19px; font-weight: 600;")
        layout.addWidget(self.status)
        self.active_label = QLabel("Active controls: —")
        self.active_label.setWordWrap(True)
        layout.addWidget(self.active_label)
        self.progress = QLabel("Measured points 0 · Best —")
        layout.addWidget(self.progress)
        cards = QHBoxLayout()
        self.ict_labels = {}
        for key in ICT_PVS:
            card = QGroupBox(key)
            card_layout = QVBoxLayout(card)
            value = QLabel("—")
            value.setStyleSheet("font-size: 21px; font-weight: 600;")
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            card_layout.addWidget(value)
            self.ict_labels[key] = value
            cards.addWidget(card)
        layout.addLayout(cards)
        self.diagnostic_status = QLabel("ICT: native particle-count scale. Ratios are diagnostic only.")
        self.diagnostic_status.setWordWrap(True)
        layout.addWidget(self.diagnostic_status)
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(TAB_STYLE)
        layout.addWidget(self.tabs, 1)
        page = QWidget()
        pv_layout = QVBoxLayout(page)
        self.filter_box = QComboBox()
        self.filter_box.addItem("All actuators", "")
        for key, title in GROUPS.items():
            self.filter_box.addItem(title, key)
        self.filter_box.currentIndexChanged.connect(self.filter_rows)
        filters = QHBoxLayout()
        filters.addWidget(self.filter_box, 1)
        self.follow_active = QCheckBox("Follow active")
        self.follow_active.setChecked(True)
        filters.addWidget(self.follow_active)
        pv_layout.addLayout(filters)
        self.table = QTableWidget(len(CATALOG), 9)
        self.table.setHorizontalHeaderLabels(["Use", "Control", "Live", "Target", "Lower", "Upper", "Grid", "Unit", "Status / limit source"])
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSortingEnabled(False)
        head = self.table.horizontalHeader()
        for col, width in enumerate([45, 105, 75, 75, 75, 75, 52, 45]):
            head.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(col, width)
        head.setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)
        for row, a in enumerate(CATALOG):
            self.row_by_name[a.name] = row
            for col in range(9):
                item = QTableWidgetItem("")
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.table.setItem(row, col, item)
            select = self.table.item(row, 0)
            select.setFlags(select.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            select.setCheckState(Qt.CheckState.Unchecked if a.group == "dr" else Qt.CheckState.Checked)
            self.table.item(row, 1).setText(a.name)
            self.table.item(row, 1).setToolTip("\n".join(a.writes) + f"\nFeedback: {a.read or 'command echo'}")
            self.table.item(row, 7).setText(a.unit)
            self.table.item(row, 8).setText("Not connected")
        self.table.itemChanged.connect(self._update_selected)
        pv_layout.addWidget(self.table)
        self.tabs.addTab(page, "Controls")
        self.trend = CurvePlot("ICT means per measurement · last 240 points")
        self.tabs.addTab(self.trend, "ICT history")
        dr_page = QWidget()
        dr_layout = QVBoxLayout(dr_page)
        self.dr_info = QLabel("Waiting for DR monitors. Mode and turn selectors are never written.")
        self.dr_info.setWordWrap(True)
        dr_layout.addWidget(self.dr_info)
        self.dr_plot = CurvePlot("DR BPM orbit [mm, interface conversion] / MB number order")
        dr_layout.addWidget(self.dr_plot, 1)
        self.xsr_info = QLabel("XSR: —")
        self.xsr_info.setWordWrap(True)
        dr_layout.addWidget(self.xsr_info)
        self.tabs.addTab(dr_page, "DR / XSR")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.workspace.add_log(self.log)
        self.path_label = QLabel("BigData stores raw signals. This tool journals commands and measurement windows.")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.path_label)

    def config(self):
        return Config(hours=self.hours.value(), max_points=self.max_points.value(),
            phase_half_range=self.phase_range.value(), timing_mode="coarse" if self.timing_mode.currentIndex() == 0 else "fine",
            samples=self.samples.value(), guard_seconds=self.guard.value(), arrival_timeout=self.arrival_timeout.value(),
            measurement_timeout=self.measurement_timeout.value(), phase_tolerance=self.phase_tol.value(),
            current_tolerance=self.current_tol.value(), max_changed=self.max_changed.value(),
            joint_every=self.joint_every.value(), reference_every=self.reference_every.value(), seed=self.seed.value(),
            score_ict=self.score_ict.currentText(), end_action=("initial", "best", "keep")[self.end_action.currentIndex()],
            save_ict_summary=self.save_means.isChecked())

    def selected(self):
        return [a for a in CATALOG if self.table.item(self.row_by_name[a.name], 0).checkState() == Qt.CheckState.Checked]

    def _update_selected(self, *args):
        if self._building:
            return
        selected = self.selected()
        self.selection_label.setText(f"Selected: {len(selected)} controls / Phase 1° · Current 0.1 A")
        active = bool(self.worker and self.worker.isRunning())
        ict_ready = self.transport and all(math.isfinite(self.transport.sample(p).scalar()) for p in ICT_PVS.values())
        self.start_button.setEnabled(bool(self.transport and selected and not active
                                          and ict_ready and all(a.name in self.prepared for a in selected)))

    def select_group(self, group, checked):
        if self._building:
            return
        self.table.blockSignals(True)
        for a in CATALOG:
            if a.group == group:
                item = self.table.item(self.row_by_name[a.name], 0)
                if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self._update_selected()

    def filter_rows(self):
        group = self.filter_box.currentData()
        for a in CATALOG:
            self.table.setRowHidden(self.row_by_name[a.name], bool(group and a.group != group))

    def choose_directory(self):
        path = QFileDialog.getExistingDirectory(self, "Command journal folder", self.output.text())
        if path:
            self.output.setText(path)

    def connect_machine(self):
        if self.worker and self.worker.isRunning():
            return
        if self.connector and self.connector.isRunning():
            return
        if self.transport:
            self.transport.close()
            self.transport = None
        self.start_button.setEnabled(False)
        self.connect_button.setEnabled(False)
        self.status.setText("Connecting · Reading commands and parameter.A/B limits")
        self.connector = ConnectWorker(self.factory, self.config(), self)
        self.connector.ready.connect(self._connected)
        self.connector.failed.connect(self._connect_failed)
        self.connector.finished.connect(self._connection_finished)
        self.connector.start()

    def _connection_finished(self):
        if self._awaiting_close:
            QTimer.singleShot(0, self.close)

    def _connect_failed(self, message):
        self.status.setText("Connection failed: " + message)
        self.connect_button.setEnabled(True)

    def _connected(self, transport, ready, errors):
        self.transport, self.prepared, self.errors = transport, ready, errors
        self._populate_ranges()
        self.connect_button.setEnabled(True)
        self.status.setText(f"READY · {len(ready)} verified controls / {len(errors)} unavailable")
        self.log.appendPlainText(f"Connected. Ready={len(ready)}, unavailable={len(errors)}")
        for name, error in errors.items():
            self.log.appendPlainText(f"{name}: {error}")
        self.refresh_live()
        self._update_selected()

    def reinspect(self, *args):
        if self._building or not self.transport or (self.worker and self.worker.isRunning()):
            return
        self.prepared, self.errors = inspect(self.transport, self.config())
        self._populate_ranges(only_groups=("phase", "timing"))
        self._update_selected()

    def _populate_ranges(self, only_groups=None):
        self.table.blockSignals(True)
        for a in CATALOG:
            if only_groups and a.group not in only_groups:
                continue
            row = self.row_by_name[a.name]
            p = self.prepared.get(a.name)
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            self.table.item(row, 0).setFlags(flags | (Qt.ItemFlag.ItemIsUserCheckable if p else Qt.ItemFlag.NoItemFlags))
            if p:
                for col, value in [(4, p.lower), (5, p.upper), (6, p.step)]:
                    item = self.table.item(row, col)
                    item.setText(f"{value:.12g}")
                    item.setFlags(flags | (Qt.ItemFlag.ItemIsEditable if a.kind == "current" and col in (4, 5) else Qt.ItemFlag.NoItemFlags))
                self.table.item(row, 8).setText(p.limit_source)
                self.table.item(row, 8).setToolTip(p.actuator.feedback)
            else:
                self.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
                self.table.item(row, 8).setText(self.errors.get(a.name, "Unavailable"))
        self.table.blockSignals(False)

    def start_run(self):
        if not self.transport or (self.worker and self.worker.isRunning()):
            return
        try:
            config = self.config()
            config.validate()
            overrides = {a.name: tuple(float(self.table.item(self.row_by_name[a.name], c).text()) for c in (4, 5))
                         for a in self.selected() if a.kind == "current"}
            if not self.output.text().strip():
                raise ValueError("Choose a command journal folder")
            runner = Runner(self.transport, self.selected(), config, self.output.text(), overrides)
        except ValueError as exc:
            QMessageBox.warning(self, "Check configuration", str(exc))
            return
        self.worker = RunWorker(runner, self)
        self.worker.update.connect(self._on_update)
        self.worker.finished.connect(self._run_finished)
        self.set_running(True)
        self.status.setText("Rechecking initial settings and ranges")
        self._trend = {k: deque(maxlen=240) for k in ICT_PVS}
        self.worker.start()

    def set_running(self, running):
        for widget in self.settings_widgets:
            widget.setEnabled(not running)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers if running else
                                   QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.blockSignals(True)
        for a in CATALOG:
            item = self.table.item(self.row_by_name[a.name], 0)
            if a.name in self.prepared:
                flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                item.setFlags(flags if running else flags | Qt.ItemFlag.ItemIsUserCheckable)
        self.table.blockSignals(False)
        self.connect_button.setEnabled(not running)
        self.start_button.setEnabled(not running and bool(self.transport))
        self.pause_button.setEnabled(running)
        self.pause_button.setText("PAUSE")
        self.stop_button.setEnabled(running)

    def pause_run(self):
        if self.worker and self.worker.isRunning():
            if self.worker.runner.is_paused:
                self.show_pause_choices()
            else:
                self.worker.runner.request_pause()
                self.pause_button.setEnabled(False)

    def show_pause_choices(self):
        if self._closing or not self.worker or not self.worker.runner.is_paused:
            return
        runner = self.worker.runner
        box = QMessageBox(self)
        box.setWindowTitle("Pause Requested")
        box.setText("Current measurement finished. Settings and history are retained.\n\n"
                    "Resume: remeasure this point and continue.\n"
                    "Save and End: apply the best recorded settings and finish.\n"
                    "Esc or closing this dialog resumes the run.")
        resume = box.addButton("Resume", QMessageBox.ButtonRole.AcceptRole)
        finish = box.addButton("Save and End", QMessageBox.ButtonRole.ActionRole)
        finish.setEnabled(runner.best is not None)
        box.setDefaultButton(resume)
        box.setEscapeButton(resume)
        box.exec()
        if not runner.running:
            return
        if box.clickedButton() is finish:
            runner.save_and_end()
        else:
            runner.resume()
        self.pause_button.setText("PAUSE")
        self.pause_button.setEnabled(True)

    def stop_run(self):
        if self.worker:
            self.worker.runner.stop()
            self.status.setText("Stopping · Retaining the applied settings")
            self.stop_button.setEnabled(False)

    def _on_update(self, data):
        state = data["state"]
        best = data.get("best")
        self.progress.setText(f"Measured points {data.get('visits', 0):,} · Best {number(best['score']) if best else '—'}"
                              + (f" (point {best['visit']})" if best else ""))
        if state == "started":
            self.path_label.setText("Folder: " + data["directory"])
            self.prepared.update(data["prepared"])
            self.table.blockSignals(True)
            for name, p in data["prepared"].items():
                for col, value in [(4, p.lower), (5, p.upper), (6, p.step)]:
                    self.table.item(self.row_by_name[name], col).setText(f"{value:.12g}")
            self.table.blockSignals(False)
        elif state == "writing":
            self.active_names = set(data["targets"])
            kind = GROUPS.get(data["kind"], data["kind"])
            names = list(data["targets"])
            self.status.setText(f"{kind} · Writing settings")
            self.active_label.setText("Active: " + ", ".join(names[:10]) + (f" … {len(names)} controls" if len(names) > 10 else ""))
            self.active_label.setToolTip(", ".join(names))
            for name, value in data["targets"].items():
                self.table.item(self.row_by_name[name], 3).setText(number(value, 2))
            self._highlight()
            if self.follow_active.isChecked():
                visible = [self.row_by_name[n] for n in names if not self.table.isRowHidden(self.row_by_name[n])]
                if visible:
                    self.table.scrollToItem(self.table.item(visible[0], 1))
        elif state == "command":
            self.table.item(self.row_by_name[data["name"]], 8).setText("Command sent · awaiting readback")
        elif state == "arrival":
            self.status.setText("Awaiting arrival · Checking readbacks against targets")
        elif state == "guard":
            self.status.setText(f"At target · Confirming for {data['seconds']:g} s before measurement")
            for name in self.active_names:
                self.table.item(self.row_by_name[name], 8).setText("At target · confirming")
        elif state == "measuring":
            self.status.setText(f"Measuring · Collecting {data['samples']} fresh samples per ICT")
            for name in self.active_names:
                self.table.item(self.row_by_name[name], 8).setText("At target · measuring")
            self.refresh_live()
        elif state == "point":
            for key, value in data["ict_means"].items():
                self._trend[key].append(value)
            self.trend.set_curves({k: list(v) for k, v in self._trend.items()})
            for name in self.active_names:
                self.table.item(self.row_by_name[name], 8).setText("Measured" if data["quality"] == "complete" else "Incomplete ICT samples")
            self.log.appendPlainText(f"Point {data['step_id']} · {data['kind']} · {data['quality']} · score={number(data['score'])}")
        elif state == "pause_requested":
            self.status.setText("Pause requested · Finishing the current measurement")
        elif state == "paused":
            self.status.setText("PAUSED · Settings, baseline and history retained")
            self.pause_button.setText("RESUME")
            self.pause_button.setEnabled(True)
            self.show_pause_choices()
        elif state == "finished":
            self.active_names.clear()
            self._highlight()
            self.active_label.setText("No active commands")
            self.status.setText({"complete": "Complete", "saved_best": "Finished at the best recorded settings", "stopped": "Stopped · Settings retained", "error": "Stopped on error · Check the journal"}[data["result"]])
            if data.get("error"):
                self.log.appendPlainText(data["error"])
                self.path_label.setText(data["error"] + "\nFolder: " + data.get("directory", ""))
                self.workspace.log_toggle.setChecked(True)

    def _run_finished(self):
        self.set_running(False)
        self._update_selected()
        if self._awaiting_close:
            QTimer.singleShot(0, self.close)

    def _highlight(self):
        active_sections = set()
        for a in CATALOG:
            active = a.name in self.active_names
            color = QColor("#fff0ce" if active else "#ffffff")
            for col in range(self.table.columnCount()):
                self.table.item(self.row_by_name[a.name], col).setBackground(color)
            if active:
                active_sections.add(a.group if a.group in ("phase", "bt", "dr") else "linac")
        for key, label in self.sections.items():
            label.setStyleSheet("padding: 10px; border-radius: 5px; font-weight: 600; " +
                                ("background: #087f8c; color: white;" if key in active_sections else "background: #e2e8f0;"))

    def refresh_live(self):
        if not self.transport:
            return
        import time
        now = time.monotonic()
        cache = self.transport.snapshot()
        values = {}
        for key, pv in ICT_PVS.items():
            sample = cache.get(pv)
            value = sample.scalar() if sample else math.nan
            values[key] = value
            age = now - sample.received if sample else math.inf
            self.ict_labels[key].setText(number(value))
            self.ict_labels[key].setToolTip(f"{pv}\nLast notification: {age:.1f}s ago / IOC time: {sample.timestamp if sample else 0}")
            self.ict_labels[key].setStyleSheet(f"font-size: 21px; font-weight: 600; color: {'#9a6700' if age > 2 else '#12324d'};")
        ratio = values["DR"] / values["LN0"] if values.get("LN0", 0) > 0 else math.nan
        self.diagnostic_status.setText(f"DR / LN0 = {number(ratio)} (diagnostic ratio) · Amber: no notification for >2s · Raw values")
        self.table.blockSignals(True)
        for a in CATALOG:
            try:
                value = (self.transport.sample(a.read).scalar() if a.read else command_value(a, self.transport))
            except ValueError:
                value = math.nan
            self.table.item(self.row_by_name[a.name], 2).setText(number(value, 2))
        self.table.blockSignals(False)
        sample = cache.get(DR_MONITORS)
        if sample and sample.connected:
            arr = np.asarray(sample.value, dtype=float)
            mode, turn = self.transport.sample(DR_MODE).scalar(), self.transport.sample(DR_TURN).scalar()
            self.dr_info.setText(f"DR:monitors · mode={number(mode, 0)} / turn={number(turn, 0)} · Updated {now-sample.received:.1f}s ago · Read only")
            if arr.size >= 980 and arr.size % 10 == 0:
                rows = arr.reshape(-1, 10)[[int(n[2:-1])-1 for n in DR_BPM_NAMES]]
                valid = (rows[:, 0] == 1) & np.isfinite(rows[:, 3]) & (rows[:, 3] > 0)
                self.dr_plot.set_curves({"X [mm]": np.where(valid, rows[:, 1]/1000, np.nan).tolist(),
                                         "Y [mm]": np.where(valid, rows[:, 2]/1000, np.nan).tolist()})
            else:
                self.dr_info.setText(self.dr_info.text() + " / Unexpected array size")
                self.dr_plot.set_curves({})
        else:
            self.dr_info.setText("DR:monitors disconnected / no waveform. Read-only diagnostics.")
            self.dr_plot.set_curves({})
        self.xsr_info.setText("XSR (PV native units): " + " / ".join(f"{k}={number(self.transport.sample(p).scalar())}" for k, p in XSR_PVS.items()))
        self._update_selected()

    def closeEvent(self, event):
        self._closing = True
        if self.connector and self.connector.isRunning():
            self._awaiting_close = True
            event.ignore()
            return
        if self.worker and self.worker.isRunning():
            self._awaiting_close = True
            self.worker.runner.stop()
            event.ignore()
            return
        self.timer.stop()
        if self.transport:
            self.transport.close()
        event.accept()
