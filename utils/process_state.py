"""Process-wide state that must survive Streamlit reruns.

Streamlit executes app.py afresh on every rerun, so a lock, cache or "already
done" flag defined at the top of app.py is re-created each time: locks stop
protecting anything across sessions, caches never hit, "once" flags fire on
every click. Imported modules persist for the life of the process, so this
module holds those objects and app.py fetches them by name.

Used by: app.py, ui/components.py, ui/config.py, ui/question_bank.py,
ui/question_log.py, ui/startup.py.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

_REGISTRY_LOCK = threading.Lock()
_LOCKS: dict[str, Any] = {}
_SHARED: dict[str, Any] = {}
_DONE: set[str] = set()


def lock(name: str, *, reentrant: bool = False):
    """The same Lock/RLock object for this name for the whole process."""
    with _REGISTRY_LOCK:
        found = _LOCKS.get(name)
        if found is None:
            found = _LOCKS[name] = threading.RLock() if reentrant else threading.Lock()
        return found


def shared(name: str, factory: Callable[[], Any]):
    """The same mutable object (dict, set, ...) for this name for the whole process."""
    with _REGISTRY_LOCK:
        if name not in _SHARED:
            _SHARED[name] = factory()
        return _SHARED[name]


def first_time(name: str) -> bool:
    """True exactly once per process for this name (thread-safe)."""
    with _REGISTRY_LOCK:
        if name in _DONE:
            return False
        _DONE.add(name)
        return True
