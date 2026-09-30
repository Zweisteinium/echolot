"""Review: songs filed on a probable match (rules.identify) or put up again (recheck), shown as
'Please confirm', and rejected downloads kept in inbox/review/ for 30 days, shown as 'Worth a look'.
Both get the same three answers; a decision is applied once it is UNDO_SECONDS old (until then Revert
takes it back):
  Perfect match (ok, accept)    it is the song: a kept download is filed and tagged as the song
  Close match (close)           another version, taken for the song: the file is (or gets) named what it
                                really is, 'Artist - Title', and the song is linked to it (songs.close_match)
  No match (wrong, discard)     not the song: the file goes, that download is never taken for the song
                                again, and a song that had it is searched again
"""

import datetime
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from echolot.library import audio, catalog, filing, rules
from echolot.library.filing import MUSIC, Want
from echolot.settings.sources import ConfigError

if TYPE_CHECKING:
    from echolot.jobs.worker import Run

KEPT = MUSIC + "inbox/review/"
DECISIONS = {"filed": ("ok", "close", "wrong"), "kept": ("accept", "close", "discard")}
LABELS = {"ok": "Perfect match", "accept": "Perfect match", "close": "Close match", "wrong": "No match"}
LABELS["discard"] = LABELS["wrong"]
FILED = ("new", "upgrade", "recheck")  # events of files in the library
UNDO_SECONDS = 120


def decision_id(ts: str, path: str) -> str:
    return f"{ts} {path}"


@dataclass
class Item:
    event: sqlite3.Row
    kind: str  # filed (a probable match in the library) or kept (a rejected download)
    file: Path  # where Echolot sees the audio file
    length: float  # of the wanted song, 0 = unknown
    decision: str | None  # made but not applied yet
    name: str | None = None  # close: what the file is ('Artist - Title')

    @property
    def id(self) -> str:
        return decision_id(self.event["ts"], self.event["path"])

    @property
    def length_diff(self) -> float | None:
        seconds = self.event["seconds"]
        return seconds - self.length if seconds and self.length else None

    @property
    def artists(self) -> str:
        """All the wanted song's artists (a remixer can be the only difference), else the event's one."""
        names = [self.event["artist"], *json.loads(self.event["song_artists"] or "[]")]
        return ", ".join(dict.fromkeys(n for n in names if n))

    @property
    def choices(self) -> list[tuple[str, str]]:
        """(decision, label): the three answers of the item's group."""
        return [(d, LABELS[d]) for d in DECISIONS[self.kind]]

    @property
    def label(self) -> str:
        return LABELS.get(self.decision or "", self.decision or "")

    @property
    def close_name(self) -> str:
        """What the download says it is, as 'Artist - Title': the name a close match gets unless edited."""
        if self.name:
            return self.name
        e = self.event
        found = re.split(r"[/\\]", (e["found"] or e["file_name"] or "").strip())[-1]
        stem, dot, ext = found.rpartition(".")
        if dot and ext.lower() in audio.AUDIO:
            found = stem
        artists = [e["artist"], *json.loads(e["song_artists"] or "[]")]
        return close_guess(found, artists) or f"{e['artist']} - {e['title']}"


# video decorations in a download's name: "(Official Music Video)", "[4K Upgrade]", "• TopPop", "| JCC 2020";
# not a bracket that names a version ("[Official Remix]")
_VERSION = r"remix|mix|edit|version|live|remake|rework|bootleg|vip|cover|acoustic"
_DECOR = r"official|video|visuali[sz]er|lyrics?|4k|hd|hq"
_VIDEO = [rf"\s*[\(\[](?![^\)\]]*\b(?:{_VERSION})\b)[^\)\]]*\b(?:{_DECOR})\b[^\)\]]*[\)\]]", r"\s+[•|].*$"]


def close_guess(found: str, artists: list[str]) -> str:
    """'Artist - Title' from a download's name: 'Artist - Title' or 'Title - Artist' when a side names one of
    the song's artists, else the song's artist and the whole name."""
    for pat in _VIDEO:
        found = re.sub(pat, "", found, flags=re.I).strip()
    if not found:
        return ""
    names = {w for a in artists if a for w in rules.artist_words(a)}

    def named(part: str) -> bool:
        return any(f" {w} " in rules.words(part) for w in names)

    head, _, tail = found.rpartition(" - ")
    if " - " in found and named(found.partition(" - ")[0]):
        return found
    if head and named(tail):
        return f"{tail} - {head}"
    return f"{artists[0]} - {found}"


def local_file(path: str, music_dir: Path) -> Path | None:
    """Echolot's path of an event file: library-relative, or /music/inbox/review/<...>."""
    if path.startswith(KEPT):
        p, base = music_dir / path.removeprefix(MUSIC), music_dir / "inbox" / "review"
    elif not path.startswith("/"):
        p, base = music_dir / "tracks" / path, music_dir / "tracks"
    else:
        return None
    p = p.resolve()
    return p if p.is_relative_to(base.resolve()) else None


def near_miss(e: sqlite3.Row, wanted: float) -> bool:
    """A rejected download worth a look: the artist matched and the length is not far off."""
    if (e["reason"] or "").startswith("artist "):
        return False
    return not (e["seconds"] and wanted) or 2 / 3 <= e["seconds"] / wanted <= 1.5


def upgrade(e: sqlite3.Row) -> bool:
    """A kept download that would replace the song's lossy library copy with genuine lossless."""
    lossless = (e["ext"] or "") in audio.LOSSLESS and not e["fake"]
    return lossless and e["song_quality"] not in ("lossless", None)


def items(con: sqlite3.Connection, music_dir: Path) -> dict[str, list[Item]]:
    """What to look at: probable matches still in the library without an applied decision, and kept
    rejected downloads of songs that are still missing, or genuine lossless ones of songs the library
    has only lossy (accepting one replaces the lossy copy). Newest first."""
    decisions = {r["id"]: r for r in con.execute("SELECT * FROM review_decisions")}
    rows = con.execute(
        "SELECT e.*, s.length AS wanted_length, s.file AS song_file, s.artists AS song_artists, s.url AS song_url, "
        "f.quality AS song_quality, f.kbps AS song_kbps FROM events e LEFT JOIN songs s ON s.key = e.song "
        f"LEFT JOIN files f ON f.path = s.file WHERE (e.action IN {FILED} AND e.matched = 'probable') "
        "OR (e.action IN ('wrong-song', 'mismatch') AND e.path LIKE ?) ORDER BY e.id DESC",
        (KEPT + "%",),
    ).fetchall()
    out: dict[str, list[Item]] = {"filed": [], "kept": []}
    for e in rows:
        kind = "filed" if e["action"] in FILED else "kept"
        d = decisions.get(decision_id(e["ts"], e["path"]))
        if (d and d["applied"]) or (kind == "kept" and e["song_file"] and not upgrade(e)):
            continue  # decided, or the song was found meanwhile
        length = e["wanted_length"] or e["wanted_seconds"] or 0
        if kind == "kept" and not near_miss(e, length):
            continue
        file = local_file(e["path"], music_dir)
        if file is None or not file.is_file():
            continue
        out[kind].append(Item(e, kind, file, float(length), d["decision"] if d else None, d["name"] if d else None))
    return out


def find(con: sqlite3.Connection, music_dir: Path, event_id: int) -> Item | None:
    return next((i for group in items(con, music_dir).values() for i in group if i.event["id"] == event_id), None)


def split_name(name: str) -> tuple[str, str]:
    artist, _, title = (name or "").partition(" - ")
    return artist.strip(), title.strip()


def decide(con: sqlite3.Connection, music_dir: Path, event_id: int, decision: str, name: str = "") -> Item:
    item = find(con, music_dir, event_id)
    if item is None:
        raise ConfigError("This download is no longer up for review.")
    if decision not in dict(item.choices):
        raise ConfigError(f"'{decision}' is not a decision for this download.")
    if decision == "close":
        artist, title = split_name(name)
        if not artist or not title:
            raise ConfigError("A close match needs the name its file gets, as 'Artist - Title'.")
        e = item.event
        wanted = (rules.artist_key(e["artist"]), rules.title_key(e["title"]))
        if (rules.artist_key(artist), rules.title_key(title)) == wanted:
            hint = "That is the wanted song's name. Add the version, e.g. '(Extended Mix)', or choose Perfect match."
            raise ConfigError(hint)
        name = f"{artist} - {title}"
    with con:
        con.execute(
            "INSERT OR REPLACE INTO review_decisions (id, event_id, decision, name, decided) VALUES (?, ?, ?, ?, ?)",
            (item.id, event_id, decision, name if decision == "close" else None, _now()),
        )
    return item


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def revert(con: sqlite3.Connection, music_dir: Path, event_id: int) -> Item:
    """Take back a decision that is not applied yet."""
    item = find(con, music_dir, event_id)
    if item is None or not item.decision:
        raise ConfigError("There is no pending decision for this download (or it is applied already).")
    with con:
        con.execute("DELETE FROM review_decisions WHERE id = ? AND applied IS NULL", (item.id,))
    return item


# ---------------------------------------------------------------- applying


def apply_due(run: "Run", con: sqlite3.Connection) -> list[str]:
    """Apply the decisions older than UNDO_SECONDS; returns what happened."""
    cutoff = (datetime.datetime.now() - datetime.timedelta(seconds=UNDO_SECONDS)).isoformat(timespec="seconds")
    done = []
    for d in con.execute(
        "SELECT r.id AS decision_id, r.decision, r.name AS close_name, e.* FROM review_decisions r "
        "JOIN events e ON e.id = r.event_id "
        "WHERE r.applied IS NULL AND r.decided <= ?",
        (cutoff,),
    ).fetchall():
        try:
            result = _apply(run, con, d)
        except Exception as e:  # keep the others going; the result says what went wrong
            result = f"failed: {e}"
        with con:
            con.execute(
                "UPDATE review_decisions SET applied = ?, result = ? WHERE id = ?",
                (datetime.datetime.now().isoformat(timespec="seconds"), result, d["decision_id"]),
            )
        done.append(f"{LABELS.get(d['decision'], d['decision'])} {d['artist']} - {d['title']}: {result}")
    return done


def _search_again(con: sqlite3.Connection, key: str, tries: int) -> None:
    """The song is due for a search right away, searched loosened (as after 2 failed searches)."""
    with con:
        con.execute(
            "INSERT INTO attempts (song_key, tries, last_try) VALUES (?, ?, 0) ON CONFLICT (song_key) "
            "DO UPDATE SET tries = max(tries, excluded.tries), last_try = 0",
            (key, max(tries, 2)),
        )


def _link(con: sqlite3.Connection, key: str, artist: str, title: str, close: bool) -> None:
    """The song is the library song <artist> - <title> whatever its length; close: another version of it."""
    with con:
        con.execute("DELETE FROM attempts WHERE song_key = ?", (key,))
        link = json.dumps([artist, title])
        con.execute("UPDATE songs SET link = ?, close_match = ? WHERE key = ?", (link, int(close), key))


def search_again(con: sqlite3.Connection, key: str) -> None:
    """Drop the song's link (a close match) and search for the song itself again."""
    _unlink(con, key)
    _search_again(con, key, 0)


def _unlink(con: sqlite3.Connection, key: str) -> None:
    with con:
        con.execute("UPDATE songs SET link = NULL, close_match = 0 WHERE key = ?", (key,))


def _block(con: sqlite3.Connection, key: str, d: sqlite3.Row) -> None:
    """Never take this download for the song again (its found and file names)."""
    names = [n for n in (d["found"], d["file_name"]) if n]
    if key and names:
        with con:
            con.executemany("INSERT OR IGNORE INTO blocked (song_key, name) VALUES (?, ?)", [(key, n) for n in names])


def _apply(run: "Run", con: sqlite3.Connection, d: sqlite3.Row) -> str:
    paths, key = run.paths, rules.norm_key(d["song"])
    decision = d["decision"]
    if decision == "ok":
        return "kept"
    if decision == "wrong":
        _block(con, key, d)
        with filing.LOCK:
            cat = catalog.Catalog.from_db(con)
            entry = next((e for e in cat.entries if e.path == d["path"]), None)
            result = "already gone"
            if entry and (paths.tracks / entry.path).is_file():
                filing.retire(con, paths, entry, "no match in review")
                result = "retired"
        if key:
            _unlink(con, key)
            _search_again(con, key, d["tries"] or 0)
        return result
    if decision == "close" and not d["path"].startswith(KEPT):
        return _close_filed(run, con, d, key)
    p = local_file(d["path"], paths.music)
    if p is None or not p.is_file() or not d["path"].startswith(KEPT):
        return "file gone"
    if decision == "discard":
        p.unlink()
        _block(con, key, d)  # the search fallback would keep it again
        return "deleted"
    if decision == "close":
        return _close_kept(run, con, d, key, p)
    # accept: the download is tagged with another artist who has this song in the library already: the
    # same recording under two artist names (Spotify lists it twice). Link the song to that file.
    want = Want(d["artist"] or "", d["title"] or "", 0, key)
    song = con.execute("SELECT * FROM songs WHERE key = ?", (key,)).fetchone() if key else None
    if song:
        want = Want.of(song)
    tag_artists, _ = audio.read_tags(p)
    own = rules.artist_words(want.artist)
    dur, _ = audio.probe(p)
    cat = catalog.Catalog.from_db(con)
    for other in tag_artists:
        if any(f" {w} " in rules.words(other) for w in own):
            continue
        if hits := cat.find(other, want.title, dur):
            if key:
                _link(con, key, other, want.title, close=False)
            p.unlink()
            filing.event(con, paths, "linked", paths.tracks / hits[0].path, song=key or None, artist=want.artist,
                         title=want.title, source=d["source"], matched="review",
                         reason=f"same recording as {hits[0].path} ({other})")  # fmt: skip
            return f"linked {hits[0].path}"
    action, dest = filing.file_into(con, paths, p, want, d["source"] or "", match="review", fake=bool(d["fake"]))
    if dest and action in ("new", "upgrade"):
        audio.write_tags(dest, artist=want.artist, title=want.title)  # accepted as this song
        from echolot.jobs.acquire import finish

        finish(run, con, dest, want)
    if key and action in ("new", "upgrade", "duplicate"):
        _link(con, key, want.artist, want.title, close=False)  # this file whatever its length (another edit)
    return f"{action} {dest.relative_to(paths.tracks) if dest else ''}".strip()


def _close_kept(run: "Run", con: sqlite3.Connection, d: sqlite3.Row, key: str, p: Path) -> str:
    """File a kept download under what it is (the decision's name) and link the song to it."""
    from echolot.jobs.acquire import finish

    artist, title = split_name(d["close_name"])
    if key:
        _unlink(con, key)  # filing compares with the song's link: not the old one
    want = Want(artist, title, 0, key)
    action, dest = filing.file_into(con, run.paths, p, want, d["source"] or "", match="close", fake=bool(d["fake"]))
    if dest and action in ("new", "upgrade"):
        audio.write_tags(dest, artist=artist, title=title)
        song = con.execute("SELECT * FROM songs WHERE key = ?", (key,)).fetchone() if key else None
        finish(run, con, dest, Want.of(song) if song else want)  # album and cover of the wanted song
    if key and dest:
        _link(con, key, artist, title, close=True)
    return f"{action} {dest.relative_to(run.paths.tracks) if dest else ''}".strip()


def _close_filed(run: "Run", con: sqlite3.Connection, d: sqlite3.Row, key: str) -> str:
    """A library file filed under the wanted song's name is another version: rename it to what it is (or,
    if the library has that version already, retire it) and link the song to it."""
    paths = run.paths
    artist, title = split_name(d["close_name"])
    with filing.LOCK:
        cat = catalog.Catalog.from_db(con)
        entry = next((e for e in cat.entries if e.path == d["path"]), None)
        if entry is None or not (paths.tracks / entry.path).is_file():
            return "file gone"
        if other := [e for e in cat.find(artist, title, entry.duration) if e.path != entry.path]:
            filing.retire(con, paths, entry, f"close match {artist} - {title} is in the library")
            result = f"linked {other[0].path}"
        else:
            dest = filing.rename(con, paths, entry, artist, title, f"close match for {d['artist']} - {d['title']}")
            result = f"renamed {dest.relative_to(paths.tracks)}"
    if key:
        _link(con, key, artist, title, close=True)
    return result


# ---------------------------------------------------------------- again


# where a library file came from: the event that placed it, else (a review accept names no download) the
# download accepted for the song, else the song's latest download
PLACED = "SELECT * FROM events WHERE path = ? AND action IN ('new', 'upgrade') ORDER BY id DESC"
ACCEPTED = (
    "SELECT e.* FROM review_decisions r JOIN events e ON e.id = r.event_id "
    "WHERE e.song = ? AND r.decision = 'accept' AND r.applied IS NOT NULL ORDER BY r.applied DESC"
)
LATEST = "SELECT * FROM events WHERE song = ? AND found IS NOT NULL ORDER BY id DESC"


def recheck(con: sqlite3.Connection, paths: filing.Paths, key: str, reason: str) -> bool:
    """Put a library song up for review again (Please confirm), e.g. one taken as the song that may be
    another version. It names the download the file came from, so No match blocks that download.
    False if the song has no library file or is up for review already."""
    song = con.execute("SELECT * FROM songs WHERE key = ? AND file IS NOT NULL", (key,)).fetchone()
    if song is None or any(i.event["path"] == song["file"] for g in items(con, paths.music).values() for i in g):
        return False
    placed = con.execute(PLACED, (song["file"],)).fetchone()
    origin = placed if placed and placed["found"] else con.execute(ACCEPTED, (key,)).fetchone()
    origin = origin or con.execute(LATEST, (key,)).fetchone()
    info = {k: origin[k] for k in ("source", "found", "file_name")} if origin else {}
    info |= {"song": key, "artist": song["artist"], "title": song["title"], "matched": "probable"}
    filing.event(con, paths, "recheck", paths.tracks / song["file"], reason=f"back in review: {reason}", **info)
    return True
