"""How numbers, sizes, times and schedules read on the pages (the templates' filters)."""

from datetime import datetime


def num(n: int | None) -> str:
    return f"{n or 0:,}"


def size(b: int | None) -> str:
    b = b or 0
    return f"{b / 1e9:.1f} GB" if b >= 1e9 else f"{b / 1e6:.0f} MB"


def pct(part: int, whole: int) -> int:
    """Whole percent, rounded down: 100 only when complete."""
    return 100 * part // whole if whole else 0


def _span(s: float) -> str:
    if s < 3600:
        return f"{max(s, 60) // 60:.0f} min"
    if s < 86400:
        return f"{s / 3600:.0f} h" if s >= 36000 else f"{s / 3600:.1f} h".replace(".0 h", " h")
    return f"{s / 86400:.0f} d" if s >= 864000 else f"{s / 86400:.1f} d".replace(".0 d", " d")


def _parse(value: str | int) -> datetime:
    return datetime.fromtimestamp(value) if isinstance(value, int) else datetime.fromisoformat(value)


def ago(value: str | int | None) -> str:
    """'5 min ago' for an ISO timestamp (local time) or unix time."""
    if not value:
        return "never"
    s = (datetime.now() - _parse(value)).total_seconds()
    return "just now" if s < 60 else f"{_span(s)} ago"


def until(value: str | int | None) -> str:
    """'in 5 min' for a future ISO timestamp or unix time."""
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
SOURCES |= {"soundcloud-search": "SoundCloud", "manual": "By hand"}  # the search fallback's downloads; added by hand


def source(name: str | None) -> str:
    """Where a song or download comes from, by its proper name (one spelling on every page)."""
    return SOURCES.get(name or "", (name or "").capitalize())


FILTERS = {"num": num, "size": size, "ago": ago, "until": until, "mmss": mmss, "minutes": minutes, "source": source}
