"""Shared web helpers: a DB connection per request, page rendering, static assets, redirects."""

import hashlib
import urllib.parse
from collections.abc import Callable, Iterator
from pathlib import Path
from sqlite3 import Connection
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from markupsafe import Markup, escape
from starlette.types import Scope

from echolot import db
from echolot.web import cache


def get_db(request: Request) -> Iterator[Connection]:
    """The request's connection; a request that changed the database drops the shared figures (cache)."""
    con = db.connect(request.app.state.settings.db_path)
    try:
        yield con
    finally:
        if con.total_changes:
            cache.clear()
        con.close()


DB = Annotated[Connection, Depends(get_db)]


def page(request: Request, template: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    """Render with the flash messages (?ok=, ?error=), the user and the CSRF token."""
    context.setdefault("ok", request.query_params.get("ok", ""))
    context.setdefault("error", request.query_params.get("error", ""))
    csrf = getattr(request.state, "csrf", "")
    context.setdefault("user", getattr(request.state, "user", None))
    if not template.startswith("_"):  # a whole page (fragments start with _): the navigation's counts
        context.setdefault("nav_counts", nav_counts(request, context["user"]))
    context["csrf_token"] = csrf
    context["csrf_input"] = Markup(f'<input type="hidden" name="csrf_token" value="{escape(csrf)}">')
    templates = request.app.state.templates
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def nav_counts(request: Request, user: Any) -> dict[str, int | None]:
    """The sidebar's counts, shared by all pages (web/cache): Missing, Buy, Discogs (None while it is off) and the
    songs waiting in Review."""
    if user is None:
        return {}
    from echolot.library import review
    from echolot.web import pages, stats

    library = request.app.state.settings.library_dir
    con = db.connect(request.app.state.settings.db_path)
    try:
        counts: dict[str, int | None] = dict(cache.get(con, pages.library_tabs, stats.scope(user)))
        if library is not None and user.can("review"):
            waiting = cache.get(con, review.waiting_count, library.parent, None if user.everyone else user.id)
            counts["review"] = waiting
        return counts
    finally:
        con.close()


def asset_urls(folder: Path) -> Callable[[str], str]:
    """/static/<name>?v=<content hash>, so no cache serves an old file after an update."""
    versions = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:10] for p in folder.iterdir() if p.is_file()}
    return lambda name: f"/static/{name}?v={versions[name]}"


class Assets(StaticFiles):
    """Static files; a hashed address (asset_urls) never changes, so it is cached for a year."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if b"v=" in scope.get("query_string", b"") and response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def back(path: str, **message: str) -> RedirectResponse:
    """Redirect to `path` (may end in #section) with ok=… or error=…."""
    path, _, section = path.partition("#")
    query = urllib.parse.urlencode(message)
    joined = f"{path}{'&' if '?' in path else '?'}{query}" if query else path
    return RedirectResponse(joined + (f"#{section}" if section else ""), status_code=303)
