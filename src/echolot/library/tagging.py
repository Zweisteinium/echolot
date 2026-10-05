"""A library file's tags come from the song it is, never from its uploader: the artists (ARTIST shows them,
ARTISTS lists them, the album artist is the main one), the title, the album (Spotify's; otherwise the file
keeps its own) and where it comes from: SOURCE, the songs' pages, and DOWNLOAD, "Soulseek" or the page it
was downloaded from. A close match is what it was named in review. The comment and the cover stay.

They are written when a song is filed (acquire.finish) and once for the whole library (normalize), which
backs the old tags up first, leaves a file alone whose tags name another song (it goes to review) and
changes nothing on a second run."""

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from echolot.library import rules

SOURCES = {"soulseek": "Soulseek", "youtube": "YouTube", "soundcloud": "SoundCloud", "manual": "by hand"}
SOURCES["soundcloud-search"] = "SoundCloud"
OLD_PAGE = re.compile(r"YouTube ([\w-]{11})")  # how an older pipeline noted a YouTube download
# where yt-dlp puts the page it downloaded (the video's description, links and all, goes elsewhere)
PAGE_FIELDS = {"vorbis": ["purl", "comment", "description"], "mp4": ["\xa9cmt"], "id3": ["COMM", "TXXX:purl"]}
LINK = re.compile(
    r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com/watch\?v=[\w-]+|youtu\.be/[\w-]+|soundcloud\.com/[^\s\"']+)"
)
# never part of a SoundCloud title: release decoration, not the song (version words stay)
_DECOR = r"free\s*(?:dl|d/l|download)|out\s*now|premiere|lyrics?(?:\s+video)?|visuali[sz]er|hq|hd|4k(?:\s+upgrade)?"
_OFFICIAL = r"official(?:\s+(?:music\s+)?(?:video|audio|visuali[sz]er))?"
_VERSION = r"(?:remix|edit|mix|vip|version|rework|bootleg|flip|live)\b"
JUNK = [
    rf"\s*[\(\[]\s*(?:{_DECOR}|{_OFFICIAL})\s*[\)\]]",
    rf"\s*\[\s*(?!{_VERSION})[A-Za-z]{{2,10}}\s?-?\d{{2,5}}\s*\]",  # catalogue numbers: [ARONAVA08], [HAK003]
    r"\s+[|•]\s.*$",
    # "[NOW ON SPOTIFY]", "( deleting soon save it on spotify )"; not a version ("(Spotify Singles)")
    r"\s*[\(\[](?![^\)\]]*\b(?:remix|edit|mix|vip|version|rework|bootleg|flip|live|singles?|sessions?|studios?)\b)[^\)\]]*\b(?:spotify|deleting|deleted)\b[^\)\]]*[\)\]]",
]
LAST_FILED = "SELECT source, url FROM events WHERE path = ? AND action IN ('new', 'upgrade', 'linked') ORDER BY id DESC"
# what players read beside or before the fields written (Navidrome's aliases): other spellings of the album
# artist, an album artist list, the artist as author and the uploader's sort names; they go when tags are written
STALE = {
    "vorbis": [
        *("album artist", "album_artist", "albumartists", "author", "artistsort", "artistssort"),
        *("albumartistsort", "albumartistssort", "titlesort", "albumsort"),
    ],
    "id3": ["TSOP", "TSO2", "TSOT", "TSOA", "TXXX:album artists", "TXXX:albumartists", "TXXX:albumartistsort"],
    "mp4": ["soar", "soaa", "sonm", "soal", "----:com.apple.iTunes:ALBUMARTISTS"],
}
FORMATS = ("flac", "ogg", "opus", "mp3", "wav", "m4a")  # what the writer handles (others keep their tags)
# a Spotify song's place on its release (songs.released, track, tracks, disc) and ISRC, as "isrc", "date",
# "track" ("n/total") and "disc"; with them go the uploader's spellings of the same fields (another release's)
FACT_FIELDS = ("isrc", "date", "track", "disc")
FACT_STALE = {
    "vorbis": ["year", "totaltracks", "tracktotal", "totaldiscs", "disctotal"],
    "id3": ["TYER", "TDAT"],
    "mp4": [],
}


@dataclass
class Tags:
    artists: list[str]
    albumartist: str
    title: str
    album: str | None  # None: the file keeps its own
    sources: list[str] = field(default_factory=list)
    download: str = ""  # "" when unknown: the file keeps what it has
    facts: dict[str, str] = field(default_factory=dict)  # the release's (FACT_FIELDS); empty: the file keeps its own

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)


def clean_title(title: str, artist: str) -> str:
    """A SoundCloud title without release decoration ("ANNIE - 10 out 10 [ARONAVA08]" -> "10 out 10")."""
    t = title
    for pat in JUNK:
        t = re.sub(pat, "", t, flags=re.I).strip()
    head, sep, rest = t.partition(" - ")
    if sep and rest and rules.artist_key(head) == rules.artist_key(artist):
        t = rest.strip()
    return t or title


NAMED = ("spotify", "youtube", "soundcloud")  # whose names a file takes first (Spotify's, YouTube Music's)


def page(song: sqlite3.Row) -> str:
    if song["service"] == "spotify":
        return f"https://open.spotify.com/track/{song['key'].split(':', 1)[1]}"
    return song["url"] or ""


def _songs(con: sqlite3.Connection, rel: str, key: str = "") -> list[sqlite3.Row]:
    """The songs a library file is (and the song `key` it was just filed for)."""
    sql = "SELECT * FROM songs WHERE file = ? OR stem = ? OR key = ?"
    return con.execute(sql, (rel, rel.rsplit(".", 1)[0], rules.norm_key(key))).fetchall()


def names(s: sqlite3.Row) -> list[str]:
    return [s["artist"], *json.loads(s["artists"] or "[]")]


def _lead(songs: list[sqlite3.Row], rel: str) -> sqlite3.Row:
    """The song a file's names and cover come from: a close match first, then Spotify's song (then a
    YouTube song) named as the file's folder, with the most artists, of the earliest release (the album
    before its compilations and anniversary editions)."""
    folder = rules.artist_keys(rel.partition("/")[0])

    def order(s: sqlite3.Row) -> tuple:
        named = bool(rules.artist_keys(s["artist"]) & folder)
        first = s["released"] or "9999"
        return not s["close_match"], NAMED.index(s["service"]), not named, -len(names(s)), first, s["key"]

    return sorted(songs, key=order)[0]


def lead(con: sqlite3.Connection, rel: str, key: str = "") -> sqlite3.Row | None:
    """The song whose names and cover a library file carries; None for a file no song has."""
    songs = _songs(con, rel, key)
    return _lead(songs, rel) if songs else None


def for_file(con: sqlite3.Connection, rel: str, path: Path, key: str = "") -> Tags | None:
    """The tags of a library file (path relative to tracks/), from the songs it is (and the song `key` it
    was just filed for); None for a file no song has. A close match is the name it was given; else
    Spotify's song, else SoundCloud's."""
    songs = _songs(con, rel, key)
    if not songs:
        return None
    lead = _lead(songs, rel)
    link = json.loads(lead["link"]) if lead["close_match"] and lead["link"] else None
    if link:
        artists, title, album = [link[0]], link[1], None
    elif lead["service"] != "soundcloud":  # songs sharing a file are one recording: all their artists
        same = [s for s in songs if s["service"] != "soundcloud" and not s["close_match"]]
        artists = list(dict.fromkeys([*names(lead), *(a for s in same for a in names(s))]))
        title, album = lead["title"], lead["album"] or None
    else:
        artists, title, album = [lead["artist"]], clean_title(lead["title"], lead["artist"]), None
    sources = [p for p in dict.fromkeys(page(s) for s in songs) if p]
    return Tags(artists, artists[0], title, album, sources, download_of(con, rel, path, songs), facts(lead))


def facts(song: sqlite3.Row) -> dict[str, str]:
    """The release facts a file of this song gets: a Spotify release's (also a YouTube song's that Spotify
    has), never a close match's (another version) or a SoundCloud upload's."""
    if song["service"] == "soundcloud" or song["close_match"] or not song["released"]:
        return {}
    track = f"{song['track']}/{song['tracks']}" if song["track"] and song["tracks"] else str(song["track"] or "")
    out = {
        "isrc": (song["isrc"] or "").upper(),
        "date": song["released"],
        "track": track,
        "disc": str(song["disc"] or ""),
    }
    return {k: v for k, v in out.items() if v}


def download_of(con: sqlite3.Connection, rel: str, path: Path, songs: list[sqlite3.Row]) -> str:
    """Where a file was downloaded from, by the best evidence: the link Echolot recorded, the page yt-dlp
    left in its tags, the source Echolot recorded, a SoundCloud song's own download; a FLAC or a file with
    an uploader's album tags came from Soulseek. '' when nothing tells."""
    e = con.execute(LAST_FILED, (rel,)).fetchone()
    if e and e["url"]:
        return e["url"]
    existing = read(path)
    if existing["page"] and (not e or e["source"] != "soulseek"):
        return existing["page"]
    if e and e["source"] in SOURCES:
        return SOURCES[e["source"]]
    if existing["download"]:
        return existing["download"]
    own = [s for s in songs if s["service"] == "soundcloud" and s["stem"] == rel.rsplit(".", 1)[0]]
    if own:
        return own[0]["url"] or "SoundCloud"
    if path.suffix.lower() == ".flac":
        return "Soulseek"
    if (existing["album"] or existing["track"]) and "lavf" not in existing["text"].lower():
        return "Soulseek"  # an uploader's album tags; one remuxed by ffmpeg (yt-dlp) has them too: unknown
    return ""


# ---------------------------------------------------------------- reading and writing


def _raw(path: Path) -> Any:
    from mutagen import File

    return File(path)


def read(path: Path) -> dict[str, Any]:
    """What a file's tags hold, in Tags terms, plus 'page': the page yt-dlp downloaded it from, 'stale': the
    STALE fields it has, and 'text': every text value."""
    out: dict[str, Any] = {"artists": [], "albumartist": "", "title": "", "album": "", "sources": [], "download": ""}
    out |= {"track": "", "facts": {}, "page": "", "stale": {}, "text": ""}
    try:
        m = _raw(path)
    except Exception:
        return out
    t = m.tags if m is not None else None
    if t is None:
        return out
    get = _getter(t)
    artists = get("ARTISTS") or get("ARTIST")
    out |= {"artists": artists, "albumartist": " ".join(get("ALBUMARTIST")), "title": " ".join(get("TITLE"))}
    out |= {"album": " ".join(get("ALBUM")), "sources": get("SOURCE"), "download": " ".join(get("DOWNLOAD"))}
    out["track"] = " ".join(get("TRACK"))
    out["facts"] = _read_facts(t)
    out["text"] = " ".join(str(v) for v in _values(t))
    out["page"] = _page(t)
    out["stale"] = _stale(t)
    return out


ID3 = {"ARTIST": "TPE1", "ALBUMARTIST": "TPE2", "TITLE": "TIT2", "ALBUM": "TALB", "TRACK": "TRCK"}
MP4 = {"ARTIST": "\xa9ART", "ALBUMARTIST": "aART", "TITLE": "\xa9nam", "ALBUM": "\xa9alb", "TRACK": "trkn"}
MP4_FREE = "----:com.apple.iTunes:"


def _kind(t: Any) -> str:
    """id3 (MP3, WAV), mp4 (M4A) or vorbis (FLAC, Ogg, Opus)."""
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4Tags

    return "id3" if isinstance(t, ID3) else "mp4" if isinstance(t, MP4Tags) else "vorbis"


def _getter(t: Any) -> Callable[[str], list[str]]:
    kind = _kind(t)

    def get(key: str) -> list[str]:
        if kind == "id3":
            frames = t.getall(ID3[key]) if key in ID3 else t.getall(f"TXXX:{key}")
            return [str(v) for f in frames for v in getattr(f, "text", [])]
        if kind == "mp4":
            if key in MP4:
                return [str(v) if not isinstance(v, tuple) else str(v[0]) for v in t.get(MP4[key], [])]
            return [bytes(v).decode("utf-8", "replace") for v in t.get(MP4_FREE + key, [])]
        return [str(v) for v in t.get(key.lower(), []) or t.get(key, [])]

    return get


def _read_facts(t: Any) -> dict[str, str]:
    """The file's release facts in FACT_FIELDS form ("track": "n/total"; only those it has)."""
    kind = _kind(t)
    if kind == "id3":
        text = {k: str(t[k].text[0]) if k in t and t[k].text else "" for k in ("TSRC", "TDRC", "TRCK", "TPOS")}
        out = {"isrc": text["TSRC"], "date": text["TDRC"], "track": text["TRCK"], "disc": text["TPOS"].split("/")[0]}
    elif kind == "mp4":
        n, total = (t.get("trkn") or [(0, 0)])[0]
        isrc = [bytes(v).decode("utf-8", "replace") for v in t.get(MP4_FREE + "ISRC", [])]
        out = {"isrc": isrc[0] if isrc else "", "date": str((t.get("\xa9day") or [""])[0])}
        out |= {
            "track": f"{n}/{total}" if n and total else str(n or ""),
            "disc": str((t.get("disk") or [(0, 0)])[0][0] or ""),
        }
    else:
        first = lambda k: str((t.get(k) or [""])[0])  # noqa: E731
        n, total = first("tracknumber"), first("tracktotal") or first("totaltracks")
        track = n if "/" in n or not total else f"{n}/{total}"
        out = {"isrc": first("isrc"), "date": first("date"), "track": track, "disc": first("discnumber").split("/")[0]}
    return {k: v.strip() for k, v in out.items() if v and v.strip() not in ("0", "")}


def _page(t: Any) -> str:
    """The first of yt-dlp's page fields that is a link and nothing else."""
    kind = _kind(t)
    for key in PAGE_FIELDS[kind]:
        values = [v for f in t.getall(key) for v in f.text] if kind == "id3" else t.get(key, [])
        for v in map(str, values):
            if LINK.fullmatch(v.strip()):
                return v.strip()
            if m := OLD_PAGE.fullmatch(v.strip()):
                return f"https://www.youtube.com/watch?v={m[1]}"
    return ""


def _stale(t: Any) -> dict[str, list[str]]:
    """The STALE fields of the tags, with their values (names compared ignoring case)."""
    kind = _kind(t)
    names = {k.lower() for k in STALE[kind]}
    out = {}
    for key in [k for k in t.keys() if k.lower() in names]:  # noqa: SIM118 (Vorbis comments iterate as pairs)
        values = t[key].text if kind == "id3" else t[key]
        out[key] = [bytes(v).decode("utf-8", "replace") if isinstance(v, bytes) else str(v) for v in values]
    return out


def _values(t: Any) -> list[Any]:
    kind = _kind(t)
    if kind == "id3":
        return [v for f in t.values() for v in (getattr(f, "text", None) or [getattr(f, "url", "")])]
    if kind == "mp4":
        return [v for vs in t.values() for v in (vs if isinstance(vs, list) else [vs])]
    return [v for _, vs in t.items() for v in vs]


def differs(path: Path, tags: Tags) -> list[str]:
    """The fields that writing these tags would change (empty: nothing to do)."""
    now = read(path)
    album = tags.album if tags.album is not None else now["album"]
    wanted = {"artists": tags.artists, "albumartist": tags.albumartist, "title": tags.title, "album": album}
    wanted |= {"sources": tags.sources, "download": tags.download or now["download"]}
    fields = [name for name, value in wanted.items() if now[name] != value] + (["stale"] if now["stale"] else [])
    return fields + [k for k, v in tags.facts.items() if now["facts"].get(k) != v]


def write(path: Path, tags: Tags) -> None:
    """Write the tags; the comment, the cover and every other field stay (but the uploader's spellings of
    the release facts, FACT_STALE, when the song has them)."""
    m = _raw(path)
    if m is None:
        raise ValueError(f"not an audio file mutagen knows: {path.name}")
    if m.tags is None:
        m.add_tags()
    t, kind = m.tags, _kind(m.tags)
    for key in _stale(t):
        del t[key]
    if tags.facts:
        _write_facts(t, kind, tags.facts)
    values = {"ARTIST": [tags.artist], "ARTISTS": tags.artists, "ALBUMARTIST": [tags.albumartist]}
    values |= {"TITLE": [tags.title], "SOURCE": tags.sources}
    if tags.album is not None:
        values["ALBUM"] = [tags.album]
    if tags.download:
        values["DOWNLOAD"] = [tags.download]
    if kind == "id3":
        from mutagen.id3 import TALB, TIT2, TPE1, TPE2, TXXX, WOAS

        frames = {"ARTIST": TPE1, "ALBUMARTIST": TPE2, "TITLE": TIT2, "ALBUM": TALB}
        for key, vals in values.items():
            if key in frames:
                t.setall(frames[key].__name__, [frames[key](encoding=3, text=vals)])
            else:
                t.setall(f"TXXX:{key}", [TXXX(encoding=3, desc=key, text=vals)] if vals else [])
        t.setall("WOAS", [WOAS(url=tags.sources[0])] if tags.sources else [])
    elif kind == "mp4":
        from mutagen.mp4 import MP4FreeForm

        for key, vals in values.items():
            if key in MP4:
                t[MP4[key]] = vals
            elif vals:
                t[MP4_FREE + key] = [MP4FreeForm(v.encode("utf-8")) for v in vals]
            else:
                t.pop(MP4_FREE + key, None)
    else:
        for key, vals in values.items():
            if vals:
                t[key] = vals
            elif key in t:
                del t[key]
    m.save()


def _write_facts(t: Any, kind: str, facts: dict[str, str]) -> None:
    """The release facts (FACT_FIELDS form) into tags of `kind`; the uploader's other spellings go."""
    for key in [k for k in t.keys() if k.lower() in {s.lower() for s in FACT_STALE[kind]}]:  # noqa: SIM118
        del t[key]
    n, _, total = facts.get("track", "").partition("/")
    if kind == "id3":
        from mutagen.id3 import TDRC, TPOS, TRCK, TSRC

        for frame, key in ((TSRC, "isrc"), (TDRC, "date"), (TRCK, "track"), (TPOS, "disc")):
            if facts.get(key):
                t.setall(frame.__name__, [frame(encoding=3, text=[facts[key]])])
    elif kind == "mp4":
        from mutagen.mp4 import MP4FreeForm

        if facts.get("isrc"):
            t[MP4_FREE + "ISRC"] = [MP4FreeForm(facts["isrc"].encode("utf-8"))]
        if facts.get("date"):
            t["\xa9day"] = [facts["date"]]
        if n:
            t["trkn"] = [(int(n), int(total or 0))]
        if facts.get("disc"):
            t["disk"] = [(int(facts["disc"]), 0)]
    else:
        values = {
            "isrc": facts.get("isrc"),
            "date": facts.get("date"),
            "tracknumber": n,
            "discnumber": facts.get("disc"),
        }
        values["tracktotal"] = total
        for key, value in values.items():
            if value:
                t[key] = [value]


def backup_line(rel: str, path: Path) -> str:
    """One JSON line with a file's current tags (to undo a normalize run)."""
    now = read(path)
    now.pop("text")
    return json.dumps({"file": rel, **now}, ensure_ascii=False)


def conflict(current: str, wanted: str) -> bool:
    """The tags' title names another song: no word of the wanted title's core in it, nor in either side of
    an "A - B" title ("Artist - Title", "Title - Original Mix", "Title - Artist"), and the other way round
    ("I Want It" for "Come & Go (with Marshmello)"). Decoration or a missing title is no conflict."""
    if not current.strip():
        return False
    want = set(rules.words(rules.title_key(rules.release_title(wanted))).split())
    if not want:
        return False
    parts = [current, *current.split(" - ", 1)] if " - " in current else [current]
    return not any(want & set(rules.words(rules.title_key(part)).split()) for part in parts)


def supported(path: Path) -> bool:
    return path.suffix.lower().lstrip(".") in FORMATS


# ---------------------------------------------------------------- the whole library


def normalize(con: sqlite3.Connection, tracks: Path, *, dry_run: bool, backup: Path | None, limit: int = 0) -> dict:
    """Give every library file the tags of its song. A dry run only reports. Otherwise each file's old tags
    are appended to `backup` before it is written. Files whose tags name another song are left alone and
    reported as conflicts (with their song keys, for review); files no song has keep their tags."""
    report: dict[str, Any] = {"files": 0, "unchanged": 0, "changed": 0, "fields": {}, "no song": 0}
    report |= {"not supported": 0, "conflicts": [], "errors": [], "examples": []}
    files = sorted(p for p in tracks.glob("*/*") if p.is_file() and not p.name.startswith("."))
    files = [p for p in files if p.suffix.lower().lstrip(".") in (*FORMATS, "aiff", "webm", "aac")]
    out = backup.open("a", encoding="utf-8") if backup and not dry_run else None
    try:
        for p in files[:limit] if limit else files:
            report["files"] += 1
            rel = p.relative_to(tracks).as_posix()
            if not supported(p):
                report["not supported"] += 1
                continue
            tags = for_file(con, rel, p)
            if tags is None:
                report["no song"] += 1
                continue
            now = read(p)
            if not now["download"] and conflict(now["title"], tags.title):
                keys = [r[0] for r in con.execute("SELECT key FROM songs WHERE file = ?", (rel,))]
                report["conflicts"].append({"file": rel, "keys": keys, "tags": now["title"], "song": tags.title})
                continue
            changes = differs(p, tags)
            if not changes:
                report["unchanged"] += 1
                continue
            report["changed"] += 1
            for name in changes:
                report["fields"][name] = report["fields"].get(name, 0) + 1
            if len(report["examples"]) < 25:
                before = {k: now[k] for k in ("artists", "albumartist", "title", "album", "download")}
                report["examples"].append({"file": rel, "changes": changes, "before": before, "after": tags.__dict__})
            if out:
                out.write(backup_line(rel, p) + "\n")
                out.flush()
                try:
                    write(p, tags)
                except Exception as e:
                    report["errors"].append({"file": rel, "error": str(e)})
    finally:
        if out:
            out.close()
    return report
