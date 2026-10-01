"""Getting songs: Soulseek through the Sockseek daemon (sync, sweep, upgrade) and the search
fallback on YouTube and SoundCloud. Missing Spotify songs are searched on Soulseek; SoundCloud songs come from
SoundCloud itself (their artist names are too unreliable to file search results by), and a FLAC the upgrade
finds for one is kept for review (Worth a look), never filed by itself.

One song, one attempt:
  1. search (the terms get looser with every search that found nothing: LOOSEN)
  2. judge every result by its path and length (rules.prejudge): clear mismatches are never downloaded;
     results naming the title come first, then those that say nothing about it, each in Sockseek's order
  3. download the best one; a transfer without progress for a while (queued at the peer) is given up
  4. check the file (audio.prepare) and whether it is the song (filing.file_into, strict: tags and names)
  5. filed, or kept for review; else the next result (at most MAX_RESULTS)
"""

import collections
import dataclasses
import json
import logging
import re
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from echolot.library import audio, catalog, filing, identity, recordings, rules, tagging
from echolot.library.filing import Want
from echolot.services import soulseek, spotify, ytdlp
from echolot.settings import options

if TYPE_CHECKING:
    from echolot.jobs.worker import Run

log = logging.getLogger(__name__)

# Songs Soulseek did not find are searched less strictly with every failure (by searches that found nothing):
#   0-1: as requested; the artist must be in the Soulseek path
#   2-3: title without feat. credits, 'From "Film"' and plain suffixes, first artist only; desperate:
#        a search without results is repeated with the title alone and the artist alone (any length)
#   4+ : also without requiring the artist in the path (the tags are still checked after the download)
LOOSEN = [(4, {"desperate": True, "strict_artist": False}), (2, {"desperate": True})]
MAX_RESULTS = 5  # downloads tried per song and attempt
SEARCH_SECONDS = 300  # a search waits at most this long (queued behind the rate limit included)
TRANSFER_SECONDS = 45 * 60  # a download may take at most this long
FOUND = {"new", "upgrade", "duplicate", "linked"}  # linked: the library had it under other names


def stage(tries: int) -> int:
    """How far the search is loosened for a song not found `tries` times: 0, 1 or 2 (see LOOSEN)."""
    return sum(tries >= n for n, _ in LOOSEN)


def kind(why: str) -> str:
    """A prejudge reason as a short kind, counted in the search reports."""
    for start, name in (
        ("length", "another length"),
        ("artist not", "artist missing"),
        ("neither", "another song"),
        ("the file name lacks", "lacks the version"),
        ("marked wrong", "marked wrong"),
        ("the artist only", "artist in another name"),
        ("title not in", "title missing, no length"),
    ):
        if why.startswith(start):
            return name
    return "another version" if why.endswith("names another version") else why


def level(tries: int) -> tuple[bool, dict[str, bool]]:
    """(loosened search terms, search options) for a song not found `tries` times."""
    for n, opts in LOOSEN:
        if tries >= n:
            return True, opts
    return False, {}


# a song Soulseek did not find goes to the YouTube and SoundCloud search at once (fallback); one found nowhere
# is searched again by the evening search: daily, weekly after WEEKLY_AFTER misses
DAILY, WEEKLY, WEEKLY_AFTER = 20 * 3600, int(6.5 * 86400), 7
UPGRADE_WAIT = (12 * 3600, 3 * 86400)  # a lossy song's FLAC search: after 12 h, 1 d, 2 d, then every 3 d


def next_search(tries: int, last: int) -> float:
    """From when the evening search takes a song searched `tries` times without a find."""
    return (last or 0) + (DAILY if tries < WEEKLY_AFTER else WEEKLY)


def retry_at(tries: int, last: int, first: int, cap: int) -> float:
    """When a song searched `tries` times is due again: `first` seconds after the last search, doubling up
    to `cap` (half an hour early, so it makes the run at that time)."""
    return (last or 0) + min(first * 2 ** max(tries - 1, 0), cap) - 1800


def due(tries: int, last: int, first: int, cap: int, now: float) -> bool:
    return not tries or now >= retry_at(tries, last, first, cap)


# ---------------------------------------------------------------- one song


@dataclass
class Outcome:
    action: str  # new, upgrade, duplicate, not found, failed, ...
    detail: str = ""
    report: dict | None = None  # what the search saw: {stage, results, fits, rejected: {kind: n}, tried}


class Fetcher:
    """Searches and downloads songs through the daemon; one per job run, shared by its threads."""

    def __init__(self, run: "Run", purpose: str) -> None:
        self.run, self.purpose = run, purpose  # purpose: search or upgrade
        con = run.connect()
        try:
            self.opts = options.get(con, options.Soulseek)
        finally:
            con.close()
        self.daemon = soulseek.Daemon(self.opts.url)
        self.local_inbox = run.paths.inbox("soulseek")
        self.daemon_inbox = self.opts.daemon_music.rstrip("/") + "/inbox/soulseek"

    def local(self, daemon_path: str) -> Path:
        """The daemon's path of a downloaded file, as Echolot sees it."""
        rel = Path(daemon_path).relative_to(self.opts.daemon_music)
        return self.run.paths.music / rel

    def song(self, want: Want, tries: int) -> Outcome:
        loosen, opts = level(tries) if self.purpose == "search" else (False, {})
        settings = soulseek.search_settings(**opts, flac_only=self.purpose == "upgrade")
        artist, title, length = rules.search_terms(want.artist, want.title, want.length, loosen)
        job = self.daemon.search(artist, title, length, settings)
        self.daemon.wait(job, self.run.stop, time.monotonic() + SEARCH_SECONDS)
        found = self.daemon.results(job)
        con = self.run.connect()
        try:
            blocked = [r[0] for r in con.execute("SELECT name FROM blocked WHERE song_key = ?", (want.key,))]
            before = filing.rejected_before(con, want.key)
        finally:
            con.close()
        wanted = 0 if rules.mix_cut(want.title) else want.length
        judged, rejected, terms = [], collections.Counter(), (wanted, opts.get("strict_artist", True), blocked, loosen)
        for c in found:
            if filing.was_rejected(before, c.name, c.length):
                rejected["rejected before"] += 1
                continue
            verdict, rank, why = rules.prejudge(want.artist, want.title, c.path, c.length, *terms)
            if verdict != rules.REJECT:
                judged.append((rank, c.rank, c))
            else:
                rejected[kind(why)] += 1
        depth = stage(tries) if self.purpose == "search" else 0
        report = {"stage": depth, "results": len(found), "fits": len(judged), "rejected": dict(rejected), "tried": []}
        if not judged:
            return Outcome("not found", f"{len(found)} results, none fits" if found else "no results", report)
        tried = []
        for _, _, c in sorted(judged, key=lambda j: j[:2])[:MAX_RESULTS]:
            if self.run.stop.is_set():
                break
            outcome = self.attempt(job, c, want, tries, settings)
            if outcome.action in ("upgrade", "confirm") or (outcome.action in FOUND and self.purpose == "search"):
                return outcome
            tried.append(f"{c.name}: {outcome.detail or outcome.action}")
            report["tried"].append([c.name, outcome.action, outcome.detail])
        return Outcome("not found", f"{len(found)} results; tried " + "; ".join(tried), report)

    def attempt(self, search_job: str, c: soulseek.Candidate, want: Want, tries: int, settings: dict) -> Outcome:
        name = uuid.uuid4().hex[:12]
        local_dir = self.local_inbox / name
        try:
            job = self.daemon.download(search_job, c, f"{self.daemon_inbox}/{name}", settings)
            t = self.watch(job)
            if t.state != "done" or not t.path:
                return Outcome("failed", t.reason)
            try:
                prepared = audio.prepare(self.local(t.path))
            except audio.Rejected as e:
                return Outcome("bad file", str(e))
            con = self.run.connect()
            try:
                heard = identity.check(con, want.isrc, prepared.path, bool(rules.mix_cut(want.title)))
                confirm = self.purpose == "upgrade" and want.key.startswith("soundcloud:")
                action, dest = filing.file_into(
                    con, self.run.paths, prepared.path, want, "soulseek", strict=True,
                    file_name=c.name, folders=c.folders, probable=self.purpose == "search",
                    tries=tries, fake=prepared.fake, heard=heard, confirm=confirm,
                )  # fmt: skip
                if dest and action in ("new", "upgrade"):
                    finish(self.run, con, dest, want)
            finally:
                con.close()
            return Outcome(action, str(dest.relative_to(self.run.paths.tracks)) if dest else "")
        finally:
            shutil.rmtree(local_dir, ignore_errors=True)

    def watch(self, job: str) -> soulseek.Transfer:
        """Wait for a download; give it up when it makes no progress for stall_minutes (queued at the
        peer: Sockseek keeps such a download alive while the peer serves others)."""
        start = last_change = time.monotonic()
        last_bytes = -1
        while True:
            t = self.daemon.transfer(job)
            if t.state != "running":
                return t
            now = time.monotonic()
            if t.done_bytes != last_bytes:
                last_bytes, last_change = t.done_bytes, now
            stalled = now - last_change > self.opts.stall_minutes * 60
            if stalled or now - start > TRANSFER_SECONDS or self.run.stop.is_set():
                self.daemon.cancel(job)
                why = "no progress (queued at the peer)" if stalled else "stopped"
                return soulseek.Transfer("cancelled", None, t.done_bytes, t.total_bytes, why)
            self.run.stop.wait(2)


def finish(run: "Run", con: sqlite3.Connection, dest: Path, want: Want, cover: Path | None = None) -> None:
    """A song just filed: its tags from the song (tagging: artists, title, album, source, download), an
    empty album the title (a single), and the cover and artist picture from Spotify (or the given cover
    file). Failures only cost the tags or the pictures."""
    rel = dest.relative_to(run.paths.tracks).as_posix()
    try:
        if tags := tagging.for_file(con, rel, dest, want.key):
            tagging.write(dest, tags)
    except Exception as e:
        log.warning("tags of %s not written: %s", dest.name, e)
    audio.write_tags(dest, album_if_empty=want.title)
    pictures(run, con, dest, want, cover)


def pictures(run: "Run", con: sqlite3.Connection, dest: Path, want: Want, cover: Path | None) -> None:
    """The cover (the given file, else Spotify's) and the artist picture of a song just filed."""
    if cover and cover.is_file():
        audio.embed_cover(dest, cover.read_bytes())
        return
    try:
        sp = spotify.Spotify(con, run.vault)
        if want.key.startswith("spotify:"):
            track = sp.track(want.key.removeprefix("spotify:"))
        else:
            track = sp.find_track(want.artist, want.title)
        if not track:
            return
        images = (track.get("album") or {}).get("images") or []
        if images and not audio.has_picture(dest):
            audio.embed_cover(dest, _download(images[0]["url"]))
        artist_id = ((track.get("artists") or [{}])[0] or {}).get("id")
        picture = dest.parent / "artist.jpg"
        if artist_id and not picture.exists() and (url := sp.artist_image(artist_id)):
            picture.write_bytes(_download(url))
    except (spotify.SpotifyError, OSError) as e:
        log.info("pictures for %s: %s", dest.name, e)


def _download(url: str) -> bytes:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


# ---------------------------------------------------------------- the jobs


def _spotify_missing(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Wanted Spotify songs not in the library, greyed-out ones first (most at risk)."""
    return con.execute(
        "SELECT s.*, coalesce(a.tries, 0) AS tries, coalesce(a.last_try, 0) AS last_try FROM wanted s "
        "LEFT JOIN attempts a ON a.song_key = s.key WHERE s.service = 'spotify' AND s.file IS NULL "
        "ORDER BY s.unavailable IS NULL, s.artist, s.title"
    ).fetchall()


def _search(run: "Run", songs: list[sqlite3.Row], purpose: str) -> str:
    """Search and download songs, several at a time; attempts are counted per song as it ends."""
    if not songs:
        return "nothing to search"
    fetcher = Fetcher(run, purpose)
    fetcher.daemon.status()  # reachable (it logs in to Soulseek with the first search)
    con = run.connect()
    try:
        index = recordings.Index(catalog.Catalog.from_db(con))
    finally:
        con.close()
    deadline = time.monotonic() + 20 * 60 + 15 * len(songs)  # a run shares Soulseek with the others
    counts: dict[str, int] = {}
    done = 0
    lock = threading.Lock()

    def one(row: sqlite3.Row) -> None:
        nonlocal done
        if run.stop.is_set() or run.give_way.is_set() or time.monotonic() > deadline:
            return
        want = _want(row)
        try:
            outcome = _in_library(run, want, index) if purpose == "search" else None
            outcome = outcome or fetcher.song(want, row["tries"])
        except soulseek.Lost:
            outcome = Outcome("interrupted", "the daemon restarted")
        except soulseek.Cancelled:
            return
        except Exception as e:  # one song's trouble never stops the others
            log.exception("%s: %s - %s", purpose, want.artist, want.title)
            outcome = Outcome("failed", str(e))
        if outcome.action not in FOUND and not _logged_in(fetcher.daemon):
            # not the song's fault: no try counted, and the rest waits for the next run
            outcome = Outcome("interrupted", "Soulseek not logged in")
            run.stop.set()
        con = run.connect()
        try:
            _count(con, purpose, want.key, outcome.action, outcome.report)
        finally:
            con.close()
        log.info("%s: %s - %s: %s %s", purpose, want.artist, want.title, outcome.action, outcome.detail)
        with lock:
            done += 1
            counts[outcome.action] = counts.get(outcome.action, 0) + 1
            run.say(f"{done} of {len(songs)} songs: " + ", ".join(f"{n} {a}" for a, n in sorted(counts.items())))

    with ThreadPoolExecutor(fetcher.opts.parallel, thread_name_prefix=purpose) as pool:
        list(pool.map(one, songs))
    run.after.add("library")
    summary = f"{done} of {len(songs)} songs: " + (", ".join(f"{n} {a}" for a, n in sorted(counts.items())) or "none")
    if run.give_way.is_set() and not run.stop.is_set() and done < len(songs):
        run.left = len(songs) - done  # searched after the job that is due (worker.resume)
        summary += f"; gave way, {run.left} left"
    return summary


def _in_library(run: "Run", want: Want, index: recordings.Index) -> Outcome | None:
    """The song is a library file under other names (its release's audio): linked, not searched."""
    con = run.connect()
    try:
        if e := recordings.in_library(con, run.paths, want, index):
            recordings.link(con, run.paths, want.key, want, e, "the release's audio")
            return Outcome("linked", e.path)
        return None
    finally:
        con.close()


def _want(row: sqlite3.Row) -> Want:
    """The song to search for; a SoundCloud title without its release decoration (tagging.clean_title)."""
    want = Want.of(row)
    if row["service"] == "soundcloud":
        want = dataclasses.replace(want, title=tagging.clean_title(want.title, want.artist))
    return want


def _logged_in(daemon: soulseek.Daemon) -> bool:
    try:
        return daemon.status()["ready"]
    except soulseek.DaemonError:
        return False


def _count(con: sqlite3.Connection, purpose: str, key: str, action: str, report: dict | None = None) -> None:
    """Record a search: found songs leave the attempts, the others count one more try and keep what the
    search saw (a search the daemon lost does not count)."""
    table = "attempts" if purpose == "search" else "upgrades"
    success = action in FOUND if purpose == "search" else action == "upgrade"
    with con:
        if success:
            con.execute(f"DELETE FROM {table} WHERE song_key = ?", (key,))
        elif action not in ("interrupted",):
            con.execute(
                f"INSERT INTO {table} (song_key, tries, last_try) VALUES (?, 1, ?) ON CONFLICT (song_key) "
                "DO UPDATE SET tries = tries + 1, last_try = excluded.last_try",
                (key, int(time.time())),
            )
            if report and purpose == "search":
                con.execute("UPDATE attempts SET result = ? WHERE song_key = ?", (json.dumps(report), key))


def sync(run: "Run") -> str:
    """Fetch the Spotify lists, then search the new songs (never searched) on Soulseek. One not found goes to
    the YouTube and SoundCloud search right after (fallback); a later search is the evening search's."""
    from echolot.jobs import lists

    parts = [lists.fetch_spotify(run)]
    con = run.connect()
    try:
        catalog.refresh(con, run.paths.tracks)
        if linked := recordings.link_isrc(con, run.paths):  # the library has them under other names
            catalog.match_songs(con)
            parts.append(f"{linked} linked by ISRC")
        songs = [r for r in _spotify_missing(con) if not r["tries"]]
    finally:
        con.close()
    parts.append(_search(run, songs, "search"))
    con = run.connect()
    try:  # a miss counted a try (a find left the attempts, an interrupted search counted nothing)
        keys = [r["key"] for r in songs]
        sql = f"SELECT 1 FROM attempts WHERE tries >= 1 AND song_key IN ({','.join('?' * len(keys))})"
        missed = con.execute(sql, keys).fetchone()
    finally:
        con.close()
    if missed:
        run.after.add("fallback")  # not on Soulseek: YouTube, then SoundCloud, now
    return "; ".join(parts)


def sweep(run: "Run") -> str:
    """Search the songs found nowhere yet again (at the hours most Soulseek users are online): each one
    daily, weekly after WEEKLY_AFTER searches without a find."""
    con = run.connect()
    try:
        catalog.refresh(con, run.paths.tracks)
        if recordings.link_isrc(con, run.paths):
            catalog.match_songs(con)
        now = time.time()
        songs = [r for r in _spotify_missing(con) if now >= next_search(r["tries"], r["last_try"])]
    finally:
        con.close()
    return _search(run, songs, "search")


def upgrade(run: "Run") -> str:
    return _upgrade(run, everything=False)


def upgrade_all(run: "Run") -> str:
    """The FLAC upgrade for every song not genuine lossless, whatever its wait (started by hand; it gives
    way to every other Soulseek job and goes on after it)."""
    return _upgrade(run, everything=True)


def _upgrade(run: "Run", everything: bool) -> str:
    """FLAC-only search for wanted songs whose library copy is not genuine lossless; each song waits 12 h,
    1 d, 2 d, then every 3 d between searches; the longest waiting first, at most upgrade_batch per run (or
    what a run that gave way left). A SoundCloud song's FLAC is kept for review (Fetcher.attempt), and the
    song is not searched while one waits there; a SoundCloud song whose file a Spotify song has is upgraded
    as that one. Close matches are not upgraded (a FLAC found would be the song, not the version taken)."""
    con = run.connect()
    try:
        catalog.refresh(con, run.paths.tracks)
        rows = con.execute(
            "SELECT s.*, coalesce(u.tries, 0) AS tries, coalesce(u.last_try, 0) AS last_try FROM wanted s "
            "JOIN files f ON f.path = s.file LEFT JOIN upgrades u ON u.song_key = s.key "
            "WHERE f.quality != 'lossless' AND NOT s.close_match AND (s.service = 'spotify' OR NOT EXISTS "
            "(SELECT 1 FROM wanted o WHERE o.service = 'spotify' AND o.file = s.file)) ORDER BY last_try"
        ).fetchall()
        with con:  # songs that are lossless now or left every list
            con.execute(
                "DELETE FROM upgrades WHERE song_key NOT IN (SELECT s.key FROM wanted s JOIN files f "
                "ON f.path = s.file WHERE f.quality != 'lossless')"
            )
        # what a run that gave way left, else every song (by hand) or the batch
        batch = run.budget or (len(rows) if everything else options.get(con, options.Soulseek).upgrade_batch)
    finally:
        con.close()
    now, seen, songs = time.time(), set(), []
    for r in rows:
        if r["service"] == "soundcloud" and filing.in_review(run.paths, r["artist"], _want(r).title):
            continue  # its FLAC waits for your answer
        if r["file"] not in seen and (everything or due(r["tries"], r["last_try"], *UPGRADE_WAIT, now)):
            seen.add(r["file"])
            songs.append(r)
    run.say(f"{len(rows)} songs not genuine lossless, {len(songs[:batch])} searched now")
    return _search(run, songs[:batch], "upgrade")


# ---------------------------------------------------------------- search fallback (home IP)


def fallback(run: "Run") -> str:
    """Songs Soulseek did not find (started by the sync right after its search), and SoundCloud songs
    SoundCloud hands out to nobody: search YouTube, then SoundCloud, each song at most once a week, the
    ones never tried first. The first result that passes the same checks as a Soulseek download is filed;
    the rest are tried in order; a download the library has under other names (its audio) is linked instead.
    The result is lossy: the FLAC upgrade looks for a FLAC from 12 h later."""
    week = int(time.time()) - 7 * 86400
    con = run.connect()
    try:
        catalog.refresh(con, run.paths.tracks)
        songs = con.execute(
            "SELECT s.*, coalesce(a.tries, 0) AS tries FROM wanted s LEFT JOIN attempts a ON a.song_key = s.key "
            "WHERE s.file IS NULL AND coalesce(a.last_fallback, 0) < ? AND ((s.service = 'spotify' "
            "AND a.tries >= 1) OR (s.service = 'soundcloud' AND s.unavailable IS NOT NULL)) "
            "ORDER BY coalesce(a.last_fallback, 0) > 0, s.unavailable IS NULL",
            (week,),
        ).fetchall()
        token = run.vault.get(con, "soundcloud.token")
    finally:
        con.close()
    ydl = ytdlp.YtDlp(run.data / "ytdlp", token)
    added = kept = linked = 0
    for n, row in enumerate(songs, 1):
        if run.stop.is_set() or run.give_way.is_set():  # paused or a more urgent job: that song was the last
            run.left = 0 if run.stop.is_set() else len(songs) - n + 1
            break
        want = Want.of(row)
        run.say(f"{n} of {len(songs)}: {want.artist} - {want.title}")
        con = run.connect()
        try:
            with con:
                con.execute(
                    "INSERT INTO attempts (song_key, tries, last_fallback) VALUES (?, 0, ?) ON CONFLICT "
                    "(song_key) DO UPDATE SET last_fallback = excluded.last_fallback",
                    (want.key, int(time.time())),
                )
            action, report = _fallback_song(run, con, ydl, want, row["tries"], row["service"] == "spotify")
            with con:
                con.execute(
                    "UPDATE attempts SET fallback_result = ? WHERE song_key = ?", (json.dumps(report), want.key)
                )
        finally:
            con.close()
        added += action in ("new", "upgrade")
        linked += action == "linked"
        kept += action == "mismatch"
        log.info("fallback: %s - %s: %s", want.artist, want.title, action)
    shutil.rmtree(run.paths.inbox("fallback"), ignore_errors=True)
    run.after.add("library")
    return (
        f"{added} of {len(songs)} songs found on YouTube or SoundCloud"
        + (f", {linked} in the library under other names" if linked else "")
        + (f", {kept} of another length kept for review" if kept else "")
        + (f"; gave way, {run.left} left" if run.left else "")
    )


def _fallback_song(
    run: "Run", con: sqlite3.Connection, ydl: ytdlp.YtDlp, want: Want, tries: int, strict_probable: bool
) -> tuple[str, dict]:
    """Search YouTube, then SoundCloud, and file the first result that passes the checks. Failing that, the
    result closest in length that names exactly this song but is another length (an official video often
    has its own edit) is downloaded and kept for review: only a listener can tell. One at a time; one
    discarded in review is not kept again. Returns the action and what the search saw, per site."""

    def fetch(site: str, r: dict) -> tuple[str, str]:
        got, error = ydl.fetch(r["url"], run.paths.inbox("fallback") / want.key.replace(":", "-"), run.stop)
        if not got:
            return "download failed", error
        try:
            prepared = audio.prepare(got)
        except audio.Rejected as e:
            return "bad file", str(e)
        source = "youtube" if site == "youtube" else "soundcloud-search"
        dur = want.length or audio.probe(prepared.path)[0]
        if same := recordings.already(
            run.paths, prepared.path, want.title, dur, recordings.Index(catalog.Catalog.from_db(con))
        ):
            prepared.path.unlink(missing_ok=True)  # the library has it under other names
            recordings.link(con, run.paths, want.key, want, same, "the same audio")
            return "linked", ""
        heard = identity.check(con, want.isrc, prepared.path, bool(rules.mix_cut(want.title)))
        found = {"file_name": r["title"], "folders": (r["uploader"],), "fake": prepared.fake, "heard": heard}
        found["url"] = r["url"]  # the page, for the DOWNLOAD tag
        checks = {"strict": True, "probable": strict_probable, "tries": tries}
        action, dest = filing.file_into(con, run.paths, prepared.path, want, source, **found, **checks)
        if dest and action in ("new", "upgrade"):
            finish(run, con, dest, want)
            with con:  # lossy: the FLAC upgrade's first search 12 h from now (Soulseek just had nothing)
                sql = "INSERT OR REPLACE INTO upgrades (song_key, tries, last_try) VALUES (?, 1, ?)"
                con.execute(sql, (want.key, int(time.time())))
        return action, ""

    query = " ".join(
        re.findall(r"[\w']+", f"{want.artist} {rules.release_title(want.title)}")
    )  # "Was!?!?" finds nothing
    queries = {"youtube": [f'{query} "Provided to YouTube"', query], "soundcloud": [query]}  # releases' own audio first
    wanted = 0 if rules.mix_cut(want.title) else want.length
    before = filing.rejected_before(con, want.key)
    report: dict = {}
    others = []  # (how far off, site, result): the song by name, another length
    for site in ("youtube", "soundcloud"):
        results = list({r["url"]: r for q in queries[site] for r in ydl.search(q, site, run.stop)}.values())
        judged, rejected = [], collections.Counter()
        for i, r in enumerate(results):
            if filing.was_rejected(before, r["title"], r["duration"] or 0):
                rejected["rejected before"] += 1
                continue
            path = f"{r['uploader']}/{r['title']}"
            verdict, rank, why = rules.prejudge(want.artist, want.title, path, r["duration"], wanted)
            if verdict != rules.REJECT:
                judged.append((rank, i, r))
                continue
            rejected[kind(why)] += 1
            if _other_length(want, path, r["duration"], wanted):
                others.append((abs(r["duration"] - wanted), site, r))
        seen = report[site] = {"results": len(results), "fits": len(judged), "rejected": dict(rejected), "tried": []}
        for _, _, r in sorted(judged, key=lambda j: j[:2])[:3]:
            action, detail = fetch(site, r)
            seen["tried"].append([r["title"], action, detail])
            if action in FOUND:
                return action, report
    near = min(others, key=lambda o: o[0], default=None)
    if near and not filing.in_review(run.paths, want.artist, want.title):
        _, site, r = near
        if not filing.is_blocked(con, want.key, [r["title"]]):
            action, detail = fetch(site, r)
            report["near"] = [site, r["title"], round(r["duration"]), action, detail]  # 'mismatch': kept for review
            return action, report
    return "not found", report


def _other_length(want: Want, path: str, seconds: float | None, wanted: float) -> bool:
    """A result that names exactly the song but is another length (from 2/3 to 1.5 times as long)."""
    if not (wanted and seconds and 2 / 3 <= seconds / wanted <= 1.5):
        return False
    return rules.prejudge(want.artist, want.title, path, seconds, 0)[:2] == (rules.ACCEPT, 0)
