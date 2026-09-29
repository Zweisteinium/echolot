"""The daemon client (soulseek.py) against answers recorded from Sockseek 3.0.6 (tests/fixtures/sockseek/v3)."""

import json
import threading
import time
from pathlib import Path

import pytest

from echolot import soulseek

FIXTURES = Path(__file__).parent / "fixtures" / "sockseek" / "v3"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class Replay(soulseek.Daemon):
    """Answers requests with the recorded responses, by method and path."""

    def __init__(self, answers: dict[tuple[str, str], str]) -> None:
        super().__init__("http://daemon")
        self.answers, self.sent = answers, []

    def _call(self, method: str, path: str, body: object = None):
        self.sent.append((method, path, body))
        for (m, prefix), name in self.answers.items():
            if m == method and path.startswith(prefix):
                f = fixture(name)
                if f["status"] == 404:
                    raise soulseek.Lost(path)
                return f["response"]
        raise AssertionError(f"unexpected {method} {path}")


def test_search_and_results() -> None:
    d = Replay(
        {("POST", "/api/jobs/search/tracks"): "search-submit", ("GET", "/api/jobs/"): "search-done"}
    )
    job = d.search("Scooter", "Aiii Shot The DJ", 5, soulseek.search_settings(desperate=True))
    body = d.sent[0][2]
    assert body["songQuery"] == {"artist": "Scooter", "title": "Aiii Shot The DJ", "length": 5}
    assert body["includeFullResults"] is False
    assert body["options"]["downloadSettings"]["search"]["desperateSearch"] is True
    detail = d.wait(job, threading.Event(), time.monotonic() + 5)
    assert detail["summary"]["terminalOutcome"] == "Succeeded"
    d.answers = {("GET", "/api/jobs/"): "search-results"}
    found = d.results(job)
    assert [(c.user, c.name, c.ext, c.length, c.rank) for c in found] == [
        ("local", "Scooter - Aiii Shot The DJ", "flac", 5, 0),
        ("local", "Scooter - Aiii Shot The DJ (Club Mix)", "mp3", 5, 1),
    ]
    assert found[0].folders == ("Scooter",) and found[1].bitrate == 320


def test_download_states() -> None:
    d = Replay({("POST", "/api/jobs/"): "download-submit"})
    c = soulseek.Candidate(
        "local", "Scooter\\Scooter - Aiii Shot The DJ.flac", 1, 95, 44100, 5, "flac", True, 100, 0
    )
    job = d.download("search-1", c, "/music/inbox/soulseek/x", soulseek.search_settings())
    assert d.sent[0][2]["files"] == [{"username": "local", "filename": c.path}]
    assert d.sent[0][2]["options"]["outputParentDir"] == "/music/inbox/soulseek/x"
    for name, state, path in [("download-done", "done", "/out/picked/Scooter - Aiii Shot The DJ.flac"),
                              ("download-running", "running", None), ("download-cancelled", "cancelled", None),
                              ("download-failed", "failed", None)]:  # fmt: skip
        d.answers = {("GET", "/api/jobs/"): name}
        t = d.transfer(job)
        assert (t.state, t.path) == (state, path), name
    assert "download failure" in d.transfer(job).reason  # the daemon's own message


def test_unknown_job_is_lost() -> None:
    d = Replay({("GET", "/api/jobs/"): "job-unknown"})
    with pytest.raises(soulseek.Lost):
        d.transfer("gone")


def test_status() -> None:
    d = Replay(
        {("GET", "/api/server/info"): "server-info", ("GET", "/api/server/status"): "server-status"}
    )
    s = d.status()
    assert s["version"] == "3.0.6.0" and s["ready"] is False  # the mock daemon logs in nowhere


def test_unreachable_daemon() -> None:
    with pytest.raises(soulseek.DaemonError, match="not reachable"):
        soulseek.Daemon("http://127.0.0.1:9", timeout=2).status()


def test_login_file(tmp_path: Path) -> None:
    soulseek.write_conf(tmp_path / "d", "me", "pw with spaces")
    f = tmp_path / "d" / "daemon.conf"
    assert "user = me\npass = pw with spaces\n" in f.read_text()
    assert oct(f.stat().st_mode & 0o777) == "0o600"
