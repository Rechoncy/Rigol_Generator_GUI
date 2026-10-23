from __future__ import annotations

import re
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from uuid import uuid4

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
                            QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                            QPushButton, QSpinBox, QScrollArea, QHeaderView,
                            QProgressBar, QSizePolicy, QTableView, QTabWidget, QWidget, QFrame)

from ..analog import AnalogAcquisitionController
from ..connections import DeviceConnectionManager
from ..drivers.newport_smc100 import MotionError
from ..drivers.ni_analog import AnalogInputBlock, AnalogInputConfig, TERMINAL_MODES
from ..models import DeviceKind
from ..repository import default_data_dir
from ..rotation_scan import RotationScanConfig, RotationScanController
from ..virtual_lab import is_virtual_resource
from .device_windows import BaseDeviceWindow, page_shell, panel
from .rotation_table import RotationPointTableModel
from .widgets import metric_card
from .waveform import WaveformPlot


PHASE_LABELS = {"preparing": "准备中", "holding": "保持 / 等待触发", "moving": "移动中",
                "settling": "稳定等待", "stopped": "已停止", "error": "错误停止"}


class NewportWindow(BaseDeviceWindow):
    def __init__(self, record, all_devices, connection_manager=None, *,
                 device_provider=None, parameter_repository=None):
        self.connection_manager = connection_manager or DeviceConnectionManager()
        self._owns_manager = connection_manager is None
        self.device_provider = device_provider
        self.ni_devices = [d for d in all_devices if d.kind is DeviceKind.NI_DAQ]
        self.device = self.connection_manager.newport_device_for(record)
        self.controller = None
        self.analog = None
        self._manual_busy = False
        self._scan_stop_pending = False
        self._cancel_manual = Event()
        self._manual_done = Event()
        self._manual_done.set()
        self._manual_owner = f"newport-manual:{record.id}"
        self._ni_signature = None
        self._last_plot_end = 0
        self._last_position = None
        self._last_velocity = None
        self._saved_ni_id = None
        self._display_run = False
        self._run_position_valid = False
        self._plan_signature = None
        self._follow_row = None
        self._scan_message = ""
        super().__init__(record, [("运动控制", self._motion_page),
                                 ("NI 循环旋转步进", self._scan_page),
                                 ("联动连续采集", self._analog_page)], parameter_repository)
        self.sidebar_stop = QPushButton("停止旋转 / 扫描")
        self.sidebar_stop.setObjectName("DangerButton")
        self.sidebar_stop.clicked.connect(self._stop)
        sidebar = self.driver_status.parentWidget().layout()
        sidebar.insertWidget(sidebar.count() - 1, self.sidebar_stop)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._tick()

    def _number(self, key, value, low, high, suffix="", decimals=4):
        widget = QDoubleSpinBox()
        widget.setDecimals(decimals)
        widget.setRange(low, high)
        widget.setValue(value)
        widget.setSuffix(suffix)
        widget.setKeyboardTracking(False)
        self._set_parameter_key(widget, f"newport.{key}")
        return widget

    def _line(self, key, text):
        widget = QLineEdit(text)
        self._set_parameter_key(widget, f"newport.{key}")
        return widget

    @staticmethod
    def _scroll(page):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(page)
        return scroll

    def _motion_page(self):
        page, layout = page_shell("SMC100CC / PR50CC 运动控制", "连接与参数恢复不会归零或移动。使用厂商正确配置的 PR50CC 角度单位。")
        self.connection_label = QLabel("未连接")
        self.position_label = QLabel("当前位置：未知")
        self.operation_label = QLabel("先连接并读取位置；未归零时可执行归零。")
        self.operation_label.setWordWrap(True)
        layout.addWidget(self.connection_label)
        layout.addWidget(self.position_label)
        layout.addWidget(self.operation_label)
        actions = QHBoxLayout()
        self.connect_button = QPushButton("连接")
        self.disconnect_button = QPushButton("断开")
        self.refresh_button = QPushButton("读取位置与状态")
        for button in (self.connect_button, self.disconnect_button, self.refresh_button):
            actions.addWidget(button)
        self.connect_button.clicked.connect(lambda: self._manual(lambda: self.connection_manager.connect(self.record)))
        self.disconnect_button.clicked.connect(lambda: self._disconnect())
        self.refresh_button.clicked.connect(lambda: self._manual(lambda: None))
        layout.addLayout(actions)
        form = QFormLayout()
        self.manual_angle = self._number("manual_angle", 0, -1e6, 1e6, " °")
        self.manual_step = self._number("manual_step", 0.2, 0.001, 360, " °")
        self.velocity = self._number("velocity", 10, 0.001, 20, " °/s")
        form.addRow("绝对目标", self.manual_angle)
        form.addRow("微动步长", self.manual_step)
        form.addRow("速度（点击应用才下发）", self.velocity)
        layout.addLayout(form)
        actions = QHBoxLayout()
        self.move_button = QPushButton("绝对定位")
        self.minus_button = QPushButton("− 微动")
        self.plus_button = QPushButton("＋ 微动")
        self.home_button = QPushButton("归零")
        self.velocity_button = QPushButton("应用速度")
        self.move_button.clicked.connect(lambda: self._move(self.manual_angle.value()))
        self.minus_button.clicked.connect(lambda: self._jog(-self.manual_step.value()))
        self.plus_button.clicked.connect(lambda: self._jog(self.manual_step.value()))
        self.home_button.clicked.connect(lambda: self._manual(self.device.home, wait_motion=True))
        self.velocity_button.clicked.connect(self._set_velocity)
        self.motion_buttons = [self.move_button, self.minus_button, self.plus_button, self.home_button, self.velocity_button]
        for button in self.motion_buttons:
            actions.addWidget(button)
        layout.addLayout(actions)
        self.manual_stop = QPushButton("停止运动 / 扫描")
        self.manual_stop.setObjectName("DangerButton")
        self.manual_stop.clicked.connect(self._stop)
        layout.addWidget(self.manual_stop)
        layout.addStretch()
        return self._scroll(page)

    def _scan_page(self):
        page, layout = page_shell("NI 循环旋转步进", "每个触发推进一个位置 · 到位后保持 · 末点下一触发回到 P0")
        layout.setContentsMargins(22, 18, 22, 12)
        layout.setSpacing(12)
        metrics = QHBoxLayout()
        metrics.setSpacing(10)
        for caption, key, accent, note in (
            ("当前位置 · 实际回读", "actual", "#55d9cc", "读取位置后显示"),
            ("扫描位置 · 当前目标", "target", "#9bb8ff", "启动保持 P0，首触发移动到 P1"),
            ("扫描点位", "point", "#e9bb71", "等待开始扫描"),
        ):
            card, value = metric_card(caption, "—", accent)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            hint = QLabel(note)
            hint.setObjectName("Muted")
            hint.setWordWrap(True)
            card.layout().addWidget(hint)
            card.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            setattr(self, f"scan_{key}_value", value)
            setattr(self, f"scan_{key}_hint", hint)
            metrics.addWidget(card, 1)
        layout.addLayout(metrics)

        origin_panel = QFrame()
        origin_panel.setObjectName("Panel")
        origin = QHBoxLayout(origin_panel)
        origin.setContentsMargins(12, 9, 12, 9)
        origin.setSpacing(8)
        origin.addWidget(QLabel("初始位置 P0"))
        self.initial_mode = QComboBox()
        self.initial_mode.addItem("启动时当前位置", "current")
        self.initial_mode.addItem("指定初始位置", "specified")
        self._set_parameter_key(self.initial_mode, "newport.initial_mode")
        self.initial_mode.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.initial_mode.setMinimumWidth(125)
        self.initial_angle = self._number("initial_angle", 0, -1e6, 1e6, " °", decimals=6)
        self.initial_angle.setMinimumWidth(100)
        self.initial_angle.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.initial_angle.setToolTip("指定扫描首点的绝对角度；修改数值只更新预览，点击“移动到 P0”才会运动。")
        self.use_current_button = QPushButton("使用当前位置")
        self.move_initial_button = QPushButton("移动到 P0")
        self.move_initial_button.setToolTip("移动到设定的初始位置，等待到位；此操作不启动扫描或触发计数。")
        self.use_current_button.setToolTip("重新读取设备位置，并填入指定初始位置；不移动设备。")
        self.use_current_button.clicked.connect(lambda: self._manual(lambda: None, capture_initial=True))
        self.move_initial_button.clicked.connect(self._move_to_initial)
        origin.addWidget(self.initial_mode, 1)
        origin.addWidget(self.initial_angle, 1)
        origin.addWidget(self.use_current_button)
        origin.addWidget(self.move_initial_button)
        layout.addWidget(origin_panel)

        actions = QHBoxLayout()
        self.start_button = QPushButton("开始循环扫描")
        self.start_button.setObjectName("PrimaryButton")
        self.stop_button = QPushButton("停止扫描")
        self.stop_button.setObjectName("DangerButton")
        self.scan_refresh_button = QPushButton("读取位置 / 更新预览")
        self.inject_button = QPushButton("虚拟触发 +1")
        self.start_button.clicked.connect(self._start_scan)
        self.stop_button.clicked.connect(self._stop)
        self.scan_refresh_button.clicked.connect(lambda: self._manual(lambda: None))
        self.inject_button.clicked.connect(self._inject)
        for button in (self.start_button, self.stop_button, self.scan_refresh_button):
            actions.addWidget(button)
        actions.addStretch()
        actions.addWidget(self.inject_button)
        layout.addLayout(actions)

        body = QHBoxLayout()
        body.setSpacing(12)
        setup, setup_layout = panel("扫描设置")
        setup.setFixedWidth(280)
        setup_layout.setSpacing(9)
        setup.setStyleSheet("QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { padding:4px 6px; }")
        settings_tabs = QTabWidget()
        forms = []
        for title in ("旋转参数", "NI 触发"):
            fields = QWidget()
            form = QFormLayout(fields)
            form.setContentsMargins(10, 8, 10, 8)
            form.setVerticalSpacing(6)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            forms.append(form)
            settings_tabs.addTab(fields, title)
        self.ni_combo = QComboBox()
        self.ni_combo.setProperty("skipDeviceParameterPersistence", True)
        for d in self.ni_devices:
            self.ni_combo.addItem(d.name, d.id)
        self.ni_combo.currentIndexChanged.connect(self._ni_selected)
        self.counter_name = self._line("counter", "ctr0")
        self.terminal = self._line("terminal", "PFI0")
        self.edge = QComboBox()
        self.edge.addItem("上升沿", True)
        self.edge.addItem("下降沿", False)
        self._set_parameter_key(self.edge, "newport.edge")
        self.step = self._number("step", 0.2, -0.9999, 0.9999, " °")
        self.count = QSpinBox()
        self.count.setRange(2, 10000)
        self.count.setValue(10)
        self._set_parameter_key(self.count, "newport.point_count")
        self.tolerance = self._number("tolerance", 0.01, 0.0001, 0.49, " °")
        self.settle = self._number("settle", 0.03, 0, 60, " s")
        self.timeout = self._number("timeout", 10, 0.1, 3600, " s")
        self.directory = self._line("directory", str(default_data_dir() / "rotation_runs"))
        for group, label, widget in ((1, "NI 设备", self.ni_combo), (1, "计数器", self.counter_name),
                              (1, "触发端口", self.terminal), (1, "边沿", self.edge),
                              (0, "角度步长", self.step), (0, "位置数", self.count),
                              (0, "到位容差", self.tolerance), (0, "稳定时间", self.settle),
                              (0, "运动超时", self.timeout)):
            widget.setMinimumWidth(0)
            widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            forms[group].addRow(label, widget)
        self.step.setToolTip("允许负步长；绝对值必须大于 0 且小于 1°")
        self.count.setToolTip("位置总数包含启动时的 P0；每轮需要相同数量的触发才能回到 P0")
        self.timeout.setToolTip("每次运动的超时，包括末点回跳和稳定等待")
        setup_layout.addWidget(settings_tabs)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setObjectName("Muted")
        setup_layout.addWidget(self.preview)
        setup_layout.addStretch()
        timing = QLabel("运动 / 稳定期间触发会停止。\n3 Hz 触发间隔约 333 ms。")
        timing.setObjectName("Muted")
        timing.setWordWrap(True)
        setup_layout.addWidget(timing)
        body.addWidget(setup)

        table_panel, table_layout = panel("扫描点表")
        table_layout.setSpacing(8)
        self.table_summary = QLabel("预览 · 尚未读取首点位置")
        self.table_summary.setObjectName("Muted")
        self.table_summary.setWordWrap(True)
        table_layout.addWidget(self.table_summary)
        self.scan_table = QTableView()
        self.scan_table.setObjectName("RotationTable")
        self.scan_table.setProperty("skipDeviceParameterPersistence", True)
        self.scan_table.setStyleSheet(
            "QTableView { background:#111823; alternate-background-color:#141e2c;"
            "border:1px solid #2a3549; border-radius:8px; gridline-color:#283347;"
            "selection-background-color:#245a5a; }"
        )
        self.scan_table_model = RotationPointTableModel(self)
        self.scan_table.setModel(self.scan_table_model)
        self.scan_table.setAlternatingRowColors(True)
        self.scan_table.setShowGrid(False)
        self.scan_table.verticalHeader().hide()
        self.scan_table.verticalHeader().setDefaultSectionSize(35)
        self.scan_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.scan_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.scan_table.setColumnWidth(0, 58)
        self.scan_table.horizontalHeader().setMinimumSectionSize(65)
        self.scan_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.scan_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.scan_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.scan_table.setMinimumWidth(280)
        self.scan_table.setMinimumHeight(200)
        table_layout.addWidget(self.scan_table, 1)
        self.scan_progress = QProgressBar()
        self.scan_progress.setTextVisible(False)
        self.scan_progress.setFixedHeight(6)
        self.scan_progress.setStyleSheet(
            "QProgressBar { border:0; border-radius:3px; background:#263044; }"
            "QProgressBar::chunk { border-radius:3px; background:#55d9cc; }"
        )
        table_layout.addWidget(self.scan_progress)
        self.scan_counts = QLabel("收到触发 0   ·   完成步进 0   ·   完成回跳 0")
        self.scan_counts.setObjectName("Muted")
        self.scan_counts.setWordWrap(True)
        table_layout.addWidget(self.scan_counts)
        self.scan_status = QLabel("未启动 · 读取位置以预览绝对角度")
        self.scan_status.setWordWrap(True)
        self.scan_status.setTextFormat(Qt.TextFormat.PlainText)
        self.scan_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        table_layout.addWidget(self.scan_status)
        body.addWidget(table_panel, 1)
        layout.addLayout(body, 1)
        save = QHBoxLayout()
        save.addWidget(QLabel("保存目录"))
        save.addWidget(self.directory, 1)
        self.browse_button = QPushButton("选择保存目录")
        self.browse_button.clicked.connect(self._browse)
        save.addWidget(self.browse_button)
        layout.addLayout(save)
        self.scan_controls = [self.initial_mode, self.initial_angle, self.ni_combo, self.counter_name, self.terminal, self.edge, self.step,
                              self.count, self.tolerance, self.settle, self.timeout, self.directory,
                              self.browse_button]
        self.initial_mode.currentIndexChanged.connect(self._reset_scan_preview)
        for widget in (self.initial_angle, self.step, self.count, self.tolerance, self.settle, self.timeout):
            widget.valueChanged.connect(self._reset_scan_preview)
        return self._scroll(page)

    def _analog_page(self):
        page, layout = page_shell("可选 NI 连续采集", "由同一旋转任务持有 NI 资源，采集不推进运动。保存完整模拟数据和独立的主机观测阶段日志；不宣称角度与样本硬件精确同步。")
        self.ai_enabled = QCheckBox("扫描期间同时连续采集并保存 CSV")
        self._set_parameter_key(self.ai_enabled, "newport.ai_enabled")
        layout.addWidget(self.ai_enabled)
        form = QFormLayout()
        self.ai_channels = self._line("ai_channels", "ai0")
        self.ai_rate = self._number("ai_rate", 10000, 1, 2e6, " Hz", 1)
        self.ai_block = QSpinBox()
        self.ai_block.setRange(1, 100000)
        self.ai_block.setValue(1000)
        self._set_parameter_key(self.ai_block, "newport.ai_block")
        self.ai_min = self._number("ai_min", -10, -10, 10, " V")
        self.ai_max = self._number("ai_max", 10, -10, 10, " V")
        self.ai_mode = QComboBox()
        self.ai_mode.addItems(TERMINAL_MODES)
        self._set_parameter_key(self.ai_mode, "newport.ai_mode")
        for label, widget in (("通道（逗号分隔，如 ai0,ai1）", self.ai_channels),
                              ("每通道采样率", self.ai_rate), ("每块样本数", self.ai_block),
                              ("量程下限", self.ai_min), ("量程上限", self.ai_max),
                              ("端接", self.ai_mode)):
            form.addRow(label, widget)
        layout.addLayout(form)
        self.ai_status = QLabel("未采集")
        layout.addWidget(self.ai_status)
        self.plot = WaveformPlot(maximum_points=10000)
        layout.addWidget(self.plot)
        self.ai_controls = [self.ai_enabled, self.ai_channels, self.ai_rate, self.ai_block,
                            self.ai_min, self.ai_max, self.ai_mode]
        return self._scroll(page)

    def _ni_selected(self):
        self._saved_ni_id = self.ni_combo.currentData()

    def _custom_device_parameters(self):
        return {"rotation_schema_version": 1, "ni_device_id": self.ni_combo.currentData()}

    def _restore_custom_device_parameters(self, state):
        self._saved_ni_id = state.get("ni_device_id")
        if self._saved_ni_id:
            index = self.ni_combo.findData(self._saved_ni_id)
            if index < 0:
                self.ni_combo.addItem("已删除的 NI 设备（请重新选择）", self._saved_ni_id)
                index = self.ni_combo.count() - 1
            self.ni_combo.setCurrentIndex(index)

    def update_ni_devices(self, records):
        if self._busy():
            return
        selected = self.ni_combo.currentData()
        self.ni_devices = [d for d in records if d.kind is DeviceKind.NI_DAQ]
        signature = tuple((d.id, d.name, self.connection_manager.is_connected(d)) for d in self.ni_devices)
        if signature == self._ni_signature:
            return
        self._ni_signature = signature
        self.ni_combo.blockSignals(True)
        self.ni_combo.clear()
        for d in self.ni_devices:
            online = self.connection_manager.is_connected(d)
            self.ni_combo.addItem(f"{d.name} · {'已连接' if online else '未连接'}", d.id)
        if selected:
            index = self.ni_combo.findData(selected)
            if index < 0:
                self.ni_combo.addItem("已删除的 NI 设备（请重新选择）", selected)
                index = self.ni_combo.count() - 1
            self.ni_combo.setCurrentIndex(index)
        self.ni_combo.blockSignals(False)

    def sync_connection_state(self):
        self._tick()

    def _busy(self):
        return self._manual_busy or self._scan_stop_pending or (self.controller is not None and self.controller.is_running)

    def _manual(self, operation, *, wait_motion=False, capture_initial=False):
        if self._busy():
            return
        self._manual_busy = True
        self._cancel_manual.clear()
        self._manual_done.clear()
        self.operation_label.setText("操作中…")
        self._tick()

        def work():
            leased = False
            try:
                self.connection_manager.acquire_devices(self._manual_owner, [self.record.id])
                leased = True
                if self._cancel_manual.is_set():
                    return None
                operation()
                if wait_motion:
                    deadline = time.monotonic() + 120
                    while not self._cancel_manual.is_set():
                        state = self.device.status()
                        if state.ready:
                            break
                        if not state.moving:
                            raise MotionError(f"控制器未就绪：0x{state.state:02X}")
                        if time.monotonic() >= deadline:
                            raise MotionError("手动运动/归零超时")
                        self._cancel_manual.wait(0.02)
                    if self._cancel_manual.is_set():
                        self._stop_manual_motion()
                if self.device.is_connected:
                    return self.device.position(), self.device.status(), self.device.velocity()
                return None
            except Exception:
                if wait_motion:
                    try:
                        self._stop_manual_motion()
                    except Exception as stop_error:
                        raise MotionError(f"运动失败且停止未确认：{stop_error}")
                raise
            finally:
                if leased:
                    self.connection_manager.release_devices(self._manual_owner)
                self._manual_done.set()

        self.submit(work, on_success=lambda result: self._manual_result(result, capture_initial=capture_initial),
                    on_error=self._manual_error)

    def _manual_result(self, result, *, capture_initial=False):
        self._manual_busy = False
        self._display_run = False
        self._scan_message = ""
        if result is not None:
            position, state, velocity = result
            self._last_position, self._last_velocity = position, velocity
            self.position_label.setText(f"当前位置：{position:.6f}° · 状态 0x{state.state:02X} · 速度 {velocity:g}°/s")
            if capture_initial and not self._cancel_manual.is_set():
                self.initial_angle.setValue(position)
                self.initial_mode.setCurrentIndex(self.initial_mode.findData("specified"))
        self.operation_label.setText("操作完成" if not self._cancel_manual.is_set() else "已请求停止")
        self._tick()

    def _manual_error(self, message):
        self._manual_busy = False
        self._last_position = self._last_velocity = None
        self._display_run = False
        self._scan_message = f"位置未确认：{message}"
        self.position_label.setText("当前位置：未知 · 读取失败")
        self.operation_label.setText(message)
        self._tick()

    def _disconnect(self):
        if self._busy():
            return
        # Disconnect checks leases itself; do not acquire our own lease around it.
        self._manual_busy = True
        self._manual_done.clear()
        def work():
            try:
                self.connection_manager.disconnect(self.record)
            finally:
                self._manual_done.set()
        self.submit(work, on_success=self._manual_result, on_error=self._manual_error)

    def _move(self, angle):
        self._manual(lambda: self.device.move_absolute(angle), wait_motion=True)

    def _stop_manual_motion(self):
        self.device.stop()
        deadline = time.monotonic() + 2.0
        while self.device.status().moving:
            if time.monotonic() >= deadline:
                raise MotionError("停止后仍在运动；停止未确认")
            time.sleep(0.01)

    def _jog(self, delta):
        self._manual(lambda: self.device.move_absolute(self.device.position() + delta), wait_motion=True)

    def _set_velocity(self):
        value = self.velocity.value()
        self._manual(lambda: self.device.set_velocity(value))

    def _specified_initial(self):
        return self.initial_angle.value() if self.initial_mode.currentData() == "specified" else None

    def _move_to_initial(self):
        angle = self._specified_initial()
        if angle is not None:
            self._move(angle)

    def _start_scan(self):
        if self._busy():
            return
        try:
            ni = next((d for d in self.ni_devices if d.id == self.ni_combo.currentData()), None)
            if ni is None:
                raise ValueError("请选择有效的 NI 设备；不会自动改绑已删除设备")
            if not self.connection_manager.is_connected(self.record) or not self.connection_manager.is_connected(ni):
                raise ValueError("请先在首页连接 Newport 与 NI")
            if not re.fullmatch(r"ctr[0-9]+", self.counter_name.text().strip()):
                raise ValueError("计数器格式为 ctr0")
            if not re.fullmatch(r"PFI[0-9]+", self.terminal.text().strip()):
                raise ValueError("触发端口格式为 PFI0")
            config = RotationScanConfig(self.step.value(), self.count.value(), self.tolerance.value(),
                                        self.settle.value(), self.timeout.value(),
                                        initial_position_deg=self._specified_initial())
            config.validate()
            if not self.directory.text().strip():
                raise ValueError("请指定保存目录")
            run = Path(self.directory.text().strip()) / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:8])
            self.analog = None
            metadata = {"newport": self.record.to_dict(), "ni": ni.to_dict(),
                        "counter": self.counter_name.text().strip(), "terminal": self.terminal.text().strip(),
                        "rising": self.edge.currentData()}
            if self.ai_enabled.isChecked():
                channels = tuple(s.strip() for s in self.ai_channels.text().split(","))
                if not channels or any(not re.fullmatch(r"ai[0-9]+", s) for s in channels):
                    raise ValueError("通道请输入 ai0,ai1 等名称")
                ai_config = AnalogInputConfig(tuple(f"{ni.resource}/{c}" for c in channels),
                                              self.ai_rate.value(), self.ai_block.value(), self.ai_min.value(),
                                              self.ai_max.value(), self.ai_mode.currentText())
                ai_config.validate()
                metadata["analog_config"] = asdict(ai_config)
                self.analog = AnalogAcquisitionController(
                    self.connection_manager.analog_input_for(ni, ai_config),
                    maximum_retained_samples=10000, stream_path=run / "analog.csv")
            counter = self.connection_manager.counter_for(ni, counter=metadata["counter"],
                                                          terminal=metadata["terminal"], rising=metadata["rising"])
            self.controller = RotationScanController(self.device, counter, config,
                manager=self.connection_manager, device_ids=[self.record.id, ni.id],
                analog=self.analog, run_directory=run, metadata=metadata)
            self._last_plot_end = 0
            self.plot.clear()
            self._display_run = True
            self._run_position_valid = True
            self._scan_message = ""
            self._follow_row = None
            self.controller.start()
            self.statusBar().showMessage(f"运行目录：{run}")
            self._tick()
        except Exception as exc:
            self._scan_message = f"启动失败：{exc}"
            self.scan_status.setText(self._scan_message)

    def _stop(self):
        if self.controller is not None and self.controller.is_running:
            if self._scan_stop_pending:
                return
            self.controller.request_stop()
            self._scan_stop_pending = True
            self._tick()

            def completed(_result=None):
                self._scan_stop_pending = False
                self._tick()

            def failed(message):
                self._scan_stop_pending = False
                self._scan_message = message
                self._tick()

            controller = self.controller

            def stop_or_recover():
                if controller.cleanup_pending and not controller.is_worker_running and not self.device.is_connected:
                    # This explicit retry owns the existing lease. Reconnect
                    # only the transport; never resume the scan or replay MOVE.
                    self.connection_manager.connect(self.record, owner=controller.owner)
                controller.stop()

            self.submit(stop_or_recover, on_success=completed, on_error=failed)
        elif self._manual_busy:
            self._cancel_manual.set()
        elif self.device.is_connected:
            self._manual(self._stop_manual_motion)

    def _inject(self):
        ni = next((d for d in self.ni_devices if d.id == self.ni_combo.currentData()), None)
        if ni is not None and is_virtual_resource(ni.resource):
            self.connection_manager.virtual_lab.emit_trigger(ni.resource)

    def _browse(self):
        directory = QFileDialog.getExistingDirectory(self, "旋转扫描保存目录", self.directory.text())
        if directory:
            self.directory.setText(directory)

    def _reset_scan_preview(self):
        if self._busy():
            return
        self._display_run = False
        self._scan_message = ""
        self._follow_row = None
        if hasattr(self, "timer"):
            self._tick()

    @staticmethod
    def _angle(value):
        return "—" if value is None else f"{value:.6f}°"

    def _render_scan(self, connected):
        # Display cached observations only. Serial I/O stays in workers/the scan controller.
        if not connected:
            self._last_position = self._last_velocity = None
            self._run_position_valid = False
            self.position_label.setText("当前位置：未知 · 未连接")
        run = self.controller if self._display_run else None
        s = run.snapshot() if run is not None else None
        if connected and self._run_position_valid and s is not None and s.actual_deg is not None:
            self._last_position = s.actual_deg
            self.position_label.setText(f"当前位置：{s.actual_deg:.6f}° · {PHASE_LABELS[s.phase]}")
        actual = self._last_position if connected else None
        if s is not None and s.phase == "preparing" and s.actual_deg is None:
            actual = None
        self.scan_actual_value.setText(self._angle(actual))
        if not connected:
            actual_hint = "未连接 · 位置未知"
        elif self._manual_busy:
            actual_hint = "正在操作 · 等待位置回读"
        elif s is not None and s.phase == "preparing" and self._run_position_valid:
            actual_hint = "准备中 · 正在核对初始位置"
        elif s is not None and run.is_running and self._run_position_valid:
            actual_hint = "扫描中 · 持续回读位置"
        elif actual is None:
            actual_hint = "点击“读取位置”获取角度"
        else:
            actual_hint = "最近一次回读 · 可点击更新"
        self.scan_actual_hint.setText(actual_hint)

        count = run.config.point_count if run is not None else self.count.value()
        step = run.config.step_deg if run is not None else self.step.value()
        specified = run.config.initial_position_deg if run is not None else self._specified_initial()
        preview_initial = specified if specified is not None else actual
        if run is not None:
            points = run.points
            signature = (id(run), points)
            mode = "本次运行" if run.is_running else "上次运行"
            if not points:
                mode += " · 起点预览"
        else:
            signature = ("preview", preview_initial, step, count)
            points = ()
            mode = "扫描预览"
        if signature != self._plan_signature:
            if not points:
                initial = preview_initial if run is None else specified
                points = tuple(None if initial is None else initial + i * step for i in range(count))
            self.scan_table_model.set_plan(points, step)
            self._plan_signature = signature
        self.scan_table_model.set_progress(s)
        active = self.scan_table_model.active_row
        if active is not None and active != self._follow_row:
            self.scan_table.scrollTo(self.scan_table_model.index(active, 0),
                                     QAbstractItemView.ScrollHint.EnsureVisible)
        self._follow_row = active

        initial = run.points[0] if run is not None and run.points else (preview_initial if run is None else specified)
        end = None if initial is None else initial + (count - 1) * step
        self.table_summary.setText(f"{mode} · {count} 个位置 · 步长 {step:+g}°\n"
                                   f"P0 {self._angle(initial)}  →  P{count - 1} {self._angle(end)}")
        target = s.target_deg if s is not None and s.target_deg is not None else initial
        self.scan_target_value.setText(self._angle(target))
        self.scan_target_hint.setText(
            (("本次目标 · " if s.target_deg is not None else "初始位置预览 · ") + PHASE_LABELS[s.phase]) if s is not None
            else "指定首点 P0 · 启动前须到位" if specified is not None
            else "预览首点 P0 · 启动时重新读取"
        )
        self.scan_point_value.setText(f"P{s.point_index}" if s is not None and s.target_deg is not None else "—")
        self.scan_point_hint.setText(
            f"第 {s.point_index + 1} / {count} 个位置 · 下一点 P{(s.point_index + 1) % count}"
            if s is not None and s.target_deg is not None else f"共 {count} 个位置 · 首触发前保持 P0"
        )
        received, completed = (s.received, s.completed) if s is not None else (0, 0)
        self.scan_counts.setText(f"收到触发 {received}   ·   完成步进 {completed}   ·   完成回跳 {completed // count}")
        self.scan_progress.setRange(0, count)
        self.scan_progress.setValue(completed % count)
        self.scan_progress.setToolTip(f"当前循环已完成 {completed % count} / {count} 步；回到 P0 后开始下一循环")
        if s is not None:
            status = f"{PHASE_LABELS[s.phase]} · 本步 {s.elapsed_ms:.1f} ms"
        elif specified is None:
            status = "未启动 · 以启动时当前位置为 P0；也可在上方指定初始位置"
        elif actual is None:
            status = "指定起点预览 · 连接后读取位置，或点击“移动到 P0”准备起点"
        elif abs(actual - specified) > self.tolerance.value():
            status = "指定起点预览 · 当前未到 P0，请先点击“移动到 P0”"
        else:
            status = "指定起点预览 · 最近回读已在 P0 容差内，启动时将复核稳定状态"
        error = self._scan_message or (s.error if s is not None else "")
        if not connected and run is not None and run.is_running and not error:
            error = "设备连接中断 · 正在结束扫描"
        self.scan_status.setText(f"{status}\n{error}" if error else status)
        self.scan_status.setStyleSheet("color:#ff9397;" if error else "color:#55d9cc;" if run is not None and run.is_running else "")
        span = abs(step) * (count - 1)
        estimate = (f"匀速时间下限 {span / self._last_velocity:.3f} s"
                    if self._last_velocity else "读取位置后估算回跳时间")
        self.preview.setText(f"回跳幅度 {span:g}°\n{estimate}")
        self.preview.setToolTip(f"每 {count} 次触发一轮，末点下一触发回跳 P0。"
                                "估算仅为匀速时间下限，不含加减速、稳定等待与通信。")

    def _tick(self):
        if not hasattr(self, "ai_controls"):
            return
        if self.device_provider is not None and not self._busy():
            self.update_ni_devices(self.device_provider())
        connected = self.connection_manager.is_connected(self.record)
        busy = self._busy()
        external_busy = self.connection_manager.lease_owner(self.record.id) not in {None, self._manual_owner,
                            self.controller.owner if self.controller is not None else None}
        self.connection_label.setText(self.device.identity if connected else "未连接")
        self.driver_status.setText("● 已连接" if connected else "● 未连接")
        self.driver_status.setStyleSheet("color:#55d9cc" if connected else "color:#f0b95d")
        self.connect_button.setEnabled(not connected and not busy and not external_busy)
        self.disconnect_button.setEnabled(connected and not busy and not external_busy)
        self.refresh_button.setEnabled(connected and not busy and not external_busy)
        self.scan_refresh_button.setEnabled(connected and not busy and not external_busy)
        self.use_current_button.setEnabled(connected and not busy and not external_busy)
        self.move_initial_button.setEnabled(connected and not busy and not external_busy
                                            and self._specified_initial() is not None)
        for button in self.motion_buttons:
            button.setEnabled(connected and not busy and not external_busy)
        for widget in [*self.scan_controls, *self.ai_controls]:
            widget.setEnabled(not busy)
        self.initial_angle.setEnabled(not busy and self._specified_initial() is not None)
        self.start_button.setEnabled(connected and not busy and not external_busy)
        self.stop_button.setEnabled(self.controller is not None and self.controller.is_running and not self._scan_stop_pending)
        self.stop_button.setText("停止中…" if self._scan_stop_pending else
                                 "重连并停止 / 清理" if self.controller is not None and self.controller.cleanup_pending
                                 and not self.controller.is_worker_running and not connected else
                                 "重试停止 / 清理" if self.controller is not None and self.controller.cleanup_pending
                                 and self.controller.snapshot().phase == "error" else "停止扫描")
        self.manual_stop.setEnabled(connected and not external_busy)
        self.sidebar_stop.setEnabled((connected or busy) and not external_busy and not self._scan_stop_pending)
        ni = next((d for d in self.ni_devices if d.id == self.ni_combo.currentData()), None)
        self.inject_button.setEnabled(ni is not None and is_virtual_resource(ni.resource)
                                      and self.controller is not None and self.controller.is_running)
        self._render_scan(connected)
        if self.analog is not None:
            s = self.analog.snapshot()
            self.ai_status.setText(f"{s.state.value} · 已保存 {s.saved_samples} 样本/通道 · {s.error or ''}")
            dataset = self.analog.dataset()
            end = dataset.first_sample_index + dataset.sample_count
            if end > self._last_plot_end:
                start = max(self._last_plot_end, dataset.first_sample_index)
                offset = start - dataset.first_sample_index
                self.plot.append_block(AnalogInputBlock(dataset.channel_names, dataset.sample_rate_hz,
                    start, tuple(values[offset:] for values in dataset.samples)))
                self._last_plot_end = end

    def closeEvent(self, event):
        self._cancel_manual.set()
        if self._busy() or self._call_tasks or not self._manual_done.is_set():
            self._stop()
            self.operation_label.setText("硬件操作或 NI 清理尚未结束，请确认停止后关闭；失败可重试停止。")
            event.ignore()
            return
        self.timer.stop()
        if self._owns_manager:
            self.connection_manager.close_all()
        super().closeEvent(event)
