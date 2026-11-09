from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any

from PyQt6.QtCore import QThreadPool, Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..connections import DeviceConnectionManager
from ..drivers.dg4000 import StepParameter
from ..linkage import (
    DispatchMode,
    LinkageAxis,
    LinkagePoint,
    LinkageRunLogger,
    LinkageSnapshot,
    LinkageStepValue,
    MultiDeviceTriggerController,
    generate_linkage_points,
    linkage_point_count,
)
from ..linkage_tasks import LinkageTaskRecord, LinkageTaskRepository
from ..linkage_parameters import (
    LinkageParameter,
    SsgLinkageMode,
    parameters_for_kind,
    validate_source_axis,
)
from ..models import DeviceKind, DeviceRecord
from ..repository import DeviceRepository
from ..scan import MAX_SCAN_AXES, ScanMode
from ..sequence import MAX_SCAN_POINTS
from ..trigger import ControllerState
from ..units import default_unit, from_base, roundtrip_number, to_base, unit_spec, unit_specs
from ..virtual_lab import is_virtual_resource
from .snapshot_bridge import SnapshotBridge
from .widgets import CallTask


def _panel(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Panel")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(17, 15, 17, 17)
    layout.setSpacing(11)
    heading = QLabel(title)
    heading.setObjectName("SectionTitle")
    layout.addWidget(heading)
    return frame, layout


class LinkageBridge(SnapshotBridge):
    pass


class CompactDoubleSpinBox(QDoubleSpinBox):
    def textFromValue(self, value: float) -> str:
        return f"{value:.12g}"


class LinkageAxisEditor(QFrame):
    changed = pyqtSignal()
    remove_requested = pyqtSignal(object)

    def __init__(
        self,
        generators: list[DeviceRecord],
        axis: LinkageAxis | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("AxisEditor")
        self._generators = {record.id: record for record in generators}
        self._active_unit = axis.unit_key if axis else "kHz"
        layout = QGridLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setHorizontalSpacing(8)

        self.device = QComboBox()
        for record in generators:
            self.device.addItem(f"{record.name} · {record.kind.label} ({record.resource})", record.id)
        self.channel = QComboBox()
        self.parameter = QComboBox()
        self.unit = QComboBox()
        self.start = CompactDoubleSpinBox()
        self.step = CompactDoubleSpinBox()
        for control in (self.start, self.step):
            control.setDecimals(9)
            control.setRange(-1e12, 1e12)
            control.setKeyboardTracking(False)
        self.count = QSpinBox()
        self.count.setRange(1, MAX_SCAN_POINTS)
        self.count.setValue(4)
        remove = QPushButton("删除")
        remove.setObjectName("DangerButton")

        labels = ("设备", "通道", "参数", "单位", "起点", "步长", "点数")
        controls = (
            self.device,
            self.channel,
            self.parameter,
            self.unit,
            self.start,
            self.step,
            self.count,
        )
        for column, (label, control) in enumerate(zip(labels, controls)):
            caption = QLabel(label)
            if column == 5:
                self.step_caption = caption
            caption.setObjectName("Muted")
            layout.addWidget(caption, 0, column)
            layout.addWidget(control, 1, column)
        layout.addWidget(remove, 1, len(controls))
        layout.setColumnStretch(0, 2)
        layout.setColumnStretch(2, 1)

        self.parameter.currentIndexChanged.connect(self._parameter_changed)
        self.unit.currentIndexChanged.connect(self._unit_changed)
        self.device.currentIndexChanged.connect(self._device_changed)
        self.channel.currentIndexChanged.connect(self.changed.emit)
        self.start.valueChanged.connect(self.changed.emit)
        self.step.valueChanged.connect(self.changed.emit)
        self.count.valueChanged.connect(self.changed.emit)
        remove.clicked.connect(
            lambda _checked=False: self.remove_requested.emit(self)
        )

        if axis is None:
            self._device_changed()
            self._populate_units("kHz")
            self.start.setValue(1.0)
            self.step.setValue(1.0)
        else:
            self.device.setCurrentIndex(self.device.findData(axis.device_id))
            self._device_changed()
            self.channel.setCurrentIndex(self.channel.findData(axis.channel))
            self.parameter.setCurrentIndex(self.parameter.findData(axis.parameter))
            self._populate_units(axis.unit_key)
            self.start.setValue(axis.start)
            self.step.setValue(axis.step)
            self.count.setValue(axis.count)

    def _device_changed(self) -> None:
        record = self._generators.get(str(self.device.currentData() or ""))
        old_parameter = self.parameter.currentData()
        old_channel = self.channel.currentData()
        old_unit = self.unit.currentData()
        self.parameter.blockSignals(True)
        self.channel.blockSignals(True)
        try:
            self.parameter.clear()
            self.channel.clear()
            if record is None:
                return
            for parameter in parameters_for_kind(record.kind):
                self.parameter.addItem(parameter.label, parameter)
            self.channel.addItem("CH1" if record.kind is DeviceKind.DG4000 else "RF 输出", 1)
            if record.kind is DeviceKind.DG4000:
                self.channel.addItem("CH2", 2)
            self.channel.setEnabled(record.kind is DeviceKind.DG4000)
            parameter_index = self.parameter.findData(old_parameter)
            self.parameter.setCurrentIndex(max(0, parameter_index))
            channel_index = self.channel.findData(old_channel)
            self.channel.setCurrentIndex(max(0, channel_index))
        finally:
            self.parameter.blockSignals(False)
            self.channel.blockSignals(False)
        # Preserve values only when their meaning survives the device change.
        # In particular, a DG Vpp amplitude never becomes an RF dBm value.
        if self.parameter.currentData() is old_parameter:
            self._populate_units(str(old_unit) if old_unit else None)
        else:
            self._parameter_changed()
        self.changed.emit()

    def model(self) -> LinkageAxis:
        return LinkageAxis(
            device_id=str(self.device.currentData() or ""),
            parameter=self.parameter.currentData(),
            channel=int(self.channel.currentData()),
            unit_key=str(self.unit.currentData()),
            start=self.start.value(),
            step=self.step.value(),
            count=self.count.value(),
        )

    def _populate_units(self, requested: str | None = None) -> None:
        parameter = self.parameter.currentData()
        if parameter is None:
            return
        selected = requested or default_unit(parameter.unit_family)
        self.unit.blockSignals(True)
        self.unit.clear()
        for spec in unit_specs(parameter.unit_family):
            self.unit.addItem(spec.symbol, spec.key)
        index = self.unit.findData(selected)
        self.unit.setCurrentIndex(index if index >= 0 else 0)
        self.unit.blockSignals(False)
        self._active_unit = str(self.unit.currentData())

    def _parameter_changed(self) -> None:
        self._populate_units()
        self.step_caption.setText(
            "步长 / dB" if self.parameter.currentData() is LinkageParameter.RF_POWER else "步长"
        )
        self.start.setValue(-20.0 if self.parameter.currentData() is LinkageParameter.RF_POWER else 1.0)
        self.step.setValue(1.0)
        self.changed.emit()

    def _unit_changed(self) -> None:
        new_unit = str(self.unit.currentData() or "")
        if not new_unit or new_unit == self._active_unit:
            return
        parameter = self.parameter.currentData()
        try:
            start_base = to_base(self.start.value(), parameter.unit_family, self._active_unit)
            step_base = to_base(self.step.value(), parameter.unit_family, self._active_unit)
            self._active_unit = new_unit
            self.start.setValue(from_base(start_base, parameter.unit_family, new_unit))
            self.step.setValue(from_base(step_base, parameter.unit_family, new_unit))
        finally:
            self.changed.emit()


@dataclass(slots=True)
class RuntimeDeviceRow:
    name: QTableWidgetItem
    status: QTableWidgetItem
    elapsed: QTableWidgetItem


class LinkageTaskWindow(QMainWindow):
    """Edit NI-driven software groups and SSG native external participants."""

    def __init__(
        self,
        repository: DeviceRepository,
        connection_manager: DeviceConnectionManager,
        task_repository: LinkageTaskRepository | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.connection_manager = connection_manager
        self.task_repository = task_repository or LinkageTaskRepository(repository.data_dir)
        self.devices = repository.list()
        self.generators = [item for item in self.devices if item.kind in {
            DeviceKind.DG4000, DeviceKind.VAUNIX_LMS, DeviceKind.SIGLENT_SSG,
        }]
        self.ni_devices = [item for item in self.devices if item.kind is DeviceKind.NI_DAQ]
        self.axis_editors: list[LinkageAxisEditor] = []
        self._table_axes: tuple[LinkageAxis, ...] = ()
        self._table_mode: ScanMode | None = None
        self._current_task_id: str | None = None
        self._lease_owner: str | None = None
        self.controller: MultiDeviceTriggerController | None = None
        self.thread_pool = QThreadPool.globalInstance()
        self._call_tasks: set[CallTask] = set()
        self._pending_stop = False
        self._start_error: str | None = None
        self._start_task: CallTask | None = None
        self._stop_task: CallTask | None = None
        self._stop_error: str | None = None
        self._connect_task: CallTask | None = None
        self._connect_error: str | None = None
        self._connect_cancel = Event()
        self._native_participants: dict[str, Any] = {}
        self._native_records: tuple[DeviceRecord, ...] = ()
        self.bridge = LinkageBridge(self)
        self.bridge.updated.connect(self._controller_updated)
        self._runtime_rows: dict[str, RuntimeDeviceRow] = {}
        self._last_error: str | None = None
        self._last_logged_applied = 0

        self.setWindowTitle("跨设备联动任务")
        self.resize(1380, 900)
        self.setMinimumSize(950, 680)

        root = QWidget()
        root.setObjectName("AppRoot")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(14)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel("跨设备联动任务")
        title.setObjectName("Title")
        subtitle = QLabel(
            "一个 NI 累计触发源驱动 DG4000、Vaunix 与鼎阳软件步进；首触发应用 P0，末点后循环。"
            "鼎阳也可使用仪器原生列表与公共外部触发，实际接收状态单独显示。"
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch()
        outer.addLayout(header)

        scroll = QScrollArea()
        self.scroll = scroll
        scroll.setWidgetResizable(True)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(14)
        body.addWidget(self._task_panel())
        body.addWidget(self._axis_panel())
        body.addWidget(self._point_panel())
        body.addWidget(self._runtime_panel())
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        self.setCentralWidget(root)
        self._ssg_mode_changed()

        if self.generators:
            self._add_axis()
        self._reload_tasks()
        if self.task_selector.currentData():
            self._load_selected_task(show_message=False)
        else:
            self._configuration_changed()

    def _task_panel(self) -> QFrame:
        frame, layout = _panel("任务与触发源")
        task_row = QHBoxLayout()
        self.task_selector = QComboBox()
        self.task_selector.setMinimumWidth(260)
        self.load_task_button = QPushButton("加载")
        self.load_task_button.clicked.connect(
            lambda _checked=False: self._load_selected_task()
        )
        self.new_task_button = QPushButton("新建")
        self.new_task_button.clicked.connect(lambda _checked=False: self._new_task())
        self.save_task_button = QPushButton("保存任务")
        self.save_task_button.setObjectName("PrimaryButton")
        self.save_task_button.clicked.connect(lambda _checked=False: self._save_task())
        self.delete_task_button = QPushButton("删除任务")
        self.delete_task_button.setObjectName("DangerButton")
        self.delete_task_button.clicked.connect(lambda _checked=False: self._delete_task())
        task_row.addWidget(self.task_selector, 1)
        task_row.addWidget(self.load_task_button)
        task_row.addWidget(self.new_task_button)
        task_row.addWidget(self.save_task_button)
        task_row.addWidget(self.delete_task_button)
        layout.addLayout(task_row)
        save_scope = QLabel(
            "保存范围：NI 触发设备、计数器/PFI/边沿、扫描模式、每个轴绑定的设备类型/通道/参数/单位、"
            "起点/步长/点数，以及手工修改后的完整联合点表。保存后下次打开自动恢复。"
        )
        save_scope.setObjectName("Muted")
        save_scope.setWordWrap(True)
        layout.addWidget(save_scope)

        form = QFormLayout()
        self.task_name = QLineEdit("跨设备扫描 1")
        self.ni_source = QComboBox()
        for record in self.ni_devices:
            self.ni_source.addItem(f"{record.name} ({record.resource})", record.id)
        if not self.ni_devices:
            self.ni_source.addItem("请先添加 NI 采集卡", None)
            self.ni_source.setEnabled(False)
        self.counter = QComboBox()
        for index in range(4):
            self.counter.addItem(f"ctr{index}", f"ctr{index}")
        self.terminal = QComboBox()
        self.terminal.setEditable(True)
        self.terminal.addItems([f"PFI{index}" for index in range(16)])
        self.edge = QComboBox()
        self.edge.addItems(["上升沿", "下降沿"])
        self.scan_mode = QComboBox()
        for mode in ScanMode:
            self.scan_mode.addItem(mode.label, mode)
        self.dispatch_mode = QComboBox()
        self.dispatch_mode.addItem(
            DispatchMode.STRICT_SERIAL.label, DispatchMode.STRICT_SERIAL
        )
        self.ssg_mode = QComboBox()
        for mode in SsgLinkageMode:
            self.ssg_mode.addItem(mode.label, mode)
        self.ssg_dwell = CompactDoubleSpinBox()
        self.ssg_dwell.setDecimals(3)
        self.ssg_dwell.setRange(0.01, 100)
        self.ssg_dwell.setValue(0.01)
        self.ssg_dwell.setKeyboardTracking(False)
        self.ssg_dwell.setEnabled(False)
        form.addRow("任务名称", self.task_name)
        form.addRow("NI 触发设备", self.ni_source)
        form.addRow("计数器", self.counter)
        form.addRow("PFI 端子", self.terminal)
        form.addRow("有效边沿", self.edge)
        form.addRow("扫描组合", self.scan_mode)
        form.addRow("设备下发", self.dispatch_mode)
        form.addRow("鼎阳参与方式", self.ssg_mode)
        form.addRow("鼎阳原生驻留 / s", self.ssg_dwell)
        layout.addLayout(form)
        self.ssg_mode_note = QLabel()
        self.ssg_mode_note.setObjectName("Muted")
        self.ssg_mode_note.setWordWrap(True)
        layout.addWidget(self.ssg_mode_note)
        self.ssg_mode.currentIndexChanged.connect(self._ssg_mode_changed)
        self.scan_mode.currentIndexChanged.connect(self._configuration_changed)
        return frame

    def _ssg_mode_changed(self) -> None:
        native = self.ssg_mode.currentData() is SsgLinkageMode.NATIVE_EXTERNAL
        self.ssg_dwell.setEnabled(native and self.controller is None and not self._call_tasks)
        self.ssg_mode_note.setText(
            "鼎阳原生：联合点表预装为 2–500 行 LIST；将同一外部边沿分接 NI PFI 与鼎阳 EXT TRIG。"
            "全部布防完成前请暂停触发。NI 计数不等于鼎阳实际接收，实际接收/积压/点位无法查询时为未知；"
            "触发须满足仪器驻留时间，软件不会补发原生触发。"
            if native else
            "鼎阳软件步进：收到 NI 累计触发后按联合点表设置频率和功率，启动检查不改变输出；"
            "软件顺序下发不表示多台仪器在电气上同时更新。"
        )
        if hasattr(self, "metric_captions"):
            self.metric_captions["received"].setText("NI 已接收触发")
            self.metric_captions["applied"].setText("软件已处理点" if native else "已完成联合点")
            self.metric_captions["backlog"].setText("软件积压" if native else "积压")
            self.metric_captions["current"].setText("软件当前点" if native else "当前点")
        if hasattr(self, "preflight"):
            self._configuration_changed()

    def _axis_panel(self) -> QFrame:
        frame, layout = _panel("跨设备扫描轴")
        toolbar = QHBoxLayout()
        self.add_axis_button = QPushButton("＋ 增加扫描轴")
        self.add_axis_button.clicked.connect(
            lambda _checked=False: self._add_axis()
        )
        self.generate_button = QPushButton("生成联合点表")
        self.generate_button.setObjectName("PrimaryButton")
        self.generate_button.clicked.connect(self._generate_table)
        self.preflight = QLabel()
        self.preflight.setWordWrap(True)
        toolbar.addWidget(self.add_axis_button)
        toolbar.addWidget(self.preflight, 1)
        toolbar.addWidget(self.generate_button)
        layout.addLayout(toolbar)
        self.axis_host = QWidget()
        self.axis_layout = QVBoxLayout(self.axis_host)
        self.axis_layout.setContentsMargins(0, 0, 0, 0)
        self.axis_layout.setSpacing(8)
        layout.addWidget(self.axis_host)
        return frame

    def _point_panel(self) -> QFrame:
        frame, layout = _panel("联合扫描点表")
        note = QLabel(
            "每一行是一次触发的完整设备参数。可双击修改小数；轴配置改变后必须重新生成。"
        )
        note.setObjectName("Muted")
        layout.addWidget(note)
        self.point_table = QTableWidget(0, 2)
        self.point_table.setMinimumHeight(260)
        self.point_table.setAlternatingRowColors(True)
        self.point_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.point_table)
        return frame

    def _runtime_panel(self) -> QFrame:
        frame, layout = _panel("运行与设备屏障")
        metrics = QHBoxLayout()
        self.metrics: dict[str, QLabel] = {}
        self.metric_captions: dict[str, QLabel] = {}
        for key, caption in (
            ("state", "任务状态"),
            ("received", "已接收触发"),
            ("applied", "已完成联合点"),
            ("backlog", "积压"),
            ("current", "当前点"),
            ("skew", "完成偏差"),
        ):
            card = QFrame()
            card.setObjectName("MetricCard")
            card_layout = QVBoxLayout(card)
            label = QLabel(caption)
            self.metric_captions[key] = label
            label.setObjectName("Muted")
            value = QLabel("停止" if key == "state" else ("—" if key in {"current", "skew"} else "0"))
            value.setObjectName("CardTitle")
            card_layout.addWidget(label)
            card_layout.addWidget(value)
            metrics.addWidget(card)
            self.metrics[key] = value
        layout.addLayout(metrics)

        controls = QHBoxLayout()
        self.connect_button = QPushButton("连接参与设备")
        self.connect_button.clicked.connect(self._connect_required_devices)
        self.start_button = QPushButton("启动联动")
        self.start_button.setObjectName("PrimaryButton")
        self.start_button.clicked.connect(self._start)
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        virtual_one = QPushButton("虚拟触发 +1")
        virtual_one.clicked.connect(lambda: self._emit_virtual(1))
        virtual_ten = QPushButton("虚拟触发 +10")
        virtual_ten.clicked.connect(lambda: self._emit_virtual(10))
        controls.addWidget(self.connect_button)
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        controls.addStretch()
        controls.addWidget(virtual_one)
        controls.addWidget(virtual_ten)
        layout.addLayout(controls)

        self.device_table = QTableWidget(0, 3)
        self.device_table.setHorizontalHeaderLabels(["设备", "本点状态", "下发耗时"])
        self.device_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.device_table.setMaximumHeight(180)
        layout.addWidget(self.device_table)
        self.run_log = QPlainTextEdit()
        self.run_log.setReadOnly(True)
        self.run_log.setMaximumBlockCount(500)
        self.run_log.setMaximumHeight(140)
        layout.addWidget(self.run_log)
        self.log_path = QLabel("运行日志将在启动时自动保存为 CSV")
        self.log_path.setObjectName("Muted")
        self.log_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.log_path)
        return frame

    def _add_axis(self, axis: LinkageAxis | bool | None = None) -> None:
        # QPushButton.clicked carries a bool. Keep this guard as a defensive
        # boundary even though the UI connection above now discards it.
        if isinstance(axis, bool):
            axis = None
        if len(self.axis_editors) >= MAX_SCAN_AXES:
            QMessageBox.information(self, "扫描轴已满", f"最多支持 {MAX_SCAN_AXES} 个扫描轴。")
            return
        if not self.generators:
            QMessageBox.warning(self, "缺少信号源", "请先添加 DG4000、Vaunix 或鼎阳 SSG。")
            return
        editor = LinkageAxisEditor(self.generators, axis)
        editor.changed.connect(self._configuration_changed)
        editor.remove_requested.connect(self._remove_axis)
        self.axis_editors.append(editor)
        self.axis_layout.addWidget(editor)
        self._configuration_changed()

    def _remove_axis(self, editor: LinkageAxisEditor) -> None:
        if len(self.axis_editors) <= 1:
            QMessageBox.information(self, "保留扫描轴", "联动任务至少需要一个扫描轴。")
            return
        self.axis_editors.remove(editor)
        editor.deleteLater()
        self._configuration_changed()

    def _axes(self) -> tuple[LinkageAxis, ...]:
        return tuple(editor.model() for editor in self.axis_editors)

    def _validate_live_axes(self, axes: tuple[LinkageAxis, ...]) -> dict[str, DeviceRecord]:
        current = {item.id: item for item in self.repository.list()}
        original = {item.id: item for item in self.generators}
        for axis in axes:
            record = current.get(axis.device_id)
            previous = original.get(axis.device_id)
            if record is None or previous is None:
                raise ValueError("扫描轴设备已经不存在，请重新打开任务窗口并选择设备")
            if record.kind is not previous.kind:
                raise ValueError(f"设备“{record.name}”的类型已改变，请重新打开窗口并确认参数单位")
            if record.resource != previous.resource:
                raise ValueError(f"设备“{record.name}”的资源地址已改变，请重新打开窗口并确认绑定")
            validate_source_axis(axis, record.kind)
        return current

    def _configuration_changed(self, _value: Any = None) -> None:
        try:
            axes = self._axes()
            mode: ScanMode = self.scan_mode.currentData()
            total = linkage_point_count(axes, mode)
            records = self._validate_live_axes(axes)
            if (self.ssg_mode.currentData() is SsgLinkageMode.NATIVE_EXTERNAL
                    and any(records[axis.device_id].kind is DeviceKind.SIGLENT_SSG for axis in axes)
                    and not 2 <= total <= 500):
                raise ValueError("鼎阳原生 LIST 参与时，联合点表必须为 2 至 500 行")
            device_count = len({axis.device_id for axis in axes})
        except (TypeError, ValueError) as exc:
            self.preflight.setText(str(exc))
            self.preflight.setStyleSheet("color:#ff9397;")
            self.generate_button.setEnabled(False)
            return
        mode_note = "最后一轴变化最快" if mode is ScanMode.CARTESIAN else "各轴同序号联动"
        self.preflight.setText(
            f"点表通过：{device_count} 台信号源 · {len(axes)} 个轴 · {total} 个联合点 · "
            f"{mode_note}；启动前按各仪器能力逐点检查"
        )
        self.preflight.setStyleSheet("color:#54d6a3;")
        self.generate_button.setEnabled(True)

    def _generate_table(self, _checked: bool = False, *, show_message: bool = True) -> None:
        try:
            axes = self._axes()
            mode: ScanMode = self.scan_mode.currentData()
            self._validate_live_axes(axes)
            points = generate_linkage_points(axes, mode)
            kinds = {item.id: item.kind for item in self.generators}
            if (self.ssg_mode.currentData() is SsgLinkageMode.NATIVE_EXTERNAL
                    and any(kinds[axis.device_id] is DeviceKind.SIGLENT_SSG for axis in axes)
                    and not 2 <= len(points) <= 500):
                raise ValueError("鼎阳原生 LIST 参与时，联合点表必须为 2 至 500 行")
        except (TypeError, ValueError) as exc:
            QMessageBox.warning(self, "无法生成联合点表", str(exc))
            return
        self._fill_point_table(axes, points)
        self._table_axes = axes
        self._table_mode = mode
        if show_message:
            self.statusBar().showMessage(f"已生成 {len(points)} 个跨设备联合点", 5000)

    def _fill_point_table(
        self, axes: tuple[LinkageAxis, ...], points: tuple[LinkagePoint, ...]
    ) -> None:
        names = {item.id: item.name for item in self.generators}
        headers = ["序号"]
        for axis in axes:
            symbol = unit_spec(axis.parameter.unit_family, axis.unit_key).symbol
            headers.append(
                f"{names.get(axis.device_id, '缺失设备')} · CH{axis.channel} "
                f"{axis.parameter.short_label} ({symbol})"
            )
        headers.append("说明")
        self.point_table.setColumnCount(len(headers))
        self.point_table.setHorizontalHeaderLabels(headers)
        self.point_table.setRowCount(len(points))
        self.point_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for column in range(1, len(headers)):
            self.point_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch
            )
        for row, point in enumerate(points):
            number = QTableWidgetItem(str(row + 1))
            number.setFlags(number.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.point_table.setItem(row, 0, number)
            for column, item in enumerate(point.values, start=1):
                self.point_table.setItem(
                    row, column, QTableWidgetItem(roundtrip_number(item.display_value))
                )
            self.point_table.setItem(
                row,
                len(headers) - 1,
                QTableWidgetItem(
                    ("软件 P0（首个 NI 触发）；鼎阳原生点位未知"
                     if self.ssg_mode.currentData() is SsgLinkageMode.NATIVE_EXTERNAL
                     else "起始点（第一个触发）") if row == 0 else ""
                ),
            )

    def _points_from_table(self) -> tuple[LinkagePoint, ...]:
        axes = self._axes()
        mode: ScanMode = self.scan_mode.currentData()
        if axes != self._table_axes or mode is not self._table_mode:
            raise ValueError("扫描轴配置已改变，请重新生成联合点表")
        if self.point_table.rowCount() < 1:
            raise ValueError("联合点表不能为空")
        points: list[LinkagePoint] = []
        for row in range(self.point_table.rowCount()):
            values: list[LinkageStepValue] = []
            for axis_index, axis in enumerate(axes):
                try:
                    display = float(self.point_table.item(row, axis_index + 1).text().strip())
                    base = to_base(display, axis.parameter.unit_family, axis.unit_key)
                except (AttributeError, ValueError) as exc:
                    raise ValueError(
                        f"第 {row + 1} 行、轴 {axis_index + 1} 不是有效数值"
                    ) from exc
                values.append(
                    LinkageStepValue(
                        device_id=axis.device_id,
                        parameter=axis.parameter,
                        channel=axis.channel,
                        value=base,
                        display_value=display,
                        unit_key=axis.unit_key,
                    )
                )
            points.append(LinkagePoint(tuple(values)))
        return tuple(points)

    def _build_task(self) -> LinkageTaskRecord:
        points = self._points_from_table()
        axes = self._axes()
        current = self._validate_live_axes(axes)
        values = dict(
            name=self.task_name.text(),
            ni_device_id=str(self.ni_source.currentData() or ""),
            counter=str(self.counter.currentData() or self.counter.currentText()),
            terminal=self.terminal.currentText(),
            rising=self.edge.currentIndex() == 0,
            scan_mode=self.scan_mode.currentData(),
            dispatch_mode=self.dispatch_mode.currentData(),
            axes=axes,
            points=points,
            source_kinds=tuple((device_id, current[device_id].kind)
                               for device_id in dict.fromkeys(axis.device_id for axis in axes)),
            ssg_mode=self.ssg_mode.currentData(),
            ssg_dwell_s=self.ssg_dwell.value(),
        )
        if self._current_task_id:
            existing = next(
                (item for item in self.task_repository.list() if item.id == self._current_task_id),
                None,
            )
            if existing is not None:
                return existing.revised(**values)
        return LinkageTaskRecord.create(**values)

    def _save_task(self) -> None:
        try:
            task = self.task_repository.save(self._build_task())
        except Exception as exc:
            QMessageBox.warning(self, "无法保存联动任务", str(exc))
            return
        self._current_task_id = task.id
        self._reload_tasks(select_id=task.id)
        self.statusBar().showMessage(f"任务“{task.name}”已保存", 5000)

    def _reload_tasks(self, *, select_id: str | None = None) -> None:
        try:
            tasks = self.task_repository.list()
            active_task_id = self.task_repository.active_task_id()
        except RuntimeError as exc:
            QMessageBox.warning(self, "无法读取联动任务", str(exc))
            tasks = []
            active_task_id = None
        self.task_selector.clear()
        if not tasks:
            self.task_selector.addItem("尚未保存任务", None)
        else:
            for task in tasks:
                self.task_selector.addItem(task.name, task.id)
        selected_task_id = select_id or active_task_id
        if selected_task_id:
            index = self.task_selector.findData(selected_task_id)
            if index >= 0:
                self.task_selector.setCurrentIndex(index)

    def _load_selected_task(
        self,
        _checked: bool = False,
        *,
        show_message: bool = True,
    ) -> None:
        task_id = self.task_selector.currentData()
        if not task_id:
            return
        try:
            task = next(item for item in self.task_repository.list() if item.id == task_id)
        except (RuntimeError, StopIteration) as exc:
            QMessageBox.warning(self, "无法加载联动任务", str(exc))
            return
        try:
            self._validate_saved_bindings(task)
        except ValueError as exc:
            QMessageBox.warning(self, "设备绑定不可用", str(exc))
            return
        self._current_task_id = task.id
        self.task_name.setText(task.name)
        self.ni_source.setCurrentIndex(self.ni_source.findData(task.ni_device_id))
        self.counter.setCurrentIndex(self.counter.findData(task.counter))
        self.terminal.setCurrentText(task.terminal)
        self.edge.setCurrentIndex(0 if task.rising else 1)
        self.scan_mode.setCurrentIndex(self.scan_mode.findData(task.scan_mode))
        self.dispatch_mode.setCurrentIndex(self.dispatch_mode.findData(task.dispatch_mode))
        self.ssg_mode.setCurrentIndex(self.ssg_mode.findData(task.ssg_mode))
        self.ssg_dwell.setValue(task.ssg_dwell_s)
        for editor in self.axis_editors:
            editor.deleteLater()
        self.axis_editors.clear()
        for axis in task.axes:
            self._add_axis(axis)
        self._table_axes = task.axes
        self._table_mode = task.scan_mode
        self._fill_point_table(task.axes, task.points)
        self._configuration_changed()
        self.task_repository.set_active_task(task.id)
        if show_message:
            self.statusBar().showMessage(
                f"已恢复实验“{task.name}”：{len(task.axes)} 个设备参数轴、"
                f"{len(task.points)} 个联合点",
                6000,
            )

    def _validate_saved_bindings(self, task: LinkageTaskRecord) -> None:
        current = {item.id: item for item in self.repository.list()}
        referenced_ids = list(
            dict.fromkeys([task.ni_device_id, *(axis.device_id for axis in task.axes)])
        )
        missing_ids = [device_id for device_id in referenced_ids if device_id not in current]
        if missing_ids:
            raise ValueError(
                "保存的实验引用了已删除的设备："
                + "、".join(missing_ids)
                + "。不会自动改绑到其他设备，请重新选择并保存任务。"
            )
        if current[task.ni_device_id].kind is not DeviceKind.NI_DAQ:
            raise ValueError("保存的 NI 绑定当前已不是 NI 采集卡，请重新选择并保存任务。")
        expected = dict(task.source_kinds) if task.source_kinds else {
            axis.device_id: DeviceKind.DG4000 for axis in task.axes
        }
        invalid = list(dict.fromkeys(
            current[axis.device_id].name for axis in task.axes
            if current[axis.device_id].kind is not expected.get(axis.device_id)
        ))
        if invalid:
            raise ValueError(
                "以下保存绑定的信号源类型已改变：" + "、".join(invalid)
                + "。旧 DG4000 幅度不会自动改为射频功率，请重新选择并保存。"
            )
        for axis in task.axes:
            validate_source_axis(axis, current[axis.device_id].kind)
        self._validate_live_axes(task.axes)

    def _new_task(self) -> None:
        if self.controller is not None:
            QMessageBox.information(self, "任务正在运行", "请先停止当前联动任务。")
            return
        self._current_task_id = None
        self.task_name.setText("跨设备扫描 1")
        self.ssg_mode.setCurrentIndex(self.ssg_mode.findData(SsgLinkageMode.SOFTWARE))
        self.ssg_dwell.setValue(0.01)
        for editor in self.axis_editors:
            editor.deleteLater()
        self.axis_editors.clear()
        self._table_axes = ()
        self._table_mode = None
        self.point_table.setRowCount(0)
        if self.generators:
            self._add_axis()
            self._generate_table(show_message=False)

    def _delete_task(self) -> None:
        task_id = self.task_selector.currentData()
        if not task_id:
            return
        if QMessageBox.question(
            self,
            "删除联动任务",
            "确认删除当前保存的联动任务吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) is not QMessageBox.StandardButton.Yes:
            return
        try:
            self.task_repository.delete(str(task_id))
        except RuntimeError as exc:
            QMessageBox.warning(self, "删除失败", str(exc))
            return
        if self._current_task_id == task_id:
            self._current_task_id = None
        self._reload_tasks()
        if self.task_selector.currentData():
            self._load_selected_task(show_message=False)

    def _required_records(self) -> tuple[DeviceRecord, ...]:
        axes = self._axes()
        ids = list(dict.fromkeys([str(self.ni_source.currentData() or "")] + [axis.device_id for axis in axes]))
        by_id = {item.id: item for item in self.repository.list()}
        missing = [device_id for device_id in ids if device_id not in by_id]
        if missing:
            raise ValueError("任务引用的设备记录已经不存在，请重新选择扫描轴")
        if by_id[ids[0]].kind is not DeviceKind.NI_DAQ:
            raise ValueError("触发设备当前已不是 NI 采集卡，请重新选择")
        original_ni = next((item for item in self.ni_devices if item.id == ids[0]), None)
        if original_ni is None or original_ni.resource != by_id[ids[0]].resource:
            raise ValueError("NI 触发设备资源已改变，请重新打开窗口并确认绑定")
        self._validate_live_axes(axes)
        return tuple(by_id[device_id] for device_id in ids)

    def _connect_required_devices(self) -> None:
        if self.controller is not None or self._call_tasks:
            return
        try:
            records = self._required_records()
        except Exception as exc:
            QMessageBox.warning(self, "连接失败", str(exc))
            return
        self._connect_cancel.clear()
        self._connect_error = None
        self._pending_stop = False
        def connect_records():
            for record in records:
                if self._connect_cancel.is_set():
                    return
                if not self.connection_manager.is_connected(record):
                    self.connection_manager.connect(record)
        task = CallTask(connect_records)
        self._connect_task = task
        self._call_tasks.add(task)
        self._set_editing_enabled(False)
        self.connect_button.setText("连接中…")
        task.signals.failed.connect(lambda message, item=task: self._connect_failed(message, item))
        task.signals.finished.connect(lambda item=task: self._connect_finished(item, len(records)))
        try:
            self.thread_pool.start(task)
        except Exception as exc:
            self._connect_failed(f"后台连接任务未能提交：{exc}", task)
            self._connect_finished(task, len(records))

    def _connect_failed(self, message: str, task: CallTask) -> None:
        if task is self._connect_task:
            self._connect_error = message

    def _connect_finished(self, task: CallTask, count: int) -> None:
        self._call_tasks.discard(task)
        if task is not self._connect_task:
            return
        self._connect_task = None
        self.connect_button.setText("连接参与设备")
        error, self._connect_error = self._connect_error, None
        self._set_editing_enabled(True)
        if self._pending_stop:
            self._pending_stop = False
            self._finish_stopped()
        elif error:
            QMessageBox.warning(self, "连接失败", error)
        else:
            self.statusBar().showMessage(f"已连接 {count} 台参与设备", 5000)

    def _start(self) -> None:
        if self.controller is not None or self._call_tasks:
            return
        try:
            points = self._points_from_table()
            records = self._required_records()
            disconnected = [
                record.name
                for record in records
                if not self.connection_manager.is_connected(record)
            ]
            if disconnected:
                raise ValueError("以下设备尚未连接：" + "、".join(disconnected))
            ni_id = str(self.ni_source.currentData() or "")
            ni_record = next(item for item in records if item.id == ni_id)
            owner = f"linkage:{self._current_task_id or id(self)}"
            source_records = [item for item in records if item.kind is not DeviceKind.NI_DAQ]
            writers = {}
            native_participants = {}
            for record in source_records:
                device_points = tuple(point.grouped()[record.id] for point in points)
                if record.kind is DeviceKind.DG4000:
                    writers[record.id] = self.connection_manager.dg_driver_for(record)
                elif record.kind is DeviceKind.VAUNIX_LMS:
                    from ..vaunix_linkage import VaunixLinkageWriter
                    writers[record.id] = VaunixLinkageWriter(
                        self.connection_manager.vaunix_device_for(record)
                    )
                elif record.kind is DeviceKind.SIGLENT_SSG:
                    from ..siglent_linkage import SsgNativeLinkageParticipant, SsgSoftwareLinkageWriter
                    session = self.connection_manager.session_for(record)
                    if self.ssg_mode.currentData() is SsgLinkageMode.NATIVE_EXTERNAL:
                        if is_virtual_resource(ni_record.resource) and not is_virtual_resource(record.resource):
                            raise ValueError("鼎阳原生外触发不能使用虚拟 NI 搭配真实鼎阳；请连接公共物理触发源")
                        if not 2 <= len(points) <= 500:
                            raise ValueError("鼎阳原生 LIST 参与时，联合点表必须为 2 至 500 行")
                        native_participants[record.id] = SsgNativeLinkageParticipant(
                            session, device_points, rising=self.edge.currentIndex() == 0,
                            dwell_s=self.ssg_dwell.value(),
                            reconnect_for_stop=lambda record=record: self.connection_manager.connect(
                                record, owner=owner
                            ),
                        )
                    else:
                        writers[record.id] = SsgSoftwareLinkageWriter(session, device_points)
                else:
                    raise ValueError(f"设备“{record.name}”不能作为跨设备联动信号源")
            participants = {**writers, **native_participants}
            for point_index, point in enumerate(points, start=1):
                for device_id, values in point.grouped().items():
                    try:
                        participants[device_id].validate_step_values(values)
                    except ValueError as exc:
                        raise ValueError(
                            f"第 {point_index} 个跨设备联合点：{exc}"
                        ) from exc
            self.connection_manager.acquire_devices(owner, tuple(item.id for item in records))
            self._lease_owner = owner
            counter = self.connection_manager.counter_for(
                ni_record,
                counter=str(self.counter.currentData()),
                terminal=self.terminal.currentText(),
                rising=self.edge.currentIndex() == 0,
            )
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            log_path = self.task_repository.data_dir / "run_logs" / f"linkage_{timestamp}.csv"
            controller = MultiDeviceTriggerController(
                counter,
                writers,
                points,
                device_order=tuple(record.id for record in source_records if record.id in writers),
                dispatch_mode=self.dispatch_mode.currentData(),
                logger=LinkageRunLogger(log_path),
                **({"native_participants": native_participants} if native_participants else {}),
            )
            self.connection_manager.watch_output_task(owner, source_records, controller)
            controller.add_callback(self.bridge.begin_session())
            self.controller = controller
            self._native_participants = native_participants
            self._native_records = tuple(record for record in source_records if record.id in native_participants)
            self._prepare_runtime_table(source_records)
            self.log_path.setText(
                "原生参与者实际接收未知；仅监测 NI，本任务无软件联合点 CSV"
                if native_participants and not writers else
                (f"软件下发日志（不含鼎阳原生接收确认）：{log_path}"
                 if native_participants else f"运行日志：{log_path}")
            )
            self._last_error = None
            self._last_logged_applied = 0
            self.run_log.clear()
            if native_participants:
                self.run_log.appendPlainText(
                    "鼎阳按自身 EXT 输入执行原生 LIST；NI 计数只表示公共输入边沿，"
                    "不能确认鼎阳实际接收、积压或当前点。全部布防完成前请暂停外部触发。"
                )
            self._set_editing_enabled(False)
            self._pending_stop = False
            self._start_error = None
            self.start_button.setText("启动中…")
            self.stop_button.setEnabled(True)
            task = CallTask(controller.start)
            self._start_task = task
            self._call_tasks.add(task)
            task.signals.failed.connect(lambda message, item=task: self._start_failed(message, item))
            task.signals.finished.connect(lambda item=task: self._start_finished(item))
            try:
                self.thread_pool.start(task)
            except Exception:
                self._call_tasks.discard(task)
                self._start_task = None
                raise
        except Exception as exc:
            self.bridge.flush()
            if self._lease_owner:
                self.connection_manager.release_devices(self._lease_owner)
                self._lease_owner = None
            self.controller = None
            self._set_editing_enabled(True)
            self.start_button.setText("启动联动")
            self.stop_button.setEnabled(False)
            QMessageBox.warning(self, "无法启动跨设备联动", str(exc))

    def _start_failed(self, message: str, task: CallTask | None = None) -> None:
        # Keep the controller and leases until CallTask has fully returned.
        if task is None or task is self._start_task:
            self._start_error = message

    def _start_finished(self, task: CallTask) -> None:
        self._call_tasks.discard(task)
        if task is not self._start_task:
            return
        self._start_task = None
        # Native scans can change output without a software point completing.
        # Invalidate old optional readback snapshots using memory-only state.
        for record in self._native_records:
            self.connection_manager.note_output_change(record)
        self.bridge.flush()
        error, self._start_error = self._start_error, None
        if self._pending_stop:
            self._stop()
        elif error:
            if self.controller is not None and not self.controller.is_quiescent:
                self.start_button.setText("等待停止")
                self.start_button.setEnabled(False)
                self.stop_button.setText("重试停止")
                self.stop_button.setToolTip("显式重试会为待停止的鼎阳原生参与者重连核对身份，仅关闭扫描并确认 OFF；不恢复扫描或参数。")
                self.stop_button.setEnabled(True)
                self.run_log.appendPlainText("启动失败且原生扫描停止尚未确认；设备占用保留，请显式重试停止。")
            else:
                self._finish_stopped()
        if error:
            QMessageBox.warning(self, "无法启动跨设备联动", error)

    def _stop(self) -> None:
        if self._stop_task is not None:
            return
        if self._call_tasks:
            self._pending_stop = True
            if self._connect_task is not None:
                self._connect_cancel.set()
            if self.controller is not None:
                self.controller.request_stop()
            self.start_button.setText("正在取消…")
            self.stop_button.setEnabled(False)
            self.statusBar().showMessage("已请求停止，正在等待设备预检查返回…")
            return
        self._pending_stop = False
        controller = self.controller
        if controller is None:
            self._finish_stopped()
            return
        controller.request_stop()
        self._stop_error = None
        self.start_button.setText("停止中…")
        self.stop_button.setEnabled(False)
        task = CallTask(controller.stop)
        self._stop_task = task
        self._call_tasks.add(task)
        task.signals.failed.connect(lambda message, item=task: self._stop_failed(message, item))
        task.signals.finished.connect(lambda item=task: self._stop_finished(item))
        try:
            self.thread_pool.start(task)
        except Exception as exc:
            self._stop_failed(f"后台任务未能提交：{exc}", task)
            self._stop_finished(task)

    def _stop_failed(self, message: str, task: CallTask | None = None) -> None:
        if task is None or task is self._stop_task:
            self._stop_error = message

    def _stop_finished(self, task: CallTask) -> None:
        self._call_tasks.discard(task)
        if task is not self._stop_task:
            return
        self._stop_task = None
        self.bridge.flush()
        error, self._stop_error = self._stop_error, None
        if error or (self.controller is not None and not self.controller.is_quiescent):
            message = error or "后台操作尚未结束，请等待后重试停止。"
            self.run_log.appendPlainText(f"停止任务时发生错误：{message}")
            self.statusBar().showMessage(message, 15000)
            self.start_button.setText("等待停止")
            self.start_button.setEnabled(False)
            self.stop_button.setText("重试停止")
            self.stop_button.setToolTip("显式重试会为待停止的鼎阳原生参与者重连核对身份，仅关闭扫描并确认 OFF；不恢复扫描或参数。")
            self.stop_button.setEnabled(True)
            return
        self._finish_stopped()

    def _finish_stopped(self) -> None:
        self.controller = None
        if self._lease_owner:
            self.connection_manager.release_devices(self._lease_owner)
            self._lease_owner = None
        self._set_editing_enabled(True)
        self.metrics["state"].setText("停止")
        self.start_button.setText("启动联动")
        self.stop_button.setEnabled(False)
        self.stop_button.setText("停止")
        self.stop_button.setToolTip("")
        for device_id in self._native_participants:
            row = self._runtime_rows.get(device_id)
            if row:
                row.status.setText("原生扫描已停止；实际接收/积压/点位未知")
                row.elapsed.setText("—")

    def _prepare_runtime_table(self, records: list[DeviceRecord]) -> None:
        self.device_table.setRowCount(len(records))
        self._runtime_rows.clear()
        for row, record in enumerate(records):
            name = QTableWidgetItem(record.name)
            status = QTableWidgetItem(
                "原生准备中；实际接收/积压/点位未知"
                if record.id in self._native_participants else "等待首个触发"
            )
            elapsed = QTableWidgetItem("—")
            for column, item in enumerate((name, status, elapsed)):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.device_table.setItem(row, column, item)
            self._runtime_rows[record.id] = RuntimeDeviceRow(name, status, elapsed)

    def _controller_updated(self, snapshot: LinkageSnapshot) -> None:
        labels = {
            ControllerState.STARTING: "启动中",
            ControllerState.RUNNING: "运行中",
            ControllerState.STOPPING: "停止中",
            ControllerState.STOPPED: "停止",
            ControllerState.ERROR: "错误",
        }
        self.metrics["state"].setText(labels[snapshot.state])
        self.metrics["received"].setText(str(snapshot.received_count))
        native_only = bool(self._native_participants) and not getattr(self.controller, "writers", None)
        self.metrics["applied"].setText("—" if native_only else str(snapshot.applied_count))
        self.metrics["backlog"].setText("—" if native_only else str(snapshot.backlog))
        self.metrics["current"].setText(
            "—" if native_only or snapshot.current_index is None else f"P{snapshot.current_index}"
        )
        if self._native_participants:
            self.metric_captions["applied"].setText("软件处理（无）" if native_only else "软件已处理点")
            self.metric_captions["backlog"].setText("软件积压（无）" if native_only else "软件积压")
            self.metric_captions["current"].setText("软件当前点（无）" if native_only else "软件当前点")
        self.metrics["skew"].setText(
            "—" if not snapshot.device_results else f"{snapshot.skew_ms:.3f} ms"
        )
        for result in snapshot.device_results:
            row = self._runtime_rows.get(result.device_id)
            if row:
                row.status.setText("完成" if result.success else f"失败：{result.error}")
                row.elapsed.setText(f"{result.elapsed_ms:.3f} ms")
        for device_id, participant in self._native_participants.items():
            row = self._runtime_rows.get(device_id)
            if row is None:
                continue
            if snapshot.state is ControllerState.STARTING:
                prefix = "原生准备中"
            elif participant.requires_stop:
                prefix = "原生已布防" if snapshot.state is ControllerState.RUNNING else "原生停止待确认"
            else:
                prefix = "原生未启用或已停止"
            row.status.setText(f"{prefix}；实际接收/积压/点位未知")
            row.elapsed.setText("—")
        if snapshot.applied_count > self._last_logged_applied:
            self._last_logged_applied = snapshot.applied_count
            self.run_log.appendPlainText(
                f"NI 触发 {snapshot.applied_count} → 软件 P{snapshot.current_index} 完成；"
                f"积压 {snapshot.backlog}；设备偏差 {snapshot.skew_ms:.3f} ms"
                + ("；鼎阳原生实际接收未知" if self._native_participants else "")
            )
        running = snapshot.state in {ControllerState.STARTING, ControllerState.RUNNING}
        if self._stop_task is not None or (self._pending_stop and self._call_tasks):
            self.start_button.setText("停止中…" if self._stop_task is not None else "正在取消…")
            self.stop_button.setEnabled(False)
        else:
            self.start_button.setText("运行中" if running else "启动联动")
            self.stop_button.setEnabled(
                running or snapshot.state in {ControllerState.ERROR, ControllerState.STOPPING}
            )
        if snapshot.error and snapshot.error != self._last_error:
            self._last_error = snapshot.error
            self.run_log.appendPlainText(snapshot.error)
            if (self._lease_owner and not self._call_tasks and self.controller is not None
                    and self.controller.is_quiescent):
                self.connection_manager.release_devices(self._lease_owner)
                self._lease_owner = None

    def _emit_virtual(self, count: int) -> None:
        ni_id = self.ni_source.currentData()
        record = next((item for item in self.ni_devices if item.id == ni_id), None)
        if record is None or not is_virtual_resource(record.resource):
            QMessageBox.information(self, "不是虚拟 NI", "当前 NI 触发源不是内置虚拟设备。")
            return
        if self.controller is not None and self.controller.snapshot().state is ControllerState.RUNNING:
            for device_id, participant in self._native_participants.items():
                source = next((item for item in self.generators if item.id == device_id), None)
                if source is None or not is_virtual_resource(source.resource):
                    QMessageBox.information(self, "无法模拟公共触发", "虚拟公共触发只适用于全部为内置模拟器的原生参与者。")
                    return
            try:
                for participant in self._native_participants.values():
                    # Simulate shared wiring only. Burst edges are not delayed
                    # or replayed to make a native scan appear to accept them.
                    participant.emit_virtual_trigger(count)
            except Exception as exc:
                QMessageBox.warning(self, "虚拟触发失败", str(exc))
                return
        self.connection_manager.virtual_lab.emit_trigger(record.resource, count)

    def _set_editing_enabled(self, enabled: bool) -> None:
        for control in (
            self.task_selector,
            self.load_task_button,
            self.new_task_button,
            self.save_task_button,
            self.delete_task_button,
            self.task_name,
            self.ni_source,
            self.counter,
            self.terminal,
            self.edge,
            self.scan_mode,
            self.dispatch_mode,
            self.ssg_mode,
            self.ssg_dwell,
            self.connect_button,
            self.add_axis_button,
            self.generate_button,
        ):
            control.setEnabled(enabled)
        for editor in self.axis_editors:
            editor.setEnabled(enabled)
        self.point_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
            if enabled
            else QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.start_button.setEnabled(enabled)
        self.ni_source.setEnabled(enabled and bool(self.ni_devices))
        self.ssg_dwell.setEnabled(enabled and self.ssg_mode.currentData() is SsgLinkageMode.NATIVE_EXTERNAL)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._call_tasks or (self.controller is not None and not self.controller.is_quiescent):
            self._stop()
            self.statusBar().showMessage("后台任务尚未停止，请等待完成后再关闭窗口。", 8000)
            event.ignore()
            return
        if self.controller is not None:
            try:
                self.controller.stop(timeout_seconds=0)
            except Exception as exc:
                self.statusBar().showMessage(str(exc), 15000)
                event.ignore()
                return
        self.bridge.flush()
        self._finish_stopped()
        self.bridge.close()
        super().closeEvent(event)
