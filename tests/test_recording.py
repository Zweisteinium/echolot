"""Recorder apps (library/recording, web/recorders): device connection (PKCE), sessions, leases, answers,
uploads into the song handed out, export."""

import base64
import hashlib
import json
import re
import secrets
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from echolot import db
from echolot.config import Settings
from echolot.jobs import availability
from echolot.library import audio, recording, upload
from echolot.settings import auth, sources
from echolot.web import create_app, stats

APP = "Mitschnitt 0.4.0"


def connect(client: TestClient, app: FastAPI, name: str = "DESKTOP-DAVID") -> str:
    """Approve in the browser, then trade the code with the PKCE verifier (once) for the token."""
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
    page = owner.get("/account/recorders").text
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
    assert "DESKTOP-DAVID" not in timon.get("/account/recorders").text  # (not even an admin sees another's devices)
    assert "TIMONS-PC" not in owner.get("/account/recorders").text
    con = db.connect(app.state.settings.db_path)
    owners = con.execute("SELECT id FROM recorders WHERE name = 'DESKTOP-DAVID'").fetchone()[0]
    con.close()
    r = timon.post("/recordings", data={"recorder": owners, "below": "lossless"}, follow_redirects=False)
    assert "No+such+device" in r.headers["location"]  # timon can't record on the owner's device
    assert (
        timon.post(f"/recorders/{owners}/disconnect", follow_redirects=False)
        .headers["location"]
        .endswith("/account?error=No+such+device.#recorders")
    )
    assert as_device(app, timon_token).delete("/api/recorder/me").status_code == 204  # disconnect itself
    assert as_device(app, timon_token).get("/api/recorder/me").status_code == 401


def test_an_admin_records_a_list_only_someone_else_follows(app: FastAPI, login: Callable[..., TestClient]) -> None:
    """In the everyone view an admin can record a list they do not follow."""
    owner = login(app)
    connect(owner, app)
    con = db.connect(app.state.settings.db_path)
    with con:  # the owner no longer follows Liked Songs
        con.execute("DELETE FROM sources WHERE key = 'spotify:likes:1'")
    con.close()
    params = {"list": "spotify:likes:1"}
    assert owner.get("/recordings/new", params=params).status_code == 404  # their own view: not theirs
    assert owner.post("/account/view", data={"view": "everyone"}, follow_redirects=False).status_code == 303
    assert owner.get("/lists/spotify:likes:1").status_code == 200
    dialog = owner.get("/recordings/new", params=params).text
    assert "DESKTOP-DAVID" in dialog and "2 songs" in dialog
    r = owner.post("/recordings", data={"recorder": 1, "below": "lossless", **params}, follow_redirects=False)
    assert r.headers["location"] == "/lists/spotify:likes:1"  # started: back without a message


def test_a_session_song_by_song(app: FastAPI, login: Callable[..., TestClient]) -> None:
    """Songs go out one at a time to an idle device, as the release that plays. lossy_only skips for 30 days,
    took_over pauses, two stalls fail the song."""
    owner = login(app)
    device = as_device(app, connect(owner, app))
    con = db.connect(app.state.settings.db_path)
    with con:  # s1 plays here as another release
        con.execute("INSERT INTO availability VALUES ('spotify:s1', 'replaced', '', 'x', 'spotify:s1b', NULL, NULL)")
    tracks = recording.candidates(con, 1)
    assert [t.key for t in tracks] == ["spotify:s1", "spotify:s2"]  # not s3 (greyed out), s1 once (two lists)
    have = {t.key: t.have for t in tracks}
    con.close()
    dialog = owner.get("/recordings/new", params={"list": "spotify:likes:1"}).text
    assert "All but FLAC" in dialog and "DESKTOP-DAVID" in dialog and "2 songs" in dialog
    rid = 1
    data = {"recorder": rid, "below": "lossless", "list": "spotify:likes:1"}
    r = owner.post("/recordings", data=data, follow_redirects=False)
    assert r.headers["location"] == "/lists/spotify:likes:1"

    assert poll(device, "busy")["item"] is None  # the user plays Spotify: nothing for it
    first = poll(device)
    item = first["item"]
    assert item["key"] == "spotify:s1" and item["uri"] == "spotify:track:s1b" and item["have"] == have["spotify:s1"]
    assert item["duration_ms"] == 200000 and first["session"]["total"] == 2 and first["session"]["recording"]
    assert poll(device)["item"]["key"] == "spotify:s2"
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
    """A song the library got meanwhile is skipped as found; an answer after the lease ran out still counts
    while the song is not handed out again."""
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
    """An upload passes the upload checks and is filed into the song handed out (source mitschnitt), once;
    a different recording is rejected with its scores and blocked."""
    seen: list[tuple] = []

    def add(paths, batch, name, fileobj, keep_hires=False):
        return upload.File(1, name, Path("/x/rec.flac"), 200, 900, "lossless")

    monkeypatch.setattr(upload, "add", add)
    monkeypatch.setattr(upload, "fit", lambda con, f, song, tracks: upload.Fit(0, 0.97, 0.95))
    monkeypatch.setattr(upload, "compare", lambda paths, f, song: upload.Copy(True, "genuine lossless"))

    def import_files(con, paths, vault, batch, chosen, remove=frozenset(), source="manual"):
        seen.append((dict(chosen), source))
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
    assert body["result"] == "filed" and "faded in 10 ms" in body["detail"]
    assert seen == [({1: item["key"]}, "mitschnitt")]  # History shows it as recorded by Mitschnitt
    assert device.post(f"/api/recorder/items/{item['id']}/file", files=files).json()["result"] == "filed"
    assert len(seen) == 1
    monkeypatch.setattr(upload, "fit", lambda con, f, song, tracks: upload.Fit(6, 0.4, 0.1))  # another recording
    nxt = poll(device)["item"]
    r = device.post(f"/api/recorder/items/{nxt['id']}/file", files=files)
    assert r.json()["result"] == "rejected" and "Sounds different" in r.json()["detail"]
    assert "(fingerprint 0.40, waveform 0.10)" in r.json()["detail"]
    con = db.connect(app.state.settings.db_path)
    assert item["key"] not in recording.blocks(con)  # filed genuine lossless: no mark
    best = recording.blocks(con)[nxt["key"]]  # Spotify's is another recording: not offered again
    assert best["reason"] == "other" and "fingerprint 0.40" in best["detail"]
    assert nxt["key"] not in [t.key for t in recording.candidates(con, 1)]
    session = con.execute("SELECT recording_id FROM recording_items WHERE id = ?", (nxt["id"],)).fetchone()[0]
    assert recording.summary(con, session)["other"] == 1  # skipped for good, not failed
    con.close()


def test_a_relinked_song_by_the_names_it_plays_as(
    app: FastAPI, login: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relinked song goes out under the relink's names when it is the same recording (ISRC) or there is a
    library copy to check against; else, or for an unknown relink, under the list's names. Against the copy,
    another recording is rejected and blocked, another master is filed."""
    con = db.connect(app.state.settings.db_path)
    plays = {"key": "spotify:s1b", "artist": "evaness", "artists": ["evaness"], "title": "Heineken", "album": "H"}
    plays |= {"length": 201, "isrc": "QZTAS2444552"}
    with con:
        con.execute(
            "INSERT INTO availability VALUES ('spotify:s1', 'replaced', '', 'x', 'spotify:s1b', NULL, ?)",
            (json.dumps(plays),),
        )

    def handed() -> tuple:
        t = next(t for t in recording.candidates(con, 1) if t.key == "spotify:s1")
        return t.uri, t.artist, t.artists, t.title, t.album, t.length, t.isrc

    assert handed() == ("spotify:track:s1b", "evaness", ["evaness"], "Heineken", "H", 201, "QZTAS2444552")
    copy = con.execute("SELECT file FROM songs WHERE key = 'spotify:s1'").fetchone()[0]
    with con:  # no copy to check against
        con.execute("UPDATE songs SET file = NULL WHERE key = 'spotify:s1'")
    assert handed()[1:4] == ("Artist A", ["Artist A"], "First Song")
    with con:  # same ISRC
        con.execute("UPDATE songs SET isrc = 'QZTAS2444552' WHERE key = 'spotify:s1'")
    assert handed()[3] == "Heineken"
    with con:  # relinked again, not known yet
        con.execute("UPDATE availability SET detail = 'spotify:s1c'")
    assert handed()[:4] == ("spotify:track:s1c", "Artist A", ["Artist A"], "First Song")
    with con:
        con.execute("UPDATE availability SET detail = 'spotify:s1b'")
        con.execute("UPDATE songs SET file = ?, isrc = NULL WHERE key = 'spotify:s1'", (copy,))
    con.close()

    scores = {"preview": (None, None), "copy": (0.58, 0.04)}  # fingerprint, waveform (the song has no ISRC)

    def add(paths, batch, name, fileobj, keep_hires=False):
        return upload.File(1, name, Path("/x/rec.flac"), 201, 900, "lossless")

    def import_files(con, paths, vault, batch, chosen, remove=frozenset(), source="manual"):
        return [("Artist A – First Song: replaced the copy", "spotify:s1")]

    monkeypatch.setattr(upload, "add", add)
    monkeypatch.setattr(upload, "fit", lambda con, f, song, tracks: upload.Fit(0, *scores["preview"], *scores["copy"]))
    monkeypatch.setattr(upload, "compare", lambda paths, f, song: upload.Copy(True, "genuine lossless"))
    monkeypatch.setattr(upload, "import_files", import_files)
    owner = login(app)
    device = as_device(app, connect(owner, app))
    owner.post("/recordings", data={"recorder": 1, "below": "lossless", "list": "spotify:likes:1"})
    files = {"file": ("rec.flac", b"fLaC", "audio/flac")}
    item = poll(device)["item"]
    assert item["key"] == "spotify:s1" and item["title"] == "Heineken"
    body = device.post(f"/api/recorder/items/{item['id']}/file", files=files).json()
    assert body["result"] == "rejected"
    assert body["detail"] == (
        "Sounds different: another recording than your copy (fingerprint 0.58, waveform 0.04) "
        "(Spotify plays it as evaness – Heineken)"
    )
    con = db.connect(app.state.settings.db_path)
    assert recording.blocks(con)["spotify:s1"]["reason"] == "other"  # not offered again
    recording.unblock(con, "spotify:s1")
    con.close()
    scores["preview"] = (0.97, 0.95)  # it is the release's recording: your copy is the odd one (DEVO - Whip It)
    owner.post("/recordings", data={"recorder": 1, "below": "lossless", "list": "spotify:likes:1"})
    item = poll(device)["item"]
    body = device.post(f"/api/recorder/items/{item['id']}/file", files=files).json()
    assert item["key"] == "spotify:s1" and body["result"] == "filed"
    scores["preview"] = (None, None)
    scores["copy"] = (0.95, 0.75)  # another master of the same recording
    owner.post("/recordings", data={"recorder": 1, "below": "lossless", "list": "spotify:likes:1"})
    item = poll(device)["item"]
    body = device.post(f"/api/recorder/items/{item['id']}/file", files=files).json()
    assert item["key"] == "spotify:s1" and body["result"] == "filed"


def test_a_song_skipped_on_the_device(app: FastAPI, login: Callable[..., TestClient]) -> None:
    """A song skipped on the device counts as skipped, is not handed out again in that session, and is
    offered by the next one (no block)."""
    owner = login(app)
    device = as_device(app, connect(owner, app))
    owner.post("/recordings", data={"recorder": 1, "below": "lossless", "list": "spotify:likes:1"})
    item = poll(device)["item"]
    r = device.post(
        f"/api/recorder/items/{item['id']}/result", json={"result": "skipped", "detail": "skipped by the user"}
    )
    assert r.status_code == 200, r.text
    session = r.json()["session"]
    assert session["state"] == "running" and session["skipped"] == 1 and session["by_user"] == 1
    rest = poll(device)["item"]
    assert rest["key"] != item["key"]
    device.post(f"/api/recorder/items/{rest['id']}/result", json={"result": "lossy_only"})
    assert poll(device)["item"] is None
    con = db.connect(app.state.settings.db_path)
    assert item["key"] in [t.key for t in recording.candidates(con, 1)]
    assert item["key"] not in recording.blocks(con)
    con.close()
    owner.post("/recordings", data={"recorder": 1, "below": "lossless", "list": "spotify:likes:1"})
    assert poll(device)["item"]["key"] == item["key"]


def test_the_export(app: FastAPI, login: Callable[..., TestClient]) -> None:
    owner = login(app)
    r = owner.get("/api/recorder/export", params={"list": "spotify:likes:1", "below": "lossless"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert data["title"] == "Liked Songs · All but FLAC" and [t["key"] for t in data["tracks"]] == [
        "spotify:s1",
        "spotify:s2",
    ]
    assert {"uri", "duration_ms", "isrc", "have", "artists"} <= set(data["tracks"][0])
    assert owner.get("/api/recorder/export", params={"below": "nope"}).status_code == 400
    other = login(app, "timon", admin=False)
    con = db.connect(app.state.settings.db_path)
    with con:
        auth.set_rights(con, auth.get_user(con, "timon").id, False, {"upload"})
    con.close()
    assert other.get("/api/recorder/export", params={"list": "spotify:likes:1"}).status_code == 404  # not theirs


def test_the_devices_own_stop_keeps_its_reason(app: FastAPI, login: Callable[..., TestClient]) -> None:
    """The device's Stop (pause, then took_over) keeps the pause reason by_user and costs the song no try."""
    owner = login(app)
    device = as_device(app, connect(owner, app))
    owner.post("/recordings", data={"recorder": 1, "below": "lossless"})
    item = poll(device)["item"]
    panel = owner.get("/recordings/panel").text
    assert "Pause after this song" in panel and "Stop after this song" in panel
    assert device.post("/api/recorder/session", json={"action": "pause"}).json()["session"]["reason"] == "by_user"
    assert "Pausing after this song" in owner.get("/recordings/panel").text
    r = device.post(
        f"/api/recorder/items/{item['id']}/result", json={"result": "took_over", "detail": "listening was stopped"}
    )
    assert (r.json()["session"]["state"], r.json()["session"]["reason"]) == ("paused", "by_user")
    device.post("/api/recorder/session", json={"action": "resume"})
    again = poll(device)["item"]
    assert again["key"] == item["key"]
    con = db.connect(app.state.settings.db_path)
    assert con.execute("SELECT tries FROM recording_items WHERE id = ?", (item["id"],)).fetchone()[0] == 1
    con.close()


def test_the_panels_clock_runs_on(app: FastAPI, login: Callable[..., TestClient]) -> None:
    """The panel's clock adds the time since the device's last report, capped at the song's end; its bar
    totals the session's songs."""
    import datetime

    then = (datetime.datetime.now() - datetime.timedelta(seconds=10)).isoformat(timespec="seconds")
    device = {"state": "recording", "detail": "1:12 / 3:45", "last_seen": then}
    now = recording.clock(device)
    assert now["total"] == 225 and 82 <= now["at"] <= 83  # last_seen is in whole seconds
    assert recording.clock(device | {"detail": "3:40 / 3:45"})["at"] == 225
    assert recording.clock(device | {"state": "idle"}) is None
    owner = login(app)
    dev = as_device(app, connect(owner, app))
    owner.post("/recordings", data={"recorder": 1, "below": "lossless"})
    poll(dev)
    dev.post("/api/recorder/poll?wait=0", json={"state": "recording", "detail": "0:30 / 3:20"})
    panel = owner.get("/recordings/panel").text
    assert re.search(r'data-clock data-at="3[01]"', panel) and 'data-all="500"' in panel  # s1 200 s + s2 300 s


def test_spotify_has_nothing_better(
    app: FastAPI, login: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recording no better than the library's copy (also genuine lossless but quieter) blocks the song for
    everyone while Spotify plays that track; lists mark it, the dialog counts it out; a relink or Try again
    lifts it."""
    tiers = {"spotify:s1": "lossless", "spotify:s2": "fake"}

    def add(paths, batch, name, fileobj, keep_hires=False):
        return upload.File(1, name, Path("/x/rec.flac"), 200, 900, tiers[name])

    monkeypatch.setattr(upload, "add", add)
    monkeypatch.setattr(upload, "fit", lambda con, f, song, tracks: upload.Fit(0, 0.97, 0.95))
    why = {"lossless": "6.7 dB quieter than your 127 kbps", "fake": "~128 kbps source, your copy fake FLAC"}
    monkeypatch.setattr(upload, "compare", lambda paths, f, song: upload.Copy(False, why[f.tier]))
    owner = login(app)
    device = as_device(app, connect(owner, app))
    owner.post("/recordings", data={"recorder": 1, "below": "lossless"})
    for _ in range(2):
        item = poll(device)["item"]
        device.post(f"/api/recorder/items/{item['id']}/file", files={"file": (item["key"], b"fLaC", "audio/flac")})
    con = db.connect(app.state.settings.db_path)
    best = recording.blocks(con)
    assert {k: r["reason"] for k, r in best.items()} == {"spotify:s1": "same", "spotify:s2": "same"}
    assert best["spotify:s2"]["uri"] == "spotify:track:s2" and "quieter" in best["spotify:s1"]["detail"]
    assert recording.unblock(con, "spotify:s1")  # the rest is about s2
    assert "spotify:s2" not in [t.key for t in recording.candidates(con, 1)]
    assert "1 Spotify has nothing better of" in recording.survey(con, 1)[1]
    timon = auth.logged_in(con, "timon", "nd-timon", False).id
    sources.add_list(con, timon, "https://open.spotify.com/playlist/AAA111")
    con.close()
    page = owner.get("/lists/spotify:likes:1").text
    assert (
        'class="icon-button spotify-best"' in page
        and "Spotify has nothing better: Spotify&#39;s stream is no better" in page
    )
    assert "1 Spotify has nothing better of" in owner.get("/recordings/new").text
    con = db.connect(app.state.settings.db_path)
    with con:  # s2 now plays as another release
        con.execute("INSERT INTO availability VALUES ('spotify:s2', 'replaced', '', 'x', 'spotify:s2new', NULL, NULL)")
    assert recording.blocks(con) == {}
    with con:
        con.execute("DELETE FROM availability WHERE song_key = 'spotify:s2'")
    assert list(recording.blocks(con)) == ["spotify:s2"]
    con.close()
    assert owner.post("/songs/spotify:s2/spotify-again").status_code == 200
    con = db.connect(app.state.settings.db_path)
    assert recording.blocks(con) == {} and not any("better" in w for w in recording.survey(con, 1)[1])
    con.close()


def test_best_marks_and_the_record_dialog_agree(app: FastAPI, login: Callable[..., TestClient]) -> None:
    """List marks (recording.marks) and the Record dialog's count-outs agree: nothing better and no longer
    played both count as Best; played as another song lately is left out for SKIP_DAYS."""
    owner = login(app)
    connect(owner, app)
    con = db.connect(app.state.settings.db_path)
    recording.block(con, "spotify:s2", "spotify:track:s2", "same")  # s2: the fake FLAC; s3: greyed out
    with con:  # s3 searched everywhere in vain: it seems wiped off the internet, left out of the share
        con.execute(
            "INSERT OR REPLACE INTO attempts (song_key, tries, last_try, last_fallback) VALUES ('spotify:s3', 4, 1, 1)"
        )

    def state() -> tuple:
        liked = next(r for r in stats.lists(con, 1) if r["key"] == "spotify:likes:1")
        marks = recording.marks(con, 1, stats.list_songs(con, "spotify:likes:1"))
        tracks, left = recording.survey(con, 1, "spotify:likes:1")
        return (
            (liked["best"], liked["songs"] - liked["unobtainable"]),
            {k: m.kind for k, m in marks.items()},
            [t.key for t in tracks],
            left,
        )

    assert state() == (
        (1, 2),
        {"spotify:s2": "best", "spotify:s3": "gone"},
        ["spotify:s1"],
        ["1 Spotify has nothing better of", "1 Spotify no longer plays"],
    )
    with con:  # s1 played as another song
        con.execute(
            "INSERT INTO recordings (id, user_id, recorder_id, title, below, state, created) "
            "VALUES (1, 1, 1, 'Liked Songs', 'lossless', 'done', ?)",
            (recording._stamp(),),
        )
        con.execute(
            "INSERT INTO recording_items (recording_id, position, song_key, state, result, finished) "
            "VALUES (1, 0, 'spotify:s1', 'done', 'wrong_song', ?)",
            (recording._stamp(),),
        )
    best, marks, tracks, left = state()
    assert best == (1, 2) and marks["spotify:s1"] == "lately" and tracks == []
    assert left[-1] == "1 played as another song lately"
    page = owner.get("/recordings/new?list=spotify:likes:1").text
    assert "Nothing to record: 1 Spotify has nothing better of, 1 Spotify no longer plays, 1 played as another" in page
    with con:
        con.execute(
            "INSERT INTO availability (song_key, state, since, checked) VALUES ('spotify:s1', 'taken_down', '', '')"
        )
    assert state()[:2] == ((2, 2), {"spotify:s1": "gone", "spotify:s2": "best", "spotify:s3": "gone"})
    with con:  # another release of s1, same file
        con.execute(
            "INSERT INTO songs (key, service, artist, title, length, file) SELECT 'spotify:s1b', service, artist, "
            "title, length, file FROM songs WHERE key = 'spotify:s1'"
        )
        con.execute(
            "INSERT INTO list_songs (list_key, position, song_key) VALUES ('spotify:likes:1', 3, 'spotify:s1b')"
        )
    con.close()
    rows = owner.get("/lists/spotify:likes:1").text.split("<tbody>", 1)[1].split("</tr>")[:4]
    assert all('<span class="nowrap"><span class="chip' in r and "spotify-best" in r for r in rows)
    assert "The same song as #1, another release at its source: one file, recorded once" in rows[3]
    assert "Spotify no longer plays it (taken down): it cannot be recorded, this copy stays" in rows[0]
    assert set(recording.GONE) == set(availability.UNPLAYABLE)


def test_spotify_lossy_only_is_for_good_and_migrated(settings: Settings) -> None:
    """lossy_only blocks for good; the schema 31 migration turns old lossy_only answers into blocks."""
    con = db.connect(settings.db_path)
    with con:
        con.execute("INSERT INTO recorders (user_id, name, token, created) VALUES (1, 'PC', 't', 'x')")
        con.execute(
            "INSERT INTO recordings (user_id, recorder_id, title, below, state, created) VALUES (1, 1, 'S', 'lossless', 'done', 'x')"
        )
        item = "INSERT INTO recording_items (recording_id, position, song_key, state, result, detail, finished) "
        con.execute(
            item + "VALUES (1, 0, 'spotify:s1', 'done', 'lossy_only', 'grid check failed', '2020-01-01T00:00:00')"
        )
        con.execute("DROP TABLE spotify_best")
        con.execute("ALTER TABLE recording_items DROP COLUMN uri")
        for column in ("peer", "peer_path", "arrived"):  # (schema 32's)
            con.execute(f"ALTER TABLE events DROP COLUMN {column}")
        con.execute("ALTER TABLE lossy_sourced DROP COLUMN master")  # (schema 33's)
        con.execute("DROP TABLE lossy_checks")
        con.execute("ALTER TABLE availability DROP COLUMN plays")  # (schema 35's)
        con.execute("ALTER TABLE list_songs DROP COLUMN added")  # (schema 38's)
        con.execute("ALTER TABLE files DROP COLUMN pages")  # (schema 41's)
        con.execute("ALTER TABLE files DROP COLUMN tier")  # (schema 43's)
        con.execute("DROP TABLE free_links")  # (schema 42's)
        con.execute("DROP TABLE origins")  # (schema 44's)
        con.execute("DROP INDEX events_path")  # (schema 45's)
        con.execute("DROP INDEX events_song")
        con.execute("PRAGMA user_version = 30")
    con.close()
    db.init(settings.db_path)
    con = db.connect(settings.db_path)
    row = recording.blocks(con)["spotify:s1"]
    assert (row["reason"], row["uri"], row["since"]) == ("lossy", "spotify:track:s1", "2020-01-01T00:00:00")
    assert "spotify:s1" not in [t.key for t in recording.candidates(con, 1)]  # years later
    con.close()


def test_closing_a_sessions_summary_closes_it(app: FastAPI, login: Callable[..., TestClient]) -> None:
    """An ended session's panel has no hx-get (a click inside would reload it) and closing it shows no older one."""
    owner = login(app)
    connect(owner, app)
    for _ in range(2):
        owner.post("/recordings", data={"recorder": 1, "below": "lossless"})
        con = db.connect(app.state.settings.db_path)
        sid = con.execute("SELECT max(id) FROM recordings").fetchone()[0]
        recording.control(con, sid, "stop")
        con.close()
    panel = owner.get("/recordings/panel").text
    assert "Close" in panel and "hx-get=" not in panel.split("<section", 1)[1].split(">", 1)[0]
    after = owner.post(f"/recordings/{sid}/dismiss").text
    assert 'id="recording" hidden' in after


@pytest.mark.parametrize(
    ("stream", "tier", "reason"),
    [("lossless", "fake", "recorded"), ("lossy", "fake", "lossy"), ("", "lossy-high", "lossy")],
)
def test_a_recording_made_from_lossy(
    app: FastAPI, login: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch, stream: str, tier: str, reason: str
) -> None:
    """A recording judged made from lossy is filed with that tier whatever the stream, and blocks the song:
    "lossy" when the stream (device or file) was lossy, else "recorded"."""
    imported: list[tuple[str, str]] = []

    def add(paths, batch, name, fileobj, keep_hires=False):
        f = upload.File(1, name, Path("/x/rec.flac"), 200, 900 if tier == "fake" else 320, tier, source="AAC ~258 kbps")
        upload._save(upload.folder(paths, batch), f)
        return f

    def import_files(con, paths, vault, batch, chosen, remove=frozenset(), source="manual"):
        f = upload.files(paths, batch)[0]
        imported.append((f.tier, f.source))
        return [("Artist B – Second Song: replaced the copy: Artist B/Artist B - Second Song.flac", "spotify:s2")]

    monkeypatch.setattr(upload, "add", add)
    monkeypatch.setattr(upload, "fit", lambda con, f, song, tracks: upload.Fit(0, 0.97, 0.95))
    monkeypatch.setattr(upload, "import_files", import_files)
    monkeypatch.setattr(audio, "spectrum", lambda path: {"verdict": "lossy", "cutoff_hz": 16000})  # the copy's
    owner = login(app)
    device = as_device(app, connect(owner, app))
    con = db.connect(app.state.settings.db_path)
    with con:  # put the fake FLAC behind s3, the list's only song
        con.execute("UPDATE songs SET unavailable = NULL WHERE key = 'spotify:s3'")
        con.execute("UPDATE songs SET file = 'Artist B/Artist B - Second Song.flac' WHERE key = 'spotify:s3'")
    con.close()
    owner.post("/recordings", data={"recorder": 1, "below": "lossless", "list": "spotify:playlist:BBB222"})
    item = poll(device)["item"]
    files = {"file": ("rec.flac", b"fLaC", "audio/flac")}
    r = device.post(f"/api/recorder/items/{item['id']}/file", files=files, data={"stream": stream}).json()
    con = db.connect(app.state.settings.db_path)
    best = recording.blocks(con).get(item["key"])
    con.close()
    assert r["result"] == "filed" and imported == [(tier, "AAC ~258 kbps")]
    assert r["recording"] == {"tier": tier, "source": "AAC ~258 kbps"}
    assert best["reason"] == reason


@pytest.fixture
def files_in(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Uploads pass the checks and are filed; returns the keys filed."""
    filed: list[str] = []
    monkeypatch.setattr(
        upload,
        "add",
        lambda paths, batch, name, fileobj, keep_hires=False: upload.File(
            1, name, Path("/x/r.flac"), 200, 900, "lossless"
        ),
    )
    monkeypatch.setattr(upload, "fit", lambda con, f, song, tracks: upload.Fit(0, 0.97, 0.95))
    monkeypatch.setattr(upload, "compare", lambda paths, f, song: None)

    def import_files(con, paths, vault, batch, chosen, remove=frozenset(), source="manual"):
        filed.extend(chosen.values())
        return [(f"{k}: filed as x", k) for k in chosen.values()]

    monkeypatch.setattr(upload, "import_files", import_files)
    return filed


def item_row(app: FastAPI, item_id: int) -> tuple:
    con = db.connect(app.state.settings.db_path)
    row = con.execute(
        "SELECT state, tries, detail, lease_until FROM recording_items WHERE id = ?", (item_id,)
    ).fetchone()
    con.close()
    return tuple(row)


def test_a_restart_or_a_stop_releases_the_song(
    app: FastAPI, login: Callable[..., TestClient], files_in: list[str]
) -> None:
    """A fresh poll after a device restart, or a stopped session, requeues the song without a try; its upload
    is still accepted."""
    owner = login(app)
    device = as_device(app, connect(owner, app))
    owner.post("/recordings", data={"recorder": 1, "below": "lossless"})
    item = poll(device)["item"]
    r = device.post("/api/recorder/poll?wait=0", json={"state": "busy", "fresh": True})
    assert r.json()["item"] is None and item_row(app, item["id"])[:3] == ("queued", 0, recording.RELEASED)
    up = device.post(f"/api/recorder/items/{item['id']}/file", files={"file": ("r.flac", b"fLaC", "audio/flac")})
    assert up.json()["result"] == "filed" and files_in == [item["key"]]  # saved before the restart
    again = poll(device)["item"]
    assert again["key"] != item["key"]
    owner.post(f"/recordings/{1}/stop")
    assert item_row(app, again["id"])[:3] == ("queued", 0, recording.STOPPED)
    late = device.post(f"/api/recorder/items/{again['id']}/file", files={"file": ("r.flac", b"fLaC", "audio/flac")})
    assert late.json()["result"] == "filed" and late.json()["session"]["state"] == "stopped"
    assert (
        device.post(f"/api/recorder/items/{again['id']}/file", files={"file": ("r.flac", b"x", "audio/flac")}).json()[
            "result"
        ]
        == "filed"
    )


def test_a_late_took_over_costs_no_second_try(app: FastAPI, login: Callable[..., TestClient]) -> None:
    """A took_over after the device restarted (the song released, its try given back) gives no try back again:
    tries never goes below 0."""
    owner = login(app)
    device = as_device(app, connect(owner, app))
    owner.post("/recordings", data={"recorder": 1, "below": "lossless"})
    item = poll(device)["item"]
    device.post("/api/recorder/poll?wait=0", json={"state": "busy", "fresh": True})
    device.post(f"/api/recorder/items/{item['id']}/result", json={"result": "took_over"})
    assert item_row(app, item["id"])[:2] == ("queued", 0)


def test_a_new_request_stops_the_old_session(app: FastAPI, login: Callable[..., TestClient]) -> None:
    """One session per device: a new one stops the running one, releases its song and says so."""
    owner = login(app)
    device = as_device(app, connect(owner, app))
    assert "recording (stops that session)" not in owner.get("/recordings/new").text
    r = owner.post("/recordings", data={"recorder": 1, "below": "lossless"}, follow_redirects=False)
    assert r.headers["location"] == "/library"  # no message: nothing stopped
    item = poll(device)["item"]
    assert "recording (stops that session)" in owner.get("/recordings/new").text
    r = owner.post("/recordings", data={"recorder": 1, "below": "lossless"}, follow_redirects=False)
    assert "previous+session" in r.headers["location"] and "stopped" in r.headers["location"]
    con = db.connect(app.state.settings.db_path)
    states = [tuple(x) for x in con.execute("SELECT id, state FROM recordings ORDER BY id")]
    con.close()
    assert states == [(1, "stopped"), (2, "running")] and item_row(app, item["id"])[:2] == ("queued", 0)
    assert poll(device)["session"]["id"] == 2


def test_a_song_that_fails_to_start_goes_to_the_end(app: FastAPI, login: Callable[..., TestClient]) -> None:
    """A song that fails to start goes to the end of its session, and of a new session within a day."""
    owner = login(app)
    device = as_device(app, connect(owner, app))
    owner.post("/recordings", data={"recorder": 1, "below": "lossless"})
    first = poll(device)["item"]
    device.post(
        f"/api/recorder/items/{first['id']}/result", json={"result": "stalled", "detail": "No silence before the track"}
    )
    second = poll(device)["item"]
    assert second["key"] != first["key"]
    device.post(f"/api/recorder/items/{second['id']}/result", json={"result": "took_over"})  # no try, keeps its place
    device.post("/api/recorder/session", json={"action": "resume"})
    assert poll(device)["item"]["key"] == second["key"]
    r = owner.post("/recordings", data={"recorder": 1, "below": "lossless"}, follow_redirects=False)
    assert r.status_code == 303
    con = db.connect(app.state.settings.db_path)
    order = [x[0] for x in con.execute("SELECT song_key FROM recording_items WHERE recording_id = 2 ORDER BY position")]
    con.close()
    assert order == [second["key"], first["key"]]


def test_stop_start_stop_start(app: FastAPI, login: Callable[..., TestClient], files_in: list[str]) -> None:
    """Repeated stop/start (browser, device, device restart): at most one session runs, nothing stays leased,
    no try is lost or doubled, late uploads come in, every song stays offered."""
    owner = login(app)
    device = as_device(app, connect(owner, app))
    upload_ = {"file": ("r.flac", b"fLaC", "audio/flac")}

    def state() -> tuple[list, list]:
        con = db.connect(app.state.settings.db_path)
        active = [
            tuple(r) for r in con.execute("SELECT id, state FROM recordings WHERE state IN ('running', 'paused')")
        ]
        leased = [tuple(r) for r in con.execute("SELECT id, tries FROM recording_items WHERE state = 'recording'")]
        con.close()
        return active, leased

    def start() -> int:
        assert (
            owner.post("/recordings", data={"recorder": 1, "below": "lossless"}, follow_redirects=False).status_code
            == 303
        )
        con = db.connect(app.state.settings.db_path)
        sid = con.execute("SELECT max(id) FROM recordings").fetchone()[0]
        con.close()
        return sid

    s1 = start()
    a = poll(device)["item"]
    owner.post(f"/recordings/{s1}/stop")  # 1. stop in the browser while A records
    assert state() == ([], [])
    s2 = start()  # 2. start again: A is first again, its try not lost
    b = poll(device)["item"]
    assert b["key"] == a["key"] and item_row(app, b["id"])[1] == 1
    up = device.post(f"/api/recorder/items/{a['id']}/file", files=upload_)  # A of session 1, late
    assert up.json()["result"] == "filed" and files_in == [a["key"]]
    device.post("/api/recorder/session", json={"action": "pause"})  # 3. the device's own Stop: pause, then took_over
    device.post(
        f"/api/recorder/items/{b['id']}/result", json={"result": "took_over", "detail": "listening was stopped"}
    )
    assert state() == ([(s2, "paused")], []) and item_row(app, b["id"])[:2] == ("queued", 0)
    s3 = start()  # 4. start again while paused: the paused one is stopped
    assert state() == ([(s3, "running")], [])
    c = poll(device)["item"]
    r = device.post("/api/recorder/poll?wait=0", json={"state": "idle", "fresh": True})  # 5. the device restarts
    d = r.json()["item"]
    assert d["id"] == c["id"] and item_row(app, c["id"])[:2] == ("recording", 1)  # released, handed out again
    assert state() == ([(s3, "running")], [(c["id"], 1)])  # try not counted twice
    owner.post(f"/recordings/{s3}/stop")  # 6. stop, 7. start
    assert state() == ([], [])
    s4 = start()
    e = poll(device)["item"]
    assert state() == ([(s4, "running")], [(e["id"], 1)])
    con = db.connect(app.state.settings.db_path)
    offered = [t.key for t in recording.candidates(con, 1)]
    failed = con.execute("SELECT count(*) FROM recording_items WHERE result = 'failed'").fetchone()[0]
    con.close()
    assert failed == 0 and e["key"] in offered


def test_the_lists_own_track_is_the_song(settings: Settings) -> None:
    """A recording of the list's own track, as Spotify lists it and as long, is the song though the preview
    says otherwise (Deezer's release under its ISRC may be another: LilC4 - V4MP); a relinked one, or one of
    another length, is checked as before."""
    con = db.connect(settings.db_path)
    assert recording.as_listed(con, "spotify:s1", "spotify:track:s1")
    assert not recording.as_listed(con, "spotify:s1", "spotify:track:s1b")
    with con:
        con.execute("INSERT INTO availability VALUES ('spotify:s1', 'replaced', '', 'x', 'spotify:s1b', NULL, NULL)")
    assert not recording.as_listed(con, "spotify:s1", "spotify:track:s1")  # Spotify plays another release
    con.close()
    other = upload.Fit(1, 0.6, 0.29)
    assert upload.recorded_different(other, True, False) == (
        "",
        "the list's own track, though another recording than the release's (fingerprint 0.60, waveform 0.29)",
    )
    assert upload.recorded_different(upload.Fit(6, 0.6, 0.29), True, False)[0].startswith("another recording")
    assert upload.recorded_different(other, False, False)[0].startswith("another recording")
    assert upload.recorded_different(upload.Fit(0, 0.9, 0.95, 0.5, 0.1), False, True) == ("", "")
    assert upload.recorded_different(upload.Fit(0, None, None, 0.5, 0.1), False, True)[0].endswith(
        "than your copy (fingerprint 0.50, waveform 0.10)"
    )
