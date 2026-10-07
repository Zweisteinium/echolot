"""Upload by hand: files for missing songs or better copies of songs in the library, checked in a dialog
(Missing, the Overview, the list pages), imported on confirm (library/upload). For admins and users with
the review permission, for the songs they see."""

import contextlib
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from echolot.library import filing, upload
from echolot.settings import options
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


def _song(request: Request, con: DB, key: str) -> object | None:
    if not key:
        return None
    found = stats.upload_songs(con, stats.scope(request.state.user), key)
    return found[0] if found else None


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
    """The dialog, with a new batch: files are uploaded and checked the moment they are picked or dropped,
    each on its own (upload_file); one Import for all once every one is done. For one song (`song`), or any
    of yours: missing ones, better copies."""
    batch = upload.start(_paths(request))
    return page(request, "_upload.html", step="pick", batch=batch, song=_song(request, con, song))


@router.post("/missing/upload/{batch}/file", response_class=HTMLResponse)
def upload_file(
    request: Request, con: DB, batch: str, file: Annotated[UploadFile, File()], song: Annotated[str, Form()] = ""
) -> HTMLResponse:
    """One file of the batch: saved, checked, its song found (the one asked for, else the one it names; no
    song: not imported) and how it fits; its row for the dialog."""
    keep_hires = options.get(con, options.Files).keep_hires
    try:
        f = upload.add(_paths(request), batch, file.filename or "", file.file, keep_hires)
    except ValueError as e:
        raise HTTPException(404, "That upload is gone (imported, cancelled, or older than a day).") from e
    songs = _songs(request, con)
    key = song if song in songs else upload.guess(f, list(songs.values()))
    match = songs.get(key or "") if not f.error else None
    copy = upload.compare(_paths(request), f, match) if match else None  # a song you have: better?
    fit = upload.fit(con, f, match)
    go = upload.importable(f, match, copy)
    return page(request, "_upload_row.html", f=f, song=match, labels=upload.labels(f, match, fit, copy), fit=fit, go=go)


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
