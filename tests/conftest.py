import json
from pathlib import Path

import pytest

from echolot import db
from echolot.config import Settings

SOURCES = """
spotify:
  likes: true
  playlists:
    - https://open.spotify.com/playlist/AAA111?si=x   # comment
    - url: https://open.spotify.com/playlist/BBB222
      title: Renamed
      playlist: false
soundcloud:
  user: someone
  likes: true
  playlists:
    - https://soundcloud.com/someone/sets/trance
"""


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def song(sid: str, artist: str, title: str, length: int) -> dict[str, object]:
    return {
        "id": sid,
        "uri": f"spotify:track:{sid}",
        "artist": artist,
        "title": title,
        "album": "",
        "length": length,
    }


@pytest.fixture
def pipeline_dir(tmp_path: Path) -> Path:
    """A small copy of the music-sync pipeline's config directory."""
    root = tmp_path / "pipeline"
    state = root / "state"
    (root / "logs").mkdir(parents=True)
    (root / "sources.yml").write_text(SOURCES, encoding="utf-8")
    write_json(
        state / "spotify-spotify-liked-songs.json",
        [
            song("s1", "Artist A", "First Song", 200),
            song("s2", "Artist B", "Second Song (Original Mix)", 300),
            song("s3", "Artist C", "Gone Song", 180),
        ],
    )
    write_json(state / "spotify-spotify-aaa111.json", [song("s1", "Artist A", "First Song", 200)])
    write_json(state / "spotify-spotify-bbb222.json", [song("s3", "Artist C", "Gone Song", 180)])
    write_json(state / "spotify-unplayable.json", ["s3"])
    write_json(state / "soundcloud-order-soundcloud-likes.json", ["1001", "1002", "1003"])
    write_json(state / "soundcloud-order-soundcloud-someone-trance.json", ["1001"])
    write_json(
        state / "soundcloud-tracks.json",
        {
            "1001": {
                "artist": "Uploader",
                "title": "Trance Tune",
                "duration": "400.1",
                "stem": "Uploader/Uploader - Trance Tune",
            },
            "1002": {
                "artist": "Label",
                "title": "Locked",
                "duration": "250",
                "stem": None,
                "unavailable": "no downloadable format (DRM)",
            },
        },
    )
    write_json(
        state / "playlist-meta.json",
        {
            "Spotify Liked Songs": {"title": "Liked Songs"},
            "spotify-AAA111": {"title": "Playlist A"},
            "SoundCloud Likes": {"title": "SoundCloud Likes"},
            "soundcloud-someone-trance": {"title": "Trance"},
        },
    )
    write_json(state / "attempts.json", {"spotify:s3": {"n": 3, "last": 1790000000, "fb": 0}})
    write_json(
        state / "lossy-sourced.json", {"Artist B/Artist B - Second Song": {"source": "~128 kbps"}}
    )
    write_json(
        state / "library-cache.json",
        {
            "/music/tracks/Artist A/Artist A - First Song.mp3": [10, 0, 201.0, 320],
        },
    )
    events = [
        {
            "ts": "2026-09-26T10:00:00",
            "action": "new",
            "path": "Artist A/Artist A - First Song.mp3",
            "ext": "mp3",
            "bytes": 10,
            "kbps": 320,
            "seconds": 201,
            "source": "soulseek",
        },
        {
            "ts": "2026-09-26T11:00:00",
            "action": "wrong-song",
            "path": "x.flac",
            "source": "soulseek",
            "artist": "Artist C",
            "title": "Gone Song",
            "reason": "artist 'Artist C' not in []",
        },
    ]
    (root / "logs" / "downloads.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    return root


@pytest.fixture
def library_dir(tmp_path: Path) -> Path:
    """Library files (not real audio: durations come from the pipeline cache or are unknown)."""
    root = tmp_path / "tracks"
    for rel in [
        "Artist A/Artist A - First Song.mp3",
        "Artist B/Artist B - Second Song.flac",
        "Uploader/Uploader - Trance Tune.m4a",
        "Other/Other - Song.txt",
    ]:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"0123456789")
    return root


@pytest.fixture
def settings(tmp_path: Path, pipeline_dir: Path, library_dir: Path) -> Settings:
    s = Settings(
        data_dir=tmp_path / "data",
        library_dir=library_dir,
        pipeline_dir=pipeline_dir,
        host="127.0.0.1",
        port=0,
    )
    db.init(s.db_path)
    return s
