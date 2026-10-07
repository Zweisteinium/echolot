"""Song search in catalogues that need no account: Deezer (api.deezer.com, about 50 requests per 5 s) and
Apple Music (the iTunes Search API, about 20 per minute: callers cache). Each returns discover.Hit, best first;
CatalogError when it can't answer."""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from echolot.library.discover import Hit

TIMEOUT = 6  # s: a search waits no longer for one catalogue
UA = {"User-Agent": "Echolot (self-hosted music library)"}


class CatalogError(RuntimeError):
    """A catalogue did not answer (unreachable, too many requests, an error)."""


def _get(url: str) -> Any:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise CatalogError(f"HTTP {e.code}") from e
    except (OSError, TimeoutError, ValueError) as e:
        raise CatalogError(f"not reachable: {e}") from e


def deezer(query: str, limit: int = 20) -> list[Hit]:
    d = _get(f"https://api.deezer.com/search/track?limit={limit}&q={urllib.parse.quote(query)}")
    if "error" in d:  # (a quota or a bad query: answered with HTTP 200)
        raise CatalogError(str((d["error"] or {}).get("message") or d["error"]))
    out = []
    for n, t in enumerate(d.get("data") or []):
        album, artist = t.get("album") or {}, (t.get("artist") or {}).get("name") or ""
        hit = Hit("deezer", n, artist, t.get("title") or "", float(t.get("duration") or 0), album.get("title") or "")
        hit.url, hit.preview, hit.cover = t.get("link") or "", t.get("preview") or "", album.get("cover_medium") or ""
        out.append(hit)
    return out


def apple(query: str, country: str = "US", limit: int = 20) -> list[Hit]:
    q = urllib.parse.urlencode({"term": query, "media": "music", "entity": "song", "limit": limit, "country": country})
    out = []
    for n, t in enumerate(_get(f"https://itunes.apple.com/search?{q}").get("results") or []):
        seconds, album = (t.get("trackTimeMillis") or 0) / 1000, t.get("collectionName") or ""
        hit = Hit("apple", n, t.get("artistName") or "", t.get("trackName") or "", seconds, album)
        hit.url, hit.preview = (t.get("trackViewUrl") or "").split("?uo=")[0], t.get("previewUrl") or ""
        hit.cover = (t.get("artworkUrl100") or "").replace("100x100bb", "250x250bb")
        hit.year = (t.get("releaseDate") or "")[:4]
        out.append(hit)
    return out
