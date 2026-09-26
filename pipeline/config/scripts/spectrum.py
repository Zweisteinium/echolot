#!/usr/bin/env python3
"""spectrum.py <file> [--json]: detect lossy-sourced "lossless" files (MP3/AAC re-encoded to FLAC/WAV).
Lossy encoders cut the spectrum with a steep low-pass (MP3 128k ~16 kHz, 192k ~19 kHz, V0/256k ~19.5-20 kHz);
real CD audio rolls off gradually up to ~22 kHz. Three 15 s excerpts are decoded, averaged into a power
spectrum, and the steepest drop between 12 kHz and Nyquist is located.
Verdict: lossy  = brickwall >= DROP_DB below 20.4 kHz (prints the likely source bitrate)
         ok     = no such edge
         unknown= too quiet / too short to judge
Limit: MP3 320k (low-pass ~20.5 kHz) is indistinguishable this way; it is also close to transparent."""
import json, subprocess, sys
import numpy as np

DROP_DB = 30.0          # dB fall within ~1 kHz that counts as an encoder low-pass
MAX_LOSSY_CUT = 20400   # edges above this are ordinary anti-alias filters of genuine masters

def probe(path):
    r = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
        "stream=sample_rate:format=duration", "-of", "json", path], capture_output=True, text=True).stdout or "{}")
    return int((r.get("streams") or [{}])[0].get("sample_rate") or 0), float(r.get("format", {}).get("duration") or 0)

def analyse(path):
    sr, dur = probe(path)
    if not sr or dur < 20: return {"verdict": "unknown", "reason": "too short or unreadable", "sr": sr}
    nfft, hop = 8192, 4096; win = np.hanning(nfft).astype(np.float32); acc = np.zeros(nfft // 2 + 1); frames = 0
    for pos in (0.3, 0.5, 0.7):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{dur * pos:.1f}", "-t", "15", "-i", path,
                              "-ac", "1", "-f", "f32le", "-"], capture_output=True).stdout
        x = np.frombuffer(raw, dtype=np.float32)
        for i in range(0, len(x) - nfft, hop):
            acc += np.abs(np.fft.rfft(x[i:i + nfft] * win)) ** 2; frames += 1
    if frames < 20: return {"verdict": "unknown", "reason": "no audio decoded", "sr": sr}
    freqs = np.fft.rfftfreq(nfft, 1 / sr); db = 10 * np.log10(acc / frames + 1e-20)
    ref = db[(freqs > 1000) & (freqs < 8000)].mean()
    if ref < -90: return {"verdict": "unknown", "reason": "too quiet", "sr": sr}
    band = lambda lo, hi: db[(freqs >= lo) & (freqs < hi)].mean()
    best_cut, best_drop = 0, 0.0
    for cut in range(12000, int(min(sr / 2 - 1600, 23000)), 100):
        drop = band(cut - 1500, cut - 300) - band(cut + 300, cut + 1500)
        if drop > best_drop: best_cut, best_drop = cut, drop
    res = {"sr": sr, "cutoff_hz": best_cut, "drop_db": round(float(best_drop), 1)}
    if best_drop >= DROP_DB and best_cut < MAX_LOSSY_CUT:
        src = "~128 kbps" if best_cut < 16800 else "~160-192 kbps" if best_cut < 19300 else "~256 kbps / V0"
        res.update(verdict="lossy", source=src)
    else:
        res["verdict"] = "ok"
    return res

TRACKS = "/music/tracks/"
LIST = "/config/state/lossy-sourced.json"   # library stem -> detection result; read by the weekly upgrade pass

def update_list(path, result):
    """Record (result given) or forget (result None) a library file; files outside tracks/ are ignored.
    Parallel Sockseek downloads can finish together, hence the lock."""
    import datetime, fcntl, os
    if not path.startswith(TRACKS): return
    stem = os.path.splitext(path[len(TRACKS):])[0]
    with open(LIST + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        data = json.load(open(LIST)) if os.path.exists(LIST) else {}
        if result and result.get("verdict") == "lossy":
            data[stem] = {**result, "detected": datetime.date.today().isoformat()}
        elif result is None or result.get("verdict") == "ok":
            data.pop(stem, None)
        with open(LIST + ".tmp", "w") as f: json.dump(data, f, indent=1, ensure_ascii=False)
        os.replace(LIST + ".tmp", LIST)

if __name__ == "__main__":
    if "--forget" in sys.argv: update_list(sys.argv[1], None); sys.exit(0)
    r = analyse(sys.argv[1])
    if "--record" in sys.argv: update_list(sys.argv[1], r)
    print(json.dumps(r) if "--json" in sys.argv else
          f"{r['verdict']}: cutoff {r.get('cutoff_hz', 0) / 1000:.1f} kHz, edge {r.get('drop_db', 0)} dB {r.get('source', '')}".strip())
