from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from echolot import db
from echolot.config import Settings
from echolot.library import history
from echolot.web import create_app


@pytest.fixture
def con(settings: Settings):
    con = db.connect(settings.db_path)  # the first snapshot is stored
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
    assert m["list_songs"]["spotify:likes:1"] == 3 and m["list_in_library"]["spotify:likes:1"] == 2


def test_snapshot_at_most_hourly_and_thinned(con) -> None:
    now = datetime.now()
    assert not history.snapshot(con, now + timedelta(minutes=30))
    assert history.snapshot(con, now + timedelta(minutes=59, seconds=30))
    # old hourly snapshots keep only the last of each (UTC) day: what the day ended with
    old = datetime.now(UTC) - timedelta(days=200)
    for hour in (3, 4, 5):
        history.snapshot(con, old.replace(hour=hour, minute=0, second=0), force=True)
    history.snapshot(con, now + timedelta(hours=2))
    days = con.execute("SELECT DISTINCT ts FROM snapshots WHERE ts < ?", (history.utc(now - timedelta(days=100)),))
    assert [r[0][11:] for r in days] == ["05:00:00Z"]


def test_times_are_utc() -> None:
    assert history.utc(datetime(2026, 10, 25, 1, 30, tzinfo=UTC)) == "2026-10-25T01:30:00Z"
    assert history.utc("2026-10-25T03:30:00+02:00") == "2026-10-25T01:30:00Z"
    local = datetime(2026, 7, 1, 12, 0)  # no zone: local time
    assert history.utc(local) == history.utc(local.astimezone())
    assert history.utc("2026-07-01") == history.utc(datetime(2026, 7, 1))


def local(t: str) -> str:
    """A UTC time as the events have it: local time without a zone."""
    return datetime.fromisoformat(t).astimezone().replace(tzinfo=None).isoformat()


def test_rebuild_fills_in_the_time_before_the_first_snapshot(con) -> None:
    """The library's files from the events (exact), its size near, songs in proportion to the files; once."""
    first = "2026-09-27T12:30:00Z"
    sql = "INSERT INTO events (ts, action, path, bytes) VALUES (?, ?, 'x', ?)"
    with con:  # (the fixture's events are out of the way)
        con.execute("UPDATE snapshots SET ts = ?", (first,))
        con.execute("UPDATE snapshots SET value = 1000 WHERE metric = 'library_bytes'")
        con.execute("DELETE FROM events WHERE action IN ('new', 'upgrade', 'retired')")
        con.execute(sql, (local("2026-09-27T07:40:00+00:00"), "new", 100))
        con.execute(sql, (local("2026-09-27T09:40:00+00:00"), "new", 100))
        con.execute(sql, (local("2026-09-27T10:40:00+00:00"), "retired", 50))
    assert history.rebuild(con) == 7  # 06:00 (before the first event) to 12:00
    files = history.totals(con, "library_files")
    assert [v for _, v in files] == [2, 2, 3, 3, 4, 3, 3, 3] and files[-1][0] == first  # 3 files at the snapshot
    assert files[0][0] == "2026-09-27T06:00:00Z"
    size = dict(history.totals(con, "library_bytes"))
    assert size[first] - size["2026-09-27T06:00:00Z"] == 150  # 2 x 100 in, 50 out
    songs = dict(history.totals(con, "songs_in_library"))
    assert songs["2026-09-27T06:00:00Z"] == round(songs[first] * 2 / 3)  # in proportion to the files
    assert history.rebuild(con) == 0 and db.get_meta(con, history.REBUILT) == first  # once


def test_rebuild_without_events_before_the_first_snapshot(con) -> None:
    with con:
        con.execute("DELETE FROM events")
    assert history.rebuild(con) == 0 and db.get_meta(con, history.REBUILT)
    assert len(history.totals(con, "library_files")) == 1


def test_series(con) -> None:
    history.snapshot(con, datetime.now() + timedelta(hours=1))
    rows = history.series(con, "songs_missing", "spotify")
    assert [r["value"] for r in rows] == [1, 1]
    assert history.series(con, "songs_missing", since="2999-01-01") == []


def test_api(con, settings: Settings, login) -> None:
    client = login(create_app(settings))
    assert client.get("/api/stats").json()["metrics"]["library_files"] == {"": 3}
    assert client.get("/api/stats/metrics").json()["songs_by_quality"]["label"] == "quality"
    rows = client.get("/api/stats/history", params={"metric": "songs_wanted"}).json()
    assert {r["key"] for r in rows} == {"spotify", "soundcloud"} and rows[0]["time"] > 0
    assert client.get("/api/stats/history", params={"metric": "nope"}).status_code == 404
    downloads = client.get("/api/stats/downloads", params={"days": 3650}).json()
    assert {"day", "action", "source", "format", "count", "bytes"} <= downloads[0].keys()


def test_prometheus(con, settings: Settings) -> None:
    text = TestClient(create_app(settings)).get("/metrics").text
    assert "# TYPE echolot_library_files gauge" in text
    assert "echolot_library_files 3\n" in text
    assert 'echolot_songs_missing{service="spotify"} 1\n' in text
    assert 'echolot_events_total{action="wrong-song",source="soulseek"} 1\n' in text
    assert 'echolot_list_songs{list="spotify:likes:1"} 3\n' in text
