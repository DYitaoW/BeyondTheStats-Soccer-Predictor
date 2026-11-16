"""Sort Womens Leagues team data — builds head-to-head, form, and season tables from processed CSVs."""

import os as _os_paths_setup
import sys as _sys_paths_setup
_FILES_DIR = _os_paths_setup.path.dirname(_os_paths_setup.path.abspath(__file__))
_REGION_DIR = _os_paths_setup.path.dirname(_FILES_DIR)
_SP_DIR = _os_paths_setup.path.dirname(_os_paths_setup.path.dirname(_REGION_DIR))
if _SP_DIR not in _sys_paths_setup.path:
    _sys_paths_setup.path.insert(0, _SP_DIR)
from shared import paths as _bts_paths
if str(_bts_paths.SHARED_DIR) not in _sys_paths_setup.path:
    _sys_paths_setup.path.insert(0, str(_bts_paths.SHARED_DIR))
BASE_DIR = str(_bts_paths.WOMENS_DIR)
PREDICTIONS_DIR = str(_bts_paths.OUTPUT_PRED_WOMENS)
PROJECT_DIR = str(_bts_paths.SP_DIR)

import os
import sys
import json
import re
from collections import defaultdict
from datetime import datetime
import pandas as pd

if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)
_WEBSITE_DIR = os.path.join(PROJECT_DIR, "Website")
if _WEBSITE_DIR not in sys.path:
    sys.path.insert(0, _WEBSITE_DIR)

from competition_rules import canonical_team_name

PROCESSED_DIR = os.path.join(BASE_DIR, "Data", "Processed_Data")
OUTPUT_DIR = os.path.join(BASE_DIR, "Data", "Team_Data")
SEASON_PATTERN = re.compile(r"^(?:[a-z0-9]+stat)(\d{4})(?:-(\d{2}))?\.csv$", re.IGNORECASE)
MIN_START_YEAR = 2020

DEFAULT_LEAGUE_STRENGTH = {
    # Model keys = processed-data folders (what Predict_Match trains on) ...
    "United States/NWSL": 0.85,
    "England/WSL": 0.88,
    # ... plus the app's competition label.
    "England/Women's Super League": 0.88,
}


def blank_team_stats():
    return {
        "games": 0,
        "wins": 0,
        "draws": 0,
        "losses": 0,
        "points": 0,
        "goals_scored": 0,
        "goals_conceded": 0,
        "home_games": 0,
        "away_games": 0,
        "home_wins": 0,
        "home_draws": 0,
        "home_losses": 0,
        "away_wins": 0,
        "away_draws": 0,
        "away_losses": 0,
        "home_goals_scored": 0,
        "home_goals_conceded": 0,
        "away_goals_scored": 0,
        "away_goals_conceded": 0,
        "shots_for": 0,
        "shots_against": 0,
        "shots_on_target_for": 0,
        "shots_on_target_against": 0,
        "home_shots_for": 0,
        "home_shots_against": 0,
        "home_sot_for": 0,
        "home_sot_against": 0,
        "away_shots_for": 0,
        "away_shots_against": 0,
        "away_sot_for": 0,
        "away_sot_against": 0,
    }


def update_team(stats, gf, ga, shots_f, shots_a, sot_f, sot_a, result, is_home):
    stats["games"] += 1
    stats["goals_scored"] += gf
    stats["goals_conceded"] += ga
    stats["shots_for"] += shots_f
    stats["shots_against"] += shots_a
    stats["shots_on_target_for"] += sot_f
    stats["shots_on_target_against"] += sot_a

    if result == "W":
        stats["wins"] += 1
        stats["points"] += 3
    elif result == "D":
        stats["draws"] += 1
        stats["points"] += 1
    else:
        stats["losses"] += 1

    if is_home:
        stats["home_games"] += 1
        stats["home_goals_scored"] += gf
        stats["home_goals_conceded"] += ga
        stats["home_shots_for"] += shots_f
        stats["home_shots_against"] += shots_a
        stats["home_sot_for"] += sot_f
        stats["home_sot_against"] += sot_a
        if result == "W":
            stats["home_wins"] += 1
        elif result == "D":
            stats["home_draws"] += 1
        else:
            stats["home_losses"] += 1
    else:
        stats["away_games"] += 1
        stats["away_goals_scored"] += gf
        stats["away_goals_conceded"] += ga
        stats["away_shots_for"] += shots_f
        stats["away_shots_against"] += shots_a
        stats["away_sot_for"] += sot_f
        stats["away_sot_against"] += sot_a
        if result == "W":
            stats["away_wins"] += 1
        elif result == "D":
            stats["away_draws"] += 1
        else:
            stats["away_losses"] += 1


def get_target_season_files():
    valid = []
    for root, _, files in os.walk(PROCESSED_DIR):
        for file_name in files:
            if not file_name.endswith(".csv"):
                continue
            match = SEASON_PATTERN.match(file_name)
            if not match:
                continue
            start_year = int(match.group(1))
            if start_year >= MIN_START_YEAR:
                full_path = os.path.join(root, file_name)
                rel_path = os.path.relpath(full_path, PROCESSED_DIR)
                valid.append((start_year, rel_path))
    valid.sort(key=lambda item: item[0])
    return [name for _, name in valid]


def sort_all_seasons():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    target_files = get_target_season_files()
    print(f"Sorting {len(target_files)} processed season files...")

    season_teams = defaultdict(lambda: defaultdict(blank_team_stats))
    overall_teams = defaultdict(blank_team_stats)
    h2h = defaultdict(lambda: defaultdict(blank_team_stats))
    current_form = defaultdict(lambda: {"last_5": [], "home_last_5": [], "away_last_5": []})

    for rel_path in target_files:
        full_path = os.path.join(PROCESSED_DIR, rel_path)
        # Season key = processed path without ".csv" ("England/WSL/wslstat2026"),
        # the format Predict_Match / Build_Historical_Tables parse (start year
        # from the filename, competition from the folder). Bare years ("2026")
        # parsed as start year -1 and merged NWSL + WSL into one season bucket.
        season_str = rel_path.replace(os.sep, "/")[: -len(".csv")]

        df = pd.read_csv(full_path)
        for _, row in df.iterrows():
            ht = str(row["HomeTeam"]).strip()
            at = str(row["AwayTeam"]).strip()
            hg = int(row["FTHG"])
            ag = int(row["FTAG"])
            hs = int(row.get("HS", 12))
            as_shots = int(row.get("AS", 10))
            hst = int(row.get("HST", 4))
            ast = int(row.get("AST", 3))
            res = str(row["FTR"]).strip()

            h_res = "W" if res == "H" else ("D" if res == "D" else "L")
            a_res = "W" if res == "A" else ("D" if res == "D" else "L")

            update_team(season_teams[season_str][ht], hg, ag, hs, as_shots, hst, ast, h_res, True)
            update_team(season_teams[season_str][at], ag, hg, as_shots, hs, ast, hst, a_res, False)

            update_team(overall_teams[ht], hg, ag, hs, as_shots, hst, ast, h_res, True)
            update_team(overall_teams[at], ag, hg, as_shots, hs, ast, hst, a_res, False)

            update_team(h2h[ht][at], hg, ag, hs, as_shots, hst, ast, h_res, True)

            # Track form
            m_date = str(row["Date"])
            current_form[ht]["last_5"].append({"date": m_date, "opponent": at, "result": h_res, "gf": hg, "ga": ag, "is_home": True})
            current_form[at]["last_5"].append({"date": m_date, "opponent": ht, "result": a_res, "gf": ag, "ga": hg, "is_home": False})
            current_form[ht]["home_last_5"].append({"date": m_date, "opponent": at, "result": h_res, "gf": hg, "ga": ag})
            current_form[at]["away_last_5"].append({"date": m_date, "opponent": ht, "result": a_res, "gf": ag, "ga": hg})

    # Trim form to last 5
    for team, form_data in current_form.items():
        form_data["last_5"] = form_data["last_5"][-5:]
        form_data["home_last_5"] = form_data["home_last_5"][-5:]
        form_data["away_last_5"] = form_data["away_last_5"][-5:]

    # Write JSON files
    with open(os.path.join(OUTPUT_DIR, "season_teams.json"), "w") as fh:
        json.dump(season_teams, fh, indent=2)
    with open(os.path.join(OUTPUT_DIR, "overall_teams.json"), "w") as fh:
        json.dump(overall_teams, fh, indent=2)
    with open(os.path.join(OUTPUT_DIR, "head_to_head.json"), "w") as fh:
        json.dump(h2h, fh, indent=2)
    with open(os.path.join(OUTPUT_DIR, "current_form.json"), "w") as fh:
        json.dump(current_form, fh, indent=2)
    with open(os.path.join(OUTPUT_DIR, "league_strength.json"), "w") as fh:
        json.dump(DEFAULT_LEAGUE_STRENGTH, fh, indent=2)

    print(f"=== Sort_Data complete. Files written to {OUTPUT_DIR} ===")


if __name__ == "__main__":
    sort_all_seasons()
