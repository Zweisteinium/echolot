"""Lyrics for the library (library/lyrics): each song's file gets the song's lyrics from LRCLIB when they
surely are its own, embedded where Navidrome and other players read them (synced ones as LRC lines). A file
with lyrics of its own keeps them. One LRCLIB has none for is asked again after AGAIN (LRCLIB grows); a file
changed in the last QUIET seconds waits (it may be being filed or tagged); one that changes later is looked
at again. The newest files first, at most PER_RUN a run; LRCLIB failing FAILURES times in a row ends it. A
file LRCLIB failed on is asked again the next day, so it can't hold up the others run after run."""

import datetime
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from echolot.library import audio, catalog, filing, lyrics, review
from echolot.services import lrclib

if TYPE_CHECKING:
    from echolot.jobs.worker import Run

KINDS = ("flac", "mp3", "m4a", "opus", "ogg")  # the files lyrics are embedded in (audio.write_lyrics)
PER_RUN = 400
AGAIN = datetime.timedelta(days=30)
QUIET = 600  # s
FAILURES = 3
WRITTEN = ("synced", "plain")  # states of lyrics Echolot wrote
ANSWERS = (*WRITTEN, "instrumental", "none")  # what LRCLIB answered


def run(run: "Run") -> str:
    con = run.connect()
    try:
        with con:
            con.execute("DELETE FROM lyrics WHERE path NOT IN (SELECT path FROM files)")
        confirm = {i.event["path"] for i in review.items(con, run.paths.music)["filed"]}  # may be another song
        todo = run.todo([r for r in due(con) if r["path"] not in confirm], lambda r: r["path"])[:PER_RUN]
        counts: Counter[str] = Counter()
        failed = 0
        for n, row in enumerate(todo, 1):
            if run.stop.is_set() or run.give_way.is_set():
                run.left = 0 if run.stop.is_set() else len(todo) - n + 1
                break
            run.say(f"{run.of(n, len(todo))}: {row['path']}", n - 1, len(todo))
            try:
                outcome = _one(run, con, row)
            except OSError as e:  # (audio.write_lyrics: a full or read-only disk)
                run.note(f"The library can't be written ({e.strerror}): the rest waits for the next run")
                break
            counts[outcome] += 1
            if outcome == "failed":
                failed += 1
            elif outcome in ANSWERS:
                failed = 0
            if outcome in WRITTEN:
                run.note(f"{row['path']}: {outcome} lyrics")
            if failed >= FAILURES:
                run.note("LRCLIB is failing: the rest waits for the next run")
                break
            if outcome != "failed":
                run.handled.add(row["path"])
    finally:
        con.close()
    return _message(counts, run.left)


def due(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """The files to look at, the newest first: never looked at, changed since, or without lyrics on LRCLIB (or
    unreadable) a while ago. One song per file (a Spotify one first: its names and album are the release's),
    never one that took the file as a close match (another version: its names would find the wrong lyrics)."""
    again = (datetime.date.today() - AGAIN).isoformat()
    kinds = " OR ".join(f"lower(f.path) LIKE '%.{k}'" for k in KINDS)
    rows = con.execute(
        "SELECT f.path, f.size, f.mtime, f.duration, s.key, s.service, s.artist, s.artists, s.title, s.album, "
        "s.artist_alias, r.album AS own_album, l.state, l.size AS seen_size, l.mtime AS seen_mtime "
        "FROM files f JOIN songs s ON s.file = f.path LEFT JOIN song_releases r ON r.song_key = s.key "
        "LEFT JOIN lyrics l ON l.path = f.path "
        f"WHERE ({kinds}) AND s.service != ? AND f.duration > 0 AND NOT s.close_match AND (l.path IS NULL "
        "OR l.size != f.size OR l.mtime != f.mtime OR (l.state IN ('none', 'unreadable') AND l.checked < ?) "
        "OR (l.state = 'failed' AND l.checked < ?)) ORDER BY f.mtime DESC, s.service != 'spotify', s.key",
        (catalog.LOCAL, again, datetime.date.today().isoformat()),
    ).fetchall()
    seen: set[str] = set()
    return [r for r in rows if not (r["path"] in seen or seen.add(r["path"]))]


def _one(run: "Run", con: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Look at one file; what came of it (synced, plain, instrumental, none, own, kept, failed, or why it
    waits)."""
    path = run.paths.tracks / row["path"]
    now = _size_mtime(path)
    if now is None:
        return "gone"
    if time.time() - now[1] < QUIET or now != (row["size"], row["mtime"]):
        return "waits"  # being filed or tagged, or changed since the library's last scan (Echolot's lyrics too)
    have = audio.read_lyrics(path)
    if have is None:
        _note(con, row, "unreadable", "", now)  # (not asked again each run; when it changes, or in a while)
        return "unreadable"
    if have:  # its own, or the ones Echolot wrote (a retag changed the file since)
        state = row["state"] if row["state"] in WRITTEN else "own"
        _note(con, row, state, None, now)
        return "kept" if state in WRITTEN else "own"
    try:
        found = lyrics.find(row, row["duration"], row["own_album"])
    except lrclib.LrclibError:
        _note(con, row, "failed", "", now)  # asked again tomorrow (due)
        return "failed"
    if found is None or found.kind == "instrumental":
        _note(con, row, found.kind if found else "none", found.source if found else "", now)
        return found.kind if found else "none"
    with filing.LOCK:  # (filing may have put another file there since: its length was not the one asked for)
        if _size_mtime(path) != now:
            return "waits"
        written = audio.write_lyrics(path, found.text)
        after = _size_mtime(path)
    if not written or after is None:
        _note(con, row, "unreadable", "", now)
        return "unreadable"
    _note(con, row, found.kind, found.source, after)
    return found.kind


def _size_mtime(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, int(st.st_mtime)


def _note(con: sqlite3.Connection, row: sqlite3.Row, state: str, source: str | None, seen: tuple[int, int]) -> None:
    """Store what came of a file, with the size and mtime it had (`source` None: the one stored stays)."""
    today = datetime.date.today().isoformat()
    with con:
        con.execute(
            "INSERT INTO lyrics (path, song_key, state, source, size, mtime, checked) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (path) DO UPDATE SET song_key = excluded.song_key, state = excluded.state, "
            "source = coalesce(?, lyrics.source), size = excluded.size, mtime = excluded.mtime, "
            "checked = excluded.checked",
            (row["path"], row["key"], state, source or "", *seen, today, source),
        )


def _message(counts: Counter[str], left: int) -> str:
    named = {"synced": "synced", "plain": "plain", "instrumental": "instrumental", "none": "none on LRCLIB"}
    named |= {"own": "had their own", "kept": "kept", "failed": "LRCLIB failed", "unreadable": "unreadable"}
    parts = [f"{counts[k]} {label}" for k, label in named.items() if counts[k]]
    if left:
        parts.append(f"{left} left")
    return ", ".join(parts) or "nothing to do"
