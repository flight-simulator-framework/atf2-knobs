from __future__ import annotations

import csv
import json
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import matplotlib
matplotlib.use("QtAgg")
import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from IPBSM_Opt import IPBSMInterface
from Interfaces.ATF2.InterfaceATF2_DR import InterfaceATF2_DR
from Interfaces.ATF2.InterfaceATF2_Ext import InterfaceATF2_Ext
from Interfaces.ATF2.InterfaceATF2_Linac import InterfaceATF2_Linac


def now_tag() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return float("nan")


def _flatten_bpms(prefix: str, data: Dict[str, Any]) -> Dict[str, float]:
    names = list(data.get("names", []) or [])
    x = np.asarray(data.get("x", []), dtype=float)
    y = np.asarray(data.get("y", []), dtype=float)
    flat: Dict[str, float] = {}
    if x.size == 0 or y.size == 0 or not names:
        return flat
    x_mean = np.nanmean(x, axis=0)
    y_mean = np.nanmean(y, axis=0)
    for idx, name in enumerate(names):
        flat[f"{prefix}_{name}_x_mm"] = _to_float(x_mean[idx]) if idx < len(x_mean) else float("nan")
        flat[f"{prefix}_{name}_y_mm"] = _to_float(y_mean[idx]) if idx < len(y_mean) else float("nan")
    return flat


def _flatten_icts(prefix: str, data: Dict[str, Any]) -> Dict[str, float]:
    names = list(data.get("names", []) or [])
    charge = np.asarray(data.get("charge", []), dtype=float)
    if charge.ndim > 1:
        charge = np.nanmean(charge, axis=0)
    flat: Dict[str, float] = {}
    for idx, name in enumerate(names):
        flat[f"{prefix}_{name}"] = _to_float(charge[idx]) if idx < len(charge) else float("nan")
    return flat


@dataclass
class StabilityRunConfig:
    max_measurements: int
    max_duration_s: float
    monitor_interval_s: float
    average_pause_ratio: float
    out_dir: str


class ModulationPlotWidget(FigureCanvas):
    def __init__(self, parent: Optional[QWidget] = None):
        self.fig = Figure(figsize=(6, 3), tight_layout=True)
        self.ax = self.fig.add_subplot(111)
        super().__init__(self.fig)
        self.setParent(parent)
        self.ax.set_title("IPBSM Modulation vs Time")
        self.ax.set_xlabel("Elapsed time [s]")
        self.ax.set_ylabel("Modulation")
        self._line, = self.ax.plot([], [], marker="o", color="tab:blue")
        self.ax.grid(True, alpha=0.3)

    def reset(self) -> None:
        self._line.set_data([], [])
        self.ax.relim()
        self.ax.autoscale_view()
        self.draw_idle()

    def update_points(self, elapsed_s: List[float], modulation: List[float]) -> None:
        self._line.set_data(elapsed_s, modulation)
        self.ax.relim()
        self.ax.autoscale_view()
        self.draw_idle()


class StabilityWorker(QThread):
    log = pyqtSignal(str)
    measurement_done = pyqtSignal(dict)
    pause_requested = pyqtSignal(dict)
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, config: StabilityRunConfig):
        super().__init__()
        self.config = config
        self._stop_requested = False
        self._pause_continue = True
        self._pause_event = threading.Event()
        self._state_lock = threading.Lock()
        self._monitor_lock = threading.Lock()
        self._state = {
            "measurement_index": 0,
            "measurement_in_progress": False,
            "last_modulation": float("nan"),
            "last_average": float("nan"),
        }
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()

    def request_stop(self) -> None:
        self._stop_requested = True
        self.log.emit("STOP requested. Current measurement will finish, then data will be saved and the run will end.")
        self._pause_continue = False
        self._pause_event.set()

    def resume_after_pause(self, should_continue: bool) -> None:
        self._pause_continue = bool(should_continue)
        self._pause_event.set()

    def _set_state(self, **kwargs: Any) -> None:
        with self._state_lock:
            self._state.update(kwargs)

    def _get_state(self) -> Dict[str, Any]:
        with self._state_lock:
            return dict(self._state)

    def _write_json(self, out_dir: Path) -> None:
        with open(out_dir / "config.json", "w", encoding="utf-8") as f:
            json.dump(asdict(self.config), f, ensure_ascii=False, indent=2)

    def _monitor_loop(
        self,
        out_dir: Path,
        run_start: float,
        ext_monitor: InterfaceATF2_Ext,
        linac: InterfaceATF2_Linac,
        dr: InterfaceATF2_DR,
    ) -> None:
        monitor_path = out_dir / "monitor_timeseries.csv"
        writer = None
        fp = None
        try:
            while not self._monitor_stop.is_set():
                state = self._get_state()
                try:
                    row = self._collect_monitor_row(run_start, state, ext_monitor, linac, dr)
                except Exception as exc:
                    self.log.emit(f"Monitor snapshot failed: {exc}")
                    if self._monitor_stop.wait(self.config.monitor_interval_s):
                        break
                    continue
                with self._monitor_lock:
                    if writer is None:
                        fieldnames = list(row.keys())
                        fp = open(monitor_path, "w", newline="", encoding="utf-8")
                        writer = csv.DictWriter(fp, fieldnames=fieldnames)
                        writer.writeheader()
                    writer.writerow(row)
                    fp.flush()
                if self._monitor_stop.wait(self.config.monitor_interval_s):
                    break
        finally:
            if fp is not None:
                fp.close()

    def _collect_monitor_row(
        self,
        run_start: float,
        state: Dict[str, Any],
        ext_monitor: InterfaceATF2_Ext,
        linac: InterfaceATF2_Linac,
        dr: InterfaceATF2_DR,
    ) -> Dict[str, Any]:
        ts = time.time()
        row: Dict[str, Any] = {
            "timestamp_iso": datetime.fromtimestamp(ts).isoformat(timespec="seconds"),
            "timestamp_epoch_s": ts,
            "elapsed_s": time.monotonic() - run_start,
            "measurement_index": state.get("measurement_index", 0),
            "measurement_in_progress": int(bool(state.get("measurement_in_progress", False))),
            "last_modulation": _to_float(state.get("last_modulation", float("nan"))),
            "last_average": _to_float(state.get("last_average", float("nan"))),
        }
        row.update(_flatten_bpms("linac_bpm", linac.get_bpms()))
        row.update(_flatten_bpms("dr_bpm", dr.get_bpms()))
        row.update(_flatten_bpms("atf2_bpm", ext_monitor.get_bpms()))
        row.update(_flatten_icts("linac_ict", linac.get_icts()))
        row.update(_flatten_icts("dr_ict", dr.get_icts()))
        row.update(_flatten_icts("atf2_ict", ext_monitor.get_icts()))
        xsr = dr.get_xsr()
        for key, value in xsr.items():
            row[f"xsr_{key}"] = _to_float(value)
        row["arc_dispersion"] = _to_float(dr.get_arc_dispersion())
        return row

    def _pause_for_average_drop(self, payload: Dict[str, Any]) -> bool:
        self._pause_continue = True
        self._pause_event.clear()
        self.pause_requested.emit(payload)
        self._pause_event.wait()
        return self._pause_continue

    def run(self) -> None:
        out_dir = ensure_dir(Path(self.config.out_dir))
        self._write_json(out_dir)
        run_start = time.monotonic()

        ipbsm = IPBSMInterface(nsamples=3)
        ext_monitor = InterfaceATF2_Ext(nsamples=1)
        linac = InterfaceATF2_Linac(nsamples=1)
        dr = InterfaceATF2_DR(nsamples=1)

        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            args=(out_dir, run_start, ext_monitor, linac, dr),
            daemon=True,
        )
        self._monitor_thread.start()

        baseline_average: Optional[float] = None
        measurement_count = 0
        measurement_path = out_dir / "ipbsm_measurements.csv"
        fieldnames = [
            "measurement_index",
            "start_iso",
            "end_iso",
            "elapsed_s",
            "modulation",
            "error",
            "beamsize",
            "ebeamsize",
            "average",
            "phase",
            "filename",
            "ict_average",
        ]

        try:
            with open(measurement_path, "w", newline="", encoding="utf-8") as fp:
                writer = csv.DictWriter(fp, fieldnames=fieldnames)
                writer.writeheader()

                while True:
                    elapsed_s = time.monotonic() - run_start
                    if self._stop_requested:
                        break
                    if self.config.max_measurements > 0 and measurement_count >= self.config.max_measurements:
                        self.log.emit("Reached the requested measurement count. Ending run.")
                        break
                    if self.config.max_duration_s > 0 and elapsed_s >= self.config.max_duration_s:
                        self.log.emit("Reached the requested duration. Ending run.")
                        break

                    measurement_count += 1
                    start_ts = time.time()
                    self._set_state(measurement_index=measurement_count, measurement_in_progress=True)
                    self.log.emit(f"Measurement {measurement_count} started.")

                    dat = ipbsm.get_ipbsm_full()

                    modulation = _to_float(dat.get("modulation"))
                    average = _to_float(dat.get("average"))
                    self._set_state(
                        measurement_in_progress=False,
                        last_modulation=modulation,
                        last_average=average,
                    )

                    if baseline_average is None and np.isfinite(average):
                        baseline_average = average

                    row = {
                        "measurement_index": measurement_count,
                        "start_iso": datetime.fromtimestamp(start_ts).isoformat(timespec="seconds"),
                        "end_iso": datetime.now().isoformat(timespec="seconds"),
                        "elapsed_s": time.monotonic() - run_start,
                        "modulation": modulation,
                        "error": _to_float(dat.get("error")),
                        "beamsize": _to_float(dat.get("beamsize")),
                        "ebeamsize": _to_float(dat.get("ebeamsize")),
                        "average": average,
                        "phase": _to_float(dat.get("phase")),
                        "filename": str(dat.get("filename", "")),
                        "ict_average": _to_float(dat.get("ict_average")),
                    }
                    writer.writerow(row)
                    fp.flush()

                    self.log.emit(
                        f"Measurement {measurement_count}: modulation={modulation:.6f}, average={average:.6f}"
                    )
                    self.measurement_done.emit(row)

                    if (
                        baseline_average is not None
                        and np.isfinite(average)
                        and average < self.config.average_pause_ratio * baseline_average
                    ):
                        threshold = self.config.average_pause_ratio * baseline_average
                        should_continue = self._pause_for_average_drop(
                            {
                                "reason": "average_below_threshold",
                                "measurement_index": measurement_count,
                                "average": average,
                                "baseline_average": baseline_average,
                                "threshold_average": threshold,
                                "modulation": modulation,
                            }
                        )
                        if not should_continue or self._stop_requested:
                            break
        except Exception as exc:
            self.failed.emit(str(exc) + "\n" + traceback.format_exc())
            return
        finally:
            self._monitor_stop.set()
            if self._monitor_thread is not None:
                self._monitor_thread.join(timeout=max(1.0, self.config.monitor_interval_s + 2.0))

        self.finished.emit(
            {
                "out_dir": str(out_dir),
                "measurement_count": measurement_count,
                "elapsed_s": time.monotonic() - run_start,
            }
        )


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("IPBSM Stability Monitor")
        self.worker: Optional[StabilityWorker] = None
        self._plot_elapsed_s: List[float] = []
        self._plot_modulation: List[float] = []
        self._build_ui()

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        controls = QGroupBox("Run Settings")
        controls_form = QFormLayout(controls)
        outer.addWidget(controls)

        self.max_measurements = QSpinBox()
        self.max_measurements.setRange(0, 100000)
        self.max_measurements.setValue(20)
        controls_form.addRow("Max measurements (0=disable)", self.max_measurements)

        self.max_duration_min = QDoubleSpinBox()
        self.max_duration_min.setRange(0.0, 24.0 * 60.0)
        self.max_duration_min.setDecimals(2)
        self.max_duration_min.setValue(0.0)
        controls_form.addRow("Max duration [min] (0=disable)", self.max_duration_min)

        self.monitor_interval_s = QDoubleSpinBox()
        self.monitor_interval_s.setRange(0.2, 60.0)
        self.monitor_interval_s.setDecimals(2)
        self.monitor_interval_s.setValue(1.0)
        controls_form.addRow("Monitor interval [s]", self.monitor_interval_s)

        self.average_pause_ratio = QDoubleSpinBox()
        self.average_pause_ratio.setRange(0.0, 1.0)
        self.average_pause_ratio.setDecimals(3)
        self.average_pause_ratio.setValue(0.70)
        controls_form.addRow("Average pause ratio", self.average_pause_ratio)

        out_row = QHBoxLayout()
        self.out_dir_edit = QLineEdit(str(Path.cwd() / "Analysis" / f"IPBSM_Stability_{now_tag()}"))
        self.browse_btn = QPushButton("Browse")
        out_row.addWidget(self.out_dir_edit, stretch=1)
        out_row.addWidget(self.browse_btn)
        out_widget = QWidget()
        out_widget.setLayout(out_row)
        controls_form.addRow("Output directory", out_widget)

        buttons = QHBoxLayout()
        outer.addLayout(buttons)
        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.status_lbl = QLabel("Status: idle")
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.stop_btn)
        buttons.addWidget(self.status_lbl, stretch=1)

        plot_group = QGroupBox("Realtime Modulation")
        plot_layout = QVBoxLayout(plot_group)
        self.plot_widget = ModulationPlotWidget()
        plot_layout.addWidget(self.plot_widget)
        outer.addWidget(plot_group, stretch=2)

        log_group = QGroupBox("Log")
        log_layout = QVBoxLayout(log_group)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        log_layout.addWidget(self.log_edit)
        outer.addWidget(log_group, stretch=2)

        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn.clicked.connect(self._on_stop)
        self.browse_btn.clicked.connect(self._on_browse)

    def _append_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_edit.appendPlainText(f"[{stamp}] {message}")

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.max_measurements.setEnabled(enabled)
        self.max_duration_min.setEnabled(enabled)
        self.monitor_interval_s.setEnabled(enabled)
        self.average_pause_ratio.setEnabled(enabled)
        self.out_dir_edit.setEnabled(enabled)
        self.browse_btn.setEnabled(enabled)
        self.start_btn.setEnabled(enabled)
        self.stop_btn.setEnabled(not enabled)

    def _on_browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Select output directory", self.out_dir_edit.text())
        if selected:
            self.out_dir_edit.setText(selected)

    def _build_config(self) -> StabilityRunConfig:
        return StabilityRunConfig(
            max_measurements=int(self.max_measurements.value()),
            max_duration_s=float(self.max_duration_min.value()) * 60.0,
            monitor_interval_s=float(self.monitor_interval_s.value()),
            average_pause_ratio=float(self.average_pause_ratio.value()),
            out_dir=self.out_dir_edit.text().strip(),
        )

    def _on_start(self) -> None:
        cfg = self._build_config()
        if cfg.max_measurements <= 0 and cfg.max_duration_s <= 0.0:
            QMessageBox.warning(self, "Missing stop condition", "Set either max measurements or max duration.")
            return
        if not cfg.out_dir:
            QMessageBox.warning(self, "Missing output directory", "Choose an output directory.")
            return

        ensure_dir(Path(cfg.out_dir))
        self.plot_widget.reset()
        self._plot_elapsed_s = []
        self._plot_modulation = []
        self.log_edit.clear()
        self._append_log(
            f"Run started. max_measurements={cfg.max_measurements}, max_duration_s={cfg.max_duration_s:.1f}, "
            f"monitor_interval_s={cfg.monitor_interval_s:.2f}, average_pause_ratio={cfg.average_pause_ratio:.3f}"
        )

        self.worker = StabilityWorker(cfg)
        self.worker.log.connect(self._append_log)
        self.worker.measurement_done.connect(self._on_measurement_done)
        self.worker.pause_requested.connect(self._on_pause_requested)
        self.worker.finished.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

        self._set_controls_enabled(False)
        self.status_lbl.setText("Status: running")

    def _on_stop(self) -> None:
        if self.worker is None:
            return
        self.worker.request_stop()
        self.status_lbl.setText("Status: stopping after current measurement")

    def _on_measurement_done(self, row: Dict[str, Any]) -> None:
        elapsed_s = _to_float(row.get("elapsed_s"))
        modulation = _to_float(row.get("modulation"))
        if np.isfinite(elapsed_s) and np.isfinite(modulation):
            self._plot_elapsed_s.append(elapsed_s)
            self._plot_modulation.append(modulation)
            self.plot_widget.update_points(self._plot_elapsed_s, self._plot_modulation)
        idx = row.get("measurement_index", "?")
        avg = _to_float(row.get("average"))
        self.status_lbl.setText(f"Status: measurement {idx} finished")
        self._append_log(f"Measurement {idx} saved. modulation={modulation:.6f}, average={avg:.6f}")

    def _on_pause_requested(self, info: Dict[str, Any]) -> None:
        idx = info.get("measurement_index", "?")
        avg = _to_float(info.get("average"))
        baseline = _to_float(info.get("baseline_average"))
        threshold = _to_float(info.get("threshold_average"))
        modulation = _to_float(info.get("modulation"))

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Average Warning")
        box.setText(
            f"Measurement {idx} finished.\n"
            f"average={avg:.6f}\n"
            f"baseline={baseline:.6f}\n"
            f"threshold={threshold:.6f}\n"
            f"modulation={modulation:.6f}"
        )
        resume_btn = box.addButton("Resume", QMessageBox.ButtonRole.AcceptRole)
        stop_btn = box.addButton("Save and End", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(resume_btn)
        self.status_lbl.setText("Status: paused by average warning")
        box.exec()

        if self.worker is None:
            return
        if box.clickedButton() is stop_btn:
            self._append_log("Average warning dialog: Save and End selected.")
            self.status_lbl.setText("Status: stopping after pause")
            self.worker.request_stop()
            self.worker.resume_after_pause(False)
        else:
            self._append_log("Average warning dialog: Resume selected.")
            self.status_lbl.setText("Status: resumed")
            self.worker.resume_after_pause(True)

    def _on_finished(self, info: Dict[str, Any]) -> None:
        self._set_controls_enabled(True)
        self.status_lbl.setText("Status: finished")
        self.worker = None
        out_dir = info.get("out_dir", "")
        self._append_log(
            f"Run finished. measurements={info.get('measurement_count', 0)}, elapsed_s={_to_float(info.get('elapsed_s')):.1f}, out_dir={out_dir}"
        )
        self.out_dir_edit.setText(str(Path.cwd() / "Analysis" / f"IPBSM_Stability_{now_tag()}"))

    def _on_failed(self, message: str) -> None:
        self._set_controls_enabled(True)
        self.status_lbl.setText("Status: failed")
        self.worker = None
        self._append_log("Run failed.")
        QMessageBox.critical(self, "IPBSM Stability Monitor", message)


if __name__ == "__main__":
    app = QApplication([])
    win = MainWindow()
    win.resize(1100, 800)
    win.show()
    app.exec()
