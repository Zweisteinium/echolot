"""Figures the pages share (the library's counts and rings, the lists' tiers, the tab counts), computed for every
client at once: a value younger than FRESH seconds answers; one younger than STALE answers too while a thread
computes it anew; an older one, or none, is computed before the page. A request that changed the database
drops them all (common.get_db), so the page after a change shows it; WARM seconds after the last change a
thread computes them again, so that page need not wait."""

import logging
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from sqlite3 import Connection
from typing import Any

from echolot import db

FRESH, STALE, WARM = 30, 120, 3  # seconds; WARM 0: not computed again after a change
log = logging.getLogger(__name__)
Compute = Callable[..., Any]
_values: dict[tuple, tuple[float, Any, Compute]] = {}  # key -> (computed at, value, its function)
_computing: defaultdict[tuple, threading.Lock] = defaultdict(threading.Lock)
_generation = 0
_dropped: dict[tuple, Compute] = {}  # to compute again after a change
_lock = threading.Lock()
_timer: threading.Timer | None = None


def get(con: Connection, compute: Compute, *args: Any) -> Any:
    """compute(con, *args), shared by the pages asking with the same `args` (the user's scope, a range)."""
    path = con.execute("PRAGMA database_list").fetchone()["file"]
    key = (path, f"{compute.__module__}.{compute.__qualname__}", *args)
    hit = _values.get(key)
    age = time.monotonic() - hit[0] if hit else STALE
    if age < FRESH:
        return hit[1]
    lock = _computing[key]
    if age < STALE:
        if lock.acquire(blocking=False):
            threading.Thread(target=_renew, args=(key, compute, lock), daemon=True).start()
        return hit[1]
    with lock:  # concurrent pages wait for the one computation
        hit = _values.get(key)
        return hit[1] if hit and time.monotonic() - hit[0] < FRESH else _compute(con, key, compute)


def _compute(con: Connection, key: tuple, compute: Compute) -> Any:
    generation, value = _generation, compute(con, *key[2:])
    if generation == _generation:  # not dropped meanwhile
        _values[key] = (time.monotonic(), value, compute)
    return value


def _renew(key: tuple, compute: Compute, lock: threading.Lock) -> None:
    try:
        con = db.connect(key[0])
        try:
            _compute(con, key, compute)
        finally:
            con.close()
    except Exception:  # the page keeps the value it has, or computes it
        log.exception("computing %s", key[1])
    finally:
        lock.release()


def clear() -> None:
    """Drop every value: the database changed."""
    global _generation, _timer
    with _lock:
        _generation += 1
        if WARM:
            _dropped.update({key: v[2] for key, v in list(_values.items())})  # (a renew may add one meanwhile)
        _values.clear()
        if _timer is not None:
            _timer.cancel()
        if WARM and _dropped:
            _timer = threading.Timer(WARM, _warm)
            _timer.daemon = True
            _timer.start()


def _warm() -> None:
    """Compute the dropped values again (each unless a page computes it already)."""
    with _lock:
        todo = dict(_dropped)
        _dropped.clear()
    for key, compute in todo.items():
        if key not in _values and (lock := _computing[key]).acquire(blocking=False):
            _renew(key, compute, lock)
