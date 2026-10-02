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
