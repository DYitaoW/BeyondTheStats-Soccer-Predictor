"""Authentication and rate limiting for the API."""
import time
import threading
from collections import deque
from flask import request, jsonify
import config


def _client_ip():
    """Return the originating client IP.

    Uses ``request.remote_addr`` (the actual TCP connection source) for
    rate-limiting to prevent spoofing.  When running behind a trusted
    reverse-proxy set ``TRUST_X_FORWARDED_FOR=1`` in the environment to
    instead read ``X-Forwarded-For``.
    """
    if config.TRUST_X_FORWARDED_FOR:
        forwarded_for = request.headers.get("X-Forwarded-For", "")
        if forwarded_for:
            first_ip = forwarded_for.split(",")[0].strip()
            if first_ip:
                return first_ip
    return request.remote_addr or "unknown"


def _refresh_auth_ok():
    """Return True if refresh endpoint is authorized."""
    if not config.REFRESH_API_TOKEN and not config.MUTATION_API_TOKEN:
        return True
    token = request.headers.get("X-Refresh-Token", "").strip()
    if not token:
        auth_header = request.headers.get("Authorization", "").strip()
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
    if config.REFRESH_API_TOKEN and token == config.REFRESH_API_TOKEN:
        return True
    if config.MUTATION_API_TOKEN and token == config.MUTATION_API_TOKEN:
        return True
    return False


def _debug_auth_ok():
    """Return True if the caller is authorized for debug endpoints.

    Requires at least one of DEBUG_API_KEY, REFRESH_API_TOKEN, or
    MUTATION_API_TOKEN to be set in the environment; the caller must present
    the matching value via a ``X-Debug-Key`` header or
    ``Authorization: Bearer <key>`` header.

    When none are set, debug endpoints are **permanently disabled**
    (always return 401). Set one of them to re-enable them.
    """
    if not config.DEBUG_API_KEY and not config.REFRESH_API_TOKEN and not config.MUTATION_API_TOKEN:
        return False
    got = request.headers.get("X-Debug-Key", "").strip()
    if not got:
        auth = request.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip()
    if config.DEBUG_API_KEY and got == config.DEBUG_API_KEY:
        return True
    if config.REFRESH_API_TOKEN and got == config.REFRESH_API_TOKEN:
        return True
    if config.MUTATION_API_TOKEN and got == config.MUTATION_API_TOKEN:
        return True
    return False


def _mutation_auth_ok():
    """Return True if the caller is authorized for backend-changing endpoints.

    Accepts:
      1. ``X-Admin-Token`` / ``Authorization: Bearer <token>`` matching
         ``MUTATION_API_TOKEN`` or ``NOTIFICATIONS_API_KEY`` (server-to-server).
      2. A session JWT whose ``sub`` is in ``ADMIN_SUBS`` (personal Apple
         accounts promoted to admin via ``APPLE_ADMIN_SUBS`` env var).
    """
    if not config.MUTATION_API_TOKEN and not config.NOTIFICATIONS_API_KEY and not config.ADMIN_SUBS:
        return False

    # 1. Shared-secret checks (server-to-server)
    for header_name in ("X-Admin-Token", "X-Notifications-Key"):
        token = request.headers.get(header_name, "").strip()
        if token and config.MUTATION_API_TOKEN and token == config.MUTATION_API_TOKEN:
            return True
        if token and config.NOTIFICATIONS_API_KEY and token == config.NOTIFICATIONS_API_KEY:
            return True

    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if config.MUTATION_API_TOKEN and token == config.MUTATION_API_TOKEN:
            return True
        if config.NOTIFICATIONS_API_KEY and token == config.NOTIFICATIONS_API_KEY:
            return True

    # 2. Session JWT check (personal Apple account admin)
    if config.ADMIN_SUBS and auth_header.lower().startswith("bearer "):
        session_token = auth_header[7:].strip()
        if session_token:
            from apple_auth import decode_session_jwt
            session = decode_session_jwt(session_token)
            if session and session.get("sub") in config.ADMIN_SUBS:
                return True

    return False


# ── Rate Limiting ─────────────────────────────────────────────────

_api_rate_lock = threading.Lock()
_api_rate_events_by_ip = {}


def register_auth_handlers(app):
    """Register mutation-auth and rate-limit before_request handlers on the Flask app."""

    @app.before_request
    def _enforce_mutation_auth():
        """Block backend-changing API calls unless a valid mutation secret is supplied."""
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None

        protected_paths = {
            "/api/notifications",
            "/api/notifications/register",
            "/api/feedback",
            "/api/predict",
            "/api/predict/mls",
            "/api/predict/extra",
            "/api/refresh",
            "/api/retrain",
        }
        if request.path not in protected_paths:
            return None

        if _mutation_auth_ok():
            return None
        if request.path in {"/api/refresh", "/api/retrain"} and _refresh_auth_ok():
            return None

        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    @app.before_request
    def _enforce_api_rate_limit():
        """Apply a per-IP rolling one-minute cap for all API routes."""
        if not request.path.startswith("/api/"):
            return None

        now = time.time()
        cutoff = now - 60.0
        ip = _client_ip()
        limit = max(1, config.API_RATE_LIMIT_PER_MINUTE)
        retry_after = 60

        with _api_rate_lock:
            events = _api_rate_events_by_ip.setdefault(ip, deque())
            while events and events[0] <= cutoff:
                events.popleft()

            if len(events) >= limit:
                retry_after = int(max(1, 60 - (now - events[0])))
                print(
                    f"[rate-limit] {ip} hit {limit} req/min cap on "
                    f"{request.path} (retry_after={retry_after}s)"
                )
                return jsonify(
                    {
                        "ok": False,
                        "error": "Rate limit exceeded. Try again later.",
                        "retry_after_seconds": retry_after,
                        "limit_per_minute": limit,
                    }
                ), 429

            events.append(now)

            stale_ips = [key for key, queue in _api_rate_events_by_ip.items() if not queue or queue[-1] <= cutoff]
            for key in stale_ips:
                _api_rate_events_by_ip.pop(key, None)

        return None
