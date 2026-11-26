"""Lyrics (library/lyrics, jobs/lyrics): only an LRCLIB entry that surely is the song's counts, embedded
where players read it; a file's own lyrics stay."""

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from echolot import db
from echolot.config import Settings
from echolot.jobs import lyrics as job
from echolot.jobs.schedule import BY_NAME
from echolot.jobs.worker import Run
from echolot.library import audio, catalog, lyrics
from echolot.services import lrclib
from echolot.settings.vault import Vault

AUDIO = Path(__file__).parent / "fixtures" / "audio"
SONG = {"key": "spotify:c1", "artist": "Radiohead", "artists": '["Radiohead"]', "title": "Creep"}
SONG |= {"album": "Pablo Honey", "artist_alias": None}
SYNCED = "[00:19.16] When you were here before\n[00:24.09] Couldn't look you in the eye"


def entry(id_: int = 1, artist: str = "Radiohead", title: str = "Creep", length: float = 239, **more) -> dict:
    return {"id": id_, "artistName": artist, "trackName": title, "albumName": "Pablo Honey", "duration": length} | more


@pytest.mark.parametrize(
    ("found", "why"),
    [
        (entry(), None),
        (entry(title="Creep - Remastered 2009"), None),
        (entry(artist="Radiohead, Someone"), None),
        (entry(length=240.5), None),
        (entry(length=248), "248 s long"),  # another recording's entry (LRCLIB has several per song)
        (entry(artist="Radiohead Tribute Band"), "not by the song's artists"),
        (entry(artist="Scala & Kolacny Brothers"), "not by the song's artists"),
        (entry(title="Creep (Acoustic)"), "is not"),
        (entry(title="Creep (Live)"), "is not"),
        (entry(title="Creep (A Cappella)"), "is not"),
        (entry(title="Creep (Tiny Desk Concert)"), "is not"),
        (entry(title="Creeping"), "is not"),
        (entry(syncedLyrics="[00:10.00] a\n[04:30.00] b"), "past the file's end"),
    ],
)
def test_an_entry_counts_only_when_it_surely_is_the_song(found: dict, why: str | None) -> None:
    got = lyrics.fits(SONG, found, 239.0)
    assert got is None if why is None else why in (got or "")


def test_titles_compare_without_what_a_release_adds() -> None:
    assert lyrics.same_title("Song - Radio Edit", "Song") and lyrics.same_title("Song", "Song (Single Version)")
    assert lyrics.same_title("Song - X Remix", "Song (X Remix)") and lyrics.same_title("Song (feat. Drake)", "Song")
    assert not lyrics.same_title("Song - X Remix", "Song") and not lyrics.same_title("Song (Instrumental)", "Song")
    assert not lyrics.same_title("Song (feat. Drake)", "Song (feat. Future)")


def test_credits_and_lyrics_sites_headers() -> None:
    assert lyrics.credited(SONG, "Radiohead - Topic") and lyrics.credited(SONG, "Radiohead y Someone")
    assert not lyrics.credited(SONG, "Radiohead Tribute Band")
    site = "Radiohead\nMiscellaneous\nCreep\nWhen you were here before"
    assert lyrics._headless(SONG, site) == "When you were here before"
    assert lyrics._headless(SONG, "Writer(s): Yorke\n\nWhen you were here before") == "When you were here before"
    assert lyrics._headless(SONG, "Creep\nCreep, creep") == "Creep\nCreep, creep"  # a song may start so


def test_lyrics_are_asked_by_the_songs_names_then_searched(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple] = []
    answers = {
        ("Radiohead", "Creep", "Pablo Honey"): entry(artist="Radiohead Tribute Band", plainLyrics="wrong"),
        ("search", "Radiohead", "Creep"): [
            entry(2, length=240, plainLyrics="plain only"),
            entry(3, length=238.5, plainLyrics="x", syncedLyrics=SYNCED),
            entry(4, length=300, plainLyrics="y", syncedLyrics=SYNCED),
        ],
    }

    def get(artist: str, title: str, album: str | None, length: float) -> dict | None:
        asked.append((artist, title, album))
        return answers.get((artist, title, album))

    def search(artist: str, title: str) -> list[dict]:
        asked.append(("search", artist, title))
        return answers.get(("search", artist, title), [])

    monkeypatch.setattr(lrclib, "get", get)
    monkeypatch.setattr(lrclib, "search", search)
    found = lyrics.find(SONG, 239.0)
    assert found == lyrics.Lyrics("synced", SYNCED, "lrclib:3")  # the synced one that fits, not the nearest plain
    assert asked == [("Radiohead", "Creep", "Pablo Honey"), ("search", "Radiohead", "Creep")]
    answers[("Radiohead", "Creep", "Pablo Honey")] = entry(9, instrumental=True)
    assert lyrics.find(SONG, 239.0) == lyrics.Lyrics("instrumental", "", "lrclib:9")
    answers.clear()
    assert lyrics.find(SONG, 239.0) is None
    asked.clear()
    feat = SONG | {"artists": '["Radiohead", "Someone"]', "title": "Creep (feat. Someone) - Radio Edit"}
    assert lyrics.find(feat, 239.0) is None and asked[:2] == [
        ("Radiohead, Someone", "Creep (feat. Someone) - Radio Edit", "Pablo Honey"),
        ("Radiohead", "Creep", None),  # the first artist and the title without what the release adds
    ]
    assert lyrics.find(SONG | {"title": "Creep (Mixed)"}, 239.0) is None  # a DJ-mix cut: not asked


@pytest.mark.parametrize("ext", ["flac", "mp3", "m4a", "opus"])
def test_lyrics_are_embedded_where_players_read_them(tmp_path: Path, ext: str) -> None:
    p = tmp_path / f"song.{ext}"
    shutil.copy(AUDIO / f"silence.{ext}", p)
    assert audio.read_lyrics(p) == ""
    assert audio.write_lyrics(p, SYNCED) and audio.read_lyrics(p) == SYNCED
    assert audio.write_lyrics(p, "plain line") and audio.read_lyrics(p) == "plain line"  # replaced, not added
    assert audio.read_lyrics(tmp_path / "nothing.flac") is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
@pytest.mark.parametrize("ext", ["flac", "mp3", "m4a", "opus"])
def test_lyrics_move_no_audio_in_place(tmp_path: Path, ext: str) -> None:
    """Lyrics that fit the file's padding are written in place; ones that do not go into a copy that replaces
    the file whole (a crash mid-write leaves the old file). Either way the audio stays as it was."""
    from mutagen import File

    p = tmp_path / f"song.{ext}"
    shutil.copy(AUDIO / f"silence.{ext}", p)
    if ext != "mp3":
        File(p).save(padding=lambda info: 0)  # no room: the lyrics need a copy
    before, inode = audio_frames(p), p.stat().st_ino
    long_text = "\n".join(f"[00:{n:02d}.00] line {n}" for n in range(60))
    assert audio.write_lyrics(p, long_text) and audio.read_lyrics(p) == long_text
    assert audio_frames(p) == before and not list(tmp_path.glob(".*"))
    replaced = p.stat().st_ino
    assert ext == "mp3" or replaced != inode  # (an MP3 without ID3 is never rewritten in place: no padding)
    assert audio.write_lyrics(p, "short") and audio.read_lyrics(p) == "short"
    assert p.stat().st_ino == replaced and audio_frames(p) == before  # fits the padding left: in place


def audio_frames(p: Path) -> bytes:
    """The decoded audio, to compare."""
    cmd = ["ffmpeg", "-v", "quiet", "-i", str(p), "-map", "0:a", "-f", "s16le", "-"]
    return subprocess.run(cmd, capture_output=True, check=True).stdout


@pytest.fixture
def library(settings: Settings) -> Settings:
    """First Song as a real MP3 (filed an hour ago), scanned and matched."""
    lib = settings.library_dir
    for fake in [p for p in lib.rglob("*.*")]:  # the conftest's files are no audio
        fake.unlink()
    p = lib / "Artist A" / "Artist A - First Song.mp3"
    shutil.copy(AUDIO / "silence.mp3", p)
    os.utime(p, (time.time() - 3600, time.time() - 3600))
    rescan(settings)
    return settings


def run_job(settings: Settings) -> tuple[str, Run]:
    run = Run(BY_NAME["lyrics"], settings, Vault.from_env(settings.data_dir, {}), "manual")
    run.stop = threading.Event()
    return job.run(run), run


def rescan(settings: Settings) -> None:
    """Scan and match (the silent file is a second long: as long as its song here)."""
    con = db.connect(settings.db_path)
    catalog.scan(con, settings.library_dir)
    with con:
        con.execute("UPDATE files SET duration = 200")
    catalog.match_songs(con)
    con.close()


def test_the_lyrics_job(library: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    p = library.library_dir / "Artist A" / "Artist A - First Song.mp3"
    asked: list[str] = []

    def find(song, length: float, album: str | None = None) -> lyrics.Lyrics | None:
        asked.append(song["key"])
        return lyrics.Lyrics("synced", SYNCED, "lrclib:7")

    monkeypatch.setattr(lyrics, "find", find)
    monkeypatch.setattr(job, "QUIET", 0)
    message, _ = run_job(library)
    assert message == "1 synced" and asked == ["spotify:s1"] and audio.read_lyrics(p) == SYNCED
    assert run_job(library)[0] == "nothing to do" and len(asked) == 1  # (the rescan has not seen the change yet)
    rescan(library)
    assert run_job(library)[0] == "nothing to do" and len(asked) == 1
    con = db.connect(library.db_path)
    row = con.execute("SELECT song_key, state, source FROM lyrics").fetchone()
    assert tuple(row) == ("spotify:s1", "synced", "lrclib:7")
    con.close()
    audio.write_tags(p, title="First Song")  # a retag: the lyrics are still there, nothing asked
    os.utime(p, (time.time() - 3600, time.time() - 3600))
    rescan(library)
    assert run_job(library)[0] == "1 kept" and len(asked) == 1


def test_a_files_own_lyrics_stay_and_none_is_asked_again_later(
    library: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = library.library_dir / "Artist A" / "Artist A - First Song.mp3"
    monkeypatch.setattr(job, "QUIET", 0)
    monkeypatch.setattr(lyrics, "find", lambda song, length, album=None: None)
    assert run_job(library)[0] == "1 none on LRCLIB"
    assert run_job(library)[0] == "nothing to do"  # asked again only after a month
    con = db.connect(library.db_path)
    with con:
        con.execute("UPDATE lyrics SET checked = '2020-01-01'")
    con.close()
    assert run_job(library)[0] == "1 none on LRCLIB"
    audio.write_lyrics(p, "the uploader's lyrics")
    os.utime(p, (time.time() - 3600, time.time() - 3600))
    rescan(library)
    monkeypatch.setattr(lyrics, "find", lambda *a, **k: pytest.fail("a file with lyrics is not asked about"))
    assert run_job(library)[0] == "1 had their own" and audio.read_lyrics(p) == "the uploader's lyrics"


def test_a_new_file_waits_and_lrclib_failing_ends_the_run(library: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    p = library.library_dir / "Artist A" / "Artist A - First Song.mp3"
    os.utime(p, None)  # just filed
    rescan(library)
    monkeypatch.setattr(lyrics, "find", lambda *a, **k: pytest.fail("a file being filed waits"))
    assert run_job(library)[0] == "nothing to do"
    monkeypatch.setattr(job, "QUIET", 0)

    def down(*a, **k):
        raise lrclib.LrclibError("LRCLIB: HTTP 503")

    monkeypatch.setattr(lyrics, "find", down)
    message, run = run_job(library)
    assert message == "1 LRCLIB failed" and not run.handled  # not noted: tried again next run
    con = db.connect(library.db_path)
    assert not con.execute("SELECT 1 FROM lyrics").fetchone()
    con.close()


def test_a_band_is_credited_whole_and_the_release_album_comes_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "Above" is not "Above & Beyond" (a name a band starts with); of two fitting entries of a title the one
    on the song's own album wins over a synced one of another album (an artist's two "Intro"s)."""
    band = SONG | {"artist": "Above & Beyond", "artists": '["Above & Beyond"]'}
    assert lyrics.credited(band, "Above & Beyond feat. Zoë Johnston") and not lyrics.credited(band, "Above")
    assert lyrics.credited({"artist": "A & B", "artists": None}, "A, B")  # one string naming both: both credited
    other = entry(2, albumName="The Bends", syncedLyrics="[00:10.00] another intro")
    own = entry(3, plainLyrics="the album's intro")
    monkeypatch.setattr(lrclib, "get", lambda *a: None)
    monkeypatch.setattr(lrclib, "search", lambda artist, title: [other, own])
    found = lyrics.find(SONG, 239.0)
    assert found and found.source == "lrclib:3"


def test_close_matches_and_files_to_confirm_get_no_lyrics(library: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file taken as a close match is another version: the song's names would find the wrong lyrics. A file
    filed as probable waits until Review confirms it is the song."""
    monkeypatch.setattr(job, "QUIET", 0)
    monkeypatch.setattr(lyrics, "find", lambda *a, **k: pytest.fail("not asked"))
    con = db.connect(library.db_path)
    with con:
        con.execute("UPDATE songs SET close_match = 1 WHERE file IS NOT NULL")
    con.close()
    assert run_job(library)[0] == "nothing to do"
    con = db.connect(library.db_path)
    with con:
        con.execute("UPDATE songs SET close_match = 0")
        path = con.execute("SELECT file FROM songs WHERE file IS NOT NULL").fetchone()[0]
        con.execute(
            "INSERT INTO events (ts, action, path, song, matched) VALUES ('2026-11-26T10:00:00', 'new', ?, "
            "'spotify:s1', 'probable')",
            (path,),
        )
    con.close()
    assert run_job(library)[0] == "nothing to do"
    con = db.connect(library.db_path)
    with con:
        con.execute("DELETE FROM events")
    con.close()
    monkeypatch.setattr(lyrics, "find", lambda *a, **k: None)
    assert run_job(library)[0] == "1 none on LRCLIB"  # confirmed: asked


def test_an_unwritable_file_is_noted_and_a_replaced_one_waits(
    library: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file whose lyrics cannot be written is not asked about at every run; one that filing replaced while
    LRCLIB was asked gets nothing (the lyrics were for the old one's length)."""
    p = library.library_dir / "Artist A" / "Artist A - First Song.mp3"
    monkeypatch.setattr(job, "QUIET", 0)
    asked: list[str] = []

    def find(song, length, album=None):
        asked.append(song["key"])
        return lyrics.Lyrics("synced", SYNCED, "lrclib:7")

    monkeypatch.setattr(lyrics, "find", find)
    monkeypatch.setattr(audio, "write_lyrics", lambda path, text: False)
    assert run_job(library)[0] == "1 unreadable"
    assert run_job(library)[0] == "nothing to do" and len(asked) == 1
    con = db.connect(library.db_path)
    with con:
        con.execute("DELETE FROM lyrics")
    con.close()

    def replaced(song, length, album=None):
        asked.append(song["key"])
        p.write_bytes(p.read_bytes() + b"\0")  # filing put another file there meanwhile
        return lyrics.Lyrics("synced", SYNCED, "lrclib:7")

    monkeypatch.setattr(lyrics, "find", replaced)
    monkeypatch.setattr(audio, "write_lyrics", lambda path, text: pytest.fail("not written"))
    assert run_job(library)[0] == "nothing to do" and len(asked) == 2


def test_a_cut_connection_is_lrclib_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    import http.client

    def cut(*a, **k):
        raise http.client.IncompleteRead(b"")

    monkeypatch.setattr(lrclib.urllib.request, "urlopen", cut)
    monkeypatch.setattr(lrclib, "SPACING", 0)
    with pytest.raises(lrclib.LrclibError):
        lrclib.search("Radiohead", "Creep")
