from pathlib import Path

from fastapi.testclient import TestClient

from echolot import __version__
from echolot.config import Settings
from echolot.web import create_app


def client(tmp_path: Path) -> TestClient:
    settings = Settings(data_dir=tmp_path, library_dir=None, host="127.0.0.1", port=0)
    return TestClient(create_app(settings))


def test_healthz(tmp_path: Path) -> None:
    response = client(tmp_path).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_dashboard_renders(tmp_path: Path) -> None:
    response = client(tmp_path).get("/")
    assert response.status_code == 200
    assert "Echolot" in response.text
    assert "not configured" in response.text


def test_static_stylesheet(tmp_path: Path) -> None:
    assert client(tmp_path).get("/static/style.css").status_code == 200
