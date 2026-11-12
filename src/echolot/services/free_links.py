"""Free downloads outside Bandcamp linked from a song's SoundCloud page (buy link, description): a gate
(Hypeddit, ToneDen, ...) wants a follow, repost or email, so it is listed (Hypeddit's Echolot gets on request:
services/hypeddit); a Dropbox file Echolot fetches itself (curl as Echolot, https only)."""

import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from echolot.services.bandcamp import USER_AGENT as USER_AGENT  # (re-exported)
from echolot.services.bandcamp import curl

GATES = {
    "hypeddit.com": "Hypeddit",
    "toneden.io": "ToneDen",
    "droploud.com": "DropLoud",
    "gaterush.me": "Gaterush",
    "theartistunion.com": "Artist Union",
}
URL = re.compile(r"https?://[^\s<>\"'()\[\]{}]+", re.I)
DROPBOX = re.compile(r"https://(?:www\.)?dropbox\.com/(?:scl/f[io]|s|sh)/", re.I)  # a shared file or folder
DROPBOX_FILE = re.compile(r"https://(?:www\.)?dropbox\.com/(?:scl/fi|s)/", re.I)  # a file (a folder: by hand)
FILES = {"wav", "flac", "aiff", "aif", "mp3", "m4a"}  # what a Dropbox link names that is fetched
MAX_BYTES = 1 << 30  # a 24-bit WAV of a long mix is some 300 MB


class LinkError(RuntimeError):
    """A file that did not come (message for the log)."""


@dataclass(frozen=True)
class Link:
    url: str
    site: str  # its proper name: Hypeddit, Dropbox, ...
    label: str  # what the uploader calls it on the buy button ("Free Download"), else ''
    fetch: bool  # a file Echolot downloads itself (else: by hand)


def links(purchase_url: str | None, purchase_title: str | None, description: str | None) -> list[Link]:
    """The free downloads named in a SoundCloud track's buy link and description, files Echolot fetches first."""
    found: dict[str, Link] = {}
    for text in (purchase_url, description):
        for m in URL.finditer(text or ""):
            url = m.group().rstrip(".,;:!?").replace("http://", "https://", 1)
            if link := _link(url, purchase_title if text == purchase_url else ""):
                found.setdefault(link.url, link)
    return sorted(found.values(), key=lambda link: not link.fetch)


def _link(url: str, label: str | None) -> Link | None:
    host = (urllib.parse.urlsplit(url).hostname or "").lower().removeprefix("www.")
    if host in GATES:
        return Link(url, GATES[host], label or "", False)
    if DROPBOX.match(url):
        ext = Path(urllib.parse.urlsplit(url).path).suffix.lower().lstrip(".")
        return Link(url, "Dropbox", label or "", bool(DROPBOX_FILE.match(url)) and ext in FILES)
    return None


def fetch(link: Link, folder: Path) -> Path:
    """Download a Dropbox file into `folder` under its own name; its path. LinkError if it does not come."""
    if not (link.fetch and DROPBOX_FILE.match(link.url)):
        raise LinkError(f"not a file to fetch: {link.url[:80]}")
    parts = urllib.parse.urlsplit(link.url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k != "dl"] + [("dl", "1")]
    url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    name = urllib.parse.unquote(Path(parts.path).name)
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / re.sub(r"[^\w\-. ()&',!]+", "_", name)[-150:]
    try:
        returncode, code, err, _ = curl(url, 900, dest, MAX_BYTES)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise LinkError(f"Dropbox: {e}") from e
    if returncode or code != 200 or not dest.is_file():
        dest.unlink(missing_ok=True)
        raise LinkError(f"Dropbox: {err[:200] or 'HTTP ' + str(code)}")
    return dest
