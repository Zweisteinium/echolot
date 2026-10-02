"""SoundCloud's web API with the account's OAuth token (SoundCloud gives out no app keys): who the token
belongs to, and the sets in its library (own and liked) for the Sources page. Downloads go through yt-dlp.
"""

import json
import sqlite3
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from echolot.settings.vault import Vault

API = "https://api-v2.soundcloud.com"
TOKEN = "soundcloud.token"  # TOKEN:<user id>: one login per user


def token_name(user_id: int) -> str:
    """The vault name of a user's SoundCloud token."""
    return f"{TOKEN}:{user_id}"


def token_of(con: sqlite3.Connection, vault: "Vault", user_id: int) -> str | None:
    return vault.get(con, token_name(user_id))


def any_token(con: sqlite3.Connection, vault: "Vault") -> str | None:
    """A connected user's token, the oldest user's first: for what is no one's in particular (a song's
    artwork, a download; SoundCloud hands them to any account)."""
    users = [r[0] for r in con.execute("SELECT id FROM users WHERE NOT disabled ORDER BY id")]
    return next((t for uid in users if (t := vault.get(con, token_name(uid)))), None)


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


def artwork(token: str, track_id: str) -> str | None:
    """A track's artwork (500 px), else its uploader's picture; None if it has neither."""
    d = _get(token, f"/tracks/{track_id}")
    url = d.get("artwork_url") or (d.get("user") or {}).get("avatar_url")
    return url.replace("-large.", "-t500x500.") if url else None


def tracks(token: str, ids: list[str]) -> list[dict[str, Any]]:
    """Up to 50 tracks at once (id, policy, snipped, ...); one deleted or private is left out."""
    return list(_get(token, f"/tracks?ids={','.join(ids)}") or [])


def states(token: str) -> dict[str, str]:
    """Per list page, what changes when the list changes (three requests): the account's likes
    ('<count>:<newest like>:<its track>') and every set in its library ('<songs>:<last change>')."""
    me = _get(token, "/me")
    newest = (_get(token, f"/users/{me['id']}/track_likes?limit=1").get("collection") or [{}])[0] or {}
    track = (newest.get("track") or {}).get("id") or ""
    out = {
        f"https://soundcloud.com/{me.get('permalink')}/likes": f"{me.get('likes_count')}:{newest.get('created_at')}:{track}"
    }
    for it in _get(token, "/me/library/all?limit=200").get("collection") or []:
        if (p := it.get("playlist") or {}).get("permalink_url"):
            out[p["permalink_url"]] = f"{p.get('track_count')}:{p.get('last_modified')}"
    return out
