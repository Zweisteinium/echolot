"""Numbers for dashboards: the hourly snapshots as JSON and in the Prometheus format."""

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from echolot.library import history
from echolot.web.common import DB

router = APIRouter(tags=["stats"])


@router.get("/api/stats")
def api_stats(con: DB) -> dict[str, Any]:
    """The newest hourly snapshot: {metric: {label value: value}} ('' = no label)."""
    ts, values = history.latest(con)
    return {"ts": ts, "metrics": values}


@router.get("/api/stats/metrics")
def api_stats_metrics() -> dict[str, dict[str, str | None]]:
    """The metrics the snapshots hold, with their label name and meaning."""
    return {m: {"label": label, "help": h} for m, (label, h) in history.METRICS.items()}


@router.get("/api/stats/history")
def api_stats_history(
    con: DB, metric: str, key: str | None = None, since: str | None = None, until: str | None = None
) -> list[dict[str, Any]]:
    """One metric over time, oldest first: [{ts, time (unix), key, value}]. since/until: ISO local
    time or date. Hourly for the last 90 days, daily before."""
    if metric not in history.METRICS:
        raise HTTPException(404, f"no metric {metric!r}, see /api/stats/metrics")
    rows = history.series(con, metric, key, since, until)
    return [dict(r) | {"time": int(datetime.fromisoformat(r["ts"]).timestamp())} for r in rows]  # ts, key, value


@router.get("/api/stats/downloads")
def api_stats_downloads(con: DB, days: int = 30) -> list[dict[str, Any]]:
    """Library events per day, action (new, upgrade, wrong-song, ...), source and format."""
    return [dict(r) for r in history.daily_events(con, days)]


@router.get("/api/stats/availability")
def api_stats_availability(con: DB, days: int = 30) -> list[dict[str, Any]]:
    """Availability probes: Soulseek users (and with lossless) per probed song and run."""
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    sql = "SELECT ts, artist, title, kind, users, lossless_users, files FROM probes WHERE ts >= ? ORDER BY ts, artist, title"
    return [dict(r) for r in con.execute(sql, (since,))]


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(con: DB) -> PlainTextResponse:
    """Current values in the Prometheus text format."""
    return PlainTextResponse(history.prometheus(con), media_type="text/plain; version=0.0.4")
