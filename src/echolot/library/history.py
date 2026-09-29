"""Metrics over time, for dashboards (the JSON API, Prometheus /metrics, or Grafana reading the database).

The refresh job stores a snapshot of every metric at most once per SNAPSHOT_SECONDS in the `snapshots`
table: one row per (ts, metric, key), where key is the label value (a quality tier, a format, a list,
...; '' for none). Snapshots older than KEEP_HOURLY_DAYS are thinned to the first one of each day.
Downloads and availability probes are time series already (the events and probes tables).
"""

import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from echolot.library.catalog import QUALITY

SNAPSHOT_SECONDS = 3600
KEEP_HOURLY_DAYS = 90

# metric -> (label name, help); the label name is None for a single value
METRICS: dict[str, tuple[str | None, str]] = {
    "library_files": (None, "Audio files in the library"),
    "library_bytes": (None, "Size of the library in bytes"),
    "library_files_by_quality": ("quality", "Library files per quality tier"),
    "library_files_by_format": ("format", "Library files per file format"),
    "library_bytes_by_format": ("format", "Library bytes per file format"),
    "songs_wanted": ("service", "Songs in the followed lists"),
    "songs_in_library": ("service", "Wanted songs that are in the library"),
    "songs_missing": ("service", "Wanted songs that are not in the library"),
    "songs_missing_by_reason": (
        "reason",
        "Missing songs: unavailable (greyed out, DRM), not_found (searched), waiting (not searched yet)",
    ),
    "songs_not_found_by_tries": ("tries", "Missing songs by searches that did not find them (any reason)"),
    "songs_by_quality": ("quality", "Wanted songs by the quality of their best library copy"),
    "songs_by_format": ("format", "Wanted songs by the format of their best library copy"),
    "list_songs": ("list", "Songs per list"),
    "list_in_library": ("list", "Songs per list that are in the library"),
    "list_lossless": ("list", "Songs per list with a genuine lossless copy"),
}


def _format(path: str) -> str:
    return PurePosixPath(path).suffix.lstrip(".").lower() or "none"


def _tries(n: int) -> str:
    return "1" if n <= 1 else "2-3" if n <= 3 else "4+"


def collect(con: sqlite3.Connection) -> list[tuple[str, str, float]]:
    """(metric, key, value) for the current state."""
    rows: list[tuple[str, str, float]] = []
    files = con.execute("SELECT path, size, quality FROM files").fetchall()
    rows += [("library_files", "", len(files)), ("library_bytes", "", sum(f[1] for f in files))]
    tiers = Counter(f[2] or "unknown" for f in files)
    rows += [("library_files_by_quality", q, tiers.get(q, 0)) for q, _ in QUALITY]
    formats, fbytes = Counter(), Counter()
    for path, size, _ in files:
        formats[_format(path)] += 1
        fbytes[_format(path)] += size
    rows += [("library_files_by_format", k, v) for k, v in sorted(formats.items())]
    rows += [("library_bytes_by_format", k, v) for k, v in sorted(fbytes.items())]

    songs = con.execute(
        "SELECT s.service, s.file, s.unavailable, f.quality, a.tries FROM wanted s "
        "LEFT JOIN files f ON f.path = s.file LEFT JOIN attempts a ON a.song_key = s.key"
    ).fetchall()
    wanted, have, reasons, tries, quality, fmt = (Counter() for _ in range(6))
    for service, file, unavailable, q, n in songs:
        wanted[service] += 1
        if file:
            have[service] += 1
            quality[q or "unknown"] += 1
            fmt[_format(file)] += 1
        else:
            reasons["unavailable" if unavailable else "not_found" if n else "waiting"] += 1
            if n:
                tries[_tries(n)] += 1
    for service in sorted(wanted):
        rows += [
            ("songs_wanted", service, wanted[service]),
            ("songs_in_library", service, have[service]),
            ("songs_missing", service, wanted[service] - have[service]),
        ]
    rows += [("songs_missing_by_reason", r, reasons[r]) for r in ("not_found", "unavailable", "waiting")]
    rows += [("songs_not_found_by_tries", t, tries[t]) for t in ("1", "2-3", "4+")]
    rows += [("songs_by_quality", q, quality.get(q, 0)) for q, _ in QUALITY]
    rows += [("songs_by_format", k, v) for k, v in sorted(fmt.items())]

    for key, total, in_library, lossless in con.execute(
        "SELECT ls.list_key, count(*), count(s.file), sum(f.quality = 'lossless') FROM list_songs ls "
        "JOIN songs s ON s.key = ls.song_key LEFT JOIN files f ON f.path = s.file GROUP BY ls.list_key"
    ):
        rows += [
            ("list_songs", key, total),
            ("list_in_library", key, in_library),
            ("list_lossless", key, lossless or 0),
        ]
    return rows


def snapshot(con: sqlite3.Connection, now: datetime | None = None, force: bool = False) -> bool:
    """Store a snapshot unless the last one is younger than SNAPSHOT_SECONDS (minus a minute of slack,
    so a 5-min refresh does not drift to every 65 min). Returns True if one was stored."""
    now = now or datetime.now()
    last = con.execute("SELECT max(ts) FROM snapshots").fetchone()[0]
    if not force and last and (now - datetime.fromisoformat(last)).total_seconds() < SNAPSHOT_SECONDS - 60:
        return False
    ts = now.isoformat(timespec="seconds")
    cutoff = (now - timedelta(days=KEEP_HOURLY_DAYS)).isoformat(timespec="seconds")
    with con:
        con.executemany(
            "INSERT OR REPLACE INTO snapshots (ts, metric, key, value) VALUES (?, ?, ?, ?)",
            [(ts, m, k, v) for m, k, v in collect(con)],
        )
        con.execute(
            "DELETE FROM snapshots WHERE ts < ? AND ts NOT IN "
            "(SELECT min(ts) FROM snapshots WHERE ts < ? GROUP BY substr(ts, 1, 10))",
            (cutoff, cutoff),
        )
    return True


def latest(con: sqlite3.Connection) -> tuple[str | None, dict[str, dict[str, float]]]:
    """The newest snapshot: (ts, {metric: {key: value}})."""
    ts = con.execute("SELECT max(ts) FROM snapshots").fetchone()[0]
    out: dict[str, dict[str, float]] = {}
    for metric, key, value in con.execute(
        "SELECT metric, key, value FROM snapshots WHERE ts = ? ORDER BY metric, key", (ts,)
    ):
        out.setdefault(metric, {})[key] = value
    return ts, out


def series(
    con: sqlite3.Connection, metric: str, key: str | None = None, since: str | None = None, until: str | None = None
) -> list[sqlite3.Row]:
    """Rows (ts, key, value) of one metric, oldest first."""
    where, args = ["metric = ?"], [metric]
    for clause, value in (("key = ?", key), ("ts >= ?", since), ("ts <= ?", until)):
        if value is not None:
            where.append(clause)
            args.append(value)
    return con.execute(
        f"SELECT ts, key, value FROM snapshots WHERE {' AND '.join(where)} ORDER BY ts, key", args
    ).fetchall()


def daily_events(con: sqlite3.Connection, days: int = 30) -> list[sqlite3.Row]:
    """Downloads per day, action, source and format (new, upgrade, wrong-song, ...)."""
    since = (datetime.now() - timedelta(days=days)).date().isoformat()
    return con.execute(
        "SELECT substr(ts, 1, 10) AS day, action, coalesce(source, '') AS source, "
        "coalesce(ext, '') AS format, count(*) AS count, coalesce(sum(bytes), 0) AS bytes "
        "FROM events WHERE ts >= ? GROUP BY day, action, source, format ORDER BY day, action",
        (since,),
    ).fetchall()


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def prometheus(con: sqlite3.Connection) -> str:
    """Current values in the Prometheus text format (Prometheus keeps the history)."""
    values: dict[str, dict[str, float]] = {}
    for metric, key, value in collect(con):
        values.setdefault(metric, {})[key] = value
    lines = []
    for metric, (label, help_text) in METRICS.items():
        name = f"echolot_{metric}"
        lines += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
        for key, value in values.get(metric, {}).items():
            labels = f'{{{label}="{_escape(key)}"}}' if label else ""
            lines.append(f"{name}{labels} {value:g}")
    lines += [
        "# HELP echolot_events_total Library events since the start (new, upgrade, wrong-song, ...)",
        "# TYPE echolot_events_total counter",
    ]
    for action, source, n in con.execute(
        "SELECT action, coalesce(source, ''), count(*) FROM events GROUP BY action, source ORDER BY 1, 2"
    ):
        lines.append(f'echolot_events_total{{action="{_escape(action)}",source="{_escape(source)}"}} {n}')
    lines += [
        "# HELP echolot_probe_users Soulseek users with the song at the last availability probe",
        "# TYPE echolot_probe_users gauge",
    ]
    for artist, title, kind, users, lossless in con.execute(
        "SELECT artist, title, kind, users, lossless_users FROM probes "
        "WHERE ts = (SELECT max(ts) FROM probes) ORDER BY artist, title"
    ):
        song = _escape(f"{artist} - {title}")
        lines.append(f'echolot_probe_users{{song="{song}",kind="{_escape(kind)}",lossless="false"}} {users}')
        lines.append(f'echolot_probe_users{{song="{song}",kind="{_escape(kind)}",lossless="true"}} {lossless}')
    refreshed = con.execute("SELECT finished FROM jobs WHERE name = 'library'").fetchone()
    if refreshed and refreshed[0]:
        lines += [
            "# HELP echolot_refresh_timestamp_seconds Last library scan",
            "# TYPE echolot_refresh_timestamp_seconds gauge",
            f"echolot_refresh_timestamp_seconds {datetime.fromisoformat(refreshed[0]).timestamp():.0f}",
        ]
    return "\n".join(lines) + "\n"
