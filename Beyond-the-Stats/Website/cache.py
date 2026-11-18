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

# key -> (fresh_until, stale_until, json). Entries past ``fresh_until`` are
# served once more while a background refresh runs (stale-while-revalidate),
# so a popular endpoint is only ever slow on its very first request.
_mem_cache: dict[str, tuple[float, float, str]] = {}
STALE_GRACE_SECONDS = int(getattr(config, "CACHE_STALE_GRACE_SECONDS", 1800))
_refreshing: set[str] = set()
_refreshing_lock = threading.Lock()
_mem_cache_lock = threading.Lock()
_MAX_MEM_CACHE_ENTRIES = 500
# cache key -> request path, so result-driven invalidation can target endpoints
# (keys are md5 hashes and cannot be matched by path otherwise).
_key_paths: dict[str, str] = {}


def _cache_key(endpoint: str, query_str: str = "") -> str:
    """Return a deterministic cache key for an API call, normalizing query parameters."""
    norm_query = query_str or ""
    if norm_query:
        try:
            from urllib.parse import parse_qsl, urlencode
            pairs = parse_qsl(norm_query, keep_blank_values=True)
            if pairs:
                pairs.sort(key=lambda x: (x[0], x[1]))
                norm_query = urlencode(pairs)
        except Exception:
            pass
    raw = f"{endpoint}:{norm_query}"
    key = f"api:{hashlib.md5(raw.encode()).hexdigest()}"
    try:
        with _mem_cache_lock:
            if len(_key_paths) > 20000:
                _key_paths.clear()
            _key_paths[key] = str(endpoint or "")
    except Exception:
        pass
    return key


def _cache_get(key: str) -> str | None:
    """Return cached JSON string or None. Never raises exceptions."""
    try:
        if _redis_client is not None:
            try:
                return _redis_client.get(key)
            except Exception:
                pass
        val, fresh = _cache_get_with_state(key)
        return val if fresh else None
    except Exception:
        pass
    return None


def _cache_get_with_state(key: str) -> tuple[str | None, bool]:
    """In-memory lookup: ``(json, is_fresh)``; ``(None, False)`` when absent."""
    now = time.monotonic()
    with _mem_cache_lock:
        item = _mem_cache.get(key)
        if item is None:
            return None, False
        fresh_until, stale_until, val = item
        if now < fresh_until:
            return val, True
        if now < stale_until:
            return val, False
        _mem_cache.pop(key, None)
    return None, False


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
                expired = [k for k, (_fresh, stale, _v) in _mem_cache.items() if now >= stale]
                for k in expired:
                    _mem_cache.pop(k, None)
                if len(_mem_cache) >= _MAX_MEM_CACHE_ENTRIES:
                    for k in list(_mem_cache.keys())[:_MAX_MEM_CACHE_ENTRIES // 5]:
                        _mem_cache.pop(k, None)
            fresh_until = now + max(1, int(ttl))
            # Stale window scales with the TTL: live-ish endpoints (30 s) are
            # served stale for at most ~5 min, slow-changing ones up to 30 min.
            grace = min(STALE_GRACE_SECONDS, max(60, int(ttl) * 10))
            _mem_cache[key] = (fresh_until, fresh_until + grace, value)
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


def _cache_clear_endpoints(prefixes) -> int:
    """Drop cached responses whose request path starts with any of *prefixes*.

    Used after a match finishes so standings / team form / past-games
    responses are recomputed instead of served stale for up to the TTL.
    Returns the number of cache keys removed.
    """
    prefixes = tuple(str(p) for p in (prefixes or ()) if p)
    if not prefixes:
        return 0
    with _mem_cache_lock:
        keys = [k for k, path in _key_paths.items() if path.startswith(prefixes)]
        for k in keys:
            _mem_cache.pop(k, None)
    if _redis_client is not None and keys:
        try:
            _redis_client.delete(*keys)
        except Exception:
            pass
    return len(keys)


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


def _lookup(key: str) -> tuple[str | None, bool]:
    """``(json, is_fresh)`` from Redis (always fresh) or the memory cache."""
    if _redis_client is not None:
        try:
            val = _redis_client.get(key)
            if val is not None:
                return val, True
        except Exception:
            pass
    try:
        return _cache_get_with_state(key)
    except Exception:
        return None, False


def _cached_json_response(body: str, ttl: int, fresh: bool) -> Response:
    return Response(
        content=body,
        status_code=200,
        media_type="application/json",
        headers={
            "X-Cache": ("redis" if _redis_client else "memory") + ("" if fresh else "-stale"),
            "Cache-Control": f"public, max-age={ttl if fresh else min(ttl, 15)}",
        },
    )


def _refresh_in_background(key: str, ttl: int, f: Callable, args: tuple, kwargs: dict, *, is_async: bool) -> None:
    """Recompute a stale entry once, off the request path (same request context)."""
    import contextvars

    with _refreshing_lock:
        if key in _refreshing:
            return
        _refreshing.add(key)
    ctx = contextvars.copy_context()

    def _run():
        try:
            if is_async:
                import asyncio
                result = asyncio.run(f(*args, **kwargs))
            else:
                result = f(*args, **kwargs)
            if isinstance(result, Response) and result.status_code == 200:
                _cache_set(key, result.body.decode("utf-8", errors="replace"), ttl=ttl)
        except Exception:
            pass
        finally:
            with _refreshing_lock:
                _refreshing.discard(key)

    threading.Thread(target=lambda: ctx.run(_run), name="cache-refresh", daemon=True).start()


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
                    cached, fresh = _lookup(key)
                    effective_ttl = _resolve_ttl(req)
                    if cached is not None:
                        if not fresh:
                            _refresh_in_background(key, effective_ttl, f, args, kwargs, is_async=True)
                        return _cached_json_response(cached, effective_ttl, fresh)
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
                    cached, fresh = _lookup(key)
                    effective_ttl = _resolve_ttl(req)
                    if cached is not None:
                        if not fresh:
                            _refresh_in_background(key, effective_ttl, f, args, kwargs, is_async=False)
                        return _cached_json_response(cached, effective_ttl, fresh)
                    result = f(*args, **kwargs)
                    if isinstance(result, Response) and result.status_code == 200:
                        _cache_set(key, result.body.decode("utf-8", errors="replace"), ttl=effective_ttl)
                        result.headers["Cache-Control"] = f"public, max-age={effective_ttl}"
                    return result
                return f(*args, **kwargs)
            return sync_wrapper
    return decorator

