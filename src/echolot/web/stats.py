"""Queries behind the dashboard pages."""

import json
from datetime import datetime, timedelta
from sqlite3 import Connection, Row
from typing import Any

from echolot.jobs import acquire
from echolot.jobs.lists import GREYED_OUT, NOT_ON_SOUNDCLOUD
from echolot.library import filing
from echolot.library.catalog import QUALITY
from echolot.library.filing import Paths

TIERS = [*QUALITY, ("missing", "Missing")]  # the quality scale of songs, best first
ADDED = ("new", "upgrade")
REJECTED = ("wrong-song", "mismatch")


def overview(con: Connection) -> dict[str, Any]:
    files, size = con.execute("SELECT count(*), coalesce(sum(size), 0) FROM files").fetchone()
    by_tier = dict(con.execute("SELECT quality, count(*) FROM files GROUP BY quality").fetchall())
    counts = "count(*), count(file), count(DISTINCT file), coalesce(sum(file IS NOT NULL AND close_match), 0)"
    wanted, have, in_lists, close = con.execute(f"SELECT {counts} FROM wanted").fetchone()
    since = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    day = {
        action: (n, b or 0)
        for action, n, b in con.execute(
            "SELECT action, count(*), sum(bytes) FROM events WHERE ts >= ? GROUP BY action", (since,)
        )
    }
    not_found = con.execute(
        "SELECT count(*) FROM wanted s JOIN attempts a ON a.song_key = s.key WHERE s.file IS NULL AND a.tries >= 1"
    ).fetchone()[0]
    songs = con.execute(
        f"SELECT count(*) AS songs, count(s.file) AS have, {_TIER_SUMS} FROM wanted s "
        "LEFT JOIN files f ON f.path = s.file"
    ).fetchone()
    services = {
        r[0]: {"songs": r[1], "missing": r[1] - r[2]}
        for r in con.execute(
            "SELECT l.service, count(DISTINCT s.key), count(DISTINCT CASE WHEN s.file IS NOT NULL "
            "THEN s.key END) FROM lists l JOIN list_songs ls ON ls.list_key = l.key "
            "JOIN songs s ON s.key = ls.song_key GROUP BY l.service"
        )
    }
    return {
        "services": services,  # distinct songs (and missing ones) per service
        "song_tiers": tier_counts(songs),
        "files": files,
        "in_lists": in_lists,  # files the wanted songs have (a file can be several songs of the lists)
        "size": size,
        "tiers": [(key, label, by_tier.get(key, 0)) for key, label in QUALITY],
        "lossless": by_tier.get("lossless", 0),
        "wanted": wanted,
        "have": have,
        "close": close,  # of 'have': covered by a close match (another version, taken in review)
        "not_found": not_found,
        "added_24h": sum(day.get(a, (0, 0))[0] for a in ADDED),
        "added_bytes_24h": sum(day.get(a, (0, 0))[1] for a in ADDED),
        "rejected_24h": sum(day.get(a, (0, 0))[0] for a in REJECTED),
        "lists": lists(con),
    }


_TIER_SUMS = ", ".join(f"coalesce(sum(f.quality = '{k}'), 0) AS \"{k}\"" for k, _ in QUALITY)


def tier_counts(row: Row | dict) -> list[tuple[str, str, int]]:
    """(tier, label, songs) for a row with a column per quality tier, 'songs' and 'have'."""
    counts = [(k, label, row[k] or 0) for k, label in QUALITY]
    return [*counts, ("missing", "Missing", (row["songs"] or 0) - (row["have"] or 0))]


def tiers_of(songs: list[Row]) -> list[tuple[str, str, int]]:
    """tier_counts of a list of songs (list_songs rows)."""
    n: dict[str, int] = {}
    for r in songs:
        key = (r["quality"] or "lossy-low") if r["file"] else "missing"
        n[key] = n.get(key, 0) + 1
    return [(k, label, n.get(k, 0)) for k, label in TIERS]


def lists(con: Connection) -> list[Row]:
    """The followed lists with song counts per quality tier (tier_counts)."""
    return con.execute(
        "SELECT l.key, l.service, l.title, l.url, l.playlist, l.fetched, "
        f"count(ls.song_key) AS songs, count(s.file) AS have, {_TIER_SUMS} "
        "FROM lists l LEFT JOIN list_songs ls ON ls.list_key = l.key "
        "LEFT JOIN songs s ON s.key = ls.song_key LEFT JOIN files f ON f.path = s.file "
        "GROUP BY l.key ORDER BY l.position"
    ).fetchall()


def get_list(con: Connection, key: str) -> Row | None:
    return con.execute("SELECT * FROM lists WHERE key = ?", (key,)).fetchone()


def list_songs(con: Connection, key: str) -> list[Row]:
    return con.execute(
        "SELECT ls.position, s.key, s.service, s.artist, s.title, s.length, s.unavailable, s.url, "
        "s.file, f.quality, f.kbps FROM list_songs ls JOIN songs s ON s.key = ls.song_key "
        "LEFT JOIN files f ON f.path = s.file WHERE ls.list_key = ? ORDER BY ls.position",
        (key,),
    ).fetchall()


UNAVAILABLE = {
    GREYED_OUT: "Spotify no longer plays it (withdrawn or not in your country); it is searched first.",
    NOT_ON_SOUNDCLOUD: "SoundCloud hands this upload out to nobody (a label release); YouTube and SoundCloud "
    "search for other uploads.",
}


NOTES = {  # (text, style, explanation)
    "drm": ("DRM on SoundCloud", "bad", "It fits, but SoundCloud serves it encrypted only (a label release)."),
    "stalled": ("stalls at peers", "warn", "Soulseek users have it, but none started the transfer. It is tried again."),
}


def missing(con: Connection, list_key: str | None = None, paths: Paths | None = None) -> list[dict[str, Any]]:
    """Songs of the followed lists that are not in the library: their lists, what the searches saw (Soulseek,
    then YouTube and SoundCloud), the downloads rejected for them, and notes worth a glance."""
    where, args = "s.file IS NULL", []
    if list_key:
        where += " AND EXISTS (SELECT 1 FROM list_songs WHERE song_key = s.key AND list_key = ?)"
        args.append(list_key)
    rows = con.execute(
        "SELECT s.key, s.service, s.artist, s.title, s.length, s.unavailable, s.url, "
        "a.tries, a.last_try, a.last_fallback, a.result, a.fallback_result, "
        "(SELECT group_concat(place, ' · ') FROM (SELECT l.title || ' #' || (ls.position + 1) AS place "
        " FROM list_songs ls JOIN lists l ON l.key = ls.list_key WHERE ls.song_key = s.key "
        " ORDER BY l.position, ls.position)) AS in_lists "
        f"FROM wanted s LEFT JOIN attempts a ON a.song_key = s.key WHERE {where} "
        "ORDER BY s.artist COLLATE NOCASE, s.title COLLATE NOCASE",
        args,
    ).fetchall()
    rejected: dict[str, list[Row]] = {}
    if keys := [r["key"] for r in rows]:
        for e in con.execute(
            "SELECT song, ts, action, source, found, file_name, reason, seconds, wanted_seconds FROM events "
            f"WHERE action IN ('wrong-song', 'mismatch') AND song IN ({', '.join('?' * len(keys))}) ORDER BY id DESC",
            keys,
        ):
            if len(rejected.setdefault(e["song"], [])) < 3:
                rejected[e["song"]].append(e)
    return [_tried(r, rejected.get(r["key"], []), paths) for r in rows]


def close_matches(con: Connection) -> list[Row]:
    """Wanted songs covered by a close match: another version, taken for the song in review."""
    return con.execute(
        "SELECT key, service, artist, title, length, url, file, f.duration FROM wanted s "
        "JOIN files f ON f.path = s.file WHERE s.close_match ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE"
    ).fetchall()


def _tried(r: Row, rejected: list[Row], paths: Paths | None) -> dict[str, Any]:
    tries = r["tries"] or 0
    result = json.loads(r["result"]) if r["result"] else None
    fallback = json.loads(r["fallback_result"]) if r["fallback_result"] else None
    notes = []  # (text, style, explanation)
    if r["unavailable"]:
        notes.append((r["unavailable"], "bad", UNAVAILABLE.get(r["unavailable"], "")))
    fetched = [t for site in ("youtube", "soundcloud") for t in ((fallback or {}).get(site) or {}).get("tried", [])]
    if any(t[2] == "DRM-protected" for t in fetched):
        notes.append(NOTES["drm"])
    stalled = [t for t in (result or {}).get("tried", []) if t[1] == "failed" and "no progress" in (t[2] or "")]
    if stalled and len(stalled) == len(result["tried"]):
        notes.append(NOTES["stalled"])
    return {
        **dict(r),
        "result": result,
        "fallback": fallback,
        "stage": acquire.stage(tries),
        "next_try": acquire.retry_at(tries, r["last_try"], *acquire.MISSING_RETRY)
        if tries and r["service"] == "spotify"
        else None,
        "rejected": rejected,
        "in_review": paths is not None and filing.in_review(paths, r["artist"], r["title"]),
        "notes": notes,
    }


EVENT_FILTERS = {"added": ADDED, "rejected": REJECTED}


def events(con: Connection, kind: str = "", limit: int = 300) -> list[Row]:
    actions = EVENT_FILTERS.get(kind)
    where = f"WHERE action IN ({', '.join('?' * len(actions))})" if actions else ""
    return con.execute(f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ?", (*(actions or ()), limit)).fetchall()
