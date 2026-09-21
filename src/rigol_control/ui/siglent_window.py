from __future__ import annotations

import json

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel,
                             QLineEdit, QPlainTextEdit, QPushButton, QScrollArea,
                             QSpinBox, QTableWidget, QTableWidgetItem, QWidget,
                             QHeaderView)

from ..connections import DeviceConnectionManager
from ..drivers.scpi import ScpiError
from ..drivers.siglent_ssg import (SiglentSsgDriver, SsgModulation, SsgPoint, SsgSweep,
                                  VIRTUAL_SSG_RESOURCE)
from .device_windows import BaseDeviceWindow, UnitValueWidget, page_shell, value_spin


class SiglentWindow(BaseDeviceWindow):
    def __init__(self, record, connection_manager=None, parameter_repository=None):
        self.connection_manager = connection_manager or DeviceConnectionManager()
        self.session = self.connection_manager.session_for(record)
        self.driver = SiglentSsgDriver(self.session)
        self.owner = f"siglent-native:{record.id}"
        self._busy = False
        self._pending_safety = []
        self.controls = {}
        self.write_buttons = []
        super().__init__(record, [
            ("连接与射频", self._rf_page), ("原生扫描与触发", self._sweep_page),
            ("模拟调制", self._mod_page), ("状态与诊断", self._diagnostics_page)], parameter_repository)
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
        self.power = self._number("rf.power", -20, -140, 30, "dBm", 2)
        form.addRow("载波频率", self.frequency)
        form.addRow("功率", self.power)
        layout.addLayout(form)
        row = QHBoxLayout()
        self._button(row, "应用频率", self.apply_frequency, write=True)
        self._button(row, "应用功率", self.apply_power, write=True)
        self._button(row, "RF 开启", lambda: self._run(lambda: self.driver.set_output(True)), write=True)
        self._button(row, "RF 关闭", lambda: self._safety(lambda: self.driver.set_output(False)))
        layout.addLayout(row)
        self.rf_status = QLabel("射频状态：尚未回读")
        self.rf_status.setWordWrap(True)
        layout.addWidget(self.rf_status)
        layout.addStretch()
        return page

    def _sweep_page(self):
        page, layout = page_shell("仪器原生扫描", "外部触发线直接连接仪器。扫描触发控制一轮，点触发控制步进；驻留期间的触发行为需实机验证。")
        form = QFormLayout()
        form.addRow("类型", self._combo("sweep.kind", [("等间隔步进", "STEP"), ("自定义列表", "LIST")]))
        form.addRow("扫描量", self._combo("sweep.target", [("频率", "FREQUENCY"), ("功率", "LEVEL"), ("频率 + 功率", "LEV_FREQ")]))
        form.addRow("起始 / 终止频率", self._pair(self._frequency("sweep.start_hz", 1e9), self._frequency("sweep.stop_hz", 1.1e9)))
        form.addRow("起始 / 终止功率", self._pair(self._number("sweep.start_dbm", -20, -140, 30, "dBm", 2), self._number("sweep.stop_dbm", -20, -140, 30, "dBm", 2)))
        points = self._bind("sweep.points", QSpinBox())
        points.setRange(2, 65535)
        points.setValue(11)
        form.addRow("步进点数 / 驻留", self._pair(points, self._number("sweep.dwell_s", .03, .01, 100, "s", 4)))
        form.addRow("形状 / 间隔", self._pair(
            self._combo("sweep.shape", [("锯齿", "SAWTOOTH"), ("三角", "TRIANGLE")]),
            self._combo("sweep.spacing", [("线性", "LINEAR"), ("对数", "LOGARITHMIC")])))
        form.addRow("方向 / 模式", self._pair(
            self._combo("sweep.direction", [("正向", "FWD"), ("反向", "REV")]),
            self._combo("sweep.mode", [("连续", "CONTINUE"), ("单次", "SINGLE")])))
        triggers = [("自动", "AUTO"), ("仪器面板按键", "KEY"), ("总线 *TRG", "BUS"), ("外部输入", "EXT")]
        form.addRow("扫描触发 / 点触发", self._pair(self._combo("sweep.sweep_trigger", triggers), self._combo("sweep.point_trigger", triggers)))
        self.controls["sweep.point_trigger"].setCurrentIndex(3)
        form.addRow("扫描外部触发沿", self._combo("sweep.slope", [("上升沿", "POSITIVE"), ("下降沿", "NEGATIVE")]))
        layout.addLayout(form)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["频率 / Hz", "功率 / dBm", "驻留 / s"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(150)
        layout.addWidget(self.table)
        row = QHBoxLayout()
        self._button(row, "添加列表行", self.add_row)
        self._button(row, "删除选中行", self.remove_rows)
        self._button(row, "从仪器读取列表", self.read_list)
        layout.addLayout(row)
        self.add_row(SsgPoint(1e9, -20))
        self.add_row(SsgPoint(1.1e9, -20))
        row = QHBoxLayout()
        self._button(row, "应用配置（保持停止）", self.apply_sweep, write=True)
        self._button(row, "启用 / 布防", self.start_sweep, write=True)
        self._button(row, "执行单次", lambda: self._run(self.driver.execute_single), write=True)
        self._button(row, "停止扫描", lambda: self._safety(self.driver.stop))
        layout.addLayout(row)
        row = QHBoxLayout()
        self._button(row, "发送全局 *TRG", lambda: self._run(self.driver.bus_trigger), write=True)
        if self.record.resource == VIRTUAL_SSG_RESOURCE:
            for label, value in (("模拟外部沿", "EXT"), ("模拟面板按键", "KEY")):
                self._button(row, label, lambda v=value: self._run(lambda: self.session.write(":SIM:TRIGger " + v)), write=True)
        layout.addLayout(row)
        note = QLabel("*TRG 为全局触发：也可能触发设为 BUS 的脉冲或低频功能。KEY 由仪器面板操作。停止扫描不关闭 RF。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.sweep_status = QLabel("扫描状态：尚未回读")
        self.sweep_status.setWordWrap(True)
        layout.addWidget(self.sweep_status)
        return self._scroll(page)

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
        self._button(layout, "触发脉冲（全局 *TRG）", lambda: self._run(self.driver.pulse_bus_trigger), write=True)
        bus_note = QLabel("BUS：先应用配置，再按触发按钮；每次只发送一个 *TRG。该命令也可能触发设为 BUS 的扫描或低频功能，不自动打开 RF。")
        bus_note.setWordWrap(True)
        layout.addWidget(bus_note)
        note = QLabel("本版提供单脉冲和已核对的双调制组合，FM/PM 偏移采用保守范围。IQ/ARB 仅回读。选件是否可用以仪器回读为准；多路组合不自动推断兼容。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.mod_status = QLabel("调制状态：尚未回读")
        self.mod_status.setWordWrap(True)
        layout.addWidget(self.mod_status)
        return self._scroll(page)

    def _diagnostics_page(self):
        page, layout = page_shell("状态与诊断", "原始写命令可能改变输出；仅在扫描停止时允许。命令失败不会自动重放。选件查询未在共同手册中确认。")
        self.readback = QPlainTextEdit()
        self.readback.setReadOnly(True)
        layout.addWidget(self.readback)
        self._button(layout, "读取扫描 / 调制详细参数", lambda: self._run(
            self.driver.configuration_snapshot,
            callback=lambda result: self.readback.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))))
        self.command = self._bind("diagnostics.command", QLineEdit("*IDN?"))
        layout.addWidget(self.command)
        self._button(layout, "执行单条 SCPI", self.send_raw)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(300)
        layout.addWidget(self.log)
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
        for column, value in enumerate((point.frequency_hz, point.power_dbm, point.dwell_s)):
            self.table.setItem(row, column, QTableWidgetItem(f"{value:.12g}"))

    def remove_rows(self):
        for row in sorted({item.row() for item in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(row)

    def _table_points(self):
        return tuple(SsgPoint(*(float(self.table.item(row, col).text()) for col in range(3))) for row in range(self.table.rowCount()))

    def apply_sweep(self):
        try:
            config = self._sweep_config()
        except (ValueError, AttributeError) as exc:
            self._error(f"列表存在无效数字：{exc}")
            return
        self._run(lambda: self.driver.prepare_sweep(config))

    def _sweep_config(self):
        values = self._values("sweep")
        return SsgSweep(**values, table=self._table_points() if values["kind"] == "LIST" else ())

    def start_sweep(self):
        try:
            if self._sweep_config() != self.driver.prepared:
                raise ValueError("扫描参数尚未应用或已有编辑，请先应用配置")
        except (ValueError, AttributeError) as exc:
            self._error(str(exc))
            return
        self._run(self.driver.start)

    def apply_frequency(self):
        value = self.frequency.value()
        self._run(lambda: self.driver.set_frequency(value))

    def apply_power(self):
        value = self.power.value()
        self._run(lambda: self.driver.set_power(value))

    def apply_modulation(self):
        config = SsgModulation(**self._values("mod"))
        self._run(lambda: self.driver.apply_modulation(config))

    def read_list(self):
        def success(points):
            self.table.setRowCount(0)
            for point in points:
                self.add_row(point)
        self._run(self.driver.read_list, callback=success)

    def send_raw(self):
        command = self.command.text()
        self._run(lambda: self.driver.raw(command), callback=lambda result: self.log.appendPlainText(f"> {command}\n{result}"))

    def connect_device(self):
        def operation():
            if not self.session.is_connected:
                self.connection_manager.info(self.record)
                self.connection_manager.connect(self.record)
            self.driver.identify()
        self._run(operation)

    def disconnect_device(self):
        if self._busy or self.driver.state == "enabled" or self.connection_manager.lease_owner(self.record.id):
            self._error("请先停止扫描并确认状态，再断开连接")
            return
        self.connection_manager.disconnect(self.record)
        self.driver.prepared = None
        self.driver.state = "unknown"
        self.driver_status.setText("● 已断开；硬件输出状态未确认")
        self._update_buttons()

    def refresh(self):
        self._run(lambda: None)

    def _poll(self):
        if self.session.is_connected and not self._busy and self.driver.state == "enabled":
            self.refresh()

    def _safety(self, operation):
        if self._busy:
            self._pending_safety.append(operation)
            self.statusBar().showMessage("停止 / RF 关闭已排队，当前通信结束后优先执行")
        else:
            self._run(operation)

    def _run(self, operation, callback=None):
        if self._busy:
            return
        try:
            self.connection_manager.acquire_devices(self.owner, [self.record.id])
        except Exception as exc:
            self._error(str(exc))
            return
        self._busy = True
        self._update_buttons()
        def work():
            try:
                result = operation()
                return result, self.driver.snapshot(), None
            except ValueError as exc:
                return None, None, str(exc)
            except Exception as exc:
                self.driver.state = "unknown"
                # A timed-out response must not contaminate the next query.
                try:
                    self.session.disconnect()
                except Exception:
                    pass
                raise ScpiError(str(exc)) from exc
        def finish(payload):
            result, snapshot, validation_error = payload
            if snapshot is not None:
                self._show_snapshot(snapshot)
            if validation_error:
                self._error(validation_error)
            elif callback:
                callback(result)
            elif result:
                message = "；".join(result) if isinstance(result, tuple) else str(result)
                self.log.appendPlainText(message)
                self.statusBar().showMessage(message, 20000)
            if self.driver.state == "off" or not self.driver.caps.known:
                self.connection_manager.release_devices(self.owner)
            self._finished()
        def failure(message):
            self.rf_status.setText("通信失败：RF / 扫描状态未知，请重连回读，必要时在仪器面板停止输出。")
            self.sweep_status.setText("扫描状态未知；没有自动重试触发或启动命令。")
            self.live_status.setText("扫描：状态未知")
            self._error(message)
            if not self.driver.caps.known:
                self.connection_manager.release_devices(self.owner)
            self._finished()
        self.submit(work, on_success=finish, on_error=failure)

    def _finished(self):
        self._busy = False
        self._update_buttons()
        if self._pending_safety:
            operation = self._pending_safety.pop(0)
            QTimer.singleShot(0, lambda: self._run(operation))

    def _error(self, message):
        self.statusBar().showMessage(message, 20000)
        self.log.appendPlainText("错误：" + message)

    def _show_snapshot(self, snapshot):
        c = self.driver.caps
        self.identity_label.setText(f"{c.model}   序列号：{c.serial}   固件：{c.firmware}")
        self.capability_label.setText(
            f"频率范围 {c.minimum_hz:g}–{c.maximum_hz:g} Hz；FM/PM：{'已确认硬件支持' if c.fm_pm else '未确认，禁用'}；选件：未查询。功率范围随频率校验。"
            if c.known else "未知型号：仅允许查询")
        if not c.known:
            self.readback.setPlainText(json.dumps(snapshot, ensure_ascii=False, indent=2))
            return
        self.driver_status.setText("● 已连接 · " + ("扫描已启用" if self.driver.state == "enabled" else "扫描已关闭"))
        self.live_status.setText("扫描：" + ("已启用 / 等待触发或运行" if self.driver.state == "enabled" else "已关闭"))
        self.rf_status.setText(f"实际频率：{snapshot[':FREQuency']} Hz　功率：{snapshot[':POWer']} dBm　RF：{snapshot[':OUTPut']}")
        self.sweep_status.setText(
            f"回读扫描：{snapshot[':SWEep:STATe']}；模式：{snapshot[':SWEep:MODE']}\n"
            f"扫描触发：{snapshot[':SWEep:SWEep:TRIGger:TYPE']}；点触发：{snapshot[':SWEep:POINt:TRIGger:TYPE']}\n"
            f"当前点（仪器回读）：{snapshot.get(':SWEep:CURRent:DATA', '—')}\n"
            "扫描启用时可能正在等待触发；当前点不足以判断单次完成。")
        self.mod_status.setText("实际开关：" + "  ".join(f"{name}={snapshot.get(':'+name+':STATe', '未确认')}" for name in ("AM", "FM", "PM", "PULM")) +
                               f"\n脉冲源={snapshot[':PULM:SOURce']}；脉冲触发={snapshot[':PULM:TRIGger:MODE']}" +
                               f"\n调制总开关={snapshot[':OUTPut:MODulation']}；IQ={snapshot.get(':FUNCtion:DM:STATe', '不适用')}；RF Blanking={snapshot.get(':RADio:ARB:MDEStination:PULSe', '不适用')}")
        self.readback.setPlainText(json.dumps(snapshot, ensure_ascii=False, indent=2))

    def _update_buttons(self):
        enabled = self.session.is_connected and self.driver.caps.known and not self._busy
        for button in self.write_buttons:
            button.setEnabled(enabled)
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
        if not self.session.is_connected:
            self.driver_status.setText("● 未连接 · 硬件状态未确认")
            self.live_status.setText("扫描：未连接 / 状态未知")
        self._update_buttons()

    def _custom_device_parameters(self):
        return {"list": [[self.table.item(row, col).text() if self.table.item(row, col) else "" for col in range(3)] for row in range(self.table.rowCount())]}

    def _restore_custom_device_parameters(self, state):
        rows = state.get("list")
        if not isinstance(rows, list) or len(rows) > 500 or any(not isinstance(row, list) or len(row) != 3 for row in rows):
            return
        self.table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))

    def closeEvent(self, event):
        if self._busy or self._call_tasks or self._pending_safety or self.connection_manager.lease_owner(self.record.id) == self.owner:
            self._error("扫描启用或状态未确认：请停止并成功回读后关闭窗口")
            event.ignore()
            return
        self.poll_timer.stop()
        super().closeEvent(event)
