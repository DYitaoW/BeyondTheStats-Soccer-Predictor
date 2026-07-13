"""
ML model training + prediction for Extra Leagues (lower-tier European).

Near-identical copy of ``files/Predict_Match.py`` but for smaller/less
data-rich competitions (e.g. Belgian Pro League, Scottish Premiership).
Same architecture, uses ExtraTreesClassifier fallback.
"""
import json
import os
import re
import hashlib
import random
import math
import time
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime

import joblib
import pandas as pd
from sklearn.ensemble import ExtraTreesClassifier, RandomForestClassifier, RandomForestRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.linear_model import LogisticRegression

try:
    from xgboost import XGBClassifier, XGBRegressor
except ImportError:
    XGBClassifier = None
    XGBRegressor = None


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROCESSED_DIR = os.path.join(BASE_DIR, "Data", "Processed_Data")
TEAM_DATA_DIR = os.path.join(BASE_DIR, "Data", "Team_Data")
MODEL_CACHE = os.path.join(TEAM_DATA_DIR, "model_cache.pkl")
SEASON_PATTERN = re.compile(r"^(?:[a-z0-9]+stat)(\d{4})(?:-(\d{2}))?\.csv$", re.IGNORECASE)
MAPPING_FILE = os.path.join(os.path.dirname(BASE_DIR), "Data", "Predictions", "team_name_mapping_master.json")
_name_mapping_cache = None
MIN_START_YEAR = 2002

# MLS tuning: higher parity than major European leagues with a meaningful home edge.
MLS_HOME_EDGE_SHIFT = 0.045
MLS_DRAW_TARGET = 0.27
MLS_DRAW_BLEND = 0.20
MLS_ADJUSTMENT_WEIGHT = 0.45
MLS_MARKET_SHIFT_MAX = 0.075
MLS_MARKET_SHIFT_SCALE = 0.10
MLS_MARKET_POSITION_WEIGHTS = {
    "attack": 2.05,
    "midfield": 0.95,
    "defense": 0.55,
    "goalkeeper": 0.5,
    "unknown": 1.0,
}
MLS_RANDOMIZER_MAX_DELTA = 0.16
MLS_HOME_ADV_MIN_SHIFT = 0.03
MLS_HOME_ADV_MAX_SHIFT = 0.13
MLS_HOME_EXTRA_BOOST = 0.012
MLS_DRAW_REDUCTION_FACTOR = 0.10
MLS_HIGH_DRAW_THRESHOLD = 0.42
MLS_HIGH_DRAW_EXTRA_REDUCTION_MAX = 0.20
CPU_COUNT = max(1, (os.cpu_count() or 1))
TRAIN_WORKERS = int(os.getenv("SOCCER_TRAIN_WORKERS", str(max(1, min(4, CPU_COUNT // 2)))))
MODEL_THREADS = int(os.getenv("SOCCER_MODEL_THREADS", str(max(1, CPU_COUNT // TRAIN_WORKERS))))


def align_predicted_score(pred_home_goals: float, pred_away_goals: float, prediction: str,
                          max_iter: int = 10) -> tuple[int, int]:
    """
    Adjust predicted goals so the rounded score matches the predicted outcome.

    Args:
        pred_home_goals: Raw predicted home goals (float)
        pred_away_goals: Raw predicted away goals (float)
        prediction: "H" (home win), "D" (draw), or "A" (away win)
        max_iter: Maximum adjustment iterations

    Returns:
        (home_goals_int, away_goals_int) where the rounded score matches the prediction.
    """
    h = max(0.0, pred_home_goals)
    a = max(0.0, pred_away_goals)

    def score_matches(hi, ai):
        if hi > ai:
            return "H"
        if hi == ai:
            return "D"
        return "A"

    for _ in range(max_iter):
        hi = int(round(h))
        ai = int(round(a))
        if score_matches(hi, ai) == prediction:
            return hi, ai
        # Nudge the raw goals toward the needed outcome
        if prediction == "H" and hi <= ai:
            h += 0.15
            a = max(0.0, a - 0.05)
        elif prediction == "A" and hi >= ai:
            a += 0.15
            h = max(0.0, h - 0.05)
        elif prediction == "D":
            if hi > ai:
                h -= 0.1
                a += 0.1
            elif hi < ai:
                h += 0.1
                a -= 0.1
            else:
                break
        else:
            break
    # Fallback: force minimal valid score
    hi = int(round(h))
    ai = int(round(a))
    if prediction == "H" and hi <= ai:
        hi = ai + 1
    elif prediction == "A" and hi >= ai:
        ai = hi + 1
    elif prediction == "D" and hi != ai:
        ai = hi
    return max(0, hi), max(0, ai)


def compute_goal_probabilities(lambda_home: float, lambda_away: float) -> dict:
    """Compute Poisson-based goal event probabilities from expected goals (lambdas).

    Returns a dict with keys:
      prob_home_goals_0, prob_home_goals_1plus, prob_home_goals_2plus,
      prob_away_goals_0, prob_away_goals_1plus, prob_away_goals_2plus,
      prob_both_score, prob_over_1_5, prob_over_2_5, prob_over_3_5
    """
    eps = 1e-9
    lh = max(lambda_home, eps)
    la = max(lambda_away, eps)

    def poisson_pmf(k, lam):
        return math.exp(-lam) * (lam ** k) / math.factorial(k)

    p_h0 = poisson_pmf(0, lh)
    p_h1 = poisson_pmf(1, lh)
    p_h2 = poisson_pmf(2, lh)
    p_a0 = poisson_pmf(0, la)
    p_a1 = poisson_pmf(1, la)
    p_a2 = poisson_pmf(2, la)

    prob_home_goals_0 = round(p_h0, 6)
    prob_home_goals_1plus = round(1.0 - p_h0, 6)
    prob_home_goals_2plus = round(1.0 - p_h0 - p_h1, 6)
    prob_away_goals_0 = round(p_a0, 6)
    prob_away_goals_1plus = round(1.0 - p_a0, 6)
    prob_away_goals_2plus = round(1.0 - p_a0 - p_a1, 6)
    prob_both_score = round((1.0 - p_h0) * (1.0 - p_a0), 6)

    p_over_1_5 = 1.0
    for h in range(0, 3):
        for a in range(0, 3):
            if h + a <= 1:
                p_over_1_5 -= poisson_pmf(h, lh) * poisson_pmf(a, la)
    p_over_2_5 = 1.0
    for h in range(0, 4):
        for a in range(0, 4):
            if h + a <= 2:
                p_over_2_5 -= poisson_pmf(h, lh) * poisson_pmf(a, la)
    p_over_3_5 = 1.0
    for h in range(0, 5):
        for a in range(0, 5):
            if h + a <= 3:
                p_over_3_5 -= poisson_pmf(h, lh) * poisson_pmf(a, la)

    return {
        "prob_home_goals_0": prob_home_goals_0,
        "prob_home_goals_1plus": prob_home_goals_1plus,
        "prob_home_goals_2plus": prob_home_goals_2plus,
        "prob_away_goals_0": prob_away_goals_0,
        "prob_away_goals_1plus": prob_away_goals_1plus,
        "prob_away_goals_2plus": prob_away_goals_2plus,
        "prob_both_score": prob_both_score,
        "prob_over_1_5": round(p_over_1_5, 6),
        "prob_over_2_5": round(p_over_2_5, 6),
        "prob_over_3_5": round(p_over_3_5, 6),
    }


def sample_score_from_probs(prob_home: float, prob_draw: float, prob_away: float,
                            base_home_xg: float, base_away_xg: float,
                            prediction: str, rng: np.random.Generator) -> tuple[int, int]:
    """
    Sample a realistic score from Poisson distributions calibrated to win probabilities.
    """
    eps = 1e-6
    ph = max(prob_home, eps)
    pa = max(prob_away, eps)
    pd = max(prob_draw, eps)

    base_total = base_home_xg + base_away_xg
    if base_total <= 0:
        base_total = 2.6

    strength_ratio = ph / pa if pa > 0 else 3.0
    strength_ratio = max(0.2, min(5.0, strength_ratio))

    draw_factor = 1.0 + pd * 0.5
    lambda_h = base_total * strength_ratio / (strength_ratio + 1.0) * draw_factor
    lambda_a = base_total / (strength_ratio + 1.0) * draw_factor

    lambda_h *= 1.05

    max_attempts = 50
    for _ in range(max_attempts):
        hg = int(rng.poisson(lambda_h))
        ag = int(rng.poisson(lambda_a))
        hg = max(0, min(hg, 6))
        ag = max(0, min(ag, 6))

        if (prediction == "H" and hg > ag) or \
           (prediction == "A" and ag > hg) or \
           (prediction == "D" and hg == ag):
            return hg, ag

    hg, ag = int(round(base_home_xg)), int(round(base_away_xg))
    if prediction == "H" and hg <= ag:
        hg = ag + 1
    elif prediction == "A" and ag <= hg:
        ag = hg + 1
    elif prediction == "D" and hg != ag:
        ag = hg
    return max(0, hg), max(0, ag)


def load_json(path):
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def save_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False)


def load_json_if_exists(path):
    if not os.path.exists(path):
        return None
    try:
        return load_json(path)
    except Exception:
        return None


def is_invalid_stat_value(value):
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except Exception:
        pass
    return value == -1


def replace_nan_with_sentinel(value, sentinel=-1):
    if isinstance(value, dict):
        return {k: replace_nan_with_sentinel(v, sentinel=sentinel) for k, v in value.items()}
    if isinstance(value, list):
        return [replace_nan_with_sentinel(item, sentinel=sentinel) for item in value]
    try:
        if pd.isna(value):
            return sentinel
    except Exception:
        pass
    return value


def clean_stats_dict(stats):
    if not isinstance(stats, dict):
        return {}
    cleaned = {}
    for key, value in stats.items():
        if is_invalid_stat_value(value):
            continue
        cleaned[key] = value
    return cleaned


def coerce_feature_value(value, default=0.0):
    if is_invalid_stat_value(value):
        return float(default)
    return float(value)


def data_fingerprint(season_files, processed_dir=PROCESSED_DIR):
    digest = hashlib.sha256()
    for rel_path in season_files:
        digest.update(rel_path.encode("utf-8"))
        full_path = os.path.join(processed_dir, rel_path)
        try:
            stat = os.stat(full_path)
            digest.update(str(stat.st_mtime_ns).encode("utf-8"))
            digest.update(str(stat.st_size).encode("utf-8"))
        except OSError:
            digest.update(b"missing")
    return digest.hexdigest()


def per_game(stats, total_key, games_key):
    games = stats.get(games_key, 0)
    total = stats.get(total_key, 0)
    if is_invalid_stat_value(games) or is_invalid_stat_value(total):
        return 0.0
    if not games:
        return 0.0
    return total / games


def parse_season_start_year(file_name):
    match = SEASON_PATTERN.match(file_name)
    if not match:
        return None

    start_year = int(match.group(1))
    end_part = match.group(2)

    if end_part:
        expected_end = (start_year + 1) % 100
        if int(end_part) != expected_end:
            return None

    if start_year < MIN_START_YEAR:
        return None
    if start_year > datetime.now().year:
        return None

    return start_year


def parse_start_year_from_key(season_key):
    file_name = os.path.basename(season_key) + ".csv"
    start = parse_season_start_year(file_name)
    return -1 if start is None else start


def season_recency_coefficient(latest_start_year, season_start_year):
    age = max(0, latest_start_year - season_start_year)
    if age <= 0:
        return 1.00
    if age == 1:
        return 0.80
    if age == 2:
        return 0.55
    if age == 3:
        return 0.35
    if age == 4:
        return 0.22
    return 0.12


def choose_season_for_teams(home_team, away_team, season_teams, fallback_season_key):
    candidates = []
    for season_key, teams in season_teams.items():
        if home_team in teams and away_team in teams:
            candidates.append(season_key)

    if not candidates:
        return fallback_season_key

    candidates.sort(key=parse_start_year_from_key)
    return candidates[-1]


def find_latest_team_season_stats(team, season_teams):
    latest_key = None
    latest_year = -1
    for season_key, teams in season_teams.items():
        if team not in teams:
            continue
        year = parse_start_year_from_key(season_key)
        if year > latest_year:
            latest_year = year
            latest_key = season_key
    if latest_key is None:
        return {}
    return season_teams.get(latest_key, {}).get(team, {})


def team_form(team, current_form):
    stats = clean_stats_dict(current_form.get("teams", {}).get(team, {}))
    points_10 = float(stats.get("points_last_10", 0.0) or 0.0)
    wins_10 = float(stats.get("wins_last_10", 0.0) or 0.0)
    losses_10 = float(stats.get("losses_last_10", 0.0) or 0.0)
    avg_for_10 = float(stats.get("avg_goals_for_last_10", 0.0) or 0.0)
    avg_against_10 = float(stats.get("avg_goals_against_last_10", 0.0) or 0.0)
    ppg_10 = points_10 / 10.0
    form_index = (wins_10 - losses_10) / 10.0
    return ppg_10, form_index, avg_for_10, avg_against_10


def build_season_position_map(season_lookup):
    if not isinstance(season_lookup, dict):
        return {}
    teams = list(season_lookup.keys())
    ranked = sorted(
        teams,
        key=lambda t: (
            -float(season_lookup.get(t, {}).get("points", 0.0) or 0.0),
            -float(season_lookup.get(t, {}).get("goal_difference", 0.0) or 0.0),
            -float(season_lookup.get(t, {}).get("goals_scored", 0.0) or 0.0),
            t,
        ),
    )
    return {team: idx + 1 for idx, team in enumerate(ranked)}


def split_season_key(season_key):
    if "/" not in season_key:
        return "Unknown", season_key
    parts = season_key.split("/")
    return "/".join(parts[:-1]), parts[-1]


def build_latest_competition_tables(season_teams):
    latest_by_comp = {}
    for season_key in season_teams.keys():
        competition, _ = split_season_key(season_key)
        year = parse_start_year_from_key(season_key)
        current = latest_by_comp.get(competition)
        if current is None or year > current[0]:
            latest_by_comp[competition] = (year, season_key)

    tables = {}
    for competition, (_, season_key) in latest_by_comp.items():
        season_lookup = season_teams.get(season_key, {})
        positions = build_season_position_map(season_lookup)
        tables[competition] = {
            "season_key": season_key,
            "positions": positions,
            "size": len(positions),
        }
    return tables


def build_competition_offsets(league_strength, competition_tables):
    competitions = list(competition_tables.keys())
    competitions.sort(
        key=lambda comp: (-float(league_strength.get(comp, 0.85)), comp)
    )
    offsets = {}
    running = 0
    for comp in competitions:
        offsets[comp] = running
        running += int(competition_tables.get(comp, {}).get("size", 0))
    return offsets


# League-strength coefficients are now static (set during initial Sort_Data.py run and never recalculated).
# Previously refresh_league_strength_coefficients() was defined here.


def build_dynamic_form_from_matches(matches):
    ordered_rows = []
    for idx, row in matches.iterrows():
        season_year = parse_start_year_from_key(row["season_key"])
        ordered_rows.append((season_year, idx, row))
    ordered_rows.sort(key=lambda item: (item[0], item[1]))

    team_history = defaultdict(list)

    for _, _, row in ordered_rows:
        home = row["HomeTeam"]
        away = row["AwayTeam"]
        hg = float(row["FTHG"])
        ag = float(row["FTAG"])
        result = row["FTR"]

        avg_h = float(row.get("AvgH", 0.0) or 0.0)
        avg_d = float(row.get("AvgD", 0.0) or 0.0)
        avg_a = float(row.get("AvgA", 0.0) or 0.0)

        if result == "H":
            home_res, away_res = "W", "L"
        elif result == "A":
            home_res, away_res = "L", "W"
        else:
            home_res, away_res = "D", "D"

        team_history[home].append(
            {"result": home_res, "gf": hg, "ga": ag, "win_odds": avg_h, "draw_odds": avg_d, "lose_odds": avg_a}
        )
        team_history[away].append(
            {"result": away_res, "gf": ag, "ga": hg, "win_odds": avg_a, "draw_odds": avg_d, "lose_odds": avg_h}
        )

    dynamic_form = {}
    for team, history in team_history.items():
        recent = history[-10:]
        wins = sum(1 for match in recent if match["result"] == "W")
        draws = sum(1 for match in recent if match["result"] == "D")
        losses = sum(1 for match in recent if match["result"] == "L")
        points = wins * 3 + draws
        gf = sum(match["gf"] for match in recent)
        ga = sum(match["ga"] for match in recent)
        n = len(recent) if recent else 1
        last = history[-1]
        dynamic_form[team] = {
            "form_last_10": "".join(match["result"] for match in recent),
            "wins_last_10": wins,
            "draws_last_10": draws,
            "losses_last_10": losses,
            "points_last_10": points,
            "avg_goals_for_last_10": round(gf / n, 2),
            "avg_goals_against_last_10": round(ga / n, 2),
            "previous_match_win_odds": last["win_odds"],
            "previous_match_draw_odds": last["draw_odds"],
            "previous_match_lose_odds": last["lose_odds"],
        }

    return dynamic_form


def build_fallback_data(matches, season_files):
    overall_teams = defaultdict(
        lambda: {
            "games": 0,
            "goals_scored": 0,
            "goals_conceded": 0,
            "home_games": 0,
            "away_games": 0,
            "home_goals_scored": 0,
            "away_goals_scored": 0,
        }
    )
    season_teams = defaultdict(lambda: defaultdict(lambda: {"games": 0, "points": 0}))
    head_to_head = defaultdict(
        lambda: defaultdict(lambda: {"games": 0, "wins": 0, "goals_scored": 0, "goals_conceded": 0})
    )

    for _, row in matches.iterrows():
        season_key = row["season_key"]
        home = row["HomeTeam"]
        away = row["AwayTeam"]
        hg = float(row["FTHG"])
        ag = float(row["FTAG"])
        result = row["FTR"]

        overall_teams[home]["games"] += 1
        overall_teams[home]["goals_scored"] += hg
        overall_teams[home]["goals_conceded"] += ag
        overall_teams[home]["home_games"] += 1
        overall_teams[home]["home_goals_scored"] += hg

        overall_teams[away]["games"] += 1
        overall_teams[away]["goals_scored"] += ag
        overall_teams[away]["goals_conceded"] += hg
        overall_teams[away]["away_games"] += 1
        overall_teams[away]["away_goals_scored"] += ag

        season_teams[season_key][home]["games"] += 1
        season_teams[season_key][away]["games"] += 1

        head_to_head[home][away]["games"] += 1
        head_to_head[home][away]["goals_scored"] += hg
        head_to_head[home][away]["goals_conceded"] += ag
        head_to_head[away][home]["games"] += 1
        head_to_head[away][home]["goals_scored"] += ag
        head_to_head[away][home]["goals_conceded"] += hg

        if result == "H":
            season_teams[season_key][home]["points"] += 3
            head_to_head[home][away]["wins"] += 1
        elif result == "A":
            season_teams[season_key][away]["points"] += 3
            head_to_head[away][home]["wins"] += 1
        else:
            season_teams[season_key][home]["points"] += 1
            season_teams[season_key][away]["points"] += 1

    for _, stats in overall_teams.items():
        games = max(stats["games"], 1)
        stats["avg_goals_scored"] = stats["goals_scored"] / games
        stats["avg_goals_conceded"] = stats["goals_conceded"] / games

    latest_key = season_files[-1].replace(".csv", "")
    current_form = {"season": latest_key, "teams": {}}
    latest_matches = matches[matches["season_key"] == latest_key]
    last_odds = {}
    for _, row in latest_matches.iterrows():
        home = row["HomeTeam"]
        away = row["AwayTeam"]
        last_odds[home] = {
            "previous_match_win_odds": float(row.get("AvgH", 0.0) or 0.0),
            "previous_match_draw_odds": float(row.get("AvgD", 0.0) or 0.0),
            "previous_match_lose_odds": float(row.get("AvgA", 0.0) or 0.0),
        }
        last_odds[away] = {
            "previous_match_win_odds": float(row.get("AvgA", 0.0) or 0.0),
            "previous_match_draw_odds": float(row.get("AvgD", 0.0) or 0.0),
            "previous_match_lose_odds": float(row.get("AvgH", 0.0) or 0.0),
        }
    current_form["teams"] = last_odds

    return (
        dict(overall_teams),
        {k: dict(v) for k, v in season_teams.items()},
        {k: dict(v) for k, v in head_to_head.items()},
        current_form,
    )


def build_features(
    match_df,
    season_key,
    competition_key,
    season_coeff,
    overall_teams,
    season_teams,
    head_to_head,
    current_form,
    league_strength,
    home_competition_override=None,
    away_competition_override=None,
):
    season_lookup = season_teams.get(season_key, {})
    season_positions = build_season_position_map(season_lookup)
    form_lookup = current_form.get("teams", {})
    rows = []
    previous_odds = {}
    default_strength = float(league_strength.get(competition_key, 0.85))

    for _, match in match_df.iterrows():
        home = match["HomeTeam"]
        away = match["AwayTeam"]

        home_overall = clean_stats_dict(overall_teams.get(home, {}))
        away_overall = clean_stats_dict(overall_teams.get(away, {}))
        home_season = clean_stats_dict(season_lookup.get(home, {}))
        away_season = clean_stats_dict(season_lookup.get(away, {}))
        if not home_season:
            home_season = clean_stats_dict(find_latest_team_season_stats(home, season_teams))
        if not away_season:
            away_season = clean_stats_dict(find_latest_team_season_stats(away, season_teams))
        h2h_home = clean_stats_dict(head_to_head.get(home, {}).get(away, {}))
        home_competition = home_competition_override or competition_key
        away_competition = away_competition_override or competition_key
        home_strength = float(league_strength.get(home_competition, default_strength))
        away_strength = float(league_strength.get(away_competition, default_strength))
        home_points_before = match.get("HomePointsBefore", home_season.get("points", 0.0))
        away_points_before = match.get("AwayPointsBefore", away_season.get("points", 0.0))
        home_pos_before = match.get("HomeLeaguePosBefore", season_positions.get(home, 0.0))
        away_pos_before = match.get("AwayLeaguePosBefore", season_positions.get(away, 0.0))
        home_points_before = coerce_feature_value(home_points_before, 0.0)
        away_points_before = coerce_feature_value(away_points_before, 0.0)
        home_pos_before = coerce_feature_value(home_pos_before, 0.0)
        away_pos_before = coerce_feature_value(away_pos_before, 0.0)

        home_prev = previous_odds.get(home)
        away_prev = previous_odds.get(away)
        home_ppg10, home_form_idx, home_form_gf10, home_form_ga10 = team_form(home, current_form)
        away_ppg10, away_form_idx, away_form_gf10, away_form_ga10 = team_form(away, current_form)

        if home_prev is None:
            form_home = clean_stats_dict(form_lookup.get(home, {}))
            home_prev = {
                "win": float(form_home.get("previous_match_win_odds") or 0.0),
                "draw": float(form_home.get("previous_match_draw_odds") or 0.0),
                "lose": float(form_home.get("previous_match_lose_odds") or 0.0),
            }

        if away_prev is None:
            form_away = clean_stats_dict(form_lookup.get(away, {}))
            away_prev = {
                "win": float(form_away.get("previous_match_win_odds") or 0.0),
                "draw": float(form_away.get("previous_match_draw_odds") or 0.0),
                "lose": float(form_away.get("previous_match_lose_odds") or 0.0),
            }

        avg_h = match.get("AvgH", 0.0)
        avg_d = match.get("AvgD", 0.0)
        avg_a = match.get("AvgA", 0.0)

        avg_h = 0.0 if pd.isna(avg_h) else float(avg_h)
        avg_d = 0.0 if pd.isna(avg_d) else float(avg_d)
        avg_a = 0.0 if pd.isna(avg_a) else float(avg_a)

        rows.append(
            {
                "home_overall_avg_goals": home_overall.get("avg_goals_scored", 0.0),
                "away_overall_avg_goals": away_overall.get("avg_goals_scored", 0.0),
                "home_overall_avg_conceded": home_overall.get("avg_goals_conceded", 0.0),
                "away_overall_avg_conceded": away_overall.get("avg_goals_conceded", 0.0),
                "home_home_goals_per_game": per_game(home_overall, "home_goals_scored", "home_games"),
                "away_away_goals_per_game": per_game(away_overall, "away_goals_scored", "away_games"),
                "home_avg_home_goals_scored": home_overall.get("avg_home_goals_scored", 0.0),
                "home_avg_home_goals_conceded": home_overall.get("avg_home_goals_conceded", 0.0),
                "away_avg_away_goals_scored": away_overall.get("avg_away_goals_scored", 0.0),
                "away_avg_away_goals_conceded": away_overall.get("avg_away_goals_conceded", 0.0),
                "home_avg_home_shots_for": home_overall.get("avg_home_shots_for", 0.0),
                "home_avg_home_shots_against": home_overall.get("avg_home_shots_against", 0.0),
                "away_avg_away_shots_for": away_overall.get("avg_away_shots_for", 0.0),
                "away_avg_away_shots_against": away_overall.get("avg_away_shots_against", 0.0),
                "home_season_points_per_game": per_game(home_season, "points", "games"),
                "away_season_points_per_game": per_game(away_season, "points", "games"),
                "home_season_avg_goals_scored": home_season.get("avg_goals_scored", 0.0),
                "home_season_avg_goals_conceded": home_season.get("avg_goals_conceded", 0.0),
                "away_season_avg_goals_scored": away_season.get("avg_goals_scored", 0.0),
                "away_season_avg_goals_conceded": away_season.get("avg_goals_conceded", 0.0),
                "home_season_avg_home_goals_scored": home_season.get("avg_home_goals_scored", 0.0),
                "home_season_avg_home_goals_conceded": home_season.get("avg_home_goals_conceded", 0.0),
                "away_season_avg_away_goals_scored": away_season.get("avg_away_goals_scored", 0.0),
                "away_season_avg_away_goals_conceded": away_season.get("avg_away_goals_conceded", 0.0),
                "home_season_avg_home_shots_for": home_season.get("avg_home_shots_for", 0.0),
                "home_season_avg_home_shots_against": home_season.get("avg_home_shots_against", 0.0),
                "away_season_avg_away_shots_for": away_season.get("avg_away_shots_for", 0.0),
                "away_season_avg_away_shots_against": away_season.get("avg_away_shots_against", 0.0),
                "h2h_home_win_rate": per_game(h2h_home, "wins", "games"),
                "h2h_goal_diff_per_game": per_game(h2h_home, "goals_scored", "games")
                - per_game(h2h_home, "goals_conceded", "games"),
                "h2h_weighted_goal_diff": (
                    h2h_home.get("weighted_avg_goals_scored", 0.0) - h2h_home.get("weighted_avg_goals_conceded", 0.0)
                ),
                "h2h_weighted_shot_diff": (
                    h2h_home.get("weighted_avg_shots_for", 0.0) - h2h_home.get("weighted_avg_shots_against", 0.0)
                ),
                "home_prev_win_odds": home_prev["win"],
                "home_prev_draw_odds": home_prev["draw"],
                "home_prev_lose_odds": home_prev["lose"],
                "away_prev_win_odds": away_prev["win"],
                "away_prev_draw_odds": away_prev["draw"],
                "away_prev_lose_odds": away_prev["lose"],
                "home_league_strength": home_strength,
                "away_league_strength": away_strength,
                "league_strength_diff": home_strength - away_strength,
                "home_points_before": home_points_before,
                "away_points_before": away_points_before,
                "points_before_diff": home_points_before - away_points_before,
                "home_league_pos_before": home_pos_before,
                "away_league_pos_before": away_pos_before,
                "league_pos_before_diff": away_pos_before - home_pos_before,
                "season_coeff": season_coeff,
                "home_adj_points_per_game": per_game(home_season, "points", "games") * home_strength * season_coeff,
                "away_adj_points_per_game": per_game(away_season, "points", "games") * away_strength * season_coeff,
                "home_adj_avg_goals_scored": home_overall.get("weighted_avg_goals_scored", home_overall.get("avg_goals_scored", 0.0))
                * home_strength,
                "away_adj_avg_goals_scored": away_overall.get("weighted_avg_goals_scored", away_overall.get("avg_goals_scored", 0.0))
                * away_strength,
                "home_form_ppg_10": home_ppg10,
                "away_form_ppg_10": away_ppg10,
                "form_ppg_diff_10": home_ppg10 - away_ppg10,
                "home_form_index_10": home_form_idx,
                "away_form_index_10": away_form_idx,
                "form_index_diff_10": home_form_idx - away_form_idx,
                "home_form_avg_goals_for_10": home_form_gf10,
                "away_form_avg_goals_for_10": away_form_gf10,
                "home_form_avg_goals_against_10": home_form_ga10,
                "away_form_avg_goals_against_10": away_form_ga10,
                "competition": competition_key,
            }
        )

        previous_odds[home] = {"win": avg_h, "draw": avg_d, "lose": avg_a}
        previous_odds[away] = {"win": avg_a, "draw": avg_d, "lose": avg_h}

    return pd.DataFrame(rows)


def load_training_matches(processed_dir):
    frames = []
    valid_files = []

    for root, _, files in os.walk(processed_dir):
        for name in files:
            if not name.endswith(".csv"):
                continue
            start_year = parse_season_start_year(name)
            if start_year is not None:
                rel_path = os.path.relpath(os.path.join(root, name), processed_dir)
                valid_files.append((start_year, rel_path))

    valid_files.sort(key=lambda item: item[0])
    season_files = [name for _, name in valid_files]
    latest_start_year = valid_files[-1][0] if valid_files else MIN_START_YEAR

    for start_year, rel_path in valid_files:
        path = os.path.join(processed_dir, rel_path)
        df = pd.read_csv(path)

        for col in ["AvgH", "AvgD", "AvgA"]:
            if col not in df.columns:
                df[col] = 0.0
        for col in ["HomePointsBefore", "AwayPointsBefore", "HomeLeaguePosBefore", "AwayLeaguePosBefore"]:
            if col not in df.columns:
                df[col] = 0.0

        df = df[
            [
                "HomeTeam",
                "AwayTeam",
                "FTR",
                "FTHG",
                "FTAG",
                "HS",
                "AS",
                "HST",
                "AST",
                "AvgH",
                "AvgD",
                "AvgA",
                "HomePointsBefore",
                "AwayPointsBefore",
                "HomeLeaguePosBefore",
                "AwayLeaguePosBefore",
            ]
        ].dropna(subset=["HomeTeam", "AwayTeam", "FTR", "FTHG", "FTAG"])

        for shot_col in ["HS", "AS", "HST", "AST"]:
            df[shot_col] = pd.to_numeric(df[shot_col], errors="coerce").fillna(-1)

        df["season_key"] = rel_path.replace(".csv", "")
        df["competition"] = os.path.dirname(rel_path).replace("\\", "/") or "Unknown"
        df["season_coeff"] = season_recency_coefficient(latest_start_year, start_year)
        frames.append(df)

    if not frames:
        raise ValueError("No season CSV files found in Data/Processed_Data.")

    return pd.concat(frames, ignore_index=True), season_files


def is_placeholder_team(name):
    text = str(name or "").lower().strip()
    if not text:
        return True
    tokens = [
        "group ", "winner", "runner", "third place", "round of",
        "quarterfinal", "quarter-final", "semifinal", "semi-final",
        "final", "playoff ", "qualifier", "tbd", "to be determined",
    ]
    return any(t in text for t in tokens)


def _load_name_mapping():
    global _name_mapping_cache
    if _name_mapping_cache is not None:
        return _name_mapping_cache
    if not os.path.exists(MAPPING_FILE):
        _name_mapping_cache = {}
        return _name_mapping_cache
    try:
        with open(MAPPING_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        _name_mapping_cache = {}
        return _name_mapping_cache
    if not isinstance(raw, dict):
        _name_mapping_cache = {}
        return _name_mapping_cache
    flat = {}
    for comp, entries in raw.items():
        if not isinstance(entries, dict):
            continue
        for display_name, canonical_name in entries.items():
            if display_name and canonical_name:
                flat[display_name.strip().lower()] = canonical_name.strip()
    _name_mapping_cache = flat
    return flat


def resolve_team_name(raw_name, valid_names):
    if not raw_name:
        return None

    def normalize(name):
        name = name.lower().strip()

        name = name.replace("&", "and")
        name = re.sub(r"[^a-z0-9]+", "", name)
        return name

    alias_map = {normalize(team): team for team in valid_names}

    manual_aliases = {
        "manutd": "Man United",
        "manchesterunited": "Man United",
        "mancity": "Man City",
        "manchestercity": "Man City",
        "spurs": "Tottenham",
        "tottenhamhotspur": "Tottenham",
        "wolves": "Wolves",
        "wolverhampton": "Wolves",
        "wolverhamptonwanderers": "Wolves",
        "newcastleutd": "Newcastle",
        "newcastleunited": "Newcastle",
        "nottinghamforest": "Nott'm Forest",
        "nottmforest": "Nott'm Forest",
    }

    for alias_key, canonical in manual_aliases.items():
        if canonical in valid_names:
            alias_map[alias_key] = canonical

    key = normalize(raw_name)
    direct = alias_map.get(key)
    if direct:
        return direct

    mapping = _load_name_mapping()
    mapped = mapping.get(raw_name.strip().lower())
    if mapped and mapped in valid_names:
        return mapped

    candidates = [team for team in valid_names if key and key in normalize(team)]
    if len(candidates) == 1:
        return candidates[0]

    return None


def build_match_input(home_team, away_team):
    return pd.DataFrame(
        [{"HomeTeam": home_team, "AwayTeam": away_team, "FTR": "D", "AvgH": 0.0, "AvgD": 0.0, "AvgA": 0.0}]
    )

def probabilities_to_odds(probabilities):
    odds = {}
    for key in ["H", "D", "A"]:
        p = max(float(probabilities.get(key, 0.0)), 1e-6)
        odds[key] = round(1.0 / p, 2)
    return odds


def format_percent_text(probability):
    pct = max(0.0, float(probability)) * 100.0
    if 0.0 < pct < 1.0:
        return "<1%"
    return f"{pct:.1f}%"


def prediction_randomizer_seed(home_team, away_team, competition_key, season_key=""):
    payload = "||".join(
        [
            str(home_team).strip().lower(),
            str(away_team).strip().lower(),
            str(competition_key).strip().lower(),
            str(season_key).strip().lower(),
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def apply_probability_randomizer(probabilities, max_delta, seed=None):
    h = max(0.0, float(probabilities.get("H", 0.0)))
    d = max(0.0, float(probabilities.get("D", 0.0)))
    a = max(0.0, float(probabilities.get("A", 0.0)))
    total = h + d + a
    if total <= 0:
        return {"H": 0.0, "D": 0.0, "A": 0.0}
    h /= total
    d /= total
    a /= total

    rng = random.Random(seed) if seed is not None else random
    delta = rng.uniform(-max_delta, max_delta)
    h = max(0.0, min(1.0, h + delta))
    a = max(0.0, min(1.0, a - delta))
    rem = max(0.0, 1.0 - (h + a))
    d_target = max(0.0, min(1.0, d + rng.uniform(-max_delta * 0.35, max_delta * 0.35)))
    d = min(rem, d_target)
    spill = max(0.0, rem - d)
    denom = h + a
    if denom > 0:
        h += spill * (h / denom)
        a += spill * (a / denom)
    else:
        h = spill * 0.5
        a = spill * 0.5
    norm = h + d + a
    if norm <= 0:
        return {"H": 1 / 3, "D": 1 / 3, "A": 1 / 3}
    return {"H": h / norm, "D": d / norm, "A": a / norm}


def reduce_draw_probability(probabilities, reduction_factor=MLS_DRAW_REDUCTION_FACTOR):
    h = max(0.0, float(probabilities.get("H", 0.0)))
    d = max(0.0, float(probabilities.get("D", 0.0)))
    a = max(0.0, float(probabilities.get("A", 0.0)))
    total = h + d + a
    if total <= 0:
        return {"H": 1 / 3, "D": 1 / 3, "A": 1 / 3}
    h /= total
    d /= total
    a /= total

    r = max(0.0, min(0.6, float(reduction_factor)))
    if d > MLS_HIGH_DRAW_THRESHOLD:
        # Flatten very high draw outputs to avoid unrealistic tie-heavy predictions.
        extra_ratio = min(1.0, (d - MLS_HIGH_DRAW_THRESHOLD) / max(1e-6, (1.0 - MLS_HIGH_DRAW_THRESHOLD)))
        r += MLS_HIGH_DRAW_EXTRA_REDUCTION_MAX * extra_ratio
    r = min(0.6, r)
    reduced_d = d * (1.0 - r)
    carry = d - reduced_d
    d = reduced_d
    h_a_total = h + a
    if h_a_total > 0:
        h += carry * (h / h_a_total)
        a += carry * (a / h_a_total)
    else:
        h += carry * 0.5
        a += carry * 0.5

    norm = h + d + a
    if norm <= 0:
        return {"H": 1 / 3, "D": 1 / 3, "A": 1 / 3}
    return {"H": h / norm, "D": d / norm, "A": a / norm}


def apply_home_advantage_boost(probabilities, boost=MLS_HOME_EXTRA_BOOST):
    h = max(0.0, float(probabilities.get("H", 0.0)))
    d = max(0.0, float(probabilities.get("D", 0.0)))
    a = max(0.0, float(probabilities.get("A", 0.0)))
    total = h + d + a
    if total <= 0:
        return {"H": 1 / 3, "D": 1 / 3, "A": 1 / 3}
    h /= total
    d /= total
    a /= total

    transfer = min(max(0.0, float(boost)), a)
    h += transfer
    a -= transfer
    norm = h + d + a
    if norm <= 0:
        return {"H": 1 / 3, "D": 1 / 3, "A": 1 / 3}
    return {"H": h / norm, "D": d / norm, "A": a / norm}


def mls_home_advantage_shift(home_team, prediction_season, season_teams):
    season_lookup = season_teams.get(prediction_season, {}) if isinstance(season_teams, dict) else {}
    home_stats = clean_stats_dict(season_lookup.get(home_team, {})) or clean_stats_dict(
        find_latest_team_season_stats(home_team, season_teams)
    )
    home_gd = float(home_stats.get("avg_home_goals_scored", 0.0)) - float(home_stats.get("avg_home_goals_conceded", 0.0))
    shot_edge = float(home_stats.get("avg_home_shots_for", 0.0)) - float(home_stats.get("avg_home_shots_against", 0.0))
    home_strength_signal = home_gd + 0.03 * shot_edge
    form_factor = 0.5 + 0.5 * math.tanh(home_strength_signal)
    blend = 0.55 * random.random() + 0.45 * form_factor
    shift = MLS_HOME_ADV_MIN_SHIFT + (MLS_HOME_ADV_MAX_SHIFT - MLS_HOME_ADV_MIN_SHIFT) * blend
    return max(MLS_HOME_ADV_MIN_SHIFT, min(MLS_HOME_ADV_MAX_SHIFT, shift))


def market_value_team_score(team_name, market_value_data):
    teams = (market_value_data or {}).get("teams", {})
    team_entry = teams.get(team_name, {})
    players = team_entry.get("top_5_players", [])
    score = 0.0
    for player in players:
        value_eur = float(player.get("market_value_eur", 0) or 0)
        position_group = str(player.get("position_group", "unknown")).strip().lower()
        pos_weight = MLS_MARKET_POSITION_WEIGHTS.get(position_group, MLS_MARKET_POSITION_WEIGHTS["unknown"])
        score += value_eur * pos_weight
    return score


def apply_league_strength_adjustment(probabilities, home_strength, away_strength):
    h = max(0.0, float(probabilities.get("H", 0.0)))
    d = max(0.0, float(probabilities.get("D", 0.0)))
    a = max(0.0, float(probabilities.get("A", 0.0)))
    total = h + d + a
    if total <= 0:
        return {"H": 1 / 3, "D": 1 / 3, "A": 1 / 3}, 1.0, 0.0
    h /= total
    d /= total
    a /= total

    strength_delta = float(home_strength) - float(away_strength)
    strength_gap = abs(strength_delta)
    draw_scale = max(0.82, 1.0 - 0.35 * strength_gap)
    old_draw = d
    d = old_draw * draw_scale
    carry = old_draw - d
    h_a_total = h + a
    if h_a_total > 0:
        h += carry * (h / h_a_total)
        a += carry * (a / h_a_total)
    else:
        h += carry * 0.5
        a += carry * 0.5

    league_direction_shift = max(-0.035, min(0.035, 0.18 * strength_delta * MLS_ADJUSTMENT_WEIGHT))
    if league_direction_shift > 0:
        transfer = min(league_direction_shift, a)
        h += transfer
        a -= transfer
    elif league_direction_shift < 0:
        transfer = min(abs(league_direction_shift), h)
        a += transfer
        h -= transfer

    norm = h + d + a
    if norm <= 0:
        return {"H": 1 / 3, "D": 1 / 3, "A": 1 / 3}, draw_scale, league_direction_shift
    return {"H": h / norm, "D": d / norm, "A": a / norm}, draw_scale, league_direction_shift


def market_value_probability_shift(home_team, away_team, market_value_data):
    home_score = market_value_team_score(home_team, market_value_data)
    away_score = market_value_team_score(away_team, market_value_data)
    total = home_score + away_score
    if total <= 0:
        return 0.0, home_score, away_score
    # Compare weighted team totals directly to create the overall edge.
    edge = (home_score - away_score) / total
    shift = max(-MLS_MARKET_SHIFT_MAX, min(MLS_MARKET_SHIFT_MAX, MLS_MARKET_SHIFT_SCALE * edge))
    return shift, home_score, away_score


class AveragedProbaClassifier:
    def __init__(self, models):
        self.models = models
        self.classes_ = models[0].classes_

    def predict_proba(self, X):
        matrices = [model.predict_proba(X) for model in self.models]
        return sum(matrices) / len(matrices)

    def predict(self, X):
        avg = self.predict_proba(X)
        idx = avg.argmax(axis=1)
        return self.classes_[idx]


def train_result_model(X_train, y_train):
    label_encoder = LabelEncoder()
    y_train_enc = label_encoder.fit_transform(y_train)

    trained_models = []
    backends = []

    if XGBClassifier is not None:
        try:
            model = XGBClassifier(
                n_estimators=420,
                max_depth=7,
                learning_rate=0.05,
                subsample=0.9,
                colsample_bytree=0.9,
                objective="multi:softprob",
                num_class=len(label_encoder.classes_),
                eval_metric="mlogloss",
                random_state=42,
                tree_method="hist",
                device="cuda",
                n_jobs=MODEL_THREADS,
            )
            model.fit(X_train, y_train_enc)
            trained_models.append(model)
            backends.append("xgboost-gpu")
        except Exception:
            try:
                model = XGBClassifier(
                    n_estimators=420,
                    max_depth=7,
                    learning_rate=0.05,
                    subsample=0.9,
                    colsample_bytree=0.9,
                    objective="multi:softprob",
                    num_class=len(label_encoder.classes_),
                    eval_metric="mlogloss",
                    random_state=42,
                    tree_method="hist",
                    device="cpu",
                    n_jobs=MODEL_THREADS,
                )
                model.fit(X_train, y_train_enc)
                trained_models.append(model)
                backends.append("xgboost-cpu")
            except Exception:
                pass

    rf_model = RandomForestClassifier(n_estimators=260, random_state=42, n_jobs=MODEL_THREADS)
    rf_model.fit(X_train, y_train_enc)
    trained_models.append(rf_model)
    backends.append("random-forest")

    et_model = ExtraTreesClassifier(n_estimators=320, random_state=42, n_jobs=MODEL_THREADS)
    et_model.fit(X_train, y_train_enc)
    trained_models.append(et_model)
    backends.append("extra-trees")

    if len(trained_models) == 1:
        return trained_models[0], label_encoder, backends[0]

    ensemble = AveragedProbaClassifier(trained_models)
    return ensemble, label_encoder, "ensemble(" + "+".join(backends) + ")"


def train_regression_model(X_train, y_train, random_state):
    if XGBRegressor is not None:
        try:
            model = XGBRegressor(
                n_estimators=450,
                max_depth=8,
                learning_rate=0.05,
                subsample=0.9,
                colsample_bytree=0.9,
                objective="reg:squarederror",
                eval_metric="rmse",
                random_state=random_state,
                tree_method="hist",
                device="cuda",
                n_jobs=MODEL_THREADS,
            )
            model.fit(X_train, y_train)
            return model
        except Exception:
            model = XGBRegressor(
                n_estimators=450,
                max_depth=8,
                learning_rate=0.05,
                subsample=0.9,
                colsample_bytree=0.9,
                objective="reg:squarederror",
                eval_metric="rmse",
                random_state=random_state,
                tree_method="hist",
                device="cpu",
                n_jobs=MODEL_THREADS,
            )
            model.fit(X_train, y_train)
            return model

    model = RandomForestRegressor(n_estimators=160, random_state=random_state, n_jobs=MODEL_THREADS)
    model.fit(X_train, y_train)
    return model


def train_all_regressors(X, targets):
    results = {}
    with ThreadPoolExecutor(max_workers=TRAIN_WORKERS) as pool:
        future_map = {
            pool.submit(train_regression_model, X, series, seed): key
            for key, series, seed in targets
        }
        for future in as_completed(future_map):
            key = future_map[future]
            results[key] = future.result()
    return results


GOAL_PROB_TARGETS = [
    ("goal_prob_home_0", lambda hg, ag: (hg == 0).astype(int)),
    ("goal_prob_home_1plus", lambda hg, ag: (hg >= 1).astype(int)),
    ("goal_prob_home_2plus", lambda hg, ag: (hg >= 2).astype(int)),
    ("goal_prob_away_0", lambda hg, ag: (ag == 0).astype(int)),
    ("goal_prob_away_1plus", lambda hg, ag: (ag >= 1).astype(int)),
    ("goal_prob_away_2plus", lambda hg, ag: (ag >= 2).astype(int)),
    ("goal_prob_both_score", lambda hg, ag: ((hg > 0) & (ag > 0)).astype(int)),
    ("goal_prob_over_1_5", lambda hg, ag: ((hg + ag) > 1).astype(int)),
    ("goal_prob_over_2_5", lambda hg, ag: ((hg + ag) > 2).astype(int)),
    ("goal_prob_over_3_5", lambda hg, ag: ((hg + ag) > 3).astype(int)),
]

GOAL_PROB_RESULT_KEYS = [
    "prob_home_goals_0", "prob_home_goals_1plus", "prob_home_goals_2plus",
    "prob_away_goals_0", "prob_away_goals_1plus", "prob_away_goals_2plus",
    "prob_both_score", "prob_over_1_5", "prob_over_2_5", "prob_over_3_5",
]


def train_goal_prob_model(X_train, y_train, random_state):
    # Standardize features before fitting lbfgs to improve convergence, per
    # https://scikit-learn.org/stable/modules/preprocessing.html and
    # https://scikit-learn.org/stable/modules/linear_model.html#logistic-regression.
    model = make_pipeline(
        StandardScaler(),
        LogisticRegression(max_iter=2000, random_state=random_state),
    )
    model.fit(X_train, y_train)
    return model


def train_all_goal_prob_models(X, matches):
    hg = matches["FTHG"]
    ag = matches["FTAG"]
    models = {}
    for name, fn in GOAL_PROB_TARGETS:
        models[name] = train_goal_prob_model(X, fn(hg, ag), hash(name) % (2**31))
    return models


def predict_goal_probabilities(X_match, goal_prob_models):
    result = {}
    for (name, _), result_key in zip(GOAL_PROB_TARGETS, GOAL_PROB_RESULT_KEYS):
        model = goal_prob_models[name]
        proba = model.predict_proba(X_match)[0]
        result[result_key] = round(float(proba[1]), 6)
    return result


def main():
    matches, season_files = load_training_matches(PROCESSED_DIR)

    overall_teams = load_json_if_exists(os.path.join(TEAM_DATA_DIR, "overall_teams.json"))
    season_teams = load_json_if_exists(os.path.join(TEAM_DATA_DIR, "season_teams.json"))
    head_to_head = load_json_if_exists(os.path.join(TEAM_DATA_DIR, "head_to_head.json"))
    current_form = load_json_if_exists(os.path.join(TEAM_DATA_DIR, "current_form.json"))
    league_strength = load_json_if_exists(os.path.join(TEAM_DATA_DIR, "league_strength.json")) or {}
    market_value_data = load_json_if_exists(os.path.join(TEAM_DATA_DIR, "team_top_market_value_players.json")) or {}
    dynamic_form = build_dynamic_form_from_matches(matches)

    if (
        overall_teams is None
        or season_teams is None
        or head_to_head is None
        or current_form is None
        or not isinstance(overall_teams, dict)
        or len(overall_teams) == 0
    ):
        fallback = build_fallback_data(matches, season_files)
        overall_teams, season_teams, head_to_head, current_form = fallback

    # Convert any NaN values to -1 sentinel values.
    overall_teams = replace_nan_with_sentinel(overall_teams)
    season_teams = replace_nan_with_sentinel(season_teams)
    head_to_head = replace_nan_with_sentinel(head_to_head)
    current_form = replace_nan_with_sentinel(current_form)
    league_strength = replace_nan_with_sentinel(league_strength)

    if not isinstance(current_form, dict):
        current_form = {"teams": {}}
    if "teams" not in current_form or not isinstance(current_form["teams"], dict):
        current_form["teams"] = {}
    # Preserve current-form file data, fill missing fields from dynamic history.
    for team, stats in dynamic_form.items():
        if team not in current_form["teams"] or not isinstance(current_form["teams"].get(team), dict):
            current_form["teams"][team] = stats
            continue
        existing = current_form["teams"][team]
        for key, value in stats.items():
            if key not in existing or existing.get(key) in (None, "", 0, 0.0):
                existing[key] = value

    feature_frames = []
    for season_key, season_matches in matches.groupby("season_key", sort=False):
        feature_frames.append(
            build_features(
                season_matches,
                season_key,
                season_matches["competition"].iloc[0],
                float(season_matches["season_coeff"].iloc[0]),
                overall_teams,
                season_teams,
                head_to_head,
                current_form,
                league_strength,
            )
        )

    X = pd.concat(feature_frames, ignore_index=True)
    X = pd.get_dummies(X, columns=["competition"], dtype=float)
    train_columns = X.columns

    y_result = matches["FTR"].reset_index(drop=True)
    y_home_goals = matches["FTHG"].reset_index(drop=True)
    y_away_goals = matches["FTAG"].reset_index(drop=True)
    y_home_shots = matches["HS"].reset_index(drop=True)
    y_away_shots = matches["AS"].reset_index(drop=True)
    y_home_sot = matches["HST"].reset_index(drop=True)
    y_away_sot = matches["AST"].reset_index(drop=True)

    fingerprint = data_fingerprint(season_files)
    cache_bundle = None
    cache_valid = False
    if os.path.exists(MODEL_CACHE):
        try:
            cache_bundle = joblib.load(MODEL_CACHE)
            cache_valid = cache_bundle.get("fingerprint") == fingerprint
            if not cache_valid:
                bt = cache_bundle.get("build_time")
                age_h = (time.time() - bt) / 3600.0 if bt is not None else None
                age_s = f" (cache age {age_h:.1f}h)" if age_h is not None else ""
                import sys
                if "--build-cache-only" in sys.argv:
                    print(f"[model-cache] fingerprint mismatch{age_s}; retraining models...")
                else:
                    print(f"[model-cache] fingerprint mismatch{age_s}; using cached models (retrain runs Tue/Fri)")
                    cache_valid = True
        except Exception:
            cache_bundle = None
            cache_valid = False

    if cache_valid:
        clf = cache_bundle["clf"]
        result_label_encoder = cache_bundle["result_label_encoder"]
        home_goal_reg = cache_bundle["home_goal_reg"]
        away_goal_reg = cache_bundle["away_goal_reg"]
        home_shot_reg = cache_bundle["home_shot_reg"]
        away_shot_reg = cache_bundle["away_shot_reg"]
        home_sot_reg = cache_bundle["home_sot_reg"]
        away_sot_reg = cache_bundle["away_sot_reg"]
        goal_prob_models = cache_bundle.get("goal_prob_models")
        train_columns = cache_bundle["train_columns"]
        backend = cache_bundle.get("backend", "cached")
    else:
        clf, result_label_encoder, backend = train_result_model(X, y_result)
        regs = train_all_regressors(
            X,
            [
                ("home_goal_reg", y_home_goals, 42),
                ("away_goal_reg", y_away_goals, 43),
                ("home_shot_reg", y_home_shots, 44),
                ("away_shot_reg", y_away_shots, 45),
                ("home_sot_reg", y_home_sot, 46),
                ("away_sot_reg", y_away_sot, 47),
            ],
        )
        home_goal_reg = regs["home_goal_reg"]
        away_goal_reg = regs["away_goal_reg"]
        home_shot_reg = regs["home_shot_reg"]
        away_shot_reg = regs["away_shot_reg"]
        home_sot_reg = regs["home_sot_reg"]
        away_sot_reg = regs["away_sot_reg"]
        goal_prob_models = train_all_goal_prob_models(X, matches)
        import traceback
        try:
            joblib.dump(
                {
                    "fingerprint": fingerprint,
                    "build_time": time.time(),
                    "clf": clf,
                    "result_label_encoder": result_label_encoder,
                    "home_goal_reg": home_goal_reg,
                    "away_goal_reg": away_goal_reg,
                    "home_shot_reg": home_shot_reg,
                    "away_shot_reg": away_shot_reg,
                    "home_sot_reg": home_sot_reg,
                    "away_sot_reg": away_sot_reg,
                    "goal_prob_models": goal_prob_models,
                    "train_columns": train_columns,
                    "backend": backend,
                },
                MODEL_CACHE,
            )
        except Exception:
            traceback.print_exc()

    available_teams = sorted(set(matches["HomeTeam"].dropna()) | set(matches["AwayTeam"].dropna()))

    import sys
    if "--build-cache-only" in sys.argv:
        print(f"Model cache ready: {MODEL_CACHE} (backend={backend})")
        return

    print("\nMatch Predictor\n")
    print(f"Model backend: {backend}")
    print("Enter teams for prediction. Team 1 is always the home team.")
    print("Type 'q' to quit.\n")
    debug_input = input("Enable debug reasoning output? (y/n): ").strip().lower()
    debug_mode = debug_input in {"y", "yes", "1", "true"}
    print("")

    latest_season = season_files[-1].replace(".csv", "")
    team_competition_map = {}
    for _, row in matches.iterrows():
        team_competition_map[row["HomeTeam"]] = row["competition"]
        team_competition_map[row["AwayTeam"]] = row["competition"]
    competition_tables = build_latest_competition_tables(season_teams)
    competition_offsets = build_competition_offsets(league_strength, competition_tables)

    while True:
        team_1_raw = input("Team 1 (Home): ").strip()
        if team_1_raw.lower() in {"q", "quit", "exit"}:
            print("\nExiting predictor.")
            break

        team_2_raw = input("Team 2 (Away): ").strip()
        if team_2_raw.lower() in {"q", "quit", "exit"}:
            print("\nExiting predictor.")
            break

        home_team = resolve_team_name(team_1_raw, available_teams)
        away_team = resolve_team_name(team_2_raw, available_teams)

        if not home_team or not away_team:
            print("\nOne or both team names were not recognized.")
            print("Try names like: Arsenal, Man City, Man Utd, Spurs, Newcastle\n")
            continue
        if home_team == away_team:
            print("\nTeams must be different.\n")
            continue

        prediction_season = choose_season_for_teams(home_team, away_team, season_teams, latest_season)
        competition_key = os.path.dirname(prediction_season).replace("\\", "/") or "Unknown"
        prediction_start_year = parse_start_year_from_key(prediction_season)
        latest_start_year = max(parse_start_year_from_key(key) for key in season_teams.keys())
        prediction_season_coeff = season_recency_coefficient(latest_start_year, prediction_start_year)
        home_competition = team_competition_map.get(home_team, competition_key)
        away_competition = team_competition_map.get(away_team, competition_key)

        match_input = build_match_input(home_team, away_team)
        X_match = build_features(
            match_input,
            prediction_season,
            competition_key,
            prediction_season_coeff,
            overall_teams,
            season_teams,
            head_to_head,
            current_form,
            league_strength,
            home_competition_override=home_competition,
            away_competition_override=away_competition,
        )
        X_match = pd.get_dummies(X_match, columns=["competition"], dtype=float)
        X_match = X_match.reindex(columns=train_columns, fill_value=0.0)

        probabilities = {"H": 0.0, "D": 0.0, "A": 0.0}
        proba_values = clf.predict_proba(X_match)[0]
        for idx, encoded_label in enumerate(clf.classes_):
            label = result_label_encoder.inverse_transform([encoded_label])[0]
            probabilities[label] = float(proba_values[idx])
        raw_probabilities = dict(probabilities)

        home_league_strength = float(league_strength.get(home_competition, 0.85))
        away_league_strength = float(league_strength.get(away_competition, 0.85))
        strength_delta = home_league_strength - away_league_strength
        strength_gap = abs(strength_delta)

        probabilities, draw_scale, league_direction_shift = apply_league_strength_adjustment(
            probabilities, home_league_strength, away_league_strength
        )
        after_league_probabilities = dict(probabilities)

        # MLS setup: disable recent-form post-model shifts.
        home_ppg10, home_form_idx, _, _ = team_form(home_team, current_form)
        away_ppg10, away_form_idx, _, _ = team_form(away_team, current_form)
        form_delta = (home_ppg10 - away_ppg10) + 0.5 * (home_form_idx - away_form_idx)
        form_shift = 0.0
        after_form_probabilities = dict(probabilities)

        # Third-priority correction: current-season home/away performance delta.
        home_stats = clean_stats_dict(season_teams.get(prediction_season, {}).get(home_team, {})) or clean_stats_dict(find_latest_team_season_stats(
            home_team, season_teams
        ))
        away_stats = clean_stats_dict(season_teams.get(prediction_season, {}).get(away_team, {})) or clean_stats_dict(find_latest_team_season_stats(
            away_team, season_teams
        ))
        home_season_strength = (
            float(home_stats.get("avg_home_goals_scored", 0.0))
            - float(home_stats.get("avg_home_goals_conceded", 0.0))
            + 0.05
            * (
                float(home_stats.get("avg_home_shots_for", 0.0))
                - float(home_stats.get("avg_home_shots_against", 0.0))
            )
        )
        away_season_strength = (
            float(away_stats.get("avg_away_goals_scored", 0.0))
            - float(away_stats.get("avg_away_goals_conceded", 0.0))
            + 0.05
            * (
                float(away_stats.get("avg_away_shots_for", 0.0))
                - float(away_stats.get("avg_away_shots_against", 0.0))
            )
        )
        season_delta = home_season_strength - away_season_strength
        season_shift = max(
            -0.035,
            min(0.035, 0.025 * season_delta * prediction_season_coeff * MLS_ADJUSTMENT_WEIGHT),
        )
        if season_shift != 0.0:
            probabilities["H"] = max(0.0, probabilities.get("H", 0.0) + season_shift)
            probabilities["A"] = max(0.0, probabilities.get("A", 0.0) - season_shift)
            total_prob = probabilities.get("H", 0.0) + probabilities.get("D", 0.0) + probabilities.get("A", 0.0)
            if total_prob > 0:
                probabilities["H"] /= total_prob
                probabilities["D"] /= total_prob
                probabilities["A"] /= total_prob
        after_season_probabilities = dict(probabilities)

        # Fourth-priority correction: standings context weighted by division gap.
        season_lookup_for_pred = season_teams.get(prediction_season, {})
        season_positions = build_season_position_map(season_lookup_for_pred)
        home_table = clean_stats_dict(season_lookup_for_pred.get(home_team, {})) or clean_stats_dict(find_latest_team_season_stats(home_team, season_teams))
        away_table = clean_stats_dict(season_lookup_for_pred.get(away_team, {})) or clean_stats_dict(find_latest_team_season_stats(away_team, season_teams))
        home_points = float(home_table.get("points", 0.0) or 0.0)
        away_points = float(away_table.get("points", 0.0) or 0.0)
        home_pos = float(season_positions.get(home_team, home_table.get("league_position", 0.0) or 0.0))
        away_pos = float(season_positions.get(away_team, away_table.get("league_position", 0.0) or 0.0))
        table_delta = ((home_points - away_points) / 30.0) + ((away_pos - home_pos) / 20.0)
        division_weight = 0.35 + 0.25 * abs(strength_delta)
        table_shift = max(-0.04, min(0.04, 0.03 * table_delta * division_weight * MLS_ADJUSTMENT_WEIGHT))

        # Interleague absolute-position correction:
        # ranks competitions by league strength, then places each team in one global ladder.
        # Example: if top tier has 20 clubs, second-tier 1st starts at absolute position 21.
        home_comp_table = competition_tables.get(home_competition, {})
        away_comp_table = competition_tables.get(away_competition, {})
        home_comp_pos = float(home_comp_table.get("positions", {}).get(home_team, home_pos or 0.0) or 0.0)
        away_comp_pos = float(away_comp_table.get("positions", {}).get(away_team, away_pos or 0.0) or 0.0)
        home_abs_pos = float(competition_offsets.get(home_competition, 0)) + home_comp_pos
        away_abs_pos = float(competition_offsets.get(away_competition, 0)) + away_comp_pos
        interleague_pos_delta = away_abs_pos - home_abs_pos
        interleague_shift = max(-0.015, min(0.015, 0.002 * interleague_pos_delta * MLS_ADJUSTMENT_WEIGHT))

        # Extra boundary boost near the division cut line.
        # Favor the stronger division side to avoid inverted odds direction.
        boundary_bonus_shift = 0.0
        abs_gap = abs(interleague_pos_delta)
        if home_competition != away_competition and abs_gap <= 6.0:
            boundary_factor = (6.0 - abs_gap) / 6.0
            if home_league_strength > away_league_strength:
                boundary_bonus_shift += 0.006 * boundary_factor * MLS_ADJUSTMENT_WEIGHT
            elif away_league_strength > home_league_strength:
                boundary_bonus_shift -= 0.006 * boundary_factor * MLS_ADJUSTMENT_WEIGHT

        table_shift = table_shift + interleague_shift + boundary_bonus_shift
        table_shift = max(-0.045, min(0.045, table_shift))
        if table_shift != 0.0:
            probabilities["H"] = max(0.0, probabilities.get("H", 0.0) + table_shift)
            probabilities["A"] = max(0.0, probabilities.get("A", 0.0) - table_shift)
            total_prob = probabilities.get("H", 0.0) + probabilities.get("D", 0.0) + probabilities.get("A", 0.0)
            if total_prob > 0:
                probabilities["H"] /= total_prob
                probabilities["D"] /= total_prob
                probabilities["A"] /= total_prob

        # MLS-specific home edge: random 3-15% blended with home-form signal.
        home_adv_shift = mls_home_advantage_shift(home_team, prediction_season, season_teams)
        transfer = min(home_adv_shift, probabilities.get("A", 0.0))
        probabilities["H"] = max(0.0, probabilities.get("H", 0.0) + transfer)
        probabilities["A"] = max(0.0, probabilities.get("A", 0.0) - transfer)
        total_prob = probabilities.get("H", 0.0) + probabilities.get("D", 0.0) + probabilities.get("A", 0.0)
        if total_prob > 0:
            probabilities["H"] /= total_prob
            probabilities["D"] /= total_prob
            probabilities["A"] /= total_prob

        # MLS market-value correction:
        # top-5 player values with strong attacker weighting shift H/A probabilities.
        market_shift, home_market_score, away_market_score = market_value_probability_shift(
            home_team, away_team, market_value_data
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
        probabilities = apply_home_advantage_boost(probabilities)
        probabilities = reduce_draw_probability(probabilities)
        seed = prediction_randomizer_seed(home_team, away_team, competition_key, prediction_season)
        probabilities = apply_probability_randomizer(probabilities, MLS_RANDOMIZER_MAX_DELTA, seed=seed)
        final_probabilities = dict(probabilities)

        prediction = max(probabilities, key=probabilities.get)
        base_home_xg = max(0.0, float(home_goal_reg.predict(X_match)[0]))
        base_away_xg = max(0.0, float(away_goal_reg.predict(X_match)[0]))
        rng = np.random.default_rng(seed)
        predicted_score_home, predicted_score_away = sample_score_from_probs(
            probabilities.get("H", 0.0),
            probabilities.get("D", 0.0),
            probabilities.get("A", 0.0),
            base_home_xg, base_away_xg, prediction, rng
        )
        predicted_home_shots = max(0.0, float(home_shot_reg.predict(X_match)[0]))
        predicted_away_shots = max(0.0, float(away_shot_reg.predict(X_match)[0]))
        predicted_home_sot = max(0.0, float(home_sot_reg.predict(X_match)[0]))
        predicted_away_sot = max(0.0, float(away_sot_reg.predict(X_match)[0]))

        result_map = {"H": f"{home_team} win", "D": "Draw", "A": f"{away_team} win"}
        print("\nPrediction")
        print(f"Home: {home_team}")
        print(f"Away: {away_team}")
        print(f"Most likely result: {result_map[prediction]}")
        print(
            "Win probabilities: "
            f"{home_team} {format_percent_text(probabilities['H'])} | "
            f"Draw {format_percent_text(probabilities['D'])} | "
            f"{away_team} {format_percent_text(probabilities['A'])}"
        )
        print(
            "Reasoning: "
            f"base model H/D/A {raw_probabilities['H'] * 100:.1f}/{raw_probabilities['D'] * 100:.1f}/{raw_probabilities['A'] * 100:.1f}, "
            f"league shift {league_direction_shift:+.3f}, form shift {form_shift:+.3f} (disabled), "
            f"season shift {season_shift:+.3f}, table shift {table_shift:+.3f}, home shift {home_adv_shift:+.3f}, "
            f"market shift {market_shift:+.3f} (attacker-weighted and capped)."
        )
        print(f"Predicted score: {home_team} {predicted_score_home} - {predicted_score_away} {away_team}")
        print(f"Predicted shots: {home_team} {predicted_home_shots:.1f} | {away_team} {predicted_away_shots:.1f}")
        print(f"Predicted shots on target: {home_team} {predicted_home_sot:.1f} | {away_team} {predicted_away_sot:.1f}\n")
        if debug_mode:
            raw_odds = probabilities_to_odds(raw_probabilities)
            final_odds = probabilities_to_odds(final_probabilities)
            print("Debug reasoning")
            print(
                f"League context: {home_team} in {home_competition} ({home_league_strength:.2f}) vs "
                f"{away_team} in {away_competition} ({away_league_strength:.2f})"
            )
            print(
                f"Raw model probs: H {raw_probabilities['H']:.3f}, D {raw_probabilities['D']:.3f}, A {raw_probabilities['A']:.3f}"
            )
            print(
                f"After league adj (delta {strength_delta:.3f}, gap {strength_gap:.3f}, draw_scale {draw_scale:.3f}, "
                f"direction_shift {league_direction_shift:.3f}): "
                f"H {after_league_probabilities['H']:.3f}, D {after_league_probabilities['D']:.3f}, A {after_league_probabilities['A']:.3f}"
            )
            print(
                f"After form adj (disabled; delta {form_delta:.3f}, shift {form_shift:.3f}): "
                f"H {after_form_probabilities['H']:.3f}, D {after_form_probabilities['D']:.3f}, A {after_form_probabilities['A']:.3f}"
            )
            print(
                f"After season adj (delta {season_delta:.3f}, shift {season_shift:.3f}, coeff {prediction_season_coeff:.3f}): "
                f"H {after_season_probabilities['H']:.3f}, D {after_season_probabilities['D']:.3f}, A {after_season_probabilities['A']:.3f}"
            )
            print(
                f"After table/division adj (delta {table_delta:.3f}, inter_pos_delta {interleague_pos_delta:.1f}, "
                f"shift {table_shift:.3f}, weight {division_weight:.3f}): "
                f"H {final_probabilities['H']:.3f}, D {final_probabilities['D']:.3f}, A {final_probabilities['A']:.3f}"
            )
            print(
                f"Market value adj (weighted top-5: {home_team} {home_market_score/1_000_000:.1f}M vs "
                f"{away_team} {away_market_score/1_000_000:.1f}M, shift {market_shift:.3f})"
            )
            print(
                f"Implied decimal odds: {home_team} {final_odds['H']}, Draw {final_odds['D']}, {away_team} {final_odds['A']} "
                f"(raw: {raw_odds['H']}/{raw_odds['D']}/{raw_odds['A']})\n"
            )


if __name__ == "__main__":
    main()
