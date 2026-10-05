"""Navidrome, the music server. Its accounts are Echolot's users: the password is checked by Navidrome's
own login (POST /auth/login, as its web page does); Echolot keeps neither the password nor the token
Navidrome answers with. The service account (an admin of Navidrome, its password in the vault) reads
Navidrome's user list and, later, sets the owners of the playlists Echolot writes (native API, /api)."""

import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from echolot.settings.vault import Vault

SERVICE_PASSWORD = "navidrome.service_password"  # vault
TOKEN_SECONDS = 3600  # a service login is used this long (Navidrome's own lasts much longer)
_TOKENS: dict[tuple[str, str], tuple[str, float]] = {}  # (address, user) -> (token, when)
_LOCK = threading.Lock()


class NavidromeError(Exception):
    """Navidrome not reachable, or an answer that is no login (not a wrong password)."""


def _post_login(url: str, name: str, password: str) -> dict[str, Any] | None:
    body = json.dumps({"username": name, "password": password}).encode()
    request = urllib.request.Request(
        url.rstrip("/") + "/auth/login", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return None
        raise NavidromeError(f"Navidrome answered HTTP {e.code}") from e
    except (OSError, ValueError) as e:
        raise NavidromeError(f"Navidrome not reachable: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("username"), str) or not data["username"]:
        raise NavidromeError("Navidrome's answer has no user name")
    return data


def login(url: str, name: str, password: str) -> tuple[str, bool, str] | None:
    """(user name as Navidrome spells it, admin, Navidrome's user id) if Navidrome takes the password,
    None if it does not."""
    data = _post_login(url, name, password)
    if data is None:
        return None
    return data["username"], data.get("isAdmin") is True, str(data.get("id") or data["username"])


def reachable(url: str) -> bool:
    """Navidrome answers at this address (its /ping)."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/ping", timeout=5) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False


class Service:
    """Navidrome's native API as its admin (the service account); one login per hour, again after a 401."""

    def __init__(self, url: str, user: str, password: str) -> None:
        self.url, self.user, self.password = url.rstrip("/"), user, password

    def _token(self, fresh: bool = False) -> str:
        key = (self.url, self.user)
        with _LOCK:
            cached = _TOKENS.get(key)
            if cached and not fresh and time.monotonic() - cached[1] < TOKEN_SECONDS:
                return cached[0]
            data = _post_login(self.url, self.user, self.password)
            if data is None:
                raise NavidromeError(f"Navidrome does not take the service account {self.user}'s password")
            if data.get("isAdmin") is not True:
                raise NavidromeError(f"The service account {self.user} is no Navidrome admin")
            _TOKENS[key] = (data["token"], time.monotonic())
            return data["token"]

    def call(self, method: str, path: str, body: Any = None) -> Any:
        payload = json.dumps(body).encode() if body is not None else None
        for fresh in (False, True):
            headers = {"X-ND-Authorization": f"Bearer {self._token(fresh)}", "Content-Type": "application/json"}
            request = urllib.request.Request(self.url + path, data=payload, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=20) as r:
                    text = r.read()
                return json.loads(text) if text else None
            except urllib.error.HTTPError as e:
                if e.code == 401 and not fresh:
                    continue  # the login ran out: once more with a new one
                raise NavidromeError(f"Navidrome: {method} {path}: HTTP {e.code}") from e
            except (OSError, ValueError) as e:
                raise NavidromeError(f"Navidrome not reachable: {e}") from e
        raise NavidromeError("unreachable")

    def users(self) -> list[dict[str, Any]]:
        """Navidrome's accounts: id, userName, isAdmin, ..."""
        return list(self.call("GET", "/api/user") or [])

    def playlists(self) -> list[dict[str, Any]]:
        """Every user's playlists (an admin sees all): id, name, path (of an imported file), ownerId, ..."""
        return list(self.call("GET", "/api/playlist?_start=0&_end=100000") or [])

    def set_owner(self, playlist_id: str, owner_id: str) -> None:
        """Give a playlist to another user (it stays theirs when its file is imported again)."""
        self.update_playlist(playlist_id, {"ownerId": owner_id})

    def update_playlist(self, playlist_id: str, fields: dict[str, Any]) -> None:
        """Change a playlist's fields (ownerId, comment); Navidrome keeps them when its file is imported again."""
        self.call("PUT", f"/api/playlist/{playlist_id}", fields)

    def delete_playlist(self, playlist_id: str) -> None:
        self.call("DELETE", f"/api/playlist/{playlist_id}")


def address(con: sqlite3.Connection) -> str:
    """Navidrome's address: the setting, else ECHOLOT_NAVIDROME_URL (how a new installation gets one)."""
    from echolot.settings import options

    return (options.get(con, options.Navidrome).url or os.environ.get("ECHOLOT_NAVIDROME_URL", "")).rstrip("/")


def service(con: sqlite3.Connection, vault: "Vault") -> Service | None:
    """The service account's client, None while its password is not set."""
    from echolot.settings import options

    url, password = address(con), vault.get(con, SERVICE_PASSWORD)
    return Service(url, options.get(con, options.Navidrome).service_user, password) if url and password else None
