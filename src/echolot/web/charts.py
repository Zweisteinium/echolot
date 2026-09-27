"""Small server-rendered SVG charts (no JavaScript)."""

import math
from dataclasses import dataclass

WIDTH, HEIGHT = 720, 190
LEFT, RIGHT, TOP, BOTTOM = 40, 8, 10, 24
BAR = 20  # px, capped; the rest of each slot is air
RADIUS = 4


@dataclass
class Bar:
    x: float
    y: float
    width: float
    height: float
    path: str  # rounded at the data end, square at the baseline
    slot_x: float
    slot_width: float
    label: str  # x-axis label ("" for most)
    tooltip: str


@dataclass
class Chart:
    width: int
    height: int
    bars: list[Bar]
    ticks: list[tuple[float, str]]  # (y, label)
    baseline: float
    left: int
    right: int
    empty: bool


def nice_max(value: float) -> float:
    """Smallest 1/2/5 x 10^k at or above value (at least 1)."""
    if value <= 1:
        return 1.0
    exp = 10 ** math.floor(math.log10(value))
    return next(m * exp for m in (1, 2, 5, 10) if m * exp >= value)


def columns(values: list[float | None], labels: list[str], tooltips: list[str]) -> Chart:
    """Column chart of `values` (None = no data) over equal slots."""
    top = nice_max(max((v for v in values if v is not None), default=0))
    plot_w, plot_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
    slot = plot_w / len(values)
    width = min(BAR, slot - 4)
    base = TOP + plot_h
    bars = []
    for i, v in enumerate(values):
        sx = LEFT + i * slot
        x = sx + (slot - width) / 2
        h = 0.0 if v is None else plot_h * v / top
        y = base - h
        r = min(RADIUS, h, width / 2)
        path = (
            f"M{x:.1f},{base:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x + width - r:.1f} Q{x + width:.1f},{y:.1f} {x + width:.1f},{y + r:.1f} V{base:.1f} Z"
            if h > 0
            else ""
        )
        bars.append(Bar(x, y, width, h, path, sx, slot, labels[i], tooltips[i]))
    ticks = [(base - plot_h * f, f"{top * f:g}") for f in (0, 0.5, 1)]
    return Chart(
        WIDTH, HEIGHT, bars, ticks, base, LEFT, WIDTH - RIGHT, all(v is None for v in values)
    )


def hours(rows: list[dict], key: str, unit: str) -> Chart:
    """Hour-of-day columns (0-23) of rows[i][key]."""
    values = [r[key] for r in rows]
    tips = [
        f"{r['hour']:02d}:00–{r['hour']:02d}:59 · "
        + (
            f"{r[key]:.1f} {unit} on average · {r['runs']} probe runs"
            if r[key] is not None
            else "no probes yet"
        )
        for r in rows
    ]
    labels = [f"{r['hour']:02d}" if r["hour"] % 3 == 0 else "" for r in rows]
    return columns(values, labels, tips)
