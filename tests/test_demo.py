"""The README video's demo (tools/demo) on the current code: its data builds on this schema and keeps what the
scenes rely on, and every scene's page answers on it. A change that breaks the demo fails here, not at the next
release (the recording itself runs locally: tools/demo/make.py)."""

import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from echolot import db
from echolot.config import Settings
from echolot.settings import auth
from echolot.web import create_app

DEMO = Path(__file__).parent.parent / "tools" / "demo"
sys.path.insert(0, str(DEMO))

from scenes import SCENES  # noqa: E402


def test_the_demo_builds_and_its_scenes_pages_answer(tmp_path: Path) -> None:
    built = subprocess.run(
        [sys.executable, str(DEMO / "data.py"), str(tmp_path), "--fast"], capture_output=True, text=True
    )
    assert built.returncode == 0, built.stderr  # the contract at its end names what a scene would miss
    assert "not in the schema" not in built.stderr  # a column gone: leave it out in data.py too
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "music" / "tracks",
        host="127.0.0.1",
        port=0,
        daemon_dir=tmp_path / "daemon",
    )
    con = db.connect(settings.db_path)
    try:
        token = auth.create_session(con, auth.get_user(con, "rick"), 1)
    finally:
        con.close()
    client = TestClient(create_app(settings))
    client.cookies.set("echolot_session", token)
    for scene in SCENES:
        r = client.get(scene.path)
        assert r.status_code == 200 and "<main" in r.text, (scene.name, r.status_code)
