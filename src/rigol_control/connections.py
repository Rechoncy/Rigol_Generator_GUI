from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock

from .drivers.dg4000 import DG4000Driver
from .drivers.newport_smc100 import Smc100Device, VirtualSmc100Device, parse_resource
from .drivers.ni_analog import AnalogInputConfig, NIDaqAnalogInput
from .drivers.ni_counter import NIDaqEdgeCounter
from .drivers.scpi import ScpiSession
from .drivers.vaunix_lms import VaunixLmsDevice, VirtualVaunixLmsDevice
from .models import DeviceKind, DeviceRecord
from .services import DeviceProbeService, ProbeResult, ProbeState
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
        self._vaunix_connected: set[str] = set()
        self._vaunix_devices: dict[str, VaunixLmsDevice | VirtualVaunixLmsDevice] = {}
        self._vaunix_resources: dict[str, str] = {}
        self._identities: dict[str, str] = {}
        self._leases: dict[str, str] = {}
        self._newport_devices: dict[str, Smc100Device | VirtualSmc100Device] = {}
        self._newport_connecting: set[str] = set()
        self._known_records: dict[str, DeviceRecord] = {}
        self._physical_resources: dict[str, str] = {}
        self._transitions: dict[str, str] = {}
        self._closing_all = False
        self._lock = RLock()

    @staticmethod
    def physical_resource(record: DeviceRecord) -> str:
        """Use one identity for SCPI profiles addressing the same VISA resource."""
        resource = record.resource.strip().casefold()
        if record.kind in {DeviceKind.DG4000, DeviceKind.SIGLENT_SSG, DeviceKind.GENERIC_SCPI}:
            return f"scpi:{resource}"
        if record.kind is DeviceKind.NEWPORT_SMC100 and not is_virtual_resource(record.resource):
            port, _ = parse_resource(record.resource)
            return f"serial:{port.casefold()}"
        if record.kind is DeviceKind.NI_DAQ:
            resource = resource.strip("/")
        return f"{record.kind.value}:{resource}"

    def _register_record_locked(self, record: DeviceRecord) -> str:
        key = self.physical_resource(record)
        old_key = self._physical_resources.get(record.id)
        if old_key is not None and old_key != key:
            if self._lease_for_resource_locked(record.id) or old_key in self._transitions:
                raise DeviceBusyError("设备仍被任务占用或正在连接/断开，不能替换资源地址")
            if self._record_connected_locked(record.id):
                raise DeviceBusyError("请先断开旧设备地址，再替换资源")
        self._known_records[record.id] = record
        self._physical_resources[record.id] = key
        return key

    def _record_connected_locked(self, device_id: str) -> bool:
        if device_id in self._scpi_connected:
            session = self._sessions.get(device_id)
            return session is not None and session.is_connected
        if device_id in self._ni_connected:
            return True
        if device_id in self._vaunix_connected:
            device = self._vaunix_devices.get(device_id)
            return device is not None and device.is_connected
        device = self._newport_devices.get(device_id)
        return device is not None and device.is_connected

    def _lease_for_resource_locked(self, device_id: str) -> str | None:
        key = self._physical_resources.get(device_id)
        for other_id, owner in self._leases.items():
            if other_id == device_id or (key is not None and self._physical_resources.get(other_id) == key):
                return owner
        return None

    @contextmanager
    def _connection_transition(self, record: DeviceRecord, action: str, *, owner: str | None = None):
        with self._lock:
            if self._closing_all:
                raise DeviceBusyError("正在关闭设备连接，请等待操作完成")
            key = self._register_record_locked(record)
            current_owner = self._lease_for_resource_locked(record.id)
            owns_exact_lease = owner is not None and self._leases.get(record.id) == owner
            if current_owner is not None and not (action == "连接" and owns_exact_lease):
                if current_owner.startswith("siglent-native:"):
                    raise DeviceBusyError(f"设备“{record.name}”正在执行 SSG 通信或原生扫描，请先停止并确认后{action}")
                raise DeviceBusyError(f"设备“{record.name}”正被联动任务占用，停止任务后才能{action}")
            if key in self._transitions:
                raise DeviceBusyError(f"同一资源正在{self._transitions[key]}，请稍后重试")
            if action in {"连接", "探测"}:
                aliases = [other_id for other_id, other_key in self._physical_resources.items()
                           if other_id != record.id and other_key == key and self._record_connected_locked(other_id)]
                if aliases:
                    raise DeviceBusyError("同一物理资源已由另一设备记录连接，请先断开该记录")
            self._transitions[key] = action
        try:
            yield
        finally:
            with self._lock:
                self._transitions.pop(key, None)

    def assert_manual_access(self, record: DeviceRecord) -> None:
        """Reject manual commands through either a task's record or its aliases."""
        with self._lock:
            if self._closing_all:
                raise DeviceBusyError("正在关闭设备连接，请等待操作完成")
            key = self._register_record_locked(record)
            if self._lease_for_resource_locked(record.id) is not None:
                raise DeviceBusyError("同一物理资源正在执行扫描/联动任务，请先停止任务再执行手动操作")
            if key in self._transitions:
                raise DeviceBusyError(f"同一资源正在{self._transitions[key]}，请等待操作完成")

    def active_transitions(self) -> dict[str, str]:
        with self._lock:
            return {**self._transitions, **({"all-devices": "关闭"} if self._closing_all else {})}

    def connect(self, record: DeviceRecord, *, owner: str | None = None) -> ConnectionInfo:
        if record.kind in {DeviceKind.DG4000, DeviceKind.SIGLENT_SSG, DeviceKind.GENERIC_SCPI}:
            self.session_for(record)
        else:
            with self._lock:
                self._register_record_locked(record)
        if self.is_connected(record):
            return self.info(record)
        with self._connection_transition(record, "连接", owner=owner):
            return self._connect_record(record)

    def probe(self, record: DeviceRecord, service: DeviceProbeService | None = None):
        if self.is_connected(record):
            info = self.info(record)
            return ProbeResult(ProbeState.ONLINE, "已连接（共享现有会话）", info.identity)
        with self._connection_transition(record, "探测"):
            return (service or self._probe_service).probe(record)

    def _connect_record(self, record: DeviceRecord) -> ConnectionInfo:
        if record.kind is DeviceKind.NEWPORT_SMC100:
            with self._lock:
                device = self.newport_device_for(record)
                if record.id in self._newport_connecting:
                    raise DeviceBusyError("Newport 正在连接")
                self._newport_connecting.add(record.id)
            try:
                identity = device.connect()
                with self._lock:
                    self._identities[record.id] = identity
                return ConnectionInfo(record.id, True, identity, "Newport 串口已连接")
            finally:
                with self._lock:
                    self._newport_connecting.discard(record.id)
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

        if record.kind is DeviceKind.VAUNIX_LMS:
            device = self.vaunix_device_for(record)
            with self._lock:
                if record.id in self._vaunix_connected and device.is_connected:
                    return self.info(record)
                self._vaunix_connected.discard(record.id)
                self._identities.pop(record.id, None)
            try:
                identity = device.connect()
            except Exception as exc:
                raise DeviceConnectionError(str(exc)) from exc
            with self._lock:
                self._vaunix_connected.add(record.id)
                self._identities[record.id] = identity
            return ConnectionInfo(record.id, True, identity, "Vaunix USB DLL 已连接")

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
        with self._connection_transition(record, "断开"):
            return self._disconnect_record(record)

    def _disconnect_record(self, record: DeviceRecord) -> ConnectionInfo:
        with self._lock:
            owner = self._leases.get(record.id)
        if owner is not None:
            if owner.startswith("siglent-native:"):
                raise DeviceBusyError(f"设备“{record.name}”正在执行 SSG 通信或原生扫描，"
                                      "请等待通信结束；扫描曾启用时请停止并回读确认后断开")
            raise DeviceBusyError(f"设备“{record.name}”正被联动任务占用，停止任务后才能断开")
        if record.kind is DeviceKind.NEWPORT_SMC100:
            with self._lock:
                if record.id in self._newport_connecting:
                    raise DeviceBusyError("Newport 正在连接，请等待操作完成")
                device = self._newport_devices.get(record.id)
                if device is not None:
                    device.disconnect()
                self._identities.pop(record.id, None)
            return ConnectionInfo(record.id, False, message="Newport 已断开")
        if record.kind is DeviceKind.NI_DAQ:
            with self._lock:
                self._ni_connected.discard(record.id)
                self._identities.pop(record.id, None)
            return ConnectionInfo(record.id, False, message="NI 逻辑连接已释放")
        if record.kind is DeviceKind.VAUNIX_LMS:
            with self._lock:
                device = self._vaunix_devices.get(record.id)
                connected = record.id in self._vaunix_connected
            if device is not None and connected:
                try:
                    device.disconnect()
                except Exception as exc:
                    raise DeviceConnectionError(str(exc)) from exc
                finally:
                    with self._lock:
                        self._vaunix_connected.discard(record.id)
                        self._identities.pop(record.id, None)
            with self._lock:
                self._vaunix_connected.discard(record.id)
                self._identities.pop(record.id, None)
            return ConnectionInfo(record.id, False, message="Vaunix USB 设备已关闭")
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
            elif record.kind is DeviceKind.NEWPORT_SMC100:
                device = self._newport_devices.get(record.id)
                connected = device is not None and device.is_connected
                if not connected:
                    self._identities.pop(record.id, None)
            elif record.kind is DeviceKind.VAUNIX_LMS:
                device = self._vaunix_devices.get(record.id)
                connected = (
                    record.id in self._vaunix_connected
                    and device is not None
                    and device.is_connected
                )
                if not connected:
                    self._vaunix_connected.discard(record.id)
                    self._identities.pop(record.id, None)
            else:
                session = self._sessions.get(record.id)
                connected = (
                    record.id in self._scpi_connected
                    and session is not None
                    and session.is_connected
                )
                if not connected:
                    self._scpi_connected.discard(record.id)
                    self._identities.pop(record.id, None)
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
            if self._closing_all:
                raise DeviceBusyError("正在关闭设备连接，不能启动任务")
            resource_keys = [self._physical_resources.get(item, f"device:{item}") for item in normalized]
            if len(set(resource_keys)) != len(resource_keys):
                raise DeviceBusyError("同一物理资源不能通过多条设备记录参与同一任务")
            transitions = [item for item in normalized
                           if self._physical_resources.get(item) in self._transitions]
            if transitions:
                raise DeviceBusyError("设备正在连接/断开，不能启动任务：" + ", ".join(transitions))
            disconnected = [item for item in normalized
                            if item in self._known_records and not self._record_connected_locked(item)
                            and not (self._known_records[item].kind is DeviceKind.SIGLENT_SSG
                                     and owner == f"siglent-native:{item}")]
            if disconnected:
                raise DeviceBusyError("设备连接已改变，请重新连接后启动任务：" + ", ".join(disconnected))
            conflicts = [
                device_id
                for device_id in normalized
                if self._lease_for_resource_locked(device_id) not in {None, owner}
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
            return self._lease_for_resource_locked(device_id)

    def active_leases(self) -> dict[str, str]:
        """Snapshot used to prevent configuration replacement during hardware work."""
        with self._lock:
            return dict(self._leases)

    def session_for(self, record: DeviceRecord) -> ScpiSession:
        if record.kind in {DeviceKind.NI_DAQ, DeviceKind.VAUNIX_LMS, DeviceKind.NEWPORT_SMC100}:
            raise ValueError("此设备不使用 SCPI 会话")
        with self._lock:
            resource_changed = (
                record.id in self._sessions
                and self._resources.get(record.id) != record.resource
            )
            connected = record.id in self._scpi_connected
        if resource_changed and connected:
            with self._lock:
                previous = self._known_records.get(record.id)
            self.disconnect(previous or record.updated(resource=self._resources[record.id]))
        with self._lock:
            self._register_record_locked(record)
            if resource_changed:
                self._sessions.pop(record.id, None)
                self._resources.pop(record.id, None)
            return self._session_for_locked(record)

    def dg_driver_for(self, record: DeviceRecord) -> DG4000Driver:
        if record.kind is not DeviceKind.DG4000:
            raise ValueError("只有 DG4000 设备可创建 DG4000Driver")
        driver = DG4000Driver(self.session_for(record))
        driver.set_identity(self.info(record).identity)
        return driver

    def vaunix_device_for(
        self,
        record: DeviceRecord,
    ) -> VaunixLmsDevice | VirtualVaunixLmsDevice:
        if record.kind is not DeviceKind.VAUNIX_LMS:
            raise ValueError("只有 Vaunix LMS/BLX 可创建设备驱动")
        with self._lock:
            self._register_record_locked(record)
            resource_changed = (
                record.id in self._vaunix_devices
                and self._vaunix_resources.get(record.id) != record.resource
            )
            if resource_changed and record.id in self._vaunix_connected:
                raise DeviceBusyError("请先断开旧的 Vaunix 资源，再修改设备地址")
            if resource_changed:
                self._vaunix_devices.pop(record.id, None)
                self._vaunix_resources.pop(record.id, None)
            device = self._vaunix_devices.get(record.id)
            if device is None:
                device = (
                    VirtualVaunixLmsDevice(record.resource)
                    if is_virtual_resource(record.resource)
                    else VaunixLmsDevice(record.resource)
                )
                self._vaunix_devices[record.id] = device
                self._vaunix_resources[record.id] = record.resource
            return device

    def newport_device_for(self, record: DeviceRecord) -> Smc100Device | VirtualSmc100Device:
        if record.kind is not DeviceKind.NEWPORT_SMC100:
            raise ValueError("只有 Newport 设备可创建 SMC100 驱动")
        record.validate()
        with self._lock:
            self._register_record_locked(record)
            existing = self._newport_devices.get(record.id)
            if existing is not None and existing.resource != record.resource:
                if existing.is_connected or self._leases.get(record.id):
                    raise DeviceBusyError("请先停止任务并断开旧 Newport 地址")
                existing = None
            # First release supports one connected axis per physical COM port.
            # Reject aliases rather than creating competing serial sessions.
            if not is_virtual_resource(record.resource):
                port, _ = parse_resource(record.resource)
                for key, other in self._newport_devices.items():
                    if (key != record.id and (other.is_connected or key in self._newport_connecting)
                            and getattr(other, "port", None) == port):
                        raise DeviceBusyError(f"串口 {port} 已被另一 Newport 设备使用")
            if existing is None:
                existing = (VirtualSmc100Device(record.resource) if is_virtual_resource(record.resource)
                            else Smc100Device(record.resource))
                self._newport_devices[record.id] = existing
            return existing

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
        with self._lock:
            self._register_record_locked(record)
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
        with self._connection_transition(record, "删除"):
            self._disconnect_record(record)
            with self._lock:
                self._sessions.pop(record.id, None)
                self._resources.pop(record.id, None)
                self._vaunix_devices.pop(record.id, None)
                self._vaunix_resources.pop(record.id, None)
                self._newport_devices.pop(record.id, None)

    def close_all(self) -> None:
        with self._lock:
            if self._leases or self._transitions or self._closing_all:
                raise DeviceBusyError("仍有任务占用设备或设备正在连接/断开，请先停止任务并等待操作完成")
            self._closing_all = True
            sessions = [
                session
                for device_id, session in self._sessions.items()
                if device_id in self._scpi_connected
            ]
            self._scpi_connected.clear()
            self._ni_connected.clear()
            vaunix_devices = [
                device
                for device_id, device in self._vaunix_devices.items()
                if device_id in self._vaunix_connected
            ]
            self._vaunix_connected.clear()
            self._identities.clear()
            self._leases.clear()
            newport_devices = list(self._newport_devices.values())
        try:
            for session in sessions:
                try:
                    session.disconnect()
                except Exception:
                    pass
            for device in [*vaunix_devices, *newport_devices]:
                try:
                    device.disconnect()
                except Exception:
                    pass
        finally:
            with self._lock:
                self._closing_all = False

    def _session_for_locked(self, record: DeviceRecord) -> ScpiSession:
        existing = self._sessions.get(record.id)
        existing_resource = self._resources.get(record.id)
        if existing is not None and existing_resource != record.resource:
            existing = None
        if existing is None:
            if record.kind is DeviceKind.SIGLENT_SSG and is_virtual_resource(record.resource):
                from .drivers.siglent_ssg import VirtualSsgBackend
                backend = VirtualSsgBackend()
                existing = ScpiSession(record.resource, backend_factory=lambda *_: backend)
                self._sessions[record.id] = existing
                self._resources[record.id] = record.resource
                return existing
            existing = (
                self.virtual_lab.scpi_session_for(record.resource)
                if is_virtual_resource(record.resource)
                else self._session_factory(record.resource)
            )
            self._sessions[record.id] = existing
            self._resources[record.id] = record.resource
        return existing
