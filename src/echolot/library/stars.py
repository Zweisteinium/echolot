"""Hearts in Navidrome for the songs people like. A song in one of a user's likes lists (Spotify Liked Songs,
SoundCloud Likes, Echolot · Wished: sources.likes) gets a heart in their Navidrome account on its file, the
one their playlists play (songs.file). Echolot notes each heart it gave (the stars table) and acts only on
what changed since: a song liked or unliked, or one with another file now (an upgrade). It takes back only
its own hearts, of songs that left a list the user still follows: a heart the user gave first stays, one
they took back in a player is not given again, and a list they no longer follow leaves its hearts. A file
Navidrome has not read yet gets its heart at a later upkeep. Hearts are given in the order the songs were
liked (list_songs.added, across the lists), so a player's recently hearted follows it; Navidrome stamps each
heart when given. When that order changes (ORDER), Echolot gives its hearts again once, in the new order, as
soon as all of the user's likes are timed."""

import sqlite3
from collections import defaultdict
from typing import TYPE_CHECKING

from echolot import db

if TYPE_CHECKING:
    from echolot.services import navidrome

Key = tuple[str, str]  # (list key, song key)
FOLLOWED = (  # each user's likes lists, read at least once
    "SELECT so.user_id, so.key FROM sources so JOIN users u ON u.id = so.user_id AND NOT u.disabled "
    "JOIN lists l ON l.key = so.key AND l.fetched WHERE so.likes AND so.enabled"
)
UNTIMED = (
    f"SELECT 1 FROM ({FOLLOWED}) x JOIN list_songs ls ON ls.list_key = x.key WHERE x.user_id = ? AND ls.added IS NULL"
)
ORDER = "liked"  # the hearts' order (meta stars_order:<user id>): when the songs were liked


def _index(svc: "navidrome.Service", tracks: str) -> dict[str, str]:
    """Navidrome's song ids by file (relative to the library folder, named `tracks`, wherever that is in
    Navidrome's library)."""
    out: dict[str, str] = {}
    for s in svc.songs():
        path = "/" + (s.get("path") or "")
        at = path.find(f"/{tracks}/")
        if at >= 0 and not s.get("missing"):
            out.setdefault(path[at + len(tracks) + 2 :], s["id"])
    return out


def sync(con: sqlite3.Connection, svc: "navidrome.Service", tracks: str) -> str:
    """Give and take back the hearts of what changed in the likes lists since; what it did."""
    followed: dict[int, set[str]] = defaultdict(set)
    for uid, key in con.execute(FOLLOWED):
        followed[uid].add(key)
    want: dict[int, dict[Key, str]] = defaultdict(dict)  # in the order they get their hearts: the oldest like first
    songs = con.execute(
        f"SELECT x.user_id, x.key, ls.song_key, s.file FROM ({FOLLOWED}) x JOIN list_songs ls ON ls.list_key = x.key "
        "JOIN songs s ON s.key = ls.song_key WHERE s.file IS NOT NULL ORDER BY ls.added, ls.position DESC, x.key"
    )
    for uid, list_key, song_key, file in songs:
        want[uid][(list_key, song_key)] = file
    given: dict[int, dict[Key, tuple[str, int]]] = defaultdict(dict)
    for uid, list_key, song_key, file, ours in con.execute("SELECT user_id, list_key, song_key, file, ours FROM stars"):
        given[uid][(list_key, song_key)] = (file, ours)
    index: dict[str, str] | None = None
    hearts = taken = waiting = again = 0
    for uid, name in con.execute("SELECT id, name FROM users WHERE NOT disabled").fetchall():
        mine, had = want[uid], given[uid]
        new = {k: f for k, f in mine.items() if k not in had or had[k][0] != f}
        gone = [k for k in had if k not in mine]
        off = {had[k][0] for k in gone if had[k][1] and k[0] in followed[uid]}  # unliked (an unfollowed list's stay)
        ours = {f for f, o in had.values() if o}  # files with Echolot's heart
        redo = uid in followed and db.get_meta(con, f"stars_order:{uid}") != ORDER
        redo = redo and not con.execute(UNTIMED, (uid,)).fetchone()
        on: list[str] = []  # in the order the songs were liked
        rows = []
        if new or off or redo:
            index = _index(svc, tracks) if index is None else index
            starred = svc.starred(name)
            for k, f in mine.items():
                nid = index.get(f)
                if k not in new:  # given before: once more, in the new order
                    if redo and had[k][1] and nid in starred and nid not in on:
                        on.append(nid)
                        again += 1
                    continue
                if nid is None:
                    waiting += 1
                    continue
                if nid not in starred and nid not in on:
                    on.append(nid)
                if k in had and had[k][1]:
                    off.add(had[k][0])  # its file before (the song has a better one now)
                rows.append((uid, *k, f, had[k][1] if k in had else int(nid not in starred or f in ours)))
            back = [nid for f in off - set(mine.values()) if (nid := index.get(f)) and nid in starred]
            svc.star(name, on)
            svc.star(name, back, on=False)
            hearts, taken = hearts + len(on), taken + len(back)
        with con:
            if redo:
                db.set_meta(con, f"stars_order:{uid}", ORDER)
            con.executemany(
                "DELETE FROM stars WHERE user_id = ? AND list_key = ? AND song_key = ?", [(uid, *k) for k in gone]
            )
            con.executemany("INSERT OR REPLACE INTO stars VALUES (?, ?, ?, ?, ?)", rows)
    hearts -= again
    done = [f"{hearts} hearts given in Navidrome"] if hearts else []
    done += [f"{again} given again in the order they were liked"] if again else []
    done += [f"{taken} taken back"] if taken else []
    return ", ".join(done + ([f"{waiting} wait for Navidrome to read their file"] if waiting else []))
