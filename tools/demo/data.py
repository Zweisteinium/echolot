"""The demo's data: a made-up Echolot library (users rick, sam and jo, a dozen lists, some five thousand songs,
two weeks of history) for the README video. Built from scratch with a fixed seed; times are relative to now, so
it is built right before recording (make.py does that).

    uv run python tools/demo/data.py <work dir> [--fast]

writes <work dir>/data/echolot.db and <work dir>/music (only the files a scene plays exist; the rest are rows).
--fast: no audio files and a snapshot every 6 hours (the test). Rows are written by column name, and a column
the schema no longer has is left out with a note (write), so a schema change seldom needs a change here. The
history is not made up: the state is replayed in the order songs and files came in, and Echolot's own snapshot
code (history.collect) counts each hour. What the scenes rely on is checked at the end (contract)."""

import datetime as dt
import json
import math
import random
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pools import (
    EXTRA_HITS,
    FEATS,
    HITS,
    NCS,
    POP_ARTISTS,
    POP_TITLES,
    STORY_HITS,
    SUFFIXES,
    TECHNO,
    TECHNO_ARTISTS,
    TECHNO_SUFFIX,
    TRANCE,
    TRANCE_ARTISTS,
    TRANCE_SUFFIX,
    WORDS_A,
    WORDS_B,
)

from echolot import db
from echolot.jobs.lists import GREYED_OUT, NOT_ON_SOUNDCLOUD
from echolot.library import history
from echolot.library.rules import clean_name

R = random.Random(20261006)
DEMO = Path(sys.argv[1])
FAST = "--fast" in sys.argv[2:]
DATA, MUSIC = DEMO / "data", DEMO / "music"
DBP = DATA / "echolot.db"
for p in DATA.glob("echolot.db*"):
    p.unlink()
db.init(DBP)
con = db.connect(DBP)
con.execute("PRAGMA foreign_keys = OFF")
COLUMNS: dict[str, list[str]] = {}
LEFT_OUT: set[tuple[str, str]] = set()


def write(table: str, row: dict, verb: str = "INSERT") -> int:
    """One row by column name; a column the table no longer has is left out (noted once)."""
    cols = COLUMNS.setdefault(table, [r[1] for r in con.execute(f"PRAGMA table_info({table})")])
    for gone in row.keys() - set(cols):
        if (table, gone) not in LEFT_OUT:
            LEFT_OUT.add((table, gone))
            print(f"note: {table}.{gone} is not in the schema any more, left out", file=sys.stderr)
    keys = [k for k in row if k in cols]
    sql = f"{verb} INTO {table} ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})"
    return con.execute(sql, [row[k] for k in keys]).lastrowid or 0


NOW = dt.datetime.now().replace(microsecond=0)
TODAY = NOW.date().isoformat()
START = NOW - dt.timedelta(days=13, hours=5)  # first sync


def ago(minutes: float) -> str:
    return (NOW - dt.timedelta(minutes=minutes)).isoformat(timespec="seconds")


def unix_ago(minutes: float) -> int:
    return int((NOW - dt.timedelta(minutes=minutes)).timestamp())


DAYS = 13  # since the first sync


def sig(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def grown(t: float) -> float:  # the library's share at day t (0: the first sync), rising in bursts
    return (
        0.10
        + 0.33 * (1 - math.exp(-t / 0.35))
        + 0.17 * sig((t - 2.6) / 0.05)
        + 0.12 * sig((t - 5.4) / 0.05)
        + 0.07 * sig((t - 8.1) / 0.04)
        + 0.04 * sig((t - 10.7) / 0.04)
        + 0.011 * t
    )


def listed(t: float) -> float:  # the lists' share at day t
    return 0.86 + 0.08 * (1 - math.exp(-t / 0.2)) + 0.03 * sig((t - 5.3) / 0.05) + 0.003 * t


def came_in(curve) -> dt.datetime:
    """When something came in, drawn by the curve: before the first sync (what was there) or during the days since."""
    u = R.random() * curve(DAYS)
    if u < curve(0):
        return START - dt.timedelta(days=R.uniform(1, 400))
    lo, hi = 0.0, float(DAYS)
    while hi - lo > 1e-4:
        lo, hi = (lo, (lo + hi) / 2) if curve((lo + hi) / 2) >= u else ((lo + hi) / 2, hi)
    return min(START + dt.timedelta(days=hi), NOW - dt.timedelta(minutes=55))


# ------------------------------------------------------------------ users, lists, sources
users = [
    (1, "rick", "2026-09-23T18:02:11", ago(3), 0, "nd-1", 1, "", "mine", 0, "rick"),
    (2, "sam", "2026-09-25T20:14:40", ago(60 * 26), 0, "nd-2", 0, "review", "mine", 0, ""),
    (3, "jo", "2026-09-29T09:31:05", ago(60 * 75), 0, "nd-3", 0, "", "mine", 0, ""),
]
USER_COLS = [
    "id",
    "name",
    "created",
    "last_login",
    "admin",
    "navidrome_id",
    "navidrome_admin",
    "permissions",
    "view",
    "disabled",
    "soundcloud_user",
]
for u in users:
    write("users", dict(zip(USER_COLS, u, strict=True)))

LISTS = [  # key, service, title, url, creator
    ("spotify:likes:1", "spotify", "Liked Songs", "https://open.spotify.com/collection/tracks", "rick"),
    ("spotify:playlist:demo1", "spotify", "Trance Classics", "https://open.spotify.com/playlist/demo1", "rick"),
    ("spotify:playlist:demo2", "spotify", "Hits", "https://open.spotify.com/playlist/demo2", "rick"),
    ("spotify:playlist:demo3", "spotify", "Techno", "https://open.spotify.com/playlist/demo3", "rick"),
    ("spotify:playlist:demo4", "spotify", "Night Drive", "https://open.spotify.com/playlist/demo4", "rick"),
    ("spotify:playlist:demo5", "spotify", "Gym", "https://open.spotify.com/playlist/demo5", "rick"),
    ("spotify:playlist:demo6", "spotify", "Chill", "https://open.spotify.com/playlist/demo6", "rick"),
    ("soundcloud:rick/likes", "soundcloud", "Likes", "https://soundcloud.com/rick/likes", "rick"),
    (
        "soundcloud:djsets/sets/trance",
        "soundcloud",
        "Trance Sets",
        "https://soundcloud.com/djsets/sets/trance",
        "djsets",
    ),
    (
        "youtube:PLdemo1",
        "youtube",
        "NCS Releases",
        "https://www.youtube.com/playlist?list=PLdemo1",
        "NoCopyrightSounds",
    ),
    ("spotify:likes:2", "spotify", "Liked Songs", "https://open.spotify.com/collection/tracks", "sam"),
    ("youtube:PLdemo2", "youtube", "Workout Mix", "https://www.youtube.com/playlist?list=PLdemo2", "jo"),
]
for pos, (key, service, title, url, creator) in enumerate(LISTS):
    con.execute(
        "INSERT INTO lists (key, service, title, url, position, playlist, fetched, snapshot, fetched_at, creator) "
        "VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?, ?)",
        (key, service, title, url, pos, "demo" if service == "spotify" else None, ago(R.randint(5, 50)), creator),
    )
FOLLOWS = {
    1: [k for k, *_ in LISTS[:10]],
    2: ["spotify:likes:2", "spotify:playlist:demo2", "spotify:playlist:demo6"],
    3: ["spotify:playlist:demo1", "soundcloud:djsets/sets/trance", "youtube:PLdemo2"],
}
URLS = {k: u for k, _, _, u, _ in LISTS}
for uid, keys in FOLLOWS.items():
    added = users[uid - 1][2]
    for pos, key in enumerate(keys):
        likes = int(key.startswith("spotify:likes") or key.endswith("/likes"))
        con.execute(
            "INSERT INTO sources (user_id, key, service, likes, url, title, playlist, enabled, position, added) "
            "VALUES (?, ?, ?, ?, ?, NULL, 1, 1, ?, ?)",
            (uid, key, key.split(":")[0], likes, URLS[key], pos, added),
        )

# ------------------------------------------------------------------ songs and files
songs: dict[str, dict] = {}
files: dict[str, dict] = {}
by_name: dict[tuple, str] = {}  # (service, artist, title) -> key
counter = {"spotify": 0, "soundcloud": 0, "youtube": 0}
list_songs: dict[str, list[str]] = {k: [] for k, *_ in LISTS}

QUAL_W = {  # tier weights per service
    "spotify": [("lossless", 90.5), ("fake", 1.5), ("lossy-high", 3.5), ("lossy-mid", 1.8), ("lossy-low", 2.7)],
    "soundcloud": [("lossless", 60), ("fake", 1.2), ("lossy-high", 16), ("lossy-mid", 9), ("lossy-low", 13.8)],
    "youtube": [("lossless", 62), ("fake", 1), ("lossy-high", 7), ("lossy-mid", 26), ("lossy-low", 4)],
}


def pick_tier(service: str) -> str:
    tiers, weights = zip(*QUAL_W[service], strict=True)
    return R.choices(tiers, weights)[0]


def fmt_for(tier: str, service: str) -> tuple[str, int]:
    if tier in ("lossless", "fake"):
        return "flac", R.randint(600, 940)
    if tier == "lossy-high":
        return R.choice([("mp3", 320), ("mp3", 320), ("m4a", 256), ("opus", 256)])
    if tier == "lossy-mid":
        return R.choice([("mp3", 192), ("opus", 160), ("mp3", 224), ("m4a", 160)])
    return R.choice([("mp3", 128), ("opus", 128), ("mp3", 128), ("opus", 96), ("mp3", 132)])


def make_file(
    artist: str,
    title: str,
    seconds: float,
    tier: str,
    ext: str | None = None,
    kbps: int | None = None,
    service: str = "spotify",
    path: str | None = None,
) -> str:
    e, k = fmt_for(tier, service)
    ext, kbps = ext or e, kbps or k
    folder = clean_name(artist)
    path = path or f"{folder}/{folder} - {clean_name(title)}.{ext}"
    if path in files:
        return path
    dur = max(seconds + R.uniform(-0.6, 0.6), 1)
    size = int(kbps * 1000 / 8 * dur) + R.randint(20_000, 900_000)
    mtime = int(came_in(grown).timestamp())
    files[path] = {"path": path, "size": size, "mtime": mtime, "duration": round(dur, 3), "kbps": kbps, "quality": tier}
    return path


def add_song(
    service: str,
    artist: str,
    title: str,
    seconds: float,
    extra=(),
    tier: str | None = "auto",
    url: str | None = None,
    file: str | None = None,
    **more,
) -> str:
    name = (service, artist.lower(), title.lower())
    if name in by_name:
        return by_name[name]
    counter[service] += 1
    key = f"{service}:demo{counter[service]}"
    if service == "soundcloud" and not url:
        slug = clean_name(title).lower().replace(" ", "-")
        url = f"https://soundcloud.com/{clean_name(artist).lower().replace(' ', '')}/{slug}"
    if service == "youtube" and not url:
        url = f"https://www.youtube.com/watch?v=demo{counter[service]:05d}"
    s = {
        "key": key,
        "service": service,
        "artist": artist,
        "title": title,
        "length": float(seconds),
        "artists": json.dumps([artist, *extra]) if service == "spotify" else None,
        "url": url,
        "file": None,
        "unavailable": None,
        "link": None,
        "close_match": 0,
        "archived": 0,
    }
    if service == "soundcloud":
        s["archived"] = 1
    if file:
        s["file"] = file
    elif tier is not None:
        t = pick_tier(service) if tier == "auto" else tier
        s["file"] = make_file(artist, title, seconds, t, service=service, **more)
    if service == "soundcloud" and s["file"]:
        s["stem"] = s["file"].rsplit(".", 1)[0]
    songs[key] = s
    by_name[name] = key
    return key


used_names: set[tuple] = set()


def filler(kind: str) -> tuple[str, str, int, list]:
    while True:
        if kind == "pop":
            a, t = R.choice(POP_ARTISTS), R.choice(POP_TITLES)
            suf = R.choice(SUFFIXES).format(f=R.choice(FEATS))
            n, extra = R.randint(165, 275), []
        elif kind == "trance":
            a, t = R.choice(TRANCE_ARTISTS), f"{R.choice(WORDS_A)} {R.choice(WORDS_B)}"
            suf, n, extra = R.choice(TRANCE_SUFFIX), R.randint(300, 520), []
        elif kind == "techno":
            a, t = R.choice(TECHNO_ARTISTS), R.choice([f"{R.choice(WORDS_A)} {R.choice(WORDS_B)}", R.choice(WORDS_B)])
            suf, n, extra = R.choice(TECHNO_SUFFIX), R.randint(320, 480), []
        else:  # chill / night
            a, t = R.choice(POP_ARTISTS + TECHNO_ARTISTS[:20]), f"{R.choice(WORDS_A)} {R.choice(WORDS_B)}"
            suf, n, extra = "", R.randint(200, 360), []
        title = t + suf
        if (a, title) not in used_names:
            used_names.add((a, title))
            return a, title, n, extra


def put(list_key: str, key: str) -> None:
    if key not in list_songs[list_key]:
        list_songs[list_key].append(key)


# ---- the songs that show: Trance Classics on top, every quality in the first rows
TRANCE_TIERS = {
    2: ("lossy-high", "mp3", 320),
    5: ("lossy-mid", "mp3", 192),
    9: ("fake", "flac", 912),
    11: ("lossy-low", "mp3", 128),
    14: ("lossy-high", "m4a", 256),
    21: ("lossy-low", "opus", 128),
    27: ("lossy-high", "mp3", 320),
    33: ("fake", "flac", 870),
}
for i, (a, t, n, extra) in enumerate(TRANCE):
    tier, ext, kbps = TRANCE_TIERS.get(i, ("lossless", None, None))
    put("spotify:playlist:demo1", add_song("spotify", a, t, n, extra, tier=tier, ext=ext, kbps=kbps))

# ---- missing songs (Spotify), and the review items
MISSING = {}  # name -> key


def missing_song(list_keys, artist, title, seconds, extra=(), service="spotify", **kw):
    key = add_song(service, artist, title, seconds, extra, tier=None, **kw)
    MISSING[f"{artist} - {title}"] = key
    for lk, pos in list_keys:
        lst = list_songs[lk]
        if key not in lst:
            lst.insert(min(pos, len(lst)), key) if pos is not None else lst.append(key)
    return key


hits_rows = [*STORY_HITS, *HITS, *EXTRA_HITS]
R.shuffle(hits_rows)
hits_rows.insert(0, hits_rows.pop(next(i for i, h in enumerate(hits_rows) if h[0] == "Icona Pop")))  # (on top)
MISSING_HITS = {
    ("Avicii", "Levels - Radio Edit"),
    ("Kesha", "TiK ToK"),
    ("Stromae", "Papaoutai"),
    ("Pitbull", "Timber"),
    ("Spice Girls", "Wannabe"),
    ("Lady Gaga", "Poker Face"),
    ("The Weeknd", "Blinding Lights"),
}
FEAT = {"Pitbull - Timber": ["Ke$ha"], "Icona Pop - I Love It (feat. Charli XCX)": ["Charli xcx"]}
special_tiers = {
    ("Icona Pop", "I Love It (feat. Charli XCX)"): ("lossless", "flac", 1012),
    ("Katy Perry", "Dark Horse"): ("lossless", "flac", 987),
    ("Calvin Harris", "Feel So Close - Radio Edit"): ("lossy-high", "mp3", 320),
    ("Imagine Dragons", "Thunder"): ("lossy-mid", "mp3", 192),
    ("Tove Lo", "Habits (Stay High) - Hippie Sabotage Remix"): ("lossy-high", "mp3", 320),
    ("Mike Posner", "I Took A Pill In Ibiza - Seeb Remix"): ("lossless", "flac", 1004),
}
for a, t, n in hits_rows:
    if (a, t) in MISSING_HITS:
        continue
    if a == "Galantis" and t.startswith("Runaway (U & I)"):
        continue  # a close match, below
    if a == "Avicii" and t.startswith("Hey Brother"):
        continue
    if (a, t) == ("PSY", ""):
        continue
    tier, ext, kbps = special_tiers.get((a, t), ("auto", None, None))
    key = add_song("spotify", a, t, n, FEAT.get(f"{a} - {t}", []), tier=tier, ext=ext, kbps=kbps)
    put("spotify:playlist:demo2", key)

# the close matches: another version taken in review
for a, t, n, other, other_n in [
    ("Galantis", "Runaway (U & I)", 227, "Runaway (U & I) (Extended Mix)", 352),
    ("Avicii", "Hey Brother", 255, "Hey Brother (Extended Version)", 390),
]:
    path = make_file(a, other, other_n, "lossless", "flac", R.randint(900, 1100))
    key = add_song("spotify", a, t, n, [], file=path)
    songs[key]["close_match"] = 1
    songs[key]["link"] = json.dumps([a, other, other_n])
    put("spotify:playlist:demo2", key)

# Hits songs that are missing, placed in the list order
for a, t, n in hits_rows:
    if (a, t) in MISSING_HITS:
        pos = next((i for i, h in enumerate(hits_rows) if (h[0], h[1]) == (a, t)), None)
        missing_song([("spotify:playlist:demo2", pos)], a, t, n, FEAT.get(f"{a} - {t}", []))

# Techno: real ones, a few missing
for a, t, n, extra in TECHNO:
    put("spotify:playlist:demo3", add_song("spotify", a, t, n, extra))
for a, t, n, extra, pos in [
    ("Adam Beyer", "Your Mind", 398, ["Bart Skils"], 7),
    ("Boris Brejcha", "Gravity", 389, [], 12),
    ("Charlotte de Witte", "Sgadi Li Mi", 346, [], 3),
    ("Kölsch", "Grey", 401, [], 18),
    ("Paul Kalkbrenner", "Sky and Sand", 245, ["Fritz Kalkbrenner"], 22),
    ("Stephan Bodzin", "Powers of Ten", 412, [], 9),
]:
    missing_song([("spotify:playlist:demo3", pos)], a, t, n, extra)

# NCS (YouTube list) and the two NCS review songs (Spotify, in Liked Songs)
for a, t, n in NCS:
    put("youtube:PLdemo1", add_song("youtube", a, t, n))
FADE = add_song("spotify", "Alan Walker", "Fade", 264, tier=None)
songs[FADE]["file"] = "Alan Walker/Alan Walker - Fade.flac"
files[songs[FADE]["file"]] = {
    "path": songs[FADE]["file"],
    "size": 29_388_211,
    "mtime": unix_ago(4),
    "duration": 263.2,
    "kbps": 893,
    "quality": "lossless",
}
JANJI = missing_song([], "Janji", "Heroes Tonight (feat. Johnning)", 208, ["Johnning"])

# SoundCloud likes: the shown ones
FISHER = missing_song([("soundcloud:rick/likes", 14)], "Fisher", "Losing It", 251, service="soundcloud")
songs[FISHER]["unavailable"] = NOT_ON_SOUNDCLOUD
songs[FISHER]["url"] = "https://soundcloud.com/fishermusic/losing-it"
PURPLE = add_song("soundcloud", "Boris Brejcha", "Purple Noise", 439, tier="lossy-low", ext="opus", kbps=128)
LENS = add_song("soundcloud", "Amelie Lens", "In My Mind", 281, tier=None)
songs[LENS]["file"] = make_file("Amelie Lens", "In My Mind", 281, "lossy-high", "m4a", 256)
songs[LENS]["stem"] = songs[LENS]["file"].rsplit(".", 1)[0]
KYGO_SC = add_song("soundcloud", "Kygo", "Firestone ft. Conrad Sewell", 272, tier=None)
kygo_sp = by_name[("spotify", "kygo", "firestone")]
songs[KYGO_SC]["file"] = songs[kygo_sp]["file"]
songs[KYGO_SC]["link"] = json.dumps(["Kygo", "Firestone", 272])
AMELIE_HYP = missing_song([("spotify:playlist:demo3", None)], "Amelie Lens", "Hypnotized", 382)

# ---- Liked Songs: newest first; the review and missing songs among them
liked = list_songs["spotify:likes:1"]
pool_spotify = [k for k in songs if k.startswith("spotify:") and songs[k]["file"] and k != FADE]
liked += [FADE]
while len(liked) < 2500:
    r = R.random()
    if r < 0.18:
        k = R.choice(pool_spotify)  # also on a playlist
    else:
        kind = R.choices(["pop", "trance", "techno", "chill"], [55, 15, 18, 12])[0]
        a, t, n, extra = filler(kind)
        k = add_song("spotify", a, t, n, extra)
    if k not in liked:
        liked.append(k)


def insert(lst, pos, key):
    if key in lst:
        lst.remove(key)
    lst.insert(pos, key)


insert(liked, 1, by_name[("spotify", "icona pop", "i love it (feat. charli xcx)")])
insert(liked, 211, JANJI)
for name in (
    "Avicii - Levels - Radio Edit",
    "Kesha - TiK ToK",
    "The Weeknd - Blinding Lights",
    "Boris Brejcha - Gravity",
):
    insert(liked, R.randint(300, 2300), MISSING[name])

# Trance Classics fillers, Techno fillers, playlists
for _ in range(312 - len(list_songs["spotify:playlist:demo1"])):
    a, t, n, extra = filler("trance")
    put("spotify:playlist:demo1", add_song("spotify", a, t, n, extra))
for _ in range(124 - len(list_songs["spotify:playlist:demo3"])):
    a, t, n, extra = filler("techno")
    put("spotify:playlist:demo3", add_song("spotify", a, t, n, extra))
for lk, size, kinds in [
    ("spotify:playlist:demo4", 64, ["techno", "chill", "trance"]),
    ("spotify:playlist:demo5", 82, ["pop", "techno"]),
    ("spotify:playlist:demo6", 96, ["chill", "pop"]),
]:
    while len(list_songs[lk]) < size:
        if R.random() < 0.3:
            k = R.choice([x for x in liked if songs[x]["file"]])
        else:
            a, t, n, extra = filler(R.choice(kinds))
            k = add_song("spotify", a, t, n, extra)
        put(lk, k)

# SoundCloud likes: some the same recordings as Spotify songs (one file), the rest their own
sc = list_songs["soundcloud:rick/likes"]
for k in (PURPLE, LENS, KYGO_SC):
    insert(sc, R.randint(0, 12), k)
spotify_with_files = [k for k in liked if songs[k]["file"]]
while len(sc) < 1400:
    if R.random() < 0.16:
        src = songs[R.choice(spotify_with_files)]
        k = add_song("soundcloud", src["artist"], src["title"], src["length"], file=src["file"])
    else:
        kind = R.choices(["techno", "trance", "pop", "chill"], [45, 20, 20, 15])[0]
        a, t, n, _ = filler(kind)
        k = add_song("soundcloud", a, t, n)
    if k not in sc:
        sc.append(k)
insert(sc, 14, FISHER)

# Trance Sets: long DJ sets, lossy
for _ in range(38):
    a = R.choice(TRANCE_ARTISTS)
    t = (
        f"{R.choice(['Live at', 'Recorded at', 'Sunrise Set at', 'Closing Set at'])} "
        f"{R.choice(['Transmission', 'ASOT', 'Dreamstate', 'Luminosity', 'Ministry of Sound', 'Tomorrowland'])} "
        f"{R.choice(range(2012, 2026))}"
    )
    n = R.randint(3000, 5400)
    t1 = R.choices(["lossy-high", "lossy-mid", "lossy-low", "lossless"], [45, 25, 20, 10])[0]
    put("soundcloud:djsets/sets/trance", add_song("soundcloud", a, t, n, tier=t1))

# NCS Releases fillers (YouTube)
while len(list_songs["youtube:PLdemo1"]) < 146:
    a, t, n, _ = filler(R.choice(["pop", "chill"]))
    put("youtube:PLdemo1", add_song("youtube", a, t, n))

# sam's likes (a third shared with rick's) and jo's workout mix
while len(list_songs["spotify:likes:2"]) < 300:
    if R.random() < 0.3:
        k = R.choice(liked)
    else:
        a, t, n, extra = filler(R.choice(["pop", "chill"]))
        k = add_song("spotify", a, t, n, extra)
    put("spotify:likes:2", k)
for extra_song in [("Martin Garrix", "Animals - Radio Edit", 183)]:
    put("spotify:likes:2", add_song("spotify", *extra_song))
while len(list_songs["youtube:PLdemo2"]) < 90:
    a, t, n, _ = filler(R.choice(["pop", "techno"]))
    put("youtube:PLdemo2", add_song("youtube", a, t, n))

# songs that left every list (their files stay)
for _ in range(40):
    a, t, n, extra = filler(R.choice(["pop", "techno", "trance", "chill"]))
    add_song("spotify", a, t, n, extra)

# fake FLACs: the spectrum check's verdicts
for f in files.values():
    if f["quality"] == "fake":
        write(
            "lossy_sourced",
            {
                "stem": f["path"].rsplit(".", 1)[0],
                "source": R.choice(["~128 kbps", "~192 kbps", "~320 kbps"]),
                "detected": ago(R.randint(600, 18000)),
            },
        )

# ------------------------------------------------------------------ write songs, files, lists
for s in songs.values():
    con.execute(
        "INSERT INTO songs (key, service, artist, title, length, unavailable, stem, file, artists, link, close_match, "
        "url, archived) VALUES (:key, :service, :artist, :title, :length, :unavailable, :stem, :file, :artists, "
        ":link, :close_match, :url, :archived)",
        {"stem": None, **s},
    )
ADDED: dict[tuple[str, str], str] = {}
for lk, keys in list_songs.items():
    times = sorted(came_in(listed).isoformat(timespec="seconds") for _ in keys)
    likes = lk.startswith("spotify:likes") or lk.endswith("/likes")
    for k, when in zip(keys, times[::-1] if likes else times, strict=True):
        ADDED[(lk, k)] = when
for lk, keys in list_songs.items():
    for pos, k in enumerate(keys):
        added = ADDED[(lk, k)]
        write("list_songs", {"list_key": lk, "position": pos, "song_key": k, "added": added})
        write(
            "list_history",
            {"list_key": lk, "song_key": k, "first_seen": added[:10], "last_seen": TODAY},
            "INSERT OR IGNORE",
        )
# a few that left their lists
for k in R.sample([k for k in songs if not any(k in v for v in list_songs.values())], 20):
    write(
        "list_history",
        {
            "list_key": "spotify:likes:1",
            "song_key": k,
            "first_seen": (START - dt.timedelta(days=3)).date().isoformat(),
            "last_seen": (START + dt.timedelta(days=R.randint(1, 12))).date().isoformat(),
        },
    )

# ------------------------------------------------------------------ attempts (Missing page)
M = MISSING
GREY = [
    "Avicii - Levels - Radio Edit",
    "Kesha - TiK ToK",
    "Paul Kalkbrenner - Sky and Sand",
    "The Weeknd - Blinding Lights",
]
for name in GREY:
    con.execute("UPDATE songs SET unavailable = ? WHERE key = ?", (GREYED_OUT, M[name]))


def attempt(name, tries, last_min, result=None, fallback=None, fallback_min=None):
    write(
        "attempts",
        {
            "song_key": M[name],
            "tries": tries,
            "last_try": unix_ago(last_min) if tries else None,
            "last_fallback": unix_ago(fallback_min) if fallback_min else None,
            "result": json.dumps(result) if result else None,
            "fallback_result": json.dumps(fallback) if fallback else None,
        },
    )


def rep(stage, results, fits, rejected, tried=()):
    return {"stage": stage, "results": results, "fits": fits, "rejected": rejected, "tried": [list(t) for t in tried]}


attempt("Adam Beyer - Your Mind", 1, 138, rep(0, 3, 0, {"another length": 2, "artist missing": 1}))
attempt(
    "Avicii - Levels - Radio Edit",
    13,
    60 * 24 * 4.2,
    rep(2, 2, 0, {"another length": 2}),
    {
        "youtube": {"results": 11, "fits": 0, "rejected": {"another length": 7, "lacks the version": 4}, "tried": []},
        "soundcloud": {"results": 6, "fits": 0, "rejected": {"another length": 4, "another song": 2}, "tried": []},
    },
    60 * 24 * 4.1,
)
attempt(
    "Boris Brejcha - Gravity",
    3,
    60 * 19,
    rep(1, 7, 1, {"another length": 4, "lacks the version": 2}, [("Boris Brejcha - Gravity.flac", "mismatch", "")]),
    {
        "youtube": {
            "results": 14,
            "fits": 2,
            "rejected": {"another length": 6, "another song": 4, "lacks the version": 2},
            "tried": [
                [
                    "Boris Brejcha - Gravity (Live at Cercle)",
                    "wrong-song",
                    "file name 'Boris Brejcha - Gravity (Live at Cercle)' names another version than 'Gravity'",
                ],
                ["Boris Brejcha - Gravity [Fckng Serious]", "mismatch", ""],
            ],
        },
        "soundcloud": {
            "results": 5,
            "fits": 0,
            "rejected": {"another length": 3, "rejected before": 1, "another song": 1},
            "tried": [],
        },
    },
    60 * 18.5,
)
attempt(
    "Charlotte de Witte - Sgadi Li Mi",
    2,
    60 * 21,
    rep(
        1,
        3,
        2,
        {"another length": 1},
        [
            ("Charlotte de Witte - Sgadi Li Mi (Original Mix).flac", "failed", "no progress (queued at the peer)"),
            ("Sgadi Li Mi.flac", "failed", "no progress (queued at the peer)"),
        ],
    ),
)
attempt(
    "Fisher - Losing It",
    0,
    0,
    None,
    {
        "youtube": {"results": 9, "fits": 0, "rejected": {"another length": 5, "another song": 4}, "tried": []},
        "soundcloud": {
            "results": 6,
            "fits": 1,
            "rejected": {"another length": 3, "artist missing": 2},
            "tried": [["FISHER - Losing It", "download failed", "DRM-protected"]],
        },
    },
    60 * 7.2,
)
attempt(
    "Janji - Heroes Tonight (feat. Johnning)",
    3,
    19,
    rep(
        1,
        4,
        2,
        {"another length": 1, "lacks the version": 1},
        [
            ("Janji - Heroes Tonight (feat. Johnning).mp3", "mismatch", ""),
            ("Heroes Tonight.flac", "failed", "no progress (queued at the peer)"),
        ],
    ),
)
attempt("Kesha - TiK ToK", 6, 60 * 24 * 2.3, rep(2, 1, 0, {"another length": 1}))
attempt("Kölsch - Grey", 2, 60 * 20.5, rep(1, 2, 0, {"lacks the version": 2}))
attempt("Lady Gaga - Poker Face", 1, 60 * 3.1, rep(0, 0, 0, {}))
attempt("Paul Kalkbrenner - Sky and Sand", 9, 60 * 24 * 3.9, rep(2, 4, 0, {"another length": 3, "another song": 1}))
attempt(
    "Pitbull - Timber",
    4,
    60 * 22,
    rep(2, 5, 0, {"another length": 3, "artist in another name": 2}),
    {
        "youtube": {
            "results": 12,
            "fits": 1,
            "rejected": {"another length": 8, "another song": 3},
            "tried": [["Pitbull - Timber (Lyrics) ft. Ke$ha", "mismatch", ""]],
        },
        "soundcloud": {"results": 3, "fits": 0, "rejected": {"another song": 3}, "tried": []},
    },
    60 * 21,
)
attempt("Spice Girls - Wannabe", 2, 60 * 20, rep(1, 1, 0, {"another length": 1}))
attempt(
    "Stephan Bodzin - Powers of Ten",
    5,
    60 * 23,
    rep(
        2,
        2,
        1,
        {"another length": 1},
        [("Stephan Bodzin - Powers Of Ten (Original Mix).flac", "bad file", "decoding failed at 2:11")],
    ),
)
attempt("Stromae - Papaoutai", 1, 60 * 2.6, rep(0, 2, 0, {"artist missing": 2}))
attempt("The Weeknd - Blinding Lights", 13, 60 * 24 * 4.4, rep(2, 3, 0, {"another length": 3}))

# ------------------------------------------------------------------ events
events: list[dict] = []


def ev(minutes, action, path, **kw):
    e = {"ts": ago(minutes), "action": action, "path": path, **kw}
    if "ext" not in e and path and "." in path.rsplit("/", 1)[-1]:
        e["ext"] = path.rsplit(".", 1)[-1].lower()
    events.append(e)
    return e


def song_of(name_artist, title, service="spotify"):
    return by_name[(service, name_artist.lower(), title.lower())]


def lib(key):
    return songs[key]["file"]


def size_of(path):
    return files[path]["size"]


def kept_path(artist, title, source, ext, day=TODAY):
    return f"/music/inbox/review/{day}/{clean_name(artist)} - {clean_name(title)} [{source}].{ext}"


def replaced_path(path, day=TODAY):
    return f"/music/inbox/replaced/{day}/{path}"


WAS: dict[str, tuple[str, int, int]] = {}  # an upgraded file: (when, kbps and bytes before)


def upgrade(minutes, key, old_ext, old_kbps, old_bytes, source="soulseek", hand=False, **kw):
    s = songs[key]
    new = lib(key)
    WAS[new] = (ago(minutes), old_kbps, old_bytes)
    old = new.rsplit(".", 1)[0] + "." + old_ext
    why = "replaced by hand" if hand else "replaced by genuine lossless"
    ev(
        minutes,
        "retired",
        replaced_path(old),
        ext=old_ext,
        bytes=old_bytes,
        kbps=old_kbps,
        seconds=round(s["length"]),
        reason=f"{why} (was {old})",
    )
    base = (
        {
            "file_name": f"{s['artist']} - {s['title']}.flac",
            "found": s["title"],
            "fake": 0,
            "tries": 0,
            "audio": f"audio of the release (0.9{R.randint(1, 8)})",
        }
        if not hand
        else {}
    )
    ev(
        minutes,
        "upgrade",
        new,
        bytes=size_of(new),
        kbps=files[new]["kbps"],
        seconds=round(files[new]["duration"]),
        source="manual" if hand else source,
        artist=s["artist"],
        title=s["title"],
        song=key,
        **{**base, **kw},
    )


def new(minutes, key, source="soulseek", matched="exact", **kw):
    s = songs[key]
    p = lib(key)
    f = files[p]
    extra = (
        {
            "file_name": f"{s['artist']} - {s['title']}.{p.rsplit('.', 1)[1]}",
            "found": s["title"],
            "fake": int(f["quality"] == "fake"),
            "tries": kw.pop("tries", 0),
        }
        if source == "soulseek"
        else {}
    )
    ev(
        minutes,
        "new",
        p,
        bytes=f["size"],
        kbps=f["kbps"],
        seconds=round(f["duration"]),
        source=source,
        artist=s["artist"],
        title=s["title"],
        song=key,
        matched=matched,
        **{**extra, **kw},
    )


# ---- older history: a steady stream of downloads over the last days (beyond the first screen)
lib_keys = [
    k for k in liked + list_songs["spotify:playlist:demo1"] + list_songs["spotify:playlist:demo3"] if songs[k]["file"]
]
lib_keys = list(dict.fromkeys(lib_keys))
R.shuffle(lib_keys)
old_minutes = sorted((R.uniform(9.6 * 60, 60 * 24 * 12) for _ in range(260)), reverse=True)
for i, m in enumerate(old_minutes):
    k = lib_keys[i]
    f = files[lib(k)]
    r = R.random()
    if r < 0.62:
        new(m, k, source=R.choices(["soulseek", "youtube", "soundcloud-search"], [88, 9, 3])[0])
    elif r < 0.78 and f["quality"] == "lossless":
        old_kbps = R.choice([128, 192, 256, 320])
        upgrade(m, k, R.choice(["mp3", "opus", "m4a"]), old_kbps, int(old_kbps * 125 * f["duration"]))
    elif r < 0.9:
        s = songs[k]
        ev(
            m,
            "duplicate",
            lib(k),
            bytes=f["size"],
            kbps=f["kbps"],
            seconds=round(f["duration"]),
            source="soulseek",
            artist=s["artist"],
            title=s["title"],
            song=k,
            matched="exact",
            found=s["title"],
            file_name=f"{s['artist']} - {s['title']}.flac",
            fake=int(R.random() < 0.3),
            tries=0,
        )
    else:
        s = songs[k]
        ev(
            m,
            "wrong-song",
            kept_path(s["artist"], s["title"], "soulseek", "flac", (NOW - dt.timedelta(minutes=m)).date().isoformat()),
            bytes=R.randint(20_000_000, 45_000_000),
            kbps=R.randint(800, 1100),
            seconds=round(s["length"]) + R.randint(-2, 2),
            source="soulseek",
            artist=s["artist"],
            title=s["title"],
            song=k,
            reason=f"title '{s['title']}': no reading of tag '{s['title']} (Live)' or file name '{s['artist']} - "
            f"{s['title']} (Live).flac' has the same core title and version",
            found=f"{s['title']} (Live)",
            file_name=f"{s['artist']} - {s['title']} (Live).flac",
            fake=0,
            tries=0,
        )

# the review history behind what shows now
blame = song_of("Calvin Harris", "Blame (feat. John Newman)")
blame_ev = ev(
    60 * 46,
    "new",
    lib(blame),
    bytes=size_of(lib(blame)),
    kbps=files[lib(blame)]["kbps"],
    seconds=round(files[lib(blame)]["duration"]),
    source="soulseek",
    artist="Calvin Harris",
    title="Blame (feat. John Newman)",
    song=blame,
    matched="probable",
    reason="probable: 'Blame (feat. John Newman) [Radio Edit]' has the core title, the same version words, length within 3 s",
    found="Blame (feat. John Newman) [Radio Edit]",
    file_name="Calvin Harris - Blame (feat. John Newman) [Radio Edit].flac",
    fake=0,
    tries=1,
    audio="audio unclear (0.64)",
)
stole = song_of("Kygo", "Stole the Show")
stole_ev = ev(
    60 * 30,
    "mismatch",
    kept_path("Kygo", "Stole the Show", "soulseek", "flac", (NOW - dt.timedelta(hours=30)).date().isoformat()),
    bytes=36_220_114,
    kbps=968,
    seconds=299,
    source="soulseek",
    artist="Kygo",
    title="Stole the Show",
    song=stole,
    found="Stole the Show",
    file_name="Kygo - Stole the Show.flac",
    fake=0,
    tries=0,
    wanted_seconds=223,
    audio="audio unclear (0.52)",
)
shm = song_of("Galantis", "Runaway (U & I)")
shm_old = "Galantis/Galantis - Runaway (U & I).flac"
shm_ev = ev(
    60 * 27,
    "new",
    shm_old,
    bytes=size_of(lib(shm)),
    kbps=files[lib(shm)]["kbps"],
    seconds=352,
    source="soulseek",
    artist="Galantis",
    title="Runaway (U & I)",
    song=shm,
    matched="probable",
    reason="probable: 'Runaway (U & I)' has the core title, the same version words, length within 3 s",
    found="Runaway (U & I)",
    file_name="Galantis - Runaway (U & I).flac",
    fake=0,
    tries=2,
    audio="audio unclear (0.71)",
)
sigala = song_of("Sigala", "Easy Love")
sigala_kept = ev(
    60 * 31,
    "mismatch",
    kept_path("Sigala", "Easy Love", "youtube", "opus", (NOW - dt.timedelta(hours=31)).date().isoformat()),
    bytes=4_710_332,
    kbps=160,
    seconds=236,
    source="youtube",
    artist="Sigala",
    title="Easy Love",
    song=sigala,
    found="Sigala - Easy Love (Official Video)",
    file_name="Sigala - Easy Love (Official Video)",
    url="https://www.youtube.com/watch?v=demoSigala1",
    wanted_seconds=227,
)
posner = song_of("Mike Posner", "I Took A Pill In Ibiza - Seeb Remix")
posner_retired = ev(
    60 * 24 * 3,
    "retired",
    replaced_path(lib(posner), (NOW - dt.timedelta(days=3)).date().isoformat()),
    bytes=size_of(lib(posner)),
    kbps=files[lib(posner)]["kbps"],
    seconds=198,
    reason=f"no match in review (was {lib(posner)})",
)
posner_ev = ev(
    60 * 24 * 3.2,
    "new",
    lib(posner),
    bytes=size_of(lib(posner)),
    kbps=files[lib(posner)]["kbps"],
    seconds=198,
    source="soulseek",
    artist="Mike Posner",
    title="I Took A Pill In Ibiza - Seeb Remix",
    song=posner,
    matched="probable",
    reason="probable: 'I Took A Pill In Ibiza (Seeb Remix)' has the core title, the same version words, length within 3 s",
    found="I Took A Pill In Ibiza (Seeb Remix)",
    file_name="Mike Posner - I Took A Pill In Ibiza (Seeb Remix).flac",
    fake=0,
    tries=0,
)
gravity = M["Boris Brejcha - Gravity"]
gravity_kept = ev(
    60 * 19.2,
    "mismatch",
    kept_path("Boris Brejcha", "Gravity", "soulseek", "flac", (NOW - dt.timedelta(hours=19.2)).date().isoformat()),
    bytes=71_404_220,
    kbps=1043,
    seconds=488,
    source="soulseek",
    artist="Boris Brejcha",
    title="Gravity",
    song=gravity,
    found="Gravity",
    file_name="Boris Brejcha - Gravity.flac",
    fake=0,
    tries=2,
    wanted_seconds=389,
    audio="audio unclear (0.47)",
)
gravity_yt = ev(
    60 * 18.6,
    "wrong-song",
    "/music/inbox/fallback/spotify-demo/Boris Brejcha - Gravity (Live at Cercle).opus",
    bytes=8_810_002,
    kbps=129,
    seconds=512,
    source="youtube",
    artist="Boris Brejcha",
    title="Gravity",
    song=gravity,
    reason="file name 'Boris Brejcha - Gravity (Live at Cercle)' names another version than 'Gravity'",
    found="Boris Brejcha - Gravity (Live at Cercle)",
    file_name="Boris Brejcha - Gravity (Live at Cercle)",
    url="https://www.youtube.com/watch?v=demoGravity1",
    fake=0,
    tries=3,
)
gravity_old = ev(
    60 * 24 * 4.5,
    "wrong-song",
    kept_path("Boris Brejcha", "Gravity", "soulseek", "flac", (NOW - dt.timedelta(days=4.5)).date().isoformat()),
    bytes=58_112_903,
    kbps=1012,
    seconds=391,
    source="soulseek",
    artist="Boris Brejcha",
    title="Gravity",
    song=gravity,
    reason="probable: 'Gravity (Original Mix)' has the core title, the same version words, length within 3 s, "
    "but the audio differs from the release (0.16)",
    found="Gravity (Original Mix)",
    file_name="Boris Brejcha - Gravity (Original Mix).flac",
    fake=0,
    tries=0,
    audio="audio differs from the release (0.16)",
)
pitbull = M["Pitbull - Timber"]
ev(
    60 * 21,
    "mismatch",
    kept_path("Pitbull", "Timber", "youtube", "opus", (NOW - dt.timedelta(hours=21)).date().isoformat()),
    bytes=5_102_377,
    kbps=131,
    seconds=284,
    source="youtube",
    artist="Pitbull",
    title="Timber",
    song=pitbull,
    found="Pitbull - Timber (Lyrics) ft. Ke$ha",
    file_name="Pitbull - Timber (Lyrics) ft. Ke$ha",
    url="https://www.youtube.com/watch?v=demoTimber1",
    wanted_seconds=204,
)

# ---- the first screen of Activity (newest first when read from the bottom up here)
ret = ev(60 * 5.5, "retagged", "/music/tracks", ext="", bytes=0, reason="186 files: 120 album, 66 date")
ev(
    60 * 2.6,
    "retired",
    replaced_path("Martin Garrix/Martin Garrix - Animals (Original Mix).flac"),
    ext="flac",
    bytes=48_377_015,
    kbps=1046,
    seconds=303,
    reason="close match removed by hand (was Martin Garrix/Martin Garrix - Animals (Original Mix).flac)",
)
habits = song_of("Tove Lo", "Habits (Stay High) - Hippie Sabotage Remix")
habits_ev = ev(
    60 * 7.4,
    "wrong-song",
    kept_path("Tove Lo", "Habits (Stay High) - Hippie Sabotage Remix", "soulseek", "flac"),
    bytes=33_290_118,
    kbps=1028,
    seconds=258,
    source="soulseek",
    artist="Tove Lo",
    title="Habits (Stay High) - Hippie Sabotage Remix",
    song=habits,
    reason="probable: 'Habits (Stay High) (Hippie Sabotage Remix)' has the core title, the same version words, "
    "length within 3 s (not filed: soulseek probable matches need a review)",
    found="Habits (Stay High) (Hippie Sabotage Remix)",
    file_name="Tove Lo - Habits (Stay High) (Hippie Sabotage Remix).flac",
    fake=0,
    tries=0,
    audio="audio unclear (0.58)",
)
ev(
    60 * 6.6,
    "restored",
    lib(posner),
    bytes=size_of(lib(posner)),
    kbps=files[lib(posner)]["kbps"],
    seconds=198,
    song=posner,
    artist="Mike Posner",
    title="I Took A Pill In Ibiza - Seeb Remix",
    reason="No match taken back by an admin",
)
ev(
    60 * 5.9,
    "new",
    lib(sigala),
    bytes=size_of(lib(sigala)),
    kbps=files[lib(sigala)]["kbps"],
    seconds=round(files[lib(sigala)]["duration"]),
    source="youtube",
    artist="Sigala",
    title="Easy Love",
    song=sigala,
    matched="review",
    found="Sigala - Easy Love (Official Video)",
    file_name="Sigala - Easy Love (Official Video)",
    url="https://www.youtube.com/watch?v=demoSigala1",
)
# (Sigala's copy is the YouTube download: 160 kbps)
files[lib(sigala)].update(kbps=160, quality="lossy-mid", size=4_610_220)
old_sigala = lib(sigala)
new_sigala = old_sigala.rsplit(".", 1)[0] + ".opus"
files[new_sigala] = {**files.pop(old_sigala), "path": new_sigala}
songs[sigala]["file"] = new_sigala
events[-1].update(path=new_sigala, ext="opus", kbps=160, bytes=4_610_220)
feel = song_of("Calvin Harris", "Feel So Close - Radio Edit")
ev(
    60 * 5.2,
    "wrong-song",
    kept_path("Calvin Harris", "Feel So Close - Radio Edit", "soulseek", "flac"),
    bytes=27_114_590,
    kbps=1049,
    seconds=205,
    source="soulseek",
    artist="Calvin Harris",
    title="Feel So Close - Radio Edit",
    song=feel,
    reason="probable: 'Feel So Close (Radio Edit)' has the core title, the same version words, length within 3 s, "
    "but the audio differs from the release (0.18)",
    found="Feel So Close (Radio Edit)",
    file_name="Calvin Harris - Feel So Close (Radio Edit).flac",
    fake=0,
    tries=0,
    audio="audio differs from the release (0.18)",
)
shm_new = lib(shm)
ev(
    60 * 4.6,
    "renamed",
    shm_new,
    bytes=size_of(shm_new),
    kbps=files[shm_new]["kbps"],
    seconds=352,
    artist="Galantis",
    title="Runaway (U & I) (Extended Mix)",
    reason=f"close match for Galantis - Runaway (U & I) (was {shm_old})",
)
robin = song_of("Avicii", "Hey Brother")
ev(
    60 * 4.4,
    "new",
    lib(robin),
    bytes=size_of(lib(robin)),
    kbps=files[lib(robin)]["kbps"],
    seconds=390,
    source="soulseek",
    artist="Avicii",
    title="Hey Brother",
    song=robin,
    matched="close",
    found="Hey Brother (Extended Version)",
    file_name="Avicii - Hey Brother (Extended Version).flac",
    fake=0,
    tries=3,
)
rihanna = song_of("Rihanna", "We Found Love")
ev(
    60 * 3.9,
    "wrong-song",
    "/music/inbox/soulseek/demo/We Found Love.flac",
    bytes=0,
    kbps=1011,
    seconds=215,
    source="soulseek",
    artist="Rihanna",
    title="We Found Love",
    song=rihanna,
    reason="artist 'Rihanna' not in the tags 'Lindsey Stirling', the file name 'We Found Love.flac' or its folders",
    found="We Found Love",
    file_name="We Found Love.flac",
    fake=0,
    tries=0,
)
blame_re = ev(
    60 * 3.3,
    "recheck",
    lib(blame),
    bytes=size_of(lib(blame)),
    kbps=files[lib(blame)]["kbps"],
    seconds=round(files[lib(blame)]["duration"]),
    source="soulseek",
    artist="Calvin Harris",
    title="Blame (feat. John Newman)",
    song=blame,
    matched="probable",
    reason="back in review: taken by a user, rick looks again",
    found="Blame (feat. John Newman) [Radio Edit]",
    file_name="Calvin Harris - Blame (feat. John Newman) [Radio Edit].flac",
)
purple_ev = ev(
    60 * 2.8,
    "confirm",
    kept_path("Boris Brejcha", "Purple Noise", "soulseek", "flac"),
    bytes=57_920_448,
    kbps=1055,
    seconds=439,
    source="soulseek",
    artist="Boris Brejcha",
    title="Purple Noise",
    song=PURPLE,
    matched="probable",
    reason="probable: 'Purple Noise (Original Mix)' has the core title, the same version words, length within 3 s",
    found="Purple Noise (Original Mix)",
    file_name="Boris Brejcha - Purple Noise (Original Mix).flac",
    fake=0,
    tries=0,
)
rivers = song_of("Lykke Li", "I Follow Rivers - The Magician Remix")
ev(
    60 * 2.3,
    "wrong-song",
    kept_path("Lykke Li", "I Follow Rivers - The Magician Remix", "soulseek", "flac"),
    bytes=30_877_201,
    kbps=979,
    seconds=275,
    source="soulseek",
    artist="Lykke Li",
    title="I Follow Rivers - The Magician Remix",
    song=rivers,
    reason="file name 'Lykke Li - I Follow Rivers (Live at Rockefeller)' names another version than "
    "'I Follow Rivers - The Magician Remix'",
    found="I Follow Rivers (Live at Rockefeller)",
    file_name="Lykke Li - I Follow Rivers (Live at Rockefeller).flac",
    fake=0,
    tries=0,
)
dark = song_of("Katy Perry", "Dark Horse")
upgrade(60 * 1.9, dark, "mp3", 128, 3_490_112, hand=True)
ev(
    60 * 1.4,
    "linked",
    lib(KYGO_SC),
    bytes=size_of(lib(KYGO_SC)),
    kbps=files[lib(KYGO_SC)]["kbps"],
    seconds=272,
    song=KYGO_SC,
    artist="Kygo",
    title="Firestone ft. Conrad Sewell",
    reason=f"same recording as {lib(KYGO_SC)} (the same audio)",
)
thunder = song_of("Imagine Dragons", "Thunder")
ev(
    60 * 1.1,
    "wrong-song",
    kept_path("Imagine Dragons", "Thunder", "soulseek", "flac"),
    bytes=24_301_877,
    kbps=1017,
    seconds=188,
    source="soulseek",
    artist="Imagine Dragons",
    title="Thunder",
    song=thunder,
    reason="title 'Thunder': no reading of tag 'Thunder / Young Dumb & Broke (Medley)' or file name "
    "'Imagine Dragons - Thunder - Young Dumb & Broke (Medley).flac' has the same core title and version",
    found="Thunder / Young Dumb & Broke (Medley)",
    file_name="Imagine Dragons - Thunder - Young Dumb & Broke (Medley).flac",
    fake=0,
    tries=0,
)
ev(
    52,
    "new",
    lib(LENS),
    bytes=size_of(lib(LENS)),
    kbps=256,
    seconds=281,
    source="soundcloud",
    artist="Amelie Lens",
    title="In My Mind",
    song=LENS,
    url=songs[LENS]["url"],
)
titan = song_of("David Guetta", "Titanium (feat. Sia)")
ev(
    34,
    "duplicate",
    lib(titan),
    bytes=size_of(lib(titan)),
    kbps=files[lib(titan)]["kbps"],
    seconds=245,
    source="soulseek",
    artist="David Guetta",
    title="Titanium (feat. Sia)",
    song=titan,
    matched="exact",
    found="Titanium",
    file_name="David Guetta - Titanium (feat. Sia).flac",
    fake=1,
    tries=0,
    peer_bytes=33_810_221,
)
janji_ev = ev(
    19,
    "mismatch",
    kept_path("Janji", "Heroes Tonight (feat. Johnning)", "soulseek", "mp3"),
    bytes=12_330_510,
    kbps=320,
    seconds=308,
    source="soulseek",
    artist="Janji",
    title="Heroes Tonight (feat. Johnning)",
    song=JANJI,
    found="Heroes Tonight",
    file_name="Janji - Heroes Tonight (feat. Johnning).mp3",
    fake=0,
    tries=3,
    wanted_seconds=208,
    audio="audio of the release (0.83)",
    compared="",
    peer_bytes=12_330_510,
)
icona = song_of("Icona Pop", "I Love It (feat. Charli XCX)")
upgrade(11, icona, "mp3", 320, 6_310_442)
fade_ev = ev(
    4,
    "new",
    lib(FADE),
    bytes=29_388_211,
    kbps=893,
    seconds=263,
    source="soulseek",
    artist="Alan Walker",
    title="Fade",
    song=FADE,
    matched="probable",
    reason="probable: 'Fade [NCS Release]' has the core title, the same version words, length within 3 s",
    found="Fade [NCS Release]",
    file_name="Alan Walker - Fade [NCS Release].flac",
    fake=0,
    tries=2,
    audio="audio unclear (0.61)",
    compared="",
    peer_bytes=29_388_211,
)

events.sort(key=lambda e: e["ts"])  # (stable: a retired file before its upgrade)
COLS = [
    "ts",
    "action",
    "path",
    "ext",
    "bytes",
    "kbps",
    "seconds",
    "source",
    "artist",
    "title",
    "reason",
    "song",
    "matched",
    "found",
    "file_name",
    "fake",
    "tries",
    "wanted_seconds",
    "audio",
    "url",
    "compared",
    "peer_bytes",
]
for e in events:
    e["id"] = write("events", {c: e.get(c) for c in COLS})

# files after the edits above; each came in when its download did (the history replays that)
for e in events:
    if e["action"] in ("new", "upgrade") and e["path"] in files:
        files[e["path"]]["mtime"] = int(dt.datetime.fromisoformat(e["ts"]).timestamp())
for f in files.values():
    write("files", {k: f[k] for k in ("path", "size", "mtime", "duration", "kbps", "quality")})
con.execute("UPDATE songs SET file = ? WHERE key = ?", (songs[sigala]["file"], sigala))


# ------------------------------------------------------------------ review decisions
def decide(e, decision, user_id, after_min, result, name=None, overridden=None):
    decided = (dt.datetime.fromisoformat(e["ts"]) + dt.timedelta(minutes=after_min)).isoformat(timespec="seconds")
    applied = (dt.datetime.fromisoformat(decided) + dt.timedelta(minutes=3)).isoformat(timespec="seconds")
    write(
        "review_decisions",
        {
            "id": f"{e['ts']} {e['path']}",
            "event_id": e["id"],
            "decision": decision,
            "name": name,
            "decided": decided,
            "applied": applied,
            "result": result,
            "user_id": user_id,
            "overridden": overridden,
        },
    )


decide(blame_ev, "ok", 2, 60 * 9, "kept", overridden=f"rick {blame_re['ts']}")
decide(stole_ev, "discard", 2, 60 * 4, "deleted")
decide(blame_re, "ok", 1, 22, "kept")
decide(shm_ev, "close", 1, 60 * 22, f"renamed {shm_new}", name="Galantis - Runaway (U & I) (Extended Mix)")
decide(sigala_kept, "accept", 1, 60 * 25, f"new {new_sigala}")
decide(posner_ev, "wrong", 1, 60 * 2, "retired")
decide(gravity_kept, "discard", 1, 60 * 3, "deleted")
decide(gravity_old, "discard", 1, 60 * 5, "deleted")
decide(habits_ev, "discard", 1, 50, "deleted")
decide(purple_ev, "discard", 1, 35, "deleted")
for e in events:  # older probable matches: confirmed
    if (
        e["action"] == "new"
        and e.get("matched") == "probable"
        and e is not fade_ev
        and e not in (blame_ev, shm_ev, posner_ev)
    ):
        decide(e, "ok", 1, 90, "kept")
for key, name in [
    (gravity, "Gravity"),
    (gravity, "Gravity (Original Mix)"),
    (stole, "Stole the Show"),
    (PURPLE, "Purple Noise (Original Mix)"),
]:
    write("blocked", {"song_key": key, "name": name})

# ------------------------------------------------------------------ jobs, availability, changes
n_files = len(files)
n_wanted = len({k for keys in list_songs.values() for k in keys})
n_have = len({k for keys in list_songs.values() for k in keys if songs[k]["file"]})
JOBS = [  # their last runs, in the words the jobs use (the Jobs page reads counts from them)
    ("sync", 2, 2, "10 lists, 1 changed"),
    ("search_new", 2, 1, "3 of 3 songs: 2 new, 1 not found"),
    ("soundcloud", 3, 3, "2 lists, 0 changed"),
    ("youtube", 21, 21, "2 lists, 0 changed"),
    ("sweep", 60 * 19.3, 60 * 18.4, "12 of 12 songs: 1 new, 11 not found"),
    ("fallback", 60 * 18.4, 60 * 17.9, "9 of 9 songs: 1 new, 8 not found"),
    ("upgrade", 25, 9, "40 of 40 songs: 6 upgrade, 1 interrupted, 33 not found"),
    ("upgrade_all", 60 * 24 * 3, 60 * 24 * 2.9, "212 of 212 songs: 9 upgrade, 203 not found"),
    (
        "library",
        4,
        4,
        f"{n_files} files, 2 new or changed (2 probed), 0 gone; {n_have} of {n_wanted} songs in the library; 1 playlists written",
    ),
    ("covers", 60 * 5, 60 * 4.9, "3 replaced, 912 already the song's, 4 without a song or cover"),
    (
        "availability",
        60 * 6.1,
        60 * 5.8,
        f"{n_wanted} songs checked: 9 taken down, 31 replaced; 2 changes, 0 removals explained",
    ),
    ("lossy", 60 * 30, 60 * 29, "11 lossy, 3 doubtful, 3790 ok; marked 0 made from lossy, unmarked 0"),
]
for name, start, end, msg in JOBS:
    write("jobs", {"name": name, "started": ago(start), "finished": ago(end), "ok": 1, "message": msg})

for name, state, since in [
    ("Avicii - Levels - Radio Edit", "blocked", 9),
    ("Kesha - TiK ToK", "taken_down", 6),
    ("Paul Kalkbrenner - Sky and Sand", "blocked", 8),
    ("The Weeknd - Blinding Lights", "blocked", 11),
]:
    write(
        "availability",
        {
            "song_key": M[name],
            "state": state,
            "since": (NOW - dt.timedelta(days=since)).date().isoformat(),
            "checked": ago(60 * 5.8),
        },
    )
    con.execute(
        "INSERT INTO changes (ts, song_key, list_key, change) VALUES (?, ?, NULL, ?)",
        (ago(60 * 24 * since), M[name], state),
    )
for k in R.sample(liked[:400], 8):
    con.execute(
        "INSERT INTO changes (ts, song_key, list_key, change) VALUES (?, ?, 'spotify:likes:1', 'added')",
        (ago(R.randint(60, 60 * 24 * 5)), k),
    )
for k in (AMELIE_HYP, MISSING["Adam Beyer - Your Mind"]):
    con.execute(
        "INSERT INTO changes (ts, song_key, list_key, change) VALUES (?, ?, 'spotify:playlist:demo3', 'added')",
        (ago(R.randint(150, 400)), k),
    )
con.execute(
    "INSERT INTO changes (ts, song_key, list_key, change, detail) VALUES (?, ?, 'spotify:playlist:demo2', "
    "'removed', 'taken down at its source')",
    (ago(60 * 30), song_of("Rihanna", "Only Girl (In The World)")),
)
con.commit()

# ------------------------------------------------------------------ the history: replayed and counted by Echolot
# Hour by hour from the first sync: the files that had come in by then (an upgraded one at its old quality
# before its upgrade), the songs they hold, the list songs added by then; history.collect counts each hour,
# so every graph and metric shows what Echolot would have stored.
con.execute("PRAGMA foreign_keys = ON")
con.commit()
mem = sqlite3.connect(":memory:")
con.backup(mem)
was = WAS
holders: dict[str, list[str]] = {}
for k, s_ in songs.items():
    if s_["file"]:
        holders.setdefault(s_["file"], []).append(k)
arrivals = sorted((f["mtime"], p) for p, f in files.items())
adds = sorted((when, lk, k) for (lk, k), when in ADDED.items())
positions = {(lk, k): pos for lk, keys in list_songs.items() for pos, k in enumerate(keys)}
mem.execute("DELETE FROM snapshots")
mem.execute("DELETE FROM files")
mem.execute("DELETE FROM list_songs")
mem.execute("UPDATE songs SET file = NULL")
step = dt.timedelta(hours=6 if FAST else 1)
when, fi, ai, upgraded, rows = (START - dt.timedelta(hours=1)).replace(minute=7, second=12), 0, 0, set(), []
while when < NOW - dt.timedelta(minutes=50):
    stamp, iso = when.timestamp(), when.isoformat(timespec="seconds")
    while fi < len(arrivals) and arrivals[fi][0] <= stamp:
        p = arrivals[fi][1]
        f = files[p]
        old = was.get(p)
        later = old and old[0] > iso
        mem.execute(
            "INSERT INTO files (path, size, mtime, duration, kbps, quality) VALUES (?, ?, ?, ?, ?, ?)",
            (
                p,
                old[2] if later else f["size"],
                f["mtime"],
                f["duration"],
                old[1] if later else f["kbps"],
                ("lossy-high" if old[1] >= 256 else "lossy-mid" if old[1] >= 160 else "lossy-low")
                if later
                else f["quality"],
            ),
        )
        mem.executemany("UPDATE songs SET file = ? WHERE key = ?", [(p, k) for k in holders.get(p, [])])
        fi += 1
    for p, (ts, _, _) in was.items():
        if p not in upgraded and ts <= iso:
            upgraded.add(p)
            f = files[p]
            mem.execute(
                "UPDATE files SET size = ?, kbps = ?, quality = ? WHERE path = ?",
                (f["size"], f["kbps"], f["quality"], p),
            )
    while ai < len(adds) and adds[ai][0] <= iso:
        _, lk, k = adds[ai]
        mem.execute(
            "INSERT INTO list_songs (list_key, position, song_key) VALUES (?, ?, ?)", (lk, positions[(lk, k)], k)
        )
        ai += 1
    rows += [(history.utc(when), m, k, v) for m, k, v in history.collect(mem)]
    when += step
mem.close()
con.executemany("INSERT OR IGNORE INTO snapshots (ts, metric, key, value) VALUES (?, ?, ?, ?)", rows)
con.commit()
history.snapshot(con, now=NOW, force=True)  # and now

# ------------------------------------------------------------------ the review files on disk
for d in (
    "tracks",
    "playlists",
    "inbox/soulseek",
    "inbox/soundcloud",
    "inbox/fallback",
    "inbox/review",
    "inbox/replaced",
    "inbox/upload",
):
    (MUSIC / d).mkdir(parents=True, exist_ok=True)
(MUSIC / "inbox" / ".ndignore").write_text("")
fade_file = MUSIC / "tracks" / "Alan Walker" / "Alan Walker - Fade.flac"
fade_file.parent.mkdir(parents=True, exist_ok=True)
janji_file = MUSIC / janji_ev["path"].removeprefix("/music/")
janji_file.parent.mkdir(parents=True, exist_ok=True)
import wave  # noqa: E402

for out, artist, title, seconds, codec in (
    []
    if FAST
    else [
        (fade_file, "Alan Walker", "Fade [NCS Release]", 263, ["-c:a", "flac"]),
        (janji_file, "Janji feat. Johnning", "Heroes Tonight", 308, ["-c:a", "libmp3lame", "-b:a", "320k"]),
    ]
):
    raw = DATA / "tone.wav"
    with wave.open(str(raw), "wb") as w:  # a quiet, slowly pulsing tone: something to play
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        one = bytearray()
        for i in range(22050):
            v = int(1500 * math.sin(2 * math.pi * 220 * i / 22050) * (0.6 + 0.4 * math.sin(2 * math.pi * i / 22050)))
            one += v.to_bytes(2, "little", signed=True)
        w.writeframes(bytes(one) * seconds)
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(raw),
            "-ac",
            "2",
            "-ar",
            "44100",
            *codec,
            "-metadata",
            f"artist={artist}",
            "-metadata",
            f"title={title}",
            str(out),
        ],
        check=True,
    )
    raw.unlink()

# ------------------------------------------------------------------ contract: what the scenes rely on
UPLOAD = [("Lady Gaga", "Poker Face", 237), ("Stromae", "Papaoutai", 232), ("Spice Girls", "Wannabe", 173)]  # make.py
q = lambda sql, *a: con.execute(sql, a).fetchone()  # noqa: E731
rick = [r[0] for r in con.execute("SELECT key FROM sources WHERE user_id = 1")]
problems = []
if q("SELECT count(*) FROM list_songs WHERE list_key = 'spotify:playlist:demo1'")[0] < 300:
    problems.append("the list scene's Trance Classics has fewer than 300 songs")
for a, t, _ in UPLOAD:
    row = q(
        "SELECT s.file, (SELECT count(*) FROM list_songs ls WHERE ls.song_key = s.key AND ls.list_key IN "
        f"({', '.join('?' * len(rick))})) FROM songs s WHERE s.artist = ? AND s.title = ?",
        *rick,
        a,
        t,
    )
    if not row or row[0] or not row[1]:
        problems.append(f"upload: {a} - {t} must be missing on one of rick's lists")
pending = q(
    "SELECT count(*) FROM events e WHERE e.id = ? AND NOT EXISTS (SELECT 1 FROM review_decisions d "
    "WHERE d.event_id = e.id)",
    fade_ev["id"],
)[0]
if not pending or not (FAST or fade_file.exists()):
    problems.append("review: the Perfect match download (Fade) must wait, its file on disk")
days = q("SELECT julianday(max(ts)) - julianday(min(ts)), count(DISTINCT ts) FROM snapshots")
if days[0] < DAYS - 1:
    problems.append(f"the history covers {days[0]:.1f} days, not {DAYS}")

print(
    "songs",
    q("SELECT count(*) FROM songs")[0],
    "wanted",
    q("SELECT count(*) FROM wanted")[0],
    "files",
    q("SELECT count(*), round(sum(size) / 1e9, 1) FROM files")[:],
    "snapshots",
    days[1],
)
print("quality", [tuple(r) for r in con.execute("SELECT quality, count(*) FROM files GROUP BY quality")])
con.close()
for problem in problems:
    print("contract:", problem, file=sys.stderr)
sys.exit(1 if problems else 0)
