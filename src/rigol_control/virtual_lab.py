from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from threading import RLock


VIRTUAL_DG_RESOURCE = "SIM::DG4000::1"
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
        }

    def reset(self) -> None:
        with self.lock:
            self.channels = {1: self._default_channel(), 2: self._default_channel()}
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
                self.values.clear()
                return
            if upper in {"*CLS", "*WAI"}:
                return
            command_path, _, raw_value = normalized.partition(" ")
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
            known = self._query_known(path)
            if known is not None:
                return known
            return self.values.get(path, "0")

    def _apply_known(self, path: str, value: str) -> None:
        output_match = re.fullmatch(r"OUTP(?:UT)?(1|2)(?::STAT(?:E)?)?", path)
        if output_match:
            self.channels[int(output_match.group(1))]["output"] = self._on(value)
            return
        source_match = re.fullmatch(r"SOUR(?:CE)?(1|2):(.+)", path)
        if not source_match:
            return
        channel = self.channels[int(source_match.group(1))]
        suffix = source_match.group(2)
        numeric = self._number(value)
        if suffix in {"FUNC", "FUNCTION", "FUNCTION:SHAPE"}:
            channel["function"] = value.upper()
        elif suffix in {"FREQ", "FREQUENCY", "FREQUENCY:FIXED"} and numeric is not None:
            channel["frequency_hz"] = numeric
        elif suffix in {"VOLT", "VOLTAGE", "VOLTAGE:LEVEL:IMMEDIATE:AMPLITUDE"} and numeric is not None:
            channel["amplitude_vpp"] = numeric
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
        output_match = re.fullmatch(r"OUTP(?:UT)?(1|2)(?::STAT(?:E)?)?", path)
        if output_match:
            return "ON" if self.channels[int(output_match.group(1))]["output"] else "OFF"
        source_match = re.fullmatch(r"SOUR(?:CE)?(1|2):(.+)", path)
        if not source_match:
            return None
        channel = self.channels[int(source_match.group(1))]
        suffix = source_match.group(2)
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


class VirtualScpiSession:
    def __init__(self, generator: VirtualGenerator) -> None:
        self.resource = generator.resource
        self._generator = generator
        self._lock = RLock()

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._generator.connected

    def connect(self) -> str:
        with self._lock:
            self._generator.connected = True
            return self._generator.query("*IDN?")

    def disconnect(self) -> None:
        with self._lock:
            self._generator.connected = False

    def write(self, command: str) -> None:
        with self._lock:
            self._require_connection()
            self._generator.write(command)

    def write_many(self, commands) -> None:
        with self._lock:
            self._require_connection()
            for command in commands:
                self._generator.write(command)

    def query(self, command: str) -> str:
        with self._lock:
            self._require_connection()
            return self._generator.query(command)

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
