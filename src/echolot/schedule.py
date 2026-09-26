"""How often the pipeline's jobs run: schedule.yml in the pipeline directory, read every minute by
its scripts/tick.py. Echolot writes the whole file (from the template below) and shows when each
job last started and runs next (state/last-<job>, else the last start line in its log)."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from echolot import pipeline
from echolot.sources import ConfigError, write_atomic

FILE = "schedule.yml"


@dataclass(frozen=True)
class JobInfo:
    name: str
    label: str
    default: int  # minutes
    minimum: int
    help: str


JOBS = [
    JobInfo("sync", "Spotify → Soulseek", 30, 10, "Spotify lists -> Soulseek (VPN)."),
    JobInfo(
        "soundcloud",
        "SoundCloud",
        30,
        15,
        "SoundCloud likes and sets (home IP). SoundCloud rate-limits bursts.",
    ),
    JobInfo(
        "fallback",
        "YouTube fallback",
        120,
        60,
        "YouTube/SoundCloud search for songs Soulseek failed twice.",
    ),
    JobInfo("playlists", "Playlist files", 10, 5, "Rebuild the playlist files."),
    JobInfo(
        "upgrade",
        "FLAC upgrade",
        10080,
        1440,
        "FLAC-only search for songs that are not genuine lossless.",
    ),
]
BY_NAME = {j.name: j for j in JOBS}

HEADER = """\
# How often the music-sync jobs run, in minutes (off = never). scripts/tick.py reads this every
# minute; Echolot's settings page edits it. A job whose previous run is still busy skips its turn.
"""


def read(root: Path) -> dict[str, int | None]:
    """Minutes per job (None = off), with the same fallbacks as tick.py."""
    try:
        conf = yaml.safe_load((root / FILE).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        conf = {}
    out: dict[str, int | None] = {}
    for j in JOBS:
        v = conf.get(j.name, j.default) if isinstance(conf, dict) else j.default
        if v is False or v == 0:
            out[j.name] = None
        elif isinstance(v, int | float) and not isinstance(v, bool):
            out[j.name] = max(int(v), j.minimum)
        else:
            out[j.name] = j.default
    return out


def render(values: dict[str, int | None]) -> str:
    lines = [HEADER.rstrip("\n")]
    for j in JOBS:
        v = values.get(j.name)
        value = "off" if v is None else str(v)
        lines.append(f"{j.name + ':':<12}{value:<8}# {j.help} Minimum {j.minimum}.")
    return "\n".join(lines) + "\n"


def save(con: sqlite3.Connection, root: Path, values: dict[str, int | None]) -> None:
    for j in JOBS:
        v = values.get(j.name)
        if v is not None and v < j.minimum:
            raise ConfigError(f"{j.label}: at least {j.minimum} minutes.")
    path = root / FILE
    text = render(values)
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if text == current:
        return
    with con:
        con.execute(
            "INSERT INTO config_versions (name, ts, text, note) VALUES (?, ?, ?, ?)",
            (FILE, datetime.now().isoformat(timespec="seconds"), current, "intervals changed"),
        )
    write_atomic(path, text)


@dataclass
class Status:
    job: JobInfo
    minutes: int | None
    started: str | None  # ISO local time of the last start
    running: bool
    next_run: str | None  # ISO local time


def status(root: Path) -> list[Status]:
    values = read(root)
    activity = {a.job: a for a in pipeline.activity(root)}
    out = []
    for j in JOBS:
        stamp = last_start(root, j.name)
        a = activity.get(j.name)
        started = a.started if a and a.started else stamp
        running = bool(a and a.running)
        minutes = values[j.name]
        nxt = None
        if minutes is not None and stamp:
            nxt = datetime.fromtimestamp(
                datetime.fromisoformat(stamp).timestamp() + minutes * 60
            ).isoformat(timespec="seconds")
        out.append(Status(j, minutes, started, running, nxt))
    return out


def last_start(root: Path, job: str) -> str | None:
    """Last start the pipeline scheduled (state/last-<job>), else the one in its log."""
    try:
        ts = float((root / "state" / f"last-{job}").read_text().strip())
        return datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    except (OSError, ValueError):
        pass
    a = next((a for a in pipeline.activity(root) if a.job == job), None)
    return a.started if a else None
