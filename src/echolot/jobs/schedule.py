"""When the jobs run. Per job either minutes between starts, fixed local times ("20:30",
"sat,sun 15:00") or off; stored as the settings section 'schedule' (defaults below).

A job is due when its interval has passed since its last start, or when a fixed time has passed since
it; a fixed-time run that could not start within LATE (Soulseek busy, Echolot down) is dropped. Jobs that
share a resource wait for each other and do not lose their turn; a running one of a lower priority ends
after its songs in progress when a more urgent one is due, and goes on after it.
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
    help: str  # one line, under its name
    details: str  # how it works, behind the info button
    priority: int = 0  # a running job of a lower one ends after its songs in progress when one of a higher
    # one of its resource is due, and goes on with the rest after it (worker.resume)
    started_by: str = ""  # the job that starts it when there is work (no schedule of its own)


JOBS = [
    JobInfo("sync", "Spotify lists", "spotify", 2, 1,
            "Checks your Spotify lists for changes; new songs start New Spotify songs.",
            "Asks Spotify what changed (a few requests: the playlists' snapshots, the state of your likes) and "
            "reads only the lists that changed; which liked songs Spotify greys out is asked once a day. It "
            "needs no Soulseek, so it never waits for a search; only when there are new songs does New Spotify "
            "songs start, and only then do the other Soulseek jobs make way.", priority=3),
    JobInfo("search_new", "New Spotify songs", "soulseek", None, 1,
            "Searches the new songs Spotify lists found, right away.",
            "Started by Spotify lists when there are new songs. A new song is first looked for in your library: a song with the "
            "same recording (ISRC), or a file with its title and length that sounds like the release, is linked "
            "instead of downloaded. Otherwise Soulseek is searched, FLAC preferred: up to five downloads are "
            "tried, each checked by length, tags and audio; a doubtful one waits in Review. A song Soulseek does "
            "not have goes to the YouTube & SoundCloud search right after this job. Comes first: a less urgent "
            "Soulseek job stops after its songs in progress and goes on afterwards.", priority=3,
            started_by="Spotify lists"),
    JobInfo("soundcloud", "New SoundCloud songs", "web", 5, 2,
            "Checks your SoundCloud lists for changes and downloads new songs from SoundCloud itself.",
            "Asks SoundCloud what changed (three requests: your likes, the sets in your library) and reads only "
            "the lists that changed, each at least hourly (a download that failed is tried again). Each new song "
            "is downloaded from SoundCloud: the uploader's own "
            "file where downloads are allowed (sometimes lossless), else the stream. Before it is filed, a "
            "download is compared by audio with the library's files of the same title and length: the same "
            "recording is linked, not kept twice. A song SoundCloud hands out to nobody (label releases) goes to "
            "the YouTube & SoundCloud search. Comes first on the home connection.", priority=3),
    JobInfo("fallback", "YouTube & SoundCloud search", "web", 120, 60,
            "Songs Soulseek does not have, and SoundCloud songs that cannot be downloaded.",
            "Starts right after New Spotify songs when Soulseek had nothing, and on its schedule. Per song: the "
            "release's own audio on YouTube (\"Provided to YouTube\") first, then YouTube, then a SoundCloud "
            "search; the first result that passes the same checks as a Soulseek download is filed, one of "
            "another length waits in Review. A download the library has under other names is linked instead. "
            "Each song at most once a week, new ones first (Run now: every one). The result is lossy: the FLAC "
            "upgrade looks for a lossless copy from 12 h later. Gives way to New SoundCloud songs.", priority=2),
    JobInfo("sweep", "Missing songs", "soulseek", ["20:00", "sat,sun 15:00"], 360,
            "Searches the songs found nowhere yet again, when most Soulseek users are online.",
            "Songs that neither Soulseek nor YouTube or SoundCloud had are searched on Soulseek again, with "
            "looser terms after two misses (title without additions, first artist only, then without the "
            "artist in the path): each song daily, weekly after 7 searches without a find (Run now: every one). "
            "Gives way to New Spotify songs.", priority=2),
    JobInfo("upgrade", "FLAC upgrade", "soulseek", ["14:00", "20:30"], 360,
            "Looks for genuine FLACs of the songs you have lossy.",
            "FLAC-only Soulseek search for songs whose file is not genuine lossless (a FLAC made from an MP3 "
            "counts as lossy): each song 12 h, 1 d and 2 d after the last search, then every 3 days, the longest "
            "waiting first, at most the batch size per run (Run now: whatever their wait). A genuine FLAC "
            "replaces the lossy file under its "
            "name. A FLAC for a SoundCloud song waits in Review. Gives way to New Spotify songs and Missing "
            "songs and goes on after them.", priority=1),
    JobInfo("upgrade_all", "FLAC upgrade, all songs", "soulseek", None, 360,
            "Every song you have lossy at once, whatever its wait. Start it with Run now.",
            "Searches a FLAC for every song that is not genuine lossless, regardless of when it was last "
            "searched, the longest waiting first. Gives way to every other Soulseek job that is due and goes on "
            "after it until all songs are done (a restart of Echolot ends it). Off on the schedule.", priority=0),
    JobInfo("covers", "Covers from your lists", "pictures", None, 60,
            "Gives every file its song's cover from Spotify or SoundCloud. Start it with Run now.",
            "New files get their song's cover when they are filed. This run gives it to the files already "
            "there: the Spotify album's cover, or the SoundCloud song's artwork, in place of the one the "
            "uploader embedded (a compilation, a remaster). The old picture is kept in cover-backups first; "
            "files done are noted, so a run that stopped goes on where it was. Off on the schedule."),
    JobInfo("library", "Library upkeep", "local", 5, 1,
            "Rescans the library, applies review decisions and writes the playlists.",
            "Notices new, changed and removed files (only those are read again), applies review decisions once "
            "their undo time is over, matches the songs to files and writes one playlist per list. Once a day "
            "it empties the replaced and review files older than 30 days."),
]  # fmt: skip
BY_NAME = {j.name: j for j in JOBS}
STEPS = {  # each job's short name inside its task, for the schedule and the live state
    "sync": "Spotify",
    "search_new": "Soulseek search",
    "soundcloud": "SoundCloud",
    "sweep": "Soulseek",
    "fallback": "YouTube & SoundCloud",
    "upgrade": "Scheduled",
    "upgrade_all": "All songs",
    "library": "",
    "covers": "",
}


@dataclass(frozen=True)
class Task:
    """What the jobs page shows: one task, done by one or more jobs (its steps), with its buttons
    (label, the jobs they start, hover text). Maintenance tasks are folded away."""

    name: str
    label: str
    help: str
    jobs: tuple[str, ...]
    buttons: tuple[tuple[str, tuple[str, ...], str], ...]
    maintenance: bool = False


TASKS = [
    Task(
        "new",
        "New songs",
        "Checks your lists; a new song comes from Soulseek, else from YouTube or SoundCloud.",
        ("sync", "search_new", "soundcloud"),
        (("Check now", ("sync", "soundcloud"), "Check the Spotify and SoundCloud lists now"),),
    ),
    Task(
        "missing",
        "Missing songs",
        "Songs found nowhere yet: Soulseek daily, YouTube and SoundCloud weekly.",
        ("sweep", "fallback"),
        (("Search all now", ("sweep", "fallback"), "Search every missing song now, whatever its wait"),),
    ),
    Task(
        "upgrade",
        "FLAC upgrade",
        "Looks for genuine FLACs of the songs you have lossy.",
        ("upgrade", "upgrade_all"),
        (
            ("Run now", ("upgrade",), "The next batch, the longest waiting first, whatever their wait"),
            ("All songs", ("upgrade_all",), "Every lossy song at once; gives way to new songs and goes on after"),
        ),
    ),
    Task(
        "library",
        "Library upkeep",
        "Rescans the library, applies review decisions, writes the playlists.",
        ("library",),
        (("Run now", ("library",), "Rescan the library now"),),
        maintenance=True,
    ),
    Task(
        "covers",
        "Covers from your lists",
        "Gives every file its song's cover from Spotify or SoundCloud.",
        ("covers",),
        (("Run now", ("covers",), "Replace the uploaders' covers with your songs' covers"),),
        maintenance=True,
    ),
]
TASK_OF = {job: t for t in TASKS for job in t.jobs}

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
            raise ConfigError(f"{job.label}: at least {job.minimum} minutes between runs.")
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
            raise ConfigError(f"{j.label}: at least {j.minimum} minutes between runs.")
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
