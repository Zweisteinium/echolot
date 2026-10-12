"""Recording with the users' recorder apps (library/recording): connecting a device (the approval page and
the code it trades for its token), the Record dialog and a session's progress on the pages, and the API the
device works through (/api/recorder/..., with its own token; /api/recorder/export with the user's login).

A device asks for work (poll, a long poll), records each song it is handed and uploads it into exactly that
song (file: the upload dialog's checks, then filed if it is better), or says why it could not (result).
"""

import asyncio
import sqlite3
import urllib.parse
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from echolot import __version__, db
from echolot.library import filing, identity, recording, upload
from echolot.settings import auth, options
from echolot.web import stats
from echolot.web.common import DB, back, page

router = APIRouter(include_in_schema=False)
api = APIRouter(prefix="/api/recorder", tags=["recorder"])
REDIRECT = "mitschnitt://"  # where the approval page sends the code: the app's own link scheme
POLL_STEP = 1.0  # seconds between looks for work during a long poll
MAX_WAIT = 30


# ---------------------------------------------------------------- connecting a device (browser)


@router.get("/recorders/connect", response_class=HTMLResponse)
def connect_page(
    request: Request, name: str = "", app: str = "", challenge: str = "", state: str = "", redirect: str = ""
) -> HTMLResponse:
    """The device asks to be connected to the logged-in user: they approve or cancel."""
    if not redirect.startswith(REDIRECT) or not challenge:
        raise HTTPException(400, "This link does not come from Mitschnitt: start connecting in the app.")
    ask = {"name": recording._name(name), "app": app[:80], "challenge": challenge, "state": state, "redirect": redirect}
    return page(request, "recorders_connect.html", nav="account", ask=ask, step="ask")


@router.post("/recorders/connect", response_class=HTMLResponse)
def connect_answer(
    request: Request,
    con: DB,
    name: Annotated[str, Form()],
    challenge: Annotated[str, Form()],
    redirect: Annotated[str, Form()],
    state: Annotated[str, Form()] = "",
    app: Annotated[str, Form()] = "",
    decision: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Approved: a code for the app, handed over by its link (and shown, for when the link does not open
    it); cancelled: the link says so."""
    if not redirect.startswith(REDIRECT):
        raise HTTPException(400, "This link does not come from Mitschnitt.")
    if decision != "connect":
        target = _with(redirect, error="cancelled", state=state)
        return page(request, "recorders_connect.html", nav="account", step="cancelled", target=target)
    try:
        code = recording.approve(con, request.state.user.id, name, app, challenge)
    except recording.RecordingError as e:
        raise HTTPException(400, str(e)) from e
    target = _with(redirect, code=code, state=state)
    return page(request, "recorders_connect.html", nav="account", step="done", target=target, code=code, name=name)


def _with(url: str, **query: str) -> str:
    return url + ("&" if "?" in url else "?") + urllib.parse.urlencode(query)


@router.post("/recorders/{recorder_id}/rename")
def device_rename(request: Request, con: DB, recorder_id: int, name: Annotated[str, Form()]) -> RedirectResponse:
    try:
        recording.rename(con, request.state.user.id, recorder_id, name)
    except recording.RecordingError as e:
        return back("/account#recorders", error=str(e))
    return back("/account#recorders", ok="Device renamed.")


@router.post("/recorders/{recorder_id}/disconnect")
def device_disconnect(request: Request, con: DB, recorder_id: int) -> RedirectResponse:
    try:
        recording.device(con, request.state.user.id, recorder_id)
    except recording.RecordingError as e:
        return back("/account#recorders", error=str(e))
    recording.disconnect(con, recorder_id)
    return back("/account#recorders", ok="Device disconnected: it can no longer record for you.")


# ---------------------------------------------------------------- recording (browser)


@router.get("/recordings/new", response_class=HTMLResponse)
def record_dialog(request: Request, con: DB) -> HTMLResponse:
    """The Record dialog: what (each choice with its songs and time), on which of the user's devices, how long
    at most. For one Spotify list, or all of the user's (no list)."""
    user = request.state.user
    key = request.query_params.get("list") or None
    lst = stats.get_list(con, key, user.id) if key else None
    if key and (lst is None or lst["service"] != "spotify"):
        raise HTTPException(404, "Not one of your Spotify lists.")
    tracks = recording.candidates(con, user.id, key)
    devices = recording.devices(con, user.id)
    return page(
        request,
        "_record_dialog.html",
        lst=lst,
        choices=recording.choices(tracks),
        devices=devices,
        hours=recording.HOURS,
        default_hours=recording.DEFAULT_HOURS,
        base=_base(request),
    )


@router.post("/recordings")
def record_start(
    request: Request,
    con: DB,
    recorder: Annotated[int, Form()],
    below: Annotated[str, Form()],
    hours: Annotated[float, Form()] = recording.DEFAULT_HOURS,
    list_key: Annotated[str, Form(alias="list")] = "",
) -> Response:
    user = request.state.user
    where = f"/lists/{list_key}" if list_key else "/stats"
    try:
        recording.start(con, user.id, recorder, list_key or None, below, hours)
    except recording.RecordingError as e:
        return back(where, error=str(e))
    return back(where, ok="Recording: your device starts as soon as Spotify is free.")


@router.get("/recordings/panel", response_class=HTMLResponse)
def record_panel(request: Request, con: DB) -> HTMLResponse:
    """A session's progress (one that runs or is paused, or ended lately), for a list page (?list=) or Stats."""
    return _panel(request, con, request.query_params.get("list"))


@router.post("/recordings/{session_id}/{action}", response_class=HTMLResponse)
def record_control(request: Request, con: DB, session_id: int, action: str) -> HTMLResponse:
    if action not in ("pause", "resume", "stop", "dismiss"):
        raise HTTPException(404, "No such action.")
    try:
        s = recording.session_of(con, request.state.user.id, session_id)
        recording.control(con, session_id, action)
    except recording.RecordingError as e:
        raise HTTPException(404, str(e)) from e
    return _panel(request, con, request.query_params.get("list", s["list_key"] or ""))


def _panel(request: Request, con: sqlite3.Connection, list_key: str | None) -> HTMLResponse:
    """`list_key`: a list page's ('' on Stats: any of the user's sessions)."""
    s = recording.latest(con, request.state.user.id, list_key or None)
    session = recording.summary(con, s["id"]) if s else None
    device = None
    if s:
        device = next((d for d in recording.devices(con, request.state.user.id) if d["id"] == s["recorder_id"]), None)
    return page(request, "_recording.html", session=session, device=device, list_key=list_key or "")


def panel_context(con: sqlite3.Connection, user: auth.User, list_key: str | None) -> dict[str, Any]:
    """What a page needs to show the panel and the Record button: whether the user may record, has devices."""
    if not user.can("upload"):
        return {"can_record": False}
    s = recording.latest(con, user.id, list_key)
    return {"can_record": True, "recording_active": bool(s), "recorders": bool(recording.devices(con, user.id))}


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


# ---------------------------------------------------------------- the device's API


def _token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def device_of(request: Request, con: DB) -> sqlite3.Row:
    """The device of the request's token (401 without one; 403 when its user may no longer upload)."""
    row = recording.by_token(con, _token(request))
    if row is None:
        raise HTTPException(401, "Unknown or revoked recorder token: connect the device again.")
    user = auth.get_user(con, row["user_name"])
    if user is None or not user.can("upload"):
        raise HTTPException(403, "This device's user may no longer put files into the library (upload permission).")
    return row


Device = Annotated[sqlite3.Row, Depends(device_of)]


class Connect(BaseModel):
    code: str
    verifier: str
    name: str = ""
    app: str = ""
    platform: str = ""


@api.post("/connect")
def api_connect(con: DB, body: Connect) -> dict[str, Any]:
    """Trade an approved connection's code (and the verifier its challenge was made of) for the device's token."""
    try:
        token, row = recording.exchange(con, body.code, body.verifier, body.name, body.app)
    except recording.RecordingError as e:
        raise HTTPException(400, str(e)) from e
    user = con.execute("SELECT name FROM users WHERE id = ?", (row["user_id"],)).fetchone()
    return {
        "token": token,
        "recorder": {"id": row["id"], "name": row["name"]},
        "user": user["name"],
        "echolot": __version__,
    }


class Poll(BaseModel):
    state: str = "idle"
    detail: str = ""  # recording: where it is ("Feel It 1:12 / 3:45")
    spotify: str = ""
    app: str = ""
    item: int | None = None  # recording: the item (the session knows it)


@api.post("/poll")
async def api_poll(request: Request, device: Device, body: Poll, wait: float = 25) -> dict[str, Any]:
    """The device's heartbeat and its request for work: a song when it is idle and its session has one (at
    once, or within `wait` seconds), else none."""
    path, rid = request.app.state.settings.db_path, device["id"]
    wait = min(max(wait, 0), MAX_WAIT)

    def look() -> tuple[dict | None, dict | None]:
        con = db.connect(path)
        try:
            recording.seen(con, rid, body.state, body.detail, body.spotify, body.app)
            item = recording.hand_out(con, rid) if body.state == "idle" else None
            s = recording.active(con, rid) or recording.latest(con, device["user_id"])
            return item, (recording.summary(con, s["id"]) if s and s["recorder_id"] == rid else None)
        finally:
            con.close()

    loop = asyncio.get_running_loop()
    end = loop.time() + wait
    while True:
        item, session = await run_in_threadpool(look)
        if item is not None or loop.time() >= end or await request.is_disconnected():
            return {"item": item, "session": session}
        await asyncio.sleep(POLL_STEP)


@api.post("/items/{item_id}/file")
def api_file(
    request: Request,
    con: DB,
    device: Device,
    item_id: int,
    file: Annotated[UploadFile, File()],
    note: Annotated[str, Form()] = "",
) -> dict[str, Any]:
    """A recording of a song handed out: checked as the upload dialog checks a file, filed into exactly that
    song when it is the song and better than the library's copy. A song answered for already answers the same;
    one whose lease ran out still comes in unless it was handed out again (recording.open_for). `note`: the
    device's word on the recording (kept with the result)."""
    try:
        item = recording.item_of(con, device["id"], item_id)
    except recording.RecordingError as e:
        raise HTTPException(404, str(e)) from e
    if item["state"] == "done":
        if item["result"] in recording.UPLOADED:
            return _answer(con, item, item["result"], item["detail"] or "")
        raise HTTPException(409, f"This song was answered for already ({item['result']}).")
    if not recording.open_for(item):
        raise HTTPException(409, "This song is not the device's any more (handed out again, or the session ended).")
    library = request.app.state.settings.library_dir
    if library is None:
        raise HTTPException(409, "No library configured.")
    result, detail = _check_and_file(request, con, item, file, filing.Paths(library.parent))
    detail = f"{detail} · {note.strip()}" if note.strip() else detail
    recording.answer(con, item, result, detail)
    return _answer(con, item, result, detail)


def _check_and_file(
    request: Request, con: sqlite3.Connection, item: sqlite3.Row, file: UploadFile, paths: filing.Paths
) -> tuple[str, str]:
    """(result, detail) of a recording: rejected (not audio, another recording than the release's), not_better
    (the library's copy is as good), or filed."""
    song = next(iter(stats.upload_songs(con, item["user_id"], item["song_key"])), None)
    if song is None:
        return "rejected", "the song is no longer in your lists"
    batch = upload.start(paths)
    try:
        keep_hires = options.get(con, options.Files).keep_hires
        f = upload.add(paths, batch, file.filename or "recording.flac", file.file, keep_hires)
        if f.error:
            return "rejected", f"not audio: {f.error}"
        fit = upload.fit(con, f, song)
        if fit and fit.fingerprint is not None and fit.fingerprint <= identity.OTHER:
            return "rejected", "Sounds different: another recording than the release's"
        copy = upload.compare(paths, f, song)
        if copy is not None and not copy.better:
            return "not_better", copy.why
        lines = upload.import_files(con, paths, request.app.state.vault, batch, {f.n: song["key"]})
        line, key = lines[0] if lines else ("not imported", None)
        if key is None:
            return "rejected", line
        # (filing found a better copy after all: "the library has a better copy", import_files' line)
        return ("not_better", line) if "has a better copy" in line else ("filed", line)
    finally:
        upload.cancel(paths, batch)


def _answer(con: sqlite3.Connection, item: sqlite3.Row, result: str, detail: str) -> dict[str, Any]:
    quality = con.execute(
        "SELECT f.quality FROM songs s LEFT JOIN files f ON f.path = s.file WHERE s.key = ?", (item["song_key"],)
    ).fetchone()
    session = recording.summary(con, item["recording_id"])
    return {"result": result, "detail": detail, "quality": quality[0] if quality else None, "session": session}


class Result(BaseModel):
    result: str = Field(pattern="^(lossy_only|wrong_song|took_over|stalled|failed)$")
    detail: str = ""


@api.post("/items/{item_id}/result")
def api_result(con: DB, device: Device, item_id: int, body: Result) -> dict[str, Any]:
    """A song the device could not record, and why (see library/recording.answer)."""
    try:
        item = recording.item_of(con, device["id"], item_id)
    except recording.RecordingError as e:
        raise HTTPException(404, str(e)) from e
    if not recording.open_for(item):
        raise HTTPException(409, "This song is not the device's any more (handed out again, or answered for).")
    recording.answer(con, item, body.result, body.detail)
    return {"session": recording.summary(con, item["recording_id"])}


class Control(BaseModel):
    action: str = Field(pattern="^(pause|resume|stop)$")


@api.post("/session")
def api_session(con: DB, device: Device, body: Control) -> dict[str, Any]:
    """Pause, resume or stop the device's session (its card's buttons)."""
    s = recording.active(con, device["id"])
    if s is None:
        raise HTTPException(404, "No session on this device.")
    recording.control(con, s["id"], body.action)
    return {"session": recording.summary(con, s["id"])}


@api.get("/me")
def api_me(con: DB, device: Device) -> dict[str, Any]:
    s = recording.active(con, device["id"])
    session = recording.summary(con, s["id"]) if s else None
    out = {"recorder": {"id": device["id"], "name": device["name"]}, "user": device["user_name"]}
    return out | {"echolot": __version__, "session": session}


@api.delete("/me")
def api_disconnect(con: DB, device: Device) -> Response:
    recording.disconnect(con, device["id"])
    return Response(status_code=204)


@api.get("/export")
def api_export(request: Request, con: DB, below: str = "lossless") -> Response:
    """The songs of a choice as a file, for a recorder without a connection (the user's login or API token)."""
    user = request.state.user
    key = request.query_params.get("list") or None
    try:
        data = recording.export(con, user.id, user.name, _base(request), key, below)
    except recording.RecordingError as e:
        raise HTTPException(400, str(e)) from e
    name = "echolot-" + "".join(c if c.isalnum() else "-" for c in data["title"].lower()).strip("-")[:60] + ".json"
    headers = {"Content-Disposition": f'attachment; filename="{name}"'}
    return JSONResponse(data, headers=headers)
