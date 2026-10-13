"""
Last.fm client.

Why Last.fm? Spotify's API caps development apps at a handful of users. Last.fm
has no such cap: the user connects Spotify to Last.fm (scrobbling) once, and we
read the public scrobble feed with a single app-wide API key.

Data flow:  profiles.lastfm_username  ->  user.getRecentTracks  ->  normalized JSON
"""
import re
import requests
from config import Config

API_ROOT = "https://ws.audioscrobbler.com/2.0/"
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,14}$")  # mirrors the SQL check


class LastFmError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.message = message
        self.status = status


def _call(method, **params):
    """Low-level GET against the Last.fm API with consistent error handling."""
    try:
        resp = requests.get(
            API_ROOT,
            params={"method": method, "api_key": Config.LASTFM_API_KEY,
                    "format": "json", **params},
            timeout=10,
        )
        data = resp.json()
    except (requests.RequestException, ValueError):
        raise LastFmError("Live tracking is temporarily unavailable. Try again in a moment.")

    if "error" in data:
        code = data["error"]
        if code == 6:
            raise LastFmError("We couldn't find that username. Check the spelling and try again.", status=404)
        if code == 17:
            raise LastFmError("That account's listening history is private. Make it public in its privacy settings, then try again.", status=403)
        if code == 29:
            raise LastFmError("Too many requests right now. Try again shortly.", status=429)
        raise LastFmError("Live tracking hit a problem. Try again in a moment.")
    return data


def is_valid_username(username):
    return bool(username and USERNAME_RE.match(username))


def verify_user(username):
    """Confirms the account exists (user.getInfo). Returns its canonical name."""
    data = _call("user.getInfo", user=username)
    return data["user"]["name"]


def _normalize_track(raw):
    """Flattens Last.fm's awkward shape ('#text' keys, '@attr') for the UI."""
    images = raw.get("image") or []
    art = next((i["#text"] for i in reversed(images) if i.get("#text")), None)
    date = raw.get("date")
    return {
        "name": raw.get("name"),
        "artist": (raw.get("artist") or {}).get("#text"),
        "album": (raw.get("album") or {}).get("#text"),
        "url": raw.get("url"),
        "image": art,
        # Last.fm marks the live track with @attr.nowplaying = "true" and
        # gives it no timestamp.
        "now_playing": (raw.get("@attr") or {}).get("nowplaying") == "true",
        "played_at": int(date["uts"]) if date and date.get("uts") else None,
    }


def get_recent_tracks(username, limit=20):
    """Calls user.getRecentTracks and returns {'now_playing': ..., 'recent': [...]}."""
    limit = max(1, min(int(limit), 50))
    data = _call("user.getRecentTracks", user=username, limit=limit, extended=0)

    raw_tracks = data.get("recenttracks", {}).get("track", [])
    if isinstance(raw_tracks, dict):  # Last.fm returns a bare object when there is 1 track
        raw_tracks = [raw_tracks]

    tracks = [_normalize_track(t) for t in raw_tracks]
    now_playing = next((t for t in tracks if t["now_playing"]), None)
    recent = [t for t in tracks if not t["now_playing"]]
    return {"now_playing": now_playing, "recent": recent}


# ---- Top lists (plays only; Last.fm does not track minutes) -----------------
PERIODS = {"7day", "1month", "3month", "6month", "12month", "overall"}


def _as_list(value):
    if isinstance(value, dict):  # Last.fm returns a bare object when there is 1 item
        return [value]
    return value or []


def get_top(username, period="overall", limit=10):
    """Top artists, tracks and albums for a preset period, by play count."""
    if period not in PERIODS:
        raise LastFmError("Unknown period.", status=400)
    limit = max(1, min(int(limit), 50))
    args = {"user": username, "period": period, "limit": limit}

    artists = _call("user.getTopArtists", **args).get("topartists", {}).get("artist", [])
    tracks = _call("user.getTopTracks", **args).get("toptracks", {}).get("track", [])
    albums = _call("user.getTopAlbums", **args).get("topalbums", {}).get("album", [])

    return {
        "period": period,
        "artists": [{"name": a.get("name"), "plays": int(a.get("playcount", 0))}
                    for a in _as_list(artists)],
        "tracks": [{"name": t.get("name"), "artist": (t.get("artist") or {}).get("name"),
                    "plays": int(t.get("playcount", 0))} for t in _as_list(tracks)],
        "albums": [{"name": a.get("name"), "artist": (a.get("artist") or {}).get("name"),
                    "plays": int(a.get("playcount", 0))} for a in _as_list(albums)],
    }
