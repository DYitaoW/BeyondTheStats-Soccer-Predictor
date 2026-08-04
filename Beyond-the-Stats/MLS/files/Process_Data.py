"""Process MLS raw data — same logic as global ``Process_Data`` but for MLS competitions."""
import os
import re
import sys
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

MLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_DIR = os.path.dirname(MLS_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import season_calendar


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_FOLDER = os.path.join(BASE_DIR, "Data", "Raw_Data")
PROCESSED_FOLDER = os.path.join(BASE_DIR, "Data", "Processed_Data")
SEASON_PATTERN = re.compile(r"^(?:[a-z0-9]+stat)(\d{4})\.csv$", re.IGNORECASE)
MIN_START_YEAR = 2002
MIN_ROWS = 100
CURRENT_SEASON_MIN_ROWS = season_calendar.CURRENT_SEASON_MIN_ROWS
PROCESS_WORKERS = int(os.getenv("SOCCER_PROCESS_WORKERS", str(max(1, (os.cpu_count() or 2) // 2))))
USE_GPU_DF = os.getenv("SOCCER_USE_GPU_DF", "1").strip().lower() not in {"0", "false", "no"}

try:
    import cudf  # type: ignore
except Exception:
    cudf = None

REQUIRED_RAW_COLUMNS = {"Date", "Home", "Away", "HG", "AG", "Res"}

RESULT_MAP = {"H": 2, "D": 1, "A": 0}
ODDS_HOME_CANDIDATES = ["AvgCH", "PSCH", "B365CH", "MaxCH"]
ODDS_DRAW_CANDIDATES = ["AvgCD", "PSCD", "B365CD", "MaxCD"]
ODDS_AWAY_CANDIDATES = ["AvgCA", "PSCA", "B36CA", "B365CA", "MaxCA"]

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
    if USE_GPU_DF and cudf is not None:
        try:
            gdf = cudf.read_csv(path)
            return gdf.to_pandas()
        except Exception:
            pass
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
    if start_year > datetime.now().year:
        return None
    return start_year


def get_target_season_files(folder):
    valid = []
    for root, _, files in os.walk(folder):
        for file_name in files:
            if not file_name.endswith(".csv"):
                continue
            start_year = parse_season_start_year(file_name)
            if start_year is None:
                continue
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
    return float(default)


def has_minimum_rows(df, season_start_year, file_name=""):
    current_year = datetime.now().year
    in_progress = season_calendar.is_in_progress_season(
        season_start_year, file_name, current_year=current_year
    )
    minimum = CURRENT_SEASON_MIN_ROWS if in_progress else MIN_ROWS
    return len(df) >= minimum


def add_table_context_columns(df):
    required = {"HomeTeam", "AwayTeam", "FTR", "FTHG", "FTAG"}
    if not required.issubset(df.columns):
        return df

    teams = sorted(set(df["HomeTeam"].dropna()) | set(df["AwayTeam"].dropna()))
    table = {team: {"points": 0, "gf": 0, "ga": 0, "gd": 0} for team in teams}
    position_map = {team: idx + 1 for idx, team in enumerate(teams)}

    home_points_before = []
    away_points_before = []
    home_pos_before = []
    away_pos_before = []

    def rank_positions():
        ranked = sorted(
            teams,
            key=lambda t: (-table[t]["points"], -table[t]["gd"], -table[t]["gf"], t),
        )
        return {team: idx + 1 for idx, team in enumerate(ranked)}

    for row in df.itertuples(index=False):
        home = row.HomeTeam
        away = row.AwayTeam

        home_points_before.append(float(table.get(home, {}).get("points", 0.0)))
        away_points_before.append(float(table.get(away, {}).get("points", 0.0)))
        home_pos_before.append(float(position_map.get(home, len(teams))))
        away_pos_before.append(float(position_map.get(away, len(teams))))

        hg = row.FTHG
        ag = row.FTAG
        ftr = row.FTR
        if pd.isna(hg) or pd.isna(ag) or pd.isna(ftr):
            continue

        hg = int(hg)
        ag = int(ag)
        table[home]["gf"] += hg
        table[home]["ga"] += ag
        table[away]["gf"] += ag
        table[away]["ga"] += hg
        table[home]["gd"] = table[home]["gf"] - table[home]["ga"]
        table[away]["gd"] = table[away]["gf"] - table[away]["ga"]

        if ftr == "H":
            table[home]["points"] += 3
        elif ftr == "A":
            table[away]["points"] += 3
        elif ftr == "D":
            table[home]["points"] += 1
            table[away]["points"] += 1

        position_map = rank_positions()

    df["HomePointsBefore"] = home_points_before
    df["AwayPointsBefore"] = away_points_before
    df["HomeLeaguePosBefore"] = home_pos_before
    df["AwayLeaguePosBefore"] = away_pos_before
    return df


def convert_mls_raw_to_standard(df):
    if not REQUIRED_RAW_COLUMNS.issubset(df.columns):
        return pd.DataFrame()

    out = pd.DataFrame()
    out["Date"] = pd.to_datetime(df["Date"], dayfirst=False, format="mixed", errors="coerce")
    out["HomeTeam"] = df["Home"].astype(str).str.strip()
    out["AwayTeam"] = df["Away"].astype(str).str.strip()
    out["FTHG"] = pd.to_numeric(df["HG"], errors="coerce")
    out["FTAG"] = pd.to_numeric(df["AG"], errors="coerce")
    out["FTR"] = df["Res"].astype(str).str.strip()

    # MLS source does not include shots in this feed; keep sentinel values.
    out["HS"] = "NA"
    out["HST"] = "NA"
    out["AS"] = "NA"
    out["AST"] = "NA"

    out["AvgH"] = [first_existing_value(row, ODDS_HOME_CANDIDATES, 0.0) for _, row in df.iterrows()]
    out["AvgD"] = [first_existing_value(row, ODDS_DRAW_CANDIDATES, 0.0) for _, row in df.iterrows()]
    out["AvgA"] = [first_existing_value(row, ODDS_AWAY_CANDIDATES, 0.0) for _, row in df.iterrows()]

    out = out.dropna(subset=["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"])
    out = out[out["FTR"].isin({"H", "D", "A"})]
    if out.empty:
        return out

    out = out.sort_values("Date").reset_index(drop=True)
    out = add_table_context_columns(out)
    out["ResultNum"] = out["FTR"].map(RESULT_MAP)

    for col in OUTPUT_COLUMNS:
        if col not in out.columns:
            out[col] = 0.0
    return out[OUTPUT_COLUMNS]


def process_one_file(rel_path):
    file_path = os.path.join(RAW_FOLDER, rel_path)
    season_start_year = parse_season_start_year(os.path.basename(rel_path))
    if season_start_year is None:
        return False, rel_path, "skipped_invalid_name"

    raw_df = read_csv_fast(file_path)
    if not has_minimum_rows(raw_df, season_start_year, os.path.basename(rel_path)):
        return False, rel_path, "skipped_insufficient_data"

    processed_df = convert_mls_raw_to_standard(raw_df)
    if processed_df.empty:
        return False, rel_path, "skipped_empty_processed"

    output_path = os.path.join(PROCESSED_FOLDER, rel_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    new_csv = processed_df.to_csv(index=False)
    write_needed = True
    try:
        with open(output_path, "r", encoding="utf-8", newline="") as existing:
            existing_csv = existing.read()
        if existing_csv == new_csv:
            write_needed = False
    except OSError:
        pass
    if write_needed:
        with open(output_path, "w", encoding="utf-8", newline="") as out:
            out.write(new_csv)
        return True, rel_path, "processed"
    return True, rel_path, "kept_content_unchanged"


def main():
    if not os.path.isdir(RAW_FOLDER):
        raise FileNotFoundError(f"Raw data folder not found: {RAW_FOLDER}")

    os.makedirs(PROCESSED_FOLDER, exist_ok=True)
    target_files = get_target_season_files(RAW_FOLDER)
    if not target_files:
        raise ValueError("No valid MLS season files were found in Raw_Data.")

    workers = max(1, PROCESS_WORKERS)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_one_file, rel_path): rel_path for rel_path in target_files}
        for future in as_completed(futures):
            _, rel_path, status = future.result()
            if status == "processed":
                print(f"Processed {rel_path}...")
            elif status == "kept_content_unchanged":
                pass
            elif status == "skipped_insufficient_data":
                print(f"Skipped {rel_path} (insufficient data)...")
            elif status == "skipped_empty_processed":
                print(f"Skipped {rel_path} (no usable rows after normalization)...")
            elif status == "skipped_invalid_name":
                print(f"Skipped {rel_path} (invalid season name)...")

    print("All MLS files processed.")


if __name__ == "__main__":
    main()
