"""
Flask API server — the main web serving layer for Beyond the Stats.
Architecture
------------
This Flask application serves all REST API endpoints plus the static frontend.
Subsystem logic lives in dedicated modules and routes are organized into
domain-specific Flask Blueprints under `Website/routes/`:
- `routes.pages_bp`: HTML page rendering routes and static asset endpoints
- `routes.predictions_bp`: Match prediction, team query, fixture and H2H API routes
- `routes.leagues_bp`: League tables, projected standings, cup brackets, and leader API routes
- `routes.live_scores_bp`: Live scores, APNs push notifications, and Live Activities API routes
- `routes.admin_bp`: Admin, pipeline status/logs, legal, help, info, and system diagnostics routes
This entrypoint file contains:
- Flask app initialization
- Middleware (CORS, cache headers, before/after hooks, rate limits)
- Error handlers
- Blueprint registration
"""
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, request
import config
if config.PROJECT_DIR not in sys.path:
    sys.path.insert(0, config.PROJECT_DIR)
from rate_limit import check_rate_limit, client_identifier
from live_poller import start_live_score_poller
from notifications import start_apns_worker
from routes.helpers import log_key_error
from routes import register_all_blueprints
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024  # 1 MB request body limit
@app.errorhandler(KeyError)
def _handle_key_error(exc):
    """Log unhandled KeyErrors and return a 500 with error info."""
    log_key_error("unhandled", exc)
    return jsonify({"error": f"KeyError: {exc}"}), 500
@app.after_request
def _add_cache_headers(response):
    """Attach a sensible Cache-Control header to every served response.
    The website re-fetches the same JSON + static assets on every page
    load because no headers were previously set. This handler adds modest
    browser + shared cache lifetimes so repeat visits are instant.
    """
    if request.path.startswith("/api/"):
        # JSON endpoints: short max-age + must-revalidate so the browser
        # revalidates on the next page load but can serve stale-while-
        # revalidate if the user navigates quickly back to the page.
        response.headers["Cache-Control"] = (
            f"private, max-age={config._API_CACHE_MAX_AGE}, must-revalidate"
        )
    elif request.path.startswith("/static/"):
        # Static JS / CSS / images. Filenames are stable between deploys;
        # version-bumping the URL is the cache-bust strategy. Override the
        # Flask default ("no-cache") so browsers actually cache them.
        response.headers["Cache-Control"] = (
            f"public, max-age={config._STATIC_CACHE_MAX_AGE}"
        )
    elif request.path.startswith("/graphics/"):
        response.headers["Cache-Control"] = (
            f"public, max-age={int(config._STATIC_CACHE_MAX_AGE * 24)}"
        )
    # Security headers
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-XSS-Protection", "0")
    # HSTS — only set when using HTTPS
    if request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    # CSP — strict but permissive enough for the legacy UI
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "img-src 'self' data: https:; "
        "font-src 'self' https://cdnjs.cloudflare.com; "
        "connect-src 'self'; "
        "frame-ancestors 'none'",
    )
    # CORS for the Cloudflare Pages frontend (and any future static origin).
    # Allow-list is read from ALLOWED_ORIGINS env var (comma-separated).
    origin = request.headers.get("Origin")
    if origin:
        allowed = config.ALLOWED_ORIGINS
        if origin in allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Headers"] = (
                "Content-Type, Authorization, X-Refresh-Token, X-Notifications-Key, X-Debug-Key"
            )
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response
@app.before_request
def _handle_cors_preflight():
    """Respond to CORS preflight (OPTIONS) requests immediately."""
    if request.method == "OPTIONS":
        # Build a minimal preflight response; after_request adds the
        # Access-Control-* headers based on the Origin.
        return ("", 204)
@app.before_request
def _enforce_api_rate_limits():
    """Per-IP rate limits for all ``/api/*`` calls (redeem is tighter)."""
    if request.method == "OPTIONS":
        return None
    path = request.path or ""
    if not path.startswith("/api/"):
        return None
    client_id = client_identifier()
    allowed, retry_after = check_rate_limit(
        "api",
        config.API_RATE_LIMIT_PER_MINUTE,
        60,
        client_id=client_id,
    )
    if not allowed:
        resp = jsonify({
            "ok": False,
            "error": "rate_limit_exceeded",
            "detail": f"Limit {config.API_RATE_LIMIT_PER_MINUTE} requests per minute",
        })
        resp.status_code = 429
        resp.headers["Retry-After"] = str(retry_after or 60)
        return resp
    if path.rstrip("/") == "/api/redeem":
        allowed, retry_after = check_rate_limit(
            "redeem",
            config.REDEEM_RATE_LIMIT_PER_MINUTE,
            60,
            client_id=client_id,
        )
        if not allowed:
            resp = jsonify({
                "ok": False,
                "error": "rate_limit_exceeded",
                "detail": f"Redeem limit {config.REDEEM_RATE_LIMIT_PER_MINUTE} requests per minute",
            })
            resp.status_code = 429
            resp.headers["Retry-After"] = str(retry_after or 60)
            return resp
    return None
# ── Register all modular route blueprints ─────────────────────────────
register_all_blueprints(app)
if __name__ == "__main__":
    import argparse
    import socket
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=5000, help="Port to bind to")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable Flask debug mode (with auto-reload; spawns a reloader process)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable Werkzeug file-change reloader (requires --debug)",
    )
    args = parser.parse_args()
    if args.reload and not args.debug:
        raise SystemExit("--reload requires --debug")
    use_reloader = bool(args.debug and args.reload)
    if args.host == "0.0.0.0":
        try:
            s = socket.socket(socket.AF_INET, socket.sock_dgram)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            print(f"\n * Connect from other devices at: http://{ip}:{args.port}\n")
        except Exception:
            pass
    if not config.MUTATION_API_TOKEN:
        print("[startup] WARNING: no mutation auth configured — write-capable API endpoints are disabled!")
    start_live_score_poller()
    # Start APNs background worker (does nothing if env vars not set)
    start_apns_worker()
    app.run(
        host=args.host,
        port=args.port,
        debug=args.debug,
        use_reloader=use_reloader,
    )