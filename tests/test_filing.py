"""Filing into the library (filing.py) and applying review decisions (review.py), on real files."""

import datetime
import sqlite3
import threading
import wave
from pathlib import Path

import pytest

from echolot import db, filing, library, review, vault
from echolot.filing import Paths, Want

CAPO = Want("CAPO", "Run Run Run (feat. Yung Kafa & Kücük Efendi) - Remix", 200, "spotify:capo")
CAPO_FILE = "CAPO - RUN RUN RUN feat. YUNG KAFA & KÜCÜK EFENDI (prod. von Jurijgold & Falconi) [Official Remix]"


class FakeRun:
    """What review.apply_due needs of a worker run."""

    def __init__(self, db_path: Path, paths: Paths, data: Path) -> None:
        self.db_path, self.paths, self.data = db_path, paths, data
        self.vault = vault.Vault.from_env(data, {})
        self.stop, self.after = threading.Event(), set()

    def connect(self) -> sqlite3.Connection:
        return db.connect(self.db_path)

    def say(self, _: str) -> None:
        pass


@pytest.fixture
def env(tmp_path: Path):
    paths = Paths(tmp_path / "music")
    paths.tracks.mkdir(parents=True)
    db.init(tmp_path / "data" / "echolot.db")
    con = db.connect(tmp_path / "data" / "echolot.db")
    yield con, paths, FakeRun(tmp_path / "data" / "echolot.db", paths, tmp_path / "data")
    con.close()


def download(paths: Paths, name: str, seconds: int = 200) -> Path:
    """A silent WAV of the given length (100 frames per second keeps it small)."""
    path = paths.inbox("soulseek") / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(100)
        w.writeframes(b"\x80" * 100 * seconds)
    return path


def events(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM events ORDER BY id").fetchall()


def file_capo(con, paths, probable: bool):
    return filing.file_into(con, paths, download(paths, "1.wav"), CAPO, "soulseek", strict=True,
                            file_name=CAPO_FILE, probable=probable, tries=2)  # fmt: skip


def test_strict_rejection_is_kept_for_review(env) -> None:
    con, paths, _ = env
    assert file_capo(con, paths, probable=False) == ("wrong-song", None)
    e = events(con)[-1]
    assert (
        e["action"] == "wrong-song"
        and e["found"] == CAPO_FILE
        and e["tries"] == 2
        and e["seconds"] == 200
    )
    assert e["path"].startswith("/music/inbox/review/")
    assert review.local_file(e["path"], paths.music).is_file()


def test_probable_match_filed_with_review_mark(env) -> None:
    con, paths, _ = env
    action, dest = file_capo(con, paths, probable=True)
    assert (
        action == "new"
        and dest.name == "CAPO - Run Run Run (feat. Yung Kafa & Kücük Efendi) - Remix.wav"
    )
    e = events(con)[-1]
    assert (
        e["matched"] == "probable"
        and e["reason"].startswith("probable")
        and e["song"] == "spotify:capo"
    )
    assert (
        con.execute("SELECT count(*) FROM files").fetchone()[0] == 1
    )  # the index knows it at once


def test_same_song_twice_is_a_duplicate(env) -> None:
    con, paths, _ = env
    want = Want("A", "Song", 200, "spotify:a")
    assert filing.file_into(con, paths, download(paths, "a.wav"), want, "soulseek")[0] == "new"
    action, dest = filing.file_into(
        con, paths, download(paths, "b.wav"), Want("A", "Song (Original Mix)", 201), "x"
    )
    assert action == "duplicate" and dest.name == "A - Song.wav"
    assert not (paths.inbox("soulseek") / "b.wav").exists()
    assert [p.name for p in (paths.tracks / "A").iterdir()] == ["A - Song.wav"]


def test_lossless_replaces_lossy_copy(env) -> None:
    con, paths, _ = env
    mp3 = paths.tracks / "A" / "A - Song.mp3"
    mp3.parent.mkdir()
    mp3.write_bytes(b"not really audio")
    library.scan(con, paths.tracks)
    with con:
        con.execute("UPDATE files SET duration = 200 WHERE path = 'A/A - Song.mp3'")
    action, dest = filing.file_into(
        con, paths, download(paths, "a.wav"), Want("A", "Song", 200), "soulseek"
    )
    assert action == "upgrade" and dest.name == "A - Song.wav"
    assert not mp3.exists()
    replaced = list(paths.inbox("replaced").rglob("*.mp3"))
    assert len(replaced) == 1 and replaced[0].name == "A - Song.mp3"
    assert [r[0] for r in con.execute("SELECT path FROM files")] == ["A/A - Song.wav"]
    assert events(con)[-2]["action"] == "retired"


def test_only_near_misses_are_kept(env) -> None:
    con, paths, _ = env

    def reject(name: str, artist: str, seconds: int, file_name: str) -> bool:
        filing.file_into(con, paths, download(paths, name, seconds), Want(artist, "Sweaters", 200, "spotify:x"),
                         "soulseek", strict=True, file_name=file_name)  # fmt: skip
        f = review.local_file(events(con)[-1]["path"], paths.music)
        return bool(f and f.is_file())

    assert reject("a.wav", "just a fake", 190, "just a fake - Sweater Weather")  # other title: kept
    assert reject("b.wav", "just a fake", 240, "just a fake - Sweaters")  # other version: kept
    assert not reject("c.wav", "just a fake", 1666, "just a fake - Sweaters")  # a whole mix
    assert not reject("d.wav", "just a fake", 200, "Mozart - Die Zauberflöte")  # another artist


def test_collaboration_listed_twice_is_one_song(env) -> None:
    """Spotify: "Mabe, Catch Vibe - Atlantis" and "Catch Vibe, Mabe - Atlantis" (single and EP)."""
    con, paths, _ = env
    filing.file_into(
        con,
        paths,
        download(paths, "a.wav", 350),
        Want("Mabe", "Atlantis", 350, "", ["Mabe", "Catch Vibe"]),
        "x",
    )
    other = Want("Catch Vibe", "Atlantis", 349, "", ["Catch Vibe", "Mabe"])
    action, dest = filing.file_into(con, paths, download(paths, "b.wav", 350), other, "x")
    assert action == "duplicate" and dest.parent.name == "Mabe"
    cat = library.Catalog.from_db(con)
    assert not cat.song("Catch Vibe", "Atlantis II", 349, ["Catch Vibe", "Mabe"])
    assert not cat.song("Catch Vibe", "Atlantis", 200, ["Catch Vibe", "Mabe"])


def test_one_folder_per_artist(env) -> None:
    con, paths, _ = env
    filing.file_into(con, paths, download(paths, "a.wav"), Want("Røyksopp", "Eple", 200), "x")
    _, dest = filing.file_into(
        con, paths, download(paths, "b.wav", 300), Want("ROYKSOPP", "Remind Me", 300), "x"
    )
    assert dest.parent.name == "Røyksopp"


def test_mix_cut_found_at_any_length(env) -> None:
    """Neelix - The Twenty Five: the liked song is a 1:43 cut of a DJ mix; the 4:55 release counts."""
    con, paths, _ = env
    title = "The Twenty Five (Official Nature One Anthem 2019)"
    filing.file_into(con, paths, download(paths, "full.wav", 295), Want("Neelix", title, 295), "x")
    cat = library.Catalog.from_db(con)
    assert cat.find("Neelix", title + " - Mixed", 103)
    assert not cat.find("Neelix", title, 103)  # a real 1:43 version would be another song


def test_purge_only_old_days(env) -> None:
    _, paths, _ = env
    old = paths.inbox("review") / (datetime.date.today() - datetime.timedelta(days=31)).isoformat()
    new = paths.inbox("replaced") / datetime.date.today().isoformat()
    other = paths.inbox("review") / "keep-me"
    for d in (old, new, other):
        d.mkdir(parents=True)
        (d / "x.flac").write_bytes(b"x")
    assert filing.purge(paths) == [f"/music/inbox/review/{old.name}"]
    assert not old.exists() and new.exists() and other.exists()


# ---------------------------------------------------------------- review decisions


def decide(con, run, event_id: int, decision: str) -> str:
    """Decide and apply at once (the undo window passed)."""
    review.decide(con, run.paths.music, event_id, decision)
    with con:
        con.execute("UPDATE review_decisions SET decided = '2000-01-01T00:00:00'")
    return review.apply_due(run, con)[-1]


def test_wrong_retires_blocks_and_searches_again(env) -> None:
    con, paths, run = env
    _, dest = file_capo(con, paths, probable=True)
    e = events(con)[-1]
    assert decide(con, run, e["id"], "wrong").endswith("retired")
    assert not dest.exists()
    assert tuple(
        con.execute(
            "SELECT tries, last_try FROM attempts WHERE song_key = 'spotify:capo'"
        ).fetchone()
    ) == (2, 0)
    # the same download is never taken again, though it is a probable match
    assert file_capo(con, paths, probable=True) == ("wrong-song", None)
    assert events(con)[-1]["reason"] == "this download was marked wrong in review"


def test_accept_files_a_kept_download(env) -> None:
    con, paths, run = env
    file_capo(con, paths, probable=False)
    e = events(con)[-1]
    items = review.items(con, paths.music)
    assert [i.event["id"] for i in items["kept"]] == [e["id"]]
    result = decide(con, run, e["id"], "accept")
    assert "new CAPO/" in result
    assert events(con)[-1]["matched"] == "review"
    assert review.items(con, paths.music) == {"filed": [], "kept": []}


def test_revert_until_applied(env) -> None:
    con, paths, run = env
    file_capo(con, paths, probable=False)
    e = events(con)[-1]
    review.decide(con, paths.music, e["id"], "discard")
    assert review.apply_due(run, con) == []  # within the undo window
    review.revert(con, paths.music, e["id"])
    assert review.items(con, paths.music)["kept"][0].decision is None
    assert decide(con, run, e["id"], "discard").endswith("deleted")
    with pytest.raises(review.ConfigError):
        review.revert(con, paths.music, e["id"])


def test_accept_links_the_same_recording_under_another_artist(env, monkeypatch) -> None:
    """Spotify: "Pbb Yea - Chilln" and "TheDoDo - Chilln"; YouTube has one video, tagged Pbb Yea."""
    con, paths, run = env
    filing.file_into(
        con,
        paths,
        download(paths, "a.wav", 227),
        Want("Pbb Yea", "Chilln", 227, "spotify:pbb"),
        "x",
    )
    with con:
        con.execute("INSERT INTO songs (key, service, artist, title, length) VALUES ('spotify:dodo', 'spotify', "
                    "'TheDoDo', 'Chilln', 227)")  # fmt: skip
    filing.file_into(con, paths, download(paths, "b.wav", 227), Want("TheDoDo", "Chilln", 227, "spotify:dodo"),
                     "youtube", strict=True, file_name="Chilln (Official Video)", probable=False)  # fmt: skip
    e = events(con)[-1]
    assert (
        e["action"] == "wrong-song"
    )  # the artist is only in the tags of the real file: here nowhere
    monkeypatch.setattr("echolot.audio.read_tags", lambda p: (["Pbb Yea"], "Chilln"))
    # a rejected download of the song, kept: accepted, it turns out to be Pbb Yea's file
    kept = paths.inbox("review") / "2026-09-27" / "TheDoDo - Chilln [youtube].wav"
    kept.parent.mkdir(parents=True, exist_ok=True)
    download(paths, "c.wav", 227).rename(kept)
    filing.event(
        con,
        paths,
        "wrong-song",
        kept,
        song="spotify:dodo",
        artist="TheDoDo",
        title="Chilln",
        source="youtube",
    )
    result = decide(con, run, events(con)[-1]["id"], "accept")
    assert result.endswith("linked Pbb Yea/Pbb Yea - Chilln.wav")
    assert not kept.exists() and not (paths.tracks / "TheDoDo").exists()
    assert (
        con.execute("SELECT link FROM songs WHERE key = 'spotify:dodo'").fetchone()[0]
        == '["Pbb Yea", "Chilln"]'
    )
