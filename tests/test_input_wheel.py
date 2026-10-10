import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QComboBox, QDoubleSpinBox, QScrollArea, QSpinBox, QVBoxLayout, QWidget

from rigol_control.app import create_application
from rigol_control.ui.device_windows import UnitValueWidget


def wheel(widget, delta=120):
    point = widget.rect().center()
    event = QWheelEvent(QPointF(point), QPointF(widget.mapToGlobal(point)), QPoint(), QPoint(0, delta),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(widget, event)


class InputWheelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(["wheel-test"])

    def test_wheel_keeps_integer_decimal_and_unit_values_with_or_without_focus(self):
        window = QWidget()
        layout = QVBoxLayout(window)
        integer, decimal = QSpinBox(), QDoubleSpinBox()
        integer.setValue(10)
        decimal.setValue(12.5)
        unit = UnitValueWidget(1e6, 1, 1e9, "frequency", "MHz")
        for control in (integer, decimal, unit):
            layout.addWidget(control)
        window.show()
        self.app.processEvents()
        spies = [QSignalSpy(integer.valueChanged), QSignalSpy(decimal.valueChanged),
                 QSignalSpy(unit.spin.valueChanged), QSignalSpy(unit.unit_combo.currentIndexChanged)]
        try:
            for focused in (False, True):
                for control in (integer, decimal, unit.spin, unit.unit_combo):
                    control.setFocus() if focused else control.clearFocus()
                    wheel(control)
                    wheel(control, -120)
                for control in (integer.lineEdit(), decimal.lineEdit(), unit.spin.lineEdit()):
                    wheel(control)
            self.assertEqual(integer.value(), 10)
            self.assertEqual(decimal.value(), 12.5)
            self.assertEqual(unit.value(), 1e6)
            self.assertEqual(unit.current_unit(), "MHz")
            self.assertTrue(all(len(spy) == 0 for spy in spies))
            integer.setFocus()
            QTest.keyClick(integer, Qt.Key.Key_Up)
            self.assertEqual(integer.value(), 11)
            decimal.lineEdit().setText("20.5")
            QTest.keyClick(decimal, Qt.Key.Key_Return)
            self.assertEqual(decimal.value(), 20.5)
        finally:
            window.close()

    def test_wheel_over_input_scrolls_page_instead_of_changing_value(self):
        area = QScrollArea()
        content = QWidget()
        layout = QVBoxLayout(content)
        control = QDoubleSpinBox()
        control.setValue(12.5)
        layout.addWidget(control)
        layout.addStretch()
        content.setMinimumHeight(1200)
        area.setWidget(content)
        area.setWidgetResizable(True)
        area.resize(320, 220)
        area.show()
        self.app.processEvents()
        try:
            control.setFocus()
            wheel(control, -120)
            self.assertEqual(control.value(), 12.5)
            self.assertGreater(area.verticalScrollBar().value(), 0)
        finally:
            area.close()

    def test_open_dropdown_list_still_scrolls(self):
        combo = QComboBox()
        combo.addItems([str(i) for i in range(100)])
        combo.view().setMaximumHeight(120)
        combo.show()
        combo.showPopup()
        self.app.processEvents()
        try:
            view = combo.view()
            self.assertTrue(view.isVisible())
            self.assertGreater(view.verticalScrollBar().maximum(), 0)
            wheel(view.viewport(), -120)
            self.assertGreater(view.verticalScrollBar().value(), 0)
        finally:
            combo.hidePopup()
            combo.close()
