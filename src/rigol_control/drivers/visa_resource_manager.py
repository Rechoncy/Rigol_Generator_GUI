"""Own shared PyVISA managers without closing another device's resource.

PyVISA may return the same ResourceManager for every call using a library.
Its close() also closes *all* resources created through that manager, so every
application connection, probe and discovery operation must hold a lease.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Condition
from typing import Any


@dataclass
class _ManagerEntry:
    manager: Any
    users: int = 0


_condition = Condition()
_entries: dict[int, _ManagerEntry] = {}
_closing_managers = 0


class VisaManagerLease:
    def __init__(self, entry: _ManagerEntry) -> None:
        self.manager = entry.manager
        self._entry = entry
        self._released = False

    def release(self) -> None:
        global _closing_managers
        with _condition:
            if self._released:
                return
            self._released = True
            self._entry.users -= 1
            if self._entry.users:
                return
            del _entries[id(self.manager)]
            # An acquire must not obtain PyVISA's singleton while close() is
            # still in progress. Native close runs outside this global lock.
            _closing_managers += 1
        try:
            self.manager.close()
        finally:
            with _condition:
                _closing_managers -= 1
                _condition.notify_all()


def acquire_visa_manager() -> VisaManagerLease:
    import pyvisa  # type: ignore[import-not-found]

    with _condition:
        while _closing_managers:
            _condition.wait()
        manager = pyvisa.ResourceManager()
        key = id(manager)
        entry = _entries.get(key)
        if entry is None:
            entry = _ManagerEntry(manager)
            _entries[key] = entry
        entry.users += 1
        return VisaManagerLease(entry)
