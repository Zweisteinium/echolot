"""Covers from your lists: a library file carries its song's cover (on Spotify its own release's: its single,
else its album, else the list's release; the SoundCloud artwork), not the one its uploader embedded (a
compilation, a remaster). New files get it when filed (acquire.pictures) and are noted done; `run` gives it
to the files not done yet, keeping the old picture in <data>/cover-backups/<file>.jpg and noting the files
done in done.txt, so a stopped run goes on where it was; `run_all` to every file again. A file changed in
the last 10 minutes waits.
"""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.request import Request, urlopen

from echolot.library import audio, filing, rules, tagging
from echolot.services import soundcloud as sc_api
from echolot.services import spotify

if TYPE_CHECKING:
    from echolot.jobs.worker import Run

log = logging.getLogger(__name__)
KINDS = ("flac", "mp3", "m4a", "opus", "ogg")  # the files a cover is embedded in (a WAV keeps none)
RELEASES = 50  # releases of a recording one search finds (a hit has dozens on compilations)
OWN_YEARS = 2  # a single or album this much later than the recording's first release is a re-release


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


class Covers:
    """The cover of a song, each Spotify album and picture fetched once."""

    def __init__(self, con: sqlite3.Connection, run: "Run") -> None:
        self.con = con
        self.cache: dict[str, bytes | None] = {}
        self.token = sc_api.any_token(con, run.vault)
        try:
            self.sp: spotify.Spotify | None = spotify.Spotify(con, run.vault)
        except spotify.SpotifyError:
            self.sp = None

    def of(self, song: sqlite3.Row) -> bytes | None:
        """The song's cover: on Spotify its own release's (own_release: its single, else its album, by the
        releases of its ISRC), else the list's release's; its SoundCloud artwork; a YouTube or Discover song's
        by its ISRC too, else the first release found (a Discover song without one: the picture Discover
        showed, large); None if it has none."""
        service, _, sid = song["key"].partition(":")
        if service in ("spotify", "youtube", "discover") and self.sp and song["isrc"]:
            time.sleep(0.25)
            hits = self.sp.search(f"isrc:{song['isrc']}", RELEASES)
            if album := own_release(hits, song):
                return self._fetch(album["id"], album["images"][0]["url"])
            if service != "spotify" and hits:
                service, sid = "spotify", hits[0]["id"]
        if service == "spotify" and self.sp:
            time.sleep(0.25)  # gently: thousands of songs, one request each
            album = self.sp.track(sid).get("album") or {}
            images = album.get("images") or []
            return self._fetch(album.get("id") or sid, images[0]["url"] if images else None)
        if service == "soundcloud" and self.token:
            return self._fetch(song["key"], sc_api.artwork(self.token, sid))
        if service == "discover":
            return self._fetch(song["key"], self._discovered(sid))
        return None

    def _discovered(self, sid: str) -> str | None:
        """The picture Discover showed of a song (web/subsonic: Deezer's, Apple Music's, SoundCloud's), in its
        large size."""
        row = self.con.execute("SELECT data FROM discover_songs WHERE id = ?", (f"ex-{sid}",)).fetchone()
        url = json.loads(row[0]).get("cover") if row else None
        return url.replace("250x250", "1000x1000").replace("-t300x300.", "-t500x500.") if url else None

    def _fetch(self, key: str, url: str | None) -> bytes | None:
        if key not in self.cache:
            self.cache[key] = download(url) if url else None
        return self.cache[key]


def own_release(tracks: list[dict], song: sqlite3.Row | dict) -> dict | None:
    """The song's own release among the Spotify tracks of its ISRC (a search's), with a cover: a single, else
    an album, by one of its artists (not Various Artists) and out with the recording (OWN_YEARS of its first
    release: not a re-release years later); the earliest, or the list's own release (by its album's name) when
    it is one of the kind. None when there is only a compilation or a late release: the list's release."""
    isrc = (song["isrc"] or "").upper()
    same = [
        t
        for t in tracks
        if ((t.get("external_ids") or {}).get("isrc") or "").upper() == isrc and (t.get("album") or {}).get("images")
    ]
    if not same:
        return None
    first = min(_year(t["album"]) for t in same)
    field = dict(song)  # (a row or a dict, some without artists or album)
    names = [field.get("artist"), *json.loads(field.get("artists") or "[]")]
    artists = {rules.fold(a) for a in names if a}

    def kind(album: dict) -> int:  # 0 single, 1 album, 2 neither
        own = artists & {rules.fold(a.get("name")) for a in album.get("artists") or []}
        if not own or _year(album) - first > OWN_YEARS:
            return 2
        return {"single": 0, "album": 1}.get(album.get("album_type") or "", 2)

    best = min(kind(t["album"]) for t in same)
    if best == 2:
        return None
    albums = [t["album"] for t in same if kind(t["album"]) == best]
    listed = rules.fold(field.get("album"))
    return next((a for a in albums if listed and rules.fold(a.get("name")) == listed), None) or min(albums, key=_year)


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
        counts = {"replaced": 0, "already the song's": 0, "without a song or cover": 0, "failed": 0}
        with done_list.open("a", encoding="utf-8") as log_done:
            for n, rel in enumerate(todo, 1):
                if run.stop.is_set() or run.give_way.is_set():
                    run.left = 0 if run.stop.is_set() else len(todo) - n + 1
                    break
                run.say(f"{run.of(n, len(todo))}: {rel}", n - 1, len(todo))
                try:
                    outcome = _one(run, con, covers, rel, backups)
                except Exception:  # one file's trouble (a cut-off download, a tag mutagen can't read) ends no run
                    log.exception("cover of %s", rel)
                    outcome = "failed"
                if outcome in ("replaced", "failed"):
                    run.note(f"{rel}: {outcome}")
                counts[outcome] = counts.get(outcome, 0) + 1
                run.handled.add(rel)
                if outcome not in ("failed", "changed lately"):
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


def _one(run: "Run", con: sqlite3.Connection, covers: Covers, rel: str, backups: Path) -> str:
    path = run.paths.tracks / rel
    song = tagging.lead(con, rel)
    if song is None or path.suffix.lower().lstrip(".") not in KINDS or not path.is_file():
        return "without a song or cover"
    if time.time() - path.stat().st_mtime < 600:
        return "changed lately"  # just filed or retagged: the next run
    try:
        jpg = covers.of(song)
    except (spotify.SpotifyError, sc_api.SoundCloudError, OSError) as e:
        log.info("cover of %s: %s", rel, e)
        return "failed"
    if not jpg:
        return "without a song or cover"
    old = audio.picture(path)
    if old == jpg:
        return "already the song's"
    if old:
        backup = backups / f"{rel}.{'png' if old[:4] == b'\x89PNG' else 'jpg'}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            backup.write_bytes(old)
    with filing.LOCK:
        return "replaced" if audio.embed_cover(path, jpg, replace=True) else "failed"
