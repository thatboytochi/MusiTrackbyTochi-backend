"""Central configuration, read once from environment variables (backend/.env)."""
import os
from dotenv import load_dotenv

# override=True FORCES Python to read the fresh keys from your .env file
# and overwrite the old ones stuck in your terminal session memory.
load_dotenv(override=True)


def _need(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing {name} in backend/.env. Compare it with backend/.env.example.")
    return value


class Config:
    SUPABASE_URL = _need("SUPABASE_URL")
    SUPABASE_SERVICE_ROLE_KEY = _need("SUPABASE_SERVICE_ROLE_KEY")

    # Spotify app (developer.spotify.com/dashboard)
    SPOTIFY_CLIENT_ID = _need("SPOTIFY_CLIENT_ID")
    SPOTIFY_CLIENT_SECRET = _need("SPOTIFY_CLIENT_SECRET")
    # Must match a Redirect URI saved in the Spotify dashboard EXACTLY.
    SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:5000/api/spotify/callback")
    # Secret used to encrypt users' Spotify tokens before they are stored, and to sign login links.
    TOKEN_ENCRYPTION_KEY = _need("TOKEN_ENCRYPTION_KEY")

    FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")
    # Hardcoded to 50MB to safely support large multi-year Spotify history exports
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024