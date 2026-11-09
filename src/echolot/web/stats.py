"""Queries behind the pages. Each takes a user id (the songs on the lists they follow, their files and
events) or None (everyone's), see scope."""

import json
from datetime import datetime, timedelta
from sqlite3 import Connection, Row
from typing import Any

from echolot import db
from echolot.jobs import acquire
from echolot.jobs.availability import UNPLAYABLE
from echolot.jobs.lists import GREYED_OUT, NOT_ON_SOUNDCLOUD
from echolot.library import catalog, origin, recording, recordings, review
from echolot.library.filing import Paths

TIERS = [*catalog.TIERS, ("missing", "Missing")]  # best first; a fake FLAC counts in its source's tier
_TIER = "coalesce(f.tier, f.quality)"  # tier is set by the library job; until then the quality


def _limits(i: int) -> str:
    """A lossy tier's minimum bitrates (catalog.LIMITS index: 0 high, 1 medium)."""
    lim = catalog.LIMITS
    return f"MP3 {lim['mp3'][i]}, AAC and Vorbis {lim['aac'][i]}, Opus {lim['opus'][i]} kbps and up"


TIER_HINTS = {  # hover texts of the rings' legends
    "lossless": "Genuine FLAC: lossless, not made from a lossy file",
    "lossy-high": f"{_limits(0)}: as good as the original for nearly everyone",
    "lossy-mid": f"{_limits(1)}, below high: good, though not quite the original",
    "lossy-low": "Below medium: audibly lossy",
    "missing": "Not in the library yet",
}
MINE = (  # a user's songs: on the lists they follow
    "SELECT ls.song_key FROM list_songs ls JOIN sources src ON src.key = ls.list_key "
    "WHERE src.user_id = ? AND src.enabled"
)


def scope(user: Any) -> int | None:
    """The user's id, or None for everyone's (an admin who chose so)."""
    return None if user.everyone else user.id


def _mine(uid: int | None, column: str = "s.key") -> tuple[str, tuple]:
    """(SQL condition, args) for rows of the user's songs; always true for None."""
    return (f"{column} IN ({MINE})", (uid,)) if uid is not None else ("1", ())


def _their_lists(uid: int | None) -> tuple[str, tuple]:
    """(SQL condition on list_songs ls, its arguments): the user's own lists ('' for everyone's)."""
    return (f" AND ls.list_key IN ({_FOLLOWS})", (uid,)) if uid is not None else ("", ())


ADDED = ("new", "upgrade")
REJECTED = ("wrong-song", "mismatch", "damaged")


def overview(con: Connection, uid: int | None = None) -> dict[str, Any]:
    mine, args = _mine(uid)
    library = con.execute("SELECT count(*), coalesce(sum(size), 0) FROM files").fetchone()
    files_of = "1" if uid is None else f"path IN (SELECT s.file FROM songs s WHERE {mine})"
    files, size = con.execute(f"SELECT count(*), coalesce(sum(size), 0) FROM files WHERE {files_of}", args).fetchone()
    tiers_sql = f"SELECT {_TIER}, count(*), sum(f.quality = 'fake') FROM files f WHERE {files_of} GROUP BY 1"
    tiered = con.execute(tiers_sql, args).fetchall()
    by_tier, file_fakes = {r[0]: r[1] for r in tiered}, sum(r[2] or 0 for r in tiered)
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
        f"SELECT count(*) AS songs, count(s.file) AS have, {_TIER_SUMS}, coalesce(sum(f.quality = 'fake'), 0) AS fakes "
        "FROM wanted s "
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
        "library_files": library[0],  # always the whole library
        "library_size": library[1],
        "services": services,  # distinct songs and missing ones per service
        "song_tiers": tier_counts(songs),  # the ring's Songs (with the missing)
        "song_fakes": songs["fakes"],  # fake FLACs among them (counted in their source's tier)
        "files": files,
        "in_lists": in_lists,  # distinct files of the wanted songs (one file can be several songs)
        "size": size,
        "tiers": [(key, label, by_tier.get(key, 0)) for key, label in catalog.TIERS],  # the ring's Files
        "file_fakes": file_fakes,
        "lossless": by_tier.get("lossless", 0),
        "wanted": wanted,
        "have": have,
        "close": close,  # of 'have': covered by a close match
        "not_found": not_found,
        "added_24h": sum(day.get(a, (0, 0))[0] for a in ADDED),
        "added_bytes_24h": sum(day.get(a, (0, 0))[1] for a in ADDED),
        "rejected_24h": sum(day.get(a, (0, 0))[0] for a in REJECTED),
        "lists": lists(con, uid),
    }


_TIER_SUMS = ", ".join(f"coalesce(sum({_TIER} = '{k}'), 0) AS \"{k}\"" for k, _ in catalog.TIERS)


def tier_counts(row: Row | dict) -> list[tuple[str, str, int]]:
    """(tier, label, songs) of a row with a column per tier, 'songs' and 'have'."""
    counts = [(k, label, row[k] or 0) for k, label in catalog.TIERS]
    return [*counts, ("missing", "Missing", (row["songs"] or 0) - (row["have"] or 0))]


def tiers_of(songs: list[Row]) -> list[tuple[str, str, int]]:
    """tier_counts of a list of songs (list_songs rows)."""
    n: dict[str, int] = {}
    for r in songs:
        key = (r["tier"] or r["quality"] or "lossy-low") if r["file"] else "missing"
        n[key] = n.get(key, 0) + 1
    return [(k, label, n.get(k, 0)) for k, label in TIERS]


# as recording._mark sees a song (keep in step with recording.marks and origin.marks)
_OWN = "(s.file IS NOT NULL AND NOT s.close_match)"  # its own copy, not only a close match
_GONE = f"(s.unavailable IS NOT NULL OR av.state IN {UNPLAYABLE})"  # Spotify no longer plays it
_HELD = f"s.key IN ({recording.HELD})"  # Spotify has nothing better of it
_ORIGINAL = f"s.key IN ({origin.BEST})"  # its file is the artist's own release
# at the best quality there is: genuine lossless, held, gone from Spotify, or the artist's own release
_BEST = (
    f"coalesce(sum({_OWN} AND (f.quality = 'lossless' OR {_HELD} OR {_ORIGINAL} "
    f"OR (s.key LIKE 'spotify:%' AND {_GONE}))), 0) AS best"
)
# searched everywhere in vain: Soulseek at its loosest (acquire.LOOSEN), YouTube and SoundCloud too
_SEARCHED_OUT = "s.key IN (SELECT song_key FROM attempts WHERE tries >= 4 AND last_fallback > 0)"
# gone from Spotify (or Spotify has nothing better) and searched out: left out of the Best share
_WIPED = f"({_GONE} OR {_HELD}) AND {_SEARCHED_OUT}"
_UNOBTAINABLE = f"coalesce(sum(NOT {_OWN} AND {_WIPED}), 0) AS unobtainable"


BEST_TIERS = [  # (key, label, hover text), best first
    (
        "best",
        "At their best",
        "genuine lossless, or the best copy there is: the artist's own release, Spotify has nothing better, or "
        "no longer plays it",
    ),
    ("better", "Could be better", "lossy (a fake FLAC too), a close match, or Spotify played another song lately"),
    ("missing", "Missing · still searching", "not in the library yet: searched for (or about to be)"),
    (
        "gone",
        "Missing · seems wiped off the internet :(",
        "gone from Spotify (greyed out, taken down) and searched in vain at Soulseek's loosest, on YouTube and "
        "SoundCloud",
    ),
]


def best_tiers(con: Connection, uid: int | None = None) -> list[tuple[str, str, int]]:
    """(tier, label, songs) on BEST_TIERS, counted as the lists' Best column."""
    mine, args = _mine(uid)
    gone = f"coalesce(sum(s.file IS NULL AND {_WIPED}), 0)"
    best, gone, have, songs = con.execute(
        f"SELECT {_BEST}, {gone}, count(s.file), count(*) FROM wanted s LEFT JOIN files f ON f.path = s.file "
        f"LEFT JOIN availability av ON av.song_key = s.key WHERE {mine}",
        args,
    ).fetchone()
    counts = {"best": best, "better": have - best, "missing": songs - have - gone, "gone": gone}
    return [(k, label, counts[k]) for k, label, _ in BEST_TIERS]


def lists(con: Connection, uid: int | None = None) -> list[Row]:
    """The followed lists (a user's with their playlist choice, or everyone's with their followers), with
    counts per tier (tier_counts), best and unobtainable."""
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
        f"count(ls.song_key) AS songs, count(s.file) AS have, {_TIER_SUMS}, {_BEST}, {_UNOBTAINABLE} "
        "FROM lists l LEFT JOIN list_songs ls ON ls.list_key = l.key "
        "LEFT JOIN songs s ON s.key = ls.song_key LEFT JOIN files f ON f.path = s.file "
        f"LEFT JOIN availability av ON av.song_key = s.key {where} "
        "GROUP BY l.key ORDER BY l.position",
        args,
    ).fetchall()


_FOLLOWS = "SELECT key FROM sources WHERE user_id = ? AND enabled"


def offers(con: Connection) -> dict[str, Row]:
    """Song key -> its Bandcamp offer (price or free)."""
    sql = "SELECT * FROM bandcamp WHERE url > '' AND (price IS NOT NULL OR free)"
    return {r["song_key"]: r for r in con.execute(sql)}


def links(con: Connection) -> dict[str, Row]:
    """Song key -> its free download outside Bandcamp (services/free_links)."""
    return {r["song_key"]: r for r in con.execute("SELECT * FROM free_links WHERE url > ''")}


def buy(con: Connection, uid: int | None = None) -> list[Row]:
    """The Buy page: songs without a genuine lossless copy that Bandcamp sells or gives away (a free download
    that did not come in), or with a free download elsewhere; free ones first, then the cheapest."""
    mine, args = _mine(uid)
    theirs, their_args = _their_lists(uid)
    return con.execute(
        "SELECT s.key, s.artist, s.title, s.length, s.file, f.quality, f.kbps, b.url AS offer, b.artist AS offer_artist, "
        "b.title AS offer_title, b.price, b.currency, b.free, b.result, b.checked, fl.url AS link, fl.site AS "
        "link_site, fl.label AS link_label, fl.fetch AS link_fetch, fl.result AS link_result, "
        "(SELECT group_concat(l.title, ' · ') FROM list_songs ls JOIN lists l ON l.key = ls.list_key "
        f" WHERE ls.song_key = s.key{theirs}) AS in_lists "
        "FROM wanted s LEFT JOIN files f ON f.path = s.file "
        "LEFT JOIN bandcamp b ON b.song_key = s.key AND b.url > '' AND (b.price IS NOT NULL OR b.free) "
        "LEFT JOIN free_links fl ON fl.song_key = s.key AND fl.url > '' AND NOT EXISTS (SELECT 1 FROM origins o "
        f"WHERE o.song_key = s.key AND o.kind = 'free') AND NOT {_ORIGINAL} "  # the gate gave what it has
        "WHERE (b.song_key IS NOT NULL OR fl.song_key IS NOT NULL) AND (s.file IS NULL OR f.quality != 'lossless') "
        f"AND {mine} ORDER BY (coalesce(b.free, 0) OR fl.song_key IS NOT NULL) DESC, b.price IS NULL, b.price, "
        "s.artist COLLATE NOCASE",
        (*their_args, *args),
    ).fetchall()


def get_list(con: Connection, key: str, uid: int | None = None) -> Row | None:
    """A list, if the user follows it (or anyone's, for None)."""
    if uid is None:
        return con.execute("SELECT * FROM lists WHERE key = ?", (key,)).fetchone()
    return con.execute(f"SELECT * FROM lists WHERE key = ? AND key IN ({_FOLLOWS})", (key, uid)).fetchone()


def list_songs(con: Connection, key: str) -> list[Row]:
    return con.execute(
        "SELECT ls.position, s.key, s.service, s.artist, s.title, s.length, s.unavailable, "
        f"{db.play_url()} AS url, s.isrc, "
        "s.file, s.close_match, f.quality, f.tier, f.kbps, a.state FROM list_songs ls JOIN songs s ON s.key = ls.song_key "
        "LEFT JOIN files f ON f.path = s.file LEFT JOIN availability a ON a.song_key = s.key "
        "WHERE ls.list_key = ? ORDER BY ls.position",
        (key,),
    ).fetchall()


def same_as(songs: list[Row]) -> dict[int, int]:
    """position -> the first position of the same recording (same file, else same ISRC): Spotify lists a
    recording once per release, so a list can hold it twice."""
    first: dict[str, int] = {}
    out = {}
    for s in songs:
        marks = [m for m in (s["file"] and f"file:{s['file']}", s["isrc"] and f"isrc:{s['isrc']}") if m]
        if (seen := next((first[m] for m in marks if m in first), None)) is not None:
            out[s["position"]] = seen
        else:
            first.update(dict.fromkeys(marks, s["position"]))
    return out


UNAVAILABLE = {
    GREYED_OUT: "Spotify no longer plays it (withdrawn or not in your country); it is searched first.",
    NOT_ON_SOUNDCLOUD: "SoundCloud hands this upload out to nobody (a label release); YouTube and SoundCloud "
    "search for other uploads.",
}


NOTES = {  # -> (text, style, hover text)
    "drm": ("DRM on SoundCloud", "bad", "It fits, but SoundCloud serves it encrypted only (a label release)."),
    "stalled": ("stalls at peers", "warn", "Soulseek users have it, but none started the transfer. It is tried again."),
}


def missing(
    con: Connection, list_key: str | None = None, paths: Paths | None = None, uid: int | None = None
) -> list[dict[str, Any]]:
    """Missing songs with their lists, what the searches saw, recent rejected downloads and notes."""
    mine, mine_args = _mine(uid)
    theirs, their_args = _their_lists(uid)
    where, args = f"s.file IS NULL AND {mine}", [*their_args, *mine_args]
    if list_key:
        where += " AND EXISTS (SELECT 1 FROM list_songs WHERE song_key = s.key AND list_key = ?)"
        args.append(list_key)
    rows = con.execute(
        f"SELECT s.key, s.service, s.artist, s.title, s.length, s.unavailable, {db.play_url()} AS url, s.isrc, "
        "s.file, s.close_match, NULL AS quality, (SELECT state FROM availability WHERE song_key = s.key) AS state, "
        "a.tries, a.last_try, a.last_fallback, a.result, a.fallback_result, "
        "(SELECT group_concat(place, ' · ') FROM (SELECT l.title || ' #' || (ls.position + 1) AS place "
        f" FROM list_songs ls JOIN lists l ON l.key = ls.list_key WHERE ls.song_key = s.key{theirs} "
        " ORDER BY l.position, ls.position)) AS in_lists "
        f"FROM wanted s LEFT JOIN attempts a ON a.song_key = s.key WHERE {where} "
        "ORDER BY s.artist COLLATE NOCASE, s.title COLLATE NOCASE",
        args,
    ).fetchall()
    twins = recordings.twins(rows)  # one recording listed twice: one row with both places
    places: dict[str, list[str]] = {}
    for r in rows:
        if r["in_lists"]:
            places.setdefault(twins.get(r["key"], r["key"]), []).append(r["in_lists"])
    rows = [r for r in rows if r["key"] not in twins]
    rejected: dict[str, list[Row]] = {}
    if keys := [r["key"] for r in rows]:
        for e in con.execute(
            "SELECT song, ts, action, source, found, file_name, reason, seconds, wanted_seconds FROM events "
            f"WHERE action IN ('wrong-song', 'mismatch') AND song IN ({', '.join('?' * len(keys))}) ORDER BY id DESC",
            keys,
        ):
            if len(rejected.setdefault(e["song"], [])) < 3:
                rejected[e["song"]].append(e)
    waits = review.waiting(con, paths.music) if paths is not None else set()
    return [
        _tried(r, rejected.get(r["key"], []), waits) | {"in_lists": " · ".join(places.get(r["key"], []))} for r in rows
    ]


def upload_songs(con: Connection, uid: int | None = None, key: str | None = None) -> list[Row]:
    """The songs an uploaded file can be (`key`: only that one), with their copy (quality, kbps, a fake
    FLAC's source, close match, shared: other songs with that file). Shares are counted in one pass:
    songs.file has no index."""
    mine, args = _mine(uid)
    one = " AND s.key = ?" if key is not None else ""
    return con.execute(
        "SELECT s.key, s.service, s.artist, s.artists, s.title, s.length, s.isrc, s.file, s.link, s.close_match, "
        "f.quality, f.kbps, coalesce(c.n, 1) - 1 AS shared, "
        "(SELECT source FROM lossy_sourced l WHERE s.file LIKE l.stem || '.%' AND length(s.file) - length(l.stem) <= 5) "
        "AS fake_source FROM wanted s LEFT JOIN files f ON f.path = s.file "
        "LEFT JOIN (SELECT file, count(*) AS n FROM songs WHERE file IS NOT NULL GROUP BY file) c ON c.file = s.file "
        f"WHERE {mine}{one} ORDER BY s.artist COLLATE NOCASE, s.title COLLATE NOCASE",
        (*args, key) if key is not None else args,
    ).fetchall()


def close_matches(con: Connection, uid: int | None = None) -> list[Row]:
    """Wanted songs covered by a close match."""
    mine, args = _mine(uid)
    return con.execute(
        f"SELECT key, service, artist, title, length, {db.play_url()} AS url, file, f.duration FROM wanted s "
        f"JOIN files f ON f.path = s.file WHERE s.close_match AND {mine} "
        "ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE",
        args,
    ).fetchall()


def _tried(r: Row, rejected: list[Row], waits: set[str]) -> dict[str, Any]:
    tries = r["tries"] or 0
    result = json.loads(r["result"]) if r["result"] else None
    fallback = json.loads(r["fallback_result"]) if r["fallback_result"] else None
    notes = []  # (text, style, hover text)
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
        "in_review": r["key"] in waits,
        "notes": notes,
    }


EVENT_FILTERS = {"added": ADDED, "rejected": REJECTED}


EVENTS = {  # action -> (label on the Activity page, pill style)
    "new": ("Added", "ok"),
    "upgrade": ("Upgraded", "ok"),
    "wrong-song": ("Rejected", "bad"),
    "mismatch": ("Rejected", "bad"),
    "damaged": ("Rejected", "bad"),
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
# a retired file's reason starts with: a genuine lossless upgrade, any upgrade (filing.file_into), or
# another file of the same audio (recordings.merge_same)
REPLACED = "replaced by genuine lossless"
UPGRADED = "replaced by "
SAME_AUDIO = "the same audio as "


def activity(con: Connection, kind: str = "", limit: int = 300, uid: int | None = None) -> list[dict[str, Any]]:
    """The latest events as Activity entries. An upgrade and the file it replaced are one entry (from -> to),
    as are a link and the same-audio file it replaced."""
    actions = (*EVENT_FILTERS[kind], "retired") if kind in EVENT_FILTERS else None
    where, args = _mine(uid, "e.song")
    if uid is not None:  # a replaced file's event has no song: fetched to pair it with its upgrade
        where = f"({where} OR (e.action = 'retired' AND e.song IS NULL))"
    if actions:
        where += f" AND e.action IN ({', '.join('?' * len(actions))})"
    sql = f"SELECT e.*, {db.play_url()} AS song_url FROM events e LEFT JOIN songs s ON s.key = e.song WHERE {where} "
    rows = con.execute(f"{sql} ORDER BY e.id DESC LIMIT ?", (*args, *(actions or ()), 2 * limit)).fetchall()
    replaced = {r["id"]: r for r in rows if r["action"] == "retired" and (r["reason"] or "").startswith(UPGRADED)}
    partners = {e["id"]: was for e in rows if e["action"] == "upgrade" and (was := _replaced(e, replaced))}
    merged = {r["id"]: r for r in rows if r["action"] == "retired" and (r["reason"] or "").startswith(SAME_AUDIO)}
    gone = {e["id"]: was for e in rows if e["action"] == "linked" and (was := _merged(e, merged))}
    taken = {was["id"] for was in (*partners.values(), *gone.values())}
    out = []
    for e in rows:
        if e["action"] == "retired" and (kind or e["id"] in taken or (uid is not None and not e["song"])):
            continue  # paired with an upgrade, or fetched only for pairing
        entry = _entry(e)
        if was := partners.get(e["id"]):
            entry["was"], entry["was_bytes"] = _quality(was, was["reason"].startswith(REPLACED)), was["bytes"]
        if was := gone.get(e["id"]):
            entry["note"] = f"Same audio as {_stem(e['path'])} · its own file removed"
        out.append(entry)
    return out[:limit]


def _replaced(upgrade: Row, replaced: dict[int, Row]) -> Row | None:
    """The file an upgrade replaced: retired within a few events, with the same name."""
    stem = _stem(upgrade["path"]).lower()
    for n in range(upgrade["id"] - 4, upgrade["id"] + 5):
        if (r := replaced.get(n)) and _stem(r["path"]).lower().startswith(stem):
            del replaced[n]
            return r
    return None


def _merged(link: Row, merged: dict[int, Row]) -> Row | None:
    """The same-audio file a link replaced: retired within a few events, naming the linked file."""
    for n in range(link["id"] - 4, link["id"] + 5):
        if (r := merged.get(n)) and r["reason"].startswith(f"{SAME_AUDIO}{link['path']} "):
            del merged[n]
            return r
    return None


def _stem(path: str | None) -> str:
    return (path or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _quality(e: Row, lossy_flac: bool = False) -> tuple[str, int] | None:
    """(tier, kbps) of an event's file. `lossy_flac`: a FLAC replaced by a genuine lossless upgrade was a
    fake (one replaced by hand can be genuine)."""
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
    elif action in (*REJECTED, "confirm"):
        wanted = e["wanted_seconds"]
        entry["diff"] = e["seconds"] - wanted if action == "mismatch" and wanted and e["seconds"] else None
        entry["tag"] = ("FLAC for a SoundCloud song", "warn") if action == "confirm" else (_why(action, reason), "bad")
        entry["note"] = f"“{(e['found'] or e['file_name'] or '').strip()}”" if e["found"] or e["file_name"] else ""
    else:
        entry["quality"] = None if action in ("retired", "renamed", "linked") else entry["quality"]
        entry["note"] = _readable(action, reason)
    return entry


def _why(action: str, reason: str) -> str:
    """A rejection's reason in a few words (the full one is the hover text)."""
    if action == "mismatch":
        return "another length"
    for test, why in (
        (reason.startswith("artist "), "another artist"),
        ("audio differs" in reason, "audio differs from the release"),
        ("marked wrong" in reason, "No match before"),
        ("another version" in reason or "lacks the version" in reason, "another version"),
        (reason.startswith("title ") or "reading of tag" in reason, "another title"),
        ("probable matches need a review" in reason, "kept for review"),
        (reason.startswith(("damaged FLAC", "cut-off FLAC")), "damaged file"),
    ):
        if test:
            return why
    return reason.split(" (", 1)[0][:50] or "not the song"


def _readable(action: str, reason: str) -> str:
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


# ---------------------------------------------------------------- Changes page

CHANGES = {  # change -> (label, pill style)
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
    """Songs that do not play now (taken down, blocked, gone, preview only), latest first; and how many play
    as another release (Spotify relinks them)."""
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
    """The latest changes; a user's: of their songs or in their lists."""
    where, args = "1", ()
    if uid is not None:
        where, args = f"(c.song_key IN ({MINE}) OR c.list_key IN ({_FOLLOWS}))", (uid, uid)
    if kinds := CHANGE_FILTERS.get(kind):
        where += f" AND c.change IN ({', '.join('?' * len(kinds))})"
        args = (*args, *kinds)
    return con.execute(
        f"SELECT c.*, s.artist, s.title, s.service, {db.play_url()} AS url, s.file, l.title AS list_title, "
        "r.artist AS new_artist, r.title AS new_title FROM changes c LEFT JOIN songs s ON s.key = c.song_key "
        "LEFT JOIN lists l ON l.key = c.list_key "
        "LEFT JOIN songs r ON r.key = c.detail AND c.change IN ('replaced', 're-uploaded') "
        f"WHERE {where} ORDER BY c.id DESC LIMIT ?",
        (*args, limit),
    ).fetchall()
