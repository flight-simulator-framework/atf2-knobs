from Backend.State import State
from datetime import datetime
from collections import deque

import numpy as np
import threading
import sys
import os

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QPushButton, QSpinBox, QDoubleSpinBox,
    QComboBox, QFileDialog, QSizePolicy, QGroupBox, QTabWidget, QPlainTextEdit
)
from PyQt6.QtGui import QPixmap, QIcon, QPainter, QTextCursor
from PyQt6.QtCore import Qt, QThread, QTimer, QObject, pyqtSignal

import matplotlib

matplotlib.use("QtAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure


def orbit_from_bpms(bpms, names=None):
    names_all = list(bpms.get("names", []))
    x_all = np.asarray(bpms.get("x", []), dtype=float)
    y_all = np.asarray(bpms.get("y", []), dtype=float)
    tmit_all = np.asarray(bpms.get("tmit", []), dtype=float)

    if names is not None:
        idx_map = {str(n): i for i, n in enumerate(names_all)}
        idx = [idx_map[str(n)] for n in names if str(n) in idx_map]
        names_use = [names_all[i] for i in idx]
        x_all = x_all[:, idx]
        y_all = y_all[:, idx]
        tmit_all = tmit_all[:, idx]
    else:
        names_use = names_all

    x = np.mean(x_all, axis=0)
    y = np.mean(y_all, axis=0)
    stdx = np.std(x_all, axis=0)
    stdy = np.std(y_all, axis=0)
    tmit = np.mean(tmit_all, axis=0)
    faulty = np.isnan(x) | np.isnan(y)
    x[faulty] = np.nan
    y[faulty] = np.nan

    return {
        "names": names_use,
        "x": x,
        "y": y,
        "stdx": stdx,
        "stdy": stdy,
        "tmit": tmit,
        "faulty": faulty,
        "nbpms": len(names_use),
    }


def save_machine_state(interface, filename):
    s = State()
    s.correctors = interface.get_correctors()
    if hasattr(interface, "get_quadrupoles"):
        try:
            s.quadrupoles = interface.get_quadrupoles()
        except Exception:
            s.quadrupoles = None
    else:
        s.quadrupoles = None
    s.bpms = interface.get_bpms()
    s.icts = interface.get_icts() if hasattr(interface, "get_icts") else {"names": [], "charge": np.array([])}
    s.sequence = interface.get_sequence()
    s.hcorrectors_names = interface.get_hcorrectors_names()
    s.vcorrectors_names = interface.get_vcorrectors_names()
    s.timestamp = datetime.now()
    s.save(filename=filename)


class MatplotlibWidget(FigureCanvas):

    def __init__(self, parent=None, title=""):
        fig = Figure(tight_layout=True)
        super().__init__(fig)
        self.setParent(parent)
        self.axes = fig.add_subplot(111)
        self.axes.set_title(title)


class OrbitPlotWidget(FigureCanvas):

    def __init__(self, parent=None):
        fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True, tight_layout=True)
        super().__init__(fig)
        self.setParent(parent)
        self.ax_x = ax1
        self.ax_y = ax2
        self.ax_x.set_ylabel("X [mm]")
        self.ax_y.set_ylabel("Y [mm]")
        self.ax_y.set_xlabel("S [m]")

    def update_orbit(
            self,
            s_m,
            x_mm,
            y_mm,
            faulty=None,
            corr_S=None,
            corr_name=None,
    ):
        # --- 型と shape を強制 ---
        s_m = np.asarray(s_m, dtype=float).reshape(-1)
        x_mm = np.asarray(x_mm, dtype=float).reshape(-1)
        y_mm = np.asarray(y_mm, dtype=float).reshape(-1)

        n = min(len(s_m), len(x_mm), len(y_mm))
        s_m = s_m[:n]
        x_mm = x_mm[:n]
        y_mm = y_mm[:n]

        self.ax_x.clear()
        self.ax_y.clear()

        self.ax_x.plot(s_m, x_mm, marker="o")
        self.ax_y.plot(s_m, y_mm, marker="o")

        self.ax_x.set_ylabel("X [mm]")
        self.ax_y.set_ylabel("Y [mm]")
        self.ax_y.set_xlabel("S [m]")

        # --- faulty BPM を × で表示 ---
        if faulty is not None:
            faulty = np.asarray(faulty, dtype=bool).reshape(-1)
            faulty = faulty[:n]

            if faulty.any():
                sf = s_m[faulty]
                zeros = np.zeros(sf.size)

                self.ax_x.plot(
                    sf, zeros,
                    linestyle="None",
                    marker="x",
                    color="red",
                    markersize=8,
                    label="faulty BPM",
                )
                self.ax_y.plot(
                    sf, zeros,
                    linestyle="None",
                    marker="x",
                    color="red",
                    markersize=8,
                )

        # --- corrector position ---
        if corr_S is not None and np.isfinite(corr_S):
            if corr_name is not None:
                if corr_name[:2].upper() == "ZH":
                    self.ax_x.axvline(float(corr_S), color="pink", alpha=0.3)
                elif corr_name[:2].upper() == "ZV":
                    self.ax_y.axvline(float(corr_S), color="pink", alpha=0.3)

        self.draw()
        self.flush_events()

        def _autoscale_with_min(ax, data, min_half_range):
            arr = np.asarray(data, dtype=float)
            arr = arr[np.isfinite(arr)]
            if arr.size == 0:
                ax.set_ylim(-min_half_range, min_half_range)
                return
            dmin = float(arr.min())
            dmax = float(arr.max())
            center = 0.5 * (dmin + dmax)
            span = dmax - dmin
            half = 0.5 * span * 1.2
            if half <= 0.0:
                half = min_half_range
            half = max(half, min_half_range)
            ax.set_ylim(center - half, center + half)

        _autoscale_with_min(self.ax_x, x_mm, 2.0)
        _autoscale_with_min(self.ax_y, y_mm, 2.0)

        self.draw()
        self.flush_events()


class DispersionPlotWidget(FigureCanvas):
    def __init__(self, parent=None):
        fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True, tight_layout=True)
        super().__init__(fig)
        self.setParent(parent)
        self.ax_h = ax1
        self.ax_v = ax2
        self.ax_h.set_ylabel("ηx [mm]")
        self.ax_v.set_ylabel("ηy [mm]")
        self.ax_v.set_xlabel("S [m]")

    def update_dispersion(
            self,
            s_m: np.ndarray,
            eta_x: np.ndarray,
            eta_y: np.ndarray,
            eta_x0: np.ndarray | None,
            eta_y0: np.ndarray | None,
    ):
        self.ax_h.clear()
        self.ax_v.clear()

        # baseline
        if eta_x0 is not None and len(eta_x0) == len(s_m):
            self.ax_h.plot(s_m, eta_x0, color="0.7", linestyle="--", label="H Target Dispersion")
        if eta_y0 is not None and len(eta_y0) == len(s_m):
            self.ax_v.plot(s_m, eta_y0, color="0.7", linestyle="--", label="V Target Dispersion")

        self.ax_h.plot(s_m, eta_x, marker="o", label="H current")
        self.ax_v.plot(s_m, eta_y, marker="o", label="V current")

        self.ax_h.set_ylabel("ηx [mm]")
        self.ax_v.set_ylabel("ηy [mm]")
        self.ax_v.set_xlabel("S [m]")

        def _autoscale_with_min(ax, data, min_half_range):
            arr = np.asarray(data, dtype=float)
            arr = arr[np.isfinite(arr)]
            if arr.size == 0:
                ax.set_ylim(-min_half_range, min_half_range)
                return
            dmin = float(arr.min())
            dmax = float(arr.max())
            center = 0.5 * (dmin + dmax)
            span = dmax - dmin
            half = 0.5 * span * 1.2
            if half <= 0.0:
                half = min_half_range
            half = max(half, min_half_range)
            ax.set_ylim(center - half, center + half)

        _autoscale_with_min(self.ax_h, eta_x, 1500.0)
        _autoscale_with_min(self.ax_v, eta_y, 200.0)

        self.ax_h.legend(loc="upper left")
        self.ax_v.legend(loc="upper left")

        self.draw()
        self.flush_events()


# ---------------------------------------------------------
# Worker (SysID
# ---------------------------------------------------------
class Worker(QObject):
    plot_data = pyqtSignal(dict, np.ndarray, np.ndarray, np.ndarray, np.ndarray, str)
    finished = pyqtSignal()
    suggestion_ready = pyqtSignal(object)

    def __init__(
            self,
            interface,
            correctors,
            bpms,
            kicks,
            max_osc_h,
            max_osc_v,
            max_curr_h,
            max_curr_v,
            Niter,
            running_flag,
    ):
        super().__init__()
        self.interface = interface
        self.correctors = correctors
        self.bpms = bpms
        self.kicks = kicks
        self.max_osc_h = max_osc_h
        self.max_osc_v = max_osc_v
        self.max_curr_h = max_curr_h
        self.max_curr_v = max_curr_v
        self.Niter = Niter
        self.running = running_flag

        self.Rx = None
        self.Ry = None

    def run(self):
        I = self.interface
        kicks = self.kicks
        hcorrs = set(I.get_hcorrectors_names())
        vcorrs = set(I.get_vcorrectors_names())

        def get_corrector_bdes(name):
            corr = I.get_correctors()
            names = list(corr.get("names", []))
            bdes = np.asarray(corr.get("bdes", []), dtype=float)
            for n, v in zip(names, bdes):
                if n == name:
                    return float(v)
            raise KeyError(f"Corrector not found: {name}")

        def clamp(val, max_val):
            if max_val == 0.0:
                return val
            return max(-max_val, min(val, max_val))

        for iter in range(self.Niter):
            if not self.running.is_set():
                break
            for icorr, corrector in enumerate(self.correctors):
                if not self.running.is_set():
                    break

                corr_bdes = get_corrector_bdes(corrector)
                kick = kicks[icorr]

                print(f"Corrector {corrector} '+' excitation...")
                curr_p = corr_bdes + kick
                if corrector in hcorrs:
                    curr_p = clamp(curr_p, self.max_osc_h)
                else:
                    curr_p = clamp(curr_p, self.max_osc_v)
                I.push(corrector, curr_p)
                save_machine_state(I, filename=f"DATA_{corrector}_p{iter:04d}.pkl")
                Op = orbit_from_bpms(I.get_bpms(), self.bpms)

                print(f"Corrector {corrector} '-' excitation...")
                curr_m = corr_bdes - kick
                if corrector in hcorrs:
                    curr_m = clamp(curr_m, self.max_osc_h)
                else:
                    curr_m = clamp(curr_m, self.max_osc_v)
                I.push(corrector, curr_m)
                save_machine_state(I, filename=f"DATA_{corrector}_m{iter:04d}.pkl")
                Om = orbit_from_bpms(I.get_bpms(), self.bpms)

                I.push(corrector, corr_bdes)

                Diff_x = (Op["x"] - Om["x"]) / 2.0
                Diff_y = (Op["y"] - Om["y"]) / 2.0

                if kick != 0.0:
                    if self.Rx is None:
                        self.Rx = np.zeros((Diff_x.size, len(self.correctors)))
                        self.Ry = np.zeros((Diff_y.size, len(self.correctors)))
                    self.Rx[:, icorr] = Diff_x / kick
                    self.Ry[:, icorr] = Diff_y / kick

                nsamples = Op["stdx"].size
                Err_x = (
                        np.sqrt(np.square(Op["stdx"]) + np.square(Om["stdx"]))
                        / np.sqrt(nsamples)
                )
                Err_y = (
                        np.sqrt(np.square(Op["stdy"]) + np.square(Om["stdy"]))
                        / np.sqrt(nsamples)
                )

                if corrector in hcorrs:
                    Diff_x_clean = Diff_x[~np.isnan(Diff_x)]
                    if Diff_x_clean.size > 0 and np.max(np.abs(Diff_x_clean)) != 0.0:
                        kicks[icorr] *= self.max_osc_h / np.max(np.abs(Diff_x_clean))
                else:
                    Diff_y_clean = Diff_y[~np.isnan(Diff_y)]
                    if Diff_y_clean.size > 0 and np.max(np.abs(Diff_y_clean)) != 0.0:
                        kicks[icorr] *= self.max_osc_v / np.max(np.abs(Diff_y_clean))

                kicks[icorr] = 0.8 * kicks[icorr] + 0.2 * kick
                np.savetxt("kicks.txt", kicks, delimiter="\n")

                self.plot_data.emit(Op, Diff_x, Err_x, Diff_y, Err_y, corrector)

        try:
            if self.Rx is not None and self.Ry is not None:
                I = self.interface
                orbit_now = orbit_from_bpms(I.get_bpms(), self.bpms)
                x_now = np.asarray(orbit_now["x"], dtype=float)
                y_now = np.asarray(orbit_now["y"], dtype=float)
                if x_now.ndim == 2:
                    x_now = np.mean(x_now, axis=0)
                if y_now.ndim == 2:
                    y_now = np.mean(y_now, axis=0)

                x_now = x_now.reshape(-1)
                y_now = y_now.reshape(-1)

                np.save("R_x.npy", self.Rx)
                np.save("R_y.npy", self.Ry)
                np.save("R_correctors.npy", np.array(self.correctors, dtype=object))
                np.save("R_bpms.npy", np.array(self.bpms, dtype=object))

                suggestion = {}

                # H-plane
                try:
                    h_names_all = set(I.get_hcorrectors_names())
                except Exception:
                    h_names_all = set(self.correctors)

                h_idx = [i for i, name in enumerate(self.correctors) if name in h_names_all]
                if len(h_idx) > 0:
                    R_sub = self.Rx[:, h_idx]
                    if R_sub.ndim == 0:
                        R_sub = np.array([[float(R_sub)]])
                    elif R_sub.ndim == 1:
                        R_sub = R_sub[:, np.newaxis]
                    dk_h, *_ = np.linalg.lstsq(R_sub, -x_now, rcond=1e-3)
                    dk_h = np.asarray(dk_h, dtype=float)
                    dk_h = np.clip(dk_h, -5.0, 5.0)
                    corr_h = [self.correctors[i] for i in h_idx]
                    suggestion["correctors_h"] = corr_h
                    suggestion["delta_k_h"] = dk_h
                    np.save("suggestion_h_correctors.npy", np.array(corr_h, dtype=object))
                    np.save("suggestion_h_delta_k.npy", dk_h)

                # V-plane
                try:
                    v_names_all = set(I.get_vcorrectors_names())
                except Exception:
                    v_names_all = set(self.correctors)

                v_idx = [i for i, name in enumerate(self.correctors) if name in v_names_all]
                if len(v_idx) > 0:
                    R_sub = self.Ry[:, v_idx]
                    if R_sub.ndim == 0:
                        R_sub = np.array([[float(R_sub)]])
                    elif R_sub.ndim == 1:
                        R_sub = R_sub[:, np.newaxis]
                    dk_v, *_ = np.linalg.lstsq(R_sub, -y_now, rcond=1e-3)
                    dk_v = np.asarray(dk_v, dtype=float)
                    dk_v = np.clip(dk_v, -5.0, 5.0)
                    corr_v = [self.correctors[i] for i in v_idx]
                    suggestion["correctors_v"] = corr_v
                    suggestion["delta_k_v"] = dk_v
                    np.save("suggestion_v_correctors.npy", np.array(corr_v, dtype=object))
                    np.save("suggestion_v_delta_k.npy", dk_v)

                self.suggestion_ready.emit(suggestion)
        except Exception as e:
            print(f"Failed to compute/save orbit-correction suggestion: {e}")

        self.finished.emit()


class EmittingStream(QObject):
    textWritten = pyqtSignal(str)

    def write(self, text):
        if text:
            self.textWritten.emit(str(text))

    def flush(self):
        pass


# ---------------------------------------------------------
# Main Window
# ---------------------------------------------------------
class MainWindow(QMainWindow):
    def __set_status_in_title(self, status):
        self.setWindowTitle(
            "Ext Tuner - " + self.interface.__class__.__name__ + " " + status
        )
        self.setWindowIcon(QIcon("SysID_GUI/CERN_logo.png"))

        self.logo = QPixmap("SysID_GUI/CERN_logo.png")
        self.logo = self.logo.scaled(
            75,
            75,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.show()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setOpacity(0.5)
        x = self.width() - self.logo.width() - 2
        y = 2
        painter.drawPixmap(x, y, self.logo)

    def __init__(self, interface, dir_name):
        super().__init__()

        self.cwd = os.getcwd()
        self.interface = interface
        bpms_list = self.interface.get_bpms()["names"]
        correctors = self.interface.get_correctors()
        correctors_list = correctors["names"]

        if correctors_list is not None:
            hcorrs = self.interface.get_hcorrectors_names()
            vcorrs = self.interface.get_vcorrectors_names()
            hcorr_indexes = np.array(
                [i for i, s in enumerate(correctors_list) if s in hcorrs], dtype=int
            )
            vcorr_indexes = np.array(
                [i for i, s in enumerate(correctors_list) if s in vcorrs], dtype=int
            )

            def clean_array(a):
                a = np.array([0 if x is None else x for x in a], dtype=float)
                a[np.isnan(a)] = 0
                return a

            max_curr_h = 1.15 * np.max(
                np.abs(clean_array(np.array(correctors["bdes"])[hcorr_indexes]))
            )
            max_curr_v = 1.15 * np.max(
                np.abs(clean_array(np.array(correctors["bdes"])[vcorr_indexes]))
            )
        else:
            max_curr_h = 1.0
            max_curr_v = 1.0

        self._misalign_baseline_eta_x = None
        self._misalign_baseline_eta_y = None

        self._last_orbit_s = None
        self._last_orbit_x = None
        self._last_orbit_y = None

        self._last_suggestion = None

        self.running = threading.Event()
        self.worker_thread = None

        self.__set_status_in_title("[Idle]")
        self.setGeometry(100, 100, 1200, 900)

        main_widget = QWidget()
        self.setCentralWidget(main_widget)

        main_layout = QVBoxLayout(main_widget)

        # --------------------------------------
        # Tab Widget （ Orbit / Dispersion / Misalignment）
        # --------------------------------------
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs, 1)

        # === Response tab DISABLED ===
        ENABLE_RESPONSE_TAB = False

        if ENABLE_RESPONSE_TAB:
            response_tab = QWidget()
            response_main_layout = QHBoxLayout(response_tab)
            self.tabs.addTab(response_tab, "Response")

            resp_left = QVBoxLayout()
            response_main_layout.addLayout(resp_left, 1)

            correctors_layout = QHBoxLayout()
            resp_left.addLayout(correctors_layout)

            correctors_label = QLabel("Correctors:")
            correctors_layout.addWidget(correctors_label)

            self.correctors_list = QListWidget()
            self.correctors_list.setSelectionMode(
                QListWidget.SelectionMode.ExtendedSelection
            )
            self.correctors_list.insertItems(0, correctors_list)
            resp_left.addWidget(self.correctors_list)

            button_layout = QHBoxLayout()
            resp_left.addLayout(button_layout)

            self.save_correctors_button = QPushButton("Save As..")
            self.save_correctors_button.clicked.connect(
                self.__save_correctors_button_clicked
            )
            button_layout.addWidget(self.save_correctors_button)

            self.load_correctors_button = QPushButton("Load..")
            self.load_correctors_button.clicked.connect(
                self.__load_correctors_button_clicked
            )
            button_layout.addWidget(self.load_correctors_button)

            self.clear_correctors_button = QPushButton("Clear")
            self.clear_correctors_button.clicked.connect(
                self.__clear_correctors_button_clicked
            )
            button_layout.addWidget(self.clear_correctors_button)

            bpms_layout = QHBoxLayout()
            resp_left.addLayout(bpms_layout)

            bpms_label = QLabel("BPMs:")
            bpms_layout.addWidget(bpms_label)

            self.bpms_list = QListWidget()
            self.bpms_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
            self.bpms_list.insertItems(0, bpms_list)
            resp_left.addWidget(self.bpms_list)

            button_layout2 = QHBoxLayout()
            resp_left.addLayout(button_layout2)

            self.save_bpms_button = QPushButton("Save As..")
            self.save_bpms_button.clicked.connect(self.__save_bpms_button_clicked)
            button_layout2.addWidget(self.save_bpms_button)

            self.load_bpms_button = QPushButton("Load..")
            self.load_bpms_button.clicked.connect(self.__load_bpms_button_clicked)
            button_layout2.addWidget(self.load_bpms_button)

            self.clear_bpms_button = QPushButton("Clear")
            self.clear_bpms_button.clicked.connect(self.__clear_bpms_button_clicked)
            button_layout2.addWidget(self.clear_bpms_button)

            resp_right = QVBoxLayout()
            response_main_layout.addLayout(resp_right, 2)

            self.info_label = QLabel("Data Storage:")
            self.info_label.setSizePolicy(
                QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
            )
            resp_right.addWidget(self.info_label)

            self.working_directory_input = QLineEdit("Working directory:")
            self.working_directory_input.setText(dir_name)
            resp_right.addWidget(self.working_directory_input)

            self.options_label = QLabel("SysID Options")
            self.options_label.setSizePolicy(
                QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
            )
            resp_right.addWidget(self.options_label)

            options_layout = QVBoxLayout()
            resp_right.addLayout(options_layout)

            cycle_mode_layout = QHBoxLayout()
            options_layout.addLayout(cycle_mode_layout)

            self.cycle_mode_label = QLabel("Cycle mode")
            cycle_mode_layout.addWidget(self.cycle_mode_label)

            self.cycle_mode_combobox = QComboBox()
            self.cycle_mode_combobox.addItems(["Repeat all"])
            cycle_mode_layout.addWidget(self.cycle_mode_combobox)

            current_layout = QHBoxLayout()
            options_layout.addLayout(current_layout)

            self.current_label = QLabel("Max current (A)")
            current_layout.addWidget(self.current_label)
            current_layout.addStretch()

            self.horizontal_current_label = QLabel("H:")
            current_layout.addWidget(self.horizontal_current_label)

            self.max_horizontal_current_spinbox = QDoubleSpinBox()
            self.max_horizontal_current_spinbox.setValue(max_curr_h)
            self.max_horizontal_current_spinbox.setSingleStep(0.01)
            current_layout.addWidget(self.max_horizontal_current_spinbox)

            self.vertical_current_label = QLabel(" V:")
            current_layout.addWidget(self.vertical_current_label)

            self.max_vertical_current_spinbox = QDoubleSpinBox()
            self.max_vertical_current_spinbox.setValue(max_curr_v)
            self.max_vertical_current_spinbox.setSingleStep(0.01)
            current_layout.addWidget(self.max_vertical_current_spinbox)

            excursion_layout = QHBoxLayout()
            options_layout.addLayout(excursion_layout)

            self.excursion_label = QLabel("Orbit excursion (mm)")
            excursion_layout.addWidget(self.excursion_label)
            excursion_layout.addStretch()

            self.horizontal_excursion_label = QLabel("H:")
            excursion_layout.addWidget(self.horizontal_excursion_label)

            self.horizontal_excursion_spinbox = QDoubleSpinBox()
            self.horizontal_excursion_spinbox.setValue(0.5)
            self.horizontal_excursion_spinbox.setSingleStep(0.1)
            excursion_layout.addWidget(self.horizontal_excursion_spinbox)

            self.vertical_excursion_label = QLabel("V:")
            excursion_layout.addWidget(self.vertical_excursion_label)

            self.vertical_excursion_spinbox = QDoubleSpinBox()
            self.vertical_excursion_spinbox.setValue(0.5)
            self.vertical_excursion_spinbox.setSingleStep(0.1)
            excursion_layout.addWidget(self.vertical_excursion_spinbox)

            self.plot = MatplotlibWidget(self, title="Response (SysID)")
            resp_right.addWidget(self.plot)

            self._plot_queue = deque()
            self._plot_timer = QTimer()
            self._plot_timer.timeout.connect(self.__flush_plot_queue)
            self._plot_timer.start(200)

            buttons_layout = QHBoxLayout()
            resp_right.addLayout(buttons_layout)

            self.start_button = QPushButton("START SysID")
            self.start_button.setStyleSheet("background-color: red; color: white;")
            self.start_button.clicked.connect(self.__start_button_clicked)
            buttons_layout.addWidget(self.start_button)

            self.stop_button = QPushButton("STOP SysID")
            self.stop_button.setStyleSheet("background-color: green; color: white;")
            self.stop_button.clicked.connect(self.__stop_button_clicked)
            buttons_layout.addWidget(self.stop_button)

        orbit_tab = QWidget()
        orbit_layout = QVBoxLayout(orbit_tab)
        self.tabs.addTab(orbit_tab, "Orbit")

        oc_group = QGroupBox("Orbit Monitor / Manual Correction")
        oc_layout = QVBoxLayout()
        oc_group.setLayout(oc_layout)
        orbit_layout.addWidget(oc_group)

        self.orbit_plot = OrbitPlotWidget(self)
        oc_layout.addWidget(self.orbit_plot)

        orbit_button_layout = QHBoxLayout()
        oc_layout.addLayout(orbit_button_layout)
        self.orbit_measure_button = QPushButton("Measure Orbit (All BPMs)")
        self.orbit_measure_button.clicked.connect(self.__measure_orbit_button_clicked)
        self.orbit_measure_button.setStyleSheet("background-color: green; color: white;")
        orbit_button_layout.addWidget(self.orbit_measure_button)

        manual_layout = QHBoxLayout()
        oc_layout.addLayout(manual_layout)
        manual_layout.addWidget(QLabel("Manual corrector:"))
        self.manual_corr_combobox = QComboBox()
        self.manual_corr_combobox.addItems(correctors_list)
        manual_layout.addWidget(self.manual_corr_combobox)

        self.manual_where_button = QPushButton("Where?")
        self.manual_where_button.setMaximumWidth(60)
        self.manual_where_button.clicked.connect(self.__where_button_clicked)
        manual_layout.addWidget(self.manual_where_button)

        self.manual_corr_strength_label = QLabel("I = N/A")
        manual_layout.addWidget(self.manual_corr_strength_label)

        manual_layout.addWidget(QLabel("STEP ΔI (A):"))
        self.manual_step_spinbox = QDoubleSpinBox()
        self.manual_step_spinbox.setRange(-10.0, 10.0)
        self.manual_step_spinbox.setSingleStep(0.01)
        self.manual_step_spinbox.setValue(0.1)
        manual_layout.addWidget(self.manual_step_spinbox)

        self.manual_corr_minus_button = QPushButton("−")
        self.manual_corr_minus_button.setStyleSheet("background-color: blue; color: white;")
        self.manual_corr_minus_button.clicked.connect(
            lambda: self.__apply_manual_step(sign=-1.0)
        )
        manual_layout.addWidget(self.manual_corr_minus_button)

        self.manual_corr_plus_button = QPushButton("+")
        self.manual_corr_plus_button.setStyleSheet("background-color: red; color: white;")
        self.manual_corr_plus_button.clicked.connect(
            lambda: self.__apply_manual_step(sign=+1.0)
        )
        manual_layout.addWidget(self.manual_corr_plus_button)

        self.manual_corr_combobox.currentTextChanged.connect(
            lambda _text: self.__update_manual_corr_strength_label()
        )
        self.__update_manual_corr_strength_label()

        suggest_row = QHBoxLayout()
        oc_layout.addLayout(suggest_row)

        self.apply_suggestion_button = QPushButton("Apply suggested correction")
        self.apply_suggestion_button.clicked.connect(self.__apply_suggested_correction)
        suggest_row.addWidget(self.apply_suggestion_button)

        suggest_row.addWidget(QLabel("Working dir:"))
        self.suggest_workdir_input = QLineEdit()
        self.suggest_workdir_input.setPlaceholderText("if no input, using global working dir:")
        suggest_row.addWidget(self.suggest_workdir_input)

        browse_btn = QPushButton("...")
        browse_btn.clicked.connect(self.__browse_suggest_workdir)
        suggest_row.addWidget(browse_btn)

        # ==== Dispersion Tab ====
        disp_tab = QWidget()
        disp_layout = QVBoxLayout(disp_tab)
        self.tabs.addTab(disp_tab, "Dispersion")

        dc_group = QGroupBox("Dispersion")
        dc_layout = QVBoxLayout()
        dc_group.setLayout(dc_layout)
        disp_layout.addWidget(dc_group)

        self.disp_plot = DispersionPlotWidget(self)
        dc_layout.addWidget(self.disp_plot)

        dc_button_layout = QHBoxLayout()
        dc_layout.addLayout(dc_button_layout)
        self.disp_measure_button = QPushButton("Measure Dispersion")
        self.disp_measure_button.clicked.connect(
            self.__measure_dispersion_button_clicked
        )
        self.disp_measure_button.setStyleSheet("background-color: green; color: white;")
        dc_button_layout.addWidget(self.disp_measure_button)

        self._target_disp_eta_x = None
        self._target_disp_eta_y = None
        try:
            tx, ty = self.interface.get_target_dispersion(self.interface.bpms)
            self._target_disp_eta_x = np.asarray(tx, dtype=float) * 1e3  # to mm
            self._target_disp_eta_y = np.asarray(ty, dtype=float) * 1e3  # to mm
            print("Target dispersion loaded.")

        except Exception as e:
            print(f"Failed to load target dispersion: {e}")
            self._target_disp_eta_x = None
            self._target_disp_eta_y = None

        # QF6X knob (STEP +/-)
        qf_layout = QHBoxLayout()
        dc_layout.addLayout(qf_layout)
        qf_layout.addWidget(QLabel("QF6X STEP ΔI (A):"))
        self.qf6x_step_spinbox = QDoubleSpinBox()
        self.qf6x_step_spinbox.setRange(-20.0, 20.0)
        self.qf6x_step_spinbox.setSingleStep(0.001)
        self.qf6x_step_spinbox.setValue(0.001)
        qf_layout.addWidget(self.qf6x_step_spinbox)
        self.qf6x_minus_button = QPushButton("−")
        self.qf6x_minus_button.setStyleSheet("background-color: blue; color: white;")
        self.qf6x_minus_button.clicked.connect(
            lambda: self.__apply_qf6x_step(sign=-1.0)
        )
        qf_layout.addWidget(self.qf6x_minus_button)
        self.qf6x_plus_button = QPushButton("+")
        self.qf6x_plus_button.setStyleSheet("background-color: red; color: white;")
        self.qf6x_plus_button.clicked.connect(
            lambda: self.__apply_qf6x_step(sign=+1.0)
        )
        qf_layout.addWidget(self.qf6x_plus_button)
        self.qf6x_curr_label = QLabel("QF6X k1: N/A")
        qf_layout.addWidget(self.qf6x_curr_label)

        # SUM knob (QS1X +k, QS2X +k) STEP +/-
        sum_layout = QHBoxLayout()
        dc_layout.addLayout(sum_layout)
        sum_layout.addWidget(
            QLabel("SUM STEP k (QS1X+ k, QS2X +k) [k units]:")
        )
        self.sum_step_spinbox = QDoubleSpinBox()
        self.sum_step_spinbox.setRange(-20.0, 20.0)
        self.sum_step_spinbox.setSingleStep(0.001)
        self.sum_step_spinbox.setValue(0.001)
        sum_layout.addWidget(self.sum_step_spinbox)
        self.sum_minus_button = QPushButton("−")
        self.sum_minus_button.setStyleSheet("background-color: blue; color: white;")
        self.sum_minus_button.clicked.connect(
            lambda: self.__apply_sum_step(sign=-1.0)
        )
        sum_layout.addWidget(self.sum_minus_button)
        self.sum_plus_button = QPushButton("+")
        self.sum_plus_button.setStyleSheet("background-color: red; color: white;")
        self.sum_plus_button.clicked.connect(
            lambda: self.__apply_sum_step(sign=+1.0)
        )
        sum_layout.addWidget(self.sum_plus_button)
        self._sum_knob_total = 0.0
        self.sum_total_label = QLabel("SUM total: +0.0")
        sum_layout.addWidget(self.sum_total_label)

        # ==== IPBSM Tab ====
        ipbsm_tab = QWidget()
        ipbsm_layout = QVBoxLayout(ipbsm_tab)
        self.tabs.addTab(ipbsm_tab, "IPBSM")

        ipbsm_group = QGroupBox("IPBSM (modulation, QF1FF/QD0FF, knobs)")
        ipbsm_group_layout = QVBoxLayout()
        ipbsm_group.setLayout(ipbsm_group_layout)
        ipbsm_layout.addWidget(ipbsm_group)

        mod_group = QGroupBox("IPBSM status")
        mod_layout = QHBoxLayout()
        mod_group.setLayout(mod_layout)
        ipbsm_group_layout.addWidget(mod_group)

        mod_layout.addWidget(QLabel("Modulation M:"))
        self.ipbsm_mod_label = QLabel("N/A")
        mod_layout.addWidget(self.ipbsm_mod_label)

        mod_layout.addWidget(QLabel("Angle mode [deg]:"))
        self.ipbsm_angle_label = QLabel("N/A")
        mod_layout.addWidget(self.ipbsm_angle_label)

        self.ipbsm_refresh_button = QPushButton("Refresh IPBSM state")
        self.ipbsm_refresh_button.setStyleSheet("background-color: green; color: white;")
        self.ipbsm_refresh_button.clicked.connect(self.__refresh_ipbsm_state)
        mod_layout.addWidget(self.ipbsm_refresh_button)

        self.ipbsm_sigma_label = QLabel("σ_y = N/A")
        ipbsm_group_layout.addWidget(self.ipbsm_sigma_label)

        qf1qd0_group = QGroupBox("QF1FF / QD0FF strength & alignment (relative Δ)")
        qf1qd0_group_layout = QVBoxLayout()
        qf1qd0_group.setLayout(qf1qd0_group_layout)
        ipbsm_group_layout.addWidget(qf1qd0_group)

        qf1qd0_group_layout.addWidget(
            QLabel("Δx, Δy: [μm], Δroll: [μrad], Δk1: [1/m²] ")
        )

        qf1_row = QHBoxLayout()
        qf1qd0_group_layout.addLayout(qf1_row)
        qf1_row.addWidget(QLabel("QF1FF Δ:"))

        qf1_row.addWidget(QLabel("dx:"))
        self.qf1_dx_spinbox = QDoubleSpinBox()
        self.qf1_dx_spinbox.setRange(-2000.0, 2000.0)
        self.qf1_dx_spinbox.setSingleStep(10.0)
        qf1_row.addWidget(self.qf1_dx_spinbox)

        qf1_row.addWidget(QLabel("dy:"))
        self.qf1_dy_spinbox = QDoubleSpinBox()
        self.qf1_dy_spinbox.setRange(-2000.0, 2000.0)
        self.qf1_dy_spinbox.setSingleStep(10.0)
        qf1_row.addWidget(self.qf1_dy_spinbox)

        qf1_row.addWidget(QLabel("roll:"))
        self.qf1_droll_spinbox = QDoubleSpinBox()
        self.qf1_droll_spinbox.setRange(-2000.0, 2000.0)
        self.qf1_droll_spinbox.setSingleStep(10.0)
        qf1_row.addWidget(self.qf1_droll_spinbox)

        qf1_row.addWidget(QLabel("Δk1:"))
        self.qf1_dk1_spinbox = QDoubleSpinBox()
        self.qf1_dk1_spinbox.setRange(-10.0, 10.0)
        self.qf1_dk1_spinbox.setSingleStep(0.001)
        self.qf1_dk1_spinbox.setValue(0.0)
        qf1_row.addWidget(self.qf1_dk1_spinbox)

        qd0_row = QHBoxLayout()
        qf1qd0_group_layout.addLayout(qd0_row)
        qd0_row.addWidget(QLabel("QD0FF Δ:"))

        qd0_row.addWidget(QLabel("dx:"))
        self.qd0_dx_spinbox = QDoubleSpinBox()
        self.qd0_dx_spinbox.setRange(-2000.0, 2000.0)
        self.qd0_dx_spinbox.setSingleStep(10.0)
        qd0_row.addWidget(self.qd0_dx_spinbox)

        qd0_row.addWidget(QLabel("dy:"))
        self.qd0_dy_spinbox = QDoubleSpinBox()
        self.qd0_dy_spinbox.setRange(-2000.0, 2000.0)
        self.qd0_dy_spinbox.setSingleStep(10.0)
        qd0_row.addWidget(self.qd0_dy_spinbox)

        qd0_row.addWidget(QLabel("roll:"))
        self.qd0_droll_spinbox = QDoubleSpinBox()
        self.qd0_droll_spinbox.setRange(-2000.0, 2000.0)
        self.qd0_droll_spinbox.setSingleStep(10.0)
        qd0_row.addWidget(self.qd0_droll_spinbox)

        qd0_row.addWidget(QLabel("Δk1:"))
        self.qd0_dk1_spinbox = QDoubleSpinBox()
        self.qd0_dk1_spinbox.setRange(-10.0, 10.0)
        self.qd0_dk1_spinbox.setSingleStep(0.001)
        self.qd0_dk1_spinbox.setValue(0.0)
        qd0_row.addWidget(self.qd0_dk1_spinbox)

        self.qf1_state_label = QLabel("QF1FF: N/A")
        self.qd0_state_label = QLabel("QD0FF: N/A")
        qf1qd0_group_layout.addWidget(self.qf1_state_label)
        qf1qd0_group_layout.addWidget(self.qd0_state_label)

        qf1qd0_btn_row = QHBoxLayout()
        qf1qd0_group_layout.addLayout(qf1qd0_btn_row)
        self.qf1qd0_apply_button = QPushButton("Apply Δ to QF1FF/QD0FF")
        self.qf1qd0_apply_button.clicked.connect(self.__apply_qf1qd0_clicked)
        qf1qd0_btn_row.addWidget(self.qf1qd0_apply_button)

        self.qf1qd0_refresh_button = QPushButton("Refresh current state")
        self.qf1qd0_refresh_button.clicked.connect(self.__update_qf1qd0_state)
        qf1qd0_btn_row.addWidget(self.qf1qd0_refresh_button)

        linear_group = QGroupBox("Linear knobs")
        linear_layout = QVBoxLayout()
        linear_group.setLayout(linear_layout)
        ipbsm_group_layout.addWidget(linear_group)

        try:
            linear_names = list(self.interface.get_linear_knob_names())
        except Exception:
            linear_names = ["A_x", "E_x", "A_y", "E_y", "Coup1", "Coup2"]

        self._linear_knob_values = {name: 0.0 for name in linear_names}
        self._linear_knob_labels = {}

        for name in linear_names:
            row = QHBoxLayout()
            linear_layout.addLayout(row)
            row.addWidget(QLabel(name))

            row.addWidget(QLabel("value:"))
            val_label = QLabel("+0.0")
            self._linear_knob_labels[name] = val_label
            row.addWidget(val_label)

            row.addWidget(QLabel("STEP:"))
            step_box = QDoubleSpinBox()
            step_box.setRange(-10.0, 10.0)
            step_box.setSingleStep(0.1)
            step_box.setValue(0.1)
            row.addWidget(step_box)

            minus_btn = QPushButton("−")
            minus_btn.setStyleSheet("background-color: blue; color: white;")
            minus_btn.clicked.connect(
                lambda _checked=False, knob=name, box=step_box:
                self.__change_linear_knob(knob, -box.value())
            )
            row.addWidget(minus_btn)

            plus_btn = QPushButton("+")
            plus_btn.setStyleSheet("background-color: red; color: white;")
            plus_btn.clicked.connect(
                lambda _checked=False, knob=name, box=step_box:
                self.__change_linear_knob(knob, +box.value())
            )
            row.addWidget(plus_btn)

        nonlinear_group = QGroupBox("Nonlinear knobs (under development)")
        nonlinear_layout = QVBoxLayout()
        nonlinear_group.setLayout(nonlinear_layout)
        ipbsm_group_layout.addWidget(nonlinear_group)

        try:
            nonlinear_names = list(self.interface.get_nonlinear_knob_names())
        except Exception:
            nonlinear_names = ["Y24", "Y46", "Y22", "Y26", "Y66", "Y44"]

        self._nonlinear_knob_values = {name: 0.0 for name in nonlinear_names}
        self._nonlinear_knob_labels = {}

        for name in nonlinear_names:
            row = QHBoxLayout()
            nonlinear_layout.addLayout(row)
            row.addWidget(QLabel(name))

            row.addWidget(QLabel("value:"))
            val_label = QLabel("+0.0")
            self._nonlinear_knob_labels[name] = val_label
            row.addWidget(val_label)

            row.addWidget(QLabel("STEP:"))
            step_box = QDoubleSpinBox()
            step_box.setRange(-10.0, 10.0)
            step_box.setSingleStep(0.1)
            step_box.setValue(0.1)
            row.addWidget(step_box)

            minus_btn = QPushButton("−")
            minus_btn.setStyleSheet("background-color: blue; color: white;")
            minus_btn.clicked.connect(
                lambda _checked=False, knob=name, box=step_box:
                self.__change_nonlinear_knob(knob, -box.value())
            )
            row.addWidget(minus_btn)

            plus_btn = QPushButton("+")
            plus_btn.setStyleSheet("background-color: red; color: white;")
            plus_btn.clicked.connect(
                lambda _checked=False, knob=name, box=step_box:
                self.__change_nonlinear_knob(knob, +box.value())
            )
            row.addWidget(plus_btn)

        try:
            self.__update_qf1qd0_state()
            # self.__refresh_ipbsm_state()
        except Exception as e:
            print(f"Failed to update IPBSM/QF1FF/QD0FF state on startup: {e}")

        # ==== Misalignment Tab ====
        is_rftrack = "RFTrack" in self.interface.__class__.__name__
        if is_rftrack:
            mis_tab = QWidget()
            mis_layout = QVBoxLayout(mis_tab)
            self.tabs.addTab(mis_tab, "Misalignment")

            mis_group = QGroupBox("Random Misalignment (simulation only)")
            mis_group_layout = QVBoxLayout()
            mis_group.setLayout(mis_group_layout)
            mis_layout.addWidget(mis_group)

            mis_row1 = QHBoxLayout()
            mis_group_layout.addLayout(mis_row1)
            mis_row1.addWidget(QLabel("Seed:"))
            self.mis_seed_spinbox = QSpinBox()
            self.mis_seed_spinbox.setRange(0, 999999)
            self.mis_seed_spinbox.setValue(0)
            mis_row1.addWidget(self.mis_seed_spinbox)

            mis_row2 = QHBoxLayout()
            mis_group_layout.addLayout(mis_row2)
            mis_row2.addWidget(QLabel("σ(dx,dy) [μm]:"))
            self.mis_dx_spinbox = QDoubleSpinBox()
            self.mis_dx_spinbox.setRange(0.0, 1000.0)
            self.mis_dx_spinbox.setValue(100.0)
            self.mis_dx_spinbox.setSingleStep(10.0)
            mis_row2.addWidget(self.mis_dx_spinbox)
            self.mis_dy_spinbox = QDoubleSpinBox()
            self.mis_dy_spinbox.setRange(0.0, 1000.0)
            self.mis_dy_spinbox.setValue(100.0)
            self.mis_dy_spinbox.setSingleStep(10.0)
            mis_row2.addWidget(self.mis_dy_spinbox)

            mis_row3 = QHBoxLayout()
            mis_group_layout.addLayout(mis_row3)
            mis_row3.addWidget(QLabel("σ(roll) [μrad]:"))
            self.mis_dtheta_spinbox = QDoubleSpinBox()
            self.mis_dtheta_spinbox.setRange(0.0, 1000.0)
            self.mis_dtheta_spinbox.setValue(200.0)
            self.mis_dtheta_spinbox.setSingleStep(10.0)
            mis_row3.addWidget(self.mis_dtheta_spinbox)

            mis_row4 = QHBoxLayout()
            mis_group_layout.addLayout(mis_row4)
            mis_row4.addWidget(QLabel("σ(dk/k) [-]:"))
            self.mis_dk_spinbox = QDoubleSpinBox()
            self.mis_dk_spinbox.setRange(0.0, 0.1)
            self.mis_dk_spinbox.setDecimals(4)
            self.mis_dk_spinbox.setValue(0.001)
            self.mis_dk_spinbox.setSingleStep(0.0005)
            mis_row4.addWidget(self.mis_dk_spinbox)

            mis_button_layout = QHBoxLayout()
            mis_group_layout.addLayout(mis_button_layout)
            self.mis_apply_button = QPushButton("Apply Random Misalignment")
            self.mis_apply_button.clicked.connect(self.__apply_misalignment_clicked)
            mis_button_layout.addWidget(self.mis_apply_button)

            self.mis_reset_button = QPushButton("RESET lattice")
            self.mis_reset_button.clicked.connect(self.__reset_misalignment_clicked)
            mis_button_layout.addWidget(self.mis_reset_button)

            self.log_widget = QPlainTextEdit()
            self.log_widget.setReadOnly(True)
            main_layout.addWidget(self.log_widget)

            self._stdout_stream = EmittingStream()
            self._stdout_stream.textWritten.connect(self._append_log_text)
            self._old_stdout = sys.stdout
            self._old_stderr = sys.stderr
            sys.stdout = self._stdout_stream
            sys.stderr = self._stdout_stream

    # ---------------------------------------------------------
    # ログ関連
    # ---------------------------------------------------------
    def _append_log_text(self, text: str):
        self.log_widget.moveCursor(QTextCursor.MoveOperation.End)
        self.log_widget.insertPlainText(text)
        self.log_widget.moveCursor(QTextCursor.MoveOperation.End)

    def closeEvent(self, event):
        # stdout/stderr を元に戻す
        sys.stdout = self._old_stdout
        sys.stderr = self._old_stderr
        super().closeEvent(event)

    # ---------------------------------------------------------
    # SysID: save/load lists
    # ---------------------------------------------------------
    def __save_correctors_button_clicked(self):
        base_dir = self.working_directory_input.text()
        os.makedirs(base_dir, exist_ok=True)

        selected_correctors = self.correctors_list.selectedItems()
        default_path = os.path.join(base_dir, "correctors.txt")
        filename, _ = QFileDialog.getSaveFileName(
            None, "Save File", default_path, "Text Files (*.txt)"
        )
        if filename:
            with open(filename, "w") as f:
                for item in selected_correctors:
                    f.write(f"{item.text()}\n")

    def __load_correctors_button_clicked(self):
        base_dir = self.__get_working_directory()
        default_path = os.path.join(base_dir, "correctors.txt")
        filename, _ = QFileDialog.getOpenFileName(
            None, "Open File", default_path, "Text Files (*.txt)"
        )
        if filename:
            with open(filename, "r") as f:
                selected_correctors = [line.strip() for line in f]
        else:
            selected_correctors = self.interface.get_correctors_names()

        self.correctors_list.clearSelection()
        for item in selected_correctors:
            items = self.correctors_list.findItems(item, Qt.MatchFlag.MatchExactly)
            for it in items:
                it.setSelected(True)

    def __clear_correctors_button_clicked(self):
        self.correctors_list.clearSelection()

    def __save_bpms_button_clicked(self):
        base_dir = self.__get_working_directory()
        os.makedirs(base_dir, exist_ok=True)

        selected_bpms = self.bpms_list.selectedItems()

        default_path = os.path.join(base_dir, "bpms.txt")
        filename, _ = QFileDialog.getSaveFileName(
            None, "Save File", default_path, "Text Files (*.txt)"
        )
        if filename:
            with open(filename, "w") as f:
                for item in selected_bpms:
                    f.write(f"{item.text()}\n")

    def __load_bpms_button_clicked(self):
        base_dir = self.__get_working_directory()
        default_path = os.path.join(base_dir, "bpms.txt")
        filename, _ = QFileDialog.getOpenFileName(
            None, "Open File", default_path, "Text Files (*.txt)"
        )

        if filename:
            with open(filename, "r") as f:
                selected_bpms = [line.strip() for line in f]
        else:
            selected_bpms = self.interface.get_bpms_names()

        self.bpms_list.clearSelection()
        for item in selected_bpms:
            items = self.bpms_list.findItems(item, Qt.MatchFlag.MatchExactly)
            for it in items:
                it.setSelected(True)

    def __clear_bpms_button_clicked(self):
        self.bpms_list.clearSelection()

    def __browse_suggest_workdir(self):
        d = QFileDialog.getExistingDirectory(self, "Select working directory for suggestion")
        if d:
            self.suggest_workdir_input.setText(d)

    # ---------------------------------------------------------
    # SysID: start/stop
    # ---------------------------------------------------------
    def __start_button_clicked(self):
        if self.worker_thread and self.worker_thread.isRunning():
            return  # already running

        self.__set_status_in_title("[SysID Running...]")
        self.running.set()

        work_dir = self.working_directory_input.text()
        os.makedirs(work_dir, exist_ok=True)
        os.chdir(work_dir)

        selected_correctors = [item.text() for item in self.correctors_list.selectedItems()]
        if not selected_correctors:
            for i in range(self.correctors_list.count()):
                self.correctors_list.item(i).setSelected(True)
            selected_correctors = self.interface.get_correctors_names()

        selected_bpms = [item.text() for item in self.bpms_list.selectedItems()]
        if not selected_bpms:
            for i in range(self.bpms_list.count()):
                self.bpms_list.item(i).setSelected(True)
            selected_bpms = self.interface.get_bpms_names()

        save_machine_state(self.interface, filename=f"machine_status_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pkl")

        kicks = 0.1 * np.ones(len(selected_correctors), dtype=float)
        max_osc_h = self.horizontal_excursion_spinbox.value()
        max_osc_v = self.vertical_excursion_spinbox.value()
        max_curr_h = self.max_horizontal_current_spinbox.value()
        max_curr_v = self.max_vertical_current_spinbox.value()
        Niter = 3

        self.worker_thread = QThread()
        self.worker = Worker(
            self.interface,
            selected_correctors,
            selected_bpms,
            kicks,
            max_osc_h,
            max_osc_v,
            max_curr_h,
            max_curr_v,
            Niter,
            self.running,
        )
        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.run)
        self.worker.plot_data.connect(self.__update_plot)
        self.worker.suggestion_ready.connect(self.__store_suggestion)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker.finished.connect(lambda: self.__set_status_in_title("[Idle]"))
        self.worker_thread.finished.connect(lambda: setattr(self, "worker_thread", None))

        self.worker_thread.start()

    def __update_plot(self, Op, Diff_x, Err_x, Diff_y, Err_y, corrector):
        self._plot_queue.append((Op, Diff_x, Err_x, Diff_y, Err_y, corrector))

    def __flush_plot_queue(self):
        if not self._plot_queue:
            return
        Op, Diff_x, Err_x, Diff_y, Err_y, corrector = self._plot_queue.popleft()

        self.plot.axes.clear()
        self.plot.axes.errorbar(
            range(Op["nbpms"]),
            Diff_x,
            yerr=Err_x,
            lw=2,
            capsize=5,
            capthick=2,
            label="X",
        )
        self.plot.axes.errorbar(
            range(Op["nbpms"]),
            Diff_y,
            yerr=Err_y,
            lw=2,
            capsize=5,
            capthick=2,
            label="Y",
        )
        self.plot.axes.legend(loc="upper left")
        self.plot.axes.set_xlabel("BPM [#]")
        self.plot.axes.set_ylabel("Orbit [mm]")
        self.plot.axes.set_title(f"Corrector '{corrector}'")
        self.plot.draw()
        self.plot.flush_events()
        self.plot.update()
        self.plot.repaint()

    def __store_suggestion(self, suggestion: dict):
        self._last_suggestion = suggestion

        print("===== Suggested orbit correction (from Response) =====")
        if suggestion.get("correctors_h") is not None:
            print("H-plane:")
            for name, dk in zip(
                    suggestion.get("correctors_h", []),
                    suggestion.get("delta_k_h", []),
            ):
                print(f"  {name:8s} : ΔI = {dk:+.4g} (A)")
        if suggestion.get("correctors_v") is not None:
            print("V-plane:")
            for name, dk in zip(
                    suggestion.get("correctors_v", []),
                    suggestion.get("delta_k_v", []),
            ):
                print(f"  {name:8s} : ΔI = {dk:+.4g} (A)")
        print("======================================================")

    def __stop_button_clicked(self):
        if self.worker_thread and self.worker_thread.isRunning():
            self.__set_status_in_title("[Stopping SysID...]")
            self.running.clear()

    def __update_manual_corr_strength_label(self):
        if not hasattr(self, "manual_corr_strength_label"):
            return
        name = self.manual_corr_combobox.currentText()
        try:
            corr_data = self.interface.get_correctors()
        except Exception as e:
            print(f"Failed to read correctors for strength label: {e}")
            self.manual_corr_strength_label.setText("I = N/A")
            return
        names = corr_data.get("names", [])
        bdes = corr_data.get("bdes", [])
        if isinstance(names, str):
            names = [names]
        try:
            for n, v in zip(names, bdes):
                if n == name:
                    self.manual_corr_strength_label.setText(
                        f"I = {float(v):+.4g} (A)"
                    )
                    return
        except Exception as e:
            print(f"Failed to parse correctors for strength label: {e}")
        self.manual_corr_strength_label.setText("I = N/A")

    def __get_working_directory(self) -> str:
        text = self.working_directory_input.text().strip()

        if not text:
            return self.cwd

        if os.path.isabs(text):
            return text

        return os.path.join(self.cwd, text)

    # ---------------------------------------------------------
    # Orbit monitor / manual OC
    # ---------------------------------------------------------
    def __measure_orbit_button_clicked(self):
        corr_name = self.manual_corr_combobox.currentText()
        try:
            corr_S = float(self.interface.get_element_S(corr_name))
        except Exception as e:
            print(f"get_element_S not available for {corr_name}: {e}")
            corr_S = None

        bpms = self.interface.get_bpms()
        orbit = orbit_from_bpms(bpms)
        s = np.asarray(bpms.get("S", np.arange(len(orbit["x"]), dtype=float)), dtype=float)
        x = orbit["x"]
        y = orbit["y"]
        faulty = orbit["faulty"]

        # --- キャッシュ ---
        self._last_orbit_s = s
        self._last_orbit_x = x
        self._last_orbit_y = y
        self._last_orbit_faulty = faulty

        self.orbit_plot.update_orbit(
            s,
            x,
            y,
            faulty=faulty,
            corr_S=corr_S,
            corr_name=corr_name,
        )

    def __where_button_clicked(self):
        if self._last_orbit_s is None:
            print("No cached orbit. Please measure orbit first.")
            return

        corr_name = self.manual_corr_combobox.currentText()
        try:
            corr_S = float(self.interface.get_element_S(corr_name))
        except Exception as e:
            print(f"get_element_S not available for {corr_name}: {e}")
            corr_S = None

        self.orbit_plot.update_orbit(
            self._last_orbit_s,
            self._last_orbit_x,
            self._last_orbit_y,
            faulty=self._last_orbit_faulty,
            corr_S=corr_S,
            corr_name=corr_name,
        )

    def __get_suggest_working_directory(self) -> str:

        if hasattr(self, "suggest_workdir_input"):
            text = self.suggest_workdir_input.text().strip()
            if text:
                if os.path.isabs(text):
                    return text
                return os.path.join(self.cwd, text)
        return self.__get_working_directory()

    def __load_suggestion_from_disk(self) -> bool:

        dir_name = self.__get_suggest_working_directory()
        files = {
            "h_corr": os.path.join(dir_name, "suggestion_h_correctors.npy"),
            "h_dk": os.path.join(dir_name, "suggestion_h_delta_k.npy"),
            "v_corr": os.path.join(dir_name, "suggestion_v_correctors.npy"),
            "v_dk": os.path.join(dir_name, "suggestion_v_delta_k.npy"),
        }

        try:
            corr_h = np.load(files["h_corr"], allow_pickle=True)
            dk_h = np.load(files["h_dk"])
        except Exception as e:
            print(f"Failed to load H suggestion from {dir_name}: {e}")
            corr_h, dk_h = None, None

        try:
            corr_v = np.load(files["v_corr"], allow_pickle=True)
            dk_v = np.load(files["v_dk"])
        except Exception as e:
            print(f"Failed to load V suggestion from {dir_name}: {e}")
            corr_v, dk_v = None, None

        if corr_h is None and corr_v is None:
            print(f"No suggestion_*.npy found in {dir_name}")
            return False

        suggestion = {}
        if corr_h is not None:
            suggestion["correctors_h"] = list(corr_h)
            suggestion["delta_k_h"] = np.asarray(dk_h, dtype=float)
        if corr_v is not None:
            suggestion["correctors_v"] = list(corr_v)
            suggestion["delta_k_v"] = np.asarray(dk_v, dtype=float)

        self._last_suggestion = suggestion
        print(f"Suggestion loaded from {dir_name}")
        return True

    def __apply_suggested_correction(self):
        if not self.__load_suggestion_from_disk():
            print("No suggestion found in working dir")
            return

        sug = self._last_suggestion

        # H-plane
        try:
            corr_h = sug.get("correctors_h", [])
            dk_h = sug.get("delta_k_h", [])
            for name, dk in zip(corr_h, dk_h):
                print(f"Apply suggested H correction: {name} += {dk:+.4g}")
                self.interface.vary_correctors(name, float(dk))
        except Exception as e:
            print(f"Failed to apply H-plane suggestion: {e}")

        # V-plane
        try:
            corr_v = sug.get("correctors_v", [])
            dk_v = sug.get("delta_k_v", [])
            for name, dk in zip(corr_v, dk_v):
                print(f"Apply suggested V correction: {name} += {dk:+.4g}")
                self.interface.vary_correctors(name, float(dk))
        except Exception as e:
            print(f"Failed to apply V-plane suggestion: {e}")

        print("Suggested correction applied. Measure Orbit to see the result.")

    def __apply_manual_step(self, sign: float):
        step = self.manual_step_spinbox.value() * sign
        corr = self.manual_corr_combobox.currentText()
        print(f"Manual vary_correctors STEP: {corr} += {step}")
        try:
            self.interface.vary_correctors(corr, step)
        except Exception as e:
            print(f"Error in vary_correctors: {e}")
            return

        try:
            corr_data = self.interface.get_correctors()
            names = corr_data["names"]
            bdes = corr_data["bdes"]
            bact = corr_data["bact"]
            if isinstance(names, str):
                names = [names]
            idx = [i for i, name in enumerate(names) if name == corr]
            if idx:
                i0 = idx[0]
                msg = f"{corr}: bdes={bdes[i0]:.4g}, bact={bact[i0]:.4g}"
                print(msg)
                self.info_label.setText(msg)
        except Exception as e:
            print(f"Failed to read corrector status: {e}")

        self.__update_manual_corr_strength_label()

    # ---------------------------------------------------------
    # Dispersion monitor + knobs
    # ---------------------------------------------------------

    def __measure_dispersion_button_clicked(self):
        try:
            print("Measuring dispersion...")
            bpms = self.interface.get_bpms()

            # --- Nominal energy ---
            self.interface.reset_energy()
            O0 = orbit_from_bpms(self.interface.get_bpms(), bpms['names'])
            O0x = O0['x'].reshape(-1, 1)
            O0y = O0['y'].reshape(-1, 1)

            # --- Shifted energy ---
            dP_P = self.interface.change_energy()
            print("dP/P =", dP_P)
            O1 = orbit_from_bpms(self.interface.get_bpms(), bpms['names'])
            O1x = O1['x'].reshape(-1, 1)
            O1y = O1['y'].reshape(-1, 1)

            # --- Restore ---
            self.interface.reset_energy()

            # --- Dispersion ---
            eta_x = (O1x - O0x) / dP_P
            eta_y = (O1y - O0y) / dP_P

            result = {
                "eta_x": eta_x,
                "eta_y": eta_y
            }
        except Exception as e:
            print(f"Dispersion measurement failed: {e}")
            return

        if result is None:
            print("measure_dispersion() returned None.")
            return

        eta_x = np.asarray(result.get("eta_x"))
        eta_y = np.asarray(result.get("eta_y"))

        if eta_x.size == 0 or eta_y.size == 0:
            print("Empty dispersion arrays.")
            return

        try:
            s = np.asarray(bpms["S"], dtype=float)
            if s.size != eta_x.size:
                raise ValueError("get_bpms_S size mismatch.")
        except Exception as e:
            print(f"get_bpms_S not available, fallback to index: {e}")
            s = np.arange(eta_x.size, dtype=float)

            # --- baseline: use target dispersion (Twiss), not first measurement ---
        eta_x0 = None
        eta_y0 = None

        if self._target_disp_eta_x is not None and self._target_disp_eta_y is not None:
            if self._target_disp_eta_x.size == eta_x.size and self._target_disp_eta_y.size == eta_y.size:
                eta_x0 = self._target_disp_eta_x
                eta_y0 = self._target_disp_eta_y
            else:
                print(
                    f"Target dispersion size mismatch: "
                    f"target=({self._target_disp_eta_x.size},{self._target_disp_eta_y.size}) "
                    f"meas=({eta_x.size},{eta_y.size}). Baseline disabled."
                )

        self.disp_plot.update_dispersion(
            s,
            eta_x,
            eta_y,
            eta_x0,
            eta_y0,
        )

    def __update_qf6x_strength_label(self):
        if not hasattr(self, "qf6x_curr_label"):
            return
        try:
            lat = getattr(self.interface, "lattice", None)
            if lat is None:
                self.qf6x_curr_label.setText("QF6X k1: N/A")
                return
            elems = lat["QF6X"]
            if not isinstance(elems, (list, tuple)):
                elems = [elems]
            k_list = []
            for e in elems:
                try:
                    k = e.get_strength()
                    if isinstance(k, (list, tuple, np.ndarray)):
                        k = float(k[0])
                    else:
                        k = float(k)
                    k_list.append(k)
                except Exception:
                    continue
            if k_list:
                k_mean = float(np.mean(k_list))
                self.qf6x_curr_label.setText(f"QF6X k1: {k_mean:+.4g} [1/m²]")
            else:
                self.qf6x_curr_label.setText("QF6X k1: N/A")
        except Exception as e:
            print(f"Failed to update QF6X strength label: {e}")
            self.qf6x_curr_label.setText("QF6X k1: N/A")

    def __apply_qf6x_step(self, sign: float):
        step = self.qf6x_step_spinbox.value() * sign
        # print(f"GUI: apply_qf6x(dk1={step})")
        print(f"GUI: apply_qmag_current(QF6X, dA={step})")
        try:
            # self.interface.apply_qf6x(step)
            self.interface.apply_qmag_current("QF6X", step)
        except Exception as e:
            print(f"apply_qmag_current is not available: {e}")
            return
        # BPM 測定はしない / メッセージのみ
        print("QF6X knob updated.")
        self.__update_qf6x_strength_label()

    def __apply_sum_step(self, sign: float):
        step = self.sum_step_spinbox.value() * sign
        print(f"GUI: apply_sum_knob(k={step})")
        try:
            self.interface.apply_qmag_current("QS1X", step)
            self.interface.apply_qmag_current("QS2X", step)

        except Exception as e:
            print(f"apply_sum_knob is not available: {e}")
            return
        if not hasattr(self, "_sum_knob_total"):
            self._sum_knob_total = 0.0
        self._sum_knob_total += step
        if hasattr(self, "sum_total_label"):
            self.sum_total_label.setText(f"SUM total: {self._sum_knob_total:+.4g}")
        # BPM 測定はしない / メッセージのみ
        print("SUM knob updated.")

    # ---------------------------------------------------------
    # Random Misalignment
    # ---------------------------------------------------------
    def __apply_misalignment_clicked(self):
        seed = self.mis_seed_spinbox.value()
        sigma_dx = self.mis_dx_spinbox.value()
        sigma_dy = self.mis_dy_spinbox.value()
        sigma_dtheta = self.mis_dtheta_spinbox.value()
        sigma_dk = self.mis_dk_spinbox.value()

        print(
            f"GUI: apply_random_misalignment(seed={seed}, "
            f"sigma_dx={sigma_dx}, sigma_dy={sigma_dy}, "
            f"sigma_dtheta={sigma_dtheta}, sigma_dk={sigma_dk})"
        )

        try:
            self.interface.apply_random_misalignment(
                seed, sigma_dx, sigma_dy, sigma_dtheta, sigma_dk
            )
        except Exception as e:
            print(f"apply_random_misalignment is not available: {e}")
            return

        print("Random misalignment applied (interface side).")

    def __reset_misalignment_clicked(self):
        print("GUI: reset_lattice()")
        try:
            self.interface.reset_lattice()
        except Exception as e:
            print(f"reset_lattice is not available: {e}")
            return

        print(
            "Lattice reset to original Twiss."
        )

    # ---------------------------------------------------------
    # IPBSM: modulation / QF1FF/QD0FF / knobs
    # ---------------------------------------------------------
    def __refresh_ipbsm_state(self):
        try:
            state = self.interface.get_ipbsm_state()
        except Exception as e:
            print(f"get_ipbsm_state not available: {e}")
            if hasattr(self, "ipbsm_mod_label"):
                self.ipbsm_mod_label.setText("N/A")
            if hasattr(self, "ipbsm_angle_label"):
                self.ipbsm_angle_label.setText("N/A")
            if hasattr(self, "ipbsm_sigma_label"):
                self.ipbsm_sigma_label.setText("σ_y = N/A")
            return

        if state is None:
            print("get_ipbsm_state returned None.")
            return

        M = state.get("modulation", float("nan"))
        angle = state.get("angle_deg", float("nan"))
        sigma_y = state.get("sigma_y_m", float("nan"))

        if hasattr(self, "ipbsm_mod_label"):
            try:
                self.ipbsm_mod_label.setText(f"{float(M):.3f}")
            except Exception:
                self.ipbsm_mod_label.setText(str(M))
        if hasattr(self, "ipbsm_angle_label"):
            try:
                self.ipbsm_angle_label.setText(f"{float(angle):.1f}")
            except Exception:
                self.ipbsm_angle_label.setText(str(angle))
        if hasattr(self, "ipbsm_sigma_label"):
            try:
                sig_nm = float(sigma_y) * 1e9
                self.ipbsm_sigma_label.setText(f"σ_y ≈ {sig_nm:.1f} nm")
            except Exception:
                self.ipbsm_sigma_label.setText(f"σ_y = {sigma_y}")

    def __update_qf1qd0_state(self):
        try:
            state = self.interface.get_qf1ff_qd0ff_state()
        except Exception as e:
            print(f"get_qf1ff_qd0ff_state not available: {e}")
            self.qf1_state_label.setText("QF1FF: N/A")
            self.qd0_state_label.setText("QD0FF: N/A")
            return

        def _set_label(name, label_widget):
            s = state.get(name)
            if not s:
                label_widget.setText(f"{name}: N/A")
                return
            label_widget.setText(
                f"{name}: x={s['dx_um']:+.1f} μm, "
                f"y={s['dy_um']:+.1f} μm, "
                f"roll={s['roll_urad']:+.1f} μrad, "
                f"k1={s['k1']:+.4g} [1/m²]"
            )

        _set_label("QF1FF", self.qf1_state_label)
        _set_label("QD0FF", self.qd0_state_label)

    def __apply_qf1qd0_clicked(self):
        qf1_dx = self.qf1_dx_spinbox.value()
        qf1_dy = self.qf1_dy_spinbox.value()
        qf1_droll = self.qf1_droll_spinbox.value()
        qf1_dk1 = self.qf1_dk1_spinbox.value()

        qd0_dx = self.qd0_dx_spinbox.value()
        qd0_dy = self.qd0_dy_spinbox.value()
        qd0_droll = self.qd0_droll_spinbox.value()
        qd0_dk1 = self.qd0_dk1_spinbox.value()

        print(
            f"GUI: apply_qf1ff_qd0ff("
            f"QF1FF Δx={qf1_dx}, Δy={qf1_dy}, Δroll={qf1_droll}, Δk1={qf1_dk1}; "
            f"QD0FF Δx={qd0_dx}, Δy={qd0_dy}, Δroll={qd0_droll}, Δk1={qd0_dk1})"
        )
        try:
            self.interface.apply_qf1ff_qd0ff(
                qf1_dx, qf1_dy, qf1_droll, qf1_dk1,
                qd0_dx, qd0_dy, qd0_droll, qd0_dk1,
            )
        except Exception as e:
            print(f"apply_qf1ff_qd0ff is not available: {e}")
            return

        # 入力は相対値なので、適用後は 0 に戻す
        self.qf1_dx_spinbox.setValue(0.0)
        self.qf1_dy_spinbox.setValue(0.0)
        self.qf1_droll_spinbox.setValue(0.0)
        self.qf1_dk1_spinbox.setValue(0.0)
        self.qd0_dx_spinbox.setValue(0.0)
        self.qd0_dy_spinbox.setValue(0.0)
        self.qd0_droll_spinbox.setValue(0.0)
        self.qd0_dk1_spinbox.setValue(0.0)

        self.__update_qf1qd0_state()

    def __change_linear_knob(self, knob_name: str, delta: float):
        old = self._linear_knob_values.get(knob_name, 0.0)
        new = old + float(delta)
        self._linear_knob_values[knob_name] = new

        label = self._linear_knob_labels.get(knob_name)
        if label is not None:
            label.setText(f"{new:+.3f}")

        print(f"Set linear knob {knob_name} = {new:+.3f}")
        try:
            self.interface.set_linear_knob(knob_name, new)
        except Exception as e:
            print(f"set_linear_knob not available: {e}")

    def __change_nonlinear_knob(self, knob_name: str, delta: float):
        old = self._nonlinear_knob_values.get(knob_name, 0.0)
        new = old + float(delta)
        self._nonlinear_knob_values[knob_name] = new

        label = self._nonlinear_knob_labels.get(knob_name)
        if label is not None:
            label.setText(f"{new:+.3f}")

        print(f"Set nonlinear knob {knob_name} = {new:+.3f}")
        try:
            self.interface.set_nonlinear_knob(knob_name, new)
        except Exception as e:
            print(f"set_nonlinear_knob not available: {e}")

    # ---------------------------------------------------------
    # MAIN
    # ---------------------------------------------------------


if __name__ == "__main__":
    app = QApplication(sys.argv)

    from Backend.SelectInterface import InterfaceSelectionDialog

    dialog = InterfaceSelectionDialog("ATF2")
    if dialog.exec():
        print(f"Selected interface: {dialog.selected_interface_name}")
        I = dialog.selected_interface
    else:
        print("Selection cancelled.")
        sys.exit(1)

    project_name = dialog.selected_interface_name
    time_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    dir_name = f"Data/{project_name}{time_str}"

    window = MainWindow(I, dir_name)
    window.show()
    sys.exit(app.exec())
