"""Covers from your lists: a library file carries its song's cover (Spotify's album, the SoundCloud song's
artwork), not the one its uploader embedded (a compilation, a remaster). New files get it when they are
filed (acquire.pictures); `run` gives it to the files already there: the old picture is kept in
<data>/cover-backups/<file>.jpg first, and the files done are noted in done.txt, so a run that stopped
(paused, a deploy) goes on where it was. A file changed in the last 10 minutes waits for the next run.
"""

import logging
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.request import Request, urlopen

from echolot.library import audio, filing, tagging
from echolot.services import soundcloud as sc_api
from echolot.services import spotify

if TYPE_CHECKING:
    from echolot.jobs.worker import Run

log = logging.getLogger(__name__)
KINDS = ("flac", "mp3", "m4a", "opus", "ogg")  # the files a cover is embedded in (a WAV keeps none)


def download(url: str) -> bytes:
    with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as r:
        return r.read()


class Covers:
    """The cover of a song, each Spotify album and picture fetched once."""

    def __init__(self, con: sqlite3.Connection, run: "Run") -> None:
        self.cache: dict[str, bytes | None] = {}
        self.token = run.vault.get(con, sc_api.TOKEN)
        try:
            self.sp: spotify.Spotify | None = spotify.Spotify(con, run.vault)
        except spotify.SpotifyError:
            self.sp = None

    def of(self, song: sqlite3.Row) -> bytes | None:
        """The song's cover: its Spotify album's, or its SoundCloud artwork; None if it has none."""
        service, _, sid = song["key"].partition(":")
        if service == "spotify" and self.sp:
            time.sleep(0.25)  # gently: thousands of songs, one request each
            album = self.sp.track(sid).get("album") or {}
            images = album.get("images") or []
            return self._fetch(album.get("id") or sid, images[0]["url"] if images else None)
        if service == "soundcloud" and self.token:
            return self._fetch(song["key"], sc_api.artwork(self.token, sid))
        return None

    def _fetch(self, key: str, url: str | None) -> bytes | None:
        if key not in self.cache:
            self.cache[key] = download(url) if url else None
        return self.cache[key]


def run(run: "Run") -> str:
    """Give every library file its song's cover (see the module); the old one is backed up first."""
    backups = run.data / "cover-backups"
    backups.mkdir(parents=True, exist_ok=True)
    journal = backups / "done.txt"
    done = set(journal.read_text(encoding="utf-8").splitlines()) if journal.exists() else set()
    con = run.connect()
    try:
        covers = Covers(con, run)
        todo = [r[0] for r in con.execute("SELECT path FROM files ORDER BY path") if r[0] not in done]
        counts = {"replaced": 0, "already the song's": 0, "without a song or cover": 0, "failed": 0}
        with journal.open("a", encoding="utf-8") as log_done:
            for n, rel in enumerate(todo, 1):
                if run.stop.is_set() or run.give_way.is_set():
                    run.left = 0 if run.stop.is_set() else len(todo) - n + 1
                    break
                run.say(f"{n} of {len(todo)}: {rel}", n - 1, len(todo))
                outcome = _one(run, con, covers, rel, backups)
                if outcome in ("replaced", "failed"):
                    run.note(f"{rel}: {outcome}")
                counts[outcome] = counts.get(outcome, 0) + 1
                if outcome not in ("failed", "changed lately"):
                    log_done.write(rel + "\n")
                    log_done.flush()
    finally:
        con.close()
    left = f"; {run.left} left" if run.left else ""
    return ", ".join(f"{v} {k}" for k, v in counts.items() if v) + left if any(counts.values()) else "nothing to do"


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
