from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, QRunnable, Qt, pyqtSignal
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..models import DeviceKind, DeviceRecord
from ..services import DeviceProbeService, ProbeResult, ProbeState


class DeviceCard(QFrame):
    opened = pyqtSignal(str)
    edited = pyqtSignal(str)
    deleted = pyqtSignal(str)
    connection_requested = pyqtSignal(str)
    disconnection_requested = pyqtSignal(str)

    def __init__(self, record: DeviceRecord, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.record = record
        self.setObjectName("DeviceCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(310, 225)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 15)
        layout.setSpacing(9)

        heading = QHBoxLayout()
        badge = QLabel(record.kind.label)
        badge.setStyleSheet(
            f"color:{record.kind.accent}; background:{record.kind.accent}22;"
            f"border:1px solid {record.kind.accent}55; border-radius:5px; padding:3px 7px;"
        )
        badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.status = QLabel("● 未检测")
        self.status.setObjectName("StatusPending")
        heading.addWidget(badge)
        heading.addStretch()
        heading.addWidget(self.status)
        layout.addLayout(heading)

        title = QLabel(record.name)
        title.setObjectName("CardTitle")
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(title)

        resource = QLabel(record.resource)
        resource.setObjectName("Muted")
        resource.setToolTip(record.resource)
        resource.setWordWrap(True)
        layout.addWidget(resource)

        detail = QLabel(record.connection or self._default_connection(record.kind))
        detail.setObjectName("Muted")
        layout.addWidget(detail)
        layout.addStretch()

        connection_actions = QHBoxLayout()
        self.connect_button = QPushButton("连接")
        self.connect_button.setObjectName("PrimaryButton")
        self.disconnect_button = QPushButton("断开")
        self.disconnect_button.setEnabled(False)
        self.connect_button.clicked.connect(lambda: self.connection_requested.emit(record.id))
        self.disconnect_button.clicked.connect(lambda: self.disconnection_requested.emit(record.id))
        connection_actions.addWidget(self.connect_button)
        connection_actions.addWidget(self.disconnect_button)
        connection_actions.addStretch()
        layout.addLayout(connection_actions)

        actions = QHBoxLayout()
        open_button = QPushButton("打开设置")
        edit_button = QPushButton("编辑")
        delete_button = QPushButton("删除")
        delete_button.setObjectName("DangerButton")
        open_button.clicked.connect(lambda: self.opened.emit(record.id))
        edit_button.clicked.connect(lambda: self.edited.emit(record.id))
        delete_button.clicked.connect(lambda: self.deleted.emit(record.id))
        actions.addWidget(open_button)
        actions.addStretch()
        actions.addWidget(edit_button)
        actions.addWidget(delete_button)
        layout.addLayout(actions)

    @staticmethod
    def _default_connection(kind: DeviceKind) -> str:
        if kind is DeviceKind.NEWPORT_SMC100:
            return "Newport RS-232"
        if kind is DeviceKind.NI_DAQ:
            return "NI-DAQmx 本机设备"
        if kind is DeviceKind.VAUNIX_LMS:
            return "Vaunix USB DLL"
        return "VISA / SCPI"

    def set_probe_result(self, result: ProbeResult) -> None:
        if result.state is ProbeState.ONLINE:
            self.status.setObjectName("StatusOnline")
            self.status.setText("● 可用")
        elif result.state is ProbeState.DRIVER_MISSING:
            self.status.setObjectName("StatusPending")
            self.status.setText("● 缺少驱动")
        elif result.state is ProbeState.UNCHECKED:
            self.status.setObjectName("StatusPending")
            self.status.setText("● 待连接验证")
        else:
            self.status.setObjectName("StatusOffline")
            self.status.setText("● 离线")
        self.status.setToolTip("\n".join(filter(None, (result.message, result.identity))))
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def set_connection_state(self, connected: bool, *, busy: bool = False) -> None:
        self.connect_button.setEnabled(not connected and not busy)
        self.disconnect_button.setEnabled(connected and not busy)
        self.connect_button.setText("处理中…" if busy else ("已连接" if connected else "连接"))
        if busy:
            self.status.setObjectName("StatusPending")
            self.status.setText("● 处理中")
        elif connected:
            self.status.setObjectName("StatusOnline")
            self.status.setText("● 已连接")
        else:
            self.status.setObjectName("StatusPending")
            self.status.setText("● 未连接")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        child = self.childAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and not isinstance(child, QPushButton):
            self.opened.emit(self.record.id)
        super().mouseReleaseEvent(event)


class AddDeviceCard(QFrame):
    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DeviceCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(310, 225)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plus = QLabel("＋")
        plus.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plus.setStyleSheet("font-size:42px; color:#53d5ca;")
        title = QLabel("添加设备")
        title.setObjectName("CardTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        note = QLabel("保存 DG4000、Vaunix、NI 采集卡或通用 SCPI 设备")
        note.setObjectName("Muted")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(plus)
        layout.addWidget(title)
        layout.addWidget(note)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class ProbeSignals(QObject):
    completed = pyqtSignal(str, object)


class ProbeTask(QRunnable):
    def __init__(self, record: DeviceRecord, service: DeviceProbeService) -> None:
        super().__init__()
        self.record = record
        self.service = service
        self.signals = ProbeSignals()

    def run(self) -> None:
        result = self.service.probe(self.record)
        self.signals.completed.emit(self.record.id, result)


class CallSignals(QObject):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    finished = pyqtSignal()


class CallTask(QRunnable):
    """Run one potentially blocking instrument operation away from the GUI thread."""

    def __init__(self, operation: Callable[[], Any]) -> None:
        super().__init__()
        self.operation = operation
        self.signals = CallSignals()

    def run(self) -> None:
        try:
            result = self.operation()
        except Exception as exc:
            self.signals.failed.emit(str(exc))
        else:
            self.signals.succeeded.emit(result)
        finally:
            self.signals.finished.emit()


def metric_card(caption: str, value: str, accent: str = "#55d9cc") -> tuple[QFrame, QLabel]:
    card = QFrame()
    card.setObjectName("MetricCard")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(14, 11, 14, 11)
    label = QLabel(caption)
    label.setObjectName("Muted")
    number = QLabel(value)
    number.setStyleSheet(f"font-size:22px; font-weight:700; color:{accent};")
    layout.addWidget(label)
    layout.addWidget(number)
    return card, number
