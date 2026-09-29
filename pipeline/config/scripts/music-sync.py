#!/usr/bin/env python3
"""music-sync: one entry point for the music library.
Reads /config/sources.yml (what) and the container environment (secrets), then:
  sync       (sockseek container, VPN) fetch the Spotify lists, ask library.py which songs are really missing
             (exact artist + title + length) and let Sockseek download only those from Soulseek, FLAC first.
             Songs not found are retried after 3 h, 6 h, 12 h, then daily; after 2 failed searches the search is
             loosened (see LOOSEN). A download that is only probably the song is filed marked for review.
  sweep      search Soulseek again for every missing Spotify song, whatever its retry wait (scheduled at the
             hours most users are online).
  soundcloud (sockseek-fallback container, home IP) SoundCloud likes/playlists via yt-dlp
             (originals kept lossless, streams as-is). SoundCloud rate-limits the VPN exit.
  upgrade    every wanted song whose library copy is not genuine lossless (lossy, or a FLAC that spectrum.py
             found to be a re-encoded MP3) is searched on Soulseek again, FLAC only. Each song waits 12 h, 1 d,
             2 d, then 3 d between searches (state/upgrade-attempts.json); at most UPGRADE_BATCH songs per run,
             longest waiting first. `upgrade --all` searches every one now.
  fallback   (sockseek-fallback container, home IP) YouTube/SoundCloud search for songs Soulseek failed twice
             (`fallback --all`: every missing song now).
  playlists  rebuild every playlist file from the lists and the library (hourly).
  probe      availability statistics: search Soulseek for the songs in /config/probe.csv without downloading
             and append how many users have each (and in lossless) to logs/probe.jsonl (hourly).
  review     apply the decisions from Echolot's review page (config/review.yml); also done by the playlists job
             (every 10 min) and at the start of every Soulseek job.
  fill-albums  fill empty album tags from the Spotify metadata.
  status     list sizes, library counts and what is missing.
  sync/upgrade accept --dry-run (show what would be fetched) and a number (limit, for tests).
All filing into /music/tracks goes through library.py (never overwrites; replaced files are kept 30 days).
Naming: /music/tracks/<Artist>/<Artist> - <Title>.<ext>"""
import base64, csv, datetime, fcntl, json, os, pathlib, re, shutil, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request
import yaml

CONFIG = pathlib.Path("/config"); STATE = CONFIG / "state"; SCRIPTS = CONFIG / "scripts"
MUSIC = pathlib.Path("/music"); TRACKS = MUSIC / "tracks"; PLAYLISTS = MUSIC / "playlists"
INBOX = MUSIC / "inbox" / "soundcloud"            # yt-dlp SoundCloud downloads
SLSK_INBOX = MUSIC / "inbox" / "sockseek"         # Sockseek output (post-track.sh files it into the library)
FB_INBOX = MUSIC / "inbox" / "fallback"           # YouTube/SoundCloud search downloads
SC_STATE = STATE / "soundcloud-tracks.json"       # soundcloud id -> {artist, title, duration, stem}
SC_ARCHIVE = STATE / "soundcloud-archive.txt"     # yt-dlp download archive (never re-download an id)
ATTEMPTS = STATE / "attempts.json"                # "spotify:<id>" -> {n, last, fb} for songs Soulseek did not deliver
UPGRADE_ATTEMPTS = STATE / "upgrade-attempts.json" # "spotify:<id>" -> {n, last}: FLAC searches that found nothing better
UPGRADE_BATCH = 150                               # songs per upgrade run (~30 min); `upgrade --all` searches all
REVIEW_FILE = CONFIG / "review.yml"               # decisions from Echolot's review page (written by Echolot only)
REVIEW_DONE = STATE / "review-done.json"          # decision id -> {at, result}: applied decisions
REVIEW_RETRY = STATE / "review-retry.json"        # song -> {n, artist, title} (search again) or null (found), for attempts.json
# Songs Soulseek did not find are searched less strictly with every failure (by the searches that found nothing):
#   0-1: as requested, the artist must be in the Soulseek path, the file must be exactly the song
#   2-3: title without feat. credits, 'From "Film"' and plain suffixes (- Radio Edit, - Unmixed Version), first
#        artist only
#   4+ : also without requiring the artist in the Soulseek path (library.py still checks tags and names)
# Both loosened levels search desperately: a search without any result is repeated with the title alone and the
# artist alone (any length). Most clients do not answer queries with a phrase the server excludes, e.g. "Scooter"
# with "DJ" gives nothing while "Aiii Shot the DJ" alone finds Scooter's files.
LOOSEN = [(4, ["--strict-artist", "false", "--desperate"]), (2, ["--desperate"])]   # (failed searches, extra options), loosest first
EXT_PREF = ["flac", "wav", "aiff", "m4a", "mp3", "opus", "ogg", "webm", "aac"]
SC_FORMATS = "download/http_aac_256/hls_aac_256/hls_aac_160k/http_mp3_1_0/hls_mp3_1_0/bestaudio/best"
SPOTIFY_API = "https://api.spotify.com/v1"
PL_META = STATE / "playlist-meta.json"            # playlist file name -> {title, image, fetched_url}: name and cover from the source
LIKED_SONGS_IMAGE = "https://misc.scdn.co/liked-songs/liked-songs-640.png"   # Spotify's own "Liked Songs" cover
ENV = os.environ
SECRETS = [v for k, v in ENV.items() if k in ("SLSK_PASS", "SPOTIFY_SECRET", "SPOTIFY_REFRESH", "SC_TOKEN") and v]
sys.path.insert(0, str(SCRIPTS))
import library, spectrum  # noqa: E402

def log(msg):
    for s in SECRETS: msg = msg.replace(s, "***")
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)

def run(cmd, check=False, capture=False):
    log("$ " + " ".join(cmd))
    r = subprocess.run(cmd, text=True, capture_output=capture)
    if check and r.returncode != 0:
        raise RuntimeError(f"command failed ({r.returncode}): {cmd[0]}")
    return r

SOCKSEEK_LIMIT = (20 * 60, 15)                    # seconds per run + per song (see run_sockseek)

def run_sockseek(cmd, songs=1):
    """Run Sockseek, at most 20 min + 15 s per song (SOCKSEEK_LIMIT): a download queued at a peer can wait forever (Sockseek keeps
    queued downloads alive while the same peer delivers others), and a run holds the Soulseek lock. Songs it did not
    finish count as not found and are searched again later."""
    limit = SOCKSEEK_LIMIT[0] + SOCKSEEK_LIMIT[1] * songs
    log("$ " + " ".join(cmd))
    p = subprocess.Popen(cmd, text=True)
    try:
        p.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        log(f"sockseek: still running after {limit // 60} min (downloads stuck in peer queues), stopped")
        p.terminate()
        try: p.wait(timeout=30)
        except subprocess.TimeoutExpired: p.kill(); p.wait()
    return p

def slug(name): return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
clean_name = library.clean_name

def read_json(p, default):
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return default

def write_json(p, data):
    tmp = p.with_name(p.name + ".tmp"); tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8"); tmp.replace(p)

def resolve(stem):
    """Best existing file for /music/tracks/<stem>.* (stem relative to TRACKS)."""
    base = TRACKS / stem
    for ext in EXT_PREF:
        p = base.with_name(base.name + "." + ext)
        if p.exists(): return p
    return None

def write_m3u(name, paths):
    """Playlist file with paths relative to the m3u (../tracks/...), as Navidrome expects."""
    PLAYLISTS.mkdir(parents=True, exist_ok=True)
    lines, missing, seen = ["#EXTM3U"], 0, set()
    for p in paths:
        if p is None: missing += 1; continue
        if p in seen: continue
        seen.add(p); lines.append("../tracks/" + pathlib.Path(p).relative_to(TRACKS).as_posix())
    title = read_json(PL_META, {}).get(name, {}).get("title")
    if title: lines.insert(1, f"#PLAYLIST:{title}")   # name Navidrome gives a newly imported playlist
    out = PLAYLISTS / f"{clean_name(name)}.m3u"
    tmp = out.with_suffix(".m3u.tmp"); tmp.write_text("\n".join(lines) + "\n", encoding="utf-8"); tmp.replace(out)
    log(f"playlist '{name}': {sum(1 for l in lines if not l.startswith('#'))} tracks, {missing} not (yet) in library")

# ---------------------------------------------------------------- sources.yml
# A list entry is a URL, or {url, title, playlist}. Every list gets a stable internal key (state and playlist
# file names): the likes keep fixed keys, playlists are keyed by their URL, so no name is needed in the config.
SPOTIFY_LIKES, SC_LIKES = "Spotify Liked Songs", "SoundCloud Likes"

def spotify_key(url):
    m = re.search(r"playlist[/:]([A-Za-z0-9]+)", url)
    return f"spotify-{m.group(1) if m else slug(url)}"

def soundcloud_key(url):
    return "soundcloud-" + slug(urllib.parse.urlparse(url).path.strip("/").replace("/sets/", "-"))

def _entries(value):
    for e in value or []:
        yield {"url": e} if isinstance(e, str) else dict(e)

def list_options(src):
    """key -> options ({title, playlist}) of every configured list."""
    opts = {}
    for section, key in (("spotify", SPOTIFY_LIKES), ("soundcloud", SC_LIKES)):
        likes = (src.get(section) or {}).get("likes")
        if likes: opts[key] = likes if isinstance(likes, dict) else {}
    for e in _entries((src.get("spotify") or {}).get("playlists")): opts[spotify_key(e["url"])] = e
    for e in _entries((src.get("soundcloud") or {}).get("playlists")): opts[soundcloud_key(e["url"])] = e
    return opts

def title_override(src, name):
    """Optional `title:` of a list (otherwise the name the list has on Spotify/SoundCloud)."""
    return list_options(src).get(name, {}).get("title")

def show_playlist(src, name):
    """`playlist: false` = download the list's songs, but no playlist in Navidrome."""
    return list_options(src).get(name, {}).get("playlist", True) is not False

def set_playlist_meta(src, name, title, image_url):
    """Remember a list's source name and fetch its cover as <playlist>.jpg/png beside the .m3u, which
    Navidrome uses as the playlist image. Failures only cost the cover, never the sync."""
    meta = read_json(PL_META, {}); cur = meta.get(name, {})
    cur["title"] = title_override(src, name) or title or cur.get("title") or name
    base = clean_name(name)
    have = [e for e in ("jpg", "png") if (PLAYLISTS / f"{base}.{e}").exists()]
    if image_url and (cur.get("fetched_url") != image_url or not have):
        try:
            data = urllib.request.urlopen(urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read()
            ext = "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
            PLAYLISTS.mkdir(parents=True, exist_ok=True)
            tmp = PLAYLISTS / f".{base}.{ext}.tmp"; tmp.write_bytes(data)
            for e in ("jpg", "png"):
                if e != ext: (PLAYLISTS / f"{base}.{e}").unlink(missing_ok=True)
            tmp.replace(PLAYLISTS / f"{base}.{ext}")
            cur["fetched_url"] = image_url
        except Exception as e:
            log(f"playlist '{name}': cover not fetched ({e})")
    meta[name] = cur; write_json(PL_META, meta)

# ---------------------------------------------------------------- Sockseek
def sockseek_cmd(inp, extra):
    idx = STATE / "sockseek-run-index.csv"
    idx.unlink(missing_ok=True)   # bookkeeping of this run only; what is missing is decided by library.py
    # the Soulseek login is in sockseek.conf (user, pass), not on the command line, where ps would show it
    cmd = ["sockseek", inp, "-c", str(CONFIG / "sockseek.conf"), "--index-path", str(idx)]
    # Second ProtonVPN forwarded port, assigned by host/sync-listen-port.sh (host cron)
    port_file = STATE / "listen-port"
    port = port_file.read_text().strip() if port_file.exists() else ""
    if port.isdigit() and ENV.get("ROLE") == "main": cmd += ["--listen-port", port]
    return cmd + extra

def search_title(title):
    """Title for a loosened search: without feat. credits, 'From "Film"' and a trailing ' - Radio Edit',
    ' - Unmixed Version', ' - 2011 Remaster' (plain words only; ' - X Remix' stays)."""
    t = re.sub(r"\s*[\(\[]\s*(?:feat|ft|featuring|with|from)\.?\s[^\)\]]*[\)\]]", "", title, flags=re.I)
    t = re.sub(r"\s+-\s+from\s.*$", "", t, flags=re.I)
    m = re.match(r"^(.+?)\s+-\s+([^-]+)$", t)
    if m and all(w in library.PLAIN_WORDS or w.isdigit() for w in library._words(m.group(2)).split()): t = m.group(1)
    return t.strip() or title

def search_terms(r, loosen=False):
    """(artist, title, length) Sockseek searches for a wanted song. loosen: first artist, search_title. A DJ-mix cut
    is searched as the release at any length. "/" and "\\" cannot occur in a Soulseek path, so strict-artist dropped
    every result for "AC/DC" or "Miksu / Macloud": "AC DC" matches AC_DC, AC DC."""
    artist, title = (library.first_artist(r["artist"]) or r["artist"], search_title(r["title"])) if loosen else (r["artist"], r["title"])
    cut = library.mix_cut(r["title"])
    if cut: title = library.release_title(title)
    return re.sub(r"\s*[/\\]+\s*", " ", artist).strip(), title, 0 if cut else int(r.get("length") or 0)

def write_search_csv(rows, path, loosen=False, probable=True):
    """Sockseek's input: the search terms (Artist, Title, Length) and the wanted song (want_artist, want_title, tries,
    artists, probable), which post-track.sh hands to library.py."""
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Artist", "Title", "Album", "Length", "uri", "want_artist", "want_title", "tries", "artists", "probable"])
        for r in rows:
            artist, title, length = search_terms(r, loosen)
            w.writerow([artist, title, r.get("album", ""), length, r.get("uri", ""),
                        r["artist"], r["title"], r.get("tries", 0), "; ".join(r.get("artists") or []), int(probable)])

def search_groups(rows):
    """[(label suffix, extra Sockseek options, loosen, rows)]: the songs by how loosely they are searched (LOOSEN,
    by 'tries', the searches that did not find them)."""
    out, rest = [], list(rows)
    for tries, extra in LOOSEN:
        group = [r for r in rest if r.get("tries", 0) >= tries]; rest = [r for r in rest if r.get("tries", 0) < tries]
        if group: out.append((f"-loose{tries}", extra, True, group))
    if rest: out.append(("", [], False, rest))
    return out

def soulseek_download(rows, label, extra=(), loosen=False, probable=True):
    """Let Sockseek fetch exactly these songs into the inbox (see write_search_csv; probable=False: a probable match is
    kept for review instead of filed, for FLAC upgrades). --no-skip-existing: Sockseek's own fuzzy "already have it"
    checks are off."""
    if not rows: return
    csvp = STATE / f"sockseek-{label}.csv"
    write_search_csv(rows, csvp, loosen, probable)
    shutil.rmtree(SLSK_INBOX, ignore_errors=True)
    log(f"soulseek: {len(rows)} songs to fetch ({label})")
    os.environ["MUSIC_SYNC_CSV"] = str(csvp)      # post-track.sh reads the wanted song from here by row number
    run_sockseek(sockseek_cmd(str(csvp), ["--no-skip-existing", "-o", str(SLSK_INBOX), *extra]), len(rows))
    shutil.rmtree(SLSK_INBOX, ignore_errors=True)   # anything the hook did not file (failed checks)

# ---------------------------------------------------------------- Spotify
def spotify_lists(src):
    """[(input, key)]: 'spotify-likes' for Liked Songs, else the playlist URL (own or public playlists of other
    users; Spotify's own editorial/algorithmic playlists are not readable through the API)."""
    sp = src.get("spotify") or {}
    lists = [("spotify-likes", SPOTIFY_LIKES)] if sp.get("likes") else []
    return lists + [(e["url"], spotify_key(e["url"])) for e in _entries(sp.get("playlists"))]

def spotify_token():
    rt_file = STATE / "spotify-refresh-token"      # a rotated refresh token (Spotify may issue a new one)
    refresh = rt_file.read_text().strip() if rt_file.exists() else ENV.get("SPOTIFY_REFRESH", "")
    body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh}).encode()
    auth = base64.b64encode(f"{ENV['SPOTIFY_ID']}:{ENV['SPOTIFY_SECRET']}".encode()).decode()
    req = urllib.request.Request("https://accounts.spotify.com/api/token", data=body,
                                 headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    if d.get("refresh_token") and d["refresh_token"] != refresh:
        rt_file.write_text(d["refresh_token"]); os.chmod(rt_file, 0o600); SECRETS.append(d["refresh_token"])
        log("spotify: refresh token rotated, stored in state/spotify-refresh-token")
    SECRETS.append(d["access_token"])
    return d["access_token"]

def spotify_get(url, token):
    for attempt in range(6):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}), timeout=30))
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = int(e.headers.get("Retry-After") or 10 * (attempt + 1)); log(f"spotify: HTTP {e.code}, waiting {wait}s"); time.sleep(wait); continue
            raise
    raise RuntimeError(f"spotify: giving up on {url}")

def spotify_items(inp, token):
    """Songs of a Spotify list in list order: id, uri, artist (first), artists, title, album, length (s)."""
    if inp == "spotify-likes":
        url = f"{SPOTIFY_API}/me/tracks?limit=50"
    else:
        m = re.search(r"playlist[/:]([A-Za-z0-9]+)", inp)
        if not m: raise ValueError(f"not a Spotify playlist: {inp}")
        url = f"{SPOTIFY_API}/playlists/{m.group(1)}/items?limit=50"
    items = []
    while url:
        d = spotify_get(url, token)
        for it in d.get("items") or []:
            t = it.get("item") or it.get("track")
            if not t or t.get("type") not in (None, "track") or t.get("is_local") or not t.get("id"): continue
            artists = [a.get("name", "") for a in t.get("artists") or []]
            items.append({"id": t["id"], "uri": t.get("uri") or f"spotify:track:{t['id']}", "artist": artists[0] if artists else "",
                          "artists": artists, "title": t.get("name", ""), "album": (t.get("album") or {}).get("name", ""),
                          "length": round((t.get("duration_ms") or 0) / 1000)})
        url = d.get("next")
    return items

def remember_songs(name, items):
    """Every song ever seen in a list is kept in state/spotify-<list>-history.json (first/last seen, last known
    artist/title/album/length). Spotify can blank the metadata of songs it removes from its catalogue while they
    stay in your list; then the remembered name is used, so the song can still be found and matched."""
    hist_file = STATE / f"spotify-{slug(name)}-history.json"
    hist, today = read_json(hist_file, {}), datetime.date.today().isoformat()
    for it in items:
        old = hist.get(it["id"])
        if old and (not it["title"] or not it["artist"]):
            for k in ("artist", "artists", "title", "album", "length"):
                if not it.get(k) and old.get(k): it[k] = old[k]
        hist[it["id"]] = {**(old or {"first_seen": today}), **{k: v for k, v in it.items() if v}, "last_seen": today}
    write_json(hist_file, hist)
    return [it for it in items if it["title"] and it["artist"]]   # nameless songs (never seen with a name) cannot be searched

def spotify_unplayable(token):
    """Ids of liked songs Spotify greys out (removed from its catalogue); they are downloaded first."""
    url, ids = f"{SPOTIFY_API}/me/tracks?limit=50&market=from_token", []
    while url:
        d = spotify_get(url, token)
        ids += [t["id"] for t in (i.get("track") for i in d.get("items") or []) if t and t.get("id") and t.get("is_playable") is False]
        url = d.get("next")
    return ids

def load_spotify_lists(src, fetch=True):
    """[(name, items)] - fetched fresh (and cached in state), or the cached copy if fetching fails / is off."""
    out, token = [], None
    for inp, name in spotify_lists(src):
        cache = STATE / f"spotify-{slug(name)}.json"
        items = None
        if fetch and ENV.get("SPOTIFY_REFRESH"):
            try:
                token = token or spotify_token()
                items = remember_songs(name, spotify_items(inp, token))
                write_json(cache, items)
                log(f"spotify '{name}': {len(items)} songs")
                if inp == "spotify-likes":
                    set_playlist_meta(src, name, "Liked Songs", LIKED_SONGS_IMAGE)
                    write_json(STATE / "spotify-unplayable.json", spotify_unplayable(token))
                else:
                    pid = re.search(r"playlist[/:]([A-Za-z0-9]+)", inp).group(1)
                    info = spotify_get(f"{SPOTIFY_API}/playlists/{pid}?fields=name,images", token)
                    set_playlist_meta(src, name, info.get("name"), ((info.get("images") or [{}])[0] or {}).get("url"))
            except Exception as e:
                log(f"spotify '{name}': listing failed ({e}), using the last known list")
        if items is None: items = read_json(cache, None)
        if items is not None: out.append((name, items))
    return out

def due(a, first=3 * 3600, cap=86400):
    """Retry after `first`, then doubling up to `cap` (missing songs: 3 h, 6 h, 12 h, then daily)."""
    if not a: return True
    wait = min(first * 2 ** max(a.get("n", 1) - 1, 0), cap)
    return time.time() - a.get("last", 0) >= wait - 1800

def wanted_from_soulseek(src, fetch=True):
    """Songs Soulseek is used for: Spotify list songs only. SoundCloud likes come from SoundCloud (or, if
    SoundCloud will not deliver them, from an exact YouTube match): their artist names are too unreliable
    for Soulseek search results. Keyed 'spotify:<id>'."""
    wanted = {}
    for _, items in load_spotify_lists(src, fetch):
        for it in items: wanted.setdefault("spotify:" + it["id"], {**it, "key": "spotify:" + it["id"]})
    return wanted

def run_spotify(src, limit=None, dry=False, sweep=False):
    """sweep: every missing song, whatever its retry wait (the lists are not fetched again)."""
    wanted = wanted_from_soulseek(src, fetch=not sweep)
    cat = library.Catalog()
    attempts = read_json(ATTEMPTS, {})
    missing = [it for it in wanted.values() if not cat.song(it)]
    todo = missing if sweep else [it for it in missing if due(attempts.get(it["key"]))]
    unplayable = set(read_json(STATE / "spotify-unplayable.json", []))
    todo.sort(key=lambda it: it["id"] not in unplayable)    # greyed out on Spotify first: most at risk
    if limit: todo = todo[:limit]
    log(f"soulseek: {len(wanted)} songs wanted, {len(wanted) - len(missing)} in library, {len(missing)} missing, {len(todo)} due now")
    if dry:
        for it in todo[:40]: log(f"  would fetch: {it['artist']} - {it['title']} ({it['length']}s)")
        return
    label = "sweep" if sweep else "spotify"
    for it in todo: it["tries"] = (attempts.get(it["key"]) or {}).get("n", 0)
    for suffix, extra, loosen, group in search_groups(todo):
        if loosen: log(f"soulseek: {len(group)} songs searched loosened ({suffix[1:]})")
        soulseek_download(group, label + suffix, extra, loosen)
    cat = library.Catalog(); now = int(time.time()); got = 0
    for it in todo:
        key = it["key"]
        if cat.song(it): attempts.pop(key, None); got += 1
        else:
            a = attempts.setdefault(key, {"n": 0}); a.update(n=a["n"] + 1, last=now, artist=it["artist"], title=it["title"])
    write_json(ATTEMPTS, attempts)
    log(f"soulseek: {got} of {len(todo)} fetched")
    write_playlists(src, cat)

# ---------------------------------------------------------------- SoundCloud
def ytdlp(args, capture=False):
    return run(["yt-dlp", "--username", "oauth", "--password", ENV["SC_TOKEN"], "--no-warnings",
                "--sleep-requests", "1.5", "--extractor-retries", "8", "--retry-sleep", "extractor:exp=10:300",
                "--retry-sleep", "http:exp=10:300", *args], capture=capture)

def sc_lists(src):
    """[(url, key)]: your likes, plus any public set or any user's likes page (https://soundcloud.com/<user>/likes)."""
    sc = src.get("soundcloud") or {}
    lists = [(f"https://soundcloud.com/{sc['user']}/likes", SC_LIKES)] if sc.get("likes") and sc.get("user") else []
    return lists + [(e["url"], soundcloud_key(e["url"])) for e in _entries(sc.get("playlists"))]

def sc_order(url):
    """Ordered (id, url) of the tracks in a SoundCloud list, or None if listing failed.
    Liked sets/albums are left out: likes mean liked songs; add a set under playlists in sources.yml to get it.
    SoundCloud answers 429 (or an empty page) after bursts, so back off and retry."""
    for attempt in range(8):
        r = ytdlp(["--flat-playlist", "-J", url], capture=True)
        if r.returncode == 0:
            es = [e for e in (json.loads(r.stdout).get("entries") or []) if e and e.get("id")]
            if es:
                sets = [e.get("url") for e in es if "/sets/" in (e.get("url") or "")]
                if sets: log(f"soundcloud: {url}: skipping {len(sets)} liked sets (add them as playlists to download): {' '.join(sets)}")
                return [(str(e["id"]), e.get("url")) for e in es if "/sets/" not in (e.get("url") or "")], json.loads(r.stdout)
            log(f"soundcloud: empty listing for {url} (usually rate limiting), waiting 120s (attempt {attempt + 1}/8)"); time.sleep(120); continue
        if "429" in r.stderr:
            log(f"soundcloud: rate limited while listing {url}, waiting 120s (attempt {attempt + 1}/8)"); time.sleep(120); continue
        log(f"soundcloud: listing failed for {url}: {r.stderr.strip()[-300:]}"); break
    return None, None

def sc_list_meta(src, url, name, info):
    """Name and cover of a SoundCloud list: a set keeps its own title and artwork; the likes get
    'SoundCloud Likes' with the profile picture (SoundCloud has no likes cover)."""
    if url.rstrip("/").endswith("/likes"):
        user = url.rstrip("/").rsplit("/", 2)[-2]
        title, image = ("SoundCloud Likes" if name == SC_LIKES else f"{user} – SoundCloud Likes"), None
        try:
            page = urllib.request.urlopen(urllib.request.Request(url.rsplit("/likes", 1)[0], headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read().decode("utf-8", "replace")
            m = re.search(r"https://i1\.sndcdn\.com/avatars-[^\"'\s]+?-t500x500\.(?:jpg|png)", page)
            image = m.group(0) if m else None
        except Exception: pass
    else:
        title = info.get("title") or info.get("album")
        thumbs = [t.get("url") for t in info.get("thumbnails") or [] if t.get("url")]
        image = re.sub(r"-(?:mini|tiny|small|badge|t67x67|large|t300x300|crop|t500x500|original)\.(jpg|png)$", "-t500x500.jpg", thumbs[-1]) if thumbs else None
    set_playlist_meta(src, name, title, image)

def sc_artist_title(uploader, artist, title):
    """Prefer 'Artist - Title' from the title, then the artist field, then the uploader; first artist only."""
    m = re.match(r"^(?P<a>[^-]{1,60}?)\s+-\s+(?P<t>.+)$", title or "")
    if m and library.title_key(m.group("t")):   # "Song - Original Mix" is a title, not "Artist - Title"
        a, t = m.group("a"), m.group("t")
    else:
        a = artist if artist and artist != "NA" else uploader
        t = title
    a = re.split(r"\s*[,，;/]\s*|\s+[xX&]\s+|\s+feat\.?\s+|\s+ft\.?\s+", a or "")[0] or (uploader or "Unknown")
    return clean_name(a), clean_name(t)

SC_NEW = STATE / "soundcloud-new.tsv"   # yt-dlp appends one line per finished download; processed by sc_process_new

def sc_archived():
    return {l.split()[-1] for l in SC_ARCHIVE.read_text().splitlines() if l.strip()} if SC_ARCHIVE.exists() else set()

def sc_unarchive(ids):
    if ids and SC_ARCHIVE.exists():
        keep = [l for l in SC_ARCHIVE.read_text().splitlines() if l.split() and l.split()[-1] not in ids]
        SC_ARCHIVE.write_text("\n".join(keep) + "\n")

def sc_download(tracks, state):
    """Download the listed tracks that are new (not in the yt-dlp archive), then file them into the library."""
    INBOX.mkdir(parents=True, exist_ok=True)
    done = sc_archived() | set(state)
    todo = [u for i, u in tracks if i not in done and u]
    if not todo: return 0
    for junk in list(INBOX.glob("*.part")) + list(INBOX.glob("*.part-Frag*")) + list(INBOX.glob("*.ytdl")): junk.unlink()
    batch = STATE / "soundcloud-batch.txt"; batch.write_text("\n".join(todo) + "\n")
    dl_args = ["--download-archive", str(SC_ARCHIVE), "-f", SC_FORMATS, "--ignore-errors", "--no-overwrites",
               "--write-thumbnail", "--convert-thumbnails", "jpg", "--embed-metadata", "--no-embed-thumbnail",
               "-o", str(INBOX / "%(id)s.%(ext)s"), "-o", "thumbnail:" + str(INBOX / "%(id)s"),
               "--print-to-file", "after_move:%(id)s\t%(uploader)s\t%(artist)s\t%(title)s\t%(duration)s\t%(filepath)s", str(SC_NEW),
               "-a", str(batch)]
    log(f"soundcloud: downloading {len(todo)} new tracks")
    for attempt in range(6):   # items that got a 429 are skipped by yt-dlp (not archived); rerun after a pause
        r = ytdlp(dl_args, capture=True)
        sys.stdout.write(r.stdout[-4000:]); sys.stderr.write(r.stderr[-4000:]); sys.stdout.flush()
        if "429" not in r.stderr: break
        log(f"soundcloud: rate limited during download, waiting 180s (attempt {attempt + 1}/6)"); time.sleep(180)
    batch.unlink(missing_ok=True)
    added = sc_process_new(state, {i for i, _ in tracks}, drop_unwanted=False)
    # tracks SoundCloud does not hand out (DRM-protected label releases): record their metadata once and stop
    # asking SoundCloud; the Soulseek sync (and the YouTube fallback) look for them like missing Spotify songs
    archived = sc_archived()
    for i, u in tracks:
        if i in state or i in archived or not u: continue
        r = ytdlp(["-J", "--skip-download", "--ignore-no-formats-error", u], capture=True)
        try: d = json.loads(r.stdout)
        except Exception: continue
        if d.get("formats"): continue            # downloadable after all (e.g. rate limited): retried next run
        a, t = sc_artist_title(d.get("uploader"), d.get("artist"), d.get("title"))
        state[i] = {"artist": a, "title": t, "duration": str(d.get("duration") or 0), "stem": None, "unavailable": "no downloadable format (DRM)"}
        log(f"soundcloud: {a} - {t} is not downloadable from SoundCloud (DRM), handed to Soulseek")
    return added

def sc_process_new(state, wanted, drop_unwanted):
    """File downloaded SoundCloud tracks (lines in SC_NEW) into the library via library.py. Also recovers
    downloads of an interrupted run. Lines for ids not in `wanted` stay pending, or with drop_unwanted (all
    lists listed fine) are deleted from the inbox and removed from the archive."""
    if not SC_NEW.exists(): return 0
    added, pending, dropped = 0, [], set()
    for line in SC_NEW.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 6: continue
        sid, uploader, artist, title, duration, fpath = parts[:6]
        src = pathlib.Path(fpath)
        thumb = INBOX / f"{sid}.jpg"
        if sid not in wanted:
            if drop_unwanted: src.unlink(missing_ok=True); thumb.unlink(missing_ok=True); dropped.add(sid)
            else: pending.append(line)
            continue
        if not src.exists(): continue
        a, t = sc_artist_title(uploader, artist, title)
        if src.suffix.lower() in (".wav", ".aiff", ".aif"):  # lossless original -> FLAC
            flac = src.with_suffix(".flac")
            if run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-map", "0:a:0", "-c:a", "flac", str(flac)]).returncode == 0:
                src.unlink(); src = flac
        check = spectrum.analyse(str(src)) if src.suffix.lower() == ".flac" else None
        action, dest = library.file_into(src, a, t, float(duration or 0), "soundcloud", [f"soundcloud:{sid}"],
                                         fake=bool(check and check.get("verdict") == "lossy"))
        if action == "duplicate":
            log(f"soundcloud: already in library ({dest.relative_to(TRACKS)}), download discarded")
        else:
            set_tags(dest, a, t)
            if check: spectrum.update_list(str(dest), check)
            subprocess.run([sys.executable, str(SCRIPTS / "artwork.py"), str(dest), "", str(thumb) if thumb.exists() else ""])
            added += 1
        thumb.unlink(missing_ok=True)
        state[sid] = {"artist": a, "title": t, "duration": duration, "stem": str(dest.relative_to(TRACKS).with_suffix(""))}
    sc_unarchive(dropped)
    if dropped: log(f"soundcloud: dropped {len(dropped)} downloads that belong to no configured list")
    if pending: SC_NEW.write_text("\n".join(pending) + "\n", encoding="utf-8")
    else: SC_NEW.unlink(missing_ok=True)
    return added

def set_tags(path, artist, title):
    """Write the parsed artist/title into the file tags (SoundCloud metadata often has the artist inside the title)."""
    try:
        from mutagen import File as MFile
        a = MFile(str(path), easy=True)
        if a is None: return
        if a.tags is None: a.add_tags()
        a["artist"] = artist; a["title"] = title; a["albumartist"] = artist
        if not a.tags.get("album"): a["album"] = title
        a.save()
    except Exception as e:
        log(f"tags: could not write {path}: {e}")

def retag_soundcloud(state):
    """Repair: apply set_tags to every known SoundCloud file."""
    n = 0
    for e in state.values():
        p = resolve(e["stem"]) if e.get("stem") else None
        if p: set_tags(p, e["artist"], e["title"]); n += 1
    log(f"retagged {n} SoundCloud files")

def run_soundcloud(src):
    if not ENV.get("SC_TOKEN"): log("soundcloud: SC_TOKEN not set, skipping"); return
    state = read_json(SC_STATE, {})
    listed = []
    for url, name in sc_lists(src):
        tracks, info = sc_order(url)
        log(f"soundcloud '{name}': " + (f"{len(tracks)} tracks listed" if tracks is not None else "listing failed, list skipped this run"))
        listed.append((url, name, tracks))
        if info: sc_list_meta(src, url, name, info)
        if tracks is not None:
            write_json(STATE / f"soundcloud-order-{slug(name)}.json", [i for i, _ in tracks])
            hist_file, today = STATE / f"soundcloud-{slug(name)}-history.json", datetime.date.today().isoformat()
            hist = read_json(hist_file, {})
            for i, _ in tracks: hist[i] = {"first_seen": hist.get(i, {}).get("first_seen", today), "last_seen": today}
            write_json(hist_file, hist)
    wanted = {i for _, _, t in listed if t for i, _ in t}
    added = sc_process_new(state, wanted, drop_unwanted=all(t is not None for _, _, t in listed))
    if added: log(f"soundcloud: filed {added} downloads of an earlier run")
    write_json(SC_STATE, state)
    for url, name, tracks in listed:
        if tracks is None: continue   # keep the old playlist file rather than writing an empty one
        added = sc_download(tracks, state); write_json(SC_STATE, state)
        log(f"soundcloud '{name}': {added} new files")
    write_playlists(src)

def sc_items(src):
    """[(name, [{artist, title, length, stem, id}])] of the SoundCloud lists, from the last successful listing."""
    state, out = read_json(SC_STATE, {}), []
    for _, name in sc_lists(src):
        order = read_json(STATE / f"soundcloud-order-{slug(name)}.json", None)
        if order is None: continue
        out.append((name, [{"artist": state[i]["artist"], "title": state[i]["title"], "length": float(state[i]["duration"] or 0),
                            "stem": state[i].get("stem"), "id": i, "key": "soundcloud:" + i, "uri": "", "album": "",
                            "unavailable": bool(state[i].get("unavailable"))} for i in order if i in state]))
    return out

# ---------------------------------------------------------------- weekly FLAC upgrade
def run_upgrade(src, dry=False, force=False):
    """Search Soulseek (FLAC only) for the Spotify songs whose library copy is not genuine lossless and whose
    wait is over (12 h, 1 d, 2 d, then every 3 d; force: all of them now).
    SoundCloud likes are not upgraded from Soulseek (unreliable artist names led to wrong songs)."""
    cat = library.Catalog()
    tries = read_json(UPGRADE_ATTEMPTS, {})
    sp_rows, seen = [], set()
    for _, items in load_spotify_lists(src, fetch=False):
        for it in items:
            hit = cat.song(it)
            if hit and not hit[0].genuine and str(hit[0].path) not in seen:
                seen.add(str(hit[0].path)); sp_rows.append(it)
    todo = sp_rows if force else [it for it in sp_rows if due(tries.get("spotify:" + it["id"]), 12 * 3600, 3 * 86400)]
    if not force:   # a run holds the Soulseek lock: keep it short, so syncs are not held up for hours
        todo = sorted(todo, key=lambda it: (tries.get("spotify:" + it["id"]) or {}).get("last", 0))[:UPGRADE_BATCH]
    log(f"upgrade: {len(sp_rows)} Spotify songs are not genuine lossless yet, {len(todo)} searched now (at most {UPGRADE_BATCH}, longest waiting first)")
    if dry: return
    soulseek_download(todo, "upgrade-spotify", ["--format", "flac"], probable=False)   # replaces a copy: exact only
    after = library.Catalog(); now = int(time.time()); better = 0
    tries = {k: v for k, v in tries.items() if k in {"spotify:" + it["id"] for it in sp_rows}}   # drop songs that left
    for it in todo:
        key = "spotify:" + it["id"]
        if (h := after.song(it)) and h[0].genuine:
            better += 1; tries.pop(key, None)
        else:
            t = tries.setdefault(key, {"n": 0}); t.update(n=t["n"] + 1, last=now)
    write_json(UPGRADE_ATTEMPTS, tries)
    log(f"upgrade: {better} of {len(todo)} now genuine lossless")
    write_playlists(src, after)

# ---------------------------------------------------------------- fallback (YouTube / SoundCloud search)
def yt_search_download(artist, title, length, dest_noext):
    """Search YouTube then SoundCloud for 'artist - title' with a matching duration; download best audio.
    Returns (file path, site, video title, [uploader]) or (None, None, "", [])."""
    query = f"{artist} - {title}"
    for prefix, site in (("ytsearch5:", "youtube"), ("scsearch5:", "soundcloud-search")):
        r = subprocess.run(["yt-dlp", "--js-runtimes", "node", "--no-warnings", "--flat-playlist", "-j", prefix + query],
                           text=True, capture_output=True)
        cands = []
        for line in r.stdout.splitlines():
            try: e = json.loads(line)
            except Exception: continue
            dur = e.get("duration")
            if dur is None or not length or abs(float(dur) - float(length)) <= 8:
                cands.append(e)
        for e in cands[:2]:
            url = e.get("url") or e.get("webpage_url")
            if not url: continue
            args = ["yt-dlp", "--js-runtimes", "node", "--no-warnings", "-f", "bestaudio/best", "-x", "--audio-quality", "0",
                    "--embed-metadata", "--no-playlist", "-o", dest_noext + ".%(ext)s", "--print", "after_move:filepath", url]
            if site == "soundcloud-search" and ENV.get("SC_TOKEN"): args += ["--username", "oauth", "--password", ENV["SC_TOKEN"]]
            r2 = run(args, capture=True)
            out = [l for l in r2.stdout.splitlines() if l.strip()]
            if r2.returncode == 0 and out and os.path.isfile(out[-1]):
                return out[-1], site, e.get("title") or "", [e.get("uploader") or e.get("channel") or ""]
            log(f"fallback: download failed for {url}: {r2.stderr.strip()[-200:]}")
    return None, None, "", []

def run_fallback(src, force=False):
    """Songs Soulseek failed at least twice: try YouTube/SoundCloud search (once a week per song). The result is
    lossy; the weekly upgrade keeps looking for a FLAC, which then replaces it.
    force (`fallback --all`): every song that is still missing, right now."""
    attempts = read_json(ATTEMPTS, {}); now = int(time.time())
    cat = library.Catalog(); done = 0
    FB_INBOX.mkdir(parents=True, exist_ok=True)
    sc_drm = [it for _, items in sc_items(src) for it in items if it["unavailable"]]
    for it in list(wanted_from_soulseek(src, fetch=False).values()) + sc_drm:
        a = attempts.setdefault(it["key"], {"n": 0}) if it in sc_drm else attempts.get(it["key"])
        if cat.song(it): continue
        if force: a = attempts.setdefault(it["key"], {"n": 0}); time.sleep(2)   # gentle on YouTube
        elif (it not in sc_drm and (not a or a.get("n", 0) < 2)) or now - a.get("fb", 0) < 7 * 86400: continue
        a["fb"] = now; write_json(ATTEMPTS, attempts)
        cut = library.mix_cut(it["title"])      # DJ-mix cut: search the release, at any length
        got, site, vtitle, uploader = yt_search_download(it["artist"], library.release_title(it["title"]), 0 if cut else it["length"],
                                                         str(FB_INBOX / it["key"].replace(":", "-")))
        if not got: log(f"fallback: nothing found for {it['artist']} - {it['title']}"); continue
        # same identity check as Soulseek downloads: the video must really be <artist> - <title>
        action, dest = library.file_into(got, it["artist"], it["title"], it["length"], site, [it["key"]],
                                         strict=True, file_name=vtitle, folders=uploader, probable=it not in sc_drm,
                                         tries=a.get("n", 0), artists=it.get("artists") or ())
        if dest is None: log(f"fallback: {action}, '{vtitle}' for {it['artist']} - {it['title']} not filed"); continue
        log(f"fallback: {action} {dest.relative_to(TRACKS)} ({site})")
        if action != "duplicate":
            art = [str(dest), it["uri"]] if it.get("uri") else [str(dest), "search", "", it["artist"], it["title"]]
            subprocess.run([sys.executable, str(SCRIPTS / "artwork.py"), *art]); done += 1
        cat = library.Catalog()
    shutil.rmtree(FB_INBOX, ignore_errors=True)
    log(f"fallback: {done} songs added from YouTube/SoundCloud search")
    write_playlists(src)

# ---------------------------------------------------------------- review decisions (Echolot)
def apply_review(own_lock):
    """Apply the new decisions of Echolot's review page (config/review.yml, each once; see library.review_apply).
    The file work is done at once (under the library lock). attempts.json may only change under the Soulseek lock
    (a running sync writes it at its end): without it, e.g. in the playlists job during a long upgrade, 'search
    again' / 'found' are queued in state/review-retry.json for the next Soulseek job."""
    with (STATE / "review.lock").open("w") as rl:
        fcntl.flock(rl, fcntl.LOCK_EX)                  # one applier at a time (playlists job and Soulseek jobs)
        try: items = (yaml.safe_load(REVIEW_FILE.read_text(encoding="utf-8")) or {}).get("decisions") or []
        except (OSError, yaml.YAMLError, AttributeError): items = []
        done, queue = read_json(REVIEW_DONE, {}), read_json(REVIEW_RETRY, {})
        todo = [d for d in items if isinstance(d, dict) and d.get("id") and str(d["id"]) not in done]
        for d in todo:
            try: result, retry = library.review_apply(d)
            except Exception as e: result, retry = f"failed: {e}", False
            song = library.norm_key(d.get("song"))
            if retry and song:
                queue[song] = {"n": max(int(d.get("tries") or 0), 2), "artist": d.get("artist"), "title": d.get("title")}
            if d.get("decision") == "accept" and result.startswith(("new", "upgrade", "duplicate", "linked")):
                if song: queue[song] = None             # found: no more searches
                dest = TRACKS / result.split(" ", 1)[1] if " " in result else None
                if dest and dest.is_file() and result.startswith(("new", "upgrade")):
                    art = [str(dest), song.replace("spotify:", "spotify:track:")] if song.startswith("spotify:") else \
                          [str(dest), "search", "", d.get("artist") or "", d.get("title") or ""]
                    subprocess.run([sys.executable, str(SCRIPTS / "artwork.py"), *art])
            done[str(d["id"])] = {"at": datetime.datetime.now().isoformat(timespec="seconds"), "result": result}
            log(f"review: {d.get('decision')} {d.get('artist')} - {d.get('title')}: {result}")
        if todo: write_json(REVIEW_DONE, done)
        if own_lock and queue:
            attempts = read_json(ATTEMPTS, {})
            for song, v in queue.items():
                if v is None: attempts.pop(song, None); continue
                a = attempts.setdefault(song, {"n": 0})
                a.update(n=max(a.get("n", 0), v["n"]), last=0, artist=v["artist"], title=v["title"])
            write_json(ATTEMPTS, attempts); queue = {}
        write_json(REVIEW_RETRY, queue)

# ---------------------------------------------------------------- availability probe
PROBE_LIST, PROBE_LOG = CONFIG / "probe.csv", CONFIG / "logs" / "probe.jsonl"

def run_probe():
    """Search (never download) each song of probe.csv; log users, users with lossless, files per song.
    Counts are after the same filters as real downloads (sockseek.conf: artist in path, title in name)."""
    if not PROBE_LIST.exists(): log("probe: no /config/probe.csv, nothing to do"); return
    with PROBE_LIST.open(newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("Artist") and r.get("Title")]
    csvp = STATE / "sockseek-probe.csv"
    with csvp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["Artist", "Title", "Length"])
        for r in rows: w.writerow([r["Artist"], r["Title"], r.get("Length") or 0])
    started = datetime.datetime.now().isoformat(timespec="seconds")
    r = subprocess.run(sockseek_cmd(str(csvp), ["--no-skip-existing", "--search-timeout", "8000", "--print", "results"]),
                       text=True, capture_output=True)
    found, cur = {}, None
    for line in r.stdout.splitlines():
        m = re.match(r"^Results for (.+?)(?: \(\d+s\))?:$", line.strip())
        if m: cur = found.setdefault(m.group(1), []); continue
        m = re.match(r"^\[[^\]]*\] ([^\\]+)\\.*\.(\w+)$", line.strip())
        if m and cur is not None: cur.append((m.group(1), m.group(2).lower()))
    if not found: log(f"probe: no results parsed (sockseek exit {r.returncode}), nothing logged"); return
    with PROBE_LOG.open("a", encoding="utf-8") as out:
        for row in rows:
            hits = found.get(f"{row['Artist']} - {row['Title']}", [])
            users = {u for u, _ in hits}; lossless = {u for u, e in hits if e in ("flac", "wav", "aiff")}
            out.write(json.dumps({"ts": started, "artist": row["Artist"], "title": row["Title"], "kind": row.get("Kind") or "",
                                  "users": len(users), "lossless_users": len(lossless), "files": len(hits)}, ensure_ascii=False) + "\n")
    log(f"probe: {len(rows)} songs searched, " + ", ".join(f"{k}: {len({u for u, _ in v})}" for k, v in found.items()))

# ---------------------------------------------------------------- playlists, tags, status
def write_playlists(src, cat=None):
    """Every list becomes one .m3u in list order, pointing at the best library copy of each song."""
    cat = cat or library.Catalog()
    def best(it):
        h = cat.song(it)
        return str(h[0].path) if h else None
    def playlist(name, paths):
        """The list's playlist, or with `playlist: false` none (an earlier file and cover are removed)."""
        if show_playlist(src, name): return write_m3u(name, paths)
        for f in [PLAYLISTS / f"{clean_name(name)}.{e}" for e in ("m3u", "jpg", "png")]:
            if f.exists(): f.unlink(); log(f"playlist '{name}': switched off (playlist: false), {f.name} removed")
    def removed_list(name, gone):
        """'<list> - removed': songs that left the list (unliked, deleted at the source) but are in the library,
        most recently gone first. Written once there is something in it (removed_playlists: false turns it off)."""
        if not show_playlist(src, name) or src.get("removed_playlists", True) is False: return
        key, paths = f"{name} - removed", [p for p in gone if p]
        if not paths and not (PLAYLISTS / f"{clean_name(key)}.m3u").exists(): return
        title = read_json(PL_META, {}).get(name, {}).get("title") or name
        set_playlist_meta(src, key, f"{title} – removed", None)
        write_m3u(key, paths)
    for name, items in load_spotify_lists(src, fetch=False):
        playlist(name, [best(it) for it in items])
        current = {it["id"] for it in items}
        hist = read_json(STATE / f"spotify-{slug(name)}-history.json", {})
        gone = sorted((h for i, h in hist.items() if i not in current and h.get("title")), key=lambda h: h.get("last_seen", ""), reverse=True)
        removed_list(name, [best(h) for h in gone])
    state = read_json(SC_STATE, {})
    for name, items in sc_items(src):
        playlist(name, [best(it) or (str(p) if it["stem"] and (p := resolve(it["stem"])) else None) for it in items])
        current = {it["id"] for it in items}
        hist = read_json(STATE / f"soundcloud-{slug(name)}-history.json", {})
        gone = [i for i, h in sorted(hist.items(), key=lambda kv: kv[1].get("last_seen", ""), reverse=True) if i not in current and i in state]
        removed_list(name, [best({"artist": state[i]["artist"], "title": state[i]["title"], "length": float(state[i]["duration"] or 0)})
                            or (str(p) if state[i].get("stem") and (p := resolve(state[i]["stem"])) else None) for i in gone])

def fill_albums(src):
    """Soulseek uploads often carry no album tag (Navidrome shows '[Unknown Album]'): fill only empty album tags
    from the Spotify metadata (singles are named after the track there). Existing album tags stay untouched."""
    from mutagen import File as MFile
    cat = library.Catalog(); n = 0
    for _, items in load_spotify_lists(src, fetch=False):
        for it in items:
            h = cat.song(it)
            if not h: continue
            try:
                a = MFile(str(h[0].path), easy=True)
                if a is None or (a.tags and (a.tags.get("album") or [""])[0].strip()): continue
                if a.tags is None: a.add_tags()
                a["album"] = it.get("album") or it["title"]; a.save(); n += 1
            except Exception as e:
                log(f"albums: could not tag {h[0].path}: {e}")
    log(f"albums: filled {n} missing album tags")

# ---------------------------------------------------------------- status (terminal overview)
def _dwidth(t):
    """Terminal columns of t: wide/emoji 2, combining marks and zero-width joiners 0, VS16 makes the previous char wide."""
    import unicodedata as u
    w, prev = 0, 0
    for c in t:
        if c == "\ufe0f": w += 2 - prev; prev = 2; continue
        cw = 0 if u.combining(c) or c in "\u200d\u200b" else 2 if u.east_asian_width(c) in "WF" else 1
        w += cw; prev = cw if cw else prev
    return w

def _fit(t, w):
    """Pad or cut text to w terminal columns (emoji and CJK count double)."""
    out = ""
    for c in t:
        if _dwidth(out + c) > w - (1 if _dwidth(t) > w else 0): out += "…"; break
        out += c
    return out + " " * (w - _dwidth(out))

def status(src):
    tty = sys.stdout.isatty()
    dim, bold, off = ("\033[2m", "\033[1m", "\033[0m") if tty else ("", "", "")
    green, amber = ("\033[32m", "\033[33m") if tty else ("", "")
    W = 78
    bar = lambda f, n=20: "█" * round(f * n) + dim + "░" * (n - round(f * n)) + off
    pct = lambda a, b: f"{100 * a / b:3.0f} %" if b else "  – "
    rule = lambda title: print(f"\n {bold}{title}{off} {dim}" + "─" * (W - _dwidth(title) - 2) + off)
    cat, meta, attempts = library.Catalog(), read_json(PL_META, {}), read_json(ATTEMPTS, {})
    size = sum(e.path.stat().st_size for e in cat.entries if e.path.exists()) / 1e9
    genuine = sum(e.genuine for e in cat.entries)
    print(f"\n {bold}MUSIC LIBRARY{off}" + " " * (W - 29) + f"{dim}{datetime.datetime.now():%Y-%m-%d %H:%M}{off}")
    print(f" {dim}" + "─" * (W - 1) + off)
    print(f" {len(cat.entries):,} songs   ·   {size:.1f} GB   ·   {pct(genuine, len(cat.entries)).strip()} genuine lossless")

    rule("QUALITY")
    tiers = [("Lossless (FLAC)", lambda e: e.genuine), ("FLAC made from MP3", lambda e: e.fake),
             ("Lossy ≥ 256 kbps", lambda e: not e.genuine and not e.fake and e.kbps >= 250),
             ("Lossy 160–250 kbps", lambda e: not e.genuine and not e.fake and 150 <= e.kbps < 250),
             ("Lossy < 160 kbps", lambda e: not e.genuine and not e.fake and e.kbps < 150)]
    for name, test in tiers:
        n = sum(1 for e in cat.entries if test(e))
        print(f"   {_fit(name, 22)} {n:6,}  {bar(n / len(cat.entries) if cat.entries else 0, 30)}  {pct(n, len(cat.entries))}")

    rule("LISTS")
    print(f"   {dim}{_fit('', 32)} {'songs':>6} {'have':>6} {'miss':>5}  {'':20} {'':>5}{off}")
    rows = []
    for name, items in load_spotify_lists(src, fetch=False):
        have = sum(1 for it in items if cat.song(it))
        rows.append((meta.get(name, {}).get("title") or name, "spotify", len(items), have))
    for name, items in sc_items(src):
        have = sum(1 for it in items if cat.song(it) or (it["stem"] and resolve(it["stem"])))
        rows.append((meta.get(name, {}).get("title") or name, "soundcloud", len(items), have))
    for title, kind, total, have in rows:
        f = have / total if total else 1
        color = green if f >= 0.95 else amber if f < 0.8 else ""
        mark = f"{dim}♫{off}" if kind == "spotify" else f"{dim}☁{off}"
        print(f" {mark} {_fit(title, 32)} {total:6,} {have:6,} {total - have:5,}  {color}{bar(f)}{off} {pct(have, total)}")
    tot, got = sum(r[2] for r in rows), sum(r[3] for r in rows)
    print(f"   {dim}{_fit(f'{len(rows)} lists', 32)} {tot:6,} {got:6,} {tot - got:5,}{off}")

    rule("ACTIVITY")
    for job, lock in (("Soulseek (sync / upgrade / fallback)", "music-sync.lock"), ("SoundCloud", "soundcloud.lock")):
        try:
            with open(STATE / lock, "w") as lk:
                fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB); running = False
        except BlockingIOError:
            running = True
        print(f"   {_fit(job, 38)} {green + '● running' + off if running else dim + '○ idle' + off}")
    since = (datetime.datetime.now() - datetime.timedelta(hours=24)).isoformat(timespec="seconds")
    ev = [json.loads(l) for l in open(library.EVENTS, encoding="utf-8")] if library.EVENTS.exists() else []
    new = [e for e in ev if e.get("ts", "") >= since and e.get("action") in ("new", "upgrade")]
    wrong = sum(1 for e in ev if e.get("ts", "") >= since and e.get("action") in ("wrong-song", "mismatch"))
    gb = sum(e.get("bytes", 0) for e in new) / 1e9
    print(f"   {_fit('Last 24 h', 38)} {len(new):,} songs added ({gb:.1f} GB), {wrong} wrong downloads rejected")
    waiting = sum(1 for a in attempts.values() if a.get("n", 0) >= 1)
    print(f"   {_fit('Not found yet', 38)} {waiting:,} songs, retried after 6 h … weekly; YouTube after 2 tries")
    print(f"   {dim}{_fit('Schedule', 38)} sync :20 :50 · SoundCloud :05 :35 · playlists 10 min{off}\n")

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "sync"
    args = sys.argv[2:]
    limit = next((int(a) for a in args if a.isdigit()), None)
    dry = "--dry-run" in args
    src = yaml.safe_load((CONFIG / "sources.yml").read_text()) or {}
    STATE.mkdir(parents=True, exist_ok=True)
    if mode == "status": return status(src)
    if mode == "retag-soundcloud": return retag_soundcloud(read_json(SC_STATE, {}))
    if mode == "fill-albums": return fill_albums(src)
    if mode == "playlists":
        subprocess.run([sys.executable, str(SCRIPTS / "library.py"), "purge"])
        with (STATE / "music-sync.lock").open("w") as lk:     # review decisions; attempts only without a Soulseek job
            try: fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB); own = True
            except BlockingIOError: own = False
            apply_review(own)
        return write_playlists(src)
    if dry and mode in ("sync", "sweep", "upgrade"):
        if mode == "upgrade": return run_upgrade(src, dry=True, force="--all" in args)
        return run_spotify(src, limit, dry=True, sweep=mode == "sweep")
    # Soulseek work (sync, upgrade, fallback) shares one lock; SoundCloud downloads have their own
    lock = (STATE / ("soundcloud.lock" if mode == "soundcloud" else "music-sync.lock")).open("w")
    # tick.py starts a job only while the lock is free, but both containers tick at the same second: a job that
    # lost that race waits (up to 30 min) instead of losing its turn
    for waited in range(181):
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB); break
        except BlockingIOError:
            if waited == 180: log("another music-sync run is still active after 30 min, exiting"); return
            if waited == 0: log("another music-sync run is active, waiting for it")
            time.sleep(10)
    log(f"=== music-sync {mode} start")
    if mode != "soundcloud": apply_review(True)
    if mode in ("sync", "spotify"):
        run_spotify(src, limit)
    elif mode == "sweep":
        run_spotify(src, limit, sweep=True)
    elif mode == "upgrade":
        run_upgrade(src, force="--all" in args)
    elif mode == "soundcloud":
        run_soundcloud(src)
    elif mode == "fallback":
        run_fallback(src, force="--all" in args)
    elif mode == "probe":
        run_probe()
    elif mode == "review":
        pass
    else:
        sys.exit(f"unknown mode {mode}")
    if mode in ("sync", "spotify", "sweep", "upgrade", "fallback"): fill_albums(src)
    log(f"=== music-sync {mode} done")

if __name__ == "__main__":
    main()
