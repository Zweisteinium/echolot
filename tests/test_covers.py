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
    for f in lib.rglob("*.flac"):  # covered an hour ago
        os.utime(f, (time.time() - 3600, time.time() - 3600))
    run_all = Run(BY_NAME["covers_all"], settings, Vault.from_env(settings.data_dir, {}), "manual")
    run_all.stop = threading.Event()
    assert covers.run_all(run_all) == "2 already the song's" and len(asked) == 2  # Replace all: every file again
    asked.clear()
    assert covers.run(run) == "nothing to do" and asked == []  # and noted done again


def test_a_file_filed_with_its_cover_is_noted_done(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A song filed gets its cover (acquire.pictures) and is noted done: Add missing does not ask for it again."""
    from echolot.jobs import acquire
    from echolot.library.filing import Want
    from echolot.services import spotify

    run = Run(BY_NAME["covers"], settings, Vault.from_env(settings.data_dir, {}), "manual")
    p = settings.library_dir / "Artist N" / "Artist N - New Song.flac"
    p.parent.mkdir(exist_ok=True)
    shutil.copy(AUDIO / "silence.flac", p)
    monkeypatch.setattr(covers.Covers, "__init__", lambda self, con, run: None)
    monkeypatch.setattr(covers.Covers, "of", lambda self, song: SONG)
    monkeypatch.setattr(acquire.tagging, "lead", lambda con, rel, key=None: {"key": "spotify:n1"})
    monkeypatch.setattr(spotify, "Spotify", lambda con, vault: (_ for _ in ()).throw(spotify.SpotifyError("off")))
    con = db.connect(settings.db_path)
    want = Want("Artist N", "New Song", 200, "spotify:n1")
    acquire.pictures(run, con, p, want, None)
    con.close()
    assert audio.picture(p) == SONG
    assert covers.journal(settings.data_dir).read_text().splitlines() == ["Artist N/Artist N - New Song.flac"]


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


def test_one_files_trouble_ends_no_covers_run(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """An error the cover step doesn't expect (a cut-off download) fails that file only; the run goes on."""
    import http.client

    run = Run(BY_NAME["covers"], settings, Vault.from_env(settings.data_dir, {}), "manual")
    run.stop = threading.Event()
    lib = settings.library_dir
    for fake in [p for p in lib.rglob("*.*") if p.suffix != ".flac"]:
        fake.unlink()
    for artist, name in (("Artist A", "First Song"), ("Artist B", "Second Song")):
        p = lib / artist / f"{artist} - {name}.flac"
        p.parent.mkdir(exist_ok=True)
        shutil.copy(AUDIO / "silence.flac", p)
        os.utime(p, (time.time() - 3600, time.time() - 3600))
    con = db.connect(settings.db_path)
    catalog.scan(con, lib)
    with con:
        con.execute("UPDATE files SET duration = 200 WHERE path LIKE 'Artist A/%'")
        con.execute("UPDATE files SET duration = 300 WHERE path LIKE 'Artist B/%'")
    catalog.match_songs(con)
    con.close()

    def of(self, song):
        if song["key"] == "spotify:s1":
            raise http.client.IncompleteRead(b"")
        return SONG

    monkeypatch.setattr(covers.Covers, "of", of)
    message = covers.run(run)
    assert "1 replaced" in message and "1 failed" in message


def _release(kind: str, date: str, name: str, artist: str = "Artist A", isrc: str = "ISRC1") -> dict:
    album = {"id": name, "album_type": kind, "release_date": date, "name": name, "images": [{"url": f"u/{name}"}]}
    return {"external_ids": {"isrc": isrc}, "album": album | {"artists": [{"name": artist}]}}


def test_the_songs_own_release() -> None:
    """A song's cover is its own release's: its single, else its album, by its artists and out with the
    recording; the list's own release when it is one of the kind; none (the list's) for compilations only."""
    song = {"isrc": "isrc1", "artist": "Artist A", "artists": '["Guest"]', "album": "The Album"}
    album, single = _release("album", "2015-03-01", "The Album"), _release("single", "2015-01-20", "The Single")
    various = _release("compilation", "2014-12-01", "Hits 2015", "Various Artists")
    assert covers.own_release([various, album, single], song)["name"] == "The Single"
    assert covers.own_release([various, album], song)["name"] == "The Album"
    late = _release("single", "2021-10-29", "Anniversary Single")  # a re-release years later
    assert covers.own_release([album, late], song)["name"] == "The Album"
    deluxe = _release("album", "2014-11-01", "The Album (Deluxe)")
    assert covers.own_release([deluxe, album], song)["name"] == "The Album"  # the list's own of the kind
    assert covers.own_release([deluxe, album], song | {"album": "Hits 2015"})["name"] == "The Album (Deluxe)"
    soundtrack = _release("album", "2015-02-01", "A Film", "Various Artists")
    assert covers.own_release([various, soundtrack], song) is None  # not by its artists: the list's release
    guest = _release("single", "2015-02-01", "Guest Single", "guest")  # a featured artist's single counts
    assert covers.own_release([album, guest], song)["name"] == "Guest Single"
    other = _release("single", "2015-01-01", "Another Recording", isrc="ISRC2")
    assert covers.own_release([other, album], song)["name"] == "The Album"  # only its ISRC's
    assert covers.own_release([], song) is None


def test_a_spotify_song_gets_its_singles_cover(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """One search of the song's ISRC; its own release's cover, fetched once; the list's release (its track)
    only when there is none."""
    run = Run(BY_NAME["covers"], settings, Vault.from_env(settings.data_dir, {}), "manual")
    fetched, tracks = [], []

    class Sp:
        def __init__(self, con, vault) -> None:
            pass

        def search(self, q: str, limit: int = 5, market: str = "") -> list[dict]:
            assert (q, limit) == ("isrc:ISRC1", covers.RELEASES)
            return [_release("compilation", "2016-01-01", "Hits", "Various Artists"), _release("single", "2015", "S")]

        def track(self, sid: str) -> dict:
            tracks.append(sid)
            return {"album": {"id": "listed", "images": [{"url": "u/listed"}]}}

    monkeypatch.setattr(covers.spotify, "Spotify", Sp)
    monkeypatch.setattr(covers.sc_api, "any_token", lambda con, vault: None)
    monkeypatch.setattr(covers, "download", lambda url: fetched.append(url) or SONG)
    monkeypatch.setattr(covers.time, "sleep", lambda s: None)
    con = db.connect(settings.db_path)
    c = covers.Covers(con, run)
    song = {"key": "spotify:t1", "isrc": "ISRC1", "artist": "Artist A", "artists": None, "album": "Hits"}
    assert c.of(song) == SONG and fetched == ["u/S"] and tracks == []
    assert c.of(song | {"isrc": None}) == SONG and fetched == ["u/S", "u/listed"] and tracks == ["t1"]
    con.close()
