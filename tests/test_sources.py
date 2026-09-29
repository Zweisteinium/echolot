import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from echolot import db, pipeline, pipeline_config, schedule, sources
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


@pytest.fixture
def con(settings: Settings) -> Iterator[sqlite3.Connection]:
    """A database with the pipeline's sources.yml and schedule.yml taken over."""
    c = db.connect(settings.db_path)
    yield c
    c.close()


def keys(con: sqlite3.Connection) -> list[str]:
    return [s.key for s in sources.lists(con)]


def test_take_over(con: sqlite3.Connection, settings: Settings) -> None:
    original = yaml.safe_load((settings.pipeline_dir / "sources.yml").read_text())
    original["spotify"]["playlists"][0] = "https://open.spotify.com/playlist/AAA111"  # canonical
    assert sources.as_config(con) == {**original, "removed_playlists": True}
    assert [s.key for s in pipeline.sources(original)] == keys(con)
    assert sources.parse(sources.render(con)) == sources.as_config(con)
    [v] = sources.versions(con)
    assert v["note"] == "Echolot took the file over" and "# comment" in sources.old_version(
        con, v["id"]
    )


def test_add_update_remove(con: sqlite3.Connection) -> None:
    before = sources.render(con)
    key = sources.add_list(con, "https://open.spotify.com/playlist/NEW1?si=1")
    assert key == "spotify:playlist:NEW1"
    assert [k for k in keys(con) if k.startswith("spotify:")][-1] == key
    assert (
        sources.as_config(con)["spotify"]["playlists"][-1]
        == "https://open.spotify.com/playlist/NEW1"
    )
    with pytest.raises(sources.ConfigError, match="already"):
        sources.add_list(con, "https://open.spotify.com/playlist/NEW1")
    sources.update_list(con, key, "My name", False)
    assert sources.as_config(con)["spotify"]["playlists"][-1] == {
        "url": "https://open.spotify.com/playlist/NEW1", "title": "My name", "playlist": False
    }  # fmt: skip
    sources.update_list(con, key, "", True)
    sources.remove_list(con, key)
    assert sources.render(con) == before
    with pytest.raises(sources.ConfigError, match="not in the sources"):
        sources.remove_list(con, key)
    sc = sources.add_list(con, "https://soundcloud.com/other/sets/techno", "Techno")
    assert sources.as_config(con)["soundcloud"]["playlists"][-1]["title"] == "Techno"
    assert sc == "soundcloud:other/sets/techno"
    notes = [v["note"] for v in sources.versions(con)]
    assert notes[:3] == ["added https://soundcloud.com/other/sets/techno",
                         "removed spotify:playlist:NEW1", "changed spotify:playlist:NEW1"]  # fmt: skip


def test_likes_and_options(con: sqlite3.Connection) -> None:
    sources.set_likes(con, "spotify", False)
    sources.set_likes(con, "soundcloud", True, "newuser")
    sources.set_removed_playlists(con, False)
    assert sources.likes_state(con) == {"spotify": False, "soundcloud": True,
                                        "soundcloud_user": "newuser", "removed_playlists": False}  # fmt: skip
    config = sources.as_config(con)
    assert config["spotify"]["likes"] is False and config["soundcloud"]["user"] == "newuser"
    assert "soundcloud:newuser/likes" in keys(con) and "spotify:likes" not in keys(con)
    with pytest.raises(sources.ConfigError):
        sources.set_likes(con, "soundcloud", True, "")
    sources.set_likes(con, "spotify", True)  # back on: the row kept its options
    assert keys(con)[0] == "spotify:likes"


def test_save_text(con: sqlite3.Connection) -> None:
    text = sources.render(con)
    with pytest.raises(sources.ConfigError, match="YAML"):
        sources.save_text(con, "spotify: [", sources.version(text), "x")
    new = text.replace("removed_playlists: true", "removed_playlists: false")
    with pytest.raises(sources.Conflict):
        sources.save_text(con, new, "stale", "x")
    sources.save_text(con, new, sources.version(text), "edited")
    assert sources.render(con) == new
    assert sources.old_version(con, sources.versions(con)[0]["id"]) == text
    sources.save_text(con, new + "# only a comment\n", sources.version(new), "same")
    assert sources.versions(con)[0]["note"] == "edited"  # nothing changed: no version


def test_write_files(con: sqlite3.Connection, settings: Settings) -> None:
    root = settings.pipeline_dir  # written at the start already
    assert (root / "sources.yml").read_text() == sources.render(con)
    assert schedule.read_file(root) == schedule.rules(con)
    assert pipeline_config.write(con, settings) == []  # unchanged: not written again
    (root / "sources.yml").write_text("spotify:\n  likes: false\n")  # someone edits the file
    assert pipeline_config.write(con, settings) == ["sources.yml"]
    assert sources.versions(con)[0]["note"] == "Echolot replaced an edit made outside it"
    assert not list(root.glob(".*tmp"))


def test_take_over_refuses_invalid_file(bare_settings: Settings) -> None:
    root = bare_settings.pipeline_dir
    (root / "sources.yml").write_text("spotfy: {}\n")
    con = db.connect(bare_settings.db_path)
    pipeline_config.start(con, bare_settings)
    assert (root / "sources.yml").read_text() == "spotfy: {}\n"  # left alone
    assert not db.get_meta(con, pipeline_config.TAKEN_OVER)
    con.close()


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


@pytest.mark.skipif(not REAL.exists(), reason="needs the real sources.yml")
def test_real_file_round_trip(bare_settings: Settings) -> None:
    """The real sources.yml, taken over and written again, means the same to the pipeline."""
    text = REAL.read_text(encoding="utf-8")
    (bare_settings.pipeline_dir / "sources.yml").write_text(text)
    con = db.connect(bare_settings.db_path)
    pipeline_config.take_over(con, bare_settings.pipeline_dir)
    original, rendered = yaml.safe_load(text), yaml.safe_load(sources.render(con))
    assert pipeline.sources(rendered) == pipeline.sources(original)
    assert rendered.get("removed_playlists", True) == original.get("removed_playlists", True)
    assert (rendered.get("soundcloud") or {}).get("user") == (original.get("soundcloud") or {}).get(
        "user"
    )
    con.close()


def test_schedule(bare_settings: Settings) -> None:
    settings = bare_settings
    root = settings.pipeline_dir
    assert schedule.read_file(root) == {j.name: j.default for j in schedule.JOBS}
    (root / "schedule.yml").write_text(
        "sync: 3\nfallback: off\nplaylists: nonsense\nupgrade:\n  at: ['sun 9:05', '21:00']\n"
    )
    con = db.connect(settings.db_path)
    pipeline_config.take_over(con, root)
    values = schedule.rules(con)
    assert (values["sync"], values["fallback"], values["playlists"]) == (10, None, 10)
    assert values["upgrade"] == ["sun 09:05", "21:00"]
    with pytest.raises(sources.ConfigError, match="at least"):
        schedule.save(con, {"soundcloud": 5})
    schedule.save(con, {"sync": 45, "sweep": ["sat,sun 15:00", "20:00"]})
    again = schedule.rules(con)
    assert (again["sync"], again["sweep"], again["fallback"]) == (
        45,
        ["sat,sun 15:00", "20:00"],
        None,
    )
    pipeline_config.write(con, settings)
    written = (root / "schedule.yml").read_text()
    assert 'at: ["sat,sun 15:00", "20:00"]' in written and "fallback: off" in written
    assert schedule.read_file(root) == again
    (root / "state" / "last-sync").write_text("1790000000\n")
    status = {s.job.name: s for s in schedule.status(root, again)}
    assert status["sync"].next_run is not None and status["fallback"].next_run is None
    con.close()


def test_parse_rules() -> None:
    assert schedule.parse_rules({"sync": 20, "fallback": "off", "sweep": {"at": ["20:00"]}}) == {
        "sync": 20, "fallback": None, "sweep": ["20:00"]
    }  # fmt: skip
    for bad in ({"nope": 5}, {"sync": 2}, {"sync": "often"}, {"upgrade": {"at": ["25:00"]}}, [1]):
        with pytest.raises(sources.ConfigError):
            schedule.parse_rules(bad)


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
