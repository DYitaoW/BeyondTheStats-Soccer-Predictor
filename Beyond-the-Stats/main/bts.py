"""Beyond The Stats CLI — predict and status commands.

Usage:
    python main/bts.py predict <home_team> <away_team> [--mode global|mls|extra]
    python main/bts.py status
"""
import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd

MAIN_DIR = Path(__file__).resolve().parent
SP_DIR = MAIN_DIR.parent
PROJECT_ROOT = SP_DIR.parent
if str(SP_DIR) not in sys.path:
    sys.path.insert(0, str(SP_DIR))
from shared import paths as _paths

FILES_DIR = _paths.EUROPE_FILES_DIR
MLS_FILES_DIR = _paths.MLS_FILES_DIR
EXTRA_FILES_DIR = _paths.EXTRA_FILES_DIR


def _build_predict_context(mode):
    files_by_mode = {
        "global": FILES_DIR,
        "mls": MLS_FILES_DIR,
        "extra": EXTRA_FILES_DIR,
    }
    files_dir = files_by_mode.get(mode, FILES_DIR)
    sys.path.insert(0, str(files_dir))
    import Predict_Match as pm
    data_dir = {
        "global": _paths.DATA_DIR,
        "mls": _paths.MLS_DATA_DIR,
        "extra": _paths.EXTRA_DATA_DIR,
    }.get(mode, _paths.DATA_DIR)
    # Predict_Match expects BASE_DIR to be the region root that contains Data/
    if mode == "global":
        pm.BASE_DIR = str(SP_DIR)
    else:
        pm.BASE_DIR = str(data_dir.parent)
    pm.PROCESSED_DIR = str(data_dir / "Processed_Data")
    pm.TEAM_DATA_DIR = str(data_dir / "Team_Data")
    pm.MODEL_CACHE = str(data_dir / "Team_Data" / "model_cache.pkl")

    if not os.path.exists(pm.MODEL_CACHE):
        print(f"Model cache not found at {pm.MODEL_CACHE}. Run 'bts refresh' first.")
        sys.exit(1)

    import joblib
    bundle = joblib.load(pm.MODEL_CACHE)
    overall_teams = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "overall_teams.json"))
    season_teams = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "season_teams.json"))
    head_to_head = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "head_to_head.json"))
    current_form = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "current_form.json"))
    league_strength = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "league_strength.json")) or {}
    return bundle, overall_teams, season_teams, head_to_head, current_form, league_strength, pm


def cmd_predict(args):
    bundle, overall_teams, season_teams, head_to_head, current_form, league_strength, pm = _build_predict_context(args.mode)
    home = str(args.home_team).strip()
    away = str(args.away_team).strip()

    match_input = pm.build_match_input(home, away)
    competition = args.competition or "Unknown/League"
    latest_season = max(season_teams.keys(), key=pm.parse_start_year_from_key) if season_teams else "Unknown"
    prediction_season = pm.choose_season_for_teams(home, away, season_teams, latest_season)
    season_coeff = 1.0

    X = pm.build_features(
        match_input, prediction_season, competition, season_coeff,
        overall_teams, season_teams, head_to_head, current_form, league_strength,
    )
    X = pd.get_dummies(X, columns=["competition"], dtype=float)
    train_columns = bundle.get("train_columns", [])
    X = X.reindex(columns=train_columns, fill_value=0.0)

    clf = bundle.get("clf")
    result_label_encoder = bundle.get("result_label_encoder")
    proba = clf.predict_proba(X)[0]
    probabilities = {}
    for idx, encoded_label in enumerate(clf.classes_):
        label = result_label_encoder.inverse_transform([encoded_label])[0]
        probabilities[label] = float(proba[idx])

    print(f"\n  {home} vs {away}")
    print(f"  {'Prediction:':<15} {max(probabilities, key=probabilities.get)}")
    print(f"  {'Home Win:':<15} {probabilities.get('H', 0)*100:.1f}%")
    print(f"  {'Draw:':<15} {probabilities.get('D', 0)*100:.1f}%")
    print(f"  {'Away Win:':<15} {probabilities.get('A', 0)*100:.1f}%")
    print()


def cmd_status(args):
    predicted_file = _paths.GLOBAL_UPCOMING_FILE
    mobile_feed = SP_DIR / "Output" / "mobile_app_feed.json"
    run_status = _paths.BACKEND_RUN_STATUS_FILE

    print(f"  {'Project Root:':<25} {PROJECT_ROOT}")
    print(f"  {'Predictions File:':<25} {'exists' if predicted_file.exists() else 'missing'}")
    print(f"  {'Mobile Feed:':<25} {'exists' if mobile_feed.exists() else 'missing'}")

    if run_status.exists():
        try:
            with open(run_status, encoding="utf-8") as f:
                data = json.load(f)
            ok = bool(data.get("ok"))
            print(f"  {'Last Pipeline Run:':<25} {data.get('finished_utc') or 'unknown'}")
            print(f"  {'Status:':<25} {'ok' if ok else 'failed'}"
                  f" (rc={data.get('return_code')}, trigger={data.get('trigger') or 'unknown'})")
            if data.get("log_file"):
                print(f"  {'Log:':<25} {data['log_file']}")
        except Exception as exc:
            print(f"  {'Last Pipeline Run:':<25} unreadable ({exc})")
    print()


def parse_args():
    parser = argparse.ArgumentParser(description="Beyond The Stats CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_predict = sub.add_parser("predict", help="Predict a match between two teams")
    p_predict.add_argument("home_team", help="Home team name")
    p_predict.add_argument("away_team", help="Away team name")
    p_predict.add_argument("--mode", choices=["global", "mls", "extra"], default="global")
    p_predict.add_argument("--competition", help="Override competition name (default: Unknown/League)")
    p_predict.set_defaults(func=cmd_predict)

    p_status = sub.add_parser("status", help="Show system status")
    p_status.set_defaults(func=cmd_status)

    return parser.parse_args()


def main():
    args = parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
