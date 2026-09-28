#!/usr/bin/env python3
"""Check the matching rules of library.py against the real library and history (read-only).

Runs on the host: python3 rules_check.py <new library.py> [<old library.py>]
  A1  library lookup (Catalog.song) of every wanted Spotify song: old and new rules agree
  A2  every wanted song against its own library file (tags, file name, folder): identify says exact/probable
  A3  negative pairs: the file of another title by the same artist, and of the same title by another
      artist, must never match
  B   every logged download with its found name (downloads.jsonl): verdict of the old and the new rules
Paths: library /media/hdd/medialib/audio/music/tracks, pipeline state /opt/sockseek/config (never written;
the catalog cache goes to a temporary copy)."""
import collections, importlib.machinery, importlib.util, json, pathlib, random, sys, tempfile

MUSIC = pathlib.Path("/media/hdd/medialib/audio/music")
CONFIG = pathlib.Path("/opt/sockseek/config")

def load(path, name):
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, str(path)))  # also *.bak-*
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    tmp = pathlib.Path(tempfile.mkdtemp())
    cache = json.loads((CONFIG / "state/library-cache.json").read_text())
    (tmp / "cache.json").write_text(json.dumps({k.replace("/music/", f"{MUSIC}/", 1): v for k, v in cache.items()}))
    m.TRACKS, m.STATE, m.CACHE = MUSIC / "tracks", CONFIG / "state", tmp / "cache.json"
    m.LOSSY_LIST, m.LINKS = CONFIG / "state/lossy-sourced.json", CONFIG / "state/song-links.json"
    m.LOCKFILE = tmp / "library.lock"
    return m

def songs():
    out = {}
    for f in (CONFIG / "state").glob("spotify-spotify-*.json"):
        if f.stem.endswith("-history"): continue
        for it in json.loads(f.read_text()): out.setdefault("spotify:" + it["id"], {**it, "key": "spotify:" + it["id"]})
    return list(out.values())

def tags(path):
    from mutagen import File
    try:
        t = File(str(path), easy=True).tags or {}
        return [v for k in ("artist", "albumartist") for v in (t.get(k) or []) if v], ((t.get("title") or [""])[0])
    except Exception:
        return [], ""

def new_verdict(lib, artist, title, tag_artists, tag_title, file_name, folders, dur, length, tol=3):
    return lib.identify(artist, title, tag_artists, tag_title, file_name, folders, dur, length, tol)

def old_verdict(lib, artist, title, tag_artists, tag_title, file_name, folders, dur, length, tol=3):
    if hasattr(lib, "identify"): return new_verdict(lib, artist, title, tag_artists, tag_title, file_name, folders, dur, length, tol)
    close = bool(dur and length and abs(dur - float(length)) <= 3)
    ok, why = lib.identity_ok(artist, title, tag_artists, tag_title, file_name, folders, True, close)
    if ok: return "exact", why
    if hasattr(lib, "probable_ok"):
        ok, why = lib.probable_ok(artist, title, tag_artists, tag_title, file_name, folders, dur, length, tol)
        if ok: return "probable", why
    return None, why

def main():
    new = load(sys.argv[1], "new_library")
    old = load(sys.argv[2], "old_library") if len(sys.argv) > 2 else None
    wanted = songs()
    print(f"{len(wanted)} wanted Spotify songs")
    cat_new = new.Catalog()
    print(f"{len(cat_new.entries)} library files")

    # A1 library lookup
    if old:
        cat_old = old.Catalog()
        diff = [(it["artist"], it["title"], [str(e.path.name) for e in cat_old.song(it)] if hasattr(cat_old, "song") else None,
                 [str(e.path.name) for e in cat_new.song(it)]) for it in wanted]
        diff = [d for d in diff if d[2] is not None and d[2] != d[3]]
        print(f"\nA1 library lookup: {len(wanted) - len(diff)} of {len(wanted)} identical" + (f", {len(diff)} differ:" if diff else ""))
        for d in diff[:30]: print("   ", d)
    hits = {it["key"]: cat_new.song(it) for it in wanted}
    have = [(it, hits[it["key"]][0]) for it in wanted if hits[it["key"]]]
    print(f"   {len(have)} songs in the library, {len(wanted) - len(have)} missing")
    shared = collections.defaultdict(list)
    for it, e in have: shared[str(e.path)].append(f"{it['artist']} - {it['title']}")
    multi = {p: s for p, s in shared.items() if len(set(s)) > 1}
    print(f"   {len(multi)} files stand for more than one differently named song:")
    for p, s in list(multi.items())[:40]: print(f"      {pathlib.Path(p).relative_to(MUSIC / 'tracks')}: {sorted(set(s))}")

    # A2 positives
    res = collections.Counter(); misses = []
    for it, e in have:
        ta, tt = tags(e.path)
        v = new_verdict(new, it["artist"], it["title"], ta, tt, e.path.stem, [e.path.parent.name], e.dur, it["length"])
        res[v[0]] += 1
        if v[0] is None: misses.append((it["artist"], it["title"], ta[:2], tt, e.path.name, v[1][:120]))
    print(f"\nA2 own library file recognised: {dict(res)}")
    for m in misses[:40]: print("   miss:", m)

    # A3 negatives
    random.seed(1)
    by_artist = collections.defaultdict(list)
    for e in cat_new.entries: by_artist[e.dir].append(e)
    fp, n = [], 0
    for it, e in have:
        same_artist = [x for x in by_artist[e.dir] if x.tkey != e.tkey]
        others = [x for x in cat_new.entries if x.tkey == e.tkey and not (x.akeys & e.akeys)]
        for x in random.sample(same_artist, min(3, len(same_artist))) + others[:3]:
            ta, tt = tags(x.path); n += 1
            v = new_verdict(new, it["artist"], it["title"], ta, tt, x.path.stem, [x.path.parent.name], x.dur, it["length"])
            if v[0]: fp.append((it["artist"], it["title"], x.path.relative_to(MUSIC / "tracks").as_posix(), v[0], v[1][:100]))
    print(f"\nA3 negative pairs: {n} checked, {len(fp)} matched (should be 0 or explained)")
    for f in fp[:40]: print("   ", f)

    # B event replay
    ev = [json.loads(l) for l in open(CONFIG / "logs/downloads.jsonl")]
    rows = [e for e in ev if e.get("found") is not None and e["action"] in ("new", "upgrade", "wrong-song", "mismatch", "duplicate")
            and e.get("source") in ("soulseek", "youtube", "soundcloud-search")]
    changes = collections.Counter(); listed = []
    for e in rows:
        tol = 3 if e["source"] == "soulseek" else 6
        length = e.get("wanted_seconds") or next((it["length"] for it in wanted if it["key"] in (e.get("ids") or [""])[0].replace("spotify:track:", "spotify:")), 0) if e.get("ids") else 0
        args = (e["artist"], e["title"], [], e.get("found") if e.get("found") != e.get("file_name") else "", e.get("file_name") or "", e.get("folders") or [], e.get("seconds") or 0, length, tol)
        n_v = new_verdict(new, *args)[0]
        o_v = old_verdict(old, *args)[0] if old else None
        changes[(e["action"], o_v, n_v)] += 1
        if o_v != n_v: listed.append((e["action"], o_v, "->", n_v, e["artist"], e["title"], "<=", e.get("found")))
    print(f"\nB {len(rows)} logged downloads with found names (action, old verdict, new verdict): (tags are gone, file names only)")
    for k, v in sorted(changes.items(), key=lambda x: -x[1]): print(f"   {v:4} {k}")
    for l in listed[:40]: print("   changed:", l)

if __name__ == "__main__":
    main()
