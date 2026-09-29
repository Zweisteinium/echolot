"""The library scan, song matching and the pages' queries, on the small collection of conftest.seed."""

import os
from pathlib import Path

import pytest
from conftest import insert

from echolot import db
from echolot.config import Settings
from echolot.library import catalog
from echolot.web import stats


@pytest.fixture
def con(settings: Settings):
    con = db.connect(settings.db_path)
    yield con
    con.close()


def test_lists_in_config_order(con) -> None:
    rows = con.execute("SELECT key, title, playlist FROM lists ORDER BY position").fetchall()
    assert [tuple(r) for r in rows] == [
        ("spotify:likes", "Liked Songs", 1),
        ("spotify:playlist:AAA111", "Playlist A", 1),
        ("spotify:playlist:BBB222", "Renamed", 0),
        ("soundcloud:someone/likes", "SoundCloud Likes", 1),
        ("soundcloud:someone/sets/trance", "Trance", 1),
    ]


def test_songs_matched_to_library_files(con) -> None:
    songs = {r["key"]: r for r in con.execute("SELECT * FROM songs")}
    assert set(songs) == {"spotify:s1", "spotify:s2", "spotify:s3", "soundcloud:1001", "soundcloud:1002"}
    assert songs["spotify:s1"]["file"] == "Artist A/Artist A - First Song.mp3"
    assert songs["spotify:s2"]["file"] == "Artist B/Artist B - Second Song.flac"  # (Original Mix)
    assert songs["soundcloud:1001"]["file"] == "Uploader/Uploader - Trance Tune.m4a"
    assert songs["spotify:s3"]["file"] is None
    assert songs["spotify:s3"]["unavailable"] == "greyed out on Spotify"
    assert songs["soundcloud:1002"]["unavailable"].startswith("not downloadable")


def test_files_and_quality(con) -> None:
    quality = dict(con.execute("SELECT path, quality FROM files").fetchall())
    assert quality == {
        "Artist A/Artist A - First Song.mp3": "lossy-low",  # not real audio: 0 kbps
        "Artist B/Artist B - Second Song.flac": "fake",  # made from a lossy file
        "Uploader/Uploader - Trance Tune.m4a": "lossy-low",
    }


def test_missing_and_overview(con) -> None:
    assert {r["key"] for r in stats.missing(con)} == {"spotify:s3", "soundcloud:1002"}
    only = stats.missing(con, "spotify:playlist:BBB222")
    assert [(r["key"], r["tries"], r["in_lists"]) for r in only] == [("spotify:s3", 3, "Liked Songs #3 · Renamed #1")]
    o = stats.overview(con)
    assert (o["files"], o["wanted"], o["have"], o["not_found"]) == (3, 5, 3, 1)
    assert [(r["key"], r["songs"], r["have"]) for r in o["lists"]][:2] == [
        ("spotify:likes", 3, 2),
        ("spotify:playlist:AAA111", 1, 1),
    ]


def test_scan_notices_changes(con, settings: Settings) -> None:
    (settings.library_dir / "Artist A" / "Artist A - First Song.mp3").unlink()
    new = settings.library_dir / "Artist C" / "Artist C - Gone Song.flac"
    new.parent.mkdir()
    new.write_bytes(b"x")
    catalog.refresh(con, settings.library_dir)
    songs = dict(con.execute("SELECT key, file FROM songs").fetchall())
    assert songs["spotify:s1"] is None
    assert songs["spotify:s3"] == "Artist C/Artist C - Gone Song.flac"


def test_scan_keeps_data_when_library_vanishes(con, settings: Settings, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RuntimeError):
        catalog.scan(con, empty)
    with pytest.raises(FileNotFoundError):
        catalog.scan(con, tmp_path / "nowhere")
    assert con.execute("SELECT count(*) FROM files").fetchone()[0] == 3


def test_scan_uses_known_durations(con, settings: Settings) -> None:
    path = settings.library_dir / "Uploader" / "Uploader - Trance Tune.m4a"
    st = path.stat()
    known = {"Uploader/Uploader - Trance Tune.m4a": (st.st_size + 1, int(st.st_mtime), 400.0, 256)}
    path.write_bytes(path.read_bytes() + b"1")  # changed: size now matches `known`
    os.utime(path, (st.st_atime, st.st_mtime))
    catalog.scan(con, settings.library_dir, known)
    row = con.execute("SELECT duration, kbps FROM files WHERE path LIKE 'Uploader/%'").fetchone()
    assert tuple(row) == (400.0, 256)


def test_probes_summarised(con) -> None:
    rare, hit, evening, night = (
        ("A", "Rare", "rare"),
        ("B", "Hit", "common"),
        "2026-09-27T20:05:00",
        "2026-09-28T03:05:00",
    )
    rows = [(evening, *rare, 2, 1, 3), (evening, *hit, 200, 80, 400), (night, *rare, 0, 0, 0)]
    with con:
        insert(con, "probes", "ts, artist, title, kind, users, lossless_users, files", rows)
    a = stats.availability(con)
    assert a["runs"] == 2
    rare = {r["hour"]: r for r in a["hours"]["rare"]}
    assert (rare[20]["users"], rare[3]["users"], rare[12]["users"]) == (2, 0, None)
    song = {s["title"]: s for s in a["songs"]}["Rare"]
    assert (song["probes"], song["found"], song["max_users"]) == (2, 0.5, 2)
