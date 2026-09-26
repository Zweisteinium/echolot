#!/usr/bin/env python3
"""tick: start the music-sync jobs that are due. cron runs this every minute in both containers;
each container starts only the jobs of its ROLE (main: VPN/Soulseek, fallback: home IP).

How often each job runs is set in /config/schedule.yml (minutes, `off` = never), which Echolot's
settings page edits; changes apply at the next tick, no crontab reload needed. A job is due once its
interval has passed since it was last started (state/last-<job>; without that, the last start logged
in logs/<job>.log). A job that finds its previous run still active exits at once (music-sync.py
lock), so short intervals are safe. `tick.py --dry-run` shows what is due without starting it."""
import datetime, os, pathlib, re, subprocess, sys, time
import yaml

CONFIG = pathlib.Path("/config"); STATE = CONFIG / "state"; LOGS = CONFIG / "logs"
ROLES = {"main": ["sync", "upgrade", "playlists"], "fallback": ["soundcloud", "fallback"]}
DEFAULTS = {"sync": 30, "soundcloud": 30, "fallback": 120, "playlists": 10, "upgrade": 10080}
MINIMUM = {"sync": 10, "soundcloud": 15, "fallback": 60, "playlists": 5, "upgrade": 1440}

def intervals():
    """Minutes per job (None = off); invalid values fall back to the default, too small ones to the minimum."""
    try: conf = yaml.safe_load((CONFIG / "schedule.yml").read_text(encoding="utf-8")) or {}
    except Exception: conf = {}
    out = {}
    for job, default in DEFAULTS.items():
        v = conf.get(job, default) if isinstance(conf, dict) else default
        if v is False or v == 0: out[job] = None
        elif isinstance(v, (int, float)) and not isinstance(v, bool): out[job] = max(int(v), MINIMUM[job])
        else: out[job] = default
    return out

def last_start(job):
    """Unix time the job was last started: stamp file, else the last start line in its log, else None."""
    try: return float((STATE / f"last-{job}").read_text().strip())
    except (OSError, ValueError): pass
    try:
        with open(LOGS / f"{job}.log", "rb") as f:
            f.seek(max(f.seek(0, os.SEEK_END) - 256_000, 0))
            starts = re.findall(rf"^(\d{{4}}-\d\d-\d\d \d\d:\d\d:\d\d) === music-sync {job} start$",
                                f.read().decode("utf-8", "replace"), re.M)
        if starts: return datetime.datetime.fromisoformat(starts[-1]).timestamp()
    except OSError: pass
    return None

def main():
    dry = "--dry-run" in sys.argv
    role, now = os.environ.get("ROLE", ""), time.time()
    for job in ROLES.get(role, []):
        minutes = intervals()[job]
        last = last_start(job)
        if minutes is None:
            if dry: print(f"{job}: off")
            continue
        if last is None:          # never ran: first run one interval from now
            if not dry: (STATE / f"last-{job}").write_text(f"{now:.0f}\n")
            if dry: print(f"{job}: no earlier run, first run in {minutes} min")
            continue
        due = now - last >= minutes * 60 - 30      # ticks come every 60 s: allow a little early
        if dry:
            left = (last + minutes * 60 - now) / 60
            print(f"{job}: every {minutes} min, last start {datetime.datetime.fromtimestamp(last):%Y-%m-%d %H:%M}, "
                  + ("due now" if due else f"next in {left:.0f} min"))
            continue
        if not due: continue
        (STATE / f"last-{job}").write_text(f"{now:.0f}\n")
        with open(LOGS / "tick.log", "a", encoding="utf-8") as log:
            log.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {role}: start {job} (every {minutes} min)\n")
        subprocess.Popen(["/config/scripts/cron.sh", job], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

if __name__ == "__main__":
    main()
