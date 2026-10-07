"""Discover: the merged search over Deezer, Apple Music and SoundCloud (library/discover, services, web)."""

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
    client = login(create_app(settings), "timon", admin=False)
    assert 'hx-get="/discover/results"' in client.get("/discover").text
    html = client.get("/discover/results", params={"q": "first song"}).text
    assert "In your library" in html and "Apple Music did not answer" in html
    assert html.count("<li class=") == 1  # Deezer's and SoundCloud's hit: one song
    assert 'href="https://www.deezer.com/t/1"' in html and 'href="https://soundcloud.com/a/first"' in html
    client.get("/discover/results", params={"q": "first song"})
    assert len(asked) == 2  # Apple Music failed: asked again
    monkeypatch.setattr(catalogs, "apple", lambda q, country="US": [])
    client.get("/discover/results", params={"q": "First  Song"})
    client.get("/discover/results", params={"q": "first song"})
    assert len(asked) == 3  # every source answered: kept (the same query, any case or spacing)
    assert "A little more" in client.get("/discover/results", params={"q": "x"}).text
