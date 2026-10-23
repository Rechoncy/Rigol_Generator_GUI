from __future__ import annotations

import csv
import math
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import Condition, Event, RLock, Thread
from typing import Protocol

from .controller_lifecycle import ControllerLifecycle
from .drivers.dg4000 import StepParameter, StepValue
from .poll_waiter import wait_for_poll
from .scan import MAX_SCAN_AXES, ScanMode
from .sequence import CyclicSequence, MAX_SCAN_POINTS, linear_scan_points
from .trigger import ControllerState, EdgeCounter
from .units import to_base, unit_spec


@dataclass(frozen=True, slots=True)
class LinkageAxis:
    """One scan axis addressed to an explicit generator device."""

    device_id: str
    parameter: StepParameter
    channel: int
    unit_key: str
    start: float
    step: float
    count: int

    @property
    def target(self) -> tuple[str, int, StepParameter]:
        return self.device_id, self.channel, self.parameter

    def validate(self) -> None:
        if not self.device_id.strip():
            raise ValueError("扫描轴必须指定 DG4000 设备")
        if self.channel not in (1, 2):
            raise ValueError("扫描轴通道只能是 1 或 2")
        unit_spec(self.parameter.unit_family, self.unit_key)
        if not math.isfinite(float(self.start)) or not math.isfinite(float(self.step)):
            raise ValueError("扫描轴起点和步长必须是有限数值")
        if self.count < 1 or self.count > MAX_SCAN_POINTS:
            raise ValueError(f"单轴点数必须在 1 至 {MAX_SCAN_POINTS} 之间")


@dataclass(frozen=True, slots=True)
class LinkageStepValue:
    device_id: str
    parameter: StepParameter
    channel: int
    value: float
    display_value: float
    unit_key: str

    def as_step_value(self) -> StepValue:
        return StepValue(
            parameter=self.parameter,
            channel=self.channel,
            value=self.value,
            display_value=self.display_value,
            unit_key=self.unit_key,
        )


@dataclass(frozen=True, slots=True)
class LinkagePoint:
    values: tuple[LinkageStepValue, ...]

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError("跨设备联合点至少包含一个参数")

    def grouped(self) -> dict[str, tuple[StepValue, ...]]:
        groups: dict[str, list[StepValue]] = defaultdict(list)
        for item in self.values:
            groups[item.device_id].append(item.as_step_value())
        return {device_id: tuple(values) for device_id, values in groups.items()}


def validate_linkage_axes(
    axes: tuple[LinkageAxis, ...] | list[LinkageAxis],
) -> tuple[LinkageAxis, ...]:
    normalized = tuple(axes)
    if not normalized:
        raise ValueError("至少需要一个跨设备扫描轴")
    if len(normalized) > MAX_SCAN_AXES:
        raise ValueError(f"扫描轴不能超过 {MAX_SCAN_AXES} 个")
    targets: set[tuple[str, int, StepParameter]] = set()
    for index, axis in enumerate(normalized, start=1):
        try:
            axis.validate()
        except ValueError as exc:
            raise ValueError(f"扫描轴 {index}：{exc}") from exc
        if axis.target in targets:
            raise ValueError(f"扫描轴 {index} 与已有轴重复控制同一设备、通道和参数")
        targets.add(axis.target)
    return normalized


def linkage_point_count(
    axes: tuple[LinkageAxis, ...] | list[LinkageAxis],
    mode: ScanMode,
    *,
    maximum: int = MAX_SCAN_POINTS,
) -> int:
    normalized = validate_linkage_axes(axes)
    if mode is ScanMode.SYNCHRONOUS:
        counts = {axis.count for axis in normalized}
        if len(counts) != 1:
            raise ValueError("同步联动要求所有扫描轴的点数相同")
        total = normalized[0].count
    else:
        total = math.prod(axis.count for axis in normalized)
    if total > maximum:
        raise ValueError(f"跨设备联合点数 {total} 超过上限 {maximum}")
    return total


def _axis_values(axis: LinkageAxis) -> tuple[LinkageStepValue, ...]:
    return tuple(
        LinkageStepValue(
            device_id=axis.device_id,
            parameter=axis.parameter,
            channel=axis.channel,
            value=to_base(value, axis.parameter.unit_family, axis.unit_key),
            display_value=value,
            unit_key=axis.unit_key,
        )
        for value in linear_scan_points(axis.start, axis.step, axis.count)
    )


def generate_linkage_points(
    axes: tuple[LinkageAxis, ...] | list[LinkageAxis],
    mode: ScanMode,
    *,
    maximum: int = MAX_SCAN_POINTS,
) -> tuple[LinkagePoint, ...]:
    normalized = validate_linkage_axes(axes)
    total = linkage_point_count(normalized, mode, maximum=maximum)
    series = tuple(_axis_values(axis) for axis in normalized)
    if mode is ScanMode.SYNCHRONOUS:
        return tuple(
            LinkagePoint(tuple(values[index] for values in series))
            for index in range(total)
        )

    from itertools import product

    return tuple(LinkagePoint(tuple(values)) for values in product(*series))


class StepBatchWriter(Protocol):
    def apply_steps(self, values: list[StepValue] | tuple[StepValue, ...]) -> None: ...


class DispatchMode(str, Enum):
    STRICT_SERIAL = "strict_serial"

    @property
    def label(self) -> str:
        return "严格顺序下发（推荐）"


@dataclass(frozen=True, slots=True)
class DeviceApplyResult:
    device_id: str
    started_at: float
    finished_at: float
    success: bool
    error: str | None = None

    @property
    def elapsed_ms(self) -> float:
        return max(0.0, (self.finished_at - self.started_at) * 1000.0)


@dataclass(frozen=True, slots=True)
class LinkageSnapshot:
    state: ControllerState
    received_count: int
    applied_count: int
    current_index: int | None
    error: str | None
    device_results: tuple[DeviceApplyResult, ...] = ()
    revision: int = 0

    @property
    def backlog(self) -> int:
        return max(0, self.received_count - self.applied_count)

    @property
    def skew_ms(self) -> float:
        completed = [item.finished_at for item in self.device_results if item.success]
        return (max(completed) - min(completed)) * 1000.0 if len(completed) > 1 else 0.0


LinkageUpdateCallback = Callable[[LinkageSnapshot], None]


class LinkageRunLogger:
    """Append one durable CSV row after every completed or failed joint point."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = RLock()

    def write(
        self,
        *,
        trigger_number: int,
        point_index: int,
        dispatch_started_at: float,
        results: tuple[DeviceApplyResult, ...],
        status: str,
        error: str | None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        completed = [item.finished_at for item in results if item.success]
        finished_at = max((item.finished_at for item in results), default=dispatch_started_at)
        skew_ms = (max(completed) - min(completed)) * 1000.0 if len(completed) > 1 else 0.0
        device_detail = "; ".join(
            f"{item.device_id}:{'OK' if item.success else 'ERROR'}:{item.elapsed_ms:.3f}ms"
            for item in results
        )
        with self._lock, self.path.open("a", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            if handle.tell() == 0:
                writer.writerow(
                    [
                        "trigger_number",
                        "point_index",
                        "dispatch_started_utc",
                        "finished_utc",
                        "elapsed_ms",
                        "skew_ms",
                        "status",
                        "device_results",
                        "error",
                    ]
                )
            writer.writerow(
                [
                    trigger_number,
                    point_index + 1,
                    datetime.fromtimestamp(dispatch_started_at, timezone.utc).isoformat(),
                    datetime.fromtimestamp(finished_at, timezone.utc).isoformat(),
                    f"{max(0.0, finished_at - dispatch_started_at) * 1000.0:.3f}",
                    f"{skew_ms:.3f}",
                    status,
                    device_detail,
                    error or "",
                ]
            )


class MultiDeviceTriggerController(ControllerLifecycle):
    """Map cumulative NI edges to ordered multi-generator joint points.

    The first edge applies P0. A point is counted as applied only after every
    participating generator has completed its device-local SCPI batch. There is
    deliberately no minimum trigger interval; cumulative edges form a backlog.
    """

    def __init__(
        self,
        counter: EdgeCounter,
        writers: Mapping[str, StepBatchWriter],
        points: list[LinkagePoint] | tuple[LinkagePoint, ...],
        *,
        device_order: list[str] | tuple[str, ...] | None = None,
        dispatch_mode: DispatchMode = DispatchMode.STRICT_SERIAL,
        monitor_poll_seconds: float = 0.002,
        logger: LinkageRunLogger | None = None,
    ) -> None:
        normalized_points = tuple(points)
        if not normalized_points:
            raise ValueError("跨设备联动任务至少需要一个联合点")
        if not math.isfinite(monitor_poll_seconds) or monitor_poll_seconds <= 0:
            raise ValueError("累计计数读取周期必须大于 0")
        referenced = {item.device_id for point in normalized_points for item in point.values}
        missing = sorted(referenced.difference(writers))
        if missing:
            raise ValueError(f"缺少设备写入器：{', '.join(missing)}")
        order = tuple(writers.keys() if device_order is None else device_order)
        if len(order) != len(set(order)) or set(order) != set(writers):
            raise ValueError("设备下发顺序必须与写入器集合一致")

        self.counter = counter
        self.writers = dict(writers)
        self._active_writers = dict(writers)
        self.sequence = CyclicSequence(normalized_points)
        self.device_order = order
        self.dispatch_mode = dispatch_mode
        self.monitor_poll_seconds = monitor_poll_seconds
        self.logger = logger

        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._stop_event = Event()
        self._cancel_before_start = False
        self._state = ControllerState.STOPPED
        self._received_count = 0
        self._applied_count = 0
        self._current_index: int | None = None
        self._error: str | None = None
        self._device_results: tuple[DeviceApplyResult, ...] = ()
        self._callbacks: list[LinkageUpdateCallback] = []
        self._snapshot_revision = 0
        self._monitor_thread: Thread | None = None
        self._writer_thread: Thread | None = None
        self._init_lifecycle()

    def add_callback(self, callback: LinkageUpdateCallback) -> None:
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def snapshot(self) -> LinkageSnapshot:
        with self._lock:
            self._snapshot_revision += 1
            return LinkageSnapshot(
                state=self._state,
                received_count=self._received_count,
                applied_count=self._applied_count,
                current_index=self._current_index,
                error=self._error,
                device_results=self._device_results,
                revision=self._snapshot_revision,
            )

    def _start_run(self) -> None:
        with self._condition:
            if self._cancel_before_start or self._stop_event.is_set():
                self._cancel_before_start = False
                self._state = ControllerState.STOPPED
                self._publish()
                return
            self._received_count = 0
            self._applied_count = 0
            self._current_index = None
            self._error = None
            self._device_results = ()
        self._publish()
        try:
            self._active_writers = {}
            for device_id, writer in self.writers.items():
                if self._stop_event.is_set():
                    break
                prepare = getattr(writer, "prepare_trigger", None)
                self._active_writers[device_id] = prepare() if callable(prepare) else writer
            with self._condition:
                if self._stop_event.is_set():
                    self._state = ControllerState.STOPPED
                    self._publish()
                    return
            self._open_counter()
        except Exception as exc:
            self._fail(f"启动 NI 累计计数器失败：{exc}")
            raise

        with self._condition:
            cancelled = self._stop_event.is_set()
            if not cancelled:
                self._start_workers_locked()
        if cancelled:
            self._close_counter()
            with self._condition:
                self._state = ControllerState.STOPPED
        self._publish()

    def _start_workers_locked(self) -> None:
        self._state = ControllerState.RUNNING
        self._monitor_thread = Thread(
            target=self._monitor_loop, name="multi-device-ni-monitor", daemon=True
        )
        self._writer_thread = Thread(
            target=self._writer_loop, name="multi-device-scpi-coordinator", daemon=True
        )
        monitor = self._monitor_thread
        writer = self._writer_thread
        monitor.start()
        writer.start()

    def _monitor_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                count = int(self.counter.read_count())
                changed = False
                with self._condition:
                    if count < self._received_count:
                        raise RuntimeError("NI 累计计数发生回退，无法保持触发顺序")
                    if count > self._received_count:
                        self._received_count = count
                        self._condition.notify_all()
                        changed = True
                if changed:
                    self._publish()
                wait_for_poll(self._stop_event, self.monitor_poll_seconds)
        except Exception as exc:
            self._fail(f"监视 NI 触发失败：{exc}")

    def _writer_loop(self) -> None:
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._stop_event.is_set()
                        or self._applied_count < self._received_count
                    )
                    if self._stop_event.is_set():
                        return
                    trigger_number = self._applied_count + 1
                    position = self.sequence.for_trigger(trigger_number)
                point = position.value
                started = time.time()
                results, failure, cancelled = self._dispatch_serial(point)
                status = ("partial_stopped" if cancelled else
                          "complete" if failure is None else "partial_error")
                # Device I/O has already happened. A disk failure must not
                # erase completed physical writes or hide partial results.
                with self._condition:
                    self._device_results = results
                    if failure is None and not cancelled:
                        self._applied_count = trigger_number
                        self._current_index = position.index
                self._publish()
                if self.logger is not None:
                    try:
                        self.logger.write(
                            trigger_number=trigger_number,
                            point_index=position.index,
                            dispatch_started_at=started,
                            results=results,
                            status=status,
                            error="收到停止请求，未继续下发其余设备" if cancelled else failure,
                        )
                    except Exception as exc:
                        outcome = ("收到停止请求，本点仅部分执行" if cancelled else
                                   f"本点部分下发失败：{failure}" if failure is not None else
                                   "本点全部设备已完成下发")
                        self._fail(f"联合点 #{position.index + 1} {outcome}；"
                                   f"运行日志写入失败：{exc}；已停止后续点，请勿自动重放")
                        return
                if cancelled:
                    return
                if failure is not None:
                    self._fail(
                        f"联合点 #{position.index + 1} 部分下发失败；已停止：{failure}"
                    )
                    return
        except Exception as exc:
            self._fail(f"跨设备联合点下发失败：{exc}")

    def _dispatch_serial(
        self, point: LinkagePoint
    ) -> tuple[tuple[DeviceApplyResult, ...], str | None, bool]:
        grouped = point.grouped()
        results: list[DeviceApplyResult] = []
        for device_id in self.device_order:
            values = grouped.get(device_id)
            if not values:
                continue
            if self._stop_event.is_set():
                return tuple(results), None, True
            started = time.time()
            try:
                self._active_writers[device_id].apply_steps(values)
            except Exception as exc:
                finished = time.time()
                results.append(DeviceApplyResult(device_id, started, finished, False, str(exc)))
                return tuple(results), f"设备 {device_id}：{exc}", False
            finished = time.time()
            results.append(DeviceApplyResult(device_id, started, finished, True))
        return tuple(results), None, False

    def _fail(self, message: str) -> None:
        with self._condition:
            if self._state is ControllerState.ERROR and self._error:
                return
            self._error = message
            self._state = ControllerState.ERROR
            self._stop_event.set()
            self._condition.notify_all()
        try:
            self._close_counter()
        except Exception:
            pass
        self._publish()

    def _publish(self) -> None:
        snapshot = self.snapshot()
        with self._lock:
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            try:
                callback(snapshot)
            except Exception:
                continue
