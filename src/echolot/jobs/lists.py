"""The followed lists as they are at their source: Spotify lists through the API, SoundCloud lists and
their downloads through yt-dlp, YouTube playlists through YouTube Music and yt-dlp. A list that can't be
read keeps its last known state.

Songs are never forgotten (their links, attempts and SoundCloud downloads stay); list_songs holds what a
list has now, list_history what it ever had; the songs coming and going are recorded as changes
(jobs/availability).
"""

import datetime
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

from echolot import db
from echolot.jobs import availability
from echolot.jobs.acquire import finish
from echolot.library import audio, catalog, filing, recordings, rules, tagging
from echolot.library.filing import Want
from echolot.services import soundcloud as sc_api
from echolot.services import spotify, ytdlp
from echolot.services import youtube as youtube_api
from echolot.settings import options, sources
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
           snapshot: str | None = None, creator: str | None = None) -> None:  # fmt: skip
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
            "fetched = 1, fetched_at = ?, creator = coalesce(?, creator) WHERE key = ?",
            (s.title or title, cover, snapshot, datetime.datetime.now().isoformat(timespec="seconds"), creator, s.key),
        )


def _song(con: sqlite3.Connection, key: str, service: str, **meta: Any) -> bool:
    """Insert or update a song; a value the source blanked keeps the known one (Spotify can blank the
    name of a song it removed while it stays in your list). False if it has no name at all."""
    old = con.execute("SELECT * FROM songs WHERE key = ?", (key,)).fetchone()
    if old and _blanked(meta.get("artist") or "", meta.get("artists")):  # "." for Timati: the known names stay
        meta = {k: v for k, v in meta.items() if k not in ("artist", "artists")}
    cur = {k: meta.get(k) or (old[k] if old else None) for k in ("artist", "title", "album", "length",
                                                                   "artists", "isrc", "url", *FACTS)}  # fmt: skip
    if not cur["artist"] or not cur["title"]:
        return False
    con.execute(
        "INSERT INTO songs (key, service, artist, title, album, length, artists, isrc, url, released, track, "
        "tracks, disc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET "
        "artist = excluded.artist, title = excluded.title, album = excluded.album, length = excluded.length, "
        "artists = excluded.artists, isrc = excluded.isrc, url = excluded.url, released = excluded.released, "
        "track = excluded.track, tracks = excluded.tracks, disc = excluded.disc, "
        "artist_alias = CASE WHEN excluded.artist = songs.artist THEN songs.artist_alias END",  # (renamed: asked again)
        (key, service, cur["artist"], cur["title"], cur["album"] or "", float(cur["length"] or 0),
         cur["artists"], cur["isrc"], cur["url"], *(cur[k] for k in FACTS)),
    )  # fmt: skip
    return True


FACTS = ("released", "track", "tracks", "disc")  # spotify.release_facts


# ---------------------------------------------------------------- Spotify


def fetch_spotify(run: "Run") -> str:
    """Read the followed Spotify lists that changed, each user's with their own login: what changed is
    asked first (the snapshots of the playlists in their library and the state of their likes, a few
    requests), so a run without changes costs next to nothing. A list two users follow is read once; one
    whose follower is not connected is read with another follower's login, and a public playlist none of its
    followers connected to Spotify with Echolot's own app (no login; likes need their owner's). Which songs
    Spotify greys out, the daily availability check finds (every list's)."""
    con = run.connect()
    try:
        sync_table(con)
        done, read, failed, seen, tried = 0, 0, [], set(), set()

        def read_list(sp: spotify.Spotify, s: sources.Source, known: dict[str, str], likes: str | None) -> None:
            nonlocal done, read
            run.say(f"reading {s.title or s.url}")
            tried.add(s.key)
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
                read_list(sp, s, known, likes)
            try:
                if aliased := _aliases(con, sp):
                    run.note(f"Spotify: {aliased} artists' English names (searched as well)")
            except spotify.SpotifyError as e:
                log.info("spotify: English names: %s", e)
        public = [s for s in sources.followed(con) if s.service == "spotify" and s.key not in tried]
        public = [s for s in public if s.name != "Spotify Liked Songs" and spotify.playlist_id(s.url)]
        if public:  # nobody connected follows them: Echolot's app reads public playlists without a login
            try:
                app = spotify.Spotify(con, run.vault, None)
            except spotify.SpotifyError as e:
                log.info("spotify app: %s", e)
            else:
                for s in public:
                    read_list(app, s, {}, None)
        message = f"{done} lists, {read} changed" + (f", failed: {'; '.join(failed)}" if failed else "")
        unread = sum(1 for s in sources.followed(con) if s.service == "spotify" and s.key not in tried)
        return message + (f", {unread} not read (no follower connected to Spotify)" if unread else "")
    finally:
        con.close()


ALIASES_PER_RUN = 50  # songs asked for their artist's English name per run (one request each)


def _blanked(artist: str, artists: str | list | None) -> bool:
    """A name without a letter or digit: Spotify's "." for an artist whose name it withholds (Timati, in
    English; Тимати in Russian), or an empty one."""
    names = json.loads(artists) if isinstance(artists, str) else artists or []
    return any(not re.search(r"\w", n or "") for n in [artist, *names])


def _aliases(con: sqlite3.Connection, sp: spotify.Spotify) -> int:
    """Spotify songs whose artist is written in another script (祖堅 正慶): its English name, if Spotify has
    one that differs (Masayoshi Soken), as songs.artist_alias, searched and matched as well ('' = asked, the
    same). A song whose names Spotify blanked ("."): the English names instead, if they are names. Asked once
    per song; returns how many got a name."""
    rows = con.execute(
        "SELECT key, artist, artists FROM songs WHERE service = 'spotify' AND artist_alias IS NULL"
    ).fetchall()
    found = 0
    for r in [r for r in rows if rules.non_latin(r["artist"]) or _blanked(r["artist"], r["artists"])][:ALIASES_PER_RUN]:
        track = sp.track(r["key"].removeprefix("spotify:"), lang="en")
        names = [(a.get("name") or "").strip() for a in track.get("artists") or []]
        if names and _blanked(r["artist"], r["artists"]) and not _blanked(names[0], names):
            with con:
                con.execute(
                    "UPDATE songs SET artist = ?, artists = ?, artist_alias = '' WHERE key = ?",
                    (names[0], json.dumps(names), r["key"]),
                )
            found += 1
            continue
        alias = names[0] if names and names[0] and names[0] != r["artist"] and not _blanked(names[0], []) else ""
        with con:
            con.execute("UPDATE songs SET artist_alias = ? WHERE key = ?", (alias, r["key"]))
        found += bool(alias)
    return found


def _active(con: sqlite3.Connection) -> list[int]:
    """The users whose lists are read, the oldest first (not the lists nobody owns yet, nor those of an
    account gone from Navidrome: theirs keep the last listing)."""
    sql = "SELECT id FROM users WHERE NOT disabled AND id IN (SELECT user_id FROM sources) ORDER BY id"
    return [r[0] for r in con.execute(sql)]


def _fetch_spotify_list(
    con: sqlite3.Connection, sp: spotify.Spotify, s: Source, known: dict[str, str], likes: str | None
) -> bool:
    """Read one list if it changed (its snapshot, or the likes' state); True if it was read."""
    pid = spotify.playlist_id(s.url) if s.name != "Spotify Liked Songs" else None
    snapshot = likes if pid is None else known.get(pid) or sp.playlist(pid)["snapshot"]  # not in the library
    row = con.execute("SELECT snapshot, fetched, creator FROM lists WHERE key = ?", (s.key,)).fetchone()
    if row and row["fetched"] and snapshot and row["snapshot"] == snapshot:  # unchanged
        creator = sp.playlist(pid).get("owner") if pid and not row["creator"] else None  # (once: who made it)
        with con:
            now = datetime.datetime.now().isoformat(timespec="seconds")
            con.execute(
                "UPDATE lists SET fetched_at = ?, creator = coalesce(?, creator) WHERE key = ?", (now, creator, s.key)
            )
        return False
    creator = None
    if pid is None:
        title, cover = "Liked Songs", spotify.LIKED_SONGS_IMAGE
    else:
        meta = sp.playlist(pid)
        title, cover, creator = meta["name"], meta["image"], meta.get("owner")
    items = sp.items(pid)
    if not items and con.execute("SELECT 1 FROM list_songs WHERE list_key = ?", (s.key,)).fetchone():
        raise spotify.SpotifyError("no songs listed although it had some (Spotify may withhold others' playlists)")
    with con:
        for it in items:
            _song(con, f"spotify:{it['id']}", "spotify", artist=it["artist"], title=it["title"],
                  album=it["album"], length=it["length"], artists=json.dumps(it["artists"]), isrc=it["isrc"],
                  **{k: it.get(k) for k in FACTS})  # fmt: skip
    _store(con, s, [f"spotify:{it['id']}" for it in items], title, cover, snapshot, creator)
    return True


# ---------------------------------------------------------------- YouTube


def youtube(run: "Run") -> str:
    """Read the followed YouTube playlists (public ones, without an account). YouTube tells no change in
    advance, so each list is read: its videos in order through yt-dlp (also those that no longer play),
    their names through YouTube Music (a request per 100 songs). A video that no longer plays stays in its
    list, as gone or blocked (the availability tracker); one taken out of the list is removed. A new song
    gets Spotify's names where Spotify has it (_youtube_song); one never searched starts New songs."""
    from echolot.jobs import acquire

    con = run.connect()
    try:
        sync_table(con)
        srcs = [s for s in sources.followed(con) if s.service == "youtube"]
        if not srcs:
            return "no YouTube lists"
        try:
            sp: spotify.Spotify | None = spotify.Spotify(con, run.vault)  # the app's own access
        except spotify.SpotifyError:
            sp = None  # the songs keep YouTube Music's names
        ydl = ytdlp.YtDlp(run.data / "ytdlp")
        done, changed, failed = 0, 0, []
        for n, s in enumerate(srcs):
            if run.stop.is_set():
                break
            run.say(f"reading {s.title or s.url}", n, len(srcs))
            try:
                if _fetch_youtube_list(run, con, ydl, sp, s):
                    changed += 1
                    run.note(f"YouTube: {s.title or s.url} changed, read again")
                done += 1
                availability.list_readable(con, s.key, True)
            except youtube_api.YouTubeError as e:
                if run.stop.is_set():
                    break  # paused while reading: not the list's fault
                log.warning("youtube %s: %s (keeping the last listing)", s.key, e)
                failed.append(f"{s.title or s.key}: {e}")
                availability.list_readable(con, s.key, False, str(e))
        new = sum(1 for r in acquire._missing(con) if not r["tries"])
    finally:
        con.close()
    if new:
        run.after.add("search_new")
    message = f"{done} lists, {changed} changed" + (f", failed: {'; '.join(failed)}" if failed else "")
    return message + (f"; {new} new songs to search" if new else "")


def _fetch_youtube_list(
    run: "Run", con: sqlite3.Connection, ydl: ytdlp.YtDlp, sp: spotify.Spotify | None, s: Source
) -> bool:
    """Read one list; True if its songs or their states changed. New songs get their names first, the
    videos that no longer play their state (asked once: a known one stays as it is)."""
    data = youtube_api.playlist(youtube_api.playlist_id(s.url) or "")
    order = ydl.video_ids(s.url, run.stop)
    if order is None:
        raise youtube_api.YouTubeError("yt-dlp could not list it")
    playing = {t["id"]: t for t in data["songs"]}
    ids = list(dict.fromkeys(order + list(playing)))  # (YouTube Music's own songs, should yt-dlp miss one)
    if not ids and con.execute("SELECT 1 FROM list_songs WHERE list_key = ?", (s.key,)).fetchone():
        raise youtube_api.YouTubeError("no songs listed although it had some")
    snapshot = hashlib.sha1(f"{' '.join(ids)}|{' '.join(sorted(playing))}".encode()).hexdigest()[:16]
    row = con.execute("SELECT snapshot, fetched FROM lists WHERE key = ?", (s.key,)).fetchone()
    retry = set()  # songs without Spotify's names, looked up again once the lookup changed (LOOKUP)
    if db.get_meta(con, f"youtube_lookup:{s.key}") != LOOKUP:
        sql = "SELECT s.key FROM list_songs ls JOIN songs s ON s.key = ls.song_key WHERE ls.list_key = ? AND s.isrc IS NULL"
        retry = {r[0] for r in con.execute(sql, (s.key,))}
    if row and row["fetched"] and row["snapshot"] == snapshot and not retry:  # the same songs, the same ones playing
        with con:
            now = datetime.datetime.now().isoformat(timespec="seconds")
            sql = "UPDATE lists SET fetched_at = ?, creator = coalesce(?, creator) WHERE key = ?"
            con.execute(sql, (now, data.get("author"), s.key))
        return False
    known = {r[0] for r in con.execute("SELECT key FROM songs WHERE service = 'youtube'")}
    for vid, t in playing.items():
        key = f"youtube:{vid}"
        if (key not in known or key in retry) and not run.stop.is_set():
            meta = _youtube_song(con, sp, t)
            if key in retry and not meta.get("isrc"):
                continue  # still not on Spotify: as it was
            old = con.execute("SELECT * FROM songs WHERE key = ?", (key,)).fetchone()
            with con:
                if _song(con, key, "youtube", **meta):
                    known.add(key)
            if old is not None and old["file"]:
                _rename_file(con, run, old, meta)
    if run.stop.is_set():
        raise youtube_api.YouTubeError("stopped while reading")
    with con:
        db.set_meta(con, f"youtube_lookup:{s.key}", LOOKUP)
    sql = "SELECT song_key, state FROM availability WHERE song_key LIKE 'youtube:%'"
    unplayable = {k for k, state in con.execute(sql) if state in availability.UNPLAYABLE}
    states: dict[str, tuple[str, str | None]] = {}
    for vid in ids:
        key = f"youtube:{vid}"
        if vid in playing:
            states[key] = ("available", None)
        elif key in known and key not in unplayable and (found := youtube_api.state(vid)):
            states[key] = found  # (a song never seen playing has no names: left out)
    _store(con, s, [f"youtube:{vid}" for vid in ids], data["title"], data["image"], snapshot, data.get("author"))
    availability.apply(con, states)
    return True


LOOKUP = "2"  # the Spotify lookup of YouTube songs (_on_spotify); a new one looks up the songs it did not find again


def _rename_file(con: sqlite3.Connection, run: "Run", old: sqlite3.Row, meta: dict[str, Any]) -> None:
    """A YouTube song found on Spotify now: its file (its own) takes the song's new names, so the song keeps it."""
    if con.execute("SELECT 1 FROM songs WHERE file = ? AND key != ?", (old["file"], old["key"])).fetchone():
        return
    entry = next((e for e in catalog.Catalog.from_db(con).entries if e.path == old["file"]), None)
    if entry is not None and (run.paths.tracks / entry.path).is_file():
        dest = filing.rename(con, run.paths, entry, meta["artist"], meta["title"], "named as on Spotify")
        rel = dest.relative_to(run.paths.tracks).as_posix()
        with con:
            con.execute("UPDATE songs SET file = ? WHERE key = ?", (rel, old["key"]))
        if tags := tagging.for_file(con, rel, dest, old["key"]):
            tagging.write(dest, tags)  # all of Spotify's names and facts


def _youtube_song(con: sqlite3.Connection, sp: spotify.Spotify | None, t: dict[str, Any]) -> dict[str, Any]:
    """A YouTube song's names: YouTube Music's (without a video's decoration: _plain; an upload's from its
    video title: _upload_names), then Spotify's where it has the song (_on_spotify), with its ISRC, album
    and the release's length and place on it (spotify.release_facts)."""
    if t["kind"] == "ugc":
        artists, title = _upload_names((t["artists"] or [""])[0], t["title"])
    else:
        artists, title = t["artists"], _plain(t["title"])
        head, *rest = PARTS.split(title)
        if rest and rules.artist_keys(head) & {rules.artist_key(a) for a in artists}:
            title = " - ".join(rest)  # a video's "Artist - Title (Official Video)"
    artist = artists[0] if artists else ""
    meta = {"artist": artist, "title": title, "album": t["album"], "length": t["length"], "isrc": None}
    meta |= {"artists": json.dumps(artists), "url": youtube_api.watch_url(t["id"])}
    if sp is None or not artist or not title:
        return meta
    try:
        hit = _on_spotify(sp, t, artists, title)
    except spotify.SpotifyError as e:
        log.info("spotify lookup of %s - %s: %s", artist, title, e)
        return meta
    if hit is None:
        return meta
    names = [a["name"] for a in hit.get("artists") or [] if a.get("name")]
    album = (hit.get("album") or {}).get("name") or t["album"]
    found = {"artist": names[0], "title": hit["name"], "album": album, "length": hit["duration_ms"] / 1000}
    found |= {"artists": json.dumps(names), "isrc": (hit.get("external_ids") or {}).get("isrc")}
    return meta | found | spotify.release_facts(hit)


PARTS = re.compile(r"\s+[-–—|]\s+(?![^\(\[]*[\)\]])")  # "Artist - Title": a dash between spaces, not in brackets
VERSION = re.compile(r"\b(?:remix|mix|edit|bootleg|vip|rework|flip|extended|live|cover|remake|version)\b", re.I)
DECORATION = re.compile(r"\s*[\(\[\{]([^\)\]\}]*)[\)\]\}]|\s+official\s+(?:music\s+)?(?:video|audio)\b", re.I)


def _plain(text: str) -> str:
    """A video title without decoration: brackets that name no version or featured artist ("[HQ Full]",
    "(THER-108)"), "Official Video" (tagging.clean_title's too)."""
    kept = re.compile(rf"{VERSION.pattern}|\b(?:feat|ft)\b", re.I)
    text = DECORATION.sub(lambda m: m.group(0) if m.group(1) and kept.search(m.group(1)) else "", text)
    return re.sub(r"\s+", " ", tagging.clean_title(text, "")).strip()


def _upload_names(channel: str, video_title: str) -> tuple[list[str], str]:
    """An upload's artists and title from its video title: "Artist - Title", also with names that have a
    hyphen ("D-Block & S-Te-Fan - Supernova") and a label's "Label 003 - Artist - Title" (the last two
    parts, unless the last is a version: "Artist - Title - Extended Mix"); else the channel's."""
    parts = PARTS.split(_plain(video_title))
    if len(parts) > 2 and not VERSION.search(rules.fold(parts[-1])):
        parts = parts[-2:]
    if len(parts) < 2:
        parts = [channel.removesuffix(" - Topic"), parts[0]]
    artists = [a for a in ytdlp.ARTISTS.split(parts[0]) if a] or [channel]
    title = " - ".join(parts[1:])
    return [rules.clean_name(a) for a in artists], rules.clean_name(title)


def _on_spotify(sp: spotify.Spotify, t: dict[str, Any], artists: list[str], title: str) -> dict[str, Any] | None:
    """Spotify's track of a YouTube song: by its artist and title, then by the words of the video (and of
    its last two parts: a label's upload), then both without featured artists ("Channel - Artist - Title
    (feat. X)" finds "Artist - Title"); one that is the song (_same_song) whose length fits the video's:
    the same for the release's audio, within 5 s for an upload, an official video up to a minute longer
    (an intro, a scene at the end)."""
    queries = [f"track:{_plain(title)} artist:{artists[0]}", _words(t["title"])]
    if len(parts := PARTS.split(_plain(t["title"]))) > 2:
        queries.append(_words(" ".join(parts[-2:])))
    # without "(feat. X)": Spotify's search then finds podcasts ("Warriyo Mortals feat Laura Brehm")
    queries += [f"{artists[0]} {rules.search_title(title)}", _words(rules.search_title(_plain(t["title"])))]
    words = rules.title_key(" ".join([*t["artists"], t["title"]]))
    for q in dict.fromkeys(q for q in queries if q):
        time.sleep(0.25)  # gently: a new list asks for each song
        for hit in sp.search(q):
            longer = t["length"] - (hit.get("duration_ms") or 0) / 1000
            limit = {"atv": (-3, 3), "omv": (-3, 60)}.get(t["kind"], (-5, 5))
            fits = not t["length"] or limit[0] <= longer <= limit[1]
            if hit.get("artists") and hit.get("name") and fits and _same_song(hit, artists, title, words):
                return hit
    return None


def _words(text: str) -> str:
    """A search for a video's title: its words without decoration (_plain)."""
    return " ".join(re.findall(r"[\w']+", _plain(text)))


def _same_song(track: dict[str, Any], artists: list[str], title: str, words: str) -> bool:
    """A Spotify track is the YouTube song: an artist and the title in common (rules.title_key: without
    "Original Mix", a mix-cut marker), or its first artist and its title are both in the video's words
    (`words`, title_key'd: an upload's "Label 003 - A & B - Song (HQ)"); a version the video names (a remix,
    an extended mix) the track's title names too."""
    theirs = [a.get("name") or "" for a in track["artists"]]
    name = rules.title_key(rules.release_title(track["name"]))
    if any(v not in name.split() for v in VERSION.findall(words)):
        return False
    ours = {rules.artist_key(a) for a in artists} - {""}
    if name == rules.title_key(rules.release_title(title)) and {rules.artist_key(a) for a in theirs} & ours:
        return True
    first = rules.title_key(theirs[0])
    return bool(name and first) and f" {first} " in f" {words} " and f" {name} " in f" {words} "


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
        if token not in ydls:  # (one client per account: each writes its netrc once)
            ydls[token] = ytdlp.YtDlp(run.data / "ytdlp", token)
        return ydls[token]

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
    new, known = _filed_before(run, ydl, any_token, new) if new else (new, 0)
    added = 0
    if new and not run.stop.is_set():
        run.claim()  # new songs come first: a running YouTube & SoundCloud search gives way now
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
    known_part = f", {known} linked to the library's file of their page" if known else ""
    return f"{total} lists, {len(srcs)} changed{unread}; {added} new songs{known_part}"


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


_PAGES: dict[str, tuple[float, list[str]]] = {}  # library file -> (mtime, the pages in its source tags)


def _page_key(url: str) -> str:
    return url.split("?", 1)[0].rstrip("/").casefold()


def _filed_before(
    run: "Run", ydl: ytdlp.YtDlp, token: str | None, new: list[tuple[str, str]]
) -> tuple[list[tuple[str, str]], int]:
    """New SoundCloud songs a library file already is: one whose source tag holds exactly the track's page
    (Echolot wrote it when it filed that file as this song, after its checks; also into a copy from another
    Echolot). Those are linked to the file, not downloaded; anything less certain (a name alone) is downloaded
    and its audio checked (recordings.already). A track a set lists by its API address gets its page first
    (with a login 50 per request, else one by one). Returns the songs left to download and how many were
    linked. Tags are read once per file change."""
    con = run.connect()
    try:
        cat = catalog.Catalog.from_db(con)
        pages: dict[str, catalog.Entry] = {}
        for e in cat.entries:
            path = run.paths.tracks / e.path
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if (seen := _PAGES.get(e.path)) is None or seen[0] != mtime:
                seen = mtime, [_page_key(u) for u in tagging.read(path)["sources"] if u.startswith("http")]
                _PAGES[e.path] = seen
            for page in seen[1]:
                pages.setdefault(page, e)
        resolved: dict[str, str] = {}
        api = [tid for tid, url in new if "://api" in url] if pages else []  # (api-v2.soundcloud.com/tracks/<id>)
        for i in range(0, len(api) if token else 0, 50):
            try:
                found = sc_api.tracks(token, api[i : i + 50])
            except sc_api.SoundCloudError as e:
                log.info("soundcloud pages: %s", e)
                break
            resolved |= {str(t.get("id")): t.get("permalink_url") or "" for t in found}
        for tid in api if not token else []:
            resolved[tid] = (ydl.meta(dict(new)[tid], run.stop) or {}).get("webpage_url") or ""
        left, linked = [], 0
        for tid, url in new:
            page = resolved.get(tid) or url
            e = pages.get(_page_key(page)) if "://api" not in page else None
            if e is None:
                left.append((tid, url))
                continue
            tags = tagging.read(run.paths.tracks / e.path)  # the file's names are the song's (Echolot wrote them)
            artist, title = (tags["artists"] or [e.path.partition("/")[0]])[0], tags["title"] or e.title
            key = f"soundcloud:{tid}"
            with con:
                _song(con, key, "soundcloud", artist=artist, title=title, length=e.duration, url=page)
                con.execute("UPDATE songs SET archived = 1, unavailable = NULL WHERE key = ?", (key,))
            want = Want(artist, title, e.duration, key)
            recordings.link(con, run.paths, key, want, e, "its page in the file's source tag")
            run.note(f"SoundCloud: {artist} – {title}: in the library already ({e.path}), not downloaded")
            linked += 1
        if linked:
            catalog.match_songs(con)
        return left, linked
    finally:
        con.close()


def _file_sc(run: "Run", d: dict[str, str], url: str, work: Path) -> int:
    """File one SoundCloud download (its title often holds the artist: _names)."""
    key = f"soundcloud:{d['id']}"
    con = run.connect()
    try:
        keep_hires = options.get(con, options.Files).keep_hires
    finally:
        con.close()
    try:
        prepared = audio.prepare(Path(d["path"]), keep_hires)
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
