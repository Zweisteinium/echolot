"""lossprint, the second lossy check: its call, the combined verdict and the modes off, shadow and on."""

import json
import subprocess
from pathlib import Path

import pytest

from echolot.library import audio

REAL_LOSSPRINT = audio.lossprint  # (conftest stubs it for every test)


def done(stdout: str, stderr: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, stdout.encode(), stderr.encode())


def test_lossprint_reads_its_jsonl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Probability, encoder family and bitrate from its JSON line; one file, one worker, every guess (-t 0)."""
    calls: list[list[str]] = []
    monkeypatch.setattr(audio.shutil, "which", lambda name: "/usr/local/bin/lossprint")
    line = {"transcode_probability": 0.9981, "verdict": "transcode", "encoder": "opus", "bitrate_kbps": 127.3}

    def run(args: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
        calls.append(args)
        return done(json.dumps(line | {"path": args[-1]}) + "\n")

    monkeypatch.setattr(audio, "_run", run)
    assert REAL_LOSSPRINT(tmp_path / "a.flac") == {"p": 0.9981, "encoder": "opus", "kbps": 127.3}
    assert calls[0][1:] == [
        "--jobs",
        "1",
        "--threshold",
        "0",
        "--output-format",
        "jsonl",
        "--",
        str(tmp_path / "a.flac"),
    ]


def test_lossprint_errors_and_absence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A file it can't judge gives its reason; not installed gives None (the spectrum check decides alone)."""
    monkeypatch.setattr(audio.shutil, "which", lambda name: "/usr/local/bin/lossprint")
    monkeypatch.setattr(audio, "_run", lambda args, timeout=600: done("", "error: 8 channels; only mono and stereo", 1))
    assert REAL_LOSSPRINT(tmp_path / "a.flac") == {"error": "error: 8 channels; only mono and stereo"}

    def hangs(args: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr(audio, "_run", hangs)
    assert REAL_LOSSPRINT(tmp_path / "a.flac") == {"error": "TimeoutExpired"}
    monkeypatch.setattr(audio.shutil, "which", lambda name: None)
    assert REAL_LOSSPRINT(tmp_path / "a.flac") is None


@pytest.mark.parametrize(
    ("spec", "lp", "expected"),
    [
        ({"verdict": "lossy", "source": "~128 kbps"}, {"p": 1.0, "encoder": "mp3", "kbps": 128.0}, (True, "~128 kbps")),
        (
            {"verdict": "lossy", "source": "~128 kbps"},
            {"p": 0.0001, "encoder": None, "kbps": None},
            (True, "~128 kbps"),
        ),
        ({"verdict": "ok"}, {"p": 0.9999, "encoder": "opus", "kbps": 122.4}, (True, "Opus ~122 kbps")),
        ({"verdict": "ok"}, {"p": 0.97, "encoder": "ffmpeg_aac", "kbps": None}, (True, "AAC")),
        ({"verdict": "ok"}, {"p": 0.4999, "encoder": "mp3", "kbps": 320.0}, (False, "")),
        ({"verdict": "ok"}, {"error": "audio is shorter than the 2-second minimum"}, (False, "")),
        ({"verdict": "unknown"}, None, (False, "")),
    ],
)
def test_judge(spec: dict, lp: dict | None, expected: tuple[bool, str]) -> None:
    """Made from lossy when either check says so; the spectrum check's verdict and source stand where it fires."""
    assert audio.judge(spec, lp) == expected


@pytest.mark.parametrize(("mode", "fake"), [("off", False), ("shadow", False), ("on", True), ("bogus", False)])
def test_modes(mode: str, fake: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A FLAC only lossprint calls lossy: shadow (the default) logs both checks and keeps the spectrum check's
    verdict, on applies the combined one (with lossprint's source), off doesn't run lossprint at all."""
    f = tmp_path / "song.flac"
    f.write_bytes(b"x")
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ECHOLOT_LOSSPRINT", mode)
    monkeypatch.setattr(audio, "stream", lambda path, field: {"codec_name": "flac", "sample_rate": "44100"}[field])
    monkeypatch.setattr(audio, "_flac_ok", lambda path: True)
    monkeypatch.setattr(audio, "damage", lambda path: "")
    monkeypatch.setattr(audio, "spectrum", lambda path: {"verdict": "ok", "cutoff_hz": 20000, "drop_db": 21.0})
    judged: list[Path] = []
    monkeypatch.setattr(
        audio, "lossprint", lambda path: judged.append(path) or {"p": 1.0, "encoder": "opus", "kbps": 160}
    )
    got = audio.prepare(f)
    assert got.fake is fake
    assert judged == ([] if mode == "off" else [f])
    assert (got.spectrum or {}).get("source") == ("Opus ~160 kbps" if fake else None)
    log = tmp_path / "lossy-checks.jsonl"
    if mode == "off":
        assert not log.exists()
        return
    line = json.loads(log.read_text())
    assert line["mode"] == ("shadow" if mode == "bogus" else mode) and line["path"] == str(f)
    assert line["lossprint"] == {"p": 1.0, "encoder": "opus", "kbps": 160}
    assert (line["spectrum_fake"], line["combined_fake"], line["applied_fake"]) == (False, True, fake)
