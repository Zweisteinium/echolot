"""Login (session cookie or API token), CSRF and per-route rights.

Navidrome checks the password; Echolot keeps none. Public: PUBLIC, /static, /rest (Subsonic, checked by
web/subsonic), /api/recorder except its export (device tokens, web/recorders) and /metrics if allowed.
A session's unsafe requests need its CSRF token (form field csrf_token or header X-CSRF-Token).

Rights (allowed): admins everything; others the routes in USER (pages scoped to their own songs) and in
PERMITTED with that permission. A route listed nowhere is admin-only, so a new page starts closed.
"""

import hmac
import logging
import sqlite3
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from echolot import db
from echolot.services import navidrome
from echolot.settings import auth, options, sources
from echolot.web.common import DB, page

log = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)
PUBLIC = {"/healthz", "/login", "/favicon.ico"}
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
USER: set[tuple[str, str]] = {  # (method, route path) any logged-in user may use
    ("GET", "/"),
    ("GET", "/stats"),
    ("GET", "/library"),
    ("GET", "/library/graphs"),
    ("GET", "/jobs/now"),
    ("GET", "/account/tokens"),
    ("GET", "/account/recorders"),
    ("GET", "/playlists"),
    ("GET", "/account"),
    ("POST", "/account/tokens"),
    ("POST", "/account/tokens/{token_id}/revoke"),
    ("POST", "/account/sessions/end-others"),
    ("POST", "/logout"),
    ("GET", "/accounts"),
    ("POST", "/accounts/spotify/login"),
    ("GET", "/accounts/spotify/callback"),
    ("POST", "/accounts/spotify/paste"),
    ("POST", "/accounts/spotify/disconnect"),
    ("POST", "/accounts/soundcloud"),
    ("POST", "/accounts/soundcloud/disconnect"),
    ("GET", "/sources"),
    ("GET", "/sources/found/{service}"),
    ("GET", "/sources/other"),
    ("POST", "/sources/follow"),
    ("POST", "/sources/add"),
    ("GET", "/missing"),
    ("GET", "/buy"),
    ("GET", "/discogs"),
    ("GET", "/discogs/release"),
    ("GET", "/discover"),
    ("GET", "/discover/results"),
    ("GET", "/discover/wished"),
    ("POST", "/discover/{sid}/get"),
    ("POST", "/discover/{sid}/forget"),
    ("GET", "/lists/{key:path}"),
    ("GET", "/activity"),
    ("GET", "/accounts/line"),
    ("GET", "/jobs"),
    ("GET", "/changes"),
}
PERMITTED: dict[tuple[str, str], str] = {  # (method, route path) -> permission (auth.PERMISSIONS)
    ("GET", "/review"): "review",
    ("GET", "/review/{event_id}/audio"): "review",
    ("POST", "/review/{event_id}"): "review",
    ("POST", "/review/{event_id}/discard-all"): "review",
    ("POST", "/review/{event_id}/revert"): "review",
    ("GET", "/missing/upload"): "upload",
    ("POST", "/missing/upload/{batch}/file"): "upload",
    ("POST", "/missing/upload/{batch}/import"): "upload",
    ("POST", "/missing/upload/{batch}/cancel"): "upload",
    ("GET", "/unreleased/upload"): "upload",  # unreleased songs and their lists (web/unreleased)
    ("POST", "/unreleased/upload/{batch}/file"): "upload",
    ("POST", "/unreleased/upload/{batch}/{n}/check"): "upload",
    ("POST", "/unreleased/upload/{batch}/import"): "upload",
    ("POST", "/unreleased/{key}/rename"): "upload",
    ("POST", "/unreleased/{key}/delete"): "upload",
    ("POST", "/unreleased/{key}/remove"): "upload",
    ("POST", "/songs/{key:path}/search"): "run",
    ("POST", "/songs/{key:path}/close-remove"): "review",
    ("POST", "/jobs/mine"): "run",
    ("GET", "/recorders/connect"): "upload",  # recording puts files into the library
    ("POST", "/recorders/connect"): "upload",
    ("POST", "/recorders/{recorder_id}/rename"): "upload",
    ("POST", "/songs/{key:path}/spotify-again"): "upload",
    ("POST", "/songs/{key:path}/origin-again"): "upload",
    ("POST", "/recorders/{recorder_id}/disconnect"): "upload",
    ("GET", "/recordings/new"): "upload",
    ("POST", "/recordings"): "upload",
    ("GET", "/recordings/panel"): "upload",
    ("POST", "/recordings/{session_id}/{action}"): "upload",
    ("GET", "/api/recorder/export"): "upload",
}
NO_NAVIDROME = "Echolot does not know Navidrome's address yet: set ECHOLOT_NAVIDROME_URL and restart it."


def _identify(request: Request) -> tuple[auth.User | None, str, str, bool]:
    """(user, via: 'session' | 'token' | '', CSRF token, public path)."""
    con = db.connect(request.app.state.settings.db_path)
    try:
        path = request.url.path
        public = path in PUBLIC or path.startswith(("/static/", "/rest/"))
        public = public or (path.startswith("/api/recorder/") and path != "/api/recorder/export")
        if path == "/metrics":
            public = options.get(con, options.Metrics).public
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            user = auth.token_user(con, header[7:].strip())
            return user, "token" if user else "", "", public
        s = auth.session(con, request.cookies.get(auth.COOKIE, ""))
        if s:
            return s.user, "session", s.csrf, public
        return None, "", "", public
    finally:
        con.close()


async def authenticate(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    user, via, csrf, public = await run_in_threadpool(_identify, request)
    request.state.user, request.state.via, request.state.csrf, request.state.public = user, via, csrf, public
    if user is None and not public:
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse("/login?" + urllib.parse.urlencode({"next": target}), status_code=303)
        return JSONResponse(
            {"detail": "Log in first, or send Authorization: Bearer <API token>."},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)


async def csrf_protect(request: Request) -> None:
    """Changing requests of a browser session must carry the session's CSRF token."""
    if request.method not in UNSAFE or getattr(request.state, "via", "") != "session":
        return
    token = request.headers.get("x-csrf-token")
    if token is None:
        form = await request.form()  # cached for the endpoint
        value = form.get("csrf_token")
        token = value if isinstance(value, str) else None
    if not token or not hmac.compare_digest(token, request.state.csrf):
        raise HTTPException(403, "The page was out of date (CSRF check). Reload it and try again.")


async def allowed(request: Request) -> None:
    """403 unless the user may use this route (see the module docstring)."""
    user: auth.User | None = getattr(request.state, "user", None)
    if user is None or user.admin or getattr(request.state, "public", False):
        return  # no user: authenticate only lets public paths through
    route = request.scope.get("route")
    method = "GET" if request.method == "HEAD" else request.method
    key = (method, getattr(route, "path", request.url.path))
    if key in USER:
        return
    need = PERMITTED.get(key)
    if need and user.can(need):
        return
    raise HTTPException(403, f"That needs the {need} permission." if need else "That is for admins.")


def _safe_next(target: str) -> str:
    return target if target.startswith("/") and not target.startswith("//") else "/"


def set_session_cookie(request: Request, response: Response, con: sqlite3.Connection, user: auth.User) -> None:
    days = options.get(con, options.Auth).session_days
    token = auth.create_session(con, user, days)
    response.set_cookie(auth.COOKIE, token, max_age=days * 86400, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/")  # fmt: skip


@router.get("/login", response_class=HTMLResponse, response_model=None)
def login_page(request: Request, con: DB, next: str = "/") -> Response:
    if getattr(request.state, "user", None):
        return RedirectResponse(_safe_next(next), status_code=303)
    return _login_page(request, con, 200, next=next)


def _login_page(request: Request, con: sqlite3.Connection, status_code: int, **extra: object) -> Response:
    ready = bool(navidrome.address(con))
    extra.setdefault("error", "" if ready else NO_NAVIDROME)
    return page(request, "login.html", status_code, ready=ready, **extra)


@router.post("/login", response_class=HTMLResponse, response_model=None)
def login(
    request: Request,
    con: DB,
    name: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
) -> Response:
    throttle: auth.Throttle = request.app.state.throttle
    client = request.client.host if request.client else "?"
    if wait := throttle.wait(client):
        error = f"Too many failed logins. Try again in {int(wait / 60) + 1} min."
        return _login_page(request, con, 429, next=next, name=name, error=error)
    url = navidrome.address(con)
    if not url:
        return _login_page(request, con, 503, next=next, name=name)
    try:
        found = navidrome.login(url, name.strip(), password) if name.strip() and password else None
    except navidrome.NavidromeError as e:
        log.warning("login of %r: %s", name, e)
        error = "Navidrome is not reachable: try again in a moment."
        return _login_page(request, con, 502, next=next, name=name, error=error)
    if found is None:
        throttle.failed(client)
        log.warning("failed login for %r from %s", name, client)
        return _login_page(request, con, 400, next=next, name=name, error="Wrong user name or password.")
    account, navidrome_admin, navidrome_id = found
    try:
        user = auth.logged_in(con, account, navidrome_id, navidrome_admin)
    except sqlite3.IntegrityError:
        error = f"Another Echolot user is called {account}: ask an admin."
        return _login_page(request, con, 409, next=next, name=name, error=error)
    if user.admin and (adopted := sources.adopt(con)):
        log.info("adopted at %s's login: %s", user.name, adopted)
    throttle.passed(client)
    response = RedirectResponse(_safe_next(next), status_code=303)
    set_session_cookie(request, response, con, user)
    return response


@router.post("/logout")
def logout(request: Request, con: DB) -> RedirectResponse:
    auth.end_session(con, request.cookies.get(auth.COOKIE, ""))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE, path="/")
    return response
