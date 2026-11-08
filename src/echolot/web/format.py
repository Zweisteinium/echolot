"""Template filters: numbers, sizes, times, schedules, prices, job lines."""

import re
from dataclasses import dataclass
from datetime import datetime


def num(n: int | None) -> str:
    return f"{n or 0:,}"


def size(b: int | None) -> str:
    b = b or 0
    return f"{b / 1e12:.2f} TB" if b >= 1e12 else f"{b / 1e9:.1f} GB" if b >= 1e9 else f"{b / 1e6:.0f} MB"


def pct(part: int, whole: int) -> int:
    """Whole percent, rounded down (100 only when complete)."""
    return 100 * part // whole if whole else 0


def share(part: int, whole: int) -> str:
    """One decimal, rounded down (100.0 only when complete); '< 0.1' for a tiny non-zero share."""
    if not whole:
        return "0.0"
    tenths = 1000 * part // whole
    return "< 0.1" if part and not tenths else f"{tenths / 10:.1f}"


def _span(s: float) -> str:
    if s < 3600:
        return f"{max(s, 60) // 60:.0f} min"
    if s < 86400:
        return f"{s / 3600:.0f} h" if s >= 36000 else f"{s / 3600:.1f} h".replace(".0 h", " h")
    return f"{s / 86400:.0f} d" if s >= 864000 else f"{s / 86400:.1f} d".replace(".0 d", " d")


def _parse(value: str | int) -> datetime:
    return datetime.fromtimestamp(value) if isinstance(value, int) else datetime.fromisoformat(value)


def ago(value: str | int | None) -> str:
    """'5 min ago' for a local ISO timestamp or unix time."""
    if not value:
        return "never"
    s = (datetime.now() - _parse(value)).total_seconds()
    return "just now" if s < 60 else f"{_span(s)} ago"


def until(value: str | int | None) -> str:
    if not value:
        return "–"
    s = (_parse(value) - datetime.now()).total_seconds()
    return "due now" if s < 60 else f"in {_span(s)}"


def minutes(rule: int | list[str] | None) -> str:
    """'every 30 min', 'every 2 h', 'at 20:00, sat,sun 15:00' or 'off'."""
    if rule is None:
        return "off"
    if isinstance(rule, list):
        return "at " + ", ".join(rule)
    return f"every {_span(rule * 60)}"


def mmss(seconds: float | None) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}" if seconds else "–"


SOURCES = {"soulseek": "Soulseek", "youtube": "YouTube", "soundcloud": "SoundCloud", "spotify": "Spotify"}
SOURCES |= {"soundcloud-search": "SoundCloud", "manual": "By hand", "mitschnitt": "Mitschnitt"}
SOURCES |= {"bandcamp": "Bandcamp", "dropbox": "Dropbox"}


def source(name: str | None) -> str:
    return SOURCES.get(name or "", (name or "").capitalize())


def span(seconds: float | None) -> str:
    return _span(seconds or 0)


CURRENCIES = {"EUR": "{} €", "USD": "${}", "GBP": "£{}", "JPY": "¥{}"}  # others: "2.00 AUD"


def price(amount: float | None, currency: str = "", free: bool = False) -> str:
    """'free', '1.50 €', '$1.50', '2.00 AUD', or '1.50' while the currency is unknown."""
    if free or amount == 0:
        return "free"
    if amount is None:
        return ""
    return CURRENCIES.get(currency, "{} " + currency).format(f"{amount:.2f}").strip()


FILTERS = {"num": num, "size": size, "ago": ago, "until": until, "mmss": mmss, "minutes": minutes, "source": source}
FILTERS["span"] = span
FILTERS["price"] = price


OUTCOMES = {  # a song outcome in a job's line -> (icon, tone); the word is the hover text
    "new": ("add", "ok"),
    "upgrade": ("arrow-up", "ok"),
    "linked": ("link", "ok"),
    "not found": ("search-off", ""),
    "interrupted": ("pause", "warn"),
    "failed": ("close", "bad"),
    "bad file": ("block", "bad"),
}
_COUNT = re.compile(r"(\d+)(?: of (\d+))?(?: ([a-z][a-z0-9 '’()-]*))?")  # "43 lists", "163 of 200 songs"


@dataclass(frozen=True)
class Fact:
    """A piece of a job's line: a count (n [of a total] label, an outcome with its icon) or plain text."""

    n: int | None = None
    of: int | None = None
    label: str = ""
    icon: str = ""
    tone: str = ""


def _fact(part: str) -> Fact | None:
    if not (m := _COUNT.fullmatch(part)) or not (m[2] or m[3]):
        return None
    icon, tone = OUTCOMES.get(m[3] or "", ("", ""))
    return Fact(int(m[1]), int(m[2]) if m[2] else None, m[3] or "", icon, tone)


def facts(line: str | None, step: str = "") -> list[Fact]:
    """A job's line in pieces: "163 of 200 songs: 4 interrupted, 158 not found" is a count of a total and
    two outcomes. Clauses split at "; ", head and rest at ": "; a part is counts only if every ", " piece is
    one (song names stay text). A leading head equal to the step name is dropped (the tile shows it)."""
    out = []
    for clause in (line or "").split("; "):
        head, colon, rest = clause.partition(": ")
        for part in (head, rest) if colon else (head,):
            pieces = [_fact(p) for p in part.split(", ")]
            out += [f for f in pieces if f] if all(pieces) else [Fact(label=part)]
    if out and step and out[0] == Fact(label=step):
        out = out[1:]
    return [f for f in out if f.n is not None or f.label]


FILTERS["facts"] = facts
