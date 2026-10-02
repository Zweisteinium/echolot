"""The followed lists as they are at their source: Spotify lists through the API, SoundCloud lists and
their downloads through yt-dlp. A list that can't be read keeps its last known state.

Songs are never forgotten (their links, attempts and SoundCloud downloads stay); list_songs holds what a
list has now, list_history what it ever had; the songs coming and going are recorded as changes
(jobs/availability).
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

from echolot.jobs import availability
from echolot.jobs.acquire import finish
from echolot.library import audio, catalog, filing, recordings, rules, tagging
from echolot.library.filing import Want
from echolot.services import soundcloud as sc_api
from echolot.services import spotify, ytdlp
from echolot.settings import sources
from echolot.settings.sources import Source

if TYPE_CHECKING:
    from echolot.jobs.worker import Run
    from echolot.settings.vault import Vault

log = logging.getLogger(__name__)
GREYED_OUT = availability.GREYED_OUT
NOT_ON_SOUNDCLOUD = "not downloadable from SoundCloud"


def sync_table(con: sqlite3.Connection) -> None:
    """The lists table follows the sources: a new source gets a row ('waiting for the first listing'),
    a removed one loses its row and current songs (its history stays), names and flags follow."""
    srcs = sources.followed(con)  # once each, as its oldest follower has it
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
    """A list's current songs (keys in order; songs not yet known are left out), its history, and what
    changed since its last reading (availability.record_list)."""
    today = datetime.date.today().isoformat()
    known = {r[0] for r in con.execute(f"SELECT key FROM songs WHERE key IN ({', '.join('?' * len(ids))})", ids)}
    sql = "SELECT song_key FROM list_songs WHERE list_key = ? ORDER BY position"
    before = [r[0] for r in con.execute(sql, (s.key,))]
    state = con.execute("SELECT fetched FROM lists WHERE key = ?", (s.key,)).fetchone()
    first = not (state and state["fetched"])  # its songs were there before Echolot knew the list
    with con:
        availability.record_list(con, s.key, before, [k for k in ids if k in known], first)
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
    """Read the followed Spotify lists that changed, each user's with their own login: what changed is
    asked first (the snapshots of the playlists in their library and the state of their likes, a few
    requests), so a run without changes costs next to nothing. A list two users follow is read once; one
    whose follower is not connected is read with another follower's login. Which songs Spotify greys
    out, the daily availability check finds (every list's)."""
    con = run.connect()
    try:
        sync_table(con)
        done, read, failed, seen = 0, 0, [], set()
        for uid in _active(con):
            mine = [s for s in sources.user_lists(con, uid) if s.service == "spotify" and s.key not in seen]
            if not mine:
                continue
            try:
                sp = spotify.Spotify(con, run.vault, uid)
                known, likes = sp.snapshots(), sp.likes_state()
            except spotify.SpotifyError as e:
                log.info("spotify lists of user %s: %s", uid, e)
                continue  # another follower may have a login
            for s in mine:
                run.say(f"reading {s.title or s.url}")
                try:
                    if _fetch_spotify_list(con, sp, s, known, likes):
                        read += 1
                        run.note(f"Spotify: {s.title or s.url} changed, read again")
                    done += 1
                    seen.add(s.key)
                    availability.list_readable(con, s.key, True)
                except spotify.SpotifyError as e:
                    log.warning("spotify %s: %s (keeping the last listing)", s.key, e)
                    failed.append(f"{s.title or s.key}: {e}")
                    availability.list_readable(con, s.key, False, str(e))
        message = f"{done} lists, {read} changed" + (f", failed: {'; '.join(failed)}" if failed else "")
        unread = sum(1 for s in sources.followed(con) if s.service == "spotify" and s.key not in seen) - len(failed)
        return message + (f", {unread} not read (no follower connected to Spotify)" if unread > 0 else "")
    finally:
        con.close()


def _active(con: sqlite3.Connection) -> list[int]:
    """The users whose lists are read, the oldest first (not the lists nobody owns yet, nor those of an
    account gone from Navidrome: theirs keep the last listing)."""
    sql = "SELECT id FROM users WHERE NOT disabled AND id IN (SELECT user_id FROM sources) ORDER BY id"
    return [r[0] for r in con.execute(sql)]


def _fetch_spotify_list(
    con: sqlite3.Connection, sp: spotify.Spotify, s: Source, known: dict[str, str], likes: str
) -> bool:
    """Read one list if it changed (its snapshot, or the likes' state); True if it was read."""
    pid = spotify.playlist_id(s.url) if s.name != "Spotify Liked Songs" else None
    snapshot = likes if pid is None else known.get(pid) or sp.playlist(pid)["snapshot"]  # not in the library
    row = con.execute("SELECT snapshot, fetched FROM lists WHERE key = ?", (s.key,)).fetchone()
    if row and row["fetched"] and snapshot and row["snapshot"] == snapshot:  # unchanged
        with con:
            now = datetime.datetime.now().isoformat(timespec="seconds")
            con.execute("UPDATE lists SET fetched_at = ? WHERE key = ?", (now, s.key))
        return False
    if pid is None:
        title, cover = "Liked Songs", spotify.LIKED_SONGS_IMAGE
    else:
        meta = sp.playlist(pid)
        title, cover = meta["name"], meta["image"]
    items = sp.items(pid)
    if not items and con.execute("SELECT 1 FROM list_songs WHERE list_key = ?", (s.key,)).fetchone():
        raise spotify.SpotifyError("no songs listed although it had some (Spotify may withhold others' playlists)")
    with con:
        for it in items:
            _song(con, f"spotify:{it['id']}", "spotify", artist=it["artist"], title=it["title"],
                  album=it["album"], length=it["length"], artists=json.dumps(it["artists"]), isrc=it["isrc"])  # fmt: skip
    _store(con, s, [f"spotify:{it['id']}" for it in items], title, cover, snapshot)
    return True


# ---------------------------------------------------------------- SoundCloud


# SoundCloud songs not to download: downloaded and still in the library (a file that left it, e.g.
# retired in review, is fetched again), or handed out to nobody
SC_HAVE = (
    "SELECT substr(key, 12) FROM songs WHERE service = 'soundcloud' "
    "AND (archived AND file IS NOT NULL OR unavailable IS NOT NULL)"
)


def soundcloud(run: "Run") -> str:
    """Read the followed SoundCloud lists that changed (sc_api.states, three requests; each list at least
    hourly), download their new songs (originals kept lossless, streams as they are) and file them. A track
    SoundCloud hands out to nobody (label releases) is marked and left to the search fallback."""
    con = run.connect()
    try:
        sync_table(con)
        srcs = [s for s in sources.followed(con) if s.service == "soundcloud"]
        if not srcs:
            return "no SoundCloud lists"
        tokens = {s.key: _sc_token(con, run.vault, s.key) for s in srcs}  # a follower's own (a private set)
        states: dict[str, str] = {}
        for token in dict.fromkeys(t for t in tokens.values() if t):
            try:
                states |= sc_api.states(token)
            except sc_api.SoundCloudError as e:
                log.info("soundcloud states: %s (reading that account's lists)", e)
        total, srcs = len(srcs), [s for s in srcs if _sc_changed(con, s, states)]
        any_token = sc_api.any_token(con, run.vault)
    finally:
        con.close()
    if not srcs:
        return f"{total} lists, 0 changed"
    work = run.paths.inbox("soundcloud")
    shutil.rmtree(work, ignore_errors=True)  # what an interrupted run left
    ydls: dict[str | None, ytdlp.YtDlp] = {}

    def ydl_of(token: str | None) -> ytdlp.YtDlp:
        return ydls.setdefault(token, ytdlp.YtDlp(run.data / "ytdlp", token))

    ydl = ydl_of(any_token)  # downloads: SoundCloud hands a song to any account
    listed: dict[str, list[tuple[str, str]]] = {}
    for s in srcs:
        run.say(f"reading {s.title or s.url}")
        tracks, info = ydl_of(tokens[s.key] or any_token).listing(s.url, run.stop)
        if tracks is None:
            if not run.stop.is_set():
                _readable(run, s.key, False, "SoundCloud did not list it")
            continue  # keep the last listing
        _readable(run, s.key, True)
        listed[s.key] = tracks
        run.note(f"SoundCloud: {s.title or s.url} read, {len(tracks)} songs")
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
        run.say(f"downloading {len(new)} new songs")
        for d in ydl.download(new, work, run.stop):
            filed = _file_sc(run, d, urls[d["id"]], work)
            added += filed
            run.note(
                f"SoundCloud: {d.get('uploader') or ''} – {d.get('title') or d['id']}: {'new' if filed else 'not filed'}"
            )
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
                _store(con, s, [f"soundcloud:{tid}" for tid, _ in listed[s.key]], row["title"], None, states.get(s.url))
    finally:
        con.close()
    shutil.rmtree(work, ignore_errors=True)
    run.after.add("library")
    unread = f", {len(srcs) - len(listed)} not read" if len(listed) < len(srcs) else ""
    return f"{total} lists, {len(srcs)} changed{unread}; {added} new songs"


def _readable(run: "Run", key: str, readable: bool, why: str = "") -> None:
    con = run.connect()
    try:
        availability.list_readable(con, key, readable, why)
    finally:
        con.close()


def _sc_token(con: sqlite3.Connection, vault: "Vault", key: str) -> str | None:
    """The SoundCloud token of a list's oldest follower who has one (who may read it if it is private)."""
    return next((t for uid in sources.followers(con, key) if (t := sc_api.token_of(con, vault, uid))), None)


def _sc_changed(con: sqlite3.Connection, s: Source, states: dict[str, str]) -> bool:
    """A SoundCloud list to read: its state changed or is unknown, or it was last read an hour ago (a
    download that failed is tried again; it leaves no trace in the list's state)."""
    row = con.execute("SELECT snapshot, fetched, fetched_at FROM lists WHERE key = ?", (s.key,)).fetchone()
    state = states.get(s.url)
    if not (row and row["fetched"] and state and row["snapshot"] == state and row["fetched_at"]):
        return True
    return datetime.datetime.fromisoformat(row["fetched_at"]) < datetime.datetime.now() - datetime.timedelta(hours=1)


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
    """A SoundCloud track's artist and title (ytdlp.artist_title) without release decoration
    (tagging.clean_title: "[Free DL]", "( deleting soon save it on spotify )"), turned round when the title
    is an artist of your Spotify songs and the artist is not: "Outside (Hardstyle) - crypvolk", uploaded by
    neither."""
    a, t = ytdlp.artist_title(uploader, artist, title)
    t = tagging.clean_title(t, a)
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
        index = recordings.Index(catalog.Catalog.from_db(con))
        if same := recordings.already(run.paths, prepared.path, title, length or audio.probe(prepared.path)[0], index):
            genuine = prepared.path.suffix.lower().lstrip(".") in audio.LOSSLESS and not prepared.fake
            if not genuine or same.genuine:  # the library has it under other names: linked, not filed twice
                prepared.path.unlink(missing_ok=True)
                with con:
                    con.execute("UPDATE songs SET archived = 1, unavailable = NULL WHERE key = ?", (key,))
                recordings.link(con, run.paths, key, want, same, "the same audio")
                return 0
            want = Want(same.path.partition("/")[0], same.title, same.duration, key)  # a FLAC takes over its name
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
