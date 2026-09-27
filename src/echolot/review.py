"""Review: songs the pipeline filed on a probable match (it loosens the search for songs it did not find,
see LOOSEN in music-sync.py) and rejected downloads it keeps in inbox/review/ for 30 days.

Decisions are appended to review.yml in the pipeline directory; music-sync applies each one once (at the
start of its Soulseek jobs, or within 10 min by the playlists job) and records the result in
state/review-done.json:
  ok       the probable match is right
  wrong    it is not: the file is retired, that download is never taken for the song again, and the song
           is searched again
  accept   a rejected download is the right song after all: it is filed
  discard  delete a rejected download now (else after 30 days)
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from echolot.sources import ConfigError, write_atomic

FILE = "review.yml"
DONE = "state/review-done.json"
PIPELINE_MUSIC = "/music/"  # the music directory as the pipeline sees it
KEPT = PIPELINE_MUSIC + "inbox/review/"
DECISIONS = {"filed": ("ok", "wrong"), "kept": ("accept", "discard")}
HEADER = """\
# Decisions from Echolot's review page. music-sync applies each one once (state/review-done.json).
# ok / wrong: a song filed on a probable match; accept / discard: a rejected download kept in inbox/review.
"""


def decision_id(ts: str, path: str) -> str:
    return f"{ts} {path}"


def _decisions(root: Path) -> list[dict]:
    try:
        data = yaml.safe_load((root / FILE).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    items = data.get("decisions") if isinstance(data, dict) else None
    return [d for d in items or [] if isinstance(d, dict) and d.get("id")]


def _done(root: Path) -> dict[str, dict]:
    try:
        return json.loads((root / DONE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


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
    """Echolot's path of an event file: library-relative, or under the pipeline's /music/inbox/review/."""
    if path.startswith(KEPT):
        p = music_dir / path.removeprefix(PIPELINE_MUSIC)
        base = music_dir / "inbox" / "review"
    elif not path.startswith("/"):
        p, base = music_dir / "tracks" / path, music_dir / "tracks"
    else:
        return None
    p = p.resolve()
    return p if p.is_relative_to(base.resolve()) else None


def items(con: sqlite3.Connection, root: Path, music_dir: Path) -> dict[str, list[Item]]:
    """What to look at: probable matches still in the library without a decision, and kept rejected
    downloads of songs that are still missing (newest first)."""
    decided = {d["id"]: d.get("decision") for d in _decisions(root)}
    done = _done(root)
    rows = con.execute(
        "SELECT e.*, s.length AS wanted_length, s.file AS song_file FROM events e "
        "LEFT JOIN songs s ON s.key = e.song WHERE (e.action IN ('new', 'upgrade') AND e.matched = 'probable') "
        "OR (e.action IN ('wrong-song', 'mismatch') AND e.path LIKE ?) ORDER BY e.id DESC",
        (KEPT + "%",),
    ).fetchall()
    out: dict[str, list[Item]] = {"filed": [], "kept": []}
    for e in rows:
        kind = "filed" if e["action"] in ("new", "upgrade") else "kept"
        key = decision_id(e["ts"], e["path"])
        if key in done or (kind == "kept" and e["song"] and e["song_file"]):  # song found meanwhile
            continue
        file = local_file(e["path"], music_dir)
        if file is None or not file.is_file():
            continue
        length = e["wanted_length"] or e["wanted_seconds"] or 0
        out[kind].append(Item(e, kind, file, float(length), decided.get(key)))
    return out


def find(con: sqlite3.Connection, root: Path, music_dir: Path, event_id: int) -> Item | None:
    return next(
        (
            i
            for group in items(con, root, music_dir).values()
            for i in group
            if i.event["id"] == event_id
        ),
        None,
    )


def decide(
    con: sqlite3.Connection, root: Path, music_dir: Path, event_id: int, decision: str
) -> Item:
    """Append a decision to review.yml (atomically; the pipeline only reads it)."""
    item = find(con, root, music_dir, event_id)
    if item is None:
        raise ConfigError("This download is no longer up for review.")
    if decision not in DECISIONS[item.kind]:
        raise ConfigError(f"'{decision}' is not a decision for this download.")
    e = item.event
    entry = {
        "id": item.id,
        "decision": decision,
        "song": e["song"] or "",
        "path": e["path"],
        "artist": e["artist"] or "",
        "title": e["title"] or "",
        "length": round(item.length),
        "source": e["source"] or "",
        "found": e["found"] or "",
        "file_name": e["file_name"] or "",
        "fake": bool(e["fake"]),
        "tries": e["tries"] or 0,
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    decisions = [d for d in _decisions(root) if d["id"] != item.id] + [entry]
    text = HEADER + yaml.safe_dump(
        {"decisions": decisions}, allow_unicode=True, sort_keys=False, width=1000
    )
    write_atomic(root / FILE, text)
    return item
