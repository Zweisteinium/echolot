"""Import from the pipeline, library scan and song matching, end to end."""

import json
import os
from pathlib import Path

import pytest

from echolot import db, jobs, library, pipeline, stats
from echolot.config import Settings


@pytest.fixture
def con(settings: Settings):
    con = db.connect(settings.db_path)
    jobs.refresh(settings, con)
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
    assert set(songs) == {
        "spotify:s1",
        "spotify:s2",
        "spotify:s3",
        "soundcloud:1001",
        "soundcloud:1002",
    }
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
        "Artist B/Artist B - Second Song.flac": "fake",  # in lossy-sourced.json
        "Uploader/Uploader - Trance Tune.m4a": "lossy-low",
    }


def test_missing_and_overview(con) -> None:
    assert {r["key"] for r in stats.missing(con)} == {"spotify:s3", "soundcloud:1002"}
    only = stats.missing(con, "spotify:playlist:BBB222")
    assert [(r["key"], r["tries"], r["in_lists"]) for r in only] == [
        ("spotify:s3", 3, "Liked Songs · Renamed")
    ]
    o = stats.overview(con)
    assert (o["files"], o["wanted"], o["have"], o["not_found"]) == (3, 5, 3, 1)
    assert [(r["key"], r["songs"], r["have"]) for r in o["lists"]][:2] == [
        ("spotify:likes", 3, 2),
        ("spotify:playlist:AAA111", 1, 1),
    ]


def test_events_are_imported_once(con, settings: Settings) -> None:
    assert con.execute("SELECT count(*) FROM events").fetchone()[0] == 2
    log = settings.pipeline_dir / "logs" / "downloads.jsonl"
    assert pipeline.import_events(con, log) == 0
    with log.open("a") as f:
        f.write(json.dumps({"ts": "2026-09-27T09:00:00", "action": "new", "path": "x"}) + "\n")
        f.write('{"ts": "2026-09-27T09:01:00", "act')  # being written: not imported yet
    assert pipeline.import_events(con, log) == 1
    assert [r["action"] for r in stats.events(con, "added")] == ["new", "new"]
    assert [r["action"] for r in stats.events(con, "rejected")] == ["wrong-song"]


def test_scan_notices_changes(con, settings: Settings) -> None:
    (settings.library_dir / "Artist A" / "Artist A - First Song.mp3").unlink()
    new = settings.library_dir / "Artist C" / "Artist C - Gone Song.flac"
    new.parent.mkdir()
    new.write_bytes(b"x")
    jobs.refresh(settings, con)
    songs = dict(con.execute("SELECT key, file FROM songs").fetchall())
    assert songs["spotify:s1"] is None
    assert songs["spotify:s3"] == "Artist C/Artist C - Gone Song.flac"


def test_scan_keeps_data_when_library_vanishes(con, settings: Settings, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RuntimeError):
        library.scan(con, empty)
    with pytest.raises(FileNotFoundError):
        library.scan(con, tmp_path / "nowhere")
    assert con.execute("SELECT count(*) FROM files").fetchone()[0] == 3


def test_scan_uses_known_durations(con, settings: Settings) -> None:
    path = settings.library_dir / "Uploader" / "Uploader - Trance Tune.m4a"
    st = path.stat()
    known = {"Uploader/Uploader - Trance Tune.m4a": (st.st_size + 1, int(st.st_mtime), 400.0, 256)}
    path.write_bytes(path.read_bytes() + b"1")  # changed: size now matches `known`
    os.utime(path, (st.st_atime, st.st_mtime))
    library.scan(con, settings.library_dir, known)
    row = con.execute("SELECT duration, kbps FROM files WHERE path LIKE 'Uploader/%'").fetchone()
    assert tuple(row) == (400.0, 256)


def test_pipeline_activity_from_logs(settings: Settings) -> None:
    logs = settings.pipeline_dir / "logs"
    (logs / "sync.log").write_text(
        "2026-09-27 00:20:00 === music-sync sync start\nsome output\n"
        "2026-09-27 00:23:02 === music-sync sync done\n"
        "2026-09-27 00:50:00 === music-sync sync start\n"
    )
    (logs / "soundcloud.log").write_text(
        "2026-09-27 00:05:00 === music-sync soundcloud start\n"
        "2026-09-27 00:05:19 === music-sync soundcloud done\n"
    )
    jobs_ = {a.job: a for a in pipeline.activity(settings.pipeline_dir)}
    assert jobs_["sync"].running and jobs_["sync"].started == "2026-09-27T00:50:00"
    assert not jobs_["soundcloud"].running
    assert jobs_["soundcloud"].finished == "2026-09-27T00:05:19"
    assert not jobs_["upgrade"].running and jobs_["upgrade"].started is None
    assert not pipeline.paused(settings.pipeline_dir)
    (settings.pipeline_dir / "state" / "PAUSED").touch()
    assert pipeline.paused(settings.pipeline_dir)


def test_probes_imported_and_summarised(con, settings: Settings) -> None:
    log = settings.pipeline_dir / "logs" / "probe.jsonl"
    lines = [
        {"ts": "2026-09-27T20:05:00", "artist": "A", "title": "Rare", "kind": "rare", "users": 2,
         "lossless_users": 1, "files": 3},
        {"ts": "2026-09-27T20:05:00", "artist": "B", "title": "Hit", "kind": "common", "users": 200,
         "lossless_users": 80, "files": 400},
        {"ts": "2026-09-28T03:05:00", "artist": "A", "title": "Rare", "kind": "rare", "users": 0,
         "lossless_users": 0, "files": 0},
    ]  # fmt: skip
    log.write_text("".join(json.dumps(x) + "\n" for x in lines))
    assert pipeline.import_probes(con, log) == 3
    assert pipeline.import_probes(con, log) == 0
    a = stats.availability(con)
    assert a["runs"] == 2
    rare = {r["hour"]: r for r in a["hours"]["rare"]}
    assert (rare[20]["users"], rare[3]["users"], rare[12]["users"]) == (2, 0, None)
    song = {s["title"]: s for s in a["songs"]}["Rare"]
    assert (song["probes"], song["found"], song["max_users"]) == (2, 0.5, 2)
