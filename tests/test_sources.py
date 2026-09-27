from pathlib import Path

import pytest
import yaml

from echolot import db, schedule, sources
from echolot.config import Settings

REAL = Path("/opt/sockseek/config/sources.yml")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://open.spotify.com/playlist/6OmeR7AmtBE9H4zeRzBGcQ?si=30b2",
         ("spotify", "https://open.spotify.com/playlist/6OmeR7AmtBE9H4zeRzBGcQ")),
        ("https://open.spotify.com/intl-de/playlist/AbC123",
         ("spotify", "https://open.spotify.com/playlist/AbC123")),
        ("spotify:playlist:AbC123", ("spotify", "https://open.spotify.com/playlist/AbC123")),
        ("https://soundcloud.com/david_lost/sets/random-shit?utm=x",
         ("soundcloud", "https://soundcloud.com/david_lost/sets/random-shit")),
        ("soundcloud.com/someone/likes", ("soundcloud", "https://soundcloud.com/someone/likes")),
    ],
)  # fmt: skip
def test_parse_url(url: str, expected: tuple[str, str]) -> None:
    assert sources.parse_url(url) == expected


@pytest.mark.parametrize(
    "url",
    ["https://open.spotify.com/collection/tracks", "https://soundcloud.com/someone",
     "https://on.soundcloud.com/abc", "https://example.com/playlist/1"],
)  # fmt: skip
def test_parse_url_rejects(url: str) -> None:
    with pytest.raises(sources.ConfigError):
        sources.parse_url(url)


def test_add_update_remove(pipeline_dir: Path) -> None:
    text = sources.read(pipeline_dir)
    added = sources.add_list(text, "https://open.spotify.com/playlist/NEW1?si=1")
    data = yaml.safe_load(added)
    assert data["spotify"]["playlists"][-1] == "https://open.spotify.com/playlist/NEW1"
    assert "# comment" in added  # comments survive
    with pytest.raises(sources.ConfigError, match="already"):
        sources.add_list(added, "https://open.spotify.com/playlist/NEW1")
    renamed = sources.update_list(added, "spotify:playlist:NEW1", "My name", False)
    assert yaml.safe_load(renamed)["spotify"]["playlists"][-1] == {
        "url": "https://open.spotify.com/playlist/NEW1", "title": "My name", "playlist": False
    }  # fmt: skip
    plain = sources.update_list(renamed, "spotify:playlist:NEW1", "", True)
    assert plain == added
    assert sources.remove_list(added, "spotify:playlist:NEW1") == text
    sc = sources.add_list(text, "https://soundcloud.com/other/sets/techno", "Techno")
    assert yaml.safe_load(sc)["soundcloud"]["playlists"][-1]["title"] == "Techno"


def test_likes_and_options(pipeline_dir: Path) -> None:
    text = sources.read(pipeline_dir)
    off = sources.set_likes(text, "spotify", False)
    off = sources.set_likes(off, "soundcloud", True, "newuser")
    off = sources.set_removed_playlists(off, False)
    state = sources.likes_state(off)
    assert state == {"spotify": False, "soundcloud": True, "soundcloud_user": "newuser",
                     "removed_playlists": False}  # fmt: skip
    with pytest.raises(sources.ConfigError):
        sources.set_likes(text, "soundcloud", True, "")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("spotfy: {}", "Unknown setting"),
        ("spotify:\n  playlists:\n    - https://soundcloud.com/a/sets/b", "not a spotify URL"),
        ("spotify:\n  playlists:\n    - https://open.spotify.com/playlist/A\n"
         "    - https://open.spotify.com/playlist/A?si=2", "Listed twice"),
        ("soundcloud:\n  likes: true", "needs soundcloud.user"),
        ("spotify:\n  playlists:\n    - url: https://open.spotify.com/playlist/A\n"
         "      playlist: maybe", "true or false"),
        ("removed_playlists: 3", "true or false"),
    ],
)  # fmt: skip
def test_check_rejects(text: str, message: str) -> None:
    with pytest.raises(sources.ConfigError, match=message):
        sources.check(yaml.safe_load(text))


def test_save(settings: Settings) -> None:
    root = settings.pipeline_dir
    con = db.connect(settings.db_path)
    text = sources.read(root)
    new = sources.add_list(text, "https://open.spotify.com/playlist/NEW1")
    with pytest.raises(sources.ConfigError, match="YAML"):
        sources.save(con, root, "spotify: [", sources.version(text), "x")
    with pytest.raises(sources.Conflict):
        sources.save(con, root, new, "stale", "x")
    sources.save(con, root, new, sources.version(text), "added NEW1")
    assert sources.read(root) == new
    assert not list(root.glob(".*tmp"))
    [v] = sources.versions(con)
    assert v["note"] == "added NEW1"
    assert sources.old_version(con, v["id"]) == text


@pytest.mark.skipif(not REAL.exists(), reason="needs the real sources.yml")
def test_real_file_round_trip() -> None:
    """Edits on the real file keep everything else, comments and blank lines included."""
    text = REAL.read_text(encoding="utf-8")
    for url in ("https://open.spotify.com/playlist/ZZZ999",
                "https://soundcloud.com/someone/sets/zzz"):  # fmt: skip
        service, canonical = sources.parse_url(url)
        key = sources._key(service, canonical)
        added = sources.add_list(text, url)
        assert added.count("\n") == text.count("\n") + 1
        assert sources.remove_list(added, key) == text
    first = sources.entries(text)[0]["key"]
    assert sources.update_list(text, first, "", True) == text
    last = sources.entries(text)[-1]["key"]
    removed = sources.remove_list(text, last)
    assert "removed_playlists: true" in removed and "keep their files" in removed


def test_schedule(settings: Settings) -> None:
    root = settings.pipeline_dir
    assert schedule.read(root) == {j.name: j.default for j in schedule.JOBS}
    (root / "schedule.yml").write_text(
        "sync: 3\nfallback: off\nplaylists: nonsense\nupgrade:\n  at: ['sun 9:05', '21:00']\n"
    )
    values = schedule.read(root)
    assert (values["sync"], values["fallback"], values["playlists"]) == (10, None, 10)
    assert values["upgrade"] == ["sun 09:05", "21:00"]
    con = db.connect(settings.db_path)
    with pytest.raises(sources.ConfigError, match="at least"):
        schedule.save(con, root, {**values, "soundcloud": 5})
    schedule.save(con, root, {**values, "sync": 45, "sweep": ["sat,sun 15:00", "20:00"]})
    again = schedule.read(root)
    assert (again["sync"], again["sweep"], again["fallback"]) == (
        45,
        ["sat,sun 15:00", "20:00"],
        None,
    )
    written = (root / "schedule.yml").read_text()
    assert 'at: ["sat,sun 15:00", "20:00"]' in written and "fallback: off" in written
    (root / "state" / "last-sync").write_text("1790000000\n")
    status = {s.job.name: s for s in schedule.status(root)}
    assert status["sync"].next_run is not None and status["fallback"].next_run is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [("30", 30), ("off", None), ("", None), ("20:00", ["20:00"]),
     ("20:00; Sat,Sun 9:30", ["20:00", "sat,sun 09:30"])],
)  # fmt: skip
def test_parse_when(text: str, expected: object) -> None:
    assert schedule.parse_when(text, schedule.BY_NAME["upgrade"] if not text.isdigit()
                               else schedule.BY_NAME["sync"]) == expected  # fmt: skip


@pytest.mark.parametrize("text", ["5", "25:00", "someday 10:00", "noon"])
def test_parse_when_rejects(text: str) -> None:
    with pytest.raises(sources.ConfigError):
        schedule.parse_when(text, schedule.BY_NAME["sync"])


def test_next_time() -> None:
    from datetime import datetime

    sunday_night = datetime(2026, 9, 27, 21, 0)  # a Sunday
    assert schedule.next_time(["20:00", "sat,sun 15:00"], sunday_night) == datetime(
        2026, 9, 28, 20, 0
    )
    saturday_noon = datetime(2026, 9, 26, 12, 0)
    assert schedule.next_time(["20:00", "sat,sun 15:00"], saturday_noon) == datetime(
        2026, 9, 26, 15, 0
    )
