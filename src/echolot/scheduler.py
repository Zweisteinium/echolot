"""In-process scheduler: runs each job at its interval (and on demand) in one background thread,
one job at a time, and records the last run of each in the jobs table."""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from sqlite3 import Connection

from echolot import db

log = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    interval: float  # seconds
    run: Callable[[Connection], str]  # returns a one-line summary
    next_run: float = 0.0  # time.monotonic(); 0 = as soon as possible


class Scheduler:
    def __init__(self, db_path: Path, jobs: list[Job]) -> None:
        self.db_path = db_path
        self.jobs = {job.name: job for job in jobs}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=30)

    def trigger(self, name: str) -> bool:
        """Run a job as soon as the current one is done."""
        job = self.jobs.get(name)
        if job is None:
            return False
        job.next_run = 0.0
        self._wake.set()
        return True

    def next_run_in(self, name: str) -> float | None:
        job = self.jobs.get(name)
        return None if job is None else max(job.next_run - time.monotonic(), 0.0)

    def _loop(self) -> None:
        con = db.connect(self.db_path)
        try:
            while not self._stop.is_set():
                for job in self.jobs.values():
                    if job.next_run <= time.monotonic() and not self._stop.is_set():
                        self.run(con, job)
                wait = min(job.next_run for job in self.jobs.values()) - time.monotonic()
                self._wake.wait(timeout=max(wait, 0.1))
                self._wake.clear()
        finally:
            con.close()

    def run(self, con: Connection, job: Job) -> None:
        started = datetime.now().isoformat(timespec="seconds")
        with con:
            con.execute(
                "INSERT INTO jobs (name, started, finished, ok, message) VALUES (?, ?, NULL, NULL, '') "
                "ON CONFLICT (name) DO UPDATE SET started = excluded.started, finished = NULL",
                (job.name, started),
            )
        try:
            message, ok = job.run(con), True
            log.info("%s: %s", job.name, message)
        except Exception as e:
            message, ok = f"{type(e).__name__}: {e}", False
            log.exception("%s failed", job.name)
        job.next_run = time.monotonic() + job.interval
        with con:
            con.execute(
                "UPDATE jobs SET finished = ?, ok = ?, message = ? WHERE name = ?",
                (datetime.now().isoformat(timespec="seconds"), int(ok), message, job.name),
            )
