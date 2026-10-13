"""
Auth guard.

The React app logs in through Supabase Auth and receives an access token (JWT).
It sends that token as `Authorization: Bearer <jwt>`. We ask Supabase to verify
it and tell us which auth.users.id it belongs to. That id is the ONLY identity
we trust: it is the key into `profiles` and `history_summaries`.

When verification fails we log the REAL reason in the backend terminal and tell
the browser whether the problem is the session or the backend's own setup.
"""
from functools import wraps

import httpx
from flask import current_app, g, jsonify, request
from supabase import AuthApiError

from supabase_client import supabase


def require_auth(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify(error="Missing bearer token."), 401
        token = header.removeprefix("Bearer ").strip()

        user = None
        try:
            result = supabase.auth.get_user(token)
            user = result.user if result else None
        except httpx.RequestError as e:
            # Could not even reach Supabase: almost always a wrong SUPABASE_URL.
            current_app.logger.error("Cannot reach Supabase: %r", e)
            return jsonify(error="The backend can't reach Supabase. Check SUPABASE_URL in backend/.env."), 503
        except AuthApiError as e:
            current_app.logger.warning("Supabase rejected the request: %s (status %s)", e.message, e.status)
            text = (e.message or "").lower()
            if "api key" in text or "apikey" in text:
                return jsonify(error="The backend's Supabase key is wrong. Check SUPABASE_SERVICE_ROLE_KEY in backend/.env."), 500
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("Token check failed: %r", e)

        if user is None:
            return jsonify(error="Session expired. Please sign in again."), 401
        g.user_id = user.id  # == profiles.id
        return view(*args, **kwargs)

    return wrapper
