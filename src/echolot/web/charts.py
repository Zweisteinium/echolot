"""The Library page's charts as server-rendered SVG: the quality rings and the graphs over time."""

import itertools
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
    """Part-to-whole ring of (key, label, count), clockwise from 12 o'clock. 2px gaps between slices come off
    the ring first, so lengths stay proportional; a slice under 2.5px is drawn 2.5px (the others shrink),
    so a tiny share stays visible and a larger count never draws shorter."""
    radius = (size - width) / 2
    total = sum(n for _, _, n in counts)
    circumference = 2 * math.pi * radius
    gap, least = 200 / circumference, 250 / circumference  # in units of pathLength 100
    shown = [(k, label, n) for k, label, n in counts if n]
    room = 100 - gap * len(shown) if len(shown) > 1 else 100
    lengths = [room * n / total for _, _, n in shown]
    small = [length < least for length in lengths]
    if any(small) and not all(small):
        rest = room - least * sum(small)
        scale = rest / sum(length for length, tiny in zip(lengths, small, strict=True) if not tiny)
        lengths = [least if tiny else length * scale for length, tiny in zip(lengths, small, strict=True)]
    slices, start = [], 0.0
    for (k, label, n), length in zip(shown, lengths, strict=True):
        slices.append(Slice(k, label, n, n / total, f"{length:.3f} {100 - length:.3f}", -start))
        start += length + (gap if len(shown) > 1 else 0)
    return Donut(size, radius, width, total, slices)


W, H = 1000, 300  # a graph's viewBox; it stretches to its frame (labels are HTML)
GAP = timedelta(hours=36)  # longer without a snapshot (Echolot off): the line breaks
MOST = 240  # points drawn at most: the last of each slot


@dataclass
class Tick:
    pos: float  # percent: from the top (values), from the left (dates)
    label: str


@dataclass
class Graph:
    values: list[Tick]
    dates: list[Tick]
    start: str  # first and last point (UTC): the stretch shown, for zooming
    stop: str
    end: tuple[float, float]  # the last point, percent from the left and the top
    points: str  # JSON [[left %, top %, when, value, ...], ...] for the pointer
    now: str  # the last value, formatted
    since: str  # the first point's date
    total: str  # the signed change since the first point
    week: str  # ... in the last 7 days ("" when less is shown)


@dataclass
class Growth(Graph):
    line: str  # SVG path in the W x H box
    area: str  # under each stretch of the line, not under a gap
    gaps: str  # dotted lines across the gaps
    also: str = ""  # a second metric's line (growth's `also`)
    also_area: str = ""  # under it
    also_end: tuple[float, float] | None = None  # its last point (% of the box)


@dataclass
class Band:
    key: str
    label: str
    area: str  # SVG path between the tiers below and its own top
    edge: str  # its top as a line


@dataclass
class Stack(Graph):
    bands: list[Band]  # bottom (best) first


def _nice(top: float) -> tuple[float, float]:
    """(axis maximum, step): the lowest maximum of 3 to 5 steps of 1, 2, 2.5 or 5 x 10^n reaching `top`
    (more steps on a tie)."""
    best = None
    for n in (5, 4, 3):
        raw = max(top, 3) / n  # whole steps for counts
        unit = 10 ** math.floor(math.log10(raw))
        step = next(m * unit for m in (1, 2, 2.5, 5, 10) if m * unit >= raw)
        if best is None or n * step < best[0]:
            best = (n * step, step)
    return best


def _span(low: float, high: float) -> tuple[float, float, float]:
    """(axis minimum, maximum, step) for values from `low` to `high`: from 0 while low is within the spread
    of 0; else cut to round steps just beyond both, so a change of a few songs shows in a large library."""
    if low <= high - low:
        top, step = _nice(high * 1.08)
        return 0, top, step
    spread = max(high - low, high * 0.002, 3)
    pad = spread * 0.08
    step = _nice(spread + 2 * pad)[1]
    while True:
        bottom, top = step * math.floor((low - pad) / step), step * math.ceil((high + pad) / step)
        if (top - bottom) / step <= 5:
            return bottom, top, step
        step *= 2


def _signed(change: float, value: Callable[[float], str]) -> str:
    return f"{'+' if change >= 0 else '−'}{value(abs(change))}"


class _Time:
    """The time axis of points (UTC ts, oldest first): the MOST kept, their x, the runs without a gap, ticks."""

    def __init__(self, stamps: list[str]) -> None:
        self.stamps = stamps
        self.when = [datetime.fromisoformat(ts).astimezone() for ts in stamps]  # local time
        t0, t1 = self.when[0], self.when[-1]
        self.span = (t1 - t0).total_seconds() or 1
        picked: dict[int, int] = {}
        for i, t in enumerate(self.when):
            picked[min(int((t - t0).total_seconds() / self.span * MOST), MOST - 1)] = i
        self.keep = sorted({0, *picked.values()})
        self.runs: list[list[int]] = []
        for i in self.keep:
            if self.runs and self.when[i] - self.when[self.runs[-1][-1]] <= GAP:
                self.runs[-1].append(i)
            else:
                self.runs.append([i])

    def x(self, i: int) -> float:
        return W * (self.when[i] - self.when[0]).total_seconds() / self.span

    def label(self, i: int) -> str:
        t = self.when[i]
        return f"{t:%a} {t.day} {t:%b, %H:%M}"

    def dates(self) -> list[Tick]:
        t0, hours = self.when[0], self.span <= 2 * 86400
        ticks = [t0 + timedelta(seconds=self.span * k / 4) for k in range(5)]
        return [Tick(25 * k, f"{d:%H:%M}" if hours else f"{d.day} {d:%b}") for k, d in enumerate(ticks)]

    def week_ago(self) -> int | None:
        """The last kept point at least 7 days before the last one; None when less is shown."""
        return next((i for i in reversed(self.keep) if self.when[i] <= self.when[-1] - timedelta(days=7)), None)

    def frame(self, **kw) -> dict:
        t0 = self.when[0]
        return {
            "dates": self.dates(),
            "start": self.stamps[0],
            "stop": self.stamps[-1],
            "since": f"{t0.day} {t0:%b}",
        } | kw


def growth(
    points: list[tuple[str, float]],
    value: Callable[[float], str],
    axis: Callable[[float], str],
    also: list[tuple[str, float]] | None = None,
    also_value: Callable[[float], str] = str,
) -> Growth | None:
    """A metric over time ((UTC ts, value), oldest first) as a line over an area (axis: _span). `value`
    formats a value, `axis` an axis label. Leading zeros are dropped (taken before the library was first
    read). `also`: a second metric drawn as a line over its own area on the same axis, at each point its last
    value by then (`also_value` formats it, after the value in the pointer's data, then its height). None for
    fewer than two points."""
    while len(points) > 1 and points[0][1] == 0 and points[1][1] != 0:
        points = points[1:]
    if len(points) < 2:
        return None
    t = _Time([ts for ts, _ in points])
    second: list[float | None] = []
    if also:
        by, last, j = sorted(also), None, 0
        for ts, _ in points:
            while j < len(by) and by[j][0] <= ts:
                last, j = by[j][1], j + 1
            second.append(last)
    values = [v for _, v in points] + [v for v in second if v is not None]
    bottom, top, step = _span(min(values), max(values))

    def y(v: float) -> float:
        return H - H * (v - bottom) / (top - bottom)

    def xy(i: int) -> tuple[float, float]:
        return t.x(i), y(points[i][1])

    def path(run: list[int]) -> str:
        return "".join(f"{'L' if n else 'M'}{xy(i)[0]:.1f},{xy(i)[1]:.1f}" for n, i in enumerate(run))

    def also_path(run: list[int]) -> str:
        at = [(t.x(i), y(v)) for i in run if (v := second[i]) is not None]
        return "".join(f"{'L' if n else 'M'}{x:.1f},{v:.1f}" for n, (x, v) in enumerate(at))

    def datum(i: int) -> list:
        d = [round(100 * xy(i)[0] / W, 2), round(100 * xy(i)[1] / H, 2), t.label(i), value(points[i][1])]
        return [*d, also_value(v), round(100 * y(v) / H, 2)] if second and (v := second[i]) is not None else d

    def also_under(run: list[int]) -> str:
        at = [i for i in run if second[i] is not None]
        return f"{also_path(at)}L{t.x(at[-1]):.1f},{H}L{t.x(at[0]):.1f},{H}Z" if len(at) > 1 else ""

    def under(run: list[int]) -> str:
        return f"{path(run)}L{xy(run[-1])[0]:.1f},{H}L{xy(run[0])[0]:.1f},{H}Z"

    def bridge(a: int, b: int) -> str:
        (x0, y0), (x1, y1) = xy(a), xy(b)
        return f"M{x0:.1f},{y0:.1f}L{x1:.1f},{y1:.1f}"

    last, ago = xy(t.keep[-1]), t.week_ago()
    also_last = next((i for i in reversed(t.keep) if second and second[i] is not None), None)
    return Growth(
        **t.frame(
            values=[
                Tick(100 - 100 * k * step / (top - bottom), axis(bottom + k * step))
                for k in range(round((top - bottom) / step) + 1)
            ],
            end=(100 * last[0] / W, 100 * last[1] / H),
            points=json.dumps([datum(i) for i in t.keep]),
            now=value(points[-1][1]),
            total=_signed(points[-1][1] - points[0][1], value),
            week=_signed(points[-1][1] - points[ago][1], value) if ago is not None else "",
        ),
        line="".join(path(r) for r in t.runs),
        area="".join(under(r) for r in t.runs if len(r) > 1),
        gaps="".join(bridge(a[-1], b[0]) for a, b in itertools.pairwise(t.runs)),
        also="".join(also_path(r) for r in t.runs) if second else "",
        also_area="".join(also_under(r) for r in t.runs) if second else "",
        also_end=(100 * t.x(also_last) / W, 100 * y(second[also_last]) / H) if also_last is not None else None,
    )


def _points(change: float) -> str:
    return f"{change:.1f} pts"


def stacked(points: list[tuple[str, dict[str, float]]], tiers: list[tuple[str, str]]) -> Stack | None:
    """Shares over time ((UTC ts, {tier: count}), oldest first) as stacked bands, the first tier at the bottom
    and in the headline. The axis starts at a round share below the first tier's lowest, so the thin bands
    on top stay readable. None for fewer than two points."""
    points = [(ts, n) for ts, n in points if sum(n.values())]
    if len(points) < 2:
        return None
    t = _Time([ts for ts, _ in points])
    shares = [[100 * n.get(k, 0) / sum(n.values()) for k, _ in tiers] for _, n in points]
    low = min(shares[i][0] for i in t.keep)
    floor = max(0, 10 * math.floor((low - 4) / 10))
    step = 5 if 100 - floor <= 30 else 10 if 100 - floor <= 60 else 20

    def y(share: float) -> float:
        return H - H * max(share - floor, 0) / (100 - floor)

    tops = [list(itertools.accumulate(row)) for row in shares]

    def top(i: int, k: int) -> float:
        return tops[i][k] if k >= 0 else floor

    bands = []
    for k, (key, label) in enumerate(tiers):
        area = edge = ""
        for run in t.runs:
            upper = [f"{t.x(i):.1f},{y(top(i, k)):.1f}" for i in run]
            lower = [f"{t.x(i):.1f},{y(top(i, k - 1)):.1f}" for i in reversed(run)]
            edge += "M" + "L".join(upper)
            area += "M" + "L".join(upper + lower) + "Z"
        bands.append(Band(key, label, area, edge))
    first, last, ago = shares[0][0], shares[-1][0], t.week_ago()

    def down(share: float) -> float:  # rounded down like format.share
        return math.floor(share * 10 + 1e-9) / 10

    rows = [
        [
            round(100 * t.x(i) / W, 2),
            round(100 * y(shares[i][0]) / H, 2),
            t.label(i),
            [[key, label, round(points[i][1].get(key, 0)), down(shares[i][k])] for k, (key, label) in enumerate(tiers)],
        ]
        for i in t.keep
    ]
    return Stack(
        **t.frame(
            values=[Tick(100 - 100 * (v - floor) / (100 - floor), f"{v} %") for v in range(floor, 101, step)],
            end=(100 * t.x(t.keep[-1]) / W, 100 * y(last) / H),
            points=json.dumps(rows),
            now=f"{down(last):.1f} %",
            total=_signed(last - first, _points),
            week=_signed(last - shares[ago][0], _points) if ago is not None else "",
        ),
        bands=bands,
    )
