"""The likes lists' hearts in Navidrome (library/stars): given and taken back where the lists changed, never
the user's own."""

import io
import json
import sqlite3
from pathlib import Path

import pytest

from echolot import db
from echolot.config import Settings
from echolot.library import stars
from echolot.services import navidrome

S1, S2, TUNE = (
    "Artist A/Artist A - First Song.mp3",
    "Artist B/Artist B - Second Song.flac",
    "Uploader/Uploader - Trance Tune.m4a",
)


class Navidrome:
    """Navidrome's songs (in the library folder `prefix`) and the owner's hearts; calls counted."""

    def __init__(self, files: list[str], hearts: set[str] = frozenset(), prefix: str = "tracks/") -> None:
        self.paths = {f"{prefix}{f}": f"id:{f}" for f in files}
        self.hearts = set(hearts)
        self.calls: list[tuple] = []

    def songs(self) -> list[dict]:
        self.calls.append(("songs",))
        return [{"id": i, "path": p, "missing": False} for p, i in self.paths.items()]

    def starred(self, user: str) -> set[str]:
        assert user == "owner"
        return set(self.hearts)

    def star(self, user: str, ids: list[str], on: bool = True) -> None:
        if ids:
            self.calls.append(("star" if on else "unstar", *ids))
        self.hearts = self.hearts | set(ids) if on else self.hearts - set(ids)


@pytest.fixture
def con(settings: Settings):
    con = db.connect(settings.db_path)
    yield con
    con.close()


def mine(con: sqlite3.Connection) -> list[tuple]:
    return [tuple(r) for r in con.execute("SELECT list_key, song_key, file, ours FROM stars ORDER BY song_key")]


def test_the_liked_songs_get_their_hearts_once(con: sqlite3.Connection) -> None:
    """Every liked song with a file gets a heart, the oldest like first; one the user gave already is theirs
    (never taken back). Nothing changed: Navidrome is not asked."""
    nd = Navidrome([S1, S2, TUNE], hearts={f"id:{S2}"})
    assert stars.sync(con, nd, "tracks") == "2 hearts given in Navidrome"
    assert nd.calls == [("songs",), ("star", f"id:{TUNE}", f"id:{S1}")]
    assert mine(con) == [
        ("soundcloud:someone/likes", "soundcloud:1001", TUNE, 1),
        ("spotify:likes:1", "spotify:s1", S1, 1),
        ("spotify:likes:1", "spotify:s2", S2, 0),
    ]
    nd.calls.clear()
    nd.hearts.discard(f"id:{S1}")  # taken back in a player: not given again
    assert stars.sync(con, nd, "tracks") == ""
    assert nd.calls == []


def test_an_unliked_song_loses_only_echolots_heart(con: sqlite3.Connection) -> None:
    nd = Navidrome([S1, S2, TUNE], hearts={f"id:{S2}"})
    stars.sync(con, nd, "tracks")
    with con:
        con.execute("DELETE FROM list_songs WHERE song_key IN ('spotify:s2', 'soundcloud:1001')")
    assert stars.sync(con, nd, "tracks") == "1 taken back"
    assert nd.calls[-1] == ("unstar", f"id:{TUNE}")
    assert nd.hearts == {f"id:{S1}", f"id:{S2}"}
    assert [r[1] for r in mine(con)] == ["spotify:s1"]


def test_a_song_in_two_lists_keeps_its_heart_while_one_has_it(con: sqlite3.Connection) -> None:
    with con:
        con.execute("UPDATE songs SET file = ? WHERE key = 'soundcloud:1001'", (S1,))  # the same file as s1
    nd = Navidrome([S1, S2])
    stars.sync(con, nd, "tracks")
    with con:
        con.execute("DELETE FROM list_songs WHERE song_key = 'spotify:s1'")
    assert stars.sync(con, nd, "tracks") == ""
    assert f"id:{S1}" in nd.hearts


def test_an_unfollowed_list_leaves_its_hearts(con: sqlite3.Connection) -> None:
    nd = Navidrome([S1, S2, TUNE])
    stars.sync(con, nd, "tracks")
    nd.calls.clear()
    with con:
        con.execute("UPDATE sources SET enabled = 0 WHERE key = 'spotify:likes:1'")
    assert stars.sync(con, nd, "tracks") == ""
    assert nd.calls == []
    assert len(nd.hearts) == 3
    assert [r[0] for r in mine(con)] == ["soundcloud:someone/likes"]


def test_a_file_navidrome_lacks_gets_its_heart_later(con: sqlite3.Connection) -> None:
    nd = Navidrome([S1, S2])
    assert stars.sync(con, nd, "tracks") == "2 hearts given in Navidrome, 1 wait for Navidrome to read their file"
    nd.paths[f"tracks/{TUNE}"] = f"id:{TUNE}"
    assert stars.sync(con, nd, "tracks") == "1 hearts given in Navidrome"
    assert f"id:{TUNE}" in nd.hearts


def test_a_better_file_takes_the_heart_over(con: sqlite3.Connection) -> None:
    """A song with another file now (an upgrade): the new file gets the heart, the old one (still in Navidrome)
    loses Echolot's."""
    new = "Artist A/Artist A - First Song.flac"
    nd = Navidrome([S1, S2, TUNE, new])
    stars.sync(con, nd, "tracks")
    with con:
        con.execute("UPDATE songs SET file = ? WHERE key = 'spotify:s1'", (new,))
    assert stars.sync(con, nd, "tracks") == "1 hearts given in Navidrome, 1 taken back"
    assert f"id:{new}" in nd.hearts and f"id:{S1}" not in nd.hearts


def test_navidromes_paths_below_another_folder(con: sqlite3.Connection) -> None:
    """Navidrome's library holds Echolot's folder (music/echolot): its paths have more in front."""
    nd = Navidrome([S1, S2, TUNE], prefix="echolot/tracks/")
    nd.paths["other/tracks2/Artist A/Artist A - First Song.mp3"] = "id:other"
    assert stars.sync(con, nd, "tracks") == "3 hearts given in Navidrome"
    assert "id:other" not in nd.hearts


def test_the_hearts_follow_when_the_songs_were_liked(con: sqlite3.Connection) -> None:
    """Across the lists, the oldest like first. Once all likes are timed, Echolot's hearts are given once more in
    that order (not the user's own, nor one they took back)."""
    nd = Navidrome([S1, S2, TUNE], hearts={f"id:{S2}"})
    assert stars.sync(con, nd, "tracks") == "2 hearts given in Navidrome"  # not timed: as listed
    assert nd.calls[-1] == ("star", f"id:{TUNE}", f"id:{S1}")
    times = {"spotify:s1": "2026-01", "spotify:s2": "2025-01", "spotify:s3": "2026-01", "soundcloud:1001": "2026-02"}
    with con:
        con.execute("UPDATE list_songs SET added = '2026-03-01T00:00:00Z'")
        con.executemany("UPDATE list_songs SET added = ? WHERE song_key = ?", [(t, k) for k, t in times.items()])
    nd.calls.clear()
    assert stars.sync(con, nd, "tracks") == "2 given again in the order they were liked"
    assert nd.calls == [("songs",), ("star", f"id:{S1}", f"id:{TUNE}")]
    nd.calls.clear()
    assert stars.sync(con, nd, "tracks") == "" and nd.calls == []  # once
    with con:
        con.execute("DELETE FROM meta WHERE key = 'stars_order:1'")
    nd.hearts.discard(f"id:{S1}")  # taken back in a player
    assert stars.sync(con, nd, "tracks") == "1 given again in the order they were liked"
    assert nd.calls[-1] == ("star", f"id:{TUNE}") and f"id:{S1}" not in nd.hearts


def test_a_song_echolot_hearted_stays_its_in_another_list(con: sqlite3.Connection) -> None:
    """A song liked in a second list after Echolot gave its heart: the heart stays Echolot's, so it goes once the
    song is unliked in both."""
    nd = Navidrome([S1, S2, TUNE])
    stars.sync(con, nd, "tracks")
    with con:
        con.execute(
            "INSERT INTO list_songs (list_key, position, song_key) VALUES ('soundcloud:someone/likes', 9, 'spotify:s1')"
        )
    stars.sync(con, nd, "tracks")
    assert ("soundcloud:someone/likes", "spotify:s1", S1, 1) in mine(con)
    with con:
        con.execute("DELETE FROM list_songs WHERE song_key = 'spotify:s1'")
    assert stars.sync(con, nd, "tracks") == "1 taken back"
    assert f"id:{S1}" not in nd.hearts


def test_a_call_as_the_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Echolot names the user in its header; a Navidrome that does not trust Echolot's address asks for a
    login, which says what to set."""
    sent: list = []
    answers = [
        {"status": "ok", "starred2": {"song": [{"id": "a"}, {"id": "b"}]}},
        {"status": "failed", "error": {"code": 10, "message": "missing parameter: 'u'"}},
    ]

    def urlopen(request, timeout):
        sent.append(request)
        return io.BytesIO(json.dumps({"subsonic-response": answers.pop(0)}).encode())

    monkeypatch.setattr(navidrome.urllib.request, "urlopen", urlopen)
    svc = navidrome.Service("http://navidrome:4533/", "admin", "x")
    assert svc.starred("luca") == {"a", "b"}
    assert sent[0].full_url.startswith("http://navidrome:4533/rest/getStarred2?v=")
    assert sent[0].get_header(navidrome.USER_HEADER.capitalize()) == "luca"
    with pytest.raises(navidrome.NavidromeError, match="ND_EXTAUTH_TRUSTEDSOURCES"):
        svc.star("luca", ["a"])
