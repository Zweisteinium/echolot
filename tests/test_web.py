import json

import pytest
import yaml
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


def test_availability_page(client: TestClient) -> None:
    html = client.get("/availability").text
    assert "No probes yet" in html and "By hour as a table" in html


def test_chart_geometry() -> None:
    from echolot.web import charts

    rows = [{"hour": h, "users": (3.0 if h == 20 else None), "runs": 1} for h in range(24)]
    c = charts.hours(rows, "users", "users")
    assert not c.empty and len(c.bars) == 24
    bar = c.bars[20]
    assert bar.path and bar.width <= charts.BAR and bar.y < c.baseline
    assert "20:00–20:59 · 3.0 users" in bar.tooltip
    assert [t[1] for t in c.ticks] == ["0", "2.5", "5"]
    assert charts.nice_max(0) == 1 and charts.nice_max(7) == 10 and charts.nice_max(120) == 200


def test_review(client: TestClient, settings: Settings) -> None:
    root, music = settings.pipeline_dir, settings.library_dir.parent
    kept = music / "inbox" / "review" / "2026-09-27" / "Artist C - Gone Song [soulseek].flac"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"audio")
    other = kept.with_name("Artist C - Gone Song [soulseek] (2).flac")
    other.write_bytes(b"audio")
    new = [
        {"ts": "2026-09-27T10:00:00", "action": "new", "path": "Artist A/Artist A - First Song.mp3",
         "ext": "mp3", "seconds": 202, "source": "youtube", "ids": ["spotify:s1"], "artist": "Artist A",
         "title": "First Song", "match": "probable", "found": "First Song (Official Video)", "tries": 2},
        {"ts": "2026-09-27T11:00:00", "action": "wrong-song",
         "path": "/music/inbox/review/2026-09-27/Artist C - Gone Song [soulseek].flac", "ext": "flac",
         "seconds": 181, "source": "soulseek", "ids": ["spotify:s3"], "artist": "Artist C",
         "title": "Gone Song", "found": "Gone Song (Club Mix)", "reason": "title differs"},
        {"ts": "2026-09-27T11:30:00", "action": "wrong-song",
         "path": "/music/inbox/review/2026-09-27/Artist C - Gone Song [soulseek] (2).flac",
         "seconds": 180, "ids": ["spotify:s3"], "artist": "Artist C", "title": "Gone Song",
         "found": "Requiem in D minor", "reason": "artist 'Artist C' not in ['Mozart']"},
        {"ts": "2026-09-27T12:00:00", "action": "wrong-song", "path": "/etc/passwd",
         "ids": ["spotify:s3"], "artist": "Artist C", "title": "Gone Song"},
    ]  # fmt: skip
    with (root / "logs" / "downloads.jsonl").open("a") as f:
        f.write("".join(json.dumps(e) + "\n" for e in new))
    con = db.connect(settings.db_path)
    jobs.refresh(settings, con)
    ids = [r[0] for r in con.execute("SELECT id FROM events WHERE ts >= '2026-09-27' ORDER BY id")]
    con.close()

    html = client.get("/review").text
    assert "First Song (Official Video)" in html and "(+2 s)" in html
    assert "Gone Song (Club Mix)" in html and "title differs" in html
    assert "/etc/passwd" not in html
    assert "Requiem in D minor" not in html  # another artist: no near miss
    assert client.get(f"/review/{ids[1]}/audio").content == b"audio"
    assert client.get(f"/review/{ids[2]}/audio").status_code == 404

    # a decision the download does not allow, then the right ones
    r = client.post(f"/review/{ids[0]}", data={"decision": "accept"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    client.post(f"/review/{ids[0]}", data={"decision": "wrong"})
    client.post(f"/review/{ids[1]}", data={"decision": "accept"})
    saved = yaml.safe_load((root / "review.yml").read_text())["decisions"]
    assert [(d["decision"], d["song"]) for d in saved] == [
        ("wrong", "spotify:s1"),
        ("accept", "spotify:s3"),
    ]
    assert saved[1]["path"].startswith("/music/inbox/review/") and saved[0]["length"] == 200
    assert "applied soon" in client.get("/review").text

    # applied by the pipeline: no longer listed
    (root / "state" / "review-done.json").write_text(json.dumps({d["id"]: {} for d in saved}))
    html = client.get("/review").text
    assert "Nothing to check." in html and "Nothing kept." in html


def test_review_upgrade_candidate_and_revert(client: TestClient, settings: Settings) -> None:
    """A genuine FLAC of a song the library has as MP3 (the FLAC upgrade), logged with Sockseek's
    spotify:track: URI; a decision can be taken back until the pipeline applies it."""
    root, music = settings.pipeline_dir, settings.library_dir.parent
    kept = music / "inbox" / "review" / "2026-09-28" / "Artist A - First Song [soulseek].flac"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"audio")
    event = {"ts": "2026-09-28T14:00:00", "action": "wrong-song", "path": "/music/inbox/review/2026-09-28/"
             "Artist A - First Song [soulseek].flac", "ext": "flac", "seconds": 200, "source": "soulseek",
             "ids": ["spotify:track:s1"], "artist": "Artist A", "title": "First Song", "found": "01 First Song",
             "reason": "title differs"}  # fmt: skip
    with (root / "logs" / "downloads.jsonl").open("a") as f:
        f.write(json.dumps(event) + "\n")
    con = db.connect(settings.db_path)
    jobs.refresh(settings, con)
    (eid,) = con.execute("SELECT id FROM events WHERE ts = '2026-09-28T14:00:00'").fetchone()
    assert con.execute("SELECT song FROM events WHERE id = ?", (eid,)).fetchone()[0] == "spotify:s1"
    con.close()

    html = client.get("/review").text
    assert "01 First Song" in html and "would replace mp3 in the library" in html
    client.post(f"/review/{eid}", data={"decision": "accept"})
    assert "Revert" in client.get("/review").text
    r = client.post(f"/review/{eid}/revert", follow_redirects=False)
    assert "ok=" in r.headers["location"]
    assert yaml.safe_load((root / "review.yml").read_text())["decisions"] == []
    # applied already: no revert
    client.post(f"/review/{eid}", data={"decision": "accept"})
    saved = yaml.safe_load((root / "review.yml").read_text())["decisions"]
    (root / "state" / "review-done.json").write_text(json.dumps({saved[0]["id"]: {}}))
    r = client.post(f"/review/{eid}/revert", follow_redirects=False)
    assert "error=" in r.headers["location"]
