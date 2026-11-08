"""Changes page: songs that no longer play (jobs/availability) and what changed in the lists."""

from collections import Counter

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from echolot.web import stats
from echolot.web.common import DB, page

router = APIRouter(include_in_schema=False)


@router.get("/changes", response_class=HTMLResponse)
def changes_page(request: Request, con: DB, kind: str = "") -> HTMLResponse:
    uid = stats.scope(request.state.user)
    now, relinked = stats.unavailable_now(con, uid)
    counts = Counter(r["state"] for r in now)
    entries = stats.changes(con, uid, kind)
    extra = {"now": now, "counts": counts, "relinked": relinked, "entries": entries, "kind": kind}
    extra["labels"] = stats.CHANGES
    return page(request, "changes.html", nav="changes", **extra)
