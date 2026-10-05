"""Queries behind the dashboard pages. Each takes a user (their songs: those on the lists they follow,
the files those have, the events of those songs) or None (everyone's: the whole library), see scope."""

import json
from datetime import datetime, timedelta
from sqlite3 import Connection, Row
from typing import Any

from echolot.jobs import acquire
from echolot.jobs.lists import GREYED_OUT, NOT_ON_SOUNDCLOUD
from echolot.library import catalog, filing
from echolot.library.catalog import QUALITY
from echolot.library.filing import Paths

TIERS = [*QUALITY, ("missing", "Missing")]  # the quality scale of songs, best first
# the songs of a user: on the lists they follow
MINE = (
    "SELECT ls.song_key FROM list_songs ls JOIN sources src ON src.key = ls.list_key "
    "WHERE src.user_id = ? AND src.enabled"
)


def scope(user: Any) -> int | None:
    """Whose songs a page shows: the user's own, or None for everyone's (an admin who chose so)."""
    return None if user.everyone else user.id


def _mine(uid: int | None, column: str = "s.key") -> tuple[str, tuple]:
    """(SQL condition, its arguments) for rows of the user's songs; everything for None."""
    return (f"{column} IN ({MINE})", (uid,)) if uid is not None else ("1", ())


ADDED = ("new", "upgrade")
REJECTED = ("wrong-song", "mismatch")


def overview(con: Connection, uid: int | None = None) -> dict[str, Any]:
    """The numbers of the overview, for a user's songs (their files, their events) or everyone's."""
    mine, args = _mine(uid)
    library = con.execute("SELECT count(*), coalesce(sum(size), 0) FROM files").fetchone()
    files_of = "1" if uid is None else f"path IN (SELECT s.file FROM songs s WHERE {mine})"
    files, size = con.execute(f"SELECT count(*), coalesce(sum(size), 0) FROM files WHERE {files_of}", args).fetchone()
    tiers_sql = f"SELECT quality, count(*) FROM files WHERE {files_of} GROUP BY quality"
    by_tier = dict(con.execute(tiers_sql, args).fetchall())
    counts = "count(*), count(file), count(DISTINCT file), coalesce(sum(file IS NOT NULL AND close_match), 0)"
    wanted, have, in_lists, close = con.execute(f"SELECT {counts} FROM wanted s WHERE {mine}", args).fetchone()
    since = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    mine_events, _ = _mine(uid, "song")
    sql = f"SELECT action, count(*), sum(bytes) FROM events WHERE ts >= ? AND {mine_events} GROUP BY action"
    day = {action: (n, b or 0) for action, n, b in con.execute(sql, (since, *args))}
    not_found = con.execute(
        "SELECT count(*) FROM wanted s JOIN attempts a ON a.song_key = s.key "
        f"WHERE s.file IS NULL AND a.tries >= 1 AND {mine}",
        args,
    ).fetchone()[0]
    songs = con.execute(
        f"SELECT count(*) AS songs, count(s.file) AS have, {_TIER_SUMS} FROM wanted s "
        f"LEFT JOIN files f ON f.path = s.file WHERE {mine}",
        args,
    ).fetchone()
    theirs = "1" if uid is None else "l.key IN (SELECT key FROM sources WHERE user_id = ? AND enabled)"
    services = {
        r[0]: {"songs": r[1], "missing": r[1] - r[2]}
        for r in con.execute(
            "SELECT l.service, count(DISTINCT s.key), count(DISTINCT CASE WHEN s.file IS NOT NULL "
            "THEN s.key END) FROM lists l JOIN list_songs ls ON ls.list_key = l.key "
            f"JOIN songs s ON s.key = ls.song_key WHERE {theirs} GROUP BY l.service",
            args,
        )
    }
    return {
        "scoped": uid is not None,
        "library_files": library[0],  # the whole library, whoever's songs are shown
        "library_size": library[1],
        "services": services,  # distinct songs (and missing ones) per service
        "song_tiers": tier_counts(songs),
        "files": files,
        "in_lists": in_lists,  # files the wanted songs have (a file can be several songs of the lists)
        "size": size,
        "tiers": [
            *[(key, label, by_tier.get(key, 0)) for key, label in QUALITY],
            ("missing", "Missing", wanted - have),
        ],
        "lossless": by_tier.get("lossless", 0),
        "wanted": wanted,
        "have": have,
        "close": close,  # of 'have': covered by a close match (another version, taken in review)
        "not_found": not_found,
        "added_24h": sum(day.get(a, (0, 0))[0] for a in ADDED),
        "added_bytes_24h": sum(day.get(a, (0, 0))[1] for a in ADDED),
        "rejected_24h": sum(day.get(a, (0, 0))[0] for a in REJECTED),
        "lists": lists(con, uid),
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


def lists(con: Connection, uid: int | None = None) -> list[Row]:
    """The followed lists (a user's, with their own playlist choice; or everyone's, with who follows
    each) with song counts per quality tier (tier_counts)."""
    owners = (
        "(SELECT group_concat(u.name, ', ') FROM sources src JOIN users u ON u.id = src.user_id "
        "WHERE src.key = l.key AND src.enabled) AS owners"
    )
    if uid is None:
        playlist, where, args = "l.playlist", "", ()
    else:
        mode = "(SELECT playlist FROM sources WHERE key = l.key AND user_id = ?)"
        playlist, where, args = f"{mode} AS playlist", f"WHERE l.key IN ({_FOLLOWS})", (uid, uid)
    return con.execute(
        f"SELECT l.key, l.service, l.title, l.url, {playlist}, l.fetched, {owners}, "
        f"count(ls.song_key) AS songs, count(s.file) AS have, {_TIER_SUMS} "
        "FROM lists l LEFT JOIN list_songs ls ON ls.list_key = l.key "
        f"LEFT JOIN songs s ON s.key = ls.song_key LEFT JOIN files f ON f.path = s.file {where} "
        "GROUP BY l.key ORDER BY l.position",
        args,
    ).fetchall()


_FOLLOWS = "SELECT key FROM sources WHERE user_id = ? AND enabled"


def get_list(con: Connection, key: str, uid: int | None = None) -> Row | None:
    """A list, if the user follows it (or anyone's, for None)."""
    if uid is None:
        return con.execute("SELECT * FROM lists WHERE key = ?", (key,)).fetchone()
    return con.execute(f"SELECT * FROM lists WHERE key = ? AND key IN ({_FOLLOWS})", (key, uid)).fetchone()


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


def missing(
    con: Connection, list_key: str | None = None, paths: Paths | None = None, uid: int | None = None
) -> list[dict[str, Any]]:
    """Songs of the followed lists (a user's, or everyone's) that are not in the library: their lists,
    what the searches saw (Soulseek, then YouTube and SoundCloud), the downloads rejected for them, and
    notes worth a glance."""
    mine, mine_args = _mine(uid)
    where, args = f"s.file IS NULL AND {mine}", list(mine_args)
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


def upload_songs(con: Connection, uid: int | None = None) -> list[Row]:
    """The songs (a user's, or everyone's) a file uploaded by hand can be: the missing ones, and those in the
    library with what their copy is (quality, kbps, a fake FLAC's lossy source; a close match: another
    version; shared: how many other songs have that file); with what a file is checked against."""
    mine, args = _mine(uid)
    return con.execute(
        "SELECT s.key, s.service, s.artist, s.artists, s.title, s.length, s.isrc, s.file, s.link, s.close_match, "
        "f.quality, f.kbps, (SELECT count(*) FROM songs o WHERE o.file = s.file AND o.key != s.key) AS shared, "
        "(SELECT source FROM lossy_sourced l WHERE s.file LIKE l.stem || '.%' AND length(s.file) - length(l.stem) <= 5) "
        f"AS fake_source FROM wanted s LEFT JOIN files f ON f.path = s.file WHERE {mine} "
        "ORDER BY s.artist COLLATE NOCASE, s.title COLLATE NOCASE",
        args,
    ).fetchall()


def close_matches(con: Connection, uid: int | None = None) -> list[Row]:
    """Wanted songs (a user's, or everyone's) covered by a close match: another version, taken in review."""
    mine, args = _mine(uid)
    return con.execute(
        "SELECT key, service, artist, title, length, url, file, f.duration FROM wanted s "
        f"JOIN files f ON f.path = s.file WHERE s.close_match AND {mine} "
        "ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE",
        args,
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
        "next_try": acquire.next_search(tries, r["last_try"]) if tries and r["service"] != "soundcloud" else None,
        "rejected": rejected,
        "in_review": paths is not None and filing.in_review(paths, r["artist"], r["title"]),
        "notes": notes,
    }


EVENT_FILTERS = {"added": ADDED, "rejected": REJECTED}


# what an event is called on the Activity page, and its colour (pill style)
EVENTS = {
    "new": ("Added", "ok"),
    "upgrade": ("Upgraded", "ok"),
    "wrong-song": ("Rejected", "bad"),
    "mismatch": ("Rejected", "bad"),
    "confirm": ("For review", "warn"),
    "duplicate": ("Already there", ""),
    "retired": ("Removed", "warn"),
    "renamed": ("Renamed", ""),
    "linked": ("Linked", ""),
    "recheck": ("Back in review", "warn"),
    "restored": ("Restored", ""),
    "retagged": ("Retagged", ""),
}
MATCHED = {"probable": ("probable match", "warn"), "review": ("from review", ""), "close": ("close match", "")}
REPLACED = "replaced by genuine lossless"  # a retired file's reason when an upgrade took its place


def activity(con: Connection, kind: str = "", limit: int = 300, uid: int | None = None) -> list[dict[str, Any]]:
    """The latest events for the Activity page (of a user's songs, or all), each as one entry: what
    happened, to which song, and the facts worth a glance. An upgrade and the file it replaced are one
    entry (from -> to); a user's include the files their songs' upgrades replaced."""
    actions = (*EVENT_FILTERS[kind], "retired") if kind in EVENT_FILTERS else None
    where, args = _mine(uid, "e.song")
    if uid is not None:  # a replaced file's event has no song: it goes with the upgrade (found below)
        where = f"({where} OR (e.action = 'retired' AND e.song IS NULL))"
    if actions:
        where += f" AND e.action IN ({', '.join('?' * len(actions))})"
    sql = f"SELECT e.*, s.url AS song_url FROM events e LEFT JOIN songs s ON s.key = e.song WHERE {where} "
    rows = con.execute(f"{sql} ORDER BY e.id DESC LIMIT ?", (*args, *(actions or ()), 2 * limit)).fetchall()
    replaced = {r["id"]: r for r in rows if r["action"] == "retired" and (r["reason"] or "").startswith(REPLACED)}
    partners = {e["id"]: was for e in rows if e["action"] == "upgrade" and (was := _replaced(e, replaced))}
    taken = {was["id"] for was in partners.values()}
    out = []
    for e in rows:
        if e["action"] == "retired" and (kind or e["id"] in taken or (uid is not None and not e["song"])):
            continue  # an upgrade's other half, or fetched only to find those
        entry = _entry(e)
        if was := partners.get(e["id"]):
            entry["was"], entry["was_bytes"] = _quality(was, lossy_flac=True), was["bytes"]
        out.append(entry)
    return out[:limit]


def _replaced(upgrade: Row, replaced: dict[int, Row]) -> Row | None:
    """The file an upgrade replaced: retired next to it (a few events apart), with the same name."""
    stem = _stem(upgrade["path"]).lower()
    for n in range(upgrade["id"] - 4, upgrade["id"] + 5):
        if (r := replaced.get(n)) and _stem(r["path"]).lower().startswith(stem):
            del replaced[n]
            return r
    return None


def _stem(path: str | None) -> str:
    return (path or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _quality(e: Row, lossy_flac: bool = False) -> tuple[str, int] | None:
    """(tier, kbps) of an event's file; a FLAC an upgrade replaced was one made from lossy."""
    if not e["ext"]:
        return None
    fake = bool(e["fake"]) or (lossy_flac and e["ext"] in catalog.LOSSLESS)
    return catalog.Entry(f"-/-.{e['ext']}", 0, e["kbps"] or 0, fake).quality, e["kbps"] or 0


def _entry(e: Row) -> dict[str, Any]:
    action, reason = e["action"], (e["reason"] or "").strip()
    label, style = EVENTS.get(action, (action.capitalize(), ""))
    song = f"{e['artist']} – {e['title']}" if e["artist"] else _stem(reason.rpartition("(was ")[2] or e["path"])
    song = "Your library" if action == "retagged" else song
    entry: dict[str, Any] = {"ts": e["ts"], "label": label, "style": style, "key": e["song"], "url": e["song_url"]}
    entry |= {"song": song, "source": e["source"], "quality": _quality(e), "seconds": e["seconds"], "bytes": e["bytes"]}
    entry |= {"tag": None, "note": "", "title": reason, "diff": None, "was": None, "was_bytes": None}
    if action in ("new", "upgrade", "duplicate"):
        entry["tag"] = MATCHED.get(e["matched"] or "")
    elif action in ("wrong-song", "mismatch", "confirm"):
        wanted = e["wanted_seconds"]
        entry["diff"] = e["seconds"] - wanted if action == "mismatch" and wanted and e["seconds"] else None
        entry["tag"] = ("FLAC for a SoundCloud song", "warn") if action == "confirm" else (_why(action, reason), "bad")
        entry["note"] = f"“{(e['found'] or e['file_name'] or '').strip()}”" if e["found"] or e["file_name"] else ""
    else:
        entry["quality"] = None if action in ("retired", "renamed", "linked") else entry["quality"]
        entry["note"] = _readable(action, reason)
    return entry


def _why(action: str, reason: str) -> str:
    """A rejection's reason in a few words (the whole one is the hover text)."""
    if action == "mismatch":
        return "another length"
    for test, why in (
        (reason.startswith("artist "), "another artist"),
        ("audio differs" in reason, "audio differs from the release"),
        ("marked wrong" in reason, "No match before"),
        ("another version" in reason or "lacks the version" in reason, "another version"),
        (reason.startswith("title ") or "reading of tag" in reason, "another title"),
        ("probable matches need a review" in reason, "kept for review"),
    ):
        if test:
            return why
    return reason.split(" (", 1)[0][:50] or "not the song"


def _readable(action: str, reason: str) -> str:
    """The reason of a removal, rename, link or second review, without file paths."""
    text = reason.split(" (was ", 1)[0].removeprefix("back in review: ")
    for wrong in ("no match in review", "marked wrong in review"):
        text = text.replace(wrong, "No match in review")
    text = text.replace(REPLACED, "replaced by a genuine FLAC")
    if action == "linked" and text.startswith("same recording as "):
        text = "same recording as " + _stem(text.removeprefix("same recording as ").split(" (", 1)[0])
    if action == "renamed" and " (was " in reason:
        text = "close match" if text.startswith("close match") else text
        text = f"{text} · was “{_stem(reason.rpartition('(was ')[2].rstrip(')'))}”"
    return text[:1].upper() + text[1:]


# ---------------------------------------------------------------- the availability tracker (Changes page)

CHANGES = {  # change -> (label, style)
    "added": ("Added", "ok"),
    "removed": ("Removed", "warn"),
    "replaced": ("Another release", ""),
    "re-uploaded": ("Re-uploaded", ""),
    "taken_down": ("Taken down", "bad"),
    "blocked": ("Not in this country", "bad"),
    "gone": ("Gone", "bad"),
    "preview": ("Preview only", "warn"),
    "available": ("Plays again", "ok"),
    "unreadable": ("List not readable", "bad"),
    "readable": ("List readable again", "ok"),
}
CHANGE_FILTERS = {
    "unavailable": ("taken_down", "blocked", "gone", "preview", "available", "replaced"),
    "lists": ("added", "removed", "re-uploaded", "unreadable", "readable"),
}


def unavailable_now(con: Connection, uid: int | None = None) -> tuple[list[Row], int]:
    """The songs of the lists (a user's, or everyone's) that do not play now: taken down, blocked, gone or
    a preview only, the latest first; and how many play as another release (Spotify relinks them: they play)."""
    mine, args = _mine(uid)
    rows = con.execute(
        "SELECT a.state, a.since, a.detail, s.key, s.service, s.artist, s.title, s.url, s.file FROM availability a "
        f"JOIN wanted s ON s.key = a.song_key WHERE a.state NOT IN ('available', 'replaced') AND {mine} "
        "ORDER BY a.state = 'preview', a.since DESC, s.artist COLLATE NOCASE",
        args,
    ).fetchall()
    relinked = "FROM availability a JOIN wanted s ON s.key = a.song_key WHERE a.state = 'replaced'"
    sql = f"SELECT count(*) {relinked} AND {mine}"
    return rows, con.execute(sql, args).fetchone()[0]


def changes(con: Connection, uid: int | None = None, kind: str = "", limit: int = 500) -> list[Row]:
    """The latest changes of songs and lists (a user's: their songs, or what happened in their lists)."""
    where, args = "1", ()
    if uid is not None:
        where, args = f"(c.song_key IN ({MINE}) OR c.list_key IN ({_FOLLOWS}))", (uid, uid)
    if kinds := CHANGE_FILTERS.get(kind):
        where += f" AND c.change IN ({', '.join('?' * len(kinds))})"
        args = (*args, *kinds)
    return con.execute(
        "SELECT c.*, s.artist, s.title, s.service, s.url, s.file, l.title AS list_title, "
        "r.artist AS new_artist, r.title AS new_title FROM changes c LEFT JOIN songs s ON s.key = c.song_key "
        "LEFT JOIN lists l ON l.key = c.list_key "
        "LEFT JOIN songs r ON r.key = c.detail AND c.change IN ('replaced', 're-uploaded') "
        f"WHERE {where} ORDER BY c.id DESC LIMIT ?",
        (*args, limit),
    ).fetchall()
