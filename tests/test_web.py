import pytest
from fastapi.testclient import TestClient

from echolot import __version__, db, jobs, schedule
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


def test_sources_page_and_add(client: TestClient, settings: Settings) -> None:
    html = client.get("/sources").text
    assert "Playlist A" in html and "Renamed" in html
    version = html.split('name="version" value="')[1].split('"')[0]
    response = client.post(
        "/sources/add",
        data={"version": version, "url": "https://open.spotify.com/playlist/NEW1", "playlist": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and "ok=" in response.headers["location"]
    assert "playlist/NEW1" in (settings.pipeline_dir / "sources.yml").read_text()
    stale = client.post(
        "/sources/remove", data={"version": version, "key": "spotify:playlist:NEW1"}
    )
    assert "changed elsewhere" in stale.text


def test_sources_yaml_keeps_invalid_edit(client: TestClient, settings: Settings) -> None:
    html = client.get("/sources/yaml").text
    version = html.split('name="version" value="')[1].split('"')[0]
    response = client.post("/sources/yaml", data={"version": version, "text": "spotfy: {}\n"})
    assert response.status_code == 400
    assert "Unknown setting" in response.text and "spotfy: {}" in response.text
    before = (settings.pipeline_dir / "sources.yml").read_text()
    new = before + "\n# note\n"
    ok = client.post("/sources/yaml", data={"version": version, "text": new})
    assert ok.status_code == 200 and (settings.pipeline_dir / "sources.yml").read_text() == new
    assert "before: edited as YAML" in ok.text


def test_settings_save(client: TestClient, settings: Settings) -> None:
    assert "Spotify → Soulseek" in client.get("/settings").text
    form = {j.name: schedule.when_text(j.default) for j in schedule.JOBS} | {
        "sync": "20",
        "fallback": "0",
        "upgrade": "13:00; sat 10:00",
    }
    response = client.post("/settings", data=form | {"refresh": "7"})
    assert "Settings saved" in response.text
    assert schedule.read(settings.pipeline_dir)["sync"] == 20
    assert schedule.read(settings.pipeline_dir)["fallback"] is None
    assert schedule.read(settings.pipeline_dir)["upgrade"] == ["13:00", "sat 10:00"]
    assert 'value="7"' in client.get("/settings").text
    bad = client.post("/settings", data=form | {"sync": "2", "refresh": "5"})
    assert "at least 10" in bad.text
