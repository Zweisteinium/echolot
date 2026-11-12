"""Covers from your lists (jobs/covers.py, audio.picture and embed_cover): the song's cover replaces the
uploader's, the old one is backed up, and a run goes on where the last one stopped."""

import os
import shutil
import threading
import time
from pathlib import Path
from typing import ClassVar

import pytest

from echolot import db
from echolot.config import Settings
from echolot.jobs import covers
from echolot.jobs.schedule import BY_NAME
from echolot.jobs.worker import Run
from echolot.library import audio, catalog, tagging
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

    def of(self, song) -> bytes:  # s1's own release is a single
        asked.append(song["key"])
        if song["key"] == "spotify:s1":
            covers.store(self.con, "spotify:s1", _release("single", "2015-01-20", "First Single"), "isrc")
        return SONG

    monkeypatch.setattr(covers.Covers, "of", of)
    message = covers.run(run)
    first = lib / "Artist A" / "Artist A - First Song.flac"
    assert audio.picture(first) == SONG and audio.picture(lib / "Artist B" / "Artist B - Second Song.flac") == SONG
    assert (
        settings.data_dir / "cover-backups" / "Artist A" / "Artist A - First Song.flac.jpg"
    ).read_bytes() == UPLOADER
    assert message.startswith("2 replaced") and sorted(asked) == ["spotify:s1", "spotify:s2"]
    assert "retagged" in message and tagging.read(first)["album"] == "First Single"  # its tags: the single's
    assert "First Song" in (settings.data_dir / "cover-backups" / "tags.jsonl").read_text()  # the old ones kept
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


def _release(
    kind: str,
    date: str,
    name: str,
    artist: str = "Artist A",
    isrc: str = "ISRC1",
    title: str = "Song",
    ms: int = 200000,
) -> dict:
    album = {"id": name, "album_type": kind, "release_date": date, "name": name, "images": [{"url": f"u/{name}"}]}
    album |= {"artists": [{"name": artist}], "total_tracks": 1 if kind == "single" else 12}
    track = {"name": title, "artists": [{"name": artist}], "duration_ms": ms, "track_number": 1, "disc_number": 1}
    return track | {"external_ids": {"isrc": isrc}, "album": album}


SONG_ROW = {"isrc": "isrc1", "artist": "Artist A", "artists": '["Guest"]', "album": "The Album", "title": "Song"}


def name(track: dict | None) -> str | None:
    return track["album"]["name"] if track else None


def test_the_songs_own_release() -> None:
    """A song's cover is its own release's: its single, else its album, by its artists and out with the
    recording; the list's own release when it is one of the kind; none (the list's) for compilations only."""
    song = SONG_ROW | {"length": 200}
    album, single = _release("album", "2015-03-01", "The Album"), _release("single", "2015-01-20", "The Single")
    various = _release("compilation", "2014-12-01", "Hits 2015", "Various Artists")
    assert name(covers.own_release([various, album, single], song)) == "The Single"
    assert name(covers.own_release([various, album], song)) == "The Album"
    late = _release("single", "2021-10-29", "Anniversary Single")  # a re-release years later
    assert name(covers.own_release([album, late], song)) == "The Album"
    deluxe = _release("album", "2014-11-01", "The Album (Deluxe)")
    assert name(covers.own_release([deluxe, album], song)) == "The Album"  # the list's own of the kind
    assert name(covers.own_release([deluxe, album], song | {"album": "Hits 2015"})) == "The Album (Deluxe)"
    soundtrack = _release("album", "2015-02-01", "A Film", "Various Artists")
    assert covers.own_release([various, soundtrack], song) is None  # not by its artists: the list's release
    guest = _release("single", "2015-02-01", "Guest Single", "guest")  # a featured artist's single counts
    assert name(covers.own_release([album, guest], song)) == "Guest Single"
    other = _release("single", "2015-01-01", "Another Recording", isrc="ISRC2")
    assert name(covers.own_release([other, album], song)) == "The Album"  # only its ISRC's
    assert covers.own_release([], song) is None
    big = _release("single", "2015-01-10", "Called A Single") | {}
    big["album"] = big["album"] | {"total_tracks": 10}  # an album Spotify calls a single
    assert name(covers.own_release([big, album], song)) == "The Album"  # the list's of the kind
    later = _release("album", "2016-07-27", "The Album 3+ (Deluxe)")  # the search shows only a later one
    assert covers.own_release([later], song | {"album": "The Album (Savage)", "released": "2015-11-06"}) is None
    assert name(covers.own_release([later], song | {"released": "2017"})) == "The Album 3+ (Deluxe)"


def test_a_song_on_compilations_only_by_its_names() -> None:
    """No own release under its ISRC (a remaster, a compilation's licence): the original by its names, the
    same title without what a release adds and as long (NAMES_LENGTH); not another version or length."""
    song = SONG_ROW | {"title": 'Night Fever - From "Saturday Night Fever" Soundtrack', "length": 213.0}
    assert covers.by_names(song) == "track:Night Fever artist:Artist A"
    assert covers.by_names(song | {"length": 0}) == ""  # nothing to compare
    original = _release("single", "1977-12-01", "Night Fever", isrc="X1", title="Night Fever", ms=211500)
    album = _release("album", "1977-11-15", "Spirits", isrc="X2", title="Night Fever - Remastered 2007", ms=213000)
    remix = _release("single", "1977-12-01", "Remix", isrc="X3", title="Night Fever - Club Remix", ms=213000)
    longer = _release("single", "1977-12-01", "Long", isrc="X4", title="Night Fever", ms=330000)
    other = _release("single", "1977-12-01", "Other", "Someone Else", isrc="X5", title="Night Fever", ms=213000)
    assert name(covers.own_release([album, remix, longer, other, original], song, names=True)) == "Night Fever"
    assert name(covers.own_release([album, remix, longer, other], song, names=True)) == "Spirits"
    assert covers.own_release([remix, longer, other], song, names=True) is None
    duet = _release("single", "1977-12-01", "Duet", isrc="X6", title="Night Fever (with Guest Star)", ms=213000)
    duet["artists"] = [{"name": "Artist A"}, {"name": "Guest Star"}]  # another version, crediting another artist
    assert covers.own_release([duet], song, names=True) is None
    duet["artists"] = [{"name": "Artist A"}]
    assert covers.own_release([duet], song, names=True) is None  # (named in its title only)
    assert covers.own_release([original], song) is None  # by its ISRC: not one of these


class Sp:
    """Spotify for the cover lookups: `isrc` and `names` are what the two searches find."""

    isrc: ClassVar[list[dict]] = []
    names: ClassVar[list[dict]] = []
    asked: ClassVar[list[str]] = []

    def __init__(self, con, vault) -> None:
        pass

    def search(self, q: str, limit: int = 5, market: str = "") -> list[dict]:
        assert limit == covers.RELEASES
        self.asked.append(q)
        return self.isrc if q.startswith("isrc:") else self.names

    def track(self, sid: str) -> dict:
        self.asked.append(f"track {sid}")
        return {"album": {"id": "listed", "images": [{"url": "u/listed"}]}}


@pytest.fixture
def lookups(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The pictures fetched (their addresses); Spotify is Sp."""
    fetched: list[str] = []
    monkeypatch.setattr(covers.spotify, "Spotify", Sp)
    monkeypatch.setattr(Sp, "isrc", [])
    monkeypatch.setattr(Sp, "names", [])
    monkeypatch.setattr(Sp, "asked", [])
    monkeypatch.setattr(covers.sc_api, "any_token", lambda con, vault: None)
    monkeypatch.setattr(covers, "download", lambda url: fetched.append(url) or SONG)
    monkeypatch.setattr(covers.time, "sleep", lambda s: None)
    return fetched


def test_a_spotify_song_gets_its_singles_cover(settings: Settings, lookups: list[str]) -> None:
    """One search of the song's ISRC, its own release stored (song_releases) and its cover fetched once; by its
    names when its ISRC has none; the list's release (its track) when that finds none either."""
    run = Run(BY_NAME["covers"], settings, Vault.from_env(settings.data_dir, {}), "manual")
    Sp.isrc = [_release("compilation", "2016-01-01", "Hits", "Various Artists"), _release("single", "2015", "S")]
    con = db.connect(settings.db_path)
    c = covers.Covers(con, run)
    song = SONG_ROW | {"key": "spotify:t1", "isrc": "ISRC1", "album": "Hits", "length": 200}
    assert c.of(song) == SONG and lookups == ["u/S"] and Sp.asked == ["isrc:ISRC1"]
    row = con.execute("SELECT album, released, track, tracks, disc, how FROM song_releases").fetchone()
    assert tuple(row) == ("S", "2015", 1, 1, 1, "isrc")
    Sp.isrc, Sp.asked = Sp.isrc[:1], []  # compilations only: by its names
    Sp.names = [_release("album", "2015-03-01", "The Album", isrc="ISRC9", ms=201000)]
    assert (
        c.of(song) == SONG
        and lookups == ["u/S", "u/The Album"]
        and Sp.asked == ["isrc:ISRC1", "track:Song artist:Artist A"]
    )
    assert con.execute("SELECT album, how FROM song_releases").fetchone()[:] == ("The Album", "names")
    Sp.isrc, Sp.asked = [_release("album", "2016-07-27", "Hits")], []  # the list's own (an album): not by names
    assert c.of(song) == SONG and Sp.asked == ["isrc:ISRC1"] and lookups[-1] == "u/Hits"
    Sp.isrc, Sp.names, Sp.asked = Sp.isrc[:0], [], []  # nothing of its own: the list's release, nothing stored
    assert c.of(song) == SONG and lookups[-1] == "u/listed" and Sp.asked[-1] == "track t1"
    assert con.execute("SELECT count(*) FROM song_releases").fetchone()[0] == 0
    con.close()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_youtube_videos_picture_as_a_square(settings: Settings, lookups: list[str], monkeypatch) -> None:
    """A YouTube song Spotify has nothing of: its video's picture, the middle square of the largest thumbnail."""
    import subprocess

    wide = settings.data_dir / "wide.jpg"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=1280x720", "-frames:v", "1", str(wide)], check=True
    )

    def download(url: str) -> bytes:
        lookups.append(url)
        if "maxresdefault" in url:
            return wide.read_bytes()
        raise OSError("404")

    monkeypatch.setattr(covers, "download", download)
    run = Run(BY_NAME["covers"], settings, Vault.from_env(settings.data_dir, {}), "manual")
    con = db.connect(settings.db_path)
    c = covers.Covers(con, run)
    jpg = c.of(
        {
            "key": "youtube:abcdefghijk",
            "isrc": None,
            "artist": "A",
            "artists": None,
            "title": "T",
            "album": "",
            "length": 0,
        }
    )
    probe = ["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", "-"]
    assert lookups == ["https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg"]
    assert subprocess.run(probe, input=jpg, capture_output=True, text=False).stdout.strip() == b"720,720"
    assert c.of({"key": "youtube:../x", "isrc": None}) is None  # not a video id
    con.close()


def test_the_tags_follow_the_songs_own_release(settings: Settings) -> None:
    """A song with its own release stored is tagged with that release's album, date and track; the list's
    release's otherwise."""
    from echolot.library import tagging

    con = db.connect(settings.db_path)
    with con:
        con.execute(
            "INSERT INTO songs (key, service, artist, title, album, length, isrc, released, track, tracks, disc, file) "
            "VALUES ('spotify:t1', 'spotify', 'Artist A', 'Song', 'Hits 2015', 200, 'ISRC1', '2016-01-01', 7, 40, 1, "
            "'Artist A/Artist A - Song.flac')"
        )
    path = settings.library_dir / "Artist A" / "Artist A - Song.flac"
    path.parent.mkdir(exist_ok=True)
    shutil.copy(AUDIO / "silence.flac", path)
    tags = tagging.for_file(con, "Artist A/Artist A - Song.flac", path)
    assert (tags.album, tags.facts["date"], tags.facts["track"]) == ("Hits 2015", "2016-01-01", "7/40")
    covers.store(con, "spotify:t1", _release("single", "2015-01-20", "Song (Single)"), "isrc")
    tags = tagging.for_file(con, "Artist A/Artist A - Song.flac", path)
    assert (tags.album, tags.facts["date"], tags.facts["track"], tags.facts["isrc"]) == (
        "Song (Single)",
        "2015-01-20",
        "1/1",
        "ISRC1",
    )
    con.close()


def test_a_song_filed_is_tagged_as_its_own_release(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Filing (acquire.finish) finds the song's own release first, then tags the file as it."""
    from echolot.jobs import acquire
    from echolot.library.filing import Want
    from echolot.services import spotify

    run = Run(BY_NAME["covers"], settings, Vault.from_env(settings.data_dir, {}), "manual")
    rel = "Artist N/Artist N - New Song.flac"
    p = settings.library_dir / rel
    p.parent.mkdir(exist_ok=True)
    shutil.copy(AUDIO / "silence.flac", p)
    con = db.connect(settings.db_path)
    with con:
        con.execute(
            "INSERT INTO songs (key, service, artist, title, album, length, isrc, released, file) "
            "VALUES ('spotify:n1', 'spotify', 'Artist N', 'New Song', 'Best Of', 200, 'ISRC1', '2020', ?)",
            (rel,),
        )

    def of(self, song) -> bytes:
        covers.store(self.con, song["key"], _release("single", "2018-05-04", "New Song (Single)"), "isrc")
        return SONG

    monkeypatch.setattr(covers.Covers, "__init__", lambda self, con, run: setattr(self, "con", con))
    monkeypatch.setattr(covers.Covers, "of", of)
    monkeypatch.setattr(spotify, "Spotify", lambda con, vault: (_ for _ in ()).throw(spotify.SpotifyError("off")))
    acquire.finish(run, con, p, Want("Artist N", "New Song", 200, "spotify:n1"))
    con.close()
    now = tagging.read(p)
    assert audio.picture(p) == SONG and now["album"] == "New Song (Single)" and now["facts"]["date"] == "2018-05-04"
