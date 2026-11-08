"""The library scan, song matching and the pages' queries, on the small collection of conftest.seed."""

import os
from pathlib import Path

import pytest

from echolot import db
from echolot.config import Settings
from echolot.library import catalog
from echolot.web import stats


@pytest.fixture
def con(settings: Settings):
    con = db.connect(settings.db_path)
    yield con
    con.close()


def test_lists_in_config_order(con) -> None:
    rows = con.execute("SELECT key, title, playlist FROM lists ORDER BY position").fetchall()
    assert [tuple(r) for r in rows] == [
        ("spotify:likes:1", "Liked Songs", 1),
        ("spotify:playlist:AAA111", "Playlist A", 1),
        ("spotify:playlist:BBB222", "Renamed", 0),
        ("soundcloud:someone/likes", "SoundCloud Likes", 1),
        ("soundcloud:someone/sets/trance", "Trance", 1),
    ]


def test_best_quality_leaves_out_what_cannot_be_had(con) -> None:
    def liked() -> tuple[int, int, int]:
        row = next(r for r in stats.lists(con) if r["key"] == "spotify:likes:1")
        return row["songs"], row["best"], row["unobtainable"]

    # s1 an MP3, s2 a FLAC made from a lossy file, s3 missing and greyed out on Spotify
    assert liked() == (3, 0, 1)
    with con:  # a recording proved Spotify has nothing better of s2
        con.execute(
            "INSERT INTO spotify_best (song_key, uri, reason, since) "
            "VALUES ('spotify:s2', 'spotify:track:s2', 'lossy', '2026-10-18T20:00:00')"
        )
    assert liked() == (3, 1, 1)
    with con:  # Spotify plays it as another release now: that one may be better
        con.execute(
            "INSERT INTO availability (song_key, state, since, checked, detail) "
            "VALUES ('spotify:s2', 'replaced', '', '2026-10-18', 'spotify:x')"
        )
    assert liked() == (3, 0, 1)


def test_songs_matched_to_library_files(con) -> None:
    songs = {r["key"]: r for r in con.execute("SELECT * FROM songs")}
    assert set(songs) == {"spotify:s1", "spotify:s2", "spotify:s3", "soundcloud:1001", "soundcloud:1002"}
    assert songs["spotify:s1"]["file"] == "Artist A/Artist A - First Song.mp3"
    assert songs["spotify:s2"]["file"] == "Artist B/Artist B - Second Song.flac"  # (Original Mix)
    assert songs["soundcloud:1001"]["file"] == "Uploader/Uploader - Trance Tune.m4a"
    assert songs["spotify:s3"]["file"] is None
    assert songs["spotify:s3"]["unavailable"] == "greyed out on Spotify"
    assert songs["soundcloud:1002"]["unavailable"].startswith("not downloadable")


def test_files_and_quality(con) -> None:
    quality = dict(con.execute("SELECT path, quality FROM files").fetchall())
    assert quality == {
        "Artist A/Artist A - First Song.mp3": "lossy-low",  # not real audio: 0 kbps
        "Artist B/Artist B - Second Song.flac": "fake",  # made from a lossy file
        "Uploader/Uploader - Trance Tune.m4a": "lossy-low",
    }


def test_missing_and_overview(con) -> None:
    assert {r["key"] for r in stats.missing(con)} == {"spotify:s3", "soundcloud:1002"}
    only = stats.missing(con, "spotify:playlist:BBB222")
    assert [(r["key"], r["tries"], r["in_lists"]) for r in only] == [("spotify:s3", 3, "Liked Songs #3 · Renamed #1")]
    o = stats.overview(con)
    assert (o["files"], o["wanted"], o["have"], o["not_found"]) == (3, 5, 3, 1)
    assert [(r["key"], r["songs"], r["have"]) for r in o["lists"]][:2] == [
        ("spotify:likes:1", 3, 2),
        ("spotify:playlist:AAA111", 1, 1),
    ]


def test_scan_notices_changes(con, settings: Settings) -> None:
    (settings.library_dir / "Artist A" / "Artist A - First Song.mp3").unlink()
    new = settings.library_dir / "Artist C" / "Artist C - Gone Song.flac"
    new.parent.mkdir()
    new.write_bytes(b"x")
    catalog.refresh(con, settings.library_dir)
    songs = dict(con.execute("SELECT key, file FROM songs").fetchall())
    assert songs["spotify:s1"] is None
    assert songs["spotify:s3"] == "Artist C/Artist C - Gone Song.flac"


def test_scan_keeps_data_when_library_vanishes(con, settings: Settings, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RuntimeError):
        catalog.scan(con, empty)
    with pytest.raises(FileNotFoundError):
        catalog.scan(con, tmp_path / "nowhere")
    assert con.execute("SELECT count(*) FROM files").fetchone()[0] == 3


def test_scan_uses_known_durations(con, settings: Settings) -> None:
    path = settings.library_dir / "Uploader" / "Uploader - Trance Tune.m4a"
    st = path.stat()
    known = {"Uploader/Uploader - Trance Tune.m4a": (st.st_size + 1, int(st.st_mtime), 400.0, 256)}
    path.write_bytes(path.read_bytes() + b"1")  # changed: size now matches `known`
    os.utime(path, (st.st_atime, st.st_mtime))
    catalog.scan(con, settings.library_dir, known)
    row = con.execute("SELECT duration, kbps FROM files WHERE path LIKE 'Uploader/%'").fetchone()
    assert tuple(row) == (400.0, 256)


def test_lossy_tiers_by_format() -> None:
    """A lossy tier by how the format sounds at its bitrate (catalog.LIMITS): YouTube's Opus at 130 kbps is
    high, an MP3 at 128 low, SoundCloud's AAC at 160 medium."""
    assert [catalog.lossy_tier("opus", k) for k in (130, 96, 64)] == ["lossy-high", "lossy-mid", "lossy-low"]
    assert [catalog.lossy_tier("mp3", k) for k in (320, 245, 192, 128)] == ["lossy-high"] * 2 + [
        "lossy-mid",
        "lossy-low",
    ]
    assert [catalog.lossy_tier("aac", k) for k in (256, 160, 96)] == ["lossy-high", "lossy-mid", "lossy-low"]
    assert catalog.Entry("A/A - T.opus", 0, 130, False).quality == "lossy-high"
    assert catalog.Entry("A/A - T.webm", 0, 130, False).quality == "lossy-high"  # (Opus too)


def test_a_fake_flac_counts_in_its_sources_tier() -> None:
    """A fake FLAC's own quality stays 'fake' (upgrades, review, recording decide by it); the pages count it
    in the tier of the lossy file it was made from, as the checks estimate it."""
    estimates = {
        "~256 kbps / V0": "lossy-high",
        "~160-192 kbps": "lossy-mid",
        "~128 kbps": "lossy-low",
        "MP3 ~311 kbps": "lossy-high",
        "Opus ~131 kbps": "lossy-high",
        "AAC ~170 kbps": "lossy-mid",
    }
    assert {s: catalog.source_tier(s) for s in estimates} == estimates
    assert catalog.source_tier("") is None
    fake = catalog.Entry("A/A - T.flac", 0, 900, True, "Opus ~131 kbps")
    assert (fake.quality, fake.tier) == ("fake", "lossy-high")
    assert catalog.Entry("A/A - T.flac", 0, 900, True).tier == "lossy-mid"  # (no estimate: medium)
    opus = catalog.Entry("A/A - T.opus", 0, 126, True)  # a lossy file under a fake FLAC's name is what it is
    assert (opus.fake, opus.quality, opus.tier) == (False, "lossy-high", "lossy-high")


def test_a_fake_without_its_own_estimate_takes_the_lossy_checks(con) -> None:
    """A fake whose source was not noted when it was filed takes the Lossy check's: lossprint's format and
    bitrate where it is sure, else the spectrum's."""
    with con:
        con.execute("UPDATE lossy_sourced SET source = '' WHERE stem = 'Artist B/Artist B - Second Song'")
        con.execute(
            "INSERT INTO lossy_checks (path, size, mtime, md5, spectrum, lossprint, verdict, checked) VALUES "
            "('Artist B/Artist B - Second Song.flac', 10, 0, '', '{\"source\": \"~128 kbps\"}', "
            '\'{"p": 0.99, "encoder": "opus", "kbps": 131.2}\', \'lossy\', \'\')'
        )
    catalog.match_songs(con)
    assert tuple(con.execute("SELECT quality, tier FROM files WHERE path LIKE 'Artist B/%'").fetchone()) == (
        "fake",
        "lossy-high",
    )
    with con:
        con.execute('UPDATE lossy_checks SET lossprint = \'{"p": 0.2, "encoder": "opus", "kbps": 131}\'')
    catalog.match_songs(con)
    assert con.execute("SELECT tier FROM files WHERE path LIKE 'Artist B/%'").fetchone()[0] == "lossy-low"  # spectrum's


def test_the_rings_count_files_and_songs(con) -> None:
    """Files: the music folder's files by tier, no missing (they are songs); Songs: the songs on the lists by
    their copy's tier and the missing; both note the fake FLACs counted in their source's tier."""
    o = stats.overview(con)
    assert [k for k, _, _ in o["tiers"]] == ["lossless", "lossy-high", "lossy-mid", "lossy-low"]
    assert sum(n for _, _, n in o["tiers"]) == o["files"] and o["file_fakes"] == 1
    assert dict((k, n) for k, _, n in o["song_tiers"])["missing"] == o["wanted"] - o["have"]
    assert sum(n for _, _, n in o["song_tiers"]) == o["wanted"] and o["song_fakes"] >= 1
    labels = [label for _, label, _ in stats.best_tiers(con)]
    assert labels == ["At their best", "Can get better", "Missing · searched", "Missing · not to be had"]
