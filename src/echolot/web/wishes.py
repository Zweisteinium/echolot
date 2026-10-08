"""Songs wished from Discover: a heart in a player (web/subsonic) or Get on the Discover page (web/discover).
A song Discover showed is kept (discover_songs, id "ex-<hash>") so a player or the page can come back to it; a
wish is a like (discover_likes), and the user's likes are their Wished list (jobs/lists.store_wished): searched
at once like a Spotify song, written as the playlist "Echolot · Wished"."""

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from fastapi import Request

from echolot.jobs import lists
from echolot.library import discover
from echolot.services import catalogs
from echolot.settings import auth

log = logging.getLogger(__name__)
PREFIX = "ex-"  # the ids of Discover's songs
KEEP_DAYS = 30  # a song shown and not wished is forgotten after this long


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def record(r: discover.Result) -> dict[str, Any]:
    hits = [{"source": h.source, "url": h.url, "preview": h.preview, "seconds": h.seconds} for h in r.sources]
    return {
        "artist": r.artist,
        "title": r.title,
        "seconds": r.seconds,
        "album": r.album,
        "year": r.year,
        "cover": r.cover,
        "hits": hits,
    }


def sid_of(song: dict[str, Any]) -> str:
    first = song["hits"][0] if song["hits"] else {}
    seed = f"{first.get('source')}|{first.get('url')}|{song['artist']}|{song['title']}"
    return PREFIX + hashlib.sha256(seed.encode()).hexdigest()[:20]


def state(con: sqlite3.Connection, user: auth.User | None, sid: str) -> str:
    if (
        not user
        or not con.execute("SELECT 1 FROM discover_likes WHERE user_id = ? AND song_id = ?", (user.id, sid)).fetchone()
    ):
        return "new"
    row = con.execute("SELECT file FROM songs WHERE key = ?", (song_key(sid),)).fetchone()
    return "here" if row and row[0] else "wished"


def song_key(sid: str) -> str:
    """The songs row a liked Discover song is wanted as."""
    return "discover:" + sid.removeprefix(PREFIX)


def song(con: sqlite3.Connection, sid: str) -> dict[str, Any] | None:
    row = con.execute("SELECT data FROM discover_songs WHERE id = ?", (sid,)).fetchone()
    return json.loads(row[0]) if row else None


def likes(con: sqlite3.Connection, user: auth.User | None) -> dict[str, str]:
    if not user:
        return {}
    rows = con.execute("SELECT song_id, liked FROM discover_likes WHERE user_id = ?", (user.id,))
    return dict(rows.fetchall())


def like(
    con: sqlite3.Connection, user: auth.User, sid: str, song: dict[str, Any], liked: bool, where: str = "in a player"
) -> None:
    """The user wishes the song (liked) or no longer does; a first wish asks Deezer for its ISRC."""
    if liked and "isrc" not in song:  # the recording's code (Deezer's): the audio check compares with its preview
        song["isrc"] = ""
        deezer = next((h["url"] for h in song["hits"] if h["source"] == "deezer"), "")
        if (track := deezer.rstrip("/").rsplit("/", 1)[-1]).isdigit():
            try:
                song["isrc"] = catalogs.track(track).get("isrc") or ""
            except catalogs.CatalogError as e:
                log.info("Deezer's ISRC of %s – %s: %s", song["artist"], song["title"], e)
        con.execute("UPDATE discover_songs SET data = ? WHERE id = ?", (json.dumps(song), sid))
    if liked:
        con.execute(
            "INSERT OR IGNORE INTO discover_likes (user_id, song_id, liked) VALUES (?, ?, ?)", (user.id, sid, _now())
        )
    else:
        con.execute("DELETE FROM discover_likes WHERE user_id = ? AND song_id = ?", (user.id, sid))
    con.commit()
    log.info(
        "%s %s %s – %s (Discover, %s)",
        user.name,
        "wished" if liked else "unwished",
        song["artist"],
        song["title"],
        where,
    )


def wish(request: Request, con: sqlite3.Connection, user: auth.User) -> None:
    """The user's Wished list as their hearts are now (jobs/lists.store_wished), and its songs searched at once
    (search_new: the songs never searched) and the playlist written (library)."""
    rows = con.execute(
        "SELECT s.id, s.data FROM discover_likes l JOIN discover_songs s ON s.id = l.song_id WHERE l.user_id = ? "
        "ORDER BY l.liked DESC, s.id",
        (user.id,),
    )
    songs = []
    for sid, data in rows:
        d = json.loads(data)
        pages = [h["url"] for h in sorted(d["hits"], key=lambda h: h["source"] != "soundcloud") if h["url"]]
        songs.append((song_key(sid), {**d, "url": pages[0] if pages else ""}))
    lists.store_wished(con, user.id, songs)
    for job in ("search_new", "library"):
        request.app.state.worker.trigger(job)


def keep(con: sqlite3.Connection, r: discover.Result, now: str) -> tuple[str, dict[str, Any]]:
    """A result kept to come back to (no commit): its id and record."""
    song = record(r)
    sid = sid_of(song)
    con.execute(
        "INSERT INTO discover_songs (id, data, seen) VALUES (?, ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET data = excluded.data, seen = excluded.seen",
        (sid, json.dumps(song), now),
    )
    return sid, song


def forget_old(con: sqlite3.Connection) -> None:
    """The kept songs not shown for KEEP_DAYS and not wished go (no commit)."""
    old = (datetime.now() - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    con.execute("DELETE FROM discover_songs WHERE seen < ? AND id NOT IN (SELECT song_id FROM discover_likes)", (old,))
