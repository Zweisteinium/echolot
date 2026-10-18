"""Bandcamp: a track's offer (its price, whether it is a free download) from its page, and a free track's FLAC.
Only pages a song links to are read (a link on its SoundCloud page): Bandcamp's robots.txt keeps bots off
its search. Pages are read with curl, as Echolot (USER_AGENT), a pause between requests; Bandcamp shows
Python's own client a bot check (Challenged, as is a refusal: HTTP 403, 429), and Echolot does not get around
one: it stops. Only https addresses on bandcamp.com and its file host bcbits.com are asked. Bandcamp streams MP3
128 kbps; a download is lossless (FLAC), for a price or free."""

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
HOSTS = re.compile(r"https://(?:[\w-]+\.)*(?:bandcamp\.com|bcbits\.com)(?:[/?]|$)")  # what is asked
_LAST = [0.0]
_LOCK = threading.Lock()


class BandcampError(RuntimeError):
    """A page or download that did not come (message for the log)."""


class Challenged(BandcampError):
    """Bandcamp answered with its bot check instead of the page, or refused (HTTP 403, 429)."""


class Gone(BandcampError):
    """The page is not there (HTTP 404, 410): the link leads to no offer."""


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
    found = (m.group().split("?")[0].replace("http://", "https://", 1) for t in texts if t for m in LINK.finditer(t))
    return list(dict.fromkeys(found))


def _safe(url: str) -> str:
    """The address, if it is one Echolot asks (https on bandcamp.com or bcbits.com)."""
    if not isinstance(url, str) or not HOSTS.match(url):
        raise BandcampError(f"not a Bandcamp address: {str(url)[:80]}")
    return url


def _get(url: str, dest: Path | None = None, timeout: int = 60) -> bytes:
    """One request through curl (https only, redirects too), after the pause; the body (or into `dest`). Gone
    for 404/410, Challenged for 403/429, BandcampError for anything else that is not a 2xx answer."""
    _safe(url)
    with _LOCK:
        wait = _LAST[0] + PAUSE - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _LAST[0] = time.monotonic()
    out = ["-o", str(dest)] if dest else []
    cmd = ["curl", "-sS", "-L", "--proto", "=https", "--proto-redir", "=https", "--max-time", str(timeout)]
    cmd += ["-A", USER_AGENT, "-w", "%{stderr}%{http_code}", *out, "--", url]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BandcampError(f"Bandcamp: {e}") from e
    err = r.stderr.decode(errors="replace").strip()
    code = int(err[-3:]) if err[-3:].isdigit() else 0
    if code in (404, 410):
        raise Gone(f"Bandcamp: HTTP {code}")
    if code in (403, 429):
        raise Challenged(f"Bandcamp refused (HTTP {code})")
    if r.returncode or not 200 <= code < 300:
        raise BandcampError(f"Bandcamp: {err[:-3].strip()[:200] or 'HTTP ' + str(code)}")
    return b"" if dest else r.stdout


def _page(url: str) -> str:
    text = _get(url).decode("utf-8", "replace")
    if "<title>Client Challenge</title>" in text or "data-" not in text:
        raise Challenged("Bandcamp asked for its bot check")
    return text


def _data(page: str, name: str, key: str = "") -> dict:
    """The page's JSON in its attribute data-<name> (a page can have several: the first with `key`)."""
    for m in re.finditer(rf'data-{name}="([^"]*)"', page):
        try:
            d = json.loads(html.unescape(m.group(1)))
        except ValueError:
            continue
        if isinstance(d, dict) and (not key or key in d):
            return d
    return {}


def _currency(page: str) -> str:
    sellers = (_data(page, "blob", "item_sellers").get("item_sellers") or {}).values()
    return next((s.get("currency") or "" for s in sellers if isinstance(s, dict)), "")


def offer(url: str, title: str = "", seconds: float = 0) -> Offer | None:
    """The offer of the track at `url`; an album page: of its track named `title` (or as long as `seconds`),
    from that track's own page. None when the page has no such track; BandcampError for a page that is not
    as expected."""
    try:
        return _offer(url, title, seconds)
    except (ValueError, TypeError, AttributeError, KeyError, IndexError) as e:
        raise BandcampError(f"Bandcamp: unreadable page ({type(e).__name__})") from e


def _offer(url: str, title: str, seconds: float) -> Offer | None:
    page = _page(url)
    t = _data(page, "tralbum")
    if t.get("item_type") == "album":
        track = _track_of(t.get("trackinfo") or [], title, seconds)
        if not track or not track.get("title_link"):
            return None
        return _offer(_safe(urllib.parse.urljoin(url, track["title_link"])), "", 0)
    if t.get("item_type") != "track":
        return None
    info, cur = (t.get("trackinfo") or [{}])[0], t.get("current") or {}
    price = cur.get("minimum_price")
    free_page = t.get("freeDownloadPage") or ""
    return Offer(
        url=t["url"] if isinstance(t.get("url"), str) and HOSTS.match(t["url"]) else url,
        artist=t.get("artist") or "",
        title=cur.get("title") or info.get("title") or "",
        seconds=float(info.get("duration") or 0),
        price=float(price) if price is not None and info.get("is_downloadable") else None,
        currency=_currency(page),
        free=bool(free_page),
        free_page=free_page if isinstance(free_page, str) else "",
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
    try:
        return _free_flac(o, folder)
    except (ValueError, TypeError, AttributeError, KeyError, IndexError) as e:
        raise BandcampError(f"Bandcamp: unreadable download page ({type(e).__name__})") from e


def _free_flac(o: Offer, folder: Path) -> Path:
    blob = _data(_page(o.free_page), "blob", "download_items")
    items = blob.get("download_items") or []
    flac = ((items[0].get("downloads") or {}).get("flac") or {}).get("url") if items else None
    if not flac:
        raise BandcampError("no FLAC to download")
    parts = urllib.parse.urlsplit(flac)
    query = f"{parts.query}&.vrs=1" if parts.query else ".vrs=1"
    stat_url = urllib.parse.urlunsplit(
        parts._replace(path=parts.path.replace("/download/", "/statdownload/", 1), query=query)
    )
    stat = _get(stat_url).decode("utf-8", "replace")
    m = re.search(r"statResult\s*\(\s*(\{.*\})\s*\)", stat, re.S)
    try:
        answer = json.loads(m.group(1) if m else stat)
    except ValueError:
        answer = {}
    answer = answer if isinstance(answer, dict) else {}
    url = answer.get("download_url") or answer.get("retry_url")  # (the page's own has expired: HTTP 403)
    if not url:
        raise BandcampError(f"no download link ({answer.get('errortype') or 'no answer'})")
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / (re.sub(r"[^\w\-. ()&',!]+", "_", f"{o.artist} - {o.title}")[:150] + ".flac")
    _get(_safe(url), dest=dest, timeout=600)
    with dest.open("rb") as f:
        flac_file = f.read(4) == b"fLaC"
    if not flac_file:
        dest.unlink(missing_ok=True)
        raise BandcampError("the download is not a FLAC")
    return dest
