"""Rules of the pipeline's library.py (pipeline/config/scripts), on cases from real rejections."""

import importlib.util
import json
import sys
import types
import wave
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / "pipeline" / "config" / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"pipeline_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


library = load("library")


def probable(
    artist: str,
    title: str,
    found: str,
    *,
    file_name: str = "",
    dur: float = 200,
    length: float = 200,
):
    return library.probable_ok(artist, title, [artist], found, file_name, (), dur, length, 3)[0]


# (artist, requested title, tag title of the download): the same recording
RIGHT = [
    ("CAPO", "Run Run Run (feat. Yung Kafa & Kücük Efendi) - Remix",
     "CAPO - RUN RUN RUN feat. YUNG KAFA & KÜCÜK EFENDI (prod. von Jurijgold & Falconi) [Official Remix]"),
    ("AC/DC", "Hells Bells", "AC/DC - Hells Bells (Official 4K Video)"),
    ("Linkin Park", "BURN IT DOWN", "BURN IT DOWN (Official Music Video) [4K Upgrade] - Linkin Park"),
    ("Frank Ocean", "In My Room", "Frank Ocean - In My Room (Lyric Video)"),
    ("Mark Terre", "Raindrops (H369) - H369 Remix", "Raindrops (H369 Remix)"),
    ("Tream", "HERZBLATT (AUA AUA) - OsTEKKe & Zombic Remix", "Tream x Mia Julia - HERZBLATT (ZOMBIC x OSTEKKE REMIX)"),
    ("Jaspa", "Auge der Vorsehung", "Jaspa - Auge der Vorsehung | JCC 2020 | Qualifikation #17"),
    ("BODYWORX", "The Push Up Song", "The Push Up Song (Extended Mix)"),  # the length tells edits apart
    ("Stefan Stürmer", "Paradies - Abrissgebeat Remix", "Paradies (Abrissgebeat Remix)"),
]  # fmt: skip

# similar names, other songs or other recordings
WRONG = [
    ("HK", "Was!?!? (feat. OG Boobie Black & Sami Nasser)", "I Was Made for Dancing"),
    ("HK", "Was!?!? (feat. OG Boobie Black & Sami Nasser)", "Was Können Sie Dir Tun = What Can They Do To You"),
    ("Wu-Tang Clan", "Cream", "Strawberries & Cream (feat. Allah Real & Mathematics)"),
    ("5udo", "One", "One - 2017 Remake"),
    ("Nostrum", "Brilliant - Radio Edit", "Brilliant (Hard Trance mix)"),
    ("Oliver Schories", "Caprice", "Caprice (Midas 104 Extended Remix)"),
    ("The Weeknd", "The Hills - Remix", "The Hills (RL Grime Remix)"),
    ("Helge Schneider", "Mr. Bojangles - Live At The Grugahalle / 2014", "Mr. Bojangles"),
    ("Miksu / Macloud", "Nachts wach (Lila Wolken Bootleg)", "Nachts wach"),
    ("Swedish House Mafia", "Ray Of Solar - Tiësto Remix", "Ray Of Solar"),
    ("Marti Fischer", "Chabos wissen, wer der Babo ist - Swing / Jazz Version", "Chabos wissen wer der Babo ist"),
    ("Bachi", "Like U", "BACHI FORTE (SUPER SLOWED)"),
    ("Timmy Trumpet", "Mad World", "Cold"),
    ("Liquid Soul", "Levitate", "Levitate (OxiDaksi Remix)"),
    ("Some Artist", "Fire", "Fire II"),
    ("Some Artist", "Tale", "Tale Pt. 2"),
]  # fmt: skip


@pytest.mark.parametrize(("artist", "title", "found"), RIGHT)
def test_probable_accepts_same_recording(artist: str, title: str, found: str) -> None:
    assert probable(artist, title, found)


@pytest.mark.parametrize(("artist", "title", "found"), WRONG)
def test_probable_rejects_other_song_or_version(artist: str, title: str, found: str) -> None:
    assert not probable(artist, title, found)


def test_probable_needs_length_and_artist() -> None:
    capo = RIGHT[0]
    assert not probable(*capo, dur=210)  # 10 s off
    assert not probable(*capo, dur=0)  # unknown
    assert not library.probable_ok(
        "Other", capo[1], ["Other Artist"], capo[2], "", (), 200, 200, 3
    )[0]


def test_probable_from_file_name() -> None:
    assert probable("Hi-Rez", "Smiling", "", file_name="Hi-Rez_A Walk To Remember_13_Smiling")


def test_segments() -> None:
    head, tail = library._segments("Run Run Run feat. X (prod. Y) [Official Remix]")
    assert head == "Run Run Run"
    assert tail == ["feat. X", "prod. Y", "Official Remix"]


# ---------------------------------------------------------------- filing and review decisions
@pytest.fixture
def lib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name, sub in [("TRACKS", "tracks"), ("REPLACED", "inbox/replaced"), ("REVIEW", "inbox/review"),
                      ("STATE", "state"), ("EVENTS", "logs/downloads.jsonl")]:  # fmt: skip
        monkeypatch.setattr(library, name, tmp_path / sub)
    for name, file in [("LOCKFILE", "library.lock"), ("CACHE", "library-cache.json"),
                       ("LOSSY_LIST", "lossy-sourced.json"), ("BLOCKED", "review-blocked.json")]:  # fmt: skip
        monkeypatch.setattr(library, name, tmp_path / "state" / file)
    (tmp_path / "tracks").mkdir()
    return library


def download(tmp_path: Path, name: str, seconds: int = 200) -> Path:
    """A silent WAV of the given length (100 frames per second keeps it small)."""
    path = tmp_path / "inbox" / "sockseek" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(100)
        w.writeframes(b"\x80" * 100 * seconds)
    return path


def events(tmp_path: Path) -> list[dict]:
    lines = (tmp_path / "logs" / "downloads.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


CAPO = ("CAPO", "Run Run Run (feat. Yung Kafa & Kücük Efendi) - Remix")
CAPO_FILE = "CAPO - RUN RUN RUN feat. YUNG KAFA & KÜCÜK EFENDI (prod. von Jurijgold & Falconi) [Official Remix]"


def file_capo(lib, tmp_path: Path, relaxed: bool):
    return lib.file_into(download(tmp_path, "1.wav"), *CAPO, 200, "soulseek", ["spotify:capo"],
                         strict=True, file_name=CAPO_FILE, loose=True, relaxed=relaxed, tries=2)  # fmt: skip


def test_strict_rejection_is_kept_for_review(lib, tmp_path: Path) -> None:
    action, dest = file_capo(lib, tmp_path, relaxed=False)
    assert (action, dest) == ("wrong-song", None)
    e = events(tmp_path)[-1]
    assert e["action"] == "wrong-song" and e["found"] == CAPO_FILE and e["tries"] == 2
    kept = Path(e["path"])
    assert kept.is_file() and kept.is_relative_to(tmp_path / "inbox" / "review")
    assert e["seconds"] == 200


def test_relaxed_files_probable_match_with_review_mark(lib, tmp_path: Path) -> None:
    action, dest = file_capo(lib, tmp_path, relaxed=True)
    assert action == "new" and dest.is_file()
    assert dest.name == "CAPO - Run Run Run (feat. Yung Kafa & Kücük Efendi) - Remix.wav"
    e = events(tmp_path)[-1]
    assert e["match"] == "probable" and e["reason"].startswith("probable")


def test_review_wrong_retires_and_blocks(lib, tmp_path: Path) -> None:
    _, dest = file_capo(lib, tmp_path, relaxed=True)
    e = events(tmp_path)[-1]
    result, retry = lib.review_apply({"decision": "wrong", "song": "spotify:capo", "path": e["path"],
                                      "found": e["found"], "file_name": e["file_name"]})  # fmt: skip
    assert (result, retry) == ("retired", True)
    assert not dest.exists()
    # the same download is not taken again, even though it is a probable match
    assert file_capo(lib, tmp_path, relaxed=True) == ("wrong-song", None)
    assert events(tmp_path)[-1]["reason"] == "this download was marked wrong in review"


def test_review_accept_files_a_kept_download(lib, tmp_path: Path) -> None:
    file_capo(lib, tmp_path, relaxed=False)
    e = events(tmp_path)[-1]
    decision = {"decision": "accept", "song": "spotify:capo", "path": e["path"], "artist": CAPO[0],
                "title": CAPO[1], "length": 200, "source": "soulseek"}  # fmt: skip
    result, retry = lib.review_apply(decision)
    assert result.startswith("new CAPO/") and not retry
    assert events(tmp_path)[-1]["match"] == "review"
    assert lib.review_apply(decision) == ("file gone", False)


def test_review_discard_only_inside_review_folder(lib, tmp_path: Path) -> None:
    outside = download(tmp_path, "keep.wav")
    assert lib.review_apply({"decision": "discard", "path": str(outside)}) == ("file gone", False)
    assert outside.exists()
    file_capo(lib, tmp_path, relaxed=False)
    kept = events(tmp_path)[-1]["path"]
    assert lib.review_apply({"decision": "discard", "path": kept}) == ("deleted", False)
    assert not Path(kept).exists()


def test_search_title(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "spectrum", types.ModuleType("spectrum"))  # needs numpy
    monkeypatch.setitem(sys.modules, "library", library)
    sync = load("music-sync")
    assert sync.search_title("Adagio for Strings - Unmixed Version") == "Adagio for Strings"
    assert sync.search_title("Brilliant - Radio Edit") == "Brilliant"
    assert sync.search_title('Lose Yourself - From "8 Mile" Soundtrack') == "Lose Yourself"
    assert sync.search_title("Run Run Run (feat. Yung Kafa) - Remix") == "Run Run Run - Remix"
    assert sync.search_title("Paradies - Abrissgebeat Remix") == "Paradies - Abrissgebeat Remix"
    assert sync.search_title("Sunrise - 2011 Remaster") == "Sunrise"
