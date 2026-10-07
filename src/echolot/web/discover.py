"""Discover: search new songs in Deezer, Apple Music and SoundCloud at once (library/discover merges them).
The three are asked side by side, each for at most WAIT seconds; a query's answers are kept CACHE_SECONDS
(Apple Music allows about 20 searches a minute). Each result says whether the library has the song already,
by the same names-and-length rule the lists use (catalog.Catalog.song)."""

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from echolot.library import catalog, discover
from echolot.library.discover import Hit
from echolot.services import catalogs, ytdlp
from echolot.services import soundcloud as sc_api
from echolot.settings import options
from echolot.web.common import DB, page

router = APIRouter(include_in_schema=False)
WAIT = 8.0  # s for all sources together; a slower one is left out of this answer
CACHE_SECONDS = 600
MIN_LENGTH = 2  # characters a query needs
_cache: dict[tuple[str, bool], tuple[float, list[Hit], dict[str, str]]] = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(6, thread_name_prefix="discover")


def _soundcloud(request: Request, con: DB) -> Callable[[str], list[Hit]]:
    """SoundCloud's own search with the user's login (or another user's); without any, yt-dlp's (fewer hits)."""
    vault = request.app.state.vault
    token = sc_api.token_of(con, vault, request.state.user.id) or sc_api.any_token(con, vault)
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


def search(request: Request, con: DB, query: str) -> tuple[list[Hit], dict[str, str]]:
    """Every source's hits for the query, and why a source gave none (source -> reason)."""
    key = (" ".join(query.casefold().split()), bool(sc_api.any_token(con, request.app.state.vault)))
    with _lock:
        if (cached := _cache.get(key)) and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1], cached[2]
    country = options.get(con, options.Spotify).market
    asks: dict[str, Callable[[str], list[Hit]]] = {
        "deezer": catalogs.deezer,
        "apple": lambda q: catalogs.apple(q, country),
        "soundcloud": _soundcloud(request, con),
    }
    futures = {name: _pool.submit(ask, query) for name, ask in asks.items()}
    wait(futures.values(), timeout=WAIT)
    hits: list[Hit] = []
    failed: dict[str, str] = {}
    for name, f in futures.items():
        if not f.done():
            failed[name] = "no answer in time"
        elif e := f.exception():
            failed[name] = str(e)[:120]
        else:
            hits += f.result()
    if not failed:  # (a source that failed is asked again next time)
        with _lock:
            if len(_cache) > 256:
                _cache.pop(next(iter(_cache)))
            _cache[key] = (time.monotonic(), hits, failed)
    return hits, failed


@router.get("/discover", response_class=HTMLResponse)
def discover_page(request: Request, con: DB, q: Annotated[str, Query()] = "") -> HTMLResponse:
    return page(request, "discover.html", nav="discover", q=q, **_answer(request, con, q))


@router.get("/discover/results", response_class=HTMLResponse)
def discover_results(request: Request, con: DB, q: Annotated[str, Query()] = "") -> HTMLResponse:
    return page(request, "_discover_results.html", q=q, **_answer(request, con, q))


NAMES = {"deezer": "Deezer", "apple": "Apple Music", "soundcloud": "SoundCloud"}


def _answer(request: Request, con: DB, q: str) -> dict:
    q = q.strip()
    if len(q) < MIN_LENGTH:
        return {"results": [], "failed": {}, "have": {}, "seconds": 0, "names": NAMES}
    t0 = time.monotonic()
    hits, failed = search(request, con, q)
    results = discover.merge(hits)[:40]
    cat = catalog.Catalog.from_db(con)
    have = {}  # result index -> the library's file of the song
    for n, r in enumerate(results):
        if entries := cat.song(r.artist, r.title, r.seconds):
            have[n] = entries[0]
    return {"results": results, "failed": failed, "have": have, "seconds": time.monotonic() - t0, "names": NAMES}
