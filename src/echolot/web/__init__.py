"""Web dashboard and HTTP API."""

import urllib.parse
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from sqlite3 import Connection
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from echolot import __version__, db, history, jobs, pipeline, review, schedule, sources, stats
from echolot.config import Settings
from echolot.library import QUALITY
from echolot.scheduler import Scheduler
from echolot.web import charts

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
    return (
        datetime.fromtimestamp(value) if isinstance(value, int) else datetime.fromisoformat(value)
    )


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
    con = db.connect(settings.db_path)
    try:
        scheduler = Scheduler(settings.db_path, jobs.all_jobs(settings, jobs.refresh_minutes(con)))
    finally:
        con.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(
        title="Echolot",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.scheduler = scheduler
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(
        num=num, size=size, ago=ago, until=until, mmss=mmss, minutes=minutes
    )
    templates.env.globals.update(pct=pct, quality_labels=dict(QUALITY), version=__version__)

    def get_db() -> Iterator[Connection]:
        con = db.connect(settings.db_path)
        try:
            yield con
        finally:
            con.close()

    DB = Annotated[Connection, Depends(get_db)]

    def page(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
        context.setdefault("ok", request.query_params.get("ok", ""))
        context.setdefault("error", request.query_params.get("error", ""))
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    def back(path: str, **message: str) -> RedirectResponse:
        query = urllib.parse.urlencode(message)
        return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)

    def pipeline_root() -> Path:
        if not settings.pipeline_dir:
            raise HTTPException(404, "no pipeline configured (ECHOLOT_PIPELINE_DIR)")
        return settings.pipeline_dir

    # ------------------------------------------------------------ pages

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request, con: DB) -> HTMLResponse:
        root = settings.pipeline_dir
        return page(
            request,
            "overview.html",
            nav="overview",
            o=stats.overview(con),
            pipeline_jobs=schedule.status(root) if root else [],
            paused=bool(root and pipeline.paused(root)),
        )

    @app.get("/missing", response_class=HTMLResponse)
    def missing(
        request: Request, con: DB, list_key: Annotated[str, Query(alias="list")] = ""
    ) -> HTMLResponse:
        return page(
            request,
            "missing.html",
            nav="missing",
            songs=stats.missing(con, list_key or None),
            lists=stats.lists(con),
            selected=list_key,
        )

    @app.get("/lists/{key:path}", response_class=HTMLResponse)
    def list_page(request: Request, con: DB, key: str) -> HTMLResponse:
        lst = stats.get_list(con, key)
        if lst is None:
            raise HTTPException(404, "no such list")
        return page(request, "list.html", nav="overview", lst=lst, songs=stats.list_songs(con, key))

    @app.get("/activity", response_class=HTMLResponse)
    def activity(request: Request, con: DB, kind: str = "") -> HTMLResponse:
        return page(
            request, "activity.html", nav="activity", events=stats.events(con, kind), kind=kind
        )

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

    def music_dir() -> Path:
        if not (settings.library_dir and settings.pipeline_dir):
            raise HTTPException(404, "no library or pipeline configured")
        return settings.library_dir.parent  # tracks/ and inbox/, as the pipeline's /music

    @app.get("/review", response_class=HTMLResponse)
    def review_page(request: Request, con: DB) -> HTMLResponse:
        return page(
            request,
            "review.html",
            nav="review",
            items=review.items(con, pipeline_root(), music_dir()),
        )

    @app.post("/review/{event_id}")
    def review_decide(con: DB, event_id: int, decision: Annotated[str, Form()]) -> RedirectResponse:
        try:
            item = review.decide(con, pipeline_root(), music_dir(), event_id, decision)
        except sources.ConfigError as e:
            return back("/review", error=str(e))
        song = f"{item.event['artist']} – {item.event['title']}"
        return back(
            "/review", ok=f"{song}: {decision}. The pipeline applies it within about 10 min."
        )

    @app.get("/review/{event_id}/audio")
    def review_audio(con: DB, event_id: int) -> FileResponse:
        item = review.find(con, pipeline_root(), music_dir(), event_id)
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
        con: DB,
        metric: str,
        key: str | None = None,
        since: str | None = None,
        until: str | None = None,
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

    @app.post("/jobs/{name}/run")
    def run_job(name: str) -> RedirectResponse:
        if not scheduler.trigger(name):
            raise HTTPException(404, "no such job")
        return RedirectResponse("/", status_code=303)

    # ------------------------------------------------------------ sources.yml

    def edit_sources(
        con: Connection, expected: str, note: str, change: Callable[[str], str], ok: str
    ) -> RedirectResponse:
        root = pipeline_root()
        try:
            text = sources.read(root)
            sources.save(con, root, change(text), expected, note)
        except sources.ConfigError as err:
            return back("/sources", error=str(err))
        scheduler.trigger("refresh")
        return back("/sources", ok=ok)

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request, con: DB) -> HTMLResponse:
        root = pipeline_root()
        text = sources.read(root)
        known = {r["key"]: r for r in stats.lists(con)}
        return page(
            request,
            "sources.html",
            nav="sources",
            entries=sources.entries(text),
            known=known,
            likes=sources.likes_state(text),
            file_version=sources.version(text),
        )

    @app.post("/sources/add")
    def sources_add(
        con: DB,
        version: Annotated[str, Form()],
        url: Annotated[str, Form()],
        title: Annotated[str, Form()] = "",
        playlist: Annotated[bool, Form()] = False,
    ) -> RedirectResponse:
        return edit_sources(
            con,
            version,
            f"added {url.strip()}",
            lambda text: sources.add_list(text, url, title, playlist),
            "List added. The pipeline fetches it on its next run.",
        )

    @app.post("/sources/update")
    def sources_update(
        con: DB,
        version: Annotated[str, Form()],
        key: Annotated[str, Form()],
        title: Annotated[str, Form()] = "",
        playlist: Annotated[bool, Form()] = False,
    ) -> RedirectResponse:
        return edit_sources(
            con,
            version,
            f"changed {key}",
            lambda text: sources.update_list(text, key, title, playlist),
            "List saved.",
        )

    @app.post("/sources/remove")
    def sources_remove(
        con: DB, version: Annotated[str, Form()], key: Annotated[str, Form()]
    ) -> RedirectResponse:
        return edit_sources(
            con,
            version,
            f"removed {key}",
            lambda text: sources.remove_list(text, key),
            "List removed. Its songs stay in the library; delete its playlist in the music "
            "server if you no longer want it.",
        )

    @app.post("/sources/options")
    def sources_options(
        con: DB,
        version: Annotated[str, Form()],
        spotify_likes: Annotated[bool, Form()] = False,
        soundcloud_likes: Annotated[bool, Form()] = False,
        soundcloud_user: Annotated[str, Form()] = "",
        removed_playlists: Annotated[bool, Form()] = False,
    ) -> RedirectResponse:
        def change(text: str) -> str:
            text = sources.set_likes(text, "spotify", spotify_likes)
            text = sources.set_likes(text, "soundcloud", soundcloud_likes, soundcloud_user)
            return sources.set_removed_playlists(text, removed_playlists)

        return edit_sources(con, version, "likes/options changed", change, "Options saved.")

    @app.get("/sources/yaml", response_class=HTMLResponse)
    def sources_yaml(request: Request, con: DB) -> HTMLResponse:
        text = sources.read(pipeline_root())
        return page(
            request,
            "sources_yaml.html",
            nav="sources",
            text=text,
            file_version=sources.version(text),
            versions=sources.versions(con),
        )

    @app.post("/sources/yaml", response_model=None)
    def sources_yaml_save(
        request: Request,
        con: DB,
        version: Annotated[str, Form()],
        text: Annotated[str, Form()],
    ) -> HTMLResponse | RedirectResponse:
        text = text.replace("\r\n", "\n")
        try:
            sources.save(con, pipeline_root(), text, version, "edited as YAML")
        except sources.ConfigError as err:  # keep the user's edit on the page
            return page(
                request,
                "sources_yaml.html",
                400,
                nav="sources",
                text=text,
                file_version=version,
                versions=sources.versions(con),
                error=str(err),
            )
        scheduler.trigger("refresh")
        return back("/sources/yaml", ok="sources.yml saved.")

    @app.get("/sources/versions/{vid}", response_class=PlainTextResponse)
    def sources_version(con: DB, vid: int) -> str:
        text = sources.old_version(con, vid)
        if text is None:
            raise HTTPException(404, "no such version")
        return text

    @app.post("/sources/versions/{vid}/restore")
    def sources_restore(con: DB, vid: int, version: Annotated[str, Form()]) -> RedirectResponse:
        text = sources.old_version(con, vid)
        if text is None:
            raise HTTPException(404, "no such version")
        try:
            sources.save(con, pipeline_root(), text, version, f"restored version {vid}")
        except sources.ConfigError as err:
            return back("/sources/yaml", error=str(err))
        scheduler.trigger("refresh")
        return back("/sources/yaml", ok=f"Version {vid} restored.")

    # ------------------------------------------------------------ settings

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, con: DB) -> HTMLResponse:
        root = settings.pipeline_dir
        return page(
            request,
            "settings.html",
            nav="settings",
            jobs=schedule.status(root) if root else [],
            refresh=jobs.refresh_minutes(con),
            settings=settings,
        )

    @app.post("/settings")
    async def settings_save(request: Request) -> RedirectResponse:
        form = await request.form()
        try:
            refresh = int(str(form.get("refresh", jobs.REFRESH_MINUTES)))
        except ValueError:
            return back("/settings", error="Echolot refresh: whole minutes.")
        if refresh < 1:
            return back("/settings", error="Echolot refresh: at least 1 minute.")
        con = db.connect(settings.db_path)
        try:
            if settings.pipeline_dir:
                current = schedule.read(settings.pipeline_dir)
                values = {
                    j.name: schedule.parse_when(str(form[j.name]), j)
                    if j.name in form
                    else current[j.name]
                    for j in schedule.JOBS
                }
                schedule.save(con, settings.pipeline_dir, values)
            with con:
                db.set_meta(con, "refresh_minutes", str(refresh))
        except sources.ConfigError as err:
            return back("/settings", error=str(err))
        finally:
            con.close()
        scheduler.set_interval("refresh", refresh * 60)
        return back("/settings", ok="Settings saved. The pipeline applies them within a minute.")

    return app
