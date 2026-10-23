from __future__ import annotations

import csv
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import Condition, Event, RLock, Thread, current_thread
from typing import Protocol, TextIO

from .drivers.ni_analog import AnalogDataset, AnalogInputBlock, save_analog_csv


class AnalogInputReader(Protocol):
    def start(self) -> None: ...

    def read(self) -> AnalogInputBlock: ...

    def stop(self) -> None: ...


class ContinuousAnalogTextWriter:
    """Append every acquired block to an Excel-friendly UTF-8 text file.

    The file is flushed once per NI read block. It therefore contains the full
    acquisition even after the bounded in-memory plotting buffer has discarded
    old samples.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._io_lock = RLock()
        self._lock = RLock()
        self._is_open = False
        self._stream: TextIO | None = None
        self._writer = None
        self._channel_names: tuple[str, ...] = ()
        self._sample_rate_hz = 0.0
        self._expected_sample_index: int | None = None
        self._saved_samples = 0

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._is_open

    @property
    def saved_samples(self) -> int:
        with self._lock:
            return self._saved_samples

    def open(self) -> None:
        with self._io_lock:
            if self._stream is not None:
                raise RuntimeError("连续采集文本文件已经打开")
            with self._lock:
                self._saved_samples = 0
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = self.path.open("w", encoding="utf-8-sig", newline="")
            with self._lock:
                self._is_open = True
            self._writer = csv.writer(self._stream)
            self._channel_names = ()
            self._sample_rate_hz = 0.0
            self._expected_sample_index = None

    def write_block(self, block: AnalogInputBlock) -> None:
        if block.sample_count == 0:
            return
        with self._io_lock:
            if self._stream is None or self._writer is None:
                raise RuntimeError("连续采集文本文件尚未打开")
            if not self._channel_names:
                self._channel_names = block.channel_names
                self._sample_rate_hz = block.sample_rate_hz
                self._expected_sample_index = block.first_sample_index
                self._writer.writerow(
                    ["# format", "RigolGeneratorControl Continuous Analog Input CSV v2"]
                )
                self._writer.writerow(
                    ["# created_utc", datetime.now(timezone.utc).isoformat()]
                )
                self._writer.writerow(
                    ["# sample_rate_hz", format(block.sample_rate_hz, ".12g")]
                )
                self._writer.writerow(["# first_sample_index", block.first_sample_index])
                self._writer.writerow(["# continuous_recording", "true"])
                self._writer.writerow(
                    ["sample_index", "elapsed_s", *block.channel_names]
                )
            if block.channel_names != self._channel_names:
                raise RuntimeError("连续记录过程中模拟输入通道发生变化")
            if not math.isclose(block.sample_rate_hz, self._sample_rate_hz):
                raise RuntimeError("连续记录过程中采样率发生变化")
            if block.first_sample_index != self._expected_sample_index:
                raise RuntimeError(
                    "连续记录检测到样本序号不连续："
                    f"期望 {self._expected_sample_index}，收到 {block.first_sample_index}"
                )
            for offset, values in enumerate(zip(*block.samples)):
                sample_index = block.first_sample_index + offset
                self._writer.writerow(
                    [
                        sample_index,
                        format(sample_index / block.sample_rate_hz, ".12g"),
                        *(format(value, ".12g") for value in values),
                    ]
                )
            self._stream.flush()
            self._expected_sample_index = block.first_sample_index + block.sample_count
            with self._lock:
                self._saved_samples += block.sample_count

    def close(self) -> None:
        with self._io_lock:
            stream = self._stream
            self._writer = None
            if stream is None:
                return
            errors = []
            try:
                stream.flush()
            except Exception as exc:
                errors.append(exc)
            try:
                stream.close()
            except Exception as exc:
                errors.append(exc)
                # TextIO.close may report a flush failure after closing its
                # underlying handle. Retain only handles still requiring work.
                if getattr(stream, "closed", False):
                    self._stream = None
            else:
                self._stream = None
            with self._lock:
                self._is_open = self._stream is not None
            if errors:
                if len(errors) == 1:
                    raise errors[0]
                raise OSError("；".join(str(error) for error in errors)) from errors[0]


class AnalogAcquisitionState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class AnalogAcquisitionSnapshot:
    state: AnalogAcquisitionState
    channel_names: tuple[str, ...]
    sample_rate_hz: float
    total_samples: int
    retained_samples: int
    dropped_samples: int
    minimum_v: tuple[float, ...] = ()
    maximum_v: tuple[float, ...] = ()
    rms_v: tuple[float, ...] = ()
    stream_path: str | None = None
    saved_samples: int = 0
    streaming: bool = False
    error: str | None = None
    cleanup_pending: bool = False
    revision: int = 0

    @property
    def duration_seconds(self) -> float:
        return self.total_samples / self.sample_rate_hz if self.sample_rate_hz > 0 else 0.0


@dataclass(frozen=True, slots=True)
class AnalogAcquisitionUpdate:
    snapshot: AnalogAcquisitionSnapshot
    block: AnalogInputBlock | None = None


class AnalogAcquisitionController:
    """Continuously read NI blocks, stream text, and retain a bounded plot window."""

    def __init__(
        self,
        reader: AnalogInputReader,
        *,
        maximum_retained_samples: int = 500_000,
        stream_path: str | Path | None = None,
    ) -> None:
        if not 1_000 <= maximum_retained_samples <= 5_000_000:
            raise ValueError("内存保留点数必须在 1000 至 5000000 之间")
        self.reader = reader
        self.maximum_retained_samples = maximum_retained_samples
        self.stream_writer = (
            ContinuousAnalogTextWriter(stream_path) if stream_path is not None else None
        )
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._shutdown_lock = RLock()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._start_thread = None
        self._read_active = False
        self._reader_open = False
        self._cleanup_thread: Thread | None = None
        self._cleanup_in_progress = False
        self._cleanup_started = False
        self._snapshot_revision = 0
        self._state = AnalogAcquisitionState.STOPPED
        self._channel_names: tuple[str, ...] = ()
        self._sample_rate_hz = 0.0
        self._total_samples = 0
        self._buffers: tuple[deque[float], ...] = ()
        self._minimum_v: tuple[float, ...] = ()
        self._maximum_v: tuple[float, ...] = ()
        self._rms_v: tuple[float, ...] = ()
        self._error: str | None = None
        self._callbacks = []

    def add_callback(self, callback) -> None:
        with self._lock:
            self._callbacks.append(callback)

    def _quiescent_locked(self) -> bool:
        return (
            self._start_thread is None
            and not self._read_active
            and not self._cleanup_in_progress
            and not self._reader_open
            and (self.stream_writer is None or not self.stream_writer.is_open)
        )

    @property
    def is_quiescent(self) -> bool:
        """True only after startup, reads and resource cleanup have finished."""
        with self._condition:
            return self._quiescent_locked()

    @property
    def is_running(self) -> bool:
        """Include failed cleanup so windows retain their NI device lease."""
        with self._condition:
            return not self._quiescent_locked()

    def snapshot(self) -> AnalogAcquisitionSnapshot:
        with self._lock:
            self._snapshot_revision += 1
            retained = len(self._buffers[0]) if self._buffers else 0
            return AnalogAcquisitionSnapshot(
                state=self._state,
                channel_names=self._channel_names,
                sample_rate_hz=self._sample_rate_hz,
                total_samples=self._total_samples,
                retained_samples=retained,
                dropped_samples=max(0, self._total_samples - retained),
                minimum_v=self._minimum_v,
                maximum_v=self._maximum_v,
                rms_v=self._rms_v,
                stream_path=(
                    str(self.stream_writer.path) if self.stream_writer is not None else None
                ),
                saved_samples=(
                    self.stream_writer.saved_samples if self.stream_writer is not None else 0
                ),
                streaming=(
                    self.stream_writer.is_open if self.stream_writer is not None else False
                ),
                error=self._error,
                cleanup_pending=(
                    self._state in {
                        AnalogAcquisitionState.ERROR,
                        AnalogAcquisitionState.STOPPING,
                    }
                    and not self._quiescent_locked()
                ),
                revision=self._snapshot_revision,
            )

    def start(self) -> None:
        with self._shutdown_lock, self._condition:
            if not self._quiescent_locked() or self._state in {
                AnalogAcquisitionState.STARTING,
                AnalogAcquisitionState.RUNNING,
                AnalogAcquisitionState.STOPPING,
            }:
                raise RuntimeError("模拟输入任务尚未完成停止和清理，请重试停止后再启动")
            self._start_thread = current_thread()
            self._state = AnalogAcquisitionState.STARTING
            self._channel_names = ()
            self._sample_rate_hz = 0.0
            self._total_samples = 0
            self._buffers = ()
            self._minimum_v = ()
            self._maximum_v = ()
            self._rms_v = ()
            self._error = None
            self._cleanup_started = False
            self._stop_event.clear()
        self._publish(None)
        try:
            if self._stop_event.is_set():
                return
            if self.stream_writer is not None:
                self.stream_writer.open()
            with self._condition:
                if self._stop_event.is_set():
                    return
                # A failed start may still leave a partially created NI task.
                self._reader_open = True
            self.reader.start()
            with self._condition:
                if self._stop_event.is_set():
                    return
                self._state = AnalogAcquisitionState.RUNNING
                self._read_active = True
                self._thread = Thread(target=self._read_loop, name="ni-analog-input", daemon=True)
                try:
                    self._thread.start()
                except Exception:
                    self._read_active = False
                    raise
        except Exception as exc:
            self._fail(f"启动模拟输入失败：{exc}")
            raise
        finally:
            with self._condition:
                self._start_thread = None
                cancelled = self._stop_event.is_set()
                self._condition.notify_all()
            if cancelled:
                self._ensure_cleanup()
            self._publish(None)

    def request_stop(self) -> None:
        """Request cancellation without blocking on NI reads or file I/O."""
        with self._condition:
            if self._quiescent_locked():
                return
            if self._state is not AnalogAcquisitionState.ERROR:
                self._state = AnalogAcquisitionState.STOPPING
            self._stop_event.set()
            self._condition.notify_all()
        self._publish(None)

    def stop(self, timeout_seconds: float = 5.0) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise ValueError("停止等待时间必须是非负有限数值")
        with self._shutdown_lock:
            with self._condition:
                if self._quiescent_locked():
                    return
            self.request_stop()
            self._ensure_cleanup(explicit_retry=True)
            deadline = time.monotonic() + timeout_seconds
            with self._condition:
                # Startup can originate in Qt's _DummyThread. Wait on our
                # condition flags rather than joining that foreign thread.
                self._condition.wait_for(
                    lambda: self._quiescent_locked()
                    or (
                        self._start_thread is None
                        and not self._cleanup_in_progress
                        and self._state is AnalogAcquisitionState.ERROR
                    ),
                    timeout=max(0.0, deadline - time.monotonic()),
                )
                pending = not self._quiescent_locked()
                if pending:
                    self._append_error_locked(
                        "停止尚未完成，NI 读取或资源清理未结束；已保留设备占用，请重试停止"
                    )
                else:
                    self._thread = None
            self._publish(None)
            if pending:
                raise RuntimeError(self.snapshot().error)

    def clear(self) -> None:
        with self._lock:
            if not self._quiescent_locked():
                raise RuntimeError("请先完成模拟输入采集的停止和清理再清空数据")
            self._total_samples = 0
            self._buffers = ()
            self._channel_names = ()
            self._sample_rate_hz = 0.0
            self._minimum_v = ()
            self._maximum_v = ()
            self._rms_v = ()
            self._error = None
            self._state = AnalogAcquisitionState.STOPPED
        self._publish(None)

    def dataset(self) -> AnalogDataset:
        with self._lock:
            retained = len(self._buffers[0]) if self._buffers else 0
            return AnalogDataset(
                channel_names=self._channel_names,
                sample_rate_hz=self._sample_rate_hz,
                first_sample_index=max(0, self._total_samples - retained),
                samples=tuple(tuple(values) for values in self._buffers),
            )

    def save_csv(self, path: str | Path) -> Path:
        return save_analog_csv(path, self.dataset())

    def _read_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    block = self.reader.read()
                except Exception:
                    if self._stop_event.is_set():
                        break
                    raise
                if self._stop_event.is_set():
                    break
                self._accept_block(block)
                if self.stream_writer is not None:
                    self.stream_writer.write_block(block)
                self._publish(block)
        except Exception as exc:
            self._fail(f"模拟输入采集失败：{exc}")
        finally:
            with self._condition:
                self._read_active = False
                # An explicit stop already attempted (and possibly failed)
                # cleanup. Only the user may retry that failed NI close.
                first_cleanup = not self._cleanup_started
                self._condition.notify_all()
            if first_cleanup:
                self._ensure_cleanup()

    def _ensure_cleanup(self, *, explicit_retry: bool = False) -> None:
        with self._condition:
            if self._cleanup_in_progress or self._quiescent_locked():
                return
            if self._cleanup_started and not explicit_retry:
                return
            self._cleanup_started = True
            self._cleanup_in_progress = True
            self._cleanup_thread = Thread(
                target=self._cleanup, name="ni-analog-cleanup", daemon=True
            )
            try:
                self._cleanup_thread.start()
            except Exception as exc:
                self._cleanup_in_progress = False
                self._append_error_locked(f"无法启动模拟输入清理：{exc}")
                self._condition.notify_all()

    def _cleanup(self) -> None:
        try:
            with self._condition:
                self._condition.wait_for(lambda: self._start_thread is None)
                reader_open = self._reader_open
            reader_failed = False
            if reader_open:
                try:
                    self.reader.stop()
                except Exception as exc:
                    reader_failed = True
                    with self._condition:
                        self._append_error_locked(f"关闭 NI 模拟输入失败：{exc}")
                else:
                    with self._condition:
                        self._reader_open = False
            with self._condition:
                if not reader_failed:
                    self._condition.wait_for(lambda: not self._read_active)
                can_close_stream = not self._read_active
            # The read worker may still be appending its final block. Never
            # detach its file before it has stopped writing.
            if can_close_stream and self.stream_writer is not None:
                try:
                    self.stream_writer.close()
                except Exception as exc:
                    with self._condition:
                        self._append_error_locked(f"关闭连续采集文件失败：{exc}")
        finally:
            with self._condition:
                self._cleanup_in_progress = False
                if self._quiescent_locked() and self._state is not AnalogAcquisitionState.ERROR:
                    self._state = AnalogAcquisitionState.STOPPED
                self._condition.notify_all()
            self._publish(None)

    def _accept_block(self, block: AnalogInputBlock) -> None:
        if block.sample_count == 0:
            return
        with self._lock:
            if not self._channel_names:
                self._channel_names = block.channel_names
                self._sample_rate_hz = block.sample_rate_hz
                self._buffers = tuple(
                    deque(maxlen=self.maximum_retained_samples) for _ in block.channel_names
                )
            if block.channel_names != self._channel_names:
                raise RuntimeError("采集过程中模拟输入通道发生变化")
            if not math.isclose(block.sample_rate_hz, self._sample_rate_hz):
                raise RuntimeError("采集过程中采样率发生变化")
            for buffer, values in zip(self._buffers, block.samples):
                buffer.extend(values)
            self._total_samples += block.sample_count
            self._minimum_v = tuple(min(values) for values in block.samples)
            self._maximum_v = tuple(max(values) for values in block.samples)
            self._rms_v = tuple(
                math.sqrt(sum(value * value for value in values) / len(values))
                for values in block.samples
            )

    def _fail(self, message: str) -> None:
        with self._lock:
            self._append_error_locked(message)
            self._stop_event.set()
        self._publish(None)

    def _append_error_locked(self, message: str) -> None:
        self._state = AnalogAcquisitionState.ERROR
        if self._error is None:
            self._error = message
        elif message not in self._error:
            self._error += "；" + message

    def _publish(self, block: AnalogInputBlock | None) -> None:
        update = AnalogAcquisitionUpdate(self.snapshot(), block)
        with self._lock:
            callbacks = tuple(self._callbacks)
        failed = []
        for callback in callbacks:
            try:
                callback(update)
            except Exception as exc:
                failed.append(callback)
                with self._condition:
                    self._append_error_locked(f"模拟输入状态回调失败：{exc}")
                    self._stop_event.set()
                logging.getLogger(__name__).exception("模拟输入状态回调失败")
        if failed:
            # Observers can record linked experiment data. A failed observer
            # therefore stops acquisition, but cannot unwind startup/cleanup
            # or recursively invoke itself through _fail/request_stop.
            self._ensure_cleanup()
            failure_update = AnalogAcquisitionUpdate(self.snapshot())
            for callback in callbacks:
                if callback in failed:
                    continue
                try:
                    callback(failure_update)
                except Exception as exc:
                    with self._condition:
                        self._append_error_locked(f"模拟输入状态回调失败：{exc}")
                        self._stop_event.set()
                    logging.getLogger(__name__).exception("模拟输入状态回调失败")
