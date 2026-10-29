"""Discogs: whether it lists a release, from its database search (api.discogs.com, read only). The search needs
a personal token (discogs.com, Settings, Developers; kept in the vault as TOKEN); Discogs allows 60 requests a
minute with one, so requests are PAUSE apart. Nothing is ever submitted: Discogs' API cannot add releases."""

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from echolot import __version__

API = "https://api.discogs.com"
TOKEN = "discogs.token"  # vault
USER_AGENT = f"Echolot/{__version__} +https://github.com/Zweisteinium/echolot"
PAUSE = 1.1  # seconds between two requests
TIMEOUT = 20
_LAST = [0.0]
_LOCK = threading.Lock()


class DiscogsError(RuntimeError):
    """Discogs did not answer as expected (message for the log)."""


class Refused(DiscogsError):
    """Discogs refused the token (HTTP 401, 403) or asked to wait longer than a minute (429)."""


@dataclass(frozen=True)
class Release:
    id: int
    artists: str  # as Discogs writes them: "Funk Tribu, ANDATA" ("Various" for a compilation)
    title: str
    formats: tuple[str, ...]  # ("File", "MP3", "EP")
    label: str
    catno: str
    year: str

    @property
    def url(self) -> str:
        return f"https://www.discogs.com/release/{self.id}"

    @property
    def digital(self) -> bool:
        return "File" in self.formats


def _get(token: str, path: str, **query: Any) -> dict[str, Any]:
    url = f"{API}{path}?{urllib.parse.urlencode(query)}" if query else API + path
    headers = {"Authorization": f"Discogs token={token}", "User-Agent": USER_AGENT}
    for attempt in range(2):
        with _LOCK:
            wait = _LAST[0] + PAUSE - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _LAST[0] = time.monotonic()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise Refused("Discogs refused the token") from e
            if e.code == 429 and not attempt:
                time.sleep(60)  # (its limit counts the last minute)
                continue
            if e.code == 429:
                raise Refused("Discogs: too many requests") from e
            raise DiscogsError(f"Discogs: HTTP {e.code}") from e
        except (OSError, ValueError) as e:
            raise DiscogsError(f"Discogs not reachable: {e}") from e
    raise DiscogsError("Discogs: no answer")


def identity(token: str) -> str:
    """The name of the account the token belongs to (Refused for a wrong one)."""
    return str(_get(token, "/oauth/identity").get("username") or "")


def search(token: str, artist: str, title: str) -> list[Release]:
    """The releases Discogs finds for an artist and a title (its full-text search: names, titles, tracks)."""
    found = _get(token, "/database/search", q=f"{artist} {title}", type="release", per_page=50)
    return [r for x in found.get("results") or [] if (r := _release(x))]


def _release(x: dict[str, Any]) -> Release | None:
    if not isinstance(x, dict) or not x.get("id"):
        return None
    artists, _, title = str(x.get("title") or "").partition(" - ")
    return Release(
        id=int(x["id"]),
        artists=artists if title else "",
        title=title or artists,
        formats=tuple(str(f) for f in x.get("format") or []),
        label=str((x.get("label") or [""])[0]),
        catno=str(x.get("catno") or ""),
        year=str(x.get("year") or ""),
    )


def names(text: str) -> list[str]:
    """The artists in Discogs' way of writing them, without its numbers for same-named ones (Odymel (2)) and
    its marks for a name as written on the release (Odymel*)."""
    parts = re.split(r",\s+|\s+(?:&|feat\.?|ft\.?|vs\.?|x)\s+", text, flags=re.I)
    return [n for p in parts if (n := re.sub(r"\s*\(\d+\)$", "", p.strip().rstrip("*")).strip())]
