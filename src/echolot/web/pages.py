"""The pages that show the library: overview, missing songs, a list and the activity."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from echolot.library import filing
from echolot.services import soundcloud, spotify
from echolot.web import charts, jobs, stats
from echolot.web.common import DB, page

router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
def overview(request: Request, con: DB) -> HTMLResponse:
    if not request.state.user.admin:  # their own lists come with per-user accounts; meanwhile the library's size
        files, songs = (con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("files", "wanted"))
        return page(request, "welcome.html", nav="overview", files=files, songs=songs)
    o, vault = stats.overview(con), request.app.state.vault
    uid = request.state.user.id
    connected = vault.has(con, spotify.refresh_name(uid)) or vault.has(con, soundcloud.token_name(uid))
    donut, running = (
        charts.donut(o["tiers"]),
        jobs.status(request, con),
    )  # the library's files by quality, and the songs missing
    return page(request, "overview.html", nav="overview", o=o, donut=donut, connected=connected, **running)


@router.get("/missing", response_class=HTMLResponse)
def missing(request: Request, con: DB, list_key: Annotated[str, Query(alias="list")] = "") -> HTMLResponse:
    library = request.app.state.settings.library_dir
    songs = stats.missing(con, list_key or None, filing.Paths(library.parent) if library else None)
    lists, close = stats.lists(con), stats.close_matches(con)
    return page(request, "missing.html", nav="missing", songs=songs, close=close, lists=lists, selected=list_key)


@router.get("/lists/{key:path}", response_class=HTMLResponse)
def list_page(request: Request, con: DB, key: str) -> HTMLResponse:
    lst = stats.get_list(con, key)
    if lst is None:
        raise HTTPException(404, "no such list")
    songs = stats.list_songs(con, key)
    return page(request, "list.html", nav="overview", lst=lst, songs=songs, tiers=stats.tiers_of(songs))


@router.get("/activity", response_class=HTMLResponse)
def activity(request: Request, con: DB, kind: str = "") -> HTMLResponse:
    return page(request, "activity.html", nav="activity", entries=stats.activity(con, kind), kind=kind)
