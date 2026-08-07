from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .drivers.vaunix_lms import (
    VIRTUAL_VAUNIX_RESOURCE,
    VaunixLmsApi,
    format_dll_version,
)
from .models import DeviceKind, DeviceRecord
from .virtual_lab import VIRTUAL_DG_RESOURCE, VIRTUAL_NI_RESOURCE


class ProbeState(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DRIVER_MISSING = "driver_missing"
    UNCHECKED = "unchecked"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    state: ProbeState
    message: str
    identity: str = ""


class DiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DiscoveredResource:
    resource: str
    connection: str
    identity: str = ""

    @property
    def display_text(self) -> str:
        return f"{self.resource}  —  {self.identity}" if self.identity else self.resource


class DeviceDiscoveryService:
    """Enumerate selectable VISA, NI-DAQmx, and Vaunix LMS/BLX resources."""

    def discover(self, kind: DeviceKind) -> list[DiscoveredResource]:
        if kind is DeviceKind.NI_DAQ:
            virtual = DiscoveredResource(
                VIRTUAL_NI_RESOURCE,
                "内置模拟器",
                "虚拟 NI USB-6363 - 无需硬件",
            )
            try:
                return [virtual, *self._discover_ni()]
            except DiscoveryError:
                return [virtual]
        if kind is DeviceKind.DG4000:
            virtual = DiscoveredResource(
                VIRTUAL_DG_RESOURCE,
                "内置模拟器",
                "虚拟 RIGOL DG4162 - 无需硬件",
            )
            try:
                return [virtual, *self._discover_visa()]
            except DiscoveryError:
                return [virtual]
        if kind is DeviceKind.VAUNIX_LMS:
            virtual = DiscoveredResource(
                VIRTUAL_VAUNIX_RESOURCE,
                "内置模拟器",
                "虚拟 Vaunix BLX-403 - 无需硬件",
            )
            try:
                api = VaunixLmsApi()
                hardware = [
                    DiscoveredResource(
                        descriptor.resource,
                        "Vaunix USB DLL",
                        f"{descriptor.model} · SN {descriptor.serial_number}",
                    )
                    for descriptor in api.enumerate_devices()
                ]
            except Exception:
                hardware = []
            return [virtual, *hardware]
        return self._discover_visa()

    @staticmethod
    def connection_for_visa(resource: str) -> str:
        prefix = resource.strip().upper().split("::", 1)[0]
        if prefix.startswith("USB"):
            return "USB VISA"
        if prefix.startswith("TCPIP"):
            return "TCP/IP VISA"
        if prefix.startswith("GPIB"):
            return "GPIB VISA"
        if prefix.startswith("ASRL"):
            return "串口 VISA"
        return "VISA"

    def _discover_visa(self) -> list[DiscoveredResource]:
        try:
            import pyvisa  # type: ignore[import-not-found]
        except ImportError as exc:
            raise DiscoveryError("未安装 PyVISA，暂时无法自动搜索；仍可手动输入资源地址") from exc
        manager = None
        try:
            manager = pyvisa.ResourceManager()
            resources = sorted(str(item) for item in manager.list_resources())
            return [
                DiscoveredResource(
                    resource=resource,
                    connection=self.connection_for_visa(resource),
                )
                for resource in resources
            ]
        except Exception as exc:
            raise DiscoveryError(f"VISA 资源搜索失败：{exc}") from exc
        finally:
            if manager is not None:
                try:
                    manager.close()
                except Exception:
                    pass

    @staticmethod
    def _discover_ni() -> list[DiscoveredResource]:
        try:
            import nidaqmx.system  # type: ignore[import-not-found]
        except ImportError as exc:
            raise DiscoveryError(
                "未安装 NI-DAQmx Python，暂时无法自动搜索；仍可手动输入设备名"
            ) from exc
        try:
            resources: list[DiscoveredResource] = []
            for device in nidaqmx.system.System.local().devices:
                identity_parts = [str(device.product_type)]
                serial = getattr(device, "dev_serial_num", None)
                if serial is not None:
                    identity_parts.append(f"SN {serial}")
                resources.append(
                    DiscoveredResource(
                        resource=str(device.name),
                        connection="NI-DAQmx 本机设备",
                        identity=" · ".join(identity_parts),
                    )
                )
            return sorted(resources, key=lambda item: item.resource.casefold())
        except Exception as exc:
            raise DiscoveryError(f"NI 设备搜索失败：{exc}") from exc


class DeviceProbeService:
    """Best-effort discovery with optional instrument packages loaded lazily."""

    def probe(self, record: DeviceRecord) -> ProbeResult:
        if record.kind is DeviceKind.NI_DAQ:
            return self._probe_ni(record)
        if record.kind is DeviceKind.VAUNIX_LMS:
            return self._probe_vaunix(record)
        return self._probe_scpi(record)

    @staticmethod
    def _probe_vaunix(record: DeviceRecord) -> ProbeResult:
        if record.resource.strip().upper().startswith("SIM::"):
            return ProbeResult(
                ProbeState.ONLINE,
                "虚拟设备可用",
                "Vaunix BLX-403-VIRTUAL · SN 4030001",
            )
        try:
            api = VaunixLmsApi()
            descriptor = next(
                (
                    item
                    for item in api.enumerate_devices()
                    if item.resource.casefold() == record.resource.strip().casefold()
                ),
                None,
            )
            if descriptor is None:
                return ProbeResult(
                    ProbeState.OFFLINE,
                    "检测失败：当前枚举结果中没有该序列号",
                )
            identity = (
                f"Vaunix {descriptor.model} · SN {descriptor.serial_number} · "
                f"DLL {format_dll_version(api.dll_version)}"
            )
            return ProbeResult(
                ProbeState.ONLINE,
                "已枚举设备（探测不会打开或关闭设备句柄）",
                identity,
            )
        except Exception as exc:
            message = str(exc)
            state = (
                ProbeState.DRIVER_MISSING
                if "vnx_fmsynth.dll" in message or "加载 Vaunix DLL" in message
                else ProbeState.OFFLINE
            )
            return ProbeResult(state, f"检测失败：{message}")

    @staticmethod
    def _probe_scpi(record: DeviceRecord) -> ProbeResult:
        try:
            import pyvisa  # type: ignore[import-not-found]
        except ImportError:
            return ProbeResult(ProbeState.DRIVER_MISSING, "未安装 PyVISA")
        manager = None
        instrument = None
        try:
            manager = pyvisa.ResourceManager()
            instrument = manager.open_resource(record.resource, open_timeout=800)
            instrument.timeout = 800
            identity = str(instrument.query("*IDN?")).strip()
            return ProbeResult(ProbeState.ONLINE, "已连接", identity)
        except Exception as exc:  # Device backends expose heterogeneous exceptions.
            return ProbeResult(ProbeState.OFFLINE, f"连接失败：{exc}")
        finally:
            if instrument is not None:
                try:
                    instrument.close()
                except Exception:
                    pass
            if manager is not None:
                try:
                    manager.close()
                except Exception:
                    pass

    @staticmethod
    def _probe_ni(record: DeviceRecord) -> ProbeResult:
        try:
            import nidaqmx.system  # type: ignore[import-not-found]
        except ImportError:
            return ProbeResult(ProbeState.DRIVER_MISSING, "未安装 NI-DAQmx Python")
        try:
            devices = nidaqmx.system.System.local().devices
            device = next((item for item in devices if item.name.casefold() == record.resource.casefold()), None)
            if device is None:
                return ProbeResult(ProbeState.OFFLINE, "NI 系统中未发现此设备")
            identity = f"{device.product_type} · {device.dev_serial_num}"
            return ProbeResult(ProbeState.ONLINE, "已连接", identity)
        except Exception as exc:
            return ProbeResult(ProbeState.OFFLINE, f"检测失败：{exc}")
