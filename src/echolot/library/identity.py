"""Is a download the recording the list asks for? Spotify gives every song's ISRC (the recording's id); Deezer
finds the release by ISRC (97 % of the library) and offers a 30 s preview (93 %). The preview's Chromaprint
fingerprint, slid along the download's, tells the recording from another: on 37 library songs 0.90 to 0.96 of
the bits agreed with the song's own file, at most 0.66 (median 0.55) with other songs. Another edit of the same
recording (a longer intro) agrees as well, so the length still decides the edit. A download tagged with the
song's ISRC needs no fingerprint. Without an ISRC, a Deezer entry or a preview nothing is known, and the name
rules decide alone."""

import json
import logging
import sqlite3
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from echolot.library import audio

log = logging.getLogger(__name__)

SAME, OTHER = 0.8, 0.7  # share of equal fingerprint bits: from SAME on the recording, up to OTHER another one
RETRY = 7 * 86400  # an ISRC Deezer did not know (or had no preview for) is asked again after a week
_TO_FINGERPRINT = ["-ac", "1", "-f", "chromaprint", "-fp_format", "raw", "-"]  # ffmpeg output options
_BITS = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


@dataclass(frozen=True)
class Evidence:
    verdict: str  # same, other, or '' (not known)
    detail: str = ""  # for the event and the review page


UNKNOWN = Evidence("")


def check(con: sqlite3.Connection, isrc: str, path: Path, any_length: bool = False) -> Evidence:
    """What the audio of `path` says about the recording `isrc`. any_length: a DJ-mix cut, whose transitions
    may blur the audio: only a match counts."""
    if not isrc:
        return UNKNOWN
    if (tag := audio.read_isrc(path)) and tag.replace("-", "").upper() == isrc.upper():
        return Evidence("same", "ISRC tag of the release")
    ref = reference(con, isrc)
    cand = fingerprint(path) if ref is not None else None
    if ref is None or cand is None or not len(cand):
        return UNKNOWN
    share = similarity(ref, cand)
    if share >= SAME:
        return Evidence("same", f"audio of the release ({share:.2f})")
    if share <= OTHER and not any_length:
        return Evidence("other", f"audio differs from the release ({share:.2f})")
    return Evidence("", f"audio unclear ({share:.2f})")


def reference(con: sqlite3.Connection, isrc: str) -> np.ndarray | None:
    """The fingerprint of the release's preview (cached in refs); None if Deezer has none."""
    row = con.execute("SELECT fingerprint, checked FROM refs WHERE isrc = ?", (isrc,)).fetchone()
    if row and (row["fingerprint"] or time.time() - row["checked"] < RETRY):
        return np.frombuffer(row["fingerprint"], dtype="<u4") if row["fingerprint"] else None
    try:
        track = _get(f"https://api.deezer.com/track/isrc:{isrc}")
        if (track.get("error") or {}).get("code", 800) != 800:  # 800: no such ISRC; others (quota): ask again
            raise OSError(track["error"].get("message"))
        preview = _get(track["preview"], raw=True) if track.get("preview") else b""
    except (OSError, ValueError) as e:  # not cached: asked again next time
        log.info("deezer %s: %s", isrc, e)
        return None
    fp = fingerprint(preview) if preview else None
    fp = fp if fp is not None and len(fp) else None
    entry = (isrc, track.get("id"), track.get("duration"), fp.tobytes() if fp is not None else None, int(time.time()))
    with con:  # the columns in their order: isrc, deezer_id, duration, fingerprint, checked
        con.execute("INSERT OR REPLACE INTO refs VALUES (?, ?, ?, ?, ?)", entry)
    return fp


def fingerprint(source: Path | bytes) -> np.ndarray | None:
    """Chromaprint of a file or of audio bytes (ffmpeg's muxer): one 32-bit value per 0.124 s; None if the
    audio could not be read."""
    data = source if isinstance(source, bytes) else None
    cmd = ["ffmpeg", "-v", "error", "-i", "pipe:0" if data else str(source), *_TO_FINGERPRINT]
    try:
        r = subprocess.run(cmd, input=data, capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return np.frombuffer(r.stdout, dtype="<u4") if r.returncode == 0 else None


def similarity(ref: np.ndarray, cand: np.ndarray) -> float:
    """Share of equal bits of `ref` at its best position inside `cand` (about 0.5: unrelated audio)."""
    n = len(ref)
    if not n or len(cand) < n:
        return 0.0
    windows = np.lib.stride_tricks.sliding_window_view(cand, n)
    diff = _BITS[np.bitwise_xor(windows, ref).view(np.uint8)].reshape(len(windows), -1).sum(axis=1)
    return 1 - float(diff.min()) / (32 * n)


def _get(url: str, raw: bool = False) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "Echolot (self-hosted music library)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read() if raw else json.load(r)


def alike(a: Path, b: Path) -> float:
    """How alike two recordings are: the least share of equal bits of three 25 s pieces of the shorter file
    (at a quarter, a half and three quarters) at their best place in the longer one. From about 0.85 the
    same recording (a remix that shares the chorus differs somewhere); 0.0 if one could not be read."""
    da, _ = audio.probe(a)
    db, _ = audio.probe(b)
    if not da or not db:
        return 0.0
    short, long_, d = (a, b, da) if da <= db else (b, a, db)
    whole = fingerprint(long_)
    if whole is None or not len(whole):
        return 0.0
    shares = []
    for at in (0.25, 0.5, 0.75):
        cmd = ["ffmpeg", "-v", "error", "-ss", str(max(0.0, d * at - 12.5)), "-t", "25", "-i", str(short)]
        try:
            piece = subprocess.run([*cmd, "-ac", "1", "-f", "wav", "-"], capture_output=True, timeout=120).stdout
        except (OSError, subprocess.TimeoutExpired):
            return 0.0
        ref = fingerprint(piece) if piece else None
        shares.append(similarity(ref, whole) if ref is not None and len(ref) else 0.0)
    return min(shares)


SAME_MASTER = 0.98  # same_master from here on: the same audio in another codec (measured: 0.995 and more)
_RATE = 11025  # Hz, mono: enough to tell a mix or master from another


def _pcm(path: Path) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(_RATE), "-f", "f32le", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=300).stdout
    except (OSError, subprocess.TimeoutExpired):
        return np.zeros(0, dtype="<f4")
    return np.frombuffer(out, dtype="<f4")


def _offset(a: np.ndarray, b: np.ndarray, frames: int = 120) -> int:
    """The shift (fingerprint frames, up to about 15 s) at which b's fingerprint agrees best with a's."""
    best, shift = -1.0, 0
    for k in range(-frames, frames + 1):
        x, y = (a[k:], b) if k >= 0 else (a, b[-k:])
        n = min(len(x), len(y))
        if n >= 100 and (share := 1 - _BITS[np.bitwise_xor(x[:n], y[:n]).view(np.uint8)].sum() / (32 * n)) > best:
            best, shift = share, k
    return shift


def _lag(x: np.ndarray, y: np.ndarray, guess: int, span: int) -> int:
    """The lag k (samples, guess ± span) at which y[i + k] matches x[i] best, from a 20 s piece of x."""
    m, n = len(x) // 3, 20 * _RATE
    lo = max(0, m + guess - span)
    piece, room = x[m : m + n], y[lo : m + guess + n + span]
    if len(piece) < _RATE or len(room) < len(piece):
        return guess
    size = 1 << int(len(room) + len(piece)).bit_length()
    c = np.fft.irfft(np.fft.rfft(room, size) * np.conj(np.fft.rfft(piece, size)), size)[: len(room) - len(piece) + 1]
    return int(np.argmax(c)) + lo - m


def same_master(a: Path, b: Path) -> float:
    """How alike two files' decoded audio is, sample by sample once aligned: the weakest correlation of
    its 10 s windows. The same master in another codec keeps 0.995 and more; another master or mix of the
    same recording (a remaster, a single version, a remix sharing the chorus) falls far below, though its
    fingerprint agrees. 0.0 when the lengths differ by more than 2 s or one cannot be read."""
    fa, fb = fingerprint(a), fingerprint(b)
    if fa is None or fb is None or not len(fa) or not len(fb):
        return 0.0
    x, y = _pcm(a), _pcm(b)
    if not len(x) or not len(y) or abs(len(x) - len(y)) > 2 * _RATE:
        return 0.0
    k = _lag(x, y, round(_offset(fa, fb) * -0.1238 * _RATE), _RATE // 2)
    x, y = (x, y[k:]) if k >= 0 else (x[-k:], y)
    n = min(len(x), len(y))
    x, y = x[:n], y[:n]
    weakest = 1.0
    for i in range(0, n - 5 * _RATE, 10 * _RATE):
        u, v = x[i : i + 10 * _RATE], y[i : i + 10 * _RATE]
        if u.std() < 1e-3 or v.std() < 1e-3:
            continue  # silence says nothing
        weakest = min(weakest, float(np.corrcoef(u, v)[0, 1]))
    return max(weakest, 0.0)
