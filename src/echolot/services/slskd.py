"""Soulseek through slskd (a full Soulseek client: it shares, keeps private messages), over its HTTP API (v0,
slskd 0.26): searches and their responses, downloads in batches with a folder of their own, cancel. The same
calls as soulseek.Daemon (status, search, wait, results, download, transfer, cancel), so the songs job uses
either backend.

What Sockseek does for its daemon, Echolot does here: the search text (without feat. credits; the desperate
search repeats a search without results with the title alone and the artist alone), the filters and the
ranking of soulseek.SEARCH, Soulseek's search limit, giving a download up that makes no progress
(maxStaleTime), and peers whose downloads failed: ranked last after one failure, left out after two (Sockseek's
fails-to-downrank 1, fails-to-ignore 2; here for PEER_MEMORY). slskd logs in with an API key or with its web login
(user and password).
"""

import contextlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from echolot.library import audio, rules
from echolot.services.soulseek import Candidate, DaemonError, Lost, Transfer

OTHER_USER = "slskd saved it as another user: run slskd as Echolot's user (user: PUID:PGID in its compose file)"
SECRET = "slskd.secret"  # the vault's name of slskd's password (with a user) or API key (without)
MOVE_WAIT = 60  # s a reported download may take to appear in its folder
SEARCH_LIMIT = (34, 220)  # Soulseek allows about 34 searches per 220 s: kept by every client of this process
# ms after the last response a search counts as complete: slskd's searchTimeout goes to Soulseek.NET as is
# (milliseconds, though slskd's API notes call it seconds); 15 s is slskd's default
SEARCH_IDLE_MS = 15_000
FAST = 1_000_000  # bytes/s from which a peer counts as fast (the ranking: fast before slow)
PEER_MEMORY = 6 * 3600  # s a peer's failed downloads count against it
BUSY_TRIES = 10  # slskd answers 429 while it handles another search or enqueue: tried again this often
_searches: deque[float] = deque()
_searches_lock = threading.Lock()
_peers: dict[str, tuple[int, float]] = {}  # user -> (downloads done minus failed, when that last changed)
_peers_lock = threading.Lock()
_post_lock = threading.Lock()  # one search or enqueue at a time (slskd: "Only one concurrent operation")
_FT = re.compile(r"\s*[\(\[]\s*(?:feat|ft|featuring|with)\.?\s[^\)\]]*[\)\]]|\s+(?:feat|ft|featuring)\.?\s.*$", re.I)


def search_text(*parts: str) -> str:
    """Soulseek's search words: without feat. credits and the punctuation peers' indexes ignore (a leading
    '-' would exclude a word)."""
    words = " ".join(_FT.sub("", p or "") for p in parts)
    return " ".join(re.sub(r"[^\w']+", " ", words).split())


def _throttle(stop: threading.Event | None = None) -> None:
    """Wait for a free search slot (SEARCH_LIMIT), shared by all threads of this process."""
    most, window = SEARCH_LIMIT
    while True:
        with _searches_lock:
            now = time.monotonic()
            while _searches and now - _searches[0] > window:
                _searches.popleft()
            if len(_searches) < most:
                _searches.append(now)
                return
            wait = window - (now - _searches[0]) + 0.1
        if stop is not None and stop.wait(min(wait, 5)):
            raise DaemonError("stopped while waiting for a search slot")
        if stop is None:
            time.sleep(min(wait, 5))


def _peer(user: str) -> int:
    """Downloads from the peer done minus failed (failures older than PEER_MEMORY forgotten)."""
    with _peers_lock:
        n, at = _peers.get(user, (0, 0.0))
    return 0 if n < 0 and time.monotonic() - at > PEER_MEMORY else n


def _record(user: str, ok: bool) -> None:
    n = _peer(user)
    with _peers_lock:
        _peers[user] = (n + (1 if ok else -1), time.monotonic())


class Slskd:
    def __init__(self, url: str, downloads: Path, user: str = "", secret: str = "", timeout: float = 30) -> None:
        """downloads: slskd's downloads folder, as Echolot sees it. user and secret: slskd's web login, or no
        user and the secret an API key."""
        self.url, self.downloads, self.timeout = url.rstrip("/"), downloads, timeout
        self.user, self.secret = user, secret
        self._token = ""
        self._jobs: dict[str, dict[str, Any]] = {}  # search id -> its query, settings and the searches made
        self._done_at: dict[str, float] = {}  # download job -> when slskd first reported it done
        self._moved: dict[str, tuple[int, float]] = {}  # download job -> (bytes, when they last changed)

    # ------------------------------------------------------------ HTTP

    def _login(self) -> None:
        body = json.dumps({"username": self.user, "password": self.secret}).encode()
        req = urllib.request.Request(
            self.url + "/api/v0/session", data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                self._token = json.loads(r.read()).get("token") or ""
        except urllib.error.HTTPError as e:
            raise DaemonError(f"slskd refused the login of {self.user!r} (HTTP {e.code})") from e
        except (OSError, TimeoutError) as e:
            raise DaemonError(f"slskd at {self.url} not reachable: {e}") from e

    def _call(self, method: str, path: str, body: object = None, again: bool = True, busy: int = 0) -> Any:
        headers = {"Content-Type": "application/json"}
        if self.user:
            if not self._token:
                self._login()
            headers["Authorization"] = f"Bearer {self._token}"
        elif self.secret:
            headers["X-API-Key"] = self.secret
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + "/api/v0" + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 401 and self.user and again:  # the session ran out: log in again
                self._token = ""
                return self._call(method, path, body, again=False)
            if e.code == 429 and busy < BUSY_TRIES:  # slskd takes one search or enqueue at a time
                time.sleep(0.5 * (busy + 1))
                return self._call(method, path, body, again, busy + 1)
            if e.code == 404:
                raise Lost(f"{method} {path}: not found") from e
            detail = e.read().decode(errors="replace")[:300]
            raise DaemonError(f"slskd {method} {path}: HTTP {e.code} {detail}") from e
        except (OSError, TimeoutError) as e:
            raise DaemonError(f"slskd at {self.url} not reachable: {e}") from e
        return json.loads(raw) if raw else None

    # ------------------------------------------------------------ the calls of soulseek.Daemon

    def status(self) -> dict[str, Any]:
        """{ready, state, user, version}; ready: logged in to Soulseek."""
        app = self._call("GET", "/application") or {}
        server = app.get("server") or {}
        state = str(server.get("state") or "")
        return {
            "ready": bool(server.get("isLoggedIn")),
            "state": state,
            "flags": [s.strip() for s in state.split(",")],
            "version": (app.get("version") or {}).get("current") or "",
            "started": "",
            "user": (app.get("user") or {}).get("username") or "",
        }

    def search(self, artist: str, title: str, length: int, settings: dict) -> str:
        """Start a search for the song; its id (the first search's; a desperate one may follow, see wait)."""
        job = str(uuid.uuid4())
        self._jobs[job] = {
            "artist": artist,
            "title": title,
            "length": int(length or 0),
            "settings": settings,
            "searches": [],
            "pending": [search_text(artist, title)],
        }
        if (settings.get("search") or {}).get("desperateSearch"):
            self._jobs[job]["desperate"] = [search_text(title), search_text(artist)]
        self._next(job)
        return job

    def _next(self, job: str, stop: threading.Event | None = None) -> bool:
        """Start the job's next pending search; False if there is none."""
        j = self._jobs[job]
        if not j["pending"]:
            return False
        text = j["pending"].pop(0)
        _throttle(stop)
        sid = job if not j["searches"] else str(uuid.uuid4())
        body = {
            "id": sid,
            "searchText": text,
            "searchTimeout": SEARCH_IDLE_MS,
            "responseLimit": 100,
            "fileLimit": 10000,
            "filterResponses": True,
            "minimumResponseFileCount": 1,
        }
        with _post_lock:
            self._call("POST", "/searches", body)
        j["searches"].append(sid)
        return True

    def wait(self, job_id: str, stop: threading.Event, deadline: float, every: float = 1.0) -> dict:
        """Wait until the job's searches have ended; a desperate search follows one that found nothing that
        fits."""
        from echolot.services.soulseek import Cancelled

        j = self._jobs.get(job_id)
        if j is None:
            raise Lost(f"search {job_id}: unknown")
        while True:
            sid = j["searches"][-1]
            state = str((self._call("GET", f"/searches/{sid}") or {}).get("state") or "")
            if "Completed" in state:
                if not j["pending"] and j.get("desperate") and not self.results(job_id):
                    j["pending"], j["desperate"] = j.pop("desperate"), None
                if not self._next(job_id, stop):
                    return {"state": state}
            elif stop.is_set() or time.monotonic() > deadline:
                self.cancel_search(job_id)
                raise Cancelled(job_id)
            else:
                stop.wait(every)

    def results(self, job_id: str) -> list[Candidate]:
        """The files the job's searches found that pass soulseek.SEARCH's necessary conditions, best first by
        its preferred ones, then free slot, speed and queue; a peer whose download failed last, one that failed
        twice not at all."""
        j = self._jobs.get(job_id)
        if j is None:
            raise Lost(f"search {job_id}: unknown")
        search = j["settings"].get("search") or {}
        need, want = search.get("necessaryCond") or {}, search.get("preferredCond") or {}
        tolerance = int(need.get("lengthTolerance") or 0)
        only = [f.lower() for f in _values(need.get("formats"))]
        found: dict[tuple[str, str], tuple[tuple, Candidate]] = {}
        for sid in j["searches"]:
            for resp in self._call("GET", f"/searches/{sid}/responses") or []:
                for f in resp.get("files") or []:
                    c = _candidate(resp, f)
                    if f.get("isLocked") or (only and c.ext not in only) or _peer(c.user) <= -2:
                        continue
                    if tolerance and j["length"] and c.length and abs(c.length - j["length"]) > tolerance:
                        continue
                    found.setdefault((c.user, c.path), (_rank(c, j, want), c))
        ranked = sorted(found.values(), key=lambda rc: rc[0])
        for n, (_, c) in enumerate(ranked):
            c.rank = n
        return [c for _, c in ranked]

    def download(self, search_job: str, c: Candidate, parent_dir: str, settings: dict) -> str:
        """Start downloading one found file into parent_dir (relative to slskd's downloads folder); the job id."""
        sid = next(iter(self._jobs.get(search_job, {}).get("searches") or []), None)
        body = {
            "username": c.user,
            "files": [{"filename": c.path, "size": c.size}],
            "options": {"destination": parent_dir},
        }
        if sid:
            body["searchId"] = sid
        with _post_lock:
            r = self._call("POST", "/transfers/downloads/batches", body) or {}
        if r.get("failures") and not (r.get("batch") or {}).get("id"):
            raise DaemonError(f"slskd could not queue {c.path}: {r['failures'][0].get('message')}")
        batch = (r.get("batch") or {}).get("id") or ""
        stale = int((settings.get("search") or {}).get("maxStaleTime") or 0)
        job = json.dumps({"user": c.user, "file": c.path, "batch": batch, "dir": parent_dir, "stale": stale})
        self._moved[job] = (0, time.monotonic())
        return job

    def _find(self, job: dict) -> dict | None:
        """The job's transfer record (by user, file and batch)."""
        try:
            listing = self._call("GET", f"/transfers/downloads/{urllib.parse.quote(job['user'], safe='')}") or {}
        except Lost:
            return None
        for d in listing.get("directories") or []:
            for t in d.get("files") or []:
                if t.get("filename") == job["file"] and (not job["batch"] or t.get("batchId") == job["batch"]):
                    return t
        return None

    def transfer(self, job_id: str) -> Transfer:
        job = json.loads(job_id)
        t = self._find(job)
        state = str((t or {}).get("state") or "")
        done, total = int((t or {}).get("bytesTransferred") or 0), int((t or {}).get("size") or 0)
        if "Completed" not in state:  # (t None: not listed yet, a moment after the enqueue)
            seen, since = self._moved.get(job_id, (done, time.monotonic()))
            if done != seen:
                self._moved[job_id] = (done, time.monotonic())
            elif job.get("stale") and time.monotonic() - since > job["stale"] / 1000:
                self.cancel(job_id)  # queued at the peer, or the peer does not answer: as Sockseek, the next file
                self._moved.pop(job_id, None)
                _record(job["user"], False)
                return Transfer("failed", None, done, total, f"no progress for {job['stale'] // 1000} s")
            return Transfer("running", None, done, total, "")
        self._moved.pop(job_id, None)
        if "Succeeded" in state:
            folder = self.downloads / job["dir"]
            files = sorted(
                (p for p in folder.glob("*") if p.is_file() and p.suffix.lower().lstrip(".") in audio.AUDIO),
                key=lambda p: p.stat().st_mtime,
            )
            if files and not os.access(folder, os.W_OK):  # slskd runs as another user: the file can't move
                self._forget(job, t)
                return Transfer("failed", None, done, total, f"{OTHER_USER} (owner uid {folder.stat().st_uid})")
            if files:
                self._forget(job, t)
                _record(job["user"], True)
                return Transfer("done", str(files[-1]), done, total, "")
            # slskd reports success, then moves the file from its incomplete folder: wait for it a while
            first = self._done_at.setdefault(job_id, time.monotonic())
            if time.monotonic() - first < MOVE_WAIT:
                return Transfer("running", None, done, total, "")
            return Transfer("failed", None, done, total, f"downloaded, but not found in {folder}")
        reason = t.get("exception") or state.replace("Completed, ", "")
        self._forget(job, t)
        if "Cancelled" in state:
            return Transfer("cancelled", None, done, total, str(reason))
        _record(job["user"], False)
        return Transfer("failed", None, done, total, str(reason))

    def _forget(self, job: dict, t: dict) -> None:
        """Take the finished transfer off slskd's list (its file stays)."""
        user = urllib.parse.quote(job["user"], safe="")
        with contextlib.suppress(DaemonError):
            self._call("DELETE", f"/transfers/downloads/{user}/{t['id']}?remove=true")

    def cancel(self, job_id: str) -> None:
        try:
            job = json.loads(job_id)
        except ValueError:  # a search job
            self.cancel_search(job_id)
            return
        if (t := self._find(job)) is not None:
            self._forget(job, t)

    def cancel_search(self, job_id: str) -> None:
        for sid in (self._jobs.get(job_id) or {}).get("searches") or []:
            with contextlib.suppress(DaemonError):
                self._call("PUT", f"/searches/{sid}")  # stop it; its responses stay

    def close(self, job_id: str) -> None:
        """Delete the job's searches from slskd (they are kept for days otherwise)."""
        for sid in (self._jobs.pop(job_id, None) or {}).get("searches") or []:
            with contextlib.suppress(DaemonError):
                self._call("DELETE", f"/searches/{sid}")


def connect(con: Any, vault: Any, opts: Any, timeout: float = 30) -> Slskd:
    """The slskd of the settings (options.Soulseek) and its secret from the vault."""
    return Slskd(opts.slskd_url, Path(opts.slskd_downloads), opts.slskd_user, vault.get(con, SECRET) or "", timeout)


def _values(v: Any) -> list[str]:
    """A list setting of soulseek.SEARCH ({"replace": [...]} patches Sockseek's own list)."""
    if isinstance(v, dict):
        v = v.get("replace") or []
    return [str(x) for x in v or []]


def _candidate(resp: dict, f: dict) -> Candidate:
    path = f.get("filename") or ""
    ext = (f.get("extension") or "").lower().lstrip(".") or path.rsplit(".", 1)[-1].lower()
    return Candidate(
        user=resp.get("username") or "",
        path=path,
        size=int(f.get("size") or 0),
        bitrate=int(f.get("bitRate") or 0),
        samplerate=int(f.get("sampleRate") or 0),
        length=int(f.get("length") or 0),
        ext=ext,
        free_slot=bool(resp.get("hasFreeUploadSlot")),
        speed=int(resp.get("uploadSpeed") or 0),
        rank=0,
        queue=int(resp.get("queueLength") or 0),
    )


def _rank(c: Candidate, job: dict, want: dict) -> tuple:
    """Sort key: a peer whose download failed last; more of the preferred conditions met (format, bitrate, sample
    rate, the title in the file name, the artist in the path), then a free slot, a fast peer, a short queue, a
    higher bitrate."""
    formats = [f.lower() for f in _values(want.get("formats"))]
    met = [
        not formats or c.ext in formats,
        not c.bitrate or int(want.get("minBitrate") or 0) <= c.bitrate <= int(want.get("maxBitrate") or 10**9),
        not c.samplerate or c.samplerate <= int(want.get("maxSampleRate") or 10**9),
        not want.get("strictTitle") or rules.words(job["title"]).strip() in rules.words(c.name),
        not want.get("strictArtist") or any(f" {w} " in rules.words(c.path) for w in rules.artist_words(job["artist"])),
    ]
    return (_peer(c.user) < 0, -sum(met), not c.free_slot, c.speed < FAST, c.queue, -c.bitrate, -c.speed)
