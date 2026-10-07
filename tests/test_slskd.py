"""slskd as the Soulseek client (services/slskd.py), against a small stand-in for its HTTP API."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from echolot.services import slskd, soulseek
from echolot.services.soulseek import Cancelled


class Fake:
    """slskd's state: searches by text (their responses), transfers, the login."""

    responses: dict[str, list[dict]]
    searches: dict[str, dict]
    transfers: list[dict]
    calls: list[tuple[str, str]]
    downloads: Path
    deliver: str  # Succeeded or Rejected

    def reset(self, downloads: Path) -> None:
        self.responses, self.searches, self.transfers, self.calls = {}, {}, [], []
        self.downloads, self.deliver, self.tokens, self.readonly, self.late = downloads, "Succeeded", 0, False, False
        self.busy = 0  # answers 429 to this many search POSTs (slskd: one operation at a time)


FAKE = Fake()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def _send(self, code: int, body: object = None) -> None:
        raw = b"" if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> object:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def _route(self, method: str) -> None:
        path = self.path.removeprefix("/api/v0")
        FAKE.calls.append((method, path))
        if path == "/session" and method == "POST":
            body = self._body()
            if body == {"username": "admin", "password": "pw"}:
                FAKE.tokens += 1
                return self._send(200, {"token": f"t{FAKE.tokens}"})
            return self._send(401)
        if self.headers.get("Authorization") != f"Bearer t{FAKE.tokens}":
            return self._send(401)
        if path == "/application":
            return self._send(
                200,
                {
                    "server": {"state": "Connected, LoggedIn", "isLoggedIn": True},
                    "version": {"current": "0.26.0"},
                    "user": {"username": "me"},
                },
            )
        if path == "/searches" and method == "POST":
            body = self._body()
            if FAKE.busy:
                FAKE.busy -= 1
                return self._send(429, "Only one concurrent operation is permitted.")
            FAKE.searches[body["id"]] = body
            return self._send(200, {"id": body["id"], "state": "InProgress"})
        if path.startswith("/searches/"):
            sid, _, rest = path.removeprefix("/searches/").partition("/")
            if sid not in FAKE.searches:
                return self._send(404)
            if rest == "responses":
                return self._send(200, FAKE.responses.get(FAKE.searches[sid]["searchText"], []))
            if method == "GET":
                return self._send(200, {"id": sid, "state": "Completed, TimedOut"})
            if method == "DELETE":
                del FAKE.searches[sid]
            return self._send(204)
        if path == "/transfers/downloads/batches":
            body = self._body()
            f = body["files"][0]
            name = f["filename"].rsplit("\\", 1)[-1]
            folder = FAKE.downloads / body["options"]["destination"]
            queued = FAKE.deliver == "Queued"
            if FAKE.deliver == "Succeeded" and not FAKE.late:
                folder.mkdir(parents=True)
                (folder / name).write_bytes(b"audio")
                if FAKE.readonly:
                    folder.chmod(0o555)
            FAKE.transfers.append(
                {
                    "id": f"x{len(FAKE.transfers)}",
                    "username": body["username"],
                    "batchId": "b1",
                    "filename": f["filename"],
                    "size": f["size"],
                    "bytesTransferred": 0 if queued else f["size"],
                    "state": "Queued, Remotely" if queued else f"Completed, {FAKE.deliver}",
                    "exception": "Transfer rejected: File not shared." if FAKE.deliver == "Rejected" else None,
                }
            )
            return self._send(201, {"batch": {"id": "b1"}, "failures": []})
        if path.startswith("/transfers/downloads/"):
            user, _, tid = path.removeprefix("/transfers/downloads/").partition("/")
            if method == "DELETE":
                FAKE.transfers = [t for t in FAKE.transfers if t["id"] != tid.split("?")[0]]
                return self._send(204)
            files = [t for t in FAKE.transfers if t["username"] == user]
            return self._send(200, {"username": user, "directories": [{"directory": "d", "files": files}]})
        return self._send(404)

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def do_PUT(self) -> None:
        self._route("PUT")

    def do_DELETE(self) -> None:
        self._route("DELETE")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    FAKE.reset(tmp_path)
    monkeypatch.setattr(slskd, "_searches", __import__("collections").deque())  # a fresh search budget
    monkeypatch.setattr(slskd, "_peers", {})  # no peer failed yet
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield slskd.Slskd(f"http://127.0.0.1:{server.server_port}", tmp_path, "admin", "pw", timeout=5)
    server.shutdown()
    server.server_close()


def file(name: str, length: int = 200, ext: str = "flac", bitrate: int = 0, locked: bool = False) -> dict:
    return {
        "filename": name,
        "size": 30_000_000,
        "length": length,
        "extension": ext,
        "bitRate": bitrate,
        "sampleRate": 44100,
        "isLocked": locked,
    }


def response(user: str, *files: dict, free: bool = True, speed: int = 2_000_000, queue: int = 0) -> dict:
    return {
        "username": user,
        "files": list(files),
        "hasFreeUploadSlot": free,
        "uploadSpeed": speed,
        "queueLength": queue,
    }


def test_status_logs_in(client: slskd.Slskd) -> None:
    s = client.status()
    assert (s["ready"], s["version"], s["user"]) == (True, "0.26.0", "me")
    FAKE.tokens += 1  # the session ran out: logged in again once
    assert client.status()["ready"] and FAKE.calls.count(("POST", "/session")) == 2


def test_search_text() -> None:
    assert slskd.search_text("Artist", "Song (feat. Other) [Remix]") == "Artist Song Remix"
    assert slskd.search_text("AC/DC", "Hells Bells - Live") == "AC DC Hells Bells Live"
    assert slskd.search_text("Sido", "Bilder im Kopf feat. X") == "Sido Bilder im Kopf"


def test_results_filtered_and_ranked(client: slskd.Slskd) -> None:
    """The necessary conditions drop files (length, a locked file, other formats for an upgrade); the
    preferred ones, a free slot and speed decide the order."""
    FAKE.responses["Artist Song"] = [
        response("slow", file("Music\\Artist\\Artist - Song.flac"), speed=50_000),
        response("busy", file("Music\\Artist\\Artist - Song.flac"), free=False),
        response(
            "fast",
            file("Music\\Artist\\Artist - Song.flac"),
            file("Music\\Artist\\Artist - Song.mp3", ext="mp3", bitrate=128),
        ),
        response(
            "off", file("Music\\Artist\\Artist - Song.flac", length=260), file("x\\Artist - Song.flac", locked=True)
        ),
    ]
    job = client.search("Artist", "Song", 200, soulseek.search_settings())
    client.wait(job, threading.Event(), 1e12)
    ranked = client.results(job)
    assert [(c.user, c.ext) for c in ranked] == [("fast", "flac"), ("slow", "flac"), ("busy", "flac"), ("fast", "mp3")]
    assert [c.rank for c in ranked] == [0, 1, 2, 3] and ranked[0].queue == 0
    upgrade = client.search("Artist", "Song", 200, soulseek.search_settings(flac_only=True))
    client.wait(upgrade, threading.Event(), 1e12)
    assert {c.ext for c in client.results(upgrade)} == {"flac"}


def test_desperate_search_tries_title_and_artist_alone(client: slskd.Slskd) -> None:
    FAKE.responses["Song"] = [response("u", file("Music\\Artist\\Song.flac"))]
    job = client.search("Artist", "Song", 200, soulseek.search_settings(desperate=True))
    client.wait(job, threading.Event(), 1e12)
    assert [s["searchText"] for s in FAKE.searches.values()] == ["Artist Song", "Song", "Artist"]
    assert [c.user for c in client.results(job)] == ["u"]
    client.close(job)
    assert not FAKE.searches  # deleted in slskd
    plain = client.search("Artist", "Song", 200, soulseek.search_settings())  # found: no desperate search
    FAKE.responses["Artist Song"] = [response("v", file("Music\\Artist\\Artist - Song.flac"))]
    client.wait(plain, threading.Event(), 1e12)
    assert len(FAKE.searches) == 1


def test_download_lands_in_its_folder(client: slskd.Slskd, tmp_path: Path) -> None:
    FAKE.responses["Artist Song"] = [response("u", file("Music\\Artist\\Artist - Song.flac"))]
    job = client.search("Artist", "Song", 200, soulseek.search_settings())
    client.wait(job, threading.Event(), 1e12)
    c = client.results(job)[0]
    t = client.transfer(client.download(job, c, "echolot/abc", {}))
    assert t.state == "done" and Path(t.path) == tmp_path / "echolot/abc/Artist - Song.flac"
    assert not FAKE.transfers  # taken off slskd's list; the file stays
    FAKE.late = True  # reported done a moment before the file is moved into its folder
    late = client.download(job, c, "echolot/late", {})
    assert client.transfer(late).state == "running"
    (tmp_path / "echolot/late").mkdir(parents=True)
    (tmp_path / "echolot/late/Artist - Song.flac").write_bytes(b"audio")
    assert client.transfer(late).state == "done"
    FAKE.late = False
    FAKE.readonly = True  # slskd runs as another user: its folder is not Echolot's to change
    try:
        t = client.transfer(client.download(job, c, "echolot/ro", {}))
    finally:
        (tmp_path / "echolot/ro").chmod(0o755)
    assert t.state == "failed" and "another user" in t.reason
    FAKE.deliver = "Rejected"
    t = client.transfer(client.download(job, c, "echolot/def", {}))
    assert (t.state, t.reason) == ("failed", "Transfer rejected: File not shared.")


def test_a_search_can_be_stopped(client: slskd.Slskd) -> None:
    job = client.search("Artist", "Song", 200, soulseek.search_settings())
    stop = threading.Event()
    stop.set()
    original = client._call

    def running(method: str, path: str, body: object = None, again: bool = True) -> object:
        if method == "GET" and path.startswith("/searches/") and "/" not in path.removeprefix("/searches/"):
            return {"state": "InProgress"}
        return original(method, path, body, again)

    client._call = running
    with pytest.raises(Cancelled):
        client.wait(job, stop, 1e12)
    assert ("PUT", f"/searches/{job}") in FAKE.calls


def test_the_search_limit_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(slskd, "_searches", __import__("collections").deque())
    monkeypatch.setattr(slskd, "SEARCH_LIMIT", (2, 60))
    slskd._throttle()
    slskd._throttle()
    stop = threading.Event()
    stop.set()
    with pytest.raises(soulseek.DaemonError, match="search slot"):
        slskd._throttle(stop)  # the third within the window waits (here: stopped)


def test_slskd_busy_with_another_search_is_asked_again(client: slskd.Slskd, monkeypatch: pytest.MonkeyPatch) -> None:
    """slskd answers 429 while it starts another search: Echolot waits a moment and asks again."""
    monkeypatch.setattr(slskd.time, "sleep", lambda s: None)
    FAKE.busy = 3
    job = client.search("Artist", "Song", 200, soulseek.search_settings())
    assert job in FAKE.searches and FAKE.busy == 0


def test_a_peer_that_failed_is_tried_last_then_not_at_all(client: slskd.Slskd) -> None:
    """As Sockseek (fails-to-downrank 1, fails-to-ignore 2): after one failed download the peer's files come
    after everyone else's, after two they are left out; a download that goes through counts for the peer."""
    FAKE.responses["Artist Song"] = [
        response("flaky", file("Music\\Artist\\Artist - Song.flac")),
        response("other", file("Music\\Artist\\Artist - Song.mp3", ext="mp3", bitrate=320)),
    ]

    def ranked() -> list[str]:
        job = client.search("Artist", "Song", 200, soulseek.search_settings())
        client.wait(job, threading.Event(), 1e12)
        return [c.user for c in client.results(job)]

    assert ranked() == ["flaky", "other"]  # the FLAC first
    FAKE.deliver = "Rejected"
    job = client.search("Artist", "Song", 200, soulseek.search_settings())
    client.wait(job, threading.Event(), 1e12)
    flaky = client.results(job)[0]
    assert client.transfer(client.download(job, flaky, "echolot/a", {})).state == "failed"
    assert ranked() == ["other", "flaky"]
    assert client.transfer(client.download(job, flaky, "echolot/b", {})).state == "failed"
    assert ranked() == ["other"]
    FAKE.deliver = "Succeeded"
    other = client.results(job)[-1]
    assert client.transfer(client.download(job, other, "echolot/c", {})).state == "done"
    assert slskd._peer("other") == 1 and slskd._peer("flaky") == -2


def test_a_download_without_progress_is_given_up(client: slskd.Slskd, monkeypatch: pytest.MonkeyPatch) -> None:
    """Queued at the peer (or the peer does not answer) for maxStaleTime: given up, as Sockseek does, and the
    next file is tried; it counts as the peer's failure."""
    FAKE.responses["Artist Song"] = [response("u", file("Music\\Artist\\Artist - Song.flac"))]
    job = client.search("Artist", "Song", 200, soulseek.search_settings())
    client.wait(job, threading.Event(), 1e12)
    FAKE.deliver = "Queued"
    dl = client.download(job, client.results(job)[0], "echolot/q", soulseek.search_settings())
    assert client.transfer(dl).state == "running"
    clock = slskd.time.monotonic() + soulseek.SEARCH["maxStaleTime"] / 1000 + 1
    monkeypatch.setattr(slskd.time, "monotonic", lambda: clock)
    t = client.transfer(dl)
    assert (t.state, t.reason) == ("failed", "no progress for 90 s")
    assert not FAKE.transfers and slskd._peer("u") == -1  # cancelled in slskd
