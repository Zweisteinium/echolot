"""Echolot's settings, stored in the database by section (table settings, one JSON value each).

Every section is a pydantic model with defaults: a section never saved reads as its defaults, and a
value that no longer validates (an older version wrote it) falls back to them field by field.
The pipeline's schedule is a section too (schedule.py); the lists are in the sources table.
"""

import json
import sqlite3
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")
    SECTION: ClassVar[str]


class SourceOptions(Section):
    SECTION = "sources"
    soundcloud_user: str = Field("", description="whose SoundCloud likes 'likes' means")
    removed_playlists: bool = Field(
        True, description="songs that leave a list go to '<list> – removed'"
    )


class Metrics(Section):
    SECTION = "metrics"
    public: bool = Field(True, description="/metrics answers without login (for Prometheus)")


class Auth(Section):
    SECTION = "auth"
    session_days: int = Field(30, ge=1, le=365, description="how long a login lasts")


class Jobs(Section):
    SECTION = "jobs"
    paused: bool = Field(False, description="no job starts (running ones finish)")


class Spotify(Section):
    SECTION = "spotify"
    client_id: str = Field("", description="of your Spotify developer app")
    redirect_uri: str = Field("", description="the one registered in the app and used to connect")


class Soulseek(Section):
    SECTION = "soulseek"
    url: str = Field("http://gluetun:5031", description="the Sockseek daemon's API")
    user: str = Field("", description="the Soulseek account the daemon logs in with")
    daemon_music: str = Field("/music", description="the music directory as the daemon sees it")
    parallel: int = Field(4, ge=1, le=8, description="songs searched and downloaded at a time")
    upgrade_batch: int = Field(150, ge=1, le=2000, description="FLAC searches per upgrade run")
    stall_minutes: int = Field(
        10, ge=2, le=120, description="a download without progress this long is given up"
    )


SECTIONS: list[type[Section]] = [SourceOptions, Metrics, Auth, Jobs, Spotify, Soulseek]
BY_NAME = {s.SECTION: s for s in SECTIONS}


class OptionsError(ValueError):
    """Invalid settings (message for the user)."""


def raw(con: sqlite3.Connection, section: str) -> Any:
    row = con.execute("SELECT value FROM settings WHERE section = ?", (section,)).fetchone()
    return json.loads(row[0]) if row else None


def put_raw(con: sqlite3.Connection, section: str, value: Any) -> None:
    con.execute(
        "INSERT INTO settings (section, value, updated) VALUES (?, ?, ?) ON CONFLICT (section) "
        "DO UPDATE SET value = excluded.value, updated = excluded.updated",
        (section, json.dumps(value, sort_keys=True), datetime.now().isoformat(timespec="seconds")),
    )


def get[S: Section](con: sqlite3.Connection, model: type[S]) -> S:
    stored = raw(con, model.SECTION)
    if not isinstance(stored, dict):
        return model()
    try:
        return model.model_validate(stored)
    except ValidationError:  # keep what is still valid
        good = {}
        for name, value in stored.items():
            try:
                model.model_validate({name: value})
                good[name] = value
            except ValidationError:
                pass
        return model.model_validate(good)


def validate[S: Section](model: type[S], value: Any) -> S:
    """A section from user input, or OptionsError with every problem."""
    try:
        return model.model_validate(value if value is not None else {})
    except ValidationError as err:
        problems = [
            f"{model.SECTION}.{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in err.errors()
        ]
        raise OptionsError(" ".join(problems)) from err


def put(con: sqlite3.Connection, value: Section) -> None:
    put_raw(con, value.SECTION, value.model_dump())


def update[S: Section](con: sqlite3.Connection, model: type[S], **changes: Any) -> S:
    """Change some fields of a section (validated), keep the others."""
    new = validate(model, {**get(con, model).model_dump(), **changes})
    put(con, new)
    return new
