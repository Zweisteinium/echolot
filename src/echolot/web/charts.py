"""The overview's charts, server-rendered SVG: the quality ring and the library's growth (a few lines of
JavaScript only show the value under the pointer)."""

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class Slice:
    key: str
    label: str
    count: int
    share: float  # of the total, 0..1
    dash: str  # stroke-dasharray on a circle with pathLength 100
    offset: float  # stroke-dashoffset


@dataclass
class Donut:
    size: int
    radius: float
    width: float
    total: int
    slices: list[Slice]


def donut(counts: list[tuple[str, str, int]], size: int = 184, width: int = 26) -> Donut:
    """Part-to-whole ring of (key, label, count), clockwise from 12 o'clock. A 2px surface gap
    separates slices; a non-zero slice is at least 8px long (6px drawn), so a small share stays visible."""
    radius = (size - width) / 2
    total = sum(n for _, _, n in counts)
    circumference = 2 * math.pi * radius
    gap, least = 200 / circumference, 800 / circumference  # in units of pathLength 100
    shown = [(k, label, n) for k, label, n in counts if n]
    lengths = [max(100 * n / total, least) for _, _, n in shown] if total else []
    scale = 100 / sum(lengths) if lengths else 1
    slices, start = [], 0.0
    for (k, label, n), length in zip(shown, lengths, strict=True):
        length *= scale
        drawn = max(length - gap, 0.5) if len(shown) > 1 else 100
        slices.append(Slice(k, label, n, n / total, f"{drawn:.3f} {100 - drawn:.3f}", -start))
        start += length
    return Donut(size, radius, width, total, slices)


W, H = 1000, 300  # the growth chart's drawing box; it stretches to its frame (labels are HTML)
GAP = timedelta(hours=36)  # longer without a snapshot (Echolot off): the line breaks


@dataclass
class Tick:
    pos: float  # percent: from the top (values), from the left (dates)
    label: str


@dataclass
class Growth:
    line: str  # SVG path in the W x H box
    rebuilt: str  # the part rebuilt from the events (dashed)
    area: str
    values: list[Tick]
    dates: list[Tick]
    end: tuple[float, float]  # the last point, percent from the left and the top
    points: str  # JSON [[left %, top %, when, value], ...] for the pointer
    now: str  # the last value, formatted
    since: str  # the first point's date
    total: str  # growth since the first point, formatted with its sign
    week: str  # ... in the last 7 days


def _nice(top: float) -> tuple[float, float]:
    """(axis maximum, step): the lowest maximum of 3 to 5 steps of 1, 2, 2.5 or 5 x 10^n reaching `top`
    (more steps when two reach as low)."""
    best = None
    for n in (5, 4, 3):
        raw = max(top, 3) / n  # (whole steps for counts)
        unit = 10 ** math.floor(math.log10(raw))
        step = next(m * unit for m in (1, 2, 2.5, 5, 10) if m * unit >= raw)
        if best is None or n * step < best[0]:
            best = (n * step, step)
    return best


def _signed(change: float, value: Callable[[float], str]) -> str:
    return f"{'+' if change >= 0 else '−'}{value(abs(change))}"


def growth(
    points: list[tuple[str, float]],
    value: Callable[[float], str],
    axis: Callable[[float], str],
    rebuilt_until: str = "",
    most: int = 240,
) -> Growth | None:
    """A metric over time ((UTC ts, value), oldest first) as a line over an area from 0; at most `most`
    points (the last of each stretch of time); `value` formats a value, `axis` an axis label. Points before
    `rebuilt_until` were rebuilt from the events. None for fewer than two points."""
    if len(points) < 2:
        return None
    when = [datetime.fromisoformat(ts).astimezone() for ts, _ in points]  # local time
    t0, t1 = when[0], when[-1]
    span = (t1 - t0).total_seconds() or 1
    picked: dict[int, int] = {}
    for i, t in enumerate(when):  # the last point of each of `most` stretches
        picked[min(int((t - t0).total_seconds() / span * most), most - 1)] = i
    keep = sorted({0, *picked.values()})
    top, step = _nice(max(v for _, v in points) * 1.08)

    def xy(i: int) -> tuple[float, float]:
        return W * (when[i] - t0).total_seconds() / span, H - H * points[i][1] / top

    def path(idx: list[int]) -> str:
        out, prev = [], None
        for i in idx:
            x, y = xy(i)
            out.append(f"{'M' if prev is None or when[i] - when[prev] > GAP else 'L'}{x:.1f},{y:.1f}")
            prev = i
        return "".join(out)

    old = [i for i in keep if rebuilt_until and points[i][0] < rebuilt_until]
    real = [i for i in keep if i not in old]
    first, last = xy(keep[0]), xy(keep[-1])
    week_ago = next((points[i][1] for i in reversed(keep) if when[i] <= t1 - timedelta(days=7)), points[0][1])
    hour = span <= 2 * 86400
    dates = [t0 + timedelta(seconds=span * k / 4) for k in range(5)]
    return Growth(
        line=path(old[-1:] + real),
        rebuilt=path(old + real[:1]),
        area=f"{path(keep)}L{last[0]:.1f},{H}L{first[0]:.1f},{H}Z",
        values=[Tick(100 - 100 * k * step / top, axis(k * step)) for k in range(round(top / step) + 1)],
        dates=[Tick(25 * k, f"{d:%H:%M}" if hour else f"{d.day} {d:%b}") for k, d in enumerate(dates)],
        end=(100 * last[0] / W, 100 * last[1] / H),
        points=json.dumps(
            [
                [
                    round(100 * xy(i)[0] / W, 2),
                    round(100 * xy(i)[1] / H, 2),
                    f"{when[i]:%a} {when[i].day} {when[i]:%b, %H:%M}",
                    value(points[i][1]) + (" (rebuilt)" if i in old else ""),
                ]
                for i in keep
            ]
        ),
        now=value(points[-1][1]),
        since=f"{t0.day} {t0:%b}",
        total=_signed(points[-1][1] - points[0][1], value),
        week=_signed(points[-1][1] - week_ago, value),
    )
