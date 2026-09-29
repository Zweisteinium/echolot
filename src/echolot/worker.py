"""The worker: starts the jobs when they are due (schedule.py), each in its own thread. Jobs that share a
resource (Soulseek, the home IP, the library upkeep) run one at a time; a due job waits for the running
one and keeps its turn. The last run of each job is in the jobs table; a run that was going when Echolot
stopped is marked interrupted at the next start. Pausing (settings section jobs) stops new starts;
a job started by hand ("Run now") runs anyway.
"""

import datetime
import logging
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from echolot import (
    acquire,
    db,
    filing,
    history,
    library,
    lists,
    options,
    playlists,
    review,
    schedule,
)
from echolot.config import Settings
from echolot.vault import Vault

log = logging.getLogger(__name__)
TICK = 20  # seconds between looks at the schedule


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class Run:
    """One run of a job: what the job functions get."""

    def __init__(
        self, job: schedule.JobInfo, settings: Settings, vault: Vault, trigger: str
    ) -> None:
        self.job, self.settings, self.vault, self.trigger = job, settings, vault, trigger
        self.stop = threading.Event()
        self.progress = ""
        self.started = _now()
        self.after: set[str] = set()  # jobs to start when this one ends

    @property
    def paths(self) -> filing.Paths:
        if self.settings.library_dir is None:
            raise RuntimeError("no library configured (ECHOLOT_LIBRARY_DIR)")
        return filing.Paths(self.settings.library_dir.parent)

    @property
    def data(self) -> Path:
        return self.settings.data_dir

    def connect(self) -> sqlite3.Connection:
        return db.connect(self.settings.db_path)

    def say(self, progress: str) -> None:
        self.progress = progress


def upkeep(run: Run) -> str:
    """Apply due review decisions, rescan the library, write the playlists, store the hourly snapshot
    and, once a day, empty the replaced/ and review/ days older than 30 days."""
    con = run.connect()
    try:
        parts = []
        if applied := review.apply_due(run, con):
            parts.append(f"review: {'; '.join(applied)}")
        parts.append(library.refresh(con, run.paths.tracks))
        parts.append(playlists.write(con, run.paths.playlists))
        if history.snapshot(con):
            parts.append("snapshot stored")
        today = datetime.date.today().isoformat()
        if db.get_meta(con, "purged") != today:
            if gone := filing.purge(run.paths):
                parts.append(f"purged {', '.join(gone)}")
            with con:
                db.set_meta(con, "purged", today)
        return "; ".join(parts)
    finally:
        con.close()


FUNCTIONS: dict[str, Callable[[Run], str]] = {
    "sync": acquire.sync,
    "sweep": acquire.sweep,
    "upgrade": acquire.upgrade,
    "probe": acquire.probe,
    "soundcloud": lists.soundcloud,
    "fallback": acquire.fallback,
    "library": upkeep,
}


class Worker:
    def __init__(self, settings: Settings, vault: Vault) -> None:
        self.settings, self.vault = settings, vault
        self.runs: dict[str, Run] = {}  # running, by job name
        self.requested: set[str] = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        con = db.connect(self.settings.db_path)
        try:
            with con:
                con.execute("UPDATE jobs SET finished = ?, ok = 0, message = 'interrupted (Echolot stopped)' "
                            "WHERE finished IS NULL", (_now(),))  # fmt: skip
        finally:
            con.close()
        self._thread = threading.Thread(target=self._loop, name="worker", daemon=True)
        self._thread.start()

    def stop(self, wait: float = 30) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            for run in self.runs.values():
                run.stop.set()
        if self._thread:
            self._thread.join(timeout=wait)

    def trigger(self, name: str) -> bool:
        """Start a job as soon as its resource is free (also while paused)."""
        if name not in FUNCTIONS:
            return False
        with self._lock:
            self.requested.add(name)
        self._wake.set()
        return True

    def cancel(self, name: str) -> bool:
        with self._lock:
            run = self.runs.get(name)
        if run:
            run.stop.set()
        return run is not None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._start_due()
            except Exception:
                log.exception("worker")
            self._wake.wait(TICK)
            self._wake.clear()

    def _start_due(self) -> None:
        con = db.connect(self.settings.db_path)
        try:
            paused = options.get(con, options.Jobs).paused
            rules = schedule.rules(con)
            last = {r["name"]: r["started"] for r in con.execute("SELECT name, started FROM jobs")}
        finally:
            con.close()
        now = datetime.datetime.now()
        with self._lock:
            busy = {r.job.resource for r in self.runs.values()}
            for job in schedule.JOBS:
                if job.resource in busy or job.name in self.runs:
                    continue
                requested = job.name in self.requested
                started = (
                    datetime.datetime.fromisoformat(last[job.name]) if last.get(job.name) else None
                )
                if requested or (not paused and schedule.due(rules[job.name], started, now)):
                    self.requested.discard(job.name)
                    run = Run(job, self.settings, self.vault, "manual" if requested else "schedule")
                    self.runs[job.name] = run
                    busy.add(job.resource)
                    threading.Thread(
                        target=self._run, args=(run,), name=job.name, daemon=True
                    ).start()

    def _run(self, run: Run) -> None:
        name = run.job.name
        con = db.connect(self.settings.db_path)
        try:
            with con:
                con.execute(
                    "INSERT INTO jobs (name, started, finished, ok, message) VALUES (?, ?, NULL, NULL, '') "
                    "ON CONFLICT (name) DO UPDATE SET started = excluded.started, finished = NULL",
                    (name, run.started),
                )
            try:
                message, ok = FUNCTIONS[name](run), True
                if run.stop.is_set() and not self._stop.is_set():
                    message = f"cancelled: {message}"
                log.info("%s: %s", name, message)
            except Exception as e:
                message, ok = f"{type(e).__name__}: {e}", False
                log.exception("%s failed", name)
            with con:
                con.execute("UPDATE jobs SET finished = ?, ok = ?, message = ? WHERE name = ?",
                            (_now(), int(ok), message[:2000], name))  # fmt: skip
        finally:
            con.close()
            with self._lock:
                self.runs.pop(name, None)
                self.requested |= run.after - {name}
            self._wake.set()
