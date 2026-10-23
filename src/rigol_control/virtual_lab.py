from __future__ import annotations

import re
import math
import random
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event, Lock, RLock

from .drivers.ni_analog import AnalogInputBlock, AnalogInputConfig


VIRTUAL_DG_RESOURCE = "SIM::DG4000::1"
VIRTUAL_DG2_RESOURCE = "SIM::DG4000::2"
VIRTUAL_NI_RESOURCE = "SIM::NI_DAQ::1"


def is_virtual_resource(resource: str) -> bool:
    return resource.strip().upper().startswith("SIM::")


@dataclass(frozen=True, slots=True)
class VirtualChannelSnapshot:
    channel: int
    function: str
    frequency_hz: float
    amplitude_vpp: float
    offset_v: float
    phase_deg: float
    output: bool
    modulation_enabled: bool
    modulation_type: str
    psk_phase_deg: float
    fsk_frequency_hz: float
    load_ohms: int | None


@dataclass(frozen=True, slots=True)
class VirtualGeneratorSnapshot:
    resource: str
    connected: bool
    scpi_delay_ms: int
    channels: tuple[VirtualChannelSnapshot, ...]
    command_count: int
    recent_commands: tuple[str, ...]


class VirtualGenerator:
    def __init__(self, resource: str) -> None:
        self.resource = resource
        self.connected = False
        self.scpi_delay_ms = 5
        self.channels = {1: self._default_channel(), 2: self._default_channel()}
        self.amplitude_units = {1: "VPP", 2: "VPP"}
        self.values: dict[str, str] = {}
        self.commands: deque[str] = deque(maxlen=500)
        self.lock = RLock()

    @staticmethod
    def _default_channel() -> dict[str, object]:
        return {
            "function": "SIN",
            "frequency_hz": 1000.0,
            "amplitude_vpp": 1.0,
            "offset_v": 0.0,
            "phase_deg": 0.0,
            "output": False,
            "modulation_enabled": False,
            "modulation_type": "AM",
            "psk_phase_deg": 180.0,
            "fsk_frequency_hz": 2000.0,
            "load_ohms": 50,
        }

    def reset(self) -> None:
        with self.lock:
            self.channels = {1: self._default_channel(), 2: self._default_channel()}
            self.amplitude_units = {1: "VPP", 2: "VPP"}
            self.values.clear()
            self.commands.append("*RST")

    def write(self, command: str) -> None:
        delay = self.scpi_delay_ms
        if delay:
            time.sleep(delay / 1000.0)
        normalized = command.strip()
        with self.lock:
            self.commands.append(normalized)
            upper = normalized.upper()
            if upper == "*RST":
                self.channels = {1: self._default_channel(), 2: self._default_channel()}
                self.amplitude_units = {1: "VPP", 2: "VPP"}
                self.values.clear()
                return
            if upper in {"*CLS", "*WAI"}:
                return
            for part in normalized.split(";"):
                command_path, _, raw_value = part.partition(" ")
                path = command_path.strip().upper().lstrip(":")
                value = raw_value.strip()
                self.values[path] = value
                self._apply_known(path, value)

    def query(self, command: str) -> str:
        normalized = command.strip()
        delay = self.scpi_delay_ms
        if delay:
            time.sleep(delay / 1000.0)
        with self.lock:
            self.commands.append(normalized)
            upper = normalized.upper()
            if upper == "*IDN?":
                return "RIGOL TECHNOLOGIES,DG4162-VIRTUAL,SIM0001,00.01"
            if upper in {"*TST?", "*OPC?"}:
                return "0" if upper == "*TST?" else "1"
            if upper in {":SYST:ERR?", "SYST:ERR?"}:
                return '0,"No error"'
            path = upper.rstrip("?").lstrip(":")
            if path in {"COUP:STAT", "COUP:STATE", "COUPLING:STATE"}:
                return self.values.get("COUP:STAT", "FREQ:OFF,PHASE:OFF,AMPL:OFF")
            known = self._query_known(path)
            if known is not None:
                return known
            return self.values.get(path, "0")

    def _apply_known(self, path: str, value: str) -> None:
        load_match = re.fullmatch(r"OUTP(?:UT)?(1|2):(?:LOAD|IMP(?:EDANCE)?)", path)
        if load_match:
            self.channels[int(load_match.group(1))]["load_ohms"] = self._load(value)
            return
        output_match = re.fullmatch(r"OUTP(?:UT)?(1|2)(?::STAT(?:E)?)?", path)
        if output_match:
            self.channels[int(output_match.group(1))]["output"] = self._on(value)
            return
        source_match = re.fullmatch(r"SOUR(?:CE)?(1|2):(.+)", path)
        if not source_match:
            return
        channel_index = int(source_match.group(1))
        channel = self.channels[channel_index]
        suffix = source_match.group(2)
        numeric = self._number(value)
        if suffix in {"FUNC", "FUNCTION", "FUNCTION:SHAPE"}:
            channel["function"] = value.upper()
        elif suffix in {"FREQ", "FREQUENCY", "FREQUENCY:FIXED"} and numeric is not None:
            channel["frequency_hz"] = numeric
        elif suffix in {"VOLT:UNIT", "VOLTAGE:UNIT"}:
            if value.upper() not in {"VPP", "VRMS", "DBM"}:
                raise ValueError("不支持的虚拟 DG 幅度单位")
            self.amplitude_units[channel_index] = value.upper()
        elif suffix in {"VOLT", "VOLTAGE", "VOLTAGE:LEVEL:IMMEDIATE:AMPLITUDE"}:
            match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?)\s*(MVPP|VPP|MVRMS|VRMS|DBM)?", value.upper())
            if match:
                amplitude = float(match.group(1))
                unit = match.group(2) or self.amplitude_units[channel_index]
                if unit in {"MVPP", "MVRMS"}:
                    amplitude *= .001
                if unit in {"VRMS", "MVRMS"}:
                    amplitude *= 2 * math.sqrt(2)
                elif unit == "DBM":
                    load = channel["load_ohms"]
                    if load is None:
                        raise ValueError("高阻负载不支持 dBm")
                    amplitude = 2 * math.sqrt(2 * float(load) * .001 * 10 ** (amplitude / 10))
                channel["amplitude_vpp"] = amplitude
        elif suffix in {"VOLT:OFFS", "VOLTAGE:OFFSET", "VOLTAGE:LEVEL:IMMEDIATE:OFFSET"} and numeric is not None:
            channel["offset_v"] = numeric
        elif suffix in {"PHAS", "PHASE", "PHASE:ADJUST"} and numeric is not None:
            channel["phase_deg"] = numeric
        elif suffix in {"MOD", "MOD:STAT", "MOD:STATE"}:
            channel["modulation_enabled"] = self._on(value)
        elif suffix == "MOD:TYPE":
            channel["modulation_type"] = value.upper()
        elif suffix in {"MOD:PSK:PHAS", "MOD:PSKEY:PHASE"} and numeric is not None:
            channel["psk_phase_deg"] = numeric
        elif suffix in {"MOD:FSK:FREQ", "MOD:FSKEY:FREQUENCY"} and numeric is not None:
            channel["fsk_frequency_hz"] = numeric

    def _query_known(self, path: str) -> str | None:
        load_match = re.fullmatch(r"OUTP(?:UT)?(1|2):(?:LOAD|IMP(?:EDANCE)?)", path)
        if load_match:
            load = self.channels[int(load_match.group(1))]["load_ohms"]
            return "INFINITY" if load is None else str(load)
        output_match = re.fullmatch(r"OUTP(?:UT)?(1|2)(?::STAT(?:E)?)?", path)
        if output_match:
            return "ON" if self.channels[int(output_match.group(1))]["output"] else "OFF"
        source_match = re.fullmatch(r"SOUR(?:CE)?(1|2):(.+)", path)
        if not source_match:
            return None
        channel = self.channels[int(source_match.group(1))]
        suffix = source_match.group(2)
        if suffix in {"VOLT:UNIT", "VOLTAGE:UNIT"}:
            return self.amplitude_units[int(source_match.group(1))]
        mapping = {
            "FUNC": "function",
            "FUNCTION": "function",
            "FREQ": "frequency_hz",
            "FREQUENCY": "frequency_hz",
            "VOLT": "amplitude_vpp",
            "VOLTAGE": "amplitude_vpp",
            "VOLT:OFFS": "offset_v",
            "VOLTAGE:OFFSET": "offset_v",
            "PHAS": "phase_deg",
            "PHASE": "phase_deg",
            "MOD:TYPE": "modulation_type",
            "MOD:PSK:PHAS": "psk_phase_deg",
            "MOD:FSK:FREQ": "fsk_frequency_hz",
        }
        key = mapping.get(suffix)
        if key:
            return str(channel[key])
        if suffix in {"MOD", "MOD:STAT", "MOD:STATE"}:
            return "ON" if channel["modulation_enabled"] else "OFF"
        return None

    def snapshot(self) -> VirtualGeneratorSnapshot:
        with self.lock:
            channels = tuple(
                VirtualChannelSnapshot(channel=index, **values)  # type: ignore[arg-type]
                for index, values in sorted(self.channels.items())
            )
            return VirtualGeneratorSnapshot(
                resource=self.resource,
                connected=self.connected,
                scpi_delay_ms=self.scpi_delay_ms,
                channels=channels,
                command_count=len(self.commands),
                recent_commands=tuple(self.commands)[-20:],
            )

    @staticmethod
    def _number(value: str) -> float | None:
        try:
            return float(value)
        except ValueError:
            return None

    @staticmethod
    def _on(value: str) -> bool:
        return value.strip().upper() in {"1", "ON", "TRUE"}

    @staticmethod
    def _load(value: str) -> int | None:
        normalized = value.strip().upper()
        if normalized in {"INF", "INFINITY", "MAX", "MAXIMUM"}:
            return None
        if normalized in {"MIN", "MINIMUM"}:
            return 1
        try:
            return int(float(normalized))
        except ValueError:
            return None


class VirtualScpiSession:
    def __init__(self, generator: VirtualGenerator) -> None:
        self.resource = generator.resource
        self._generator = generator
        self._identity = ""
        self._lock = RLock()
        self._connection_generation = 0
        self._status_lock = Lock()
        self._status = (False, "")

    @property
    def is_connected(self) -> bool:
        with self._status_lock:
            return self._status[0]

    @property
    def identity(self) -> str:
        with self._status_lock:
            return self._status[1]

    def _publish_status(self, connected: bool) -> None:
        # Match the real session: manager/card status reads must never acquire
        # the I/O lock held by a manual transaction that checks manager leases.
        with self._status_lock:
            self._status = (connected, self._identity if connected else "")

    @property
    def connection_generation(self) -> int:
        with self._lock:
            return self._connection_generation

    def connect(self) -> str:
        with self._lock:
            previously_connected = self._generator.connected
            if not previously_connected:
                self._connection_generation += 1
            self._generator.connected = True
            try:
                self._identity = self._generator.query("*IDN?")
            except Exception:
                if not previously_connected:
                    self._generator.connected = False
                    self._connection_generation += 1
                    self._identity = ""
                    self._publish_status(False)
                raise
            self._publish_status(True)
            return self._identity

    def disconnect(self) -> None:
        with self._lock:
            if self._generator.connected:
                self._connection_generation += 1
            self._generator.connected = False
            self._identity = ""
            self._publish_status(False)

    def write(self, command: str, *, recover: bool = False, after_recover=None) -> None:
        del recover, after_recover
        with self._lock:
            self._require_connection()
            self._generator.write(command)

    @contextmanager
    def transaction(self):
        with self._lock:
            yield self

    def write_many(self, commands, *, recover: bool = False, after_recover=None) -> None:
        del recover, after_recover
        with self._lock:
            self._require_connection()
            for command in commands:
                self._generator.write(command)

    def query(self, command: str, *, recover: bool = False) -> str:
        del recover
        with self._lock:
            self._require_connection()
            result = self._generator.query(command)
            if command.strip().upper() == "*IDN?":
                self._identity = result
                self._publish_status(True)
            return result

    def _require_connection(self) -> None:
        if not self._generator.connected:
            raise RuntimeError("虚拟 DG4000 尚未连接")


class VirtualEdgeCounter:
    def __init__(self, lab: "VirtualLab", resource: str) -> None:
        self.lab = lab
        self.resource = resource
        self._baseline = 0
        self._running = False
        self._lock = RLock()

    def start(self) -> None:
        with self._lock:
            if self._running:
                raise RuntimeError("虚拟触发计数器已经运行")
            self._baseline = self.lab.trigger_total(self.resource)
            self._running = True

    def read_count(self) -> int:
        with self._lock:
            if not self._running:
                raise RuntimeError("虚拟触发计数器尚未启动")
            return self.lab.trigger_total(self.resource) - self._baseline

    def stop(self) -> None:
        with self._lock:
            self._running = False


class VirtualAnalogInput:
    """Real-time virtual voltage input for plotting and data-export tests."""

    def __init__(
        self,
        config: AnalogInputConfig,
        *,
        signal_frequency_hz: float = 50.0,
        amplitude_v: float = 1.0,
        offset_v: float = 0.0,
        noise_v: float = 0.01,
    ) -> None:
        self.config = config
        self.signal_frequency_hz = float(signal_frequency_hz)
        self.amplitude_v = float(amplitude_v)
        self.offset_v = float(offset_v)
        self.noise_v = float(noise_v)
        self._next_sample_index = 0
        self._stop_event = Event()
        self._running = False
        self._random = random.Random(4000)

    def start(self) -> None:
        self.config.validate()
        if not all(
            math.isfinite(value)
            for value in (
                self.signal_frequency_hz,
                self.amplitude_v,
                self.offset_v,
                self.noise_v,
            )
        ):
            raise ValueError("虚拟信号参数必须是有限数值")
        if self.signal_frequency_hz < 0 or self.amplitude_v < 0 or self.noise_v < 0:
            raise ValueError("虚拟信号频率、幅度和噪声不能为负数")
        if self._running:
            raise RuntimeError("虚拟模拟输入已经运行")
        self._next_sample_index = 0
        self._random.seed(4000)
        self._stop_event.clear()
        self._running = True

    def read(self) -> AnalogInputBlock:
        if not self._running:
            raise RuntimeError("虚拟模拟输入尚未启动")
        duration = self.config.samples_per_read / self.config.sample_rate_hz
        if self._stop_event.wait(timeout=duration) or not self._running:
            raise RuntimeError("虚拟模拟输入已停止")
        first = self._next_sample_index
        data = []
        span = self.config.maximum_v - self.config.minimum_v
        for channel_index, _channel in enumerate(self.config.channels):
            phase = channel_index * math.pi / 4.0
            values = []
            for offset in range(self.config.samples_per_read):
                sample_index = first + offset
                angle = (
                    2.0
                    * math.pi
                    * self.signal_frequency_hz
                    * sample_index
                    / self.config.sample_rate_hz
                    + phase
                )
                noise = self._random.uniform(-self.noise_v, self.noise_v)
                value = self.offset_v + self.amplitude_v * math.sin(angle) + noise
                values.append(
                    min(self.config.maximum_v, max(self.config.minimum_v, value))
                )
            data.append(tuple(values))
        self._next_sample_index += self.config.samples_per_read
        return AnalogInputBlock(
            channel_names=self.config.channels,
            sample_rate_hz=self.config.sample_rate_hz,
            first_sample_index=first,
            samples=tuple(data),
        )

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()


class VirtualLab:
    def __init__(self) -> None:
        self._generators: dict[str, VirtualGenerator] = {}
        self._sessions: dict[str, VirtualScpiSession] = {}
        self._trigger_totals: dict[str, int] = {}
        self._lock = RLock()

    def scpi_session_for(self, resource: str) -> VirtualScpiSession:
        with self._lock:
            if resource not in self._generators:
                self._generators[resource] = VirtualGenerator(resource)
            if resource not in self._sessions:
                self._sessions[resource] = VirtualScpiSession(self._generators[resource])
            return self._sessions[resource]

    def edge_counter_for(self, resource: str) -> VirtualEdgeCounter:
        with self._lock:
            self._trigger_totals.setdefault(resource, 0)
        return VirtualEdgeCounter(self, resource)

    def analog_input_for(
        self,
        resource: str,
        config: AnalogInputConfig,
        *,
        signal_frequency_hz: float = 50.0,
        amplitude_v: float = 1.0,
        offset_v: float = 0.0,
        noise_v: float = 0.01,
    ) -> VirtualAnalogInput:
        with self._lock:
            self._trigger_totals.setdefault(resource, 0)
        return VirtualAnalogInput(
            config,
            signal_frequency_hz=signal_frequency_hz,
            amplitude_v=amplitude_v,
            offset_v=offset_v,
            noise_v=noise_v,
        )

    def emit_trigger(self, resource: str, count: int = 1) -> int:
        if count < 1:
            raise ValueError("虚拟触发次数必须大于 0")
        with self._lock:
            self._trigger_totals[resource] = self._trigger_totals.get(resource, 0) + count
            return self._trigger_totals[resource]

    def trigger_total(self, resource: str) -> int:
        with self._lock:
            return self._trigger_totals.get(resource, 0)

    def generator_snapshot(self, resource: str) -> VirtualGeneratorSnapshot:
        self.scpi_session_for(resource)
        with self._lock:
            generator = self._generators[resource]
        return generator.snapshot()

    def set_scpi_delay(self, resource: str, delay_ms: int) -> None:
        if not 0 <= delay_ms <= 2000:
            raise ValueError("虚拟 SCPI 延迟必须在 0 到 2000 ms 之间")
        session = self.scpi_session_for(resource)
        session._generator.scpi_delay_ms = int(delay_ms)
