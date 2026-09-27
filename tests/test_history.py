from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from echolot import db, history, jobs
from echolot.config import Settings
from echolot.web import create_app


@pytest.fixture
def con(settings: Settings):
    con = db.connect(settings.db_path)
    jobs.refresh(settings, con)  # stores the first snapshot
    yield con
    con.close()


def test_snapshot_values(con) -> None:
    ts, m = history.latest(con)
    assert ts
    assert m["library_files"] == {"": 3}  # the .txt file is no audio
    assert m["library_files_by_format"] == {"flac": 1, "m4a": 1, "mp3": 1}
    assert m["songs_wanted"] == {"soundcloud": 2, "spotify": 3}
    assert m["songs_missing"] == {"soundcloud": 1, "spotify": 1}
    # Gone Song: greyed out on Spotify (searched 3 times too); Locked: DRM
    assert m["songs_missing_by_reason"] == {"not_found": 0, "unavailable": 2, "waiting": 0}
    assert m["songs_not_found_by_tries"]["2-3"] == 1
    assert m["list_songs"]["spotify:likes"] == 3 and m["list_in_library"]["spotify:likes"] == 2


def test_snapshot_at_most_hourly_and_thinned(con) -> None:
    now = datetime.now()
    assert not history.snapshot(con, now + timedelta(minutes=30))
    assert history.snapshot(con, now + timedelta(minutes=59, seconds=30))
    # old hourly snapshots keep only the first of each day
    old = now - timedelta(days=200)
    for hour in (3, 4, 5):
        history.snapshot(con, old.replace(hour=hour, minute=0), force=True)
    history.snapshot(con, now + timedelta(hours=2))
    cutoff = (now - timedelta(days=100)).isoformat()
    days = con.execute("SELECT DISTINCT ts FROM snapshots WHERE ts < ?", (cutoff,))
    assert [r[0][11:13] for r in days] == ["03"]


def test_series(con) -> None:
    history.snapshot(con, datetime.now() + timedelta(hours=1))
    rows = history.series(con, "songs_missing", "spotify")
    assert [r["value"] for r in rows] == [1, 1]
    assert history.series(con, "songs_missing", since="2999-01-01") == []


def test_api(con, settings: Settings) -> None:
    client = TestClient(create_app(settings))
    assert client.get("/api/stats").json()["metrics"]["library_files"] == {"": 3}
    assert client.get("/api/stats/metrics").json()["songs_by_quality"]["label"] == "quality"
    rows = client.get("/api/stats/history", params={"metric": "songs_wanted"}).json()
    assert {r["key"] for r in rows} == {"spotify", "soundcloud"} and rows[0]["time"] > 0
    assert client.get("/api/stats/history", params={"metric": "nope"}).status_code == 404
    downloads = client.get("/api/stats/downloads", params={"days": 3650}).json()
    assert {"day", "action", "source", "format", "count", "bytes"} <= downloads[0].keys()
    assert client.get("/api/stats/availability").status_code == 200


def test_prometheus(con, settings: Settings) -> None:
    text = TestClient(create_app(settings)).get("/metrics").text
    assert "# TYPE echolot_library_files gauge" in text
    assert "echolot_library_files 3\n" in text
    assert 'echolot_songs_missing{service="spotify"} 1\n' in text
    assert 'echolot_events_total{action="wrong-song",source="soulseek"} 1\n' in text
    assert 'echolot_list_songs{list="spotify:likes"} 3\n' in text
