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
from echolot.web import create_app

AUDIO = Path(__file__).parent / "fixtures" / "audio"


@pytest.fixture(autouse=True)
def no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check before a file is shown (audio.prepare runs ffmpeg): audio passes as it is, text does not."""

    def prepare(path: Path) -> audio.Prepared:
        if path.suffix == ".txt":
            path.unlink()
            raise audio.Rejected("codec 'none' in a .txt file")
        return audio.Prepared(path, False, {"verdict": "ok"})

    monkeypatch.setattr(audio, "prepare", prepare)


def batch_of(html: str) -> str:
    return re.search(r'data-batch="([0-9a-f]{16})"', html).group(1)


def test_upload_check_and_import(settings: Settings, login: Callable[..., TestClient]) -> None:
    client = login(create_app(settings))
    page = client.get("/missing").text
    assert "Upload files" in page and 'hx-get="/missing/upload?song=spotify%3As3"' in page
    assert "Choose files" in client.get("/missing/upload").text
    files = [
        ("files", ("Artist C - Gone Song.flac", (AUDIO / "silence.flac").read_bytes(), "audio/flac")),
        ("files", ("notes.txt", b"not audio", "text/plain")),
    ]
    html = client.post("/missing/upload", files=files).text
    batch = batch_of(html)
    assert '<option value="spotify:s3" selected>' in html  # guessed by its name
    assert "not audio Echolot can use" in html and "Import 2 files?" in html
    assert re.search(r"q-lossless", html)  # its quality, for information
    row = client.get(f"/missing/upload/{batch}/1", params={"song_1": ""}).text
    assert "no song chosen" in row and "selected>" not in row.replace('value="" ', "")
    done = client.post(f"/missing/upload/{batch}/import", data={"song_1": "spotify:s3"}).text
    assert "Artist C – Gone Song: filed as Artist C/Artist C - Gone Song.flac" in done
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
    batch = batch_of(client.post("/missing/upload", files=one, data={"song": "spotify:s3"}).text)
    assert (
        client.post(f"/missing/upload/{batch}/import", data={"song_1": "spotify:s1"}).status_code == 403
    )  # not missing
    assert client.post(f"/missing/upload/{batch}/cancel").status_code == 200
    assert not (filing.Paths(settings.library_dir.parent).inbox("upload") / batch).exists()
    viewer = login(app, "timon", admin=False)
    assert viewer.get("/missing/upload").status_code == 403  # no review permission
    assert "Upload files" not in viewer.get("/missing").text


def test_guess_by_title_and_length() -> None:
    songs = [
        {"key": "a", "artist": "Hurts", "title": "2 More - Radio Edit", "length": 211},
        {"key": "b", "artist": "Hurts", "title": "Wonderful Life", "length": 220},
    ]
    f = upload.File(1, "02 - 2 More.flac", seconds=211.2, artist="Hurts", title="2 More - Radio Edit")
    assert upload.guess(f, songs) == "a"
    assert upload.guess(upload.File(2, "track01.flac", seconds=100), songs) is None
    other = upload.File(3, "Passenger - 2 More.flac", seconds=211, artist="Passenger", title="2 More")
    assert upload.guess(other, songs) is None  # another artist's song of that title
