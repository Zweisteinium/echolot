"""The library's lossy check: every library FLAC through both lossy checks (audio.judge, lossprint at the lowest CPU
priority), stored in lossy_checks; once per audio: a file changed by a retag (the same STREAMINFO MD5) is not checked
again. With ECHOLOT_LOSSPRINT on the stored verdicts are applied (apply): lossy marks a file made from a lossy one
(lossy_sourced), ok and doubtful unmark it, a lossy master (lossy_sourced.master) stays as it is; shadow: stored
only, the result says what on would change; off: nothing.
"""

import datetime
import json
import sqlite3
from collections import Counter
from typing import TYPE_CHECKING

from echolot.library import audio, catalog, filing

if TYPE_CHECKING:
    from echolot.jobs.worker import Run


def run(run: "Run") -> str:
    mode = audio.lossprint_mode()
    if mode == "off":
        return "off (ECHOLOT_LOSSPRINT)"
    con = run.connect()
    try:
        checks = {r["path"]: r for r in con.execute("SELECT * FROM lossy_checks")}
        flacs = "SELECT path, size, mtime FROM files WHERE lower(path) LIKE '%.flac' ORDER BY path"
        stale = [r for r in con.execute(flacs) if _stale(r, checks.get(r["path"]))]
        todo = run.todo(stale, lambda r: r["path"])
        counts: Counter[str] = Counter()
        for n, row in enumerate(todo, 1):
            if run.stop.is_set() or run.give_way.is_set():
                run.left = 0 if run.stop.is_set() else len(todo) - n + 1
                break
            run.say(f"{run.of(n, len(todo))}: {row['path']}", n - 1, len(todo))
            outcome = _one(run, con, row, checks.get(row["path"]))
            counts[outcome] += 1
            if outcome == "lossy":
                run.note(f"{row['path']}: lossy")
            run.handled.add(row["path"])
        with con:
            con.execute("DELETE FROM lossy_checks WHERE path NOT IN (SELECT path FROM files)")
        marked, unmarked = apply(con, write=mode == "on")
    finally:
        con.close()
    order = ["lossy", "doubtful", "ok"]
    found = ", ".join(f"{counts[k]} {k}" for k in sorted(counts, key=lambda k: order.index(k) if k in order else 3))
    parts = [found] if found else []
    would = "" if mode == "on" else "on would "
    if marked or unmarked:
        parts.append(f"{would}mark {len(marked)} made from lossy, unmark {len(unmarked)}")
    if run.left:
        parts.append(f"{run.left} left")
    return "; ".join(parts) or "nothing to do"


def _stale(row: sqlite3.Row, check: sqlite3.Row | None) -> bool:
    """Not checked, or changed since (its size or mtime; a retag too: _one tells it by the audio MD5)."""
    return check is None or (check["size"], check["mtime"]) != (row["size"], row["mtime"])


def _one(run: "Run", con: sqlite3.Connection, row: sqlite3.Row, old: sqlite3.Row | None) -> str:
    """Check one library FLAC (its old check stands when only its tags changed); its verdict, or why none."""
    path = run.paths.tracks / row["path"]
    if not path.is_file():
        return "gone"
    md5 = audio.streaminfo_md5(path)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    if old is not None and md5 and old["md5"] == md5:
        moved = "UPDATE lossy_checks SET size = ?, mtime = ? WHERE path = ?"
        with con:
            con.execute(moved, (row["size"], row["mtime"], row["path"]))
        return "retagged"
    spec = audio.spectrum(path)
    lp = audio.lossprint(path, background=True)
    if lp is None:
        return "not checked (lossprint not installed)"
    verdict, _ = audio.judge(spec, lp)
    with con:
        con.execute(
            "INSERT OR REPLACE INTO lossy_checks (path, size, mtime, md5, spectrum, lossprint, verdict, checked) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row["path"], row["size"], row["mtime"], md5, json.dumps(spec), json.dumps(lp), verdict, now),
        )
    return verdict


def apply(con: sqlite3.Connection, write: bool) -> tuple[list[str], list[str]]:
    """The stored verdicts of the files as they are now (their size and mtime) against the marks: (files to mark
    made from lossy, files to unmark), marked and unmarked with `write` (the songs' tiers follow). A lossy master
    is never touched: a recording of Spotify's lossless stream proved it the best copy there is."""
    current = (
        "SELECT c.path, c.spectrum, c.lossprint, c.verdict, l.stem IS NOT NULL AS marked, coalesce(l.master, 0) "
        "AS master FROM lossy_checks c JOIN files f ON f.path = c.path AND f.size = c.size AND f.mtime = c.mtime "
        "LEFT JOIN lossy_sourced l ON l.stem = substr(c.path, 1, length(c.path) - 5)"
    )
    rows = con.execute(current).fetchall()
    mark = [r for r in rows if r["verdict"] == "lossy" and not r["marked"]]
    unmark = [r for r in rows if r["verdict"] != "lossy" and r["marked"] and not r["master"]]
    if write and (mark or unmark):
        today = datetime.date.today().isoformat()
        with filing.LOCK, con:
            for r in mark:
                _, source = audio.judge(json.loads(r["spectrum"] or "{}"), json.loads(r["lossprint"] or "null"))
                add = "INSERT OR IGNORE INTO lossy_sourced (stem, source, detected) VALUES (?, ?, ?)"
                con.execute(add, (r["path"][:-5], source, today))
            stems = [(r["path"][:-5],) for r in unmark]
            con.executemany("DELETE FROM lossy_sourced WHERE stem = ? AND NOT master", stems)
        catalog.match_songs(con)
    return [r["path"] for r in mark], [r["path"] for r in unmark]
