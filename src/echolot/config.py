"""How Echolot is deployed, from ECHOLOT_* environment variables (everything else is configured in the
web interface and stored in the database)."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path  # database, secret key file, private working files
    library_dir: Path | None  # <music>/tracks; <music>/inbox and <music>/playlists beside it
    host: str
    port: int
    # where the Sockseek daemon's login file is written (daemon.conf)
    daemon_dir: Path | None = None
    worker: bool = True  # run the jobs (a development copy runs none: ECHOLOT_WORKER=off)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "echolot.db"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env

        def path(name: str) -> Path | None:
            return Path(env[name]) if env.get(name) else None

        return cls(
            data_dir=Path(env.get("ECHOLOT_DATA_DIR", "data")),
            library_dir=path("ECHOLOT_LIBRARY_DIR"),
            host=env.get("ECHOLOT_HOST", "127.0.0.1"),
            port=int(env.get("ECHOLOT_PORT", "8490")),
            daemon_dir=path("ECHOLOT_DAEMON_DIR"),
            worker=env.get("ECHOLOT_WORKER", "on").lower() not in ("off", "0", "false", "no"),
        )
