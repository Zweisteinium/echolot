"""Is this the same song? The rules of the music-sync pipeline (library.py), unchanged.

Same song = same artist (case, accents and punctuation ignored; the first of several artists also
counts) + same title after removing noise that does not change the recording ("(Free DL)",
"[HAK003]", "(Original Mix)", "(feat. X)", "- 2011 Remaster", punctuation) + length within
max(10 s, 4 %) when both lengths are known. Version words (Remix, Edit, Extended, VIP, II,
Pt. 2, ...) stay part of the title: "Glow" and "Glow - X Remix" are different songs.
"""

import re
import unicodedata
from collections.abc import Iterable

_LETTERS = str.maketrans(
    {
        "ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe", "ß": "ss", "ł": "l",
        "Ł": "l", "đ": "d", "Đ": "d", "þ": "th", "Þ": "th", "ı": "i", "$": "s",  # Ke$ha, A$AP
    }
)  # fmt: skip


def fold(s: str | None) -> str:
    """Lower case without accents (ø -> o etc.); non-Latin scripts stay."""
    s = unicodedata.normalize("NFKD", (s or "").translate(_LETTERS))
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def clean_name(s: str | None) -> str:
    """File and folder safe name."""
    s = unicodedata.normalize("NFC", s or "").replace("/", " ").replace("\\", " ")
    s = re.sub(r'[:*?"<>|]', " ", s)
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    return s or "Unknown"


def first_artist(a: str | None) -> str:
    return re.split(
        r"\s*[,;/]\s*|\s+(?:&|x|feat\.?|ft\.?|featuring|vs\.?)\s+", (a or "").strip(), flags=re.I
    )[0].strip()


def artist_key(a: str | None) -> str:
    return re.sub(r"[\W_]+", "", fold(a).replace("&", "and"))


def artist_keys(a: str | None) -> set[str]:
    """Full name and first artist ("Above & Beyond" -> {aboveandbeyond, above})."""
    return {k for k in (artist_key(a), artist_key(first_artist(a))) if k}


_NOISE = [
    r"[\(\[\{]\s*(?:free\s*(?:dl|d/l|download)|freel\s*dl|free|out\s*now|premiere"
    r"|official(?:\s+(?:4k|hd))?(?:\s+(?:audio|video|music\s+video|visuali[sz]er))?"
    r"|visuali[sz]er|lyrics?|hq|hd|explicit|clean|original(?:\s+(?:mix|version))?)\s*[\)\]\}]",
    # catalog numbers like [HAK003]
    r"\[\s*(?!remix|edit|mix|vip|version|rework|bootleg|flip|live)[a-z]{2,6}\s?-?\d{2,5}\s*\]",
    r"[\(\[]\s*(?:feat|ft|featuring|with)\.?\s[^\)\]]*[\)\]]",
    r"\s+(?:feat|ft|featuring)\.?\s[^\-\(\[]*$",
    r"\s+-\s+original(?:\s+(?:mix|version))?\s*$",
    r"\s+-\s+free\s*(?:dl|download)\s*$",
    r"\s+\*?free\s+(?:dl|download)\*?\s*$",
    r"[\(\[]\s*(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?\s*[\)\]]",
    r"\s+-\s+(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?\s*$",
    # ids and catalog numbers like [///A001], [2157720717]
    r"\[\s*(?!remix|edit|mix|vip|version|rework|bootleg|flip|live)[/\\\-_#]*[a-z]{0,6}"
    r"[/\\\-_#\s]*\d{3,}\s*\]",
    r"_\d{6,}\b",  # upload ids glued to titles: Title_1554143200
    r"[\(\[]\s*(?:clean|dirty|explicit)\s*[\)\]]",  # DJ-pool edition tags
    r"\s+(?:clean|dirty)(?=\s+\d{1,2}[ab]\s+\d{2,3}\s*$|\s*$)",
    r"\s+\d{1,2}[ab]\s+\d{2,3}\s*$",  # DJ-pool Camelot key + BPM: "1A 132"
    r"[\(\[][^a-z0-9\(\)\[\]]+[\)\]]",  # parentheses without Latin letters (translations)
    r"[\(\[]\s*mixed\s*[\)\]]",  # DJ-mix cut, see mix_cut()
    r"\s+-\s+mixed\s*$",
]
_MIX_CUT = re.compile(r"[\(\[]\s*mixed\s*[\)\]]|\s+-\s+mixed\s*$", re.I)


def mix_cut(title: str | None) -> bool:
    """A cut out of a continuous DJ mix ("Song - Mixed", "Song (Mixed) - X Remix"): the same song as the
    release, and its length says nothing, so length checks are skipped for it."""
    return bool(_MIX_CUT.search(title or ""))


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


def _words(s: str) -> str:
    return " " + re.sub(r"[\W_]+", " ", fold(s).replace("&", " and ")).strip() + " "


def _strip_track_no(s: str) -> str:
    return re.sub(r"^\s*(?:[a-z]?\d{1,4}|\d{1,2}-\d{1,3})[\s.\-_)]+(?=\S)", "", s, flags=re.I)


def identity_ok(
    artist: str,
    title: str,
    tag_artists: Iterable[str] = (),
    tag_title: str = "",
    file_name: str = "",
    folders: Iterable[str] = (),
    loose: bool = False,
    length_close: bool = False,
) -> tuple[bool, str]:
    """Is a downloaded file really <artist> - <title>? Returns (ok, reason).

    Artist: the wanted artist (full or first) appears as whole words in the file's artist tags or
    in its source file or folder names. Title: the title tag, or the file name (never a folder:
    that is the album) without track number and artist prefix, gives exactly the wanted title.
    loose (Spotify songs): the title tag may also just contain the wanted title's words in order
    ("Edit" vs "Radio Edit") when the length is within 3 s. The artist must always match.
    """
    base = re.sub(r"\s*\([^)]*\)\s*$", "", artist or "")  # Spotify disambiguation: "Vegas (Brazil)"
    want_a = [
        w
        for w in {
            _words(artist).strip(),
            _words(first_artist(artist)).strip(),
            _words(base).strip(),
        }
        if w
    ]
    texts = [t for t in [*tag_artists, file_name, *folders] if t]
    if not want_a or not any(f" {w} " in _words(t) for w in want_a for t in texts):
        return False, f"artist '{artist}' not in {texts}"
    tk = title_key(title)

    def has_artist(x: str) -> bool:
        return any(f" {w} " in _words(x) for w in want_a)

    def readings(name: str) -> list[str]:
        """A tag title or file name read as a plain title: without track number, without
        (repeated) artist prefixes, 'Album - 07 - Title' and reversed 'Title - Artist' forms."""
        m = re.match(r"^[^_]+_[^_]+_\d{1,3}_(.+)$", name)  # "Artist_Album_13_Title"
        name = name.replace("_-_", " - ").replace("_", " ") if name.count("_") > 2 else name
        rest = _strip_track_no(name)
        out = [name, rest]  # unstripped too: "93 Bang Bang", "H2 (...)" start with a number
        if m:
            out.append(m.group(1).replace("_", " "))
        while " - " in rest:
            head, tail = rest.split(" - ", 1)
            if not has_artist(head):
                break
            rest = _strip_track_no(tail)
            out.append(rest)
        m = re.match(r"^.+? - \d{1,3} - (.+)$", rest)
        if m:
            out.append(m.group(1))
        if " - " in rest:
            head, tail = rest.rsplit(" - ", 1)
            if has_artist(tail):
                out.append(head)
        return out

    if tag_title and any(title_key(c) == tk for c in readings(tag_title)):
        return True, "tags"
    if file_name and any(title_key(c) == tk for c in readings(file_name)):
        return True, "file name"
    if loose and length_close and tk:

        def in_order(want: str, have: str) -> bool:  # "... Edit" in "... Radio Edit"
            it = iter(have.split())
            return all(w in it for w in want.split())

        # tag only: file names mix in album names
        for c in readings(tag_title) if tag_title else []:
            if in_order(tk, title_key(c)):
                return True, "title tag has the title words in order, length within 3 s"
    return False, f"title '{title}' is neither tag '{tag_title}' nor file name '{file_name}'"
