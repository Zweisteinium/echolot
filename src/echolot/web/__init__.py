"""Web dashboard and HTTP API."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from sqlite3 import Connection
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from echolot import __version__, db, jobs, pipeline, stats
from echolot.config import Settings
from echolot.library import QUALITY
from echolot.scheduler import Scheduler

HERE = Path(__file__).parent


def num(n: int | None) -> str:
    return f"{n or 0:,}"


def size(b: int | None) -> str:
    b = b or 0
    return f"{b / 1e9:.1f} GB" if b >= 1e9 else f"{b / 1e6:.0f} MB"


def pct(part: int, whole: int) -> int:
    """Whole percent, rounded down: 100 only when complete."""
    return 100 * part // whole if whole else 0


def ago(value: str | int | None) -> str:
    """'5 min ago' for an ISO timestamp (local time) or unix time."""
    if not value:
        return "never"
    then = (
        datetime.fromtimestamp(value) if isinstance(value, int) else datetime.fromisoformat(value)
    )
    s = (datetime.now() - then).total_seconds()
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60:.0f} min ago"
    if s < 86400:
        return f"{s // 3600:.0f} h ago"
    return f"{s // 86400:.0f} d ago"


def mmss(seconds: float | None) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}" if seconds else "–"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db.init(settings.db_path)
    scheduler = Scheduler(settings.db_path, jobs.all_jobs(settings))

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
    templates.env.filters.update(num=num, size=size, ago=ago, mmss=mmss)
    templates.env.globals.update(pct=pct, quality_labels=dict(QUALITY), version=__version__)

    def get_db() -> Iterator[Connection]:
        con = db.connect(settings.db_path)
        try:
            yield con
        finally:
            con.close()

    DB = Annotated[Connection, Depends(get_db)]

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context)

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
            pipeline_jobs=pipeline.activity(root) if root else [],
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

    @app.post("/jobs/{name}/run")
    def run_job(name: str) -> RedirectResponse:
        if not scheduler.trigger(name):
            raise HTTPException(404, "no such job")
        return RedirectResponse("/", status_code=303)

    return app
