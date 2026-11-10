"""The web UI and HTTP API: create_app wires the routers, the login check, the templates and the worker."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException
from starlette.middleware.gzip import GZipMiddleware

from echolot import COMMIT, __version__, db
from echolot.config import Settings
from echolot.jobs.worker import Worker
from echolot.library import filing
from echolot.settings import auth, vault
from echolot.settings.sources import adopt
from echolot.web import (
    access,
    account,
    accounts,
    admin,
    api,
    changes,
    discogs,
    discover,
    jobs,
    pages,
    recorders,
    review,
    sources,
    stats,
    subsonic,
    unreleased,
    upload,
    users,
)
from echolot.web.common import Assets, asset_urls, page
from echolot.web.format import FILTERS, pct, share

HERE = Path(__file__).parent
log = logging.getLogger(__name__)
ROUTERS = (
    access,
    account,
    users,
    pages,
    discogs,
    discover,
    subsonic,
    changes,
    jobs,
    review,
    upload,
    unreleased,
    recorders,
    sources,
    accounts,
    admin,
    api,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db.init(settings.db_path)
    con = db.connect(settings.db_path)
    if settings.library_dir is not None:
        try:
            filing.Paths(settings.library_dir.parent).create()
        except OSError as e:  # a read-only test copy
            log.warning("folders not created: %s", e)
    try:  # lists from before users had lists go to the oldest admin
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
    # added last so it wraps everything; Starlette skips audio, images, streams and ranges
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
    app.mount("/static", Assets(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(FILTERS)
    templates.env.globals.update(pct=pct, percent=share, tier_counts=stats.tier_counts, tiers=stats.TIERS)
    templates.env.globals.update(version=__version__, commit=COMMIT)
    templates.env.globals["asset"] = asset_urls(HERE / "static")
    app.state.templates = templates
    for module in ROUTERS:
        app.include_router(module.router)
    app.include_router(recorders.api)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        """Browsers get an error page, scripts JSON."""
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
