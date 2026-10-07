"""Covers from your lists (jobs/covers.py, audio.picture and embed_cover): the song's cover replaces the
uploader's, the old one is backed up, and a run goes on where the last one stopped."""

import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from echolot import db
from echolot.config import Settings
from echolot.jobs import covers
from echolot.jobs.schedule import BY_NAME
from echolot.jobs.worker import Run
from echolot.library import audio, catalog
from echolot.settings.vault import Vault

AUDIO = Path(__file__).parent / "fixtures" / "audio"
UPLOADER, SONG = b"\xff\xd8\xff\xe0uploader's compilation", b"\xff\xd8\xff\xe0the album on Spotify"


@pytest.mark.parametrize("ext", ["flac", "mp3", "m4a", "opus"])
def test_a_cover_is_replaced_only_when_asked(tmp_path: Path, ext: str) -> None:
    p = tmp_path / f"song.{ext}"
    shutil.copy(AUDIO / f"silence.{ext}", p)
    assert audio.picture(p) is None and audio.embed_cover(p, UPLOADER)
    assert not audio.embed_cover(p, SONG) and audio.picture(p) == UPLOADER  # one there: kept
    assert audio.embed_cover(p, SONG, replace=True) and audio.picture(p) == SONG  # replaced, not added


def test_the_covers_run(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    run = Run(BY_NAME["covers"], settings, Vault.from_env(settings.data_dir, {}), "manual")
    run.stop = threading.Event()
    lib = settings.library_dir
    for fake in [p for p in lib.rglob("*.*") if p.suffix != ".flac"]:  # the conftest's files that are no audio
        fake.unlink()
    for name, cover in (("First Song", UPLOADER), ("Second Song", None)):
        artist = "Artist A" if name == "First Song" else "Artist B"
        p = lib / artist / f"{artist} - {name}.flac"
        p.parent.mkdir(exist_ok=True)
        shutil.copy(AUDIO / "silence.flac", p)
        if cover:
            audio.embed_cover(p, cover)
        os.utime(p, (time.time() - 3600, time.time() - 3600))  # filed an hour ago
    con = db.connect(settings.db_path)
    catalog.scan(con, lib)
    with con:  # the songs' lengths (the silent files are a second long)
        con.execute("UPDATE files SET duration = 200 WHERE path LIKE 'Artist A/%'")
        con.execute("UPDATE files SET duration = 300 WHERE path LIKE 'Artist B/%'")
    catalog.match_songs(con)
    con.close()
    asked = []
    monkeypatch.setattr(covers.Covers, "of", lambda self, song: asked.append(song["key"]) or SONG)
    message = covers.run(run)
    first = lib / "Artist A" / "Artist A - First Song.flac"
    assert audio.picture(first) == SONG and audio.picture(lib / "Artist B" / "Artist B - Second Song.flac") == SONG
    assert (
        settings.data_dir / "cover-backups" / "Artist A" / "Artist A - First Song.flac.jpg"
    ).read_bytes() == UPLOADER
    assert message.startswith("2 replaced") and sorted(asked) == ["spotify:s1", "spotify:s2"]
    asked.clear()
    assert covers.run(run) == "nothing to do" and asked == []  # done files are not asked again


def test_a_discover_songs_cover(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A song hearted in a player: the album of the Spotify track with its ISRC; without one (or Spotify
    knows none), the picture Discover showed, large."""
    run = Run(BY_NAME["covers"], settings, Vault.from_env(settings.data_dir, {}), "manual")
    con = db.connect(settings.db_path)
    with con:
        con.execute(
            "INSERT INTO discover_songs (id, data, seen) VALUES ('ex-abc', ?, 'now')",
            ('{"cover": "https://cdn-images.dzcdn.net/images/cover/x/250x250-000000-80-0-0.jpg"}',),
        )
    fetched: list[str] = []
    monkeypatch.setattr(covers, "download", lambda url: fetched.append(url) or SONG)
    c = covers.Covers(con, run)
    c.sp = None  # no Spotify: Discover's picture
    song = {"key": "discover:abc", "isrc": "GB2LD0901580"}
    assert c.of(song) == SONG and fetched == ["https://cdn-images.dzcdn.net/images/cover/x/1000x1000-000000-80-0-0.jpg"]

    class Sp:
        def search(self, q: str, limit: int) -> list[dict]:
            return [{"id": "t1"}] if q == "isrc:GB2LD0901580" else []

        def track(self, tid: str) -> dict:
            return {"album": {"id": "a1", "images": [{"url": "https://i.scdn.co/a1"}]}}

    monkeypatch.setattr(covers.time, "sleep", lambda s: None)
    c.sp, c.cache = Sp(), {}
    assert c.of(song) == SONG and fetched[-1] == "https://i.scdn.co/a1"
    con.close()
