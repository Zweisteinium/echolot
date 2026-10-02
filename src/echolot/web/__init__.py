"""The web interface and HTTP API: create_app puts the pages (one router per module) together with the
login check, the templates and the background worker."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates

from echolot import COMMIT, __version__, db
from echolot.config import Settings
from echolot.jobs.worker import Worker
from echolot.settings import auth, vault
from echolot.web import access, accounts, admin, api, jobs, pages, review, sources, stats
from echolot.web.common import Assets, asset_urls
from echolot.web.format import FILTERS, pct

HERE = Path(__file__).parent
ROUTERS = (access, pages, jobs, review, sources, accounts, admin, api)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db.init(settings.db_path)
    secrets = vault.Vault.from_env(settings.data_dir)  # a wrong key stops Echolot here
    worker = Worker(settings, secrets)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.worker:
            worker.start()
        yield
        worker.stop()

    csrf = [Depends(access.csrf_protect), Depends(access.changes_allowed)]
    docs = {"docs_url": "/api/docs", "redoc_url": None}
    app = FastAPI(title="Echolot", version=__version__, lifespan=lifespan, dependencies=csrf, **docs)
    app.state.settings, app.state.worker = settings, worker
    app.state.vault, app.state.throttle = secrets, auth.Throttle()
    con = db.connect(settings.db_path)
    try:
        access.first_user(app, con)
    finally:
        con.close()
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

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(HERE / "static" / "favicon.ico", headers={"Cache-Control": "max-age=86400"})

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "commit": COMMIT}

    return app
