"""Upload dialog: files for missing songs or better copies, checked one by one, imported on confirm
(library/upload). A file that is none of the user's songs is looked for as a release (upload.release):
found, it can be imported as that song into their Echolot · Wished. Upload permission, for the songs the
user sees."""

import contextlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from echolot.library import filing, origin, review, upload
from echolot.services import catalogs
from echolot.settings import options
from echolot.web import stats, wishes
from echolot.web.common import DB, page

router = APIRouter(include_in_schema=False)
WISH = "wish:"  # a file's song in the dialog that is a release on none of the user's lists: wish:<its id>


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


RESULTS = 10  # Deezer results the release check looks through


@dataclass
class Found:
    """The release check of a file (upload.release) for the dialog: what it found (None: nothing), the id its
    release is kept by to be wished (wishes.keep), why it could not look (failed) and the library file with
    the same audio (have)."""

    release: upload.Release | None = None
    sid: str = ""
    failed: str = ""
    have: str = ""
    mine: Any = None  # the user's song the release is (yours), when it is one
    isrc: str | None = None  # the release's, when it is the file's recording


def yours(found: Found, songs: dict[str, Any]) -> Any:
    """The user's song (of `songs`) the release found is: by its ISRC (another release of the recording, other
    names: "Kissing Disease – Rausch" is your "para//e/ – Rausch"), else by its names and length
    (upload.guess); None when it is none of them (or the audio was not compared)."""
    if found.release is None or found.release.verdict != "released":
        return None
    same = [s for s in songs.values() if found.isrc and (s["isrc"] or "").replace("-", "").upper() == found.isrc]
    if same:
        return max(same, key=upload.needs)
    r = found.release.result
    probe = upload.File(0, f"{r.artist} - {r.title}", artist=r.artist, title=r.title, seconds=r.seconds)
    key = upload.guess(probe, list(songs.values()))
    return songs.get(key or "")


def find_release(con: sqlite3.Connection, paths: filing.Paths, f: upload.File, artist: str, title: str) -> Found:
    """Look for the file as a release by these names (one Deezer search, a preview or three)."""
    out = Found(have=upload.have_as(con, paths, f, title))
    try:
        out.release = upload.release(f, artist, title, lambda q: catalogs.deezer(q, RESULTS))
    except catalogs.CatalogError as e:
        out.failed = f"The release check could not ask Deezer ({e}); check again later."
        return out
    if out.release is not None:
        out.isrc = _isrc(out.release) if out.release.verdict == "released" else None
        now = datetime.now().isoformat(timespec="seconds")
        out.sid = wishes.keep(con, out.release.result, now, out.isrc)[0]
        con.commit()
    return out


def _isrc(found: upload.Release) -> str | None:
    """The release's ISRC, from Deezer (its track's page); '' when it has none, None when Deezer did not say."""
    url = next((h.url for h in found.result.sources if h.source == "deezer" and h.url), "")
    track = url.rstrip("/").rsplit("/", 1)[-1]
    if not track.isdigit():
        return None
    try:
        return (catalogs.track(track).get("isrc") or "").upper()
    except catalogs.CatalogError:
        return None


def wish_files(request: Request, con: sqlite3.Connection, wished: dict[int, str]) -> tuple[dict[int, str], list]:
    """The files to import as released songs (n -> the id their release is kept by): each release wished by the
    user (into their Echolot · Wished, not searched: its file is here), then (n -> song key) for import_files;
    a release two files are gets the first one (a line for each other)."""
    user, chosen, lines, seen = request.state.user, {}, [], {}
    for n, sid in sorted(wished.items()):
        if (song := wishes.song(con, sid)) is None:
            raise HTTPException(404, "Echolot does not know that release (any more): check the file again.")
        if sid in seen:
            lines.append((f"{song['artist']} – {song['title']}: not imported twice (file {seen[sid]} is it)", None))
            continue
        seen[sid] = n
        wishes.like(con, user, sid, song, True, "by an upload")
        chosen[n] = wishes.song_key(sid)
    if chosen:
        wishes.store(con, user)
    return chosen, lines


@router.get("/missing/upload", response_class=HTMLResponse)
def upload_form(request: Request, con: DB, song: str = "") -> HTMLResponse:
    """The dialog with a new batch, for one song (`song`) or any of the user's."""
    batch = upload.start(_paths(request))
    return page(request, "_upload.html", step="pick", batch=batch, song=_song(request, con, song))


@router.post("/missing/upload/{batch}/file", response_class=HTMLResponse)
def upload_file(
    request: Request, con: DB, batch: str, file: Annotated[UploadFile, File()], song: Annotated[str, Form()] = ""
) -> HTMLResponse:
    """Save and check one file; its song: by Spotify/ISRC tag, else the one asked for, else guessed from tags
    or name, else the release its names and audio are (one of the user's songs after all, or one to wish).
    Answers its dialog row."""
    keep_hires = options.get(con, options.Files).keep_hires
    try:
        f = upload.add(_paths(request), batch, file.filename or "", file.file, keep_hires)
    except ValueError as e:
        raise HTTPException(404, "That upload is gone (imported, cancelled, or older than a day).") from e
    songs = _songs(request, con)
    key = upload.tagged(con, f, songs) or (song if song in songs else upload.guess(f, list(songs.values())))
    match = songs.get(key or "") if not f.error else None
    paths = _paths(request)
    found = find_release(con, paths, f, *upload.names(f)) if match is None and not f.error else None
    if found and (mine := yours(found, songs)) is not None:  # one of your songs, named otherwise
        match, found = mine, None
    copy = upload.compare(paths, f, match) if match else None  # better than the library's copy?
    fit = upload.fit(con, f, match, paths.tracks)
    go = upload.importable(f, match, copy, fit)
    different = upload.sounds_different(fit)
    labels = upload.found_labels(f, found.release, found.have, found.failed) if found else []
    likely, froms = origin.choices(con, match["key"] if match else "") if match or found else ("", [])
    return page(
        request,
        "_upload_row.html",
        f=f,
        song=match,
        labels=labels or upload.labels(f, match, fit, copy),
        checks=upload.release_checks(found.release) if found and found.release else upload.checks(fit, match),
        go=go,
        different=different,
        found=found,
        likely=likely,
        froms=froms,
    )


@router.post("/missing/upload/{batch}/import", response_class=HTMLResponse)
async def upload_import(request: Request, con: DB, batch: str) -> HTMLResponse:
    """File every file with a song (song_<n>); remove_<n>: drop its close match; from_<n>: its origin."""
    form = await request.form()
    songs = _songs(request, con)
    _batch(request, batch)
    chosen, remove = {}, {int(n[7:]) for n in form if n.startswith("remove_") and n[7:].isdigit()}
    froms = {
        int(n[5:]): str(v) for n, v in form.items() if n.startswith("from_") and n[5:].isdigit() and v in origin.KINDS
    }
    wished: dict[int, str] = {}  # released songs on none of the user's lists (n -> the release's id)
    for name, value in form.multi_items():
        if name.startswith("song_") and name[5:].isdigit() and isinstance(value, str) and value:
            if value.startswith(WISH):
                wished[int(name[5:])] = value.removeprefix(WISH)
            elif value not in songs:
                raise HTTPException(403, "That song is not one of your missing songs.")
            else:
                chosen[int(name[5:])] = value
    if not chosen and not wished:
        upload.cancel(_paths(request), batch)
        return page(request, "_upload.html", step="done", results=[], imported=[], none=True)
    as_wished, results = await run_in_threadpool(wish_files, request, con, wished)
    chosen |= as_wished
    vault = request.app.state.vault  # filing asks Spotify for the cover
    results += await run_in_threadpool(
        upload.import_files, con, _paths(request), vault, batch, chosen, remove, origins=froms
    )
    imported = [key for _, key in results if key]  # the page takes these off Missing
    if as_wished:
        request.app.state.worker.trigger("library")  # Echolot · Wished's playlist
    return page(request, "_upload.html", step="done", results=[line for line, _ in results], imported=imported)


@router.post("/songs/{key:path}/origin-again", response_class=HTMLResponse)
def song_origin_again(request: Request, con: DB, key: str) -> HTMLResponse:
    """Drop the "artist's own release" mark, so a better copy is searched again."""
    if not request.state.user.admin and key not in review.songs_of(con, request.state.user.id):
        raise HTTPException(403, "That song is on another user's lists.")
    origin.forget(con, key)
    return HTMLResponse("")


@router.post("/missing/upload/{batch}/cancel")
def upload_cancel(request: Request, batch: str) -> Response:
    with contextlib.suppress(ValueError):  # no such batch
        upload.cancel(_paths(request), batch)
    return HTMLResponse("")
