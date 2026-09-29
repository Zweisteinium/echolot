"""The pipeline's config files, written by Echolot from its database: sources.yml and schedule.yml
(review.yml: review.py). The pipeline keeps running unchanged on them until it moves into Echolot.

On its first start Echolot takes both files over once (the originals are kept in config_versions).
From then on the database is what counts: after every change and at every start the files are
written again (atomically, only when their text changes). A file someone edited by hand in between
is kept as a version before it is replaced. Until the takeover worked, nothing is written, so a
file Echolot could not read stays as it is.
"""

import hashlib
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from echolot import db, options, schedule, sources
from echolot.config import Settings

log = logging.getLogger(__name__)

TAKEN_OVER = "pipeline_config_taken_over"  # meta: the pipeline's files are in the database


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _keep(con: sqlite3.Connection, name: str, text: str, note: str) -> None:
    con.execute(
        "INSERT INTO config_versions (name, ts, text, note) VALUES (?, ?, ?, ?)",
        (name, datetime.now().isoformat(timespec="seconds"), text, note),
    )


def take_over(con: sqlite3.Connection, root: Path) -> str:
    """Read the pipeline's sources.yml and schedule.yml into the database (once)."""
    if db.get_meta(con, TAKEN_OVER):
        return "already taken over"
    src = root / sources.FILE
    text = src.read_text(encoding="utf-8") if src.exists() else ""
    data = sources.parse(text) if text else {}  # ConfigError: nothing is taken over
    sched = root / schedule.FILE
    sched_text = sched.read_text(encoding="utf-8") if sched.exists() else ""
    with con:
        sources.replace_rows(con, data)
        options.put_raw(con, schedule.SECTION, schedule.read_file(root))
        for name, old in ((sources.FILE, text), (schedule.FILE, sched_text)):
            if old:
                _keep(con, name, old, "Echolot took the file over")
                db.set_meta(con, f"written:{name}", _hash(old))
        db.set_meta(con, TAKEN_OVER, datetime.now().isoformat(timespec="seconds"))
    n = con.execute("SELECT count(*) FROM sources").fetchone()[0]
    return f"took over {n} lists and the schedule from the pipeline's files"


def files(con: sqlite3.Connection) -> dict[str, str]:
    """The files as they should be now."""
    return {
        sources.FILE: sources.render(con),
        schedule.FILE: schedule.render(schedule.rules(con)),
    }


def write(con: sqlite3.Connection, settings: Settings) -> list[str]:
    """Write the files that differ from the database; returns their names."""
    out = settings.out_dir
    if out is None or not db.get_meta(con, TAKEN_OVER):
        return []
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in files(con).items():
        path = out / name
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            continue
        with con:
            if current is not None and _hash(current) != db.get_meta(con, f"written:{name}"):
                _keep(con, name, current, "Echolot replaced an edit made outside it")
                log.warning("%s was edited outside Echolot; replaced (kept as a version)", path)
            db.set_meta(con, f"written:{name}", _hash(text))
        sources.write_atomic(path, text)
        written.append(name)
    return written


def start(con: sqlite3.Connection, settings: Settings) -> None:
    """At startup: take the files over if not done yet, then write them."""
    if settings.pipeline_dir is None:
        return
    if not db.get_meta(con, TAKEN_OVER):
        try:
            log.info("%s", take_over(con, settings.pipeline_dir))
        except (sources.ConfigError, OSError) as err:
            log.error("Can't take over the pipeline's config files, not writing them: %s", err)
            return
    try:
        if written := write(con, settings):
            log.info("wrote %s", ", ".join(written))
    except OSError as err:
        log.error("Can't write the pipeline's config files: %s", err)
