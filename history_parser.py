"""
Lifetime History Parser ("The JSON Hack").

Spotify lets every user download their data (Account -> Privacy -> Download your
data). No API key, no 5-user cap. Two export formats exist:

  Extended (EndSong / Streaming_History_Audio_*.json)
      {"ts": "2021-03-04T18:22:11Z", "ms_played": 183000,
       "master_metadata_album_artist_name": "Artist",
       "master_metadata_track_name": "Song",
       "master_metadata_album_album_name": "Album", ...}

  Standard (StreamingHistory*.json)   (no album information)
      {"endTime": "2021-03-04 18:22", "artistName": "Artist",
       "trackName": "Song", "msPlayed": 183000}

One pass over the streams produces TWO things:
  1. a summary (total time, skips, top artists) shown right after upload
  2. daily rows: one row per (day, artist, track, album) with plays and ms.
     These are stored in `listening_daily`, so any date range can be summed later.

Security notes: the file is parsed in memory only (never written to disk),
size is capped by Flask (MAX_CONTENT_LENGTH), json.loads never executes
anything, and every field is type-checked before use.
"""
import json
from collections import defaultdict
from datetime import date

MIN_PLAY_MS = 30_000  # streams shorter than this are treated as skips
MAX_TEXT = 300        # clip absurdly long names


class HistoryFormatError(ValueError):
    """Raised when an upload is not a recognizable Spotify history file."""


def load_entries(raw_bytes):
    """Decode bytes -> list of stream dicts, or raise HistoryFormatError."""
    try:
        data = json.loads(raw_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HistoryFormatError("That file is not valid JSON.")
    if not isinstance(data, list):
        raise HistoryFormatError("Expected a JSON array of streams.")
    return data


def _text(value):
    return value.strip()[:MAX_TEXT] if isinstance(value, str) else ""


def _day(value):
    """'2021-03-04T18:22:11Z' or '2021-03-04 18:22' -> '2021-03-04' (or None)."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _extract(entry):
    """Return {day, artist, track, album, ms} for a music stream, or None to ignore it."""
    if not isinstance(entry, dict):
        return None
    if "ms_played" in entry or "master_metadata_album_artist_name" in entry:  # extended
        artist = _text(entry.get("master_metadata_album_artist_name"))
        track = _text(entry.get("master_metadata_track_name"))
        album = _text(entry.get("master_metadata_album_album_name"))
        ms, day = entry.get("ms_played"), _day(entry.get("ts"))
    else:  # standard
        artist = _text(entry.get("artistName"))
        track = _text(entry.get("trackName"))
        album = ""
        ms, day = entry.get("msPlayed"), _day(entry.get("endTime"))

    # Podcasts/audiobooks have no artist; bools are ints in Python, so exclude them.
    if not artist or day is None:
        return None
    if isinstance(ms, bool) or not isinstance(ms, (int, float)) or ms < 0:
        return None
    return {"day": day, "artist": artist, "track": track or "(unknown track)",
            "album": album, "ms": int(ms)}


def analyze(entries, top_n=10):
    """
    Returns (summary, daily_rows).

    1. Drop streams under 30s (skips).
    2. Sum the remaining ms_played = total listening time.
    3. Group by artist for the Top N, and by (day, artist, track, album) for the daily rows.
    """
    total_ms = 0
    counted = skipped = ignored = 0
    per_artist = defaultdict(lambda: {"artist": "", "plays": 0, "ms": 0})
    daily = {}

    for entry in entries:
        s = _extract(entry)
        if s is None:
            ignored += 1
            continue
        if s["ms"] < MIN_PLAY_MS:
            skipped += 1
            continue

        counted += 1
        total_ms += s["ms"]

        bucket = per_artist[s["artist"].casefold()]  # "Drake" and "drake" merge
        bucket["artist"] = bucket["artist"] or s["artist"]
        bucket["plays"] += 1
        bucket["ms"] += s["ms"]

        key = (s["day"], s["artist"], s["track"], s["album"])
        row = daily.get(key)
        if row is None:
            daily[key] = {"day": s["day"], "artist": s["artist"], "track": s["track"],
                          "album": s["album"], "plays": 1, "ms": s["ms"]}
        else:
            row["plays"] += 1
            row["ms"] += s["ms"]

    if counted == 0 and skipped == 0:
        raise HistoryFormatError("No Spotify music streams found in this file.")

    top = sorted(per_artist.values(), key=lambda a: a["ms"], reverse=True)[:top_n]
    days = [r["day"] for r in daily.values()]
    summary = {
        "total_ms": total_ms,
        "total_hours": round(total_ms / 3_600_000, 1),
        "counted_streams": counted,
        "skipped_streams": skipped,
        "ignored_entries": ignored,  # podcasts, malformed rows
        "first_day": min(days) if days else None,
        "last_day": max(days) if days else None,
        "top_artists": top,
    }
    return summary, list(daily.values())


def parse_history_files(blobs, top_n=10):
    """Spotify splits big exports into several files; merge them all."""
    entries = []
    for blob in blobs:
        entries.extend(load_entries(blob))
    return analyze(entries, top_n=top_n)
