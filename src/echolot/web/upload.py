"""Upload by hand: files for missing songs or better copies of songs in the library, checked in a dialog
(Missing, the Overview, the list pages), imported on confirm (library/upload). For admins and users with
the review permission, for the songs they see."""

import contextlib
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from echolot.library import filing, upload
from echolot.web import stats
from echolot.web.common import DB, page

router = APIRouter(include_in_schema=False)


def _paths(request: Request) -> filing.Paths:
    library = request.app.state.settings.library_dir
    if library is None:
        raise HTTPException(409, "No library configured.")
    return filing.Paths(library.parent)


def _songs(request: Request, con: DB) -> dict[str, object]:
    return {s["key"]: s for s in stats.upload_songs(con, stats.scope(request.state.user))}


def _batch(request: Request, batch: str) -> list[upload.File]:
    try:
        found = upload.files(_paths(request), batch)
    except ValueError:
        found = []
    if not found:
        raise HTTPException(404, "That upload is gone (imported, cancelled, or older than a day).")
    return found


@router.get("/missing/upload", response_class=HTMLResponse)
def upload_form(request: Request, con: DB, song: str = "") -> HTMLResponse:
    """The dialog's first step: pick files (for one song, or any of yours: missing ones, better copies)."""
    songs = _songs(request, con)
    return page(request, "_upload.html", step="pick", song=songs.get(song))


@router.post("/missing/upload", response_class=HTMLResponse)
def upload_files(
    request: Request, con: DB, files: Annotated[list[UploadFile], File()], song: Annotated[str, Form()] = ""
) -> HTMLResponse:
    """The files are saved and checked; each one's song as detected and how it fits, for confirmation."""
    songs = _songs(request, con)
    picked = [f for f in files if f.filename]
    if not picked:
        return page(request, "_upload.html", step="pick", song=songs.get(song), upload_error="Choose a file first.")
    batch = upload.stage(_paths(request), [(f.filename or "", f.file) for f in picked])
    found = upload.files(_paths(request), batch)
    rows = []
    for f in found:  # each file's song: the one asked for, else the one it names (no song: not imported)
        key = song if song in songs else upload.guess(f, list(songs.values()))
        match = songs.get(key or "") if not f.error else None
        copy = upload.compare(_paths(request), f, match) if match else None  # a song you have: better?
        fit = upload.fit(con, f, match)
        rows.append((f, match, upload.labels(f, match, fit, copy), fit, upload.importable(f, match, copy)))
    n = sum(1 for *_, go in rows if go)
    return page(request, "_upload.html", step="check", batch=batch, rows=rows, importable=n)


@router.post("/missing/upload/{batch}/import", response_class=HTMLResponse)
async def upload_import(request: Request, con: DB, batch: str) -> HTMLResponse:
    """File every file that has a song (song_<n>: those left in the dialog, a missing song or a better copy)."""
    form = await request.form()
    songs = _songs(request, con)
    _batch(request, batch)
    chosen, remove = {}, {int(n[7:]) for n in form if n.startswith("remove_") and n[7:].isdigit()}
    for name, value in form.multi_items():
        if name.startswith("song_") and name[5:].isdigit() and isinstance(value, str) and value:
            if value not in songs:
                raise HTTPException(403, "That song is not one of your missing songs.")
            chosen[int(name[5:])] = value
    if not chosen:
        upload.cancel(_paths(request), batch)
        return page(request, "_upload.html", step="done", results=[], imported=[], none=True)
    vault = request.app.state.vault  # (filing reads and writes files and asks Spotify for the cover)
    results = await run_in_threadpool(upload.import_files, con, _paths(request), vault, batch, chosen, remove)
    imported = [key for _, key in results if key]  # (the page takes these songs off Missing)
    return page(request, "_upload.html", step="done", results=[line for line, _ in results], imported=imported)


@router.post("/missing/upload/{batch}/cancel")
def upload_cancel(request: Request, batch: str) -> Response:
    with contextlib.suppress(ValueError):  # no such batch: nothing to delete
        upload.cancel(_paths(request), batch)
    return HTMLResponse("")
