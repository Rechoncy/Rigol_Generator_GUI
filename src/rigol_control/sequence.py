from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Generic, Sequence, TypeVar


T = TypeVar("T")
MAX_SCAN_POINTS = 10_000


def linear_scan_points(
    start: float,
    step: float,
    point_count: int,
    *,
    maximum: int = MAX_SCAN_POINTS,
) -> tuple[float, ...]:
    """Build an inclusive-start linear scan without binary-float accumulation."""

    if isinstance(point_count, bool) or not isinstance(point_count, int):
        raise ValueError("扫描点数必须是整数")
    if point_count < 1:
        raise ValueError("扫描点数至少为 1")
    if point_count > maximum:
        raise ValueError(f"扫描点数不能超过 {maximum}")
    try:
        start_decimal = Decimal(str(start))
        step_decimal = Decimal(str(step))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("扫描起点和步长必须是有效数值") from exc
    if not start_decimal.is_finite() or not step_decimal.is_finite():
        raise ValueError("扫描起点和步长必须是有限数值")

    points: list[float] = []
    for index in range(point_count):
        value = start_decimal + step_decimal * index
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"第 {index + 1} 个扫描点超出可表示范围")
        points.append(number)
    return tuple(points)


@dataclass(frozen=True, slots=True)
class TriggerPosition(Generic[T]):
    trigger_number: int
    index: int
    value: T


class CyclicSequence(Generic[T]):
    """Maps received triggers to an ordered, endlessly repeating sequence.

    Trigger 1 applies index 0.  This makes the first trigger explicitly send and
    hold the configured start point.  Trigger 2 advances to index 1, and so on.
    """

    def __init__(self, points: Sequence[T]) -> None:
        self._points = tuple(points)
        if not self._points:
            raise ValueError("步进序列至少需要一个点")

    @property
    def points(self) -> tuple[T, ...]:
        return self._points

    def index_for_trigger(self, trigger_number: int) -> int:
        if trigger_number < 1:
            raise ValueError("触发编号必须从 1 开始")
        return (trigger_number - 1) % len(self._points)

    def for_trigger(self, trigger_number: int) -> TriggerPosition[T]:
        index = self.index_for_trigger(trigger_number)
        return TriggerPosition(trigger_number, index, self._points[index])

    def pending(self, applied_count: int, received_count: int) -> list[TriggerPosition[T]]:
        if applied_count < 0 or received_count < 0:
            raise ValueError("计数不能为负数")
        if applied_count > received_count:
            raise ValueError("已下发计数不能大于已接收触发计数")
        return [self.for_trigger(number) for number in range(applied_count + 1, received_count + 1)]
