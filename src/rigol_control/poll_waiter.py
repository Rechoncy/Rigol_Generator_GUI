"""Short, cancellable waits for cumulative hardware-counter polling."""

from __future__ import annotations

import math
import sys
import time
from threading import Event


# CPython 3.11 switched Windows time.sleep to a waitable timer. Event.wait
# can still round a 2 ms poll up to one approximately 15.6 ms system tick.
# Other platforms and the supported Python 3.10 source runtime keep the
# directly interruptible Event.wait path; no global timer resolution changes.
# https://docs.python.org/3.13/library/time.html#time.sleep
_WINDOWS_HIGH_RESOLUTION_SLEEP = (
    sys.platform == "win32"
    and sys.implementation.name == "cpython"
    and sys.version_info >= (3, 11)
)
_MAX_SLEEP_SLICE_SECONDS = 0.005


def wait_for_poll(stop_event: Event, seconds: float) -> bool:
    """Wait for the poll interval; return True when stopping was requested.

    Windows CPython 3.11+ uses short high-resolution sleeps. Splitting longer
    intervals into at most 5 ms requests lets a stop interrupt the next slice.
    Scheduling may delay any wake-up, so this is not a real-time guarantee.
    The cumulative counter still records every edge between reads.
    """

    if not math.isfinite(seconds):
        raise ValueError("累计计数读取周期必须为有限数值")
    if not _WINDOWS_HIGH_RESOLUTION_SLEEP:
        return stop_event.wait(seconds)
    if stop_event.is_set():
        return True
    deadline = time.monotonic() + max(0.0, seconds)
    while not stop_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(remaining, _MAX_SLEEP_SLICE_SECONDS))
    return True
