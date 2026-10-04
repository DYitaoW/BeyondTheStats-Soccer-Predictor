"""
Form, strength, recent matches, and head-to-head services.
"""

from __future__ import annotations

import csv
import os
import time
from collections import defaultdict
import pandas as pd

import config
from team_utils import _to_int

_h2h_form_cache: dict[str, tuple[float, tuple[dict, dict]]] = {}
_LAST_5_FORM_CACHE: dict = {}
_LAST_5_FORM_CACHE_TIME: float = 0.0
_STRENGTH_CACHE: dict = {}
_STRENGTH_CACHE_TIME: float = 0.0
_RECENT_MATCHES_CACHE: dict = {}
_RECENT_MATCHES_CACHE_TIME: float = 0.0


def _build_last5_form_index(mode: str) -> dict:
    """Build a cached index of last-5 matches per team for a given mode."""
    global _LAST_5_FORM_CACHE_TIME
    now = time.time()
    cache_key = mode
    cached = _LAST_5_FORM_CACHE.get(cache_key)
    if cached and (now - _LAST_5_FORM_CACHE_TIME) < 300:
        return cached

    mode_dirs = {
        "global": config.EUROPE_PROCESSED_DIR,
        "mls": config.MLS_PROCESSED_DIR,
        "extra": config.EXTRA_PROCESSED_DIR,
    }
    processed_dir = mode_dirs.get(mode)
    if not processed_dir or not os.path.exists(processed_dir):
        _LAST_5_FORM_CACHE[cache_key] = {}
        _LAST_5_FORM_CACHE_TIME = now
        return {}

    team_matches = defaultdict(list)
    for root, _, files in os.walk(processed_dir):
        csv_files = [f for f in sorted(files, reverse=True) if f.endswith(".csv")][:3]
        for name in csv_files:
            path = os.path.join(root, name)
            comp = os.path.basename(root) or "Unknown"
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    reader = csv.DictReader(f)
                    for r in reader:
                        home = str(r.get("HomeTeam", "")).strip()
                        away = str(r.get("AwayTeam", "")).strip()
                        if not home or not away:
                            continue
                        date_str = str(r.get("Date", ""))
                        result = str(r.get("FTR", ""))
                        try:
                            hg = int(float(r.get("FTHG", 0) or 0))
                            ag = int(float(r.get("FTAG", 0) or 0))
                        except (ValueError, TypeError):
                            continue
                        team_matches[home].append({
                            "opponent": away,
                            "date": date_str,
                            "result": result,
                            "home_score": hg,
                            "away_score": ag,
                            "is_home": True,
                            "venue": "home",
                            "competition": comp,
                        })
                        team_matches[away].append({
                            "opponent": home,
                            "date": date_str,
                            "result": "H" if result == "A" else ("A" if result == "H" else result),
                            "home_score": hg,
                            "away_score": ag,
                            "is_home": False,
                            "venue": "away",
                            "competition": comp,
                        })
            except Exception:
                continue

    result_index = {}
    for team, matches in team_matches.items():
        matches.sort(key=lambda x: x["date"], reverse=True)
        result_index[team] = matches[:5]

    _LAST_5_FORM_CACHE[cache_key] = result_index
    _LAST_5_FORM_CACHE_TIME = now
    return result_index


def _build_strength_cache(mode: str) -> dict:
    """Build cached attack/defence strength ratings per team for a mode."""
    global _STRENGTH_CACHE_TIME
    now = time.time()
    cache_key = f"strength_{mode}"
    cached = _STRENGTH_CACHE.get(cache_key)
    if cached and (now - _STRENGTH_CACHE_TIME) < 300:
        return cached

    import predictions
    pm_mod_map = {
        "global": predictions.pm_global,
        "mls": predictions.pm_mls,
        "extra": predictions.pm_extra,
    }
    pm_mod = pm_mod_map.get(mode)
    if not pm_mod:
        _STRENGTH_CACHE[cache_key] = {}
        _STRENGTH_CACHE_TIME = now
        return {}

    form_path = os.path.join(pm_mod.TEAM_DATA_DIR, "current_form.json")
    try:
        current_form = pm_mod.load_json_if_exists(form_path) or {}
    except Exception:
        current_form = {}

    teams_data = current_form.get("teams", {}) if isinstance(current_form, dict) else {}
    if not teams_data:
        _STRENGTH_CACHE[cache_key] = {}
        _STRENGTH_CACHE_TIME = now
        return {}

    gf_vals = [t.get("avg_goals_for_last_10", 0) or 0 for t in teams_data.values() if isinstance(t, dict)]
    ga_vals = [t.get("avg_goals_against_last_10", 0) or 0 for t in teams_data.values() if isinstance(t, dict)]
    avg_gf = (sum(gf_vals) / len(gf_vals)) if gf_vals else 1.0
    avg_ga = (sum(ga_vals) / len(ga_vals)) if ga_vals else 1.0
    if avg_gf <= 0:
        avg_gf = 1.0
    if avg_ga <= 0:
        avg_ga = 1.0

    result = {}
    for team, stats in teams_data.items():
        if not isinstance(stats, dict):
            continue
        gf = stats.get("avg_goals_for_last_10", 0) or 0
        ga = stats.get("avg_goals_against_last_10", 0) or 0
        result[team] = {
            "attack_rating": round(float(gf) / avg_gf, 4),
            "defence_rating": round(float(ga) / avg_ga, 4),
        }

    _STRENGTH_CACHE[cache_key] = result
    _STRENGTH_CACHE_TIME = now
    return result


def _build_recent_matches_cache(team: str, processed_dir: str, limit: int = 10) -> list[dict]:
    return _load_team_recent_matches(team, processed_dir, limit)


def _load_teams_from_team_data(pm_mod) -> list[str]:
    overall = pm_mod.load_json_if_exists(os.path.join(pm_mod.TEAM_DATA_DIR, "overall_teams.json")) or {}
    teams = []
    if isinstance(overall, dict):
        teams = list(overall.keys())
    if not teams:
        season_teams = pm_mod.load_json_if_exists(os.path.join(pm_mod.TEAM_DATA_DIR, "season_teams.json")) or {}
        if isinstance(season_teams, dict):
            for season_map in season_teams.values():
                if isinstance(season_map, dict):
                    teams.extend(list(season_map.keys()))
    return sorted({str(team).strip() for team in teams if str(team).strip()})


def _load_h2h_form(pm_mod, use_cache: bool = True) -> tuple[dict, dict]:
    return _load_h2h_and_form(pm_mod, use_cache=use_cache)


def _load_h2h_and_form(pm_mod, use_cache: bool = True) -> tuple[dict, dict]:
    """Load head-to-head + current-form maps for one predictor mode."""
    cache_key = pm_mod.TEAM_DATA_DIR
    h2h_path = os.path.join(pm_mod.TEAM_DATA_DIR, "head_to_head.json")
    form_path = os.path.join(pm_mod.TEAM_DATA_DIR, "current_form.json")

    def _mtime_sum():
        total = 0.0
        for path in (h2h_path, form_path):
            try:
                total += os.path.getmtime(path)
            except Exception:
                pass
        return total

    if use_cache and cache_key in _h2h_form_cache:
        mtime_sum, result = _h2h_form_cache[cache_key]
        if _mtime_sum() == mtime_sum:
            return result

    head_to_head = pm_mod.load_json_if_exists(h2h_path)
    current_form = pm_mod.load_json_if_exists(form_path)

    json_ok = (
        isinstance(head_to_head, dict)
        and isinstance(current_form, dict)
        and isinstance(current_form.get("teams"), dict)
    )
    if json_ok:
        try:
            head_to_head = pm_mod.replace_nan_with_sentinel(head_to_head)
            current_form = pm_mod.replace_nan_with_sentinel(current_form)
        except Exception:
            pass
        result = (head_to_head or {}, current_form)
        if use_cache:
            _h2h_form_cache[cache_key] = (_mtime_sum(), result)
        return result

    try:
        matches, season_files = pm_mod.load_training_matches(pm_mod.PROCESSED_DIR)
    except (ValueError, FileNotFoundError, OSError):
        result = (head_to_head or {}, current_form if isinstance(current_form, dict) else {"teams": {}})
        if use_cache:
            _h2h_form_cache[cache_key] = (_mtime_sum(), result)
        return result

    dynamic_form = pm_mod.build_dynamic_form_from_matches(matches)

    if (
        head_to_head is None
        or current_form is None
        or not isinstance(head_to_head, dict)
        or not isinstance(current_form, dict)
    ):
        _, _, head_to_head, current_form = pm_mod.build_fallback_data(matches, season_files)

    try:
        head_to_head = pm_mod.replace_nan_with_sentinel(head_to_head)
        current_form = pm_mod.replace_nan_with_sentinel(current_form)
    except Exception:
        pass

    if not isinstance(current_form, dict):
        current_form = {"teams": {}}
    if "teams" not in current_form or not isinstance(current_form["teams"], dict):
        current_form["teams"] = {}

    current_form_teams = current_form["teams"]
    for team, stats in dynamic_form.items():
        if team not in current_form_teams or not isinstance(current_form_teams.get(team), dict):
            current_form_teams[team] = stats
            continue

        for key, value in stats.items():
            if key not in current_form_teams[team] or current_form_teams[team][key] is None:
                current_form_teams[team][key] = value

    result = (head_to_head or {}, current_form)
    if use_cache:
        _h2h_form_cache[cache_key] = (_mtime_sum(), result)
    return result


def _to_float_or_none(value: object) -> float | None:
    try:
        num = float(value)
    except Exception:
        return None
    if pd.isna(num):
        return None
    return float(num)


def _load_team_recent_matches(team: str, processed_dir: str, limit: int = 10) -> list[dict]:
    """Return last N matches for a team from Processed_Data CSVs with dates and opponents."""
    global _RECENT_MATCHES_CACHE_TIME
    now = time.time()
    cache_key = f"{team}|{limit}"
    cached = _RECENT_MATCHES_CACHE.get(cache_key)
    if cached and (now - _RECENT_MATCHES_CACHE_TIME) < 300:
        return cached
    if not os.path.exists(processed_dir):
        return []
    rows = []
    for root, _, files in os.walk(processed_dir):
        for name in sorted(files):
            if not name.endswith(".csv"):
                continue
            path = os.path.join(root, name)
            try:
                df = pd.read_csv(path, usecols=lambda c: c in {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"})
            except Exception:
                continue
            if "HomeTeam" not in df.columns or "AwayTeam" not in df.columns:
                continue
            mask = (df["HomeTeam"] == team) | (df["AwayTeam"] == team)
            if not mask.any():
                continue
            sub = df[mask]
            for _, r in sub.iterrows():
                is_home = r["HomeTeam"] == team
                rows.append({
                    "date": str(r.get("Date", "")),
                    "competition": os.path.basename(os.path.dirname(path)) or "Unknown",
                    "home_team": r["HomeTeam"],
                    "away_team": r["AwayTeam"],
                    "home_score": int(r["FTHG"]) if pd.notna(r.get("FTHG")) else None,
                    "away_score": int(r["FTAG"]) if pd.notna(r.get("FTAG")) else None,
                    "result": str(r.get("FTR", "")),
                    "is_home": bool(is_home),
                    "opponent": str(r["AwayTeam"]) if is_home else str(r["HomeTeam"]),
                })
    rows.sort(key=lambda x: x["date"], reverse=True)
    result = rows[:limit]
    _RECENT_MATCHES_CACHE[cache_key] = result
    _RECENT_MATCHES_CACHE_TIME = now
    return result


def _normalize_h2h_payload(payload: dict) -> dict:
    """Normalize head-to-head payload for H2H card display."""
    out = dict(payload) if isinstance(payload, dict) else {}
    int_fields = {
        "games", "wins", "draws", "losses", "goals_scored", "goals_conceded",
        "home_games", "away_games", "home_wins", "home_draws", "home_losses",
        "away_wins", "away_draws", "away_losses",
    }
    for key in int_fields:
        if key in out:
            out[key] = _to_int(out.get(key))
    return out


def _normalize_recent_form_payload(payload: dict) -> dict:
    """Normalize recent-form payload for H2H card display."""
    src = payload if isinstance(payload, dict) else {}
    return {
        "points_last_10": _to_int(src.get("points_last_10")),
        "wins_last_10": _to_int(src.get("wins_last_10")),
        "draws_last_10": _to_int(src.get("draws_last_10")),
        "losses_last_10": _to_int(src.get("losses_last_10")),
        "avg_goals_for_last_10": _to_float_or_none(src.get("avg_goals_for_last_10")),
        "avg_goals_against_last_10": _to_float_or_none(src.get("avg_goals_against_last_10")),
        "avg_shots_for_last_10": _to_float_or_none(src.get("avg_shots_for_last_10")),
        "avg_shots_against_last_10": _to_float_or_none(src.get("avg_shots_against_last_10")),
    }
