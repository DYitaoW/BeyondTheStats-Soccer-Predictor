"""
FastAPI API server — the main web serving layer for Beyond the Stats.
"""
from __future__ import annotations
import os
import sys
from contextlib import asynccontextmanager
from typing import Any
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
import config
if config.PROJECT_DIR not in sys.path:
    sys.path.insert(0, config.PROJECT_DIR)
from rate_limit import check_rate_limit, client_identifier
from live_poller import start_live_score_poller
from notifications import start_apns_worker
from routes.helpers import log_key_error
from routes.compat import RequestContextMiddleware, jsonify
from routes import register_all_routers
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown lifecycle."""
    if not config.MUTATION_API_TOKEN:
        print("[startup] WARNING: no mutation auth configured — write-capable API endpoints are disabled!")
    start_live_score_poller()
    start_apns_worker()
    yield
app = FastAPI(
    title="Beyond the Stats",
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
)
app.config: dict[str, Any] = {
    "MAX_CONTENT_LENGTH": 1024 * 1024,
    "_backend_refresh": None,
}
@app.exception_handler(KeyError)
async def _handle_key_error(request: Request, exc: KeyError):
    log_key_error("unhandled", exc)
    return JSONResponse(status_code=500, content={"error": f"KeyError: {exc}"})
@app.exception_handler(Exception)
async def _handle_general_exception(request: Request, exc: Exception):
    import logging
    logging.getLogger("app").exception("Unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"error": "Internal server error"})
class SecurityAndCacheHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/"):
            client_ip = client_identifier(request)
            limit = getattr(config, "API_RATE_LIMIT_PER_MINUTE", 120)
            allowed, retry_after = check_rate_limit("api", limit, 60, client_id=client_ip)
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={"error": "Rate limit exceeded. Try again in a minute."},
                    headers={"Retry-After": str(retry_after or 60)},
                )
        response: Response = await call_next(request)
        if path.startswith("/api/"):
            if any(path.startswith(p) for p in (
                "/api/past-games", "/api/scorers", "/api/stats",
                "/api/league-leaders", "/api/pipeline/status", "/api/key-errors",
                "/api/help", "/api/legal",
            )):
                response.headers["Cache-Control"] = "public, max-age=60"
            elif path.startswith("/api/live"):
                response.headers["Cache-Control"] = "public, max-age=15"
            else:
                response.headers["Cache-Control"] = "public, max-age=30"
        elif any(path.endswith(ext) for ext in (".js", ".css", ".png", ".jpg", ".svg", ".ico", ".woff2")):
            response.headers["Cache-Control"] = "public, max-age=86400"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(SecurityAndCacheHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=getattr(config, "CORS_ORIGINS", ["*"]),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestContextMiddleware)
_website_dir = os.path.dirname(os.path.abspath(__file__))
_static_dir = os.path.join(_website_dir, "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")
register_all_routers(app)
def _run_app(host: str = "0.0.0.0", port: int = 5000, **kwargs):
    import uvicorn
    uvicorn.run(app, host=host, port=port)
app.run = _run_app
if __name__ == "__main__":
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=5000, help="Port to bind to")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on changes")
    args = parser.parse_args()
    uvicorn.run("app:app", host=args.host, port=args.port, reload=args.reload)