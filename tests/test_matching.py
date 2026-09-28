import importlib.util
import json
from pathlib import Path

import pytest

from echolot.matching import artist_key, artist_keys, identity_ok, same_length, title_key


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Song (Original Mix)", "Song"),
        ("Song - Original Mix", "Song"),
        ("Song [HAK003]", "Song"),
        ("Song (feat. Somebody)", "Song"),
        ("Song feat. Somebody", "Song"),
        ("Song - 2011 Remaster", "Song"),
        ("Song (Free DL)", "Song"),
        ("Tale Pt. III", "Tale Part III"),
        ("Rock & Roll", "Rock and Roll"),
    ],
)
def test_same_title(a: str, b: str) -> None:
    assert title_key(a) == title_key(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Glow", "Glow - Nick Schwenderling Remix"),
        ("Fire", "Fire II"),
        ("Song", "Song - Radio Edit"),
        ("Song", "Song (VIP)"),
    ],
)
def test_different_title(a: str, b: str) -> None:
    assert title_key(a) != title_key(b)


def test_artist_keys() -> None:
    assert artist_keys("Above & Beyond") == {"aboveandbeyond", "above"}
    assert artist_key("Ke$ha") == artist_key("Kesha")
    assert artist_key("Røyksopp") == artist_key("Royksopp")


def test_same_length() -> None:
    assert same_length(200, 209)
    assert not same_length(200, 215)
    assert same_length(400, 415)  # 4 % of the longer one
    assert same_length(0, 123)  # unknown length


def test_identity() -> None:
    assert not identity_ok("TINOS", "All Night", ["Vanilla"], "All Night")[0]
    assert identity_ok("TINOS", "All Night", ["TINOS"], "All Night")[0]
    assert identity_ok("Hi-Rez", "Smiling", file_name="Hi-Rez_A Walk To Remember_13_Smiling")[0]
    edit = dict(tag_artists=["A"], tag_title="Song - Radio Edit")
    assert not identity_ok("A", "Song - Edit", **edit)[0]
    assert identity_ok("A", "Song - Edit", **edit, loose=True, length_close=True)[0]
    assert not identity_ok("A", "Song - Edit", **edit, loose=True, length_close=False)[0]


PIPELINE = Path("/opt/sockseek/config")


@pytest.mark.skipif(not PIPELINE.exists(), reason="needs the music-sync pipeline on this machine")
def test_parity_with_pipeline() -> None:
    """The port behaves exactly like the pipeline's library.py on the real song data."""
    spec = importlib.util.spec_from_file_location(
        "pipeline_library", PIPELINE / "scripts/library.py"
    )
    assert spec and spec.loader
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)

    from echolot import matching

    songs = json.loads((PIPELINE / "state/spotify-spotify-liked-songs.json").read_text())
    files = [
        p.removeprefix("/music/tracks/").rsplit(".", 1)[0]
        for p in json.loads((PIPELINE / "state/library-cache.json").read_text())
    ]
    assert len(songs) > 100 and len(files) > 100
    for s in songs:
        for name in ("title_key", "artist_keys", "first_artist", "clean_name", "fold"):
            value = s["title"] if name in ("title_key", "clean_name") else s["artist"]
            assert getattr(matching, name)(value) == getattr(old, name)(value), (name, value)
    for i, s in enumerate(songs):
        folder, _, stem = files[i % len(files)].partition("/")
        tag_title = stem.split(" - ", 1)[-1]
        for loose in (False, True):
            args = (s["artist"], s["title"], [folder], tag_title, stem, [folder], loose, True)
            assert matching.identity_ok(*args) == old.identity_ok(*args), args
            own = (s["artist"], s["title"], [s["artist"]], s["title"], "", [], loose, True)
            assert matching.identity_ok(*own) == old.identity_ok(*own), own


def test_catalog_song_other_artists_and_link() -> None:
    from echolot.library import Catalog, Entry

    cat = Catalog(
        [
            Entry("Mabe/Mabe - Atlantis.opus", 350, 160, False),
            Entry("Pbb Yea/Pbb Yea - Chilln.opus", 227, 160, False),
        ]
    )
    assert cat.song("Catch Vibe", "Atlantis", 349, ["Catch Vibe", "Mabe"])
    assert not cat.song("Catch Vibe", "Atlantis", 349)
    assert cat.song("TheDoDo", "Chilln", 227, ["TheDoDo"], ["Pbb Yea", "Chilln"])
    assert not cat.song("TheDoDo", "Chilln", 227, ["TheDoDo"])
