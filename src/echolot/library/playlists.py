"""The playlist files the music server reads, each user's in their folder (<music>/playlists/<user>/):
one .m3u per list they follow as a playlist, in list order, pointing at each song's best library copy,
with the list's cover beside it (<name>.jpg/png). A list two users follow as a playlist is two files: a
Navidrome playlist has one owner (sync_owners gives each file's playlist to its user). What songs left a
list, and why, the changes record (jobs/availability).

File names come from the list keys (sources.Source.name) and never change: the music server would take a
renamed file for a new playlist. Files Echolot wrote (meta playlist_files) whose list is gone or shown no
longer are deleted, and remembered (meta playlists_gone) until Navidrome's playlist of them is deleted
too; no other file or playlist is ever touched.
"""

import contextlib
import json
import logging
import re
import sqlite3
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from echolot import db
from echolot.library import catalog
from echolot.library.rules import clean_name
from echolot.settings import sources

if TYPE_CHECKING:
    from echolot.services import navidrome

log = logging.getLogger(__name__)
OURS = re.compile(
    r"^(?:spotify-[A-Za-z0-9]+|soundcloud-[a-z0-9-]+|Spotify Liked Songs|SoundCloud Likes)"
    r"(?: - removed)?\.(?:m3u|jpg|png)$"
)


def _write(path: Path, text: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return True


def _m3u(folder: Path, name: str, title: str, files: list[str]) -> bool:
    """Write <name>.m3u in a user's folder (paths relative to it, ../../tracks/..., as the music server
    expects)."""
    lines = ["#EXTM3U", f"#PLAYLIST:{title}"]  # the name a newly imported playlist gets
    seen: set[str] = set()
    for f in files:
        if f not in seen:
            seen.add(f)
            lines.append(f"../../tracks/{f}")
    return _write(folder / f"{name}.m3u", "\n".join(lines) + "\n")


def _cover(
    con: sqlite3.Connection, folder: Path, name: str, key: str, url: str | None, saved: str | None
) -> str | None:
    """The list's cover as <name>.jpg/png (fetched again only when its address changed)."""
    have = [e for e in ("jpg", "png") if (folder / f"{name}.{e}").exists()]
    if not url or (saved == url and have):
        return have[0] if have else None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
    except OSError as e:
        log.info("cover of %s not fetched: %s", name, e)
        return have[0] if have else None
    ext = "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
    tmp = folder / f".{name}.{ext}.tmp"
    tmp.write_bytes(data)
    tmp.replace(folder / f"{name}.{ext}")
    for other in {"jpg", "png"} - {ext}:
        (folder / f"{name}.{other}").unlink(missing_ok=True)
    with con:
        con.execute("UPDATE lists SET cover_file = ? WHERE key = ?", (url, key))
    return ext


def user_folders(con: sqlite3.Connection) -> dict[int, str]:
    """Each user's playlist folder, named after them (with their id if two names come out the same);
    not for an account gone from Navidrome (its playlists go)."""
    rows = con.execute("SELECT id, name FROM users WHERE NOT disabled ORDER BY id").fetchall()
    names = [clean_name(r["name"]) for r in rows]
    return {r["id"]: (n if names.count(n) == 1 else f"{n} ({r['id']})") for r, n in zip(rows, names, strict=True)}


def write(con: sqlite3.Connection, folder: Path) -> str:
    """Write every playlist that changed, each user's in their folder; delete the files of lists no
    longer shown."""
    folder.mkdir(parents=True, exist_ok=True)
    cat = catalog.Catalog.from_db(con)
    lists = {r["key"]: r for r in con.execute("SELECT * FROM lists")}
    folders = user_folders(con)
    keep: set[str] = set()  # relative to the folder: <user>/<name>.<ext>
    written = 0

    def best(r: sqlite3.Row) -> str | None:
        hits = cat.song(r["artist"], r["title"], r["length"], json.loads(r["artists"] or "[]"),
                        json.loads(r["link"] or "null"))  # fmt: skip
        e = hits[0] if hits else cat.by_stem.get(r["stem"] or "")
        return e.path if e else None

    for uid in sources.owners(con):
        if uid not in folders:
            continue  # the lists nobody owns yet, or an account gone from Navidrome
        mine = folder / folders[uid]
        for s in sources.user_lists(con, uid):
            row = lists.get(s.key)
            name = clean_name(s.name)
            if not row or not s.playlist:
                continue
            keep.update(f"{folders[uid]}/{name}.{e}" for e in ("m3u", "jpg", "png"))
            if not row["fetched"]:  # not listed yet: its old files stay
                continue
            mine.mkdir(exist_ok=True)
            songs = con.execute(
                "SELECT s.* FROM list_songs ls JOIN songs s ON s.key = ls.song_key WHERE ls.list_key = ? "
                "ORDER BY ls.position",
                (s.key,),
            ).fetchall()
            written += _m3u(mine, name, row["title"], [f for r in songs if (f := best(r))])
            _cover(con, mine, name, s.key, row["cover_url"], row["cover_file"])
    if not keep:  # no list to show: nothing is deleted (a missing configuration must not empty the folder)
        return f"{written} playlists written"
    ours = set(json.loads(db.get_meta(con, "playlist_files", "[]")))
    stale = [rel for rel in ours if rel not in keep and OURS.match(rel.rsplit("/", 1)[-1])]
    for rel in stale:
        (folder / rel).unlink(missing_ok=True)
        if (parent := (folder / rel).parent) != folder:
            with contextlib.suppress(OSError):  # a user's folder that is empty now goes too
                parent.rmdir()
    gone = set(json.loads(db.get_meta(con, "playlists_gone", "[]"))) | {r for r in stale if r.endswith(".m3u")}
    with con:
        db.set_meta(con, "playlist_files", json.dumps(sorted(n for n in keep if (folder / n).exists())))
        db.set_meta(con, "playlists_gone", json.dumps(sorted(gone)))
    return f"{written} playlists written" + (f", {len(stale)} old files removed" if stale else "")


def _legacy(path: str, folder: Path) -> str | None:
    """The name of a playlist file Echolot wrote at the top of its folder (before users had folders) that
    is gone: its playlist goes too."""
    _, sep, name = path.rpartition(f"/{folder.name}/")
    if not sep or "/" in name or not OURS.match(name) or (folder / name).exists():
        return None
    return name


SERVICES = {"spotify": "Spotify", "soundcloud": "SoundCloud", "youtube": "YouTube"}


def comment(row: sqlite3.Row, owner: str = "") -> str:
    """A playlist's comment in the music server: "Auto-imported from Spotify, by Timon: <the list's page>"
    (who made the list: Spotify's owner, YouTube's author, the SoundCloud account; Liked Songs: their user)."""
    creator = row["creator"]
    if not creator and row["service"] == "soundcloud":
        creator = (row["url"] or "").removeprefix("https://soundcloud.com/").split("/", 1)[0]
    if not creator and str(row["key"]).startswith("spotify:likes:"):
        creator = owner
    text = f"Auto-imported from {SERVICES.get(row['service'], row['service'])}"
    return text + (f", by {creator}" if creator else "") + (f": {row['url']}" if row["url"] else "")


def comments(con: sqlite3.Connection) -> dict[str, str]:
    """The comment of each playlist file Echolot writes (<user>/<name>.m3u, as write names them)."""
    lists = {r["key"]: r for r in con.execute("SELECT * FROM lists")}
    names = dict(con.execute("SELECT id, name FROM users").fetchall())
    folders, out = user_folders(con), {}
    for uid in sources.owners(con):
        if uid not in folders:
            continue
        for s in sources.user_lists(con, uid):
            if (row := lists.get(s.key)) is not None and s.playlist:
                out[f"{folders[uid]}/{clean_name(s.name)}.m3u"] = comment(row, names.get(uid, ""))
    return out


def sync_owners(con: sqlite3.Connection, svc: "navidrome.Service", folder: Path) -> str:
    """Navidrome's playlists of the files Echolot writes: each given to its user (the folder's) with its
    comment (where the list comes from), and those of files Echolot removed deleted. Only those: a playlist is Echolot's when its path is exactly one of
    its files; one made in Navidrome has no file and is never touched."""
    folders = user_folders(con)
    ids = dict(con.execute("SELECT id, navidrome_id FROM users WHERE navidrome_id IS NOT NULL").fetchall())
    owner_of = {name: ids[uid] for uid, name in folders.items() if uid in ids}
    current = {r for r in json.loads(db.get_meta(con, "playlist_files", "[]")) if r.endswith(".m3u")}
    gone = set(json.loads(db.get_meta(con, "playlists_gone", "[]")))
    given = deleted = commented = 0
    found: dict[str, list[dict]] = {}
    present = bool(current) and all((folder / r).exists() for r in current)  # the folder is there, as written
    for p in svc.playlists():
        path = p.get("path") or ""
        if rel := next((r for r in current | gone if path.endswith(f"/{folder.name}/{r}")), None):
            found.setdefault(rel, []).append(p)
        elif present and (old := _legacy(path, folder)):
            found.setdefault(old, []).append(p)  # from before users had folders (a "- removed" playlist)
            gone.add(old)
    notes = comments(con)
    for rel in current & set(found):
        owner = owner_of.get(rel.split("/", 1)[0]) if "/" in rel else None
        for p in found[rel]:
            fields = {"ownerId": owner} if owner and p.get("ownerId") != owner else {}
            if (note := notes.get(rel)) and p.get(
                "comment"
            ) != note:  # in place of Navidrome's "Auto-imported from '<file>'"
                fields["comment"] = note
            if fields:
                svc.update_playlist(p["id"], fields)
                given += "ownerId" in fields
                commented += "comment" in fields
    done = [f"{given} playlists given to their users"] if given else []
    done += [f"{commented} playlist comments set"] if commented else []
    if current - set(found):  # not all imported yet: the old playlists stay until the new ones are there
        return ", ".join(done)
    for rel in gone & set(found):
        if not (folder / rel).exists():
            for p in found[rel]:
                svc.delete_playlist(p["id"])
                deleted += 1
    with con:  # every playlist of a removed file is deleted now (Navidrome said which there are)
        db.set_meta(con, "playlists_gone", json.dumps(sorted(r for r in gone if (folder / r).exists())))
    return ", ".join(done + ([f"{deleted} old playlists deleted"] if deleted else []))
