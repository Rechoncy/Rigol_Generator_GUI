from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, QPointF
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QApplication, QAbstractScrollArea, QAbstractSpinBox, QComboBox, QWidget


class InputWheelGuard(QObject):
    """Let the page scroll without changing a parameter under the pointer."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.Wheel or not isinstance(watched, QWidget):
            return False
        control = watched
        while control is not None:
            if isinstance(control, QAbstractSpinBox):
                self._scroll_parent(control, event)
                return True
            if isinstance(control, QComboBox):
                if control.view().isVisible():
                    return False
                self._scroll_parent(control, event)
                return True
            control = control.parentWidget()
        return False

    @staticmethod
    def _scroll_parent(control: QWidget, event: QWheelEvent) -> None:
        parent = control.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                viewport = parent.viewport()
                forwarded = QWheelEvent(
                    QPointF(viewport.mapFromGlobal(event.globalPosition().toPoint())),
                    event.globalPosition(), event.pixelDelta(), event.angleDelta(),
                    event.buttons(), event.modifiers(), event.phase(), event.inverted(),
                    device=event.pointingDevice())
                QApplication.sendEvent(viewport, forwarded)
                event.accept()
                return
            parent = parent.parentWidget()
        event.ignore()
