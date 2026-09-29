"""The audio check (identity.py): a download's fingerprint against the release's preview."""

from pathlib import Path

import numpy as np
import pytest

from echolot import db, identity


def noisy(fp: np.ndarray, share: float, rng: np.random.Generator) -> np.ndarray:
    """fp with about 1 - share of its bits flipped (a lossy copy of the same audio)."""
    flips = rng.random((len(fp), 32)) > share
    return fp ^ np.packbits(flips, axis=1, bitorder="little").view("<u4").ravel()


@pytest.fixture
def con(tmp_path: Path):
    db.init(tmp_path / "e.db")
    c = db.connect(tmp_path / "e.db")
    yield c
    c.close()


def test_similarity_finds_the_preview_inside_the_song() -> None:
    rng = np.random.default_rng(1)
    song = rng.integers(0, 2**32, 1600, dtype=np.uint32)
    preview = noisy(song[400:621], 0.93, rng)
    assert identity.similarity(preview, song) > 0.9
    assert identity.similarity(preview, rng.integers(0, 2**32, 1600, dtype=np.uint32)) < 0.6
    assert identity.similarity(preview, song[:100]) == 0.0  # shorter than the preview


def test_check(con, monkeypatch: pytest.MonkeyPatch) -> None:
    rng = np.random.default_rng(2)
    song, other = rng.integers(0, 2**32, (2, 1600), dtype=np.uint32)
    monkeypatch.setattr(identity, "reference", lambda con, isrc: noisy(song[100:321], 0.93, rng))
    monkeypatch.setattr(identity.audio, "read_isrc", lambda p: "")
    monkeypatch.setattr(identity, "fingerprint", lambda p: song)
    assert identity.check(con, "NLF712605912", Path("x.flac")).verdict == "same"
    monkeypatch.setattr(identity, "fingerprint", lambda p: other)
    assert identity.check(con, "NLF712605912", Path("x.flac")).verdict == "other"
    assert identity.check(con, "NLF712605912", Path("x.flac"), any_length=True).verdict == ""  # a DJ-mix cut
    assert identity.check(con, "", Path("x.flac")) == identity.UNKNOWN
    monkeypatch.setattr(identity.audio, "read_isrc", lambda p: "nl-f71-26-05912")
    assert identity.check(con, "NLF712605912", Path("x.flac")).detail == "ISRC tag of the release"


def test_reference_is_cached(con, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def get(url: str, raw: bool = False) -> object:
        calls.append(url)
        if raw:
            return b"mp3"
        return {"id": 1, "duration": 200, "preview": "https://p"} if "isrc:A" in url else {"error": {"code": 800}}

    monkeypatch.setattr(identity, "_get", get)
    monkeypatch.setattr(identity, "fingerprint", lambda source: np.arange(5, dtype="<u4"))
    assert identity.reference(con, "A").tolist() == [0, 1, 2, 3, 4]
    assert identity.reference(con, "A").tolist() == [0, 1, 2, 3, 4] and len(calls) == 2  # the second from refs
    assert identity.reference(con, "B") is None and identity.reference(con, "B") is None
    assert len(calls) == 3  # Deezer does not know B: asked again in a week
