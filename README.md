# Echolot

Self-hosted music library sync. Playlists and likes from Spotify and SoundCloud go in; the library
gets only the right songs, in the best quality available, ready for Navidrome or any other
Subsonic server.

The name is German for *sonar*: ping every source, keep only what echoes back clearly.

> **Status:** the downloading is done by the music-sync pipeline in [`pipeline/`](pipeline/)
> (Sockseek plus Python scripts). Echolot is its dashboard and control panel: statistics, missing
> songs, activity, and editing sources and schedules. The pipeline's logic will move into Echolot
> step by step.

## How it works

```
 Spotify API ─┐                        VPN (gluetun, forwarded ports)
 SoundCloud ──┤    ┌──────────────────────────────────────────────┐
              │    │ sockseek (music-sync)       slskd            │
              ├───▶│  Spotify lists -> Soulseek   shares tracks/  │──▶ Soulseek network
              │    └──────────────┬───────────────────────────────┘
              │                   │ inbox/ -> checks -> library.py
              │    ┌──────────────▼──────────────┐
              └───▶│ sockseek-fallback (home IP) │──▶ SoundCloud, YouTube
                   │  SoundCloud lists, fallback │
                   └──────────────┬──────────────┘
                                  ▼
               <music>/tracks/<Artist>/<Artist> - <Title>.flac
               <music>/playlists/<list>.m3u + cover
                                  │
                   Navidrome (scans every 15 min) ──▶ Feishin, Symfonium, ...

 Echolot: reads the pipeline's state and logs, scans the library, edits sources.yml and
          schedule.yml. Web UI on port 8490.
```

A song's way into the library:

1. **Lists.** `sources.yml` names the lists: Spotify Liked Songs and playlists, SoundCloud likes
   and sets. The pipeline fetches them itself and remembers every song it has ever seen.
2. **Missing?** `library.py` decides whether the library already has a song. Same song means:
   - the same artist (case, accents and punctuation ignored; the first of several artists counts);
   - the same title once noise is removed ("(Original Mix)", "(feat. X)", "[HAK003]",
     "- 2011 Remaster");
   - a length within max(10 s, 4 %).

   Version words (Remix, Edit, Extended, VIP, II) keep songs apart.
3. **Download.**
   - **Spotify songs:** Soulseek via [Sockseek](https://github.com/fiso64/sockseek), FLAC first.
     A song that isn't found is retried after 3 h, 6 h, 12 h, then daily, and every evening a
     sweep searches all missing songs at once, when the most users are online. After two misses,
     YouTube/SoundCloud search is tried as well.
   - **SoundCloud likes:** from SoundCloud, as the original upload where the artist allows it.
     Tracks SoundCloud won't hand out (DRM) are looked up on YouTube, and must match exactly.
   - **Never Soulseek for SoundCloud likes:** uploader names are too unreliable to search Soulseek
     with.
4. **Checks.** Every search result must really be the wanted artist and title (tags or source
   file name) at the right length; anything else is discarded as `wrong-song` or `mismatch`.
   Files are also:
   - checked for codec problems;
   - converted from WAV/AIFF/ALAC to FLAC;
   - resampled from hi-res to 44.1/48 kHz 24 bit;
   - spectrum-checked, so FLACs made from MP3s count as lossy.
5. **Filing.** `library.py` is the only code that writes into `tracks/`, and it never overwrites.
   - A download that is a song already in the library is discarded.
   - A genuine lossless download replaces a lossy or fake copy; the old file goes to
     `inbox/replaced/<date>/` for 30 days.
6. **Upgrade.** Twice a day (14:00 and 20:30) Spotify songs that aren't genuine lossless are searched
   again, FLAC only. Each song waits 12 h, 1 d, 2 d, then 3 d between searches, so a run stays short.
7. **Playlists.** One `.m3u` per list, in list order, with the list's cover, rebuilt every
   10 minutes. Songs that leave a list stay in the library and move to "<list> – removed".
   Navidrome imports the playlists.

## Echolot

| Page     | What it shows or does                                                                        |
|----------|-----------------------------------------------------------------------------------------------|
| Overview | Library size, lossless share, quality tiers, completeness of every list, job activity        |
| Missing  | Songs not in the library yet, with their lists and why (not found, greyed out, DRM)          |
| Activity | Everything filed, upgraded or rejected, with reasons                                         |
| Sources  | Add, rename, hide or remove lists, likes and options, or edit `sources.yml` directly (with undo) |
| Settings | How often each pipeline job runs (`schedule.yml`) and how often Echolot refreshes            |

- **Refresh:** every 5 minutes (adjustable) Echolot imports the pipeline's state into its SQLite
  database, rescans the library and matches every song to its best file.
- **What it writes:** in the pipeline directory, only `sources.yml` and `schedule.yml`. Each
  change is validated, written atomically, and the previous version is kept for undo.
- **What it never touches:** the library, and the pipeline's state, logs and scripts, which it
  mounts read-only.

## Deploy

What you need:
- a Docker host;
- one filesystem for the music, because files are hard-linked from `inbox/` into `tracks/`;
- a VPN with port forwarding (ProtonVPN through gluetun, as below);
- two Soulseek accounts (one for slskd, one for the pipeline);
- a Spotify account for the developer app (Spotify requires Premium for dev-mode apps);
- a SoundCloud account.

All containers run as the same user (UID/GID 1000 here) and use the same time zone.

### 1. Music disk

```
<music>/tracks/      the library: Navidrome reads it, slskd shares it
<music>/playlists/   .m3u files and covers
<music>/inbox/       downloads in progress, replaced files (kept 30 days)
```

Every container that touches these mounts `<music>` at `/music`; Navidrome mounts `tracks/` and
`playlists/` at the same paths, read-only. The playlist files hold relative paths
(`../tracks/...`).

### 2. VPN: gluetun

Soulseek transfers with firewalled peers only work with an open listen port, so the VPN needs
port forwarding.

```yaml
services:
  gluetun:
    image: qmcgaw/gluetun
    cap_add: [NET_ADMIN]
    devices: [/dev/net/tun:/dev/net/tun]
    environment:
      - VPN_SERVICE_PROVIDER=protonvpn
      - VPN_TYPE=wireguard
      - WIREGUARD_PRIVATE_KEY=...
      - VPN_PORT_FORWARDING=on
      - VPN_PORT_FORWARDING_PORTS_COUNT=2   # one port for slskd, one for the pipeline
      - LOCAL_NETWORK_SUBSET=192.168.1.0/24 # your LAN, for the slskd web UI
    ports:
      - "5030:5030"                         # slskd web UI
```

### 3. slskd

slskd runs in gluetun's network, shares `tracks/` and downloads into `inbox/`:

```yaml
services:
  slskd:
    image: slskd/slskd
    network_mode: "container:gluetun"
    volumes: [./slskd:/app, <music>:/music]
```

Things to set in `slskd.yml`:
- `directories.downloads: /music/inbox/slskd` and `directories.incomplete: /music/inbox/incomplete`;
- `shares.directories: [/music/tracks]`;
- transfer limits under `transfers.upload/download/groups`. There is no `global` level; an
  invalid config silently breaks search responses.

**Forwarded ports.** ProtonVPN hands out new ports whenever gluetun reconnects, and each client
must listen on the public port number itself. [`pipeline/host/sync-listen-port.sh`](pipeline/host/sync-listen-port.sh)
distributes them: slskd gets one (written to `slskd.yml`, followed by a restart) and the pipeline
gets the other (`state/listen-port`). Run it from the host's crontab:

```
*/10 * * * * /path/to/pipeline/host/sync-listen-port.sh >> /path/to/sync-listen-port.log 2>&1
```

### 4. The pipeline

```sh
cp -r pipeline /opt/sockseek && cd /opt/sockseek
git clone https://github.com/fiso64/sockseek src          # Sockseek source: its official image is the base
cp .env.example .env                                      # fill in, see below
cp config/sources.example.yml config/sources.yml          # your lists
docker compose up -d --build
```

Why two containers: `sockseek` runs inside gluetun's network (Soulseek over the VPN), while
`sockseek-fallback` runs on the home IP, because YouTube refuses VPN exits and SoundCloud
rate-limits them. Both share `config/`. Cron starts `scripts/tick.py` every minute in each
container, and it starts that container's jobs when they are due according to `schedule.yml`.
A job runs either every N minutes or at fixed local times (`at: ["20:00", "sat,sun 15:00"]`).
The Soulseek jobs share one connection, so a job that is due while another runs starts right
after it instead of losing its turn.

**Spotify.** Create an app on developer.spotify.com, add the redirect URI
`http://127.0.0.1:48721/callback` and add your account under "Users and Access". Put the app's ID
and secret into `.env`. Then get the refresh token once:

1. Open in the browser (with your app's ID):
   `https://accounts.spotify.com/authorize?client_id=<ID>&response_type=code&redirect_uri=http%3A%2F%2F127.0.0.1%3A48721%2Fcallback&scope=user-library-read%20playlist-read-private%20playlist-read-collaborative`
2. After you agree, the browser lands on a page that doesn't load. Copy the `code=` value from its
   address.
3. Exchange the code for a refresh token and put `refresh_token` into `SPOTIFY_REFRESH`:
   ```sh
   curl -s -u '<ID>:<SECRET>' -d grant_type=authorization_code -d code='<CODE>' \
     -d redirect_uri=http://127.0.0.1:48721/callback https://accounts.spotify.com/api/token
   ```

Spotify may rotate the token; the pipeline stores the new one in `state/spotify-refresh-token`.
Redo the login if `logs/sync.log` shows auth errors.

**SoundCloud.** Log in on soundcloud.com and open the browser's developer tools (Network tab).
Take any request to `api-v2.soundcloud.com`; `SC_TOKEN` is the part after `OAuth ` in its
`Authorization` header.

First run and checks:

```sh
docker exec -u abc sockseek /config/scripts/music-sync.py sync --dry-run   # what would be fetched
docker exec -u abc sockseek /config/scripts/tick.py --dry-run              # when jobs run next
docker exec -u abc sockseek /config/scripts/music-sync.py status           # terminal overview
tail -f config/logs/sync.log config/logs/post-track.log
```

### 5. Navidrome

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

A playlist's name is set when Navidrome first imports it. Rename it later in Navidrome itself.
A list removed from `sources.yml` keeps its playlist until you delete it in Navidrome.

### 6. Echolot

```yaml
services:
  echolot:
    image: echolot:local            # docker build -t echolot:local .
    user: "1000:1000"
    ports:
      - "192.168.1.10:8490:8490"    # LAN only: there is no login yet
    environment:
      ECHOLOT_LIBRARY_DIR: /music/tracks
      ECHOLOT_PIPELINE_DIR: /pipeline
    volumes:
      - ./data:/data
      - <music>:/music:ro
      - /opt/sockseek/config:/pipeline                   # writes sources.yml, schedule.yml
      - /opt/sockseek/config/state:/pipeline/state:ro
      - /opt/sockseek/config/logs:/pipeline/logs:ro
      - /opt/sockseek/config/scripts:/pipeline/scripts:ro
      - /opt/sockseek/config/crontabs:/pipeline/crontabs:ro
      - /etc/localtime:/etc/localtime:ro                 # the pipeline logs local time
    restart: unless-stopped
```

| Variable               | Default     | Meaning                                                  |
|------------------------|-------------|----------------------------------------------------------|
| `ECHOLOT_DATA_DIR`     | `data`      | SQLite database (`/data` in the image)                   |
| `ECHOLOT_LIBRARY_DIR`  | unset       | The library (`<music>/tracks`), read-only                |
| `ECHOLOT_PIPELINE_DIR` | unset       | The pipeline's `config/` directory                       |
| `ECHOLOT_HOST`         | `127.0.0.1` | Listen address (`0.0.0.0` in the image)                  |
| `ECHOLOT_PORT`         | `8490`      | Listen port                                              |

## Things to know

- **VPN and home IP.** Soulseek runs over the VPN. YouTube and SoundCloud don't work well through
  it, which is why the fallback container exists. Don't move SoundCloud or YouTube jobs into the
  VPN container.
- **Never let Sockseek write into `tracks/`.** Its own mover deletes an existing target file.
  `sockseek.conf` sends everything to `inbox/sockseek`, and the hook files it through `library.py`.
- **Correctness before quality.** `strict-artist = true` in `sockseek.conf` and the identity check
  in `library.py` are deliberate. Loosening them brings in same-titled songs by other artists.
- **When people are online.** Rare songs often exist on only a few peers, who come and go.
  Soulseek publishes no usage statistics. The retry sweep and the FLAC upgrade are placed in the
  European evening (20:00–21:00), which overlaps with the American afternoon, the general
  internet peak hours, plus weekend afternoons.
- **Rate limits.**
  - Soulseek bans clients that search too fast (Sockseek allows 34 searches per 220 s), so a
    search of 900 songs takes about 1.5–2 hours.
  - SoundCloud answers 429 after bursts, so keep its job at 15 minutes or more.
  - Spotify dev-mode apps allow 5 users. Spotify's editorial playlists ("Today's Top Hits",
    "Discover Weekly") can't be read; copy them into a playlist of your own.
- **Pausing.** `touch config/state/PAUSED` stops all downloads (playlist rebuilds keep running);
  remove the file to resume. Turning off single jobs: set their interval to 0 in Settings.
- **Where to look.**
  - `config/logs/<job>.log`: output of each job.
  - `config/logs/post-track.log`: rejected downloads with reasons.
  - `config/logs/downloads.jsonl`: every filing.
  - `config/logs/tick.log`: job starts.
- **Back up** `config/` (state, sources, secrets), Echolot's `data/`, and the library itself.
- **Legal.** Download only what you may download where you live. Nothing here circumvents copy
  protection. The Soulseek client shares the library, as the network expects.

## Develop

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest            # tests; with the pipeline on this machine also a parity check against it
uv run ruff check && uv run ruff format
uv run echolot serve     # http://127.0.0.1:8490
docker compose up -d --build   # dev instance, see docker-compose.yml
```
