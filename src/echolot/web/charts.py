"""The overview's quality ring, server-rendered SVG (no JavaScript)."""

import math
from dataclasses import dataclass


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
