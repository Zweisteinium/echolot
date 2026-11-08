"""The Library page's graphs (charts.growth, charts.stacked) and its rings (charts.donut)."""

import json
import math

import pytest

from echolot.web import charts


def fmt(v: float) -> str:
    return f"{round(v)} songs"


def axis(v: float) -> str:
    return str(round(v))


def test_growth_geometry() -> None:
    points = [("2026-09-24T12:00:00Z", 0), ("2026-09-25T12:00:00Z", 2000), ("2026-10-01T12:00:00Z", 5535)]
    g = charts.growth(points, fmt, axis)
    assert [t.label for t in g.values] == ["0", "2000", "4000", "6000"]  # 3 steps reach 6,000 (5,535 + 8 %)
    assert g.values[0].pos == 100 and g.values[-1].pos == 0  # 0 at the bottom
    assert g.line.count("M") == 2 and g.gaps.count("M") == 1  # (the 0 before is left out; 6 days apart: a gap)
    assert g.end[0] == 100 and round(g.end[1], 1) == round(100 - 100 * 5535 / 6000, 1)
    assert (g.now, g.total, g.week) == ("5535 songs", "+3535 songs", "")  # less than 7 days shown: no week
    hover = json.loads(g.points)
    assert hover[0][3] == "2000 songs" and hover[-1][3] == "5535 songs"
    assert (g.start, g.stop) == ("2026-09-25T12:00:00Z", "2026-10-01T12:00:00Z")  # the stretch, to zoom into


def test_growth_downsamples_and_breaks_at_gaps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(charts, "MOST", 50)
    hourly = [(f"2026-09-{d:02d}T{h:02d}:00:00Z", d * 24 + h) for d in range(1, 21) for h in range(24)]
    g = charts.growth(hourly, fmt, axis)
    assert (
        len(json.loads(g.points)) <= 51 and g.line.count("M") == 1 and 168 <= int(g.week[1:].split()[0]) < 180
    )  # (from the kept point 7 days back)
    gap = [("2026-09-01T00:00:00Z", 1), ("2026-09-02T00:00:00Z", 2), ("2026-09-05T00:00:00Z", 3)]
    g = charts.growth(gap, fmt, axis)
    assert g.line.count("M") == 2  # 3 days without a snapshot
    assert g.area.count("Z") == 1 and g.area.count("M") == 1  # an area under the first stretch only
    assert g.gaps.count("M") == 1 and g.end[0] == 100  # dotted to the lone last point, which is marked


def test_growth_skips_a_count_before_the_first_scan() -> None:
    """A 0 before the first real count (the library not read yet) is no growth."""
    points = [("2026-10-07T17:55:00Z", 0), ("2026-10-07T18:55:00Z", 10.5e9), ("2026-10-08T18:55:00Z", 10.6e9)]
    g = charts.growth(points, fmt, axis)
    assert g.since == "7 Oct" and g.total == "+100000000 songs"


def test_growth_needs_two_points() -> None:
    assert charts.growth([("2026-09-01T00:00:00Z", 1)], fmt, axis) is None
    assert charts._nice(0) == (3, 1)  # an empty library still gets an axis


def drawn(d: charts.Donut) -> dict[str, float]:
    """Each slice's drawn length in px."""
    circumference = 2 * math.pi * d.radius
    return {s.key: float(s.dash.split()[0]) * circumference / 100 for s in d.slices}


def test_ring_slices_keep_the_order_of_their_counts() -> None:
    """A slice is its share of the ring less the gaps, at least 2.5px: more songs never draw shorter."""
    counts = {"lossless": 6816, "fake": 224, "lossy-high": 198, "lossy-mid": 147, "lossy-low": 397, "missing": 16}
    tiers = [(k, "", n) for k, n in counts.items()]
    d = charts.donut(tiers, size=160, width=22)
    px = drawn(d)
    assert px["missing"] == pytest.approx(2.5, abs=0.01) and px["lossy-mid"] > 3 * px["missing"]
    order = sorted(tiers, key=lambda t: t[2])
    assert [px[k] for k, _, _ in order] == sorted(px.values())  # more songs, never a shorter slice
    ring = 2 * math.pi * d.radius
    assert sum(px.values()) + 2 * len(tiers) == pytest.approx(ring)  # the slices and their gaps: the whole ring
    assert px["lossy-mid"] == pytest.approx((ring - 12) * 147 / 7798, rel=0.01)
    one = charts.donut([("lossless", "", 5), ("missing", "", 0)])
    assert [s.dash for s in one.slices] == ["100.000 0.000"]  # alone: the whole ring, no gap


def test_stacked_shares() -> None:
    """A band per tier, the first at the bottom; the axis starts at a round share below the first tier's
    lowest (70 % for 78 %); the headline in percentage points."""
    tiers = [("lossless", "Lossless"), ("lossy-low", "Lossy"), ("missing", "Missing")]
    points = [
        ("2026-09-27T23:00:00Z", {"lossless": 78, "lossy-low": 20, "missing": 2}),
        ("2026-10-10T23:00:00Z", {"lossless": 85, "lossy-low": 15}),
        ("2026-10-20T23:00:00Z", {"lossless": 0, "lossy-low": 0}),  # (none counted: left out)
        ("2026-11-05T12:00:00Z", {"lossless": 87.4, "lossy-low": 12.6}),
    ]
    g = charts.stacked(points, tiers)
    assert [t.label for t in g.values] == ["70 %", "75 %", "80 %", "85 %", "90 %", "95 %", "100 %"]
    assert g.values[0].pos == 100 and g.values[-1].pos == 0
    assert [b.key for b in g.bands] == ["lossless", "lossy-low", "missing"] and g.bands[0].area.endswith("Z")
    assert (g.now, g.total, g.week) == ("87.4 %", "+9.4 pts", "+2.4 pts")
    hover = json.loads(g.points)
    assert len(hover) == 3 and hover[0][3][2] == ["missing", "Missing", 2, 2.0]
    assert round(g.end[1], 1) == round(100 * (1 - (87.4 - 70) / 30), 1)  # the lossless band's top
    assert charts.stacked(points[:1], tiers) is None
    hover_now = json.loads(
        charts.stacked([points[0], ("2026-11-08T12:00:00Z", {"lossless": 7130, "lossy-low": 856})], tiers).points
    )[-1][3][0]
    assert hover_now[3] == 89.2  # 89.28: rounded down, as the headline and the ring (not 89.3)


def test_growth_axis_is_cut_to_a_stretch_of_a_large_library() -> None:
    """A small change on a large value cuts the axis to that stretch; growth from little keeps 0, a flat line
    gets a narrow band."""
    day = [("2026-11-06T12:00:00Z", 218.4e9), ("2026-11-07T00:00:00Z", 219.1e9), ("2026-11-07T12:00:00Z", 219.7e9)]
    g = charts.growth(day, fmt, axis)
    bottom, top = (float(g.values[k].label) for k in (0, -1))
    assert 217e9 < bottom < 218.4e9 and 219.7e9 < top < 221e9 and len(g.values) <= 6
    assert g.values[0].pos == 100 and g.values[-1].pos == 0
    ys = [p[1] for p in json.loads(g.points)]
    assert min(ys) > 0 and max(ys) < 100 and max(ys) - min(ys) > 50  # the line uses most of the height
    assert charts._span(2000, 5535) == (0, 6000, 2000)  # from 0: the values reach down near it
    low, high, step = charts._span(7791, 7791)  # flat: a narrow band of whole steps around it
    assert low < 7791 < high and step >= 1 and (high - low) / step <= 5
