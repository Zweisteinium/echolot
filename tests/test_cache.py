import sqlite3
import time
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from echolot import db
from echolot.config import Settings
from echolot.web import cache, create_app


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The cache's clock, set by the test; FRESH 30 s, STALE 120 s, no warming after a change."""
    now = [1000.0]
    monkeypatch.setattr(cache, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(cache, "FRESH", 30)
    monkeypatch.setattr(cache, "STALE", 120)
    return now


def counting() -> tuple[list[int], Callable[[sqlite3.Connection, int], tuple[int, int]]]:
    calls = []

    def figures(con: sqlite3.Connection, uid: int) -> tuple[int, int]:
        calls.append(uid)
        return uid, len(calls)

    return calls, figures


def eventually(test: Callable[[], bool]) -> bool:
    for _ in range(200):
        if test():
            return True
        time.sleep(0.01)
    return False


def test_figures_are_shared_while_fresh_then_renewed(settings: Settings, clock: list[float]) -> None:
    calls, figures = counting()
    con = db.connect(settings.db_path)
    try:
        assert cache.get(con, figures, 1) == (1, 1) and cache.get(con, figures, 1) == (1, 1)
        assert cache.get(con, figures, 2) == (2, 2)  # other arguments: their own value
        clock[0] += 60  # stale: the old value answers, a thread computes the new one
        assert cache.get(con, figures, 1) == (1, 1)
        assert eventually(lambda: cache.get(con, figures, 1) == (1, 3))
        clock[0] += 500  # too old: computed before the answer
        assert cache.get(con, figures, 1) == (1, 4) and calls == [1, 2, 1, 1]
    finally:
        con.close()


def test_a_change_drops_the_figures_and_they_are_computed_again(
    settings: Settings, clock: list[float], login: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, figures = counting()
    client = login(create_app(settings))
    con = db.connect(settings.db_path)
    try:
        cache.get(con, figures, 1)
        client.get("/library")  # reading changes nothing
        assert cache.get(con, figures, 1) == (1, 1)
        monkeypatch.setattr(cache, "WARM", 0.01)
        assert client.post("/account/view", data={"view": "everyone"}, follow_redirects=False).status_code == 303
        assert eventually(lambda: len(calls) == 2)  # computed again after the change, without a page asking
        assert cache.get(con, figures, 1) == (1, 2) and len(calls) == 2
    finally:
        con.close()
