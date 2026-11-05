"""Unreleased songs (library/unreleased): the upload dialog for them (the upload dialog's script, its own rows:
each file named by hand, checked for a release first) and the Unreleased lists they go into (made in the
dialog; renamed, deleted and songs taken out on their list page). A file that is a release after all can be
imported as the released song instead (into Echolot · Wished, as web/upload does it). For admins and users with
the upload permission."""

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as FormFile

from echolot.library import catalog, upload
from echolot.library import unreleased as songs
from echolot.web import upload as uploads
from echolot.web.common import DB, back, page

router = APIRouter(include_in_schema=False)
NEW = "new"  # the list picker's choice of a new list
COVER_BYTES = 10 * 1024 * 1024  # the most a cover picture may have
JPEG = b"\xff\xd8\xff"  # how a JPEG starts (the covers embedded are JPEGs: audio.embed_cover)


def _user(request: Request) -> int:
    return request.state.user.id


@router.get("/unreleased/upload", response_class=HTMLResponse)
def upload_form(request: Request, con: DB, list_key: Annotated[str, Query(alias="list")] = "") -> HTMLResponse:
    """The dialog, with a new batch (the upload dialog's: files uploaded and checked one by one); its list
    picker set to `list` (one of the user's Unreleased lists), else their newest one, else a new one."""
    mine = songs.lists(con, _user(request))
    chosen = list_key if any(r["key"] == list_key for r in mine) else (mine[-1]["key"] if mine else NEW)
    batch = upload.start(uploads._paths(request))
    return page(request, "_unreleased.html", step="pick", batch=batch, lists=mine, chosen=chosen, new=NEW)


def _row(request: Request, con: DB, batch: str, f: upload.File, names: songs.Names) -> HTMLResponse:
    """A file's row: the names it is to have, the release check by them, whether an unreleased song has that
    name already."""
    paths = uploads._paths(request)
    n = names.clean()
    found = uploads.find_release(con, paths, f, n.artist, n.title) if not f.error and n.artist and n.title else None
    if found:
        found.mine = uploads.yours(found, uploads._songs(request, con))
    labels = upload.found_labels(f, found.release, found.have, found.failed, unreleased=True) if found else []
    if not f.error and not (n.artist and n.title):
        labels = [("Needs names", "warn", "Give it an artist and a title, then check again"), *upload.file_notes(f)]
    taken = not f.error and bool(n.artist and n.title) and songs.taken(paths, n)
    if taken:
        detail = "An unreleased song has these names already: put what differs into the title, e.g. (Custom Edit)"
        labels.append(("Name taken", "bad", detail))
    checks = upload.release_checks(found.release) if found and found.release else []
    context = {"f": f, "names": n, "found": found, "labels": labels, "checks": checks}
    return page(request, "_unreleased_row.html", batch=batch, **context)


@router.post("/unreleased/upload/{batch}/file", response_class=HTMLResponse)
def upload_file(request: Request, con: DB, batch: str, file: Annotated[UploadFile, File()]) -> HTMLResponse:
    """One file of the batch: saved and checked (hi-res kept: the file is the master), named by its tags or
    file name, checked for a release; its row."""
    try:
        f = upload.add(uploads._paths(request), batch, file.filename or "", file.file, keep_hires=True)
    except ValueError as e:
        raise HTTPException(404, "That upload is gone (imported, cancelled, or older than a day).") from e
    artist, title = upload.names(f)
    return _row(request, con, batch, f, songs.Names(artist, title, ""))


@router.post("/unreleased/upload/{batch}/{n}/check", response_class=HTMLResponse)
async def check_again(request: Request, con: DB, batch: str, n: int) -> HTMLResponse:
    """A file's row again, by the names typed into it."""
    form = await request.form()
    f = next((f for f in uploads._batch(request, batch) if f.n == n), None)
    if f is None:
        raise HTTPException(404, "That file is no longer in the upload.")
    names = songs.Names(*(str(form.get(f"{field}_{n}") or "") for field in ("artist", "title", "album")))
    return await run_in_threadpool(_row, request, con, batch, f, names)


def _cover(data: bytes) -> bytes | None:
    if not data:
        return None
    if len(data) > COVER_BYTES or not data.startswith(JPEG):
        raise HTTPException(400, "The cover must be a JPEG picture of at most 10 MB.")
    return data


def _import(
    request: Request,
    con: DB,
    batch: str,
    rows: dict[int, tuple[str, songs.Names]],
    target: str,
    name: str,
    note: str,
    cover: bytes | None,
) -> tuple[list[tuple[str, str | None]], str]:
    """File the batch's files: those kept unreleased into the list `target` (NEW: a new one named `name`),
    the releases into Echolot · Wished (web/upload.wish_files, upload.import_files); the rest of the batch is
    deleted. Returns a line per file and the list's key ('' when none was filed into one)."""
    paths, uid = uploads._paths(request), _user(request)
    by_n = {f.n: f for f in uploads._batch(request, batch)}
    local = {n: names for n, (value, names) in rows.items() if value == "local" and n in by_n}
    wished = {n: value.removeprefix(uploads.WISH) for n, (value, _) in rows.items() if value.startswith(uploads.WISH)}
    yours = {n: value for n, (value, _) in rows.items() if value != "local" and not value.startswith(uploads.WISH)}
    mine = uploads._songs(request, con) if yours else {}
    if any(key not in mine for key in yours.values()):
        raise HTTPException(403, "That song is not one of yours.")
    key, out = "", []
    if local:
        try:
            key = songs.create(con, uid, name) if target == NEW else target
        except songs.Refused as e:
            raise HTTPException(400, str(e)) from e
        if not songs.owns(con, uid, key):
            raise HTTPException(403, "That is not one of your Unreleased lists (any more).")
        out = [songs.add(con, paths, by_n[n], names, key, note, cover) for n, names in sorted(local.items())]
        catalog.match_songs(con)
    filed = any(k for _, k in out)
    chosen, lines = uploads.wish_files(request, con, wished)
    out += lines
    chosen |= yours
    if chosen:
        out += upload.import_files(con, paths, request.app.state.vault, batch, chosen)
    else:
        upload.cancel(paths, batch)
    return out, key if filed else ""


@router.post("/unreleased/upload/{batch}/import", response_class=HTMLResponse)
async def upload_import(request: Request, con: DB, batch: str) -> HTMLResponse:
    """Import the files left in the dialog: song_<n> 'local' (with artist_<n>, title_<n>, album_<n>) into the
    chosen list, 'wish:<id>' as that release, a song key as that song of yours; the note and the cover for the
    unreleased ones."""
    form = await request.form()
    rows = {}
    for field, value in form.multi_items():
        if field.startswith("song_") and field[5:].isdigit() and isinstance(value, str) and value:
            n = int(field[5:])
            names = songs.Names(*(str(form.get(f"{part}_{n}") or "") for part in ("artist", "title", "album")))
            rows[n] = (value, names)
    if not rows:
        upload.cancel(uploads._paths(request), batch)
        return page(request, "_upload.html", step="done", results=[], imported=[], none=True)
    cover_file = form.get("cover")
    cover = _cover(await cover_file.read() if isinstance(cover_file, FormFile) else b"")
    target, name = str(form.get("list") or NEW), str(form.get("new_list") or "")
    note = " ".join(str(form.get("note") or "").split())
    results, key = await run_in_threadpool(_import, request, con, batch, rows, target, name, note, cover)
    request.app.state.worker.trigger("library")  # (the list's playlist)
    lines, imported = [line for line, _ in results], [k for _, k in results if k]
    return page(request, "_upload.html", step="done", results=lines, imported=imported, list_key=key)


def _refused(key: str, e: songs.Refused) -> RedirectResponse:
    return back(f"/lists/{key}", error=str(e))


@router.post("/unreleased/{key}/rename")
def rename_list(request: Request, con: DB, key: str, name: Annotated[str, Form()]) -> RedirectResponse:
    try:
        songs.rename(con, _user(request), key, name)
    except songs.Refused as e:
        return _refused(key, e)
    request.app.state.worker.trigger("library")  # (the playlist takes the name)
    return back(f"/lists/{key}", ok="Renamed.")


@router.post("/unreleased/{key}/delete")
def delete_list(request: Request, con: DB, key: str) -> RedirectResponse:
    try:
        songs.delete(con, _user(request), key)
    except songs.Refused as e:
        return _refused(key, e)
    request.app.state.worker.trigger("library")  # (its playlist goes)
    return back("/library", ok="List deleted; its songs and their files stay in the library.")


@router.post("/unreleased/{key}/remove")
def remove_song(request: Request, con: DB, key: str, song: Annotated[str, Form()]) -> RedirectResponse:
    try:
        songs.remove(con, _user(request), key, song)
    except songs.Refused as e:
        return _refused(key, e)
    request.app.state.worker.trigger("library")
    return back(f"/lists/{key}", ok="Taken out of the list; its file stays in the library.")
