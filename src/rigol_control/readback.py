"""Cooperative read-only diagnostics that yield to instrument control work."""
from __future__ import annotations

from collections.abc import Callable


class ReadbackDeferred(Exception):
    """The optional snapshot yielded before completion; it is not an I/O error."""


def check_readback_allowed(should_continue: Callable[[], bool] | None) -> None:
    """Check between individual I/O calls, without retrying or changing hardware."""
    if should_continue is not None and not should_continue():
        raise ReadbackDeferred("自动回读已让位于参数下发或停止任务")
