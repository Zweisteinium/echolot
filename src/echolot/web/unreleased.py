"""Unreleased songs (library/unreleased): the upload dialog for them (the upload dialog's script, its own rows:
each file named by hand, checked for a release first) and the Unreleased lists they go into (made in the
dialog; renamed, deleted and songs taken out on their list page). A file that is a release after all can be
imported as the released song instead (into Echolot · Wished, as web/upload does it). For admins and users with
the upload permission."""

from typing import Annotated, Any

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
    context = {"lists": mine, "chosen": chosen, "new": NEW, "max_lists": songs.MAX_LISTS}
    return page(request, "_unreleased.html", step="pick", batch=batch, **context)


REPLACE = "replace:"  # a file's choice: in place of this unreleased song's file (replace:<its key>)
KEEP = "local"  # a file's choice: a new unreleased song


def _twin_label(t: songs.Twin, owner: Any) -> upload.Label:
    """What a library file the upload sounds like says, for its row."""
    what = (
        f"your unreleased {t.path.rpartition('/')[2]}"
        if t.local
        else (f"your {owner['artist']} – {owner['title']}" if owner else t.path)
    )
    if t.verdict == "same":
        return ("You have it", "warn", f"The same audio as {what}")
    if t.verdict == "version":
        return ("In your library", "warn", f"The same recording as {what}, in another master, mix or edit")
    return ("Resembles yours", "warn", f"Like {what} (unclear: another edit, or a song that shares parts)")


def _choices(
    found: uploads.Found | None, twins: list[songs.Twin], owners: dict[str, Any], mine: set[str]
) -> tuple[list[tuple[str, str]], str]:
    """What a file can be imported as (value, text) and the likeliest: a new unreleased song (KEEP), in place of
    an unreleased song's file it is (REPLACE), as your song or the release it is (wish:<id>), or not at all
    (''). A file whose audio the library has is left out, unless it is the better copy; a release is imported
    as the release."""
    out = [(KEEP, "Keep as a new unreleased song")]
    release, as_release = found.release if found else None, None
    if found and release is not None:
        if found.mine:
            as_release = found.mine["key"]
            text = f"Import as your {found.mine['artist']} – {found.mine['title']} (released)"
        else:
            as_release = f"{uploads.WISH}{found.sid}"
            text = f"Import as {release.result.artist} – {release.result.title} (released, into Wished)"
        out.append((as_release, text))
    as_same = None  # what a file the library has (the same audio) is imported as: '' not at all
    for t in twins:
        better = "better" if t.better else "not better"
        value = ""
        if t.local and t.local in mine:
            value = f"{REPLACE}{t.local}"
            out.append((value, f"Replace your copy ({better})"))
        elif (owner := owners.get(t.path)) is not None:
            value = owner["key"]
            if value not in dict(out):
                out.append((value, f"Import as your {owner['artist']} – {owner['title']} ({better})"))
        if t.verdict == "same" and as_same is None:
            as_same = value if t.better else ""
    out.append(("", "Leave it out"))
    if as_same is not None:
        return out, as_same
    return out, as_release if release is not None and release.verdict == "released" and as_release else KEEP


def _row(request: Request, con: DB, batch: str, f: upload.File, names: songs.Names) -> HTMLResponse:
    """A file's row: the names it is to have; what it may be already (a release: upload.release, by its names,
    its audio against the previews; a library file: unreleased.twins, named alike, by its audio), shown only
    when the audio says it may be; what it can be imported as; whether an unreleased song has its names."""
    paths = uploads._paths(request)
    n = names.clean()
    named = not f.error and bool(n.artist and n.title)
    found = uploads.find_release(con, paths, f, n.artist, n.title, loose=True) if named else None
    if found and found.release and not found.release.verdict:  # (named so, no preview: nothing the audio says)
        found.release = None
    mysongs = uploads._songs(request, con)
    if found and found.release:
        found.mine = uploads.yours(found, mysongs)
    twins = songs.twins(con, paths, f, n)
    owners = {s["file"]: s for s in mysongs.values() if s["file"] and not s["close_match"]}
    choices, likely = _choices(found, twins, owners, set(songs.user_songs(con, _user(request))))
    labels: list[upload.Label] = []
    if f.error:
        labels.append(("Not audio", "bad", f.error))
    elif not named:
        labels.append(("Needs names", "warn", "Give it an artist and a title, then check again"))
    if found and found.failed:
        labels.append(("Not checked", "warn", found.failed))
    if found and found.release:
        r = found.release
        who = f"{r.result.artist} – {r.result.title}"
        if r.verdict == "released":
            labels.append(("Released", "warn", f"The recording of {who} at its length: better imported as that song"))
        else:
            labels.append(("Resembles a release", "warn", f"Like {who}, in another edit or length (or unclear)"))
    labels += [_twin_label(t, owners.get(t.path)) for t in twins]
    if named and not (found and (found.release or found.failed)) and not twins:
        labels.append(("Not released", "ok", "No release with its audio (Deezer), nothing like it in your library"))
    if named and likely == KEEP and songs.taken(paths, n):
        detail = "An unreleased song has these names already: put what differs into the title, e.g. (Custom Edit)"
        labels.append(("Name taken", "bad", detail))
    labels += upload.file_notes(f) if not f.error else []
    checks = upload.release_checks(found.release) if found and found.release else []
    for t in twins:
        what = f"{t.path}: three 30 s pieces of the shorter file held against the longer one"
        checks.append(("Yours", what, upload.graded(t.fingerprint, t.waveform, t.path.rpartition("/")[2]), ""))
    context = {"f": f, "names": n, "labels": labels, "checks": checks, "choices": choices, "likely": likely}
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
    """File the batch's files: those kept unreleased into the list `target` (NEW: a new one named `name`), those
    replacing an unreleased song's file in its place, the releases into Echolot · Wished (web/upload.wish_files,
    upload.import_files) and your songs as theirs; the rest of the batch is deleted. Returns a line per file and
    the list's key ('' when none was filed into one)."""
    paths, uid = uploads._paths(request), _user(request)
    by_n = {f.n: f for f in uploads._batch(request, batch)}
    local = {n: names for n, (value, names) in rows.items() if value == KEEP and n in by_n}
    wished = {n: value.removeprefix(uploads.WISH) for n, (value, _) in rows.items() if value.startswith(uploads.WISH)}
    replaced = {n: value.removeprefix(REPLACE) for n, (value, _) in rows.items() if value.startswith(REPLACE)}
    yours = {n: v for n, (v, _) in rows.items() if v != KEEP and not v.startswith((uploads.WISH, REPLACE))}
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
    filed = any(k for _, k in out)
    out += [
        songs.replace(con, paths, by_n[n], uid, song, note, cover) for n, song in sorted(replaced.items()) if n in by_n
    ]
    if local or replaced:
        catalog.match_songs(con)
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
    chosen list, 'replace:<key>' in place of that unreleased song's file, 'wish:<id>' as that release, a song key
    as that song of yours, '' not at all; the note and the cover for the unreleased ones."""
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
def delete_list(request: Request, con: DB, key: str, songs_to: Annotated[str, Form()] = "") -> RedirectResponse:
    """The list goes; its songs on no other list into the list `songs_to`, or (DROP) deleted with it."""
    try:
        n = songs.delete(con, uploads._paths(request), _user(request), key, songs_to)
    except songs.Refused as e:
        return _refused(key, e)
    request.app.state.worker.trigger("library")  # (its playlist goes)
    if not n:
        return back("/library", ok="List deleted.")
    what = f"{n} song{'s' if n != 1 else ''}"
    if songs_to == songs.DROP:
        return back("/library", ok=f"List deleted with {what}; their files are kept 30 days in inbox/replaced.")
    return back(f"/lists/{songs_to}", ok=f"List deleted; its {what} moved here.")


@router.post("/unreleased/{key}/remove")
def remove_song(request: Request, con: DB, key: str, song: Annotated[str, Form()]) -> RedirectResponse:
    try:
        songs.remove(con, _user(request), key, song)
    except songs.Refused as e:
        return _refused(key, e)
    request.app.state.worker.trigger("library")
    return back(f"/lists/{key}", ok="Taken out of this list; it stays on your other ones.")


@router.post("/unreleased/{key}/song")
def song_action(
    request: Request, con: DB, key: str, song: Annotated[str, Form()], to: Annotated[str, Form()]
) -> RedirectResponse:
    """A song of the list: move:<list> (into that list), add:<list> (into that one too), remove (out of this
    one), delete (the song)."""
    uid = _user(request)
    if key not in songs.in_lists(con, uid, song):
        return _refused(key, songs.Refused("That song is not on this list (any more)."))
    try:
        if to.startswith("move:"):
            songs.move(con, uid, key, song, to[5:])
            done = "Moved."
        elif to.startswith("add:"):
            songs.add_to(con, uid, to[4:], song)
            done = "Added to that list too."
        elif to == "remove":
            songs.remove(con, uid, key, song)
            done = "Taken out of this list; it stays on your other ones."
        elif to == songs.DROP:
            name = songs.delete_song(con, uploads._paths(request), uid, song)
            done = f"{name} deleted; its file is kept 30 days in inbox/replaced."
        else:
            raise songs.Refused("Choose what to do with it.")
    except songs.Refused as e:
        return _refused(key, e)
    request.app.state.worker.trigger("library")  # (the playlists)
    return back(f"/lists/{key}", ok=done)
