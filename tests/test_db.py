"""The database schema (db.py): created at the current version, older ones migrated step by step."""

from pathlib import Path

import pytest

from echolot import db


def columns(con, table: str) -> set[str]:
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}


def test_version_12_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "echolot.db"
    db.init(path)
    con = db.connect(path)
    con.executescript(
        "ALTER TABLE songs DROP COLUMN close_match; ALTER TABLE review_decisions DROP COLUMN name; "
        "ALTER TABLE events DROP COLUMN url; ALTER TABLE events DROP COLUMN compared; PRAGMA user_version = 12;"
    )
    con.close()
    db.init(path)
    con = db.connect(path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
    assert "close_match" in columns(con, "songs") and "name" in columns(con, "review_decisions")
    assert {"url", "compared"} <= columns(con, "events")
    con.close()


def test_an_unknown_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "echolot.db"
    db.init(path)
    con = db.connect(path)
    con.execute(f"PRAGMA user_version = {db.VERSION + 1}")
    con.close()
    with pytest.raises(RuntimeError, match="database schema"):
        db.init(path)
