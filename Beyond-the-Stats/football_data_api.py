"""Rate limiting helpers for football-data.org API requests + response caching."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_DELAY_SECONDS = 120  # free tier: ~10 requests/minute; 2 min is safe between leagues
API_CACHE_TTL = 3600  # 1 hour
_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "Data", "Predictions", ".football_data_api_cache.json",
)

# Timestamp of the last LIVE football-data.org HTTP request in this process.
# Used to enforce the inter-request pause only when a real network call is
# about to happen -- cached responses return immediately with no sleep.
_last_request_ts = 0.0


def _load_cache() -> dict[str, Any]:
    try:
        with open(_CACHE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
    # Write atomically via temp file
    tmp = _CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, _CACHE_FILE)


def delay_seconds() -> int:
    raw = os.getenv("FOOTBALL_DATA_API_DELAY_SECONDS", str(DEFAULT_DELAY_SECONDS)).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_DELAY_SECONDS


def wait_between_requests(competition_name: str = "", *, force: bool = False) -> None:
    """Pause long enough to respect the free-tier rate limit BEFORE a live request.

    Called only from ``fetch_json`` right before an actual HTTP round-trip;
    cache hits never reach this. A full pause happens on the first request of
    the process only when ``force`` is set; otherwise the first request goes
    out immediately and subsequent ones are spaced by ``delay_seconds()``.
    """
    global _last_request_ts
    seconds = delay_seconds()
    if seconds <= 0:
        return
    now = time.time()
    if _last_request_ts <= 0.0 and not force:
        _last_request_ts = now
        return
    remaining = seconds - (now - _last_request_ts)
    if remaining > 0:
        label = str(competition_name or "").strip() or "next request"
        print(f"[football-data.org] waiting {remaining:.0f}s before {label}...")
        time.sleep(remaining)
    _last_request_ts = time.time()


def fetch_json(
    url: str,
    headers: dict | None = None,
    *,
    timeout: int = 45,
    competition_name: str = "",
) -> dict[str, Any]:
    """Fetch JSON from football-data.org with caching (1-hour TTL).

    Returns cached data when available and fresh -- instantly, with no
    rate-limit sleep. Otherwise enforces the free-tier pause, fetches live,
    caches the response, and returns it.
    """
    now = time.time()
    cache = _load_cache()
    entry = cache.get(url)
    if entry and isinstance(entry, dict) and now - entry.get("ts", 0) < API_CACHE_TTL:
        label = competition_name or url
        print(f"[football-data.org] cache hit for {label}")
        return entry["data"]

    wait_between_requests(competition_name)
    now = time.time()
    request = urllib.request.Request(url, headers=headers or {})
    attempts = 2
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                import json as _json

                data = _json.loads(response.read().decode("utf-8"))
                cache[url] = {"ts": now, "data": data}
                _save_cache(cache)
                return data
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt + 1 < attempts:
                seconds = delay_seconds()
                label = competition_name or "request"
                print(
                    f"[football-data.org] 429 Too Many Requests for {label}; "
                    f"waiting {seconds}s and retrying..."
                )
                time.sleep(seconds)
                _last_request_ts = time.time()
                continue
            raise
    return {}
