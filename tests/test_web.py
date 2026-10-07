import json
import re
import time
import urllib.parse
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from echolot import __version__, db
from echolot.config import Settings
from echolot.jobs import schedule
from echolot.library import filing, history
from echolot.library.filing import Paths
from echolot.services import spotify
from echolot.settings import options
from echolot.web import create_app, stats
from echolot.web import sources as sources_web

OWNER = 1  # conftest: the owner of the small collection


@pytest.fixture
def client(settings: Settings, login: Callable[..., TestClient]) -> TestClient:
    return login(create_app(settings))  # no `with`: the worker thread stays off


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__, "commit": ""}


def test_overview(client: TestClient) -> None:
    html = client.get("/").text
    assert "Playlist A" in html
    assert 'href="/lists/spotify:playlist:BBB222"' in html
    assert "no playlist" in html
    assert "Run now" in html and "Resume" in html  # jobs paused since the takeover


def test_overview_growth_graph(client: TestClient, settings: Settings) -> None:
    """No snapshot: no graph yet; one: from it to now, the songs (shown, as files) and the size (behind the
    toggle). The last point is the value of now, not the last snapshot's."""
    con = db.connect(settings.db_path)
    with con:
        con.execute("DELETE FROM snapshots")
    con.close()
    html = client.get("/").text
    assert "starts with the first hourly snapshot" in html and '<figure class="growth"' not in html
    con = db.connect(settings.db_path)
    history.snapshot(con)
    with con:
        con.execute("UPDATE snapshots SET ts = '2026-09-27T10:00:00Z'")
        con.execute("UPDATE snapshots SET value = 1 WHERE metric = 'user_library_files'")  # (one file then)
    con.close()
    html = client.get("/").text
    assert html.count('<figure class="growth"') == 2 and 'name="growth" value="size"' in html
    assert 'data-panel="size" hidden' in html and '<span class="figure">3 songs</span>' in html  # the owner's 3 files
    assert "&#34;1 songs&#34;" in html and ">+2 songs</span>" in html  # one file at the snapshot, two more since


def test_missing(client: TestClient) -> None:
    html = client.get("/missing").text
    assert "Gone Song" in html and "Locked" in html
    assert "greyed out on Spotify" in html
    html = client.get("/missing", params={"list": "soundcloud:someone/likes"}).text
    assert "Locked" in html and "Gone Song" not in html


def test_list_page(client: TestClient, settings: Settings) -> None:
    con = db.connect(settings.db_path)
    with con:
        con.execute("UPDATE songs SET url = 'https://soundcloud.com/up/trance-tune' WHERE key = 'soundcloud:1001'")
    con.close()
    html = client.get("/lists/soundcloud:someone/sets/trance").text
    assert '<a href="https://soundcloud.com/up/trance-tune" target="_blank" rel="noopener">Trance Tune</a>' in html
    assert client.get("/lists/nope").status_code == 404


def test_a_recording_twice_in_a_list(client: TestClient, settings: Settings) -> None:
    """Spotify lists a recording once per release: a list can hold it twice. The second entry is marked
    the same song (one file, once in the playlist), not shown as a song of its own."""
    rows = [
        {"position": 0, "file": "A/A - Song.flac", "isrc": "X1"},
        {"position": 1, "file": None, "isrc": "Y1"},
        {"position": 2, "file": "A/A - Song.flac", "isrc": "X2"},  # another release, the same file
        {"position": 3, "file": None, "isrc": "Y1"},  # missing twice: the same ISRC
        {"position": 4, "file": "B/B - Other.flac", "isrc": None},
    ]
    assert stats.same_as(rows) == {2: 0, 3: 1}
    con = db.connect(settings.db_path)
    with con:
        file = con.execute("SELECT file FROM songs WHERE key = 'spotify:s1'").fetchone()[0]
        con.execute(
            "INSERT INTO songs (key, service, artist, title, length, file) "
            "VALUES ('spotify:s1b', 'spotify', 'Somebody', 'First Song', 200, ?)",
            (file,),
        )
        con.execute("INSERT INTO list_songs VALUES ('spotify:playlist:AAA111', 1, 'spotify:s1b')")
    con.close()
    html = client.get("/lists/spotify:playlist:AAA111").text
    assert 'class="same"' in html and "same song as #1, another release" in html


def test_activity(client: TestClient) -> None:
    html = client.get("/activity").text
    assert "Artist A – First Song" in html and ">Added</span>" in html and ">Soulseek</span>" in html
    html = client.get("/activity", params={"kind": "rejected"}).text
    assert "Gone Song" in html and "First Song" not in html and ">another artist</span>" in html


@pytest.mark.parametrize(
    ("why", "source", "ext", "was"),
    [  # a FLAC genuine lossless replaced was one made from lossy; one replaced by hand (Replace anyway) can be genuine
        ("replaced by genuine lossless", "soulseek", "mp3", ("lossy-high", 320)),
        ("replaced by genuine lossless", "soulseek", "flac", ("fake", 320)),
        ("replaced by hand", "manual", "opus", ("lossy-high", 320)),
        ("replaced by hand", "manual", "flac", ("lossless", 320)),
    ],
)
def test_activity_upgrade_is_one_entry(settings: Settings, why: str, source: str, ext: str, was: tuple) -> None:
    """An upgrade and the file it replaced: one entry (from -> to), for everyone and for the song's user."""
    con = db.connect(settings.db_path)
    old = f"Artist A/Artist A - First Song.{ext}"
    columns = "ts, action, path, ext, bytes, kbps, reason, source, song, artist, title"
    retired = ("retired", f"/music/inbox/replaced/2026-09-30/{old}", ext, 8000000, 320, f"{why} (was {old})")
    upgrade = ("upgrade", "Artist A/Artist A - First Song.flac", "flac", 30000000, 900, None)
    with con:
        sql = f"INSERT INTO events ({columns}) VALUES ('2026-09-30T10:00:00', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        con.execute(sql, (*retired, None, None, None, None))
        con.execute(sql, (*upgrade, source, "spotify:s1", "Artist A", "First Song"))
    for uid in (None, OWNER):
        first, *rest = stats.activity(con, uid=uid)
        assert (first["label"], first["was"], first["quality"]) == ("Upgraded", was, ("lossless", 900))
        assert first["was_bytes"] == 8000000 and not any(a["label"] == "Removed" for a in rest)
    con.close()


def test_jobs(client: TestClient) -> None:
    response = client.post("/jobs/library/run", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/?ok=")
    assert set(client.app.state.worker.requested) == {"library"}
    assert client.post("/jobs/nope/run").status_code == 404
    html = client.post("/jobs/pause", data={}, headers={"HX-Request": "true"}).text  # resume
    assert 'id="jobs"' in html and "Pause all" in html
    assert "after Spotify and YouTube lists" in html  # New songs search: no schedule, started when there are new songs
    jobs = {j["name"]: j for j in client.get("/api/jobs").json()["jobs"]}
    assert jobs["sync"]["schedule"] == 2 and not jobs["sync"]["running"]
    html = client.post("/jobs/sync/run", headers={"HX-Request": "true"}).text  # shown at once, asked again every second
    assert "starting …" in html and 'hx-trigger="every 1s"' in html


def test_static_stylesheet(client: TestClient) -> None:
    assert client.get("/static/style.css").status_code == 200
    # root-relative: no http:// link on an https page behind a proxy
    assert 'href="/static/style.css?v=' in client.get("/login").text
    assert "immutable" in client.get("/static/style.css?v=1").headers["cache-control"]


def test_sources_page(client: TestClient) -> None:
    html = client.get("/sources").text
    assert "Connect Spotify" in html and "Connect SoundCloud" in html  # no accounts yet
    cards = client.get("/sources/other").text  # followed lists without an account: all of them
    assert "Playlist A" in cards and "Renamed" in cards and "Trance" in cards
    assert 'value="songs" checked' in cards and 'value="playlist" checked' in cards
    assert 'class="mode-legend"' in html and " Playlist</label>" in cards and "Songs + playlist" not in html + cards


def test_found_cards_by_kind(client: TestClient, monkeypatch) -> None:
    class FakeSpotify:
        def __init__(self, con, vault, user_id=None) -> None:
            pass

        def liked_count(self) -> int:
            return 7

        def playlists(self) -> list[dict]:
            p = {"url": "https://open.spotify.com/playlist/x", "songs": 3, "image": None, "readable": True}
            own = p | {"id": "O1", "name": "Mine", "owner": "me", "own": True, "collaborative": False}
            shared = p | {"id": "C1", "name": "Shared", "owner": "Ann", "own": False, "collaborative": True}
            return [own, shared, shared | {"id": "X1", "name": "Hers", "collaborative": False, "readable": False}]

    monkeypatch.setattr(spotify, "Spotify", FakeSpotify)
    sources_web._found.clear()
    html = client.get("/sources/found/spotify").text
    assert 'class="seg kind-filter"' in html and "By others" in html and "Collaborative" in html
    assert html.count('data-kind="own"') == 2 and 'data-kind="other"' in html and "· collaborative" in html
    assert html.count('class="small warn-text"') == 1  # only on the one Spotify withholds
    sources_web._found.clear()


def test_missing_shows_what_was_tried(client: TestClient, settings: Settings) -> None:
    con = db.connect(settings.db_path)
    key = con.execute("SELECT key FROM wanted WHERE file IS NULL AND service = 'spotify' ORDER BY key").fetchone()[0]
    soulseek = {
        "stage": 1,
        "results": 12,
        "fits": 1,
        "rejected": {"another length": 7, "another version": 4},
        "tried": [["Song (Original Mix).mp3", "failed", "no progress (queued at the peer)"]],
    }
    youtube = {"results": 5, "fits": 0, "rejected": {"another length": 5}, "tried": []}
    drm = {"results": 1, "fits": 1, "rejected": {}, "tried": [["Song", "download failed", "DRM-protected"]]}
    near = ["youtube", "Song (Official Visualizer)", 219, "mismatch", ""]
    fallback = {"youtube": youtube, "soundcloud": drm, "near": near}
    with con:
        row = (key, json.dumps(soulseek), json.dumps(fallback))  # song_key, tries, last_try, last_fallback, results
        con.execute("INSERT OR REPLACE INTO attempts VALUES (?, 3, 1, 1, ?, ?)", row)
    con.close()
    html = client.get("/missing").text
    assert "DRM on SoundCloud" in html and "stalls at peers" in html
    assert "Last search: 12 results, 1 fit" in html and "7 another length, 4 another version" in html
    assert "no progress (queued at the peer)" in html and "Kept for review" in html and "3:39" in html


def test_follow_and_stop_following(client: TestClient, settings: Settings, monkeypatch) -> None:
    card = {"key": "spotify:playlist:NEW1", "service": "spotify", "url": "https://open.spotify.com/playlist/NEW1"}
    found = [card | {"name": "New list", "owner": "you", "songs": 12, "image": ""}]
    monkeypatch.setitem(sources_web._found, ("spotify", 1), (time.time(), found))
    html = client.post("/sources/follow", data=card | {"mode": "songs"}, headers={"HX-Request": "true"}).text
    assert 'class="src-card on"' in html and 'value="songs" checked' in html
    con = db.connect(settings.db_path)
    assert con.execute("SELECT playlist FROM sources WHERE key = 'spotify:playlist:NEW1'").fetchone()[0] == 0
    assert con.execute("SELECT title FROM lists WHERE key = 'spotify:playlist:NEW1'").fetchone()[0] == "New list"
    client.post("/sources/follow", data=card | {"mode": "playlist"}, headers={"HX-Request": "true"})
    assert con.execute("SELECT playlist FROM sources WHERE key = 'spotify:playlist:NEW1'").fetchone()[0] == 1
    html = client.post("/sources/follow", data=card | {"mode": "off"}, headers={"HX-Request": "true"}).text
    assert 'class="src-card"' in html and 'value="off" checked' in html
    assert not con.execute("SELECT 1 FROM sources WHERE key = 'spotify:playlist:NEW1'").fetchone()
    likes = {"key": "spotify:likes:1", "service": "spotify", "url": "likes", "name": "Liked Songs", "mode": "off"}
    client.post("/sources/follow", data=likes, headers={"HX-Request": "true"})
    assert con.execute("SELECT enabled FROM sources WHERE key = 'spotify:likes:1'").fetchone()[0] == 0
    con.close()
    assert set(client.app.state.worker.requested) == {"sync"}


def test_add_by_link(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("echolot.web.sources._preview", lambda con, request, service, url: ("Their list", None))
    r = client.post("/sources/add", data={"url": "https://soundcloud.com/other/sets/techno", "mode": "songs"},
                    follow_redirects=False)  # fmt: skip
    assert "Following+Their+list" in r.headers["location"]
    assert "Their list" in client.get("/sources/other").text
    r = client.post("/sources/add", data={"url": "https://example.com/x"}, follow_redirects=False)
    assert "error=" in r.headers["location"]


def test_accounts_page(client: TestClient, settings: Settings) -> None:
    html = client.get("/accounts").text
    assert "developer.spotify.com/dashboard" in html and "http://127.0.0.1:0/accounts/spotify/callback" in html
    r = client.post("/accounts/spotify/app", data={"client_id": "short"}, follow_redirects=False)
    assert "32+characters" in r.headers["location"]
    r = client.post("/accounts/soulseek", data={"user": "me", "password": "secret pw"}, follow_redirects=False)
    assert "ok=" in r.headers["location"]
    assert "user = me\n" in (settings.daemon_dir / "daemon.conf").read_text()  # (the file: test_soulseek)
    # the Spotify login: a pasted address with another state is refused
    r = client.post("/accounts/spotify/paste", data={"url": "http://127.0.0.1:48721/callback?code=x&state=y"},
                    follow_redirects=False)  # fmt: skip
    assert "not+the+address+of+the+last" in r.headers["location"]


def test_spotify_login_over_https(client: TestClient, monkeypatch) -> None:
    """Behind an https proxy the login returns to Echolot; the exchange uses the address the login used."""
    callback = "https://testserver/accounts/spotify/callback"
    html = client.get("https://testserver/accounts").text
    assert callback in html and "Spotify Premium required" in html and "?code=" not in html
    r = client.post("/accounts/spotify/app", data={"client_id": "x" * 32, "client_secret": "s"}, follow_redirects=False)
    assert "ok=" in r.headers["location"]
    r = client.post("https://testserver/accounts/spotify/login", follow_redirects=False)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)
    assert query["redirect_uri"] == [callback]
    used = []
    monkeypatch.setattr(spotify, "exchange", lambda con, vault, code, redirect, uid: used.append((redirect, uid)))
    r = client.get(f"/accounts/spotify/callback?code=c&state={query['state'][0]}", follow_redirects=False)
    assert "Spotify+connected" in r.headers["location"] and used == [(callback, 1)]  # the owner's own login


def test_connection_line(client: TestClient) -> None:
    assert 'hx-get="/accounts/line"' in client.get("/").text
    html = client.get("/accounts/line").text
    assert "Spotify: not connected" in html and "SoundCloud: not connected" in html
    assert "Soulseek: daemon not reachable" in html and " free</span>" in html


def test_a_lost_soulseek_login_shows(client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A daemon that dropped its Soulseek login is red on the overview, not "logs in with the next search"."""
    from echolot.services import soulseek

    con = db.connect(settings.db_path)
    with con:
        options.update(con, options.Soulseek, user="someone")
    con.close()
    state = {"ready": False, "state": "Disconnected", "flags": ["Disconnected"], "version": "", "started": ""}
    monkeypatch.setattr(soulseek.Daemon, "status", lambda self: state)
    assert "Soulseek: disconnected" in client.get("/accounts/line").text


def test_settings_save(client: TestClient, settings: Settings) -> None:
    assert "New songs search" in client.get("/settings").text
    form = {j.name: schedule.when_text(j.default) for j in schedule.JOBS} | {
        "sync": "20",
        "fallback": "0",
        "upgrade": "13:00; sat 10:00",
        "parallel": "3",
        "keep_hires": "1",
    }
    response = client.post("/settings", data=form)
    assert "Settings saved" in response.text
    con = db.connect(settings.db_path)
    rules = schedule.rules(con)
    kept = options.get(con, options.Files).keep_hires
    con.close()
    assert (rules["sync"], rules["fallback"], rules["upgrade"]) == (20, None, ["13:00", "sat 10:00"]) and kept
    assert 'value="3"' in client.get("/settings").text
    bad = client.post("/settings", data=form | {"fallback": "2"})
    assert "at least 60" in bad.text


def test_review(client: TestClient, settings: Settings) -> None:
    from echolot.library import filing

    music = settings.library_dir.parent
    paths = Paths(music)
    kept = music / "inbox" / "review" / "2026-09-27" / "Artist C - Gone Song [soulseek].flac"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"audio")
    other = kept.with_name("Artist C - Gone Song [soulseek] (2).flac")
    other.write_bytes(b"audio")
    con = db.connect(settings.db_path)
    filing.event(con, paths, "new", paths.tracks / "Artist A" / "Artist A - First Song.mp3", song="spotify:s1",
                 artist="Artist A", title="First Song", source="youtube", matched="probable",
                 found="First Song (Official Video)", tries=2)  # fmt: skip
    filing.event(con, paths, "wrong-song", kept, song="spotify:s3", artist="Artist C", title="Gone Song",
                 source="soulseek", found="Gone Song (Club Mix)", reason="title differs")  # fmt: skip
    filing.event(con, paths, "wrong-song", other, song="spotify:s3", artist="Artist C", title="Gone Song",
                 found="Requiem in D minor", reason="artist 'Artist C' not in ['Mozart']")  # fmt: skip
    with con:
        con.execute("INSERT INTO events (ts, action, path, song, artist, title) VALUES "
                    "('2026-09-27T12:00:00', 'wrong-song', '/etc/passwd', 'spotify:s3', 'Artist C', 'Gone Song')")  # fmt: skip
    ids = [r[0] for r in con.execute("SELECT id FROM events WHERE song IS NOT NULL ORDER BY id DESC LIMIT 4")][::-1]
    with con:
        con.execute("""UPDATE songs SET artists = '["Artist C", "Remixer"]' WHERE key = 'spotify:s3'""")
    html = client.get("/review").text
    assert "First Song (Official Video)" in html
    assert 'href="https://open.spotify.com/track/s1" data-app="spotify:track:s1"' in html  # desktop app first
    assert "Gone Song (Club Mix)" in html and "title differs" in html
    assert "Artist C, Remixer – Gone Song" in html  # all the wanted song's artists
    assert html.count("data-volume aria-label") == 1 and "data-mute" in html  # one volume for the page's player
    assert "/etc/passwd" not in html and "Requiem in D minor" not in html  # no near miss
    assert client.get(f"/review/{ids[1]}/audio").content == b"audio"
    assert client.get(f"/review/{ids[2]}/audio").status_code == 404
    r = client.post(f"/review/{ids[0]}", data={"decision": "accept"}, follow_redirects=False)
    assert "error=" in r.headers["location"]  # not a decision for a filed song
    client.post(f"/review/{ids[0]}", data={"decision": "wrong"})
    client.post(f"/review/{ids[1]}", data={"decision": "accept"})
    decided = {r["event_id"]: r["decision"] for r in con.execute("SELECT * FROM review_decisions")}
    assert decided == {ids[0]: "wrong", ids[1]: "accept"}
    html = client.get("/review").text
    assert "applied soon" in html and "Revert" in html
    r = client.post(f"/review/{ids[1]}/revert", follow_redirects=False)
    assert "ok=" in r.headers["location"]
    assert con.execute("SELECT count(*) FROM review_decisions").fetchone()[0] == 1
    html = client.post(f"/review/{ids[1]}", data={"decision": "discard"}, headers={"HX-Request": "true"}).text
    assert html.startswith('<article class="song-review" id="song-spotify-s3">')  # only this song's card
    assert 'class="download settled"' in html and "Revert" in html
    html = client.post(f"/review/{ids[1]}/revert", headers={"HX-Request": "true"}).text
    assert 'class="download"' in html and ">Close match</button>" in html and ">No match</button>" in html
    close = {"decision": "close", "name": "Artist A - First Song"}
    html = client.post(f"/review/{ids[1]}", data=close, headers={"HX-Request": "true"}).text
    assert "already a file in your library" in html and 'role="alert"' in html  # next to the download
    assert 'value="Artist A - First Song"' in html  # the name typed is kept, the line to name it stays open
    close["name"] = " Artist C  -  Gone Song (Club Mix) "
    html = client.post(f"/review/{ids[1]}", data=close, headers={"HX-Request": "true"}).text
    assert "Close match as “Artist C - Gone Song (Club Mix)” · applied soon" in html
    con.close()


def test_every_close_match_button_is_wired(client: TestClient, settings: Settings) -> None:
    """Close match opens a line (data-close-line) whose input and button submit a form with the decision
    close: each id the page refers to exists, so the button cannot silently do nothing."""
    two_kept_downloads(settings)
    html = client.get("/review").text
    lines = re.findall(r'data-close-line="([^"]+)"', html)
    assert lines
    for line in lines:
        block = re.search(rf'<div class="close-line" id="{line}"[^>]*>(.*?)</div>', html, re.S)
        assert block, line
        forms = set(re.findall(r'form="([^"]+)"', block[1]))
        assert len(forms) == 1 and 'name="name"' in block[1]
        assert re.search(rf'<form id="{forms.pop()}"[^>]*>.*?name="decision" value="close"', html, re.S)


def two_kept_downloads(settings: Settings) -> None:
    """Two kept downloads of one song (the fixture library has none)."""
    music = settings.library_dir.parent
    con = db.connect(settings.db_path)
    info = {"song": "spotify:s3", "artist": "Artist C", "title": "Gone Song", "source": "soulseek"}
    for n in (1, 2):
        kept = music / "inbox" / "review" / "2026-10-01" / f"Artist C - Gone Song [soulseek] ({n}).flac"
        kept.parent.mkdir(parents=True, exist_ok=True)
        kept.write_bytes(b"audio" * n)
        filing.event(con, Paths(music), "wrong-song", kept, found=f"Gone Song (Club Mix {n})", reason="differs", **info)
    con.close()


def test_close_matches_on_the_missing_page(client: TestClient, settings: Settings) -> None:
    con = db.connect(settings.db_path)
    path, link = "Artist A/Artist A - First Song (Extended Mix).flac", '["Artist A", "First Song (Extended Mix)"]'
    with con:
        con.execute("INSERT INTO files (path, size, mtime, duration, kbps) VALUES (?, 1, 1, 320, 900)", (path,))
        close = "UPDATE songs SET file = ?, close_match = 1, link = ? WHERE key = 'spotify:s1'"
        con.execute(close, (path, link))
    html = client.get("/missing").text
    assert "Covered by a close match" in html and "Artist A - First Song (Extended Mix)" in html
    assert "· 1 close match<" in client.get("/").text
    r = client.post("/songs/spotify:s1/search", follow_redirects=False)
    assert "ok=" in r.headers["location"]
    song = con.execute("SELECT link, close_match FROM songs WHERE key = 'spotify:s1'").fetchone()
    assert tuple(song) == (None, 0)
    assert con.execute("SELECT tries, last_try FROM attempts WHERE song_key = 'spotify:s1'").fetchone()[1] == 0
    assert "error=" in client.post("/songs/spotify:s1/search", follow_redirects=False).headers["location"]
    con.close()


def test_library_job(client: TestClient, settings: Settings) -> None:
    """The library job writes the playlists (what they hold: test_lists.test_playlists)."""
    import threading

    from echolot.jobs import worker

    playlists = settings.library_dir.parent / "playlists"
    assert playlists.is_dir()  # made at the start, with the other folders Echolot writes
    assert (settings.library_dir.parent / "inbox" / ".ndignore").is_file()  # Navidrome skips the inbox
    (playlists / "My own.m3u").write_text("#EXTM3U\n")
    run = worker.Run(__import__("echolot.jobs.schedule", fromlist=["BY_NAME"]).BY_NAME["library"], settings,
                     client.app.state.vault, "manual")  # fmt: skip
    run.stop = threading.Event()
    message = worker.upkeep(run)
    assert "playlists written" in message
    assert (playlists / "owner" / "Spotify Liked Songs.m3u").exists() and (playlists / "My own.m3u").exists()


def test_the_jobs_card_shows_tasks_with_their_run(client: TestClient, settings: Settings) -> None:
    """One row per task; a running job shows its line (cut, the whole on hover), its progress bar, Stop and
    its live log; a task's button starts its jobs, Stop stops them."""
    from echolot.jobs.worker import Run
    from echolot.settings.vault import Vault

    wk = client.app.state.worker
    run = Run(schedule.BY_NAME["upgrade"], settings, Vault.from_env(settings.data_dir, {}), "schedule")
    long = "Artist – A Very Long Title (Extended Mix) [https://soundcloud.com/some/very/long/link/" + "x" * 120 + "]"
    run.say(f"12 of 250 songs: 2 upgrade, 10 not found · {long}", 12, 250)
    run.note(f"{long}: not found")
    wk.runs["upgrade"] = run
    html = client.get("/jobs").text
    for label in ("New songs", "Missing songs", "FLAC upgrade", "Maintenance (3)"):
        assert label in html
    assert 'aria-valuenow="12"' in html and 'style="width: 4.8%"' in html  # 12 of 250
    assert f'title="12 of 250 songs: 2 upgrade, 10 not found · {long}"' in html  # cut in the line, whole on hover
    assert 'name="names" value="upgrade"' in html and ">Stop</button>" in html and "Live log (1)" in html
    run.say("SoundCloud: listing Likes")  # starts with its step's name: not said twice
    wk.runs["soundcloud"] = run
    assert "SoundCloud</span>" not in client.get("/jobs").text
    del wk.runs["soundcloud"]
    assert client.post("/jobs/stop", data={"names": "upgrade"}).status_code in (200, 303) and run.stop.is_set()
    del wk.runs["upgrade"]
    client.post("/jobs/start", data={"names": "sync,soundcloud"})
    assert {"sync", "soundcloud"} <= set(wk.requested)
    assert client.post("/jobs/start", data={"names": "nope"}).status_code == 404
    settings_html = client.get("/settings").text
    assert "after Spotify and YouTube lists" in settings_html and 'class="schedule-task"' in settings_html


def test_soulseek_backend_choice(client: TestClient, settings: Settings) -> None:
    """slskd or the Sockseek daemon, chosen on the Accounts page; the secret is kept in the vault."""
    form = {
        "backend": "slskd",
        "slskd_url": "http://slskd:5030/",
        "slskd_user": "admin",
        "slskd_secret": "pw",
        "slskd_downloads": "/music/inbox/slskd/",
    }
    r = client.post("/accounts/soulseek/backend", data=form, follow_redirects=False)
    assert "ok=" in r.headers["location"]
    con = db.connect(settings.db_path)
    opts = options.get(con, options.Soulseek)
    assert (opts.backend, opts.slskd_url, opts.slskd_downloads) == ("slskd", "http://slskd:5030", "/music/inbox/slskd")
    assert client.app.state.vault.get(con, "slskd.secret") == "pw"
    con.close()
    html = client.get("/accounts").text
    assert "through slskd" in html and "Soulseek client: slskd" in html
    client.post("/accounts/soulseek/backend", data={"backend": "sockseek"})
    con = db.connect(settings.db_path)
    assert options.get(con, options.Soulseek).backend == "sockseek"
    con.close()


def test_spotify_403_says_to_check_the_users_email(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 403 for a connected account: almost always an account the app's owner did not add (or a wrong
    e-mail address); other errors keep the general hint."""
    from echolot.web import accounts

    error = "Spotify: HTTP 403 for https://api.spotify.com/v1/me"
    monkeypatch.setattr(accounts, "_spotify_status", lambda state, uid: {"connected": False, "error": error})
    html = client.get("/accounts").text
    assert "exactly this account's e-mail address under User Management" in html
    monkeypatch.setattr(
        accounts, "_spotify_status", lambda state, uid: {"connected": False, "error": "Spotify: timeout"}
    )
    html = client.get("/accounts").text
    assert "User Management in the Spotify developer app" not in html and "no longer has Premium" in html
