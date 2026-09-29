"""Users, browser sessions and API tokens.

Passwords are hashed with scrypt (stdlib). A session is a random cookie value; the database keeps its
SHA-256 only, with a CSRF token that every form and htmx request of the session sends back. API tokens
(Authorization: Bearer <token>) are for scripts; they are stored as SHA-256 as well and shown once.
"""

import base64
import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**17, 8, 1  # OWASP minimum; about 128 MB and a fraction of a second
MIN_PASSWORD = 10
COOKIE = "echolot_session"


class AuthError(ValueError):
    """Message for the user."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            maxmem=256 * 1024 * 1024, dklen=32)  # fmt: skip
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r),
                             p=int(p), maxmem=256 * 1024 * 1024, dklen=32)  # fmt: skip
    except ValueError:
        return False
    return hmac.compare_digest(got, base64.b64decode(digest))


def check_new_password(password: str, repeat: str | None = None) -> None:
    if repeat is not None and password != repeat:
        raise AuthError("The two passwords differ.")
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"The password needs at least {MIN_PASSWORD} characters.")


# ---------------------------------------------------------------- users


@dataclass(frozen=True)
class User:
    id: int
    name: str


def has_users(con: sqlite3.Connection) -> bool:
    return con.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def add_user(con: sqlite3.Connection, name: str, password: str) -> User:
    name = name.strip()
    if not name or len(name) > 64:
        raise AuthError("A user name is 1 to 64 characters.")
    check_new_password(password)
    try:
        with con:
            cur = con.execute(
                "INSERT INTO users (name, password, created) VALUES (?, ?, ?)", (name, hash_password(password), _now())
            )
    except sqlite3.IntegrityError as err:
        raise AuthError(f"There is a user '{name}' already.") from err
    return User(int(cur.lastrowid or 0), name)


def get_user(con: sqlite3.Connection, name: str) -> User | None:
    row = con.execute("SELECT id, name FROM users WHERE name = ?", (name.strip(),)).fetchone()
    return User(row[0], row[1]) if row else None


def users(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute("SELECT id, name, created, last_login FROM users ORDER BY id").fetchall()


def set_password(con: sqlite3.Connection, user: User, password: str) -> None:
    """New password; every session of the user ends (API tokens stay)."""
    check_new_password(password)
    with con:
        con.execute("UPDATE users SET password = ? WHERE id = ?", (hash_password(password), user.id))
        con.execute("DELETE FROM sessions WHERE user_id = ?", (user.id,))


def verify(con: sqlite3.Connection, name: str, password: str) -> User | None:
    row = con.execute("SELECT id, name, password FROM users WHERE name = ?", (name.strip(),)).fetchone()
    if row is None:
        hash_password(password)  # same time as a wrong password: no hint which names exist
        return None
    return User(row[0], row[1]) if check_password(password, row[2]) else None


# ---------------------------------------------------------------- sessions


@dataclass(frozen=True)
class Session:
    user: User
    csrf: str


def create_session(con: sqlite3.Connection, user: User, days: int) -> str:
    """A new login; returns the cookie value."""
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    with con:
        con.execute("DELETE FROM sessions WHERE expires < ?", (now.isoformat(timespec="seconds"),))
        con.execute(
            "INSERT INTO sessions (id, user_id, csrf, created, expires, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
            (_sha(token), user.id, secrets.token_urlsafe(24), _now(),
             (now + timedelta(days=days)).isoformat(timespec="seconds"), _now()),
        )  # fmt: skip
        con.execute("UPDATE users SET last_login = ? WHERE id = ?", (_now(), user.id))
    return token


def session(con: sqlite3.Connection, token: str) -> Session | None:
    if not token:
        return None
    row = con.execute(
        "SELECT s.id, s.csrf, s.expires, s.last_seen, u.id AS uid, u.name FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.id = ?",
        (_sha(token),),
    ).fetchone()
    now = datetime.now()
    if row is None or row["expires"] < now.isoformat(timespec="seconds"):
        return None
    if row["last_seen"] < (now - timedelta(minutes=5)).isoformat(timespec="seconds"):
        with con:
            con.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (_now(), row["id"]))
    return Session(User(row["uid"], row["name"]), row["csrf"])


def end_session(con: sqlite3.Connection, token: str) -> None:
    with con:
        con.execute("DELETE FROM sessions WHERE id = ?", (_sha(token),))


def end_other_sessions(con: sqlite3.Connection, user: User, keep: str) -> int:
    with con:
        return con.execute("DELETE FROM sessions WHERE user_id = ? AND id != ?", (user.id, _sha(keep))).rowcount


# ---------------------------------------------------------------- API tokens


def create_token(con: sqlite3.Connection, user: User, name: str) -> str:
    """A new API token; the value is returned once and stored as its hash."""
    name = name.strip()
    if not name or len(name) > 64:
        raise AuthError("Name the token (1 to 64 characters), e.g. after the script that uses it.")
    token = "echolot_" + secrets.token_urlsafe(32)
    with con:
        con.execute(
            "INSERT INTO api_tokens (name, token, user_id, created) VALUES (?, ?, ?, ?)",
            (name, _sha(token), user.id, _now()),
        )
    return token


def token_user(con: sqlite3.Connection, token: str) -> User | None:
    row = con.execute(
        "SELECT t.id, t.last_used, u.id AS uid, u.name FROM api_tokens t "
        "JOIN users u ON u.id = t.user_id WHERE t.token = ?",
        (_sha(token),),
    ).fetchone()
    if row is None:
        return None
    if not row["last_used"] or row["last_used"][:16] != _now()[:16]:  # at most once a minute
        with con:
            con.execute("UPDATE api_tokens SET last_used = ? WHERE id = ?", (_now(), row["id"]))
    return User(row["uid"], row["name"])


def tokens(con: sqlite3.Connection, user: User) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT id, name, created, last_used FROM api_tokens WHERE user_id = ? ORDER BY id", (user.id,)
    ).fetchall()


def revoke_token(con: sqlite3.Connection, user: User, token_id: int) -> bool:
    with con:
        return con.execute("DELETE FROM api_tokens WHERE id = ? AND user_id = ?", (token_id, user.id)).rowcount > 0


# ---------------------------------------------------------------- login attempts


class Throttle:
    """Failed logins per client address: after `limit` within `window` seconds, wait."""

    def __init__(self, limit: int = 5, window: float = 600) -> None:
        self.limit, self.window = limit, window
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _recent(self, client: str) -> deque[float]:
        q = self._failures[client]
        while q and q[0] < time.monotonic() - self.window:
            q.popleft()
        return q

    def wait(self, client: str) -> float:
        """Seconds until the next attempt is allowed (0: now)."""
        with self._lock:
            q = self._recent(client)
            return q[0] + self.window - time.monotonic() if len(q) >= self.limit else 0.0

    def failed(self, client: str) -> None:
        with self._lock:
            self._recent(client).append(time.monotonic())

    def passed(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)
