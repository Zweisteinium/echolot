"""The music library: <root>/<Artist>/<Artist> - <Title>.<ext>.

scan() keeps the files table in step with the disk; match_songs() finds each song's best library copy
with the same-song rules (rules.py). Files are put in and taken out only by filing.py.
"""

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from echolot.library.audio import AUDIO, LOSSLESS, probe
from echolot.library.rules import artist_keys, mix_cut, same_feat, same_length, title_key

# quality tiers, best first: (key, label)
QUALITY = [
    ("lossless", "Lossless"),
    ("fake", "FLAC made from lossy"),
    ("lossy-high", "Lossy ≥ 256 kbps"),
    ("lossy-mid", "Lossy 160–250 kbps"),
    ("lossy-low", "Lossy < 160 kbps"),
]

# "(3m43s)": what filing appends to a second file with the same name (another version of the title)
LENGTH_SUFFIX = re.compile(r"\s\(\d+m\d{2}s\)(?:\s\(\d+\))?$")


def title_part(stem: str, artist_dir: str) -> str:
    """Title from '<Artist> - <Title>[ (3m43s)]'."""
    stem = LENGTH_SUFFIX.sub("", stem)
    if stem.lower().startswith(artist_dir.lower() + " - "):
        return stem[len(artist_dir) + 3 :]
    return stem.split(" - ", 1)[1] if " - " in stem else stem


@dataclass
class Entry:
    path: str  # relative to the library root
    duration: float
    kbps: int
    fake: bool
    ext: str = field(init=False)
    stem: str = field(init=False)
    title: str = field(init=False)
    akeys: set[str] = field(init=False)
    tkey: str = field(init=False)

    def __post_init__(self) -> None:
        folder, _, name = self.path.rpartition("/")
        base, _, ext = name.rpartition(".")
        self.ext = ext.lower()
        self.stem = f"{folder}/{base}"
        self.title = title_part(base, folder)
        self.akeys = artist_keys(folder)
        self.tkey = title_key(self.title)

    @property
    def genuine(self) -> bool:
        return self.ext in LOSSLESS and not self.fake

    @property
    def quality(self) -> str:
        if self.genuine:
            return "lossless"
        if self.fake:
            return "fake"
        return "lossy-high" if self.kbps >= 250 else "lossy-mid" if self.kbps >= 150 else "lossy-low"

    def rank(self) -> tuple[int, int, int]:
        return (int(self.genuine), 0 if self.fake else self.kbps, -AUDIO.index(self.ext))


class Catalog:
    def __init__(self, entries: list[Entry]) -> None:
        self.entries = entries
        self.by_key: dict[tuple[str, str], list[Entry]] = {}
        self.by_stem: dict[str, Entry] = {}
        self.folders: dict[str, dict[str, int]] = {}  # artist key -> {folder: files}
        for e in entries:
            for k in e.akeys:
                self.by_key.setdefault((k, e.tkey), []).append(e)
                folders = self.folders.setdefault(k, {})
                folder = e.path.partition("/")[0]
                folders[folder] = folders.get(folder, 0) + 1
            other = self.by_stem.get(e.stem)
            if other is None or AUDIO.index(e.ext) < AUDIO.index(other.ext):
                self.by_stem[e.stem] = e

    @classmethod
    def from_db(cls, con: sqlite3.Connection) -> "Catalog":
        fakes = {r[0] for r in con.execute("SELECT stem FROM lossy_sourced")}
        rows = con.execute("SELECT path, duration, kbps FROM files ORDER BY path")
        entries = [Entry(p, d, k, p.rpartition(".")[0] in fakes) for p, d, k in rows]
        return cls([e for e in entries if e.ext in AUDIO])

    def song(
        self,
        artist: str,
        title: str,
        length: float = 0,
        artists: list[str] | None = None,
        link: list[str] | None = None,
    ) -> list[Entry]:
        """Library files for a wanted song: a review link wins, then the song's artist, then its other artists
        (a collaboration listed twice with the artists swapped). A link with a length (a close match) is told
        by it from the song itself: "Megator (Original Mix)" 6:59 and "Megator" 5:30 share a title key."""
        if link and (hits := self.find(link[0], link[1], link[2] if len(link) > 2 else 0)):
            return hits
        for a in dict.fromkeys([artist, *(artists or [])]):
            if hits := self.find(a, title, length):
                return hits
        return []

    def find(self, artist: str, title: str, length: float = 0) -> list[Entry]:
        """Library files that are the same song, best quality first (any length for a DJ-mix cut)."""
        tk = title_key(title)
        if not tk:
            return []
        if mix_cut(title):
            length = 0
        hits = {
            e.path: e
            for k in artist_keys(artist)
            for e in self.by_key.get((k, tk), [])
            if same_length(e.duration, length) and same_feat(e.title, title)
        }
        return sorted(hits.values(), key=Entry.rank, reverse=True)


Known = dict[str, tuple[int, int, float, int]]  # path -> (size, mtime, duration, kbps)


def scan(con: sqlite3.Connection, root: Path, known: Known | None = None) -> str:
    """Bring the files table up to date. Only new or changed files are probed; `known` can
    supply their duration and bitrate from another cache (size and mtime must match)."""
    if not root.is_dir():
        raise FileNotFoundError(f"library not found: {root}")
    rows = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, size, mtime FROM files")}
    known = known or {}
    seen: set[str] = set()
    changed: list[tuple[str, int, int, float, int]] = []
    probed = 0
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        for p in sorted(folder.iterdir()):
            if p.suffix.lower().lstrip(".") not in AUDIO or not p.is_file():
                continue
            rel = f"{folder.name}/{p.name}"
            st = p.stat()
            size, mtime = st.st_size, int(st.st_mtime)
            seen.add(rel)
            if rows.get(rel) == (size, mtime):
                continue
            k = known.get(rel)
            if k and k[:2] == (size, mtime):
                duration, kbps = k[2], k[3]
            else:
                duration, kbps = probe(p)
                probed += 1
            changed.append((rel, size, mtime, duration, kbps))
    if rows and not seen:
        raise RuntimeError(f"no audio files under {root} (not mounted?), keeping the last scan")
    gone = rows.keys() - seen
    with con:
        con.executemany(
            "INSERT INTO files (path, size, mtime, duration, kbps) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (path) DO UPDATE SET size = excluded.size, mtime = excluded.mtime, "
            "duration = excluded.duration, kbps = excluded.kbps",
            changed,
        )
        con.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in gone])
    return f"{len(seen)} files, {len(changed)} new or changed ({probed} probed), {len(gone)} gone"


def match_songs(con: sqlite3.Connection) -> str:
    """Set each file's quality tier and each song's best library copy."""
    cat = Catalog.from_db(con)
    songs = con.execute("SELECT key, artist, title, length, stem, artists, link FROM songs").fetchall()
    found: list[tuple[str | None, str]] = []
    for key, artist, title, length, stem, artists, link in songs:
        hits = cat.song(artist, title, length, json.loads(artists or "[]"), json.loads(link or "null"))
        best = hits[0] if hits else cat.by_stem.get(stem or "")  # SoundCloud: its own download
        found.append((best.path if best else None, key))
    with con:
        con.executemany("UPDATE files SET quality = ? WHERE path = ?", [(e.quality, e.path) for e in cat.entries])
        con.executemany("UPDATE songs SET file = ? WHERE key = ?", found)
    have = sum(1 for path, _ in found if path)
    return f"{have} of {len(songs)} songs in the library"


def refresh(con: sqlite3.Connection, root: Path) -> str:
    """Rescan the library (only new or changed files are probed) and match the songs to files."""
    return f"{scan(con, root)}; {match_songs(con)}"
