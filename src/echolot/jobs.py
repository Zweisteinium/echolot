"""The scheduled jobs."""

from sqlite3 import Connection

from echolot import library, pipeline
from echolot.config import Settings
from echolot.scheduler import Job


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


def all_jobs(settings: Settings) -> list[Job]:
    return [Job("refresh", 5 * 60, lambda con: refresh(settings, con))]
