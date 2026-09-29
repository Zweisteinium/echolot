# Echolot

Self-hosted music library manager. Echolot follows your playlists and likes on Spotify and
SoundCloud, keeps a clean local library that matches them, knows the quality of every file, and
shows what is missing. The library is plain files, ready for Navidrome or any other Subsonic
server.

The name is German for *sonar*: ping every source, keep only what echoes back clearly.

> **Status:** the file handling is done by the music-sync pipeline in [`pipeline/`](pipeline/)
> (Python scripts around [Sockseek](https://github.com/fiso64/sockseek) and
> [yt-dlp](https://github.com/yt-dlp/yt-dlp)). Echolot is its dashboard and control panel:
> statistics, missing songs, review, activity, sources and schedules. The pipeline's logic moves
> into Echolot step by step.

## Intended use

Echolot organises, verifies and deduplicates audio files and keeps them in step with your lists.
Which sources you connect, and what you obtain, keep and share through them, is up to you: use it
for music you are entitled to (purchases, free and Creative Commons releases, artist-provided
downloads, your own uploads) and respect the terms of the services you connect and the law where
you live. Echolot does not circumvent copy protection.

## Features

- **Lists as the source of truth.** Spotify Liked Songs and playlists, SoundCloud likes and sets.
  Every song is kept once, however many lists contain it; every list becomes a playlist file with
  its cover.
- **Strict matching.** A file only counts as a song when artist, title and length agree, with
  explicit rules for noise ("(Original Mix)", "[HAK003]"), versions (remix, live, VIP),
  featured artists, DJ-mix cuts and scene file names. Wrong files are rejected, near misses land
  on a review page.
- **Quality tracking.** Lossless, lossy by bitrate, and FLACs made from lossy files (spectrum
  check). Better copies replace worse ones automatically; replaced files are kept for 30 days.
- **Dashboard.** Completeness and quality per list, missing songs with reasons, a review queue
  with a player, activity, and settings for sources and schedules.
- **Metrics.** Hourly snapshots, a JSON API and a Prometheus endpoint for Grafana.

## Architecture

```
 list sources                 acquisition                        library
 ─────────────                ───────────                        ───────
 Spotify API ─┐   ┌────────────────────────────────────┐
              ├──▶│ music-sync pipeline (2 containers) │──▶ inbox/ ──▶ checks ──▶ tracks/
 SoundCloud ──┘   │  sockseek: Soulseek (Sockseek)      │                 │        playlists/
                  │  sockseek-fallback: SoundCloud,     │                 │
                  │    search fallback (yt-dlp)         │                 ▼
                  └────────────────────────────────────┘        Navidrome ──▶ any Subsonic client
                  slskd (optional Soulseek client with web UI)

 Echolot: reads the pipeline's state and logs, scans the library, edits sources.yml,
          schedule.yml and review.yml. Web UI and API on port 8490.
```

| Component | Image | Role |
|---|---|---|
| `sockseek` | built from [`pipeline/`](pipeline/) on the official Sockseek image | Spotify lists: searches and downloads via Soulseek, FLAC upgrades, availability probe |
| `sockseek-fallback` | same image | SoundCloud lists (yt-dlp), search fallback for songs not found otherwise |
| `slskd` | `slskd/slskd` | optional Soulseek client with a web UI, independent of the pipeline |
| `navidrome` | `deluan/navidrome` | music server for the library and playlists |
| `echolot` | built from this repo | dashboard, control panel, metrics |

Both pipeline containers run the same scripts with the same `config/` directory; an environment
variable (`ROLE`) decides which jobs each one runs. Splitting them lets the two kinds of traffic
use different networks (see [Network](#network)).

## A song's way into the library

1. **Lists.** `sources.yml` names the lists. The pipeline reads them through the Spotify Web API
   and SoundCloud's web API and remembers every song it has seen.
2. **Missing?** `library.py` checks whether the library already has the song (see
   [Matching](#matching)).
3. **Acquire.**
   - *Spotify songs:* Sockseek searches Soulseek and prefers FLAC. A song that is not found is
     retried after 3 h, 6 h, 12 h, then daily; a sweep searches all missing songs at fixed times.
     After two misses the search is loosened (see below) and the search fallback may try too.
   - *SoundCloud songs:* downloaded from SoundCloud with yt-dlp, as the original file where the
     uploader offers it, otherwise the stream. Tracks SoundCloud does not provide are marked as
     unavailable.
4. **Verify** every download (see [Verification](#verification)) and prepare it: codec check,
   WAV/AIFF/ALAC to FLAC, hi-res to 44.1/48 kHz 24 bit, spectrum check.
5. **File.** `library.py` is the only code that writes into `tracks/`, and it never overwrites:
   - a song the library already has is discarded, unless the download is genuine lossless and the
     library copy is not; then it takes over, and the old file goes to `inbox/replaced/<date>/`
     for 30 days;
   - layout: `tracks/<Artist>/<Artist> - <Title>.<ext>`, one folder per artist, whatever the
     spelling.
6. **Upgrade.** Twice a day songs that are not genuine lossless are searched again, FLAC only: at
   most 150 per run, longest waiting first; each song waits 12 h, 1 d, 2 d, then 3 d.
7. **Playlists.** One `.m3u` per list, in list order, with the list's cover, rebuilt every
   10 minutes. Songs that leave a list stay in the library and move to "<list> – removed".

## Matching

*Same song* (library lookup, `Catalog.song`):
- the same **artist**, ignoring case, accents and punctuation; the first of several artists
  counts too, and so does any other artist of the song (a collaboration listed twice with the
  artists swapped is one song);
- the same **title** once noise is removed: "(Original Mix)", "(feat. X)", "[HAK003]",
  "- 2011 Remaster", "(Official Video)" and similar;
- a **length** within max(10 s, 4 %).

Version words (remix, edit, extended, VIP, live, II, Pt. 2) keep songs apart, and so do different
featured artists ("Swervin (feat. A)" and "Swervin (feat. B)"). A DJ-mix cut ("Song - Mixed",
from compilation mixes) is the released song at any length. Two artist names for the same
recording can be linked on the review page (`state/song-links.json`).

## Verification

`identify` decides whether a download is the song. The artist must appear in its tags or source
path, always. Then:

| Result | Rule | What happens |
|---|---|---|
| **exact** | the title tag or file name gives exactly the title (noise removed) | filed |
| **probable** | same core title (the part before any bracket, " - ", "\|" or "feat."), same version words, no named variant the request lacks ("(Hard Trance Mix)"), no other featured artist, length within 3 s (6 s for the fallback) | filed and listed on the review page; FLAC upgrades keep it for review instead |
| **none** | anything else | rejected; near misses (right artist, similar length) are kept in `inbox/review/<date>/` for 30 days |

File names are read with and without track numbers ("07 ", "1-04 ", "CD-01 - "), artist
prefixes, "Album - 07 - Title" and scene-style names (`02-artist-title-grp`). A file name that
names another version overrules plain tags.

**Loosened search** (the checks stay the same): a song not found twice is searched without
feat. credits, 'From "Film"' and plain suffixes (" - Radio Edit"), with the first artist only,
and a search without results is repeated with the title alone and the artist alone. After four
misses the artist is no longer required in the Soulseek path (the checks still require it).

## Echolot

| Page | What it shows or does |
|---|---|
| Overview | library size, lossless share, quality of all songs, every list with its quality and missing songs, job activity |
| Missing | songs not in the library yet, with their lists and why (not found yet, unavailable at the source) |
| Review | songs filed on a probable match (right / wrong) and kept near misses (accept / discard), with a player; decisions can be reverted until applied |
| Activity | everything filed, upgraded or rejected, with reasons |
| Availability | how many Soulseek users have a set of probe songs, by hour |
| Sources | add, rename, hide or remove lists and options, or edit `sources.yml` directly (with undo) |
| Settings | when each pipeline job runs (`schedule.yml`) and how often Echolot refreshes |

- **Refresh:** every 5 minutes (adjustable) Echolot imports the pipeline's state into its SQLite
  database, rescans the library and matches every song to its best file.
- **Writes:** in the pipeline directory only `sources.yml`, `schedule.yml` and `review.yml`, each
  validated and written atomically; earlier versions of the first two are kept for undo. The
  pipeline applies review decisions once (`state/review-done.json`).
- **Never touches** the library, or the pipeline's state, logs and scripts (mounted read-only).

### Metrics and API

Every hour the refresh stores a snapshot in the `snapshots` table (hourly for 90 days, then one
per day):

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
| `GET /api/docs` | OpenAPI documentation of all endpoints |

Grafana: let Prometheus scrape `/metrics`, or read `data/echolot.db` with the SQLite data source
(`SELECT ts AS time, key AS metric, value FROM snapshots WHERE metric = 'library_files_by_format'
ORDER BY ts`), or use the Infinity data source on the JSON endpoints.

## Deploy

### Requirements

- a Docker host with Docker Compose;
- one filesystem for the music: files are hard-linked from `inbox/` into `tracks/`;
- a Spotify developer app (Spotify requires Premium for development-mode apps) and a SoundCloud
  account, for reading your lists;
- for the Soulseek backend: a Soulseek account for the pipeline (and a second one if you also
  run slskd: an account can be logged in only once).

All containers run as the same user (UID/GID 1000 below) and in the same time zone.

### Directory layout

```
<music>/tracks/      the library (Navidrome reads it)
<music>/playlists/   .m3u files and covers (relative paths: ../tracks/...)
<music>/inbox/       downloads in progress, review and replaced files (kept 30 days)

/opt/sockseek/       the pipeline: a copy of pipeline/ with .env, src/ and config/
/opt/echolot/        Echolot's data directory (SQLite)
```

Every container that works with files mounts `<music>` at `/music`.

### Network

Peer-to-peer clients show your IP address to the peers they talk to, so running the Soulseek
side (`sockseek`, and `slskd` if you use it) behind a VPN is a sensible default for privacy.
[gluetun](https://github.com/qdm12/gluetun) fits this stack well: the P2P containers join its
network namespace with `network_mode: "container:gluetun"`, and everything else stays on the
normal network. If your VPN forwards ports for incoming connections,
[`pipeline/host/sync-listen-port.sh`](pipeline/host/sync-listen-port.sh) hands the forwarded
ports to slskd and the pipeline whenever they change (run it from cron).

`sockseek-fallback` stays on the normal network: SoundCloud rate-limits VPN addresses, and video
sites often refuse them.

### The pipeline

```sh
cp -r pipeline /opt/sockseek && cd /opt/sockseek
git clone https://github.com/fiso64/sockseek src     # Sockseek's source: its official image is the base
cp .env.example .env                                 # fill in, see below
cp config/sources.example.yml config/sources.yml     # your lists
docker compose up -d --build
```

[`pipeline/docker-compose.yml`](pipeline/docker-compose.yml), in short:

```yaml
services:
  sockseek-upstream:            # build step only: the official Sockseek image from src/
    build: ./src
    image: sockseek:upstream
    scale: 0

  sockseek:                     # Soulseek jobs
    build: { context: ., additional_contexts: { sockseek-upstream: "service:sockseek-upstream" } }
    image: sockseek:local
    network_mode: "container:gluetun"          # or a normal network without a VPN
    environment: [PUID=1000, PGID=1000, TZ=Europe/Berlin, ROLE=main,
                  DOCKER_MODS=linuxserver/mods:universal-cron]
    env_file: .env
    volumes: [./config:/config, "${MUSIC_DIR}:/music"]
    restart: unless-stopped

  sockseek-fallback:            # SoundCloud and the search fallback
    image: sockseek:local
    environment: [PUID=1000, PGID=1000, TZ=Europe/Berlin, ROLE=fallback,
                  DOCKER_MODS=linuxserver/mods:universal-cron]
    env_file: .env
    volumes: [./config:/config, "${MUSIC_DIR}:/music"]
    restart: unless-stopped
```

`.env` ([`pipeline/.env.example`](pipeline/.env.example)):

| Variable | Meaning |
|---|---|
| `MUSIC_DIR` | the music directory on the host |
| `SPOTIFY_ID`, `SPOTIFY_SECRET` | the Spotify app's credentials |
| `SPOTIFY_REFRESH` | refresh token, see below |
| `SC_TOKEN` | SoundCloud web token, see below |

**Spotify.** Create an app on developer.spotify.com with the redirect URI
`http://127.0.0.1:48721/callback` and add your account under "Users and Access". Then get the
refresh token once:

1. Open in the browser (with your app's ID):
   `https://accounts.spotify.com/authorize?client_id=<ID>&response_type=code&redirect_uri=http%3A%2F%2F127.0.0.1%3A48721%2Fcallback&scope=user-library-read%20playlist-read-private%20playlist-read-collaborative`
2. After you agree, the browser lands on a page that doesn't load; copy the `code=` value from its
   address.
3. Exchange it and put `refresh_token` into `SPOTIFY_REFRESH`:
   ```sh
   curl -s -u '<ID>:<SECRET>' -d grant_type=authorization_code -d code='<CODE>' \
     -d redirect_uri=http://127.0.0.1:48721/callback https://accounts.spotify.com/api/token
   ```

Spotify may rotate the token; the pipeline stores the new one in `state/spotify-refresh-token`.

**SoundCloud.** Log in on soundcloud.com, open the browser's developer tools (Network tab) and
take any request to `api-v2.soundcloud.com`: `SC_TOKEN` is the part after `OAuth ` in its
`Authorization` header.

**Lists** ([`config/sources.example.yml`](pipeline/config/sources.example.yml)):

```yaml
spotify:
  likes: true                        # your Liked Songs
  playlists:
    - https://open.spotify.com/playlist/<id>
    - url: https://open.spotify.com/playlist/<id>
      title: Workout                 # name in the music server (default: the list's own)
      playlist: false                # songs only, no playlist file
soundcloud:
  user: <your user name>
  likes: true
  playlists:
    - https://soundcloud.com/<user>/sets/<set>
removed_playlists: true              # songs that leave a list move to "<list> – removed"
```

**Schedule** ([`config/schedule.yml`](pipeline/config/schedule.yml), editable in Echolot's
Settings): per job minutes between runs, fixed local times or `off`.

```yaml
sync: 30                      # new and due missing songs
sweep:
  at: ["20:00", "sat,sun 15:00"]
upgrade:
  at: ["14:00", "20:30"]
playlists: 10
soundcloud: 30                # at least 15
fallback: 120
probe: 60                     # availability statistics (search only)
```

Cron starts `scripts/tick.py` every minute in both containers; it starts the jobs that are due.
The Soulseek jobs share one lock: a job that is due while another runs starts right after it.
Every Sockseek run is time-limited (20 min + 15 s per song).

**Sockseek settings** live in [`config/sockseek.conf`](pipeline/config/sockseek.conf): FLAC
preferred, the artist required in the result path, length tolerance 3 s, downloads only into
`inbox/sockseek` (never into `tracks/`: its own file mover replaces existing files), and a hook
(`post-track.sh`) that hands every finished file to `library.py`. The pipeline account's Soulseek
login goes there too (`user`, `pass`; `chmod 600` the file), so it never shows up in the process list.

First checks:

```sh
docker exec -u abc sockseek /config/scripts/music-sync.py sync --dry-run   # what would be fetched
docker exec -u abc sockseek /config/scripts/tick.py --dry-run              # when jobs run next
docker exec -u abc sockseek /config/scripts/music-sync.py status           # terminal overview
tail -f config/logs/sync.log config/logs/post-track.log
```

Run manual jobs with `-u abc`, or they write root-owned files into the library.

### slskd (optional)

A Soulseek client with a web UI, handy for searching by hand. It is independent of the pipeline
and uses its own account.

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

A playlist's name is set when Navidrome first imports it; rename it later in Navidrome. A list
removed from `sources.yml` keeps its playlist until you delete it there.

### Echolot

```yaml
services:
  echolot:
    build: .                            # or image: echolot:local (docker build -t echolot:local .)
    user: "1000:1000"
    ports:
      - "192.168.1.10:8490:8490"        # LAN only: there is no login yet
    environment:
      ECHOLOT_LIBRARY_DIR: /music/tracks
      ECHOLOT_PIPELINE_DIR: /pipeline
    volumes:
      - ./data:/data
      - <music>:/music:ro
      - /opt/sockseek/config:/pipeline                   # writes sources.yml, schedule.yml, review.yml
      - /opt/sockseek/config/state:/pipeline/state:ro
      - /opt/sockseek/config/logs:/pipeline/logs:ro
      - /opt/sockseek/config/scripts:/pipeline/scripts:ro
      - /opt/sockseek/config/crontabs:/pipeline/crontabs:ro
      - /etc/localtime:/etc/localtime:ro                 # the pipeline logs local time
    restart: unless-stopped
```

| Variable | Default | Meaning |
|---|---|---|
| `ECHOLOT_DATA_DIR` | `data` | SQLite database (`/data` in the image) |
| `ECHOLOT_LIBRARY_DIR` | unset | the library (`<music>/tracks`), read-only |
| `ECHOLOT_PIPELINE_DIR` | unset | the pipeline's `config/` directory |
| `ECHOLOT_HOST` | `127.0.0.1` | listen address (`0.0.0.0` in the image) |
| `ECHOLOT_PORT` | `8490` | listen port |

## Operation

- **Pause** all downloads with `touch config/state/PAUSED` (playlist rebuilds keep running);
  remove the file to resume. Single jobs: set them to `off` in Settings.
- **Logs:** `config/logs/<job>.log` per job, `post-track.log` for every checked download,
  `downloads.jsonl` for every filing, `tick.log` for job starts.
- **Rate limits:** Soulseek limits searches (about 34 per 220 s), so a large search takes a
  while; SoundCloud answers 429 after bursts (keep its job at 15 minutes or more); Spotify
  development-mode apps allow 5 users and cannot read Spotify's own editorial playlists.
- **Rule changes:** `pipeline/tools/rules_check.py` compares matching rules against the real
  library and history, `pipeline/tools/live_check.py` runs a sample of searches without
  downloading and judges every result.
- **Back up** `config/` (state, sources, secrets), Echolot's `data/` and the library.

## Develop

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest                  # tests; with the pipeline data on this machine also parity checks
uv run ruff check && uv run ruff format
uv run echolot serve           # http://127.0.0.1:8490
docker compose up -d --build   # dev instance, see docker-compose.yml
```

Echolot's matching rules (`src/echolot/matching.py`) mirror the pipeline's `library.py`; a test
checks both on every wanted song and library file.
