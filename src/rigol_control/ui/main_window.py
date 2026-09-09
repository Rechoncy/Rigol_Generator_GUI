from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QThreadPool, QTimer, Qt
from PyQt6.QtGui import QCloseEvent, QResizeEvent
from PyQt6.QtWidgets import (
    QFrame,
    QCheckBox,
    QFileDialog,
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
from ..config_archive import ConfigArchiveService, JOURNAL
from ..drivers.vaunix_lms import VIRTUAL_VAUNIX_RESOURCE
from ..drivers.newport_smc100 import VIRTUAL_SMC100_RESOURCE
from ..models import DeviceKind, DeviceRecord
from ..repository import (
    DeviceParameterRepository,
    DeviceRepository,
    DuplicateDeviceError,
)
from ..services import DeviceProbeService, ProbeResult
from ..virtual_lab import VIRTUAL_DG2_RESOURCE, VIRTUAL_DG_RESOURCE, VIRTUAL_NI_RESOURCE
from .device_windows import BaseDeviceWindow, create_device_window
from .dialogs import DeviceDialog
from .linkage_window import LinkageTaskWindow
from .widgets import AddDeviceCard, CallTask, DeviceCard, ProbeTask


class DeviceCenterWindow(QMainWindow):
    def __init__(
        self,
        repository: DeviceRepository,
        connection_manager: DeviceConnectionManager | None = None,
    ) -> None:
        super().__init__()
        self.repository = repository
        self.parameter_repository = DeviceParameterRepository(repository.data_dir)
        self.connection_manager = connection_manager or DeviceConnectionManager()
        self.probe_service = DeviceProbeService()
        self.thread_pool = QThreadPool.globalInstance()
        self.cards: dict[str, DeviceCard] = {}
        self.card_widgets: list[QWidget] = []
        self.probe_tasks: dict[str, ProbeTask] = {}
        self.connection_tasks: dict[str, CallTask] = {}
        self.open_windows: dict[str, BaseDeviceWindow] = {}
        self.linkage_window: LinkageTaskWindow | None = None
        self._last_columns = 0
        self._transfer_task = None
        self._configuration_recovery_failed = False

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
        subtitle = QLabel("统一保存和打开 DG4000、Vaunix、Newport 旋转台与 NI 采集卡")
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
        virtual.setToolTip("创建并连接两台虚拟 DG4000、NI、Vaunix 和 Newport PR50CC")
        virtual.clicked.connect(self.create_virtual_lab)
        linkage = QPushButton("跨设备联动")
        linkage.setToolTip("使用一个 NI 触发源统一驱动多台 DG4000 的联合扫描")
        linkage.clicked.connect(self.open_linkage_tasks)
        add = QPushButton("＋ 添加设备")
        add.setObjectName("PrimaryButton")
        add.clicked.connect(self.add_device)
        header_layout.addWidget(self.count_label)
        header_layout.addWidget(refresh)
        header_layout.addWidget(virtual)
        header_layout.addWidget(linkage)
        header_layout.addWidget(add)
        outer.addWidget(header)

        config_actions = QHBoxLayout()
        config_hint = QLabel("版本更新前可导出完整配置，更新后导入恢复")
        config_hint.setObjectName("Muted")
        config_actions.addWidget(config_hint)
        config_actions.addStretch()
        self.export_config_button = QPushButton("导出配置")
        self.import_config_button = QPushButton("导入配置")
        self.export_config_button.setToolTip("导出设备、连接地址、全部设备参数及已保存任务；不包含采集数据")
        self.import_config_button.setToolTip("校验配置包，自动备份当前配置后整体恢复；可恢复设备连接")
        self.export_config_button.clicked.connect(lambda: self.export_configuration())
        self.import_config_button.clicked.connect(lambda: self.import_configuration())
        config_actions.addWidget(self.export_config_button)
        config_actions.addWidget(self.import_config_button)
        outer.addLayout(config_actions)

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
        path = QLabel("设备列表及参数已自动保存在本机")
        path.setObjectName("Muted")
        path.setToolTip(
            f"设备列表：{self.repository.path}\n"
            f"设备参数：{self.parameter_repository.path}"
        )
        footer.addWidget(path)
        footer.addStretch()
        version = QLabel("DG4000 + Vaunix + NI 联动控制 · v0.6.0")
        version.setObjectName("Muted")
        footer.addWidget(version)
        outer.addLayout(footer)

        self.setCentralWidget(root)
        self.reload_devices()
        self.connection_timer = QTimer(self)
        self.connection_timer.setInterval(500)
        self.connection_timer.timeout.connect(self._sync_connection_states)
        self.connection_timer.start()

    def reload_devices(self, *, probe: bool = True) -> None:
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
        self._sync_open_windows(records)
        if records and probe:
            self.probe_all()

    def _flush_device_parameters(self) -> None:
        # Read live controls instead of relying on the 500 ms autosave timer.
        # Save directly so an I/O failure aborts export instead of being swallowed.
        for window in tuple(self.open_windows.values()):
            snapshot = window._device_parameter_snapshot()
            self.parameter_repository.save(window.record.id, kind=window.record.kind.value,
                                           parameters=snapshot)
            window._last_parameter_snapshot = snapshot

    def _connected_device_ids(self) -> tuple[str, ...]:
        return tuple(r.id for r in self.repository.list() if self.connection_manager.is_connected(r))

    def export_configuration(self, path: str | Path | None = None) -> None:
        if self._transfer_task is not None:
            return
        if path is None:
            name = f"lab-configuration-{datetime.now():%Y%m%d-%H%M%S}.json"
            path, _ = QFileDialog.getSaveFileName(self, "导出完整配置", name, "配置包 (*.json)")
            if not path:
                return
        try:
            path = Path(path)
            if not path.suffix:
                path = path.with_suffix(".json")
            self._flush_device_parameters()
            bundle = ConfigArchiveService(self.repository.data_dir).export_file(path, self._connected_device_ids())
            self.statusBar().showMessage(f"已导出：{path} · {bundle.summary}", 15000)
        except Exception as exc:
            QMessageBox.warning(self, "配置导出失败", str(exc))

    def _assert_import_idle(self) -> None:
        if self._transfer_task is not None:
            raise RuntimeError("配置导入尚未结束")
        if self.connection_tasks or self.probe_tasks:
            raise RuntimeError("设备连接或探测尚未完成，请稍后再导入")
        if self.connection_manager.active_leases():
            raise RuntimeError("请先停止扫描、触发和模拟采集，再导入配置")
        windows = [*self.open_windows.values()]
        if self.linkage_window is not None:
            windows.append(self.linkage_window)
        for window in windows:
            if getattr(window, "_call_tasks", None) or getattr(window, "_manual_busy", False):
                raise RuntimeError("请等待设备后台操作完成，再导入配置")
            controller = getattr(window, "controller", None)
            if controller is not None and getattr(controller, "is_running", False):
                raise RuntimeError("请先停止旋转扫描，再导入配置")

    def import_configuration(self, path: str | Path | None = None) -> None:
        try:
            self._assert_import_idle()
            if path is None:
                path, _ = QFileDialog.getOpenFileName(self, "导入完整配置", "", "配置包 (*.json)")
                if not path:
                    return
            bundle = ConfigArchiveService(self.repository.data_dir).read_file(path)
            dialog = QMessageBox(self)
            dialog.setWindowTitle("恢复完整配置")
            dialog.setIcon(QMessageBox.Icon.Question)
            dialog.setText(bundle.summary)
            dialog.setInformativeText(
                "将整体替换本机设备、参数和已保存任务，保留设备与任务的对应关系。当前配置会自动备份，"
                "已打开的设置窗口将关闭。\n"
                "恢复参数只更新界面，不自动开启输出、移动转台或启动任务。"
                "跨设备窗口尚未保存的编辑请先保存。")
            reconnect = QCheckBox(f"导入后重新连接备份中已连接的 {len(bundle.connected_ids)} 台设备")
            reconnect.setChecked(bool(bundle.connected_ids))
            reconnect.setEnabled(bool(bundle.connected_ids))
            dialog.setCheckBox(reconnect)
            dialog.setDetailedText(f"来源：{path}\n导出版本：{bundle.payload.get('application_version', '未知')}\n"
                                   f"导出时间：{bundle.payload.get('exported_utc', '未知')}\n"
                                   + "\n".join(f"{d['name']} · {d['kind']} · {d['resource']}"
                                               for d in bundle.files["devices.json"]["devices"])
                                   + "\n"
                                   + "\n".join(bundle.warnings))
            dialog.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
            dialog.button(QMessageBox.StandardButton.Ok).setText("备份并导入")
            dialog.button(QMessageBox.StandardButton.Cancel).setText("取消")
            dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if dialog.exec() != QMessageBox.StandardButton.Ok:
                return
            self._restore_configuration(bundle, reconnect=reconnect.isChecked())
        except Exception as exc:
            QMessageBox.warning(self, "配置导入失败", str(exc))

    def _restore_configuration(self, bundle, *, reconnect: bool) -> None:
        self._assert_import_idle()
        self._flush_device_parameters()
        previous_connected = self._connected_device_ids()
        for window in tuple(self.open_windows.values()):
            if not window.close():
                raise RuntimeError("设备窗口尚未关闭，已取消导入")
        self.open_windows.clear()
        if self.linkage_window is not None:
            if not self.linkage_window.close():
                raise RuntimeError("联动窗口尚未关闭，已取消导入")
            self.linkage_window = None
        service = ConfigArchiveService(self.repository.data_dir)
        self.connection_timer.stop()
        self.setEnabled(False)
        self.statusBar().showMessage("正在备份并恢复配置…")

        def restore():
            self.connection_manager.close_all()
            return service.import_bundle(bundle, previous_connected_ids=previous_connected)

        task = CallTask(restore)
        self._transfer_task = task
        task.signals.succeeded.connect(lambda backup: self._configuration_restored(bundle, backup, reconnect))
        task.signals.failed.connect(lambda message: self._configuration_restore_failed(message, previous_connected))
        task.signals.finished.connect(self._configuration_transfer_finished)
        self.thread_pool.start(task)

    def _configuration_restored(self, bundle, backup, reconnect):
        self.reload_devices(probe=False)
        self.statusBar().showMessage(f"配置已恢复。{bundle.summary} 原配置备份：{backup}", 30000)
        if reconnect:
            for device_id in bundle.connected_ids:
                self.connect_device(device_id)

    def _configuration_restore_failed(self, message, previous_connected=()):
        self._configuration_recovery_failed = (self.repository.data_dir / JOURNAL).exists()
        if not self._configuration_recovery_failed:
            self.reload_devices(probe=False)
            for device_id in previous_connected:
                self.connect_device(device_id)
        QMessageBox.warning(self, "配置导入失败", message)

    def _configuration_transfer_finished(self):
        self._transfer_task = None
        self.setEnabled(not self._configuration_recovery_failed)
        if not self._configuration_recovery_failed:
            self.connection_timer.start()

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
            (DeviceKind.NEWPORT_SMC100, "虚拟 Newport PR50CC", VIRTUAL_SMC100_RESOURCE),
            (
                DeviceKind.DG4000,
                "虚拟 DG4162",
                VIRTUAL_DG_RESOURCE,
            ),
            (
                DeviceKind.DG4000,
                "虚拟 DG4162-B",
                VIRTUAL_DG2_RESOURCE,
            ),
            (
                DeviceKind.NI_DAQ,
                "虚拟 NI USB-6363",
                VIRTUAL_NI_RESOURCE,
            ),
            (
                DeviceKind.VAUNIX_LMS,
                "虚拟 Vaunix BLX-403",
                VIRTUAL_VAUNIX_RESOURCE,
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

    def open_linkage_tasks(self) -> None:
        if self.linkage_window is not None:
            self.linkage_window.showNormal()
            self.linkage_window.raise_()
            self.linkage_window.activateWindow()
            return
        self.linkage_window = LinkageTaskWindow(
            self.repository,
            self.connection_manager,
            parent=None,
        )
        self.linkage_window.destroyed.connect(self._linkage_window_destroyed)
        self.linkage_window.show()
        self.linkage_window.raise_()
        self.linkage_window.activateWindow()

    def _linkage_window_destroyed(self, _object=None) -> None:
        self.linkage_window = None

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
            self.parameter_repository.delete(device_id)
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
        window = create_device_window(
            record,
            all_devices,
            self.connection_manager,
            device_provider=self.repository.list,
            parameter_repository=self.parameter_repository,
        )
        self.open_windows[device_id] = window
        window.destroyed.connect(lambda _=None, key=device_id, old=window: self._remove_closed_window(key, old))
        window.show()
        window.raise_()
        window.activateWindow()

    def _remove_closed_window(self, key, old):
        if self.open_windows.get(key) is old:
            self.open_windows.pop(key, None)

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
        self._sync_open_windows([item.record for item in self.cards.values()])

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
        self._sync_open_windows([item.record for item in self.cards.values()])

    def _sync_open_windows(self, records: list[DeviceRecord]) -> None:
        for window in tuple(self.open_windows.values()):
            sync_connection = getattr(window, "sync_connection_state", None)
            if callable(sync_connection):
                sync_connection()
            update_ni_devices = getattr(window, "update_ni_devices", None)
            if callable(update_ni_devices):
                update_ni_devices(records)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._transfer_task is not None:
            event.ignore()
            return
        self.connection_timer.stop()
        for window in list(self.open_windows.values()):
            if not window.close():
                self.connection_timer.start()
                event.ignore()
                return
        if self.linkage_window is not None:
            self.linkage_window.close()
            self.linkage_window = None
        self.connection_manager.close_all()
        super().closeEvent(event)
