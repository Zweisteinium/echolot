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
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from echolot.library import audio, catalog, filing, identity, recordings, rules, tagging
from echolot.library.filing import Want
from echolot.services import slskd, soulseek, spotify, ytdlp
from echolot.services import soundcloud as sc_api
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
POLL_SECONDS = 2  # how often a running download is looked at
EDIT_REVIEW = "another edit, in review"  # a YouTube song's download that is probably a library song's other edit
FOUND = {"new", "upgrade", "duplicate", "linked", EDIT_REVIEW}  # linked: the library had it under other names
SEARCHING: set[str] = set()  # the songs (or files, upgrades) searched now: a run started beside one giving way
SEARCHING_LOCK = threading.Lock()  # (worker) skips them, so nothing is downloaded twice at once


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
            self.keep_hires = options.get(con, options.Files).keep_hires
            if self.opts.backend == "slskd":  # downloads go to <slskd's downloads>/echolot/<name>
                self.daemon: soulseek.Daemon | slskd.Slskd = slskd.connect(con, run.vault, self.opts)
                self.local_inbox, self.daemon_inbox = Path(self.opts.slskd_downloads) / "echolot", "echolot"
            else:  # the Sockseek daemon downloads to <music>/inbox/soulseek/<name>, as it sees the music folder
                self.daemon = soulseek.Daemon(self.opts.url)
                self.local_inbox = run.paths.inbox("soulseek")
                self.daemon_inbox = self.opts.daemon_music.rstrip("/") + "/inbox/soulseek"
        finally:
            con.close()

    def local(self, daemon_path: str) -> Path:
        """The backend's path of a downloaded file, as Echolot sees it (slskd's: already Echolot's)."""
        if self.opts.backend == "slskd":
            return Path(daemon_path)
        rel = Path(daemon_path).relative_to(self.opts.daemon_music)
        return self.run.paths.music / rel

    def song(self, want: Want, tries: int) -> Outcome:
        """Search and download the song; with nothing that fits under the artist's name, once more under its
        English one (祖堅 正慶 as Masayoshi Soken: peers name folders either way)."""
        outcome = self._song(want, tries, want.artist)
        if want.alias and outcome.action == "not found" and not (outcome.report or {}).get("fits"):
            again = self._song(want, tries, want.alias)
            if again.action != "not found" or (again.report or {}).get("fits"):
                return again
        return outcome

    def _song(self, want: Want, tries: int, artist_name: str) -> Outcome:
        loosen, opts = level(tries) if self.purpose == "search" else (False, {})
        settings = soulseek.search_settings(**opts, flac_only=self.purpose == "upgrade")
        artist, title, length = rules.search_terms(artist_name, want.title, want.length, loosen)
        job = self.daemon.search(artist, title, length, settings)
        try:
            self.daemon.wait(job, self.run.stop, time.monotonic() + SEARCH_SECONDS)
            found = self.daemon.results(job)
            con = self.run.connect()
            try:
                blocked = [r[0] for r in con.execute("SELECT name FROM blocked WHERE song_key = ?", (want.key,))]
                before = filing.rejected_before(con, want.key, fakes=self.purpose == "upgrade")
            finally:
                con.close()
            wanted = 0 if rules.mix_cut(want.title) else want.length
            judged, rejected, terms = (
                [],
                collections.Counter(),
                (wanted, opts.get("strict_artist", True), blocked, loosen),
            )
            for c in found:
                if filing.was_rejected(before, c.name, c.length, c.size):
                    rejected["rejected before"] += 1
                    continue
                verdict, rank, why = rules.prejudge(want.artist, want.title, c.path, c.length, *terms, want.aliases)
                if verdict != rules.REJECT:
                    judged.append((rank, c.rank, c))
                else:
                    rejected[kind(why)] += 1
            depth = stage(tries) if self.purpose == "search" else 0
            report = {
                "stage": depth,
                "results": len(found),
                "fits": len(judged),
                "rejected": dict(rejected),
                "tried": [],
            }
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
        finally:
            self.done(job)

    def done(self, search_job: str) -> None:
        """The song's search is over: slskd deletes it (Sockseek forgets its jobs by itself)."""
        if close := getattr(self.daemon, "close", None):
            close(search_job)

    def attempt(self, search_job: str, c: soulseek.Candidate, want: Want, tries: int, settings: dict) -> Outcome:
        name = uuid.uuid4().hex[:12]
        local_dir = self.local_inbox / name
        try:
            job = self.daemon.download(search_job, c, f"{self.daemon_inbox}/{name}", settings)
            t = self.watch(job, want.key)
            if t.state != "done" or not t.path:
                track(self.run, want.key, "searching")
                return Outcome("failed", t.reason)
            track(self.run, want.key, "checking")
            try:
                prepared = audio.prepare(self.local(t.path), self.keep_hires)
            except audio.Rejected as e:
                return Outcome("bad file", str(e))
            con = self.run.connect()
            try:
                heard = identity.check(con, want.isrc, prepared.path, bool(rules.mix_cut(want.title)))
                confirm = self.purpose == "upgrade" and want.key.startswith("soundcloud:")
                same = _same_audio(self.run, con, want, prepared) if confirm else None
                action, dest = filing.file_into(
                    con, self.run.paths, prepared.path, want, "soulseek", strict=True,
                    file_name=c.name, folders=c.folders, probable=self.purpose == "search",
                    tries=tries, fake=prepared.fake, heard=heard, confirm=confirm and not same,
                    same_audio=same[1] if same else "", replaces=same[0] if same else "", peer_bytes=c.size,
                )  # fmt: skip
                if dest and action in ("new", "upgrade"):
                    finish(self.run, con, dest, want)
            finally:
                con.close()
            return Outcome(action, str(dest.relative_to(self.run.paths.tracks)) if dest else "")
        finally:
            shutil.rmtree(local_dir, ignore_errors=True)

    def watch(self, job: str, key: str = "") -> soulseek.Transfer:
        """Wait for a download (of the song `key`: its progress noted, see track); give it up when it makes no
        progress for stall_minutes (queued at the peer: Sockseek keeps such a download alive while the peer
        serves others)."""
        start = last_change = time.monotonic()
        last_bytes = -1
        while True:
            t = self.daemon.transfer(job)
            if t.state != "running":
                return t
            now = time.monotonic()
            if key:
                track(self.run, key, "downloading", t.done_bytes, t.total_bytes)
            if t.done_bytes != last_bytes:
                last_bytes, last_change = t.done_bytes, now
            stalled = now - last_change > self.opts.stall_minutes * 60
            if stalled or now - start > TRANSFER_SECONDS or self.run.stop.is_set():
                self.daemon.cancel(job)
                why = "no progress (queued at the peer)" if stalled else "stopped"
                return soulseek.Transfer("cancelled", None, t.done_bytes, t.total_bytes, why)
            self.run.stop.wait(POLL_SECONDS)


STAGES = ("searching", "downloading", "checking", "other sources")  # a song's fetch (Run.fetching), in order


def track(run: "Run", key: str, stage: str | None, done: int = 0, total: int = 0) -> None:
    """Note how far a song's fetch is (None: over); downloading: its bytes done and total (0: unknown)."""
    if stage is None:
        run.fetching.pop(key, None)
    else:
        run.fetching[key] = {"stage": stage, "done": done, "total": total}


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
    """The song's cover, in place of one the uploader embedded (the given file: a SoundCloud download's
    artwork; else covers.Covers), and the artist picture from Spotify."""
    from echolot.jobs import covers

    try:
        song = tagging.lead(con, dest.relative_to(run.paths.tracks).as_posix(), want.key)
        jpg = cover.read_bytes() if cover and cover.is_file() else covers.Covers(con, run).of(song) if song else None
        if jpg:
            audio.embed_cover(dest, jpg, replace=True)
    except (spotify.SpotifyError, sc_api.SoundCloudError, OSError) as e:
        log.info("cover of %s: %s", dest.name, e)
    if cover and not want.key.startswith("spotify:"):
        return  # a SoundCloud download: its uploader's name is no Spotify artist to look up
    try:
        sp = spotify.Spotify(con, run.vault)
        if want.key.startswith("spotify:"):
            track = sp.track(want.key.removeprefix("spotify:"))
        else:
            track = sp.find_track(want.artist, want.title)
        if not track:
            return
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


SEARCHED = "s.service IN ('spotify', 'youtube', 'discover')"  # the songs searched for (SoundCloud's: downloaded)


def _missing(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Wanted Spotify, YouTube and Discover songs not in the library, greyed-out ones first (most at risk); one song
    per recording (recordings.twins)."""
    rows = con.execute(
        "SELECT s.*, coalesce(a.tries, 0) AS tries, coalesce(a.last_try, 0) AS last_try FROM wanted s "
        f"LEFT JOIN attempts a ON a.song_key = s.key WHERE {SEARCHED} AND s.file IS NULL "
        "ORDER BY s.unavailable IS NULL, s.artist, s.title"
    ).fetchall()
    twins = recordings.twins(rows)
    return [r for r in rows if r["key"] not in twins]


def _for(run: "Run", con: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """The songs of the users a run by hand is for ("search my missing songs now"); all for everyone's."""
    if run.only is None:
        return rows
    marks = ", ".join("?" * len(run.only))
    sql = (
        "SELECT ls.song_key FROM list_songs ls JOIN sources src ON src.key = ls.list_key "
        f"WHERE src.enabled AND src.user_id IN ({marks})"
    )
    keys = {r[0] for r in con.execute(sql, tuple(run.only))}
    return [r for r in rows if r["key"] in keys]


def _search(run: "Run", songs: list[sqlite3.Row], purpose: str) -> str:
    """Search and download songs, several at a time; attempts are counted per song as it ends. A run that
    goes on from one that gave way leaves out the songs that one did (Run.todo)."""
    songs = run.todo(songs)
    if not songs:
        return "nothing to search"
    fetcher = Fetcher(run, purpose)
    state = fetcher.daemon.status()["state"]  # reachable (it logs in to Soulseek with the first search)
    if "Disconnected" in state:  # it tried and failed: the songs wait (the daemon is restarted after 15 min)
        raise soulseek.DaemonError("Soulseek not logged in: the songs wait for it")
    con = run.connect()
    try:
        index = recordings.Index(catalog.Catalog.from_db(con))
    finally:
        con.close()
    deadline = time.monotonic() + 20 * 60 + 15 * len(songs)  # a run shares Soulseek with the others
    counts: dict[str, int] = {}
    done = 0
    lock, waiting = threading.Lock(), threading.Lock()

    def one(row: sqlite3.Row) -> None:
        busy = row["file"] or row["key"]
        with SEARCHING_LOCK:
            if busy in SEARCHING:
                return  # the run giving way searches it
            SEARCHING.add(busy)
        try:
            search(row)
        finally:
            with SEARCHING_LOCK:
                SEARCHING.discard(busy)

    def search(row: sqlite3.Row) -> None:
        nonlocal done
        if run.stop.is_set() or run.give_way.is_set() or time.monotonic() > deadline:
            return
        want = _want(row)
        track(run, want.key, "searching")
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
        finally:
            track(run, want.key, None)
        if outcome.action not in FOUND and not _logged_in(fetcher.daemon):
            # not the song's fault: no try counted; the rest waits until Soulseek is back, or the next run
            outcome = Outcome("interrupted", "Soulseek not logged in")
            if not _soulseek_back(run, fetcher.daemon, waiting) and not run.give_way.is_set():
                run.note("Soulseek not logged in for 20 min: the rest waits for the next run")
                run.stop.set()
        con = run.connect()
        try:
            _count(con, purpose, want.key, outcome.action, outcome.report)
        finally:
            con.close()
        with lock:
            run.handled.add(row["key"])
        log.info("%s: %s - %s: %s %s", purpose, want.artist, want.title, outcome.action, outcome.detail)
        run.note(f"{want.artist} – {want.title}: {outcome.action}" + (f" · {outcome.detail}" if outcome.detail else ""))
        with lock:
            done += 1
            counts[outcome.action] = counts.get(outcome.action, 0) + 1
            summary = ", ".join(f"{n} {a}" for a, n in sorted(counts.items()))
            run.say(f"{run.of(done, len(songs))} songs: {summary}", done, len(songs))

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


def _same_audio(run: "Run", con: sqlite3.Connection, want: Want, prepared: audio.Prepared) -> tuple[str, str] | None:
    """(library file, detail) when a FLAC for a SoundCloud song is the audio of the song's lossy file in
    another codec (identity.same_master): no review needed for that, only for other names."""
    row = con.execute("SELECT file FROM songs WHERE key = ?", (want.key,)).fetchone()
    if prepared.fake or not row or not row[0]:
        return None
    share = identity.same_master(prepared.path, run.paths.tracks / row[0])
    return (row[0], f"the same audio as your copy ({share:.3f})") if share >= identity.SAME_MASTER else None


def _want(row: sqlite3.Row) -> Want:
    """The song to search for; a SoundCloud title without its release decoration (tagging.clean_title)."""
    want = Want.of(row)
    if row["service"] == "soundcloud":
        want = dataclasses.replace(want, title=tagging.clean_title(want.title, want.artist))
    return want


SOULSEEK_WAIT = 20 * 60  # seconds a run waits for a lost Soulseek login (the daemon's watchdog: 15 min)
SOULSEEK_POLL = 15  # seconds between looks meanwhile


def _soulseek_back(run: "Run", daemon: soulseek.Daemon, waiting: threading.Lock) -> bool:
    """Soulseek dropped the login (a server outage, the daemon restarted): wait until the daemon is logged
    in again or can log in (a fresh one: state None, it logs in with the next search), at most
    SOULSEEK_WAIT, while the run is not stopped and does not give way. One song's thread waits, the
    others behind it. True if the run can go on."""
    with waiting:
        end = time.monotonic() + SOULSEEK_WAIT
        while not (run.stop.is_set() or run.give_way.is_set()):
            try:
                st = daemon.status()
            except soulseek.DaemonError:
                st = {"ready": False, "state": "daemon not reachable"}
            if st["ready"] or st["state"] in ("", "None"):
                return True
            if time.monotonic() > end:
                return False
            run.say(f"waiting for Soulseek ({st['state']})")
            run.stop.wait(SOULSEEK_POLL)
        return False


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
    """Spotify lists: read the followed lists that changed (a few requests when none did). A new song (never
    searched) starts New songs search (search_new), so Soulseek jobs only make way for real work."""
    from echolot.jobs import lists

    message = lists.fetch_spotify(run)
    con = run.connect()
    try:
        new = sum(1 for r in _missing(con) if not r["tries"])
    finally:
        con.close()
    if new:
        run.after.add("search_new")
    return message + (f"; {new} new songs to search" if new else "")


def search_new(run: "Run") -> str:
    """New songs search (started by Spotify and YouTube lists): the songs never searched, the library asked
    first (the same recording under other names is linked), then Soulseek. One not found goes to the YouTube
    and SoundCloud search right after (fallback); a later search is the evening search's."""
    parts = []
    con = run.connect()
    try:
        catalog.refresh(con, run.paths.tracks)
        if linked := recordings.link_isrc(con, run.paths):  # the library has them under other names
            catalog.match_songs(con)
            parts.append(f"{linked} linked by ISRC")
        songs = [r for r in _missing(con) if not r["tries"]]
    finally:
        con.close()
    if not songs:
        return "; ".join(parts) or "no new songs"
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
    daily, weekly after WEEKLY_AFTER searches without a find; started by hand, every one now."""
    con = run.connect()
    try:
        catalog.refresh(con, run.paths.tracks)
        if recordings.link_isrc(con, run.paths):
            catalog.match_songs(con)
        now, missing = time.time(), _for(run, con, _missing(con))
        by_hand = run.trigger == "manual"
        songs = [r for r in missing if by_hand or now >= next_search(r["tries"], r["last_try"])]
    finally:
        con.close()
    if not songs:
        return f"{len(missing)} missing songs, none due" if missing else "no missing songs"
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
    song is not searched while one waits there; a SoundCloud song whose file a Spotify (or YouTube) song has
    is upgraded as that one. Close matches are not upgraded (a FLAC found would be the song, not the version
    taken)."""
    con = run.connect()
    try:
        catalog.refresh(con, run.paths.tracks)
        rows = con.execute(
            "SELECT s.*, coalesce(u.tries, 0) AS tries, coalesce(u.last_try, 0) AS last_try FROM wanted s "
            "JOIN files f ON f.path = s.file LEFT JOIN upgrades u ON u.song_key = s.key "
            f"WHERE f.quality != 'lossless' AND NOT s.close_match AND ({SEARCHED} OR NOT EXISTS "
            "(SELECT 1 FROM wanted o WHERE o.service != 'soundcloud' AND o.file = s.file)) ORDER BY last_try"
        ).fetchall()
        with con:  # songs that are lossless now or left every list
            con.execute(
                "DELETE FROM upgrades WHERE song_key NOT IN (SELECT s.key FROM wanted s JOIN files f "
                "ON f.path = s.file WHERE f.quality != 'lossless')"
            )
        rows = run.todo(rows)  # (without the songs a run that gave way did)
        # what a run that gave way left, else every song (by hand) or the batch
        batch = run.budget or (len(rows) if everything else options.get(con, options.Soulseek).upgrade_batch)
    finally:
        con.close()
    now, seen, songs = time.time(), set(), []
    by_hand = everything or run.trigger == "manual"  # Run now: whatever their wait (the batch still counts)
    for r in rows:
        if r["service"] == "soundcloud" and filing.in_review(run.paths, r["artist"], _want(r).title):
            continue  # its FLAC waits for your answer
        if r["file"] not in seen and (by_hand or due(r["tries"], r["last_try"], *UPGRADE_WAIT, now)):
            seen.add(r["file"])
            songs.append(r)
    run.say(f"{len(rows)} lossy songs, {len(songs[:batch])} searched now")
    if not songs:
        return f"{len(rows)} lossy songs, none due"
    return _search(run, songs[:batch], "upgrade")


# ---------------------------------------------------------------- search fallback (home IP)


def fallback(run: "Run") -> str:
    """Songs Soulseek did not find (started by the sync right after its search), and SoundCloud songs
    SoundCloud hands out to nobody: search YouTube, then SoundCloud, each song at most once a week, the
    ones never tried first. The first result that passes the same checks as a Soulseek download is filed;
    the rest are tried in order; a download the library has under other names (its audio) is linked instead.
    The result is lossy: the FLAC upgrade looks for a FLAC from 12 h later."""
    week = int(time.time()) + 1 if run.trigger == "manual" else int(time.time()) - 7 * 86400  # Run now: all
    con = run.connect()
    try:
        catalog.refresh(con, run.paths.tracks)
        songs = con.execute(
            "SELECT s.*, coalesce(a.tries, 0) AS tries FROM wanted s LEFT JOIN attempts a ON a.song_key = s.key "
            f"WHERE s.file IS NULL AND coalesce(a.last_fallback, 0) < ? AND (({SEARCHED} "
            "AND a.tries >= 1) OR (s.service = 'soundcloud' AND s.unavailable IS NOT NULL)) "
            "ORDER BY coalesce(a.last_fallback, 0) > 0, s.unavailable IS NULL",
            (week,),
        ).fetchall()
        twins = recordings.twins(songs)
        songs = [r for r in songs if r["key"] not in twins]
        songs = run.todo(_for(run, con, songs))  # (by hand: without the songs a run that gave way did)
        token = sc_api.any_token(con, run.vault)
    finally:
        con.close()
    if not songs:
        return "none due"
    ydl = ytdlp.YtDlp(run.data / "ytdlp", token)
    added = kept = linked = 0
    for n, row in enumerate(songs, 1):
        if run.stop.is_set() or run.give_way.is_set():  # paused or a more urgent job: that song was the last
            run.left = 0 if run.stop.is_set() else len(songs) - n + 1
            break
        want = Want.of(row)
        run.say(f"{run.of(n, len(songs))}: {want.artist} – {want.title}", n - 1, len(songs))
        con = run.connect()
        try:
            with con:
                con.execute(
                    "INSERT INTO attempts (song_key, tries, last_fallback) VALUES (?, 0, ?) ON CONFLICT "
                    "(song_key) DO UPDATE SET last_fallback = excluded.last_fallback",
                    (want.key, int(time.time())),
                )
            listed = row["url"] if row["service"] == "youtube" else None
            track(run, want.key, "other sources")
            try:
                action, report = _fallback_song(
                    run, con, ydl, want, row["tries"], row["service"] != "soundcloud", listed
                )
            finally:
                track(run, want.key, None)
            with con:
                con.execute(
                    "UPDATE attempts SET fallback_result = ? WHERE song_key = ?", (json.dumps(report), want.key)
                )
        finally:
            con.close()
        run.handled.add(want.key)
        added += action in ("new", "upgrade")
        linked += action == "linked"
        kept += action == "mismatch"
        log.info("fallback: %s - %s: %s", want.artist, want.title, action)
        run.note(f"{want.artist} – {want.title}: {action}")
    shutil.rmtree(run.paths.inbox("fallback"), ignore_errors=True)
    run.after.add("library")
    return (
        f"{added} of {len(songs)} songs found"
        + (f", {linked} in the library under other names" if linked else "")
        + (f", {kept} of another length kept for review" if kept else "")
        + (f"; gave way, {run.left} left" if run.left else "")
    )


def _fallback_song(
    run: "Run",
    con: sqlite3.Connection,
    ydl: ytdlp.YtDlp,
    want: Want,
    tries: int,
    strict_probable: bool,
    listed: str | None = None,
) -> tuple[str, dict]:
    """Search YouTube, then SoundCloud, and file the first result that passes the checks; a YouTube song's
    own video (`listed`) comes first, as the song (it is, by its list) when it is as long, else as a result.
    Failing that, the result closest in length that names exactly this song but is another length (an
    official video often has its own edit) is downloaded and kept for review: only a listener can tell. One
    at a time; one discarded in review is not kept again. Returns the action and what the search saw, per site."""

    def fetch(site: str, r: dict) -> tuple[str, str]:
        track(run, want.key, "downloading")
        got, error = ydl.fetch(r["url"], run.paths.inbox("fallback") / want.key.replace(":", "-"), run.stop)
        if not got:
            track(run, want.key, "other sources")
            return "download failed", error
        track(run, want.key, "checking")
        try:
            prepared = audio.prepare(got, options.get(con, options.Files).keep_hires)
        except audio.Rejected as e:
            return "bad file", str(e)
        source = "youtube" if site == "youtube" else "soundcloud-search"
        dur = want.length or audio.probe(prepared.path)[0]
        cat = catalog.Catalog.from_db(con)
        if same := recordings.already(run.paths, prepared.path, want.title, dur, recordings.Index(cat)):
            prepared.path.unlink(missing_ok=True)  # the library has it under other names
            recordings.link(con, run.paths, want.key, want, same, "the same audio")
            return "linked", ""
        if want.key.startswith("youtube:") and (
            edit := recordings.another_edit(run.paths, prepared.path, want, cat, con=con)
        ):
            e, share = edit  # a YouTube song named as a library song of another length, by its audio:
            if share >= recordings.SAME_EDIT:  # that song, in another edit: linked, nothing filed twice
                prepared.path.unlink(missing_ok=True)
                recordings.link(con, run.paths, want.key, want, e, f"another edit, audio {share:.2f}")
                return "linked", ""
            kept = filing.keep(run.paths, prepared.path, want.artist, want.title, source)  # probably: you decide
            info = {"source": source, "song": want.key, "artist": want.artist, "title": want.title, "url": r["url"]}
            info |= {"found": r["title"], "file_name": r["title"], "wanted_seconds": round(want.length)}
            filing.event(
                con, run.paths, "mismatch", kept, reason=recordings.EDIT_REASON.format(path=e.path, share=share), **info
            )
            return EDIT_REVIEW, ""
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
    own = _video(ydl, listed, run.stop) if listed else None
    named = own | {"title": f"{want.artist} - {want.title}", "uploader": want.artist} if own else {}
    if (
        own
        and (not wanted or abs(own["duration"] - wanted) <= 3)
        and not filing.was_rejected(before, named["title"], own["duration"])
    ):
        action, detail = fetch("youtube", named)
        report["listed"] = [own["title"], action, detail]
        if action in FOUND:
            return action, report
        own = None  # tried
    for site in ("youtube", "soundcloud"):
        found = [r for q in queries[site] for r in ydl.search(q, site, run.stop)]
        results = list({_video_id(r["url"]): r for r in ([own] if own and site == "youtube" else []) + found}.values())
        if listed and not own:
            results = [r for r in results if _video_id(r["url"]) != _video_id(listed)]
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


def _video(ydl: ytdlp.YtDlp, url: str, stop: threading.Event) -> dict | None:
    """A video as a search result: {url, title, uploader, duration}; None if it does not play."""
    meta = ydl.meta(url, stop)
    if not meta or not meta.get("formats"):
        return None
    uploader = meta.get("uploader") or meta.get("channel") or ""
    return {"url": url, "title": meta.get("title") or "", "uploader": uploader, "duration": meta.get("duration") or 0}


def _video_id(url: str) -> str:
    """A result's video, however its address is written (a search lists https://www.youtube.com/watch?v=)."""
    return (urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("v") or [url])[0]


def _other_length(want: Want, path: str, seconds: float | None, wanted: float) -> bool:
    """A result that names exactly the song but is another length (from 2/3 to 1.5 times as long)."""
    if not (wanted and seconds and 2 / 3 <= seconds / wanted <= 1.5):
        return False
    return rules.prejudge(want.artist, want.title, path, seconds, 0)[:2] == (rules.ACCEPT, 0)
