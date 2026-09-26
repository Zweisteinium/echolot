import time

from echolot import db
from echolot.config import Settings
from echolot.scheduler import Job, Scheduler


def fail(con: object) -> str:
    raise ValueError("boom")


def test_runs_are_recorded(settings: Settings) -> None:
    ok, bad = Job("ok", 60, lambda con: "done"), Job("bad", 60, fail)
    scheduler = Scheduler(settings.db_path, [ok, bad])
    con = db.connect(settings.db_path)
    scheduler.run(con, ok)
    scheduler.run(con, bad)
    rows = {r["name"]: r for r in con.execute("SELECT * FROM jobs")}
    assert (rows["ok"]["ok"], rows["ok"]["message"]) == (1, "done")
    assert (rows["bad"]["ok"], rows["bad"]["message"]) == (0, "ValueError: boom")
    assert rows["bad"]["finished"]
    assert 59 < scheduler.next_run_in("ok") <= 60


def test_thread_runs_due_and_triggered_jobs(settings: Settings) -> None:
    calls = []
    job = Job("count", 3600, lambda con: calls.append(1) or "ok")
    scheduler = Scheduler(settings.db_path, [job])
    assert not scheduler.trigger("nope")
    scheduler.start()
    try:
        deadline = time.monotonic() + 5
        while len(calls) < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert scheduler.trigger("count")
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        scheduler.stop()
    assert len(calls) == 2
