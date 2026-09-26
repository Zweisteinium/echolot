#!/usr/bin/env python3
"""library.py: the single authority on "is this the same song?" and the only code that puts files into /music/tracks.

Same song = same artist (case, accents and punctuation ignored; the first of several artists also counts)
          + same title after removing noise that does not change the recording:
            "(Free DL)", "[HAK003]", "(Original Mix)", "- Original Mix", "(feat. X)", "- 2011 Remaster", punctuation
          + length within max(10 s, 4 %) when both lengths are known.
Version words (Remix, Edit, Extended, Radio Edit, VIP, II, Pt. 2, Mashup, ...) stay part of the title, so
"Glow" and "Glow - Nick Schwenderling Remix", or "Fire" and "Fire II", are different songs.

Filing rules (file_into), all under one lock shared by every container:
  - nothing in the library is ever overwritten; files are linked into place with an exclusive create
  - a download that is the same song as a library file is discarded, unless it is a genuine lossless copy
    of a lossy/fake one: then it takes over and the old file moves to /music/inbox/replaced/<date>/ (kept 30 days)
  - a different song whose file name is taken (e.g. same title, other length) gets its length appended
  - artist folders are matched case/accent-insensitively, so one artist keeps one folder
  - every filing is appended to /config/logs/downloads.jsonl (for statistics)

CLI:
  library.py file <src> --artist A --title T [--length S] [--source X] [--id ID] [--fake]  -> prints "<action>\t<path>"
  library.py find --artist A --title T [--length S]
  library.py check            read-only: artist folder spellings and duplicate songs
  library.py merge [--apply]  merge folder spellings and duplicates (dry run unless --apply; losers go to replaced/)
  library.py purge            delete replaced/ entries older than 30 days"""
import argparse, contextlib, datetime, fcntl, json, os, pathlib, re, shutil, sys, time, unicodedata

TRACKS = pathlib.Path("/music/tracks")
STATE = pathlib.Path("/config/state")
REPLACED = pathlib.Path("/music/inbox/replaced")
EVENTS = pathlib.Path("/config/logs/downloads.jsonl")
LOCKFILE = STATE / "library.lock"
CACHE = STATE / "library-cache.json"
LOSSY_LIST = STATE / "lossy-sourced.json"      # written by spectrum.py: library stem -> detection result
AUDIO = ["flac", "wav", "aiff", "m4a", "mp3", "opus", "ogg", "webm", "aac"]
LOSSLESS = {"flac", "wav", "aiff"}
KEEP_REPLACED_DAYS = 30

# ------------------------------------------------------------------ normalisation
_LETTERS = str.maketrans({"ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe", "ß": "ss", "ł": "l", "Ł": "l",
                          "đ": "d", "Đ": "d", "þ": "th", "Þ": "th", "ı": "i", "$": "s"})   # Ke$ha, A$AP

def fold(s):
    """Lower case without accents (ø -> o etc.); non-Latin scripts stay (an ASCII-only fold would erase "СВЕТЛАНА")."""
    s = unicodedata.normalize("NFKD", (s or "").translate(_LETTERS))
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()

def clean_name(s):
    """File/folder-safe name (same rule the rest of the stack uses)."""
    s = unicodedata.normalize("NFC", s or "").replace("/", " ").replace("\\", " ")
    s = re.sub(r'[:*?"<>|]', " ", s)
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    return s or "Unknown"

def first_artist(a):
    return re.split(r"\s*[,;/]\s*|\s+(?:&|x|feat\.?|ft\.?|featuring|vs\.?)\s+", (a or "").strip(), flags=re.I)[0].strip()

def artist_key(a):
    return re.sub(r"[\W_]+", "", fold(a).replace("&", "and"))

def artist_keys(a):
    """Full name and first artist ("Above & Beyond" -> {aboveandbeyond, above}); empty keys dropped."""
    return {k for k in (artist_key(a), artist_key(first_artist(a))) if k}

_NOISE = [
    r"[\(\[\{]\s*(?:free\s*(?:dl|d/l|download)|freel\s*dl|free|out\s*now|premiere|official(?:\s+(?:audio|video|music\s+video|visuali[sz]er))?"
    r"|visuali[sz]er|lyrics?|hq|hd|explicit|clean|original(?:\s+(?:mix|version))?)\s*[\)\]\}]",
    r"\[\s*(?!remix|edit|mix|vip|version|rework|bootleg|flip|live)[a-z]{2,6}\s?-?\d{2,5}\s*\]",   # catalog no. [HAK003]
    r"[\(\[]\s*(?:feat|ft|featuring|with)\.?\s[^\)\]]*[\)\]]",
    r"\s+(?:feat|ft|featuring)\.?\s[^\-\(\[]*$",
    r"\s+-\s+original(?:\s+(?:mix|version))?\s*$",
    r"\s+-\s+free\s*(?:dl|download)\s*$",
    r"\s+\*?free\s+(?:dl|download)\*?\s*$",
    r"[\(\[]\s*(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?\s*[\)\]]",
    r"\s+-\s+(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?\s*$",
    r"\[\s*(?!remix|edit|mix|vip|version|rework|bootleg|flip|live)[/\\\-_#]*[a-z]{0,6}[/\\\-_#\s]*\d{3,}\s*\]",            # ids / catalog numbers like [///A001], [2157720717]
    r"_\d{6,}\b",                                                      # upload ids glued to titles: Title_1554143200
    r"[\(\[]\s*(?:clean|dirty|explicit)\s*[\)\]]",  # DJ-pool edition tags
    r"\s+(?:clean|dirty)(?=\s+\d{1,2}[ab]\s+\d{2,3}\s*$|\s*$)",
    r"\s+\d{1,2}[ab]\s+\d{2,3}\s*$",                                   # DJ-pool Camelot key + BPM: "1A 132"
    r"[\(\[][^a-z0-9\(\)\[\]]+[\)\]]",                                 # parentheses without Latin letters (translations)
]

def title_key(t):
    s = fold(t).replace("&", " and ")
    prev = None
    while prev != s:
        prev = s
        for pat in _NOISE:
            s = re.sub(pat, " ", s, flags=re.I)
    s = re.sub(r"[\W_]+", " ", s).strip()
    return re.sub(r"\bpart\b", "pt", s)                                  # "Pt. III" == "Part III"

def _words(s):
    return " " + re.sub(r"[\W_]+", " ", fold(s).replace("&", " and ")).strip() + " "

def _strip_track_no(s):
    return re.sub(r"^\s*(?:[a-z]?\d{1,4}|\d{1,2}-\d{1,3})[\s.\-_)]+(?=\S)", "", s, flags=re.I)

def identity_ok(artist, title, tag_artists=(), tag_title="", file_name="", folders=(), loose=False, length_close=False):
    """Is this file really <artist> - <title>? (Download matching goes by search results and length and can
    pick another artist's song with the same title, e.g. "Vanilla - All Night" for "TINOS - All Night".)
    Artist: the requested artist (full or first) appears as whole words in the file's artist tags or in its
    Soulseek file or folder names. Title: the title tag, or the file name (never a folder: that is the album)
    without track number and artist prefix, gives exactly the requested title (noise rules of title_key).
    loose (Spotify songs): the title may also just contain the requested title's words in order ("Edit" vs
    "Radio Edit") when the length is within 3 s. The artist must always match. Returns (ok, reason)."""
    base = re.sub(r"\s*\([^)]*\)\s*$", "", artist or "")               # Spotify disambiguation: "Vegas (Brazil)"
    want_a = [w for w in {_words(artist).strip(), _words(first_artist(artist)).strip(), _words(base).strip()} if w]
    texts = [t for t in [*tag_artists, file_name, *folders] if t]
    if not want_a or not any(f" {w} " in _words(t) for w in want_a for t in texts):
        return False, f"artist '{artist}' not in {texts}"
    tk = title_key(title)
    has_artist = lambda x: any(f" {w} " in _words(x) for w in want_a)
    def candidates(name):
        """Readings of a tag title / file name as a plain title: without track number, without (repeated)
        artist prefixes, 'Album - 07 - Title' and reversed 'Title - Artist' forms."""
        m = re.match(r"^[^_]+_[^_]+_\d{1,3}_(.+)$", name)      # "Artist_Album_13_Title"
        name = name.replace("_-_", " - ").replace("_", " ") if name.count("_") > 2 else name
        rest = _strip_track_no(name)
        out = [name, rest]              # the unstripped form too: "93 Bang Bang", "H2 (...)" start with a number
        if m: out.append(m.group(1).replace("_", " "))
        while " - " in rest:
            head, tail = rest.split(" - ", 1)
            if not has_artist(head): break
            rest = _strip_track_no(tail); out.append(rest)
        m = re.match(r"^.+? - \d{1,3} - (.+)$", rest)
        if m: out.append(m.group(1))
        if " - " in rest:
            head, tail = rest.rsplit(" - ", 1)
            if has_artist(tail): out.append(head)
        return out
    if tag_title and any(title_key(c) == tk for c in candidates(tag_title)): return True, "tags"
    if file_name and any(title_key(c) == tk for c in candidates(file_name)): return True, "file name"
    if loose and length_close and tk:
        def in_order(want, have):          # every requested word, in order ("... Edit" in "... Radio Edit")
            it = iter(have.split()); return all(w in it for w in want.split())
        for c in candidates(tag_title) if tag_title else []:   # tag only: file names mix in album names
            if in_order(tk, title_key(c)): return True, "title tag has the title words in order, length within 3 s"
    return False, f"title '{title}' is neither tag '{tag_title}' nor file name '{file_name}'"

def _tags(p):
    try:
        from mutagen import File as MFile
        m = MFile(str(p), easy=True)
        t = m.tags if m is not None and m.tags is not None else {}
        get = lambda k: [v for v in (t.get(k) or []) if v]
        return get("artist") + get("albumartist"), (get("title") or [""])[0]
    except Exception:
        return [], ""

def same_length(a, b):
    a, b = float(a or 0), float(b or 0)
    return not a or not b or abs(a - b) <= max(10.0, 0.04 * max(a, b))

# ------------------------------------------------------------------ catalog
def _probe(p):
    """(duration s, bitrate kbps) from the file header."""
    try:
        from mutagen import File as MFile
        m = MFile(str(p))
        if m is None or m.info is None: return 0.0, 0
        return float(getattr(m.info, "length", 0) or 0), int((getattr(m.info, "bitrate", 0) or 0) / 1000)
    except Exception:
        return 0.0, 0

LENGTH_SUFFIX = re.compile(r"\s\(\d+m\d{2}s\)(?:\s\(\d+\))?$")   # added by _free_name to tell versions apart

def title_part(stem, dirname):
    """Title from '<Artist> - <Title>[ (3m43s)]'; the length suffix _free_name adds is not part of the title."""
    stem = LENGTH_SUFFIX.sub("", stem)
    if stem.lower().startswith(dirname.lower() + " - "): return stem[len(dirname) + 3:]
    return stem.split(" - ", 1)[1] if " - " in stem else stem

class Entry:
    def __init__(self, path, dur, kbps, fake):
        self.path, self.dur, self.kbps, self.fake = path, dur, kbps, fake
        self.ext = path.suffix.lower().lstrip(".")
        self.dir = path.parent.name
        self.title = title_part(path.stem, self.dir)
        self.akeys = artist_keys(self.dir)
        self.tkey = title_key(self.title)
        self.stem = f"{self.dir}/{path.stem}"
    @property
    def genuine(self):
        return self.ext in LOSSLESS and not self.fake
    def rank(self):
        return (1 if self.genuine else 0, 0 if self.fake else self.kbps, AUDIO.index(self.ext) * -1)

class Catalog:
    def __init__(self):
        cache = _read_json(CACHE, {})
        fakes = set(_read_json(LOSSY_LIST, {}))
        self.entries, self.dirs, fresh = [], {}, {}
        for d in sorted(TRACKS.iterdir()) if TRACKS.exists() else []:
            if not d.is_dir() or d.name.startswith("."): continue
            for k in artist_keys(d.name): self.dirs.setdefault(k, []).append(d)
            for p in sorted(d.iterdir()):
                ext = p.suffix.lower().lstrip(".")
                if ext not in AUDIO or not p.is_file(): continue
                st = p.stat(); c = cache.get(str(p))
                if c and c[0] == st.st_size and c[1] == int(st.st_mtime): dur, kbps = c[2], c[3]
                else: dur, kbps = _probe(p)
                fresh[str(p)] = [st.st_size, int(st.st_mtime), dur, kbps]
                self.entries.append(Entry(p, dur, kbps, f"{d.name}/{p.stem}" in fakes))
        _write_json(CACHE, fresh)
        self.by_key = {}
        for e in self.entries:
            for k in e.akeys: self.by_key.setdefault((k, e.tkey), []).append(e)

    def find(self, artist, title, length=0):
        """Library files that are the same song, best quality first."""
        tk = title_key(title)
        if not tk: return []
        hits = {}
        for k in artist_keys(artist):
            for e in self.by_key.get((k, tk), []):
                if same_length(e.dur, length): hits[str(e.path)] = e
        return sorted(hits.values(), key=lambda e: e.rank(), reverse=True)

    def artist_dir(self, artist):
        """Existing folder of this artist (any spelling), else a new one named after the given spelling."""
        for k in [artist_key(artist), artist_key(first_artist(artist))]:
            if k and k in self.dirs:
                return max(self.dirs[k], key=lambda d: sum(1 for _ in d.iterdir()))
        return TRACKS / clean_name(artist)

# ------------------------------------------------------------------ helpers
def _read_json(p, default):
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return default

def _write_json(p, data):
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")   # several processes (hook, cron, containers) may write
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8"); tmp.replace(p)

@contextlib.contextmanager
def locked():
    STATE.mkdir(parents=True, exist_ok=True)
    with open(LOCKFILE, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield

def _place(src, dest):
    """Move src to dest without ever replacing an existing file (exclusive hard link, then unlink)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dest)          # raises FileExistsError instead of overwriting
    except OSError as e:
        if isinstance(e, FileExistsError): raise
        with open(src, "rb") as fi, open(dest, "xb") as fo: shutil.copyfileobj(fi, fo)   # other filesystem
        shutil.copystat(src, dest)
    os.unlink(src)

def _retire(entry, reason):
    """Take a file out of the library into replaced/<date>/ (kept KEEP_REPLACED_DAYS days)."""
    dest = REPLACED / datetime.date.today().isoformat() / entry.path.relative_to(TRACKS)
    n = 1
    while dest.exists():
        dest = dest.with_name(f"{entry.path.stem} ({n}){entry.path.suffix}"); n += 1
    _place(entry.path, dest)
    lst = _read_json(LOSSY_LIST, None)
    if lst and entry.stem in lst:
        lst.pop(entry.stem); _write_json(LOSSY_LIST, lst)
    _event("retired", dest, reason=reason, was=str(entry.path.relative_to(TRACKS)))
    return dest

def _free_name(folder, name, ext, length):
    """First unused '<name>.<ext>' in folder; a taken name (by any extension) gets the length appended."""
    def taken(n): return any((folder / f"{n}.{e}").exists() for e in AUDIO)
    if not taken(name): return folder / f"{name}.{ext}"
    base = f"{name} ({int(length) // 60}m{int(length) % 60:02d}s)" if length else f"{name} (2)"
    cand, i = base, 2
    while taken(cand): cand, i = f"{base} ({i})", i + 1
    return folder / f"{cand}.{ext}"

def _event(action, path, **kw):
    try:
        p = pathlib.Path(path); st = p.stat() if p.exists() else None
        dur, kbps = _probe(p) if st else (0.0, 0)
        rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "action": action,
               "path": str(p.relative_to(TRACKS)) if str(p).startswith(str(TRACKS)) else str(p),
               "ext": p.suffix.lstrip(".").lower(), "bytes": st.st_size if st else 0, "kbps": kbps, "seconds": round(dur), **kw}
        EVENTS.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENTS, "a", encoding="utf-8") as f: f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

# ------------------------------------------------------------------ filing
def file_into(src, artist, title, length=0, source="", ids=None, fake=False, strict=False, file_name="", folders=(), loose=False):
    """Put a downloaded file into the library. Returns (action, library path); action is
    'new', 'upgrade' (replaced a lossy/fake copy), 'duplicate' (discarded, the library already has it),
    'mismatch' (strict: length is not the requested song's, i.e. another version; discarded) or
    'wrong-song' (strict: tags / source file name are not the requested artist and title; discarded).
    strict is for search results (Soulseek, YouTube); file_name/folders = where the download came from."""
    src = pathlib.Path(src)
    ext = src.suffix.lower().lstrip(".")
    dur, _ = _probe(src)
    if strict:
        tag_artists, tag_title = _tags(src)
        close = bool(dur and length and abs(dur - float(length)) <= 3)
        ok, why = identity_ok(artist, title, tag_artists, tag_title, file_name, folders, loose, close)
        if not ok:
            src.unlink(missing_ok=True)
            _event("wrong-song", src, source=source, ids=ids or [], artist=artist, title=title, reason=why)
            return "wrong-song", None
        if dur and length and not same_length(dur, length):
            src.unlink(missing_ok=True)
            _event("mismatch", src, source=source, ids=ids or [], artist=artist, title=title, seconds=round(dur), wanted_seconds=round(float(length)))
            return "mismatch", None
    length = dur or float(length or 0)
    new_genuine = ext in LOSSLESS and not fake
    with locked():
        cat = Catalog()
        same = cat.find(artist, title, length)
        if same:
            best = same[0]
            if not (new_genuine and not best.genuine):
                src.unlink(missing_ok=True)
                _event("duplicate", best.path, source=source, ids=ids or [], artist=artist, title=title)
                return "duplicate", best.path
            # genuine lossless takes over: keep the existing name, retire every non-genuine copy of the song
            losers = [e for e in same if not e.genuine]
            dest = best.path.with_suffix("." + ext)
            if dest.exists() and dest not in [e.path for e in losers]:
                dest = _free_name(best.path.parent, best.path.stem, ext, length)
            retired = []
            for e in losers:
                if e.path == dest: retired.append(str(_retire(e, "replaced by genuine lossless")))
            _place(src, dest)
            for e in losers:
                if e.path != dest and e.path.exists(): retired.append(str(_retire(e, "replaced by genuine lossless")))
            _event("upgrade", dest, source=source, ids=ids or [], replaced=retired, artist=artist, title=title)
            return "upgrade", dest
        folder = cat.artist_dir(artist)
        dest = _free_name(folder, f"{folder.name} - {clean_name(title)}", ext, length)
        _place(src, dest)
        _event("new", dest, source=source, ids=ids or [], fake=bool(fake), artist=artist, title=title)
        return "new", dest

# ------------------------------------------------------------------ maintenance
def plan_merge(cat):
    """Moves that give every artist one folder, and duplicate songs to retire (dry-run data)."""
    groups = {}
    for d in (p for p in TRACKS.iterdir() if p.is_dir() and not p.name.startswith(".")):
        groups.setdefault(artist_key(d.name), []).append(d)
    moves = []
    for k, ds in groups.items():
        if len(ds) < 2 or not k: continue
        def score(d):   # prefer file-system-safe, mixed-case spellings, then the fuller folder
            n = sum(1 for p in d.iterdir() if p.suffix.lower().lstrip(".") in AUDIO)
            mixed = d.name != d.name.upper() and d.name != d.name.lower()
            return (clean_name(d.name) == d.name, mixed, n)
        canon = max(ds, key=score)
        for d in ds:
            if d == canon: continue
            for e in [e for e in cat.entries if e.path.parent == d]:
                moves.append((e, canon))
    return moves

def merge(apply=False):
    with locked():
        cat = Catalog()
        moves = plan_merge(cat)
        for e, canon in moves:
            name = f"{canon.name} - {e.title}"
            print(f"{'move' if apply else 'would move'}: {e.path.relative_to(TRACKS)} -> {canon.name}/{name}{e.path.suffix}")
            if apply:
                dest = _free_name(canon, name, e.ext, e.dur)
                _place(e.path, dest)
                lst = _read_json(LOSSY_LIST, None)
                if lst and e.stem in lst:
                    lst[f"{canon.name}/{dest.stem}"] = lst.pop(e.stem); _write_json(LOSSY_LIST, lst)
                _event("renamed", dest, old=str(e.path.relative_to(TRACKS)))
                side = e.path.parent / "artist.jpg"
                if side.exists() and not (canon / "artist.jpg").exists(): _place(side, canon / "artist.jpg")
        if apply:
            for e, _ in moves:
                d = e.path.parent
                if d.exists() and not any(p.suffix.lower().lstrip(".") in AUDIO for p in d.iterdir()):
                    for p in d.iterdir():
                        dest = REPLACED / datetime.date.today().isoformat() / p.relative_to(TRACKS)
                        dest.parent.mkdir(parents=True, exist_ok=True); _place(p, dest)
                    d.rmdir()
            cat = Catalog()
        # duplicates: same song more than once (after the merge) -> keep the best, retire the rest
        seen, dups = set(), []
        for e in cat.entries:
            if str(e.path) in seen: continue
            same = [x for x in cat.find(e.dir, e.title, e.dur) if x.path.parent == e.path.parent]
            if len(same) > 1:
                for x in same: seen.add(str(x.path))
                dups.append(same)
        for same in dups:
            keep, rest = same[0], same[1:]
            for x in rest:
                print(f"{'retire' if apply else 'would retire'} duplicate: {x.path.relative_to(TRACKS)}  (keeping {keep.path.name})")
                if apply: _retire(x, f"duplicate of {keep.path.relative_to(TRACKS)}")
        print(f"{len(moves)} files in other folder spellings, {sum(len(s) - 1 for s in dups)} duplicate files"
              + ("" if apply else " (dry run, nothing changed)"))

def purge():
    if not REPLACED.exists(): return
    cutoff = datetime.date.today() - datetime.timedelta(days=KEEP_REPLACED_DAYS)
    for d in REPLACED.iterdir():
        try: day = datetime.date.fromisoformat(d.name)
        except ValueError: continue
        if day < cutoff: shutil.rmtree(d); print(f"purged {d}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["file", "find", "check", "merge", "purge"])
    ap.add_argument("src", nargs="?")
    ap.add_argument("--artist", default=""); ap.add_argument("--title", default="")
    ap.add_argument("--length", type=float, default=0); ap.add_argument("--source", default="")
    ap.add_argument("--id", action="append", default=[]); ap.add_argument("--fake", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--strict", action="store_true", help="discard the file unless it is really --artist/--title with --length")
    ap.add_argument("--file-name", default="", help="source file name of the download, without extension (for --strict)")
    ap.add_argument("--folder", action="append", default=[], help="source folder name of the download (for --strict)")
    ap.add_argument("--loose", action="store_true", help="with --strict: title may contain the requested title if the length is within 3 s")
    a = ap.parse_args()
    if a.cmd == "file":
        action, path = file_into(a.src, a.artist, a.title, a.length, a.source, [i for i in a.id if i], a.fake, a.strict, a.file_name, [f for f in a.folder if f], a.loose)
        print(f"{action}\t{path or ''}")
    elif a.cmd == "find":
        for e in Catalog().find(a.artist, a.title, a.length):
            print(f"{e.path}\t{e.dur:.0f}s\t{e.kbps}kbps\t{'genuine' if e.genuine else 'lossy/fake'}")
    elif a.cmd == "check": merge(apply=False)
    elif a.cmd == "merge": merge(apply=a.apply)
    elif a.cmd == "purge": purge()

if __name__ == "__main__":
    main()
