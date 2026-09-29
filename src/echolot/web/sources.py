"""Sources page: the lists of the connected accounts (found through their APIs) as cards, each followed
as off, songs only, or songs and a playlist in the music server; other people's lists by link.
Stopping to follow deletes nothing: the songs stay in the library, the list's history in the database,
following it again brings it back."""

import collections
import contextlib
import json
import logging
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from echolot import db
from echolot.jobs import lists
from echolot.services import soundcloud, spotify
from echolot.settings import options, sources
from echolot.settings.sources import ConfigError
from echolot.web import stats
from echolot.web.common import DB, back, page

log = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)
MODES = ("off", "songs", "playlist")
KINDS = (("own", "Yours"), ("collab", "Collaborative"), ("other", "By others"))  # the filter on the page
NOT_READABLE = (
    "Spotify doesn't hand out this playlist's songs (its rules for private apps cover only your own and "
    "collaborative playlists). Copy them into a playlist of yours (select all, Add to playlist)."
)
FRESH_SECONDS = 300
_found: dict[str, tuple[float, list[dict[str, Any]]]] = {}  # service -> (when, cards)
_fetching = {"spotify": threading.Lock(), "soundcloud": threading.Lock()}


def _state(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Followed lists by key: mode, songs, have."""
    counts = {r["key"]: r for r in stats.lists(con)}
    out = {}
    for s in sources.lists(con):
        c = counts.get(s.key)
        out[s.key] = {"mode": "playlist" if s.playlist else "songs", "songs": c["songs"] if c else 0,
                      "have": c["have"] if c else 0, "fetched": bool(c and c["fetched"]),
                      "title": c["title"] if c else s.title}  # fmt: skip
    return out


def _card(card: dict[str, Any], state: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A discovered or followed list with its follow state."""
    s = state.get(card["key"])
    return {**card, "mode": s["mode"] if s else "off", "have": s["have"] if s else None,
            "in_library": s["songs"] if s else None}  # fmt: skip


def _cards_of(app: FastAPI, service: str) -> list[dict[str, Any]]:
    """An account's lists as cards: from the cache while it is fresh; an older cache answers at once and
    is renewed in the background; only the first call waits for the service (callers meanwhile share it)."""
    cached = _found.get(service)
    if cached and time.time() - cached[0] < FRESH_SECONDS:
        return cached[1]
    lock = _fetching[service]
    if cached:
        if lock.acquire(blocking=False):
            threading.Thread(target=_renew, args=(app, service, lock), daemon=True).start()
        return cached[1]
    with lock:
        return _found[service][1] if service in _found else _fetch(app, service)


def _renew(app: FastAPI, service: str, lock: threading.Lock) -> None:
    try:
        _fetch(app, service)
    except (spotify.SpotifyError, soundcloud.SoundCloudError, OSError) as e:
        log.info("renewing the %s lists: %s", service, e)
    finally:
        lock.release()


def _fetch(app: FastAPI, service: str) -> list[dict[str, Any]]:
    con = db.connect(app.state.settings.db_path)
    try:
        cards = _spotify_cards(app, con) if service == "spotify" else _soundcloud_cards(app, con)
    finally:
        con.close()
    _found[service] = (time.time(), cards)
    return cards


def _spotify_cards(app: FastAPI, con: sqlite3.Connection) -> list[dict[str, Any]]:
    sp = spotify.Spotify(con, app.state.vault)
    likes = {"key": "spotify:likes", "service": "spotify", "url": "likes", "name": "Liked Songs", "owner": "you"}
    cards = [likes | {"songs": sp.liked_count(), "image": spotify.LIKED_SONGS_IMAGE, "kind": "own"}]
    for p in sp.playlists():
        kind = "own" if p["own"] else "collab" if p["collaborative"] else "other"
        card = {"key": f"spotify:playlist:{p['id']}", "service": "spotify", "url": p["url"], "name": p["name"]}
        about = {"owner": "you" if p["own"] else p["owner"], "songs": p["songs"], "image": p["image"], "kind": kind}
        cards.append(card | about | {"note": "" if p["readable"] else NOT_READABLE})
    return cards


def _soundcloud_cards(app: FastAPI, con: sqlite3.Connection) -> list[dict[str, Any]]:
    token = app.state.vault.get(con, soundcloud.TOKEN)
    if not token:
        raise soundcloud.SoundCloudError("SoundCloud is not connected.")
    me = soundcloud.me(token)
    user = me["user"]
    likes = {"key": f"soundcloud:{user}/likes", "service": "soundcloud", "url": "likes", "name": "SoundCloud Likes"}
    cards = [likes | {"owner": "you", "songs": me["likes"], "image": me["avatar"], "kind": "own"}]
    for p in soundcloud.sets(token):
        try:
            _, url = sources.parse_url(p["url"])
        except ConfigError:
            continue
        card = {"key": f"soundcloud:{urllib.parse.urlparse(url).path.strip('/')}", "service": "soundcloud", "url": url}
        owner, kind = ("you", "own") if p["own"] else (p["owner"], "other")
        cards.append(card | {"name": p["name"], "owner": owner, "songs": p["songs"], "image": p["image"], "kind": kind})
    return cards


@router.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, con: DB) -> HTMLResponse:
    from echolot.web.accounts import known

    return page(request, "sources.html", nav="sources", s=known(request, con),
                followed=collections.Counter(x.service for x in sources.lists(con)),
                removed_playlists=options.get(con, options.SourceOptions).removed_playlists)  # fmt: skip


@router.get("/sources/found/{service}", response_class=HTMLResponse)
def found(request: Request, con: DB, service: str) -> HTMLResponse:
    """The lists of a connected account as cards (the page loads them with htmx)."""
    state = _state(con)
    try:
        cards, error = _cards_of(request.app, service), None
    except (spotify.SpotifyError, soundcloud.SoundCloudError) as e:
        cards, error = [], str(e)
    kinds = collections.Counter(c["kind"] for c in cards)
    shown, cards = [(k, label, kinds[k]) for k, label in KINDS if kinds[k]], [_card(c, state) for c in cards]
    return page(request, "_cards.html", cards=cards, error=error, service=service, kinds=shown)


@router.get("/sources/other", response_class=HTMLResponse)
def other(request: Request, con: DB) -> HTMLResponse:
    """Followed lists that are not among the connected accounts' own (other people's, by link)."""
    shown: set[str] = set()
    for service in ("spotify", "soundcloud"):
        with contextlib.suppress(spotify.SpotifyError, soundcloud.SoundCloudError):
            shown |= {c["key"] for c in _cards_of(request.app, service)}
    state, cards = _state(con), []
    for s in sources.lists(con):
        if s.key in shown or s.name in sources.LIKES:
            continue
        row = con.execute("SELECT title, cover_url FROM lists WHERE key = ?", (s.key,)).fetchone()
        cards.append(_card({"key": s.key, "service": s.service, "url": s.url,
                            "name": (row["title"] if row else None) or s.title or s.url, "owner": "",
                            "songs": None, "image": row["cover_url"] if row else None}, state))  # fmt: skip
    return page(request, "_cards.html", cards=cards, error=None, service="other")


def _account_card(key: str) -> dict[str, Any] | None:
    """The card of a list in a connected account, as last read."""
    return next((c for _, cards in _found.values() for c in cards if c["key"] == key), None)


def _db_card(con: sqlite3.Connection, key: str, service: str, url: str) -> dict[str, Any]:
    """What the database knows of a list added by link."""
    row = con.execute("SELECT title, cover_url FROM lists WHERE key = ?", (key,)).fetchone()
    name, image = (row["title"], row["cover_url"]) if row else (url, None)
    return {"key": key, "service": service, "url": url, "name": name, "owner": "", "songs": None, "image": image}


def _card_answer(request: Request, con: sqlite3.Connection, card: dict[str, Any], error: str = "") -> Response:
    if request.headers.get("hx-request"):
        return page(request, "_card.html", c=_card(card, _state(con)), error=error)
    return back("/sources", **({"error": error} if error else {"ok": "Saved."}))


@router.post("/sources/follow")
def follow(
    request: Request,
    con: DB,
    key: Annotated[str, Form()],
    service: Annotated[str, Form()],
    url: Annotated[str, Form()],
    mode: Annotated[str, Form()],
) -> Response:
    """Follow a list (mode songs or playlist) or stop following it (off: nothing is deleted)."""
    account_card = _account_card(key)
    card = account_card or _db_card(con, key, service, url)
    if mode not in MODES:
        return _card_answer(request, con, card, "Unknown choice.")
    try:
        if url == "likes":
            user = options.get(con, options.SourceOptions).soundcloud_user if service == "soundcloud" else None
            sources.set_likes(con, service, mode != "off", user)
            if mode != "off":
                row = con.execute("SELECT key FROM sources WHERE service = ? AND likes = 1", (service,)).fetchone()
                if row:
                    sources.set_playlist(con, row["key"], mode == "playlist")
        elif mode == "off":
            if con.execute("SELECT 1 FROM sources WHERE key = ?", (key,)).fetchone():
                sources.remove_list(con, key)
        elif con.execute("SELECT 1 FROM sources WHERE key = ?", (key,)).fetchone():
            sources.set_playlist(con, key, mode == "playlist")
        else:
            sources.add_list(con, url, "", mode == "playlist")
    except ConfigError as e:
        return _card_answer(request, con, card, str(e))
    lists.sync_table(con)
    if account_card:  # a list not read yet shows its account's name and cover meanwhile
        title_sql = "title = CASE WHEN fetched THEN title ELSE ? END, cover_url = coalesce(cover_url, ?)"
        with con:
            con.execute(f"UPDATE lists SET {title_sql} WHERE key = ?", (card["name"], card["image"], key))
    if mode != "off":
        request.app.state.worker.trigger("sync" if service == "spotify" else "soundcloud")
    return _card_answer(request, con, card)


def _preview(con: sqlite3.Connection, request: Request, service: str, url: str) -> tuple[str, str | None]:
    """Name and cover of a list before it is read: Spotify's API when connected, else oEmbed."""
    if service == "spotify":
        try:
            meta = spotify.Spotify(con, request.app.state.vault).playlist(spotify.playlist_id(url) or "")
            return meta["name"], meta["image"]
        except spotify.SpotifyError:
            pass
    endpoint = (
        "https://open.spotify.com/oembed?url="
        if service == "spotify"
        else "https://soundcloud.com/oembed?format=json&url="
    )
    try:
        req = urllib.request.Request(endpoint + urllib.parse.quote(url, safe=""), headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)
        return d.get("title") or url, d.get("thumbnail_url")
    except (OSError, ValueError):
        return url, None


@router.post("/sources/add")
def add(
    request: Request, con: DB, url: Annotated[str, Form()], mode: Annotated[str, Form()] = "playlist"
) -> RedirectResponse:
    """Follow a list by its link (another user's playlist, a public set)."""
    try:
        service, canonical = sources.parse_url(url)
        name, image = _preview(con, request, service, canonical)
        key = sources.add_list(con, canonical, "", mode != "songs")
    except ConfigError as e:
        return back("/sources", error=str(e))
    lists.sync_table(con)
    with con:
        con.execute("UPDATE lists SET title = ?, cover_url = ? WHERE key = ? AND NOT fetched", (name, image, key))
    request.app.state.worker.trigger("sync" if service == "spotify" else "soundcloud")
    return back("/sources", ok=f"Following {name}. Its songs are fetched now.")


@router.post("/sources/options")
def list_options(con: DB, removed_playlists: Annotated[bool, Form()] = False) -> RedirectResponse:
    sources.set_removed_playlists(con, removed_playlists)
    return back("/sources", ok="Saved.")
