"""
Match prediction, team query, fixture and head-to-head API routes.
"""

from __future__ import annotations

import json
import os
import random as _random
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .compat import Blueprint, Request, redirect, render_template, send_from_directory, request, jsonify
import pandas as pd

import config
from cache import _cached_response
from team_utils import _team_name_for_db, _team_name_for_display
from espn_api import _fetch_team_info, _ROSTER_CACHE
from team_mappings import (
    build_app_teams_catalog_payload,
    build_predictor_teams_payload,
    build_unmapped_espn_payload,
)
from accuracy_tracker import _load_prediction_tracking
from predictions import (
    _build_past_game_prediction_lookup,
    _collect_live_past_game_rows,
    _enrich_json_past_row,
    _get_static_predictions,
    _is_placeholder_game,
    _load_h2h_and_form,
    _load_json_payload,
    _load_team_recent_matches,
    _load_teams_from_team_data,
    _load_upcoming_rows,
    _merge_prediction_onto_past_row,
    _normalize_h2h_payload,
    _normalize_recent_form_payload,
    _past_row_date_iso,
    _past_row_looks_api_complete,
    _predict,
    _utc_to_et,
    get_context,
    get_last_pipeline_run,
    pm_extra,
    pm_global,
    pm_mls,
)
from routes.helpers import (
    _ALL_UPCOMING_SOURCES,
    build_extra_api_payload,
    build_global_api_payload,
    build_mls_api_payload,
    date_window_bounds,
    exclude_upcoming_only_rows,
    group_rows_by_league,
    merged_upcoming_file_is_fresh,
    parse_query_date,
    pick_league_winner_row,
    regional_espn_schedule_fallback,
    row_date_iso,
    week_bounds_from_iso,
)

predictions_bp = Blueprint("predictions", __name__)
predictions_router = predictions_bp

_UPCOMING_MODE_MAP = {
    "global": (config.GLOBAL_UPCOMING_FILE, "global"),
    "mls": (config.MLS_UPCOMING_FILE, "mls"),
    "extra": (config.EXTRA_UPCOMING_FILE, "extra"),
    "cups": (config.CUP_UPCOMING_FILE, "cups"),
    "world-cup": (config.NATIONAL_UPCOMING_FILE, "national"),
    "friendlies": (config.FRIENDLIES_UPCOMING_FILE, "friendlies"),
}


def _to_float(val):
    try:
        return round(float(val), 1)
    except (ValueError, TypeError):
        return None


def _to_compact_upcoming_row(r: dict) -> dict:
    """Strip bulky secondary markets, last-5 match logs, and verbose reasoning for lightweight web/list views."""
    return {
        "competition": r.get("competition", ""),
        "match_date": r.get("match_date", ""),
        "match_date_iso": r.get("match_date_iso", ""),
        "match_datetime_et": r.get("match_datetime_et", ""),
        "match_datetime_utc": r.get("match_datetime_utc", ""),
        "weekday": r.get("weekday", ""),
        "date_label": r.get("date_label", ""),
        "time_label": r.get("time_label", ""),
        "home_team": r.get("home_team", ""),
        "away_team": r.get("away_team", ""),
        "match_id": r.get("match_id"),
        "predicted_result": r.get("predicted_result", ""),
        "winner_label": r.get("winner_label", ""),
        "prob_home": r.get("prob_home"),
        "prob_draw": r.get("prob_draw"),
        "prob_away": r.get("prob_away"),
        "pred_home_goals": r.get("pred_home_goals"),
        "pred_away_goals": r.get("pred_away_goals"),
        "actual_home_goals": r.get("actual_home_goals"),
        "actual_away_goals": r.get("actual_away_goals"),
        "actual_result": r.get("actual_result", ""),
        "is_correct": r.get("is_correct", ""),
        "has_prediction": r.get("has_prediction", True),
        "prediction_quality": r.get("prediction_quality", ""),
        "schedule_only": r.get("schedule_only", False),
        "source": r.get("source", ""),
        # Live overlay fields when game is active
        "live_status": r.get("live_status"),
        "live_clock": r.get("live_clock"),
        "live_period": r.get("live_period"),
        "live_updates": r.get("live_updates", False),
    }


def _resolve_team_api_payload(team_input: str, mode: str):
    """Build the single-team API payload used by ``/api/team``."""
    mode = (mode or "global").strip().lower()
    if mode not in {"global", "mls", "extra"}:
        mode = "global"

    if mode == "mls":
        pm_mod = pm_mls
    elif mode == "extra":
        pm_mod = pm_extra
    else:
        pm_mod = pm_global

    head_to_head, current_form = _load_h2h_and_form(pm_mod)
    form_teams = current_form.get("teams", {}) if isinstance(current_form, dict) else {}
    h2h_root = head_to_head if isinstance(head_to_head, dict) else {}

    team = _team_name_for_db(team_input)
    team_lower = team.lower()

    form_key = team if team in form_teams else next(
        (name for name in form_teams if str(name).strip().lower() == team_lower),
        team,
    )
    h2h_key = team if team in h2h_root else next(
        (name for name in h2h_root if str(name).strip().lower() == team_lower),
        team,
    )

    team_form = _normalize_recent_form_payload(form_teams.get(form_key, {}))
    recent_matches = _load_team_recent_matches(team, pm_mod.PROCESSED_DIR, 10)

    all_h2h = {}
    for opponent, payload in (h2h_root.get(h2h_key) or {}).items():
        all_h2h[opponent] = _normalize_h2h_payload(payload)

    upcoming = []
    csv_path = config.UPCOMING_CSV_FILES.get(mode) or config.UPCOMING_CSV_FILES.get("global")
    if csv_path and os.path.exists(csv_path):
        try:
            frame = pd.read_csv(csv_path, dtype=str)
            for _, row in frame.iterrows():
                home = str(row.get("home_team", "") or "").strip()
                away = str(row.get("away_team", "") or "").strip()
                if team_lower not in (home.lower(), away.lower()):
                    continue
                upcoming.append({
                    "competition": str(row.get("competition", "") or "").strip(),
                    "match_date": str(row.get("match_date", "") or "").strip(),
                    "match_datetime_utc": _utc_to_et(str(row.get("match_datetime_utc", "") or "").strip()),
                    "home_team": home,
                    "away_team": away,
                    "predicted_result": str(row.get("predicted_result", "") or "").strip(),
                })
        except Exception:
            pass

    team_pred_data = {}
    tracking = _load_prediction_tracking()
    pt = tracking.get("per_team", {}) if isinstance(tracking, dict) else {}
    if isinstance(pt, dict):
        if team in pt:
            team_pred_data = pt[team]
        else:
            for tname, tdata in pt.items():
                if str(tname).strip().lower() == team_lower:
                    team_pred_data = tdata
                    break

    from features import compute_weighted_team_form
    weighted_form = compute_weighted_team_form(recent_matches, team)

    return {
        "ok": True,
        "team": team,
        "display_team": _team_name_for_display(team),
        "mode": mode,
        "form": team_form,
        "weighted_form": weighted_form,
        "recent_matches": recent_matches,
        "upcoming_games": upcoming,
        "head_to_head": all_h2h,
        "prediction_accuracy": team_pred_data,
    }


def _build_home_league_sidebar_entries() -> list[dict]:
    priority = {
        name: index
        for index, name in enumerate(
            list(config.GLOBAL_DATASET_COMPETITIONS)
            + [config.MLS_COMPETITION, config.LIGA_MX_COMPETITION]
            + list(config.EXTRA_DATASET_COMPETITIONS)
        )
    }
    entries: list[dict] = []
    seen: set[str] = set()
    for dataset, builder in (
        ("global", build_global_api_payload),
        ("mls", build_mls_api_payload),
        ("extra", build_extra_api_payload),
    ):
        payload = builder()
        tables = payload.get("tables") or {}
        for league, rows in tables.items():
            if league in config.HOME_SIDEBAR_SKIP_COMPETITIONS or league == "__mls_bracket__":
                continue
            if not rows or league in seen:
                continue
            winner = pick_league_winner_row(rows)
            entries.append({
                "dataset": dataset,
                "league": league,
                "winner": (winner or {}).get("team") or "N/A",
                "win_pct": float((winner or {}).get("win_league_pct") or 0),
            })
            seen.add(league)
    entries.sort(
        key=lambda item: (
            priority.get(item["league"], 1000),
            item["league"],
        )
    )
    return entries


@predictions_bp.get("/api/teams")
def api_teams():
    """Return selectable teams for the requested prediction mode."""
    mode = str(request.args.get("mode", "global")).strip().lower()
    if mode not in {"global", "mls", "extra"}:
        mode = "global"
    if config.STATIC_PREDICTIONS:
        _, teams = _get_static_predictions(mode)
        if not teams:
            if mode == "mls":
                teams = _load_teams_from_team_data(pm_mls)
            elif mode == "extra":
                teams = _load_teams_from_team_data(pm_extra)
            else:
                teams = _load_teams_from_team_data(pm_global)
        display_teams = sorted({_team_name_for_display(team) for team in teams})
    else:
        try:
            teams = get_context(mode).available_teams
            display_teams = sorted({_team_name_for_display(team) for team in teams})
        except Exception:
            display_teams = []
    return jsonify({"teams": display_teams})


@predictions_bp.get("/api/teams/catalog")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_teams_catalog():
    """Return all unique canonical teams for app-available leagues."""
    competition = str(request.args.get("competition", "")).strip() or None
    payload = build_app_teams_catalog_payload(competition_filter=competition)
    if not payload.get("ok"):
        return jsonify(payload), 404
    return jsonify(payload)


@predictions_bp.get("/api/team")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_team():
    """Return form, recent results, upcoming games, and H2H for one team."""
    team_input = request.args.get("team", "").strip()
    mode = request.args.get("mode", "global").strip().lower()
    if not team_input:
        return jsonify({"ok": False, "error": "Missing team"}), 400
    return jsonify(_resolve_team_api_payload(team_input, mode))


@predictions_bp.get("/api/team/<path:team_name>")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_team_by_path(team_name):
    """Path-style alias for ``/api/team?team=...`` (e.g. ``/api/team/Arsenal``)."""
    mode = request.args.get("mode", "global").strip().lower()
    team_input = str(team_name or "").strip()
    if not team_input:
        return jsonify({"ok": False, "error": "Missing team"}), 400
    return jsonify(_resolve_team_api_payload(team_input, mode))


@predictions_bp.get("/api/teams/<team_id>/roster")
def api_team_roster(team_id):
    """Return full roster and season stats for a team by ESPN team ID."""
    comp = request.args.get("competition", "").strip()
    if not comp:
        return jsonify({"ok": False, "error": "Missing 'competition' query parameter"}), 400
    if comp not in config.LIVE_SCORE_COMPETITIONS:
        return jsonify({"ok": False, "error": f"Unknown competition: {comp}"}), 400
    espn_id = config.LIVE_SCORE_COMPETITIONS[comp]
    if request.args.get("refresh", "").strip().lower() in ("1", "true"):
        _ROSTER_CACHE.pop(f"roster_{comp}_{team_id}", None)
    info = _fetch_team_info(comp, espn_id, team_id)
    if info is None:
        return jsonify({"ok": False, "error": f"Could not fetch roster for team {team_id}"}), 502
    return jsonify({"ok": True, "team": info})


@predictions_bp.get("/api/team-mappings/unmapped")
def api_team_mappings_unmapped():
    """List ESPN / API upcoming team names missing or blank in the mapping master."""
    raw_lookahead = request.args.get("lookahead_days")
    competition = str(request.args.get("competition", "")).strip() or None
    lookahead_days = None
    if raw_lookahead is not None and str(raw_lookahead).strip() != "":
        try:
            lookahead_days = int(raw_lookahead)
        except (TypeError, ValueError):
            lookahead_days = None
    return jsonify(
        build_unmapped_espn_payload(
            lookahead_days=lookahead_days,
            competition_filter=competition,
        )
    )


@predictions_bp.get("/api/team-mappings/predictor-teams")
def api_team_mappings_predictor_teams():
    """List canonical team names used by global, MLS, and extra predictors."""
    return jsonify(build_predictor_teams_payload())


def _upcoming_cache_ttl(req: Request) -> int:
    """Cache upcoming predictions until the next scheduled daily pipeline run (default 02:00 AM ET)."""
    if req.query_params.get("window", "").strip() == "4week":
        try:
            tz = ZoneInfo("America/New_York")
            now_et = datetime.now(tz)
            # Daily pipeline tick (02:00 AM America/New_York)
            target = now_et.replace(hour=2, minute=0, second=0, microsecond=0)
            if target <= now_et:
                target = target + timedelta(days=1)
            remaining_seconds = int((target - now_et).total_seconds())
            return max(60, min(remaining_seconds, 86400))
        except Exception:
            return 86400
    return config.CACHE_TTL_LIVE


@predictions_bp.get("/api/upcoming/<mode>")
@_cached_response(ttl=_upcoming_cache_ttl)
def api_upcoming(mode):
    """Return upcoming prediction rows for the given source mode."""
    month = str(request.args.get("month", "")).strip()
    window = str(request.args.get("window", "")).strip()
    if window == "4week":
        window_days = 28
        window_include_past = True
    elif window == "2week":
        window_days = 14
        window_include_past = False
    else:
        window_days = None
        window_include_past = False
    is_filtered = bool(month or window)

    compact = str(request.args.get("compact", "")).strip().lower() in {"1", "true", "yes"}

    limit = None
    offset = 0
    raw_limit = request.args.get("limit", "").strip()
    raw_offset = request.args.get("offset", "").strip()
    if raw_limit.isdigit():
        limit = max(1, int(raw_limit))
    if raw_offset.isdigit():
        offset = max(0, int(raw_offset))

    def _match_window(date_iso: str) -> bool:
        if month:
            if not str(date_iso).startswith(month):
                return False
        if window_days:
            try:
                d = datetime.strptime(str(date_iso)[:10], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                return False
            if window_include_past:
                try:
                    today_et = datetime.now(ZoneInfo("America/New_York")).date()
                except Exception:
                    today_et = datetime.now(timezone.utc).date()
                lower = today_et - timedelta(days=30)
                cutoff = today_et + timedelta(days=21)
            else:
                today = datetime.now(timezone.utc).date()
                lower = today
                cutoff = today + timedelta(days=window_days)
            if d < lower or d > cutoff:
                return False
        return True

    four_week_mode = window_include_past
    date_range = "all" if four_week_mode else "upcoming"

    if mode == "global":
        if four_week_mode and merged_upcoming_file_is_fresh(config.FOUR_WEEK_WINDOW_FILE):
            all_rows, combined_stats, combined_league_stats = \
                _load_upcoming_rows(config.FOUR_WEEK_WINDOW_FILE, "global", date_range="all")
            combined_stats = dict(combined_stats or {})
            combined_league_stats = {ls.get("competition", ""): ls for ls in (combined_league_stats or []) if ls.get("competition")}
        elif (not four_week_mode) and merged_upcoming_file_is_fresh(config.ALL_UPCOMING_FILE):
            all_rows, combined_stats, combined_league_stats = \
                _load_upcoming_rows(config.ALL_UPCOMING_FILE, "global", date_range=date_range, window_days=window_days)
            combined_stats = dict(combined_stats or {})
            combined_league_stats = {ls.get("competition", ""): ls for ls in (combined_league_stats or []) if ls.get("competition")}
        else:
            all_rows = []
            combined_stats = {"correct_total": 0, "total_predictions": 0, "pending_total": 0, "accuracy_pct": 0.0}
            combined_league_stats = {}
            seen_keys = set()
            for source, csv_path in _ALL_UPCOMING_SOURCES:
                rows, _st, _ls = _load_upcoming_rows(csv_path, source, date_range=date_range, window_days=window_days)
                for r in rows:
                    ck = "|".join(
                        str(r.get(k, "")).strip().lower()
                        for k in ("match_date_iso", "competition", "home_team", "away_team")
                    )
                    if ck and ck not in seen_keys:
                        seen_keys.add(ck)
                        all_rows.append(r)
                for k, v in (_st or {}).items():
                    if k not in combined_stats or isinstance(v, (int, float)):
                        combined_stats[k] = (combined_stats.get(k, 0) if isinstance(v, (int, float)) else 0) + (v if isinstance(v, (int, float)) else 0)
                for ls in (_ls or []):
                    comp = ls.get("competition", "")
                    if comp and comp not in combined_league_stats:
                        combined_league_stats[comp] = ls

        all_rows = regional_espn_schedule_fallback(all_rows)
        seen_keys = set()
        deduped_rows = []
        for row in all_rows:
            ck = "|".join(
                str(row.get(k, "")).strip().lower()
                for k in ("match_date_iso", "competition", "home_team", "away_team")
            )
            if ck and ck not in seen_keys:
                seen_keys.add(ck)
                deduped_rows.append(row)
        all_rows = deduped_rows
        if is_filtered:
            all_rows = [r for r in all_rows if _match_window(str(r.get("match_date_iso", "")))]

        all_rows.sort(key=lambda r: (
            row_date_iso(r),
            str(r.get("competition", "")),
            str(r.get("match_datetime_et", "") or r.get("match_datetime_utc", "")),
            str(r.get("home_team", "")),
        ))
        league_names = sorted({r.get("competition", "") for r in all_rows if r.get("competition")})
        available_leagues = [
            {"name": name, "live_score_tier": config.get_live_score_tier(name)}
            for name in league_names
        ]

        paged_rows = all_rows[offset : (offset + limit) if limit else None] if (limit or offset) else all_rows
        if compact:
            paged_rows = [_to_compact_upcoming_row(r) for r in paged_rows]
        return jsonify({
            "ok": True,
            "total": len(all_rows),
            "rows": paged_rows,
            "stats": combined_stats,
            "league_stats": list(combined_league_stats.values()),
            "available_leagues": available_leagues,
        })

    entry = _UPCOMING_MODE_MAP.get(mode)
    if not entry:
        return jsonify({"ok": False, "error": f"Unknown mode: {mode}"}), 400
    csv_path, source_mode = entry
    rows, stats, league_stats = _load_upcoming_rows(csv_path, source_mode, date_range=date_range, window_days=window_days)
    if mode == "mls":
        rows = regional_espn_schedule_fallback(rows)
    if is_filtered:
        rows = [r for r in rows if _match_window(str(r.get("match_date_iso", "")))]
    league_names = sorted({r.get("competition", "") for r in rows if r.get("competition")})
    available_leagues = [
        {"name": name, "live_score_tier": config.get_live_score_tier(name)}
        for name in league_names
    ]
    paged_rows = rows[offset : (offset + limit) if limit else None] if (limit or offset) else rows
    if compact:
        paged_rows = [_to_compact_upcoming_row(r) for r in paged_rows]
    return jsonify({
        "ok": True,
        "total": len(rows),
        "rows": paged_rows,
        "stats": stats,
        "league_stats": league_stats,
        "available_leagues": available_leagues,
    })


@predictions_bp.get("/api/home/league-sidebar")
@_cached_response(ttl=config.CACHE_TTL_LONG)
def api_home_league_sidebar():
    """Compact predicted-winner list for the home page league sidebar."""
    return jsonify({
        "ok": True,
        "leagues": _build_home_league_sidebar_entries(),
    })


@predictions_bp.get("/api/home/upcoming")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_home_upcoming():
    """Return home-page upcoming matches grouped by league for a date range."""
    window_start, window_end = date_window_bounds()
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    default_day = min(max(today_et, window_start), window_end)
    start_date = parse_query_date(request.args.get("start"), default_day)
    end_date = parse_query_date(request.args.get("end"), start_date)

    if end_date < start_date:
        start_date, end_date = end_date, start_date
    start_date = min(max(start_date, window_start), window_end)
    end_date = min(max(end_date, window_start), window_end)

    if merged_upcoming_file_is_fresh(config.ALL_UPCOMING_FILE):
        merged_rows, _, _ = _load_upcoming_rows(config.ALL_UPCOMING_FILE, "global", date_range="all")
        feed = merged_rows
    else:
        feed = []
        seen_keys = set()
        for source, csv_path in _ALL_UPCOMING_SOURCES:
            rows, _, _ = _load_upcoming_rows(csv_path, source, date_range="all")
            for row in rows:
                key = "|".join(
                    str(row.get(field, "")).strip().lower()
                    for field in ("match_date_iso", "competition", "home_team", "away_team")
                )
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                feed.append(row)

    feed = regional_espn_schedule_fallback(feed)

    all_rows = []
    seen_keys = set()
    for row in feed:
        comp = str(row.get("competition", "")).strip()
        if comp in config.UPCOMING_ONLY_COMPETITIONS:
            continue
        if comp in config.LEAGUE_API_EXCLUDED_COMPETITIONS:
            continue
        match_date = row_date_iso(row)
        if not match_date:
            continue
        try:
            parsed_date = datetime.strptime(match_date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if parsed_date < start_date or parsed_date > end_date:
            continue
        key = "|".join(
            str(row.get(field, "")).strip().lower()
            for field in ("match_date_iso", "competition", "home_team", "away_team")
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        all_rows.append(row)

    all_rows = exclude_upcoming_only_rows(all_rows)
    all_rows.sort(key=lambda row: (
        row_date_iso(row),
        str(row.get("competition") or ""),
        str(row.get("match_datetime_et") or row.get("match_datetime_utc") or ""),
        str(row.get("home_team") or ""),
    ))
    compact = str(request.args.get("compact", "")).strip().lower() in {"1", "true", "yes"}
    out_rows = [_to_compact_upcoming_row(r) for r in all_rows] if compact else all_rows
    return jsonify({
        "ok": True,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "groups": group_rows_by_league(out_rows),
        "rows": out_rows,
    })


@predictions_bp.get("/api/past-games")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_past_games():
    """Return completed games persisted across pipeline runs grouped by week."""
    league = request.args.get("league", "").strip()
    target_date = request.args.get("week_start", "").strip() or request.args.get("date", "").strip()
    from_date = request.args.get("from", "").strip()
    to_date = request.args.get("to", "").strip()
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (ValueError, TypeError):
        page = 1

    limit = None
    offset = 0
    raw_limit = request.args.get("limit", "").strip()
    raw_offset = request.args.get("offset", "").strip()
    if raw_limit.isdigit():
        limit = max(1, int(raw_limit))
    if raw_offset.isdigit():
        offset = max(0, int(raw_offset))

    prediction_lookup = _build_past_game_prediction_lookup()
    by_key = {}

    def _put_past_row(r, *, overwrite_incomplete_only: bool = False):
        if not isinstance(r, dict) or _is_placeholder_game(r):
            return
        row = dict(r)
        _enrich_json_past_row(row)
        ck = "|".join(
            [
                _past_row_date_iso(row),
                str(row.get("competition", "")).strip().lower(),
                str(row.get("home_team", "")).strip().lower(),
                str(row.get("away_team", "")).strip().lower(),
            ]
        )
        if not ck.strip("|"):
            return
        existing = by_key.get(ck)
        if existing and overwrite_incomplete_only and _past_row_looks_api_complete(existing):
            for key in (
                "actual_home_goals",
                "actual_away_goals",
                "actual_result",
                "home_score",
                "away_score",
                "is_correct",
            ):
                if existing.get(key) in (None, "") and row.get(key) not in (None, ""):
                    existing[key] = row[key]
            by_key[ck] = existing
            return
        if existing and _past_row_looks_api_complete(existing) and not _past_row_looks_api_complete(row):
            merged = dict(row)
            merged.update({k: v for k, v in existing.items() if v not in (None, "", [], {})})
            by_key[ck] = merged
            return
        by_key[ck] = row

    archive = None
    try:
        from shared import sqlite_store as _sqlite_store
        _sqlite_store.ensure_store()
        if _sqlite_store.count_rows("past_games") > 0:
            archive = _sqlite_store.load_past_games(
                competition_substr=league,
                from_date=from_date,
                to_date=to_date,
            )
        if _sqlite_store.count_rows("upcoming_games") > 0:
            for r in _sqlite_store.load_upcoming_games(
                competition_substr=league,
                status="settled",
                from_date=from_date,
                to_date=to_date,
            ):
                _put_past_row(r)
    except Exception:
        archive = None

    if archive is None:
        archive = _load_json_payload(config.PAST_GAMES_FILE)
    if isinstance(archive, list):
        for r in archive:
            _put_past_row(r)

    live_cutoff = (datetime.now(timezone.utc).date() - timedelta(days=60)).isoformat()
    for r in _collect_live_past_game_rows(live_cutoff):
        r = _merge_prediction_onto_past_row(r, prediction_lookup)
        _put_past_row(r, overwrite_incomplete_only=True)

    for source, csv_path in (
        ("global", config.GLOBAL_UPCOMING_FILE),
        ("mls", config.MLS_UPCOMING_FILE),
        ("extra", config.EXTRA_UPCOMING_FILE),
        ("cups", config.CUP_UPCOMING_FILE),
        ("national", config.NATIONAL_UPCOMING_FILE),
        ("friendlies", config.FRIENDLIES_UPCOMING_FILE),
    ):
        rows, _st, _ls = _load_upcoming_rows(csv_path, source, date_range="completed")
        for r in rows:
            if str(r.get("actual_result", "")).strip().upper() not in {"H", "D", "A"}:
                continue
            if _is_placeholder_game(r):
                continue
            _put_past_row(r)

    all_rows = list(by_key.values())
    if league:
        league_lower = league.lower()
        all_rows = [r for r in all_rows if league_lower in r.get("competition", "").lower()]
    if from_date:
        all_rows = [r for r in all_rows if _past_row_date_iso(r) >= from_date]
    if to_date:
        all_rows = [r for r in all_rows if _past_row_date_iso(r) <= to_date]

    all_rows.sort(key=lambda r: _past_row_date_iso(r), reverse=True)

    # Direct limit / offset pagination mode for high performance
    if limit is not None or offset > 0:
        paged_rows = all_rows[offset : (offset + limit) if limit else None]
        return jsonify({
            "ok": True,
            "total": len(all_rows),
            "limit": limit,
            "offset": offset,
            "rows": paged_rows,
        })

    weeks_map = {}
    for r in all_rows:
        d_iso = _past_row_date_iso(r)
        bounds = week_bounds_from_iso(d_iso)
        if bounds is None:
            continue
        m_iso, s_iso, label = bounds
        if m_iso not in weeks_map:
            weeks_map[m_iso] = {
                "week_start": m_iso,
                "week_end": s_iso,
                "week_label": label,
                "rows": [],
            }
        weeks_map[m_iso]["rows"].append(r)

    sorted_mondays = sorted(weeks_map.keys(), reverse=True)
    total_weeks = len(sorted_mondays)

    if target_date:
        target_bounds = week_bounds_from_iso(target_date)
        if target_bounds:
            target_mon = target_bounds[0]
            if target_mon in sorted_mondays:
                page = sorted_mondays.index(target_mon) + 1
            else:
                return jsonify({
                    "ok": True,
                    "page": 1,
                    "total_pages": total_weeks,
                    "total_weeks": total_weeks,
                    "week_start": target_mon,
                    "week_end": target_bounds[1],
                    "week_label": target_bounds[2],
                    "rows": [],
                    "total": len(all_rows),
                    "per_page": 0,
                    "available_weeks": [
                        {
                            "page": idx,
                            "week_start": m,
                            "week_end": weeks_map[m]["week_end"],
                            "week_label": weeks_map[m]["week_label"],
                            "total_games": len(weeks_map[m]["rows"]),
                        }
                        for idx, m in enumerate(sorted_mondays, start=1)
                    ],
                })

    if total_weeks > 0 and 1 <= page <= total_weeks:
        active_mon = sorted_mondays[page - 1]
        active_week = weeks_map[active_mon]
        page_rows = active_week["rows"]
        week_start = active_week["week_start"]
        week_end = active_week["week_end"]
        week_label = active_week["week_label"]
    else:
        page_rows = []
        week_start = None
        week_end = None
        week_label = None

    available_weeks = [
        {
            "page": idx,
            "week_start": m,
            "week_end": weeks_map[m]["week_end"],
            "week_label": weeks_map[m]["week_label"],
            "total_games": len(weeks_map[m]["rows"]),
        }
        for idx, m in enumerate(sorted_mondays, start=1)
    ]

    return jsonify({
        "ok": True,
        "page": page,
        "total_pages": total_weeks,
        "total_weeks": total_weeks,
        "week_start": week_start,
        "week_end": week_end,
        "week_label": week_label,
        "rows": page_rows,
        "total": len(all_rows),
        "per_page": len(page_rows),
        "available_weeks": available_weeks,
    })


@predictions_bp.get("/api/h2h")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_h2h():
    """Return head-to-head and form data for two teams."""
    team1_input = request.args.get("team1", "").strip()
    team2_input = request.args.get("team2", "").strip()
    mode = request.args.get("mode", "global").strip().lower()

    if not team1_input or not team2_input:
        return jsonify({"ok": False, "error": "Missing teams"}), 400

    if mode == "mls":
        pm_mod = pm_mls
    elif mode == "extra":
        pm_mod = pm_extra
    else:
        pm_mod = pm_global

    head_to_head, current_form = _load_h2h_and_form(pm_mod)
    form_teams = current_form.get("teams", {}) if isinstance(current_form, dict) else {}

    team1 = _team_name_for_db(team1_input)
    team2 = _team_name_for_db(team2_input)

    t1_form = _normalize_recent_form_payload(form_teams.get(team1, {}))
    t2_form = _normalize_recent_form_payload(form_teams.get(team2, {}))

    h2h_data = _normalize_h2h_payload((head_to_head or {}).get(team1, {}).get(team2))
    h2h_data_reverse = _normalize_h2h_payload((head_to_head or {}).get(team2, {}).get(team1))
    h2h_total_games = max(h2h_data.get("games", 0), h2h_data_reverse.get("games", 0))

    return jsonify({
        "ok": True,
        "team1_form": t1_form,
        "team2_form": t2_form,
        "h2h_data": h2h_data,
        "h2h_data_reverse": h2h_data_reverse,
        "h2h_total_games": h2h_total_games,
    })


@predictions_bp.post("/api/predict")
def api_predict():
    """Predict a single matchup from user input."""
    payload = request.get_json(silent=True) or request.form
    home_team = str(payload.get("home_team", "")).strip()
    away_team = str(payload.get("away_team", "")).strip()
    mode = str(payload.get("mode", "global")).strip().lower()
    if mode not in ("global", "mls", "extra"):
        mode = "global"
    try:
        result = _predict(home_team, away_team, mode=mode)
    except Exception:
        return jsonify({"ok": False, "error": "Prediction failed"}), 400
    return jsonify({"ok": True, "prediction": result})


@predictions_bp.post("/api/predict/mls")
def api_predict_mls():
    """Predict a single MLS matchup from user input."""
    payload = request.get_json(silent=True) or request.form
    home_team = str(payload.get("home_team", "")).strip()
    away_team = str(payload.get("away_team", "")).strip()
    try:
        result = _predict(home_team, away_team, mode="mls")
    except Exception:
        return jsonify({"ok": False, "error": "Prediction failed"}), 400
    return jsonify({"ok": True, "prediction": result})


@predictions_bp.post("/api/predict/extra")
def api_predict_extra():
    """Predict a single extra-league matchup from user input."""
    payload = request.get_json(silent=True) or request.form
    home_team = str(payload.get("home_team", "")).strip()
    away_team = str(payload.get("away_team", "")).strip()
    try:
        result = _predict(home_team, away_team, mode="extra")
    except Exception:
        return jsonify({"ok": False, "error": "Prediction failed"}), 400
    return jsonify({"ok": True, "prediction": result})


@predictions_bp.get("/api/mobile/widget")
def api_mobile_widget():
    """Lightweight widget feed: upcoming games filtered by league/team/random."""
    leagues_param = request.args.get("league", "").strip()
    team_param = request.args.get("team", "").strip()
    try:
        limit = min(max(1, int(request.args.get("limit", "10"))), 50)
    except (ValueError, TypeError):
        limit = 10
    mode = request.args.get("mode", "").strip().lower()

    filter_leagues = [l.strip() for l in leagues_param.split(",") if l.strip()] if leagues_param else []

    rows = []
    seen = set()
    for csv_path in config.UPCOMING_CSV_FILES.values():
        if not os.path.exists(csv_path):
            continue
        try:
            frame = pd.read_csv(csv_path, dtype=str)
        except Exception:
            continue
        for _, row in frame.iterrows():
            comp = str(row.get("competition", "") or "").strip()
            home = str(row.get("home_team", "") or "").strip()
            away = str(row.get("away_team", "") or "").strip()
            dedup_key = f"{comp}|{home}|{away}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            if filter_leagues and comp not in filter_leagues:
                continue
            if team_param and team_param.lower() not in (home.lower(), away.lower()):
                continue
            rows.append({
                "competition": comp,
                "match_date": str(row.get("match_date", "") or "").strip(),
                "match_datetime_utc": _utc_to_et(str(row.get("match_datetime_utc", "") or "").strip()),
                "home_team": home,
                "away_team": away,
                "predicted_result": str(row.get("predicted_result", "") or "").strip(),
                "prob_home": _to_float(row.get("prob_home")),
                "prob_draw": _to_float(row.get("prob_draw")),
                "prob_away": _to_float(row.get("prob_away")),
            })

    if mode == "random":
        _random.shuffle(rows)

    return jsonify({
        "ok": True,
        "count": min(len(rows), limit),
        "total": len(rows),
        "rows": rows[:limit],
        "generated_at_utc": datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


@predictions_bp.get("/api/scorers")
def api_scorers():
    """Return current season top scorers by competition."""
    if not os.path.exists(config.TOP_SCORERS_FILE):
        return jsonify({"ok": False, "error": "Scorers data not available", "competitions": {}}), 404

    try:
        with open(config.TOP_SCORERS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return jsonify({"ok": False, "error": "Could not load scorers", "competitions": {}}), 500

    competitions = data.get("competitions", {})
    last_updated = data.get("last_updated_utc", "Unknown")

    return jsonify({
        "ok": True,
        "last_updated_utc": last_updated,
        "competitions": competitions,
        "available_leagues": sorted(competitions.keys()),
    })
