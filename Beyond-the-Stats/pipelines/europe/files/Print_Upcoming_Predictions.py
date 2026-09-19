
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
import os

import pandas as pd


# BASE_DIR set by shared.paths bootstrap above
PREDICTIONS_FILE = str(_bts_paths.OUTPUT_PRED_EUROPE / "upcoming_matchweek_predictions.csv")


def label_for_prediction(code, home_team, away_team):
    code = str(code).strip().upper()
    if code == "H":
        return f"{home_team} win"
    if code == "A":
        return f"{away_team} win"
    return "Draw"


def to_percent(value):
    try:
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return "0.0%"


def main():
    if not os.path.exists(PREDICTIONS_FILE):
        raise FileNotFoundError(f"Predictions file not found: {PREDICTIONS_FILE}")

    df = pd.read_csv(PREDICTIONS_FILE)
    if df.empty:
        print("No upcoming predictions found.")
        return

    required = [
        "competition",
        "match_date",
        "home_team",
        "away_team",
        "predicted_result",
        "prob_home",
        "prob_draw",
        "prob_away",
    ]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns in predictions file: {', '.join(missing)}")

    df = df.sort_values(["competition", "match_date", "home_team", "away_team"])

    current_comp = None
    for _, row in df.iterrows():
        competition = str(row["competition"]).strip()
        if competition != current_comp:
            if current_comp is not None:
                print("")
            print(f"[{competition}]")
            current_comp = competition

        home = str(row["home_team"]).strip()
        away = str(row["away_team"]).strip()
        winner = label_for_prediction(row["predicted_result"], home, away)
        print(
            f"{row['match_date']} | {home} vs {away} | Predicted: {winner} | "
            f"H {to_percent(row['prob_home'])} D {to_percent(row['prob_draw'])} A {to_percent(row['prob_away'])}"
        )


if __name__ == "__main__":
    main()
