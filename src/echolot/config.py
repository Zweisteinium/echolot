"""The deployment, from ECHOLOT_* environment variables; everything else is configured in the web interface."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path  # database, secret key file, private working files
    library_dir: Path | None  # <music>/tracks; <music>/inbox and <music>/playlists beside it
    host: str
    port: int
    daemon_dir: Path | None = None  # the Sockseek daemon's login file (daemon.conf) goes here
    worker: bool = True  # ECHOLOT_WORKER=off: no jobs (a development copy)

    def __post_init__(self) -> None:
        # inbox/ and playlists/ are taken from beside tracks/: another name would split pages and jobs
        if self.library_dir is not None and self.library_dir.name != "tracks":
            raise ValueError(f"ECHOLOT_LIBRARY_DIR must be <music>/tracks, not {self.library_dir}")

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
