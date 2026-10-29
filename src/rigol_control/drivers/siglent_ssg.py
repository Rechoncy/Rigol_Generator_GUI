"""SIGLENT native sweep driver. No NI timing, automatic restart, or RF enable.

Command references and conservative capability limits: docs/SIGLENT_SSG_CN.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
import math
import re
import time

from .scpi import ScpiError, ScpiSession
from ..readback import ReadbackDeferred, check_readback_allowed
from ..units import to_base

VIRTUAL_SSG_RESOURCE = "SIM::SIGLENT::SSG6082A-V"


class SsgCommandError(ScpiError):
    """The instrument answered, but rejected a command or its readback."""


@dataclass(frozen=True)
class SsgCapabilities:
    model: str
    serial: str
    firmware: str
    minimum_hz: float = 0
    maximum_hz: float = 0
    vector: bool = False
    fm_pm: bool = False

    @property
    def known(self) -> bool:
        return self.maximum_hz > 0

    @property
    def pulse_bus(self) -> bool:
        # SSG3000X user manual C01B 8.4.4.13 explicitly lists BUS / *TRG.
        # Its programming guide omits BUS from the enumeration; verify readback.
        return self.known and (self.vector or self.model in ("SSG3021X", "SSG3032X"))

    def power_bounds(self, frequency: float) -> tuple[float, float]:
        # Standard hardware setting limits, not guaranteed calibrated output power.
        # V uses a conservative common envelope; optional high power is not assumed.
        if self.vector:
            return -110, 10
        if self.model.startswith("SSG30"):
            return -110, 9 if frequency < 1e5 else 15 if frequency < 1e6 else 20
        if frequency < 1e6:
            return -110, 15
        if frequency < 3e6:
            return -110, 24
        if frequency <= 4e9:
            return -130, 24
        if frequency <= 6e9:
            return -130, 20
        return -120, 25 if frequency <= 20e9 else 20


def capabilities(identity: str) -> SsgCapabilities:
    parts = [s.strip() for s in identity.split(",")]
    parts += [""] * (4 - len(parts))
    vendor, model, serial, firmware = parts[:4]
    limits = {"SSG3021X": (9e3, 2.1e9), "SSG3032X": (9e3, 3.2e9),
              "SSG6083A": (1e5, 13.6e9), "SSG6085A": (1e5, 20e9),
              "SSG6087A": (1e5, 40e9), "SSG6082A-V": (9e3, 8e9)}
    model = model.upper()
    low, high = limits.get(model, (0, 0)) if "SIGLENT" in vendor.upper() else (0, 0)
    vector = model == "SSG6082A-V"
    newer = bool(re.fullmatch(r"SSG6[A-Z0-9]{2}[3-9][A-Z0-9]+", serial.upper()))
    return SsgCapabilities(model, serial, firmware, low, high, vector,
                           bool(high) and (vector or model.startswith("SSG30") or newer))


@dataclass(frozen=True)
class SsgOutputReadback:
    """Sequential RF configuration queries, not an electrical measurement."""

    frequency_hz: float
    power_dbm: float
    output_enabled: bool
    connection_generation: int


@dataclass(frozen=True)
class SsgPoint:
    frequency_hz: float
    power_dbm: float
    dwell_s: float = 0.03


@dataclass(frozen=True)
class SsgSweep:
    kind: str = "STEP"
    target: str = "FREQUENCY"
    start_hz: float = 1e9
    stop_hz: float = 1.1e9
    start_dbm: float = -20
    stop_dbm: float = -20
    points: int = 11
    dwell_s: float = 0.03
    shape: str = "SAWTOOTH"
    spacing: str = "LINEAR"
    direction: str = "FWD"
    mode: str = "CONTINUE"
    sweep_trigger: str = "AUTO"
    point_trigger: str = "EXT"
    slope: str = "POSITIVE"
    table: tuple[SsgPoint, ...] = ()


@dataclass(frozen=True)
class SsgModulation:
    am: bool = False
    fm: bool = False
    pm: bool = False
    pulse: bool = False
    master: bool = False
    am_source: str = "INTERNAL"
    fm_source: str = "INTERNAL"
    pm_source: str = "INTERNAL"
    am_wave: str = "SINE"
    fm_wave: str = "SINE"
    pm_wave: str = "SINE"
    am_rate: float = 1000
    fm_rate: float = 1000
    pm_rate: float = 1000
    am_depth: float = 30
    fm_deviation: float = 1000
    pm_deviation: float = 0.1
    pulse_source: str = "INTERNAL"
    pulse_trigger: str = "AUTO"
    pulse_period: float = 0.001
    pulse_width: float = 0.0005
    pulse_delay: float = 0.000001
    pulse_slope: str = "POSITIVE"
    gate_polarity: str = "NORMAL"


def _range(value: float, low: float, high: float, label: str) -> None:
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{label} 应在 {low:g} 至 {high:g} 之间")


def _choice(value: str, choices: tuple[str, ...], label: str) -> None:
    if value not in choices:
        raise ValueError(f"{label} 不支持 {value!r}")


def _on(value: str) -> bool:
    value = value.strip().upper()
    if value not in ("0", "1", "OFF", "ON"):
        raise ScpiError(f"无法解析开关回读：{value!r}")
    return value in ("1", "ON")


def _enum_value(value: str) -> str:
    # Canonical SCPI short forms; prefix matching confuses LEV with LEV_FREQ.
    aliases = {"FREQ": "FREQUENCY", "LEV": "LEVEL", "CONT": "CONTINUE",
               "SING": "SINGLE", "TRI": "TRIANGLE", "SAW": "SAWTOOTH",
               "LIN": "LINEAR", "LOG": "LOGARITHMIC", "POS": "POSITIVE",
               "NEG": "NEGATIVE", "INT": "INTERNAL", "EXT": "EXTERNAL",
               "SQU": "SQUARE", "SQUA": "SQUARE", "NORM": "NORMAL",
               "INV": "INVERTED", "EGAT": "EGATE", "DOUB": "DOUBLE"}
    return ",".join(aliases.get(part.strip().upper(), part.strip().upper())
                    for part in value.split(","))


def _power_close(actual: float, expected: float) -> bool:
    # SSG amplitude setting resolution is 0.01 dB. Voltage conversions commonly
    # have more digits; accept one setting step, not the RF accuracy specification.
    return math.isclose(actual, expected, rel_tol=0, abs_tol=0.01 + 1e-9)


class SiglentSsgDriver:
    def __init__(self, session: ScpiSession):
        self.session = session
        self.caps = capabilities("")
        self.prepared: SsgSweep | None = None
        self.state = "unknown"
        # A failed status query is not evidence that this window started a scan.
        # Keep this evidence across disconnects until OFF is actually read back.
        self.scan_may_be_active = False

    def identify(self) -> SsgCapabilities:
        self.caps = capabilities(self.session.query("*IDN?"))
        self.prepared = None
        return self.caps

    def read_output_parameters(self, *, should_continue: Callable[[], bool] | None = None) -> SsgOutputReadback:
        """Read RF parameters without changing scan state or recovering a session.

        SSG3000X C01A 3.4.3.2/3.4.4.2/3.3.1 define Hz, dBm and 1|0.
        These common headers are also used by the supported SSG6000 variants.
        No modulation, sweep-current, ESR or optional queries are appended.
        """
        self._known()

        def query(command):
            check_readback_allowed(should_continue)
            try:
                result = self.session.query(command, recover=False)
            except ReadbackDeferred:
                raise
            except Exception as exc:
                raise SsgCommandError(f"SSG 参数回读失败 [{command}]：{exc}") from exc
            check_readback_allowed(should_continue)
            return result

        def number(command, *, positive=False):
            raw = query(command)
            try:
                value = float(raw)
                if not math.isfinite(value) or (positive and value <= 0):
                    raise ValueError("无效数值")
            except (TypeError, ValueError, OverflowError) as exc:
                raise SsgCommandError(f"SSG 数值回读无效 [{command}]：{raw!r}") from exc
            return value

        check_readback_allowed(should_continue)
        with self.session.transaction():
            generation = self.session.connection_generation
            frequency = number(":FREQuency?", positive=True)
            power = number(":POWer?")
            output = query(":OUTPut?")
            try:
                enabled = _on(output)
            except (AttributeError, ScpiError) as exc:
                raise SsgCommandError(f"SSG 输出状态回读无效 [:OUTPut?]：{output!r}") from exc
            if self.session.connection_generation != generation:
                raise SsgCommandError("SSG 回读期间连接已变化，本次结果已丢弃")
            check_readback_allowed(should_continue)
            return SsgOutputReadback(frequency, power, enabled, generation)

    def _known(self):
        if not self.caps.known:
            raise ValueError("未识别的型号仅允许查询；请先连接并读取身份")

    @property
    def modulation_header(self) -> str:
        # C01A 3.4.7.1 uses MODulation in both the primary syntax and example.
        # A user's SSG3021X timed out on the documented OUTPut alias.
        return ":MODulation" if self.caps.model in ("SSG3021X", "SSG3032X") else ":OUTPut:MODulation"

    def _idle(self):
        self._known()
        if self.read_sweep_state().upper() != "OFF":
            raise ValueError("扫描已启用，请先停止扫描再修改配置")

    def read_sweep_state(self) -> str:
        try:
            actual = self.session.query(":SWEep:STATe?")
            value = _enum_value(actual)
            if value not in ("OFF", "FREQUENCY", "LEVEL", "LEV_FREQ"):
                raise SsgCommandError(f"无法解析扫描状态 [:SWEep:STATe?]：{actual!r}")
        except Exception:
            self.state = "unknown"
            raise
        self.state = "off" if value == "OFF" else "enabled"
        self.scan_may_be_active = value != "OFF"
        return actual

    def _errors(self, *, command: str | None = None):
        value = int(self.session.query("*ESR?"))
        if value & 0x3C:
            reasons = "、".join(label for bit, label in ((32, "命令错误"), (16, "执行错误"),
                                                     (8, "设备错误"), (4, "查询错误")) if value & bit)
            if command is None:
                raise SsgCommandError(f"下发前检测到历史 SCPI 错误（ESR={value}：{reasons}）；"
                                      "本批命令尚未发送。*ESR? 已读取并清除事件值，请核对后重新应用。")
            raise SsgCommandError(f"下发命令 [{command}] 后仪器报告 SCPI 错误（ESR={value}：{reasons}）；"
                                  "已停止后续下发，部分配置可能生效，请回读检查。")

    def _batch(self, commands: list[str]):
        # ESR is documented in all three guides; SYST:ERR? and *OPT? are not.
        self._errors()
        for command in commands:
            if command.upper().startswith(":SWEEP:STATE ") and command.split()[-1].upper() != "OFF":
                self.scan_may_be_active = True
            self.session.write(command, recover=False)
            self._errors(command=command)

    def validate_point(self, point: SsgPoint):
        self._known()
        _range(point.frequency_hz, self.caps.minimum_hz, self.caps.maximum_hz, "频率 / Hz")
        _range(point.power_dbm, *self.caps.power_bounds(point.frequency_hz), "功率 / dBm")
        _range(point.dwell_s, 0.01, 100, "驻留时间 / s")

    def set_frequency(self, frequency: float):
        with self.session.transaction():
            self._idle()
            power = float(self.session.query(":POWer?"))
            self.validate_point(SsgPoint(frequency, power))
            if self.caps.vector and _on(self.session.query(":FUNCtion:DM:STATe?")) and frequency < 1e7:
                raise ValueError("IQ 开启时频率至少为 10 MHz")
            self._batch([f":FREQuency {frequency:.12g}"])
            self._verify(":FREQuency", frequency)

    def set_power(self, power: float, unit: str = "dBm"):
        power = to_base(power, "rf_power", unit)
        with self.session.transaction():
            self._idle()
            self.validate_point(SsgPoint(float(self.session.query(":FREQuency?")), power))
            self._batch([f":POWer {power:.12g}"])
            self._verify(":POWer", power)

    def set_output(self, enabled: bool):
        with self.session.transaction():
            self._known()
            # RF off remains available even with a pre-existing instrument error.
            if enabled:
                self._batch([":OUTPut ON"])
            else:
                self.session.write(":OUTPut OFF")
            self._verify(":OUTPut", int(enabled))

    def _verify(self, command: str, expected: float | str):
        actual = self.session.query(command + "?")
        if isinstance(expected, (int, float)):
            try:
                value = float(actual) if actual.upper() not in ("ON", "OFF") else int(_on(actual))
                if command.upper() in (":POWER", ":SWEEP:STEP:START:LEVEL", ":SWEEP:STEP:STOP:LEVEL"):
                    match = _power_close(value, expected)
                else:
                    match = math.isclose(value, expected, rel_tol=1e-9, abs_tol=1e-9)
            except ValueError as exc:
                raise SsgCommandError(f"无法解析回读 {command}：{actual!r}") from exc
        else:
            match = _enum_value(actual) == _enum_value(expected)
        if not match:
            raise SsgCommandError(f"回读不一致 {command}：请求 {expected}，实际 {actual}；请检查选件/参数限制")

    def validate_sweep(self, c: SsgSweep):
        self._known()
        for value, choices, label in (
            (c.kind, ("STEP", "LIST"), "扫描类型"),
            (c.target, ("FREQUENCY", "LEVEL", "LEV_FREQ"), "扫描量"),
            (c.mode, ("CONTINUE", "SINGLE"), "扫描模式"),
            (c.shape, ("SAWTOOTH", "TRIANGLE"), "形状"),
            (c.spacing, ("LINEAR", "LOGARITHMIC"), "间隔"),
            (c.direction, ("FWD", "REV"), "方向"),
            (c.slope, ("POSITIVE", "NEGATIVE"), "触发沿"),
            (c.sweep_trigger, ("AUTO", "KEY", "BUS", "EXT"), "扫描触发"),
            (c.point_trigger, ("AUTO", "KEY", "BUS", "EXT"), "点触发")):
            _choice(value, choices, label)
        if c.kind == "LIST":
            _range(len(c.table), 2, 500, "列表点数（基础版）")
            for p in c.table:
                self.validate_point(p)
        else:
            if not isinstance(c.points, int):
                raise ValueError("扫描点数必须为整数")
            _range(c.points, 2, 65535, "扫描点数")
            for p in (SsgPoint(c.start_hz, c.start_dbm, c.dwell_s), SsgPoint(c.stop_hz, c.stop_dbm, c.dwell_s)):
                self.validate_point(p)

    def prepare_sweep(self, c: SsgSweep):
        self.validate_sweep(c)
        with self.session.transaction():
            self._idle()
            self.prepared = None
            if _on(self.session.query(":POWer:SPC:STATe?")):
                raise ValueError("功率传感器闭环控制已开启，与原生扫描冲突，请在仪器上关闭后重试")
            # Validate every effective point including fixed carrier/level and band transitions.
            fixed_hz = float(self.session.query(":FREQuency?"))
            fixed_dbm = float(self.session.query(":POWer?"))
            iq = self.caps.vector and _on(self.session.query(":FUNCtion:DM:STATe?"))
            for p in sweep_points(c):
                hz = fixed_hz if c.target == "LEVEL" else p.frequency_hz
                dbm = fixed_dbm if c.target == "FREQUENCY" else p.power_dbm
                self.validate_point(SsgPoint(hz, dbm, p.dwell_s))
                if iq and hz < 1e7:
                    raise ValueError("IQ 开启时扫描频率至少为 10 MHz")
            settings: list[tuple[str, str | float]] = [
                (":SWEep:TYPE", c.kind), (":SWEep:MODE", c.mode),
                (":SWEep:DIRect", c.direction),
                (":SWEep:SWEep:TRIGger:TYPE", c.sweep_trigger),
                (":SWEep:POINt:TRIGger:TYPE", c.point_trigger),
                (":INPut:TRIGger:SLOPe", c.slope)]
            if c.kind == "STEP":
                settings += [(":SWEep:STEP:STARt:FREQuency", c.start_hz),
                             (":SWEep:STEP:STOP:FREQuency", c.stop_hz),
                             (":SWEep:STEP:STARt:LEVel", c.start_dbm),
                             (":SWEep:STEP:STOP:LEVel", c.stop_dbm),
                             (":SWEep:STEP:DWELl", c.dwell_s),
                             (":SWEep:STEP:POINts", c.points),
                             (":SWEep:STEP:SHAPe", c.shape),
                             (":SWEep:STEP:SPACe", c.spacing)]
            self._batch([f"{key} {value}" for key, value in settings])
            if c.kind == "LIST":
                self._batch([":SWEep:LIST:INITialize:PRESet"])
                count = int(self.session.query(":SWEep:LIST:CPOint?"))
                if count not in (0, 1):
                    raise ScpiError(f"清空后列表点数异常：{count}")
                commands = []
                for i, p in enumerate(c.table):
                    prefix = ":SWEep:LIST:CHANGe 1," if i == 0 and count else ":SWEep:LIST:ADDList "
                    commands.append(prefix + f"{p.frequency_hz:.12g},{p.power_dbm:.12g},{p.dwell_s:.12g}")
                self._batch(commands)
                self._verify(":SWEep:LIST:CPOint", len(c.table))
                actual = self.read_list()
                if len(actual) != len(c.table) or any(
                    not (math.isclose(p.frequency_hz, q.frequency_hz, rel_tol=1e-9, abs_tol=1e-9)
                         and _power_close(p.power_dbm, q.power_dbm)
                         and math.isclose(p.dwell_s, q.dwell_s, rel_tol=1e-9, abs_tol=1e-9))
                    for p, q in zip(actual, c.table)):
                    raise ScpiError("扫描列表回读与请求不一致")
            for command, value in settings:
                self._verify(command, value)
            self._verify(":SWEep:STATe", "OFF")
            self.prepared = c
            self.state = "off"

    def read_list(self) -> tuple[SsgPoint, ...]:
        count = int(self.session.query(":SWEep:LIST:CPOint?"))
        if not 0 <= count <= 500:
            raise ScpiError("列表点数超出支持范围")
        result = []
        for start in range(1, count + 1, 50):
            raw = self.session.query(f":SWEep:LIST:LIST? {start},{min(start + 49, count)}")
            values = re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", raw)
            if len(values) % 3:
                raise ScpiError("无法解析扫描列表回读")
            result.extend(SsgPoint(*map(float, values[i:i+3])) for i in range(0, len(values), 3))
        if len(result) != count:
            raise ScpiError(f"列表回读不完整：期望 {count} 行，收到 {len(result)} 行")
        return tuple(result)

    def start(self):
        with self.session.transaction():
            self._idle()
            if self.prepared is None:
                raise ValueError("请先应用扫描配置")
            # Recheck RF/IQ/closed-loop constraints if the front panel changed meanwhile.
            config = self.prepared
            self.prepare_sweep(config)
            self.state = "unknown"
            self._batch([f":SWEep:STATe {config.target}"])
            self._verify(":SWEep:STATe", config.target)
            self.state = "enabled"

    def execute_single(self):
        with self.session.transaction():
            self._known()
            if self.read_sweep_state().upper() == "OFF":
                raise ValueError("请先启用扫描")
            if not self.session.query(":SWEep:MODE?").upper().startswith("SING"):
                raise ValueError("仅单次扫描模式可执行单次")
            self._batch([":SWEep:EXECute"])

    def stop(self):
        with self.session.transaction():
            self._known()
            self.state = "unknown"
            self.session.write(":SWEep:STATe OFF", recover=False)
            actual = self.read_sweep_state()
            if _enum_value(actual) != "OFF":
                raise SsgCommandError(f"停止未确认 [:SWEep:STATe?]：实际 {actual}；扫描仍启用")
            self.state = "off"
            self.scan_may_be_active = False

    def bus_trigger(self):
        self._known()
        try:
            self.session.write("*TRG", recover=False)
        except Exception:
            self.state = "unknown"
            raise

    def pulse_bus_trigger(self):
        """Check the actual pulse configuration, then send one global trigger."""
        self._known()
        if not self.caps.pulse_bus:
            raise ValueError("当前型号尚未开放脉冲 BUS")
        with self.session.transaction():
            if _enum_value(self.session.query(":PULM:SOURce?")) != "INTERNAL":
                raise ValueError("脉冲 BUS 需要内部脉冲源")
            if self.session.query(":PULM:TRIGger:MODE?").upper() != "BUS":
                raise ValueError("仪器脉冲触发尚未设为 BUS，请先应用配置并确认回读")
            if not _on(self.session.query(":PULM:STATe?")) or not _on(self.session.query(self.modulation_header + "?")):
                raise ValueError("请先启用脉冲调制及调制总开关")
            self.bus_trigger()

    def configuration_snapshot(self) -> dict[str, str]:
        """Explicit, potentially slower read of configuration without changing controls."""
        self._known()
        with self.session.transaction():
            keys = [
                ":SWEep:TYPE", ":SWEep:DIRect", ":INPut:TRIGger:SLOPe",
                ":SWEep:STEP:STARt:FREQuency", ":SWEep:STEP:STOP:FREQuency",
                ":SWEep:STEP:STARt:LEVel", ":SWEep:STEP:STOP:LEVel",
                ":SWEep:STEP:DWELl", ":SWEep:STEP:POINts", ":SWEep:STEP:SHAPe", ":SWEep:STEP:SPACe",
                ":PULM:SOURce", ":PULM:TRIGger:MODE", ":PULM:MODE",
                ":PULM:OUT:STATe", ":PULM:TRIGger:STATe",
            ]
            for name in ("AM", "FM", "PM") if self.caps.fm_pm else ("AM",):
                prefix = "FM1" if name == "FM" and self.caps.vector else name
                keys += [f":{name}:SOURce", f":{prefix}:WAVEform", f":{prefix}:FREQuency",
                         f":{prefix}:DEPTh" if name == "AM" else f":{prefix}:DEViation"]
            result = {key: self.session.query(key + "?") for key in keys}
            if _enum_value(result[":PULM:SOURce"]) == "INTERNAL":
                if _enum_value(result[":PULM:MODE"]) in ("SINGLE", "DOUBLE"):
                    for key in (":PULM:PERiod", ":PULM:WIDTh"):
                        result[key] = self.session.query(key + "?")
                trigger = _enum_value(result[":PULM:TRIGger:MODE"])
                extra = (":PULM:DELay", ":PULM:TRIGger:EXTernal:SLOPe") if trigger == "EXTERNAL" else (
                    (":PULM:TRIGger:EXTernal:GATE:POLarity",) if trigger == "EGATE" else ())
                for key in extra:
                    result[key] = self.session.query(key + "?")
            return result

    def snapshot(self, *, include_modulation: bool = True, include_current_point: bool = False) -> dict[str, str]:
        """Read RF/sweep state; modulation reads can be explicitly requested."""
        with self.session.transaction():
            if not self.caps.known:
                return {"identity": self.session.query("*IDN?"), "state": "只读：未知型号"}
            result = {":SWEep:STATe": self.read_sweep_state()}
            keys = [":FREQuency", ":POWer", ":OUTPut",
                    ":SWEep:MODE", ":SWEep:SWEep:TRIGger:TYPE", ":SWEep:POINt:TRIGger:TYPE"]
            result.update({key: self.session.query(key + "?") for key in keys})
            if self.state == "enabled" and include_current_point:
                # C01A 3.4.5.29/30: independent numeric queries avoid the
                # combined DATA? query that fails on the user's SSG3021X.
                for key in (":SWEep:CURRent:FREQuency", ":SWEep:CURRent:LEVel"):
                    reply = self.session.query(key + "?")
                    try:
                        value = float(reply)
                        if not math.isfinite(value) or (key.endswith("FREQuency") and value <= 0):
                            raise ValueError("非有限数值或无效频率")
                    except ValueError as exc:
                        raise SsgCommandError(f"当前扫描值无法解析 [{key}?]：{reply!r}") from exc
                    result[key] = reply
            if include_modulation:
                # Retain the normalized result key for existing API consumers.
                result[":OUTPut:MODulation"] = self.session.query(self.modulation_header + "?")
                keys = [":AM:STATe", ":PULM:STATe", ":PULM:SOURce", ":PULM:TRIGger:MODE"]
                if self.caps.fm_pm:
                    keys += [":FM:STATe", ":PM:STATe"]
                if self.caps.vector:
                    keys += [":FUNCtion:DM:STATe", ":RADio:ARB:MDEStination:PULSe"]
                result.update({key: self.session.query(key + "?") for key in keys})
            return result

    def apply_modulation(self, c: SsgModulation) -> tuple[str, ...]:
        self._known()
        if (c.fm or c.pm) and not self.caps.fm_pm:
            raise ValueError("当前型号/硬件批次尚未确认支持 FM/PM")
        if c.fm and c.pm:
            raise ValueError("FM 和 PM 不能同时启用")
        if sum((c.am, c.fm, c.pm, c.pulse)) > 2:
            raise ValueError("基础版只开放已核对的双调制组合，多路组合需实机验证")
        for name in ("am", "fm", "pm"):
            _choice(getattr(c, name + "_source"), ("INTERNAL", "EXTERNAL", "INT,EXT"), name + " 源")
            wave = getattr(c, name + "_wave")
            _choice(wave, ("SINE", "SQUARE"), name + " 波形")
            _range(getattr(c, name + "_rate"), 0.01, 100000 if wave == "SINE" else 20000, name + " 调制频率 / Hz")
        _range(c.am_depth, 0, 100, "AM 深度 / %")
        # Common lower envelope of frequency-band-dependent deviation limits.
        _range(c.fm_deviation, 1, 31250, "FM 频偏 / Hz（基础版保守范围）")
        _range(c.pm_deviation, 0.01, 0.15, "PM 相偏 / rad（基础版保守范围）")
        if c.pulse:
            _choice(c.pulse_source, ("INTERNAL", "EXTERNAL"), "脉冲源")
            if c.pulse_source == "INTERNAL":
                _choice(c.pulse_trigger, ("AUTO", "KEY", "EXT", "EGATE", "BUS") if self.caps.pulse_bus else ("AUTO", "KEY", "EXT", "EGATE"), "脉冲触发")
                _range(c.pulse_period, 40e-9, 300, "脉冲周期 / s")
                _range(c.pulse_width, 20e-9, c.pulse_period - 20e-9, "脉冲宽度 / s")
                if c.pulse_trigger == "EXT":
                    _choice(c.pulse_slope, ("POSITIVE", "NEGATIVE"), "脉冲触发沿")
                    _range(c.pulse_delay, 140e-9, 300, "脉冲延时 / s（共同范围）")
                if c.pulse_trigger == "EGATE":
                    _choice(c.gate_polarity, ("NORMAL", "INVERTED"), "门控极性")
        warnings = []
        with self.session.transaction():
            self._idle()
            iq = self.caps.vector and _on(self.session.query(":FUNCtion:DM:STATe?"))
            if iq and sum((c.am, c.fm, c.pm, c.pulse)) > 1:
                raise ValueError("IQ 加两路模拟调制的三路组合尚未核对")
            if iq and c.pulse and self.session.query(":RADio:ARB:MDEStination:PULSe?").upper() != "NONE":
                raise ValueError("IQ 的 RF Blanking 已启用，不能同时开启脉冲调制")
            if c.am and (c.pulse or iq):
                warnings.append("AM 与脉冲/IQ 并用存在幅度性能限制，请按手册确认测量条件")
            if c.pulse and self.caps.model.startswith("SSG30") and float(self.session.query(":FREQuency?")) < 1e6:
                warnings.append("SSG3000X 在 1 MHz 以下可使用脉冲调制，但官方不保证该频段的脉冲性能指标")
            settings: list[tuple[str, float | str]] = []
            # Disable only the blocks the user explicitly deselected, then configure selected blocks.
            states = {"AM": c.am, "PULM": c.pulse}
            if self.caps.fm_pm:
                states.update(FM=c.fm, PM=c.pm)
            disable = [f":{name}:STATe OFF" for name, enabled in states.items() if not enabled]
            bus_pulse = c.pulse and c.pulse_source == "INTERNAL" and c.pulse_trigger == "BUS"
            if bus_pulse:
                # A firmware that ignores BUS must not leave a pulse running in AUTO.
                disable.append(":PULM:STATe OFF")
            self._batch(disable)
            if bus_pulse:
                self._verify(":PULM:STATe", 0)
            for name in ("AM", "FM", "PM"):
                if not getattr(c, name.lower()):
                    continue
                source = getattr(c, name.lower() + "_source")
                prefix = "FM1" if name == "FM" and self.caps.vector else name
                if prefix == "FM1":
                    source = {"INTERNAL": "INT1", "INT,EXT": "INT1,EXT", "EXTERNAL": "EXTERNAL"}[source]
                settings += [(f":{name}:SOURce", source),
                             (f":{prefix}:WAVEform", getattr(c, name.lower() + "_wave")),
                             (f":{prefix}:FREQuency", getattr(c, name.lower() + "_rate")),
                             (f":{prefix}:DEPTh" if name == "AM" else f":{prefix}:DEViation",
                              c.am_depth / 100 if name == "AM" else c.fm_deviation if name == "FM" else c.pm_deviation)]
            if c.pulse:
                if c.pulse_source == "INTERNAL":
                    trigger = "SINGLE" if c.pulse_trigger == "KEY" and self.caps.vector else c.pulse_trigger
                    # PERIOD? is defined only for single/double pulse, not PTRain.
                    self._batch([":PULM:SOURce INTERNAL", ":PULM:MODE SINGLE"])
                    self._verify(":PULM:SOURce", "INTERNAL")
                    self._verify(":PULM:MODE", "SINGLE")
                    # Shrink width before shortening period; grow period before increasing width.
                    current_period = float(self.session.query(":PULM:PERiod?"))
                    timing = [(":PULM:PERiod", c.pulse_period), (":PULM:WIDTh", c.pulse_width)]
                    if c.pulse_period < current_period:
                        timing.reverse()
                    settings += [*timing, (":PULM:TRIGger:MODE", trigger)]
                    if c.pulse_trigger == "EXT":
                        settings += [(":PULM:DELay", c.pulse_delay),
                                     (":PULM:TRIGger:EXTernal:SLOPe", c.pulse_slope)]
                    elif c.pulse_trigger == "EGATE":
                        settings.append((":PULM:TRIGger:EXTernal:GATE:POLarity", c.gate_polarity))
                else:
                    settings.append((":PULM:SOURce", c.pulse_source))
            self._batch([f"{key} {value}" for key, value in settings])
            for key, value in settings:
                self._verify(key, value)
            self._batch([f":{name}:STATe ON" for name, enabled in states.items() if enabled] +
                        [f"{self.modulation_header} {int(c.master)}"])
            for name, enabled in states.items():
                self._verify(f":{name}:STATe", int(enabled))
            self._verify(self.modulation_header, int(c.master))
        return tuple(warnings)

    def raw(self, command: str) -> str:
        command = command.strip()
        if not command or ";" in command or "\n" in command or "\r" in command:
            raise ValueError("诊断仅接受一条命令，不接受复合命令")
        if "?" in command.split(" ")[0]:
            return self.session.query(command)
        with self.session.transaction():
            self._idle()
            self.prepared = None
            self.state = "unknown"
            # A raw write can enable a scan; only a subsequent OFF query releases it.
            self.scan_may_be_active = True
            self.session.write(command, recover=False)
            return "已发送；扫描配置需重新应用，状态以回读为准"


def sweep_points(c: SsgSweep) -> tuple[SsgPoint, ...]:
    if c.kind == "LIST":
        return c.table
    return tuple(SsgPoint(
        c.start_hz * (c.stop_hz / c.start_hz) ** (i / (c.points - 1)) if c.spacing == "LOGARITHMIC"
        else c.start_hz + (c.stop_hz - c.start_hz) * i / (c.points - 1),
        c.start_dbm + (c.stop_dbm - c.start_dbm) * i / (c.points - 1), c.dwell_s)
        for i in range(c.points))


def _key(command: str) -> str:
    key = ":".join(part[:4] for part in command.upper().lstrip(":").split(":"))
    return "OUTP:MODU" if key in ("MODU", "MODU:STAT", "OUTP:MODU:STAT") else key


class VirtualSsgBackend:
    """Deterministic functional simulator, not an electrical/timing specification.

    The sweep gate consumes a separate event from the point gate. Trigger pulses
    received during dwell are ignored here; real hardware behavior needs validation.
    """
    def __init__(self, identity="SIGLENT,SSG6082A-V,SIMSSG001,1.0", clock=time.monotonic):
        self.identity = identity
        self.clock = clock
        self.commands: list[str] = []
        self.values: dict[str, str] = {}
        defaults = {
            "FREQuency": "1000000000", "POWer": "-20", "OUTPut": "0",
            "OUTPut:MODulation": "0", "SWEep:STATe": "OFF", "SWEep:TYPE": "STEP",
            "SWEep:MODE": "CONTINUE", "SWEep:DIRect": "FWD",
            "SWEep:SWEep:TRIGger:TYPE": "AUTO", "SWEep:POINt:TRIGger:TYPE": "EXT",
            "INPut:TRIGger:SLOPe": "POSITIVE", "SWEep:STEP:STARt:FREQuency": "1000000000",
            "SWEep:STEP:STOP:FREQuency": "1100000000", "SWEep:STEP:STARt:LEVel": "-20",
            "SWEep:STEP:STOP:LEVel": "-20", "SWEep:STEP:DWELl": "0.03",
            "SWEep:STEP:POINts": "11", "SWEep:STEP:SHAPe": "SAWTOOTH",
            "SWEep:STEP:SPACe": "LINEAR", "POWer:SPC:STATe": "0",
            "PULM:OUT:STATe": "0", "PULM:TRIGger:STATe": "0",
            "FUNCtion:DM:STATe": "0", "RADio:ARB:MDEStination:PULSe": "NONE",
            "PULM:SOURce": "INTERNAL", "PULM:MODE": "SINGLE", "PULM:TRIGger:MODE": "AUTO",
            "PULM:PERiod": "0.001", "PULM:WIDTh": "0.0005", "PULM:DELay": "0.000001",
            "PULM:TRIGger:EXTernal:SLOPe": "POSITIVE", "PULM:TRIGger:EXTernal:GATE:POLarity": "NORMAL",
        }
        for name in ("AM", "FM", "PM", "PULM"):
            defaults[name + ":STATe"] = "0"
        for name in ("AM", "FM", "FM1", "PM"):
            defaults.update({name + ":SOURce": "INTERNAL", name + ":WAVEform": "SINE",
                             name + ":FREQuency": "1000", name + ":DEViation": "0.1" if name == "PM" else "1000"})
        defaults["AM:DEPTh"] = "0.3"
        self.values.update({_key(k): v for k, v in defaults.items()})
        self.table = [SsgPoint(1e9, -20)]
        self.phase = "off"
        self.index = -1
        self.path: tuple[SsgPoint, ...] = ()
        self.deadline = 0.0
        self.esr = 0
        self.pulse_triggers = 0

    def get(self, key: str) -> str:
        return self.values[_key(key)]

    def put(self, key: str, value: str):
        self.values[_key(key)] = value

    def close(self):
        pass  # Closing a transport does not stop a physical instrument.

    def write(self, command: str):
        self.commands.append(command)
        self._tick()
        command = command.strip()
        if command.upper() == "*TRG":
            self.trigger("BUS")
            return
        if command.upper() == "*CLS":
            self.esr = 0
            return
        head, _, value = command.partition(" ")
        key, value = _key(head), value.strip().upper()
        if key == "SIM:TRIG":
            self.trigger(value)
        elif key == _key("SWEep:EXECute"):
            if self.get("SWEep:STATe") != "OFF" and self.get("SWEep:MODE").startswith("SING"):
                self._begin()
        elif key == _key("SWEep:LIST:INITialize:PRESet"):
            self.table = [SsgPoint(1e9, -20)]
        elif key == _key("SWEep:LIST:ADDList"):
            self.table.append(SsgPoint(*map(float, value.split(","))))
        elif key == _key("SWEep:LIST:CHANGe"):
            row, *fields = value.split(",")
            self.table[int(row) - 1] = SsgPoint(*map(float, fields))
        elif key in self.values:
            if value in ("ON", "OFF") and (key.endswith("STAT") or key in ("OUTP", "OUTP:MODU")) and key != "SWEE:STAT":
                value = "1" if value == "ON" else "0"
            self.values[key] = value
            if key == "SWEE:STAT":
                if value == "OFF":
                    self.phase = "off"
                else:
                    self._load_path()
                    self.index = -1
                    if self.get("SWEep:MODE").startswith("SING"):
                        self.phase = "single_armed"
                    else:
                        self._begin()
            if key in ("SWEE:SWEE:TRIG:TYPE", "SWEE:POIN:TRIG:TYPE") and value == "EXT":
                self.put("PULM:TRIGger:STATe", "0")
            if key == "PULM:SOUR" and value.startswith("EXT"):
                self.put("PULM:OUT:STATe", "0")
        else:
            self.esr |= 32

    def query(self, command: str) -> str:
        self.commands.append(command)
        self._tick()
        if command.upper() == "*IDN?":
            return self.identity
        if command.upper() == "*ESR?":
            result, self.esr = self.esr, 0
            return str(result)
        if command.upper() == "*OPC?":
            return "1"
        head, _, args = command.partition(" ")
        key = _key(head.rstrip("?"))
        if key == "SIM:PHAS":
            return self.phase
        if key == _key("SWEep:LIST:CPOint"):
            return str(len(self.table))
        if key == _key("SWEep:LIST:LIST"):
            start, end = map(int, args.split(","))
            return " ".join(f"{p.frequency_hz},{p.power_dbm},{p.dwell_s}" for p in self.table[start-1:end])
        if key == _key("SWEep:CURRent:DATA"):
            dwell = self.path[self.index].dwell_s if self.path and self.index >= 0 else 0
            return f"{self.index + 1},{{{self.get('FREQuency')},{self.get('POWer')},{dwell}}}"
        if key == _key("SWEep:CURRent:FREQuency"):
            return self.get("FREQuency")
        if key == _key("SWEep:CURRent:LEVel"):
            return self.get("POWer")
        if key in self.values:
            return self.values[key]
        raise ValueError(f"虚拟仪器不支持查询 {command}")

    def _load_path(self):
        c = SsgSweep(kind=self.get("SWEep:TYPE"),
                     start_hz=float(self.get("SWEep:STEP:STARt:FREQuency")),
                     stop_hz=float(self.get("SWEep:STEP:STOP:FREQuency")),
                     start_dbm=float(self.get("SWEep:STEP:STARt:LEVel")),
                     stop_dbm=float(self.get("SWEep:STEP:STOP:LEVel")),
                     dwell_s=float(self.get("SWEep:STEP:DWELl")),
                     points=int(self.get("SWEep:STEP:POINts")), spacing=self.get("SWEep:STEP:SPACe"),
                     table=tuple(self.table))
        self.path = sweep_points(c)
        if self.get("SWEep:DIRect") == "REV":
            self.path = self.path[::-1]
        if c.kind == "STEP" and self.get("SWEep:STEP:SHAPe").startswith("TRI"):
            self.path += self.path[-2:0:-1]

    def _begin(self):
        self.index = -1
        self.phase = "sweep_wait"
        if self.get("SWEep:SWEep:TRIGger:TYPE") == "AUTO":
            self._point_wait()

    def _point_wait(self):
        self.phase = "point_wait"
        if self.get("SWEep:POINt:TRIGger:TYPE") == "AUTO":
            self._advance()

    def _advance(self):
        self.index += 1
        point = self.path[self.index]
        target = self.get("SWEep:STATe")
        if target != "LEVEL":
            self.put("FREQuency", str(point.frequency_hz))
        if target != "FREQUENCY":
            self.put("POWer", str(point.power_dbm))
        self.phase = "dwell"
        self.deadline = self.clock() + point.dwell_s

    def _tick(self):
        # Deliberately bounded: this is a UI simulation, not a real-time scheduler.
        if self.phase == "dwell" and self.clock() >= self.deadline:
            if self.index + 1 < len(self.path):
                self._point_wait()
            elif self.get("SWEep:MODE").startswith("SING"):
                self.phase = "single_complete"
            else:
                self._begin()

    def trigger(self, source: str):
        self._tick()
        if self.get("PULM:STATe") == "1" and self.get("PULM:TRIGger:MODE") == source:
            self.pulse_triggers += 1
        if self.phase == "sweep_wait" and self.get("SWEep:SWEep:TRIGger:TYPE") == source:
            self._point_wait()
        elif self.phase == "point_wait" and self.get("SWEep:POINt:TRIGger:TYPE") == source:
            self._advance()
