"""The demo instance: Echolot from this checkout on the demo data (data.py), no jobs running, on 127.0.0.1:<port>.
Writes rick's session to <work dir>/cookie.txt. One addition for the Jobs scene: GET /demo/run starts a made-up
FLAC upgrade that counts on by itself (the worker is off, so nothing else would ever run).

    uv run python tools/demo/serve.py <work dir> [port]   (make.py starts and stops it)"""

import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

WORK = Path(sys.argv[1]).resolve()
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8497
os.environ.update(ECHOLOT_DATA_DIR=str(WORK / "data"), ECHOLOT_LIBRARY_DIR=str(WORK / "music" / "tracks"))
os.environ.update(ECHOLOT_WORKER="off", ECHOLOT_PORT=str(PORT), TZ="Europe/Berlin")

import uvicorn  # noqa: E402

from echolot.config import Settings  # noqa: E402
from echolot.jobs import schedule  # noqa: E402
from echolot.jobs.worker import Run  # noqa: E402
from echolot.settings import auth  # noqa: E402
from echolot.settings.vault import Vault  # noqa: E402
from echolot.web import create_app  # noqa: E402

settings = Settings.from_env()
app = create_app(settings)


def upgrade(run: Run, start: int = 152, total: int = 200) -> None:
    """A FLAC upgrade run going on: a song every half second, mostly not found, now and then an upgrade."""
    counts = {"interrupted": 1, "not found": start - 6, "upgrade": 5}
    for n in range(start, total + 1):
        if run.stop.is_set():
            break
        what = "upgrade" if n % 9 == 0 else "not found"
        if n > start:
            counts[what] += 1
        run.say(f"{n} of {total} songs: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())), n, total)
        run.note(f"song {n}: {what}")
        time.sleep(0.5)


@app.get("/demo/run", include_in_schema=False)
def demo_run() -> dict:
    runs = app.state.worker.runs
    if old := runs.get("upgrade"):
        old.stop.set()
    run = Run(schedule.BY_NAME["upgrade"], settings, Vault.from_env(settings.data_dir, {}), "schedule")
    runs["upgrade"] = run
    threading.Thread(target=upgrade, args=(run,), daemon=True).start()
    return {"ok": True}


con = sqlite3.connect(WORK / "data" / "echolot.db")
con.row_factory = sqlite3.Row
(WORK / "cookie.txt").write_text(auth.create_session(con, auth.get_user(con, "rick"), 30))
con.close()
uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
