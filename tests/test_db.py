"""The database schema (db.py): created at the current version, older ones migrated step by step."""

from pathlib import Path

import pytest

from echolot import db
from echolot.settings import auth


def columns(con, table: str) -> set[str]:
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}


V12_USERS = (
    "id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, password TEXT NOT NULL, "
    "created TEXT NOT NULL, last_login TEXT"
)


def test_version_12_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "echolot.db"
    db.init(path)
    con = db.connect(path)
    con.executescript(
        "ALTER TABLE songs DROP COLUMN close_match; ALTER TABLE review_decisions DROP COLUMN name; "
        "ALTER TABLE review_decisions DROP COLUMN user_id; ALTER TABLE review_decisions DROP COLUMN overridden; "
        "ALTER TABLE events DROP COLUMN url; "
        "ALTER TABLE events DROP COLUMN compared; ALTER TABLE events DROP COLUMN peer_bytes; DROP TABLE users; "
        f"CREATE TABLE users ({V12_USERS}); PRAGMA user_version = 12;"
    )
    con.close()
    db.init(path)
    con = db.connect(path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
    assert "close_match" in columns(con, "songs") and {"name", "user_id"} <= columns(con, "review_decisions")
    assert {"url", "compared", "peer_bytes"} <= columns(con, "events")
    assert "password" not in columns(con, "users")  # Navidrome's accounts
    assert {"navidrome_id", "navidrome_admin", "permissions", "view", "disabled"} <= columns(con, "users")
    con.close()


def test_the_local_accounts_go(tmp_path: Path) -> None:
    """Version 17 to 18: the local account goes with its sessions; a Navidrome account stays, an admin."""
    path = tmp_path / "echolot.db"
    db.init(path)
    con = db.connect(path)
    con.executescript(
        "DROP TABLE users; "
        f"CREATE TABLE users ({V12_USERS}, source TEXT NOT NULL DEFAULT 'local', admin INTEGER NOT NULL DEFAULT 1); "
        "INSERT INTO users (id, name, password, created) VALUES (1, 'admin', 'scrypt$x', '2026-09-29'); "
        "INSERT INTO users (id, name, password, created, source, admin) VALUES (2, 'david', '', '2026-10-02', 'navidrome', 1); "
        "INSERT INTO sessions VALUES ('s1', 1, 'c', 'now', '2099-01-01', 'now'), ('s2', 2, 'c', 'now', '2099-01-01', 'now'); "
        "ALTER TABLE review_decisions DROP COLUMN user_id; ALTER TABLE review_decisions DROP COLUMN overridden; "
        "PRAGMA user_version = 17;"
    )
    con.close()
    db.init(path)
    con = db.connect(path)
    rows = [tuple(r) for r in con.execute("SELECT name, admin, navidrome_admin, disabled FROM users")]
    assert rows == [("david", 1, 1, 0)]
    assert [r[0] for r in con.execute("SELECT user_id FROM sessions")] == [2]
    timon = auth.logged_in(con, "timon", "nd-timon", False)  # the column's old DEFAULT 1 must not make an admin
    assert not timon.admin and not auth.get_user(con, "timon").admin
    con.close()


def test_an_unknown_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "echolot.db"
    db.init(path)
    con = db.connect(path)
    con.execute(f"PRAGMA user_version = {db.VERSION + 1}")
    con.close()
    with pytest.raises(RuntimeError, match="database schema"):
        db.init(path)


def test_the_history_becomes_the_owners(tmp_path: Path) -> None:
    """Version 19 to 20: until then every list was one user's, so the song history so far is theirs."""
    path = tmp_path / "echolot.db"
    db.init(path)
    con = db.connect(path)
    con.executescript(
        "INSERT INTO users (id, name, created) VALUES (7, 'david', 'x'); "
        "INSERT INTO sources (user_id, key, service, url, position, added) VALUES (7, 'k', 'spotify', 'u', 0, 'x'); "
        "INSERT INTO snapshots VALUES ('t1', 'songs_wanted', 'spotify', 10), ('t1', 'songs_wanted', 'soundcloud', 5), "
        "('t1', 'songs_by_quality', 'lossless', 8); "
        "ALTER TABLE review_decisions DROP COLUMN overridden; PRAGMA user_version = 19;"
    )
    con.close()
    db.init(path)
    con = db.connect(path)
    rows = {(m, k): v for m, k, v in con.execute("SELECT metric, key, value FROM snapshots WHERE metric LIKE 'user_%'")}
    con.close()
    assert rows == {("user_songs_wanted", "7"): 15, ("user_songs_by_quality", "7:lossless"): 8}
