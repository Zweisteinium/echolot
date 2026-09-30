"""The version is set in pyproject.toml only; everything that names it follows it."""

import re
import tomllib
from pathlib import Path

from echolot import __version__

ROOT = Path(__file__).parent.parent
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_the_package_reports_it() -> None:
    assert __version__ == VERSION  # after a bump: uv lock (CI and the image build use the lock file)


def test_the_quick_start_runs_it() -> None:
    image = re.search(r"image: lordlayer/echolot:(\S+)", (ROOT / "deploy" / "compose.yaml").read_text())
    assert image and image[1] == VERSION
