"""The worker (worker.py): jobs start when due, one per resource, paused jobs wait, Run now works."""

import threading
import time
from datetime import datetime

import pytest

from echolot import db
from echolot.config import Settings
from echolot.jobs import worker
from echolot.settings import options
from echolot.settings.vault import Vault


@pytest.fixture
def w(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    started: list[str] = []
    release = threading.Event()

    def job(name: str):
        def run(r: worker.Run) -> str:
            started.append(name)
            release.wait(5)
            if name == "fallback":
                raise RuntimeError("no network")
            return f"{name} done"

        return run

    monkeypatch.setattr(worker, "FUNCTIONS", {n: job(n) for n in worker.FUNCTIONS})
    wk = worker.Worker(settings, Vault.from_env(settings.data_dir, {}))
    yield wk, started, release, settings
    release.set()
    wk.stop()


def last(settings: Settings, name: str):
    con = db.connect(settings.db_path)
    try:
        return con.execute("SELECT * FROM jobs WHERE name = ?", (name,)).fetchone()
    finally:
        con.close()


def wait_for(cond, seconds: float = 5) -> None:
    end = time.monotonic() + seconds
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.02)


def test_paused_jobs_wait_but_run_now_works(w) -> None:
    wk, started, release, settings = w  # the takeover left the jobs paused
    wk._start_due()
    assert started == []
    wk.trigger("library")
    wk._start_due()
    wait_for(lambda: started == ["library"])
    release.set()
    wait_for(lambda: last(settings, "library")["finished"] is not None)
    assert last(settings, "library")["message"] == "library done"


def test_one_job_per_resource_and_failures_recorded(w) -> None:
    wk, started, release, settings = w
    con = db.connect(settings.db_path)
    with con:
        options.update(con, options.Jobs, paused=False)
        con.execute("DELETE FROM jobs")  # never run: everything is due
    con.close()
    wk._start_due()
    wait_for(lambda: len(started) == 3)
    assert set(started) == {"sync", "soundcloud", "library"}  # one each of soulseek, web, local
    assert {r.job.resource for r in wk.runs.values()} == {"soulseek", "web", "local"}
    release.set()
    wait_for(lambda: not wk.runs)
    started.clear()
    release.clear()
    con = db.connect(settings.db_path)
    with con:  # sweep and upgrade run at fixed times: one may be due at this hour, so they just ran
        now = datetime.now().isoformat(timespec="seconds")
        ran = [(n, now, now) for n in ("sweep", "upgrade")]
        con.executemany("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, 1, '')", ran)
    con.close()
    wk._start_due()  # probe and fallback are next in line
    wait_for(lambda: len(started) == 2)
    assert set(started) == {"probe", "fallback"}
    release.set()
    wait_for(lambda: not wk.runs)
    fallback = last(settings, "fallback")
    assert fallback["ok"] == 0 and fallback["message"] == "RuntimeError: no network"


def test_interrupted_runs_are_marked(w) -> None:
    wk, _, _, settings = w
    con = db.connect(settings.db_path)
    with con:
        con.execute("INSERT OR REPLACE INTO jobs (name, started, finished, ok, message) "
                    "VALUES ('sync', '2026-09-29T10:00:00', NULL, NULL, '')")  # fmt: skip
    con.close()
    wk.start()
    row = last(settings, "sync")
    assert row["ok"] == 0 and "interrupted" in row["message"]


def test_cancel(w) -> None:
    wk, started, _, settings = w

    def slow(r: worker.Run) -> str:
        started.append("sync")
        r.stop.wait(5)
        return "stopped early"

    worker.FUNCTIONS["sync"] = slow
    wk.trigger("sync")
    wk._start_due()
    wait_for(lambda: "sync" in wk.runs)
    assert wk.cancel("sync") and not wk.cancel("sweep")
    wait_for(lambda: (row := last(settings, "sync")) is not None and row["finished"] is not None)
    assert last(settings, "sync")["message"] == "cancelled: stopped early"
