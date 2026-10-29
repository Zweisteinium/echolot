"""Discogs: what it lists of the library's releases (jobs/discogs, services/discogs and beatport), off by
default, the page, the suggestion dialog and the settings."""

import io
import json
import sqlite3
import threading
import urllib.error
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from echolot import db
from echolot.config import Settings
from echolot.jobs import discogs as job
from echolot.jobs.schedule import BY_NAME, TASK_OF
from echolot.jobs.worker import Run
from echolot.services import beatport, discogs
from echolot.settings import options, vault
from echolot.web import create_app

STORE = {
    "id": 77,
    "name": "First EP",
    "artists": ["Artist A"],
    "label": "Trance Label",
    "catno": "TL001",
    "upc": "123456789012",
    "date": "2024-04-19",
    "cover": "https://geo-media.beatport.com/image_size/1400x1400/x.jpg",
    "genre": "Trance (Main Floor)",
    "url": "https://www.beatport.com/release/first-ep/77",
    "tracks": [
        {"name": "First Song", "mix": "Extended Mix", "artists": ["Artist A"], "remixers": [], "seconds": 391},
        {"name": "Other", "mix": "Remix", "artists": ["Artist A", "Guest"], "remixers": ["Guest"], "seconds": 300},
    ],
}


def release(title: str, artists: str = "Artist A", formats: tuple[str, ...] = ("File", "MP3")) -> discogs.Release:
    return discogs.Release(1, artists, title, formats, "Trance Label", "TL001", "2024")


def album(title: str = "First EP", artists: tuple[str, ...] = ("Artist A",)) -> job.Album:
    return job.Album(job.album_key(artists[0], title), artists[0], title, "2024-04-19", 2, list(artists))


def test_titles_and_artists_decide_what_discogs_lists() -> None:
    assert job.same_title("First EP", "first ep") == "exact"
    assert job.same_title("Gucci (Extended Mix)", "Gucci") == "close" == job.same_title("Viento", "Viento EP")
    assert job.same_title("Gucci (Remixes)", "Gucci") == ""
    a = album()
    assert job.match(a, [release("First EP", formats=("Vinyl", '12"'))])[0] == "digital"
    state, found = job.match(a, [release("First", formats=("CD",)), release("First EP"), release("Other")])
    assert (state, found["exact"], found["formats"]) == ("listed", True, ["File", "MP3"])
    assert job.match(a, [release("First EP", "Someone Else")]) == ("missing", None)  # another artist's
    assert job.match(a, [release("First EP", "Various")])[0] == "listed"
    assert discogs.names("Artist A (2), Guest* & Other") == ["Artist A", "Guest", "Other"]


def test_the_store_release_with_the_same_title_and_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    hits = [
        {"release_id": 1, "release_name": "First EP", "artists": [{"artist_name": "Nobody"}], "track_count": 2},
        {"release_id": 2, "release_name": "First EP (Extended Mix)", "artists": [{"artist_name": "Artist A"}]},
        {"release_id": 3, "release_name": "First EP", "artists": [{"artist_name": "Artist A"}], "track_count": 5},
        {"release_id": 4, "release_name": "First EP", "artists": [{"artist_name": "Artist A"}], "track_count": 2},
    ]
    monkeypatch.setattr(beatport, "search", lambda q: hits)
    monkeypatch.setattr(beatport, "release", lambda rid: {"id": rid})
    assert job.in_store(album()) == {"id": 4}
    monkeypatch.setattr(beatport, "search", lambda q: hits[:1])
    assert job.in_store(album()) == {}


def page(queries: list) -> bytes:
    data = {"props": {"pageProps": {"dehydratedState": {"queries": queries}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'.encode()


def test_beatport_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(beatport, "PAUSE", 0)
    rel = {"id": 77, "name": "First EP", "slug": "first-ep", "artists": [{"name": "Artist A"}], "upc": "1"}
    rel |= {"label": {"name": "Trance Label"}, "catalog_number": "TL001", "new_release_date": "2024-04-19"}
    track = {"name": "First Song", "mix_name": "Extended Mix", "length_ms": 391747, "genre": {"name": "Trance"}}
    tracks = {"queryKey": ["tracks", {}], "state": {"data": {"results": [track]}}}
    answers = {
        "https://www.beatport.com/release/-/77": page([{"queryKey": ["release-77"], "state": {"data": rel}}, tracks]),
        "https://www.beatport.com/search/releases?q=a+b": b"<html>Just a moment...</html>",
    }

    def urlopen(request, timeout=0):
        if request.full_url == "https://www.beatport.com/search/releases?q=busy":
            raise urllib.error.HTTPError(request.full_url, 429, "busy", {}, None)
        return io.BytesIO(answers[request.full_url])

    monkeypatch.setattr(beatport.urllib.request, "urlopen", urlopen)
    r = beatport.release(77)
    assert (r["catno"], r["url"], r["genre"]) == ("TL001", "https://www.beatport.com/release/first-ep/77", "Trance")
    first = r["tracks"][0]
    assert (first["name"], first["mix"], first["seconds"], first["remixers"]) == ("First Song", "Extended Mix", 392, [])
    for q in ("a b", "busy"):  # a page without its data (a bot check) or a refusal: stop, not get around
        with pytest.raises(beatport.Refused):
            beatport.search(q)


@pytest.fixture
def library(settings: Settings) -> Settings:
    """Artist A's First Song on the release First EP, in the library; Discogs on, with a token."""
    con = db.connect(settings.db_path)
    with con:
        con.execute("UPDATE songs SET album = 'First EP', released = '2024-04-19', tracks = 2 WHERE key = 'spotify:s1'")
        con.execute("UPDATE songs SET album = 'Second' WHERE key = 'spotify:s2'")
        options.update(con, options.Discogs, enabled=True, user="someone")
        vault.Vault.from_env(settings.data_dir, {}).set(con, discogs.TOKEN, "secret")
    con.close()
    return settings


def make_run(settings: Settings) -> Run:
    r = Run(BY_NAME["discogs"], settings, vault.Vault.from_env(settings.data_dir, {}), "manual")
    r.stop = threading.Event()
    return r


def ask(settings: Settings, list_key: str, again: bool = False) -> None:
    con = db.connect(settings.db_path)
    with con:
        db.set_meta(con, job.REQUEST, json.dumps({"list": list_key, "again": again}))
    con.close()


def rows(settings: Settings) -> dict[str, sqlite3.Row]:
    con = db.connect(settings.db_path)
    try:
        return {r["album"]: r for r in con.execute("SELECT * FROM discogs")}
    finally:
        con.close()


def test_a_check_looks_at_a_lists_releases_once(library: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    searched = []

    def search(token: str, artist: str, title: str) -> list[discogs.Release]:
        searched.append((token, title))
        return [release("Second", "Artist B")] if title == "Second" else []

    monkeypatch.setattr(discogs, "search", search)
    monkeypatch.setattr(job, "in_store", lambda a: STORE)
    ask(library, "spotify:likes:1")
    assert job.run(make_run(library)) == "2 releases: 1 on Discogs, 0 only in other formats, 1 missing"
    found = rows(library)
    assert found["artist a|first ep"]["state"] == "missing" and json.loads(found["artist a|first ep"]["store"]) == STORE
    assert found["artist b|second"]["store"] is None  # (listed: Beatport not asked)
    assert searched == [("secret", "First EP"), ("secret", "Second")]
    assert job.run(make_run(library)) == "nothing new"
    ask(library, "spotify:playlist:AAA111", again=True)  # only First Song's release; Beatport refuses now

    def refused(a: job.Album) -> dict:
        raise beatport.Refused("Beatport refused (HTTP 429)")

    monkeypatch.setattr(job, "in_store", refused)
    assert job.run(make_run(library)).startswith("1 releases")
    assert json.loads(rows(library)["artist a|first ep"]["store"]) == STORE  # the last one kept
    assert "discogs" not in TASK_OF and BY_NAME["discogs"].default is None  # not on the Jobs page, no schedule


def test_off_by_default_and_a_refused_token_stops(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    assert not options.get(db.connect(settings.db_path), options.Discogs).enabled
    assert job.run(make_run(settings)) == "Discogs is off"


def test_a_refused_token_stops_the_check(library: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def search(token: str, artist: str, title: str) -> list:
        raise discogs.Refused("Discogs refused the token")

    monkeypatch.setattr(discogs, "search", search)
    ask(library, "spotify:likes:1")
    assert job.run(make_run(library)).startswith("stopped: Discogs refused the token") and not rows(library)


def test_the_page_and_the_suggestion(library: Settings, login: Callable[..., TestClient]) -> None:
    con = db.connect(library.db_path)
    with con:
        known = [
            ("artist a|first ep", "First EP", "missing", json.dumps(STORE)),
            ("artist b|second", "Second", "listed", None),
        ]
        for key, title, state, store in known:
            con.execute(
                "INSERT INTO discogs (album, artist, title, state, store, genre, checked) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, "Artist", title, state, store, "Neo Trance", "2026-10-29T12:00:00"),
            )
    con.close()
    client = login(create_app(library))
    text = client.get("/discogs").text
    assert "First EP" in text and "TL001" in text and ">Second<" not in text  # (listed: not to add)
    assert "Second" in client.get("/discogs?show=all").text
    assert "First EP" not in client.get("/discogs?genre=Techno").text
    assert "First EP" not in client.get("/discogs?list=spotify:playlist:BBB222").text
    assert "Discogs · 1" in client.get("/library").text
    dialog = client.get("/discogs/release", params={"album": "artist a|first ep"}).text
    notes = "Release details from the Beatport release page: https://www.beatport.com/release/first-ep/77"
    shown = ["Trance Label", "TL001", "123456789012", "First Song (Extended Mix)", "6:31", "Remix – Guest", notes]
    for text in [*shown, 'data-copy="#dc-catno"']:
        assert text in dialog, text
    assert ">Neo Trance<" in dialog and ">Trance<" in dialog and "<b>Electronic</b>" in dialog  # (yours, Beatport's)
    assert ">320 kbps<" in dialog and "Description: <b>EP</b>" in dialog and "Optional, and what stays empty" in dialog
    assert client.get("/discogs/release", params={"album": "nobody|nothing"}).status_code == 404
    con = db.connect(library.db_path)
    with con:  # Second: on Discogs only as a CD, no Beatport release
        found = json.dumps({"url": "https://www.discogs.com/release/9", "formats": ["CD"], "label": "", "catno": ""})
        con.execute(
            "UPDATE discogs SET state = 'digital', found = ?, store = '{}' WHERE album = 'artist b|second'", (found,)
        )
    con.close()
    bare = client.get("/discogs/release", params={"album": "artist b|second"}).text
    assert "No release on Beatport found" in bare and ">Not On Label (Artist B Self-released)<" in bare
    assert "only as" in bare and "<b>Electronic</b>" in bare and ">Neo Trance<" in bare  # (your genre still counts)
    viewer = login(create_app(library), "guest", admin=False)
    assert viewer.get("/discogs").status_code == 200 and 'action="/discogs/check"' not in viewer.get("/discogs").text
    assert viewer.post("/discogs/check", data={"list": "spotify:likes:1"}, follow_redirects=False).status_code == 403


def test_what_the_suggestion_says(library: Settings) -> None:
    from echolot.web.discogs import styles, suggestion

    row = {"store": json.dumps(STORE | {"label": "DistroKid", "catno": "123456789012"}), "genre": ""}
    s = suggestion(album(), row)
    assert (s["label"], s["catno"]) == ("Not On Label (Artist A Self-released)", "none")
    assert suggestion(album(), {"store": json.dumps(STORE | {"catno": STORE["upc"]}), "genre": ""})["catno"] == "none"
    assert styles("Neo Trance", "Hard Dance / Hardcore / Neo Rave") == ["Neo Trance"]  # (Beatport's bucket: too vague)
    assert styles("Tekk", "Techno (Peak Time / Driving)") == ["Techno"] and styles("Vocal Trance") == []
    rap = suggestion(album(), {"store": json.dumps(STORE | {"genre": "Dance / Pop"}), "genre": "Trap"})
    assert (rap["styles"], rap["genres"]) == (["Trap"], ["Hip Hop"])
    nothing = suggestion(album(), {"store": "{}", "genre": "Tekk"})  # no store release: no label, no genre known
    assert (nothing["label"], nothing["genres"], nothing["self_released"]) == (
        "",
        [],
        "Not On Label (Artist A Self-released)",
    )


def test_settings_turn_it_on_with_a_token(settings: Settings, login, monkeypatch: pytest.MonkeyPatch) -> None:
    client = login(create_app(settings))
    assert "Off." in client.get("/discogs").text
    r = client.post("/settings/discogs", data={"enabled": "1"}, follow_redirects=False)
    assert "needs+a+token" in r.headers["location"]

    def identity(token: str) -> str:
        if token != "good":
            raise discogs.Refused("Discogs refused the token")
        return "someone"

    monkeypatch.setattr(discogs, "identity", identity)
    r = client.post("/settings/discogs", data={"enabled": "1", "token": "bad"}, follow_redirects=False)
    assert "refused" in r.headers["location"]
    client.post("/settings/discogs", data={"enabled": "1", "token": "good"})
    con = db.connect(settings.db_path)
    try:
        assert options.get(con, options.Discogs).user == "someone"
        assert vault.Vault.from_env(settings.data_dir, {}).get(con, discogs.TOKEN) == "good"
    finally:
        con.close()
    assert '"good"' not in client.get("/settings").text and ">someone<" in client.get("/settings").text
    client.post("/settings/discogs", data={})  # off: the token stays
    assert "Off." in client.get("/discogs").text
