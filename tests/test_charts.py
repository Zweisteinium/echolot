"""The overview's growth graph (charts.growth) and where it shows."""

import json

from echolot.web import charts


def fmt(v: float) -> str:
    return f"{round(v)} songs"


def axis(v: float) -> str:
    return str(round(v))


def test_growth_geometry() -> None:
    points = [("2026-09-24T12:00:00Z", 0), ("2026-09-25T12:00:00Z", 2000), ("2026-10-01T12:00:00Z", 5535)]
    g = charts.growth(points, fmt, axis, rebuilt_until="2026-09-25T12:00:00Z")
    assert [t.label for t in g.values] == ["0", "2000", "4000", "6000"]  # 3 steps reach 6,000 (5,535 + 8 %)
    assert g.values[0].pos == 100 and g.values[-1].pos == 0  # 0 at the bottom
    assert g.line.startswith("M") and g.rebuilt.count("L") == 1  # the rebuilt part ends where the real one starts
    assert g.end[0] == 100 and round(g.end[1], 1) == round(100 - 100 * 5535 / 6000, 1)
    assert (g.now, g.total, g.week) == ("5535 songs", "+5535 songs", "+5535 songs")  # 7 days back: 24 Sep
    hover = json.loads(g.points)
    assert hover[0][3] == "0 songs (rebuilt)" and hover[-1][3] == "5535 songs"


def test_growth_downsamples_and_breaks_at_gaps() -> None:
    hourly = [(f"2026-09-{d:02d}T{h:02d}:00:00Z", d * 24 + h) for d in range(1, 21) for h in range(24)]
    g = charts.growth(hourly, fmt, axis, most=50)
    assert len(json.loads(g.points)) <= 51 and g.line.count("M") == 1
    gap = [("2026-09-01T00:00:00Z", 1), ("2026-09-02T00:00:00Z", 2), ("2026-09-05T00:00:00Z", 3)]
    assert charts.growth(gap, fmt, axis).line.count("M") == 2  # 3 days without a snapshot


def test_growth_needs_two_points() -> None:
    assert charts.growth([("2026-09-01T00:00:00Z", 1)], fmt, axis) is None
    assert charts._nice(0) == (3, 1)  # an empty library still gets an axis
