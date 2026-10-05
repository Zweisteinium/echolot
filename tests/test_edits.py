"""A YouTube song named as a library song of another length (an upload is often a shorter edit): the same
recording by its audio is linked to that file, nothing filed twice; probably the same goes to review
(library/recordings.another_edit, merge_edits; jobs/lists._on_spotify finds songs without "feat.")."""

import json
from pathlib import Path

import pytest

from echolot import db
from echolot.jobs import lists
from echolot.library import catalog, identity, recordings
from echolot.library.filing import Paths, Want


@pytest.fixture
def env(tmp_path: Path):
    paths = Paths(tmp_path / "music")
    db.init(tmp_path / "echolot.db")
    con = db.connect(tmp_path / "echolot.db")
    for rel, seconds in (("JJD/JJD - Adventure.flac", 298), ("JJD/JJD - Adventure (4m39s).opus", 279)):
        p = paths.tracks / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"audio")
        with con:
            con.execute("INSERT INTO files VALUES (?, 5, 0, ?, 900, NULL)", (rel, seconds))
    song = "INSERT INTO songs (key, service, artist, title, length, file) VALUES (?, ?, 'JJD', 'Adventure', ?, ?)"
    with con:
        con.execute(song, ("spotify:a", "spotify", 298, "JJD/JJD - Adventure.flac"))
        con.execute(song, ("youtube:v", "youtube", 279, "JJD/JJD - Adventure (4m39s).opus"))
        con.execute("INSERT INTO lists (key, service, title, position) VALUES ('L', 'youtube', 'NCS', 0)")
        con.execute("INSERT INTO list_songs VALUES ('L', 0, 'youtube:v')")
    yield con, paths
    con.close()


def test_other_edits_by_name_any_length(env) -> None:
    con, _ = env
    cat = catalog.Catalog.from_db(con)
    want = Want("JJD", "Adventure", 279, "youtube:v")
    assert [e.path for e in recordings.other_edits(cat, want, 279, "JJD/JJD - Adventure (4m39s).opus")] == [
        "JJD/JJD - Adventure.flac"
    ]
    assert recordings.other_edits(cat, want, 297, "JJD/JJD - Adventure (4m39s).opus") == []  # the same length
    with con:  # a collaboration filed under its other artist: found through its song
        con.execute("INSERT INTO files VALUES ('Distrion/Distrion - Rubik.flac', 5, 0, 214, 900, NULL)")
        con.execute(
            "INSERT INTO songs (key, service, artist, title, length, artists, file) VALUES "
            "('spotify:r', 'spotify', 'Electro-Light', 'Rubik', 214, '[\"Electro-Light\", \"Distrion\"]', 'Distrion/Distrion - Rubik.flac')"
        )
    rubik = Want("Electro-Light", "Rubik", 201, "youtube:r")
    cat = catalog.Catalog.from_db(con)
    assert recordings.other_edits(cat, rubik, 201) == []
    assert [e.path for e in recordings.other_edits(cat, rubik, 201, con=con)] == ["Distrion/Distrion - Rubik.flac"]


def test_merge_links_a_sure_edit_and_retires_the_copy(env, monkeypatch: pytest.MonkeyPatch) -> None:
    con, paths = env
    calls = []
    monkeypatch.setattr(identity, "alike", lambda a, b: calls.append((a, b)) or 0.93)
    assert recordings.merge_edits(con, paths) == ["JJD - Adventure -> JJD/JJD - Adventure.flac"]
    link = json.loads(con.execute("SELECT link FROM songs WHERE key = 'youtube:v'").fetchone()[0])
    assert link == ["JJD", "Adventure", 298]  # the song is the Spotify song's file now
    assert not (paths.tracks / "JJD/JJD - Adventure (4m39s).opus").exists()  # its own copy retired
    assert list(paths.inbox("replaced").rglob("*.opus"))
    catalog.match_songs(con)
    assert con.execute("SELECT file FROM songs WHERE key = 'youtube:v'").fetchone()[0] == "JJD/JJD - Adventure.flac"
    assert recordings.merge_edits(con, paths) == [] and len(calls) == 1  # each pair compared once


def test_merge_puts_a_probable_edit_up_for_review(env, monkeypatch: pytest.MonkeyPatch) -> None:
    con, paths = env
    monkeypatch.setattr(identity, "alike", lambda a, b: 0.87)
    assert recordings.merge_edits(con, paths) == ["JJD - Adventure: in review (0.87)"]
    reason = con.execute("SELECT reason FROM events WHERE action = 'recheck'").fetchone()[0]
    assert "in another edit (audio 0.87)" in reason
    assert (paths.tracks / "JJD/JJD - Adventure (4m39s).opus").exists()  # nothing retired
    monkeypatch.setattr(identity, "alike", lambda a, b: 0.5)
    con.execute("UPDATE songs SET file = 'JJD/JJD - Adventure (4m39s).opus' WHERE key = 'youtube:v'")
    assert (
        recordings.another_edit(
            paths,
            paths.tracks / "JJD/JJD - Adventure (4m39s).opus",
            Want.of(con.execute("SELECT * FROM songs WHERE key = 'youtube:v'").fetchone()),
            catalog.Catalog.from_db(con),
            "JJD/JJD - Adventure (4m39s).opus",
        )
        is None
    )  # another song: nothing


def test_edit_reason_round_trip() -> None:
    reason = recordings.EDIT_REASON.format(path="JJD/JJD - Adventure.flac", share=0.874)
    assert recordings.EDIT_RE.fullmatch(reason)["path"] == "JJD/JJD - Adventure.flac"


def test_spotify_is_asked_without_featured_artists(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = []

    class Spotify:
        def search(self, q: str, limit: int = 5) -> list[dict]:
            asked.append(q)
            if q == "Warriyo Mortals":
                return [{"name": "Mortals", "artists": [{"name": "Warriyo"}], "duration_ms": 228000}]
            return []

    monkeypatch.setattr(lists.time, "sleep", lambda s: None)
    t = {"title": "Warriyo - Mortals (feat. Laura Brehm)", "artists": ["Warriyo"], "length": 230, "kind": "omv"}
    hit = lists._on_spotify(Spotify(), t, ["Warriyo"], "Mortals (feat. Laura Brehm)")
    assert hit and hit["name"] == "Mortals" and "Warriyo Mortals" in asked
