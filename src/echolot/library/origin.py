"""Where a file uploaded by hand comes from (origins): the artist's free download (a gate on Hypeddit and the
like: the song's free_links), bought (on Bandcamp or elsewhere) or elsewhere. The artist's own release (free or
bought) is the best there is, even lossy (an MP3 320 the artist gives away): it counts as at its best, leaves
the Buy list and the FLAC upgrade skips it, unless Bandcamp sells the song (lossless: better). Noted for the
file it came with: another file of the song ends it; Try again (the lists' mark) forgets it. Files carry no
sign of their origin, so the upload dialog asks, prefilled from the song's links."""

import datetime
import sqlite3
from dataclasses import dataclass

KINDS = ("free", "bought", "elsewhere")
_OFFER = "SELECT 1 FROM bandcamp b WHERE b.song_key = o.song_key AND b.url > '' AND (b.price IS NOT NULL OR b.free)"
# SQL: the keys of the songs whose file is the artist's own release, which no Bandcamp offer betters (one bought
# there is that offer's file)
BEST = (
    "SELECT o.song_key FROM origins o JOIN songs os ON os.key = o.song_key AND os.file = o.file "
    f"WHERE o.kind IN ('free', 'bought') AND (o.site = 'Bandcamp' OR NOT EXISTS ({_OFFER}))"
)


@dataclass(frozen=True)
class Choice:
    value: str  # KINDS
    label: str


def choices(con: sqlite3.Connection, key: str) -> tuple[str, list[Choice]]:
    """(the likely one, the choices) of where an upload of the song comes from: its free download if it has
    one, else Bandcamp if Bandcamp sells it, else elsewhere."""
    link = con.execute("SELECT site FROM free_links WHERE song_key = ? AND url > ''", (key,)).fetchone()
    offer = con.execute("SELECT 1 FROM bandcamp WHERE song_key = ? AND url > ''", (key,)).fetchone()
    out = [
        Choice("free", f"{link[0]}: the artist's free download" if link else "The artist's free download"),
        Choice("bought", "Bought on Bandcamp" if offer else "Bought (Bandcamp, Beatport, …)"),
        Choice("elsewhere", "Elsewhere"),
    ]
    return ("free" if link else "bought" if offer else "elsewhere"), out


def link(con: sqlite3.Connection, key: str, kind: str) -> tuple[str, str]:
    """(site, address) a file of the song came from: its free download's, or its Bandcamp page; ('', '')."""
    if kind == "free" and (
        r := con.execute("SELECT site, url FROM free_links WHERE song_key = ? AND url > ''", (key,)).fetchone()
    ):
        return r[0], r[1]
    if kind == "bought" and (
        r := con.execute("SELECT url FROM bandcamp WHERE song_key = ? AND url > ''", (key,)).fetchone()
    ):
        return "Bandcamp", r[0]
    return "", ""


def note(con: sqlite3.Connection, key: str, file: str, kind: str) -> None:
    """The song's file `file` (relative to the library) came from `kind` (KINDS)."""
    if kind not in KINDS:
        return
    site, url = link(con, key, kind)
    with con:
        con.execute(
            "INSERT OR REPLACE INTO origins (song_key, file, kind, site, url, since) VALUES (?, ?, ?, ?, ?, ?)",
            (key, file, kind, site, url, datetime.datetime.now().isoformat(timespec="seconds")),
        )


def forget(con: sqlite3.Connection, key: str) -> bool:
    """Try again: the song is searched and offered again (whether there was a note)."""
    with con:
        return con.execute("DELETE FROM origins WHERE song_key = ?", (key,)).rowcount > 0


def marks(con: sqlite3.Connection, keys: list[str]) -> dict[str, str]:
    """Song key -> why it is the best there is though lossy (the artist's own release), for those of `keys`."""
    if not keys:
        return {}
    rows = con.execute(
        "SELECT o.song_key, o.kind, o.site FROM origins o JOIN files f ON f.path = o.file "
        f"WHERE f.quality != 'lossless' AND o.song_key IN ({BEST})"
    ).fetchall()
    wanted = set(keys)
    out = {}
    for key, kind, site in rows:
        if key in wanted:
            where = (
                f"{site}: the artist's free download"
                if kind == "free" and site
                else ("the artist's free download" if kind == "free" else f"bought{' on ' + site if site else ''}")
            )
            out[key] = f"The artist's own release ({where}): the best there is"
    return out
