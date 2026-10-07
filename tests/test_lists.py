"""Reading the lists at their source (lists.py) and writing the playlists (playlists.py)."""

import json
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

    def __init__(self, con, vault, user_id=None) -> None:
        pass

    def items(self, pid: str | None) -> list[dict]:
        FakeSpotify.calls.append(f"items {pid}")
        return FakeSpotify.liked if pid is None else [song("s1", "Artist A", "First Song")]

    def playlist(self, pid: str) -> dict:
        FakeSpotify.calls.append(f"playlist {pid}")
        return {"name": f"Name {pid}", "image": f"https://img/{pid}", "snapshot": "snap1", "owner": "Timon"}

    def snapshots(self) -> dict[str, str]:  # BBB222 is not in the account's library: asked on its own
        return {"AAA111": "snap1"}

    def likes_state(self) -> str:
        return f"{len(FakeSpotify.liked)}:{FakeSpotify.liked[0]['id'] if FakeSpotify.liked else ''}"

    def unplayable_liked(self) -> set[str]:
        return {"s9"}

    english: ClassVar[dict[str, str]] = {}  # track id -> its first artist in English (else the same name)

    def track(self, track_id: str, lang: str = "") -> dict:
        FakeSpotify.calls.append(f"track {track_id} {lang}")
        name = next(s["artist"] for s in FakeSpotify.liked if s["id"] == track_id)
        return {"artists": [{"name": FakeSpotify.english.get(track_id, name) if lang == "en" else name}]}


@pytest.fixture
def run(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Run:
    FakeSpotify.calls = []
    monkeypatch.setattr(spotify, "Spotify", FakeSpotify)
    r = Run(BY_NAME["sync"], settings, Vault.from_env(settings.data_dir, {}), "manual")
    r.stop = threading.Event()
    return r


def test_two_users_lists_are_read_once(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    """A list two users follow is read once; a follower not connected to Spotify does not keep it from
    being read (the other's login reads it), and a public playlist only unconnected users follow is read with
    Echolot's own app (no user)."""
    from echolot.settings import auth, sources

    logins: list[int | None] = []

    class Unconnected(FakeSpotify):
        def __init__(self, con, vault, user_id=None) -> None:
            if user_id == 2:
                raise spotify.SpotifyError("Spotify is not connected.")
            logins.append(user_id)

    monkeypatch.setattr(spotify, "Spotify", Unconnected)
    con = run.connect()
    timon = auth.logged_in(con, "timon", "nd-timon", False).id
    sources.add_list(con, timon, "https://open.spotify.com/playlist/AAA111")
    sources.add_list(con, timon, "https://open.spotify.com/playlist/ONLY2")
    con.close()
    message = lists.fetch_spotify(run)
    assert FakeSpotify.calls.count("items AAA111") == 1 and FakeSpotify.calls.count("items ONLY2") == 1
    assert message == "4 lists, 4 changed" and logins[-1] is None  # ONLY2: the app's


def test_fetch_spotify(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    assert lists.fetch_spotify(run) == "3 lists, 3 changed"
    con = run.connect()
    sql = "SELECT song_key FROM list_songs WHERE list_key = 'spotify:likes:1' ORDER BY position"
    likes = [r[0] for r in con.execute(sql)]
    assert likes == ["spotify:s1", "spotify:s9", "spotify:s3"]  # s3 keeps its known name
    s3 = con.execute("SELECT artist, title, unavailable FROM songs WHERE key = 'spotify:s3'").fetchone()
    assert tuple(s3) == ("Artist C", "Gone Song", "greyed out on Spotify")  # the availability check's to change
    s9 = con.execute("SELECT unavailable, isrc FROM songs WHERE key = 'spotify:s9'").fetchone()
    assert tuple(s9) == (None, "ISRCs9")  # new: whether it plays, the availability check tells
    assert con.execute("SELECT count(*) FROM wanted WHERE key = 'spotify:s2'").fetchone()[0] == 0  # left
    assert con.execute("SELECT count(*) FROM songs WHERE key = 'spotify:s2'").fetchone()[0] == 1  # kept
    row = con.execute("SELECT title, cover_url, snapshot FROM lists WHERE key = 'spotify:playlist:BBB222'").fetchone()
    assert tuple(row) == ("Renamed", "https://img/BBB222", "snap1")  # the name override wins
    creator = "SELECT creator FROM lists WHERE key = 'spotify:playlist:BBB222'"
    assert con.execute(creator).fetchone()[0] == "Timon"  # who made it, for its playlist's comment
    con.close()
    FakeSpotify.calls = []
    assert lists.fetch_spotify(run) == "3 lists, 0 changed"  # nothing changed: nothing read
    assert FakeSpotify.calls == ["playlist BBB222"]  # only its snapshot (not in the account's library)
    FakeSpotify.calls = []
    monkeypatch.setattr(FakeSpotify, "liked", [song("s10", "Artist N", "Newest Song"), *FakeSpotify.liked])  # a like
    assert lists.fetch_spotify(run) == "3 lists, 1 changed"
    assert FakeSpotify.calls == ["items None", "playlist BBB222"]


def test_an_artist_in_another_script_gets_its_english_name(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spotify writes 祖堅 正慶 as Masayoshi Soken only when asked in English: asked once per such song (not for
    Latin names; one with no other English name is marked as asked); renamed, it is asked again."""
    soken, other = song("k1", "祖堅 正慶", "Dynamis"), song("k2", "川子", "Song K")
    monkeypatch.setattr(FakeSpotify, "liked", [*FakeSpotify.liked[:2], soken, other])
    monkeypatch.setattr(FakeSpotify, "english", {"k1": "Masayoshi Soken"})
    lists.fetch_spotify(run)
    con = run.connect()
    aliases = dict(con.execute("SELECT key, artist_alias FROM songs WHERE key LIKE 'spotify:%'").fetchall())
    assert [aliases[k] for k in ("spotify:k1", "spotify:k2", "spotify:s9")] == ["Masayoshi Soken", "", None]
    asked = [c for c in FakeSpotify.calls if c.startswith("track")]
    assert sorted(asked) == ["track k1 en", "track k2 en"]
    with con:
        lists._song(con, "spotify:k1", "spotify", artist="Masayoshi Soken", title="Dynamis")
        lists._song(con, "spotify:k2", "spotify", artist="川子", title="Song K")
    assert con.execute("SELECT artist_alias FROM songs WHERE key = 'spotify:k1'").fetchone()[0] is None  # renamed
    assert con.execute("SELECT artist_alias FROM songs WHERE key = 'spotify:k2'").fetchone()[0] == ""  # the same
    con.close()


def test_a_name_spotify_blanks_is_kept_or_asked_in_english(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spotify names Timati "." (without a language; "Timati" in English): a new song gets the English names,
    a list read later giving "." again keeps them."""
    timati = {**song("t1", ".", "Groove On"), "artists": [".", "Snoop Dogg"]}
    monkeypatch.setattr(FakeSpotify, "liked", [*FakeSpotify.liked[:2], timati])
    monkeypatch.setattr(FakeSpotify, "english", {"t1": "Timati"})

    def track(self, tid: str, lang: str = "") -> dict:
        return {"artists": [{"name": "Timati" if lang == "en" else "."}, {"name": "Snoop Dogg"}]}

    monkeypatch.setattr(FakeSpotify, "track", track)
    lists.fetch_spotify(run)
    con = run.connect()
    row = con.execute("SELECT artist, artists FROM songs WHERE key = 'spotify:t1'").fetchone()
    assert (row[0], json.loads(row[1])) == ("Timati", ["Timati", "Snoop Dogg"])
    with con:
        lists._song(
            con, "spotify:t1", "spotify", artist=".", artists=json.dumps([".", "Snoop Dogg"]), title="Groove On"
        )
    assert con.execute("SELECT artist FROM songs WHERE key = 'spotify:t1'").fetchone()[0] == "Timati"
    con.close()


def test_empty_listing_keeps_the_last(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    lists.fetch_spotify(run)
    monkeypatch.setattr(FakeSpotify, "liked", [])  # e.g. a playlist Spotify no longer hands out
    assert "failed: spotify:likes:1: no songs listed" in lists.fetch_spotify(run)
    con = run.connect()
    n = con.execute("SELECT count(*) FROM list_songs WHERE list_key = 'spotify:likes:1'").fetchone()[0]
    assert n == 3
    con.close()


def test_playlists(run: Run) -> None:
    lists.fetch_spotify(run)
    folder = run.paths.playlists
    folder.mkdir()
    (folder / "spotify-GONE.m3u").write_text("#EXTM3U\n")  # looks like ours, but Echolot did not write it
    con = run.connect()
    playlists.write(con, folder)
    mine = folder / "owner"  # the owner's folder
    likes = (mine / "Spotify Liked Songs.m3u").read_text().splitlines()
    assert likes == ["#EXTM3U", "#PLAYLIST:Liked Songs", "../../tracks/Artist A/Artist A - First Song.mp3"]
    assert not (mine / "spotify-BBB222.m3u").exists()  # playlist: false
    # a list that is no longer followed loses its playlist file; a file Echolot did not write stays
    from echolot.settings import sources

    sources.remove_list(con, 1, "spotify:playlist:AAA111")
    lists.sync_table(con)
    assert (mine / "spotify-AAA111.m3u").exists()
    playlists.write(con, folder)
    assert not (mine / "spotify-AAA111.m3u").exists() and (folder / "spotify-GONE.m3u").exists()
    # with no list at all nothing is deleted
    for s in sources.lists(con):
        if s.name in sources.LIKES:
            sources.set_likes(con, 1, s.service, False)
        else:
            sources.remove_list(con, 1, s.key)
    lists.sync_table(con)
    playlists.write(con, folder)
    assert (mine / "Spotify Liked Songs.m3u").exists()
    con.close()


class FakeYtDlp:
    asked: ClassVar[list[str]] = []  # meta() calls
    fetched: ClassVar[list[str]] = []  # the tracks download() was given

    def __init__(self, private: Path, token: str | None = None) -> None:
        pass

    def listing(self, url: str, stop) -> tuple:
        if url.endswith("/likes"):  # 1001 by API address, as a set lists all but its first tracks
            tracks = [("1001", "https://api-v2.soundcloud.com/tracks/1001"), ("2002", "https://sc/2002")]
            return [*tracks, ("3003", "https://sc/3003")], {}
        return [("2002", "https://sc/2002")], {"title": "Trance", "thumbnails": [{"url": "https://i/x-large.jpg"}]}

    def download(self, tracks: list, folder: Path, stop) -> list[dict]:
        out = []
        FakeYtDlp.fetched += [tid for tid, _ in tracks]
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
    monkeypatch.setattr(audio, "prepare", lambda p, keep_hires=False: audio.Prepared(p, False, None))
    monkeypatch.setattr("echolot.jobs.acquire.pictures", lambda *a, **k: None)
    message = lists.soundcloud(run)
    assert message == "2 lists, 2 changed; 1 new songs"
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
    assert lists.soundcloud(run) == "2 lists, 2 changed; 0 new songs"  # nothing downloaded twice
    library(run)
    asked = len(FakeYtDlp.asked)  # the pages of the songs stored in the first run
    lists.soundcloud(run)
    assert len(FakeYtDlp.asked) == asked  # no page asked for twice
    con = run.connect()  # a download that left the library (e.g. retired in review) is fetched again
    (run.paths.tracks / con.execute("SELECT file FROM songs WHERE key = 'soundcloud:2002'").fetchone()[0]).unlink()
    con.close()
    library(run)
    assert lists.soundcloud(run) == "2 lists, 2 changed; 1 new songs"


def test_a_soundcloud_song_the_library_has_by_its_page_is_not_downloaded(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A library file whose source tag holds exactly the track's page is that song (Echolot filed it so, here or in
    the Echolot it was copied from): linked to it, not downloaded. The others are downloaded as before."""
    from mutagen.flac import FLAC

    monkeypatch.setattr(ytdlp, "YtDlp", FakeYtDlp)
    monkeypatch.setattr(FakeYtDlp, "fetched", [])
    monkeypatch.setattr(audio, "prepare", lambda p, keep_hires=False: audio.Prepared(p, False, None))
    monkeypatch.setattr("echolot.jobs.acquire.pictures", lambda *a, **k: None)
    copy = run.paths.tracks / "DJ Nobody" / "DJ Nobody - Night Drive.flac"
    copy.parent.mkdir(parents=True)
    copy.write_bytes((Path(__file__).parent / "fixtures" / "audio" / "silence.flac").read_bytes())
    tags = FLAC(copy)
    tags.update({"artist": "DJ Nobody", "title": "Night Drive", "source": "https://sc/2002/"})
    tags.save()
    second = run.paths.tracks / "Uploader" / "Uploader - Set Track.flac"  # a set lists it by API address
    second.parent.mkdir(parents=True, exist_ok=True)
    second.write_bytes(copy.read_bytes())
    tags = FLAC(second)
    tags.update({"artist": "Uploader", "title": "Set Track", "source": "https://soundcloud.com/uploader/4004"})
    tags.save()
    listing = FakeYtDlp.listing

    def with_set_track(self, url: str, stop) -> tuple:
        tracks, info = listing(self, url, stop)
        return [*tracks, ("4004", "https://api-v2.soundcloud.com/tracks/4004")], info

    monkeypatch.setattr(FakeYtDlp, "listing", with_set_track)
    library(run)
    message = lists.soundcloud(run)
    assert message.endswith("; 0 new songs, 2 linked to the library's file of their page")
    assert "2002" not in FakeYtDlp.fetched and "4004" not in FakeYtDlp.fetched
    library(run)
    con = run.connect()
    song = con.execute("SELECT artist, title, file, archived FROM songs WHERE key = 'soundcloud:2002'").fetchone()
    con.close()
    assert tuple(song) == ("DJ Nobody", "Night Drive", "DJ Nobody/DJ Nobody - Night Drive.flac", 1)
    assert lists.soundcloud(run).endswith("; 0 new songs")  # known now: nothing linked or downloaded again


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


def test_a_soundcloud_download_the_library_has_is_linked(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    """Night Drive is in the library under another artist name and sounds the same: the SoundCloud song is
    linked to that file, nothing is filed twice."""
    monkeypatch.setattr(ytdlp, "YtDlp", FakeYtDlp)
    monkeypatch.setattr(audio, "prepare", lambda p, keep_hires=False: audio.Prepared(p, False, None))
    monkeypatch.setattr("echolot.jobs.acquire.pictures", lambda *a, **k: None)
    monkeypatch.setattr("echolot.library.identity.alike", lambda a, b: 0.95)
    there = run.paths.tracks / "Night Rider" / "Night Rider - Night Drive (Original Mix).wav"
    there.parent.mkdir(parents=True)
    with wave.open(str(there), "wb") as w:
        w.setnchannels(1), w.setsampwidth(1), w.setframerate(100)
        w.writeframes(b"\x80" * 100 * 251)
    library(run)
    assert lists.soundcloud(run) == "2 lists, 2 changed; 0 new songs"
    library(run)
    con = run.connect()
    assert con.execute("SELECT file FROM songs WHERE key = 'soundcloud:2002'").fetchone()[0] == (
        "Night Rider/Night Rider - Night Drive (Original Mix).wav"
    )
    assert not (run.paths.tracks / "DJ Nobody").exists()
    con.close()


def test_a_soundcloud_list_is_read_when_it_changed_or_hourly(run: Run) -> None:
    from datetime import datetime, timedelta

    from echolot.settings import sources

    con = run.connect()
    likes = next(s for s in sources.lists(con) if s.key == "soundcloud:someone/likes")
    states = {likes.url: "218:2026-10-01T20:32:39Z:42"}
    assert lists._sc_changed(con, likes, states)  # never read with this state
    recently = (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds")
    with con:
        con.execute(
            "UPDATE lists SET snapshot = ?, fetched = 1, fetched_at = ? WHERE key = ?",
            (states[likes.url], recently, likes.key),
        )
    assert not lists._sc_changed(con, likes, states)  # unchanged, read 10 min ago
    assert lists._sc_changed(con, likes, {likes.url: "219:2026-10-02T09:00:00Z:43"})  # a new like
    assert lists._sc_changed(con, likes, {})  # the state could not be asked
    with con:
        con.execute(
            "UPDATE lists SET fetched_at = ? WHERE key = ?",
            ((datetime.now() - timedelta(hours=2)).isoformat(), likes.key),
        )
    assert lists._sc_changed(con, likes, states)  # read again at least hourly
    con.close()


def test_each_users_playlists(run: Run) -> None:
    """A list two users follow as a playlist is two files, one in each user's folder; the playlists of
    before users had folders go (remembered for Navidrome), a file Echolot did not write stays."""
    from echolot import db
    from echolot.settings import auth, sources

    lists.fetch_spotify(run)
    folder = run.paths.playlists
    folder.mkdir()
    con = run.connect()
    for old in ("Spotify Liked Songs.m3u", "spotify-AAA111.m3u", "spotify-AAA111.jpg"):  # before: one folder
        (folder / old).write_text("old")
    (folder / "My own.m3u").write_text("#EXTM3U\n")
    with con:
        db.set_meta(con, "playlist_files", '["Spotify Liked Songs.m3u", "spotify-AAA111.m3u", "spotify-AAA111.jpg"]')
    timon = auth.logged_in(con, "timon", "nd-timon", False).id
    sources.add_list(con, timon, "https://open.spotify.com/playlist/AAA111")
    lists.sync_table(con)
    playlists.write(con, folder)
    ours, theirs = (folder / "owner" / "spotify-AAA111.m3u", folder / "timon" / "spotify-AAA111.m3u")
    assert ours.read_text() == theirs.read_text()
    assert not (folder / "Spotify Liked Songs.m3u").exists() and not (folder / "spotify-AAA111.jpg").exists()
    assert (folder / "My own.m3u").exists()
    gone = db.get_meta(con, "playlists_gone")
    assert gone == '["Spotify Liked Songs.m3u", "spotify-AAA111.m3u"]'
    con.close()


def test_navidrome_gets_each_playlist_to_its_user(run: Run) -> None:
    """Navidrome imports a file as its first admin's: Echolot gives it to its user; the playlist of a
    file Echolot removed is deleted; one made in Navidrome (no file) or not Echolot's is never touched."""
    from echolot import db

    lists.fetch_spotify(run)
    folder = run.paths.playlists
    con = run.connect()
    playlists.write(con, folder)
    with con:
        db.set_meta(con, "playlists_gone", '["Spotify Liked Songs.m3u"]')  # moved into the owner's folder
    written = json.loads(db.get_meta(con, "playlist_files"))
    listed = ("owner/Spotify Liked Songs.m3u", "owner/spotify-AAA111.m3u")
    others = [r for r in written if r.endswith(".m3u") and r not in listed]

    class Navidrome:
        calls: ClassVar[list[tuple]] = []
        comments: ClassVar[dict[str, str]] = {}

        def playlists(self) -> list[dict]:
            p = "/music/playlists/"
            imported = [{"id": f"i{n}", "path": p + r, "ownerId": "nd-owner"} for n, r in enumerate(others)]
            return [
                {"id": "a", "path": p + "owner/Spotify Liked Songs.m3u", "ownerId": "nd-admin"},  # new: the admin's
                {"id": "b", "path": p + "owner/spotify-AAA111.m3u", "ownerId": "nd-owner"},  # theirs already
                {"id": "c", "path": p + "Spotify Liked Songs.m3u", "ownerId": "nd-owner"},  # the old file's
                {"id": "d", "path": "", "ownerId": "nd-admin"},  # made in Navidrome
                {"id": "e", "path": "/music/tracks/x/spotify-AAA111.m3u", "ownerId": "nd-admin"},  # not Echolot's
                *imported,  # the owner's other playlists, theirs already
            ]

        def update_playlist(self, pid: str, fields: dict) -> None:
            if "ownerId" in fields:
                Navidrome.calls.append(("owner", pid, fields["ownerId"]))
            if "comment" in fields:
                Navidrome.comments[pid] = fields["comment"]

        def delete_playlist(self, pid: str) -> None:
            Navidrome.calls.append(("delete", pid))

    message = playlists.sync_owners(con, Navidrome(), folder)
    assert Navidrome.calls == [("owner", "a", "nd-owner"), ("delete", "c")]
    n = len(Navidrome.comments)
    assert message == f"1 playlists given to their users, {n} playlist comments set, 1 old playlists deleted"
    # where each list comes from, in place of Navidrome's "Auto-imported from '<file>'" (Feishin links it)
    assert Navidrome.comments["a"] == "Auto-imported from Spotify, by owner: https://open.spotify.com/collection/tracks"
    assert (
        Navidrome.comments["b"].startswith("Auto-imported from Spotify")
        and "open.spotify.com/playlist/AAA111" in Navidrome.comments["b"]
    )
    assert db.get_meta(con, "playlists_gone") == "[]"
    con.close()


def test_old_playlists_stay_until_the_new_ones_are_imported(run: Run) -> None:
    """Moved files: Navidrome imports them a moment later; until then the old playlists stay, so their
    user has their playlists all the time."""
    from echolot import db

    lists.fetch_spotify(run)
    folder = run.paths.playlists
    con = run.connect()
    playlists.write(con, folder)
    with con:
        db.set_meta(con, "playlists_gone", '["Spotify Liked Songs.m3u"]')
    deleted = []

    class Navidrome:  # the new files not imported yet: only the old playlist
        def playlists(self) -> list[dict]:
            return [{"id": "c", "path": "/music/playlists/Spotify Liked Songs.m3u", "ownerId": "nd-owner"}]

        def delete_playlist(self, pid: str) -> None:
            deleted.append(pid)

    assert playlists.sync_owners(con, Navidrome(), folder) == "" and deleted == []
    assert db.get_meta(con, "playlists_gone") == '["Spotify Liked Songs.m3u"]'  # still to do
    con.close()
