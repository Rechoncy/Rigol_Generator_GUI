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

    @property
    def not_referenced(self) -> bool:
        return self.error == 0 and 0x0A <= self.state <= 0x11

    @property
    def disabled(self) -> bool:
        return self.error == 0 and self.state in {0x3C, 0x3D, 0x3E}

    @property
    def can_home(self) -> bool:
        # 0x10 identifies an ESP stage/configuration error, even after a TS
        # read has consumed its error bits. It is not permission to send OR.
        return self.not_referenced and self.state != 0x10

    @property
    def description(self) -> str:
        if self.state == 0x10:
            label = "ESP 旋转台识别/配置错误"
        elif 0x0A <= self.state <= 0x11:
            label = "尚未建立机械参考（NOT REFERENCED）"
        elif self.state in {0x32, 0x33, 0x34, 0x35}:
            label = "已就绪（READY）"
        elif self.state in {0x3C, 0x3D, 0x3E}:
            label = "电机已禁用（DISABLE）"
        elif self.state in {0x1E, 0x1F}:
            label = "正在寻找机械原点（HOMING）"
        elif self.state == 0x28:
            label = "正在定位（MOVING）"
        elif self.state in {0x46, 0x47}:
            label = "正在连续微动（JOGGING）"
        elif self.state == 0x14:
            label = "控制器配置模式（CONFIGURATION）"
        else:
            label = "未识别的控制器状态"
        result = f"{label} · 状态 0x{self.state:02X}"
        return f"{result} · 硬件错误 0x{self.error:04X}" if self.error else result

    @property
    def guidance(self) -> str:
        if self.error:
            return "先核对控制器硬件错误；软件不会自动清除故障或重新执行运动。"
        if self.state == 0x10:
            return "请用厂商工具核对旋转台识别和配置，解决 ESP 错误后重新读取状态。"
        if self.ready:
            return "可以直接定位；若要转回 0°，将绝对目标设为 0°，无需再次寻找机械原点。"
        if self.disabled:
            return "可显式点击恢复使能；电机会重新励磁，随后回读 READY，再执行定位。"
        if self.can_home:
            return "先点击寻找机械原点，等完成并回读 READY 后再定位。"
        if self.state in {0x46, 0x47}:
            return "请按厂商流程用控制器面板或手持操作器退出连续微动（JOGGING），再读取状态；软件不会自动复位。"
        if self.moving:
            return "等待当前运动结束，或点击停止并确认停止后再操作。"
        if self.state == 0x14:
            return "请用厂商工具核对并退出配置模式，再重新读取状态；软件不会自动复位。"
        return "请重新读取状态，并根据控制器手册核对当前模式。"

    def check(self) -> None:
        if self.error:
            raise MotionError(f"SMC100 运动错误 0x{self.error:04X}；状态 0x{self.state:02X}")


class MotionStateError(MotionError):
    """A local state precondition refused an action before its command write."""

    def __init__(self, message: str, status: MotionStatus) -> None:
        self.status = status
        super().__init__(f"{message}：{status.description}。{status.guidance}")


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
            state = self.status()
            if not state.ready:
                raise MotionStateError("定位要求 READY 状态", state)
            low, high = self.limits()
            if not low <= angle <= high:
                raise MotionError(f"目标 {angle}° 超出软件限位 [{low}, {high}]°")
            self._command(f"PA{angle:.9f}")

    def set_velocity(self, value: float) -> None:
        value = finite(value)
        if not 0 < value <= 20:
            raise ValueError("PR50CC 速度必须大于 0 且不超过 20°/s")
        with self._lock:
            state = self.status()
            if not state.ready:
                raise MotionStateError("设置速度要求 READY 状态", state)
            self._command(f"VA{value:.9f}")

    def home(self) -> None:
        with self._lock:
            state = self.status()
            if not state.can_home:
                raise MotionStateError("当前状态不允许寻找机械原点", state)
            self._command("OR")

    def enable_motor(self) -> MotionStatus:
        """Explicitly leave DISABLE using addressed MM1; never reset or home.

        SMC100CC/PP manual EDH0206En2060, MM section (printed page 52): MM1
        sets the set point to the current position and energizes the motor.
        No retry is safe after an uncertain write. TS must confirm READY.
        """
        with self._lock:
            state = self.status()
            if state.ready:
                return state
            if not state.disabled:
                raise MotionStateError("恢复使能仅允许在 DISABLE 状态执行", state)
            self._command("MM1")
            result = self.status()
            if not result.ready:
                # A command was sent; this is not a local precondition failure.
                raise MotionError(f"恢复使能命令已发送，但未确认 READY：{result.description}。{result.guidance}")
            return result

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
        self.enable_count = 0
        self.home_count = 0
        self.state = 0x33
        self._motion_state = 0x28
        self._completed_state = 0x33
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
                    self.state = self._completed_state
            return self._position

    def status(self) -> MotionStatus:
        with self._lock:
            self.position()
            result = MotionStatus(self.error, self._motion_state if self._duration else self.state)
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
        if not 0 < value <= 20:
            raise MotionError("无效速度")
        with self._lock:
            state = self.status()
            if not state.ready:
                raise MotionStateError("设置速度要求 READY 状态", state)
            self._velocity = value

    def move_absolute(self, angle: float) -> None:
        with self._lock:
            angle = finite(angle)
            state = self.status()
            if not state.ready:
                raise MotionStateError("定位要求 READY 状态", state)
            if not -360 <= angle <= 360:
                raise MotionError("目标超限")
            self._origin = self.position()
            self._target = angle
            self._started = time.monotonic()
            self._duration = abs(angle - self._origin) / self._velocity + 0.02
            self._motion_state = 0x28
            self._completed_state = 0x33
            self.move_history.append(angle)

    def home(self) -> None:
        with self._lock:
            state = self.status()
            if not state.can_home:
                raise MotionStateError("当前状态不允许寻找机械原点", state)
            self._origin = self.position()
            self._target = 0.0
            self._started = time.monotonic()
            self._duration = abs(self._origin) / self._velocity + 0.02
            self._motion_state = 0x1E
            self._completed_state = 0x32
            self.home_count += 1

    def enable_motor(self) -> MotionStatus:
        with self._lock:
            state = self.status()
            if state.ready:
                return state
            if not state.disabled:
                raise MotionStateError("恢复使能仅允许在 DISABLE 状态执行", state)
            self.state = 0x34
            self.enable_count += 1
            return self.status()

    def set_virtual_state(self, state: int, *, error: int = 0) -> None:
        """Inject a controller state for simulation; never sends hardware I/O."""
        if not isinstance(state, int) or not 0 <= state <= 0xFF:
            raise ValueError("虚拟状态必须为 0x00–0xFF")
        if not isinstance(error, int) or not 0 <= error <= 0xFFFF:
            raise ValueError("虚拟错误必须为 0x0000–0xFFFF")
        with self._lock:
            if self.is_connected:
                self.position()
            self._duration = 0.0
            self.state, self.error = state, error

    def stop(self) -> None:
        with self._lock:
            self._check()
            state = self._motion_state if self._duration else self.state
            if 0x0A <= state <= 0x11 or state == 0x14 or state in {0x46, 0x47}:
                # Match the controller's ST command restrictions. An idle,
                # unreferenced/configuration state can instead be confirmed
                # with TS; pretending ST succeeded would hide a recovery loop.
                MotionStatus(self.error, state).check()
                code = "H" if 0x0A <= state <= 0x11 else "I" if state == 0x14 else "D"
                raise MotionError(f"虚拟 SMC100 拒绝 ST：命令错误 {code}；状态 0x{state:02X}")
            self._position = self.position()
            state = self._motion_state if self._duration else self.state
            self._duration = 0.0
            if state in {0x1E, 0x1F}:
                self.state = 0x0B
            elif state == 0x28:
                self.state = 0x33
            self.stop_count += 1
