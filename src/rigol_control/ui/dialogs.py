from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QThreadPool, QTimer, Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..models import DeviceKind, DeviceRecord
from ..services import DeviceDiscoveryService, DiscoveredResource
from .widgets import CallTask


class DeviceDialog(QDialog):
    def __init__(
        self,
        record: DeviceRecord | None = None,
        parent: QWidget | None = None,
        discovery_service: DeviceDiscoveryService | None = None,
    ) -> None:
        super().__init__(parent)
        self.record = record
        self.discovery_service = discovery_service or DeviceDiscoveryService()
        self.thread_pool = QThreadPool.globalInstance()
        self._scan_generation = 0
        self._scan_tasks: set[CallTask] = set()
        self.setWindowTitle("编辑设备" if record else "添加设备")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        title = QLabel("编辑设备信息" if record else "添加到设备中心")
        title.setObjectName("WindowTitle")
        note = QLabel("设备配置保存在本机；连接驱动可稍后安装。")
        note.setObjectName("Muted")
        layout.addWidget(title)
        layout.addWidget(note)

        form = QFormLayout()
        form.setContentsMargins(0, 12, 0, 12)
        form.setVerticalSpacing(12)
        self.kind = QComboBox()
        for kind in DeviceKind:
            self.kind.addItem(kind.label, kind)
        self.name = QLineEdit()
        self.resource = QComboBox()
        self.resource.setEditable(True)
        self.resource.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.search_button = QPushButton("搜索")
        self.search_button.setToolTip("重新搜索本机可用的 VISA/NI-DAQmx 资源")
        resource_widget = QWidget()
        resource_layout = QHBoxLayout(resource_widget)
        resource_layout.setContentsMargins(0, 0, 0, 0)
        resource_layout.setSpacing(8)
        resource_layout.addWidget(self.resource, 1)
        resource_layout.addWidget(self.search_button)
        self.connection = QComboBox()
        self.connection.setEditable(True)
        form.addRow("设备类型", self.kind)
        form.addRow("显示名称", self.name)
        form.addRow("资源地址/设备名", resource_widget)
        form.addRow("连接方式", self.connection)
        layout.addLayout(form)

        self.scan_status = QLabel("等待搜索设备资源")
        self.scan_status.setObjectName("Muted")
        self.scan_status.setWordWrap(True)
        layout.addWidget(self.scan_status)

        self.hint = QLabel()
        self.hint.setObjectName("Muted")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存设备")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("PrimaryButton")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if record:
            self.kind.setCurrentIndex(self.kind.findData(record.kind))
            self.name.setText(record.name)
        self._kind_changed()
        if record:
            self.resource.setEditText(record.resource)
        if record and record.connection:
            self.connection.setCurrentText(record.connection)
        self.kind.currentIndexChanged.connect(self._kind_changed_and_scan)
        self.resource.currentIndexChanged.connect(self._resource_selected)
        self.search_button.clicked.connect(self.scan_resources)
        QTimer.singleShot(0, self.scan_resources)

    def _kind_changed_and_scan(self) -> None:
        self.resource.blockSignals(True)
        self.resource.clear()
        self.resource.blockSignals(False)
        self._kind_changed()
        QTimer.singleShot(0, self.scan_resources)

    def _kind_changed(self) -> None:
        selected: DeviceKind = self.kind.currentData()
        current_name = self.name.text().strip()
        self.connection.clear()
        if selected is DeviceKind.NI_DAQ:
            self.connection.addItems(["NI-DAQmx 本机设备", "USB"])
            self.resource.lineEdit().setPlaceholderText("自动搜索，或手动输入 Dev1")
            self.hint.setText("自动读取 NI-DAQmx 中已注册的设备；也可手动输入 NI MAX 中的设备名。")
            if not current_name:
                self.name.setPlaceholderText("例如 NI USB-6363")
        else:
            self.connection.addItems(["USB VISA", "TCP/IP VISA", "GPIB VISA", "串口 VISA"])
            self.resource.lineEdit().setPlaceholderText("自动搜索，或手动输入 USB0::...::INSTR")
            self.hint.setText("自动枚举本机 VISA 资源；DG4000 推荐 USB 或局域网连接。")
            if not current_name:
                self.name.setPlaceholderText("例如 实验台 DG4000")

    def scan_resources(self) -> None:
        self._scan_generation += 1
        generation = self._scan_generation
        kind: DeviceKind = self.kind.currentData()
        self.search_button.setEnabled(False)
        self.search_button.setText("搜索中…")
        self.scan_status.setText(
            "正在搜索 NI-DAQmx 设备…" if kind is DeviceKind.NI_DAQ else "正在搜索 VISA 资源…"
        )
        task = CallTask(lambda: self.discovery_service.discover(kind))
        self._scan_tasks.add(task)
        task.signals.succeeded.connect(
            lambda results, token=generation, selected_kind=kind: self._discovery_completed(
                token, selected_kind, results
            )
        )
        task.signals.failed.connect(
            lambda message, token=generation: self._discovery_failed(token, message)
        )
        task.signals.finished.connect(lambda item=task: self._scan_tasks.discard(item))
        self.thread_pool.start(task)

    def _discovery_completed(
        self,
        generation: int,
        kind: DeviceKind,
        results: Any,
    ) -> None:
        if generation != self._scan_generation or kind is not self.kind.currentData():
            return
        discovered = list(results)
        current_resource = self.selected_resource()
        self.resource.blockSignals(True)
        self.resource.clear()
        selected_index = -1
        for index, item in enumerate(discovered):
            self.resource.addItem(item.display_text, item)
            if item.identity:
                self.resource.setItemData(index, item.identity, Qt.ItemDataRole.ToolTipRole)
            if item.resource.casefold() == current_resource.casefold():
                selected_index = index
        if selected_index >= 0:
            self.resource.setCurrentIndex(selected_index)
        elif current_resource:
            self.resource.setEditText(current_resource)
        elif discovered:
            self.resource.setCurrentIndex(0)
        self.resource.blockSignals(False)
        self.search_button.setEnabled(True)
        self.search_button.setText("重新搜索")
        if discovered:
            self.scan_status.setText(f"已发现 {len(discovered)} 个可用资源，请从下拉框选择。")
            self._resource_selected(self.resource.currentIndex())
        else:
            self.scan_status.setText("未发现可用资源；可以继续手动输入资源地址或设备名。")

    def _discovery_failed(self, generation: int, message: str) -> None:
        if generation != self._scan_generation:
            return
        self.search_button.setEnabled(True)
        self.search_button.setText("重新搜索")
        self.scan_status.setText(message)

    def _resource_selected(self, _index: int) -> None:
        selected = self.resource.currentData()
        if isinstance(selected, DiscoveredResource):
            self.connection.setCurrentText(selected.connection)

    def selected_resource(self) -> str:
        selected = self.resource.currentData()
        if isinstance(selected, DiscoveredResource) and self.resource.currentText() == selected.display_text:
            return selected.resource.strip()
        return self.resource.currentText().strip()

    def _validate_and_accept(self) -> None:
        if not self.name.text().strip():
            QMessageBox.warning(self, "信息不完整", "请输入设备显示名称。")
            self.name.setFocus()
            return
        if not self.selected_resource():
            QMessageBox.warning(self, "信息不完整", "请输入资源地址或 NI 设备名。")
            self.resource.setFocus()
            return
        self.accept()

    def result_record(self) -> DeviceRecord:
        values = {
            "kind": self.kind.currentData(),
            "name": self.name.text().strip(),
            "resource": self.selected_resource(),
            "connection": self.connection.currentText().strip(),
        }
        if self.record:
            return self.record.updated(**values)
        return DeviceRecord.create(**values)
