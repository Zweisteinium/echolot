"""Spotify's rate limit (services/spotify): a long Retry-After stops every request until then, kept across a
restart; a short one is waited out. Liked Songs are read only up to the first known one when only songs were
added; the list job reads nothing while Spotify rests, and no list counts as unreadable."""

import email.message
import io
import json
import threading
import time
import urllib.error

import pytest

from echolot import db
from echolot.config import Settings
from echolot.jobs import lists
from echolot.jobs.schedule import BY_NAME
from echolot.jobs.worker import Run
from echolot.services import spotify
from echolot.settings.vault import Vault


@pytest.fixture
def client(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(spotify, "_rest", {"until": 0.0})
    monkeypatch.setattr(spotify, "SPACING", 0)
    monkeypatch.setattr(spotify, "_me", {})
    con = db.connect(settings.db_path)
    sp = spotify.Spotify.__new__(spotify.Spotify)
    sp.con, sp.vault, sp.creds = con, None, spotify.Credentials("id", "secret", "refresh", 1)
    monkeypatch.setattr(sp, "token", lambda: "token")
    yield sp
    con.close()


def answer(monkeypatch: pytest.MonkeyPatch, *responses: object) -> list[str]:
    """Spotify's answers in turn: a dict (JSON) or (status, Retry-After); returns the URLs asked."""
    asked: list[str] = []
    queue = list(responses)

    def urlopen(req, timeout=0):
        asked.append(req.full_url)
        r = queue.pop(0)
        if isinstance(r, tuple):
            headers = email.message.Message()
            headers["Retry-After"] = str(r[1])
            raise urllib.error.HTTPError(req.full_url, r[0], "Too Many Requests", headers, io.BytesIO(b""))
        return io.BytesIO(json.dumps(r).encode())

    monkeypatch.setattr(spotify.urllib.request, "urlopen", urlopen)
    return asked


def test_a_long_retry_after_stops_every_request_until_then(client, monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(spotify.time, "sleep", slept.append)
    asked = answer(monkeypatch, (429, 5), {"id": "me"}, (429, 25000))
    assert client.get("/me") == {"id": "me"} and slept == [5]  # a short wait: waited out, asked again
    with pytest.raises(spotify.SpotifyResting, match="wait until"):
        client.get("/me/tracks")
    assert len(asked) == 3
    with pytest.raises(spotify.SpotifyResting):
        client.get("/me/playlists")  # not asked: still resting
    assert len(asked) == 3
    spotify._rest["until"] = 0.0  # a restart: the database still knows
    assert spotify.resting_until(client.con) > time.time() + 24000
    with pytest.raises(spotify.SpotifyResting):
        client.get("/me/playlists")
    assert len(asked) == 3


def test_only_the_songs_liked_since_are_read(client, monkeypatch: pytest.MonkeyPatch) -> None:
    def page(ids: list[str], total: int, more: bool) -> dict:
        items = [
            {"track": {"id": i, "name": f"Song {i}", "artists": [{"name": "A"}], "duration_ms": 1000}} for i in ids
        ]
        return {"items": items, "total": total, "next": "https://api.spotify.com/v1/next" if more else None}

    asked = answer(monkeypatch, page(["n1", "n2", "o1"], 4, True))
    assert [s["id"] for s in client.liked_since({"o1", "o2"}, 2) or []] == ["n1", "n2"] and len(asked) == 1
    answer(monkeypatch, page(["n1", "o1"], 2, True))  # one added and one unliked: not only added, read all
    assert client.liked_since({"o1", "o2"}, 2) is None
    answer(monkeypatch, {"id": "me"})
    assert client.me() == client.me() == {"id": "me"}  # asked once


def test_the_lists_wait_while_spotify_rests(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    con = db.connect(settings.db_path)
    with con:
        db.set_meta(con, spotify.REST_KEY, str(time.time() + 3600))
    con.close()
    monkeypatch.setattr(spotify, "Spotify", lambda *a, **k: pytest.fail("asked Spotify while it rests"))
    run = Run(BY_NAME["sync"], settings, Vault.from_env(settings.data_dir, {}), "manual")
    run.stop = threading.Event()
    assert "asked Echolot to wait until" in lists.fetch_spotify(run)
    con = db.connect(settings.db_path)
    assert not con.execute("SELECT 1 FROM changes WHERE change = 'unreadable'").fetchone()
    con.close()
