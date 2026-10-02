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
    runs, requested = request.app.state.worker.state()
    busy = {r.job.resource: r.job.label for r in runs.values()}
    rows = []
    for j in schedule.JOBS:
        r, run = last.get(j.name), runs.get(j.name)
        started = datetime.fromisoformat(r["started"]) if r and r["started"] else None
        nxt = schedule.next_run(rules[j.name], started, now)
        starting, stopping = j.name in requested and not run, bool(run and run.stop.is_set())
        waiting = busy.get(j.resource) if not run and (starting or (nxt and nxt <= now)) else None
        next_start = nxt.isoformat(timespec="seconds") if nxt else None
        state = {"next": next_start, "waiting": waiting, "starting": starting, "stopping": stopping}
        rows.append({"job": j, "rule": rules[j.name], "last": r, "run": run} | state)
    changing = any(r["starting"] or r["stopping"] for r in rows)  # the table asks again every second then
    paused = options.get(con, options.Jobs).paused
    tasks = _tasks(rows, request.app.state.worker.finished())
    state = {"jobs": rows, "tasks": tasks, "steps": schedule.STEPS, "paused": paused}
    return state | {"running": bool(runs), "changing": changing}


def _tasks(rows: list[dict], finished: dict) -> list[dict]:
    """The jobs as the page shows them: per task its steps (job rows), the runs going on, its last run (the
    latest of its steps'), its next start, and the log to show (of the run going on, else of the last)."""
    by_job = {r["job"].name: r for r in rows}
    out = []
    for t in schedule.TASKS:
        steps = [by_job[j] for j in t.jobs]
        running = [s for s in steps if s["run"]]
        done = [s for s in steps if s["last"] and s["last"]["started"]]
        last = max(done, key=lambda s: s["last"]["started"]) if done else None
        upcoming = [s["next"] for s in steps if s["next"] and not s["run"] and s["rule"] is not None]
        shown = running[0]["run"] if running else finished.get(last["job"].name) if last else None
        waiting = next((s for s in steps if s["starting"] or s["waiting"]), None)
        out.append(
            {
                "task": t,
                "steps": steps,
                "running": running,
                "last": last,
                "waiting": waiting,
                "next": min(upcoming) if upcoming else None,
                "log": list(reversed(shown.log)) if shown else [],
            }
        )
    return out


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


@router.post("/jobs/start", include_in_schema=False)
def start_jobs(request: Request, con: DB, names: Annotated[str, Form()]) -> Response:
    """A task's button: start its jobs (comma-separated names) as soon as their queues are free."""
    jobs = [n for n in names.split(",") if n in schedule.BY_NAME]
    if not jobs or not all(request.app.state.worker.trigger(n) for n in jobs):
        raise HTTPException(404, "no such job")
    return answer(request, con, f"{schedule.TASK_OF[jobs[0]].label} starts in a moment.")


@router.post("/jobs/stop", include_in_schema=False)
def stop_jobs(request: Request, con: DB, names: Annotated[str, Form()]) -> Response:
    """A task's Stop: its running jobs end after their songs in progress."""
    stopped = [n for n in names.split(",") if request.app.state.worker.cancel(n)]
    if not stopped:
        raise HTTPException(404, "not running")
    return answer(request, con, "Stopping.")


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
