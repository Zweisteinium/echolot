"""Recording with the users' recorder apps (library/recording, web/recorders): connecting a device (approve in
the browser, trade the code with its PKCE verifier), each device its user's alone, what a session records,
the songs handed out one by one with their leases and answers, the upload into exactly that song, and the
file export."""

import base64
import hashlib
import re
import secrets
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from echolot import db
from echolot.config import Settings
from echolot.library import recording, upload
from echolot.settings import auth
from echolot.web import create_app

APP = "Mitschnitt 0.4.0"


def connect(client: TestClient, app: FastAPI, name: str = "DESKTOP-DAVID") -> str:
    """As Mitschnitt and the user do it: the approval page, Connect, then the code traded for the token."""
    verifier = secrets.token_urlsafe(40)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    ask = {"name": name, "app": APP, "challenge": challenge, "state": "st1", "redirect": "mitschnitt://connected"}
    page = client.get("/recorders/connect", params=ask)
    assert page.status_code == 200 and "asks to be connected" in page.text and name in page.text
    done = client.post("/recorders/connect", data=ask | {"decision": "connect"})
    assert "mitschnitt://connected?code=" in done.text and "state=st1" in done.text
    code = re.search(r'id="recorder-code" type="text" value="([^"]+)"', done.text).group(1)
    device = TestClient(app)  # (no login: the device has its token only)
    assert device.post("/api/recorder/connect", json={"code": code, "verifier": "wrong" * 10}).status_code == 400
    r = device.post("/api/recorder/connect", json={"code": code, "verifier": verifier, "app": APP})
    assert r.status_code == 200, r.text
    assert device.post("/api/recorder/connect", json={"code": code, "verifier": verifier}).status_code == 400  # once
    return r.json()["token"]


def as_device(app: FastAPI, token: str) -> TestClient:
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


def poll(device: TestClient, state: str = "idle") -> dict:
    r = device.post("/api/recorder/poll?wait=0", json={"state": state, "spotify": "idle", "app": APP})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


def test_connecting_and_each_device_its_users_alone(app: FastAPI, login: Callable[..., TestClient]) -> None:
    owner = login(app)
    token = connect(owner, app)
    me = as_device(app, token).get("/api/recorder/me").json()
    assert me["user"] == "owner" and me["recorder"]["name"] == "DESKTOP-DAVID" and me["session"] is None
    assert TestClient(app).get("/api/recorder/me").status_code == 401  # no token
    assert as_device(app, "echolot_rec_nope").get("/api/recorder/me").status_code == 401
    page = owner.get("/account").text
    assert "DESKTOP-DAVID" in page and "mitschnitt://connect?url=" in page
    bad = {"name": "x", "challenge": "c" * 43, "redirect": "https://evil.example/"}
    assert owner.get("/recorders/connect", params=bad).status_code == 400  # only Mitschnitt's own link

    timon = login(app, "timon", admin=False)
    assert timon.get("/recorders/connect", params=bad | {"redirect": "mitschnitt://connected"}).status_code == 403
    con = db.connect(app.state.settings.db_path)
    tid = con.execute("SELECT id FROM users WHERE name = 'timon'").fetchone()[0]
    auth.set_rights(con, tid, False, {"upload"})
    con.close()
    timon_token = connect(timon, app, "TIMONS-PC")
    assert "DESKTOP-DAVID" not in timon.get("/account").text  # (not even an admin sees another's devices)
    assert "TIMONS-PC" not in owner.get("/account").text
    con = db.connect(app.state.settings.db_path)
    owners = con.execute("SELECT id FROM recorders WHERE name = 'DESKTOP-DAVID'").fetchone()[0]
    con.close()
    r = timon.post("/recordings", data={"recorder": owners, "below": "lossless"}, follow_redirects=False)
    assert "No+such+device" in r.headers["location"]  # timon can't record on the owner's device
    assert (
        timon.post(f"/recorders/{owners}/disconnect", follow_redirects=False)
        .headers["location"]
        .endswith("error=No+such+device.#recorders")
    )
    assert as_device(app, timon_token).delete("/api/recorder/me").status_code == 204  # disconnect itself
    assert as_device(app, timon_token).get("/api/recorder/me").status_code == 401


def test_a_session_song_by_song(app: FastAPI, login: Callable[..., TestClient]) -> None:
    """Worst first, each song once; an idle device gets one song at a time; the answers: Spotify only has it
    lossy (skipped, and for 30 days), the user took over Spotify (the session pauses), a stall (again once,
    then failed); then the session is done. A song Spotify relinks is handed out as the release that plays."""
    owner = login(app)
    device = as_device(app, connect(owner, app))
    con = db.connect(app.state.settings.db_path)
    with con:  # s1 plays here as another release
        con.execute("INSERT INTO availability VALUES ('spotify:s1', 'replaced', '', 'x', 'spotify:s1b', NULL)")
    tracks = recording.candidates(con, 1)
    assert [t.key for t in tracks] == ["spotify:s1", "spotify:s2"]  # not s3 (greyed out), s1 once (two lists)
    have = {t.key: t.have for t in tracks}
    con.close()
    dialog = owner.get("/recordings/new", params={"list": "spotify:likes:1"}).text
    assert "Not lossless" in dialog and "DESKTOP-DAVID" in dialog and "2 songs" in dialog
    rid = 1
    r = owner.post("/recordings", data={"recorder": rid, "below": "lossless", "list": "spotify:likes:1"})
    assert r.status_code == 200 and "Recording: your device starts" in r.text
    again = owner.post("/recordings", data={"recorder": rid, "below": "lossless"}, follow_redirects=False)
    assert "recording+already" in again.headers["location"]

    assert poll(device, "busy")["item"] is None  # the user plays Spotify: nothing for it
    first = poll(device)
    item = first["item"]
    assert item["key"] == "spotify:s1" and item["uri"] == "spotify:track:s1b" and item["have"] == have["spotify:s1"]
    assert item["duration_ms"] == 200000 and first["session"]["total"] == 2 and first["session"]["recording"]
    assert poll(device)["item"]["key"] == "spotify:s2"  # (it asked again: the next one)
    r = device.post(f"/api/recorder/items/{item['id']}/result", json={"result": "lossy_only"})
    assert r.json()["session"]["lossy_only"] == 1
    assert device.post(f"/api/recorder/items/{item['id']}/result", json={"result": "failed"}).status_code == 409

    con = db.connect(app.state.settings.db_path)
    s2 = con.execute("SELECT id FROM recording_items WHERE song_key = 'spotify:s2'").fetchone()[0]
    con.close()
    r = device.post(f"/api/recorder/items/{s2}/result", json={"result": "took_over"})
    assert r.json()["session"]["state"] == "paused" and r.json()["session"]["reason"] == "took_over"
    assert poll(device)["item"] is None
    panel = owner.get("/recordings/panel", params={"list": "spotify:likes:1"}).text
    assert "You took over Spotify on DESKTOP-DAVID" in panel and "Resume" in panel
    assert device.post("/api/recorder/session", json={"action": "resume"}).json()["session"]["state"] == "running"
    for _ in range(2):  # stalled twice: failed
        assert poll(device)["item"]["key"] == "spotify:s2"
        device.post(f"/api/recorder/items/{s2}/result", json={"result": "stalled"})
    done = device.get("/api/recorder/me").json()
    assert done["session"] is None  # (ended)
    panel = owner.get("/recordings/panel", params={"list": "spotify:likes:1"}).text
    assert "Done" in panel and "Spotify has only lossy" in panel and "could not be recorded" in panel
    con = db.connect(app.state.settings.db_path)
    assert [t.key for t in recording.candidates(con, 1)] == ["spotify:s2"]  # s1: not again for 30 days
    con.close()
    owner.post("/recordings/1/dismiss")
    assert 'id="recording" hidden' in owner.get("/recordings/panel", params={"list": "spotify:likes:1"}).text


def test_found_meanwhile_and_leases(
    app: FastAPI, login: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A song the library got meanwhile is skipped when its turn comes (found); a song whose lease ran out is
    queued again, and the device's late answer still counts until it is handed out again."""
    owner = login(app)
    device = as_device(app, connect(owner, app))
    owner.post("/recordings", data={"recorder": 1, "below": "lossless"})
    con = db.connect(app.state.settings.db_path)
    s1 = con.execute("SELECT file FROM songs WHERE key = 'spotify:s1'").fetchone()[0]
    with con:
        con.execute("UPDATE files SET quality = 'lossless' WHERE path = ?", (s1,))
    con.close()
    item = poll(device)["item"]
    assert item["key"] == "spotify:s2"
    con = db.connect(app.state.settings.db_path)
    with con:  # the lease ran out
        con.execute("UPDATE recording_items SET lease_until = 1 WHERE id = ?", (item["id"],))
    con.close()
    assert poll(device, "busy")["session"]["found"] == 1
    late = device.post(f"/api/recorder/items/{item['id']}/result", json={"result": "wrong_song"})
    assert late.status_code == 200 and late.json()["session"]["wrong_song"] == 1  # late, but not handed out again
    assert late.json()["session"]["state"] == "done"


def test_the_upload_into_the_song(
    app: FastAPI, login: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recording runs through the upload dialog's checks and is filed into the song handed out; the same
    upload again answers the same; the device's note stays with the result."""
    seen: list[tuple] = []

    def add(paths, batch, name, fileobj, keep_hires=False):
        return upload.File(1, name, Path("/x/rec.flac"), 200, 900, "lossless")

    monkeypatch.setattr(upload, "add", add)
    monkeypatch.setattr(upload, "fit", lambda con, f, song: upload.Fit(0, 0.97, 0.95))
    monkeypatch.setattr(upload, "compare", lambda paths, f, song: upload.Copy(True, "genuine lossless"))

    def import_files(con, paths, vault, batch, chosen, remove=frozenset()):
        seen.append(dict(chosen))
        return [("Artist B – Second Song: replaced the copy: Artist B/Artist B - Second Song.flac", "spotify:s2")]

    monkeypatch.setattr(upload, "import_files", import_files)
    owner = login(app)
    device = as_device(app, connect(owner, app))
    owner.post("/recordings", data={"recorder": 1, "below": "lossless", "list": "spotify:likes:1"})
    item = poll(device)["item"]
    files = {"file": ("rec.flac", b"fLaC", "audio/flac")}
    r = device.post(f"/api/recorder/items/{item['id']}/file", files=files, data={"note": "faded in 10 ms"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == "filed" and "faded in 10 ms" in body["detail"] and seen == [{1: item["key"]}]
    assert device.post(f"/api/recorder/items/{item['id']}/file", files=files).json()["result"] == "filed"
    assert len(seen) == 1  # (not imported twice)
    monkeypatch.setattr(upload, "fit", lambda con, f, song: upload.Fit(0, 0.4, 0.1))  # another recording
    nxt = poll(device)["item"]
    r = device.post(f"/api/recorder/items/{nxt['id']}/file", files=files)
    assert r.json()["result"] == "rejected" and "Sounds different" in r.json()["detail"]


def test_the_export(app: FastAPI, login: Callable[..., TestClient]) -> None:
    owner = login(app)
    r = owner.get("/api/recorder/export", params={"list": "spotify:likes:1", "below": "lossless"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert data["title"] == "Liked Songs · Not lossless" and [t["key"] for t in data["tracks"]] == [
        "spotify:s1",
        "spotify:s2",
    ]
    assert {"uri", "duration_ms", "isrc", "have", "artists"} <= set(data["tracks"][0])
    assert owner.get("/api/recorder/export", params={"below": "nope"}).status_code == 400
