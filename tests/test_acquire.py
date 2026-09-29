"""Getting songs through the Soulseek daemon (acquire.py), against a fake daemon, with real filing."""

import threading
import wave
from pathlib import Path
from typing import ClassVar

import pytest

from echolot import acquire, audio, db, options, soulseek, vault
from echolot.config import Settings
from echolot.schedule import BY_NAME
from echolot.worker import Run


def wav(path: Path, seconds: int = 200) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(100)
        w.writeframes(b"\x80" * 100 * seconds)


class FakeDaemon:
    """Search results by query title: [(user, path, seconds, behaviour)]; behaviour: ok, stuck, fail."""

    files: ClassVar[dict[str, list[tuple[str, str, int, str]]]] = {}
    searches: ClassVar[list[tuple[str, str, int, dict]]] = []
    downloads: ClassVar[list[str]] = []
    lost = False

    def __init__(self, url: str, timeout: float = 30) -> None:
        self.jobs: dict[str, object] = {}

    ready = True

    def status(self) -> dict:
        return {
            "ready": FakeDaemon.ready,
            "state": "Connected, LoggedIn" if FakeDaemon.ready else "Disconnected",
        }

    def search(self, artist: str, title: str, length: int, settings: dict) -> str:
        FakeDaemon.searches.append((artist, title, length, settings))
        if FakeDaemon.lost:
            raise soulseek.Lost("gone")
        return f"search:{title}"

    def wait(self, job: str, stop: threading.Event, deadline: float, every: float = 1.0) -> dict:
        return {}

    def results(self, job: str) -> list[soulseek.Candidate]:
        found = FakeDaemon.files.get(job.removeprefix("search:"), [])
        return [soulseek.Candidate(u, p, 1, 320, 44100, s, p.rsplit(".", 1)[-1], True, 100, n)
                for n, (u, p, s, _) in enumerate(found)]  # fmt: skip

    def download(
        self, search_job: str, c: soulseek.Candidate, parent_dir: str, settings: dict
    ) -> str:
        FakeDaemon.downloads.append(c.path)
        behaviour = next(
            b for u, p, s, b in FakeDaemon.files[search_job.removeprefix("search:")] if p == c.path
        )
        self.jobs[c.path] = (behaviour, f"{parent_dir}/{c.parts[-1]}", c.length)
        return c.path

    def transfer(self, job: str) -> soulseek.Transfer:
        behaviour, path, seconds = self.jobs[job]
        if behaviour == "stuck":
            return soulseek.Transfer("running", None, 0, 1000, "")
        if behaviour == "fail":
            return soulseek.Transfer("failed", None, 0, 1000, "AllDownloadsFailed")
        wav(
            Path(path), 300 if behaviour == "long" else seconds
        )  # long: the peer's length was wrong
        return soulseek.Transfer("done", path, 1000, 1000, "")

    def cancel(self, job: str) -> None:
        self.jobs[job] = ("fail", "", 0)


@pytest.fixture
def run(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Run:
    FakeDaemon.files, FakeDaemon.searches, FakeDaemon.downloads, FakeDaemon.lost = {}, [], [], False
    FakeDaemon.ready = True
    monkeypatch.setattr(soulseek, "Daemon", FakeDaemon)
    monkeypatch.setattr(
        audio, "prepare", lambda p: audio.Prepared(p, False, None)
    )  # no ffmpeg here
    monkeypatch.setattr(acquire, "finish", lambda *a, **k: None)  # no Spotify pictures
    con = db.connect(settings.db_path)
    with con:
        options.update(
            con, options.Soulseek, daemon_music=str(settings.library_dir.parent), stall_minutes=2
        )
    con.close()
    r = Run(BY_NAME["sync"], settings, vault.Vault.from_env(settings.data_dir, {}), "manual")
    r.stop = threading.Event()
    return r


def missing(run: Run) -> list:
    con = run.connect()
    try:
        return acquire._spotify_missing(con)
    finally:
        con.close()


def test_found_after_skipping_wrong_and_stuck_results(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gone Song (s3, 180 s, searched 3 times: loosened). A remix is never downloaded, a transfer queued
    at the peer is given up, a file whose real length is another version's is rejected, the next one is
    filed."""
    monotonic = iter(range(0, 10**6, 200))  # every look at the clock: 200 s later
    monkeypatch.setattr(acquire.time, "monotonic", lambda: next(monotonic))
    FakeDaemon.files["Gone Song"] = [
        ("u1", "Music\\Artist C\\Gone Song (Club Remix).flac", 180, "ok"),
        ("u2", "Music\\Artist C\\Artist C - Gone Song.flac", 180, "stuck"),
        ("u3", "Music\\Artist C\\Artist C - Gone Song.mp3", 181, "long"),
        ("u4", "Music\\Artist C\\Album\\03 Gone Song.m4a", 180, "ok"),
    ]
    rows = [r for r in missing(run) if r["key"] == "spotify:s3"]
    message = acquire._search(run, rows, "search")
    assert message == "1 of 1 songs: 1 new", message
    assert "Club Remix" not in " ".join(
        FakeDaemon.downloads
    )  # judged by its name, never downloaded
    assert FakeDaemon.downloads == [
        "Music\\Artist C\\Artist C - Gone Song.flac",
        "Music\\Artist C\\Artist C - Gone Song.mp3",
        "Music\\Artist C\\Album\\03 Gone Song.m4a",
    ]
    con = run.connect()
    files = [r[0] for r in con.execute("SELECT path FROM files WHERE path LIKE 'Artist C/%'")]
    rejected = con.execute(
        "SELECT action, wanted_seconds FROM events WHERE action = 'mismatch'"
    ).fetchone()
    con.close()
    assert files == ["Artist C/Artist C - Gone Song.m4a"]
    assert tuple(rejected) == ("mismatch", 180)
    artist, title, length, settings = FakeDaemon.searches[-1]
    assert (artist, title, length) == ("Artist C", "Gone Song", 180)
    assert (
        settings["search"]["desperateSearch"]
        and settings["search"]["necessaryCond"]["strictArtist"]
    )


def test_filed_and_attempts_cleared(run: Run) -> None:
    FakeDaemon.files["Gone Song"] = [("u1", "Music\\Artist C\\Singles\\01 - Gone Song.flac", 180, "fail"),
                                     ("u2", "Music\\Artist C\\Artist C - Gone Song.flac", 180, "ok")]  # fmt: skip
    rows = [r for r in missing(run) if r["key"] == "spotify:s3"]
    assert "1 new" in acquire._search(run, rows, "search")
    con = run.connect()
    assert [r[0] for r in con.execute("SELECT path FROM files WHERE path LIKE 'Artist C/%'")] == [
        "Artist C/Artist C - Gone Song.flac"
    ]
    assert not con.execute("SELECT 1 FROM attempts WHERE song_key = 'spotify:s3'").fetchone()
    assert con.execute("SELECT source, song FROM events ORDER BY id DESC LIMIT 1").fetchone()[
        :
    ] == ("soulseek", "spotify:s3")
    con.close()
    assert not any((run.paths.inbox("soulseek")).iterdir())  # nothing left in the inbox


def test_nothing_found_counts_a_try_and_loosens(run: Run) -> None:
    con = run.connect()
    with con:
        con.execute("UPDATE attempts SET tries = 4 WHERE song_key = 'spotify:s3'")
    con.close()
    rows = [r for r in missing(run) if r["key"] == "spotify:s3"]
    assert "1 not found" in acquire._search(run, rows, "search")
    _, _, _, settings = FakeDaemon.searches[-1]
    assert not settings["search"]["necessaryCond"][
        "strictArtist"
    ]  # 4+ tries: the artist may be missing
    con = run.connect()
    assert (
        con.execute("SELECT tries FROM attempts WHERE song_key = 'spotify:s3'").fetchone()[0] == 5
    )
    con.close()


def test_daemon_restart_counts_no_try(run: Run) -> None:
    FakeDaemon.lost = True
    rows = [r for r in missing(run) if r["key"] == "spotify:s3"]
    assert "1 interrupted" in acquire._search(run, rows, "search")
    con = run.connect()
    assert (
        con.execute("SELECT tries FROM attempts WHERE song_key = 'spotify:s3'").fetchone()[0] == 3
    )
    con.close()


def test_upgrade_replaces_the_lossy_copy(run: Run) -> None:
    """First Song is in the library as MP3: a FLAC search finds a genuine one, which takes over."""
    FakeDaemon.files["First Song"] = [
        ("u1", "Music\\Artist A\\Artist A - First Song.flac", 201, "ok")
    ]
    con = run.connect()
    with con:
        con.execute(
            "UPDATE files SET duration = 201 WHERE path = 'Artist A/Artist A - First Song.mp3'"
        )
    con.close()
    message = acquire.upgrade(run)
    assert "1 upgrade" in message
    _, _, _, settings = FakeDaemon.searches[-1]
    assert settings["search"]["necessaryCond"]["formats"] == {"replace": ["flac"]}
    con = run.connect()
    paths = sorted(r[0] for r in con.execute("SELECT path FROM files WHERE path LIKE 'Artist A/%'"))
    con.close()
    assert paths == ["Artist A/Artist A - First Song.flac"]
    assert list(run.paths.inbox("replaced").rglob("*.mp3"))


def test_levels_and_due() -> None:
    assert acquire.level(0) == (False, {}) and acquire.level(2) == (True, {"desperate": True})
    assert acquire.level(7) == (True, {"desperate": True, "strict_artist": False})
    now = 10**9
    assert acquire.due(0, 0, 3 * 3600, 86400, now)
    assert not acquire.due(1, now - 3600, 3 * 3600, 86400, now)
    assert acquire.due(1, now - 3 * 3600 + 1800, 3 * 3600, 86400, now)  # half an hour early is fine
    assert not acquire.due(9, now - 20 * 3600, 3 * 3600, 86400, now)  # capped at a day


def test_login_failure_counts_no_try(run: Run) -> None:
    """A wrong Soulseek password: nothing is found, but that is not the songs' fault."""
    FakeDaemon.ready = False
    rows = missing(run)
    assert "interrupted" in acquire._search(run, rows, "search")
    con = run.connect()
    assert (
        con.execute("SELECT tries FROM attempts WHERE song_key = 'spotify:s3'").fetchone()[0] == 3
    )
    con.close()
    assert run.stop.is_set()
