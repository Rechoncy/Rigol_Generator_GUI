from __future__ import annotations

import re
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from uuid import uuid4

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
                            QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                            QPushButton, QSpinBox, QScrollArea)

from ..analog import AnalogAcquisitionController
from ..connections import DeviceConnectionManager
from ..drivers.newport_smc100 import MotionError
from ..drivers.ni_analog import AnalogInputBlock, AnalogInputConfig, TERMINAL_MODES
from ..models import DeviceKind
from ..repository import default_data_dir
from ..rotation_scan import RotationScanConfig, RotationScanController
from ..virtual_lab import is_virtual_resource
from .device_windows import BaseDeviceWindow, page_shell
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
        self._cancel_manual = Event()
        self._manual_done = Event()
        self._manual_done.set()
        self._manual_owner = f"newport-manual:{record.id}"
        self._ni_signature = None
        self._last_plot_end = 0
        self._last_position = None
        self._last_velocity = None
        self._saved_ni_id = None
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
        page, layout = page_shell("NI 触发循环旋转", "启动保持当前位置 P0；首触发 P1；末点的下一触发回跳 P0。运动/稳定期间触发会报错停止，不补跑。")
        form = QFormLayout()
        form.setVerticalSpacing(4)
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
        for label, widget in (("NI 设备", self.ni_combo), ("计数器", self.counter_name),
                              ("触发端口", self.terminal), ("边沿", self.edge),
                              ("角度步长（可负）", self.step), ("位置数（包含 P0）", self.count),
                              ("到位容差", self.tolerance), ("到位后稳定时间", self.settle),
                              ("单次运动超时（含回跳）", self.timeout), ("保存目录", self.directory)):
            form.addRow(label, widget)
        layout.addLayout(form)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        layout.addWidget(self.preview)
        self.scan_status = QLabel("未启动")
        self.scan_status.setWordWrap(True)
        self.scan_status.setTextInteractionFlags(self.position_label.textInteractionFlags())
        layout.addWidget(self.scan_status)
        actions = QHBoxLayout()
        self.start_button = QPushButton("开始循环扫描")
        self.start_button.setObjectName("PrimaryButton")
        self.stop_button = QPushButton("停止扫描")
        self.stop_button.setObjectName("DangerButton")
        self.inject_button = QPushButton("虚拟触发 +1")
        self.browse_button = QPushButton("选择保存目录")
        self.start_button.clicked.connect(self._start_scan)
        self.stop_button.clicked.connect(self._stop)
        self.inject_button.clicked.connect(self._inject)
        self.browse_button.clicked.connect(self._browse)
        for button in (self.start_button, self.stop_button, self.inject_button, self.browse_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.scan_controls = [self.ni_combo, self.counter_name, self.terminal, self.edge, self.step,
                              self.count, self.tolerance, self.settle, self.timeout, self.directory,
                              self.browse_button]
        layout.addStretch()
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
        return self._manual_busy or (self.controller is not None and self.controller.is_running)

    def _manual(self, operation, *, wait_motion=False):
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

        self.submit(work, on_success=self._manual_result, on_error=self._manual_error)

    def _manual_result(self, result):
        self._manual_busy = False
        if result is not None:
            position, state, velocity = result
            self._last_position, self._last_velocity = position, velocity
            self.position_label.setText(f"当前位置：{position:.6f}° · 状态 0x{state.state:02X} · 速度 {velocity:g}°/s")
        self.operation_label.setText("操作完成" if not self._cancel_manual.is_set() else "已请求停止")
        self._tick()

    def _manual_error(self, message):
        self._manual_busy = False
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
                                        self.settle.value(), self.timeout.value())
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
            self.controller.start()
            self.statusBar().showMessage(f"运行目录：{run}")
            self._tick()
        except Exception as exc:
            self.scan_status.setText(f"启动失败：{exc}")

    def _stop(self):
        if self.controller is not None and self.controller.is_running:
            self.controller.request_stop()
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
        for button in self.motion_buttons:
            button.setEnabled(connected and not busy and not external_busy)
        for widget in [*self.scan_controls, *self.ai_controls]:
            widget.setEnabled(not busy)
        self.start_button.setEnabled(connected and not busy and not external_busy)
        self.stop_button.setEnabled(self.controller is not None and self.controller.is_running)
        self.manual_stop.setEnabled(connected and not external_busy)
        self.sidebar_stop.setEnabled((connected or busy) and not external_busy)
        ni = next((d for d in self.ni_devices if d.id == self.ni_combo.currentData()), None)
        self.inject_button.setEnabled(ni is not None and is_virtual_resource(ni.resource)
                                      and self.controller is not None and self.controller.is_running)
        span = abs(self.step.value()) * (self.count.value() - 1)
        estimate = (f"回跳匀速时间下限 {span / self._last_velocity:.3f} s（不含加减速、稳定与通信）"
                    if self._last_velocity else "读取状态后显示回跳时间下限")
        self.preview.setText(f"P0 = 启动时当前位置；P末 = P0 + {(self.count.value()-1)*self.step.value():g}°；"
                             f"每 {self.count.value()} 次触发回到 P0。3 Hz 间隔约 0.333 s；{estimate}。")
        if self.controller is not None:
            s = self.controller.snapshot()
            target = "未知" if s.target_deg is None else f"{s.target_deg:.6f}"
            actual = "未知" if s.actual_deg is None else f"{s.actual_deg:.6f}"
            self.scan_status.setText(f"{PHASE_LABELS[s.phase]} · 收到 {s.received} / 完成 {s.completed} · "
                                     f"P{s.point_index} · 已回跳 {s.completed // self.controller.config.point_count} 次 · "
                                     f"目标 {target}° / 实际 {actual}° · 本步 {s.elapsed_ms:.1f} ms\n{s.error}")
            if self.controller.is_running and s.actual_deg is not None:
                self._last_position = s.actual_deg
                self.position_label.setText(f"当前位置：{s.actual_deg:.6f}° · {PHASE_LABELS[s.phase]}")
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
        try:
            if self.controller is not None:
                self.controller.stop()
            if not self._manual_done.wait(5):
                raise RuntimeError("硬件操作尚未结束，请等待停止完成后关闭")
        except Exception as exc:
            self.operation_label.setText(str(exc))
            event.ignore()
            return
        self.timer.stop()
        if self._owns_manager:
            self.connection_manager.close_all()
        super().closeEvent(event)
