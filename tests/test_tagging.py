"""A library file's tags from its song (tagging.py), on real files of every format the library holds."""

import shutil
import sqlite3
from pathlib import Path

import pytest
from mutagen import File

from echolot import db
from echolot.library import tagging
from echolot.library.filing import Paths
from echolot.library.tagging import Tags

AUDIO = Path(__file__).parent / "fixtures" / "audio"
FORMATS = ["flac", "mp3", "m4a", "opus", "wav"]
SPOTIFY = ["https://open.spotify.com/track/0Evl2AXlWFuAnDxryIIuYG"]
ARTISTS, TITLE = ["Hardwell", "Azteck", "Alex Hepburn"], "Anybody Out There"
TAGS = Tags(ARTISTS, "Hardwell", TITLE, TITLE, SPOTIFY, "Soulseek")


@pytest.fixture
def env(tmp_path: Path):
    paths = Paths(tmp_path / "music")
    paths.tracks.mkdir(parents=True)
    db.init(tmp_path / "echolot.db")
    con = db.connect(tmp_path / "echolot.db")
    yield con, paths
    con.close()


def copy(paths: Paths, ext: str, rel: str) -> Path:
    dest = paths.tracks / f"{rel}.{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(AUDIO / f"silence.{ext}", dest)
    return dest


def add_song(con: sqlite3.Connection, key: str, artist: str, title: str, **more: object) -> None:
    columns = {"key": key, "service": key.split(":")[0], "artist": artist, "title": title, "length": 1} | more
    sql = f"INSERT INTO songs ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"
    with con:
        con.execute(sql, list(columns.values()))


@pytest.mark.parametrize("ext", FORMATS)
def test_written_tags_read_back_and_the_comment_stays(env, ext: str) -> None:
    _, paths = env
    p = copy(paths, ext, "Hardwell/Hardwell - Anybody Out There")
    if ext in ("flac", "opus", "m4a"):  # a DJ's note in the comment
        m = File(p)
        m.tags["\xa9cmt" if ext == "m4a" else "comment"] = ["3A - 160"]
        m.save()
    stale = {"mp3": "TSO2", "wav": "TSO2", "m4a": "soaa"}.get(ext, "album_artist")  # the uploader's album artist
    m = File(p)
    if m.tags is None:
        m.add_tags()
    if ext in ("mp3", "wav"):
        from mutagen.id3 import TSO2

        m.tags.add(TSO2(encoding=3, text=["Hardwell, Azteck & Hepburn, Alex"]))
    else:
        m.tags[stale] = ["Hardwell, Azteck & Hepburn, Alex"]
    m.save()
    assert tagging.read(p)["stale"] == {stale: ["Hardwell, Azteck & Hepburn, Alex"]}
    assert "stale" in tagging.differs(p, TAGS)
    tagging.write(p, TAGS)
    now = tagging.read(p)
    assert now["stale"] == {}  # what players would read beside the album artist is gone
    assert now["artists"] == TAGS.artists and now["albumartist"] == "Hardwell" and now["title"] == TAGS.title
    assert now["album"] == TAGS.album and now["sources"] == TAGS.sources and now["download"] == "Soulseek"
    assert tagging.differs(p, TAGS) == []  # a second write changes nothing
    if ext in ("flac", "opus", "m4a"):
        assert "3A - 160" in now["text"]  # the comment is not touched


def test_the_tags_come_from_the_song(env) -> None:
    con, paths = env
    rel, artists = "Hardwell/Hardwell - Anybody Out There.flac", '["Hardwell", "Azteck", "Alex Hepburn"]'
    add_song(con, "spotify:hw", "Hardwell", "Anybody Out There", album="Anybody Out There", artists=artists, file=rel)
    sc = {"url": "https://soundcloud.com/hw/aot", "file": rel}
    add_song(con, "soundcloud:1", "Hardwell", "Anybody Out There (Free DL) [HW001]", **sc)
    p = copy(paths, "flac", "Hardwell/Hardwell - Anybody Out There")
    tags = tagging.for_file(con, "Hardwell/Hardwell - Anybody Out There.flac", p)
    assert tags.artists == ["Hardwell", "Azteck", "Alex Hepburn"] and tags.album == "Anybody Out There"
    assert tags.sources == ["https://open.spotify.com/track/hw", "https://soundcloud.com/hw/aot"]
    assert tags.download == "Soulseek"  # a FLAC: nothing recorded, but only Soulseek delivered FLACs


def test_a_soundcloud_title_loses_its_decoration_and_a_close_match_keeps_its_name(env) -> None:
    con, paths = env
    sc = {"file": "ANNIE/ANNIE - 10 out 10.m4a", "url": "https://soundcloud.com/annie/10-out-10"}
    add_song(con, "soundcloud:2", "ANNIE", "ANNIE - 10 out 10 [ARONAVA08]", **sc)
    p = copy(paths, "m4a", "ANNIE/ANNIE - 10 out 10")
    tags = tagging.for_file(con, "ANNIE/ANNIE - 10 out 10.m4a", p)
    assert (tags.title, tags.album) == ("10 out 10", None)  # SoundCloud: the file keeps its album
    close = {
        "file": "T78/T78 - Megator (Original Mix).mp3",
        "close_match": 1,
        "link": '["T78", "Megator (Original Mix)", 419]',
    }
    add_song(con, "spotify:t78", "T78", "Megator", **close)
    p = copy(paths, "mp3", "T78/T78 - Megator (Original Mix)")
    tags = tagging.for_file(con, "T78/T78 - Megator (Original Mix).mp3", p)
    assert (tags.artists, tags.title) == (["T78"], "Megator (Original Mix)")


def test_where_a_download_came_from(env) -> None:
    con, paths = env
    p = copy(paths, "opus", "A/A - Song")
    m = File(p)
    m.tags["synopsis"] = ["Out now!\nhttps://soundcloud.com/label"]  # the video's description: not the page
    m.save()
    assert tagging.download_of(con, "A/A - Song.opus", p, []) == ""  # nothing tells
    m.tags["comment"] = ["YouTube wOIcV_r7TmU"]  # an older pipeline's note
    m.save()
    assert tagging.download_of(con, "A/A - Song.opus", p, []) == "https://www.youtube.com/watch?v=wOIcV_r7TmU"
    m.tags["comment"] = ["https://music.youtube.com/watch?v=wOIcV_r7TmU"]  # spotDL's
    m.save()
    assert tagging.download_of(con, "A/A - Song.opus", p, []) == "https://music.youtube.com/watch?v=wOIcV_r7TmU"
    del m.tags["comment"]
    m.tags["purl"] = ["https://www.youtube.com/watch?v=wOIcV_r7TmU"]  # what yt-dlp leaves
    m.save()
    assert tagging.download_of(con, "A/A - Song.opus", p, []) == "https://www.youtube.com/watch?v=wOIcV_r7TmU"
    with con:
        sql = "INSERT INTO events (ts, action, path, source, url) VALUES ('2026-10-01T10:00:00', 'new', ?, ?, ?)"
        con.execute(sql, ("A/A - Song.opus", "youtube", "https://www.youtube.com/watch?v=recorded"))
    assert tagging.download_of(con, "A/A - Song.opus", p, []) == "https://www.youtube.com/watch?v=recorded"
    f = copy(paths, "flac", "B/B - Song")  # a page in SOURCE is where the song is, not where the file came from
    tagging.write(f, Tags(["B"], "B", "Song", None, ["https://soundcloud.com/b/song"], "Soulseek"))
    assert tagging.download_of(con, "B/B - Song.flac", f, []) == "Soulseek"
    m = File(copy(paths, "m4a", "C/C - Song"))
    m.tags["\xa9cmt"] = ["https://soundcloud.com/c/song"]  # yt-dlp's page, the song's own: still the download
    m.save()
    tagging.write(Path(m.filename), Tags(["C"], "C", "Song", None, ["https://soundcloud.com/c/song"]))
    assert tagging.download_of(con, "C/C - Song.m4a", Path(m.filename), []) == "https://soundcloud.com/c/song"


@pytest.mark.parametrize(
    ("tags", "song", "conflict"),
    [
        ("I Want It", "Come & Go (with Marshmello)", True),
        ("They Can't Take That Away From Me", "Body", True),
        ("Come & Go", "Come & Go (with Marshmello)", False),
        ("Juice WRLD - Come & Go (Official Video)", "Come & Go (with Marshmello)", False),
        ("Megator (Original Mix)", "Megator", False),
        ("Francium - Original Mix", "Francium - Original Mix", False),
        ("Started From the Bottom - Drake", "Started From the Bottom", False),
        ("Oots (Original Mix) 145", "Feel It", True),
        ("", "Megator", False),
    ],
)
def test_conflict(tags: str, song: str, conflict: bool) -> None:
    assert tagging.conflict(tags, song) is conflict


def test_normalize(env, tmp_path: Path) -> None:
    """A dry run writes nothing; a run backs the old tags up and writes; a second run has nothing to do; a
    file whose tags name another song is left alone; a file no song has keeps its tags."""
    con, paths = env
    add_song(con, "spotify:jw", "Juice WRLD", "Come & Go (with Marshmello)", file="JW/JW - Come & Go.flac")
    add_song(con, "spotify:hw", "Hardwell", "Anybody Out There", file="Hardwell/Hardwell - Anybody Out There.mp3")
    wrong = copy(paths, "flac", "JW/JW - Come & Go")
    tagging.write(wrong, Tags(["Juice WRLD"], "Juice WRLD", "I Want It", None))
    right = copy(paths, "mp3", "Hardwell/Hardwell - Anybody Out There")
    copy(paths, "opus", "Other/Other - Song")
    before = {p: p.read_bytes() for p in paths.tracks.rglob("*.*")}
    report = tagging.normalize(con, paths.tracks, dry_run=True, backup=tmp_path / "b.jsonl")
    assert {p: p.read_bytes() for p in before} == before and not (tmp_path / "b.jsonl").exists()
    assert (report["changed"], report["no song"], len(report["conflicts"])) == (1, 1, 1)
    assert report["conflicts"][0]["keys"] == ["spotify:jw"]
    report = tagging.normalize(con, paths.tracks, dry_run=False, backup=tmp_path / "b.jsonl")
    assert report["changed"] == 1 and tagging.read(right)["title"] == "Anybody Out There"
    assert tagging.read(wrong)["title"] == "I Want It"  # left for review
    assert '"file": "Hardwell/Hardwell - Anybody Out There.mp3"' in (tmp_path / "b.jsonl").read_text()
    assert tagging.normalize(con, paths.tracks, dry_run=False, backup=tmp_path / "b.jsonl")["changed"] == 0


@pytest.mark.parametrize(
    ("title", "clean"),
    [
        ("[NOW ON SPOTIFY] UNENDLICHKEIT (The Boy The G Remix)", "UNENDLICHKEIT (The Boy The G Remix)"),
        ("Can't Get Enough ( deleting soon save it on spotify )", "Can't Get Enough"),
        ("Song (Recorded at Spotify Studios NYC)", "Song (Recorded at Spotify Studios NYC)"),
    ],
)
def test_a_note_about_spotify_is_not_part_of_a_title(title: str, clean: str) -> None:
    assert tagging.clean_title(title, "Someone") == clean


def test_the_core_of_a_title() -> None:
    from echolot.library.recordings import core

    assert core("Sweet Lovin' - Radio Edit") == core("Sweet Lovin' - Original Mix") == "sweet lovin"
    assert core("Ignite (feat. SEUNGRI)") == core("Ignite") == "ignite"
    assert core("Adagio for Strings - Unmixed Version") == "adagio for strings"


def test_songs_sharing_a_file_give_it_all_their_artists(env) -> None:
    """Spotify lists Komm mit twice (with and without its featured artist): one recording, so the file gets
    both artists, led by the song named as its folder."""
    con, paths = env
    rel = "TheDoDo/TheDoDo - Komm mit.flac"
    add_song(con, "spotify:a", "TheDoDo", "Komm Mit", artists='["TheDoDo"]', file=rel)
    add_song(con, "spotify:b", "TheDoDo", "Komm mit", artists='["TheDoDo", "Pbb Yea"]', file=rel)
    add_song(con, "spotify:c", "Pbb Yea", "Komm mit", artists='["Pbb Yea", "TheDoDo"]', file=rel)
    tags = tagging.for_file(con, rel, copy(paths, "flac", "TheDoDo/TheDoDo - Komm mit"))
    assert (tags.artists, tags.albumartist, tags.title) == (["TheDoDo", "Pbb Yea"], "TheDoDo", "Komm mit")


@pytest.mark.parametrize("ext", FORMATS)
def test_a_spotify_songs_release_facts(env, ext: str) -> None:
    """A Spotify song's file gets its release's date, track and disc number and the ISRC; the uploader's
    other spellings of them (another release's) go. A close match or a SoundCloud song gets none."""
    con, paths = env
    rel = f"Inner Voice/Inner Voice - Celestial.{ext}"
    facts = {"released": "2020-07-21", "track": 2, "tracks": 4, "disc": 1, "isrc": "qzhn92089013"}
    add_song(con, "spotify:iv", "Inner Voice", "Celestial", album="Sphere", file=rel, **facts)
    p = copy(paths, ext, "Inner Voice/Inner Voice - Celestial")
    if ext == "flac":
        m = File(p)
        m.tags["year"], m.tags["totaltracks"], m.tags["tracknumber"] = ["1999"], ["12"], ["7"]
        m.save()
    tags = tagging.for_file(con, rel, p)
    assert tags.facts == {"isrc": "QZHN92089013", "date": "2020-07-21", "track": "2/4", "disc": "1"}
    assert set(tagging.differs(p, tags)) >= {"isrc", "date", "track", "disc"}
    tagging.write(p, tags)
    assert tagging.read(p)["facts"] == tags.facts and not tagging.differs(p, tags)
    if ext == "flac":
        assert "year" not in File(p).tags and "totaltracks" not in File(p).tags
    with con:
        con.execute('UPDATE songs SET close_match = 1, link = \'["Inner Voice", "Celestial (Edit)", 200]\'')
    assert tagging.for_file(con, rel, p).facts == {}
    add_song(con, "soundcloud:5", "Inner Voice", "Celestial", released="2020")
    assert tagging.facts(con.execute("SELECT * FROM songs WHERE key = 'soundcloud:5'").fetchone()) == {}
