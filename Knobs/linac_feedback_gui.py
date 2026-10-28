"""Active Feedback panel and cancellable worker for LinacOpt."""
from collections import deque
from dataclasses import asdict
from datetime import datetime
import csv
import json
import math
from pathlib import Path
import threading
import time
import uuid

from PyQt6.QtCore import QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
                            QGroupBox, QDoubleSpinBox, QSpinBox, QComboBox, QLabel,
                            QPushButton, QPlainTextEdit, QMessageBox, QSizePolicy,
                            QTableWidget, QTableWidgetItem, QHeaderView)
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

from Knobs.linac_feedback import FeedbackConfig, FeedbackController, RecentResponse, CURRENT_PVS, PVS, PHASE_PV, TIMING_PV
from Knobs.linac_feedback_io import FeedbackMachine
from Knobs.optimizer_workspace import (OptimizerWorkspace, WORKSPACE_STYLE, WrappingLabel,
                                      run_button_style, status_badge_style)


class FeedbackWorker(QThread):
    event_signal = pyqtSignal(dict)

    def __init__(self, config, save_dir, machine_factory=FeedbackMachine):
        super().__init__()
        self.config, self.machine_factory = config, machine_factory
        self.run_dir = Path(save_dir) / (datetime.now().strftime("%Y%m%d_%H%M%S") +
                                       "_ActiveFeedback_" + uuid.uuid4().hex[:8])
        self.cancelled = threading.Event()
        self.paused = threading.Event()
        self.pause_reason = ""
        self.stage, self.cycle_number = "Starting", 0
        self.on_pause = None
        self.response_cache = Path(save_dir) / "active_feedback_response.json"

    def stop(self):
        self.cancelled.set()
        self.paused.clear()

    def checkpoint(self):
        if self.paused.is_set() and not self.cancelled.is_set():
            if self.on_pause is not None:
                self.on_pause("Feedback paused")
            self.emit_event(dict(kind="state", state=("Auto-paused: " + self.pause_reason if self.pause_reason
                                                       else "Paused (accepted settings retained)")))
            while self.paused.is_set() and not self.cancelled.wait(.05):
                pass
            if not self.cancelled.is_set():
                self.pause_reason = ""
                self.emit_event(dict(kind="state", state="Resuming"))
        if self.cancelled.is_set():
            raise InterruptedError("Feedback stopped")

    def pause_for_loss(self, reason):
        self.pause_reason = reason
        self.paused.set()
        self.emit_event(dict(kind="auto_pause", state="Auto-paused: " + reason,
                             message="Temporary settings restored. Resolve beam loss, then press Resume."))
        self.checkpoint()

    def emit_event(self, event):
        self.stage = event.get("state", self.stage)
        self.cycle_number = event.get("cycle", self.cycle_number)
        event = dict(event, state=self.stage, cycle=self.cycle_number,
                     timestamp=datetime.now().astimezone().isoformat(timespec="milliseconds"),
                     elapsed=time.monotonic() - self.started)
        # Flush commands and observations as they occur, not only on clean exit.
        if self.journal is not None:
            self.journal.write(json.dumps(event, allow_nan=False) + "\n")
            self.journal.flush()
        if self.operations is not None and event["kind"] in ("command", "command_result"):
            for pv, target in event["settings"].items():
                self.operations_writer.writerow([event["timestamp"], self.stage, self.cycle_number,
                    event["command_id"], pv, event.get("previous", {}).get(pv), target,
                    event.get("readbacks", {}).get(pv), event["restore"], event["status"], event.get("error", "")])
            self.operations.flush()
        if event["kind"] == "response" and event.get("matrix_mm_per_A") is not None:
            try:
                temporary = self.response_cache.with_name(self.run_dir.name + "_response.tmp")
                temporary.write_text(json.dumps(dict(event["model"], saved_at=event["timestamp"]), allow_nan=False))
                temporary.replace(self.response_cache)
            except OSError as exc:
                self.emit_event(dict(kind="log", message=f"Response cache could not be saved: {exc}"))
        self.event_signal.emit(event)

    def run(self):
        self.started, self.journal, self.operations = time.monotonic(), None, None
        machine = None
        try:
            self.run_dir.mkdir(parents=True, exist_ok=False)
            (self.run_dir / "config.json").write_text(json.dumps(asdict(self.config), indent=2))
            self.journal = (self.run_dir / "events.jsonl").open("w")
            self.operations = (self.run_dir / "operations.csv").open("w", newline="")
            self.operations_writer = csv.writer(self.operations)
            self.operations_writer.writerow(["TimestampISO", "Stage", "Cycle", "CommandID", "PV",
                                              "Before", "Requested", "Readback", "Restore", "Status", "Error"])
            self.operations.flush()
            self.emit_event(dict(kind="log", message=f"Run data: {self.run_dir}"))
            machine = self.machine_factory(self.config, self.cancelled.is_set, self.checkpoint, self.emit_event)
            controller = FeedbackController(machine, self.config, machine.limits(), self.emit_event)
            self.on_pause = controller.reset_baseline_candidate
            if self.response_cache.exists():
                try:
                    controller.response = RecentResponse.from_snapshot(json.loads(self.response_cache.read_text()))
                    controller.response_needs_refresh = True
                    self.emit_event(dict(kind="log", message="Stored response loaded; refresh with 0.01 A probes before orbit correction."))
                    controller.report_response()
                except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                    self.emit_event(dict(kind="log", message=f"Stored response unavailable; new calibration required: {exc}"))
            controller.run(pause_for_loss=self.pause_for_loss)
        except InterruptedError:
            self.emit_event(dict(kind="state", state="Stopped; accepted settings retained"))
        except Exception as exc:
            # Even a journal/storage failure must reach the UI.
            try:
                self.emit_event(dict(kind="error", message=str(exc)))
            except Exception:
                self.event_signal.emit(dict(kind="error", message=str(exc)))
        finally:
            self.on_pause = None
            try:
                if machine is not None:
                    machine.close()
            finally:
                if self.journal is not None:
                    self.journal.close()
                if self.operations is not None:
                    self.operations.close()


class FeedbackPanel(QWidget):
    busy_changed = pyqtSignal(bool)

    def __init__(self, save_dir, main_busy, parent=None):
        super().__init__(parent)
        self.save_dir, self.main_busy = save_dir, main_busy
        self.worker = None
        self.points, self.orbits = deque(maxlen=2000), deque(maxlen=2000)
        self.reference = None
        self.initial_transmission = None
        self.transmission_baseline = None
        self.initial_settings = {}
        self.degradation_threshold = None
        self.latest_transmission = None
        self.average_result = None
        self.averages = deque(maxlen=2000)
        self.dirty = False
        self.setStyleSheet(WORKSPACE_STYLE)
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        self.workspace = OptimizerWorkspace(controls_width=480)
        root.addWidget(self.workspace)
        left, monitor = self.workspace.controls_layout, self.workspace.monitor_layout
        left.setSpacing(10)
        run_group = QGroupBox("Run Control")
        run_layout = QVBoxLayout(run_group)
        run_layout.addWidget(WrappingLabel("Timing tracking • Orbit drift → correctors + timing • Low transmission with normal orbit → CM8L phase"))
        note = WrappingLabel("Response stored internally: first calibration 0.05 A, refresh 0.01 A. ICT ≤ 0 pauses feedback; press RESUME after recovery.")
        note.setStyleSheet("font-size: 12px; color: #475569;")
        run_layout.addWidget(note)
        row = QHBoxLayout()
        self.start_button, self.pause_button, self.stop_button = (QPushButton(s) for s in ("START", "PAUSE", "STOP"))
        for button, role in zip((self.start_button, self.pause_button, self.stop_button), ("start", "pause", "stop")):
            button.setStyleSheet(run_button_style(role))
            row.addWidget(button)
        run_layout.addLayout(row)
        left.addWidget(run_group)
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.start_button.clicked.connect(self.start)
        self.pause_button.clicked.connect(self.pause)
        self.stop_button.clicked.connect(self.stop)

        self.settings = QGroupBox("Feedback Settings")
        form = QFormLayout(self.settings)
        form.setVerticalSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.fields = {}
        defaults = FeedbackConfig()
        descriptions = {
            "settle_sec": "Wait after each PV write before measuring. Default: 2 s.",
            "interval_sec": "Wait after a full feedback cycle before starting the next timing search. This is additional to the wait after each write.",
            "min_gain": "Minimum absolute transmission improvement to accept a local search step. 0 disables the deadband; 0.001 means 0.1 percentage point. Phase recovery uses the separate drop threshold.",
            "degradation_percent": "Absolute drop in percentage points: threshold = recovery baseline − drop / 100. Default: 80% → 75%. The baseline rises after sustained improvement and never decreases.",
            "baseline_hold_sec": "Raise the recovery baseline after improvement lasts this long AND at least 3 complete operating-point means confirm it. Uses their lowest mean; probes are excluded. Pause, beam loss or orbit recovery restarts confirmation.",
        }
        for name, label, lower, upper, decimals, step in (
                ("orbit_threshold_um", "Orbit threshold [µm]", 1., 10000., 1, 10.),
                ("settle_sec", "Settle after write [s]", 0., 120., 2, .5),
                ("interval_sec", "Wait between cycles [s]", 0., 600., 1, 1.),
                ("degradation_percent", "Transmission drop [pp]", 0., 99., 1, 1.),
                ("baseline_hold_sec", "Raise baseline after [s]", 0., 3600., 0, 30.),
                ("min_gain", "Search deadband [ratio]", 0., .1, 5, .001),
                ("timing_range_ns", "Timing range from start [±ns]", 2.8, 1120., 1, 2.8),
                ("phase_range_deg", "CM8L range from start [±°]", .5, 90., 1, .5),
                ("current_range_A", "Corrector range from start [±A]", .01, 2., 2, .01)):
            box = QDoubleSpinBox()
            box.setRange(lower, upper)
            box.setDecimals(decimals)
            box.setSingleStep(step)
            box.setValue(getattr(defaults, name))
            box.setMinimumHeight(26)
            box.setToolTip(descriptions.get(name, ""))
            form.addRow(label, box)
            self.fields[name] = box
            if name in ("interval_sec", "min_gain"):
                help_text = ("Wait between cycles: delay after one full loop, before the next timing search."
                             if name == "interval_sec" else
                             "Search deadband: minimum improvement for a step. Phase recovery uses the drop threshold above.")
                help_label = WrappingLabel(help_text)
                help_label.setStyleSheet("font-size: 12px; color: #475569;")
                form.addRow(help_label)
        average_count = QSpinBox()
        average_count.setRange(1, 100)
        average_count.setValue(defaults.degradation_samples)
        average_count.setToolTip("Average new measurements at the timing optimum. Temporary probe measurements are excluded.")
        self.fields["degradation_samples"] = average_count
        form.addRow("Transmission average [measurements]", average_count)
        self.downstream = QComboBox()
        self.downstream.addItems(["DR", "LNE", "BTM", "BTE"])
        form.addRow("Transmission / LN0", self.downstream)
        left.addWidget(self.settings)
        self.status = QLabel("Status: IDLE")
        self.status.setObjectName("statusBadge")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setMinimumHeight(48)
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        self.status.setStyleSheet(status_badge_style("idle"))
        monitor.addWidget(self.status)
        self.values = WrappingLabel("Initial reference is captured when START is pressed.")
        self.values.setStyleSheet("font-size: 13px; font-weight: 700; color: #0f172a; background: #ecf5ff; "
                                 "border: 1px solid #bfdbfe; border-radius: 8px; padding: 6px 8px;")
        self.values.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        current_group = QGroupBox("Parameter Changes from Start")
        current_layout = QVBoxLayout(current_group)
        self.current_values = WrappingLabel("Live settings include temporary probes. Offsets are relative to START.")
        self.current_values.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        current_layout.addWidget(self.current_values)
        self.parameter_table = QTableWidget(len(PVS), 4)
        self.parameter_table.setHorizontalHeaderLabels(["Parameter", "Initial", "Current (live)", "Δ from start"])
        self.parameter_table.verticalHeader().hide()
        self.parameter_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.parameter_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.parameter_table.setAlternatingRowColors(True)
        self.parameter_table.verticalHeader().setDefaultSectionSize(22)
        self.parameter_table.setFixedHeight(162)
        for row, name in enumerate(("Timing [ns]", "CM8L phase [°]") + tuple(f"{pv.split(':')[0]} [A]" for pv in CURRENT_PVS)):
            self.parameter_table.setItem(row, 0, QTableWidgetItem(name))
        current_layout.addWidget(self.parameter_table)
        response_group = QGroupBox("Learned Response [mm/A]")
        self.response_group = response_group
        response_group.setToolTip("Stored in the save folder and reused across runs. Refreshed when orbit correction starts. Phase changes and beam loss retain the matrix and require a refresh before reuse.")
        response_layout = QVBoxLayout(response_group)
        self.response_table = QTableWidget(4, 4)
        self.response_table.setHorizontalHeaderLabels([pv.split(":")[0] for pv in CURRENT_PVS])
        self.response_table.setVerticalHeaderLabels(["ML8L x", "ML10L x", "ML8L y", "ML10L y"])
        self.response_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.response_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.response_table.setAlternatingRowColors(True)
        self.response_table.setMinimumHeight(165)
        self.response_table.setMaximumHeight(180)
        self.response_table.setStyleSheet("QTableWidget { background: white; alternate-background-color: #f5f8fc; "
                                         "gridline-color: #dce3eb; border: 1px solid #dce3eb; } "
                                         "QHeaderView::section { background: #ecf5ff; color: #172b4d; padding: 4px; border: none; }")
        response_layout.addWidget(self.response_table)
        left.addWidget(response_group)
        plot_group = QGroupBox("Live Plots")
        plot_layout = QVBoxLayout(plot_group)
        monitor.addWidget(plot_group, stretch=1)
        self.figure = Figure(figsize=(8, 6), layout="constrained")
        self.axes = self.figure.subplots(2, 1)
        self.axes[0].set_ylabel("Transmission")
        self.axes[1].set_ylabel("Orbit change [µm]")
        self.axes[1].set_xlabel("Elapsed [s]")
        for axis in self.axes:
            axis.grid(alpha=.25)
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setMinimumHeight(180)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        plot_layout.addWidget(self.canvas, 1)
        monitor.addWidget(current_group)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.workspace.add_log(self.log)
        monitor.addWidget(self.values)
        self.plot_timer = QTimer(self)
        self.plot_timer.setInterval(500)
        self.plot_timer.timeout.connect(self.draw_plot)

    def set_status(self, text, state="running"):
        self.status.setText(text)
        self.status.setStyleSheet(status_badge_style(state))

    @property
    def active(self):
        # Keep the interlock until the queued finished handler has run.
        return self.worker is not None

    def config(self):
        return FeedbackConfig(**{key: box.value() for key, box in self.fields.items()},
                              downstream=self.downstream.currentText())

    def start(self):
        if self.active or self.main_busy():
            QMessageBox.warning(self, "Busy", "Stop the current optimizer/feedback run first.")
            return
        self.worker = FeedbackWorker(self.config(), self.save_dir())
        self.points.clear()
        self.orbits.clear()
        self.averages.clear()
        self.reference = self.initial_transmission = self.transmission_baseline = None
        self.initial_settings = {}
        self.degradation_threshold = self.latest_transmission = self.average_result = None
        for row in range(len(PVS)):
            for col in range(1, 4):
                self.parameter_table.setItem(row, col, QTableWidgetItem("—"))
        self.response_table.clearContents()
        self.log.clear()
        self.failed = False
        self.settings.setEnabled(False)
        self.start_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.stop_button.setEnabled(True)
        self.pause_button.setText("PAUSE")
        self.set_status("Starting; capturing new reference")
        self.worker.event_signal.connect(self.on_event)
        self.worker.finished.connect(self.finished)
        self.busy_changed.emit(True)
        self.plot_timer.start()
        self.worker.start()

    def pause(self):
        if self.worker is None:
            return
        if self.worker.paused.is_set():
            self.worker.paused.clear()
            self.pause_button.setText("PAUSE")
            self.set_status("Resuming feedback")
        else:
            self.worker.paused.set()
            self.pause_button.setText("RESUME")
            self.set_status("Pause requested; completing current probe", "paused")

    def stop(self):
        if self.worker is not None:
            self.worker.stop()
            self.pause_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.set_status("Stopping; restoring temporary probes", "paused")

    def shutdown(self):
        if self.worker is None:
            return True
        self.worker.stop()
        return self.worker.wait(15000)

    def finished(self):
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.deleteLater()
        self.plot_timer.stop()
        self.draw_plot()
        self.settings.setEnabled(True)
        self.start_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.busy_changed.emit(False)

    def on_event(self, event):
        kind = event["kind"]
        elapsed = event.get("elapsed", 0.)
        if kind == "state":
            paused = event["state"].lower().startswith(("paused", "auto-paused", "stopped"))
            prefix = "" if paused else f"RUNNING | Cycle {event.get('cycle', 0)} | "
            self.set_status(prefix + event["state"], "paused" if paused else "running")
        elif kind == "activity":
            detail = event["detail"]
            if "remaining_sec" in event:
                detail += f" — {event['remaining_sec']:.1f} s remaining"
            self.set_status(f"RUNNING | Cycle {event.get('cycle', 0)} | {event.get('state', 'Working')}\n{detail}")
        elif kind == "waiting":
            self.set_status(f"RUNNING | Cycle {event.get('cycle', 0)} | Waiting\n"
                            f"Next timing scan in {math.ceil(event['remaining_sec'])} s • BPM monitoring active")
        elif kind == "auto_pause":
            self.set_status(event["state"] + "\n" + event["message"], "paused")
            self.pause_button.setText("RESUME")
        elif kind == "error":
            self.failed = True
            self.set_status("Failed: " + event["message"], "error")
        elif kind == "reference":
            self.reference = event["orbit_mm"]
            self.initial_transmission = event["transmission"]
            self.transmission_baseline = event.get("baseline", self.initial_transmission)
            self.initial_settings = dict(event["settings"])
            self.degradation_threshold = event.get("degradation_threshold",
                max(0., self.transmission_baseline - self.fields["degradation_percent"].value() / 100))
            self.update_parameters(event["settings"])
        elif kind == "baseline_updated":
            self.transmission_baseline = event["baseline"]
            self.degradation_threshold = event["threshold"]
        elif kind == "settings":
            self.initial_settings = dict(event["initial_settings"])
            self.update_parameters(event["settings"])
        elif kind == "transmission":
            self.points.append((elapsed, event["transmission"]))
            self.latest_transmission = event["transmission"]
            self.update_parameters(event["settings"])
        elif kind == "transmission_average":
            self.average_result = event
            if event["complete"]:
                self.averages.append((elapsed, event["average"]))
            self.set_status(f"RUNNING | Cycle {event.get('cycle', 0)} | Verifying transmission\n"
                            f"Operating-point measurements: {event['samples']} / {event['required']}")
        elif kind in ("orbit", "orbit_monitor") and self.reference is not None:
            drift = [1000 * (v - r) for v, r in zip(event["orbit_mm"], self.reference)]
            self.orbits.append((elapsed, drift))
        elif kind == "response":
            self.response_group.setTitle("Stored Response [mm/A] — refresh pending" if event.get("needs_refresh")
                                         else "Learned Response [mm/A]")
            self.response_table.clearContents()
            for row, values in enumerate(event["matrix_mm_per_A"] or []):
                for col, value in enumerate(values):
                    self.response_table.setItem(row, col, QTableWidgetItem(f"{value:.4g}"))
        if kind in ("transmission", "transmission_average", "reference", "baseline_updated"):
            initial = f"{self.initial_transmission:.6f}" if self.initial_transmission is not None else "—"
            current = f"{self.latest_transmission:.6f}" if self.latest_transmission is not None else "—"
            text = f"Transmission ({self.downstream.currentText()} / LN0): {current} | Initial: {initial}"
            if self.transmission_baseline is not None:
                text += (f"\nRecovery baseline: {self.transmission_baseline:.6f}"
                         f" | Threshold: {self.degradation_threshold:.6f}")
            if self.average_result:
                avg = self.average_result
                text += f"\nLatest operating-point mean ({avg['samples']}/{avg['required']}): {avg['average']:.6f}"
            self.values.setText(text)
        if kind not in ("transmission", "orbit", "orbit_monitor", "orbit_samples", "ict_samples", "command",
                        "command_result", "settings", "activity", "waiting"):
            self.log.appendPlainText(json.dumps(event, ensure_ascii=False))
        self.dirty = True

    def update_parameters(self, settings):
        for row, pv in enumerate(PVS):
            if pv not in settings or pv not in self.initial_settings:
                continue
            initial, current = self.initial_settings[pv], settings[pv]
            for col, text in enumerate((f"{initial:.3f}", f"{current:.3f}", f"{current - initial:+.3f}"), 1):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.parameter_table.setItem(row, col, item)

    def draw_plot(self):
        if not self.dirty:
            return
        self.dirty = False
        upper, lower = self.axes
        upper.clear()
        lower.clear()
        if self.points:
            x, y = zip(*self.points)
            upper.plot(x, y, ".-", markersize=3, label="Measured transmission (includes probes)")
        if self.initial_transmission is not None:
            upper.axhline(self.initial_transmission, color="gray", linestyle="--", label="Initial")
        if self.transmission_baseline is not None:
            upper.axhline(self.transmission_baseline, color="#7c3aed", linestyle="-.", label="Recovery baseline")
        if self.degradation_threshold is not None:
            upper.axhline(self.degradation_threshold, color="#d97706", linestyle=":", label="Recovery threshold")
        if self.averages:
            x, y = zip(*self.averages)
            upper.plot(x, y, "o-", color="#15803d", label="Operating-point mean", markersize=4)
        if self.orbits:
            for i, label in enumerate(("ML8L x", "ML10L x", "ML8L y", "ML10L y")):
                lower.plot([p[0] for p in self.orbits], [p[1][i] for p in self.orbits], label=label)
        threshold = self.fields["orbit_threshold_um"].value()
        for value in (-threshold, threshold):
            lower.axhline(value, color="gray", linestyle="--")
        upper.set_ylabel("Transmission")
        lower.set_ylabel("Orbit change [µm]")
        lower.set_xlabel("Elapsed [s]")
        for axis in self.axes:
            axis.grid(alpha=.25)
            if axis.lines and axis.get_legend_handles_labels()[0]:
                axis.legend(loc="best", fontsize=8)
        self.canvas.draw_idle()
