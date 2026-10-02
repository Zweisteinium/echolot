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
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from echolot.library import audio, catalog, filing, identity, rules, tagging
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
    def song_artists(self) -> list[str]:
        return [self.event["artist"], *json.loads(self.event["song_artists"] or "[]")]

    @cached_property
    def download(self) -> tuple[str, str, bool]:
        """(artist, title, another artist): what the download is by its tags; another artist when they name
        none of the wanted song's artists (a video's artist tag is often its channel; its title then starts
        with the artist: 'Artist - Title'). Without tags, or with the song's own ones (Echolot tagged a file accepted before), the name
        it was downloaded as, without an artist."""
        e = self.event
        artist, title = audio.read_credit(self.file)
        if not title or (artist, title) == (e["artist"], e["title"]):
            return "", (e["found"] or e["file_name"] or "–").strip(), False
        credited = artist + (" / " + title.partition(" - ")[0] if " - " in title else "")
        return artist, title, bool(artist) and not names_one(credited, self.song_artists)

    @property
    def quality(self) -> str:
        """The download's quality tier (catalog.QUALITY), for its chip."""
        e = self.event
        return catalog.Entry(f"-/-.{e['ext'] or ''}", 0, e["kbps"] or 0, bool(e["fake"])).quality

    @property
    def likelihood(self) -> tuple:
        """Sort key, the likeliest to be the song first: the audio check (the release's audio, unclear or not
        checked, other audio), the artist named, named as the song (only another length) before another title,
        the length within 3 s, how far off, genuine lossless, bitrate."""
        e, diff = self.event, abs(self.length_diff) if self.length_diff is not None else 10**6
        audio_ = e["audio"] or ""
        heard = 0 if "of the release" in audio_ or audio_.startswith("ISRC") else 2 if "differs" in audio_ else 1
        lossless = (e["ext"] or "") in audio.LOSSLESS and not e["fake"]
        named = e["action"] != "wrong-song"
        return heard, self.download[2], not named, diff > 3, diff, not lossless, -(e["kbps"] or 0)

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
        return close_guess(found, self.song_artists) or f"{e['artist']} - {e['title']}"


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
    head, _, tail = found.rpartition(" - ")
    if " - " in found and names_one(found.partition(" - ")[0], artists):
        return found
    if head and names_one(tail, artists):
        return f"{tail} - {head}"
    return f"{artists[0]} - {found}"


def names_one(text: str, artists: list[str]) -> bool:
    """The text names one of the artists (as a whole name, see rules.artist_words)."""
    names = {w for a in artists if a for w in rules.artist_words(a)}
    return any(f" {w} " in rules.words(text) for w in names)


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
    has only lossy (accepting one replaces the lossy copy; a SoundCloud song's FLAC always waits here,
    'confirm'). Newest first."""
    decisions = {r["id"]: r for r in con.execute("SELECT * FROM review_decisions")}
    rows = con.execute(
        "SELECT e.*, s.length AS wanted_length, s.file AS song_file, s.artists AS song_artists, s.url AS song_url, "
        "f.quality AS song_quality, f.kbps AS song_kbps FROM events e LEFT JOIN songs s ON s.key = e.song "
        f"LEFT JOIN files f ON f.path = s.file WHERE (e.action IN {FILED} AND e.matched = 'probable') "
        "OR (e.action IN ('wrong-song', 'mismatch', 'confirm') AND e.path LIKE ?) ORDER BY e.id DESC",
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


TAKE = ("ok", "accept", "close")  # decisions that give the song a file


@dataclass
class Group:
    """One wanted song and its downloads up for review, the likeliest first. Taking one of its kept downloads
    (Perfect or Close match) discards the others when it is applied."""

    items: list[Item]

    @property
    def lead(self) -> Item:
        return self.items[0]

    @property
    def dom_id(self) -> str:
        return "song-" + re.sub(r"\W", "-", self.lead.event["song"] or f"event-{self.lead.event['id']}")

    @property
    def taken(self) -> Item | None:
        return next((i for i in self.items if i.decision in TAKE), None) if self.lead.kind == "kept" else None

    @property
    def undecided(self) -> list[Item]:
        return [i for i in self.items if not i.decision]

    @property
    def tries(self) -> int:
        return max(i.event["tries"] or 0 for i in self.items)


def groups(found: list[Item]) -> list[Group]:
    """The items by wanted song, in the order of each song's newest item."""
    by_song: dict[str, list[Item]] = {}
    for i in found:
        by_song.setdefault(i.event["song"] or f"event {i.event['id']}", []).append(i)
    return [Group(sorted(rows, key=lambda i: i.likelihood)) for rows in by_song.values()]


def partner(con: sqlite3.Connection, e: sqlite3.Row, music_dir: Path) -> Path | None:
    """The other file of the song a review item is compared with: for a kept download the song's library
    file (what accepting it would replace), for a filed upgrade the file it replaced (in replaced/)."""
    if e["path"].startswith(KEPT):
        return local_file(e["song_file"], music_dir) if e["song_file"] else None
    if e["action"] != "upgrade":
        return None
    stem = e["path"].rsplit(".", 1)[0].lower()
    for r in con.execute(
        "SELECT path FROM events WHERE action = 'retired' AND reason LIKE 'replaced by genuine lossless%' "
        "AND id BETWEEN ? AND ?",
        (e["id"] - 4, e["id"] + 4),
    ):
        was = r["path"].removeprefix(MUSIC + "inbox/replaced/").partition("/")[2]  # <date>/<library path>
        if was.rsplit(".", 1)[0].lower() == stem and (p := music_dir / r["path"].removeprefix(MUSIC)).is_file():
            return p
    return None


def compare_open(con: sqlite3.Connection, music_dir: Path, limit: int = 4) -> int:
    """Compare up to `limit` open review items with their partner file (identity.compare, about 1 s each)
    and keep the result with the event; '' when there is nothing to compare. Returns how many."""
    todo = [i for rows in items(con, music_dir).values() for i in rows if i.event["compared"] is None][:limit]
    for i in todo:
        other = partner(con, i.event, music_dir)
        hint = identity.compare(i.file, other) if other else ""
        with con:
            con.execute("UPDATE events SET compared = ? WHERE id = ?", (hint, i.event["id"]))
    return len(todo)


def find_group(con: sqlite3.Connection, music_dir: Path, event_id: int) -> Group | None:
    for rows in items(con, music_dir).values():
        for g in groups(rows):
            if any(i.event["id"] == event_id for i in g.items):
                return g
    return None


def discard_all(con: sqlite3.Connection, music_dir: Path, event_id: int) -> Group:
    """No match for every kept download of the song that has no decision yet."""
    g = find_group(con, music_dir, event_id)
    if g is None or g.lead.kind != "kept":
        raise ConfigError("These downloads are no longer up for review.")
    for i in g.undecided:
        decide(con, music_dir, i.event["id"], "discard")
    return g


def find(con: sqlite3.Connection, music_dir: Path, event_id: int) -> Item | None:
    return next((i for group in items(con, music_dir).values() for i in group if i.event["id"] == event_id), None)


def split_name(name: str) -> tuple[str, str]:
    artist, _, title = (name or "").partition(" - ")
    return artist.strip(), title.strip()


def decide(
    con: sqlite3.Connection, music_dir: Path, event_id: int, decision: str, name: str = "", user_id: int | None = None
) -> Item:
    """Record a decision (applied when its undo time is over: apply_due) and who made it."""
    item = find(con, music_dir, event_id)
    if item is None:
        raise ConfigError("This download is no longer up for review.")
    if decision not in dict(item.choices):
        raise ConfigError(f"'{decision}' is not a decision for this download.")
    taken = find_group(con, music_dir, event_id).taken
    if decision in TAKE and taken and taken.event["id"] != event_id:
        raise ConfigError("Another download of this song is taken already: revert that one first.")
    if decision == "close":  # any name; a title alone takes the song's artist
        artist, title = split_name(name) if " - " in name else (item.event["artist"], name.strip())
        if not artist or not title:
            raise ConfigError("A close match needs a name for its file.")
        name = f"{artist} - {title}"
        if taken_by := _name_taken(con, music_dir, artist, title, item.file):
            raise ConfigError(f"“{taken_by}” is already a file in your library: give this one another name.")
    with con:
        con.execute(
            "INSERT OR REPLACE INTO review_decisions (id, event_id, decision, name, decided, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (item.id, event_id, decision, name if decision == "close" else None, _now(), user_id),
        )
    return item


def _name_taken(con: sqlite3.Connection, music_dir: Path, artist: str, title: str, own: Path) -> str:
    """The library file that has the name a close match would get ('' if none): filing would otherwise add
    the length to the name, and the name is to be the one given."""
    paths = filing.Paths(music_dir)
    folder = filing.artist_dir(paths, catalog.Catalog.from_db(con), artist)
    stem = f"{folder.name} - {rules.clean_name(title)}"
    taken = [folder / f"{stem}.{ext}" for ext in audio.AUDIO if (folder / f"{stem}.{ext}").exists()]
    return next((p.name for p in taken if p.resolve() != own.resolve()), "")


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


def _link(con: sqlite3.Connection, key: str, artist: str, title: str, close: bool, seconds: float = 0) -> None:
    """The song is the library song <artist> - <title> whatever the song's length; close: another version
    of it, linked with the version's length (its title can reduce to the song's, catalog.Catalog.song)."""
    with con:
        con.execute("DELETE FROM attempts WHERE song_key = ?", (key,))
        link = json.dumps([artist, title, round(seconds)] if close and seconds else [artist, title])
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
        return _confirm(run, con, d)
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
    taken = _close_kept(run, con, d, key, p) if decision == "close" else _accept_kept(run, con, d, key, p)
    if gone := _discard_rest(run, con, d, key):
        taken += f"; {gone} other download{'s' if gone != 1 else ''} of the song discarded"
    return taken


def _confirm(run: "Run", con: sqlite3.Connection, d: sqlite3.Row) -> str:
    """Perfect match for a library file: it stays and gets the song's tags (the uploader's may name another
    song: why it was in review)."""
    p = run.paths.tracks / d["path"]
    if not p.is_file():
        return "file gone"
    if (tags := tagging.for_file(con, d["path"], p)) and tagging.differs(p, tags):
        tagging.write(p, tags)
        return "kept, tagged as the song"
    return "kept"


def _accept_kept(run: "Run", con: sqlite3.Connection, d: sqlite3.Row, key: str, p: Path) -> str:
    """File a kept download as the song (Perfect match)."""
    paths = run.paths
    # the download is tagged with another artist who has this song in the library already: the same
    # recording under two artist names (Spotify lists it twice). Link the song to that file.
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
    if key:  # it is the song whatever its length: a lossy copy the library has is replaced, under its name
        _link(con, key, want.artist, want.title, close=False)
    origin = {"fake": bool(d["fake"]), "url": d["url"] or ""}
    action, dest = filing.file_into(con, paths, p, want, d["source"] or "", match="review", **origin)
    if dest and action in ("new", "upgrade"):
        from echolot.jobs.acquire import finish

        finish(run, con, dest, want)  # tagged as this song
    if key and action in ("new", "upgrade", "duplicate"):
        _link(con, key, want.artist, want.title, close=False)  # this file whatever its length (another edit)
    return f"{action} {dest.relative_to(paths.tracks) if dest else ''}".strip()


def _discard_rest(run: "Run", con: sqlite3.Connection, d: sqlite3.Row, key: str) -> int:
    """The song took one of its kept downloads: the others without a decision of their own go (No match)."""
    decided = {r[0] for r in con.execute("SELECT event_id FROM review_decisions")}
    rows = con.execute(
        "SELECT * FROM events WHERE song = ? AND id != ? AND action IN ('wrong-song', 'mismatch') AND path LIKE ?",
        (d["song"], d["id"], KEPT + "%"),
    ).fetchall()
    gone = 0
    for e in rows:
        p = local_file(e["path"], run.paths.music)
        if e["id"] not in decided and p is not None and p.is_file():
            p.unlink()
            _block(con, key, e)
            gone += 1
    return gone


def _close_kept(run: "Run", con: sqlite3.Connection, d: sqlite3.Row, key: str, p: Path) -> str:
    """File a kept download under what it is (the decision's name) and link the song to it."""
    from echolot.jobs.acquire import finish

    artist, title = split_name(d["close_name"])
    if key:
        _unlink(con, key)  # filing compares with the song's link: not the old one
    want, origin = Want(artist, title, 0, key), {"fake": bool(d["fake"]), "url": d["url"] or ""}
    action, dest = filing.file_into(con, run.paths, p, want, d["source"] or "", match="close", **origin)
    if key and dest:  # first: the tags are the name given here (tagging reads the link)
        _link(con, key, artist, title, close=True, seconds=audio.probe(dest)[0])
    if dest and action in ("new", "upgrade"):
        song = con.execute("SELECT * FROM songs WHERE key = ?", (key,)).fetchone() if key else None
        finish(run, con, dest, Want.of(song) if song else want)  # the cover of the wanted song
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
        dest = None
        if other := [e for e in cat.find(artist, title, entry.duration) if e.path != entry.path]:
            filing.retire(con, paths, entry, f"close match {artist} - {title} is in the library")
            result = f"linked {other[0].path}"
        else:
            dest = filing.rename(con, paths, entry, artist, title, f"close match for {d['artist']} - {d['title']}")
            result = f"renamed {dest.relative_to(paths.tracks)}"
    if key:
        _link(con, key, artist, title, close=True, seconds=entry.duration)
    if dest and (tags := tagging.for_file(con, dest.relative_to(paths.tracks).as_posix(), dest, key)):
        tagging.write(dest, tags)  # the name given, the songs' pages, where it was downloaded
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
