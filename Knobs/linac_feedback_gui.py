"""Active Feedback panel and cancellable worker for LinacOpt."""
from collections import deque
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import threading
import time
import uuid

from PyQt6.QtCore import QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
                            QGroupBox, QDoubleSpinBox, QComboBox, QLabel,
                            QPushButton, QPlainTextEdit, QMessageBox, QScrollArea,
                            QSplitter, QTableWidget, QTableWidgetItem, QHeaderView)
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

from Knobs.linac_feedback import FeedbackConfig, FeedbackController, CURRENT_PVS, PHASE_PV, TIMING_PV
from Knobs.linac_feedback_io import FeedbackMachine


class FeedbackWorker(QThread):
    event_signal = pyqtSignal(dict)

    def __init__(self, config, save_dir, machine_factory=FeedbackMachine):
        super().__init__()
        self.config, self.machine_factory = config, machine_factory
        self.run_dir = Path(save_dir) / (datetime.now().strftime("%Y%m%d_%H%M%S") +
                                       "_ActiveFeedback_" + uuid.uuid4().hex[:8])
        self.cancelled = threading.Event()
        self.paused = threading.Event()

    def stop(self):
        self.cancelled.set()
        self.paused.clear()

    def checkpoint(self):
        if self.paused.is_set() and not self.cancelled.is_set():
            self.emit_event(dict(kind="state", state="Paused (accepted settings retained)"))
            while self.paused.is_set() and not self.cancelled.wait(.05):
                pass
            if not self.cancelled.is_set():
                self.emit_event(dict(kind="state", state="Resuming"))
        if self.cancelled.is_set():
            raise InterruptedError("Feedback stopped")

    def emit_event(self, event):
        event = dict(event, timestamp=datetime.now().isoformat(timespec="milliseconds"), elapsed=time.monotonic() - self.started)
        # Flush commands and observations as they occur, not only on clean exit.
        if self.journal is not None:
            self.journal.write(json.dumps(event, allow_nan=False) + "\n")
            self.journal.flush()
        self.event_signal.emit(event)

    def run(self):
        self.started, self.journal = time.monotonic(), None
        machine = None
        try:
            self.run_dir.mkdir(parents=True, exist_ok=False)
            (self.run_dir / "config.json").write_text(json.dumps(asdict(self.config), indent=2))
            self.journal = (self.run_dir / "events.jsonl").open("w")
            self.emit_event(dict(kind="log", message=f"Run data: {self.run_dir}"))
            machine = self.machine_factory(self.config, self.cancelled.is_set, self.checkpoint, self.emit_event)
            controller = FeedbackController(machine, self.config, machine.limits(), self.emit_event)
            controller.run()
        except InterruptedError:
            self.emit_event(dict(kind="state", state="Stopped; accepted settings retained"))
        except Exception as exc:
            # Even a journal/storage failure must reach the UI.
            try:
                self.emit_event(dict(kind="error", message=str(exc)))
            except Exception:
                self.event_signal.emit(dict(kind="error", message=str(exc)))
        finally:
            try:
                if machine is not None:
                    machine.close()
            finally:
                if self.journal is not None:
                    self.journal.close()


class FeedbackPanel(QWidget):
    busy_changed = pyqtSignal(bool)

    def __init__(self, save_dir, main_busy, parent=None):
        super().__init__(parent)
        self.save_dir, self.main_busy = save_dir, main_busy
        self.worker = None
        self.points, self.orbits = deque(maxlen=2000), deque(maxlen=2000)
        self.reference = None
        self.initial_transmission = None
        self.dirty = False
        root = QVBoxLayout(self)
        title = QLabel("Timing 2.8 ns → orbit recovery 0.01 A → timing → CM8L 0.5° → repeat")
        title.setWordWrap(True)
        root.addWidget(title)
        split = QSplitter()
        root.addWidget(split, 1)
        controls = QWidget()
        left = QVBoxLayout(controls)
        self.settings = QGroupBox("Active Feedback settings")
        form = QFormLayout(self.settings)
        self.fields = {}
        defaults = FeedbackConfig()
        for name, label, lower, upper, decimals, step in (
                ("orbit_threshold_um", "Orbit threshold [µm]", 1., 10000., 1, 10.),
                ("settle_sec", "Settle after write [s]", 0., 120., 2, .5),
                ("interval_sec", "Between cycles [s]", 0., 600., 1, 1.),
                ("min_gain", "Transmission comparison deadband", 0., .1, 5, .001),
                ("timing_range_ns", "Timing range from start [±ns]", 2.8, 1120., 1, 2.8),
                ("phase_range_deg", "CM8L range from start [±°]", .5, 90., 1, .5),
                ("current_range_A", "Corrector range from start [±A]", .01, 2., 2, .01)):
            box = QDoubleSpinBox()
            box.setRange(lower, upper)
            box.setDecimals(decimals)
            box.setSingleStep(step)
            box.setValue(getattr(defaults, name))
            form.addRow(label, box)
            self.fields[name] = box
        self.downstream = QComboBox()
        self.downstream.addItems(["DR", "LNE", "BTM", "BTE"])
        form.addRow("Transmission / LN0", self.downstream)
        self.fields["min_gain"].setToolTip("Absolute transmission difference used for neighbour acceptance, peak decline and initial-baseline comparison.")
        left.addWidget(self.settings)
        row = QHBoxLayout()
        self.start_button, self.pause_button, self.stop_button = (QPushButton(s) for s in ("Start", "Pause", "Stop"))
        for button in (self.start_button, self.pause_button, self.stop_button):
            row.addWidget(button)
        left.addLayout(row)
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.start_button.clicked.connect(self.start)
        self.pause_button.clicked.connect(self.pause)
        self.stop_button.clicked.connect(self.stop)
        self.status = QLabel("Idle")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-size: 17px; font-weight: bold")
        left.addWidget(self.status)
        self.values = QLabel("Initial reference is captured when Start is pressed.")
        self.values.setWordWrap(True)
        left.addWidget(self.values)
        self.current_values = QLabel("Timing / CM8L / correctors: —")
        self.current_values.setWordWrap(True)
        left.addWidget(self.current_values)
        self.response_table = QTableWidget(4, 4)
        self.response_table.setHorizontalHeaderLabels([pv.split(":")[0] for pv in CURRENT_PVS])
        self.response_table.setVerticalHeaderLabels(["ML8L x", "ML10L x", "ML8L y", "ML10L y"])
        self.response_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.response_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        left.addWidget(QLabel("Learned response [mm/A]"))
        left.addWidget(self.response_table)
        left.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(controls)
        split.addWidget(scroll)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.figure = Figure(figsize=(8, 6), layout="constrained")
        self.axes = self.figure.subplots(2, 1)
        self.canvas = FigureCanvasQTAgg(self.figure)
        right_layout.addWidget(self.canvas, 1)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setMaximumHeight(180)
        right_layout.addWidget(self.log)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([430, 900])
        self.plot_timer = QTimer(self)
        self.plot_timer.setInterval(500)
        self.plot_timer.timeout.connect(self.draw_plot)

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
        self.reference = self.initial_transmission = None
        self.response_table.clearContents()
        self.log.clear()
        self.failed = False
        self.settings.setEnabled(False)
        self.start_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.stop_button.setEnabled(True)
        self.pause_button.setText("Pause")
        self.status.setText("Starting; capturing new reference")
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
            self.pause_button.setText("Pause")
        else:
            self.worker.paused.set()
            self.pause_button.setText("Resume")
            self.status.setText("Pause requested; completing current probe")

    def stop(self):
        if self.worker is not None:
            self.worker.stop()
            self.pause_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.status.setText("Stopping; restoring temporary probes")

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
            self.status.setText(event["state"])
        elif kind == "error":
            self.failed = True
            self.status.setText("Failed: " + event["message"])
        elif kind == "reference":
            self.reference = event["orbit_mm"]
            self.initial_transmission = event["transmission"]
        elif kind == "transmission":
            self.points.append((elapsed, event["transmission"]))
            baseline = f"{self.initial_transmission:.6f}" if self.initial_transmission is not None else "—"
            self.values.setText(f"Transmission: {event['transmission']:.6f} | initial: {baseline}")
            settings = event["settings"]
            self.current_values.setText(f"Timing: {settings[TIMING_PV]:.3f} ns | CM8L: {settings[PHASE_PV]:.2f}°\n" +
                " | ".join(f"{pv.split(':')[0]}: {settings[pv]:.3f} A" for pv in CURRENT_PVS))
        elif kind == "orbit" and self.reference is not None:
            drift = [1000 * (v - r) for v, r in zip(event["orbit_mm"], self.reference)]
            self.orbits.append((elapsed, drift))
        elif kind == "response":
            self.response_table.clearContents()
            for row, values in enumerate(event["matrix_mm_per_A"] or []):
                for col, value in enumerate(values):
                    self.response_table.setItem(row, col, QTableWidgetItem(f"{value:.4g}"))
        if kind not in ("transmission", "orbit", "orbit_samples", "ict_samples", "command"):
            self.log.appendPlainText(json.dumps(event, ensure_ascii=False))
        self.dirty = True

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
