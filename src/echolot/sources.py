"""Edit the pipeline's sources.yml: structured changes (add, change, remove a list; likes; options)
and whole-file edits. Comments and layout of the file are kept (ruamel.yaml round trip).

Every save is checked first, refused if the file changed on disk since it was read, stored in the
config_versions table (for undo) and written atomically (temp file + rename), so the pipeline never
reads half a file.
"""

import contextlib
import hashlib
import io
import os
import re
import sqlite3
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import CommentMark
from ruamel.yaml.tokens import CommentToken

from echolot import pipeline

FILE = "sources.yml"
TOP_LEVEL = {"spotify", "soundcloud", "removed_playlists"}
ENTRY_KEYS = {"url", "title", "playlist"}


class ConfigError(ValueError):
    """The new content is not a valid configuration (message for the user)."""


class Conflict(ConfigError):
    """The file changed on disk since it was read."""


def version(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def read(root: Path) -> str:
    return (root / FILE).read_text(encoding="utf-8")


# ---------------------------------------------------------------- URLs


def parse_url(url: str) -> tuple[str, str]:
    """(service, canonical URL) of a list URL, or ConfigError."""
    url = url.strip()
    if m := re.search(
        r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?playlist/|spotify:playlist:)(\w+)", url
    ):
        return "spotify", f"https://open.spotify.com/playlist/{m.group(1)}"
    if re.search(r"open\.spotify\.com/collection/tracks", url):
        raise ConfigError("That is your Liked Songs: switch on Spotify likes instead.")
    parsed = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    if parsed.hostname in ("soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"):
        parts = [p for p in parsed.path.split("/") if p]
        if (len(parts) >= 3 and parts[1] == "sets") or (len(parts) == 2 and parts[1] == "likes"):
            return "soundcloud", "https://soundcloud.com/" + "/".join(parts[:3])
        raise ConfigError(
            "SoundCloud: use a set (…/<user>/sets/<name>) or a likes page (…/<user>/likes)."
        )
    if parsed.hostname == "on.soundcloud.com":
        raise ConfigError(
            "Short on.soundcloud.com links can't be resolved: open it and copy the full URL."
        )
    raise ConfigError("Not a Spotify playlist or SoundCloud set/likes URL.")


# ---------------------------------------------------------------- checks


def check(data: Any) -> list[pipeline.Source]:
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
            problems += [
                f"{service}.likes: unknown option '{k}'."
                for k in likes
                if k not in ("title", "playlist")
            ]
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
    lists = pipeline.sources(data)
    keys = [s.key for s in lists]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise ConfigError("Listed twice: " + ", ".join(dupes))
    return lists


# ---------------------------------------------------------------- saving


def save(con: sqlite3.Connection, root: Path, text: str, expected: str, note: str) -> None:
    """Check `text`, keep the current file as a version, then replace it atomically."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise ConfigError(f"Not valid YAML: {err}") from err
    check(data)
    path = root / FILE
    current = path.read_text(encoding="utf-8")
    if version(current) != expected:
        raise Conflict("sources.yml was changed elsewhere in the meantime. Reload and try again.")
    if text == current:
        return
    with con:
        con.execute(
            "INSERT INTO config_versions (name, ts, text, note) VALUES (?, ?, ?, ?)",
            (FILE, datetime.now().isoformat(timespec="seconds"), current, note),
        )
    write_atomic(path, text)


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.echolot-tmp")
    tmp.write_text(text, encoding="utf-8")
    with contextlib.suppress(OSError):  # keep the file's permissions
        os.chmod(tmp, path.stat().st_mode & 0o777)
    tmp.replace(path)


def versions(con: sqlite3.Connection, name: str = FILE, limit: int = 20) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT id, ts, note, length(text) AS size FROM config_versions WHERE name = ? "
        "ORDER BY id DESC LIMIT ?",
        (name, limit),
    ).fetchall()


def old_version(con: sqlite3.Connection, vid: int) -> str | None:
    row = con.execute("SELECT text FROM config_versions WHERE id = ?", (vid,)).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- structured edits


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _load(text: str) -> CommentedMap:
    data = _yaml().load(text) or CommentedMap()
    if not isinstance(data, CommentedMap):
        raise ConfigError("The file must be a mapping.")
    return data


def _dump(data: CommentedMap) -> str:
    out = io.StringIO()
    _yaml().dump(data, out)
    return out.getvalue()


def _section(data: CommentedMap, service: str) -> CommentedMap:
    if not isinstance(data.get(service), CommentedMap):
        data[service] = CommentedMap()
    return data[service]


def _seq(data: CommentedMap, service: str) -> CommentedSeq:
    section = _section(data, service)
    if not isinstance(section.get("playlists"), CommentedSeq):
        section["playlists"] = CommentedSeq()
    return section["playlists"]


def _split_trailing(seq: CommentedSeq, index: int) -> str:
    """Cut what follows the end-of-line comment of item `index` (blank lines, comments of what comes
    next) off that item and return it, so it can move to the item that becomes last."""
    ca = seq.ca.items.get(index)
    token = ca[0] if ca else None
    if token is None or token.value.count("\n") < 2:
        return ""
    head, _, tail = token.value.partition("\n")
    token.value = head + "\n"
    return tail


def _attach_trailing(seq: CommentedSeq, index: int, tail: str) -> None:
    if not tail:
        return
    ca = seq.ca.items.get(index)
    if ca and ca[0] is not None:
        ca[0].value = ca[0].value.rstrip("\n") + "\n" + tail
    else:
        seq.ca.items[index] = [CommentToken("\n" + tail, CommentMark(0), None), None, None, None]


def _find(data: CommentedMap, key: str) -> tuple[CommentedSeq, int]:
    for service in ("spotify", "soundcloud"):
        seq = (data.get(service) or {}).get("playlists") or []
        for i, e in enumerate(seq):
            url = e.get("url") if isinstance(e, dict) else e
            if isinstance(url, str) and _key(service, url) == key:
                return seq, i
    raise ConfigError("That list is not in sources.yml (any more).")


def _key(service: str, url: str) -> str:
    return pipeline.sources({service: {"playlists": [url]}})[0].key


def add_list(text: str, url: str, title: str = "", playlist: bool = True) -> str:
    service, canonical = parse_url(url)
    data = _load(text)
    key = _key(service, canonical)
    if any(s.key == key for s in pipeline.sources(yaml.safe_load(text) or {})):
        raise ConfigError("That list is already in sources.yml.")
    seq = _seq(data, service)
    entry: Any = canonical
    if title.strip() or not playlist:
        entry = CommentedMap(url=canonical)
        if title.strip():
            entry["title"] = title.strip()
        if not playlist:
            entry["playlist"] = False
    tail = _split_trailing(seq, len(seq) - 1) if seq else ""
    seq.append(entry)
    _attach_trailing(seq, len(seq) - 1, tail)
    return _dump(data)


def update_list(text: str, key: str, title: str, playlist: bool) -> str:
    data = _load(text)
    seq, i = _find(data, key)
    old = seq[i]
    url = old.get("url") if isinstance(old, dict) else old
    if title.strip() or not playlist:
        entry = CommentedMap(url=url)
        if title.strip():
            entry["title"] = title.strip()
        if not playlist:
            entry["playlist"] = False
    else:
        entry = url  # no options left: back to a plain URL
    seq[i] = entry
    return _dump(data)


def remove_list(text: str, key: str) -> str:
    data = _load(text)
    seq, i = _find(data, key)
    last = i == len(seq) - 1
    tail = _split_trailing(seq, i) if last else ""
    del seq[i]
    if last and seq:
        _attach_trailing(seq, len(seq) - 1, tail)
    return _dump(data)


def set_likes(text: str, service: str, enabled: bool, user: str | None = None) -> str:
    data = _load(text)
    section = _section(data, service)
    likes = section.get("likes")
    if enabled:
        if not isinstance(likes, dict):
            section["likes"] = True
    else:
        section["likes"] = False
    if service == "soundcloud" and user is not None:
        user = user.strip().strip("/")
        if enabled and not user:
            raise ConfigError("SoundCloud likes need your SoundCloud user name.")
        if user:
            section["user"] = user
    return _dump(data)


def set_removed_playlists(text: str, enabled: bool) -> str:
    data = _load(text)
    data["removed_playlists"] = enabled
    return _dump(data)


def likes_state(text: str) -> dict[str, Any]:
    """Current likes settings for the form."""
    data = yaml.safe_load(text) or {}
    sp, sc = data.get("spotify") or {}, data.get("soundcloud") or {}
    return {
        "spotify": bool(sp.get("likes")),
        "soundcloud": bool(sc.get("likes")),
        "soundcloud_user": sc.get("user") or "",
        "removed_playlists": data.get("removed_playlists", True) is not False,
    }


def entries(text: str) -> list[dict[str, Any]]:
    """The playlist entries of the file (not the likes): key, service, url, title, playlist flag."""
    return [
        {"key": s.key, "service": s.service, "url": s.url, "title": s.title, "playlist": s.playlist}
        for s in pipeline.sources(yaml.safe_load(text) or {})
        if s.name not in pipeline.LIKES
    ]
