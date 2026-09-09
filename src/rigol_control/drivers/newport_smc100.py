"""SMC100CC ASCII protocol, with no automatic motion-command retries.

Resource: COM3::1 (port and controller address), or SIM::SMC100::1.
PR50CC positions are expressed in degrees after correct stage configuration.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from threading import RLock

VIRTUAL_SMC100_RESOURCE = "SIM::SMC100::1"


class MotionError(RuntimeError):
    pass


def finite(value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("运动参数必须是有限数值")
    return value


def parse_resource(resource: str) -> tuple[str, int]:
    match = re.fullmatch(r"(COM[1-9][0-9]*)::([1-9][0-9]?)", resource.strip(), re.I)
    if not match or not 1 <= int(match[2]) <= 31:
        raise ValueError("Newport 地址格式为 COM3::1（控制器地址 1–31）")
    return match[1].upper(), int(match[2])


@dataclass(frozen=True)
class MotionStatus:
    error: int
    state: int

    @property
    def ready(self) -> bool:
        return self.error == 0 and self.state in {0x32, 0x33, 0x34, 0x35}

    @property
    def moving(self) -> bool:
        return self.state in {0x1E, 0x1F, 0x28, 0x46, 0x47}

    def check(self) -> None:
        if self.error:
            raise MotionError(f"SMC100 运动错误 0x{self.error:04X}；状态 0x{self.state:02X}")


class Smc100Device:
    def __init__(self, resource: str, *, serial_factory=None) -> None:
        self.resource = resource
        self.port, self.address = parse_resource(resource)
        self._serial_factory = serial_factory
        self._serial = None
        self._lock = RLock()
        self.identity = ""
        self.firmware_revision = ""
        self.stage_model = ""
        self.stage_identifier = ""

    @property
    def is_connected(self) -> bool:
        serial = self._serial
        return serial is not None and serial.is_open

    def connect(self) -> str:
        with self._lock:
            if self.is_connected:
                return self.identity
            factory = self._serial_factory
            if factory is None:
                try:
                    from serial import Serial
                except ImportError as exc:
                    raise MotionError("未安装 pyserial；请安装项目 instrument 可选依赖") from exc
                factory = Serial
            try:
                self._serial = factory(port=self.port, baudrate=57600, bytesize=8,
                                       parity="N", stopbits=1, xonxoff=True,
                                       timeout=0.3, write_timeout=0.3)
                self._serial.reset_input_buffer()
                revision = self._query("VE")
                # VE identifies firmware, not the stage. Field firmware 3.1.2
                # uses SMC_CC; older versions use SMC100CC, SMC100 or SMC.
                # Match a complete known name so SMC_PP / SMC100PP stay rejected.
                if not re.match(r"(?:SMC_CC|SMC100CC|SMC100|SMC)(?=\s|[-:/]|$)", revision, re.I):
                    raise MotionError(f"无法识别为 SMC100CC 兼容控制器；VE 回包：{revision!r}")
                stage = self._query("ID?")
                # ID? may include factory metadata, e.g.
                # PR50CC_PN:B2210116_UD:01/10/2021. The model is the first
                # underscore-delimited token; retain the complete identifier.
                stage_model = stage.partition("_")[0].strip().upper()
                if stage_model != "PR50CC":
                    raise MotionError(
                        f"控制器已响应，但 ID? 返回标识 {stage!r}，"
                        f"解析型号 {stage_model!r}，预期 PR50CC；"
                        f"VE={revision!r}。请核对端口、控制器地址及原软件 Stage name。"
                    )
                self.firmware_revision = revision
                self.stage_model = stage_model
                self.stage_identifier = stage
                self.identity = f"{revision} / {stage}"
                return self.identity
            except Exception:
                self.disconnect()
                raise

    def disconnect(self) -> None:
        with self._lock:
            serial, self._serial = self._serial, None
            self.identity = ""
            self.firmware_revision = ""
            self.stage_model = ""
            self.stage_identifier = ""
            if serial is not None:
                serial.close()

    def _write(self, command: str) -> None:
        if not self.is_connected:
            raise MotionError("SMC100 未连接")
        payload = f"{self.address}{command}\r\n".encode("ascii")
        try:
            if self._serial.write(payload) != len(payload):
                raise MotionError("串口写入不完整；运动结果未知")
        except Exception as exc:
            self.disconnect()
            raise MotionError(f"SMC100 通信失败，未重发指令：{exc}") from exc

    def _query(self, command: str) -> str:
        with self._lock:
            self._write(command)
            try:
                raw = self._serial.read_until(b"\r\n", 512)
                if not raw.endswith(b"\r\n"):
                    raise MotionError("回包超时或不完整")
                reply = raw.decode("ascii").strip()
                prefix = f"{self.address}{command[:2]}"
                if not reply.startswith(prefix) or len(reply) == len(prefix):
                    raise MotionError(f"回包地址/命令不匹配：{reply!r}")
                return reply[len(prefix):].strip()
            except Exception as exc:
                self.disconnect()  # A late reply must never be used by the next query.
                raise MotionError(f"SMC100 查询失败：{exc}") from exc

    def _command(self, command: str) -> None:
        with self._lock:
            self._write(command)
            error = self._query("TE")
            if error != "@":
                raise MotionError(f"SMC100 拒绝 {command}：命令错误 {error}")

    def status(self) -> MotionStatus:
        reply = self._query("TS")
        if not re.fullmatch(r"[0-9A-Fa-f]{6}", reply):
            self.disconnect()
            raise MotionError(f"无效 TS 状态：{reply!r}")
        result = MotionStatus(int(reply[:4], 16), int(reply[4:], 16))
        result.check()  # TS consumes errors; never discard the error bits.
        return result

    def position(self) -> float:
        return finite(float(self._query("TP")))

    def limits(self) -> tuple[float, float]:
        return finite(float(self._query("SL?"))), finite(float(self._query("SR?")))

    def velocity(self) -> float:
        return finite(float(self._query("VA?")))

    def move_absolute(self, angle: float) -> None:
        angle = finite(angle)
        with self._lock:
            if not self.status().ready:
                raise MotionError("移动要求 READY 状态；请先完成归零")
            low, high = self.limits()
            if not low <= angle <= high:
                raise MotionError(f"目标 {angle}° 超出软件限位 [{low}, {high}]°")
            self._command(f"PA{angle:.9f}")

    def set_velocity(self, value: float) -> None:
        value = finite(value)
        if not 0 < value <= 20:
            raise ValueError("PR50CC 速度必须大于 0 且不超过 20°/s")
        with self._lock:
            if not self.status().ready:
                raise MotionError("请在归零完成且 READY 后设置速度")
            self._command(f"VA{value:.9f}")

    def home(self) -> None:
        with self._lock:
            state = self.status()
            if not 0x0A <= state.state <= 0x11:
                raise MotionError("归零仅允许在 NOT REFERENCED 状态执行；不会自动复位控制器")
            self._command("OR")

    def stop(self) -> None:
        with self._lock:
            self._command("ST")


class VirtualSmc100Device:
    """Timed motion (including long flyback), usable without serial drivers."""
    def __init__(self, resource: str = VIRTUAL_SMC100_RESOURCE) -> None:
        self.resource = resource
        self.identity = "SMC100CC VIRTUAL / PR50CC"
        self.is_connected = False
        self._lock = RLock()
        self._position = 0.0
        self._origin = 0.0
        self._target = 0.0
        self._started = 0.0
        self._duration = 0.0
        self._velocity = 20.0
        self.move_history: list[float] = []
        self.stop_count = 0
        self.error = 0

    def connect(self) -> str:
        self.is_connected = True
        return self.identity

    def disconnect(self) -> None:
        self.is_connected = False

    def _check(self) -> None:
        if not self.is_connected:
            raise MotionError("虚拟 SMC100 未连接")

    def position(self) -> float:
        with self._lock:
            self._check()
            if self._duration:
                fraction = min(1.0, (time.monotonic() - self._started) / self._duration)
                self._position = self._origin + fraction * (self._target - self._origin)
                if fraction == 1:
                    self._duration = 0.0
            return self._position

    def status(self) -> MotionStatus:
        with self._lock:
            self.position()
            result = MotionStatus(self.error, 0x28 if self._duration else 0x33)
            result.check()
            return result

    def limits(self) -> tuple[float, float]:
        self._check()
        return -360.0, 360.0

    def velocity(self) -> float:
        self._check()
        return self._velocity

    def set_velocity(self, value: float) -> None:
        value = finite(value)
        if not 0 < value <= 20 or not self.status().ready:
            raise MotionError("无效速度或设备未就绪")
        self._velocity = value

    def move_absolute(self, angle: float) -> None:
        with self._lock:
            angle = finite(angle)
            if not self.status().ready or not -360 <= angle <= 360:
                raise MotionError("设备未就绪或目标超限")
            self._origin = self.position()
            self._target = angle
            self._started = time.monotonic()
            self._duration = abs(angle - self._origin) / self._velocity + 0.02
            self.move_history.append(angle)

    def home(self) -> None:
        self.move_absolute(0.0)

    def stop(self) -> None:
        with self._lock:
            self._position = self.position()
            self._duration = 0.0
            self.stop_count += 1
