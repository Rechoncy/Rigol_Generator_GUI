from __future__ import annotations

import json
import math
import sys
import traceback
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QVBoxLayout, QLabel,
                             QLineEdit, QPlainTextEdit, QPushButton, QScrollArea,
                             QSpinBox, QTableWidget, QTableWidgetItem, QWidget,
                             QHeaderView)

from ..connections import DeviceConnectionManager
from ..readback import ReadbackDeferred
from .. import __version__
from ..units import from_base, roundtrip_number, to_base, unit_specs
from ..drivers.scpi import ScpiError
from ..drivers.siglent_ssg import (SiglentSsgDriver, SsgModulation, SsgOutputReadback, SsgPoint, SsgSweep,
                                  SsgCommandError, VIRTUAL_SSG_RESOURCE, capabilities)
from ..drivers.dg4000 import parse_output_state
from .device_windows import BaseDeviceWindow, UnitValueWidget, page_shell, panel, value_spin
from .auto_readback import AutoReadback, window_readback_activity
from .widgets import metric_card


class RfPowerWidget(UnitValueWidget):
    """Keep the canonical dBm value exact when formatting a different unit."""

    def __init__(self, value, minimum, maximum):
        self._dbm = value
        self._updating_display = False
        super().__init__(value, minimum, maximum, "rf_power", "dBm")
        self.spin.valueChanged.connect(self._edited)

    def _edited(self, value):
        if not self._updating_display:
            self._dbm = to_base(value, "rf_power", self._active_unit)

    def value(self):
        return self._dbm

    def setValue(self, value):
        dbm = to_base(value, "rf_power", "dBm")
        self._dbm = min(max(dbm, self.minimum_base), self.maximum_base)
        self._updating_display = True
        try:
            super().setValue(self._dbm)
        finally:
            self._updating_display = False

    def _unit_changed(self, _index):
        self._updating_display = True
        try:
            self._active_unit = self.unit_combo.currentData()
            self._apply_display_range()
            self.spin.setValue(from_base(self._dbm, "rf_power", self._active_unit))
        finally:
            self._updating_display = False


class SiglentWindow(BaseDeviceWindow):
    def __init__(self, record, connection_manager=None, parameter_repository=None):
        self.connection_manager = connection_manager or DeviceConnectionManager()
        self.session = self.connection_manager.session_for(record)
        self.driver = SiglentSsgDriver(self.session)
        self.owner = f"siglent-native:{record.id}"
        self._busy = False
        self._pending_safety = []
        self._current_scan_preview = None
        self._output_readback_pending = False
        self._output_readback_token = 0
        self._output_readback_generation = None
        self.output_readback_values = {}
        self._selected_sweep_kind = "STEP"
        self._scan_layouts = {}
        self.controls = {}
        self.write_buttons = []
        super().__init__(record, [
            ("连接与射频", self._rf_page),
            ("触发步进扫描", lambda: self._sweep_page("STEP")),
            ("列表扫描", lambda: self._sweep_page("LIST")),
            ("模拟调制", self._mod_page), ("状态与诊断", self._diagnostics_page)], parameter_repository)
        self.auto_readback = AutoReadback(
            self, refresh=lambda: self._refresh_output_readback(automatic=True),
            invalidate=self._invalidate_output_readback,
            activity=lambda: window_readback_activity(self),
            connected=lambda: self.session.is_connected,
            generation=lambda: self.session.connection_generation,
            pending=lambda: self._output_readback_pending,
            ready=lambda: ((not self._busy or self._output_readback_pending)
                           and not self._pending_safety
                           and len(self._call_tasks) <= (1 if self._output_readback_pending else 0)),
        )
        self.auto_readback_checkbox.toggled.connect(self.auto_readback.set_enabled)
        self.pages.currentChanged.connect(self._scan_page_selected)
        self.log.appendPlainText(self.runtime_info.text())
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(1500)
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start()
        self.live_status = QLabel("扫描：尚未回读")
        self.statusBar().addPermanentWidget(self.live_status)
        for label, operation in (("停止扫描", self.driver.stop),
                                 ("RF 关闭", lambda: self.driver.set_output(False))):
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, op=operation: self._safety(op))
            self.statusBar().addPermanentWidget(button)
        for key in ("mod.pulse_source", "mod.pulse_trigger"):
            self.controls[key].currentIndexChanged.connect(self._update_pulse_controls)
        self.controls["mod.pulse"].toggled.connect(self._update_pulse_controls)
        self._update_buttons()
        if self.session.is_connected:
            QTimer.singleShot(0, self.connect_device)

    def _bind(self, key, control):
        self.controls[key] = control
        self._set_parameter_key(control, "ssg." + key)
        return control

    def _combo(self, key, items):
        box = self._bind(key, QComboBox())
        for text, value in items:
            box.addItem(text, value)
        return box

    def _number(self, key, value, low, high, unit, decimals=6):
        return self._bind(key, value_spin(value, low, high, unit, decimals))

    def _frequency(self, key, value):
        return self._bind(key, UnitValueWidget(value, 9e3, 40e9, "frequency", "GHz"))

    def _power(self, key, value):
        control = self._bind(key, RfPowerWidget(value, -140, 30))
        control.setToolTip("50 Ω 正弦载波：mV 为有效值，mVpp 为峰峰值；下发及功率限值校验使用等效 dBm。")
        return control

    def _check(self, key, text):
        return self._bind(key, QCheckBox(text))

    def _button(self, layout, text, callback, *, write=False):
        button = QPushButton(text)
        button.clicked.connect(lambda: callback())
        layout.addWidget(button)
        if write:
            self.write_buttons.append(button)
        return button

    @staticmethod
    def _scroll(page):
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(page)
        return area

    def _rf_page(self):
        page, layout = page_shell("鼎阳射频 / 微波信号发生器", "参数编辑和恢复仅保存在本机；连接、刷新不会打开 RF 或启动扫描。")
        row = QHBoxLayout()
        self._button(row, "连接 / 读取身份", self.connect_device)
        self._button(row, "刷新状态", self.refresh)
        self._button(row, "断开连接", self.disconnect_device)
        layout.addLayout(row)
        self.identity_label = QLabel("未连接")
        self.identity_label.setWordWrap(True)
        layout.addWidget(self.identity_label)
        self.capability_label = QLabel("已核对型号可写；未知型号仅查询。选件未自动识别。")
        self.capability_label.setWordWrap(True)
        layout.addWidget(self.capability_label)
        form = QFormLayout()
        self.frequency = self._frequency("rf.frequency", 1e9)
        self.power = self._power("rf.power", -20)
        form.addRow("载波频率", self.frequency)
        form.addRow("输出幅度", self.power)
        form.addRow(QLabel("电压按 50 Ω 正弦载波换算：mV 为有效值，mVpp 为峰峰值。"))
        layout.addLayout(form)
        row = QHBoxLayout()
        self._button(row, "应用频率", self.apply_frequency, write=True)
        self._button(row, "应用幅度", self.apply_power, write=True)
        self._button(row, "RF 开启", lambda: self._run(lambda: self.driver.set_output(True), after_write=True), write=True)
        self._button(row, "RF 关闭", lambda: self._safety(lambda: self.driver.set_output(False)))
        layout.addLayout(row)
        self.rf_status = QLabel("射频状态：尚未回读")
        self.rf_status.setWordWrap(True)
        layout.addWidget(self.rf_status)
        layout.addWidget(self._output_readback_panel())
        layout.addStretch()
        return self._scroll(page)

    def _output_readback_panel(self):
        monitor, monitor_layout = panel("仪器频率与幅度回读")
        monitor.setProperty("skipDeviceParameterPersistence", True)
        self.output_readback_widget = monitor
        row = QHBoxLayout()
        self.output_readback_button = QPushButton("回读频率 / 幅度")
        self.output_readback_button.clicked.connect(self._refresh_output_readback)
        self.output_readback_status = QLabel("未读取 · 请先连接设备")
        self.output_readback_status.setTextFormat(Qt.TextFormat.PlainText)
        self.output_readback_status.setWordWrap(True)
        row.addWidget(self.output_readback_button)
        row.addWidget(self.output_readback_status, 1)
        monitor_layout.addLayout(row)
        self.auto_readback_checkbox = QCheckBox("自动回读（下发后 / 触发空隙）")
        self.auto_readback_checkbox.setChecked(True)
        monitor_layout.addWidget(self.auto_readback_checkbox)
        cards = QHBoxLayout()
        for key, caption in (("frequency", "载波频率"), ("amplitude", "幅度"), ("output", "RF 输出")):
            card, value = metric_card(caption, "未知")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setMinimumHeight(value.fontMetrics().height() + 4)
            self.output_readback_values[key] = value
            cards.addWidget(card)
        monitor_layout.addLayout(cards)
        note = QLabel("按需查询仪器参数，不覆盖上方编辑值。幅度回读单位为 dBm；RF 关闭时数值仅为配置。"
                      "原生扫描和调制期间的瞬时输出仍需测量设备确认，触发接收和积压计数未知。")
        note.setWordWrap(True)
        note.setObjectName("Muted")
        monitor_layout.addWidget(note)
        return monitor

    def _invalidate_output_readback(self, message):
        self._output_readback_token += 1
        for value in self.output_readback_values.values():
            value.setText("未知")
            value.setToolTip("")
        self.output_readback_status.setText(message)

    def _sync_output_readback_connection(self):
        generation = self.session.connection_generation
        if not self.session.is_connected:
            self._invalidate_output_readback("未连接 · 回读值未知")
        elif self._output_readback_generation is not None and generation != self._output_readback_generation:
            self._invalidate_output_readback("连接已变化 · 请重新回读")
            self._output_readback_generation = None

    def _refresh_output_readback(self, *, automatic=False):
        if self._busy or self._output_readback_pending:
            return
        if not self.session.is_connected or not self.driver.caps.known:
            self._invalidate_output_readback("请先连接并识别支持的仪器 · 回读值未知")
            self._update_buttons()
            return
        self._invalidate_output_readback("正在回读仪器频率、幅度与 RF 输出…")
        token = self._output_readback_token
        ticket = self.auto_readback.capture(automatic)
        generation = self.session.connection_generation
        self._output_readback_generation = generation
        self._output_readback_pending = True
        self._busy = True
        self._update_buttons()

        def work():
            if automatic:
                try:
                    with self.session.try_transaction() as acquired:
                        if not acquired or not self.auto_readback.can_continue(ticket):
                            raise ReadbackDeferred()
                        reading = self.driver.read_output_parameters(
                            should_continue=lambda: self.auto_readback.can_continue(ticket))
                        return reading, datetime.now().astimezone()
                except ReadbackDeferred:
                    return None
            with self.session.transaction():
                if not self.session.is_connected or self.session.connection_generation != generation:
                    raise SsgCommandError("连接已变化，旧连接的回读已取消")
                return self.driver.read_output_parameters(), datetime.now().astimezone()

        def completed(result):
            try:
                if result is None:
                    self.auto_readback.finish(ticket, "deferred")
                    if token == self._output_readback_token:
                        self._invalidate_output_readback("自动回读等待通信 / 安全操作完成")
                    return
                if token != self._output_readback_token or not self.auto_readback.accepts(ticket):
                    self.auto_readback.finish(ticket, "stale")
                    if token == self._output_readback_token:
                        self._invalidate_output_readback("参数或连接已变化 · 等待最新回读")
                    return
                reading, read_at = result
                if (not self.session.is_connected or self.session.connection_generation != generation
                        or reading.connection_generation != generation):
                    self._invalidate_output_readback("连接已变化 · 回读值未知，请重新回读")
                    self.auto_readback.finish(ticket, "stale")
                    return
                self._display_output_readback(reading, read_at)
                self.auto_readback.finish(ticket, "success")
            except Exception as exc:
                self._invalidate_output_readback(f"回读显示失败 · 本次结果已丢弃：{exc}")
                self.auto_readback.finish(ticket, "failure")
                self._error(f"SSG 参数回读显示失败：{exc}")
            finally:
                finish()

        def failed(message):
            try:
                self.auto_readback.finish(ticket, "failure")
                if token == self._output_readback_token:
                    self._invalidate_output_readback(f"回读失败 · 自动回读已暂停，频率、幅度和 RF 状态未知：{message}")
                    self._error(f"SSG 参数回读失败：{message}")
            finally:
                finish()

        def finish():
            self._output_readback_pending = False
            # A read does not acquire/release the native scan's lease and never
            # changes scan_may_be_active, reconnects, or restarts the instrument.
            self._finished()

        self.submit(work, on_success=completed, on_error=failed)

    def _display_output_readback(self, reading, read_at):
        self.output_readback_values["frequency"].setText(f"{roundtrip_number(reading.frequency_hz)} Hz")
        self.output_readback_values["amplitude"].setText(f"{roundtrip_number(reading.power_dbm)} dBm")
        self.output_readback_values["output"].setText("开启" if reading.output_enabled else "关闭（未输出）")
        self.output_readback_status.setText(f"回读时间：{read_at:%Y-%m-%d %H:%M:%S}（本地） · 点击可刷新")

    def _snapshot_output_readback(self, snapshot, generation):
        """Reuse a complete RF snapshot already queried by a write/native poll."""
        try:
            frequency = float(snapshot[":FREQuency"])
            power = float(snapshot[":POWer"])
            output = parse_output_state(snapshot[":OUTPut"])
            if not math.isfinite(frequency) or frequency <= 0 or not math.isfinite(power):
                raise ValueError("频率或幅度不是有效有限数值")
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
            raise SsgCommandError(f"自动回读三项参数无效：{exc}") from exc
        if not self.session.is_connected or self.session.connection_generation != generation:
            raise SsgCommandError("连接已变化，自动回读结果已丢弃")
        return SsgOutputReadback(frequency, power, output, generation)

    def _sweep_page(self, kind):
        is_step = kind == "STEP"
        prefix = "sweep" if is_step else "list_sweep"
        title = "触发步进扫描" if is_step else "列表扫描"
        subtitle = "设置起止值、点数与驻留，由仪器按触发逐点推进。" if is_step else "逐行编辑频率、幅度和驻留，由仪器执行 LIST 序列。"
        page, layout = page_shell(title, subtitle)
        self._scan_layouts[kind] = layout
        config = QWidget()
        config_layout = QVBoxLayout(config)
        config_layout.setContentsMargins(0, 0, 8, 0)
        config_layout.setSpacing(10)
        form = QFormLayout()
        form.setVerticalSpacing(10)
        form.addRow("扫描量", self._combo(prefix + ".target", [("频率", "FREQUENCY"), ("功率", "LEVEL"), ("频率 + 功率", "LEV_FREQ")]))
        if is_step:
            form.addRow("起始 / 终止频率", self._pair(self._frequency("sweep.start_hz", 1e9), self._frequency("sweep.stop_hz", 1.1e9)))
            form.addRow("起始 / 终止幅度", self._pair(self._power("sweep.start_dbm", -20), self._power("sweep.stop_dbm", -20)))
            points = self._bind("sweep.points", QSpinBox())
            points.setRange(2, 65535)
            points.setValue(11)
            form.addRow("步进点数 / 驻留", self._pair(points, self._number("sweep.dwell_s", .03, .01, 100, "s", 4)))
            form.addRow("形状 / 间隔", self._pair(
                self._combo("sweep.shape", [("锯齿", "SAWTOOTH"), ("三角", "TRIANGLE")]),
                self._combo("sweep.spacing", [("线性", "LINEAR"), ("对数", "LOGARITHMIC")])))
        form.addRow("方向 / 模式", self._pair(
            self._combo(prefix + ".direction", [("正向", "FWD"), ("反向", "REV")]),
            self._combo(prefix + ".mode", [("连续", "CONTINUE"), ("单次", "SINGLE")])))
        triggers = [("自动", "AUTO"), ("仪器面板按键", "KEY"), ("总线 *TRG", "BUS"), ("外部输入", "EXT")]
        form.addRow("扫描触发 / 点触发", self._pair(self._combo(prefix + ".sweep_trigger", triggers), self._combo(prefix + ".point_trigger", triggers)))
        self.controls[prefix + ".point_trigger"].setCurrentIndex(3)
        form.addRow("扫描外部触发沿", self._combo(prefix + ".slope", [("上升沿", "POSITIVE"), ("下降沿", "NEGATIVE")]))
        config_layout.addLayout(form)
        if not is_step:
            self.list_power_unit = self._combo("list.power_unit", [(s.symbol, s.key) for s in unit_specs("rf_power")])
            self._list_power_unit_active = "dBm"
            list_actions = QHBoxLayout()
            list_actions.addWidget(QLabel("列表幅度单位"))
            list_actions.addWidget(self.list_power_unit)
            self._button(list_actions, "添加列表行", self.add_row)
            self._button(list_actions, "删除选中行", self.remove_rows)
            self._button(list_actions, "从仪器读取列表", self.read_list)
            config_layout.addLayout(list_actions)
            self.table = QTableWidget(0, 3)
            self.table.setHorizontalHeaderLabels(["频率 / Hz", "幅度 / dBm", "驻留 / s"])
            self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            self.table.setMinimumHeight(170)
            self.table.setAlternatingRowColors(True)
            config_layout.addWidget(self.table, 1)
            self.add_row(SsgPoint(1e9, -20))
            self.add_row(SsgPoint(1.1e9, -20))
            self.list_power_unit.currentIndexChanged.connect(self._change_list_power_unit)
        else:
            config_layout.addStretch()
        layout.addWidget(self._scroll(config), 1)
        row = QHBoxLayout()
        apply = self._button(row, "应用配置（保持停止）", lambda: self.apply_sweep(kind), write=True)
        apply.setObjectName("PrimaryButton")
        self._button(row, "启用 / 布防", lambda: self.start_sweep(kind), write=True)
        self._button(row, "执行单次", lambda: self.start_sweep(kind, single=True), write=True)
        self._button(row, "停止扫描", lambda: self._safety(self.driver.stop))
        self._button(row, "发送全局 *TRG", lambda: self._run(self.driver.bus_trigger, after_write=True), write=True)
        layout.addLayout(row)
        if self.record.resource == VIRTUAL_SSG_RESOURCE:
            row = QHBoxLayout()
            for label, value in (("模拟外部沿", "EXT"), ("模拟面板按键", "KEY")):
                self._button(row, label, lambda v=value: self._run(lambda: self.session.write(":SIM:TRIGger " + v), after_write=True), write=True)
            layout.addLayout(row)
        note = QLabel("外部触发线直接接仪器；*TRG 会同时触发其他 BUS 功能。停止扫描不关闭 RF。")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        if is_step:
            self.scan_monitor_widget = self._scan_monitor()
            layout.addWidget(self.scan_monitor_widget)
        return page

    def _scan_monitor(self):
        panel = QWidget()
        panel.setProperty("skipDeviceParameterPersistence", True)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.scan_metrics = {}
        for key, title, initial, color in (
                ("received", "已收到触发", "未知", "#8f7cff"),
                ("backlog", "待处理积压", "未知", "#f0b95d"),
                ("current", "当前扫描频率", "未读取", "#e7ebf4"),
                ("power", "当前扫描幅度", "未读取", "#55d9cc")):
            card, value = metric_card(title, initial, color)
            card.layout().setSpacing(6)
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.scan_metrics[key] = value
            row.addWidget(card, 1)
            if key in ("received", "backlog"):
                card.setToolTip("仪器手册未提供外部触发接收/积压计数查询；未知不表示零，不从扫描值推算。")
            if key == "power":
                self.scan_voltage = QLabel("50 Ω 正弦载波")
                self.scan_voltage.setObjectName("Muted")
                self.scan_voltage.setWordWrap(True)
                card.layout().addWidget(self.scan_voltage)
            card.layout().addStretch(1)
        layout.addLayout(row)
        self.sweep_status = QLabel("仪器扫描状态：尚未回读")
        self.sweep_status.setObjectName("Muted")
        self.sweep_status.setWordWrap(True)
        layout.addWidget(self.sweep_status)
        row = QHBoxLayout()
        self._button(row, "刷新当前值预览", self.refresh_scan_preview)
        self.scan_preview_auto = QCheckBox("自动刷新（约 1.5 秒）")
        self.scan_preview_auto.setProperty("skipDeviceParameterPersistence", True)
        row.addWidget(self.scan_preview_auto)
        row.addStretch()
        layout.addLayout(row)
        self.scan_preview_status = QLabel("仪器共用预览 · 当前值未读取；触发接收与积压不可查询，显示未知。")
        self.scan_preview_status.setObjectName("Muted")
        self.scan_preview_status.setWordWrap(True)
        layout.addWidget(self.scan_preview_status)
        return panel

    def _scan_page_selected(self, index):
        if index not in (1, 2):
            return
        kind = "STEP" if index == 1 else "LIST"
        self._selected_sweep_kind = kind
        # A single hardware monitor follows the selected editor. Page changes
        # neither issue commands nor duplicate timers or hardware state.
        for layout in self._scan_layouts.values():
            layout.removeWidget(self.scan_monitor_widget)
        self._scan_layouts[kind].addWidget(self.scan_monitor_widget)
        self.scan_monitor_widget.show()

    @staticmethod
    def _pair(first, second):
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(first)
        row.addWidget(second)
        return widget

    def _mod_page(self):
        page, layout = page_shell("模拟调制与脉冲", "扫描可以保留已有调制。FM/PM 互斥；AM 与脉冲或 IQ 并用有性能限制；IQ + 脉冲要求 RF Blanking 关闭。")
        layout.addWidget(self._check("mod.master", "调制总开关（与 RF 开关独立）"))
        form = QFormLayout()
        sources = [("内部", "INTERNAL"), ("外部", "EXTERNAL"), ("内部 + 外部", "INT,EXT")]
        for name, label, parameter, initial, low, high, unit in (
            ("am", "AM", "depth", 30, 0, 100, "%"),
            ("fm", "FM", "deviation", 1000, 1, 31250, "Hz"),
            ("pm", "PM", "deviation", .1, .01, .15, "rad")):
            form.addRow(self._check("mod." + name, "启用 " + label), self._combo("mod." + name + "_source", sources))
            form.addRow(label + " 波形 / 频率", self._pair(
                self._combo("mod." + name + "_wave", [("正弦", "SINE"), ("方波", "SQUARE")]),
                self._number("mod." + name + "_rate", 1000, .01, 100000, "Hz", 2)))
            form.addRow(label + (" 深度" if name == "am" else " 偏移"), self._number("mod." + name + "_" + parameter, initial, low, high, unit, 3))
        form.addRow(self._check("mod.pulse", "启用脉冲调制"), self._combo("mod.pulse_source", sources[:2]))
        form.addRow("脉冲周期 / 宽度", self._pair(self._number("mod.pulse_period", .001, 40e-9, 300, "s", 9), self._number("mod.pulse_width", .0005, 20e-9, 300, "s", 9)))
        form.addRow("脉冲触发", self._combo("mod.pulse_trigger", [("自动", "AUTO"), ("面板单次", "KEY"), ("外部触发", "EXT"), ("外部门控", "EGATE"), ("总线 BUS", "BUS")]))
        form.addRow("脉冲触发延时", self._number("mod.pulse_delay", .000001, 140e-9, 300, "s", 9))
        form.addRow("脉冲触发沿 / 门极性", self._pair(self._combo("mod.pulse_slope", [("上升沿", "POSITIVE"), ("下降沿", "NEGATIVE")]), self._combo("mod.gate_polarity", [("正逻辑", "NORMAL"), ("反逻辑", "INVERTED")])))
        layout.addLayout(form)
        self._button(layout, "应用调制配置", self.apply_modulation, write=True)
        self._button(layout, "读取调制状态", self.refresh_modulation)
        self._button(layout, "触发脉冲（全局 *TRG）", lambda: self._run(self.driver.pulse_bus_trigger, after_write=True), write=True)
        bus_note = QLabel("BUS：先应用配置，再按触发按钮；每次只发送一个 *TRG。该命令也可能触发设为 BUS 的扫描或低频功能，不自动打开 RF。")
        bus_note.setWordWrap(True)
        layout.addWidget(bus_note)
        note = QLabel("本版提供单脉冲和已核对的双调制组合，FM/PM 偏移采用保守范围。IQ/ARB 仅回读。选件是否可用以仪器回读为准；多路组合不自动推断兼容。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.mod_status = QLabel("调制状态：尚未读取；点击“读取调制状态”获取。")
        self.mod_status.setWordWrap(True)
        layout.addWidget(self.mod_status)
        return self._scroll(page)

    def _diagnostics_page(self):
        page, layout = page_shell("状态与诊断", "原始写命令可能改变输出；仅在扫描停止时允许。命令失败不会自动重放。选件查询未在共同手册中确认。")
        self.runtime_info = QLabel(
            f"软件版本：v{__version__}\n运行程序：{Path(sys.executable).resolve()}\n"
            f"程序模块：{Path(__file__).resolve()}")
        self.runtime_info.setWordWrap(True)
        self.runtime_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.runtime_info)
        self.readback = QPlainTextEdit()
        self.readback.setReadOnly(True)
        layout.addWidget(self.readback)
        self._button(layout, "读取扫描 / 调制详细参数", lambda: self._run(
            self.driver.configuration_snapshot,
            callback=lambda result: self.readback.setPlainText(json.dumps(result, ensure_ascii=False, indent=2)),
            read_modulation=True))
        self.command = self._bind("diagnostics.command", QLineEdit("*IDN?"))
        layout.addWidget(self.command)
        self._button(layout, "执行单条 SCPI", self.send_raw)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(300)
        layout.addWidget(self.log)
        self._button(layout, "保存诊断报告", self.save_diagnostics)
        return page

    def _values(self, prefix):
        values = {}
        for key, control in self.controls.items():
            if not key.startswith(prefix + "."):
                continue
            name = key[len(prefix) + 1:]
            if isinstance(control, QComboBox):
                value = control.currentData()
            elif isinstance(control, QCheckBox):
                value = control.isChecked()
            else:
                value = control.value()
            values[name] = value
        return values

    def add_row(self, point=None):
        if self.table.rowCount() >= 500:
            return
        point = point or SsgPoint(1e9, -20)
        row = self.table.rowCount()
        self.table.insertRow(row)
        power = from_base(point.power_dbm, "rf_power", self._list_power_unit_active)
        for column, value in enumerate((point.frequency_hz, power, point.dwell_s)):
            item = QTableWidgetItem(f"{value:.12g}")
            if column == 1:
                item.setData(Qt.ItemDataRole.UserRole, (item.text(), self._list_power_unit_active, point.power_dbm))
            self.table.setItem(row, column, item)

    def _list_power_dbm(self, item):
        cached = item.data(Qt.ItemDataRole.UserRole)
        # Formatting a new unit must not change a prepared scan's canonical dBm.
        if cached and tuple(cached[:2]) == (item.text(), self._list_power_unit_active):
            return cached[2]
        return to_base(float(item.text()), "rf_power", self._list_power_unit_active)

    def _change_list_power_unit(self, _index):
        unit = self.list_power_unit.currentData()
        old_unit = self._list_power_unit_active
        if unit == old_unit:
            return
        converted = []
        try:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 1)
                dbm = self._list_power_dbm(item)
                converted.append((item, f"{from_base(dbm, 'rf_power', unit):.12g}", dbm))
        except (ValueError, AttributeError) as exc:
            self.list_power_unit.blockSignals(True)
            self.list_power_unit.setCurrentIndex(self.list_power_unit.findData(old_unit))
            self.list_power_unit.blockSignals(False)
            self._error(f"列表第 {row + 1} 行幅度无效，单位未切换：{exc}")
            return
        blocked = self.table.blockSignals(True)
        for item, value, dbm in converted:
            item.setText(value)
            item.setData(Qt.ItemDataRole.UserRole, (value, unit, dbm))
        self.table.blockSignals(blocked)
        self._list_power_unit_active = unit
        self.table.setHorizontalHeaderItem(1, QTableWidgetItem(f"幅度 / {unit}"))

    def remove_rows(self):
        for row in sorted({item.row() for item in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(row)

    def _table_points(self):
        return tuple(SsgPoint(float(self.table.item(row, 0).text()),
                              self._list_power_dbm(self.table.item(row, 1)),
                              float(self.table.item(row, 2).text())) for row in range(self.table.rowCount()))

    def apply_sweep(self, kind=None):
        try:
            config = self._sweep_config(kind)
        except (ValueError, AttributeError) as exc:
            self._error(f"列表存在无效数字：{exc}")
            return
        self._run(lambda: self.driver.prepare_sweep(config), after_write=True)

    def _sweep_config(self, kind=None):
        kind = kind or self._selected_sweep_kind
        values = self._values("sweep" if kind == "STEP" else "list_sweep")
        return SsgSweep(kind=kind, **values, table=self._table_points() if kind == "LIST" else ())

    def start_sweep(self, kind=None, *, single=False):
        try:
            if self._sweep_config(kind) != self.driver.prepared:
                raise ValueError("扫描参数尚未应用或已有编辑，请先应用配置")
        except (ValueError, AttributeError) as exc:
            self._error(str(exc))
            return
        self._clear_scan_preview("重新布防，等待读取当前扫描值。")
        self._run(self.driver.execute_single if single else self.driver.start, after_write=True)

    def apply_frequency(self):
        value = self.frequency.value()
        self._run(lambda: self.driver.set_frequency(value), after_write=True)

    def apply_power(self):
        value = self.power.value()
        self._run(lambda: self.driver.set_power(value), after_write=True)

    def apply_modulation(self):
        config = SsgModulation(**self._values("mod"))
        self._run(lambda: self.driver.apply_modulation(config), read_modulation=True, after_write=True)

    def refresh_modulation(self):
        self._run(lambda: None, read_modulation=True)

    def read_list(self):
        def success(points):
            self.table.setRowCount(0)
            for point in points:
                self.add_row(point)
        self._run(self.driver.read_list, callback=success)

    def send_raw(self):
        command = self.command.text()
        self._run(lambda: self.driver.raw(command), callback=lambda result: self.log.appendPlainText(f"> {command}\n{result}"),
                  after_write=not command.strip().endswith("?"))

    def connect_device(self):
        self._invalidate_output_readback("正在连接 · 请连接完成后重新回读")
        self.scan_preview_auto.setChecked(False)
        self._clear_scan_preview("重新连接后需重新读取当前扫描值。")
        def operation():
            if not self.session.is_connected:
                self.connection_manager.info(self.record)
                self.connection_manager.connect(self.record, owner=self.owner)
            self.driver.identify()
        self._run(operation)

    def disconnect_device(self):
        if self._busy or self.driver.state == "enabled" or self.connection_manager.lease_owner(self.record.id):
            self._error("请先停止扫描并确认状态，再断开连接")
            return
        self.connection_manager.disconnect(self.record)
        self._invalidate_output_readback("已断开 · 回读值未知")
        self.scan_preview_auto.setChecked(False)
        self._clear_scan_preview("已断开，当前扫描值未知。")
        self.driver.prepared = None
        self.driver.state = "unknown"
        self.driver_status.setText("● 已断开；硬件输出状态未确认")
        self._update_buttons()

    def refresh(self):
        self._run(lambda: None)

    def _poll(self):
        self._sync_output_readback_connection()
        if self.session.is_connected and not self._busy and self.driver.state == "enabled":
            if ((self.auto_readback.paused or not self.auto_readback_checkbox.isChecked())
                    and not self.scan_preview_auto.isChecked()):
                # Keep essential scan/lease confirmation alive after a basic
                # readback failure, without repeating the failed RF queries.
                def state_received(state):
                    active = self.driver.state == "enabled"
                    self.live_status.setText("扫描：" + ("已启用 / 等待触发或运行" if active else "已关闭"))
                    self.sweep_status.setText(f"扫描状态：{state}；频率/幅度自动回读已暂停或关闭。")
                    if not active:
                        self._clear_scan_preview("扫描未启用，当前扫描值不可用。")
                self._run(self.driver.read_sweep_state, callback=state_received, read_snapshot=False)
            else:
                self._run(lambda: None, read_current_point=self.scan_preview_auto.isChecked())

    def refresh_scan_preview(self):
        if not self._busy:
            self.scan_preview_status.setText("正在读取当前扫描频率和幅度…")
            self._run(lambda: None, read_current_point=True)

    def _clear_scan_preview(self, message):
        self._current_scan_preview = None
        self.scan_metrics["current"].setText("未知")
        self.scan_metrics["current"].setToolTip("")
        self.scan_metrics["power"].setText("未知")
        self.scan_voltage.setText("50 Ω 正弦载波")
        self.scan_preview_status.setText(message)

    def _safety(self, operation):
        self.scan_preview_auto.setChecked(False)
        self._clear_scan_preview("正在处理停止 / RF 关闭，之前的预览已失效。")
        is_stop = operation == self.driver.stop
        def work():
            if not self.session.is_connected:
                previous = self.driver.caps
                self.connection_manager.info(self.record)  # Drop stale manager connection flags.
                info = self.connection_manager.connect(self.record, owner=self.owner)
                current = capabilities(info.identity)
                if (not current.known or
                        (previous.model and previous.model != current.model) or
                        (previous.serial and previous.serial != current.serial)):
                    self.session.disconnect()
                    raise ValueError("重连身份不一致或型号未知，未发送停止 / RF 关闭命令；请核对仪器。")
                self.driver.caps = current
                self.driver.prepared = None
            operation()  # One write attempt; never replay a trigger, arm, or configuration.
        def finished(_result):
            message = "扫描已停止（已回读 OFF）；RF 未改变。" if is_stop else "RF 已关闭（已回读 OFF）；扫描状态需单独确认。"
            self.readback.setPlainText(json.dumps(
                {":SWEep:STATe" if is_stop else ":OUTPut": "OFF"}, ensure_ascii=False, indent=2))
            if is_stop:
                self._clear_scan_preview("扫描已停止；重新布防后可读取当前值。")
                self.sweep_status.setText(message)
                self.live_status.setText("扫描：已关闭")
                self.driver_status.setText("● 已连接 · 扫描已关闭")
                self.rf_status.setText("扫描已停止；频率、幅度及 RF 状态请手动刷新回读。")
            else:
                self.rf_status.setText(message)
                self.driver_status.setText("● 已连接 · RF 已关闭")
            self.log.appendPlainText(message)
            self.statusBar().showMessage(message, 20000)
        def dispatch():
            self._run(work, callback=finished, read_snapshot=False, after_write=True)
        if self._busy:
            self._pending_safety.append(dispatch)
            self.statusBar().showMessage("停止 / RF 关闭已排队，当前通信结束后优先执行")
        else:
            dispatch()

    def _run(self, operation, callback=None, *, read_modulation=False, read_current_point=False, read_snapshot=True,
             after_write=False):
        if self._busy:
            return
        try:
            self.connection_manager.acquire_devices(self.owner, [self.record.id])
        except Exception as exc:
            self._error(str(exc))
            return
        self._busy = True
        self._update_buttons()
        generation = self.session.connection_generation
        def work():
            phase = "操作"
            try:
                if after_write:
                    with self.session.transaction():
                        try:
                            result = operation()
                        finally:
                            # Unknown acceptance must invalidate older reads,
                            # while the failed write is never replayed.
                            self.connection_manager.note_output_change(self.record)
                else:
                    result = operation()
                phase = "状态读取"
                snapshot = self.driver.snapshot(include_modulation=read_modulation,
                                                include_current_point=read_current_point) if read_snapshot else None
                return result, snapshot, None
            except (ValueError, SsgCommandError) as exc:
                # A valid instrument response does not require disconnecting USB.
                return None, None, f"{phase}未完成：{exc}"
            except Exception as exc:
                self.driver.state = "unknown"
                # A timed-out response must not contaminate the next query.
                try:
                    self.session.disconnect()
                except Exception:
                    pass
                raise ScpiError(f"{phase}失败：{exc}") from exc
        def finish(payload):
            try:
                result, snapshot, validation_error = payload
                self._show_identity()
                if snapshot is not None:
                    self._show_snapshot(snapshot)
                    if (self.auto_readback_checkbox.isChecked() and not self.auto_readback.paused
                            and not self._pending_safety
                            and (after_write or self.driver.state == "enabled")):
                        # These three basic replies are already in the native
                        # snapshot. Reuse them rather than appending queries.
                        reading = self._snapshot_output_readback(snapshot, generation)
                        ticket = self.auto_readback.capture()
                        self._display_output_readback(reading, datetime.now().astimezone())
                        self.auto_readback.finish(ticket, "success")
                elif after_write and not validation_error:
                    self.auto_readback.request()
                if validation_error:
                    if after_write or self.driver.state == "enabled":
                        self._invalidate_output_readback("本次状态回读未完成 · 参数未知，自动回读已暂停")
                        self.auto_readback.finish(self.auto_readback.capture(), "failure")
                    if read_current_point:
                        self.scan_preview_auto.setChecked(False)
                        self._clear_scan_preview("当前值读取未完成，自动刷新已停止；详见状态与诊断。")
                    self._error(validation_error)
                elif callback:
                    callback(result)
                elif result:
                    message = "；".join(result) if isinstance(result, tuple) else str(result)
                    self.log.appendPlainText(message)
                    self.statusBar().showMessage(message, 20000)
            except Exception as exc:
                # Qt callback exceptions escape the worker's try/except and the
                # frozen entry point exits on them. Contain this operation here.
                self.scan_preview_auto.setChecked(False)
                self._clear_scan_preview("结果处理失败，当前值未知；自动刷新已停止。")
                self._invalidate_output_readback("本次回读显示失败 · 参数未知，自动回读已暂停")
                self.auto_readback.finish(self.auto_readback.capture(), "failure")
                self.driver_status.setText("● 已连接 · 本次结果显示失败" if self.session.is_connected else "● 未连接 · 本次结果显示失败")
                self.rf_status.setText("本次结果未能完整显示，RF / 幅度以仪器回读或面板为准。")
                self._error(f"结果处理失败：{exc}\n{traceback.format_exc()}")
            finally:
                if not self.driver.scan_may_be_active:
                    self.connection_manager.release_devices(self.owner)
                self._finished()
        def failure(message):
            self._invalidate_output_readback("通信失败 · 回读值未知")
            self.auto_readback.finish(self.auto_readback.capture(), "failure")
            self.scan_preview_auto.setChecked(False)
            self._clear_scan_preview("通信失败，当前值未知；自动刷新已停止。")
            self._show_identity()
            self.rf_status.setText("通信失败：RF / 扫描状态未知，请重连回读，必要时在仪器面板停止输出。")
            self.sweep_status.setText("扫描状态未知；没有自动重试触发或启动命令。" +
                                      ("曾启用或尝试启用扫描，点击停止扫描会尝试重连并确认停止。" if self.driver.scan_may_be_active else
                                       "本窗口没有扫描运行证据，可以关闭窗口或断开连接。"))
            self.live_status.setText("扫描：状态未知")
            self._error(message)
            if not self.driver.scan_may_be_active:
                self.connection_manager.release_devices(self.owner)
            self._finished()
        self.submit(work, on_success=finish, on_error=failure)

    def _finished(self):
        self._busy = False
        self._update_buttons()
        if self._pending_safety:
            QTimer.singleShot(0, self._run_pending_safety)

    def _run_pending_safety(self):
        if not self._busy and self._pending_safety:
            # Keep the queue visible to closeEvent until this callback starts.
            self._pending_safety.pop(0)()

    def _error(self, message):
        self.statusBar().showMessage(message, 20000)
        self.log.appendPlainText("错误：" + message)
        if self.parameter_repository is not None:
            try:
                self._write_diagnostics()
            except OSError as exc:
                self.log.appendPlainText(f"诊断报告保存失败：{exc}")

    def _write_diagnostics(self) -> Path:
        from ..repository import default_data_dir
        import hashlib

        data_dir = self.parameter_repository.data_dir if self.parameter_repository is not None else default_data_dir()
        folder = Path(data_dir) / "diagnostics"
        folder.mkdir(parents=True, exist_ok=True)
        device_key = hashlib.sha256(self.record.id.encode("utf-8")).hexdigest()[:16]
        path = folder / f"siglent-{device_key}.json"
        c = self.driver.caps
        report = {"timestamp": datetime.now().astimezone().isoformat(), "software_version": __version__,
                  "executable": str(Path(sys.executable).resolve()),
                  "module_path": str(Path(__file__).resolve()),
                  "frozen": bool(getattr(sys, "frozen", False)),
                  "device_id": self.record.id, "resource": self.record.resource,
                  "model": c.model, "serial": c.serial, "firmware": c.firmware,
                  "connected": self.session.is_connected, "scan_state": self.driver.state,
                  "scan_may_be_active": self.driver.scan_may_be_active,
                  "modulation_command": self.driver.modulation_header,
                  "scan_monitor": {"trigger_received": None, "trigger_backlog": None,
                                   "current_values": self._current_scan_preview,
                                   "auto_refresh": self.scan_preview_auto.isChecked(),
                                   "status": self.scan_preview_status.text()},
                  "readback": self.readback.toPlainText(), "log": self.log.toPlainText()}
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def save_diagnostics(self):
        try:
            path = self._write_diagnostics()
        except OSError as exc:
            self.log.appendPlainText(f"诊断报告保存失败：{exc}")
            self.statusBar().showMessage(f"诊断报告保存失败：{exc}", 20000)
            return
        self.statusBar().showMessage(f"诊断报告已保存：{path}", 20000)

    def _show_identity(self):
        c = self.driver.caps
        if not c.model:
            return
        suffix = "（当前未连接，保留最近身份）" if not self.session.is_connected else ""
        self.identity_label.setText(f"{c.model}   序列号：{c.serial}   固件：{c.firmware}{suffix}")
        self.capability_label.setText(
            f"频率范围 {c.minimum_hz:g}–{c.maximum_hz:g} Hz；FM/PM：{'已确认硬件支持' if c.fm_pm else '未确认，禁用'}；选件：未查询。功率范围随频率校验。"
            if c.known else "未知型号：仅允许查询")

    def _show_snapshot(self, snapshot):
        # Preserve raw responses before parsing, including values that cannot be
        # rendered; the error report must contain the reply that caused failure.
        self.readback.setPlainText(json.dumps(snapshot, ensure_ascii=False, indent=2))
        self._show_identity()
        c = self.driver.caps
        if not c.known:
            return
        self.driver_status.setText("● 已连接 · " + ("扫描已启用" if self.driver.state == "enabled" else "扫描已关闭"))
        self.live_status.setText("扫描：" + ("已启用 / 等待触发或运行" if self.driver.state == "enabled" else "已关闭"))
        try:
            dbm = float(snapshot[':POWer'])
            millivolts = from_base(dbm, 'rf_power', 'mV')
            millivolts_pp = from_base(dbm, 'rf_power', 'mVpp')
        except (ValueError, OverflowError) as exc:
            raise SsgCommandError(f"幅度回读无法显示 [:POWer?]：{snapshot[':POWer']!r}；{exc}") from exc
        self.rf_status.setText(f"实际频率：{snapshot[':FREQuency']} Hz　幅度：{snapshot[':POWer']} dBm / "
                               f"{millivolts:.6g} mV / {millivolts_pp:.6g} mVpp　RF：{snapshot[':OUTPut']}")
        self.sweep_status.setText(
            f"仪器共用状态：{snapshot[':SWEep:STATe']} · {snapshot[':SWEep:MODE']}　"
            f"扫描触发：{snapshot[':SWEep:SWEep:TRIGger:TYPE']}　点触发：{snapshot[':SWEep:POINt:TRIGger:TYPE']}")
        if self.driver.state != "enabled":
            self._clear_scan_preview("扫描未启用，当前扫描值不可用。")
        elif ":SWEep:CURRent:FREQuency" in snapshot and ":SWEep:CURRent:LEVel" in snapshot:
            frequency = float(snapshot[":SWEep:CURRent:FREQuency"])
            power = float(snapshot[":SWEep:CURRent:LEVel"])
            mv, mvpp = from_base(power, "rf_power", "mV"), from_base(power, "rf_power", "mVpp")
            timestamp = datetime.now().astimezone()
            scale, unit = (1e9, "GHz") if frequency >= 1e9 else (1e6, "MHz") if frequency >= 1e6 else (1e3, "kHz")
            self.scan_metrics["current"].setText(f"{frequency / scale:.9g} {unit}")
            self.scan_metrics["current"].setToolTip(f"{frequency:.12g} Hz")
            self.scan_metrics["power"].setText(f"{power:.6g} dBm")
            self.scan_voltage.setText(f"{mv:.6g} mV / {mvpp:.6g} mVpp")
            self.scan_preview_status.setText(f"最近回读 {timestamp:%H:%M:%S}（本机时间）· 频率/幅度依次读取，可能跨点；触发计数未知。")
            self._current_scan_preview = {"frequency_hz": frequency, "power_dbm": power,
                                          "read_at": timestamp.isoformat()}
        if ":OUTPut:MODulation" in snapshot:
            stamp = datetime.now().strftime("%H:%M:%S")
            self.mod_status.setText(f"最近调制回读（{stamp}，需手动刷新）：" + "  ".join(f"{name}={snapshot.get(':'+name+':STATe', '未确认')}" for name in ("AM", "FM", "PM", "PULM")) +
                                   f"\n脉冲源={snapshot[':PULM:SOURce']}；脉冲触发={snapshot[':PULM:TRIGger:MODE']}" +
                                   f"\n调制总开关={snapshot[':OUTPut:MODulation']}；IQ={snapshot.get(':FUNCtion:DM:STATe', '不适用')}；RF Blanking={snapshot.get(':RADio:ARB:MDEStination:PULSe', '不适用')}")

    def _update_buttons(self):
        enabled = self.session.is_connected and self.driver.caps.known and not self._busy
        for button in self.write_buttons:
            button.setEnabled(enabled)
        self.output_readback_button.setEnabled(enabled and not self._output_readback_pending)
        self.output_readback_button.setText("回读中…" if self._output_readback_pending else "回读频率 / 幅度")
        for name in ("fm", "pm"):
            self.controls["mod." + name].setEnabled(self.driver.caps.fm_pm)
        self._update_pulse_controls()

    def _update_pulse_controls(self):
        internal = self.controls["mod.pulse"].isChecked() and self.controls["mod.pulse_source"].currentData() == "INTERNAL"
        trigger = self.controls["mod.pulse_trigger"]
        bus = trigger.model().item(trigger.findData("BUS"))
        bus.setEnabled(self.driver.caps.pulse_bus)
        bus.setToolTip("应用时核对仪器 BUS 回读，触发按钮发送全局 *TRG" if self.driver.caps.pulse_bus else "当前型号尚未开放脉冲 BUS；扫描/点触发的 BUS 不受此限制")
        for key in ("pulse_period", "pulse_width", "pulse_trigger"):
            self.controls["mod." + key].setEnabled(internal)
        for key in ("pulse_delay", "pulse_slope"):
            self.controls["mod." + key].setEnabled(internal and trigger.currentData() == "EXT")
        self.controls["mod.gate_polarity"].setEnabled(internal and trigger.currentData() == "EGATE")

    def sync_connection_state(self):
        self._sync_output_readback_connection()
        if not self.session.is_connected:
            self.driver_status.setText("● 未连接 · 硬件状态未确认")
            self.live_status.setText("扫描：未连接 / 状态未知")
        self._update_buttons()

    def _custom_device_parameters(self):
        powers = []
        for row in range(self.table.rowCount()):
            try:
                powers.append(self._list_power_dbm(self.table.item(row, 1)))
            except (ValueError, AttributeError):
                powers.append(None)  # Preserve an invalid draft without inventing a value.
        return {"list": [[self.table.item(row, col).text() if self.table.item(row, col) else "" for col in range(3)] for row in range(self.table.rowCount())],
                "list_power_unit": self._list_power_unit_active, "list_power_dbm": powers}

    def _restore_device_parameters(self):
        super()._restore_device_parameters()
        if self.parameter_repository is None:
            return
        try:
            saved = self.parameter_repository.load(self.record.id, kind=self.record.kind.value)
        except Exception:
            return  # Base restoration already reports malformed configuration.
        controls = saved.get("controls", {})
        if not isinstance(controls, dict) or any(k.startswith("named/ssg.list_sweep.") for k in controls):
            return
        # <= 0.7.8 used common trigger controls for STEP and LIST. Copy those
        # saved values into both editors once; future edits stay independent.
        for key, control in self.controls.items():
            if key.startswith("list_sweep."):
                state = controls.get("named/ssg.sweep." + key.split(".", 1)[1])
                if isinstance(state, dict):
                    self._restore_parameter_control(control, state)

    def _restore_parameter_control(self, control, state):
        # Versions <=0.7.1 stored RF/STEP dBm editors as plain double controls.
        if isinstance(control, UnitValueWidget) and control.family == "rf_power" and state.get("type") == "double":
            control.set_unit("dBm")
            control.setValue(float(state.get("value", control.value())))
            return
        super()._restore_parameter_control(control, state)

    def _restore_custom_device_parameters(self, state):
        rows = state.get("list")
        if not isinstance(rows, list) or len(rows) > 500 or any(not isinstance(row, list) or len(row) != 3 for row in rows):
            return
        unit = state.get("list_power_unit", "dBm")
        if unit not in ("dBm", "mV", "mVpp"):
            return
        self.list_power_unit.setCurrentIndex(self.list_power_unit.findData(unit))
        self.table.setRowCount(len(rows))
        powers = state.get("list_power_dbm", [])
        for row, values in enumerate(rows):
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))
            if isinstance(powers, list) and row < len(powers) and powers[row] is not None:
                try:
                    dbm = to_base(float(powers[row]), "rf_power", "dBm")
                    actual = to_base(float(values[1]), "rf_power", unit)
                    if math.isclose(dbm, actual, rel_tol=1e-10, abs_tol=1e-9):
                        item = self.table.item(row, 1)
                        item.setData(Qt.ItemDataRole.UserRole, (item.text(), unit, dbm))
                except (ValueError, TypeError):
                    pass  # A changed or invalid cell must be interpreted from its visible text.

    def closeEvent(self, event):
        self.auto_readback_checkbox.setChecked(False)
        self.auto_readback.stop()
        if self._busy or self._call_tasks or self._pending_safety or self.connection_manager.lease_owner(self.record.id) == self.owner:
            self._error("扫描启用或状态未确认：请停止并成功回读后关闭窗口")
            event.ignore()
            return
        self.poll_timer.stop()
        super().closeEvent(event)
