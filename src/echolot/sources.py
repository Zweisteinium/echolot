"""The lists the library follows (sources table): Spotify playlists and likes, SoundCloud sets and likes.
Every change is checked with the same rules the configuration file import uses (check). Each list has a
stable key and name (derive): its state and playlist file are named after it, so they never change.
"""

import re
import sqlite3
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import yaml

from echolot import options

TOP_LEVEL = {"spotify", "soundcloud", "removed_playlists"}
ENTRY_KEYS = {"url", "title", "playlist"}
LIKES = {"Spotify Liked Songs", "SoundCloud Likes"}  # the likes lists' names


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()


@dataclass(frozen=True)
class Source:
    key: str  # the list key: spotify:likes, spotify:playlist:<id>, soundcloud:<path>
    name: str  # its playlist file is named after this (the pipeline's list name)
    service: str
    url: str
    title: str | None  # name override
    playlist: bool  # also a playlist in the music server


def _entries(value: Any) -> list[dict[str, Any]]:
    return [{"url": e} if isinstance(e, str) else dict(e) for e in value or []]


def _entry_options(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def derive(config: dict[str, Any]) -> list[Source]:
    """The lists of a sources.yml structure, in order, with their keys and names."""
    out = []
    spotify = config.get("spotify") or {}
    if likes := spotify.get("likes"):
        o = _entry_options(likes)
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
        o = _entry_options(likes)
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


class ConfigError(ValueError):
    """The new content is not a valid configuration (message for the user)."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- URLs


def parse_url(url: str) -> tuple[str, str]:
    """(service, canonical URL) of a list URL, or ConfigError."""
    url = url.strip()
    if m := re.search(r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?playlist/|spotify:playlist:)(\w+)", url):
        return "spotify", f"https://open.spotify.com/playlist/{m.group(1)}"
    if re.search(r"open\.spotify\.com/collection/tracks", url):
        raise ConfigError("That is your Liked Songs: switch on Spotify likes instead.")
    parsed = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    if parsed.hostname in ("soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"):
        parts = [p for p in parsed.path.split("/") if p]
        if (len(parts) >= 3 and parts[1] == "sets") or (len(parts) == 2 and parts[1] == "likes"):
            return "soundcloud", "https://soundcloud.com/" + "/".join(parts[:3])
        raise ConfigError("SoundCloud: use a set (…/<user>/sets/<name>) or a likes page (…/<user>/likes).")
    if parsed.hostname == "on.soundcloud.com":
        raise ConfigError("Short on.soundcloud.com links can't be resolved: open it and copy the full URL.")
    raise ConfigError("Not a Spotify playlist or SoundCloud set/likes URL.")


# ---------------------------------------------------------------- checks


def check(data: Any) -> list[Source]:
    """The lists of a parsed sources.yml, or ConfigError describing every problem."""
    problems = []
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("The file must be a mapping (spotify:, soundcloud:, removed_playlists:).")
    problems += [f"Unknown setting '{k}'." for k in data if k not in TOP_LEVEL]
    if not isinstance(data.get("removed_playlists", True), bool):
        problems.append("removed_playlists must be true or false.")
    for service in ("spotify", "soundcloud"):
        section = data.get(service)
        if section is None:
            continue
        if not isinstance(section, dict):
            problems.append(f"{service}: must be a mapping.")
            continue
        allowed = {"likes", "playlists"} | ({"user"} if service == "soundcloud" else set())
        problems += [f"{service}: unknown setting '{k}'." for k in section if k not in allowed]
        likes = section.get("likes")
        if likes is not None and not isinstance(likes, bool | dict):
            problems.append(f"{service}.likes must be true, false or {{title, playlist}}.")
        if isinstance(likes, dict):
            problems += [f"{service}.likes: unknown option '{k}'." for k in likes if k not in ("title", "playlist")]
        if service == "soundcloud" and likes and not section.get("user"):
            problems.append("soundcloud.likes needs soundcloud.user (whose likes).")
        entries = section.get("playlists") or []
        if not isinstance(entries, list):
            problems.append(f"{service}.playlists must be a list.")
            continue
        for n, e in enumerate(entries, 1):
            where = f"{service}.playlists #{n}"
            if isinstance(e, dict):
                problems += [f"{where}: unknown option '{k}'." for k in e if k not in ENTRY_KEYS]
                url = e.get("url")
                if "playlist" in e and not isinstance(e["playlist"], bool):
                    problems.append(f"{where}: playlist must be true or false.")
                if "title" in e and not isinstance(e["title"], str):
                    problems.append(f"{where}: title must be text.")
            else:
                url = e
            if not isinstance(url, str) or not url:
                problems.append(f"{where}: needs a URL.")
                continue
            try:
                kind, _ = parse_url(url)
                if kind != service:
                    problems.append(f"{where}: {url} is not a {service} URL.")
            except ConfigError as err:
                problems.append(f"{where}: {err}")
    if problems:
        raise ConfigError(" ".join(problems))
    lists = derive(data)
    keys = [s.key for s in lists]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise ConfigError("Listed twice: " + ", ".join(dupes))
    return lists


def parse(text: str) -> dict[str, Any]:
    """A sources.yml text, parsed and checked."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise ConfigError(f"Not valid YAML: {err}") from err
    check(data)
    return data or {}


# ---------------------------------------------------------------- the table as sources.yml


def _options(r: sqlite3.Row) -> dict[str, Any]:
    o: dict[str, Any] = {}
    if r["title"]:
        o["title"] = r["title"]
    if not r["playlist"]:
        o["playlist"] = False
    return o


def as_config(con: sqlite3.Connection) -> dict[str, Any]:
    """The lists in the structure of sources.yml."""
    opts = options.get(con, options.SourceOptions)
    sections: dict[str, dict[str, Any]] = {"spotify": {}, "soundcloud": {}}
    if opts.soundcloud_user:
        sections["soundcloud"]["user"] = opts.soundcloud_user
    rows = con.execute("SELECT * FROM sources ORDER BY position, key").fetchall()
    for r in rows:
        if r["likes"]:
            sections[r["service"]]["likes"] = (_options(r) or True) if r["enabled"] else False
    for r in rows:
        if not r["likes"]:
            o = _options(r)
            entry = {"url": r["url"], **o} if o else r["url"]
            sections[r["service"]].setdefault("playlists", []).append(entry)
    config: dict[str, Any] = {name: s for name, s in sections.items() if s}
    config["removed_playlists"] = opts.removed_playlists
    return config


class _Dumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)  # list items indented under their key


def dump(data: Any) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=4096)


def lists(con: sqlite3.Connection) -> list[Source]:
    """The lists in order, with the keys and names the pipeline uses."""
    return derive(as_config(con))


def likes_state(con: sqlite3.Connection) -> dict[str, Any]:
    """Current likes settings for the form."""
    opts = options.get(con, options.SourceOptions)
    on = {r["service"]: bool(r["enabled"]) for r in con.execute("SELECT service, enabled FROM sources WHERE likes = 1")}
    return {
        "spotify": on.get("spotify", False),
        "soundcloud": on.get("soundcloud", False),
        "soundcloud_user": opts.soundcloud_user,
        "removed_playlists": opts.removed_playlists,
    }


def entries(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """The playlist entries (not the likes): key, service, url, title, playlist flag."""
    return [
        {"key": r["key"], "service": r["service"], "url": r["url"], "title": r["title"],
         "playlist": bool(r["playlist"])}
        for r in con.execute("SELECT * FROM sources WHERE likes = 0 ORDER BY position, key")
    ]  # fmt: skip


# ---------------------------------------------------------------- changes


def _change(con: sqlite3.Connection, change: Callable[[], None]) -> None:
    """Apply `change` in one transaction if the result is valid."""
    try:
        with con:
            change()
            check(as_config(con))
    except sqlite3.IntegrityError as err:
        raise ConfigError("That list is in the sources already.") from err


def _next_position(con: sqlite3.Connection) -> int:
    return con.execute("SELECT coalesce(max(position), -1) + 1 FROM sources").fetchone()[0]


def _key(service: str, url: str) -> str:
    return derive({service: {"playlists": [url]}})[0].key


def _likes_key(service: str, user: str) -> tuple[str, str]:
    if service == "spotify":
        return "spotify:likes", "https://open.spotify.com/collection/tracks"
    return f"soundcloud:{user}/likes", f"https://soundcloud.com/{user}/likes"


def replace_rows(con: sqlite3.Connection, data: dict[str, Any]) -> None:
    """Replace every list with those of a checked sources.yml structure (no commit)."""
    rows = []
    for service in ("spotify", "soundcloud"):
        section = data.get(service) or {}
        likes = section.get("likes")
        user = str(section.get("user") or "").strip().strip("/")
        if likes is not None and (service == "spotify" or user):
            key, url = _likes_key(service, user)
            o = likes if isinstance(likes, dict) else {}
            rows.append((key, service, 1, url, o.get("title") or None,
                         int(o.get("playlist", True) is not False), int(bool(likes))))  # fmt: skip
        for e in section.get("playlists") or []:
            e = {"url": e} if isinstance(e, str) else e
            _, canonical = parse_url(e["url"])
            rows.append((_key(service, canonical), service, 0, canonical, e.get("title") or None,
                         int(e.get("playlist", True) is not False), 1))  # fmt: skip
    con.execute("DELETE FROM sources")
    now = _now()
    con.executemany(
        "INSERT INTO sources (key, service, likes, url, title, playlist, enabled, position, added) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(*r, n, now) for n, r in enumerate(rows)],
    )
    sc = data.get("soundcloud") or {}
    opts = options.SourceOptions(
        soundcloud_user=str(sc.get("user") or "").strip().strip("/"),
        removed_playlists=data.get("removed_playlists", True) is not False,
    )
    options.put(con, opts)


def replace(con: sqlite3.Connection, data: Any) -> None:
    """Replace every list with those of a checked sources.yml structure."""
    check(data)
    _change(con, lambda: replace_rows(con, data or {}))


def add_list(con: sqlite3.Connection, url: str, title: str = "", playlist: bool = True) -> str:
    service, canonical = parse_url(url)
    key = _key(service, canonical)
    if con.execute("SELECT 1 FROM sources WHERE key = ?", (key,)).fetchone():
        raise ConfigError("That list is already in the sources.")
    _change(con, lambda: con.execute(
        "INSERT INTO sources (key, service, likes, url, title, playlist, enabled, position, added) "
        "VALUES (?, ?, 0, ?, ?, ?, 1, ?, ?)",
        (key, service, canonical, title.strip() or None, int(playlist), _next_position(con), _now()),
    ))  # fmt: skip
    return key


def _playlist(con: sqlite3.Connection, key: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM sources WHERE key = ? AND likes = 0", (key,)).fetchone()
    if row is None:
        raise ConfigError("That list is not in the sources (any more).")
    return row


def set_playlist(con: sqlite3.Connection, key: str, playlist: bool) -> None:
    """Also show the list as a playlist in the music server, or not."""
    if not con.execute("SELECT 1 FROM sources WHERE key = ?", (key,)).fetchone():
        raise ConfigError("That list is not followed (any more).")
    _change(con, lambda: con.execute("UPDATE sources SET playlist = ? WHERE key = ?", (int(playlist), key)))


def remove_list(con: sqlite3.Connection, key: str) -> None:
    _playlist(con, key)
    _change(con, lambda: con.execute("DELETE FROM sources WHERE key = ?", (key,)))


def set_likes(con: sqlite3.Connection, service: str, enabled: bool, user: str | None = None) -> None:
    """Switch the account's likes on or off; SoundCloud: `user` whose likes (kept when empty)."""
    opts = options.get(con, options.SourceOptions)
    if service == "soundcloud" and user is not None:
        user = user.strip().strip("/")
        if enabled and not user:
            raise ConfigError("SoundCloud likes need your SoundCloud user name.")
    name = (user or opts.soundcloud_user) if service == "soundcloud" else ""
    if service == "soundcloud" and enabled and not name:
        raise ConfigError("SoundCloud likes need your SoundCloud user name.")

    def change() -> None:
        if service == "soundcloud" and user:
            options.update(con, options.SourceOptions, soundcloud_user=user)
        row = con.execute("SELECT key FROM sources WHERE service = ? AND likes = 1", (service,)).fetchone()
        key, url = _likes_key(service, name)
        if row is None:
            if enabled:
                con.execute(
                    "INSERT INTO sources (key, service, likes, url, title, playlist, enabled, "
                    "position, added) VALUES (?, ?, 1, ?, NULL, 1, 1, ?, ?)",
                    (key, service, url, _next_position(con), _now()),
                )
        else:
            new = (key, url) if name else (row["key"], None)
            con.execute(
                "UPDATE sources SET key = ?, url = coalesce(?, url), enabled = ? WHERE key = ?",
                (new[0], new[1], int(enabled), row["key"]),
            )

    _change(con, change)


def set_removed_playlists(con: sqlite3.Connection, enabled: bool) -> None:
    _change(con, lambda: options.update(con, options.SourceOptions, removed_playlists=enabled))


# ---------------------------------------------------------------- versions


# ---------------------------------------------------------------- files
