"""
League tables, projected standings, cup brackets, and leader API routes.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
import pandas as pd

import config
from cache import _cached_response
from math_utils import _safe_float
from knockout import (
    _append_projected_cup_matches,
    _build_cup_knockout_payload,
    _build_knockout_framework,
    _compute_odds_bracket,
    _enrich_tournament_payload,
    _gather_competition_cup_matches,
    _normalize_round_label,
)
from standings import (
    _UEFA_COMPETITIONS,
    _build_fallback_standings,
    _clear_leaders_cache,
    _clear_standings_cache,
    _compute_standings_from_history,
    _load_live_score_history,
    _sanitize_real_standings,
)
from predictions import (
    _build_mls_winners_odds_bundle,
    _build_winner_probability_payload,
    _file_mtime_utc,
    _load_all_fixtures_by_competition,
    _load_json_payload,
    _load_projected_competition_table,
    _load_projected_tables,
    _load_upcoming_rows,
    get_last_pipeline_run,
)
from cup_data import build_cup_data_payload, cup_data_competitions
from league_data import build_league_data_payload
from live_poller import _live_scores, _live_scores_lock
from routes.helpers import (
    build_extra_api_payload,
    build_global_api_payload,
    build_mls_api_payload,
    is_league_api_competition,
    pick_league_winner_row,
)

leagues_bp = Blueprint("leagues", __name__)
leagues_router = leagues_bp


def _attach_projected_winner_fields(comp: str, result: dict) -> dict:
    """Add World Cup-style winner probability fields when not already present."""
    if not isinstance(result, dict) or result.get("winner_probabilities"):
        return result
    winner_payload = _build_winner_probability_payload(_load_projected_competition_table(comp))
    for key in ("winner_probabilities", "champion", "simulations_run"):
        if winner_payload.get(key) is not None:
            result[key] = winner_payload[key]
    return result


def _enrich_league_data_mls_fields(comp: str, payload: dict) -> dict:
    """Attach MLS Cup bracket, all MLS winner-odds views, and fixtures."""
    if not str(comp or "").startswith("United States/MLS"):
        return payload

    mls_winners = _build_mls_winners_odds_bundle()
    if mls_winners:
        payload["mls_winners_odds"] = mls_winners

    from competition_rules import resolve_competition_query

    base_comp, view = resolve_competition_query(comp)
    view_key_map = {
        "shield": "supporters_shield",
        "east": "eastern_conference",
        "west": "western_conference",
    }
    if comp == config.MLS_CUP_COMPETITION:
        view_key = "mls_cup"
    elif view:
        view_key = view_key_map.get(view)
    else:
        view_key = None

    if view_key and view_key in mls_winners:
        view_payload = mls_winners[view_key]
        for key in ("winner_probabilities", "winners_odds", "champion", "simulations_run"):
            if view_payload.get(key) is not None:
                payload[key] = view_payload[key]

    bracket = _load_json_payload(config.MLS_PROJECTED_BRACKET_FILE)
    if isinstance(bracket, dict) and bracket:
        payload["bracket"] = bracket
        cup_data = bracket.get("mls_cup") or {}
        if cup_data.get("winner"):
            payload["mls_cup_winner"] = cup_data.get("winner")

    if not payload.get("fixtures"):
        from competition_rules import resolve_competition_query

        base_comp, _view = resolve_competition_query(comp)
        fixture_comp = base_comp if base_comp == "United States/MLS" else comp
        for csv_path in (config.MLS_UPCOMING_FILE, config.GLOBAL_UPCOMING_FILE):
            try:
                rows, _, _ = _load_upcoming_rows(csv_path, date_range="all")
            except Exception:
                continue
            comp_fixtures = [
                r for r in rows
                if r.get("competition") in (comp, fixture_comp, "United States/MLS")
            ]
            if comp_fixtures:
                payload["fixtures"] = comp_fixtures
                break
    return payload


@leagues_bp.get("/api/world-cup")
def api_world_cup():
    """Return the World Cup projection data."""
    world_cup_file = config.WORLD_CUP_PROJECTION_FILE
    if not os.path.exists(world_cup_file):
        return jsonify({
            "ok": True,
            "available": False,
            "error": "World Cup projection not available",
            "group_tables": [],
            "simulations": {"winner_probabilities": {}},
        })
    try:
        with open(world_cup_file, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("ok", True)
        return jsonify(data)
    except Exception:
        return jsonify({
            "ok": True,
            "available": False,
            "error": "Could not load World Cup projection",
            "group_tables": [],
            "simulations": {"winner_probabilities": {}},
        })


@leagues_bp.get("/api/tournament/<key>")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_tournament(key):
    """Return tournament projection data in World Cup format for the mobile app."""
    comp_name = config.TOURNAMENT_KEY_MAP.get(str(key or "").strip().lower())
    if not comp_name:
        return jsonify({"ok": False, "error": f"Unknown tournament: {key}"}), 404
    if comp_name == "International/World Cup":
        return api_world_cup()

    # Call api_competition_data directly with competition parameter
    data = _build_competition_data_payload(comp_name)
    if not isinstance(data, dict) or data.get("ok") is False:
        return jsonify(data), (400 if not isinstance(data, dict) else 200)
    return jsonify(_enrich_tournament_payload(comp_name, data))


@leagues_bp.get("/api/cup-bracket")
@_cached_response(ttl=config.CACHE_TTL_LONG)
def api_cup_bracket():
    """Return real bracket for a cup competition in World Cup format."""
    comp = request.args.get("competition", "").strip()
    if not comp:
        return jsonify({"ok": False, "error": "Missing 'competition' parameter"}), 400
    if comp not in config.LIVE_SCORE_COMPETITIONS:
        return jsonify({"ok": False, "error": f"Unknown competition: {comp}"}), 400

    matches = []
    history = _load_live_score_history()
    seen_ids = set()
    for g in history:
        if g.get("competition") == comp:
            mid = g.get("match_id", "")
            if mid:
                seen_ids.add(mid)
            matches.append(g)

    with _live_scores_lock:
        current = _live_scores.get(comp, {}).get("games", [])
    for g in current:
        mid = g.get("match_id", "")
        if mid not in seen_ids:
            if mid:
                seen_ids.add(mid)
            matches.append(g)

    bracket_data = _load_json_payload(config.CUP_PROJECTED_BRACKET_FILE)
    _append_projected_cup_matches(matches, comp, bracket_data)

    odds_index = {}
    try:
        odds_df = pd.read_csv(config.CUP_UPCOMING_FILE)
        if not odds_df.empty and all(c in odds_df.columns for c in ("home_team", "away_team", "prob_home", "prob_draw", "prob_away")):
            for _, row in odds_df.iterrows():
                key = (str(row["home_team"]).strip().lower(), str(row["away_team"]).strip().lower())
                odds_index[key] = {
                    "prob_home": _safe_float(row["prob_home"], None),
                    "prob_draw": _safe_float(row["prob_draw"], None),
                    "prob_away": _safe_float(row["prob_away"], None),
                }
    except Exception:
        pass

    for g in matches:
        rnd = _normalize_round_label(g.get("round"))
        order = g.get("round_order", 0)
        if not isinstance(order, (int, float)):
            try:
                order = int(order)
            except (ValueError, TypeError):
                order = 0
        g["round_order"] = order
        g["round"] = rnd

        if g.get("status") == "post":
            hs = g.get("home_score")
            aws = g.get("away_score")
            if hs is not None and aws is not None:
                if hs > aws:
                    g["winner"] = g.get("home_team", "")
                elif aws > hs:
                    g["winner"] = g.get("away_team", "")

        hm_name = str(g.get("home_team", "")).strip().lower()
        aw_name = str(g.get("away_team", "")).strip().lower()
        odds = odds_index.get((hm_name, aw_name)) or odds_index.get((aw_name, hm_name), {})
        g["prob_home"] = odds.get("prob_home")
        g["prob_draw"] = odds.get("prob_draw")
        g["prob_away"] = odds.get("prob_away")

    knockout, odds_knockout, real_knockout = _build_cup_knockout_payload(matches, comp)

    result = {
        "ok": True,
        "competition": comp,
        "knockout": knockout,
        "odds_knockout": odds_knockout,
        "real_knockout": real_knockout,
    }
    ko_framework = _build_knockout_framework(comp)
    if ko_framework:
        result["knockout_rounds"] = ko_framework
    if comp in _UEFA_COMPETITIONS:
        league_table = _compute_standings_from_history(comp)
        if league_table:
            result["league_phase"] = league_table
    cup_format = config._CUP_FORMATS.get(comp)
    if cup_format:
        result["cup_format"] = cup_format

    return jsonify(result)


@leagues_bp.get("/api/real-cup-data")
def api_real_cup_data():
    """Return real cup data in World Cup format."""
    comp = request.args.get("competition", "").strip()
    if not comp:
        return jsonify({"ok": False, "error": "Missing 'competition' parameter"}), 400
    if comp not in config.LIVE_SCORE_COMPETITIONS:
        return jsonify({"ok": False, "error": f"Unknown competition: {comp}"}), 400

    cup_format = config._CUP_FORMATS.get(comp)
    if comp in _UEFA_COMPETITIONS and cup_format is None:
        for key, fmt in config._CUP_FORMATS.items():
            if key in _UEFA_COMPETITIONS and config.LIVE_SCORE_COMPETITIONS.get(comp) == config.LIVE_SCORE_COMPETITIONS.get(key):
                cup_format = fmt
                break

    table = _compute_standings_from_history(comp)
    matches = _gather_competition_cup_matches(comp)
    knockout, odds_knockout, real_knockout = _build_cup_knockout_payload(matches, comp)

    result = {
        "ok": True,
        "competition": comp,
        "cup_format": cup_format,
        "knockout": knockout,
        "odds_knockout": odds_knockout,
        "real_knockout": real_knockout,
    }
    ko_framework = _build_knockout_framework(comp)
    if ko_framework:
        result["knockout_rounds"] = ko_framework
    if table is not None:
        result["table"] = table

    return jsonify(result)


@leagues_bp.get("/api/real-tables")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_real_tables():
    """Return real league tables computed from live-score history."""
    comp_filter = request.args.get("competition", "").strip()
    force_refresh = request.args.get("refresh", "").strip().lower() in ("1", "true")

    from competition_rules import should_use_persisted_table

    if comp_filter:
        if comp_filter in config.LEAGUE_API_EXCLUDED_COMPETITIONS:
            return jsonify({
                "ok": False,
                "error": f"Competition not available in league APIs: {comp_filter}",
            }), 404
        if comp_filter not in config.LIVE_SCORE_COMPETITIONS and comp_filter not in config.MLS_TABLE_VIEW_ALIASES:
            return jsonify({"ok": False, "error": f"Unknown competition: {comp_filter}"}), 400
        if force_refresh:
            _clear_standings_cache(comp_filter)
            _clear_leaders_cache(comp_filter)
        persisted = _load_json_payload(config.REAL_TABLES_PERSIST_FILE)
        if isinstance(persisted, dict) and comp_filter in persisted:
            cached = persisted[comp_filter]
            if should_use_persisted_table(cached, force_refresh):
                cleaned = _sanitize_real_standings(cached, comp_filter) or cached
                return jsonify({"ok": True, "table": cleaned})
        table = _compute_standings_from_history(comp_filter)
        if table is not None:
            return jsonify({"ok": True, "table": table})
        fallback = _build_fallback_standings(comp_filter)
        return jsonify({"ok": True, "table": fallback})

    results = {}
    now_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    persisted = _load_json_payload(config.REAL_TABLES_PERSIST_FILE)
    if isinstance(persisted, dict):
        for comp_name in config.LIVE_SCORE_COMPETITIONS:
            if comp_name in config.LEAGUE_API_EXCLUDED_COMPETITIONS:
                continue
            cached = persisted.get(comp_name)
            if should_use_persisted_table(cached, force_refresh):
                results[comp_name] = _sanitize_real_standings(cached, comp_name) or cached
                continue
            if force_refresh:
                _clear_standings_cache(comp_name)
                _clear_leaders_cache(comp_name)
            table = _compute_standings_from_history(comp_name)
            if table is not None:
                results[comp_name] = table
            else:
                fallback = _build_fallback_standings(comp_name)
                if fallback is not None:
                    results[comp_name] = fallback
                else:
                    results[comp_name] = {
                        "competition": comp_name,
                        "updated_at": now_utc,
                        "groups": [{"name": "Overall", "entries": []}],
                        "source": "placeholder",
                    }
    else:
        for comp_name in config.LIVE_SCORE_COMPETITIONS:
            if comp_name in config.LEAGUE_API_EXCLUDED_COMPETITIONS:
                continue
            if force_refresh:
                _clear_standings_cache(comp_name)
                _clear_leaders_cache(comp_name)
            table = _compute_standings_from_history(comp_name)
            if table is not None:
                results[comp_name] = table
            else:
                fallback = _build_fallback_standings(comp_name)
                if fallback is not None:
                    results[comp_name] = fallback
                else:
                    results[comp_name] = {
                        "competition": comp_name,
                        "updated_at": now_utc,
                        "groups": [{"name": "Overall", "entries": []}],
                        "source": "placeholder",
                    }
    for alias in config.MLS_TABLE_VIEW_ALIASES:
        if alias in results:
            continue
        if force_refresh:
            _clear_standings_cache(alias)
        table = _compute_standings_from_history(alias)
        if table:
            results[alias] = table
    return jsonify({"ok": True, "tables": results, "total": len(results)})


def _build_competition_data_payload(comp: str) -> dict:
    """Build full competition data payload in World Cup format."""
    if comp == "International/World Cup":
        world_cup_file = config.WORLD_CUP_PROJECTION_FILE
        if os.path.exists(world_cup_file):
            try:
                with open(world_cup_file, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "ok": True,
            "available": False,
            "error": "World Cup projection not available",
            "group_tables": [],
            "simulations": {"winner_probabilities": {}},
        }

    table = _compute_standings_from_history(comp)
    cup_format = config._CUP_FORMATS.get(comp)
    ko_framework = _build_knockout_framework(comp)

    result = {
        "ok": True,
        "competition": comp,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    if table is not None:
        result["table"] = table
    if cup_format:
        result["cup_format"] = cup_format
    if ko_framework:
        result["knockout_rounds"] = ko_framework

    bracket_data = _load_json_payload(config.CUP_PROJECTED_BRACKET_FILE)
    if isinstance(bracket_data, dict):
        comps = bracket_data.get("competitions", bracket_data)
        if isinstance(comps, dict) and comp in comps:
            entry = comps[comp]
            if isinstance(entry, dict):
                for key in ("champion", "simulations_run", "winner_probabilities", "sim_index"):
                    if key in entry:
                        result[key] = entry[key]
                if "generated_at_utc" in bracket_data:
                    result["generated_at_utc"] = bracket_data["generated_at_utc"]

    matches = _gather_competition_cup_matches(comp)
    if not matches:
        if comp in config._CUP_FORMATS:
            result = _enrich_tournament_payload(comp, result)
        result = _attach_projected_winner_fields(comp, result)
        return result

    knockout, odds_knockout, real_knockout = _build_cup_knockout_payload(matches, comp)
    result["knockout"] = knockout
    result["odds_knockout"] = odds_knockout
    result["real_knockout"] = real_knockout

    if comp in config._CUP_FORMATS:
        result = _enrich_tournament_payload(comp, result)

    result = _attach_projected_winner_fields(comp, result)
    return result


@leagues_bp.get("/api/competition-data")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_competition_data():
    """Return full competition data in World Cup format for any competition."""
    comp = request.args.get("competition", "").strip()
    if not comp:
        return jsonify({"ok": False, "error": "Missing 'competition' parameter"}), 400
    if comp not in config.LIVE_SCORE_COMPETITIONS:
        return jsonify({"ok": False, "error": f"Unknown competition: {comp}"}), 400
    return jsonify(_build_competition_data_payload(comp))


@leagues_bp.get("/api/league-tables")
@_cached_response(ttl=config.CACHE_TTL_LONG)
def api_league_tables():
    """Return projected league tables (and MLS playoff bracket when requested)."""
    mode = str(request.args.get("mode", "global")).strip().lower()

    if mode == "mls":
        data = build_mls_api_payload()
        return jsonify({"ok": True, **data})
    if mode == "cups":
        csv_path = config.CUP_PROJECTED_TABLE_FILE
        data = _load_projected_tables(csv_path)
        if not data.get("leagues"):
            data["leagues"] = list(config._CUP_FORMATS)
        brackets = _load_json_payload(config.CUP_PROJECTED_BRACKET_FILE)
        data["last_prediction_refresh"] = _file_mtime_utc(csv_path)
        known_cups = set(data.get("leagues") or [])
        for comp_name in config._CUP_FORMATS:
            if comp_name not in known_cups:
                known_cups.add(comp_name)
        known_cups.discard("International/World Cup")
        data["leagues"] = sorted(known_cups)
        if isinstance(data.get("tables"), dict):
            data["tables"].pop("International/World Cup", None)
        cup_formats = {}
        if isinstance(brackets, dict):
            comps = brackets.get("competitions", brackets)
            if isinstance(comps, dict):
                for comp_name in comps:
                    fmt = config._CUP_FORMATS.get(comp_name)
                    if fmt:
                        cup_formats[comp_name] = fmt
        for comp_name in data.get("leagues") or []:
            if comp_name not in cup_formats:
                fmt = config._CUP_FORMATS.get(comp_name)
                if fmt:
                    cup_formats[comp_name] = fmt
        odds_bracket = _compute_odds_bracket()
        knockout_frameworks = {}
        for comp_name in list(cup_formats.keys()) + (data.get("leagues") or []):
            kf = _build_knockout_framework(comp_name)
            if kf:
                knockout_frameworks[comp_name] = kf
        fixtures = _load_all_fixtures_by_competition(config.CUP_UPCOMING_FILE)
        return jsonify({
            "ok": True, **data,
            "cup_brackets": brackets, "cup_formats": cup_formats,
            "odds_bracket": odds_bracket,
            "knockout_frameworks": knockout_frameworks,
            "fixtures": fixtures,
        })
    if mode == "extra":
        data = build_extra_api_payload()
        data["last_prediction_refresh"] = (
            _file_mtime_utc(config.EXTRA_PROJECTED_TABLE_FILE)
            if os.path.exists(config.EXTRA_PROJECTED_TABLE_FILE)
            else None
        )
        fixtures = _load_all_fixtures_by_competition(config.EXTRA_UPCOMING_FILE)
        return jsonify({"ok": True, **data, "fixtures": fixtures})

    data = build_global_api_payload()
    data["last_prediction_refresh"] = (
        _file_mtime_utc(config.GLOBAL_PROJECTED_TABLE_FILE)
        if os.path.exists(config.GLOBAL_PROJECTED_TABLE_FILE)
        else None
    )
    fixtures = _load_all_fixtures_by_competition(config.GLOBAL_UPCOMING_FILE)
    excluded = config.LEAGUE_API_EXCLUDED_COMPETITIONS
    if isinstance(fixtures, dict):
        fixtures = {k: v for k, v in fixtures.items() if k not in excluded}
    return jsonify({"ok": True, **data, "fixtures": fixtures})


@leagues_bp.get("/api/league-data/<path:competition>")
@_cached_response(ttl=config.CACHE_TTL_LONG)
def api_league_data(competition):
    """Return consolidated data for a single league / cup / competition."""
    comp = competition.strip()

    if comp in config.LEAGUE_API_EXCLUDED_COMPETITIONS:
        return jsonify({
            "ok": False,
            "error": f"Competition not available in league APIs: {comp}",
        }), 404

    from league_data import _load_league_data_from_cache
    cached = _load_league_data_from_cache(comp)
    if cached is not None:
        return jsonify(cached)

    return jsonify(build_league_data_payload(comp))


@leagues_bp.get("/api/cup-data")
@_cached_response(ttl=config.CACHE_TTL_LONG)
def api_cup_data_index():
    """List cup competitions available via ``/api/cup-data/<competition>``."""
    from competition_rules import cup_format_style_for, cup_position_stages_for

    cups = []
    for comp in cup_data_competitions():
        cups.append({
            "competition": comp,
            "format_style": cup_format_style_for(comp),
            "position_stages": cup_position_stages_for(comp),
            "path": f"/api/cup-data/{comp}",
        })
    return jsonify({"ok": True, "cups": cups, "count": len(cups)})


@leagues_bp.get("/api/cup-data/<path:competition>")
@_cached_response(ttl=config.CACHE_TTL_LONG)
def api_cup_data(competition):
    """Return consolidated cup data (league-data twin for cup competitions)."""
    comp = competition.strip()
    from competition_rules import is_cup_competition

    if not is_cup_competition(comp):
        return jsonify({
            "ok": False,
            "error": f"Not a cup competition (use /api/league-data): {comp}",
            "hint": "/api/cup-data",
        }), 404

    from cup_data import _load_cup_data_from_cache
    cached = _load_cup_data_from_cache(comp)
    if cached is not None:
        return jsonify(cached)

    return jsonify(build_cup_data_payload(comp))


@leagues_bp.get("/api/stats")
def api_stats():
    """Return overall site stats: accuracy, league count, last refresh time."""
    try:
        rows, stats, league_stats = _load_upcoming_rows(config.GLOBAL_UPCOMING_FILE, "global")
        accuracy_pct = (stats or {}).get("accuracy_pct", 0.0)
    except Exception:
        accuracy_pct = 0.0
    refreshed = get_last_pipeline_run()
    refreshed_at = refreshed.isoformat() if refreshed else None
    return jsonify({
        "ok": True,
        "accuracy_pct": accuracy_pct,
        "league_count": 18,
        "refreshed_at": refreshed_at,
    })


@leagues_bp.get("/api/league-leaders")
@_cached_response(ttl=config.CACHE_TTL_LONG)
def api_league_leaders():
    """Return predicted and real (live) leaders for every league and cup."""
    leagues = []
    cups = []

    _COMPETITION_ALIASES = {
        "Europe/Champions League", "Europe/Europa League", "Europe/Conference League",
    }

    table_sources = [
        ("global", config.GLOBAL_PROJECTED_TABLE_FILE),
        ("mls", config.MLS_PROJECTED_TABLE_FILE),
        ("extra", config.EXTRA_PROJECTED_TABLE_FILE),
        ("cups", config.CUP_PROJECTED_TABLE_FILE),
    ]

    mls_supporters = {}
    mls_east = {}
    mls_west = {}

    seen_league_comps: set[str] = set()
    seen_cup_comps: set[str] = set()

    for source_mode, csv_path in table_sources:
        proj = _load_projected_tables(csv_path)
        comp_list = proj.get("leagues") or []
        for comp_name in comp_list:
            if comp_name in config.LEAGUE_API_EXCLUDED_COMPETITIONS:
                continue
            if config.is_national_team_competition(comp_name):
                continue
            if comp_name.startswith("United States/MLS"):
                comp_tbl = proj.get("tables", {}).get(comp_name, [])
                winner_row = pick_league_winner_row(comp_tbl)
                if winner_row:
                    if "Supporters Shield" in comp_name:
                        mls_supporters = winner_row
                    elif "Eastern Conference" in comp_name:
                        mls_east = winner_row
                    elif "Western Conference" in comp_name:
                        mls_west = winner_row
                continue
            if comp_name in _COMPETITION_ALIASES:
                continue
            comp_tbl = proj.get("tables", {}).get(comp_name, [])
            winner_row = pick_league_winner_row(comp_tbl)
            predicted = None
            if winner_row:
                predicted = {
                    "winner": winner_row.get("team", ""),
                    "odds": winner_row.get("win_league_pct"),
                }
            is_cup_comp = comp_name in config._CUP_FORMATS or comp_name == "International/World Cup"
            if not predicted:
                if source_mode == "cups":
                    if not is_cup_comp:
                        continue
                else:
                    continue
            entry = {
                "competition": comp_name,
                "predicted_winner": predicted["winner"] if predicted else "—",
                "predicted_winner_odds": round(predicted["odds"], 1) if predicted and predicted["odds"] is not None else None,
            }
            if source_mode == "cups" or is_cup_comp:
                if comp_name in seen_cup_comps:
                    continue
                seen_cup_comps.add(comp_name)
                cups.append(entry)
            else:
                if comp_name in seen_league_comps:
                    continue
                seen_league_comps.add(comp_name)
                leagues.append(entry)

    mls_bracket = _load_json_payload(config.MLS_PROJECTED_BRACKET_FILE)
    mls_cup_winner = None
    if isinstance(mls_bracket, dict):
        cup_data = mls_bracket.get("mls_cup") or {}
        mls_cup_winner = cup_data.get("winner")
    mls_entry = {
        "competition": "United States/MLS",
        "predicted_winner": (mls_supporters.get("team") if mls_supporters else None) or "—",
        "predicted_winner_odds": round(mls_supporters.get("win_league_pct"), 1) if mls_supporters and mls_supporters.get("win_league_pct") is not None else None,
        "east_leader": (mls_east.get("team") if mls_east else None) or None,
        "west_leader": (mls_west.get("team") if mls_west else None) or None,
        "mls_cup_winner": mls_cup_winner or None,
    }
    leagues.append(mls_entry)

    bracket_data = _load_json_payload(config.CUP_PROJECTED_BRACKET_FILE)
    if isinstance(bracket_data, dict):
        comps = bracket_data.get("competitions", bracket_data)
        if isinstance(comps, dict):
            for comp_name, comp_entry in comps.items():
                if not isinstance(comp_entry, dict):
                    continue
                if comp_name in _COMPETITION_ALIASES:
                    continue
                if config.is_national_team_competition(comp_name):
                    continue
                champion = comp_entry.get("champion")
                winner_probs = comp_entry.get("winner_probabilities") or {}
                prob = winner_probs.get(champion, None) if champion else None
                if not any(c["competition"] == comp_name for c in cups):
                    cups.append({
                        "competition": comp_name,
                        "predicted_winner": champion or "—",
                        "predicted_winner_odds": round(prob * 100, 1) if prob is not None else None,
                    })
                else:
                    if prob is not None:
                        for c in cups:
                            if c["competition"] == comp_name and champion:
                                c["predicted_winner"] = champion
                                c["predicted_winner_odds"] = round(prob * 100, 1)

    cup_set = set(c["competition"] for c in cups)
    league_names = set(e["competition"] for e in leagues)

    for comp_name in config.LIVE_SCORE_COMPETITIONS:
        if comp_name in cup_set or comp_name in league_names:
            continue
        if comp_name in _COMPETITION_ALIASES:
            continue
        if comp_name in config.LEAGUE_API_EXCLUDED_COMPETITIONS:
            continue
        if config.is_national_team_competition(comp_name):
            continue
        if comp_name in config._CUP_FORMATS:
            cups.append({
                "competition": comp_name,
                "predicted_winner": "—",
                "predicted_winner_odds": None,
            })
        else:
            leagues.append({
                "competition": comp_name,
                "predicted_winner": "—",
                "predicted_winner_odds": None,
            })

    cup_set = set(c["competition"] for c in cups)
    for entry in leagues:
        comp = entry["competition"]
        if comp in cup_set:
            continue
        if comp == "United States/MLS":
            real = _compute_standings_from_history("United States/MLS")
            if real and isinstance(real, dict):
                for g in (real.get("groups") or []):
                    name = str(g.get("name", "")).strip()
                    leader = (g.get("entries") or [{}])[0].get("team", "")
                    if not leader:
                        continue
                    if name == "Eastern Conference":
                        entry["east_leader"] = leader
                    elif name == "Western Conference":
                        entry["west_leader"] = leader
                    elif name == "Supporters Shield":
                        entry["current_leader"] = leader
                        entry["leader_source"] = "real"
            if "leader_source" not in entry:
                entry["current_leader"] = entry.get("predicted_winner") if entry.get("predicted_winner") and entry["predicted_winner"] != "—" else None
                entry["leader_source"] = "predicted"
            continue
        real = _compute_standings_from_history(comp)
        if real and isinstance(real, dict):
            for g in (real.get("groups") or []):
                if g.get("entries"):
                    entry["current_leader"] = g["entries"][0].get("team", "")
                    entry["leader_source"] = "real"
                    break
            else:
                if entry.get("predicted_winner") and entry["predicted_winner"] != "—":
                    entry["current_leader"] = entry["predicted_winner"]
                    entry["leader_source"] = "predicted"
                else:
                    entry["current_leader"] = None
                    entry["leader_source"] = "predicted"
        else:
            if entry.get("predicted_winner") and entry["predicted_winner"] != "—":
                entry["current_leader"] = entry["predicted_winner"]
                entry["leader_source"] = "predicted"
            else:
                entry["current_leader"] = None
                entry["leader_source"] = "predicted"

    leagues = [e for e in leagues if is_league_api_competition(e.get("competition"))]

    return jsonify({
        "ok": True,
        "leagues": leagues,
        "cups": cups,
    })

