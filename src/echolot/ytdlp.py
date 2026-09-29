"""yt-dlp for SoundCloud (the lists and their downloads) and for the search fallback (YouTube and
SoundCloud search), from the home IP (YouTube refuses VPN exits). The SoundCloud token reaches yt-dlp
through a private netrc file, never the command line (ps would show it).
"""

import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

from echolot.rules import clean_name, title_key

log = logging.getLogger(__name__)

# originals first (kept lossless), then the best streams
SC_FORMATS = (
    "download/http_aac_256/hls_aac_256/hls_aac_160k/http_mp3_1_0/hls_mp3_1_0/bestaudio/best"
)


class YtDlp:
    def __init__(self, private: Path, sc_token: str | None = None) -> None:
        """private: a directory only Echolot reads (its data directory), for the netrc file."""
        self.netrc: Path | None = None
        if sc_token:
            private.mkdir(parents=True, exist_ok=True)
            self.netrc = private / "netrc"
            fd = os.open(self.netrc, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(f"machine soundcloud login oauth password {sc_token}\n")

    def run(
        self, args: list[str], stop: threading.Event | None = None, timeout: float = 3600
    ) -> subprocess.CompletedProcess:
        cmd = ["yt-dlp", "--no-warnings", "--js-runtimes", "node", "--sleep-requests", "1.5",
               "--extractor-retries", "8", "--retry-sleep", "extractor:exp=10:300",
               "--retry-sleep", "http:exp=10:300"]  # fmt: skip
        if self.netrc:
            cmd += ["--netrc", "--netrc-location", str(self.netrc)]
        p = subprocess.Popen(cmd + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = (
                p.communicate(timeout=timeout) if stop is None else _communicate(p, stop, timeout)
            )
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
            err += "\n(stopped: took too long)"
        return subprocess.CompletedProcess(p.args, p.returncode, out, err)

    # ------------------------------------------------------------ SoundCloud lists

    def listing(
        self, url: str, stop: threading.Event
    ) -> tuple[list[tuple[str, str]] | None, dict[str, Any]]:
        """Ordered (id, url) of the tracks of a SoundCloud list, and the list's info; None if listing
        failed. Liked sets are left out (likes mean liked songs; add a set as its own list). SoundCloud
        answers 429 or an empty page after bursts: back off and retry."""
        for attempt in range(6):
            r = self.run(["--flat-playlist", "-J", url], stop, 600)
            if r.returncode == 0:
                info = json.loads(r.stdout)
                entries = [e for e in info.get("entries") or [] if e and e.get("id")]
                if entries:
                    tracks = [
                        (str(e["id"]), e.get("url") or "")
                        for e in entries
                        if "/sets/" not in (e.get("url") or "")
                    ]
                    return tracks, info
            elif "429" not in r.stderr:
                log.warning("soundcloud: listing %s failed: %s", url, r.stderr.strip()[-300:])
                return None, {}
            log.info("soundcloud: %s rate limited or empty, waiting 2 min (%d/6)", url, attempt + 1)
            if stop.wait(120):
                break
        return None, {}

    def download(
        self, tracks: list[tuple[str, str]], folder: Path, stop: threading.Event
    ) -> list[dict[str, str]]:
        """Download SoundCloud tracks ((id, url)) into folder: [{id, uploader, artist, title, duration,
        path}] of the finished ones, each with its thumbnail as <id>.jpg. Tracks that got a 429 are tried
        again after a pause."""
        folder.mkdir(parents=True, exist_ok=True)
        done_file, batch = folder / "done.tsv", folder / "batch.txt"
        done: dict[str, dict[str, str]] = {}
        for attempt in range(4):
            todo = [url for tid, url in tracks if tid not in done and url]
            if not todo:
                break
            batch.write_text("\n".join(todo) + "\n")
            done_file.unlink(missing_ok=True)
            r = self.run(["-f", SC_FORMATS, "--ignore-errors", "--no-overwrites", "--write-thumbnail",
                          "--convert-thumbnails", "jpg", "--embed-metadata", "--no-embed-thumbnail",
                          "-o", str(folder / "%(id)s.%(ext)s"), "-o", "thumbnail:" + str(folder / "%(id)s"),
                          "--print-to-file", "after_move:%(id)s\t%(uploader)s\t%(artist)s\t%(title)s\t"
                          "%(duration)s\t%(filepath)s", str(done_file), "-a", str(batch)],
                         stop)  # fmt: skip
            lines = (
                done_file.read_text(encoding="utf-8", errors="replace").splitlines()
                if done_file.exists()
                else []
            )
            for line in lines:
                parts = line.split("\t")
                if len(parts) >= 6 and Path(parts[5]).is_file():
                    keys = ("id", "uploader", "artist", "title", "duration", "path")
                    done[parts[0]] = dict(zip(keys, parts[:6], strict=True))
            if "429" not in r.stderr or stop.is_set():
                break
            log.info(
                "soundcloud: rate limited while downloading, waiting 3 min (%d/4)", attempt + 1
            )
            if stop.wait(180):
                break
        batch.unlink(missing_ok=True)
        done_file.unlink(missing_ok=True)
        return list(done.values())

    def meta(self, url: str, stop: threading.Event) -> dict[str, Any] | None:
        """A track's metadata without downloading; formats [] = SoundCloud hands it out to nobody."""
        r = self.run(["-J", "--skip-download", "--ignore-no-formats-error", url], stop, 300)
        try:
            return json.loads(r.stdout)
        except ValueError:
            return None

    # ------------------------------------------------------------ search fallback

    def search(self, query: str, site: str, stop: threading.Event) -> list[dict[str, Any]]:
        """Up to 5 results: [{url, title, uploader, duration}], site youtube or soundcloud."""
        prefix = "ytsearch5:" if site == "youtube" else "scsearch5:"
        r = self.run(["--flat-playlist", "-j", prefix + query], stop, 300)
        out = []
        for line in r.stdout.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if url := e.get("url") or e.get("webpage_url"):
                out.append({"url": url, "title": e.get("title") or "", "duration": e.get("duration") or 0,
                            "uploader": e.get("uploader") or e.get("channel") or ""})  # fmt: skip
        return out

    def fetch(self, url: str, dest: Path, stop: threading.Event) -> Path | None:
        """Download a found video's or track's best audio as dest.<ext>."""
        r = self.run(["-f", "bestaudio/best", "-x", "--audio-quality", "0", "--embed-metadata",
                      "--no-playlist", "-o", f"{dest}.%(ext)s", "--print", "after_move:filepath", url],
                     stop, 900)  # fmt: skip
        out = [line for line in r.stdout.splitlines() if line.strip()]
        if r.returncode == 0 and out and os.path.isfile(out[-1]):
            return Path(out[-1])
        log.info("fallback: download of %s failed: %s", url, r.stderr.strip()[-200:])
        return None


def _communicate(p: subprocess.Popen, stop: threading.Event, timeout: float) -> tuple[str, str]:
    """communicate() that gives up when `stop` is set."""
    result: list[tuple[str, str]] = []
    t = threading.Thread(target=lambda: result.append(p.communicate()), daemon=True)
    t.start()
    waited = 0.0
    while t.is_alive():
        t.join(2)
        waited += 2
        if stop.is_set() or waited > timeout:
            p.terminate()
            t.join(30)
            if t.is_alive():
                p.kill()
                t.join()
            break
    return result[0] if result else ("", "(stopped)")


def artist_title(uploader: str, artist: str, title: str) -> tuple[str, str]:
    """A SoundCloud track's artist and title: 'Artist - Title' from the title, else the artist field,
    else the uploader; the first artist only."""
    m = re.match(r"^(?P<a>[^-]{1,60}?)\s+-\s+(?P<t>.+)$", title or "")
    if m and title_key(m.group("t")):  # "Song - Original Mix" is a title, not "Artist - Title"
        a, t = m.group("a"), m.group("t")
    else:
        a = artist if artist and artist != "NA" else uploader
        t = title
    a = re.split(r"\s*[,，;/]\s*|\s+[xX&]\s+|\s+feat\.?\s+|\s+ft\.?\s+", a or "")[0] or (
        uploader or "Unknown"
    )
    return clean_name(a), clean_name(t)
