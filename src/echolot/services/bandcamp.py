"""Bandcamp: a track's offer (its price, whether it is a free download) from its page, and a free track's FLAC.
Only pages a song links to are read (a link on its SoundCloud page): Bandcamp's robots.txt keeps bots off
its search. Pages are read with curl, as Echolot (USER_AGENT), a pause between requests; Bandcamp shows
Python's own client a bot check (Challenged), and Echolot does not get around one: it stops for that run.
Bandcamp streams MP3 128 kbps; a download is lossless (FLAC), for a price or free."""

import html
import json
import re
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from echolot import __version__

USER_AGENT = f"Echolot/{__version__} (+https://github.com/Zweisteinium/echolot)"
PAUSE = 2.0  # seconds between two requests
LINK = re.compile(r"https?://[\w-]+\.bandcamp\.com/(?:track|album)/[\w-]+")
_LAST = [0.0]
_LOCK = threading.Lock()


class BandcampError(RuntimeError):
    """A page or download that did not come (message for the log)."""


class Challenged(BandcampError):
    """Bandcamp answered with its bot check instead of the page."""


@dataclass(frozen=True)
class Offer:
    url: str  # the track's page
    artist: str
    title: str
    seconds: float
    price: float | None  # the least it sells for (0: name your price from nothing); None: not for sale
    currency: str
    free: bool  # a free download right away (no email asked)
    free_page: str = ""  # where the free download is


def links(*texts: str | None) -> list[str]:
    """The Bandcamp track and album pages named in texts (a SoundCloud track's buy link, its description)."""
    found = (m.group().split("?")[0] for t in texts if t for m in LINK.finditer(t))
    return list(dict.fromkeys(found))


def _get(url: str, binary: bool = False, dest: Path | None = None, timeout: int = 60) -> bytes:
    """One request through curl, after the pause; the body (or into `dest`)."""
    with _LOCK:
        wait = _LAST[0] + PAUSE - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _LAST[0] = time.monotonic()
    out = ["-o", str(dest)] if dest else []
    cmd = ["curl", "-sS", "-L", "--fail", "--max-time", str(timeout), "-A", USER_AGENT, *out, url]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BandcampError(f"Bandcamp: {e}") from e
    if r.returncode:
        raise BandcampError(f"Bandcamp: {r.stderr.decode(errors='replace').strip()[:200] or r.returncode}")
    return b"" if dest else r.stdout


def _page(url: str) -> str:
    text = _get(url).decode("utf-8", "replace")
    if "<title>Client Challenge</title>" in text or "data-" not in text:
        raise Challenged("Bandcamp asked for its bot check")
    return text


def _data(page: str, name: str, key: str = "") -> dict:
    """The page's JSON in its attribute data-<name> (a page can have several: the first with `key`)."""
    for m in re.finditer(rf'data-{name}="([^"]*)"', page):
        d = json.loads(html.unescape(m.group(1)))
        if not key or key in d:
            return d
    return {}


def _currency(page: str) -> str:
    sellers = (_data(page, "blob", "item_sellers").get("item_sellers") or {}).values()
    return next((s.get("currency") or "" for s in sellers if isinstance(s, dict)), "")


def offer(url: str, title: str = "", seconds: float = 0) -> Offer | None:
    """The offer of the track at `url`; an album page: of its track named `title` (or as long as `seconds`),
    from that track's own page. None when the page has no such track."""
    page = _page(url)
    t = _data(page, "tralbum")
    if t.get("item_type") == "album":
        track = _track_of(t.get("trackinfo") or [], title, seconds)
        if not track or not track.get("title_link"):
            return None
        return offer(urllib.parse.urljoin(url, track["title_link"]))
    if t.get("item_type") != "track":
        return None
    info, cur = (t.get("trackinfo") or [{}])[0], t.get("current") or {}
    price = cur.get("minimum_price")
    return Offer(
        url=t.get("url") or url,
        artist=t.get("artist") or "",
        title=cur.get("title") or info.get("title") or "",
        seconds=float(info.get("duration") or 0),
        price=float(price) if price is not None and info.get("is_downloadable") else None,
        currency=_currency(page),
        free=bool(t.get("freeDownloadPage")),
        free_page=t.get("freeDownloadPage") or "",
    )


def _track_of(tracks: list[dict], title: str, seconds: float) -> dict | None:
    """The album track that is the song, the surest first: the one titled as the song, the one whose title
    holds the song's ("Artist - First Light (Remix)"), the one whose title the song's holds ("First Light"
    for "First Light [KTA022]"); each only when a single one is (and as long, ±2 s, with `seconds`), else
    the one as long when that is a single one."""

    def words(text: str) -> str:
        return re.sub(r"\W+", " ", text.lower()).strip()

    def long(t: dict) -> bool:
        return not seconds or abs(float(t.get("duration") or 0) - seconds) <= 2

    want, titled = words(title), [(words(t.get("title") or ""), t) for t in tracks]
    if want:
        for fits in (lambda n: n == want, lambda n: want in n, lambda n: n in want):
            named = [t for n, t in titled if n and fits(n) and long(t)]
            if len(named) == 1:
                return named[0]
    same = [t for t in tracks if seconds and long(t)]
    return same[0] if len(same) == 1 else None


def free_flac(o: Offer, folder: Path) -> Path:
    """Download a free track's FLAC into `folder`; its path. The download page's links expire within
    minutes, so a fresh one is asked for (statdownload) first."""
    if not o.free_page:
        raise BandcampError("not a free download")
    blob = _data(_page(o.free_page), "blob", "download_items")
    items = blob.get("download_items") or []
    flac = ((items[0].get("downloads") or {}).get("flac") or {}).get("url") if items else None
    if not flac:
        raise BandcampError("no FLAC to download")
    stat = _get(flac.replace("/download/", "/statdownload/", 1) + "&.vrs=1").decode("utf-8", "replace")
    m = re.search(r"statResult\(\s*(\{.*\})\s*\)", stat, re.S)
    answer = json.loads(m.group(1)) if m else {}
    url = answer.get("download_url") or answer.get("retry_url")
    if not url:
        raise BandcampError(f"no download link ({answer.get('errortype') or 'no answer'})")
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / (re.sub(r"[^\w\-. ()&',!]+", "_", f"{o.artist} - {o.title}")[:150] + ".flac")
    _get(url, dest=dest, timeout=600)
    with dest.open("rb") as f:
        flac_file = f.read(4) == b"fLaC"
    if not flac_file:
        dest.unlink(missing_ok=True)
        raise BandcampError("the download is not a FLAC")
    return dest
