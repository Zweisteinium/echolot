"""Unreleased songs: files that exist nowhere else (a friend's own music, customs deleted everywhere), uploaded
by hand (web/unreleased) into the user's Unreleased lists, which hold only such songs. Each one is its own song
(service local, key local:<12 hex>, the names it was given) and its own file, tracks/Unreleased/<Artist> -
<Title>.<ext> (filing.place_unreleased): kept as uploaded (a WAV or AIFF as FLAC, hi-res kept), its tags written
once (tagging.write_unreleased). From then on Echolot leaves it alone: nothing searches, upgrades, retags,
covers, merges or links another song to it (catalog.LOCAL and catalog.unreleased are left out wherever those
pick songs or files); if its file is gone, Missing says so and nothing looks for it (UNAVAILABLE).

Before an upload becomes one, the release check (upload.release) asks whether it is released after all; a
released song goes into Echolot · Wished as any released song (web/unreleased)."""

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime

from echolot.library import audio, catalog, filing, tagging
from echolot.library.filing import Paths
from echolot.library.upload import File

LOCAL = catalog.LOCAL
UNAVAILABLE = "unreleased: there is no other copy"  # (songs.unavailable: nothing better can be had, nothing searches)
LONGEST_NAME = 80  # characters of a list's name


class Refused(ValueError):
    """What the user asked for can't be done (the message says why)."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def lists(con: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """The user's Unreleased lists (key, title, songs), the oldest first."""
    return con.execute(
        "SELECT s.key, s.title, (SELECT count(*) FROM list_songs ls WHERE ls.list_key = s.key) AS songs "
        "FROM sources s WHERE s.user_id = ? AND s.service = ? ORDER BY s.position, s.added, s.key",
        (user_id, LOCAL),
    ).fetchall()


def owns(con: sqlite3.Connection, user_id: int, key: str) -> bool:
    sql = "SELECT 1 FROM sources WHERE user_id = ? AND key = ? AND service = ?"
    return con.execute(sql, (user_id, key, LOCAL)).fetchone() is not None


def _name(con: sqlite3.Connection, user_id: int, name: str, key: str = "") -> str:
    name = " ".join(name.split())
    if not name:
        raise Refused("A list needs a name.")
    if len(name) > LONGEST_NAME:
        raise Refused(f"A list's name has at most {LONGEST_NAME} characters.")
    sql = "SELECT 1 FROM sources WHERE user_id = ? AND service = ? AND key != ? AND lower(title) = lower(?)"
    if con.execute(sql, (user_id, LOCAL, key, name)).fetchone():
        raise Refused(f"You have an Unreleased list named {name} already.")
    return name


def _sync(con: sqlite3.Connection) -> None:
    from echolot.jobs import lists as list_jobs

    list_jobs.sync_table(con)


def create(con: sqlite3.Connection, user_id: int, name: str) -> str:
    """A new Unreleased list of the user's; its key."""
    name = _name(con, user_id, name)
    key = f"{LOCAL}:list:{secrets.token_hex(6)}"
    position = 2_000_000 + len(lists(con, user_id))  # (after the configured lists and Wished)
    with con:
        con.execute(
            "INSERT INTO sources (user_id, key, service, likes, url, title, playlist, enabled, position, added) "
            "VALUES (?, ?, ?, 0, ?, ?, 1, 1, ?, ?)",
            (user_id, key, LOCAL, f"/lists/{key}", name, position, _now()),
        )
    _sync(con)
    with con:  # (no source to fetch it from: listed from the start)
        con.execute("UPDATE lists SET fetched = 1, fetched_at = ? WHERE key = ?", (_now(), key))
    return key


def rename(con: sqlite3.Connection, user_id: int, key: str, name: str) -> None:
    """The list's new name (its playlist follows: playlists.sync_owners)."""
    if not owns(con, user_id, key):
        raise Refused("That is not one of your Unreleased lists (any more).")
    name = _name(con, user_id, name, key)
    with con:
        con.execute("UPDATE sources SET title = ? WHERE user_id = ? AND key = ?", (name, user_id, key))
    _sync(con)


def delete(con: sqlite3.Connection, user_id: int, key: str) -> None:
    """The list and its playlist go; its songs and their files stay in the library."""
    if not owns(con, user_id, key):
        raise Refused("That is not one of your Unreleased lists (any more).")
    with con:
        con.execute("DELETE FROM sources WHERE user_id = ? AND key = ?", (user_id, key))
    _sync(con)  # (the list's row goes, and with it its songs: list_songs cascades)


def remove(con: sqlite3.Connection, user_id: int, key: str, song: str) -> None:
    """A song out of the list (its file stays in the library); the others keep their order."""
    if not owns(con, user_id, key):
        raise Refused("That is not one of your Unreleased lists (any more).")
    rows = con.execute("SELECT song_key, added FROM list_songs WHERE list_key = ? ORDER BY position", (key,))
    left = [(k, added) for k, added in rows if k != song]
    _store(con, key, left)


def _store(con: sqlite3.Connection, key: str, songs: list[tuple[str, str | None]]) -> None:
    today = date.today().isoformat()
    with con:
        con.execute("DELETE FROM list_songs WHERE list_key = ?", (key,))
        con.executemany(
            "INSERT INTO list_songs (list_key, position, song_key, added) VALUES (?, ?, ?, ?)",
            [(key, n, k, added) for n, (k, added) in enumerate(songs)],
        )
        con.executemany(
            "INSERT INTO list_history (list_key, song_key, first_seen, last_seen) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (list_key, song_key) DO UPDATE SET last_seen = excluded.last_seen",
            [(key, k, today, today) for k, _ in songs],
        )


@dataclass(frozen=True)
class Names:
    """What an unreleased song is called, as the user gave it: artist and title (both needed), album (empty: the
    title, as a single's)."""

    artist: str
    title: str
    album: str = ""

    def clean(self) -> "Names":
        return Names(*(" ".join(v.split()) for v in (self.artist, self.title, self.album)))


def taken(paths: Paths, names: Names) -> bool:
    """An unreleased song has this file name already (two must differ in their names: "(Custom Edit)")."""
    n = names.clean()
    return filing.unreleased_taken(paths, filing.unreleased_name(n.artist, n.title))


def add(
    con: sqlite3.Connection, paths: Paths, f: File, names: Names, key: str, note: str = "", cover: bytes | None = None
) -> tuple[str, str | None]:
    """File an uploaded file as a new unreleased song at the end of the list `key` (the user's: checked by the
    caller): its tags (the names, "Unreleased · <note>", the `cover` if it has none), its file in
    tracks/Unreleased/, its song. Returns a line for the dialog and the song's key (None: not filed)."""
    n = names.clean()
    label = f"{n.artist or '?'} – {n.title or '?'}"
    if not n.artist or not n.title:
        return f"{f.name}: not imported (it needs an artist and a title)", None
    if f.path is None or not f.path.is_file() or f.error:
        return f"{f.name}: not imported (gone)", None
    name = filing.unreleased_name(n.artist, n.title)
    if filing.unreleased_taken(paths, name):
        return f"{label}: not imported ({catalog.UNRELEASED}/{name} is another song's: give it another title)", None
    try:
        tagging.write_unreleased(f.path, n.artist, n.title, n.album, note)
    except Exception as e:  # (a format the tagger can't write: the file keeps its own tags)
        tag_note = f" (tags not written: {e})"
    else:
        tag_note = ""
    if cover:  # (a JPEG, for a file without a picture of its own)
        audio.embed_cover(f.path, cover)
    song = f"{LOCAL}:{secrets.token_hex(6)}"
    info = {"source": "manual", "matched": "by hand", "song": song, "artist": n.artist, "title": n.title}
    try:
        dest = filing.place_unreleased(con, paths, f.path, name, f.tier == "fake", f.source, reason=note, **info)
    except FileExistsError:  # (filed meanwhile)
        return f"{label}: not imported ({catalog.UNRELEASED}/{name} is another song's: give it another title)", None
    stem = dest.relative_to(paths.tracks).as_posix().rsplit(".", 1)[0]
    with con:
        con.execute(
            "INSERT INTO songs (key, service, artist, title, album, length, artists, stem, unavailable) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (song, LOCAL, n.artist, n.title, n.album, float(f.seconds or 0), json.dumps([n.artist]), stem, UNAVAILABLE),
        )
    rows = con.execute("SELECT song_key, added FROM list_songs WHERE list_key = ? ORDER BY position", (key,))
    added = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    _store(con, key, [*rows.fetchall(), (song, added)])
    return f"{label}: filed as {dest.relative_to(paths.tracks).as_posix()}{tag_note}", song
