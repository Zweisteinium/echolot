#!/usr/bin/env python3
"""Check the Sockseek daemon's job API and optionally record its answers as test fixtures.

Runs inside a throwaway container of the Sockseek image, without network, against a daemon in mock mode
(every file under the mock directory belongs to one peer, "local"):

  S=<scratch dir>; mkdir -p $S/out $S/fixtures
  docker run --rm --network none --user 1000:1000 -e HOME=/tmp --entrypoint sh \\
    -v $S/out:/out -v $S/fixtures:/fixtures -v $PWD/tools/daemon_check.py:/check.py:ro \\
    sockseek:local -c 'python3 /check.py --mock /tmp/mock --record /fixtures'

--mock DIR creates a few short test files in DIR (ffmpeg) and starts three mock daemons itself: normal (5031),
slow transfers (5032, a running download is cancelled) and failing transfers (5033).
Without it, the daemon at --url is checked as it is (no downloads unless --download).
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

QUERY = {"artist": "Scooter", "title": "Aiii Shot The DJ", "length": 5}
MOCK_FILES = [  # (path, ffmpeg args)
    ("Scooter/Scooter - Aiii Shot The DJ.flac", ["-metadata", "artist=Scooter", "-metadata", "title=Aiii Shot The DJ"]),
    ("Scooter/Scooter - Aiii Shot The DJ (Club Mix).mp3", ["-b:a", "320k"]),
    ("Other/Someone - Something.flac", []),
]


class Daemon:
    def __init__(self, url: str, record: Path | None) -> None:
        self.url, self.record = url.rstrip("/"), record

    def call(self, method: str, path: str, body: object = None, name: str = "") -> tuple[int, object]:
        req = urllib.request.Request(
            self.url + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = raw.decode("utf-8", "replace")
        if name and self.record:
            fixture = {"request": {"method": method, "path": path, "body": body}, "status": status, "response": data}
            (self.record / f"{name}.json").write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n")
        return status, data

    def wait(self, job: str, states: tuple[str, ...] = ("Terminal", "AwaitingSelection"), timeout: float = 60) -> dict:
        end = time.monotonic() + timeout
        while True:
            status, d = self.call("GET", f"/api/jobs/{job}")
            if status == 200 and isinstance(d, dict) and d["summary"]["lifecycleState"] in states:
                return d
            if time.monotonic() > end:
                raise TimeoutError(f"job {job}: {status} {str(d)[:300]}")
            time.sleep(0.25)


def make_mock(root: Path) -> None:
    for rel, args in MOCK_FILES:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=5", *args, str(p)], check=True
        )


def start_mock(mock: Path, port: int, out: str, *flags: str) -> None:
    with open(f"/tmp/daemon-{port}.log", "w") as log:  # the child keeps its own copy of the handle
        subprocess.Popen(
            [
                "sockseek",
                "daemon",
                "--mock-files-dir",
                str(mock),
                "--server-port",
                str(port),
                "-o",
                f"{out}/default",
                *flags,
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )


def reachable(d: Daemon) -> bool:
    for _ in range(80):
        try:
            d.call("GET", "/api/server/status")
            return True
        except urllib.error.URLError:
            time.sleep(0.25)
    return False


def search(d: Daemon, prefix: str = "") -> tuple[str, list[dict]]:
    _, job = d.call(
        "POST",
        "/api/jobs/search/tracks",
        {
            "songQuery": QUERY,
            "includeFullResults": True,
            "options": {"downloadSettings": {"search": {"desperateSearch": False}}},
        },
        name=prefix and f"{prefix}search-submit",
    )
    d.wait(job["jobId"])
    _, results = d.call("GET", f"/api/jobs/{job['jobId']}/results/files", name=prefix and f"{prefix}search-results")
    return job["jobId"], results.get("items", [])


def basic(d: Daemon, check, out: str, download: bool) -> None:
    status, s = d.call("GET", "/api/server/status", name="server-status")
    check(status == 200 and "soulseekClient" in s, "server status")
    status, info = d.call("GET", "/api/server/info", name="server-info")
    check(status == 200, f"server info: {json.dumps(info)[:200]}")

    status, job = d.call(
        "POST",
        "/api/jobs/search/tracks",
        {
            "songQuery": QUERY,
            "includeFullResults": True,
            "options": {"downloadSettings": {"search": {"desperateSearch": False}}},
        },
        name="search-submit",
    )
    check(status == 202 and job.get("kind") == "search", "search submitted")
    search_job = job["jobId"]
    detail = d.wait(search_job)
    d.call("GET", f"/api/jobs/{search_job}", name="search-done")
    check(detail["summary"]["terminalOutcome"] == "Succeeded", "search succeeded")
    status, results = d.call("GET", f"/api/jobs/{search_job}/results/files", name="search-results")
    items = results.get("items", [])
    check(status == 200 and results.get("isComplete") and items, f"{len(items)} candidates")
    for it in items:
        print(
            f"      {it['ref']['username']}  {it['ref']['filename']}  {it.get('length')} s  "
            f"{it.get('bitRate')} kbps  {it.get('extension')}"
        )

    _, none = d.call(
        "POST",
        "/api/jobs/search/tracks",
        {"songQuery": {"artist": "Nobody", "title": "Nothing At All", "length": 100}},
        name="search-empty-submit",
    )
    empty = d.wait(none["jobId"])
    d.call("GET", f"/api/jobs/{none['jobId']}", name="search-empty-done")
    s = empty["summary"]
    check(
        s["lifecycleState"] == "Terminal" and s.get("discoveryRawResultCount") == 0,
        f"empty search: {s['terminalOutcome']}, {s.get('discoveryRawResultCount')} results",
    )

    status, _ = d.call("GET", "/api/jobs/00000000-0000-0000-0000-000000000000", name="job-unknown")
    check(status == 404, f"unknown job: {status}")

    if not (download and items):
        return
    pick = [it["ref"] for it in items if "Club" not in it["ref"]["filename"]][:1]
    status, started = d.call(
        "POST",
        f"/api/jobs/{search_job}/downloads/files",
        {"files": pick, "options": {"outputParentDir": f"{out}/picked"}},
        name="download-submit",
    )
    check(status == 202 and isinstance(started, list) and started, "download submitted")
    song = started[0]["jobId"]
    done = d.wait(song, ("Terminal",))
    d.call("GET", f"/api/jobs/{song}", name="download-done")
    path = done["payload"].get("downloadPath")
    check(done["summary"]["terminalOutcome"] == "Succeeded" and bool(path), f"downloaded to {path}")

    _, manual = d.call(
        "POST",
        "/api/jobs/downloads/song",
        {
            "songQuery": QUERY,
            "options": {"outputParentDir": f"{out}/manual"},
            "downloadBehavior": {"default": "Manual"},
        },
        name="song-manual-submit",
    )
    waiting = d.wait(manual["jobId"])
    d.call("GET", f"/api/jobs/{manual['jobId']}", name="song-manual-awaiting")
    check(waiting["summary"]["lifecycleState"] == "AwaitingSelection", "manual song job awaits selection")
    status, _ = d.call("POST", f"/api/jobs/{manual['jobId']}/cancel", name="song-manual-cancel")
    try:
        d.wait(manual["jobId"], ("Terminal",), timeout=10)
        print(f"      cancel ({status}): the manual job ended")
    except TimeoutError:  # v3.0.6: the cancel is accepted, the job stays AwaitingSelection
        print(f"      cancel ({status}): the manual job still awaits selection after 10 s")


def slow(d: Daemon, check, out: str) -> None:
    """--mock-files-slow: a download that is still running gets cancelled."""
    search_job, items = search(d)
    status, started = d.call(
        "POST",
        f"/api/jobs/{search_job}/downloads/files",
        {"files": [items[0]["ref"]], "options": {"outputParentDir": f"{out}/slow"}},
    )
    song = started[0]["jobId"]
    running = d.wait(song, ("Running",), timeout=20)
    d.call("GET", f"/api/jobs/{song}", name="download-running")
    print(
        f"      running: phase {running['summary']['activityPhase']}, "
        f"{running['payload'].get('bytesTransferred')} of {running['payload'].get('totalBytes')} bytes"
    )
    status, _ = d.call("POST", f"/api/jobs/{song}/cancel", name="download-cancel")
    done = d.wait(song, ("Terminal",), timeout=30)
    d.call("GET", f"/api/jobs/{song}", name="download-cancelled")
    check(done["summary"]["terminalOutcome"] == "Cancelled", f"running download cancelled ({status})")


def fail(d: Daemon, check, out: str) -> None:
    """--mock-files-fail-downloads: every transfer fails."""
    search_job, items = search(d)
    _, started = d.call(
        "POST",
        f"/api/jobs/{search_job}/downloads/files",
        {"files": [items[0]["ref"]], "options": {"outputParentDir": f"{out}/fail"}},
    )
    song = started[0]["jobId"]
    done = d.wait(song, ("Terminal",), timeout=60)
    d.call("GET", f"/api/jobs/{song}", name="download-failed")
    s = done["summary"]
    check(s["terminalOutcome"] == "Failed", f"failed download: {s['terminalOutcome']} / {s.get('failureReason')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:5031")
    ap.add_argument("--mock", type=Path, help="create mock files here and start mock daemons (5031-5033)")
    ap.add_argument("--record", type=Path, help="write each answer as <name>.json into this directory")
    ap.add_argument("--download", action="store_true", help="also download (always on with --mock)")
    ap.add_argument("--out", default="/out", help="download directory as the daemon sees it")
    args = ap.parse_args()
    if args.record:
        args.record.mkdir(parents=True, exist_ok=True)
    problems = []

    def check(ok: bool, what: str) -> None:
        print(("ok  " if ok else "BAD ") + what)
        if not ok:
            problems.append(what)

    if args.mock:
        make_mock(args.mock)
        start_mock(args.mock, 5031, args.out)
        start_mock(args.mock, 5032, args.out, "--mock-files-slow")
        start_mock(args.mock, 5033, args.out, "--mock-files-fail-downloads", "100")  # the first 100 fail
        runs = [
            ("http://127.0.0.1:5031", lambda d: basic(d, check, args.out, True)),
            ("http://127.0.0.1:5032", lambda d: slow(d, check, args.out)),
            ("http://127.0.0.1:5033", lambda d: fail(d, check, args.out)),
        ]
    else:
        runs = [(args.url, lambda d: basic(d, check, args.out, args.download))]
    for url, run in runs:
        d = Daemon(url, args.record)
        if not reachable(d):
            print("daemon not reachable at", url)
            return 1
        print(f"--- {url}")
        run(d)
    print("all checks passed" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
