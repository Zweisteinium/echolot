"""Soulseek through the Sockseek daemon (`sockseek daemon`, in the VPN's network namespace), over its HTTP
job API, which v3 and v4 share: search jobs, their ranked results, downloads of chosen files, cancel.
Polling only. Unknown fields are ignored; a job that is gone (the daemon restarted) raises Lost, so the
caller retries later without counting a try.

Echolot owns the search settings (SEARCH below: Sockseek's filters and preferences) and sends them with
every job.
"""

import contextlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Sockseek's filters (necessary) and ranking preferences
SEARCH: dict[str, Any] = {  # lists are patches of Sockseek's own: {"replace": [...]}
    "necessaryCond": {"strictArtist": True, "lengthTolerance": 3},
    "preferredCond": {"formats": {"replace": ["flac"]}, "minBitrate": 200, "maxBitrate": 2500,
                      "maxSampleRate": 48000, "strictTitle": True, "strictArtist": True},
    "maxStaleTime": 90000,  # ms without progress before Sockseek gives a transfer up
}  # fmt: skip


def search_settings(*, desperate: bool = False, strict_artist: bool = True, flac_only: bool = False) -> dict:
    """downloadSettings for a job. desperate: a search without results is repeated with the title
    alone and the artist alone. flac_only: only FLAC files count (upgrades)."""
    search = json.loads(json.dumps(SEARCH))
    search["desperateSearch"] = desperate
    search["necessaryCond"]["strictArtist"] = strict_artist
    if flac_only:
        search["necessaryCond"]["formats"] = {"replace": ["flac"]}
    return {"search": search, "preprocess": {"removeFt": True}}


class DaemonError(RuntimeError):
    """The daemon can't be reached or refused a request."""


class Lost(DaemonError):
    """The job is unknown to the daemon (it restarted)."""


class Cancelled(Exception):
    """Our side stopped waiting (the job run was cancelled or ran out of time)."""


@dataclass
class Candidate:
    """A file a search found, in Sockseek's ranking order (rank 0 = best)."""

    user: str
    path: str  # the peer's path, folders separated by \\
    size: int
    bitrate: int
    samplerate: int
    length: int  # seconds, 0 = unknown
    ext: str
    free_slot: bool
    speed: int
    rank: int
    queue: int = 0  # the peer's upload queue (slskd reports it)

    @property
    def parts(self) -> list[str]:
        return [p for p in self.path.replace("\\", "/").split("/") if p]

    @property
    def name(self) -> str:
        """The file name without its extension."""
        name = self.parts[-1] if self.parts else ""
        return name.rsplit(".", 1)[0]

    @property
    def folders(self) -> tuple[str, ...]:
        """All folders of the path (the artist is often a few levels up)."""
        return tuple(self.parts[:-1])

    @property
    def key(self) -> str:
        return f"slsk:{self.user}:{self.path}"


@dataclass
class Transfer:
    state: str  # running, done, failed, cancelled
    path: str | None  # the daemon's path of the downloaded file (done)
    done_bytes: int
    total_bytes: int
    reason: str


class Daemon:
    def __init__(self, url: str, timeout: float = 30) -> None:
        self.url, self.timeout = url.rstrip("/"), timeout
        self._submitted: dict[str, float] = {}  # job id -> when it was started here

    def _call(self, method: str, path: str, body: object = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})  # fmt: skip
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404 and path.startswith("/api/jobs/"):
                raise Lost(f"{method} {path}: unknown job") from e
            detail = e.read().decode(errors="replace")[:300]
            raise DaemonError(f"{method} {path}: HTTP {e.code} {detail}") from e
        except (OSError, TimeoutError) as e:
            raise DaemonError(f"Sockseek daemon at {self.url} not reachable: {e}") from e
        return json.loads(raw) if raw else None

    def status(self) -> dict[str, Any]:
        """{ready, state, version, started}; ready: logged in to Soulseek."""
        info = self._call("GET", "/api/server/info") or {}
        s = (self._call("GET", "/api/server/status") or {}).get("soulseekClient") or {}
        return {"ready": bool(s.get("isReady")), "state": s.get("state") or "", "flags": s.get("flags") or [],
                "version": info.get("version") or "", "started": info.get("startedAtUtc") or ""}  # fmt: skip

    def search(self, artist: str, title: str, length: int, settings: dict) -> str:
        query = {"artist": artist, "title": title, "length": int(length or 0)}
        job = self._call("POST", "/api/jobs/search/tracks",
                         {"songQuery": query, "includeFullResults": False,
                          "options": {"downloadSettings": settings}})  # fmt: skip
        self._submitted[job["jobId"]] = time.monotonic()
        return job["jobId"]

    def job(self, job_id: str) -> dict[str, Any]:
        try:
            return self._call("GET", f"/api/jobs/{job_id}")
        except Lost:
            # a job is queryable only a moment after it was accepted; unknown later: the daemon restarted
            if time.monotonic() - self._submitted.get(job_id, float("-inf")) < 30:
                return {"summary": {"lifecycleState": "Pending"}, "payload": {}}
            raise

    def wait(self, job_id: str, stop: threading.Event, deadline: float, every: float = 1.0) -> dict:
        """The job's detail once it has ended (lifecycleState Terminal)."""
        while True:
            detail = self.job(job_id)
            if (detail.get("summary") or {}).get("lifecycleState") == "Terminal":
                return detail
            if stop.is_set() or time.monotonic() > deadline:
                self.cancel(job_id)
                raise Cancelled(job_id)
            stop.wait(every)

    def results(self, job_id: str) -> list[Candidate]:
        items = (self._call("GET", f"/api/jobs/{job_id}/results/files") or {}).get("items") or []
        out = []
        for rank, it in enumerate(items):
            ref, peer = it.get("ref") or {}, it.get("peer") or {}
            path = ref.get("filename") or it.get("filename") or ""
            # peers send length and bitrate as attributes, if at all; the extension is often empty
            attrs = {a.get("type"): a.get("value") for a in it.get("attributes") or [] if isinstance(a, dict)}
            ext = (it.get("extension") or "").lower().lstrip(".") or path.rsplit(".", 1)[-1].lower()
            out.append(Candidate(
                user=ref.get("username") or it.get("username") or "", path=path, size=int(it.get("size") or 0),
                bitrate=int(it.get("bitRate") or attrs.get("BitRate") or 0),
                samplerate=int(it.get("sampleRate") or attrs.get("SampleRate") or 0),
                length=int(it.get("length") or attrs.get("Length") or 0), ext=ext,
                free_slot=bool(peer.get("hasFreeUploadSlot")), speed=int(peer.get("uploadSpeed") or 0),
                rank=rank,
            ))  # fmt: skip
        return out

    def download(self, search_job: str, c: Candidate, parent_dir: str, settings: dict) -> str:
        """Start downloading one found file into parent_dir (the daemon's path); the song job id."""
        started = self._call("POST", f"/api/jobs/{search_job}/downloads/files",
                             {"files": [{"username": c.user, "filename": c.path}],
                              "options": {"outputParentDir": parent_dir, "downloadSettings": settings}})  # fmt: skip
        jobs = started if isinstance(started, list) else [started]
        self._submitted[jobs[0]["jobId"]] = time.monotonic()
        return jobs[0]["jobId"]

    def transfer(self, job_id: str) -> Transfer:
        detail = self.job(job_id)
        s, p = detail.get("summary") or {}, detail.get("payload") or {}
        done, total = (int(p.get("bytesTransferred") or 0), int(p.get("totalBytes") or p.get("resolvedSize") or 0))
        if s.get("lifecycleState") != "Terminal":
            return Transfer("running", None, done, total, "")
        outcome = s.get("terminalOutcome") or ""
        state = {"Succeeded": "done", "Cancelled": "cancelled"}.get(outcome, "failed")
        reason = s.get("failureMessage") or s.get("failureReason") or outcome
        return Transfer(state, p.get("downloadPath") if state == "done" else None, done, total, str(reason))

    def cancel(self, job_id: str) -> None:
        with contextlib.suppress(DaemonError):
            self._call("POST", f"/api/jobs/{job_id}/cancel")


CONF = "daemon.conf"


def write_conf(folder: Path, user: str, password: str) -> None:
    """The daemon's login, in its config file (mode 600): the daemon container restarts the daemon when
    the file changes (deploy/sockseek/run.sh)."""
    folder.mkdir(parents=True, exist_ok=True)
    text = (
        "# Written by Echolot (Accounts page): the Soulseek account the Sockseek daemon logs in with.\n"
        f"user = {user}\npass = {password}\n"
    )
    tmp = folder / f".{CONF}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    tmp.replace(folder / CONF)
