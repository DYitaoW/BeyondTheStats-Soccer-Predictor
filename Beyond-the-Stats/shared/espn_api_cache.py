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
import ssl
from datetime import date, datetime, timedelta
from typing import Iterable, Iterator

DEFAULT_TIMEOUT_SECONDS = 30
TTL_PAST_SECONDS = 2 * 3600    # 2h for today and earlier
TTL_FUTURE_SECONDS = 24 * 3600  # 24h for future dates

# Month scoreboards (``dates=YYYYMM&limit=1000``): ESPN accepts month queries
# for every league / cup / friendlies feed even where it rejects date ranges,
# and returns exactly the events a day-by-day walk finds. One cached request
# per month replaced day walks of 180-366 requests per competition (cups
# ~1,700 requests, friendlies 732 per run). The cached month payloads are the
# store of found games; how often a month is re-fetched depends on distance:
MONTH_TTL_CURRENT_SECONDS = 3 * 3600      # month containing today
MONTH_TTL_NEAR_SECONDS = 12 * 3600        # next month starting within NEAR_DAYS
MONTH_TTL_FAR_SECONDS = 3 * 24 * 3600     # later months (cups: weeks between rounds)
MONTH_TTL_PAST_SECONDS = 24 * 3600        # fully past months (late result corrections)
MONTH_NEAR_DAYS = 21
# Friendlies cluster in international breaks / pre-season and are announced
# weeks ahead: far months are re-checked weekly.
SPARSE_FEED_ESPN_IDS = frozenset({"fifa.friendly", "club.friendly"})
MONTH_TTL_SPARSE_FAR_SECONDS = 7 * 24 * 3600

try:
    import certifi
    _SSL_CONTEXT: ssl.SSLContext | None = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CONTEXT = None


def _urlopen(req, timeout: int = DEFAULT_TIMEOUT_SECONDS):
    """Open a URL with certifi SSL context to ensure clean SSL verification on macOS."""
    if _SSL_CONTEXT is not None:
        return urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT)
    return urllib.request.urlopen(req, timeout=timeout)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # Beyond-the-Stats/
CACHE_ROOT = os.path.join(BASE_DIR, "Output", "Cache", "espn")

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
    with _urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    _write_cache(path, data)
    return data


def month_ttl_seconds(espn_id: str, month_start: date, today: date | None = None) -> int:
    """Cache lifetime for one league-month (see ``MONTH_TTL_*``)."""
    today = today or date.today()
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    if next_month <= today - timedelta(days=3):
        return MONTH_TTL_PAST_SECONDS
    if next_month <= today:
        # Just-finished month: late games / results still settling.
        return MONTH_TTL_CURRENT_SECONDS
    if month_start <= today < next_month:
        return MONTH_TTL_CURRENT_SECONDS
    if (month_start - today).days <= MONTH_NEAR_DAYS:
        return MONTH_TTL_NEAR_SECONDS
    if str(espn_id or "").strip().lower() in SPARSE_FEED_ESPN_IDS:
        return MONTH_TTL_SPARSE_FAR_SECONDS
    return MONTH_TTL_FAR_SECONDS


def fetch_scoreboard_month(
    espn_id: str,
    month_start,
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_age_seconds: int | None = None,
):
    """ESPN scoreboard for one calendar month (``dates=YYYYMM&limit=1000``), disk-cached."""
    month_d = _to_date(month_start)
    if month_d is None:
        raise ValueError(f"bad month: {month_start!r}")
    month_d = month_d.replace(day=1)
    code = month_d.strftime("%Y%m")
    path = cache_path(espn_id, f"m{code}")
    cached = _read_cache(path)
    ttl = month_ttl_seconds(espn_id, month_d) if max_age_seconds is None else max(0, int(max_age_seconds))
    if cached is not None and time.time() - float(cached.get("ts", 0)) < ttl:
        return cached["data"]
    url = (
        "https://site.api.espn.com/apis/site/v2/sports/soccer/"
        f"{espn_id}/scoreboard?dates={code}&limit=1000"
    )
    request = urllib.request.Request(url)
    with _urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    _write_cache(path, data)
    return data


def _months_between(start_d: date, end_d: date) -> list[date]:
    out = []
    cursor = start_d.replace(day=1)
    while cursor <= end_d:
        out.append(cursor)
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def _event_in_window(event: dict, start_d: date, end_d: date) -> bool:
    """Event kickoff (UTC or US-Eastern calendar date) inside ``[start, end]``."""
    raw = str(event.get("date") or "").strip()
    if not raw:
        return True
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    days = {dt.date()}
    try:
        from zoneinfo import ZoneInfo

        days.add(dt.astimezone(ZoneInfo("America/New_York")).date())
    except Exception:
        pass
    return any(start_d <= d <= end_d for d in days)


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
    with _urlopen(request, timeout=timeout) as response:
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

    Uses cached month scoreboards (``fetch_scoreboard_month``; events outside
    the window are dropped). Only if a month query fails: a single multi-day
    range query, then the default scoreboard plus a smart day walk (Tue/Wed
    only for UEFA).
    """
    start_d = _to_date(start)
    end_d = _to_date(end)
    if start_d is None or end_d is None or start_d > end_d:
        return []

    events: list[dict] = []
    seen_ids: set[str] = set()

    def _absorb(payload, window: bool = False) -> int:
        added = 0
        if not isinstance(payload, dict):
            return 0
        for event in payload.get("events") or []:
            if not isinstance(event, dict):
                continue
            if window and not _event_in_window(event, start_d, end_d):
                continue
            eid = str(event.get("id") or event.get("uid") or "")
            if eid and eid in seen_ids:
                continue
            if eid:
                seen_ids.add(eid)
            events.append(event)
            added += 1
        return added

    # 0) Cached month scoreboards (one request per month, tiered TTL).
    months = _months_between(start_d, end_d)
    try:
        for month_d in months:
            _absorb(fetch_scoreboard_month(espn_id, month_d, timeout=timeout), window=True)
        if progress_label:
            print(
                f"  [espn] {progress_label}: {len(months)} month(s) {start_d}->{end_d} -> {len(events)} events",
                flush=True,
            )
        return events
    except Exception as exc:
        if progress_label:
            print(f"  [espn] {progress_label}: month query failed ({exc}); falling back to day walk", flush=True)
        events.clear()
        seen_ids.clear()

    # 1) Prefer a single range pull when ESPN allows it.
    date_param = f"{start_d.strftime('%Y%m%d')}-{end_d.strftime('%Y%m%d')}"
    range_url = (
        "https://site.api.espn.com/apis/site/v2/sports/soccer/"
        f"{espn_id}/scoreboard?dates={date_param}&limit=1000"
    )
    try:
        request = urllib.request.Request(range_url)
        with _urlopen(request, timeout=timeout) as response:
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
