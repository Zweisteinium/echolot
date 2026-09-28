"""Read-only import from the music-sync pipeline (/opt/sockseek/config), which still does the
downloading. Its state files are mirrored into the database; nothing there is ever written.

Layout of the pipeline directory:
  sources.yml                        the lists to follow
  state/playlist-meta.json           list names from Spotify/SoundCloud
  state/spotify-<list>.json          current songs of a Spotify list
  state/soundcloud-order-<list>.json current track ids of a SoundCloud list
  state/soundcloud-tracks.json       SoundCloud track id -> artist, title, duration, stem
  state/spotify-unplayable.json      liked songs Spotify greys out
  state/attempts.json                songs Soulseek did not deliver yet
  state/lossy-sourced.json           FLACs made from lossy files
  state/song-links.json              song -> the library song it is (a review accept)
  state/library-cache.json           duration and bitrate of library files
  state/PAUSED                       downloads paused
  logs/downloads.jsonl               every filing into the library
  logs/<job>.log                     output of each job, with start/done lines
"""

import json
import os
import re
import sqlite3
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from echolot import db
from echolot.library import Known

PIPELINE_TRACKS = "/music/tracks/"  # library root as the pipeline sees it
LIKES = {"Spotify Liked Songs", "SoundCloud Likes"}  # pipeline names of the likes lists
JOBS = {  # log name -> label
    "sync": "Spotify → Soulseek",
    "sweep": "Missing songs sweep",
    "probe": "Availability probe",
    "soundcloud": "SoundCloud",
    "fallback": "YouTube fallback",
    "upgrade": "Weekly FLAC upgrade",
}


@dataclass(frozen=True)
class Source:
    key: str  # Echolot's list key
    name: str  # the pipeline's list name (its state files are named after it)
    service: str
    url: str
    title: str | None  # title override from sources.yml
    playlist: bool


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _entries(value: Any) -> list[dict[str, Any]]:
    return [{"url": e} if isinstance(e, str) else dict(e) for e in value or []]


def _options(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def sources(config: dict[str, Any]) -> list[Source]:
    """The lists in sources.yml, in order (same keys the pipeline derives)."""
    out = []
    spotify = config.get("spotify") or {}
    if likes := spotify.get("likes"):
        o = _options(likes)
        out.append(
            Source(
                "spotify:likes",
                "Spotify Liked Songs",
                "spotify",
                "https://open.spotify.com/collection/tracks",
                o.get("title"),
                o.get("playlist", True) is not False,
            )
        )
    for e in _entries(spotify.get("playlists")):
        m = re.search(r"playlist[/:]([A-Za-z0-9]+)", e["url"])
        pid = m.group(1) if m else slug(e["url"])
        out.append(
            Source(
                f"spotify:playlist:{pid}",
                f"spotify-{pid}",
                "spotify",
                f"https://open.spotify.com/playlist/{pid}",
                e.get("title"),
                e.get("playlist", True) is not False,
            )
        )
    soundcloud = config.get("soundcloud") or {}
    if (likes := soundcloud.get("likes")) and (user := soundcloud.get("user")):
        o = _options(likes)
        out.append(
            Source(
                f"soundcloud:{user}/likes",
                "SoundCloud Likes",
                "soundcloud",
                f"https://soundcloud.com/{user}/likes",
                o.get("title"),
                o.get("playlist", True) is not False,
            )
        )
    for e in _entries(soundcloud.get("playlists")):
        path = urllib.parse.urlparse(e["url"]).path.strip("/")
        out.append(
            Source(
                f"soundcloud:{path}",
                "soundcloud-" + slug(path.replace("/sets/", "-")),
                "soundcloud",
                f"https://soundcloud.com/{path}",
                e.get("title"),
                e.get("playlist", True) is not False,
            )
        )
    return out


def import_state(con: sqlite3.Connection, root: Path) -> str:
    """Mirror lists, songs, download attempts and lossy-sourced FLACs into the database."""
    state = root / "state"
    config = yaml.safe_load((root / "sources.yml").read_text(encoding="utf-8")) or {}
    meta = _read_json(state / "playlist-meta.json", {})
    unplayable = set(_read_json(state / "spotify-unplayable.json", []))
    sc_tracks = _read_json(state / "soundcloud-tracks.json", {})
    links = _read_json(state / "song-links.json", {})

    def link(key: str) -> str | None:
        v = links.get(key)
        return (
            json.dumps([v["artist"], v["title"]])
            if isinstance(v, dict) and v.get("artist")
            else None
        )

    lists, songs, list_songs = [], {}, []
    for src in sources(config):
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
        (key, a.get("n", 0), a.get("last"), a.get("fb"))
        for key, a in _read_json(state / "attempts.json", {}).items()
    ]
    lossy = [
        (stem, v.get("source"), v.get("detected"))
        for stem, v in _read_json(state / "lossy-sourced.json", {}).items()
    ]
    with con:
        con.execute("DELETE FROM list_songs")
        con.execute("DELETE FROM lists")
        con.executemany("INSERT INTO lists VALUES (?, ?, ?, ?, ?, ?, ?)", lists)
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
        con.executemany("INSERT INTO attempts VALUES (?, ?, ?, ?)", attempts)
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
        con.executemany(
            f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
            rows,
        )
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


def known_files(root: Path) -> Known:
    """Duration and bitrate the pipeline already probed, keyed by library-relative path."""
    cache = _read_json(root / "state" / "library-cache.json", {})
    return {
        p.removeprefix(PIPELINE_TRACKS): (v[0], v[1], v[2], v[3])
        for p, v in cache.items()
        if p.startswith(PIPELINE_TRACKS) and len(v) >= 4
    }


@dataclass
class Activity:
    job: str
    label: str
    started: str | None  # local time of the last start
    finished: str | None  # local time of the last end (None while running or never)

    @property
    def running(self) -> bool:
        return bool(self.started) and (not self.finished or self.finished < self.started)


MARKER = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) === music-sync (\S+) (start|done)$", re.M)


def activity(root: Path) -> list[Activity]:
    """Last run of each pipeline job, from the start/done lines in its log. A run that crashed
    has no done line and shows as running until the job starts again."""
    out = []
    for job, label in JOBS.items():
        started = finished = None
        try:
            with (root / "logs" / f"{job}.log").open("rb") as f:
                f.seek(max(f.seek(0, os.SEEK_END) - 256_000, 0))
                tail = f.read().decode("utf-8", "replace")
        except OSError:
            tail = ""
        for ts, _, what in MARKER.findall(tail):
            if what == "start":
                started = ts.replace(" ", "T")
            else:
                finished = ts.replace(" ", "T")
        out.append(Activity(job, label, started, finished))
    return out


def paused(root: Path) -> bool:
    """Downloads paused with state/PAUSED (the pipeline's cron.sh skips its jobs)."""
    return (root / "state" / "PAUSED").exists()
