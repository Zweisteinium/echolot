"""Covers from your lists: a library file carries its song's cover, not the one its uploader embedded (a
compilation, a remaster): on Spotify the list's release's when that is its artist's own single or album and
covers are as listed (options.Files, the default), else its own release's (own_release: its original single,
else its album, by its ISRC, else by its names; else the list's release), stored with the album, date and
track its file is tagged with and what the list's release is beside it (song_releases, tagging.release_of);
the SoundCloud artwork; a YouTube video's picture as a square. New files get it when filed (acquire.pictures)
and are noted done; `run` gives it to the files not done yet, keeping the old picture in
<data>/cover-backups/<file>.jpg and noting the files done in done.txt, so a stopped run goes on where it was;
`run_all` to every file again; `resync` to the files a switch of the setting changes. A file changed in the
last 10 minutes waits. Pictures come as uploaded where the site keeps that (largest), and a file's picture
that is the same one, larger, stays (keeps).
"""

import datetime
import json
import logging
import re
import sqlite3
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.request import Request, urlopen

import numpy as np

from echolot import db
from echolot.library import audio, filing, rules, tagging
from echolot.services import soundcloud as sc_api
from echolot.services import spotify

if TYPE_CHECKING:
    from echolot.jobs.worker import Run

log = logging.getLogger(__name__)
KINDS = ("flac", "mp3", "m4a", "opus", "ogg")  # the files a cover is embedded in (a WAV keeps none)
RELEASES = 50  # releases of a recording one search finds (a hit has dozens on compilations)
OWN_YEARS = 2  # a single or album this much later than the recording's first release is a re-release
NAMES_LENGTH = 3  # s: a release found by the song's names (none of its own under its ISRC) is this long
SINGLE_TRACKS = 6  # a "single" with more tracks is an album Spotify calls one (singles and EPs have up to 6)
# what a release adds to a song's title: " - Remastered 2011", " - 2011", " - From "X" Soundtrack",
# " - Single Version", "(2011 Remaster)"; the song by its names is the title without it
_ADDED = re.compile(
    r"\s+-\s+(?:[^-]*\b(?:remaster(?:ed)?|from|mono|stereo|single version|album version)\b[^-]*|\d{4})$"
    r"|\s*[\(\[][^\)\]]*\bremaster(?:ed)?\b[^\)\]]*[\)\]]",
    re.I,
)
VIDEO = re.compile(r"[\w-]{11}")
# Spotify's 640 px and SoundCloud's 500 px picture addresses, and those of the picture as uploaded
ORIGINALS = (("ab67616d0000b273", "ab67616d000082c1"), ("-t500x500.", "-original."))
MAX_PICTURE = 6 << 20  # bytes: an original larger than this (a print scan) is left for the usual size
SAME_PICTURE = 4  # mean difference (0-255) of two pictures' 16x16 grey pixels up to which they are one picture


def journal(data: Path) -> Path:
    """The list of the files done (relative to the library), one per line."""
    return data / "cover-backups" / "done.txt"


def note_done(data: Path, rel: str) -> None:
    """A file that got its song's cover when it was filed (acquire.pictures): runs leave it out."""
    path = journal(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(rel + "\n")


def download(url: str) -> bytes:
    with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as r:
        return r.read()


def largest(url: str) -> bytes:
    """The picture at `url` as uploaded where Spotify or SoundCloud keep it (a JPEG up to MAX_PICTURE), else as
    given."""
    for small, big in ORIGINALS:
        if small in url:
            try:
                jpg = download(url.replace(small, big))
            except OSError:
                break
            if jpg[:3] == b"\xff\xd8\xff" and len(jpg) <= MAX_PICTURE:
                return jpg
            break
    return download(url)


def width(picture: bytes) -> int:
    """A JPEG's or PNG's width, from its header; 0 if unknown."""
    if picture[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(picture[16:20])
    i = 2
    while picture[:2] == b"\xff\xd8" and i + 9 <= len(picture) and picture[i] == 0xFF:
        marker = picture[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # a frame header: height, width
            return int.from_bytes(picture[i + 7 : i + 9])
        i += 2 + int.from_bytes(picture[i + 2 : i + 4])
    return 0


def _grey(picture: bytes) -> np.ndarray | None:
    """A picture as 16x16 grey pixels (ffmpeg); None if it cannot be read."""
    cmd = ["ffmpeg", "-v", "error", "-i", "-", "-vf", "scale=16:16:flags=area,format=gray", "-f", "rawvideo", "-"]
    try:
        out = subprocess.run(cmd, input=picture, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return np.frombuffer(out.stdout, np.uint8).astype(int) if out.returncode == 0 and len(out.stdout) == 256 else None


def keeps(old: bytes | None, new: bytes) -> bool:
    """Whether a file's picture `old` stays rather than `new`: the same picture, larger (the release's own
    print-size cover over a store's 640 px)."""
    if not old or width(old) <= width(new):
        return False
    a, b = _grey(old), _grey(new)
    return a is not None and b is not None and float(np.abs(a - b).mean()) <= SAME_PICTURE


class Covers:
    """The cover of a song, each Spotify album and picture fetched once; a Spotify track the song's searches
    found is not asked for again (track)."""

    def __init__(self, con: sqlite3.Connection, run: "Run") -> None:
        self.con = con
        self.as_listed = tagging.as_listed(con)
        self.cache: dict[str, bytes | None] = {}
        self.found: dict[str, dict] = {}  # Spotify track id -> the track, as the last song's searches gave it
        self.token = sc_api.any_token(con, run.vault)
        try:
            self.sp: spotify.Spotify | None = spotify.Spotify(con, run.vault)
        except spotify.SpotifyError:
            self.sp = None

    def of(self, song: sqlite3.Row) -> bytes | None:
        """The song's cover: on Spotify its own release's (release), else (or covers as listed, when the list's
        release is its artist's own) the list's release's; its SoundCloud artwork; a YouTube or Discover song's
        own release's too, else the first release of its ISRC, else a Discover song's picture Discover showed
        (large), a YouTube song's video picture (thumbnail); None if it has none."""
        service, _, sid = song["key"].partition(":")
        if service in ("spotify", "youtube", "discover") and self.sp:
            own, hits = self.release(song)
            if own and not (self.as_listed and own["listed"] == "own"):
                return self._fetch(own["album_id"], own["image"])
            if service != "spotify" and hits:
                service, sid = "spotify", hits[0]["id"]
        if service == "spotify" and self.sp:
            album = self.track(sid).get("album") or {}
            images = album.get("images") or []
            return self._fetch(album.get("id") or sid, images[0]["url"] if images else None)
        if service == "soundcloud" and self.token:
            return self._fetch(song["key"], sc_api.artwork(self.token, sid))
        if service == "discover":
            return self._fetch(song["key"], self._discovered(sid))
        if service == "youtube" and VIDEO.fullmatch(sid):
            if song["key"] not in self.cache:
                self.cache[song["key"]] = thumbnail(sid)
            return self.cache[song["key"]]
        return None

    def release(self, song: sqlite3.Row) -> tuple[dict | None, list[dict]]:
        """(the song's own release (own_release: by its ISRC, else by its names) as stored in song_releases,
        for its file's tags too; the tracks its ISRC search found). None, and nothing stored: the list's
        release is the song's."""
        assert self.sp is not None
        hits: list[dict] = []
        self.found = {}
        if song["isrc"]:
            time.sleep(0.25)  # gently: thousands of songs, one request each
            hits = self._seen(self.sp.search(f"isrc:{song['isrc']}", RELEASES))
        track, listed = pick(hits, song)
        how = "isrc"
        if track is None and not listed and (q := by_names(song)):
            time.sleep(0.25)
            track, how = own_release(self._seen(self.sp.search(q, RELEASES)), song, names=True), "names"
        kind = "same" if listed and track is not None else self.listed(song, track)  # (pick: the list's is its own)
        return store(self.con, song["key"], track, how, kind), hits

    def listed(self, song: sqlite3.Row, own: dict | None) -> str | None:
        """What a Spotify song's list release is beside its own release `own` (a track): listed_kind; None
        without one, or for another service's song."""
        service, _, sid = song["key"].partition(":")
        if own is None or service != "spotify":
            return None
        if own.get("id") == sid:
            return "same"
        album = own["album"]
        image = ((album.get("images") or [{}])[0] or {}).get("url") or ""
        mine = {"id": album["id"], "name": album.get("name") or "", "image": image}
        return listed_kind(self.track(sid).get("album") or {}, song, mine)

    def track(self, sid: str) -> dict:
        """A Spotify track: as the song's search found it (the same track: its album, its artists), else asked."""
        assert self.sp is not None
        if sid not in self.found:
            time.sleep(0.25)  # gently: thousands of songs, one request each
            self.found[sid] = self.sp.track(sid)
        return self.found[sid]

    def _seen(self, tracks: list[dict]) -> list[dict]:
        """`tracks` (a search's), noted for track: those whole (with their album and artists)."""
        self.found.update({t["id"]: t for t in tracks if t and t.get("id") and "album" in t and "artists" in t})
        return tracks

    def _discovered(self, sid: str) -> str | None:
        """The picture Discover showed of a song (web/subsonic: Deezer's, Apple Music's, SoundCloud's), in its
        large size."""
        row = self.con.execute("SELECT data FROM discover_songs WHERE id = ?", (f"ex-{sid}",)).fetchone()
        url = json.loads(row[0]).get("cover") if row else None
        return url.replace("250x250", "1000x1000").replace("-t300x300.", "-t500x500.") if url else None

    def _fetch(self, key: str, url: str | None) -> bytes | None:
        if key not in self.cache:
            self.cache[key] = largest(url) if url else None
        return self.cache[key]


def own_release(tracks: list[dict], song: sqlite3.Row | dict, names: bool = False) -> dict | None:
    """The track of the song's own release (pick); None: the list's release."""
    return pick(tracks, song, names)[0]


def pick(tracks: list[dict], song: sqlite3.Row | dict, names: bool = False) -> tuple[dict | None, bool]:
    """The track of the song's own release among Spotify tracks of its ISRC (a search's; `names`: found by its
    names, the same title without what a release adds and as long), with a cover: a single, else an album, by
    one of its artists (not Various Artists) and out with the recording (OWN_YEARS of its first release: not a
    re-release years later); the earliest, or the list's own release (by its album's name) when it is one of
    the kind (or an album that came out before the one found). None when there is only a compilation or a late
    release: the list's release. And whether the list's release is the song's own (then nothing else is looked
    for). By its names, a release crediting an artist the song does not have is another version."""
    field = dict(song)  # (a row or a dict, some without artists or album)
    artists = {rules.fold(a) for a in [field.get("artist"), *json.loads(field.get("artists") or "[]")] if a}
    isrc, core, length = (field.get("isrc") or "").upper(), _core(field.get("title")), field.get("length") or 0

    def same_song(t: dict) -> bool:
        if not names:
            return ((t.get("external_ids") or {}).get("isrc") or "").upper() == isrc
        by = {rules.fold(a.get("name")) for a in t.get("artists") or []}
        return (
            bool(by)
            and by <= artists
            and rules.feat_keys(t.get("name")) <= {rules.artist_key(a) for a in artists}
            and _core(t.get("name")) == core
            and rules.same_feat(t.get("name"), field.get("title"))
            and bool(length)
            and abs((t.get("duration_ms") or 0) / 1000 - length) <= NAMES_LENGTH
        )

    same = [t for t in tracks if (t.get("album") or {}).get("images") and same_song(t)]
    if not same:
        return None, False
    first = min(_year(t["album"]) for t in same)

    def kind(album: dict) -> int:  # 0 single, 1 album, 2 neither
        own = artists & {rules.fold(a.get("name")) for a in album.get("artists") or []}
        if not own or _year(album) - first > OWN_YEARS:
            return 2
        typ = album.get("album_type") or ""
        if typ == "single" and (album.get("total_tracks") or 0) > SINGLE_TRACKS:
            typ = "album"
        return {"single": 0, "album": 1}.get(typ, 2)

    best = min(kind(t["album"]) for t in same)
    if best == 2:
        return None, False
    own = [t for t in same if kind(t["album"]) == best]
    listed = rules.fold(field.get("album"))
    if found := next((t for t in own if listed and rules.fold(t["album"].get("name")) == listed), None):
        return found, True
    earliest = min(own, key=lambda t: _year(t["album"]))
    if best == 1 and _year({"release_date": field.get("released")}) < _year(earliest["album"]):
        return None, True  # the list's release came first (the search shows one release of several): the album
    return earliest, False


def by_names(song: sqlite3.Row | dict) -> str:
    """The Spotify search for a song's releases by its names (its title without what a release adds); '' when
    it has no length to compare."""
    field = dict(song)
    title = _ADDED.sub("", field.get("title") or "").strip()
    return f"track:{title} artist:{field.get('artist')}" if title and field.get("length") else ""


def _core(title: str | None) -> str:
    return rules.title_key(_ADDED.sub("", title or "").strip())


def store(con: sqlite3.Connection, key: str, track: dict | None, how: str, listed: str | None = None) -> dict | None:
    """Note a song's own release (song_releases; None: none, the list's) and what the list's release is beside
    it (`listed`: listed_kind), as stored."""
    with con:
        if track is None:
            con.execute("DELETE FROM song_releases WHERE song_key = ?", (key,))
            return None
        album, facts = track["album"], spotify.release_facts(track)
        row = {"song_key": key, "album_id": album["id"], "album": album.get("name") or "", **facts}
        row |= {"image": album["images"][0]["url"], "how": how, "checked": datetime.date.today().isoformat()}
        row["listed"] = listed
        con.execute(
            f"INSERT OR REPLACE INTO song_releases ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()),
        )
        return row


def thumbnail(video: str) -> bytes | None:
    """A YouTube video's picture as a square JPEG: the middle of its largest thumbnail (an art track's is its
    release's cover between bars); None if it has none."""
    for name, crop in (("maxresdefault", "ih:ih"), ("hqdefault", "270:270:105:45")):  # (hq: 4:3, the 16:9 in it)
        try:
            jpg = download(f"https://i.ytimg.com/vi/{video}/{name}.jpg")
        except OSError:
            continue
        cmd = ["ffmpeg", "-v", "error", "-f", "jpeg_pipe", "-i", "-", "-vf", f"crop={crop}", "-q:v", "2"]
        try:
            out = subprocess.run(
                [*cmd, "-f", "image2pipe", "-c:v", "mjpeg", "-"], input=jpg, capture_output=True, timeout=30
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.stdout if out.returncode == 0 and out.stdout[:3] == b"\xff\xd8\xff" else None
    return None


def listed_kind(album: dict, song: sqlite3.Row | dict, own: dict) -> str:
    """What a Spotify song's release as its list has it (`album`) is beside its own release (`own`: id, name,
    image): same (also another copy of it, as Spotify keeps one per market: the same name and picture), own
    (its artist's single or album, any year: covers as listed take it) or other (a compilation, another
    artist's: never)."""
    if album.get("id") == own["id"]:
        return "same"
    field = dict(song)
    artists = {rules.fold(a) for a in [field.get("artist"), *json.loads(field.get("artists") or "[]")] if a}
    by = {rules.fold(a.get("name")) for a in album.get("artists") or []}
    if album.get("album_type") not in ("single", "album") or not artists & by:
        return "other"
    image = ((album.get("images") or [{}])[0] or {}).get("url") or ""
    named = rules.title_key(album.get("name")) == rules.title_key(own["name"])
    return "same" if named and image and own["image"] and same_picture(image, own["image"]) else "own"


def same_picture(a: str, b: str) -> bool:
    """Whether two Spotify covers (their addresses) are one picture, by their 64 px copies (keeps' measure)."""
    small = [url.replace("ab67616d0000b273", "ab67616d00004851") for url in (a, b)]
    try:
        x, y = (_grey(download(url)) for url in small)
    except OSError:
        return False
    return x is not None and y is not None and float(np.abs(x - y).mean()) <= SAME_PICTURE


def _year(album: dict) -> float:
    """When a release came out, in years (Spotify's date: 2015, 2015-07 or 2015-07-31); unknown: never."""
    parts = (album.get("release_date") or "").split("-")
    try:
        year = int(parts[0])
    except ValueError:
        return float("inf")
    month = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    day = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    return year + (month - 1) / 12 + (day - 1) / 365


def run(run: "Run") -> str:
    """Give every library file not done yet its song's cover (see the module)."""
    backups = run.data / "cover-backups"
    backups.mkdir(parents=True, exist_ok=True)
    done_list = journal(run.data)
    done = set(done_list.read_text(encoding="utf-8").splitlines()) if done_list.exists() else set()
    con = run.connect()
    try:
        covers = Covers(con, run)
        todo = run.todo([r[0] for r in con.execute("SELECT path FROM files ORDER BY path") if r[0] not in done], str)
        counts = {"replaced": 0, "right already": 0, "no cover found": 0, "failed": 0, "retagged": 0}
        with done_list.open("a", encoding="utf-8") as log_done:
            for n, rel in enumerate(todo, 1):
                if run.stop.is_set() or run.give_way.is_set():
                    run.left = 0 if run.stop.is_set() else len(todo) - n + 1
                    break
                run.say(f"{run.of(n, len(todo))}: {rel}", n - 1, len(todo))
                try:
                    outcome = _one(run, con, covers, rel, backups)
                    if outcome not in ("failed", "just changed") and _retag(run, con, rel, backups):
                        counts["retagged"] += 1
                except Exception:  # one file's trouble (a cut-off download, a tag mutagen can't read) ends no run
                    log.exception("cover of %s", rel)
                    outcome = "failed"
                if outcome in ("replaced", "failed"):
                    run.note(f"{rel}: {outcome}")
                counts[outcome] = counts.get(outcome, 0) + 1
                run.handled.add(rel)
                if outcome not in ("failed", "just changed"):
                    log_done.write(rel + "\n")
                    log_done.flush()
    finally:
        con.close()
    left = f"; {run.left} left" if run.left else ""
    return ", ".join(f"{v} {k}" for k, v in counts.items() if v) + left if any(counts.values()) else "nothing to do"


def run_all(job: "Run") -> str:
    """Replace all: every library file its song's cover again, done before or not (the list of the files done
    starts anew, so Add missing goes on from a stopped run; one that goes on after giving way keeps it)."""
    if not job.skip:
        journal(job.data).unlink(missing_ok=True)
    return run(job)


# ---------------------------------------------------------------- the Covers switch

MODE = "covers_mode"  # meta: listed or original, as the library's covers and album tags are ('' or none: not
# known, while a switch is under way or for a library from before the switch)
RETRY = 3600  # s: a switch not done (Spotify failing, a file just changed) is tried again this long after
FAILURES = 3  # files in a row failing end a switch's run (Spotify resting, the network down)
BATCH = 50  # tracks to a request (Spotify's limit)


def _mode(con: sqlite3.Connection) -> str:
    return "listed" if tagging.as_listed(con) else "original"


def resync_due(con: sqlite3.Connection) -> bool:
    """Whether files wait for the release the Covers switch gives them (resync), not tried in the last RETRY s."""
    if db.get_meta(con, MODE) == _mode(con):
        return False
    last = con.execute("SELECT started FROM jobs WHERE name = 'covers_release'").fetchone()
    then = datetime.datetime.fromisoformat(last[0]).timestamp() if last and last[0] else 0
    return time.time() - then >= RETRY


def switched(con: sqlite3.Connection) -> list[str]:
    """The files whose cover and album tags the Covers switch changes: of another song or another release
    either way (tagging.lead, tagging.release_of)."""

    def way(rel: str, listed: bool) -> tuple[str, str] | None:
        song = tagging.lead(con, rel, listed=listed)
        own = tagging.release_of(con, song, listed) if song is not None else None
        return (song["key"], own["album_id"] if own else "") if song is not None else None

    return [rel for (rel,) in con.execute("SELECT path FROM files ORDER BY path") if way(rel, True) != way(rel, False)]


def fill_listed(con: sqlite3.Connection, covers: Covers) -> int:
    """What each Spotify song's list release is beside its own (song_releases.listed), where not known yet: its
    track as the list has it, 50 to a request. How many were asked about."""
    if covers.sp is None:
        return 0
    sql = (
        "SELECT r.song_key, r.album_id, r.album, r.image, s.artist, s.artists FROM song_releases r "
        "JOIN songs s ON s.key = r.song_key WHERE r.listed IS NULL AND r.song_key LIKE 'spotify:%'"
    )
    rows = con.execute(sql).fetchall()
    for n in range(0, len(rows), BATCH):
        part = {r["song_key"].removeprefix("spotify:"): r for r in rows[n : n + BATCH]}
        kinds = []
        for t in covers.sp.get(f"/tracks?ids={','.join(part)}").get("tracks") or []:
            if t and (r := part.get(t.get("id"))) is not None:
                own = {"id": r["album_id"], "name": r["album"], "image": r["image"]}
                kinds.append((listed_kind(t.get("album") or {}, r, own), r["song_key"]))
        with con:
            con.executemany("UPDATE song_releases SET listed = ? WHERE song_key = ?", kinds)
    return len(rows)


def switch_journal(data: Path) -> Path:
    """The files done for a switch: its mode on the first line, then one file per line."""
    return data / "cover-backups" / "switched.txt"


def resync(run: "Run") -> str:
    """After the Covers switch (options.Files.covers_as_listed): the files whose cover and album tags it
    changes (switched) get them, the rest stay; first what the songs' list releases are, where not known yet
    (fill_listed). Done files are noted (switch_journal), so a stopped run goes on where it was (upkeep starts
    it again, RETRY after); MODE is set once all are done. Spotify failing FAILURES times in a row ends the run."""
    backups = run.data / "cover-backups"
    backups.mkdir(parents=True, exist_ok=True)
    con = run.connect()
    try:
        mode = _mode(con)
        if db.get_meta(con, MODE) == mode:
            return "nothing to do"
        with con:
            db.set_meta(con, MODE, "")  # under way: a stop, or a switch back meanwhile, finds it so
        covers = Covers(con, run)
        try:
            asked = fill_listed(con, covers)
        except spotify.SpotifyError as e:
            run.note(f"Spotify: {e}. The switch waits for the next try")
            return "Spotify failed: tried again in an hour"
        journal_path = switch_journal(run.data)
        lines = journal_path.read_text(encoding="utf-8").splitlines() if journal_path.exists() else []
        if not lines or lines[0] != mode:
            lines = [mode]
            journal_path.write_text(mode + "\n", encoding="utf-8")
        todo = run.todo([rel for rel in switched(con) if rel not in set(lines[1:])], str)
        counts: Counter[str] = Counter()
        failed, through = 0, False
        with journal_path.open("a", encoding="utf-8") as noted:
            for n, rel in enumerate(todo, 1):
                if run.stop.is_set() or run.give_way.is_set():
                    run.left = 0 if run.stop.is_set() else len(todo) - n + 1
                    break
                run.say(f"{run.of(n, len(todo))}: {rel}", n - 1, len(todo))
                try:
                    outcome = _one(run, con, covers, rel, backups)
                    retagged = outcome not in ("failed", "just changed") and _retag(run, con, rel, backups)
                except Exception:  # one file's trouble ends no run
                    log.exception("cover of %s", rel)
                    outcome, retagged = "failed", False
                counts[outcome] += 1
                counts["retagged"] += retagged
                failed = failed + 1 if outcome == "failed" else 0
                if outcome in ("failed", "just changed"):
                    run.note(f"{rel}: {outcome}")
                    if failed >= FAILURES:
                        run.note("Covers keep failing: the rest waits for the next try")
                        break
                    continue
                run.handled.add(rel)
                noted.write(rel + "\n")
                noted.flush()
            else:
                through = True  # (not stopped, given way or ended by failures)
        if through and not counts["failed"] and not counts["just changed"]:
            with con:
                db.set_meta(con, MODE, mode)
    finally:
        con.close()
    looked = [f"{asked} songs' list releases looked up"] if asked else []
    parts = [f"{v} {k}" for k, v in counts.items() if v] + looked + ([f"{run.left} left"] if run.left else [])
    return ", ".join(parts) or "no file changes"


def _retag(run: "Run", con: sqlite3.Connection, rel: str, backups: Path) -> bool:
    """The file's tags of the release its cover is of (tagging.for_file), the old ones kept in
    cover-backups/tags.jsonl first; not a file whose tags name another song (tagging.conflict)."""
    path = run.paths.tracks / rel
    if not tagging.supported(path) or (tags := tagging.for_file(con, rel, path)) is None:
        return False
    now = tagging.read(path)
    if (not now["download"] and tagging.conflict(now["title"], tags.title)) or not tagging.differs(path, tags):
        return False
    with (backups / "tags.jsonl").open("a", encoding="utf-8") as f:
        f.write(tagging.backup_line(rel, path) + "\n")
    with filing.LOCK:
        tagging.write(path, tags)
    return True


def _one(run: "Run", con: sqlite3.Connection, covers: Covers, rel: str, backups: Path) -> str:
    path = run.paths.tracks / rel
    song = tagging.lead(con, rel)
    if song is None or path.suffix.lower().lstrip(".") not in KINDS or not path.is_file():
        return "no cover found"
    if time.time() - path.stat().st_mtime < 600:
        return "just changed"  # just filed or retagged: the next run
    try:
        jpg = covers.of(song)
    except (spotify.SpotifyError, sc_api.SoundCloudError, OSError) as e:
        log.info("cover of %s: %s", rel, e)
        return "failed"
    if not jpg:
        return "no cover found"
    old = audio.picture(path)
    if old == jpg or keeps(old, jpg):
        return "right already"
    if old:
        backup = backups / f"{rel}.{'png' if old[:4] == b'\x89PNG' else 'jpg'}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            backup.write_bytes(old)
    with filing.LOCK:
        return "replaced" if audio.embed_cover(path, jpg, replace=True) else "failed"
