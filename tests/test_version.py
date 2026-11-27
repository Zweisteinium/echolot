"""The version is set in pyproject.toml only; everything that names it follows it."""

import importlib.util
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


def test_the_quick_start_runs_the_sockseek_it_builds() -> None:
    compose = (ROOT / "deploy" / "compose.yaml").read_text()
    image = re.search(r"image: lordlayer/echolot-sockseek:(\S+)", compose)
    tag = re.search(r"ARG TAG=(\S+)", (ROOT / "deploy" / "sockseek" / "Dockerfile").read_text())
    assert image and tag and image[1] == tag[1]  # a new TAG: CI pushes its image from main


def test_its_release_has_notes() -> None:
    """CI publishes the version's section of CHANGELOG.md (tools/release_notes.py) as its release notes."""
    spec = importlib.util.spec_from_file_location("release_notes", ROOT / "tools" / "release_notes.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    notes = module.notes(VERSION, "v0.0.1")
    assert notes.count("\n- ") >= 1 and "\n  " not in notes and notes.endswith("compare/v0.0.1...v" + VERSION + ")\n")
