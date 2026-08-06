from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock

from .drivers.dg4000 import DG4000Driver
from .drivers.ni_analog import AnalogInputConfig, NIDaqAnalogInput
from .drivers.ni_counter import NIDaqEdgeCounter
from .drivers.scpi import ScpiSession
from .models import DeviceKind, DeviceRecord
from .services import DeviceProbeService, ProbeState
from .virtual_lab import (
    VirtualAnalogInput,
    VirtualEdgeCounter,
    VirtualLab,
    is_virtual_resource,
)


class DeviceConnectionError(RuntimeError):
    pass


class DeviceBusyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ConnectionInfo:
    device_id: str
    connected: bool
    identity: str = ""
    message: str = ""


SessionFactory = Callable[[str], ScpiSession]


class DeviceConnectionManager:
    """Own persistent device sessions shared by the home page and settings windows."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory = ScpiSession,
        probe_service: DeviceProbeService | None = None,
        virtual_lab: VirtualLab | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._probe_service = probe_service or DeviceProbeService()
        self.virtual_lab = virtual_lab or VirtualLab()
        self._sessions: dict[str, ScpiSession] = {}
        self._resources: dict[str, str] = {}
        self._scpi_connected: set[str] = set()
        self._ni_connected: set[str] = set()
        self._identities: dict[str, str] = {}
        self._leases: dict[str, str] = {}
        self._lock = RLock()

    def connect(self, record: DeviceRecord) -> ConnectionInfo:
        if record.kind is DeviceKind.NI_DAQ:
            with self._lock:
                if record.id in self._ni_connected:
                    return self.info(record)
            if is_virtual_resource(record.resource):
                with self._lock:
                    self._ni_connected.add(record.id)
                    identity = "NI USB-6363 VIRTUAL · SIM0001"
                    self._identities[record.id] = identity
                return ConnectionInfo(record.id, True, identity, "虚拟 NI 设备已连接")
            result = self._probe_service.probe(record)
            if result.state is not ProbeState.ONLINE:
                raise DeviceConnectionError(result.message)
            with self._lock:
                self._ni_connected.add(record.id)
                self._identities[record.id] = result.identity
            return ConnectionInfo(record.id, True, result.identity, "NI-DAQmx 已识别设备")

        session = self.session_for(record)
        with self._lock:
            if record.id in self._scpi_connected:
                return self.info(record)
        try:
            identity = session.connect()
        except Exception as exc:
            raise DeviceConnectionError(str(exc)) from exc
        with self._lock:
            self._scpi_connected.add(record.id)
            self._identities[record.id] = identity
        return ConnectionInfo(record.id, True, identity, "VISA 会话已连接")

    def disconnect(self, record: DeviceRecord) -> ConnectionInfo:
        with self._lock:
            owner = self._leases.get(record.id)
        if owner is not None:
            raise DeviceBusyError(f"设备“{record.name}”正被联动任务占用，停止任务后才能断开")
        if record.kind is DeviceKind.NI_DAQ:
            with self._lock:
                self._ni_connected.discard(record.id)
                self._identities.pop(record.id, None)
            return ConnectionInfo(record.id, False, message="NI 逻辑连接已释放")
        with self._lock:
            session = self._sessions.get(record.id)
            connected = record.id in self._scpi_connected
        if session is not None and connected:
            try:
                session.disconnect()
            except Exception as exc:
                raise DeviceConnectionError(str(exc)) from exc
        with self._lock:
            self._scpi_connected.discard(record.id)
            self._identities.pop(record.id, None)
        return ConnectionInfo(record.id, False, message="VISA 会话已断开")

    def info(self, record: DeviceRecord) -> ConnectionInfo:
        with self._lock:
            if record.kind is DeviceKind.NI_DAQ:
                connected = record.id in self._ni_connected
            else:
                connected = record.id in self._scpi_connected
            return ConnectionInfo(
                record.id,
                connected,
                self._identities.get(record.id, ""),
                "已连接" if connected else "未连接",
            )

    def is_connected(self, record: DeviceRecord) -> bool:
        return self.info(record).connected

    def acquire_devices(self, owner: str, device_ids: list[str] | tuple[str, ...]) -> None:
        """Atomically reserve devices for one running hardware task."""

        normalized = tuple(dict.fromkeys(item.strip() for item in device_ids if item.strip()))
        if not owner.strip():
            raise ValueError("设备占用者不能为空")
        if not normalized:
            raise ValueError("至少需要占用一台设备")
        with self._lock:
            conflicts = [
                device_id
                for device_id in normalized
                if device_id in self._leases and self._leases[device_id] != owner
            ]
            if conflicts:
                raise DeviceBusyError(
                    "以下设备已被其他任务占用：" + ", ".join(conflicts)
                )
            for device_id in normalized:
                self._leases[device_id] = owner

    def release_devices(self, owner: str) -> None:
        with self._lock:
            for device_id in [
                key for key, current_owner in self._leases.items() if current_owner == owner
            ]:
                self._leases.pop(device_id, None)

    def lease_owner(self, device_id: str) -> str | None:
        with self._lock:
            return self._leases.get(device_id)

    def session_for(self, record: DeviceRecord) -> ScpiSession:
        if record.kind is DeviceKind.NI_DAQ:
            raise ValueError("NI DAQ 不使用 SCPI 会话")
        with self._lock:
            resource_changed = (
                record.id in self._sessions
                and self._resources.get(record.id) != record.resource
            )
            connected = record.id in self._scpi_connected
        if resource_changed and connected:
            self.disconnect(record)
        with self._lock:
            if resource_changed:
                self._sessions.pop(record.id, None)
                self._resources.pop(record.id, None)
            return self._session_for_locked(record)

    def dg_driver_for(self, record: DeviceRecord) -> DG4000Driver:
        if record.kind is not DeviceKind.DG4000:
            raise ValueError("只有 DG4000 设备可创建 DG4000Driver")
        return DG4000Driver(self.session_for(record))

    def counter_for(
        self,
        record: DeviceRecord,
        *,
        counter: str = "ctr0",
        terminal: str | None = None,
        rising: bool = True,
    ) -> NIDaqEdgeCounter | VirtualEdgeCounter:
        if record.kind is not DeviceKind.NI_DAQ:
            raise ValueError("只有 NI DAQ 设备可创建触发计数器")
        if is_virtual_resource(record.resource):
            return self.virtual_lab.edge_counter_for(record.resource)
        return NIDaqEdgeCounter(
            record.resource,
            counter=counter,
            terminal=terminal,
            rising=rising,
        )

    def analog_channels_for(self, record: DeviceRecord) -> tuple[str, ...]:
        if record.kind is not DeviceKind.NI_DAQ:
            raise ValueError("只有 NI DAQ 设备可读取模拟输入通道")
        if is_virtual_resource(record.resource):
            return tuple(f"{record.resource}/ai{index}" for index in range(4))
        return NIDaqAnalogInput.available_channels(record.resource)

    def analog_input_for(
        self,
        record: DeviceRecord,
        config: AnalogInputConfig,
        *,
        signal_frequency_hz: float = 50.0,
        amplitude_v: float = 1.0,
        offset_v: float = 0.0,
        noise_v: float = 0.01,
    ) -> NIDaqAnalogInput | VirtualAnalogInput:
        if record.kind is not DeviceKind.NI_DAQ:
            raise ValueError("只有 NI DAQ 设备可创建模拟输入任务")
        if is_virtual_resource(record.resource):
            return self.virtual_lab.analog_input_for(
                record.resource,
                config,
                signal_frequency_hz=signal_frequency_hz,
                amplitude_v=amplitude_v,
                offset_v=offset_v,
                noise_v=noise_v,
            )
        return NIDaqAnalogInput(record.resource, config)

    def forget(self, record: DeviceRecord) -> None:
        self.disconnect(record)
        with self._lock:
            self._sessions.pop(record.id, None)
            self._resources.pop(record.id, None)

    def close_all(self) -> None:
        with self._lock:
            sessions = [
                session
                for device_id, session in self._sessions.items()
                if device_id in self._scpi_connected
            ]
            self._scpi_connected.clear()
            self._ni_connected.clear()
            self._identities.clear()
            self._leases.clear()
        for session in sessions:
            try:
                session.disconnect()
            except Exception:
                pass

    def _session_for_locked(self, record: DeviceRecord) -> ScpiSession:
        existing = self._sessions.get(record.id)
        existing_resource = self._resources.get(record.id)
        if existing is not None and existing_resource != record.resource:
            existing = None
        if existing is None:
            existing = (
                self.virtual_lab.scpi_session_for(record.resource)
                if is_virtual_resource(record.resource)
                else self._session_factory(record.resource)
            )
            self._sessions[record.id] = existing
            self._resources[record.id] = record.resource
        return existing
