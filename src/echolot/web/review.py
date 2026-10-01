"""The review page: downloads filed on a probable match (Please confirm) and kept rejected ones (Worth a
look), with a player; a decision is applied by the library job after a short undo window
(library/review.py). Also: a close match's song searched again (from the Missing page)."""

import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from echolot.library import catalog, review
from echolot.settings.sources import ConfigError
from echolot.web.common import DB, back, page

router = APIRouter(include_in_schema=False)
Field = Annotated[str, Form()]


def music_dir(request: Request) -> Path:
    library = request.app.state.settings.library_dir
    if not library:
        raise HTTPException(404, "no library configured (ECHOLOT_LIBRARY_DIR)")
    return library.parent


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request, con: DB) -> HTMLResponse:
    found = review.items(con, music_dir(request))
    songs = {kind: review.groups(rows) for kind, rows in found.items()}
    return page(request, "review.html", nav="review", songs=songs, items=found)


def _answer(
    request: Request, con: sqlite3.Connection, event_id: int, ok: str = "", error: str = "", name: str = ""
) -> Response:
    """htmx: the song's card as it is now (gone when nothing is up for review), an error next to the
    download it is about (with the close match name typed); otherwise back to the page."""
    if not request.headers.get("hx-request"):
        return back("/review", **({"error": error} if error else {"ok": ok}))
    g = review.find_group(con, music_dir(request), event_id)
    if g is None:
        return HTMLResponse("")
    return page(request, "_review_group.html", g=g, card_error=error, error_id=event_id, tried=name)


@router.post("/review/{event_id}")
def review_decide(request: Request, con: DB, event_id: int, decision: Field, name: Field = "") -> Response:
    try:
        item = review.decide(con, music_dir(request), event_id, decision, name)
    except ConfigError as e:
        return _answer(request, con, event_id, error=str(e), name=name)
    song, label = f"{item.event['artist']} – {item.event['title']}", review.LABELS[decision]
    return _answer(request, con, event_id, ok=f"{song}: {label}. Applied within a few minutes (Revert until then).")


@router.post("/review/{event_id}/discard-all")
def review_discard_all(request: Request, con: DB, event_id: int) -> Response:
    """No match for every download of the song that has no decision yet."""
    try:
        g = review.discard_all(con, music_dir(request), event_id)
    except ConfigError as e:
        return _answer(request, con, event_id, error=str(e))
    song = f"{g.lead.event['artist']} – {g.lead.event['title']}"
    return _answer(request, con, event_id, ok=f"{song}: No match for all. Applied within a few minutes.")


@router.post("/review/{event_id}/revert")
def review_revert(request: Request, con: DB, event_id: int) -> Response:
    try:
        item = review.revert(con, music_dir(request), event_id)
    except ConfigError as e:
        return _answer(request, con, event_id, error=str(e))
    song = f"{item.event['artist']} – {item.event['title']}"
    return _answer(request, con, event_id, ok=f"{song}: {item.label} taken back.")


@router.get("/review/{event_id}/audio")
def review_audio(request: Request, con: DB, event_id: int) -> FileResponse:
    item = review.find(con, music_dir(request), event_id)
    if item is None:
        raise HTTPException(404, "not up for review")
    return FileResponse(item.file)


@router.post("/songs/{key:path}/search")
def close_search(con: DB, key: str) -> Response:
    """A song linked to a close match: look for the perfect match again (the close match stays a file)."""
    song = con.execute("SELECT artist, title FROM songs WHERE key = ? AND close_match", (key,)).fetchone()
    if song is None:
        return back("/missing", error="That song is not linked to a close match.")
    review.search_again(con, key)
    catalog.match_songs(con)
    return back("/missing", ok=f"{song['artist']} – {song['title']}: searched again for the perfect match.")
