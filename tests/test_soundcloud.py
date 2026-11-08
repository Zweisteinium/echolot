"""SoundCloud's tokens (services/soundcloud.py): day-long JWTs, renewed with a refresh token that works once."""

import base64
import datetime
import io
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
from collections.abc import Callable, Iterator

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from echolot import db
from echolot.config import Settings
from echolot.services import soundcloud
from echolot.settings.vault import Vault
from echolot.web import accounts, create_app

CLIENT = "W" * 32  # the web app the token names
OWNER = 1


def jwt(exp: datetime.datetime, client: str = CLIENT) -> str:
    claims = {"exp": int(exp.timestamp()), "client_id": client}
    part = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{part}.signature"


def hours(n: float) -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=n)


class LoginServer:
    """secure.soundcloud.com's token endpoint: each refresh token works once; `down` answers nothing."""

    def __init__(self, valid: str) -> None:
        self.valid, self.asked, self.down = {valid}, [], False
        self.issued = 0

    def __call__(self, req, timeout):
        if req.full_url.startswith(soundcloud.API):  # /me
            return io.BytesIO(json.dumps({"permalink": "me", "username": "Me"}).encode())
        if not req.full_url.startswith(soundcloud.SECURE):  # (the Soulseek daemon of the Playlists page)
            raise OSError("offline")
        assert req.full_url == f"{soundcloud.SECURE}/oauth/token" and req.get_method() == "POST"
        form = dict(urllib.parse.parse_qsl(req.data.decode()))
        self.asked.append(form)
        if self.down:
            raise OSError("unreachable")
        if form["refresh_token"] not in self.valid or form["client_id"] != CLIENT:
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"error":"invalid_grant"}'))  # type: ignore[arg-type]
        self.valid.discard(form["refresh_token"])
        self.issued += 1
        new = f"refresh-{self.issued}"
        self.valid.add(new)
        time.sleep(0.05)  # (long enough for other threads to ask meanwhile)
        return io.BytesIO(json.dumps({"access_token": jwt(hours(24)), "refresh_token": new}).encode())


@pytest.fixture
def con(settings: Settings) -> Iterator[sqlite3.Connection]:
    c = db.connect(settings.db_path)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_failures() -> Iterator[None]:
    yield
    soundcloud._failed.clear()


def stored(con: sqlite3.Connection, vault: Vault, token: str, refresh: str | None) -> None:
    with con:
        vault.set(con, soundcloud.token_name(OWNER), token)
        if refresh:
            vault.set(con, soundcloud.refresh_name(OWNER), refresh)


def refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    def urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO())  # type: ignore[arg-type]

    monkeypatch.setattr(soundcloud.urllib.request, "urlopen", urlopen)


def test_expiry_of_a_login_token() -> None:
    now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    assert soundcloud.expires(jwt(now)) == now
    assert soundcloud.expired(jwt(now - datetime.timedelta(hours=1)))
    assert not soundcloud.expired(jwt(now + datetime.timedelta(hours=1)))
    assert soundcloud.expires("2-123456-789-abcdef") is None and not soundcloud.expired("2-123456-789-abcdef")


def test_a_refused_token_says_when_it_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    refuse(monkeypatch)
    old = jwt(datetime.datetime(2026, 11, 3, 16, 14, tzinfo=datetime.UTC))
    with pytest.raises(soundcloud.SoundCloudError, match=r"token expired on 2026-11-03 .*last a day"):
        soundcloud.me(old)
    with pytest.raises(soundcloud.SoundCloudError, match="refused the token"):  # not expired: copied wrong
        soundcloud.me(jwt(datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)))
    assert accounts._soundcloud_status(old, None)["expired"]
    assert not accounts._soundcloud_status("2-123456-789-abcdef", None)["expired"]


def test_a_token_that_expires_soon_is_renewed_once(con, monkeypatch: pytest.MonkeyPatch) -> None:
    """Within RENEW_BEFORE of its end: renewed and the new pair stored; threads asking meanwhile wait for
    that one renewal (a second with the same refresh token would be refused)."""
    vault, server = Vault(Fernet.generate_key(), "test"), LoginServer("refresh-0")
    monkeypatch.setattr(soundcloud.urllib.request, "urlopen", server)
    old = jwt(hours(0.5))
    stored(con, vault, old, "refresh-0")
    got: list[str | None] = []
    path = con.execute("PRAGMA database_list").fetchone()[2]

    def ask() -> None:
        c = db.connect(path)  # (one connection per thread, as for requests and jobs)
        got.append(soundcloud.token_of(c, vault, OWNER))
        c.close()

    threads = [threading.Thread(target=ask) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(server.asked) == 1 and server.asked[0] == {
        "client_id": CLIENT,
        "grant_type": "refresh_token",
        "refresh_token": "refresh-0",
    }
    new = vault.get(con, soundcloud.token_name(OWNER))
    assert new != old and got == [new] * 4 and not soundcloud.expired(new)
    assert vault.get(con, soundcloud.refresh_name(OWNER)) == "refresh-1"
    assert soundcloud.any_token(con, vault) == new and len(server.asked) == 1  # fresh: not renewed again


def test_a_token_is_renewed_only_when_due_and_renewable(con, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, server = Vault(Fernet.generate_key(), "test"), LoginServer("refresh-0")
    monkeypatch.setattr(soundcloud.urllib.request, "urlopen", server)
    for token, refresh in ((jwt(hours(5)), "refresh-0"), ("2-123456-789-abcdef", "refresh-0"), (jwt(hours(-1)), None)):
        stored(con, vault, token, refresh)
        if not refresh:
            with con:
                vault.delete(con, soundcloud.refresh_name(OWNER))
        assert soundcloud.token_of(con, vault, OWNER) == token
    assert not server.asked


def test_a_refused_refresh_token_is_dropped(con, monkeypatch: pytest.MonkeyPatch) -> None:
    """A refresh token SoundCloud refuses (used up, logged out) is dropped; the expired token stays."""
    vault, server = Vault(Fernet.generate_key(), "test"), LoginServer("refresh-0")
    monkeypatch.setattr(soundcloud.urllib.request, "urlopen", server)
    old = jwt(hours(-2))
    stored(con, vault, old, "used-up")
    assert soundcloud.token_of(con, vault, OWNER) == old
    assert not vault.has(con, soundcloud.refresh_name(OWNER))
    assert soundcloud.token_of(con, vault, OWNER) == old and len(server.asked) == 1


def test_a_renewal_soundcloud_does_not_answer_is_tried_again_later(con, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, server = Vault(Fernet.generate_key(), "test"), LoginServer("refresh-0")
    monkeypatch.setattr(soundcloud.urllib.request, "urlopen", server)
    old = jwt(hours(0.2))
    stored(con, vault, old, "refresh-0")
    server.down = True
    assert soundcloud.token_of(con, vault, OWNER) == old
    assert soundcloud.token_of(con, vault, OWNER) == old and len(server.asked) == 1  # not within RETRY_AFTER
    assert vault.get(con, soundcloud.refresh_name(OWNER)) == "refresh-0"
    server.down = False
    soundcloud._failed[OWNER] -= soundcloud.RETRY_AFTER
    assert soundcloud.token_of(con, vault, OWNER) != old
    assert vault.get(con, soundcloud.refresh_name(OWNER)) == "refresh-1"


def test_connecting_with_the_refresh_token_renews_at_once(
    settings: Settings, login: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pasted refresh token is tried at once and the new pair stored; a token alone drops the stored one."""
    app = create_app(settings)
    client, server = login(app), LoginServer("refresh-0")
    monkeypatch.setattr(soundcloud.urllib.request, "urlopen", server)
    r = client.post(
        "/accounts/soundcloud",
        data={"token": f"OAuth {jwt(hours(20))}", "refresh": " refresh-0 "},
        follow_redirects=False,
    )
    assert "renews+its+token" in r.headers["location"] and server.asked[0]["refresh_token"] == "refresh-0"
    con, vault = db.connect(settings.db_path), app.state.vault
    try:
        assert vault.get(con, soundcloud.refresh_name(OWNER)) == "refresh-1"
        assert "renews its token itself each day" in client.get("/playlists").text
        r = client.post(
            "/accounts/soundcloud", data={"token": jwt(hours(20)), "refresh": "used-up"}, follow_redirects=False
        )
        assert "refused+the+refresh+token" in r.headers["location"]
        assert vault.get(con, soundcloud.refresh_name(OWNER)) == "refresh-1"  # the login stays as it was
        client.post("/accounts/soundcloud", data={"token": jwt(hours(20))})
        assert not vault.has(con, soundcloud.refresh_name(OWNER))
        html = client.get("/playlists").text
        assert "renews its token itself" not in html and "Paste the refresh token with it" in html
    finally:
        con.close()
