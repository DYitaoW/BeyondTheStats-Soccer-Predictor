"""
Predict upcoming Extra Leagues fixtures — same workflow as global / MLS.

For smaller European competitions that don't have enough data for a standalone
model; reuses the extra-leagues model cache and outputs predictions to
``Data/Predictions/``.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import joblib
import pandas as pd

import Predict_Match as pm


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_DIR = os.path.dirname(BASE_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import season_calendar
RAW_DATA_DIR = os.path.join(BASE_DIR, "Data", "Raw_Data")
GLOBAL_RAW_DATA_DIR = os.path.join(os.path.dirname(BASE_DIR), "Data", "Raw_Data")
PREDICTIONS_DIR = os.path.join(BASE_DIR, "Data", "Predictions")
PREDICTIONS_FILE = os.path.join(PREDICTIONS_DIR, "upcoming_matchweek_predictions.csv")

ESPN_SCOREBOARD_API = "https://site.api.espn.com/apis/site/v2/sports/soccer/{espn_id}/scoreboard"
EASTERN_TZ = ZoneInfo("America/New_York")

# Extra-league competitions with ESPN scoreboard coverage for full-season fixtures.
EXTRA_ESPN_COMPETITIONS = {
    "Argentina/Primera Division": "arg.1",
    "Brazil/Serie A": "bra.1",
    "Japan/J1 League": "jpn.1",
    "Netherlands/Eredivisie": "ned.1",
    "Belgium/First Division A": "bel.1",
    "Scotland/Premiership": "sco.1",
    "Turkey/Super Lig": "tur.1",
}

# All competitions handled by the extra-leagues pipeline.
# Data lives in either Extra-leagues/Data/Raw_Data/ or (for European leagues)
# the global Data/Raw_Data/ directory, which is scanned at prediction time.
EXTRA_COMPETITIONS = frozenset({
    "Argentina/Primera Division",
    "Brazil/Serie A",
    "Japan/J1 League",
    "Netherlands/Eredivisie",
    "Austria/Bundesliga",
    "Greece/Super League",
    "Norway/Eliteserien",
    "Romania/Liga I",
    "Sweden/Allsvenskan",
    "Belgium/First Division A",
    "Turkey/Super Lig",
    "Scotland/Premiership",
    "Poland/Ekstraklasa",
})


def rebuild_model_cache_once():
    """Rebuild the extra-leagues model cache in non-interactive mode."""
    import subprocess

    predict_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Predict_Match.py")
    proc = subprocess.run(
        [sys.executable, predict_script, "--build-cache-only"],
        cwd=BASE_DIR,
        text=True,
        capture_output=True,
        check=False,
        timeout=3600,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        message = stderr or stdout or f"exit code {proc.returncode}"
        raise RuntimeError(f"Auto-rebuild of model cache failed: {message}")


# Allow import of global pipeline modules (UEFA_Data_Manager, team mapping sync, etc.)
_GLOBAL_FILES_DIR = os.path.join(os.path.dirname(BASE_DIR), "files")
if _GLOBAL_FILES_DIR not in sys.path:
    sys.path.insert(0, _GLOBAL_FILES_DIR)

import Predict_Upcoming_Matchweek as global_upcoming
import UEFA_Data_Manager as uefa

RESULT_COLUMNS = [
    "prediction_key",
    "created_at_utc",
    "match_date",
    "match_datetime_utc",
    "competition",
    "home_team",
    "away_team",
    "display_home_team",
    "display_away_team",
    "unmapped_teams",
    "is_neutral_site",
    "schedule_only",
    "prediction_quality",
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
    "actual_home_goals",
    "actual_away_goals",
    "actual_result",
    "is_correct",
    "settled_at_utc",
]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Predict upcoming fixtures for extra leagues based on raw CSV schedules."
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=365,
        help="Lookahead window in days for upcoming fixtures (default: full season). Short windows (<90 days) extend through the next Tuesday.",
    )
    return parser.parse_args()


def _normalize_team_key(name):
    if not name:
        return ""
    text = str(name).strip().lower()
    text = text.replace("&", "and")
    return "".join(ch for ch in text if ch.isalnum())


def make_prediction_key(match_date, competition, home_team, away_team):
    date_key = match_date.strftime("%Y-%m-%d")
    home_key = _normalize_team_key(home_team)
    away_key = _normalize_team_key(away_team)
    team_pair = sorted([home_key, away_key])
    return f"{date_key}|{competition}|{team_pair[0]}|{team_pair[1]}"


def parse_date(value):
    date_value = pd.to_datetime(value, dayfirst=True, format="mixed", errors="coerce")
    if pd.isna(date_value):
        return None
    return date_value.normalize()


def calculate_fixture_window_end(window_days, start_date=None):
    # Anchor the window to today so extra-league pulls reflect the current slate.
    today = pd.Timestamp(start_date or datetime.utcnow().date()).normalize()
    window_days = max(0, int(window_days))

    # Full-season windows include every remaining scheduled fixture.
    if window_days >= 90:
        return today + pd.Timedelta(days=window_days)

    min_window_end = today + pd.Timedelta(days=window_days)
    # Short windows extend through the next Tuesday to keep Friday-to-Tuesday blocks together.
    days_to_tuesday = (1 - today.weekday()) % 7
    if days_to_tuesday == 0:
        days_to_tuesday = 7
    next_tuesday = today + pd.Timedelta(days=days_to_tuesday)
    return max(min_window_end, next_tuesday)


def latest_raw_file_per_competition(raw_root):
    latest = {}
    for root, _, files in os.walk(raw_root):
        for name in files:
            if not name.endswith(".csv"):
                continue
            start_year = pm.parse_season_start_year(name)
            if start_year is None:
                continue
            full_path = os.path.join(root, name)
            rel_path = os.path.relpath(full_path, raw_root)
            competition = os.path.dirname(rel_path).replace("\\", "/") or "Unknown"
            current = latest.get(competition)
            if current is None or start_year > current[0]:
                latest[competition] = (start_year, full_path)
    return {comp: path for comp, (_, path) in latest.items()}


def build_context():
    matches, season_files = pm.load_training_matches(pm.PROCESSED_DIR)
    if not os.path.exists(pm.MODEL_CACHE):
        print("[model-cache] cache missing; rebuilding model cache...")
        rebuild_model_cache_once()

    try:
        # Ensure custom wrapper class is resolvable when cache was pickled from __main__.
        setattr(sys.modules.get("__main__"), "AveragedProbaClassifier", pm.AveragedProbaClassifier)
    except Exception:
        pass

    try:
        bundle = joblib.load(pm.MODEL_CACHE)
    except Exception as exc:
        print(f"[model-cache] failed to load cache ({exc.__class__.__name__}); rebuilding...")
        rebuild_model_cache_once()
        bundle = joblib.load(pm.MODEL_CACHE)
    if bundle.get("fingerprint") != pm.data_fingerprint(season_files):
        print("[model-cache] using cached models (data newer than cache; full retrain runs Tue/Fri)")

    required_keys = {
        "clf",
        "result_label_encoder",
        "home_goal_reg",
        "away_goal_reg",
        "home_shot_reg",
        "away_shot_reg",
        "home_sot_reg",
        "away_sot_reg",
        "train_columns",
        "goal_prob_models",
    }
    missing = sorted(k for k in required_keys if k not in bundle)
    if missing:
        print(f"[model-cache] cache missing required fields ({', '.join(missing)}); rebuilding...")
        if os.path.exists(pm.MODEL_CACHE):
            os.remove(pm.MODEL_CACHE)
        rebuild_model_cache_once()
        bundle = joblib.load(pm.MODEL_CACHE)

    overall_teams = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "overall_teams.json"))
    season_teams = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "season_teams.json"))
    head_to_head = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "head_to_head.json"))
    current_form = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "current_form.json"))
    league_strength = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "league_strength.json")) or {}
    market_value = pm.load_json_if_exists(
        os.path.join(pm.TEAM_DATA_DIR, "team_top_market_value_players.json")
    ) or {}
    dynamic_form = pm.build_dynamic_form_from_matches(matches)

    if (
        overall_teams is None
        or season_teams is None
        or head_to_head is None
        or current_form is None
        or not isinstance(overall_teams, dict)
        or len(overall_teams) == 0
    ):
        overall_teams, season_teams, head_to_head, current_form = pm.build_fallback_data(
            matches, season_files
        )

    overall_teams = pm.replace_nan_with_sentinel(overall_teams)
    season_teams = pm.replace_nan_with_sentinel(season_teams)
    head_to_head = pm.replace_nan_with_sentinel(head_to_head)
    current_form = pm.replace_nan_with_sentinel(current_form)
    league_strength = pm.replace_nan_with_sentinel(league_strength)

    if not isinstance(current_form, dict):
        current_form = {"teams": {}}
    current_form.setdefault("teams", {})
    for team, stats in dynamic_form.items():
        if team not in current_form["teams"] or not isinstance(current_form["teams"].get(team), dict):
            current_form["teams"][team] = stats
            continue
        existing = current_form["teams"][team]
        for key, value in stats.items():
            if key not in existing or existing.get(key) in (None, "", 0, 0.0):
                existing[key] = value

    team_comp_map = {}
    for _, row in matches.iterrows():
        team_comp_map[row["HomeTeam"]] = row["competition"]
        team_comp_map[row["AwayTeam"]] = row["competition"]

    latest_start_year = max(pm.parse_start_year_from_key(k) for k in season_teams.keys())
    latest_season = season_files[-1].replace(".csv", "")
    goal_prob_models = bundle["goal_prob_models"]
    available = sorted(set(matches["HomeTeam"].dropna()) | set(matches["AwayTeam"].dropna()))

    ctx = {
        "clf": bundle["clf"],
        "result_le": bundle["result_label_encoder"],
        "home_goal_reg": bundle["home_goal_reg"],
        "away_goal_reg": bundle["away_goal_reg"],
        "home_shot_reg": bundle["home_shot_reg"],
        "away_shot_reg": bundle["away_shot_reg"],
        "home_sot_reg": bundle["home_sot_reg"],
        "away_sot_reg": bundle["away_sot_reg"],
        "goal_prob_models": goal_prob_models,
        "train_columns": bundle["train_columns"],
        "overall_teams": overall_teams,
        "season_teams": season_teams,
        "head_to_head": head_to_head,
        "current_form": current_form,
        "league_strength": league_strength,
        "market_value": market_value,
        "team_comp_map": team_comp_map,
        "latest_start": latest_start_year,
        "latest_season": latest_season,
        "available_teams": available,
    }

    uefa.build_uefa_context(ctx)
    return ctx


def latest_season_for_competition(season_teams, competition, fallback):
    competition = str(competition or "").strip()
    if not competition:
        return fallback
    best_key = None
    best_year = -1
    prefix = f"{competition}/"
    for season_key in season_teams.keys():
        if not str(season_key).startswith(prefix):
            continue
        year = pm.parse_start_year_from_key(season_key)
        if year > best_year:
            best_year = year
            best_key = season_key
    return best_key or fallback


def choose_prediction_season(home_team, away_team, competition, match_date, season_teams, fallback_season):
    competition_latest = latest_season_for_competition(season_teams, competition, fallback_season)
    fixture_year = -1
    if match_date is not None:
        try:
            fixture_year = int(pd.Timestamp(match_date).year)
        except Exception:
            fixture_year = -1
    if fixture_year > 0:
        competition_latest = season_calendar.season_key_for_fixture_year(
            competition, competition_latest, fixture_year
        )
    if home_team in season_teams.get(competition_latest, {}) and away_team in season_teams.get(
        competition_latest, {}
    ):
        return competition_latest
    return pm.choose_season_for_teams(home_team, away_team, season_teams, competition_latest)


def inject_fallback_team(team_name, competition, season_key, context):
    """Ensure *team_name* exists in all context structures; fallback to UEFA
    data or minimal placeholders when it is not in the training data."""
    overall = context["overall_teams"]
    season_teams = context["season_teams"]
    available = context["available_teams"]

    if team_name in overall:
        if team_name not in available:
            available.append(team_name)
        if team_name not in season_teams.get(season_key, {}):
            season_teams.setdefault(season_key, {})[team_name] = {"games": 0, "points": 0}
        return

    # Try UEFA data
    uefa_data = None
    if "uefa_coefficients" in context:
        uefa_data = uefa.lookup_team_data_for_fallback(
            team_name,
            context.get("uefa_coefficients"),
            context.get("uefa_team_registry"),
            context.get("uefa_squad_values"),
            context.get("uefa_domestic_tables"),
        )
    if uefa_data and uefa_data["league"] is not None:
        real_league = uefa_data["league"]
        context.setdefault("league_strength", {})[real_league] = uefa_data["league_strength"]
        context.setdefault("_uefa_team_league", {})[team_name] = real_league
        if uefa_data["squad_value_eur_m"] is not None:
            context.setdefault("uefa_squad_values", {})[team_name] = uefa_data["squad_value_eur_m"]

        ls = uefa_data["league_strength"]
        value_scale = uefa.squad_value_scale_factor(uefa_data.get("squad_value_eur_m"))
        scale = max(0.6, min(1.2, ls / 0.85)) * value_scale

        domestic = uefa_data.get("domestic")
        if domestic:
            played = max(1, domestic.get("played", 20))
            pts = domestic.get("points", 28)
            gf = domestic.get("goals_for", 27)
            ga = domestic.get("goals_against", 27)
            ppg = pts / played
            avg_gf = gf / played
            avg_ga = ga / played
        else:
            ppg = 1.2 * scale
            avg_gf = 1.35 * scale
            avg_ga = 1.35 * scale

        overall[team_name] = {
            "avg_gf": round(avg_gf, 4), "avg_ga": round(avg_ga, 4),
            "avg_shots": round(11.0 * scale, 4), "avg_sot": round(4.5 * scale, 4),
            "avg_home_gf": round(1.45 * scale, 4), "avg_home_ga": round(1.20 * scale, 4),
            "avg_away_gf": round(1.20 * scale, 4), "avg_away_ga": round(1.45 * scale, 4),
        }
        context["team_comp_map"][team_name] = real_league
    else:
        # Minimal fallback
        overall[team_name] = {
            "avg_gf": 1.2, "avg_ga": 1.2,
            "avg_shots": 10.0, "avg_sot": 4.0,
            "avg_home_gf": 1.3, "avg_home_ga": 1.1,
            "avg_away_gf": 1.1, "avg_away_ga": 1.3,
        }

    season_teams.setdefault(season_key, {})[team_name] = {"games": 0, "points": 0}
    if team_name not in available:
        available.append(team_name)


def predict_fixture(ctx, home_raw, away_raw, competition_hint, match_date=None):
    if pm.is_placeholder_team(home_raw) or pm.is_placeholder_team(away_raw):
        return None

    home_team = pm.resolve_team_name(home_raw, ctx["available_teams"])
    away_team = pm.resolve_team_name(away_raw, ctx["available_teams"])

    competition_fallback = latest_season_for_competition(
        ctx["season_teams"], competition_hint, ctx["latest_season"]
    )
    prediction_season = choose_prediction_season(
        home_team or home_raw,
        away_team or away_raw,
        competition_hint,
        match_date,
        ctx["season_teams"],
        competition_fallback,
    )
    season_key = prediction_season

    # Resolve / inject fallback data for unknown teams
    if not home_team:
        home_team = home_raw.strip()
        inject_fallback_team(home_team, competition_hint, season_key, ctx)
    if not away_team:
        away_team = away_raw.strip()
        inject_fallback_team(away_team, competition_hint, season_key, ctx)
    if home_team == away_team:
        return None

    competition_key = os.path.dirname(prediction_season).replace("\\", "/") or "Unknown"
    feature_competition = competition_hint or competition_key
    prediction_start_year = pm.parse_start_year_from_key(prediction_season)
    season_coeff = pm.season_recency_coefficient(ctx["latest_start"], prediction_start_year)

    # Use real league from UEFA data when available
    _uefa_leagues = ctx.get("_uefa_team_league", {})
    home_uefa_league = _uefa_leagues.get(home_team)
    away_uefa_league = _uefa_leagues.get(away_team)
    home_comp = home_uefa_league or ctx["team_comp_map"].get(home_team, feature_competition)
    away_comp = away_uefa_league or ctx["team_comp_map"].get(away_team, feature_competition)

    if home_uefa_league or away_uefa_league:
        ls = ctx.get("league_strength", {})
        home_ls = ls.get(home_uefa_league, 0.50) if home_uefa_league else 0.50
        away_ls = ls.get(away_uefa_league, 0.50) if away_uefa_league else 0.50
        effective_ls = max(home_ls, away_ls)
        season_coeff = min(season_coeff, max(effective_ls, 0.50))

    X_match = pm.build_features(
        pm.build_match_input(home_team, away_team),
        prediction_season,
        feature_competition,
        season_coeff,
        ctx["overall_teams"],
        ctx["season_teams"],
        ctx["head_to_head"],
        ctx["current_form"],
        ctx["league_strength"],
        home_competition_override=home_comp,
        away_competition_override=away_comp,
    )
    X_match = pd.get_dummies(X_match, columns=["competition"], dtype=float)
    X_match = X_match.reindex(columns=ctx["train_columns"], fill_value=0.0)

    probabilities = {"H": 0.0, "D": 0.0, "A": 0.0}
    proba_values = ctx["clf"].predict_proba(X_match)[0]
    for idx, enc in enumerate(ctx["clf"].classes_):
        label = ctx["result_le"].inverse_transform([enc])[0]
        probabilities[label] = float(proba_values[idx])

    probabilities = pm.reduce_draw_probability(probabilities)
    seed = pm.prediction_randomizer_seed(home_team, away_team, feature_competition, prediction_season)
    max_delta = getattr(pm, "EU_RANDOMIZER_MAX_DELTA", None)
    if max_delta is None:
        max_delta = getattr(pm, "MLS_RANDOMIZER_MAX_DELTA", 0.12)
    probabilities = pm.apply_probability_randomizer(probabilities, max_delta, seed=seed)

    prediction = max(probabilities, key=probabilities.get)
    home_goals = max(0.0, float(ctx["home_goal_reg"].predict(X_match)[0]))
    away_goals = max(0.0, float(ctx["away_goal_reg"].predict(X_match)[0]))

    # Compute Poisson-based goal probabilities from raw expected goals
    goal_probs = pm.predict_goal_probabilities(X_match, ctx["goal_prob_models"])

    # Align predicted scores to match the most likely result
    aligned_home, aligned_away = pm.align_predicted_score(home_goals, away_goals, prediction)

    home_shots = max(0.0, float(ctx["home_shot_reg"].predict(X_match)[0]))
    away_shots = max(0.0, float(ctx["away_shot_reg"].predict(X_match)[0]))
    home_sot = max(0.0, float(ctx["home_sot_reg"].predict(X_match)[0]))
    away_sot = max(0.0, float(ctx["away_sot_reg"].predict(X_match)[0]))

    competition_display = home_comp if home_comp == away_comp else f"{home_comp} vs {away_comp}"
    return {
        "home_team": home_team,
        "away_team": away_team,
        "display_home_team": home_team,
        "display_away_team": away_team,
        "unmapped_teams": "",
        "is_neutral_site": "0",
        "schedule_only": "0",
        "prediction_quality": "prediction",
        "competition": competition_display,
        "predicted_result": prediction,
        "prob_home": round(probabilities["H"], 6),
        "prob_draw": round(probabilities["D"], 6),
        "prob_away": round(probabilities["A"], 6),
        "pred_home_goals": aligned_home,
        "pred_away_goals": aligned_away,
        "pred_home_shots": round(home_shots, 3),
        "pred_away_shots": round(away_shots, 3),
        "pred_home_sot": round(home_sot, 3),
        "pred_away_sot": round(away_sot, 3),
        "probability_reasoning": "",
        **goal_probs,
    }


def upcoming_fixtures_from_raw(raw_path, window_days, competition_name):
    try:
        df = pd.read_csv(raw_path)
    except Exception:
        return pd.DataFrame()
    required = {"Date", "Home", "Away", "HG", "AG", "Res"}
    if not required.issubset(df.columns):
        return pd.DataFrame()

    work = df.copy()
    work["DateParsed"] = work["Date"].apply(parse_date)
    work = work[work["Home"].notna() & work["Away"].notna()]
    work = work[work["DateParsed"].notna()]
    if work.empty:
        return work

    def is_played(row):
        res = str(row.get("Res", "")).strip().upper()
        hg = row.get("HG")
        ag = row.get("AG")
        if res in {"H", "D", "A"}:
            return True
        try:
            return pd.notna(hg) and pd.notna(ag) and float(hg) >= 0 and float(ag) >= 0
        except (TypeError, ValueError):
            return False

    work = work[~work.apply(is_played, axis=1)].copy()
    if work.empty:
        return work

    today = pd.Timestamp(datetime.now(UTC).date())
    future = work[work["DateParsed"] >= today]
    if future.empty:
        future = work.copy()

    bounded = season_calendar.filter_fixtures_to_bounds(
        future.assign(match_date=future["DateParsed"]),
        competition_name,
        reference_date=today,
        date_column="match_date",
    )
    return bounded.rename(columns={"match_date": "DateParsed"}).copy()


def fetch_json(url, timeout=30):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def upcoming_fixtures_from_espn(competition, window_days, lookahead_days=None):
    espn_id = EXTRA_ESPN_COMPETITIONS.get(competition)
    if not espn_id:
        return pd.DataFrame()

    today = pd.Timestamp(datetime.now(UTC).date())
    if lookahead_days is None:
        lookahead_days = season_calendar.espn_scan_day_count(competition, reference_date=today)
    lookahead_days = min(int(lookahead_days), int(window_days))
    cutoff_end = season_calendar.fixture_search_bounds(competition, reference_date=today)[1]
    rows = []
    seen = set()

    for offset in range(0, max(1, int(lookahead_days) + 1)):
        day = today + pd.Timedelta(days=offset)
        if day > cutoff_end:
            break
        url = ESPN_SCOREBOARD_API.format(espn_id=espn_id) + f"?dates={day.strftime('%Y%m%d')}"
        try:
            data = fetch_json(url, timeout=30)
        except Exception:
            continue

        for event in data.get("events", []) or []:
            event_date = pd.to_datetime(event.get("date"), utc=True, errors="coerce")
            if pd.isna(event_date):
                continue
            event_dt_et = event_date.tz_convert(EASTERN_TZ)
            match_date = event_dt_et.tz_localize(None).normalize()
            if match_date < today or match_date > cutoff_end:
                continue

            competitions = event.get("competitions", [])
            if not competitions:
                continue
            comp0 = competitions[0] or {}
            status_state = (
                ((comp0.get("status") or {}).get("type") or {}).get("state", "")
            ).strip().lower()
            if status_state and status_state not in {"pre"}:
                continue

            home_team = ""
            away_team = ""
            for competitor in comp0.get("competitors", []) or []:
                team_name = ((competitor.get("team") or {}).get("displayName") or "").strip()
                side = str(competitor.get("homeAway", "")).strip().lower()
                if side == "home":
                    home_team = team_name
                elif side == "away":
                    away_team = team_name
            if not home_team or not away_team:
                continue

            key = (match_date.strftime("%Y-%m-%d"), home_team, away_team)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "DateParsed": match_date,
                    "Home": home_team,
                    "Away": away_team,
                }
            )

    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    return frame.sort_values(["DateParsed", "Home", "Away"]).reset_index(drop=True)


def merge_fixture_frames(*frames):
    valid = [frame for frame in frames if frame is not None and not frame.empty]
    if not valid:
        return pd.DataFrame()
    merged = pd.concat(valid, ignore_index=True)
    merged["DateParsed"] = pd.to_datetime(merged["DateParsed"], errors="coerce").dt.normalize()
    merged = merged[merged["DateParsed"].notna() & merged["Home"].notna() & merged["Away"].notna()]
    merged = merged.drop_duplicates(subset=["DateParsed", "Home", "Away"], keep="first")
    return merged.sort_values(["DateParsed", "Home", "Away"]).reset_index(drop=True)


def _mapping_context(ctx):
    return {
        "available_teams": ctx.get("available_teams", []),
        "team_competition_map": ctx.get("team_comp_map", {}),
        "season_teams": ctx.get("season_teams", {}),
    }


def main():
    args = parse_args()
    all_latest = {}
    for root in [RAW_DATA_DIR, GLOBAL_RAW_DATA_DIR]:
        all_latest.update(latest_raw_file_per_competition(root))
    latest = {comp: p for comp, p in all_latest.items() if comp in EXTRA_COMPETITIONS}
    if not latest:
        raise ValueError(f"No raw season files found for extra-league competitions in {RAW_DATA_DIR} or {GLOBAL_RAW_DATA_DIR}")

    ctx = build_context()
    mapping_ctx = _mapping_context(ctx)
    fixture_frames = []
    for competition, path in sorted(latest.items()):
        raw_fixtures = upcoming_fixtures_from_raw(path, args.window_days, competition)
        espn_fixtures = upcoming_fixtures_from_espn(competition, args.window_days)
        merged = merge_fixture_frames(raw_fixtures, espn_fixtures)
        if merged.empty:
            continue
        frame = merged.copy()
        frame["competition"] = competition
        frame["home_team"] = frame["Home"].astype(str).str.strip()
        frame["away_team"] = frame["Away"].astype(str).str.strip()
        frame["match_date"] = frame["DateParsed"]
        fixture_frames.append(frame[["match_date", "competition", "home_team", "away_team"]])

    if not fixture_frames:
        print("No upcoming extra-league fixtures found.")
        return

    fixtures = pd.concat(fixture_frames, ignore_index=True)
    team_mapping = global_upcoming.load_shared_mapping()
    team_mapping, _canonical_added = global_upcoming.ensure_canonical_self_mappings(team_mapping, mapping_ctx)
    team_mapping, _new_entries, _drift = global_upcoming.update_team_mapping_from_fixtures(
        fixtures, mapping_ctx, team_mapping
    )
    global_upcoming.save_team_mapping(global_upcoming.TEAM_MAPPING_FILE, team_mapping)
    fixtures = global_upcoming.apply_team_mapping_to_fixtures(fixtures, team_mapping, mapping_ctx)

    created_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for _, row in fixtures.iterrows():
        match_date = pd.Timestamp(row["match_date"]).date()
        mapped_home = str(row.get("mapped_home_team", "")).strip()
        mapped_away = str(row.get("mapped_away_team", "")).strip()
        raw_home = str(row.get("home_team", "")).strip()
        raw_away = str(row.get("away_team", "")).strip()
        display_home = str(row.get("display_home_team", "")).strip() or raw_home
        display_away = str(row.get("display_away_team", "")).strip() or raw_away
        competition = str(row.get("competition", "")).strip()
        if not raw_home or not raw_away:
            continue
        if mapped_home and mapped_away:
            home = mapped_home
            away = mapped_away
            pred = predict_fixture(ctx, home, away, competition, match_date)
            if pred is None:
                continue
            unmapped_teams = ""
            schedule_only = "0"
            prediction_quality = pred.get("prediction_quality", "prediction")
            predicted_result = pred["predicted_result"]
            prob_home = pred["prob_home"]
            prob_draw = pred["prob_draw"]
            prob_away = pred["prob_away"]
            pred_home_goals = pred["pred_home_goals"]
            pred_away_goals = pred["pred_away_goals"]
            pred_home_shots = pred["pred_home_shots"]
            pred_away_shots = pred["pred_away_shots"]
            pred_home_sot = pred["pred_home_sot"]
            pred_away_sot = pred["pred_away_sot"]
            probability_reasoning = pred.get("probability_reasoning", "")
            goal_prob_fields = {k: pred[k] for k in ["prob_home_goals_0","prob_home_goals_1plus","prob_home_goals_2plus","prob_away_goals_0","prob_away_goals_1plus","prob_away_goals_2plus","prob_both_score","prob_over_1_5","prob_over_2_5","prob_over_3_5"] if k in pred}
        else:
            home = raw_home
            away = raw_away
            unmapped = []
            if not mapped_home:
                unmapped.append(raw_home)
            if not mapped_away:
                unmapped.append(raw_away)
            unmapped_teams = ",".join(unmapped)
            schedule_only = "1"
            prediction_quality = "no_prediction"
            predicted_result = ""
            prob_home = 0.0
            prob_draw = 0.0
            prob_away = 0.0
            pred_home_goals = None
            pred_away_goals = None
            pred_home_shots = None
            pred_away_shots = None
            pred_home_sot = None
            pred_away_sot = None
            probability_reasoning = "Teams are not in the model database — fixture listed without odds."
            goal_prob_fields = {k: None for k in ["prob_home_goals_0","prob_home_goals_1plus","prob_home_goals_2plus","prob_away_goals_0","prob_away_goals_1plus","prob_away_goals_2plus","prob_both_score","prob_over_1_5","prob_over_2_5","prob_over_3_5"]}
        rows.append(
            {
                "prediction_key": make_prediction_key(match_date, competition, home, away),
                "created_at_utc": created_at,
                "match_date": match_date.strftime("%Y-%m-%d"),
                "match_datetime_utc": "",
                "competition": competition,
                "home_team": home,
                "away_team": away,
                "display_home_team": display_home,
                "display_away_team": display_away,
                "unmapped_teams": unmapped_teams,
                "is_neutral_site": "0",
                "schedule_only": schedule_only,
                "prediction_quality": prediction_quality,
                "predicted_result": predicted_result,
                "prob_home": prob_home,
                "prob_draw": prob_draw,
                "prob_away": prob_away,
                "pred_home_goals": pred_home_goals,
                "pred_away_goals": pred_away_goals,
                "pred_home_shots": pred_home_shots,
                "pred_away_shots": pred_away_shots,
                "pred_home_sot": pred_home_sot,
                "pred_away_sot": pred_away_sot,
                "probability_reasoning": probability_reasoning,
                **goal_prob_fields,
                "actual_home_goals": "",
                "actual_away_goals": "",
                "actual_result": "",
                "is_correct": "",
                "settled_at_utc": "",
            }
        )

    out = pd.DataFrame(rows, columns=RESULT_COLUMNS).astype("object")
    if not out.empty:
        out = out.sort_values(["match_date", "competition", "home_team", "away_team"])

    os.makedirs(PREDICTIONS_DIR, exist_ok=True)
    out.to_csv(PREDICTIONS_FILE, index=False)
    print(f"Saved upcoming extra-league predictions: {PREDICTIONS_FILE}")
    print(f"Rows: {len(out)}")


if __name__ == "__main__":
    main()
