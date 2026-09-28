import importlib.util
import json
from pathlib import Path

import pytest

from echolot.matching import artist_key, artist_keys, same_length, title_key


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


PIPELINE = Path("/opt/sockseek/config")  # real song data
SCRIPTS = Path(__file__).parents[1] / "pipeline" / "config" / "scripts"  # the rules' source


@pytest.mark.skipif(
    not PIPELINE.exists(), reason="needs the music-sync pipeline data on this machine"
)
def test_parity_with_pipeline() -> None:
    """The port gives exactly what the pipeline's library.py gives, on every wanted song and library file."""
    spec = importlib.util.spec_from_file_location("pipeline_library", SCRIPTS / "library.py")
    assert spec and spec.loader
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)

    from echolot import matching

    songs = [
        s
        for f in PIPELINE.glob("state/spotify-spotify-*.json")
        if not f.stem.endswith("-history")
        for s in json.loads(f.read_text())
    ]
    files = [
        p.removeprefix("/music/tracks/")
        for p in json.loads((PIPELINE / "state/library-cache.json").read_text())
    ]
    assert len(songs) > 1000 and len(files) > 1000
    titles = [s["title"] for s in songs] + [f.rsplit(".", 1)[0].split(" - ", 1)[-1] for f in files]
    artists = [a for s in songs for a in s.get("artists") or [s["artist"]]] + [
        f.split("/")[0] for f in files
    ]
    for t in titles:
        for name in ("title_key", "clean_name", "mix_cut", "feat_keys"):
            assert getattr(matching, name)(t) == getattr(old, name)(t), (name, t)
    for a in artists:
        for name in ("artist_keys", "first_artist", "fold"):
            assert getattr(matching, name)(a) == getattr(old, name)(a), (name, a)


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
