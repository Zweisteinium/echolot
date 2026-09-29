"""The matching rules (rules.py), on cases from real downloads and rejections."""

import pytest

from echolot import rules
from echolot.rules import artist_key, artist_keys, same_length, title_key


def probable(
    artist: str,
    title: str,
    found: str,
    *,
    file_name: str = "",
    dur: float = 200,
    length: float = 200,
):
    return (
        rules.identify(artist, title, [artist], found, file_name, (), dur, length, 3)[0] is not None
    )


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Song (Original Mix)", "Song"),
        ("Song - Original Mix", "Song"),
        ("Song [HAK003]", "Song"),
        ("Song (feat. Somebody)", "Song"),
        ("Song feat. Somebody", "Song"),
        ("Song - 2011 Remaster", "Song"),
        ("Song (Free DL)", "Song"),
        ("Tale Pt. III", "Tale Part III"),
        ("Rock & Roll", "Rock and Roll"),
    ],
)
def test_same_title(a: str, b: str) -> None:
    assert title_key(a) == title_key(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Glow", "Glow - Nick Schwenderling Remix"),
        ("Fire", "Fire II"),
        ("Song", "Song - Radio Edit"),
        ("Song", "Song (VIP)"),
    ],
)
def test_different_title(a: str, b: str) -> None:
    assert title_key(a) != title_key(b)


def test_artist_keys() -> None:
    assert artist_keys("Above & Beyond") == {"aboveandbeyond", "above"}
    assert artist_key("Ke$ha") == artist_key("Kesha")
    assert artist_key("Røyksopp") == artist_key("Royksopp")


def test_same_length() -> None:
    assert same_length(200, 209)
    assert not same_length(200, 215)
    assert same_length(400, 415)  # 4 % of the longer one
    assert same_length(0, 123)  # unknown length


# (artist, requested title, tag title of the download): the same recording
RIGHT = [
    ("CAPO", "Run Run Run (feat. Yung Kafa & Kücük Efendi) - Remix",
     "CAPO - RUN RUN RUN feat. YUNG KAFA & KÜCÜK EFENDI (prod. von Jurijgold & Falconi) [Official Remix]"),
    ("AC/DC", "Hells Bells", "AC/DC - Hells Bells (Official 4K Video)"),
    ("Linkin Park", "BURN IT DOWN", "BURN IT DOWN (Official Music Video) [4K Upgrade] - Linkin Park"),
    ("Frank Ocean", "In My Room", "Frank Ocean - In My Room (Lyric Video)"),
    ("Mark Terre", "Raindrops (H369) - H369 Remix", "Raindrops (H369 Remix)"),
    ("Tream", "HERZBLATT (AUA AUA) - OsTEKKe & Zombic Remix", "Tream x Mia Julia - HERZBLATT (ZOMBIC x OSTEKKE REMIX)"),
    ("Jaspa", "Auge der Vorsehung", "Jaspa - Auge der Vorsehung | JCC 2020 | Qualifikation #17"),
    ("BODYWORX", "The Push Up Song", "The Push Up Song (Extended Mix)"),  # the length tells edits apart
    ("Stefan Stürmer", "Paradies - Abrissgebeat Remix", "Paradies (Abrissgebeat Remix)"),
]  # fmt: skip

# similar names, other songs or other recordings
WRONG = [
    ("HK", "Was!?!? (feat. OG Boobie Black & Sami Nasser)", "I Was Made for Dancing"),
    ("HK", "Was!?!? (feat. OG Boobie Black & Sami Nasser)", "Was Können Sie Dir Tun = What Can They Do To You"),
    ("Wu-Tang Clan", "Cream", "Strawberries & Cream (feat. Allah Real & Mathematics)"),
    ("5udo", "One", "One - 2017 Remake"),
    ("Nostrum", "Brilliant - Radio Edit", "Brilliant (Hard Trance mix)"),
    ("Oliver Schories", "Caprice", "Caprice (Midas 104 Extended Remix)"),
    ("The Weeknd", "The Hills - Remix", "The Hills (RL Grime Remix)"),
    ("Helge Schneider", "Mr. Bojangles - Live At The Grugahalle / 2014", "Mr. Bojangles"),
    ("Miksu / Macloud", "Nachts wach (Lila Wolken Bootleg)", "Nachts wach"),
    ("Swedish House Mafia", "Ray Of Solar - Tiësto Remix", "Ray Of Solar"),
    ("Marti Fischer", "Chabos wissen, wer der Babo ist - Swing / Jazz Version", "Chabos wissen wer der Babo ist"),
    ("Bachi", "Like U", "BACHI FORTE (SUPER SLOWED)"),
    ("Timmy Trumpet", "Mad World", "Cold"),
    ("Liquid Soul", "Levitate", "Levitate (OxiDaksi Remix)"),
    ("Some Artist", "Fire", "Fire II"),
    ("Some Artist", "Tale", "Tale Pt. 2"),
]  # fmt: skip


@pytest.mark.parametrize(("artist", "title", "found"), RIGHT)
def test_probable_accepts_same_recording(artist: str, title: str, found: str) -> None:
    assert probable(artist, title, found)


@pytest.mark.parametrize(("artist", "title", "found"), WRONG)
def test_probable_rejects_other_song_or_version(artist: str, title: str, found: str) -> None:
    assert not probable(artist, title, found)


def test_probable_needs_length_and_artist() -> None:
    capo = RIGHT[0]
    assert not probable(*capo, dur=210)  # 10 s off
    assert not probable(*capo, dur=0)  # unknown
    assert (
        rules.identify("Other", capo[1], ["Other Artist"], capo[2], "", (), 200, 200, 3)[0] is None
    )


def test_probable_from_file_name() -> None:
    assert probable("Hi-Rez", "Smiling", "", file_name="Hi-Rez_A Walk To Remember_13_Smiling")


def test_segments() -> None:
    head, tail = rules.segments("Run Run Run feat. X (prod. Y) [Official Remix]")
    assert head == "Run Run Run"
    assert tail == ["feat. X", "prod. Y", "Official Remix"]


@pytest.mark.parametrize(
    ("title", "cut", "release"),
    [
        ("The Twenty Five (Official Nature One Anthem 2019) - Mixed", True,
         "The Twenty Five (Official Nature One Anthem 2019)"),
        ("Discopolis 2.0 (Mixed) - MEDUZA Remix", True, "Discopolis 2.0 - MEDUZA Remix"),
        ("Napoleon (Mixed)", True, "Napoleon"),
        ("Adagio for Strings - Unmixed Version", False, "Adagio for Strings - Unmixed Version"),
        ("Mixed Emotions", False, "Mixed Emotions"),
        ("Song - Mixed By DJ X", False, "Song - Mixed By DJ X"),
    ],
)  # fmt: skip
def test_mix_cut(title: str, cut: bool, release: str) -> None:
    assert rules.mix_cut(title) is cut
    assert rules.release_title(title) == release
    assert (rules.title_key(title) == rules.title_key(release)) is True


@pytest.mark.parametrize(
    ("title", "found", "dur", "match"),
    [
        ("Hells Bells", "AC/DC - Hells Bells (Official 4K Video)", 0, "exact"),  # noise only
        ("Tale Part 2", "Tale Pt. 2", 0, "exact"),
        ("Tale Part 2 (Club Mix)", "Tale Pt. 2 (Club Mix) [HAK003]", 0, "exact"),
        (
            "Tale Part 2 - Remix",
            "Tale Pt. 2 (Official Remix)",
            200,
            "probable",
        ),  # part == pt in version words
        ("Song - Edit", "Song (Radio Edit)", 200, "probable"),  # the old 'loose' case
        ("Song - Edit", "Song (Radio Edit)", 210, None),  # ... needs the length
        ("Song", "Song (Hard Trance Mix)", 200, None),  # a named variant
        ("Song", "Other Song", 200, None),
    ],
)
def test_identify(title: str, found: str, dur: int, match: str | None) -> None:
    assert rules.identify("AC/DC", title, ["AC/DC"], found, "", (), dur, 200, 3)[0] == match


def test_identify_artist_first() -> None:
    assert rules.identify("TINOS", "All Night", ["Vanilla"], "All Night")[0] is None
    assert rules.identify("TINOS", "All Night", ["TINOS"], "All Night")[0] == "exact"


def test_file_name_version_overrules_tags() -> None:
    """Guru Josh Project: the Klaas Vocal Edit is tagged plainly 'Infinity 2008'."""
    args = ("Guru Josh Project", "Infinity 2008", ["Guru Josh Project"], "Infinity 2008")
    assert rules.identify(*args, "Guru Josh Project - Infinity 2008 - Klaas Vocal Edit")[0] is None
    assert rules.identify(*args, "03 - Infinity 2008 (Original Mix)")[0] == "exact"
    assert rules.identify(*args, "Guru Josh Project - Infinity 2008 (Live)")[0] is None
    assert (
        rules.identify(*args, "Guru Josh Project - Club Hits 2009 - 03 - Something Else")[0]
        == "exact"
    )


def test_small_gaps() -> None:
    assert rules.identify(
        "NTO", "Trauma - Worakls Remix", ["N'to"], "Trauma (Worakls Remix)", "", (), 0, 0
    )[0]
    assert rules.title_key("10 out 10 [ARONAVA08]") == rules.title_key("10 out 10")
    assert rules.title_key("Liebeslied (Official Lyric Video)") == rules.title_key("Liebeslied")
    assert rules.title_key("Liebeslied (Lyric Video)") == rules.title_key("Liebeslied")


@pytest.mark.parametrize(
    ("artist", "title", "file_name", "match"),
    [  # file names from live Soulseek searches (tools/live_check.py)
        ("Solo Viking", "War Harangue", "02-solo_viking-war_harangue", "exact"),
        ("Trancemaster Krause", "In Flames", "10-trancemaster_krause_-_in_flames-zzzz", "exact"),
        ("Pawlowski", "Now Is The Time", "01-johannes_schuster_x_pawlowski-now_is_the_time_(original_mix)", "exact"),
        ("LAWTON", "Believe In", "Caroline_Roxy__Trancemaster_Krause__Lawton__UK_-Believe_In-Original_Mix-86100923", "exact"),
        ("A-ha", "Take On Me", "01-a-ha-take_on_me", "exact"),
        ("Jay-Z", "Empire State of Mind", "jay-z-empire_state_of_mind-group", "exact"),
        ("Odymel", "The Basement", "1-01 The Basement", "exact"),
        ("Pendulum", "The Tempest", "10 Pendulum - CD-01 - The Tempest", "exact"),
        ("SSIO", "Hör Dir nicht dieses Lied an", "01-06 - Hor Dir Nicht Dieses Lied An", "exact"),
        ("Anyone", "Song", "Anyone - Song-Remix", None),  # a trailing part is a group tag only in scene names
        ("Anyone", "Song", "anyone-song-remix", None),
        ("Anyone", "Song-A-Long", "Anyone - Song-A-Long", "exact"),
        ("Vortex", "Desert", "Portal Vortex", None),
    ],
)  # fmt: skip
def test_file_name_readings(artist: str, title: str, file_name: str, match: str | None) -> None:
    # the Soulseek path has the artist folder
    assert rules.identify(artist, title, [], "", file_name, [artist])[0] == match


def test_search_title() -> None:
    assert rules.search_title("Adagio for Strings - Unmixed Version") == "Adagio for Strings"
    assert rules.search_title("Brilliant - Radio Edit") == "Brilliant"
    assert rules.search_title('Lose Yourself - From "8 Mile" Soundtrack') == "Lose Yourself"
    assert rules.search_title("Run Run Run (feat. Yung Kafa) - Remix") == "Run Run Run - Remix"
    assert rules.search_title("Paradies - Abrissgebeat Remix") == "Paradies - Abrissgebeat Remix"
    assert rules.search_title("Sunrise - 2011 Remaster") == "Sunrise"


def test_search_terms() -> None:
    assert rules.search_terms("AC/DC", "Hells Bells", 313) == ("AC DC", "Hells Bells", 313)
    assert rules.search_terms("Neelix, X", "The Twenty Five - Mixed", 103, loosen=True) == (
        "Neelix",
        "The Twenty Five",
        0,
    )
    assert rules.search_terms("A", "Song (feat. B) - Radio Edit", 200, loosen=True) == (
        "A",
        "Song",
        200,
    )


@pytest.mark.parametrize(
    ("path", "length", "verdict"),
    [
        ("Music\\Stefan Stürmer\\Paradies (Abrissgebeat Remix).flac", 200, "accept"),
        ("Music\\Stefan Stürmer\\01 Paradies.flac", 200, "reject"),  # the original, not the remix
        ("Music\\Stefan Stürmer\\Paradies - Abrissgebeat Remix (Extended).mp3", 260, "reject"),  # length
        ("Music\\Stefan Stürmer - Single\\track01.flac", 201, "unknown"),  # the tags decide
        ("Music\\Someone Else\\Paradies - Abrissgebeat Remix.flac", 200, "reject"),  # artist
        ("Stefan Stürmer\\Paradies - Abrissgebeat Remix.flac", 0, "accept"),  # length unknown
    ],
)  # fmt: skip
def test_prejudge(path: str, length: int, verdict: str) -> None:
    got, _, why = rules.prejudge(
        "Stefan Stürmer", "Paradies - Abrissgebeat Remix", path, length, 200
    )
    assert got == verdict, why


def test_prejudge_loosened_and_blocked() -> None:
    path = "Music\\Various\\Paradies - Abrissgebeat Remix.flac"
    assert (
        rules.prejudge("Stefan Stürmer", "Paradies - Abrissgebeat Remix", path, 200, 200)[0]
        == "reject"
    )
    assert rules.prejudge("Stefan Stürmer", "Paradies - Abrissgebeat Remix", path, 200, 200,
                          strict_artist=False)[0] == "unknown"  # fmt: skip
    blocked = rules.prejudge("A", "Song", "A\\A - Song.flac", 0, 0, blocked=["A - Song"])
    assert blocked[0] == "reject"
    cut = rules.prejudge(
        "Neelix", "The Twenty Five - Mixed", "Neelix\\Neelix - The Twenty Five.flac", 295, 103
    )
    assert cut[0] == "accept"  # a DJ-mix cut: any length


def test_catalog_song_other_artists_and_link() -> None:
    from echolot.library import Catalog, Entry

    cat = Catalog(
        [
            Entry("Mabe/Mabe - Atlantis.opus", 350, 160, False),
            Entry("Pbb Yea/Pbb Yea - Chilln.opus", 227, 160, False),
        ]
    )
    assert cat.song("Catch Vibe", "Atlantis", 349, ["Catch Vibe", "Mabe"])
    assert not cat.song("Catch Vibe", "Atlantis", 349)
    assert cat.song("TheDoDo", "Chilln", 227, ["TheDoDo"], ["Pbb Yea", "Chilln"])
    assert not cat.song("TheDoDo", "Chilln", 227, ["TheDoDo"])


def test_prejudge_needs_artist_or_title() -> None:
    """A loosened search for "HK - Was!?!?" finds anything with "was" in it: with neither the artist nor
    the title in the path nothing is downloaded; with the title alone the tags decide."""
    title = "Was!?!? (feat. OG Boobie Black)"
    other = "Music\\The Decemberists\\438 - The Decemberists - Here I Dreamt I Was An Architect.mp3"
    assert rules.prejudge("HK", title, other, 0, 180, strict_artist=False)[0] == "reject"
    maybe = "Music\\Deutschrap 2021\\07 - Was!?!?.mp3"
    assert rules.prejudge("HK", title, maybe, 180, 180, strict_artist=False)[0] == "unknown"
    assert rules.prejudge("HK", title, maybe, 180, 180)[0] == "reject"  # the artist is required
