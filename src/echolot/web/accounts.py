"""Accounts page, the welcome screen after the first login: connect Spotify (your own developer app,
then the login: automatic when the redirect reaches Echolot, else the final address pasted back),
SoundCloud (the web token, checked right away) and Soulseek (the account the Sockseek daemon logs in
with; Echolot writes its login file, the daemon restarts with it)."""

import secrets
import shutil
import sqlite3
import time
import urllib.parse
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from echolot import db
from echolot.services import soulseek, soundcloud, spotify
from echolot.settings import options
from echolot.settings.vault import VaultError
from echolot.web.common import DB, back, page

router = APIRouter(include_in_schema=False)
CALLBACK = "/accounts/spotify/callback"
STATE = "spotify_login_state"  # meta: '<state> <unix time> <redirect address>' of the last login
CACHE_SECONDS = 120  # the overview's connection line reuses a status this young
LOW_DISK = 20 * 2**30


def redirect_uri(request: Request) -> str:
    """Where Spotify sends the browser after the login. Spotify accepts https addresses and, over http,
    only the loopback address: reached over https (a reverse proxy whose headers Echolot trusts), the
    login returns to Echolot itself; else to 127.0.0.1, which the browser can't open and the user
    pastes back."""
    if request.url.scheme == "https":
        return f"https://{request.url.netloc}{CALLBACK}"
    return f"http://127.0.0.1:{request.app.state.settings.port}{CALLBACK}"


def status(request: Request, con: sqlite3.Connection, cached: bool = False) -> dict[str, Any]:
    """What is connected, for this page, the Sources page and the overview (asks all three services;
    `cached`: a result of the last CACHE_SECONDS will do)."""
    state = request.app.state
    last = getattr(state, "account_status", None)
    if cached and last and time.monotonic() - last[0] < CACHE_SECONDS:
        return last[1]
    vault = state.vault
    sp: dict[str, Any] = {"app": False, "connected": False, "client_id": ""}
    try:  # a secret stored with another key can't be read: shown as the account's error
        creds = spotify.Credentials.load(con, vault)
        sp.update(app=creds.app, connected=creds.connected, client_id=creds.client_id)
        if creds.connected:
            me = spotify.Spotify(con, vault).me()
            sp.update(name=me.get("display_name") or me.get("id"), error=None)
    except (spotify.SpotifyError, VaultError) as e:
        sp["error"] = str(e)
    sc: dict[str, Any] = {"connected": False}
    try:
        if token := vault.get(con, soundcloud.TOKEN):
            sc.update(connected=True, **soundcloud.me(token))
    except (soundcloud.SoundCloudError, VaultError) as e:
        sc.update(connected=False, error=str(e))
    opts = options.get(con, options.Soulseek)
    slsk: dict[str, Any] = {"user": opts.user, "password": vault.has(con, "soulseek.password")}
    try:
        slsk.update(soulseek.Daemon(opts.url, timeout=5).status(), reachable=True)
    except soulseek.DaemonError as e:
        slsk.update(reachable=False, ready=False, error=str(e))
    result = {"spotify": sp, "soundcloud": sc, "soulseek": slsk}
    state.account_status = (time.monotonic(), result)
    return result


@router.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request, con: DB) -> HTMLResponse:
    return page(request, "accounts.html", nav="accounts", s=status(request, con),
                redirect_uri=redirect_uri(request), automatic=request.url.scheme == "https",
                has_daemon_dir=request.app.state.settings.daemon_dir is not None)  # fmt: skip


@router.get("/accounts/line", response_class=HTMLResponse)
def connection_line(request: Request, con: DB) -> HTMLResponse:
    """The connections and the free disk space on the overview (loaded after the page)."""
    folder = request.app.state.settings.library_dir
    free = shutil.disk_usage(folder).free if folder and folder.is_dir() else None
    return page(request, "_connections.html", s=status(request, con, cached=True), free=free, low_disk=LOW_DISK)


@router.get("/accounts/soulseek", response_class=HTMLResponse)
def soulseek_fragment(request: Request, con: DB) -> HTMLResponse:
    """The Soulseek card alone (htmx polls it while the daemon logs in)."""
    return page(request, "_soulseek.html", s=status(request, con),
                has_daemon_dir=request.app.state.settings.daemon_dir is not None)  # fmt: skip


# ------------------------------------------------------------ Spotify


@router.post("/accounts/spotify/app")
def spotify_app(
    request: Request, con: DB, client_id: Annotated[str, Form()], client_secret: Annotated[str, Form()] = ""
) -> RedirectResponse:
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if len(client_id) != 32:
        return back("/accounts", error="The client ID is the 32 characters under the app's name.")
    vault = request.app.state.vault
    if not client_secret and not vault.get(con, spotify.SECRET):
        return back("/accounts", error="Paste the client secret too (the app's Settings, 'View client secret').")
    with con:
        options.update(con, options.Spotify, client_id=client_id)
        if client_secret:
            vault.set(con, spotify.SECRET, client_secret)
    return back("/accounts", ok="Spotify app saved. Now connect your account.")


@router.post("/accounts/spotify/login")
def spotify_login(request: Request, con: DB) -> Response:
    """Off to Spotify's login; it comes back to the redirect address with a code."""
    sp = options.get(con, options.Spotify)
    if not sp.client_id:
        return back("/accounts", error="Save your Spotify app's client ID and secret first.")
    state, redirect = secrets.token_urlsafe(16), redirect_uri(request)
    with con:
        db.set_meta(con, STATE, f"{state} {int(time.time())} {redirect}")
    url = spotify.authorize_url(sp.client_id, redirect, state)
    return RedirectResponse(url, status_code=303)


def _finish_login(request: Request, con: sqlite3.Connection, query: dict[str, list[str]]) -> RedirectResponse:
    if error := (query.get("error") or [""])[0]:
        return back("/accounts", error=f"Spotify: {error.replace('_', ' ')}.")
    code, state = (query.get("code") or [""])[0], (query.get("state") or [""])[0]
    expected, started, redirect = ([*db.get_meta(con, STATE).split(" "), "", ""])[:3]
    if not code or not state or state != expected or not redirect or time.time() - int(started or 0) > 1800:
        return back("/accounts", error="That is not the address of the last Spotify login (or it is older "
                                       "than 30 min). Click 'Connect Spotify' again.")  # fmt: skip
    try:
        spotify.exchange(con, request.app.state.vault, code, redirect)  # the address the login used
    except spotify.SpotifyError as e:
        return back("/accounts", error=str(e))
    with con:
        db.set_meta(con, STATE, "")
    return back("/accounts", ok="Spotify connected.")


@router.get(CALLBACK)
def spotify_callback(request: Request, con: DB) -> RedirectResponse:
    return _finish_login(request, con, urllib.parse.parse_qs(request.url.query))


@router.post("/accounts/spotify/paste")
def spotify_paste(request: Request, con: DB, url: Annotated[str, Form()]) -> RedirectResponse:
    """The address the browser showed after the login (it could not open 127.0.0.1)."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url.strip()).query)
    if not query.get("code") and not query.get("error"):
        return back("/accounts", error="Paste the whole address from the address bar (it contains ?code=...).")
    return _finish_login(request, con, query)


@router.post("/accounts/spotify/disconnect")
def spotify_disconnect(request: Request, con: DB) -> RedirectResponse:
    with con:
        request.app.state.vault.delete(con, spotify.REFRESH)
    return back("/accounts", ok="Spotify disconnected. Your lists and songs stay.")


# ------------------------------------------------------------ SoundCloud


@router.post("/accounts/soundcloud")
def soundcloud_token(request: Request, con: DB, token: Annotated[str, Form()]) -> RedirectResponse:
    token = token.strip().removeprefix("OAuth ").strip()
    try:
        me = soundcloud.me(token)
    except soundcloud.SoundCloudError as e:
        return back("/accounts", error=str(e))
    with con:
        request.app.state.vault.set(con, soundcloud.TOKEN, token)
        options.update(con, options.SourceOptions, soundcloud_user=me["user"])
    return back("/accounts", ok=f"SoundCloud connected as {me['name']}.")


@router.post("/accounts/soundcloud/disconnect")
def soundcloud_disconnect(request: Request, con: DB) -> RedirectResponse:
    with con:
        request.app.state.vault.delete(con, soundcloud.TOKEN)
    return back("/accounts", ok="SoundCloud disconnected. Your lists and songs stay.")


# ------------------------------------------------------------ Soulseek


@router.post("/accounts/soulseek")
def soulseek_account(
    request: Request, con: DB, user: Annotated[str, Form()], password: Annotated[str, Form()] = ""
) -> RedirectResponse:
    user = user.strip()
    vault = request.app.state.vault
    password = password or vault.get(con, "soulseek.password") or ""
    if not user or not password or any(c in user + password for c in "\n\r"):
        return back("/accounts", error="Soulseek needs a user name and a password.")
    folder = request.app.state.settings.daemon_dir
    if folder is None:
        return back("/accounts", error="No daemon directory configured (ECHOLOT_DAEMON_DIR).")
    with con:
        options.update(con, options.Soulseek, user=user)
        vault.set(con, "soulseek.password", password)
    soulseek.write_conf(folder, user, password)
    return back("/accounts", ok="Soulseek account saved; the daemon logs in with it in a moment.")


@router.post("/accounts/soulseek/url")
def soulseek_url(con: DB, url: Annotated[str, Form()]) -> RedirectResponse:
    try:
        with con:
            options.update(con, options.Soulseek, url=url.strip().rstrip("/"))
    except options.OptionsError as e:
        return back("/accounts", error=str(e))
    return back("/accounts", ok="Daemon address saved.")
