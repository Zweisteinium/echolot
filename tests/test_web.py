import pytest
from fastapi.testclient import TestClient

from echolot import __version__, db, jobs
from echolot.config import Settings
from echolot.web import create_app


@pytest.fixture
def client(settings: Settings) -> TestClient:
    con = db.connect(settings.db_path)
    jobs.refresh(settings, con)
    con.close()
    return TestClient(create_app(settings))  # no `with`: the scheduler thread stays off


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_overview(client: TestClient) -> None:
    html = client.get("/").text
    assert "Playlist A" in html
    assert 'href="/lists/spotify:playlist:BBB222"' in html
    assert "no playlist" in html
    assert "Refresh now" in html


def test_missing(client: TestClient) -> None:
    html = client.get("/missing").text
    assert "Gone Song" in html and "Locked" in html
    assert "greyed out on Spotify" in html
    html = client.get("/missing", params={"list": "soundcloud:someone/likes"}).text
    assert "Locked" in html and "Gone Song" not in html


def test_list_page(client: TestClient) -> None:
    html = client.get("/lists/soundcloud:someone/sets/trance").text
    assert "Trance Tune" in html
    assert client.get("/lists/nope").status_code == 404


def test_activity(client: TestClient) -> None:
    assert "Artist A - First Song.mp3" in client.get("/activity").text
    html = client.get("/activity", params={"kind": "rejected"}).text
    assert "Gone Song" in html and "First Song" not in html


def test_trigger_job(client: TestClient) -> None:
    response = client.post("/jobs/refresh/run", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/")
    assert client.post("/jobs/nope/run").status_code == 404


def test_static_stylesheet(client: TestClient) -> None:
    assert client.get("/static/style.css").status_code == 200
