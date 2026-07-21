"""Per-client API rate limiting (sliding window).

Uses Redis when ``REDIS_URL`` is configured; otherwise an in-process
memory store (correct for the single gunicorn worker + gthread setup).
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from flask import request

import config

_memory_hits: dict[str, deque[float]] = defaultdict(deque)
_memory_lock = threading.Lock()

_redis = None
if getattr(config, "REDIS_URL", ""):
    try:
        import redis as _redis_mod

        _redis = _redis_mod.from_url(config.REDIS_URL, socket_timeout=1, decode_responses=True)
        _redis.ping()
    except Exception:
        _redis = None


def client_identifier() -> str:
    """Best-effort client IP behind Cloudflare / reverse proxies."""
    cf = (request.headers.get("CF-Connecting-IP") or "").strip()
    if cf:
        return cf
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    real_ip = (request.headers.get("X-Real-IP") or "").strip()
    if real_ip:
        return real_ip
    return request.remote_addr or "unknown"


def _check_memory(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    now = time.monotonic()
    cutoff = now - window_seconds
    with _memory_lock:
        hits = _memory_hits[key]
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= limit:
            retry_after = max(1, int(hits[0] + window_seconds - now) + 1)
            return False, retry_after
        hits.append(now)
        return True, 0


def _check_redis(key: str, limit: int, window_seconds: int) -> tuple[bool, int] | None:
    """Return allow/deny, or None if Redis is unavailable so memory can be used."""
    if _redis is None:
        return None
    redis_key = f"rl:{key}"
    try:
        pipe = _redis.pipeline()
        pipe.incr(redis_key)
        pipe.ttl(redis_key)
        count, ttl = pipe.execute()
        if count == 1 or ttl < 0:
            _redis.expire(redis_key, window_seconds)
            ttl = window_seconds
        if int(count) > limit:
            return False, max(1, int(ttl) if ttl and ttl > 0 else window_seconds)
        return True, 0
    except Exception:
        return None


def check_rate_limit(
    bucket: str,
    limit: int,
    window_seconds: int = 60,
    *,
    client_id: str | None = None,
) -> tuple[bool, int]:
    """Return ``(allowed, retry_after_seconds)``.

    ``bucket`` distinguishes limit scopes (e.g. ``api`` vs ``redeem``).
    """
    if limit <= 0:
        return True, 0
    cid = client_id if client_id is not None else client_identifier()
    key = f"{cid}:{bucket}"
    redis_result = _check_redis(key, limit, window_seconds)
    if redis_result is not None:
        return redis_result
    return _check_memory(key, limit, window_seconds)
