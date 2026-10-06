import pytest

from echolot import __version__
from echolot.cli import main
from echolot.config import Settings


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_settings_defaults() -> None:
    settings = Settings.from_env({})
    assert settings.library_dir is None and settings.daemon_dir is None
    assert (settings.host, settings.port) == ("127.0.0.1", 8490)


def test_settings_from_env() -> None:
    settings = Settings.from_env(
        {
            "ECHOLOT_LIBRARY_DIR": "/music/tracks",
            "ECHOLOT_DAEMON_DIR": "/daemon",
            "ECHOLOT_PORT": "9000",
            "ECHOLOT_HOST": "0.0.0.0",
        }
    )
    assert str(settings.library_dir) == "/music/tracks"
    with pytest.raises(ValueError, match="must be <music>/tracks"):
        Settings.from_env({"ECHOLOT_LIBRARY_DIR": "/music"})
    assert str(settings.daemon_dir) == "/daemon"
    assert str(settings.db_path) == "data/echolot.db"
    assert (settings.host, settings.port) == ("0.0.0.0", 9000)


def test_tags_normalize(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dry run reports and writes a report file; a run needs the jobs paused and none running."""
    from echolot import db
    from echolot.settings import options

    env = {"ECHOLOT_DATA_DIR": str(settings.data_dir), "ECHOLOT_LIBRARY_DIR": str(settings.library_dir)}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert main(["tags", "normalize", "--dry-run"]) == 0
    assert '"files"' in capsys.readouterr().out
    assert list((settings.data_dir / "tag-backups").glob("normalize-*-dry-run.json"))
    con = db.connect(settings.db_path)
    with con:
        options.update(con, options.Jobs, paused=False)
    con.close()
    with pytest.raises(SystemExit, match="Pause the jobs first"):
        main(["tags", "normalize"])


def test_jobs_pause_waits_for_the_running_ones(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """What a deploy does: remember whether the jobs were paused, pause them and wait until none runs."""
    from echolot import db
    from echolot.settings import options

    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(settings.data_dir))
    con = db.connect(settings.db_path)
    with con:
        options.update(con, options.Jobs, paused=False)
        con.execute(
            "INSERT OR REPLACE INTO jobs (name, started, finished) VALUES ('upgrade', '2026-10-01T21:30:00', NULL)"
        )
    assert main(["jobs", "status"]) == 0
    assert capsys.readouterr().out.splitlines() == ["paused: no", "running: upgrade"]
    assert main(["jobs", "pause", "--wait", "--timeout", "0"]) == 1  # still running
    assert "Still running after 0 min: upgrade." in capsys.readouterr().out
    assert options.get(con, options.Jobs).paused
    with con:
        con.execute("UPDATE jobs SET finished = '2026-10-01T21:40:00'")
    assert main(["jobs", "pause", "--wait"]) == 0
    assert main(["jobs", "resume"]) == 0 and not options.get(con, options.Jobs).paused
    con.close()
