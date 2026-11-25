"""LRCLIB (lrclib.net): a free lyrics database, synced (LRC) and plain, kept by its users. No account; asked
as Echolot. Several entries may share a song, each with its own length: `get` answers the one of that length
(within 2 s), `search` all of a title. Entries are anyone's: library/lyrics checks each before it is used."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from echolot import __version__

API = "https://lrclib.net/api"
UA = {"User-Agent": f"Echolot/{__version__} (self-hosted music library)"}
TIMEOUT = 20  # s: an entry LRCLIB has not cached yet may be looked up elsewhere first
SPACING = 1.0  # s at least between two requests (LRCLIB is run by volunteers)
_last = {"at": 0.0}


class LrclibError(RuntimeError):
    """LRCLIB not reachable or failing (not: no entry)."""


def _get(path: str, params: dict[str, Any]) -> Any:
    wait = _last["at"] + SPACING - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    url = f"{API}/{path}?{urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, '')})}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise LrclibError(f"LRCLIB: HTTP {e.code}") from e
    except (OSError, ValueError) as e:
        raise LrclibError(f"LRCLIB not reachable: {e}") from e
    finally:
        _last["at"] = time.monotonic()


def get(artist: str, title: str, album: str | None, length: float) -> dict[str, Any] | None:
    """The entry of this artist, title (and album) of this length within 2 s; None if LRCLIB has none."""
    params = {"artist_name": artist, "track_name": title, "album_name": album, "duration": round(length)}
    found = _get("get", params)
    return found if isinstance(found, dict) and found.get("id") else None


def search(artist: str, title: str) -> list[dict[str, Any]]:
    """The entries of a title by an artist (any length, any album), as LRCLIB ranks them."""
    found = _get("search", {"artist_name": artist, "track_name": title})
    return [e for e in found or [] if isinstance(e, dict) and e.get("id")]
