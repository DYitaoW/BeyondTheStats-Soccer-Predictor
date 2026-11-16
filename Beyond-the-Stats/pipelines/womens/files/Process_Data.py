"""Process Womens Leagues raw data — converts raw CSVs into standardized processed datasets."""

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
import re
import sys
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import season_calendar

RAW_FOLDER = os.path.join(BASE_DIR, "Data", "Raw_Data")
PROCESSED_FOLDER = os.path.join(BASE_DIR, "Data", "Processed_Data")
SEASON_PATTERN = re.compile(r"^(?:[a-z0-9]+stat)(\d{4})(?:-(\d{2}))?\.csv$", re.IGNORECASE)
MIN_START_YEAR = 2020
MIN_ROWS = 50
CURRENT_SEASON_MIN_ROWS = 20
PROCESS_WORKERS = max(1, (os.cpu_count() or 2) // 2)

REQUIRED_RAW_COLUMNS = {"Date", "Home", "Away", "HG", "AG", "Res"}
RESULT_MAP = {"H": 2, "D": 1, "A": 0}
ODDS_HOME_CANDIDATES = ["AvgH", "AvgCH", "PSCH", "B365CH", "MaxCH"]
ODDS_DRAW_CANDIDATES = ["AvgD", "AvgCD", "PSCD", "B365CD", "MaxCD"]
ODDS_AWAY_CANDIDATES = ["AvgA", "AvgCA", "PSCA", "B36CA", "B365CA", "MaxCA"]

OUTPUT_COLUMNS = [
    "Date",
    "HomeTeam",
    "AwayTeam",
    "FTHG",
    "FTAG",
    "FTR",
    "HS",
    "HST",
    "AS",
    "AST",
    "AvgH",
    "AvgD",
    "AvgA",
    "HomePointsBefore",
    "AwayPointsBefore",
    "HomeLeaguePosBefore",
    "AwayLeaguePosBefore",
    "ResultNum",
]


def read_csv_fast(path):
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.read_csv(path, encoding="latin-1", engine="python", on_bad_lines="skip")


def parse_season_start_year(file_name):
    match = SEASON_PATTERN.match(file_name)
    if not match:
        return None
    start_year = int(match.group(1))
    if start_year < MIN_START_YEAR:
        return None
    return start_year


def get_target_season_files(folder):
    valid = []
    for root, _, files in os.walk(folder):
        for file_name in files:
            if not file_name.endswith(".csv"):
                continue
            start_year = parse_season_start_year(file_name)
            if start_year is not None:
                full_path = os.path.join(root, file_name)
                rel_path = os.path.relpath(full_path, folder)
                valid.append((start_year, rel_path))

    valid.sort(key=lambda item: item[0])
    return [name for _, name in valid]


def first_existing_value(row, candidates, default=0.0):
    for col in candidates:
        if col in row and pd.notna(row[col]):
            try:
                return float(row[col])
            except Exception:
                continue
    return default


def add_table_context_columns(df):
    required = {"HomeTeam", "AwayTeam", "FTR", "FTHG", "FTAG"}
    if not required.issubset(df.columns):
        return df

    teams = sorted(set(df["HomeTeam"].dropna()) | set(df["AwayTeam"].dropna()))
    table = {
        team: {"points": 0, "gf": 0, "ga": 0, "gd": 0, "played": 0}
        for team in teams
    }
    position_map = {team: idx + 1 for idx, team in enumerate(teams)}

    home_points_before = []
    away_points_before = []
    home_pos_before = []
    away_pos_before = []

    for _, row in df.iterrows():
        home = row["HomeTeam"]
        away = row["AwayTeam"]
        if home not in table or away not in table:
            home_points_before.append(0)
            away_points_before.append(0)
            home_pos_before.append(0)
            away_pos_before.append(0)
            continue

        home_points_before.append(table[home]["points"])
        away_points_before.append(table[away]["points"])
        home_pos_before.append(position_map.get(home, 0))
        away_pos_before.append(position_map.get(away, 0))

        hg = row["FTHG"]
        ag = row["FTAG"]
        table[home]["gf"] += hg
        table[home]["ga"] += ag
        table[home]["gd"] += hg - ag
        table[home]["played"] += 1

        table[away]["gf"] += ag
        table[away]["ga"] += hg
        table[away]["gd"] += ag - hg
        table[away]["played"] += 1

        if row["FTR"] == "H":
            table[home]["points"] += 3
        elif row["FTR"] == "D":
            table[home]["points"] += 1
            table[away]["points"] += 1
        else:
            table[away]["points"] += 3

        sorted_table = sorted(
            table.items(),
            key=lambda item: (item[1]["points"], item[1]["gd"], item[1]["gf"]),
            reverse=True,
        )
        position_map = {team: idx + 1 for idx, (team, _) in enumerate(sorted_table)}

    df["HomePointsBefore"] = home_points_before
    df["AwayPointsBefore"] = away_points_before
    df["HomeLeaguePosBefore"] = home_pos_before
    df["AwayLeaguePosBefore"] = away_pos_before
    return df


def convert_raw_to_standard(df):
    df = df.copy()
    if not REQUIRED_RAW_COLUMNS.issubset(set(df.columns)):
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    # Filter out unplayed fixtures (empty HG/AG/Res)
    df = df[df["Res"].isin(["H", "D", "A"])].copy()
    if df.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    out = pd.DataFrame()
    out["Date"] = pd.to_datetime(df["Date"], format="%d/%m/%Y", errors="coerce").dt.strftime("%Y-%m-%d")
    out["HomeTeam"] = df["Home"].astype(str).str.strip()
    out["AwayTeam"] = df["Away"].astype(str).str.strip()
    out["FTHG"] = pd.to_numeric(df["HG"], errors="coerce").fillna(0).astype(int)
    out["FTAG"] = pd.to_numeric(df["AG"], errors="coerce").fillna(0).astype(int)
    out["FTR"] = df["Res"].astype(str).str.strip()

    out["HS"] = pd.to_numeric(df.get("HS"), errors="coerce").fillna(12).astype(int)
    out["HST"] = pd.to_numeric(df.get("HST"), errors="coerce").fillna(4).astype(int)
    out["AS"] = pd.to_numeric(df.get("AS"), errors="coerce").fillna(10).astype(int)
    out["AST"] = pd.to_numeric(df.get("AST"), errors="coerce").fillna(3).astype(int)

    out["AvgH"] = [first_existing_value(row, ODDS_HOME_CANDIDATES, 2.0) for _, row in df.iterrows()]
    out["AvgD"] = [first_existing_value(row, ODDS_DRAW_CANDIDATES, 3.2) for _, row in df.iterrows()]
    out["AvgA"] = [first_existing_value(row, ODDS_AWAY_CANDIDATES, 3.5) for _, row in df.iterrows()]

    out["ResultNum"] = out["FTR"].map(RESULT_MAP).fillna(1).astype(int)
    out = out.dropna(subset=["Date", "HomeTeam", "AwayTeam"])
    out = out.sort_values(by=["Date", "HomeTeam"]).reset_index(drop=True)
    out = add_table_context_columns(out)

    return out[OUTPUT_COLUMNS]


def process_file(rel_path):
    raw_path = os.path.join(RAW_FOLDER, rel_path)
    processed_path = os.path.join(PROCESSED_FOLDER, rel_path)
    os.makedirs(os.path.dirname(processed_path), exist_ok=True)

    df_raw = read_csv_fast(raw_path)
    df_std = convert_raw_to_standard(df_raw)
    if not df_std.empty:
        df_std.to_csv(processed_path, index=False)
        return rel_path, len(df_std)
    return rel_path, 0


def process_all():
    print("=== Processing Womens Leagues Data ===")
    target_files = get_target_season_files(RAW_FOLDER)
    print(f"Found {len(target_files)} target season files to process.")

    results = []
    with ThreadPoolExecutor(max_workers=PROCESS_WORKERS) as executor:
        futures = {executor.submit(process_file, f): f for f in target_files}
        for future in as_completed(futures):
            f = futures[future]
            try:
                rel, count = future.result()
                print(f"  Processed {rel}: {count} matches")
                results.append((rel, count))
            except Exception as exc:
                print(f"  Error processing {f}: {exc}")

    print(f"=== Processed {len(results)} files successfully ===")


if __name__ == "__main__":
    process_all()

