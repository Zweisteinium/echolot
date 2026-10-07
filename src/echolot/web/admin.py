"""Settings page (schedule, access, Navidrome, configuration file) and /api/config; admins only."""

import os
import sqlite3
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from echolot.jobs import schedule
from echolot.services import navidrome
from echolot.settings import configfile, options
from echolot.settings.sources import ConfigError
from echolot.web.common import DB, back, page

router = APIRouter()


def _settings_page(request: Request, con: sqlite3.Connection) -> HTMLResponse:
    rules = schedule.rules(con)
    return page(
        request,
        "settings.html",
        nav="settings",
        tasks=[(t, [(schedule.BY_NAME[n], schedule.when_text(rules[n])) for n in t.jobs]) for t in schedule.TASKS],
        settings=request.app.state.settings,
        soulseek=options.get(con, options.Soulseek),
        files=options.get(con, options.Files),
        metrics=options.get(con, options.Metrics),
        auth_options=options.get(con, options.Auth),
        navidrome=options.get(con, options.Navidrome),
        navidrome_env=os.environ.get("ECHOLOT_NAVIDROME_URL", ""),
        navidrome_ok=_service_state(request, con),
        vault_source=request.app.state.vault.source,
    )


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
def settings_page(request: Request, con: DB) -> HTMLResponse:
    return _settings_page(request, con)


@router.post("/settings", include_in_schema=False)
async def settings_save(request: Request, con: DB) -> RedirectResponse:
    form = await request.form()  # one field per job
    try:
        values = {j.name: schedule.parse_when(str(form[j.name]), j) for j in schedule.JOBS if j.name in form}
        soulseek = options.validate(options.Soulseek, {
            **options.get(con, options.Soulseek).model_dump(),
            **{k: form[k] for k in ("parallel", "upgrade_batch", "stall_minutes") if k in form},
        })  # fmt: skip
        files = options.validate(options.Files, {"keep_hires": form.get("keep_hires") == "1"})
        with con:
            schedule.store(con, values)
            options.put(con, soulseek)
            options.put(con, files)
    except (ConfigError, options.OptionsError) as err:
        return back("/settings", error=str(err))
    return back("/settings", ok="Settings saved.")


def _service_state(request: Request, con: sqlite3.Connection) -> str:
    """'' when the service account works, else what is wrong (for the settings page)."""
    svc = navidrome.service(con, request.app.state.vault)
    if svc is None:
        return "Not set up: Echolot can't read Navidrome's users or set playlist owners."
    try:
        svc.users()
    except navidrome.NavidromeError as e:
        return str(e)
    return ""


@router.post("/settings/access", include_in_schema=False)
def settings_access(
    request: Request,
    con: DB,
    session_days: Annotated[int, Form()],
    metrics_public: Annotated[bool, Form()] = False,
    navidrome_url: Annotated[str, Form()] = "",
    service_user: Annotated[str, Form()] = "",
    service_password: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Login length, /metrics, Navidrome's address and service account (an empty password keeps the
    stored one). A Navidrome address that does not answer is not taken: nobody could log in."""
    url = navidrome_url.strip().rstrip("/")
    if not url and not os.environ.get("ECHOLOT_NAVIDROME_URL"):
        return back("/settings", error="Not saved: without Navidrome's address nobody could log in.")
    if url and url != options.get(con, options.Navidrome).url and not navidrome.reachable(url):
        return back("/settings", error=f"Not saved: Navidrome does not answer at {url} (logins would fail).")
    try:
        with con:
            options.update(con, options.Auth, session_days=session_days)
            options.update(con, options.Metrics, public=metrics_public)
            options.update(con, options.Navidrome, url=url, service_user=service_user.strip() or "admin")
    except options.OptionsError as err:
        return back("/settings", error=f"Not saved: {err}")
    if service_password:
        with con:
            request.app.state.vault.set(con, navidrome.SERVICE_PASSWORD, service_password)
    problem = _service_state(request, con)
    if problem:
        return back("/settings", error=f"Saved. The service account does not work yet: {problem}")
    return back("/settings", ok="Access settings saved; the service account works.")


# ------------------------------------------------------------ echolot.yml


@router.get("/settings/config/export", include_in_schema=False)
def config_export(con: DB) -> PlainTextResponse:
    return PlainTextResponse(
        configfile.export_text(con),
        media_type="application/yaml",
        headers={"Content-Disposition": f'attachment; filename="echolot-{date.today()}.yml"'},
    )


@router.post("/settings/config/import", response_class=HTMLResponse, include_in_schema=False)
async def config_import_preview(
    request: Request, con: DB, file: Annotated[UploadFile | None, File()] = None, text: Annotated[str, Form()] = ""
) -> Response:
    if file is not None and file.filename:
        raw = await file.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return back("/settings", error="That file is not UTF-8 text.")
    text = text.replace("\r\n", "\n")
    if not text.strip():
        return back("/settings", error="Choose an echolot.yml to import.")
    try:
        diff = configfile.preview(con, text)
    except ConfigError as err:
        return page(request, "config_import.html", 400, nav="settings", text=text, diff="",
                    error=f"Not imported: {err}")  # fmt: skip
    return page(request, "config_import.html", nav="settings", text=text, diff=diff)


@router.post("/settings/config/import/apply", include_in_schema=False)
def config_import_apply(request: Request, con: DB, text: Annotated[str, Form()]) -> RedirectResponse:
    try:
        configfile.apply(con, text.replace("\r\n", "\n"))
    except ConfigError as err:
        return back("/settings", error=f"Not imported: {err}")
    return back("/settings", ok="Configuration imported.")


@router.get("/api/config", tags=["config"])
def api_config(con: DB) -> dict[str, Any]:
    """The whole configuration as echolot.yml holds it (lists, schedule, settings; no secrets)."""
    return configfile.export_data(con)


@router.put("/api/config", tags=["config"])
def api_config_put(
    request: Request, con: DB, data: Annotated[dict[str, Any], Body()], dry_run: bool = False
) -> dict[str, Any]:
    """Import a configuration (the structure of GET /api/config; parts left out stay as they are).
    Answers what changes as a unified diff; with dry_run nothing is stored."""
    text = configfile.dump(data)
    try:
        diff = configfile.preview(con, text)
        if diff and not dry_run:
            configfile.apply(con, text)
    except ConfigError as err:
        raise HTTPException(422, str(err)) from err
    return {"changed": bool(diff), "applied": bool(diff) and not dry_run, "diff": diff}
