"""Coalesced, event driven readback; no queries run on the GUI/trigger threads."""
from __future__ import annotations

from dataclasses import dataclass
import time
from types import SimpleNamespace
from typing import Any, Callable

from PyQt6.QtCore import QObject, QTimer


def window_readback_activity(window):
    manager = window.connection_manager
    state = (manager.output_readback_activity(window.record) if manager is not None
             else SimpleNamespace(revision=0, barrier=(), busy=False))
    controller = getattr(window, "controller", None)
    if controller is None:
        return state
    snapshot = controller.snapshot()
    phase = snapshot.state.value
    applied = snapshot.applied_count
    return SimpleNamespace(
        revision=state.revision,
        barrier=(state.barrier, id(controller), phase, snapshot.received_count, applied),
        busy=(state.busy or phase in {"starting", "stopping"}
              or (phase == "running" and snapshot.backlog > 0)
              or (phase in {"stopped", "error"} and not controller.is_quiescent)),
    )


@dataclass(frozen=True, slots=True)
class ReadbackTicket:
    generation: int
    revision: Any
    barrier: tuple
    local_revision: int
    automatic: bool
    request_id: int
    change_revision: int


class AutoReadback(QObject):
    def __init__(self, parent, *, refresh: Callable[[], None], invalidate: Callable[[str], None],
                 activity, connected, generation, pending, ready,
                 quiet_seconds: float = .15, interval_seconds: float = .5):
        super().__init__(parent)
        self._refresh, self._invalidate = refresh, invalidate
        self._activity, self._connected, self._generation = activity, connected, generation
        self._pending, self._ready = pending, ready
        self._quiet_seconds, self._interval_seconds = quiet_seconds, interval_seconds
        self._enabled = True
        self._paused = False
        self._stopped = False
        self._dirty = False
        self._local_revision = 0
        self._change_revision = 0
        self._request_serial = 0
        self._latest_request = None
        self._last_started = float("-inf")
        self.reset()
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def reset(self) -> None:
        state = self._activity()
        self._seen_generation = self._generation()
        self._seen_revision = state.revision
        self._seen_barrier = state.barrier
        self._changed_at = time.monotonic()
        self._dirty = False
        self._local_revision += 1
        self._request_serial += 1
        self._latest_request = None

    def set_enabled(self, enabled: bool) -> None:
        if enabled and self._stopped:
            self._stopped = False
            self.reset()
            self.timer.start()
        self._enabled = enabled
        self._local_revision += 1
        self._paused = False
        if enabled and not self._stopped:
            self._dirty = True
            self._changed_at = time.monotonic()

    @property
    def paused(self) -> bool:
        return self._paused

    def request(self) -> None:
        self._local_revision += 1
        self._change_revision += 1
        self._dirty = True
        self._changed_at = time.monotonic()
        self._invalidate("参数已变化 · 等待自动回读" if self._enabled else "参数已变化 · 请手动回读")

    def capture(self, automatic: bool = False) -> ReadbackTicket:
        state = self._activity()
        # Observe a successful write before starting the matching read, so the
        # next timer tick cannot invalidate that very same request.
        self._seen_generation = self._generation()
        self._seen_revision, self._seen_barrier = state.revision, state.barrier
        self._last_started = time.monotonic()
        self._request_serial += 1
        self._latest_request = self._request_serial
        return ReadbackTicket(self._seen_generation, state.revision, state.barrier,
                              self._local_revision, automatic, self._latest_request, self._change_revision)

    def cache_is_current(self, ticket: ReadbackTicket) -> bool:
        state = self._activity()
        return (self._connected() and self._generation() == ticket.generation
                and state.revision == ticket.revision and state.barrier == ticket.barrier
                and self._change_revision == ticket.change_revision)

    def accepts(self, ticket: ReadbackTicket) -> bool:
        return (self.cache_is_current(ticket)
                and ticket.request_id == self._latest_request
                and self._local_revision == ticket.local_revision
                and (not ticket.automatic or (self._enabled and not self._stopped)))

    def can_continue(self, ticket: ReadbackTicket) -> bool:
        return self.accepts(ticket) and not self._activity().busy and self._ready()

    def finish(self, ticket: ReadbackTicket, outcome: str) -> None:
        if (self._stopped or ticket.request_id != self._latest_request
                or ticket.generation != self._generation()):
            return
        if outcome == "failure":
            self._paused = True
            self._dirty = False
        elif outcome == "success" and self.accepts(ticket):
            self._dirty = False
            self._paused = False
        else:
            self._dirty = self._connected()
            self._changed_at = time.monotonic()

    def stop(self) -> None:
        self._stopped = True
        self._local_revision += 1
        self._dirty = False
        self.timer.stop()

    def _tick(self) -> None:
        if self._stopped:
            return
        state, now = self._activity(), time.monotonic()
        generation = self._generation()
        if generation != self._seen_generation or not self._connected():
            self.reset()
            return
        if state.revision != self._seen_revision:
            self._seen_revision = state.revision
            self.request()
        if state.barrier != self._seen_barrier:
            self._seen_barrier = state.barrier
            self._changed_at = now
            if state.busy and not self._paused:
                # A partially accepted/failed point may have no confirmed
                # applied increment. Do not present its preceding value as
                # current, and do not query merely because arming began.
                self._invalidate("任务忙碌或状态待确认 · 回读值未知")
        if (self._enabled and not self._paused and self._dirty and not self._pending()
                and not state.busy and self._ready()
                and now - self._changed_at >= self._quiet_seconds
                and now - self._last_started >= self._interval_seconds):
            self._refresh()
