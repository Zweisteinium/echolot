"""Reading the lists at their source (lists.py) and writing the playlists (playlists.py)."""

import threading
import wave
from pathlib import Path
from typing import ClassVar

import pytest

from echolot.config import Settings
from echolot.jobs import lists
from echolot.jobs.schedule import BY_NAME
from echolot.jobs.worker import Run
from echolot.library import audio, catalog, playlists
from echolot.services import spotify, ytdlp
from echolot.settings.vault import Vault


def song(sid: str, artist: str, title: str, length: int = 200) -> dict:
    return {"id": sid, "artist": artist, "artists": [artist], "title": title, "album": "", "length": length,
            "isrc": f"ISRC{sid}"}  # fmt: skip


class FakeSpotify:
    calls: ClassVar[list[str]] = []
    liked: ClassVar[list[dict]] = [
        song("s1", "Artist A", "First Song"),
        song("s9", "New Artist", "New Song"),
        {**song("s3", "", ""), "artists": []},
    ]  # s3: Spotify blanked its name

    def __init__(self, con, vault) -> None:
        pass

    def items(self, pid: str | None) -> list[dict]:
        FakeSpotify.calls.append(f"items {pid}")
        return FakeSpotify.liked if pid is None else [song("s1", "Artist A", "First Song")]

    def playlist(self, pid: str) -> dict:
        return {"name": f"Name {pid}", "image": f"https://img/{pid}", "snapshot": "snap1"}

    def unplayable_liked(self) -> set[str]:
        return {"s9"}


@pytest.fixture
def run(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Run:
    FakeSpotify.calls = []
    monkeypatch.setattr(spotify, "Spotify", FakeSpotify)
    r = Run(BY_NAME["sync"], settings, Vault.from_env(settings.data_dir, {}), "manual")
    r.stop = threading.Event()
    return r


def test_fetch_spotify(run: Run) -> None:
    assert lists.fetch_spotify(run) == "Spotify: 3 lists read"
    con = run.connect()
    likes = [
        r[0] for r in con.execute("SELECT song_key FROM list_songs WHERE list_key = 'spotify:likes' ORDER BY position")
    ]
    assert likes == ["spotify:s1", "spotify:s9", "spotify:s3"]  # s3 keeps its known name
    s3 = con.execute("SELECT artist, title, unavailable FROM songs WHERE key = 'spotify:s3'").fetchone()
    assert tuple(s3) == ("Artist C", "Gone Song", None)  # no longer greyed out
    s9 = con.execute("SELECT unavailable, isrc FROM songs WHERE key = 'spotify:s9'").fetchone()
    assert tuple(s9) == ("greyed out on Spotify", "ISRCs9")
    assert con.execute("SELECT count(*) FROM wanted WHERE key = 'spotify:s2'").fetchone()[0] == 0  # left
    assert con.execute("SELECT count(*) FROM songs WHERE key = 'spotify:s2'").fetchone()[0] == 1  # kept
    row = con.execute("SELECT title, cover_url, snapshot FROM lists WHERE key = 'spotify:playlist:BBB222'").fetchone()
    assert tuple(row) == ("Renamed", "https://img/BBB222", "snap1")  # the name override wins
    con.close()
    FakeSpotify.calls = []
    lists.fetch_spotify(run)  # the playlists did not change: only the likes are read again
    assert FakeSpotify.calls == ["items None"]


def test_empty_listing_keeps_the_last(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    lists.fetch_spotify(run)
    monkeypatch.setattr(FakeSpotify, "liked", [])  # e.g. a playlist Spotify no longer hands out
    assert "failed: spotify:likes: no songs listed" in lists.fetch_spotify(run)
    con = run.connect()
    n = con.execute("SELECT count(*) FROM list_songs WHERE list_key = 'spotify:likes'").fetchone()[0]
    assert n == 3
    con.close()


def test_playlists(run: Run) -> None:
    lists.fetch_spotify(run)
    folder = run.paths.playlists
    folder.mkdir()
    (folder / "spotify-GONE.m3u").write_text("#EXTM3U\n")  # looks like ours, but Echolot did not write it
    con = run.connect()
    playlists.write(con, folder)
    likes = (folder / "Spotify Liked Songs.m3u").read_text().splitlines()
    assert likes == ["#EXTM3U", "#PLAYLIST:Liked Songs", "../tracks/Artist A/Artist A - First Song.mp3"]
    removed = (folder / "Spotify Liked Songs - removed.m3u").read_text().splitlines()
    assert removed == ["#EXTM3U", "#PLAYLIST:Liked Songs – removed", "../tracks/Artist B/Artist B - Second Song.flac"]
    assert not (folder / "spotify-BBB222.m3u").exists()  # playlist: false
    # a list that is no longer followed loses its playlist file; a file Echolot did not write stays
    from echolot.settings import sources

    sources.remove_list(con, "spotify:playlist:AAA111")
    lists.sync_table(con)
    assert (folder / "spotify-AAA111.m3u").exists()
    playlists.write(con, folder)
    assert not (folder / "spotify-AAA111.m3u").exists() and (folder / "spotify-GONE.m3u").exists()
    # with no list at all nothing is deleted
    for s in sources.lists(con):
        sources.remove_list(con, s.key) if s.name not in sources.LIKES else sources.set_likes(con, s.service, False)
    lists.sync_table(con)
    playlists.write(con, folder)
    assert (folder / "Spotify Liked Songs.m3u").exists()
    con.close()


class FakeYtDlp:
    asked: ClassVar[list[str]] = []  # meta() calls

    def __init__(self, private: Path, token: str | None = None) -> None:
        pass

    def listing(self, url: str, stop) -> tuple:
        if url.endswith("/likes"):  # 1001 by API address, as a set lists all but its first tracks
            tracks = [("1001", "https://api-v2.soundcloud.com/tracks/1001"), ("2002", "https://sc/2002")]
            return [*tracks, ("3003", "https://sc/3003")], {}
        return [("2002", "https://sc/2002")], {"title": "Trance", "thumbnails": [{"url": "https://i/x-large.jpg"}]}

    def download(self, tracks: list, folder: Path, stop) -> list[dict]:
        out = []
        for tid, _ in tracks:
            if tid == "2002":
                path = folder / "2002.wav"
                folder.mkdir(parents=True, exist_ok=True)
                with wave.open(str(path), "wb") as w:
                    w.setnchannels(1), w.setsampwidth(1), w.setframerate(100)
                    w.writeframes(b"\x80" * 100 * 250)
                out.append({"id": "2002", "uploader": "someone", "artist": "NA", "title": "DJ Nobody - Night Drive",
                            "duration": "250.0", "path": str(path)})  # fmt: skip
        return out

    def meta(self, url: str, stop) -> dict:
        FakeYtDlp.asked.append(url)
        page = "https://soundcloud.com/uploader/" + url.rsplit("/", 1)[-1]
        locked = {"formats": [], "uploader": "Label", "title": "Big Label - Locked Two", "duration": 222}
        return {**locked, "webpage_url": page}


def test_soundcloud(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ytdlp, "YtDlp", FakeYtDlp)
    monkeypatch.setattr(audio, "prepare", lambda p: audio.Prepared(p, False, None))
    monkeypatch.setattr("echolot.jobs.acquire.pictures", lambda *a, **k: None)
    message = lists.soundcloud(run)
    assert message == "SoundCloud: 2 of 2 lists read, 1 new files"
    con = run.connect()
    new = con.execute("SELECT artist, title, archived, stem FROM songs WHERE key = 'soundcloud:2002'").fetchone()
    assert tuple(new) == ("DJ Nobody", "Night Drive", 1, "DJ Nobody/DJ Nobody - Night Drive")
    locked = con.execute("SELECT artist, unavailable FROM songs WHERE key = 'soundcloud:3003'").fetchone()
    assert tuple(locked) == ("Big Label", "not downloadable from SoundCloud")  # left to the fallback
    order = [r[0] for r in con.execute("SELECT song_key FROM list_songs WHERE list_key = 'soundcloud:someone/likes' "
                                       "ORDER BY position")]  # fmt: skip
    assert order == ["soundcloud:1001", "soundcloud:2002", "soundcloud:3003"]
    old = con.execute("SELECT url FROM songs WHERE key = 'soundcloud:1001'").fetchone()[0]
    assert old == "https://soundcloud.com/uploader/1001"  # the page of a song downloaded before, asked for once
    assert (
        con.execute("SELECT cover_url FROM lists WHERE key = 'soundcloud:someone/sets/trance'").fetchone()[0]
        == "https://i/x-t500x500.jpg"
    )
    con.close()
    library(run)
    assert lists.soundcloud(run) == "SoundCloud: 2 of 2 lists read, 0 new files"  # nothing downloaded twice
    library(run)
    asked = len(FakeYtDlp.asked)  # the pages of the songs stored in the first run
    lists.soundcloud(run)
    assert len(FakeYtDlp.asked) == asked  # no page asked for twice
    con = run.connect()  # a download that left the library (e.g. retired in review) is fetched again
    (run.paths.tracks / con.execute("SELECT file FROM songs WHERE key = 'soundcloud:2002'").fetchone()[0]).unlink()
    con.close()
    library(run)
    assert lists.soundcloud(run) == "SoundCloud: 2 of 2 lists read, 1 new files"


def library(run: Run) -> None:
    """What the library job does after each SoundCloud run: rescan, match the songs to files."""
    con = run.connect()
    catalog.refresh(con, run.paths.tracks)
    con.close()


def test_a_soundcloud_title_naming_a_known_artist_is_turned_round(run: Run) -> None:
    """'Song - Artist' from an uploader who is neither: the side that is an artist of the Spotify songs is the
    artist (else a duplicate of the Spotify song, filed under the song's title as artist)."""
    con = run.connect()
    assert lists._names(con, "user-1", "NA", "First Song (Hardstyle) - Artist A") == (
        "Artist A",
        "First Song (Hardstyle)",
    )
    assert lists._names(con, "user-1", "NA", "Artist A - First Song") == ("Artist A", "First Song")
    assert lists._names(con, "someone", "NA", "DJ Nobody - Night Drive") == ("DJ Nobody", "Night Drive")
    con.close()
