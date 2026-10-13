"""
MusiTrackByTochi API.

Every route (except /api/health) requires a valid Supabase session. The verified
user id (g.user_id) is the join key across the whole data model:
  auth.users.id == profiles.id == history_summaries.user_id
"""
import threading
import time
from datetime import date, datetime
import requests

from flask import Flask, g, jsonify, redirect, request
from flask_cors import CORS

import history_parser
import spotify
from auth import require_auth
from config import Config
from supabase_client import supabase

ROW_CHUNK = 2000  # rows per insert request when saving history

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = Config.MAX_CONTENT_LENGTH
CORS(app, origins=[Config.FRONTEND_ORIGIN])

# Create a traffic lock to prevent spamming Spotify API on fresh cache loads
image_fetch_lock = threading.Lock()


def _first_row(res):
    """Returns the first row of a query result, or None if there are none."""
    return res.data[0] if res and res.data else None


def _get_profile(user_id):
    res = supabase.table("profiles").select("*").eq("id", user_id).limit(1).execute()
    profile = _first_row(res)
    if profile is None:
        res = supabase.table("profiles").upsert({"id": user_id}).execute()
        profile = _first_row(res)
    return profile


@app.get("/api/health")
def health():
    return jsonify(status="ok")


@app.get("/api/me")
@require_auth
def me():
    profile = _get_profile(g.user_id)
    summary = (supabase.table("history_summaries").select("*")
               .eq("user_id", g.user_id).limit(1).execute())
    return jsonify(profile=profile, history=_first_row(summary),
                   spotify=spotify.connection_summary(g.user_id))


@app.get("/api/spotify/login")
@require_auth
def spotify_login():
    return jsonify(url=spotify.authorize_url(g.user_id))


@app.get("/api/spotify/callback")
def spotify_callback():
    def back(status):
        return redirect(f"{Config.FRONTEND_ORIGIN}/?spotify={status}")

    if request.args.get("error"):
        return back("denied")
    try:
        user_id = spotify.read_state(request.args.get("state", ""))
    except spotify.StateError:
        return back("expired")
    try:
        spotify.connect(user_id, request.args.get("code", ""))
    except spotify.SpotifyError as e:
        app.logger.warning("Spotify connect failed: %s (%s)", e.message, e.code)
        return back("not_allowed" if e.code == "not_allowed" else "error")
    return back("connected")


@app.delete("/api/spotify/connection")
@require_auth
def spotify_disconnect():
    spotify.disconnect(g.user_id)
    return jsonify(ok=True)


@app.get("/api/live/recent")
@require_auth
def live_recent():
    try:
        return jsonify(spotify.get_live(g.user_id, request.args.get("limit", 20, type=int)))
    except spotify.SpotifyError as e:
        return jsonify(error=e.message), e.status


@app.get("/api/live/top")
@require_auth
def live_top():
    try:
        return jsonify(spotify.get_top(g.user_id, request.args.get("period", "medium_term"),
                                       request.args.get("limit", 10, type=int)))
    except spotify.SpotifyError as e:
        return jsonify(error=e.message), e.status


@app.post("/api/history/sync")
@require_auth
def sync_history():
    """Fetches recently played tracks from Spotify and safely aggregates them into daily totals."""
    try:
        profile = _get_profile(g.user_id)
        token = profile.get("spotify_access_token")
        last_sync = profile.get("last_sync_time") or 0

        if not token:
            return jsonify(status="skipped", message="Spotify not connected")

        headers = {"Authorization": f"Bearer {token}"}
        url = "https://api.spotify.com/v1/me/player/recently-played?limit=50"
        if last_sync > 0:
            url += f"&after={last_sync}"

        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            app.logger.error("Spotify API error: %s", resp.text)
            return jsonify(error="Failed to fetch from Spotify API"), 400

        recent_tracks = resp.json().get("items", [])
        if not recent_tracks:
            return jsonify(status="success", added=0)

        daily_aggregates = {}
        max_ts = last_sync

        for item in recent_tracks:
            played_at_str = item["played_at"]
            dt_str = played_at_str.replace('Z', '+00:00')
            dt = datetime.fromisoformat(dt_str)
            track_ts = int(dt.timestamp() * 1000)

            if track_ts <= last_sync:
                continue
            if track_ts > max_ts:
                max_ts = track_ts

            track = item.get("track")
            if not track:
                continue

            day = dt.strftime("%Y-%m-%d")
            artist = track["artists"][0]["name"] if track.get("artists") else "Unknown Artist"
            track_name = track.get("name", "Unknown Track")
            album = track["album"]["name"] if track.get("album") else "Unknown Album"
            ms = track.get("duration_ms", 0)

            key = (day, artist, track_name, album)
            if key not in daily_aggregates:
                daily_aggregates[key] = {"plays": 0, "ms": 0}

            daily_aggregates[key]["plays"] += 1
            daily_aggregates[key]["ms"] += ms

        if not daily_aggregates:
            return jsonify(status="success", added=0)

        days_to_fetch = list(set([k[0] for k in daily_aggregates.keys()]))
        existing_res = supabase.table("listening_daily").select("*").eq("user_id", g.user_id).in_("day",
                                                                                                  days_to_fetch).execute()

        existing_map = {}
        if existing_res.data:
            for row in existing_res.data:
                k = (row["day"], row["artist"], row["track"], row["album"])
                existing_map[k] = row

        upsert_payload = []
        for key, stats in daily_aggregates.items():
            day, artist, track_name, album = key
            ex = existing_map.get(key)
            new_plays = ex["plays"] + stats["plays"] if ex else stats["plays"]
            new_ms = ex["ms"] + stats["ms"] if ex else stats["ms"]

            upsert_payload.append({
                "user_id": g.user_id, "day": day, "artist": artist,
                "track": track_name, "album": album, "plays": new_plays, "ms": new_ms
            })

        supabase.table("listening_daily").upsert(upsert_payload, on_conflict="user_id,day,artist,track,album").execute()
        supabase.table("profiles").update({"last_sync_time": max_ts}).eq("id", g.user_id).execute()

        res = supabase.rpc("listening_stats", {"p_user": g.user_id}).execute()
        if res.data and "totals" in res.data:
            totals = res.data["totals"]
            summary_res = supabase.table("history_summaries").select("*").eq("user_id", g.user_id).limit(1).execute()
            summary = _first_row(summary_res) or {}
            supabase.table("history_summaries").upsert({
                "user_id": g.user_id,
                "total_ms": totals.get("ms", summary.get("total_ms", 0)),
                "counted_streams": totals.get("plays", summary.get("counted_streams", 0)),
                "skipped_streams": summary.get("skipped_streams", 0),
                "top_artists": res.data.get("top_artists", summary.get("top_artists", []))
            }).execute()

        return jsonify(status="success", added=sum(s["plays"] for s in daily_aggregates.values()))

    except Exception as e:
        app.logger.exception("Sync failed")
        return jsonify(error=f"Could not sync Spotify tracks: {e}"), 500


@app.post("/api/history/upload")
@require_auth
def upload_history():
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="Choose at least one Spotify history file."), 400
    if len(files) > 30:
        return jsonify(error="Upload 30 files or fewer at a time."), 400
    for f in files:
        if not (f.filename or "").lower().endswith(".json"):
            return jsonify(error=f"{f.filename} is not a .json file."), 400

    try:
        summary, rows = history_parser.parse_history_files([f.read() for f in files])
    except history_parser.HistoryFormatError as e:
        return jsonify(error=str(e)), 422

    try:
        for i in range(0, len(rows), ROW_CHUNK):
            chunk = [{**r, "user_id": g.user_id} for r in rows[i:i + ROW_CHUNK]]
            supabase.table("listening_daily").upsert(chunk).execute()
    except Exception as e:
        app.logger.exception("Saving listening_daily failed")
        return jsonify(error=f"Couldn't save your history. ({e})"), 500

    try:
        res = supabase.rpc("listening_stats", {"p_user": g.user_id}).execute()
        if res.data and "totals" in res.data:
            totals = res.data["totals"]
            summary["total_ms"] = totals.get("ms", summary["total_ms"])
            summary["counted_streams"] = totals.get("plays", summary["counted_streams"])
            summary["top_artists"] = res.data.get("top_artists", summary["top_artists"])
    except Exception:
        pass

    supabase.table("history_summaries").upsert({
        "user_id": g.user_id,
        "total_ms": summary["total_ms"],
        "counted_streams": summary["counted_streams"],
        "skipped_streams": summary["skipped_streams"],
        "top_artists": summary["top_artists"],
    }).execute()

    return jsonify(summary)


@app.delete("/api/history")
@require_auth
def delete_history():
    try:
        supabase.table("listening_daily").delete().eq("user_id", g.user_id).execute()
        supabase.table("history_summaries").delete().eq("user_id", g.user_id).execute()
        return jsonify(ok=True)
    except Exception as e:
        app.logger.exception("Failed to delete history")
        return jsonify(error="Could not delete history."), 500


def _parse_day(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("Dates must look like 2025-03-31.")


@app.get("/api/stats")
@require_auth
def stats():
    try:
        d_from, d_to = _parse_day(request.args.get("from")), _parse_day(request.args.get("to"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if d_from and d_to and d_from > d_to:
        return jsonify(error="The start date must be before the end date."), 400

    sort = request.args.get("sort", "minutes")
    if sort not in ("minutes", "plays"):
        return jsonify(error="sort must be 'minutes' or 'plays'."), 400
    limit = max(1, min(request.args.get("limit", 10, type=int), 50))

    res = supabase.rpc("listening_stats", {
        "p_user": g.user_id,
        "p_from": d_from.isoformat() if d_from else None,
        "p_to": d_to.isoformat() if d_to else None,
        "p_limit": limit,
        "p_sort": sort,
    }).execute()
    return jsonify(res.data)


@app.get("/api/stats/timeline")
@require_auth
def stats_timeline():
    try:
        res = supabase.rpc("listening_timeline", {"p_user": g.user_id}).execute()
        return jsonify(res.data)
    except Exception as e:
        app.logger.exception("Failed to fetch timeline stats")
        return jsonify(error="Could not load timeline data."), 500


@app.get("/api/image")
@require_auth
def get_image():
    """Hybrid Image Fetcher that actively repairs bad Deezer cache entries."""
    entity_type = request.args.get("type")
    name = request.args.get("name")
    artist = request.args.get("artist", "")

    if not entity_type or not name:
        return jsonify(error="Missing type or name"), 400

    # 1. Check Cache, but explicitly ignore Deezer or logo fallbacks
    cache_res = supabase.table("image_cache").select("image_url").eq(
        "entity_type", entity_type).eq("entity_name", name).eq("artist_name", artist).execute()

    if cache_res.data and len(cache_res.data) > 0:
        cached_url = cache_res.data[0]["image_url"]
        # If it is a real Spotify URL, return it instantly.
        # If it is a Deezer link or a logo, skip this and fetch a real one.
        if "dzcdn" not in cached_url and "deezer" not in cached_url and "logo-mark" not in cached_url:
            return jsonify(url=cached_url)

    image_url = None

    # 2. Fetch from Spotify API sequentially using a Threading Lock
    with image_fetch_lock:
        time.sleep(0.3)

        try:
            # THIS IS THE FIX: Securely request the decrypted, auto-refreshing token
            token = spotify.access_token_for(g.user_id)
            headers = {"Authorization": f"Bearer {token}"}

            if entity_type == "artists":
                query = requests.utils.quote(name)
                res = requests.get(f"https://api.spotify.com/v1/search?q={query}&type=artist&limit=10", headers=headers,
                                   timeout=5)
                if res.status_code == 200:
                    items = res.json().get("artists", {}).get("items", [])
                    if items:
                        best_match = sorted(items, key=lambda x: x.get("popularity", 0), reverse=True)[0]
                        if best_match.get("images"):
                            image_url = best_match["images"][0]["url"]

            elif entity_type == "tracks":
                query = requests.utils.quote(f"track:{name} artist:{artist}")
                res = requests.get(f"https://api.spotify.com/v1/search?q={query}&type=track&limit=10", headers=headers,
                                   timeout=5)
                if res.status_code == 200:
                    items = res.json().get("tracks", {}).get("items", [])
                    if items:
                        best_match = sorted(items, key=lambda x: x.get("popularity", 0), reverse=True)[0]
                        if best_match.get("album", {}).get("images"):
                            image_url = best_match["album"]["images"][0]["url"]

            elif entity_type == "albums":
                query = requests.utils.quote(f"album:{name} artist:{artist}")
                res = requests.get(f"https://api.spotify.com/v1/search?q={query}&type=album&limit=10", headers=headers,
                                   timeout=5)
                if res.status_code == 200:
                    items = res.json().get("albums", {}).get("items", [])
                    if items and items[0].get("images"):
                        image_url = items[0]["images"][0]["url"]

        except spotify.SpotifyError as e:
            app.logger.warning(f"Could not get Spotify token for images: {e.message}")
        except Exception as e:
            app.logger.warning(f"Spotify image fetch failed: {e}")

        # 3. Fallback to Deezer ONLY if Spotify completely fails (e.g., track removed from Spotify)
        if not image_url:
            try:
                if entity_type == "artists":
                    res = requests.get(f"https://api.deezer.com/search/artist?q={requests.utils.quote(name)}&limit=1",
                                       timeout=5).json()
                    if res.get("data"):
                        image_url = res["data"][0].get("picture_xl") or res["data"][0].get("picture_medium")
                elif entity_type == "tracks":
                    query = f"{name} {artist}"
                    res = requests.get(f"https://api.deezer.com/search/track?q={requests.utils.quote(query)}&limit=1",
                                       timeout=5).json()
                    if res.get("data"):
                        image_url = res["data"][0].get("album", {}).get("cover_xl") or res["data"][0].get("album",
                                                                                                          {}).get(
                            "cover_medium")
                elif entity_type == "albums":
                    query = f"{name} {artist}"
                    res = requests.get(f"https://api.deezer.com/search/album?q={requests.utils.quote(query)}&limit=1",
                                       timeout=5).json()
                    if res.get("data"):
                        image_url = res["data"][0].get("cover_xl") or res["data"][0].get("cover_medium")
            except Exception as e:
                app.logger.warning(f"Deezer fallback failed: {e}")

    # 4. Save whatever we found to the cache (Outside the lock)
    final_url = image_url or "/logo-mark.png"
    if image_url:
        try:
            # Delete any existing bad cached row for this specific item before saving the new one
            supabase.table("image_cache").delete().eq(
                "entity_type", entity_type).eq("entity_name", name).eq("artist_name", artist).execute()

            supabase.table("image_cache").insert({
                "entity_type": entity_type, "entity_name": name,
                "artist_name": artist, "image_url": image_url
            }).execute()
        except Exception:
            pass

    return jsonify(url=final_url)


@app.errorhandler(Exception)
def unexpected(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("Unhandled error")
    return jsonify(error=f"Server error: {e}"), 500


@app.errorhandler(413)
def too_large(_):
    return jsonify(error=f"File too large (limit {Config.MAX_CONTENT_LENGTH // 1_048_576} MB)."), 413


if __name__ == "__main__":
    app.run(port=5000, debug=True)