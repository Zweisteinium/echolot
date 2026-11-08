"""Filing into the library (filing.py) and applying review decisions (review.py), on real files."""

import datetime
import shutil
import sqlite3
import subprocess
import threading
import wave
from pathlib import Path

import pytest

from echolot import db
from echolot.library import audio, catalog, filing, review, tagging
from echolot.library.filing import Paths, Want
from echolot.library.identity import Evidence
from echolot.settings import vault

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
    assert e["action"] == "wrong-song" and e["found"] == CAPO_FILE and e["tries"] == 2 and e["seconds"] == 200
    assert e["path"].startswith("/music/inbox/review/")
    assert review.local_file(e["path"], paths.music).is_file()


def test_waiting_goes_by_the_review_pages_rules(env) -> None:
    con, paths, _ = env
    file_capo(con, paths, probable=False)
    assert review.waiting(con, paths.music) == {CAPO.key} and review.waiting(con, paths.music, CAPO.key)
    # a download of another artist stays in inbox/review but is not shown: nothing waits for the song
    with con:
        con.execute("UPDATE events SET reason = 'artist ''X'' not in [''Y'']' WHERE id = ?", (events(con)[-1]["id"],))
    assert filing.in_review(paths, CAPO.artist, CAPO.title)
    assert review.waiting(con, paths.music) == set() and not review.waiting(con, paths.music, CAPO.key)


def test_probable_match_filed_with_review_mark(env) -> None:
    con, paths, _ = env
    action, dest = file_capo(con, paths, probable=True)
    assert action == "new" and dest.name == "CAPO - Run Run Run (feat. Yung Kafa & Kücük Efendi) - Remix.wav"
    e = events(con)[-1]
    assert e["matched"] == "probable" and e["reason"].startswith("probable") and e["song"] == "spotify:capo"
    assert con.execute("SELECT count(*) FROM files").fetchone()[0] == 1  # the index knows it at once


def test_same_song_twice_is_a_duplicate(env) -> None:
    con, paths, _ = env
    want = Want("A", "Song", 200, "spotify:a")
    assert filing.file_into(con, paths, download(paths, "a.wav"), want, "soulseek")[0] == "new"
    action, dest = filing.file_into(con, paths, download(paths, "b.wav"), Want("A", "Song (Original Mix)", 201), "x")
    assert action == "duplicate" and dest.name == "A - Song.wav"
    assert not (paths.inbox("soulseek") / "b.wav").exists()
    assert [p.name for p in (paths.tracks / "A").iterdir()] == ["A - Song.wav"]


def test_a_fake_flac_that_takes_over_stays_marked(env) -> None:
    """A fake FLAC replacing a lossy copy of the same stem stays marked fake, in lossy_sourced and its event
    (retiring the MP3 once cleared the shared stem's mark)."""
    con, paths, _ = env
    mp3 = paths.tracks / "A" / "A - Song.mp3"
    mp3.parent.mkdir()
    mp3.write_bytes(b"not really audio")
    catalog.scan(con, paths.tracks)
    with con:
        con.execute("UPDATE files SET duration = 200 WHERE path = 'A/A - Song.mp3'")
    src = download(paths, "a.flac")
    want = Want("A", "Song", 200)
    action, dest = filing.file_into(
        con, paths, src, want, "mitschnitt", fake=True, fake_source="~256 kbps / V0", replace=True
    )
    assert action == "upgrade" and dest.name == "A - Song.flac" and not mp3.exists()
    assert [tuple(r) for r in con.execute("SELECT stem, source FROM lossy_sourced")] == [
        ("A/A - Song", "~256 kbps / V0")
    ]
    assert catalog.Catalog.from_db(con).entries[0].quality == "fake"
    assert con.execute("SELECT fake FROM events WHERE action = 'upgrade'").fetchone()[0] == 1


def test_lossless_replaces_lossy_copy(env) -> None:
    con, paths, _ = env
    mp3 = paths.tracks / "A" / "A - Song.mp3"
    mp3.parent.mkdir()
    mp3.write_bytes(b"not really audio")
    catalog.scan(con, paths.tracks)
    with con:
        con.execute("UPDATE files SET duration = 200 WHERE path = 'A/A - Song.mp3'")
    action, dest = filing.file_into(con, paths, download(paths, "a.wav"), Want("A", "Song", 200), "soulseek")
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
    """The same artists in another order are one song ("Mabe, Catch Vibe" vs "Catch Vibe, Mabe")."""
    con, paths, _ = env
    filing.file_into(
        con, paths, download(paths, "a.wav", 350), Want("Mabe", "Atlantis", 350, "", ["Mabe", "Catch Vibe"]), "x"
    )
    other = Want("Catch Vibe", "Atlantis", 349, "", ["Catch Vibe", "Mabe"])
    action, dest = filing.file_into(con, paths, download(paths, "b.wav", 350), other, "x")
    assert action == "duplicate" and dest.parent.name == "Mabe"
    cat = catalog.Catalog.from_db(con)
    assert not cat.song("Catch Vibe", "Atlantis II", 349, ["Catch Vibe", "Mabe"])
    assert not cat.song("Catch Vibe", "Atlantis", 200, ["Catch Vibe", "Mabe"])


def test_one_folder_per_artist(env) -> None:
    con, paths, _ = env
    filing.file_into(con, paths, download(paths, "a.wav"), Want("Røyksopp", "Eple", 200), "x")
    _, dest = filing.file_into(con, paths, download(paths, "b.wav", 300), Want("ROYKSOPP", "Remind Me", 300), "x")
    assert dest.parent.name == "Røyksopp"


def test_mix_cut_found_at_any_length(env) -> None:
    """A "- Mixed" cut of a DJ mix is found by the full release at any length."""
    con, paths, _ = env
    title = "The Twenty Five (Official Nature One Anthem 2019)"
    filing.file_into(con, paths, download(paths, "full.wav", 295), Want("Neelix", title, 295), "x")
    cat = catalog.Catalog.from_db(con)
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
    assert tuple(con.execute("SELECT tries, last_try FROM attempts WHERE song_key = 'spotify:capo'").fetchone()) == (
        2,
        0,
    )
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
    """An accepted download whose tags name a library file's artist links the song to that file."""
    con, paths, run = env
    filing.file_into(con, paths, download(paths, "a.wav", 227), Want("Pbb Yea", "Chilln", 227, "spotify:pbb"), "x")
    with con:
        con.execute("INSERT INTO songs (key, service, artist, title, length) VALUES ('spotify:dodo', 'spotify', "
                    "'TheDoDo', 'Chilln', 227)")  # fmt: skip
    filing.file_into(con, paths, download(paths, "b.wav", 227), Want("TheDoDo", "Chilln", 227, "spotify:dodo"),
                     "youtube", strict=True, file_name="Chilln (Official Video)", probable=False)  # fmt: skip
    e = events(con)[-1]
    assert e["action"] == "wrong-song"  # the artist is only in the tags of the real file: here nowhere
    monkeypatch.setattr("echolot.library.audio.read_tags", lambda p: (["Pbb Yea"], "Chilln"))
    kept = paths.inbox("review") / "2026-09-27" / "TheDoDo - Chilln [youtube].wav"
    kept.parent.mkdir(parents=True, exist_ok=True)
    download(paths, "c.wav", 227).rename(kept)
    filing.event(
        con, paths, "wrong-song", kept, song="spotify:dodo", artist="TheDoDo", title="Chilln", source="youtube"
    )
    result = decide(con, run, events(con)[-1]["id"], "accept")
    assert result.endswith("linked Pbb Yea/Pbb Yea - Chilln.wav")
    assert not kept.exists() and not (paths.tracks / "TheDoDo").exists()
    assert con.execute("SELECT link FROM songs WHERE key = 'spotify:dodo'").fetchone()[0] == '["Pbb Yea", "Chilln"]'


def search_hit(con, paths: Paths, src: Path, want: Want, name: str, source: str = "youtube", **kw) -> tuple:
    """File a search result: the download must be the song."""
    return filing.file_into(con, paths, src, want, source, strict=True, file_name=name, **kw)


def test_accepted_other_length_counts_and_a_discarded_one_is_blocked(env) -> None:
    """An accepted download of another length counts for the song; a discarded one is blocked for good."""
    con, paths, run = env
    rows = [("spotify:a", "LAWTON", "Believe In"), ("spotify:b", "LAWTON", "Horizon")]
    sql = "INSERT INTO songs (key, service, artist, title, length) VALUES (?, 'spotify', ?, ?, 200)"
    with con:
        con.executemany(sql, rows)
    video = "LAWTON - Believe In (Official Visualizer)"
    want = Want("LAWTON", "Believe In", 200, "spotify:a")
    assert search_hit(con, paths, download(paths, "a.wav", 219), want, video) == ("mismatch", None)
    assert filing.in_review(paths, "LAWTON", "Believe In")
    assert "new LAWTON/" in decide(con, run, events(con)[-1]["id"], "accept")
    catalog.match_songs(con)
    file = con.execute("SELECT file FROM songs WHERE key = 'spotify:a'").fetchone()[0]
    assert file == "LAWTON/LAWTON - Believe In.wav"
    other = "LAWTON - Horizon (Official Video)"
    search_hit(con, paths, download(paths, "b.wav", 240), Want("LAWTON", "Horizon", 200, "spotify:b"), other)
    assert decide(con, run, events(con)[-1]["id"], "discard").endswith("deleted")
    assert filing.is_blocked(con, "spotify:b", [other])


def test_the_audio_confirms_or_overrules_the_name(env) -> None:
    con, paths, _ = env
    same = Evidence("same", "audio of the release (0.95)")
    src = download(paths, "1.wav")
    action, _ = search_hit(con, paths, src, CAPO, CAPO_FILE, "soulseek", probable=False, heard=same)
    assert action == "new" and events(con)[-1]["matched"] == "exact"  # probable, confirmed: no review
    assert events(con)[-1]["audio"] == "audio of the release (0.95)"
    other = Evidence("other", "audio differs from the release (0.58)")
    song = Want("A", "Song", 200, "spotify:a")
    result = search_hit(con, paths, download(paths, "2.wav"), song, "A - Song", heard=other)
    assert result == ("wrong-song", None)
    assert "audio differs" in events(con)[-1]["reason"] and filing.in_review(paths, "A", "Song")


def test_a_wrong_download_far_off_the_length_is_deleted(env) -> None:
    con, paths, _ = env
    src, want = download(paths, "3.wav", 78), Want("HK", "Was!?!?", 269, "spotify:hk")
    search_hit(con, paths, src, want, "31 Eine Art Chansons - Was können sie dir tun", "soulseek")
    assert events(con)[-1]["action"] == "wrong-song"
    assert not src.exists() and not filing.in_review(paths, "HK", "Was!?!?")


def close(con, run, event_id: int, name: str) -> str:
    """Take a download as a close match and apply it at once."""
    review.decide(con, run.paths.music, event_id, "close", name)
    with con:
        con.execute("UPDATE review_decisions SET decided = '2000-01-01T00:00:00'")
    return review.apply_due(run, con)[-1]


def song(con, key: str) -> sqlite3.Row:
    return con.execute("SELECT * FROM songs WHERE key = ?", (key,)).fetchone()


def add_song(con, key: str, artist: str, title: str, length: int) -> Want:
    with con:
        sql = "INSERT INTO songs (key, service, artist, title, length) VALUES (?, 'spotify', ?, ?, ?)"
        con.execute(sql, (key, artist, title, length))
        con.execute("INSERT INTO attempts (song_key, tries, last_try) VALUES (?, 3, 1)", (key,))
    return Want(artist, title, length, key)


def test_a_close_match_is_filed_under_its_own_name(env) -> None:
    """A close match is filed under its own name and the song linked to it; search_again unlinks it."""
    con, paths, run = env
    want = add_song(con, "spotify:nc", "No Chasa", "Master Disaster", 212)
    found = "NO CHASA - MASTER DISASTER (EXTENDED MIX)"
    search_hit(con, paths, download(paths, "a.wav", 316), want, found, "soulseek")
    item = review.items(con, paths.music)["kept"][0]
    assert item.close_name == found and [d for d, _ in item.choices] == ["accept", "close", "discard"]
    result = close(con, run, item.event["id"], "No Chasa - Master Disaster (Extended Mix)")
    assert result.endswith(": new No Chasa/No Chasa - Master Disaster (Extended Mix).wav")
    s = song(con, "spotify:nc")
    assert (s["link"], s["close_match"]) == ('["No Chasa", "Master Disaster (Extended Mix)", 316]', 1)
    assert con.execute("SELECT 1 FROM attempts WHERE song_key = 'spotify:nc'").fetchone() is None
    catalog.match_songs(con)
    assert song(con, "spotify:nc")["file"] == "No Chasa/No Chasa - Master Disaster (Extended Mix).wav"
    review.search_again(con, "spotify:nc")  # the file stays
    catalog.match_songs(con)
    assert song(con, "spotify:nc")["file"] is None
    assert (paths.tracks / "No Chasa" / "No Chasa - Master Disaster (Extended Mix).wav").is_file()


def accept_visualizer(con, paths, run) -> Path:
    """A video 19 s longer, accepted: filed and linked as the song."""
    want = add_song(con, "spotify:lw", "LAWTON", "Believe In", 200)
    search_hit(con, paths, download(paths, "a.wav", 219), want, "LAWTON - Believe In (Official Visualizer)")
    decide(con, run, events(con)[-1]["id"], "accept")
    catalog.match_songs(con)
    return paths.tracks / song(con, "spotify:lw")["file"]


def test_recheck_and_rename_a_close_match(env) -> None:
    con, paths, run = env
    old = accept_visualizer(con, paths, run)
    assert review.recheck(con, paths, "spotify:lw", "maybe another version")
    assert not review.recheck(con, paths, "spotify:lw", "twice")  # up for review already
    item = review.items(con, paths.music)["filed"][0]
    assert item.event["action"] == "recheck" and item.close_name == "LAWTON - Believe In"  # without the video noise
    result = close(con, run, item.event["id"], "LAWTON - Believe In (Video Edit)")
    assert result.endswith(": renamed LAWTON/LAWTON - Believe In (Video Edit).wav")
    assert not old.exists() and events(con)[-1]["action"] == "renamed"
    catalog.match_songs(con)
    assert song(con, "spotify:lw")["file"] == "LAWTON/LAWTON - Believe In (Video Edit).wav"
    assert song(con, "spotify:lw")["close_match"] == 1


def test_no_match_on_a_recheck_takes_the_file_out(env) -> None:
    con, paths, run = env
    old = accept_visualizer(con, paths, run)
    review.recheck(con, paths, "spotify:lw", "maybe another version")
    assert decide(con, run, events(con)[-1]["id"], "wrong") == "No match LAWTON - Believe In: retired"
    assert not old.exists() and song(con, "spotify:lw")["link"] is None
    assert filing.is_blocked(con, "spotify:lw", ["LAWTON - Believe In (Official Visualizer)"])
    assert con.execute("SELECT last_try FROM attempts WHERE song_key = 'spotify:lw'").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("found", "artists", "name"),
    [
        ("One - 2017 Remake", ["5udo"], "5udo - One - 2017 Remake"),
        ("BURN IT DOWN (Official Video) [4K] - Linkin Park", ["Linkin Park"], "Linkin Park - BURN IT DOWN"),
        ("Raket One - Techno & Tekk (Actek Remix)", ["Actek", "Raket One"], "Raket One - Techno & Tekk (Actek Remix)"),
        ("Jaspa - Auge der Vorsehung | JCC 2020 | Qualifikation #17", ["Jaspa"], "Jaspa - Auge der Vorsehung"),
        ("CAPO - Run Run Run [Official Remix]", ["CAPO"], "CAPO - Run Run Run [Official Remix]"),
    ],
)
def test_close_guess(found: str, artists: list[str], name: str) -> None:
    assert review.close_guess(found, artists) == name


def test_a_close_match_for_a_lossy_song_keeps_both(env) -> None:
    """A close match for a song the library has lossy is filed beside it; the lossy copy stays."""
    con, paths, run = env
    want = add_song(con, "spotify:tk", "TEKKNO", "say it right tekkno", 135)
    mp3 = paths.tracks / "TEKKNO" / "TEKKNO - say it right tekkno.mp3"
    mp3.parent.mkdir()
    mp3.write_bytes(b"not really audio")
    catalog.scan(con, paths.tracks)
    with con:
        con.execute("UPDATE files SET duration = 135, kbps = 312")
    catalog.match_songs(con)
    other = Evidence("other", "audio differs from the release (0.57)")
    search_hit(con, paths, download(paths, "a.wav", 134), want, "say it right tekkno", "soulseek", heard=other)
    item = review.items(con, paths.music)["kept"][0]
    assert [d for d, _ in item.choices] == ["accept", "close", "discard"]
    result = close(con, run, item.event["id"], "TEKKNO - say it right tekkno (Hardtekk Mix)")
    assert result.endswith(": new TEKKNO/TEKKNO - say it right tekkno (Hardtekk Mix).wav")
    assert mp3.exists()
    catalog.match_songs(con)
    assert song(con, "spotify:tk")["file"] == "TEKKNO/TEKKNO - say it right tekkno (Hardtekk Mix).wav"


def test_a_renamed_file_stays_the_soundcloud_songs_download(env) -> None:
    """A SoundCloud song (matched by stem) follows its file through a rename."""
    con, paths, _ = env
    filing.file_into(con, paths, download(paths, "a.wav", 316), Want("No Chasa", "MASTER DISASTER"), "x")
    row = ("soundcloud:1", "No Chasa", "MASTER DISASTER", 316, "No Chasa/No Chasa - MASTER DISASTER")
    with con:
        sql = "INSERT INTO songs (key, service, artist, title, length, stem, archived) VALUES (?, 'soundcloud', ?, ?, ?, ?, 1)"
        con.execute(sql, row)
    entry = catalog.Catalog.from_db(con).entries[0]
    filing.rename(con, paths, entry, "No Chasa", "MASTER DISASTER (EXTENDED MIX)", "close match")
    catalog.match_songs(con)
    assert song(con, "soundcloud:1")["file"] == "No Chasa/No Chasa - MASTER DISASTER (EXTENDED MIX).wav"


def test_a_rename_and_a_removal_keep_the_fake_mark(env) -> None:
    """A renamed fake keeps its lossy_sourced row; a retired one's event is marked fake."""
    con, paths, _ = env
    filing.file_into(con, paths, download(paths, "a.flac", 316), Want("No Chasa", "MASTER DISASTER"), "x", fake=True)
    with con:
        con.execute("UPDATE lossy_sourced SET source = 'Opus ~130 kbps'")
    entry = catalog.Catalog.from_db(con).entries[0]
    filing.rename(con, paths, entry, "No Chasa", "MASTER DISASTER (EXTENDED MIX)", "close match")
    stem = "No Chasa/No Chasa - MASTER DISASTER (EXTENDED MIX)"
    assert [tuple(r) for r in con.execute("SELECT stem, source FROM lossy_sourced")] == [(stem, "Opus ~130 kbps")]
    filing.retire(con, paths, catalog.Catalog.from_db(con).entries[0], "replaced by hand")
    assert events(con)[-1]["action"] == "retired" and events(con)[-1]["fake"] == 1
    assert con.execute("SELECT count(*) FROM lossy_sourced").fetchone()[0] == 0


def test_the_card_names_the_download_by_its_tags(env, monkeypatch) -> None:
    con, paths, _ = env
    want = add_song(con, "spotify:tk", "TEKKNO", "say it right tekkno", 135)
    search_hit(con, paths, download(paths, "a.wav", 200), want, "say it right tekkno", "soulseek")
    tags = {"credit": ("BananaCar", "say it right tekkno")}
    monkeypatch.setattr("echolot.library.audio.read_credit", lambda p: tags["credit"])
    assert review.items(con, paths.music)["kept"][0].download == ("BananaCar", "say it right tekkno", True)
    tags["credit"] = ("TEKKNO, BananaCar", "say it right tekkno")
    assert review.items(con, paths.music)["kept"][0].download[2] is False  # names the wanted artist
    tags["credit"] = ("yourdancefloortv", "TEKKNO - say it right tekkno (Official Video)")  # a channel
    assert review.items(con, paths.music)["kept"][0].download[2] is False
    tags["credit"] = ("", "")  # no tags: the name it was downloaded as
    assert review.items(con, paths.music)["kept"][0].download == ("", "say it right tekkno", False)


def test_the_same_file_is_not_kept_twice(env) -> None:
    con, paths, _ = env
    want = add_song(con, "spotify:tk", "TEKKNO", "say it right tekkno", 135)
    first = download(paths, "a.wav", 200)
    twin = paths.inbox("soulseek") / "b.wav"
    twin.write_bytes(first.read_bytes())
    search_hit(con, paths, first, want, "01-01. say it right tekkno", "soulseek")
    search_hit(con, paths, twin, want, "01-01. say it right tekkno", "soulseek")
    assert len(list(paths.inbox("review").rglob("*.wav"))) == 1 and not twin.exists()
    assert len(review.items(con, paths.music)["kept"]) == 1


def test_one_card_per_song_and_taking_one_discards_the_others(env, monkeypatch) -> None:
    """Downloads of one song form one card, closest length first; taking one discards and blocks the rest."""
    con, paths, run = env
    want = add_song(con, "spotify:t78", "T78", "Megator", 330)
    monkeypatch.setattr("echolot.library.audio.read_credit", lambda p: ("T78", "Megator (Original Mix)"))
    for n, seconds in enumerate((419, 390, 400)):
        search_hit(con, paths, download(paths, f"{n}.wav", seconds), want, f"0{n} T78 - Megator", "soulseek")
    [g] = review.groups(review.items(con, paths.music)["kept"])
    assert [i.event["seconds"] for i in g.items] == [390, 400, 419]
    best, *rest = g.items
    review.decide(con, paths.music, best.event["id"], "accept")
    with pytest.raises(review.ConfigError, match="taken already"):
        review.decide(con, paths.music, rest[0].event["id"], "accept")
    g = review.find_group(con, paths.music, best.event["id"])
    assert g.taken.event["id"] == best.event["id"] and len(g.undecided) == 2
    with con:
        con.execute("UPDATE review_decisions SET decided = '2000-01-01T00:00:00'")
    assert review.apply_due(run, con)[-1].endswith("; 2 other downloads of the song discarded")
    assert list(paths.inbox("review").rglob("*.wav")) == []
    assert all(filing.is_blocked(con, "spotify:t78", [f"0{n} T78 - Megator"]) for n in (0, 2))  # 1 was taken


def test_no_match_for_all(env) -> None:
    con, paths, _ = env
    want = add_song(con, "spotify:t78", "T78", "Megator", 330)
    for n, seconds in enumerate((419, 400)):
        search_hit(con, paths, download(paths, f"{n}.wav", seconds), want, f"0{n} T78 - Megator", "soulseek")
    first = review.items(con, paths.music)["kept"][0].event["id"]
    g = review.discard_all(con, paths.music, first)
    assert {r[0] for r in con.execute("SELECT decision FROM review_decisions")} == {"discard"} and len(g.items) == 2
    assert review.find_group(con, paths.music, first).undecided == []


def test_a_close_match_whose_title_reduces_to_the_songs(env) -> None:
    """A close match whose title reduces to the song's ("(Original Mix)") gets its length in the link, so
    the song keeps to this file and not the radio version."""
    con, paths, run = env
    want = add_song(con, "spotify:t78", "T78", "Megator", 330)
    search_hit(con, paths, download(paths, "a.wav", 419), want, "T78 - Megator (Original Mix)", "soulseek")
    item = review.items(con, paths.music)["kept"][0]
    result = close(con, run, item.event["id"], "T78 - Megator (Original Mix)")
    assert result.endswith(": new T78/T78 - Megator (Original Mix).wav")
    assert song(con, "spotify:t78")["link"] == '["T78", "Megator (Original Mix)", 419]'
    download(paths, "b.wav", 330).rename(paths.tracks / "T78" / "T78 - Megator.wav")  # the radio version too
    catalog.scan(con, paths.tracks)
    catalog.match_songs(con)  # without the length in the link, both files would fit its title
    assert song(con, "spotify:t78")["file"] == "T78/T78 - Megator (Original Mix).wav"
    review.search_again(con, "spotify:t78")
    catalog.match_songs(con)
    assert song(con, "spotify:t78")["file"] == "T78/T78 - Megator.wav"


def test_a_close_match_takes_any_name_but_another_files(env) -> None:
    """A close-match name is free (a bare title gets the song's artist), but not another library file's."""
    con, paths, _ = env
    want = add_song(con, "spotify:t78", "T78", "Megator", 330)
    (paths.tracks / "T78").mkdir()
    download(paths, "radio.wav", 330).rename(paths.tracks / "T78" / "T78 - Megator.wav")
    catalog.scan(con, paths.tracks)
    other = Evidence("other", "audio differs from the release (0.60)")
    search_hit(con, paths, download(paths, "a.wav", 419), want, "T78 - Megator (Original Mix)", "soulseek", heard=other)
    item = review.items(con, paths.music)["kept"][0]
    with pytest.raises(review.ConfigError, match="already a file in your library"):
        review.decide(con, paths.music, item.event["id"], "close", "T78 - Megator")
    review.decide(con, paths.music, item.event["id"], "close", "Megator (Original Mix)")
    assert con.execute("SELECT name FROM review_decisions").fetchone()[0] == "T78 - Megator (Original Mix)"


def test_a_perfect_match_of_another_length_replaces_the_lossy_copy(env) -> None:
    """An accepted lossless file of another length replaces the lossy copy under its name, not beside it."""
    con, paths, run = env
    want = add_song(con, "spotify:rn", "T78", "Rave Nation", 302)
    lossy = paths.tracks / "T78" / "T78 - Rave Nation.mp3"
    lossy.parent.mkdir()
    lossy.write_bytes(b"not really audio")
    catalog.scan(con, paths.tracks)
    with con:
        con.execute("UPDATE files SET duration = 290, kbps = 128")
    catalog.match_songs(con)
    search_hit(con, paths, download(paths, "a.wav", 341), want, "T78 - Rave Nation", "soulseek")
    item = review.items(con, paths.music)["kept"][0]
    assert decide(con, run, item.event["id"], "accept").endswith(": upgrade T78/T78 - Rave Nation.wav")
    assert not lossy.exists() and list(paths.inbox("replaced").rglob("T78 - Rave Nation.mp3"))
    catalog.match_songs(con)
    assert song(con, "spotify:rn")["file"] == "T78/T78 - Rave Nation.wav"


def test_tags_naming_another_song_need_a_confirmation(env, monkeypatch) -> None:
    """Tags naming another song make the match probable (unless the audio check hears the release); "ok"
    retags the file as the song."""
    con, paths, run = env
    want = add_song(con, "spotify:jw", "Juice WRLD", "Come & Go (with Marshmello)", 205)
    monkeypatch.setattr("echolot.library.audio.read_tags", lambda p: (["Juice WRLD"], "I Want It"))
    name = "Juice WRLD - Come & Go (with Marshmello)"
    search_hit(con, paths, download(paths, "a.wav", 205), want, name, "soulseek")
    e = events(con)[-1]
    assert (e["action"], e["matched"]) == ("new", "probable") and "tags name another song: 'I Want It'" in e["reason"]
    catalog.match_songs(con)
    assert decide(con, run, e["id"], "ok").endswith(": kept, tagged as the song")
    assert tagging.read(paths.tracks / e["path"])["title"] == "Come & Go (with Marshmello)"
    same = Evidence("same", "audio of the release (0.95)")
    search_hit(con, paths, download(paths, "b.wav", 205), want, name, "soulseek", heard=same)
    assert events(con)[-1]["matched"] == "exact"


def test_review_items_are_compared_with_your_copy_once(env, monkeypatch) -> None:
    """Review items are compared once with the library's copy; one for a missing song gets an empty hint."""
    con, paths, _ = env
    want = add_song(con, "spotify:rn", "T78", "Rave Nation", 302)
    lossy = paths.tracks / "T78" / "T78 - Rave Nation.mp3"
    lossy.parent.mkdir()
    lossy.write_bytes(b"not really audio")
    catalog.scan(con, paths.tracks)
    with con:
        con.execute("UPDATE files SET duration = 290, kbps = 128")
    catalog.match_songs(con)
    search_hit(con, paths, download(paths, "a.wav", 341), want, "T78 - Rave Nation", "soulseek")
    gone = add_song(con, "spotify:gone", "Artist C", "Gone Song", 180)
    search_hit(con, paths, download(paths, "b.wav", 200), gone, "Artist C - Gone Song", "soulseek")
    asked = []
    monkeypatch.setattr("echolot.library.identity.compare", lambda a, b: asked.append(b) or "same audio")
    assert review.compare_open(con, paths.music) == 2
    assert asked == [lossy]
    hints = {
        r["song"]: r["compared"] for r in con.execute("SELECT song, compared FROM events WHERE compared IS NOT NULL")
    }
    assert hints == {"spotify:rn": "same audio", "spotify:gone": ""}
    assert review.compare_open(con, paths.music) == 0  # not again


def test_hires_is_made_44_or_48_khz_unless_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 96 kHz FLAC is resampled to 48 kHz, 24 bit; keep_hires leaves it."""
    f = tmp_path / "hires.flac"
    f.write_bytes(b"x")
    monkeypatch.setattr(audio, "stream", lambda path, field: {"codec_name": "flac", "sample_rate": "96000"}[field])
    monkeypatch.setattr(audio, "_flac_ok", lambda path: True)
    monkeypatch.setattr(audio, "damage", lambda path: "")
    monkeypatch.setattr(audio, "spectrum", lambda path: {"verdict": "ok"})
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(audio, "_to_flac", lambda src, dest, *extra: bool(calls.append(extra)) or True)
    audio.prepare(f)
    assert len(calls) == 1 and "48000" in calls[0] and "24" in calls[0]
    calls.clear()
    audio.prepare(f, keep_hires=True)
    assert calls == []


def test_bad_soulseek_files_are_skipped_for_any_song(env) -> None:
    """By name and exact size, for any song: damaged once is skipped for KEEP_DAYS, twice for good; a fake
    FLAC only in upgrades."""
    con = env[0]
    old = (datetime.datetime.now() - datetime.timedelta(days=filing.KEEP_DAYS + 5)).isoformat(timespec="seconds")
    rows = [(old, "damaged", "spotify:a", "once", 0, 100), (old, "damaged", "spotify:a", "twice", 0, 200)]
    rows += [(old, "damaged", "spotify:b", "twice", 0, 200), (old, "duplicate", "spotify:b", "fake", 1, 300)]
    with con:
        con.executemany(
            "INSERT INTO events (ts, action, song, file_name, fake, peer_bytes) VALUES (?, ?, ?, ?, ?, ?)", rows
        )
    before = filing.rejected_before(con, "spotify:c")
    assert not filing.was_rejected(before, "once", 0, 100)  # expired
    assert filing.was_rejected(before, "twice", 0, 200) and not filing.was_rejected(before, "twice", 0, 201)
    assert not filing.was_rejected(before, "fake", 0, 300)  # a missing song takes a fake FLAC
    assert filing.was_rejected(filing.rejected_before(con, "spotify:c", fakes=True), "fake", 0, 300)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_damaged_or_cut_off_flac_is_rejected(tmp_path: Path) -> None:
    """Overwritten or cut-off FLACs are Damaged (and deleted by prepare); a clean one passes, also with a
    tag or junk after its last frame."""
    good = tmp_path / "good.flac"
    tone = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=20", "-ac", "2", "-c:a", "flac", str(good)]
    subprocess.run(tone, check=True)
    assert audio.damage(good) == ""
    data = good.read_bytes()
    middle = len(data) // 2
    (tmp_path / "overwritten.flac").write_bytes(data[:middle] + bytes(24) + data[middle + 24 :])
    (tmp_path / "cut.flac").write_bytes(data[: len(data) // 3])
    assert audio.damage(tmp_path / "overwritten.flac").startswith("damaged FLAC")
    (tmp_path / "tagged.flac").write_bytes(data + b"TAG" + bytes(125))  # an ID3v1 tag appended
    (tmp_path / "junk.flac").write_bytes(data + data[100:4000])  # bytes after the last frame
    assert audio.damage(tmp_path / "tagged.flac") == "" and audio.damage(tmp_path / "junk.flac") == ""
    assert audio.damage(tmp_path / "cut.flac").startswith(("damaged FLAC", "cut-off FLAC"))
    with pytest.raises(audio.Damaged):
        audio.prepare(tmp_path / "overwritten.flac")
    assert not (tmp_path / "overwritten.flac").exists()  # rejected downloads are deleted


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_levels_of_a_file_recorded_at_a_quarter(tmp_path: Path) -> None:
    """A quarter-gain recording (Spotify at 25 % in the mixer) measures 12 dB lower."""
    for name, gain in (("full", 1.0), ("quarter", 0.25)):
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=f=440:d=2,volume={gain * 2:g}",
                "-af",
                "alimiter=limit=1:level=false",
                "-c:a",
                "flac",
                str(tmp_path / f"{name}.flac"),
            ],
            check=True,
        )
    full, quarter = audio.levels(tmp_path / "full.flac"), audio.levels(tmp_path / "quarter.flac")
    assert full[0] is not None and quarter[0] is not None and full[1] is not None and quarter[1] is not None
    assert abs((full[0] - quarter[0]) - 12.0) < 0.3 and abs((full[1] - quarter[1]) - 12.0) < 0.3
    assert audio.levels(tmp_path / "missing.flac") == (None, None)


def _wav(path: Path, bits: int, floating: bool = False, rate: int = 44100, frames: int = 44100) -> Path:
    """A stereo WAV of noise: integer samples of `bits`, or 32-bit float."""
    import random
    import struct

    rng, data = random.Random(bits), bytearray()
    for _ in range(frames * 2):
        v = rng.uniform(-0.7, 0.7)
        if floating:
            data += struct.pack("<f", v)
        else:
            data += int(v * (2 ** (bits - 1) - 1)).to_bytes(bits // 8, "little", signed=True)
    width = 4 if floating else bits // 8
    fmt = struct.pack(
        "<IHHIIHH", 16, 3 if floating else 1, 2, rate, rate * 2 * width, 2 * width, 32 if floating else bits
    )
    path.write_bytes(
        b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + fmt + b"data" + struct.pack("<I", len(data)) + data
    )
    return path


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_wav_becomes_flac_sample_for_sample(tmp_path: Path) -> None:
    """A WAV becomes a sample-exact FLAC and is removed (32 bit stays 32, not ffmpeg's 24); float WAV gives
    32-bit FLAC."""
    for bits in (16, 24, 32):
        wav = _wav(tmp_path / f"song{bits}.wav", bits)
        before = audio.pcm_digest(wav)
        got = audio.exact_flac(wav, audio.stream(wav, "codec_name"))
        assert got.suffix == ".flac" and not wav.exists() and audio.pcm_digest(got) == before
        assert audio.stream(got, "bits_per_raw_sample") == str(bits)
    wav = _wav(tmp_path / "float.wav", 32, floating=True)
    got = audio.exact_flac(wav, audio.stream(wav, "codec_name"))
    assert got.suffix == ".flac" and not wav.exists() and audio.stream(got, "bits_per_raw_sample") == "32"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_an_original_whose_flac_differs_stays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the FLAC's samples differ, the original stays; its cover goes inside the WAV (an ID3 in front of
    RIFF would break it)."""
    wav = _wav(tmp_path / "song.wav", 24)
    digests = iter(["original", "other"])
    monkeypatch.setattr(audio, "pcm_digest", lambda p: next(digests))
    assert audio.exact_flac(wav, "pcm_s24le") == wav and wav.exists() and not wav.with_suffix(".flac").exists()
    monkeypatch.undo()
    before = audio.pcm_digest(wav)
    assert not audio.has_picture(wav) and audio.embed_cover(wav, b"\xff\xd8\xff\xe0jpeg") and audio.has_picture(wav)
    assert wav.read_bytes()[:4] == b"RIFF" and audio.pcm_digest(wav) == before
