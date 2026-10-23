"""Shared, resizable desktop layout for the optimization GUIs."""
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QPainter
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
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def heightForWidth(self, width):
        return max(80, round(width * self.pixmap.height() / max(1, self.pixmap.width())))

    def sizeHint(self):
        return QSize(600, self.heightForWidth(600))

    def minimumSizeHint(self):
        return QSize(100, 80)

    def paintEvent(self, event):
        if self.pixmap.isNull():
            return
        scaled = self.pixmap.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap((self.width() - scaled.width()) // 2, (self.height() - scaled.height()) // 2,
                           scaled.width(), scaled.height(), self.pixmap)


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
        from PyQt6.QtGui import QPixmap
        for path in paths:
            pix = QPixmap(str(path))
            panel = PlotImage(pix, self)
            panel.setToolTip(path.name)
            # The grid owns both dimensions; image hints must never cause scrolling.
            panel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
            self.panels.append(panel)
        self.reflow()

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
