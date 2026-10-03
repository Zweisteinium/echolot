"""The availability tracker (jobs/availability.py): the lists' songs coming and going, a list that can't be
read, the daily check of every song's state at Spotify and SoundCloud, the Changes page."""

import threading
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from echolot import db
from echolot.config import Settings
from echolot.jobs import availability
from echolot.jobs.schedule import BY_NAME
from echolot.jobs.worker import Run
from echolot.services import soundcloud as sc_api
from echolot.services import spotify
from echolot.settings import auth, sources
from echolot.settings.vault import Vault
from echolot.web import create_app


def changes(con) -> list[tuple]:
    return [tuple(r) for r in con.execute("SELECT song_key, list_key, change, detail FROM changes ORDER BY id")]


def test_a_lists_songs_coming_and_going(settings: Settings) -> None:
    """Added, removed, and a swap for another release of the same recording (the same ISRC; on SoundCloud
    a re-upload: the same uploader, title and length) as one change; nothing at a list's first reading. A
    list that can't be read is a change once it has failed for an hour."""
    con = db.connect(settings.db_path)
    song = "INSERT INTO songs (key, service, artist, title, length, isrc) VALUES (?, ?, ?, ?, ?, ?)"
    with con:
        con.execute("UPDATE songs SET isrc = 'ISRC1' WHERE key = 'spotify:s1'")
        con.execute(song, ("spotify:s1b", "spotify", "Artist A", "First Song", 200, "ISRC1"))
        con.execute(song, ("soundcloud:1003", "soundcloud", "Uploader", "Trance Tune", 401, None))
        availability.record_list(con, "L", ["spotify:s1", "spotify:s2"], ["spotify:s1b", "spotify:s3"], first=False)
        availability.record_list(con, "S", ["soundcloud:1001"], ["soundcloud:1003"], first=False)
        availability.record_list(con, "N", [], ["spotify:s1"], first=True)
    assert changes(con) == [
        ("spotify:s1", "L", "replaced", "spotify:s1b"),
        ("spotify:s2", "L", "removed", None),
        ("spotify:s3", "L", "added", None),
        ("soundcloud:1001", "S", "re-uploaded", "soundcloud:1003"),
    ]
    availability.list_readable(con, "L", False, "giving up")
    availability.list_readable(con, "L", True)  # a hiccup: no change
    availability.list_readable(con, "L", False, "HTTP 404")
    assert changes(con)[-1][2] == "re-uploaded"  # failing, not for an hour yet
    with con:  # an hour later
        con.execute("UPDATE meta SET value = '2000-01-01T00:00:00' WHERE key = 'failing:L'")
    availability.list_readable(con, "L", False, "HTTP 404")
    availability.list_readable(con, "L", False, "HTTP 404")  # once
    availability.list_readable(con, "L", True)
    availability.list_readable(con, "L", True)
    assert changes(con)[-2:] == [("", "L", "unreadable", "HTTP 404"), ("", "L", "readable", None)]
    con.close()


class FakeSpotify:
    """s1 plays, s2 is taken down (no market has it), s3 does not play here (others have it), s9 is gone,
    s7 plays as another release (relinked)."""

    def __init__(self, con, vault, user_id=None) -> None:
        pass

    def get(self, url: str) -> dict:
        ids = url.split("ids=")[1].split("&")[0].split(",")
        with_market = "market=" in url
        out = []
        for sid in ids:
            if sid == "s9":
                out.append(None)
            elif sid in ("s2", "s3") and with_market:
                out.append({"id": sid, "is_playable": False, "restrictions": {"reason": "market"}})
            elif sid in ("s2", "s3"):
                out.append({"id": sid, "available_markets": [] if sid == "s2" else ["US"]})
            elif sid == "s7":
                out.append({"id": "s7b", "is_playable": True, "linked_from": {"id": "s7"}})
            else:
                out.append({"id": sid, "is_playable": True})
        return {"tracks": out}


@pytest.fixture
def run(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Run:
    monkeypatch.setattr(spotify, "Spotify", FakeSpotify)
    monkeypatch.setattr(sc_api, "any_token", lambda con, vault: "token")
    found = {"1001": {"id": 1001, "policy": "MONETIZE"}}  # 1002 is gone
    monkeypatch.setattr(sc_api, "tracks", lambda token, ids: [found[i] for i in ids if i in found])
    r = Run(BY_NAME["availability"], settings, Vault.from_env(settings.data_dir, {}), "manual")
    r.stop = threading.Event()
    return r


def test_the_daily_check(run: Run) -> None:
    con = run.connect()
    song = "INSERT INTO songs (key, service, artist, title, length) VALUES (?, 'spotify', ?, ?, 1)"
    with con:  # s7 on a list too, and a song removed lately (why: this check)
        con.execute(song, ("spotify:s7", "A", "B"))
        con.execute("INSERT INTO list_songs VALUES ('spotify:playlist:AAA111', 9, 'spotify:s7')")
        con.execute(song, ("spotify:s9", "C", "D"))
        con.execute("INSERT INTO changes (ts, song_key, list_key, change) VALUES ('x', 'spotify:s9', 'L', 'removed')")
        con.execute("UPDATE songs SET unavailable = NULL WHERE key = 'spotify:s3'")
    con.close()
    message = availability.check(run)
    assert message.startswith("7 songs checked: 1 taken down, 1 blocked, 2 gone, 1 replaced; 0 changes")
    con = run.connect()
    states = dict(con.execute("SELECT song_key, state FROM availability").fetchall())
    spotify_states = {"s1": "available", "s2": "taken_down", "s3": "blocked", "s9": "gone", "s7": "replaced"}
    expected = {f"spotify:{k}": v for k, v in spotify_states.items()}
    assert states == expected | {"soundcloud:1001": "available", "soundcloud:1002": "gone"}
    assert con.execute("SELECT detail FROM changes WHERE song_key = 'spotify:s9'").fetchone()[0] == "it exists no more"
    flags = dict(con.execute("SELECT key, unavailable FROM songs WHERE service = 'spotify'").fetchall())
    assert flags["spotify:s2"] == flags["spotify:s3"] == availability.GREYED_OUT and flags["spotify:s1"] is None
    assert flags["spotify:s7"] is None  # it plays (as another release)
    with con:  # s2 plays again: a change
        con.execute("UPDATE availability SET state = 'available' WHERE song_key = 'spotify:s2'")
    con.close()
    availability.check(run)
    con = run.connect()
    assert [c[2] for c in changes(con)] == ["removed", "taken_down"]
    con.close()


def test_the_changes_page(settings: Settings, login: Callable[..., TestClient]) -> None:
    """A user sees their songs' changes and their lists'; the owner's other songs not."""
    app = create_app(settings)
    con = db.connect(settings.db_path)
    uid = auth.logged_in(con, "timon", "nd-timon", False).id
    sources.add_list(con, uid, "https://open.spotify.com/playlist/AAA111")
    change = "INSERT INTO changes (ts, song_key, list_key, change) VALUES (?, ?, ?, ?)"
    with con:
        con.execute("INSERT INTO availability VALUES ('spotify:s3', 'taken_down', '2026-10-01', 'x', NULL)")
        con.execute(change, ("2026-10-01T00:00:00", "spotify:s3", "spotify:likes:1", "taken_down"))
        con.execute(change, ("2026-10-02T00:00:00", "spotify:s2", "spotify:playlist:AAA111", "removed"))
    con.close()
    owner = login(app).get("/changes").text
    assert "Taken down" in owner and "Gone Song" in owner and 'playable <span class="count">1</span>' in owner
    timon = login(app, "timon", admin=False).get("/changes").text
    assert "Gone Song" not in timon and "Second Song" in timon  # removed from timon's list
