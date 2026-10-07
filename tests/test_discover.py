"""Discover: the merged search over Deezer, Apple Music and SoundCloud (library/discover, services, web), and\nin the players (web/subsonic)."""

import json

import pytest

from echolot.library import discover
from echolot.library.discover import Hit
from echolot.services import catalogs
from echolot.services import soundcloud as sc_api


def test_one_release_from_several_sources_is_one_result() -> None:
    """Deezer's "Levels - Radio Edit" and Apple's "Levels (Radio Edit)" are one song; the extended mix and a
    song of the same name 40 s longer are others. The catalogue's names win over an uploader's."""
    hits = [
        Hit("deezer", 0, "Avicii", "Levels - Radio Edit", 199, "Levels"),
        Hit("apple", 1, "Avicii", "Levels (Radio Edit)", 200),
        Hit("soundcloud", 0, "Avicii", "Levels (Radio Edit) [FREE DL]", 198),
        Hit("deezer", 1, "Avicii", "Levels - Extended Mix", 336),
        Hit("apple", 0, "Avicii", "Levels - Radio Edit", 240),  # 40 s longer: another song
        Hit("soundcloud", 1, "Other Artist", "Levels - Radio Edit", 199),
    ]
    results = discover.merge(hits)
    first = results[0]
    assert (first.artist, first.title, first.album) == ("Avicii", "Levels - Radio Edit", "Levels")
    assert [h.source for h in first.sources] == ["deezer", "apple", "soundcloud"]  # three sources: first
    assert len(results) == 4 and sum(len(r.hits) for r in results) == 6
    assert {r.title for r in results[1:]} == {"Levels - Extended Mix", "Levels - Radio Edit"}


def test_a_source_naming_only_the_main_artist_is_the_same_song() -> None:
    """Deezer names one of three artists, Apple Music all; an hour-long mix is no song; invisible blanks go."""
    hits = [
        Hit("deezer", 0, "Aexhy", "Sternschauer (Stars In Your Eyes)\u2800", 277),
        Hit("apple", 0, "1luu, Happysadgirl & Aexhy", "Sternschauer (Stars In Your Eyes)", 277),
        Hit("soundcloud", 0, "Transcend with me", "VICTRONICA Trance Mix", 3660),
    ]
    (one,) = discover.merge(hits)
    assert [h.source for h in one.sources] == ["deezer", "apple"] and one.title.endswith("Eyes)")


def test_more_sources_rank_higher() -> None:
    """A song two catalogues list comes before one only the top of one list has."""
    hits = [Hit("deezer", 0, "A", "Solo", 100), Hit("deezer", 3, "B", "Both", 150), Hit("apple", 2, "B", "Both", 151)]
    assert [r.title for r in discover.merge(hits)] == ["Both", "Solo"]


def test_the_sources_answers_become_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    deezer = {
        "data": [
            {
                "title": "Fade",
                "duration": 264,
                "link": "https://www.deezer.com/track/1",
                "preview": "https://cdn/p.mp3",
                "artist": {"name": "Alan Walker"},
                "album": {"title": "Fade", "cover_medium": "https://img/c.jpg"},
            }
        ]
    }
    monkeypatch.setattr(catalogs, "_get", lambda url: deezer)
    (d,) = catalogs.deezer("alan walker fade")
    assert (d.source, d.artist, d.title, d.seconds, d.preview, d.cover) == (
        "deezer",
        "Alan Walker",
        "Fade",
        264,
        "https://cdn/p.mp3",
        "https://img/c.jpg",
    )
    monkeypatch.setattr(catalogs, "_get", lambda url: {"error": {"message": "Quota limit exceeded"}})
    with pytest.raises(catalogs.CatalogError, match="Quota"):
        catalogs.deezer("x")
    apple = {
        "results": [
            {
                "artistName": "Alan Walker",
                "trackName": "Faded",
                "trackTimeMillis": 212627,
                "collectionName": "Faded - Single",
                "trackViewUrl": "https://music.apple.com/x?uo=4",
                "artworkUrl100": "https://a/100x100bb.jpg",
                "releaseDate": "2015-12-03T08:00:00Z",
            }
        ]
    }
    monkeypatch.setattr(catalogs, "_get", lambda url: apple)
    (a,) = catalogs.apple("faded")
    assert (a.title, a.seconds, a.url, a.cover, a.year) == (
        "Faded",
        212.627,
        "https://music.apple.com/x",
        "https://a/250x250bb.jpg",
        "2015",
    )
    sc = {
        "collection": [
            {
                "title": "Alan Walker - Fade [NCS Release]",
                "duration": 264000,
                "user": {"username": "NoCopyrightSounds"},
                "permalink_url": "https://soundcloud.com/n/fade",
                "artwork_url": "https://i/x-large.jpg",
                "publisher_metadata": {},
            }
        ]
    }
    monkeypatch.setattr(sc_api, "_get", lambda token, path: sc)
    (s,) = sc_api.search("token", "fade")
    assert (s.artist, s.title, s.seconds, s.cover) == ("Alan Walker", "Fade", 264, "https://i/x-t300x300.jpg")


def test_the_page(settings, login, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every user may search. A song the library has says so; a source that fails is named and the others
    show; an answer is kept for the next same query only when every source answered."""
    from echolot.web import create_app
    from echolot.web import discover as page

    asked: list[str] = []

    def deezer(q: str) -> list[Hit]:
        asked.append(q)
        return [Hit("deezer", 0, "Artist A", "First Song", 0, "An Album", url="https://www.deezer.com/t/1")]

    def apple(q: str, country: str = "US") -> list[Hit]:
        raise catalogs.CatalogError("HTTP 403")

    soundcloud = [Hit("soundcloud", 0, "Artist A", "First Song", 0, url="https://soundcloud.com/a/first")]
    monkeypatch.setattr(catalogs, "deezer", deezer)
    monkeypatch.setattr(catalogs, "apple", apple)
    monkeypatch.setattr(sc_api, "any_token", lambda con, vault: "token")
    monkeypatch.setattr(sc_api, "search", lambda token, q: soundcloud)
    page._cache.clear()
    page._resting.clear()
    client = login(create_app(settings), "timon", admin=False)
    assert 'hx-get="/discover/results"' in client.get("/discover").text
    html = client.get("/discover/results", params={"q": "first song"}).text
    assert "In your library" in html and "Apple Music did not answer" in html
    assert html.count("<li class=") == 1  # Deezer's and SoundCloud's hit: one song
    assert 'href="https://www.deezer.com/t/1"' in html and 'href="https://soundcloud.com/a/first"' in html
    client.get("/discover/results", params={"q": "first song"})
    assert len(asked) == 2  # Apple Music failed: asked again
    monkeypatch.setattr(catalogs, "apple", lambda q, country="US": [])
    page._resting.clear()  # (Apple Music failed: it rested)
    client.get("/discover/results", params={"q": "First  Song"})
    client.get("/discover/results", params={"q": "first song"})
    assert len(asked) == 3  # every source answered: kept (the same query, any case or spacing)
    assert "A little more" in client.get("/discover/results", params={"q": "x"}).text


def test_a_players_search_and_hearts(settings, login, monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the gate, a player's search gets Navidrome's songs and then Discover's the library lacks (the
    library's First Song is not added twice), each saying what it is: "♥ to add" in its artist line and
    album, a badge on its cover. A heart puts it in the user's Wished list (searched like a Spotify song, its
    ISRC Deezer's) and says "on its way"; ids of the library's songs in the same call go on to Navidrome."""
    from fastapi.testclient import TestClient

    from echolot import db
    from echolot.settings import sources
    from echolot.web import create_app, subsonic
    from echolot.web import discover as page

    library = {"id": "nd1", "title": "First Song", "artist": "Artist A"}
    calls: list[tuple[str, list[tuple[str, str]]]] = []

    def navidrome(con, method: str, params: list[tuple[str, str]]) -> tuple[int, str, bytes]:
        calls.append((method, params))
        good = ("t", "good") in params
        body: dict = {"status": "ok" if good else "failed"}
        if good and method == "search3.view":
            body["searchResult3"] = {"song": [library]}
        return 200, "application/json", json.dumps({"subsonic-response": body}).encode()

    cover = "https://img/new.jpg"
    new_song = Hit("deezer", 1, "New Artist", "New Song", 215, "New Album", "https://www.deezer.com/track/9")
    new_song.preview, new_song.cover = "https://p/9", cover
    deezer = [Hit("deezer", 0, "Artist A", "First Song", 200), new_song]
    monkeypatch.setattr(subsonic, "_navidrome", navidrome)
    monkeypatch.setattr(catalogs, "deezer", lambda q: deezer)
    monkeypatch.setattr(catalogs, "apple", lambda q, country="US": [])
    monkeypatch.setattr(catalogs, "track", lambda track_id: {"isrc": "USAB12345678", "preview": "https://p/9b"})
    monkeypatch.setattr(sc_api, "any_token", lambda con, vault: None)
    monkeypatch.setattr(page, "_soundcloud", lambda request, con, user_id: lambda q: [])
    monkeypatch.setattr(subsonic, "_preview", lambda hit: b"ID3 preview")
    monkeypatch.setattr(subsonic, "_picture", lambda url: (b"JPEG", "image/jpeg"))
    page._cache.clear()
    page._resting.clear()
    client = TestClient(create_app(settings))
    triggered: list[str] = []
    monkeypatch.setattr(client.app.state.worker, "trigger", lambda name, only=None: triggered.append(name))
    who = {"u": "owner", "t": "good", "s": "salt", "v": "1.16.1", "c": "Feishin", "f": "json"}

    def search() -> list[dict]:
        found = client.get("/rest/search3.view", params={**who, "query": "new song", "songCount": 20}).json()
        return found["subsonic-response"]["searchResult3"]["song"]

    songs = search()
    assert [s["title"] for s in songs] == ["First Song", "New Song"]
    new = songs[1]
    sid = new["id"]
    assert sid.startswith("ex-") and new["album"] == "♥ to add · Deezer · 30 s preview"
    assert new["artists"][0]["name"] == "New Artist · ♥ to add" and new["artist"] == "New Artist · ♥ to add"
    assert (new["duration"], new["coverArt"], "year" in new) == (30, f"{sid}~new", False)
    paged = client.get("/rest/search3.view", params={**who, "query": "new song", "songOffset": 20}).json()
    assert len(paged["subsonic-response"]["searchResult3"]["song"]) == 1  # later pages: Navidrome's only
    xml = client.get("/rest/search3", params={**who, "f": "xml", "query": "new song"})
    assert xml.headers["content-type"].startswith("application/json")  # (passed on as Navidrome answered)
    art = client.get("/rest/getCoverArt.view", params={**who, "id": new["coverArt"]})
    assert art.headers["content-type"] == "image/svg+xml" and "data:image/jpeg;base64,SlBFRw==" in art.text
    assert ">30s</text>" in art.text and "#1f6bcf" in art.text  # the preview's mark, the "+" badge

    wrong = client.get("/rest/star.view", params={**who, "t": "bad", "id": sid}).json()
    assert wrong["subsonic-response"]["error"]["code"] == 40
    calls.clear()
    starred = client.get("/rest/star.view", params=[*who.items(), ("id", sid), ("id", "nd1")]).json()
    assert starred["subsonic-response"]["status"] == "ok"
    assert [m for m, _ in calls] == ["star.view"] and ("id", "nd1") in calls[0][1]  # (login checked: cover)
    assert ("id", sid) not in calls[0][1]  # Navidrome gets its own song only
    assert triggered == ["search_new", "library"]
    con = db.connect(settings.db_path)
    key = "discover:" + sid.removeprefix("ex-")
    listed = con.execute("SELECT song_key FROM list_songs WHERE list_key = 'discover:wished:1'").fetchall()
    row = con.execute("SELECT * FROM songs WHERE key = ?", (key,)).fetchone()
    assert [r[0] for r in listed] == [key] and (row["service"], row["length"], row["isrc"]) == (
        "discover",
        215,
        "USAB12345678",
    )
    assert row["url"] == "https://www.deezer.com/track/9" and row["file"] is None
    assert "discover" not in sources.as_config(con, 1)  # Echolot's own list, not in the configuration
    assert [s.title for s in sources.user_lists(con, 1) if s.service == "discover"] == ["Echolot · Wished"]
    (again,) = [s for s in search() if s["id"] == sid]
    assert again["starred"] and again["artist"] == "New Artist · on its way" and again["coverArt"] == f"{sid}~wished"
    detail = client.get("/rest/getSong.view", params={**who, "id": sid}).json()["subsonic-response"]
    assert detail["song"]["album"] == "On its way · Deezer · 30 s preview"
    mine = login(client.app, "owner").get("/discover").text
    assert "Wished" in mine and "On its way" in mine
    with con:
        con.execute("UPDATE songs SET file = 'New Artist/New Artist - New Song.flac' WHERE key = ?", (key,))
    detail = client.get("/rest/getSong.view", params={**who, "id": sid}).json()["subsonic-response"]
    assert detail["song"]["artist"] == "New Artist · in your library"
    monkeypatch.setattr(subsonic, "_token_user", lambda con, header: "owner" if header == "Bearer jwt" else None)
    native = client.get(f"/rest/navidrome/song/{sid}", headers={"x-nd-authorization": "Bearer jwt"}).json()
    assert (native["id"], native["starred"], native["participants"]["artist"][0]["name"]) == (
        sid,
        True,
        "New Artist · in your library",
    )
    assert client.get(f"/rest/navidrome/song/{sid}").status_code == 401
    played = client.get("/rest/stream.view", params={**who, "id": sid})
    assert (played.content, played.headers["content-type"]) == (b"ID3 preview", "audio/mpeg")
    client.get("/rest/unstar.view", params={**who, "id": sid})
    assert not con.execute("SELECT 1 FROM list_songs WHERE list_key = 'discover:wished:1'").fetchone()
    con.close()
    assert "On its way" not in login(client.app, "owner").get("/discover").text
    gone = client.get("/rest/getSong", params={**who, "f": "", "id": "ex-0000"})
    assert 'code="70"' in gone.text and gone.text.startswith("<?xml")  # XML when not asked for JSON
