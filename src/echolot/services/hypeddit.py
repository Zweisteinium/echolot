"""A free download behind a Hypeddit gate, without the clicking. A gate page names its file and the steps it
asks for (follow on SoundCloud, follow on Instagram, ...), but Hypeddit neither performs nor checks those steps:
the download request returns the file regardless. A gate with an email step wants an address (verified once, then
sent with the request, so the artist gets it); the verify has a captcha only after five failed tries, which a
valid address never reaches. Other gate sites (ToneDen, Artist Union, ...) do enforce their steps: not here.

    gate = read(url)             # what the gate is
    file = unlock(gate, email)   # the file it gives: a short-lived link, name, format, size
    path = fetch(file, folder)   # the file itself
"""

import html
import http.client
import http.cookiejar
import json
import random
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from echolot.services.bandcamp import USER_AGENT

BASE = "https://hypeddit.com"
HOST = "hypeddit.com"
MAX_BYTES = 1 << 30  # a 24-bit WAV of a long mix is some 300 MB
_FILE_CHUNK = 1 << 20
_NET = (OSError, http.client.HTTPException)  # URLError, timeouts, resets, a cut-off body


class GateError(RuntimeError):
    """A gate that gives no file (message for the log)."""


@dataclass
class Gate:
    url: str
    title: str
    steps: list[str]  # the step codes it asks for, without the download: sc, email, ig, sp, ...
    file_id: str
    form: dict[str, str]
    csrf: str
    opener: urllib.request.OpenerDirector = field(repr=False)

    @property
    def email(self) -> bool:
        return "email" in self.steps


@dataclass(frozen=True)
class File:
    url: str  # where the file is, valid only for a while
    name: str
    ext: str  # wav, mp3, aif, zip, ...
    size: int
    kind: str  # the content type Hypeddit reports


def _opener() -> urllib.request.OpenerDirector:
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    op.addheaders = [("User-Agent", USER_AGENT), ("Accept-Language", "en")]
    return op


def read(url: str) -> Gate:
    """The gate at `url` (a hypeddit.com page). GateError if it is none (gone, or a link gate to a store)."""
    op = _opener()
    try:
        page = op.open(url, timeout=30).read().decode("utf-8", "replace")
    except (*_NET, ValueError) as e:
        raise GateError(f"page: {e}") from e

    def value(name: str) -> str:
        m = re.search(rf"<input[^>]*(?:id|name)=\"{re.escape(name)}\"[^>]*value=['\"]([^'\"]*)", page)
        return html.unescape(m.group(1)) if m else ""

    csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', page)
    file_id = value("current_download_file_listner")
    if not (csrf and file_id):
        raise GateError("not a download gate")
    ext = re.search(r'"externID":"([^"]*)"', page)
    title = re.search(r"<title>\s*(.*?)\s*(?:\|\s*(?:Free Download on\s*)?Hypeddit)?\s*</title>", page, re.S | re.I)
    form = {k: value(k) for k in ("fan_gate_id", "wrndk", "nwSteps", "is_skippable", "duration", "gvf")}
    form["external_id"] = ext.group(1) if ext else ""
    steps = [s for s in value("steps_select").split(",") if s and s != "dw"]
    return Gate(url, html.unescape(title.group(1)) if title else "", steps, file_id, form, csrf.group(1), op)


def _post(gate: Gate, path: str, data: dict[str, str]) -> dict:
    req = urllib.request.Request(
        BASE + path,
        urllib.parse.urlencode(data).encode(),
        headers={
            "X-CSRF-TOKEN": gate.csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": gate.url,
            "Origin": BASE,
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
    )
    name = path.rsplit("/", 1)[-1]
    try:
        body = gate.opener.open(req, timeout=30).read()
    except _NET as e:
        raise GateError(f"{name}: {e}") from e
    try:
        res = json.loads(body)
    except ValueError:
        res = None
    if not isinstance(res, dict):
        raise GateError(f"{name}: no answer")
    return res


def unlock(gate: Gate, email: str = "") -> File:
    """The file the gate gives; a gate with an email step needs `email`. GateError if it gives none (a store
    gate dressed as a download, or an empty upload)."""
    if gate.email:
        if not email:
            raise GateError("wants an email")
        # registers the address; the download below is the real test, so its answer is not parsed hard
        res = _post(
            gate,
            "/verifyEmailAddress",
            {
                "validateEmailAddress": email,
                "fan_gate_id": gate.form["fan_gate_id"],
                "email_name": "",
                "adcode": "",
                "hypesource": "",
            },
        )
        if str(res.get("attempts", "")).lower() == "over" or "captcha" in str(res).lower():
            raise GateError("email step wants a captcha")
    duration = int(d) if (d := gate.form["duration"]).isdigit() and int(d) else 180_000
    res = _post(
        gate,
        "/gate/download/ul",
        {
            "file": urllib.parse.quote(gate.file_id, safe=""),
            "download_visit": "true",
            "profile_downloads": "true",
            "time": str(random.randrange(duration)),
            "sc_comment_text": "",
            "yt_comment_text": "",
            "page": "nonsingle",
            "is_skippable": gate.form["is_skippable"],
            "steps": gate.form["nwSteps"],
            "email": email if gate.email else "",
            "download_action": "DOWNLOAD",
            "wrndk": gate.form["wrndk"],
            "is_mobile": "0",
            "external_id": gate.form["external_id"],
            "hypesource": "",
            "adcode": "",
            "lifetime_fan_spotify": "",
            "lifetime_fan_deezer": "",
            "lifetime_fan_apple": "",
            "gvf": gate.form["gvf"] or "0",
        },
    )
    if str(res.get("download_status")) != "True" or not res.get("URL"):
        raise GateError("no file (a store gate, or an empty upload)")
    size = int(n) if (n := str(res.get("size"))).isdigit() else 0  # (0: not checked)
    name, ext = str(res.get("name") or gate.title), str(res.get("ext") or "").lower()
    return File(str(res["URL"]), name, ext, size, str(res.get("type") or ""))


def fetch(file: File, folder: Path, max_bytes: int = MAX_BYTES) -> Path:
    """Download `file` into `folder` under its own name; its path. GateError if it does not come whole."""
    if file.size > max_bytes:
        raise GateError(f"too big: {file.size / 1e6:.0f} MB")
    if not file.url.lower().startswith("https://"):
        raise GateError("file: not an https link")
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{file.name}.{file.ext}" if file.ext and not file.name.lower().endswith("." + file.ext) else file.name
    dest = folder / (re.sub(r"[^\w\-. ()&',!]+", "_", stem)[-150:].lstrip(". ") or "download")
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(file.url, headers={"User-Agent": USER_AGENT})
    got = 0
    try:
        with urllib.request.urlopen(req, timeout=60) as r, part.open("wb") as out:
            while chunk := r.read(_FILE_CHUNK):
                got += len(chunk)
                if got > max_bytes:
                    raise GateError(f"too big: over {max_bytes / 1e6:.0f} MB")
                out.write(chunk)
    except _NET as e:
        part.unlink(missing_ok=True)
        raise GateError(f"file: {e}") from e
    if file.size and got != file.size:
        part.unlink(missing_ok=True)
        raise GateError(f"file: {got} of {file.size} bytes")
    part.replace(dest)
    return dest
