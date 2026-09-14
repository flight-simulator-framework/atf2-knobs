import sys
import time
import math
import csv
import traceback
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
import matplotlib

matplotlib.use("QtAgg")

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPushButton, QComboBox, QSpinBox, QDoubleSpinBox, QGroupBox,
    QTableWidget, QTableWidgetItem, QMessageBox, QPlainTextEdit
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from Interfaces.ATF2.InterfaceATF2_DR import InterfaceATF2_DR


SCAN_WIDTH_A = 3.0
SCAN_HALF_WIDTH_A = 1.5
SKEW_ABS_LIMIT_A = 9.8
MAGNET_DROP_NEAR_ZERO_A = 0.2
MAGNET_DROP_TARGET_MIN_A = 0.5


def kernel_rbf_ard(Xa, Xb, length_scales, sigma_f):
    Xa = np.asarray(Xa, dtype=float)
    Xb = np.asarray(Xb, dtype=float)
    ls = np.maximum(np.asarray(length_scales, dtype=float), 1e-12)
    diff = (Xa[:, None, :] - Xb[None, :, :]) / ls[None, None, :]
    d2 = np.sum(diff * diff, axis=2)
    return (float(sigma_f) ** 2) * np.exp(-0.5 * d2)


def gp_posterior_nd(X_train, y_train, X_test, length_scales, sigma_f, sigma_n):
    X_train = np.asarray(X_train, dtype=float)
    y_train = np.asarray(y_train, dtype=float).reshape(-1)
    X_test = np.asarray(X_test, dtype=float)
    if X_train.size == 0:
        return np.zeros(X_test.shape[0], dtype=float), np.ones(X_test.shape[0], dtype=float)

    K = kernel_rbf_ard(X_train, X_train, length_scales, sigma_f) + (float(sigma_n) ** 2) * np.eye(X_train.shape[0])
    Ks = kernel_rbf_ard(X_train, X_test, length_scales, sigma_f)
    Kss = kernel_rbf_ard(X_test, X_test, length_scales, sigma_f)
    try:
        L = np.linalg.cholesky(K + 1e-12 * np.eye(K.shape[0]))
        alpha = np.linalg.solve(L.T, np.linalg.solve(L, y_train))
        mu = Ks.T @ alpha
        v = np.linalg.solve(L, Ks)
        cov = Kss - v.T @ v
    except np.linalg.LinAlgError:
        Kinv = np.linalg.pinv(K)
        mu = Ks.T @ (Kinv @ y_train)
        cov = Kss - Ks.T @ (Kinv @ Ks)
    var = np.clip(np.diag(cov), 1e-12, None)
    return mu.reshape(-1), np.sqrt(var).reshape(-1)


def expected_improvement(mu, std, y_best, xi=0.0):
    std = np.maximum(np.asarray(std, dtype=float), 1e-12)
    z = (np.asarray(mu, dtype=float) - float(y_best) - float(xi)) / std
    pdf = (1.0 / np.sqrt(2.0 * np.pi)) * np.exp(-0.5 * z * z)
    cdf = 0.5 * (1.0 + np.vectorize(math.erf)(z / np.sqrt(2.0)))
    ei = (mu - y_best - xi) * cdf + std * pdf
    ei[std <= 1e-12] = 0.0
    return ei


class SkewBOWorker(QThread):
    progress = pyqtSignal(dict)
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)
    pause_requested = pyqtSignal(dict)

    def __init__(self, interface, config):
        super().__init__()
        self.interface = interface
        self.config = dict(config)
        self._stop = threading.Event()
        self._pause_event = threading.Event()
        self._pause_action = "resume"

    def stop(self):
        self._stop.set()

    def _stopped(self):
        return self._stop.is_set()

    def resume_from_pause(self, action="resume"):
        self._pause_action = str(action)
        self._pause_event.set()

    def _scan_candidates(self, current, step):
        step = max(float(step), 1e-9)
        center = float(current)
        lo = center - SCAN_HALF_WIDTH_A
        hi = center + SCAN_HALF_WIDTH_A
        if lo < -SKEW_ABS_LIMIT_A:
            lo = -SKEW_ABS_LIMIT_A
            hi = lo + SCAN_WIDTH_A
        if hi > SKEW_ABS_LIMIT_A:
            hi = SKEW_ABS_LIMIT_A
            lo = hi - SCAN_WIDTH_A
        lo = max(lo, -SKEW_ABS_LIMIT_A)
        hi = min(hi, SKEW_ABS_LIMIT_A)
        vals = np.arange(lo, hi + 0.5 * step, step, dtype=float)
        vals = np.clip(np.round(vals / step) * step, -SKEW_ABS_LIMIT_A, SKEW_ABS_LIMIT_A)
        vals = np.unique(np.round(vals, 10))
        if vals.size == 0:
            vals = np.array([float(np.clip(center, -SKEW_ABS_LIMIT_A, SKEW_ABS_LIMIT_A))], dtype=float)
        return vals

    def _make_csv_path(self):
        out_dir = Path.cwd() / "Analysis"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return out_dir / f"DR_SkewBO_{stamp}.csv"

    def run(self):
        try:
            series = str(self.config["series"]).strip()
            step = float(self.config["step"])
            verify_timeout_sec = float(self.config.get("verify_timeout_sec", 5.0))
            xsr_wait_sec = float(self.config.get("xsr_wait_sec", 7.0))
            min_init = int(self.config["min_init"])
            max_evals = int(self.config["max_evals"])
            cand_pool = int(self.config["cand_pool"])
            csv_path = self._make_csv_path()
            selection = [dict(item) for item in self.config.get("selection", [])]
            if len(selection) == 0:
                raise RuntimeError("No valid skew current readback was found.")

            names = [item["name"] for item in selection]
            cur = np.array([item["current"] for item in selection], dtype=float)
            lo = np.array([item["lo"] for item in selection], dtype=float)
            hi = np.array([item["hi"] for item in selection], dtype=float)
            steps = np.full(len(selection), step, dtype=float)
            length_scales = np.maximum((hi - lo) / 3.0, steps)
            sigma_f = 1.0
            sigma_n = 1e-3
            xi = 0.01

            X, Y = [], []
            with csv_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp",
                    "eval_count",
                    "note",
                    "series",
                    "selected_names",
                    "setpoints",
                    "readback",
                    "verify_mode",
                    "verify_elapsed_sec",
                    "xsr_v_sigma",
                    "xsr_v_mean",
                    "xsr_h_sigma",
                    "xsr_h_mean",
                    "best_xsr_v_sigma",
                ])

                def quantize_vec(x):
                    q = np.round((np.asarray(x, dtype=float) - lo) / steps) * steps + lo
                    return np.clip(q, lo, hi)

                def key(v):
                    return tuple(np.round((np.asarray(v, dtype=float) - lo) / steps).astype(int).tolist())

                def emit_progress(note, xq, v_sigma, best_v_sigma, xsr, verify_info):
                    payload = {
                        "note": note,
                        "setpoints": np.asarray(xq, dtype=float).tolist(),
                        "names": names,
                        "v_sigma": float(v_sigma),
                        "best_v_sigma": float(best_v_sigma),
                        "v_mean": float(xsr.get("proj_v_mean_ave", np.nan)),
                        "eval_count": len(Y),
                        "verify_ok": bool(verify_info["updated"]),
                        "verify_elapsed_sec": float(verify_info["elapsed_sec"]),
                        "verify_mode": str(verify_info["mode"]),
                        "readback": np.asarray(verify_info["readback"], dtype=float).tolist(),
                    }
                    self.progress.emit(payload)

                def request_pause_for_drop(target, readback):
                    self.interface.set_skew_currents({name: float(val) for name, val in zip(names, target)})
                    self._pause_action = "resume"
                    self._pause_event.clear()
                    self.pause_requested.emit({
                        "reason": "magnet_drop",
                        "message": "A skew current dropped near 0 A after setting. Re-applied the target PVs.",
                        "target": np.asarray(target, dtype=float).tolist(),
                        "readback": np.asarray(readback, dtype=float).tolist(),
                        "names": list(names),
                    })
                    self._pause_event.wait()
                    if self._pause_action != "resume":
                        self.stop()
                        return False
                    return True

                def wait_for_set_reflection(target):
                    target = np.asarray(target, dtype=float)
                    t0 = time.time()
                    tol = max(step * 0.5, 1e-3)
                    last_readback = np.full_like(target, np.nan, dtype=float)
                    while (time.time() - t0) < verify_timeout_sec:
                        if self._stopped():
                            break
                        last_readback = np.array([self.interface.get_skew_current(name) for name in names], dtype=float)
                        drop_mask = (
                            np.isfinite(last_readback)
                            & (np.abs(last_readback) <= MAGNET_DROP_NEAR_ZERO_A)
                            & (np.abs(target) >= MAGNET_DROP_TARGET_MIN_A)
                        )
                        if np.any(drop_mask):
                            if not request_pause_for_drop(target, last_readback):
                                return {
                                    "updated": False,
                                    "elapsed_sec": time.time() - t0,
                                    "mode": "stopped_after_drop",
                                    "readback": last_readback,
                                }
                            t0 = time.time()
                            continue
                        if np.all(np.isfinite(last_readback)) and np.all(np.abs(last_readback - target) <= tol):
                            return {
                                "updated": True,
                                "elapsed_sec": time.time() - t0,
                                "mode": "readback_updated",
                                "readback": last_readback,
                            }
                        time.sleep(0.1)
                    return {
                        "updated": False,
                        "elapsed_sec": min(time.time() - t0, verify_timeout_sec),
                        "mode": "timeout_assumed_set",
                        "readback": last_readback,
                    }

                def wait_with_stop(duration_sec):
                    t0 = time.time()
                    while (time.time() - t0) < duration_sec:
                        if self._stopped():
                            break
                        time.sleep(0.05)

                def apply_and_measure(x, note):
                    xq = quantize_vec(x)
                    self.interface.set_skew_currents({name: float(val) for name, val in zip(names, xq)})
                    verify_info = wait_for_set_reflection(xq)
                    wait_with_stop(xsr_wait_sec)
                    xsr = self.interface.get_xsr()
                    v_sigma = float(xsr.get("proj_v_sigma_ave", np.nan))
                    if not np.isfinite(v_sigma):
                        v_sigma = 1e9
                    X.append(xq.copy())
                    Y.append(v_sigma)
                    best_now = float(np.min(Y))
                    writer.writerow([
                        datetime.now().isoformat(timespec="seconds"),
                        len(Y),
                        note,
                        series,
                        ";".join(names),
                        ";".join(f"{float(v):.6f}" for v in xq),
                        ";".join(f"{float(v):.6f}" for v in verify_info["readback"]),
                        verify_info["mode"],
                        f"{float(verify_info['elapsed_sec']):.3f}",
                        f"{float(xsr.get('proj_v_sigma_ave', np.nan)):.6f}",
                        f"{float(xsr.get('proj_v_mean_ave', np.nan)):.6f}",
                        f"{float(xsr.get('proj_h_sigma_ave', np.nan)):.6f}",
                        f"{float(xsr.get('proj_h_mean_ave', np.nan)):.6f}",
                        f"{best_now:.6f}",
                    ])
                    f.flush()
                    emit_progress(note, xq, v_sigma, best_now, xsr, verify_info)
                    return v_sigma

                apply_and_measure(cur, "INIT_CUR")

                for idx in range(max(0, min_init - 1)):
                    if self._stopped() or len(X) >= max_evals:
                        break
                    rnd = lo + (hi - lo) * np.random.rand(len(names))
                    apply_and_measure(rnd, f"INIT_RAND_{idx + 1}")

                while (not self._stopped()) and len(X) < max_evals:
                    X_train = np.vstack(X)
                    y_train = -np.asarray(Y, dtype=float)
                    candidates = lo + (hi - lo) * np.random.rand(cand_pool, len(names))
                    candidates = np.vstack([quantize_vec(candidates[i]) for i in range(candidates.shape[0])])

                    seen = set(key(x) for x in X_train)
                    cand_list = []
                    for idx in range(candidates.shape[0]):
                        k = key(candidates[idx])
                        if k in seen:
                            continue
                        cand_list.append(candidates[idx])
                        if len(cand_list) >= cand_pool:
                            break
                    if not cand_list:
                        break

                    C = np.vstack(cand_list)
                    mu, std = gp_posterior_nd(X_train, y_train, C, length_scales=length_scales, sigma_f=sigma_f, sigma_n=sigma_n)
                    y_best = float(np.max(y_train))
                    ei = expected_improvement(mu, std, y_best, xi=xi)
                    best_idx = int(np.argmax(ei))
                    if float(ei[best_idx]) <= 0.0:
                        break

                    apply_and_measure(C[best_idx], "BO_EI")

                best_i = int(np.argmin(Y))
                best_x = np.asarray(X[best_i], dtype=float)
                best_y = float(Y[best_i])
                self.interface.set_skew_currents({name: float(val) for name, val in zip(names, best_x)})
            self.finished.emit({
                "names": names,
                "best_setpoints": best_x.tolist(),
                "best_v_sigma": best_y,
                "selection": selection,
                "canceled": self._stopped(),
                "csv_path": str(csv_path),
            })
        except Exception:
            self.failed.emit(traceback.format_exc())


class MainWindow(QMainWindow):
    def __init__(self, interface):
        super().__init__()
        self.interface = interface
        self.worker = None
        self.raw_history = []
        self.best_history = []
        self.group_offset = 0
        self.current_selection = []
        self.current_csv_path = None

        self.setWindowTitle("DR Skew Bayesian Optimization")
        root = QWidget(self)
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        cfg_group = QGroupBox("Configuration", self)
        cfg_form = QFormLayout(cfg_group)
        outer.addWidget(cfg_group)

        self.series_combo = QComboBox(self)
        self.series_combo.addItems(self.interface.get_skew_series_names())
        cfg_form.addRow("Series", self.series_combo)

        self.count_spin = QSpinBox(self)
        self.count_spin.setRange(1, 10)
        self.count_spin.setValue(5)
        cfg_form.addRow("Number of skews", self.count_spin)

        self.step_spin = QDoubleSpinBox(self)
        self.step_spin.setRange(0.01, 1.0)
        self.step_spin.setDecimals(2)
        self.step_spin.setSingleStep(0.01)
        self.step_spin.setValue(0.05)
        cfg_form.addRow("Step", self.step_spin)

        self.range_info_lbl = QLabel(f"Fixed scan width: {SCAN_WIDTH_A:.1f} A (±{SCAN_HALF_WIDTH_A:.1f} A), |I| <= {SKEW_ABS_LIMIT_A:.1f} A", self)
        cfg_form.addRow("Scan range", self.range_info_lbl)

        self.verify_timeout_spin = QDoubleSpinBox(self)
        self.verify_timeout_spin.setRange(0.5, 30.0)
        self.verify_timeout_spin.setDecimals(1)
        self.verify_timeout_spin.setSingleStep(0.5)
        self.verify_timeout_spin.setValue(5.0)
        cfg_form.addRow("Set verify timeout [s]", self.verify_timeout_spin)

        self.xsr_wait_spin = QDoubleSpinBox(self)
        self.xsr_wait_spin.setRange(0.5, 60.0)
        self.xsr_wait_spin.setDecimals(1)
        self.xsr_wait_spin.setSingleStep(0.5)
        self.xsr_wait_spin.setValue(7.0)
        cfg_form.addRow("XSR wait after set [s]", self.xsr_wait_spin)

        self.init_spin = QSpinBox(self)
        self.init_spin.setRange(2, 50)
        self.init_spin.setValue(8)
        cfg_form.addRow("Initial points", self.init_spin)

        self.maxeval_spin = QSpinBox(self)
        self.maxeval_spin.setRange(3, 500)
        self.maxeval_spin.setValue(30)
        cfg_form.addRow("Max evals", self.maxeval_spin)

        self.pool_spin = QSpinBox(self)
        self.pool_spin.setRange(16, 5000)
        self.pool_spin.setSingleStep(16)
        self.pool_spin.setValue(256)
        cfg_form.addRow("Candidate pool", self.pool_spin)

        btn_row = QHBoxLayout()
        outer.addLayout(btn_row)
        self.preview_btn = QPushButton("Preview top skews", self)
        self.refresh_btn = QPushButton("Refresh current values", self)
        self.run_btn = QPushButton("Run skew BO", self)
        self.stop_btn = QPushButton("Stop", self)
        self.stop_btn.setEnabled(False)
        btn_row.addWidget(self.preview_btn)
        btn_row.addWidget(self.refresh_btn)
        btn_row.addWidget(self.run_btn)
        btn_row.addWidget(self.stop_btn)

        self.group_lbl = QLabel("Group: 1", self)
        outer.addWidget(self.group_lbl)

        self.status_lbl = QLabel("Objective: minimize XSR V_sigma", self)
        outer.addWidget(self.status_lbl)

        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["Name", "Current", "|Current|", "Scan range"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        outer.addWidget(self.table)

        self.fig = Figure(figsize=(8, 4), tight_layout=True)
        self.canvas = FigureCanvas(self.fig)
        self.ax_obj = self.fig.add_subplot(111)
        outer.addWidget(self.canvas)

        self.log_box = QPlainTextEdit(self)
        self.log_box.setReadOnly(True)
        outer.addWidget(self.log_box, stretch=1)

        self.preview_btn.clicked.connect(self.refresh_preview)
        self.refresh_btn.clicked.connect(self.refresh_preview)
        self.run_btn.clicked.connect(self.start_optimization)
        self.stop_btn.clicked.connect(self.stop_optimization)
        self.series_combo.currentTextChanged.connect(self._reset_and_refresh_preview)
        self.count_spin.valueChanged.connect(self._reset_and_refresh_preview)
        self.step_spin.valueChanged.connect(self.refresh_preview)

        self.refresh_preview()

    def log(self, text):
        self.log_box.appendPlainText(text)

    def _reset_and_refresh_preview(self):
        self.group_offset = 0
        self.refresh_preview()

    def _scan_candidates(self, current):
        step = max(self.step_spin.value(), 1e-9)
        center = float(current)
        lo = center - SCAN_HALF_WIDTH_A
        hi = center + SCAN_HALF_WIDTH_A
        if lo < -SKEW_ABS_LIMIT_A:
            lo = -SKEW_ABS_LIMIT_A
            hi = lo + SCAN_WIDTH_A
        if hi > SKEW_ABS_LIMIT_A:
            hi = SKEW_ABS_LIMIT_A
            lo = hi - SCAN_WIDTH_A
        lo = max(lo, -SKEW_ABS_LIMIT_A)
        hi = min(hi, SKEW_ABS_LIMIT_A)
        vals = np.arange(lo, hi + 0.5 * step, step, dtype=float)
        vals = np.clip(np.round(vals / step) * step, -SKEW_ABS_LIMIT_A, SKEW_ABS_LIMIT_A)
        vals = np.unique(np.round(vals, 10))
        return vals if vals.size else np.array([float(np.clip(center, -SKEW_ABS_LIMIT_A, SKEW_ABS_LIMIT_A))], dtype=float)

    def _build_selection_snapshot(self):
        skews = self.interface.get_skews(self.series_combo.currentText().strip())
        vals = np.asarray(skews["current"], dtype=float)
        valid = np.where(np.isfinite(vals))[0]
        ranked = valid[np.argsort(-np.abs(vals[valid]))]
        start = int(self.group_offset)
        stop = start + self.count_spin.value()
        chosen = ranked[start:stop]
        selection = []
        for idx in chosen:
            current = float(vals[idx])
            candidates = self._scan_candidates(current)
            selection.append({
                "name": str(skews["names"][idx]),
                "current": current,
                "abs_current": abs(current),
                "candidates": candidates.tolist(),
                "lo": float(np.min(candidates)),
                "hi": float(np.max(candidates)),
            })
        return selection, len(ranked)

    def _populate_table(self, selection):
        self.table.setRowCount(len(selection))
        for row, item in enumerate(selection):
            candidates = np.asarray(item["candidates"], dtype=float)
            scan_txt = f"{float(np.min(candidates)):.2f} .. {float(np.max(candidates)):.2f} ({len(candidates)} pts)"
            values = (
                str(item["name"]),
                f"{float(item['current']):.3f}",
                f"{float(item['abs_current']):.3f}",
                scan_txt,
            )
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))

    def refresh_preview(self):
        try:
            selection, total_ranked = self._build_selection_snapshot()
            self.current_selection = [dict(item) for item in selection]
            self._populate_table(self.current_selection)
            group_no = self.group_offset // max(self.count_spin.value(), 1) + 1
            self.group_lbl.setText(f"Group: {group_no}")
            if len(selection) > 0:
                self.status_lbl.setText(
                    f"Objective: minimize XSR V_sigma | group {group_no} | "
                    f"selected {', '.join(item['name'] for item in selection)}"
                )
            else:
                self.status_lbl.setText("Objective: minimize XSR V_sigma | no valid skew current readback")
        except Exception as e:
            self.status_lbl.setText(f"Preview error: {e}")

    def _clear_history(self):
        self.raw_history.clear()
        self.best_history.clear()
        self._redraw()

    def _redraw(self):
        self.ax_obj.clear()
        if self.raw_history:
            xs = range(1, len(self.raw_history) + 1)
            self.ax_obj.plot(xs, self.raw_history, marker="o", label="V_sigma")
            self.ax_obj.plot(xs, self.best_history, linestyle="--", label="best")
            self.ax_obj.legend(fontsize=8)
        self.ax_obj.set_title("XSR V_sigma")
        self.ax_obj.set_xlabel("Evaluation")
        self.ax_obj.grid(True, alpha=0.3)
        self.canvas.draw_idle()

    def _config_payload(self):
        return {
            "series": self.series_combo.currentText(),
            "count": self.count_spin.value(),
            "step": self.step_spin.value(),
            "verify_timeout_sec": self.verify_timeout_spin.value(),
            "xsr_wait_sec": self.xsr_wait_spin.value(),
            "min_init": self.init_spin.value(),
            "max_evals": self.maxeval_spin.value(),
            "cand_pool": self.pool_spin.value(),
            "selection": [dict(item) for item in self.current_selection],
            "group_offset": self.group_offset,
        }

    def start_optimization(self):
        if self.worker is not None:
            return
        self._clear_history()
        self.refresh_preview()
        if len(self.current_selection) == 0:
            QMessageBox.warning(self, "DR Skew BO", "No valid skew group is available.")
            return
        self.worker = SkewBOWorker(self.interface, self._config_payload())
        self.worker.progress.connect(self.on_progress)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.pause_requested.connect(self.on_pause_requested)
        self.worker.start()
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText("Running skew BO...")
        self.log(f"Starting DR skew BO for {', '.join(item['name'] for item in self.current_selection)}")

    def stop_optimization(self):
        if self.worker is not None:
            self.worker.stop()
            self.status_lbl.setText("Stop requested. Finishing current evaluation...")
            self.log("Stop requested")

    def on_pause_requested(self, payload):
        if self.worker is None:
            return
        text = (
            f"{payload['message']}\n\n"
            f"Names: {', '.join(payload['names'])}\n"
            f"Readback: {np.array2string(np.asarray(payload['readback']), precision=3)}\n"
            f"Target: {np.array2string(np.asarray(payload['target']), precision=3)}"
        )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Magnet Drop Detected")
        box.setText(text)
        resume_btn = box.addButton("Resume", QMessageBox.ButtonRole.AcceptRole)
        save_btn = box.addButton("Save and Finish", QMessageBox.ButtonRole.DestructiveRole)
        box.exec()
        if box.clickedButton() == resume_btn:
            self.log("Operator chose Resume after magnet drop pause.")
            self.worker.resume_from_pause("resume")
        else:
            self.log("Operator chose Save and Finish after magnet drop pause.")
            self.worker.resume_from_pause("stop")

    def on_progress(self, payload):
        self.raw_history.append(float(payload["v_sigma"]))
        self.best_history.append(float(payload["best_v_sigma"]))
        self._redraw()
        self.status_lbl.setText(
            f"{payload['note']}: V_sigma={payload['v_sigma']:.6f}, best={payload['best_v_sigma']:.6f}, "
            f"set-check={payload['verify_mode']}"
        )
        self.log(
            f"[{payload['eval_count']:02d}] {payload['note']} "
            f"V_sigma={payload['v_sigma']:.6f} best={payload['best_v_sigma']:.6f} "
            f"verify={payload['verify_mode']} after {payload['verify_elapsed_sec']:.2f}s "
            f"readback={np.array2string(np.asarray(payload['readback']), precision=3)} "
            f"set={np.array2string(np.asarray(payload['setpoints']), precision=3)}"
        )

    def _cleanup_worker(self):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.worker = None

    def on_finished(self, payload):
        self._cleanup_worker()
        self.current_csv_path = payload.get("csv_path")
        msg = (
            f"Finished. Best XSR V_sigma = {float(payload['best_v_sigma']):.6f}\n"
            f"CSV: {payload['csv_path']}"
        )
        if payload.get("canceled"):
            msg = "Stopped. " + msg
        self.status_lbl.setText(msg)
        self.log(msg)
        if payload.get("canceled"):
            QMessageBox.information(self, "DR Skew BO", msg)
            return

        _, total_ranked = self._build_selection_snapshot()
        next_offset = self.group_offset + self.count_spin.value()
        if next_offset >= total_ranked:
            QMessageBox.information(self, "DR Skew BO", msg)
            return

        box = QMessageBox(self)
        box.setWindowTitle("Next Group")
        box.setText(msg + "\n\nContinue with the next 5 skew parameters?")
        next_btn = box.addButton("Next 5 and Continue", QMessageBox.ButtonRole.AcceptRole)
        finish_btn = box.addButton("Save and Finish", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() != next_btn:
            return

        self.group_offset = next_offset
        self.refresh_preview()
        self.start_optimization()

    def on_failed(self, message):
        self._cleanup_worker()
        self.status_lbl.setText("Optimization failed")
        self.log(message)
        QMessageBox.critical(self, "DR Skew BO error", message)


def main():
    app = QApplication(sys.argv)
    interface = InterfaceATF2_DR(nsamples=10)
    window = MainWindow(interface)
    window.resize(980, 820)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
