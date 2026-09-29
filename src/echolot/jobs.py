"""The scheduled jobs."""

from sqlite3 import Connection

from echolot import db, history, library, options, pipeline, pipeline_config, sources
from echolot.config import Settings
from echolot.scheduler import Job


def refresh(settings: Settings, con: Connection) -> str:
    """Import the pipeline's state, rescan the library, match songs to files."""
    parts = []
    if settings.pipeline_dir and db.get_meta(con, pipeline_config.TAKEN_OVER):
        parts.append(pipeline.import_state(con, settings.pipeline_dir, sources.lists(con)))
    elif settings.pipeline_dir:  # the lists are not known yet: keep what was imported before
        parts.append("pipeline state not imported (its config files are not taken over)")
    if settings.library_dir:
        known = pipeline.known_files(settings.pipeline_dir) if settings.pipeline_dir else None
        parts.append(library.scan(con, settings.library_dir, known))
    parts.append(library.match_songs(con))
    if history.snapshot(con):
        parts.append("snapshot stored")
    return "; ".join(parts)


def refresh_minutes(con: Connection) -> int:
    return options.get(con, options.General).refresh_minutes


def all_jobs(settings: Settings, minutes: int) -> list[Job]:
    return [Job("refresh", minutes * 60, lambda con: refresh(settings, con))]
