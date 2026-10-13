"""
Spotify connection: sign-in with Spotify (OAuth "Authorization Code" flow) plus
the three things the dashboard needs: now playing, recently played, top lists.

HOW THE SIGN-IN WORKS
  1. Browser asks us for a login link      GET  /api/spotify/login      (needs Supabase login)
  2. Browser goes to Spotify, user approves
  3. Spotify sends the browser back to us    GET  /api/spotify/callback?code=...&state=...
  4. We swap the code for tokens, encrypt them and save them in `spotify_connections`
  5. We send the browser back to the website

  The `state` value is a signed, 10-minute note that says WHICH MusiTrack user started
  step 1. It is how the callback (which has no Supabase login) knows whose account to link,
  and signing it stops anyone from forging it.

Spotify tokens never reach the browser. They are encrypted at rest.
"""
import base64
import time  # Added for timestamping the cache
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, URLSafeTimedSerializer

from config import Config
from supabase_client import supabase

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_ROOT = "https://api.spotify.com/v1"
# Ask for the minimum: what's playing now, what was played recently, top artists/tracks.
SCOPES = "user-read-currently-playing user-read-recently-played user-top-read"
TIME_RANGES = {"short_term", "medium_term", "long_term"}  # ~4 weeks, ~6 months, ~12 months
STATE_MAX_AGE = 600  # seconds

# ---------------------------------------------------------------- backend cache limits
_API_CACHE = {}
CACHE_TTL_LIVE = 60  # Hold 'Now Playing' & 'Recent' data for 60 seconds
CACHE_TTL_TOP = 3600  # Hold 'Top Artists & Tracks' for 1 hour


class SpotifyError(Exception):
    def __init__(self, message, status=502, code=None):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


class StateError(Exception):
    """The login link's `state` was missing, forged or too old."""


# ---------------------------------------------------------------- encryption / state
def _fernet():
    return Fernet(Config.TOKEN_ENCRYPTION_KEY.encode())


def _encrypt(text):
    return _fernet().encrypt(text.encode()).decode()


def _decrypt(token):
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise SpotifyError("Your saved Spotify login can't be read any more. Please connect again.", 409)


def _signer():
    return URLSafeTimedSerializer(Config.TOKEN_ENCRYPTION_KEY, salt="spotify-oauth-state")


def make_state(user_id):
    return _signer().dumps({"uid": user_id})


def read_state(state):
    try:
        return _signer().loads(state, max_age=STATE_MAX_AGE)["uid"]
    except (BadSignature, KeyError, TypeError):
        raise StateError()


def authorize_url(user_id):
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": Config.SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": Config.SPOTIFY_REDIRECT_URI,
        "scope": SCOPES,
        "state": make_state(user_id),
    })


# ---------------------------------------------------------------- talking to Spotify
def _token_request(data):
    basic = base64.b64encode(f"{Config.SPOTIFY_CLIENT_ID}:{Config.SPOTIFY_CLIENT_SECRET}".encode()).decode()
    try:
        resp = requests.post(TOKEN_URL, data=data, headers={"Authorization": f"Basic {basic}"}, timeout=10)
    except requests.RequestException:
        raise SpotifyError("Spotify isn't responding. Try again in a moment.", 503)

    if resp.status_code == 429:
        wait_time = resp.headers.get("Retry-After", "unknown")
        print(f"\n--- URGENT RATE LIMIT ---")
        print(f"Spotify says we must wait: {wait_time} seconds.")
        print(f"-------------------------\n")
        raise SpotifyError(f"Spotify asked us to slow down. Try again in {wait_time} seconds.", 429)

    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200:
        raise SpotifyError("Spotify refused the login.", 400, code=body.get("error"))
    return body


def _api_get(token, path, params=None):
    try:
        resp = requests.get(f"{API_ROOT}{path}", params=params,
                            headers={"Authorization": f"Bearer {token}"}, timeout=10)
    except requests.RequestException:
        raise SpotifyError("Spotify isn't responding. Try again in a moment.", 503)

    if resp.status_code == 204:
        return None  # e.g. nothing is playing
    if resp.status_code == 401:
        raise SpotifyError("Spotify login expired.", 401, code="token_expired")
    if resp.status_code == 403:
        raise SpotifyError(
            "Spotify blocked this account. While MusiTrack is in Spotify's test mode, only people "
            "added to the app's allow list can connect.", 403, code="not_allowed")
    if resp.status_code == 429:
        # Ask Spotify exactly how many seconds we are in timeout for
        wait_time = resp.headers.get("Retry-After", "unknown")
        print(f"\n--- URGENT RATE LIMIT ---")
        print(f"Spotify says we must wait: {wait_time} seconds.")
        print(f"-------------------------\n")
        raise SpotifyError(f"Spotify asked us to slow down. Try again in {wait_time} seconds.", 429)
    if resp.status_code >= 400:
        raise SpotifyError(f"Spotify returned an error ({resp.status_code}).", 502)
    return resp.json()


# ---------------------------------------------------------------- stored connection
def _load(user_id):
    res = supabase.table("spotify_connections").select("*").eq("user_id", user_id).limit(1).execute()
    return res.data[0] if res.data else None


def _expiry(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=int(seconds))).isoformat()


def connect(user_id, code):
    """Step 4: swap the one-time code for tokens and save them (encrypted)."""
    tokens = _token_request({"grant_type": "authorization_code", "code": code,
                             "redirect_uri": Config.SPOTIFY_REDIRECT_URI})
    me = _api_get(tokens["access_token"], "/me")  # also proves this person is allowed to use the app
    supabase.table("spotify_connections").upsert({
        "user_id": user_id,
        "spotify_user_id": me["id"],
        "display_name": me.get("display_name") or me["id"],
        "access_token_enc": _encrypt(tokens["access_token"]),
        "refresh_token_enc": _encrypt(tokens["refresh_token"]),
        "expires_at": _expiry(tokens.get("expires_in", 3600)),
        "scope": tokens.get("scope", SCOPES),
    }).execute()


def disconnect(user_id):
    supabase.table("spotify_connections").delete().eq("user_id", user_id).execute()


def connection_summary(user_id):
    """What the website may know: connected or not, and the display name. Never tokens."""
    res = (supabase.table("spotify_connections").select("display_name,connected_at")
           .eq("user_id", user_id).limit(1).execute())
    return res.data[0] if res.data else None


def access_token_for(user_id, force_refresh=False):
    """A working access token, refreshing it first if it is about to expire."""
    conn = _load(user_id)
    if not conn:
        raise SpotifyError("Connect Spotify first.", 409)
    expires = datetime.fromisoformat(conn["expires_at"].replace("Z", "+00:00"))
    if not force_refresh and expires - datetime.now(timezone.utc) > timedelta(seconds=60):
        return _decrypt(conn["access_token_enc"])

    try:
        tokens = _token_request({"grant_type": "refresh_token",
                                 "refresh_token": _decrypt(conn["refresh_token_enc"])})
    except SpotifyError as e:
        if e.code == "invalid_grant":  # the user removed MusiTrack in their Spotify account
            disconnect(user_id)
            raise SpotifyError("Your Spotify connection expired. Please connect again.", 409)
        raise
    update = {"access_token_enc": _encrypt(tokens["access_token"]),
              "expires_at": _expiry(tokens.get("expires_in", 3600))}
    if tokens.get("refresh_token"):  # Spotify sometimes rotates it
        update["refresh_token_enc"] = _encrypt(tokens["refresh_token"])
    supabase.table("spotify_connections").update(update).eq("user_id", user_id).execute()
    return tokens["access_token"]


def _get(user_id, path, params=None):
    token = access_token_for(user_id)
    try:
        return _api_get(token, path, params)
    except SpotifyError as e:
        if e.code != "token_expired":
            raise
        return _api_get(access_token_for(user_id, force_refresh=True), path, params)


# ---------------------------------------------------------------- shaping data for the website
def _image(images):
    if not images:
        return None
    return (images[1] if len(images) > 1 else images[0]).get("url")  # ~300px


def _unix(iso):
    if not iso:
        return None
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _track(t, played_at=None, now_playing=False):
    album = t.get("album") or {}
    return {
        "name": t.get("name"),
        "artist": ", ".join(a.get("name", "") for a in t.get("artists", [])),
        "album": album.get("name"),
        "url": (t.get("external_urls") or {}).get("spotify"),
        "image": _image(album.get("images")),
        "now_playing": now_playing,
        "played_at": played_at,
    }


def get_live(user_id, limit=20):
    """{'now_playing': track|None, 'recent': [tracks]}  (same shape the dashboard already uses)."""
    limit = max(1, min(int(limit), 50))

    # Check if we have fresh data saved for this user
    cache_key = f"live_{user_id}_{limit}"
    now = time.time()
    if cache_key in _API_CACHE:
        timestamp, cached_data = _API_CACHE[cache_key]
        if now - timestamp < CACHE_TTL_LIVE:
            return cached_data

    # If no fresh cache exists, ask Spotify
    playing = _get(user_id, "/me/player/currently-playing")
    recent = _get(user_id, "/me/player/recently-played", {"limit": limit})

    now_playing = None
    item = (playing or {}).get("item") or {}
    if playing and playing.get("is_playing") and item.get("type") == "track":
        now_playing = _track(item, now_playing=True)

    items = [_track(i["track"], played_at=_unix(i.get("played_at")))
             for i in (recent or {}).get("items", []) if i.get("track")]

    result = {"now_playing": now_playing, "recent": items}

    # Save the result to the cache before sending it back
    _API_CACHE[cache_key] = (now, result)
    return result


def _albums_from_tracks(tracks):
    """Spotify has no 'top albums' list, so rank albums by where their songs sit in the top tracks."""
    scores, first_seen = {}, {}
    n = len(tracks)
    for idx, t in enumerate(tracks):
        album = t.get("album") or {}
        if not album.get("name"):
            continue
        artist = ((album.get("artists") or [{}])[0]).get("name") or ""
        key = (album["name"], artist)
        scores[key] = scores.get(key, 0) + (n - idx)
        first_seen.setdefault(key, idx)
    ranked = sorted(scores, key=lambda k: (-scores[k], first_seen[k]))
    return [{"name": name, "artist": artist} for name, artist in ranked]


def get_top(user_id, time_range="medium_term", limit=10):
    """Spotify's own top artists / tracks for ~4 weeks, ~6 months or ~12 months. No play counts or minutes."""
    if time_range not in TIME_RANGES:
        raise SpotifyError("Unknown period.", 400)
    limit = max(1, min(int(limit), 50))

    # Check if we have fresh data saved for this user and time range
    cache_key = f"top_{user_id}_{time_range}_{limit}"
    now = time.time()
    if cache_key in _API_CACHE:
        timestamp, cached_data = _API_CACHE[cache_key]
        if now - timestamp < CACHE_TTL_TOP:
            return cached_data

    # If no fresh cache exists, ask Spotify
    artists = _get(user_id, "/me/top/artists", {"time_range": time_range, "limit": limit}) or {}
    tracks = (_get(user_id, "/me/top/tracks", {"time_range": time_range, "limit": 50}) or {}).get("items", [])

    result = {
        "period": time_range,
        "artists": [{"name": a.get("name")} for a in artists.get("items", [])],
        "tracks": [{"name": t.get("name"), "artist": ", ".join(a.get("name", "") for a in t.get("artists", []))}
                   for t in tracks[:limit]],
        "albums": _albums_from_tracks(tracks)[:limit],
    }

    # Save the result to the cache before sending it back
    _API_CACHE[cache_key] = (now, result)
    return result