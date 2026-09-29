"""Compare the matching rules of the working copy with another version on real data (read-only).

  uv run python tools/rules_check.py [git ref, default origin/main] [database, default /opt/echolot/data/echolot.db]

  A  every wanted song against its own library file (identify on name and folder, prejudge as a search
     result): both versions must accept it
  B  other songs of the same artist: must never be exact
  C  every logged download with its found name (events): the verdict of both versions
Differences are listed; run it before deploying a change to src/echolot/rules.py.
"""

import collections
import importlib.util
import itertools
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(ref: str):
    text = subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:src/echolot/rules.py"],
                          check=True, capture_output=True, text=True).stdout  # fmt: skip
    path = Path(tempfile.mkdtemp()) / "rules_old.py"
    path.write_text(text)
    spec = importlib.util.spec_from_file_location("rules_old", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    ref = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    dbfile = sys.argv[2] if len(sys.argv) > 2 else "/opt/echolot/data/echolot.db"
    sys.path.insert(0, str(ROOT / "src"))
    from echolot import rules as new

    old = load(ref)
    con = sqlite3.connect(f"file:{dbfile}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    diffs: list[str] = []

    own = con.execute("SELECT s.artist, s.title, s.length, f.path, f.duration FROM wanted s "
                      "JOIN files f ON f.path = s.file WHERE s.service = 'spotify'").fetchall()  # fmt: skip
    counts: collections.Counter = collections.Counter()
    for r in own:
        folder, name = r["path"].split("/", 1)
        stem = name.rsplit(".", 1)[0]
        for label, m in (("old", old), ("new", new)):
            v = m.identify(
                r["artist"], r["title"], [], "", stem, [folder], r["duration"], r["length"], 3
            )[0]
            p = m.prejudge(
                r["artist"], r["title"], f"Music\\{folder}\\{name}", r["duration"], r["length"]
            )[0]
            counts[(label, "identify", v)] += 1
            counts[(label, "prejudge", p)] += 1
        a = old.identify(
            r["artist"], r["title"], [], "", stem, [folder], r["duration"], r["length"], 3
        )[0]
        b = new.identify(
            r["artist"], r["title"], [], "", stem, [folder], r["duration"], r["length"], 3
        )[0]
        if a != b:
            diffs.append(f"A {r['artist']} - {r['title']} <- {r['path']}: {a} -> {b}")
    print(f"A  {len(own)} own files:", dict(sorted(counts.items(), key=str)))

    by_folder = collections.defaultdict(list)
    for r in own:
        by_folder[r["path"].split("/")[0]].append(r)
    pairs = wrong = 0
    for folder, rows in by_folder.items():
        for a, b in itertools.pairwise(rows):
            if new.title_key(a["title"]) == new.title_key(b["title"]):
                continue
            pairs += 1
            stem = b["path"].split("/", 1)[1].rsplit(".", 1)[0]
            if (
                new.identify(
                    a["artist"], a["title"], [], "", stem, [folder], b["duration"], a["length"], 3
                )[0]
                == "exact"
            ):
                wrong += 1
                diffs.append(f"B exact for another song: {a['title']} <- {b['path']}")
    print(f"B  {pairs} pairs of other songs of the same artist: {wrong} exact")

    events = con.execute("SELECT artist, title, found, file_name, seconds, wanted_seconds, source FROM events "
                         "WHERE found IS NOT NULL AND artist IS NOT NULL").fetchall()  # fmt: skip
    changed = 0
    for e in events:
        args = (e["artist"], e["title"], [], e["found"] or "", e["file_name"] or "", (), e["seconds"] or 0,
                e["wanted_seconds"] or 0, 3 if e["source"] == "soulseek" else 6)  # fmt: skip
        a, b = old.identify(*args)[0], new.identify(*args)[0]
        if a != b:
            changed += 1
            diffs.append(f"C {e['artist']} - {e['title']} / found {e['found']!r}: {a} -> {b}")
    print(f"C  {len(events)} logged downloads: {changed} verdicts changed")
    for d in diffs[:60]:
        print("  ", d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
