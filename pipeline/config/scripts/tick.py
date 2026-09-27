#!/usr/bin/env python3
"""tick: start the music-sync jobs that are due. cron runs this every minute in both containers;
each container starts only the jobs of its ROLE (main: VPN/Soulseek, fallback: home IP).

When each job runs is set in /config/schedule.yml (Echolot's settings page edits it; changes apply at
the next tick, no crontab reload needed). Per job either
  <minutes>                              every N minutes since the last start
  {at: ["20:30", "sat,sun 15:00"]}       at fixed local times, optionally only on some weekdays
  off                                    never
The last start is kept in state/last-<job> (without it: the last start logged in logs/<job>.log).
Soulseek jobs share one lock: a job that is due while another one holds it waits (it starts at the
first tick after the other finished) instead of losing its turn.
`tick.py --dry-run` shows what is due without starting it."""
import datetime, fcntl, os, pathlib, re, subprocess, sys, time
import yaml

CONFIG = pathlib.Path("/config"); STATE = CONFIG / "state"; LOGS = CONFIG / "logs"
ROLES = {"main": ["sync", "sweep", "upgrade", "playlists", "probe"], "fallback": ["soundcloud", "fallback"]}
DEFAULTS = {"sync": 30, "sweep": {"at": ["20:00", "sat,sun 15:00"]}, "upgrade": {"at": ["14:00", "20:30"]},
            "playlists": 10, "probe": 60, "soundcloud": 30, "fallback": 120}
MINIMUM = {"sync": 10, "sweep": 360, "upgrade": 360, "playlists": 5, "probe": 30, "soundcloud": 15, "fallback": 60}
LOCKS = {"sync": "music-sync.lock", "sweep": "music-sync.lock", "upgrade": "music-sync.lock", "probe": "music-sync.lock",
         "fallback": "music-sync.lock", "soundcloud": "soundcloud.lock"}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
LATE = 6 * 3600          # a fixed-time run that could not start within 6 h (busy lock, downtime) is dropped

def parse_at(entries):
    """["20:30", "sat,sun 15:00"] -> [(weekday set or None, hour, minute)]; invalid entries are skipped."""
    out = []
    for e in entries if isinstance(entries, list) else [entries]:
        m = re.fullmatch(r"\s*(?:([a-z,\s]+?)\s+)?(\d{1,2}):(\d\d)\s*", str(e).lower())
        if not m or int(m.group(2)) > 23 or int(m.group(3)) > 59: continue
        days = None
        if m.group(1):
            days = {DAYS.index(d.strip()) for d in m.group(1).split(",") if d.strip() in DAYS}
            if not days: continue
        out.append((days, int(m.group(2)), int(m.group(3))))
    return out

def schedule():
    """job -> ("every", minutes) | ("at", [(days, h, m)]) | None (off); invalid values -> default."""
    try: conf = yaml.safe_load((CONFIG / "schedule.yml").read_text(encoding="utf-8")) or {}
    except Exception: conf = {}
    if not isinstance(conf, dict): conf = {}
    def read(job, v):
        if v is False or v == 0: return None
        if isinstance(v, (int, float)) and not isinstance(v, bool): return ("every", max(int(v), MINIMUM[job]))
        if isinstance(v, dict) and (at := parse_at(v.get("at") or [])): return ("at", at)
        return None if v is None else read(job, DEFAULTS[job]) if v is not DEFAULTS[job] else None
    return {job: read(job, conf.get(job, default)) for job, default in DEFAULTS.items()}

def last_point(at, now):
    """Most recent scheduled moment (unix time) at or before now."""
    today = datetime.datetime.fromtimestamp(now).replace(second=0, microsecond=0)
    best = None
    for back in range(8):
        day = today - datetime.timedelta(days=back)
        for days, h, m in at:
            if days is not None and day.weekday() not in days: continue
            t = day.replace(hour=h, minute=m).timestamp()
            if t <= now and (best is None or t > best): best = t
    return best

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

def lock_free(job):
    """True if the job's lock is free right now (checked, then released at once)."""
    if job not in LOCKS: return True
    try:
        with open(STATE / LOCKS[job], "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(f, fcntl.LOCK_UN)
        return True
    except BlockingIOError:
        return False

def main():
    dry = "--dry-run" in sys.argv
    role, now = os.environ.get("ROLE", ""), time.time()
    plan = schedule()
    for job in ROLES.get(role, []):
        rule, last = plan[job], last_start(job)
        if rule is None:
            if dry: print(f"{job}: off")
            continue
        if rule[0] == "every":
            if last is None:      # never ran: first run one interval from now
                if not dry: (STATE / f"last-{job}").write_text(f"{now:.0f}\n")
                if dry: print(f"{job}: no earlier run, first run in {rule[1]} min")
                continue
            due, what = now - last >= rule[1] * 60 - 30, f"every {rule[1]} min"   # ticks come every 60 s
            left = (last + rule[1] * 60 - now) / 60
        else:
            point = last_point(rule[1], now)
            due = point is not None and (last is None or last < point) and now - point < LATE
            what = "at " + ", ".join(("" if d is None else ",".join(DAYS[i] for i in sorted(d)) + " ") + f"{h:02d}:{m:02d}" for d, h, m in rule[1])
            nxt = min((p for p in (last_point(rule[1], now + s * 60) for s in range(1, 8 * 1440, 15)) if p and p > now), default=None)
            left = (nxt - now) / 60 if nxt else 0
        busy = due and not lock_free(job)
        if dry:
            since = f"last start {datetime.datetime.fromtimestamp(last):%Y-%m-%d %H:%M}" if last else "never ran"
            print(f"{job}: {what}, {since}, " + ("waiting for the lock" if busy else "due now" if due else f"next in {left:.0f} min"))
            continue
        if not due or busy: continue
        (STATE / f"last-{job}").write_text(f"{now:.0f}\n")
        with open(LOGS / "tick.log", "a", encoding="utf-8") as log:
            log.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {role}: start {job} ({what})\n")
        subprocess.Popen(["/config/scripts/cron.sh", job], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

if __name__ == "__main__":
    main()
