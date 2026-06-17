import os
import sys
import json
import threading
import importlib.util
import subprocess
import time
import urllib.request
from collections import deque, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

_last_pipeline_run: datetime | None = None

# ── Live Score Polling ──────────────────────────────────────────
LIVE_SCORE_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

LIVE_SCORE_COMPETITIONS = {
    # Club leagues
    "England/Premier League": "eng.1",
    "England/Championship": "eng.2",
    "Spain/La Liga": "esp.1",
    "Spain/La Liga 2": "esp.2",
    "Italy/Serie A": "ita.1",
    "Italy/Serie B": "ita.2",
    "Germany/Bundesliga": "ger.1",
    "Germany/Bundesliga 2": "ger.2",
    "France/Ligue 1": "fra.1",
    "France/Ligue 2": "fra.2",
    "Portugal/Liga Portugal": "por.1",
    "Netherlands/Eredivisie": "ned.1",
    "United States/MLS": "usa.1",
    # Cups
    "England/FA Cup": "eng.fa",
    "England/League Cup": "eng.efl",
    "UEFA/Champions League": "uefa.champions",
    "UEFA/Europa League": "uefa.europa",
    "UEFA/Conference League": "uefa.europa.conf",
    # National team / World Cup
    "FIFA/World Cup": "fifa.world",
    "FIFA/World Cup Qualifying - UEFA": "fifa.worldq.uefa",
    "FIFA/World Cup Qualifying - CONMEBOL": "fifa.worldq.conmebol",
    "FIFA/World Cup Qualifying - CONCACAF": "fifa.worldq.concacaf",
    "FIFA/Friendly": "fifa.friendly",
    "UEFA/European Championship": "uefa.euro",
    "UEFA/Nations League": "uefa.nations",
    "CONMEBOL/Copa America": "conmebol.america",
    "CONCACAF/Gold Cup": "concacaf.gold",
    "CAF/Africa Cup of Nations": "caf.nations",
}

_live_scores: dict[str, dict] = {}
_live_scores_lock = threading.Lock()

# ── End Live Score Polling ──────────────────────────────────────

import joblib
import pandas as pd
from flask import Flask, jsonify, render_template, request, send_from_directory


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


WEBSITE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(WEBSITE_DIR)
LAST_REFRESH_FILE = os.path.join(PROJECT_DIR, "Data", "last_refresh.json")
FILES_DIR = os.path.join(PROJECT_DIR, "files")
MLS_FILES_DIR = os.path.join(PROJECT_DIR, "MLS", "files")
EXTRA_FILES_DIR = os.path.join(PROJECT_DIR, "Extra-leagues", "files")
WEBSITE_FILES_DIR = os.path.join(WEBSITE_DIR, "files")
GRAPHICS_DIR = os.path.join(WEBSITE_DIR, "graphics")
FEEDBACK_DIR = os.path.join(WEBSITE_FILES_DIR, "feedback")
FEEDBACK_FILE = os.path.join(FEEDBACK_DIR, "feedback.txt")
ACCURACY_HISTORY_DIR = os.path.join(WEBSITE_FILES_DIR, "accuracy_history")
ACCURACY_TOTALS_FILE = os.path.join(WEBSITE_FILES_DIR, "accuracy_totals.json")
GLOBAL_UPCOMING_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "upcoming_matchweek_predictions.csv")
CUP_UPCOMING_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "upcoming_cup_predictions.csv")
CUP_COMPLETED_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "completed_cup_predictions.csv")
MLS_UPCOMING_FILE = os.path.join(PROJECT_DIR, "MLS", "Data", "Predictions", "upcoming_matchweek_predictions.csv")
EXTRA_UPCOMING_FILE = os.path.join(PROJECT_DIR, "Extra-leagues", "Data", "Predictions", "upcoming_matchweek_predictions.csv")
NATIONAL_UPCOMING_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "upcoming_national_team_predictions.csv")
GLOBAL_PROJECTED_TABLE_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "projected_league_tables.csv")
CUP_PROJECTED_TABLE_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "projected_cup_tables.csv")
CUP_PROJECTED_BRACKET_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "projected_cup_brackets.json")
MLS_PROJECTED_TABLE_FILE = os.path.join(PROJECT_DIR, "MLS", "Data", "Predictions", "projected_league_tables.csv")
EXTRA_PROJECTED_TABLE_FILE = os.path.join(PROJECT_DIR, "Extra-leagues", "Data", "Predictions", "projected_league_tables.csv")
MLS_PROJECTED_BRACKET_FILE = os.path.join(PROJECT_DIR, "MLS", "Data", "Predictions", "projected_mls_playoff_bracket.json")
LIVE_RESULTS_UPDATER = os.path.join(FILES_DIR, "Update_Live_Prediction_Results.py")
RUN_ALL_PIPELINE = os.path.join(PROJECT_DIR, "Run_All_Pipeline.py")
LAST_DATA_REFRESH_FILE = os.path.join(PROJECT_DIR, "Data", "last_data_refresh.json")
TEAM_NAME_DISPLAY_MAPPING_FILE = os.path.join(PROJECT_DIR, "Data", "Predictions", "team_name_mapping_master.json")
TOP_SCORERS_FILE = os.path.join(PROJECT_DIR, "Data", "Team_Data", "current_season_top_scorers.json")
USE_DISPLAY_NAME_MAPPING = False
MLS_COMPETITION = "United States/MLS"
CUP_COMPETITIONS = {
    "England/FA Cup",
    "England/League Cup",
    "UEFA/Champions League",
    "UEFA/Europa League",
    "UEFA/Conference League",
    "Europe/Champions League",
    "Europe/Europa League",
    "Europe/Conference League",
}
STATIC_PREDICTIONS = os.environ.get("STATIC_PREDICTIONS", "1").strip().lower() in {"1", "true", "yes"}


def _save_last_refresh() -> None:
    """Persist _last_pipeline_run to a file so it survives server restarts."""
    dt = _last_pipeline_run
    if dt is None:
        return
    try:
        os.makedirs(os.path.dirname(LAST_REFRESH_FILE), exist_ok=True)
        with open(LAST_REFRESH_FILE, "w") as f:
            json.dump({"last_refresh_utc": dt.isoformat()}, f)
    except Exception:
        pass


def _load_last_refresh() -> datetime | None:
    """Load the persisted last-refresh timestamp from disk."""
    if not os.path.exists(LAST_REFRESH_FILE):
        return None
    try:
        with open(LAST_REFRESH_FILE, "r") as f:
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
        os.makedirs(os.path.dirname(LAST_DATA_REFRESH_FILE), exist_ok=True)
        with open(LAST_DATA_REFRESH_FILE, "w") as f:
            json.dump({"last_data_refresh_utc": dt.isoformat()}, f)
    except Exception:
        pass


def _load_last_data_refresh() -> datetime | None:
    """Load the persisted last data refresh timestamp from disk."""
    if not os.path.exists(LAST_DATA_REFRESH_FILE):
        return None
    try:
        with open(LAST_DATA_REFRESH_FILE, "r") as f:
            data = json.load(f)
        raw = data.get("last_data_refresh_utc", "")
        if raw:
            return datetime.fromisoformat(raw)
    except Exception:
        pass
    return None


# Initialize from persisted file so the timestamp survives server restarts.
_last_pipeline_run = _load_last_refresh()

LOW_MEMORY_STATIC = os.environ.get("LOW_MEMORY_STATIC", "1").strip().lower() in {"1", "true", "yes"}
STATIC_PREDICTIONS_CACHE = os.environ.get("STATIC_PREDICTIONS_CACHE", "0").strip().lower() in {"1", "true", "yes"}
STATIC_PREDICTIONS_GLOBAL_FILE = os.environ.get("STATIC_PREDICTIONS_GLOBAL_FILE", GLOBAL_UPCOMING_FILE)
STATIC_PREDICTIONS_MLS_FILE = os.environ.get("STATIC_PREDICTIONS_MLS_FILE", MLS_UPCOMING_FILE)
STATIC_PREDICTIONS_EXTRA_FILE = os.environ.get("STATIC_PREDICTIONS_EXTRA_FILE", EXTRA_UPCOMING_FILE)
REFRESH_API_TOKEN = os.environ.get("REFRESH_API_TOKEN", "").strip()
NOTIFICATIONS_API_KEY = os.environ.get("NOTIFICATIONS_API_KEY", "").strip()
_notifications = deque(maxlen=100)
_device_tokens = set()
if FILES_DIR not in sys.path:
    sys.path.insert(0, FILES_DIR)
if MLS_FILES_DIR not in sys.path:
    sys.path.insert(0, MLS_FILES_DIR)
if EXTRA_FILES_DIR not in sys.path:
    sys.path.insert(0, EXTRA_FILES_DIR)


def _load_module(module_name, file_path):
    """Dynamically import a module from a specific file path."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


pm_global = _load_module("predict_match_global", os.path.join(FILES_DIR, "Predict_Match.py"))
pm_mls = _load_module("predict_match_mls", os.path.join(MLS_FILES_DIR, "Predict_Match.py"))
pm_extra = _load_module("predict_match_extra", os.path.join(EXTRA_FILES_DIR, "Predict_Match.py"))


app = Flask(__name__, template_folder="templates", static_folder="static")
API_RATE_LIMIT_PER_MINUTE = int(os.environ.get("API_RATE_LIMIT_PER_MINUTE", "120"))
_api_rate_lock = threading.Lock()
_api_rate_events_by_ip = {}


# Cache-Control policy:
# - /api/* JSON: 5 minutes shared cache + 5 minutes private. Pipeline runs
#   daily so the data is stale for at most 24 h; browsers should revalidate
#   on every navigation through the site, but a returning visitor who hits
#   "back" within a few minutes gets an instant response.
# - /static/*: 1 hour shared cache. JS / CSS change only on deploy, but
#   browser cache + hard refresh (Ctrl-F5) covers the upgrade path without
#   us needing query-string versioning.
# - /graphics/* and other routes: short browser cache only.
_API_CACHE_MAX_AGE = int(os.environ.get("API_CACHE_MAX_AGE", "300"))
_STATIC_CACHE_MAX_AGE = int(os.environ.get("STATIC_CACHE_MAX_AGE", "86400"))


@app.after_request
def _add_cache_headers(response):
    """Attach a sensible Cache-Control header to every served response.

    The website re-fetches the same JSON + static assets on every page
    load because no headers were previously set. This handler adds modest
    browser + shared cache lifetimes so repeat visits are instant.
    """
    if request.path.startswith("/api/"):
        # JSON endpoints: short max-age + must-revalidate so the browser
        # revalidates on the next page load but can serve stale-while-
        # revalidate if the user navigates quickly back to the page.
        response.headers["Cache-Control"] = (
            f"private, max-age={_API_CACHE_MAX_AGE}, must-revalidate"
        )
    elif request.path.startswith("/static/"):
        # Static JS / CSS / images. Filenames are stable between deploys;
        # version-bumping the URL is the cache-bust strategy. Override the
        # Flask default ("no-cache") so browsers actually cache them.
        response.headers["Cache-Control"] = (
            f"public, max-age={_STATIC_CACHE_MAX_AGE}"
        )
    elif request.path.startswith("/graphics/"):
        response.headers["Cache-Control"] = (
            f"public, max-age={int(_STATIC_CACHE_MAX_AGE * 24)}"
        )

    # CORS for the Cloudflare Pages frontend (and any future static origin).
    # Allow-list is read from ALLOWED_ORIGINS env var (comma-separated).
    origin = request.headers.get("Origin")
    if origin:
        allowed = {
            o.strip()
            for o in os.environ.get("ALLOWED_ORIGINS", "").split(",")
            if o.strip()
        }
        # Default allow-list when no env var is set (local dev convenience).
        if not allowed:
            allowed = {"http://localhost:5000", "http://127.0.0.1:5000"}
        if origin in allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Refresh-Token, X-Notifications-Key"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"

    return response


@app.before_request
def _handle_cors_preflight():
    """Respond to CORS preflight (OPTIONS) requests immediately."""
    if request.method == "OPTIONS":
        # Build a minimal preflight response; after_request adds the
        # Access-Control-* headers based on the Origin.
        return ("", 204)


@dataclass
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


_ctx_lock = threading.Lock()
_feedback_lock = threading.Lock()
_ctx_global = None
_ctx_mls = None
_ctx_extra = None
_static_predictions_cache = {}
_static_team_cache = {}


def _client_ip():
    """Return best-effort client IP, respecting trusted proxy forwarding headers."""
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        first_ip = forwarded_for.split(",")[0].strip()
        if first_ip:
            return first_ip
    return request.remote_addr or "unknown"


def _refresh_auth_ok():
    """Return True if refresh endpoint is authorized."""
    if not REFRESH_API_TOKEN:
        return True
    token = request.headers.get("X-Refresh-Token", "").strip()
    if not token:
        auth_header = request.headers.get("Authorization", "").strip()
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
    return token == REFRESH_API_TOKEN


@app.before_request
def _enforce_api_rate_limit():
    """Apply a per-IP rolling one-minute cap for all API routes."""
    if not request.path.startswith("/api/"):
        return None

    now = time.time()
    cutoff = now - 60.0
    ip = _client_ip()
    limit = max(1, API_RATE_LIMIT_PER_MINUTE)
    retry_after = 60

    with _api_rate_lock:
        events = _api_rate_events_by_ip.setdefault(ip, deque())
        while events and events[0] <= cutoff:
            events.popleft()

        if len(events) >= limit:
            retry_after = int(max(1, 60 - (now - events[0])))
            print(
                f"[rate-limit] {ip} hit {limit} req/min cap on "
                f"{request.path} (retry_after={retry_after}s)"
            )
            return jsonify(
                {
                    "ok": False,
                    "error": "Rate limit exceeded. Try again later.",
                    "retry_after_seconds": retry_after,
                    "limit_per_minute": limit,
                }
            ), 429

        events.append(now)

        # Best-effort memory cleanup for IPs that have no recent events.
        stale_ips = [key for key, queue in _api_rate_events_by_ip.items() if not queue or queue[-1] <= cutoff]
        for key in stale_ips:
            _api_rate_events_by_ip.pop(key, None)

    return None


def _load_team_display_mappings():
    """Load flattened team-name display mappings from mapping master JSON."""
    if not os.path.exists(TEAM_NAME_DISPLAY_MAPPING_FILE):
        return {}, {}
    try:
        with open(TEAM_NAME_DISPLAY_MAPPING_FILE, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return {}, {}
    if not isinstance(payload, dict):
        return {}, {}

    db_to_display = {}
    display_to_db = {}
    for _, comp_map in payload.items():
        if not isinstance(comp_map, dict):
            continue
        for raw_name, mapped_name in comp_map.items():
            db_name = str(raw_name or "").strip()
            display_name = str(mapped_name or "").strip()
            if not db_name or not display_name:
                continue
            db_to_display.setdefault(db_name, display_name)
            display_to_db.setdefault(display_name, db_name)
    return db_to_display, display_to_db


TEAM_DB_TO_DISPLAY, TEAM_DISPLAY_TO_DB = _load_team_display_mappings()


def _team_name_for_display(name):
    """Map DB/canonical team names to UI display names."""
    text = str(name or "").strip()
    if not text:
        return ""
    if not USE_DISPLAY_NAME_MAPPING:
        return text
    return TEAM_DB_TO_DISPLAY.get(text, text)


def _team_name_for_db(name):
    """Map UI display names back to DB/canonical team names."""
    text = str(name or "").strip()
    if not text:
        return ""
    if not USE_DISPLAY_NAME_MAPPING:
        return text
    return TEAM_DISPLAY_TO_DB.get(text, text)


def _normalize_team_key(name):
    return str(name or "").strip().lower()


def _to_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


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
        lookup[key] = record
        teams.add(home)
        teams.add(away)

    return lookup, teams


def _get_static_predictions(mode):
    if mode == "mls":
        path = STATIC_PREDICTIONS_MLS_FILE
    elif mode == "extra":
        path = STATIC_PREDICTIONS_EXTRA_FILE
    else:
        path = STATIC_PREDICTIONS_GLOBAL_FILE
    if not path or not os.path.exists(path):
        return {}, set()
    mtime = os.path.getmtime(path)
    if STATIC_PREDICTIONS_CACHE:
        cache = _static_predictions_cache.get(mode)
        if cache and cache.get("path") == path and cache.get("mtime") == mtime:
            return cache["lookup"], cache["teams"]

    lookup, teams = _load_static_predictions(path)
    if STATIC_PREDICTIONS_CACHE:
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


def _load_h2h_and_form(pm_mod):
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

    return head_to_head or {}, current_form

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
        raise RuntimeError(
            "Model cache is stale for current processed data. Rebuild by running Predict_Match.py."
        )

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
    latest_start_year = max(pm_mod.parse_start_year_from_key(key) for key in season_teams.keys())
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


# ── Live Score Poller ───────────────────────────────────────────

LIVE_SCORE_FETCH_TIMEOUT = 15


def _parse_espn_live_event(event):
    """Parse a single ESPN event dict into a minimal live-score payload."""
    try:
        comp = event.get("competitions") or [{}]
        comp_data = comp[0] if comp else {}
        competitors = comp_data.get("competitors") or []
        if len(competitors) < 2:
            return None
        home = competitors[0] if competitors[0].get("homeAway") == "home" else competitors[1]
        away = competitors[1] if competitors[0].get("homeAway") == "home" else competitors[0]
        status = comp_data.get("status") or {}
        type_detail = status.get("type") or {}
        state = type_detail.get("state", "pre")
        detail = type_detail.get("detail", "")
        clock = comp_data.get("clock") or ""
        display_clock = f"{clock} {detail}" if clock else detail
        return {
            "match_id": str(event.get("id", "")),
            "home_team": str(home.get("team", {}).get("displayName", "")),
            "away_team": str(away.get("team", {}).get("displayName", "")),
            "home_score": _to_int(home.get("score")),
            "away_score": _to_int(away.get("score")),
            "status": state,
            "period": detail,
            "clock": display_clock.strip(),
            "kickoff_utc": event.get("date", ""),
        }
    except Exception:
        return None


def _fetch_competition_scores(comp_name, espn_id, today_str):
    """Fetch ESPN scoreboard for one competition/date, return parsed games."""
    url = f"{LIVE_SCORE_ESPN_BASE}/{espn_id}/scoreboard?dates={today_str}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=LIVE_SCORE_FETCH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return []
    events = data.get("events") or []
    games = []
    for ev in events:
        parsed = _parse_espn_live_event(ev)
        if parsed:
            parsed["competition"] = comp_name
            games.append(parsed)
    return games


BREAK_PERIODS = {"Halftime", "HT", "Half Time"}


def _get_todays_competitions():
    """Return {competition: [kickoff_et, ...]} for competitions with games today.

    Reads the upcoming predictions CSVs saved by the daily pipeline to find
    which competitions have games on the current date and at what times.
    """
    today_iso = date.today().isoformat()
    now_et = datetime.now(ZoneInfo("America/New_York"))
    todays = defaultdict(list)

    for csv_path in _UPCOMING_CSV_FILES.values():
        if not os.path.exists(csv_path):
            continue
        try:
            frame = pd.read_csv(csv_path, dtype=str)
        except Exception:
            continue
        for _, row in frame.iterrows():
            comp = str(row.get("competition", "") or "").strip()
            if comp not in LIVE_SCORE_COMPETITIONS:
                continue
            match_date = str(row.get("match_date", "") or "").strip()
            if match_date == today_iso:
                kickoff_utc_str = str(row.get("match_datetime_utc", "") or "").strip()
                if kickoff_utc_str:
                    try:
                        dt_utc = datetime.fromisoformat(kickoff_utc_str.replace("Z", "+00:00"))
                        kickoff_et = dt_utc.astimezone(ZoneInfo("America/New_York"))
                    except Exception:
                        kickoff_et = now_et
                else:
                    kickoff_et = now_et
                todays[comp].append(kickoff_et)
    return {k: sorted(v) for k, v in todays.items()}


def _live_score_poller_loop():
    """Background thread: poll ESPN for live scores every 90 seconds.

    Only polls competitions that have games today per the daily pipeline's
    predictions CSVs.  A competition is polled when at least one of its
    games has a kickoff within the next 5 minutes.  Once polled, it stays
    active as long as at least one game is live (status="in" and not on a
    named break like Halftime).  When all games finish or go on break,
    polling stops until the next game's 5-minute pre-window.
    """
    while True:
        try:
            today_str = date.today().strftime("%Y%m%d")
            now_et = datetime.now(ZoneInfo("America/New_York"))

            todays_comps = _get_todays_competitions()

            active_comps = {}
            if todays_comps:
                # Use CSV data to only poll competitions whose games are imminent or live.
                for comp, kickoffs in todays_comps.items():
                    espn_id = LIVE_SCORE_COMPETITIONS.get(comp)
                    if not espn_id:
                        continue
                    for k in kickoffs:
                        window_start = k - timedelta(minutes=5)
                        window_end = k + timedelta(hours=3, minutes=30)
                        if window_start <= now_et <= window_end:
                            active_comps[comp] = espn_id
                            break
            else:
                # No CSV data for today (stale predictions / pipeline not run yet).
                # Fall back to polling all known competitions so live scores
                # still work even without an up-to-date predictions file.
                active_comps = dict(LIVE_SCORE_COMPETITIONS)

            results = {}
            if active_comps:
                with ThreadPoolExecutor(max_workers=min(8, len(active_comps))) as pool:
                    ft_to_name = {
                        pool.submit(_fetch_competition_scores, name, eid, today_str): name
                        for name, eid in active_comps.items()
                    }
                    for ft in as_completed(ft_to_name):
                        name = ft_to_name[ft]
                        try:
                            games = ft.result()
                            if not games:
                                continue
                            # Only keep this competition if at least one game is
                            # actively live and NOT on a named break (Halftime/HT).
                            has_live = any(
                                g.get("status") == "in"
                                and g.get("period", "") not in BREAK_PERIODS
                                for g in games
                            )
                            if has_live:
                                results[name] = {
                                    "competition": name,
                                    "games": games,
                                    "last_polled_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
                                }
                        except Exception:
                            pass

            with _live_scores_lock:
                _live_scores.clear()
                _live_scores.update(results)
        except Exception:
            pass
        time.sleep(90)


# ── End Live Score Poller ───────────────────────────────────────


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
    if STATIC_PREDICTIONS:
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


def _compute_accuracy_stats(frame):
    """Compute aggregate accuracy counters from a predictions dataframe."""
    if frame.empty:
        return {
            "total_predictions": 0,
            "settled_total": 0,
            "correct_total": 0,
            "pending_total": 0,
            "accuracy_pct": 0.0,
        }

    if "actual_result" in frame.columns:
        settled_mask = frame["actual_result"].astype(str).str.strip().isin({"H", "D", "A"})
    else:
        settled_mask = pd.Series([False] * len(frame), index=frame.index)
    settled = frame[settled_mask].copy()
    if settled.empty:
        return {
            "total_predictions": int(len(frame)),
            "settled_total": 0,
            "correct_total": 0,
            "pending_total": int(len(frame)),
            "accuracy_pct": 0.0,
        }

    correct = (
        settled["predicted_result"].astype(str).str.strip().str.upper()
        == settled["actual_result"].astype(str).str.strip().str.upper()
    ).sum()

    settled_total = int(len(settled))
    correct_total = int(correct)
    accuracy = round((100.0 * correct_total / settled_total), 1) if settled_total else 0.0
    return {
        "total_predictions": int(len(frame)),
        "settled_total": settled_total,
        "correct_total": correct_total,
        "pending_total": int(len(frame) - settled_total),
        "accuracy_pct": accuracy,
    }


def _compute_league_accuracy_stats(frame):
    """Compute accuracy counters grouped by competition."""
    if frame.empty or "competition" not in frame.columns:
        return []

    rows = []
    grouped = frame.groupby("competition", dropna=False)
    for competition, comp_frame in grouped:
        stats = _compute_accuracy_stats(comp_frame)
        rows.append(
            {
                "competition": str(competition),
                "correct_total": stats["correct_total"],
                "settled_total": stats["settled_total"],
                "pending_total": stats["pending_total"],
                "total_predictions": stats["total_predictions"],
                "accuracy_pct": stats["accuracy_pct"],
            }
        )
    rows.sort(key=lambda item: item["competition"].lower())
    return rows


def _load_accuracy_totals():
    """Load persistent all-time accuracy totals written by the live updater."""
    payload = _load_json_payload(ACCURACY_TOTALS_FILE)
    if not isinstance(payload, dict):
        return {"overall": {}, "by_league": {}}
    overall = payload.get("overall")
    by_league = payload.get("by_league")
    if not isinstance(overall, dict):
        overall = {}
    if not isinstance(by_league, dict):
        by_league = {}
    return {"overall": overall, "by_league": by_league}


def _build_persistent_accuracy_stats(mode, rows):
    """Build response stats by combining persistent settled totals with current pending rows."""
    totals = _load_accuracy_totals()
    by_league_all = totals.get("by_league", {})
    if mode == "mls":
        filtered = {
            str(k): v for k, v in by_league_all.items()
            if str(k).strip() == MLS_COMPETITION
        }
    elif mode == "extra":
        filtered = {}
    elif mode == "national":
        filtered = {}
    elif mode == "cups":
        filtered = {
            str(k): v for k, v in by_league_all.items()
            if str(k).strip() in CUP_COMPETITIONS
        }
    else:
        filtered = {
            str(k): v for k, v in by_league_all.items()
            if str(k).strip() != MLS_COMPETITION and str(k).strip() not in CUP_COMPETITIONS
        }

    pending_by_league = {}
    for row in rows:
        comp = str(row.get("competition", "")).strip() or "Unknown"
        pending_by_league[comp] = pending_by_league.get(comp, 0) + 1

    league_stats = []
    comps = sorted(set(filtered.keys()) | set(pending_by_league.keys()), key=lambda name: name.lower())
    correct_sum = 0
    settled_sum = 0
    for comp in comps:
        league_payload = filtered.get(comp, {}) if isinstance(filtered.get(comp), dict) else {}
        correct_total = int(league_payload.get("correct_total", 0) or 0)
        settled_total = int(league_payload.get("total_predictions", 0) or 0)
        pending_total = int(pending_by_league.get(comp, 0))
        accuracy_pct = round((100.0 * correct_total / settled_total), 1) if settled_total else 0.0
        league_stats.append(
            {
                "competition": comp,
                "correct_total": correct_total,
                "settled_total": settled_total,
                "pending_total": pending_total,
                "total_predictions": settled_total,
                "accuracy_pct": accuracy_pct,
            }
        )
        correct_sum += correct_total
        settled_sum += settled_total

    stats = {
        "total_predictions": settled_sum,
        "settled_total": settled_sum,
        "correct_total": correct_sum,
        "pending_total": int(len(rows)),
        "accuracy_pct": round((100.0 * correct_sum / settled_sum), 1) if settled_sum else 0.0,
    }
    return stats, league_stats


def _load_upcoming_rows(csv_path, mode=None):
    """Load upcoming prediction rows and attach persistent accuracy stats."""
    if not os.path.exists(csv_path):
        empty = pd.DataFrame()
        target_mode = mode or "global"
        return [], _compute_accuracy_stats(empty), _compute_league_accuracy_stats(empty)
    try:
        if LOW_MEMORY_STATIC:
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
                "actual_result",
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

    required = ["match_date", "competition", "home_team", "away_team", "predicted_result", "prob_home", "prob_draw", "prob_away"]
    for col in required:
        if col not in frame.columns:
            return [], _compute_accuracy_stats(frame), _compute_league_accuracy_stats(frame)

    # Drop past fixtures so stale upcoming rows never show on the website.
    # CRITICAL: Must reset index after each filter to avoid index alignment issues
    frame = frame.copy()
    frame["parsed_date"] = pd.to_datetime(frame["match_date"], errors="coerce").dt.normalize()
    frame = frame[frame["parsed_date"].notna()].reset_index(drop=True)
    
    if frame.empty:
        return [], _compute_accuracy_stats(frame), _compute_league_accuracy_stats(frame)
    
    today = pd.Timestamp(datetime.now().date())
    frame = frame[frame["parsed_date"] >= today].reset_index(drop=True)
    
    if frame.empty:
        return [], _compute_accuracy_stats(frame), _compute_league_accuracy_stats(frame)
    
    # Now safely convert dates for display
    frame["match_date"] = frame["parsed_date"].dt.strftime("%Y-%m-%d")
    frame = frame.drop(columns=["parsed_date"])

    frame = frame.sort_values(["match_date", "competition", "home_team", "away_team"])
    target_mode = mode or ("mls" if os.path.normpath(csv_path) == os.path.normpath(MLS_UPCOMING_FILE) else "global")
    is_mls_file = target_mode == "mls"
    rows = []
    for _, row in frame.iterrows():
        # Prefer display labels so provisional cup teams can be marked without affecting tracking keys.
        home = _team_name_for_display(str(row.get("display_home_team", row["home_team"])).strip())
        away = _team_name_for_display(str(row.get("display_away_team", row["away_team"])).strip())
        raw_date = str(row["match_date"])
        time_label = ""
        mls_dt_raw = str(row.get("match_datetime_et", "")).strip() if "match_datetime_et" in frame.columns else ""
        utc_dt_raw = str(row.get("match_datetime_utc", "")).strip() if "match_datetime_utc" in frame.columns else ""
        
        # Convert match_datetime_utc to Eastern time for display
        if utc_dt_raw:
            date_val = pd.to_datetime(utc_dt_raw, utc=True, errors="coerce")
            if pd.notna(date_val):
                try:
                    date_val = date_val.tz_convert("America/New_York")
                    time_label = date_val.strftime("%I:%M %p ET").lstrip("0")
                except Exception:
                    pass
        elif is_mls_file and mls_dt_raw:
            date_val = pd.to_datetime(mls_dt_raw, utc=True, errors="coerce")
            if pd.notna(date_val):
                try:
                    date_val = date_val.tz_convert("America/New_York")
                    time_label = date_val.strftime("%I:%M %p ET").lstrip("0")
                except Exception:
                    pass
        elif is_mls_file and len(raw_date) == 10 and raw_date.count("-") == 2:
            date_val = pd.to_datetime(raw_date, errors="coerce")
        else:
            date_val = pd.to_datetime(raw_date, utc=True, errors="coerce")
            if pd.notna(date_val):
                try:
                    date_val = date_val.tz_convert("America/New_York")
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
        rows.append(
            {
                "match_date": date_label if is_mls_file else str(row["match_date"]),
                "match_datetime_et": mls_dt_raw if is_mls_file else "",
                "weekday": weekday,
                "date_label": date_label,
                "time_label": time_label,
                "competition": str(row["competition"]),
                "home_team": home,
                "away_team": away,
                "winner_label": _winner_label(row["predicted_result"], home, away),
                "prob_home": ph,
                "prob_draw": pdv,
                "prob_away": pa,
                "prob_home_text": _format_percent_value(ph_raw),
                "prob_draw_text": _format_percent_value(pdv_raw),
                "prob_away_text": _format_percent_value(pa_raw),
                "pred_home_goals": int(pd.to_numeric(row.get("pred_home_goals"), errors="coerce")) if pd.notna(pd.to_numeric(row.get("pred_home_goals"), errors="coerce")) else None,
                "pred_away_goals": int(pd.to_numeric(row.get("pred_away_goals"), errors="coerce")) if pd.notna(pd.to_numeric(row.get("pred_away_goals"), errors="coerce")) else None,
                "reasoning": str(row.get("probability_reasoning", "")).strip(),
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
    persistent_stats, persistent_league_stats = _build_persistent_accuracy_stats(target_mode, rows)
    return rows, persistent_stats, persistent_league_stats


def _load_projected_tables(csv_path):
    """Load projected table CSV into API-ready league/table structure."""
    if not os.path.exists(csv_path):
        return {"leagues": [], "tables": {}}
    try:
        if LOW_MEMORY_STATIC:
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
    for competition, comp_frame in frame.groupby("competition", dropna=False):
        rows = []
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
            rows.append(
                {
                    "position": int(row["position"]) if pd.notna(row["position"]) else 0,
                    "team": _team_name_for_display(str(row["team"])),
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
        tables[str(competition)] = rows

    leagues = sorted(tables.keys(), key=lambda name: name.lower())
    return {"leagues": leagues, "tables": tables}


def _load_json_payload(path):
    """Safely load JSON payload from disk, returning None on failure."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
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
    if not os.path.exists(LIVE_RESULTS_UPDATER):
        print(f"[startup] Live updater not found: {LIVE_RESULTS_UPDATER}")
        return
    try:
        proc = subprocess.run(
            [sys.executable, LIVE_RESULTS_UPDATER],
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


def _run_full_pipeline_once():
    """Run full data/model refresh pipeline and reload in-memory predictor contexts."""
    global _ctx_global, _ctx_mls, _ctx_extra
    if not os.path.exists(RUN_ALL_PIPELINE):
        print(f"[refresh] Pipeline runner not found: {RUN_ALL_PIPELINE}")
        return False
    try:
        proc = subprocess.run(
            [sys.executable, RUN_ALL_PIPELINE],
            cwd=PROJECT_DIR,
            timeout=3600,
            check=False,
        )
        if proc.returncode != 0:
            print(f"[refresh] Daily pipeline failed with rc={proc.returncode}.")
            return False
        print("[refresh] Daily pipeline finished successfully.")
    except subprocess.TimeoutExpired:
        print("[refresh] Daily pipeline timed out after 3600s.")
        return False
    except Exception as exc:
        print(f"[refresh] Daily pipeline error: {exc}")
        return False

    global _last_pipeline_run
    _last_pipeline_run = datetime.now(ZoneInfo("America/New_York"))
    _save_last_refresh()
    with _ctx_lock:
        _ctx_global = None
        _ctx_mls = None
        _ctx_extra = None
    _static_predictions_cache.clear()
    _static_team_cache.clear()
    update_accuracy_history_files()
    if not STATIC_PREDICTIONS:
        try:
            # Warm both contexts so API requests do not pay first-load penalty.
            get_context("global")
            get_context("mls")
            get_context("extra")
            print("[refresh] Model contexts reloaded successfully.")
        except Exception as exc:
            print(f"[refresh] Context reload warning: {exc}")
    return True


# Scheduler removed — BackendServer handles pipeline scheduling.
# Previously _seconds_until_next_refresh, _daily_refresh_loop, and start_daily_refresh_scheduler were here.


def _should_run_startup_tasks(debug_mode):
    """Avoid running startup jobs twice when Flask reloader is enabled."""
    return (not debug_mode) or os.environ.get("WERKZEUG_RUN_MAIN") == "true"


def _safe_filename(name):
    """Convert league names into filesystem-safe filenames."""
    text = "".join(ch if ch.isalnum() else "_" for ch in str(name or "").strip())
    text = "_".join(part for part in text.split("_") if part)
    return text[:120] or "unknown_league"


HISTORY_COLUMNS = [
    "prediction_key",
    "match_date",
    "competition",
    "home_team",
    "away_team",
    "predicted_result",
    "actual_result",
    "is_correct",
]


def _update_accuracy_history_from_csv(csv_path, source_key):
    """Append settled predictions into per-league accuracy history CSV files.

    Each per-league file stores only the singular values needed to compute
    accuracy (one row per match). Derived metrics (totals/percentages) and
    prediction detail columns (probabilities, predicted goals/shots, etc.)
    are intentionally excluded — they can be recomputed on demand from
    these rows.
    """
    if not os.path.exists(csv_path):
        return 0, 0
    try:
        frame = pd.read_csv(csv_path)
    except Exception:
        return 0, 0
    if frame.empty or "competition" not in frame.columns or "prediction_key" not in frame.columns:
        return 0, 0

    source_dir = os.path.join(ACCURACY_HISTORY_DIR, source_key)
    os.makedirs(source_dir, exist_ok=True)
    files_touched = 0
    rows_added = 0
    if "actual_result" in frame.columns:
        settled_mask = frame["actual_result"].astype(str).str.strip().isin({"H", "D", "A"})
        settled = frame[settled_mask].copy()
    else:
        settled = pd.DataFrame(columns=HISTORY_COLUMNS)

    all_competitions = sorted(set(frame["competition"].astype(str).str.strip()))
    for competition in all_competitions:
        league_name = str(competition).strip() or "Unknown"
        league_file = os.path.join(source_dir, f"{_safe_filename(league_name)}.csv")
        comp_data = settled[settled["competition"].astype(str).str.strip() == league_name].copy()
        if not comp_data.empty:
            comp_data = comp_data.reindex(columns=HISTORY_COLUMNS).copy()
            comp_data["competition"] = league_name
        else:
            comp_data = pd.DataFrame(columns=HISTORY_COLUMNS)

        if os.path.exists(league_file):
            try:
                existing = pd.read_csv(league_file)
            except Exception:
                existing = pd.DataFrame(columns=HISTORY_COLUMNS)
        else:
            existing = pd.DataFrame(columns=HISTORY_COLUMNS)

        # Existing files written by the old schema get re-projected to the
        # new singular-value schema on the next read; missing columns are
        # introduced as empty so the concat stays consistent.
        for col in HISTORY_COLUMNS:
            if col not in existing.columns:
                existing[col] = pd.Series(dtype="object")
        existing = existing.reindex(columns=HISTORY_COLUMNS)

        before = len(existing)
        merged = pd.concat([existing, comp_data], ignore_index=True) if not comp_data.empty else existing.copy()
        if not merged.empty:
            merged = merged.drop_duplicates(subset=["prediction_key"], keep="last")
        after = len(merged)
        merged.to_csv(league_file, index=False)
        files_touched += 1
        rows_added += max(0, after - before)

    return files_touched, rows_added


def update_accuracy_history_files():
    """Refresh global, MLS, extra-league, and cup accuracy history stores."""
    os.makedirs(ACCURACY_HISTORY_DIR, exist_ok=True)
    global_files, global_rows = _update_accuracy_history_from_csv(GLOBAL_UPCOMING_FILE, "global")
    mls_files, mls_rows = _update_accuracy_history_from_csv(MLS_UPCOMING_FILE, "mls")
    extra_files, extra_rows = _update_accuracy_history_from_csv(EXTRA_UPCOMING_FILE, "extra")
    cup_files, cup_rows = _update_accuracy_history_from_csv(CUP_COMPLETED_FILE, "cups")
    print(
        "[startup] Accuracy history updated: "
        f"global_files={global_files}, global_new_rows={global_rows}, "
        f"mls_files={mls_files}, mls_new_rows={mls_rows}, "
        f"extra_files={extra_files}, extra_new_rows={extra_rows}, "
        f"cup_files={cup_files}, cup_new_rows={cup_rows}"
    )


@app.get("/")
def index():
    """Render the home page with shared team context."""
    return _render_site_page("home.html", active_page="home")


def _render_site_page(template_name, active_page):
    """Render a website tab page with shared team lists for forms and datalists."""
    # Shared route map used by template JS navigation helpers.
    page_routes = {
        "home": "/",
        "global": "/upcoming-matches",
        "cups": "/cups",
        "h2h": "/head-to-head",
        "league-table": "/league-tables",
        "world-cup": "/world-cup",
        "players": "/players",
        "tactics": "/tactics",
        "about": "/about",
    }
    # Template defaults prevent Undefined errors for pages that serialize these values.
    upcoming_leagues = {"global": [], "mls": [], "extra": [], "cups": []}
    table_leagues = {"global": [], "mls": [], "extra": [], "cups": []}

    if STATIC_PREDICTIONS:
        _, global_teams = _get_static_predictions("global")
        _, mls_teams = _get_static_predictions("mls")
        _, extra_teams = _get_static_predictions("extra")
        if not global_teams:
            global_teams = set(_load_teams_from_team_data(pm_global))
        if not mls_teams:
            mls_teams = set(_load_teams_from_team_data(pm_mls))
        if not extra_teams:
            extra_teams = set(_load_teams_from_team_data(pm_extra))
        global_display_teams = sorted({_team_name_for_display(team) for team in global_teams})
        mls_display_teams = sorted({_team_name_for_display(team) for team in mls_teams})
        extra_display_teams = sorted({_team_name_for_display(team) for team in extra_teams})
    else:
        global_ctx = get_context("global")
        mls_ctx = get_context("mls")
        global_display_teams = sorted({_team_name_for_display(team) for team in global_ctx.available_teams})
        mls_display_teams = sorted({_team_name_for_display(team) for team in mls_ctx.available_teams})
        try:
            extra_ctx = get_context("extra")
            extra_display_teams = sorted({_team_name_for_display(team) for team in extra_ctx.available_teams})
        except Exception:
            extra_display_teams = sorted({_team_name_for_display(team) for team in _load_teams_from_team_data(pm_extra)})
    return render_template(
        template_name,
        # Active page keeps nav highlighting/panel state aligned per template.
        active_page=active_page,
        page_routes=page_routes,
        upcoming_leagues=upcoming_leagues,
        table_leagues=table_leagues,
        teams=global_display_teams,
        mls_teams=mls_display_teams,
        extra_teams=extra_display_teams,
    )


@app.get("/upcoming-matches")
def upcoming_matches():
    """Render the upcoming matches tab page."""
    return _render_site_page("upcoming_matches.html", active_page="global")


@app.get("/cups")
def cups_page():
    """Render the cups tab page."""
    return _render_site_page("cups.html", active_page="cups")


@app.get("/head-to-head")
def head_to_head():
    """Render the head-to-head tab page."""
    return _render_site_page("head_to_head.html", active_page="h2h")


@app.get("/league-tables")
def league_tables():
    """Render the projected league tables tab page."""
    return _render_site_page("league_tables.html", active_page="league-table")


@app.get("/world-cup")
def world_cup():
    """Render the World Cup tab page."""
    return _render_site_page("world_cup.html", active_page="world-cup")


@app.get("/about")
def about():
    """Render the about tab page."""
    return _render_site_page("about.html", active_page="about")


@app.get("/api/teams")
def api_teams():
    """Return selectable teams for the requested prediction mode."""
    mode = str(request.args.get("mode", "global")).strip().lower()
    if mode not in {"global", "mls", "extra"}:
        mode = "global"
    if STATIC_PREDICTIONS:
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


@app.get("/api/world-cup")
def api_world_cup():
    """Return the World Cup projection data."""
    world_cup_file = os.path.join(PROJECT_DIR, "Data", "Predictions", "world_cup_projection.json")
    if not os.path.exists(world_cup_file):
        return jsonify({"ok": False, "error": "World Cup projection not available"}), 404
    try:
        with open(world_cup_file, "r") as f:
            data = json.load(f)
        return jsonify(data)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/refresh")
def api_refresh():
    """Trigger a full pipeline refresh in the background."""
    if not _refresh_auth_ok():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    backend_refresh = app.config.get("_backend_refresh")
    if backend_refresh:
        backend_refresh(trigger="manual")
        return jsonify({"ok": True, "message": "Refresh started."})

    def _run():
        print("[refresh] Manual refresh requested via API (legacy path).")
        _run_full_pipeline_once()

    threading.Thread(target=_run, daemon=True, name="manual-refresh").start()
    return jsonify({"ok": True, "message": "Refresh started."})


@app.get("/api/last-refresh")
def api_last_refresh():
    """Return the timestamp of the last successful pipeline run.
    
    The iOS app can compare this with its own cached timestamp to decide
    whether to reload data or use the cache.
    """
    global _last_pipeline_run
    if _last_pipeline_run is None:
        return jsonify({"ok": True, "last_refresh_utc": None})
    return jsonify({
        "ok": True,
        "last_refresh_utc": _last_pipeline_run.isoformat(),
    })


@app.get("/api/last-data-refresh")
def api_last_data_refresh():
    """Return the timestamp of the last data refresh (any pipeline run).
    
    The iOS app can compare this with its own cached timestamp to decide
    whether to reload data or use the cache. This is updated on every
    pipeline run (both full retrain and light refresh).
    """
    dt = _load_last_data_refresh()
    if dt is None:
        return jsonify({"ok": True, "last_data_refresh_utc": None})
    return jsonify({
        "ok": True,
        "last_data_refresh_utc": dt.isoformat(),
    })


MOBILE_FEED_FILE = os.path.join(PROJECT_DIR, "Output", "mobile_app_feed.json")
_UPCOMING_CSV_FILES = {
    "global": os.path.join(PROJECT_DIR, "Data", "Predictions", "upcoming_matchweek_predictions.csv"),
    "mls": os.path.join(PROJECT_DIR, "MLS", "Data", "Predictions", "upcoming_matchweek_predictions.csv"),
    "extra": os.path.join(PROJECT_DIR, "Extra-leagues", "Data", "Predictions", "upcoming_matchweek_predictions.csv"),
    "cups": os.path.join(PROJECT_DIR, "Data", "Predictions", "upcoming_cup_predictions.csv"),
    "national": os.path.join(PROJECT_DIR, "Data", "Predictions", "upcoming_national_team_predictions.csv"),
}


@app.get("/api/mobile/feed")
def api_mobile_feed():
    """Return the full mobile-app feed JSON."""
    if not os.path.exists(MOBILE_FEED_FILE):
        return jsonify({"ok": False, "error": "Mobile feed not yet generated."}), 404
    try:
        with open(MOBILE_FEED_FILE, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/mobile/widget")
def api_mobile_widget():
    """Lightweight widget feed: upcoming games filtered by league/team/random.
    
    Query params:
        league  — comma-separated league names (e.g. \"England/Premier League,Spain/La Liga\")
        team    — team name to filter by (e.g. \"Chelsea\")
        limit   — max rows to return (default 10, max 50)
        mode    — \"random\" to shuffle and return random picks
    """
    import random as _random

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
    for csv_path in _UPCOMING_CSV_FILES.values():
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
                "match_datetime_utc": str(row.get("match_datetime_utc", "") or "").strip(),
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


def _to_float(val):
    try:
        return round(float(val), 1)
    except (ValueError, TypeError):
        return None


@app.post("/api/predict")
def api_predict():
    """Predict a single matchup from user input.
    
    JSON body:
        home_team (str, required)
        away_team (str, required)
        mode (str, optional) — "global" (default), "mls", or "extra"
    """
    payload = request.get_json(silent=True) or request.form
    home_team = str(payload.get("home_team", "")).strip()
    away_team = str(payload.get("away_team", "")).strip()
    mode = str(payload.get("mode", "global")).strip().lower()
    if mode not in ("global", "mls", "extra"):
        mode = "global"
    try:
        result = _predict(home_team, away_team, mode=mode)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "prediction": result})


@app.post("/api/predict/mls")
def api_predict_mls():
    """Predict a single MLS matchup from user input."""
    payload = request.get_json(silent=True) or request.form
    home_team = str(payload.get("home_team", "")).strip()
    away_team = str(payload.get("away_team", "")).strip()
    try:
        result = _predict(home_team, away_team, mode="mls")
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "prediction": result})


@app.post("/api/predict/extra")
def api_predict_extra():
    """Predict a single extra-league matchup from user input."""
    payload = request.get_json(silent=True) or request.form
    home_team = str(payload.get("home_team", "")).strip()
    away_team = str(payload.get("away_team", "")).strip()
    try:
        result = _predict(home_team, away_team, mode="extra")
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "prediction": result})


@app.post("/api/notifications")
def api_push_notification():
    """Push a notification to the in-memory queue. Requires API key auth."""
    key = request.headers.get("X-Notifications-Key", "").strip()
    if NOTIFICATIONS_API_KEY and key != NOTIFICATIONS_API_KEY:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title", "")).strip()
    body = str(payload.get("body", "")).strip()
    if not title or not body:
        return jsonify({"ok": False, "error": "title and body required"}), 400
    _notifications.append({
        "id": len(_notifications),
        "title": title,
        "body": body,
        "created_at": datetime.now(UTC).isoformat(),
        "type": payload.get("type", "info"),
    })
    return jsonify({"ok": True})


@app.get("/api/notifications")
def api_get_notifications():
    """Return recent notifications."""
    limit = min(int(request.args.get("limit", "20")), 100)
    items = list(_notifications)[-limit:]
    return jsonify({"ok": True, "notifications": items})


@app.post("/api/notifications/register")
def api_register_device():
    """Register a device token for push notifications."""
    key = request.headers.get("X-Notifications-Key", "").strip()
    if NOTIFICATIONS_API_KEY and key != NOTIFICATIONS_API_KEY:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "")).strip()
    if not token:
        return jsonify({"ok": False, "error": "token required"}), 400
    _device_tokens.add(token)
    return jsonify({"ok": True, "registered": True})


@app.get("/api/live-scores")
def api_live_scores():
    """Return live scores for active competitions (polled every 5 min from ESPN).

    Query params:
        competition  -- optional, filter to specific competition(s) (comma-separated)
    """
    comp_filter = request.args.get("competition", "").strip()
    with _live_scores_lock:
        if not _live_scores:
            return jsonify({"ok": True, "competitions": {}, "message": "No live games at this time."})
        if comp_filter:
            wanted = {c.strip() for c in comp_filter.split(",") if c.strip()}
            filtered = {k: v for k, v in _live_scores.items() if k in wanted}
            return jsonify({"ok": True, "competitions": filtered})
        return jsonify({"ok": True, "competitions": dict(_live_scores)})


@app.get("/api/h2h")
def api_h2h():
    """Return head-to-head and form data for two teams."""
    team1_input = request.args.get("team1", "").strip()
    team2_input = request.args.get("team2", "").strip()
    mode = request.args.get("mode", "global").strip().lower()
    
    if STATIC_PREDICTIONS:
        if mode == "mls":
            pm_mod = pm_mls
        elif mode == "extra":
            pm_mod = pm_extra
        else:
            pm_mod = pm_global
        head_to_head, current_form = _load_h2h_and_form(pm_mod)
        ctx = type("StaticCtx", (), {"head_to_head": head_to_head, "current_form": current_form})
    else:
        ctx = get_context(mode)
    
    if not team1_input or not team2_input:
        return jsonify({"ok": False, "error": "Missing teams"}), 400
    team1 = _team_name_for_db(team1_input)
    team2 = _team_name_for_db(team2_input)
        
    t1_form = _normalize_recent_form_payload(ctx.current_form.get("teams", {}).get(team1, {}))
    t2_form = _normalize_recent_form_payload(ctx.current_form.get("teams", {}).get(team2, {}))
    
    h2h_data = _normalize_h2h_payload(ctx.head_to_head.get(team1, {}).get(team2))
    h2h_data_reverse = _normalize_h2h_payload(ctx.head_to_head.get(team2, {}).get(team1))
    h2h_total_games = max(h2h_data.get("games", 0), h2h_data_reverse.get("games", 0))

    return jsonify({
        "ok": True,
        "team1_form": t1_form,
        "team2_form": t2_form,
        "h2h_data": h2h_data,
        "h2h_data_reverse": h2h_data_reverse,
        "h2h_total_games": h2h_total_games,
    })


@app.get("/api/team")
def api_team():
    """Return form, recent results, upcoming games, and head-to-head vs all opponents for one team.

    Query params:
        team  -- team name (required)
        mode  -- global / mls / extra (default: global)
    """
    team_input = request.args.get("team", "").strip()
    mode = request.args.get("mode", "global").strip().lower()

    if STATIC_PREDICTIONS:
        if mode == "mls":
            pm_mod = pm_mls
        elif mode == "extra":
            pm_mod = pm_extra
        else:
            pm_mod = pm_global
        head_to_head, current_form = _load_h2h_and_form(pm_mod)
        ctx = type("StaticCtx", (), {"head_to_head": head_to_head, "current_form": current_form})
    else:
        ctx = get_context(mode)

    if not team_input:
        return jsonify({"ok": False, "error": "Missing team"}), 400
    team = _team_name_for_db(team_input)

    team_form = _normalize_recent_form_payload(ctx.current_form.get("teams", {}).get(team, {}))

    all_h2h = {}
    h2h_opponents = ctx.head_to_head.get(team, {})
    for opponent, payload in h2h_opponents.items():
        all_h2h[opponent] = _normalize_h2h_payload(payload)

    upcoming = []
    csv_path = _UPCOMING_CSV_FILES.get(mode) or _UPCOMING_CSV_FILES.get("global")
    if csv_path and os.path.exists(csv_path):
        try:
            frame = pd.read_csv(csv_path, dtype=str)
            team_lower = team.lower()
            for _, row in frame.iterrows():
                home = str(row.get("home_team", "") or "").strip()
                away = str(row.get("away_team", "") or "").strip()
                if team_lower not in (home.lower(), away.lower()):
                    continue
                upcoming.append({
                    "competition": str(row.get("competition", "") or "").strip(),
                    "match_date": str(row.get("match_date", "") or "").strip(),
                    "match_datetime_utc": str(row.get("match_datetime_utc", "") or "").strip(),
                    "home_team": home,
                    "away_team": away,
                    "predicted_result": str(row.get("predicted_result", "") or "").strip(),
                })
        except Exception:
            pass

    return jsonify({
        "ok": True,
        "team": team,
        "form": team_form,
        "upcoming_games": upcoming,
        "head_to_head": all_h2h,
    })


@app.get("/api/help")
def api_help():
    """List all available API endpoints with brief descriptions."""
    return jsonify({
        "ok": True,
        "endpoints": [
            {"method": "GET", "path": "/api/help", "desc": "List all available API endpoints"},
            {"method": "GET", "path": "/api/teams?mode=global|mls|extra", "desc": "List teams for a given mode"},
            {"method": "GET", "path": "/api/world-cup", "desc": "World Cup projection data"},
            {"method": "POST", "path": "/api/refresh", "desc": "Trigger a full pipeline refresh"},
            {"method": "GET", "path": "/api/last-refresh", "desc": "Timestamp of last successful pipeline run"},
            {"method": "GET", "path": "/api/last-data-refresh", "desc": "Timestamp of last data refresh (any pipeline run)"},
            {"method": "GET", "path": "/api/mobile/feed", "desc": "Full mobile-app feed JSON"},
            {"method": "GET", "path": "/api/mobile/widget?league=&team=&limit=&mode=", "desc": "Lightweight widget feed with filters"},
            {"method": "POST", "path": "/api/predict", "desc": "Predict outcome for a specific match"},
            {"method": "POST", "path": "/api/predict/mls", "desc": "Predict outcome for an MLS match"},
            {"method": "POST", "path": "/api/predict/extra", "desc": "Predict outcome for an extra-league match"},
            {"method": "POST", "path": "/api/notifications", "desc": "Send a push notification"},
            {"method": "GET", "path": "/api/notifications", "desc": "Retrieve recent notifications"},
            {"method": "POST", "path": "/api/notifications/register", "desc": "Register a device for push notifications"},
            {"method": "GET", "path": "/api/h2h?team1=&team2=&mode=", "desc": "Head-to-head and form data for two teams"},
            {"method": "GET", "path": "/api/team?team=&mode=", "desc": "Form, upcoming games, and H2H for a single team"},
            {"method": "GET", "path": "/api/live-scores?competition=", "desc": "Live scores (polled from ESPN every 90s)"},
            {"method": "GET", "path": "/api/upcoming/global", "desc": "Upcoming global fixtures (club + national team)"},
            {"method": "GET", "path": "/api/upcoming/extra", "desc": "Upcoming extra-league fixtures"},
            {"method": "GET", "path": "/api/upcoming/cups", "desc": "Upcoming cup fixtures"},
            {"method": "GET", "path": "/api/upcoming/world-cup", "desc": "Upcoming World Cup group-stage fixtures"},
            {"method": "GET", "path": "/api/top-picks", "desc": "Top picks for the upcoming matchweek"},
            {"method": "GET", "path": "/api/league-tables?mode=global|mls|extra|cups", "desc": "Projected league tables"},
            {"method": "GET", "path": "/api/stats", "desc": "Overall site statistics (accuracy, league count)"},
            {"method": "POST", "path": "/api/feedback", "desc": "Submit user feedback"},
            {"method": "GET", "path": "/api/scorers", "desc": "Top scorers by competition"},
        ],
    })


@app.get("/api/upcoming/global")
def api_upcoming_global():
    """Return upcoming global fixtures (club + national team) and persistent accuracy stats."""
    global_rows, global_stats, global_league_stats = _load_upcoming_rows(GLOBAL_UPCOMING_FILE, "global")
    national_rows, national_stats, national_league_stats = _load_upcoming_rows(NATIONAL_UPCOMING_FILE, "national")
    rows = global_rows + national_rows
    stats = global_stats.copy()
    stats["pending_total"] = global_stats.get("pending_total", 0) + national_stats.get("pending_total", 0)
    stats["total_predictions"] = stats.get("settled_total", 0) + stats.get("pending_total", 0)
    league_stats = global_league_stats + national_league_stats
    return jsonify({"ok": True, "rows": rows, "stats": stats, "league_stats": league_stats})


@app.get("/api/upcoming/extra")
def api_upcoming_extra():
    """Return upcoming extra-league fixtures and persistent accuracy stats."""
    rows, stats, league_stats = _load_upcoming_rows(EXTRA_UPCOMING_FILE, "extra")
    return jsonify({"ok": True, "rows": rows, "stats": stats, "league_stats": league_stats})


@app.get("/api/upcoming/cups")
def api_upcoming_cups():
    """Return upcoming cup fixtures and persistent accuracy stats."""
    rows, stats, league_stats = _load_upcoming_rows(CUP_UPCOMING_FILE, "cups")
    return jsonify({"ok": True, "rows": rows, "stats": stats, "league_stats": league_stats})


@app.get("/api/upcoming/world-cup")
def api_upcoming_world_cup():
    """Return upcoming World Cup GROUP-STAGE fixtures (not knockouts).

    Knockout fixtures are excluded until the actual knockout teams are decided
    (i.e. once the group stage is complete and the Round of 32 bracket is set).
    """
    world_cup_file = os.path.join(PROJECT_DIR, "Data", "Predictions", "world_cup_projection.json")
    if not os.path.exists(world_cup_file):
        return jsonify({"ok": True, "rows": [], "stats": {}, "league_stats": []})
    try:
        with open(world_cup_file, "r", encoding="utf-8") as fh:
            wc_data = json.load(fh)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Failed to load World Cup projection: {exc}"}), 500

    # Only include group-stage fixtures for the upcoming list. Knockout matchups
    # depend on group-stage outcomes that are not yet determined, so we leave
    # the round-of-32 / round-of-16 / QF / SF / Final off the upcoming page
    # until real match data confirms the teams.
    raw_fixtures = wc_data.get("group_fixtures", [])
    today = pd.Timestamp(datetime.now().date())
    rows = []
    for fixture in raw_fixtures:
        match_date_str = str(fixture.get("match_date", "")).strip()
        if not match_date_str:
            continue
        try:
            match_date = pd.to_datetime(match_date_str, errors="coerce")
        except Exception:
            match_date = None
        if match_date is None or pd.isna(match_date):
            continue
        if match_date < today:
            continue

        home = _team_name_for_display(str(fixture.get("display_home_team") or fixture.get("home_team") or "").strip())
        away = _team_name_for_display(str(fixture.get("display_away_team") or fixture.get("away_team") or "").strip())
        if not home or not away:
            continue

        try:
            ph_raw = float(fixture.get("prob_home", 0.0)) * 100
            pdv_raw = float(fixture.get("prob_draw", 0.0)) * 100
            pa_raw = float(fixture.get("prob_away", 0.0)) * 100
        except Exception:
            ph_raw = pdv_raw = pa_raw = 0.0

        utc_dt_raw = str(fixture.get("match_datetime_utc", "")).strip()
        date_val = pd.to_datetime(utc_dt_raw, utc=True, errors="coerce") if utc_dt_raw else pd.NaT
        if pd.isna(date_val):
            weekday = ""
            date_label = match_date_str
            time_label = ""
        else:
            try:
                date_val_et = date_val.tz_convert("America/New_York")
            except Exception:
                date_val_et = date_val
            weekday = date_val_et.strftime("%A")
            date_label = date_val_et.strftime("%B %d, %Y")
            try:
                time_label = date_val_et.strftime("%I:%M %p ET").lstrip("0")
            except Exception:
                time_label = ""

        # Score prediction: prefer the deterministic `pred_home_goals`/`pred_away_goals`
        # already on the projection. If those are missing, fall back to a 0-0 placeholder.
        ph_goals = fixture.get("pred_home_goals")
        pa_goals = fixture.get("pred_away_goals")
        try:
            ph_goals_int = int(ph_goals) if ph_goals is not None and not pd.isna(ph_goals) else None
        except Exception:
            ph_goals_int = None
        try:
            pa_goals_int = int(pa_goals) if pa_goals is not None and not pd.isna(pa_goals) else None
        except Exception:
            pa_goals_int = None

        rows.append({
            "match_date": match_date_str,
            "match_datetime_et": "",
            "weekday": weekday,
            "date_label": date_label,
            "time_label": time_label,
            "competition": str(fixture.get("competition", "FIFA/World Cup")),
            "stage": str(fixture.get("stage", "group-stage")),
            "group": str(fixture.get("group", "")),
            "venue": str(fixture.get("venue", "")),
            "home_team": home,
            "away_team": away,
            "winner_label": _winner_label(str(fixture.get("predicted_result", "")), home, away),
            "prob_home": round(ph_raw, 3),
            "prob_draw": round(pdv_raw, 3),
            "prob_away": round(pa_raw, 3),
            "prob_home_text": _format_percent_value(ph_raw),
            "prob_draw_text": _format_percent_value(pdv_raw),
            "prob_away_text": _format_percent_value(pa_raw),
            "pred_home_goals": ph_goals_int,
            "pred_away_goals": pa_goals_int,
            "reasoning": "",
            "actual_result": "",
            "is_correct": "",
        })

    rows.sort(key=lambda r: (r["match_date"], r["competition"], r["home_team"]))
    empty_frame = pd.DataFrame(rows)
    return jsonify({
        "ok": True,
        "rows": rows,
        "stats": _compute_accuracy_stats(empty_frame),
        "league_stats": _compute_league_accuracy_stats(empty_frame),
    })


def _row_confidence(row):
    """Confidence for a top-picks ranking: max of the three outcome probs.

    Range 0-100. Higher means the model is more decisive about the result.
    """
    try:
        ph = float(row.get("prob_home") or 0.0)
        pdv = float(row.get("prob_draw") or 0.0)
        pa = float(row.get("prob_away") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return max(ph, pdv, pa)


def _row_match_key(row):
    """Stable key for de-duplicating the same matchup across data sources."""
    return (
        str(row.get("match_date") or "").strip(),
        str(row.get("home_team") or "").strip().lower(),
        str(row.get("away_team") or "").strip().lower(),
    )


def _is_valid_top_pick(row):
    """Filter to rows with full team labels + non-negative numeric probs."""
    if not row:
        return False
    if not row.get("home_team") or not row.get("away_team") or not row.get("winner_label"):
        return False
    try:
        h = float(row.get("prob_home"))
        d = float(row.get("prob_draw"))
        a = float(row.get("prob_away"))
    except (TypeError, ValueError):
        return False
    if not all(map(lambda v: v == v and v >= 0, (h, d, a))):  # filter NaN + negatives
        return False
    return True


def _is_future_top_pick(row):
    """Keep only fixtures dated today or later."""
    raw = str(row.get("match_date") or "").strip()
    if not raw:
        return False
    parsed = pd.to_datetime(raw, errors="coerce")
    if pd.isna(parsed):
        return False
    return parsed.normalize() >= pd.Timestamp(datetime.now().date())


@app.get("/api/top-picks")
def api_top_picks():
    """Return the top-N most confident upcoming predictions across all sources.

    Aggregates rows from the five /api/upcoming/* sources (global, mls, extra,
    cups, world-cup), de-duplicates identical matchups, filters to future
    fixtures, sorts by max(prob_home, prob_draw, prob_away) descending, and
    returns the top N rows. The home page calls this single endpoint for
    its top-picks widget instead of fetching the five separate sources
    (which together ship 100+ rows just to display 12 cards).
    """
    try:
        limit = int(request.args.get("limit", "12"))
    except (TypeError, ValueError):
        limit = 12
    limit = max(1, min(limit, 50))

    seen = {}
    for source, csv_path in (
        ("global", GLOBAL_UPCOMING_FILE),
        ("mls", MLS_UPCOMING_FILE),
        ("extra", EXTRA_UPCOMING_FILE),
        ("cups", CUP_UPCOMING_FILE),
        ("national", NATIONAL_UPCOMING_FILE),
    ):
        rows, _stats, _league_stats = _load_upcoming_rows(csv_path, source)
        for row in rows:
            if not _is_valid_top_pick(row):
                continue
            key = _row_match_key(row)
            if not key[0] or not key[1] or not key[2]:
                continue
            # First source wins; later sources are skipped.
            seen.setdefault(key, row)

    # World Cup upcoming is a different shape (read from the projection JSON,
    # not the CSV files). Append those rows so WC group-stage games show up
    # alongside the league fixtures in the top-picks widget.
    try:
        world_cup_file = os.path.join(PROJECT_DIR, "Data", "Predictions", "world_cup_projection.json")
        if os.path.exists(world_cup_file):
            with open(world_cup_file, "r", encoding="utf-8") as fh:
                wc_data = json.load(fh)
            today = pd.Timestamp(datetime.now().date())
            for fixture in wc_data.get("group_fixtures", []) or []:
                match_date = pd.to_datetime(fixture.get("match_date", ""), errors="coerce")
                if pd.isna(match_date) or match_date < today:
                    continue
                home = str(fixture.get("display_home_team") or fixture.get("home_team") or "").strip()
                away = str(fixture.get("display_away_team") or fixture.get("away_team") or "").strip()
                if not home or not away:
                    continue
                try:
                    ph = round(float(fixture.get("prob_home", 0.0)) * 100, 3)
                    pdv = round(float(fixture.get("prob_draw", 0.0)) * 100, 3)
                    pa = round(float(fixture.get("prob_away", 0.0)) * 100, 3)
                except (TypeError, ValueError):
                    continue
                try:
                    phg = int(fixture["pred_home_goals"]) if fixture.get("pred_home_goals") is not None and not pd.isna(fixture["pred_home_goals"]) else None
                    pag = int(fixture["pred_away_goals"]) if fixture.get("pred_away_goals") is not None and not pd.isna(fixture["pred_away_goals"]) else None
                except (TypeError, ValueError):
                    phg = pag = None
                weekday = ""
                date_label = str(fixture.get("match_date", ""))
                time_label = ""
                utc_raw = str(fixture.get("match_datetime_utc", "")).strip()
                if utc_raw:
                    dt_val = pd.to_datetime(utc_raw, utc=True, errors="coerce")
                    if not pd.isna(dt_val):
                        try:
                            dt_val_et = dt_val.tz_convert("America/New_York")
                        except Exception:
                            dt_val_et = dt_val
                        weekday = dt_val_et.strftime("%A")
                        date_label = dt_val_et.strftime("%B %d, %Y")
                        try:
                            time_label = dt_val_et.strftime("%I:%M %p ET").lstrip("0")
                        except Exception:
                            time_label = ""
                wc_row = {
                    "match_date": str(fixture.get("match_date", "")),
                    "match_datetime_et": "",
                    "weekday": weekday,
                    "date_label": date_label,
                    "time_label": time_label,
                    "competition": str(fixture.get("competition", "FIFA/World Cup")),
                    "stage": str(fixture.get("stage", "group-stage")),
                    "group": str(fixture.get("group", "")),
                    "venue": str(fixture.get("venue", "")),
                    "home_team": home,
                    "away_team": away,
                    "winner_label": _winner_label(str(fixture.get("predicted_result", "")), home, away),
                    "prob_home": ph,
                    "prob_draw": pdv,
                    "prob_away": pa,
                    "pred_home_goals": phg,
                    "pred_away_goals": pag,
                    "reasoning": "",
                    "actual_result": "",
                    "is_correct": "",
                }
                if not _is_valid_top_pick(wc_row):
                    continue
                key = _row_match_key(wc_row)
                if not key[0] or not key[1] or not key[2]:
                    continue
                seen.setdefault(key, wc_row)
    except Exception:
        # WC projection is optional for the top-picks widget. Swallow errors so
        # the rest of the picks still render.
        pass

    # De-dupe and filter to future fixtures.
    candidates = [r for r in seen.values() if _is_future_top_pick(r)]

    # Sort by confidence desc, then by date asc (earliest first), then by
    # competition + teams for a deterministic order when ties exist.
    candidates.sort(
        key=lambda r: (
            -_row_confidence(r),
            str(r.get("match_date") or ""),
            str(r.get("competition") or "").lower(),
            str(r.get("home_team") or "").lower(),
        )
    )
    return jsonify({
        "ok": True,
        "rows": candidates[:limit],
        "total_candidates": len(candidates),
        "limit": limit,
    })


@app.get("/api/league-tables")
def api_league_tables():
    """Return projected league tables (and MLS playoff bracket when requested)."""
    mode = str(request.args.get("mode", "global")).strip().lower()
    if mode == "mls":
        data = _load_projected_tables(MLS_PROJECTED_TABLE_FILE)
        bracket = _load_json_payload(MLS_PROJECTED_BRACKET_FILE)
        return jsonify({"ok": True, **data, "bracket": bracket})
    if mode == "cups":
        data = _load_projected_tables(CUP_PROJECTED_TABLE_FILE)
        brackets = _load_json_payload(CUP_PROJECTED_BRACKET_FILE)
        return jsonify({"ok": True, **data, "cup_brackets": brackets})
    if mode == "extra":
        data = _load_projected_tables(EXTRA_PROJECTED_TABLE_FILE)
        return jsonify({"ok": True, **data})
    else:
        data = _load_projected_tables(GLOBAL_PROJECTED_TABLE_FILE)
    return jsonify({"ok": True, **data})


@app.get("/api/stats")
def api_stats():
    """Return overall site stats: accuracy, league count, last refresh time."""
    global _last_pipeline_run
    try:
        rows, stats, league_stats = _load_upcoming_rows(GLOBAL_UPCOMING_FILE, "global")
        accuracy_pct = (stats or {}).get("accuracy_pct", 0.0)
    except Exception:
        accuracy_pct = 0.0
    refreshed_at = _last_pipeline_run.isoformat() if _last_pipeline_run else None
    return jsonify({
        "ok": True,
        "accuracy_pct": accuracy_pct,
        "league_count": 18,
        "refreshed_at": refreshed_at,
    })


@app.post("/api/feedback")
def api_feedback():
    """Persist user feedback to a local text file."""
    payload = request.get_json(silent=True) or request.form or {}
    feedback_text = str(payload.get("feedback", "")).strip()
    if not feedback_text:
        return jsonify({"ok": False, "error": "Feedback cannot be empty."}), 400
    if len(feedback_text) > 5000:
        return jsonify({"ok": False, "error": "Feedback is too long (max 5000 characters)."}), 400

    timestamp = datetime.now(ZoneInfo("America/New_York")).isoformat()
    remote_addr = request.headers.get("X-Forwarded-For") or request.remote_addr or "unknown"
    user_agent = request.headers.get("User-Agent", "unknown")
    entry = (
        f"[{timestamp}] ip={remote_addr}\n"
        f"user_agent={user_agent}\n"
        f"feedback={feedback_text}\n"
        "-----\n"
    )
    try:
        os.makedirs(FEEDBACK_DIR, exist_ok=True)
        with _feedback_lock:
            with open(FEEDBACK_FILE, "a", encoding="utf-8") as fh:
                fh.write(entry)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Failed to save feedback: {exc}"}), 500
    return jsonify({"ok": True})


@app.get("/tactics")
def tactics():
    """Render the tactics whiteboard page."""
    return render_template("tactics.html")


@app.get("/players")
def players():
    """Render the players/top scorers page."""
    return render_template("players.html")


@app.get("/api/scorers")
def api_scorers():
    """Return current season top scorers by competition."""
    if not os.path.exists(TOP_SCORERS_FILE):
        return jsonify({"ok": False, "error": "Scorers data not available", "competitions": {}}), 404
    
    try:
        with open(TOP_SCORERS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Could not load scorers: {exc}", "competitions": {}}), 500
    
    competitions = data.get("competitions", {})
    last_updated = data.get("last_updated_utc", "Unknown")
    
    return jsonify({
        "ok": True,
        "last_updated_utc": last_updated,
        "competitions": competitions,
        "available_leagues": sorted(competitions.keys()),
    })


@app.get("/graphics/<path:filename>")
def serve_graphic(filename):
    """Serve assets from Website/graphics for logos and other static artwork."""
    return send_from_directory(GRAPHICS_DIR, filename)


if __name__ == "__main__":
    import argparse
    import socket

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=5000, help="Port to bind to")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable Flask debug mode (with auto-reload; spawns a reloader process)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable Werkzeug file-change reloader (requires --debug)",
    )
    args = parser.parse_args()

    if args.reload and not args.debug:
        raise SystemExit("--reload requires --debug")

    use_reloader = bool(args.debug and args.reload)

    threading.Thread(target=_live_score_poller_loop, daemon=True, name="live-score-poller").start()
    print("[startup] Live score poller started (90-second interval, smart-comp filtering).")

    if args.host == "0.0.0.0":
        try:
            s = socket.socket(socket.AF_INET, socket.sock_dgram)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            print(f"\n * Connect from other devices at: http://{ip}:{args.port}\n")
        except Exception:
            pass

    app.run(
        host=args.host,
        port=args.port,
        debug=args.debug,
        use_reloader=use_reloader,
    )
