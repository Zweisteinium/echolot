"""When the pipeline's jobs run: schedule.yml in the pipeline directory, read every minute by its
scripts/tick.py. Per job either minutes between runs, fixed local times ({at: ["20:30",
"sat,sun 15:00"]}) or off. Echolot writes the whole file (from the template below) and shows when
each job last started and runs next (state/last-<job>, else the last start line in its log)."""

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from echolot import pipeline
from echolot.sources import ConfigError, write_atomic

FILE = "schedule.yml"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

Rule = int | list[str] | None  # minutes, ["20:30", "sat,sun 15:00"], or off


@dataclass(frozen=True)
class JobInfo:
    name: str
    label: str
    default: Rule
    minimum: int  # minutes between runs
    help: str


JOBS = [
    JobInfo("sync", "Spotify → Soulseek", 30, 10,
            "Spotify lists -> Soulseek (VPN): new songs; missing ones retried after 3 h, 6 h, 12 h, then daily"),
    JobInfo("sweep", "Missing songs sweep", ["20:00", "sat,sun 15:00"], 360,
            "every missing Spotify song searched again, at the hours most users are online"),
    JobInfo("upgrade", "FLAC upgrade", ["14:00", "20:30"], 360,
            "FLAC-only search for songs that are not genuine lossless (each song: 12 h, 1 d, 2 d, then every 3 d)"),
    JobInfo("playlists", "Playlist files", 10, 5, "rebuild the playlist files"),
    JobInfo("soundcloud", "SoundCloud", 30, 15,
            "SoundCloud likes and sets (home IP); SoundCloud rate-limits bursts"),
    JobInfo("fallback", "YouTube fallback", 120, 60,
            "YouTube/SoundCloud search for songs Soulseek failed twice (home IP)"),
]  # fmt: skip
BY_NAME = {j.name: j for j in JOBS}

HEADER = """\
# When the music-sync jobs run. scripts/tick.py reads this every minute; Echolot's settings page edits it.
# Per job: minutes between runs, or {at: [...]} with local times ("20:30", "sat,sun 15:00"), or off.
# Soulseek jobs share one lock: a due job waits for the one running, it does not lose its turn.
"""

TIME = re.compile(r"(?:([a-z,\s]+?)\s+)?(\d{1,2}):(\d\d)")


def parse_time(entry: str) -> tuple[set[int] | None, int, int]:
    """'sat,sun 15:00' -> ({5, 6}, 15, 0); ConfigError if invalid (same rules as tick.py)."""
    m = TIME.fullmatch(entry.strip().lower())
    if not m or int(m.group(2)) > 23 or int(m.group(3)) > 59:
        raise ConfigError(f"'{entry}' is not a time like 20:30 or sat,sun 15:00.")
    days = None
    if m.group(1):
        names = [d.strip() for d in m.group(1).split(",") if d.strip()]
        if not names or any(d not in DAYS for d in names):
            raise ConfigError(f"'{entry}': days are mon, tue, wed, thu, fri, sat, sun.")
        days = {DAYS.index(d) for d in names}
    return days, int(m.group(2)), int(m.group(3))


def normalise(entry: str) -> str:
    days, h, m = parse_time(entry)
    prefix = ",".join(DAYS[d] for d in sorted(days)) + " " if days else ""
    return f"{prefix}{h:02d}:{m:02d}"


def parse_when(text: str, job: JobInfo) -> Rule:
    """Settings form value: '30' (minutes), '20:00; sat,sun 15:00' (times) or 'off' / '' / '0'."""
    text = text.strip().lower()
    if text in ("", "off", "0"):
        return None
    if text.isdigit():
        if int(text) < job.minimum:
            raise ConfigError(f"{job.label}: at least {job.minimum} minutes.")
        return int(text)
    return [normalise(t) for t in text.split(";") if t.strip()]


def when_text(rule: Rule) -> str:
    """The form value for a rule."""
    if rule is None:
        return "off"
    return str(rule) if isinstance(rule, int) else "; ".join(rule)


def _rule(value: object, job: JobInfo) -> Rule:
    if value is False or value == 0 or value is None:
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return max(int(value), job.minimum)
    if isinstance(value, dict) and isinstance(value.get("at"), list):
        try:
            return [normalise(str(t)) for t in value["at"]] or job.default
        except ConfigError:
            return job.default
    return job.default


def read(root: Path) -> dict[str, Rule]:
    """The rule per job, with the same fallbacks as tick.py."""
    try:
        conf = yaml.safe_load((root / FILE).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        conf = {}
    if not isinstance(conf, dict):
        conf = {}
    return {j.name: _rule(conf.get(j.name, j.default), j) for j in JOBS}


def render(values: dict[str, Rule]) -> str:
    lines = [HEADER.rstrip("\n")]
    for j in JOBS:
        v = values.get(j.name)
        if isinstance(v, list):
            lines.append(f"{j.name + ':':<20}# {j.help}")
            lines.append("  at: [" + ", ".join(f'"{t}"' for t in v) + "]")
        else:
            value = "off" if v is None else str(v)
            lines.append(f"{j.name + ': ' + value:<20}# {j.help}")
    return "\n".join(lines) + "\n"


def save(con: sqlite3.Connection, root: Path, values: dict[str, Rule]) -> None:
    for j in JOBS:
        v = values.get(j.name)
        if isinstance(v, int) and v < j.minimum:
            raise ConfigError(f"{j.label}: at least {j.minimum} minutes.")
        if isinstance(v, list):
            for t in v:
                parse_time(t)
    path = root / FILE
    text = render(values)
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if text == current:
        return
    with con:
        con.execute(
            "INSERT INTO config_versions (name, ts, text, note) VALUES (?, ?, ?, ?)",
            (FILE, datetime.now().isoformat(timespec="seconds"), current, "schedule changed"),
        )
    write_atomic(path, text)


def next_time(times: list[str], after: datetime) -> datetime | None:
    """First scheduled moment after `after`."""
    parsed = [parse_time(t) for t in times]
    for back in range(8):
        day = (after + timedelta(days=back)).replace(second=0, microsecond=0)
        hits = [
            day.replace(hour=h, minute=m)
            for days, h, m in parsed
            if days is None or day.weekday() in days
        ]
        hits = [t for t in hits if t > after]
        if hits:
            return min(hits)
    return None


@dataclass
class Status:
    job: JobInfo
    rule: Rule
    started: str | None  # ISO local time of the last start
    running: bool
    next_run: str | None  # ISO local time

    @property
    def when(self) -> str:
        return when_text(self.rule)


def status(root: Path, now: datetime | None = None) -> list[Status]:
    now = now or datetime.now()
    rules = read(root)
    activity = {a.job: a for a in pipeline.activity(root)}
    out = []
    for j in JOBS:
        stamp = last_start(root, j.name)
        a = activity.get(j.name)
        rule = rules[j.name]
        nxt = None
        if isinstance(rule, int) and stamp:
            nxt = datetime.fromisoformat(stamp) + timedelta(minutes=rule)
        elif isinstance(rule, list):
            nxt = next_time(rule, now)
        out.append(
            Status(
                j,
                rule,
                a.started if a and a.started else stamp,
                bool(a and a.running),
                nxt.isoformat(timespec="seconds") if nxt else None,
            )
        )
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
