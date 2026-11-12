"""Files uploaded by hand: for missing songs, or better copies of library songs (the upload dialog on Missing,
the Overview and the list pages). They wait in inbox/upload/<batch>/, each checked once (audio.prepare; its
length, quality and tags kept beside it as <n>.json). Each file's song is found among the user's songs, a
missing one first; how it fits (length, quality, the audio against the release's) is shown for information.
Imported, a file is that song whatever its names or length (as Perfect match in review): a missing song gets
it, a library song only a better copy (compare); the download source is "by hand". The rest of the batch is
deleted, as is a batch left alone for KEEP_SECONDS."""

import dataclasses
import json
import re
import secrets
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from echolot.library import audio, catalog, discover, filing, identity, origin, recordings, rules
from echolot.library.filing import Paths, Want
from echolot.settings.sources import WISHED_TITLE

if TYPE_CHECKING:
    from echolot.settings.vault import Vault

KEEP_SECONDS = 24 * 3600  # a batch nobody imported or cancelled is deleted after this
QUIETER = 3.0  # dB (RMS) under your copy from which an upload is no better: recorded at a lower volume
QUIET_PEAK = -9.0  # dBFS: a file peaking lower is flagged (releases peak near 0; recorded at 25 %: -12)
BATCH = re.compile(r"[0-9a-f]{16}")


@dataclass
class File:
    """An uploaded file as checked: n (its place in the batch), name (as uploaded), path (after prepare), tier
    (catalog.QUALITY), tags (artist, title; the Spotify track and ISRC a recorder or tagger wrote), error (no
    usable audio), what the spectrum check saw (source, band), levels (peak, rms: dBFS)."""

    n: int
    name: str
    path: Path | None = None
    seconds: float = 0
    kbps: int = 0
    tier: str = ""
    artist: str = ""
    title: str = ""
    error: str = ""
    source: str = ""  # a fake FLAC: the lossy file it was made from, as the lossy checks estimate it
    band: int = 0  # Hz up to which it has sound (a lossy encoder's edge; 0: none, its full range)
    peak: float | None = None
    rms: float | None = None
    spotify_id: str = ""
    isrc: str = ""


@dataclass
class Fit:
    """A file as a song: its length off the song's (s), and its audio against the release's preview and your
    copy (identity.against): the fingerprint's share of equal bits (the recording from identity.SAME; another
    song about 0.55) and the waveform's correlation (the same master and mix from identity.WAVE_SAME); None:
    unknown."""

    diff: float | None
    fingerprint: float | None = None
    waveform: float | None = None
    copy_fingerprint: float | None = None
    copy_waveform: float | None = None


def folder(paths: Paths, batch: str) -> Path:
    if not BATCH.fullmatch(batch):
        raise ValueError("no such upload")
    return paths.inbox("upload") / batch


def start(paths: Paths) -> str:
    """A new, empty batch (the dialog opened); files come one by one (add)."""
    purge(paths)
    batch = secrets.token_hex(8)
    folder(paths, batch).mkdir(parents=True)
    return batch


_NUMBER = threading.Lock()  # files added at the same time get numbers of their own


def add(paths: Paths, batch: str, name: str, fileobj: Any, keep_hires: bool = False) -> File:
    """Save an uploaded file into the batch as its next number and check it (audio.prepare, length, quality,
    tags). ValueError when the batch is gone (cancelled)."""
    d = folder(paths, batch)
    with _NUMBER:
        if not d.is_dir():
            raise ValueError("no such upload")
        taken = (re.match(r"\d+", p.name) for p in d.iterdir())
        n = 1 + max((int(m.group()) for m in taken if m), default=0)
        base = Path(name or "file").name
        stem, ext = re.sub(r"[^\w\-. ()&',!]+", "_", Path(base).stem)[:120] or "file", Path(base).suffix.lower()
        dest = d / f"{n:02d} {stem}{ext}"
        dest.touch()  # (the number is taken)
    with dest.open("wb") as out:
        shutil.copyfileobj(fileobj, out)
    f = File(n, base)
    try:
        prepared = audio.prepare(dest, keep_hires)
        f.path = prepared.path
        f.seconds, f.kbps = audio.probe(prepared.path)
        f.tier = tier(prepared.path, f.kbps, prepared.fake)
        f.source, f.band = (prepared.spectrum or {}).get("source", ""), _band(prepared.spectrum)
        artists, f.title = audio.read_tags(prepared.path)
        f.artist = ", ".join(dict.fromkeys(artists))
        f.peak, f.rms = audio.levels(prepared.path)
        f.spotify_id, f.isrc = audio.read_spotify_id(prepared.path), audio.read_isrc(prepared.path).upper()
    except audio.Rejected as e:
        f.error = str(e)
    if d.is_dir():  # (cancelled meanwhile: nothing to keep)
        _save(d, f)
    return f


def tier(path: Path, kbps: int, fake: bool) -> str:
    """The quality tier of a file (catalog.QUALITY), as the library counts it."""
    return catalog.Entry(f"-/-{path.suffix}", 0, kbps, fake).quality


def files(paths: Paths, batch: str) -> list[File]:
    d = folder(paths, batch)
    out = []
    # only the batch's own records ("01.json"): an upload named "x.json" is saved as "01 x.json"; and only
    # files inside the batch, whatever a record says
    for p in sorted(p for p in d.glob("*.json") if re.fullmatch(r"\d+\.json", p.name)) if d.is_dir() else []:
        raw = json.loads(p.read_text())
        path = Path(raw["path"]) if raw.get("path") else None
        raw["path"] = path if path and path.resolve().parent == d.resolve() else None
        out.append(File(**{k: v for k, v in raw.items() if k in _FIELDS}))  # (a field gone since: dropped)
    return out


_FIELDS = {f.name for f in dataclasses.fields(File)}


def _save(d: Path, f: File) -> None:
    raw = dataclasses.asdict(f) | {"path": str(f.path) if f.path else None}
    (d / f"{f.n:02d}.json").write_text(json.dumps(raw))


def tagged(con: sqlite3.Connection, f: File, songs: dict[str, sqlite3.Row]) -> str | None:
    """The song a file's tags name exactly: its Spotify track (the song's own, or the release Spotify plays it
    as: availability replaced), else its ISRC (a missing song first). None: no such tag or song."""
    if f.error:
        return None
    if f.spotify_id:
        if (key := f"spotify:{f.spotify_id}") in songs:
            return key
        sql = "SELECT song_key FROM availability WHERE state = 'replaced' AND detail = ?"
        for (key,) in con.execute(sql, (f"spotify:{f.spotify_id}",)):
            if key in songs:
                return key
    if f.isrc:
        same = [s for s in songs.values() if (s["isrc"] or "").upper() == f.isrc]
        if same:
            return max(same, key=needs)["key"]
    return None


def guess(f: File, songs: list[sqlite3.Row]) -> str | None:
    """The song a file most likely is: by its tags and file name (rules.identify), then by its title and artist
    anywhere in them; missing songs first, then the closest in length. Only songs whose core title is in the
    file's names count; None when nothing names one."""
    if f.error:
        return None
    stem = re.sub(r"^\d{2} ", "", Path(f.path).stem if f.path else f.name)
    text = f" {rules.title_key(f.title)} {rules.title_key(stem)} "
    best: tuple[float, bool, float, str] | None = None
    for s in songs:
        core = rules.title_key(rules.segments(s["title"])[0])
        if not core or f" {core} " not in text:
            continue
        match, _ = rules.identify(s["artist"], s["title"], [f.artist], f.title, stem, [], f.seconds, s["length"], 6)
        score = {"exact": 3, "probable": 2}.get(match or "", 0)
        if not score and any(f" {w} " in rules.words(f"{f.artist} {stem}") for w in rules.artist_words(s["artist"])):
            score = 1  # the title and the artist somewhere (never another artist's song of that title)
        if score:
            cand = (score, needs(s), -abs((f.seconds or 0) - (s["length"] or 0)), s["key"])
            best = max(best, cand) if best else cand
    return best[3] if best else _by_title(f, stem, songs)


def _by_title(f: File, stem: str, songs: list[sqlite3.Row]) -> str | None:
    """A file that names no artist (no tags, a name of the title alone): the one song whose whole title it has,
    version words and all, within 2 s. Two such songs: none (never a guess)."""
    if not f.seconds or f.artist or " - " in stem:  # (an artist named: guess's rules, another's title is not his)
        return None
    names = {k for k in (rules.title_key(f.title), rules.title_key(stem)) if k}
    fits = [s for s in songs if rules.title_key(s["title"]) in names and abs(f.seconds - (s["length"] or 0)) <= 2]
    return fits[0]["key"] if len(fits) == 1 else None


def names(f: File) -> tuple[str, str]:
    """The artist and title a file names: its tags, else its file name ("Artist - Title", without a track
    number); ('', '') when neither does."""
    if f.artist and f.title:
        return f.artist, f.title
    stem = re.sub(r"^\d{2} ", "", Path(f.path).stem if f.path else Path(f.name).stem)  # (its number in the batch)
    stem = re.sub(r"^\d{1,3}\s*[-._)]\s*", "", stem)  # "01 - ", "01. "
    artist, sep, title = stem.partition(" - ")
    if sep and artist.strip() and title.strip():
        return f.artist or artist.strip(), f.title or title.strip()
    return f.artist, f.title


@dataclass
class Release:
    """What the release check found of a file (release): the release (its names, album, year, cover and where
    it is: discover.Result), how the file fits its 30 s preview (fit: the length off it, fingerprint,
    waveform) and what that says (verdict): 'released' (the release's recording, at the release's length),
    'resembles' (the recording in another edit or length, or unclear) or '' (named so, its audio not
    compared: no preview)."""

    result: discover.Result
    fit: Fit
    verdict: str


CANDIDATES = 3  # releases named as the file whose previews are held against it (a request each)
NEAR = 6.0  # s: a file this much off the release's length (or 3 %) is the release's edit


def release(f: File, artist: str, title: str, search: Any, loose: bool = False) -> Release | None:
    """Is the file a release? A catalogue search (`search`: a query -> discover.Hit list, Deezer's, which
    gives previews) for its names (`loose`: by its title alone too when that finds none: released under
    another artist name); the CANDIDATES first results with the file's core title, each one's preview held
    against the file (the fingerprint: the recording; then the best one's waveform: the master and mix).
    None: nothing named so, or only other recordings (fingerprint OTHER or less); the search's errors are
    raised (catalogs.CatalogError)."""
    if f.error or f.path is None or not artist.strip() or not title.strip():
        return None
    core = rules.title_key(rules.segments(title)[0])
    named: list[discover.Result] = []
    for query in (f"{artist} {title}", title) if loose else (f"{artist} {title}",):
        results = [r for r in discover.merge(search(query)) if r.seconds]
        named = [r for r in results if core and rules.title_key(rules.segments(r.title)[0]) == core][:CANDIDATES]
        if named:
            break
    mine = identity.fingerprint(f.path)
    best: tuple[tuple[bool, float], discover.Result, float, bytes] | None = None
    for r in named:
        url = next((h.preview for h in r.sources if h.preview), "")
        clip = identity.clip(url) if url and mine is not None and len(mine) else None
        ref = identity.fingerprint(clip) if clip else None
        if ref is None or not len(ref):
            continue
        fp = round(identity.similarity(ref, mine), 2)
        rank = (fp >= identity.SAME and _near(f.seconds, r.seconds), fp)
        if best is None or rank > best[0]:
            best = (rank, r, fp, clip)
    if best is None:  # no preview to compare: by its names only
        return Release(named[0], Fit(_diff(f.seconds, named[0].seconds)), "") if named else None
    (released, _), r, fp, clip = best
    if fp <= identity.OTHER:
        return None
    wave = identity.waveform(f.path, clip)
    fit = Fit(_diff(f.seconds, r.seconds), fp, round(wave, 2) if wave is not None else None)
    return Release(r, fit, "released" if released else "resembles")


def _near(seconds: float, length: float) -> bool:
    return not seconds or not length or abs(seconds - length) <= max(NEAR, 0.03 * length)


def _diff(seconds: float, length: float) -> float | None:
    return round(seconds - length) if seconds and length else None


def have_as(con: sqlite3.Connection, paths: Paths, f: File, title: str) -> str:
    """The library file that is the same audio as the upload (a like title and length: recordings.already),
    as its path; '' when there is none."""
    if f.error or f.path is None or not title:
        return ""
    e = recordings.already(paths, f.path, title, f.seconds, recordings.Index(catalog.Catalog.from_db(con)))
    return e.path if e else ""


def needs(song: sqlite3.Row) -> bool:
    """A song without its own file: missing, or covered by a close match (another version)."""
    return not song["file"] or bool(song["close_match"])


def copy_label(song: sqlite3.Row) -> str:
    """What the library has of a song: "FLAC", "fake FLAC", "160 kbps"."""
    return {"lossless": "FLAC", "fake": "fake FLAC"}.get(song["quality"] or "", f"{song['kbps'] or '?'} kbps")


# what a lossy file's quality is worth in kbps, for a fake FLAC by the source the spectrum check estimates
SOURCE_KBPS = {"~128 kbps": 128, "~160-192 kbps": 176, "~256 kbps / V0": 256}
MORE = 1.25  # a better lossy copy has a quarter more
SLACK = 1000  # Hz: a band less lower is the same (above 19 kHz a few hundred Hz go unheard; 128 kbps stops at 16)


def worth(tier: str, kbps: int, source: str = "") -> float:
    """A copy's quality as kbps: genuine lossless beyond any, a fake FLAC its source's (lossprint's: "AAC ~258
    kbps"; unknown: 128)."""
    if tier == "lossless":
        return float("inf")
    if tier == "fake":
        guessed = re.fullmatch(r"\w+ ~(\d+) kbps", source)
        return SOURCE_KBPS.get(source) or (int(guessed.group(1)) if guessed else 128)
    return kbps or 0


def _band(spectrum: dict | None) -> int:
    """Hz up to which a file has sound: a steep edge's (a lossy encoder's), else 0 (its full range)."""
    s = spectrum or {}
    steep = s.get("verdict") == "lossy" or (s.get("drop_db") or 0) >= 20
    return int(s.get("cutoff_hz") or 0) if steep else 0


@dataclass
class Copy:
    """An upload against the song's copy in the library: better (really: by its quality and its sound's
    range) and why, in a few words."""

    better: bool
    why: str
    quieter: float = 0.0  # dB (RMS) under your copy, from QUIETER on


def compare(paths: Paths, f: File, song: sqlite3.Row) -> Copy | None:
    """Whether the file is a better copy than the song's (None: the song has none of its own; a close match is
    another version). Genuine lossless beats lossy or fake; else its worth (a fake FLAC: its source's) must be
    a quarter more than the copy's and its sound reach as high (the copy measured the same way): a 256 kbps
    source made into a FLAC beats a 123 kbps Opus, a 128 kbps one does not. One QUIETER than the copy is no
    better whatever its quality (recorded at a lower volume)."""
    if needs(song):  # nothing to compare with
        return None
    have = copy_label(song)
    lib = paths.tracks / song["file"]
    if f.rms is not None and lib.is_file():
        _, theirs_rms = audio.levels(lib)
        if theirs_rms is not None and f.rms < theirs_rms - QUIETER:
            down = round(theirs_rms - f.rms, 1)
            return Copy(False, f"{down:g} dB quieter than your {have} (recorded at a lower volume?)", down)
    if f.tier == "lossless":
        ok = song["quality"] != "lossless"
        return Copy(ok, "genuine lossless" + ("" if ok else f", like your {have}"))
    if song["quality"] == "lossless":
        return Copy(False, "your copy is genuine lossless")
    mine = worth(f.tier, f.kbps, f.source)
    theirs = worth(song["quality"], song["kbps"] or 0, song["fake_source"] or "")
    what = f"{f.source} source" if f.tier == "fake" and f.source else f"{f.kbps} kbps"
    if mine < max(theirs * MORE, theirs + 1):
        return Copy(False, f"{what}, your copy {have}")
    band = f.band or 22050
    theirs_band = (_band(audio.spectrum(lib)) or 22050) if lib.is_file() else 22050
    khz = f"sound up to {band / 1000:.1f} kHz (yours {theirs_band / 1000:.1f})"
    if band < theirs_band - SLACK:
        return Copy(False, f"{what}, but {khz}")
    return Copy(True, f"{what} against your {have}, {khz}")


def importable(f: File, song: sqlite3.Row | None, copy: Copy | None, fit: Fit | None = None) -> bool:
    """Imported for a missing song, or as a better copy; not when it sounds like another recording than the
    release's or your copy (unless imported anyway, by hand)."""
    return song is not None and not f.error and (copy is None or copy.better) and not sounds_different(fit)


WAVE_OTHER = 0.3  # waveform under this: another song (with a weak fingerprint, sounds_different)


def _scores(fp: float | None, wave: float | None) -> str:
    """Scores for a reason: "fingerprint 0.56, waveform 0.11" (1: the same audio)."""
    return ", ".join(f"{name} {v:.2f}" for name, v in (("fingerprint", fp), ("waveform", wave)) if v is not None)


def _other(fp: float | None, wave: float | None) -> bool:
    """Another recording: by its fingerprint, or a weak one with a waveform unlike it."""
    return fp is not None and (fp <= identity.OTHER or (fp < identity.SAME and wave is not None and wave < WAVE_OTHER))


def different(fit: Fit | None, copy: bool = False) -> str:
    """Why the file's audio is another recording: than the release's preview, or than your copy (without a
    preview; with `copy` also with one); '' when not, or unknown."""
    if fit is None:
        return ""
    if _other(fit.fingerprint, fit.waveform):
        return f"another recording than the release's ({_scores(fit.fingerprint, fit.waveform)})"
    if (copy or fit.fingerprint is None) and _other(fit.copy_fingerprint, fit.copy_waveform):
        return f"another recording than your copy ({_scores(fit.copy_fingerprint, fit.copy_waveform)})"
    return ""


AS_LISTED = 3  # s: a recording of the list's own track this close to its length is the song


def recorded_different(fit: Fit | None, as_listed: bool, unsure: bool) -> tuple[str, str]:
    """(why a recording is another recording than the song: different, '' if not; a note when it is filed
    though a reference disagrees). It is the song when it sounds like the release's preview (your copy may be
    the odd one), or when it is the list's own track as Spotify lists it (recording.as_listed) and as long:
    then the preview may be another release under the ISRC; else different(), with your copy when Spotify
    plays another release that may be another recording (`unsure`)."""
    if fit is None or (fit.fingerprint is not None and fit.fingerprint >= identity.SAME):
        return "", ""
    why = different(fit, copy=unsure)
    if why and as_listed and fit.diff is not None and abs(fit.diff) <= AS_LISTED:
        return "", f"the list's own track, though {why}"
    return why, ""


def sounds_different(fit: Fit | None) -> bool:
    return bool(different(fit))


def fit(con: sqlite3.Connection, f: File, song: sqlite3.Row | None, tracks: Path) -> Fit | None:
    """How the file fits the song: its length, and its audio against the release's preview (not its ISRC tag:
    a tool that fetched it by the song's ISRC tags it so whatever it found) and against your copy in `tracks`
    (not a close match: another version)."""
    if song is None or f.error or f.path is None:
        return None
    diff = round(f.seconds - song["length"]) if f.seconds and song["length"] else None
    out = Fit(diff)
    isrc = song["isrc"] or ""
    ref = identity.reference(con, isrc, song["length"] or 0) if isrc else None
    cand = identity.fingerprint(f.path) if ref is not None else None
    if ref is not None and cand is not None and len(cand):
        out.fingerprint = round(identity.similarity(ref, cand), 2)
        clip = identity.preview(isrc)
        wave = identity.waveform(f.path, clip) if clip else None
        out.waveform = round(wave, 2) if wave is not None else None
    if not needs(song) and (lib := tracks / song["file"]).is_file():
        fp, wave = identity.against(f.path, lib)
        out.copy_fingerprint = round(fp, 2) if fp is not None else None
        out.copy_waveform = round(wave, 2) if wave is not None else None
    return out


Score = tuple[str, str, str] | None  # (text, ok|warn|bad, what it says); None: not known


def checks(fit: Fit | None, song: sqlite3.Row | None) -> list[tuple[str, str, list[Score], str]]:
    """The audio check for the dialog, a row per reference (the release's preview; your copy, if the song has
    one): (name, what it is, [fingerprint, waveform], why it has neither)."""
    if fit is None or song is None:
        return []
    what = "The release's 30 s preview on Deezer, found by the song's ISRC"
    rows = [("Preview", what, graded(fit.fingerprint, fit.waveform, "the release's preview"), _no_preview(song))]
    if not needs(song):
        what = "Your copy in the library: three 30 s pieces of it, each held against the file as the preview is"
        rows.append(("Your copy", what, graded(fit.copy_fingerprint, fit.copy_waveform, "your copy"), "not readable"))
    return [(name, what, scores, "" if any(scores) else none) for name, what, scores, none in rows]


def graded(fp: float | None, wave: float | None, against: str) -> list[Score]:
    out: list[Score] = [None, None]
    if fp is not None:
        grade = "ok" if fp >= identity.SAME else "bad" if fp <= identity.OTHER else "warn"
        says = {"ok": "the same recording", "warn": "unclear", "bad": "another recording"}[grade]
        out[0] = (f"{round(fp * 100)} %", grade, f"Fingerprint against {against}: {says}")
    if wave is not None:
        grade = "ok" if wave >= identity.WAVE_SAME else "bad" if wave < WAVE_OTHER else "warn"
        says = {"ok": "the same master and mix", "warn": "another master or mix", "bad": "another song"}[grade]
        out[1] = (f"{round(wave * 100)} %", grade, f"Waveform against {against}: {says}")
    return out


def _no_preview(song: sqlite3.Row) -> str:
    return "Deezer has none" if song["isrc"] else "the song has no ISRC"


def release_checks(found: Release) -> list[tuple[str, str, list[Score], str]]:
    """The release check for the dialog, as checks shows it: the release's preview."""
    what = f"The 30 s preview of {found.result.artist} – {found.result.title}"
    scores = graded(found.fit.fingerprint, found.fit.waveform, "the release's preview")
    return [("Preview", what, scores, "" if any(scores) else "no preview")]


def labels(f: File, song: sqlite3.Row | None, fit: Fit | None, copy: Copy | None = None) -> list[tuple[str, str, str]]:
    """Short labels for a file as its song: "Looks right" (a song you have: "Better than your 160 kbps"), or
    what is off; a copy no better than yours is "Not better than your FLAC". (text, style ok|warn|bad, detail)"""
    if f.error:
        return [("Not audio", "bad", f.error)]
    if song is None:
        return [("No song found", "bad", "Neither its tags nor its name name one of your songs")]
    if copy is not None and not copy.better:
        why = f"{copy.why}: your copy stays unless you replace it anyway"
        if copy.quieter:
            return [(f"{copy.quieter:.0f} dB quieter than your copy", "bad", why)]
        return [(f"Not better than your {copy_label(song)}", "bad", why)]
    why = f"{copy.why if copy else ''}; your copy is kept 30 days in inbox/replaced"
    out = [(f"Better than your {copy_label(song)}", "ok", why)] if copy else []
    if fit and fit.diff is not None and abs(fit.diff) > 3:
        n = abs(int(fit.diff))
        amount = f"{n // 60}:{n % 60:02d}" if n >= 60 else f"{n} s"
        detail = f"{_mmss(f.seconds)}, the song {_mmss(song['length'])}"
        out.append((f"{amount} {'longer' if fit.diff > 0 else 'shorter'}", "warn", detail))
    fp, wave = (fit.fingerprint, fit.waveform) if fit else (None, None)
    mine, my_wave = (fit.copy_fingerprint, fit.copy_waveform) if fit else (None, None)
    if why := different(fit):
        out.append(("Sounds different", "bad", why[0].upper() + why[1:]))
    elif fp is not None and _other(mine, my_wave):
        detail = (
            f"The release's recording, but another than your copy ({_scores(mine, my_wave)}): was yours another song?"
        )
        out.append(("Unlike your copy", "warn", detail))
    elif wave is not None and wave < identity.WAVE_SAME:
        out.append(("Other master or mix", "warn", "The same recording, but its waveform is not the release's"))
    elif wave is None and my_wave is not None and my_wave < identity.WAVE_SAME:
        out.append(("Other master or mix", "warn", "The same recording, but its waveform is not your copy's"))
    out += file_notes(f)
    if not out:
        heard = "the release's audio" if wave is not None else "the release's recording" if fp is not None else ""
        why = f"Length fits, {heard or f'audio not compared (no preview: {_no_preview(song)})'}"
        out.append(("Looks right", "ok", why))
    return out


def file_notes(f: File) -> list["Label"]:
    """What the file itself shows, whatever song it is: a FLAC made from a lossy file, a quiet recording."""
    out = []
    if f.tier == "fake":
        out.append(("Fake FLAC", "warn", "A FLAC made from a lossy file (spectrum check)"))
    if f.peak is not None and f.peak <= QUIET_PEAK:
        detail = "Releases peak near 0 dB: recorded at a low volume? (at 25 % it peaks at -12 dB)"
        out.append((f"Quiet: peaks at {f.peak:g} dB", "warn", detail))
    return out


Label = tuple[str, str, str]  # (text, style ok|warn|bad, what it says)


def found_labels(f: File, found: Release | None, have: str, failed: str) -> list[Label]:
    """Short labels for a file that is none of your songs, by its release check (release): a release can be
    imported into your Echolot · Wished."""
    if f.error:
        return [("Not audio", "bad", f.error)]
    out = [("Not checked", "warn", failed)] if failed else []
    who = f"{found.result.artist} – {found.result.title}" if found else ""
    if found is None:
        out.append(("No song found", "bad", "None of your songs, and no release named so (Deezer)"))
    elif found.verdict == "released":
        out.append(
            ("Released", "ok", f"The recording of {who} at its length: imported as that song into your {WISHED_TITLE}")
        )
    elif found.verdict == "resembles":
        out.append(
            ("Resembles a release", "warn", f"Like {who}, but in another edit or length (or unclear): is it that?")
        )
    else:
        out.append(("Named like a release", "warn", f"{who} has these names; no preview to compare the audio with"))
    if have:
        out.append(("You have it", "warn", f"The same audio as your {have}"))
    return out + file_notes(f)


def _mmss(seconds: float) -> str:
    n = round(seconds or 0)
    return f"{n // 60}:{n % 60:02d}"


@dataclass
class _Run:
    """What filing a song needs of a job run (acquire.finish: tags, cover, artist picture)."""

    paths: Paths
    vault: "Vault"
    data: Path | None = None  # the data folder: the covers job's list of the files done (covers.note_done)
    stop: threading.Event = dataclasses.field(default_factory=threading.Event)


def import_files(
    con: sqlite3.Connection,
    paths: Paths,
    vault: "Vault",
    batch: str,
    chosen: dict[int, str],
    remove: set[int] = frozenset(),
    source: str = "manual",
    origins: dict[int, str] | None = None,
    data: Path | None = None,
) -> list[tuple[str, str | None]]:
    """File the chosen files (n -> song key) as their songs, as by Perfect match: a missing song gets the file
    (one covered by a close match too: the version stays, unless n is in `remove`), a library song the file in
    place of its copy (filing.file_into replace); then delete the batch. `source`: as History shows it
    (manual, mitschnitt); `origins`: where each came from (origin.KINDS). One line per file, with the song key
    when the song has the file now."""
    from echolot.jobs.acquire import finish

    by_n = {f.n: f for f in files(paths, batch)}
    out = []
    for n, key in sorted(chosen.items()):
        f, song = by_n.get(n), con.execute("SELECT * FROM songs WHERE key = ?", (key,)).fetchone()
        if f is None or f.path is None or not f.path.is_file() or song is None:
            out.append((f"{f.name if f else n}: not imported (gone)", None))
            continue
        close = song["file"] if song["close_match"] else None  # the version it was covered by: stays, or `remove`
        want, fake, have = Want.of(song), f.tier == "fake", None if close else song["file"]
        if not have:
            _link(con, key, want, f.seconds if close else 0)  # this file whatever its length
        kind = (origins or {}).get(n, "")
        info = {
            "match": "by hand",
            "fake": fake,
            "fake_source": f.source,
            "replaces": have or "",
            "replace": bool(have),
            "url": origin.link(con, key, kind)[1],
        }
        action, dest = filing.file_into(con, paths, f.path, want, source, **info)  # a better copy takes over
        if dest and action in ("new", "upgrade"):
            finish(_Run(paths, vault, data), con, dest, want)
            if kind:
                origin.note(con, key, dest.relative_to(paths.tracks).as_posix(), kind)
        if not have and action in ("new", "upgrade", "duplicate"):
            _link(con, key, want, f.seconds if close else 0)
        elif have and dest and action == "upgrade":  # linked to its new copy, whatever that copy's length
            e = next((x for x in catalog.Catalog.from_db(con).entries if paths.tracks / x.path == dest), None)
            if e is not None:
                link = json.dumps([e.path.partition("/")[0], e.title, round(e.duration)])
                with con:
                    con.execute("UPDATE songs SET link = ? WHERE key = ?", (link, key))
        if close and n in remove and dest and action in ("new", "upgrade"):
            out.append(_remove_close(con, paths, key, close))
        where = dest.relative_to(paths.tracks).as_posix() if dest else ""
        label = {"new": "filed as", "upgrade": "replaced the copy:", "duplicate": "the library has a better copy:"}
        line = f"{want.artist} – {want.title}: {label.get(action, action)} {where}".strip()
        out.append((line, key if action in ("new", "upgrade", "duplicate") else None))
    catalog.match_songs(con)
    cancel(paths, batch)
    return out


def _remove_close(con: sqlite3.Connection, paths: Paths, key: str, path: str) -> tuple[str, None]:
    """The close match a song was covered by, retired with its upload; not while another song has that file."""
    if con.execute("SELECT 1 FROM songs WHERE file = ? AND key != ?", (path, key)).fetchone():
        return (f"{path}: kept, another song has it", None)
    entry = next((e for e in catalog.Catalog.from_db(con).entries if e.path == path), None)
    if entry is None or not (paths.tracks / path).is_file():
        return (f"{path}: already gone", None)
    filing.retire(con, paths, entry, "close match removed by hand")
    return (f"{path}: removed (kept 30 days in inbox/replaced)", None)


def _link(con: sqlite3.Connection, key: str, want: Want, seconds: float = 0) -> None:
    """The song is its library file <artist> - <title> whatever its length (as review links it); with
    `seconds`, the one of that length (a close match's version can reduce to the same title)."""
    link = [want.artist, want.title, round(seconds)] if seconds else [want.artist, want.title]
    with con:
        con.execute("DELETE FROM attempts WHERE song_key = ?", (key,))
        con.execute("UPDATE songs SET link = ?, close_match = 0 WHERE key = ?", (json.dumps(link), key))


def cancel(paths: Paths, batch: str) -> None:
    shutil.rmtree(folder(paths, batch), ignore_errors=True)


def purge(paths: Paths, older: float = KEEP_SECONDS) -> None:
    """Delete the batches left alone for a day."""
    base = paths.inbox("upload")
    for d in base.iterdir() if base.is_dir() else []:
        if d.is_dir() and BATCH.fullmatch(d.name) and time.time() - d.stat().st_mtime > older:
            shutil.rmtree(d, ignore_errors=True)
