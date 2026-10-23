from __future__ import annotations

import ctypes
import math
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Callable

from .dg4000 import StepParameter, StepValue
from ..runtime import bundle_root, executable_dir, is_frozen


MAX_DEVICES = 64
MAX_MODEL_NAME = 32
MAX_SEQUENCE_ENTRIES = 50
POWER_ENCODING_DB = 0.25
POWER_RESOLUTION_DB = 0.5
VIRTUAL_VAUNIX_RESOURCE = "SIM::VAUNIX_LMS::1"
_VAUNIX_DLL_LOCK = RLock()
_TRANSIENT_STATUS_CODES = {0x80020000, 0xFFFF0002}
_NOT_READY_STATUS_CODES = {0x80030000, 0xFFFF0003}
_CONNECTION_STATUS_CODES = (
    _TRANSIENT_STATUS_CODES | _NOT_READY_STATUS_CODES | {0x80000000, 0xFFFF0000}
)
_HID_RETRY_ATTEMPTS = 2
_HID_RETRY_DELAY_SECONDS = 0.02

DEV_CONNECTED = 0x00000001
DEV_OPENED = 0x00000002
SWP_ACTIVE = 0x00000004
SWP_UP = 0x00000008
SWP_REPEAT = 0x00000010
SWP_BIDIRECTIONAL = 0x00000020
PLL_LOCKED = 0x00000040
FAST_PULSE_OPTION = 0x00000080

STOP_SEQUENCE = 0
START_SEQUENCE = 1
REPEAT_SEQUENCE = 2
PAUSE_SEQUENCE = 3
RESUME_SEQUENCE = 4

_STATUS_NAMES = {
    0x80000000: "INVALID_DEVID",
    0x80010000: "BAD_PARAMETER",
    0x80020000: "BAD_HID_IO",
    0x80030000: "DEVICE_NOT_READY",
    0xFFFF0000: "INVALID_DEVID",
    0xFFFF0001: "BAD_PARAMETER",
    0xFFFF0002: "BAD_HID_IO",
    0xFFFF0003: "DEVICE_NOT_READY",
}


class VaunixLmsError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VaunixDeviceDescriptor:
    device_id: int
    model: str
    serial_number: int
    status: int

    @property
    def resource(self) -> str:
        return f"VAUNIX::LMS::{self.serial_number}"


@dataclass(frozen=True, slots=True)
class VaunixCapabilities:
    model: str
    serial_number: int
    dll_version: int
    minimum_frequency_hz: float
    maximum_frequency_hz: float
    minimum_power_dbm: float
    maximum_power_dbm: float
    has_fast_pulse: bool
    has_chirp: bool
    is_blx: bool


@dataclass(frozen=True, slots=True)
class VaunixSequenceElement:
    frequency_hz: float
    power_dbm: float
    controls_power: bool


@dataclass(frozen=True, slots=True)
class VaunixOutputSnapshot:
    connected: bool
    identity: str
    capabilities: VaunixCapabilities | None
    frequency_hz: float
    power_dbm: float
    rf_on: bool
    internal_reference: bool
    device_status: int


@dataclass(frozen=True, slots=True)
class VaunixSnapshot:
    connected: bool
    identity: str
    capabilities: VaunixCapabilities | None
    frequency_hz: float
    power_dbm: float
    rf_on: bool
    internal_reference: bool
    sweep_start_hz: float
    sweep_end_hz: float
    sweep_step_hz: float | None
    sweep_time_ms: int
    dwell_time_ms: int
    idle_time_ms: int
    sweep_active: bool
    sweep_up: bool
    sweep_repeat: bool
    sweep_bidirectional: bool
    external_sweep_trigger: bool
    pulse_on_seconds: float
    pulse_off_seconds: float
    internal_pulse_enabled: bool
    external_pulse_enabled: bool
    chirp_length_seconds: float
    chirp_repeat_seconds: float
    sequence_start: int
    sequence_count: int
    sequence_dwell_ms: int
    sequence_idle_ms: int
    sequence: tuple[VaunixSequenceElement, ...]
    device_status: int


def locate_vaunix_dll() -> Path:
    configured = os.environ.get("VAUNIX_LMS_DLL", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    module_path = Path(__file__).resolve()
    resource_root = bundle_root()
    if is_frozen():
        candidates.extend((
            executable_dir() / "vendor" / "vaunix" / "vnx_fmsynth.dll",
            resource_root / "vendor" / "vaunix" / "vnx_fmsynth.dll",
            executable_dir() / "vnx_fmsynth.dll",
            resource_root / "vnx_fmsynth.dll",
            module_path.parent / "vnx_fmsynth.dll",
        ))
        search_roots = ()
    else:
        search_roots = (Path.cwd(), resource_root)
        candidates.extend((
            resource_root / "vendor" / "vaunix" / "vnx_fmsynth.dll",
            Path.cwd() / "vnx_fmsynth.dll",
            resource_root / "vnx_fmsynth.dll",
            module_path.parent / "vnx_fmsynth.dll",
        ))
    patterns = (
        "lms-sdk-*/LMS_Windows_SDK_*/x64/Release/vnx_fmsynth.dll",
        "lms-sdk-*/LMS_Windows_SDK_*/Python_Example/vnx_fmsynth.dll",
    )
    for root in dict.fromkeys(search_roots):
        for pattern in patterns:
            candidates.extend(sorted(root.glob(pattern), reverse=True))
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
    raise VaunixLmsError(
        "未找到 Vaunix vnx_fmsynth.dll。请将厂商 Windows x64 DLL 放到 "
        "EXE 旁的 vendor/vaunix/ 文件夹（源码版也支持 lms-sdk 文件夹），或设置 "
        "VAUNIX_LMS_DLL 指向 Windows x64 DLL。"
    )


class VaunixLmsApi:
    """Typed ctypes binding for the complete LMS/BLX Windows USB API."""

    def __init__(self, dll_path: Path | str | None = None) -> None:
        self.dll_path = Path(dll_path) if dll_path else locate_vaunix_dll()
        # vnx_fmsynth.dll owns process-global USB state. Discovery, probing and
        # connected devices must therefore share one lock even when represented
        # by different Python API objects.
        self._lock = _VAUNIX_DLL_LOCK
        with self._lock:
            self.dll = self.load_library(self.dll_path)
            self.dll.fnLMS_SetTestMode(False)

    @classmethod
    def load_library(cls, dll_path: Path | str) -> ctypes.CDLL:
        """Load and bind the SDK without enumerating or opening USB devices."""
        try:
            dll = ctypes.CDLL(str(dll_path))
            cls._configure_prototypes(dll)
        except (OSError, AttributeError) as exc:
            raise VaunixLmsError(f"无法加载 Vaunix DLL：{exc}") from exc
        return dll

    @staticmethod
    def _configure_prototypes(d) -> None:
        c_uint = ctypes.c_uint
        c_int = ctypes.c_int
        c_bool = ctypes.c_bool
        c_float = ctypes.c_float
        c_void_p = ctypes.c_void_p

        signatures: dict[str, tuple[list[Any], Any]] = {
            "fnLMS_SetTraceLevel": ([c_int, c_int, c_bool], None),
            "fnLMS_SetTestMode": ([c_bool], None),
            "fnLMS_GetNumDevices": ([], c_int),
            "fnLMS_GetDevInfo": ([ctypes.POINTER(c_uint)], c_int),
            "fnLMS_GetModelNameA": ([c_uint, c_void_p], c_int),
            "fnLMS_InitDevice": ([c_uint], c_int),
            "fnLMS_CloseDevice": ([c_uint], c_int),
            "fnLMS_GetSerialNumber": ([c_uint], c_int),
            "fnLMS_GetDLLVersion": ([], c_int),
            "fnLMS_SetFrequencyEx": ([c_uint, c_uint], c_int),
            "fnLMS_SetStartFrequencyEx": ([c_uint, c_uint], c_int),
            "fnLMS_SetEndFrequencyEx": ([c_uint, c_uint], c_int),
            "fnLMS_SetSweepTime": ([c_uint, c_int], c_int),
            "fnLMS_SetFrequencyStepEx": ([c_uint, c_uint], c_int),
            "fnLMS_SetDwellTime": ([c_uint, c_int], c_int),
            "fnLMS_SetIdleTime": ([c_uint, c_int], c_int),
            "fnLMS_SetPowerLevel": ([c_uint, c_int], c_int),
            "fnLMS_SetRFOn": ([c_uint, c_bool], c_int),
            "fnLMS_SetPulseOnTime": ([c_uint, c_float], c_int),
            "fnLMS_SetPulseOffTime": ([c_uint, c_float], c_int),
            "fnLMS_EnableInternalPulseMod": ([c_uint, c_bool], c_int),
            "fnLMS_SetUseExternalPulseMod": ([c_uint, c_bool], c_int),
            "fnLMS_SetFastPulsedOutput": ([c_uint, c_float, c_float, c_bool], c_int),
            "fnLMS_SetUseInternalRef": ([c_uint, c_bool], c_int),
            "fnLMS_SetSweepDirection": ([c_uint, c_bool], c_int),
            "fnLMS_SetSweepMode": ([c_uint, c_bool], c_int),
            "fnLMS_SetSweepType": ([c_uint, c_bool], c_int),
            "fnLMS_StartSweep": ([c_uint, c_bool], c_int),
            "fnLMS_SetUseExternalSweepTrigger": ([c_uint, c_bool], c_int),
            "fnLMS_SetChirpMode": ([c_uint, c_bool], c_int),
            "fnLMS_SetChirpLength": ([c_uint, c_float], c_int),
            "fnLMS_SetChirpRepeatRate": ([c_uint, c_float], c_int),
            "fnLMS_SetSequenceElement": ([c_uint, c_int, c_uint, c_int, c_bool], c_int),
            "fnLMS_SetSequenceStart": ([c_uint, c_int], c_int),
            "fnLMS_SetSequenceCount": ([c_uint, c_int], c_int),
            "fnLMS_SetSequenceDwellTime": ([c_uint, c_int], c_int),
            "fnLMS_SetSequenceIdleTime": ([c_uint, c_int], c_int),
            "fnLMS_StartSequence": ([c_uint, c_int], c_int),
            "fnLMS_SaveSettings": ([c_uint], c_int),
            "fnLMS_GetFrequencyEx": ([c_uint], c_uint),
            "fnLMS_GetStartFrequencyEx": ([c_uint], c_uint),
            "fnLMS_GetEndFrequencyEx": ([c_uint], c_uint),
            "fnLMS_GetSweepTime": ([c_uint], c_int),
            "fnLMS_GetFrequencyStepEx": ([c_uint], c_uint),
            "fnLMS_GetDwellTime": ([c_uint], c_int),
            "fnLMS_GetIdleTime": ([c_uint], c_int),
            "fnLMS_GetRF_On": ([c_uint], c_int),
            "fnLMS_GetUseInternalRef": ([c_uint], c_int),
            "fnLMS_GetPowerLevel": ([c_uint], c_int),
            "fnLMS_GetAbsPowerLevel": ([c_uint], c_int),
            "fnLMS_GetMaxPwr": ([c_uint], c_int),
            "fnLMS_GetMinPwr": ([c_uint], c_int),
            "fnLMS_GetMaxFreqEx": ([c_uint], c_uint),
            "fnLMS_GetMinFreqEx": ([c_uint], c_uint),
            "fnLMS_GetPulseOnTime": ([c_uint], c_float),
            "fnLMS_GetPulseOffTime": ([c_uint], c_float),
            "fnLMS_GetPulseMode": ([c_uint], c_int),
            "fnLMS_GetChirpLength": ([c_uint], c_float),
            "fnLMS_GetChirpRepeatRate": ([c_uint], c_float),
            "fnLMS_GetHasChirpMode": ([c_uint], c_int),
            "fnLMS_GetHasFastPulseMode": ([c_uint], c_int),
            "fnLMS_GetUseInternalPulseMod": ([c_uint], c_int),
            "fnLMS_GetUseInternalSweepTrigger": ([c_uint], c_int),
            "fnLMS_GetSeqElementFrequency": ([c_uint, c_int], c_uint),
            "fnLMS_GetSeqElementPower": ([c_uint, c_int], c_int),
            "fnLMS_GetSeqElementPwrControl": ([c_uint, c_int], c_int),
            "fnLMS_GetSequenceStart": ([c_uint], c_int),
            "fnLMS_GetSequenceCount": ([c_uint], c_int),
            "fnLMS_GetSequenceDwellTime": ([c_uint], c_int),
            "fnLMS_GetSequenceIdleTime": ([c_uint], c_int),
            "fnLMS_GetDeviceStatus": ([c_uint], c_int),
        }
        for name, (argtypes, restype) in signatures.items():
            function = getattr(d, name)
            function.argtypes = argtypes
            function.restype = restype

    @property
    def dll_version(self) -> int:
        with self._lock:
            return int(self.dll.fnLMS_GetDLLVersion())

    def enumerate_devices(self) -> tuple[VaunixDeviceDescriptor, ...]:
        with self._lock:
            self.dll.fnLMS_SetTestMode(False)
            count = int(self.dll.fnLMS_GetNumDevices())
            if count < 0:
                _raise_status("fnLMS_GetNumDevices", count)
            handles = (ctypes.c_uint * MAX_DEVICES)()
            reported = int(self.dll.fnLMS_GetDevInfo(handles))
            if reported < 0:
                _raise_status("fnLMS_GetDevInfo", reported)
            descriptors = []
            for index in range(min(reported, MAX_DEVICES)):
                device_id = int(handles[index])
                model_buffer = ctypes.create_string_buffer(MAX_MODEL_NAME)
                model_length = self.dll.fnLMS_GetModelNameA(
                    device_id,
                    ctypes.cast(model_buffer, ctypes.c_void_p),
                )
                model = model_buffer.value.decode("ascii", errors="replace")
                serial = int(self.dll.fnLMS_GetSerialNumber(device_id))
                status = int(self.dll.fnLMS_GetDeviceStatus(device_id))
                # Hot unplug can occur between the enumeration and these reads.
                if any(_unsigned_32(value) in _STATUS_NAMES
                       for value in (model_length, serial, status)):
                    continue
                if not model or serial <= 0 or not status & DEV_CONNECTED:
                    continue
                descriptors.append(
                    VaunixDeviceDescriptor(device_id, model, serial, status)
                )
            return tuple(descriptors)


ApiFactory = Callable[[], VaunixLmsApi]


class VaunixLmsDevice:
    """Connected LMS/BLX device with every SDK 1.15 operation exposed."""

    def __init__(
        self,
        resource: str,
        *,
        api_factory: ApiFactory = VaunixLmsApi,
    ) -> None:
        self.resource = resource.strip()
        self._api_factory = api_factory
        self._api: VaunixLmsApi | None = None
        self._device_id: int | None = None
        self._descriptor: VaunixDeviceDescriptor | None = None
        self._capabilities: VaunixCapabilities | None = None
        self._identity = ""
        self._lock = RLock()
        self._connection_generation = 0
        # Connection-card timers must not wait behind USB calls or a whole
        # multi-parameter point. This lock protects cached fields only.
        self._status_lock = Lock()
        self._connection_status = (False, "")

    @contextmanager
    def transaction(self):
        """Serialize a related UI operation with task arming and device writes."""
        with self._lock:
            yield self

    @property
    def connection_generation(self) -> int:
        with self._lock:
            return self._connection_generation

    @property
    def is_connected(self) -> bool:
        with self._status_lock:
            return self._connection_status[0]

    @property
    def identity(self) -> str:
        with self._status_lock:
            return self._connection_status[1]

    def _publish_connection_status(self, connected: bool) -> None:
        with self._status_lock:
            self._connection_status = (connected, self._identity if connected else "")

    @property
    def capabilities(self) -> VaunixCapabilities:
        if self._capabilities is None:
            raise VaunixLmsError("Vaunix 设备尚未连接")
        return self._capabilities

    def connect(self) -> str:
        with self._lock:
            if self._device_id is not None:
                return self._identity
            api = self._api_factory()
            serial = _serial_from_resource(self.resource)
            descriptor = next(
                (item for item in api.enumerate_devices() if item.serial_number == serial),
                None,
            )
            if descriptor is None:
                raise VaunixLmsError(f"未发现序列号为 {serial} 的 Vaunix LMS/BLX")
            with api._lock:
                _check_set_status(
                    "fnLMS_InitDevice",
                    api.dll.fnLMS_InitDevice(descriptor.device_id),
                )
            self._api = api
            self._device_id = descriptor.device_id
            self._connection_generation += 1
            self._descriptor = descriptor
            try:
                minimum_frequency = self._get_frequency("fnLMS_GetMinFreqEx")
                maximum_frequency = self._get_frequency("fnLMS_GetMaxFreqEx")
                minimum_power = self._get_int("fnLMS_GetMinPwr") / 4.0
                maximum_power = self._get_int("fnLMS_GetMaxPwr") / 4.0
                self._capabilities = VaunixCapabilities(
                    model=descriptor.model,
                    serial_number=descriptor.serial_number,
                    dll_version=api.dll_version,
                    minimum_frequency_hz=minimum_frequency,
                    maximum_frequency_hz=maximum_frequency,
                    minimum_power_dbm=minimum_power,
                    maximum_power_dbm=maximum_power,
                    has_fast_pulse=bool(self._get_int("fnLMS_GetHasFastPulseMode")),
                    has_chirp=bool(self._get_int("fnLMS_GetHasChirpMode")),
                    is_blx=descriptor.model.upper().startswith("BLX"),
                )
            except Exception:
                self._invalidate_connection()
                raise
            self._identity = (
                f"Vaunix {descriptor.model} · SN {descriptor.serial_number} · "
                f"DLL {format_dll_version(api.dll_version)} "
                f"(0x{api.dll_version:04X})"
            )
            self._publish_connection_status(True)
            return self._identity

    def disconnect(self) -> None:
        with self._lock:
            self._publish_connection_status(False)
            if self._device_id is None or self._api is None:
                return
            try:
                with self._api._lock:
                    status = self._api.dll.fnLMS_CloseDevice(self._device_id)
                _check_set_status("fnLMS_CloseDevice", status)
            finally:
                self._clear_connection()

    def _clear_connection(self) -> None:
        self._connection_generation += 1
        self._api = None
        self._device_id = None
        self._descriptor = None
        self._capabilities = None
        self._identity = ""
        self._publish_connection_status(False)

    def _invalidate_connection(self) -> None:
        # Preserve the original USB error even if releasing a stale handle fails.
        try:
            self.disconnect()
        except Exception:
            self._clear_connection()

    def set_frequency_hz(self, frequency_hz: float) -> None:
        value = _finite(frequency_hz, "输出频率")
        caps = self.capabilities
        if not caps.minimum_frequency_hz <= value <= caps.maximum_frequency_hz:
            raise ValueError(
                f"输出频率必须在 {caps.minimum_frequency_hz:g} 至 "
                f"{caps.maximum_frequency_hz:g} Hz 之间"
            )
        self._set("fnLMS_SetFrequencyEx", _frequency_units(value))

    def set_power_dbm(self, power_dbm: float) -> None:
        value = validate_power_dbm(power_dbm, "输出功率")
        caps = self.capabilities
        if not caps.minimum_power_dbm <= value <= caps.maximum_power_dbm:
            raise ValueError(
                f"输出功率必须在 {caps.minimum_power_dbm:g} 至 "
                f"{caps.maximum_power_dbm:g} dBm 之间"
            )
        self._set("fnLMS_SetPowerLevel", _power_units(value, "输出功率"))

    def set_rf_on(self, enabled: bool) -> None:
        self._set("fnLMS_SetRFOn", bool(enabled))

    def set_internal_reference(self, enabled: bool) -> None:
        self._set("fnLMS_SetUseInternalRef", bool(enabled))

    def configure_output(
        self,
        *,
        frequency_hz: float,
        power_dbm: float,
        rf_on: bool,
        internal_reference: bool,
    ) -> None:
        """Apply one output state as a DLL-serialized, preflighted operation."""

        frequency = _finite(frequency_hz, "输出频率")
        power = validate_power_dbm(power_dbm, "输出功率")
        caps = self.capabilities
        if not caps.minimum_frequency_hz <= frequency <= caps.maximum_frequency_hz:
            raise ValueError(
                f"输出频率必须在 {caps.minimum_frequency_hz:g} 至 "
                f"{caps.maximum_frequency_hz:g} Hz 之间"
            )
        if not caps.minimum_power_dbm <= power <= caps.maximum_power_dbm:
            raise ValueError(
                f"输出功率必须在 {caps.minimum_power_dbm:g} 至 "
                f"{caps.maximum_power_dbm:g} dBm 之间"
            )
        frequency_units = _frequency_units(frequency)
        power_units = _power_units(power, "输出功率")
        with self._lock:
            self._set("fnLMS_SetUseInternalRef", bool(internal_reference))
            self._set("fnLMS_SetFrequencyEx", frequency_units)
            self._set("fnLMS_SetPowerLevel", power_units)
            self._set("fnLMS_SetRFOn", bool(rf_on))

    def configure_sweep(
        self,
        *,
        start_hz: float,
        end_hz: float,
        step_hz: float,
        sweep_time_ms: int,
        dwell_time_ms: int,
        idle_time_ms: int,
        upward: bool,
        repeating: bool,
        bidirectional: bool,
        external_trigger: bool,
    ) -> None:
        caps = self.capabilities
        start = _finite(start_hz, "扫频起点")
        end = _finite(end_hz, "扫频终点")
        step = _finite(step_hz, "扫频步长") if caps.is_blx else None
        if not caps.minimum_frequency_hz <= start <= caps.maximum_frequency_hz:
            raise ValueError("扫频起点超出设备范围")
        if not caps.minimum_frequency_hz <= end <= caps.maximum_frequency_hz:
            raise ValueError("扫频终点超出设备范围")
        if step is not None and step <= 0:
            raise ValueError("扫频步长必须大于 0")
        if upward and start >= end:
            raise ValueError("向上扫频要求起点小于终点")
        if not upward and start <= end:
            raise ValueError("向下扫频要求起点大于终点")
        start_units, end_units = _frequency_units(start), _frequency_units(end)
        if (upward and start_units >= end_units) or (not upward and start_units <= end_units):
            raise ValueError("扫频起点和终点按 10 Hz 分辨率量化后必须保持扫描方向")
        if caps.is_blx:
            step_units = _frequency_units(step)
            if step_units < 1:
                raise ValueError("扫频步长按 10 Hz 分辨率量化后必须大于 0")
            dwell_ms = _milliseconds(dwell_time_ms, "扫频驻留时间", 10)
            idle_ms = _milliseconds(idle_time_ms, "扫频空闲时间", 0)
        else:
            duration_ms = _milliseconds(sweep_time_ms, "扫频时间", 1)
        with self._lock:
            self._set("fnLMS_SetStartFrequencyEx", start_units)
            self._set("fnLMS_SetEndFrequencyEx", end_units)
            if caps.is_blx:
                self._set("fnLMS_SetFrequencyStepEx", step_units)
                self._set("fnLMS_SetDwellTime", dwell_ms)
                self._set("fnLMS_SetIdleTime", idle_ms)
            else:
                self._set("fnLMS_SetSweepTime", duration_ms)
            self._set("fnLMS_SetSweepDirection", bool(upward))
            self._set("fnLMS_SetSweepMode", bool(repeating))
            self._set("fnLMS_SetSweepType", bool(bidirectional))
            self._set("fnLMS_SetUseExternalSweepTrigger", bool(external_trigger))

    def start_sweep(self, enabled: bool) -> None:
        self._set("fnLMS_StartSweep", bool(enabled), retry_transient=not enabled)

    def configure_pulse(
        self,
        *,
        on_seconds: float,
        period_seconds: float,
        enabled: bool,
        external_enabled: bool,
    ) -> None:
        has_internal_pulse = self.capabilities.has_fast_pulse
        if enabled and not has_internal_pulse:
            raise VaunixLmsError("此型号未安装内部脉冲调制选件")
        on_time = _finite(on_seconds, "脉冲开启时间")
        period = _finite(period_seconds, "脉冲周期")
        if not 1e-7 <= on_time <= 1e3:
            raise ValueError("脉冲开启时间必须在 100 ns 至 1000 s 之间")
        if not 1e-7 <= period <= 1e3 or period < on_time:
            raise ValueError("脉冲周期必须不小于开启时间，且不超过 1000 s")
        with self._lock:
            if has_internal_pulse:
                if enabled:
                    self._set("fnLMS_SetFastPulsedOutput", on_time, period, True)
                else:
                    self._set("fnLMS_EnableInternalPulseMod", False)
            self._set("fnLMS_SetUseExternalPulseMod", bool(external_enabled))

    def configure_chirp(
        self,
        *,
        enabled: bool,
        length_seconds: float,
        repeat_seconds: float,
        external_trigger: bool,
    ) -> None:
        if not self.capabilities.has_chirp:
            raise VaunixLmsError("此型号不支持 Chirp 模式")
        length = _finite(length_seconds, "Chirp 长度")
        repeat = _finite(repeat_seconds, "Chirp 重复周期")
        if not 1e-6 <= length <= 50e-3:
            raise ValueError("Chirp 长度必须在 1 µs 至 50 ms 之间")
        if repeat < 10e-6:
            raise ValueError("Chirp 重复周期不能小于 10 µs")
        with self._lock:
            self._set("fnLMS_SetChirpLength", length)
            self._set("fnLMS_SetChirpRepeatRate", repeat)
            self._set("fnLMS_SetUseExternalSweepTrigger", bool(external_trigger))
            self._set("fnLMS_SetChirpMode", bool(enabled))

    def set_sequence_element(
        self,
        index: int,
        frequency_hz: float,
        power_dbm: float,
        controls_power: bool,
    ) -> None:
        if not self.capabilities.is_blx:
            raise VaunixLmsError("序列功能仅适用于 BLX 型号")
        if not 0 <= int(index) < MAX_SEQUENCE_ENTRIES:
            raise ValueError("序列索引必须在 0 至 49 之间")
        frequency = _finite(frequency_hz, "序列频率")
        power = validate_power_dbm(power_dbm, "序列功率")
        caps = self.capabilities
        if not caps.minimum_frequency_hz <= frequency <= caps.maximum_frequency_hz:
            raise ValueError("序列频率超出设备范围")
        if not caps.minimum_power_dbm <= power <= caps.maximum_power_dbm:
            raise ValueError("序列功率超出设备范围")
        self._set(
            "fnLMS_SetSequenceElement",
            int(index),
            _frequency_units(frequency),
            _power_units(power, "序列功率"),
            bool(controls_power),
        )

    def configure_sequence(
        self,
        elements: list[VaunixSequenceElement] | tuple[VaunixSequenceElement, ...],
        *,
        start_index: int,
        dwell_time_ms: int,
        idle_time_ms: int,
    ) -> None:
        with self._lock:
            encoded, start, dwell_ms, idle_ms = _sequence_configuration(
                elements, self.capabilities, start_index, dwell_time_ms, idle_time_ms,
            )
            for index, (frequency, power, controls_power) in enumerate(encoded):
                self._set("fnLMS_SetSequenceElement", index, frequency, power, controls_power)
            self._set("fnLMS_SetSequenceStart", start)
            self._set("fnLMS_SetSequenceCount", len(encoded))
            self._set("fnLMS_SetSequenceDwellTime", dwell_ms)
            self._set("fnLMS_SetSequenceIdleTime", idle_ms)

    def control_sequence(self, control: int) -> None:
        if not self.capabilities.is_blx:
            raise VaunixLmsError("序列功能仅适用于 BLX 型号")
        if control not in {
            STOP_SEQUENCE,
            START_SEQUENCE,
            REPEAT_SEQUENCE,
            PAUSE_SEQUENCE,
            RESUME_SEQUENCE,
        }:
            raise ValueError("无效的序列控制命令")
        self._set("fnLMS_StartSequence", int(control), retry_transient=control == STOP_SEQUENCE)

    def save_settings(self) -> None:
        self._set("fnLMS_SaveSettings", retry_transient=False)

    def apply_step(self, parameter: StepParameter, channel: int, value: float) -> None:
        self.apply_steps((StepValue(parameter, channel, value),))

    def apply_steps(self, values: list[StepValue] | tuple[StepValue, ...]) -> None:
        with self._lock:
            commands = _step_commands(values, self.capabilities)
            for name, argument in commands:
                self._set(name, argument)

    def prepare_trigger(self) -> _VaunixTriggerWriter:
        """Bind this run to one connection without reading or changing output."""
        with self._lock:
            self._require()
            return _VaunixTriggerWriter(self, self._connection_generation)

    def _apply_trigger_steps(self, values, connection_generation: int) -> None:
        with self._lock:
            if connection_generation != self._connection_generation:
                raise VaunixLmsError("Vaunix 连接已经断开或重建，原触发任务已失效；请停止并重新布防")
            self._require()
            commands = _step_commands(values, self.capabilities)
            for name, argument in commands:
                # An SDK transport error may occur after accepting a setting.
                # Preserve the incomplete point and stop, never repeat it.
                self._set(name, argument, retry_transient=False)

    def _connected_status(self) -> int:
        status = self._get_int("fnLMS_GetDeviceStatus")
        if status & (DEV_CONNECTED | DEV_OPENED) != (DEV_CONNECTED | DEV_OPENED):
            self._invalidate_connection()
            raise VaunixLmsError("Vaunix USB 已断开或句柄失效，请重新连接设备")
        return status

    def output_snapshot(self) -> VaunixOutputSnapshot:
        """Connection metrics must not depend on optional sweep/sequence features."""
        with self._lock:
            status = self._connected_status()
            return VaunixOutputSnapshot(
                connected=True, identity=self._identity, capabilities=self.capabilities,
                frequency_hz=self._get_frequency("fnLMS_GetFrequencyEx"),
                power_dbm=self._get_int("fnLMS_GetAbsPowerLevel") / 4.0,
                rf_on=bool(self._get_int("fnLMS_GetRF_On")),
                internal_reference=bool(self._get_int("fnLMS_GetUseInternalRef")),
                device_status=status,
            )

    def snapshot(self) -> VaunixSnapshot:
        with self._lock:
            self._require()
            status = self._connected_status()
            count = self._get_int("fnLMS_GetSequenceCount") if self.capabilities.is_blx else 0
            count = min(MAX_SEQUENCE_ENTRIES, max(0, count))
            sequence = tuple(
                VaunixSequenceElement(
                    self._get_frequency("fnLMS_GetSeqElementFrequency", index),
                    self._get_int("fnLMS_GetSeqElementPower", index) / 4.0,
                    bool(self._get_int("fnLMS_GetSeqElementPwrControl", index)),
                )
                for index in range(count)
            )
            pulse_on = (
                self._get_float("fnLMS_GetPulseOnTime")
                if self.capabilities.has_fast_pulse
                else 0.0
            )
            pulse_off = (
                self._get_float("fnLMS_GetPulseOffTime")
                if self.capabilities.has_fast_pulse
                else 0.0
            )
            chirp_length = (
                self._get_float("fnLMS_GetChirpLength")
                if self.capabilities.has_chirp
                else 0.0
            )
            chirp_repeat = (
                self._get_float("fnLMS_GetChirpRepeatRate")
                if self.capabilities.has_chirp
                else 0.0
            )
            return VaunixSnapshot(
                connected=True,
                identity=self._identity,
                capabilities=self.capabilities,
                frequency_hz=self._get_frequency("fnLMS_GetFrequencyEx"),
                power_dbm=self._get_int("fnLMS_GetAbsPowerLevel") / 4.0,
                rf_on=bool(self._get_int("fnLMS_GetRF_On")),
                internal_reference=bool(self._get_int("fnLMS_GetUseInternalRef")),
                sweep_start_hz=self._get_frequency("fnLMS_GetStartFrequencyEx"),
                sweep_end_hz=self._get_frequency("fnLMS_GetEndFrequencyEx"),
                # The SDK implements frequency-step read/write for BLX only.
                # LMS time sweeps return BAD_PARAMETER for these functions.
                sweep_step_hz=(self._get_frequency("fnLMS_GetFrequencyStepEx")
                               if self.capabilities.is_blx else None),
                sweep_time_ms=(
                    0
                    if self.capabilities.is_blx
                    else self._get_int("fnLMS_GetSweepTime")
                ),
                dwell_time_ms=(
                    self._get_int("fnLMS_GetDwellTime")
                    if self.capabilities.is_blx
                    else 0
                ),
                idle_time_ms=(
                    self._get_int("fnLMS_GetIdleTime")
                    if self.capabilities.is_blx
                    else 0
                ),
                sweep_active=bool(status & SWP_ACTIVE),
                sweep_up=bool(status & SWP_UP),
                sweep_repeat=bool(status & SWP_REPEAT),
                sweep_bidirectional=bool(status & SWP_BIDIRECTIONAL),
                external_sweep_trigger=not bool(
                    self._get_int("fnLMS_GetUseInternalSweepTrigger")
                ),
                pulse_on_seconds=pulse_on,
                pulse_off_seconds=pulse_off,
                internal_pulse_enabled=(
                    bool(self._get_int("fnLMS_GetPulseMode"))
                    if self.capabilities.has_fast_pulse
                    else False
                ),
                external_pulse_enabled=(
                    not bool(self._get_int("fnLMS_GetUseInternalPulseMod"))
                    if self.capabilities.has_fast_pulse
                    else False
                ),
                chirp_length_seconds=chirp_length,
                chirp_repeat_seconds=chirp_repeat,
                sequence_start=(
                    self._get_int("fnLMS_GetSequenceStart")
                    if self.capabilities.is_blx
                    else 0
                ),
                sequence_count=count,
                sequence_dwell_ms=(
                    self._get_int("fnLMS_GetSequenceDwellTime")
                    if self.capabilities.is_blx
                    else 0
                ),
                sequence_idle_ms=(
                    self._get_int("fnLMS_GetSequenceIdleTime")
                    if self.capabilities.is_blx
                    else 0
                ),
                sequence=sequence,
                device_status=status,
            )

    def _require(self) -> tuple[VaunixLmsApi, int]:
        if self._api is None or self._device_id is None:
            raise VaunixLmsError("Vaunix LMS/BLX 尚未连接")
        return self._api, self._device_id

    def _set(self, function_name: str, *args: Any, retry_transient: bool = True) -> None:
        with self._lock:
            api, device_id = self._require()
            with api._lock:
                attempts = _HID_RETRY_ATTEMPTS if retry_transient else 0
                for attempt in range(attempts + 1):
                    status = getattr(api.dll, function_name)(device_id, *args)
                    code = _unsigned_32(status)
                    if code == 0:
                        return
                    if attempt < attempts and code in _TRANSIENT_STATUS_CODES:
                        time.sleep(_HID_RETRY_DELAY_SECONDS)
                        continue
                    if code in _CONNECTION_STATUS_CODES:
                        self._invalidate_connection()
                    _check_set_status(function_name, status)

    def _get_int(self, function_name: str, *args: Any) -> int:
        api, device_id = self._require()
        with api._lock:
            for attempt in range(_HID_RETRY_ATTEMPTS + 1):
                value = int(getattr(api.dll, function_name)(device_id, *args))
                code = _unsigned_32(value)
                if code not in _TRANSIENT_STATUS_CODES | _NOT_READY_STATUS_CODES:
                    if code in _CONNECTION_STATUS_CODES:
                        self._invalidate_connection()
                    _raise_get_error(function_name, value)
                    return value
                if attempt < _HID_RETRY_ATTEMPTS and code in _TRANSIENT_STATUS_CODES:
                    time.sleep(_HID_RETRY_DELAY_SECONDS)
                    continue
                self._invalidate_connection()
                _raise_get_error(function_name, value)
        raise AssertionError("Vaunix integer read retry loop exited unexpectedly")

    def _get_frequency(self, function_name: str, *args: Any) -> float:
        value = self._get_int(function_name, *args)
        return float(_unsigned_32(value)) * 10.0

    def _get_float(self, function_name: str, *args: Any) -> float:
        api, device_id = self._require()
        with api._lock:
            value = float(getattr(api.dll, function_name)(device_id, *args))
            if value in {-1.0, -3.0, -4.0}:
                if value in {-1.0, -3.0}:
                    self._invalidate_connection()
                raise VaunixLmsError(f"{function_name} 返回错误 {value:g}")
            return value


class VirtualVaunixLmsDevice:
    """Full-featured BLX-403 simulator used for hardware-free UI and NI linkage."""

    def __init__(self, resource: str = VIRTUAL_VAUNIX_RESOURCE) -> None:
        self.resource = resource
        self._connected = False
        self._lock = RLock()
        self._connection_generation = 0
        self._caps = VaunixCapabilities(
            model="BLX-403-VIRTUAL",
            serial_number=4030001,
            dll_version=0x010F,
            minimum_frequency_hz=500e6,
            maximum_frequency_hz=40e9,
            minimum_power_dbm=-40.0,
            maximum_power_dbm=10.0,
            has_fast_pulse=True,
            has_chirp=True,
            is_blx=True,
        )
        self.frequency_hz = 5e9
        self.power_dbm = 0.0
        self.rf_on = False
        self.internal_reference = True
        self.sweep_start_hz = 5e9
        self.sweep_end_hz = 6e9
        self.sweep_step_hz = 100e6
        self.sweep_time_ms = 1000
        self.dwell_time_ms = 10
        self.idle_time_ms = 0
        self.sweep_active = False
        self.sweep_up = True
        self.sweep_repeat = False
        self.sweep_bidirectional = False
        self.external_sweep_trigger = False
        self.pulse_on_seconds = 0.001
        self.pulse_off_seconds = 0.001
        self.internal_pulse_enabled = False
        self.external_pulse_enabled = False
        self.chirp_enabled = False
        self.chirp_length_seconds = 50e-6
        self.chirp_repeat_seconds = 100e-6
        self.sequence_start = 0
        self.sequence_dwell_ms = 10
        self.sequence_idle_ms = 0
        self.sequence: list[VaunixSequenceElement] = []
        self.sequence_control = STOP_SEQUENCE
        self.saved_settings_count = 0
        self.command_delay_ms = 2

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def identity(self) -> str:
        return (
            f"Vaunix {self._caps.model} · SN {self._caps.serial_number} · "
            f"DLL {format_dll_version(self._caps.dll_version)} "
            f"(0x{self._caps.dll_version:04X})"
        )

    @property
    def capabilities(self) -> VaunixCapabilities:
        return self._caps

    def connect(self) -> str:
        with self._lock:
            if not self._connected:
                self._connection_generation += 1
            self._connected = True
            return self.identity

    def disconnect(self) -> None:
        with self._lock:
            if self._connected:
                self._connection_generation += 1
            self._connected = False

    @contextmanager
    def transaction(self):
        with self._lock:
            yield self

    @property
    def connection_generation(self) -> int:
        with self._lock:
            return self._connection_generation

    def _delay(self) -> None:
        if not self._connected:
            raise VaunixLmsError("虚拟 Vaunix 尚未连接")
        if self.command_delay_ms:
            time.sleep(self.command_delay_ms / 1000.0)

    def set_frequency_hz(self, value: float) -> None:
        self._delay()
        number = _finite(value, "输出频率")
        if not self._caps.minimum_frequency_hz <= number <= self._caps.maximum_frequency_hz:
            raise ValueError("输出频率超出虚拟设备范围")
        self.frequency_hz = round(number / 10.0) * 10.0

    def set_power_dbm(self, value: float) -> None:
        self._delay()
        number = validate_power_dbm(value, "输出功率")
        if not self._caps.minimum_power_dbm <= number <= self._caps.maximum_power_dbm:
            raise ValueError("输出功率超出虚拟设备范围")
        self.power_dbm = round(number / POWER_RESOLUTION_DB) * POWER_RESOLUTION_DB

    def set_rf_on(self, enabled: bool) -> None:
        self._delay()
        self.rf_on = bool(enabled)

    def set_internal_reference(self, enabled: bool) -> None:
        self._delay()
        self.internal_reference = bool(enabled)

    def configure_output(
        self,
        *,
        frequency_hz: float,
        power_dbm: float,
        rf_on: bool,
        internal_reference: bool,
    ) -> None:
        with self._lock:
            self._delay()
            frequency = _finite(frequency_hz, "输出频率")
            power = validate_power_dbm(power_dbm, "输出功率")
            if not self._caps.minimum_frequency_hz <= frequency <= self._caps.maximum_frequency_hz:
                raise ValueError("输出频率超出虚拟设备范围")
            if not self._caps.minimum_power_dbm <= power <= self._caps.maximum_power_dbm:
                raise ValueError("输出功率超出虚拟设备范围")
            self.internal_reference = bool(internal_reference)
            self.frequency_hz = round(frequency / 10.0) * 10.0
            self.power_dbm = (
                round(power / POWER_RESOLUTION_DB) * POWER_RESOLUTION_DB
            )
            self.rf_on = bool(rf_on)

    def configure_sweep(self, **settings: Any) -> None:
        self._delay()
        start = _finite(settings["start_hz"], "扫频起点")
        end = _finite(settings["end_hz"], "扫频终点")
        step = _finite(settings["step_hz"], "扫频步长") if self._caps.is_blx else None
        upward = bool(settings["upward"])
        if not self._caps.minimum_frequency_hz <= start <= self._caps.maximum_frequency_hz:
            raise ValueError("扫频起点超出虚拟设备范围")
        if not self._caps.minimum_frequency_hz <= end <= self._caps.maximum_frequency_hz:
            raise ValueError("扫频终点超出虚拟设备范围")
        if step is not None and step <= 0:
            raise ValueError("扫频步长必须大于 0")
        if upward and start >= end:
            raise ValueError("向上扫频要求起点小于终点")
        if not upward and start <= end:
            raise ValueError("向下扫频要求起点大于终点")
        start_units, end_units = _frequency_units(start), _frequency_units(end)
        if (upward and start_units >= end_units) or (not upward and start_units <= end_units):
            raise ValueError("扫频起点和终点按 10 Hz 分辨率量化后必须保持扫描方向")
        if self._caps.is_blx:
            step_units = _frequency_units(step)
            if step_units < 1:
                raise ValueError("扫频步长按 10 Hz 分辨率量化后必须大于 0")
            dwell_ms = _milliseconds(settings["dwell_time_ms"], "扫频驻留时间", 10)
            idle_ms = _milliseconds(settings["idle_time_ms"], "扫频空闲时间", 0)
        else:
            duration_ms = _milliseconds(settings["sweep_time_ms"], "扫频时间", 1)
        self.sweep_start_hz = start_units * 10.0
        self.sweep_end_hz = end_units * 10.0
        if self._caps.is_blx:
            self.sweep_step_hz = step_units * 10.0
            self.dwell_time_ms, self.idle_time_ms = dwell_ms, idle_ms
        else:
            self.sweep_time_ms = duration_ms
        self.sweep_up = upward
        self.sweep_repeat = bool(settings["repeating"])
        self.sweep_bidirectional = bool(settings["bidirectional"])
        self.external_sweep_trigger = bool(settings["external_trigger"])

    def start_sweep(self, enabled: bool) -> None:
        self._delay()
        self.sweep_active = bool(enabled)

    def configure_pulse(self, **settings: Any) -> None:
        self._delay()
        on_time = _finite(settings["on_seconds"], "脉冲开启时间")
        period = _finite(settings["period_seconds"], "脉冲周期")
        if not 1e-7 <= on_time <= 1e3:
            raise ValueError("脉冲开启时间必须在 100 ns 至 1000 s 之间")
        if not 1e-7 <= period <= 1e3 or period < on_time:
            raise ValueError("脉冲周期必须不小于开启时间，且不超过 1000 s")
        self.pulse_on_seconds = on_time
        self.pulse_off_seconds = max(0.0, period - on_time)
        self.internal_pulse_enabled = bool(settings["enabled"])
        self.external_pulse_enabled = bool(settings["external_enabled"])

    def configure_chirp(self, **settings: Any) -> None:
        self._delay()
        length = _finite(settings["length_seconds"], "Chirp 长度")
        repeat = _finite(settings["repeat_seconds"], "Chirp 重复周期")
        if not 1e-6 <= length <= 50e-3:
            raise ValueError("Chirp 长度必须在 1 µs 至 50 ms 之间")
        if repeat < 10e-6:
            raise ValueError("Chirp 重复周期不能小于 10 µs")
        self.chirp_enabled = bool(settings["enabled"])
        self.chirp_length_seconds = length
        self.chirp_repeat_seconds = repeat
        self.external_sweep_trigger = bool(settings["external_trigger"])

    def set_sequence_element(
        self,
        index: int,
        frequency_hz: float,
        power_dbm: float,
        controls_power: bool,
    ) -> None:
        self._delay()
        if not 0 <= int(index) < MAX_SEQUENCE_ENTRIES:
            raise ValueError("序列索引必须在 0 至 49 之间")
        frequency = _finite(frequency_hz, "序列频率")
        power = validate_power_dbm(power_dbm, "序列功率")
        if not self._caps.minimum_frequency_hz <= frequency <= self._caps.maximum_frequency_hz:
            raise ValueError("序列频率超出虚拟设备范围")
        if not self._caps.minimum_power_dbm <= power <= self._caps.maximum_power_dbm:
            raise ValueError("序列功率超出虚拟设备范围")
        element = VaunixSequenceElement(
            round(frequency / 10.0) * 10.0,
            round(power / POWER_RESOLUTION_DB) * POWER_RESOLUTION_DB,
            bool(controls_power),
        )
        while len(self.sequence) <= index:
            self.sequence.append(VaunixSequenceElement(5e9, 0.0, True))
        self.sequence[index] = element

    def configure_sequence(
        self,
        elements: list[VaunixSequenceElement] | tuple[VaunixSequenceElement, ...],
        *,
        start_index: int,
        dwell_time_ms: int,
        idle_time_ms: int,
    ) -> None:
        self._delay()
        encoded, start, dwell_ms, idle_ms = _sequence_configuration(
            elements, self._caps, start_index, dwell_time_ms, idle_time_ms,
        )
        self.sequence = [VaunixSequenceElement(frequency * 10.0, power * POWER_ENCODING_DB, controls)
                         for frequency, power, controls in encoded]
        self.sequence_start = start
        self.sequence_dwell_ms = dwell_ms
        self.sequence_idle_ms = idle_ms

    def control_sequence(self, control: int) -> None:
        self._delay()
        if control not in {
            STOP_SEQUENCE,
            START_SEQUENCE,
            REPEAT_SEQUENCE,
            PAUSE_SEQUENCE,
            RESUME_SEQUENCE,
        }:
            raise ValueError("无效的序列控制命令")
        self.sequence_control = int(control)

    def save_settings(self) -> None:
        self._delay()
        self.saved_settings_count += 1

    def apply_step(self, parameter: StepParameter, channel: int, value: float) -> None:
        self.apply_steps((StepValue(parameter, channel, value),))

    def apply_steps(self, values: list[StepValue] | tuple[StepValue, ...]) -> None:
        with self._lock:
            commands = _step_commands(values, self._caps)
            for name, argument in commands:
                if name == "fnLMS_SetFrequencyEx":
                    self.set_frequency_hz(argument * 10.0)
                else:
                    self.set_power_dbm(argument * POWER_ENCODING_DB)

    def prepare_trigger(self) -> _VaunixTriggerWriter:
        with self._lock:
            if not self._connected:
                raise VaunixLmsError("虚拟 Vaunix 尚未连接")
            return _VaunixTriggerWriter(self, self._connection_generation)

    def _apply_trigger_steps(self, values, connection_generation: int) -> None:
        with self._lock:
            if connection_generation != self._connection_generation:
                raise VaunixLmsError("Vaunix 连接已经断开或重建，原触发任务已失效；请停止并重新布防")
            self.apply_steps(values)

    def output_snapshot(self) -> VaunixOutputSnapshot:
        with self._lock:
            snapshot = self.snapshot()
            return VaunixOutputSnapshot(
                connected=snapshot.connected, identity=snapshot.identity,
                capabilities=snapshot.capabilities, frequency_hz=snapshot.frequency_hz,
                power_dbm=snapshot.power_dbm, rf_on=snapshot.rf_on,
                internal_reference=snapshot.internal_reference,
                device_status=snapshot.device_status,
            )

    def snapshot(self) -> VaunixSnapshot:
        status = DEV_CONNECTED | PLL_LOCKED
        if self._connected:
            status |= DEV_OPENED
        if self.sweep_active:
            status |= SWP_ACTIVE
        if self.sweep_up:
            status |= SWP_UP
        if self.sweep_repeat:
            status |= SWP_REPEAT
        if self.sweep_bidirectional:
            status |= SWP_BIDIRECTIONAL
        status |= FAST_PULSE_OPTION
        return VaunixSnapshot(
            connected=self._connected,
            identity=self.identity,
            capabilities=self._caps,
            frequency_hz=self.frequency_hz,
            power_dbm=self.power_dbm,
            rf_on=self.rf_on,
            internal_reference=self.internal_reference,
            sweep_start_hz=self.sweep_start_hz,
            sweep_end_hz=self.sweep_end_hz,
            sweep_step_hz=self.sweep_step_hz,
            sweep_time_ms=self.sweep_time_ms,
            dwell_time_ms=self.dwell_time_ms,
            idle_time_ms=self.idle_time_ms,
            sweep_active=self.sweep_active,
            sweep_up=self.sweep_up,
            sweep_repeat=self.sweep_repeat,
            sweep_bidirectional=self.sweep_bidirectional,
            external_sweep_trigger=self.external_sweep_trigger,
            pulse_on_seconds=self.pulse_on_seconds,
            pulse_off_seconds=self.pulse_off_seconds,
            internal_pulse_enabled=self.internal_pulse_enabled,
            external_pulse_enabled=self.external_pulse_enabled,
            chirp_length_seconds=self.chirp_length_seconds,
            chirp_repeat_seconds=self.chirp_repeat_seconds,
            sequence_start=self.sequence_start,
            sequence_count=len(self.sequence),
            sequence_dwell_ms=self.sequence_dwell_ms,
            sequence_idle_ms=self.sequence_idle_ms,
            sequence=tuple(self.sequence),
            device_status=status,
        )


class _VaunixTriggerWriter:
    def __init__(self, device, connection_generation: int) -> None:
        self.device = device
        self.connection_generation = connection_generation

    def apply_steps(self, values) -> None:
        self.device._apply_trigger_steps(values, self.connection_generation)


def _milliseconds(value: int, label: str, minimum: int) -> int:
    try:
        result = max(minimum, int(value))
    except (ValueError, OverflowError, TypeError) as exc:
        raise ValueError(f"{label}必须是有限毫秒数") from exc
    if result > 0x7FFFFFFF:
        raise ValueError(f"{label}超出 SDK 32 位整数范围")
    return result


def _sequence_configuration(elements, capabilities, start_index, dwell_time_ms, idle_time_ms):
    if not capabilities.is_blx:
        raise VaunixLmsError("序列功能仅适用于 BLX 型号")
    normalized = tuple(elements)
    if not 1 <= len(normalized) <= MAX_SEQUENCE_ENTRIES:
        raise ValueError("序列必须包含 1 至 50 项")
    try:
        start = int(start_index)
    except (ValueError, OverflowError, TypeError) as exc:
        raise ValueError("序列起始索引必须为有限整数") from exc
    if not 0 <= start < len(normalized):
        raise ValueError("序列起始索引超出当前序列")
    dwell_ms = _milliseconds(dwell_time_ms, "序列驻留时间", 10)
    idle_ms = _milliseconds(idle_time_ms, "序列空闲时间", 0)
    encoded = []
    for element in normalized:
        frequency = _finite(element.frequency_hz, "序列频率")
        power = validate_power_dbm(element.power_dbm, "序列功率")
        if not capabilities.minimum_frequency_hz <= frequency <= capabilities.maximum_frequency_hz:
            raise ValueError("序列频率超出设备范围")
        if not capabilities.minimum_power_dbm <= power <= capabilities.maximum_power_dbm:
            raise ValueError("序列功率超出设备范围")
        encoded.append((_frequency_units(frequency), _power_units(power, "序列功率"), bool(element.controls_power)))
    return tuple(encoded), start, dwell_ms, idle_ms


def _step_commands(values, capabilities: VaunixCapabilities) -> tuple[tuple[str, int], ...]:
    normalized = tuple(values)
    if not normalized:
        raise ValueError("Vaunix 步进点至少需要一个参数")
    commands = []
    for item in normalized:
        if item.channel != 1:
            raise ValueError("Vaunix 只有一个射频输出，步进通道必须为 1")
        if item.parameter is StepParameter.FREQUENCY:
            frequency = _finite(item.value, "步进频率")
            if not capabilities.minimum_frequency_hz <= frequency <= capabilities.maximum_frequency_hz:
                raise ValueError("步进频率超出设备范围")
            commands.append(("fnLMS_SetFrequencyEx", _frequency_units(frequency)))
        elif item.parameter is StepParameter.AMPLITUDE:
            power = validate_power_dbm(item.value, "步进功率")
            if not capabilities.minimum_power_dbm <= power <= capabilities.maximum_power_dbm:
                raise ValueError("步进功率超出设备范围")
            commands.append(("fnLMS_SetPowerLevel", _power_units(power, "步进功率")))
        else:
            raise ValueError("Vaunix NI 联动仅支持频率和输出功率")
    return tuple(commands)


def _serial_from_resource(resource: str) -> int:
    parts = resource.strip().split("::")
    if len(parts) != 3 or parts[0].upper() != "VAUNIX" or parts[1].upper() != "LMS":
        raise ValueError("Vaunix 资源地址必须为 VAUNIX::LMS::<序列号>")
    try:
        serial = int(parts[2])
    except ValueError as exc:
        raise ValueError("Vaunix 资源地址中的序列号无效") from exc
    if serial <= 0:
        raise ValueError("Vaunix 序列号必须大于 0")
    return serial


def format_dll_version(value: int) -> str:
    """Format the SDK's 0xMMmm packed version (0x010F means 1.15)."""

    encoded = int(value) & 0xFFFF
    return f"{encoded >> 8}.{encoded & 0xFF}"


def _finite(value: float, label: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label}必须是有限值")
    return number


def _frequency_units(frequency_hz: float) -> int:
    units = int(round(_finite(frequency_hz, "频率") / 10.0))
    if not 0 <= units <= 0xFFFFFFFF:
        raise ValueError("频率超出 Vaunix 10 Hz 无符号整数范围")
    return units


def validate_power_dbm(power_dbm: float, label: str = "功率") -> float:
    """Require the 0.5 dB hardware grid while preserving 0.25 dB API encoding."""

    value = _finite(power_dbm, label)
    steps = value / POWER_RESOLUTION_DB
    if not math.isclose(steps, round(steps), rel_tol=0.0, abs_tol=1e-9):
        raise ValueError(
            f"{label}必须是 {POWER_RESOLUTION_DB:g} dB 的整数倍；"
            f"Vaunix DLL 虽以 {POWER_ENCODING_DB:g} dB 编码，但仪器分辨率为 "
            f"{POWER_RESOLUTION_DB:g} dB"
        )
    return value


def _power_units(power_dbm: float, label: str = "功率") -> int:
    value = validate_power_dbm(power_dbm, label)
    return int(round(value / POWER_ENCODING_DB))


def _unsigned_32(value: int) -> int:
    return ctypes.c_uint32(int(value)).value


def _raise_status(function_name: str, value: int) -> None:
    code = _unsigned_32(value)
    name = _STATUS_NAMES.get(code, f"0x{code:08X}")
    raise VaunixLmsError(f"{function_name} 返回 {name}")


def _check_set_status(function_name: str, value: int) -> None:
    code = _unsigned_32(value)
    if code == 0:
        return
    if code in _STATUS_NAMES or code & 0x80000000:
        _raise_status(function_name, value)
    raise VaunixLmsError(f"{function_name} 返回未知状态 0x{code:08X}")


def _raise_get_error(function_name: str, value: int) -> None:
    code = _unsigned_32(value)
    if code in _STATUS_NAMES:
        _raise_status(function_name, value)
