"""Bandcamp offers of the SoundCloud songs without a genuine lossless copy: the Bandcamp track a song's
SoundCloud page links to (buy link, description), with its price, stored in `bandcamp` for the Buy page. A
free download (no email asked) is fetched and filed like any download. The pages' other free downloads go
into `free_links`: a Dropbox file is fetched the same way, a gate is listed (Hypeddit's got by `gates`).
After New SoundCloud songs only the songs never looked at (Bandcamp only for a link); by hand (Refresh on
the Buy page) every one again, a stored offer's own page too. Bandcamp's bot check (bandcamp.Challenged)
ends the run.
"""

import dataclasses
import datetime
import shutil
import sqlite3
import time
from typing import TYPE_CHECKING

from echolot import db
from echolot.jobs import acquire
from echolot.library import audio, filing, identity, origin, rules
from echolot.services import bandcamp, free_links, hypeddit
from echolot.services import soundcloud as sc_api
from echolot.settings import options

if TYPE_CHECKING:
    from echolot.jobs.worker import Run

SONGS = (
    "SELECT s.*, b.song_key AS looked, b.url AS offer_url, b.result AS offer_result, fl.song_key AS looked_free, "
    "fl.url AS free_url, fl.result AS free_result FROM wanted s LEFT JOIN files f ON f.path = s.file "
    "LEFT JOIN bandcamp b ON b.song_key = s.key LEFT JOIN free_links fl ON fl.song_key = s.key "
    "WHERE s.service = 'soundcloud' AND NOT s.close_match AND (s.file IS NULL OR f.quality != 'lossless') "
    "ORDER BY s.key"
)
RETRY = ("", "download failed")  # results after which a free download is tried again
REFUSED = "bandcamp_refused"  # meta: when Bandcamp last asked for its bot check or refused (ISO time)
COOLDOWN = datetime.timedelta(hours=24)  # after REFUSED, runs not by hand leave Bandcamp alone this long


def run(run: "Run") -> str:
    every = run.trigger == "manual"
    con = run.connect()
    try:
        refused = db.get_meta(con, REFUSED)
        if not every and refused and datetime.datetime.fromisoformat(refused) + COOLDOWN > datetime.datetime.now():
            return f"Bandcamp refused at {refused[11:16]}: left alone for a day (Refresh asks again)"
        rows = run.todo([r for r in con.execute(SONGS) if every or r["looked"] is None or r["looked_free"] is None])
        token = sc_api.any_token(con, run.vault)
    finally:
        con.close()
    if not rows:
        return "nothing new"
    if not token:
        return "no SoundCloud login"
    links: dict[str, list[str]] = {}  # a track SoundCloud no longer has: none
    others: dict[str, list[free_links.Link]] = {}
    ids = [r["key"].removeprefix("soundcloud:") for r in rows]
    try:
        for i in range(0, len(ids), 50):
            for t in sc_api.tracks(token, ids[i : i + 50]):
                buy, text = t.get("purchase_url"), t.get("description")
                links[f"soundcloud:{t['id']}"] = bandcamp.links(buy, text)
                others[f"soundcloud:{t['id']}"] = free_links.links(buy, t.get("purchase_title"), text)
    except sc_api.SoundCloudError as e:
        return str(e)
    offers = free = filed = gates = 0
    try:
        for n, row in enumerate(rows, 1):
            if run.stop.is_set() or run.give_way.is_set():
                run.left = 0 if run.stop.is_set() else len(rows) - n + 1
                break
            want = acquire._want(row)
            run.say(f"{run.of(n, len(rows))}: {want.artist} – {want.title}", n - 1, len(rows))
            con = run.connect()
            try:
                link = next(iter(others.get(row["key"]) or []), None)
                if every or row["looked_free"] is None:
                    result = _free_link(run, con, row, link, want)
                    filed += result in ("new", "upgrade")
                    gates += bool(link and not link.fetch)
                    if result in ("new", "upgrade"):
                        run.handled.add(row["key"])
                        continue  # (lossless now: no Bandcamp offer needed)
            finally:
                con.close()
            if not (every or row["looked"] is None):
                run.handled.add(row["key"])
                continue  # (its Bandcamp page was read before: only by hand again)
            urls = links.get(row["key"]) or []
            if every and row["offer_url"] and row["offer_url"] not in urls:
                urls = [*urls, row["offer_url"]]  # (found by other means than a link: read again, kept if there)
            o, unread = _offer(urls, want.title, want.length)
            if unread and not o and row["looked"] is not None:
                run.note(f"{want.artist} – {want.title}: Bandcamp's page did not come, kept as it was")
                run.handled.add(row["key"])
                continue  # (a page that did not come is no lost offer)
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
    except bandcamp.Challenged as e:
        con = run.connect()
        try:
            with con:
                db.set_meta(con, REFUSED, datetime.datetime.now().isoformat(timespec="seconds"))
        finally:
            con.close()
        run.note(f"{e}: stopped")
        return f"stopped: {e} ({offers} offers before)"
    finally:
        shutil.rmtree(run.paths.inbox("bandcamp"), ignore_errors=True)
        shutil.rmtree(run.paths.inbox("free-links"), ignore_errors=True)
    if filed:
        run.after.add("library")
    done = (
        f"{len(run.handled)} songs looked at: {offers} on Bandcamp, {free} free"
        + (f", {gates} free downloads to get by hand" if gates else "")
        + (f", {filed} filed" if filed else "")
    )
    return done + (f"; gave way, {run.left} left" if run.left else "")


def _offer(urls: list[str], title: str, seconds: float) -> tuple[bandcamp.Offer | None, bool]:
    """(The first linked page with the song's track, whether a page did not come: a timeout or an error, not
    a page that is gone.)"""
    unread = False
    for url in urls:
        try:
            if o := bandcamp.offer(url, title, seconds):
                return o, unread
        except bandcamp.Challenged:
            raise
        except bandcamp.Gone:
            continue
        except bandcamp.BandcampError:
            unread = True
    return None, unread


def _free_link(
    run: "Run", con: sqlite3.Connection, row: sqlite3.Row, link: free_links.Link | None, want: filing.Want
) -> str:
    """Store the song's free download outside Bandcamp (or that it has none); a file is fetched and filed
    (again only after a failed download, or when the link changed): what it came to."""
    result = row["free_result"] or "" if link and link.url == row["free_url"] else ""
    if link and link.fetch and result in RETRY:
        result = _fetch(run, con, link, want)
        run.note(f"{want.artist} – {want.title}: free on {link.site}, {result}")
    now = datetime.datetime.now().isoformat(timespec="seconds")
    values = (link.url, link.site, link.label, int(link.fetch)) if link else ("", "", "", 0)
    with con:
        con.execute(
            "INSERT OR REPLACE INTO free_links (song_key, url, site, label, fetch, result, checked) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["key"], *values, result, now),
        )
    return result


def _fetch(run: "Run", con: sqlite3.Connection, link: free_links.Link, want: filing.Want) -> str:
    """Download a linked file and file it as a free Bandcamp FLAC is (_download): the same audio as the song's
    copy replaces it if it is better; anything else waits in Review."""
    try:
        got = free_links.fetch(link, run.paths.inbox("free-links") / want.key.replace(":", "-"))
        prepared = audio.prepare(got, options.get(con, options.Files).keep_hires)
    except free_links.LinkError:
        return "download failed"
    except audio.Rejected as e:
        return f"bad file: {e}"
    return _file(run, con, want, prepared, got.stem, (), link.url, link.site.lower())


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
    want = dataclasses.replace(want, artists=[*want.artists, o.artist])  # Bandcamp's artist is the song's too
    return _file(run, con, want, prepared, got.stem, (o.artist,), o.url, "bandcamp")


def _file(
    run: "Run",
    con: sqlite3.Connection,
    want: filing.Want,
    prepared: audio.Prepared,
    name: str,
    folders: tuple[str, ...],
    url: str,
    source: str,
    origin_kind: str = "",
) -> str:
    heard = identity.check(con, want.isrc, prepared.path, bool(rules.mix_cut(want.title)), want.length)
    same = acquire._same_audio(run, con, want, prepared)
    found = {"file_name": name, "folders": folders, "fake": prepared.fake, "heard": heard, "url": url}
    checks = {"strict": True, "probable": False, "confirm": not same}
    if same:
        checks |= {"same_audio": same[1], "replaces": same[0]}
        own = origin_kind and want.length and abs(audio.probe(prepared.path)[0] - want.length) <= 1
        checks["sure"] = bool(own)  # the artist's file from the song's page, same audio and length: names aside
    action, dest = filing.file_into(con, run.paths, prepared.path, want, source, **found, **checks)
    if dest and action in ("new", "upgrade"):
        acquire.finish(run, con, dest, want)
        if origin_kind:  # the file is the artist's own (a free download): kept even lossy, off the Buy list
            origin.note(con, want.key, dest.relative_to(run.paths.tracks).as_posix(), origin_kind)
    return action


GATE_SITE = "Hypeddit"  # the only gate site that does not check the steps it asks for
GATE_RETRY = ("", "download failed", "needs email")  # free_links.result values a gate run tries (again)
GATE_PACE = 2  # seconds between gates
GATES = (  # the songs whose gate to get; params: GATE_SITE, *GATE_RETRY
    "SELECT s.*, fl.url AS gate_url FROM wanted s JOIN free_links fl ON fl.song_key = s.key "
    "LEFT JOIN files f ON f.path = s.file WHERE fl.site = ? AND fl.url > '' AND fl.result IN (?, ?, ?) "
    "AND NOT s.close_match AND (s.file IS NULL OR f.quality != 'lossless') "
    "AND NOT EXISTS (SELECT 1 FROM origins o WHERE o.song_key = s.key) ORDER BY s.key"
)
_GATE_NOTE = {  # a filing action as the log tells it
    "new": "filed",
    "upgrade": "replaced your copy",
    "confirm": "waits in Review",
    "duplicate": "no better than your copy",
    "wrong-song": "not the song",
    "mismatch": "another length",
}


def gates(run: "Run") -> str:
    """Get the Hypeddit free downloads of lossy songs and file them as the artist's own (origin free): the same
    audio replaces your copy, a different one waits in Review. On demand from the Buy page; an email gate needs the
    address in Settings, else it waits (needs email)."""
    con = run.connect()
    try:
        email = options.get(con, options.Files).gate_email
        rows = run.todo([dict(r) for r in con.execute(GATES, (GATE_SITE, *GATE_RETRY))])
    finally:
        con.close()
    if not rows:
        return "nothing to get"
    got = review = waiting = nothing = 0
    try:
        for n, row in enumerate(rows, 1):
            if run.stop.is_set() or run.give_way.is_set():
                run.left = 0 if run.stop.is_set() else len(rows) - n + 1
                break
            if n > 1:
                time.sleep(GATE_PACE)
            want = acquire._want(row)
            run.say(f"{run.of(n, len(rows))}: {want.artist} – {want.title}", n - 1, len(rows))
            con = run.connect()
            try:
                result = _gate(run, con, row, email, want)
            finally:
                con.close()
            got += result in ("new", "upgrade")
            review += result == "confirm"
            waiting += result == "needs email"
            nothing += result not in ("new", "upgrade", "confirm", "needs email")
            run.handled.add(row["key"])
    finally:
        shutil.rmtree(run.paths.inbox("hypeddit"), ignore_errors=True)
    if got:
        run.after.add("library")
    done = f"{len(run.handled)} gates: {got} filed"
    done += f", {review} in Review" if review else ""
    done += f", {waiting} need an email" if waiting else ""
    done += f", {nothing} gave nothing" if nothing else ""
    return done + (f"; gave way, {run.left} left" if run.left else "")


def _gate(run: "Run", con: sqlite3.Connection, row: dict, email: str, want: filing.Want) -> str:
    """Fetch and file one gate; its result (also stored in free_links)."""
    url = row["gate_url"]
    try:
        gate = hypeddit.read(url)
        if gate.email and not email:
            result, note = "needs email", "wants an email (set one in Settings → Files)"
        else:
            file = hypeddit.unlock(gate, email)
            got = hypeddit.fetch(file, run.paths.inbox("hypeddit") / want.key.replace(":", "-"))
            prepared = audio.prepare(got, options.get(con, options.Files).keep_hires)
            result = _file(run, con, want, prepared, got.stem, (), url, GATE_SITE.lower(), origin_kind="free")
            note = _GATE_NOTE.get(result, result)
    except hypeddit.GateError as e:
        result, note = "download failed", str(e)
    except audio.Rejected as e:
        result, note = f"bad file: {e}", str(e)
    run.note(f"{want.artist} – {want.title}: {GATE_SITE}, {note}")
    now = datetime.datetime.now().isoformat(timespec="seconds")
    with con:
        con.execute("UPDATE free_links SET result = ?, checked = ? WHERE song_key = ?", (result, now, row["key"]))
    return result
