"""Trigger-paced cyclic rotation; P0 is held, first edge moves to P1."""
from __future__ import annotations

import csv
import json
import math
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock, Thread, current_thread

from .analog import AnalogAcquisitionState
from .drivers.newport_smc100 import MotionError


@dataclass(frozen=True)
class RotationScanConfig:
    step_deg: float = 0.2
    point_count: int = 10
    tolerance_deg: float = 0.01
    settle_seconds: float = 0.03
    motion_timeout_seconds: float = 10.0
    poll_seconds: float = 0.01
    initial_position_deg: float | None = None

    def validate(self) -> None:
        for value in (self.step_deg, self.tolerance_deg, self.settle_seconds,
                      self.motion_timeout_seconds, self.poll_seconds):
            if not math.isfinite(value):
                raise ValueError("扫描参数必须为有限数值")
        if self.initial_position_deg is not None and not math.isfinite(self.initial_position_deg):
            raise ValueError("初始位置 P0 必须为有限数值")
        if not 0 < abs(self.step_deg) < 1:
            raise ValueError("角度步长绝对值必须大于 0 且小于 1°")
        if isinstance(self.point_count, bool) or not isinstance(self.point_count, int) or not 2 <= self.point_count <= 10000:
            raise ValueError("位置数必须为 2–10000 的整数，包含 P0")
        if not 0 < self.tolerance_deg < abs(self.step_deg) / 2:
            raise ValueError("到位容差必须大于 0 且小于步长的一半")
        if not 0 <= self.settle_seconds < self.motion_timeout_seconds:
            raise ValueError("稳定时间必须非负且小于运动超时")
        if not 0.001 <= self.poll_seconds <= 0.1 or not 0 < self.motion_timeout_seconds <= 3600:
            raise ValueError("轮询周期或运动超时无效")

    def points(self, initial: float, limits: tuple[float, float]) -> tuple[float, ...]:
        self.validate()
        low, high = limits
        if not all(math.isfinite(x) for x in (initial, low, high)) or low >= high:
            raise ValueError("当前位置或软件限位无效")
        if self.initial_position_deg is not None:
            initial = self.initial_position_deg
        points = tuple(initial + i * self.step_deg for i in range(self.point_count))
        if any(not low <= p <= high for p in points):
            raise ValueError(f"扫描范围超出控制器软件限位 [{low}, {high}]°")
        return points


@dataclass(frozen=True)
class RotationSnapshot:
    phase: str = "stopped"
    received: int = 0
    completed: int = 0
    point_index: int = 0
    cycle: int = 0
    target_deg: float | None = None
    actual_deg: float | None = None
    elapsed_ms: float = 0.0
    error: str = ""


class RotationScanController:
    """One owner reserves both devices; AI is optional and never advances motion.

    Counts observed while moving/settling (or count jumps) stop the run rather
    than replaying motion later. Host observations are not hardware timestamps.
    """
    def __init__(self, device, counter, config: RotationScanConfig, *,
                 manager=None, device_ids=(), analog=None, run_directory=None,
                 metadata=None) -> None:
        config.validate()
        self.device, self.counter, self.config = device, counter, config
        self.manager, self.device_ids, self.analog = manager, tuple(device_ids), analog
        self.run_directory = Path(run_directory) if run_directory is not None else None
        self.metadata = dict(metadata or {})
        self.owner = f"rotation:{id(self)}"
        self._lock = RLock()
        self._log_lock = RLock()
        self._stop = Event()
        self._thread = None
        self._snapshot = RotationSnapshot()
        self._callbacks = []
        self._log = None
        self._stream = None
        self._counter_cleanup_pending = False
        self._analog_cleanup_pending = False
        self._motion_cleanup_pending = False
        self._leased = False
        self._cleanup_lock = RLock()
        self.points: tuple[float, ...] = ()
        if self.analog is not None:
            self.analog.add_callback(self._analog_observed)

    @property
    def is_worker_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_running(self) -> bool:
        return self.is_worker_running or self.cleanup_pending

    @property
    def cleanup_pending(self) -> bool:
        return self._counter_cleanup_pending or self._analog_cleanup_pending or self._motion_cleanup_pending

    def snapshot(self) -> RotationSnapshot:
        with self._lock:
            return self._snapshot

    def add_callback(self, callback) -> None:
        self._callbacks.append(callback)

    def _update(self, **changes) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, **changes)
            snap = self._snapshot
        for callback in self._callbacks:
            callback(snap)

    def start(self) -> None:
        if self.is_running:
            raise RuntimeError("旋转扫描已经运行")
        self._stop.clear()
        self._snapshot = RotationSnapshot(phase="preparing")
        self._thread = Thread(target=self._run, name="newport-rotation-scan", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()

    def stop(self, timeout: float = 10.0) -> None:
        self.request_stop()
        was_alive = self._thread is not None and self._thread.is_alive()
        if self._thread is not None and self._thread is not current_thread():
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise RuntimeError("扫描尚未停止，设备租约继续保留")
        if self.cleanup_pending:
            if not was_alive:
                with self._cleanup_lock:
                    errors = self._cleanup_remaining()
                    if errors:
                        self._update(phase="error", error="；".join(filter(None, [self.snapshot().error, *errors])))
                    self._release_if_clean()
            if self.cleanup_pending:
                raise RuntimeError("硬件任务停止未确认，设备租约继续保留；请重试停止")

    def _cleanup_counter(self) -> list[str]:
        if not self._counter_cleanup_pending:
            return []
        try:
            self.counter.stop()
            self._counter_cleanup_pending = False
            return []
        except Exception as exc:
            return [f"NI 计数停止失败：{exc}"]

    def _cleanup_analog(self) -> list[str]:
        if not self._analog_cleanup_pending:
            return []
        try:
            self.analog.stop()
            self._analog_cleanup_pending = False
            return []
        except Exception as exc:
            return [f"AI 停止失败：{exc}"]

    def _cleanup_motion(self) -> list[str]:
        if not self._motion_cleanup_pending:
            return []
        try:
            self.device.stop()
            deadline = time.monotonic() + 2.0
            while self.device.status().moving:
                if time.monotonic() >= deadline:
                    raise MotionError("停止后仍在运动")
                time.sleep(0.01)
            self._motion_cleanup_pending = False
            return []
        except Exception as exc:
            return [f"运动停止未确认，状态未知：{exc}"]

    def _cleanup_remaining(self) -> list[str]:
        return [*self._cleanup_counter(), *self._cleanup_motion(), *self._cleanup_analog()]

    def _release_if_clean(self) -> None:
        if self._leased and not self.cleanup_pending:
            self.manager.release_devices(self.owner)
            self._leased = False

    def _record(self, event: str, block=None) -> None:
        with self._log_lock:
            if self._log is not None:
                s = self.snapshot()
                self._log.writerow([datetime.now(timezone.utc).isoformat(), time.monotonic(),
                                    event, s.phase, s.received, s.completed, s.point_index,
                                    s.cycle, s.target_deg, s.actual_deg, s.elapsed_ms, s.error,
                                    block.first_sample_index if block is not None else "",
                                    block.sample_count if block is not None else "",
                                    block.sample_rate_hz if block is not None else ""])
                self._stream.flush()

    def _analog_observed(self, update) -> None:
        if update.block is not None:
            # A block can straddle multiple phases. This is its host receipt time,
            # not a claim that every sample was acquired at the logged angle.
            self._record("analog_block_received", update.block)

    def _count(self, previous: int) -> int:
        count = self.counter.read_count()
        self._update(received=count)
        if count < previous:
            raise MotionError("NI 累计计数回退，无法证明触发顺序；扫描已停止")
        return count

    def _check_analog(self) -> None:
        if self.analog is not None:
            s = self.analog.snapshot()
            if s.state is not AnalogAcquisitionState.RUNNING:
                raise MotionError(f"联动采集已停止或失败：{s.error or s.state.value}")

    def _wait_initial_stable(self, target: float) -> float | None:
        """Observe an explicitly selected P0 without commanding any motion.

        A short drift resets the continuous settling interval. No NI task is
        armed until the requested position is READY and has stayed in tolerance.
        """
        started = time.monotonic()
        stable_since = None
        while not self._stop.is_set():
            state = self.device.status()
            state.check()
            actual = self.device.position()
            if not math.isfinite(actual):
                raise MotionError("初始位置回读无效")
            now = time.monotonic()
            self._update(actual_deg=actual, elapsed_ms=(now - started) * 1000)
            if now - started > self.config.motion_timeout_seconds:
                raise MotionError("初始位置 P0 稳定等待超时；请先移动到初始位置并确认 READY")
            if state.ready and abs(actual - target) <= self.config.tolerance_deg:
                if stable_since is None:
                    stable_since = now
                if now - stable_since >= self.config.settle_seconds:
                    return actual
            else:
                stable_since = None
            self._stop.wait(self.config.poll_seconds)
        return None

    def _run(self) -> None:
        counter_started = False
        motion_active = False
        failure = ""
        try:
            if self.manager is not None:
                self.manager.acquire_devices(self.owner, self.device_ids)
                self._leased = True
            if self._stop.is_set():
                return
            if not self.device.status().ready:
                raise MotionError("启动要求 READY；请先手动完成归零，扫描不会自动移动到起点")
            initial = self.device.position()
            self.points = self.config.points(initial, self.device.limits())
            target = self.points[0]
            actual = initial
            self._update(target_deg=target, actual_deg=actual)
            if (self.config.initial_position_deg is not None
                    and abs(actual - target) > self.config.tolerance_deg):
                raise MotionError(
                    f"当前位置 {actual:.6f}° 未到初始位置 P0 {target:.6f}°"
                    f"（容差 {self.config.tolerance_deg:g}°）；请先移动到初始位置，再开始扫描"
                )
            velocity = self.device.velocity()
            if not math.isfinite(velocity) or velocity <= 0:
                raise MotionError("控制器速度回读无效")
            if self.config.initial_position_deg is not None:
                actual = self._wait_initial_stable(target)
                if actual is None:
                    return
            if self.run_directory is not None:
                self.run_directory.mkdir(parents=True, exist_ok=True)
                payload = {"schema_version": 1, "config": asdict(self.config),
                           "points_deg": self.points, "velocity_deg_s": velocity,
                           "initial_position_mode": "specified" if self.config.initial_position_deg is not None else "current",
                           "measured_initial_position_deg": initial,
                           "armed_position_deg": actual,
                           "identity": self.device.identity, "metadata": self.metadata,
                           "semantics": "start=P0; edge k=P[k mod N]; busy edge=error",
                           "timestamps": "host observations; not hardware edge timestamps",
                           "analog_alignment": "phase events are host observations, not sample-exact angle tags"}
                (self.run_directory / "config.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                self._stream = (self.run_directory / "rotation.csv").open("x", encoding="utf-8-sig", newline="")
                self._log = csv.writer(self._stream)
                self._log.writerow(["observed_utc", "host_monotonic_s", "event", "phase",
                                    "received", "completed", "point_index", "cycle",
                                    "target_deg", "actual_deg", "elapsed_ms", "error",
                                    "first_sample_index", "sample_count", "sample_rate_hz"])
            self._update(target_deg=target, actual_deg=actual, elapsed_ms=0.0)
            self._record("initial_position")
            if self._stop.is_set():
                return
            if self.analog is not None:
                self._analog_cleanup_pending = True
                self.analog.start()
                self._record("analog_started")
            if self._stop.is_set():
                return
            self._counter_cleanup_pending = True
            self.counter.start()
            counter_started = True
            accepted = 0
            # No initial count read-and-baseline: a first edge must not be discarded.
            self._update(phase="holding")
            self._record("armed_P0")
            stable_since = None
            started = 0.0
            while not self._stop.is_set():
                self._check_analog()
                phase = self.snapshot().phase
                count = self._count(accepted)
                if count != accepted:
                    if phase != "holding" or count != accepted + 1:
                        raise MotionError("触发过快：运动/稳定期间收到边沿或计数跳增；不补跑积压运动")
                    accepted = count
                    index = accepted % len(self.points)
                    target = self.points[index]
                    started = time.monotonic()
                    stable_since = None
                    self._update(phase="moving", point_index=index, cycle=accepted // len(self.points),
                                 target_deg=target, elapsed_ms=0.0)
                    self._record("trigger_observed")
                    if self._stop.is_set():
                        break
                    motion_active = True  # Includes an uncertain PA write outcome.
                    self._motion_cleanup_pending = True
                    self.device.move_absolute(target)
                    self._record("move_sent")
                state = self.device.status()
                state.check()
                actual = self.device.position()
                self._update(actual_deg=actual)
                phase = self.snapshot().phase
                if phase in {"moving", "settling"}:
                    now = time.monotonic()
                    self._update(elapsed_ms=(now - started) * 1000)
                    if now - started > self.config.motion_timeout_seconds:
                        raise MotionError("运动或稳定等待超时（包括末点回跳）")
                    if not state.ready and not state.moving:
                        raise MotionError(f"运动中控制器进入异常状态 0x{state.state:02X}")
                    if state.ready and abs(actual - self.snapshot().target_deg) <= self.config.tolerance_deg:
                        if stable_since is None:
                            stable_since = now
                            self._update(phase="settling")
                            self._record("in_position")
                        if now - stable_since >= self.config.settle_seconds:
                            # Conservatively reject edges arriving before readiness was observed.
                            if self._count(accepted) != accepted:
                                raise MotionError("稳定完成前收到新触发；不补跑积压运动")
                            if self._stop.is_set():
                                break
                            self._update(phase="holding", completed=accepted)
                            self._record("settled")
                            motion_active = False
                            self._motion_cleanup_pending = False
                    else:
                        stable_since = None
                        self._update(phase="moving")
                elif not state.ready or abs(actual - self.snapshot().target_deg) > self.config.tolerance_deg:
                    raise MotionError("保持期间位置或控制器状态发生变化")
                self._stop.wait(self.config.poll_seconds)
        except Exception as exc:
            failure = str(exc)
        finally:
            cleanup_errors = self._cleanup_counter()
            if motion_active or (failure and counter_started):
                self._motion_cleanup_pending = True
            cleanup_errors.extend(self._cleanup_motion())
            cleanup_errors.extend(self._cleanup_analog())
            failure = "；".join(filter(None, [failure, *cleanup_errors]))
            self._update(phase="error" if failure else "stopped", error=failure)
            try:
                self._record("error" if failure else "stopped")
            except Exception as exc:
                self._update(phase="error", error=f"{failure}；日志写入失败：{exc}".strip("；"))
            finally:
                try:
                    with self._log_lock:
                        if self._stream is not None:
                            self._stream.close()
                except Exception as exc:
                    self._update(phase="error", error=f"{failure}；日志关闭失败：{exc}".strip("；"))
                finally:
                    self._stream = self._log = None
                    self._release_if_clean()
