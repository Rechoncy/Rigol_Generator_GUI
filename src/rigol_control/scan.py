from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from itertools import product
import math

from .drivers.dg4000 import StepParameter, StepValue
from .sequence import MAX_SCAN_POINTS, linear_scan_points
from .units import to_base, unit_spec


MAX_SCAN_AXES = 10


class ScanMode(str, Enum):
    SYNCHRONOUS = "synchronous"
    CARTESIAN = "cartesian"

    @property
    def label(self) -> str:
        return {
            ScanMode.SYNCHRONOUS: "同步联动（同序号）",
            ScanMode.CARTESIAN: "组合扫描（笛卡尔积）",
        }[self]


@dataclass(frozen=True, slots=True)
class ScanAxis:
    parameter: StepParameter
    channel: int
    unit_key: str
    start: float
    step: float
    count: int

    def validate(self) -> None:
        if self.channel not in (1, 2):
            raise ValueError("扫描轴通道只能是 1 或 2")
        unit_spec(self.parameter.unit_family, self.unit_key)
        if not math.isfinite(float(self.start)) or not math.isfinite(float(self.step)):
            raise ValueError("扫描轴起点和步长必须是有限数值")
        if self.count < 1 or self.count > MAX_SCAN_POINTS:
            raise ValueError(f"单轴点数必须在 1 至 {MAX_SCAN_POINTS} 之间")

    @property
    def target(self) -> tuple[int, StepParameter]:
        return self.channel, self.parameter


@dataclass(frozen=True, slots=True)
class ScanPoint:
    values: tuple[StepValue, ...]

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError("联合扫描点至少包含一个参数")


def validate_scan_axes(axes: tuple[ScanAxis, ...] | list[ScanAxis]) -> tuple[ScanAxis, ...]:
    normalized = tuple(axes)
    if not normalized:
        raise ValueError("至少需要一个扫描轴")
    if len(normalized) > MAX_SCAN_AXES:
        raise ValueError(f"扫描轴不能超过 {MAX_SCAN_AXES} 个")
    targets: set[tuple[int, StepParameter]] = set()
    for index, axis in enumerate(normalized, start=1):
        try:
            axis.validate()
        except ValueError as exc:
            raise ValueError(f"扫描轴 {index}：{exc}") from exc
        if axis.target in targets:
            raise ValueError(f"扫描轴 {index} 与已有轴重复控制同一通道和参数")
        targets.add(axis.target)
    return normalized


def scan_point_count(
    axes: tuple[ScanAxis, ...] | list[ScanAxis],
    mode: ScanMode,
    *,
    maximum: int = MAX_SCAN_POINTS,
) -> int:
    normalized = validate_scan_axes(axes)
    if mode is ScanMode.SYNCHRONOUS:
        counts = {axis.count for axis in normalized}
        if len(counts) != 1:
            raise ValueError("同步联动要求所有扫描轴的点数相同")
        total = normalized[0].count
    else:
        total = math.prod(axis.count for axis in normalized)
    if total > maximum:
        raise ValueError(f"联合扫描点数 {total} 超过上限 {maximum}")
    return total


def _axis_values(axis: ScanAxis) -> tuple[StepValue, ...]:
    display_values = linear_scan_points(axis.start, axis.step, axis.count)
    return tuple(
        StepValue(
            parameter=axis.parameter,
            channel=axis.channel,
            value=to_base(value, axis.parameter.unit_family, axis.unit_key),
            display_value=value,
            unit_key=axis.unit_key,
        )
        for value in display_values
    )


def generate_scan_points(
    axes: tuple[ScanAxis, ...] | list[ScanAxis],
    mode: ScanMode,
    *,
    maximum: int = MAX_SCAN_POINTS,
) -> tuple[ScanPoint, ...]:
    normalized = validate_scan_axes(axes)
    total = scan_point_count(normalized, mode, maximum=maximum)
    series = tuple(_axis_values(axis) for axis in normalized)
    if mode is ScanMode.SYNCHRONOUS:
        return tuple(
            ScanPoint(tuple(values[index] for values in series))
            for index in range(total)
        )
    return tuple(ScanPoint(tuple(values)) for values in product(*series))
