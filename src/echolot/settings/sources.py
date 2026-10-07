"""The lists each user follows (sources table): Spotify playlists and likes, SoundCloud sets and likes,
public YouTube playlists.
Every change is checked with the same rules the configuration file import uses (check), per user. Each
list has a stable key and name (derive): its state and playlist file are named after it, so they never
change. A list two users follow is one list (fetched once: lists table) followed twice, each with their
own options; a user's Spotify likes are their own list (spotify:likes:<user id>), SoundCloud likes are a
SoundCloud account's (soundcloud:<name>/likes). Lists from before users had lists belong to nobody until
adopt gives them to the oldest admin.
"""

import dataclasses
import re
import sqlite3
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import yaml

from echolot.services.youtube import ACCOUNT_LISTS
from echolot.settings import options

SERVICES = ("spotify", "soundcloud", "youtube")
TOP_LEVEL = {*SERVICES, "removed_playlists"}  # removed_playlists: of old files, ignored
ENTRY_KEYS = {"url", "title", "playlist"}
LIKES = {"Spotify Liked Songs", "SoundCloud Likes"}  # the likes lists' names
WISHED = "discover"  # a user's hearts in a player on songs the library lacked (web/subsonic): Echolot's own
WISHED_TITLE = "Echolot · Wished"  # list, kept out of the configuration (as_config, replace_rows)


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()


@dataclass(frozen=True)
class Source:
    key: str  # the list key: spotify:likes:<user id>, spotify:playlist:<id>, soundcloud:<path>, youtube:playlist:<id>
    name: str  # its playlist file is named after this
    service: str
    url: str
    title: str | None  # name override
    playlist: bool  # also a playlist in the music server
    user_id: int | None = None  # who follows it (None: nobody yet, see adopt)


def _entries(value: Any) -> list[dict[str, Any]]:
    return [{"url": e} if isinstance(e, str) else dict(e) for e in value or []]


def _entry_options(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def spotify_likes_key(user_id: int | None) -> str:
    return f"spotify:likes:{user_id}" if user_id is not None else "spotify:likes"


def derive(config: dict[str, Any], user_id: int | None = None) -> list[Source]:
    """The lists of a user's sources.yml structure, in order, with their keys and names."""
    out = []
    spotify = config.get("spotify") or {}
    if likes := spotify.get("likes"):
        o = _entry_options(likes)
        out.append(
            Source(
                spotify_likes_key(user_id),
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
    for e in _entries((config.get("youtube") or {}).get("playlists")):
        pid = (urllib.parse.parse_qs(urllib.parse.urlparse(e["url"]).query).get("list") or [slug(e["url"])])[0]
        url, shown = f"https://www.youtube.com/playlist?list={pid}", e.get("playlist", True) is not False
        out.append(Source(f"youtube:playlist:{pid}", f"youtube-{pid}", "youtube", url, e.get("title"), shown))
    return [dataclasses.replace(x, user_id=user_id) for x in out]


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
    if parsed.hostname in YOUTUBE_HOSTS:
        pid = (urllib.parse.parse_qs(parsed.query).get("list") or [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,64}", pid):
            raise ConfigError("YouTube: use a playlist link (…/playlist?list=…, or a video link with &list=).")
        if pid in ACCOUNT_LISTS:
            raise ConfigError(f"YouTube: {ACCOUNT_LISTS[pid]} can only be read with your account (not supported).")
        if pid.startswith("RD"):
            raise ConfigError("YouTube: a Mix is made anew for each listener; follow a playlist instead.")
        return "youtube", f"https://www.youtube.com/playlist?list={pid}"
    raise ConfigError("Not a Spotify playlist, SoundCloud set/likes or YouTube playlist URL.")


YOUTUBE_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be")


# ---------------------------------------------------------------- checks


def check(data: Any) -> list[Source]:
    """The lists of a parsed sources.yml, or ConfigError describing every problem."""
    problems = []
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("The file must be a mapping (spotify:, soundcloud:, youtube:).")
    problems += [f"Unknown setting '{k}'." for k in data if k not in TOP_LEVEL]
    if not isinstance(data.get("removed_playlists", True), bool):
        problems.append("removed_playlists must be true or false.")
    for service in SERVICES:
        section = data.get(service)
        if section is None:
            continue
        if not isinstance(section, dict):
            problems.append(f"{service}: must be a mapping.")
            continue
        allowed = {"playlists"} | ({"likes"} if service != "youtube" else set())
        allowed |= {"user"} if service == "soundcloud" else set()
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


def _rows(con: sqlite3.Connection, user_id: int | None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM sources WHERE user_id IS ? AND service != ? ORDER BY position, key"
    return con.execute(sql, (user_id, WISHED)).fetchall()


def wished_key(user_id: int) -> str:
    return f"discover:wished:{user_id}"


def wished(con: sqlite3.Connection, user_id: int | None) -> list[Source]:
    """The user's Wished list once they hearted a song in a player (add_wished)."""
    if (
        user_id is None
        or not con.execute("SELECT 1 FROM sources WHERE user_id = ? AND service = ?", (user_id, WISHED)).fetchone()
    ):
        return []
    return [Source(wished_key(user_id), "echolot-wished", WISHED, "/discover", WISHED_TITLE, True, user_id)]


def add_wished(con: sqlite3.Connection, user_id: int) -> Source:
    """The user's Wished list, made at their first heart (no commit)."""
    con.execute(
        "INSERT OR IGNORE INTO sources (user_id, key, service, likes, url, title, playlist, enabled, position, "
        "added) VALUES (?, ?, ?, 1, '/discover', NULL, 1, 1, 1000000, ?)",
        (user_id, wished_key(user_id), WISHED, _now()),
    )
    return wished(con, user_id)[0]


def soundcloud_user(con: sqlite3.Connection, user_id: int | None) -> str:
    """Whose likes a user's SoundCloud likes are (nobody's lists: the old global setting)."""
    if user_id is None:
        return options.get(con, options.SourceOptions).soundcloud_user
    row = con.execute("SELECT soundcloud_user FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["soundcloud_user"] if row else ""


def as_config(con: sqlite3.Connection, user_id: int | None) -> dict[str, Any]:
    """A user's lists in the structure of sources.yml."""
    sections: dict[str, dict[str, Any]] = {service: {} for service in SERVICES}
    if sc_user := soundcloud_user(con, user_id):
        sections["soundcloud"]["user"] = sc_user
    rows = _rows(con, user_id)
    for r in rows:
        if r["likes"]:
            sections[r["service"]]["likes"] = (_options(r) or True) if r["enabled"] else False
    for r in rows:
        if not r["likes"]:
            o = _options(r)
            entry = {"url": r["url"], **o} if o else r["url"]
            sections[r["service"]].setdefault("playlists", []).append(entry)
    return {name: s for name, s in sections.items() if s}


class _Dumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)  # list items indented under their key


def dump(data: Any) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=4096)


def owners(con: sqlite3.Connection) -> list[int | None]:
    """The users who follow lists, the oldest first (None: the lists nobody owns yet, last)."""
    return [r[0] for r in con.execute("SELECT DISTINCT user_id FROM sources ORDER BY user_id IS NULL, user_id")]


def user_lists(con: sqlite3.Connection, user_id: int | None) -> list[Source]:
    """A user's lists in order, with their keys and names (None: the lists nobody owns yet)."""
    return derive(as_config(con, user_id), user_id) + wished(con, user_id)


def lists(con: sqlite3.Connection) -> list[Source]:
    """Every user's lists (the oldest user's first): a list two users follow is in it twice."""
    return [s for uid in owners(con) for s in user_lists(con, uid)]


def followed(con: sqlite3.Connection) -> list[Source]:
    """Every list once (as its oldest follower has it), in order: what is fetched and stored in lists."""
    seen: set[str] = set()
    return [s for s in lists(con) if not (s.key in seen or seen.add(s.key))]


def followers(con: sqlite3.Connection, key: str) -> list[int]:
    """Who follows a list, the oldest user first."""
    sql = "SELECT user_id FROM sources WHERE key = ? AND user_id IS NOT NULL AND enabled ORDER BY user_id"
    return [r[0] for r in con.execute(sql, (key,))]


# ---------------------------------------------------------------- changes


def _change(con: sqlite3.Connection, user_id: int | None, change: Callable[[], None]) -> None:
    """Apply `change` in one transaction if the user's lists are valid then."""
    try:
        with con:
            change()
            check(as_config(con, user_id))
    except sqlite3.IntegrityError as err:
        raise ConfigError("That list is in your sources already.") from err


def _next_position(con: sqlite3.Connection, user_id: int | None) -> int:
    sql = "SELECT coalesce(max(position), -1) + 1 FROM sources WHERE user_id IS ?"
    return con.execute(sql, (user_id,)).fetchone()[0]


def _key(service: str, url: str) -> str:
    return derive({service: {"playlists": [url]}})[0].key


def _likes_key(service: str, user_id: int | None, sc_user: str) -> tuple[str, str]:
    if service == "spotify":
        return spotify_likes_key(user_id), "https://open.spotify.com/collection/tracks"
    return f"soundcloud:{sc_user}/likes", f"https://soundcloud.com/{sc_user}/likes"


def _insert(con: sqlite3.Connection, user_id: int | None, row: tuple, position: int) -> None:
    """row: key, service, likes, url, title, playlist, enabled."""
    con.execute(
        "INSERT INTO sources (user_id, key, service, likes, url, title, playlist, enabled, position, added) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, *row, position, _now()),
    )


def replace_rows(con: sqlite3.Connection, data: dict[str, Any], user_id: int | None) -> None:
    """Replace a user's lists with those of a checked sources.yml structure (no commit), and their
    SoundCloud user."""
    rows = []
    for service in SERVICES:
        section = data.get(service) or {}
        likes = section.get("likes")
        sc_user = str(section.get("user") or "").strip().strip("/")
        if likes is not None and (service == "spotify" or sc_user):
            key, url = _likes_key(service, user_id, sc_user)
            o = likes if isinstance(likes, dict) else {}
            playlist = int(o.get("playlist", True) is not False)
            rows.append((key, service, 1, url, o.get("title") or None, playlist, int(bool(likes))))
        for e in section.get("playlists") or []:
            e = {"url": e} if isinstance(e, str) else e
            _, canonical = parse_url(e["url"])
            playlist = int(e.get("playlist", True) is not False)
            rows.append((_key(service, canonical), service, 0, canonical, e.get("title") or None, playlist, 1))
    con.execute("DELETE FROM sources WHERE user_id IS ? AND service != ?", (user_id, WISHED))
    for n, row in enumerate(rows):
        _insert(con, user_id, row, n)
    sc = str((data.get("soundcloud") or {}).get("user") or "").strip().strip("/")
    if user_id is not None:
        con.execute("UPDATE users SET soundcloud_user = ? WHERE id = ?", (sc, user_id))
    else:
        options.update(con, options.SourceOptions, soundcloud_user=sc)


def replace(con: sqlite3.Connection, data: Any, user_id: int | None) -> None:
    """Replace a user's lists with those of a checked sources.yml structure."""
    check(data)
    _change(con, user_id, lambda: replace_rows(con, data or {}, user_id))


def add_list(con: sqlite3.Connection, user_id: int, url: str, title: str = "", playlist: bool = True) -> str:
    service, canonical = parse_url(url)
    key = _key(service, canonical)
    if con.execute("SELECT 1 FROM sources WHERE user_id = ? AND key = ?", (user_id, key)).fetchone():
        raise ConfigError("You follow that list already.")
    row = (key, service, 0, canonical, title.strip() or None, int(playlist), 1)
    _change(con, user_id, lambda: _insert(con, user_id, row, _next_position(con, user_id)))
    return key


def set_playlist(con: sqlite3.Connection, user_id: int, key: str, playlist: bool) -> None:
    """Also show the list as a playlist in the music server, or not."""
    if not con.execute("SELECT 1 FROM sources WHERE user_id = ? AND key = ?", (user_id, key)).fetchone():
        raise ConfigError("You don't follow that list (any more).")
    sql = "UPDATE sources SET playlist = ? WHERE user_id = ? AND key = ?"
    _change(con, user_id, lambda: con.execute(sql, (int(playlist), user_id, key)))


def remove_list(con: sqlite3.Connection, user_id: int, key: str) -> None:
    sql = "SELECT 1 FROM sources WHERE user_id = ? AND key = ? AND likes = 0"
    if not con.execute(sql, (user_id, key)).fetchone():
        raise ConfigError("You don't follow that list (any more).")
    _change(con, user_id, lambda: con.execute("DELETE FROM sources WHERE user_id = ? AND key = ?", (user_id, key)))


def set_likes(con: sqlite3.Connection, user_id: int, service: str, enabled: bool, user: str | None = None) -> None:
    """Switch a user's likes on or off; SoundCloud: `user` whose likes (kept when empty)."""
    if service == "soundcloud" and user is not None:
        user = user.strip().strip("/")
        if enabled and not user:
            raise ConfigError("SoundCloud likes need your SoundCloud user name.")
    name = (user or soundcloud_user(con, user_id)) if service == "soundcloud" else ""
    if service == "soundcloud" and enabled and not name:
        raise ConfigError("SoundCloud likes need your SoundCloud user name.")

    def change() -> None:
        if service == "soundcloud" and user:
            con.execute("UPDATE users SET soundcloud_user = ? WHERE id = ?", (user, user_id))
        sql = "SELECT key FROM sources WHERE user_id = ? AND service = ? AND likes = 1"
        row = con.execute(sql, (user_id, service)).fetchone()
        key, url = _likes_key(service, user_id, name)
        if row is None:
            if enabled:
                _insert(con, user_id, (key, service, 1, url, None, 1, 1), _next_position(con, user_id))
        else:
            new = (key, url) if name or service == "spotify" else (row["key"], None)
            con.execute(
                "UPDATE sources SET key = ?, url = coalesce(?, url), enabled = ? WHERE user_id = ? AND key = ?",
                (new[0], new[1], int(enabled), user_id, row["key"]),
            )

    _change(con, user_id, change)


# ---------------------------------------------------------------- the lists of before users had lists


def adopt(con: sqlite3.Connection) -> str | None:
    """Give the lists nobody owns (from before users had lists) to the oldest admin, with what was the
    one account's: the Spotify likes become theirs (key spotify:likes -> spotify:likes:<id>, in every table
    that has list keys), and so do the Spotify and SoundCloud logins and the SoundCloud user. One
    transaction; nothing to do (no orphans, no admin yet) changes nothing. Returns what it did."""
    owner = con.execute(
        "SELECT id, name FROM users WHERE (admin OR navidrome_admin) AND NOT disabled ORDER BY id LIMIT 1"
    ).fetchone()
    orphans = con.execute("SELECT count(*) FROM sources WHERE user_id IS NULL").fetchone()[0]
    plain = [r[0] for r in con.execute("SELECT name FROM secrets WHERE name IN (?, ?)", SINGLE_ACCOUNT)]
    if owner is None or not (orphans or plain):
        return None
    uid, old, new = owner["id"], "spotify:likes", spotify_likes_key(owner["id"])
    with con:
        sc_user = options.get(con, options.SourceOptions).soundcloud_user
        con.execute("UPDATE users SET soundcloud_user = ? WHERE id = ? AND soundcloud_user = ''", (sc_user, uid))
        con.execute("UPDATE sources SET user_id = ? WHERE user_id IS NULL", (uid,))
        con.execute("UPDATE sources SET key = ? WHERE key = ? AND user_id = ?", (new, old, uid))
        _rename_list(con, old, new)
        for name in plain:  # unless they connected their own already
            taken = con.execute("SELECT 1 FROM secrets WHERE name = ?", (f"{name}:{uid}",)).fetchone()
            if not taken:
                con.execute("UPDATE secrets SET name = ? WHERE name = ?", (f"{name}:{uid}", name))
    return f"{orphans} lists and the accounts given to {owner['name']}"


SINGLE_ACCOUNT = ("spotify.refresh_token", "soundcloud.token")  # the one account's logins, before users had lists


def _rename_list(con: sqlite3.Connection, old: str, new: str) -> None:
    """A list's key, everywhere (no commit): its state, songs, history and snapshots."""
    if not con.execute("SELECT 1 FROM lists WHERE key = ?", (old,)).fetchone():
        return
    columns = "service, title, url, position, playlist, fetched, cover_url, cover_file, snapshot, fetched_at"
    con.execute(f"INSERT INTO lists (key, {columns}) SELECT ?, {columns} FROM lists WHERE key = ?", (new, old))
    con.execute("UPDATE list_songs SET list_key = ? WHERE list_key = ?", (new, old))
    con.execute("UPDATE list_history SET list_key = ? WHERE list_key = ?", (new, old))
    con.execute("UPDATE snapshots SET key = ? WHERE key = ? AND metric LIKE 'list_%'", (new, old))
    con.execute("DELETE FROM lists WHERE key = ?", (old,))
