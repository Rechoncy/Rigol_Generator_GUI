from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import Any

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from ..trigger import ControllerState


_STATE_ORDER = {
    ControllerState.STARTING: 0,
    ControllerState.RUNNING: 1,
    ControllerState.STOPPING: 2,
    ControllerState.ERROR: 3,
    ControllerState.STOPPED: 4,
}


class SnapshotBridge(QObject):
    """Display the latest controller snapshot without queuing an event per point.

    Controller observers still run for every point. Only this UI observer is
    coalesced: worker threads replace one Python object, and the GUI timer emits
    at most once per interval. Session-bound callbacks reject a former task's
    late updates when a new task has already started.
    """

    updated = pyqtSignal(object)

    def __init__(self, parent: QObject | None = None, *, interval_ms: int = 50) -> None:
        super().__init__(parent)
        if interval_ms < 1:
            raise ValueError("快照刷新间隔必须大于 0")
        self._lock = Lock()
        self._generation = 0
        self._latest: Any = None
        self._state: ControllerState | None = None
        self._counts: tuple[int, int] | None = None
        self._revision = 0
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.flush)
        self._timer.start()

    def begin_session(self) -> Callable[[Any], None]:
        """Return a worker-safe publisher bound to the newly started task."""
        self._require_gui_thread()
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._latest = None
            self._state = None
            self._counts = None
            self._revision = 0
        self._timer.start()
        return lambda snapshot: self.publish(snapshot, generation=generation)

    def publish(self, snapshot: Any, *, generation: int | None = None) -> None:
        """Run on any thread; never invoke Qt or a UI callback from here."""
        counts = (snapshot.received_count, snapshot.applied_count)
        revision = getattr(snapshot, "revision", 0)
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            # Controllers stamp snapshots while holding their state lock. This
            # orders same-count terminal changes even when callbacks arrive in
            # the opposite order. Once stamped, do not accept unordered input.
            if self._revision > 0 and revision <= self._revision:
                return
            # The monitor and writer callbacks can race after constructing
            # their snapshots. Reject stale counts even for terminal updates;
            # a delayed ERROR/STOPPED must not rewind the final preview.
            if self._counts is not None and any(
                current < previous for current, previous in zip(counts, self._counts)
            ):
                return
            # Legacy snapshots have no creation revision, so state progress is
            # the best available fallback. Stamped snapshots can legitimately
            # report a newer ERROR after STOPPED and must not be rank-filtered.
            if revision <= 0 and self._state is not None and (
                _STATE_ORDER[snapshot.state] < _STATE_ORDER[self._state]
            ):
                return
            self._state = snapshot.state
            self._counts = counts
            self._revision = revision
            self._latest = snapshot

    def flush(self) -> None:
        """Deliver a pending final update before resetting stopped UI buttons."""
        self._require_gui_thread()
        with self._lock:
            snapshot, self._latest = self._latest, None
        if snapshot is not None:
            self.updated.emit(snapshot)

    def close(self) -> None:
        """Discard late callbacks and stop polling while the window is closed."""
        self._require_gui_thread()
        self._timer.stop()
        with self._lock:
            self._generation += 1
            self._latest = None
            self._counts = None
            self._state = None
            self._revision = 0

    def _require_gui_thread(self) -> None:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("快照桥的会话和刷新操作必须在所属 GUI 线程执行")
