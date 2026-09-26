"""Settings, read from ECHOLOT_* environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path  # database; the only place Echolot writes to for now
    library_dir: Path | None  # the music library, read-only until Echolot files songs itself
    pipeline_dir: Path | None  # the music-sync pipeline's config directory, read-only
    host: str
    port: int

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
            pipeline_dir=path("ECHOLOT_PIPELINE_DIR"),
            host=env.get("ECHOLOT_HOST", "127.0.0.1"),
            port=int(env.get("ECHOLOT_PORT", "8490")),
        )
