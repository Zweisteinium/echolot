# Changelog

What changed in each release, newest first. CI publishes a version's section as its release notes.

## 0.16.0 (2026-11-26)

A new look, lyrics in your files, and a setup that is just one compose file.

### New

- **Lyrics.** A new job embeds lyrics from LRCLIB into your files, synced where available. It only takes
  lyrics whose artist, title, version and length match the song.
- **A new interface.** A sidebar instead of tabs, a panel with everything about a song, a Search that finds
  your own songs first, Review by keyboard and Availability as its own page. All texts are shorter and
  plainer.
- **Easier setup.** The quick start is one compose file with a prebuilt Sockseek image. A new install gets a
  Soulseek account of its own, and the first Navidrome admin who logs in can lend Echolot their account with
  one click.

### Improved

- Variants of the same length, like a cappella, stripped or re-recorded versions, are no longer filed as the
  song.
- An upgrade that sounds unlike your copy waits in Review.
- A failed YouTube or SoundCloud search is tried again the next day instead of counting as not found.
- When Spotify asks Echolot to wait, the pages show a wait, not a login error.
- Pages load faster. Long lists and search details load when you open them.

### Fixed

- A scan that sees less than half the library changes nothing, so a disk that isn't mounted can't empty it.
- Lyrics are written so that a crash can't damage the file.
- The database is backed up before every schema change.
- Missing counts match the Missing page.

### Updating

- The Sockseek daemon now comes as an image instead of being built. Download the new `compose.yaml` as the
  README's Updates section shows.

## 0.15.3 (2026-11-25)

### Improved

- Review asks less. A download whose name misses the title is filed when its audio is the release's, and
  deleted when it sounds different. A probable match by the core title is filed without review.
- Spotify's own playlists, like This Is and editorial ones, are read through their public page.
- Spotify is asked once per run, and for liked songs only when you follow them.
- Free downloads behind a gate are judged by their audio, not their names.
- Covers are embedded as uploaded, and larger ones are kept.
- History names the list each song came from.
- Echolot asks Navidrome to scan when a playlist is missing there.

### Fixed

- An unreachable Soulseek daemon is logged in one line instead of a traceback.

## 0.15.2 (2026-11-12)

### New

- **Hypeddit free downloads** from the Buy page. Each gate is tried on its own, so one failure doesn't stop
  the rest.

### Improved

- Files are tagged and get covers as their own release. The song's own cover comes first, and Replace all
  gives every file its cover again.
- New songs are checked for relinked tracks right away.

## 0.15.1 (2026-11-12)

### Improved

- Re-releases under other names are found.
- Songs that played as another song lately can be recorded again.

### Fixed

- A list's own track counts over a wrong preview.
- New-song counts include only what a list sync brought in.

## 0.15.0 (2026-11-10)

### New

- **Unreleased music.** Lists of songs that exist nowhere else, uploaded by hand and checked against the
  library. Songs can be edited, reordered and removed.
- Messages show as toasts.

### Improved

- New songs join a search that is already running.
- The growth graph shows library files beside listed songs.
- The playlist setting is a switch, the quality tiers have clearer names, and a song counts as wiped off the
  internet only on firmer evidence.

## 0.14.2 (2026-11-08)

### New

- **Quality rings** on the Library page, with lossy tiers by format.

### Improved

- WAV, AIFF and ALAC originals become FLAC, verified to hold the very same samples.
- Uploads are matched by title and length, and remember where they came from: a free download, a purchase or
  elsewhere.
- Many small fixes from a code review.

## 0.14.1 (2026-11-08)

### Improved

- Flatter job cards that name the list each step works on.
- The Buy page shows free downloads.
- Pages are compressed, and graph axes are trimmed.

## 0.14.0 (2026-11-05)

### New

- **Discogs.** An optional page lists the releases of your songs that Discogs lacks, with what to submit, the
  way Discogs wants it.
- **Graphs** you can zoom, with the history of the library's quality.
- **Library tabs** and a phone layout.
- **SoundCloud login** renewed every day, with its expiry shown.

### Improved

- Redesigned job cards.
- Soulseek skips peers that keep failing and gives up downloads too slow to finish in time.
- Lists remember when each song came in, and hearts follow the order you liked them in.
- Renamed lists keep their new name, in Navidrome too, and playlist comments are cleaner.
- SoundCloud pages are kept in the source tags.

## 0.13.3 (2026-10-20)

### New

- **Hearts.** Your liked songs get their heart in Navidrome.

### Improved

- Best, the list marks and the Record dialog agree, and recorded fake FLACs are judged like uploads.

### Updating

- Hearts need the new `compose.yaml`. It puts Echolot and Navidrome on a network of their own, with fixed
  addresses.

## 0.13.2 (2026-10-19)

### Improved

- The upload check compares every file with your copy too and shows both results.
- Recording uses the names Spotify plays a relinked song as, and checks another recording against the library
  copy.

## 0.13.1 (2026-10-19)

### Improved

- Songs with other names but the same audio share one file.
- SoundCloud names are kept as written, and premiere prefixes are removed.
- History keeps a removed fake marked as fake and shows a same-audio merge as one entry.

## 0.13.0 (2026-10-18)

### New

- **Bandcamp.** Lossy SoundCloud songs that link to Bandcamp show their price on a new Buy page, and free FLACs
  are fetched by themselves.

### Improved

- slskd rescans its shares even when a scan is pending.

## 0.12.1 (2026-10-18)

### Improved

- Shorter job texts, and no recording banner.
- The Review badge follows the Review page, and Lists has a Best column.

## 0.12.0 (2026-10-16)

### New

- **lossprint** joins the spectrum check as a second test for FLACs made from lossy files. The Lossy check
  job runs it over the whole library.
- Skip on a recording device drops a song from its session.

## 0.11.6 (2026-10-14)

### Improved

- Echolot's own artist check decides on Soulseek results, not Sockseek's exact artist filter.
- Soulseek downloads are checked on Echolot's own copy, and a bad file is skipped for every song.
- "Sounds different" shows its scores, and a recording that sounds different blocks the song.
- A reworked recording panel. Record works on every list a page shows.

### Fixed

- Damaged FLAC downloads are rejected, and the next result is tried.
- A fake FLAC that replaces its copy keeps its mark.

## 0.11.5 (2026-10-13)

### Improved

- Co-artists count in the artist check of downloads.
- Previews with a lead-in the file lacks are found, and a file that sounds different can be imported anyway.
- The upload button sits next to Record.

## 0.11.4 (2026-10-12)

### Fixed

- Recording: a restart or a stop releases the song, a new request replaces the session, and a failed start
  goes last.

## 0.11.3 (2026-10-12)

### Improved

- Your account is one page again, with recorders and API tokens.
- Songs Spotify has nothing better for are no longer offered for recording, and are marked in the lists.

## 0.11.2 (2026-10-12)

### Improved

- A simpler menu. Missing opens from the Library page.

## 0.11.1 (2026-10-12)

### Improved

- A new menu: Library with Missing, Review on its own, Server settings.
- The recording's song time runs by the second, and History names Mitschnitt as the source.

## 0.11.0 (2026-10-12)

### New

- **Recording with [Mitschnitt](https://github.com/Zweisteinium/mitschnitt).** Record songs from Spotify on
  your PC, bit-exact. Pause and Stop act after the current song.

### Improved

- An uploaded file goes to the song its Spotify track or ISRC tag names.
- The growth chart shows gaps as a dotted line.

## 0.10.1 (2026-10-12)

### Improved

- Relinked Spotify songs open as the release that plays, and other releases are found by ISRC.
- The phone header fits in two rows.

## 0.10.0 (2026-10-08)

### New

- **Discover.** Search Deezer, Apple Music and SoundCloud at once, listen, and get a song. In Subsonic
  players, hearts on search results fetch the songs into Echolot · Wished.
- Public playlists no connected user follows are read with Echolot's own Spotify app.
- A SoundCloud song the library already has is linked by its page instead of downloaded again.
- Uploads compare loudness with your copy, and Replace all lets them replace your copies even where they are
  not better.

### Improved

- Spotify: a rate limit is waited out in full, and Echolot asks less.
- Search finds YouTube's artist channels without " - Topic".
- slskd rescans its share when the library changed.
- Songs hearted in a player get covers.

## 0.9.8 (2026-10-07)

### New

- A setting to keep hi-res FLAC as downloaded.
- An upload permission. Admin rights come only from Navidrome admins.

### Improved

- Artists are found by their English name too, and Chinese and Japanese names with or without spaces.
- A recording listed twice is searched and shown once.
- slskd searches one at a time, skips failing peers and gives up stale downloads.
- Real names for artists Spotify leaves blank.

## 0.9.7 (2026-10-06)

### New

- **slskd** as the Soulseek client, if you prefer it to the bundled daemon.
- Images for amd64 and arm64 on Docker Hub, pushed by CI for every release.

### Improved

- A fresh install works without making folders by hand.
- Navidrome's address can be set in `.env`.
- A Spotify 403 is explained where you connect Spotify.

### Fixed

- Taking back a No match in Review.

## 0.9.6 (2026-10-06)

### New

- The library's growth on the Overview.

### Improved

- A manual upgrade is one History entry, and each upload shows its result as soon as it is checked.
- Video titles with other dashes and labels match.

## 0.9.5 (2026-10-06)

### Improved

- Uploads are checked the moment they are added, with one progress bar.
- Close matches are handled in uploads.
- Playlist comments name their source and creator.

## 0.9.4 (2026-10-05)

### New

- **Uploads.** Upload files for missing songs, or better copies of songs you have. They are checked like
  downloads, by their real quality and their waveform.
- A volume control on the Review page.

### Improved

- Shared files are tagged with the earliest release, and relinked songs get their release info.
- Soulseek outages are handled without errors piling up.
- Follow-up jobs wait while the jobs are paused.

## 0.9.3 (2026-10-03)

### Improved

- Runs resume after a restart.

## 0.9.2 (2026-10-03)

### Improved

- Interrupted runs resume where they stopped.

## 0.9.1 (2026-10-03)

### Improved

- A refreshed design.

## 0.9.0 (2026-10-03)

### New

- **YouTube playlists** as a source.

## 0.8.8 (2026-10-03)

### New

- **Availability.** Echolot tracks whether your songs still play on Spotify and SoundCloud.

## 0.8.7 (2026-10-03)

### New

- Each user gets their own playlists in Navidrome.

## 0.8.6 (2026-10-03)

### New

- Views and permissions per user.

## 0.8.5 (2026-10-02)

### New

- Accounts and lists per user.

## 0.8.4 (2026-10-02)

### New

- **Log in with Navidrome.** Navidrome's accounts are Echolot's users.

### Improved

- Better fake FLAC detection.

## 0.8.3 (2026-10-02)

### Improved

- Urgent jobs start right away, and the Jobs page updates more smoothly.

## 0.8.2 (2026-10-02)

### Improved

- Jobs are grouped into tasks with live progress.
- Spotify lists are checked faster, in a queue of their own, with song covers.
- Lossy SoundCloud copies are replaced by matching FLACs.
- Review shows how a download differs.
- Removed playlists are off by default.

## 0.8.0 (2026-10-01)

### Improved

- New songs come first. Upgrades give way to list syncs, and SoundCloud songs get upgraded too.
- The library is checked before a search, which avoids duplicates.
- Tags merge the artists of songs that share a file and recognise YouTube Music links.

### Fixed

- Reversed "Song - Artist" titles from SoundCloud.

## 0.7.1 (2026-10-01)

### Improved

- Review shows one redesigned card per song and retags confirmed files.
- A perfect match replaces the lossy copy.
- Tags are written from the songs.
- The quality chart shows missing songs.

### Fixed

- Close matches for shortened titles, and the detection of a download's source.

## 0.7.0 (2026-09-30)

The first tagged release, built and tested by CI.

### Improved

- The image ships an audio-only ffmpeg and runs YouTube's JavaScript with QuickJS.
- Review names downloads by their tags, shows all artists of a song and knows perfect, close and no match.
- SoundCloud downloads stay linked after renames.
- Spotify links open in the desktop app.
- Pages are faster with caching and lazy loading.
