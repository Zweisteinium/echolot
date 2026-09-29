"""Settings page (schedule, access, account, API tokens, configuration file) and /api/config."""

import sqlite3
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from echolot import auth, configfile, jobs, options, pipeline_config, schedule, sources, vault
from echolot.web.access import set_session_cookie
from echolot.web.common import DB, back, page

router = APIRouter()


def _written(request: Request, con: sqlite3.Connection) -> None:
    """Write the pipeline's config files after a change, and refresh."""
    pipeline_config.write(con, request.app.state.settings)
    request.app.state.scheduler.trigger("refresh")


def _settings_page(request: Request, con: sqlite3.Connection, status_code: int = 200,
                   **extra: Any) -> HTMLResponse:  # fmt: skip
    settings = request.app.state.settings
    root = settings.pipeline_dir
    user: auth.User = request.state.user
    return page(
        request,
        "settings.html",
        status_code,
        nav="settings",
        jobs=schedule.status(root, schedule.rules(con)) if root else [],
        refresh=jobs.refresh_minutes(con),
        settings=settings,
        metrics=options.get(con, options.Metrics),
        auth_options=options.get(con, options.Auth),
        tokens=auth.tokens(con, user),
        secrets=vault.listing(con),
        vault_source=request.app.state.vault.source,
        **extra,
    )


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
def settings_page(request: Request, con: DB) -> HTMLResponse:
    return _settings_page(request, con)


@router.post("/settings", include_in_schema=False)
async def settings_save(request: Request, con: DB) -> RedirectResponse:
    form = await request.form()  # one field per pipeline job
    try:
        general = options.validate(options.General, {"refresh_minutes": form.get("refresh", "")})
    except options.OptionsError:
        return back("/settings", error="Echolot refresh: whole minutes, 1 to 1440.")
    try:
        values = {
            j.name: schedule.parse_when(str(form[j.name]), j)
            for j in schedule.JOBS
            if j.name in form
        }
        with con:
            schedule.store(con, values)
            options.put(con, general)
    except sources.ConfigError as err:
        return back("/settings", error=str(err))
    pipeline_config.write(con, request.app.state.settings)
    request.app.state.scheduler.set_interval("refresh", general.refresh_minutes * 60)
    return back("/settings", ok="Settings saved. The pipeline applies them within a minute.")


@router.post("/settings/access", include_in_schema=False)
def settings_access(
    con: DB,
    session_days: Annotated[int, Form()],
    metrics_public: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    try:
        with con:
            options.update(con, options.Auth, session_days=session_days)
            options.update(con, options.Metrics, public=metrics_public)
    except options.OptionsError:
        return back("/settings", error="A login lasts 1 to 365 days.")
    return back(
        "/settings", ok="Access settings saved (the login length counts from the next login)."
    )


@router.post("/settings/password", include_in_schema=False)
def settings_password(
    request: Request,
    con: DB,
    current: Annotated[str, Form()],
    password: Annotated[str, Form()],
    repeat: Annotated[str, Form()],
) -> RedirectResponse:
    user: auth.User = request.state.user
    if auth.verify(con, user.name, current) is None:
        return back("/settings", error="The current password is wrong.")
    try:
        auth.check_new_password(password, repeat)
        auth.set_password(con, user, password)
    except auth.AuthError as err:
        return back("/settings", error=str(err))
    response = back("/settings", ok="Password changed. Other logins of yours have ended.")
    set_session_cookie(request, response, con, user)  # this browser stays logged in
    return response


@router.post("/settings/sessions/end-others", include_in_schema=False)
def settings_end_sessions(request: Request, con: DB) -> RedirectResponse:
    n = auth.end_other_sessions(con, request.state.user, request.cookies.get(auth.COOKIE, ""))
    return back("/settings", ok=f"{n} other login(s) ended.")


@router.post("/settings/tokens", response_class=HTMLResponse, include_in_schema=False)
def settings_token_create(request: Request, con: DB, name: Annotated[str, Form()]) -> Response:
    try:
        token = auth.create_token(con, request.state.user, name)
    except auth.AuthError as err:
        return back("/settings", error=str(err))
    # shown once, in this response only (a redirect would put it into the URL)
    return _settings_page(request, con, new_token=token, new_token_name=name.strip())


@router.post("/settings/tokens/{token_id}/revoke", include_in_schema=False)
def settings_token_revoke(request: Request, con: DB, token_id: int) -> RedirectResponse:
    if not auth.revoke_token(con, request.state.user, token_id):
        return back("/settings", error="No such token.")
    return back("/settings", ok="Token revoked.")


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
    request: Request,
    con: DB,
    file: Annotated[UploadFile | None, File()] = None,
    text: Annotated[str, Form()] = "",
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
    except configfile.ConfigError as err:
        return page(request, "config_import.html", 400, nav="settings", text=text, diff="",
                    error=f"Not imported: {err}")  # fmt: skip
    return page(request, "config_import.html", nav="settings", text=text, diff=diff)


@router.post("/settings/config/import/apply", include_in_schema=False)
def config_import_apply(
    request: Request, con: DB, text: Annotated[str, Form()]
) -> RedirectResponse:
    try:
        configfile.apply(con, text.replace("\r\n", "\n"))
    except configfile.ConfigError as err:
        return back("/settings", error=f"Not imported: {err}")
    _written(request, con)
    request.app.state.scheduler.set_interval("refresh", jobs.refresh_minutes(con) * 60)
    return back("/settings", ok="Configuration imported.")


@router.get("/api/config", tags=["config"])
def api_config(con: DB) -> dict[str, Any]:
    """The whole configuration as echolot.yml holds it (lists, schedule, settings; no secrets)."""
    return configfile.export_data(con)


@router.put("/api/config", tags=["config"])
def api_config_put(
    request: Request,
    con: DB,
    data: Annotated[dict[str, Any], Body()],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Import a configuration (the structure of GET /api/config; parts left out stay as they are).
    Answers what changes as a unified diff; with dry_run nothing is stored."""
    text = configfile.dump(data)
    try:
        diff = configfile.preview(con, text)
        if diff and not dry_run:
            configfile.apply(con, text)
    except configfile.ConfigError as err:
        raise HTTPException(422, str(err)) from err
    if diff and not dry_run:
        _written(request, con)
        request.app.state.scheduler.set_interval("refresh", jobs.refresh_minutes(con) * 60)
    return {"changed": bool(diff), "applied": bool(diff) and not dry_run, "diff": diff}
