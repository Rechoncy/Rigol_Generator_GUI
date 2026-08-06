from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from .ni_counter import NIDaqError


TERMINAL_MODES = ("DEFAULT", "RSE", "NRSE", "DIFF", "PSEUDODIFF")


@dataclass(frozen=True, slots=True)
class AnalogInputConfig:
    channels: tuple[str, ...]
    sample_rate_hz: float
    samples_per_read: int
    minimum_v: float = -10.0
    maximum_v: float = 10.0
    terminal_mode: str = "DEFAULT"

    def validate(self) -> None:
        normalized = tuple(channel.strip().strip("/") for channel in self.channels)
        if not normalized or any(not channel for channel in normalized):
            raise ValueError("至少需要选择一个模拟输入通道")
        if len(set(channel.casefold() for channel in normalized)) != len(normalized):
            raise ValueError("模拟输入通道不能重复")
        rate = float(self.sample_rate_hz)
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("采样率必须是大于 0 的有限数值")
        if isinstance(self.samples_per_read, bool) or not isinstance(self.samples_per_read, int):
            raise ValueError("每次读取点数必须是整数")
        if not 1 <= self.samples_per_read <= 1_000_000:
            raise ValueError("每次读取点数必须在 1 至 1000000 之间")
        minimum = float(self.minimum_v)
        maximum = float(self.maximum_v)
        if not math.isfinite(minimum) or not math.isfinite(maximum) or minimum >= maximum:
            raise ValueError("输入量程下限必须小于上限，且两者均为有限数值")
        if self.terminal_mode.upper() not in TERMINAL_MODES:
            raise ValueError(f"不支持的端接方式：{self.terminal_mode}")


@dataclass(frozen=True, slots=True)
class AnalogInputBlock:
    channel_names: tuple[str, ...]
    sample_rate_hz: float
    first_sample_index: int
    samples: tuple[tuple[float, ...], ...]

    def __post_init__(self) -> None:
        if not self.channel_names or len(self.channel_names) != len(self.samples):
            raise ValueError("模拟输入数据的通道名称和数据列不匹配")
        lengths = {len(values) for values in self.samples}
        if len(lengths) != 1:
            raise ValueError("所有模拟输入通道必须包含相同点数")

    @property
    def sample_count(self) -> int:
        return len(self.samples[0])


@dataclass(frozen=True, slots=True)
class AnalogDataset:
    channel_names: tuple[str, ...]
    sample_rate_hz: float
    first_sample_index: int
    samples: tuple[tuple[float, ...], ...]

    @property
    def sample_count(self) -> int:
        return len(self.samples[0]) if self.samples else 0


def save_analog_csv(path: str | Path, dataset: AnalogDataset) -> Path:
    """Save retained samples as an Excel-friendly UTF-8 CSV with metadata."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not dataset.channel_names or len(dataset.channel_names) != len(dataset.samples):
        raise ValueError("没有可保存的模拟输入通道")
    lengths = {len(values) for values in dataset.samples}
    if len(lengths) != 1 or not lengths or next(iter(lengths)) == 0:
        raise ValueError("没有可保存的模拟输入数据")
    if dataset.sample_rate_hz <= 0:
        raise ValueError("采样率必须大于 0")

    with destination.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["# format", "RigolGeneratorControl Analog Input CSV v1"])
        writer.writerow(["# created_utc", datetime.now(timezone.utc).isoformat()])
        writer.writerow(["# sample_rate_hz", format(dataset.sample_rate_hz, ".12g")])
        writer.writerow(["# first_sample_index", dataset.first_sample_index])
        writer.writerow(["sample_index", "elapsed_s", *dataset.channel_names])
        for offset, values in enumerate(zip(*dataset.samples)):
            sample_index = dataset.first_sample_index + offset
            writer.writerow(
                [
                    sample_index,
                    format(sample_index / dataset.sample_rate_hz, ".12g"),
                    *(format(value, ".12g") for value in values),
                ]
            )
    return destination


class NIDaqAnalogInput:
    """NI-DAQmx hardware-timed continuous voltage input reader."""

    def __init__(self, device_name: str, config: AnalogInputConfig) -> None:
        self.device_name = device_name.strip().strip("/")
        self.config = config
        self._task = None
        self._next_sample_index = 0
        self._actual_sample_rate_hz = float(config.sample_rate_hz)
        self._lock = RLock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._task is not None

    @classmethod
    def available_channels(cls, device_name: str) -> tuple[str, ...]:
        try:
            from nidaqmx.system.device import Device  # type: ignore[import-not-found]
        except ImportError as exc:
            raise NIDaqError("未安装 nidaqmx；请先安装 NI-DAQmx 驱动及 Python 包") from exc
        normalized = device_name.strip().strip("/")
        try:
            return tuple(Device(normalized).ai_physical_chans.channel_names)
        except Exception as exc:
            raise NIDaqError(f"无法读取 NI 模拟输入通道：{exc}") from exc

    def start(self) -> None:
        self.config.validate()
        with self._lock:
            if self._task is not None:
                raise NIDaqError("NI 模拟输入任务已经运行")
            try:
                import nidaqmx  # type: ignore[import-not-found]
                from nidaqmx.constants import (  # type: ignore[import-not-found]
                    AcquisitionType,
                    TerminalConfiguration,
                )
            except ImportError as exc:
                raise NIDaqError("未安装 nidaqmx；请先安装 NI-DAQmx 驱动及 Python 包") from exc

            terminal_map = {
                "DEFAULT": TerminalConfiguration.DEFAULT,
                "RSE": TerminalConfiguration.RSE,
                "NRSE": TerminalConfiguration.NRSE,
                "DIFF": TerminalConfiguration.DIFF,
                "PSEUDODIFF": TerminalConfiguration.PSEUDO_DIFF,
            }
            task = nidaqmx.Task(new_task_name="Rigol_Control_Analog_Input")
            try:
                for channel in self.config.channels:
                    task.ai_channels.add_ai_voltage_chan(
                        self._physical_channel(channel),
                        terminal_config=terminal_map[self.config.terminal_mode.upper()],
                        min_val=float(self.config.minimum_v),
                        max_val=float(self.config.maximum_v),
                    )
                task.timing.cfg_samp_clk_timing(
                    rate=float(self.config.sample_rate_hz),
                    sample_mode=AcquisitionType.CONTINUOUS,
                    samps_per_chan=max(self.config.samples_per_read * 10, 1000),
                )
                self._actual_sample_rate_hz = float(task.timing.samp_clk_rate)
                task.start()
            except Exception as exc:
                task.close()
                raise NIDaqError(f"无法启动 NI 模拟输入：{exc}") from exc
            self._next_sample_index = 0
            self._task = task

    def read(self) -> AnalogInputBlock:
        with self._lock:
            task = self._task
            first_sample_index = self._next_sample_index
        if task is None:
            raise NIDaqError("NI 模拟输入任务尚未启动")
        timeout = max(
            2.0,
            self.config.samples_per_read / self._actual_sample_rate_hz * 3.0,
        )
        try:
            raw = task.read(
                number_of_samples_per_channel=self.config.samples_per_read,
                timeout=timeout,
            )
            samples = self._normalize_read(raw)
        except Exception as exc:
            raise NIDaqError(f"读取 NI 模拟输入失败：{exc}") from exc
        count = len(samples[0])
        with self._lock:
            if self._task is task:
                self._next_sample_index += count
        return AnalogInputBlock(
            channel_names=tuple(self.config.channels),
            sample_rate_hz=self._actual_sample_rate_hz,
            first_sample_index=first_sample_index,
            samples=samples,
        )

    def stop(self) -> None:
        with self._lock:
            task, self._task = self._task, None
        if task is None:
            return
        try:
            task.stop()
        except Exception:
            pass
        finally:
            task.close()

    def _physical_channel(self, channel: str) -> str:
        normalized = channel.strip().strip("/")
        if "/" in normalized:
            return normalized
        return f"{self.device_name}/{normalized}"

    def _normalize_read(self, raw) -> tuple[tuple[float, ...], ...]:
        channel_count = len(self.config.channels)
        if channel_count == 1:
            if isinstance(raw, (int, float)):
                return ((float(raw),),)
            if raw and isinstance(raw[0], (list, tuple)):
                raw = raw[0]
            return (tuple(float(value) for value in raw),)
        if not isinstance(raw, (list, tuple)) or len(raw) != channel_count:
            raise NIDaqError("NI-DAQmx 返回的数据通道数与配置不一致")
        return tuple(tuple(float(value) for value in channel) for channel in raw)
