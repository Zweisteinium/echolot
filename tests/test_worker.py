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
    wait_for(lambda: {"sync", "soundcloud", "library"} <= set(started))
    assert {"sync", "soundcloud", "library"} <= set(started)  # Spotify lists, web, local
    assert {"spotify", "web", "local"} <= {r.job.resource for r in wk.runs.values()}
    assert sum(r.job.resource == "soulseek" for r in wk.runs.values()) <= 1  # one at a time
    release.set()
    wait_for(lambda: not wk.runs)
    started.clear()
    release.clear()
    con = db.connect(settings.db_path)
    with con:  # sweep and upgrade run at fixed times: one may be due at this hour, so they just ran; YouTube
        now = datetime.now().isoformat(timespec="seconds")  # lists waited behind SoundCloud's: they just ran too
        ran = [(n, now, now) for n in ("sweep", "upgrade", "youtube")]
        con.executemany("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, 1, '')", ran)
    con.close()
    wk._start_due()  # the fallback is next in line
    wait_for(lambda: len(started) == 1)
    assert started == ["fallback"]
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


def test_the_upgrade_gives_way_and_goes_on_after(w) -> None:
    """New songs do not wait for a long upgrade: New Spotify songs starts right away while the upgrade ends
    its songs in progress, then the upgrade goes on with the songs it left, as started (Run now: whatever
    their wait). Spotify lists (no Soulseek) does not stop it."""
    wk, _, _, settings = w
    con = db.connect(settings.db_path)
    with con:  # nothing due by the schedule
        options.update(con, options.Jobs, paused=False)
        now = datetime.now().isoformat(timespec="seconds")
        con.executemany(
            "INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, 1, '')", [(n, now, now) for n in worker.FUNCTIONS]
        )
    con.close()
    ran: list[tuple[str, int | None, str, frozenset[str]]] = []
    in_progress = threading.Event()

    def upgrade(r: worker.Run) -> str:
        ran.append(("upgrade", r.budget, r.trigger, r.skip))
        if r.budget is None and r.give_way.wait(5):
            in_progress.wait(5)  # its songs in progress end
            r.handled, r.left = {"spotify:a", "spotify:b"}, 7
        return "upgrade done"

    def search_new(r: worker.Run) -> str:
        ran.append(("search_new", r.budget, r.trigger, r.skip))
        return "searched"

    worker.FUNCTIONS["upgrade"] = upgrade
    worker.FUNCTIONS["search_new"] = search_new
    worker.FUNCTIONS["sync"] = lambda r: "lists checked"  # at once (the fixture's waits up to 5 s: a race in CI)
    wk.trigger("upgrade")
    wk._start_due()
    wait_for(lambda: ran == [("upgrade", None, "manual", frozenset())])
    wk.trigger("sync")
    wk._start_due()  # Spotify lists has its own queue: the upgrade goes on
    assert not wk.runs["upgrade"].give_way.is_set()
    wk.trigger("search_new")
    wk._start_due()  # new songs, Soulseek busy: the upgrade gives way, the new songs start beside its last ones
    wait_for(lambda: ("search_new", None, "manual", frozenset()) in ran)
    assert wk.runs["upgrade"].give_way.is_set()
    wk._start_due()  # the upgrade does not start again while it ends
    in_progress.set()
    wait_for(lambda: not wk.runs)
    done = frozenset({"spotify:a", "spotify:b"})
    assert wk.resume == {"upgrade": (7, "manual", done)}
    wk._start_due()
    wait_for(lambda: len(ran) == 3)
    assert ran[2] == ("upgrade", 7, "manual", done)  # without the songs it did
    wait_for(lambda: not wk.runs)
    assert wk.resume == {}


def test_pausing_ends_scheduled_runs_after_their_songs(w) -> None:
    """A pause (as a deploy does) makes the runs give way, also one started by hand before (the covers can
    take hours); one started by hand while paused goes on."""
    wk, _, release, settings = w  # paused
    wk.trigger("sync")
    wk._start_due()
    wait_for(lambda: "sync" in wk.runs)
    con = db.connect(settings.db_path)
    with con:
        options.update(con, options.Jobs, paused=False)
        con.execute("DELETE FROM jobs WHERE name != 'sync'")  # never run: due
    con.close()
    wk.trigger("covers")
    wk._start_due()
    wait_for(lambda: {"soundcloud", "library", "covers"} <= set(wk.runs))
    con = db.connect(settings.db_path)
    with con:
        options.update(con, options.Jobs, paused=True)
    con.close()
    wk._start_due()
    assert all(wk.runs[n].give_way.is_set() for n in ("soundcloud", "library", "covers"))
    assert wk.runs["sync"].trigger == "manual" and not wk.runs["sync"].give_way.is_set()
    release.set()


def test_only_a_more_urgent_job_makes_one_give_way(w) -> None:
    """The evening search is not stopped by a due FLAC upgrade (less urgent), the full upgrade is."""
    wk, _, release, settings = w
    con = db.connect(settings.db_path)
    with con:
        options.update(con, options.Jobs, paused=False)
        now = datetime.now().isoformat(timespec="seconds")
        con.executemany(
            "INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, 1, '')", [(n, now, now) for n in worker.FUNCTIONS]
        )
    con.close()
    wk.trigger("sweep")
    wk._start_due()
    wait_for(lambda: "sweep" in wk.runs)
    wk.trigger("upgrade")
    wk._start_due()
    assert not wk.runs["sweep"].give_way.is_set()
    release.set()
    wait_for(lambda: not wk.runs)
    release.clear()
    wk.requested.clear()
    wk.trigger("upgrade_all")
    wk._start_due()
    wait_for(lambda: "upgrade_all" in wk.runs)
    wk.trigger("upgrade")
    wk._start_due()
    assert wk.runs["upgrade_all"].give_way.is_set()
    release.set()


def test_a_run_that_gave_way_goes_on_after_a_restart(w) -> None:
    """A deploy pauses the jobs: the runs give way and Echolot restarts; the new worker goes on with them,
    without the songs they did, once the jobs are resumed."""
    wk, started, release, settings = w  # paused
    wk.resume = {"fallback": (50, "manual", frozenset({"spotify:a", "spotify:b"}))}
    wk._save_resume()
    again = worker.Worker(settings, Vault.from_env(settings.data_dir, {}))
    again.start()
    try:
        assert again.resume == wk.resume
        again._start_due()
        assert "fallback" not in again.runs  # paused: it waits
        con = db.connect(settings.db_path)
        with con:  # resumed, nothing else due
            options.update(con, options.Jobs, paused=False)
            now = datetime.now().isoformat(timespec="seconds")
            ran = [(n, now, now) for n in worker.FUNCTIONS]
            con.executemany("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, 1, '')", ran)
        con.close()
        again._start_due()
        wait_for(lambda: "fallback" in started)
        run = again.runs["fallback"]
        assert (run.trigger, run.budget, run.skip) == ("manual", 50, frozenset({"spotify:a", "spotify:b"}))
        con = db.connect(settings.db_path)
        assert db.get_meta(con, worker.RESUME) == "{}"  # taken up: a later restart does not run it again
        con.close()
    finally:
        release.set()
        again.stop()


def test_a_list_check_runs_beside_the_search_and_claims_only_to_download(w) -> None:
    """New SoundCloud songs every few minutes does not stop the YouTube & SoundCloud search: it checks its
    lists beside it, and only to download new songs does the search give way (Run.claim). YouTube lists,
    due meanwhile, waits for the check (one beside is enough)."""
    wk, started, release, settings = w
    con = db.connect(settings.db_path)
    with con:  # nothing due by the schedule
        options.update(con, options.Jobs, paused=False)
        now = datetime.now().isoformat(timespec="seconds")
        con.executemany(
            "INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, 1, '')", [(n, now, now) for n in worker.FUNCTIONS]
        )
    con.close()
    checking, claim = threading.Event(), threading.Event()

    def soundcloud(r: worker.Run) -> str:
        checking.set()
        if claim.wait(5):
            r.claim()  # new songs to download
        return "1 new song"

    worker.FUNCTIONS["soundcloud"] = soundcloud
    wk.trigger("fallback")
    wk._start_due()
    wait_for(lambda: "fallback" in started)
    search = wk.runs["fallback"]
    wk.trigger("soundcloud")
    wk._start_due()
    wait_for(checking.is_set)
    assert "soundcloud" in wk.runs and not search.give_way.is_set()  # beside, the search goes on
    wk.trigger("youtube")
    wk._start_due()
    assert "youtube" not in wk.runs  # waits for the check
    claim.set()
    assert search.give_way.wait(5)  # downloads: the search gives way after its songs in progress
    release.set()
