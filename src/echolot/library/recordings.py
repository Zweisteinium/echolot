"""The same recording under other names. Spotify lists one recording twice (single and album, another
main artist, "Edit" and "Radio Edit", a re-release), and a SoundCloud upload names it its own way; matched
by name (catalog.Catalog.song), each got its own file. So the library is asked first:

  link_isrc   a missing song whose ISRC another song has a file of is linked to that file
  in_library  before a missing song is searched: a file with the same core title and length (±3 s) that
              sounds like the song's release (identity.check) is the song
  already     before a SoundCloud download is filed: a file with the same core title and length that sounds
              the same (identity.alike) is the song; a genuine FLAC still replaces a lossy copy

Either way the song is linked to the file (songs.link, as a review link) and an event says why."""

import re
import sqlite3
from pathlib import Path

from echolot.library import catalog, filing, identity, rules
from echolot.library.filing import Paths, Want

ALIKE = 0.85  # identity.alike from here on: the same recording
# not part of a title's core: versions, edits, features, genres a title is often labelled with
_VERSION = (
    r"\b(?:remix|rmx|edit|mix|extended|original|radio|video|version|vip|rework|bootleg|flip|remaster(?:ed)?|"
    r"unmixed|single|album|live|feat|ft|featuring|with|hardstyle|techno|hardtekk|tekk)\b"
)


def core(title: str) -> str:
    """A title reduced to its core: no brackets, nothing after ' - ', no version words."""
    t = re.sub(r"[\(\[].*?[\)\]]", " ", rules.fold(title)).split(" - ")[0]
    return " ".join(re.findall(r"\w+", re.sub(_VERSION, " ", t)))


class Index:
    """The library's files by core title."""

    def __init__(self, cat: catalog.Catalog) -> None:
        self.by_core: dict[str, list[catalog.Entry]] = {}
        for e in cat.entries:
            self.by_core.setdefault(core(e.title), []).append(e)

    def candidates(self, title: str, length: float) -> list[catalog.Entry]:
        """Files with the title's core and a length within 3 s, best first."""
        c = core(title)
        if len(c) < 2 or not length:
            return []
        hits = [e for e in self.by_core.get(c, []) if abs(e.duration - length) <= 3]
        return sorted(hits, key=catalog.Entry.rank, reverse=True)


def link(con: sqlite3.Connection, paths: Paths, key: str, want: Want, e: catalog.Entry, why: str) -> None:
    """The song is the file `e` from now on: found by its name and length, as a review link (the length tells
    "I Remember" 3:21 from "I Remember (9m54s)")."""
    with con:
        con.execute("DELETE FROM attempts WHERE song_key = ?", (key,))
        con.execute("UPDATE songs SET link = json_array(?, ?, ?), close_match = 0 WHERE key = ?",
                    (e.path.partition("/")[0], e.title, round(e.duration), key))  # fmt: skip
    filing.event(con, paths, "linked", paths.tracks / e.path, song=key, artist=want.artist, title=want.title,
                 reason=f"same recording as {e.path} ({why})")  # fmt: skip


def link_isrc(con: sqlite3.Connection, paths: Paths) -> int:
    """Missing songs whose ISRC a song with a file has: linked to that file. Returns how many."""
    rows = con.execute(
        "SELECT s.*, o.file AS other FROM wanted s JOIN songs o ON o.key != s.key AND o.file IS NOT NULL "
        "AND NOT o.close_match AND upper(replace(o.isrc, '-', '')) = upper(replace(s.isrc, '-', '')) "
        "WHERE s.file IS NULL AND s.link IS NULL AND coalesce(s.isrc, '') != '' GROUP BY s.key"
    ).fetchall()
    entries = {e.path: e for e in catalog.Catalog.from_db(con).entries}
    n = 0
    for r in rows:
        if e := entries.get(r["other"]):
            link(con, paths, r["key"], Want.of(r), e, f"ISRC {r['isrc']}")
            n += 1
    return n


def in_library(con: sqlite3.Connection, paths: Paths, want: Want, index: Index) -> catalog.Entry | None:
    """A library file that is this song by its release's audio (a like title and length), or None."""
    if not want.isrc or rules.mix_cut(want.title):
        return None
    for e in index.candidates(want.title, want.length):
        if identity.check(con, want.isrc, paths.tracks / e.path).verdict == "same":
            return e
    return None


def already(paths: Paths, src: Path, title: str, length: float, index: Index) -> catalog.Entry | None:
    """A library file that sounds the same as the download `src` (a like title and length), or None."""
    for e in index.candidates(title, length):
        if identity.alike(src, paths.tracks / e.path) >= ALIKE:
            return e
    return None
