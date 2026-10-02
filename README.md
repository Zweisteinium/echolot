<p align="center">
  <img src="src/echolot/web/static/echolot.svg" alt="" width="160">
</p>

<h1 align="center">Echolot</h1>

<p align="center">
  <b>Your Spotify, SoundCloud and YouTube lists as a music library on your own server, in the best quality there is.</b><br>
  German for <i>sonar</i>: ping every source, keep only what echoes back clearly.
</p>

---

You follow playlists and likes on Spotify and SoundCloud, and playlists on YouTube. Echolot turns them
into plain audio files, one per song (`Artist/Artist - Title.flac`), and keeps them in step: new songs
arrive by themselves, better copies replace worse ones, and every list can become a playlist in
[Navidrome](https://www.navidrome.org) or any other Subsonic server. What is still missing, it tells
you, and why.

## Highlights

- **Pick your lists, done.** Connect Spotify and SoundCloud once; your playlists, likes and sets show
  up as cards. Follow one for its songs, or for its songs and a playlist with its cover. Anyone's public
  playlist, set or YouTube playlist follows by its link (a YouTube song gets Spotify's names where Spotify
  has it; its own video is the first fallback).
- **Lossless first.** Songs come from Soulseek, FLAC preferred, with YouTube and SoundCloud as the
  fallback. A lossy copy is upgraded to a genuine FLAC later, by itself.
- **Never the wrong song.** Artist, title, version and length must agree, and the audio is compared
  with the official release by fingerprint (the recording's ISRC, the release's preview). An official
  video with another edit waits for your decision instead of slipping in.
- **Fake FLACs found.** A spectrum check spots FLACs made from MP3s and keeps looking for the real one.
- **Tags from your lists.** Every file is tagged as the song in your lists, not as its uploader tagged it:
  all artists, title, album, and where it comes from (`SOURCE`: the song's pages, `DOWNLOAD`: Soulseek or
  the page it was downloaded from). `echolot tags normalize [--dry-run]` does this once for a library.
- **Review with a player.** Uncertain matches wait for one click: perfect match, close match (another
  version you take for the song; it keeps its real name) or no match; each decision can be taken
  back for two minutes.
- **Knows why something is missing.** Every search is kept: how many results, why they did not fit,
  transfers that stalled, DRM on SoundCloud, songs Spotify greys out.
- **Nothing gets lost.** No file is ever overwritten; a replaced one waits 30 days in an inbox. Songs
  that leave a list keep their files; the Changes page tells what left which list and why, and which
  songs Spotify, SoundCloud or YouTube took down, no longer play here, or deleted (a deleted video stays
  in its list).
- **One web app.** Guided setup, accounts, lists, schedule, activity and jobs; a JSON API and
  Prometheus metrics for dashboards.

## Quick start

You need Docker with Compose, a free [Soulseek](https://www.slsknet.org) account and, for Spotify,
a Premium account (Spotify only runs developer apps of Premium users).

```sh
git clone https://github.com/Zweisteinium/echolot && cd echolot/deploy
cp .env.example .env            # set MUSIC to your music folder, e.g. MUSIC=/srv/music
mkdir -p data/echolot data/sockseek data/navidrome /srv/music/inbox/soulseek
docker compose up -d            # pulls Echolot, builds Sockseek on the first start (a few minutes)
```

1. Open **Navidrome** at http://&lt;host&gt;:4533 and create its admin account. Do this first: whoever
   opens Navidrome first creates it. Echolot's users are Navidrome's accounts.
2. Open **http://&lt;host&gt;:8490** and log in with that account. In **Settings**, enter it as the service
   account too (Echolot reads Navidrome's users with it and sets the owners of its playlists).
3. **Accounts** walks you through the Spotify app, the SoundCloud login and the Soulseek account.
4. **Sources**: pick the lists to follow. The first songs arrive within minutes.
5. Listen in Navidrome, or in any Subsonic app.

| Service | Role |
|---|---|
| `echolot` | the web app and its jobs, one image ([`lordlayer/echolot`](https://hub.docker.com/r/lordlayer/echolot)); the only one that writes the library |
| `sockseek` | the [Sockseek](https://github.com/fiso64/sockseek) daemon: Soulseek searches and downloads for Echolot (not reachable from outside) |
| `navidrome` | plays the library and the playlists |

**Soulseek through a VPN.** Peers see the IP address of a Soulseek client. Set your VPN provider in
`.env` and start with `docker compose -f compose.yaml -f vpn.yaml up -d`:
[gluetun](https://github.com/qdm12/gluetun) then carries the daemon's traffic and hands it a
forwarded port where your provider offers one.

**HTTPS.** Behind a reverse proxy, set `FORWARDED_ALLOW_IPS` (below) so Echolot trusts it. The
Spotify login then returns to Echolot by itself; without HTTPS you paste one address back, the
Accounts page shows where.

## How it works

```
 Spotify, SoundCloud, YouTube ──lists──▶ Echolot ──search, download──▶ Sockseek ──▶ Soulseek
                                            │ └──── fallback ─────────▶ YouTube, SoundCloud
                                            ▼
                            <music>/tracks, <music>/playlists ──▶ Navidrome
```

1. **Lists.** Echolot reads the followed lists and keeps every song once, however many lists hold it.
2. **Search.** A missing song is searched on Soulseek. Each result is judged by its path and length
   before anything is downloaded; the best one is fetched, and if it stalls or fails, the next.
   A song not found is tried again after 3 h, 6 h, 12 h, then daily, with looser terms after two
   misses; a sweep searches every missing song at fixed hours.
3. **Fallback.** After two misses YouTube (the releases' own "Topic" uploads first) and SoundCloud
   are searched too; a YouTube song's own video comes first.
4. **Check.** Every download is repaired and normalised (WAV, AIFF and ALAC become FLAC, hi-res
   becomes 44.1/48 kHz 24 bit), spectrum-checked, and identified by name and by audio.
5. **File.** `tracks/<Artist>/<Artist> - <Title>.<ext>`, one folder per artist. A genuine FLAC
   replaces a lossy or fake copy; the old file waits in `inbox/replaced/` for 30 days.
6. **Upgrade.** Songs that are not genuine lossless are searched FLAC-only, the longest waiting
   first (each one after 12 h, 1 d, 2 d, then every 3 d). A long run gives way to the Spotify sync and
   goes on after it. A FLAC found for a SoundCloud song waits on the review page for your Perfect match.
7. **Playlists.** One `.m3u` per list shown as a playlist, in list order, with the list's cover.

### When is a file the song?

- **Same song:** the same artist (any of the song's artists, ignoring case, accents and
  punctuation), the same title once noise is removed ("(Original Mix)", "(feat. X)", "[HAK003]",
  "- 2011 Remaster", "(Official Video)"), a length within 10 s (4 % for songs over four minutes).
  Version words (remix, edit, extended, VIP, live, Pt. 2) and other featured artists keep songs
  apart; a DJ-mix cut is the song at any length.
- **Before a download:** a result is skipped when it names another version or lacks the one asked
  for, has another song's length, was marked wrong before, or does not name the artist as a name of
  its own ("HK", not "HK Gruber").
- **After a download:** *exact* (the title matches) is filed; *probable* (the core title matches,
  length within 3 s) is filed and listed for review; anything else is rejected, and a near miss
  waits for review.
- **The audio:** from 0.8 of the fingerprint bits in common with the release's preview a download is
  the recording, which confirms a probable match; up to 0.7 it is another one, which sends even an
  exact name to review. A file tagged with the song's ISRC counts as the recording.

## Configuration

Everything is set in the web app and stored in `data/echolot.db`; `echolot.yml` (Settings) exports
the lists, schedule and settings as one file for a backup or another install. The container reads:

| Variable | Default | Meaning |
|---|---|---|
| `ECHOLOT_LIBRARY_DIR` | unset | the library, `<music>/tracks` (`inbox/` and `playlists/` beside it) |
| `ECHOLOT_DAEMON_DIR` | unset | where Echolot writes the Sockseek daemon's login |
| `ECHOLOT_DATA_DIR` | `/data` | the database |
| `ECHOLOT_SECRET_KEY` | unset | key for the stored credentials; unset, `data/secret.key` is created (a copy of `data/` then holds both) |
| `ECHOLOT_NAVIDROME_URL` | unset | Navidrome's address until it is set in Settings (its accounts log in) |
| `ECHOLOT_WORKER` | `on` | `off`: run no jobs (a test copy) |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | the address a reverse proxy's requests come from; Echolot then trusts its `X-Forwarded-*` headers |

## Operation

- **Jobs** run on their own schedule (Settings); the overview starts, stops and pauses them.
- **Logs:** the Activity page lists every filing and rejection; `docker compose logs echolot` has every
  job and song.
- **Users** log in with their Navidrome account (Navidrome checks the password; while it is down,
  nobody can log in to the pages, API tokens keep working). A Navidrome admin is an admin here too;
  Users gives anyone else admin rights or permissions. No admin left:
  `docker compose exec echolot echolot user admin <name>`.
- **Back up** `data/` and the library.
- **Limits:** Soulseek allows about 34 searches per 220 s, so a long list takes a while; SoundCloud
  pauses bursts (keep its job at 15 minutes or more); a Spotify developer app serves five accounts
  and cannot read Spotify's own editorial playlists.

## API and metrics

Scripts use a token from Settings (`Authorization: Bearer <token>`); `/api/docs` describes every
endpoint.

| Endpoint | Returns |
|---|---|
| `GET /metrics` | library, songs and lists in the Prometheus format |
| `GET /api/stats`, `/api/stats/history?metric=…` | the newest hourly snapshot, one metric over time |
| `GET /api/stats/downloads?days=30` | filings and rejections per day, source and format |
| `GET /api/jobs`, `POST /jobs/<name>/run` | the jobs, and starting one |
| `GET /api/config`, `PUT /api/config` | the whole configuration as `echolot.yml` |

## Develop

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest                  # the audio checks need ffmpeg, as in the image
uv run ruff check && uv run ruff format
uv run echolot serve           # http://127.0.0.1:8490
```

| Package (`src/echolot/`) | Holds |
|---|---|
| `cli.py`, `config.py`, `db.py` | the command line, the environment, the SQLite schema |
| `settings/` | settings sections, encrypted secrets, logins and tokens, the followed lists, `echolot.yml` |
| `services/` | Spotify, SoundCloud, the Sockseek daemon, yt-dlp |
| `library/` | matching rules, the audio check, ffmpeg and tags, the catalog, filing, review, playlists, snapshots |
| `jobs/` | the worker and its schedule, getting songs, reading the lists |
| `web/` | the pages and the API, one router per module |

`tools/` has checks to run by hand: `rules_check.py` compares the matching rules of two versions on
a real library, `daemon_check.py` tests the Sockseek daemon's API against a mock, `icons.py` draws
the icons. `smoke.py` checks a built image (ffmpeg with chromaprint and soxr, yt-dlp with QuickJS); CI runs it.

**Releases:** CI tests every push and builds the image. A new version in `pyproject.toml` (after
`uv lock`, and in `deploy/compose.yaml`; a test checks both) is released when it reaches `main`: CI
tags the commit `v<version>` and publishes the release notes. The image brings its own audio-only ffmpeg (built in the Dockerfile, cached).

## Intended use

Echolot organises, verifies and deduplicates audio files and keeps them in step with your lists.
What you obtain through the sources you connect is up to you: use it for music you are entitled to,
and respect the terms of those services and the law where you live. Echolot does not circumvent copy
protection.

## License

[GNU AGPL v3](LICENSE) or later, like Sockseek.
