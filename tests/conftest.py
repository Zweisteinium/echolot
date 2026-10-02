import datetime
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from echolot import db
from echolot.config import Settings
from echolot.library import catalog, history
from echolot.services import spotify
from echolot.settings import auth, options

PASSWORD = "correct horse battery"

# a small collection, all followed by OWNER (user 1, a Navidrome admin): Spotify likes and two playlists,
# SoundCloud likes and a set
OWNER = 1
LISTS = [  # key, service, likes, url, title override, title, playlist
    ("spotify:likes:1", "spotify", 1, "https://open.spotify.com/collection/tracks", None, "Liked Songs", 1),
    ("spotify:playlist:AAA111", "spotify", 0, "https://open.spotify.com/playlist/AAA111", None, "Playlist A", 1),
    ("spotify:playlist:BBB222", "spotify", 0, "https://open.spotify.com/playlist/BBB222", "Renamed", "Renamed", 0),
    ("soundcloud:someone/likes", "soundcloud", 1, "https://soundcloud.com/someone/likes", None, "SoundCloud Likes", 1),
    (
        "soundcloud:someone/sets/trance",
        "soundcloud",
        0,
        "https://soundcloud.com/someone/sets/trance",
        None,
        "Trance",
        1,
    ),
]
SONGS = [  # key, service, artist, title, length, unavailable, stem, artists, archived
    ("spotify:s1", "spotify", "Artist A", "First Song", 200, None, None, '["Artist A"]', 0),
    ("spotify:s2", "spotify", "Artist B", "Second Song (Original Mix)", 300, None, None, '["Artist B"]', 0),
    ("spotify:s3", "spotify", "Artist C", "Gone Song", 180, "greyed out on Spotify", None, '["Artist C"]', 0),
    (
        "soundcloud:1001",
        "soundcloud",
        "Uploader",
        "Trance Tune",
        400.1,
        None,
        "Uploader/Uploader - Trance Tune",
        None,
        1,
    ),
    ("soundcloud:1002", "soundcloud", "Label", "Locked", 250, "not downloadable on SoundCloud (DRM)", None, None, 0),
]
MEMBERS = {
    "spotify:likes:1": ["spotify:s1", "spotify:s2", "spotify:s3"],
    "spotify:playlist:AAA111": ["spotify:s1"],
    "spotify:playlist:BBB222": ["spotify:s3"],
    "soundcloud:someone/likes": ["soundcloud:1001", "soundcloud:1002"],
    "soundcloud:someone/sets/trance": ["soundcloud:1001"],
}
NO_ARTIST = "artist 'Artist C' not in []"  # a rejection the review page does not show
GONE = (NO_ARTIST, "spotify:s3")  # reason, song
FIRST = ("Artist A/Artist A - First Song.mp3", "mp3", 10, 320, 201)  # path, ext, bytes, kbps, seconds
EVENTS = [  # ts, action, path, ext, bytes, kbps, seconds, source, artist, title, reason, song
    ("2026-09-26T10:00:00", "new", *FIRST, "soulseek", "Artist A", "First Song", None, "spotify:s1"),
    ("2026-09-26T11:00:00", "wrong-song", "x.flac", *[None] * 4, "soulseek", "Artist C", "Gone Song", *GONE),
]


def insert(con: sqlite3.Connection, table: str, columns: str, rows: list[tuple]) -> None:
    con.executemany(f"INSERT INTO {table} ({columns}) VALUES ({', '.join('?' * len(rows[0]))})", rows)


def seed(con: sqlite3.Connection) -> None:
    """The small collection: lists, songs (s3 greyed out and searched three times, 1002 locked on
    SoundCloud), two logged downloads, a FLAC made from a lossy file; the jobs paused."""
    today, now = datetime.date.today().isoformat(), datetime.datetime.now().isoformat(timespec="seconds")
    sources = [(OWNER, *row[:5], row[6], n, now) for n, row in enumerate(LISTS)]  # without the fetched title
    lists = [(k, s, title, url, n, playlist) for n, (k, s, _, url, _, title, playlist) in enumerate(LISTS)]
    members = [(key, n, song) for key, songs in MEMBERS.items() for n, song in enumerate(songs)]
    history = [(key, song, today, today) for key, _, song in members]
    with con:
        con.execute(
            "INSERT INTO users (id, name, created, navidrome_id, navidrome_admin, soundcloud_user) "
            "VALUES (?, 'owner', ?, 'nd-owner', 1, 'someone')",
            (OWNER, now),
        )
        insert(con, "sources", "user_id, key, service, likes, url, title, playlist, position, added", sources)
        insert(con, "lists", "key, service, title, url, position, playlist", lists)
        con.execute("UPDATE lists SET cover_url = ? WHERE key = 'spotify:likes:1'", (spotify.LIKED_SONGS_IMAGE,))
        insert(con, "songs", "key, service, artist, title, length, unavailable, stem, artists, archived", SONGS)
        insert(con, "list_songs", "list_key, position, song_key", members)
        insert(con, "list_history", "list_key, song_key, first_seen, last_seen", history)
        insert(con, "attempts", "song_key, tries, last_try, last_fallback", [("spotify:s3", 3, 1790000000, 0)])
        insert(con, "lossy_sourced", "stem, source", [("Artist B/Artist B - Second Song", "~128 kbps")])
        columns = "ts, action, path, ext, bytes, kbps, seconds, source, artist, title, reason, song"
        insert(con, "events", columns, EVENTS)
        db.set_meta(con, "playlist_files", "[]")
        options.update(con, options.Jobs, paused=True)
        options.update(con, options.SourceOptions, soundcloud_user="someone")


@pytest.fixture(autouse=True)
def offline_audio_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The audio check (identity.py) never asks Deezer in tests."""

    def offline(url: str, raw: bool = False) -> None:
        raise OSError("offline")

    monkeypatch.setattr("echolot.library.identity._get", offline)


@pytest.fixture
def library_dir(tmp_path: Path) -> Path:
    """Library files (not real audio: their durations are unknown)."""
    root = tmp_path / "tracks"
    for rel in [
        "Artist A/Artist A - First Song.mp3",
        "Artist B/Artist B - Second Song.flac",
        "Uploader/Uploader - Trance Tune.m4a",
        "Other/Other - Song.txt",
    ]:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"0123456789")
    return root


@pytest.fixture
def bare_settings(tmp_path: Path, library_dir: Path) -> Settings:
    """Settings with an empty database."""
    s = Settings(data_dir=tmp_path / "data", library_dir=library_dir, host="127.0.0.1", port=0,
                 daemon_dir=tmp_path / "daemon")  # fmt: skip
    db.init(s.db_path)
    return s


@pytest.fixture
def settings(bare_settings: Settings) -> Settings:
    """The small collection in the database, the library scanned and matched, the first snapshot stored."""
    con = db.connect(bare_settings.db_path)
    try:
        seed(con)
        catalog.refresh(con, bare_settings.library_dir)
        history.snapshot(con)
    finally:
        con.close()
    return bare_settings


@pytest.fixture(autouse=True)
def fake_navidrome(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Navidrome's login, faked: PASSWORD is everyone's password; the returned map makes a name a
    Navidrome admin (its id is 'nd-<name>')."""
    from echolot.services import navidrome

    admins: dict[str, bool] = {}

    def login(url: str, name: str, password: str) -> tuple[str, bool, str] | None:
        return (name, admins.get(name, False), f"nd-{name.lower()}") if password == PASSWORD else None

    monkeypatch.setenv("ECHOLOT_NAVIDROME_URL", "http://navidrome.test:4533")
    monkeypatch.setattr(navidrome, "login", login)
    return admins


def logged_in(app: FastAPI, name: str = "owner", admin: bool = True) -> TestClient:
    """A client (no `with`: the scheduler thread stays off) logged in with a Navidrome account (an admin
    in Echolot, or not; by default the owner of the small collection), sending the session's CSRF token
    with every request."""
    client = TestClient(app)
    con = db.connect(app.state.settings.db_path)
    try:
        user = auth.logged_in(con, name, f"nd-{name.lower()}", False)
        auth.set_rights(con, user.id, admin, set())
    finally:
        con.close()
    r = client.post("/login", data={"name": name, "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303, r.text
    con = db.connect(app.state.settings.db_path)
    try:
        csrf = con.execute("SELECT csrf FROM sessions ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    finally:
        con.close()
    client.headers["X-CSRF-Token"] = csrf
    return client


@pytest.fixture
def login() -> Callable[..., TestClient]:
    return logged_in
