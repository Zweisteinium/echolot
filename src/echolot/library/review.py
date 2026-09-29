"""Review: songs filed on a probable match (rules.identify) and rejected downloads kept in inbox/review/
for 30 days. A decision is applied once it is UNDO_SECONDS old (until then Revert takes it back):
  ok       the probable match is right
  wrong    it is not: the file is retired, that download is never taken for the song again, and the song
           is searched again
  accept   a rejected download is the right song after all: it is filed (and tagged as the song)
  discard  delete a rejected download now (else after 30 days)
"""

import datetime
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from echolot.library import audio, catalog, filing, rules
from echolot.library.filing import MUSIC, Want
from echolot.settings.sources import ConfigError

if TYPE_CHECKING:
    from echolot.jobs.worker import Run

KEPT = MUSIC + "inbox/review/"
DECISIONS = {"filed": ("ok", "wrong"), "kept": ("accept", "discard")}
UNDO_SECONDS = 120


def decision_id(ts: str, path: str) -> str:
    return f"{ts} {path}"


@dataclass
class Item:
    event: sqlite3.Row
    kind: str  # filed (a probable match in the library) or kept (a rejected download)
    file: Path  # where Echolot sees the audio file
    length: float  # of the wanted song, 0 = unknown
    decision: str | None  # made but not applied yet

    @property
    def id(self) -> str:
        return decision_id(self.event["ts"], self.event["path"])

    @property
    def length_diff(self) -> float | None:
        seconds = self.event["seconds"]
        return seconds - self.length if seconds and self.length else None


def local_file(path: str, music_dir: Path) -> Path | None:
    """Echolot's path of an event file: library-relative, or /music/inbox/review/<...>."""
    if path.startswith(KEPT):
        p, base = music_dir / path.removeprefix(MUSIC), music_dir / "inbox" / "review"
    elif not path.startswith("/"):
        p, base = music_dir / "tracks" / path, music_dir / "tracks"
    else:
        return None
    p = p.resolve()
    return p if p.is_relative_to(base.resolve()) else None


def near_miss(e: sqlite3.Row, wanted: float) -> bool:
    """A rejected download worth a look: the artist matched and the length is not far off."""
    if (e["reason"] or "").startswith("artist "):
        return False
    return not (e["seconds"] and wanted) or 2 / 3 <= e["seconds"] / wanted <= 1.5


def upgrade(e: sqlite3.Row) -> bool:
    """A kept download that would replace the song's lossy library copy with genuine lossless."""
    lossless = (e["ext"] or "") in audio.LOSSLESS and not e["fake"]
    return lossless and e["song_quality"] not in ("lossless", None)


def items(con: sqlite3.Connection, music_dir: Path) -> dict[str, list[Item]]:
    """What to look at: probable matches still in the library without an applied decision, and kept
    rejected downloads of songs that are still missing, or genuine lossless ones of songs the library
    has only lossy (accepting one replaces the lossy copy). Newest first."""
    decisions = {r["id"]: r for r in con.execute("SELECT * FROM review_decisions")}
    rows = con.execute(
        "SELECT e.*, s.length AS wanted_length, s.file AS song_file, f.quality AS song_quality, "
        "f.kbps AS song_kbps FROM events e LEFT JOIN songs s ON s.key = e.song "
        "LEFT JOIN files f ON f.path = s.file WHERE (e.action IN ('new', 'upgrade') AND e.matched = 'probable') "
        "OR (e.action IN ('wrong-song', 'mismatch') AND e.path LIKE ?) ORDER BY e.id DESC",
        (KEPT + "%",),
    ).fetchall()
    out: dict[str, list[Item]] = {"filed": [], "kept": []}
    for e in rows:
        kind = "filed" if e["action"] in ("new", "upgrade") else "kept"
        d = decisions.get(decision_id(e["ts"], e["path"]))
        if (d and d["applied"]) or (kind == "kept" and e["song_file"] and not upgrade(e)):
            continue  # decided, or the song was found meanwhile
        length = e["wanted_length"] or e["wanted_seconds"] or 0
        if kind == "kept" and not near_miss(e, length):
            continue
        file = local_file(e["path"], music_dir)
        if file is None or not file.is_file():
            continue
        out[kind].append(Item(e, kind, file, float(length), d["decision"] if d else None))
    return out


def find(con: sqlite3.Connection, music_dir: Path, event_id: int) -> Item | None:
    return next((i for group in items(con, music_dir).values() for i in group if i.event["id"] == event_id), None)


def decide(con: sqlite3.Connection, music_dir: Path, event_id: int, decision: str) -> Item:
    item = find(con, music_dir, event_id)
    if item is None:
        raise ConfigError("This download is no longer up for review.")
    if decision not in DECISIONS[item.kind]:
        raise ConfigError(f"'{decision}' is not a decision for this download.")
    with con:
        con.execute(
            "INSERT OR REPLACE INTO review_decisions (id, event_id, decision, decided) VALUES (?, ?, ?, ?)",
            (item.id, event_id, decision, datetime.datetime.now().isoformat(timespec="seconds")),
        )
    return item


def revert(con: sqlite3.Connection, music_dir: Path, event_id: int) -> Item:
    """Take back a decision that is not applied yet."""
    item = find(con, music_dir, event_id)
    if item is None or not item.decision:
        raise ConfigError("There is no pending decision for this download (or it is applied already).")
    with con:
        con.execute("DELETE FROM review_decisions WHERE id = ? AND applied IS NULL", (item.id,))
    return item


# ---------------------------------------------------------------- applying


def apply_due(run: "Run", con: sqlite3.Connection) -> list[str]:
    """Apply the decisions older than UNDO_SECONDS; returns what happened."""
    cutoff = (datetime.datetime.now() - datetime.timedelta(seconds=UNDO_SECONDS)).isoformat(timespec="seconds")
    done = []
    for d in con.execute(
        "SELECT r.id AS decision_id, r.decision, e.* FROM review_decisions r JOIN events e ON e.id = r.event_id "
        "WHERE r.applied IS NULL AND r.decided <= ?",
        (cutoff,),
    ).fetchall():
        try:
            result = _apply(run, con, d)
        except Exception as e:  # keep the others going; the result says what went wrong
            result = f"failed: {e}"
        with con:
            con.execute(
                "UPDATE review_decisions SET applied = ?, result = ? WHERE id = ?",
                (datetime.datetime.now().isoformat(timespec="seconds"), result, d["decision_id"]),
            )
        done.append(f"{d['decision']} {d['artist']} - {d['title']}: {result}")
    return done


def _search_again(con: sqlite3.Connection, key: str, tries: int) -> None:
    """The song is due for a search right away, searched loosened (as after 2 failed searches)."""
    with con:
        con.execute(
            "INSERT INTO attempts (song_key, tries, last_try) VALUES (?, ?, 0) ON CONFLICT (song_key) "
            "DO UPDATE SET tries = max(tries, excluded.tries), last_try = 0",
            (key, max(tries, 2)),
        )


def _block(con: sqlite3.Connection, key: str, d: sqlite3.Row) -> None:
    """Never take this download for the song again (its found and file names)."""
    names = [n for n in (d["found"], d["file_name"]) if n]
    if key and names:
        with con:
            con.executemany("INSERT OR IGNORE INTO blocked (song_key, name) VALUES (?, ?)", [(key, n) for n in names])


def _apply(run: "Run", con: sqlite3.Connection, d: sqlite3.Row) -> str:
    paths, key = run.paths, rules.norm_key(d["song"])
    decision = d["decision"]
    if decision == "ok":
        return "kept"
    if decision == "wrong":
        _block(con, key, d)
        with filing.LOCK:
            cat = catalog.Catalog.from_db(con)
            entry = next((e for e in cat.entries if e.path == d["path"]), None)
            result = "already gone"
            if entry and (paths.tracks / entry.path).is_file():
                filing.retire(con, paths, entry, "marked wrong in review")
                result = "retired"
        if key:
            _search_again(con, key, d["tries"] or 0)
        return result
    p = local_file(d["path"], paths.music)
    if p is None or not p.is_file() or not d["path"].startswith(KEPT):
        return "file gone"
    if decision == "discard":
        p.unlink()
        _block(con, key, d)  # the search fallback would keep it again
        return "deleted"
    # accept: the download is tagged with another artist who has this song in the library already: the
    # same recording under two artist names (Spotify lists it twice). Link the song to that file.
    want = Want(d["artist"] or "", d["title"] or "", 0, key)
    song = con.execute("SELECT * FROM songs WHERE key = ?", (key,)).fetchone() if key else None
    if song:
        want = Want.of(song)
    tag_artists, _ = audio.read_tags(p)
    own = rules.artist_words(want.artist)
    dur, _ = audio.probe(p)
    cat = catalog.Catalog.from_db(con)
    for other in tag_artists:
        if any(f" {w} " in rules.words(other) for w in own):
            continue
        if hits := cat.find(other, want.title, dur):
            if key:
                with con:
                    con.execute("UPDATE songs SET link = ? WHERE key = ?", (json.dumps([other, want.title]), key))
            p.unlink()
            filing.event(con, paths, "linked", paths.tracks / hits[0].path, song=key or None, artist=want.artist,
                         title=want.title, source=d["source"], matched="review",
                         reason=f"same recording as {hits[0].path} ({other})")  # fmt: skip
            return f"linked {hits[0].path}"
    action, dest = filing.file_into(con, paths, p, want, d["source"] or "", match="review", fake=bool(d["fake"]))
    if dest and action in ("new", "upgrade"):
        audio.write_tags(dest, artist=want.artist, title=want.title)  # accepted as this song
        from echolot.jobs.acquire import finish

        finish(run, con, dest, want)
    if key and action in ("new", "upgrade", "duplicate"):
        with con:  # the link makes the song this file whatever its length (a near miss of another edit)
            con.execute("DELETE FROM attempts WHERE song_key = ?", (key,))
            con.execute("UPDATE songs SET link = ? WHERE key = ?", (json.dumps([want.artist, want.title]), key))
    return f"{action} {dest.relative_to(paths.tracks) if dest else ''}".strip()
