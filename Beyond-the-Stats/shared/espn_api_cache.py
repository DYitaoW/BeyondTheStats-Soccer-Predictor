"""Per-league ESPN scoreboard disk cache.

The cup/friendlies upcoming-fixture crawlers request ESPN scoreboard payloads
per (league, day). UEFA scoreboards reject multi-day ``dates=YYYYMMDD-YYYYMMDD``
queries (HTTP 400), so naive day-by-day crawls of a 180-day window × many cups
can hang the pipeline for an hour before any Monte Carlo runs.

This module:
- Persists each payload under ``Output/Cache/espn/<espn_id>/<yyyymmdd>.json``
- Exposes a smart date iterator that, for UEFA leagues, only hits typical
  match nights (Tue/Wed) plus the default scoreboard, instead of every calendar day
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Iterable, Iterator

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # Beyond-the-Stats/
CACHE_ROOT = os.path.join(BASE_DIR, "Output", "Cache", "espn")

DEFAULT_TIMEOUT_SECONDS = 30
TTL_PAST_SECONDS = 2 * 3600    # 2h for today and earlier
TTL_FUTURE_SECONDS = 24 * 3600  # 24h for future dates

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")

# UEFA club competitions play midweek; scanning every day wastes ~5× the requests.
_UEFA_ESPN_PREFIXES = ("uefa.",)
# Monday=0 … Sunday=6 — UCL is Tue/Wed; UEL/UECL league phase is often Thu.
_UEFA_MATCH_WEEKDAYS = frozenset({1, 2, 3})


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


def is_uefa_espn_id(espn_id: str) -> bool:
    text = str(espn_id or "").strip().lower()
    return any(text.startswith(prefix) for prefix in _UEFA_ESPN_PREFIXES)


def _to_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def iter_scoreboard_dates(
    espn_id: str,
    start,
    end,
    *,
    force_all_days: bool = False,
) -> list[date]:
    """Return scoreboard query dates for ``[start, end]``.

    UEFA leagues: Tue/Wed only (plus endpoints). Other leagues: every day.
    """
    start_d = _to_date(start)
    end_d = _to_date(end)
    if start_d is None or end_d is None or start_d > end_d:
        return []

    uefa = is_uefa_espn_id(espn_id) and not force_all_days
    out: list[date] = []
    cursor = start_d
    while cursor <= end_d:
        if (not uefa) or cursor.weekday() in _UEFA_MATCH_WEEKDAYS or cursor in {start_d, end_d}:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


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


def fetch_scoreboard_default(
    espn_id: str,
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_age_seconds: int | None = None,
):
    """Fetch the league's default scoreboard (current/next slate, no dates=)."""
    path = cache_path(espn_id, "_default")
    cached = _read_cache(path)
    ttl = TTL_PAST_SECONDS if max_age_seconds is None else max(0, int(max_age_seconds))
    if cached is not None and time.time() - float(cached.get("ts", 0)) < ttl:
        return cached["data"]
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{espn_id}/scoreboard"
    request = urllib.request.Request(url)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    _write_cache(path, data)
    return data


def fetch_scoreboard_range(
    espn_id: str,
    start,
    end,
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    include_default: bool = True,
    force_all_days: bool = False,
    progress_label: str | None = None,
) -> list[dict]:
    """Collect ESPN events across ``[start, end]``.

    Tries a single multi-day scoreboard query first (works for many domestic
    leagues). On HTTP 400 / empty (UEFA rejects ranges), falls back to a
    smart day walk — Tue/Wed only for UEFA — and merges the default scoreboard.
    """
    start_d = _to_date(start)
    end_d = _to_date(end)
    if start_d is None or end_d is None or start_d > end_d:
        return []

    events: list[dict] = []
    seen_ids: set[str] = set()

    def _absorb(payload) -> int:
        added = 0
        if not isinstance(payload, dict):
            return 0
        for event in payload.get("events") or []:
            if not isinstance(event, dict):
                continue
            eid = str(event.get("id") or event.get("uid") or "")
            if eid and eid in seen_ids:
                continue
            if eid:
                seen_ids.add(eid)
            events.append(event)
            added += 1
        return added

    # 1) Prefer a single range pull when ESPN allows it.
    date_param = f"{start_d.strftime('%Y%m%d')}-{end_d.strftime('%Y%m%d')}"
    range_url = (
        "https://site.api.espn.com/apis/site/v2/sports/soccer/"
        f"{espn_id}/scoreboard?dates={date_param}&limit=1000"
    )
    try:
        request = urllib.request.Request(range_url)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if _absorb(payload):
            if progress_label:
                print(
                    f"  [espn] {progress_label}: range {date_param} -> {len(events)} events",
                    flush=True,
                )
            return events
    except Exception:
        pass

    # 2) Default scoreboard often already holds the next matchday slate.
    if include_default:
        try:
            _absorb(fetch_scoreboard_default(espn_id, timeout=timeout))
        except Exception:
            pass

    # 3) Smart day walk (UEFA = Tue/Wed only).
    days = iter_scoreboard_dates(espn_id, start_d, end_d, force_all_days=force_all_days)
    if progress_label:
        print(
            f"  [espn] {progress_label}: walking {len(days)} day(s) "
            f"{start_d}->{end_d}"
            + (" (UEFA Tue/Wed/Thu)" if is_uefa_espn_id(espn_id) and not force_all_days else ""),
            flush=True,
        )
    for idx, day in enumerate(days, start=1):
        code = day.strftime("%Y%m%d")
        try:
            _absorb(fetch_scoreboard(espn_id, code, timeout=timeout))
        except Exception as exc:
            if progress_label and idx <= 3:
                print(f"  [espn] {progress_label}: skip {code}: {exc}", flush=True)
            continue
        if progress_label and (idx == 1 or idx == len(days) or idx % 15 == 0):
            print(
                f"  [espn] {progress_label}: {idx}/{len(days)} days, {len(events)} events",
                flush=True,
            )
    return events


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
