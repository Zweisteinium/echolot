"""Public YouTube and YouTube Music playlists, read without an account through YouTube Music's web API
(ytmusicapi): per song its video, the title and artists as YouTube Music names the song (not the video's
title with its "(Official Video)"), album, length and kind: the release's own audio ("Provided to
YouTube", atv), an official video (omv, often longer than the release) or an upload (ugc: its title and
channel as uploaded). YouTube Music leaves out the videos that no longer play (deleted, private, blocked
here); yt-dlp's listing still has them (lists.youtube), and state tells why one does not play."""

import urllib.parse
from typing import Any

import requests

ACCOUNT_LISTS = {"LL": "your liked videos", "LM": "your liked music", "WL": "Watch later"}
KINDS = {"MUSIC_VIDEO_TYPE_ATV": "atv", "MUSIC_VIDEO_TYPE_OMV": "omv", "MUSIC_VIDEO_TYPE_OFFICIAL_SOURCE_MUSIC": "omv"}


class YouTubeError(Exception):
    """A list YouTube does not hand out (private, deleted) or a failed request."""


def playlist_id(url: str) -> str | None:
    """The list= of a YouTube or YouTube Music link."""
    return (urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("list") or [None])[0]


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def playlist(pid: str) -> dict[str, Any]:
    """A playlist's title, image (its largest thumbnail), author and the songs that play, in list order: id,
    title, artists, album, length (s), kind."""
    from ytmusicapi import YTMusic
    from ytmusicapi.exceptions import YTMusicError

    try:
        data = YTMusic().get_playlist(pid, limit=None)
    except (YTMusicError, requests.RequestException, KeyError, TypeError) as e:  # Key/TypeError: a private list
        raise YouTubeError(f"not readable: private, deleted or no answer ({str(e)[:80]})") from e
    songs = []
    for t in data.get("tracks") or []:
        if not t.get("videoId") or t.get("isAvailable") is False:
            continue
        artists = [a["name"] for a in t.get("artists") or [] if a.get("name")]
        album = (t.get("album") or {}).get("name") or ""
        kind = KINDS.get(t.get("videoType") or "", "ugc")
        song = {"id": t["videoId"], "title": t.get("title") or "", "artists": artists, "album": album}
        songs.append(song | {"length": t.get("duration_seconds") or 0, "kind": kind})
    thumbs = data.get("thumbnails") or []
    author = (data.get("author") or {}).get("name") if isinstance(data.get("author"), dict) else data.get("author")
    return {"title": data.get("title") or pid, "image": thumbs[-1]["url"] if thumbs else None, "songs": songs,
            "author": author or None}  # fmt: skip


def state(video_id: str) -> tuple[str, str | None] | None:
    """Whether a video plays (availability's states): available, blocked (not in this country) or gone
    (deleted, private, taken down for copyright: YouTube's reason as the detail); None when YouTube did not
    say (a failed request, a bot check)."""
    from ytmusicapi import YTMusic
    from ytmusicapi.exceptions import YTMusicError

    try:
        status = YTMusic().get_song(video_id).get("playabilityStatus") or {}
    except (YTMusicError, requests.RequestException, KeyError, TypeError):
        return None
    reason = status.get("reason") or ""
    if status.get("status") == "OK" or "confirm your age" in reason:
        return "available", None
    if "bot" in reason or not status.get("status"):
        return None
    return ("blocked", None) if "country" in reason else ("gone", reason[:200] or None)
