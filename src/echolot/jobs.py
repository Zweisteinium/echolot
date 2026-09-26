"""The scheduled jobs."""

from sqlite3 import Connection

from echolot import db, library, pipeline
from echolot.config import Settings
from echolot.scheduler import Job

REFRESH_MINUTES = 5  # default; changed on the settings page


def refresh(settings: Settings, con: Connection) -> str:
    """Import the pipeline's state, rescan the library, match songs to files."""
    parts = []
    if settings.pipeline_dir:
        parts.append(pipeline.import_state(con, settings.pipeline_dir))
    if settings.library_dir:
        known = pipeline.known_files(settings.pipeline_dir) if settings.pipeline_dir else None
        parts.append(library.scan(con, settings.library_dir, known))
    parts.append(library.match_songs(con))
    return "; ".join(parts)


def refresh_minutes(con: Connection) -> int:
    return int(db.get_meta(con, "refresh_minutes", str(REFRESH_MINUTES)))


def all_jobs(settings: Settings, minutes: int = REFRESH_MINUTES) -> list[Job]:
    return [Job("refresh", minutes * 60, lambda con: refresh(settings, con))]
