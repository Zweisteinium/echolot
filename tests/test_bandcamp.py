"""Bandcamp: offers read from a song's linked page (services/bandcamp), the run after New SoundCloud songs and
by hand (jobs/bandcamp), the Buy page and the badge."""

import html
import json
import shutil
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from echolot import db
from echolot.config import Settings
from echolot.jobs import acquire
from echolot.jobs import bandcamp as job
from echolot.jobs.schedule import BY_NAME, TASK_OF
from echolot.jobs.worker import Run
from echolot.library import audio
from echolot.services import bandcamp
from echolot.services import soundcloud as sc_api
from echolot.settings import vault
from echolot.web import create_app
from echolot.web.format import price

FIXTURES = Path(__file__).parent / "fixtures" / "audio"


def page(tralbum: dict, currency: str = "EUR") -> bytes:
    blob = {"item_sellers": {"1": {"currency": currency}}}
    attr = html.escape(json.dumps(tralbum), quote=True)
    return f'<html><div data-blob="{html.escape(json.dumps(blob), quote=True)}" data-tralbum="{attr}"></div>'.encode()


def track(title: str, price: float, free: bool = False, seconds: float = 200) -> dict:
    return {
        "item_type": "track",
        "url": f"https://artist.bandcamp.com/track/{title.lower().replace(' ', '-')}",
        "artist": "Uploader",
        "current": {"title": title, "minimum_price": price},
        "trackinfo": [{"title": title, "duration": seconds, "is_downloadable": True}],
        "freeDownloadPage": "https://bandcamp.com/download?fsig=x" if free else None,
    }


def test_links_are_track_and_album_pages() -> None:
    text = "Buy: https://x.bandcamp.com/track/tune?from=sc, label https://x.bandcamp.com/music, again x.bandcamp.com"
    assert bandcamp.links(text, None, "https://y.bandcamp.com/album/ep") == [
        "https://x.bandcamp.com/track/tune",
        "https://y.bandcamp.com/album/ep",
    ]


def test_an_offer_from_a_track_or_its_album(monkeypatch: pytest.MonkeyPatch) -> None:
    album = {
        "item_type": "album",
        "trackinfo": [
            {"title": "First Light", "duration": 239, "title_link": "/track/first-light"},
            {"title": "Artist - First Light (Remix)", "duration": 319, "title_link": "/track/first-light-remix"},
        ],
    }
    pages = {
        "https://artist.bandcamp.com/album/ep": page(album),
        "https://artist.bandcamp.com/track/first-light-remix": page(track("First Light (Remix)", 1.0, seconds=319)),
        "https://artist.bandcamp.com/track/poison": page(track("Poison", 0.0, free=True), "AUD"),
    }
    monkeypatch.setattr(bandcamp, "_get", lambda url, **kw: pages[url])
    o = bandcamp.offer("https://artist.bandcamp.com/album/ep", "First Light (Remix)")
    assert (o.title, o.price, o.currency, o.free, o.seconds) == ("First Light (Remix)", 1.0, "EUR", False, 319)
    free = bandcamp.offer("https://artist.bandcamp.com/track/poison", "Poison")
    assert (free.price, free.currency, free.free) == (0.0, "AUD", True)


def test_a_bot_check_is_not_got_around(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bandcamp, "_get", lambda url, **kw: b"<html><title>Client Challenge</title></html>")
    with pytest.raises(bandcamp.Challenged):
        bandcamp.offer("https://artist.bandcamp.com/track/poison")


def test_prices() -> None:
    assert [price(1.5, "EUR"), price(2.0, "AUD"), price(1.5), price(0.0, "EUR"), price(3, "USD", free=True)] == [
        "€1.50",
        "2.00 AUD",
        "1.50",
        "free",
        "free",
    ]
    assert price(None) == ""


@pytest.fixture
def run(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], Run]:
    monkeypatch.setattr(sc_api, "any_token", lambda con, v: "token")
    tracks = [{"id": 1001, "purchase_url": "https://artist.bandcamp.com/track/trance-tune"}, {"id": 1002}]
    monkeypatch.setattr(sc_api, "tracks", lambda token, ids: [t for t in tracks if str(t["id"]) in ids])

    def make(trigger: str) -> Run:
        r = Run(BY_NAME["bandcamp"], settings, vault.Vault.from_env(settings.data_dir, {}), trigger)
        r.stop = threading.Event()
        return r

    return make


def offers(settings: Settings) -> dict[str, sqlite3.Row]:
    con = db.connect(settings.db_path)
    try:
        return {r["song_key"]: r for r in con.execute("SELECT * FROM bandcamp")}
    finally:
        con.close()


def test_after_soundcloud_only_new_songs_and_by_hand_all(run, settings, monkeypatch: pytest.MonkeyPatch) -> None:
    asked = []

    def offer(url: str, title: str = "", seconds: float = 0) -> bandcamp.Offer:
        asked.append(url)
        return bandcamp.Offer(url, "Uploader", "Trance Tune", 400, 1.5, "EUR", False)

    monkeypatch.setattr(bandcamp, "offer", offer)
    assert "1 on Bandcamp, 0 free" in job.run(run("after"))
    found = offers(settings)
    assert (found["soundcloud:1001"]["price"], found["soundcloud:1001"]["currency"]) == (1.5, "EUR")
    assert found["soundcloud:1002"]["url"] == ""  # no link: not looked at again after the next sync
    assert job.run(run("after")) == "nothing new" and len(asked) == 1  # Bandcamp is not asked again
    job.run(run("manual"))  # Refresh: every song again
    assert len(asked) == 2
    assert "bandcamp" not in TASK_OF and BY_NAME["bandcamp"].default is None  # not on the Jobs page, no schedule


def test_a_free_download_is_filed_from_bandcamp(run, settings, monkeypatch: pytest.MonkeyPatch) -> None:
    o = bandcamp.Offer("https://artist.bandcamp.com/track/trance-tune", "Uploader", "Trance Tune", 400, 0.0, "", True)
    monkeypatch.setattr(bandcamp, "offer", lambda url, title="", seconds=0: o)

    def free_flac(offer: bandcamp.Offer, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        return Path(shutil.copy(FIXTURES / "silence.flac", folder / "Uploader - Trance Tune.flac"))

    monkeypatch.setattr(bandcamp, "free_flac", free_flac)
    monkeypatch.setattr(audio, "prepare", lambda p, keep_hires=False: audio.Prepared(p, False, None))
    monkeypatch.setattr(audio, "probe", lambda p: (400.0, 900))  # as long as the song (no ffprobe on CI)
    copy = "Uploader/Uploader - Trance Tune.m4a"
    monkeypatch.setattr(acquire, "_same_audio", lambda *a: (copy, "the same audio as your copy (0.999)"))
    monkeypatch.setattr(acquire, "finish", lambda *a, **k: None)
    r = run("after")
    assert job.run(r).endswith("1 free, 1 filed") and "library" in r.after  # (it matches the song to the FLAC)
    assert offers(settings)["soundcloud:1001"]["result"] == "upgrade"
    con = db.connect(settings.db_path)
    try:
        e = con.execute(
            "SELECT action, source, url FROM events WHERE song = 'soundcloud:1001' ORDER BY id DESC"
        ).fetchone()
        assert tuple(e) == ("upgrade", "bandcamp", o.url)
        assert (settings.library_dir / "Uploader" / "Uploader - Trance Tune.flac").is_file()
    finally:
        con.close()


def test_the_buy_page_and_the_badge(settings, login: Callable[..., TestClient]) -> None:
    con = db.connect(settings.db_path)
    with con:
        con.execute(
            "INSERT INTO bandcamp (song_key, url, artist, title, price, currency, checked) VALUES "
            "('soundcloud:1001', 'https://artist.bandcamp.com/track/trance-tune', 'Uploader', 'Trance Tune', 1.5, "
            "'EUR', '2026-10-18T20:00:00')"
        )
    con.close()
    client = login(create_app(settings))
    buy = client.get("/buy").text
    assert "Trance Tune" in buy and "Bandcamp €1.50" in buy and 'action="/buy/refresh"' in buy
    assert 'href="https://artist.bandcamp.com/track/trance-tune"' in client.get("/lists/soundcloud:someone/likes").text
    assert "Buy on Bandcamp · 1" in client.get("/library").text
    viewer = login(create_app(settings), "guest", admin=False)
    assert 'action="/buy/refresh"' not in viewer.get("/buy").text
    assert viewer.post("/buy/refresh", follow_redirects=False).status_code == 403
