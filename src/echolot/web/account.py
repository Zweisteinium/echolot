"""Account page: who you are here (an admin, or your permissions), which pages an admin sees (theirs or
everyone's), your recorder devices (web/recorders), your API tokens and your logins. The password is
Navidrome's: it is changed there."""

import sqlite3
import urllib.parse
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from echolot.library import recording
from echolot.settings import auth
from echolot.web.common import DB, back, page

router = APIRouter(include_in_schema=False)


def _account_page(request: Request, con: sqlite3.Connection, **extra: Any) -> HTMLResponse:
    user = request.state.user
    tokens = auth.tokens(con, user)
    devices = recording.devices(con, user.id) if user.can("upload") else []
    base = str(request.base_url).rstrip("/")
    perms = auth.PERMISSIONS
    return page(
        request, "account.html", nav="account", tokens=tokens, permissions=perms, devices=devices, base=base, **extra
    )


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, con: DB) -> HTMLResponse:
    return _account_page(request, con)


@router.get("/account/recorders")
def old_recorders_page() -> RedirectResponse:
    """The account's parts were pages of their own (0.11.1)."""
    return RedirectResponse("/account#recorders", status_code=301)


@router.get("/account/tokens")
def old_tokens_page() -> RedirectResponse:
    return RedirectResponse("/account#tokens", status_code=301)


@router.post("/account/view")
def account_view(request: Request, con: DB, view: Annotated[str, Form()]) -> RedirectResponse:
    """An admin's pages show their own lists and songs, or everyone's."""
    try:
        auth.set_view(con, request.state.user, view)
    except auth.AuthError as err:
        return back(_same_page(request), error=str(err))
    return back(_same_page(request))


def _same_page(request: Request) -> str:
    """Back to the page the switch was on: the path of the referring page, never another site."""
    path = urllib.parse.urlparse(request.headers.get("referer") or "/").path or "/"
    return path if path.startswith("/") and not path.startswith("//") else "/"


@router.post("/account/sessions/end-others")
def end_other_sessions(request: Request, con: DB) -> RedirectResponse:
    n = auth.end_other_sessions(con, request.state.user, request.cookies.get(auth.COOKIE, ""))
    return back("/account", ok=f"{n} other login(s) ended.")


@router.post("/account/tokens", response_class=HTMLResponse)
def token_create(request: Request, con: DB, name: Annotated[str, Form()]) -> Response:
    try:
        token = auth.create_token(con, request.state.user, name)
    except auth.AuthError as err:
        return back("/account#tokens", error=str(err))
    # shown once, in this response only (a redirect would put it into the URL)
    return _account_page(request, con, new_token=token, new_token_name=name.strip())


@router.post("/account/tokens/{token_id}/revoke")
def token_revoke(request: Request, con: DB, token_id: int) -> RedirectResponse:
    if not auth.revoke_token(con, request.state.user, token_id):
        return back("/account#tokens", error="No such token.")
    return back("/account#tokens", ok="Token revoked.")
