from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Sequence, TypeVar


T = TypeVar("T")


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

