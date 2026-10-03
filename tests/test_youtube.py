"""YouTube playlists as a source (jobs/lists.youtube): their links, a list read through YouTube Music and
yt-dlp, the songs' names from Spotify where it has them, the videos that no longer play kept in their
list, and the search of a YouTube song (its own video first)."""

import threading
from pathlib import Path

import pytest

from echolot.config import Settings
from echolot.jobs import acquire, lists
from echolot.jobs.schedule import BY_NAME
from echolot.jobs.worker import Run
from echolot.library.filing import Want
from echolot.services import spotify, ytdlp
from echolot.services import youtube as youtube_api
from echolot.settings import sources
from echolot.settings.sources import ConfigError
from echolot.settings.vault import Vault

PID = "PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
URL = f"https://www.youtube.com/playlist?list={PID}"


def test_links() -> None:
    for link in (
        URL,
        f"https://www.youtube.com/watch?v=abc&list={PID}",
        f"https://music.youtube.com/playlist?list={PID}",
        f"https://youtu.be/abc?list={PID}",
    ):
        assert sources.parse_url(link) == ("youtube", URL)
    for link, why in (
        ("https://www.youtube.com/playlist?list=LL", "your liked videos"),
        ("https://www.youtube.com/watch?v=abc&list=RDabc", "Mix"),
        ("https://www.youtube.com/watch?v=abc", "playlist link"),
    ):
        with pytest.raises(ConfigError, match=why):
            sources.parse_url(link)
    config = {"youtube": {"playlists": [{"url": URL, "title": "Mine", "playlist": False}]}}
    assert [(s.key, s.name, s.title, s.playlist) for s in sources.check(config)] == [
        (f"youtube:playlist:{PID}", f"youtube-{PID}", "Mine", False)
    ]
    with pytest.raises(ConfigError, match="youtube: unknown setting 'likes'"):
        sources.check({"youtube": {"likes": True}})


class FakeSpotify:
    """Spotify has Song A (by its names) and Tune (by the upload's words), each with its ISRC."""

    def __init__(self, con, vault, user_id=None) -> None:
        pass

    def search(self, q: str, limit: int = 5) -> list[dict]:
        tracks = {
            "track:Song A artist:Artist A": {
                "name": "Song A",
                "artists": [{"name": "Artist A"}],
                "duration_ms": 201000,
            },
            "Label 003 Artist B Tune": {"name": "Tune", "artists": [{"name": "Artist B"}], "duration_ms": 300500},
        }
        hit = tracks.get(q)
        return [hit | {"album": {"name": "Album"}, "external_ids": {"isrc": f"ISRC-{hit['name']}"}}] if hit else []


@pytest.fixture
def youtube(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> dict:
    """The list as YouTube has it: `order` (yt-dlp, every video), `playing` (YouTube Music's songs), and
    which videos YouTube is asked about (state)."""
    yt: dict = {"order": [], "playing": [], "asked": []}
    song = {
        "v1": {"id": "v1", "title": "Song A", "artists": ["Artist A"], "album": "", "length": 200, "kind": "atv"},
        "v2": {
            "id": "v2",
            "title": "Label 003 - Artist B - Tune (HQ)",
            "artists": ["Label"],
            "album": "",
            "length": 301,
            "kind": "ugc",
        },
        "v3": {"id": "v3", "title": "Other", "artists": ["Artist C"], "album": "", "length": 250, "kind": "omv"},
    }
    monkeypatch.setattr(spotify, "Spotify", FakeSpotify)
    monkeypatch.setattr(lists.time, "sleep", lambda s: None)
    monkeypatch.setattr(ytdlp.YtDlp, "video_ids", lambda self, url, stop: list(yt["order"]))
    playlist = {"title": "Tunes", "image": "https://img/tunes"}
    monkeypatch.setattr(youtube_api, "playlist", lambda pid: playlist | {"songs": [song[v] for v in yt["playing"]]})
    monkeypatch.setattr(youtube_api, "state", lambda vid: yt["asked"].append(vid) or ("gone", "Video unavailable"))
    return yt


def read(settings: Settings) -> tuple[str, Run]:
    r = Run(BY_NAME["youtube"], settings, Vault.from_env(settings.data_dir, {}), "manual")
    r.stop = threading.Event()
    return lists.youtube(r), r


def test_a_youtube_list(settings: Settings, youtube: dict) -> None:
    con = Run(BY_NAME["youtube"], settings, Vault.from_env(settings.data_dir, {}), "manual").connect()
    sources.add_list(con, 1, URL)
    assert sources.as_config(con, 1)["youtube"] == {"playlists": [URL]}  # (the configuration file)
    youtube["order"], youtube["playing"] = ["v1", "v2", "v3", "v4"], ["v1", "v2", "v3"]  # v4: never seen playing
    message, run = read(settings)
    assert message.startswith("1 lists, 1 changed") and "search_new" in run.after
    names = "SELECT key, artist, title, album, length, isrc, url FROM songs WHERE service = 'youtube' ORDER BY key"
    assert [tuple(r) for r in con.execute(names)] == [
        ("youtube:v1", "Artist A", "Song A", "Album", 201.0, "ISRC-Song A", "https://www.youtube.com/watch?v=v1"),
        ("youtube:v2", "Artist B", "Tune", "Album", 300.5, "ISRC-Tune", "https://www.youtube.com/watch?v=v2"),
        ("youtube:v3", "Artist C", "Other", "", 250.0, None, "https://www.youtube.com/watch?v=v3"),  # not on Spotify
    ]
    listed = "SELECT song_key FROM list_songs WHERE list_key = ? ORDER BY position"
    assert [r[0] for r in con.execute(listed, (f"youtube:playlist:{PID}",))] == [
        "youtube:v1",
        "youtube:v2",
        "youtube:v3",
    ]
    assert con.execute("SELECT title, cover_url FROM lists WHERE service = 'youtube'").fetchone()[:] == (
        "Tunes",
        "https://img/tunes",
    )
    assert youtube["asked"] == [] and not con.execute("SELECT 1 FROM changes").fetchone()  # the first reading
    assert {r[0] for r in acquire._missing(con)} >= {"youtube:v1", "youtube:v2", "youtube:v3"}  # searched

    youtube["order"], youtube["playing"] = ["v1", "v2", "v4"], ["v1"]  # v2 deleted (it stays), v3 taken out
    assert read(settings)[0].startswith("1 lists, 1 changed")
    assert [r[0] for r in con.execute(listed, (f"youtube:playlist:{PID}",))] == ["youtube:v1", "youtube:v2"]
    changes = "SELECT song_key, change, detail FROM changes ORDER BY id"
    assert [tuple(r) for r in con.execute(changes)] == [
        ("youtube:v3", "removed", None),
        ("youtube:v2", "gone", "Video unavailable"),
    ]
    assert youtube["asked"] == ["v2"]  # v4 has no names: nothing to tell
    assert read(settings)[0].startswith("1 lists, 0 changed") and youtube["asked"] == ["v2"]  # known gone
    con.close()


def test_a_youtube_song_is_searched_with_its_own_video_first(settings: Settings, monkeypatch) -> None:
    class Ydl:
        def __init__(self) -> None:
            self.fetched: list[str] = []

        def meta(self, url: str, stop) -> dict:
            return {"title": "Artist A - Song A", "uploader": "Artist A", "duration": 201, "formats": [{}]}

        def search(self, query: str, site: str, stop) -> list[dict]:
            other = {
                "url": "https://www.youtube.com/watch?v=x",
                "title": "Artist A - Song A",
                "uploader": "Fan",
                "duration": 201,
            }
            again = {
                "url": "https://www.youtube.com/watch?v=v1",
                "title": "Artist A - Song A",
                "uploader": "Artist A",
                "duration": 201,
            }
            return [other, again] if site == "youtube" else []

        def fetch(self, url: str, dest: Path, stop) -> tuple[None, str]:
            self.fetched.append(url)
            return None, "failed"

    run = Run(BY_NAME["fallback"], settings, Vault.from_env(settings.data_dir, {}), "manual")
    run.stop = threading.Event()
    con = run.connect()
    want = Want("Artist A", "Song A", 201, "youtube:v1")
    listed = "https://www.youtube.com/watch?v=v1"
    ydl = Ydl()
    action, report = acquire._fallback_song(run, con, ydl, want, 1, True, listed)  # type: ignore[arg-type]
    con.close()
    assert action == "not found" and ydl.fetched[0] == listed
    assert report["listed"] == ["Artist A - Song A", "download failed", "failed"]
    assert report["youtube"]["results"] == 1  # not its own video again, however the search writes it


def test_an_uploads_names() -> None:
    """From the video's title, as uploaders write them (a public hardstyle list)."""
    cases = {
        ("Scantraxx", "Squaretraxx 003 - Frontliner & Ruthless - One Bananaz (HQ)"): (
            ["Frontliner", "Ruthless"],
            "One Bananaz",
        ),
        ("Hardstylealbums", "D-Block & S-Te-Fan - Supernova"): (["D-Block", "S-Te-Fan"], "Supernova"),
        ("Headhunterz", "WozNotWoz [Full - HD HQ]"): (["Headhunterz"], "WozNotWoz"),
        ("Dj Thera", "Degos & Re-Done - This Mind (THER-108) Official Video"): (["Degos", "Re-Done"], "This Mind"),
        ("Adaro", "Adaro ft MC Renegade - The House Of Wax (bootleg)"): (
            ["Adaro", "MC Renegade"],
            "The House Of Wax (bootleg)",
        ),
        ("Label", "Artist - Title - Extended Mix"): (["Artist"], "Title - Extended Mix"),
    }
    for (channel, title), names in cases.items():
        assert lists._upload_names(channel, title) == names


def test_follow_a_youtube_list_by_its_link(settings: Settings, login, monkeypatch: pytest.MonkeyPatch) -> None:
    from echolot.web import create_app
    from echolot.web import sources as sources_page

    monkeypatch.setattr(sources_page, "_preview", lambda con, request, service, url: ("Tunes", None))
    app = create_app(settings)
    client = login(app)
    r = client.post(
        "/sources/add", data={"url": f"https://music.youtube.com/playlist?list={PID}"}, follow_redirects=False
    )
    assert r.status_code == 303 and "Following+Tunes" in r.headers["location"]
    assert app.state.worker.requested == {"youtube": None}  # read now
    assert "YouTube" in client.get("/sources/other").text


def test_a_refused_stream_is_asked_for_once_more(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """YouTube now and then refuses a stream (HTTP 403): the download is tried once more, other errors not."""
    answers = [(None, "unable to download video data: HTTP Error 403: Forbidden"), (tmp_path / "a.opus", "")]
    tries = []
    monkeypatch.setattr(ytdlp.YtDlp, "_fetch", lambda self, url, dest, stop: tries.append(url) or answers.pop(0))
    ydl = ytdlp.YtDlp(tmp_path)
    assert ydl.fetch("https://www.youtube.com/watch?v=x", tmp_path / "a", threading.Event()) == (
        tmp_path / "a.opus",
        "",
    )
    answers[:] = [(None, "DRM-protected")]
    assert ydl.fetch("u", tmp_path / "b", threading.Event()) == (None, "DRM-protected") and len(tries) == 3
