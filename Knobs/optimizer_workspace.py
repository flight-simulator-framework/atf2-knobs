"""Shared, resizable desktop layout for the optimization GUIs."""
import json
import math

from PyQt6.QtCore import Qt, QSize, QRectF, QPointF
from PyQt6.QtGui import QPainter, QColor, QPen
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QSplitter,
    QFrame, QSizePolicy, QPushButton, QLabel, QProgressBar, QGridLayout,
)


WORKSPACE_STYLE = """
QWidget { color: #172b4d; font-size: 14px; }
QScrollArea { border: none; background: transparent; }
QGroupBox { font-size: 14px; font-weight: 600; background: #ffffff;
            border: 1px solid #dce3eb; border-radius: 8px;
            margin-top: 10px; padding: 10px 8px 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
QLabel { background: transparent; }
QCheckBox { spacing: 6px; font-size: 14px; }
QCheckBox[knobCheck="true"] { font-size: 15px; font-weight: 600; }
QCheckBox[knobCheck="true"]:checked { color: #125a96; }
QCheckBox::indicator { width: 17px; height: 17px; }
QPushButton { padding: 6px 10px; min-height: 24px; border-radius: 5px;
              border: 1px solid #cbd5e1; background: #f5f8fc; }
QPushButton:hover { border-color: #418bca; }
QPushButton:disabled { color: #94a3b8; background: #edf1f5; }
QLineEdit, QComboBox { min-height: 26px; padding: 2px 5px; }
QTextEdit, QPlainTextEdit { font-size: 12px; border: 1px solid #dce3eb; }
QSplitter::handle { background: #e2e8f0; border-radius: 2px; }
QSplitter::handle:hover { background: #93b9df; }
"""

TAB_STYLE = "QTabBar::tab { font-size: 14px; font-weight: 600; padding: 6px 16px; min-width: 72px; }"


def run_button_style(role):
    """Common LinacOpt run-control buttons for Main and Active Feedback."""
    color = {"start": "#1f7a1f", "pause": "#a32020", "stop": "#585f66"}[role]
    return ("QPushButton { font-size: 15px; font-weight: 700; padding: 6px 10px; min-height: 30px; "
            f"background: {color}; color: white; }} "
            "QPushButton:disabled { background: #edf1f5; color: #94a3b8; border-color: #dce3eb; }")


def status_badge_style(state):
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
    return ("QLabel#statusBadge {"
            f"font-size: 17px; font-weight: 600; color: {fg}; "
            f"background: {bg}; border: 2px solid {bd}; "
            "border-radius: 6px; padding: 5px 8px; }")


class OptimizerWorkspace(QWidget):
    """Scrollable controls at left; plots and optional logs at right."""
    def __init__(self, parent=None, *, controls_width=460):
        super().__init__(parent)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(6)
        outer.addWidget(self.splitter)

        self.controls_scroll = QScrollArea()
        self.controls_scroll.setWidgetResizable(True)
        self.controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.controls_scroll.setMinimumWidth(300)
        self.controls = QWidget()
        self.controls_layout = QVBoxLayout(self.controls)
        self.controls_layout.setContentsMargins(0, 0, 6, 0)
        self.controls_layout.setSpacing(8)
        self.controls_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.controls_scroll.setWidget(self.controls)
        self.splitter.addWidget(self.controls_scroll)

        self.monitor = QWidget()
        self.monitor.setMinimumWidth(380)
        self.monitor_layout = QVBoxLayout(self.monitor)
        self.monitor_layout.setContentsMargins(4, 0, 0, 0)
        self.monitor_layout.setSpacing(6)
        self.splitter.addWidget(self.monitor)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([controls_width, 1360 - controls_width])

    def add_log(self, log_widget):
        """Keep logs accessible without taking height from live plots by default."""
        self.log_toggle = QPushButton("Show log")
        self.log_toggle.setCheckable(True)
        self.log_toggle.setMaximumWidth(120)
        self.monitor_layout.addWidget(self.log_toggle)
        log_widget.setMinimumHeight(80)
        log_widget.setMaximumHeight(160)
        self.monitor_layout.addWidget(log_widget)
        log_widget.hide()
        def toggle(visible):
            log_widget.setVisible(visible)
            self.log_toggle.setText("Hide log" if visible else "Show log")
        self.log_toggle.toggled.connect(toggle)


class PlotImage(QWidget):
    """A gallery image that fits the panel width instead of widening the window."""
    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        self.pixmap = pixmap
        self.axis_metadata = {}
        self.measurement_value = None
        self.measurement_visible = False
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def heightForWidth(self, width):
        return max(80, round(width * self.pixmap.height() / max(1, self.pixmap.width())))

    def sizeHint(self):
        return QSize(600, self.heightForWidth(600))

    def minimumSizeHint(self):
        return QSize(100, 80)

    def measurement_line(self):
        """Widget-space line coordinates, including image letterboxing."""
        if self.measurement_value is None or not self.axis_metadata or self.pixmap.isNull():
            return None
        try:
            lo, hi = self.axis_metadata["xlim"]
            left, top, width, height = self.axis_metadata["rect"]
            fraction = (float(self.measurement_value) - lo) / (hi - lo)
            if not math.isfinite(fraction) or not 0 <= fraction <= 1:
                return None
            scaled = self.pixmap.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            x0, y0 = (self.width() - scaled.width()) / 2, (self.height() - scaled.height()) / 2
            rect = QRectF(x0 + left * scaled.width(), y0 + top * scaled.height(),
                          width * scaled.width(), height * scaled.height())
            x = rect.left() + fraction * rect.width()
            return rect, QPointF(x, rect.top()), QPointF(x, rect.bottom())
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return None

    def paintEvent(self, event):
        if self.pixmap.isNull():
            return
        scaled = self.pixmap.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap((self.width() - scaled.width()) // 2, (self.height() - scaled.height()) // 2,
                           scaled.width(), scaled.height(), self.pixmap)
        line = self.measurement_line()
        if line is not None:
            rect, start, end = line
            painter.setClipRect(rect)
            color = QColor("#ea580c")
            if self.measurement_visible:
                painter.setPen(QPen(color, 2.5, Qt.PenStyle.DotLine))
                painter.drawLine(start, end)
            label = f"Measuring: {self.measurement_value:+.5g}"
            font = painter.font()
            font.setPixelSize(12)
            font.setBold(True)
            painter.setFont(font)
            label_rect = QRectF(rect.left() + 3, rect.top() + 3,
                                min(rect.width() - 6, painter.fontMetrics().horizontalAdvance(label) + 8), 20)
            painter.fillRect(label_rect, QColor(255, 255, 255, 225))
            painter.setPen(color)
            painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, label)


class WrappingLabel(QLabel):
    """Keep live values fully visible when their text or sidebar width changes."""
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)

    def _fit_height(self):
        self.setMinimumHeight(max(0, self.heightForWidth(max(1, self.width()))))

    def setText(self, text):
        super().setText(text)
        self._fit_height()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_height()


class OverallProgressBar(QProgressBar):
    """Run-wide budget progress displayed below the status badge."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRange(0, 1000)
        self.setMinimumHeight(26)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("QProgressBar { border: 1px solid #bfdbfe; border-radius: 5px; "
                           "background: #eff6ff; color: #172b4d; font-size: 14px; font-weight: 600; }"
                           "QProgressBar::chunk { background: #93c5fd; border-radius: 4px; }")
        self.setToolTip("Selected groups' total evaluation budget. Early-completed groups count as fully complete.")
        self.update_progress({})

    def update_progress(self, progress):
        percent = max(0.0, min(100.0, float(progress.get("percent", 0))))
        self.setValue(round(percent * 10))
        self.setFormat(f"Total progress: {percent:.1f}%")


class FitPlotGallery(QWidget):
    """Choose a grid that maximizes image size inside the available viewport."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(4)
        self.panels = []
        self.measurement_values = {}
        self.measurement_visible = False
        self.columns = 0
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

    def clear(self):
        while self.grid.count():
            widget = self.grid.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.panels = []
        self.columns = 0

    def set_plots(self, paths):
        self.clear()
        from PyQt6.QtGui import QImage, QPixmap
        for path in paths:
            source = QImage(str(path))
            panel = PlotImage(QPixmap.fromImage(source), self)
            try:
                panel.axis_metadata = json.loads(source.text("optimizer_axis") or "{}")
                if not isinstance(panel.axis_metadata, dict):
                    panel.axis_metadata = {}
            except (ValueError, TypeError):
                panel.axis_metadata = {}
            panel.setToolTip(path.name)
            # The grid owns both dimensions; image hints must never cause scrolling.
            panel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
            self.panels.append(panel)
        self.set_measurement_position(self.measurement_values, visible=self.measurement_visible)
        self.reflow()

    def set_measurement_position(self, values, *, visible=True):
        self.measurement_values = dict(values)
        self.measurement_visible = visible
        for panel in self.panels:
            value = values.get(panel.axis_metadata.get("axis"))
            try:
                value = float(value)
                if not math.isfinite(value):
                    value = None
            except (ValueError, TypeError):
                value = None
            panel.measurement_value = value
            panel.measurement_visible = visible
            panel.update()

    def reflow(self):
        count = len(self.panels)
        if not count:
            return
        def score(cols):
            rows = (count + cols - 1) // cols
            width = max(1, (self.width() - (cols - 1) * 4) / cols)
            height = max(1, (self.height() - (rows - 1) * 4) / rows)
            areas = []
            for panel in self.panels:
                ratio = panel.pixmap.width() / max(1, panel.pixmap.height())
                ratio = max(0.1, ratio)
                areas.append(min(width, height * ratio) * min(height, width / ratio))
            return sum(areas)
        cols = max(range(1, count + 1), key=score)
        if cols == self.columns:
            return
        for index in range(max(self.grid.rowCount(), self.grid.columnCount())):
            self.grid.setRowStretch(index, 0)
            self.grid.setColumnStretch(index, 0)
        while self.grid.count():
            self.grid.takeAt(0)
        self.columns = cols
        for index, panel in enumerate(self.panels):
            row, col = divmod(index, cols)
            self.grid.addWidget(panel, row, col)
            self.grid.setRowStretch(row, 1)
            self.grid.setColumnStretch(col, 1)
            panel.show()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()
