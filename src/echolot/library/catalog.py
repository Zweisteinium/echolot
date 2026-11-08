"""The music library: <root>/<Artist>/<Artist> - <Title>.<ext>.

scan() keeps the files table in step with the disk; match_songs() finds each song's best copy by the
same-song rules (rules.py). Only filing.py puts files in or takes them out.
"""

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from echolot.library.audio import AUDIO, LOSSLESS, LOSSPRINT_FAKE, probe
from echolot.library.rules import artist_keys, mix_cut, same_feat, same_length, title_key

# files.quality, best first: (key, label); a fake FLAC is a tier of its own here (upgrades, review and recording
# decide by it)
QUALITY = [
    ("lossless", "FLAC"),
    ("fake", "Fake FLAC"),
    ("lossy-high", "Lossy, high"),
    ("lossy-mid", "Lossy, medium"),
    ("lossy-low", "Lossy, low"),
]
# what the pages count by (files.tier): a fake FLAC in the tier of the lossy file it was made from
TIERS = [t for t in QUALITY if t[0] != "fake"]
# measured kbps a format needs for high and for medium, a little under the nominal rates (files measure below
# them): high is transparent for nearly everyone (MP3 V0/320, AAC and Vorbis 192, Opus 128), medium good (MP3
# V2/192, AAC 128-160, Opus 96)
LIMITS = {"mp3": (240, 160), "aac": (190, 120), "vorbis": (190, 120), "opus": (120, 80)}
CODECS = {"mp3": "mp3", "m4a": "aac", "aac": "aac", "ogg": "vorbis", "opus": "opus", "webm": "opus"}
_SOURCE = re.compile(r"(?:(mp3|aac|opus|vorbis)\s*)?~(\d+)(?:-\d+)?\s*kbps", re.I)


def lossy_tier(codec: str, kbps: float) -> str:
    """The tier of lossy audio of a codec (CODECS' values; others count as MP3) at a bitrate."""
    high, mid = LIMITS.get(codec, LIMITS["mp3"])
    return "lossy-high" if kbps >= high else "lossy-mid" if kbps >= mid else "lossy-low"


def source_tier(source: str | None) -> str | None:
    """The tier of the lossy file a fake FLAC was made from, by the checks' estimate ("Opus ~131 kbps"; the
    spectrum's "~256 kbps / V0" as MP3; a range by its lower end)."""
    m = _SOURCE.search(source or "")
    return lossy_tier((m[1] or "mp3").lower(), int(m[2])) if m else None


# what filing appends to another version's file of the same name: " (3m43s)", " (3m43s) (2)"
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
    fake: bool  # made from a lossy file (lossy_sourced); only a lossless format can be
    source: str = ""  # a fake's source as the checks estimate it (source_tier)
    ext: str = field(init=False)
    stem: str = field(init=False)
    title: str = field(init=False)
    akeys: set[str] = field(init=False)
    tkey: str = field(init=False)

    def __post_init__(self) -> None:
        folder, _, name = self.path.rpartition("/")
        base, _, ext = name.rpartition(".")
        self.ext = ext.lower()
        self.fake = self.fake and self.ext in LOSSLESS  # (a lossy file under a fake FLAC's name is what it is)
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
        return lossy_tier(CODECS.get(self.ext, "mp3"), self.kbps)

    @property
    def tier(self) -> str:
        """The tier the pages count it in: a fake FLAC's source's (medium if unknown)."""
        return (source_tier(self.source) or "lossy-mid") if self.fake else self.quality

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
        fakes = dict(con.execute("SELECT stem, source FROM lossy_sourced").fetchall())
        rows = con.execute("SELECT path, duration, kbps FROM files ORDER BY path")
        checked = _estimates(con)
        entries = [
            Entry(p, d, k, (stem := p.rpartition(".")[0]) in fakes, fakes.get(stem) or checked.get(p, ""))
            for p, d, k in rows
        ]
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
        (a collaboration listed with the artists swapped). A link's length tells a close match from the song:
        "Megator (Original Mix)" 6:59 and "Megator" 5:30 share a title key."""
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


def _estimates(con: sqlite3.Connection) -> dict[str, str]:
    """path -> the lossy check's source estimate (for fakes without their own): lossprint's format and bitrate
    where it is sure (as audio.judge trusts it), else the spectrum's."""
    out = {}
    for path, lossprint, spectrum in con.execute(
        "SELECT path, lossprint, spectrum FROM lossy_checks WHERE verdict != 'ok'"
    ).fetchall():
        try:
            lp, sp = json.loads(lossprint or "null") or {}, json.loads(spectrum or "null") or {}
        except ValueError:
            continue
        if (lp.get("p") or 0) >= LOSSPRINT_FAKE and lp.get("kbps"):
            out[path] = f"{lp.get('encoder') or ''} ~{round(lp['kbps'])} kbps"
        elif sp.get("source"):
            out[path] = sp["source"]
    return out


Known = dict[str, tuple[int, int, float, int]]  # path -> (size, mtime, duration, kbps)


def scan(con: sqlite3.Connection, root: Path, known: Known | None = None) -> str:
    """Bring the files table up to date, probing only new or changed files; `known` may supply their duration
    and bitrate (size and mtime must match)."""
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
            "duration = excluded.duration, kbps = excluded.kbps, pages = NULL",  # (its tags are read again)
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
        con.executemany(
            "UPDATE files SET quality = ?, tier = ? WHERE path = ?", [(e.quality, e.tier, e.path) for e in cat.entries]
        )
        con.executemany("UPDATE songs SET file = ? WHERE key = ?", found)
    have = sum(1 for path, _ in found if path)
    return f"{have} of {len(songs)} songs in the library"


def refresh(con: sqlite3.Connection, root: Path) -> str:
    """Rescan the library and match the songs to files."""
    return f"{scan(con, root)}; {match_songs(con)}"
