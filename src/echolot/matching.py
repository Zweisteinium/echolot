"""Is this the same song? The library rules of the music-sync pipeline (library.py), unchanged; the check of
downloads (identify) stays in the pipeline.

Same song = same artist (case, accents and punctuation ignored; the first of several artists also
counts) + same title after removing noise that does not change the recording ("(Free DL)",
"[HAK003]", "(Original Mix)", "(feat. X)", "- 2011 Remaster", punctuation) + length within
max(10 s, 4 %) when both lengths are known. Version words (Remix, Edit, Extended, VIP, II,
Pt. 2, ...) stay part of the title: "Glow" and "Glow - X Remix" are different songs.
"""

import re
import unicodedata

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


_MIX_CUT_RE = r"[\(\[]\s*mixed\s*[\)\]]|\s+-\s+mixed\s*$"
_NOISE = [
    r"[\(\[\{]\s*(?:free\s*(?:dl|d/l|download)|freel\s*dl|free|out\s*now|premiere"
    r"|official(?:\s+(?:4k|hd))?(?:\s+(?:audio|video|music\s+video|lyrics?\s+video|visuali[sz]er))?"
    r"|lyrics?\s+video"
    r"|visuali[sz]er|lyrics?|hq|hd|explicit|clean|original(?:\s+(?:mix|version))?)\s*[\)\]\}]",
    # catalog numbers like [HAK003]
    r"\[\s*(?!remix|edit|mix|vip|version|rework|bootleg|flip|live)[a-z]{2,8}\s?-?\d{2,5}\s*\]",
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
    _MIX_CUT_RE,  # DJ-mix cut, see mix_cut()
]
_MIX_CUT = re.compile(_MIX_CUT_RE, re.I)


def mix_cut(title: str | None) -> bool:
    """A cut out of a continuous DJ mix ("Song - Mixed", "Song (Mixed) - X Remix"): the same song as the
    release, and its length says nothing, so length checks are skipped for it."""
    return bool(_MIX_CUT.search(title or ""))


_FEAT = re.compile(
    r"[\(\[]\s*(?:feat|ft|featuring|with)\.?\s([^\)\]]*)[\)\]]|\s(?:feat|ft|featuring)\.?\s([^\-\(\[]*)$",
    re.I,
)


def feat_keys(t: str | None) -> set[str]:
    """Artist keys of the featured artists in a title ("Swervin (feat. 6ix9ine)" -> {"6ix9ine"})."""
    names = [n for m in _FEAT.finditer(t or "") for n in m.groups() if n]
    return {
        k
        for n in names
        for a in re.split(r"\s*(?:,|&|\band\b|\bx\b)\s*", n, flags=re.I)
        if (k := artist_key(a))
    }


def same_feat(a: str | None, b: str | None) -> bool:
    """Featured artists on both sides must share one: "Swervin (feat. 6ix9ine)" and "Swervin (feat. Veysel)"
    are two recordings; a credit on one side only ("Monody (feat. Laura Brehm)") is not."""
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
