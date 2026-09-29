"""Reading the music-sync pipeline's state files (/opt/sockseek/config), for the one-time takeover
(migrate.py): lists, songs, download attempts, lossy-sourced FLACs, the download log and the probes.

Layout of the pipeline directory:
  state/playlist-meta.json           list names from Spotify/SoundCloud
  state/spotify-<list>.json          current songs of a Spotify list
  state/soundcloud-order-<list>.json current track ids of a SoundCloud list
  state/soundcloud-tracks.json       SoundCloud track id -> artist, title, duration, stem
  state/spotify-unplayable.json      liked songs Spotify greys out
  state/attempts.json                songs Soulseek did not deliver yet
  state/lossy-sourced.json           FLACs made from lossy files
  state/song-links.json              song -> the library song it is (a review accept)
  logs/downloads.jsonl               every filing into the library
  logs/probe.jsonl                   availability probes
"""

import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from echolot import db
from echolot.sources import LIKES, Source, slug


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def import_state(con: sqlite3.Connection, root: Path, lists_: list[Source]) -> str:
    """Mirror lists (those of `lists_`, the sources table), songs, download attempts and lossy-sourced
    FLACs into the database."""
    state = root / "state"
    meta = _read_json(state / "playlist-meta.json", {})
    unplayable = set(_read_json(state / "spotify-unplayable.json", []))
    sc_tracks = _read_json(state / "soundcloud-tracks.json", {})
    links = _read_json(state / "song-links.json", {})

    def link(key: str) -> str | None:
        v = links.get(key)
        return json.dumps([v["artist"], v["title"]]) if isinstance(v, dict) and v.get("artist") else None

    lists, songs, list_songs = [], {}, []
    for src in lists_:
        keys = []
        if src.service == "spotify":
            items = _read_json(state / f"spotify-{slug(src.name)}.json", None)
            for it in items or []:
                key = f"spotify:{it['id']}"
                why = "greyed out on Spotify" if it["id"] in unplayable else None
                songs.setdefault(
                    key,
                    (
                        key,
                        "spotify",
                        it["artist"],
                        it["title"],
                        it.get("album") or "",
                        float(it.get("length") or 0),
                        why,
                        None,
                        json.dumps(it.get("artists") or [it["artist"]]),
                        link(key),
                    ),
                )
                keys.append(key)
        else:
            items = _read_json(state / f"soundcloud-order-{slug(src.name)}.json", None)
            for track_id in items or []:
                t = sc_tracks.get(track_id)
                if not t:  # listed but not downloaded or checked yet
                    continue
                key = f"soundcloud:{track_id}"
                why = "not downloadable on SoundCloud (DRM)" if t.get("unavailable") else None
                songs.setdefault(
                    key,
                    (
                        key,
                        "soundcloud",
                        t["artist"],
                        t["title"],
                        "",
                        float(t.get("duration") or 0),
                        why,
                        t.get("stem"),
                        None,
                        link(key),
                    ),
                )
                keys.append(key)
        default = src.name.replace("Spotify ", "") if src.name in LIKES else src.url
        title = src.title or meta.get(src.name, {}).get("title") or default
        fetched = int(items is not None)  # else: added, not fetched by the pipeline yet
        lists.append((src.key, src.service, title, src.url, len(lists), int(src.playlist), fetched))
        list_songs += [(src.key, n, key) for n, key in enumerate(keys)]

    attempts = [
        (key, a.get("n", 0), a.get("last"), a.get("fb")) for key, a in _read_json(state / "attempts.json", {}).items()
    ]
    lossy = [
        (stem, v.get("source"), v.get("detected")) for stem, v in _read_json(state / "lossy-sourced.json", {}).items()
    ]
    with con:
        con.execute("DELETE FROM list_songs")
        con.execute("DELETE FROM lists")
        con.executemany(
            "INSERT INTO lists (key, service, title, url, position, playlist, fetched) VALUES (?, ?, ?, ?, ?, ?, ?)",
            lists,
        )
        con.executemany(
            "INSERT INTO songs (key, service, artist, title, album, length, unavailable, stem, artists, link) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET "
            "artist = excluded.artist, title = excluded.title, album = excluded.album, "
            "length = excluded.length, unavailable = excluded.unavailable, stem = excluded.stem, "
            "artists = excluded.artists, link = excluded.link",
            songs.values(),
        )
        old = {r[0] for r in con.execute("SELECT key FROM songs")}
        con.executemany("DELETE FROM songs WHERE key = ?", [(k,) for k in old - songs.keys()])
        con.executemany("INSERT INTO list_songs VALUES (?, ?, ?)", list_songs)
        con.execute("DELETE FROM attempts")
        con.executemany("INSERT INTO attempts (song_key, tries, last_try, last_fallback) VALUES (?, ?, ?, ?)", attempts)
        con.execute("DELETE FROM lossy_sourced")
        con.executemany("INSERT INTO lossy_sourced VALUES (?, ?, ?)", lossy)
    events = import_events(con, root / "logs" / "downloads.jsonl")
    probes = import_probes(con, root / "logs" / "probe.jsonl")
    return f"{len(lists)} lists, {len(songs)} songs, {events} new events, {probes} new probes"


EVENT_FIELDS = [
    "ts",
    "action",
    "path",
    "ext",
    "bytes",
    "kbps",
    "seconds",
    "source",
    "artist",
    "title",
    "reason",
    "song",
    "matched",
    "found",
    "file_name",
    "fake",
    "tries",
    "wanted_seconds",
]


def _import_jsonl(
    con: sqlite3.Connection,
    path: Path,
    meta_key: str,
    table: str,
    fields: list[str],
    valid: Callable[[dict], bool],
    prepare: Callable[[dict], dict] = lambda e: e,
) -> int:
    """Append the JSON lines added to `path` since the last import (complete lines only) to
    `table`. A file that shrank was replaced: the table is filled again from the start."""
    if not path.exists():
        return 0
    offset = int(db.get_meta(con, meta_key, "0"))
    if path.stat().st_size < offset:
        offset = 0
        with con:
            con.execute(f"DELETE FROM {table}")
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read()
    data = data[: data.rfind(b"\n") + 1]
    rows = []
    for line in data.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and valid(e):
            e = prepare(e)
            rows.append([e.get(k) for k in fields])
    with con:
        con.executemany(f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})", rows)
        db.set_meta(con, meta_key, str(offset + len(data)))
    return len(rows)


def _event(e: dict) -> dict:
    ids = e.get("ids")
    song = ids[0] if isinstance(ids, list) and ids else None
    return {
        **e,
        "song": re.sub(r"^spotify:track:", "spotify:", song) if isinstance(song, str) else None,
        "matched": e.get("match"),
    }


def import_events(con: sqlite3.Connection, path: Path) -> int:
    """Append the lines of downloads.jsonl added since the last import."""
    return _import_jsonl(con, path, "events_offset", "events", EVENT_FIELDS,
                         lambda e: bool(e.get("ts") and e.get("action")), _event)  # fmt: skip


PROBE_FIELDS = ["ts", "artist", "title", "kind", "users", "lossless_users", "files"]


def import_probes(con: sqlite3.Connection, path: Path) -> int:
    """Append the availability probes logged since the last import (logs/probe.jsonl)."""
    return _import_jsonl(con, path, "probes_offset", "probes", PROBE_FIELDS,
                         lambda e: bool(e.get("ts") and e.get("title")))  # fmt: skip
