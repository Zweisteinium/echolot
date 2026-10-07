"""Discover in the players: the few Subsonic API calls Echolot answers. A gate (nginx, in front of Navidrome)
sends it a player's searches and every request about a song the library does not have yet (an "ex-" id);
everything else goes to Navidrome directly, so playing the library never needs Echolot, and a search falls
back to Navidrome's own when Echolot does not answer.

- search3: Navidrome's answer, its songs followed by up to EXTRA that Discover finds and the library lacks.
  They are kept (discover_songs), so a player can come back to them by their id.
- such a song's stream: SoundCloud's in full when SoundCloud has it, else a 30 s preview (Deezer, Apple
  Music); its cover, its details (getSong).
- a heart on it (star) is a like Echolot keeps (discover_likes), unstar takes it back; scrobbles of it are
  dropped. Ids of the library's own songs in the same request go on to Navidrome.

A player's login (u with t and s, or p) is Navidrome's: Echolot passes it on to Navidrome and keeps no
password. Answers are JSON (f=json, as Feishin and most players ask) or else XML."""

import asyncio
import base64
import hashlib
import json
import logging
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from xml.sax.saxutils import quoteattr

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from echolot import __version__, db
from echolot.jobs import lists
from echolot.library import catalog, discover
from echolot.services import catalogs, navidrome, ytdlp
from echolot.services import soundcloud as sc_api
from echolot.settings import auth
from echolot.web import discover as discover_page

log = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)
PREFIX = "ex-"  # the ids of Discover's songs
EXTRA = 10  # Discover songs a search's first page gets
WAIT = 4.0  # s Discover's sources get within a player's search (a slower one is left out)
MIN_QUERY = 3  # characters a search needs before Discover is asked too
KEEP_DAYS = 30  # a song shown and not liked is forgotten after this long
PREVIEW = 30  # s of a preview
AUTH = ("u", "p", "t", "s", "apiKey", "v", "c")  # a player's login and who it is
CHECK_SECONDS = 600  # a login Navidrome took is not asked about again for this long
API_VERSION = "1.16.1"
Params = list[tuple[str, str]]
_checked: dict[str, float] = {}
_lock = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _one(params: Params, name: str, default: str = "") -> str:
    return next((v for k, v in params if k == name), default)


async def _params(request: Request) -> Params:
    """The call's parameters: the query string's and, for a POST, the form's."""
    items = list(request.query_params.multi_items())
    if request.method == "POST" and "form" in request.headers.get("content-type", ""):
        items += [(k, v) for k, v in (await request.form()).multi_items() if isinstance(v, str)]
    return items


# ---------------------------------------------------------------- answers


def _answer(params: Params, body: dict[str, Any] | None = None, error: tuple[int, str] | None = None) -> Response:
    """A Subsonic answer in the format the player asked for (error: Subsonic's code and message)."""
    out: dict[str, Any] = {
        "status": "failed" if error else "ok",
        "version": API_VERSION,
        "type": "echolot",
        "serverVersion": __version__,
        "openSubsonic": True,
    }
    if error:
        out["error"] = {"code": error[0], "message": error[1]}
    out.update(body or {})
    if _one(params, "f").startswith("json"):
        return JSONResponse({"subsonic-response": out})
    out["xmlns"] = "http://subsonic.org/restapi"
    xml = '<?xml version="1.0" encoding="UTF-8"?>' + _xml("subsonic-response", out)
    return Response(xml, media_type="application/xml")


def _xml(tag: str, value: dict[str, Any]) -> str:
    """Subsonic's XML of a JSON answer: plain values are attributes, objects and lists child elements."""
    attrs, children = [], []
    for k, v in value.items():
        if isinstance(v, dict):
            children.append(_xml(k, v))
        elif isinstance(v, list):
            children += [_xml(k, x) for x in v if isinstance(x, dict)]
        elif v is not None:
            attrs.append(f" {k}={quoteattr(str(v).lower() if isinstance(v, bool) else str(v))}")
    inner = "".join(children)
    return f"<{tag}{''.join(attrs)}>{inner}</{tag}>" if inner else f"<{tag}{''.join(attrs)}/>"


WRONG_LOGIN = (40, "Wrong user name or password.")
NOT_FOUND = (70, "Echolot does not know this song (any more): search it again.")


# ---------------------------------------------------------------- Navidrome


def _navidrome(con: sqlite3.Connection, method: str, params: Params) -> tuple[int, str, bytes]:
    """Navidrome's answer to the same call: (HTTP status, content type, body)."""
    url = f"{navidrome.address(con)}/rest/{method}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return r.status, r.headers.get("content-type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("content-type", ""), e.read()
    except OSError as e:
        raise navidrome.NavidromeError(f"Navidrome not reachable: {e}") from e


def _ok(body: bytes) -> bool:
    try:
        return json.loads(body)["subsonic-response"]["status"] == "ok"
    except (ValueError, KeyError, TypeError):
        return False


def _user(con: sqlite3.Connection, params: Params) -> auth.User | bool | None:
    """Who the player's login is (Navidrome checks it): their Echolot user, None if they never logged in to
    Echolot, False for a wrong login."""
    login = sorted((k, v) for k, v in params if k in AUTH)
    name = _one(params, "u")
    if not name:
        return False
    key = hashlib.sha256(repr(login).encode()).hexdigest()
    with _lock:
        fresh = time.monotonic() - _checked.get(key, -CHECK_SECONDS) < CHECK_SECONDS
    if not fresh:
        status, _, body = _navidrome(con, "ping.view", [*login, ("f", "json")])
        if status != 200 or not _ok(body):
            return False
        with _lock:
            if len(_checked) > 1000:
                _checked.clear()
            _checked[key] = time.monotonic()
    return auth.get_user(con, name)


def _pass_on(con: sqlite3.Connection, method: str, params: Params) -> Response:
    status, kind, body = _navidrome(con, method, params)
    return Response(body, status, media_type=kind or None)


# ---------------------------------------------------------------- Discover's songs


def _record(r: discover.Result) -> dict[str, Any]:
    hits = [{"source": h.source, "url": h.url, "preview": h.preview, "seconds": h.seconds} for h in r.sources]
    return {
        "artist": r.artist,
        "title": r.title,
        "seconds": r.seconds,
        "album": r.album,
        "year": r.year,
        "cover": r.cover,
        "hits": hits,
    }


def _id(song: dict[str, Any]) -> str:
    first = song["hits"][0] if song["hits"] else {}
    seed = f"{first.get('source')}|{first.get('url')}|{song['artist']}|{song['title']}"
    return PREFIX + hashlib.sha256(seed.encode()).hexdigest()[:20]


def _plays(song: dict[str, Any]) -> tuple[str, str, float]:
    """What a player hears: ('soundcloud', page, seconds) in full, ('preview', source, 30), or ('', '', 0)."""
    if sc := next((h for h in song["hits"] if h["source"] == "soundcloud" and h["url"]), None):
        return "soundcloud", sc["url"], sc["seconds"] or song["seconds"]
    if pre := next((h for h in song["hits"] if h["preview"]), None):
        return "preview", pre["source"], PREVIEW
    return "", "", 0


STATES = {  # a Discover song for a user: what its artist line adds and its album says first
    "new": "♥ to add",  # not liked: a heart fetches it
    "wished": "on its way",  # liked, not in the library yet
    "here": "in your library",  # liked and fetched
}


def _state(con: sqlite3.Connection, user: auth.User | None, sid: str) -> str:
    if (
        not user
        or not con.execute("SELECT 1 FROM discover_likes WHERE user_id = ? AND song_id = ?", (user.id, sid)).fetchone()
    ):
        return "new"
    row = con.execute("SELECT file FROM songs WHERE key = ?", (song_key(sid),)).fetchone()
    return "here" if row and row[0] else "wished"


def song_key(sid: str) -> str:
    """The songs row a liked Discover song is wanted as."""
    return "discover:" + sid.removeprefix(PREFIX)


def _base(sid: str) -> str:
    """The song's id without the cover's state ("ex-…~wished": a new state is a new picture for the player)."""
    return sid.split("~", 1)[0]


def _entry(sid: str, song: dict[str, Any], state: str, starred: str | None) -> dict[str, Any]:
    """The song as Subsonic describes one (the fields players expect of Navidrome's). The artist line and the
    album tell what it is (a player shows no more): "Icona Pop · ♥ to add", "♥ to add · Deezer · 30 s preview";
    the cover has a badge (see _cover)."""
    kind, _, seconds = _plays(song)
    names = ", ".join(discover_page.NAMES.get(h["source"], h["source"]) for h in song["hits"])
    line = f"{song['artist']} · {STATES[state]}"
    album = " · ".join([STATES[state].capitalize() if state != "new" else STATES[state], names])
    out: dict[str, Any] = {
        "id": sid,
        "parent": "discover",
        "isDir": False,
        "isVideo": False,
        "type": "music",
        "mediaType": "song",
        "title": song["title"],
        "artist": line,
        "displayArtist": line,
        "album": album + (" · 30 s preview" if kind == "preview" else ""),
        "albumArtists": [],
        "artists": [{"id": f"{PREFIX}artist", "name": line}],
        "duration": round(seconds),
        "contentType": "audio/mpeg",
        "suffix": "mp3",
        "bitRate": 192 if kind == "soundcloud" else 128,
        "size": 0,
        "discNumber": 1,
        "path": f"Discover/{song['artist']} - {song['title']}.mp3",
        "created": _now(),
    }
    out["coverArt"] = f"{sid}~{state}"
    if song["year"].isdigit():
        out["year"] = int(song["year"])
    if starred:
        out["starred"] = starred
    return out


def _song(con: sqlite3.Connection, sid: str) -> dict[str, Any] | None:
    row = con.execute("SELECT data FROM discover_songs WHERE id = ?", (sid,)).fetchone()
    return json.loads(row[0]) if row else None


def _likes(con: sqlite3.Connection, user: auth.User | None) -> dict[str, str]:
    if not user:
        return {}
    rows = con.execute("SELECT song_id, liked FROM discover_likes WHERE user_id = ?", (user.id,))
    return dict(rows.fetchall())


def _found(request: Request, con: sqlite3.Connection, query: str, user: auth.User | None) -> list[dict[str, Any]]:
    """Discover's songs for a search that the library lacks, kept for the player to come back to."""
    hits, failed = discover_page.search(request, con, query, user.id if user else None, wait=WAIT)
    if failed:
        log.info("player search %r: %s", query, "; ".join(f"{k}: {v}" for k, v in failed.items()))
    cat, liked, now, out = catalog.Catalog.from_db(con), _likes(con, user), _now(), []
    for r in discover.merge(hits):
        if len(out) >= EXTRA:
            break
        if not r.title or cat.song(r.artist, r.title, r.seconds):
            continue
        song = _record(r)
        sid = _id(song)
        con.execute(
            "INSERT INTO discover_songs (id, data, seen) VALUES (?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET data = excluded.data, seen = excluded.seen",
            (sid, json.dumps(song), now),
        )
        out.append(_entry(sid, song, _state(con, user, sid), liked.get(sid)))
    old = (datetime.now() - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    con.execute("DELETE FROM discover_songs WHERE seen < ? AND id NOT IN (SELECT song_id FROM discover_likes)", (old,))
    con.commit()
    return out


# ---------------------------------------------------------------- the calls


def _search3(request: Request, params: Params) -> Response:
    con = db.connect(request.app.state.settings.db_path)
    try:
        status, kind, body = _navidrome(con, "search3.view", params)
        query = _one(params, "query").strip().strip('"*').strip()
        wanted = _one(params, "songOffset", "0") == "0" and _one(params, "songCount", "20") != "0"
        if status != 200 or not _one(params, "f").startswith("json") or not wanted or len(query) < MIN_QUERY:
            return Response(body, status, media_type=kind or None)
        try:
            answer = json.loads(body)
            result = answer["subsonic-response"]
        except (ValueError, KeyError):
            return Response(body, status, media_type=kind or None)
        if result.get("status") != "ok":
            return Response(body, status, media_type=kind or None)
        user = auth.get_user(con, _one(params, "u"))
        found = result.setdefault("searchResult3", {})
        found["song"] = list(found.get("song") or []) + _found(request, con, query, user)
        return JSONResponse(answer)
    finally:
        con.close()


def _calls(request: Request, method: str, params: Params) -> Response | tuple[str, dict[str, Any], str]:
    """Every call but search3: an answer, or for a stream ('stream', song, its id)."""
    con = db.connect(request.app.state.settings.db_path)
    try:
        ids = [_base(v) for k, v in params if k in ("id", "mediaId") and v.startswith(PREFIX)]
        if not ids:
            return _pass_on(con, f"{method}.view", params)
        user = _user(con, params)
        if user is False:
            return _answer(params, error=WRONG_LOGIN)
        rest = [(k, v) for k, v in params if not (k in ("id", "mediaId") and v.startswith(PREFIX))]
        others = any(k in ("id", "albumId", "artistId", "mediaId") for k, _ in rest)
        if method in ("star", "unstar"):
            if not isinstance(user, auth.User):
                return _answer(params, error=(50, "Log in to Echolot once first, then like it again."))
            for sid in ids:
                if (song := _song(con, sid)) is None:
                    return _answer(params, error=NOT_FOUND)
                _like(con, user, sid, song, method == "star")
            _wish(request, con, user)
            return _pass_on(con, f"{method}.view", rest) if others else _answer(params)
        if method in ("scrobble", "reportPlayback"):  # not the library's: nothing to count
            return _pass_on(con, f"{method}.view", rest) if others and method == "scrobble" else _answer(params)
        song = _song(con, ids[0])
        if song is None:
            return _answer(params, error=NOT_FOUND)
        who = user if isinstance(user, auth.User) else None
        if method == "getSong":
            starred = _likes(con, who).get(ids[0])
            return _answer(params, {"song": _entry(ids[0], song, _state(con, who, ids[0]), starred)})
        if method == "getCoverArt":  # (an id without the state: the state as of now, not kept by the browser)
            state = next((v for k, v in params if k == "id"), "").partition("~")[2]
            if state in STATES:
                return _cover(song, state, 86400)
            return _cover(song, _state(con, who, ids[0]), 0)
        if method in ("stream", "download"):
            return "stream", song, ids[0]
        return _answer(params, error=(0, "Echolot has not got this song yet."))
    finally:
        con.close()


def _like(con: sqlite3.Connection, user: auth.User, sid: str, song: dict[str, Any], liked: bool) -> None:
    if liked and "isrc" not in song:  # the recording's code (Deezer's): the audio check compares with its preview
        song["isrc"] = ""
        deezer = next((h["url"] for h in song["hits"] if h["source"] == "deezer"), "")
        if (track := deezer.rstrip("/").rsplit("/", 1)[-1]).isdigit():
            try:
                song["isrc"] = catalogs.track(track).get("isrc") or ""
            except catalogs.CatalogError as e:
                log.info("Deezer's ISRC of %s – %s: %s", song["artist"], song["title"], e)
        con.execute("UPDATE discover_songs SET data = ? WHERE id = ?", (json.dumps(song), sid))
    if liked:
        con.execute(
            "INSERT OR IGNORE INTO discover_likes (user_id, song_id, liked) VALUES (?, ?, ?)", (user.id, sid, _now())
        )
    else:
        con.execute("DELETE FROM discover_likes WHERE user_id = ? AND song_id = ?", (user.id, sid))
    con.commit()
    log.info(
        "%s %s %s – %s (Discover, in a player)",
        user.name,
        "liked" if liked else "unliked",
        song["artist"],
        song["title"],
    )


def _wish(request: Request, con: sqlite3.Connection, user: auth.User) -> None:
    """The user's Wished list as their hearts are now (jobs/lists.store_wished), and its songs searched at once
    (search_new: the songs never searched) and the playlist written (library)."""
    rows = con.execute(
        "SELECT s.id, s.data FROM discover_likes l JOIN discover_songs s ON s.id = l.song_id WHERE l.user_id = ? "
        "ORDER BY l.liked DESC, s.id",
        (user.id,),
    )
    songs = []
    for sid, data in rows:
        d = json.loads(data)
        pages = [h["url"] for h in sorted(d["hits"], key=lambda h: h["source"] != "soundcloud") if h["url"]]
        songs.append((song_key(sid), {**d, "url": pages[0] if pages else ""}))
    lists.store_wished(con, user.id, songs)
    for job in ("search_new", "library"):
        request.app.state.worker.trigger(job)


@router.get("/rest/navidrome/song/{sid}")
def native_song(request: Request, sid: str) -> Response:
    """GET /api/song/<ex- id> of Navidrome's own API (the gate sends it here): Feishin in Navidrome mode asks
    it for the song that starts playing, and shows what it answers. The login is Navidrome's token."""
    con = db.connect(request.app.state.settings.db_path)
    try:
        name = _token_user(con, request.headers.get("x-nd-authorization", ""))
        if name is None:
            return JSONResponse({"error": "Not authenticated"}, 401)
        song, user = _song(con, _base(sid)), auth.get_user(con, name)
        if song is None:
            return JSONResponse({"error": "data not found"}, 404)
        return JSONResponse(_native(_base(sid), song, _state(con, user, _base(sid)), _likes(con, user)))
    except navidrome.NavidromeError as e:
        log.warning("player call song: %s", e)
        return JSONResponse({"error": "Navidrome is not reachable"}, 502)
    finally:
        con.close()


def _token_user(con: sqlite3.Connection, header: str) -> str | None:
    """Whose Navidrome token it is ("Bearer <JWT>"), if Navidrome takes it: its user name (the token's sub)."""
    token = header.removeprefix("Bearer ").strip()
    try:
        part = token.split(".")[1]
        name = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["sub"]
    except (IndexError, ValueError, KeyError, TypeError):
        return None
    key = hashlib.sha256(token.encode()).hexdigest()
    with _lock:
        fresh = time.monotonic() - _checked.get(key, -CHECK_SECONDS) < CHECK_SECONDS
    if not fresh:
        request = urllib.request.Request(
            f"{navidrome.address(con)}/api/song?_start=0&_end=1", headers={"X-ND-Authorization": f"Bearer {token}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=10):
                pass
        except urllib.error.HTTPError:
            return None
        except OSError as e:
            raise navidrome.NavidromeError(f"Navidrome not reachable: {e}") from e
        with _lock:
            _checked[key] = time.monotonic()
    return name if isinstance(name, str) else None


def _native(sid: str, song: dict[str, Any], state: str, liked: dict[str, str]) -> dict[str, Any]:
    """The song as Navidrome's own API describes one (the same names as _entry)."""
    entry = _entry(sid, song, state, liked.get(sid))
    artist = {"id": entry["artists"][0]["id"], "name": entry["artist"]}
    return {
        "id": sid,
        "title": entry["title"],
        "album": entry["album"],
        "albumId": "",
        "artist": entry["artist"],
        "artistId": artist["id"],
        "albumArtist": entry["artist"],
        "albumArtistId": artist["id"],
        "participants": {"artist": [artist], "albumartist": [artist]},
        "bitRate": entry["bitRate"],
        "bookmarkPosition": 0,
        "compilation": False,
        "createdAt": entry["created"],
        "updatedAt": entry["created"],
        "discNumber": 1,
        "trackNumber": 1,
        "duration": entry["duration"],
        "genre": "",
        "genres": None,
        "hasCoverArt": True,
        "path": entry["path"],
        "sampleRate": 44100,
        "size": 0,
        "suffix": "mp3",
        "orderTitle": entry["title"],
        "orderAlbumName": entry["album"],
        "orderArtistName": entry["artist"],
        "orderAlbumArtistName": entry["artist"],
        "year": entry.get("year", 0),
        "starred": sid in liked,
        **({"starredAt": liked[sid]} if sid in liked else {}),
    }


@router.api_route("/rest/{method}", methods=["GET", "POST"])
async def rest(request: Request, method: str) -> Response:
    method = method.removesuffix(".view")
    params = await _params(request)
    try:
        if method == "search3":
            return await run_in_threadpool(_search3, request, params)
        got = await run_in_threadpool(_calls, request, method, params)
        if isinstance(got, Response):
            return got
        return await _stream(request, params, got[1])
    except navidrome.NavidromeError as e:
        log.warning("player call %s: %s", method, e)
        return _answer(params, error=(0, "Navidrome is not reachable."))


# ---------------------------------------------------------------- listening


async def _stream(request: Request, params: Params, song: dict[str, Any]) -> Response:
    kind, where, _ = _plays(song)
    try:
        if kind == "soundcloud":
            url = await run_in_threadpool(_soundcloud_stream, request, where)
            return StreamingResponse(_mp3(url), media_type="audio/mpeg")
        if kind == "preview":
            hit = next(h for h in song["hits"] if h["source"] == where)
            audio = await run_in_threadpool(_preview, hit)
            return Response(audio, media_type="audio/mpeg", headers={"Accept-Ranges": "none"})
    except (sc_api.SoundCloudError, catalogs.CatalogError, OSError, ValueError) as e:
        log.info("player stream of %s – %s: %s", song["artist"], song["title"], e)
    return _answer(params, error=(70, "This song can't be heard before Echolot has it."))


def _soundcloud_stream(request: Request, page: str) -> str:
    """The track's stream address: with a SoundCloud login through its API (quick), else yt-dlp's."""
    con = db.connect(request.app.state.settings.db_path)
    try:
        token = sc_api.any_token(con, request.app.state.vault)
    finally:
        con.close()
    if token:
        return sc_api.stream(token, page)
    r = ytdlp.YtDlp(request.app.state.settings.data_dir / "ytdlp").run(
        ["-g", "-f", "bestaudio/best", "--no-playlist", page], timeout=60
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise sc_api.SoundCloudError("yt-dlp found no stream")
    return r.stdout.strip().splitlines()[-1]


async def _mp3(url: str) -> AsyncIterator[bytes]:
    """The stream as MP3 while it comes (ffmpeg); a player that stops listening stops ffmpeg."""
    p = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        url,
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        "-f",
        "mp3",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        assert p.stdout is not None
        while chunk := await p.stdout.read(65536):
            yield chunk
    finally:
        if p.returncode is None:
            p.kill()
            await p.wait()


def _preview(hit: dict[str, Any]) -> bytes:
    """A 30 s preview's MP3; Deezer's addresses expire, so Deezer is asked for a fresh one."""
    url = hit["preview"]
    if hit["source"] == "deezer" and (track := hit["url"].rstrip("/").rsplit("/", 1)[-1]).isdigit():
        url = catalogs.track(track).get("preview") or url
    request = urllib.request.Request(url, headers=catalogs.UA)
    with urllib.request.urlopen(request, timeout=15) as r:
        return r.read()


# ---------------------------------------------------------------- covers


BADGES = {  # state -> colour, the badge's drawing (on a 600 x 600 cover, centred at 486, 486)
    "new": ("#1f6bcf", '<path d="M486 436v100M436 486h100" stroke="#fff" stroke-width="24" stroke-linecap="round"/>'),
    "wished": (
        "#b7791f",
        '<circle cx="486" cy="486" r="48" fill="none" stroke="#fff" stroke-width="16"/>'
        '<path d="M486 456v32l22 16" fill="none" stroke="#fff" stroke-width="16" stroke-linecap="round"/>',
    ),
    "here": (
        "#1a7f37",
        '<path d="M446 488l28 28 58-62" fill="none" stroke="#fff" stroke-width="24" stroke-linecap="round" '
        'stroke-linejoin="round"/>',
    ),
}
NO_COVER = (  # a note on grey, for a song without a picture
    '<rect width="600" height="600" fill="#2b3240"/><g fill="#5b6578"><circle cx="262" cy="392" r="58"/>'
    '<rect x="296" y="160" width="26" height="236"/><path d="M296 160h130v64H322z"/></g>'
)
_covers: dict[str, tuple[bytes, str]] = {}  # the source's picture -> (bytes, type), the last ones asked


def _picture(url: str) -> tuple[bytes, str] | None:
    if (got := _covers.get(url)) is None:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=catalogs.UA), timeout=10) as r:
                got = r.read(2_000_000), (r.headers.get("content-type") or "image/jpeg").split(";")[0]
        except (OSError, ValueError):
            return None
        if len(_covers) > 300:
            _covers.pop(next(iter(_covers)))
        _covers[url] = got
    return got


def _cover(song: dict[str, Any], state: str, keep: int) -> Response:
    """The song's picture with a badge in its corner: + (a heart adds it), a clock (on its way), a tick (in
    the library); "30s" when only a preview plays. An SVG with the picture inside, so it needs no image library
    and stays sharp at any size."""
    picture = _picture(song["cover"]) if song["cover"] else None
    if picture:
        data = base64.b64encode(picture[0]).decode()
        art = f'<image href="data:{picture[1]};base64,{data}" width="600" height="600" preserveAspectRatio="xMidYMid slice"/>'
    else:
        art = NO_COVER
    colour, mark = BADGES.get(state, BADGES["new"])
    badge = f'<circle cx="486" cy="486" r="96" fill="{colour}" stroke="#fff" stroke-width="10"/>{mark}'
    if _plays(song)[0] == "preview":
        badge += (
            '<rect x="22" y="22" width="168" height="80" rx="40" fill="#000" fill-opacity=".7"/><text x="106" y="78" '
            'font-family="sans-serif" font-size="48" font-weight="700" fill="#fff" text-anchor="middle">30s</text>'
        )
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 600" width="600" height="600">{art}{badge}</svg>'
    cache = f"max-age={keep}" if keep else "no-cache"
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": cache})
