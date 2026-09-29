"""Is this the same song? The matching rules, in one place: the library lookup (same song), the check
of downloads (identify) and of search results before they are downloaded (prejudge), and the search
terms. Pure functions, no I/O.

Same song = same artist (case, accents and punctuation ignored; the first of several artists also
counts) + same title after removing noise that does not change the recording ("(Free DL)",
"[HAK003]", "(Original Mix)", "(feat. X)", "- 2011 Remaster", punctuation) + length within
max(10 s, 4 %) when both lengths are known. Version words (Remix, Edit, Extended, VIP, II, Pt. 2,
Mashup, ...) stay part of the title, so "Glow" and "Glow - X Remix", or "Fire" and "Fire II", are
different songs. A DJ-mix cut ("Song - Mixed", "Song (Mixed)") is the song itself, at any length.
"""

import re
import unicodedata
from collections.abc import Callable, Iterable

# ---------------------------------------------------------------- normalisation

_LETTERS = str.maketrans(
    {
        "ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe", "ß": "ss", "ł": "l",
        "Ł": "l", "đ": "d", "Đ": "d", "þ": "th", "Þ": "th", "ı": "i", "$": "s",  # Ke$ha, A$AP
    }
)  # fmt: skip


def fold(s: str | None) -> str:
    """Lower case without accents (ø -> o etc.); non-Latin scripts stay (an ASCII-only fold would
    erase "СВЕТЛАНА")."""
    s = unicodedata.normalize("NFKD", (s or "").translate(_LETTERS))
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def clean_name(s: str | None) -> str:
    """File and folder safe name."""
    s = unicodedata.normalize("NFC", s or "").replace("/", " ").replace("\\", " ")
    s = re.sub(r'[:*?"<>|]', " ", s)
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    return s or "Unknown"


def first_artist(a: str | None) -> str:
    return re.split(r"\s*[,;/]\s*|\s+(?:&|x|feat\.?|ft\.?|featuring|vs\.?)\s+", (a or "").strip(), flags=re.I)[
        0
    ].strip()


def artist_key(a: str | None) -> str:
    return re.sub(r"[\W_]+", "", fold(a).replace("&", "and"))


def artist_keys(a: str | None) -> set[str]:
    """Full name and first artist ("Above & Beyond" -> {aboveandbeyond, above}); empty keys dropped."""
    return {k for k in (artist_key(a), artist_key(first_artist(a))) if k}


_MIX_CUT_RE = r"[\(\[]\s*mixed\s*[\)\]]|\s+-\s+mixed\s*$"
_NOISE = [
    r"[\(\[\{]\s*(?:free\s*(?:dl|d/l|download)|freel\s*dl|free|out\s*now|premiere|official(?:\s+(?:4k|hd))?"
    r"(?:\s+(?:audio|video|music\s+video|lyrics?\s+video|visuali[sz]er))?|lyrics?\s+video"
    r"|visuali[sz]er|lyrics?|hq|hd|explicit|clean|original(?:\s+(?:mix|version))?)\s*[\)\]\}]",
    # catalog numbers [HAK003]
    r"\[\s*(?!remix|edit|mix|vip|version|rework|bootleg|flip|live)[a-z]{2,8}\s?-?\d{2,5}\s*\]",
    r"[\(\[]\s*(?:feat|ft|featuring|with)\.?\s[^\)\]]*[\)\]]",
    r"\s+(?:feat|ft|featuring)\.?\s[^\-\(\[]*$",
    r"\s+-\s+original(?:\s+(?:mix|version))?\s*$",
    r"\s+-\s+free\s*(?:dl|download)\s*$",
    r"\s+\*?free\s+(?:dl|download)\*?\s*$",
    r"[\(\[]\s*(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?\s*[\)\]]",
    r"\s+-\s+(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?\s*$",
    # ids and catalog numbers like [///A001], [2157720717]
    r"\[\s*(?!remix|edit|mix|vip|version|rework|bootleg|flip|live)[/\\\-_#]*[a-z]{0,6}[/\\\-_#\s]*\d{3,}\s*\]",
    r"_\d{6,}\b",  # upload ids glued to titles: Title_1554143200
    r"[\(\[]\s*(?:clean|dirty|explicit)\s*[\)\]]",  # DJ-pool edition tags
    r"\s+(?:clean|dirty)(?=\s+\d{1,2}[ab]\s+\d{2,3}\s*$|\s*$)",
    r"\s+\d{1,2}[ab]\s+\d{2,3}\s*$",  # DJ-pool Camelot key + BPM: "1A 132"
    r"[\(\[][^a-z0-9\(\)\[\]]+[\)\]]",  # parentheses without Latin letters (translations)
    _MIX_CUT_RE,  # DJ-mix cut, see mix_cut()
]
_MIX_CUT = re.compile(_MIX_CUT_RE, re.I)


def mix_cut(title: str | None) -> bool:
    """A cut out of a continuous DJ mix ("Song - Mixed", "Song (Mixed) - X Remix", from albums like
    "Kontor Festival Sounds ... Mix"): 1-4 minutes with transitions, never released on its own. It is
    the same song as the release, and its length says nothing, so length checks are skipped for it."""
    return bool(_MIX_CUT.search(title or ""))


def release_title(title: str | None) -> str:
    """The title without a DJ-mix cut marker ("Song (Mixed) - X Remix" -> "Song - X Remix")."""
    return re.sub(r"\s+", " ", _MIX_CUT.sub("", title or "")).strip()


_FEAT = re.compile(
    r"[\(\[]\s*(?:feat|ft|featuring|with)\.?\s([^\)\]]*)[\)\]]|\s(?:feat|ft|featuring)\.?\s([^\-\(\[]*)$", re.I
)


def feat_keys(t: str | None) -> set[str]:
    """Artist keys of the featured artists in a title ("Swervin (feat. 6ix9ine)" -> {"6ix9ine"})."""
    names = [n for m in _FEAT.finditer(t or "") for n in m.groups() if n]
    return {k for n in names for a in re.split(r"\s*(?:,|&|\band\b|\bx\b)\s*", n, flags=re.I) if (k := artist_key(a))}


def same_feat(a: str | None, b: str | None) -> bool:
    """Titles with featured artists on both sides must share one: "Swervin (feat. 6ix9ine)" and
    "Swervin (feat. Veysel)" are two recordings; a credit on one side only is not."""
    fa, fb = feat_keys(a), feat_keys(b)
    return not fa or not fb or bool(fa & fb)


def title_key(t: str | None) -> str:
    s = fold(t).replace("&", " and ")
    prev = None
    while prev != s:
        prev = s
        for pat in _NOISE:
            s = re.sub(pat, " ", s, flags=re.I)
    s = re.sub(r"[\W_]+", " ", s).strip()
    return re.sub(r"\bpart\b", "pt", s)  # "Pt. III" == "Part III"


def same_length(a: float | str | None, b: float | str | None) -> bool:
    a, b = float(a or 0), float(b or 0)
    return not a or not b or abs(a - b) <= max(10.0, 0.04 * max(a, b))


def norm_key(key: str | None) -> str:
    """Song keys are 'spotify:<id>' / 'soundcloud:<id>'; Sockseek knows songs as 'spotify:track:<id>'."""
    return re.sub(r"^spotify:track:", "spotify:", key or "")


def words(s: str | None) -> str:
    """' w1 w2 ... ': whole-word form for 'contains' checks; apostrophes vanish ("N'to" = "NTO")."""
    s = re.sub(r"['’`´]", "", fold(s)).replace("&", " and ")
    return " " + re.sub(r"[\W_]+", " ", s).strip() + " "


def _strip_track_no(s: str) -> str:
    """Without a leading track number: "07 ", "A2. ", disc-track "1-04 ", "CD-01 - ", "Disc 2 - "."""
    return re.sub(
        r"^\s*(?:(?:cd|disc|disk)[\s\-_]?\d{1,2}|\d{1,2}-\d{1,3}|[a-z]?\d{1,4})[\s.\-_)]+(?=\S)", "", s, flags=re.I
    )


# ---------------------------------------------------------------- is this download the song?


def artist_words(artist: str) -> list[str]:
    """The requested artist as whole-word forms: full name, first artist, without a disambiguation
    suffix (Spotify: "Vegas (Brazil)")."""
    base = re.sub(r"\s*\([^)]*\)\s*$", "", artist or "")
    forms = {words(artist).strip(), words(first_artist(artist)).strip(), words(base).strip()}
    return [w for w in forms if w]


def identify(
    artist: str,
    title: str,
    tag_artists: Iterable[str] = (),
    tag_title: str = "",
    file_name: str = "",
    folders: Iterable[str] = (),
    dur: float = 0,
    length: float = 0,
    tol: float = 3,
) -> tuple[str | None, str]:
    """Is this download <artist> - <title>? (Search results can be another artist's song with the same
    title.) Returns (match, reason), match one of
      'exact'     the artist appears as whole words in the artist tags or the source file/folder names,
                  and the title tag or file name (never a folder: that is the album), read without track
                  number and artist prefix, gives exactly the requested title (noise rules of title_key)
      'probable'  the artist as above, the same core title (the part before any bracket, ' - ', '|' or
                  'feat.'), the same version words (remix, live, VIP, remake, ...), a named variant such
                  as "(Hard Trance Mix)" only if the request names it too, and the length within `tol`
      None        neither."""
    want = artist_words(artist)
    texts = [t for t in [*tag_artists, file_name, *folders] if t]

    def has_artist(x: str) -> bool:
        return any(f" {w} " in words(x) for w in want)

    if not want or not any(has_artist(t) for t in texts):
        return None, f"artist '{artist}' not in {texts}"
    tk = title_key(title)
    known = set(" ".join(want).split())
    readings = {
        "tags": _readings(tag_title, has_artist) if tag_title else [],
        "file name": _readings(file_name, has_artist) if file_name else [],
    }
    # the file name can name a version the tags leave out ("Infinity 2008 - Klaas Vocal Edit")
    if c := other_version(title, readings["file name"], known):
        return None, f"file name '{c}' names another version than '{title}'"
    for source, names in readings.items():
        if tk and any(title_key(c) == tk and same_feat(c, title) for c in names):
            return "exact", source
    if not (dur and length and abs(dur - float(length)) <= tol):
        return None, (
            f"title '{title}' is neither tag '{tag_title}' nor file name '{file_name}' "
            f"(a probable match needs the length within {tol:.0f} s: {dur or 0:.0f} s, "
            f"wanted {float(length or 0):.0f} s)"
        )
    if c := probable(title, readings["tags"] + readings["file name"], known):
        return ("probable", f"probable: '{c}' has the core title, the same version words, length within {tol:.0f} s")
    return None, (
        f"title '{title}': no reading of tag '{tag_title}' or file name '{file_name}' "
        "has the same core title and version"
    )


def _readings(name: str, has_artist: Callable[[str], bool]) -> list[str]:
    """Readings of a tag title / file name as a plain title, for comparing with the requested one:
    - with and without a leading track number ("07 ", "1-04 ", "CD-01 - ")
    - without (repeated) artist prefixes "Artist - ", 'Album - 07 - Title', reversed 'Title - Artist'
    - 'Artist_Album_13_Title'
    - scene names without ' - ': "02-solo_viking-war_harangue-grp" -> "war harangue"; every dash is
      tried as the end of the artist ("a-ha-take_on_me"), and a trailing group tag or id is dropped
      only in real scene names (lower case, no spaces) or when it is a number."""
    out: list[str] = []

    def add(x: str) -> None:
        if x and x not in out:
            out.append(x)

    if m := re.match(r"^[^_]+_[^_]+_\d{1,3}_(.+)$", name):  # "Artist_Album_13_Title"
        add(m.group(1).replace("_", " "))
    scene = bool(re.fullmatch(r"[a-z0-9_\-().&'!]+", name))
    name = name.replace("_-_", " - ").replace("_", " ") if name.count("_") > 2 else name
    for rest in (name, _strip_track_no(name)):
        add(rest)
        while " - " in rest:
            head, tail = rest.split(" - ", 1)
            if not has_artist(head):
                break
            rest = tail
            add(rest)
            add(_strip_track_no(rest))
            if scene:
                add(re.sub(r"(?<=\S)-[^\s-]+$", "", rest))  # group tag: "in flames-zzzz"
        if m := re.match(r"^.+? - \d{1,3} - (.+)$", rest):
            add(m.group(1))
        if " - " in rest:
            head, tail = rest.rsplit(" - ", 1)
            if has_artist(tail):
                add(head)
        parts = [x.strip() for x in re.split(r"\s*-\s*", rest)]
        for i in range(1, len(parts)):  # dash-joined names (scene releases)
            if not has_artist("-".join(parts[:i])):
                continue
            tail = parts[i:]
            add(" - ".join(tail))
            if len(tail) > 1 and (scene or tail[-1].isdigit()):
                add(" - ".join(tail[:-1]))
    return out


# Words that mark another recording of a song: request and download must agree on them.
VERSION_WORDS = {
    "remix", "remixed", "rmx", "live", "acoustic", "instrumental", "inst", "slowed", "sped",
    "nightcore", "reverb", "cover", "karaoke", "vip", "bootleg", "mashup", "rework", "flip",
    "remake", "reprise", "unplugged", "demo", "acapella", "orchestral", "piano", "lofi", "8d",
    "medley", "tribute", "dub", "megamix", "pt", "ii", "iii", "iv",
}  # fmt: skip
# Words that name no other recording (an edit or extended mix differs in length, which is checked)
PLAIN_WORDS = {
    "original", "radio", "extended", "club", "album", "single", "edit", "mix", "version",
    "remaster", "remastered", "mono", "stereo", "explicit", "clean", "dirty", "short", "long",
    "full", "unmixed", "mixed", "official", "video", "audio", "music", "lyric", "lyrics",
    "visualizer", "visualiser", "4k", "hd", "hq", "upgrade", "prod", "feat", "ft", "featuring",
    "with", "x", "and", "vs", "by", "von", "und", "a", "the", "at", "in", "on", "of", "from",
    "for", "to", "de", "der", "die", "das",
}  # fmt: skip
_MARKERS = VERSION_WORDS | {"mix", "edit", "version"}  # a segment with one of these names a variant
_SEGMENT = re.compile(r"[\(\)\[\]\{\}|•]|\s+-\s+|\s+//\s+|\s+(?=(?:feat|ft|featuring|prod)\.?\s)", re.I)


def segments(title: str | None) -> tuple[str, list[str]]:
    """'Run Run Run feat. X (prod. Y) [Official Remix]' -> 'Run Run Run', ['feat. X', 'prod. Y',
    'Official Remix']."""
    parts = [p.strip() for p in _SEGMENT.split(title or "") if p and p.strip()]
    return (parts[0], parts[1:]) if parts else ("", [])


def _vwords(s: str) -> list[str]:
    """Words of s, 'part' as 'pt' (as title_key does)."""
    return ["pt" if w == "part" else w for w in words(s).split()]


def _named(segs: list[str], known: set[str]) -> set[str]:
    """Words that name a variant ("RL Grime" in "(RL Grime Remix)") and are not in `known`."""
    return {
        w
        for seg in segs
        if set(_vwords(seg)) & _MARKERS
        for w in _vwords(seg)
        if w not in PLAIN_WORDS and w not in VERSION_WORDS and w not in known and not w.isdigit()
    }


def other_version(title: str, names: list[str], artist_words_: set[str]) -> str | None:
    """The first of `names` with the requested core title that names another version: a version word
    or a named variant the request does not have. None if there is none."""
    head, _ = segments(title)
    core, want_all = title_key(head), set(_vwords(title))
    for c in names:
        c_head, c_tail = segments(c)
        if (
            core
            and title_key(c_head) == core
            and (
                (set(_vwords(c)) & VERSION_WORDS) - want_all
                or _named(c_tail, want_all | artist_words_)
                or not same_feat(c, title)
            )
        ):
            return c
    return None


def probable(title: str, names: list[str], artist_words_: set[str]) -> str | None:
    """The first of `names` (readings of the download) that is probably the requested title."""
    head, tail = segments(title)
    core, want_all = title_key(head), set(_vwords(title))
    if not core:
        return None
    need = _named(tail, set())  # remixers etc. the request names must be in the download
    for c in names:
        c_head, c_tail = segments(c)
        have_all = set(_vwords(c))
        if (
            title_key(c_head) == core
            and have_all & VERSION_WORDS == want_all & VERSION_WORDS
            and need <= have_all
            and not _named(c_tail, want_all | artist_words_)
            and same_feat(c, title)
        ):
            return c
    return None


# ---------------------------------------------------------------- search results before download

ACCEPT, UNKNOWN, REJECT = "accept", "unknown", "reject"


def prejudge(
    artist: str,
    title: str,
    path: str,
    length: float = 0,
    wanted: float = 0,
    strict_artist: bool = True,
    blocked: Iterable[str] = (),
) -> tuple[str, int, str]:
    """A search result, judged from its path and length before it is downloaded: (verdict, rank,
    reason). Only what is certain from the name rejects: the artist missing (when the search requires
    it), a file name naming another version, a length that makes it another song, or a download
    marked wrong in review, or the requested title without the version it asks for ("Paradies" for
    "Paradies - Abrissgebeat Remix"). With the artist in the path, a name that shows the title is
    accepted (rank 0 exact, 1 probable) and one that says nothing about it is unknown (rank 2): its tags
    decide after the download. Without the artist (a loosened search), only a name showing the title is
    worth a download (unknown: the tags must name the artist)."""
    parts = [p for p in re.split(r"[\\/]+", path) if p]
    name = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", parts[-1]) if parts else ""
    # the artist is often a few levels up: Music\\Artist\\Singles\\Song\\01. Song.flac
    folders = parts[:-1]
    if name in set(blocked):
        return REJECT, 9, "marked wrong in review"
    if wanted and length and not mix_cut(title) and not same_length(length, wanted):
        return REJECT, 9, f"length {length:.0f} s, wanted {wanted:.0f} s"
    want = artist_words(artist)

    def has_artist(x: str) -> bool:
        return any(f" {w} " in words(x) for w in want)

    artist_seen = any(has_artist(t) for t in [name, *folders])
    if not artist_seen and strict_artist:
        return REJECT, 9, "artist not in the path"
    known = set(" ".join(want).split())
    names = _readings(name, has_artist)
    if c := other_version(title, names, known):
        return REJECT, 9, f"'{c}' names another version"
    versions = set(_vwords(title)) & VERSION_WORDS  # a remix, live, Pt. 2 ... is asked for
    core = title_key(segments(title)[0])
    cores = [c for c in names if title_key(segments(c)[0]) == core]
    if versions and cores and not any(versions <= set(_vwords(c)) for c in cores):
        return REJECT, 9, f"the file name lacks '{' '.join(sorted(versions))}' (another recording)"
    tk = title_key(title)
    exact = bool(tk) and any(title_key(c) == tk and same_feat(c, title) for c in names)
    close = exact or bool(length and wanted and abs(length - wanted) <= 3 and probable(title, names, known))
    if not artist_seen:  # a loosened search: the tags must name the artist
        if close:
            return UNKNOWN, 2, "title in the file name, artist unseen"
        return REJECT, 9, "neither artist nor title in the path"
    if exact:
        return ACCEPT, 0, "exact"
    if close:
        return ACCEPT, 1, "probable"
    return UNKNOWN, 2, "title not in the file name"


# ---------------------------------------------------------------- search terms


def search_title(title: str) -> str:
    """Title for a loosened search: without feat. credits, 'From "Film"' and a trailing ' - Radio Edit',
    ' - Unmixed Version', ' - 2011 Remaster' (plain words only; ' - X Remix' stays)."""
    t = re.sub(r"\s*[\(\[]\s*(?:feat|ft|featuring|with|from)\.?\s[^\)\]]*[\)\]]", "", title, flags=re.I)
    t = re.sub(r"\s+-\s+from\s.*$", "", t, flags=re.I)
    m = re.match(r"^(.+?)\s+-\s+([^-]+)$", t)
    if m and all(w in PLAIN_WORDS or w.isdigit() for w in words(m.group(2)).split()):
        t = m.group(1)
    return t.strip() or title


def search_terms(artist: str, title: str, length: float = 0, loosen: bool = False) -> tuple[str, str, int]:
    """(artist, title, length) to search for. loosen: first artist, search_title. A DJ-mix cut is
    searched as the release at any length. "/" and "\\" cannot occur in a Soulseek path, so they
    become spaces ("AC/DC" finds AC_DC, AC DC)."""
    cut = mix_cut(title)  # of the requested title: search_title drops a plain " - Mixed"
    if loosen:
        artist, title = first_artist(artist) or artist, search_title(title)
    if cut:
        title = release_title(title)
    return re.sub(r"\s*[/\\]+\s*", " ", artist).strip(), title, 0 if cut else int(length or 0)
