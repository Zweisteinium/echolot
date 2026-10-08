"""Accounts page: each user connects their own Spotify (the login through the Spotify app an admin set
up: automatic when the redirect reaches Echolot, else the final address pasted back) and SoundCloud (the
web token, checked right away). Admins also set up the Spotify app and Soulseek (the account the Sockseek
daemon logs in with; Echolot writes its login file, the daemon restarts with it)."""

import secrets
import shutil
import sqlite3
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from echolot import db
from echolot.services import slskd, soulseek, soundcloud, spotify
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
    """What the logged-in user has connected (and Soulseek), for this page and the overview: asks the
    services at once (`cached`: a result of the last CACHE_SECONDS will do)."""
    state, uid = request.app.state, request.state.user.id
    if not hasattr(state, "account_status"):
        state.account_status = {}
    last = state.account_status.get(uid)
    if cached and last and time.monotonic() - last[0] < CACHE_SECONDS:
        return last[1]
    opts = options.get(con, options.Soulseek)
    slsk: dict[str, Any] = {
        "user": opts.user,
        "password": state.vault.has(con, "soulseek.password"),
        "backend": opts.backend,
        "slskd_url": opts.slskd_url,
        "slskd_user": opts.slskd_user,
        "slskd_downloads": opts.slskd_downloads,
        "slskd_secret": state.vault.has(con, slskd.SECRET),
    }
    client = slskd.connect(con, state.vault, opts, timeout=5) if opts.backend == "slskd" else None
    try:  # a secret stored with another key can't be read: shown as the account's error
        token, token_error = soundcloud.token_of(con, state.vault, uid), None
    except VaultError as e:
        token, token_error = None, str(e)
    with ThreadPoolExecutor(3) as pool:
        sp = pool.submit(_spotify_status, state, uid)
        sc = pool.submit(_soundcloud_status, token, token_error)
        daemon = pool.submit(_daemon_status, opts.url, client)
        result = {"spotify": sp.result(), "soundcloud": sc.result(), "soulseek": slsk | daemon.result()}
    state.account_status[uid] = (time.monotonic(), result)
    return result


def known(request: Request, con: sqlite3.Connection) -> dict[str, Any]:
    """What the logged-in user has connected, without asking the services (for pages that must not
    wait): their stored logins, and the account names of their last status check."""
    uid, vault = request.state.user.id, request.app.state.vault
    last = getattr(request.app.state, "account_status", {}).get(uid, (0, {}))[1]
    sp = options.get(con, options.Spotify).client_id and vault.has(con, spotify.refresh_name(uid))
    connected = {"spotify": bool(sp), "soundcloud": vault.has(con, soundcloud.token_name(uid))}
    return {s: {"connected": on, "name": (last.get(s) or {}).get("name")} for s, on in connected.items()}


def _spotify_status(state: Any, uid: int) -> dict[str, Any]:
    sp: dict[str, Any] = {"app": False, "connected": False, "client_id": ""}
    con = db.connect(state.settings.db_path)  # its own: the login may store a new refresh token
    try:
        creds = spotify.Credentials.load(con, state.vault, uid)
        sp.update(app=creds.app, connected=creds.connected, client_id=creds.client_id)
        if creds.connected:
            me = spotify.Spotify(con, state.vault, uid).me()
            sp.update(name=me.get("display_name") or me.get("id"), error=None)
    except (spotify.SpotifyError, VaultError) as e:
        sp["error"] = str(e)
    finally:
        con.close()
    return sp


def _soundcloud_status(token: str | None, token_error: str | None) -> dict[str, Any]:
    if token_error or not token:
        return {"connected": False} | ({"error": token_error} if token_error else {})
    try:
        return {"connected": True, **soundcloud.me(token)}
    except soundcloud.SoundCloudError as e:
        return {"connected": False, "error": str(e)}


def _daemon_status(url: str, client: Any = None) -> dict[str, Any]:
    """The Soulseek client's state: the Sockseek daemon at `url`, or `client` (slskd)."""
    try:
        return (client or soulseek.Daemon(url, timeout=5)).status() | {"reachable": True}
    except soulseek.DaemonError as e:
        return {"reachable": False, "ready": False, "error": str(e)}


@router.get("/accounts")
def accounts_redirect() -> RedirectResponse:
    """The accounts' old page: Spotify and SoundCloud are on Playlists, Soulseek in Settings."""
    return RedirectResponse("/playlists", status_code=301)


def connections(request: Request, con: sqlite3.Connection) -> dict[str, Any]:
    """What the Playlists page needs to show and set up the user's Spotify and SoundCloud accounts."""
    return {
        "s": status(request, con),
        "market": options.get(con, options.Spotify).market,
        "redirect_uri": redirect_uri(request),
        "automatic": request.url.scheme == "https",
    }


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
    request: Request,
    con: DB,
    client_id: Annotated[str, Form()],
    client_secret: Annotated[str, Form()] = "",
    market: Annotated[str, Form()] = "",
) -> RedirectResponse:
    client_id, client_secret, market = client_id.strip(), client_secret.strip(), market.strip().upper()
    if market and len(market) != 2:
        return back("/playlists#spotify", error="The country is its two letters, e.g. DE or US.")
    if len(client_id) != 32:
        return back("/playlists#spotify", error="The client ID is the 32 characters under the app's name.")
    vault = request.app.state.vault
    if not client_secret and not vault.get(con, spotify.SECRET):
        return back(
            "/playlists#spotify", error="Paste the client secret too (the app's Settings, 'View client secret')."
        )
    with con:
        options.update(con, options.Spotify, client_id=client_id)
        if market:
            options.update(con, options.Spotify, market=market)
        if client_secret:
            vault.set(con, spotify.SECRET, client_secret)
    return back("/playlists#spotify", ok="Spotify app saved. Now connect your account.")


@router.post("/accounts/spotify/market")
def spotify_market(con: DB, market: Annotated[str, Form()]) -> RedirectResponse:
    """The country whose catalogue the availability check asks."""
    try:
        with con:
            options.update(con, options.Spotify, market=market.strip().upper())
    except options.OptionsError:
        return back("/playlists#spotify", error="The country is its two letters, e.g. DE or US.")
    return back("/playlists#spotify", ok="Country saved; the next availability check asks its catalogue.")


@router.post("/accounts/spotify/login")
def spotify_login(request: Request, con: DB) -> Response:
    """Off to Spotify's login; it comes back to the redirect address with a code."""
    sp = options.get(con, options.Spotify)
    if not sp.client_id:
        return back("/playlists#spotify", error="Save your Spotify app's client ID and secret first.")
    state, redirect = secrets.token_urlsafe(16), redirect_uri(request)
    with con:
        db.set_meta(con, f"{STATE}:{request.state.user.id}", f"{state} {int(time.time())} {redirect}")
    url = spotify.authorize_url(sp.client_id, redirect, state)
    return RedirectResponse(url, status_code=303)


def _finish_login(request: Request, con: sqlite3.Connection, query: dict[str, list[str]]) -> RedirectResponse:
    if error := (query.get("error") or [""])[0]:
        return back("/playlists#spotify", error=f"Spotify: {error.replace('_', ' ')}.")
    code, state, uid = (query.get("code") or [""])[0], (query.get("state") or [""])[0], request.state.user.id
    expected, started, redirect = ([*db.get_meta(con, f"{STATE}:{uid}").split(" "), "", ""])[:3]
    if not code or not state or state != expected or not redirect or time.time() - int(started or 0) > 1800:
        return back("/playlists#spotify", error="That is not the address of the last Spotify login (or it is older "
                                       "than 30 min). Click 'Connect Spotify' again.")  # fmt: skip
    try:
        spotify.exchange(con, request.app.state.vault, code, redirect, uid)  # the address the login used
    except spotify.SpotifyError as e:
        return back("/playlists#spotify", error=str(e))
    with con:
        db.set_meta(con, f"{STATE}:{uid}", "")
    return back("/playlists#spotify", ok="Spotify connected.")


@router.get(CALLBACK)
def spotify_callback(request: Request, con: DB) -> RedirectResponse:
    return _finish_login(request, con, urllib.parse.parse_qs(request.url.query))


@router.post("/accounts/spotify/paste")
def spotify_paste(request: Request, con: DB, url: Annotated[str, Form()]) -> RedirectResponse:
    """The address the browser showed after the login (it could not open 127.0.0.1)."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url.strip()).query)
    if not query.get("code") and not query.get("error"):
        return back("/playlists#spotify", error="Paste the whole address from the address bar (it contains ?code=...).")
    return _finish_login(request, con, query)


@router.post("/accounts/spotify/disconnect")
def spotify_disconnect(request: Request, con: DB) -> RedirectResponse:
    with con:
        request.app.state.vault.delete(con, spotify.refresh_name(request.state.user.id))
    return back("/playlists#spotify", ok="Spotify disconnected. Your lists and songs stay.")


# ------------------------------------------------------------ SoundCloud


@router.post("/accounts/soundcloud")
def soundcloud_token(request: Request, con: DB, token: Annotated[str, Form()]) -> RedirectResponse:
    token = token.strip().removeprefix("OAuth ").strip()
    try:
        me = soundcloud.me(token)
    except soundcloud.SoundCloudError as e:
        return back("/playlists#soundcloud", error=str(e))
    uid = request.state.user.id
    with con:
        request.app.state.vault.set(con, soundcloud.token_name(uid), token)
        con.execute("UPDATE users SET soundcloud_user = ? WHERE id = ?", (me["user"], uid))
    return back("/playlists#soundcloud", ok=f"SoundCloud connected as {me['name']}.")


@router.post("/accounts/soundcloud/disconnect")
def soundcloud_disconnect(request: Request, con: DB) -> RedirectResponse:
    with con:
        request.app.state.vault.delete(con, soundcloud.token_name(request.state.user.id))
    return back("/playlists#soundcloud", ok="SoundCloud disconnected. Your lists and songs stay.")


# ------------------------------------------------------------ Soulseek


@router.post("/accounts/soulseek")
def soulseek_account(
    request: Request, con: DB, user: Annotated[str, Form()], password: Annotated[str, Form()] = ""
) -> RedirectResponse:
    user = user.strip()
    vault = request.app.state.vault
    password = password or vault.get(con, "soulseek.password") or ""
    if not user or not password or any(c in user + password for c in "\n\r"):
        return back("/settings#soulseek-client", error="Soulseek needs a user name and a password.")
    folder = request.app.state.settings.daemon_dir
    if folder is None:
        return back("/settings#soulseek-client", error="No daemon directory configured (ECHOLOT_DAEMON_DIR).")
    with con:
        options.update(con, options.Soulseek, user=user)
        vault.set(con, "soulseek.password", password)
    soulseek.write_conf(folder, user, password)
    return back("/settings#soulseek-client", ok="Soulseek account saved; the daemon logs in with it in a moment.")


@router.post("/accounts/soulseek/backend")
def soulseek_backend(
    request: Request,
    con: DB,
    backend: Annotated[str, Form()],
    slskd_url: Annotated[str, Form()] = "",
    slskd_user: Annotated[str, Form()] = "",
    slskd_secret: Annotated[str, Form()] = "",
    slskd_downloads: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Which Soulseek client Echolot uses: the Sockseek daemon, or slskd (its address, login and downloads
    folder; an empty secret keeps the saved one)."""
    vault = request.app.state.vault
    fields: dict[str, Any] = {"backend": backend}
    if backend == "slskd":
        fields |= {
            "slskd_url": slskd_url.strip().rstrip("/"),
            "slskd_user": slskd_user.strip(),
            "slskd_downloads": slskd_downloads.strip().rstrip("/"),
        }
    try:
        with con:
            options.update(con, options.Soulseek, **fields)
            if backend == "slskd" and slskd_secret:
                vault.set(con, slskd.SECRET, slskd_secret)
    except options.OptionsError as e:
        return back("/settings#soulseek-client", error=str(e))
    name = "slskd" if backend == "slskd" else "the Sockseek daemon"
    return back("/settings#soulseek-client", ok=f"Soulseek through {name} from the next search on.")


@router.post("/accounts/soulseek/url")
def soulseek_url(con: DB, url: Annotated[str, Form()]) -> RedirectResponse:
    try:
        with con:
            options.update(con, options.Soulseek, url=url.strip().rstrip("/"))
    except options.OptionsError as e:
        return back("/settings#soulseek-client", error=str(e))
    return back("/settings#soulseek-client", ok="Daemon address saved.")
