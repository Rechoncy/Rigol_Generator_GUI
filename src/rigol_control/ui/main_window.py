from __future__ import annotations

from PyQt6.QtCore import QThreadPool, QTimer, Qt
from PyQt6.QtGui import QCloseEvent, QResizeEvent
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..connections import ConnectionInfo, DeviceConnectionManager
from ..models import DeviceKind, DeviceRecord
from ..repository import DeviceRepository, DuplicateDeviceError
from ..services import DeviceProbeService, ProbeResult
from ..virtual_lab import VIRTUAL_DG_RESOURCE, VIRTUAL_NI_RESOURCE
from .device_windows import BaseDeviceWindow, create_device_window
from .dialogs import DeviceDialog
from .widgets import AddDeviceCard, CallTask, DeviceCard, ProbeTask


class DeviceCenterWindow(QMainWindow):
    def __init__(
        self,
        repository: DeviceRepository,
        connection_manager: DeviceConnectionManager | None = None,
    ) -> None:
        super().__init__()
        self.repository = repository
        self.connection_manager = connection_manager or DeviceConnectionManager()
        self.probe_service = DeviceProbeService()
        self.thread_pool = QThreadPool.globalInstance()
        self.cards: dict[str, DeviceCard] = {}
        self.card_widgets: list[QWidget] = []
        self.probe_tasks: dict[str, ProbeTask] = {}
        self.connection_tasks: dict[str, CallTask] = {}
        self.open_windows: dict[str, BaseDeviceWindow] = {}
        self._last_columns = 0

        self.setWindowTitle("实验室设备中心")
        self.resize(1240, 790)
        self.setMinimumSize(760, 560)

        root = QWidget()
        root.setObjectName("AppRoot")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(18)

        header = QFrame()
        header.setObjectName("Header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(20, 16, 18, 16)
        titles = QVBoxLayout()
        title = QLabel("设备中心")
        title.setObjectName("Title")
        subtitle = QLabel("统一保存和打开 DG4000、NI 采集卡及其他仪器")
        subtitle.setObjectName("Muted")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header_layout.addLayout(titles)
        header_layout.addStretch()
        self.count_label = QLabel("0 台设备")
        self.count_label.setObjectName("Muted")
        refresh = QPushButton("刷新状态")
        refresh.clicked.connect(self.probe_all)
        virtual = QPushButton("虚拟实验室")
        virtual.setToolTip("创建并连接一套无需硬件的 DG4000 + NI 模拟设备")
        virtual.clicked.connect(self.create_virtual_lab)
        add = QPushButton("＋ 添加设备")
        add.setObjectName("PrimaryButton")
        add.clicked.connect(self.add_device)
        header_layout.addWidget(self.count_label)
        header_layout.addWidget(refresh)
        header_layout.addWidget(virtual)
        header_layout.addWidget(add)
        outer.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(1, 1, 1, 1)
        self.grid.setHorizontalSpacing(16)
        self.grid.setVerticalSpacing(16)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(self.grid_host)
        outer.addWidget(scroll, 1)

        footer = QHBoxLayout()
        path = QLabel("设备列表已自动保存在本机")
        path.setObjectName("Muted")
        path.setToolTip(f"保存位置：{self.repository.path}")
        footer.addWidget(path)
        footer.addStretch()
        version = QLabel("DG4000 + NI 联动控制 · v0.2")
        version.setObjectName("Muted")
        footer.addWidget(version)
        outer.addLayout(footer)

        self.setCentralWidget(root)
        self.reload_devices()
        self.connection_timer = QTimer(self)
        self.connection_timer.setInterval(500)
        self.connection_timer.timeout.connect(self._sync_connection_states)
        self.connection_timer.start()

    def reload_devices(self) -> None:
        try:
            records = self.repository.list()
        except RuntimeError as exc:
            QMessageBox.critical(self, "无法加载设备", str(exc))
            records = []

        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.cards.clear()
        self.card_widgets.clear()

        for record in records:
            card = DeviceCard(record)
            card.opened.connect(self.open_device)
            card.edited.connect(self.edit_device)
            card.deleted.connect(self.delete_device)
            card.connection_requested.connect(self.connect_device)
            card.disconnection_requested.connect(self.disconnect_device)
            card.set_connection_state(self.connection_manager.is_connected(record))
            self.cards[record.id] = card
            self.card_widgets.append(card)
        add_card = AddDeviceCard()
        add_card.clicked.connect(self.add_device)
        self.card_widgets.append(add_card)

        self.count_label.setText(f"{len(records)} 台设备")
        self._last_columns = 0
        self._layout_cards()
        if records:
            self.probe_all()

    def _layout_cards(self) -> None:
        available = max(1, self.grid_host.width() - 2)
        columns = max(1, min(3, available // 330))
        if columns == self._last_columns and self.grid.count() == len(self.card_widgets):
            return
        while self.grid.count():
            self.grid.takeAt(0)
        for index, card in enumerate(self.card_widgets):
            self.grid.addWidget(card, index // columns, index % columns)
        for column in range(columns):
            self.grid.setColumnStretch(column, 1)
        self._last_columns = columns

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._layout_cards)

    def add_device(self) -> None:
        dialog = DeviceDialog(parent=self)
        if dialog.exec() != DeviceDialog.DialogCode.Accepted:
            return
        self._save_dialog_record(dialog.result_record())

    def create_virtual_lab(self) -> None:
        records = self.repository.list()
        specifications = (
            (
                DeviceKind.DG4000,
                "虚拟 DG4162",
                VIRTUAL_DG_RESOURCE,
            ),
            (
                DeviceKind.NI_DAQ,
                "虚拟 NI USB-6363",
                VIRTUAL_NI_RESOURCE,
            ),
        )
        virtual_ids: list[str] = []
        try:
            for kind, name, resource in specifications:
                existing = next(
                    (
                        item
                        for item in records
                        if item.kind is kind and item.resource.casefold() == resource.casefold()
                    ),
                    None,
                )
                if existing is None:
                    existing = DeviceRecord.create(
                        kind=kind,
                        name=name,
                        resource=resource,
                        connection="内置模拟器",
                    )
                    self.repository.save(existing)
                    records.append(existing)
                virtual_ids.append(existing.id)
        except Exception as exc:
            QMessageBox.warning(self, "无法创建虚拟实验室", str(exc))
            return
        self.reload_devices()
        for device_id in virtual_ids:
            QTimer.singleShot(0, lambda key=device_id: self.connect_device(key))

    def edit_device(self, device_id: str) -> None:
        record = self.repository.get(device_id)
        if not record:
            QMessageBox.warning(self, "设备不存在", "该设备记录已被删除。")
            self.reload_devices()
            return
        dialog = DeviceDialog(record, self)
        if dialog.exec() != DeviceDialog.DialogCode.Accepted:
            return
        self._save_dialog_record(dialog.result_record())

    def _save_dialog_record(self, record: DeviceRecord) -> None:
        try:
            existing = self.repository.get(record.id)
            if existing is not None and (
                existing.kind is not record.kind or existing.resource != record.resource
            ):
                self.connection_manager.forget(existing)
            self.repository.save(record)
        except (DuplicateDeviceError, RuntimeError, ValueError) as exc:
            QMessageBox.warning(self, "无法保存设备", str(exc))
            return
        self.reload_devices()

    def delete_device(self, device_id: str) -> None:
        record = self.repository.get(device_id)
        if not record:
            return
        answer = QMessageBox.question(
            self,
            "删除设备",
            f"确认从设备中心删除“{record.name}”吗？\n这不会修改或重置实体仪器。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        window = self.open_windows.pop(device_id, None)
        if window:
            window.close()
        try:
            self.connection_manager.forget(record)
            self.repository.delete(device_id)
        except Exception as exc:
            QMessageBox.warning(self, "删除失败", str(exc))
            return
        self.reload_devices()

    def open_device(self, device_id: str) -> None:
        existing = self.open_windows.get(device_id)
        if existing:
            existing.showNormal()
            existing.raise_()
            existing.activateWindow()
            return
        record = self.repository.get(device_id)
        if not record:
            return
        try:
            record = self.repository.save(record.mark_opened())
            all_devices = self.repository.list()
        except RuntimeError as exc:
            QMessageBox.warning(self, "打开失败", str(exc))
            return
        window = create_device_window(record, all_devices, self.connection_manager)
        self.open_windows[device_id] = window
        window.destroyed.connect(lambda _=None, key=device_id: self.open_windows.pop(key, None))
        window.show()
        window.raise_()
        window.activateWindow()

    def probe_all(self) -> None:
        for device_id, card in list(self.cards.items()):
            if device_id in self.probe_tasks:
                continue
            if self.connection_manager.is_connected(card.record):
                card.set_connection_state(True)
                continue
            card.status.setObjectName("StatusPending")
            card.status.setText("● 检测中")
            card.status.style().unpolish(card.status)
            card.status.style().polish(card.status)
            task = ProbeTask(card.record, self.probe_service)
            task.signals.completed.connect(self._probe_completed)
            self.probe_tasks[device_id] = task
            self.thread_pool.start(task)

    def _probe_completed(self, device_id: str, result: ProbeResult) -> None:
        self.probe_tasks.pop(device_id, None)
        card = self.cards.get(device_id)
        if card:
            if self.connection_manager.is_connected(card.record):
                card.set_connection_state(True)
            else:
                card.set_probe_result(result)

    def connect_device(self, device_id: str) -> None:
        record = self.repository.get(device_id)
        card = self.cards.get(device_id)
        if record is None or card is None or device_id in self.connection_tasks:
            return
        card.set_connection_state(False, busy=True)
        task = CallTask(lambda: self.connection_manager.connect(record))
        self.connection_tasks[device_id] = task
        task.signals.succeeded.connect(
            lambda result, key=device_id: self._connection_completed(key, result)
        )
        task.signals.failed.connect(
            lambda message, key=device_id: self._connection_failed(key, message)
        )
        task.signals.finished.connect(lambda key=device_id: self.connection_tasks.pop(key, None))
        self.thread_pool.start(task)

    def disconnect_device(self, device_id: str) -> None:
        record = self.repository.get(device_id)
        card = self.cards.get(device_id)
        if record is None or card is None or device_id in self.connection_tasks:
            return
        card.set_connection_state(True, busy=True)
        task = CallTask(lambda: self.connection_manager.disconnect(record))
        self.connection_tasks[device_id] = task
        task.signals.succeeded.connect(
            lambda result, key=device_id: self._connection_completed(key, result)
        )
        task.signals.failed.connect(
            lambda message, key=device_id: self._connection_failed(key, message)
        )
        task.signals.finished.connect(lambda key=device_id: self.connection_tasks.pop(key, None))
        self.thread_pool.start(task)

    def _connection_completed(self, device_id: str, result: ConnectionInfo) -> None:
        card = self.cards.get(device_id)
        if card:
            card.set_connection_state(result.connected)
            card.status.setToolTip("\n".join(filter(None, (result.message, result.identity))))

    def _connection_failed(self, device_id: str, message: str) -> None:
        card = self.cards.get(device_id)
        if card:
            card.set_connection_state(False)
            card.status.setObjectName("StatusOffline")
            card.status.setText("● 连接失败")
            card.status.setToolTip(message)
            card.status.style().unpolish(card.status)
            card.status.style().polish(card.status)
        QMessageBox.warning(self, "设备连接失败", message)

    def _sync_connection_states(self) -> None:
        for card in self.cards.values():
            if card.record.id in self.connection_tasks:
                continue
            connected = self.connection_manager.is_connected(card.record)
            if connected:
                card.set_connection_state(True)
            elif card.status.text() == "● 已连接":
                card.set_connection_state(False)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.connection_timer.stop()
        for window in list(self.open_windows.values()):
            window.close()
        self.connection_manager.close_all()
        super().closeEvent(event)
