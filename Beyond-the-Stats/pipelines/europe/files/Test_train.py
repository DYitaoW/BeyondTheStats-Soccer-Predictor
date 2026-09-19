
import os as _os_paths_setup
import sys as _sys_paths_setup
_FILES_DIR = _os_paths_setup.path.dirname(_os_paths_setup.path.abspath(__file__))
_REGION_DIR = _os_paths_setup.path.dirname(_FILES_DIR)  # pipelines/europe
_SP_DIR = _os_paths_setup.path.dirname(_os_paths_setup.path.dirname(_REGION_DIR))  # Beyond-the-Stats
if _SP_DIR not in _sys_paths_setup.path:
    _sys_paths_setup.path.insert(0, _SP_DIR)
from shared import paths as _bts_paths
if str(_bts_paths.SHARED_DIR) not in _sys_paths_setup.path:
    _sys_paths_setup.path.insert(0, str(_bts_paths.SHARED_DIR))
BASE_DIR = str(_bts_paths.SP_DIR)  # europe uses project-level Data/
PREDICTIONS_DIR = str(_bts_paths.OUTPUT_PRED_EUROPE)
PROJECT_DIR = str(_bts_paths.SP_DIR)
import json
import os
import pandas as pd
import re
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import LabelEncoder
from datetime import datetime

try:
    from xgboost import XGBClassifier
except ImportError:
    XGBClassifier = None

PROCESSED_DIR = os.path.join("Data", "Processed_Data")
SEASON_PATTERN = re.compile(r"^(?:[a-z0-9]+stat)(\d{4})-(\d{2})\.csv$", re.IGNORECASE)
MIN_START_YEAR = 2002


def load_json(path):
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)

def load_json_if_exists(path):
    if not os.path.exists(path):
        return None
    try:
        return load_json(path)
    except Exception:
        return None


def team_form(team, current_form):
    stats = current_form.get("teams", {}).get(team, {})
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


def per_game(stats, total_key, games_key):
    games = stats.get(games_key, 0)
    if not games:
        return 0.0
    return stats.get(total_key, 0) / games


def parse_season_start_year(file_name):
    match = SEASON_PATTERN.match(file_name)
    if not match:
        return None

    start_year = int(match.group(1))
    end_year_two_digits = int(match.group(2))
    if end_year_two_digits != (start_year + 1) % 100:
        return None
    if start_year < MIN_START_YEAR:
        return None
    if start_year > datetime.now().year:
        return None

    return start_year


def season_recency_coefficient(latest_start_year, season_start_year):
    age = max(0, latest_start_year - season_start_year)
    if age <= 0:
        return 1.00
    if age == 1:
        return 0.92
    if age == 2:
        return 0.84
    if age == 3:
        return 0.76
    if age == 4:
        return 0.70
    return 0.60

# function used to setup the progrma
def build_features(
    match_df,
    season_key,
    competition_key,
    season_coeff,
    overall_teams,
    season_teams,
    head_to_head,
    league_strength,
    current_form,
):
    season_lookup = season_teams.get(season_key, {})
    season_positions = build_season_position_map(season_lookup)
    rows = []
    previous_odds = {}
    competition_strength = float(league_strength.get(competition_key, 0.85))

    for _, match in match_df.iterrows():
        home = match["HomeTeam"]
        away = match["AwayTeam"]

        home_overall = overall_teams.get(home, {})
        away_overall = overall_teams.get(away, {})
        home_season = season_lookup.get(home, {})
        away_season = season_lookup.get(away, {})
        h2h_home = head_to_head.get(home, {}).get(away, {})
        home_points_before = match.get("HomePointsBefore", home_season.get("points", 0.0))
        away_points_before = match.get("AwayPointsBefore", away_season.get("points", 0.0))
        home_pos_before = match.get("HomeLeaguePosBefore", season_positions.get(home, 0.0))
        away_pos_before = match.get("AwayLeaguePosBefore", season_positions.get(away, 0.0))
        home_points_before = 0.0 if pd.isna(home_points_before) else float(home_points_before)
        away_points_before = 0.0 if pd.isna(away_points_before) else float(away_points_before)
        home_pos_before = 0.0 if pd.isna(home_pos_before) else float(home_pos_before)
        away_pos_before = 0.0 if pd.isna(away_pos_before) else float(away_pos_before)

        home_prev = previous_odds.get(home, {"win": 0.0, "draw": 0.0, "lose": 0.0})
        away_prev = previous_odds.get(away, {"win": 0.0, "draw": 0.0, "lose": 0.0})
        home_ppg10, home_form_idx, home_form_gf10, home_form_ga10 = team_form(home, current_form)
        away_ppg10, away_form_idx, away_form_gf10, away_form_ga10 = team_form(away, current_form)

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
                "home_league_strength": competition_strength,
                "away_league_strength": competition_strength,
                "league_strength_diff": 0.0,
                "home_points_before": home_points_before,
                "away_points_before": away_points_before,
                "points_before_diff": home_points_before - away_points_before,
                "home_league_pos_before": home_pos_before,
                "away_league_pos_before": away_pos_before,
                "league_pos_before_diff": away_pos_before - home_pos_before,
                "season_coeff": season_coeff,
                "home_adj_points_per_game": per_game(home_season, "points", "games") * competition_strength * season_coeff,
                "away_adj_points_per_game": per_game(away_season, "points", "games") * competition_strength * season_coeff,
                "home_adj_avg_goals_scored": home_overall.get("weighted_avg_goals_scored", home_overall.get("avg_goals_scored", 0.0))
                * competition_strength,
                "away_adj_avg_goals_scored": away_overall.get("weighted_avg_goals_scored", away_overall.get("avg_goals_scored", 0.0))
                * competition_strength,
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

# functino used to ge thte values form the files
def load_matches(processed_dir):
    season_frames = []
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
        file_path = os.path.join(processed_dir, rel_path)
        frame = pd.read_csv(file_path)

        for odds_col in ["AvgH", "AvgD", "AvgA"]:
            if odds_col not in frame.columns:
                frame[odds_col] = 0.0
        for table_col in ["HomePointsBefore", "AwayPointsBefore", "HomeLeaguePosBefore", "AwayLeaguePosBefore"]:
            if table_col not in frame.columns:
                frame[table_col] = 0.0

        frame = frame[
            [
                "HomeTeam",
                "AwayTeam",
                "FTR",
                "AvgH",
                "AvgD",
                "AvgA",
                "HomePointsBefore",
                "AwayPointsBefore",
                "HomeLeaguePosBefore",
                "AwayLeaguePosBefore",
            ]
        ].dropna(
            subset=["HomeTeam", "AwayTeam", "FTR"]
        )

        frame["season_key"] = rel_path.replace(".csv", "")
        frame["competition"] = os.path.dirname(rel_path).replace("\\", "/") or "Unknown"
        frame["season_coeff"] = season_recency_coefficient(latest_start_year, start_year)
        season_frames.append(frame)

    if not season_frames:
        raise ValueError("No processed season CSV files were found.")

    return pd.concat(season_frames, ignore_index=True), season_files

# run and test the results
def main():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    processed_dir = os.path.join(project_root, PROCESSED_DIR)
    overall_json = os.path.join(project_root, "Data", "Team_Data", "overall_teams.json")
    season_json = os.path.join(project_root, "Data", "Team_Data", "season_teams.json")
    h2h_json = os.path.join(project_root, "Data", "Team_Data", "head_to_head.json")
    league_strength_json = os.path.join(project_root, "Data", "Team_Data", "league_strength.json")
    current_form_json = os.path.join(project_root, "Data", "Team_Data", "current_form.json")

    overall_teams = load_json(overall_json)
    season_teams = load_json(season_json)
    head_to_head = load_json(h2h_json)
    league_strength = load_json_if_exists(league_strength_json) or {}
    current_form = load_json_if_exists(current_form_json) or {"teams": {}}

    matches, season_files = load_matches(processed_dir)
    latest_season_file = season_files[-1]
    latest_season_key = latest_season_file.replace(".csv", "")

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
                league_strength,
                current_form,
            )
        )

    X = pd.concat(feature_frames, ignore_index=True)
    X = pd.get_dummies(X, columns=["competition"], dtype=float)
    y = matches["FTR"].reset_index(drop=True)

    train_mask = matches["season_key"] != latest_season_key
    test_mask = matches["season_key"] == latest_season_key

    X_train = X[train_mask]
    y_train = y[train_mask]
    X_test = X[test_mask]
    y_test = y[test_mask]

    label_encoder = LabelEncoder()
    y_train_enc = label_encoder.fit_transform(y_train)
    y_test_enc = label_encoder.transform(y_test)

    backend = "random-forest-cpu"
    model = None

    if XGBClassifier is not None:
        try:
            model = XGBClassifier(
                n_estimators=500,
                max_depth=8,
                learning_rate=0.05,
                subsample=0.9,
                colsample_bytree=0.9,
                objective="multi:softprob",
                num_class=len(label_encoder.classes_),
                eval_metric="mlogloss",
                random_state=42,
                tree_method="hist",
                device="cuda",
            )
            model.fit(X_train, y_train_enc)
            backend = "xgboost-gpu"
        except Exception:
            model = XGBClassifier(
                n_estimators=500,
                max_depth=8,
                learning_rate=0.05,
                subsample=0.9,
                colsample_bytree=0.9,
                objective="multi:softprob",
                num_class=len(label_encoder.classes_),
                eval_metric="mlogloss",
                random_state=42,
                tree_method="hist",
                device="cpu",
            )
            model.fit(X_train, y_train_enc)
            backend = "xgboost-cpu"

    if model is None:
        model = RandomForestClassifier(n_estimators=300, random_state=42)
        model.fit(X_train, y_train_enc)

    accuracy = model.score(X_test, y_test_enc)

    print("Train seasons:", len(season_files) - 1)
    print("Test season:", latest_season_file)
    print("Model backend:", backend)
    print("Rows used:", len(X))
    print("Train rows:", len(X_train), "| Test rows:", len(X_test))
    print("Accuracy:", accuracy)


if __name__ == "__main__":
    main()
