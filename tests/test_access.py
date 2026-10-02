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


def log_in(client: TestClient, name: str, password: str = PASSWORD, **data: str) -> int:
    return client.post("/login", data={"name": name, "password": password, **data}, follow_redirects=False).status_code


def test_login_required(app) -> None:
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/favicon.ico").headers["content-type"] == "image/vnd.microsoft.icon"
    r = client.get("/sources", headers=HTML, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/login?next=%2Fsources")
    r = client.get("/api/stats")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert client.post("/sources/add", data={"url": "x"}).status_code == 401
    assert client.get("/setup").status_code == 401  # gone: there is no first account to set up


def test_login_with_navidrome_and_logout(app) -> None:
    """Navidrome checks the password; the account it answers with is logged in (as Navidrome spells it)."""
    client = TestClient(app)
    assert "With your Navidrome account" in client.get("/login").text
    r = client.post("/login", data={"name": "anna", "password": "wrong password"})
    assert r.status_code == 400 and "Wrong user name or password" in r.text
    r = client.post("/login", data={"name": "anna", "password": PASSWORD, "next": "/review"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/review")
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "anna" in client.get("/").text
    evil = {"name": "anna", "password": PASSWORD, "next": "//evil.example"}
    assert TestClient(app).post("/login", data=evil, follow_redirects=False).headers["location"] == "/"
    csrf = client.get("/account").text.split('name="csrf_token" value="')[1].split('"')[0]
    assert client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
    assert client.get("/account").status_code == 401
    con = db.connect(app.state.settings.db_path)
    anna = [(u["name"], u["navidrome_id"], u["admin"]) for u in auth.users(con) if u["name"] == "anna"]
    assert anna == [("anna", "nd-anna", 0)]
    con.close()


def test_navidrome_down_or_unknown(app, monkeypatch: pytest.MonkeyPatch) -> None:
    from echolot.services import navidrome

    def down(url: str, name: str, password: str) -> None:
        raise navidrome.NavidromeError("connection refused")

    monkeypatch.setattr(navidrome, "login", down)
    r = TestClient(app).post("/login", data={"name": "anna", "password": PASSWORD})
    assert r.status_code == 502 and "Navidrome is not reachable" in r.text
    monkeypatch.delenv("ECHOLOT_NAVIDROME_URL")
    r = TestClient(app).get("/login")
    assert "ECHOLOT_NAVIDROME_URL" in r.text and 'name="password"' not in r.text


def test_throttle(app) -> None:
    client = TestClient(app)
    for _ in range(5):
        assert client.post("/login", data={"name": "x", "password": "y"}).status_code == 400
    r = client.post("/login", data={"name": "x", "password": "y"})
    assert r.status_code == 429 and "Too many failed logins" in r.text


def test_rights(app, login: Callable[..., TestClient], fake_navidrome: dict[str, bool]) -> None:
    """Anyone else than an admin uses only their own pages (USER); every other route is an admin's, so a
    page made later is closed until it is opened on purpose. A Navidrome admin is always an admin here."""
    timon = login(app, "timon", admin=False)
    home = timon.get("/").text
    assert "Your files" in home and "in the library" in home and "/users" not in home and "Mine</button>" not in home
    assert timon.get("/account").status_code == 200
    r = timon.get("/settings", headers=HTML)
    assert r.status_code == 403 and "for admins" in r.text and "<html" in r.text  # a page, not JSON
    for path in ("/sources", "/accounts", "/missing", "/activity"):  # their own
        assert timon.get(path).status_code == 200, path
    r = timon.get("/review", headers=HTML)
    assert r.status_code == 403 and "the review permission" in r.text
    assert timon.post("/sources/options", data={}).status_code == 403  # everyone's option
    assert timon.post("/accounts/spotify/app", data={"client_id": "x" * 32}).status_code == 403
    assert timon.get("/accounts/soulseek").status_code == 403
    assert timon.get("/api/stats").status_code == 403
    assert timon.post("/jobs/start", data={"names": "sync"}).status_code == 403
    assert timon.post("/users/1", data={"admin": "1"}).status_code == 403
    fake_navidrome["boss"] = True
    boss = TestClient(app)
    assert log_in(boss, "boss") == 303 and boss.get("/sources").status_code == 200  # Navidrome's admin
    david = login(app, "david")  # an admin given in Echolot
    assert david.get("/users").status_code == 200 and "boss" in david.get("/users").text
    con = db.connect(app.state.settings.db_path)
    ids = {u["name"]: u["id"] for u in auth.users(con)}
    con.close()
    assert david.post(f"/users/{ids['timon']}", data={"review": "1"}, follow_redirects=False).status_code == 303
    r = david.post(f"/users/{ids['david']}", data={}, follow_redirects=True)
    assert "take your own admin rights away" in r.text
    assert david.post(f"/users/{ids['boss']}", data={}, follow_redirects=False).status_code == 303
    con = db.connect(app.state.settings.db_path)
    timon_now, boss_now, david_now = (auth.get_user(con, n) for n in ("timon", "boss", "david"))
    con.close()
    assert timon_now and timon_now.permissions == {"review"} and not timon_now.admin and timon_now.can("review")
    assert boss_now and boss_now.admin  # a Navidrome admin's box is locked
    assert david_now and david_now.admin and david_now.view == "mine"
    assert david.post("/account/view", data={"view": "everyone"}, follow_redirects=False).status_code == 303
    assert timon.post("/account/view", data={"view": "everyone"}).status_code == 403  # a view is an admin's


def test_navidrome_users_renew_the_rights(app, login: Callable[..., TestClient]) -> None:
    """Navidrome's user list: who is a Navidrome admin now; an account gone is disabled, logged out
    everywhere and its API tokens end; renamed, it keeps its rights."""
    timon = login(app, "timon", admin=False)
    login(app, "david")
    con = db.connect(app.state.settings.db_path)
    auth.create_token(con, auth.get_user(con, "timon"), "script")
    changes = auth.sync_users(con, [{"id": "nd-david", "userName": "David", "isAdmin": True}])
    assert "timon is gone from Navidrome: disabled" in changes and "David: Navidrome admin yes" in changes
    assert auth.get_user(con, "timon") is None and auth.get_user(con, "david").admin
    assert con.execute("SELECT count(*) FROM api_tokens").fetchone()[0] == 0
    assert auth.sync_users(con, []) == []  # Navidrome said nothing: nothing changes
    con.close()
    assert timon.get("/account").status_code == 401
    assert log_in(TestClient(app), "timon") == 303  # Navidrome takes the password again: back
    con = db.connect(app.state.settings.db_path)
    assert auth.get_user(con, "timon") is not None
    con.close()


def test_a_migrated_user_is_found_by_name(app) -> None:
    """A user from before Navidrome's ids were kept (version 17) is not taken for gone: found by its name."""
    con = db.connect(app.state.settings.db_path)
    with con:
        con.execute("INSERT INTO users (name, created, admin) VALUES ('david', 'x', 1)")
    user = auth.get_user(con, "david")
    auth.create_session(con, user, 1)
    accounts = [{"id": "nd-42", "userName": "David", "isAdmin": False}, {"id": "nd-owner", "userName": "owner"}]
    changes = auth.sync_users(con, accounts)
    assert changes == ["owner: Navidrome admin no", "david is David in Navidrome now"]
    row = con.execute("SELECT navidrome_id, disabled, admin FROM users WHERE name = 'David'").fetchone()
    assert tuple(row) == ("nd-42", 0, 1) and con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    con.close()


def test_a_name_freed_in_navidrome(app, login: Callable[..., TestClient]) -> None:
    """An account deleted in Navidrome and a new one with its name: the new one is another user."""
    login(app, "anna", admin=False)
    con = db.connect(app.state.settings.db_path)
    auth.sync_users(con, [{"id": "nd-other", "userName": "other", "isAdmin": True}])  # anna is gone
    user = auth.logged_in(con, "anna", "nd-anna-2", False)
    names = sorted(u["name"] for u in auth.users(con))
    con.close()
    assert user.name == "anna" and names[0] == "anna" and names[1].startswith("anna (gone ")


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
    html = client.post("/account/tokens", data={"name": "grafana"}).text
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
    assert "grafana" in client.get("/account").text and token not in client.get("/account").text
    client.post(f"/account/tokens/{tid}/revoke")
    assert bearer.get("/api/stats").status_code == 401
    timon = login(app, "timon", admin=False)  # a token may do what its user may
    html = timon.post("/account/tokens", data={"name": "mine"}).text
    weak = html.split('id="new-token" type="text" value="')[1].split('"')[0]
    assert TestClient(app, headers={"Authorization": f"Bearer {weak}"}).get("/api/stats").status_code == 403


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


def test_navidrome_settings(app, login: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch) -> None:
    """An address that does not answer is not taken (nobody could log in), nor none without a fallback;
    the service password is stored in the vault, never shown."""
    from echolot.services import navidrome

    client = login(app)
    monkeypatch.setattr(navidrome, "reachable", lambda url: False)
    r = client.post("/settings/access", data={"session_days": 30, "navidrome_url": "http://wrong:4533"})
    assert "does not answer" in r.text
    monkeypatch.delenv("ECHOLOT_NAVIDROME_URL")
    r = client.post("/settings/access", data={"session_days": 30, "navidrome_url": ""})
    assert "nobody could log in" in r.text
    monkeypatch.setattr(navidrome, "reachable", lambda url: True)
    monkeypatch.setattr(navidrome.Service, "users", lambda self: [{"id": "nd-tester", "userName": "tester"}])
    data = {"session_days": 30, "navidrome_url": "http://nd:4533", "service_user": "admin", "service_password": "s3"}
    r = client.post("/settings/access", data=data)
    assert "the service account works" in r.text and "s3" not in r.text
    con = db.connect(app.state.settings.db_path)
    assert app.state.vault.get(con, navidrome.SERVICE_PASSWORD) == "s3"
    assert options.get(con, options.Navidrome).url == "http://nd:4533"
    con.close()


def test_navidrome_login_asks_navidrome(monkeypatch: pytest.MonkeyPatch) -> None:
    """The login is Navidrome's own: POST /auth/login with the name and password (JSON); 401 is a wrong one.
    The service account logs in the same way and sends the token with its API calls."""
    monkeypatch.undo()  # the real login, not the tests' fake
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from echolot.services import navidrome

    class Navidrome(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            ok = self.path == "/auth/login" and body["password"] == "secret"
            self.send_response(200 if ok else 401)
            self.end_headers()
            admin = body["username"] == "admin"
            answer = {"id": "u1", "username": body["username"].lower(), "isAdmin": admin, "token": "jwt"}
            self.wfile.write(json.dumps(answer if ok else {"error": "Invalid"}).encode())

        def do_GET(self) -> None:
            if self.path == "/api/user" and self.headers.get("X-ND-Authorization") == "Bearer jwt":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps([{"id": "u1", "userName": "timon", "isAdmin": False}]).encode())
                return
            self.send_response(200 if self.path == "/ping" else 401)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Navidrome)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        assert navidrome.login(url, "Timon", "secret") == ("timon", False, "u1")
        assert navidrome.login(url, "Timon", "wrong") is None
        assert navidrome.reachable(url)
        assert navidrome.Service(url, "admin", "secret").users()[0]["userName"] == "timon"
        with pytest.raises(navidrome.NavidromeError, match="no Navidrome admin"):
            navidrome.Service(url, "timon", "secret").users()
    finally:
        server.shutdown()
    with pytest.raises(navidrome.NavidromeError):
        navidrome.login(url, "Timon", "secret")
    assert not navidrome.reachable(url)


def test_cli_users(settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """The way back in: there is no password here to reset, but a user can be made an admin."""
    from echolot.cli import main

    monkeypatch.setenv("ECHOLOT_DATA_DIR", str(settings.data_dir))
    con = db.connect(settings.db_path)
    auth.logged_in(con, "bob", "nd-bob", False)
    con.close()
    assert main(["user", "admin", "bob"]) == 0
    assert main(["user", "permit", "bob", "review", "run"]) == 0
    main(["user", "list"])
    assert "bob\tadmin" in capsys.readouterr().out
    assert main(["user", "admin", "bob", "--off"]) == 0
    con = db.connect(settings.db_path)
    bob = auth.get_user(con, "bob")
    con.close()
    assert bob and not bob.admin and bob.permissions == {"review", "run"}
    with pytest.raises(SystemExit, match="No user"):
        main(["user", "admin", "nobody"])
