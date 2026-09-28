#!/usr/bin/env python3
"""library.py: the single authority on "is this the same song?" and the only code that puts files into /music/tracks.

Same song = same artist (case, accents and punctuation ignored; the first of several artists also counts)
          + same title after removing noise that does not change the recording:
            "(Free DL)", "[HAK003]", "(Original Mix)", "- Original Mix", "(feat. X)", "- 2011 Remaster", punctuation
          + length within max(10 s, 4 %) when both lengths are known.
Version words (Remix, Edit, Extended, Radio Edit, VIP, II, Pt. 2, Mashup, ...) stay part of the title, so
"Glow" and "Glow - Nick Schwenderling Remix", or "Fire" and "Fire II", are different songs.
A DJ-mix cut ("Song - Mixed", "Song (Mixed)") is the song itself, at any length (mix_cut).

Filing rules (file_into), all under one lock shared by every container:
  - nothing in the library is ever overwritten; files are linked into place with an exclusive create
  - a download that is the same song as a library file is discarded, unless it is a genuine lossless copy
    of a lossy/fake one: then it takes over and the old file moves to /music/inbox/replaced/<date>/ (kept 30 days)
  - a different song whose file name is taken (e.g. same title, other length) gets its length appended
  - artist folders are matched case/accent-insensitively, so one artist keeps one folder
  - every filing is appended to /config/logs/downloads.jsonl (for statistics and Echolot's review page)
  - rejected search results that are near misses (right artist, similar length) are kept in /music/inbox/review/<date>/
    for 30 days
  - a download that is only probably the song (identify) is filed marked for review in Echolot

CLI:
  library.py file <src> --artist A --title T [--length S] [--source X] [--id ID] [--fake]  -> prints "<action>\t<path>"
  library.py find --artist A --title T [--length S]
  library.py check            read-only: artist folder spellings and duplicate songs
  library.py merge [--apply]  merge folder spellings and duplicates (dry run unless --apply; losers go to replaced/)
  library.py purge            delete replaced/ and review/ entries older than 30 days"""
import argparse, contextlib, datetime, fcntl, json, os, pathlib, re, shutil, sys, time, unicodedata

TRACKS = pathlib.Path("/music/tracks")
STATE = pathlib.Path("/config/state")
REPLACED = pathlib.Path("/music/inbox/replaced")
REVIEW = pathlib.Path("/music/inbox/review")         # rejected downloads, kept for review
EVENTS = pathlib.Path("/config/logs/downloads.jsonl")
LOCKFILE = STATE / "library.lock"
CACHE = STATE / "library-cache.json"
LOSSY_LIST = STATE / "lossy-sourced.json"      # written by spectrum.py: library stem -> detection result
BLOCKED = STATE / "review-blocked.json"       # song id -> tag titles / file names marked wrong in review
LINKS = STATE / "song-links.json"             # song id -> {artist, title}: the library song it is (review accept)
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

_MIX_CUT_RE = r"[\(\[]\s*mixed\s*[\)\]]|\s+-\s+mixed\s*$"
_NOISE = [
    r"[\(\[\{]\s*(?:free\s*(?:dl|d/l|download)|freel\s*dl|free|out\s*now|premiere|official(?:\s+(?:4k|hd))?(?:\s+(?:audio|video|music\s+video|lyrics?\s+video|visuali[sz]er))?|lyrics?\s+video"
    r"|visuali[sz]er|lyrics?|hq|hd|explicit|clean|original(?:\s+(?:mix|version))?)\s*[\)\]\}]",
    r"\[\s*(?!remix|edit|mix|vip|version|rework|bootleg|flip|live)[a-z]{2,8}\s?-?\d{2,5}\s*\]",   # catalog no. [HAK003]
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
    _MIX_CUT_RE,                                                        # DJ-mix cut, see mix_cut()
]
_MIX_CUT = re.compile(_MIX_CUT_RE, re.I)

def mix_cut(title):
    """A cut out of a continuous DJ mix ("Song - Mixed", "Song (Mixed) - X Remix", from albums like "Kontor
    Festival Sounds ... Mix"): 1-4 minutes with transitions, never released on its own. It is the same song
    as the release, and its length says nothing, so length checks are skipped for it."""
    return bool(_MIX_CUT.search(title or ""))

def release_title(title):
    """The title without a DJ-mix cut marker ("Song (Mixed) - X Remix" -> "Song - X Remix"), for searching."""
    return re.sub(r"\s+", " ", _MIX_CUT.sub("", title or "")).strip()

_FEAT = re.compile(r"[\(\[]\s*(?:feat|ft|featuring|with)\.?\s([^\)\]]*)[\)\]]|\s(?:feat|ft|featuring)\.?\s([^\-\(\[]*)$", re.I)

def feat_keys(t):
    """Artist keys of the featured artists in a title ("Swervin (feat. 6ix9ine)" -> {"6ix9ine"})."""
    names = [n for m in _FEAT.finditer(t or "") for n in m.groups() if n]
    return {k for n in names for a in re.split(r"\s*(?:,|&|\band\b|\bx\b)\s*", n, flags=re.I) if (k := artist_key(a))}

def same_feat(a, b):
    """Titles with featured artists on both sides must share one: "Swervin (feat. 6ix9ine)" and "Swervin
    (feat. Veysel)" are two recordings; a credit on one side only ("Monody (feat. Laura Brehm)") is not."""
    fa, fb = feat_keys(a), feat_keys(b)
    return not fa or not fb or bool(fa & fb)

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
    """' w1 w2 ... ': whole-word form for 'contains' checks; apostrophes vanish ("N'to" = "NTO")."""
    return " " + re.sub(r"[\W_]+", " ", re.sub(r"['’`´]", "", fold(s)).replace("&", " and ")).strip() + " "

def _strip_track_no(s):
    """Without a leading track number: "07 ", "A2. ", disc-track "1-04 ", "CD-01 - ", "Disc 2 - "."""
    return re.sub(r"^\s*(?:(?:cd|disc|disk)[\s\-_]?\d{1,2}|\d{1,2}-\d{1,3}|[a-z]?\d{1,4})[\s.\-_)]+(?=\S)", "", s, flags=re.I)

def identify(artist, title, tag_artists=(), tag_title="", file_name="", folders=(), dur=0, length=0, tol=3):
    """Is this download <artist> - <title>? (Search results can be another artist's song with the same title,
    e.g. "Vanilla - All Night" for "TINOS - All Night".) Returns (match, reason), match one of
      'exact'     the artist appears as whole words in the artist tags or the source file/folder names, and the
                  title tag or file name (never a folder: that is the album), read without track number and artist
                  prefix (_readings), gives exactly the requested title (noise rules of title_key)
      'probable'  the artist as above, the same core title (the part before any bracket, ' - ', '|' or 'feat.'),
                  the same version words (remix, live, VIP, remake, ...), a named variant such as "(Hard Trance Mix)"
                  only if the request names it too, and the length within `tol` seconds. This replaces a list of
                  harmless extras ("(Official 4K Video)", "(prod. von X)", "| JCC 2020") by a list of what makes
                  another recording.
      None        neither."""
    want = _artist_words(artist)
    texts = [t for t in [*tag_artists, file_name, *folders] if t]
    has_artist = lambda x: any(f" {w} " in _words(x) for w in want)
    if not want or not any(has_artist(t) for t in texts):
        return None, f"artist '{artist}' not in {texts}"
    tk = title_key(title)
    artist_words = set(" ".join(want).split())
    readings = {"tags": _readings(tag_title, has_artist) if tag_title else [],
                "file name": _readings(file_name, has_artist) if file_name else []}
    # the file name can name a version the tags leave out ("Infinity 2008 - Klaas Vocal Edit" tagged "Infinity 2008")
    if c := _other_version(title, readings["file name"], artist_words):
        return None, f"file name '{c}' names another version than '{title}'"
    for source, names in readings.items():
        if tk and any(title_key(c) == tk and same_feat(c, title) for c in names): return "exact", source
    if not (dur and length and abs(dur - float(length)) <= tol):
        return None, (f"title '{title}' is neither tag '{tag_title}' nor file name '{file_name}' "
                      f"(a probable match needs the length within {tol} s: {dur or 0:.0f} s, wanted {float(length or 0):.0f} s)")
    names = readings["tags"] + readings["file name"]
    if c := _probable(title, names, artist_words):
        return "probable", f"probable: '{c}' has the core title, the same version words, length within {tol} s"
    return None, f"title '{title}': no reading of tag '{tag_title}' or file name '{file_name}' has the same core title and version"

def _artist_words(artist):
    """The requested artist as whole-word forms: full name, first artist, without a disambiguation suffix."""
    base = re.sub(r"\s*\([^)]*\)\s*$", "", artist or "")               # Spotify disambiguation: "Vegas (Brazil)"
    return [w for w in {_words(artist).strip(), _words(first_artist(artist)).strip(), _words(base).strip()} if w]

def _readings(name, has_artist):
    """Readings of a tag title / file name as a plain title, for comparing with the requested one:
      - with and without a leading track number ("07 ", "1-04 ", "CD-01 - "; a title can start with a number)
      - without (repeated) artist prefixes "Artist - ", and 'Album - 07 - Title', reversed 'Title - Artist'
      - 'Artist_Album_13_Title'
      - scene names without ' - ': "02-solo_viking-war_harangue-grp" -> "war harangue"; every dash is tried as the
        end of the artist ("a-ha-take_on_me"), and a trailing group tag or id is dropped only in real scene names
        (all lower case without spaces) or when it is a number, so "Song-Remix" is never read as "Song"."""
    out = []
    add = lambda x: x and x not in out and out.append(x)
    m = re.match(r"^[^_]+_[^_]+_\d{1,3}_(.+)$", name)      # "Artist_Album_13_Title"
    if m: add(m.group(1).replace("_", " "))
    scene = bool(re.fullmatch(r"[a-z0-9_\-().&'!]+", name))
    name = name.replace("_-_", " - ").replace("_", " ") if name.count("_") > 2 else name
    for rest in (name, _strip_track_no(name)):
        add(rest)
        while " - " in rest:
            head, tail = rest.split(" - ", 1)
            if not has_artist(head): break
            rest = tail; add(rest); add(_strip_track_no(rest))
            if scene: add(re.sub(r"(?<=\S)-[^\s-]+$", "", rest))   # group tag: "in flames-zzzz"
        if m := re.match(r"^.+? - \d{1,3} - (.+)$", rest): add(m.group(1))
        if " - " in rest:
            head, tail = rest.rsplit(" - ", 1)
            if has_artist(tail): add(head)
        parts = [x.strip() for x in re.split(r"\s*-\s*", rest)]
        for i in range(1, len(parts)):                   # dash-joined names (scene releases)
            if not has_artist("-".join(parts[:i])): continue
            tail = parts[i:]
            add(" - ".join(tail))
            if len(tail) > 1 and (scene or tail[-1].isdigit()): add(" - ".join(tail[:-1]))
    return out

# Words that mark another recording of a song: request and download must agree on them.
VERSION_WORDS = {"remix", "remixed", "rmx", "live", "acoustic", "instrumental", "inst", "slowed", "sped", "nightcore",
                 "reverb", "cover", "karaoke", "vip", "bootleg", "mashup", "rework", "flip", "remake", "reprise",
                 "unplugged", "demo", "acapella", "orchestral", "piano", "lofi", "8d", "medley", "tribute", "dub",
                 "megamix", "pt", "ii", "iii", "iv"}
# Words that name no other recording (an edit or extended mix differs in length, which is checked instead)
PLAIN_WORDS = {"original", "radio", "extended", "club", "album", "single", "edit", "mix", "version", "remaster",
               "remastered", "mono", "stereo", "explicit", "clean", "dirty", "short", "long", "full", "unmixed", "mixed",
               "official", "video", "audio", "music", "lyric", "lyrics", "visualizer", "visualiser", "4k", "hd", "hq",
               "upgrade", "prod", "feat", "ft", "featuring", "with", "x", "and", "vs", "by", "von", "und",
               "a", "the", "at", "in", "on", "of", "from", "for", "to", "de", "der", "die", "das"}
_MARKERS = VERSION_WORDS | {"mix", "edit", "version"}      # a segment with one of these names a variant
_SEGMENT = re.compile(r"[\(\)\[\]\{\}|•]|\s+-\s+|\s+//\s+|\s+(?=(?:feat|ft|featuring|prod)\.?\s)", re.I)

def _segments(title):
    """'Run Run Run feat. X (prod. Y) [Official Remix]' -> 'Run Run Run', ['feat. X', 'prod. Y', 'Official Remix']."""
    parts = [p.strip() for p in _SEGMENT.split(title or "") if p and p.strip()]
    return (parts[0], parts[1:]) if parts else ("", [])

def _vwords(s):
    """Words of s, 'part' as 'pt' (as title_key does)."""
    return ["pt" if w == "part" else w for w in _words(s).split()]

def _named(segments, known):
    """Words that name a variant ("RL Grime" in "(RL Grime Remix)") and are not in `known`."""
    return {w for seg in segments if set(_vwords(seg)) & _MARKERS for w in _vwords(seg)
            if w not in PLAIN_WORDS and w not in VERSION_WORDS and w not in known and not w.isdigit()}

def _other_version(title, names, artist_words):
    """The first of `names` with the requested core title that names another version: a version word or a named
    variant the request does not have. None if there is none."""
    head, _ = _segments(title)
    core, want_all = title_key(head), set(_vwords(title))
    for c in names:
        c_head, c_tail = _segments(c)
        if core and title_key(c_head) == core and ((set(_vwords(c)) & VERSION_WORDS) - want_all
                                                   or _named(c_tail, want_all | artist_words) or not same_feat(c, title)):
            return c
    return None

def _probable(title, names, artist_words):
    """The first of `names` (readings of the download) that is probably the requested title, or None (see identify)."""
    head, tail = _segments(title)
    core, want_all = title_key(head), set(_vwords(title))
    if not core: return None
    need = _named(tail, set())                              # remixers etc. the request names must be in the download
    for c in names:
        c_head, c_tail = _segments(c)
        have_all = set(_vwords(c))
        if (title_key(c_head) == core and have_all & VERSION_WORDS == want_all & VERSION_WORDS and need <= have_all
                and not _named(c_tail, want_all | artist_words) and same_feat(c, title)):
            return c
    return None

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
        self.links = _read_json(LINKS, {})

    def song(self, it):
        """Library files for a wanted song (artist, title, length; artists and id/key when known), best first.
        A link set on the review page wins (the same recording under another artist name, e.g. Spotify's
        "Pbb Yea - Chilln" and "TheDoDo - Chilln"); then the song's artist; then its other artists: a
        collaboration Spotify lists twice with the artists swapped ("Mabe, Catch Vibe - Atlantis" and
        "Catch Vibe, Mabe - Atlantis") is one song."""
        link = self.links.get(song_key(it) or "")
        if link and (hits := self.find(link["artist"], link["title"])): return hits
        for a in dict.fromkeys([it["artist"], *(it.get("artists") or [])]):
            if hits := self.find(a, it["title"], it.get("length") or 0): return hits
        return []

    def find(self, artist, title, length=0):
        """Library files that are the same song, best quality first (any length for a DJ-mix cut)."""
        tk = title_key(title)
        if mix_cut(title): length = 0
        if not tk: return []
        hits = {}
        for k in artist_keys(artist):
            for e in self.by_key.get((k, tk), []):
                if same_length(e.dur, length) and same_feat(e.title, title): hits[str(e.path)] = e
        return sorted(hits.values(), key=lambda e: e.rank(), reverse=True)

    def artist_dir(self, artist):
        """Existing folder of this artist (any spelling), else a new one named after the given spelling."""
        for k in [artist_key(artist), artist_key(first_artist(artist))]:
            if k and k in self.dirs:
                return max(self.dirs[k], key=lambda d: sum(1 for _ in d.iterdir()))
        return TRACKS / clean_name(artist)

# ------------------------------------------------------------------ helpers
def norm_key(key):
    """Song keys are 'spotify:<id>' / 'soundcloud:<id>'; Sockseek hands over the URI 'spotify:track:<id>'."""
    return re.sub(r"^spotify:track:", "spotify:", key or "")

def song_key(it):
    """'spotify:<id>' / 'soundcloud:<id>' of a list item (the key music-sync and Echolot use), or None."""
    if it.get("key"): return norm_key(it["key"])
    uri = str(it.get("uri") or "")
    return f"spotify:{it['id']}" if uri.startswith("spotify:") and it.get("id") else None

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
def file_into(src, artist, title, length=0, source="", ids=None, fake=False, strict=False, file_name="", folders=(),
              probable=True, tries=0, match=None, artists=()):
    """Put a downloaded file into the library. Returns (action, library path); action is
    'new', 'upgrade' (replaced a lossy/fake copy), 'duplicate' (discarded, the library already has it),
    'mismatch' (strict: length is not the requested song's, i.e. another version) or
    'wrong-song' (strict: tags / source file name are not the requested artist and title).
    strict is for search results (Soulseek, YouTube): the download must be the song (identify; file_name/folders =
    where it came from). An exact match is filed; a probable one is filed marked match=probable for review in
    Echolot, or, with probable=False (FLAC upgrades replacing a copy, SoundCloud uploader names), kept for review.
    Rejected near misses (the artist matched, 2/3 to 1.5 times the wanted length) are kept in inbox/review/ for
    30 days, so they can still be accepted there; other rejects are deleted.
    artists: all artists of the song; the library copy of any of them (or a review link) is the same song."""
    src = pathlib.Path(src)
    ext = src.suffix.lower().lstrip(".")
    dur, _ = _probe(src)
    ids = [norm_key(i) for i in ids or [] if i]
    if mix_cut(title): length = 0             # a DJ-mix cut: the full release is the song
    info = dict(source=source, ids=ids or [], artist=artist, title=title)
    if strict:
        tag_artists, tag_title = _tags(src)
        info.update(found=tag_title or file_name, file_name=file_name, folders=list(folders), fake=bool(fake), tries=tries)
        tol = 3 if source == "soulseek" else 6              # YouTube videos have intros
        match, why = identify(artist, title, tag_artists, tag_title, file_name, folders, dur, length, tol)
        ok = match == "exact" or (match == "probable" and probable)
        if match == "probable" and not probable: why = f"{why} (not filed: {source} probable matches need a review)"
        if ok and _blocked(ids, [tag_title, file_name]):
            ok, why = False, "this download was marked wrong in review"
        if not ok:
            kept = _keep(src, artist, title, source) if not why.startswith("artist ") else _drop(src)
            _event("wrong-song", kept, reason=why, **info)
            return "wrong-song", None
        if dur and length and not same_length(dur, length):
            near = 2 / 3 <= dur / float(length) <= 1.5
            _event("mismatch", _keep(src, artist, title, source) if near else _drop(src), wanted_seconds=round(float(length)), **info)
            return "mismatch", None
        info["reason"] = why
    if match: info["match"] = match
    length = dur or float(length or 0)
    new_genuine = ext in LOSSLESS and not fake
    with locked():
        cat = Catalog()
        same = cat.song({"artist": artist, "artists": list(artists), "title": title, "length": length,
                         "key": (ids or [None])[0]})
        if same:
            best = same[0]
            if not (new_genuine and not best.genuine):
                src.unlink(missing_ok=True)
                _event("duplicate", best.path, **info)
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
            _event("upgrade", dest, replaced=retired, **info)
            return "upgrade", dest
        folder = cat.artist_dir(artist)
        dest = _free_name(folder, f"{folder.name} - {clean_name(title)}", ext, length)
        _place(src, dest)
        _event("new", dest, **{"fake": bool(fake), **info})
        return "new", dest

def _keep(src, artist, title, source):
    """Move a rejected download to inbox/review/<date>/ (kept KEEP_REPLACED_DAYS days, see Echolot's review page)."""
    src = pathlib.Path(src)
    folder = REVIEW / datetime.date.today().isoformat()
    base = f"{clean_name(artist)} - {clean_name(title)} [{clean_name(source or 'download')}]"
    for n in range(1, 1000):
        dest = folder / (f"{base}{src.suffix.lower()}" if n == 1 else f"{base} ({n}){src.suffix.lower()}")
        if dest.exists(): continue
        try: _place(src, dest); return dest
        except FileExistsError: continue
        except OSError: break
    src.unlink(missing_ok=True)
    return src

def _retag(path, artist, title):
    """Set the artist and title tags (a download accepted on the review page as this song)."""
    try:
        from mutagen import File as MFile
        m = MFile(str(path), easy=True)
        if m is None: return
        if m.tags is None: m.add_tags()
        m["artist"], m["title"] = [artist], [title]
        m.save()
    except Exception:
        pass

def _drop(src):
    """Delete a rejected download that is no near miss (another artist, or a very different length: a mix)."""
    src = pathlib.Path(src); src.unlink(missing_ok=True)
    return src

def _blocked(ids, names):
    """A download of this song was marked wrong in review with this tag title or file name."""
    bad = {norm_key(k): v for k, v in _read_json(BLOCKED, {}).items()}
    return any(n and n in bad.get(norm_key(i), []) for i in ids or [] for n in names)

# ------------------------------------------------------------------ review decisions (Echolot)
def review_apply(d):
    """Apply one decision from Echolot's review page. d: decision, song, path, artist, title, length, source,
    found, file_name, fake. Returns (result, retry): retry = search the song again now.
      ok       a probable match is right: nothing to do
      wrong    a filed probable match is wrong: retire it, never take this download for the song again
      accept   a rejected download kept in inbox/review/ is right: file it (no checks)
      discard  delete such a kept download"""
    decision, song = d.get("decision"), norm_key(d.get("song"))
    if decision == "ok": return "kept", False
    if decision == "wrong":
        names = [n for n in (d.get("found"), d.get("file_name")) if n]
        if song and names:
            bad = _read_json(BLOCKED, {}); bad[song] = sorted(set(bad.get(song, [])) | set(names)); _write_json(BLOCKED, bad)
        p = TRACKS / (d.get("path") or "")
        with locked():
            entry = next((e for e in Catalog().entries if e.path == p), None) if d.get("path") and p.is_file() else None
            if entry: _retire(entry, "marked wrong in review")
        return ("retired" if entry else "already gone"), True
    p = pathlib.Path(d.get("path") or "")
    if not (p.is_absolute() and p.resolve().is_relative_to(REVIEW.resolve()) and p.is_file()): return "file gone", False
    if decision == "discard":
        p.unlink(); return "deleted", False
    if decision == "accept":
        artist, title = d.get("artist", ""), d.get("title", "")
        # the download is tagged with another artist who already has this song in the library: the same recording
        # under two artist names (Spotify lists it twice). Link the song to that file instead of filing a copy.
        tag_artists, _ = _tags(p)
        want = _artist_words(artist)
        dur, _ = _probe(p)
        with locked():                        # released before file_into, which takes it itself
            cat = Catalog()
            for other in tag_artists:
                if any(f" {w} " in _words(other) for w in want) or not (hits := cat.find(other, title, dur)): continue
                if song:
                    links = _read_json(LINKS, {}); links[song] = {"artist": other, "title": title}; _write_json(LINKS, links)
                p.unlink()
                _event("linked", hits[0].path, ids=[song] if song else [], artist=artist, title=title, source=d.get("source", ""),
                       reason=f"same recording as {hits[0].path.relative_to(TRACKS)} ({other})", match="review")
                return f"linked {hits[0].path.relative_to(TRACKS)}", False
        action, dest = file_into(p, artist, title, d.get("length") or 0, d.get("source", ""),
                                 [song] if song else [], bool(d.get("fake")), match="review")
        if action in ("new", "upgrade") and dest:
            _retag(dest, artist, title)       # accepted as this song: its tags say so (downloads often have none or others)
        return f"{action} {dest.relative_to(TRACKS) if dest else ''}".strip(), False
    return f"unknown decision {decision!r}", False

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
    """Delete replaced/ and review/ days older than KEEP_REPLACED_DAYS."""
    cutoff = datetime.date.today() - datetime.timedelta(days=KEEP_REPLACED_DAYS)
    for d in [*(REPLACED.iterdir() if REPLACED.exists() else []), *(REVIEW.iterdir() if REVIEW.exists() else [])]:
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
    ap.add_argument("--no-probable", action="store_true", help="with --strict: keep a probable match for review instead of filing it")
    ap.add_argument("--tries", type=int, default=0, help="earlier searches that did not find the song (logged)")
    ap.add_argument("--artists", default="", help="all artists of the song, '; '-separated (Spotify's artist list)")
    a = ap.parse_args()
    if a.cmd == "file":
        action, path = file_into(a.src, a.artist, a.title, a.length, a.source, [i for i in a.id if i], a.fake, a.strict, a.file_name,
                                 [f for f in a.folder if f], not a.no_probable, a.tries,
                                 artists=[x.strip() for x in a.artists.split("; ") if x.strip()])
        print(f"{action}\t{path or ''}")
    elif a.cmd == "find":
        for e in Catalog().find(a.artist, a.title, a.length):
            print(f"{e.path}\t{e.dur:.0f}s\t{e.kbps}kbps\t{'genuine' if e.genuine else 'lossy/fake'}")
    elif a.cmd == "check": merge(apply=False)
    elif a.cmd == "merge": merge(apply=a.apply)
    elif a.cmd == "purge": purge()

if __name__ == "__main__":
    main()
