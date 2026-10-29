"""The Discogs page: the releases of a user's library songs Discogs lacks (jobs/discogs), filtered by list, genre
and whether a download store has them; a release's suggestion of what to submit, field by field as Discogs'
form asks; Check (admins) looks at a list's releases. Only while Discogs is on (settings section discogs)."""

import json
import re
import sqlite3
import urllib.parse
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from echolot import db
from echolot.jobs import discogs as job
from echolot.settings import options
from echolot.web import stats
from echolot.web.common import DB, back, page

router = APIRouter(include_in_schema=False)
SHOW = {  # the page's choice of rows: label, which states, only with a store release
    "todo": ("To add", ("digital", "missing"), False),
    "store": ("To add, on Beatport", ("digital", "missing"), True),
    "all": ("All looked at", ("listed", "digital", "missing"), False),
}
ADD = "https://www.discogs.com/release/add"
SELF = re.compile(r"\b(?:distrokid|cd ?baby|tunecore|amuse|ditto music|routenote|unitedmasters|spinnup)\b", re.I)


def _on(con: sqlite3.Connection) -> bool:
    return options.get(con, options.Discogs).enabled


def _rows(con: sqlite3.Connection, uid: int | None) -> tuple[dict[str, job.Album], dict[str, sqlite3.Row]]:
    return job.albums(con, None, uid), {r["album"]: r for r in con.execute("SELECT * FROM discogs")}


def to_add(con: sqlite3.Connection, uid: int | None) -> int | None:
    """How many releases of the user's library songs Discogs lacks (the page's default rows); None while off."""
    if not _on(con):
        return None
    albums, rows = _rows(con, uid)
    return sum(1 for k, r in rows.items() if k in albums and r["state"] != "listed")


def _store(row: sqlite3.Row) -> dict[str, Any] | None:
    return json.loads(row["store"]) if row["store"] is not None else None


@router.get("/discogs", response_class=HTMLResponse)
def discogs_page(
    request: Request, con: DB, list_key: Annotated[str, Query(alias="list")] = "", genre: str = "", show: str = "todo"
) -> HTMLResponse:
    if not _on(con):
        return page(request, "discogs.html", nav="discogs", on=False)
    uid = stats.scope(request.state.user)
    albums, rows = _rows(con, uid)
    _, states, need_store = SHOW.get(show, SHOW["todo"])
    lists: dict[str, list] = {}  # list key -> [title, releases, looked at]
    entries = []
    for a in albums.values():
        row = rows.get(a.key)
        for key, title in a.lists.items():
            counts = lists.setdefault(key, [title, 0, 0])
            counts[1] += 1
            counts[2] += row is not None
        if not row or (list_key and list_key not in a.lists) or (genre and row["genre"] != genre):
            continue
        store = _store(row)
        if row["state"] in states and (store or not need_store):
            found = json.loads(row["found"]) if row["found"] else None
            entries.append({"album": a, "row": row, "store": store, "found": found})
    genres = sorted({r["genre"] for k, r in rows.items() if k in albums and r["genre"]}, key=str.lower)
    last = con.execute("SELECT * FROM jobs WHERE name = 'discogs'").fetchone()
    runs, asked = request.app.state.worker.state()
    return page(
        request,
        "discogs.html",
        nav="discogs",
        on=True,
        entries=entries,
        lists=sorted(lists.items(), key=lambda kv: kv[1][0].lower()),
        genres=genres,
        selected={"list": list_key, "genre": genre, "show": show if show in SHOW else "todo"},
        shows=SHOW,
        last=last,
        running="discogs" in runs or "discogs" in asked,
    )


@router.post("/discogs/check")
def discogs_check(
    request: Request, con: DB, list_key: Annotated[str, Form(alias="list")], again: Annotated[bool, Form()] = False
) -> RedirectResponse:
    """Look at a list's releases on Discogs (admins: it asks Discogs and Beatport for each)."""
    where = f"/discogs?list={urllib.parse.quote(list_key)}"
    if not _on(con):
        return back("/discogs", error="Discogs is off.")
    if not con.execute("SELECT 1 FROM lists WHERE key = ?", (list_key,)).fetchone():
        return back("/discogs", error="No such list.")
    with con:
        db.set_meta(con, job.REQUEST, json.dumps({"list": list_key, "again": again}))
    if not request.app.state.worker.trigger("discogs"):
        return back(where, error="The check could not start.")
    return back(where, ok="The check starts in a moment; reload the page in a few minutes.")


@router.get("/discogs/release", response_class=HTMLResponse)
def discogs_release(request: Request, con: DB, album: str) -> HTMLResponse:
    """A release's suggestion, for the dialog."""
    if not _on(con):
        raise HTTPException(404, "Discogs is off")
    albums, rows = _rows(con, stats.scope(request.state.user))
    if album not in albums or album not in rows:
        raise HTTPException(404, "no such release")
    a, row = albums[album], rows[album]
    found = json.loads(row["found"]) if row["found"] else None
    return page(request, "_discogs_release.html", a=a, row=row, found=found, s=suggestion(a, row), add=ADD)


def _length(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}" if seconds else ""


def suggestion(a: job.Album, row: sqlite3.Row) -> dict[str, Any]:
    """What to enter in Discogs' form, in its order: from the Beatport release, else what Spotify and the
    library know (then the tracks are only the library's songs of it)."""
    store = _store(row) or {}
    artists = store.get("artists") or a.artists[:1]
    name = store.get("name") or a.title
    tracks = []
    for n, t in enumerate(store.get("tracks") or [], 1):
        title = f"{t['name']} ({t['mix']})" if t.get("mix") else t["name"]
        own = t.get("artists") if t.get("artists") and t["artists"] != artists else []
        credits = [f"Remix – {r}" for r in t.get("remixers") or []]
        length = _length(t.get("seconds") or 0)
        tracks.append({"position": str(n), "artists": own, "title": title, "length": length, "credits": credits})
    style = re.sub(r"\s*\(.*\)$", "", store.get("genre") or "").strip()
    label, catno = store.get("label") or "", store.get("catno") or ""
    if SELF.search(label):  # (a distributor for artists: Discogs calls that self-released)
        label, catno = f"Not On Label ({artists[0]} Self-released)", "none"
    elif catno and catno == store.get("upc"):
        catno = "none"
    return {
        "from_store": bool(store),
        "artists": artists,
        "title": name,
        "label": label,
        "catno": catno,
        "barcode": store.get("upc") or "",
        "qty": len(tracks) or a.tracks or len(a.songs),
        "description": "EP" if re.search(r"\bEP\b", name) else "",
        "released": store.get("date") or a.released,
        "tracks": tracks,
        "genre": "Electronic" if store else "",
        "style": style,
        "your_genre": row["genre"],
        "cover": store.get("cover") or "",
        "url": store.get("url") or "",
        "notes": f"Release details from its Beatport page: {store['url']}" if store.get("url") else "",
        "search": "https://www.discogs.com/search/?" + urllib.parse.urlencode({"q": f"{a.artist} {a.title}"}),
    }
