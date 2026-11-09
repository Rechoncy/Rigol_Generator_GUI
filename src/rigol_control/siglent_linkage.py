"""SIGLENT linkage: verified CW setting batches or instrument-native EXT LIST.

The two paths are explicit. Native EXT edges never get forwarded as SCPI *TRG
and NI counts are not evidence of an instrument's accepted point/trigger count.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from threading import RLock

from .drivers.dg4000 import StepParameter
from .drivers.scpi import ScpiError
from .drivers.siglent_ssg import (
    SiglentSsgDriver, SsgPoint, SsgSweep, VirtualSsgBackend, capabilities, _on,
)
from .linkage_parameters import LinkageParameter
from .units import roundtrip_number, unit_spec
from .virtual_lab import is_virtual_resource


class _SsgLinkageSource:
    def __init__(self, session, device_points):
        self.session = session
        self.driver = SiglentSsgDriver(session)
        self._generation = session.connection_generation
        self._identity = session.identity
        self._resource = session.resource
        self.driver.caps = capabilities(self._identity)
        self.points = tuple(tuple(values) for values in device_points)
        self._assert_binding()
        if not self.points:
            raise ValueError("鼎阳联动点表不能为空")
        self._targets = tuple(item.parameter for item in self.points[0])
        for values in self.points:
            self.validate_step_values(values)
            if tuple(item.parameter for item in values) != self._targets:
                raise ValueError("鼎阳联合点表的参数及顺序必须一致")

    def _assert_binding(self):
        if (self.session.resource != self._resource or not self.session.is_connected
                or self.session.connection_generation != self._generation
                or self.session.identity != self._identity):
            raise ScpiError("鼎阳连接已经断开或重建，原联动任务已失效；请停止并重新布防")
        self.driver._known()

    def validate_step_values(self, values):
        self._assert_binding()
        values = tuple(values)
        if not values:
            raise ValueError("鼎阳联动点至少需要一个参数")
        result = {}
        for item in values:
            if item.channel != 1:
                raise ValueError("鼎阳只有一个射频输出，联动通道必须为 1")
            if item.parameter in result:
                raise ValueError("鼎阳联动点不能重复设置同一参数")
            value = float(item.value)
            if not math.isfinite(value):
                raise ValueError("鼎阳联动参数必须是有限数值")
            if item.parameter is StepParameter.FREQUENCY:
                unit_spec("frequency", item.unit_key or "Hz")
                if not self.driver.caps.minimum_hz <= value <= self.driver.caps.maximum_hz:
                    raise ValueError("鼎阳联动频率超出已连接型号范围")
            elif item.parameter is LinkageParameter.RF_POWER:
                if item.unit_key != "dBm":
                    raise ValueError("鼎阳联动射频功率必须明确使用 dBm")
            else:
                raise ValueError("鼎阳跨设备联动仅支持频率和射频输出功率（dBm）")
            result[item.parameter] = value
        return result

    def _number(self, command):
        self._assert_binding()
        raw = self.session.query(command, recover=False)
        try:
            value = float(raw)
        except (ValueError, TypeError) as exc:
            raise ScpiError(f"鼎阳联动回读无效 [{command}]：{raw!r}") from exc
        if not math.isfinite(value):
            raise ScpiError(f"鼎阳联动回读不是有限数值 [{command}]：{raw!r}")
        return value

    def _context(self):
        self._assert_binding()
        self.driver._idle()
        if _on(self.session.query(":POWer:SPC:STATe?", recover=False)):
            raise ValueError("功率传感器闭环控制已开启，不能执行鼎阳联合点设置")
        iq = self.driver.caps.vector and _on(self.session.query(":FUNCtion:DM:STATe?", recover=False))
        point = SsgPoint(self._number(":FREQuency?"), self._number(":POWer?"))
        self._validate_point(point, iq)
        return point, iq

    def _validate_point(self, point, iq):
        self.driver.validate_point(point)
        if iq and point.frequency_hz < 1e7:
            raise ValueError("IQ 开启时鼎阳联动频率至少为 10 MHz")

    def _resolve(self, values, current, dwell=.01):
        mapped = self.validate_step_values(values)
        return SsgPoint(mapped.get(StepParameter.FREQUENCY, current.frequency_hz),
                        mapped.get(LinkageParameter.RF_POWER, current.power_dbm), dwell)


class SsgSoftwareLinkageWriter(_SsgLinkageSource):
    """Bind a CW run and verify each accepted batch, never retrying settings."""
    def __init__(self, session, device_points):
        super().__init__(session, device_points)
        self._prepared = False

    def _ordered_commands(self, values, current, target):
        mapped = self.validate_step_values(values)
        frequency = f":FREQuency {roundtrip_number(target.frequency_hz)}"
        power = f":POWer {roundtrip_number(target.power_dbm)}"
        if len(mapped) == 1:
            return [frequency] if StepParameter.FREQUENCY in mapped else [power]
        low, high = self.driver.caps.power_bounds(target.frequency_hz)
        if low <= current.power_dbm <= high:
            return [frequency, power]
        low, high = self.driver.caps.power_bounds(current.frequency_hz)
        if low <= target.power_dbm <= high:
            return [power, frequency]
        raise ValueError("鼎阳频率/功率切换没有合法的中间设置；请在点表中加入过渡点")

    def prepare_trigger(self):
        self._prepared = False
        with self.session.transaction():
            current, iq = self._context()
            initial = current
            for values in self.points:
                target = self._resolve(values, current)
                self._validate_point(target, iq)
                self._ordered_commands(values, current, target)
                current = target
            # Every later cycle must also have a valid last -> P0 transition.
            first = self._resolve(self.points[0], initial)
            self._ordered_commands(self.points[0], current, first)
            self._assert_binding()
            self._prepared = True
        return self

    def apply_steps(self, values):
        with self.session.transaction():
            if not self._prepared:
                raise ScpiError("鼎阳软件联动尚未完成预检，或上次下发结果未知；请重新布防")
            try:
                current, iq = self._context()
                target = self._resolve(values, current)
                self._validate_point(target, iq)
                commands = self._ordered_commands(values, current, target)
                self.driver._batch(commands)
                mapped = self.validate_step_values(values)
                if StepParameter.FREQUENCY in mapped:
                    self.driver._verify(":FREQuency", target.frequency_hz)
                if LinkageParameter.RF_POWER in mapped:
                    self.driver._verify(":POWer", target.power_dbm)
                self._assert_binding()
            except Exception:
                self._prepared = False
                raise


class SsgNativeLinkageParticipant(_SsgLinkageSource):
    """Preload the expanded joint table and use the source's own EXT input."""
    def __init__(self, session, device_points, *, rising=True, dwell_s=.01,
                 reconnect_for_stop=None):
        super().__init__(session, device_points)
        if not 2 <= len(self.points) <= 500:
            raise ValueError("鼎阳原生列表联动的展开联合点数必须为 2 至 500")
        if not math.isfinite(dwell_s) or not .01 <= dwell_s <= 100:
            raise ValueError("鼎阳原生列表驻留必须在 0.01 至 100 秒之间")
        self.rising, self.dwell_s = bool(rising), dwell_s
        self._config = None
        self._requires_stop = False
        self._run_invalidated = False
        self._cancellation_probe: Callable[[], bool] | None = None
        self._reconnect_for_stop = reconnect_for_stop
        self._operation_lock = RLock()

    @property
    def requires_stop(self):
        # Cached evidence only: status readers must never wait for VISA I/O.
        return self._requires_stop

    def set_cancellation_probe(self, should_continue: Callable[[], bool]):
        self._cancellation_probe = should_continue

    def prepare(self):
        with self._operation_lock, self.session.transaction():
            if self.requires_stop:
                raise ScpiError("鼎阳上次原生扫描尚未确认停止，不能重新布防")
            if self._run_invalidated:
                raise ScpiError("鼎阳原生联动已执行恢复停止；请重新建立任务并布防")
            self._config = None
            current, iq = self._context()
            table = tuple(self._resolve(values, current, self.dwell_s) for values in self.points)
            for point in table:
                self._validate_point(point, iq)
            target = ("LEV_FREQ" if len(self._targets) == 2 else
                      "FREQUENCY" if self._targets[0] is StepParameter.FREQUENCY else "LEVEL")
            config = SsgSweep(kind="LIST", target=target, mode="CONTINUE",
                              direction="FWD", sweep_trigger="AUTO", point_trigger="EXT",
                              slope="POSITIVE" if self.rising else "NEGATIVE", table=table)
            self.driver.prepare_sweep(config)
            self._assert_binding()
            self._config = config

    def arm(self):
        with self._operation_lock, self.session.transaction():
            self._assert_binding()
            if self._config is None or self.requires_stop or self._run_invalidated:
                raise ScpiError("鼎阳原生联动请先完成列表上传与回读")
            # Mark before attempting an enable: a lost reply may follow acceptance.
            self._requires_stop = True
            if self._cancellation_probe is None:
                result = self.driver.start()
            else:
                result = self.driver.start(should_continue=self._cancellation_probe)
            self._assert_binding()
            if result is False:
                self._requires_stop = False

    def stop(self):
        with self._operation_lock:
            if not self.requires_stop:
                return
            with self.session.transaction():
                self._assert_binding()
                self.driver.stop()
                self._assert_binding()
                self._requires_stop = False

    def recover_and_stop(self):
        """Explicit retry only: reconnect the same identity and confirm OFF.

        The controller calls this only after a visible cleanup failure and a
        user-requested retry. Never reinstate the old list or arm the source.
        """
        with self._operation_lock, self.session.transaction():
            if not self.requires_stop:
                return
            if self.session.resource != self._resource or not self.driver.caps.serial:
                raise ScpiError("无法确认原鼎阳资源和序列号；保留占用，请核对仪器后重试")
            self._run_invalidated = True
            self._config = None
            self.driver.prepared = None
            self.session.disconnect()
            if self._reconnect_for_stop is None:
                self.session.connect()
            else:
                self._reconnect_for_stop()
            if not self.session.is_connected or self.session.identity != self._identity:
                raise ScpiError("重连后的鼎阳身份与原任务不符；未发送停止或恢复设置，已保留占用")
            self._generation = self.session.connection_generation
            self._assert_binding()
            self.driver.stop()
            self._assert_binding()
            self._requires_stop = False

    def emit_virtual_trigger(self, count=1):
        if not is_virtual_resource(self.session.resource):
            raise ValueError("仅内置模拟器可模拟原生 EXT 输入")
        if type(count) is not int or count < 1:
            raise ValueError("模拟触发次数必须为正整数")
        with self.session.transaction():
            self._assert_binding()
            backend = getattr(self.session, "_backend", None)
            if not isinstance(backend, VirtualSsgBackend):
                raise ValueError("未连接内置鼎阳模拟器")
            for _ in range(count):
                backend.trigger("EXT")
