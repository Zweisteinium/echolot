"""Settings, read from ECHOLOT_* environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path  # database, state and logs; the only place Echolot writes to for now
    library_dir: Path | None  # the music library, read-only until Echolot files songs itself
    host: str
    port: int

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        library = env.get("ECHOLOT_LIBRARY_DIR")
        return cls(
            data_dir=Path(env.get("ECHOLOT_DATA_DIR", "data")),
            library_dir=Path(library) if library else None,
            host=env.get("ECHOLOT_HOST", "127.0.0.1"),
            port=int(env.get("ECHOLOT_PORT", "8490")),
        )
