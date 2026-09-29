"""The jobs: their table on the overview (htmx refreshes it while jobs run), starting and stopping them,
pausing the schedule, and the same as JSON."""

import sqlite3
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from echolot.jobs import schedule
from echolot.settings import options
from echolot.web.common import DB, back, page

router = APIRouter()
LAST = {"last_start": "started", "last_end": "finished", "last_message": "message"}  # API name: column


def status(request: Request, con: sqlite3.Connection) -> dict:
    """Every job with its schedule, last run, the run in progress and the next start."""
    now = datetime.now()
    rules = schedule.rules(con)
    last = {r["name"]: r for r in con.execute("SELECT * FROM jobs")}
    runs = dict(request.app.state.worker.runs)
    busy = {r.job.resource: r.job.label for r in runs.values()}
    rows = []
    for j in schedule.JOBS:
        r, run = last.get(j.name), runs.get(j.name)
        started = datetime.fromisoformat(r["started"]) if r and r["started"] else None
        nxt = schedule.next_run(rules[j.name], started, now)
        waiting = busy.get(j.resource) if not run and nxt and nxt <= now else None
        next_start = nxt.isoformat(timespec="seconds") if nxt else None
        rows.append({"job": j, "rule": rules[j.name], "last": r, "run": run, "next": next_start, "waiting": waiting})
    return {"jobs": rows, "paused": options.get(con, options.Jobs).paused, "running": bool(runs)}


def answer(request: Request, con: sqlite3.Connection, ok: str) -> Response:
    if request.headers.get("hx-request"):
        return page(request, "_jobs.html", **status(request, con))
    return back("/", ok=ok)


@router.get("/jobs", response_class=HTMLResponse, include_in_schema=False)
def jobs_fragment(request: Request, con: DB) -> HTMLResponse:
    """The jobs table (htmx refreshes it while jobs run)."""
    return page(request, "_jobs.html", **status(request, con))


@router.get("/api/jobs", tags=["jobs"])
def api_jobs(request: Request, con: DB) -> dict:
    """Every job: its schedule, last run, whether it runs now (with progress) and when it runs next."""
    s = status(request, con)
    jobs = []
    for r in s["jobs"]:
        last, run = r["last"], r["run"]
        entry = {"name": r["job"].name, "label": r["job"].label, "schedule": r["rule"], "next": r["next"]}
        entry |= {"running": bool(run), "progress": run.progress if run else None}
        entry |= {key: last[column] if last else None for key, column in LAST.items()}
        entry["last_ok"] = bool(last["ok"]) if last and last["ok"] is not None else None
        jobs.append(entry)
    return {"paused": s["paused"], "jobs": jobs}


@router.post("/jobs/{name}/run", tags=["jobs"])
def run_job(request: Request, con: DB, name: str) -> Response:
    """Start a job now (also while jobs are paused)."""
    if not request.app.state.worker.trigger(name):
        raise HTTPException(404, "no such job")
    return answer(request, con, f"{schedule.BY_NAME[name].label} starts in a moment.")


@router.post("/jobs/{name}/cancel", tags=["jobs"])
def cancel_job(request: Request, con: DB, name: str) -> Response:
    """Stop a running job (songs in progress end as they are)."""
    if not request.app.state.worker.cancel(name):
        raise HTTPException(404, "not running")
    return answer(request, con, "Stopping.")


@router.post("/jobs/pause", tags=["jobs"])
def pause_jobs(request: Request, con: DB, paused: Annotated[bool, Form()] = False) -> Response:
    """Pause (no job starts on its schedule) or resume."""
    with con:
        options.update(con, options.Jobs, paused=paused)
    return answer(request, con, "Jobs paused." if paused else "Jobs resumed.")
