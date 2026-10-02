"""The web interface and HTTP API: create_app puts the pages (one router per module) together with the
login check, the templates and the background worker."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException

from echolot import COMMIT, __version__, db
from echolot.config import Settings
from echolot.jobs.worker import Worker
from echolot.settings import auth, vault
from echolot.settings.sources import adopt
from echolot.web import access, account, accounts, admin, api, jobs, pages, review, sources, stats, users
from echolot.web.common import Assets, asset_urls, page
from echolot.web.format import FILTERS, pct

HERE = Path(__file__).parent
log = logging.getLogger(__name__)
ROUTERS = (access, account, users, pages, jobs, review, sources, accounts, admin, api)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db.init(settings.db_path)
    con = db.connect(settings.db_path)
    try:  # lists from before users had lists: the oldest admin's (sources.adopt)
        if adopted := adopt(con):
            log.info("adopted: %s", adopted)
    finally:
        con.close()
    secrets = vault.Vault.from_env(settings.data_dir)  # a wrong key stops Echolot here
    worker = Worker(settings, secrets)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.worker:
            worker.start()
        yield
        worker.stop()

    csrf = [Depends(access.csrf_protect), Depends(access.allowed)]
    docs = {"docs_url": "/api/docs", "redoc_url": None}
    app = FastAPI(title="Echolot", version=__version__, lifespan=lifespan, dependencies=csrf, **docs)
    app.state.settings, app.state.worker = settings, worker
    app.state.vault, app.state.throttle = secrets, auth.Throttle()
    app.middleware("http")(access.authenticate)
    app.mount("/static", Assets(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(FILTERS)
    templates.env.globals.update(pct=pct, tier_counts=stats.tier_counts, tiers=stats.TIERS)
    templates.env.globals.update(version=__version__, commit=COMMIT)
    templates.env.globals["asset"] = asset_urls(HERE / "static")
    app.state.templates = templates
    for module in ROUTERS:
        app.include_router(module.router)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        """A browser gets a page with the message (not allowed, out-of-date form), scripts the JSON."""
        if "text/html" in request.headers.get("accept", "") and exc.status_code in (400, 403, 404, 409):
            return page(request, "error.html", exc.status_code, message=exc.detail)
        return await http_exception_handler(request, exc)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(HERE / "static" / "favicon.ico", headers={"Cache-Control": "max-age=86400"})

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "commit": COMMIT}

    return app
