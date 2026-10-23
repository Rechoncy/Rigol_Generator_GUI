from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from threading import Condition, Event, RLock, Thread
from typing import Protocol

from .controller_lifecycle import ControllerLifecycle
from .drivers.dg4000 import StepParameter, StepValue
from .poll_waiter import wait_for_poll
from .scan import ScanPoint
from .sequence import CyclicSequence


class EdgeCounter(Protocol):
    def start(self) -> None: ...

    def read_count(self) -> int: ...

    def stop(self) -> None: ...


class StepWriter(Protocol):
    def apply_step(self, parameter: StepParameter, channel: int, value: float) -> None: ...

    def apply_steps(self, values: list[StepValue] | tuple[StepValue, ...]) -> None: ...


class ControllerState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class TriggerSnapshot:
    state: ControllerState
    received_count: int
    applied_count: int
    current_index: int | None
    current_value: float | None
    error: str | None
    current_values: tuple[StepValue, ...] = ()
    revision: int = 0

    @property
    def backlog(self) -> int:
        return max(0, self.received_count - self.applied_count)


UpdateCallback = Callable[[TriggerSnapshot], None]


class TriggerStepController(ControllerLifecycle):
    """Two-thread hardware-count-to-SCPI controller with lossless ordering.

    The monitor only samples a cumulative hardware counter. Its poll cadence is
    not a trigger filter or minimum interval: multiple edges between two reads
    appear as a count jump and every missing sequence number is then applied by
    the writer thread.
    """

    def __init__(
        self,
        counter: EdgeCounter,
        writer: StepWriter,
        parameter: StepParameter | None = None,
        channel: int | None = None,
        points: list[float] | tuple[float, ...] | None = None,
        *,
        scan_points: list[ScanPoint] | tuple[ScanPoint, ...] | None = None,
        monitor_poll_seconds: float = 0.002,
    ) -> None:
        if not math.isfinite(monitor_poll_seconds) or monitor_poll_seconds <= 0:
            raise ValueError("累计计数读取周期必须大于 0")
        self.counter = counter
        self.writer = writer
        self._active_writer = writer
        self.parameter = parameter
        self.channel = channel
        if scan_points is not None:
            if parameter is not None or channel is not None or points is not None:
                raise ValueError("联合扫描点不能与旧式单轴参数同时指定")
            normalized_points = tuple(scan_points)
        else:
            if parameter is None or channel is None or points is None:
                raise ValueError("单轴触发步进必须指定参数、通道和点表")
            normalized_points = tuple(
                ScanPoint((StepValue(parameter, channel, float(value)),)) for value in points
            )
        self.sequence = CyclicSequence(normalized_points)
        self.monitor_poll_seconds = monitor_poll_seconds

        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._stop_event = Event()
        self._cancel_before_start = False
        self._state = ControllerState.STOPPED
        self._received_count = 0
        self._applied_count = 0
        self._current_index: int | None = None
        self._current_value: float | None = None
        self._current_values: tuple[StepValue, ...] = ()
        self._error: str | None = None
        self._callbacks: list[UpdateCallback] = []
        self._snapshot_revision = 0
        self._monitor_thread: Thread | None = None
        self._writer_thread: Thread | None = None
        self._init_lifecycle()

    def add_callback(self, callback: UpdateCallback) -> None:
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def remove_callback(self, callback: UpdateCallback) -> None:
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    def snapshot(self) -> TriggerSnapshot:
        with self._lock:
            self._snapshot_revision += 1
            return TriggerSnapshot(
                state=self._state,
                received_count=self._received_count,
                applied_count=self._applied_count,
                current_index=self._current_index,
                current_value=self._current_value,
                error=self._error,
                current_values=self._current_values,
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
            self._current_value = None
            self._current_values = ()
            self._error = None
        self._publish()
        try:
            prepare = getattr(self.writer, "prepare_trigger", None)
            self._active_writer = prepare() if callable(prepare) else self.writer
            with self._condition:
                if self._stop_event.is_set():
                    self._state = ControllerState.STOPPED
                    self._publish()
                    return
            self._open_counter()
        except Exception as exc:
            self._fail(f"启动前检查或硬件计数器启动失败：{exc}")
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
            target=self._monitor_loop,
            name="ni-trigger-monitor",
            daemon=True,
        )
        self._writer_thread = Thread(
            target=self._writer_loop,
            name="instrument-step-writer",
            daemon=True,
        )
        monitor = self._monitor_thread
        writer = self._writer_thread
        # Stop cannot join these threads between creation and start.
        monitor.start()
        writer.start()

    def _monitor_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                count = int(self.counter.read_count())
                changed = False
                with self._condition:
                    if count < self._received_count:
                        raise RuntimeError("NI 累计计数发生回退，任务无法保持触发序号")
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
                values = position.value.values
                apply_many = getattr(self._active_writer, "apply_steps", None)
                if callable(apply_many):
                    apply_many(values)
                else:
                    for item in values:
                        self._active_writer.apply_step(item.parameter, item.channel, item.value)
                with self._condition:
                    # Only the sole writer thread mutates applied_count.
                    self._applied_count = trigger_number
                    self._current_index = position.index
                    self._current_value = values[0].value
                    self._current_values = values
                self._publish()
        except Exception as exc:
            self._fail(f"向目标设备下发步进参数失败：{exc}")

    def _fail(self, message: str) -> None:
        with self._condition:
            self._error = message
            self._state = ControllerState.ERROR
            self._stop_event.set()
            self._condition.notify_all()
        try:
            self._close_counter()
        except Exception:
            pass  # Keep _counter_open set so the lease cannot be released.
        self._publish()

    def _publish(self) -> None:
        snapshot = self.snapshot()
        with self._lock:
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            try:
                callback(snapshot)
            except Exception:
                # Observers must never stop hardware counting or SCPI delivery.
                continue

    def __enter__(self) -> "TriggerStepController":
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop()
