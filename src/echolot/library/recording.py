"""Recording Spotify songs with a recorder on the user's own computer (Mitschnitt): the devices, what to
record, the sessions and their songs (items), handed to the device one at a time (web/recorders).

A device belongs to the user who connected it: only they see it and record on it (an admin neither). It
connects the way a desktop app signs in (OAuth with PKCE): the browser asks the user (approve), the app
trades the code for its token (exchange). What to record is a user's Spotify songs that the library has worse
than a tier (BELOW), worst first, each recording once, without what Spotify plays nowhere, what played as
another song lately (SKIP_DAYS) or what Spotify has nothing better of (block: its stream is lossy, or no better
than the library's copy; for good, while Spotify plays the song as that track); a session's songs are fixed
when it starts, one that got better meanwhile is skipped when its turn comes (found); a song whose start
failed goes to the end of the queue, and to the end of the next session within TROUBLE_HOURS. A song handed
out is the device's until its lease ends (its length and LEASE_SLACK), then it is queued again once (TRIES);
a device that restarts (fresh) or a session stopped releases it at once, without costing it a try. One
session runs on a device at a time: a new one stops the old.
"""

import base64
import hashlib
import json
import re
import secrets
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

WORST = ["missing", "lossy-low", "lossy-mid", "lossy-high", "fake"]  # the order a session records in
BELOW = {  # a session's choice -> the tiers it records (a fake FLAC is worse than lossless only)
    "lossless": tuple(WORST),
    "lossy-high": ("missing", "lossy-low", "lossy-mid"),
    "lossy-mid": ("missing", "lossy-low"),
    "missing": ("missing",),
}
BELOW_LABELS = {
    "lossless": "Not lossless",
    "lossy-high": "Below 256 kbps",
    "lossy-mid": "Below 160 kbps",
    "missing": "Missing only",
}
CODE_MINUTES = 10  # an approved connection's code works this long, once
ONLINE_SECONDS = 75  # a device that asked this lately is online (it asks at least every 30 s)
LEASE_SLACK = 15 * 60  # seconds a device has beyond the song's length to answer for it
TRIES = 2  # a song that failed (or got no answer) is queued again until it was handed out this often
SKIP_DAYS = 30  # a song Spotify has only lossy, or plays as another song, is not offered again for this long
SKIPS = ("lossy_only", "wrong_song")  # a song done with this was not recorded (the session's "skipped")
SKIPPED_FOR_DAYS = ("wrong_song",)  # ... and is not offered again for SKIP_DAYS (lossy_only: never, see block)
BEST = {  # spotify_best.reason -> why Spotify has nothing better (the lists show it)
    "lossy": "Spotify plays this song lossy only",
    "same": "Spotify's stream is no better than your copy",
    "recorded": "your copy is the best Spotify has (recorded, not lossless)",
}
PLAYS_AS = (  # SQL: the Spotify track a song plays as here (the release Spotify relinks it to), of {key}
    "'spotify:track:' || CASE WHEN a.state = 'replaced' AND a.detail LIKE 'spotify:%' THEN substr(a.detail, 9) "
    "ELSE substr({key}, 9) END"
)
UPLOADED = ("filed", "not_better", "rejected")  # what Echolot made of a recording (web/recorders)
AGAIN = ("stalled", "failed")  # the device could not record it: queued again until TRIES
TOKEN = "echolot_rec_"
EXPIRED = "no answer from the device"  # an item's detail once its lease ran out
STOPPED = "its session was stopped while it was recorded"
RELEASED = "the device restarted while it was recorded"
LATE = (EXPIRED, STOPPED, RELEASED)  # queued again so: the device's late answer for it still counts (open_for)
TROUBLE_HOURS = 24  # a song whose start failed this lately comes after the others in a new session


class RecordingError(ValueError):
    """Not possible (for the user): no such device, already recording, nothing to record, a wrong code."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(t: datetime) -> str:
    """UTC, for the device (API)."""
    return t.isoformat(timespec="seconds").replace("+00:00", "Z")


def _stamp(delta: timedelta = timedelta()) -> str:
    """Local time, as Echolot stores times (and its pages show them)."""
    return (datetime.now() + delta).isoformat(timespec="seconds")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ---------------------------------------------------------------- devices


def approve(con: sqlite3.Connection, user_id: int, name: str, app: str, challenge: str) -> str:
    """The user approved a device's connection (in the browser): a code for the app to exchange."""
    if not 43 <= len(challenge) <= 128:
        raise RecordingError("The app sent no valid challenge.")
    code = secrets.token_urlsafe(24)
    expires = _stamp(timedelta(minutes=CODE_MINUTES))
    with con:
        con.execute("DELETE FROM recorder_codes WHERE expires < ?", (_stamp(),))
        con.execute(
            "INSERT INTO recorder_codes (code, user_id, name, app, challenge, expires) VALUES (?, ?, ?, ?, ?, ?)",
            (_sha(code), user_id, _name(name), app[:80], challenge, expires),
        )
    return code


def exchange(
    con: sqlite3.Connection, code: str, verifier: str, name: str = "", app: str = ""
) -> tuple[str, sqlite3.Row]:
    """The app's token for an approved code (once, CODE_MINUTES) and its verifier: (token, device)."""
    row = con.execute("SELECT * FROM recorder_codes WHERE code = ?", (_sha(code),)).fetchone()
    if row is None or row["expires"] < _stamp():
        raise RecordingError("Unknown or expired code: connect again.")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    if not secrets.compare_digest(challenge, row["challenge"]):
        raise RecordingError("The code does not belong to this app.")
    token = TOKEN + secrets.token_urlsafe(32)
    with con:
        con.execute("DELETE FROM recorder_codes WHERE code = ?", (row["code"],))
        cur = con.execute(
            "INSERT INTO recorders (user_id, name, token, app, created) VALUES (?, ?, ?, ?, ?)",
            (row["user_id"], _name(name or row["name"]), _sha(token), (app or row["app"])[:80], _stamp()),
        )
    return token, con.execute("SELECT * FROM recorders WHERE id = ?", (cur.lastrowid,)).fetchone()


def by_token(con: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """The device of a token, with its user's name and rights (a disabled user's devices answer to nothing)."""
    if not token.startswith(TOKEN):
        return None
    return con.execute(
        "SELECT r.*, u.name AS user_name, u.admin, u.permissions FROM recorders r JOIN users u ON u.id = r.user_id "
        "WHERE r.token = ? AND NOT u.disabled",
        (_sha(token),),
    ).fetchone()


def devices(con: sqlite3.Connection, user_id: int) -> list[dict]:
    """The user's devices, the one seen last first (online ones lead), each with whether it is online and its
    session."""
    since = _stamp(-timedelta(seconds=ONLINE_SECONDS))
    out = []
    sql = "SELECT * FROM recorders WHERE user_id = ? ORDER BY last_seen IS NULL, last_seen DESC, id DESC"
    for r in con.execute(sql, (user_id,)):
        d = dict(r) | {"online": bool(r["last_seen"] and r["last_seen"] >= since)}
        session = active(con, r["id"])
        d["session"] = summary(con, session["id"]) if session else None
        out.append(d)
    return out


def device(con: sqlite3.Connection, user_id: int, recorder_id: int) -> sqlite3.Row:
    row = con.execute("SELECT * FROM recorders WHERE id = ? AND user_id = ?", (recorder_id, user_id)).fetchone()
    if row is None:
        raise RecordingError("No such device.")
    return row


def rename(con: sqlite3.Connection, user_id: int, recorder_id: int, name: str) -> None:
    device(con, user_id, recorder_id)
    if not name.strip():
        raise RecordingError("A device needs a name.")
    with con:
        con.execute("UPDATE recorders SET name = ? WHERE id = ?", (_name(name), recorder_id))


def disconnect(con: sqlite3.Connection, recorder_id: int) -> None:
    """The device and its sessions go; its token stops working at once."""
    with con:
        con.execute("DELETE FROM recorders WHERE id = ?", (recorder_id,))


def seen(con: sqlite3.Connection, recorder_id: int, state: str, detail: str, spotify: str, app: str) -> None:
    """What the device reported (it asks for work): its state, Spotify's, the app's version."""
    sql = "UPDATE recorders SET last_seen = ?, state = ?, detail = ?, spotify = ?, app = coalesce(nullif(?, ''), app) WHERE id = ?"
    with con:
        con.execute(sql, (_stamp(), state[:20], detail[:200], spotify[:20], app[:80], recorder_id))


def _name(name: str) -> str:
    return " ".join(name.split())[:60] or "Recorder"


# ---------------------------------------------------------------- what to record


@dataclass
class Track:
    """A song to record: its key, the Spotify URI that plays it here (the release Spotify relinks it to:
    the list's own can start the album one song off), its names, length (s), ISRC, and what the library has."""

    key: str
    uri: str
    artist: str
    artists: list[str]
    title: str
    album: str
    length: float
    isrc: str
    have: str

    def item(self) -> dict:
        d = asdict(self)
        d["duration_ms"] = round(d.pop("length") * 1000)
        return d


def candidates(con: sqlite3.Connection, user_id: int, list_key: str | None = None, held: bool = False) -> list[Track]:
    """Every song of the user's Spotify lists (or of that one, whoever follows it: the pages decide who may see
    it) the library has no genuine lossless copy of, worst first (then in the lists' order); each recording once
    (the same song, file or ISRC in several lists); not what Spotify plays nowhere (or greys out), nor what
    played as another song in the last SKIP_DAYS, nor (but with `held`) what Spotify has nothing better of
    (blocks)."""
    songs = (
        "SELECT s.key, s.artist, s.artists, s.title, s.album, s.length, s.isrc, s.file, s.close_match, s.unavailable, "
        "f.quality, a.state, a.detail FROM lists l JOIN list_songs ls ON ls.list_key = l.key "
        "JOIN songs s ON s.key = ls.song_key LEFT JOIN files f ON f.path = s.file "
        "LEFT JOIN availability a ON a.song_key = s.key"
    )
    spotify = "l.service = 'spotify' AND s.key LIKE 'spotify:%'"
    if list_key:
        sql, args = f"{songs} WHERE l.key = ? AND {spotify} ORDER BY ls.position", (list_key,)
    else:
        mine = f"{songs} JOIN sources src ON src.key = l.key WHERE src.user_id = ? AND src.enabled AND {spotify}"
        sql, args = f"{mine} ORDER BY src.position, ls.position", (user_id,)
    rows = con.execute(sql, args).fetchall()
    since = _stamp(-timedelta(days=SKIP_DAYS))
    recent = (
        "SELECT i.song_key FROM recording_items i JOIN recordings r ON r.id = i.recording_id "
        f"WHERE r.user_id = ? AND i.result IN ({', '.join('?' * len(SKIPPED_FOR_DAYS))}) AND i.finished >= ?"
    )
    skipped = {r[0] for r in con.execute(recent, (user_id, *SKIPPED_FOR_DAYS, since))}
    if not held:
        skipped |= set(blocks(con))
    seen_marks: set[str] = set()
    out = []
    for r in rows:
        marks = {f"key:{r['key']}"} | {
            m for m in (r["file"] and f"file:{r['file']}", r["isrc"] and f"isrc:{r['isrc']}") if m
        }
        if marks & seen_marks:
            continue
        seen_marks |= marks
        have = _have(r)
        if (
            have == "lossless"
            or r["key"] in skipped
            or r["unavailable"]
            or r["state"] in ("taken_down", "blocked", "gone")
        ):
            continue
        sid = r["key"].removeprefix("spotify:")
        if r["state"] == "replaced" and (r["detail"] or "").startswith("spotify:"):
            sid = r["detail"].removeprefix("spotify:")
        artists = json.loads(r["artists"]) if r["artists"] else [r["artist"]]
        names = (r["artist"], artists, r["title"], r["album"] or "")
        out.append(Track(r["key"], f"spotify:track:{sid}", *names, r["length"] or 0, r["isrc"] or "", have))
    return sorted(out, key=lambda t: WORST.index(t.have))


def blocks(con: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """The songs Spotify has nothing better of (song key -> spotify_best row): those it still plays as the track
    that was recorded (a song Spotify relinks to another release since may be better there)."""
    plays = PLAYS_AS.format(key="b.song_key")
    sql = f"SELECT b.* FROM spotify_best b LEFT JOIN availability a ON a.song_key = b.song_key WHERE b.uri = {plays}"
    return {r["song_key"]: r for r in con.execute(sql)}


def block(con: sqlite3.Connection, key: str, uri: str | None, reason: str, detail: str = "") -> None:
    """Spotify has nothing better of a song than the library (BEST): it is not offered for recording again while
    Spotify plays it as `uri` (None: as it does now)."""
    if not uri:
        plays = PLAYS_AS.format(key="?")
        row = con.execute(f"SELECT {plays} FROM (SELECT 1) LEFT JOIN availability a ON a.song_key = ?", (key, key))
        uri = row.fetchone()[0]
    with con:
        con.execute(
            "INSERT OR REPLACE INTO spotify_best (song_key, uri, reason, detail, since) VALUES (?, ?, ?, ?, ?)",
            (key, uri, reason, detail[:300], _stamp()),
        )


def unblock(con: sqlite3.Connection, key: str) -> bool:
    """Offer a song for recording again (the user wants Spotify tried again)."""
    with con:
        return con.execute("DELETE FROM spotify_best WHERE song_key = ?", (key,)).rowcount > 0


def held_back(con: sqlite3.Connection, user_id: int, list_key: str | None = None) -> int:
    """How many songs of a choice are left out because Spotify has nothing better of them."""
    return len(candidates(con, user_id, list_key, held=True)) - len(candidates(con, user_id, list_key))


def _have(row: sqlite3.Row) -> str:
    """What the library has of a song: missing (also when only a close match covers it), or its file's tier."""
    if not row["file"] or row["close_match"]:
        return "missing"
    return row["quality"] or "missing"


def select(tracks: list[Track], below: str) -> list[Track]:
    """The tracks of a choice (BELOW)."""
    return [t for t in tracks if t.have in BELOW[below]]


def choices(tracks: list[Track]) -> list[dict]:
    """Each choice with how many songs and how long: for the Record dialog."""
    out = []
    for below, label in BELOW_LABELS.items():
        mine = [t for t in tracks if t.have in BELOW[below]]
        out.append({"below": below, "label": label, "songs": len(mine), "seconds": sum(t.length for t in mine)})
    return out


# ---------------------------------------------------------------- sessions


def start(con: sqlite3.Connection, user_id: int, recorder_id: int, list_key: str | None, below: str) -> int:
    """A new session on the user's device: every song of the choice, fixed now. RecordingError when the device
    is another's or busy with a session, or there is nothing to record."""
    if below not in BELOW:
        raise RecordingError("Choose what to record.")
    device(con, user_id, recorder_id)
    tracks = select(candidates(con, user_id, list_key), below)
    if not tracks:
        raise RecordingError("Nothing to record: every song is there in that quality.")
    if old := active(con, recorder_id):  # one session per device: the new one replaces it
        control(con, old["id"], "stop")
    since = _stamp(-timedelta(hours=TROUBLE_HOURS))
    failed = (  # songs whose start failed lately: a try counted, not done
        "SELECT i.song_key FROM recording_items i JOIN recordings r ON r.id = i.recording_id WHERE r.user_id = ? "
        "AND r.created >= ? AND (i.result = 'failed' OR (i.state = 'queued' AND i.tries > 0))"
    )
    trouble = {r[0] for r in con.execute(failed, (user_id, since))}
    tracks.sort(key=lambda t: t.key in trouble)  # (stable: worst first otherwise)
    name = "All Spotify lists"
    if list_key:
        row = con.execute("SELECT title FROM lists WHERE key = ?", (list_key,)).fetchone()
        name = row["title"] if row else list_key
    with con:
        cur = con.execute(
            "INSERT INTO recordings (user_id, recorder_id, title, list_key, below, state, created) "
            "VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (user_id, recorder_id, f"{name} · {BELOW_LABELS[below]}", list_key, below, _stamp()),
        )
        con.executemany(
            "INSERT INTO recording_items (recording_id, position, song_key, state) VALUES (?, ?, ?, 'queued')",
            [(cur.lastrowid, n, t.key) for n, t in enumerate(tracks)],
        )
    return cur.lastrowid


def active(con: sqlite3.Connection, recorder_id: int) -> sqlite3.Row | None:
    """The device's session that runs or is paused."""
    return con.execute(
        "SELECT * FROM recordings WHERE recorder_id = ? AND state IN ('running', 'paused') ORDER BY id DESC LIMIT 1",
        (recorder_id,),
    ).fetchone()


def latest(con: sqlite3.Connection, user_id: int, list_key: str | None = None, hours: int = 24) -> sqlite3.Row | None:
    """The user's session to show: one that runs or is paused, else the newest, if it ended in the last `hours`
    and was not dismissed (never an older one: closing a session's summary closes it). For a list page: that
    list's; None: any."""
    one = " AND list_key IS ?" if list_key is not None else ""
    args = (user_id, list_key) if list_key is not None else (user_id,)
    row = con.execute(
        f"SELECT * FROM recordings WHERE user_id = ?{one} ORDER BY state IN ('running', 'paused') DESC, id DESC LIMIT 1",
        args,
    ).fetchone()
    if row is None or row["state"] in ("running", "paused"):
        return row
    fresh = (row["ended"] or "") >= _stamp(-timedelta(hours=hours))
    return row if fresh and not row["dismissed"] else None


def control(con: sqlite3.Connection, session_id: int, action: str, reason: str = "by_user") -> None:
    """Pause, resume or stop a session, or dismiss an ended one. Stopping releases the song being recorded (queued,
    no try counted); its upload still comes in (open_for)."""
    s = con.execute("SELECT * FROM recordings WHERE id = ?", (session_id,)).fetchone()
    if s is None:
        raise RecordingError("No such session.")
    if action == "stop" and s["state"] in ("running", "paused"):
        leased = "SELECT id FROM recording_items WHERE recording_id = ? AND state = 'recording'"
        _release(con, [r[0] for r in con.execute(leased, (session_id,))], STOPPED)
    with con:
        if action == "pause" and s["state"] == "running":
            con.execute("UPDATE recordings SET state = 'paused', reason = ? WHERE id = ?", (reason, session_id))
        elif action == "resume" and s["state"] == "paused":
            con.execute("UPDATE recordings SET state = 'running', reason = '' WHERE id = ?", (session_id,))
        elif action == "stop" and s["state"] in ("running", "paused"):
            con.execute(
                "UPDATE recordings SET state = 'stopped', reason = '', ended = ? WHERE id = ?", (_stamp(), session_id)
            )
        elif action == "dismiss" and s["state"] in ("done", "stopped"):  # (and the user's older ones)
            sql = "UPDATE recordings SET dismissed = 1 WHERE user_id = ? AND id <= ? AND state IN ('done', 'stopped')"
            con.execute(sql, (s["user_id"], session_id))


def session_of(con: sqlite3.Connection, user_id: int, session_id: int) -> sqlite3.Row:
    row = con.execute("SELECT * FROM recordings WHERE id = ? AND user_id = ?", (session_id, user_id)).fetchone()
    if row is None:
        raise RecordingError("No such session.")
    return row


def summary(con: sqlite3.Connection, session_id: int) -> dict:
    """A session as the device and the pages show it: its state, its songs' counts and lengths (s: all, done)."""
    s = con.execute("SELECT * FROM recordings WHERE id = ?", (session_id,)).fetchone()
    counts: dict[str, int] = {}
    left = everything = 0.0
    for r in con.execute(
        "SELECT i.state, i.result, s.length FROM recording_items i LEFT JOIN songs s ON s.key = i.song_key "
        "WHERE i.recording_id = ?",
        (session_id,),
    ):
        counts[r["result"] or r["state"]] = counts.get(r["result"] or r["state"], 0) + 1
        everything += r["length"] or 0
        if r["state"] != "done":
            left += r["length"] or 0
    total = sum(counts.values())
    n = counts.get
    out = {"id": s["id"], "title": s["title"], "state": s["state"], "reason": s["reason"], "list": s["list_key"]}
    out |= {"created": s["created"], "ended": s["ended"]}
    out |= {"total": total, "done": total - n("queued", 0) - n("recording", 0), "filed": n("filed", 0)}
    out |= {"not_better": n("not_better", 0), "skipped": sum(n(k, 0) for k in (*SKIPS, "found"))}
    out |= {"found": n("found", 0), "lossy_only": n("lossy_only", 0), "wrong_song": n("wrong_song", 0)}
    out |= {"failed": n("failed", 0) + n("rejected", 0), "minutes_left": round(left / 60)}
    out |= {"seconds_total": round(everything), "seconds_done": round(everything - left)}
    return out | {"recording": current(con, session_id)}


TIME = re.compile(r"(\d+):(\d{2}) / (\d+):(\d{2})")  # a recording device's detail: where it is in the song


def clock(device: dict) -> dict | None:
    """Where a recording device is in its song now: its last word ("1:12 / 3:45", sent every 30 s) and the
    time since (s: at, total); None when it records nothing."""
    m = TIME.search(device.get("detail") or "")
    if device.get("state") != "recording" or not m or not device.get("last_seen"):
        return None
    at, total = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
    since = (datetime.now() - datetime.fromisoformat(device["last_seen"])).total_seconds()
    return {"at": round(min(at + max(since, 0), total)), "total": total}


def current(con: sqlite3.Connection, session_id: int) -> dict | None:
    """The song being recorded: artist and title."""
    row = con.execute(
        "SELECT s.artist, s.title FROM recording_items i JOIN songs s ON s.key = i.song_key "
        "WHERE i.recording_id = ? AND i.state = 'recording' LIMIT 1",
        (session_id,),
    ).fetchone()
    return dict(row) if row else None


def items(con: sqlite3.Connection, session_id: int) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT i.*, s.artist, s.title FROM recording_items i LEFT JOIN songs s ON s.key = i.song_key "
        "WHERE i.recording_id = ? ORDER BY i.position",
        (session_id,),
    ).fetchall()


def hand_out(con: sqlite3.Connection, recorder_id: int) -> dict | None:
    """The next song of the device's running session (the item for the device), leased to it; None: nothing to
    do. Leases that ran out are queued again first; a song that got better meanwhile is skipped (found); a
    session without songs left is done."""
    s = active(con, recorder_id)
    if s is None:
        return None
    _expire(con, s["id"])
    if s["state"] != "running":
        return None
    while True:
        row = con.execute(
            "SELECT * FROM recording_items WHERE recording_id = ? AND state = 'queued' ORDER BY position LIMIT 1",
            (s["id"],),
        ).fetchone()
        if row is None:
            _end_if_done(con, s["id"])
            return None
        track = next((t for t in candidates(con, s["user_id"], s["list_key"]) if t.key == row["song_key"]), None)
        if track is None or track.have not in BELOW[s["below"]]:
            _finish(con, row["id"], *_gone(con, row["song_key"]))
            continue
        until = _now() + timedelta(seconds=track.length + LEASE_SLACK)
        with con:
            sql = "UPDATE recording_items SET state = 'recording', tries = tries + 1, lease_until = ?, uri = ? WHERE id = ?"
            con.execute(sql, (until.timestamp(), track.uri, row["id"]))
        out = track.item() | {"id": row["id"], "lease_until": _iso(until)}
        return out | {
            "upload": f"/api/recorder/items/{row['id']}/file",
            "result": f"/api/recorder/items/{row['id']}/result",
        }


def _gone(con: sqlite3.Connection, key: str) -> tuple[str, str]:
    """Why a session's song is no longer one to record: found (the library has it in the quality now),
    not_better (Spotify has nothing better of it, as another session found), or failed (Spotify no longer plays
    it, or it left the user's lists)."""
    if (held := blocks(con).get(key)) is not None:
        return "not_better", f"Spotify has nothing better: {BEST.get(held['reason'], held['reason'])}"
    row = con.execute(
        "SELECT s.file, s.close_match, f.quality FROM songs s LEFT JOIN files f ON f.path = s.file WHERE s.key = ?",
        (key,),
    ).fetchone()
    if row is not None and _have(row) != "missing":
        return "found", "Echolot found it meanwhile"
    return "failed", "no longer to record: Spotify no longer plays it, or it left your lists"


def item_of(con: sqlite3.Connection, recorder_id: int, item_id: int) -> sqlite3.Row:
    """An item of one of the device's sessions (with its session's user and state)."""
    row = con.execute(
        "SELECT i.*, r.user_id, r.state AS session_state, r.below FROM recording_items i "
        "JOIN recordings r ON r.id = i.recording_id WHERE i.id = ? AND r.recorder_id = ?",
        (item_id, recorder_id),
    ).fetchone()
    if row is None:
        raise RecordingError("No such song in this device's sessions.")
    return row


def open_for(item: sqlite3.Row) -> bool:
    """Whether the device may still answer for an item: handed to it, or let go of (LATE: its lease ran out, its
    session was stopped, the device restarted) and neither handed out again nor answered for since (an upload
    retried for a while, or after a restart, still comes in)."""
    late = item["detail"] in LATE and (item["state"] == "queued" or item["result"] == "failed")
    return item["state"] == "recording" or late


def fresh(con: sqlite3.Connection, recorder_id: int) -> int:
    """The device (re)started: whatever it was recording is released (queued, no try counted). How many."""
    sql = (
        "SELECT i.id FROM recording_items i JOIN recordings r ON r.id = i.recording_id "
        "WHERE r.recorder_id = ? AND i.state = 'recording'"
    )
    ids = [r[0] for r in con.execute(sql, (recorder_id,))]
    _release(con, ids, RELEASED)
    return len(ids)


def _release(con: sqlite3.Connection, item_ids: list[int], why: str) -> None:
    """Items handed out, queued again as they were (the try they were handed out for does not count)."""
    if not item_ids:
        return
    marks = ", ".join("?" * len(item_ids))
    sql = (
        "UPDATE recording_items SET state = 'queued', tries = max(tries - 1, 0), lease_until = NULL, detail = ? "
        f"WHERE id IN ({marks}) AND state = 'recording'"
    )
    with con:
        con.execute(sql, (why, *item_ids))


def answer(
    con: sqlite3.Connection, item: sqlite3.Row, result: str, detail: str = "", tier: str = "", stream: str = ""
) -> None:
    """What became of a song handed out: done (UPLOADED, SKIPS), queued again (AGAIN: until it was handed out
    TRIES times), or queued again as it was and its session paused (took_over: the user played Spotify, or
    stopped the device; a session paused already keeps its reason, by_user from the device's own Stop).
    Spotify has nothing better of the song (block) when it plays it lossy only (lossy_only), or when a
    recording that is not genuine lossless (`tier`; `stream`: lossy, as the device heard it) was filed or no
    better than the library's copy."""
    if result in (*UPLOADED, *SKIPS):
        _finish(con, item["id"], result, detail)
        uploaded = result in ("filed", "not_better") and tier and tier != "lossless"
        if result == "lossy_only" or uploaded:
            reason = (
                "lossy"
                if result == "lossy_only" or stream == "lossy"
                else "same"
                if result == "not_better"
                else "recorded"
            )
            block(con, item["song_key"], item["uri"], reason, detail)
    elif result == "took_over":
        sql = "UPDATE recording_items SET state = 'queued', tries = tries - 1, lease_until = NULL WHERE id = ?"
        with con:
            con.execute(sql, (item["id"],))
        control(con, item["recording_id"], "pause", "took_over")
    elif result in AGAIN:
        _again(con, item["id"], detail)
    else:
        raise RecordingError(f"Unknown result {result!r}.")
    _end_if_done(con, item["recording_id"])


def _again(con: sqlite3.Connection, item_id: int, detail: str) -> None:
    """A song that failed: failed once it was tried TRIES times, else queued again at the end of its session (it
    does not hold up the others)."""
    row = con.execute("SELECT tries, recording_id FROM recording_items WHERE id = ?", (item_id,)).fetchone()
    if row["tries"] >= TRIES:
        _finish(con, item_id, "failed", detail)
    else:
        last = "SELECT max(position) + 1 FROM recording_items WHERE recording_id = ?"
        end = con.execute(last, (row["recording_id"],)).fetchone()[0]
        with con:
            sql = (
                "UPDATE recording_items SET state = 'queued', lease_until = NULL, detail = ?, position = ? WHERE id = ?"
            )
            con.execute(sql, (detail[:300], end, item_id))


def _finish(con: sqlite3.Connection, item_id: int, result: str, detail: str) -> None:
    with con:
        con.execute(
            "UPDATE recording_items SET state = 'done', result = ?, detail = ?, lease_until = NULL, finished = ? WHERE id = ?",
            (result, detail[:300], _stamp(), item_id),
        )


def _expire(con: sqlite3.Connection, session_id: int) -> None:
    """Songs whose lease ran out (no answer from the device): queued again, or failed after TRIES."""
    late = con.execute(
        "SELECT id FROM recording_items WHERE recording_id = ? AND state = 'recording' AND lease_until < ?",
        (session_id, _now().timestamp()),
    ).fetchall()
    for r in late:
        _again(con, r["id"], EXPIRED)


def _end_if_done(con: sqlite3.Connection, session_id: int) -> None:
    left = "SELECT 1 FROM recording_items WHERE recording_id = ? AND state != 'done'"
    if not con.execute(left, (session_id,)).fetchone():
        with con:
            con.execute(
                "UPDATE recordings SET state = 'done', reason = '', ended = ? WHERE id = ? AND state IN ('running', 'paused')",
                (_stamp(), session_id),
            )


def export(con: sqlite3.Connection, user_id: int, user: str, base: str, list_key: str | None, below: str) -> dict:
    """The songs of a choice as a file for a recorder without a connection (it records them by itself)."""
    if below not in BELOW:
        raise RecordingError("Choose what to record.")
    name = "All Spotify lists"
    if list_key:
        row = con.execute("SELECT title FROM lists WHERE key = ?", (list_key,)).fetchone()
        name = row["title"] if row else list_key
    tracks = select(candidates(con, user_id, list_key), below)
    out = {"echolot": base, "user": user, "title": f"{name} · {BELOW_LABELS[below]}", "below": below}
    return out | {"exported": _iso(_now()), "tracks": [t.item() for t in tracks]}
