"""The availability tracker: whether every list's songs still play at their source (check, daily), and what
happened to them and to the lists (the changes table).

Spotify, through the app's own access in one market (options.Spotify.market): a song plays (available),
plays as another release of the recording (replaced: Spotify relinks it, or one with its ISRC plays that
Spotify does not link, or one of other names and ISRC that sounds as its file), plays nowhere (taken_down), not in that market (blocked), or exists no more (gone).
SoundCloud, through any user's token: gone (deleted or private: SoundCloud does not tell which), blocked
here (policy BLOCK), or a 30-second preview without Go+ (preview); a label release that can't be
downloaded still plays. YouTube, as its lists are read (lists.youtube): available, blocked or gone
(YouTube's reason as the detail).

A list's songs coming and going are recorded when it is read (record_list); why a removed song went, the
next check tells (_why_removed). A song first seen unavailable is no change. Spotify's greyed-out flag of
the searches (songs.unavailable) follows the check.
"""

import datetime
import json
import logging
import sqlite3
from typing import TYPE_CHECKING

from echolot import db
from echolot.library import identity, recordings, rules
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
SEARCH_AGAIN = datetime.timedelta(days=7)  # another release of a song that plays nowhere: looked for again after
SEARCHES = 50  # ... and at most this many per check
NAMED = 10  # tracks a search by a lost song's names looks at
RENAMED_LENGTH = 2  # s: a release of the recording under other names is as long


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- the lists' changes, as they are read


def record_list(con: sqlite3.Connection, list_key: str, before: list[str], after: list[str], first: bool) -> None:
    """A list's songs as last read and as now (no commit): the songs added and removed; a pair swapped for
    another release (rules.same_recording; on SoundCloud a re-upload) as one change. Nothing for a list's
    first reading (its songs were there before Echolot knew it)."""
    if first:
        return
    before_set, after_set = set(before), set(after)
    gone = [k for k in dict.fromkeys(before) if k not in after_set]
    new = [k for k in dict.fromkeys(after) if k not in before_set]
    if not gone and not new:
        return
    marks = ", ".join("?" * len(gone + new))
    songs = {r["key"]: r for r in con.execute(f"SELECT * FROM songs WHERE key IN ({marks})", gone + new)}
    now, rows = _now(), []
    for key in gone:
        twin = next((n for n in new if rules.same_recording(songs.get(key), songs.get(n))), None)
        if twin:
            new.remove(twin)
            swap = "re-uploaded" if key.startswith("soundcloud:") else "replaced"
            rows.append((now, key, list_key, swap, twin))
        else:
            rows.append((now, key, list_key, "removed", None))
    rows += [(now, key, list_key, "added", None) for key in new]
    con.executemany("INSERT INTO changes (ts, song_key, list_key, change, detail) VALUES (?, ?, ?, ?, ?)", rows)


UNREADABLE_AFTER = datetime.timedelta(hours=1)


def list_readable(con: sqlite3.Connection, list_key: str, readable: bool, why: str = "") -> None:
    """A list that can't be read (gone, private) is a change once it has failed for UNREADABLE_AFTER (a
    hiccup or a rate limit is none; meta "failing:<list>" holds since when), and again when it can be read."""
    last = con.execute(
        "SELECT change FROM changes WHERE list_key = ? AND song_key = '' ORDER BY id DESC LIMIT 1", (list_key,)
    ).fetchone()
    was = last["change"] if last else "readable"
    failing = f"failing:{list_key}"
    with con:
        if readable:
            con.execute("DELETE FROM meta WHERE key = ?", (failing,))
        elif not db.get_meta(con, failing):
            db.set_meta(con, failing, _now())
        since = db.get_meta(con, failing)
        long = bool(since) and datetime.datetime.fromisoformat(since) <= datetime.datetime.now() - UNREADABLE_AFTER
        if (readable and was != "readable") or (not readable and was == "readable" and long):
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
        try:
            _plays_as(con, run, states)
        except spotify.SpotifyError as e:
            log.warning("availability, relinked songs on Spotify: %s", e)
        explained = _why_removed(con, states)
    finally:
        con.close()
    counts = {s: sum(1 for st, _ in states.values() if st == s) for s in (*UNPLAYABLE, "preview", "replaced")}
    summary = ", ".join(f"{n} {s.replace('_', ' ')}" for s, n in counts.items() if n) or "all play"
    explained_text = f", {explained} removals explained" if explained else ""
    return f"{len(states)} songs checked: {summary}; {changed} changes{explained_text}"


def _spotify(con: sqlite3.Connection, run: "Run", keys: list[str]) -> dict[str, tuple[str, str | None]]:
    """The Spotify songs' states, in the market's catalogue (the app's own access: no user needed); their
    release facts are stored on the way (the tags: tagging.for_file)."""
    if not keys:
        return {}
    sp = spotify.Spotify(con, run.vault)
    market = options.get(con, options.Spotify).market
    out: dict[str, tuple[str, str | None]] = {}
    facts = []  # each song's place on its release (spotify.release_facts), for the tags
    lost: dict[str, str | None] = {}  # song key -> ISRC, of the ones that do not play here
    for n in range(0, len(keys), BATCH):
        if run.stop.is_set():
            break
        ids = [k.removeprefix("spotify:") for k in keys[n : n + BATCH]]
        run.say(f"Spotify: {n + len(ids)} of {len(keys)}", n, len(keys))
        tracks = sp.get(f"/tracks?ids={','.join(ids)}&market={market}").get("tracks") or []
        unplayable, relinked = [], []
        for sid, t in zip(ids, tracks, strict=False):
            if t and t.get("id") == sid:  # (a relinked one is another release: asked for again below)
                f = spotify.release_facts(t)
                facts.append((f["released"], f["track"], f["tracks"], f["disc"], f"spotify:{sid}"))
            if t is None:
                out[f"spotify:{sid}"] = ("gone", None)
            elif t.get("is_playable") is False and _restriction(t) not in ("explicit", "product"):
                unplayable.append(sid)  # nowhere, or only not here: asked without a market
            elif t.get("id") and t["id"] != sid:
                out[f"spotify:{sid}"] = ("replaced", f"spotify:{t['id']}")  # relinked to another release
                relinked.append(sid)
            else:
                out[f"spotify:{sid}"] = ("available", None)
        if unplayable:
            everywhere = sp.get(f"/tracks?ids={','.join(unplayable)}").get("tracks") or []
            for sid, t in zip(unplayable, everywhere, strict=False):
                markets = (t or {}).get("available_markets") or []
                out[f"spotify:{sid}"] = ("blocked" if markets else "taken_down", None)
                lost[f"spotify:{sid}"] = ((t or {}).get("external_ids") or {}).get("isrc")
        if relinked:  # asked without a market: the release in the list, for its facts
            for sid, t in zip(relinked, sp.get(f"/tracks?ids={','.join(relinked)}").get("tracks") or [], strict=False):
                if t and t.get("id") == sid:
                    f = spotify.release_facts(t)
                    facts.append((f["released"], f["track"], f["tracks"], f["disc"], f"spotify:{sid}"))
    with con:
        sql = "UPDATE songs SET released = ?, track = ?, tracks = ?, disc = ? WHERE key = ?"
        con.executemany(sql, facts)
    return out | _other_release(con, run, sp, market, lost, out)


def _other_release(
    con: sqlite3.Connection,
    run: "Run",
    sp: spotify.Spotify,
    market: str,
    lost: dict[str, str | None],
    states: dict[str, tuple[str, str | None]],
) -> dict[str, tuple[str, str | None]]:
    """Songs that do not play here as the release in their list: another release of the recording (by ISRC)
    that plays here, which Spotify does not relink them to (it links only some). One found before is checked
    again (looked for anew when it plays no more); one not found is looked for again after SEARCH_AGAIN,
    SEARCHES per check. Returns the ones found (replaced) and stores when each was looked for; `states`:
    their states without it."""
    if not lost:
        return {}
    known = {r["song_key"]: r for r in con.execute("SELECT song_key, state, detail, searched FROM availability")}
    found: dict[str, tuple[str, str | None]] = {}
    before = {k: known[k]["detail"] for k in lost if k in known and known[k]["state"] == "replaced"}
    before = {k: d for k, d in before.items() if (d or "").startswith("spotify:")}
    keys = list(before)
    for n in range(0, len(keys), BATCH):
        ids = [before[k].removeprefix("spotify:") for k in keys[n : n + BATCH]]
        tracks = sp.get(f"/tracks?ids={','.join(ids)}&market={market}").get("tracks") or []
        for key, t in zip(keys[n : n + BATCH], tracks, strict=False):
            if t and t.get("is_playable") is not False:
                found[key] = ("replaced", f"spotify:{t['id']}")
    due = (datetime.datetime.now() - SEARCH_AGAIN).isoformat(timespec="seconds")
    ask = [k for k in lost if k not in found]
    ask = [k for k in ask if k in before or not (k in known and (known[k]["searched"] or "") > due)][:SEARCHES]
    marks = ", ".join("?" * len(ask))
    songs = {r["key"]: r for r in con.execute(f"SELECT * FROM songs WHERE key IN ({marks})", ask)}
    searched = []
    for n, key in enumerate(ask):
        if run.stop.is_set():
            break
        run.say(f"Spotify: other releases, {n + 1} of {len(ask)}", n, len(ask))
        t = sp.by_isrc(isrc, market) if (isrc := lost[key]) else None
        if t is None and key in songs:
            t = _renamed(con, run, sp, market, songs[key])
        if t:
            found[key] = ("replaced", f"spotify:{t['id']}")
        searched.append(key)
    now, state = _now(), {k: found.get(k) or states[k] for k in searched}
    with con:  # (a song checked the first time gets its row here; apply sees no change then)
        con.executemany(
            "INSERT INTO availability (song_key, state, since, checked, detail, searched) VALUES (?, ?, '', ?, ?, ?) "
            "ON CONFLICT (song_key) DO UPDATE SET searched = excluded.searched",
            [(k, state[k][0], now, state[k][1], now) for k in searched],
        )
    return found


def _renamed(con: sqlite3.Connection, run: "Run", sp: spotify.Spotify, market: str, song: sqlite3.Row) -> dict | None:
    """A release of the song under other names and another ISRC that plays here (a self-release a label
    released again, renamed: "Fayahh - Robinson" as "Robinson - Fayahh (Ayo Girl & Love Me Back Beat)", its
    artist and title swapped): a track a search for its names finds, as long (RENAMED_LENGTH), naming its
    artist and title's core either way round, whose release sounds as the song's library file
    (identity.check). None without a file."""
    if not song["file"] or rules.mix_cut(song["title"]) or not (title := recordings.core(song["title"])):
        return None
    try:
        path = run.paths.tracks / song["file"]
    except RuntimeError:  # no library
        return None
    if not path.is_file():
        return None
    artists = [c for a in [song["artist"], *json.loads(song["artists"] or "[]")] if (c := recordings.core(a))]
    for t in sp.search(f"{song['artist']} {song['title']}", NAMED, market):
        names = f" {' '.join(recordings.core(n) for n in [t.get('name') or '', *_artists(t)])} "
        isrc = (t.get("external_ids") or {}).get("isrc") or ""
        if (
            t.get("id")
            and f"spotify:{t['id']}" != song["key"]
            and isrc
            and t.get("is_playable") is not False
            and abs((t.get("duration_ms") or 0) / 1000 - song["length"]) <= RENAMED_LENGTH
            and f" {title} " in names
            and any(f" {a} " in names for a in artists)
            and identity.check(con, isrc, path, length=(t.get("duration_ms") or 0) / 1000).verdict == "same"
        ):
            return t
    return None


def _artists(track: dict) -> list[str]:
    return [a.get("name") or "" for a in track.get("artists") or []]


def _plays_as(con: sqlite3.Connection, run: "Run", states: dict[str, tuple[str, str | None]]) -> None:
    """What each relinked song plays as (availability.plays): the release Spotify relinks it to, by its own
    names (asked without a market: with one, Spotify gives the list's names), once per release. The list's
    track keeps its names: the release may be renamed, or another recording."""
    relinked = {k: d for k, (st, d) in states.items() if st == "replaced" and (d or "").startswith("spotify:")}
    stored = con.execute("SELECT song_key, plays FROM availability WHERE plays IS NOT NULL")
    known = {r[0]: json.loads(r[1]).get("key") for r in stored}
    ask = sorted({d for k, d in relinked.items() if known.get(k) != d})
    found: dict[str, dict] = {}
    if ask:
        sp = spotify.Spotify(con, run.vault)
        for n in range(0, len(ask), BATCH):
            ids = [d.removeprefix("spotify:") for d in ask[n : n + BATCH]]
            for t in sp.get(f"/tracks?ids={','.join(ids)}").get("tracks") or []:
                if song := spotify.song_of_track(t):
                    found[f"spotify:{song['id']}"] = {"key": f"spotify:{song['id']}"} | {k: song[k] for k in PLAYS}
    with con:
        rows = [(json.dumps(found[d], ensure_ascii=False), k) for k, d in relinked.items() if d in found]
        con.executemany("UPDATE availability SET plays = ? WHERE song_key = ?", rows)
        con.execute("UPDATE availability SET plays = NULL WHERE plays IS NOT NULL AND state != 'replaced'")


PLAYS = ("artist", "artists", "title", "album", "length", "isrc")  # availability.plays, besides the key


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
    """Store the states and Spotify's greyed-out flag; returns the number of changes (a song seen the first
    time is none, and since when its state holds is unknown)."""
    known = {r["song_key"]: r for r in con.execute("SELECT * FROM availability")}
    now, today, changes = _now(), datetime.date.today().isoformat(), 0
    with con:
        for key, (state, detail) in states.items():
            old = known.get(key)
            if old is None:
                sql = "INSERT INTO availability (song_key, state, since, checked, detail) VALUES (?, ?, '', ?, ?)"
                con.execute(sql, (key, state, now, detail))
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


def _why_removed(con: sqlite3.Connection, checked: dict[str, tuple[str, str | None]]) -> int:
    """The removed songs' reason, for those checked in this run (a failed check leaves the rest for the next:
    their stored state could be days old)."""
    pending = [
        r
        for r in con.execute(
            "SELECT c.id, c.song_key, a.state FROM changes c JOIN availability a ON a.song_key = c.song_key "
            "WHERE c.change = 'removed' AND c.detail IS NULL"
        )
        if r["song_key"] in checked
    ]
    with con:
        con.executemany(
            "UPDATE changes SET detail = ? WHERE id = ?",
            [(WHY_REMOVED.get(r["state"], "removed from the list"), r["id"]) for r in pending],
        )
    return len(pending)
