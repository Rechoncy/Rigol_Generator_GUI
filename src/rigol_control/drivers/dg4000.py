from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum

from .scpi import ScpiSession
from ..units import roundtrip_number


DG4000_MODEL_MAX_FREQUENCY_HZ: dict[str, float] = {
    "DG4062": 60_000_000.0,
    "DG4102": 100_000_000.0,
    "DG4162": 160_000_000.0,
    "DG4202": 200_000_000.0,
}
DG4000_FAMILY_MAX_FREQUENCY_HZ = max(DG4000_MODEL_MAX_FREQUENCY_HZ.values())
_DG4000_MODEL_PATTERN = re.compile(r"(?<![A-Z0-9])(DG\d{4})(?![A-Z0-9])", re.IGNORECASE)
DG4000_WAVEFORM_MAX_FREQUENCY_HZ = {
    "DG4062": {"SIN": 60e6, "SQU": 25e6, "RAMP": 1e6, "PULS": 15e6, "USER": 15e6, "HARM": 30e6},
    "DG4102": {"SIN": 100e6, "SQU": 40e6, "RAMP": 3e6, "PULS": 25e6, "USER": 25e6, "HARM": 50e6},
    "DG4162": {"SIN": 160e6, "SQU": 50e6, "RAMP": 4e6, "PULS": 40e6, "USER": 40e6, "HARM": 80e6},
    "DG4202": {"SIN": 200e6, "SQU": 60e6, "RAMP": 5e6, "PULS": 50e6, "USER": 50e6, "HARM": 100e6},
}
_BUILTIN_ARBITRARY_WAVEFORMS = frozenset((
    "DC ABSSINE ABSSINEHALF AMPALT ATTALT GAUSSPULSE NEGRAMP NPULSE PPULSE SINETRA SINEVER "
    "STAIRDN STAIRUD STAIRUP TRAPEZIA BANDLIMITED BUTTERWORTH CHEBYSHEV1 CHEBYSHEV2 COMBIN "
    "CPULSE CWPULSE DAMPEDOSC DUALTONE GAMMA GATEVIBR LFMPULSE MCNOSIE NIMHDISCHARGE PAHCUR "
    "QUAKE RADAR RIPPLE ROUNDHALF ROUNDPM STEPRESP SWINGOSC TV VOICE THREEAM THREEFM THREEPM "
    "THREEPWM THREEPFM CARDIAC EOG EEG EMG PULSILOGRAM RESSPEED LFPULSE TENS1 TENS2 TENS3 "
    "IGNITION ISO167502SP ISO167502VR ISO76372TP1 ISO76372TP2A ISO76372TP2B ISO76372TP3A "
    "ISO76372TP3B ISO76372TP4 ISO76372TP5A ISO76372TP5B SCR SURGE AIRY BESSELJ BESSELY CAUCHY "
    "CUBIC DIRICHLET ERF ERFC ERFCINV ERFINV EXPFALL EXPRISE GAUSS HAVERSINE LAGUERRE LAPLACE "
    "LEGEND LOG LOGNORMAL LORENTZ MAXWELL RAYLEIGH VERSIERA WEIBULL X2DATA COSH COSINT COT "
    "COTHCON COTHPRO CSCCON CSCPRO CSCHCON CSCHPRO RECIPCON RECIPPRO SECCON SECPRO SECH SINC "
    "SINH SININT SQRT TAN TANH ACOS ACOSH ACOTCON ACOTPRO ACOTHCON ACOTHPRO ACSCCON ACSCPRO "
    "ACSCHCON ACSCHPRO ASECCON ASECPRO ASECH ASIN ASINH ATAN ATANH BARLETT BARTHANN BLACKMAN "
    "BLACKMANH BOHMANWIN BOXCAR CHEBWIN FLATTOPWIN HAMMING HANNING KAISER NUTTALLWIN PARZENWIN "
    "TAYLORWIN TRIANG TUKEYWIN"
).split())


@dataclass(frozen=True, slots=True)
class DG4000Capabilities:
    """Frequency capability inferred from the model token in ``*IDN?``."""

    model: str | None
    maximum_frequency_hz: float
    model_recognized: bool


def dg4000_capabilities(identity: str) -> DG4000Capabilities:
    """Return a safe UI capability profile for one DG4000 identity.

    Before a connection exists (or for an unknown future model), the editor
    uses the widest range of the documented DG4000 family. Once ``*IDN?``
    identifies a known model, all typed controls and commands use that model's
    actual sine-wave frequency ceiling.
    """

    match = _DG4000_MODEL_PATTERN.search(str(identity or ""))
    model = match.group(1).upper() if match else None
    maximum = DG4000_MODEL_MAX_FREQUENCY_HZ.get(
        model or "",
        DG4000_FAMILY_MAX_FREQUENCY_HZ,
    )
    return DG4000Capabilities(
        model=model,
        maximum_frequency_hz=maximum,
        model_recognized=model in DG4000_MODEL_MAX_FREQUENCY_HZ,
    )


def validate_dg4000_frequency(
    value_hz: float,
    capabilities: DG4000Capabilities,
    *,
    parameter_name: str = "频率",
    waveform: str | None = None,
) -> float:
    """Validate an absolute output/FSK frequency against a model profile."""

    value = float(value_hz)
    if not math.isfinite(value):
        raise ValueError(f"{parameter_name}必须是有限数值")
    if value < 1e-6:
        raise ValueError(f"{parameter_name}必须不小于 1 μHz")
    maximum = DG4000_WAVEFORM_MAX_FREQUENCY_HZ.get(capabilities.model, {}).get(
        waveform, capabilities.maximum_frequency_hz
    )
    if value > maximum:
        model = capabilities.model or "DG4000 系列"
        maximum_mhz = maximum / 1_000_000.0
        raise ValueError(
            f"{parameter_name} {value / 1_000_000.0:g} MHz 超过 "
            f"{model} {waveform or ''} 的 {maximum_mhz:g} MHz 上限"
        )
    return value


class StepParameter(str, Enum):
    FREQUENCY = "frequency"
    AMPLITUDE = "amplitude"
    PHASE = "phase"
    PSK_PHASE = "psk_phase"
    FSK_FREQUENCY = "fsk_frequency"

    @property
    def label(self) -> str:
        return {
            StepParameter.FREQUENCY: "输出频率",
            StepParameter.AMPLITUDE: "输出幅度",
            StepParameter.PHASE: "输出相位",
            StepParameter.PSK_PHASE: "PSK 调制相位",
            StepParameter.FSK_FREQUENCY: "FSK 跳跃频率",
        }[self]

    @property
    def unit(self) -> str:
        return {
            StepParameter.FREQUENCY: "Hz",
            StepParameter.AMPLITUDE: "Vpp",
            StepParameter.PHASE: "°",
            StepParameter.PSK_PHASE: "°",
            StepParameter.FSK_FREQUENCY: "Hz",
        }[self]

    @property
    def unit_family(self) -> str:
        return {
            StepParameter.FREQUENCY: "frequency",
            StepParameter.AMPLITUDE: "amplitude",
            StepParameter.PHASE: "angle",
            StepParameter.PSK_PHASE: "angle",
            StepParameter.FSK_FREQUENCY: "frequency",
        }[self]

    @property
    def short_label(self) -> str:
        return {
            StepParameter.FREQUENCY: "频率",
            StepParameter.AMPLITUDE: "幅度",
            StepParameter.PHASE: "相位",
            StepParameter.PSK_PHASE: "PSK 相位",
            StepParameter.FSK_FREQUENCY: "FSK 频率",
        }[self]

    def command(self, channel: int) -> str:
        validate_channel(channel)
        return {
            StepParameter.FREQUENCY: f":SOUR{channel}:FREQ",
            StepParameter.AMPLITUDE: f":SOUR{channel}:VOLT",
            StepParameter.PHASE: f":SOUR{channel}:PHAS",
            StepParameter.PSK_PHASE: f":SOUR{channel}:MOD:PSK:PHAS",
            StepParameter.FSK_FREQUENCY: f":SOUR{channel}:MOD:FSK:FREQ",
        }[self]


@dataclass(frozen=True, slots=True)
class StepValue:
    parameter: StepParameter
    channel: int
    value: float
    display_value: float | None = None
    unit_key: str | None = None


@dataclass(frozen=True, slots=True)
class BasicWaveform:
    function: str
    frequency_hz: float
    amplitude_vpp: float
    offset_v: float = 0.0
    phase_deg: float = 0.0


def validate_channel(channel: int) -> None:
    if channel not in (1, 2):
        raise ValueError("DG4000 通道只能是 1 或 2")


def finite_number(value: float) -> str:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("下发参数必须是有限数值")
    return roundtrip_number(number)


def phase_number(value: float) -> str:
    result = finite_number(value)
    if not 0 <= float(value) <= 360:
        raise ValueError("DG4000 相位必须在 0 至 360° 之间")
    return result


def _identity_key(identity: str):
    parts = [part.strip() for part in str(identity).split(",")]
    return (parts[1].upper(), parts[2]) if len(parts) >= 3 and parts[1] and parts[2] else None


def output_load_value(load_ohms: int | None) -> str:
    """Format the DG4000 LOAD/IMPEDANCE argument documented as 1..10000 ohm."""

    if load_ohms is None:
        return "INFINITY"
    if isinstance(load_ohms, bool) or not isinstance(load_ohms, int):
        raise ValueError("输出负载阻抗必须是 1 至 10000 Ω 的整数或高阻")
    if not 1 <= load_ohms <= 10_000:
        raise ValueError("输出负载阻抗必须在 1 至 10000 Ω 之间")
    return str(load_ohms)


def parse_output_state(value: str) -> bool:
    """Parse the common numeric/text responses returned by ``:OUTP<n>?``."""

    normalized = value.strip().strip('"').upper()
    if normalized in {"1", "ON", "TRUE"}:
        return True
    if normalized in {"0", "OFF", "FALSE"}:
        return False
    raise ValueError(f"无法识别 DG4000 输出状态：{value!r}")


class DG4000Driver:
    """DG4000 convenience API plus unrestricted raw SCPI access.

    Typed methods cover the controls used by the application. ``write`` and
    ``query`` deliberately remain public so every programming-manual command,
    including model/firmware-specific features, stays reachable.
    """

    FUNCTIONS = {
        "正弦波": "SIN",
        "方波": "SQU",
        "锯齿波": "RAMP",
        "脉冲": "PULS",
        "噪声": "NOIS",
        "任意波": "USER",
        "SIN": "SIN",
        "SQU": "SQU",
        "RAMP": "RAMP",
        "PULS": "PULS",
        "NOIS": "NOIS",
        "USER": "USER",
    }

    def __init__(self, session: ScpiSession) -> None:
        self.session = session
        self._identity = ""

    @property
    def is_connected(self) -> bool:
        return self.session.is_connected

    @property
    def identity(self) -> str:
        return str(getattr(self.session, "identity", "") or self._identity)

    @property
    def capabilities(self) -> DG4000Capabilities:
        return dg4000_capabilities(self.identity)

    def set_identity(self, identity: str) -> None:
        """Remember identity supplied by the shared connection manager."""

        self._identity = str(identity or "").strip()

    def connect(self) -> str:
        identity = self.session.connect()
        self.set_identity(identity)
        return identity

    def disconnect(self) -> None:
        self.session.disconnect()
        self._identity = ""

    def write(self, command: str) -> None:
        self.session.write(command)

    def write_many(self, commands: list[str] | tuple[str, ...]) -> None:
        self.session.write_many(commands)

    def write_settings(self, commands: list[str] | tuple[str, ...]) -> None:
        """Write idempotent settings with one automatic VISA recovery attempt."""
        self.session.write_many(commands, recover=True)

    def query(self, command: str) -> str:
        return self.session.query(command, recover=self.session.is_connected)

    def _write_configuration(self, command: str) -> None:
        """Retry an idempotent setting once after a transient VISA failure."""
        self._write_configuration_many([command])

    def _write_configuration_many(self, commands: list[str]) -> None:
        with self.session.transaction():
            expected_identity = self._identity or self.identity
            self._check_recovered_identity(expected_identity)
            self.ensure_independent_channels()
            def after_recover():
                self._check_recovered_identity(expected_identity)
                self.ensure_independent_channels()
            self.session.write_many(commands, recover=True, after_recover=after_recover)

    def _check_recovered_identity(self, expected: str) -> None:
        key = _identity_key(expected)
        if key is not None and _identity_key(self.identity) != key:
            raise ValueError("DG4000 设备型号或序列号已变化，未继续下发；请断开并重新连接，核对仪器身份")

    def ensure_independent_channels(self) -> None:
        """Read the DG4000's three global coupling switches without altering them."""
        reply = self.session.query(":COUP:STAT?").strip().upper()
        if reply in {"0", "OFF"}:
            return
        if reply in {"1", "ON"}:
            enabled = ["全局耦合"]
        else:
            states = {}
            try:
                for entry in reply.split(","):
                    name, value = entry.split(":")
                    name, value = name.strip()[:4], value.strip()
                    if name not in {"FREQ", "PHAS", "AMPL"} or name in states:
                        raise ValueError()
                    states[name] = parse_output_state(value)
                if set(states) != {"FREQ", "PHAS", "AMPL"}:
                    raise ValueError()
            except ValueError as exc:
                raise ValueError(f"无法确认双通道耦合状态 [:COUP:STAT?]：{reply!r}；未下发参数") from exc
            labels = {"FREQ": "频率", "PHAS": "相位", "AMPL": "幅度"}
            enabled = [labels[key] for key, state in states.items() if state]
        if enabled:
            raise ValueError("通道耦合已启用（" + "、".join(enabled) +
                             "）；修改 CH2 可能带动 CH1。请先在仪器上关闭耦合并核对两通道，再操作或启动步进。")

    def write_channel_settings(self, channel: int, commands: list[str]) -> None:
        validate_channel(channel)
        for command in commands:
            header = command.strip().upper().split(" ", 1)[0]
            if not header.startswith((f":SOUR{channel}:", f":OUTP{channel}:")) and header != f":OUTP{channel}":
                raise ValueError(f"CH{channel} 设置包含其他通道或全局命令：{command}")
            if ";" in command or "?" in command or "\n" in command or "\r" in command:
                raise ValueError("通道设置只接受独立的设置命令")
        self._write_configuration_many(commands)

    def prepare_trigger(self):
        # Called before the NI counter starts, once per task/run. Do not change
        # coupling globally, query it per edge, or enable either output here.
        with self.session.transaction():
            self._check_recovered_identity(self._identity or self.identity)
            self.ensure_independent_channels()
            waveforms = self._read_waveforms((1, 2))
            return _DG4000TriggerWriter(self, self.session.connection_generation, waveforms)

    def _read_waveforms(self, channels) -> dict[int, str]:
        result = {}
        for channel in channels:
            raw = self.session.query(f":SOUR{channel}:FUNC?").strip().strip('"').upper()
            function = {"PULSE": "PULS", "NOISE": "NOIS", "HARMONIC": "HARM",
                        "CUSTOM": "USER", "SQUARE": "SQU", "SINUSOID": "SIN"}.get(raw)
            if function is None:
                function = "USER" if raw in _BUILTIN_ARBITRARY_WAVEFORMS else self.FUNCTIONS.get(raw)
            if function is None:
                raise ValueError(f"无法确认 CH{channel} 当前波形：{raw!r}；未下发参数")
            result[channel] = function
        return result

    def identify(self) -> str:
        identity = self.query("*IDN?")
        self.set_identity(identity)
        return identity

    def error(self) -> str:
        return self.query(":SYST:ERR?")

    def set_output(self, channel: int, enabled: bool) -> None:
        validate_channel(channel)
        if not enabled:
            # Coupling affects frequency/phase/amplitude, not OUTP<n>. Keep an
            # explicit output-off usable even when coupling cannot be read.
            expected = self._identity or self.identity
            self._check_recovered_identity(expected)
            self.session.write(f":OUTP{channel} OFF", recover=True,
                               after_recover=lambda: self._check_recovered_identity(expected))
            return
        self._write_configuration(f":OUTP{channel} {'ON' if enabled else 'OFF'}")

    def output_enabled(self, channel: int) -> bool:
        validate_channel(channel)
        return parse_output_state(self.query(f":OUTP{channel}?"))

    def output_states(self) -> tuple[bool, bool]:
        return (self.output_enabled(1), self.output_enabled(2))

    def set_output_load(self, channel: int, load_ohms: int | None) -> None:
        validate_channel(channel)
        self._write_configuration(
            f":OUTP{channel}:LOAD {output_load_value(load_ohms)}"
        )

    def configure_basic(self, channel: int, waveform: BasicWaveform) -> None:
        self._write_configuration_many(self._basic_commands(channel, waveform))

    def configure_waveform_output(
        self,
        channel: int,
        waveform: BasicWaveform,
        *,
        enabled: bool,
    ) -> None:
        """Apply waveform and output state without changing the configured load."""

        self._write_configuration_many(
            [
                *self._basic_commands(channel, waveform),
                f":OUTP{channel} {'ON' if enabled else 'OFF'}",
            ]
        )

    def _basic_commands(self, channel: int, waveform: BasicWaveform) -> list[str]:
        validate_channel(channel)
        function = self.FUNCTIONS.get(waveform.function.upper(), self.FUNCTIONS.get(waveform.function))
        if function is None:
            raise ValueError(f"不支持的基本波形：{waveform.function}")
        validate_dg4000_frequency(
            waveform.frequency_hz,
            self.capabilities,
            parameter_name=f"CH{channel} 输出频率",
            waveform=function,
        )
        return [
            f":SOUR{channel}:FUNC {function}",
            f":SOUR{channel}:FREQ {finite_number(waveform.frequency_hz)}",
            f":SOUR{channel}:VOLT {finite_number(waveform.amplitude_vpp)}VPP",
            f":SOUR{channel}:VOLT:OFFS {finite_number(waveform.offset_v)}",
            f":SOUR{channel}:PHAS {phase_number(waveform.phase_deg)}",
        ]

    def configure_channel(
        self,
        channel: int,
        waveform: BasicWaveform,
        *,
        load_ohms: int | None,
        enabled: bool,
    ) -> None:
        """Apply load, waveform and output state as one serialized SCPI batch."""

        self._write_configuration_many(
            [
                f":OUTP{channel}:LOAD {output_load_value(load_ohms)}",
                *self._basic_commands(channel, waveform),
                f":OUTP{channel} {'ON' if enabled else 'OFF'}",
            ]
        )

    def configure_modulation(
        self,
        channel: int,
        mode: str,
        *,
        source: str = "INT",
        internal_function: str | None = None,
        internal_frequency_hz: float | None = None,
        parameter: float | None = None,
        enabled: bool = True,
    ) -> None:
        validate_channel(channel)
        mode = mode.strip().upper()
        if mode not in {"AM", "FM", "PM", "ASK", "FSK", "PSK", "BPSK", "QPSK", "3FSK", "4FSK", "OSK", "PWM"}:
            raise ValueError(f"不支持的调制类型：{mode}")
        commands = [f":SOUR{channel}:MOD:TYPE {mode}"]
        if source:
            commands.append(f":SOUR{channel}:MOD:{mode}:SOUR {source.strip().upper()}")
        if internal_frequency_hz is not None:
            rate_word = "RATE" if mode in {"ASK", "FSK", "PSK", "BPSK", "QPSK", "3FSK", "4FSK", "OSK"} else "FREQ"
            commands.append(
                f":SOUR{channel}:MOD:{mode}:INT:{rate_word} {finite_number(internal_frequency_hz)}"
            )
        if internal_function is not None and mode in {"AM", "FM", "PM", "PWM"}:
            functions = {
                "正弦": "SIN",
                "方波": "SQU",
                "三角波": "TRI",
                "上锯齿": "RAMP",
                "下锯齿": "NRAMP",
                "噪声": "NOIS",
            }
            function = functions.get(internal_function, internal_function.strip().upper())
            commands.append(f":SOUR{channel}:MOD:{mode}:INT:FUNC {function}")
        if parameter is not None:
            parameter_word = {
                "AM": "DEPTH",
                "FM": "DEV",
                "PM": "DEV",
                "ASK": "AMPL",
                "FSK": "FREQ",
                "PSK": "PHAS",
                "BPSK": "PHAS",
                "PWM": "DEV",
                "OSK": "TIME",
            }.get(mode)
            if parameter_word:
                if mode == "FSK":
                    validate_dg4000_frequency(
                        parameter,
                        self.capabilities,
                        parameter_name=f"CH{channel} FSK 跳跃频率",
                    )
                commands.append(
                    f":SOUR{channel}:MOD:{mode}:{parameter_word} "
                    f"{phase_number(parameter) if mode in {'PSK', 'BPSK'} else finite_number(parameter)}"
                    f"{'VPP' if mode == 'ASK' else ''}"
                )
        commands.append(f":SOUR{channel}:MOD {'ON' if enabled else 'OFF'}")
        self._write_configuration_many(commands)

    def apply_step(self, parameter: StepParameter, channel: int, value: float) -> None:
        self.apply_steps((StepValue(parameter, channel, value),))

    def apply_steps(self, values: list[StepValue] | tuple[StepValue, ...]) -> None:
        if not values:
            raise ValueError("联合扫描点至少需要一个参数")
        with self.session.transaction():
            self._check_recovered_identity(self._identity or self.identity)
            self.ensure_independent_channels()
            channels = sorted({item.channel for item in values if item.parameter is StepParameter.FREQUENCY})
            self._apply_trigger_steps(values, self._read_waveforms(channels))

    def _apply_trigger_steps(self, values, waveforms=None) -> None:
        if not values:
            raise ValueError("联合扫描点至少需要一个参数")
        self.validate_step_values(values)
        for item in values:
            if item.parameter is StepParameter.FREQUENCY and waveforms is not None:
                validate_dg4000_frequency(item.value, self.capabilities,
                                         parameter_name=f"CH{item.channel} 输出频率",
                                         waveform=waveforms.get(item.channel))
        # Every header is absolute: a semicolon must never inherit SOUR1/2
        # context from the preceding command. Only idempotent point values are
        # batched; raw writes, triggers and output switches use their old API.
        message = ";".join(f"{item.parameter.command(item.channel)} {finite_number(item.value)}"
                           f"{'VPP' if item.parameter is StepParameter.AMPLITUDE else ''}" for item in values)
        # A transport failure leaves acceptance unknown. Stop the run rather
        # than reconnect/replay a point with an unverified coupling state.
        self.session.write(message, recover=False)

    def validate_step_value(self, item: StepValue) -> None:
        validate_channel(item.channel)
        if item.parameter in {
            StepParameter.FREQUENCY,
            StepParameter.FSK_FREQUENCY,
        }:
            validate_dg4000_frequency(
                item.value,
                self.capabilities,
                parameter_name=f"CH{item.channel} {item.parameter.short_label}",
            )
        elif item.parameter in {StepParameter.PHASE, StepParameter.PSK_PHASE}:
            phase_number(item.value)
        else:
            finite_number(item.value)

    def validate_step_values(
        self,
        values: list[StepValue] | tuple[StepValue, ...],
    ) -> None:
        for item in values:
            self.validate_step_value(item)


class _DG4000TriggerWriter:
    """A task-local writer whose coupling preflight finished before NI start."""

    def __init__(self, driver: DG4000Driver, connection_generation: int, waveforms: dict[int, str]) -> None:
        self.driver = driver
        self.connection_generation = connection_generation
        self.waveforms = dict(waveforms)

    def apply_steps(self, values) -> None:
        with self.driver.session.transaction():
            if self.driver.session.connection_generation != self.connection_generation:
                raise RuntimeError("DG4000 连接已经断开或重建，原触发任务的通道检查已失效；请停止并重新布防。")
            self.driver._apply_trigger_steps(values, self.waveforms)
