"""Redis caching utilities for API responses."""
import hashlib
import functools
from flask import request
import config

_redis_client = None
if config.REDIS_URL:
    try:
        import redis as _redis_mod
        _redis_client = _redis_mod.from_url(config.REDIS_URL, socket_timeout=2, decode_responses=True)
        _redis_client.ping()
    except Exception:
        _redis_client = None


def _cache_key(endpoint: str, query_str: str = "") -> str:
    """Return a deterministic cache key for an API call."""
    raw = f"{endpoint}:{query_str}"
    return f"api:{hashlib.md5(raw.encode()).hexdigest()}"


def _cache_get(key: str) -> str | None:
    """Return cached JSON string or None."""
    if _redis_client is None:
        return None
    try:
        return _redis_client.get(key)
    except Exception:
        return None


def _cache_set(key: str, value: str, ttl: int = config.CACHE_TTL_DEFAULT) -> None:
    """Store a JSON string in cache with TTL."""
    if _redis_client is None:
        return
    try:
        _redis_client.setex(key, ttl, value)
    except Exception:
        pass


def _cache_clear_pattern(pattern: str = "api:*") -> None:
    """Clear all cached API responses. Called after pipeline refresh."""
    if _redis_client is None:
        return
    try:
        for k in _redis_client.scan_iter(match=pattern):
            _redis_client.delete(k)
    except Exception:
        pass


def _cached_response(ttl: int = config.CACHE_TTL_DEFAULT):
    """Decorator that caches a route's JSON response in Redis.

    The cache key is ``api:{md5(endpoint + query_string)}``.
    Skips caching when ``?no_cache=1`` or ``?refresh=1`` is present.
    """
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            if request.args.get("no_cache", "").strip() in ("1", "true"):
                return f(*args, **kwargs)
            if request.args.get("refresh", "").strip() in ("1", "true"):
                return f(*args, **kwargs)
            from app import app  # Lazy import to avoid circular dep
            key = _cache_key(request.path, request.query_string.decode("utf-8", errors="replace"))
            cached = _cache_get(key)
            if cached is not None:
                resp = app.response_class(
                    response=cached, status=200, mimetype="application/json"
                )
                resp.headers["X-Cache"] = "redis"
                return resp
            result = f(*args, **kwargs)
            if isinstance(result, app.response_class) and result.status_code == 200:
                _cache_set(key, result.get_data(as_text=True), ttl=ttl)
            return result
        return wrapper
    return decorator
