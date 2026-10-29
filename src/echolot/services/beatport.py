"""Beatport: a release's details (label, catalogue number, UPC, tracks) from its public pages, the release
search and the release page, which carry them as JSON (__NEXT_DATA__). Its robots.txt lets anyone read them;
Echolot asks as itself (USER_AGENT), a pause between requests, and stops at a refusal (HTTP 403, 429) or a
page without its data (Refused): it does not get around a bot check."""

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from echolot import __version__

SITE = "https://www.beatport.com"
USER_AGENT = f"Echolot/{__version__} (+https://github.com/Zweisteinium/echolot)"
PAUSE = 2.0  # seconds between two requests
TIMEOUT = 30
# what a purchase downloads as, for any release: the account's audio quality setting (the page has no list per
# release); best first, MP3 at 320 kbps
FORMATS = ("FLAC", "WAV", "AIFF", "MP3")
DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_LAST = [0.0]
_LOCK = threading.Lock()


class BeatportError(RuntimeError):
    """A page that did not come or is not as expected (message for the log)."""


class Refused(BeatportError):
    """Beatport refused (HTTP 403, 429) or answered with a page without its data (a bot check)."""


def _queries(url: str) -> list[dict[str, Any]]:
    """The page's data queries (each with queryKey and state.data)."""
    with _LOCK:
        wait = _LAST[0] + PAUSE - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _LAST[0] = time.monotonic()
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as r:
            text = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise Refused(f"Beatport refused (HTTP {e.code})") from e
        raise BeatportError(f"Beatport: HTTP {e.code}") from e
    except (OSError, ValueError) as e:
        raise BeatportError(f"Beatport not reachable: {e}") from e
    m = DATA.search(text)
    if not m:
        raise Refused("Beatport sent a page without its data")
    try:
        return json.loads(m.group(1))["props"]["pageProps"]["dehydratedState"]["queries"]
    except (ValueError, KeyError, TypeError) as e:
        raise BeatportError("Beatport: unreadable page") from e


def search(q: str) -> list[dict[str, Any]]:
    """The releases a search finds: release_id, release_name, artists, track_count, release_date, ..."""
    for query in _queries(f"{SITE}/search/releases?{urllib.parse.urlencode({'q': q})}"):
        if (query.get("queryKey") or [""])[0] == "search-releases":
            return [r for r in ((query.get("state") or {}).get("data") or {}).get("data") or [] if r]
    return []


def release(release_id: int) -> dict[str, Any]:
    """A release as the Discogs suggestion needs it: name, artists, label, catno, upc, date, cover, genre,
    url and its tracks (name, mix, artists, remixers, seconds, isrc, bpm)."""
    queries = _queries(f"{SITE}/release/-/{int(release_id)}")
    rel = next((q["state"]["data"] for q in queries if q.get("queryKey") == [f"release-{release_id}"]), None)
    tracks = next((q["state"]["data"] for q in queries if (q.get("queryKey") or [""])[0] == "tracks"), None)
    if not isinstance(rel, dict) or not isinstance(tracks, dict):
        raise BeatportError("Beatport: the release page lacks the release")
    try:
        return _release(rel, tracks.get("results") or [])
    except (KeyError, TypeError, AttributeError, ValueError) as e:
        raise BeatportError(f"Beatport: unreadable release ({type(e).__name__})") from e


def _names(items: list[dict[str, Any]] | None) -> list[str]:
    return [str(a["name"]) for a in items or [] if a.get("name")]


def _release(rel: dict[str, Any], tracks: list[dict[str, Any]]) -> dict[str, Any]:
    genres = [str((t.get("genre") or {}).get("name") or "") for t in tracks]
    return {
        "id": int(rel["id"]),
        "name": str(rel.get("name") or ""),
        "artists": _names(rel.get("artists")),
        "label": str((rel.get("label") or {}).get("name") or ""),
        "catno": str(rel.get("catalog_number") or ""),
        "upc": str(rel.get("upc") or ""),
        "date": str(rel.get("new_release_date") or rel.get("publish_date") or ""),
        "cover": str((rel.get("image") or {}).get("uri") or ""),
        "genre": max(set(genres), key=genres.count) if genres else "",
        "url": f"{SITE}/release/{rel.get('slug') or '-'}/{int(rel['id'])}",
        "tracks": [
            {
                "name": str(t.get("name") or ""),
                "mix": str(t.get("mix_name") or ""),
                "artists": _names(t.get("artists")),
                "remixers": _names(t.get("remixers")),
                "seconds": round((t.get("length_ms") or 0) / 1000),
                "isrc": str(t.get("isrc") or ""),
                "bpm": t.get("bpm"),
            }
            for t in tracks
        ],
    }
