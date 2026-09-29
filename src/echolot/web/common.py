"""Helpers the web modules share: database connection per request, page rendering, redirects."""

import hashlib
import urllib.parse
from collections.abc import Callable, Iterator
from pathlib import Path
from sqlite3 import Connection
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import Markup, escape

from echolot import db


def get_db(request: Request) -> Iterator[Connection]:
    con = db.connect(request.app.state.settings.db_path)
    try:
        yield con
    finally:
        con.close()


DB = Annotated[Connection, Depends(get_db)]


def page(request: Request, template: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    """Render a template with the flash messages, the logged-in user and the CSRF token."""
    context.setdefault("ok", request.query_params.get("ok", ""))
    context.setdefault("error", request.query_params.get("error", ""))
    csrf = getattr(request.state, "csrf", "")
    context.setdefault("user", getattr(request.state, "user", None))
    context["csrf_token"] = csrf
    context["csrf_input"] = Markup(f'<input type="hidden" name="csrf_token" value="{escape(csrf)}">')
    templates = request.app.state.templates
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def asset_urls(folder: Path) -> Callable[[str], str]:
    """/static/<name>?v=<content hash>: a changed file gets a new address, so no cache (browser, reverse
    proxy) serves the old one after an update."""
    versions = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:10] for p in folder.iterdir() if p.is_file()}
    return lambda name: f"/static/{name}?v={versions[name]}"


def back(path: str, **message: str) -> RedirectResponse:
    query = urllib.parse.urlencode(message)
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)
