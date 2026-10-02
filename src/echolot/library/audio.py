"""Downloaded audio before it is filed: checked, repaired and normalised (ffprobe, ffmpeg, mutagen), the
spectrum check for "lossless" files made from lossy ones, tags and embedded covers.

prepare() is what every download goes through:
  1. the codec must match the extension (an MP3 renamed to .flac, an unreadable file: rejected)
  2. a FLAC mutagen cannot read (junk before the header) is remuxed, else re-encoded (lossless)
  3. WAV, AIFF and ALAC become FLAC (lossless, smaller, standard tags)
  4. hi-res FLAC (> 48 kHz) becomes 44.1 or 48 kHz, 24 bit (inaudible, about half the size)
  5. the spectrum check: a FLAC with an encoder's low-pass edge is marked fake (made from lossy); an edge
     from 19 kHz up only with the encoder's traces below it (a mastering filter can cut there too)
"""

import base64
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

AUDIO = ["flac", "wav", "aiff", "m4a", "mp3", "opus", "ogg", "webm", "aac"]  # preference order
LOSSLESS = {"flac", "wav", "aiff"}
CODECS = {  # extension -> codecs a file with it may hold
    "flac": ("flac",), "mp3": ("mp3",), "m4a": ("aac", "alac"), "aac": ("aac",),
    "opus": ("opus",), "ogg": ("vorbis", "opus"), "webm": ("opus", "vorbis"), "wav": ("pcm_",),
    "aiff": ("pcm_",), "aif": ("pcm_",),
}  # fmt: skip


class Rejected(Exception):
    """The file is no usable audio (message: why)."""


def _run(args: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, timeout=timeout)


def stream(path: Path, field: str) -> str:
    """One field of the first audio stream ('' if unknown). ffprobe can print "flac," with side data:
    only the first value counts."""
    r = _run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", f"stream={field}",
              "-of", "csv=p=0", str(path)], 60)  # fmt: skip
    line = r.stdout.decode(errors="replace").strip().splitlines()
    return line[0].split(",")[0].strip() if line else ""


def probe(path: Path) -> tuple[float, int]:
    """(duration in s, bitrate in kbps) from the file header; zeros if unreadable."""
    from mutagen import File

    try:
        audio = File(path)
        info = audio.info if audio is not None else None
        if info is None:
            return 0.0, 0
        kbps = int((getattr(info, "bitrate", 0) or 0) / 1000)
        return float(getattr(info, "length", 0) or 0), kbps
    except Exception:
        return 0.0, 0


def _flac_ok(path: Path) -> bool:
    from mutagen.flac import FLAC

    try:
        FLAC(path)
        return True
    except Exception:
        return False


def _to_flac(src: Path, dest: Path, *extra: str) -> bool:
    """Encode (or copy) the first audio stream of src into dest as FLAC, keeping the tags."""
    tmp = dest.with_name(dest.name + ".tmp")
    r = _run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:a:0", *extra,
              "-map_metadata", "0", "-f", "flac", str(tmp)])  # fmt: skip
    if r.returncode == 0 and _flac_ok(tmp):
        tmp.replace(dest)
        return True
    tmp.unlink(missing_ok=True)
    return False


@dataclass
class Prepared:
    path: Path
    fake: bool  # a FLAC made from a lossy file
    spectrum: dict | None


def prepare(path: Path) -> Prepared:
    """Check and normalise a download (see the module doc); Rejected if it is no usable audio. The
    file may be replaced by a converted one (the returned path)."""
    ext = path.suffix.lower().lstrip(".")
    codec = stream(path, "codec_name")
    if not codec or not codec.startswith(CODECS.get(ext, ("-",))):
        path.unlink(missing_ok=True)
        raise Rejected(f"codec '{codec or 'none'}' in a .{ext} file")
    repaired = ext != "flac" or _flac_ok(path)  # else: remux, or re-encode (lossless)
    if not (repaired or _to_flac(path, path, "-c", "copy") or _to_flac(path, path, "-c:a", "flac")):
        path.unlink(missing_ok=True)
        raise Rejected("unreadable FLAC")
    if ext in ("wav", "aiff", "aif") or (ext == "m4a" and codec == "alac"):
        flac = path.with_suffix(".flac")
        if _to_flac(path, flac, "-c:a", "flac", "-compression_level", "5"):
            path.unlink(missing_ok=True)
            path, ext = flac, "flac"
    if ext != "flac":
        return Prepared(path, False, None)
    rate = int(stream(path, "sample_rate") or 0)
    if rate > 48000:
        target = "44100" if rate % 44100 == 0 else "48000"
        _to_flac(path, path, "-af", "aresample=resampler=soxr:precision=28", "-ar", target,
                 "-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24",
                 "-compression_level", "5")  # fmt: skip
    result = spectrum(path)
    return Prepared(path, result.get("verdict") == "lossy", result)


# ---------------------------------------------------------------- spectrum check

DROP_DB = 30.0  # dB fall within ~1 kHz that counts as an encoder low-pass
MAX_LOSSY_CUT = 20400  # edges above this are ordinary anti-alias filters of genuine masters
TRACES_FROM = 19000  # an edge from here up may be a mastering filter: lossy only with the encoder's traces
FLICKER = 0.03  # traces: share of bins below the edge switching between hole and content from frame to frame


def spectrum(path: Path) -> dict:
    """Is this "lossless" file made from MP3/AAC? Lossy encoders cut the spectrum with a steep
    low-pass (MP3 128k ~16 kHz, 192k ~19 kHz, V0/256k ~19.5-20 kHz); real CD audio rolls off gradually
    up to ~22 kHz. Three 15 s excerpts are averaged into a power spectrum and the steepest drop between
    12 kHz and Nyquist is located. An edge from TRACES_FROM up also needs the encoder's traces below it
    (_flicker): a steep mastering or resampling filter cuts there as well, and on genuine files tested with
    such filters (19.5 and 20 kHz) the flicker stayed under 0.027, while MP3 320/V0 transcodes were mostly
    far above. verdict: lossy (with the likely source), ok, or unknown (too short or quiet). MP3 320k
    without an edge, and AAC near 20 kHz, can't be told apart this way; they are nearly transparent."""
    import numpy as np

    r = _run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
              "stream=sample_rate:format=duration", "-of", "json", str(path)], 60)  # fmt: skip
    info = json.loads(r.stdout or b"{}")
    sr = int((info.get("streams") or [{}])[0].get("sample_rate") or 0)
    dur = float((info.get("format") or {}).get("duration") or 0)
    if not sr or dur < 20:
        return {"verdict": "unknown", "reason": "too short or unreadable", "sr": sr}
    nfft, hop = 8192, 4096
    win = np.hanning(nfft).astype(np.float32)
    acc, frames, pieces = np.zeros(nfft // 2 + 1), 0, []
    for pos in (0.3, 0.5, 0.7):
        raw = _run(["ffmpeg", "-v", "error", "-ss", f"{dur * pos:.1f}", "-t", "15", "-i", str(path),
                    "-ac", "1", "-f", "f32le", "-"], 120).stdout  # fmt: skip
        x = np.frombuffer(raw, dtype=np.float32)
        pieces.append(x)
        for i in range(0, len(x) - nfft, hop):
            acc += np.abs(np.fft.rfft(x[i : i + nfft] * win)) ** 2
            frames += 1
    if frames < 20:
        return {"verdict": "unknown", "reason": "no audio decoded", "sr": sr}
    freqs = np.fft.rfftfreq(nfft, 1 / sr)
    db = 10 * np.log10(acc / frames + 1e-20)
    if db[(freqs > 1000) & (freqs < 8000)].mean() < -90:
        return {"verdict": "unknown", "reason": "too quiet", "sr": sr}

    def band(lo: float, hi: float) -> float:
        return float(db[(freqs >= lo) & (freqs < hi)].mean())

    best_cut, best_drop = 0, 0.0
    for cut in range(12000, int(min(sr / 2 - 1600, 23000)), 100):
        drop = band(cut - 1500, cut - 300) - band(cut + 300, cut + 1500)
        if drop > best_drop:
            best_cut, best_drop = cut, drop
    res = {"sr": sr, "cutoff_hz": best_cut, "drop_db": round(best_drop, 1), "verdict": "ok"}
    if best_drop >= DROP_DB and best_cut < MAX_LOSSY_CUT:
        flicker = _flicker(pieces, sr, best_cut) if best_cut >= TRACES_FROM else None
        if flicker is not None:
            res["flicker"] = flicker
        if flicker is not None and flicker <= FLICKER:
            res["reason"] = "a steep edge without an encoder's traces (a mastering filter)"
        else:
            source = "~128 kbps" if best_cut < 16800 else "~160-192 kbps" if best_cut < 19300 else "~256 kbps / V0"
            res.update(verdict="lossy", source=source)
    return res


def _flicker(pieces: list, sr: int, cut: int) -> float | None:
    """An encoder's traces below its edge: it gives the bits of the highest bands to some frames (~23 ms)
    and none to others, so there a bin switches between content and a hole (25 dB under the band) from one
    frame to the next; a tone or noise through a filter stays as it is. The share of such switches in the
    2.2 kHz below the edge, over loud frames whose band is well above 16-bit rounding; None when too few."""
    import numpy as np

    nfft = 1024
    win = np.hanning(nfft).astype(np.float32)
    f = np.fft.rfftfreq(nfft, 1 / sr)
    band, mid = (f >= cut - 2500) & (f < cut - 300), (f >= 1000) & (f < 8000)
    floor = 10 * np.log10(nfft / 4 * 1e-10)  # 16-bit rounding noise per bin, about
    flips = frames = 0
    for x in pieces:
        if len(x) < nfft * 4:
            continue
        p = np.abs(np.fft.rfft(np.lib.stride_tricks.sliding_window_view(x, nfft)[:: nfft // 2] * win, axis=1)) ** 2
        p += 1e-20
        mid_db, b = 10 * np.log10(p[:, mid].mean(1)), p[:, band]
        loud = (mid_db > np.percentile(mid_db, 90) - 15) & (10 * np.log10(b.mean(1)) > floor + 30)
        if loud.sum() < 2:
            continue
        hole = b[loud] < b[loud].mean(1, keepdims=True) * 10**-2.5
        flips += int((hole[1:] != hole[:-1]).sum())
        frames += int(loud.sum()) - 1
    return round(flips / (frames * int(band.sum())), 4) if frames >= 30 else None


# ---------------------------------------------------------------- tags and covers


def read_tags(path: Path) -> tuple[list[str], str]:
    """(artist and album artist tags, title tag)."""
    from mutagen import File

    try:
        m = File(path, easy=True)
        t = m.tags if m is not None and m.tags is not None else {}

        def get(k: str) -> list[str]:
            return [v for v in (t.get(k) or []) if v]

        return get("artist") + get("albumartist"), (get("title") or [""])[0]
    except Exception:
        return [], ""


def read_credit(path: Path) -> tuple[str, str]:
    """(artist tag, title tag) as the file names them, for showing it ('' where missing)."""
    from mutagen import File

    try:
        m = File(path, easy=True)
        t = m.tags if m is not None and m.tags is not None else {}
        return ", ".join(v.strip() for v in t.get("artist") or [] if v.strip()), ((t.get("title") or [""])[0]).strip()
    except Exception:
        return "", ""


def read_isrc(path: Path) -> str:
    """The ISRC tag ('' if none)."""
    from mutagen import File

    try:
        m = File(path, easy=True)
        return ((m.tags.get("isrc") if m is not None and m.tags is not None else None) or [""])[0].strip()
    except Exception:
        return ""


def write_tags(path: Path, **values: str) -> None:
    """Set tags (artist=..., title=..., album=...); <tag>_if_empty=... only where the tag is empty."""
    from mutagen import File

    try:
        m = File(path, easy=True)
        if m is None:
            return
        if m.tags is None:
            m.add_tags()
        for key, value in values.items():
            if key.endswith("_if_empty"):
                key = key.removesuffix("_if_empty")
                if (m.tags.get(key) or [""])[0].strip():
                    continue
            m[key] = [value]
        m.save()
    except Exception as e:
        log.warning("tags of %s not written: %s", path, e)


def has_picture(path: Path) -> bool:
    from mutagen import File
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3, ID3NoHeaderError
    from mutagen.mp4 import MP4
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis

    try:
        a = File(path)
        if a is None:
            return True
        if isinstance(a, FLAC):
            return bool(a.pictures)
        if isinstance(a, MP4):
            return bool(a.tags and a.tags.get("covr"))
        if isinstance(a, OggOpus | OggVorbis):
            return bool(a.get("metadata_block_picture"))
        try:
            return any(k.startswith("APIC") for k in ID3(path))
        except ID3NoHeaderError:
            return False
    except Exception:
        return True  # unreadable: don't touch


def picture(path: Path) -> bytes | None:
    """The file's embedded picture (the first one), None if it has none or cannot be read."""
    from mutagen import File
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis

    try:
        a = File(path)
        if isinstance(a, FLAC):
            return a.pictures[0].data if a.pictures else None
        if isinstance(a, MP4):
            covers = (a.tags or {}).get("covr") or []
            return bytes(covers[0]) if covers else None
        if isinstance(a, OggOpus | OggVorbis):
            blocks = a.get("metadata_block_picture") or []
            return Picture(base64.b64decode(blocks[0])).data if blocks else None
        frames = ID3(path).getall("APIC")
        return frames[0].data if frames else None
    except Exception:
        return None


def embed_cover(path: Path, jpg: bytes, replace: bool = False) -> bool:
    """Embed a JPEG as the front cover unless the file has a picture already (replace: instead of the
    pictures it has)."""
    from mutagen import File
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis

    if not jpg or (not replace and has_picture(path)):
        return False
    try:
        a = File(path)
        pic = Picture()
        pic.type, pic.mime, pic.data = 3, "image/jpeg", jpg
        if isinstance(a, FLAC):
            a.clear_pictures()
            a.add_picture(pic)
            a.save()
        elif isinstance(a, MP4):
            if a.tags is None:
                a.add_tags()
            a.tags["covr"] = [MP4Cover(jpg, MP4Cover.FORMAT_JPEG)]
            a.save()
        elif isinstance(a, OggOpus | OggVorbis):
            a["metadata_block_picture"] = [base64.b64encode(pic.write()).decode()]
            a.save()
        else:
            try:
                t = ID3(path)
            except ID3NoHeaderError:
                t = ID3()
            t.delall("APIC")
            t.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpg))
            t.save(path)
        return True
    except Exception as e:
        log.warning("cover of %s not embedded: %s", path, e)
        return False
