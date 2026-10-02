"""The availability tracker: whether the songs of every user's lists still play at their source, checked
once a day (check), and what happened to them and to the lists (the changes table), for everyone.

Spotify, through the app's own access in one country's catalogue (options.Spotify.market): a song plays
(available), plays as another release of the same recording (replaced: Spotify relinks it), plays
nowhere (taken_down: no country has it), not in that country (blocked), or exists no more (gone).
SoundCloud, through any user's token: a song is gone when it was deleted or made private (SoundCloud does
not tell which), blocked in this country (policy BLOCK), or a 30-second preview without Go+ (preview); a
label release that can't be downloaded still plays (available). YouTube, as its lists are read
(lists.youtube: each reading has every video): a video plays (available), not in this country (blocked),
or no more (gone: deleted, private, taken down; YouTube's reason as the detail).

A list's songs coming and going are recorded when the list is read (record_list): added, removed, or
replaced (a song swapped for another release: the same ISRC, or the same artist, title and length; on
SoundCloud a re-upload). Why a song was removed the next check tells: taken down, gone, or removed from
the list (by its owner). A song first seen unavailable is recorded so, not as a change; a list that can't
be read any more is a change (unreadable), and again when it can (readable). Spotify's greyed-out flag of
the searches (songs.unavailable) follows the check.
"""

import datetime
import logging
import sqlite3
from typing import TYPE_CHECKING

from echolot.library import rules
from echolot.services import soundcloud as sc_api
from echolot.services import spotify
from echolot.settings import options

if TYPE_CHECKING:
    from echolot.jobs.worker import Run

log = logging.getLogger(__name__)
GREYED_OUT = "greyed out on Spotify"  # songs.unavailable: searched first, before Spotify loses them for good
UNPLAYABLE = ("taken_down", "blocked", "gone")
WHY_REMOVED = {
    "taken_down": "taken down: it plays nowhere any more",
    "blocked": "no longer plays in this country",
    "gone": "it exists no more",
}
BATCH = 50  # songs per request (Spotify's and SoundCloud's limit)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- the lists' changes, as they are read


def record_list(con: sqlite3.Connection, list_key: str, before: list[str], after: list[str], first: bool) -> None:
    """A list's songs as last read and as now (no commit): the songs added and removed, a swapped pair as
    one change. Nothing for a list's first reading (its songs were there before Echolot knew it)."""
    if first:
        return
    gone = [k for k in dict.fromkeys(before) if k not in set(after)]
    new = [k for k in dict.fromkeys(after) if k not in set(before)]
    if not gone and not new:
        return
    marks = ", ".join("?" * len(gone + new))
    songs = {r["key"]: r for r in con.execute(f"SELECT * FROM songs WHERE key IN ({marks})", gone + new)}
    now, rows = _now(), []
    for key in gone:
        twin = next((n for n in new if _same_recording(songs.get(key), songs.get(n))), None)
        if twin:
            new.remove(twin)
            swap = "re-uploaded" if key.startswith("soundcloud:") else "replaced"
            rows.append((now, key, list_key, swap, twin))
        else:
            rows.append((now, key, list_key, "removed", None))
    rows += [(now, key, list_key, "added", None) for key in new]
    con.executemany("INSERT INTO changes (ts, song_key, list_key, change, detail) VALUES (?, ?, ?, ?, ?)", rows)


def _same_recording(a: sqlite3.Row | None, b: sqlite3.Row | None) -> bool:
    """Two songs of a list are one recording: the same ISRC, or the same artist and title, as long."""
    if a is None or b is None:
        return False
    if a["isrc"] and b["isrc"]:
        return a["isrc"] == b["isrc"]
    same_names = rules.fold(a["artist"]) == rules.fold(b["artist"]) and rules.fold(
        rules.release_title(a["title"])
    ) == rules.fold(rules.release_title(b["title"]))
    return same_names and abs((a["length"] or 0) - (b["length"] or 0)) <= 2


def list_readable(con: sqlite3.Connection, list_key: str, readable: bool, why: str = "") -> None:
    """A list that can't be read (gone, private) is a change once; again when it can be read."""
    last = con.execute(
        "SELECT change FROM changes WHERE list_key = ? AND song_key = '' ORDER BY id DESC LIMIT 1", (list_key,)
    ).fetchone()
    was = last["change"] if last else "readable"
    if readable != (was == "readable"):
        with con:
            con.execute(
                "INSERT INTO changes (ts, song_key, list_key, change, detail) VALUES (?, '', ?, ?, ?)",
                (_now(), list_key, "readable" if readable else "unreadable", why[:300] or None),
            )


# ---------------------------------------------------------------- the daily check


def check(run: "Run") -> str:
    """Check every song of every list (and the ones just removed, for why): record each one's state, a
    change where it differs from the last check, why the removed ones went, and the searches' flag."""
    con = run.connect()
    try:
        pending = "SELECT song_key FROM changes WHERE change = 'removed' AND detail IS NULL"
        keys = [r[0] for r in con.execute(f"SELECT key FROM wanted UNION {pending}")]
        states: dict[str, tuple[str, str | None]] = {}
        try:
            states |= _spotify(con, run, [k for k in keys if k.startswith("spotify:")])
        except spotify.SpotifyError as e:
            log.warning("availability on Spotify: %s", e)
        try:
            states |= _soundcloud(con, run, [k for k in keys if k.startswith("soundcloud:")])
        except sc_api.SoundCloudError as e:
            log.warning("availability on SoundCloud: %s", e)
        changed = apply(con, states)
        explained = _why_removed(con)
    finally:
        con.close()
    counts = {s: sum(1 for st, _ in states.values() if st == s) for s in (*UNPLAYABLE, "preview", "replaced")}
    summary = ", ".join(f"{n} {s.replace('_', ' ')}" for s, n in counts.items() if n) or "all play"
    explained_text = f", {explained} removals explained" if explained else ""
    return f"{len(states)} songs checked: {summary}; {changed} changes{explained_text}"


def _spotify(con: sqlite3.Connection, run: "Run", keys: list[str]) -> dict[str, tuple[str, str | None]]:
    """The Spotify songs' states, in the market's catalogue (the app's own access: no user needed)."""
    if not keys:
        return {}
    sp = spotify.Spotify(con, run.vault)
    market = options.get(con, options.Spotify).market
    out: dict[str, tuple[str, str | None]] = {}
    for n in range(0, len(keys), BATCH):
        if run.stop.is_set():
            break
        ids = [k.removeprefix("spotify:") for k in keys[n : n + BATCH]]
        run.say(f"Spotify: {n + len(ids)} of {len(keys)}", n, len(keys))
        tracks = sp.get(f"/tracks?ids={','.join(ids)}&market={market}").get("tracks") or []
        unplayable = []
        for sid, t in zip(ids, tracks, strict=False):
            if t is None:
                out[f"spotify:{sid}"] = ("gone", None)
            elif t.get("is_playable") is False and _restriction(t) not in ("explicit", "product"):
                unplayable.append(sid)  # nowhere, or only not here: asked without a market
            elif t.get("id") and t["id"] != sid:
                out[f"spotify:{sid}"] = ("replaced", f"spotify:{t['id']}")  # relinked to another release
            else:
                out[f"spotify:{sid}"] = ("available", None)
        if unplayable:
            everywhere = sp.get(f"/tracks?ids={','.join(unplayable)}").get("tracks") or []
            for sid, t in zip(unplayable, everywhere, strict=False):
                markets = (t or {}).get("available_markets") or []
                out[f"spotify:{sid}"] = ("blocked" if markets else "taken_down", None)
    return out


def _restriction(track: dict) -> str | None:
    """Why Spotify does not play a track here: market (not in it), or explicit / product (the account's
    settings or plan: it does play)."""
    return (track.get("restrictions") or {}).get("reason")


def _soundcloud(con: sqlite3.Connection, run: "Run", keys: list[str]) -> dict[str, tuple[str, str | None]]:
    """The SoundCloud songs' states (any user's token: they are public, or gone for everyone)."""
    token = sc_api.any_token(con, run.vault)
    if not keys or not token:
        return {}
    out: dict[str, tuple[str, str | None]] = {}
    for n in range(0, len(keys), BATCH):
        if run.stop.is_set():
            break
        ids = [k.removeprefix("soundcloud:") for k in keys[n : n + BATCH]]
        run.say(f"SoundCloud: {n + len(ids)} of {len(keys)}", n, len(keys))
        found = {str(t.get("id")): t for t in sc_api.tracks(token, ids)}
        for sid in ids:
            t = found.get(sid)
            if t is None:
                out[f"soundcloud:{sid}"] = ("gone", None)  # deleted or private
            elif t.get("policy") == "BLOCK":
                out[f"soundcloud:{sid}"] = ("blocked", None)
            elif t.get("policy") == "SNIP" or t.get("snipped"):
                out[f"soundcloud:{sid}"] = ("preview", None)
            else:
                out[f"soundcloud:{sid}"] = ("available", None)
    return out


def apply(con: sqlite3.Connection, states: dict[str, tuple[str, str | None]]) -> int:
    """Store the states (a change where one differs from the last check; a song seen for the first time is
    no change) and Spotify's greyed-out flag of the searches. Returns the number of changes."""
    known = {r["song_key"]: r for r in con.execute("SELECT * FROM availability")}
    now, today, changes = _now(), datetime.date.today().isoformat(), 0
    with con:
        for key, (state, detail) in states.items():
            old = known.get(key)
            if old is None:
                con.execute("INSERT INTO availability VALUES (?, ?, ?, ?, ?)", (key, state, today, now, detail))
            elif (old["state"], old["detail"]) != (state, detail):
                sql = "UPDATE availability SET state = ?, since = ?, checked = ?, detail = ? WHERE song_key = ?"
                con.execute(sql, (state, today, now, detail, key))
                con.execute(
                    "INSERT INTO changes (ts, song_key, change, detail) VALUES (?, ?, ?, ?)", (now, key, state, detail)
                )
                changes += 1
            else:
                con.execute("UPDATE availability SET checked = ? WHERE song_key = ?", (now, key))
        spotify_songs = [(k, st) for k, (st, _) in states.items() if k.startswith("spotify:")]
        flags = [(GREYED_OUT if st in UNPLAYABLE else None, k, GREYED_OUT) for k, st in spotify_songs]
        sql = "UPDATE songs SET unavailable = ? WHERE key = ? AND coalesce(unavailable, ?) = ?"
        con.executemany(sql, [(flag, k, g, g) for flag, k, g in flags])
    return changes


def _why_removed(con: sqlite3.Connection) -> int:
    """The removed songs' reason, now that their state is known."""
    pending = con.execute(
        "SELECT c.id, a.state FROM changes c JOIN availability a ON a.song_key = c.song_key "
        "WHERE c.change = 'removed' AND c.detail IS NULL"
    ).fetchall()
    with con:
        con.executemany(
            "UPDATE changes SET detail = ? WHERE id = ?",
            [(WHY_REMOVED.get(r["state"], "removed from the list"), r["id"]) for r in pending],
        )
    return len(pending)
