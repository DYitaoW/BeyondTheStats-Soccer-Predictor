"""
Shared helper utilities for Website route blueprints.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, List, Optional, Tuple
from zoneinfo import ZoneInfo

_ROUTES_DIR = os.path.dirname(os.path.abspath(__file__))
_WEBSITE_DIR = os.path.dirname(_ROUTES_DIR)
if _WEBSITE_DIR not in sys.path:
    sys.path.insert(0, _WEBSITE_DIR)
_PROJECT_DIR = os.path.dirname(_WEBSITE_DIR)
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)
_SHARED_DIR = os.path.join(_PROJECT_DIR, "shared")
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)
    
from .compat import request
import pandas as pd

import config
from team_utils import _team_name_for_display
from espn_api import _fetch_competition_schedule


# ── KeyError Tracking ────────────────────────────────────────────────
_key_error_log: list[dict] = []
_key_error_log_lock = Lock()
_KEY_ERROR_LOG_MAX = 200


def log_key_error(context: str, exc: KeyError):
    """Record a KeyError with context for the /api/key-errors endpoint."""
    entry = {
        "context": context,
        "key": str(exc.args[0]) if exc.args else None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with _key_error_log_lock:
        _key_error_log.append(entry)
        if len(_key_error_log) > _KEY_ERROR_LOG_MAX:
            _key_error_log.pop(0)


def get_key_error_log() -> list[dict]:
    with _key_error_log_lock:
        return list(_key_error_log)


def clear_key_error_log() -> None:
    with _key_error_log_lock:
        _key_error_log.clear()


# ── Mutation Authorization ───────────────────────────────────────────
def mutation_authorized() -> bool:
    """Return True if the caller has a valid admin token."""
    token = request.headers.get("X-Admin-Token", "").strip()
    if not token:
        auth = request.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    return bool(token and config.MUTATION_API_TOKEN and token == config.MUTATION_API_TOKEN)


# ── Redeem Code Parsing ──────────────────────────────────────────────
def parse_redeem_code(raw: Any) -> Optional[str]:
    """Parse a redeem code for case-sensitive comparison (letters and digits only)."""
    text = str(raw or "").strip()
    if not text:
        return ""
    if not text.isalnum():
        return None
    return text


def load_redeem_code_entries() -> list[dict]:
    """Load redeem codes from repo Data/redeem_codes.json."""
    path = config.REDEEM_CODES_FILE
    if not os.path.isfile(path):
        example_path = getattr(config, "REDEEM_CODES_EXAMPLE_FILE", "")
        if example_path and os.path.isfile(example_path):
            path = example_path
        else:
            return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    entries = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        if code is None or str(code).strip() == "":
            continue
        parsed = parse_redeem_code(code)
        if not parsed:
            continue
        entries.append({
            "code": parsed,
            "value": item.get("value", True),
        })
    return entries


# ── Upcoming and Match Row Helpers ────────────────────────────────────
_ALL_UPCOMING_SOURCES = [
    ("global", config.GLOBAL_UPCOMING_FILE),
    ("global_projected", config.GLOBAL_PROJECTED_MATCHES_FILE),
    ("mls", config.MLS_UPCOMING_FILE),
    ("extra", config.EXTRA_UPCOMING_FILE),
    ("extra_projected", config.EXTRA_PROJECTED_MATCHES_FILE),
    ("cups", config.CUP_UPCOMING_FILE),
    ("national", config.NATIONAL_UPCOMING_FILE),
    ("friendlies", config.FRIENDLIES_UPCOMING_FILE),
]


def merged_upcoming_file_is_fresh(merged_path: str) -> bool:
    """True when merged_path exists and is at least as new as every source CSV."""
    if not merged_path or not os.path.exists(merged_path):
        return False
    try:
        merged_mtime = os.path.getmtime(merged_path)
    except OSError:
        return False
    required_sources = {
        "mls": config.MLS_UPCOMING_FILE,
        "extra": config.EXTRA_UPCOMING_FILE,
        "cups": config.CUP_UPCOMING_FILE,
    }
    for _, csv_path in _ALL_UPCOMING_SOURCES:
        if not csv_path:
            continue
        if not os.path.exists(csv_path):
            if csv_path in required_sources.values():
                return False
            continue
        try:
            if os.path.getmtime(csv_path) > merged_mtime + 1.0:
                return False
        except OSError:
            continue
    return True


def regional_espn_schedule_fallback(existing_rows: list) -> list:
    """Add schedule-only MLS/Liga MX rows when the generated MLS CSV is absent/empty."""
    rows = list(existing_rows or [])
    present = {
        str(row.get("competition", "")).strip()
        for row in rows
        if str(row.get("competition", "")).strip()
    }
    for competition in ("United States/MLS", "Mexico/Liga MX"):
        if competition in present:
            continue
        espn_id = config.LIVE_SCORE_COMPETITIONS.get(competition)
        if not espn_id:
            continue
        try:
            games = _fetch_competition_schedule(competition, espn_id, days_forward=365) or []
        except Exception:
            games = []
        for game in games:
            if str(game.get("status", "")).strip().lower() not in ("", "pre"):
                continue
            match_date = str(game.get("match_date", "") or "").strip()
            home = _team_name_for_display(game.get("home_team", ""))
            away = _team_name_for_display(game.get("away_team", ""))
            if not match_date or not home or not away:
                continue
            rows.append({
                "match_id": str(game.get("match_id", "") or ""),
                "match_date": match_date,
                "match_date_iso": match_date,
                "match_datetime_et": str(game.get("kickoff_utc", "") or ""),
                "competition": competition,
                "home_team": home,
                "away_team": away,
                "schedule_only": True,
                "has_prediction": False,
                "prediction_quality": "no_prediction",
                "prediction_note": "Fixture available; prediction pending team/model mapping.",
                "live_updates": False,
                "live_status": "scheduled",
            })
    return rows


def exclude_upcoming_only_rows(rows: list) -> list:
    """Drop competitions that have a dedicated upcoming source only."""
    blocked = config.UPCOMING_ONLY_COMPETITIONS | config.LEAGUE_API_EXCLUDED_COMPETITIONS
    return [r for r in rows if str(r.get("competition", "")).strip() not in blocked]


def is_league_api_competition(comp_name: Any) -> bool:
    """Return True when a competition should appear in league-facing APIs."""
    comp = str(comp_name or "").strip()
    if not comp:
        return False
    if comp in config.UPCOMING_ONLY_COMPETITIONS:
        return False
    if comp in config.LEAGUE_API_EXCLUDED_COMPETITIONS:
        return False
    if config.is_national_team_competition(comp):
        return False
    return True


def filter_league_tables_payload(data: dict) -> dict:
    """Remove fallback-only / upcoming-only leagues from table API payloads."""
    excluded = config.LEAGUE_API_EXCLUDED_COMPETITIONS | config.UPCOMING_ONLY_COMPETITIONS

    def _keep(name):
        return name not in excluded and not config.is_national_team_competition(name)

    leagues = [c for c in (data.get("leagues") or []) if _keep(c)]
    tables = {
        k: v for k, v in (data.get("tables") or {}).items()
        if _keep(k)
    }
    fixtures = data.get("fixtures")
    if isinstance(fixtures, dict):
        fixtures = {k: v for k, v in fixtures.items() if _keep(k)}
    return {**data, "leagues": leagues, "tables": tables, "fixtures": fixtures}


def pick_league_winner_row(rows: list[dict]) -> Optional[dict]:
    if not rows:
        return None
    return max(
        rows,
        key=lambda row: (
            float(row.get("win_league_pct") or 0),
            -(int(row.get("position") or 999)),
        ),
    )


def date_window_bounds() -> Tuple[datetime.date, datetime.date]:
    """Return the website's stored match window as ISO date bounds."""
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    season_end = today_et + timedelta(days=365)
    current_week_start = today_et - timedelta(days=today_et.weekday())
    return current_week_start - timedelta(days=7), season_end


def parse_query_date(value: Any, fallback: datetime.date) -> datetime.date:
    """Parse a YYYY-MM-DD query date, falling back when invalid."""
    try:
        return datetime.strptime(str(value or ""), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return fallback


def row_date_iso(row: dict) -> str:
    """Return the normalized ISO match date used by website filters."""
    raw = str(row.get("match_date_iso") or row.get("match_date") or "").strip()
    if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-":
        return raw[:10]
    parsed = pd.to_datetime(raw, errors="coerce")
    if pd.isna(parsed):
        return ""
    return parsed.strftime("%Y-%m-%d")


def group_rows_by_league(rows: list) -> list:
    """Group matches by league, keeping games chronologically ordered."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.get("competition") or "Other"].append(row)
    groups = []
    for league, league_rows in grouped.items():
        league_rows.sort(key=lambda r: (
            row_date_iso(r),
            str(r.get("match_datetime_et") or r.get("match_datetime_utc") or ""),
            str(r.get("home_team") or ""),
        ))
        groups.append({"league": league, "matches": league_rows})
    groups.sort(key=lambda group: (
        row_date_iso(group["matches"][0]) if group["matches"] else "",
        group["league"],
    ))
    return groups


def week_bounds_from_iso(date_str: str) -> Optional[Tuple[str, str, str]]:
    """Given an ISO date YYYY-MM-DD, compute (monday_iso, sunday_iso, label)."""
    if not date_str:
        return None
    raw = str(date_str).strip()
    if len(raw) < 10:
        return None
    try:
        dt = datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    monday = dt - timedelta(days=dt.weekday())    
    sunday = monday + timedelta(days=6)
    if monday.year == sunday.year:
        label = f"{monday.strftime('%b %d')} - {sunday.strftime('%b %d, %Y')}"
    else:
        label = f"{monday.strftime('%b %d, %Y')} - {sunday.strftime('%b %d, %Y')}"
    return monday.isoformat(), sunday.isoformat(), label


def merge_projected_with_season_tables(projected: dict, season_data: Optional[dict], dataset_competitions: tuple[str, ...]) -> dict:
    """Prefer projected CSV rows; fill gaps from season rosters and placeholders."""
    from standings import _fill_placeholder_tables
    tables = dict(projected.get("tables") or {})
    leagues = set(projected.get("leagues") or [])
    if season_data:
        for comp, rows in (season_data.get("tables") or {}).items():
            if comp not in tables or not tables.get(comp):
                tables[comp] = rows
            leagues.add(comp)
    for comp in dataset_competitions:
        leagues.add(comp)
    data = {"leagues": sorted(leagues), "tables": tables}
    _fill_placeholder_tables(data)
    data["leagues"] = sorted(set(data.get("leagues") or []) | set(dataset_competitions))
    return filter_league_tables_payload(data)


def build_global_api_payload() -> dict:
    from predictions import _load_projected_tables, _load_current_season_tables
    projected = _load_projected_tables(config.GLOBAL_PROJECTED_TABLE_FILE)
    season_data = _load_current_season_tables()
    return merge_projected_with_season_tables(
        projected,
        season_data,
        config.GLOBAL_DATASET_COMPETITIONS,
    )


def build_extra_api_payload() -> dict:
    from predictions import _load_projected_tables, _load_current_season_tables
    projected = _load_projected_tables(config.EXTRA_PROJECTED_TABLE_FILE)
    season_data = _load_current_season_tables()
    return merge_projected_with_season_tables(
        projected,
        season_data,
        config.EXTRA_DATASET_COMPETITIONS,
    )


def build_mls_api_payload() -> dict:
    """Shared MLS payload for ``/api/league-tables?mode=mls``."""
    from standings import _fill_placeholder_tables
    from predictions import (
        _build_mls_winners_odds_bundle,
        _file_mtime_utc,
        _load_all_fixtures_by_competition,
        _load_current_season_tables,
        _load_json_payload,
        _load_projected_tables,
        _normalize_mls_conference_tables,
    )
    projected = _load_projected_tables(config.MLS_PROJECTED_TABLE_FILE)
    last_refresh = (
        _file_mtime_utc(config.MLS_PROJECTED_TABLE_FILE)
        if os.path.exists(config.MLS_PROJECTED_TABLE_FILE)
        else None
    )

    tables = dict(projected.get("tables") or {})
    leagues = set(projected.get("leagues") or [])

    season_data = _load_current_season_tables()
    if season_data:
        for comp, rows in (season_data.get("tables") or {}).items():
            if comp not in tables or not tables.get(comp):
                tables[comp] = rows
            leagues.add(comp)

    for comp in config.MLS_DATASET_COMPETITIONS:
        leagues.add(comp)

    data = {"leagues": sorted(leagues), "tables": tables}
    _fill_placeholder_tables(data)
    data["leagues"] = sorted(set(data.get("leagues") or []) | set(config.MLS_DATASET_COMPETITIONS))

    payload = {
        "leagues": data.get("leagues") or [],
        "tables": data.get("tables") or {},
        "bracket": _load_json_payload(config.MLS_PROJECTED_BRACKET_FILE),
        "fixtures": _load_all_fixtures_by_competition(config.MLS_UPCOMING_FILE),
        "last_prediction_refresh": last_refresh,
        "mls_winners_odds": _build_mls_winners_odds_bundle(),
    }
    _normalize_mls_conference_tables(payload)
    return payload

