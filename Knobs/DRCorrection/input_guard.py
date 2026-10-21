"""Keep wheel scrolling from changing settings in protected tuning windows."""
import atexit
import weakref
from PyQt6 import sip
from PyQt6.QtCore import QObject,QEvent,QPointF
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (QApplication,QWidget,QAbstractSpinBox,QComboBox,
    QLineEdit,QAbstractScrollArea)


_guards=weakref.WeakSet()


@atexit.register
def _detach_filters():
    # Qt may still deliver teardown events after Python starts clearing module
    # globals. Detach Python application filters before that phase, including
    # offscreen/test programs that never enter QApplication.exec().
    app=QApplication.instance()
    if app is not None and not sip.isdeleted(app):
        for guard in list(_guards):
            if not sip.isdeleted(guard):app.removeEventFilter(guard)


class SettingsWheelGuard(QObject):
    def eventFilter(self,widget,event):
        if event.type()!=QEvent.Type.Wheel or not isinstance(widget,QWidget):return False
        root=self.parent()
        owner=widget
        while owner is not None and owner is not root:owner=owner.parentWidget()
        if owner is None:return False
        control=widget.parentWidget() if isinstance(widget,QLineEdit) else widget
        if not isinstance(control,(QAbstractSpinBox,QComboBox)):return False
        # Also protect focused controls. Editing remains available via typing,
        # arrow keys/buttons, and explicitly opening a combo's popup menu.
        ancestor=control.parentWidget()
        while ancestor is not None and not isinstance(ancestor,QAbstractScrollArea):
            ancestor=ancestor.parentWidget()
        if ancestor is not None:
            if not event.pixelDelta().isNull():
                # Some Qt scroll-area styles only consume angleDelta. Honor
                # high-resolution trackpad movement even without wheel ticks.
                for bar,delta in ((ancestor.horizontalScrollBar(),event.pixelDelta().x()),
                                  (ancestor.verticalScrollBar(),event.pixelDelta().y())):
                    if delta:bar.setValue(bar.value()-delta)
            else:
                viewport=ancestor.viewport()
                local=QPointF(viewport.mapFromGlobal(event.globalPosition().toPoint()))
                forwarded=QWheelEvent(local,event.globalPosition(),event.pixelDelta(),
                    event.angleDelta(),event.buttons(),event.modifiers(),event.phase(),
                    event.inverted())
                QApplication.sendEvent(viewport,forwarded)
        event.accept()
        return True


def protect_settings_from_wheel(window):
    # Application-level filtering also covers controls/dialogs created later,
    # but the ancestry check confines this policy to the given tuning window.
    if not hasattr(window,'_settings_wheel_guard'):
        window._settings_wheel_guard=SettingsWheelGuard(window)
        _guards.add(window._settings_wheel_guard)
        QApplication.instance().installEventFilter(window._settings_wheel_guard)
