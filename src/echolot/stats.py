"""Queries behind the dashboard pages."""

from datetime import datetime, timedelta
from sqlite3 import Connection, Row
from typing import Any

from echolot.library import QUALITY

ADDED = ("new", "upgrade")
REJECTED = ("wrong-song", "mismatch")


def overview(con: Connection) -> dict[str, Any]:
    files, size = con.execute("SELECT count(*), coalesce(sum(size), 0) FROM files").fetchone()
    by_tier = dict(con.execute("SELECT quality, count(*) FROM files GROUP BY quality").fetchall())
    wanted, have = con.execute("SELECT count(*), count(file) FROM songs").fetchone()
    since = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    day = {
        action: (n, b or 0)
        for action, n, b in con.execute(
            "SELECT action, count(*), sum(bytes) FROM events WHERE ts >= ? GROUP BY action",
            (since,),
        )
    }
    not_found = con.execute(
        "SELECT count(*) FROM songs s JOIN attempts a ON a.song_key = s.key "
        "WHERE s.file IS NULL AND a.tries >= 1"
    ).fetchone()[0]
    return {
        "files": files,
        "size": size,
        "tiers": [(key, label, by_tier.get(key, 0)) for key, label in QUALITY],
        "lossless": by_tier.get("lossless", 0),
        "wanted": wanted,
        "have": have,
        "not_found": not_found,
        "added_24h": sum(day.get(a, (0, 0))[0] for a in ADDED),
        "added_bytes_24h": sum(day.get(a, (0, 0))[1] for a in ADDED),
        "rejected_24h": sum(day.get(a, (0, 0))[0] for a in REJECTED),
        "lists": lists(con),
        "jobs": {r["name"]: r for r in con.execute("SELECT * FROM jobs")},
    }


def lists(con: Connection) -> list[Row]:
    return con.execute(
        "SELECT l.key, l.service, l.title, l.url, l.playlist, l.fetched, "
        "count(ls.song_key) AS songs, count(s.file) AS have "
        "FROM lists l LEFT JOIN list_songs ls ON ls.list_key = l.key "
        "LEFT JOIN songs s ON s.key = ls.song_key GROUP BY l.key ORDER BY l.position"
    ).fetchall()


def get_list(con: Connection, key: str) -> Row | None:
    return con.execute("SELECT * FROM lists WHERE key = ?", (key,)).fetchone()


def list_songs(con: Connection, key: str) -> list[Row]:
    return con.execute(
        "SELECT ls.position, s.key, s.service, s.artist, s.title, s.length, s.unavailable, "
        "s.file, f.quality, f.kbps FROM list_songs ls JOIN songs s ON s.key = ls.song_key "
        "LEFT JOIN files f ON f.path = s.file WHERE ls.list_key = ? ORDER BY ls.position",
        (key,),
    ).fetchall()


def missing(con: Connection, list_key: str | None = None) -> list[Row]:
    """Songs of the followed lists that are not in the library, with the lists they are in."""
    where, args = "s.file IS NULL", []
    if list_key:
        where += " AND EXISTS (SELECT 1 FROM list_songs WHERE song_key = s.key AND list_key = ?)"
        args.append(list_key)
    return con.execute(
        "SELECT s.key, s.service, s.artist, s.title, s.length, s.unavailable, "
        "a.tries, a.last_try, a.last_fallback, "
        "(SELECT group_concat(title, ' · ') FROM (SELECT DISTINCT l.title FROM list_songs ls "
        " JOIN lists l ON l.key = ls.list_key WHERE ls.song_key = s.key ORDER BY l.position)) "
        "AS in_lists "
        f"FROM songs s LEFT JOIN attempts a ON a.song_key = s.key WHERE {where} "
        "ORDER BY s.artist COLLATE NOCASE, s.title COLLATE NOCASE",
        args,
    ).fetchall()


EVENT_FILTERS = {"added": ADDED, "rejected": REJECTED}


def events(con: Connection, kind: str = "", limit: int = 300) -> list[Row]:
    actions = EVENT_FILTERS.get(kind)
    where = f"WHERE action IN ({', '.join('?' * len(actions))})" if actions else ""
    return con.execute(
        f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ?", (*(actions or ()), limit)
    ).fetchall()
