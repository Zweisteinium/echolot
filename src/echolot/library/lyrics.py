"""A song's lyrics from LRCLIB, only when they are surely its own. Anyone can add an entry there, and one song
may have several (each with its own length), so an entry counts only when
- it names the song: one of the names it credits is one of the song's artists (not just contains one: a
  "Radiohead Tribute Band" is not Radiohead), and the title is the same once both leave out their noise and
  what a release adds (" - Radio Edit", "(feat. X)"), with the same version words and featured artists;
  another version ("Live", "Remix", "Acoustic", "A Cappella") is another song here
- it is as long as the file, within LENGTH (an edit or an extended mix has its own lines and times)
- its synced lines all fall inside the file (lines past its end are another recording's).
Asked by the song's names (artist credit, title, album, the file's length), then by its first artist and the
title without what a release adds, then a search of those, the nearest fitting entry first. Synced lyrics
(LRC) are kept over plain ones; an instrumental entry is an answer too (nothing to write)."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from echolot.library import rules
from echolot.services import lrclib

LENGTH = 2.0  # s: an entry further from the file's length is of another recording (LRCLIB's own tolerance)
_STAMP = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_CREDIT = re.compile(r"\s*[,;/&]\s*|\s+(?:x|feat\.?|ft\.?|featuring|with|vs\.?|and|und|y|e|et)\s+", re.I)
_FEAT = re.compile(r"\s*[,;/]\s*|\s+(?:x|feat\.?|ft\.?|featuring|with|vs\.?)\s+", re.I)  # "&" kept: one name
_TOPIC = re.compile(r"\s+-\s+topic$", re.I)  # YouTube's channel of an artist: "Reinier Zonneveld - Topic"
_HEADER = re.compile(r"^(?:writers?\s*(?:\(s\))?|written by|composers?|lyrics by|songwriters?|miscellaneous)\b", re.I)
_PLACEHOLDER = re.compile(r"^[\s\[\(]*(?:instrumental|instrumentale?|no lyrics|♪+)[\s\]\)]*$", re.I)


@dataclass(frozen=True)
class Lyrics:
    kind: str  # synced, plain or instrumental
    text: str  # LRC lines, plain lines, or '' (instrumental)
    source: str  # "lrclib:<entry id>"


def find(song: Mapping[str, Any], length: float, album: str | None = None) -> Lyrics | None:
    """The song's lyrics for a file of `length` seconds (`song`: a songs row; `album`: its own release's, else
    the list's); None if LRCLIB has none that surely are (fits). Raises LrclibError when LRCLIB fails."""
    if not length or rules.mix_cut(song["title"]):  # a cut from a DJ mix has no lyrics of its own length
        return None
    artists = json.loads(song["artists"] or "[]") or [song["artist"]]
    credit, title = ", ".join(artists), song["title"]
    loose = rules.search_title(title)
    tries = [(credit, title, album or song["album"])]
    if (song["artist"], loose) != (credit, title):
        tries.append((song["artist"], loose, None))
    for artist, name, on in tries:
        entry = lrclib.get(artist, name, on, length)
        if entry and fits(song, entry, length) is None and (found := _lyrics(song, entry)):
            return found
    entries = [e for e in lrclib.search(song["artist"], loose) if fits(song, e, length) is None]
    release = rules.title_key(album or song["album"] or "")

    def order(e: Mapping[str, Any]) -> tuple:  # the release's own album first, then synced, then the nearest
        elsewhere = bool(release) and rules.title_key(e.get("albumName") or "") != release
        return elsewhere, not (e.get("syncedLyrics") or "").strip(), abs(float(e["duration"]) - length)

    entries.sort(key=order)
    return next((found for e in entries if (found := _lyrics(song, e))), None)


def fits(song: Mapping[str, Any], entry: Mapping[str, Any], length: float) -> str | None:
    """Why an LRCLIB entry is not the song's lyrics for a file of `length` seconds; None if it is."""
    entry_length = float(entry.get("duration") or 0)
    if not entry_length or abs(entry_length - length) > LENGTH:
        return f"{entry_length:.0f} s long, the file {length:.0f} s"
    artist, name = entry.get("artistName") or "", entry.get("trackName") or ""
    if not credited(song, artist):
        return f"by '{artist}', not by the song's artists"
    if not same_title(song["title"], name):
        return f"'{name}' is not '{song['title']}'"
    stamps = [_seconds(m) for m in _STAMP.finditer(entry.get("syncedLyrics") or "")]
    if stamps and max(stamps) > length + LENGTH:
        return f"its synced lines run to {max(stamps):.0f} s, past the file's end ({length:.0f} s)"
    return None


def credited(song: Mapping[str, Any], credit: str) -> bool:
    """One of the names `credit` lists ("A, B", "A & B", "A feat. B") is one of the song's artists (or its
    English name), whole: "Above" is not "Above & Beyond". A song's name that is several ("A & B" as one
    string) counts when the credit lists all of them."""
    names = [n for n in (song["artist"], *json.loads(song["artists"] or "[]"), dict(song).get("artist_alias")) if n]
    credit = _TOPIC.sub("", credit)
    listed = {rules.artist_key(n) for n in [credit, *_CREDIT.split(credit), *_FEAT.split(credit)]} - {""}
    parts = [{rules.artist_key(p) for p in _CREDIT.split(n)} - {""} for n in names]
    return bool({rules.artist_key(n) for n in names} & listed) or any(len(p) > 1 and p <= listed for p in parts)


def same_title(a: str, b: str) -> bool:
    """The same title once both leave out their noise and what a release adds (" - Radio Edit", "(Single
    Version)", "(feat. X)"), with the same version words and featured artists."""
    versions = (set(rules.words(a).split()) ^ set(rules.words(b).split())) & rules.VERSION_WORDS
    return not versions and rules.title_key(_plain(a)) == rules.title_key(_plain(b)) and rules.same_feat(a, b)


def _plain(title: str) -> str:
    """The title without its segments of plain words only (rules.PLAIN_WORDS, years) and featured artists."""
    head, segs = rules.segments(rules.search_title(title))
    keep = [s for s in segs if not all(w in rules.PLAIN_WORDS or w.isdigit() for w in rules.words(s).split())]
    return " ".join([head, *keep])


def _lyrics(song: Mapping[str, Any], entry: Mapping[str, Any]) -> Lyrics | None:
    source = f"lrclib:{entry['id']}"
    synced = _tidy(entry.get("syncedLyrics") or "")
    plain = _headless(song, _tidy(entry.get("plainLyrics") or ""))
    if entry.get("instrumental") or (_PLACEHOLDER.match(plain) and not synced):
        return Lyrics("instrumental", "", source)
    if synced and _STAMP.search(synced):
        return Lyrics("synced", synced, source)
    return Lyrics("plain", plain, source) if plain else None


def _headless(song: Mapping[str, Any], text: str) -> str:
    """Plain lyrics without a lyrics site's header ("Writer(s): …" or "Miscellaneous" in the first lines, with
    the artist's and the song's name around it) before the first line sung; other lyrics as they are (a song
    may start with its title)."""
    lines = text.split("\n")
    if not any(_HEADER.match(line.strip()) for line in lines[:4]):
        return text
    names = {rules.title_key(song["artist"]), rules.title_key(song["title"]), rules.title_key(song["album"] or "")}
    while lines and (_HEADER.match(lines[0].strip()) or rules.title_key(lines[0]) in names - {""} or not lines[0]):
        lines.pop(0)
    return "\n".join(lines).strip()


def _tidy(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()


def _seconds(m: re.Match) -> float:
    fraction = m.group(3) or "0"
    return int(m.group(1)) * 60 + int(m.group(2)) + int(fraction) / 10 ** len(fraction)
