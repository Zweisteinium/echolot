"""Which releases of the library's Spotify songs Discogs lacks, and what to submit for them: a list's releases
(by the songs' album and first artist) looked up on Discogs, one search each; a release it lists as a download
(format File) is done. For the others the release on Beatport, a download store as Discogs asks for one (it
takes no streaming-only release), is read for the suggestion: label, catalogue number, UPC, tracks. Stored in
`discogs`, the Discogs page lists them. Only when Discogs is on (settings section discogs, a token in the
vault), never on a schedule and not on the Jobs page: the page's Check asks for one list's releases never looked
at (or all of them again). Read only: nothing is submitted anywhere."""

import datetime
import json
import logging
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from echolot import db
from echolot.services import beatport, discogs, navidrome
from echolot.settings import options

if TYPE_CHECKING:
    from echolot.jobs.worker import Run

log = logging.getLogger(__name__)
REQUEST = "discogs_request"  # meta: what the Discogs page asked to check, JSON {"list": key, "again": bool}
SONGS = (
    "SELECT s.key, s.artist, s.artists, s.album, s.released, s.tracks, s.file, l.key AS list_key, "
    "l.title AS list_title FROM songs s JOIN list_songs ls ON ls.song_key = s.key JOIN lists l ON l.key = ls.list_key "
    "WHERE s.service = 'spotify' AND s.file IS NOT NULL AND s.album > ''"
)
VERSION = re.compile(  # what a store adds to a title and Discogs leaves out (or the other way round)
    r"\s*[(\[](?:original|extended|radio|club)(?:\s+(?:mix|edit|version))?[)\]]"
    r"|\s+-\s+(?:original|extended)\s+mix$|\s+-?\s*\b(?:ep|single)$",
    re.I,
)


@dataclass
class Album:
    """A release of library songs: the album Spotify names, by the songs' first artist."""

    key: str
    artist: str
    title: str
    released: str = ""
    tracks: int = 0  # on the release, as Spotify counts them
    artists: list[str] = field(default_factory=list)  # every artist of its songs
    songs: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    lists: dict[str, str] = field(default_factory=dict)  # key -> title


def words(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def _core(text: str) -> str:
    return words(VERSION.sub("", text))


def same_title(a: str, b: str) -> str:
    """'exact' when two titles are the same but for case and punctuation, 'close' when only a store's
    additions differ (Extended Mix, EP), else ''."""
    if words(a) and words(a) == words(b):
        return "exact"
    return "close" if _core(a) and _core(a) == _core(b) else ""


def album_key(artist: str, title: str) -> str:
    return f"{words(artist)}|{words(title)}"


def albums(con: sqlite3.Connection, list_key: str | None = None, uid: int | None = None) -> dict[str, Album]:
    """The releases of the library songs (with a file) of a list, or of all lists (a user's: those they
    follow), by key."""
    sql, args = SONGS, []
    if list_key:
        sql, args = sql + " AND l.key = ?", [list_key]
    if uid is not None:
        sql += " AND l.key IN (SELECT key FROM sources WHERE user_id = ? AND enabled)"
        args.append(uid)
    out: dict[str, Album] = {}
    for r in con.execute(sql + " ORDER BY s.artist COLLATE NOCASE, s.album COLLATE NOCASE", args):
        key = album_key(r["artist"], r["album"])
        a = out.setdefault(key, Album(key, r["artist"], r["album"], r["released"] or "", r["tracks"] or 0))
        if r["key"] not in a.songs:
            a.songs.append(r["key"])
            a.files.append(r["file"])
            a.artists += [n for n in json.loads(r["artists"] or "[]") or [r["artist"]] if n not in a.artists]
        a.lists[r["list_key"]] = r["list_title"]
    return out


def _by(names: list[str], others: list[str]) -> bool:
    """Whether two artist lists share a name."""
    return bool({words(n) for n in names} & {words(n) for n in others} - {""})


def match(a: Album, releases: list[discogs.Release]) -> tuple[str, dict[str, Any] | None]:
    """(listed, the download release) when Discogs has the album as a download (format File); (digital, a
    release of it) when only in other formats (vinyl, CD); (missing, None) when not at all. Same title, an
    artist of its songs among the release's (or Various), the exact title first."""
    fits = []
    for r in releases:
        how = same_title(a.title, r.title)
        if how and (r.artists.lower() in ("various", "various artists") or _by(a.artists, discogs.names(r.artists))):
            fits.append((how != "exact", not r.digital, r))
    if not fits:
        return "missing", None
    close, physical, best = min(fits, key=lambda f: f[:2])
    found = {"id": best.id, "title": f"{best.artists} - {best.title}", "formats": list(best.formats)}
    found |= {"label": best.label, "catno": best.catno, "year": best.year, "url": best.url, "exact": not close}
    return ("digital" if physical else "listed"), found


def in_store(a: Album) -> dict[str, Any]:
    """The album's release on Beatport (beatport.release), {} when Beatport has none: the same title, an
    artist in common, the one with Spotify's number of tracks and date first."""
    fits = []
    for h in beatport.search(f"{a.artist} {a.title}"):
        how = same_title(a.title, str(h.get("release_name") or ""))
        names = [str(x.get("artist_name") or "") for x in h.get("artists") or []]
        if how and _by(a.artists, names):
            date = str(h.get("release_date") or "")[:10]
            fits.append((how != "exact", h.get("track_count") != a.tracks, date != a.released, h["release_id"]))
    return beatport.release(min(fits)[-1]) if fits else {}


def genres(con: sqlite3.Connection, run: "Run") -> dict[str, str]:
    """Library file -> its genre in Navidrome (the first), for the page's genre filter; {} without Navidrome."""
    svc = navidrome.service(con, run.vault)
    if svc is None:
        return {}
    tracks = f"/{run.paths.tracks.name}/"
    out = {}
    try:
        for s in svc.songs():
            path = "/" + (s.get("path") or "")
            if (at := path.find(tracks)) >= 0 and s.get("genre") and not s.get("missing"):
                out[path[at + len(tracks) :]] = str(s["genre"])
    except navidrome.NavidromeError as e:
        log.info("discogs: no genres from Navidrome: %s", e)
    return out


def run(run: "Run") -> str:
    con = run.connect()
    try:
        if not options.get(con, options.Discogs).enabled:
            return "Discogs is off"
        token = run.vault.get(con, discogs.TOKEN)
        ask = json.loads(db.get_meta(con, REQUEST) or "{}")
        known = {r[0] for r in con.execute("SELECT album FROM discogs")}
        todo = [a for a in albums(con, ask.get("list")).values() if ask.get("again") or a.key not in known]
        todo = run.todo(todo, key=lambda a: a.key)
        genre_of = genres(con, run) if todo else {}
    finally:
        con.close()
    if not token:
        return "no Discogs token"
    if not todo:
        return "nothing new"
    counts: Counter[str] = Counter()
    store_ok = True
    for n, a in enumerate(todo, 1):
        if run.stop.is_set() or run.give_way.is_set():
            run.left = 0 if run.stop.is_set() else len(todo) - n + 1
            break
        run.say(f"{run.of(n, len(todo))}: {a.artist} – {a.title}", n - 1, len(todo))
        try:
            state, found = match(a, discogs.search(token, a.artist, a.title))
        except discogs.Refused as e:
            run.note(f"{e}: stopped")
            return f"stopped: {e} ({sum(counts.values())} releases before)"
        except discogs.DiscogsError as e:
            run.note(f"{a.artist} – {a.title}: {e}")
            continue
        store = None  # (not looked)
        if state != "listed" and store_ok:
            try:
                store = in_store(a)
            except beatport.Refused as e:
                store_ok = False
                run.note(f"{e}: Beatport is not asked again in this run")
            except beatport.BeatportError as e:
                run.note(f"{a.artist} – {a.title}: {e}")
        top = Counter(g for f in a.files if (g := genre_of.get(f))).most_common(1)
        con = run.connect()
        try:
            _store(con, a, state, found, store, top[0][0] if top else "")
        finally:
            con.close()
        run.handled.add(a.key)
        counts[state] += 1
    done = f"{sum(counts.values())} releases: {counts['listed']} on Discogs, "
    done += f"{counts['digital']} only in other formats, {counts['missing']} missing"
    return done + (f"; gave way, {run.left} left" if run.left else "")


def _store(con: sqlite3.Connection, a: Album, state: str, found: dict | None, store: dict | None, genre: str) -> None:
    now = datetime.datetime.now().isoformat(timespec="seconds")
    store_json = json.dumps(store) if store is not None else None
    with con:
        con.execute(
            "INSERT INTO discogs (album, artist, title, state, found, store, genre, checked) VALUES (?, ?, ?, ?, ?, ?, "
            "?, ?) ON CONFLICT (album) DO UPDATE SET artist = excluded.artist, title = excluded.title, state = "
            "excluded.state, found = excluded.found, store = coalesce(excluded.store, discogs.store), genre = "
            "excluded.genre, checked = excluded.checked",  # (a store not looked at this time keeps the last one)
            (a.key, a.artist, a.title, state, json.dumps(found) if found else None, store_json, genre, now),
        )
