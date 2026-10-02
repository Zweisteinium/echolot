"""Smoke test of a built image: the tools Echolot runs are there and do what it needs. CI runs it in the
image; locally: docker run --rm -v "$PWD/tools:/tools:ro" echolot:dev python /tools/smoke.py"""

import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

from echolot.library import audio, identity


def hires_wav(path: Path, seconds: int = 20, rate: int = 96000, tone: float = 220, glide: float = 20) -> None:
    """24-bit stereo WAV at 96 kHz: a gliding tone with beats, enough for a fingerprint."""
    t = np.arange(seconds * rate) / rate
    x = 0.4 * np.sin(2 * np.pi * (tone + glide * t) * t) + 0.3 * np.sin(2 * np.pi * 3 * tone * t) * (t % 1 < 0.4)
    x += 0.02 * np.random.default_rng(1).standard_normal(len(t))
    pcm = (np.clip(x, -1, 1) * (2**23 - 1)).astype("<i4")
    frames = np.repeat(pcm, 2).view(np.uint8).reshape(-1, 4)[:, :3].tobytes()  # 3 bytes per sample
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(3)
        w.setframerate(rate)
        w.writeframes(frames)


def run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def main() -> int:
    failed = []

    def check(name: str, ok: bool, detail: object = "") -> None:
        print(f"{'ok  ' if ok else 'FAIL'} {name} {detail}".rstrip())
        if not ok:
            failed.append(name)

    check("ffmpeg chromaprint muxer", " chromaprint " in run("ffmpeg", "-hide_banner", "-muxers"))
    check("ffmpeg soxr", "--enable-libsoxr" in run("ffmpeg", "-hide_banner", "-buildconf"))
    runtimes = subprocess.run(["yt-dlp", "-v", "--js-runtimes", "quickjs"], capture_output=True, text=True).stderr
    js = next((line for line in runtimes.splitlines() if "JS runtimes:" in line), "")
    check("yt-dlp JavaScript runtime", "quickjs-" in js and "unsupported" not in js, js.split(": ", 1)[-1])
    check("yt-dlp finds ffmpeg", FFmpegPostProcessor(None).available)
    check("curl_cffi", __import__("curl_cffi") is not None)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "hires.wav"
        hires_wav(src)
        prepared = audio.prepare(src)  # WAV to FLAC, 96 kHz resampled with soxr, the spectrum check
        rate, bits = audio.stream(prepared.path, "sample_rate"), audio.stream(prepared.path, "bits_per_raw_sample")
        check("prepare: FLAC, 48 kHz, 24 bit", (prepared.path.suffix, rate, bits) == (".flac", "48000", "24"))
        duration, _ = audio.probe(prepared.path)
        check("probe: length", abs(duration - 20) < 0.1, f"{duration:.2f} s")
        check("spectrum check", bool(prepared.spectrum and prepared.spectrum.get("verdict")), prepared.spectrum)
        fp = identity.fingerprint(prepared.path)
        check("fingerprint", fp is not None and len(fp) > 100, None if fp is None else len(fp))
        mp3 = Path(tmp) / "x.mp3"
        run("ffmpeg", "-v", "error", "-i", str(prepared.path), "-c:a", "libmp3lame", "-q:a", "2", str(mp3))
        check("MP3 encoding (yt-dlp conversions)", audio.stream(mp3, "codec_name") == "mp3")
        same = identity.same_master(prepared.path, mp3)
        check("same master: FLAC and its MP3", same >= identity.SAME_MASTER, f"{same:.4f}")
        hires_wav(other := Path(tmp) / "other.wav", tone=440, glide=-15)  # no shift makes it the first
        differ = identity.same_master(prepared.path, other)
        check("same master: another signal is not", differ < 0.5, f"{differ:.4f}")
        louder = Path(tmp) / "louder.flac"
        run("ffmpeg", "-v", "error", "-i", str(prepared.path), "-af", "volume=6dB:precision=double", str(louder))
        hint = identity.compare(louder, mp3)
        check("compare: the same audio, louder", hint.startswith("same audio") and "dB louder" in hint, hint)
        hint = identity.compare(other, mp3)
        check("compare: another signal", hint.startswith("another version"), hint)
    print("smoke test failed: " + ", ".join(failed) if failed else "smoke test passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
