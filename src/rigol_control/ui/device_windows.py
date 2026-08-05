from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, QThreadPool, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QCheckBox,
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
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..connections import ConnectionInfo, DeviceConnectionManager
from ..drivers.dg4000 import BasicWaveform, DG4000Driver, StepParameter
from ..drivers.ni_counter import NIDaqEdgeCounter
from ..drivers.scpi import ScpiSession
from ..models import DeviceKind, DeviceRecord
from ..sequence import MAX_SCAN_POINTS, linear_scan_points
from ..services import DeviceProbeService, ProbeResult, ProbeState
from ..trigger import ControllerState, TriggerSnapshot, TriggerStepController
from ..virtual_lab import is_virtual_resource
from .widgets import CallTask, metric_card


def page_shell(title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(22, 20, 22, 22)
    layout.setSpacing(14)
    heading = QLabel(title)
    heading.setObjectName("WindowTitle")
    description = QLabel(subtitle)
    description.setObjectName("Muted")
    description.setWordWrap(True)
    layout.addWidget(heading)
    layout.addWidget(description)
    return page, layout


def panel(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Panel")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(17, 15, 17, 17)
    layout.setSpacing(12)
    heading = QLabel(title)
    heading.setObjectName("SectionTitle")
    layout.addWidget(heading)
    return frame, layout


def value_spin(value: float, minimum: float, maximum: float, suffix: str, decimals: int = 6) -> QDoubleSpinBox:
    control = QDoubleSpinBox()
    control.setDecimals(decimals)
    control.setRange(minimum, maximum)
    control.setValue(value)
    control.setSuffix(f" {suffix}")
    control.setKeyboardTracking(False)
    return control


class CompactDoubleSpinBox(QDoubleSpinBox):
    """Keep high edit precision without filling the UI with trailing zeroes."""

    def textFromValue(self, value: float) -> str:
        return f"{value:.12g}"


def scan_value_spin(value: float, suffix: str) -> CompactDoubleSpinBox:
    control = CompactDoubleSpinBox()
    control.setDecimals(9)
    control.setRange(-1e15, 1e15)
    control.setValue(value)
    control.setSuffix(f" {suffix}")
    control.setKeyboardTracking(False)
    return control


class TriggerBridge(QObject):
    updated = pyqtSignal(object)


class BaseDeviceWindow(QMainWindow):
    def __init__(self, record: DeviceRecord, navigation: list[tuple[str, Callable[[], QWidget]]]) -> None:
        super().__init__()
        self.record = record
        self.thread_pool = QThreadPool.globalInstance()
        self._call_tasks: set[CallTask] = set()
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(f"{record.name} · 设备设置")
        self.resize(1180, 760)
        self.setMinimumSize(940, 620)

        root = QWidget()
        root.setObjectName("AppRoot")
        body = QHBoxLayout(root)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        sidebar = QFrame()
        sidebar.setFixedWidth(230)
        sidebar.setStyleSheet("background:#111824; border-right:1px solid #273145;")
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(15, 20, 15, 18)
        side.setSpacing(5)

        kind = QLabel(record.kind.label)
        kind.setStyleSheet(f"color:{record.kind.accent}; font-weight:700;")
        name = QLabel(record.name)
        name.setObjectName("CardTitle")
        name.setWordWrap(True)
        resource = QLabel(record.resource)
        resource.setObjectName("Muted")
        resource.setWordWrap(True)
        side.addWidget(kind)
        side.addWidget(name)
        side.addWidget(resource)
        side.addSpacing(18)

        self.pages = QStackedWidget()
        self.nav_buttons: list[QPushButton] = []
        for index, (label, page_factory) in enumerate(navigation):
            button = QPushButton(label)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, i=index: self._select_page(i))
            self.nav_buttons.append(button)
            side.addWidget(button)
            self.pages.addWidget(page_factory())
        side.addStretch()
        self.driver_status = QLabel("● 驱动尚未连接")
        self.driver_status.setObjectName("StatusPending")
        self.driver_status.setToolTip("保存配置不依赖仪器驱动；实际下发命令前需安装并连接驱动。")
        side.addWidget(self.driver_status)
        self._select_page(0)

        body.addWidget(sidebar)
        body.addWidget(self.pages, 1)
        self.setCentralWidget(root)

    def submit(
        self,
        operation: Callable[[], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        task = CallTask(operation)
        self._call_tasks.add(task)
        if on_success:
            task.signals.succeeded.connect(on_success)
        task.signals.failed.connect(on_error or self._show_hardware_error)
        task.signals.finished.connect(lambda item=task: self._call_tasks.discard(item))
        self.thread_pool.start(task)

    def _show_hardware_error(self, message: str) -> None:
        QMessageBox.warning(self, "仪器操作失败", message)

    def _select_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        for position, button in enumerate(self.nav_buttons):
            button.setChecked(position == index)

class DG4000Window(BaseDeviceWindow):
    def __init__(
        self,
        record: DeviceRecord,
        ni_devices: list[DeviceRecord],
        connection_manager: DeviceConnectionManager | None = None,
    ) -> None:
        self.ni_devices = ni_devices
        self.connection_manager = connection_manager
        self.driver = (
            connection_manager.dg_driver_for(record)
            if connection_manager is not None
            else DG4000Driver(ScpiSession(record.resource))
        )
        self.controller: TriggerStepController | None = None
        self.trigger_bridge = TriggerBridge()
        self.trigger_bridge.updated.connect(self._trigger_updated)
        self._last_trigger_error: str | None = None
        self.channel_controls: dict[int, dict[str, Any]] = {}
        self.modulation_controls: dict[str, dict[str, Any]] = {}
        navigation = [
            ("总览", self._overview_page),
            ("通道与波形", self._channel_page),
            ("调制", self._modulation_page),
            ("扫频与脉冲串", self._sweep_burst_page),
            ("触发步进", self._trigger_step_page),
            ("存储与系统", self._system_page),
        ]
        super().__init__(record, navigation)
        if self.driver.is_connected:
            identity = (
                self.connection_manager.info(record).identity
                if self.connection_manager is not None
                else self.driver.identify()
            )
            self._connected(identity)

    def _overview_page(self) -> QWidget:
        page, layout = page_shell("DG4000 设备总览", "管理连接、输出状态与最近的联动任务。")
        metrics = QHBoxLayout()
        for caption, value, color in (
            ("连接状态", "未连接", "#f0b95d"),
            ("通道 1", "关闭", "#8995aa"),
            ("通道 2", "关闭", "#8995aa"),
            ("触发积压", "0", "#55d9cc"),
        ):
            card, _ = metric_card(caption, value, color)
            metrics.addWidget(card)
        layout.addLayout(metrics)

        connection, connection_layout = panel("设备连接")
        form = QFormLayout()
        form.addRow("资源地址", QLabel(self.record.resource))
        form.addRow("连接方式", QLabel(self.record.connection or "VISA / SCPI"))
        self.identity_label = QLabel("连接后读取 *IDN?")
        self.identity_label.setWordWrap(True)
        form.addRow("身份信息", self.identity_label)
        connection_layout.addLayout(form)
        buttons = QHBoxLayout()
        self.connect_button = QPushButton("连接设备")
        self.connect_button.setObjectName("PrimaryButton")
        self.connect_button.clicked.connect(self._connect_device)
        self.disconnect_button = QPushButton("断开")
        self.disconnect_button.setEnabled(False)
        self.disconnect_button.clicked.connect(self._disconnect_device)
        self.self_test_button = QPushButton("执行自检")
        self.self_test_button.setEnabled(False)
        self.self_test_button.clicked.connect(lambda: self._execute_scpi("*TST?"))
        buttons.addWidget(self.connect_button)
        buttons.addWidget(self.disconnect_button)
        buttons.addWidget(self.self_test_button)
        buttons.addStretch()
        connection_layout.addLayout(buttons)
        layout.addWidget(connection)

        note, note_layout = panel("实施状态")
        status = QLabel(
            "设备中心、参数页面和触发步进模型已经建立。硬件会话层将使用单一 SCPI 写入队列，"
            "确保频率、幅度、相位、PSK 相位与 FSK 频率按触发顺序下发。"
        )
        status.setWordWrap(True)
        status.setObjectName("Muted")
        note_layout.addWidget(status)
        layout.addWidget(note)
        if is_virtual_resource(self.record.resource) and self.connection_manager is not None:
            virtual_panel, virtual_layout = panel("虚拟 DG4000 实时状态")
            self.virtual_generator_state = QLabel()
            self.virtual_generator_state.setWordWrap(True)
            self.virtual_generator_state.setStyleSheet("font-family:Consolas; color:#b8c7da;")
            virtual_layout.addWidget(self.virtual_generator_state)
            self.virtual_generator_timer = QTimer(self)
            self.virtual_generator_timer.setInterval(250)
            self.virtual_generator_timer.timeout.connect(self._refresh_virtual_generator_state)
            self.virtual_generator_timer.start()
            self._refresh_virtual_generator_state()
            layout.addWidget(virtual_panel)
        layout.addStretch()
        return page

    def _refresh_virtual_generator_state(self) -> None:
        if self.connection_manager is None or not hasattr(self, "virtual_generator_state"):
            return
        snapshot = self.connection_manager.virtual_lab.generator_snapshot(self.record.resource)
        lines = []
        for channel in snapshot.channels:
            lines.append(
                f"CH{channel.channel}: {channel.function}  {channel.frequency_hz:g} Hz  "
                f"{channel.amplitude_vpp:g} Vpp  {channel.phase_deg:g}°  "
                f"输出={'ON' if channel.output else 'OFF'}  "
                f"{channel.modulation_type}={'ON' if channel.modulation_enabled else 'OFF'}  "
                f"PSK={channel.psk_phase_deg:g}°  FSK={channel.fsk_frequency_hz:g} Hz"
            )
        lines.append(
            f"SCPI 命令数：{snapshot.command_count}    模拟下发延迟：{snapshot.scpi_delay_ms} ms"
        )
        self.virtual_generator_state.setText("\n".join(lines))

    def _connect_device(self) -> None:
        self.connect_button.setEnabled(False)
        self.connect_button.setText("连接中…")
        self.submit(
            lambda: (
                self.connection_manager.connect(self.record)
                if self.connection_manager is not None
                else self.driver.connect()
            ),
            on_success=self._connected,
            on_error=self._connect_failed,
        )

    def _connected(self, identity: Any) -> None:
        identity_text = identity.identity if isinstance(identity, ConnectionInfo) else str(identity)
        self.identity_label.setText(identity_text)
        self.connect_button.setText("已连接")
        self.connect_button.setEnabled(False)
        self.disconnect_button.setEnabled(True)
        self.self_test_button.setEnabled(True)
        self.driver_status.setObjectName("StatusOnline")
        self.driver_status.setText("● 已连接")
        self.driver_status.style().unpolish(self.driver_status)
        self.driver_status.style().polish(self.driver_status)
        self.statusBar().showMessage("DG4000 VISA 会话已连接", 5000)

    def _connect_failed(self, message: str) -> None:
        self.connect_button.setText("连接设备")
        self.connect_button.setEnabled(True)
        self._show_hardware_error(message)

    def _disconnect_device(self) -> None:
        if self.controller is not None:
            self._stop_trigger()
        self.disconnect_button.setEnabled(False)
        self.submit(
            lambda: (
                self.connection_manager.disconnect(self.record)
                if self.connection_manager is not None
                else self.driver.disconnect()
            ),
            on_success=self._disconnected,
        )

    def _disconnected(self, _result: Any = None) -> None:
        self.identity_label.setText("连接后读取 *IDN?")
        self.connect_button.setText("连接设备")
        self.connect_button.setEnabled(True)
        self.self_test_button.setEnabled(False)
        self.driver_status.setObjectName("StatusPending")
        self.driver_status.setText("● 驱动尚未连接")
        self.driver_status.style().unpolish(self.driver_status)
        self.driver_status.style().polish(self.driver_status)
        self.statusBar().showMessage("DG4000 已断开", 5000)

    def _require_connection(self) -> bool:
        if self.driver.is_connected:
            return True
        QMessageBox.information(self, "尚未连接", "请先在“总览”页连接 DG4000。")
        return False

    def _channel_page(self) -> QWidget:
        page, layout = page_shell("通道与基本波形", "配置两个输出通道的波形、频率、幅度、偏置和初相位。")
        tabs = QTabWidget()
        for channel in (1, 2):
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)
            settings, settings_layout = panel(f"CH{channel} 输出")
            form = QFormLayout()
            waveform = QComboBox()
            waveform.addItems(["正弦波", "方波", "锯齿波", "脉冲", "噪声", "任意波"])
            frequency = value_spin(1000, 1e-6, 160e6, "Hz")
            amplitude = value_spin(1.0, 0.001, 20.0, "Vpp", 4)
            offset = value_spin(0, -10, 10, "V", 4)
            phase = value_spin(0, 0, 360, "°", 3)
            form.addRow("波形", waveform)
            form.addRow("频率", frequency)
            form.addRow("幅度", amplitude)
            form.addRow("直流偏置", offset)
            form.addRow("初相位", phase)
            output = QCheckBox("启用通道输出")
            form.addRow("输出", output)
            self.channel_controls[channel] = {
                "waveform": waveform,
                "frequency": frequency,
                "amplitude": amplitude,
                "offset": offset,
                "phase": phase,
                "output": output,
            }
            settings_layout.addLayout(form)
            apply_button = QPushButton(f"应用到 CH{channel}")
            apply_button.setObjectName("PrimaryButton")
            apply_button.clicked.connect(lambda checked=False, selected=channel: self._apply_channel(selected))
            settings_layout.addWidget(apply_button, alignment=Qt.AlignmentFlag.AlignLeft)
            tab_layout.addWidget(settings)
            tab_layout.addStretch()
            tabs.addTab(tab, f"CH{channel}")
        layout.addWidget(tabs)
        return page

    def _apply_channel(self, channel: int) -> None:
        if not self._require_connection():
            return
        controls = self.channel_controls[channel]
        waveform = BasicWaveform(
            function=controls["waveform"].currentText(),
            frequency_hz=controls["frequency"].value(),
            amplitude_vpp=controls["amplitude"].value(),
            offset_v=controls["offset"].value(),
            phase_deg=controls["phase"].value(),
        )
        output = controls["output"].isChecked()

        def apply() -> None:
            self.driver.configure_basic(channel, waveform)
            self.driver.set_output(channel, output)

        self.submit(
            apply,
            on_success=lambda _=None: self.statusBar().showMessage(
                f"CH{channel} 参数已下发", 4000
            ),
        )

    def _modulation_page(self) -> QWidget:
        page, layout = page_shell("调制", "覆盖 DG4000 的模拟调制、数字键控和脉宽调制设置。")
        tabs = QTabWidget()
        for mode in ("AM", "FM", "PM", "ASK", "FSK", "PSK", "PWM"):
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)
            settings, settings_layout = panel(f"{mode} 参数")
            form = QFormLayout()
            modulation_channel = QComboBox()
            modulation_channel.addItem("CH1", 1)
            modulation_channel.addItem("CH2", 2)
            source = QComboBox()
            source.addItems(["内部", "外部"])
            shape = QComboBox()
            shape.addItems(["正弦", "方波", "三角波", "上锯齿", "下锯齿", "噪声"])
            internal_frequency = value_spin(100, 0.001, 1e6, "Hz")
            form.addRow("输出通道", modulation_channel)
            form.addRow("调制源", source)
            form.addRow("内部波形", shape)
            form.addRow("调制频率", internal_frequency)
            if mode == "FSK":
                parameter = value_spin(2000, 1e-6, 160e6, "Hz")
                form.addRow("跳跃频率", parameter)
            elif mode == "PSK":
                parameter = value_spin(180, 0, 360, "°", 3)
                form.addRow("调制相位", parameter)
            elif mode == "AM":
                parameter = value_spin(50, 0, 120, "%", 2)
                form.addRow("调制度", parameter)
            else:
                parameter = value_spin(10, 0, 1e8, "", 3)
                form.addRow("偏移/深度", parameter)
            settings_layout.addLayout(form)
            enabled = QCheckBox(f"启用 {mode}")
            settings_layout.addWidget(enabled)
            self.modulation_controls[mode] = {
                "channel": modulation_channel,
                "source": source,
                "shape": shape,
                "frequency": internal_frequency,
                "parameter": parameter,
                "enabled": enabled,
            }
            apply_button = QPushButton("应用调制设置")
            apply_button.setObjectName("PrimaryButton")
            apply_button.clicked.connect(lambda checked=False, selected=mode: self._apply_modulation(selected))
            settings_layout.addWidget(apply_button, alignment=Qt.AlignmentFlag.AlignLeft)
            tab_layout.addWidget(settings)
            tab_layout.addStretch()
            tabs.addTab(tab, mode)
        layout.addWidget(tabs)
        return page

    def _apply_modulation(self, mode: str) -> None:
        if not self._require_connection():
            return
        controls = self.modulation_controls[mode]
        source = "INT" if controls["source"].currentText() == "内部" else "EXT"
        channel = controls["channel"].currentData()
        internal_frequency = controls["frequency"].value() if source == "INT" else None
        parameter = controls["parameter"].value()
        enabled = controls["enabled"].isChecked()
        self.submit(
            lambda: self.driver.configure_modulation(
                channel,
                mode,
                source=source,
                internal_function=controls["shape"].currentText(),
                internal_frequency_hz=internal_frequency,
                parameter=parameter,
                enabled=enabled,
            ),
            on_success=lambda _=None: self.statusBar().showMessage(
                f"{mode} 参数已下发到 CH{channel}", 4000
            ),
        )

    def _sweep_burst_page(self) -> QWidget:
        page, layout = page_shell("扫频与脉冲串", "配置仪器原生扫频、标记频率、触发源及脉冲串周期。")
        tabs = QTabWidget()
        sweep = QWidget()
        sweep_layout = QVBoxLayout(sweep)
        sweep_panel, sweep_form_layout = panel("扫频设置")
        form = QFormLayout()
        mode = QComboBox()
        mode.addItems(["线性", "对数", "步进"])
        sweep_start = value_spin(1000, 1e-6, 160e6, "Hz")
        sweep_stop = value_spin(10000, 1e-6, 160e6, "Hz")
        sweep_time = value_spin(1, 0.001, 500, "s", 3)
        form.addRow("扫频类型", mode)
        form.addRow("起始频率", sweep_start)
        form.addRow("终止频率", sweep_stop)
        form.addRow("扫频时间", sweep_time)
        trigger = QComboBox()
        trigger.addItems(["内部", "外部", "手动"])
        form.addRow("触发源", trigger)
        sweep_form_layout.addLayout(form)
        sweep_apply = QPushButton("应用并启用扫频")
        sweep_apply.setObjectName("PrimaryButton")
        sweep_apply.clicked.connect(
            lambda: self._apply_sweep(mode, sweep_start, sweep_stop, sweep_time, trigger)
        )
        sweep_form_layout.addWidget(sweep_apply, alignment=Qt.AlignmentFlag.AlignLeft)
        sweep_layout.addWidget(sweep_panel)
        sweep_layout.addStretch()
        tabs.addTab(sweep, "扫频")

        burst = QWidget()
        burst_layout = QVBoxLayout(burst)
        burst_panel, burst_form_layout = panel("脉冲串设置")
        burst_form = QFormLayout()
        burst_mode = QComboBox()
        burst_mode.addItems(["N 周期", "无限", "门控"])
        count = QSpinBox()
        count.setRange(1, 1_000_000)
        count.setValue(10)
        burst_form.addRow("模式", burst_mode)
        burst_form.addRow("周期数", count)
        burst_period = value_spin(0.1, 1e-6, 500, "s")
        burst_phase = value_spin(0, 0, 360, "°", 3)
        burst_form.addRow("周期", burst_period)
        burst_form.addRow("起始相位", burst_phase)
        burst_form_layout.addLayout(burst_form)
        burst_apply = QPushButton("应用并启用脉冲串")
        burst_apply.setObjectName("PrimaryButton")
        burst_apply.clicked.connect(
            lambda: self._apply_burst(burst_mode, count, burst_period, burst_phase)
        )
        burst_form_layout.addWidget(burst_apply, alignment=Qt.AlignmentFlag.AlignLeft)
        burst_layout.addWidget(burst_panel)
        burst_layout.addStretch()
        tabs.addTab(burst, "脉冲串")
        layout.addWidget(tabs)
        return page

    def _apply_sweep(
        self,
        mode: QComboBox,
        start: QDoubleSpinBox,
        stop: QDoubleSpinBox,
        duration: QDoubleSpinBox,
        trigger: QComboBox,
    ) -> None:
        if not self._require_connection():
            return
        spacing = {"线性": "LIN", "对数": "LOG", "步进": "STEP"}[mode.currentText()]
        source = {"内部": "INT", "外部": "EXT", "手动": "MAN"}[trigger.currentText()]
        commands = [
            f":SOUR1:FREQ:START {start.value():.12g}",
            f":SOUR1:FREQ:STOP {stop.value():.12g}",
            f":SOUR1:SWE:TIME {duration.value():.12g}",
            f":SOUR1:SWE:SPAC {spacing}",
            f":SOUR1:SWE:TRIG:SOUR {source}",
            ":SOUR1:SWE:STAT ON",
        ]
        self.submit(
            lambda: self.driver.write_many(commands),
            on_success=lambda _=None: self.statusBar().showMessage("CH1 扫频已启用", 4000),
        )

    def _apply_burst(
        self,
        mode: QComboBox,
        count: QSpinBox,
        period: QDoubleSpinBox,
        phase: QDoubleSpinBox,
    ) -> None:
        if not self._require_connection():
            return
        mode_word = {"N 周期": "TRIG", "无限": "INF", "门控": "GAT"}[mode.currentText()]
        commands = [
            f":SOUR1:BURS:MODE {mode_word}",
            f":SOUR1:BURS:NCYC {count.value()}",
            f":SOUR1:BURS:INT:PER {period.value():.12g}",
            f":SOUR1:BURS:PHAS {phase.value():.12g}",
            ":SOUR1:BURS ON",
        ]
        self.submit(
            lambda: self.driver.write_many(commands),
            on_success=lambda _=None: self.statusBar().showMessage("CH1 脉冲串已启用", 4000),
        )

    def _trigger_step_page(self) -> QWidget:
        page, layout = page_shell(
            "NI 触发步进",
            "NI 只负责检测外部触发；每次计数增加后，软件按序向 DG4000 下发新的 SCPI 参数。第一个触发应用起始点，末点后自动循环。",
        )
        config, config_layout = panel("任务配置")
        form = QFormLayout()
        self.step_target = QComboBox()
        for parameter in StepParameter:
            self.step_target.addItem(parameter.label, parameter)
        self.ni_source = QComboBox()
        if self.ni_devices:
            for device in self.ni_devices:
                self.ni_source.addItem(f"{device.name} ({device.resource})", device.id)
        else:
            self.ni_source.addItem("请先在设备中心添加 NI 采集卡", None)
            self.ni_source.setEnabled(False)
        self.step_channel = QComboBox()
        self.step_channel.addItem("CH1", 1)
        self.step_channel.addItem("CH2", 2)
        self.step_counter = QComboBox()
        for index in range(4):
            self.step_counter.addItem(f"ctr{index}", f"ctr{index}")
        self.step_terminal = QComboBox()
        self.step_terminal.setEditable(True)
        self.step_terminal.addItems([f"PFI{index}" for index in range(16)])
        self.step_edge = QComboBox()
        self.step_edge.addItems(["上升沿", "下降沿"])
        form.addRow("步进参数", self.step_target)
        form.addRow("DG4000 通道", self.step_channel)
        form.addRow("NI 计数设备", self.ni_source)
        form.addRow("NI 计数器", self.step_counter)
        form.addRow("PFI 触发端子", self.step_terminal)
        form.addRow("触发边沿", self.step_edge)
        command = QLabel(":SOUR1:FREQ")
        self.step_command = command
        command.setStyleSheet("font-family:Consolas; color:#55d9cc;")
        form.addRow("SCPI 模板", command)
        if is_virtual_resource(self.record.resource) and self.connection_manager is not None:
            self.simulation_delay = QSpinBox()
            self.simulation_delay.setRange(0, 2000)
            self.simulation_delay.setSuffix(" ms")
            self.simulation_delay.setValue(
                self.connection_manager.virtual_lab.generator_snapshot(
                    self.record.resource
                ).scpi_delay_ms
            )
            self.simulation_delay.setToolTip("增大延迟可模拟真实 VISA 较慢时的触发积压")
            self.simulation_delay.valueChanged.connect(
                lambda value: self.connection_manager.virtual_lab.set_scpi_delay(
                    self.record.resource, value
                )
            )
            form.addRow("模拟下发延迟", self.simulation_delay)
        self.step_channel.currentIndexChanged.connect(self._update_step_command)
        config_layout.addLayout(form)

        sequence_panel, sequence_layout = panel("循环序列")
        generator = QGridLayout()
        generator.setHorizontalSpacing(10)
        generator.setVerticalSpacing(5)
        self.scan_start = scan_value_spin(1000, "Hz")
        self.scan_step = scan_value_spin(1000, "Hz")
        self.scan_point_count = QSpinBox()
        self.scan_point_count.setRange(1, MAX_SCAN_POINTS)
        self.scan_point_count.setValue(4)
        self.scan_point_count.setSuffix(" 点")
        self.scan_point_count.setToolTip(
            f"自动生成最多 {MAX_SCAN_POINTS} 个点；允许正步长、负步长和零步长"
        )
        generate_scan = QPushButton("生成扫描表")
        generate_scan.setObjectName("PrimaryButton")
        generate_scan.clicked.connect(self._generate_scan_table)
        generator.addWidget(QLabel("扫描起点"), 0, 0)
        generator.addWidget(QLabel("扫描步长"), 0, 1)
        generator.addWidget(QLabel("扫描点数"), 0, 2)
        generator.addWidget(self.scan_start, 1, 0)
        generator.addWidget(self.scan_step, 1, 1)
        generator.addWidget(self.scan_point_count, 1, 2)
        generator.addWidget(generate_scan, 1, 3)
        generator.setColumnStretch(0, 2)
        generator.setColumnStretch(1, 2)
        generator.setColumnStretch(2, 1)
        sequence_layout.addLayout(generator)

        toolbar = QHBoxLayout()
        add_point = QPushButton("添加点")
        remove_point = QPushButton("删除选中")
        toolbar.addWidget(add_point)
        toolbar.addWidget(remove_point)
        toolbar.addStretch()
        sequence_layout.addLayout(toolbar)
        self.step_table = QTableWidget(0, 3)
        self.step_table.setHorizontalHeaderLabels(["序号", "参数值", "说明"])
        self.step_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.step_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.step_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.step_table.setAlternatingRowColors(True)
        self._generate_scan_table(show_message=False)
        add_point.clicked.connect(lambda: self._append_sequence_row(self.step_table, 1000, ""))
        remove_point.clicked.connect(lambda: self._remove_sequence_rows(self.step_table))
        self.step_target.currentIndexChanged.connect(self._step_parameter_changed)
        self._scan_settings: dict[StepParameter, tuple[float, float]] = {
            StepParameter.FREQUENCY: (1000, 1000),
            StepParameter.AMPLITUDE: (1, 0.1),
            StepParameter.PHASE: (0, 45),
            StepParameter.PSK_PHASE: (0, 45),
            StepParameter.FSK_FREQUENCY: (1000, 1000),
        }
        self._active_scan_parameter = self.step_target.currentData()
        self._update_step_command()
        sequence_layout.addWidget(self.step_table)
        config.setMinimumWidth(370)
        upper = QHBoxLayout()
        upper.setSpacing(14)
        upper.addWidget(config, 1)
        upper.addWidget(sequence_panel, 2)
        layout.addLayout(upper, 1)

        status = QHBoxLayout()
        self.trigger_metrics: dict[str, QLabel] = {}
        for key, caption, value, color in (
            ("received", "已收到触发", "0", "#8f7cff"),
            ("applied", "已完成下发", "0", "#55d9cc"),
            ("backlog", "待处理积压", "0", "#f0b95d"),
            ("current", "当前点", "—", "#e7ebf4"),
        ):
            card, number = metric_card(caption, value, color)
            self.trigger_metrics[key] = number
            status.addWidget(card)
        layout.addLayout(status)

        actions = QHBoxLayout()
        self.trigger_start = QPushButton("启动联动")
        self.trigger_start.setObjectName("PrimaryButton")
        self.trigger_start.clicked.connect(self._start_trigger)
        self.trigger_stop = QPushButton("停止")
        self.trigger_stop.setEnabled(False)
        self.trigger_stop.clicked.connect(self._stop_trigger)
        clear = QPushButton("显示清零")
        clear.clicked.connect(self._clear_trigger_display)
        actions.addWidget(self.trigger_start)
        actions.addWidget(self.trigger_stop)
        actions.addWidget(clear)
        actions.addStretch()
        note = QLabel("不设置最小触发间隔；过快触发进入有序积压队列")
        note.setObjectName("Muted")
        actions.addWidget(note)
        layout.addLayout(actions)
        return page

    def _update_step_command(self, _index: int = -1) -> None:
        self.step_command.setText(
            self.step_target.currentData().command(self.step_channel.currentData())
        )

    def _step_parameter_changed(self, _index: int = -1) -> None:
        previous = getattr(self, "_active_scan_parameter", None)
        if previous is not None:
            self._scan_settings[previous] = (self.scan_start.value(), self.scan_step.value())
        current = self.step_target.currentData()
        start, step = self._scan_settings[current]
        unit = current.unit
        self.scan_start.setSuffix(f" {unit}")
        self.scan_step.setSuffix(f" {unit}")
        self.scan_start.setValue(start)
        self.scan_step.setValue(step)
        self._active_scan_parameter = current
        self._update_step_command()

    def _generate_scan_table(self, _checked: bool = False, *, show_message: bool = True) -> None:
        try:
            values = linear_scan_points(
                self.scan_start.value(),
                self.scan_step.value(),
                self.scan_point_count.value(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "无法生成扫描表", str(exc))
            return
        self.step_table.setUpdatesEnabled(False)
        try:
            self.step_table.setRowCount(0)
            for index, value in enumerate(values):
                note = "起始点（第一个触发）" if index == 0 else ""
                self._append_sequence_row(self.step_table, value, note)
        finally:
            self.step_table.setUpdatesEnabled(True)
            self.step_table.viewport().update()
        if show_message:
            end = values[-1]
            self.statusBar().showMessage(
                f"已生成 {len(values)} 个扫描点：{values[0]:.12g} → {end:.12g}",
                5000,
            )

    def _sequence_values(self) -> list[float]:
        values: list[float] = []
        for row in range(self.step_table.rowCount()):
            item = self.step_table.item(row, 1)
            try:
                values.append(float(item.text().strip()))
            except (AttributeError, ValueError) as exc:
                raise ValueError(f"第 {row + 1} 个步进点不是有效数值") from exc
        if not values:
            raise ValueError("至少需要一个步进点")
        return values

    def _start_trigger(self) -> None:
        if not self._require_connection():
            return
        ni_id = self.ni_source.currentData()
        ni_record = next((item for item in self.ni_devices if item.id == ni_id), None)
        if ni_record is None:
            QMessageBox.warning(self, "缺少 NI 设备", "请先在设备中心添加并选择 NI 采集卡。")
            return
        try:
            values = self._sequence_values()
        except ValueError as exc:
            QMessageBox.warning(self, "序列无效", str(exc))
            return
        counter = (
            self.connection_manager.counter_for(
                ni_record,
                counter=self.step_counter.currentData(),
                terminal=self.step_terminal.currentText(),
                rising=self.step_edge.currentText() == "上升沿",
            )
            if self.connection_manager is not None
            else NIDaqEdgeCounter(
                ni_record.resource,
                counter=self.step_counter.currentData(),
                terminal=self.step_terminal.currentText(),
                rising=self.step_edge.currentText() == "上升沿",
            )
        )
        controller = TriggerStepController(
            counter,
            self.driver,
            self.step_target.currentData(),
            self.step_channel.currentData(),
            values,
        )
        controller.add_callback(self.trigger_bridge.updated.emit)
        self.controller = controller
        self._last_trigger_error = None
        self.trigger_start.setEnabled(False)
        self.trigger_start.setText("启动中…")
        self.submit(
            controller.start,
            on_success=lambda _=None: self.statusBar().showMessage("NI 触发步进已启动", 4000),
            on_error=self._trigger_start_failed,
        )

    def _trigger_start_failed(self, message: str) -> None:
        self.controller = None
        self.trigger_start.setText("启动联动")
        self.trigger_start.setEnabled(True)
        self.trigger_stop.setEnabled(False)
        self._show_hardware_error(message)

    def _stop_trigger(self) -> None:
        controller = self.controller
        if controller is None:
            return
        self.trigger_stop.setEnabled(False)

        def stopped(_result: Any = None) -> None:
            self.controller = None
            self.trigger_start.setText("启动联动")
            self.trigger_start.setEnabled(True)
            self.statusBar().showMessage("触发步进已停止", 4000)

        self.submit(controller.stop, on_success=stopped)

    def _trigger_updated(self, snapshot: TriggerSnapshot) -> None:
        self.trigger_metrics["received"].setText(str(snapshot.received_count))
        self.trigger_metrics["applied"].setText(str(snapshot.applied_count))
        self.trigger_metrics["backlog"].setText(str(snapshot.backlog))
        if snapshot.current_index is None:
            self.trigger_metrics["current"].setText("—")
        else:
            self.trigger_metrics["current"].setText(
                f"#{snapshot.current_index + 1} · {snapshot.current_value:g}"
            )
        running = snapshot.state in {ControllerState.STARTING, ControllerState.RUNNING}
        self.trigger_stop.setEnabled(running or snapshot.state is ControllerState.ERROR)
        if snapshot.state is ControllerState.RUNNING:
            self.trigger_start.setText("运行中")
        elif snapshot.state is ControllerState.ERROR:
            self.trigger_start.setText("任务错误")
        if snapshot.error and snapshot.error != self._last_trigger_error:
            self._last_trigger_error = snapshot.error
            self.statusBar().showMessage(snapshot.error, 10000)

    def _clear_trigger_display(self) -> None:
        if self.controller and self.controller.snapshot().state is ControllerState.RUNNING:
            QMessageBox.information(self, "任务正在运行", "请先停止任务，再清除界面计数。")
            return
        for key in ("received", "applied", "backlog"):
            self.trigger_metrics[key].setText("0")
        self.trigger_metrics["current"].setText("—")

    @staticmethod
    def _append_sequence_row(table: QTableWidget, value: float, note: str) -> None:
        row = table.rowCount()
        table.insertRow(row)
        number = QTableWidgetItem(str(row + 1))
        number.setFlags(number.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(row, 0, number)
        table.setItem(row, 1, QTableWidgetItem(f"{value:.12g}"))
        table.setItem(row, 2, QTableWidgetItem(note))

    @staticmethod
    def _remove_sequence_rows(table: QTableWidget) -> None:
        rows = sorted({index.row() for index in table.selectedIndexes()}, reverse=True)
        if not rows and table.currentRow() >= 0:
            rows = [table.currentRow()]
        for row in rows:
            if table.rowCount() > 1:
                table.removeRow(row)
        for row in range(table.rowCount()):
            table.item(row, 0).setText(str(row + 1))
            if row == 0 and not table.item(row, 2).text():
                table.item(row, 2).setText("起始点（第一个触发）")

    def _system_page(self) -> QWidget:
        page, layout = page_shell("存储与系统", "管理仪器状态文件、SCPI 控制台、显示和系统命令。")
        storage, storage_layout = panel("仪器状态")
        buttons = QHBoxLayout()
        state_commands = {
            "保存状态": "*SAV 1",
            "调用状态": "*RCL 1",
            "恢复默认": "*RST",
            "读取错误队列": ":SYST:ERR?",
        }
        for label, command in state_commands.items():
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, selected=command: self._execute_scpi(selected))
            buttons.addWidget(button)
        buttons.addStretch()
        storage_layout.addLayout(buttons)
        layout.addWidget(storage)
        console, console_layout = panel("SCPI 控制台")
        self.scpi_input = QLineEdit()
        self.scpi_input.setPlaceholderText("输入查询或命令，例如 *IDN?")
        self.scpi_input.returnPressed.connect(lambda: self._execute_scpi(self.scpi_input.text()))
        self.scpi_log = QPlainTextEdit()
        self.scpi_log.setReadOnly(True)
        self.scpi_log.setPlaceholderText("命令和返回值将在此显示")
        self.scpi_log.setMaximumBlockCount(1000)
        console_layout.addWidget(self.scpi_input)
        console_layout.addWidget(self.scpi_log)
        execute = QPushButton("执行")
        execute.setObjectName("PrimaryButton")
        execute.clicked.connect(lambda: self._execute_scpi(self.scpi_input.text()))
        console_layout.addWidget(execute, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(console, 1)
        return page

    def _execute_scpi(self, command: str) -> None:
        command = command.strip()
        if not command:
            return
        if not self._require_connection():
            return

        def operation() -> str:
            if command.endswith("?"):
                return self.driver.query(command)
            self.driver.write(command)
            return "已发送"

        def completed(result: Any) -> None:
            if hasattr(self, "scpi_log"):
                self.scpi_log.appendPlainText(f"> {command}\n{result}")
            else:
                self.statusBar().showMessage(f"{command} → {result}", 8000)

        self.submit(operation, on_success=completed)

    def closeEvent(self, event: QCloseEvent) -> None:
        if hasattr(self, "virtual_generator_timer"):
            self.virtual_generator_timer.stop()
        if self.controller is not None:
            self.controller.stop()
            self.controller = None
        if self.driver.is_connected and self.connection_manager is None:
            try:
                self.driver.disconnect()
            except Exception:
                pass
        super().closeEvent(event)


class NIDeviceWindow(BaseDeviceWindow):
    def __init__(
        self,
        record: DeviceRecord,
        connection_manager: DeviceConnectionManager | None = None,
    ) -> None:
        self.probe_service = DeviceProbeService()
        self.connection_manager = connection_manager
        self.test_counter: NIDaqEdgeCounter | None = None
        self.counter_read_pending = False
        navigation = [
            ("总览", self._overview_page),
            ("触发输入", self._trigger_page),
            ("计数器", self._counter_page),
            ("路由", self._routing_page),
            ("诊断", self._diagnostics_page),
        ]
        super().__init__(record, navigation)
        if self.connection_manager is not None and self.connection_manager.is_connected(record):
            self._ni_connected(self.connection_manager.info(record))

    def _overview_page(self) -> QWidget:
        page, layout = page_shell("NI 采集卡总览", "该设备只承担外部触发检测与计数，不采集 DG4000 输出，也不通过 AO 控制参数。")
        metrics = QHBoxLayout()
        for caption, value, color in (
            ("设备状态", "未连接", "#f0b95d"),
            ("触发任务", "未运行", "#8995aa"),
            ("累计触发", "0", "#8f7cff"),
            ("缓冲积压", "0", "#55d9cc"),
        ):
            card, _ = metric_card(caption, value, color)
            metrics.addWidget(card)
        layout.addLayout(metrics)
        info, info_layout = panel("设备信息")
        form = QFormLayout()
        form.addRow("NI 设备名", QLabel(self.record.resource))
        form.addRow("连接方式", QLabel(self.record.connection or "NI-DAQmx 本机设备"))
        form.addRow("用途", QLabel("外部边沿触发计数 → 通知 SCPI 下发队列"))
        self.ni_identity = QLabel("检测后读取产品类型和序列号")
        self.ni_identity.setWordWrap(True)
        form.addRow("驱动报告", self.ni_identity)
        info_layout.addLayout(form)
        connection_buttons = QHBoxLayout()
        self.ni_connect_button = QPushButton("连接并读取能力")
        self.ni_connect_button.setObjectName("PrimaryButton")
        self.ni_disconnect_button = QPushButton("断开")
        self.ni_disconnect_button.setEnabled(False)
        self.ni_connect_button.clicked.connect(self._connect_ni)
        self.ni_disconnect_button.clicked.connect(self._disconnect_ni)
        connection_buttons.addWidget(self.ni_connect_button)
        connection_buttons.addWidget(self.ni_disconnect_button)
        connection_buttons.addStretch()
        info_layout.addLayout(connection_buttons)
        layout.addWidget(info)
        if is_virtual_resource(self.record.resource) and self.connection_manager is not None:
            virtual_panel, virtual_layout = panel("虚拟触发控制台")
            virtual_note = QLabel(
                "无需接线：先在 DG4000 的“触发步进”页启动任务，再从这里发送单次、批量或连续触发。"
            )
            virtual_note.setObjectName("Muted")
            virtual_note.setWordWrap(True)
            virtual_layout.addWidget(virtual_note)
            controls = QHBoxLayout()
            emit_one = QPushButton("发送 1 次触发")
            emit_one.setObjectName("PrimaryButton")
            emit_one.clicked.connect(lambda: self._emit_virtual_triggers(1))
            self.virtual_batch_count = QSpinBox()
            self.virtual_batch_count.setRange(1, 100000)
            self.virtual_batch_count.setValue(10)
            emit_batch = QPushButton("批量发送")
            emit_batch.clicked.connect(
                lambda: self._emit_virtual_triggers(self.virtual_batch_count.value())
            )
            controls.addWidget(emit_one)
            controls.addWidget(self.virtual_batch_count)
            controls.addWidget(emit_batch)
            controls.addStretch()
            virtual_layout.addLayout(controls)

            continuous = QHBoxLayout()
            self.virtual_interval = QSpinBox()
            self.virtual_interval.setRange(1, 60000)
            self.virtual_interval.setValue(100)
            self.virtual_interval.setSuffix(" ms/次")
            self.virtual_continuous_start = QPushButton("开始连续触发")
            self.virtual_continuous_stop = QPushButton("停止连续触发")
            self.virtual_continuous_stop.setEnabled(False)
            self.virtual_continuous_start.clicked.connect(self._start_virtual_continuous)
            self.virtual_continuous_stop.clicked.connect(self._stop_virtual_continuous)
            continuous.addWidget(self.virtual_interval)
            continuous.addWidget(self.virtual_continuous_start)
            continuous.addWidget(self.virtual_continuous_stop)
            continuous.addStretch()
            self.virtual_total_label = QLabel(
                f"累计发送：{self.connection_manager.virtual_lab.trigger_total(self.record.resource)}"
            )
            self.virtual_total_label.setStyleSheet("color:#55d9cc; font-weight:700;")
            continuous.addWidget(self.virtual_total_label)
            virtual_layout.addLayout(continuous)
            self.virtual_trigger_timer = QTimer(self)
            self.virtual_trigger_timer.timeout.connect(lambda: self._emit_virtual_triggers(1))
            layout.addWidget(virtual_panel)
        layout.addStretch()
        return page

    def _emit_virtual_triggers(self, count: int) -> None:
        if self.connection_manager is None:
            return
        if not self.connection_manager.is_connected(self.record):
            QMessageBox.information(self, "虚拟 NI 未连接", "请先连接虚拟 NI 设备。")
            return
        total = self.connection_manager.virtual_lab.emit_trigger(self.record.resource, count)
        self.virtual_total_label.setText(f"累计发送：{total}")

    def _start_virtual_continuous(self) -> None:
        if self.connection_manager is None:
            return
        if not self.connection_manager.is_connected(self.record):
            QMessageBox.information(self, "虚拟 NI 未连接", "请先连接虚拟 NI 设备。")
            return
        self.virtual_trigger_timer.setInterval(self.virtual_interval.value())
        self.virtual_trigger_timer.start()
        self.virtual_continuous_start.setEnabled(False)
        self.virtual_continuous_stop.setEnabled(True)

    def _stop_virtual_continuous(self) -> None:
        if hasattr(self, "virtual_trigger_timer"):
            self.virtual_trigger_timer.stop()
        if hasattr(self, "virtual_continuous_start"):
            self.virtual_continuous_start.setEnabled(True)
            self.virtual_continuous_stop.setEnabled(False)

    def _connect_ni(self) -> None:
        self.ni_connect_button.setEnabled(False)

        if self.connection_manager is not None:
            self.submit(
                lambda: self.connection_manager.connect(self.record),
                on_success=self._ni_connected,
                on_error=self._ni_connection_failed,
            )
            return

        def completed(result: ProbeResult) -> None:
            self.ni_identity.setText(result.identity or result.message)
            if result.state is ProbeState.ONLINE:
                self._ni_connected(ConnectionInfo(self.record.id, True, result.identity, result.message))
            else:
                self._ni_connection_failed(result.message)

        self.submit(lambda: self.probe_service.probe(self.record), on_success=completed)

    def _ni_connected(self, result: Any) -> None:
        identity = result.identity if isinstance(result, ConnectionInfo) else str(result)
        self.ni_identity.setText(identity or "NI-DAQmx 已识别设备")
        self.ni_connect_button.setEnabled(False)
        self.ni_connect_button.setText("已连接")
        self.ni_disconnect_button.setEnabled(True)
        self.driver_status.setObjectName("StatusOnline")
        self.driver_status.setText("● 已连接")
        self.driver_status.style().unpolish(self.driver_status)
        self.driver_status.style().polish(self.driver_status)
        self.statusBar().showMessage("NI 设备已由 NI-DAQmx 识别", 5000)

    def _ni_connection_failed(self, message: str) -> None:
        self.ni_connect_button.setEnabled(True)
        self.ni_connect_button.setText("连接并读取能力")
        self._show_hardware_error(message)

    def _disconnect_ni(self) -> None:
        self.ni_disconnect_button.setEnabled(False)
        if self.test_counter is not None:
            self._stop_ni_counter()
        if self.connection_manager is None:
            self._ni_disconnected()
            return
        self.submit(
            lambda: self.connection_manager.disconnect(self.record),
            on_success=self._ni_disconnected,
        )

    def _ni_disconnected(self, _result: Any = None) -> None:
        self.ni_identity.setText("检测后读取产品类型和序列号")
        self.ni_connect_button.setText("连接并读取能力")
        self.ni_connect_button.setEnabled(True)
        self.ni_disconnect_button.setEnabled(False)
        self.driver_status.setObjectName("StatusPending")
        self.driver_status.setText("● 驱动尚未连接")
        self.driver_status.style().unpolish(self.driver_status)
        self.driver_status.style().polish(self.driver_status)
        self.statusBar().showMessage("NI 设备连接已释放", 4000)

    def _trigger_page(self) -> QWidget:
        page, layout = page_shell("触发输入", "选择接收外部触发的 PFI 端子和有效边沿；不配置模拟输入或模拟输出。")
        settings, settings_layout = panel("数字边沿")
        form = QFormLayout()
        terminal = QComboBox()
        terminal.setEditable(True)
        terminal.addItems([f"/{self.record.resource}/PFI{i}" for i in range(16)])
        edge = QComboBox()
        edge.addItems(["上升沿", "下降沿"])
        debounce = QCheckBox("启用 NI 硬件数字滤波（可选）")
        form.addRow("输入端子", terminal)
        form.addRow("有效边沿", edge)
        form.addRow("输入处理", debounce)
        settings_layout.addLayout(form)
        layout.addWidget(settings)
        note, note_layout = panel("触发规则")
        label = QLabel(
            "软件不设置最小触发间隔。硬件计数器保存实际收到的边沿总数，控制程序比较“已收到”和“已下发”计数，"
            "逐项补发全部参数；因此快速触发不会被软件节流，但 DG4000 的生效时刻可能落后于触发。"
        )
        label.setWordWrap(True)
        label.setObjectName("Muted")
        note_layout.addWidget(label)
        layout.addWidget(note)
        layout.addStretch()
        return page

    def _counter_page(self) -> QWidget:
        page, layout = page_shell("计数器", "为联动任务选择硬件边沿计数器并查看单调递增的触发总数。")
        settings, settings_layout = panel("计数任务")
        form = QFormLayout()
        counter = QComboBox()
        counter.addItems([f"{self.record.resource}/ctr{i}" for i in range(4)])
        terminal = QComboBox()
        terminal.setEditable(True)
        terminal.addItems([f"/{self.record.resource}/PFI{i}" for i in range(16)])
        edge = QComboBox()
        edge.addItems(["上升沿", "下降沿"])
        sample_mode = QComboBox()
        sample_mode.addItems(["连续硬件计数", "按需读取累计值"])
        form.addRow("计数器", counter)
        form.addRow("PFI 端子", terminal)
        form.addRow("有效边沿", edge)
        form.addRow("读取方式", sample_mode)
        settings_layout.addLayout(form)
        count_card, self.ni_count_label = metric_card("当前累计边沿", "0", "#8f7cff")
        settings_layout.addWidget(count_card)
        controls = QHBoxLayout()
        start = QPushButton("开始计数")
        start.setObjectName("PrimaryButton")
        self.ni_counter_start = start
        self.ni_counter_stop = QPushButton("停止")
        self.ni_counter_stop.setEnabled(False)
        start.clicked.connect(lambda: self._start_ni_counter(counter, terminal, edge))
        self.ni_counter_stop.clicked.connect(self._stop_ni_counter)
        controls.addWidget(start)
        controls.addWidget(self.ni_counter_stop)
        controls.addStretch()
        settings_layout.addLayout(controls)
        self.ni_counter_timer = QTimer(self)
        self.ni_counter_timer.setInterval(100)
        self.ni_counter_timer.timeout.connect(self._read_ni_counter)
        layout.addWidget(settings)
        layout.addStretch()
        return page

    def _start_ni_counter(self, counter: QComboBox, terminal: QComboBox, edge: QComboBox) -> None:
        if self.test_counter is not None:
            return
        self.test_counter = (
            self.connection_manager.counter_for(
                self.record,
                counter=counter.currentText(),
                terminal=terminal.currentText(),
                rising=edge.currentText() == "上升沿",
            )
            if self.connection_manager is not None
            else NIDaqEdgeCounter(
                self.record.resource,
                counter=counter.currentText(),
                terminal=terminal.currentText(),
                rising=edge.currentText() == "上升沿",
            )
        )
        self.ni_counter_start.setEnabled(False)

        def started(_result: Any = None) -> None:
            self.ni_counter_stop.setEnabled(True)
            self.ni_counter_timer.start()
            self.statusBar().showMessage("NI 累计边沿计数已启动", 4000)

        def failed(message: str) -> None:
            self.test_counter = None
            self.ni_counter_start.setEnabled(True)
            self._show_hardware_error(message)

        self.submit(self.test_counter.start, on_success=started, on_error=failed)

    def _read_ni_counter(self) -> None:
        if self.test_counter is None or self.counter_read_pending:
            return
        self.counter_read_pending = True

        def received(value: Any) -> None:
            self.counter_read_pending = False
            self.ni_count_label.setText(str(value))

        def failed(message: str) -> None:
            self.counter_read_pending = False
            self.statusBar().showMessage(message, 8000)

        self.submit(self.test_counter.read_count, on_success=received, on_error=failed)

    def _stop_ni_counter(self) -> None:
        counter, self.test_counter = self.test_counter, None
        if counter is None:
            return
        self.ni_counter_timer.stop()
        self.ni_counter_stop.setEnabled(False)

        def stopped(_result: Any = None) -> None:
            self.ni_counter_start.setEnabled(True)
            self.statusBar().showMessage("NI 计数已停止", 4000)

        self.submit(counter.stop, on_success=stopped)

    def _routing_page(self) -> QWidget:
        page, layout = page_shell("端子与路由", "记录外部触发接线并检查 PFI/计数器端子映射。")
        routes, routes_layout = panel("建议接线")
        text = QLabel(
            "外部触发源 → NI PFI 输入；NI 与触发源共地。DG4000 通过 USB/LAN 接收软件 SCPI 命令，"
            "无需将 NI AO 接到 DG4000，也无需将 DG4000 输出接回 NI。"
        )
        text.setWordWrap(True)
        routes_layout.addWidget(text)
        layout.addWidget(routes)
        layout.addStretch()
        return page

    def _diagnostics_page(self) -> QWidget:
        page, layout = page_shell("诊断", "检查 NI-DAQmx 安装、设备枚举、端子能力与最近的计数错误。")
        diagnostics, diagnostics_layout = panel("本机环境")
        checks = QLabel("○ NI-DAQmx Python 包\n○ NI-DAQmx 系统驱动\n○ 设备名匹配\n○ PFI/计数器资源可用")
        checks.setObjectName("Muted")
        diagnostics_layout.addWidget(checks)
        run = QPushButton("运行诊断")
        run.setObjectName("PrimaryButton")
        run.clicked.connect(self._connect_ni)
        diagnostics_layout.addWidget(run, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(diagnostics)
        layout.addStretch()
        return page

    def closeEvent(self, event: QCloseEvent) -> None:
        self._stop_virtual_continuous()
        if self.test_counter is not None:
            self.ni_counter_timer.stop()
            try:
                self.test_counter.stop()
            except Exception:
                pass
            self.test_counter = None
        super().closeEvent(event)


class GenericScpiWindow(BaseDeviceWindow):
    def __init__(
        self,
        record: DeviceRecord,
        connection_manager: DeviceConnectionManager | None = None,
    ) -> None:
        self.connection_manager = connection_manager
        self.session = (
            connection_manager.session_for(record)
            if connection_manager is not None
            else ScpiSession(record.resource)
        )
        super().__init__(record, [("SCPI 控制台", self._console_page)])
        if self.session.is_connected:
            identity = (
                self.connection_manager.info(record).identity
                if self.connection_manager is not None
                else self.session.query("*IDN?")
            )
            self._generic_connected(identity)

    def _console_page(self) -> QWidget:
        page, layout = page_shell("通用 SCPI 设备", "连接 VISA 资源并手动发送命令或查询。")
        connection, connection_layout = panel("VISA 连接")
        row = QHBoxLayout()
        self.generic_status = QLabel("● 未连接")
        self.generic_status.setObjectName("StatusPending")
        self.generic_connect = QPushButton("连接并读取 *IDN?")
        self.generic_connect.setObjectName("PrimaryButton")
        self.generic_connect.clicked.connect(self._generic_connect)
        self.generic_disconnect = QPushButton("断开")
        self.generic_disconnect.setEnabled(False)
        self.generic_disconnect.clicked.connect(self._generic_disconnect)
        row.addWidget(self.generic_status)
        row.addStretch()
        row.addWidget(self.generic_connect)
        row.addWidget(self.generic_disconnect)
        connection_layout.addLayout(row)
        layout.addWidget(connection)
        console, console_layout = panel("命令控制台")
        self.generic_input = QLineEdit()
        self.generic_input.setPlaceholderText("例如 *IDN?")
        self.generic_input.returnPressed.connect(self._generic_send)
        self.generic_log = QPlainTextEdit()
        self.generic_log.setReadOnly(True)
        console_layout.addWidget(self.generic_input)
        console_layout.addWidget(self.generic_log)
        send = QPushButton("发送")
        send.setObjectName("PrimaryButton")
        send.clicked.connect(self._generic_send)
        console_layout.addWidget(send, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(console, 1)
        return page

    def _generic_connect(self) -> None:
        self.generic_connect.setEnabled(False)

        def failed(message: str) -> None:
            self.generic_connect.setEnabled(True)
            self._show_hardware_error(message)

        self.submit(
            lambda: (
                self.connection_manager.connect(self.record)
                if self.connection_manager is not None
                else self.session.connect()
            ),
            on_success=self._generic_connected,
            on_error=failed,
        )

    def _generic_connected(self, identity: Any) -> None:
        identity_text = identity.identity if isinstance(identity, ConnectionInfo) else str(identity)
        self.generic_status.setText("● 已连接")
        self.generic_status.setObjectName("StatusOnline")
        self.generic_status.style().unpolish(self.generic_status)
        self.generic_status.style().polish(self.generic_status)
        if identity_text:
            self.generic_log.appendPlainText(identity_text)
        self.generic_connect.setText("已连接")
        self.generic_connect.setEnabled(False)
        self.generic_disconnect.setEnabled(True)
        self.driver_status.setObjectName("StatusOnline")
        self.driver_status.setText("● 已连接")
        self.driver_status.style().unpolish(self.driver_status)
        self.driver_status.style().polish(self.driver_status)

    def _generic_disconnect(self) -> None:
        self.generic_disconnect.setEnabled(False)
        self.submit(
            lambda: (
                self.connection_manager.disconnect(self.record)
                if self.connection_manager is not None
                else self.session.disconnect()
            ),
            on_success=self._generic_disconnected,
        )

    def _generic_disconnected(self, _result: Any = None) -> None:
        self.generic_status.setText("● 未连接")
        self.generic_status.setObjectName("StatusPending")
        self.generic_status.style().unpolish(self.generic_status)
        self.generic_status.style().polish(self.generic_status)
        self.generic_connect.setText("连接并读取 *IDN?")
        self.generic_connect.setEnabled(True)
        self.generic_disconnect.setEnabled(False)
        self.driver_status.setObjectName("StatusPending")
        self.driver_status.setText("● 驱动尚未连接")
        self.driver_status.style().unpolish(self.driver_status)
        self.driver_status.style().polish(self.driver_status)

    def _generic_send(self) -> None:
        command = self.generic_input.text().strip()
        if not command:
            return
        if not self.session.is_connected:
            QMessageBox.information(self, "尚未连接", "请先连接 VISA 资源。")
            return

        def operation() -> str:
            if command.endswith("?"):
                return self.session.query(command)
            self.session.write(command)
            return "已发送"

        self.submit(
            operation,
            on_success=lambda result: self.generic_log.appendPlainText(f"> {command}\n{result}"),
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.session.is_connected and self.connection_manager is None:
            try:
                self.session.disconnect()
            except Exception:
                pass
        super().closeEvent(event)


def create_device_window(
    record: DeviceRecord,
    all_devices: list[DeviceRecord],
    connection_manager: DeviceConnectionManager | None = None,
) -> BaseDeviceWindow:
    if record.kind is DeviceKind.DG4000:
        ni_devices = [item for item in all_devices if item.kind is DeviceKind.NI_DAQ]
        return DG4000Window(record, ni_devices, connection_manager)
    if record.kind is DeviceKind.NI_DAQ:
        return NIDeviceWindow(record, connection_manager)
    return GenericScpiWindow(record, connection_manager)
