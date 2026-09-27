"""SQLite database in the data directory. The schema is versioned with PRAGMA user_version."""

import sqlite3
from pathlib import Path

# One entry per schema version; a database is brought up to date by running the missing ones.
MIGRATIONS = [
    """
    CREATE TABLE files (                -- audio files in the library
        path TEXT PRIMARY KEY,          -- relative to the library: <Artist>/<Artist> - <Title>.<ext>
        size INTEGER NOT NULL,
        mtime INTEGER NOT NULL,
        duration REAL NOT NULL,         -- seconds, 0 = unknown
        kbps INTEGER NOT NULL,
        quality TEXT                    -- see library.QUALITY
    );
    CREATE TABLE lossy_sourced (        -- FLACs the spectrum check found to be made from lossy files
        stem TEXT PRIMARY KEY,          -- files.path without extension
        source TEXT,                    -- estimated original, e.g. "~128 kbps"
        detected TEXT
    );
    CREATE TABLE lists (                -- playlists and likes the library follows
        key TEXT PRIMARY KEY,           -- spotify:likes, spotify:playlist:<id>, soundcloud:<path>
        service TEXT NOT NULL,
        title TEXT NOT NULL,
        url TEXT,
        position INTEGER NOT NULL,
        playlist INTEGER NOT NULL DEFAULT 1  -- shown as a playlist in the music server
    );
    CREATE TABLE songs (                -- every song of every list, once
        key TEXT PRIMARY KEY,           -- spotify:<track id>, soundcloud:<track id>
        service TEXT NOT NULL,
        artist TEXT NOT NULL,
        title TEXT NOT NULL,
        album TEXT NOT NULL DEFAULT '',
        length REAL NOT NULL DEFAULT 0,
        unavailable TEXT,               -- why the source cannot deliver it (greyed out, DRM)
        stem TEXT,                      -- SoundCloud: the library file it was downloaded as
        file TEXT                       -- best library copy (files.path), NULL = missing
    );
    CREATE TABLE list_songs (
        list_key TEXT NOT NULL REFERENCES lists(key) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        song_key TEXT NOT NULL REFERENCES songs(key),
        PRIMARY KEY (list_key, position)
    );
    CREATE INDEX list_songs_song ON list_songs(song_key);
    CREATE TABLE attempts (             -- download attempts for songs that were not found
        song_key TEXT PRIMARY KEY,
        tries INTEGER NOT NULL,
        last_try INTEGER,               -- unix time
        last_fallback INTEGER           -- unix time of the last YouTube/SoundCloud search
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
        reason TEXT
    );
    CREATE INDEX events_ts ON events(ts);
    CREATE TABLE jobs (                 -- last run of each scheduled job
        name TEXT PRIMARY KEY,
        started TEXT,
        finished TEXT,                  -- NULL while running
        ok INTEGER,
        message TEXT
    );
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """,
    """
    ALTER TABLE lists ADD COLUMN fetched INTEGER NOT NULL DEFAULT 1;  -- 0: not fetched by the pipeline yet
    CREATE TABLE config_versions (      -- earlier contents of the pipeline config files Echolot edits
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,             -- sources.yml, schedule.yml
        ts TEXT NOT NULL,               -- when it was replaced
        text TEXT NOT NULL,
        note TEXT
    );
    """,
    """
    CREATE TABLE probes (               -- availability probes: how many Soulseek users have a song, when
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,               -- local time the probe started
        artist TEXT NOT NULL,
        title TEXT NOT NULL,
        kind TEXT NOT NULL,             -- rare, common (from probe.csv)
        users INTEGER NOT NULL,         -- users with a matching file
        lossless_users INTEGER NOT NULL,
        files INTEGER NOT NULL
    );
    CREATE INDEX probes_ts ON probes(ts);
    """,
    """
    ALTER TABLE events ADD COLUMN song TEXT;            -- songs.key the download was for
    ALTER TABLE events ADD COLUMN matched TEXT;         -- exact, probable (loosened search), review (accepted)
    ALTER TABLE events ADD COLUMN found TEXT;           -- tag title or source file name of the download
    ALTER TABLE events ADD COLUMN file_name TEXT;       -- source file name (Soulseek) or video title
    ALTER TABLE events ADD COLUMN fake INTEGER;         -- FLAC made from a lossy file
    ALTER TABLE events ADD COLUMN tries INTEGER;        -- searches that had not found the song before
    ALTER TABLE events ADD COLUMN wanted_seconds INTEGER;
    """,
]


def connect(path: Path) -> sqlite3.Connection:
    # one connection per request or job; FastAPI may open and use it on different threads
    con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init(path: Path) -> None:
    """Create or upgrade the database."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(path)
    try:
        con.execute("PRAGMA journal_mode = WAL")  # readers don't wait for the jobs' writes
        version = con.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            con.executescript(f"BEGIN; {script}; PRAGMA user_version = {number}; COMMIT;")
    finally:
        con.close()


def get_meta(con: sqlite3.Connection, key: str, default: str = "") -> str:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
