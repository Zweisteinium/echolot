"""lossprint, the second lossy check: its call, the combined verdict, on and off, and the library's check."""

import json
import subprocess
from pathlib import Path

import pytest

from echolot import db
from echolot.config import Settings
from echolot.jobs import lossycheck, schedule
from echolot.jobs.worker import Run
from echolot.library import audio, catalog, upload
from echolot.settings.vault import Vault

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


OK, LOSSY = {"verdict": "ok", "cutoff_hz": 20000}, {"verdict": "lossy", "cutoff_hz": 16000, "source": "~128 kbps"}
TRACES = {"verdict": "lossy", "cutoff_hz": 20300, "source": "~320 kbps"}  # an edge from TRACES_FROM up: traces only


def lp(p: float, encoder: str = "mp3", kbps: float | None = 320.0) -> dict:
    return {"p": p, "encoder": encoder, "kbps": kbps}


@pytest.mark.parametrize(
    ("spec", "lp_", "expected"),
    [
        (OK, lp(0.9), ("lossy", "MP3 ~320 kbps")),  # lossprint alone from LOSSPRINT_FAKE
        (OK, lp(0.97, "ffmpeg_aac", None), ("lossy", "AAC")),
        (OK, lp(0.8999), ("doubtful", "")),  # between the two: a label, no decision
        (OK, lp(0.5), ("doubtful", "")),
        (OK, lp(0.4999), ("ok", "")),
        (LOSSY, lp(0.0001), ("lossy", "~128 kbps")),  # the spectrum check's verdict stands, whatever lossprint says
        (TRACES, lp(0.0), ("lossy", "~320 kbps")),  # also on the encoder's traces alone
        (OK, {"error": "audio is shorter than the 2-second minimum"}, ("ok", "")),  # the spectrum check alone
        (LOSSY, None, ("lossy", "~128 kbps")),
        ({"verdict": "unknown"}, None, ("ok", "")),
    ],
)
def test_judge(spec: dict, lp_: dict | None, expected: tuple[str, str]) -> None:
    """Both checks together: lossy, doubtful or ok, with the likely source of a lossy one."""
    assert audio.judge(spec, lp_) == expected


# recordings of Spotify's lossless stream (Mitschnitt, bit-exact, 2026-10-16) as the checks saw them: the
# release's own audio, whatever its verdict (a lossy one is a lossy master)
SPOTIFY = [  # song, spectrum (verdict, edge), lossprint (p, encoder, kbps), verdict
    ("Taylor Swift - Everything Has Changed", ("ok", 20400), (1.6e-07, "musepack", 367.6), "ok"),
    ("Turno - Killer VIP", ("ok", 17500), (4.2e-05, "ffmpeg_aac", 463.1), "ok"),
    ("keshi - i swear i'll never leave again", ("ok", 12400), (0.9999841, "ffmpeg_aac", 258.1), "lossy"),
    ("mxmtoon - cliché", ("ok", 20400), (1.0, "ffmpeg_aac", 299.5), "lossy"),
    ("Sydney Rose - Novels", ("ok", 15900), (0.9999924, "ffmpeg_aac", 207.0), "lossy"),
    ("Zeph - Forever & Always", ("lossy", 18900), (1.0, "ffmpeg_aac", 255.0), "lossy"),
    ("Pink Sweat$ - Cocaine", ("ok", 12000), (0.9681299, "mp3", 322.5), "lossy"),
    ("Hedex - Clara", ("ok", 19500), (0.9998949, "mp3", 277.6), "lossy"),
    ("Indivision - Untold Words", ("ok", 19900), (0.9999996, "mp3", 297.5), "lossy"),
    ("FindMyName - Rebel", ("ok", 19800), (0.9999883, "mp3", 289.1), "lossy"),
]


@pytest.mark.parametrize(("song", "spec", "lp_", "verdict"), SPOTIFY, ids=[s[0] for s in SPOTIFY])
def test_spotify_masters(song: str, spec: tuple, lp_: tuple, verdict: str) -> None:
    """Spotify's own masters: two clean, eight made from lossy files (their library copies are bit-identical to
    six of them): lossy says made from lossy, not that a better copy exists."""
    assert audio.judge({"verdict": spec[0], "cutoff_hz": spec[1]}, lp(*lp_))[0] == verdict


@pytest.mark.parametrize(("mode", "fake"), [("off", False), ("on", True), ("", True)])
def test_on_and_off(mode: str, fake: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A FLAC only lossprint calls lossy: on (the default) it is made from lossy (lossprint's source), both checks
    logged; off lossprint doesn't run at all."""
    f = tmp_path / "song.flac"
    f.write_bytes(b"x")
    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(tmp_path))
    if mode:
        monkeypatch.setenv("ECHOLOT_LOSSPRINT", mode)
    else:
        monkeypatch.delenv("ECHOLOT_LOSSPRINT", raising=False)
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
    assert line["path"] == str(f) and line["verdict"] == "lossy"
    assert line["lossprint"] == {"p": 1.0, "encoder": "opus", "kbps": 160}


def test_a_lossprint_source_is_worth_its_bitrate() -> None:
    """A fake FLAC lossprint judged counts as its source's bitrate against another copy (upload.compare)."""
    assert upload.worth("fake", 900, "AAC ~258 kbps") == 258
    assert upload.worth("fake", 900, "~256 kbps / V0") == 256  # the spectrum check's
    assert upload.worth("fake", 900, "AAC") == 128  # (no bitrate guessed)


def test_the_library_check(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every library FLAC is checked once per audio (a retag is not checked again) and its verdict applied: lossy
    marks, ok and doubtful unmark, a lossy master stays."""
    tracks = settings.library_dir
    for rel in (
        "Artist A/Artist A - Clean.flac",
        "Artist C/Artist C - Opus Rip.flac",
        "Artist D/Artist D - Master.flac",
    ):
        (tracks / rel).parent.mkdir(parents=True, exist_ok=True)
        (tracks / rel).write_bytes(b"0123456789")
    con = db.connect(settings.db_path)
    with con:
        con.execute(
            "INSERT INTO lossy_sourced (stem, source, master) VALUES ('Artist D/Artist D - Master', 'MP3 ~290 kbps', 1)"
        )
    catalog.refresh(con, tracks)
    con.close()
    judged = {  # Second Song: marked made from ~128 kbps when filed, found clean now
        "Artist B - Second Song.flac": (
            {"verdict": "ok", "cutoff_hz": 21000},
            {"p": 0.01, "encoder": "mp3", "kbps": 300},
        ),
        "Artist A - Clean.flac": ({"verdict": "ok", "cutoff_hz": 21000}, {"p": 0.6, "encoder": "mp3", "kbps": 300}),
        "Artist C - Opus Rip.flac": ({"verdict": "ok", "cutoff_hz": 20000}, {"p": 1.0, "encoder": "opus", "kbps": 128}),
        "Artist D - Master.flac": ({"verdict": "ok", "cutoff_hz": 19800}, {"p": 1.0, "encoder": "mp3", "kbps": 290}),
    }
    checked: list[str] = []
    monkeypatch.setattr(audio, "spectrum", lambda path: judged[path.name][0])
    monkeypatch.setattr(
        audio, "lossprint", lambda path, background=False: checked.append(path.name) or judged[path.name][1]
    )
    monkeypatch.setattr(audio, "streaminfo_md5", lambda path: "md5-" + path.name)
    vault = Vault.from_env(settings.data_dir, {})

    def run_it(mode: str) -> str:
        monkeypatch.setenv("ECHOLOT_LOSSPRINT", mode)
        return lossycheck.run(Run(schedule.BY_NAME["lossy"], settings, vault, "schedule"))

    def marks() -> dict[str, tuple[str, int]]:
        con = db.connect(settings.db_path)
        try:
            return {r[0]: (r[1], r[2]) for r in con.execute("SELECT stem, source, master FROM lossy_sourced")}
        finally:
            con.close()

    assert run_it("on") == "2 lossy, 1 doubtful, 1 ok; marked 1 made from lossy, unmarked 1"
    assert run_it("on") == "nothing to do" and len(checked) == 4  # (none checked again)
    assert marks() == {
        "Artist C/Artist C - Opus Rip": ("Opus ~128 kbps", 0),
        "Artist D/Artist D - Master": ("MP3 ~290 kbps", 1),
    }
    con = db.connect(settings.db_path)
    quality = dict(con.execute("SELECT path, quality FROM files WHERE path LIKE '%.flac'").fetchall())
    con.close()
    assert (
        quality["Artist C/Artist C - Opus Rip.flac"] == "fake"
        and quality["Artist B/Artist B - Second Song.flac"] == "lossless"
    )
    assert quality["Artist D/Artist D - Master.flac"] == "lossless"
    opus = tracks / "Artist C/Artist C - Opus Rip.flac"
    opus.write_bytes(b"0123456789 retagged")  # the same audio (STREAMINFO MD5)
    con = db.connect(settings.db_path)
    catalog.refresh(con, tracks)
    con.close()
    assert run_it("on") == "1 retagged" and len(checked) == 4
    assert run_it("off") == "off (ECHOLOT_LOSSPRINT)"
