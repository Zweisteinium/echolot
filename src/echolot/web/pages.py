"""Library pages: overview and its graphs, Missing, Buy, a list, History (the activity)."""

import sqlite3
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from echolot.library import catalog, filing, history, origin, recording, unreleased
from echolot.settings import options
from echolot.web import cache, charts, discogs, stats
from echolot.web.common import DB, back, page
from echolot.web.format import num, size

router = APIRouter(include_in_schema=False)


@router.get("/stats")
def old_overview(request: Request) -> RedirectResponse:
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/library{query}", status_code=301)


@router.get("/library", response_class=HTMLResponse)
def overview(request: Request, con: DB) -> HTMLResponse:
    uid = stats.scope(request.state.user)
    o = cache.get(con, stats.overview, uid)
    lists = cache.get(con, stats.lists, uid)  # Quality by list
    record = any(r["service"] == "spotify" for r in lists)  # Add music: Record
    rings = {
        "files": charts.donut(o["tiers"], size=160, width=22),
        "songs": charts.donut(o["song_tiers"], size=160, width=22),
        "best": charts.donut(best := cache.get(con, stats.best_tiers, uid), size=160, width=22),
    }
    return page(
        request,
        "overview.html",
        nav="library",
        o=o,
        rings=rings,
        best=best,
        best_hints={k: hint for k, _, hint in stats.BEST_TIERS},
        tier_hints=stats.TIER_HINTS,
        record=record,
        lists=lists,
    )


@router.get("/library/graphs", response_class=HTMLResponse)
def graphs(request: Request, con: DB) -> HTMLResponse:
    """The graphs fragment for ?range=7d|30d|90d|all or ?from=&to= (UTC); a range's are shared (cache)."""
    uid, (chosen, start, stop) = stats.scope(request.state.user), _window(request.query_params)
    o = cache.get(con, stats.overview, uid)
    graphs = cache.get(con, _ranged, uid, chosen) if chosen else _graphs(con, uid, chosen, start, stop)
    return page(request, "_graphs.html", o=o, **graphs)


def library_tabs(con: sqlite3.Connection, uid: int | None) -> dict[str, int | None]:
    """The sidebar's counts of things to do: Missing (as its page lists them), Buy, and Discogs (None while Discogs
    is off: no entry)."""
    missing, buy = len(stats.missing(con, uid=uid)), len(stats.buy(con, uid))
    return {"missing": missing, "buy": buy, "discogs": discogs.to_add(con, uid)}


RANGES = {"7d": ("7 d", 7), "30d": ("30 d", 30), "90d": ("3 mo", 90), "all": ("All", None)}


def _window(query: Any) -> tuple[str, str, str]:
    """(range, from, to): a RANGES key, or "" for a stretch dragged on the graph (from, to in UTC).
    from "" means from the start, to "" until now."""
    if (start := _stamp(query.get("from"))) and (stop := _stamp(query.get("to"))) and start < stop:
        return "", start, stop
    chosen = query.get("range") if query.get("range") in RANGES else "all"
    days = RANGES[chosen][1]
    return chosen, history.utc(datetime.now() - timedelta(days=days)) if days else "", ""


def _stamp(value: str | None) -> str:
    try:
        return history.utc(datetime.fromisoformat(value)) if value else ""
    except ValueError:
        return ""


def _span(start: str, stop: str) -> str:
    """A dragged stretch in local time: "12 Oct – 19 Oct", with hours when at most two days."""
    a, b = (datetime.fromisoformat(ts).astimezone() for ts in (start, stop))
    if b - a <= timedelta(days=2):
        return f"{a.day} {a:%b %H:%M} – {b.day} {b:%b %H:%M}"
    return f"{a.day} {a:%b} – {b.day} {b:%b}"


def _ranged(con: sqlite3.Connection, uid: int | None, chosen: str) -> dict[str, Any]:
    """The graphs of a range chip (RANGES)."""
    return _graphs(con, uid, *_window({"range": chosen}))


def _crop(points: list[tuple[str, Any]], start: str, stop: str) -> list[tuple[str, Any]]:
    """The points from `start` to `stop` ("": open) plus one on each side, so the line reaches the edges
    and a stretch between two points still shows."""
    inside = [n for n, (ts, _) in enumerate(points) if (not start or ts >= start) and (not stop or ts <= stop)]
    if not inside:
        before = [n for n, (ts, _) in enumerate(points) if start and ts < start]
        inside = before[-1:]
    if not inside:
        return []
    return points[max(inside[0] - 1, 0) if start else 0 : inside[-1] + 2]


def _graphs(con: sqlite3.Connection, uid: int | None, chosen: str, start: str, stop: str) -> dict[str, Any]:
    """The library's files (always everyone's) with the songs on the lists beside, library size (everyone's
    too) and quality over the stretch; each series ends with the current value."""
    o, now = cache.get(con, stats.overview, uid), history.utc(datetime.now())
    files = _files(con, uid, o)
    library = [*history.totals(con, "library_files"), (now, o["library_files"])]
    listed = history.totals(con, "songs_wanted") if uid is None else history.totals(con, "user_songs_wanted", str(uid))
    listed.append((now, o["wanted"]))
    sizes = [*history.totals(con, "library_bytes"), (now, o["library_size"])]
    quality = [*_quality(con, uid, dict(files)), (now, {k: n for k, _, n in o["tiers"]})]
    return {
        "growth": {
            "songs": charts.growth(
                _crop(library, start, stop),
                lambda v: f"{num(round(v))} files",
                lambda v: num(round(v)),
                also=listed,
                also_value=lambda v: f"{num(round(v))} songs on the lists",
            ),
            "size": charts.growth(
                _crop(sizes, start, stop),
                size,
                lambda v: "0" if not v else f"{v / 1e9:g} GB" if v >= 1e9 else f"{v / 1e6:g} MB",
            ),
            "quality": charts.stacked(_crop(quality, start, stop), catalog.TIERS),
        },
        "window": {"range": chosen, "ranges": RANGES, "zoomed": _span(start, stop) if not chosen else ""},
    }


def _quality(con: sqlite3.Connection, uid: int | None, files: dict[str, float]) -> list[tuple[str, dict[str, float]]]:
    """Files per quality tier over time; for a user, their songs per tier scaled to their file count."""
    if uid is None:
        return [(ts, _tiers(n)) for ts, n in history.keyed(con, "library_files_by_quality")]
    out = []
    for ts, n in history.keyed(con, "user_songs_by_quality", f"{uid}:"):
        scale = files[ts] / sum(n.values()) if files.get(ts) and sum(n.values()) else 1
        out.append((ts, {k: v * scale for k, v in _tiers(n).items()}))
    return out


def _tiers(n: dict[str, float]) -> dict[str, float]:
    """A snapshot on catalog.TIERS: older snapshots have a "fake" tier, counted as lossy-high (where most
    fake FLACs are by their source)."""
    n = dict(n)
    if fake := n.pop("fake", 0):
        n["lossy-high"] = n.get("lossy-high", 0) + fake
    return n


def _files(con: sqlite3.Connection, uid: int | None, o: dict) -> list[tuple[str, float]]:
    """Library files over time. A user's are counted since 2026-10-07; before that, their songs in the
    library scaled to their files at the first count (or now)."""
    if uid is None:
        return history.totals(con, "library_files")
    files = history.totals(con, "user_library_files", str(uid))
    songs = history.totals(con, "user_songs_in_library", str(uid))
    start, had = files[0] if files else (None, o["files"])
    then = dict(songs).get(start) if start else o["have"]
    ratio = had / then if then else 1
    return [(ts, v * ratio) for ts, v in songs if start is None or ts < start] + files


@router.get("/missing", response_class=HTMLResponse)
def missing(
    request: Request, con: DB, list_key: Annotated[str, Query(alias="list")] = "", show: str = ""
) -> HTMLResponse:
    """The missing songs (of a list); `show`: only those still searched for (searching) or seemingly wiped off the
    internet (wiped: gone from Spotify and searched for everywhere in vain)."""
    library = request.app.state.settings.library_dir
    uid = stats.scope(request.state.user)
    found = stats.missing(con, list_key or None, filing.Paths(library.parent) if library else None, uid)
    wiped = sum(1 for s in found if s["wiped"])
    shown = {"wiped": True, "searching": False}.get(show)
    songs = found if shown is None else [s for s in found if bool(s["wiped"]) is shown]
    lists, close = cache.get(con, stats.lists, uid), stats.close_matches(con, uid)
    marks, show = recording.marks(con, request.state.user.id, songs), show if shown is not None else ""
    extra = {"close": close, "lists": lists, "selected": list_key, "marks": marks, "show": show}
    counts = {"all": len(found), "wiped": wiped, "searching": len(found) - wiped}
    return page(request, "missing.html", nav="missing", songs=songs, counts=counts, **extra)


@router.get("/song", response_class=HTMLResponse)
def song_page(request: Request, con: DB, key: str) -> HTMLResponse:
    """Everything about a song: its panel (htmx, in the song dialog), else a page of its own."""
    library = request.app.state.settings.library_dir
    paths = filing.Paths(library.parent) if library else None
    s = stats.song(con, key, stats.scope(request.state.user), paths)
    if s is None:
        raise HTTPException(404, "Not a song of yours.")
    extra = {"s": s, "lyric_states": stats.LYRICS_STATES}
    if request.headers.get("hx-request"):
        return page(request, "_song_panel.html", **extra)
    return page(request, "song.html", nav="library", **extra)


@router.get("/missing/tried", response_class=HTMLResponse)
def missing_tried(request: Request, con: DB, song: str) -> HTMLResponse:
    """What the searches of a missing song tried: loaded when its row on the Missing page opens."""
    found = stats.missing(con, uid=stats.scope(request.state.user), song=song)
    if not found:
        raise HTTPException(404, "Not a missing song of yours.")
    return page(request, "_missing_tried.html", s=found[0])


@router.get("/buy", response_class=HTMLResponse)
def buy_page(request: Request, con: DB) -> HTMLResponse:
    songs = stats.buy(con, stats.scope(request.state.user))
    last = con.execute("SELECT * FROM jobs WHERE name = 'bandcamp'").fetchone()
    runs, asked = request.app.state.worker.state()
    running = "bandcamp" in runs or "bandcamp" in asked
    gates = stats.gates_to_get(con)
    gates_last = con.execute("SELECT * FROM jobs WHERE name = 'hypeddit'").fetchone()
    gates_running = "hypeddit" in runs or "hypeddit" in asked
    has_email = bool(options.get(con, options.Files).gate_email)
    return page(
        request,
        "buy.html",
        nav="buy",
        songs=songs,
        last=last,
        running=running,
        gates=gates,
        gates_last=gates_last,
        gates_running=gates_running,
        has_email=has_email,
    )


@router.post("/buy/refresh")
def buy_refresh(request: Request) -> RedirectResponse:
    """Admins only: it asks Bandcamp for every page."""
    if not request.app.state.worker.trigger("bandcamp"):
        return back("/buy", error="Refresh could not start.")
    return back("/buy", ok="Refresh starts in a moment. Reload the page in a few minutes.")


@router.post("/buy/gates")
def buy_gates(request: Request) -> RedirectResponse:
    """Admins only: get the Hypeddit free downloads (jobs/bandcamp.gates)."""
    if not request.app.state.worker.trigger("hypeddit"):
        return back("/buy", error="Getting the free downloads could not start.")
    return back("/buy", ok="Getting the free downloads starts in a moment. Reload the page in a few minutes.")


ROWS = 300  # a list's songs on its page at first and per scroll (an unreleased list, ordered by dragging: all)
From = Annotated[int, Query(alias="from", ge=0)]
SHOW = {"missing": "Missing", "lossy": "Lossy"}  # a list page's filters besides the words: ?show=
FILTER_FROM = 20  # songs: a longer list's page has the filter


@router.get("/lists/{key:path}", response_class=HTMLResponse)
def list_page(request: Request, con: DB, key: str, start: From = 0, q: str = "", show: str = "") -> HTMLResponse:
    """A list's page with its first ROWS songs; ?from=: the next ROWS rows (htmx, as the page scrolls); ?q= and
    ?show= filter them (words of the artist or title, SHOW), the rows alone for the filter's htmx request."""
    lst = stats.get_list(con, key, stats.scope(request.state.user))
    if lst is None:  # or not theirs
        raise HTTPException(404, "No such list.")
    songs = cache.get(con, stats.list_songs, key)
    same = stats.same_as(songs)  # counted once in the quality bar
    chosen = stats.filter_songs(songs, q, show if show in SHOW else "")
    stop = len(chosen) if lst["service"] == "local" else start + ROWS
    shown = chosen[start:stop]
    marks = recording.marks(con, request.state.user.id, shown) if lst["service"] == "spotify" else {}
    marks |= {k: recording.Mark("original", why) for k, why in origin.marks(con, [s["key"] for s in shown]).items()}
    offers = stats.offers(con) if lst["service"] == "soundcloud" else {}
    links = stats.links(con) if lst["service"] == "soundcloud" else {}
    mine = unreleased.owns(con, request.state.user.id, key)  # an Unreleased list of theirs: they manage it
    alone = set(unreleased.only_here(con, request.state.user.id, key)) if mine else set()
    rows = {"lst": lst, "songs": shown, "same": same, "marks": marks, "offers": offers, "links": links, "mine": mine}
    filters = {"q": q, "show": show if show in SHOW else "", "shows": SHOW, "filter": len(songs) > FILTER_FROM}
    rows |= {"alone": alone, "more": stop if stop < len(chosen) else None, **filters}
    if start or request.headers.get("hx-target") == "list-rows":
        return page(request, "_list_rows.html", **rows)
    tiers = stats.tiers_of([s for s in songs if s["position"] not in same])
    others = [r for r in unreleased.lists(con, request.state.user.id) if r["key"] != key] if mine else []
    have = sum(1 for s in songs if s["file"])
    extra = {"total": len(songs), "have": have, "tiers": tiers, "others": others, "drop": unreleased.DROP}
    return page(request, "list.html", nav="playlists", **rows, **extra)


@router.get("/activity", response_class=HTMLResponse)
def activity(request: Request, con: DB, kind: str = "", q: str = "") -> HTMLResponse:
    entries = stats.activity(con, kind, uid=stats.scope(request.state.user), query=q.strip())
    return page(request, "activity.html", nav="activity", entries=entries, kind=kind, q=q)
