from collections.abc import Callable

import pytest
from conftest import PASSWORD
from fastapi.testclient import TestClient

from echolot import db
from echolot.config import Settings
from echolot.settings import auth, options
from echolot.web import create_app

HTML = {"accept": "text/html"}


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


def test_password_hash() -> None:
    stored = auth.hash_password("a good password")
    assert stored.startswith("scrypt$") and "a good password" not in stored
    assert auth.check_password("a good password", stored)
    assert not auth.check_password("a bad password", stored)
    assert not auth.check_password("x", "garbage")


def test_login_required(app) -> None:
    con = db.connect(app.state.settings.db_path)
    auth.add_user(con, "admin", PASSWORD)
    con.close()
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/favicon.ico").headers["content-type"] == "image/vnd.microsoft.icon"
    r = client.get("/sources", headers=HTML, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/login?next=%2Fsources")
    r = client.get("/api/stats")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert client.post("/sources/add", data={"url": "x"}).status_code == 401


def test_login_and_logout(app) -> None:
    con = db.connect(app.state.settings.db_path)
    auth.add_user(con, "anna", PASSWORD)
    con.close()
    client = TestClient(app)
    r = client.post("/login", data={"name": "anna", "password": "wrong password"})
    assert r.status_code == 400 and "Wrong user name or password" in r.text
    r = client.post("/login", data={"name": "Anna", "password": PASSWORD, "next": "/review"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/review")
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "anna" in client.get("/").text
    r = TestClient(app).post("/login", data={"name": "anna", "password": PASSWORD,
                             "next": "//evil.example"}, follow_redirects=False)  # fmt: skip
    assert r.headers["location"] == "/"  # no redirect to another site
    page = client.get("/settings").text
    csrf = page.split('name="csrf_token" value="')[1].split('"')[0]
    assert client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
    assert client.get("/api/stats").status_code == 401


def test_throttle(app) -> None:
    client = TestClient(app)
    for _ in range(5):
        assert client.post("/login", data={"name": "x", "password": "y"}).status_code == 400
    r = client.post("/login", data={"name": "x", "password": "y"})
    assert r.status_code == 429 and "Too many failed logins" in r.text


def test_csrf(app, login: Callable[..., TestClient]) -> None:
    client = login(app)
    token = client.headers.pop("X-CSRF-Token")
    r = client.post("/sources/options", data={"version": "x"})
    assert r.status_code == 403 and "CSRF" in r.text
    r = client.post("/sources/options", data={"version": "x", "csrf_token": "wrong"})
    assert r.status_code == 403
    r = client.post("/sources/options", data={"version": "x", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303  # passed the check (the stale version is the page's problem)
    r = client.post("/sources/options", data={"version": "x"}, headers={"X-CSRF-Token": token}, follow_redirects=False)
    assert r.status_code == 303


def test_session_expiry(app, login: Callable[..., TestClient]) -> None:
    client = login(app)
    assert client.get("/api/stats").status_code == 200
    con = db.connect(app.state.settings.db_path)
    with con:
        con.execute("UPDATE sessions SET expires = '2000-01-01T00:00:00'")
    con.close()
    assert client.get("/api/stats").status_code == 401


def test_api_tokens(app, login: Callable[..., TestClient]) -> None:
    client = login(app)
    html = client.post("/settings/tokens", data={"name": "grafana"}).text
    token = html.split('id="new-token" type="text" value="')[1].split('"')[0]
    assert token.startswith("echolot_")
    bearer = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    assert bearer.get("/api/stats").status_code == 200
    # no CSRF for token requests (no cookie that a foreign page could make the browser send)
    assert bearer.put("/api/config", params={"dry_run": True}, json={}).json()["changed"] is False
    assert TestClient(app, headers={"Authorization": "Bearer nope"}).get("/api/stats").status_code == 401
    con = db.connect(app.state.settings.db_path)
    (tid,) = con.execute("SELECT id FROM api_tokens").fetchone()
    assert con.execute("SELECT last_used FROM api_tokens").fetchone()[0]
    con.close()
    assert "grafana" in client.get("/settings").text and token not in client.get("/settings").text
    client.post(f"/settings/tokens/{tid}/revoke")
    assert bearer.get("/api/stats").status_code == 401


def test_metrics_public_setting(app, login: Callable[..., TestClient]) -> None:
    anonymous = TestClient(app)
    assert anonymous.get("/metrics").status_code == 200
    client = login(app)
    client.post("/settings/access", data={"session_days": 7})  # checkbox off
    assert anonymous.get("/metrics").status_code == 401
    assert client.get("/metrics").status_code == 200
    con = db.connect(app.state.settings.db_path)
    assert options.get(con, options.Auth).session_days == 7
    con.close()


def test_password_change(app, login: Callable[..., TestClient]) -> None:
    client = login(app)
    other = login(app)  # a second login of the same user
    r = client.post("/settings/password",
                    data={"current": "wrong", "password": "new password 1", "repeat": "new password 1"})  # fmt: skip
    assert "current password is wrong" in r.text
    r = client.post("/settings/password", data={"current": PASSWORD, "password": "short", "repeat": "short"})
    assert "at least 10" in r.text
    r = client.post("/settings/password",
                    data={"current": PASSWORD, "password": "new password 1", "repeat": "new password 1"})  # fmt: skip
    assert "Password changed" in r.text
    assert client.get("/api/stats").status_code == 200  # this browser got a new session
    assert other.get("/api/stats").status_code == 401


def test_first_visit_sets_admin_password(app) -> None:
    client = TestClient(app)
    r = client.get("/", headers=HTML, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/setup")
    assert client.get("/login", follow_redirects=False).headers["location"] == "/setup"
    assert "admin</strong> account" in client.get("/setup").text
    r = client.post("/setup", data={"password": "long enough 1", "repeat": "different 12"})
    assert r.status_code == 400 and "differ" in r.text
    r = client.post("/setup", data={"name": "someone", "password": "long enough 1",
                                    "repeat": "long enough 1"}, follow_redirects=False)  # fmt: skip
    assert r.status_code == 303
    assert client.get("/api/stats").status_code == 200  # logged in right away
    con = db.connect(app.state.settings.db_path)
    assert [u["name"] for u in auth.users(con)] == ["admin"]  # always the admin account
    con.close()
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"
    other = TestClient(app)  # nobody can set it again
    assert other.post("/setup", data={"password": "x" * 12, "repeat": "x" * 12}).status_code == 403


def test_admin_from_env(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ECHOLOT_ADMIN_PASSWORD", PASSWORD)
    create_app(settings)
    con = db.connect(settings.db_path)
    assert auth.get_user(con, "admin")
    con.close()


def test_cli_users(settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    import io

    from echolot.cli import main

    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(settings.data_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert main(["user", "add", "bob", "--password-stdin"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("another password\n"))
    assert main(["user", "passwd", "bob", "--password-stdin"]) == 0
    main(["user", "list"])
    assert "bob" in capsys.readouterr().out
    con = db.connect(settings.db_path)
    assert auth.verify(con, "bob", "another password")
    con.close()
    with pytest.raises(SystemExit, match="No user"):
        main(["user", "passwd", "nobody", "--password-stdin"])


def test_navidrome_accounts_log_in(app, monkeypatch: pytest.MonkeyPatch) -> None:
    """With Navidrome's address set, its accounts log in with their Navidrome password: an admin there may
    change things here, anyone else only look. Echolot's own accounts keep their own password, and work
    while Navidrome is down."""
    from echolot.services import navidrome

    accounts = {"timon": ("timon's password", False), "david": ("david's password", True), "anna": ("x", True)}
    asked: list[str] = []
    down = False

    def fake(url: str, name: str, password: str) -> tuple[str, bool] | None:
        asked.append(name)
        if down:
            raise navidrome.NavidromeError("connection refused")
        known = accounts.get(name.lower())
        return (name.lower(), known[1]) if known and known[0] == password else None

    def log_in(client: TestClient, name: str, password: str) -> int:
        return client.post("/login", data={"name": name, "password": password}, follow_redirects=False).status_code

    monkeypatch.setattr(navidrome, "login", fake)
    con = db.connect(app.state.settings.db_path)
    auth.add_user(con, "anna", PASSWORD)
    with con:
        options.update(con, options.Navidrome, url="http://navidrome.test:4533")
    con.close()
    assert "With your Navidrome account" in TestClient(app).get("/login").text
    timon = TestClient(app)
    assert log_in(timon, "Timon", "timon's password") == 303
    page = timon.get("/settings").text
    assert "View only" in page and "change its password in Navidrome" in page
    csrf = page.split('name="csrf_token" value="')[1].split('"')[0]
    r = timon.post("/sources/options", data={"csrf_token": csrf, "removed_playlists": "1"})
    assert r.status_code == 403 and "changes need a Navidrome admin" in r.text
    assert timon.post("/logout", data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
    david = TestClient(app)
    assert log_in(david, "david", "david's password") == 303
    page = david.get("/settings").text
    csrf = page.split('name="csrf_token" value="')[1].split('"')[0]
    assert "View only" not in page
    assert david.post("/sources/options", data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
    assert log_in(TestClient(app), "timon", "wrong") == 400
    asked.clear()  # an Echolot account's name: its own password only, Navidrome is not asked
    assert log_in(TestClient(app), "anna", "x") == 400 and not asked
    down = True
    r = TestClient(app).post("/login", data={"name": "timon", "password": "timon's password"})
    assert r.status_code == 502 and "Navidrome is not reachable" in r.text
    assert log_in(TestClient(app), "anna", PASSWORD) == 303
    down, accounts["david"] = False, ("david's password", False)  # no admin in Navidrome any more
    log_in(TestClient(app), "david", "david's password")
    con = db.connect(app.state.settings.db_path)
    david_now = auth.get_user(con, "david")
    assert david_now is not None and david_now.navidrome and not david_now.admin
    with pytest.raises(auth.AuthError, match="change it in Navidrome"):
        auth.set_password(con, david_now, PASSWORD)
    con.close()


def test_navidrome_login_asks_navidrome() -> None:
    """The login is Navidrome's own: POST /auth/login with the name and password (JSON); 401 is a wrong one."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from echolot.services import navidrome

    class Navidrome(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            ok = self.path == "/auth/login" and body == {"username": "Timon", "password": "secret"}
            self.send_response(200 if ok else 401)
            self.end_headers()
            answer = {"username": "timon", "isAdmin": False, "token": "jwt"} if ok else {"error": "Invalid"}
            self.wfile.write(json.dumps(answer).encode())

        def do_GET(self) -> None:
            self.send_response(200 if self.path == "/ping" else 404)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Navidrome)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        assert navidrome.login(url, "Timon", "secret") == ("timon", False)
        assert navidrome.login(url, "Timon", "wrong") is None
        assert navidrome.reachable(url)
    finally:
        server.shutdown()
    with pytest.raises(navidrome.NavidromeError):
        navidrome.login(url, "Timon", "secret")
    assert not navidrome.reachable(url)
