"""Cached output-change and task activity evidence; never performs hardware I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Any


@dataclass(frozen=True, slots=True)
class OutputReadbackActivity:
    revision: int
    barrier: tuple
    busy: bool


@dataclass(frozen=True, slots=True)
class _Observation:
    state: str
    received: int
    applied: int
    quiescent: bool
    results: tuple
    revision: int


@dataclass(slots=True)
class _Watch:
    token: int
    owner: str
    outputs: tuple[tuple[str, str, bool], ...]
    controller: Any
    latest: _Observation
    last_good: _Observation
    started: bool
    result_tokens: dict[str, tuple] = field(default_factory=dict)


class OutputActivityTracker:
    """Observe controller state outside our lock and retain canonical revisions.

    Revisions are change notifications, not trigger counts. Consecutive points
    may be coalesced into one observation. Controller snapshot revisions order
    racing observations, but do not enter the public quiet-window barrier.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._revisions: dict[str, int] = {}
        self._watches: dict[str, _Watch] = {}
        self._next_token = 0

    def note(self, key: str) -> int:
        with self._lock:
            self._revisions[key] = self._revisions.get(key, 0) + 1
            return self._revisions[key]

    @staticmethod
    def read(controller: Any) -> _Observation:
        # Both accessors read controller state only. Neither joins workers,
        # opens counters, starts/stops tasks, nor queries an instrument.
        try:
            snapshot = controller.snapshot()
            state = getattr(snapshot.state, "value", str(snapshot.state))
            return _Observation(
                state, int(snapshot.received_count), int(snapshot.applied_count),
                bool(controller.is_quiescent),
                tuple((item.device_id, item.started_at, item.finished_at, bool(item.success))
                      for item in getattr(snapshot, "device_results", ())),
                int(getattr(snapshot, "revision", 0)),
            )
        except Exception:
            # Missing activity evidence must not silently permit a query.
            return _Observation("unavailable", 0, 0, False, (), 0)

    def register(self, owner: str, outputs: tuple[tuple[str, str, bool], ...],
                 controller: Any, initial: _Observation) -> bool:
        """Register an already observed controller without calling it here."""
        with self._lock:
            existing = self._watches.get(owner)
            if existing is not None:
                return existing.controller is controller and existing.outputs == outputs
            self._next_token += 1
            self._watches[owner] = _Watch(
                self._next_token, owner, outputs, controller, initial, initial,
                initial.state != "stopped" or not initial.quiescent,
                {item[0]: item[1:] for item in initial.results},
            )
            return True

    def _observe(self, watches: tuple[_Watch, ...]) -> None:
        for watch in watches:
            observed = self.read(watch.controller)
            with self._lock:
                if self._watches.get(watch.owner) is not watch:
                    continue
                if observed.state == "unavailable":
                    watch.latest = observed
                    continue
                previous = watch.last_good
                if previous.revision > 0 and observed.revision <= previous.revision:
                    continue
                if (observed.state != "stopped" or not observed.quiescent
                        or observed.received != previous.received or observed.applied != previous.applied):
                    watch.started = True
                reset = observed.received < previous.received or observed.applied < previous.applied
                prior_applied = 0 if reset else previous.applied
                prior_results = {} if reset else watch.result_tokens
                changed = set()
                if observed.applied > prior_applied:
                    changed.update(key for _, key, _ in watch.outputs)
                else:
                    by_id = {device_id: key for device_id, key, _ in watch.outputs}
                    for result in observed.results:
                        device_id, *_, success = result
                        if success and result[1:] != prior_results.get(device_id) and device_id in by_id:
                            changed.add(by_id[device_id])
                for key in changed:
                    self._revisions[key] = self._revisions.get(key, 0) + 1
                watch.result_tokens = {item[0]: item[1:] for item in observed.results}
                watch.latest = observed
                watch.last_good = observed

    def observe_owner(self, owner: str) -> int | None:
        with self._lock:
            watch = self._watches.get(owner)
        if watch is not None:
            self._observe((watch,))
            return watch.token
        return None

    def release_observed(self, owner: str, token: int | None) -> bool:
        """Remove only the watched, confirmed-quiescent lifetime just observed."""
        with self._lock:
            watch = self._watches.get(owner)
            if watch is None:
                return token is None
            if (watch.token != token or not watch.latest.quiescent
                    or watch.latest.state in {"starting", "running", "stopping", "unavailable"}):
                return False
            self._watches.pop(owner)
            return True

    @staticmethod
    def _busy(watch: _Watch) -> bool:
        current = watch.latest
        if current.state == "running":
            return current.received > current.applied
        if current.state == "stopped":
            return not current.quiescent or not watch.started
        return True

    def activity(self, key: str, *, vaunix: bool,
                 leases: tuple[tuple[str, str, bool], ...]) -> OutputReadbackActivity:
        def relevant(watch: _Watch) -> bool:
            return any(output_key == key or (vaunix and is_vaunix)
                       for _, output_key, is_vaunix in watch.outputs)

        with self._lock:
            watches = tuple(watch for watch in self._watches.values() if relevant(watch))
        self._observe(watches)
        with self._lock:
            watches = tuple(watch for watch in self._watches.values() if relevant(watch))
            matching_leases = tuple(sorted((lease_key, owner) for lease_key, owner, is_vaunix in leases
                                          if lease_key == key or (vaunix and is_vaunix)))
            covered = {(output_key, watch.owner) for watch in watches for _, output_key, _ in watch.outputs}
            unknown_lease = any(item not in covered for item in matching_leases)
            barriers = tuple(
                (watch.token, watch.owner, watch.latest.state, watch.latest.received,
                 watch.latest.applied, watch.latest.quiescent, watch.latest.results)
                for watch in sorted(watches, key=lambda item: item.token)
            )
            revision = self._revisions.get(key, 0)
            return OutputReadbackActivity(
                revision, (revision, matching_leases, barriers),
                unknown_lease or any(self._busy(watch) for watch in watches),
            )
