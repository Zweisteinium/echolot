"""Bandcamp offers of the SoundCloud songs the library has no genuine lossless copy of: the Bandcamp track a
song's SoundCloud page links to (its buy link, its description), with its price, stored in `bandcamp` (the Buy
page lists them). A free download (no email asked) is fetched and filed like any download. Never on a schedule
and not on the Jobs page: after New SoundCloud songs only the songs never looked at (their SoundCloud pages,
and Bandcamp only for a link); by hand (Refresh on the Buy page) every one again.
Bandcamp's bot check (bandcamp.Challenged) ends the run: the rest stays as it was.
"""

import datetime
import shutil
import sqlite3
from typing import TYPE_CHECKING

from echolot.jobs import acquire
from echolot.library import audio, filing, identity, rules
from echolot.services import bandcamp
from echolot.services import soundcloud as sc_api
from echolot.settings import options

if TYPE_CHECKING:
    from echolot.jobs.worker import Run

SONGS = (
    "SELECT s.*, b.song_key AS looked, b.url AS offer_url, b.result AS offer_result FROM wanted s "
    "LEFT JOIN files f ON f.path = s.file LEFT JOIN bandcamp b ON b.song_key = s.key "
    "WHERE s.service = 'soundcloud' AND NOT s.close_match AND (s.file IS NULL OR f.quality != 'lossless')"
)
RETRY = ("", "download failed")  # results after which a free download is tried again


def run(run: "Run") -> str:
    every = run.trigger == "manual"
    con = run.connect()
    try:
        rows = run.todo([r for r in con.execute(SONGS) if every or r["looked"] is None])
        token = sc_api.any_token(con, run.vault)
    finally:
        con.close()
    if not rows:
        return "nothing new"
    if not token:
        return "no SoundCloud login"
    links: dict[str, list[str]] = {}  # a track SoundCloud no longer has: none
    ids = [r["key"].removeprefix("soundcloud:") for r in rows]
    try:
        for i in range(0, len(ids), 50):
            for t in sc_api.tracks(token, ids[i : i + 50]):
                links[f"soundcloud:{t['id']}"] = bandcamp.links(t.get("purchase_url"), t.get("description"))
    except sc_api.SoundCloudError as e:
        return str(e)
    offers = free = filed = 0
    try:
        for n, row in enumerate(rows, 1):
            if run.stop.is_set() or run.give_way.is_set():  # paused or a more urgent job: goes on afterwards
                run.left = 0 if run.stop.is_set() else len(rows) - n + 1
                break
            want = acquire._want(row)
            run.say(f"{run.of(n, len(rows))}: {want.artist} – {want.title}", n - 1, len(rows))
            o = _offer(links.get(row["key"]) or [], want.title, want.length)
            con = run.connect()
            try:
                result = row["offer_result"] or "" if o and o.url == row["offer_url"] else ""
                if o and o.free and result in RETRY:
                    result = _download(run, con, o, want)
                    filed += result in ("new", "upgrade")
                    run.note(f"{want.artist} – {want.title}: free on Bandcamp, {result}")
                _store(con, row["key"], o, result)
            finally:
                con.close()
            run.handled.add(row["key"])
            offers += o is not None
            free += bool(o and o.free)
    except bandcamp.Challenged:
        run.note("Bandcamp asked for its bot check: stopped")
        return f"stopped: Bandcamp asked for its bot check ({offers} offers before)"
    finally:
        shutil.rmtree(run.paths.inbox("bandcamp"), ignore_errors=True)
    if filed:
        run.after.add("library")
    done = f"{len(run.handled)} songs looked at: {offers} on Bandcamp, {free} free" + (
        f", {filed} filed" if filed else ""
    )
    return done + (f"; gave way, {run.left} left" if run.left else "")


def _offer(urls: list[str], title: str, seconds: float) -> bandcamp.Offer | None:
    """The first linked page with the song's track; a page that would not come is passed over."""
    for url in urls:
        try:
            if o := bandcamp.offer(url, title, seconds):
                return o
        except bandcamp.Challenged:
            raise
        except bandcamp.BandcampError:
            continue
    return None


def _store(con: sqlite3.Connection, key: str, o: bandcamp.Offer | None, result: str) -> None:
    """The song's offer, or that it has none (url ''): it is looked at again only by hand."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    row = (key, o.url, o.artist, o.title, o.seconds, o.price, o.currency, int(o.free)) if o else (key, "", "", "", 0)
    with con:
        if o:
            con.execute(
                "INSERT OR REPLACE INTO bandcamp (song_key, url, artist, title, seconds, price, currency, free, result, "
                "checked) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*row, result, now),
            )
        else:
            con.execute(
                "INSERT OR REPLACE INTO bandcamp (song_key, url, artist, title, seconds, checked) VALUES (?, ?, ?, ?, ?, ?)",
                (*row, now),
            )


def _download(run: "Run", con: sqlite3.Connection, o: bandcamp.Offer, want: filing.Want) -> str:
    """Fetch a free track's FLAC and file it with the same checks as a SoundCloud song's FLAC from Soulseek: the
    audio of the song's copy in another codec replaces it; anything else waits in Review (the names on Bandcamp
    are the artist's, SoundCloud's an uploader's: only you can tell)."""
    try:
        got = bandcamp.free_flac(o, run.paths.inbox("bandcamp") / want.key.replace(":", "-"))
        prepared = audio.prepare(got, options.get(con, options.Files).keep_hires)
    except bandcamp.Challenged:
        raise
    except bandcamp.BandcampError:
        return "download failed"
    except audio.Rejected as e:
        return f"bad file: {e}"
    heard = identity.check(con, want.isrc, prepared.path, bool(rules.mix_cut(want.title)))
    same = acquire._same_audio(run, con, want, prepared)
    found = {"file_name": got.name, "folders": (o.artist,), "fake": prepared.fake, "heard": heard, "url": o.url}
    checks = {"strict": True, "probable": False, "confirm": not same}
    if same:
        checks |= {"same_audio": same[1], "replaces": same[0]}
    action, dest = filing.file_into(con, run.paths, prepared.path, want, "bandcamp", **found, **checks)
    if dest and action in ("new", "upgrade"):
        acquire.finish(run, con, dest, want)
    return action
