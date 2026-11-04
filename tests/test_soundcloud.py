"""SoundCloud's tokens (services/soundcloud.py): the browser's login tokens are JWTs that last a day."""

import base64
import datetime
import io
import json
import urllib.error

import pytest

from echolot.services import soundcloud
from echolot.web import accounts


def jwt(exp: datetime.datetime) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"exp": int(exp.timestamp())}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{claims}.signature"


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
