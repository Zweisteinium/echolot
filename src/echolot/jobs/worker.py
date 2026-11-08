"""The worker: starts due jobs (schedule.py), each in its own thread.

Jobs sharing a resource run one at a time: a due job waits and keeps its turn. A running job of a lower
priority gives way instead: it ends after its songs in progress, the urgent one starts at once, and the
one that gave way goes on later as it was started, without the songs it did (Run.skip). That survives a
restart (meta "resume"): a deploy pauses the jobs, so they give way and go on when Echolot is back.
Pausing stops new starts and makes runs give way. A Run now while paused runs anyway; the jobs it starts
(Run.after) wait for the resume and run as scheduled (trigger "after": the songs' waits count).
A run still open in the jobs table at start was interrupted.
"""

import collections
import datetime
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from echolot import db
from echolot.config import Settings
from echolot.jobs import acquire, availability, bandcamp, covers, discogs, lists, lossycheck, schedule
from echolot.library import catalog, filing, history, playlists, recordings, review, stars
from echolot.services import navidrome, slskd, soulseek
from echolot.settings import auth, options, sources
from echolot.settings.vault import Vault

log = logging.getLogger(__name__)
TICK = 20  # seconds between looks at the schedule
RESUME = "resume"  # meta key of Worker.resume
EXPECTED = (soulseek.DaemonError,)  # reported in one line, without a traceback


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class Run:
    """One run of a job: what the job functions get."""

    def __init__(self, job: schedule.JobInfo, settings: Settings, vault: Vault, trigger: str) -> None:
        self.job, self.settings, self.vault, self.trigger = job, settings, vault, trigger
        self.stop = threading.Event()
        self.progress = ""  # one line: what it does now
        self.done = self.total = 0  # for the progress bar; total 0: unknown
        self.log: collections.deque[tuple[str, str]] = collections.deque(maxlen=200)  # (time, line), newest last
        self.started = _now()
        self.after: set[str] = set()  # jobs to start when this one ends
        self.give_way = threading.Event()  # end after the songs in progress: a more urgent job is due, or paused
        self.budget: int | None = None  # songs left by the run that gave way (None: the job's own batch)
        self.left = 0  # songs left when it gave way
        self.skip: frozenset[str] = frozenset()  # songs (or files) the runs it goes on from did
        self.handled: set[str] = set()  # songs this run did: handed on when it gives way
        self.fetching: dict[str, dict[str, Any]] = {}  # song key -> acquire.track's state
        self.while_paused = False  # started by hand while paused: the pause leaves it alone
        self.only: set[int] | None = None  # by hand for these users' songs (None: everyone's)
        self.claim_resource: Callable[[Run], None] = lambda run: None  # Worker._claim

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

    def say(self, progress: str, done: int | None = None, total: int | None = None) -> None:
        """`done` of `total` count the run's own songs; the bar adds the skipped ones."""
        self.progress = progress
        if done is not None:
            self.done, self.total = len(self.skip) + done, len(self.skip) + total if total else 0

    def claim(self) -> None:
        """For a JobInfo.beside job: its work needs the resource now, the less urgent runs give way."""
        self.claim_resource(self)

    def todo(self, rows: list, key: Callable[[Any], str] = lambda r: r["key"]) -> list:
        """`rows` without those the runs this one goes on from did."""
        return [r for r in rows if key(r) not in self.skip] if self.skip else rows

    def of(self, n: int, total: int) -> str:
        """'n of total' counted on from the skipped songs: 14 of 63, not 1 of 50."""
        return f"{len(self.skip) + n} of {len(self.skip) + total}"

    def note(self, line: str) -> None:
        """A line for the run's live log on the jobs page."""
        self.log.append((datetime.datetime.now().strftime("%H:%M:%S"), line))


def upkeep(run: Run) -> str:
    """The library upkeep job: users, review decisions, rescan, merges, playlists, slskd's share, hearts,
    the hourly snapshot and the daily purge."""
    con = run.connect()
    try:
        parts = []
        if svc := navidrome.service(con, run.vault):
            try:
                if changed := auth.sync_users(con, svc.users()):
                    parts.append("users: " + "; ".join(changed))
            except navidrome.NavidromeError as e:
                log.info("Navidrome's users: %s", e)
        if adopted := sources.adopt(con):
            parts.append(f"adopted: {adopted}")
        if applied := review.apply_due(run, con):
            parts.append(f"review: {'; '.join(applied)}")
        if compared := review.compare_open(con, run.paths.music):
            parts.append(f"{compared} review items compared with your copy")
        if merged := recordings.merge_edits(con, run.paths):
            parts.append(f"other edits: {'; '.join(merged)}")
        parts.append(catalog.refresh(con, run.paths.tracks))
        if linked := recordings.link_isrc(con, run.paths):
            catalog.match_songs(con)
            parts.append(f"{linked} linked by ISRC")
        if merged := recordings.merge_same(con, run.paths):
            catalog.match_songs(con)
            parts.append(f"same audio: {'; '.join(merged)}")
        parts.append(playlists.write(con, run.paths.playlists))
        if reshared := _reshare(run, con):
            parts.append(reshared)
        if svc := navidrome.service(con, run.vault):
            try:
                if owners := playlists.sync_owners(con, svc, run.paths.playlists):
                    parts.append(owners)
            except navidrome.NavidromeError as e:
                log.info("Navidrome's playlists: %s", e)
            try:
                if hearts := stars.sync(con, svc, run.paths.tracks.name):
                    parts.append(hearts)
            except navidrome.NavidromeError as e:
                log.info("Navidrome's hearts: %s", e)
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


RESHARE_SECONDS = 3600


def _reshare(run: Run, con: sqlite3.Connection) -> str:
    """slskd scans its share only at its start: after a library change it is asked to rescan (at most every
    RESHARE_SECONDS), else it offers the old names of upgraded files and their downloads fail."""
    if not run.vault.get(con, slskd.SECRET):
        return ""  # no slskd set up
    state = "{}:{}".format(*con.execute("SELECT count(*), coalesce(max(mtime), 0) FROM files").fetchone())
    last = float(db.get_meta(con, "shares_scanned", "0") or 0)
    if state == db.get_meta(con, "shares_state") or time.time() - last < RESHARE_SECONDS:
        return ""
    try:
        client = slskd.connect(con, run.vault, options.get(con, options.Soulseek), timeout=15)
        if not client.rescan_shares():
            return ""
    except soulseek.DaemonError as e:  # the next upkeep asks again
        log.info("slskd's share: %s", e)
        return ""
    with con:
        db.set_meta(con, "shares_state", state)
        db.set_meta(con, "shares_scanned", str(time.time()))
    return "slskd scans its share again"


FUNCTIONS: dict[str, Callable[[Run], str]] = {
    "sync": acquire.sync,
    "search_new": acquire.search_new,
    "sweep": acquire.sweep,
    "upgrade": acquire.upgrade,
    "upgrade_all": acquire.upgrade_all,
    "soundcloud": lists.soundcloud,
    "youtube": lists.youtube,
    "fallback": acquire.fallback,
    "covers": covers.run,
    "availability": availability.check,
    "library": upkeep,
    "lossy": lossycheck.run,
    "bandcamp": bandcamp.run,
    "discogs": discogs.run,
}


class Worker:
    def __init__(self, settings: Settings, vault: Vault) -> None:
        self.settings, self.vault = settings, vault
        self.runs: dict[str, Run] = {}  # running, by job name
        self.requested: dict[str, set[int] | None] = {}  # job -> for these users' songs (None: all)
        self.followups: set[str] = set()  # requested by another job (Run.after), not by hand
        self.resume: dict[str, tuple[int, str, frozenset[str]]] = {}  # gave way: songs left, trigger, done
        self.last: dict[str, Run] = {}  # each job's last finished run (its log stays on the jobs page)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        con = db.connect(self.settings.db_path)
        try:
            saved = json.loads(db.get_meta(con, RESUME) or "{}")
            self.resume = {n: (left, how, frozenset(skip)) for n, (left, how, skip) in saved.items() if n in FUNCTIONS}
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

    def trigger(self, name: str, only: int | None = None) -> bool:
        """Start a job as soon as its resource is free, also while paused; `only`: for that user's songs
        (a request for everyone's covers it)."""
        if name not in FUNCTIONS:
            return False
        with self._lock:
            self.followups.discard(name)
            if only is None or self.requested.get(name, set()) is None:
                self.requested[name] = None
            else:
                self.requested[name] = (self.requested.get(name) or set()) | {only}
        self._wake.set()
        return True

    def state(self) -> tuple[dict[str, Run], set[str]]:
        """The runs in progress and the jobs asked for, at one moment."""
        with self._lock:
            return dict(self.runs), set(self.requested)

    def finished(self) -> dict[str, Run]:
        """Since Echolot started."""
        with self._lock:
            return dict(self.last)

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
            for r in self.runs.values():
                if paused and not r.while_paused:
                    r.give_way.set()
            # per resource the run holding it: the most urgent one not giving way
            running: dict[str, Run] = {}
            for r in self.runs.values():
                held = running.get(r.job.resource)
                if (
                    held is None
                    or held.give_way.is_set()
                    or (not r.give_way.is_set() and r.job.priority > held.job.priority)
                ):
                    running[r.job.resource] = r
            for job in schedule.JOBS:
                if job.name in self.runs:
                    continue
                requested = job.name in self.requested
                followup = requested and job.name in self.followups
                if followup and paused:
                    continue  # a deploy waits for no new work
                by_hand = requested and not followup
                resume = not paused and job.name in self.resume
                started = datetime.datetime.fromisoformat(last[job.name]) if last.get(job.name) else None
                if not (requested or resume or (not paused and schedule.due(rules[job.name], started, now))):
                    continue
                other = running.get(job.resource)
                if other and other.job.priority >= job.priority:
                    continue
                if other and not job.beside:
                    other.give_way.set()
                only = self.requested.pop(job.name, None)
                self.followups.discard(job.name)
                budget, how, skip = self.resume.pop(job.name, (None, "schedule", frozenset()))
                if budget is not None:
                    self._save_resume()
                # manual: whatever the songs' waits; after: started by another job, with their waits
                trigger = (
                    "manual"
                    if by_hand or how == "manual"
                    else "after"
                    if followup
                    else "resume"
                    if resume
                    else "schedule"
                )
                run = Run(job, self.settings, self.vault, trigger)
                run.budget, run.only, run.skip = budget, only if by_hand else None, skip
                run.while_paused = paused
                run.claim_resource = self._claim
                self.runs[job.name] = running[job.resource] = run
                threading.Thread(target=self._run, args=(run,), name=job.name, daemon=True).start()

    def _claim(self, run: Run) -> None:
        """Run.claim."""
        with self._lock:
            for r in self.runs.values():
                if r is not run and r.job.resource == run.job.resource and r.job.priority < run.job.priority:
                    r.give_way.set()

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
            except EXPECTED as e:
                message, ok = str(e), False
                log.warning("%s: %s", name, e)
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
                self.last[name] = run
                for after in run.after - {name}:  # for everyone, as scheduled
                    if after not in self.requested:  # one asked for by hand stays so
                        self.followups.add(after)
                    self.requested[after] = None
                if run.left:
                    self.resume[name] = (run.left, run.trigger, run.skip | run.handled)
                self._save_resume()
            self._wake.set()

    def _save_resume(self) -> None:
        """Store self.resume for a restart; with the lock held."""
        saved = json.dumps({n: [left, how, sorted(skip)] for n, (left, how, skip) in self.resume.items()})
        con = db.connect(self.settings.db_path)
        try:
            with con:
                db.set_meta(con, RESUME, saved)
        finally:
            con.close()
