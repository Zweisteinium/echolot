"""When the jobs run. Per job either minutes between starts, fixed local times ("20:30",
"sat,sun 15:00") or off; stored as the settings section 'schedule' (defaults below).

A job is due when its interval has passed since its last start, or when a fixed time has passed since
it; a fixed-time run that could not start within LATE (Soulseek busy, Echolot down) is dropped. Jobs that
share a resource wait for each other and do not lose their turn.
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from echolot.settings import options
from echolot.settings.sources import ConfigError

SECTION = "schedule"  # settings section: {job: minutes | ["HH:MM", ...] | null}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
LATE = timedelta(hours=6)

Rule = int | list[str] | None  # minutes, ["20:30", "sat,sun 15:00"], or off


@dataclass(frozen=True)
class JobInfo:
    name: str
    label: str
    resource: str  # jobs sharing one run one at a time: soulseek, web (home IP), local
    default: Rule
    minimum: int  # minutes between runs
    help: str


JOBS = [
    JobInfo("sync", "Spotify → Soulseek", "soulseek", 30, 10,
            "read the Spotify lists, search new songs; missing ones again after 3 h, 6 h, 12 h, then daily"),
    JobInfo("sweep", "Missing songs sweep", "soulseek", ["20:00", "sat,sun 15:00"], 360,
            "search every missing Spotify song again, at the hours most users are online"),
    JobInfo("upgrade", "FLAC upgrade", "soulseek", ["14:00", "20:30"], 360,
            "FLAC-only search for songs that are not genuine lossless (each: 12 h, 1 d, 2 d, then every 3 d)"),
    JobInfo("soundcloud", "SoundCloud", "web", 30, 15,
            "read the SoundCloud lists and download new songs (SoundCloud rate-limits bursts)"),
    JobInfo("fallback", "YouTube fallback", "web", 120, 60,
            "search YouTube and SoundCloud for songs Soulseek did not find twice"),
    JobInfo("library", "Library", "local", 5, 1,
            "rescan the library, apply review decisions, write the playlists"),
]  # fmt: skip
BY_NAME = {j.name: j for j in JOBS}

TIME = re.compile(r"(?:([a-z,\s]+?)\s+)?(\d{1,2}):(\d\d)")


def parse_time(entry: str) -> tuple[set[int] | None, int, int]:
    """'sat,sun 15:00' -> ({5, 6}, 15, 0); ConfigError if invalid."""
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
    """A stored rule, with the defaults for anything invalid."""
    if value is False or value == 0 or value is None:
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return max(int(value), job.minimum)
    times = value.get("at") if isinstance(value, dict) else value
    if isinstance(times, list):
        try:
            return [normalise(str(t)) for t in times] or job.default
        except ConfigError:
            return job.default
    return job.default


def rules(con: sqlite3.Connection) -> dict[str, Rule]:
    """The rule per job (the default for a job never set)."""
    stored = options.raw(con, SECTION)
    stored = stored if isinstance(stored, dict) else {}
    return {j.name: _rule(stored[j.name], j) if j.name in stored else j.default for j in JOBS}


def parse_rules(data: object) -> dict[str, Rule]:
    """Rules in the file format (echolot.yml: minutes, {at: [...]} or off), strictly checked."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("schedule must be a mapping of job: minutes, {at: [...]} or off.")
    out: dict[str, Rule] = {}
    for name, value in data.items():
        job = BY_NAME.get(name)
        if job is None:
            raise ConfigError(f"schedule: unknown job '{name}' ({', '.join(BY_NAME)}).")
        if value is False or value is None or value == "off":
            out[name] = None
        elif isinstance(value, int) and not isinstance(value, bool):
            out[name] = parse_when(str(value), job)
        elif isinstance(value, dict) and set(value) == {"at"} and isinstance(value["at"], list):
            out[name] = [normalise(str(t)) for t in value["at"]] or None
        else:
            raise ConfigError(f"schedule.{name}: minutes, {{at: [...]}} or off.")
    return out


def file_form(values: dict[str, Rule]) -> dict[str, object]:
    """Rules as echolot.yml writes them: minutes, {at: [...]} or off."""
    return {
        name: {"at": rule} if isinstance(rule, list) else ("off" if rule is None else rule)
        for name, rule in values.items()
    }


def store(con: sqlite3.Connection, values: dict[str, Rule]) -> None:
    """Check and store rules (jobs not in `values` keep theirs); no commit."""
    for j in JOBS:
        v = values.get(j.name)
        if isinstance(v, int) and v < j.minimum:
            raise ConfigError(f"{j.label}: at least {j.minimum} minutes.")
        if isinstance(v, list):
            for t in v:
                parse_time(t)
    new = {**rules(con), **{k: v for k, v in values.items() if k in BY_NAME}}
    options.put_raw(con, SECTION, new)


def save(con: sqlite3.Connection, values: dict[str, Rule]) -> None:
    with con:
        store(con, values)


def last_point(times: list[str], now: datetime) -> datetime | None:
    """The most recent scheduled moment at or before now."""
    parsed = [parse_time(t) for t in times]
    for back in range(8):
        day = (now - timedelta(days=back)).replace(second=0, microsecond=0)
        hits = [day.replace(hour=h, minute=m) for days, h, m in parsed if days is None or day.weekday() in days]
        hits = [t for t in hits if t <= now]
        if hits:
            return max(hits)
    return None


def next_time(times: list[str], after: datetime) -> datetime | None:
    """The first scheduled moment after `after`."""
    parsed = [parse_time(t) for t in times]
    for ahead in range(8):
        day = (after + timedelta(days=ahead)).replace(second=0, microsecond=0)
        hits = [day.replace(hour=h, minute=m) for days, h, m in parsed if days is None or day.weekday() in days]
        hits = [t for t in hits if t > after]
        if hits:
            return min(hits)
    return None


def due(rule: Rule, last: datetime | None, now: datetime) -> bool:
    if rule is None:
        return False
    if isinstance(rule, int):
        return last is None or now - last >= timedelta(minutes=rule)
    point = last_point(rule, now)
    return point is not None and (last is None or last < point) and now - point <= LATE


def next_run(rule: Rule, last: datetime | None, now: datetime) -> datetime | None:
    if rule is None:
        return None
    if isinstance(rule, int):
        return (last + timedelta(minutes=rule)) if last else now
    return next_time(rule, now)
