# Echolot

Self-hosted music library manager. Echolot follows your playlists and likes on Spotify and
SoundCloud, keeps a clean local library that matches them, knows the quality of every file, and
shows what is missing. The library is plain files, ready for Navidrome or any other Subsonic
server.

The name is German for *sonar*: ping every source, keep only what echoes back clearly.

## Intended use

Echolot organises, verifies and deduplicates audio files and keeps them in step with your lists.
Which sources you connect, and what you obtain, keep and share through them, is up to you: use it
for music you are entitled to (purchases, free and Creative Commons releases, artist-provided
downloads, your own uploads) and respect the terms of the services you connect and the law where
you live. Echolot does not circumvent copy protection.

## Features

- **Lists as the source of truth.** Spotify Liked Songs and playlists, SoundCloud likes and sets,
  picked from your accounts on the Sources page. Every song is kept once, however many lists
  contain it; a list can also become a playlist in the music server, with its cover.
- **Strict matching.** A file only counts as a song when artist, title and length agree, with
  explicit rules for noise ("(Original Mix)", "[HAK003]"), versions (remix, live, VIP),
  featured artists, DJ-mix cuts and scene file names. Search results are judged before they are
  downloaded; wrong files are rejected, near misses land on a review page.
- **Quality tracking.** Lossless, lossy by bitrate, and FLACs made from lossy files (spectrum
  check). Better copies replace worse ones automatically; replaced files are kept for 30 days.
- **One web app.** Setup of the accounts, the lists to follow, completeness and quality per list,
  missing songs with reasons, a review queue with a player, activity, jobs, settings.
- **Metrics.** Hourly snapshots, a JSON API and a Prometheus endpoint for Grafana.

## Architecture

```
 browser ──▶ Echolot (web + worker, SQLite)          home network
               │  Spotify API, SoundCloud (yt-dlp), YouTube/SoundCloud search (yt-dlp)
               │  checks and files every download: <music>/tracks, playlists/
               │
               │ HTTP (job API, port 5031, not published)
               ▼
             Sockseek daemon ──▶ Soulseek            in the VPN container's network namespace
                                                     (gluetun, optional)
 Navidrome ◀── <music>/tracks, <music>/playlists (read-only)
```

| Component | Image | Role |
|---|---|---|
| `echolot` | built from this repo | web interface, API, jobs; the only writer of the library |
| `sockseek-daemon` | [Sockseek](https://github.com/fiso64/sockseek)'s official image | Soulseek client: searches and transfers for Echolot ([`deploy/sockseek-daemon`](deploy/sockseek-daemon)) |
| `navidrome` | `deluan/navidrome` | music server for the library and playlists |
| `slskd` | `slskd/slskd` | optional Soulseek client with a web UI, independent of Echolot |

## A song's way into the library

1. **Lists.** Echolot reads the lists you follow through Spotify's Web API and, for SoundCloud,
   through yt-dlp, and remembers every song a list ever had.
2. **Missing?** A song is in the library when a file matches it (see [Matching](#matching)).
3. **Acquire.**
   - *Spotify songs* are searched on Soulseek through the Sockseek daemon. Sockseek filters and
     ranks the results (FLAC first); Echolot judges each by its path and length and downloads the
     best one; if it fails, is rejected or makes no progress (queued at the peer), the next one.
     A song that is not found is retried after 3 h, 6 h, 12 h, then daily; a sweep searches all
     missing songs at fixed times. After two misses the search is loosened (see below), and a
     YouTube/SoundCloud search may try too.
   - *SoundCloud songs* are downloaded from SoundCloud with yt-dlp, as the original file where the
     uploader offers it, otherwise the stream. Tracks SoundCloud does not provide are marked as
     unavailable and left to the search.
4. **Check** every download: the codec must match the file type, WAV/AIFF/ALAC become FLAC,
   hi-res becomes 44.1/48 kHz 24 bit, and a spectrum check finds FLACs made from lossy files.
   Then [verification](#verification) decides whether it is the song.
5. **File.** Echolot never overwrites a file:
   - a song the library already has is discarded, unless the download is genuine lossless and the
     library copy is not; then it takes over, and the old file goes to `inbox/replaced/<date>/`
     for 30 days;
   - layout: `tracks/<Artist>/<Artist> - <Title>.<ext>`, one folder per artist, whatever the
     spelling.
6. **Upgrade.** Twice a day songs that are not genuine lossless are searched again, FLAC only: at
   most 150 per run, longest waiting first; each song waits 12 h, 1 d, 2 d, then 3 d.
7. **Playlists.** One `.m3u` per list shown as a playlist, in list order, with the list's cover.
   Songs that leave a list stay in the library and move to "<list> – removed".

## Matching

*Same song* (library lookup):
- the same **artist**, ignoring case, accents and punctuation; the first of several artists
  counts too, and so does any other artist of the song (a collaboration listed twice with the
  artists swapped is one song);
- the same **title** once noise is removed: "(Original Mix)", "(feat. X)", "[HAK003]",
  "- 2011 Remaster", "(Official Video)" and similar;
- a **length** within max(10 s, 4 %).

Version words (remix, edit, extended, VIP, live, II, Pt. 2) keep songs apart, and so do different
featured artists ("Swervin (feat. A)" and "Swervin (feat. B)"). A DJ-mix cut ("Song - Mixed",
from compilation mixes) is the released song at any length. Two artist names for the same
recording can be linked on the review page.

## Verification

**Before the download** a search result is judged by its path and length: it is skipped when the
artist is missing (unless the search is loosened), the file name names another version or lacks
the one asked for ("Paradies" for "Paradies - X Remix"), the length is another song's, or the
file was marked wrong in review. Results naming the title come first.

**After the download** `identify` decides. The artist must appear in the tags or the source path,
always. Then:

| Result | Rule | What happens |
|---|---|---|
| **exact** | the title tag or file name gives exactly the title (noise removed) | filed |
| **probable** | same core title (the part before any bracket, " - ", "\|" or "feat."), same version words, no named variant the request lacks ("(Hard Trance Mix)"), no other featured artist, length within 3 s (6 s for videos) | filed and listed on the review page; FLAC upgrades keep it for review instead |
| **none** | anything else | rejected; near misses (right artist, similar length) are kept in `inbox/review/<date>/` for 30 days |

File names are read with and without track numbers ("07 ", "1-04 ", "CD-01 - "), artist
prefixes, "Album - 07 - Title" and scene-style names (`02-artist-title-grp`). A file name that
names another version overrules plain tags.

**Loosened search** (the checks stay the same): a song not found twice is searched without
feat. credits, 'From "Film"' and plain suffixes (" - Radio Edit"), with the first artist only,
and a search without results is repeated with the title alone and the artist alone. After four
misses the artist is no longer required in the Soulseek path; a result then needs the title in
its name, and its tags must name the artist.

## Echolot

| Page | What it shows or does |
|---|---|
| Overview | library size, lossless share, quality of all songs, every list with its quality and missing songs, the jobs (run now, stop, pause) |
| Sources | your Spotify playlists and SoundCloud likes and sets as cards: off, songs, or songs + playlist; other lists by link |
| Missing | songs not in the library yet, with their lists and why (not found yet, unavailable at the source) |
| Review | songs filed on a probable match (right / wrong) and kept near misses (accept / discard), with a player; decisions can be reverted for 2 minutes |
| Activity | everything filed, upgraded or rejected, with reasons |
| Availability | how many Soulseek users have a set of probe songs, by hour |
| Accounts | connecting Spotify, SoundCloud and the Soulseek account, step by step |
| Settings | when each job runs, Soulseek limits, login length and public metrics, password, API tokens, the configuration as one file (`echolot.yml`) |

- **Login:** every page and API call needs a login (browser) or an API token (scripts,
  `Authorization: Bearer <token>`); only `/healthz` and, by default, `/metrics` are open. You log
  in as `admin`: until its password is set, opening Echolot asks for it (do that right after the
  first start, before Echolot is reachable from outside, or set `ECHOLOT_ADMIN_PASSWORD`).
- **Jobs:** sync (Spotify lists, due songs), sweep, FLAC upgrade and availability probe share the
  Soulseek connection; SoundCloud and the search fallback share the home IP; the library job
  (rescan, review decisions, playlists, snapshots, cleanup) runs every 5 minutes. A job due while
  another holds its resource waits and keeps its turn.
- **Configuration** lives in the SQLite database. `echolot.yml` holds all of it (lists, schedule,
  settings; no secrets) as one file for backups or another install: Settings,
  `echolot config export|import`, `GET/PUT /api/config`.
- **Secrets** (the accounts' credentials) are stored encrypted with `ECHOLOT_SECRET_KEY` and never
  shown again.

### Metrics and API

Every hour the library job stores a snapshot in the `snapshots` table (hourly for 90 days, then
one per day):

| Metric | Label | What |
|---|---|---|
| `library_files`, `library_bytes` | | library size |
| `library_files_by_quality` | quality | lossless, fake, lossy-high / -mid / -low |
| `library_files_by_format`, `library_bytes_by_format` | format | flac, mp3, m4a, opus, ... |
| `songs_wanted`, `songs_in_library`, `songs_missing` | service | spotify, soundcloud |
| `songs_missing_by_reason` | reason | not_found, unavailable, waiting |
| `songs_not_found_by_tries` | tries | 1, 2-3, 4+ |
| `songs_by_quality`, `songs_by_format` | quality / format | wanted songs by their best copy |
| `list_songs`, `list_in_library`, `list_lossless` | list | per list |

| Endpoint | Returns |
|---|---|
| `GET /metrics` | current values in the Prometheus format, plus `echolot_events_total{action,source}` and `echolot_probe_users{song,kind,lossless}` |
| `GET /api/stats` | the newest snapshot |
| `GET /api/stats/metrics` | metric names, labels and meaning |
| `GET /api/stats/history?metric=songs_missing[&key=spotify][&since=...][&until=...]` | one metric over time: `[{ts, time, key, value}]` |
| `GET /api/stats/downloads?days=30` | events per day, action, source and format |
| `GET /api/stats/availability?days=30` | probe results per run and song |
| `GET /api/jobs`, `POST /jobs/<name>/run`, `/cancel`, `/jobs/pause` | the jobs, starting and stopping them |
| `GET /api/config`, `PUT /api/config[?dry_run=true]` | the whole configuration (as `echolot.yml`); an import answers what changes as a diff |
| `GET /api/docs` | OpenAPI documentation of all endpoints |

Grafana: let Prometheus scrape `/metrics`, or read `data/echolot.db` with the SQLite data source
(`SELECT ts AS time, key AS metric, value FROM snapshots WHERE metric = 'library_files_by_format'
ORDER BY ts`), or use the Infinity data source on the JSON endpoints.

## Deploy

### Requirements

- a Docker host with Docker Compose;
- one filesystem for the music: files are hard-linked from `inbox/` into `tracks/`;
- for Spotify: a developer app of your own; its owner needs Premium (Spotify's rule since March 2026),
  the connected account does not (list it under the app's User Management if it is not the owner);
  for SoundCloud: your account;
- for Soulseek: an account for the Sockseek daemon (a second one if you also run slskd: an
  account can be logged in only once).

All containers run as the same user (UID/GID 1000 below) and in the same time zone.

### Directory layout

```
<music>/tracks/      the library (Navidrome reads it)
<music>/playlists/   .m3u files and covers (relative paths: ../tracks/...)
<music>/inbox/       downloads in progress, review and replaced files (kept 30 days)

/opt/echolot/        Echolot: compose file, .env, data/ (SQLite)
/opt/sockseek/       the Sockseek daemon: compose file, src/ (its source), daemon/ (run.sh, login)
```

### Network

Peer-to-peer clients show your IP address to the peers they talk to, so running the Soulseek
side (the Sockseek daemon, and slskd if you use it) behind a VPN is a sensible default for
privacy. [gluetun](https://github.com/qdm12/gluetun) fits this stack well: the daemon joins its
network namespace with `network_mode: "container:gluetun"`, and Echolot joins gluetun's Docker
network to reach the daemon's API (not published on the host). If your VPN forwards ports for
incoming connections, [`deploy/host/sync-listen-port.sh`](deploy/host/sync-listen-port.sh)
hands the forwarded ports to slskd and the daemon whenever they change (run it from cron).

Echolot itself stays on the normal network: SoundCloud rate-limits VPN addresses, and video
sites often refuse them.

### The Sockseek daemon

```sh
mkdir -p /opt/sockseek/daemon && cd /opt/sockseek
git clone https://github.com/fiso64/sockseek src
docker build -t sockseek:upstream src                        # the official image
cp <this repo>/deploy/sockseek-daemon/run.sh daemon/
# add the service from deploy/sockseek-daemon/docker-compose.yml next to gluetun, then:
docker compose up -d sockseek-daemon
```

`run.sh` starts `sockseek daemon` on port 5031 and restarts it when its login (`daemon.conf`,
written by Echolot's Accounts page) or its listen port changes. It downloads only into
`<music>/inbox/soulseek`.

### slskd (optional)

A Soulseek client with a web UI, handy for searching by hand. It is independent of Echolot and
uses its own account.

```yaml
services:
  slskd:
    image: slskd/slskd
    network_mode: "container:gluetun"    # or a normal network
    volumes: [./slskd:/app, <music>:/music]
```

In `slskd.yml` set the download directories (`directories.downloads: /music/inbox/slskd`,
`directories.incomplete: /music/inbox/incomplete`) and, if you share anything, what and at which
limits (`shares`, `transfers`). An invalid config silently breaks search responses; check the log
after every change.

### Navidrome

```yaml
services:
  navidrome:
    image: deluan/navidrome
    environment:
      - ND_MUSICFOLDER=/music
      - ND_SCANSCHEDULE=@every 15m
      - ND_AUTOIMPORTPLAYLISTS=true
    volumes:
      - ./data:/data
      - <music>/tracks:/music/tracks:ro
      - <music>/playlists:/music/playlists:ro
```

A playlist's name is set when Navidrome first imports it; rename it later in Navidrome. A list you
stop following keeps its playlist in Navidrome until you delete it there.

### Echolot

```yaml
services:
  echolot:
    build: .                            # or image: echolot:local (docker build -t echolot:local .)
    user: "1000:1000"
    ports:
      - "192.168.1.10:8490:8490"        # LAN, or behind a reverse proxy with HTTPS
    env_file: .env                      # ECHOLOT_SECRET_KEY (chmod 600)
    environment:
      ECHOLOT_LIBRARY_DIR: /music/tracks
      ECHOLOT_DAEMON_DIR: /daemon
    volumes:
      - ./data:/data
      - <music>:/music
      - /opt/sockseek/daemon:/daemon    # Echolot writes the daemon's login here
    networks: [default, gluetun]        # the daemon's API: http://gluetun:5031
    restart: unless-stopped
networks:
  gluetun:
    name: gluetun_default               # gluetun's compose network
    external: true
```

| Variable | Default | Meaning |
|---|---|---|
| `ECHOLOT_DATA_DIR` | `data` | SQLite database (`/data` in the image) |
| `ECHOLOT_LIBRARY_DIR` | unset | the library (`<music>/tracks`; `inbox/` and `playlists/` beside it) |
| `ECHOLOT_DAEMON_DIR` | unset | where the Sockseek daemon's login (`daemon.conf`) is written |
| `ECHOLOT_SECRET_KEY` | unset | key for the stored secrets: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Unset: a key file `data/secret.key` is created (a copy of `data/` then holds key and secrets together) |
| `ECHOLOT_SECRET_KEY_FILE` | `data/secret.key` | the key file, when the key is not in the environment |
| `ECHOLOT_ADMIN_PASSWORD` | unset | the password of the `admin` account, set on a start when there is none yet (else: asked for on the first visit) |
| `ECHOLOT_WORKER` | `on` | `off`: run no jobs (a test copy) |
| `ECHOLOT_HOST`, `ECHOLOT_PORT` | `127.0.0.1`, `8490` | listen address (`0.0.0.0` in the image) and port |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | behind a reverse proxy: the address the proxy's requests come from (for a proxy in another container reaching the published port: the gateway of Echolot's Docker network). Echolot then trusts its `X-Forwarded-Proto`/`-For`: https addresses, the Spotify login's automatic return, Secure cookies, the real client IP for the login throttle |

Then open Echolot, set the admin password, and follow the Accounts page: it explains the Spotify
app (and the address to register for the login), the SoundCloud token, and the Soulseek account.
Pick the lists on the Sources page. Without HTTPS, Spotify's login ends on an address that does
not load: paste it back into Echolot (the page says where).

Forgotten password: `docker exec -it echolot echolot user passwd admin`.

## Operation

- **Pause** all jobs on the overview (Run now still works); single jobs: set them to `off` in
  Settings.
- **Logs:** `docker logs echolot` (every job and song), the Activity page for every filing and
  rejection, `docker logs sockseek-daemon` for Soulseek.
- **Limits:** Soulseek limits searches (about 34 per 220 s), so a large search takes a while;
  SoundCloud answers 429 after bursts (keep its job at 15 minutes or more); Spotify
  development-mode apps serve 5 accounts and can't read Spotify's editorial playlists. Spotify's
  rules also let it withhold other people's playlists (in September 2026 they were still
  readable); the Sources page marks any it withholds.
- **Rule changes:** `uv run python tools/rules_check.py` compares the working copy's rules with
  the deployed version on the real library and history; `tools/daemon_check.py` checks the
  daemon's API against a mock.
- **Back up** Echolot's `data/` (lists, schedule, settings, account, encrypted secrets;
  `echolot config export` for a readable copy), its `ECHOLOT_SECRET_KEY`, and the library.

## Develop

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest                  # tests (the audio checks need ffmpeg: they run in the image)
uv run ruff check && uv run ruff format
uv run echolot serve           # http://127.0.0.1:8490
docker compose up -d --build   # dev instance, see docker-compose.yml (runs no jobs)
```
