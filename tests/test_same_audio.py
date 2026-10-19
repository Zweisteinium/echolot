"""Songs of other names whose files are the same audio keep one file, the better one, whichever came first
(library/recordings.merge_same); a fingerprint alone does not decide (identity.same_master)."""

from pathlib import Path

import numpy as np
import pytest

from echolot import db
from echolot.library import catalog, identity, recordings
from echolot.library.filing import Paths

LOSSY = "MC WINDHUND/MC WINDHUND - USCHI (DJ WASSERFALL TRANCE EDIT).m4a"
FLAC = "MC WINDHUND/MC WINDHUND - Uschi - Trance Edit.flac"


@pytest.fixture
def env(tmp_path: Path):
    paths = Paths(tmp_path / "music")
    db.init(tmp_path / "echolot.db")
    con = db.connect(tmp_path / "echolot.db")
    for rel, seconds, kbps in ((LOSSY, 250.9, 160), (FLAC, 250.86, 958)):
        add_file(con, paths, rel, seconds, kbps)
    song = "INSERT INTO songs (key, service, artist, title, length, file) VALUES (?, ?, 'MC WINDHUND', ?, ?, ?)"
    with con:
        con.execute(song, ("soundcloud:1", "soundcloud", "USCHI (DJ WASSERFALL TRANCE EDIT)", 250.9, LOSSY))
        con.execute(song, ("spotify:u", "spotify", "Uschi - Trance Edit", 251, FLAC))
    yield con, paths
    con.close()


def add_file(con, paths: Paths, rel: str, seconds: float, kbps: int) -> None:
    p = paths.tracks / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"audio")
    with con:
        con.execute("INSERT INTO files VALUES (?, 5, 0, ?, ?, NULL)", (rel, seconds, kbps))


def file_of(con, key: str) -> str | None:
    return con.execute("SELECT file FROM songs WHERE key = ?", (key,)).fetchone()[0]


def test_the_same_audio_keeps_the_better_file_for_both_songs(env, monkeypatch: pytest.MonkeyPatch) -> None:
    con, paths = env
    monkeypatch.setattr(identity, "same_master", lambda a, b: 0.993)
    assert recordings.merge_same(con, paths) == [f"{LOSSY} -> {FLAC}"]
    catalog.match_songs(con)
    assert file_of(con, "soundcloud:1") == file_of(con, "spotify:u") == FLAC
    assert not (paths.tracks / LOSSY).exists() and (paths.tracks / FLAC).exists()
    assert list(paths.inbox("replaced").rglob("*.m4a"))
    linked = con.execute("SELECT reason FROM events WHERE action = 'linked' AND song = 'soundcloud:1'").fetchone()
    assert "the same audio, 0.993" in linked[0]


def test_a_remix_sharing_the_vocals_is_kept_and_compared_once(env, monkeypatch: pytest.MonkeyPatch) -> None:
    con, paths = env
    calls = []
    monkeypatch.setattr(identity, "same_master", lambda a, b: calls.append((a, b)) or 0.136)
    assert recordings.merge_same(con, paths) == [] and recordings.merge_same(con, paths) == []
    assert len(calls) == 1 and (paths.tracks / LOSSY).exists()
    with con:
        con.execute("UPDATE files SET size = 6 WHERE path = ?", (LOSSY,))
    (paths.tracks / LOSSY).write_bytes(b"audio2")
    recordings.merge_same(con, paths)
    assert len(calls) == 2  # a changed file is compared again


def test_close_matches_mix_cuts_and_far_lengths_are_left_alone(env, monkeypatch: pytest.MonkeyPatch) -> None:
    con, paths = env
    monkeypatch.setattr(identity, "same_master", lambda a, b: 1.0)
    with con:
        con.execute("UPDATE songs SET close_match = 1 WHERE key = 'soundcloud:1'")
    assert recordings.merge_same(con, paths) == []
    with con:
        con.execute("UPDATE songs SET close_match = 0 WHERE key = 'soundcloud:1'")
        con.execute("UPDATE files SET duration = 255 WHERE path = ?", (LOSSY,))
    assert recordings.merge_same(con, paths) == []
    assert (paths.tracks / LOSSY).exists()


def test_a_fake_flac_gives_way_to_the_lossy_file(env, monkeypatch: pytest.MonkeyPatch) -> None:
    con, paths = env
    monkeypatch.setattr(identity, "same_master", lambda a, b: 0.999)
    with con:
        con.execute("INSERT INTO lossy_sourced (stem, master) VALUES (?, 0)", (FLAC.rpartition(".")[0],))
    assert recordings.merge_same(con, paths) == [f"{FLAC} -> {LOSSY}"]
    catalog.match_songs(con)
    assert file_of(con, "spotify:u") == file_of(con, "soundcloud:1") == LOSSY


def test_a_file_changed_meanwhile_is_not_retired(env, monkeypatch: pytest.MonkeyPatch) -> None:
    con, paths = env
    monkeypatch.setattr(identity, "same_master", lambda a, b: 0.993)
    (paths.tracks / LOSSY).write_bytes(b"other audio")
    assert recordings.merge_same(con, paths) == []
    assert (paths.tracks / LOSSY).exists() and file_of(con, "soundcloud:1") == LOSSY


def test_same_master_of_nothing_but_silence_is_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(identity, "fingerprint", lambda p: np.ones(200, dtype=np.uint32))
    monkeypatch.setattr(identity, "_pcm", lambda p: np.zeros(30 * identity._RATE, dtype="<f4"))
    assert identity.same_master(tmp_path / "a.flac", tmp_path / "b.flac") == 0.0
