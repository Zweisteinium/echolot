"""The pages that show the library: overview, missing songs, a list and the activity."""

import sqlite3
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from echolot.library import filing, history, recording
from echolot.services import soundcloud, spotify
from echolot.web import charts, discogs, stats
from echolot.web.common import DB, back, page
from echolot.web.format import num, size

router = APIRouter(include_in_schema=False)


@router.get("/stats")
def old_overview(request: Request) -> RedirectResponse:
    """The Library page was Stats (0.10)."""
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/library{query}", status_code=301)


@router.get("/library", response_class=HTMLResponse)
def overview(request: Request, con: DB) -> HTMLResponse:
    """The user's songs (an admin's, or everyone's as they chose), with the library's size besides."""
    uid = stats.scope(request.state.user)
    o, vault = stats.overview(con, uid), request.app.state.vault
    connected = vault.has(con, spotify.refresh_name(request.state.user.id)) or vault.has(
        con, soundcloud.token_name(request.state.user.id)
    )
    rings = {  # the library's files by quality and the songs missing; the songs by how near their best they are
        "quality": charts.donut(o["tiers"], size=160, width=22),
        "best": charts.donut(best := stats.best_tiers(con, uid), size=160, width=22),
    }
    return page(
        request,
        "overview.html",
        nav="library",
        o=o,
        rings=rings,
        best=best,
        best_hints={k: hint for k, _, hint in stats.BEST_TIERS},
        connected=connected,
        tabs=library_tabs(con, uid),
        **_graphs(con, uid, o, *_window(request.query_params)),
    )


@router.get("/library/graphs", response_class=HTMLResponse)
def graphs(request: Request, con: DB) -> HTMLResponse:
    """The Library page's graphs for a stretch of time (?range=7d|30d|90d|all, or ?from=&to= in UTC)."""
    uid = stats.scope(request.state.user)
    return page(
        request,
        "_graphs.html",
        o=(o := stats.overview(con, uid)),
        **_graphs(con, uid, o, *_window(request.query_params)),
    )


def library_tabs(con: sqlite3.Connection, uid: int | None) -> dict[str, int | None]:
    """The counts on the Library page's tabs: the songs missing, those to buy on Bandcamp (the Buy page), the
    releases Discogs lacks (None while Discogs is off: no tab)."""
    mine, args = stats._mine(uid)
    missing = con.execute(f"SELECT count(*) FROM wanted s WHERE s.file IS NULL AND {mine}", args).fetchone()[0]
    return {"missing": missing, "buy": len(stats.buy(con, uid)), "discogs": discogs.to_add(con, uid)}


RANGES = {"7d": ("7 d", 7), "30d": ("30 d", 30), "90d": ("3 mo", 90), "all": ("All", None)}  # the graphs' chips


def _window(query: Any) -> tuple[str, str, str]:
    """(range, from, to): a chip of RANGES, or "" for a stretch chosen on the graph (from and to, UTC); from
    is "" for all of it, to "" for until now."""
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
    """A stretch dragged on a graph, in local time: "12 Oct – 19 Oct", with the hours when it is short."""
    a, b = (datetime.fromisoformat(ts).astimezone() for ts in (start, stop))
    if b - a <= timedelta(days=2):
        return f"{a.day} {a:%b %H:%M} – {b.day} {b:%b %H:%M}"
    return f"{a.day} {a:%b} – {b.day} {b:%b}"


def _crop(points: list[tuple[str, Any]], start: str, stop: str) -> list[tuple[str, Any]]:
    """The points from `start` to `stop` ("": open), with the one before and the one after: the line
    reaches the edges, and a stretch between two points still shows."""
    inside = [n for n, (ts, _) in enumerate(points) if (not start or ts >= start) and (not stop or ts <= stop)]
    if not inside:
        before = [n for n, (ts, _) in enumerate(points) if start and ts < start]
        inside = before[-1:]
    if not inside:
        return []
    return points[max(inside[0] - 1, 0) if start else 0 : inside[-1] + 2]


def _graphs(con: sqlite3.Connection, uid: int | None, o: dict, chosen: str, start: str, stop: str) -> dict[str, Any]:
    """The songs in the library over time, as files (a recording several list entries share counts once:
    the user's, or everyone's), the library's size (everyone's) and the files' quality with the songs missing
    (as the ring shows them), for the stretch shown; each ends with the value of now."""
    now = history.utc(datetime.now())
    files = _files(con, uid, o)
    songs = [*files, (now, o["files"])]
    sizes = [*history.totals(con, "library_bytes"), (now, o["library_size"])]
    quality = [*_quality(con, uid, dict(files)), (now, {k: n for k, _, n in o["tiers"]})]
    return {
        "growth": {
            "songs": charts.growth(
                _crop(songs, start, stop), lambda v: f"{num(round(v))} songs", lambda v: num(round(v))
            ),
            "size": charts.growth(
                _crop(sizes, start, stop),
                size,
                lambda v: "0" if not v else f"{v / 1e9:g} GB" if v >= 1e9 else f"{v / 1e6:g} MB",
            ),
            "quality": charts.stacked(_crop(quality, start, stop), [(k, label) for k, label in stats.TIERS]),
        },
        "window": {"range": chosen, "ranges": RANGES, "zoomed": _span(start, stop) if not chosen else ""},
    }


def _quality(con: sqlite3.Connection, uid: int | None, files: dict[str, float]) -> list[tuple[str, dict[str, float]]]:
    """The library files per quality tier over time with the songs missing (everyone's); a user's: their songs
    per tier, taken in the proportion of their files then (as the songs graph counts them)."""
    if uid is None:
        missing = dict(history.totals(con, "songs_missing"))
        return [(ts, {**n, "missing": missing.get(ts, 0)}) for ts, n in history.keyed(con, "library_files_by_quality")]
    missing = dict(history.totals(con, "user_songs_missing", str(uid)))
    out = []
    for ts, n in history.keyed(con, "user_songs_by_quality", f"{uid}:"):
        scale = files[ts] / sum(n.values()) if files.get(ts) and sum(n.values()) else 1
        out.append((ts, {**{k: v * scale for k, v in n.items()}, "missing": missing.get(ts, 0)}))
    return out


def _files(con: sqlite3.Connection, uid: int | None, o: dict) -> list[tuple[str, float]]:
    """The library files over time (everyone's), or the user's: counted since 2026-10-07, before that their
    songs in the library scaled to the files they had at the first count (or have now)."""
    if uid is None:
        return history.totals(con, "library_files")
    files = history.totals(con, "user_library_files", str(uid))
    songs = history.totals(con, "user_songs_in_library", str(uid))
    start, had = files[0] if files else (None, o["files"])
    then = dict(songs).get(start) if start else o["have"]
    ratio = had / then if then else 1
    return [(ts, v * ratio) for ts, v in songs if start is None or ts < start] + files


@router.get("/missing", response_class=HTMLResponse)
def missing(request: Request, con: DB, list_key: Annotated[str, Query(alias="list")] = "") -> HTMLResponse:
    library = request.app.state.settings.library_dir
    uid = stats.scope(request.state.user)
    songs = stats.missing(con, list_key or None, filing.Paths(library.parent) if library else None, uid)
    lists, close = stats.lists(con, uid), stats.close_matches(con, uid)
    marks = recording.marks(con, request.state.user.id, songs)
    return page(
        request,
        "missing.html",
        nav="missing",
        songs=songs,
        close=close,
        lists=lists,
        selected=list_key,
        marks=marks,
        tabs=library_tabs(con, uid),
    )


@router.get("/buy", response_class=HTMLResponse)
def buy_page(request: Request, con: DB) -> HTMLResponse:
    """Songs Bandcamp sells lossless, with their price and a link (jobs/bandcamp); an upload takes in the file."""
    songs = stats.buy(con, stats.scope(request.state.user))
    last = con.execute("SELECT * FROM jobs WHERE name = 'bandcamp'").fetchone()
    runs, asked = request.app.state.worker.state()
    running = "bandcamp" in runs or "bandcamp" in asked
    tabs = library_tabs(con, stats.scope(request.state.user))
    return page(request, "buy.html", nav="buy", songs=songs, last=last, running=running, tabs=tabs)


@router.post("/buy/refresh")
def buy_refresh(request: Request) -> RedirectResponse:
    """Look at every lossy SoundCloud song's Bandcamp link again (admins: it asks Bandcamp for each page)."""
    if not request.app.state.worker.trigger("bandcamp"):
        return back("/buy", error="Refresh could not start.")
    return back("/buy", ok="Refresh starts in a moment; reload the page in a few minutes.")


@router.get("/lists/{key:path}", response_class=HTMLResponse)
def list_page(request: Request, con: DB, key: str) -> HTMLResponse:
    lst = stats.get_list(con, key, stats.scope(request.state.user))
    if lst is None:  # (or not theirs)
        raise HTTPException(404, "no such list")
    songs = stats.list_songs(con, key)
    same = stats.same_as(songs)  # (the quality bar counts such a song once)
    tiers = stats.tiers_of([s for s in songs if s["position"] not in same])
    marks = recording.marks(con, request.state.user.id, songs) if lst["service"] == "spotify" else {}
    offers = stats.offers(con) if lst["service"] == "soundcloud" else {}  # (Bandcamp sells these)
    return page(
        request, "list.html", nav="library", lst=lst, songs=songs, same=same, tiers=tiers, marks=marks, offers=offers
    )


@router.get("/activity", response_class=HTMLResponse)
def activity(request: Request, con: DB, kind: str = "") -> HTMLResponse:
    entries = stats.activity(con, kind, uid=stats.scope(request.state.user))
    return page(request, "activity.html", nav="activity", entries=entries, kind=kind)
