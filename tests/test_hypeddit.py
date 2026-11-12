"""Hypeddit free downloads: reading a gate, unlocking it (no steps performed), and fetching the file
(services/hypeddit). The gate's download request returns the file regardless of the follow or repost it asks for;
an email gate needs an address."""

import io
import json
from pathlib import Path

import pytest

from echolot.services import hypeddit

URL = "https://hypeddit.com/artist/tune"


def gate_page(steps: str = "dw,sc") -> str:
    return (
        '<html><head><meta name="csrf-token" content="CSRF">'
        "<title>Tune by Artist | Free Download on Hypeddit</title></head><body>"
        f'<input id="steps_select" value="{steps}">'
        '<input id="current_download_file_listner" value="fileX">'
        '<input id="fan_gate_id" value="123"><input id="wrndk" value="w"><input id="nwSteps" value="sc">'
        '<input id="is_skippable" value="0"><input id="duration" value="200000"><input id="gvf" value="0">'
        '<script>var j = {"externID":"ext123"};</script></body></html>'
    )


class FakeResp(io.BytesIO):
    def __enter__(self) -> "FakeResp":
        return self

    def __exit__(self, *a: object) -> None:
        self.close()


class FakeOpener:
    def __init__(self, page: str, answers: dict[str, dict]) -> None:
        self.page, self.answers, self.posted = page, answers, []

    def open(self, req: object, timeout: int = 30) -> FakeResp:
        url = req if isinstance(req, str) else req.full_url  # type: ignore[union-attr]
        if url == URL:
            return FakeResp(self.page.encode())
        path = url.removeprefix(hypeddit.BASE)
        self.posted.append(path)
        return FakeResp(json.dumps(self.answers[path]).encode())


def wire(monkeypatch: pytest.MonkeyPatch, page: str, answers: dict[str, dict]) -> FakeOpener:
    opener = FakeOpener(page, answers)
    monkeypatch.setattr(hypeddit, "_opener", lambda: opener)
    return opener


def test_unlock_gets_the_file_without_doing_the_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {
        "/gate/download/ul": {
            "download_status": "True",
            "URL": "https://s3/f?sig=1",
            "name": "Artist - Tune",
            "ext": "wav",
            "size": 1234,
            "type": "audio/wav",
        }
    }
    opener = wire(monkeypatch, gate_page("dw,sc"), answers)
    gate = hypeddit.read(URL)
    assert gate.steps == ["sc"] and not gate.email and gate.file_id == "fileX"
    file = hypeddit.unlock(gate)
    assert (file.url, file.ext, file.size) == ("https://s3/f?sig=1", "wav", 1234)
    assert opener.posted == ["/gate/download/ul"]  # no step was performed or checked


def test_an_email_gate_needs_an_address(monkeypatch: pytest.MonkeyPatch) -> None:
    wire(monkeypatch, gate_page("dw,email,sc"), {})
    gate = hypeddit.read(URL)
    assert gate.email
    with pytest.raises(hypeddit.GateError, match="wants an email"):
        hypeddit.unlock(gate)


def test_an_email_gate_verifies_then_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {
        "/verifyEmailAddress": {"status": "T", "attempts": "notover"},
        "/gate/download/ul": {"download_status": "True", "URL": "https://s3/f", "ext": "wav", "size": 9},
    }
    opener = wire(monkeypatch, gate_page("dw,email,sc"), answers)
    hypeddit.unlock(hypeddit.read(URL), "you@example.com")
    assert opener.posted == ["/verifyEmailAddress", "/gate/download/ul"]  # email registered first


def test_a_store_gate_gives_no_file(monkeypatch: pytest.MonkeyPatch) -> None:
    wire(monkeypatch, gate_page("dw,sc"), {"/gate/download/ul": {"download_status": "False", "URL": ""}})
    with pytest.raises(hypeddit.GateError, match="no file"):
        hypeddit.unlock(hypeddit.read(URL))


def test_fetch_writes_the_file_and_checks_its_size(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hypeddit.urllib.request, "urlopen", lambda req, timeout=60: FakeResp(b"x" * 20))
    file = hypeddit.File("https://s3/f", "Artist - Tune", "wav", 20, "audio/wav")
    got = hypeddit.fetch(file, tmp_path / "dl")
    assert got.read_bytes() == b"x" * 20 and got.name == "Artist - Tune.wav"

    short = hypeddit.File("https://s3/f", "Artist - Tune", "wav", 99, "audio/wav")
    with pytest.raises(hypeddit.GateError, match="of 99 bytes"):
        hypeddit.fetch(short, tmp_path / "dl2")  # a truncated download is rejected
