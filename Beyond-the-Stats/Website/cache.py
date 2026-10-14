"""Redis and in-memory caching utilities for API responses."""
from __future__ import annotations

import functools
import hashlib
import inspect
import threading
import time
from typing import Any, Callable

import config
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

_redis_client = None
if config.REDIS_URL:
    try:
        import redis as _redis_mod
        _redis_client = _redis_mod.from_url(config.REDIS_URL, socket_timeout=2, decode_responses=True)
        _redis_client.ping()
    except Exception:
        _redis_client = None

_mem_cache: dict[str, tuple[float, str]] = {}
_mem_cache_lock = threading.Lock()
_MAX_MEM_CACHE_ENTRIES = 500


def _cache_key(endpoint: str, query_str: str = "") -> str:
    """Return a deterministic cache key for an API call."""
    raw = f"{endpoint}:{query_str}"
    return f"api:{hashlib.md5(raw.encode()).hexdigest()}"


def _cache_get(key: str) -> str | None:
    """Return cached JSON string or None. Never raises exceptions."""
    try:
        if _redis_client is not None:
            try:
                return _redis_client.get(key)
            except Exception:
                pass
        now = time.monotonic()
        with _mem_cache_lock:
            item = _mem_cache.get(key)
            if item is not None:
                expires_at, val = item
                if now < expires_at:
                    return val
                _mem_cache.pop(key, None)
    except Exception:
        pass
    return None


def _cache_set(key: str, value: str, ttl: int = config.CACHE_TTL_DEFAULT) -> None:
    """Store a JSON string in cache with TTL. Never raises exceptions."""
    try:
        if _redis_client is not None:
            try:
                _redis_client.setex(key, ttl, value)
                return
            except Exception:
                pass
        now = time.monotonic()
        with _mem_cache_lock:
            if len(_mem_cache) >= _MAX_MEM_CACHE_ENTRIES:
                expired = [k for k, (exp, _) in _mem_cache.items() if now >= exp]
                for k in expired:
                    _mem_cache.pop(k, None)
                if len(_mem_cache) >= _MAX_MEM_CACHE_ENTRIES:
                    for k in list(_mem_cache.keys())[:_MAX_MEM_CACHE_ENTRIES // 5]:
                        _mem_cache.pop(k, None)
            _mem_cache[key] = (now + max(1, int(ttl)), value)
    except Exception:
        pass


def _cache_clear_pattern(pattern: str = "api:*") -> None:
    """Clear all cached API responses. Called after pipeline refresh."""
    if _redis_client is not None:
        try:
            for k in _redis_client.scan_iter(match=pattern):
                _redis_client.delete(k)
        except Exception:
            pass
    with _mem_cache_lock:
        _mem_cache.clear()


def _extract_request(args: tuple, kwargs: dict) -> Request | None:
    for a in args:
        if isinstance(a, Request):
            return a
    for v in kwargs.values():
        if isinstance(v, Request):
            return v
    try:
        from routes.compat import _request_ctx
        ctx_req = _request_ctx.get()
        if ctx_req is not None:
            return ctx_req
    except Exception:
        pass
    return None


def _cached_response(ttl: int | Callable[[Request], int] = config.CACHE_TTL_DEFAULT):
    """Decorator that caches a route's JSON response in Redis (or in-memory).

    The cache key is ``api:{md5(endpoint + query_string)}``.
    Skips caching when ``?no_cache=1`` or ``?refresh=1`` is present.
    Supports both sync and async FastAPI route handlers.
    ``ttl`` can be an integer in seconds or a callable taking ``request`` and returning seconds.
    """
    def decorator(f: Callable) -> Callable:
        def _resolve_ttl(req: Request) -> int:
            if callable(ttl):
                try:
                    return max(1, int(ttl(req)))
                except Exception:
                    return config.CACHE_TTL_DEFAULT
            return max(1, int(ttl))

        if inspect.iscoroutinefunction(f):
            @functools.wraps(f)
            async def async_wrapper(*args, **kwargs):
                req = _extract_request(args, kwargs)
                if req is not None:
                    if req.query_params.get("no_cache", "").strip() in ("1", "true"):
                        return await f(*args, **kwargs)
                    if req.query_params.get("refresh", "").strip() in ("1", "true"):
                        return await f(*args, **kwargs)
                    key = _cache_key(req.url.path, str(req.url.query))
                    cached = _cache_get(key)
                    effective_ttl = _resolve_ttl(req)
                    if cached is not None:
                        return Response(
                            content=cached,
                            status_code=200,
                            media_type="application/json",
                            headers={
                                "X-Cache": "redis" if _redis_client else "memory",
                                "Cache-Control": f"public, max-age={effective_ttl}",
                            },
                        )
                    result = await f(*args, **kwargs)
                    if isinstance(result, Response) and result.status_code == 200:
                        _cache_set(key, result.body.decode("utf-8", errors="replace"), ttl=effective_ttl)
                        result.headers["Cache-Control"] = f"public, max-age={effective_ttl}"
                    return result
                return await f(*args, **kwargs)
            return async_wrapper
        else:
            @functools.wraps(f)
            def sync_wrapper(*args, **kwargs):
                req = _extract_request(args, kwargs)
                if req is not None:
                    if req.query_params.get("no_cache", "").strip() in ("1", "true"):
                        return f(*args, **kwargs)
                    if req.query_params.get("refresh", "").strip() in ("1", "true"):
                        return f(*args, **kwargs)
                    key = _cache_key(req.url.path, str(req.url.query))
                    cached = _cache_get(key)
                    effective_ttl = _resolve_ttl(req)
                    if cached is not None:
                        return Response(
                            content=cached,
                            status_code=200,
                            media_type="application/json",
                            headers={
                                "X-Cache": "redis" if _redis_client else "memory",
                                "Cache-Control": f"public, max-age={effective_ttl}",
                            },
                        )
                    result = f(*args, **kwargs)
                    if isinstance(result, Response) and result.status_code == 200:
                        _cache_set(key, result.body.decode("utf-8", errors="replace"), ttl=effective_ttl)
                        result.headers["Cache-Control"] = f"public, max-age={effective_ttl}"
                    return result
                return f(*args, **kwargs)
            return sync_wrapper
    return decorator

