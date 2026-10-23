"""Shared, resizable desktop layout for the optimization GUIs."""
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QSplitter,
    QFrame, QSizePolicy, QPushButton, QLabel,
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
