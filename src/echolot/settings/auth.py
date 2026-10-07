"""Users, browser sessions and API tokens.

The users are Navidrome's accounts: Navidrome checks the password at each login (web/access), Echolot keeps
no password, only the account and what it may do here. An admin may do everything, for everyone, except
make someone an admin or no admin any more: only a Navidrome admin (always an admin here) can. Everyone else
manages their own accounts and lists, and may be given PERMISSIONS for their own songs. Navidrome's user list (sync_users)
renews who is a Navidrome admin and disables accounts gone from Navidrome.

A session is a random cookie value; the database keeps its SHA-256 only, with a CSRF token that every form
and htmx request of the session sends back. API tokens (Authorization: Bearer <token>) are for scripts;
they are stored as SHA-256 as well and shown once.
"""

import hashlib
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

COOKIE = "echolot_session"
PERMISSIONS = {  # what an admin may give a user who is no admin, always for the user's own songs and lists
    "review": "Review: decide the downloads of their songs",
    "upload": "Upload: add files they got elsewhere for their songs",
    "run": "Run Jobs: check their lists and search their missing songs now",
}
VIEWS = ("mine", "everyone")
USER_COLUMNS = "u.id, u.name, u.admin, u.navidrome_admin, u.permissions, u.view"


class AuthError(ValueError):
    """Message for the user."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ---------------------------------------------------------------- users


@dataclass(frozen=True)
class User:
    id: int
    name: str
    admin: bool = False  # everything, for everyone (an Echolot admin, or a Navidrome admin)
    permissions: frozenset[str] = field(default_factory=frozenset)
    view: str = "mine"  # an admin's pages: their own lists, or everyone's
    navidrome_admin: bool = False  # also gives and takes admin rights

    def can(self, permission: str) -> bool:
        return self.admin or permission in self.permissions

    @property
    def everyone(self) -> bool:
        """The pages show everyone's lists and songs (an admin who chose so)."""
        return self.admin and self.view == "everyone"


def _user(row: sqlite3.Row) -> User:
    perms = frozenset(p for p in (row["permissions"] or "").split(",") if p in PERMISSIONS)
    view = row["view"] if row["view"] in VIEWS else "mine"
    nd_admin = bool(row["navidrome_admin"])
    return User(row["id"], row["name"], bool(row["admin"]) or nd_admin, perms, view, nd_admin)


def get_user(con: sqlite3.Connection, name: str) -> User | None:
    sql = f"SELECT {USER_COLUMNS} FROM users u WHERE u.name = ? AND NOT u.disabled"
    row = con.execute(sql, (name.strip(),)).fetchone()
    return _user(row) if row else None


def users(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM users ORDER BY name").fetchall()


def logged_in(con: sqlite3.Connection, name: str, navidrome_id: str, navidrome_admin: bool) -> User:
    """The Navidrome account that just logged in: added (an admin only if Navidrome's), or renewed (its
    name as Navidrome spells it now, its Navidrome admin flag); one that was gone from Navidrome is back."""
    known = con.execute(
        "SELECT id FROM users WHERE navidrome_id = ? OR (navidrome_id IS NULL AND name = ?)", (navidrome_id, name)
    ).fetchone()
    with con:
        # the name of an account gone from Navidrome, now another Navidrome account's: the old one gets out of the way
        con.execute(
            "UPDATE users SET name = name || ' (gone ' || id || ')' WHERE name = ? AND disabled AND id != ?",
            (name, known["id"] if known else -1),
        )
        if known:
            con.execute(
                "UPDATE users SET name = ?, navidrome_id = ?, navidrome_admin = ?, disabled = 0 WHERE id = ?",
                (name, navidrome_id, int(navidrome_admin), known["id"]),
            )
        else:
            # every right explicit: a database migrated from version 17 has admin DEFAULT 1
            con.execute(
                "INSERT INTO users (name, created, navidrome_id, navidrome_admin, admin, permissions, view, disabled) "
                "VALUES (?, ?, ?, ?, 0, '', 'mine', 0)",
                (name, _now(), navidrome_id, int(navidrome_admin)),
            )
    user = get_user(con, name)
    assert user is not None
    return user


def set_rights(con: sqlite3.Connection, user_id: int, admin: bool, permissions: set[str]) -> None:
    unknown = permissions - set(PERMISSIONS)
    if unknown:
        raise AuthError(f"Unknown permission: {', '.join(sorted(unknown))}.")
    with con:
        con.execute(
            "UPDATE users SET admin = ?, permissions = ? WHERE id = ?",
            (int(admin), ",".join(sorted(permissions)), user_id),
        )


def set_view(con: sqlite3.Connection, user: User, view: str) -> None:
    if view not in VIEWS:
        raise AuthError("The view is mine or everyone.")
    with con:
        con.execute("UPDATE users SET view = ? WHERE id = ?", (view, user.id))


def end_sessions(con: sqlite3.Connection, user_id: int) -> int:
    """Log a user out everywhere (API tokens stay)."""
    with con:
        return con.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,)).rowcount


def sync_users(con: sqlite3.Connection, accounts: list[dict]) -> list[str]:
    """Navidrome's user list (id, userName, isAdmin): who is a Navidrome admin now, names that changed;
    an account gone from Navidrome is disabled, its sessions and API tokens end (its lists stay). An
    empty list changes nothing (Navidrome always has an admin). Returns what changed, for the log."""
    if not accounts:
        return []
    by_id = {a["id"]: a for a in accounts if a.get("id")}
    by_name = {(a.get("userName") or "").casefold(): a for a in accounts}
    changes = []
    with con:
        for u in con.execute("SELECT * FROM users").fetchall():
            # a user from before Navidrome's ids were kept (migrated): found by name, its id kept from now on
            a = by_id.get(u["navidrome_id"]) if u["navidrome_id"] else by_name.get(u["name"].casefold())
            if a is not None and not u["navidrome_id"]:
                con.execute("UPDATE users SET navidrome_id = ? WHERE id = ?", (a["id"], u["id"]))
            if a is None:
                if not u["disabled"]:
                    con.execute("UPDATE users SET disabled = 1 WHERE id = ?", (u["id"],))
                    con.execute("DELETE FROM sessions WHERE user_id = ?", (u["id"],))
                    con.execute("DELETE FROM api_tokens WHERE user_id = ?", (u["id"],))
                    changes.append(f"{u['name']} is gone from Navidrome: disabled")
                continue
            admin, name = int(bool(a.get("isAdmin"))), a.get("userName") or u["name"]
            if (admin, name, 0) == (u["navidrome_admin"], u["name"], u["disabled"]):
                continue
            taken = con.execute("SELECT 1 FROM users WHERE name = ? AND id != ?", (name, u["id"])).fetchone()
            if taken:  # renamed in Navidrome to the name of another user here: keep the old name for now
                name = u["name"]
            sql = "UPDATE users SET navidrome_admin = ?, name = ?, disabled = 0 WHERE id = ?"
            con.execute(sql, (admin, name, u["id"]))
            if admin != u["navidrome_admin"]:
                changes.append(f"{name}: Navidrome admin {'yes' if admin else 'no'}")
            if name != u["name"]:
                changes.append(f"{u['name']} is {name} in Navidrome now")
    return changes


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
        f"SELECT s.id AS sid, s.csrf, s.expires, s.last_seen, {USER_COLUMNS} FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.id = ? AND NOT u.disabled",
        (_sha(token),),
    ).fetchone()
    now = datetime.now()
    if row is None or row["expires"] < now.isoformat(timespec="seconds"):
        return None
    if row["last_seen"] < (now - timedelta(minutes=5)).isoformat(timespec="seconds"):
        with con:
            con.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (_now(), row["sid"]))
    return Session(_user(row), row["csrf"])


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
        f"SELECT t.id AS tid, t.last_used, {USER_COLUMNS} FROM api_tokens t "
        "JOIN users u ON u.id = t.user_id WHERE t.token = ? AND NOT u.disabled",
        (_sha(token),),
    ).fetchone()
    if row is None:
        return None
    if not row["last_used"] or row["last_used"][:16] != _now()[:16]:  # at most once a minute
        with con:
            con.execute("UPDATE api_tokens SET last_used = ? WHERE id = ?", (_now(), row["tid"]))
    return _user(row)


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
