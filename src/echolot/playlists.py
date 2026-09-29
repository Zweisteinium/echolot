"""The playlist files the music server reads (<music>/playlists): one .m3u per followed list in list
order, pointing at each song's best library copy, with the list's cover beside it (<name>.jpg/png) and,
if songs have left the list, a "<list> – removed" playlist of those still in the library.

File names come from the list keys (sources.Source.name) and never change: the music server would take a
renamed file for a new playlist. Files Echolot wrote (meta playlist_files) whose list is gone or shown no
longer are deleted; no other file is ever touched.
"""

import json
import logging
import re
import sqlite3
import urllib.request
from pathlib import Path

from echolot import db, library, options, sources
from echolot.rules import clean_name

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
    """Write <name>.m3u (paths relative to it, ../tracks/..., as the music server expects)."""
    lines = ["#EXTM3U", f"#PLAYLIST:{title}"]  # the name a newly imported playlist gets
    seen: set[str] = set()
    for f in files:
        if f not in seen:
            seen.add(f)
            lines.append(f"../tracks/{f}")
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


def write(con: sqlite3.Connection, folder: Path) -> str:
    """Write every playlist that changed; delete the files of lists no longer shown."""
    folder.mkdir(parents=True, exist_ok=True)
    cat = library.Catalog.from_db(con)
    removed_lists = options.get(con, options.SourceOptions).removed_playlists
    lists = {r["key"]: r for r in con.execute("SELECT * FROM lists")}
    keep: set[str] = set()
    written = 0

    def best(r: sqlite3.Row) -> str | None:
        hits = cat.song(r["artist"], r["title"], r["length"], json.loads(r["artists"] or "[]"),
                        json.loads(r["link"] or "null"))  # fmt: skip
        e = hits[0] if hits else cat.by_stem.get(r["stem"] or "")
        return e.path if e else None

    for s in sources.lists(con):
        row = lists.get(s.key)
        name = clean_name(s.name)
        if not row or not s.playlist:
            continue
        keep.update(f"{name}.{e}" for e in ("m3u", "jpg", "png"))
        if not row["fetched"]:  # not listed yet: its old files stay
            continue
        songs = con.execute(
            "SELECT s.* FROM list_songs ls JOIN songs s ON s.key = ls.song_key WHERE ls.list_key = ? "
            "ORDER BY ls.position",
            (s.key,),
        ).fetchall()
        written += _m3u(folder, name, row["title"], [f for r in songs if (f := best(r))])
        _cover(con, folder, name, s.key, row["cover_url"], row["cover_file"])
        if removed_lists:
            gone = con.execute(
                "SELECT s.* FROM list_history h JOIN songs s ON s.key = h.song_key WHERE h.list_key = ? "
                "AND h.song_key NOT IN (SELECT song_key FROM list_songs WHERE list_key = ?) "
                "ORDER BY h.last_seen DESC",
                (s.key, s.key),
            ).fetchall()
            files = [f for r in gone if (f := best(r))]
            removed = f"{name} - removed"
            if files or (folder / f"{removed}.m3u").exists():
                keep.add(f"{removed}.m3u")
                written += _m3u(folder, removed, f"{row['title']} – removed", files)
    if (
        not keep
    ):  # no list to show: nothing is deleted (a missing configuration must not empty the folder)
        return f"{written} playlists written"
    ours = set(json.loads(db.get_meta(con, "playlist_files", "[]")))
    stale = [
        p for p in folder.iterdir() if p.name in ours and p.name not in keep and OURS.match(p.name)
    ]
    for p in stale:
        p.unlink()
    with con:
        db.set_meta(
            con, "playlist_files", json.dumps(sorted(n for n in keep if (folder / n).exists()))
        )
    return f"{written} playlists written" + (f", {len(stale)} old files removed" if stale else "")
