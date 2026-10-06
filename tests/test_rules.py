"""The matching rules (rules.py), on cases from real downloads and rejections."""

import pytest

from echolot.library import rules
from echolot.library.rules import artist_key, artist_keys, same_length, title_key


def probable(artist: str, title: str, found: str, *, file_name: str = "", dur: float = 200, length: float = 200):
    return rules.identify(artist, title, [artist], found, file_name, (), dur, length, 3)[0] is not None


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
        ("Song [///A001]", "Song"),
        ("Song [2157720717]", "Song"),
        ("Song (Official Lyric Video)", "Song"),
        ("Song (Lyric Video)", "Song"),
        ("Song (Official Video HD)", "Song"),
        ("Song (Audio)", "Song"),
        ("Song [4K]", "Song"),
        ("Tale Pt. III", "Tale Part III"),
        ("Rock & Roll", "Rock and Roll"),
        ("Straße", "Strasse"),
        ("BEYONCÉ", "Beyonce"),
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
        ("Song", "Song [Mashup 2019]"),  # a version word, not a catalog number
        ("Song", "Song [Acoustic 2021]"),
        ("Song", "Song [Remake 2017]"),
        ("Song", "Song [RMX 2020]"),
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
]


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
    match, why = rules.identify("Other", capo[1], ["Somebody"], capo[2], "", (), 200, 200, 3)
    assert match is None and why.startswith("artist ")


def test_probable_from_file_name() -> None:
    assert probable("Hi-Rez", "Smiling", "", file_name="Hi-Rez_A Walk To Remember_13_Smiling")


@pytest.mark.parametrize(
    ("found", "match"),
    [  # video titles: tilde, en and em dashes separate like " - "; a suffix naming no version is a label or channel
        ("Somewhen - Without You ~ '44 LABEL GROUP'", "probable"),
        ("Somewhen – Without You", "exact"),
        ("Somewhen — Without You (Official Video)", "exact"),
        ("Without You – Somewhen", "exact"),
        ("Somewhen – Without You ~ Somewhen Remix", "probable"),  # the artist's own remix
        ("Somewhen - Say Nothing ~ '44 LABEL GROUP'", None),
        ("Somewhen - Without You ~ VIP", None),
        ("Somewhen – Without You – Other Guy Remix", None),
        ("Somewhen - Without You~Me", None),  # no spaces: part of the title
    ],
)
def test_other_dashes_separate(found: str, match: str | None) -> None:
    assert rules.identify("Somewhen", "Without You", ["Somewhen"], found, found, (), 172, 172, 6)[0] == match
    if match:  # a search result of that name is worth the download
        assert rules.prejudge("Somewhen", "Without You", found, 172, 172)[0] == rules.ACCEPT


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
    if cut:  # the same song
        assert rules.title_key(title) == rules.title_key(release)


@pytest.mark.parametrize(
    ("title", "found", "dur", "match"),
    [
        ("Hells Bells", "AC/DC - Hells Bells (Official 4K Video)", 0, "exact"),  # noise only
        ("Tale Part 2", "Tale Pt. 2", 0, "exact"),
        ("Tale Part 2 (Club Mix)", "Tale Pt. 2 (Club Mix) [HAK003]", 0, "exact"),
        ("Tale Part 2 - Remix", "Tale Pt. 2 (Official Remix)", 200, "probable"),  # part == pt in version words
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
    assert rules.identify("NTO", "Trauma - Worakls Remix", ["N'to"], "Trauma (Worakls Remix)")[0]  # apostrophe


def test_file_name_version_overrules_tags() -> None:
    """Guru Josh Project: the Klaas Vocal Edit is tagged plainly 'Infinity 2008'."""
    args = ("Guru Josh Project", "Infinity 2008", ["Guru Josh Project"], "Infinity 2008")
    assert rules.identify(*args, "Guru Josh Project - Infinity 2008 - Klaas Vocal Edit")[0] is None
    assert rules.identify(*args, "03 - Infinity 2008 (Original Mix)")[0] == "exact"
    assert rules.identify(*args, "Guru Josh Project - Infinity 2008 (Live)")[0] is None
    assert rules.identify(*args, "Guru Josh Project - Club Hits 2009 - 03 - Something Else")[0] == "exact"


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
    assert rules.search_terms("AC/DC", "Hells Bells", 313) == ("AC/DC", "Hells Bells", 313)  # Sockseek #211
    assert rules.search_terms("Neelix, X", "The Twenty Five - Mixed", 103, loosen=True) == (
        "Neelix",
        "The Twenty Five",
        0,
    )
    assert rules.search_terms("A", "Song (feat. B) - Radio Edit", 200, loosen=True) == ("A", "Song", 200)


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
)
def test_prejudge(path: str, length: int, verdict: str) -> None:
    got, _, why = rules.prejudge("Stefan Stürmer", "Paradies - Abrissgebeat Remix", path, length, 200)
    assert got == verdict, why


def test_prejudge_loosened_and_blocked() -> None:
    path = "Music\\Various\\Paradies - Abrissgebeat Remix.flac"
    assert rules.prejudge("Stefan Stürmer", "Paradies - Abrissgebeat Remix", path, 200, 200)[0] == "reject"
    assert rules.prejudge("Stefan Stürmer", "Paradies - Abrissgebeat Remix", path, 200, 200,
                          strict_artist=False)[0] == "unknown"  # fmt: skip
    blocked = rules.prejudge("A", "Song", "A\\A - Song.flac", 0, 0, blocked=["A - Song"])
    assert blocked[0] == "reject"
    cut = rules.prejudge("Neelix", "The Twenty Five - Mixed", "Neelix\\Neelix - The Twenty Five.flac", 295, 103)
    assert cut[0] == "accept"  # a DJ-mix cut: any length


def test_catalog_song_other_artists_and_link() -> None:
    from echolot.library.catalog import Catalog, Entry

    cat = Catalog(
        [Entry("Mabe/Mabe - Atlantis.opus", 350, 160, False), Entry("Pbb Yea/Pbb Yea - Chilln.opus", 227, 160, False)]
    )
    assert cat.song("Catch Vibe", "Atlantis", 349, ["Catch Vibe", "Mabe"])
    assert not cat.song("Catch Vibe", "Atlantis", 349)
    assert cat.song("TheDoDo", "Chilln", 227, ["TheDoDo"], ["Pbb Yea", "Chilln"])
    assert not cat.song("TheDoDo", "Chilln", 227, ["TheDoDo"])


def test_cjk_names_with_or_without_a_space() -> None:
    """Spotify writes "祖堅 正慶", most peers "祖堅正慶" (and some the other way round)."""
    title = "Close to the Heavens"
    together = "Music\\祖堅正慶\\Heavensward (2016)\\15 - Close to the Heavens.flac"
    apart = "Music\\祖堅 正慶\\2016 - Heavensward\\15. Close to the Heavens.flac"
    for path in (together, apart):
        assert rules.prejudge("祖堅 正慶", title, path, 297, 298)[0] == "accept"
        assert rules.prejudge("祖堅正慶", title, path, 297, 298)[0] == "accept"
    assert rules.words("祖堅 正慶 & Keiko") == " 祖堅正慶 and keiko "
    assert len(rules.words("아이유 노래").split()) == 2  # Korean keeps its spaces
    assert rules.prejudge("祖堅 正慶", title, "Music\\植松伸夫\\Close to the Heavens.flac", 297, 298)[0] == "reject"


def test_prejudge_needs_artist_or_title() -> None:
    """A loosened search for "HK - Was!?!?" finds anything with "was" in it: with neither the artist nor
    the title in the path nothing is downloaded; with the title alone the tags decide."""
    title = "Was!?!? (feat. OG Boobie Black)"
    other = "Music\\The Decemberists\\438 - The Decemberists - Here I Dreamt I Was An Architect.mp3"
    assert rules.prejudge("HK", title, other, 0, 180, strict_artist=False)[0] == "reject"
    maybe = "Music\\Deutschrap 2021\\07 - Was!?!?.mp3"
    assert rules.prejudge("HK", title, maybe, 180, 180, strict_artist=False)[0] == "unknown"
    assert rules.prejudge("HK", title, maybe, 180, 180)[0] == "reject"  # the artist is required


def test_prejudge_title_unseen_needs_the_artist_as_a_name_and_a_length() -> None:
    """HK - Was!?!?: "HK" in "HK Gruber" is not the artist; a loosened search downloads a file that does not
    show the title only when its length is known (the length rule decides then)."""
    title = "Was!?!? (feat. OG Boobie Black & Sami Nasser)"
    gruber = (
        "Klassik\\Friedrich Cerha - HK Gruber\\Eine Art Chansons\\31 Eine Art Chansons - Was können sie dir tun.flac"
    )
    assert rules.prejudge("HK", title, gruber, 0, 269)[2] == "the artist only as part of another name"
    compilation = "Music\\HK - World Series USA\\34 Sample Was Borrowed.mp3"
    assert rules.prejudge("HK", title, compilation, 0, 269)[0] == "unknown"
    assert rules.prejudge("HK", title, compilation, 0, 269, loosened=True)[0] == "reject"
    assert rules.prejudge("HK", title, compilation, 270, 269, loosened=True)[0] == "unknown"
    assert rules.prejudge("HK", title, "Rap\\GRiNGO, HK\\Was!!.mp3", 0, 269, loosened=True)[0] == "accept"


def test_named() -> None:
    assert rules.named("HK", "07 - HK - Was") and rules.named("HK", "GRiNGO, HK") and rules.named("HK", "01 HK")
    assert not rules.named("HK", "HK Gruber, Kurt Prihoda") and not rules.named("Scooter", "Scooter Discography")
    assert rules.named("Vegas (Brazil)", "Vegas - Mandala") and rules.named("Above & Beyond", "Above & Beyond - Sun")


def test_own_artist_credit_is_the_song_for_review() -> None:
    """High Tekk's "Irgendwie Irgendwo Irgendwann" is uploaded as "... - HIGH TEKK REMIX": the song as its
    artist released it (Spotify leaves the word out). A search result is taken, and the download is probable
    (Review: only a listener can tell); another remixer, the artist's VIP or a request naming a version stay
    apart."""
    title, sc = "Irgendwie Irgendwo Irgendwann", "Irgendwie Irgendwo Irgendwann - HIGH TEKK REMIX"
    assert rules.prejudge("High Tekk", title, f"High Tekk (Offiziell)/{sc}", 170, 170)[0] == "accept"
    match, why = rules.identify(
        "High Tekk", title, ["High Tekk (Offiziell)"], sc, sc, ["High Tekk (Offiziell)"], 170, 170, 6
    )
    assert match == "probable" and "own artist" in why
    assert rules.identify("High Tekk", title, ["High Tekk"], sc, sc, [], 150, 170, 6)[0] is None  # another length
    other = f"High Tekk/{title} (DJ Foo Remix)"
    assert rules.prejudge("High Tekk", title, other, 170, 170)[0] == "reject"
    assert rules.prejudge("High Tekk", title, f"High Tekk/{title} (High Tekk VIP)", 170, 170)[0] == "reject"
    assert rules.prejudge("Tekk", "Song", "Tekk/Song (High Tekk Remix)", 170, 170)[0] == "reject"  # a longer name
    assert rules.identify("Bar", "Song - Foo Remix", ["Bar"], "Song - Bar Remix", "", [], 170, 170)[0] is None
    collab = "Song (High Tekk & DJ Foo Remix)"  # with another remixer: not the artist's own release
    assert rules.identify("High Tekk", "Song", ["High Tekk"], collab, collab, [], 170, 170, 6)[0] is None
