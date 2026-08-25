"""Per-league ESPN scoreboard disk cache.

The cup/friendlies upcoming-fixture crawlers request one ESPN scoreboard
payload per (league, day) -- up to 366 days x ~10 competitions per run with
no caching. This module persists each payload as its own JSON file under
``Data/ApiCache/espn/<espn_id>/<yyyymmdd>.json`` so repeated runs within the
TTL skip the network entirely.

TTL policy:
- today/past dates: short TTL (scores and statuses keep changing)
- future dates: long TTL (scheduled fixtures rarely change)
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_ROOT = os.path.join(BASE_DIR, "Data", "ApiCache", "espn")

DEFAULT_TIMEOUT_SECONDS = 30
TTL_PAST_SECONDS = 2 * 3600    # 2h for today and earlier
TTL_FUTURE_SECONDS = 24 * 3600  # 24h for future dates

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_segment(value: str) -> str:
    cleaned = _SAFE_SEGMENT.sub("_", str(value or "").strip())
    return cleaned or "_"


def cache_path(espn_id: str, date_code: str) -> str:
    return os.path.join(CACHE_ROOT, _safe_segment(espn_id), f"{_safe_segment(date_code)}.json")


def _read_cache(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict) and "ts" in payload and "data" in payload:
            return payload
    except Exception:
        pass
    return None


def _write_cache(path: str, data) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "data": data}, fh)
        os.replace(tmp, path)
    except Exception:
        pass


def fetch_scoreboard(
    espn_id: str,
    date_code: str,
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_age_seconds: int | None = None,
):
    """Return the ESPN scoreboard payload for ``(espn_id, yyyymmdd)``.

    Serves a fresh disk-cache copy when available; otherwise fetches live and
    caches atomically. Raises on network/HTTP errors like the raw fetch did.

    ``max_age_seconds`` overrides the default TTL for callers that need
    tighter freshness (e.g. short-interval result pollers); pass 0 to always
    revalidate against the network while still refreshing the cache file.
    """
    path = cache_path(espn_id, date_code)
    cached = _read_cache(path)
    if cached is not None:
        if max_age_seconds is None:
            try:
                date_ts = time.mktime(time.strptime(date_code, "%Y%m%d"))
                is_past_or_today = date_ts <= time.mktime(time.localtime())
            except ValueError:
                is_past_or_today = True
            ttl = TTL_PAST_SECONDS if is_past_or_today else TTL_FUTURE_SECONDS
        else:
            ttl = max(0, int(max_age_seconds))
        if time.time() - float(cached.get("ts", 0)) < ttl:
            return cached["data"]

    url = (
        "https://site.api.espn.com/apis/site/v2/sports/soccer/"
        f"{espn_id}/scoreboard?dates={date_code}"
    )
    request = urllib.request.Request(url)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    _write_cache(path, data)
    return data


def clear_cache(espn_id: str | None = None) -> int:
    """Delete cached payloads (all leagues, or one espn_id). Returns file count."""
    import shutil

    root = os.path.join(CACHE_ROOT, _safe_segment(espn_id)) if espn_id else CACHE_ROOT
    count = 0
    if not os.path.isdir(root):
        return 0
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(".json"):
                try:
                    os.remove(os.path.join(dirpath, name))
                    count += 1
                except Exception:
                    continue
    if espn_id is None:
        shutil.rmtree(root, ignore_errors=True)
    return count
