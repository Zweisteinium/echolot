"""Upload by hand for missing songs (library/upload.py, web/upload.py): files checked in a dialog, each
one's song guessed and changeable, imported on confirm as that song; the rest of the batch goes."""

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from echolot import db
from echolot.config import Settings
from echolot.library import audio, filing, upload
from echolot.library.filing import Paths
from echolot.web import create_app

AUDIO = Path(__file__).parent / "fixtures" / "audio"


@pytest.fixture(autouse=True)
def no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check before a file is shown (audio.prepare runs ffmpeg): audio passes as it is, text does not."""

    def prepare(path: Path, keep_hires: bool = False) -> audio.Prepared:
        if path.suffix == ".txt":
            path.unlink()
            raise audio.Rejected("codec 'none' in a .txt file")
        return audio.Prepared(path, False, {"verdict": "ok"})

    monkeypatch.setattr(audio, "prepare", prepare)


def batch_of(html: str) -> str:
    return re.search(r'data-batch="([0-9a-f]{16})"', html).group(1)


def post_files(client: TestClient, files: list[tuple], data: dict | None = None) -> str:
    """As the dialog does it: opened (a new batch), then each file uploaded on its own; the rows, with the batch."""
    song = (data or {}).get("song", "")
    dialog = client.get("/missing/upload", params={"song": song} if song else None)
    assert dialog.status_code == 200 and "data-files" in dialog.text
    batch = batch_of(dialog.text)
    rows = []
    for _, (name, content, mime) in files:
        r = client.post(f"/missing/upload/{batch}/file", files={"file": (name, content, mime)}, data=data or {})
        assert r.status_code == 200
        rows.append(r.text)
    return f'<div data-batch="{batch}">' + "".join(rows)


def test_upload_check_and_import(settings: Settings, login: Callable[..., TestClient]) -> None:
    client = login(create_app(settings))
    page = client.get("/missing").text
    assert "Upload files" in page and 'hx-get="/missing/upload?song=spotify%3As3"' in page
    assert "Choose files" in client.get("/missing/upload").text
    assert (
        client.post("/missing/upload/0123456789abcdef/file", files={"file": ("x.flac", b"x", "audio/flac")}).status_code
        == 404
    )
    files = [
        ("files", ("Artist C - Gone Song.flac", (AUDIO / "silence.flac").read_bytes(), "audio/flac")),
        ("files", ("notes.txt", b"not audio", "text/plain")),
    ]
    html = post_files(client, files)
    batch = batch_of(html)
    assert '<input type="hidden" name="song_1" value="spotify:s3">' in html  # detected by its name
    assert "Artist C – Gone Song" in html and "<select" not in html
    assert "Not audio" in html and 'name="song_2"' not in html and html.count('class="upload-row"') == 2
    assert re.search(r"\d:\d\d shorter|\d+ s shorter", html)  # what is off: its length
    assert "q-lossless" in html  # its quality
    done = client.post(f"/missing/upload/{batch}/import", data={"song_1": "spotify:s3"}).text
    assert "Artist C – Gone Song: filed as Artist C/Artist C - Gone Song.flac" in done
    assert 'data-imported="[&#34;spotify:s3&#34;]"' in done  # the page takes the song off
    assert (settings.library_dir / "Artist C" / "Artist C - Gone Song.flac").is_file()
    con = db.connect(settings.db_path)
    assert (
        con.execute("SELECT file FROM songs WHERE key = 'spotify:s3'").fetchone()[0]
        == "Artist C/Artist C - Gone Song.flac"
    )
    assert con.execute("SELECT source, matched FROM events ORDER BY id DESC LIMIT 1").fetchone()[:] == (
        "manual",
        "by hand",
    )
    con.close()
    assert not (filing.Paths(settings.library_dir.parent).inbox("upload") / batch).exists()  # the batch went
    assert client.post(f"/missing/upload/{batch}/import", data={"song_1": "spotify:s3"}).status_code == 404


def test_cancel_and_scope(settings: Settings, login: Callable[..., TestClient]) -> None:
    app = create_app(settings)
    client = login(app)
    one = [("files", ("x.flac", (AUDIO / "silence.flac").read_bytes(), "audio/flac"))]
    batch = batch_of(post_files(client, one, {"song": "spotify:s3"}))
    assert (
        client.post(f"/missing/upload/{batch}/import", data={"song_1": "spotify:nobody"}).status_code == 403
    )  # none of the user's songs
    assert client.post(f"/missing/upload/{batch}/cancel").status_code == 200
    assert not (filing.Paths(settings.library_dir.parent).inbox("upload") / batch).exists()
    viewer = login(app, "timon", admin=False)
    assert viewer.get("/missing/upload").status_code == 403  # no review permission
    assert "Upload files" not in viewer.get("/missing").text


def test_guess_by_title_and_length() -> None:
    songs = [
        {"key": "a", "artist": "Hurts", "title": "2 More - Radio Edit", "length": 211, "file": None, "close_match": 0},
        {"key": "b", "artist": "Hurts", "title": "Wonderful Life", "length": 220, "file": None, "close_match": 0},
    ]
    f = upload.File(1, "02 - 2 More.flac", seconds=211.2, artist="Hurts", title="2 More - Radio Edit")
    assert upload.guess(f, songs) == "a"
    assert upload.guess(upload.File(2, "track01.flac", seconds=100), songs) is None
    other = upload.File(3, "Passenger - 2 More.flac", seconds=211, artist="Passenger", title="2 More")
    assert upload.guess(other, songs) is None  # another artist's song of that title


def test_labels() -> None:
    song = {"key": "a", "artist": "Hurts", "title": "2 More", "length": 211, "file": None, "close_match": 0}
    f = upload.File(1, "x.flac", seconds=212, tier="lossless")
    assert upload.labels(f, song, upload.Fit(1, 0.95, 0.99))[0][:2] == ("Looks right", "ok")
    assert upload.labels(f, song, upload.Fit(1))[0][:2] == ("Looks right", "ok")  # no preview: the length
    off = upload.labels(upload.File(1, "x.flac", seconds=346, tier="fake"), song, upload.Fit(135, 0.55, 0.02))
    assert [t for t, _, _ in off] == ["2:15 longer", "Sounds different", "Fake FLAC"]
    master = upload.labels(f, song, upload.Fit(0, 0.96, 0.72))  # SpotiFLAC's other version under the ISRC
    assert [t for t, _, _ in master] == ["Other master or mix"]
    assert upload.labels(f, None, None)[0][0] == "No song found"
    have = song | {"file": "Hurts/Hurts - 2 More.mp3", "quality": "lossy-mid", "kbps": 160, "fake_source": None}
    copy = upload.compare(Paths(Path("/nowhere")), f, have)
    assert upload.labels(f, have, upload.Fit(0, 0.95, 0.99), copy)[0][:2] == ("Better than your 160 kbps", "ok")
    flac = have | {"quality": "lossless"}
    copy = upload.compare(Paths(Path("/nowhere")), f, flac)
    assert upload.labels(f, flac, None, copy)[0][:2] == ("Not better than your FLAC", "bad")
    assert not upload.importable(f, flac, copy)


def test_an_upload_much_quieter_than_your_copy_is_no_better(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Recorded at 25 % (Spotify in the Windows mixer): 12 dB under your copy, so no better however lossless.
    Peaking at -12 dB it is flagged on its own too; for a missing song it is still imported (your call)."""
    paths = Paths(tmp_path)
    (paths.tracks / "Gusted").mkdir(parents=True)
    (paths.tracks / "Gusted/Gusted - You Make Me.opus").write_bytes(b"x")
    monkeypatch.setattr(audio, "levels", lambda p: (-0.1, -9.5))  # the copy in the library
    monkeypatch.setattr(audio, "spectrum", lambda p: {"verdict": "ok"})
    song = {
        "key": "a",
        "artist": "Gusted",
        "title": "You Make Me",
        "length": 200,
        "close_match": 0,
        "file": "Gusted/Gusted - You Make Me.opus",
        "quality": "lossy-low",
        "kbps": 128,
        "fake_source": None,
    }
    quiet = upload.File(1, "x.flac", seconds=200, tier="lossless", peak=-12.1, rms=-21.6)
    copy = upload.compare(paths, quiet, song)
    assert copy is not None and not copy.better and copy.quieter == 12.1
    assert upload.labels(quiet, song, None, copy)[0][:2] == ("12 dB quieter than your copy", "bad")
    assert not upload.importable(quiet, song, copy)
    loud = upload.File(1, "x.flac", seconds=200, tier="lossless", peak=-0.1, rms=-9.8)
    assert upload.compare(paths, loud, song).better
    missing = song | {"file": None}
    assert ("Quiet: peaks at -12.1 dB", "warn") in [label[:2] for label in upload.labels(quiet, missing, upload.Fit(0))]
    assert upload.importable(quiet, missing, None)


def test_a_fake_flac_counts_by_its_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Enmity's FLAC made from a ~256 kbps file beats a 123 kbps Opus of the same reach (20 kHz); one made from
    ~128 kbps does not, nor one whose sound stops lower than the copy's; a lossy file needs a quarter more."""
    paths = Paths(tmp_path)
    (paths.tracks / "Enmity").mkdir(parents=True)
    (paths.tracks / "Enmity/Enmity - Sex.opus").write_bytes(b"x")
    monkeypatch.setattr(audio, "spectrum", lambda p: {"verdict": "ok", "cutoff_hz": 20000, "drop_db": 46.0})
    opus = {
        "file": "Enmity/Enmity - Sex.opus",
        "quality": "lossy-low",
        "kbps": 123,
        "fake_source": None,
        "close_match": 0,
    }
    fake = upload.File(1, "x.flac", tier="fake", source="~256 kbps / V0", band=20000)
    assert upload.compare(paths, fake, opus).better
    assert upload.compare(
        paths, upload.File(1, "x.flac", tier="fake", source="~256 kbps / V0", band=19400), opus
    ).better
    assert not upload.compare(paths, upload.File(1, "x.flac", tier="fake", source="~128 kbps", band=16000), opus).better
    assert not upload.compare(
        paths, upload.File(1, "x.flac", tier="fake", source="~256 kbps / V0", band=18500), opus
    ).better
    assert upload.compare(paths, upload.File(1, "x.mp3", tier="lossy-high", kbps=320), opus).better
    assert not upload.compare(paths, upload.File(1, "x.mp3", tier="lossy-mid", kbps=150), opus).better  # barely more
    fake_copy = opus | {"quality": "fake", "kbps": 1100, "fake_source": "~128 kbps"}
    assert upload.compare(paths, upload.File(1, "x.mp3", tier="lossy-high", kbps=320), fake_copy).better


def test_a_better_copy_replaces_yours(settings: Settings, login: Callable[..., TestClient]) -> None:
    """First Song is an MP3 in the library: a FLAC of it uploaded on its list page takes over its name."""
    client = login(create_app(settings))
    assert 'hx-get="/missing/upload?song=spotify%3As1"' in client.get("/lists/spotify:playlist:AAA111").text
    assert "data-force-all hidden" in client.get("/missing/upload").text  # Replace all (shown once one is no better)
    one = [("files", ("Artist A - First Song.flac", (AUDIO / "silence.flac").read_bytes(), "audio/flac"))]
    html = post_files(client, one)
    assert '<input type="hidden" name="song_1" value="spotify:s1">' in html and "you have it" in html
    assert "Better than your" in html
    done = client.post(f"/missing/upload/{batch_of(html)}/import", data={"song_1": "spotify:s1"}).text
    assert "Artist A – First Song: replaced the copy: Artist A/Artist A - First Song.flac" in done
    assert (settings.library_dir / "Artist A" / "Artist A - First Song.flac").is_file()
    assert not (settings.library_dir / "Artist A" / "Artist A - First Song.mp3").exists()  # retired
    con = db.connect(settings.db_path)
    assert (
        con.execute("SELECT file FROM songs WHERE key = 'spotify:s1'").fetchone()[0]
        == "Artist A/Artist A - First Song.flac"
    )
    con.close()


def test_a_close_match_song_gets_its_own_file(settings: Settings, login: Callable[..., TestClient]) -> None:
    """Gone Song covered by a close match (another version): an upload of the song itself is filed under its own
    name, the song is no close match any more; the version stays a file of its own, or goes when ticked."""
    con = db.connect(settings.db_path)
    version = settings.library_dir / "Artist C" / "Artist C - Gone Song (Club Mix).flac"
    version.parent.mkdir(parents=True, exist_ok=True)
    version.write_bytes(b"0123456789")
    with con:
        con.execute(
            "INSERT INTO files VALUES ('Artist C/Artist C - Gone Song (Club Mix).flac', 10, 0, 300, 900, 'lossless')"
        )
        con.execute("""UPDATE songs SET file = 'Artist C/Artist C - Gone Song (Club Mix).flac', close_match = 1,
                       link = '["Artist C", "Gone Song (Club Mix)", 300]' WHERE key = 'spotify:s3'""")
    con.close()
    client = login(create_app(settings))
    one = [("files", ("Artist C - Gone Song.flac", (AUDIO / "silence.flac").read_bytes(), "audio/flac"))]
    html = post_files(client, one)
    assert "covered by a close match" in html and 'name="remove_1"' in html and "Not better" not in html
    done = client.post(f"/missing/upload/{batch_of(html)}/import", data={"song_1": "spotify:s3", "remove_1": "1"}).text
    assert "Artist C – Gone Song: filed as Artist C/Artist C - Gone Song.flac" in done
    assert "Gone Song (Club Mix).flac: removed" in done and not version.exists()
    con = db.connect(settings.db_path)
    row = con.execute("SELECT file, close_match FROM songs WHERE key = 'spotify:s3'").fetchone()
    assert tuple(row) == ("Artist C/Artist C - Gone Song.flac", 0)  # its playlists play the song itself now
    con.close()


def test_remove_a_close_match(settings: Settings, login: Callable[..., TestClient]) -> None:
    con = db.connect(settings.db_path)
    with con:
        con.execute(
            "UPDATE songs SET close_match = 1, link = '[\"Artist A\", \"First Song\", 201]' WHERE key = 'spotify:s1'"
        )
    con.close()
    client = login(create_app(settings))
    page = client.get("/missing").text
    assert 'action="/songs/spotify:s1/close-remove"' in page
    r = client.post("/songs/spotify:s1/close-remove", follow_redirects=False)
    assert "removed" in r.headers["location"]
    assert not (settings.library_dir / "Artist A" / "Artist A - First Song.mp3").exists()
    con = db.connect(settings.db_path)
    assert tuple(con.execute("SELECT file, close_match, link FROM songs WHERE key = 'spotify:s1'").fetchone()) == (
        None,
        0,
        None,
    )
    con.close()


def test_files_added_one_by_one(tmp_path: Path) -> None:
    """A batch opened empty takes files one at a time, each with the next number; a cancelled one takes none."""
    paths = Paths(tmp_path)
    batch = upload.start(paths)
    import io

    first = upload.add(paths, batch, "a.flac", io.BytesIO((AUDIO / "silence.flac").read_bytes()))
    second = upload.add(paths, batch, "b.txt", io.BytesIO(b"not audio"))
    assert (first.n, second.n) == (1, 2) and not first.error and second.error
    assert [f.n for f in upload.files(paths, batch)] == [1, 2]
    upload.cancel(paths, batch)
    with pytest.raises(ValueError):
        upload.add(paths, batch, "c.flac", io.BytesIO(b"x"))
