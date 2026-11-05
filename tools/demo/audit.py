"""How much of a real library the demo shows: each demo list against each list of a real Echolot database (read
only), so the demo never mirrors someone's playlist. Run before publishing a new demo:

    uv run python tools/demo/audit.py <work dir>/data/echolot.db /opt/echolot/data/echolot.db

Per demo list: the real list it shares most songs with (share of the demo list), and the longest run of songs in
the same order. It fails (exit 1) if a demo list shares more than SHARE with one real list, or RUN songs in a
row: songs from a real library are welcome, a copy of someone's playlist is not. Prints no song or list names of
the real database, only counts and the demo's own names."""

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

SHARE, RUN = 0.25, 3  # a list of well-known songs shares some with any big library; a copy shares most


def norm(artist: str, title: str) -> tuple[str, str]:
    """Artist and title compared loosely: letters and digits only, the title without its version part."""
    key = lambda s: "".join(ch for ch in s.lower() if ch.isalnum())  # noqa: E731
    return key(artist)[:8], key(title.split(" - ")[0].split(" (")[0].split(" [")[0])


def lists(path: Path) -> dict[str, list[tuple[str, str]]]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    rows = con.execute(
        "SELECT ls.list_key, s.artist, s.title FROM list_songs ls JOIN songs s ON s.key = ls.song_key "
        "ORDER BY ls.list_key, ls.position"
    )
    for key, artist, title in rows:
        out[key].append(norm(artist, title))
    con.close()
    return out


def run(a: list, b: list) -> int:
    """The longest run of songs of `a` that stand in `b` in the same order, one after the other."""
    where = {s: i for i, s in enumerate(b)}
    best = cur = 0
    for prev, s in zip([None, *a], a, strict=False):
        cur = cur + 1 if prev in where and s in where and where[s] == where[prev] + 1 else 1 if s in where else 0
        best = max(best, cur)
    return best


def audit(demo: Path, real: Path) -> list[str]:
    """What is wrong (empty: nothing)."""
    mine, theirs = lists(demo), lists(real)
    con = sqlite3.connect(f"file:{demo}?mode=ro", uri=True)
    titles = dict(con.execute("SELECT key, title FROM lists"))
    con.close()
    problems = []
    for key, songs in mine.items():
        shared = max(((len(set(songs) & set(t)) / len(set(songs)), k) for k, t in theirs.items()), default=(0, ""))
        longest = max((run(songs, t) for t in theirs.values()), default=0)
        flag = shared[0] > SHARE or longest > RUN
        print(
            f"{'!!' if flag else 'ok'}  {titles.get(key, key)[:28]:28} {len(songs):5} songs  "
            f"most shared with one real list {shared[0]:5.1%}  longest run in order {longest}"
        )
        if flag:
            problems.append(f"{titles.get(key, key)}: {shared[0]:.0%} shared, {longest} in a row")
    return problems


if __name__ == "__main__":
    found = audit(Path(sys.argv[1]), Path(sys.argv[2]))
    for p in found:
        print("  ", p)
    sys.exit(1 if found else 0)
