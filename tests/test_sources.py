import sqlite3
from collections.abc import Iterator
from datetime import datetime

import pytest
import yaml

from echolot import db
from echolot.config import Settings
from echolot.jobs import lists, schedule
from echolot.settings import auth, sources

OWNER = 1  # conftest: the owner of the small collection


@pytest.fixture
def con(settings: Settings) -> Iterator[sqlite3.Connection]:
    c = db.connect(settings.db_path)
    yield c
    c.close()


def keys(con: sqlite3.Connection) -> list[str]:
    return [s.key for s in sources.lists(con)]


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


def test_taken_over(con: sqlite3.Connection) -> None:
    assert keys(con) == ["spotify:likes:1", "spotify:playlist:AAA111", "spotify:playlist:BBB222",
                         "soundcloud:someone/likes", "soundcloud:someone/sets/trance"]  # fmt: skip
    names = [s.name for s in sources.lists(con)]
    assert names == ["Spotify Liked Songs", "spotify-AAA111", "spotify-BBB222", "SoundCloud Likes",
                     "soundcloud-someone-trance"]  # fmt: skip


def test_add_playlist_flag_remove(con: sqlite3.Connection) -> None:
    key = sources.add_list(con, OWNER, "https://open.spotify.com/playlist/NEW1?si=1", "", False)
    assert key == "spotify:playlist:NEW1"
    assert [k for k in keys(con) if k.startswith("spotify:")][-1] == key
    with pytest.raises(sources.ConfigError, match="already"):
        sources.add_list(con, OWNER, "https://open.spotify.com/playlist/NEW1")
    assert not next(s for s in sources.lists(con) if s.key == key).playlist
    sources.set_playlist(con, OWNER, key, True)
    assert next(s for s in sources.lists(con) if s.key == key).playlist
    lists.sync_table(con)
    assert con.execute("SELECT fetched FROM lists WHERE key = ?", (key,)).fetchone()[0] == 0
    sources.remove_list(con, OWNER, key)
    lists.sync_table(con)
    assert key not in keys(con) and not con.execute("SELECT 1 FROM lists WHERE key = ?", (key,)).fetchone()
    with pytest.raises(sources.ConfigError, match="don't follow"):
        sources.remove_list(con, OWNER, key)


def test_two_users_follow_a_list(con: sqlite3.Connection) -> None:
    """Each follows with their own options; the list is one (fetched once, its state stays while anyone
    follows it); one user's change never touches the other's lists."""
    timon = auth.logged_in(con, "timon", "nd-timon", False).id
    sources.add_list(con, timon, "https://open.spotify.com/playlist/AAA111", "", False)
    sources.set_likes(con, timon, "spotify", True)
    assert sources.followers(con, "spotify:playlist:AAA111") == [OWNER, timon]
    assert [s.key for s in sources.followed(con)].count("spotify:playlist:AAA111") == 1
    mine = {s.key: s.playlist for s in sources.user_lists(con, timon)}
    assert mine == {"spotify:likes:2": True, "spotify:playlist:AAA111": False}  # their own likes, their own mode
    assert next(s for s in sources.user_lists(con, OWNER) if s.key == "spotify:playlist:AAA111").playlist
    sources.remove_list(con, OWNER, "spotify:playlist:AAA111")
    lists.sync_table(con)
    assert con.execute("SELECT 1 FROM lists WHERE key = 'spotify:playlist:AAA111'").fetchone()  # timon's still
    assert "spotify:playlist:AAA111" not in [s.key for s in sources.user_lists(con, OWNER)]
    with pytest.raises(sources.ConfigError, match="don't follow"):
        sources.remove_list(con, timon, "spotify:playlist:BBB222")  # the owner's, not timon's


def test_adopt(con: sqlite3.Connection, settings: Settings) -> None:
    """The lists from before users had lists go to the oldest admin with everything that was the one
    account's; a second run changes nothing."""
    from echolot.settings.vault import Vault

    vault = Vault.from_env(settings.data_dir, {})
    with con:  # as version 19 leaves them: nobody's, the old likes key, the one account's logins
        con.execute("UPDATE sources SET user_id = NULL")
        con.execute("UPDATE sources SET key = 'spotify:likes' WHERE key = 'spotify:likes:1'")
        copy = "SELECT 'spotify:likes', service, title, position FROM lists WHERE key = 'spotify:likes:1'"
        con.execute(f"INSERT INTO lists (key, service, title, position) {copy}")
        con.execute("UPDATE list_songs SET list_key = 'spotify:likes' WHERE list_key = 'spotify:likes:1'")
        con.execute("UPDATE list_history SET list_key = 'spotify:likes' WHERE list_key = 'spotify:likes:1'")
        con.execute("DELETE FROM lists WHERE key = 'spotify:likes:1'")
        con.execute("UPDATE users SET soundcloud_user = ''")
        vault.set(con, "spotify.refresh_token", "refresh")
        vault.set(con, "soundcloud.token", "token")
    songs = con.execute("SELECT count(*) FROM list_songs").fetchone()[0]
    assert sources.adopt(con) == "5 lists and the accounts given to owner"
    assert keys(con)[0] == "spotify:likes:1" and sources.owners(con) == [OWNER]
    assert con.execute("SELECT count(*) FROM list_songs WHERE list_key = 'spotify:likes:1'").fetchone()[0] == 3
    assert con.execute("SELECT count(*) FROM list_songs").fetchone()[0] == songs
    assert not con.execute("SELECT 1 FROM lists WHERE key = 'spotify:likes'").fetchone()
    assert vault.get(con, "spotify.refresh_token:1") == "refresh" and vault.get(con, "soundcloud.token:1") == "token"
    assert sources.soundcloud_user(con, OWNER) == "someone"
    assert sources.adopt(con) is None


def test_remove_keeps_songs_and_history(con: sqlite3.Connection) -> None:
    songs = con.execute("SELECT count(*) FROM songs").fetchone()[0]
    sources.remove_list(con, OWNER, "spotify:playlist:AAA111")
    lists.sync_table(con)
    assert con.execute("SELECT count(*) FROM songs").fetchone()[0] == songs
    assert con.execute("SELECT count(*) FROM list_history WHERE list_key = 'spotify:playlist:AAA111'").fetchone()[0]
    assert not con.execute("SELECT 1 FROM list_songs WHERE list_key = 'spotify:playlist:AAA111'").fetchone()


def test_likes(con: sqlite3.Connection) -> None:
    sources.set_likes(con, OWNER, "spotify", False)
    sources.set_likes(con, OWNER, "soundcloud", True, "newuser")
    likes = dict(con.execute("SELECT service, enabled FROM sources WHERE likes = 1").fetchall())
    assert likes == {"spotify": 0, "soundcloud": 1}
    assert sources.soundcloud_user(con, OWNER) == "newuser"
    assert "soundcloud:newuser/likes" in keys(con) and "spotify:likes:1" not in keys(con)
    with pytest.raises(sources.ConfigError):
        sources.set_likes(con, OWNER, "soundcloud", True, "")
    sources.set_likes(con, OWNER, "spotify", True)
    assert keys(con)[0] == "spotify:likes:1"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("spotfy: {}", "Unknown setting"),
        ("spotify:\n  playlists:\n    - https://soundcloud.com/a/sets/b", "not a spotify URL"),
        ("spotify:\n  playlists:\n    - https://open.spotify.com/playlist/A\n"
         "    - https://open.spotify.com/playlist/A?si=2", "Listed twice"),
        ("soundcloud:\n  likes: true", "needs soundcloud.user"),
        ("removed_playlists: 3", "true or false"),
    ],
)  # fmt: skip
def test_check_rejects(text: str, message: str) -> None:
    with pytest.raises(sources.ConfigError, match=message):
        sources.check(yaml.safe_load(text))


def test_schedule(con: sqlite3.Connection) -> None:
    assert schedule.rules(con)["library"] == 5
    with pytest.raises(sources.ConfigError, match="at least"):
        schedule.save(con, {"soundcloud": 1})  # at least every 2 min
    schedule.save(con, {"sync": 45, "sweep": ["sat,sun 15:00", "20:00"]})
    rules = schedule.rules(con)
    assert (rules["sync"], rules["sweep"], rules["fallback"]) == (45, ["sat,sun 15:00", "20:00"], 120)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("30", 30), ("off", None), ("", None), ("20:00", ["20:00"]), ("20:00; Sat,Sun 9:30", ["20:00", "sat,sun 09:30"])],
)
def test_parse_when(text: str, expected: object) -> None:
    job = schedule.BY_NAME["upgrade"] if not text.isdigit() else schedule.BY_NAME["sync"]
    assert schedule.parse_when(text, job) == expected


@pytest.mark.parametrize("text", ["5", "25:00", "someday 10:00", "noon"])
def test_parse_when_rejects(text: str) -> None:
    with pytest.raises(sources.ConfigError):
        schedule.parse_when(text, schedule.BY_NAME["fallback"])  # at least every 60 min


def test_parse_rules() -> None:
    assert schedule.parse_rules({"sync": 20, "fallback": "off", "sweep": {"at": ["20:00"]}}) == {
        "sync": 20, "fallback": None, "sweep": ["20:00"]
    }  # fmt: skip
    for bad in ({"nope": 5}, {"fallback": 2}, {"sync": "often"}, {"upgrade": {"at": ["25:00"]}}, [1]):
        with pytest.raises(sources.ConfigError):
            schedule.parse_rules(bad)


def test_due() -> None:
    sunday_night = datetime(2026, 9, 27, 21, 0)  # a Sunday
    times = ["20:00", "sat,sun 15:00"]
    assert schedule.next_time(times, sunday_night) == datetime(2026, 9, 28, 20, 0)
    assert schedule.next_time(times, datetime(2026, 9, 26, 12, 0)) == datetime(2026, 9, 26, 15, 0)
    assert schedule.due(times, datetime(2026, 9, 27, 19, 0), sunday_night)  # 20:00 passed since
    assert not schedule.due(times, datetime(2026, 9, 27, 20, 1), sunday_night)
    assert not schedule.due(times, None, datetime(2026, 9, 28, 3, 0))  # 20:00 is more than 6 h ago
    assert schedule.due(30, datetime(2026, 9, 27, 20, 29), sunday_night)
    assert not schedule.due(30, datetime(2026, 9, 27, 20, 31), sunday_night)
    assert schedule.due(30, None, sunday_night) and not schedule.due(None, None, sunday_night)
