"""The only code that puts files into the library (<music>/tracks/<Artist>/<Artist> - <Title>.<ext>)
or takes them out. Everything happens under one lock, against the files table, which every change here
keeps current (catalog.scan picks up changes made by others):
  - nothing in the library is ever overwritten: files are linked into place with an exclusive create
  - a download that is the same song as a library file is discarded, unless it is a genuine lossless
    copy of a lossy or fake one: then it takes over and the old file moves to inbox/replaced/<date>/
  - a different song whose file name is taken (same title, other length) gets its length appended
  - artist folders are matched case/accent-insensitively, so one artist keeps one folder
  - search results must be the requested song (rules.identify); rejected near misses (right artist,
    2/3 to 1.5 times the length) are kept in inbox/review/<date>/, other rejects are deleted
  - replaced/ and review/ are emptied after 30 days (purge)
  - every change is an event (events table)
"""

import datetime
import filecmp
import glob
import os
import shutil
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from echolot.library import audio, catalog, tagging
from echolot.library.identity import UNKNOWN, Evidence
from echolot.library.rules import artist_key, clean_name, first_artist, identify, mix_cut, norm_key, same_length

LOCK = threading.RLock()
KEEP_DAYS = 30
MUSIC = "/music/"  # how events name files outside the library (independent of the mount point)


@dataclass(frozen=True)
class Paths:
    music: Path

    @property
    def tracks(self) -> Path:
        return self.music / "tracks"

    @property
    def playlists(self) -> Path:
        return self.music / "playlists"

    def inbox(self, name: str) -> Path:
        return self.music / "inbox" / name


@dataclass
class Want:
    """The song a file is meant to be."""

    artist: str
    title: str
    length: float = 0
    key: str = ""
    artists: list[str] = field(default_factory=list)
    isrc: str = ""  # the recording (Spotify), for the audio check

    @classmethod
    def of(cls, row: sqlite3.Row) -> "Want":
        import json

        isrc = row["isrc"] if "isrc" in row.keys() else ""  # noqa: SIM118 (on a Row, "in" tests the values)
        artists = json.loads(row["artists"] or "[]")
        return cls(row["artist"], row["title"], row["length"] or 0, row["key"], artists, isrc or "")


def event_path(paths: Paths, p: Path) -> str:
    """Library files relative to tracks/, others as /music/<...>."""
    if p.is_relative_to(paths.tracks):
        return p.relative_to(paths.tracks).as_posix()
    if p.is_relative_to(paths.music):
        return MUSIC + p.relative_to(paths.music).as_posix()
    return str(p)


def event(con: sqlite3.Connection, paths: Paths, action: str, p: Path, **info: object) -> None:
    st = p.stat() if p.exists() else None
    dur, kbps = audio.probe(p) if st else (0.0, 0)
    row = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "path": event_path(paths, p),
        "ext": p.suffix.lstrip(".").lower(),
        "bytes": st.st_size if st else 0,
        "kbps": kbps,
        "seconds": round(dur),
        **{k: v for k, v in info.items() if v not in (None, "")},
    }
    with con:
        con.execute(f"INSERT INTO events ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))


def _place(src: Path, dest: Path) -> None:
    """Move src to dest without ever replacing an existing file (exclusive hard link, then unlink)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dest)  # FileExistsError instead of overwriting
    except FileExistsError:
        raise
    except OSError:  # another filesystem
        with open(src, "rb") as fi, open(dest, "xb") as fo:
            shutil.copyfileobj(fi, fo)
        shutil.copystat(src, dest)
    os.unlink(src)


def _free_name(folder: Path, name: str, ext: str, length: float, label: str = "") -> Path:
    """First unused '<name>.<ext>' in folder; a taken name (by any extension) gets the label (another
    version from SoundCloud: '(SoundCloud)'), else the length appended."""

    def taken(n: str) -> bool:
        return any((folder / f"{n}.{e}").exists() for e in audio.AUDIO)

    if not taken(name):
        return folder / f"{name}.{ext}"
    if label and not taken(f"{name} ({label})"):
        return folder / f"{name} ({label}).{ext}"
    base = f"{name} ({int(length) // 60}m{int(length) % 60:02d}s)" if length else f"{name} (2)"
    cand, i = base, 2
    while taken(cand):
        cand, i = f"{base} ({i})", i + 1
    return folder / f"{cand}.{ext}"


def _add_file(con: sqlite3.Connection, paths: Paths, p: Path, fake: bool) -> None:
    """Record a file just placed in the library (files, lossy_sourced)."""
    st = p.stat()
    dur, kbps = audio.probe(p)
    rel = p.relative_to(paths.tracks).as_posix()
    entry = catalog.Entry(rel, dur, kbps, fake)
    with con:
        con.execute(
            "INSERT OR REPLACE INTO files (path, size, mtime, duration, kbps, quality) VALUES (?, ?, ?, ?, ?, ?)",
            (rel, st.st_size, int(st.st_mtime), dur, kbps, entry.quality),
        )
        if fake:
            con.execute(
                "INSERT OR REPLACE INTO lossy_sourced (stem, source, detected) VALUES (?, ?, ?)",
                (entry.stem, "", datetime.date.today().isoformat()),
            )


def retire(con: sqlite3.Connection, paths: Paths, entry: catalog.Entry, reason: str) -> Path:
    """Take a library file out into inbox/replaced/<date>/ (kept KEEP_DAYS days)."""
    src = paths.tracks / entry.path
    dest = paths.inbox("replaced") / datetime.date.today().isoformat() / entry.path
    n = 1
    while dest.exists():
        dest = dest.with_name(f"{src.stem} ({n}){src.suffix}")
        n += 1
    _place(src, dest)
    with con:
        con.execute("DELETE FROM files WHERE path = ?", (entry.path,))
        con.execute("DELETE FROM lossy_sourced WHERE stem = ?", (entry.stem,))
    event(con, paths, "retired", dest, reason=f"{reason} (was {entry.path})")
    return dest


def rename(con: sqlite3.Connection, paths: Paths, entry: catalog.Entry, artist: str, title: str, reason: str) -> Path:
    """File a library file under what it really is (<Artist>/<Artist> - <Title>), e.g. a close match that
    was filed under the wanted song's name. The tags follow the name."""
    src = paths.tracks / entry.path
    with LOCK:
        cat = catalog.Catalog.from_db(con)
        folder = artist_dir(paths, cat, artist)
        stem = f"{folder.name} - {clean_name(title)}"
        dest = src
        if (src.parent, src.stem) != (folder, stem):
            dest = _free_name(folder, stem, entry.ext, entry.duration)
            _place(src, dest)
            stem = dest.relative_to(paths.tracks).with_suffix("").as_posix()
            with con:
                con.execute("DELETE FROM files WHERE path = ?", (entry.path,))
                con.execute("DELETE FROM lossy_sourced WHERE stem = ?", (entry.stem,))
                # a SoundCloud song is its own download, found by the name: it keeps the file
                con.execute("UPDATE songs SET stem = ? WHERE stem = ?", (stem, entry.stem))
            _add_file(con, paths, dest, entry.fake)
        audio.write_tags(dest, artist=artist, title=title)
        event(con, paths, "renamed", dest, artist=artist, title=title, reason=f"{reason} (was {entry.path})")
    return dest


def keep(paths: Paths, src: Path, artist: str, title: str, source: str) -> Path:
    """Move a rejected download to inbox/review/<date>/ (kept KEEP_DAYS days, see the review page). The same
    file kept for the song already is not kept twice: then it is deleted (the returned path is gone)."""
    song = f"{clean_name(artist)} - {clean_name(title)} ["
    size = src.stat().st_size
    for other in paths.inbox("review").glob(f"*/{glob.escape(song)}*"):
        if other.stat().st_size == size and filecmp.cmp(other, src, shallow=False):
            return drop(src)
    folder = paths.inbox("review") / datetime.date.today().isoformat()
    base = f"{clean_name(artist)} - {clean_name(title)} [{clean_name(source or 'download')}]"
    for n in range(1, 1000):
        dest = folder / (f"{base}{src.suffix.lower()}" if n == 1 else f"{base} ({n}){src.suffix.lower()}")
        if dest.exists():
            continue
        try:
            _place(src, dest)
            return dest
        except FileExistsError:
            continue
    src.unlink(missing_ok=True)
    return src


def drop(src: Path) -> Path:
    """Delete a rejected download that is no near miss (another artist, or a whole mix)."""
    src.unlink(missing_ok=True)
    return src


def in_review(paths: Paths, artist: str, title: str) -> bool:
    """A kept download of this song waits on the review page (named as keep() names it)."""
    name = f"{clean_name(artist)} - {clean_name(title)} ["
    return any(paths.inbox("review").glob(f"*/{glob.escape(name)}*"))


def rejected_before(con: sqlite3.Connection, key: str) -> list[tuple[str, int]]:
    """(name, seconds) of the downloads the checks rejected for the song in the last KEEP_DAYS days. The same
    file would be rejected again, so it is not downloaded again (after that a changed rule gets its chance)."""
    since = (datetime.datetime.now() - datetime.timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    sql = "SELECT file_name, seconds FROM events WHERE song = ? AND action IN ('wrong-song', 'mismatch') AND ts >= ?"
    return [(n.strip(), s or 0) for n, s in con.execute(sql, (norm_key(key), since)) if n]


def was_rejected(before: list[tuple[str, int]], name: str, seconds: float) -> bool:
    """A search result is a download rejected before: the same name and, where both are known, length (±2 s)."""
    name = name.strip()
    return any(n == name and (not s or not seconds or abs(s - seconds) <= 2) for n, s in before)


def is_blocked(con: sqlite3.Connection, key: str, names: list[str]) -> bool:
    names = [n for n in names if n]
    if not key or not names:
        return False
    marks = ", ".join("?" * len(names))
    return bool(
        con.execute(f"SELECT 1 FROM blocked WHERE song_key = ? AND name IN ({marks})", (key, *names)).fetchone()
    )


def artist_dir(paths: Paths, cat: catalog.Catalog, artist: str) -> Path:
    """The artist's existing folder (any spelling; the fuller one), else a new one."""
    keys = [k for k in (artist_key(artist), artist_key(first_artist(artist))) if k]
    for k in keys:
        if folders := cat.folders.get(k):
            return paths.tracks / max(folders, key=lambda f: folders[f])
    for d in paths.tracks.iterdir():  # a folder without audio (only artist.jpg left)
        if d.is_dir() and artist_key(d.name) in keys:
            return d
    return paths.tracks / clean_name(artist)


def song_link(con: sqlite3.Connection, key: str) -> list[str] | None:
    import json

    row = con.execute("SELECT link FROM songs WHERE key = ?", (key,)).fetchone() if key else None
    return json.loads(row[0]) if row and row[0] else None


def file_into(
    con: sqlite3.Connection,
    paths: Paths,
    src: Path,
    want: Want,
    source: str,
    *,
    strict: bool = False,
    file_name: str = "",
    folders: tuple[str, ...] = (),
    probable: bool = True,
    tries: int = 0,
    match: str | None = None,
    fake: bool = False,
    heard: Evidence = UNKNOWN,
    url: str = "",
) -> tuple[str, Path | None]:
    """Put a downloaded file into the library. Returns (action, library path); action is
    'new', 'upgrade' (replaced a lossy or fake copy), 'duplicate' (discarded: the library has it),
    'mismatch' (strict: the length is another version's) or 'wrong-song' (strict: tags and source names
    are not the song). strict is for search results: the download must be the song (identify with
    file_name/folders, where it came from). An exact match is filed; a probable one is filed marked for
    review, or with probable=False (upgrades, SoundCloud uploader names) kept for review. `heard` is what
    the audio says (identity.check): the release's audio makes a probable match exact, other audio keeps
    even an exact one for review. A rejected download far off the length is deleted, not kept. `url` is
    the page it was downloaded from (YouTube, SoundCloud), kept with the event."""
    ext = src.suffix.lower().lstrip(".")
    dur, _ = audio.probe(src)
    key = norm_key(want.key)
    length = 0 if mix_cut(want.title) else float(want.length or 0)
    info: dict[str, object] = {"source": source, "song": key or None, "artist": want.artist, "title": want.title}
    info["url"] = url
    if strict:
        tag_artists, tag_title = audio.read_tags(src)
        info.update(found=tag_title or file_name, file_name=file_name, fake=int(fake), tries=tries, audio=heard.detail)
        tol = 3 if source == "soulseek" else 6  # videos have intros
        match, why = identify(want.artist, want.title, tag_artists, tag_title, file_name, folders, dur, length, tol)
        if match == "exact" and tagging.conflict(tag_title, want.title):  # named the song, tagged as another
            match, why = "probable", f"the file's tags name another song: '{tag_title}'"
        if match == "probable" and heard.verdict == "same":
            match = "exact"  # confirmed by the audio: no review needed
        ok = match == "exact" or (match == "probable" and probable)
        if match == "probable" and not probable:
            why = f"{why} (not filed: {source} probable matches need a review)"
        if ok and is_blocked(con, key, [tag_title, file_name]):
            ok, why = False, "this download was marked wrong in review"
        if ok and heard.verdict == "other":
            ok, why = False, f"{why}, but the {heard.detail}"
        if not ok:
            far = bool(dur and length) and not 2 / 3 <= dur / length <= 1.5  # another song: nothing to review
            kept = drop(src) if why.startswith("artist ") or far else keep(paths, src, want.artist, want.title, source)
            event(con, paths, "wrong-song", kept, reason=why, **info)
            return "wrong-song", None
        if dur and length and not same_length(dur, length):
            near = 2 / 3 <= dur / length <= 1.5
            kept = keep(paths, src, want.artist, want.title, source) if near else drop(src)
            event(con, paths, "mismatch", kept, wanted_seconds=round(length), **info)
            return "mismatch", None
        info["reason"] = why
    if match:
        info["matched"] = match
    length = dur or length
    genuine = ext in audio.LOSSLESS and not fake
    with LOCK:
        cat = catalog.Catalog.from_db(con)
        same = cat.song(want.artist, want.title, length, want.artists, song_link(con, key))
        if same:
            best = same[0]
            if not (genuine and not best.genuine):
                src.unlink(missing_ok=True)
                event(con, paths, "duplicate", paths.tracks / best.path, **info)
                return "duplicate", paths.tracks / best.path
            # genuine lossless takes over: the existing name, every non-genuine copy retired
            losers = [e for e in same if not e.genuine]
            best_path = paths.tracks / best.path
            dest = best_path.with_suffix("." + ext)
            if dest.exists() and dest not in [paths.tracks / e.path for e in losers]:
                dest = _free_name(best_path.parent, best_path.stem, ext, length)
            for e in losers:
                if paths.tracks / e.path == dest:
                    retire(con, paths, e, "replaced by genuine lossless")
            _place(src, dest)
            _add_file(con, paths, dest, fake)
            for e in losers:
                if paths.tracks / e.path != dest and (paths.tracks / e.path).exists():
                    retire(con, paths, e, "replaced by genuine lossless")
            event(con, paths, "upgrade", dest, **info)
            return "upgrade", dest
        folder = artist_dir(paths, cat, want.artist)
        label = "SoundCloud" if source == "soundcloud" else ""  # a SoundCloud like next to its Spotify version
        dest = _free_name(folder, f"{folder.name} - {clean_name(want.title)}", ext, length, label)
        _place(src, dest)
        _add_file(con, paths, dest, fake)
        event(con, paths, "new", dest, **{"fake": int(fake), **info})
        return "new", dest


def purge(paths: Paths, days: int = KEEP_DAYS) -> list[str]:
    """Delete the days of inbox/replaced/ and inbox/review/ older than `days`."""
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    gone = []
    for base in (paths.inbox("replaced"), paths.inbox("review")):
        for d in base.iterdir() if base.exists() else []:
            try:
                day = datetime.date.fromisoformat(d.name)
            except ValueError:
                continue
            if day < cutoff and d.is_dir():
                shutil.rmtree(d)
                gone.append(event_path(paths, d))
    return gone
