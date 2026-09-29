"""Take over the music-sync pipeline's state once, when Echolot starts running the jobs itself
(`echolot migrate-pipeline <pipeline config dir>`): its lists and songs with their history, download
attempts, FLAC upgrade attempts, SoundCloud downloads, review decisions, probe songs, list covers and
the last run of each job; secrets from its .env on standard input (never printed). The jobs stay
paused until `echolot jobs resume`.
"""

import csv
import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any

from echolot import db, options, pipeline, playlists, schedule, sources, spotify
from echolot.vault import Vault

JOB_NAMES = {"sync": "sync", "sweep": "sweep", "upgrade": "upgrade", "probe": "probe",
             "soundcloud": "soundcloud", "fallback": "fallback", "playlists": "library"}  # fmt: skip


def _json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def run(con: sqlite3.Connection, root: Path, music: Path, env: dict[str, str], vault: Vault) -> list[str]:
    """Import everything; returns a report line per part."""
    state = root / "state"
    report = []
    if not con.execute("SELECT 1 FROM sources").fetchone():  # lists and schedule not taken over yet
        with con:
            if (root / "sources.yml").exists():
                sources.replace_rows(con, sources.parse((root / "sources.yml").read_text(encoding="utf-8")))
            try:
                import yaml

                old = yaml.safe_load((root / "schedule.yml").read_text(encoding="utf-8")) or {}
            except (OSError, ValueError):
                old = {}
            if isinstance(old, dict):
                options.put_raw(con, schedule.SECTION, {j.name: schedule._rule(old.get(j.name, j.default), j)
                                                        for j in schedule.JOBS})  # fmt: skip
        report.append(f"took over {len(sources.lists(con))} lists and the schedule")
    srcs = sources.lists(con)
    report.append(pipeline.import_state(con, root, srcs))
    with con:
        options.update(con, options.Jobs, paused=True)
        con.execute("UPDATE songs SET artists = coalesce(artists, json_array(artist)) WHERE service = 'spotify'")

        # history of every list; songs that left their list keep their last known metadata
        history, extra = 0, 0
        sc_state = _json(state / "soundcloud-tracks.json", {})
        for s in srcs:
            prefix = "spotify" if s.service == "spotify" else "soundcloud"
            for sid, h in _json(state / f"{prefix}-{sources.slug(s.name)}-history.json", {}).items():
                key = f"{prefix}:{sid}"
                if not con.execute("SELECT 1 FROM songs WHERE key = ?", (key,)).fetchone():
                    meta = h if prefix == "spotify" else sc_state.get(sid) or {}
                    if not (meta.get("artist") and meta.get("title")):
                        continue
                    length = float(meta.get("length") or meta.get("duration") or 0)
                    con.execute(
                        "INSERT INTO songs (key, service, artist, title, album, length, artists, stem) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (key, s.service, meta["artist"], meta["title"], meta.get("album") or "", length,
                         json.dumps(meta.get("artists") or [meta["artist"]]), meta.get("stem")),
                    )  # fmt: skip
                    extra += 1
                con.execute(
                    "INSERT OR REPLACE INTO list_history (list_key, song_key, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                    (s.key, key, h.get("first_seen") or "", h.get("last_seen") or ""),
                )
                history += 1
        today = datetime.date.today().isoformat()  # what the lists have now counts as seen, file or not
        con.execute("INSERT OR IGNORE INTO list_history (list_key, song_key, first_seen, last_seen) "
                    "SELECT list_key, song_key, ?, ? FROM list_songs", (today, today))  # fmt: skip
        report.append(f"{history} history entries, {extra} songs that left their lists")

        # SoundCloud: downloaded once, never again
        archive = root / "state" / "soundcloud-archive.txt"
        ids = (
            {line.split()[-1] for line in archive.read_text().splitlines() if line.strip()}
            if archive.exists()
            else set()
        )
        ids |= {sid for sid, t in sc_state.items() if t.get("stem")}
        con.executemany("UPDATE songs SET archived = 1 WHERE key = ?", [(f"soundcloud:{i}",) for i in ids])
        report.append(f"{len(ids)} SoundCloud songs downloaded before")

        upgrades = _json(state / "upgrade-attempts.json", {})
        con.executemany(
            "INSERT OR REPLACE INTO upgrades (song_key, tries, last_try) VALUES (?, ?, ?)",
            [(k.replace("spotify:track:", "spotify:"), v.get("n", 0), v.get("last", 0)) for k, v in upgrades.items()],
        )
        blocked = _json(state / "review-blocked.json", {})
        con.executemany(
            "INSERT OR IGNORE INTO blocked (song_key, name) VALUES (?, ?)",
            [(k.replace("spotify:track:", "spotify:"), n) for k, names in blocked.items() for n in names],
        )
        report.append(f"{len(upgrades)} upgrade attempts, {sum(len(v) for v in blocked.values())} blocked downloads")

        # covers: the playlist files have them already
        meta = _json(state / "playlist-meta.json", {})
        for s in srcs:
            if url := (meta.get(s.name) or {}).get("fetched_url"):
                con.execute("UPDATE lists SET cover_url = ?, cover_file = ? WHERE key = ?", (url, url, s.key))
            elif s.name == "Spotify Liked Songs":
                con.execute("UPDATE lists SET cover_url = ? WHERE key = ?", (spotify.LIKED_SONGS_IMAGE, s.key))
        existing = [p.name for p in (music / "playlists").iterdir() if playlists.OURS.match(p.name)] \
            if (music / "playlists").is_dir() else []  # fmt: skip
        db.set_meta(con, "playlist_files", json.dumps(sorted(existing)))

        # review decisions
        done = _json(state / "review-done.json", {})
        try:
            import yaml

            decisions = (yaml.safe_load((root / "review.yml").read_text(encoding="utf-8")) or {}).get("decisions") or []
        except (OSError, ValueError):
            decisions = []
        pending = 0
        for d in decisions:
            if not isinstance(d, dict) or not d.get("id"):
                continue
            ts, _, path = str(d["id"]).partition(" ")
            row = con.execute("SELECT id FROM events WHERE ts = ? AND path = ?", (ts, path)).fetchone()
            applied = done.get(str(d["id"]))
            pending += applied is None
            con.execute(
                "INSERT OR REPLACE INTO review_decisions (id, event_id, decision, decided, applied, result) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (d["id"], row[0] if row else None, d.get("decision") or "", d.get("at") or ts,
                 (applied or {}).get("at"), (applied or {}).get("result")),
            )  # fmt: skip
        report.append(f"{len(decisions)} review decisions ({pending} not applied yet: Echolot applies them)")

        probe = root / "probe.csv"
        if probe.exists():
            with probe.open(newline="", encoding="utf-8") as f:
                rows = [(r["Artist"], r["Title"], r.get("Kind") or "") for r in csv.DictReader(f)
                        if r.get("Artist") and r.get("Title")]  # fmt: skip
            con.executemany("INSERT OR REPLACE INTO probe_songs (artist, title, kind) VALUES (?, ?, ?)", rows)
            report.append(f"{len(rows)} probe songs")

        for job, name in JOB_NAMES.items():
            try:
                ts = float((state / f"last-{job}").read_text().strip())
            except (OSError, ValueError):
                continue
            started = datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")
            con.execute(
                "INSERT INTO jobs (name, started, finished, ok, message) VALUES (?, ?, ?, 1, 'run by the pipeline') "
                "ON CONFLICT (name) DO UPDATE SET started = excluded.started, finished = excluded.finished",
                (name, started, started),
            )

        # secrets and account names
        secrets = {"SPOTIFY_SECRET": spotify.SECRET, "SPOTIFY_REFRESH": spotify.REFRESH,
                   "SC_TOKEN": "soundcloud.token", "SLSK_PASS": "soulseek.password"}  # fmt: skip
        rotated = state / "spotify-refresh-token"
        if rotated.exists():
            env["SPOTIFY_REFRESH"] = rotated.read_text().strip()
        stored = []
        for var, name in secrets.items():
            if env.get(var):
                vault.set(con, name, env[var])
                stored.append(name)
        if env.get("SPOTIFY_ID"):
            options.update(con, options.Spotify, client_id=env["SPOTIFY_ID"])
        if env.get("SLSK_USER"):
            options.update(con, options.Soulseek, user=env["SLSK_USER"])
        report.append(f"secrets stored: {', '.join(stored) or 'none'}")
        db.set_meta(con, "pipeline_migrated", datetime.datetime.now().isoformat(timespec="seconds"))
    return report


def parse_env(text: str) -> dict[str, str]:
    """KEY=value lines of a .env file (quotes removed)."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out
