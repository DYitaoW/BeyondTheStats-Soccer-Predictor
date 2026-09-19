"""Rate limiting helpers for football-data.org API requests + response caching.

Rate limiting uses a rolling per-minute budget (60-100 requests/minute),
never a fixed inter-request delay, so the pipeline keeps calling as long as
the total in any 60-second window stays under the site's allowance.
"""
from __future__ import annotations

import collections
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_MAX_REQUESTS_PER_MINUTE = 60  # site allows 60-100/min; stay under it
API_CACHE_TTL = 3600  # 1 hour
_RATE_WINDOW_SECONDS = 60.0
# Cache lives under Output/Cache (created on first write).
_SP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_FILE = os.path.join(_SP_DIR, "Output", "Cache", ".football_data_api_cache.json")

# Timestamps of LIVE football-data.org HTTP requests in this process, oldest
# first. Used to enforce the per-minute budget only when a real network call
# is about to happen -- cached responses return immediately with no sleep.
_request_timestamps: collections.deque[float] = collections.deque()
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


def max_requests_per_minute() -> int:
    raw = os.getenv("FOOTBALL_DATA_API_MAX_REQUESTS_PER_MINUTE", str(DEFAULT_MAX_REQUESTS_PER_MINUTE)).strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_MAX_REQUESTS_PER_MINUTE


def min_inter_request_gap() -> float:
    """Optional extra floor between live requests (0 by default; the per-minute
    budget alone is what protects the site's 60-100/min allowance)."""
    raw = os.getenv("FOOTBALL_DATA_API_MIN_GAP_SECONDS", "0").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def delay_seconds() -> int:
    """Back-compat getter, now interpreted as an optional minimum gap.

    ``FOOTBALL_DATA_API_DELAY_SECONDS`` used to force a fixed wait before
    every request (default 120s). With the per-minute budget it is no longer
    needed for correctness, so the default is 0 (no forced pause).
    """
    raw = os.getenv("FOOTBALL_DATA_API_DELAY_SECONDS", "0").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def wait_between_requests(competition_name: str = "", *, force: bool = False) -> None:
    """Keep live football-data.org requests inside a rolling per-minute budget.

    Called only from ``fetch_json`` right before an actual HTTP round-trip;
    cache hits never reach this. Maintains a sliding 60-second window: a slot
    is used immediately when still available, otherwise the call sleeps just
    long enough for the oldest request to fall out of the window.
    """
    global _request_timestamps, _last_request_ts
    now = time.time()

    # Optional hard floor between two requests (backward-compat knob).
    gap = max(delay_seconds(), min_inter_request_gap())
    if gap > 0 and _last_request_ts > 0:
        remaining = gap - (now - _last_request_ts)
        if remaining > 0:
            time.sleep(remaining)
            now = time.time()

    budget = max_requests_per_minute()

    # Drop requests that have aged out of the window.
    cutoff = now - _RATE_WINDOW_SECONDS
    while _request_timestamps and _request_timestamps[0] <= cutoff:
        _request_timestamps.popleft()

    if len(_request_timestamps) >= budget:
        oldest = _request_timestamps[0]
        wait_s = (oldest + _RATE_WINDOW_SECONDS) - now
        if wait_s > 0:
            label = str(competition_name or "").strip() or "next request"
            print(
                f"[football-data.org] at {budget} reqs in the last 60s; "
                f"waiting {wait_s:.0f}s before {label}..."
            )
            time.sleep(wait_s)
            now = time.time()

    _request_timestamps.append(now)
    _last_request_ts = now


def fetch_json(
    url: str,
    headers: dict | None = None,
    *,
    timeout: int = 45,
    competition_name: str = "",
) -> dict[str, Any]:
    """Fetch JSON from football-data.org with caching (1-hour TTL).

    Returns cached data when available and fresh -- instantly, with no
    rate-limit wait. Otherwise enforces the rolling per-minute budget, fetches
    live, caches the response, and returns it.
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
                label = competition_name or "request"
                print(
                    f"[football-data.org] 429 Too Many Requests for {label}; "
                    "waiting for a free rate-limit slot and retrying..."
                )
                # Wait for the oldest request to exit the 60s window before
                # retrying, so the retry itself doesn't burn a budget slot.
                wait_between_requests(competition_name="429 retry for " + label)
                continue
            raise
    return {}
