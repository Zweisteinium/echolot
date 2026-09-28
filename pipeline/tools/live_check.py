#!/usr/bin/env python3
"""Live check of search + matching rules on Soulseek (search only, nothing is downloaded).

Run in the sockseek container as abc: python3 /config/tools/live_check.py [missing-all] [lossy N] [flac N]
Waits for the Soulseek lock, then searches a sample of wanted songs exactly as the pipeline would
(music-sync.search_groups / write_search_csv / sockseek_cmd) and judges every result Sockseek offers, in its
download order, with library.identify and the length check (file name and folders only: tags exist only
after a download). Report: /config/logs/live-check.log, details /config/state/live-check.json."""
import collections, fcntl, importlib.util, json, pathlib, random, re, subprocess, sys, time

spec = importlib.util.spec_from_file_location("ms", "/config/scripts/music-sync.py")
ms = importlib.util.module_from_spec(spec); spec.loader.exec_module(ms)
L = ms.library
LOG, DETAIL = pathlib.Path("/config/logs/live-check.log"), ms.STATE / "live-check.json"
RESULT = re.compile(r"^\s*\[([^\]]*)\]\s+(.+?)\s*$")

def log(msg):
    with LOG.open("a", encoding="utf-8") as f: f.write(msg + "\n")

def judge(r, dur, path):
    path = path.split(") ", 1)[1] if path.startswith("(") else path      # "(5.40MB/s) user\\..."
    path = path.rsplit(" (nec:", 1)[0]                                    # "... (nec:Satisfied, prf:...)"
    parts = [p for p in path.split("\\") if p]
    stem, folders = pathlib.PurePath(parts[-1]).stem, parts[1:-1]           # parts[0] is the user
    tol = 3
    match, why = L.identify(r["artist"], r["title"], [], "", stem, folders, dur, r.get("length") or 0, tol)
    length = 0 if L.mix_cut(r["title"]) else r.get("length") or 0
    if match and dur and length and not L.same_length(dur, length): return "mismatch", why
    return match or "none", why

def main():
    args = sys.argv[1:]
    n_lossy = int(args[args.index("lossy") + 1]) if "lossy" in args else 60
    n_flac = int(args[args.index("flac") + 1]) if "flac" in args else 40
    src = ms.yaml.safe_load((ms.CONFIG / "sources.yml").read_text()) or {}
    wanted = list(ms.wanted_from_soulseek(src, fetch=False).values())
    cat, attempts = L.Catalog(), ms.read_json(ms.ATTEMPTS, {})
    missing, lossy, flac = [], [], []
    for it in wanted:
        h = cat.song(it)
        (missing if not h else flac if h[0].genuine else lossy).append(it)
    random.seed(7)
    sample = [dict(it, kind="missing", tries=(attempts.get(it["key"]) or {}).get("n", 0)) for it in missing]
    sample += [dict(it, kind="lossy", tries=0) for it in random.sample(lossy, min(n_lossy, len(lossy)))]
    sample += [dict(it, kind="flac", tries=0) for it in random.sample(flac, min(n_flac, len(flac)))]
    log(f"=== live check {time.strftime('%F %T')}: {len(missing)} missing, {n_lossy} lossy, {n_flac} flac songs; waiting for the lock")
    with (ms.STATE / "music-sync.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        log(f"lock at {time.strftime('%T')}")
        details = []
        for suffix, extra, loosen, rows in ms.search_groups(sample):
            csvp = ms.STATE / f"live-check{suffix}.csv"
            ms.write_search_csv(rows, csvp, loosen)
            out = subprocess.run(ms.sockseek_cmd(str(csvp), ["--no-skip-existing", "--search-timeout", "8000",
                                                             "--print", "results-full", *extra]),
                                 text=True, capture_output=True).stdout
            by_query, cur = collections.defaultdict(list), None
            for line in out.splitlines():
                if line.startswith("Results for "):
                    cur = re.sub(r"(?: \(\d+s\))?:$", "", line[len("Results for "):].strip()); continue
                m = RESULT.match(line)
                if m and cur is not None:
                    secs = re.search(r"(-?\d+)s", m.group(1))
                    by_query[cur].append((int(secs.group(1)) if secs and int(secs.group(1)) > 0 else 0, m.group(2)))
            for r in rows:
                a, t, _ = ms.search_terms(r, loosen)
                res = by_query.get(f"{a} - {t}", [])
                verdicts = [(*judge(r, d, p), d, p) for d, p in res]
                details.append({"kind": r["kind"], "tries": r["tries"], "group": suffix or "strict", "artist": r["artist"],
                                "title": r["title"], "length": r.get("length"), "results": len(res),
                                "first": verdicts[0][0] if verdicts else "no results",
                                "verdicts": [{"v": v, "why": w[:160], "s": d, "path": p} for v, w, d, p in verdicts[:15]]})
            log(f"group {suffix or 'strict'}: {len(rows)} songs searched")
    DETAIL.write_text(json.dumps(details, ensure_ascii=False, indent=1))
    for kind in ("lossy", "flac", "missing"):
        ds = [d for d in details if d["kind"] == kind]
        firsts = collections.Counter(d["first"] for d in ds)
        any_exact = sum(1 for d in ds if any(v["v"] == "exact" for v in d["verdicts"]))
        log(f"{kind}: {len(ds)} songs | first result (what would be downloaded): {dict(firsts)} | songs with an exact result: {any_exact}")
    log("first results that are not exact (would be downloaded and rejected, or filed for review):")
    for d in details:
        if d["first"] not in ("exact", "no results"):
            v = d["verdicts"][0]
            log(f"  [{d['kind']}/{d['group']}] {d['artist']} - {d['title']} ({d['length']}s) <= {v['v']}: {v['path']} ({v['s']}s) | {v['why']}")
    log("missing songs with an exact or probable result somewhere:")
    for d in details:
        if d["kind"] == "missing":
            good = [v for v in d["verdicts"] if v["v"] in ("exact", "probable")]
            if good: log(f"  {d['artist']} - {d['title']} ({d['length']}s): {good[0]['v']} {good[0]['path']} ({good[0]['s']}s)")
    log(f"=== done {time.strftime('%T')}")

if __name__ == "__main__":
    main()
