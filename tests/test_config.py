import io
import os
import sqlite3
from collections.abc import Callable, Iterator

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from echolot import db
from echolot.config import Settings
from echolot.jobs import schedule
from echolot.settings import configfile, options, sources, vault
from echolot.web import create_app


@pytest.fixture
def con(settings: Settings) -> Iterator[sqlite3.Connection]:
    c = db.connect(settings.db_path)
    yield c
    c.close()


def test_options(con: sqlite3.Connection) -> None:
    assert options.get(con, options.Soulseek).parallel == 4  # never saved: defaults
    options.update(con, options.Soulseek, parallel=2)
    assert options.get(con, options.Soulseek).parallel == 2
    with pytest.raises(options.OptionsError, match="parallel"):
        options.update(con, options.Soulseek, parallel=0)
    options.put_raw(con, "auth", {"session_days": "many", "unknown": 1})  # an old or broken value
    assert options.get(con, options.Auth).session_days == 30


def test_migration_moves_refresh_minutes(tmp_path) -> None:
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    for script in db.MIGRATIONS[:7]:  # a database from before v8
        c.executescript(script)
    c.execute("PRAGMA user_version = 7")
    c.execute("INSERT INTO meta VALUES ('refresh_minutes', '9')")
    c.commit()
    c.close()
    db.init(path)
    c = db.connect(path)
    assert options.raw(c, "echolot") == {"refresh_minutes": 9}
    assert db.get_meta(c, "refresh_minutes") == ""
    c.close()


def test_vault(settings: Settings, con: sqlite3.Connection) -> None:
    v = vault.Vault.from_env(settings.data_dir, {})
    key_file = settings.data_dir / "secret.key"
    assert v.source == str(key_file) and oct(os.stat(key_file).st_mode & 0o777) == "0o600"
    with con:
        v.set(con, "soundcloud.token", "s3cret-value")
    stored = con.execute("SELECT value FROM secrets").fetchone()[0]
    assert b"s3cret-value" not in stored
    assert v.get(con, "soundcloud.token") == "s3cret-value"
    assert vault.Vault.from_env(settings.data_dir, {}).get(con, "soundcloud.token") == "s3cret-value"
    other = vault.Vault(Fernet.generate_key(), "test")
    with pytest.raises(vault.VaultError, match="another key"):
        other.get(con, "soundcloud.token")
    with pytest.raises(vault.VaultError, match="not a Fernet key"):
        vault.Vault.from_env(settings.data_dir, {"ECHOLOT_SECRET_KEY": "short"})
    listing = {s["name"]: s for s in vault.listing(con)}
    assert listing["soundcloud.token"]["updated"] and not listing["spotify.refresh_token"]["updated"]
    with con:
        assert v.delete(con, "soundcloud.token") and not v.delete(con, "soundcloud.token")


def test_export_import_round_trip(con: sqlite3.Connection) -> None:
    text = configfile.export_text(con)
    data = yaml.safe_load(text)
    assert set(data) == {"version", "sources", "schedule", "settings"}
    assert "sources" not in data["settings"] and data["schedule"]["fallback"] == 120
    assert configfile.preview(con, text) == ""
    data["sources"]["spotify"]["playlists"].append("https://open.spotify.com/playlist/NEW1")
    data["schedule"]["sync"] = 20
    data["settings"]["soulseek"] = {"parallel": 3}
    changed = configfile.dump(data)
    diff = configfile.preview(con, changed)
    assert "+      - https://open.spotify.com/playlist/NEW1" in diff and "+  sync: 20" in diff
    assert configfile.preview(con, changed) == diff  # the preview stored nothing
    configfile.apply(con, changed)
    assert "spotify:playlist:NEW1" in [s.key for s in sources.lists(con)]
    assert schedule.rules(con)["sync"] == 20
    assert options.get(con, options.Soulseek).parallel == 3


def test_partial_import_keeps_the_rest(con: sqlite3.Connection) -> None:
    before = sources.as_config(con)
    configfile.apply(con, "schedule:\n  probe: off\n")
    assert sources.as_config(con) == before and schedule.rules(con)["probe"] is None
    assert schedule.rules(con)["sync"] == 30


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[1, 2]", "mapping"),
        ("sourcez: {}", "Unknown part"),
        ("version: 2", "format version 2"),
        ("sources:\n  spotfy: {}", "sources: Unknown setting"),
        ("schedule:\n  sync: 1", "at least"),
        ("settings:\n  auth: {session_days: 0}", "session_days"),
        ("settings:\n  nope: {}", "unknown section"),
        ("settings:\n  sources: {}", "unknown section"),
        ("schedule: [", "Not valid YAML"),
    ],
)
def test_import_rejects(con: sqlite3.Connection, text: str, message: str) -> None:
    before = configfile.export_data(con)
    with pytest.raises(configfile.ConfigError, match=message):
        configfile.apply(con, text)
    assert configfile.export_data(con) == before


def test_config_pages(settings: Settings, login: Callable[..., TestClient]) -> None:
    client = login(create_app(settings))
    r = client.get("/settings/config/export")
    assert "attachment" in r.headers["content-disposition"]
    data = yaml.safe_load(r.text)
    data["schedule"]["probe"] = "off"
    upload = {"file": ("echolot.yml", io.BytesIO(configfile.dump(data).encode()), "text/yaml")}
    html = client.post("/settings/config/import", files=upload).text
    assert "-  probe: 60" in html and "+  probe: &#39;off&#39;" in html
    r = client.post("/settings/config/import/apply", data={"text": configfile.dump(data)})
    assert "Configuration imported" in r.text
    con = db.connect(settings.db_path)
    assert schedule.rules(con)["probe"] is None
    con.close()
    html = client.post("/settings/config/import", files={
        "file": ("x.yml", io.BytesIO(b"sourcez: 1"), "text/yaml")}).text  # fmt: skip
    assert "Not imported: Unknown part" in html
    api = client.get("/api/config").json()
    assert api["schedule"]["probe"] == "off"
    r = client.put("/api/config", json={"schedule": {"probe": 30}}, params={"dry_run": True})
    assert r.json()["changed"] and not r.json()["applied"]
    assert client.put("/api/config", json={"schedule": {"probe": 1}}).status_code == 422


def test_cli_config_and_secrets(settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys, tmp_path) -> None:
    from echolot.cli import main

    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(settings.data_dir))
    out = tmp_path / "echolot.yml"
    assert main(["config", "export", str(out)]) == 0
    out.write_text(out.read_text().replace("fallback: 120", "fallback: 90"))
    assert main(["config", "import", str(out), "--dry-run"]) == 0
    assert "+  fallback: 90" in capsys.readouterr().out
    con = db.connect(settings.db_path)
    assert schedule.rules(con)["fallback"] == 120
    assert main(["config", "import", str(out)]) == 0
    assert schedule.rules(con)["fallback"] == 90
    con.close()
    monkeypatch.setattr("sys.stdin", io.StringIO("tok-123\n"))
    assert main(["secret", "set", "soundcloud.token", "--stdin"]) == 0
    main(["secret", "list"])
    listed = capsys.readouterr().out
    assert "soundcloud.token\tset" in listed and "tok-123" not in listed
    assert main(["secret", "delete", "soundcloud.token"]) == 0
    assert main(["jobs", "resume"]) == 0
