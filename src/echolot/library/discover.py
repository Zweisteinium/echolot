"""A song search over several catalogues at once (Deezer, Apple Music, SoundCloud): their hits merged into
one list, best first. The same release from several sources is one result that names them all: the same
artist (any of its keys) and the same title key (rules.title_key: noise like "(Original Mix)" goes,
versions like "Remix" or "Extended" stay) at a length within LENGTH seconds. A result ranks by where each
source placed it and by how many sources suggest it."""

from collections.abc import Iterable
from dataclasses import dataclass, field

from echolot.library import rules

LENGTH = 3.0  # s: two hits of one song may differ this much (a fade, a silent second)
SOURCES = ("deezer", "apple", "soundcloud")  # the order their icons are shown in
WEIGHT = {"deezer": 1.0, "apple": 0.9, "soundcloud": 0.85}  # how much a top place counts per source
TOGETHER = 0.6  # the bonus per further source that suggests the same song


@dataclass
class Hit:
    """One catalogue's answer: position is its place in that source's list (0 first)."""

    source: str
    position: int
    artist: str
    title: str
    seconds: float = 0
    album: str = ""
    url: str = ""  # the song's page at the source
    preview: str = ""  # a short excerpt (Deezer, Apple Music: 30 s mp3)
    cover: str = ""
    year: str = ""

    @property
    def akeys(self) -> set[str]:
        return rules.artist_keys(self.artist)

    @property
    def tkey(self) -> str:
        return rules.title_key(self.title)


@dataclass
class Result:
    """A song as the sources suggest it: the best hit's names, every source's page."""

    hits: list[Hit] = field(default_factory=list)
    score: float = 0.0

    @property
    def best(self) -> Hit:
        return self.hits[0]

    @property
    def artist(self) -> str:
        return self.best.artist

    @property
    def title(self) -> str:
        return self.best.title

    @property
    def seconds(self) -> float:
        return next((h.seconds for h in self.hits if h.seconds), 0)

    @property
    def album(self) -> str:
        return next((h.album for h in self.hits if h.album), "")

    @property
    def year(self) -> str:
        return next((h.year for h in self.hits if h.year), "")

    @property
    def cover(self) -> str:
        return next((h.cover for h in self.hits if h.cover), "")

    @property
    def preview(self) -> str:
        return next((h.preview for h in self.hits if h.preview), "")

    @property
    def sources(self) -> list[Hit]:
        """One hit per source (its best placed), in SOURCES order: the icons and their links."""
        first: dict[str, Hit] = {}
        for h in sorted(self.hits, key=lambda h: h.position):
            first.setdefault(h.source, h)
        return [first[s] for s in SOURCES if s in first]


def same(a: Hit, b: Hit) -> bool:
    """The same song: a shared artist key, the same title key, lengths within LENGTH (or one unknown)."""
    if not a.tkey or a.tkey != b.tkey or not a.akeys & b.akeys:
        return False
    return not a.seconds or not b.seconds or abs(a.seconds - b.seconds) <= LENGTH


def merge(hits: Iterable[Hit]) -> list[Result]:
    """The hits as songs, best first. A hit joins the first result it is the same song as; the result's
    names are its best-placed hit's (a catalogue's tidy names before an uploader's)."""
    results: list[Result] = []
    for hit in sorted(hits, key=lambda h: (h.position, SOURCES.index(h.source) if h.source in SOURCES else 9)):
        home = next((r for r in results if any(same(hit, h) for h in r.hits)), None)
        if home is None:
            results.append(Result([hit]))
        else:
            home.hits.append(hit)
    for r in results:
        places = {s.source: s.position for s in r.sources}
        r.score = sum(WEIGHT.get(src, 0.5) / (1 + pos) for src, pos in places.items()) + TOGETHER * (len(places) - 1)
    return sorted(results, key=lambda r: -r.score)
