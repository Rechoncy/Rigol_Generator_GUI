from __future__ import annotations

from datetime import datetime
from itertools import product
from typing import Any, Callable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..connections import DeviceBusyError, DeviceConnectionManager
from ..drivers.dg4000 import StepParameter, StepValue
from ..drivers.ni_counter import NIDaqEdgeCounter
from ..drivers.vaunix_lms import (
    PAUSE_SEQUENCE,
    REPEAT_SEQUENCE,
    RESUME_SEQUENCE,
    START_SEQUENCE,
    STOP_SEQUENCE,
    VIRTUAL_VAUNIX_RESOURCE,
    VaunixCapabilities,
    VaunixLmsDevice,
    VaunixOutputSnapshot,
    VaunixSequenceElement,
    VaunixSnapshot,
    VirtualVaunixLmsDevice,
    format_dll_version,
    validate_power_dbm,
)
from ..models import DeviceKind, DeviceRecord
from ..repository import DeviceParameterRepository
from ..scan import ScanPoint
from ..sequence import MAX_SCAN_POINTS, linear_scan_points
from ..trigger import ControllerState, TriggerSnapshot, TriggerStepController
from ..units import roundtrip_number
from ..virtual_lab import is_virtual_resource
from .device_windows import (
    BaseDeviceWindow,
    UnitValueWidget,
    metric_card,
    page_shell,
    panel,
    unit_value_spin,
)
from .snapshot_bridge import SnapshotBridge
from .widgets import CallTask


class VaunixTriggerBridge(SnapshotBridge):
    pass


class VaunixLmsWindow(BaseDeviceWindow):
    """Complete LMS/BLX control surface plus lossless NI-triggered stepping."""

    def __init__(
        self,
        record: DeviceRecord,
        ni_devices: list[DeviceRecord],
        connection_manager: DeviceConnectionManager | None = None,
        device_provider: Callable[[], list[DeviceRecord]] | None = None,
        parameter_repository: DeviceParameterRepository | None = None,
    ) -> None:
        self.connection_manager = connection_manager
        self.device_provider = device_provider
        self.ni_devices = ni_devices
        self.device = (
            connection_manager.vaunix_device_for(record)
            if connection_manager is not None
            else (
                VirtualVaunixLmsDevice(record.resource)
                if is_virtual_resource(record.resource)
                else VaunixLmsDevice(record.resource)
            )
        )
        self._ui_connected = False
        self._ui_connection_generation: int | None = None
        self._output_readback_pending = False
        self._output_readback_token = 0
        self.output_readback_values: dict[str, QLabel] = {}
        self._ni_signature: tuple[tuple[str, str, str, bool], ...] = ()
        self._lease_owner = f"vaunix-trigger:{record.id}"
        self.controller: TriggerStepController | None = None
        self._trigger_stop_in_progress = False
        self._trigger_start_task: CallTask | None = None
        self._trigger_start_error: str | None = None
        self._trigger_pending_stop = False
        self.trigger_bridge = VaunixTriggerBridge()
        self.trigger_bridge.updated.connect(self._trigger_updated)
        self._last_trigger_error: str | None = None
        navigation = [
            ("总览", self._overview_page),
            ("射频输出", self._output_page),
            ("扫频与 Chirp", self._sweep_page),
            ("脉冲调制", self._pulse_page),
            ("BLX 序列", self._sequence_page),
            ("NI 触发步进", self._trigger_page),
            ("状态与存储", self._system_page),
        ]
        super().__init__(record, navigation, parameter_repository)
        self.trigger_bridge.setParent(self)
        if self.device.is_connected:
            self._connected(self.device.identity)

    def _require_manual_access(self) -> None:
        if self.connection_manager is not None:
            self.connection_manager.assert_manual_access(self.record)
        owner = (self.connection_manager.lease_owner(self.record.id)
                 if self.connection_manager is not None else None)
        busy = self.controller is not None and (
            self._trigger_start_task is not None
            or self.controller.snapshot().state in {
                ControllerState.STARTING, ControllerState.RUNNING, ControllerState.STOPPING,
            }
            or not self.controller.is_quiescent
        )
        if owner is not None or busy:
            raise DeviceBusyError("Vaunix 正在执行触发任务，请先停止任务再执行手动操作。")

    def submit(self, operation: Callable[[], Any], *, on_success: Callable[[Any], None] | None = None,
               on_error: Callable[[str], None] | None = None, allow_during_scan: bool = False) -> None:
        if allow_during_scan:
            super().submit(operation, on_success=on_success, on_error=on_error)
            return
        try:
            self._require_manual_access()
        except DeviceBusyError as exc:
            (on_error or self._show_hardware_error)(str(exc))
            return

        def guarded_operation() -> Any:
            with self.device.transaction():
                self._require_manual_access()
                return operation()

        super().submit(guarded_operation, on_success=on_success, on_error=on_error)

    def _overview_page(self) -> QWidget:
        page, layout = page_shell(
            "Vaunix LMS/BLX 设备总览",
            "通过 Windows vnx_fmsynth.dll 控制 Lab Brick 信号源；资源以序列号稳定识别。",
        )
        metrics = QHBoxLayout()
        self.overview_metrics: dict[str, QLabel] = {}
        for key, caption, value, color in (
            ("connection", "连接状态", "未连接", "#f0b95d"),
            ("frequency", "当前频率", "—", "#55d9cc"),
            ("power", "当前功率", "—", "#f08ab8"),
            ("rf", "RF 输出", "未知", "#8f7cff"),
        ):
            card, number = metric_card(caption, value, color)
            self.overview_metrics[key] = number
            metrics.addWidget(card)
        layout.addLayout(metrics)
        connection, connection_layout = panel("设备连接")
        form = QFormLayout()
        form.addRow("资源地址", QLabel(self.record.resource))
        form.addRow("连接方式", QLabel(self.record.connection or "Vaunix USB DLL"))
        self.identity_label = QLabel("连接后读取型号、序列号、DLL 和量程")
        self.identity_label.setWordWrap(True)
        form.addRow("设备身份", self.identity_label)
        connection_layout.addLayout(form)
        actions = QHBoxLayout()
        self.connect_button = QPushButton("连接设备")
        self.connect_button.setObjectName("PrimaryButton")
        self.disconnect_button = QPushButton("断开")
        self.disconnect_button.setEnabled(False)
        self.read_all_button = QPushButton("读取全部参数")
        self.read_all_button.setEnabled(False)
        self.connect_button.clicked.connect(self._connect)
        self.disconnect_button.clicked.connect(self._disconnect)
        self.read_all_button.clicked.connect(self._refresh_all)
        self.overview_readback_button = QPushButton("回读频率 / 幅度")
        self.overview_readback_button.setEnabled(False)
        self.overview_readback_button.clicked.connect(self._show_output_readback)
        actions.addWidget(self.connect_button)
        actions.addWidget(self.disconnect_button)
        actions.addWidget(self.read_all_button)
        actions.addWidget(self.overview_readback_button)
        actions.addStretch()
        connection_layout.addLayout(actions)
        layout.addWidget(connection)
        capabilities, capabilities_layout = panel("检测到的能力")
        self.capability_label = QLabel("尚未连接")
        self.capability_label.setWordWrap(True)
        self.capability_label.setObjectName("Muted")
        capabilities_layout.addWidget(self.capability_label)
        layout.addWidget(capabilities)
        layout.addStretch()
        return page

    def _output_page(self) -> QWidget:
        page, layout = page_shell(
            "射频输出",
            "设置当前频率、绝对输出功率、RF 开关以及内部/外部参考源。",
        )
        output, output_layout = panel("LMS/BLX 输出参数")
        form = QFormLayout()
        self.output_frequency = unit_value_spin(
            5e9, 1e3, 42e9, "frequency", "GHz"
        )
        self.output_power = QDoubleSpinBox()
        self.output_power.setDecimals(2)
        self.output_power.setSingleStep(0.5)
        self.output_power.setRange(-100.0, 30.0)
        self.output_power.setSuffix(" dBm")
        self.output_rf = QCheckBox("启用 RF 输出")
        self.output_reference = QComboBox()
        self.output_reference.addItem("内部参考", True)
        self.output_reference.addItem("外部参考", False)
        form.addRow("输出频率", self.output_frequency)
        form.addRow("输出功率/幅度", self.output_power)
        form.addRow("RF 状态", self.output_rf)
        form.addRow("参考源", self.output_reference)
        output_layout.addLayout(form)
        apply_button = QPushButton("应用输出参数")
        apply_button.setObjectName("PrimaryButton")
        apply_button.clicked.connect(self._apply_output)
        output_layout.addWidget(apply_button, alignment=Qt.AlignmentFlag.AlignLeft)
        note = QLabel(
            "功率使用绝对 dBm；DLL 以 0.25 dB/单位编码，仪器实际分辨率为 0.5 dB。"
            "恢复软件参数不会自动打开 RF，"
            "只有点击应用才写入设备。"
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        output_layout.addWidget(note)
        layout.addWidget(output)
        layout.addWidget(self._output_readback_panel())
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setObjectName("VaunixOutputScroll")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    def _output_readback_panel(self) -> QWidget:
        monitor, monitor_layout = panel("仪器频率与幅度回读 · 手动快照")
        self.output_readback_widget = monitor
        actions = QHBoxLayout()
        self.output_readback_button = QPushButton("回读频率 / 幅度")
        self.output_readback_button.setEnabled(False)
        self.output_readback_button.clicked.connect(self._refresh_output_readback)
        self.output_readback_status = QLabel("未读取 · 请先连接设备")
        self.output_readback_status.setTextFormat(Qt.TextFormat.PlainText)
        self.output_readback_status.setWordWrap(True)
        self.output_readback_status.setObjectName("Muted")
        actions.addWidget(self.output_readback_button)
        actions.addWidget(self.output_readback_status, 1)
        monitor_layout.addLayout(actions)
        cards = QHBoxLayout()
        for name, caption, color in (
            ("frequency", "频率", "#55d9cc"),
            ("amplitude", "幅度 / 功率", "#f08ab8"),
            ("output", "RF 输出", "#8f7cff"),
        ):
            card, value = metric_card(caption, "未知", color)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setMinimumHeight(value.fontMetrics().height() + 4)
            self.output_readback_values[name] = value
            cards.addWidget(card)
        monitor_layout.addLayout(cards)
        note = QLabel(
            "回读 DLL 的频率、绝对 dBm 功率与 RF 开关，不覆盖上方编辑值。"
            "原生扫频时频率可随设备更新；各值为顺序查询快照，非同步电气测量。"
            "RF 关闭时数值仅为配置。"
        )
        note.setWordWrap(True)
        note.setObjectName("Muted")
        monitor_layout.addWidget(note)
        return monitor

    def _show_output_readback(self) -> None:
        self._select_page(1)
        page = self.pages.widget(1)
        if isinstance(page, QScrollArea):
            page.ensureWidgetVisible(self.output_readback_widget, 20, 20)
        self._refresh_output_readback()

    def _update_output_readback_buttons(self) -> None:
        enabled = self.device.is_connected and not self._output_readback_pending
        for button in (self.overview_readback_button, self.output_readback_button):
            button.setEnabled(enabled)
            button.setText("回读中…" if self._output_readback_pending else "回读频率 / 幅度")

    def _invalidate_output_readback(self, message: str) -> None:
        self._output_readback_token += 1
        for value in self.output_readback_values.values():
            value.setText("未知")
            value.setToolTip("")
        self.output_readback_status.setText(message)

    def _clear_output_metrics(self) -> None:
        for name in ("frequency", "power", "rf"):
            self.overview_metrics[name].setText("未知")

    def _refresh_output_readback(self) -> None:
        if self._output_readback_pending:
            return
        if not self.device.is_connected:
            self._invalidate_output_readback("未连接 · 回读值未知")
            self._clear_output_metrics()
            self._update_output_readback_buttons()
            return
        self._invalidate_output_readback("正在回读频率、幅度与 RF 状态…")
        token = self._output_readback_token
        generation = self.device.connection_generation
        # This request already targets the current connection. A main-window
        # timer observing that same generation must not invalidate the new read
        # merely because it has not yet observed an external reconnect itself.
        self._ui_connection_generation = generation
        self._output_readback_pending = True
        self._update_output_readback_buttons()

        def read_parameters():
            with self.device.transaction():
                if not self.device.is_connected or self.device.connection_generation != generation:
                    raise RuntimeError("连接已变化，已取消旧连接的回读，请重新回读")
                return self.device.read_output_parameters(), datetime.now().astimezone()

        def completed(result):
            self._output_readback_pending = False
            self._update_output_readback_buttons()
            reading, read_at = result
            if token != self._output_readback_token:
                return
            if (not self.device.is_connected or self.device.connection_generation != generation
                    or reading.connection_generation != generation):
                self._invalidate_output_readback("连接已变化 · 回读值未知，请重新回读")
                self._clear_output_metrics()
                return
            frequency = f"{roundtrip_number(reading.frequency_hz)} Hz"
            amplitude = f"{roundtrip_number(reading.power_dbm)} dBm"
            self.output_readback_values["frequency"].setText(frequency)
            self.output_readback_values["amplitude"].setText(amplitude)
            self.output_readback_values["output"].setText("开启" if reading.rf_on else "关闭（未输出）")
            self.output_readback_values["frequency"].setToolTip("DLL 当前频率设置；非瞬时电气实测值")
            self.output_readback_values["amplitude"].setToolTip("DLL 当前绝对功率设置（GetAbsPowerLevel），未切换单位或修改参数")
            self.overview_metrics["frequency"].setText(frequency)
            self.overview_metrics["power"].setText(amplitude)
            self.overview_metrics["rf"].setText("已打开" if reading.rf_on else "关闭")
            self.output_readback_status.setText(
                f"回读时间：{read_at:%Y-%m-%d %H:%M:%S}（本地） · 点击可刷新"
            )

        def failed(message: str) -> None:
            self._output_readback_pending = False
            self._update_output_readback_buttons()
            if token != self._output_readback_token:
                return
            self._invalidate_output_readback(f"回读失败 · 频率、幅度和输出状态未知：{message}")
            self._clear_output_metrics()
            self.statusBar().showMessage(f"Vaunix 参数回读失败：{message}", 10000)

        self.submit(read_parameters, on_success=completed, on_error=failed, allow_during_scan=True)

    def _sweep_page(self) -> QWidget:
        page, layout = page_shell(
            "扫频与 Chirp",
            "覆盖 LMS 时间扫频、BLX 步进驻留、双向/重复模式、外部触发及支持型号的 Chirp。",
        )
        upper = QHBoxLayout()
        sweep, sweep_layout = panel("频率扫频")
        form = QFormLayout()
        self.sweep_start = unit_value_spin(5e9, 1e3, 42e9, "frequency", "GHz")
        self.sweep_end = unit_value_spin(6e9, 1e3, 42e9, "frequency", "GHz")
        self.sweep_step = unit_value_spin(100e6, 10, 42e9, "frequency", "MHz")
        self.sweep_time = unit_value_spin(1.0, 0.001, 3600, "time", "s")
        self.sweep_dwell = unit_value_spin(0.01, 0.01, 3600, "time", "ms")
        self.sweep_idle = unit_value_spin(0.0, 0.0, 3600, "time", "ms")
        self.sweep_direction = QComboBox()
        self.sweep_direction.addItem("向上", True)
        self.sweep_direction.addItem("向下", False)
        self.sweep_repeat = QCheckBox("连续重复")
        self.sweep_bidirectional = QCheckBox("双向三角扫频")
        self.sweep_external_trigger = QCheckBox("使用外部扫频/Chirp 触发")
        form.addRow("起始频率", self.sweep_start)
        form.addRow("终止频率", self.sweep_end)
        form.addRow("BLX 频率步长", self.sweep_step)
        form.addRow("LMS 扫频时间", self.sweep_time)
        form.addRow("BLX 点驻留", self.sweep_dwell)
        form.addRow("BLX 空闲时间", self.sweep_idle)
        form.addRow("方向", self.sweep_direction)
        form.addRow(self.sweep_repeat)
        form.addRow(self.sweep_bidirectional)
        form.addRow(self.sweep_external_trigger)
        sweep_layout.addLayout(form)
        sweep_buttons = QHBoxLayout()
        apply_sweep = QPushButton("应用扫频参数")
        apply_sweep.setObjectName("PrimaryButton")
        start_sweep = QPushButton("启动扫频")
        stop_sweep = QPushButton("停止")
        apply_sweep.clicked.connect(self._apply_sweep)
        start_sweep.clicked.connect(lambda: self._set_sweep_running(True))
        stop_sweep.clicked.connect(lambda: self._set_sweep_running(False))
        sweep_buttons.addWidget(apply_sweep)
        sweep_buttons.addWidget(start_sweep)
        sweep_buttons.addWidget(stop_sweep)
        sweep_layout.addLayout(sweep_buttons)
        upper.addWidget(sweep, 3)

        chirp, chirp_layout = panel("Chirp")
        chirp_form = QFormLayout()
        self.chirp_enabled = QCheckBox("启用 Chirp 模式")
        self.chirp_length = unit_value_spin(50e-6, 1e-6, 50e-3, "time", "us")
        self.chirp_repeat = unit_value_spin(100e-6, 10e-6, 5.0, "time", "us")
        self.chirp_external_trigger = QCheckBox("外部触发")
        chirp_form.addRow(self.chirp_enabled)
        chirp_form.addRow("Chirp 长度", self.chirp_length)
        chirp_form.addRow("重复周期", self.chirp_repeat)
        chirp_form.addRow(self.chirp_external_trigger)
        chirp_layout.addLayout(chirp_form)
        self.apply_chirp_button = QPushButton("应用 Chirp 参数")
        self.apply_chirp_button.setObjectName("PrimaryButton")
        self.apply_chirp_button.clicked.connect(self._apply_chirp)
        chirp_layout.addWidget(
            self.apply_chirp_button,
            alignment=Qt.AlignmentFlag.AlignLeft,
        )
        chirp_note = QLabel("Chirp 使用 StartSweep 启停；仅支持 DLL 报告有 Chirp 能力的型号。")
        chirp_note.setObjectName("Muted")
        chirp_note.setWordWrap(True)
        chirp_layout.addWidget(chirp_note)
        chirp_layout.addStretch()
        upper.addWidget(chirp, 2)
        layout.addLayout(upper, 1)
        return page

    def _pulse_page(self) -> QWidget:
        page, layout = page_shell(
            "脉冲调制",
            "使用推荐的快速脉冲 API 设置开启时间和重复周期，并可叠加外部脉冲输入。",
        )
        pulse, pulse_layout = panel("内部/外部脉冲")
        form = QFormLayout()
        self.pulse_enabled = QCheckBox("启用内部快速脉冲")
        self.pulse_on = unit_value_spin(1e-3, 1e-7, 1e3, "time", "ms")
        self.pulse_period = unit_value_spin(2e-3, 1e-7, 1e3, "time", "ms")
        self.pulse_external = QCheckBox("同时启用外部脉冲调制输入")
        form.addRow(self.pulse_enabled)
        form.addRow("RF 开启时间", self.pulse_on)
        form.addRow("重复周期", self.pulse_period)
        form.addRow(self.pulse_external)
        pulse_layout.addLayout(form)
        self.apply_pulse_button = QPushButton("应用脉冲参数")
        self.apply_pulse_button.setObjectName("PrimaryButton")
        self.apply_pulse_button.clicked.connect(self._apply_pulse)
        pulse_layout.addWidget(
            self.apply_pulse_button,
            alignment=Qt.AlignmentFlag.AlignLeft,
        )
        note = QLabel(
            "有效范围为 100 ns 至 1000 s，重复周期必须不小于开启时间。"
            "未安装快速脉冲选件的型号会返回明确错误。"
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        pulse_layout.addWidget(note)
        layout.addWidget(pulse)
        layout.addStretch()
        return page

    def _sequence_page(self) -> QWidget:
        page, layout = page_shell(
            "BLX 频率/功率序列",
            "编辑最多 50 项的设备端序列；每项可仅控制频率或同时控制频率与功率。",
        )
        settings, settings_layout = panel("序列参数")
        form = QFormLayout()
        self.sequence_start = QSpinBox()
        self.sequence_start.setRange(0, 49)
        self.sequence_dwell = unit_value_spin(0.01, 0.01, 3600, "time", "ms")
        self.sequence_idle = unit_value_spin(0.0, 0.0, 3600, "time", "ms")
        form.addRow("起始索引（0 基）", self.sequence_start)
        form.addRow("每项驻留", self.sequence_dwell)
        form.addRow("重复空闲", self.sequence_idle)
        settings_layout.addLayout(form)
        row_actions = QHBoxLayout()
        add = QPushButton("添加序列项")
        remove = QPushButton("删除选中")
        add.clicked.connect(self._add_sequence_row)
        remove.clicked.connect(self._remove_sequence_rows)
        row_actions.addWidget(add)
        row_actions.addWidget(remove)
        row_actions.addStretch()
        settings_layout.addLayout(row_actions)
        self.sequence_table = QTableWidget(0, 4)
        self.sequence_table.setProperty("skipDeviceParameterPersistence", True)
        self.sequence_table.setHorizontalHeaderLabels(
            ["索引", "频率 (MHz)", "功率 (dBm)", "同时控制功率"]
        )
        self.sequence_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.sequence_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        settings_layout.addWidget(self.sequence_table, 1)
        for frequency, power in ((5000.0, 0.0), (5100.0, -3.0), (5200.0, -6.0)):
            self._append_sequence_row(frequency, power, True)
        controls = QHBoxLayout()
        apply_button = QPushButton("写入全部序列")
        apply_button.setObjectName("PrimaryButton")
        once = QPushButton("运行一次")
        repeat = QPushButton("重复运行")
        pause = QPushButton("暂停")
        resume = QPushButton("继续")
        stop = QPushButton("停止")
        apply_button.clicked.connect(self._apply_sequence)
        once.clicked.connect(lambda: self._control_sequence(START_SEQUENCE))
        repeat.clicked.connect(lambda: self._control_sequence(REPEAT_SEQUENCE))
        pause.clicked.connect(lambda: self._control_sequence(PAUSE_SEQUENCE))
        resume.clicked.connect(lambda: self._control_sequence(RESUME_SEQUENCE))
        stop.clicked.connect(lambda: self._control_sequence(STOP_SEQUENCE))
        for button in (apply_button, once, repeat, pause, resume, stop):
            controls.addWidget(button)
        self.sequence_control_buttons = (
            apply_button,
            once,
            repeat,
            pause,
            resume,
            stop,
        )
        controls.addStretch()
        settings_layout.addLayout(controls)
        layout.addWidget(settings, 1)
        return page

    def _trigger_page(self) -> QWidget:
        page, layout = page_shell(
            "NI 触发频率/功率步进",
            "NI 只累计外部边沿；每个边沿到达后通过 Vaunix USB DLL 下发新参数。第一个触发应用起始点，随后循环。",
        )
        upper = QHBoxLayout()
        config, config_layout = panel("NI 触发源")
        form = QFormLayout()
        self.trigger_ni = QComboBox()
        self.trigger_counter = QComboBox()
        for index in range(4):
            self.trigger_counter.addItem(f"ctr{index}", f"ctr{index}")
        self.trigger_terminal = QComboBox()
        self.trigger_terminal.setEditable(True)
        self.trigger_terminal.addItems([f"PFI{index}" for index in range(16)])
        self.trigger_edge = QComboBox()
        self.trigger_edge.addItems(["上升沿", "下降沿"])
        self.trigger_mode = QComboBox()
        self.trigger_mode.addItem("同步联动（同序号）", "synchronous")
        self.trigger_mode.addItem("组合扫描（笛卡尔积）", "cartesian")
        form.addRow("NI 设备", self.trigger_ni)
        form.addRow("计数器", self.trigger_counter)
        form.addRow("PFI 端子", self.trigger_terminal)
        form.addRow("有效边沿", self.trigger_edge)
        form.addRow("联动方式", self.trigger_mode)
        config_layout.addLayout(form)
        refresh = QPushButton("刷新 NI 设备")
        refresh.clicked.connect(self.refresh_ni_devices)
        config_layout.addWidget(refresh, alignment=Qt.AlignmentFlag.AlignLeft)
        self.refresh_ni_devices(force=True)
        upper.addWidget(config, 1)

        axes, axes_layout = panel("扫描轴")
        axes_form = QFormLayout()
        self.trigger_frequency_enabled = QCheckBox("启用频率轴")
        self.trigger_frequency_enabled.setChecked(True)
        self.trigger_frequency_start = unit_value_spin(
            5e9, 1e3, 42e9, "frequency", "GHz"
        )
        self.trigger_frequency_step = unit_value_spin(
            100e6, -42e9, 42e9, "frequency", "MHz"
        )
        self.trigger_frequency_count = QSpinBox()
        self.trigger_frequency_count.setRange(1, MAX_SCAN_POINTS)
        self.trigger_frequency_count.setValue(4)
        self.trigger_power_enabled = QCheckBox("启用功率/幅度轴")
        self.trigger_power_start = QDoubleSpinBox()
        self.trigger_power_start.setRange(-100, 30)
        self.trigger_power_start.setDecimals(2)
        self.trigger_power_start.setSingleStep(0.5)
        self.trigger_power_start.setSuffix(" dBm")
        self.trigger_power_step = QDoubleSpinBox()
        self.trigger_power_step.setRange(-100, 100)
        self.trigger_power_step.setDecimals(2)
        self.trigger_power_step.setSingleStep(0.5)
        self.trigger_power_step.setValue(1.0)
        self.trigger_power_step.setSuffix(" dB")
        self.trigger_power_count = QSpinBox()
        self.trigger_power_count.setRange(1, MAX_SCAN_POINTS)
        self.trigger_power_count.setValue(4)
        axes_form.addRow(self.trigger_frequency_enabled)
        axes_form.addRow("频率起点", self.trigger_frequency_start)
        axes_form.addRow("频率步长", self.trigger_frequency_step)
        axes_form.addRow("频率点数", self.trigger_frequency_count)
        axes_form.addRow(self.trigger_power_enabled)
        axes_form.addRow("功率起点", self.trigger_power_start)
        axes_form.addRow("功率步长", self.trigger_power_step)
        axes_form.addRow("功率点数", self.trigger_power_count)
        axes_layout.addLayout(axes_form)
        generate = QPushButton("生成循环点表")
        generate.setObjectName("PrimaryButton")
        generate.clicked.connect(self._generate_trigger_table)
        axes_layout.addWidget(generate, alignment=Qt.AlignmentFlag.AlignLeft)
        upper.addWidget(axes, 1)
        layout.addLayout(upper)

        table_panel, table_layout = panel("触发循环序列")
        self.trigger_table = QTableWidget(0, 4)
        self.trigger_table.setProperty("skipDeviceParameterPersistence", True)
        self.trigger_table.setHorizontalHeaderLabels(
            ["序号", "频率 (Hz)", "功率 (dBm)", "说明"]
        )
        self.trigger_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.trigger_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        table_layout.addWidget(self.trigger_table)
        layout.addWidget(table_panel, 1)
        self._generate_trigger_table(show_message=False)

        metrics = QHBoxLayout()
        self.trigger_metrics: dict[str, QLabel] = {}
        for key, caption, value, color in (
            ("received", "已收到触发", "0", "#8f7cff"),
            ("applied", "已完成下发", "0", "#55d9cc"),
            ("backlog", "待处理积压", "0", "#f0b95d"),
            ("current", "当前点", "—", "#e7ebf4"),
        ):
            card, number = metric_card(caption, value, color)
            self.trigger_metrics[key] = number
            metrics.addWidget(card)
        layout.addLayout(metrics)
        actions = QHBoxLayout()
        self.trigger_start_button = QPushButton("启动联动")
        self.trigger_start_button.setObjectName("PrimaryButton")
        self.trigger_stop_button = QPushButton("停止")
        self.trigger_stop_button.setEnabled(False)
        self.trigger_start_button.clicked.connect(self._start_trigger)
        self.trigger_stop_button.clicked.connect(self._stop_trigger)
        actions.addWidget(self.trigger_start_button)
        actions.addWidget(self.trigger_stop_button)
        if self.connection_manager is not None:
            plus_one = QPushButton("虚拟触发 +1")
            plus_ten = QPushButton("虚拟触发 +10")
            plus_one.clicked.connect(lambda: self._emit_virtual_trigger(1))
            plus_ten.clicked.connect(lambda: self._emit_virtual_trigger(10))
            actions.addWidget(plus_one)
            actions.addWidget(plus_ten)
        actions.addStretch()
        note = QLabel("不设置最小触发间隔；过快边沿进入有序积压队列")
        note.setObjectName("Muted")
        actions.addWidget(note)
        layout.addLayout(actions)
        page.setMinimumHeight(900)
        scroll = QScrollArea()
        scroll.setObjectName("VaunixTriggerScroll")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    def _system_page(self) -> QWidget:
        page, layout = page_shell(
            "状态与设备存储",
            "读取 DLL 状态位和型号能力，并将当前硬件参数写入设备非易失存储。",
        )
        status, status_layout = panel("实时状态")
        self.status_text = QLabel("连接后读取 DEV_CONNECTED、PLL、扫频和选件状态")
        self.status_text.setWordWrap(True)
        self.status_text.setStyleSheet("font-family:Consolas; color:#b8c7da;")
        status_layout.addWidget(self.status_text)
        refresh = QPushButton("刷新状态与全部参数")
        refresh.clicked.connect(self._refresh_all)
        status_layout.addWidget(refresh, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(status)
        storage, storage_layout = panel("设备上电设置")
        save = QPushButton("保存当前设置到设备")
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self._save_settings)
        storage_layout.addWidget(save, alignment=Qt.AlignmentFlag.AlignLeft)
        warning = QLabel("此操作调用 fnLMS_SaveSettings 并写入设备非易失存储，仅在确认参数后使用。")
        warning.setObjectName("Muted")
        warning.setWordWrap(True)
        storage_layout.addWidget(warning)
        layout.addWidget(storage)
        layout.addStretch()
        return page

    def _require_connected(self) -> bool:
        if self.device.is_connected:
            return True
        QMessageBox.information(self, "尚未连接", "请先在总览页连接 Vaunix 设备。")
        return False

    def _connect(self) -> None:
        self.connect_button.setEnabled(False)
        self.connect_button.setText("连接中…")
        self.submit(
            lambda: (
                self.connection_manager.connect(self.record).identity
                if self.connection_manager is not None
                else self.device.connect()
            ),
            on_success=self._connected,
            on_error=self._connect_failed,
        )

    def _connect_failed(self, message: str) -> None:
        self.connect_button.setEnabled(True)
        self.connect_button.setText("连接设备")
        self._show_hardware_error(message)

    def _connected(self, result: Any) -> None:
        identity = getattr(result, "identity", None) or str(result)
        self._ui_connected = True
        self._ui_connection_generation = self.device.connection_generation
        self.identity_label.setText(identity)
        self.driver_status.setObjectName("StatusOnline")
        self.driver_status.setText("● Vaunix DLL 已连接")
        self.driver_status.style().unpolish(self.driver_status)
        self.driver_status.style().polish(self.driver_status)
        self.overview_metrics["connection"].setText("已连接")
        self.connect_button.setText("已连接")
        self.connect_button.setEnabled(False)
        self.disconnect_button.setEnabled(True)
        self.read_all_button.setEnabled(True)
        self._apply_capabilities(self.device.capabilities)
        self._invalidate_output_readback("已连接 · 点击回读频率 / 幅度")
        self._update_output_readback_buttons()
        self._refresh_metrics_only()

    def _disconnect(self) -> None:
        self.disconnect_button.setEnabled(False)
        self.submit(
            lambda: (
                self.connection_manager.disconnect(self.record)
                if self.connection_manager is not None
                else self.device.disconnect()
            ),
            on_success=self._disconnected,
        )

    def _disconnected(self, _result: Any = None) -> None:
        self._ui_connected = False
        self._ui_connection_generation = None
        self.overview_metrics["connection"].setText("未连接")
        self.overview_metrics["frequency"].setText("—")
        self.overview_metrics["power"].setText("—")
        self.overview_metrics["rf"].setText("未知")
        self.connect_button.setText("连接设备")
        self.connect_button.setEnabled(True)
        self.disconnect_button.setEnabled(False)
        self.read_all_button.setEnabled(False)
        self.driver_status.setObjectName("StatusPending")
        self.driver_status.setText("● 驱动尚未连接")
        self.driver_status.style().unpolish(self.driver_status)
        self.driver_status.style().polish(self.driver_status)
        self._invalidate_output_readback("未连接 · 回读值未知")
        self._update_output_readback_buttons()

    def sync_connection_state(self) -> None:
        if self.connection_manager is None:
            return
        connected = self.connection_manager.is_connected(self.record)
        if connected and not self._ui_connected:
            self._connected(self.connection_manager.info(self.record))
        elif not connected and self._ui_connected:
            self._disconnected()
        elif connected and self.device.connection_generation != self._ui_connection_generation:
            self._ui_connection_generation = self.device.connection_generation
            self._invalidate_output_readback("连接已变化 · 回读值未知，请重新回读")
            self._clear_output_metrics()
            self._update_output_readback_buttons()

    def _apply_capabilities(self, caps: VaunixCapabilities) -> None:
        self.capability_label.setText(
            f"{caps.model} · SN {caps.serial_number} · "
            f"DLL {format_dll_version(caps.dll_version)} "
            f"(0x{caps.dll_version:04X})\n"
            f"频率 {caps.minimum_frequency_hz / 1e6:g} 至 "
            f"{caps.maximum_frequency_hz / 1e6:g} MHz · 功率 "
            f"{caps.minimum_power_dbm:g} 至 {caps.maximum_power_dbm:g} dBm\n"
            f"BLX 序列={'支持' if caps.is_blx else '不支持'} · "
            f"快速脉冲={'支持' if caps.has_fast_pulse else '不支持'} · "
            f"Chirp={'支持' if caps.has_chirp else '不支持'}"
        )
        self.sweep_time.setEnabled(not caps.is_blx)
        self.sweep_step.setEnabled(caps.is_blx)
        self.sweep_step.setToolTip(
            "BLX 内部扫频的频率步长" if caps.is_blx else
            "LMS 使用扫频时间，不支持独立设置内部扫频步长；NI 触发步进仍可设置频率步长。"
        )
        self.sweep_dwell.setEnabled(caps.is_blx)
        self.sweep_idle.setEnabled(caps.is_blx)
        self.pulse_enabled.setEnabled(caps.has_fast_pulse)
        self.pulse_on.setEnabled(caps.has_fast_pulse)
        self.pulse_period.setEnabled(caps.has_fast_pulse)
        self.apply_pulse_button.setEnabled(True)
        self.apply_chirp_button.setEnabled(caps.has_chirp)
        for button in self.sequence_control_buttons:
            button.setEnabled(caps.is_blx)
        for control in (
            self.output_frequency,
            self.sweep_start,
            self.sweep_end,
            self.trigger_frequency_start,
        ):
            self._set_unit_range(
                control,
                caps.minimum_frequency_hz,
                caps.maximum_frequency_hz,
            )
        self.output_power.setRange(caps.minimum_power_dbm, caps.maximum_power_dbm)
        self.trigger_power_start.setRange(
            caps.minimum_power_dbm,
            caps.maximum_power_dbm,
        )

    @staticmethod
    def _set_unit_range(control: UnitValueWidget, minimum: float, maximum: float) -> None:
        control.minimum_base = minimum
        control.maximum_base = maximum
        control._apply_display_range()

    def _refresh_metrics_only(self) -> None:
        if not self.device.is_connected:
            return
        try:
            self._require_manual_access()
        except DeviceBusyError:
            return
        self.submit(self.device.output_snapshot, on_success=self._metrics_refreshed,
                    on_error=lambda message: self.statusBar().showMessage(message, 8000))

    def _metrics_refreshed(self, snapshot: VaunixSnapshot | VaunixOutputSnapshot) -> None:
        self.overview_metrics["frequency"].setText(
            f"{snapshot.frequency_hz / 1e6:.9g} MHz"
        )
        self.overview_metrics["power"].setText(f"{snapshot.power_dbm:.6g} dBm")
        self.overview_metrics["rf"].setText("已打开" if snapshot.rf_on else "关闭")
        self._set_status_text(snapshot)

    def _refresh_all(self) -> None:
        if not self._require_connected():
            return
        self.submit(self.device.snapshot, on_success=self._all_refreshed)

    def _all_refreshed(self, snapshot: VaunixSnapshot) -> None:
        self._metrics_refreshed(snapshot)
        self.output_frequency.setValue(snapshot.frequency_hz)
        self.output_power.setValue(snapshot.power_dbm)
        self.output_rf.setChecked(snapshot.rf_on)
        self.output_reference.setCurrentIndex(
            self.output_reference.findData(snapshot.internal_reference)
        )
        self.sweep_start.setValue(snapshot.sweep_start_hz)
        self.sweep_end.setValue(snapshot.sweep_end_hz)
        if snapshot.sweep_step_hz is not None:
            self.sweep_step.setValue(max(10.0, snapshot.sweep_step_hz))
        if snapshot.sweep_time_ms:
            self.sweep_time.setValue(snapshot.sweep_time_ms / 1000.0)
        if snapshot.dwell_time_ms:
            self.sweep_dwell.setValue(snapshot.dwell_time_ms / 1000.0)
        self.sweep_idle.setValue(snapshot.idle_time_ms / 1000.0)
        self.sweep_direction.setCurrentIndex(
            self.sweep_direction.findData(snapshot.sweep_up)
        )
        self.sweep_repeat.setChecked(snapshot.sweep_repeat)
        self.sweep_bidirectional.setChecked(snapshot.sweep_bidirectional)
        self.sweep_external_trigger.setChecked(snapshot.external_sweep_trigger)
        if snapshot.capabilities and snapshot.capabilities.has_fast_pulse:
            self.pulse_on.setValue(snapshot.pulse_on_seconds)
            self.pulse_period.setValue(
                snapshot.pulse_on_seconds + snapshot.pulse_off_seconds
            )
            self.pulse_enabled.setChecked(snapshot.internal_pulse_enabled)
            self.pulse_external.setChecked(snapshot.external_pulse_enabled)
        if snapshot.capabilities and snapshot.capabilities.has_chirp:
            self.chirp_length.setValue(snapshot.chirp_length_seconds)
            self.chirp_repeat.setValue(snapshot.chirp_repeat_seconds)
            self.chirp_external_trigger.setChecked(snapshot.external_sweep_trigger)
        if snapshot.sequence:
            self.sequence_table.setRowCount(0)
            for element in snapshot.sequence:
                self._append_sequence_row(
                    element.frequency_hz / 1e6,
                    element.power_dbm,
                    element.controls_power,
                )
            self.sequence_start.setValue(snapshot.sequence_start)
            self.sequence_dwell.setValue(snapshot.sequence_dwell_ms / 1000.0)
            self.sequence_idle.setValue(snapshot.sequence_idle_ms / 1000.0)
        self.statusBar().showMessage("已从 Vaunix 设备读取全部参数", 5000)

    def _set_status_text(self, snapshot: VaunixSnapshot | VaunixOutputSnapshot) -> None:
        bits = []
        for flag, label in (
            (0x1, "CONNECTED"),
            (0x2, "OPENED"),
            (0x4, "SWEEP_ACTIVE"),
            (0x8, "SWEEP_UP"),
            (0x10, "SWEEP_REPEAT"),
            (0x20, "BIDIRECTIONAL"),
            (0x40, "PLL_LOCKED"),
            (0x80, "FAST_PULSE_OPTION"),
        ):
            if snapshot.device_status & flag:
                bits.append(label)
        self.status_text.setText(
            f"status=0x{snapshot.device_status:08X}  " + " | ".join(bits)
        )

    def _apply_output(self) -> None:
        if not self._require_connected():
            return
        frequency = self.output_frequency.value()
        try:
            power = validate_power_dbm(self.output_power.value(), "输出功率")
        except ValueError as exc:
            QMessageBox.warning(self, "功率无效", str(exc))
            return
        rf_on = self.output_rf.isChecked()
        internal_ref = bool(self.output_reference.currentData())

        def apply() -> None:
            self.device.configure_output(
                frequency_hz=frequency,
                power_dbm=power,
                rf_on=rf_on,
                internal_reference=internal_ref,
            )

        self.submit(
            apply,
            on_success=lambda _=None: (
                self.statusBar().showMessage("Vaunix 输出参数已应用", 4000),
                self._refresh_metrics_only(),
            ),
        )

    def _apply_sweep(self) -> None:
        if not self._require_connected():
            return
        self.submit(
            lambda: self.device.configure_sweep(
                start_hz=self.sweep_start.value(),
                end_hz=self.sweep_end.value(),
                step_hz=self.sweep_step.value(),
                sweep_time_ms=round(self.sweep_time.value() * 1000),
                dwell_time_ms=round(self.sweep_dwell.value() * 1000),
                idle_time_ms=round(self.sweep_idle.value() * 1000),
                upward=bool(self.sweep_direction.currentData()),
                repeating=self.sweep_repeat.isChecked(),
                bidirectional=self.sweep_bidirectional.isChecked(),
                external_trigger=self.sweep_external_trigger.isChecked(),
            ),
            on_success=lambda _=None: self.statusBar().showMessage(
                "Vaunix 扫频参数已应用", 4000
            ),
        )

    def _set_sweep_running(self, enabled: bool) -> None:
        if not self._require_connected():
            return
        self.submit(
            lambda: self.device.start_sweep(enabled),
            on_success=lambda _=None: self.statusBar().showMessage(
                "扫频/Chirp 已启动" if enabled else "扫频/Chirp 已停止",
                4000,
            ),
        )

    def _apply_chirp(self) -> None:
        if not self._require_connected():
            return
        self.submit(
            lambda: self.device.configure_chirp(
                enabled=self.chirp_enabled.isChecked(),
                length_seconds=self.chirp_length.value(),
                repeat_seconds=self.chirp_repeat.value(),
                external_trigger=self.chirp_external_trigger.isChecked(),
            ),
            on_success=lambda _=None: self.statusBar().showMessage(
                "Chirp 参数已应用", 4000
            ),
        )

    def _apply_pulse(self) -> None:
        if not self._require_connected():
            return
        self.submit(
            lambda: self.device.configure_pulse(
                on_seconds=self.pulse_on.value(),
                period_seconds=self.pulse_period.value(),
                enabled=self.pulse_enabled.isChecked(),
                external_enabled=self.pulse_external.isChecked(),
            ),
            on_success=lambda _=None: self.statusBar().showMessage(
                "脉冲调制参数已应用", 4000
            ),
        )

    def _append_sequence_row(
        self,
        frequency_mhz: float,
        power_dbm: float,
        controls_power: bool,
    ) -> None:
        if self.sequence_table.rowCount() >= 50:
            return
        row = self.sequence_table.rowCount()
        self.sequence_table.insertRow(row)
        number = QTableWidgetItem(str(row))
        number.setFlags(number.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.sequence_table.setItem(row, 0, number)
        self.sequence_table.setItem(row, 1, QTableWidgetItem(roundtrip_number(frequency_mhz)))
        self.sequence_table.setItem(row, 2, QTableWidgetItem(roundtrip_number(power_dbm)))
        enabled = QTableWidgetItem("是" if controls_power else "否")
        enabled.setFlags(enabled.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        enabled.setCheckState(
            Qt.CheckState.Checked if controls_power else Qt.CheckState.Unchecked
        )
        self.sequence_table.setItem(row, 3, enabled)

    def _add_sequence_row(self) -> None:
        if self.sequence_table.rowCount() >= 50:
            QMessageBox.information(self, "序列已满", "设备最多支持 50 个序列项。")
            return
        if self.sequence_table.rowCount():
            last = self.sequence_table.rowCount() - 1
            frequency = float(self.sequence_table.item(last, 1).text())
            power = float(self.sequence_table.item(last, 2).text())
        else:
            frequency, power = 5000.0, 0.0
        self._append_sequence_row(frequency, power, True)

    def _remove_sequence_rows(self) -> None:
        rows = sorted(
            {index.row() for index in self.sequence_table.selectedIndexes()},
            reverse=True,
        )
        for row in rows:
            self.sequence_table.removeRow(row)
        for row in range(self.sequence_table.rowCount()):
            self.sequence_table.item(row, 0).setText(str(row))

    def _sequence_elements(self) -> tuple[VaunixSequenceElement, ...]:
        if self.sequence_table.rowCount() < 1:
            raise ValueError("序列至少需要一个项目")
        elements = []
        for row in range(self.sequence_table.rowCount()):
            try:
                frequency_hz = float(self.sequence_table.item(row, 1).text()) * 1e6
                power_value = float(self.sequence_table.item(row, 2).text())
            except (AttributeError, ValueError) as exc:
                raise ValueError(f"序列第 {row + 1} 行包含无效数值") from exc
            power_dbm = validate_power_dbm(
                power_value,
                f"序列第 {row + 1} 行功率",
            )
            elements.append(
                VaunixSequenceElement(
                    frequency_hz,
                    power_dbm,
                    self.sequence_table.item(row, 3).checkState()
                    == Qt.CheckState.Checked,
                )
            )
        return tuple(elements)

    def _apply_sequence(self) -> None:
        if not self._require_connected():
            return
        try:
            elements = self._sequence_elements()
        except ValueError as exc:
            QMessageBox.warning(self, "序列无效", str(exc))
            return
        self.submit(
            lambda: self.device.configure_sequence(
                elements,
                start_index=self.sequence_start.value(),
                dwell_time_ms=round(self.sequence_dwell.value() * 1000),
                idle_time_ms=round(self.sequence_idle.value() * 1000),
            ),
            on_success=lambda _=None: self.statusBar().showMessage(
                "BLX 序列已写入", 4000
            ),
        )

    def _control_sequence(self, control: int) -> None:
        if not self._require_connected():
            return
        self.submit(
            lambda: self.device.control_sequence(control),
            on_success=lambda _=None: self.statusBar().showMessage(
                "序列控制命令已发送", 4000
            ),
        )

    def _save_settings(self) -> None:
        if not self._require_connected():
            return
        answer = QMessageBox.question(
            self,
            "保存设备上电设置",
            "确认将当前硬件设置写入 Vaunix 非易失存储吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        self.submit(
            self.device.save_settings,
            on_success=lambda _=None: self.statusBar().showMessage(
                "Vaunix 当前设置已保存到设备", 5000
            ),
        )

    def refresh_ni_devices(
        self,
        _checked: bool = False,
        *,
        force: bool = False,
        from_provider: bool = True,
    ) -> None:
        if from_provider and self.device_provider is not None:
            self.ni_devices = [
                item
                for item in self.device_provider()
                if item.kind is DeviceKind.NI_DAQ
            ]
        if not hasattr(self, "trigger_ni"):
            return
        signature = tuple(
            (
                item.id,
                item.name,
                item.resource,
                bool(
                    self.connection_manager is not None
                    and self.connection_manager.is_connected(item)
                ),
            )
            for item in self.ni_devices
        )
        if not force and signature == self._ni_signature:
            return
        selected = self.trigger_ni.currentData()
        self.trigger_ni.blockSignals(True)
        self.trigger_ni.clear()
        for device in self.ni_devices:
            connected = bool(
                self.connection_manager is not None
                and self.connection_manager.is_connected(device)
            )
            self.trigger_ni.addItem(
                f"● {'已连接' if connected else '未连接'} · {device.name} ({device.resource})",
                device.id,
            )
        index = self.trigger_ni.findData(selected)
        self.trigger_ni.setCurrentIndex(max(0, index))
        self.trigger_ni.setEnabled(bool(self.ni_devices) and self.controller is None)
        self.trigger_ni.blockSignals(False)
        self._ni_signature = signature

    def update_ni_devices(self, all_devices: list[DeviceRecord]) -> None:
        self.ni_devices = [item for item in all_devices if item.kind is DeviceKind.NI_DAQ]
        self.refresh_ni_devices(from_provider=False)

    def _trigger_axes_values(self) -> tuple[tuple[float, ...], tuple[float, ...]]:
        frequencies = (
            linear_scan_points(
                self.trigger_frequency_start.value(),
                self.trigger_frequency_step.value(),
                self.trigger_frequency_count.value(),
            )
            if self.trigger_frequency_enabled.isChecked()
            else ()
        )
        powers: tuple[float, ...] = ()
        if self.trigger_power_enabled.isChecked():
            power_start = validate_power_dbm(
                self.trigger_power_start.value(),
                "功率扫描起点",
            )
            power_step = validate_power_dbm(
                self.trigger_power_step.value(),
                "功率扫描步长",
            )
            powers = linear_scan_points(
                power_start,
                power_step,
                self.trigger_power_count.value(),
            )
            for index, power in enumerate(powers, start=1):
                validate_power_dbm(power, f"功率扫描第 {index} 点")
        if not frequencies and not powers:
            raise ValueError("至少启用频率或功率中的一个扫描轴")
        return frequencies, powers

    def _generate_trigger_table(
        self,
        _checked: bool = False,
        *,
        show_message: bool = True,
    ) -> None:
        try:
            frequencies, powers = self._trigger_axes_values()
            mode = self.trigger_mode.currentData()
            if frequencies and powers and mode == "synchronous":
                if len(frequencies) != len(powers):
                    raise ValueError("同步联动要求频率轴和功率轴点数相同")
                values = tuple(zip(frequencies, powers))
            elif frequencies and powers:
                values = tuple(product(frequencies, powers))
            elif frequencies:
                values = tuple((frequency, None) for frequency in frequencies)
            else:
                values = tuple((None, power) for power in powers)
            if len(values) > MAX_SCAN_POINTS:
                raise ValueError(f"联合点数不能超过 {MAX_SCAN_POINTS}")
        except ValueError as exc:
            if show_message:
                QMessageBox.warning(self, "无法生成点表", str(exc))
            return
        self.trigger_table.setRowCount(0)
        for index, (frequency, power) in enumerate(values):
            row = self.trigger_table.rowCount()
            self.trigger_table.insertRow(row)
            number = QTableWidgetItem(str(index + 1))
            number.setFlags(number.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.trigger_table.setItem(row, 0, number)
            self.trigger_table.setItem(
                row,
                1,
                QTableWidgetItem("" if frequency is None else roundtrip_number(frequency)),
            )
            self.trigger_table.setItem(
                row,
                2,
                QTableWidgetItem("" if power is None else roundtrip_number(power)),
            )
            self.trigger_table.setItem(
                row,
                3,
                QTableWidgetItem("起始点（第一个触发）" if index == 0 else ""),
            )
        if show_message:
            self.statusBar().showMessage(f"已生成 {len(values)} 个循环点", 4000)

    def _trigger_points(self) -> tuple[ScanPoint, ...]:
        if self.trigger_table.rowCount() < 1:
            raise ValueError("触发点表不能为空")
        points = []
        for row in range(self.trigger_table.rowCount()):
            values = []
            frequency_text = self.trigger_table.item(row, 1).text().strip()
            power_text = self.trigger_table.item(row, 2).text().strip()
            if frequency_text:
                values.append(
                    StepValue(
                        StepParameter.FREQUENCY,
                        1,
                        float(frequency_text),
                        float(frequency_text),
                        "Hz",
                    )
                )
            if power_text:
                power_value = validate_power_dbm(
                    float(power_text),
                    f"点表第 {row + 1} 行功率",
                )
                values.append(
                    StepValue(
                        StepParameter.AMPLITUDE,
                        1,
                        power_value,
                        power_value,
                        "dBm",
                    )
                )
            if not values:
                raise ValueError(f"点表第 {row + 1} 行没有任何参数")
            points.append(ScanPoint(tuple(values)))
        return tuple(points)

    def _start_trigger(self) -> None:
        if self.controller is not None:
            self.statusBar().showMessage("请先完成当前任务的停止，再启动新的触发任务。", 5000)
            return
        if self._call_tasks:
            self.statusBar().showMessage("请等待设备后台操作完成，再启动触发任务。", 5000)
            return
        if not self._require_connected():
            return
        ni_id = self.trigger_ni.currentData()
        ni_record = next((item for item in self.ni_devices if item.id == ni_id), None)
        if ni_record is None:
            QMessageBox.warning(self, "缺少 NI", "请选择 NI 采集卡。")
            return
        if (
            self.connection_manager is not None
            and not self.connection_manager.is_connected(ni_record)
        ):
            QMessageBox.warning(self, "NI 未连接", "请先连接 NI 采集卡。")
            return
        try:
            points = self._trigger_points()
            if self.connection_manager is not None:
                self.connection_manager.acquire_devices(
                    self._lease_owner,
                    (self.record.id, ni_record.id),
                )
                counter = self.connection_manager.counter_for(
                    ni_record,
                    counter=self.trigger_counter.currentData(),
                    terminal=self.trigger_terminal.currentText(),
                    rising=self.trigger_edge.currentIndex() == 0,
                )
            else:
                counter = NIDaqEdgeCounter(
                    ni_record.resource,
                    counter=self.trigger_counter.currentData(),
                    terminal=self.trigger_terminal.currentText(),
                    rising=self.trigger_edge.currentIndex() == 0,
                )
            controller = TriggerStepController(counter, self.device, scan_points=points)
        except Exception as exc:
            if self.connection_manager is not None:
                self.connection_manager.release_devices(self._lease_owner)
            QMessageBox.warning(self, "无法启动联动", str(exc))
            return
        controller.add_callback(self.trigger_bridge.begin_session())
        self.controller = controller
        self.trigger_start_button.setEnabled(False)
        self.trigger_start_button.setText("启动中…")
        self.trigger_stop_button.setEnabled(False)
        self._trigger_pending_stop = False
        self._trigger_start_error = None
        task = CallTask(controller.start)
        self._trigger_start_task = task
        self._call_tasks.add(task)
        task.signals.failed.connect(
            lambda message, item=task, source=controller: self._trigger_start_task_failed(source, item, message)
        )
        task.signals.finished.connect(
            lambda item=task, source=controller: self._trigger_start_finished(source, item)
        )
        self.trigger_stop_button.setEnabled(True)
        try:
            self.thread_pool.start(task)
        except Exception as exc:
            self._trigger_start_task_failed(controller, task, f"后台任务未能提交：{exc}")
            self._trigger_start_finished(controller, task)

    def _trigger_start_task_failed(self, controller: TriggerStepController, task: CallTask, message: str) -> None:
        if controller is self.controller and task is self._trigger_start_task:
            self._trigger_start_error = message

    def _trigger_start_finished(self, controller: TriggerStepController, task: CallTask) -> None:
        self._call_tasks.discard(task)
        if task is not self._trigger_start_task:
            return
        self._trigger_start_task = None
        error, self._trigger_start_error = self._trigger_start_error, None
        if controller is not self.controller:
            return
        self.trigger_bridge.flush()
        if self._trigger_pending_stop:
            self._trigger_pending_stop = False
            self._stop_trigger()
        elif error:
            self._trigger_start_failed(error)
        else:
            self.statusBar().showMessage("NI 触发步进已启动", 4000)

    def _trigger_start_failed(self, message: str) -> None:
        self.trigger_bridge.flush()
        if self.controller is not None and not self.controller.is_quiescent:
            self._trigger_stop_failed(message)
            return
        if self.connection_manager is not None:
            self.connection_manager.release_devices(self._lease_owner)
        self.controller = None
        self.trigger_start_button.setText("启动联动")
        self.trigger_start_button.setEnabled(True)
        self._show_hardware_error(message)

    def _stop_trigger(self) -> None:
        if self.controller is None or self._trigger_stop_in_progress:
            return
        self.controller.request_stop()
        if self._trigger_start_task is not None:
            self._trigger_pending_stop = True
            self.trigger_stop_button.setEnabled(False)
            self.statusBar().showMessage("已请求停止，正在等待启动任务返回…", 8000)
            return
        self._trigger_stop_in_progress = True
        self.trigger_stop_button.setEnabled(False)
        controller = self.controller
        self.submit(controller.stop,
                    on_success=lambda result: self._trigger_stopped(result)
                    if controller is self.controller else None,
                    on_error=lambda message: self._trigger_stop_failed(message)
                    if controller is self.controller else None,
                    allow_during_scan=True)

    def _trigger_stopped(self, _result: Any = None) -> None:
        self.trigger_bridge.flush()
        if self.controller is not None and not self.controller.is_quiescent:
            self._trigger_stop_failed("后台操作尚未结束，请等待后重试停止。")
            return
        self._trigger_stop_in_progress = False
        if self.connection_manager is not None:
            self.connection_manager.release_devices(self._lease_owner)
        self.controller = None
        self.trigger_start_button.setText("启动联动")
        self.trigger_start_button.setEnabled(True)
        self.trigger_stop_button.setEnabled(False)
        self.trigger_stop_button.setText("停止")
        self.trigger_ni.setEnabled(bool(self.ni_devices))

    def _trigger_stop_failed(self, message: str) -> None:
        self._trigger_stop_in_progress = False
        self.trigger_bridge.flush()
        self.trigger_start_button.setEnabled(False)
        self.trigger_start_button.setText("等待停止")
        self.trigger_stop_button.setText("重试停止")
        self.trigger_stop_button.setEnabled(self.controller is not None)
        self.statusBar().showMessage(message, 15000)

    def _trigger_updated(self, snapshot: TriggerSnapshot) -> None:
        self.trigger_metrics["received"].setText(str(snapshot.received_count))
        self.trigger_metrics["applied"].setText(str(snapshot.applied_count))
        self.trigger_metrics["backlog"].setText(str(snapshot.backlog))
        if snapshot.current_index is None:
            self.trigger_metrics["current"].setText("—")
        else:
            details = []
            for value in snapshot.current_values:
                if value.parameter is StepParameter.FREQUENCY:
                    details.append(f"{value.value / 1e6:.9g} MHz")
                else:
                    details.append(f"{value.value:.6g} dBm")
            self.trigger_metrics["current"].setText(
                f"#{snapshot.current_index + 1} · " + " · ".join(details)
            )
        running = snapshot.state in {ControllerState.STARTING, ControllerState.RUNNING}
        self.trigger_stop_button.setEnabled(
            self.controller is not None and not self._trigger_stop_in_progress and not self._trigger_pending_stop
            and (running or snapshot.state in {ControllerState.ERROR, ControllerState.STOPPING})
        )
        if snapshot.state is ControllerState.RUNNING:
            self.trigger_start_button.setText("运行中")
            self.trigger_ni.setEnabled(False)
        elif snapshot.state is ControllerState.ERROR:
            self.trigger_start_button.setText("任务错误")
            self.trigger_start_button.setEnabled(False)
            if (self.connection_manager is not None and self.controller is not None
                    and self.controller.is_quiescent and not self._call_tasks):
                self.connection_manager.release_devices(self._lease_owner)
        if snapshot.error and snapshot.error != self._last_trigger_error:
            self._last_trigger_error = snapshot.error
            self.statusBar().showMessage(snapshot.error, 10000)

    def _emit_virtual_trigger(self, count: int) -> None:
        if self.connection_manager is None:
            return
        ni_id = self.trigger_ni.currentData()
        ni = next((item for item in self.ni_devices if item.id == ni_id), None)
        if ni is None or not is_virtual_resource(ni.resource):
            QMessageBox.information(self, "不是虚拟 NI", "请选择虚拟 NI 设备。")
            return
        self.connection_manager.virtual_lab.emit_trigger(ni.resource, count)

    def _custom_device_parameters(self) -> dict[str, Any]:
        if not hasattr(self, "sequence_table") or not hasattr(self, "trigger_table"):
            return {}
        sequence_rows = []
        for row in range(self.sequence_table.rowCount()):
            sequence_rows.append(
                {
                    "frequency_mhz": self.sequence_table.item(row, 1).text(),
                    "power_dbm": self.sequence_table.item(row, 2).text(),
                    "controls_power": self.sequence_table.item(row, 3).checkState()
                    == Qt.CheckState.Checked,
                }
            )
        trigger_rows = []
        for row in range(self.trigger_table.rowCount()):
            trigger_rows.append(
                [
                    self.trigger_table.item(row, column).text()
                    for column in range(1, 4)
                ]
            )
        return {
            "sequence_rows": sequence_rows,
            "trigger_rows": trigger_rows,
        }

    def _restore_custom_device_parameters(self, state: dict[str, Any]) -> None:
        sequence_rows = state.get("sequence_rows")
        if isinstance(sequence_rows, list) and sequence_rows:
            self.sequence_table.setRowCount(0)
            for item in sequence_rows[:50]:
                if not isinstance(item, dict):
                    continue
                try:
                    frequency = float(item["frequency_mhz"])
                    power = float(item["power_dbm"])
                except (KeyError, TypeError, ValueError):
                    continue
                self._append_sequence_row(
                    frequency,
                    power,
                    bool(item.get("controls_power", True)),
                )
        trigger_rows = state.get("trigger_rows")
        if isinstance(trigger_rows, list) and trigger_rows:
            self.trigger_table.setRowCount(0)
            for index, values in enumerate(trigger_rows[:MAX_SCAN_POINTS]):
                if not isinstance(values, list) or len(values) != 3:
                    continue
                row = self.trigger_table.rowCount()
                self.trigger_table.insertRow(row)
                number = QTableWidgetItem(str(index + 1))
                number.setFlags(number.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.trigger_table.setItem(row, 0, number)
                for column, value in enumerate(values, start=1):
                    self.trigger_table.setItem(row, column, QTableWidgetItem(str(value)))

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._call_tasks or (self.controller is not None and not self.controller.is_quiescent):
            self._stop_trigger()
            self.statusBar().showMessage("后台任务尚未停止，请等待完成后再关闭窗口。", 8000)
            event.ignore()
            return
        if self.controller is not None:
            try:
                self.controller.stop(timeout_seconds=0)
            except Exception as exc:
                self._trigger_stop_failed(str(exc))
                event.ignore()
                return
            self.controller = None
        self.trigger_bridge.close()
        if self.connection_manager is not None:
            self.connection_manager.release_devices(self._lease_owner)
        elif self.device.is_connected:
            try:
                self.device.disconnect()
            except Exception:
                pass
        super().closeEvent(event)
