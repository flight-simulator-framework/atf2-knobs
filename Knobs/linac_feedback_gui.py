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
                            QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox)
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

from Knobs.linac_feedback import FeedbackConfig, FeedbackController, RecentResponse, CURRENT_PVS, PVS, PHASE_PV, TIMING_PV
from Knobs.linac_feedback_io import FeedbackMachine, ICT_SHOTS
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
                             message="Temporary settings restored. Resolve the ICT beam loss, then press Resume."))
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
        if event["kind"] == "response" and event.get("model", {}).get("samples"):
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
                    self.emit_event(dict(kind="log", message="Stored response observations loaded; learning continues from correction results only."))
                    controller.report_response()
                except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                    self.emit_event(dict(kind="log", message=f"Stored response unavailable; learn from 0.005 A correction attempts: {exc}"))
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
        self.plot_waiting = False
        self.reference = None
        self.orbit_reference = None
        self.initial_transmission = None
        self.transmission_baseline = None
        self.initial_settings = {}
        self.latest_settings = {}
        self.parameter_points = deque(maxlen=2000)
        self.latest_elapsed = 0.
        self.degradation_threshold = None
        self.threshold_history = deque(maxlen=2000)
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
        self.targets = QWidget()
        target_layout = QHBoxLayout(self.targets)
        target_layout.setContentsMargins(0, 0, 0, 0)
        self.target_checks = {}
        for name, label in (("adjust_timing", "Timing"), ("adjust_phase", "CM8L phase"),
                            ("adjust_correctors", "Correctors")):
            box = QCheckBox(label)
            box.setChecked(True)
            box.setToolTip("Enable automatic adjustment for this run. Select before START; all unchecked runs monitoring only.")
            self.target_checks[name] = box
            target_layout.addWidget(box)
        run_layout.addWidget(self.targets)
        row = QHBoxLayout()
        self.start_button, self.pause_button, self.stop_button = (QPushButton(s) for s in ("START", "PAUSE", "STOP"))
        for button, role in zip((self.start_button, self.pause_button, self.stop_button), ("start", "pause", "stop")):
            button.setStyleSheet(run_button_style(role) + "QPushButton { min-height: 44px; }")
            row.addWidget(button)
        run_layout.addLayout(row)
        left.addWidget(run_group)
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.start_button.clicked.connect(self.start)
        self.pause_button.clicked.connect(self.pause)
        self.stop_button.clicked.connect(self.stop)

        self.settings = QGroupBox("Feedback Settings", self)
        self.settings.hide()  # MainWindow installs it in Config / Active Feedback.
        form = QFormLayout(self.settings)
        form.setVerticalSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.fields = {}
        defaults = FeedbackConfig()
        descriptions = {
            "orbit_threshold_um": "Compare the latest 10 distinct BPM shots with the orbit reference, updated when the transmission baseline rises. Default: 150 µm. The plot remains relative to the initial orbit; dotted limits follow the reference.",
            "settle_sec": "Wait after each PV write before measuring. Default: 2 s.",
            "interval_sec": "Wait after a full feedback cycle before starting the next timing search. Default: 30 s. This is additional to the wait after each write.",
            "min_gain": "Minimum absolute transmission improvement to accept a local search step. Default: 0.0005 (0.05 percentage point). 0 disables the deadband; 0.001 means 0.1 percentage point. Phase recovery uses the separate drop threshold.",
            "degradation_percent": "Absolute drop in percentage points: threshold = recovery baseline − drop / 100. Default: 80% → 75%. The baseline rises after sustained improvement and never decreases.",
            "baseline_hold_sec": "Raise the recovery baseline after improvement lasts this long AND at least 3 complete operating-point means confirm it. Uses their lowest mean; probes are excluded. Orbit correction retains the candidate, elapsed time and confirmations. Pause or beam loss restarts confirmation.",
        }
        for name, label, lower, upper, decimals, step in (
                ("orbit_threshold_um", "Orbit threshold [µm]", 1., 10000., 1, 10.),
                ("settle_sec", "Settle after write [s]", 0., 120., 2, .5),
                ("interval_sec", "Wait between cycles [s]", 0., 600., 1, 1.),
                ("degradation_percent", "Transmission drop [pp]", 0., 99., 1, 1.),
                ("baseline_hold_sec", "Raise baseline after [s]", 0., 3600., 0, 30.),
                ("min_gain", "Search deadband [ratio]", 0., .1, 5, .0001),
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
        average_count.setToolTip(f"Degradation decision: default 1 measurement = {ICT_SHOTS} fresh ICT shots at the timing optimum. Higher counts average multiple measurements. Temporary probe measurements are excluded.")
        self.fields["degradation_samples"] = average_count
        form.addRow("Transmission average [measurements]", average_count)
        self.downstream = QComboBox()
        self.downstream.addItems(["DR", "LNE", "BTM", "BTE"])
        form.addRow("Transmission / LN0", self.downstream)
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
        current_group.setToolTip("Changes from the initial setpoints, including temporary probes and restorations. Timing uses the left axis; CM8L phase uses the right axis.")
        self.parameter_figure = Figure(figsize=(8, 3), layout="constrained")
        self.parameter_axes = self.parameter_figure.subplots(1, 2, sharex=True)
        self.phase_axis = self.parameter_axes[0].twinx()
        self.parameter_canvas = FigureCanvasQTAgg(self.parameter_figure)
        self.parameter_canvas.setMinimumHeight(200)
        self.parameter_canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        current_layout.addWidget(self.parameter_canvas, 1)
        self.draw_parameters(redraw=False)
        response_group = QGroupBox("Learned Response [mm/A]")
        response_group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.response_group = response_group
        response_group.setToolTip("Learned only from actual 0.005 A correction attempts and stored across runs. Improving settings are kept; unsuccessful attempts are restored. No separate response measurement.")
        response_layout = QVBoxLayout(response_group)
        response_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
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
        left.addStretch(1)
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
        monitor.addWidget(current_group, stretch=1)
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
                              **{key: box.isChecked() for key, box in self.target_checks.items()},
                              downstream=self.downstream.currentText())

    def start(self):
        if self.active or self.main_busy():
            QMessageBox.warning(self, "Busy", "Stop the current optimizer/feedback run first.")
            return
        self.worker = FeedbackWorker(self.config(), self.save_dir())
        self.points.clear()
        self.plot_waiting = False
        self.orbits.clear()
        self.averages.clear()
        self.threshold_history.clear()
        self.reference = self.initial_transmission = self.transmission_baseline = None
        self.orbit_reference = None
        self.initial_settings = {}
        self.latest_settings = {}
        self.parameter_points.clear()
        self.latest_elapsed = 0.
        self.degradation_threshold = self.latest_transmission = self.average_result = None
        self.dirty = True
        self.draw_plot()
        self.response_table.clearContents()
        self.log.clear()
        self.failed = False
        self.settings.setEnabled(False)
        self.targets.setEnabled(False)
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
        self.targets.setEnabled(True)
        self.start_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.busy_changed.emit(False)

    def on_event(self, event):
        kind = event["kind"]
        elapsed = event.get("elapsed", 0.)
        self.latest_elapsed = max(self.latest_elapsed, elapsed)
        if kind == "state":
            self.plot_waiting = event["state"] in ("Waiting for next timing scan", "Waiting for next feedback cycle")
            paused = event["state"].lower().startswith(("paused", "auto-paused", "stopped"))
            labels = {"Checking orbit": "Timing scan" if self.target_checks["adjust_timing"].isChecked() else "Feedback cycle", "Timing tracking": "Timing scan",
                      "Timing after orbit correction": "Timing scan", "Timing after phase recovery": "Timing scan", "CM8L phase recovery": "Phase scan",
                      "Checking transmission after timing": "Evaluating transmission"}
            if event["state"] not in ("Monitoring", "Waiting for next timing scan", "Waiting for next feedback cycle"):
                prefix = "" if paused else "RUNNING | "
                self.set_status(prefix + labels.get(event["state"], event["state"]), "paused" if paused else "running")
        elif kind == "waiting":
            self.plot_waiting = True
            self.set_status(f"RUNNING | Next {event.get('next_action', 'Timing scan').lower()} in {math.ceil(event['remaining_sec'])} s")
        elif kind == "auto_pause":
            self.plot_waiting = False
            self.set_status(event["state"] + "\n" + event["message"], "paused")
            self.pause_button.setText("RESUME")
        elif kind == "error":
            self.plot_waiting = False
            self.failed = True
            self.set_status("Failed: " + event["message"], "error")
        elif kind == "reference":
            self.reference = event["orbit_mm"]
            self.orbit_reference = list(self.reference)
            self.initial_transmission = event["transmission"]
            self.transmission_baseline = event.get("baseline", self.initial_transmission)
            self.initial_settings = dict(event["settings"])
            self.parameter_points.clear()
            self.latest_settings = {}
            self.degradation_threshold = event.get("degradation_threshold",
                max(0., self.transmission_baseline - self.fields["degradation_percent"].value() / 100))
            self.threshold_history.clear()
            self.threshold_history.append((elapsed, self.degradation_threshold))
            self.update_parameters(event["settings"], elapsed)
        elif kind == "baseline_updated":
            self.transmission_baseline = event["baseline"]
            self.degradation_threshold = event["threshold"]
            self.threshold_history.append((elapsed, self.degradation_threshold))
            if "orbit_reference_mm" in event:
                self.orbit_reference = list(event["orbit_reference_mm"])
        elif kind == "settings":
            self.initial_settings = dict(event["initial_settings"])
            self.update_parameters(event["settings"], elapsed)
        elif kind == "transmission":
            self.points.append((elapsed, event["transmission"]))
            self.latest_transmission = event["transmission"]
            self.update_parameters(event["settings"], elapsed)
        elif kind == "transmission_monitor" and self.plot_waiting:
            # During a scan, plot only the exact fresh mean used by control.
            # In the cycle wait, keep displaying the background monitor.
            self.points.append((elapsed, event["transmission"]))
            self.latest_transmission = event["transmission"]
        elif kind == "transmission_average":
            self.average_result = event
            if event["complete"]:
                self.averages.append((elapsed, event["average"]))
        elif kind in ("orbit", "orbit_monitor") and self.reference is not None:
            drift = [1000 * (v - r) for v, r in zip(event["orbit_mm"], self.reference)]
            self.orbits.append((elapsed, drift))
        elif kind == "response":
            self.response_group.setTitle("Learning from Corrections [mm/A]" if event["matrix_mm_per_A"] is None
                                         else "Learned Response [mm/A]")
            self.response_table.clearContents()
            for row, values in enumerate(event["matrix_mm_per_A"] or []):
                for col, value in enumerate(values):
                    self.response_table.setItem(row, col, QTableWidgetItem(f"{value:.4g}"))
        if kind in ("transmission", "transmission_monitor", "transmission_average", "reference", "baseline_updated"):
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
        if kind not in ("transmission", "transmission_monitor", "orbit", "orbit_monitor", "orbit_samples", "ict_samples", "command",
                        "command_result", "settings", "activity", "waiting"):
            self.log.appendPlainText(json.dumps(event, ensure_ascii=False))
        self.dirty = True

    def update_parameters(self, settings, elapsed):
        self.latest_settings.update(settings)
        if all(pv in self.latest_settings and pv in self.initial_settings for pv in PVS):
            offsets = tuple(self.latest_settings[pv] - self.initial_settings[pv] for pv in PVS)
            if not self.parameter_points or offsets != self.parameter_points[-1][1]:
                self.parameter_points.append((elapsed, offsets))

    def draw_parameters(self, redraw=True):
        timing, currents = self.parameter_axes
        for axis in (timing, currents, self.phase_axis):
            axis.clear()
            axis.tick_params(labelsize=8)
        if self.parameter_points:
            points = list(self.parameter_points)
            if self.latest_elapsed > points[-1][0]:
                points.append((self.latest_elapsed, points[-1][1]))
            x = [point[0] for point in points]
            traces = [dict(axis=timing, index=0, color="#2563eb", label="Timing"),
                      dict(axis=self.phase_axis, index=1, color="#d97706", label="CM8L phase")]
            traces += [dict(axis=currents, index=i + 2, color=color, label=pv.split(":")[0])
                       for i, (pv, color) in enumerate(zip(CURRENT_PVS, ("#15803d", "#7c3aed", "#dc2626", "#0891b2")))]
            for trace in traces:
                trace["axis"].step(x, [point[1][trace["index"]] for point in points], where="post",
                                   color=trace["color"], label=trace["label"], marker=".", markersize=2)
            lines = timing.lines + self.phase_axis.lines
            timing.legend(lines, [line.get_label() for line in lines], fontsize=8, loc="best")
            currents.legend(fontsize=8, ncol=2, loc="best")
        timing.set_title("Timing / CM8L phase", fontsize=10)
        currents.set_title("Corrector currents", fontsize=10)
        timing.set_ylabel("Δ Timing [ns]", color="#2563eb", fontsize=9)
        self.phase_axis.set_ylabel("Δ Phase [°]", color="#d97706", fontsize=9)
        self.phase_axis.yaxis.set_label_position("right")
        self.phase_axis.yaxis.tick_right()
        self.phase_axis.tick_params(axis="y", colors="#d97706")
        timing.tick_params(axis="y", colors="#2563eb")
        currents.set_ylabel("Δ Current [A]", fontsize=9)
        for axis in (timing, currents):
            axis.set_xlabel("Elapsed [s]", fontsize=9)
            axis.grid(alpha=.25)
        if redraw:
            self.parameter_canvas.draw_idle()

    def draw_plot(self):
        if not self.dirty:
            return
        self.dirty = False
        upper, lower = self.axes
        upper.clear()
        lower.clear()
        if self.points:
            x, y = zip(*self.points)
            upper.plot(x, y, ".-", markersize=3, color="#2563eb",
                       label="Measured transmission")
        if self.initial_transmission is not None:
            upper.axhline(self.initial_transmission, color="gray", linestyle="--", label="Initial")
        if self.degradation_threshold is not None:
            if self.threshold_history and self.latest_elapsed > self.threshold_history[0][0]:
                history = list(self.threshold_history)
                # Keep threshold history aligned with the visible measurements,
                # rather than stretching the plot back to the start forever.
                start = max(history[0][0], self.points[0][0]) if self.points else history[0][0]
                prior = next(value for stamp, value in reversed(history) if stamp <= start)
                visible = [(start, prior)] + [(stamp, value) for stamp, value in history if stamp > start]
                if self.latest_elapsed > visible[-1][0]:
                    visible.append((self.latest_elapsed, visible[-1][1]))
                x, y = zip(*visible)
                upper.step(x, y, where="post", color="#d97706", linestyle=":", label="Threshold")
            else:
                upper.axhline(self.degradation_threshold, color="#d97706", linestyle=":", label="Threshold")
        threshold = self.fields["orbit_threshold_um"].value()
        colors = ("#1f77b4", "#ff7f0e", "#2ca02c", "#d62728")
        for i, label in enumerate(("ML8L x", "ML10L x", "ML8L y", "ML10L y")):
            if self.orbits:
                lower.plot([p[0] for p in self.orbits], [p[1][i] for p in self.orbits],
                           label=label, color=colors[i])
            offset = (1000 * (self.orbit_reference[i] - self.reference[i])
                      if self.reference is not None and self.orbit_reference is not None else 0.)
            for value in (offset - threshold, offset + threshold):
                lower.axhline(value, color=colors[i], linestyle=":", alpha=.6)
        upper.set_ylabel("Transmission")
        lower.set_ylabel("Orbit change from initial [µm]")
        lower.set_xlabel("Elapsed [s]")
        for axis in self.axes:
            axis.grid(alpha=.25)
            if axis.lines and axis.get_legend_handles_labels()[0]:
                axis.legend(loc="best", fontsize=8, ncol=2)
        self.canvas.draw_idle()
        self.draw_parameters()
