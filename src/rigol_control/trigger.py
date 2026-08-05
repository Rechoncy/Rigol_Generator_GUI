from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from threading import Condition, Event, RLock, Thread, current_thread
from typing import Protocol

from .drivers.dg4000 import StepParameter
from .sequence import CyclicSequence


class EdgeCounter(Protocol):
    def start(self) -> None: ...

    def read_count(self) -> int: ...

    def stop(self) -> None: ...


class StepWriter(Protocol):
    def apply_step(self, parameter: StepParameter, channel: int, value: float) -> None: ...


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

    @property
    def backlog(self) -> int:
        return max(0, self.received_count - self.applied_count)


UpdateCallback = Callable[[TriggerSnapshot], None]


class TriggerStepController:
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
        parameter: StepParameter,
        channel: int,
        points: list[float] | tuple[float, ...],
        *,
        monitor_poll_seconds: float = 0.002,
    ) -> None:
        if monitor_poll_seconds <= 0:
            raise ValueError("累计计数读取周期必须大于 0")
        self.counter = counter
        self.writer = writer
        self.parameter = parameter
        self.channel = channel
        self.sequence = CyclicSequence(points)
        self.monitor_poll_seconds = monitor_poll_seconds

        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._stop_event = Event()
        self._state = ControllerState.STOPPED
        self._received_count = 0
        self._applied_count = 0
        self._current_index: int | None = None
        self._current_value: float | None = None
        self._error: str | None = None
        self._callbacks: list[UpdateCallback] = []
        self._monitor_thread: Thread | None = None
        self._writer_thread: Thread | None = None

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
            return TriggerSnapshot(
                state=self._state,
                received_count=self._received_count,
                applied_count=self._applied_count,
                current_index=self._current_index,
                current_value=self._current_value,
                error=self._error,
            )

    def start(self) -> None:
        with self._condition:
            if self._state in {ControllerState.STARTING, ControllerState.RUNNING}:
                raise RuntimeError("触发步进任务已经运行")
            self._state = ControllerState.STARTING
            self._received_count = 0
            self._applied_count = 0
            self._current_index = None
            self._current_value = None
            self._error = None
            self._stop_event.clear()
        self._publish()
        try:
            self.counter.start()
        except Exception as exc:
            self._fail(f"启动硬件计数器失败：{exc}")
            raise

        with self._condition:
            self._state = ControllerState.RUNNING
            self._monitor_thread = Thread(
                target=self._monitor_loop,
                name="ni-trigger-monitor",
                daemon=True,
            )
            self._writer_thread = Thread(
                target=self._writer_loop,
                name="dg4000-scpi-writer",
                daemon=True,
            )
            monitor = self._monitor_thread
            writer = self._writer_thread
        monitor.start()
        writer.start()
        self._publish()

    def stop(self) -> None:
        with self._condition:
            if self._state is ControllerState.STOPPED:
                return
            if self._state is not ControllerState.ERROR:
                self._state = ControllerState.STOPPING
            self._stop_event.set()
            self._condition.notify_all()
            monitor = self._monitor_thread
            writer = self._writer_thread
        self._publish()
        for thread in (monitor, writer):
            if thread is not None and thread is not current_thread():
                thread.join(timeout=3.0)
        try:
            self.counter.stop()
        finally:
            with self._condition:
                if self._state is not ControllerState.ERROR:
                    self._state = ControllerState.STOPPED
                self._monitor_thread = None
                self._writer_thread = None
            self._publish()

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
                self._stop_event.wait(self.monitor_poll_seconds)
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
                self.writer.apply_step(
                    self.parameter,
                    self.channel,
                    float(position.value),
                )
                with self._condition:
                    # Only the sole writer thread mutates applied_count.
                    self._applied_count = trigger_number
                    self._current_index = position.index
                    self._current_value = float(position.value)
                self._publish()
        except Exception as exc:
            self._fail(f"向 DG4000 下发步进参数失败：{exc}")

    def _fail(self, message: str) -> None:
        with self._condition:
            self._error = message
            self._state = ControllerState.ERROR
            self._stop_event.set()
            self._condition.notify_all()
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

