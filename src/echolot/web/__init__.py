"""Web dashboard and HTTP API."""

import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from echolot import __version__, auth, db, filing, history, options, review, schedule, stats, vault
from echolot.config import Settings
from echolot.sources import ConfigError
from echolot.web import access, accounts, admin, charts, lists
from echolot.web.common import DB, back, page
from echolot.worker import Worker

log = logging.getLogger(__name__)

HERE = Path(__file__).parent


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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db.init(settings.db_path)
    secret_store = vault.Vault.from_env(settings.data_dir)  # a wrong key stops Echolot here
    worker = Worker(settings, secret_store)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.worker:
            worker.start()
        yield
        worker.stop()

    app = FastAPI(
        title="Echolot",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
        dependencies=[Depends(access.csrf_protect)],
    )
    app.state.settings = settings
    app.state.worker = worker
    app.state.vault = secret_store
    app.state.throttle = auth.Throttle()
    con = db.connect(settings.db_path)
    try:
        access.first_user(app, con)
    finally:
        con.close()
    app.middleware("http")(access.authenticate)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(num=num, size=size, ago=ago, until=until, mmss=mmss, minutes=minutes)
    templates.env.globals.update(pct=pct, tier_counts=stats.tier_counts, tiers=stats.TIERS, version=__version__)
    app.state.templates = templates
    for router in (access.router, admin.router, accounts.router, lists.router):
        app.include_router(router)

    def music_dir() -> Path:
        if not settings.library_dir:
            raise HTTPException(404, "no library configured (ECHOLOT_LIBRARY_DIR)")
        return settings.library_dir.parent

    # ------------------------------------------------------------ pages

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(HERE / "static" / "favicon.ico", headers={"Cache-Control": "max-age=86400"})

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request, con: DB) -> HTMLResponse:
        o = stats.overview(con)
        return page(
            request,
            "overview.html",
            nav="overview",
            o=o,
            donut=charts.donut(o["song_tiers"]),
            connected=secret_store.has(con, "spotify.refresh_token") or secret_store.has(con, "soundcloud.token"),
            **job_status(con),
        )

    def job_status(con: sqlite3.Connection) -> dict:
        now = datetime.now()
        rules = schedule.rules(con)
        last = {r["name"]: r for r in con.execute("SELECT * FROM jobs")}
        runs = dict(worker.runs)
        busy = {r.job.resource: r.job.label for r in runs.values()}
        rows = []
        for j in schedule.JOBS:
            r, run = last.get(j.name), runs.get(j.name)
            started = datetime.fromisoformat(r["started"]) if r and r["started"] else None
            nxt = schedule.next_run(rules[j.name], started, now)
            rows.append({
                "job": j, "rule": rules[j.name], "last": r, "run": run,
                "next": nxt.isoformat(timespec="seconds") if nxt else None,
                "waiting": busy.get(j.resource) if not run and nxt and nxt <= now else None,
            })  # fmt: skip
        return {"jobs": rows, "paused": options.get(con, options.Jobs).paused, "running": bool(runs)}

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_fragment(request: Request, con: DB) -> HTMLResponse:
        """The jobs table (htmx refreshes it while jobs run)."""
        return page(request, "_jobs.html", **job_status(con))

    @app.get("/api/jobs", tags=["jobs"])
    def api_jobs(con: DB) -> dict:
        """Every job: its schedule, last run, whether it runs now (with progress) and when it runs next."""
        s = job_status(con)
        return {
            "paused": s["paused"],
            "jobs": [
                {"name": r["job"].name, "label": r["job"].label, "schedule": r["rule"],
                 "running": bool(r["run"]), "progress": r["run"].progress if r["run"] else None,
                 "last_start": r["last"]["started"] if r["last"] else None,
                 "last_end": r["last"]["finished"] if r["last"] else None,
                 "last_ok": bool(r["last"]["ok"]) if r["last"] and r["last"]["ok"] is not None else None,
                 "last_message": r["last"]["message"] if r["last"] else None, "next": r["next"]}
                for r in s["jobs"]
            ],
        }  # fmt: skip

    def jobs_answer(request: Request, con: sqlite3.Connection, ok: str) -> Response:
        if request.headers.get("hx-request"):
            return page(request, "_jobs.html", **job_status(con))
        return back("/", ok=ok)

    @app.post("/jobs/{name}/run", tags=["jobs"])
    def run_job(request: Request, con: DB, name: str) -> Response:
        """Start a job now (also while jobs are paused)."""
        if not worker.trigger(name):
            raise HTTPException(404, "no such job")
        return jobs_answer(request, con, f"{schedule.BY_NAME[name].label} starts in a moment.")

    @app.post("/jobs/{name}/cancel", tags=["jobs"])
    def cancel_job(request: Request, con: DB, name: str) -> Response:
        """Stop a running job (songs in progress end as they are)."""
        if not worker.cancel(name):
            raise HTTPException(404, "not running")
        return jobs_answer(request, con, "Stopping.")

    @app.post("/jobs/pause", tags=["jobs"])
    def pause_jobs(request: Request, con: DB, paused: Annotated[bool, Form()] = False) -> Response:
        """Pause (no job starts on its schedule) or resume."""
        with con:
            options.update(con, options.Jobs, paused=paused)
        return jobs_answer(request, con, "Jobs paused." if paused else "Jobs resumed.")

    @app.get("/missing", response_class=HTMLResponse)
    def missing(request: Request, con: DB, list_key: Annotated[str, Query(alias="list")] = "") -> HTMLResponse:
        paths = filing.Paths(settings.library_dir.parent) if settings.library_dir else None
        return page(
            request,
            "missing.html",
            nav="missing",
            songs=stats.missing(con, list_key or None, paths),
            lists=stats.lists(con),
            selected=list_key,
        )

    @app.get("/lists/{key:path}", response_class=HTMLResponse)
    def list_page(request: Request, con: DB, key: str) -> HTMLResponse:
        lst = stats.get_list(con, key)
        if lst is None:
            raise HTTPException(404, "no such list")
        songs = stats.list_songs(con, key)
        return page(request, "list.html", nav="overview", lst=lst, songs=songs, tiers=stats.tiers_of(songs))

    @app.get("/activity", response_class=HTMLResponse)
    def activity(request: Request, con: DB, kind: str = "") -> HTMLResponse:
        return page(request, "activity.html", nav="activity", events=stats.events(con, kind), kind=kind)

    @app.get("/availability", response_class=HTMLResponse)
    def availability(request: Request, con: DB) -> HTMLResponse:
        a = stats.availability(con)
        return page(
            request,
            "availability.html",
            nav="availability",
            a=a,
            rare=charts.hours(a["hours"]["rare"], "users", "users"),
            common=charts.hours(a["hours"]["common"], "users", "users"),
        )

    @app.get("/review", response_class=HTMLResponse)
    def review_page(request: Request, con: DB) -> HTMLResponse:
        return page(request, "review.html", nav="review", items=review.items(con, music_dir()))

    @app.post("/review/{event_id}")
    def review_decide(con: DB, event_id: int, decision: Annotated[str, Form()]) -> RedirectResponse:
        try:
            item = review.decide(con, music_dir(), event_id, decision)
        except ConfigError as e:
            return back("/review", error=str(e))
        song = f"{item.event['artist']} – {item.event['title']}"
        return back("/review", ok=f"{song}: {decision}. Applied within a few minutes (Revert until then).")

    @app.post("/review/{event_id}/revert")
    def review_revert(con: DB, event_id: int) -> RedirectResponse:
        try:
            item = review.revert(con, music_dir(), event_id)
        except ConfigError as e:
            return back("/review", error=str(e))
        song = f"{item.event['artist']} – {item.event['title']}"
        return back("/review", ok=f"{song}: decision '{item.decision}' taken back.")

    @app.get("/review/{event_id}/audio")
    def review_audio(con: DB, event_id: int) -> FileResponse:
        item = review.find(con, music_dir(), event_id)
        if item is None:
            raise HTTPException(404, "not up for review")
        return FileResponse(item.file)

    # ------------------------------------------------------------ stats for dashboards

    @app.get("/api/stats", tags=["stats"])
    def api_stats(con: DB) -> dict[str, Any]:
        """The newest hourly snapshot: {metric: {label value: value}} ('' = no label)."""
        ts, values = history.latest(con)
        return {"ts": ts, "metrics": values}

    @app.get("/api/stats/metrics", tags=["stats"])
    def api_stats_metrics() -> dict[str, dict[str, str | None]]:
        """The metrics the snapshots hold, with their label name and meaning."""
        return {m: {"label": label, "help": h} for m, (label, h) in history.METRICS.items()}

    @app.get("/api/stats/history", tags=["stats"])
    def api_stats_history(
        con: DB, metric: str, key: str | None = None, since: str | None = None, until: str | None = None
    ) -> list[dict[str, Any]]:
        """One metric over time, oldest first: [{ts, time (unix), key, value}]. since/until: ISO local
        time or date. Hourly for the last 90 days, daily before."""
        if metric not in history.METRICS:
            raise HTTPException(404, f"no metric {metric!r}, see /api/stats/metrics")
        return [
            {
                "ts": r["ts"],
                "time": int(datetime.fromisoformat(r["ts"]).timestamp()),
                "key": r["key"],
                "value": r["value"],
            }
            for r in history.series(con, metric, key, since, until)
        ]

    @app.get("/api/stats/downloads", tags=["stats"])
    def api_stats_downloads(con: DB, days: int = 30) -> list[dict[str, Any]]:
        """Library events per day, action (new, upgrade, wrong-song, ...), source and format."""
        return [dict(r) for r in history.daily_events(con, days)]

    @app.get("/api/stats/availability", tags=["stats"])
    def api_stats_availability(con: DB, days: int = 30) -> list[dict[str, Any]]:
        """Availability probes: Soulseek users (and with lossless) per probed song and run."""
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        rows = con.execute(
            "SELECT ts, artist, title, kind, users, lossless_users, files FROM probes "
            "WHERE ts >= ? ORDER BY ts, artist, title",
            (since,),
        )
        return [dict(r) for r in rows]

    @app.get("/metrics", response_class=PlainTextResponse, tags=["stats"])
    def metrics(con: DB) -> PlainTextResponse:
        """Current values in the Prometheus text format."""
        return PlainTextResponse(history.prometheus(con), media_type="text/plain; version=0.0.4")

    return app
