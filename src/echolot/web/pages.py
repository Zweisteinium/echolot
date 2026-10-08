"""The pages that show the library: overview, missing songs, a list and the activity."""

import sqlite3
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from echolot import db
from echolot.library import filing, history
from echolot.services import soundcloud, spotify
from echolot.web import charts, jobs, stats
from echolot.web.common import DB, page
from echolot.web.format import num, size

router = APIRouter(include_in_schema=False)


@router.get("/stats", response_class=HTMLResponse)
def overview(request: Request, con: DB) -> HTMLResponse:
    """The user's songs (an admin's, or everyone's as they chose), with the library's size besides."""
    o, vault = stats.overview(con, stats.scope(request.state.user)), request.app.state.vault
    uid = request.state.user.id
    connected = vault.has(con, spotify.refresh_name(uid)) or vault.has(con, soundcloud.token_name(uid))
    donut, running = (
        charts.donut(o["tiers"], size=160, width=22),
        jobs.status(request, con),
    )  # the library's files by quality, and the songs missing
    growth = _growth(con, stats.scope(request.state.user), o)
    return page(request, "overview.html", nav="stats", o=o, donut=donut, growth=growth, connected=connected, **running)


def _growth(con: sqlite3.Connection, uid: int | None, o: dict) -> dict[str, charts.Growth | None]:
    """The songs in the library over time, as files (a recording several list entries share counts once:
    the user's, or everyone's), and the library's size (everyone's); each ends with the value of now."""
    now = history.utc(datetime.now())
    rebuilt = db.get_meta(con, history.REBUILT)
    return {
        "songs": charts.growth(
            [*_files(con, uid, o), (now, o["files"])],
            lambda v: f"{num(round(v))} songs",
            lambda v: num(round(v)),
            rebuilt,
        ),
        "size": charts.growth(
            [*history.totals(con, "library_bytes"), (now, o["library_size"])],
            size,
            lambda v: "0" if not v else f"{v / 1e9:g} GB" if v >= 1e9 else f"{v / 1e6:g} MB",
            rebuilt,
        ),
    }


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
    return page(request, "missing.html", nav="missing", songs=songs, close=close, lists=lists, selected=list_key)


@router.get("/lists/{key:path}", response_class=HTMLResponse)
def list_page(request: Request, con: DB, key: str) -> HTMLResponse:
    lst = stats.get_list(con, key, stats.scope(request.state.user))
    if lst is None:  # (or not theirs)
        raise HTTPException(404, "no such list")
    songs = stats.list_songs(con, key)
    same = stats.same_as(songs)  # (the quality bar counts such a song once)
    tiers = stats.tiers_of([s for s in songs if s["position"] not in same])
    return page(request, "list.html", nav="playlists", lst=lst, songs=songs, same=same, tiers=tiers)


@router.get("/activity", response_class=HTMLResponse)
def activity(request: Request, con: DB, kind: str = "") -> HTMLResponse:
    entries = stats.activity(con, kind, uid=stats.scope(request.state.user))
    return page(request, "activity.html", nav="activity", entries=entries, kind=kind)
