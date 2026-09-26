import pytest

from echolot import __version__
from echolot.cli import main
from echolot.config import Settings


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_settings_defaults() -> None:
    settings = Settings.from_env({})
    assert settings.library_dir is None and settings.pipeline_dir is None
    assert (settings.host, settings.port) == ("127.0.0.1", 8490)


def test_settings_from_env() -> None:
    settings = Settings.from_env(
        {
            "ECHOLOT_LIBRARY_DIR": "/music",
            "ECHOLOT_PIPELINE_DIR": "/pipeline",
            "ECHOLOT_PORT": "9000",
            "ECHOLOT_HOST": "0.0.0.0",
        }
    )
    assert str(settings.library_dir) == "/music"
    assert str(settings.pipeline_dir) == "/pipeline"
    assert str(settings.db_path) == "data/echolot.db"
    assert (settings.host, settings.port) == ("0.0.0.0", 9000)
