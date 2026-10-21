"""Exercise wheel delivery, scrolling, and intentional editing without EPICS."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest
from PyQt6.QtCore import Qt,QPoint,QPointF
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtTest import QTest,QSignalSpy
from PyQt6.QtWidgets import (QApplication,QWidget,QVBoxLayout,QScrollArea,
    QSpinBox,QDoubleSpinBox,QComboBox,QDialog)
from Knobs.DRCorrection.input_guard import protect_settings_from_wheel


class WheelGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.root=QWidget();layout=QVBoxLayout(self.root)
        self.area=QScrollArea();self.area.setWidgetResizable(True);layout.addWidget(self.area)
        content=QWidget();form=QVBoxLayout(content)
        self.integer=QSpinBox();self.integer.setValue(20)
        self.decimal=QDoubleSpinBox();self.decimal.setValue(2.1)
        self.combo=QComboBox();self.combo.addItems(['X','Y','Z'])
        for control in (self.integer,self.decimal,self.combo):form.addWidget(control)
        form.addStretch();content.setMinimumHeight(1200);self.area.setWidget(content)
        protect_settings_from_wheel(self.root)
        self.root.resize(400,300);self.root.show();self.app.processEvents()
    def tearDown(self):
        self.root.close();self.root.deleteLater();self.app.processEvents()
    def wheel(self,widget,pixel=False):
        local=QPointF(widget.rect().center());global_pos=QPointF(widget.mapToGlobal(local.toPoint()))
        event=QWheelEvent(local,global_pos,QPoint(0,-40) if pixel else QPoint(),
            QPoint() if pixel else QPoint(0,-120),Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,Qt.ScrollPhase.ScrollUpdate if pixel else Qt.ScrollPhase.NoScrollPhase,False)
        QApplication.sendEvent(widget,event);self.app.processEvents()
    def test_wheel_preserves_focused_and_unfocused_values_and_scrolls(self):
        for control in (self.integer,self.decimal,self.combo):
            getter=control.currentIndex if isinstance(control,QComboBox) else control.value
            signal=control.currentIndexChanged if isinstance(control,QComboBox) else control.valueChanged
            spy=QSignalSpy(signal);before=getter()
            for focus in (False,True):
                with self.subTest(control=type(control).__name__,focus=focus):
                    control.setFocus() if focus else control.clearFocus()
                    self.area.verticalScrollBar().setValue(0)
                    self.wheel(control)
                    self.assertEqual(getter(),before);self.assertEqual(len(spy),0)
                    self.assertGreater(self.area.verticalScrollBar().value(),0)
    def test_line_edit_trackpad_and_keyboard(self):
        self.decimal.setFocus();before=self.decimal.value()
        self.wheel(self.decimal.lineEdit(),pixel=True)
        self.assertEqual(self.decimal.value(),before)
        self.assertGreater(self.area.verticalScrollBar().value(),0)
        self.integer.setFocus();self.integer.selectAll();QTest.keyClicks(self.integer,'37')
        QTest.keyClick(self.integer,Qt.Key.Key_Return);self.assertEqual(self.integer.value(),37)
        QTest.keyClick(self.integer,Qt.Key.Key_Up);self.assertEqual(self.integer.value(),38)
        self.integer.stepDown();self.assertEqual(self.integer.value(),37)
    def test_later_dialog_is_protected_and_unrelated_window_is_not(self):
        dialog=QDialog(self.root);layout=QVBoxLayout(dialog);spin=QSpinBox();spin.setValue(20);layout.addWidget(spin)
        dialog.show();self.app.processEvents();self.wheel(spin);self.assertEqual(spin.value(),20)
        external=QSpinBox();external.setValue(20);external.show();external.setFocus()
        self.wheel(external);self.assertNotEqual(external.value(),20)
        external.close();dialog.close()


if __name__=='__main__':unittest.main()
