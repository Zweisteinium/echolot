"""Availability page: songs that no longer play (jobs/availability) and how that changed; Changes page: what
changed in the lists."""

from collections import Counter

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from echolot.web import stats
from echolot.web.common import DB, page

router = APIRouter(include_in_schema=False)


@router.get("/availability", response_class=HTMLResponse)
def availability_page(request: Request, con: DB, q: str = "") -> HTMLResponse:
    uid = stats.scope(request.state.user)
    now, relinked = stats.unavailable_now(con, uid)
    entries = stats.changes(con, uid, "unavailable", query=q.strip())
    extra = {"now": now, "counts": Counter(r["state"] for r in now), "relinked": relinked, "entries": entries}
    return page(request, "availability.html", nav="availability", labels=stats.CHANGES, q=q, **extra)


@router.get("/changes", response_class=HTMLResponse)
def changes_page(request: Request, con: DB, q: str = "") -> HTMLResponse:
    entries = stats.changes(con, stats.scope(request.state.user), "lists", query=q.strip())
    return page(request, "changes.html", nav="changes", labels=stats.CHANGES, q=q, entries=entries)
