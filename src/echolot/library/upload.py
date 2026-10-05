"""Files uploaded by hand: for missing songs, or better copies of songs in the library (the upload dialog on
Missing, the Overview and the list pages). Uploaded files wait in inbox/upload/<batch>/, each checked once
(audio.prepare: repaired, converted, spectrum-checked; its length, quality and tags kept next to it as
<file>.json). Echolot finds each file's song among the user's songs, a missing one first; what it sees (length
against the song's, quality, the audio against the release's) is shown for information. Imported, a file is
that song whatever its names or length (as Perfect match in review): a missing song gets it, a song in the
library takes it in place of its copy only when it is better (`better`); the download source is "by hand".
The rest of the batch is deleted, as is a batch left alone for a day."""

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

from echolot.library import audio, catalog, filing, identity, rules
from echolot.library.filing import Paths, Want

if TYPE_CHECKING:
    from echolot.settings.vault import Vault

KEEP_SECONDS = 24 * 3600  # a batch nobody imported or cancelled is deleted after this
BATCH = re.compile(r"[0-9a-f]{16}")


@dataclass
class File:
    """An uploaded file as checked: n (its place in the batch), name (as uploaded), path (after prepare),
    seconds, kbps, tier (catalog.QUALITY), tags (artist, title), error (not usable audio) and what the
    spectrum check saw (source, band)."""

    n: int
    name: str
    path: Path | None = None
    seconds: float = 0
    kbps: int = 0
    tier: str = ""
    artist: str = ""
    title: str = ""
    error: str = ""
    source: str = ""  # a fake FLAC: the lossy file it was made from, as the spectrum check estimates it
    band: int = 0  # Hz up to which it has sound (a lossy encoder's edge; 0: none, its full range)


@dataclass
class Fit:
    """What a file looks like as a song: its length off the song's (s), and its audio against the release's
    preview: the fingerprint's share of equal bits (the recording: identity.SAME on; about 0.55 another
    song) and the waveform's correlation (the same master and mix: identity.WAVE_SAME on); None: not known."""

    diff: float | None
    fingerprint: float | None = None
    waveform: float | None = None


def folder(paths: Paths, batch: str) -> Path:
    if not BATCH.fullmatch(batch):
        raise ValueError("no such upload")
    return paths.inbox("upload") / batch


def stage(paths: Paths, uploads: list[tuple[str, Any]]) -> str:
    """Save the uploaded files (name, file object) as a new batch and check each one; returns the batch."""
    purge(paths)
    batch = secrets.token_hex(8)
    d = folder(paths, batch)
    d.mkdir(parents=True)
    for n, (name, fileobj) in enumerate(uploads, 1):
        base = Path(name or "file").name
        stem, ext = re.sub(r"[^\w\-. ()&',!]+", "_", Path(base).stem)[:120] or "file", Path(base).suffix.lower()
        dest = d / f"{n:02d} {stem}{ext}"
        with dest.open("wb") as out:
            shutil.copyfileobj(fileobj, out)
        f = File(n, base)
        try:
            prepared = audio.prepare(dest)
            f.path = prepared.path
            f.seconds, f.kbps = audio.probe(prepared.path)
            f.tier = tier(prepared.path, f.kbps, prepared.fake)
            f.source, f.band = (prepared.spectrum or {}).get("source", ""), _band(prepared.spectrum)
            artists, f.title = audio.read_tags(prepared.path)
            f.artist = ", ".join(dict.fromkeys(artists))
        except audio.Rejected as e:
            f.error = str(e)
        _save(d, f)
    return batch


def tier(path: Path, kbps: int, fake: bool) -> str:
    """The quality tier of a file (catalog.QUALITY), as the library counts it."""
    if path.suffix.lower().lstrip(".") in audio.LOSSLESS:
        return "fake" if fake else "lossless"
    return "lossy-high" if kbps >= 250 else "lossy-mid" if kbps >= 150 else "lossy-low"


def files(paths: Paths, batch: str) -> list[File]:
    d = folder(paths, batch)
    out = []
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        raw = json.loads(p.read_text())
        raw["path"] = Path(raw["path"]) if raw.get("path") else None
        out.append(File(**raw))
    return out


def _save(d: Path, f: File) -> None:
    raw = dataclasses.asdict(f) | {"path": str(f.path) if f.path else None}
    (d / f"{f.n:02d}.json").write_text(json.dumps(raw))


def guess(f: File, songs: list[sqlite3.Row]) -> str | None:
    """The song a file most likely is: by its tags and file name (rules.identify), then by its title and
    artist anywhere in them; a missing song before one in the library, then the closest in length. None
    when nothing names one. Only songs whose core title is in the file's names are compared."""
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
        if not score and rules.artist_key(s["artist"]) in rules.artist_key(f"{f.artist} {stem}"):
            score = 1  # the title and the artist somewhere (never another artist's song of that title)
        if score:
            cand = (score, not s["file"], -abs((f.seconds or 0) - (s["length"] or 0)), s["key"])
            best = max(best, cand) if best else cand
    return best[3] if best else None


def copy_label(song: sqlite3.Row) -> str:
    """What the library has of a song: "FLAC", "fake FLAC", "160 kbps"."""
    return {"lossless": "FLAC", "fake": "fake FLAC"}.get(song["quality"] or "", f"{song['kbps'] or '?'} kbps")


# what a lossy file's quality is worth in kbps, for a fake FLAC by the source the spectrum check estimates
SOURCE_KBPS = {"~128 kbps": 128, "~160-192 kbps": 176, "~256 kbps / V0": 256}
MORE = 1.25  # a better lossy copy has a quarter more
SLACK = 300  # Hz: a band this much lower is the same


def worth(tier: str, kbps: int, source: str = "") -> float:
    """A copy's quality as kbps: genuine lossless beyond any, a fake FLAC its source's (unknown: 128)."""
    if tier == "lossless":
        return float("inf")
    if tier == "fake":
        return SOURCE_KBPS.get(source, 128)
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


def compare(paths: Paths, f: File, song: sqlite3.Row) -> Copy | None:
    """Whether the file is a better copy than the song's in the library (None: the song has none). Genuine
    lossless beats a lossy or fake copy; otherwise its worth (a fake FLAC: its source's) must be a quarter
    more than the copy's, and its sound reach as high (the library copy is measured by the same spectrum
    check): a 256 kbps source made into a FLAC beats a 123 kbps Opus, a 128 kbps one does not."""
    if not song["file"]:
        return None
    have = copy_label(song)
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
    lib = paths.tracks / song["file"]
    band = f.band or 22050
    theirs_band = (_band(audio.spectrum(lib)) or 22050) if lib.is_file() else 22050
    khz = f"sound up to {band / 1000:.1f} kHz (yours {theirs_band / 1000:.1f})"
    if band < theirs_band - SLACK:
        return Copy(False, f"{what}, but {khz}")
    return Copy(True, f"{what} against your {have}, {khz}")


def importable(f: File, song: sqlite3.Row | None, copy: Copy | None) -> bool:
    """A file is imported for a missing song, or as a better copy of one in the library."""
    return song is not None and not f.error and (copy is None or copy.better)


def fit(con: sqlite3.Connection, f: File, song: sqlite3.Row | None) -> Fit | None:
    """How the file fits the song: its length, and its audio against the release's preview (the audio only:
    a tool that fetched the file by the song's ISRC tags it so whatever it found)."""
    if song is None or f.error or f.path is None:
        return None
    diff = round(f.seconds - song["length"]) if f.seconds and song["length"] else None
    out = Fit(diff)
    isrc = song["isrc"] or ""
    ref = identity.reference(con, isrc) if isrc else None
    cand = identity.fingerprint(f.path) if ref is not None else None
    if ref is not None and cand is not None and len(cand):
        out.fingerprint = round(identity.similarity(ref, cand), 2)
        clip = identity.preview(isrc)
        wave = identity.waveform(f.path, clip) if clip else None
        out.waveform = round(wave, 2) if wave is not None else None
    return out


def labels(f: File, song: sqlite3.Row | None, fit: Fit | None, copy: Copy | None = None) -> list[tuple[str, str, str]]:
    """Short labels for a file as its song: "Looks right" (a song you have: "Better than your 160 kbps"), or
    what is off; a copy no better than yours is "Not better than your FLAC". (text, style ok|warn|bad, detail)"""
    if f.error:
        return [("Not audio", "bad", f.error)]
    if song is None:
        return [("No song found", "bad", "Neither its tags nor its name name one of your songs")]
    if copy is not None and not copy.better:
        why = f"{copy.why}: your copy stays unless you replace it anyway"
        return [(f"Not better than your {copy_label(song)}", "bad", why)]
    why = f"{copy.why if copy else ''}; your copy is kept 30 days in inbox/replaced"
    out = [(f"Better than your {copy_label(song)}", "ok", why)] if copy else []
    if fit and fit.diff is not None and abs(fit.diff) > 3:
        n = abs(int(fit.diff))
        amount = f"{n // 60}:{n % 60:02d}" if n >= 60 else f"{n} s"
        detail = f"{_mmss(f.seconds)}, the song {_mmss(song['length'])}"
        out.append((f"{amount} {'longer' if fit.diff > 0 else 'shorter'}", "warn", detail))
    fp, wave = (fit.fingerprint, fit.waveform) if fit else (None, None)
    if fp is not None and (fp <= identity.OTHER or (fp < identity.SAME and wave is not None and wave < 0.3)):
        out.append(("Sounds different", "bad", "Another recording than the release's (its fingerprint)"))
    elif wave is not None and wave < identity.WAVE_SAME:
        out.append(("Other master or mix", "warn", "The same recording, but its waveform is not the release's"))
    if f.tier == "fake":
        out.append(("Fake FLAC", "warn", "A FLAC made from a lossy file (spectrum check)"))
    if not out:
        why = "Length fits" + (", the release's audio" if wave is not None else ", audio not compared (no preview)")
        out.append(("Looks right", "ok", why))
    return out


def _mmss(seconds: float) -> str:
    n = round(seconds or 0)
    return f"{n // 60}:{n % 60:02d}"


@dataclass
class _Run:
    """What filing a song needs of a job run (acquire.finish: tags, cover, artist picture)."""

    paths: Paths
    vault: "Vault"
    stop: threading.Event = dataclasses.field(default_factory=threading.Event)


def import_files(
    con: sqlite3.Connection, paths: Paths, vault: "Vault", batch: str, chosen: dict[int, str]
) -> list[tuple[str, str | None]]:
    """File the chosen files (n -> song key) as their songs, as by Perfect match: a missing song gets the file, a
    song in the library the file in place of its copy (better, or replaced anyway: filing.file_into replace);
    delete the batch.
    Returns one line per file, with the song key when the song has the file now."""
    from echolot.jobs.acquire import finish

    by_n = {f.n: f for f in files(paths, batch)}
    out = []
    for n, key in sorted(chosen.items()):
        f, song = by_n.get(n), con.execute("SELECT * FROM songs WHERE key = ?", (key,)).fetchone()
        if f is None or f.path is None or not f.path.is_file() or song is None:
            out.append((f"{f.name if f else n}: not imported (gone)", None))
            continue
        want, fake, have = Want.of(song), f.tier == "fake", song["file"]
        if not have:
            _link(con, key, want)  # this file whatever its length
        info = {
            "match": "by hand",
            "fake": fake,
            "fake_source": f.source,
            "replaces": have or "",
            "replace": bool(have),
        }
        action, dest = filing.file_into(con, paths, f.path, want, "manual", **info)  # a better copy takes over
        if dest and action in ("new", "upgrade"):
            finish(_Run(paths, vault), con, dest, want)
        if not have and action in ("new", "upgrade", "duplicate"):
            _link(con, key, want)
        elif have and dest and action == "upgrade":  # linked to its new copy, whatever that copy's length
            e = next((x for x in catalog.Catalog.from_db(con).entries if paths.tracks / x.path == dest), None)
            if e is not None:
                link = json.dumps([e.path.partition("/")[0], e.title, round(e.duration)])
                with con:
                    con.execute("UPDATE songs SET link = ? WHERE key = ?", (link, key))
        where = dest.relative_to(paths.tracks).as_posix() if dest else ""
        label = {"new": "filed as", "upgrade": "replaced the copy:", "duplicate": "the library has a better copy:"}
        line = f"{want.artist} – {want.title}: {label.get(action, action)} {where}".strip()
        out.append((line, key if action in ("new", "upgrade", "duplicate") else None))
    catalog.match_songs(con)
    cancel(paths, batch)
    return out


def _link(con: sqlite3.Connection, key: str, want: Want) -> None:
    """The song is its library file <artist> - <title>, whatever the file's length (as review links it)."""
    with con:
        con.execute("DELETE FROM attempts WHERE song_key = ?", (key,))
        con.execute(
            "UPDATE songs SET link = ?, close_match = 0 WHERE key = ?", (json.dumps([want.artist, want.title]), key)
        )


def cancel(paths: Paths, batch: str) -> None:
    shutil.rmtree(folder(paths, batch), ignore_errors=True)


def purge(paths: Paths, older: float = KEEP_SECONDS) -> None:
    """Delete the batches left alone for a day."""
    base = paths.inbox("upload")
    for d in base.iterdir() if base.is_dir() else []:
        if d.is_dir() and BATCH.fullmatch(d.name) and time.time() - d.stat().st_mtime > older:
            shutil.rmtree(d, ignore_errors=True)
