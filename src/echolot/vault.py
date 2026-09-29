"""Secrets (credentials for Spotify, SoundCloud, ...) in the database, encrypted with Fernet.

The key comes from ECHOLOT_SECRET_KEY, else from the file ECHOLOT_SECRET_KEY_FILE, else from
<data dir>/secret.key, which is created on first use. With the key in the environment, a copy of the
database alone reveals no secret. Values are never logged or shown; the UI only tells whether a secret
is set and when it changed.
"""

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

# the secrets Echolot knows, with what they are for (used from PLAN.md phase 3 on)
KNOWN = {
    "spotify.client_secret": "Spotify app: client secret",
    "spotify.refresh_token": "Spotify: refresh token of the connected account",
    "soundcloud.token": "SoundCloud: OAuth token of the account",
}


class VaultError(RuntimeError):
    """The key is missing or wrong (message for the user)."""


class Vault:
    def __init__(self, key: bytes, source: str) -> None:
        try:
            self._fernet = Fernet(key)
        except ValueError as err:
            raise VaultError(
                f"The secret key from {source} is not a Fernet key (32 bytes, URL-safe base64)."
            ) from err
        self.source = source  # where the key came from, for the settings page

    @classmethod
    def from_env(cls, data_dir: Path, env: dict[str, str] | None = None) -> "Vault":
        env = os.environ if env is None else env
        if key := env.get("ECHOLOT_SECRET_KEY", "").strip():
            return cls(key.encode(), "ECHOLOT_SECRET_KEY")
        path = Path(env.get("ECHOLOT_SECRET_KEY_FILE") or data_dir / "secret.key")
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        return cls(path.read_bytes().strip(), str(path))

    def set(self, con: sqlite3.Connection, name: str, value: str) -> None:
        if not value:
            raise ValueError("empty secret")
        con.execute(
            "INSERT INTO secrets (name, value, updated) VALUES (?, ?, ?) ON CONFLICT (name) "
            "DO UPDATE SET value = excluded.value, updated = excluded.updated",
            (
                name,
                self._fernet.encrypt(value.encode()),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )

    def get(self, con: sqlite3.Connection, name: str) -> str | None:
        row = con.execute("SELECT value FROM secrets WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        try:
            return self._fernet.decrypt(row[0]).decode()
        except InvalidToken as err:
            raise VaultError(
                f"Secret '{name}' can't be decrypted: it was stored with another key than {self.source}."
            ) from err

    def has(self, con: sqlite3.Connection, name: str) -> bool:
        """Whether the secret is stored (without decrypting it)."""
        return con.execute("SELECT 1 FROM secrets WHERE name = ?", (name,)).fetchone() is not None

    def delete(self, con: sqlite3.Connection, name: str) -> bool:
        return con.execute("DELETE FROM secrets WHERE name = ?", (name,)).rowcount > 0


def listing(con: sqlite3.Connection) -> list[dict[str, str | None]]:
    """Known and stored secrets with when they changed (never the values)."""
    stored = {r[0]: r[1] for r in con.execute("SELECT name, updated FROM secrets")}
    names = list(KNOWN) + sorted(n for n in stored if n not in KNOWN)
    return [{"name": n, "help": KNOWN.get(n, ""), "updated": stored.get(n)} for n in names]
