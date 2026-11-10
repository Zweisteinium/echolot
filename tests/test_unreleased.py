"""Unreleased songs (library/unreleased.py, web/unreleased.py) and released songs on none of your lists
(web/upload.py): uploaded by hand, checked for a release first; an unreleased song is its own file in
tracks/Unreleased/ and left alone from then on (no search, upgrade, retag, cover, picture, merge or name match)."""

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import OWNER
from fastapi.testclient import TestClient
from test_acquire import FakeDaemon
from test_acquire import run as sync_run  # noqa: F401 (a fixture: a sync run with a fake Soulseek)

from echolot import db
from echolot.config import Settings
from echolot.jobs import acquire, covers, lossycheck
from echolot.jobs.worker import Run
from echolot.library import (
    audio,
    catalog,
    discover,
    filing,
    identity,
    playlists,
    recordings,
    tagging,
    unreleased,
    upload,
)
from echolot.library.filing import Paths
from echolot.services import catalogs
from echolot.settings import auth, sources
from echolot.web import create_app, stats

AUDIO = Path(__file__).parent / "fixtures" / "audio"


@pytest.fixture(autouse=True)
def no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check before a file is shown (audio.prepare runs ffmpeg): audio passes as it is."""
    monkeypatch.setattr(audio, "prepare", lambda path, keep_hires=False: audio.Prepared(path, False, {"verdict": "ok"}))


def paths_of(settings: Settings) -> Paths:
    return Paths(settings.library_dir.parent)


def uploaded(settings: Settings, src: str = "silence.mp3", name: str = "") -> upload.File:
    """A file uploaded into a new batch, as checked."""
    paths = paths_of(settings)
    batch = upload.start(paths)
    with (AUDIO / src).open("rb") as fh:
        return upload.add(paths, batch, name or f"track{Path(src).suffix}", fh)


def put(settings: Settings, artist: str = "Felix", title: str = "Night Drive", src: str = "silence.mp3") -> str:
    """An unreleased song in a new list of the owner's (its key)."""
    con = db.connect(settings.db_path)
    try:
        key = unreleased.create(con, OWNER, f"From {artist}")
        f = uploaded(settings, src)
        line, song = unreleased.add(con, paths_of(settings), f, unreleased.Names(artist, title), key, "from Felix")
        assert song, line
        catalog.match_songs(con)
        return song
    finally:
        con.close()


def test_an_unreleased_song_is_its_own_file_in_its_list(settings: Settings) -> None:
    con = db.connect(settings.db_path)
    key = unreleased.create(con, OWNER, "Friends")
    f = uploaded(settings, "silence.mp3", "track01.mp3")
    line, song = unreleased.add(con, paths_of(settings), f, unreleased.Names(" Felix ", "Night  Drive"), key, "a gift")
    catalog.match_songs(con)
    assert line == "Felix – Night Drive: filed as Unreleased/Felix - Night Drive.mp3" and song.startswith("local:")
    row = con.execute("SELECT * FROM songs WHERE key = ?", (song,)).fetchone()
    assert (row["service"], row["file"], row["stem"]) == (
        "local",
        "Unreleased/Felix - Night Drive.mp3",
        "Unreleased/Felix - Night Drive",
    )
    assert row["unavailable"] == unreleased.UNAVAILABLE  # (nothing better can be had, nothing searches)
    assert [r[0] for r in con.execute("SELECT song_key FROM list_songs WHERE list_key = ?", (key,))] == [song]
    lst = con.execute("SELECT * FROM lists WHERE key = ?", (key,)).fetchone()
    assert (lst["service"], lst["title"], lst["fetched"]) == ("local", "Friends", 1)
    event = con.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()
    assert (event["action"], event["source"], event["song"], event["reason"]) == ("new", "manual", song, "a gift")
    path = settings.library_dir / "Unreleased" / "Felix - Night Drive.mp3"
    tags = tagging.read(path)
    assert (tags["artists"], tags["title"], tags["album"], tags["download"]) == (
        ["Felix"],
        "Night Drive",
        "Night Drive",
        "by hand",
    )
    assert "Unreleased · a gift" in tags["text"]
    again = uploaded(settings)
    line, none = unreleased.add(con, paths_of(settings), again, unreleased.Names("Felix", "Night Drive"), key)
    assert none is None and "is another song's: give it another title" in line  # never in place of another
    assert unreleased.taken(paths_of(settings), unreleased.Names("felix", "Night Drive")) is False  # (case: own name)
    con.close()


def test_no_other_song_takes_an_unreleased_file_by_its_names(settings: Settings) -> None:
    """A released song named as the unreleased one (and the other way round) is never matched to it."""
    song = put(settings, "Artist C", "Gone Song")  # s3 is missing and named so
    con = db.connect(settings.db_path)
    assert con.execute("SELECT file FROM songs WHERE key = 'spotify:s3'").fetchone()[0] is None
    assert (
        con.execute("SELECT file FROM songs WHERE key = ?", (song,)).fetchone()[0]
        == "Unreleased/Artist C - Gone Song.mp3"
    )
    cat = catalog.Catalog.from_db(con)
    assert cat.find("Artist C", "Gone Song") == [] and not recordings.Index(cat).candidates("Gone Song", 1)
    other = put(settings, "Artist A", "First Song")  # spotify:s1's file Artist A/Artist A - First Song.mp3 exists
    assert (
        con.execute("SELECT file FROM songs WHERE key = ?", (other,)).fetchone()[0]
        == "Unreleased/Artist A - First Song.mp3"
    )
    assert (
        con.execute("SELECT file FROM songs WHERE key = 'spotify:s1'").fetchone()[0]
        == "Artist A/Artist A - First Song.mp3"
    )
    con.close()


def test_a_gone_file_is_missing_and_never_searched(settings: Settings) -> None:
    song = put(settings)
    (settings.library_dir / "Unreleased" / "Felix - Night Drive.mp3").unlink()
    con = db.connect(settings.db_path)
    catalog.refresh(con, settings.library_dir)
    assert con.execute("SELECT file FROM songs WHERE key = ?", (song,)).fetchone()[0] is None
    assert song not in [r["key"] for r in acquire._missing(con)]  # (nothing searches for it)
    gone = next(s for s in stats.missing(con, uid=OWNER) if s["key"] == song)
    assert gone["notes"][0][0] == unreleased.UNAVAILABLE
    tiers = {tier: n for tier, _, n in stats.best_tiers(con, OWNER)}
    assert tiers["gone"] >= 1  # (wiped, not "still searching": there is no other copy)
    con.close()


def test_jobs_leave_an_unreleased_song_alone(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Its names, cover and file stay: no tags from a song, no cover, no artist picture, no FLAC upgrade, no
    lossy check, no merge with a file of the same audio."""
    song = put(settings, src="silence.flac")
    rel = "Unreleased/Felix - Night Drive.flac"
    con = db.connect(settings.db_path)
    assert tagging.lead(con, rel)["key"] == song and tagging.for_file(con, rel, settings.library_dir / rel) is None
    report = tagging.normalize(con, settings.library_dir, dry_run=True, backup=None)
    assert not any(e["file"] == rel for e in report["examples"])
    asked = []

    class Sp:
        def search(self, q: str, limit: int) -> list:
            return asked.append(q) or []

        def track(self, tid: str) -> dict:
            return asked.append(tid) or {}

    c = covers.Covers.__new__(covers.Covers)
    c.con, c.cache, c.token, c.sp = con, {}, "token", Sp()
    assert c.of(tagging.lead(con, rel)) is None and asked == []
    monkeypatch.setattr(acquire.spotify, "Spotify", lambda *a: Sp())
    acquire.pictures(None, con, settings.library_dir / rel, acquire.Want.of(tagging.lead(con, rel)), None)
    assert asked == []
    copy = settings.library_dir / "Artist B" / "Artist B - Night Drive.flac"
    shutil.copy(AUDIO / "silence.flac", copy)
    catalog.scan(con, settings.library_dir)
    with con:
        con.execute("UPDATE songs SET title = 'Night Drive', length = 1, link = NULL WHERE key = 'spotify:s2'")
    catalog.match_songs(con)
    assert (
        con.execute("SELECT file FROM songs WHERE key = 'spotify:s2'").fetchone()[0]
        == copy.relative_to(settings.library_dir).as_posix()
    )
    monkeypatch.setattr(identity, "same_master", lambda a, b: 1.0)
    assert recordings.merge_same(con, paths_of(settings)) == []
    assert (settings.library_dir / rel).is_file()
    con.close()


def test_the_flac_upgrade_skips_an_unreleased_song(sync_run: Run) -> None:  # noqa: F811 (the imported fixture)
    """A lossy unreleased song is never searched for a FLAC (there is no other copy), Run now neither."""
    put(sync_run.settings)
    acquire.upgrade_all(sync_run)
    assert FakeDaemon.searches and "Night Drive" not in str(FakeDaemon.searches)


def test_the_lossy_check_skips_unreleased(sync_run: Run, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    put(sync_run.settings, src="silence.flac")
    checked = []
    monkeypatch.setattr(audio, "lossprint_on", lambda: True)
    monkeypatch.setattr(lossycheck, "_one", lambda run, con, row, old: checked.append(row["path"]) or "ok")
    lossycheck.run(sync_run)
    assert checked == ["Artist B/Artist B - Second Song.flac"]


def test_its_lists_are_echolots_own(settings: Settings) -> None:
    """Out of the configuration (an import keeps them), a playlist of their files, the Best column counts them."""
    song = put(settings)
    con = db.connect(settings.db_path)
    key = unreleased.lists(con, OWNER)[0]["key"]
    assert "local" not in json.dumps(sources.as_config(con, OWNER))
    with con:
        sources.replace_rows(con, sources.as_config(con, OWNER), OWNER)
    assert [s.key for s in sources.user_lists(con, OWNER) if s.service == "local"] == [key]
    other = settings.library_dir / "Felix" / "Felix - Night Drive.mp3"  # another song's file of that name
    other.parent.mkdir()
    shutil.copy(AUDIO / "silence.mp3", other)
    catalog.scan(con, settings.library_dir)
    folder = settings.library_dir.parent / "playlists"
    playlists.write(con, folder)
    m3u = folder / "owner" / f"{sources.unreleased_name(key)}.m3u"
    assert m3u.read_text().splitlines()[1:] == [
        "#PLAYLIST:From Felix",
        "../../tracks/Unreleased/Felix - Night Drive.mp3",
    ]
    row = con.execute("SELECT * FROM lists WHERE key = ?", (key,)).fetchone()
    assert playlists.comment(row) == "Unreleased songs you uploaded · kept as they are"
    mine = next(r for r in stats.lists(con, OWNER) if r["key"] == key)
    assert (mine["grp"], mine["songs"], mine["best"]) == ("echolot", 1, 1)
    assert song not in [r["key"] for r in stats.upload_songs(con, OWNER)]  # (Upload files never takes it)
    unreleased.rename(con, OWNER, key, "Felix")
    assert con.execute("SELECT title FROM lists WHERE key = ?", (key,)).fetchone()[0] == "Felix"
    with pytest.raises(unreleased.Refused, match="already"):
        unreleased.create(con, OWNER, " felix ")
    with pytest.raises(unreleased.Refused, match="none of your other"):  # (a song stays on one of your lists)
        unreleased.remove(con, OWNER, key, song)
    second = unreleased.create(con, OWNER, "Second")
    unreleased.add_to(con, OWNER, second, song)
    unreleased.remove(con, OWNER, key, song)
    assert not con.execute("SELECT 1 FROM list_songs WHERE list_key = ?", (key,)).fetchone()
    unreleased.move(con, OWNER, second, song, key)
    assert unreleased.in_lists(con, OWNER, song) == [key]
    paths = paths_of(settings)
    with pytest.raises(unreleased.Refused, match="Choose"):  # (its song is on no other list)
        unreleased.delete(con, paths, OWNER, key, "")
    assert unreleased.delete(con, paths, OWNER, key, second) == 1
    assert not con.execute("SELECT 1 FROM lists WHERE key = ?", (key,)).fetchone()
    assert unreleased.in_lists(con, OWNER, song) == [second]
    assert (settings.library_dir / "Unreleased" / "Felix - Night Drive.mp3").is_file()  # (its file stays)
    with pytest.raises(unreleased.Refused):
        unreleased.delete(con, paths, OWNER, key, second)
    con.close()


def test_an_artist_named_unreleased_gets_a_folder_of_its_own(settings: Settings) -> None:
    put(settings)
    con = db.connect(settings.db_path)
    cat = catalog.Catalog.from_db(con)
    con.close()
    assert filing.artist_dir(paths_of(settings), cat, "Unreleased") != settings.library_dir / "Unreleased"


def test_the_names_a_file_gives() -> None:
    f = upload.File(1, "x.mp3", path=Path("/x/02 01 - Felix - Night Drive.mp3"))
    assert upload.names(f) == ("Felix", "Night Drive")
    assert upload.names(upload.File(1, "a.wav", path=Path("/x/03 track01.wav"))) == ("", "")
    assert upload.names(upload.File(1, "a.mp3", artist="A", title="T", path=Path("/x/04 B - C.mp3"))) == ("A", "T")


def hit(title: str, seconds: float, preview: str = "https://cdn.test/p.mp3", artist: str = "Artist X") -> discover.Hit:
    h = discover.Hit("deezer", 0, artist, title, seconds, "The Album", url="https://www.deezer.com/track/1")
    h.preview = preview
    return h


@pytest.fixture
def audio_check(monkeypatch: pytest.MonkeyPatch) -> dict:
    """The release check's audio, faked: the preview's fingerprint agrees with the file's by `share`."""
    state = {"share": 0.9, "clips": []}
    monkeypatch.setattr(identity, "clip", lambda url: state["clips"].append(url) or b"mp3")
    monkeypatch.setattr(identity, "fingerprint", lambda source: identity.np.ones(4, dtype="<u4"))
    monkeypatch.setattr(identity, "similarity", lambda ref, cand: state["share"])
    monkeypatch.setattr(identity, "waveform", lambda path, clip: 0.95)
    return state


def test_the_release_check(tmp_path: Path, audio_check: dict) -> None:
    f = upload.File(1, "x.flac", path=tmp_path / "x.flac", seconds=200)
    found = upload.release(f, "Artist X", "Song", lambda q: [hit("Song", 201), hit("Song (Remix)", 300)])
    assert found.verdict == "released" and found.fit.fingerprint == 0.9 and found.fit.waveform == 0.95
    assert found.result.title == "Song" and len(audio_check["clips"]) == 2  # (the remix too: the same core title)
    found = upload.release(f, "Artist X", "Song", lambda q: [hit("Song", 260)])
    assert found.verdict == "resembles"  # the recording at another length: another edit
    audio_check["share"] = 0.55
    assert upload.release(f, "Artist X", "Song", lambda q: [hit("Song", 200)]) is None  # another song
    found = upload.release(f, "Artist X", "Song", lambda q: [hit("Song", 200, preview="")])
    assert found.verdict == "" and found.fit.fingerprint is None  # by its names only
    assert upload.release(f, "", "Song", lambda q: [hit("Song", 200)]) is None


def test_upload_unreleased_songs(settings: Settings, login: Callable[..., TestClient], monkeypatch) -> None:
    """The dialog: each file named by its tags or file name, checked for a release (none here), imported into a
    new list with the note and a cover; the list's page, the Library page."""
    monkeypatch.setattr(catalogs, "deezer", lambda q, limit=20: [])
    client = login(create_app(settings))
    dialog = client.get("/unreleased/upload").text
    batch = re.search(r'data-batch="([0-9a-f]{16})"', dialog).group(1)
    assert 'data-post="/unreleased/upload/' in dialog and '<option value="new" selected>New list…' in dialog
    files = {"file": ("Felix - Night Drive.mp3", (AUDIO / "silence.mp3").read_bytes(), "audio/mpeg")}
    row = client.post(f"/unreleased/upload/{batch}/file", files=files).text
    assert 'name="artist_1" value="Felix"' in row and 'name="title_1" value="Night Drive"' in row
    assert "Not released" in row and 'name="song_1" value="local"' in row
    files = {"file": ("track02.wav", (AUDIO / "silence.wav").read_bytes(), "audio/wav")}
    row = client.post(f"/unreleased/upload/{batch}/file", files=files).text
    assert "Needs names" in row and 'name="artist_2" value=""' in row
    again = client.post(f"/unreleased/upload/{batch}/2/check", data={"artist_2": "Felix", "title_2": "Night Drive"})
    assert "Name taken" not in again.text  # (not filed yet: the other one is still in the dialog)
    cover = (b"\xff\xd8\xff\xe0" + b"0" * 64, "image/jpeg")
    batch_form = {"list": "new", "new_list": "From Felix", "note": "from Felix, 2026"}
    rows = {"song_1": "local", "artist_1": "Felix", "title_1": "Night Drive", "album_1": ""}
    rows |= {"song_2": "local", "artist_2": "Felix", "title_2": "Morning Run"}
    form = batch_form | rows
    done = client.post(f"/unreleased/upload/{batch}/import", data=form, files={"cover": ("c.jpg", *cover)}).text
    assert "Felix – Night Drive: filed as Unreleased/Felix - Night Drive.mp3" in done
    assert "Felix – Morning Run: filed as Unreleased/Felix - Morning Run.wav" in done
    assert audio.picture(settings.library_dir / "Unreleased" / "Felix - Night Drive.mp3") == cover[0]
    con = db.connect(settings.db_path)
    key = unreleased.lists(con, OWNER)[0]["key"]
    con.close()
    assert f'href="/lists/{key}"' in done
    page = client.get(f"/lists/{key}").text
    assert "From Felix" in page and "Upload unreleased" in page and f"/unreleased/{key}/song" in page
    assert 'value="move:' not in page and ">Delete the song</option>" in page  # (no other list to move into)
    assert "Open on" not in page and "Felix - Night Drive.mp3" in page
    library = client.get("/library").text
    assert "Echolot" in library and ">unreleased</span>" in library and "Upload unreleased" in library
    assert client.post(f"/unreleased/{key}/rename", data={"name": "Felix"}, follow_redirects=False).status_code == 303
    r = client.post(f"/unreleased/{key}/delete", follow_redirects=False)
    assert r.status_code == 303 and "error=Choose" in r.headers["location"]  # (where its songs go, or not)
    assert (settings.library_dir / "Unreleased" / "Felix - Morning Run.wav").is_file()
    r = client.post(f"/unreleased/{key}/delete", data={"songs_to": unreleased.DROP}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/library")
    assert not (settings.library_dir / "Unreleased" / "Felix - Morning Run.wav").exists()
    kept = list((paths_of(settings).inbox("replaced")).rglob("Felix - Morning Run.wav"))
    assert len(kept) == 1  # (kept 30 days)


def test_a_released_upload_goes_into_wished(
    settings: Settings, login: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch, audio_check: dict
) -> None:
    """Both dialogs: a file that is a release on none of your lists is imported as that song, into your
    Echolot · Wished (not searched: its file is here)."""
    monkeypatch.setattr(catalogs, "deezer", lambda q, limit=20: [hit("Fresh Tune", 1, artist="Newcomer")])
    monkeypatch.setattr(catalogs, "track", lambda tid: {"isrc": "QZ0000000001"})
    client = login(create_app(settings))
    dialog = client.get("/missing/upload").text
    batch = re.search(r'data-batch="([0-9a-f]{16})"', dialog).group(1)
    files = {"file": ("Newcomer - Fresh Tune.flac", (AUDIO / "silence.flac").read_bytes(), "audio/flac")}
    row = client.post(f"/missing/upload/{batch}/file", files=files).text
    sid = re.search(r'name="song_1" value="wish:(ex-[0-9a-f]+)"', row).group(1)
    assert "Released" in row and "into Echolot · Wished" in row and " disabled" not in row.split("wish:")[1][:40]
    done = client.post(f"/missing/upload/{batch}/import", data={"song_1": f"wish:{sid}"}).text
    assert "Newcomer – Fresh Tune: filed as Newcomer/Newcomer - Fresh Tune.flac" in done
    con = db.connect(settings.db_path)
    key = "discover:" + sid.removeprefix("ex-")
    song = con.execute("SELECT * FROM songs WHERE key = ?", (key,)).fetchone()
    assert song["file"] == "Newcomer/Newcomer - Fresh Tune.flac" and song["isrc"] == "QZ0000000001"
    wished = "SELECT song_key FROM list_songs WHERE list_key = ?"
    assert key in [r[0] for r in con.execute(wished, (sources.wished_key(OWNER),))]
    con.close()
    dialog = client.get("/unreleased/upload").text  # the unreleased dialog: a release is better imported as it
    batch = re.search(r'data-batch="([0-9a-f]{16})"', dialog).group(1)
    files = {"file": ("Newcomer - Fresh Tune.flac", (AUDIO / "silence.flac").read_bytes(), "audio/flac")}
    row = client.post(f"/unreleased/upload/{batch}/file", files=files).text
    assert "You have it" in row and '<option value="" selected>Leave it out' in row  # (its audio is in the library)
    assert re.search(r'<option value="discover:[0-9a-f]+">Import as your Newcomer – Fresh Tune', row)


def test_unreleased_needs_the_upload_permission(settings: Settings, login: Callable[..., TestClient]) -> None:
    client = login(create_app(settings), "guest", admin=False)
    assert client.get("/unreleased/upload").status_code == 403
    con = db.connect(settings.db_path)
    user = auth.get_user(con, "guest")
    auth.set_rights(con, user.id, False, {"upload"})
    con.close()
    assert client.get("/unreleased/upload").status_code == 200
    con = db.connect(settings.db_path)
    key = unreleased.create(con, OWNER, "Owner's")
    con.close()
    r = client.post(f"/unreleased/{key}/delete", follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]  # (not theirs)


def test_a_release_that_is_one_of_your_songs_is_that_song(
    settings: Settings, login: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch, audio_check: dict
) -> None:
    """A file named as another release of your song (another artist credit), whose audio is it: that song, by
    the release's ISRC, in both dialogs; never a second song in Echolot · Wished."""
    con = db.connect(settings.db_path)
    with con:
        con.execute("UPDATE songs SET isrc = 'QZHZD2280962' WHERE key = 'spotify:s3'")
    con.close()
    monkeypatch.setattr(catalogs, "deezer", lambda q, limit=20: [hit("Gone Song", 1, artist="Other Credit")])
    monkeypatch.setattr(catalogs, "track", lambda tid: {"isrc": "QZHZD2280962"})
    client = login(create_app(settings))
    files = {"file": ("Other Credit - Gone Song.flac", (AUDIO / "silence.flac").read_bytes(), "audio/flac")}
    batch = re.search(r'data-batch="([0-9a-f]{16})"', client.get("/missing/upload").text).group(1)
    row = client.post(f"/missing/upload/{batch}/file", files=files).text
    assert '<input type="hidden" name="song_1" value="spotify:s3">' in row and "wish:" not in row
    batch = re.search(r'data-batch="([0-9a-f]{16})"', client.get("/unreleased/upload").text).group(1)
    row = client.post(f"/unreleased/upload/{batch}/file", files=files).text
    assert '<option value="spotify:s3" selected>Import as your Artist C – Gone Song' in row
    assert client.post(f"/unreleased/upload/{batch}/import", data={"song_1": "spotify:nope"}).status_code == 403
    done = client.post(f"/unreleased/upload/{batch}/import", data={"song_1": "spotify:s3"}).text
    assert "Artist C – Gone Song: filed as Artist C/Artist C - Gone Song.flac" in done


def test_what_the_dialog_refuses(settings: Settings, login: Callable[..., TestClient], monkeypatch) -> None:
    """Said as the dialog shows it (the import's error text); the files stay in the dialog to try again."""
    monkeypatch.setattr(catalogs, "deezer", lambda q, limit=20: [])
    client = login(create_app(settings))
    batch = re.search(r'data-batch="([0-9a-f]{16})"', client.get("/unreleased/upload").text).group(1)
    files = {"file": ("Felix - Night Drive.mp3", (AUDIO / "silence.mp3").read_bytes(), "audio/mpeg")}
    client.post(f"/unreleased/upload/{batch}/file", files=files)
    form = {"list": "new", "new_list": " ", "song_1": "local", "artist_1": "Felix", "title_1": "Night Drive"}
    r = client.post(f"/unreleased/upload/{batch}/import", data=form)
    assert r.status_code == 400 and r.json()["detail"] == "A list needs a name."
    png = {"cover": ("c.png", b"\x89PNG\r\n\x1a\n", "image/png")}
    r = client.post(f"/unreleased/upload/{batch}/import", data=form | {"new_list": "Felix"}, files=png)
    assert r.status_code == 400 and "JPEG" in r.json()["detail"]
    done = client.post(f"/unreleased/upload/{batch}/import", data=form | {"new_list": "Felix"}).text
    assert "Felix – Night Drive: filed as Unreleased/Felix - Night Drive.mp3" in done


def test_only_its_owner_manages_an_unreleased_list(settings: Settings, login: Callable[..., TestClient]) -> None:
    """An admin seeing everyone's lists sees another user's Unreleased list, without its Rename, Delete or ×."""
    put(settings)
    con = db.connect(settings.db_path)
    key = unreleased.lists(con, OWNER)[0]["key"]
    con.close()
    client = login(create_app(settings), "admin2")
    assert client.post("/account/view", data={"view": "everyone"}, follow_redirects=False).status_code == 303
    page = client.get(f"/lists/{key}").text
    assert "From Felix" in page and "Felix - Night Drive.mp3" in page
    assert "/rename" not in page and "/delete" not in page and "/song" not in page and "Upload" not in page


def test_up_to_ten_lists(settings: Settings, login: Callable[..., TestClient]) -> None:
    con = db.connect(settings.db_path)
    for n in range(unreleased.MAX_LISTS):
        unreleased.create(con, OWNER, f"List {n}")
    with pytest.raises(unreleased.Refused, match="10 Unreleased lists already"):
        unreleased.create(con, OWNER, "One more")
    con.close()
    dialog = login(create_app(settings)).get("/unreleased/upload").text
    assert "List 9 (0)" in dialog and "New list…" not in dialog  # (the newest chosen)


def test_a_songs_menu_moves_adds_takes_out_and_deletes(settings: Settings, login: Callable[..., TestClient]) -> None:
    song = put(settings)
    con = db.connect(settings.db_path)
    first = unreleased.lists(con, OWNER)[0]["key"]
    second = unreleased.create(con, OWNER, "Second")
    con.close()
    client = login(create_app(settings))

    def act(key: str, to: str) -> str:
        r = client.post(f"/unreleased/{key}/song", data={"song": song, "to": to}, follow_redirects=False)
        assert r.status_code == 303
        return r.headers["location"]

    page = client.get(f"/lists/{first}").text
    assert f'value="move:{second}"' in page and f'value="add:{second}"' in page and 'value="remove"' not in page
    assert "error=" in act(first, "remove")  # (its only list)
    assert "ok=" in act(first, f"add:{second}")
    assert 'value="remove"' in client.get(f"/lists/{first}").text
    assert "ok=" in act(first, "remove")
    assert "error=" in act(first, f"move:{second}")  # (not on this list any more)
    assert "ok=" in act(second, f"move:{first}")
    con = db.connect(settings.db_path)
    assert unreleased.in_lists(con, OWNER, song) == [first]
    con.close()
    path = settings.library_dir / "Unreleased" / "Felix - Night Drive.mp3"
    assert "ok=" in act(first, unreleased.DROP) and not path.exists()
    assert list(paths_of(settings).inbox("replaced").rglob("Felix - Night Drive.mp3"))  # (kept 30 days)
    con = db.connect(settings.db_path)
    assert con.execute("SELECT 1 FROM songs WHERE key = ?", (song,)).fetchone() is None
    con.close()


def test_a_file_the_library_has_is_told_and_a_better_copy_replaces_it(
    settings: Settings, login: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every upload is held against the library files named alike (unreleased ones too): shown only when its
    audio says it may be one; the same audio is left out, a better copy of an unreleased song replaces its file."""
    song = put(settings)  # (Felix - Night Drive, an MP3)
    monkeypatch.setattr(catalogs, "deezer", lambda q, limit=20: [])
    held: list[tuple[str, str]] = []
    share = {"fp": 0.95, "wave": 0.97}

    def against(path: Path, copy: Path) -> tuple[float, float]:
        held.append((path.name, copy.name))
        return share["fp"], share["wave"]

    monkeypatch.setattr(identity, "against", against)
    client = login(create_app(settings))
    batch = re.search(r'data-batch="([0-9a-f]{16})"', client.get("/unreleased/upload").text).group(1)
    files = {"file": ("Felix - Night Drive (Master).flac", (AUDIO / "silence.flac").read_bytes(), "audio/flac")}
    row = client.post(f"/unreleased/upload/{batch}/file", files=files).text
    assert held and "You have it" in row and "Not released" not in row
    assert f'<option value="replace:{song}" selected>Replace your copy (better' in row
    share["fp"] = 0.6  # (named alike, another song by its audio: not shown)
    again = client.post(f"/unreleased/upload/{batch}/1/check", data={"artist_1": "Felix", "title_1": "Night Drive"})
    assert "You have it" not in again.text and "Not released" in again.text and "replace:" not in again.text
    share["fp"] = 0.95
    done = client.post(f"/unreleased/upload/{batch}/import", data={"song_1": f"replace:{song}"}).text
    assert "Felix – Night Drive: replaced by Unreleased/Felix - Night Drive.flac (was Felix - Night Drive.mp3)" in done
    assert not (settings.library_dir / "Unreleased" / "Felix - Night Drive.mp3").exists()
    assert list(paths_of(settings).inbox("replaced").rglob("Felix - Night Drive.mp3"))
    con = db.connect(settings.db_path)
    assert con.execute("SELECT file FROM songs WHERE key = ?", (song,)).fetchone()[0] == (
        "Unreleased/Felix - Night Drive.flac"
    )
    con.close()
    batch = re.search(r'data-batch="([0-9a-f]{16})"', client.get("/unreleased/upload").text).group(1)
    files = {"file": ("Felix - Night Drive.mp3", (AUDIO / "silence.mp3").read_bytes(), "audio/mpeg")}
    row = client.post(f"/unreleased/upload/{batch}/file", files=files).text  # (the MP3 again: not better)
    assert "You have it" in row and '<option value="" selected>Leave it out' in row


def test_the_release_check_looks_by_title_alone_and_shows_only_what_the_audio_says(
    tmp_path: Path, settings: Settings, login: Callable[..., TestClient], monkeypatch, audio_check: dict
) -> None:
    f = upload.File(1, "x.flac", path=tmp_path / "x.flac", seconds=200)
    asked: list[str] = []

    def search(q: str) -> list[discover.Hit]:
        asked.append(q)
        return [hit("Song", 201, artist="Alias")] if q == "Song" else []

    assert upload.release(f, "Friend", "Song", search) is None and asked == ["Friend Song"]
    found = upload.release(f, "Friend", "Song", search, loose=True)
    assert asked[1:] == ["Friend Song", "Song"] and found.verdict == "released"
    monkeypatch.setattr(catalogs, "deezer", lambda q, limit=20: [hit("Night Drive", 1, preview="")])
    client = login(create_app(settings))
    batch = re.search(r'data-batch="([0-9a-f]{16})"', client.get("/unreleased/upload").text).group(1)
    files = {"file": ("Felix - Night Drive.mp3", (AUDIO / "silence.mp3").read_bytes(), "audio/mpeg")}
    row = client.post(f"/unreleased/upload/{batch}/file", files=files).text
    assert "Named like" not in row and "Not released" in row  # (named so, no preview: nothing to tell by)
