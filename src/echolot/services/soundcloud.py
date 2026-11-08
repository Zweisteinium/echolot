"""SoundCloud's web API with a user's browser OAuth token (SoundCloud gives out no app keys): the account,
its library's sets, search, streams. Downloads go through yt-dlp. The token lasts a day; token_of renews it
with the login's refresh token, as the browser does.
"""

import base64
import datetime
import json
import logging
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from echolot.library.discover import Hit
    from echolot.settings.vault import Vault

API = "https://api-v2.soundcloud.com"
SECURE = "https://secure.soundcloud.com"  # its login server: renews tokens
TOKEN = "soundcloud.token"  # TOKEN:<user id>: one login per user
REFRESH = "soundcloud.refresh_token"  # REFRESH:<user id>: the browser's cookie oauth_refresh_token, works once
RENEW_BEFORE = 3600  # s before a token expires that it is renewed
RETRY_AFTER = 900  # s after a renewal failed (SoundCloud not reachable) before the next try

log = logging.getLogger(__name__)
_renewing = threading.Lock()  # one renewal at a time: a second with the same refresh token would be refused
_failed: dict[int, float] = {}  # user id -> when a renewal last failed


def token_name(user_id: int) -> str:
    return f"{TOKEN}:{user_id}"


def refresh_name(user_id: int) -> str:
    return f"{REFRESH}:{user_id}"


def token_of(con: sqlite3.Connection, vault: "Vault", user_id: int) -> str | None:
    """The user's token, renewed first when it expires within RENEW_BEFORE (the new pair is stored at once:
    the old refresh token is used up); else the stored one."""
    token = vault.get(con, token_name(user_id))
    if not (token and _due(token)) or time.time() < _failed.get(user_id, 0) + RETRY_AFTER:
        return token
    with _renewing:
        token = vault.get(con, token_name(user_id)) or token  # (another thread may have renewed it)
        if not _due(token) or not (refresh := vault.get(con, refresh_name(user_id))):
            return token
        try:
            new, refresh = renew(token, refresh)
        except Refused as e:  # used up or logged out: of no more use
            log.warning("soundcloud: the token of user %s is not renewed: %s", user_id, e)
            with con:
                vault.delete(con, refresh_name(user_id))
            return token
        except SoundCloudError as e:
            log.warning("soundcloud: the token of user %s is not renewed (tried again later): %s", user_id, e)
            _failed[user_id] = time.time()
            return token
        try:
            with con:
                vault.set(con, token_name(user_id), new)
                vault.set(con, refresh_name(user_id), refresh)
        except sqlite3.Error as e:  # (the old pair works no more: only a new login helps)
            log.error("soundcloud: the renewed token of user %s could not be stored: %s", user_id, e)
            return new
        _failed.pop(user_id, None)
        log.info("soundcloud: the token of user %s renewed, works until %s", user_id, expires(new))
        return new


def _due(token: str) -> bool:
    return (when := expires(token)) is not None and when.timestamp() < time.time() + RENEW_BEFORE


def any_token(con: sqlite3.Connection, vault: "Vault") -> str | None:
    """The oldest connected user's token, for what any account may fetch (artwork, downloads)."""
    users = [r[0] for r in con.execute("SELECT id FROM users WHERE NOT disabled ORDER BY id")]
    return next((t for uid in users if (t := token_of(con, vault, uid))), None)


class SoundCloudError(RuntimeError):
    """The token is missing, expired or refused (message for the user)."""


class Refused(SoundCloudError):
    """SoundCloud refused a refresh token: used already, or the login ended."""


def _claims(token: str) -> dict[str, Any]:
    """What a JWT token says about itself; {} for older ones (2-…)."""
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def expires(token: str) -> datetime.datetime | None:
    """When the token expires: login tokens are JWTs that last a day; older ones (2-…) don't say."""
    try:
        return datetime.datetime.fromtimestamp(int(_claims(token)["exp"]), datetime.UTC)
    except (KeyError, TypeError, ValueError):
        return None


def renew(token: str, refresh: str) -> tuple[str, str]:
    """A new (token, refresh token), asked of the login server as SoundCloud's website does (the token's
    client id, no secret). A refresh token works once: Refused after."""
    client = _claims(token).get("client_id")
    if not client:
        raise SoundCloudError("The refresh token needs the token beside it (the one that starts with eyJ).")
    body = urllib.parse.urlencode({"client_id": client, "grant_type": "refresh_token", "refresh_token": refresh})
    form = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(f"{SECURE}/oauth/token", data=body.encode(), headers=form)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (400, 401):  # {"error": "invalid_grant"}
            raise Refused("SoundCloud refused the refresh token (used already, copied wrong or logged out).") from e
        raise SoundCloudError(f"SoundCloud: HTTP {e.code} renewing the token") from e
    except (OSError, TimeoutError, ValueError) as e:
        raise SoundCloudError(f"SoundCloud not reachable: {e}") from e
    if not (isinstance(d, dict) and d.get("access_token") and d.get("refresh_token")):
        raise SoundCloudError("SoundCloud answered without a new token.")
    return d["access_token"], d["refresh_token"]


def expired(token: str) -> bool:
    return (when := expires(token)) is not None and when <= datetime.datetime.now(datetime.UTC)


def _get(token: str, path: str) -> Any:
    req = urllib.request.Request(f"{API}{path}", headers={"Authorization": f"OAuth {token}",
                                                          "User-Agent": "Mozilla/5.0"})  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and expired(token) and (when := expires(token)):
            raise SoundCloudError(
                f"SoundCloud's token expired on {when.astimezone():%Y-%m-%d %H:%M}: its login tokens last a day."
            ) from e
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


def stream(token: str, page: str) -> str:
    """The URL of a track's best stream of STREAMS (HLS or a file: ffmpeg reads both); SoundCloudError if it
    has only encrypted ones. A track this account may only preview gives the snippet."""
    t = _get(token, f"/resolve?url={urllib.parse.quote(page, safe='')}")
    codings = [c for c in (t.get("media") or {}).get("transcodings") or [] if c.get("url")]
    rank = {kind: n for n, kind in enumerate(STREAMS)}
    usable = [c for c in codings if _kind(c) in rank]
    if not usable:
        raise SoundCloudError("SoundCloud has no stream of it for this account")
    best = min(usable, key=lambda c: rank[_kind(c)])
    query = urllib.parse.urlencode({"track_authorization": t.get("track_authorization") or ""})
    return _get(token, best["url"].removeprefix(API) + ("&" if "?" in best["url"] else "?") + query)["url"]


STREAMS = ("hls:audio/mp4", "progressive:audio/mpeg", "hls:audio/mpeg", "hls:audio/ogg")  # best first


def _kind(coding: dict[str, Any]) -> str:
    f = coding.get("format") or {}
    return f"{f.get('protocol')}:{(f.get('mime_type') or '').split(';')[0]}"


def states(token: str) -> dict[str, str]:
    """Per list page, a value that changes with the list (three requests): the likes
    ('<count>:<newest like>:<its track>') and every library set ('<songs>:<last change>')."""
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


def liked(token: str, page: str) -> dict[str, str]:
    """Track id -> when the likes page's account liked it (a request per 200)."""
    user = _get(token, f"/resolve?url={urllib.parse.quote(page.rstrip('/').removesuffix('/likes'), safe='')}")
    out: dict[str, str] = {}
    path = f"/users/{user['id']}/track_likes?limit=200"
    while path:
        d = _get(token, path)
        for it in d.get("collection") or []:
            if tid := (it.get("track") or {}).get("id"):
                out.setdefault(str(tid), it.get("created_at") or "")
        path = (d.get("next_href") or "").removeprefix(API) if d.get("collection") else ""
    return out


def search(token: str, query: str, limit: int = 20) -> list["Hit"]:
    """SoundCloud's search results as discover.Hit, best first, names split as for downloads (artist_title)."""
    from echolot.library.discover import Hit
    from echolot.library.tagging import clean_title
    from echolot.services.ytdlp import artist_title

    found = _get(token, f"/search/tracks?q={urllib.parse.quote(query)}&limit={limit}") or {}
    out = []
    for n, t in enumerate(found.get("collection") or []):
        meta, uploader = t.get("publisher_metadata") or {}, (t.get("user") or {}).get("username") or ""
        artist, title = artist_title(uploader, meta.get("artist") or "", t.get("title") or "")
        hit = Hit("soundcloud", n, artist, clean_title(title, artist), (t.get("duration") or 0) / 1000)
        hit.album, hit.url = meta.get("album_title") or meta.get("release_title") or "", t.get("permalink_url") or ""
        art = t.get("artwork_url") or (t.get("user") or {}).get("avatar_url") or ""
        hit.cover = art.replace("-large.", "-t300x300.")
        hit.year = (t.get("release_date") or t.get("display_date") or t.get("created_at") or "")[:4]
        out.append(hit)
    return out
