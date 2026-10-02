"""Getting songs through the Soulseek daemon (acquire.py), against a fake daemon, with real filing."""

import threading
import time
import wave
from pathlib import Path
from typing import ClassVar

import pytest

from echolot import db
from echolot.config import Settings
from echolot.jobs import acquire
from echolot.jobs.schedule import BY_NAME
from echolot.jobs.worker import Run
from echolot.library import audio, catalog, filing, identity, review, tagging
from echolot.library.filing import Want
from echolot.services import soulseek
from echolot.settings import options, vault


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
        return {"ready": FakeDaemon.ready, "state": "Connected, LoggedIn" if FakeDaemon.ready else "Disconnected"}

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

    def download(self, search_job: str, c: soulseek.Candidate, parent_dir: str, settings: dict) -> str:
        FakeDaemon.downloads.append(c.path)
        behaviour = next(b for u, p, s, b in FakeDaemon.files[search_job.removeprefix("search:")] if p == c.path)
        self.jobs[c.path] = (behaviour, f"{parent_dir}/{c.parts[-1]}", c.length)
        return c.path

    def transfer(self, job: str) -> soulseek.Transfer:
        behaviour, path, seconds = self.jobs[job]
        if behaviour == "stuck":
            return soulseek.Transfer("running", None, 0, 1000, "")
        if behaviour == "fail":
            return soulseek.Transfer("failed", None, 0, 1000, "AllDownloadsFailed")
        wav(Path(path), 300 if behaviour == "long" else seconds)  # long: the peer's length was wrong
        return soulseek.Transfer("done", path, 1000, 1000, "")

    def cancel(self, job: str) -> None:
        self.jobs[job] = ("fail", "", 0)


@pytest.fixture
def run(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Run:
    FakeDaemon.files, FakeDaemon.searches, FakeDaemon.downloads, FakeDaemon.lost = {}, [], [], False
    FakeDaemon.ready = True
    monkeypatch.setattr(soulseek, "Daemon", FakeDaemon)
    monkeypatch.setattr(audio, "prepare", lambda p: audio.Prepared(p, False, None))  # no ffmpeg here
    monkeypatch.setattr(acquire, "pictures", lambda *a, **k: None)  # no Spotify pictures
    con = db.connect(settings.db_path)
    with con:
        options.update(con, options.Soulseek, daemon_music=str(settings.library_dir.parent), stall_minutes=2)
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


def test_found_after_skipping_wrong_and_stuck_results(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert "Club Remix" not in " ".join(FakeDaemon.downloads)  # judged by its name, never downloaded
    assert FakeDaemon.downloads == [
        "Music\\Artist C\\Artist C - Gone Song.flac",
        "Music\\Artist C\\Artist C - Gone Song.mp3",
        "Music\\Artist C\\Album\\03 Gone Song.m4a",
    ]
    con = run.connect()
    files = [r[0] for r in con.execute("SELECT path FROM files WHERE path LIKE 'Artist C/%'")]
    rejected = con.execute("SELECT action, wanted_seconds FROM events WHERE action = 'mismatch'").fetchone()
    con.close()
    assert files == ["Artist C/Artist C - Gone Song.m4a"]
    assert tuple(rejected) == ("mismatch", 180)
    artist, title, length, settings = FakeDaemon.searches[-1]
    assert (artist, title, length) == ("Artist C", "Gone Song", 180)
    assert settings["search"]["desperateSearch"] and settings["search"]["necessaryCond"]["strictArtist"]


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
    assert con.execute("SELECT source, song FROM events ORDER BY id DESC LIMIT 1").fetchone()[:] == (
        "soulseek",
        "spotify:s3",
    )
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
    assert not settings["search"]["necessaryCond"]["strictArtist"]  # 4+ tries: the artist may be missing
    con = run.connect()
    assert con.execute("SELECT tries FROM attempts WHERE song_key = 'spotify:s3'").fetchone()[0] == 5
    con.close()


def test_daemon_restart_counts_no_try(run: Run) -> None:
    FakeDaemon.lost = True
    rows = [r for r in missing(run) if r["key"] == "spotify:s3"]
    assert "1 interrupted" in acquire._search(run, rows, "search")
    con = run.connect()
    assert con.execute("SELECT tries FROM attempts WHERE song_key = 'spotify:s3'").fetchone()[0] == 3
    con.close()


def test_upgrade_replaces_the_lossy_copy(run: Run) -> None:
    """First Song is in the library as MP3: a FLAC search finds a genuine one, which takes over."""
    FakeDaemon.files["First Song"] = [("u1", "Music\\Artist A\\Artist A - First Song.flac", 201, "ok")]
    con = run.connect()
    with con:
        con.execute("UPDATE files SET duration = 201 WHERE path = 'Artist A/Artist A - First Song.mp3'")
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


def test_a_song_another_run_searches_is_left_to_it(run: Run) -> None:
    """The scheduled upgrade starts while the full one gives way (worker): the file the full one still
    downloads for is not searched a second time."""
    FakeDaemon.files["First Song"] = [("u1", "Music\\Artist A\\Artist A - First Song.flac", 201, "ok")]
    acquire.SEARCHING.add("Artist A/Artist A - First Song.mp3")
    try:
        message = acquire.upgrade(run)
    finally:
        acquire.SEARCHING.clear()
    assert message == "2 of 3 songs: 2 not found" and not FakeDaemon.downloads  # the other two: not on Soulseek
    assert "First Song" not in str(FakeDaemon.searches)
    assert (run.paths.tracks / "Artist A/Artist A - First Song.mp3").exists()


def test_a_fake_flac_is_not_downloaded_again_for_an_upgrade(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    """The only FLAC of First Song is made from an MP3: no upgrade, and the next upgrade search skips that
    file (its name and exact size) instead of downloading it again."""
    monkeypatch.setattr(audio, "prepare", lambda p: audio.Prepared(p, True, {"verdict": "lossy"}))
    FakeDaemon.files["First Song"] = [("u1", "Music\\Artist A\\Artist A - First Song.flac", 201, "ok")]
    con = run.connect()
    with con:
        con.execute("UPDATE files SET duration = 201 WHERE path = 'Artist A/Artist A - First Song.mp3'")
    con.close()
    acquire.upgrade(run)
    con = run.connect()
    fake = con.execute("SELECT file_name, fake, peer_bytes FROM events WHERE action = 'duplicate'").fetchone()
    con.close()
    assert tuple(fake) == ("Artist A - First Song", 1, 1) and len(FakeDaemon.downloads) == 1
    acquire.upgrade(run)
    assert len(FakeDaemon.downloads) == 1
    assert (run.paths.tracks / "Artist A/Artist A - First Song.mp3").exists()


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
    assert con.execute("SELECT tries FROM attempts WHERE song_key = 'spotify:s3'").fetchone()[0] == 3
    con.close()
    assert run.stop.is_set()


class FakeYtDlp:
    """Search results per site; fetch writes a WAV of the result's length, or fails with errors[url]."""

    def __init__(self, results: dict[str, list[dict]], errors: dict[str, str]) -> None:
        self.results, self.errors, self.fetched, self.queries = results, errors, [], []

    def search(self, query: str, site: str, stop: threading.Event) -> list[dict]:
        self.queries.append(query)
        return self.results.get(site, [])

    def fetch(self, url: str, dest: Path, stop: threading.Event) -> tuple[Path | None, str]:
        self.fetched.append(url)
        if url in self.errors:
            return None, self.errors[url]
        wav(
            dest.with_suffix(".wav"), next(r["duration"] for rs in self.results.values() for r in rs if r["url"] == url)
        )
        return dest.with_suffix(".wav"), ""


def hit(url: str, uploader: str, title: str, seconds: int) -> dict:
    return {"url": url, "uploader": uploader, "title": title, "duration": seconds}


def test_fallback_keeps_another_length_for_review(run: Run) -> None:
    """LAWTON - Believe In (200 s): SoundCloud has it, DRM-protected; YouTube only an official video of another
    edit (219 s) and the extended mix. The video is kept for review, once; the report says why."""
    want = Want("LAWTON", "Believe In", 200, "spotify:lawton")
    video = "LAWTON, Trancemaster Krause & Caroline Roxy - Believe In (Official Visualizer)"
    extended = hit("yt2", "Trance Paradise", "LAWTON - Believe In (Extended Mix)", 245)
    youtube = [hit("yt1", "Armada Music TV", video, 219), extended]
    soundcloud = [hit("sc1", "LAWTON", "Believe In", 200)]
    ydl = FakeYtDlp({"youtube": youtube, "soundcloud": soundcloud}, {"sc1": "DRM-protected"})
    con = run.connect()
    action, report = acquire._fallback_song(run, con, ydl, want, 2, strict_probable=True)
    assert action == "mismatch" and ydl.fetched == ["sc1", "yt1"]
    assert ydl.queries == ['LAWTON Believe In "Provided to YouTube"', "LAWTON Believe In", "LAWTON Believe In"]
    assert report["youtube"] == {"results": 2, "fits": 0, "rejected": {"another length": 2}, "tried": []}
    assert report["soundcloud"]["tried"] == [["Believe In", "download failed", "DRM-protected"]]
    assert report["near"] == ["youtube", video, 219, "mismatch", ""]
    assert filing.in_review(run.paths, "LAWTON", "Believe In")
    assert acquire._fallback_song(run, con, ydl, want, 2, strict_probable=True)[0] == "not found"
    assert ydl.fetched.count("yt1") == 1  # one waits for review already
    con.close()


def test_a_download_rejected_before_is_not_downloaded_again(run: Run) -> None:
    """A file only its tags could judge (they are not the song): kept for review once, then skipped by
    the next searches, where the same name and length show up again."""
    FakeDaemon.files["Gone Song"] = [("u1", "Music\\Artist C\\Album\\07 Track Seven.flac", 180, "ok")]
    rows = [r for r in missing(run) if r["key"] == "spotify:s3"]
    acquire._search(run, rows, "search")
    assert len(FakeDaemon.downloads) == 1
    FakeDaemon.files["Gone Song"].append(("u2", "Share\\Artist C\\07 Track Seven.flac", 181, "ok"))  # another peer
    acquire._search(run, [r for r in missing(run) if r["key"] == "spotify:s3"], "search")
    assert len(FakeDaemon.downloads) == 1
    con = run.connect()
    result = con.execute("SELECT result FROM attempts WHERE song_key = 'spotify:s3'").fetchone()[0]
    con.close()
    assert '"rejected before": 2' in result


def test_a_fallback_download_keeps_its_page(run: Run) -> None:
    """The page a YouTube download came from is kept with the event and in the DOWNLOAD tag."""
    want = Want("Artist C", "Gone Song", 180, "spotify:s3")
    ydl = FakeYtDlp(
        {"youtube": [hit("https://www.youtube.com/watch?v=gone", "Artist C", "Artist C - Gone Song", 180)]}, {}
    )
    con = run.connect()
    action, _ = acquire._fallback_song(run, con, ydl, want, 2, strict_probable=True)
    assert action == "new"
    assert con.execute("SELECT url FROM events WHERE action = 'new' ORDER BY id DESC").fetchone()[0].endswith("v=gone")
    path = run.paths.tracks / con.execute("SELECT path FROM events WHERE action = 'new' ORDER BY id DESC").fetchone()[0]
    con.close()
    assert tagging.read(path)["download"] == "https://www.youtube.com/watch?v=gone"


def test_a_filed_song_is_tagged_from_the_song(run: Run) -> None:
    """The file's tags: the song's artist and title, its Spotify page, downloaded from Soulseek."""
    FakeDaemon.files["Gone Song"] = [("u2", "Music\\Artist C\\Artist C - Gone Song.wav", 180, "ok")]
    acquire._search(run, [r for r in missing(run) if r["key"] == "spotify:s3"], "search")
    tags = tagging.read(run.paths.tracks / "Artist C" / "Artist C - Gone Song.wav")
    assert (tags["artists"], tags["title"]) == (["Artist C"], "Gone Song")
    assert (tags["sources"], tags["download"]) == (["https://open.spotify.com/track/s3"], "Soulseek")


def test_a_run_that_gives_way_leaves_the_rest(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    """Another job of the resource is due: the song in progress ends, the others are left for the run that
    goes on after it (worker.resume)."""
    con = run.connect()
    with con:
        options.update(con, options.Soulseek, parallel=1)
    con.close()
    search = FakeDaemon.search

    def search_then_due(self, *args):
        run.give_way.set()
        return search(self, *args)

    monkeypatch.setattr(FakeDaemon, "search", search_then_due)
    rows = missing(run) * 3  # three songs to search (the same one: only the first is)
    message = acquire._search(run, rows, "search")
    assert len(rows) > 1 and len(FakeDaemon.searches) == 1
    assert run.left == len(rows) - 1 and message.endswith(f"; gave way, {run.left} left")


def test_a_soundcloud_songs_flac_waits_for_review(run: Run) -> None:
    """Trance Tune is a SoundCloud song, in the library as M4A. The upgrade finds a FLAC, but its names are
    an uploader's: it is kept for review, not searched again while it waits, and Perfect match replaces
    the M4A."""
    FakeDaemon.files["Trance Tune"] = [("u1", "Music\\Uploader\\Uploader - Trance Tune.flac", 400, "ok")]
    con = run.connect()
    with con:
        con.execute("UPDATE files SET duration = 400 WHERE path = 'Uploader/Uploader - Trance Tune.m4a'")
    con.close()
    assert "1 confirm" in acquire.upgrade(run)
    con = run.connect()
    assert con.execute("SELECT 1 FROM files WHERE path = 'Uploader/Uploader - Trance Tune.m4a'").fetchone()
    (item,) = review.items(con, run.paths.music)["kept"]
    assert item.event["action"] == "confirm" and item.event["song"] == "soundcloud:1001"
    with con:
        con.execute("UPDATE upgrades SET last_try = 0")  # due again, but its FLAC waits for an answer
    con.close()
    searches = len(FakeDaemon.searches)
    acquire.upgrade(run)
    assert [s for s in FakeDaemon.searches[searches:] if s[1] == "Trance Tune"] == []
    con = run.connect()
    review.decide(con, run.paths.music, item.event["id"], "accept")
    with con:
        con.execute("UPDATE review_decisions SET decided = '2000-01-01T00:00:00'")
    assert "upgrade Uploader/Uploader - Trance Tune.flac" in review.apply_due(run, con)[-1]
    paths = sorted(r[0] for r in con.execute("SELECT path FROM files WHERE path LIKE 'Uploader/%'"))
    con.close()
    assert paths == ["Uploader/Uploader - Trance Tune.flac"]


def test_a_song_the_library_has_under_other_names_is_linked(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spotify lists First Song twice: by ISRC, and as a single by another artist whose release sounds like
    the library's file. Both are linked to Artist A's file instead of being searched."""
    con = run.connect()
    with con:
        con.execute("UPDATE songs SET isrc = 'QZAAA0000001' WHERE key = 'spotify:s1'")
        con.execute("UPDATE files SET duration = 201 WHERE path = 'Artist A/Artist A - First Song.mp3'")
        sql = "INSERT INTO songs (key, service, artist, title, length, isrc) VALUES (?, 'spotify', ?, ?, 201, ?)"
        con.execute(sql, ("spotify:twin", "Artist Z", "First Song - Single Version", "QZAAA0000001"))
        con.execute(sql, ("spotify:single", "Artist Y", "First Song (Radio Edit)", "QZBBB0000002"))
        con.executemany("INSERT INTO list_songs (list_key, position, song_key) VALUES ('spotify:likes', ?, ?)",
                        [(10, "spotify:twin"), (11, "spotify:single")])  # fmt: skip
    from echolot.library import recordings

    assert recordings.link_isrc(con, run.paths) == 1
    catalog.match_songs(con)
    assert con.execute("SELECT file FROM songs WHERE key = 'spotify:twin'").fetchone()[0] == (
        "Artist A/Artist A - First Song.mp3"
    )
    con.close()
    monkeypatch.setattr(identity, "check", lambda con, isrc, path, any_length=False: identity.Evidence("same", "x"))
    rows = [r for r in missing(run) if r["key"] == "spotify:single"]
    assert acquire._search(run, rows, "search") == "1 of 1 songs: 1 linked"
    assert FakeDaemon.searches == []  # not searched
    con = run.connect()
    catalog.match_songs(con)
    assert con.execute("SELECT file FROM songs WHERE key = 'spotify:single'").fetchone()[0] == (
        "Artist A/Artist A - First Song.mp3"
    )
    reasons = [r[0] for r in con.execute("SELECT reason FROM events WHERE action = 'linked' ORDER BY id")]
    con.close()
    assert reasons == [
        "same recording as Artist A/Artist A - First Song.mp3 (ISRC QZAAA0000001)",
        "same recording as Artist A/Artist A - First Song.mp3 (the release's audio)",
    ]


def add_missing(run: Run, key: str, title: str, tries: int = 0, last_try: int = 0, last_fallback: int = 0) -> None:
    con = run.connect()
    with con:
        sql = "INSERT INTO songs (key, service, artist, title, length) VALUES (?, 'spotify', 'Artist N', ?, 200)"
        con.execute(sql, (key, title))
        sql = "INSERT INTO list_songs (list_key, position, song_key) SELECT 'spotify:likes', max(position) + 1, ? FROM list_songs"
        con.execute(sql, (key,))
        if tries or last_fallback:
            sql = "INSERT INTO attempts (song_key, tries, last_try, last_fallback) VALUES (?, ?, ?, ?)"
            con.execute(sql, (key, tries, last_try, last_fallback))
    con.close()


def test_the_sync_searches_new_songs_and_hands_a_miss_to_the_fallback(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spotify lists starts New Spotify songs only when there is a new song (Soulseek jobs make way only
    then). It searches only songs never searched; Gone Song (searched 3 times) is the evening search's. One
    Soulseek does not have goes to YouTube and SoundCloud right after."""
    monkeypatch.setattr("echolot.jobs.lists.fetch_spotify", lambda run: "3 lists, 0 changed")
    assert acquire.sync(run) == "3 lists, 0 changed" and "search_new" not in run.after
    add_missing(run, "spotify:new", "New Song")
    assert acquire.sync(run).endswith("; 1 new songs to search") and "search_new" in run.after
    run.after.clear()
    acquire.search_new(run)
    assert [s[1] for s in FakeDaemon.searches] == ["New Song"] and "fallback" in run.after
    run.after.clear()
    FakeDaemon.files["Newer Song"] = [("u1", "Music\\Artist N\\Artist N - Newer Song.flac", 200, "ok")]
    add_missing(run, "spotify:newer", "Newer Song")
    acquire.search_new(run)
    assert "fallback" not in run.after  # found: nothing for the fallback
    assert acquire.search_new(run) == "no new songs"


def test_the_evening_search_takes_each_song_daily_then_weekly(run: Run) -> None:
    now = int(time.time())
    add_missing(run, "spotify:recent", "Recent Song", tries=1, last_try=now - 3600)  # searched an hour ago
    add_missing(run, "spotify:old", "Old Song", tries=8, last_try=now - 2 * 86400)  # weekly by now
    add_missing(run, "spotify:due", "Due Song", tries=8, last_try=now - 7 * 86400)
    run.trigger = "schedule"
    acquire.sweep(run)
    assert sorted(s[1] for s in FakeDaemon.searches) == ["Due Song", "Gone Song"]
    FakeDaemon.searches.clear()
    assert "none due" in acquire.sweep(run)  # all searched just now
    run.trigger = "manual"  # Run now: every missing song, whatever its wait
    acquire.sweep(run)
    assert sorted(s[1] for s in FakeDaemon.searches) == ["Due Song", "Gone Song", "Old Song", "Recent Song"]


def test_the_fallback_takes_new_misses_first_and_the_upgrade_waits(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    """After one Soulseek miss; a song never tried before one tried a week ago. A lossy file filed from
    YouTube gets its first FLAC search 12 h later (Soulseek just had nothing)."""
    add_missing(run, "spotify:new", "New Song", tries=1, last_try=int(time.time()))
    con = run.connect()
    with con:
        con.execute(
            "UPDATE attempts SET last_fallback = ? WHERE song_key = 'spotify:s3'", (int(time.time()) - 8 * 86400,)
        )
    con.close()
    ydl = FakeYtDlp({"youtube": [hit("yt-new", "Artist N", "Artist N - New Song", 200)]}, {})
    monkeypatch.setattr(acquire.ytdlp, "YtDlp", lambda *a, **k: ydl)
    assert acquire.fallback(run).startswith("1 of ")
    first = [q.split(' "')[0] for q in ydl.queries if q.endswith('"Provided to YouTube"')]
    assert first.index("Artist N New Song") < first.index("Artist C Gone Song")  # never tried before tried
    con = run.connect()
    tries, last = con.execute("SELECT tries, last_try FROM upgrades WHERE song_key = 'spotify:new'").fetchone()
    con.close()
    assert tries == 1 and not acquire.due(tries, last, *acquire.UPGRADE_WAIT, time.time())


def test_a_fallback_download_the_library_has_is_linked(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gone Song is in the library under another artist's name and sounds the same: linked, not filed."""
    monkeypatch.setattr(identity, "alike", lambda a, b: 0.95)
    wav(run.paths.tracks / "Other Name" / "Other Name - Gone Song (Radio Edit).wav", 181)
    con = run.connect()
    catalog.refresh(con, run.paths.tracks)
    ydl = FakeYtDlp({"youtube": [hit("yt-gone", "Artist C", "Artist C - Gone Song", 180)]}, {})
    action, _ = acquire._fallback_song(run, con, ydl, Want("Artist C", "Gone Song", 180, "spotify:s3"), 2, True)
    con.close()
    assert action == "linked" and not (run.paths.tracks / "Artist C" / "Artist C - Gone Song.wav").exists()


@pytest.mark.parametrize(("names", "for_review"), [("exact", False), ("probable", True)])
def test_a_soundcloud_flac_with_the_same_audio_takes_over(
    run: Run, monkeypatch: pytest.MonkeyPatch, names: str, for_review: bool
) -> None:
    """The FLAC found for Trance Tune is its M4A's audio in another codec: it replaces the M4A at once; with
    names that differ (an artist missing) it is filed for a last look (Please confirm), not kept aside."""
    FakeDaemon.files["Trance Tune"] = [("u1", "Music\\Uploader\\Uploader - Trance Tune.flac", 400, "ok")]
    monkeypatch.setattr(identity, "same_master", lambda a, b: 0.999)
    if names == "probable":
        monkeypatch.setattr(filing, "identify", lambda *a: ("probable", "the artist is not named"))
    con = run.connect()
    with con:
        con.execute("UPDATE files SET duration = 400 WHERE path = 'Uploader/Uploader - Trance Tune.m4a'")
    con.close()
    assert "1 upgrade" in acquire.upgrade(run)
    con = run.connect()
    paths = sorted(r[0] for r in con.execute("SELECT path FROM files WHERE path LIKE 'Uploader/%'"))
    e = con.execute("SELECT matched, audio FROM events WHERE action = 'upgrade' ORDER BY id DESC").fetchone()
    filed = review.items(con, run.paths.music)["filed"]
    con.close()
    assert paths == ["Uploader/Uploader - Trance Tune.flac"] and e["audio"] == "the same audio as your copy (0.999)"
    assert (e["matched"] == "probable") is for_review and bool(filed) is for_review
