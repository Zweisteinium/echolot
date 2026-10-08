"""Discover: search new songs in Deezer, Apple Music and SoundCloud at once (library/discover merges them).
The three are asked side by side, each for at most WAIT seconds; a query's answers are kept CACHE_SECONDS
(Apple Music allows about 20 searches a minute). Each result says whether the library has the song already,
by the same names-and-length rule the lists use (catalog.Catalog.song). A source that failed rests REST
seconds (Apple Music answers 403 when asked too often; a player's search asks with every typed letter)."""

import json
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as done
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from echolot.library import catalog, discover, tagging
from echolot.library.discover import Hit
from echolot.services import catalogs, spotify, ytdlp
from echolot.services import soundcloud as sc_api
from echolot.settings import options, sources
from echolot.settings.sources import ConfigError
from echolot.web import wishes
from echolot.web.common import DB, page

router = APIRouter(include_in_schema=False)
WAIT = 8.0  # s for all sources together; a slower one is left out of this answer
CACHE_SECONDS = 600
MIN_LENGTH = 2  # characters a query needs
REST = 60.0  # s a source that failed is left out
_resting: dict[str, tuple[float, str]] = {}  # source -> (until, why)
_cache: dict[tuple[str, bool], tuple[float, list[Hit], dict[str, str]]] = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(6, thread_name_prefix="discover")


def _soundcloud(request: Request, con: DB, user_id: int | None) -> Callable[[str], list[Hit]]:
    """SoundCloud's own search with the user's login (or another user's); without any, yt-dlp's (fewer hits)."""
    vault = request.app.state.vault
    token = (user_id and sc_api.token_of(con, vault, user_id)) or sc_api.any_token(con, vault)
    if token:
        return lambda q: sc_api.search(token, q)
    ydl = ytdlp.YtDlp(request.app.state.settings.data_dir / "ytdlp")

    def search(q: str) -> list[Hit]:
        hits = []
        for n, r in enumerate(ydl.search(q, "soundcloud", threading.Event())):
            artist, title = ytdlp.artist_title(r["uploader"], "", r["title"])
            hits.append(Hit("soundcloud", n, artist, title, float(r["duration"] or 0), url=r["url"]))
        return hits

    return search


def search(
    request: Request, con: DB, query: str, user_id: int | None = None, wait: float = WAIT
) -> tuple[list[Hit], dict[str, str]]:
    """Every source's hits for the query, and why a source gave none (source -> reason); user_id: whose
    SoundCloud login to search with (default: the logged-in user's)."""
    if user_id is None and (user := getattr(request.state, "user", None)):
        user_id = user.id
    key = (" ".join(query.casefold().split()), bool(sc_api.any_token(con, request.app.state.vault)))
    with _lock:
        if (cached := _cache.get(key)) and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1], cached[2]
    country = options.get(con, options.Spotify).market
    asks: dict[str, Callable[[str], list[Hit]]] = {
        "deezer": catalogs.deezer,
        "apple": lambda q: catalogs.apple(q, country),
        "soundcloud": _soundcloud(request, con, user_id),
    }
    now = time.monotonic()
    failed = {name: why for name, (until, why) in _resting.items() if now < until}
    futures = {name: _pool.submit(ask, query) for name, ask in asks.items() if name not in failed}
    done(futures.values(), timeout=wait)
    hits: list[Hit] = []
    for name, f in futures.items():
        if not f.done():
            failed[name] = "no answer in time"
        elif e := f.exception():
            failed[name] = str(e)[:120]
            _resting[name] = (time.monotonic() + REST, failed[name])
        else:
            hits += f.result()
    if not failed:  # (a source that failed is asked again next time)
        with _lock:
            if len(_cache) > 256:
                _cache.pop(next(iter(_cache)))
            _cache[key] = (time.monotonic(), hits, failed)
    return hits, failed


@router.get("/discover")
def discover_redirect(request: Request) -> RedirectResponse:
    """Discover's old address: Search is the start page."""
    query = request.url.query
    return RedirectResponse("/" + (f"?{query}" if query else ""), status_code=301)


@router.get("/", response_class=HTMLResponse)
def discover_page(request: Request, con: DB, q: Annotated[str, Query()] = "") -> HTMLResponse:
    wished = _wished(request, con)
    return page(request, "discover.html", nav="search", q=q, **{**wished, **_answer(request, con, q)})


@router.get("/discover/results", response_class=HTMLResponse)
def discover_results(request: Request, con: DB, q: Annotated[str, Query()] = "") -> HTMLResponse:
    return page(request, "_discover_results.html", q=q, **_answer(request, con, q))


NAMES = {"deezer": "Deezer", "apple": "Apple Music", "soundcloud": "SoundCloud"}


def _answer(request: Request, con: DB, q: str) -> dict:
    """The results: each with what ▶ plays (play) and either the library's file (have) or its id to Get it
    by and the user's state of it (ids, states: new, wished, here; wishes.state)."""
    q = q.strip()
    out: dict = {"results": [], "failed": {}, "have": {}, "ids": {}, "states": {}, "plays": {}, "names": NAMES}
    if len(q) < MIN_LENGTH:
        return {**out, "seconds": 0}
    t0 = time.monotonic()
    if LINK.match(q):  # a pasted link: a list to follow, or a song to search for by its names
        out["link"] = link = _link(request, con, q)
        if "song" not in link:
            return {**out, "seconds": time.monotonic() - t0}
        q = " ".join(link["song"])
    hits, failed = search(request, con, q)
    results = discover.merge(hits)[:40]
    cat, now, user = catalog.Catalog.from_db(con), datetime.now().isoformat(timespec="seconds"), request.state.user
    for n, r in enumerate(results):
        out["plays"][n] = play(r.sources)
        if entries := cat.song(r.artist, r.title, r.seconds):
            out["have"][n] = entries[0]
        else:
            out["ids"][n] = sid = wishes.keep(con, r, now)[0]
            out["states"][n] = wishes.state(con, user, sid)
    wishes.forget_old(con)
    con.commit()
    return {**out, "results": results, "failed": failed, "seconds": time.monotonic() - t0}


LINK = re.compile(r"(?:https?://|spotify:|(?:www\.|m\.)?(?:open\.spotify|soundcloud|youtube)\.com/|youtu\.be/)", re.I)
SPOTIFY_TRACK = re.compile(r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?track/|spotify:track:)(\w+)")


def _link(request: Request, con: sqlite3.Connection, url: str) -> dict[str, Any]:
    """What a pasted link is: a list to follow ({follow: service, url, name, image}), a song to search for
    ({song: (artist, title)}: a Spotify, SoundCloud or YouTube track), or neither ({error})."""
    from echolot.web import sources as sources_page

    try:
        service, canonical = sources.parse_url(url)
        name, image = sources_page._preview(con, request, service, canonical)
        return {"follow": {"service": service, "url": canonical, "name": name, "image": image}}
    except ConfigError as e:
        why = str(e)
    if song := _track(request, con, url):
        return {"song": song}
    return {"error": why}


def _track(request: Request, con: sqlite3.Connection, url: str) -> tuple[str, str] | None:
    """A track link's artist and title: Spotify's catalogue (the app's own access), else the page's oEmbed
    (SoundCloud, YouTube: the uploader and the title, split as the lists split them)."""
    if m := SPOTIFY_TRACK.search(url):
        try:
            t = spotify.Spotify(con, request.app.state.vault).track(m.group(1))
            return ", ".join(a["name"] for a in t.get("artists") or []), t.get("name") or ""
        except spotify.SpotifyError:
            return None
    host = urllib.parse.urlparse(url if "://" in url else "https://" + url).hostname or ""
    service = "soundcloud" if host.endswith("soundcloud.com") else "youtube" if "youtu" in host else ""
    if not service:
        return None
    from echolot.web.sources import OEMBED

    try:
        req = urllib.request.Request(OEMBED[service] + urllib.parse.quote(url, safe=""), headers=catalogs.UA)
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.load(r)
    except (OSError, ValueError):
        return None
    title, author = d.get("title") or "", d.get("author_name") or ""
    title = title.removesuffix(f" by {author}") if service == "soundcloud" else title
    artist, title = ytdlp.artist_title(author, "", title)
    return (artist, tagging.clean_title(title, artist)) if title else None


def play(hits: Sequence[Hit | dict[str, Any]]) -> dict[str, str] | None:
    """What ▶ plays of a song: a 30 s preview (Deezer's, Apple Music's), else the track in SoundCloud's own
    player (its widget, driven by the page's player line); None without either."""

    def get(h: Hit | dict[str, Any], name: str) -> str:
        return str(h.get(name) or "") if isinstance(h, dict) else str(getattr(h, name) or "")

    if pre := next((h for h in hits if get(h, "preview")), None):
        return {"kind": "preview", "src": get(pre, "preview"), "source": get(pre, "source"), "page": get(pre, "url")}
    if sc := next((h for h in hits if get(h, "source") == "soundcloud" and get(h, "url")), None):
        return {"kind": "soundcloud", "src": get(sc, "url"), "source": "soundcloud", "page": get(sc, "url")}
    return None


@router.post("/discover/{sid}/get", response_class=HTMLResponse)
def get_song(request: Request, con: DB, sid: str) -> HTMLResponse:
    """Get: the user wishes the song; it is searched at once and filed into their "Echolot · Wished"."""
    if (song := wishes.song(con, sid)) is None:
        raise HTTPException(404, "Echolot does not know this song (any more): search it again.")
    wishes.like(con, request.state.user, sid, song, True, "on the Discover page")
    wishes.wish(request, con, request.state.user)
    response = page(request, "_discover_get.html", sid=sid, state="wished")
    response.headers["HX-Trigger"] = "wished"  # the Wished list shows it
    return response


@router.post("/discover/{sid}/forget", response_class=HTMLResponse)
def forget_song(request: Request, con: DB, sid: str) -> HTMLResponse:
    """The user no longer wishes the song (a file already fetched stays in the library)."""
    if (song := wishes.song(con, sid)) is not None:
        wishes.like(con, request.state.user, sid, song, False, "on the Discover page")
        wishes.wish(request, con, request.state.user)
    return page(request, "_discover_wished.html", **_wished(request, con))


@router.get("/discover/wished", response_class=HTMLResponse)
def wished_list(request: Request, con: DB) -> HTMLResponse:
    return page(request, "_discover_wished.html", **_wished(request, con))


STAGES = {  # a fetch's stage (acquire.track) as the Wished list says it
    "searching": "Searching Soulseek",
    "downloading": "Downloading",
    "checking": "Checking the audio",
    "other sources": "Searching YouTube and SoundCloud",
}


def _wished(request: Request, con: sqlite3.Connection) -> dict[str, Any]:
    """The user's wished songs, newest first, each with how far it is: in the library, being fetched (the
    running job's stage, a download's percent: acquire.track), waiting for its search, or not found yet
    (searched again later). active: one is still on its way (the list asks again every few seconds)."""
    runs, _ = request.app.state.worker.state()
    fetching = {key: f for run in runs.values() for key, f in dict(run.fetching).items()}
    rows = con.execute(
        "SELECT s.id, s.data, l.liked, so.file, coalesce(a.tries, 0) FROM discover_likes l "
        "JOIN discover_songs s ON s.id = l.song_id LEFT JOIN songs so ON so.key = 'discover:' || substr(s.id, 4) "
        "LEFT JOIN attempts a ON a.song_key = so.key WHERE l.user_id = ? ORDER BY l.liked DESC",
        (request.state.user.id,),
    )
    wished = []
    for sid, data, when, file, tries in rows:
        d = json.loads(data)
        f = fetching.get(wishes.song_key(sid))
        percent = None
        if file:
            status = "In your library"
        elif f:
            status = STAGES.get(f["stage"], f["stage"])
            if f["stage"] == "downloading" and f["total"]:
                percent = min(100, round(100 * f["done"] / f["total"]))
                status += f" {percent} %"
        elif tries:
            status = f"Not found yet · searched {tries}× · again later"
        else:
            status = "Waiting for its search"
        wished.append(
            {
                **d,
                "id": sid,
                "liked": when,
                "file": file,
                "status": status,
                "fetching": bool(f),
                "percent": percent,
                "play": play(d["hits"]),
            }
        )
    return {"wished": wished, "active": any(not w["file"] for w in wished), "names": NAMES}
