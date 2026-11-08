"""Review page: probable matches (Please confirm) and kept rejects (Worth a look); the library job applies a
decision after an undo window (library/review). Also the Missing page's close-match actions.

The review permission covers downloads of the user's own songs; admins decide everyone's and may take back
(override) other users' decisions within 30 days."""

import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from echolot.library import catalog, filing, review
from echolot.settings.sources import ConfigError
from echolot.web import stats
from echolot.web.common import DB, back, page

router = APIRouter(include_in_schema=False)
Field = Annotated[str, Form()]


def music_dir(request: Request) -> Path:
    library = request.app.state.settings.library_dir
    if not library:
        raise HTTPException(404, "no library configured (ECHOLOT_LIBRARY_DIR)")
    return library.parent


def _own(request: Request, con: sqlite3.Connection, event_id: int) -> None:
    """403 unless an admin or the download is for one of the user's songs."""
    user = request.state.user
    if not user.admin and not review.theirs(con, user.id, event_id):
        raise HTTPException(403, "That download is for a song of another user.")


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request, con: DB) -> HTMLResponse:
    user = request.state.user
    found = review.items(con, music_dir(request), stats.scope(user))
    songs = {kind: review.groups(rows) for kind, rows in found.items()}
    others = review.by_others(con) if user.admin else []
    hidden = _hidden(music_dir(request), found) if user.admin else 0
    extra = {"others": others, "hidden": hidden, "labels": review.LABELS}
    return page(request, "review.html", nav="review", songs=songs, items=found, **extra)


def _hidden(music: Path, found: dict[str, list[review.Item]]) -> int:
    """Files in inbox/review no longer up for review (song found since, or far off in length)."""
    folder = music / "inbox" / "review"
    shown = {i.file for rows in found.values() for i in rows}
    return sum(1 for f in folder.rglob("*") if f.is_file() and f not in shown) if folder.is_dir() else 0


def _answer(
    request: Request, con: sqlite3.Connection, event_id: int, ok: str = "", error: str = "", name: str = ""
) -> Response:
    """htmx: the song's card as it is now ("" when nothing is left), an error by its download (keeping the
    typed close-match name); else a redirect."""
    if not request.headers.get("hx-request"):
        return back("/review", **({"error": error} if error else {"ok": ok}))
    g = review.find_group(con, music_dir(request), event_id)
    if g is None:
        return HTMLResponse("")
    return page(request, "_review_group.html", g=g, card_error=error, error_id=event_id, tried=name)


@router.post("/review/{event_id}")
def review_decide(request: Request, con: DB, event_id: int, decision: Field, name: Field = "") -> Response:
    _own(request, con, event_id)
    try:
        item = review.decide(con, music_dir(request), event_id, decision, name, request.state.user.id)
    except ConfigError as e:
        return _answer(request, con, event_id, error=str(e), name=name)
    song, label = f"{item.event['artist']} – {item.event['title']}", review.LABELS[decision]
    return _answer(request, con, event_id, ok=f"{song}: {label}. Applied within a few minutes (Revert until then).")


@router.post("/review/{event_id}/discard-all")
def review_discard_all(request: Request, con: DB, event_id: int) -> Response:
    """No match for every undecided download of the song."""
    _own(request, con, event_id)
    try:
        g = review.discard_all(con, music_dir(request), event_id)
    except ConfigError as e:
        return _answer(request, con, event_id, error=str(e))
    song = f"{g.lead.event['artist']} – {g.lead.event['title']}"
    return _answer(request, con, event_id, ok=f"{song}: No match for all. Applied within a few minutes.")


@router.post("/review/{event_id}/revert")
def review_revert(request: Request, con: DB, event_id: int) -> Response:
    _own(request, con, event_id)
    try:
        item = review.revert(con, music_dir(request), event_id)
    except ConfigError as e:
        return _answer(request, con, event_id, error=str(e))
    song = f"{item.event['artist']} – {item.event['title']}"
    return _answer(request, con, event_id, ok=f"{song}: {item.label} taken back.")


@router.get("/review/{event_id}/audio")
def review_audio(request: Request, con: DB, event_id: int) -> FileResponse:
    _own(request, con, event_id)
    item = review.find(con, music_dir(request), event_id)
    if item is None:
        raise HTTPException(404, "not up for review")
    return FileResponse(item.file)


@router.post("/review/{event_id}/override")
def review_override(request: Request, con: DB, event_id: int) -> Response:
    paths = filing.Paths(music_dir(request))
    try:
        result = review.override(con, paths, event_id, request.state.user.name)
    except ConfigError as e:
        return back("/review", error=str(e))
    return back("/review", ok=f"Taken back: {result}.")


@router.post("/songs/{key:path}/search")
def close_search(request: Request, con: DB, key: str) -> Response:
    """Search a close-matched song again for the perfect match (the close match stays)."""
    user = request.state.user
    if not user.admin and key not in review.songs_of(con, user.id):
        raise HTTPException(403, "That song is on another user's lists.")
    song = con.execute("SELECT artist, title FROM songs WHERE key = ? AND close_match", (key,)).fetchone()
    if song is None:
        return back("/missing", error="That song is not linked to a close match.")
    review.search_again(con, key)
    catalog.match_songs(con)
    return back("/missing", ok=f"{song['artist']} – {song['title']}: searched again for the perfect match.")


@router.post("/songs/{key:path}/close-remove")
def close_remove(request: Request, con: DB, key: str) -> Response:
    """Remove a song's close match (kept 30 days in inbox/replaced) and search the song again."""
    user = request.state.user
    if not user.admin and key not in review.songs_of(con, user.id):
        raise HTTPException(403, "That song is on another user's lists.")
    try:
        gone = review.remove_close(con, filing.Paths(music_dir(request)), key)
    except ConfigError as e:
        return back("/missing", error=str(e))
    catalog.match_songs(con)
    return back("/missing", ok=f"{gone} removed (kept 30 days in inbox/replaced); the song is searched again.")
