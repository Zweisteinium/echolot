"""Navidrome, the music server: its accounts can log in to Echolot. The password is checked by Navidrome's
own login (POST /auth/login, as its web page does); Echolot keeps neither the password nor the token
Navidrome answers with, only the user name and whether the user is a Navidrome admin."""

import json
import urllib.error
import urllib.request


class NavidromeError(Exception):
    """Navidrome not reachable, or an answer that is no login (not a wrong password)."""


def login(url: str, name: str, password: str) -> tuple[str, bool] | None:
    """(user name as Navidrome spells it, admin) if Navidrome takes the password, None if it does not."""
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
    user = data.get("username") if isinstance(data, dict) else None
    if not isinstance(user, str) or not user:
        raise NavidromeError("Navidrome's answer has no user name")
    return user, data.get("isAdmin") is True


def reachable(url: str) -> bool:
    """Navidrome answers at this address (its /ping)."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/ping", timeout=5) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False
