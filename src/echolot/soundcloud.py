"""SoundCloud's web API with the account's OAuth token (SoundCloud gives out no app keys): who the token
belongs to, and the sets in its library (own and liked) for the Sources page. Downloads go through yt-dlp.
"""

import json
import urllib.error
import urllib.request
from typing import Any

API = "https://api-v2.soundcloud.com"
TOKEN = "soundcloud.token"


class SoundCloudError(RuntimeError):
    """The token is missing, expired or refused (message for the user)."""


def _get(token: str, path: str) -> Any:
    req = urllib.request.Request(f"{API}{path}", headers={"Authorization": f"OAuth {token}",
                                                          "User-Agent": "Mozilla/5.0"})  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SoundCloudError("SoundCloud refused the token (expired or copied wrong).") from e
        raise SoundCloudError(f"SoundCloud: HTTP {e.code}") from e
    except (OSError, TimeoutError, ValueError) as e:
        raise SoundCloudError(f"SoundCloud not reachable: {e}") from e


def me(token: str) -> dict[str, Any]:
    """The account: user (the name in its links), name, avatar, likes count."""
    d = _get(token, "/me")
    return {"user": d.get("permalink") or "", "name": d.get("username") or "",
            "avatar": d.get("avatar_url"), "likes": d.get("likes_count") or 0}  # fmt: skip


def sets(token: str) -> list[dict[str, Any]]:
    """The sets in the account's library, own and liked: url, name, owner, songs, image, own."""
    out = []
    for it in _get(token, "/me/library/all?limit=200").get("collection") or []:
        kind, p = it.get("type") or "", it.get("playlist") or {}
        if kind not in ("playlist", "playlist-like") or not p.get("permalink_url"):
            continue  # stations and other generated lists can't be followed
        out.append({"url": p["permalink_url"], "name": p.get("title") or "",
                    "owner": (p.get("user") or {}).get("username") or "", "songs": p.get("track_count") or 0,
                    "image": (p.get("artwork_url") or "").replace("-large.", "-t500x500.") or None,
                    "own": kind == "playlist"})  # fmt: skip
    return out
