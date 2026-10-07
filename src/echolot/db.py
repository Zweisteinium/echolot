"""SQLite database in the data directory. The schema is below; its version is PRAGMA user_version.
A change to it gets a numbered migration step then (version 13 onwards)."""

import sqlite3
from pathlib import Path

VERSION = 27
SCHEMA = """
CREATE TABLE files (                -- audio files in the library
    path TEXT PRIMARY KEY,          -- relative to the library: <Artist>/<Artist> - <Title>.<ext>
    size INTEGER NOT NULL,
    mtime INTEGER NOT NULL,
    duration REAL NOT NULL,         -- seconds, 0 = unknown
    kbps INTEGER NOT NULL,
    quality TEXT                    -- see catalog.QUALITY
);
CREATE TABLE lossy_sourced (        -- FLACs the spectrum check found to be made from lossy files
    stem TEXT PRIMARY KEY,          -- files.path without extension
    source TEXT,                    -- estimated original, e.g. "~128 kbps"
    detected TEXT
);
CREATE TABLE lists (                -- the followed lists as last fetched, once however many users follow one
    key TEXT PRIMARY KEY,           -- spotify:likes:<user id>, spotify:playlist:<id>, soundcloud:<path>
    service TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    position INTEGER NOT NULL,
    playlist INTEGER NOT NULL DEFAULT 1,  -- shown as a playlist in the music server
    fetched INTEGER NOT NULL DEFAULT 1,   -- 0: not fetched yet
    cover_url TEXT,                 -- the list's picture at its source
    cover_file TEXT,                -- the cover_url saved next to its playlist file
    snapshot TEXT,                  -- Spotify: snapshot_id of the last listing
    fetched_at TEXT,                -- last successful listing
    creator TEXT                    -- who made the list at its source (Spotify's owner, YouTube's author)
);
CREATE TABLE songs (                -- every song of every list, once; kept when it leaves every list
    key TEXT PRIMARY KEY,           -- spotify:<track id>, soundcloud:<track id>
    service TEXT NOT NULL,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    album TEXT NOT NULL DEFAULT '',
    length REAL NOT NULL DEFAULT 0,
    unavailable TEXT,               -- why the source cannot deliver it (greyed out, DRM)
    stem TEXT,                      -- SoundCloud: the library file it was downloaded as
    file TEXT,                      -- best library copy (files.path), NULL = missing
    artists TEXT,                   -- JSON list of all the song's artists (Spotify)
    link TEXT,                      -- JSON [artist, title]: the library song it is (review decision)
    close_match INTEGER NOT NULL DEFAULT 0,  -- the link is another version, taken as a close match in review
    url TEXT,                       -- SoundCloud: the track page it is downloaded from
    archived INTEGER NOT NULL DEFAULT 0,  -- SoundCloud: downloaded once
    isrc TEXT,                      -- Spotify: the recording's ISRC
    released TEXT,                  -- Spotify: its release's date (YYYY, YYYY-MM or YYYY-MM-DD)
    track INTEGER,                  -- Spotify: its number on that release, of `tracks`, on disc `disc`
    tracks INTEGER,
    disc INTEGER,
    artist_alias TEXT               -- Spotify: the artist's English name if it differs (祖堅 正慶: Masayoshi Soken),
                                    -- searched and matched as well; '' = checked, the same; NULL = not asked yet
);
CREATE TABLE list_songs (
    list_key TEXT NOT NULL REFERENCES lists(key) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    song_key TEXT NOT NULL REFERENCES songs(key),
    PRIMARY KEY (list_key, position)
);
CREATE INDEX list_songs_song ON list_songs(song_key);
CREATE INDEX songs_file ON songs(file);  -- the songs of a file (tags, shared files, merges)
CREATE INDEX songs_stem ON songs(stem);
CREATE VIEW wanted AS SELECT * FROM songs WHERE key IN (SELECT song_key FROM list_songs);
CREATE TABLE list_history (         -- every song a list ever had (first and last seen)
    list_key TEXT NOT NULL,
    song_key TEXT NOT NULL,
    first_seen TEXT NOT NULL,       -- date
    last_seen TEXT NOT NULL,
    PRIMARY KEY (list_key, song_key)
);
CREATE TABLE attempts (             -- searches for songs that were not found
    song_key TEXT PRIMARY KEY,
    tries INTEGER NOT NULL,
    last_try INTEGER,               -- unix time
    last_fallback INTEGER,          -- unix time of the last YouTube/SoundCloud search
    result TEXT,                    -- JSON: what the last Soulseek search saw (acquire.Fetcher.song)
    fallback_result TEXT            -- JSON: what the last YouTube/SoundCloud search saw (acquire.fallback)
);
CREATE TABLE upgrades (             -- FLAC searches that found nothing better yet
    song_key TEXT PRIMARY KEY,
    tries INTEGER NOT NULL,
    last_try INTEGER NOT NULL       -- unix time
);
CREATE TABLE refs (                 -- the releases behind ISRCs, for the audio check (identity.py)
    isrc TEXT PRIMARY KEY,
    deezer_id INTEGER,              -- NULL: Deezer does not know it
    duration INTEGER,
    fingerprint BLOB,               -- Chromaprint of the 30 s preview; NULL: none
    checked INTEGER NOT NULL        -- unix time
);
CREATE TABLE events (               -- everything that was filed into or taken out of the library
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,               -- local time, ISO 8601
    action TEXT NOT NULL,           -- new, upgrade, duplicate, wrong-song, mismatch, retired, ...
    path TEXT,
    ext TEXT,
    bytes INTEGER,
    kbps INTEGER,
    seconds INTEGER,
    source TEXT,
    artist TEXT,
    title TEXT,
    reason TEXT,
    song TEXT,                      -- songs.key the download was for
    matched TEXT,                   -- exact, probable (loosened search), review (accepted)
    found TEXT,                     -- tag title or source file name of the download
    file_name TEXT,                 -- source file name (Soulseek) or video title
    fake INTEGER,                   -- FLAC made from a lossy file
    tries INTEGER,                  -- searches that had not found the song before
    wanted_seconds INTEGER,
    audio TEXT,                     -- what the audio check found
    url TEXT,                       -- the page a download came from (YouTube, SoundCloud)
    compared TEXT,                  -- review: what differs from your copy (identity.compare); '' = nothing to compare
    peer_bytes INTEGER              -- Soulseek: the size of the peer's file (it tells the same file in later searches)
);
CREATE INDEX events_ts ON events(ts);
CREATE TABLE availability (         -- whether a song still plays at its source, as last checked (jobs/availability)
    song_key TEXT PRIMARY KEY,
    state TEXT NOT NULL,            -- available, taken_down (plays nowhere), blocked (not in this country),
                                    -- gone (exists no more), preview (SoundCloud: 30 s without Go+), replaced
    since TEXT NOT NULL,            -- date it came to this state; '' when it was so at its first check
    checked TEXT NOT NULL,
    detail TEXT                     -- replaced: the song key of the release it plays as now
);
CREATE TABLE changes (              -- what happened to the songs and lists of everyone (jobs/availability)
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    song_key TEXT NOT NULL DEFAULT '',  -- '' for a list's own change
    list_key TEXT,                  -- the list it happened in (added, removed, replaced, re-uploaded, unreadable)
    change TEXT NOT NULL,           -- added, removed, replaced, re-uploaded, unreadable, readable, or a state
    detail TEXT                     -- removed: why (filled in by the next check); replaced: the new song's key
);
CREATE INDEX changes_ts ON changes(ts);
CREATE INDEX changes_song ON changes(song_key);
CREATE TABLE blocked (              -- downloads marked wrong in review: never taken for the song again
    song_key TEXT NOT NULL,
    name TEXT NOT NULL,             -- tag title or source file name
    PRIMARY KEY (song_key, name)
);
CREATE TABLE review_decisions (
    id TEXT PRIMARY KEY,            -- review.decision_id: '<event ts> <path>'
    event_id INTEGER,
    decision TEXT NOT NULL,         -- ok, close, wrong (filed); accept, close, discard (kept)
    name TEXT,                      -- close: 'Artist - Title', what the download really is
    decided TEXT NOT NULL,
    applied TEXT,                   -- NULL: not yet (can be reverted)
    result TEXT,
    user_id INTEGER,                -- who decided (NULL: before users were recorded)
    overridden TEXT                 -- '<admin> <time>': an admin took it back after it was applied
);
CREATE TABLE snapshots (            -- metrics over time (history.py), hourly
    ts TEXT NOT NULL,               -- UTC, ISO 8601 ("...T14:00:00Z"); all rows of one snapshot share it
    metric TEXT NOT NULL,           -- see history.METRICS
    key TEXT NOT NULL DEFAULT '',   -- label value: quality tier, format, service, list key, ...
    value REAL NOT NULL,
    PRIMARY KEY (ts, metric, key)
);
CREATE INDEX snapshots_metric ON snapshots(metric, ts);
CREATE TABLE jobs (                 -- last run of each scheduled job
    name TEXT PRIMARY KEY,
    started TEXT,
    finished TEXT,                  -- NULL while running
    ok INTEGER,
    message TEXT
);
CREATE TABLE sources (              -- the lists each user follows, as configured (lists: as fetched)
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),  -- NULL: from before users had lists, until an admin adopts it
    key TEXT NOT NULL,              -- the list key: spotify:likes:<user id>, spotify:playlist:<id>, soundcloud:<path>
    service TEXT NOT NULL,          -- spotify, soundcloud
    likes INTEGER NOT NULL DEFAULT 0,     -- 1: the account's own likes
    url TEXT NOT NULL,
    title TEXT,                     -- name override; NULL: the list's own name
    playlist INTEGER NOT NULL DEFAULT 1,  -- also a playlist in the music server
    enabled INTEGER NOT NULL DEFAULT 1,   -- 0: likes switched off (the row keeps their options)
    position INTEGER NOT NULL,
    added TEXT NOT NULL,
    UNIQUE (user_id, key)
);
CREATE TABLE settings (             -- configuration by section (options.py), JSON
    section TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE TABLE secrets (              -- credentials, encrypted (vault.py)
    name TEXT PRIMARY KEY,
    value BLOB NOT NULL,
    updated TEXT NOT NULL
);
CREATE TABLE users (               -- Navidrome's accounts that logged in (Navidrome checks the password)
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created TEXT NOT NULL,
    last_login TEXT,
    admin INTEGER NOT NULL DEFAULT 0,       -- Echolot admin, given in Echolot (a Navidrome admin is one anyway)
    navidrome_id TEXT,
    navidrome_admin INTEGER NOT NULL DEFAULT 0,  -- as Navidrome last said (login, or its user list)
    permissions TEXT NOT NULL DEFAULT '',   -- what a user who is no admin may do besides their own: 'review,run,upload'
    view TEXT NOT NULL DEFAULT 'mine',      -- an admin's pages: mine or everyone
    disabled INTEGER NOT NULL DEFAULT 0,    -- gone from Navidrome: no login, sessions and tokens ended
    soundcloud_user TEXT NOT NULL DEFAULT ''  -- whose likes their SoundCloud likes are
);
CREATE TABLE sessions (             -- browser logins
    id TEXT PRIMARY KEY,            -- SHA-256 of the cookie value
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL,             -- token every form and htmx request of the session sends back
    created TEXT NOT NULL,
    expires TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE api_tokens (           -- bearer tokens for scripts
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,     -- SHA-256 of the token
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created TEXT NOT NULL,
    last_used TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


# version -> the statements from the version before; each step runs in one transaction with its user_version
MIGRATIONS = {
    13: [
        "ALTER TABLE songs ADD COLUMN close_match INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE review_decisions ADD COLUMN name TEXT",
    ],
    14: ["ALTER TABLE events ADD COLUMN url TEXT"],
    15: ["ALTER TABLE events ADD COLUMN compared TEXT"],
    16: ["ALTER TABLE events ADD COLUMN peer_bytes INTEGER"],
    17: [
        "ALTER TABLE users ADD COLUMN source TEXT NOT NULL DEFAULT 'local'",
        "ALTER TABLE users ADD COLUMN admin INTEGER NOT NULL DEFAULT 1",
    ],
    18: [  # Navidrome accounts only: the local ones go with their sessions and tokens
        "DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE source = 'local')",
        "DELETE FROM api_tokens WHERE user_id IN (SELECT id FROM users WHERE source = 'local')",
        "DELETE FROM users WHERE source = 'local'",
        "ALTER TABLE users ADD COLUMN navidrome_id TEXT",
        "ALTER TABLE users ADD COLUMN navidrome_admin INTEGER NOT NULL DEFAULT 0",
        "UPDATE users SET navidrome_admin = admin",
        "ALTER TABLE users ADD COLUMN permissions TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE users ADD COLUMN view TEXT NOT NULL DEFAULT 'mine'",
        "ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users DROP COLUMN password",
        "ALTER TABLE users DROP COLUMN source",
        "ALTER TABLE review_decisions ADD COLUMN user_id INTEGER",
    ],
    19: [  # lists per user: the existing ones belong to nobody until sources.adopt gives them to the oldest admin
        "ALTER TABLE users ADD COLUMN soundcloud_user TEXT NOT NULL DEFAULT ''",
        "CREATE TABLE sources_19 (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), key TEXT NOT NULL, "
        "service TEXT NOT NULL, likes INTEGER NOT NULL DEFAULT 0, url TEXT NOT NULL, title TEXT, "
        "playlist INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1, position INTEGER NOT NULL, "
        "added TEXT NOT NULL, UNIQUE (user_id, key))",
        "INSERT INTO sources_19 (key, service, likes, url, title, playlist, enabled, position, added) "
        "SELECT key, service, likes, url, title, playlist, enabled, position, added FROM sources ORDER BY position",
        "DROP TABLE sources",
        "ALTER TABLE sources_19 RENAME TO sources",
    ],
    20: [  # an admin's override of a user's decision; each user's song history (until now everything
        # was one user's: the oldest owner's history is the library's song history so far)
        "ALTER TABLE review_decisions ADD COLUMN overridden TEXT",
        "INSERT OR IGNORE INTO snapshots (ts, metric, key, value) SELECT ts, 'user_' || metric, "
        "(SELECT min(user_id) FROM sources) || ':' || key, value FROM snapshots WHERE metric IN "
        "('songs_by_quality', 'songs_missing_by_reason') AND (SELECT min(user_id) FROM sources) IS NOT NULL",
        "INSERT OR IGNORE INTO snapshots (ts, metric, key, value) SELECT ts, 'user_' || metric, "
        "(SELECT min(user_id) FROM sources), sum(value) FROM snapshots WHERE metric IN "
        "('songs_wanted', 'songs_in_library', 'songs_missing') AND (SELECT min(user_id) FROM sources) IS NOT NULL "
        "GROUP BY ts, metric",
    ],
    21: [  # the availability tracker (replaces the "- removed" playlists)
        "CREATE TABLE availability (song_key TEXT PRIMARY KEY, state TEXT NOT NULL, since TEXT NOT NULL, "
        "checked TEXT NOT NULL, detail TEXT)",
        "CREATE TABLE changes (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, song_key TEXT NOT NULL DEFAULT '', "
        "list_key TEXT, change TEXT NOT NULL, detail TEXT)",
        "CREATE INDEX changes_ts ON changes(ts)",
        "CREATE INDEX changes_song ON changes(song_key)",
    ],
    22: [  # the release facts of Spotify songs (tags); a state seen at the first check has no known date
        "ALTER TABLE songs ADD COLUMN released TEXT",
        "ALTER TABLE songs ADD COLUMN track INTEGER",
        "ALTER TABLE songs ADD COLUMN tracks INTEGER",
        "ALTER TABLE songs ADD COLUMN disc INTEGER",
        "UPDATE availability SET since = '' WHERE NOT EXISTS (SELECT 1 FROM changes c "
        "WHERE c.song_key = availability.song_key AND c.change = availability.state)",
    ],
    23: ["ALTER TABLE lists ADD COLUMN creator TEXT"],  # who made a list, for its playlist's comment
    24: [  # the songs of a file found without reading all songs (it grows with the library)
        "CREATE INDEX IF NOT EXISTS songs_file ON songs(file)",
        "CREATE INDEX IF NOT EXISTS songs_stem ON songs(stem)",
    ],
    25: [  # snapshot times in UTC, not local time (SQLite converts with the server's time zone)
        "UPDATE snapshots SET ts = strftime('%Y-%m-%dT%H:%M:%SZ', ts, 'utc') "
        "WHERE ts NOT LIKE '%Z' AND strftime('%Y-%m-%dT%H:%M:%SZ', ts, 'utc') IS NOT NULL"
    ],
    26: [  # uploading by hand is a permission of its own: who could (with review) still can
        "UPDATE users SET permissions = CASE WHEN ',' || permissions || ',' LIKE '%,run,%' "
        "THEN 'review,run,upload' ELSE 'review,upload' END WHERE ',' || permissions || ',' LIKE '%,review,%'"
    ],
    27: ["ALTER TABLE songs ADD COLUMN artist_alias TEXT"],  # Spotify's English name of a non-Latin artist
}


def connect(path: Path) -> sqlite3.Connection:
    # one connection per request or job; FastAPI may open and use it on different threads
    con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init(path: Path) -> None:
    """Create the database, or bring it to this schema (from version 12 on)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(path)
    try:
        con.execute("PRAGMA journal_mode = WAL")  # readers don't wait for the jobs' writes
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version == 0 and not con.execute("SELECT 1 FROM sqlite_master").fetchone():
            con.executescript(f"BEGIN; {SCHEMA}; PRAGMA user_version = {VERSION}; COMMIT;")
            version = VERSION
        elif not 12 <= version <= VERSION:
            raise RuntimeError(f"{path} has database schema {version}; this Echolot needs {VERSION}")
        for step in range(version + 1, VERSION + 1):
            con.executescript(f"BEGIN; {'; '.join(MIGRATIONS[step])}; PRAGMA user_version = {step}; COMMIT;")
    finally:
        con.close()


def get_meta(con: sqlite3.Connection, key: str, default: str = "") -> str:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
