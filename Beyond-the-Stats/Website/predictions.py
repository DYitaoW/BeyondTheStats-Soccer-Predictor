"""Pre-match predictions, model context loading, and data helpers."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import joblib
import pandas as pd

import config
from math_utils import (
    _compute_asian_handicap,
    _compute_clean_sheet,
    _compute_correct_score_dist,
    _compute_double_chance,
    _compute_first_to_score,
    _compute_total_goals_dist,
    _safe_float,
)
from team_utils import (
    TEAM_DB_TO_DISPLAY,
    _normalize_team_key,
    _team_name_for_db,
    _team_name_for_display,
    _to_float,
)

# Dynamic pipeline modules
if config.FILES_DIR not in sys.path:
    sys.path.insert(0, config.FILES_DIR)
if config.MLS_FILES_DIR not in sys.path:
    sys.path.insert(0, config.MLS_FILES_DIR)
if config.EXTRA_FILES_DIR not in sys.path:
    sys.path.insert(0, config.EXTRA_FILES_DIR)


def _load_module(module_name, file_path):
    """Dynamically import a module from a specific file path."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


pm_global = _load_module("predict_match_global", os.path.join(config.FILES_DIR, "Predict_Match.py"))
pm_mls = _load_module("predict_match_mls", os.path.join(config.MLS_FILES_DIR, "Predict_Match.py"))
pm_extra = _load_module("predict_match_extra", os.path.join(config.EXTRA_FILES_DIR, "Predict_Match.py"))
uefa = _load_module("uefa_data_manager", os.path.join(config.FILES_DIR, "UEFA_Data_Manager.py"))

_h2h_form_cache: dict[str, tuple[float, tuple[dict, dict]]] = {}
_ctx_lock = threading.Lock()
_ctx_global = None
_ctx_mls = None
_ctx_extra = None
_static_predictions_cache = {}
_static_team_cache = {}
_last_pipeline_run = None

_LAST_5_FORM_CACHE: dict = {}
_LAST_5_FORM_CACHE_TIME: float = 0.0
_STRENGTH_CACHE: dict = {}
_STRENGTH_CACHE_TIME: float = 0.0

class AveragedProbaClassifier:
    # Cache compatibility shim for previously pickled wrappers.
    def __init__(self, models):
        """Store underlying ensemble members and expose shared classes."""
        self.models = models
        self.classes_ = models[0].classes_

    def predict_proba(self, X):
        """Average probability outputs from all wrapped models."""
        matrices = [model.predict_proba(X) for model in self.models]
        return sum(matrices) / len(matrices)

    def predict(self, X):
        """Predict class labels from averaged probabilities."""
        avg = self.predict_proba(X)
        idx = avg.argmax(axis=1)
        return self.classes_[idx]

def _save_last_refresh() -> None:
    """Persist _last_pipeline_run to a file so it survives server restarts."""
    dt = _last_pipeline_run
    if dt is None:
        return
    try:
        os.makedirs(os.path.dirname(config.LAST_REFRESH_FILE), exist_ok=True)
        with open(config.LAST_REFRESH_FILE, "w") as f:
            json.dump({"last_refresh_utc": dt.isoformat()}, f)
    except Exception:
        pass


def _load_last_refresh() -> datetime | None:
    """Load the persisted last-refresh timestamp from disk."""
    if not os.path.exists(config.LAST_REFRESH_FILE):
        return None
    try:
        with open(config.LAST_REFRESH_FILE, "r") as f:
            data = json.load(f)
        raw = data.get("last_refresh_utc", "")
        if raw:
            return datetime.fromisoformat(raw)
    except Exception:
        pass
    return None


def _save_last_data_refresh() -> None:
    """Persist the last data refresh timestamp (for any pipeline run)."""
    dt = datetime.now(ZoneInfo("America/New_York"))
    try:
        os.makedirs(os.path.dirname(config.LAST_DATA_REFRESH_FILE), exist_ok=True)
        with open(config.LAST_DATA_REFRESH_FILE, "w") as f:
            json.dump({"last_data_refresh_utc": dt.isoformat()}, f)
    except Exception:
        pass


def _load_last_data_refresh() -> datetime | None:
    """Load the persisted last data refresh timestamp from disk."""
    if not os.path.exists(config.LAST_DATA_REFRESH_FILE):
        return None
    try:
        with open(config.LAST_DATA_REFRESH_FILE, "r") as f:
            data = json.load(f)
        raw = data.get("last_data_refresh_utc", "")
        if raw:
            return datetime.fromisoformat(raw)
    except Exception:
        pass
    return None


# Initialize from persisted file so the timestamp survives server restarts.
_last_pipeline_run = _load_last_refresh()


def get_last_pipeline_run():
    """Return the in-memory last pipeline run timestamp."""
    return _last_pipeline_run


def set_last_pipeline_run(dt) -> None:
    """Update the in-memory last pipeline run timestamp (used by BackendServer)."""
    global _last_pipeline_run
    _last_pipeline_run = dt

class PredictorContext:
    pm: object
    clf: object
    result_label_encoder: object
    home_goal_reg: object
    away_goal_reg: object
    home_shot_reg: object
    away_shot_reg: object
    home_sot_reg: object
    away_sot_reg: object
    train_columns: pd.Index
    overall_teams: dict
    season_teams: dict
    head_to_head: dict
    current_form: dict
    league_strength: dict
    latest_season: str
    latest_start_year: int
    team_competition_map: dict
    available_teams: list
    market_value_data: dict

def _get_goal_prob(row, col_name):
    """Return goal probability from CSV column or compute from predicted goals."""
    val = row.get(col_name)
    if val is not None:
        try:
            f = float(val)
            if not pd.isna(f):
                return round(f, 6)
        except Exception:
            pass
    try:
        hg = float(row.get("pred_home_goals", 0))
        ag = float(row.get("pred_away_goals", 0))
    except Exception:
        return None
    try:
        probs = pm_global.compute_goal_probabilities(hg, ag)
        return round(float(probs.get(col_name, 0)), 6)
    except Exception:
        return None

def _build_last5_form_index(mode):
    """Build a cached index of last-5 matches per team for a given mode.
    
    Returns dict mapping team name (str) -> list of last-5 match dicts:
        {"opponent": str, "date": str, "result": str, "home_score": int,
         "away_score": int, "is_home": bool}
    """
    global _LAST_5_FORM_CACHE_TIME
    now = time.time()
    cache_key = mode
    cached = _LAST_5_FORM_CACHE.get(cache_key)
    if cached and (now - _LAST_5_FORM_CACHE_TIME) < 300:
        return cached

    # Map mode to processed data directory
    mode_dirs = {
        "global": os.path.join(config.FILES_DIR, "Processed_Data"),
        "mls": os.path.join(config.MLS_FILES_DIR, "Processed_Data"),
        "extra": os.path.join(config.EXTRA_FILES_DIR, "Processed_Data"),
    }
    processed_dir = mode_dirs.get(mode)
    if not processed_dir or not os.path.exists(processed_dir):
        _LAST_5_FORM_CACHE[cache_key] = {}
        _LAST_5_FORM_CACHE_TIME = now
        return {}

    # Walk all CSVs and build team -> matches
    team_matches = defaultdict(list)
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
            comp = os.path.basename(os.path.dirname(path)) or "Unknown"
            for _, r in df.iterrows():
                home = str(r["HomeTeam"]).strip()
                away = str(r["AwayTeam"]).strip()
                try:
                    hg = int(r["FTHG"]) if pd.notna(r.get("FTHG")) else 0
                    ag = int(r["FTAG"]) if pd.notna(r.get("FTAG")) else 0
                except (ValueError, TypeError):
                    continue
                date_str = str(r.get("Date", ""))
                result = str(r.get("FTR", ""))
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

    # Keep only last-5 per team, sorted by date descending
    result_index = {}
    for team, matches in team_matches.items():
        matches.sort(key=lambda x: x["date"], reverse=True)
        result_index[team] = matches[:5]

    _LAST_5_FORM_CACHE[cache_key] = result_index
    _LAST_5_FORM_CACHE_TIME = now
    return result_index


def _build_strength_index(mode):
    """Build cached attack/defence strength ratings per team for a mode.
    
    Returns dict mapping team name -> {"attack_rating": float, "defence_rating": float}.
    Ratings are relative to league average (1.0 = average).
    Computed from current_form.json when available.
    """
    global _STRENGTH_CACHE_TIME
    now = time.time()
    cache_key = f"strength_{mode}"
    cached = _STRENGTH_CACHE.get(cache_key)
    if cached and (now - _STRENGTH_CACHE_TIME) < 300:
        return cached

    # Map mode to Predict_Match module and TEAM_DATA_DIR
    mode_info = {
        "global": (pm_global, "global"),
        "mls": (pm_mls, "mls"),
        "extra": (pm_extra, "extra"),
    }
    entry = mode_info.get(mode)
    if not entry:
        _STRENGTH_CACHE[cache_key] = {}
        _STRENGTH_CACHE_TIME = now
        return {}

    pm_mod, _ = entry
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

    # Compute league averages for goals for and against
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

def _load_static_predictions(path):
    if not path or not os.path.exists(path):
        return {}, set()
    try:
        df = pd.read_csv(path)
    except Exception:
        return {}, set()
    if df.empty:
        return {}, set()

    lower_cols = {str(col).strip().lower(): col for col in df.columns}
    def find_col(*names):
        for name in names:
            col = lower_cols.get(name)
            if col is not None:
                return col
        return None

    home_col = find_col("home_team", "hometeam", "home")
    away_col = find_col("away_team", "awayteam", "away")
    if home_col is None or away_col is None:
        return {}, set()

    comp_col = find_col("competition", "league")
    result_col = find_col("predicted_result", "prediction", "result")
    ph_col = find_col("prob_home", "prob_h", "home_prob")
    pd_col = find_col("prob_draw", "prob_d", "draw_prob")
    pa_col = find_col("prob_away", "prob_a", "away_prob")
    hg_col = find_col("pred_home_goals", "home_goals")
    ag_col = find_col("pred_away_goals", "away_goals")
    hs_col = find_col("pred_home_shots", "home_shots")
    as_col = find_col("pred_away_shots", "away_shots")
    hst_col = find_col("pred_home_sot", "home_sot")
    ast_col = find_col("pred_away_sot", "away_sot")

    lookup = {}
    teams = set()
    for _, row in df.iterrows():
        home_raw = row.get(home_col)
        away_raw = row.get(away_col)
        home = str(home_raw or "").strip()
        away = str(away_raw or "").strip()
        if not home or not away:
            continue
        key = (_normalize_team_key(home), _normalize_team_key(away))
        record = {
            "home_team": home,
            "away_team": away,
            "competition": str(row.get(comp_col, "")).strip() if comp_col else "",
            "predicted_result": str(row.get(result_col, "")).strip().upper() if result_col else "",
            "prob_home": _to_float(row.get(ph_col)) if ph_col else 0.0,
            "prob_draw": _to_float(row.get(pd_col)) if pd_col else 0.0,
            "prob_away": _to_float(row.get(pa_col)) if pa_col else 0.0,
            "pred_home_goals": _to_float(row.get(hg_col)) if hg_col else 0.0,
            "pred_away_goals": _to_float(row.get(ag_col)) if ag_col else 0.0,
            "pred_home_shots": _to_float(row.get(hs_col)) if hs_col else 0.0,
            "pred_away_shots": _to_float(row.get(as_col)) if as_col else 0.0,
            "pred_home_sot": _to_float(row.get(hst_col)) if hst_col else 0.0,
            "pred_away_sot": _to_float(row.get(ast_col)) if ast_col else 0.0,
        }
        hg = record.get("pred_home_goals", 0.0)
        ag = record.get("pred_away_goals", 0.0)
        try:
            goal_probs = pm_global.compute_goal_probabilities(float(hg), float(ag))
        except Exception:
            goal_probs = {}
        for gp_key in ["prob_home_goals_0", "prob_home_goals_1plus", "prob_home_goals_2plus",
                        "prob_away_goals_0", "prob_away_goals_1plus", "prob_away_goals_2plus",
                        "prob_both_score", "prob_over_1_5", "prob_over_2_5", "prob_over_3_5"]:
            record[gp_key] = goal_probs.get(gp_key, None)
        lookup[key] = record
        teams.add(home)
        teams.add(away)

    return lookup, teams


def _get_static_predictions(mode):
    if mode == "mls":
        path = config.STATIC_PREDICTIONS_MLS_FILE
    elif mode == "extra":
        path = config.STATIC_PREDICTIONS_EXTRA_FILE
    else:
        path = config.STATIC_PREDICTIONS_GLOBAL_FILE
    if not path or not os.path.exists(path):
        return {}, set()
    mtime = os.path.getmtime(path)
    if config.STATIC_PREDICTIONS_CACHE:
        cache = _static_predictions_cache.get(mode)
        if cache and cache.get("path") == path and cache.get("mtime") == mtime:
            return cache["lookup"], cache["teams"]

    lookup, teams = _load_static_predictions(path)
    if config.STATIC_PREDICTIONS_CACHE:
        _static_predictions_cache[mode] = {"path": path, "mtime": mtime, "lookup": lookup, "teams": teams}
    return lookup, teams


def _load_teams_from_team_data(pm_mod):
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


def _load_h2h_and_form(pm_mod, use_cache=True):
    cache_key = pm_mod.TEAM_DATA_DIR
    if use_cache and cache_key in _h2h_form_cache:
        mtime_sum, result = _h2h_form_cache[cache_key]
        # Check if either JSON file has changed since we cached.
        h2h_path = os.path.join(pm_mod.TEAM_DATA_DIR, "head_to_head.json")
        form_path = os.path.join(pm_mod.TEAM_DATA_DIR, "current_form.json")
        current_sum = 0.0
        for p in (h2h_path, form_path):
            try:
                current_sum += os.path.getmtime(p)
            except Exception:
                pass
        if current_sum == mtime_sum:
            return result
    head_to_head = pm_mod.load_json_if_exists(os.path.join(pm_mod.TEAM_DATA_DIR, "head_to_head.json"))
    current_form = pm_mod.load_json_if_exists(os.path.join(pm_mod.TEAM_DATA_DIR, "current_form.json"))
    try:
        matches, season_files = pm_mod.load_training_matches(pm_mod.PROCESSED_DIR)
    except (ValueError, FileNotFoundError, OSError):
        return head_to_head or {}, {"teams": {}}
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
        # Compute an mtime summary for cache invalidation.
        mtime_sum = 0.0
        for p in (os.path.join(pm_mod.TEAM_DATA_DIR, "head_to_head.json"),
                  os.path.join(pm_mod.TEAM_DATA_DIR, "current_form.json")):
            try:
                mtime_sum += os.path.getmtime(p)
            except Exception:
                pass
        _h2h_form_cache[cache_key] = (mtime_sum, result)
    return result

def _load_context(pm_mod):
    """Load cached model bundle and supporting team data for one predictor mode."""
    matches, season_files = pm_mod.load_training_matches(pm_mod.PROCESSED_DIR)

    if not os.path.exists(pm_mod.MODEL_CACHE):
        raise FileNotFoundError(
            f"Model cache not found at {pm_mod.MODEL_CACHE}. Run Predict_Match.py once first."
        )

    bundle = joblib.load(pm_mod.MODEL_CACHE)
    fingerprint = pm_mod.data_fingerprint(season_files)
    if bundle.get("fingerprint") != fingerprint:
        print(f"[predictions] Model cache fingerprint mismatch; using cached models (full retrain runs Tue/Fri)")

    overall_teams = pm_mod.load_json_if_exists(os.path.join(pm_mod.TEAM_DATA_DIR, "overall_teams.json"))
    season_teams = pm_mod.load_json_if_exists(os.path.join(pm_mod.TEAM_DATA_DIR, "season_teams.json"))
    head_to_head = pm_mod.load_json_if_exists(os.path.join(pm_mod.TEAM_DATA_DIR, "head_to_head.json"))
    current_form = pm_mod.load_json_if_exists(os.path.join(pm_mod.TEAM_DATA_DIR, "current_form.json"))
    league_strength = pm_mod.load_json_if_exists(os.path.join(pm_mod.TEAM_DATA_DIR, "league_strength.json")) or {}
    market_value_data = pm_mod.load_json_if_exists(
        os.path.join(pm_mod.TEAM_DATA_DIR, "mls_squad_values.json")
    ) or {}
    dynamic_form = pm_mod.build_dynamic_form_from_matches(matches)

    if (
        overall_teams is None
        or season_teams is None
        or head_to_head is None
        or current_form is None
        or not isinstance(overall_teams, dict)
        or len(overall_teams) == 0
    ):
        overall_teams, season_teams, head_to_head, current_form = pm_mod.build_fallback_data(matches, season_files)

    overall_teams = pm_mod.replace_nan_with_sentinel(overall_teams)
    season_teams = pm_mod.replace_nan_with_sentinel(season_teams)
    head_to_head = pm_mod.replace_nan_with_sentinel(head_to_head)
    current_form = pm_mod.replace_nan_with_sentinel(current_form)
    league_strength = pm_mod.replace_nan_with_sentinel(league_strength)

    if not isinstance(current_form, dict):
        current_form = {"teams": {}}
    if "teams" not in current_form or not isinstance(current_form["teams"], dict):
        current_form["teams"] = {}
    current_form_teams = current_form["teams"]
    for team, stats in dynamic_form.items():
        if team not in current_form_teams or not isinstance(current_form_teams.get(team), dict):
            current_form_teams[team] = stats
            continue
        existing = current_form_teams[team]
        for key, value in stats.items():
            if key not in existing or existing.get(key) in (None, "", 0, 0.0):
                existing[key] = value

    team_competition_map = {}
    for _, row in matches.iterrows():
        team_competition_map[row["HomeTeam"]] = row["competition"]
        team_competition_map[row["AwayTeam"]] = row["competition"]

    latest_season = season_files[-1].replace(".csv", "")
    csv_latest_year = max(pm_mod.parse_start_year_from_key(key) for key in season_teams.keys())
    latest_start_year = max(csv_latest_year, pm_mod.expected_current_latest_start_year())
    available_teams = sorted(set(matches["HomeTeam"].dropna()) | set(matches["AwayTeam"].dropna()))

    return PredictorContext(
        pm=pm_mod,
        clf=bundle["clf"],
        result_label_encoder=bundle["result_label_encoder"],
        home_goal_reg=bundle["home_goal_reg"],
        away_goal_reg=bundle["away_goal_reg"],
        home_shot_reg=bundle["home_shot_reg"],
        away_shot_reg=bundle["away_shot_reg"],
        home_sot_reg=bundle["home_sot_reg"],
        away_sot_reg=bundle["away_sot_reg"],
        train_columns=bundle["train_columns"],
        overall_teams=overall_teams,
        season_teams=season_teams,
        head_to_head=head_to_head,
        current_form=current_form,
        league_strength=league_strength,
        latest_season=latest_season,
        latest_start_year=latest_start_year,
        team_competition_map=team_competition_map,
        available_teams=available_teams,
        market_value_data=market_value_data,
    )


def _latest_season_for_competition(season_teams, competition, fallback, parse_start_year):
    """Return latest season key for competition or fallback when unknown."""
    competition = str(competition or "").strip()
    if not competition:
        return fallback
    best_key = None
    best_year = -1
    prefix = f"{competition}/"
    for season_key in season_teams.keys():
        if not str(season_key).startswith(prefix):
            continue
        year = parse_start_year(season_key)
        if year > best_year:
            best_year = year
            best_key = season_key
    return best_key or fallback


def _utc_to_et(utc_str):
    """Convert a UTC datetime string to ET; return empty string on failure."""
    try:
        if not utc_str:
            return ""
        dt = pd.to_datetime(str(utc_str), utc=True)
        if pd.isna(dt):
            return ""
        return dt.tz_convert(ZoneInfo("America/New_York")).strftime("%Y-%m-%dT%H:%M:%S%z")
    except Exception:
        return ""

def get_context(mode="global"):
    """Return lazily initialized prediction context for global or MLS mode."""
    global _ctx_global, _ctx_mls, _ctx_extra
    if mode == "mls":
        if _ctx_mls is None:
            with _ctx_lock:
                if _ctx_mls is None:
                    _ctx_mls = _load_context(pm_mls)
        return _ctx_mls
    if mode == "extra":
        if _ctx_extra is None:
            with _ctx_lock:
                if _ctx_extra is None:
                    _ctx_extra = _load_context(pm_extra)
        return _ctx_extra

    if _ctx_global is None:
        with _ctx_lock:
            if _ctx_global is None:
                _ctx_global = _load_context(pm_global)
    return _ctx_global


def _predict(home_raw, away_raw, mode="global"):
    """Run a single match prediction and return probabilities plus stat projections."""
    if config.STATIC_PREDICTIONS:
        lookup, _ = _get_static_predictions(mode)
        key = (_normalize_team_key(home_raw), _normalize_team_key(away_raw))
        record = lookup.get(key)
        if not record:
            raise ValueError("Prediction not available in static data.")
        prediction = record.get("predicted_result") or ""
        if prediction not in {"H", "D", "A"}:
            probs = {"H": record.get("prob_home", 0.0), "D": record.get("prob_draw", 0.0), "A": record.get("prob_away", 0.0)}
            prediction = max(probs, key=probs.get)
        home_display = _team_name_for_display(record["home_team"])
        away_display = _team_name_for_display(record["away_team"])
        return {
            "home_team": home_display,
            "away_team": away_display,
            "competition": record.get("competition") or "",
            "predicted_result": prediction,
            "winner_label": {"H": f"{home_display} win", "D": "Draw", "A": f"{away_display} win"}[prediction],
            "prob_home": round(_to_float(record.get("prob_home", 0.0)) * 100, 3),
            "prob_draw": round(_to_float(record.get("prob_draw", 0.0)) * 100, 3),
            "prob_away": round(_to_float(record.get("prob_away", 0.0)) * 100, 3),
            "pred_home_goals": int(round(_to_float(record.get("pred_home_goals", 0.0)))),
            "pred_away_goals": int(round(_to_float(record.get("pred_away_goals", 0.0)))),
            "pred_home_shots": round(_to_float(record.get("pred_home_shots", 0.0)), 2),
            "pred_away_shots": round(_to_float(record.get("pred_away_shots", 0.0)), 2),
            "pred_home_sot": round(_to_float(record.get("pred_home_sot", 0.0)), 2),
            "pred_away_sot": round(_to_float(record.get("pred_away_sot", 0.0)), 2),
            "prob_home_goals_0": _get_goal_prob(record, "prob_home_goals_0"),
            "prob_home_goals_1plus": _get_goal_prob(record, "prob_home_goals_1plus"),
            "prob_home_goals_2plus": _get_goal_prob(record, "prob_home_goals_2plus"),
            "prob_away_goals_0": _get_goal_prob(record, "prob_away_goals_0"),
            "prob_away_goals_1plus": _get_goal_prob(record, "prob_away_goals_1plus"),
            "prob_away_goals_2plus": _get_goal_prob(record, "prob_away_goals_2plus"),
            "prob_both_score": _get_goal_prob(record, "prob_both_score"),
            "prob_over_1_5": _get_goal_prob(record, "prob_over_1_5"),
            "prob_over_2_5": _get_goal_prob(record, "prob_over_2_5"),
            "prob_over_3_5": _get_goal_prob(record, "prob_over_3_5"),
            "correct_score_dist": _compute_correct_score_dist(
                record.get("pred_home_goals"), record.get("pred_away_goals"),
            ),
            "double_chance": _compute_double_chance(
                _to_float(record.get("prob_home", 0.0)),
                _to_float(record.get("prob_draw", 0.0)),
                _to_float(record.get("prob_away", 0.0)),
            ),
            "asian_handicap": _compute_asian_handicap(
                record.get("pred_home_goals"), record.get("pred_away_goals"),
                prob_home=_to_float(record.get("prob_home", 0.0)),
                prob_draw=_to_float(record.get("prob_draw", 0.0)),
                prob_away=_to_float(record.get("prob_away", 0.0)),
            ),
        }

    ctx = get_context(mode)
    pm = ctx.pm
    home_input = _team_name_for_db(home_raw)
    away_input = _team_name_for_db(away_raw)
    home_team = pm.resolve_team_name(home_input, ctx.available_teams)
    away_team = pm.resolve_team_name(away_input, ctx.available_teams)
    if not home_team or not away_team:
        raise ValueError("One or both team names were not recognized.")
    if home_team == away_team:
        raise ValueError("Home and away teams must be different.")

    home_comp = str(ctx.team_competition_map.get(home_team, "")).strip()
    away_comp = str(ctx.team_competition_map.get(away_team, "")).strip()
    competition_hint = home_comp if home_comp and home_comp == away_comp else (home_comp or away_comp)
    competition_fallback = _latest_season_for_competition(
        ctx.season_teams,
        competition_hint,
        ctx.latest_season,
        pm.parse_start_year_from_key,
    )
    prediction_season = pm.choose_season_for_teams(home_team, away_team, ctx.season_teams, competition_fallback)
    competition_key = os.path.dirname(prediction_season).replace("\\", "/") or "Unknown"
    feature_competition = competition_hint or competition_key
    prediction_start_year = pm.parse_start_year_from_key(prediction_season)
    season_coeff = pm.season_recency_coefficient(ctx.latest_start_year, prediction_start_year)
    home_comp = ctx.team_competition_map.get(home_team, feature_competition)
    away_comp = ctx.team_competition_map.get(away_team, feature_competition)

    match_input = pm.build_match_input(home_team, away_team)
    X_match = pm.build_features(
        match_input,
        prediction_season,
        feature_competition,
        season_coeff,
        ctx.overall_teams,
        ctx.season_teams,
        ctx.head_to_head,
        ctx.current_form,
        ctx.league_strength,
        home_competition_override=home_comp,
        away_competition_override=away_comp,
    )
    X_match = pd.get_dummies(X_match, columns=["competition"], dtype=float)
    X_match = X_match.reindex(columns=ctx.train_columns, fill_value=0.0)

    probabilities = {"H": 0.0, "D": 0.0, "A": 0.0}
    proba_values = ctx.clf.predict_proba(X_match)[0]
    for idx, encoded_label in enumerate(ctx.clf.classes_):
        label = ctx.result_label_encoder.inverse_transform([encoded_label])[0]
        probabilities[label] = float(proba_values[idx])
    if mode == "mls":
        home_league_strength = float(ctx.league_strength.get(home_comp, 0.85))
        away_league_strength = float(ctx.league_strength.get(away_comp, 0.85))
        probabilities, _, _ = pm.apply_league_strength_adjustment(
            probabilities, home_league_strength, away_league_strength
        )

        home_adv_shift = pm.mls_home_advantage_shift(home_team, prediction_season, ctx.season_teams)
        transfer = min(home_adv_shift, probabilities.get("A", 0.0))
        probabilities["H"] = max(0.0, probabilities.get("H", 0.0) + transfer)
        probabilities["A"] = max(0.0, probabilities.get("A", 0.0) - transfer)
        total_prob = probabilities.get("H", 0.0) + probabilities.get("D", 0.0) + probabilities.get("A", 0.0)
        if total_prob > 0:
            probabilities["H"] /= total_prob
            probabilities["D"] /= total_prob
            probabilities["A"] /= total_prob

        market_shift, _, _ = pm.market_value_probability_shift(
            home_team, away_team, ctx.market_value_data
        )
        if market_shift != 0.0:
            if market_shift > 0:
                transfer = min(market_shift, probabilities.get("A", 0.0))
                probabilities["H"] += transfer
                probabilities["A"] -= transfer
            else:
                transfer = min(abs(market_shift), probabilities.get("H", 0.0))
                probabilities["A"] += transfer
                probabilities["H"] -= transfer
            total_prob = probabilities.get("H", 0.0) + probabilities.get("D", 0.0) + probabilities.get("A", 0.0)
            if total_prob > 0:
                probabilities["H"] /= total_prob
                probabilities["D"] /= total_prob
                probabilities["A"] /= total_prob

        probabilities = pm.apply_home_advantage_boost(probabilities)
        probabilities = pm.reduce_draw_probability(probabilities)
        seed = pm.prediction_randomizer_seed(home_team, away_team, feature_competition, prediction_season)
        probabilities = pm.apply_probability_randomizer(
            probabilities,
            pm.MLS_RANDOMIZER_MAX_DELTA,
            seed=seed,
        )
    else:
        probabilities = pm.reduce_draw_probability(probabilities)
        seed = pm.prediction_randomizer_seed(home_team, away_team, feature_competition, prediction_season)
        max_delta = getattr(pm, "EU_RANDOMIZER_MAX_DELTA", None)
        if max_delta is None:
            max_delta = getattr(pm, "MLS_RANDOMIZER_MAX_DELTA", 0.12)
        probabilities = pm.apply_probability_randomizer(
            probabilities,
            max_delta,
            seed=seed,
        )

    prediction = max(probabilities, key=probabilities.get)
    home_goals = max(0.0, float(ctx.home_goal_reg.predict(X_match)[0]))
    away_goals = max(0.0, float(ctx.away_goal_reg.predict(X_match)[0]))
    home_shots = max(0.0, float(ctx.home_shot_reg.predict(X_match)[0]))
    away_shots = max(0.0, float(ctx.away_shot_reg.predict(X_match)[0]))
    home_sot = max(0.0, float(ctx.home_sot_reg.predict(X_match)[0]))
    away_sot = max(0.0, float(ctx.away_sot_reg.predict(X_match)[0]))

    home_display = _team_name_for_display(home_team)
    away_display = _team_name_for_display(away_team)

    goal_probs = pm.compute_goal_probabilities(home_goals, away_goals)
    aligned_home, aligned_away = pm.align_predicted_score(home_goals, away_goals, prediction)

    return {
        "home_team": home_display,
        "away_team": away_display,
        "competition": home_comp if home_comp == away_comp else f"{home_comp} vs {away_comp}",
        "predicted_result": prediction,
        "winner_label": {"H": f"{home_display} win", "D": "Draw", "A": f"{away_display} win"}[prediction],
        "prob_home": round(probabilities["H"] * 100, 3),
        "prob_draw": round(probabilities["D"] * 100, 3),
        "prob_away": round(probabilities["A"] * 100, 3),
        "pred_home_goals": int(round(home_goals)),
        "pred_away_goals": int(round(away_goals)),
        "pred_home_shots": round(home_shots, 2),
        "pred_away_shots": round(away_shots, 2),
        "pred_home_sot": round(home_sot, 2),
        "pred_away_sot": round(away_sot, 2),
        "prob_home_goals_0": round(goal_probs.get("prob_home_goals_0", 0), 6),
        "prob_home_goals_1plus": round(goal_probs.get("prob_home_goals_1plus", 0), 6),
        "prob_home_goals_2plus": round(goal_probs.get("prob_home_goals_2plus", 0), 6),
        "prob_away_goals_0": round(goal_probs.get("prob_away_goals_0", 0), 6),
        "prob_away_goals_1plus": round(goal_probs.get("prob_away_goals_1plus", 0), 6),
        "prob_away_goals_2plus": round(goal_probs.get("prob_away_goals_2plus", 0), 6),
        "prob_both_score": round(goal_probs.get("prob_both_score", 0), 6),
        "prob_over_1_5": round(goal_probs.get("prob_over_1_5", 0), 6),
        "prob_over_2_5": round(goal_probs.get("prob_over_2_5", 0), 6),
        "prob_over_3_5": round(goal_probs.get("prob_over_3_5", 0), 6),
        "correct_score_dist": _compute_correct_score_dist(home_goals, away_goals),
        "double_chance": _compute_double_chance(
            probabilities.get("H", 0), probabilities.get("D", 0), probabilities.get("A", 0),
        ),
        "asian_handicap": _compute_asian_handicap(
            home_goals, away_goals,
            prob_home=probabilities.get("H", 0),
            prob_draw=probabilities.get("D", 0),
            prob_away=probabilities.get("A", 0),
        ),
    }

def _winner_label(code, home_team, away_team):
    """Convert H/D/A code to a display winner label."""
    code = str(code).strip().upper()
    if code == "H":
        return f"{home_team}"
    if code == "A":
        return f"{away_team}"
    return "Draw"


def _format_percent_value(value):
    """Format percent values and clamp tiny non-zero values to '<1'."""
    try:
        v = float(value)
    except Exception:
        return "0"
    if 0.0 < v < 1.0:
        return "<1"
    return f"{v:.1f}"


def _prediction_quality_message(quality: str) -> str:
    messages = {
        "prediction": "",
        "provisional": "",
        "fallback": "Fallback league averages used for unmapped cup teams.",
        "no_prediction": "No model prediction available for this fixture.",
    }
    return messages.get(quality, "")


def _row_has_model_prediction(row, schedule_only: bool = False) -> bool:
    if schedule_only:
        return False
    predicted = str(row.get("predicted_result", "")).strip().upper()
    if predicted not in {"H", "D", "A"}:
        return False
    try:
        total_prob = (
            float(row.get("prob_home") or 0)
            + float(row.get("prob_draw") or 0)
            + float(row.get("prob_away") or 0)
        )
    except (TypeError, ValueError):
        return False
    return total_prob > 0.001


def _infer_prediction_quality(row, schedule_only: bool = False) -> tuple[str, str]:
    stored = str(row.get("prediction_quality", "")).strip().lower()
    if stored in {"prediction", "provisional", "fallback", "no_prediction"}:
        return stored, _prediction_quality_message(stored)

    if schedule_only or not _row_has_model_prediction(row, schedule_only=False):
        return "no_prediction", _prediction_quality_message("no_prediction")

    home_disp = str(row.get("display_home_team") or row.get("home_team") or "")
    away_disp = str(row.get("display_away_team") or row.get("away_team") or "")
    if "(P)" in home_disp or "(P)" in away_disp:
        return "provisional", _prediction_quality_message("provisional")

    reasoning = str(row.get("probability_reasoning") or "").lower()
    if any(token in reasoning for token in ("unknown team", "unmapped", "not in database", "schedule only")):
        return "no_prediction", _prediction_quality_message("no_prediction")

    return "prediction", ""


def _live_updates_eligible(competition: str, schedule_only: bool = False) -> bool:
    if schedule_only:
        return False
    comp = str(competition or "").strip()
    if not comp:
        return False
    aliases = config.competition_live_aliases(comp)
    for alias in aliases:
        espn_id = config.LIVE_SCORE_COMPETITIONS.get(alias)
        if espn_id:
            if alias in config.UEFA_LIVE_SCORE_COMPETITIONS and not config.uefa_live_scoring_allowed():
                return False
            return True
    return False


def _build_live_games_index() -> dict[tuple[str, str, str], dict]:
    """Index in-memory live games by normalized team pair and competition alias."""
    try:
        from live_prediction import _normalize_team_for_live
        from live_poller import _live_scores, _live_scores_lock
    except Exception:
        return {}

    index: dict[tuple[str, str, str], dict] = {}
    with _live_scores_lock:
        for comp_name, comp_data in _live_scores.items():
            for game in comp_data.get("games", []) or []:
                home = _normalize_team_for_live(game.get("home_team"))
                away = _normalize_team_for_live(game.get("away_team"))
                if not home or not away:
                    continue
                for alias in config.competition_live_aliases(comp_name):
                    index[(home, away, alias)] = game
    return index


def _find_live_game_for_row(row: dict, live_index: dict[tuple[str, str, str], dict]) -> dict | None:
    try:
        from live_prediction import _normalize_team_for_live
    except Exception:
        return None

    home = _normalize_team_for_live(row.get("home_team"))
    away = _normalize_team_for_live(row.get("away_team"))
    if not home or not away:
        return None

    comp = str(row.get("competition", "")).strip()
    for alias in config.competition_live_aliases(comp):
        game = live_index.get((home, away, alias))
        if game:
            return game
        game = live_index.get((away, home, alias))
        if game:
            return game
    return None


def _annotate_upcoming_rows_with_live(rows: list[dict]) -> list[dict]:
    live_index = _build_live_games_index()
    for row in rows:
        comp = str(row.get("competition", "")).strip()
        schedule_only = bool(row.get("schedule_only"))
        quality, note = _infer_prediction_quality(row, schedule_only=schedule_only)
        row["prediction_quality"] = quality
        row["has_prediction"] = quality in {"prediction", "provisional"}
        row["prediction_note"] = note
        if schedule_only and quality == "no_prediction":
            row["winner_label"] = "No prediction"

        eligible = _live_updates_eligible(comp, schedule_only=schedule_only)
        row["live_updates_eligible"] = eligible
        live_game = _find_live_game_for_row(row, live_index)
        if not live_game:
            row["live_updates"] = False
            row["live_status"] = None
            continue

        status = str(live_game.get("status") or "").strip().lower()
        uefa_blocked = (
            any(alias in config.UEFA_LIVE_SCORE_COMPETITIONS for alias in config.competition_live_aliases(comp))
            and not config.uefa_live_scoring_allowed()
        )
        if uefa_blocked:
            row["live_updates"] = False
            row["live_status"] = "final_only" if status == "post" else "qualifying"
            if status == "post" and row.get("actual_home_goals") is None:
                home_score = live_game.get("home_score")
                away_score = live_game.get("away_score")
                if home_score is not None and away_score is not None:
                    row["actual_home_goals"] = home_score
                    row["actual_away_goals"] = away_score
            continue

        row["live_updates"] = eligible and status in {"pre", "in"}
        row["live_status"] = status or None
        if status == "in" and live_game.get("live_prediction"):
            row["live_prediction"] = live_game.get("live_prediction")
    return rows


def _load_upcoming_rows(csv_path, mode=None, date_range="upcoming", window_days=None):
    """Load prediction rows from CSV filtered by date range.
    
    Args:
        csv_path: Path to the prediction CSV.
        mode: Source mode ("global", "mls", "extra", "cups", "national").
        date_range: ``"upcoming"`` — today onward, all future fixtures (default).
                    ``"completed"`` — previous full prediction week to yesterday.
        window_days: If set, only load rows within ``window_days`` from today
                     (e.g. ``14`` for a 2-week window).  Applied after the
                     ``date_range`` lower bound to create a tight window,
                     reducing the number of rows that need form/strength processing.
    """
    from accuracy_tracker import _compute_accuracy_stats, _compute_league_accuracy_stats

    if not os.path.exists(csv_path):
        empty = pd.DataFrame()
        target_mode = mode or "global"
        return [], _compute_accuracy_stats(empty), _compute_league_accuracy_stats(empty)
    try:
        if config.LOW_MEMORY_STATIC:
            allowed = {
                "match_date",
                "competition",
                "home_team",
                "away_team",
                "display_home_team",
                "display_away_team",
                "predicted_result",
                "prob_home",
                "prob_draw",
                "prob_away",
                "pred_home_goals",
                "pred_away_goals",
                "pred_home_shots",
                "pred_away_shots",
                "pred_home_sot",
                "pred_away_sot",
                "probability_reasoning",
                "prob_home_goals_0",
                "prob_home_goals_1plus",
                "prob_home_goals_2plus",
                "prob_away_goals_0",
                "prob_away_goals_1plus",
                "prob_away_goals_2plus",
                "prob_both_score",
                "prob_over_1_5",
                "prob_over_2_5",
                "prob_over_3_5",
                "actual_result",
                "actual_home_goals",
                "actual_away_goals",
                "schedule_only",
                "live_tracking",
                "prediction_quality",
                "match_datetime_utc",
                "match_datetime_et",
            }
            frame = pd.read_csv(
                csv_path,
                usecols=lambda c: c in allowed,
                dtype={
                    "match_date": "string",
                    "competition": "string",
                    "home_team": "string",
                    "away_team": "string",
                    "display_home_team": "string",
                    "display_away_team": "string",
                    "predicted_result": "string",
                    "probability_reasoning": "string",
                    "actual_result": "string",
                    "schedule_only": "string",
                    "live_tracking": "string",
                    "prediction_quality": "string",
                    "match_datetime_utc": "string",
                    "match_datetime_et": "string",
                },
            )
        else:
            frame = pd.read_csv(csv_path)
    except Exception:
        empty = pd.DataFrame()
        target_mode = mode or "global"
        return [], _compute_accuracy_stats(empty), _compute_league_accuracy_stats(empty)
    if frame.empty:
        target_mode = mode or "global"
        return [], _compute_accuracy_stats(frame), _compute_league_accuracy_stats(frame)

    required = ["match_date", "competition", "home_team", "away_team"]
    for col in required:
        if col not in frame.columns:
            return [], _compute_accuracy_stats(frame), _compute_league_accuracy_stats(frame)
    if "schedule_only" not in frame.columns:
        frame["schedule_only"] = "0"
    prediction_required = ["predicted_result", "prob_home", "prob_draw", "prob_away"]
    for col in prediction_required:
        if col not in frame.columns:
            frame[col] = ""

    # Drop past fixtures so stale upcoming rows never show on the website.
    # CRITICAL: Must reset index after each filter to avoid index alignment issues
    frame = frame.copy()
    frame["parsed_date"] = pd.to_datetime(frame["match_date"], errors="coerce").dt.normalize()
    frame = frame[frame["parsed_date"].notna()].reset_index(drop=True)
    
    if frame.empty:
        return [], _compute_accuracy_stats(frame), _compute_league_accuracy_stats(frame)
    
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    if date_range == "completed":
        # Previous full prediction week → today
        prev_thursday = today_et - timedelta(days=(today_et.weekday() - 3) % 7 + 7)
        lo = pd.Timestamp(prev_thursday)
        hi = pd.Timestamp(today_et + timedelta(days=1))
        frame = frame[(frame["parsed_date"] >= lo) & (frame["parsed_date"] < hi)].reset_index(drop=True)
    elif date_range == "all":
        # All available rows (no date filter)
        pass
    else:
        # Upcoming: today onward (full season — no upper date bound)
        lo = pd.Timestamp(today_et)
        frame = frame[frame["parsed_date"] >= lo].reset_index(drop=True)

    # Optional tight window: only keep rows within window_days of today
    if window_days and not frame.empty:
        hi = pd.Timestamp(today_et + timedelta(days=int(window_days)))
        frame = frame[frame["parsed_date"] <= hi].reset_index(drop=True)
    
    if frame.empty:
        return [], _compute_accuracy_stats(frame), _compute_league_accuracy_stats(frame)
    
    # Now safely convert dates for display
    frame["match_date"] = frame["parsed_date"].dt.strftime("%Y-%m-%d")
    frame = frame.drop(columns=["parsed_date"])

    frame = frame.sort_values(["match_date", "competition", "match_datetime_utc", "home_team", "away_team"])
    target_mode = mode or ("mls" if os.path.normpath(csv_path) == os.path.normpath(config.MLS_UPCOMING_FILE) else "global")
    if os.path.normpath(csv_path) == os.path.normpath(config.FRIENDLIES_UPCOMING_FILE):
        target_mode = "friendlies"
    is_mls_file = target_mode == "mls"

    # Pre-build form & strength indices (only for modes that have processed data)
    form_index = _build_last5_form_index(target_mode) if target_mode in ("global", "mls", "extra") else {}
    strength_index = _build_strength_index(target_mode) if target_mode in ("global", "mls", "extra") else {}

    rows = []
    for _, row in frame.iterrows():
        # Prefer display labels so provisional cup teams can be marked without affecting tracking keys.
        home = _team_name_for_display(str(row.get("display_home_team", row["home_team"])).strip())
        away = _team_name_for_display(str(row.get("display_away_team", row["away_team"])).strip())
        raw_date = str(row["match_date"])
        time_label = ""
        mls_dt_raw = str(row.get("match_datetime_et", "")).strip() if "match_datetime_et" in frame.columns else ""
        utc_dt_raw = str(row.get("match_datetime_utc", "")).strip() if "match_datetime_utc" in frame.columns else ""
        match_dt_et = ""

        # Convert match_datetime_utc to Eastern time for display and as match_dt_et
        if utc_dt_raw:
            date_val = pd.to_datetime(utc_dt_raw, utc=True, errors="coerce")
            if pd.notna(date_val):
                try:
                    date_val = date_val.tz_convert("America/New_York")
                    time_label = date_val.strftime("%I:%M %p ET").lstrip("0")
                    match_dt_et = date_val.strftime("%Y-%m-%dT%H:%M:%S%z")
                except Exception:
                    pass
        elif is_mls_file and mls_dt_raw:
            date_val = pd.to_datetime(mls_dt_raw, utc=True, errors="coerce")
            if pd.notna(date_val):
                try:
                    date_val = date_val.tz_convert("America/New_York")
                    time_label = date_val.strftime("%I:%M %p ET").lstrip("0")
                    match_dt_et = date_val.strftime("%Y-%m-%dT%H:%M:%S%z")
                except Exception:
                    pass
        elif is_mls_file and len(raw_date) == 10 and raw_date.count("-") == 2:
            date_val = pd.to_datetime(raw_date, errors="coerce")
        else:
            date_val = pd.to_datetime(raw_date, utc=True, errors="coerce")
            if pd.notna(date_val):
                try:
                    date_val = date_val.tz_convert("America/New_York")
                    match_dt_et = date_val.strftime("%Y-%m-%dT%H:%M:%S%z")
                except Exception:
                    pass
        if pd.isna(date_val):
            weekday = ""
            date_label = str(row["match_date"])
        else:
            weekday = date_val.strftime("%A")
            date_label = date_val.strftime("%B %d, %Y")
        try:
            ph_raw = float(row["prob_home"]) * 100
            pdv_raw = float(row["prob_draw"]) * 100
            pa_raw = float(row["prob_away"]) * 100
            ph = round(ph_raw, 3)
            pdv = round(pdv_raw, 3)
            pa = round(pa_raw, 3)
        except Exception:
            ph_raw, pdv_raw, pa_raw = 0.0, 0.0, 0.0
            ph, pdv, pa = 0.0, 0.0, 0.0
        schedule_only = str(row.get("schedule_only", "")).strip().lower() in {"1", "true", "yes"}
        quality, note = _infer_prediction_quality(row, schedule_only=schedule_only)
        has_prediction = quality in {"prediction", "provisional", "fallback"}
        actual_home_goals = pd.to_numeric(row.get("actual_home_goals"), errors="coerce")
        actual_away_goals = pd.to_numeric(row.get("actual_away_goals"), errors="coerce")
        rows.append(
            {
                "match_date": date_label if is_mls_file else str(row["match_date"]),
                "match_date_iso": str(row["match_date"]),
                "match_datetime_et": match_dt_et,
                "weekday": weekday,
                "date_label": date_label,
                "time_label": time_label,
                "competition": str(row["competition"]),
                "home_team": home,
                "away_team": away,
                "unmapped_teams": str(row.get("unmapped_teams", "")).strip(),
                "schedule_only": schedule_only,
                "prediction_quality": quality,
                "has_prediction": has_prediction,
                "prediction_note": note,
                "winner_label": (
                    _winner_label(row["predicted_result"], home, away)
                    if has_prediction
                    else ("Schedule only" if schedule_only else "No prediction")
                ),
                "prob_home": ph,
                "prob_draw": pdv,
                "prob_away": pa,
                "prob_home_text": _format_percent_value(ph_raw),
                "prob_draw_text": _format_percent_value(pdv_raw),
                "prob_away_text": _format_percent_value(pa_raw),
                "pred_home_goals": int(pd.to_numeric(row.get("pred_home_goals"), errors="coerce")) if pd.notna(pd.to_numeric(row.get("pred_home_goals"), errors="coerce")) else None,
                "pred_away_goals": int(pd.to_numeric(row.get("pred_away_goals"), errors="coerce")) if pd.notna(pd.to_numeric(row.get("pred_away_goals"), errors="coerce")) else None,
                "prob_home_goals_0": _get_goal_prob(row, "prob_home_goals_0"),
                "prob_home_goals_1plus": _get_goal_prob(row, "prob_home_goals_1plus"),
                "prob_home_goals_2plus": _get_goal_prob(row, "prob_home_goals_2plus"),
                "prob_away_goals_0": _get_goal_prob(row, "prob_away_goals_0"),
                "prob_away_goals_1plus": _get_goal_prob(row, "prob_away_goals_1plus"),
                "prob_away_goals_2plus": _get_goal_prob(row, "prob_away_goals_2plus"),
                "prob_both_score": _get_goal_prob(row, "prob_both_score"),
                "prob_over_1_5": _get_goal_prob(row, "prob_over_1_5"),
                "prob_over_2_5": _get_goal_prob(row, "prob_over_2_5"),
                "prob_over_3_5": _get_goal_prob(row, "prob_over_3_5"),
                "correct_score_dist": _compute_correct_score_dist(
                    row.get("pred_home_goals"), row.get("pred_away_goals"),
                ),
                "double_chance": _compute_double_chance(
                    ph_raw / 100.0 if ph_raw else 0,
                    pdv_raw / 100.0 if pdv_raw else 0,
                    pa_raw / 100.0 if pa_raw else 0,
                ),
                "asian_handicap": _compute_asian_handicap(
                    row.get("pred_home_goals"), row.get("pred_away_goals"),
                    prob_home=ph_raw / 100.0 if ph_raw else None,
                    prob_draw=pdv_raw / 100.0 if pdv_raw else None,
                    prob_away=pa_raw / 100.0 if pa_raw else None,
                ),
                # Last-5 form with opponent details
                "last_5_home": form_index.get(home, []),
                "last_5_away": form_index.get(away, []),
                # Attack / defence strength ratings (relative to league avg)
                "home_attack_rating": strength_index.get(home, {}).get("attack_rating"),
                "home_defence_rating": strength_index.get(home, {}).get("defence_rating"),
                "away_attack_rating": strength_index.get(away, {}).get("attack_rating"),
                "away_defence_rating": strength_index.get(away, {}).get("defence_rating"),
                # Additional Poisson-derived markets
                "total_goals_dist": _compute_total_goals_dist(
                    row.get("pred_home_goals"), row.get("pred_away_goals"),
                ),
                "first_to_score": _compute_first_to_score(
                    row.get("pred_home_goals"), row.get("pred_away_goals"),
                ),
                "clean_sheet": _compute_clean_sheet(
                    row.get("pred_home_goals"), row.get("pred_away_goals"),
                ),
                "reasoning": str(row.get("probability_reasoning", "")).strip(),
                "actual_home_goals": int(actual_home_goals) if pd.notna(actual_home_goals) else None,
                "actual_away_goals": int(actual_away_goals) if pd.notna(actual_away_goals) else None,
                "actual_result": str(row.get("actual_result", "")).strip(),
                "is_correct": (
                    "1"
                    if str(row.get("actual_result", "")).strip().upper() in {"H", "D", "A"}
                    and str(row.get("predicted_result", "")).strip().upper()
                    == str(row.get("actual_result", "")).strip().upper()
                    else (
                        "0"
                        if str(row.get("actual_result", "")).strip().upper() in {"H", "D", "A"}
                        else ""
                    )
                ),
            }
        )
    from accuracy_tracker import _build_persistent_accuracy_stats
    persistent_stats, persistent_league_stats = _build_persistent_accuracy_stats(target_mode, rows)
    rows = _annotate_upcoming_rows_with_live(rows)
    rows = [_sanitize_for_json(row) for row in rows]
    return (
        rows,
        _sanitize_for_json(persistent_stats),
        _sanitize_for_json(persistent_league_stats),
    )


def _load_all_fixtures_by_competition(csv_path):
    """Load all fixtures from a predictions CSV grouped by competition.

    Returns a dict ``{competition_name: [row_dict, …]}`` where each row
    has the same enriched format as ``_load_upcoming_rows()`` output.
    """
    rows, _st, _ls = _load_upcoming_rows(csv_path, date_range="all")
    grouped = {}
    for r in rows:
        comp = str(r.get("competition", "")).strip()
        if not comp:
            continue
        grouped.setdefault(comp, []).append(r)
    return grouped


def _load_current_season_tables():
    """Load projected tables from ``current_season_teams.json`` when no
    pipeline-generated CSV exists yet for the new season.

    Returns the same shape as ``_load_projected_tables``:
    ``{"leagues": [str, ...], "tables": {name: [row, ...]}}``
    where every team starts on 0-0-0-0 with zero probabilities.
    """
    if not os.path.exists(config.CURRENT_SEASON_TEAMS_FILE):
        return None
    try:
        with open(config.CURRENT_SEASON_TEAMS_FILE, "r", encoding="utf-8") as f:
            roster = json.load(f)
    except Exception:
        return None
    if not isinstance(roster, dict):
        return None
    leagues = []
    tables = {}
    for comp_name, teams in roster.items():
        if not teams:
            continue
        leagues.append(comp_name)
        entries = []
        for pos, team in enumerate(sorted(teams), start=1):
            entries.append({
                "position": pos, "team": team,
                "P": 0, "W": 0, "D": 0, "L": 0,
                "GF": 0, "GA": 0, "GD": 0, "Pts": 0,
                "PlayedReal": 0, "PlayedPred": 0,
                "win_league_pct": 0.0, "top4_pct": 0.0, "bottom3_pct": 0.0,
                "most_likely_position": 0, "most_likely_position_pct": 0.0,
                "position_odds": {}, "sim_runs": 0,
            })
        tables[comp_name] = entries
    return {"leagues": sorted(leagues), "tables": tables}


def _load_projected_tables(csv_path):
    """Load projected table CSV into API-ready league/table structure."""
    if not os.path.exists(csv_path):
        return {"leagues": [], "tables": {}}
    try:
        if config.LOW_MEMORY_STATIC:
            allowed = {
                "competition",
                "position",
                "team",
                "P",
                "W",
                "D",
                "L",
                "GF",
                "GA",
                "GD",
                "Pts",
                "PlayedReal",
                "PlayedPred",
                "win_league_pct",
                "top4_pct",
                "bottom3_pct",
                "most_likely_position",
                "most_likely_position_pct",
                "position_odds_json",
                "sim_runs",
                "remaining_games",
            }
            frame = pd.read_csv(
                csv_path,
                usecols=lambda c: c in allowed,
                dtype={"competition": "string", "team": "string"},
            )
        else:
            frame = pd.read_csv(csv_path)
    except Exception:
        return {"leagues": [], "tables": {}}

    required = {
        "competition",
        "position",
        "team",
        "P",
        "W",
        "D",
        "L",
        "GF",
        "GA",
        "GD",
        "Pts",
    }
    if frame.empty or not required.issubset(frame.columns):
        return {"leagues": [], "tables": {}}

    frame = frame.copy()
    frame["competition"] = frame["competition"].astype(str).str.strip()
    frame = frame[frame["competition"] != ""]
    if frame.empty:
        return {"leagues": [], "tables": {}}

    frame["position"] = pd.to_numeric(frame["position"], errors="coerce")
    frame = frame.sort_values(["competition", "position", "team"], na_position="last")

    tables = {}
    position_odds_tables = {}
    for competition, comp_frame in frame.groupby("competition", dropna=False):
        rows = []
        pos_odds_rows = []
        for _, row in comp_frame.iterrows():
            win_league_pct_raw = pd.to_numeric(row.get("win_league_pct"), errors="coerce")
            top4_pct_raw = pd.to_numeric(row.get("top4_pct"), errors="coerce")
            bottom3_pct_raw = pd.to_numeric(row.get("bottom3_pct"), errors="coerce")
            most_likely_position_raw = pd.to_numeric(row.get("most_likely_position"), errors="coerce")
            most_likely_position_pct_raw = pd.to_numeric(row.get("most_likely_position_pct"), errors="coerce")
            sim_runs_raw = pd.to_numeric(row.get("sim_runs"), errors="coerce")
            position_odds = {}
            position_odds_raw = row.get("position_odds_json")
            if pd.notna(position_odds_raw):
                try:
                    parsed_position_odds = json.loads(str(position_odds_raw))
                    if isinstance(parsed_position_odds, dict):
                        for pos_key, pct_value in parsed_position_odds.items():
                            pos_num = pd.to_numeric(pos_key, errors="coerce")
                            pct_num = pd.to_numeric(pct_value, errors="coerce")
                            if pd.notna(pos_num) and pd.notna(pct_num):
                                position_odds[int(pos_num)] = float(pct_num)
                except Exception:
                    position_odds = {}
            team_name = _team_name_for_display(str(row["team"]))
            rows.append(
                {
                    "position": int(row["position"]) if pd.notna(row["position"]) else 0,
                    "team": team_name,
                    "P": int(pd.to_numeric(row.get("P"), errors="coerce")) if pd.notna(pd.to_numeric(row.get("P"), errors="coerce")) else 0,
                    "W": int(pd.to_numeric(row.get("W"), errors="coerce")) if pd.notna(pd.to_numeric(row.get("W"), errors="coerce")) else 0,
                    "D": int(pd.to_numeric(row.get("D"), errors="coerce")) if pd.notna(pd.to_numeric(row.get("D"), errors="coerce")) else 0,
                    "L": int(pd.to_numeric(row.get("L"), errors="coerce")) if pd.notna(pd.to_numeric(row.get("L"), errors="coerce")) else 0,
                    "GF": int(pd.to_numeric(row.get("GF"), errors="coerce")) if pd.notna(pd.to_numeric(row.get("GF"), errors="coerce")) else 0,
                    "GA": int(pd.to_numeric(row.get("GA"), errors="coerce")) if pd.notna(pd.to_numeric(row.get("GA"), errors="coerce")) else 0,
                    "GD": int(pd.to_numeric(row.get("GD"), errors="coerce")) if pd.notna(pd.to_numeric(row.get("GD"), errors="coerce")) else 0,
                    "Pts": int(pd.to_numeric(row.get("Pts"), errors="coerce")) if pd.notna(pd.to_numeric(row.get("Pts"), errors="coerce")) else 0,
                    "PlayedReal": int(pd.to_numeric(row.get("PlayedReal"), errors="coerce")) if pd.notna(pd.to_numeric(row.get("PlayedReal"), errors="coerce")) else 0,
                    "PlayedPred": int(pd.to_numeric(row.get("PlayedPred"), errors="coerce")) if pd.notna(pd.to_numeric(row.get("PlayedPred"), errors="coerce")) else 0,
                    "win_league_pct": float(win_league_pct_raw) if pd.notna(win_league_pct_raw) else 0.0,
                    "top4_pct": float(top4_pct_raw) if pd.notna(top4_pct_raw) else 0.0,
                    "bottom3_pct": float(bottom3_pct_raw) if pd.notna(bottom3_pct_raw) else 0.0,
                    "most_likely_position": int(most_likely_position_raw) if pd.notna(most_likely_position_raw) else 0,
                    "most_likely_position_pct": float(most_likely_position_pct_raw) if pd.notna(most_likely_position_pct_raw) else 0.0,
                    "position_odds": position_odds,
                    "sim_runs": int(sim_runs_raw) if pd.notna(sim_runs_raw) else 0,
                }
            )
            # Build position odds row: team + odds for each position
            if position_odds:
                odds_entry: dict = {"team": team_name}
                for pos, pct in position_odds.items():
                    odds_entry[str(pos)] = pct
                pos_odds_rows.append(odds_entry)
        tables[str(competition)] = rows
        if pos_odds_rows:
            position_odds_tables[str(competition)] = pos_odds_rows

    leagues = sorted(tables.keys(), key=lambda name: name.lower())
    result = {"leagues": leagues, "tables": tables, "position_odds": position_odds_tables}
    if os.path.normpath(csv_path) == os.path.normpath(config.MLS_PROJECTED_TABLE_FILE):
        _normalize_mls_conference_tables(result)
    return result


PROJECTED_TABLE_SOURCES = (
    config.GLOBAL_PROJECTED_TABLE_FILE,
    config.MLS_PROJECTED_TABLE_FILE,
    config.EXTRA_PROJECTED_TABLE_FILE,
    config.CUP_PROJECTED_TABLE_FILE,
)

PROJECTED_WINNER_COMP_ALIASES = {
    "United States/MLS": "United States/MLS - Supporters Shield Table",
}

MLS_SHIELD_TABLE = "United States/MLS - Supporters Shield Table"
MLS_EAST_TABLE = "United States/MLS - Eastern Conference"
MLS_WEST_TABLE = "United States/MLS - Western Conference"
MLS_CONFERENCE_STAT_FIELDS = ("P", "W", "D", "L", "GF", "GA", "GD", "Pts", "PlayedReal", "PlayedPred")


def _normalize_mls_conference_tables(data: dict) -> dict:
    """Ensure MLS conference projected rows reuse Supporters Shield season stats."""
    from competition_rules import mls_conference

    tables = data.get("tables") or {}
    shield_rows = tables.get(MLS_SHIELD_TABLE)
    if not shield_rows:
        return data

    shield_by_team = {str(row.get("team", "")).strip(): row for row in shield_rows if row.get("team")}
    for conf_name, target_conf in ((MLS_EAST_TABLE, "east"), (MLS_WEST_TABLE, "west")):
        conf_rows = tables.get(conf_name)
        if not conf_rows:
            continue
        synced_rows = []
        for row in conf_rows:
            team = str(row.get("team", "")).strip()
            base = shield_by_team.get(team)
            if not base or mls_conference(team) != target_conf:
                continue
            synced = dict(row)
            for field in MLS_CONFERENCE_STAT_FIELDS:
                if field in base:
                    synced[field] = base[field]
            synced_rows.append(synced)
        synced_rows.sort(
            key=lambda item: (
                -(int(item.get("Pts") or 0)),
                -(int(item.get("GD") or 0)),
                -(int(item.get("GF") or 0)),
                str(item.get("team", "")),
            )
        )
        for pos, row in enumerate(synced_rows, start=1):
            row["position"] = pos
        tables[conf_name] = synced_rows
    data["tables"] = tables
    return data


def _build_mls_winners_odds_bundle() -> dict:
    """Return separate winner odds for Shield, East, West, and MLS Cup."""
    bundle: dict = {}
    for key, comp_name in config.MLS_WINNER_VIEWS.items():
        table = _load_projected_competition_table(comp_name)
        if table:
            payload = _build_winner_probability_payload(table)
            if payload.get("winner_probabilities"):
                bundle[key] = {
                    "competition": comp_name,
                    "winner_probabilities": payload.get("winner_probabilities", {}),
                    "winners_odds": payload.get("winners_odds", []),
                    "champion": payload.get("champion"),
                    "simulations_run": payload.get("simulations_run"),
                }

    if "mls_cup" not in bundle:
        bracket = _load_json_payload(config.MLS_PROJECTED_BRACKET_FILE)
        if isinstance(bracket, dict):
            cup_probs = bracket.get("mls_cup_winner_probabilities") or {}
            if cup_probs:
                winners_odds = [
                    {
                        "team": team,
                        "win_league_pct": round(float(pct), 2),
                        "top4_pct": None,
                        "bottom3_pct": None,
                        "most_likely_position": None,
                        "most_likely_position_pct": None,
                    }
                    for team, pct in sorted(cup_probs.items(), key=lambda x: -float(x[1] or 0))
                    if float(pct or 0) > 0
                ]
                champion = winners_odds[0]["team"] if winners_odds else (bracket.get("mls_cup") or {}).get("winner")
                bundle["mls_cup"] = {
                    "competition": config.MLS_CUP_COMPETITION,
                    "winner_probabilities": {k: round(float(v), 2) for k, v in cup_probs.items() if float(v or 0) > 0},
                    "winners_odds": winners_odds,
                    "champion": champion,
                    "simulations_run": bracket.get("simulations_run"),
                }
    return bundle


def _load_projected_competition_table(comp_name: str) -> list[dict]:
    """Return projected table rows for a competition from any pipeline CSV."""
    lookup_names = [str(comp_name or "").strip()]
    alias = PROJECTED_WINNER_COMP_ALIASES.get(lookup_names[0])
    if alias and alias not in lookup_names:
        lookup_names.append(alias)
    for lookup in lookup_names:
        if not lookup:
            continue
        for csv_path in PROJECTED_TABLE_SOURCES:
            proj = _load_projected_tables(csv_path)
            table = (proj.get("tables") or {}).get(lookup)
            if table:
                return table
    return []


def _build_winner_probability_payload(comp_table: list[dict]) -> dict:
    """Build World Cup-style winner odds fields from projected table rows."""
    winner_probabilities: dict[str, float] = {}
    winners_odds: list[dict] = []
    champion = None
    sim_runs = None
    best_pct = -1.0

    for row in comp_table:
        team = str(row.get("team", "")).strip()
        if not team:
            continue
        if sim_runs is None and row.get("sim_runs") is not None:
            sim_runs = row.get("sim_runs")
        try:
            pct_f = float(row.get("win_league_pct") or 0)
        except (TypeError, ValueError):
            pct_f = 0.0
        entry = {
            "team": team,
            "win_league_pct": round(pct_f, 2),
            "top4_pct": row.get("top4_pct"),
            "bottom3_pct": row.get("bottom3_pct"),
            "most_likely_position": row.get("most_likely_position"),
            "most_likely_position_pct": row.get("most_likely_position_pct"),
        }
        if pct_f > 0:
            winner_probabilities[team] = round(pct_f, 2)
            winners_odds.append(entry)
            if pct_f > best_pct:
                best_pct = pct_f
                champion = team

    winners_odds.sort(key=lambda x: x.get("win_league_pct") or 0, reverse=True)
    payload: dict = {"winners_odds": winners_odds}
    if winner_probabilities:
        payload["winner_probabilities"] = winner_probabilities
    if champion:
        payload["champion"] = champion
    if sim_runs is not None:
        payload["simulations_run"] = sim_runs
    return payload


def _load_json_payload(path):
    """Safely load JSON payload from disk, returning None on failure."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _file_mtime_utc(path):
    """Return file modification time as ISO-8601 UTC string, or None."""
    try:
        ts = os.path.getmtime(path)
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _to_int(value):
    """Best-effort integer coercion for display-safe counters."""
    try:
        num = float(value)
    except Exception:
        return 0
    if pd.isna(num):
        return 0
    return int(round(num))


def _normalize_h2h_payload(payload):
    """Normalize head-to-head stats to whole-number counters."""
    if not isinstance(payload, dict):
        return {}
    out = dict(payload)
    int_fields = {
        "games",
        "wins",
        "draws",
        "losses",
        "goals_scored",
        "goals_conceded",
        "home_games",
        "away_games",
        "home_wins",
        "home_draws",
        "home_losses",
        "away_wins",
        "away_draws",
        "away_losses",
    }
    for key in int_fields:
        if key in out:
            out[key] = _to_int(out.get(key))
    return out


def _to_float_or_none(value):
    """Best-effort float coercion for optional stat fields."""
    try:
        num = float(value)
    except Exception:
        return None
    if pd.isna(num):
        return None
    return float(num)


_RECENT_MATCHES_CACHE = {}
_RECENT_MATCHES_CACHE_TIME = 0


def _load_team_recent_matches(team, processed_dir, limit=10):
    """Return last N matches for a team from Processed_Data CSVs with dates and opponents."""
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


def _normalize_recent_form_payload(payload):
    """Normalize recent-form payload for H2H card display."""
    src = payload if isinstance(payload, dict) else {}
    out = {
        "points_last_10": _to_int(src.get("points_last_10")),
        "wins_last_10": _to_int(src.get("wins_last_10")),
        "draws_last_10": _to_int(src.get("draws_last_10")),
        "losses_last_10": _to_int(src.get("losses_last_10")),
        "avg_goals_for_last_10": _to_float_or_none(src.get("avg_goals_for_last_10")),
        "avg_goals_against_last_10": _to_float_or_none(src.get("avg_goals_against_last_10")),
        "avg_shots_for_last_10": _to_float_or_none(src.get("avg_shots_for_last_10")),
        "avg_shots_against_last_10": _to_float_or_none(src.get("avg_shots_against_last_10")),
    }
    return out

def run_live_results_updater():
    """Run the live-results updater script once at app startup."""
    if not os.path.exists(config.LIVE_RESULTS_UPDATER):
        print(f"[startup] Live updater not found: {config.LIVE_RESULTS_UPDATER}")
        return
    try:
        proc = subprocess.run(
            [sys.executable, config.LIVE_RESULTS_UPDATER],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if proc.stdout:
            print(proc.stdout.strip())
        if proc.returncode != 0:
            print("[startup] Live updater failed.")
            if proc.stderr:
                print(proc.stderr.strip())
    except Exception as exc:
        print(f"[startup] Live updater error: {exc}")


def _invalidate_prediction_caches(*, reload_contexts: bool = False) -> None:
    """Clear in-memory predictor state and Redis API caches after a pipeline run."""
    global _ctx_global, _ctx_mls, _ctx_extra
    with _ctx_lock:
        _ctx_global = None
        _ctx_mls = None
        _ctx_extra = None
    _static_predictions_cache.clear()
    _static_team_cache.clear()
    try:
        from cache import _cache_clear_pattern
        _cache_clear_pattern()
    except Exception:
        pass
    try:
        from standings import _clear_all_real_data_caches
        _clear_all_real_data_caches()
    except Exception:
        pass
    if reload_contexts and not config.STATIC_PREDICTIONS:
        try:
            get_context("global")
            get_context("mls")
            get_context("extra")
            print("[refresh] Model contexts reloaded successfully.")
        except Exception as exc:
            print(f"[refresh] Context reload warning: {exc}")


def _run_full_pipeline_once(*, full_retrain: bool = True):
    """Run data/model refresh pipeline and reload in-memory predictor contexts."""
    global _last_pipeline_run
    if not os.path.exists(config.RUN_ALL_PIPELINE):
        print(f"[refresh] Pipeline runner not found: {config.RUN_ALL_PIPELINE}")
        return False
    cmd = [sys.executable, config.RUN_ALL_PIPELINE]
    if not full_retrain:
        cmd.append("--skip-model-train")
    try:
        proc = subprocess.run(
            cmd,
            cwd=config.PROJECT_DIR,
            timeout=3600,
            check=False,
        )
        if proc.returncode != 0:
            print(f"[refresh] Pipeline failed with rc={proc.returncode}.")
            _last_pipeline_run = datetime.now(ZoneInfo("America/New_York"))
            _save_last_refresh()
            return False
        print(f"[refresh] Pipeline finished successfully (full_retrain={full_retrain}).")
    except subprocess.TimeoutExpired:
        print("[refresh] Pipeline timed out after 3600s.")
        return False
    except Exception as exc:
        print(f"[refresh] Pipeline error: {exc}")
        return False

    _last_pipeline_run = datetime.now(ZoneInfo("America/New_York"))
    _save_last_refresh()
    _save_last_data_refresh()
    _invalidate_prediction_caches(reload_contexts=True)
    from accuracy_tracker import update_accuracy_history_files
    update_accuracy_history_files()
    return True


# Scheduler removed — BackendServer handles pipeline scheduling.
# Previously _seconds_until_next_refresh, _daily_refresh_loop, and start_daily_refresh_scheduler were here.


def _should_run_startup_tasks(debug_mode):
    """Avoid running startup jobs twice when Flask reloader is enabled."""
    return (not debug_mode) or os.environ.get("WERKZEUG_RUN_MAIN") == "true"


def _valid_date_iso(s):
    """Return True if s matches YYYY-MM-DD (ISO 8601 date)."""
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s))


def _enrich_json_past_row(r):
    """Add display fields to a raw past_games.json row so it matches upcoming format."""
    pred = str(r.get("predicted_result", "")).strip().upper()
    home = str(r.get("home_team", "")).strip()
    away = str(r.get("away_team", "")).strip()
    r["winner_label"] = (
        f"Pred: {home}" if pred == "H" else
        f"Pred: {away}" if pred == "A" else
        "Pred: Draw" if pred == "D" else ""
    )
    actual = str(r.get("actual_result", "")).strip().upper()
    if actual in {"H", "D", "A"}:
        r["is_correct"] = "1" if pred == actual else "0"
    else:
        r["is_correct"] = ""

    try:
        ph = float(r.get("prob_home", 0) or 0) * 100
        pdv = float(r.get("prob_draw", 0) or 0) * 100
        pa = float(r.get("prob_away", 0) or 0) * 100
    except Exception:
        ph = pdv = pa = 0.0
    r["prob_home_text"] = _format_percent_value(ph)
    r["prob_draw_text"] = _format_percent_value(pdv)
    r["prob_away_text"] = _format_percent_value(pa)

    md = str(r.get("match_date", "")).strip()
    if md:
        try:
            dt = pd.to_datetime(md, errors="coerce")
            if pd.notna(dt):
                r["weekday"] = dt.strftime("%A")
                r["date_label"] = dt.strftime("%B %d, %Y")
        except Exception:
            r["weekday"] = ""
            r["date_label"] = md
    else:
        r["weekday"] = ""
        r["date_label"] = ""

    utc_dt = str(r.get("match_datetime_utc", "")).strip()
    if utc_dt:
        try:
            dt = pd.to_datetime(utc_dt, utc=True, errors="coerce")
            if pd.notna(dt):
                dt = dt.tz_convert("America/New_York")
                r["match_datetime_et"] = dt.strftime("%Y-%m-%dT%H:%M:%S%z")
                r["time_label"] = dt.strftime("%I:%M %p ET").lstrip("0")
        except Exception:
            pass
    if "time_label" not in r:
        r["time_label"] = ""

    for score_key in ("actual_home_goals", "actual_away_goals", "home_score", "away_score"):
        v = r.get(score_key)
        if v is not None:
            try:
                r[score_key] = int(float(v))
            except (ValueError, TypeError):
                pass

    if r.get("home_score") is not None and r.get("actual_home_goals") is None:
        r["actual_home_goals"] = r["home_score"]
    if r.get("away_score") is not None and r.get("actual_away_goals") is None:
        r["actual_away_goals"] = r["away_score"]
    if r.get("actual_home_goals") is not None and r.get("home_score") is None:
        r["home_score"] = r["actual_home_goals"]
    if r.get("actual_away_goals") is not None and r.get("away_score") is None:
        r["away_score"] = r["actual_away_goals"]


def _past_row_date_iso(row: dict) -> str:
    """Normalize any past-game row to an ISO date (YYYY-MM-DD) in US/Eastern."""
    for key in ("match_date_iso", "match_date", "kickoff_utc", "match_datetime_utc", "completed_at"):
        raw = str(row.get(key, "") or "").strip()
        if not raw:
            continue
        if len(raw) >= 10 and _valid_date_iso(raw[:10]):
            return raw[:10]
        try:
            parsed = pd.to_datetime(raw, errors="coerce", utc=True)
            if pd.notna(parsed):
                if parsed.tzinfo is None:
                    parsed = parsed.tz_localize("UTC")
                return parsed.tz_convert("America/New_York").date().isoformat()
        except Exception:
            continue
    return ""


def _live_game_to_past_row(game: dict, competition: str | None = None) -> dict | None:
    """Convert a completed live-score game into a past-games API row."""
    from accuracy_tracker import _compute_actual_result

    try:
        hs = int(float(game.get("home_score")))
        aws = int(float(game.get("away_score")))
    except (TypeError, ValueError):
        return None
    actual = _compute_actual_result(hs, aws)
    if not actual:
        return None

    match_date_iso = _past_row_date_iso(game)
    if not match_date_iso:
        return None

    home = str(game.get("home_team", "")).strip()
    away = str(game.get("away_team", "")).strip()
    if not home or not away:
        return None

    row = {
        "match_date": match_date_iso,
        "match_date_iso": match_date_iso,
        "competition": str(competition or game.get("competition", "")).strip(),
        "home_team": home,
        "away_team": away,
        "actual_home_goals": hs,
        "actual_away_goals": aws,
        "actual_result": actual,
        "home_score": hs,
        "away_score": aws,
        "match_datetime_utc": str(game.get("kickoff_utc", "") or game.get("match_datetime_utc", "")).strip(),
        "source": "live_score_history",
    }
    _enrich_json_past_row(row)
    return row


def _collect_live_past_game_rows(cutoff: str) -> list[dict]:
    """Return completed games from persisted and in-memory live scores."""
    from standings import _load_live_score_history

    rows: list[dict] = []
    seen: set[str] = set()

    def add_game(game: dict, competition: str | None = None) -> None:
        if str(game.get("status", "")).lower() != "post":
            return
        if str(game.get("match_id", "")).strip().lower().startswith("test-"):
            return
        row = _live_game_to_past_row(game, competition)
        if not row:
            return
        date_iso = _past_row_date_iso(row)
        if not date_iso or date_iso < cutoff:
            return
        if _is_placeholder_game(row):
            return
        ck = "|".join(
            [
                date_iso,
                str(row.get("competition", "")).strip().lower(),
                str(row.get("home_team", "")).strip().lower(),
                str(row.get("away_team", "")).strip().lower(),
            ]
        )
        if not ck or ck in seen:
            return
        seen.add(ck)
        rows.append(row)

    for game in _load_live_score_history():
        add_game(game)

    try:
        from live_poller import _live_scores, _live_scores_lock

        with _live_scores_lock:
            for comp_name, comp_data in _live_scores.items():
                for game in comp_data.get("games", []):
                    add_game(game, comp_name)
    except Exception:
        pass

    return rows


def _build_past_game_prediction_lookup() -> dict[str, dict]:
    """Index prediction CSV rows by match key for enriching live results."""
    lookup: dict[str, dict] = {}
    for source, csv_path in (
        ("global", config.GLOBAL_UPCOMING_FILE),
        ("mls", config.MLS_UPCOMING_FILE),
        ("extra", config.EXTRA_UPCOMING_FILE),
        ("cups", config.CUP_UPCOMING_FILE),
        ("national", config.NATIONAL_UPCOMING_FILE),
    ):
        pred_rows, _, _ = _load_upcoming_rows(csv_path, source, date_range="all")
        for row in pred_rows:
            date_iso = _past_row_date_iso(row)
            if not date_iso:
                continue
            ck = "|".join(
                [
                    date_iso,
                    str(row.get("competition", "")).strip().lower(),
                    str(row.get("home_team", "")).strip().lower(),
                    str(row.get("away_team", "")).strip().lower(),
                ]
            )
            if ck:
                lookup[ck] = row
    return lookup


def _merge_prediction_onto_past_row(row: dict, lookup: dict[str, dict]) -> dict:
    """Attach pre-match prediction fields when a CSV row exists for the fixture."""
    date_iso = _past_row_date_iso(row)
    if not date_iso:
        return row
    ck = "|".join(
        [
            date_iso,
            str(row.get("competition", "")).strip().lower(),
            str(row.get("home_team", "")).strip().lower(),
            str(row.get("away_team", "")).strip().lower(),
        ]
    )
    pred = lookup.get(ck)
    if not pred:
        return row
    merged = dict(row)
    for key in (
        "predicted_result",
        "prob_home",
        "prob_draw",
        "prob_away",
        "prob_home_text",
        "prob_draw_text",
        "prob_away_text",
        "pred_home_goals",
        "pred_away_goals",
        "winner_label",
        "schedule_only",
        "match_datetime_et",
        "weekday",
        "date_label",
        "time_label",
    ):
        if pred.get(key) not in (None, ""):
            merged[key] = pred[key]
    if not merged.get("match_datetime_utc") and pred.get("match_datetime_utc"):
        merged["match_datetime_utc"] = pred.get("match_datetime_utc")
    _enrich_json_past_row(merged)
    return merged


def _past_game_storage_key(row: dict) -> str:
    """Stable dedupe key for past_games.json rows."""
    date_iso = _past_row_date_iso(row)
    if not date_iso:
        return ""
    return "|".join(
        [
            date_iso,
            str(row.get("competition", "")).strip().lower(),
            str(row.get("home_team", "")).strip().lower(),
            str(row.get("away_team", "")).strip().lower(),
        ]
    )


def _sanitize_for_json(value):
    """Recursively coerce API payloads into strict JSON-safe Python values."""
    import math

    if value is None:
        return None
    if isinstance(value, dict):
        return {str(key): _sanitize_for_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_for_json(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    try:
        import numpy as np

        if isinstance(value, np.floating):
            number = float(value)
            if math.isnan(number) or math.isinf(number):
                return None
            return number
        if isinstance(value, np.integer):
            return int(value)
    except Exception:
        pass
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, str):
        return "".join(ch for ch in value if ord(ch) >= 32 or ch in "\t\n\r")
    return value


def _json_safe_row(row: dict) -> dict:
    """Make an upcoming API row JSON-serializable for past_games.json."""
    sanitized = _sanitize_for_json(row)
    return sanitized if isinstance(sanitized, dict) else {}


def archive_todays_games_to_past_games_file() -> int:
    """Upsert recent enriched completed rows into past_games.json.

    Uses the same enriched row shape as ``/api/upcoming/*`` so
    ``/api/past-games`` can serve identical payloads for the retained window.
    Intended to run at the end of the daily pipeline after predictions
    are refreshed and results are settled.
    """
    today_str = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    cutoff_str = _week_based_cutoff()

    csv_sources = [
        ("global", config.GLOBAL_UPCOMING_FILE),
        ("mls", config.MLS_UPCOMING_FILE),
        ("extra", config.EXTRA_UPCOMING_FILE),
        ("cups", config.CUP_UPCOMING_FILE),
        ("national", config.NATIONAL_UPCOMING_FILE),
    ]
    extra_sources = [
        ("global", os.path.join(config.PROJECT_DIR, "Output", "Upcoming", "all_upcoming.csv")),
        ("global", os.path.join(config.PROJECT_DIR, "Output", "Europe", "Upcoming", "europe_upcoming.csv")),
        ("global", os.path.join(config.PROJECT_DIR, "Output", "National", "Upcoming", "national_upcoming.csv")),
    ]

    all_rows: list[dict] = []
    seen: set[str] = set()
    for source, csv_path in csv_sources + extra_sources:
        if not csv_path or not os.path.exists(csv_path):
            continue
        try:
            rows, _, _ = _load_upcoming_rows(csv_path, source, date_range="completed")
        except Exception:
            continue
        for row in rows:
            date_iso = _past_row_date_iso(row)
            if not date_iso or date_iso < cutoff_str or date_iso > today_str:
                continue
            actual = str(row.get("actual_result", "")).strip().upper()
            if actual not in {"H", "D", "A"}:
                continue
            if _is_placeholder_game(row):
                continue
            ck = _past_game_storage_key(row)
            if not ck or ck in seen:
                continue
            seen.add(ck)
            stored = _json_safe_row(dict(row))
            stored["match_date_iso"] = date_iso
            all_rows.append(stored)

    existing_by_key: dict[str, dict] = {}
    if os.path.exists(config.PAST_GAMES_FILE):
        try:
            with open(config.PAST_GAMES_FILE, "r", encoding="utf-8") as fh:
                existing = json.load(fh)
            if not isinstance(existing, list):
                raise ValueError("past_games.json must contain a JSON list")
        except Exception as exc:
            # A damaged archive must never be replaced with only the current
            # run's rows. Leave it untouched for recovery.
            print(f"[past-games] Refusing to overwrite unreadable archive: {exc}")
            return 0
    else:
        existing = []

    if isinstance(existing, list):
        for row in existing:
            if isinstance(row, dict):
                ck = _past_game_storage_key(row)
                if ck:
                    existing_by_key[ck] = row

    inserted = 0
    replaced = 0
    for row in all_rows:
        ck = _past_game_storage_key(row)
        if not ck:
            continue
        if ck in existing_by_key:
            existing_by_key[ck] = row
            replaced += 1
        else:
            existing_by_key[ck] = row
            inserted += 1

    before = len(existing_by_key)
    # Keep rows whose date cannot be parsed rather than deleting potentially
    # recoverable records. Valid rows expire only before the previous full week.
    merged = [
        r for r in existing_by_key.values()
        if not _past_row_date_iso(r) or _past_row_date_iso(r) >= cutoff_str
    ]
    pruned = before - len(merged)

    os.makedirs(os.path.dirname(config.PAST_GAMES_FILE), exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(
        prefix=".past-games-", suffix=".json", dir=os.path.dirname(config.PAST_GAMES_FILE)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2, ensure_ascii=False, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary_path, config.PAST_GAMES_FILE)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)

    print(
        f"[past-games] Archived {len(all_rows)} recent row(s) "
        f"({inserted} new, {replaced} replaced), pruned {pruned} old "
        f"→ past_games.json ({len(merged)} total)"
    )
    return len(all_rows)


def _is_test_live_game(r) -> bool:
    """Return True for synthetic live-score rows that should never surface in the UI."""
    match_id = str(r.get("match_id", "")).strip().lower()
    if match_id.startswith("test-") or "test-past-games" in match_id:
        return True
    source = str(r.get("source", "")).strip().lower()
    if source == "test":
        return True
    return False


def _is_placeholder_game(r):
    """Return True if a game dict is a placeholder (not a real match)."""
    if _is_test_live_game(r):
        return True
    for key in ("home_team", "away_team"):
        val = str(r.get(key, "")).lower()
        if "group" in val or "third place" in val or "winner" in val or "runner" in val:
            return True
    return False


def _week_based_cutoff():
    """Return ISO date string for the start of the previous full week (Mon)."""
    today_local = datetime.now(ZoneInfo("America/New_York")).date()
    current_week_start = today_local - timedelta(days=today_local.weekday())
    return (current_week_start - timedelta(days=7)).isoformat()
