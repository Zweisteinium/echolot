"""Downloads before they are filed: checked, repaired and normalised (ffprobe, ffmpeg, mutagen), the lossy
checks, tags and embedded covers.

prepare(), which every download goes through:
  1. the codec must match the extension, else Rejected; a FLAC must decode without errors and as long as its
     header says, else Damaged
  2. a FLAC mutagen cannot read (junk before the header) is remuxed, else re-encoded
  3. WAV, AIFF and ALAC become FLAC (exact_flac)
  4. FLAC above 48 kHz becomes 44.1 or 48 kHz, 24 bit (inaudible, half the size), unless keep_hires
  5. the spectrum check: a lossless file with an encoder's low-pass edge is fake (made from lossy); an edge
     from TRACES_FROM up only with the encoder's traces below it (a mastering filter cuts there too)
  6. lossprint (ECHOLOT_LOSSPRINT, default on) decides with the spectrum check (judge); each FLAC's checks
     go to lossy-checks.jsonl in the data folder
"""

import base64
import datetime
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
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
    """No usable audio (the message says why)."""


class Damaged(Rejected):
    """A FLAC with overwritten or missing bytes (noise, gaps, wrong seeks) or cut off (its header still
    claims the whole song)."""


SHORT = 0.5  # s: decoding this much shorter than the header says is cut off (a short last frame is less)


def _run(args: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, timeout=timeout)


def stream(path: Path, field: str) -> str:
    """One field of the first audio stream ('' if unknown); only the first value (ffprobe may print "flac,")."""
    try:
        r = _run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", f"stream={field}",
                  "-of", "csv=p=0", str(path)], 60)  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):  # (a hanging file: unknown, so prepare rejects it)
        return ""
    line = r.stdout.decode(errors="replace").strip().splitlines()
    return line[0].split(",")[0].strip() if line else ""


def probe(path: Path) -> tuple[float, int]:
    """(duration s, bitrate kbps) from the header; zeros if unreadable."""
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


def levels(path: Path) -> tuple[float | None, float | None]:
    """(peak, RMS) of the whole file in dBFS, all channels; None where unreadable or silent. Releases peak
    near 0 dB; one recorded at 25 % volume about -12 dB."""
    stats = "astats=measure_perchannel=none:measure_overall=Peak_level+RMS_level"
    try:
        r = _run(
            ["ffmpeg", "-v", "info", "-nostats", "-i", str(path), "-map", "0:a:0", "-af", stats, "-f", "null", "-"]
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    err = r.stderr.decode(errors="replace")

    def last(name: str) -> float | None:
        found = re.findall(rf"{name} level dB:\s*(-?[0-9.]+)", err)
        return round(float(found[-1]), 1) if found else None

    return last("Peak"), last("RMS")


def _flac_ok(path: Path) -> bool:
    from mutagen.flac import FLAC

    try:
        FLAC(path)
        return True
    except Exception:
        return False


def _stream_bytes(path: Path) -> tuple[int, int]:
    """(start, end) of a FLAC's stream in its file: after a leading ID3v2 tag, before a trailing ID3v1 or APE
    tag (a decoder reads those as broken frames)."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        head = f.read(10)
        f.seek(max(size - 160, 0))
        tail = f.read()
    start = end = 0
    if len(head) == 10 and head[:3] == b"ID3":
        start = 10 + ((head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14 | (head[8] & 0x7F) << 7 | (head[9] & 0x7F))
        start += 10 if head[5] & 0x10 else 0  # ID3v2 footer
    if tail[-128:-125] == b"TAG":
        end = 128
    ape = tail[
        -end - 32 : len(tail) - end
    ]  # APEv2 footer: "APETAGEX", version, size (items + footer), count, flags (bit 31: a header too)
    if ape[:8] == b"APETAGEX":
        end += int.from_bytes(ape[12:16], "little") + (32 if int.from_bytes(ape[20:24], "little") >> 31 else 0)
    return start, size - end


def damage(path: Path) -> str:
    """Why a FLAC's audio is damaged, '' if it is not. Decoded in full with frame checksums: a CRC error,
    lost frames or a cut-off end is damage; bytes after the last frame are not."""
    from mutagen.flac import FLAC

    try:
        start, end = _stream_bytes(path)
        info = FLAC(path).info
        header = info.total_samples * info.channels
    except Exception:  # unreadable: prepare() repairs or rejects it
        start, end, header = 0, path.stat().st_size, 0
    stream = f"subfile,,start,{start},end,{end},,:{path}"  # the FLAC stream without the tags around it
    checked = ["-nostats", "-loglevel", "level+info", "-err_detect", "crccheck+bitstream+buffer"]
    counted = ["-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"]  # volumedetect counts the samples
    try:
        r = _run(["ffmpeg", *checked, "-i", stream, *counted])
    except (OSError, subprocess.TimeoutExpired):
        return ""  # unknown: the other checks decide
    said = r.stderr.decode(errors="replace")
    errors = [line.split("] ", 2)[-1] for line in said.splitlines() if "[error]" in line]
    counts = re.findall(r"n_samples: (\d+)", said)
    decoded = int(counts[-1]) if counts else 0
    lost = (header - decoded) / (info.sample_rate * info.channels) if header and decoded < header else 0.0
    if any(e.startswith("CRC error") for e in errors) or (errors and lost):
        return f"damaged FLAC ({len(errors)} decoding error{'s' if len(errors) != 1 else ''}: {errors[0][:100]})"
    if lost > SHORT:
        return f"cut-off FLAC ({lost:.0f} s of the song missing)"
    return ""


def _to_flac(src: Path, dest: Path, *extra: str) -> bool:
    """src's first audio stream encoded (or copied) into dest as FLAC, with the tags; False if that failed."""
    tmp = dest.with_name(dest.name + ".tmp")
    r = _run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:a:0", *extra,
              "-map_metadata", "0", "-f", "flac", str(tmp)])  # fmt: skip
    if r.returncode == 0 and _flac_ok(tmp):
        tmp.replace(dest)
        return True
    tmp.unlink(missing_ok=True)
    return False


def exact_flac(path: Path, codec: str) -> Path:
    """A lossless original (WAV, AIFF, ALAC) as FLAC. Integer samples must decode identical, else the original
    stays; above 24 bit as 32-bit FLAC (ffmpeg would cut to 24). Floats (pcm_f32/64) become 32-bit integers
    (FLAC has none): only what is below about -180 dB or above 0 dBFS goes."""
    floating = codec.startswith("pcm_f")
    bits = max(
        (int(v) for f in ("bits_per_raw_sample", "bits_per_sample") if (v := stream(path, f)).isdigit()), default=0
    )
    wide = ["-strict", "experimental", "-sample_fmt", "s32", "-bits_per_raw_sample", "32"]
    flac = path.with_suffix(".flac")
    if not _to_flac(path, flac, "-c:a", "flac", *(wide if floating or bits > 24 else []), "-compression_level", "5"):
        return path
    if not floating and (not (original := pcm_digest(path)) or pcm_digest(flac) != original):
        log.warning("%s: its FLAC is not sample for sample the same: the original stays", path.name)
        flac.unlink(missing_ok=True)
        return path
    path.unlink(missing_ok=True)
    return flac


def pcm_digest(path: Path) -> str:
    """MD5 of the decoded audio as 32-bit integers (exact up to 32-bit samples); "" if undecodable."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "s32le", "-c:a", "pcm_s32le", "-"]
    md5, size = hashlib.md5(), 0
    try:
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as p:
            assert p.stdout is not None
            while chunk := p.stdout.read(1 << 20):
                md5.update(chunk)
                size += len(chunk)
            ok = p.wait(timeout=600) == 0
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return md5.hexdigest() if ok and size else ""


@dataclass
class Prepared:
    path: Path
    fake: bool  # a FLAC made from a lossy file
    spectrum: dict | None
    lossprint: dict | None = None


def prepare(path: Path, keep_hires: bool = False) -> Prepared:
    """Check and normalise a download (module doc); Rejected if no usable audio. The returned path may be a
    converted file."""
    ext = path.suffix.lower().lstrip(".")
    codec = stream(path, "codec_name")
    if not codec or not codec.startswith(CODECS.get(ext, ("-",))):
        path.unlink(missing_ok=True)
        raise Rejected(f"codec '{codec or 'none'}' in a .{ext} file")
    if ext == "flac" and (why := damage(path)):
        path.unlink(missing_ok=True)
        raise Damaged(why)
    repaired = ext != "flac" or _flac_ok(path)  # else remux, or re-encode
    if not (repaired or _to_flac(path, path, "-c", "copy") or _to_flac(path, path, "-c:a", "flac")):
        path.unlink(missing_ok=True)
        raise Rejected("unreadable FLAC")
    if ext in ("wav", "aiff", "aif") or (ext == "m4a" and codec == "alac"):
        path = exact_flac(path, codec)
        ext = path.suffix.lower().lstrip(".")
    if ext not in (*LOSSLESS, "aif"):
        return Prepared(path, False, None)
    rate = int(stream(path, "sample_rate") or 0)
    if ext == "flac" and rate > 48000 and not keep_hires:  # (an original exact_flac kept is left alone)
        target = "44100" if rate % 44100 == 0 else "48000"
        _to_flac(path, path, "-af", "aresample=resampler=soxr:precision=28", "-ar", target,
                 "-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24",
                 "-compression_level", "5")  # fmt: skip
    result = spectrum(path)
    lp = lossprint(path) if lossprint_on() else None
    if lp is None:  # off or not installed: the spectrum check alone
        return Prepared(path, result.get("verdict") == "lossy", result)
    label, source = judge(result, lp)
    if label == "lossy":
        result = result | {"source": source} | ({} if result.get("verdict") == "lossy" else {"by": "lossprint"})
    _log_checks(path, result, lp, label)
    return Prepared(path, label == "lossy", result, lp)


# ---------------------------------------------------------------- lossprint, the second lossy check

LOSSPRINT_FAKE = 0.9  # P(transcode) from which lossprint alone makes a FLAC lossy (99.8% of 4,162 test transcodes)
LOSSPRINT_DOUBT = 0.5  # up to LOSSPRINT_FAKE: doubtful (a label, no decision)
FAMILIES = {
    "mp3": "MP3",
    "ffmpeg_aac": "AAC",
    "vorbis": "Vorbis",
    "opus": "Opus",
    "wma": "WMA",
    "mp2": "MP2",
    "musepack": "Musepack",
}  # lossprint's encoder families (it reports every AAC encoder as ffmpeg_aac)
_one_at_a_time = threading.Semaphore(1)  # about 450 MB per run
_log_lock = threading.Lock()


def lossprint_on() -> bool:
    """ECHOLOT_LOSSPRINT: on (default) or off."""
    return os.environ.get("ECHOLOT_LOSSPRINT", "on").strip().lower() != "off"


def lossprint(path: Path, background: bool = False) -> dict | None:
    """lossprint's judgement of a FLAC: {"p": P(made from lossy), "encoder", "kbps"} (its guesses), {"error":
    why} when it cannot judge (over 2 channels, under 2 s, unreadable), None if not installed. A CNN on six
    excerpts' spectrograms; deterministic, about 3 s per file. `background`: lowest CPU priority."""
    exe = shutil.which("lossprint")
    if not exe:
        return None
    nice = ["nice", "-n", "19"] if background and shutil.which("nice") else []
    try:
        with _one_at_a_time:
            args = [*nice, exe, "--jobs", "1", "--threshold", "0", "--output-format", "jsonl", "--", str(path)]
            r = _run(args, 300)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": type(e).__name__}
    for line in r.stdout.decode(errors="replace").splitlines():
        try:
            d = json.loads(line)
            return {"p": float(d["transcode_probability"]), "encoder": d.get("encoder"), "kbps": d.get("bitrate_kbps")}
        except (ValueError, KeyError, TypeError):
            continue
    return {"error": r.stderr.decode(errors="replace").strip()[-200:] or f"exit {r.returncode}"}


def judge(spec: dict, lp: dict | None) -> tuple[str, str]:
    """(lossy | doubtful | ok, the likely source of a lossy one). Lossy: the spectrum check's verdict whatever
    lossprint says (it misses some old encoders whose low-pass shows), or lossprint from LOSSPRINT_FAKE (it
    hears Opus, V0, AAC 256, MP3 320 behind a clipped master); doubtful: lossprint from LOSSPRINT_DOUBT. The
    source: lossprint's guess, else the spectrum's band ("~128 kbps"); when both find it lossy, lossprint's
    only if it names a bitrate."""
    p = lp.get("p") if lp else None
    heard = ""
    if p is not None and p >= LOSSPRINT_FAKE:
        family = FAMILIES.get(lp.get("encoder") or "", lp.get("encoder") or "lossy")
        heard = f"{family} ~{lp['kbps']:.0f} kbps" if lp.get("kbps") else family
    if spec.get("verdict") == "lossy":
        return "lossy", heard if " ~" in heard else spec.get("source", "")
    if heard:
        return "lossy", heard
    return ("doubtful", "") if p is not None and p >= LOSSPRINT_DOUBT else ("ok", "")


def streaminfo_md5(path: Path) -> str:
    """A FLAC's audio MD5 from STREAMINFO (a retag keeps it); "" if unreadable or unset."""
    try:
        from mutagen.flac import FLAC

        md5 = FLAC(path).info.md5_signature
    except Exception:
        return ""
    return f"{md5:032x}" if md5 else ""


def _log_checks(path: Path, spec: dict, lp: dict | None, label: str) -> None:
    """One line per checked FLAC in lossy-checks.jsonl. The STREAMINFO MD5 finds the file later (filing
    renames it)."""
    md5 = streaminfo_md5(path)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    size = path.stat().st_size if path.exists() else 0
    line = {
        "ts": now,
        "path": str(path),
        "bytes": size,
        "md5": md5,
        "spectrum": spec,
        "lossprint": lp,
        "verdict": label,
    }
    try:
        with _log_lock, open(Path(os.environ.get("ECHOLOT_DATA_DIR", "data")) / "lossy-checks.jsonl", "a") as f:
            f.write(json.dumps(line) + "\n")
    except OSError as e:
        log.warning("lossy-checks.jsonl: %s", e)


# ---------------------------------------------------------------- spectrum check

DROP_DB = 30.0  # dB fall within ~1 kHz that counts as an encoder low-pass
MAX_LOSSY_CUT = 20400  # Hz: edges above are anti-alias filters of genuine masters
TRACES_FROM = 19000  # Hz: an edge from here up may be a mastering filter: lossy only with the encoder's traces
FLICKER = 0.03  # traces: share of bins below the edge flipping between hole and content per frame


def spectrum(path: Path) -> dict:
    """Is this "lossless" file made from MP3/AAC? Lossy encoders cut with a steep low-pass (MP3 128k ~16 kHz,
    192k ~19 kHz, V0/256k ~19.5-20 kHz); CD audio rolls off gradually up to ~22 kHz. Three 15 s excerpts give
    a power spectrum; the steepest drop from 12 kHz to Nyquist is the edge. From TRACES_FROM up it also needs
    the encoder's traces (_flicker): genuine files through steep 19.5 and 20 kHz filters stayed under 0.027,
    MP3 320/V0 transcodes mostly far above. verdict: lossy (with the likely source), ok, or unknown (too short
    or quiet). MP3 320 without an edge and AAC near 20 kHz go unseen; they are nearly transparent."""
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
    """An encoder's traces below its edge: it gives the top bands' bits to some frames (~23 ms) and none to
    others, so a bin there flips between content and a hole (25 dB under the band); filtered audio does not.
    The share of flips in the 2.2 kHz below the edge, over loud frames well above 16-bit rounding; None when
    too few."""
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
    """(artist, title) tags for showing ('' where missing)."""
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


SPOTIFY_ID = re.compile(r"[0-9A-Za-z]{22}")


def read_spotify_id(path: Path) -> str:
    """The SPOTIFY_TRACK_ID tag a recorder such as Mitschnitt writes ('' if none or malformed)."""
    from mutagen import File

    try:
        m = File(path)
        t = m.tags if m is not None and m.tags is not None else None
        if t is None:
            return ""
        if hasattr(t, "getall"):  # ID3
            values = [str(v) for f in t.getall("TXXX:SPOTIFY_TRACK_ID") for v in f.text]
        elif "----:com.apple.iTunes:SPOTIFY_TRACK_ID" in t:  # MP4
            values = [bytes(v).decode() for v in t["----:com.apple.iTunes:SPOTIFY_TRACK_ID"]]
        else:  # Vorbis comments (FLAC, Ogg, Opus)
            values = list(t.get("SPOTIFY_TRACK_ID") or [])
        value = (values or [""])[0].strip().removeprefix("spotify:track:")
        return value if SPOTIFY_ID.fullmatch(value) else ""
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
    from mutagen.aiff import AIFF
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3, ID3NoHeaderError
    from mutagen.mp4 import MP4
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
    from mutagen.wave import WAVE

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
        if isinstance(a, WAVE | AIFF):  # its ID3 is a chunk of the file, not in front of it
            return bool(a.tags and a.tags.getall("APIC"))
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
    """Embed a JPEG as front cover unless the file has a picture (replace: in place of its pictures)."""
    from mutagen import File
    from mutagen.aiff import AIFF
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
    from mutagen.wave import WAVE

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
        elif isinstance(a, WAVE | AIFF):  # an ID3 in front of a RIFF or AIFF file would break it
            if a.tags is None:
                a.add_tags()
            a.tags.delall("APIC")
            a.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpg))
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
