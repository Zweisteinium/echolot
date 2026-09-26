#!/usr/bin/env python3
"""Embed cover art into an audio file and drop artist.jpg into the artist folder.
Usage: artwork.py <file> [spotify:track:ID | "search" | ""] [cover.jpg] [artist] [title]
- With a Spotify URI: fetch the album cover (640px) and artist image from the Spotify API
  (client-credentials token from SPOTIFY_ID/SPOTIFY_SECRET, cached in /config/state).
- With a local cover file (SoundCloud thumbnails): embed that instead.
Never overwrites an existing embedded picture. Fails soft: any error only prints a warning."""
import base64, json, os, pathlib, sys, time, urllib.parse, urllib.request
from mutagen import File as MFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, APIC, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

STATE = pathlib.Path("/config/state"); TOKEN_FILE = STATE / "spotify-cc-token.json"
UA = "music-sync/1.0"

def warn(msg): print(f"artwork: {msg}", file=sys.stderr, flush=True)

def http(url, headers=None, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

def spotify_token():
    try:
        t = json.loads(TOKEN_FILE.read_text())
        if t.get("expires_at", 0) > time.time() + 60: return t["access_token"]
    except Exception: pass
    cid, sec = os.environ.get("SPOTIFY_ID"), os.environ.get("SPOTIFY_SECRET")
    if not cid or not sec: return None
    body = http("https://accounts.spotify.com/api/token", {"Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode(), "Content-Type": "application/x-www-form-urlencoded"}, b"grant_type=client_credentials")
    t = json.loads(body); t["expires_at"] = time.time() + t.get("expires_in", 3600)
    STATE.mkdir(parents=True, exist_ok=True); TOKEN_FILE.write_text(json.dumps(t))
    return t["access_token"]

def spotify_get(path):
    tok = spotify_token()
    if not tok: return None
    return json.loads(http("https://api.spotify.com/v1/" + path, {"Authorization": "Bearer " + tok}))

def has_picture(path):
    a = MFile(path)
    if a is None: return True
    if isinstance(a, FLAC): return bool(a.pictures)
    if isinstance(a, MP4): return bool(a.tags and a.tags.get("covr"))
    if isinstance(a, (OggOpus, OggVorbis)): return bool(a.get("metadata_block_picture"))
    try: return any(k.startswith("APIC") for k in ID3(path).keys())
    except ID3NoHeaderError: return False

def embed(path, jpg):
    a = MFile(path)
    if isinstance(a, FLAC):
        p = Picture(); p.type = 3; p.mime = "image/jpeg"; p.data = jpg; a.add_picture(p); a.save()
    elif isinstance(a, MP4):
        a.tags = a.tags or a.add_tags() or a.tags; a.tags["covr"] = [MP4Cover(jpg, MP4Cover.FORMAT_JPEG)]; a.save()
    elif isinstance(a, (OggOpus, OggVorbis)):
        p = Picture(); p.type = 3; p.mime = "image/jpeg"; p.data = jpg
        a["metadata_block_picture"] = [base64.b64encode(p.write()).decode()]; a.save()
    else:
        try: t = ID3(path)
        except ID3NoHeaderError: t = ID3()
        t.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpg)); t.save(path)

def main():
    path = sys.argv[1]; uri = sys.argv[2] if len(sys.argv) > 2 else ""; local = sys.argv[3] if len(sys.argv) > 3 else ""
    if not os.path.isfile(path): return
    if local and os.path.isfile(local):
        if not has_picture(path): embed(path, open(local, "rb").read()); print(f"artwork: embedded local cover into {path}")
        return
    tr = None
    try:
        if uri.startswith("spotify:track:"):
            tr = spotify_get(f"tracks/{uri.split(':')[-1]}")
        elif uri == "search" and len(sys.argv) > 5:   # fallback downloads: find the track by artist + title
            q = urllib.parse.quote(f"track:{sys.argv[5]} artist:{sys.argv[4]}")
            res = spotify_get(f"search?q={q}&type=track&limit=1") or {}
            items = ((res.get("tracks") or {}).get("items")) or []
            tr = items[0] if items else None
        if not tr: return
        if not tr: return
        imgs = (tr.get("album") or {}).get("images") or []
        if imgs and not has_picture(path):
            embed(path, http(imgs[0]["url"])); print(f"artwork: embedded Spotify cover into {path}")
        artist = (tr.get("artists") or [{}])[0]
        aj = pathlib.Path(path).parent / "artist.jpg"
        if artist.get("id") and not aj.exists():
            ar = spotify_get(f"artists/{artist['id']}")
            aimg = (ar or {}).get("images") or []
            if aimg: aj.write_bytes(http(aimg[0]["url"])); print(f"artwork: wrote {aj}")
    except Exception as e:
        warn(f"{path}: {e}")

if __name__ == "__main__":
    main()
