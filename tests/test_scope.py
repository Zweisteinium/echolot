"""Whose songs the pages show (web/stats.scope): a user's own (the songs on the lists they follow), an
admin's own or everyone's as they chose; the Review and Run permissions for a user's own songs; an
admin taking back a user's decision; the history per user."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from echolot import db
from echolot.config import Settings
from echolot.jobs import acquire
from echolot.library import filing, history, review
from echolot.settings import auth, sources
from echolot.web import create_app, stats

HTML = {"accept": "text/html"}


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


def timon_follows_playlist_a(app) -> int:
    """timon follows Playlist A (First Song) only; the owner (user 1) has the whole small collection."""
    con = db.connect(app.state.settings.db_path)
    uid = auth.logged_in(con, "timon", "nd-timon", False).id
    sources.add_list(con, uid, "https://open.spotify.com/playlist/AAA111")
    con.close()
    return uid


def test_a_user_sees_their_own_songs(app, login: Callable[..., TestClient]) -> None:
    uid = timon_follows_playlist_a(app)
    timon = login(app, "timon", admin=False)
    con = db.connect(app.state.settings.db_path)
    mine, everyone = stats.overview(con, uid), stats.overview(con, None)
    con.close()
    assert (mine["wanted"], mine["have"], mine["scoped"]) == (1, 1, True)
    assert mine["library_files"] == everyone["files"] and everyone["wanted"] > mine["wanted"]
    assert [r["key"] for r in mine["lists"]] == ["spotify:playlist:AAA111"]
    missing = timon.get("/missing").text
    assert "Gone Song" not in missing  # the owner's missing song, on a list timon does not follow
    activity = timon.get("/activity").text
    assert "First Song" in activity and "Gone Song" not in activity
    assert timon.get("/lists/spotify:playlist:AAA111").status_code == 200
    assert timon.get("/lists/spotify:playlist:BBB222").status_code == 404  # not theirs
    owner = login(app)  # an admin: their own, then everyone's
    assert "Gone Song" in owner.get("/missing").text
    assert owner.post("/account/view", data={"view": "everyone"}, follow_redirects=False).status_code == 303
    home = owner.get("/stats").text
    assert "<h2>Library</h2>" in home and "owner, timon" in home  # everyone's; who follows Playlist A


def test_review_is_for_their_own_songs(app, login: Callable[..., TestClient]) -> None:
    uid = timon_follows_playlist_a(app)
    timon = login(app, "timon", admin=False)  # (sets the rights: none)
    con = db.connect(app.state.settings.db_path)
    auth.set_rights(con, uid, False, {"review"})
    gone = con.execute("SELECT id FROM events WHERE song = 'spotify:s3'").fetchone()[0]
    first = con.execute("SELECT id FROM events WHERE song = 'spotify:s1'").fetchone()[0]
    con.close()
    assert timon.get("/review").status_code == 200 and "Decisions by others" not in timon.get("/review").text
    r = timon.post(f"/review/{gone}", data={"decision": "wrong"})
    assert r.status_code == 403 and "another user" in r.text
    assert timon.post(f"/review/{first}", data={"decision": "ok"}).status_code == 200  # theirs (not up for review)
    assert timon.get(f"/review/{gone}/audio").status_code == 403


def test_an_admin_takes_back_a_users_decision(app, settings: Settings) -> None:
    """A No match a user made (applied: the download blocked, the file deleted) is lifted: the song is
    searched again; one not applied yet is dropped."""
    uid = timon_follows_playlist_a(app)
    con = db.connect(settings.db_path)
    eid = con.execute("SELECT id FROM events WHERE song = 'spotify:s1'").fetchone()[0]
    with con:
        con.execute("UPDATE events SET found = 'First Song (Bootleg)' WHERE id = ?", (eid,))
        con.execute("INSERT INTO blocked VALUES ('spotify:s1', 'First Song (Bootleg)')")
        con.execute(
            "INSERT INTO review_decisions (id, event_id, decision, decided, applied, result, user_id) "
            "VALUES ('d1', ?, 'discard', '2099-01-01T00:00:00', '2099-01-01T00:02:00', 'deleted', ?)",
            (eid, uid),
        )
    assert [d["user"] for d in review.by_others(con)] == ["timon"]
    paths = filing.Paths(settings.library_dir.parent)
    assert review.override(con, paths, eid, "owner") == "searched again"
    assert not con.execute("SELECT 1 FROM blocked").fetchone()
    assert con.execute("SELECT overridden FROM review_decisions").fetchone()[0].startswith("owner ")
    with pytest.raises(review.ConfigError, match="taken back already"):
        review.override(con, paths, eid, "owner")
    with con:
        con.execute("UPDATE review_decisions SET overridden = NULL, applied = NULL")
    assert review.override(con, paths, eid, "owner") == "dropped before it was applied"
    assert not con.execute("SELECT 1 FROM review_decisions").fetchone()
    con.close()


def test_search_my_missing_songs(app, login: Callable[..., TestClient], settings: Settings) -> None:
    """The Run permission: the search jobs by hand, for the user's songs only; the list check for all
    (it asks what changed, cheap)."""
    uid = timon_follows_playlist_a(app)
    timon = login(app, "timon", admin=False)
    assert timon.post("/jobs/mine", data={"what": "search"}).status_code == 403  # no permission yet
    con = db.connect(settings.db_path)
    auth.set_rights(con, uid, False, {"run"})
    con.close()
    assert timon.post("/jobs/mine", data={"what": "search"}).status_code == 200
    timon.post("/jobs/mine", data={"what": "check"})
    wk = app.state.worker
    assert wk.requested == {"sweep": {uid}, "fallback": {uid}, "sync": None, "youtube": None, "soundcloud": None}
    wk.trigger("sweep")  # an admin's Run now: everyone's
    assert wk.requested["sweep"] is None
    run = type("R", (), {"only": {uid}})()
    con = db.connect(settings.db_path)
    rows = acquire._missing(con)
    assert {r["key"] for r in rows} > {r["key"] for r in acquire._for(run, con, rows)}  # s3: the owner's
    con.close()


def test_history_per_user(app, settings: Settings) -> None:
    uid = timon_follows_playlist_a(app)
    con = db.connect(settings.db_path)
    rows = {(m, k): v for m, k, v in history.collect(con) if m.startswith("user_")}
    con.close()
    assert rows[("user_songs_wanted", f"{uid}")] == 1 and rows[("user_songs_in_library", f"{uid}")] == 1
    assert rows[("user_songs_wanted", "1")] == 5  # the owner's: every song of the small collection
    assert ("user_songs_by_quality", f"{uid}:lossy-high") in rows


def test_a_restored_file_names_its_song(settings: Settings) -> None:
    """A No match an admin takes back puts the library file back; Activity names the song it is for."""
    con = db.connect(settings.db_path)
    paths = filing.Paths(settings.library_dir.parent)
    rel = "Artist A/Artist A - First Song.mp3"
    kept = paths.inbox("replaced") / "2099-01-01" / rel
    kept.parent.mkdir(parents=True)
    (paths.tracks / rel).rename(kept)
    with con:
        con.execute(
            "INSERT INTO events (ts, action, path, reason) VALUES ('2099-01-01T00:03:00', 'retired', ?, ?)",
            (filing.event_path(paths, kept), f"no match in review (was {rel})"),
        )
    d = {"path": rel, "decided": "2099-01-01T00:00:00", "song": "spotify:s1"}
    assert review._restore(con, paths, d) == "file back"
    row = con.execute("SELECT action, artist, title FROM events ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(row) == ("restored", "Artist A", "First Song") and (paths.tracks / rel).is_file()
    assert review._replaced_file("/music/inbox/replaced/../../tracks/x.mp3", paths) is None  # only replaced/
    con.close()
