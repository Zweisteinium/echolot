"""Who may use Echolot: login with a session cookie (browsers) or an API token (scripts).

Every request needs one of them, except /healthz, the login page, the static files and, when the
metrics setting allows it, /metrics. A session's changing requests (POST, PUT, ...) also need its
CSRF token: the csrf_token form field or the X-CSRF-Token header (htmx sends it for every request).
With no user yet, /setup sets the password of the admin account; its link, with a one-time token,
is in the log (or ECHOLOT_ADMIN_PASSWORD sets it at the start).
"""

import hmac
import logging
import os
import secrets
import sqlite3
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from echolot import auth, db, options
from echolot.web.common import DB, page

log = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)
PUBLIC = {"/healthz", "/login", "/setup"}
ADMIN = "admin"  # the account the setup creates
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def first_user(app: FastAPI, con: sqlite3.Connection) -> None:
    """With no user yet: create the admin account with ECHOLOT_ADMIN_PASSWORD, else log a /setup link
    where its password is set."""
    app.state.setup_token = None
    if auth.has_users(con):
        return
    if password := os.environ.get("ECHOLOT_ADMIN_PASSWORD"):
        auth.add_user(con, ADMIN, password)
        log.info("account %s created with ECHOLOT_ADMIN_PASSWORD", ADMIN)
        return
    app.state.setup_token = secrets.token_urlsafe(18)
    log.warning(
        "No account yet. Set the admin password at /setup?token=%s (this link works until then)",
        app.state.setup_token,
    )


def _identify(request: Request) -> tuple[auth.User | None, str, str, bool]:
    """(user, how: 'session' | 'token' | '', CSRF token, whether the path is public)."""
    con = db.connect(request.app.state.settings.db_path)
    try:
        path = request.url.path
        public = path in PUBLIC or path.startswith("/static/")
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


async def authenticate(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    user, via, csrf, public = await run_in_threadpool(_identify, request)
    request.state.user, request.state.via, request.state.csrf = user, via, csrf
    if user is None and not public:
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(
                "/login?" + urllib.parse.urlencode({"next": target}), status_code=303
            )
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
        form = await request.form()  # cached: the endpoint reads the same form
        value = form.get("csrf_token")
        token = value if isinstance(value, str) else None
    if not token or not hmac.compare_digest(token, request.state.csrf):
        raise HTTPException(403, "The page was out of date (CSRF check). Reload it and try again.")


def _safe_next(target: str) -> str:
    return target if target.startswith("/") and not target.startswith("//") else "/"


def set_session_cookie(
    request: Request, response: Response, con: sqlite3.Connection, user: auth.User
) -> None:
    """Log `user` in on this browser (a new session)."""
    days = options.get(con, options.Auth).session_days
    token = auth.create_session(con, user, days)
    response.set_cookie(auth.COOKIE, token, max_age=days * 86400, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/")  # fmt: skip


@router.get("/login", response_class=HTMLResponse, response_model=None)
def login_page(request: Request, con: DB, next: str = "/") -> Response:
    if getattr(request.state, "user", None):
        return RedirectResponse(_safe_next(next), status_code=303)
    if not auth.has_users(con):
        return page(request, "login.html", first_run=True, next=next)
    return page(request, "login.html", next=next)


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
    wait = throttle.wait(client)
    if wait:
        return page(request, "login.html", 429, next=next, name=name,
                    error=f"Too many failed logins. Try again in {int(wait / 60) + 1} min.")  # fmt: skip
    user = auth.verify(con, name, password)
    if user is None:
        throttle.failed(client)
        log.warning("failed login for %r from %s", name, client)
        return page(request, "login.html", 400, next=next, name=name,
                    error="Wrong user name or password.")  # fmt: skip
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


def _setup_allowed(request: Request, con: sqlite3.Connection, token: str) -> None:
    expected = request.app.state.setup_token
    if auth.has_users(con) or not expected:
        raise HTTPException(404, "The admin password is set already: log in.")
    if not hmac.compare_digest(token, expected):
        raise HTTPException(403, "Open the /setup link from Echolot's log.")


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, con: DB, token: str = "") -> HTMLResponse:
    _setup_allowed(request, con, token)
    return page(request, "setup.html", token=token, admin=ADMIN)


@router.post("/setup", response_class=HTMLResponse, response_model=None)
def setup(
    request: Request,
    con: DB,
    token: Annotated[str, Form()],
    password: Annotated[str, Form()],
    repeat: Annotated[str, Form()],
) -> Response:
    _setup_allowed(request, con, token)
    try:
        auth.check_new_password(password, repeat)
        user = auth.add_user(con, ADMIN, password)
    except auth.AuthError as err:
        return page(request, "setup.html", 400, token=token, admin=ADMIN, error=str(err))
    request.app.state.setup_token = None
    log.info("account %s created", user.name)
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(request, response, con, user)
    return response
