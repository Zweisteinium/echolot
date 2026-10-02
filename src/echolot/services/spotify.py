"""Spotify Web API: the connected account's lists, covers, and the login (authorization code flow).

A developer app of the user's own (developer.spotify.com, development mode: up to 5 users, the owner
needs Premium) gives the client id (settings section spotify) and secret (vault); the connected account's
refresh token is in the vault too. Spotify's own editorial playlists can't be read through the API.
"""

import base64
import json
import logging
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from echolot.settings import options
from echolot.settings.vault import Vault

log = logging.getLogger(__name__)

API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"
SCOPES = "user-library-read playlist-read-private playlist-read-collaborative"
LIKED_SONGS_IMAGE = "https://misc.scdn.co/liked-songs/liked-songs-640.png"
SECRET, REFRESH = "spotify.client_secret", "spotify.refresh_token"  # REFRESH:<user id>, one login per user


def refresh_name(user_id: int) -> str:
    """The vault name of a user's Spotify login."""
    return f"{REFRESH}:{user_id}"


_tokens: dict[str, tuple[str, float]] = {}  # refresh token hash -> (access token, expires)
_lock = threading.Lock()


class SpotifyError(RuntimeError):
    """Spotify is not connected or refused (message for the user)."""


def playlist_id(url: str) -> str | None:
    m = re.search(r"playlist[/:]([A-Za-z0-9]+)", url)
    return m.group(1) if m else None


@dataclass
class Credentials:
    client_id: str
    client_secret: str | None
    refresh_token: str | None
    user_id: int | None = None  # whose login (None: the app's own access, catalogue only)

    @classmethod
    def load(cls, con: sqlite3.Connection, vault: Vault, user_id: int | None = None) -> "Credentials":
        refresh = vault.get(con, refresh_name(user_id)) if user_id is not None else None
        return cls(options.get(con, options.Spotify).client_id, vault.get(con, SECRET), refresh, user_id)

    @property
    def app(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def connected(self) -> bool:
        return self.app and bool(self.refresh_token)


def _post_token(creds: Credentials, form: dict[str, str]) -> dict[str, Any]:
    auth = base64.b64encode(f"{creds.client_id}:{creds.client_secret}".encode()).decode()
    req = urllib.request.Request(
        f"{ACCOUNTS}/api/token",
        data=urllib.parse.urlencode(form).encode(),
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            detail = json.loads(body).get("error_description") or json.loads(body).get("error")
        except ValueError:
            detail = body[:200]
        raise SpotifyError(f"Spotify refused: {detail or e.code}") from e


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = {"client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
             "scope": SCOPES, "state": state, "show_dialog": "true"}  # fmt: skip
    return f"{ACCOUNTS}/authorize?{urllib.parse.urlencode(query)}"


def exchange(con: sqlite3.Connection, vault: Vault, code: str, redirect_uri: str, user_id: int) -> None:
    """The code from a user's login redirect -> their refresh token, stored in the vault."""
    creds = Credentials.load(con, vault)
    d = _post_token(creds, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
    if not d.get("refresh_token"):
        raise SpotifyError("Spotify answered without a refresh token.")
    with con:
        vault.set(con, refresh_name(user_id), d["refresh_token"])
    with _lock:
        _tokens.clear()


class Spotify:
    """Spotify's Web API as a user (their login: their lists and likes), or as the app itself (no user:
    the catalogue only, e.g. covers and artist pictures; Spotify's client credentials)."""

    def __init__(self, con: sqlite3.Connection, vault: Vault, user_id: int | None = None) -> None:
        self.con, self.vault = con, vault
        self.creds = Credentials.load(con, vault, user_id)
        if not (self.creds.connected if user_id is not None else self.creds.app):
            raise SpotifyError("Spotify is not connected." if user_id is not None else "No Spotify app set up.")

    def token(self) -> str:
        if self.creds.user_id is None:
            return self._app_token()
        key = str(hash(self.creds.refresh_token))
        with _lock:
            cached = _tokens.get(key)
            if cached and cached[1] > time.time() + 60:
                return cached[0]
        d = _post_token(self.creds, {"grant_type": "refresh_token", "refresh_token": self.creds.refresh_token or ""})
        if (new := d.get("refresh_token")) and new != self.creds.refresh_token:  # Spotify may rotate it
            with self.con:
                self.vault.set(self.con, refresh_name(self.creds.user_id), new)
            self.creds.refresh_token = new
            key = str(hash(new))
        with _lock:
            _tokens[key] = (d["access_token"], time.time() + int(d.get("expires_in") or 3600))
        return d["access_token"]

    def _app_token(self) -> str:
        key = f"app:{self.creds.client_id}"
        with _lock:
            cached = _tokens.get(key)
            if cached and cached[1] > time.time() + 60:
                return cached[0]
        d = _post_token(self.creds, {"grant_type": "client_credentials"})
        with _lock:
            _tokens[key] = (d["access_token"], time.time() + int(d.get("expires_in") or 3600))
        return d["access_token"]

    def get(self, url: str) -> dict[str, Any]:
        url = url if url.startswith("http") else API + url
        for attempt in range(6):
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token()}"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    wait = int(e.headers.get("Retry-After") or 10 * (attempt + 1))
                    log.info("spotify: HTTP %s, waiting %s s", e.code, wait)
                    time.sleep(min(wait, 120))
                    continue
                if e.code == 404:
                    raise SpotifyError("not found (Spotify's own playlists can't be read)") from e
                raise SpotifyError(f"Spotify: HTTP {e.code} for {url.split('?')[0]}") from e
            except (OSError, TimeoutError) as e:
                if attempt == 5:
                    raise SpotifyError(f"Spotify not reachable: {e}") from e
                time.sleep(5 * (attempt + 1))
        raise SpotifyError(f"Spotify: giving up on {url.split('?')[0]}")

    def pages(self, url: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        while next_url:
            d = self.get(next_url)
            items += d.get("items") or []
            next_url = d.get("next")
        return items

    # ------------------------------------------------------------ account and lists

    def me(self) -> dict[str, Any]:
        return self.get("/me")

    def playlists(self) -> list[dict[str, Any]]:
        """The account's playlists (own and followed): id, name, owner, own, collaborative, readable, songs,
        image, url. Spotify's rules let development-mode apps read only own and collaborative playlists; where
        it enforces that, the listing lacks the songs' count (readable False)."""
        me = self.me().get("id")
        out = []
        for p in self.pages("/me/playlists?limit=50"):
            if not p or not p.get("id"):
                continue
            listing = p.get("items") or p.get("tracks")
            out.append({
                "id": p["id"], "name": p.get("name") or "", "url": f"https://open.spotify.com/playlist/{p['id']}",
                "owner": (p.get("owner") or {}).get("display_name") or "", "own": (p.get("owner") or {}).get("id") == me,
                "collaborative": bool(p.get("collaborative")), "readable": listing is not None,
                "songs": (listing or {}).get("total") or 0,
                "image": ((p.get("images") or [{}])[0] or {}).get("url"),
            })  # fmt: skip
        return out

    def playlist(self, pid: str) -> dict[str, Any]:
        """name, image, snapshot of a playlist."""
        d = self.get(f"/playlists/{pid}?fields=name,images,snapshot_id")
        return {"name": d.get("name") or "", "image": ((d.get("images") or [{}])[0] or {}).get("url"),
                "snapshot": d.get("snapshot_id") or ""}  # fmt: skip

    def snapshots(self) -> dict[str, str]:
        """The snapshot of every playlist in the account's library (own and followed): one request per 50."""
        return {p["id"]: p.get("snapshot_id") or "" for p in self.pages("/me/playlists?limit=50") if p and p.get("id")}

    def likes_state(self) -> str:
        """'<count>:<newest added>:<its id>': changes with every like added or removed (one request)."""
        d = self.get("/me/tracks?limit=1")
        it = (d.get("items") or [{}])[0] or {}
        return f"{d.get('total') or 0}:{it.get('added_at') or ''}:{(it.get('track') or {}).get('id') or ''}"

    def liked_count(self) -> int:
        return int(self.get("/me/tracks?limit=1").get("total") or 0)

    def items(self, pid: str | None) -> list[dict[str, Any]]:
        """Songs of a playlist (None: Liked Songs) in list order: id, artist (first), artists, title,
        album, length (s), isrc. Local files and podcast episodes are left out."""
        url = "/me/tracks?limit=50" if pid is None else f"/playlists/{pid}/items?limit=50"
        out = []
        for it in self.pages(url):
            t = it.get("item") or it.get("track")
            if not t or t.get("type") not in (None, "track") or t.get("is_local") or not t.get("id"):
                continue
            artists = [a.get("name", "") for a in t.get("artists") or []]
            out.append({
                "id": t["id"], "artist": artists[0] if artists else "", "artists": artists,
                "title": t.get("name") or "", "album": (t.get("album") or {}).get("name") or "",
                "length": round((t.get("duration_ms") or 0) / 1000),
                "isrc": (t.get("external_ids") or {}).get("isrc"),
            })  # fmt: skip
        return out

    def track(self, track_id: str) -> dict[str, Any]:
        return self.get(f"/tracks/{track_id}")

    def find_track(self, artist: str, title: str) -> dict[str, Any] | None:
        q = urllib.parse.quote(f"track:{title} artist:{artist}")
        items = (self.get(f"/search?q={q}&type=track&limit=1").get("tracks") or {}).get("items") or []
        return items[0] if items else None

    def artist_image(self, artist_id: str) -> str | None:
        return ((self.get(f"/artists/{artist_id}").get("images") or [{}])[0] or {}).get("url")
