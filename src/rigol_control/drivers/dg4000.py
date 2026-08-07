from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from .scpi import ScpiSession


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
    return format(number, ".12g")


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

    @property
    def is_connected(self) -> bool:
        return self.session.is_connected

    def connect(self) -> str:
        return self.session.connect()

    def disconnect(self) -> None:
        self.session.disconnect()

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
        self.session.write(command, recover=True)

    def _write_configuration_many(self, commands: list[str]) -> None:
        self.write_settings(commands)

    def identify(self) -> str:
        return self.query("*IDN?")

    def error(self) -> str:
        return self.query(":SYST:ERR?")

    def set_output(self, channel: int, enabled: bool) -> None:
        validate_channel(channel)
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
        return [
            f":SOUR{channel}:FUNC {function}",
            f":SOUR{channel}:FREQ {finite_number(waveform.frequency_hz)}",
            f":SOUR{channel}:VOLT {finite_number(waveform.amplitude_vpp)}",
            f":SOUR{channel}:VOLT:OFFS {finite_number(waveform.offset_v)}",
            f":SOUR{channel}:PHAS {finite_number(waveform.phase_deg)}",
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
                commands.append(
                    f":SOUR{channel}:MOD:{mode}:{parameter_word} {finite_number(parameter)}"
                )
        commands.append(f":SOUR{channel}:MOD {'ON' if enabled else 'OFF'}")
        self._write_configuration_many(commands)

    def apply_step(self, parameter: StepParameter, channel: int, value: float) -> None:
        self._write_configuration(
            f"{parameter.command(channel)} {finite_number(value)}"
        )

    def apply_steps(self, values: list[StepValue] | tuple[StepValue, ...]) -> None:
        if not values:
            raise ValueError("联合扫描点至少需要一个参数")
        self._write_configuration_many(
            [
                f"{item.parameter.command(item.channel)} {finite_number(item.value)}"
                for item in values
            ]
        )
