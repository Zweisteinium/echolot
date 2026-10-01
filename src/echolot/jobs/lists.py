"""The followed lists as they are at their source: Spotify lists through the API, SoundCloud lists and
their downloads through yt-dlp. A list that can't be read keeps its last known state.

Songs are never forgotten (their links, attempts and SoundCloud downloads stay); list_songs holds what a
list has now, list_history what it ever had (for its "– removed" playlist).
"""

import datetime
import json
import logging
import re
import shutil
import sqlite3
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

from echolot.jobs.acquire import finish
from echolot.library import audio, filing, rules
from echolot.library.filing import Want
from echolot.services import spotify, ytdlp
from echolot.settings import sources
from echolot.settings.sources import Source

if TYPE_CHECKING:
    from echolot.jobs.worker import Run

log = logging.getLogger(__name__)
GREYED_OUT = "greyed out on Spotify"
NOT_ON_SOUNDCLOUD = "not downloadable from SoundCloud"


def sync_table(con: sqlite3.Connection) -> None:
    """The lists table follows the sources: a new source gets a row ('waiting for the first listing'),
    a removed one loses its row and current songs (its history stays), names and flags follow."""
    srcs = sources.lists(con)
    keys = [s.key for s in srcs]
    with con:
        for n, s in enumerate(srcs):
            con.execute(
                "INSERT INTO lists (key, service, title, url, position, playlist, fetched) "
                "VALUES (?, ?, ?, ?, ?, ?, 0) ON CONFLICT (key) DO UPDATE SET position = excluded.position, "
                "playlist = excluded.playlist, url = excluded.url",
                (s.key, s.service, s.title or _default_title(s), s.url, n, int(s.playlist)),
            )
            if s.title:
                con.execute("UPDATE lists SET title = ? WHERE key = ?", (s.title, s.key))
        marks = ", ".join("?" * len(keys))
        con.execute(f"DELETE FROM lists WHERE key NOT IN ({marks})", keys)


def _default_title(s: Source) -> str:
    if s.name == "Spotify Liked Songs":
        return "Liked Songs"
    return "SoundCloud Likes" if s.name == "SoundCloud Likes" else s.url


def _store(con: sqlite3.Connection, s: Source, ids: list[str], title: str, cover: str | None,
           snapshot: str | None = None) -> None:  # fmt: skip
    """A list's current songs (keys in order; songs not yet known are left out) and its history."""
    today = datetime.date.today().isoformat()
    known = {r[0] for r in con.execute(f"SELECT key FROM songs WHERE key IN ({', '.join('?' * len(ids))})", ids)}
    with con:
        con.execute("DELETE FROM list_songs WHERE list_key = ?", (s.key,))
        con.executemany("INSERT INTO list_songs (list_key, position, song_key) VALUES (?, ?, ?)",
                        [(s.key, n, k) for n, k in enumerate(k for k in ids if k in known)])  # fmt: skip
        con.executemany(
            "INSERT INTO list_history (list_key, song_key, first_seen, last_seen) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (list_key, song_key) DO UPDATE SET last_seen = excluded.last_seen",
            [(s.key, k, today, today) for k in ids if k in known],
        )
        con.execute(
            "UPDATE lists SET title = ?, cover_url = coalesce(?, cover_url), snapshot = coalesce(?, snapshot), "
            "fetched = 1, fetched_at = ? WHERE key = ?",
            (s.title or title, cover, snapshot, datetime.datetime.now().isoformat(timespec="seconds"), s.key),
        )


def _song(con: sqlite3.Connection, key: str, service: str, **meta: Any) -> bool:
    """Insert or update a song; a value the source blanked keeps the known one (Spotify can blank the
    name of a song it removed while it stays in your list). False if it has no name at all."""
    old = con.execute("SELECT * FROM songs WHERE key = ?", (key,)).fetchone()
    cur = {k: meta.get(k) or (old[k] if old else None) for k in ("artist", "title", "album", "length",
                                                                   "artists", "isrc", "url")}  # fmt: skip
    if not cur["artist"] or not cur["title"]:
        return False
    con.execute(
        "INSERT INTO songs (key, service, artist, title, album, length, artists, isrc, url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET artist = excluded.artist, "
        "title = excluded.title, album = excluded.album, length = excluded.length, "
        "artists = excluded.artists, isrc = excluded.isrc, url = excluded.url",
        (key, service, cur["artist"], cur["title"], cur["album"] or "", float(cur["length"] or 0),
         cur["artists"], cur["isrc"], cur["url"]),
    )  # fmt: skip
    return True


# ---------------------------------------------------------------- Spotify


def fetch_spotify(run: "Run") -> str:
    """Read every followed Spotify list (a playlist whose snapshot did not change is not read again)."""
    con = run.connect()
    try:
        sync_table(con)
        try:
            sp = spotify.Spotify(con, run.vault)
        except spotify.SpotifyError as e:
            return str(e)
        done, failed = 0, []
        for s in sources.lists(con):
            if s.service != "spotify":
                continue
            run.say(f"Spotify: {s.title or s.url}")
            try:
                _fetch_spotify_list(con, sp, s)
                done += 1
            except spotify.SpotifyError as e:
                log.warning("spotify %s: %s (keeping the last listing)", s.key, e)
                failed.append(f"{s.title or s.key}: {e}")
        return f"Spotify: {done} lists read" + (f", failed: {'; '.join(failed)}" if failed else "")
    finally:
        con.close()


def _fetch_spotify_list(con: sqlite3.Connection, sp: spotify.Spotify, s: Source) -> None:
    pid = spotify.playlist_id(s.url) if s.name != "Spotify Liked Songs" else None
    if pid is None:
        title, cover, snapshot = "Liked Songs", spotify.LIKED_SONGS_IMAGE, None
    else:
        meta = sp.playlist(pid)
        title, cover, snapshot = meta["name"], meta["image"], meta["snapshot"]
        row = con.execute("SELECT snapshot, fetched FROM lists WHERE key = ?", (s.key,)).fetchone()
        if row and row["fetched"] and snapshot and row["snapshot"] == snapshot:  # unchanged
            with con:
                con.execute("UPDATE lists SET fetched_at = ?, title = ? WHERE key = ?",
                            (datetime.datetime.now().isoformat(timespec="seconds"), s.title or title, s.key))  # fmt: skip
            return
    items = sp.items(pid)
    if not items and con.execute("SELECT 1 FROM list_songs WHERE list_key = ?", (s.key,)).fetchone():
        raise spotify.SpotifyError("no songs listed although it had some (Spotify may withhold others' playlists)")
    with con:
        for it in items:
            _song(con, f"spotify:{it['id']}", "spotify", artist=it["artist"], title=it["title"],
                  album=it["album"], length=it["length"], artists=json.dumps(it["artists"]), isrc=it["isrc"])  # fmt: skip
    if pid is None:  # the likes tell which songs Spotify greys out
        grey = sp.unplayable_liked()
        with con:
            con.execute("UPDATE songs SET unavailable = NULL WHERE service = 'spotify' AND unavailable = ?",
                        (GREYED_OUT,))  # fmt: skip
            con.executemany("UPDATE songs SET unavailable = ? WHERE key = ?",
                            [(GREYED_OUT, f"spotify:{i}") for i in grey])  # fmt: skip
    _store(con, s, [f"spotify:{it['id']}" for it in items], title, cover, snapshot)


# ---------------------------------------------------------------- SoundCloud


# SoundCloud songs not to download: downloaded and still in the library (a file that left it, e.g.
# retired in review, is fetched again), or handed out to nobody
SC_HAVE = (
    "SELECT substr(key, 12) FROM songs WHERE service = 'soundcloud' "
    "AND (archived AND file IS NOT NULL OR unavailable IS NOT NULL)"
)


def soundcloud(run: "Run") -> str:
    """Read the followed SoundCloud lists, download their new songs (originals kept lossless, streams
    as they are) and file them. A track SoundCloud hands out to nobody (label releases) is marked and
    left to the search fallback."""
    con = run.connect()
    try:
        sync_table(con)
        token = run.vault.get(con, "soundcloud.token")
        srcs = [s for s in sources.lists(con) if s.service == "soundcloud"]
    finally:
        con.close()
    if not srcs:
        return "no SoundCloud lists"
    work = run.paths.inbox("soundcloud")
    shutil.rmtree(work, ignore_errors=True)  # what an interrupted run left
    ydl = ytdlp.YtDlp(run.data / "ytdlp", token)
    listed: dict[str, list[tuple[str, str]]] = {}
    for s in srcs:
        run.say(f"SoundCloud: listing {s.title or s.url}")
        tracks, info = ydl.listing(s.url, run.stop)
        if tracks is None:
            continue  # keep the last listing
        listed[s.key] = tracks
        con = run.connect()
        try:
            title, cover = _sc_meta(s, info)
            with con:
                con.execute("UPDATE lists SET title = ?, cover_url = coalesce(?, cover_url) WHERE key = ?",
                            (s.title or title, cover, s.key))  # fmt: skip
        finally:
            con.close()
    urls = {tid: url for tracks in listed.values() for tid, url in tracks}
    _sc_pages(run, ydl, urls)
    con = run.connect()
    try:
        have = {r[0] for r in con.execute(SC_HAVE)}
    finally:
        con.close()
    new = [(tid, url) for tid, url in urls.items() if tid not in have and url]
    added = 0
    if new and not run.stop.is_set():
        run.say(f"SoundCloud: downloading {len(new)} new songs")
        for d in ydl.download(new, work, run.stop):
            added += _file_sc(run, d, urls[d["id"]], work)
        con = run.connect()
        try:
            for tid, url in new:  # not downloaded: SoundCloud's protected releases, or a hiccup (next run)
                if con.execute("SELECT 1 FROM songs WHERE key = ? AND archived = 1", (f"soundcloud:{tid}",)).fetchone():
                    continue
                meta = ydl.meta(url, run.stop)
                if meta and not meta.get("formats"):
                    artist, title = _names(con, meta.get("uploader") or "", meta.get("artist") or "",
                                           meta.get("title") or "")  # fmt: skip
                    with con:
                        if _song(con, f"soundcloud:{tid}", "soundcloud", artist=artist, title=title,
                                 length=meta.get("duration") or 0, url=url):  # fmt: skip
                            con.execute("UPDATE songs SET unavailable = ? WHERE key = ?",
                                        (NOT_ON_SOUNDCLOUD, f"soundcloud:{tid}"))  # fmt: skip
        finally:
            con.close()
    con = run.connect()
    try:
        for s in srcs:
            if s.key in listed:
                row = con.execute("SELECT title FROM lists WHERE key = ?", (s.key,)).fetchone()
                _store(con, s, [f"soundcloud:{tid}" for tid, _ in listed[s.key]], row["title"], None)
    finally:
        con.close()
    shutil.rmtree(work, ignore_errors=True)
    run.after.add("library")
    return f"SoundCloud: {len(listed)} of {len(srcs)} lists read, {added} new files"


SC_PAGE = "https://soundcloud.com/"


def _sc_pages(run: "Run", ydl: ytdlp.YtDlp, urls: dict[str, str]) -> None:
    """Store the track pages of the listed songs (songs.url; the web pages link to them). A set lists only
    its first tracks in full, the others by API address: their page is asked for once."""
    con = run.connect()
    try:
        known = dict(con.execute("SELECT substr(key, 12), coalesce(url, '') FROM songs WHERE service = 'soundcloud'"))
        pages = {tid: url for tid, url in urls.items() if url.startswith(SC_PAGE)}
        for tid, url in urls.items():
            if tid in pages or known.get(tid, SC_PAGE).startswith(SC_PAGE) or run.stop.is_set():
                continue  # a page, a page known already, or a song not stored yet (its download stores it)
            if (page := (ydl.meta(url, run.stop) or {}).get("webpage_url") or "").startswith(SC_PAGE):
                pages[tid] = page
        with con:
            rows = [(page, f"soundcloud:{tid}", page) for tid, page in pages.items()]
            con.executemany("UPDATE songs SET url = ? WHERE key = ? AND url IS NOT ?", rows)
    finally:
        con.close()


def _names(con: sqlite3.Connection, uploader: str, artist: str, title: str) -> tuple[str, str]:
    """A SoundCloud track's artist and title (ytdlp.artist_title), turned round when the title is an artist
    of your Spotify songs and the artist is not: "Outside (Hardstyle) - crypvolk", uploaded by neither."""
    a, t = ytdlp.artist_title(uploader, artist, title)
    known = {
        rules.artist_key(name)
        for r in con.execute("SELECT artist, artists FROM songs WHERE service = 'spotify'")
        for name in [r["artist"], *json.loads(r["artists"] or "[]")]
        if name
    }
    if rules.artist_key(t) in known and rules.artist_key(a) not in known:
        return t, a
    return a, t


def _file_sc(run: "Run", d: dict[str, str], url: str, work: Path) -> int:
    """File one SoundCloud download (its title often holds the artist: _names)."""
    key = f"soundcloud:{d['id']}"
    try:
        prepared = audio.prepare(Path(d["path"]))
    except audio.Rejected as e:
        log.warning("soundcloud %s: %s", key, e)
        return 0
    length = float(d["duration"] or 0) if d["duration"] not in ("NA", "") else 0
    con = run.connect()
    try:
        artist, title = _names(con, d["uploader"], d["artist"], d["title"])
        want = Want(artist, title, length, key)
        with con:
            _song(con, key, "soundcloud", artist=artist, title=title, length=length, url=url)
        page = url if url.startswith("https://soundcloud.com/") else ""  # a set lists some tracks by API address
        action, dest = filing.file_into(con, run.paths, prepared.path, want, "soundcloud", fake=prepared.fake, url=page)
        if dest is None:
            return 0
        stem = dest.relative_to(run.paths.tracks).with_suffix("").as_posix()
        with con:
            con.execute("UPDATE songs SET stem = ?, archived = 1, unavailable = NULL WHERE key = ?", (stem, key))
        if action == "duplicate":
            return 0
        finish(run, con, dest, want, cover=work / f"{d['id']}.jpg")  # the tags from the song, the cover
        return 1
    finally:
        con.close()


def _sc_meta(s: Source, info: dict[str, Any]) -> tuple[str, str | None]:
    """Name and cover of a SoundCloud list: a set keeps its own title and artwork; likes get
    'SoundCloud Likes' and the profile picture (SoundCloud has no likes cover)."""
    if s.url.rstrip("/").endswith("/likes"):
        user = s.url.rstrip("/").rsplit("/", 2)[-2]
        title = "SoundCloud Likes" if s.name == "SoundCloud Likes" else f"{user} – SoundCloud Likes"
        try:
            req = urllib.request.Request(s.url.rsplit("/likes", 1)[0], headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                page = r.read().decode("utf-8", "replace")
            m = re.search(r"https://i1\.sndcdn\.com/avatars-[^\"'\s]+?-t500x500\.(?:jpg|png)", page)
            return title, m.group(0) if m else None
        except OSError:
            return title, None
    thumbs = [t.get("url") for t in info.get("thumbnails") or [] if t.get("url")]
    cover = None
    if thumbs:
        cover = re.sub(r"-(?:mini|tiny|small|badge|t67x67|large|t300x300|crop|t500x500|original)\.(jpg|png)$",
                       "-t500x500.jpg", thumbs[-1])  # fmt: skip
    return info.get("title") or info.get("album") or s.url, cover
