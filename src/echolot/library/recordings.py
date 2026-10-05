"""The same recording under other names. Spotify lists one recording twice (single and album, another
main artist, "Edit" and "Radio Edit", a re-release), and a SoundCloud upload names it its own way; matched
by name (catalog.Catalog.song), each got its own file. So the library is asked first:

  link_isrc   a missing song whose ISRC another song has a file of is linked to that file
  in_library  before a missing song is searched: a file with the same core title and length (±3 s) that
              sounds like the song's release (identity.check) is the song
  already     before a SoundCloud or YouTube download is filed: a file with the same core title and length
              that sounds the same (identity.alike) is the song; a genuine FLAC still replaces a lossy copy
  another_edit / merge_edits  a YouTube song (an upload is often a shorter edit) named as a library song of
              another length: the same recording by its audio (SAME_EDIT) is linked to that file, one that is
              probably the same (ALIKE) goes to review

Either way the song is linked to the file (songs.link, as a review link) and an event says why."""

import json
import re
import sqlite3
from pathlib import Path

from echolot.library import audio, catalog, filing, identity, rules
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


# ---------------------------------------------------------------- another edit of a library song (YouTube)

SAME_EDIT = 0.9  # identity.alike from here on: the same recording for sure, whatever the lengths (linked)
EDIT_REASON = "probably the same recording as {path} in another edit (audio {share:.2f})"
EDIT_RE = re.compile(r"probably the same recording as (?P<path>.+) in another edit \(audio [\d.]+\)")


def other_edits(
    cat: catalog.Catalog, want: Want, seconds: float, own: str = "", con: sqlite3.Connection | None = None
) -> list[catalog.Entry]:
    """Library files named as the song (any of its artists, its title), or the files of songs named so (a
    collaboration filed under its other artist), but more than 3 s off its length `seconds`: maybe another
    edit of the recording (a YouTube upload often is shorter); not `own`."""
    if rules.mix_cut(want.title):
        return []
    found = [e for a in dict.fromkeys([want.artist, *want.artists]) for e in cat.find(a, want.title, 0)]
    if con is not None:
        ours, tk = {k for a in [want.artist, *want.artists] for k in rules.artist_keys(a)}, rules.title_key(want.title)
        entries = {e.path: e for e in cat.entries}
        for r in con.execute(
            "SELECT artist, artists, title, file FROM songs WHERE file IS NOT NULL AND key != ?", (want.key,)
        ):
            names = [r["artist"], *json.loads(r["artists"] or "[]")]
            if (
                r["file"] in entries
                and rules.title_key(r["title"]) == tk
                and ours & {k for a in names for k in rules.artist_keys(a)}
            ):
                found.append(entries[r["file"]])
    hits = {e.path: e for e in found if e.path != own and abs(e.duration - seconds) > 3}
    return sorted(hits.values(), key=catalog.Entry.rank, reverse=True)


def another_edit(
    paths: Paths, src: Path, want: Want, cat: catalog.Catalog, own: str = "", con: sqlite3.Connection | None = None
) -> tuple[catalog.Entry, float] | None:
    """The library file that sounds most like `src` among the song's other edits (other_edits), with how
    alike they are (identity.alike); None when none is the same recording (ALIKE)."""
    seconds = audio.probe(src)[0]
    best = None
    for e in other_edits(cat, want, seconds, own, con):
        share = identity.alike(src, paths.tracks / e.path)
        if share >= ALIKE and (best is None or share > best[1]):
            best = (e, share)
    return best


def merge_edits(con: sqlite3.Connection, paths: Paths) -> list[str]:
    """YouTube songs whose own file is another edit of a file the library has under their names: at
    SAME_EDIT linked to that file, their own one retired; from ALIKE put up for review again (Please
    confirm). Each pair of files is compared once (meta edits_checked)."""
    from echolot import db
    from echolot.library import review

    checked = json.loads(db.get_meta(con, "edits_checked", "{}"))
    cat = catalog.Catalog.from_db(con)
    entries = {e.path: e for e in cat.entries}
    rows = con.execute(
        "SELECT * FROM wanted s WHERE service = 'youtube' AND file IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM songs o WHERE o.file = s.file AND o.service != 'youtube')"
    ).fetchall()
    done = []
    for row in rows:
        own = entries.get(row["file"])
        if own is None:
            continue
        want = Want.of(row)
        for e in other_edits(cat, want, own.duration, own.path, con):
            pair = f"{own.path}|{e.path}"
            if pair not in checked:
                checked[pair] = round(identity.alike(paths.tracks / own.path, paths.tracks / e.path), 3)
            share = checked[pair]
            if share >= SAME_EDIT:
                link(con, paths, row["key"], want, e, f"another edit, audio {share:.2f}")
                others = con.execute(
                    "SELECT 1 FROM songs WHERE file = ? AND key != ?", (own.path, row["key"])
                ).fetchone()
                if not others and (paths.tracks / own.path).is_file():
                    filing.retire(con, paths, own, f"another edit of {e.path}, which the song has now")
                done.append(f"{want.artist} - {want.title} -> {e.path}")
                break
            if share >= ALIKE and review.recheck(con, paths, row["key"], EDIT_REASON.format(path=e.path, share=share)):
                done.append(f"{want.artist} - {want.title}: in review ({share:.2f})")
                break
    with con:
        db.set_meta(con, "edits_checked", json.dumps(checked))
    return done
