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

from echolot.library import audio, catalog, filing, identity, recordings, rules, tagging, upload
from echolot.library.filing import Paths
from echolot.library.upload import File

LOCAL = catalog.LOCAL
UNAVAILABLE = "unreleased: there is no other copy"  # (songs.unavailable: nothing better can be had, nothing searches)
LONGEST_NAME = 80  # characters of a list's name
MAX_LISTS = 10  # Unreleased lists a user can have


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
    """A new Unreleased list of the user's (at most MAX_LISTS); its key."""
    if len(lists(con, user_id)) >= MAX_LISTS:
        raise Refused(f"You have {MAX_LISTS} Unreleased lists already: put the songs into one of them.")
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


DROP = "delete"  # delete's choice for the songs on no other list: deleted with it


def only_here(con: sqlite3.Connection, user_id: int, key: str) -> list[str]:
    """The songs of the list on none of the user's other Unreleased lists."""
    sql = (
        "SELECT ls.song_key FROM list_songs ls WHERE ls.list_key = ? AND NOT EXISTS (SELECT 1 FROM list_songs o "
        "JOIN sources s ON s.key = o.list_key AND s.user_id = ? AND s.service = ? "
        "WHERE o.song_key = ls.song_key AND o.list_key != ls.list_key) ORDER BY ls.position"
    )
    return [r[0] for r in con.execute(sql, (key, user_id, LOCAL))]


def delete(con: sqlite3.Connection, paths: Paths, user_id: int, key: str, songs_to: str) -> int:
    """The list and its playlist go. Its songs on no other list of the user's go into the list `songs_to`, or
    (DROP) are deleted with it (delete_song: their files kept KEEP_DAYS in inbox/replaced/). Returns how many
    songs were moved or deleted."""
    if not owns(con, user_id, key):
        raise Refused("That is not one of your Unreleased lists (any more).")
    alone = only_here(con, user_id, key)
    if alone and songs_to != DROP and (songs_to == key or not owns(con, user_id, songs_to)):
        raise Refused("Choose the list its songs go into, or delete them with it.")
    for song in alone:
        if songs_to == DROP:
            delete_song(con, paths, user_id, song)
        else:
            add_to(con, user_id, songs_to, song)
    with con:
        con.execute("DELETE FROM sources WHERE user_id = ? AND key = ?", (user_id, key))
    _sync(con)  # (the list's row goes, and with it its songs: list_songs cascades)
    return len(alone)


def _songs_of(con: sqlite3.Connection, key: str) -> list[tuple[str, str | None]]:
    rows = con.execute("SELECT song_key, added FROM list_songs WHERE list_key = ? ORDER BY position", (key,))
    return [(k, added) for k, added in rows]


def in_lists(con: sqlite3.Connection, user_id: int, song: str) -> list[str]:
    """The keys of the user's Unreleased lists the song is on."""
    sql = "SELECT ls.list_key FROM list_songs ls JOIN sources s ON s.key = ls.list_key WHERE ls.song_key = ? "
    return [r[0] for r in con.execute(sql + "AND s.user_id = ? AND s.service = ?", (song, user_id, LOCAL))]


def user_songs(con: sqlite3.Connection, user_id: int) -> list[str]:
    """The keys of the unreleased songs on the user's lists."""
    sql = "SELECT DISTINCT ls.song_key FROM list_songs ls JOIN sources s ON s.key = ls.list_key "
    return [r[0] for r in con.execute(sql + "WHERE s.user_id = ? AND s.service = ?", (user_id, LOCAL))]


def _added() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_to(con: sqlite3.Connection, user_id: int, key: str, song: str) -> None:
    """The song (on one of the user's Unreleased lists) at the end of their list `key` too."""
    if not owns(con, user_id, key):
        raise Refused("That is not one of your Unreleased lists (any more).")
    if not in_lists(con, user_id, song):
        raise Refused("That song is on none of your Unreleased lists.")
    here = _songs_of(con, key)
    if all(k != song for k, _ in here):
        _store(con, key, [*here, (song, _added())])


def move(con: sqlite3.Connection, user_id: int, key: str, song: str, to: str) -> None:
    """The song out of the list `key` into the list `to` (both the user's)."""
    if to == key:
        return
    add_to(con, user_id, to, song)
    remove(con, user_id, key, song, moved=True)


def remove(con: sqlite3.Connection, user_id: int, key: str, song: str, moved: bool = False) -> None:
    """A song out of the list (the others keep their order); not out of its last one: a song is on at least one
    of its owner's lists (else move or delete it)."""
    if not owns(con, user_id, key):
        raise Refused("That is not one of your Unreleased lists (any more).")
    if not moved and in_lists(con, user_id, song) == [key]:
        raise Refused("It is on none of your other Unreleased lists: move it into one, or delete the song.")
    _store(con, key, [(k, added) for k, added in _songs_of(con, key) if k != song])


def delete_song(con: sqlite3.Connection, paths: Paths, user_id: int, song: str) -> str:
    """The song (on one of the user's Unreleased lists) goes: out of their lists, its file into
    inbox/replaced/<date>/ (kept KEEP_DAYS days, then deleted). Returns its names."""
    row = con.execute("SELECT * FROM songs WHERE key = ? AND service = ?", (song, LOCAL)).fetchone()
    lists_in = in_lists(con, user_id, song)
    if row is None or not lists_in:
        raise Refused("That song is on none of your Unreleased lists.")
    if row["file"] and (e := next((e for e in catalog.Catalog.from_db(con).entries if e.path == row["file"]), None)):
        with filing.LOCK:
            filing.retire(con, paths, e, f"unreleased song deleted by hand: {row['artist']} – {row['title']}")
    for key in lists_in:
        _store(con, key, [(k, added) for k, added in _songs_of(con, key) if k != song])
    with con:
        con.execute("DELETE FROM list_songs WHERE song_key = ?", (song,))
        con.execute("DELETE FROM songs WHERE key = ?", (song,))
    return f"{row['artist']} – {row['title']}"


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
    _store(con, key, [*_songs_of(con, key), (song, _added())])
    return f"{label}: filed as {dest.relative_to(paths.tracks).as_posix()}{tag_note}", song


TWINS = 3  # library files named like an upload whose audio is held against it


@dataclass(frozen=True)
class Twin:
    """A library file an upload sounds like (twins): its path, its quality, the upload's audio against it
    (identity.against, on the preview's scales), what that says (verdict: 'same' the same master and mix,
    'version' the recording in another master, mix or edit, 'resembles' unclear), the unreleased song whose file
    it is (local: its key, else '') and whether the upload is the better copy."""

    path: str
    quality: str
    kbps: int
    fingerprint: float
    waveform: float | None
    verdict: str
    local: str
    better: bool


def _artists(e: catalog.Entry) -> set[str]:
    """An entry's artist keys: its folder's, an unreleased file's by its name ("<Artist> - <Title>")."""
    if catalog.unreleased(e.path):
        return rules.artist_keys(e.path.rpartition("/")[2].partition(" - ")[0])
    return e.akeys


def twins(con: sqlite3.Connection, paths: Paths, f: File, names: Names) -> list[Twin]:
    """The library files (unreleased ones too) the upload may be: named alike (the title's core, or the artist
    at a length within 3 s), the TWINS likeliest held against it by their audio; those it sounds like (a
    fingerprint above identity.OTHER), the likeliest first."""
    n = names.clean()
    if f.error or f.path is None or not (n.artist or n.title):
        return []
    core, artists = recordings.core(n.title), rules.artist_keys(n.artist) if n.artist else set()
    picks = []
    for e in catalog.Catalog.from_db(con).entries:
        named = len(core) >= 2 and recordings.core(e.title) == core
        near = bool(f.seconds) and abs(e.duration - f.seconds) <= 3
        if named or (near and artists & _artists(e)):
            picks.append((not (named and artists & _artists(e)), not near, abs(e.duration - f.seconds), e))
    owners = dict(con.execute("SELECT file, key FROM songs WHERE service = ? AND file IS NOT NULL", (LOCAL,)))
    mine = upload.worth(f.tier, f.kbps, f.source)
    out = []
    for *_, e in sorted(picks, key=lambda p: p[:3])[:TWINS]:
        lib = paths.tracks / e.path
        if not lib.is_file():
            continue
        # the shorter one's pieces held against the longer one (an edit is in its extended mix)
        fp, wave = identity.against(f.path, lib) if e.duration <= f.seconds + 3 else identity.against(lib, f.path)
        if fp is None or fp <= identity.OTHER:
            continue
        verdict = "version" if fp >= identity.SAME else "resembles"
        if verdict == "version" and wave is not None and wave >= identity.WAVE_SAME:
            verdict = "same"
        better = mine > upload.worth(e.quality, e.kbps, e.source)
        out.append(
            Twin(
                e.path,
                e.quality,
                e.kbps,
                round(fp, 2),
                wave and round(wave, 2),
                verdict,
                owners.get(e.path, ""),
                better,
            )
        )
    return sorted(out, key=lambda t: ("same", "version", "resembles").index(t.verdict))


def _note(con: sqlite3.Connection, rel: str | None) -> str:
    """The note an unreleased file was uploaded with (its event "new")."""
    sql = "SELECT reason FROM events WHERE path = ? AND action = 'new' ORDER BY id DESC"
    row = con.execute(sql, (rel,)).fetchone() if rel else None
    return (row[0] or "") if row else ""


def replace(
    con: sqlite3.Connection, paths: Paths, f: File, user_id: int, song: str, note: str = "", cover: bytes | None = None
) -> tuple[str, str | None]:
    """An uploaded file in place of the file of the unreleased song `song` (on one of the user's lists): a better
    copy (a friend's WAV of the MP3). It takes the song's names and its note (or `note`); the old file goes
    into inbox/replaced/<date>/ (kept KEEP_DAYS days). A line for the dialog and the song's key (None: not)."""
    row = con.execute("SELECT * FROM songs WHERE key = ? AND service = ?", (song, LOCAL)).fetchone()
    if row is None or not in_lists(con, user_id, song):
        return f"{f.name}: not imported (that unreleased song is gone)", None
    if f.path is None or not f.path.is_file() or f.error:
        return f"{f.name}: not imported (gone)", None
    label = f"{row['artist']} – {row['title']}"
    note = note or _note(con, row["file"])
    try:
        tagging.write_unreleased(f.path, row["artist"], row["title"], row["album"] or "", note)
    except Exception as e:  # (a format the tagger can't write: the file keeps its own tags)
        tag_note = f" (tags not written: {e})"
    else:
        tag_note = ""
    if cover:
        audio.embed_cover(f.path, cover)
    name = row["stem"].removeprefix(catalog.UNRELEASED + "/")
    info = {"source": "manual", "matched": "by hand", "song": song, "artist": row["artist"], "title": row["title"]}
    with filing.LOCK:
        entries = {e.path: e for e in catalog.Catalog.from_db(con).entries}
        old = entries.get(row["file"] or "")
        retired = (
            filing.retire(con, paths, old, f"replaced by a better copy of {label} uploaded by hand") if old else None
        )
        try:
            dest = filing.place_unreleased(con, paths, f.path, name, f.tier == "fake", f.source, reason=note, **info)
        except FileExistsError:
            if retired and old:
                filing.place_back(con, paths, retired, paths.tracks / old.path)
            return f"{label}: not imported ({catalog.UNRELEASED}/{name} is taken)", None
    with con:
        con.execute("UPDATE songs SET length = ? WHERE key = ?", (float(f.seconds or 0), song))
    was = f" (was {old.path.rpartition('/')[2]})" if old else ""
    return f"{label}: replaced by {dest.relative_to(paths.tracks).as_posix()}{was}{tag_note}", song
