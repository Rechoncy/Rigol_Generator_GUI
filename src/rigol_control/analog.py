from __future__ import annotations

import csv
import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import Event, RLock, Thread, current_thread
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
        self._lock = RLock()
        self._stream: TextIO | None = None
        self._writer = None
        self._channel_names: tuple[str, ...] = ()
        self._sample_rate_hz = 0.0
        self._expected_sample_index: int | None = None
        self._saved_samples = 0

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._stream is not None

    @property
    def saved_samples(self) -> int:
        with self._lock:
            return self._saved_samples

    def open(self) -> None:
        with self._lock:
            if self._stream is not None:
                raise RuntimeError("连续采集文本文件已经打开")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = self.path.open("w", encoding="utf-8-sig", newline="")
            self._writer = csv.writer(self._stream)
            self._channel_names = ()
            self._sample_rate_hz = 0.0
            self._expected_sample_index = None
            self._saved_samples = 0

    def write_block(self, block: AnalogInputBlock) -> None:
        if block.sample_count == 0:
            return
        with self._lock:
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
            self._saved_samples += block.sample_count
            self._expected_sample_index = block.first_sample_index + block.sample_count
            self._stream.flush()

    def close(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
            self._writer = None
        if stream is not None:
            stream.flush()
            stream.close()


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
        self._stop_event = Event()
        self._thread: Thread | None = None
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

    def snapshot(self) -> AnalogAcquisitionSnapshot:
        with self._lock:
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
            )

    def start(self) -> None:
        with self._lock:
            if self._state in {AnalogAcquisitionState.STARTING, AnalogAcquisitionState.RUNNING}:
                raise RuntimeError("模拟输入采集已经运行")
            self._state = AnalogAcquisitionState.STARTING
            self._channel_names = ()
            self._sample_rate_hz = 0.0
            self._total_samples = 0
            self._buffers = ()
            self._minimum_v = ()
            self._maximum_v = ()
            self._rms_v = ()
            self._error = None
            self._stop_event.clear()
        self._publish(None)
        try:
            if self.stream_writer is not None:
                self.stream_writer.open()
            self.reader.start()
        except Exception as exc:
            if self.stream_writer is not None:
                self.stream_writer.close()
            self._fail(f"启动模拟输入失败：{exc}")
            raise
        with self._lock:
            self._state = AnalogAcquisitionState.RUNNING
            self._thread = Thread(target=self._read_loop, name="ni-analog-input", daemon=True)
            thread = self._thread
        thread.start()
        self._publish(None)

    def stop(self) -> None:
        with self._lock:
            if self._state is AnalogAcquisitionState.STOPPED:
                return
            self._state = AnalogAcquisitionState.STOPPING
            self._stop_event.set()
            thread = self._thread
        self._publish(None)
        try:
            self.reader.stop()
        finally:
            if thread is not None and thread is not current_thread():
                thread.join(timeout=5.0)
            if self.stream_writer is not None:
                self.stream_writer.close()
            with self._lock:
                self._thread = None
                self._state = AnalogAcquisitionState.STOPPED
            self._publish(None)

    def clear(self) -> None:
        with self._lock:
            if self._state in {AnalogAcquisitionState.STARTING, AnalogAcquisitionState.RUNNING}:
                raise RuntimeError("请先停止模拟输入采集再清空数据")
            self._total_samples = 0
            self._buffers = ()
            self._channel_names = ()
            self._minimum_v = ()
            self._maximum_v = ()
            self._rms_v = ()
            self._error = None
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
                block = self.reader.read()
                if self._stop_event.is_set():
                    break
                self._accept_block(block)
                if self.stream_writer is not None:
                    self.stream_writer.write_block(block)
                self._publish(block)
        except Exception as exc:
            if not self._stop_event.is_set():
                if self.stream_writer is not None:
                    self.stream_writer.close()
                self._fail(f"模拟输入采集失败：{exc}")
        finally:
            try:
                self.reader.stop()
            except Exception:
                pass
            if self.stream_writer is not None:
                self.stream_writer.close()

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
            self._state = AnalogAcquisitionState.ERROR
            self._error = message
            self._stop_event.set()
        self._publish(None)

    def _publish(self, block: AnalogInputBlock | None) -> None:
        update = AnalogAcquisitionUpdate(self.snapshot(), block)
        with self._lock:
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            callback(update)
