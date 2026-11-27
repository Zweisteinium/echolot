"""When the jobs run: per job minutes between starts, fixed local times ("20:30", "sat,sun 15:00") or off,
stored as the settings section 'schedule'.

A job is due when its interval has passed since its last start, or a fixed time has passed since it; a
fixed-time run that could not start within LATE (Soulseek busy, Echolot down) is dropped. Resources and
priorities: worker.py.
"""

import re
import sqlite3
from dataclasses import dataclass, field
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
    resource: str  # jobs sharing one run one at a time (web: the home IP)
    default: Rule
    minimum: int  # minutes between runs
    help: str  # one line, under its name
    details: str  # how it works, behind the info button
    priority: int = 0  # a lower one gives way to a higher one of its resource (worker.py)
    started_by: str = ""  # the job that starts it (no schedule of its own)
    beside: bool = False  # starts beside a lower one; makes it give way only from Run.claim on


JOBS = [
    JobInfo("sync", "Spotify lists", "spotify", 3, 1,
            "Checks your Spotify lists. New songs start the search.",
            "Asks Spotify what changed and reads only those lists, a few requests a run. New songs start New songs "
            "search.", priority=3),
    JobInfo("search_new", "New songs search", "soulseek", None, 1,
            "Searches the new songs of Spotify and YouTube lists.",
            "Looks in your library first: the same recording is linked, not downloaded. Then searches Soulseek, FLAC "
            "first, and checks each download's length, tags and audio. Doubtful ones wait in Review. Songs Soulseek "
            "lacks go to the YouTube and SoundCloud search.", priority=3, started_by="Spotify and YouTube lists"),
    JobInfo("soundcloud", "New SoundCloud songs", "web", 5, 2,
            "Checks your SoundCloud lists and downloads new songs.",
            "Asks SoundCloud what changed and reads only those lists, each at least hourly. Downloads the uploader's "
            "own file where allowed, else the stream. A recording the library has is linked, not kept twice.",
            priority=3, beside=True),
    JobInfo("youtube", "YouTube lists", "web", 30, 10,
            "Reads your YouTube playlists. New songs start the search.",
            "YouTube reports no changes, so each playlist is read in full. New songs get Spotify's names, ISRC and "
            "length where Spotify has them, and are searched like Spotify songs. Videos that no longer play stay in "
            "the list and show under Availability.", priority=3, beside=True),
    JobInfo("fallback", "YouTube & SoundCloud search", "web", 120, 60,
            "Searches YouTube and SoundCloud for songs Soulseek lacks.",
            "Tries the song's own video, the release's audio on YouTube, a YouTube search, then SoundCloud. The "
            "first result that passes the checks is filed. One of another length waits in Review. Each song at most "
            "once a week, or a day later when a site failed. The result is lossy, so FLAC upgrade keeps looking.", priority=2),
    JobInfo("sweep", "Missing songs", "soulseek", ["20:00", "sat,sun 15:00"], 360,
            "Searches Soulseek again for songs found nowhere.",
            "Runs at Soulseek's busiest hours. Each song daily, weekly after 7 misses. After 2 misses the search "
            "terms get looser, after 4 looser still.", priority=2),
    JobInfo("upgrade", "FLAC upgrade", "soulseek", ["14:00", "20:30"], 360,
            "Looks for genuine FLACs of your lossy songs.",
            "Searches Soulseek for FLACs only, for songs whose file is lossy or a fake FLAC. Each song 12 h, 1 day "
            "and 2 days after its last search, then every 3 days. A genuine FLAC replaces the file. One that sounds "
            "unlike your copy waits in Review.", priority=1),
    JobInfo("upgrade_all", "FLAC upgrade, all songs", "soulseek", None, 360,
            "Every lossy song at once. Start it with All songs.",
            "Searches a FLAC for every lossy song, however recently searched. Makes way for other Soulseek jobs and "
            "goes on after them. Off on the schedule."),
    JobInfo("covers", "Covers from your lists", "pictures", None, 60,
            "Gives files their song's cover. Start it with Add missing.",
            "New files get their cover when they are filed. This does the rest: on Spotify the cover of the release "
            "on your lists or the original one (Server settings, Files), the SoundCloud artwork, or the YouTube "
            "video's picture. Old pictures and tags are kept in cover-backups. Off on the schedule."),
    JobInfo("covers_all", "Covers, all files", "pictures", None, 60,
            "Gives every file its song's cover again. Start it with Replace all.",
            "Like Add missing, for every file. One request per song, so it takes hours. Off on the schedule."),
    JobInfo("covers_release", "Covers after the switch", "pictures", None, 60,
            "Gives the files the cover of the release the Covers switch chose.",
            "Only the files whose cover or album name the switch changes, about one request per song. Old pictures "
            "and tags are kept in cover-backups.", started_by="the Covers switch in Files"),
    JobInfo("availability", "Availability check", "catalog", ["05:30"], 360,
            "Checks whether your lists' songs still play.",
            "Asks Spotify and SoundCloud about every song, 50 at a time: does it play, as another release, or not at "
            "all. What changed shows under Availability. Songs Spotify stopped playing are searched first.", priority=1),
    JobInfo("library", "Library upkeep", "local", 5, 1,
            "Rescans the library, applies Review decisions, writes the playlists.",
            "Reads new and changed files, applies decisions once their undo time is over, matches songs to files and "
            "writes one playlist per list. Hearts liked songs in Navidrome. Deletes replaced files after 30 days."),
    JobInfo("lossy", "Lossy check", "audio", None, 30,
            "Checks your FLACs for a lossy source. Start it with Run now.",
            "Runs the two checks every download gets: the spectrum and lossprint. A few seconds per file, at low "
            "priority. A fake FLAC counts as lossy, so FLAC upgrade looks for a better one. Off on the schedule."),
    JobInfo("lyrics", "Lyrics", "pictures", 30, 10,
            "Adds lyrics from LRCLIB, synced where it has them.",
            "Keeps only lyrics that surely are the song's: same artist, title and version, as long as the file. They "
            "are embedded in the file, so Navidrome and other players show them. A file's own lyrics stay. Songs "
            "without lyrics are asked again after a month."),
    # not on the Jobs page, so never scheduled (rules)
    JobInfo("bandcamp", "Bandcamp offers", "web", None, 60,
            "Finds Bandcamp offers of your lossy SoundCloud songs.",
            "Reads the Bandcamp page a SoundCloud song links to. Its price goes to the Buy page, a free FLAC is "
            "filed with the usual checks. One page every 2 s.", started_by="New SoundCloud songs"),
    JobInfo("hypeddit", "Free downloads", "web", None, 60,
            "Gets the Hypeddit free downloads of your lossy SoundCloud songs.",
            "Downloads each file directly, without clicking through its page, one every 2 s, and files it like any "
            "download. Downloads that ask for an email use the address in Settings.", started_by="the Buy page"),
    # not on the Jobs page either
    JobInfo("discogs", "Discogs check", "web", None, 60,
            "Finds the releases of a list that Discogs lacks.",
            "Looks up each release on Discogs, about one a second, and on Beatport for what to submit. Nothing is "
            "submitted.", started_by="Check on the Discogs page"),
]  # fmt: skip
BY_NAME = {j.name: j for j in JOBS}
STEPS = {  # each job's short name inside its task
    "sync": "Spotify",
    "search_new": "Soulseek search",
    "soundcloud": "SoundCloud",
    "youtube": "YouTube",
    "sweep": "Soulseek",
    "fallback": "YouTube & SoundCloud",
    "upgrade": "Scheduled",
    "upgrade_all": "All songs",
    "library": "",
    "covers": "Add missing",
    "covers_all": "Replace all",
    "covers_release": "After the switch",
    "availability": "",
    "lossy": "",
    "lyrics": "",
}


@dataclass(frozen=True)
class Task:
    """A group of the jobs page: its jobs (steps), buttons (label, jobs started, hover text) and `about`,
    its info card. Maintenance tasks are listed under their own heading."""

    name: str
    label: str
    help: str
    jobs: tuple[str, ...]
    buttons: tuple[tuple[str, tuple[str, ...], str], ...]
    maintenance: bool = False
    about: str = ""
    confirm: dict[str, str] = field(default_factory=dict)  # button label -> question first


TASKS = [
    Task(
        "new",
        "New songs",
        "Checks your lists and gets their new songs.",
        ("sync", "soundcloud", "youtube", "search_new"),  # tile order
        (("Check now", ("sync", "youtube", "soundcloud"), "Check the Spotify, YouTube and SoundCloud lists now"),),
        about="Spotify and SoundCloud are asked every few minutes, YouTube less often. A song the library has is linked. Others come from Soulseek as FLAC where possible, else from YouTube or SoundCloud. Doubtful downloads wait in Review.",
    ),
    Task(
        "missing",
        "Missing songs",
        "Searches songs found nowhere again.",
        ("fallback", "sweep"),
        (("Search all now", ("sweep", "fallback"), "Search every missing song now"),),
        about="Soulseek at its busiest hours, YouTube and SoundCloud every few hours. Each song daily at first, then weekly, with looser terms the longer it is missing.",
    ),
    Task(
        "upgrade",
        "FLAC upgrade",
        "Looks for genuine FLACs of your lossy songs.",
        ("upgrade", "upgrade_all"),
        (
            ("Run now", ("upgrade",), "Search the next batch now"),
            ("All songs", ("upgrade_all",), "Search every lossy song now"),
        ),
        about="Lossy songs are searched on Soulseek a few times a day, soon after they came and then less often. A FLAC found replaces your copy.",
    ),
    Task(
        "library",
        "Library upkeep",
        "Rescans the library, applies Review decisions, writes the playlists.",
        ("library",),
        (("Run now", ("library",), "Rescan the library now"),),
        maintenance=True,
        about="Keeps Echolot in step with the music folder, applies Review decisions and writes a playlist for every list.",
    ),
    Task(
        "availability",
        "Availability check",
        "Checks whether your lists' songs still play.",
        ("availability",),
        (("Run now", ("availability",), "Check every song now"),),
        maintenance=True,
        about="Once a day every song is checked at its source. What changed shows under Availability.",
    ),
    Task(
        "lossy",
        "Lossy check",
        "Checks your FLACs for a lossy source.",
        ("lossy",),
        (("Run now", ("lossy",), "Check the FLACs not checked yet"),),
        maintenance=True,
        about="Listens to every FLAC for signs of a lossy source. A fake FLAC counts as lossy and gets a FLAC upgrade.",
    ),
    Task(
        "lyrics",
        "Lyrics",
        "Adds lyrics from LRCLIB, synced where it has them.",
        ("lyrics",),
        (("Run now", ("lyrics",), "Look for lyrics now"),),
        maintenance=True,
        about="Embeds each song's lyrics when they surely are the song's, so Navidrome and other players show them. Synced lyrics scroll along. A file's own lyrics stay.",
    ),
    Task(
        "covers",
        "Covers from your lists",
        "Gives every file its song's cover.",
        ("covers", "covers_all", "covers_release"),
        (
            ("Add missing", ("covers",), "Files not done yet"),
            ("Replace all", ("covers_all",), "Every file again. Takes hours."),
        ),
        maintenance=True,
        about="The cover of the release on your lists or the original one on Spotify (Server settings, Files), the SoundCloud artwork, or the YouTube video's picture, in place of the uploader's. The old pictures are kept.",
        confirm={
            "Replace all": "Check the cover of every file again? This takes hours. Covers that are right already stay."
        },
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
    """The rule per job (the default where none is stored); a job of no task is never scheduled."""
    stored = options.raw(con, SECTION)
    stored = stored if isinstance(stored, dict) else {}
    out = {j.name: _rule(stored[j.name], j) if j.name in stored else j.default for j in JOBS}
    return {name: rule if name in TASK_OF else None for name, rule in out.items()}


def scheduled(values: dict[str, Rule]) -> dict[str, Rule]:
    """The rules of the jobs of a task: what the settings and echolot.yml show."""
    return {name: rule for name, rule in values.items() if name in TASK_OF}


def parse_rules(data: object) -> dict[str, Rule]:
    """Rules in the file format (echolot.yml: minutes, {at: [...]} or off), strictly checked."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("schedule must be a mapping of job: minutes, {at: [...]} or off.")
    out: dict[str, Rule] = {}
    for name, value in data.items():
        job = BY_NAME.get(name) if name in TASK_OF else None
        if job is None:
            raise ConfigError(f"schedule: unknown job '{name}' ({', '.join(TASK_OF)}).")
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
    new = {**rules(con), **{k: v for k, v in values.items() if k in TASK_OF}}
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
