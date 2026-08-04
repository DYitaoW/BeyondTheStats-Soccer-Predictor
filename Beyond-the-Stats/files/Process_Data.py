"""
Data processing — transform raw football-data.co.uk CSVs into training features.

Sub-pipeline step after ``Download_Latest_Data``.  For each competition CSV:
- Validates required columns (Date, HomeTeam, AwayTeam, FTHG, FTAG, FTR, …)
- Adds rolling form features (last N matches home / away / overall)
- Converts date strings to datetime
- Saves to ``Data/Processed_Data/``

Two tiebreaker regimes are configured here via ``H2H_TIEBREAKER_PREFIXES``:
the H2H leagues (La Liga, Serie A, etc.) use points → GD → GF → team-name
within head-to-head groups; all others use overall GD → GF → team-name.
"""
import pandas as pd
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
import season_calendar

# Head-to-head tiebreaker leagues: La Liga, La Liga 2, Serie A, Serie B, Liga Portugal.
H2H_TIEBREAKER_PREFIXES = {"laligastat", "laliga2stat", "seriaastat", "seriabstat", "portstat"}

RAW_FOLDER = os.path.join(BASE_DIR, "Data", "Raw_Data")
PROCESSED_FOLDER = os.path.join(BASE_DIR, "Data", "Processed_Data")
SEASON_PATTERN = re.compile(r"^(?:[a-z0-9]+stat)(\d{4})-(\d{2})\.csv$", re.IGNORECASE)
GENERAL_REQUIRED_COLUMNS = ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR", "HS", "HST", "AS", "AST"]
CORE_REQUIRED_COLUMNS = ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"]
MIN_COMPLETENESS_RATIO = 0.95
MIN_ROWS = 250
CURRENT_SEASON_MIN_ROWS = season_calendar.CURRENT_SEASON_MIN_ROWS
MIN_START_YEAR = 2002

# Leagues sourced from football-data.co.uk's "new" single-CSV format (see
# Download_Latest_Data.py) don't publish shot statistics (HS/HST/AS/AST).
# These file prefixes are exempt from the shot-column completeness check so
# their matches aren't dropped outright; missing shot stats are instead left
# blank and treated as -1 sentinels by Predict_Match.py.
NO_SHOT_STATS_PREFIXES = {"norstat", "swestat", "autstat", "polstat", "roustat"}

# Smaller top-flight divisions play noticeably fewer matches per season than
# the 250-row default MIN_ROWS (tuned for ~20+ team top-5-league divisions):
# Norway/Sweden have 16 teams (240 matches/season) and Greece's Super League
# (14 teams plus a playoff round) runs ~230-240. Without this override every
# one of their *completed* seasons would be silently dropped as
# "insufficient data" (only the current in-progress season, which uses the
# separate CURRENT_SEASON_MIN_ROWS bar, would ever pass).
MIN_ROWS_OVERRIDES = {"norstat": 220, "swestat": 220, "grecstat": 220, "autstat": 170, "polstat": 220, "roustat": 220}
PROCESS_WORKERS = int(os.getenv("SOCCER_PROCESS_WORKERS", str(max(1, (os.cpu_count() or 2) // 2))))
USE_GPU_DF = os.getenv("SOCCER_USE_GPU_DF", "1").strip().lower() not in {"0", "false", "no"}

try:
    import cudf  # type: ignore
except Exception:
    cudf = None

columns = [
    "Date", "HomeTeam", "FTHG", "HTHG", "HS", "HST", "HC", "HF","HFKC", "HO", "HY", "HR", "HBP", # home team data
    "AwayTeam", "FTAG", "HTAG", "AS", "AST", "AC", "AF", "AFKC", "AO", "AY", "AR", "ABP", # away team data
    "Referee", "FTR", "HTR", # overall game data
    "AvgH", "AvgD", "AvgA", # overall betting odds data
    "Max>2.5", "Max<2.5", "Avg>2.5", "Avg<2.5", # goal over/under betting odds
    "AvgAHH", "AvgAHA" # overall handicap betting odds 
]

result_map = {"H": 2, "D": 1, "A": 0}


def read_csv_fast(path):
    """
    Read CSV with optional GPU acceleration (cuDF) while preserving pandas output.
    Falls back to pandas parsing behavior used previously.
    """
    if USE_GPU_DF and cudf is not None:
        try:
            gdf = cudf.read_csv(path)
            return gdf.to_pandas()
        except Exception:
            pass
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.read_csv(
            path,
            encoding="latin-1",
            engine="python",
            on_bad_lines="skip",
        )


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


def has_required_general_data(df, start_year, competition_prefix="", file_name="", *, force_min_rows=None):
    required_columns = (
        CORE_REQUIRED_COLUMNS if competition_prefix in NO_SHOT_STATS_PREFIXES else GENERAL_REQUIRED_COLUMNS
    )
    if any(col not in df.columns for col in required_columns):
        return False

    min_rows_floor = force_min_rows
    if min_rows_floor is None:
        if season_calendar.is_in_progress_season(start_year, file_name or f"{competition_prefix}{start_year}-00.csv"):
            min_rows_floor = CURRENT_SEASON_MIN_ROWS
        else:
            min_rows_floor = MIN_ROWS_OVERRIDES.get(competition_prefix, MIN_ROWS)

    if len(df) < max(1, int(min_rows_floor)):
        return False

    # Current / force-relaxed seasons: keep whatever early results we have.
    if min_rows_floor <= CURRENT_SEASON_MIN_ROWS:
        return True

    complete_rows = df[required_columns].notna().all(axis=1).mean()
    return complete_rows >= MIN_COMPLETENESS_RATIO


def add_table_context_columns(df, competition=""):
    required = {"HomeTeam", "AwayTeam", "FTR", "FTHG", "FTAG"}
    if not required.issubset(df.columns):
        return df

    use_h2h = competition in H2H_TIEBREAKER_PREFIXES

    teams = sorted(set(df["HomeTeam"].dropna()) | set(df["AwayTeam"].dropna()))
    table = {
        team: {"points": 0, "gf": 0, "ga": 0, "gd": 0, "played": 0}
        for team in teams
    }
    # Track head-to-head between every pair: h2h[a][b] = {pts, gd, gf, ga}
    h2h = defaultdict(lambda: defaultdict(lambda: {"pts": 0, "gd": 0, "gf": 0, "ga": 0}))
    position_map = {team: idx + 1 for idx, team in enumerate(teams)}

    home_points_before = []
    away_points_before = []
    home_pos_before = []
    away_pos_before = []

    def rank_positions():
        if not use_h2h:
            ranked = sorted(
                teams,
                key=lambda t: (
                    -table[t]["points"],
                    -table[t]["gd"],
                    -table[t]["gf"],
                    t,
                ),
            )
        else:
            pts_groups = defaultdict(list)
            for t in teams:
                pts_groups[table[t]["points"]].append(t)
            ranked = []
            for pts in sorted(pts_groups, reverse=True):
                tied = pts_groups[pts]
                if len(tied) == 1:
                    ranked.append(tied[0])
                else:
                    h2h_scores = defaultdict(lambda: {"pts": 0, "gd": 0, "gf": 0})
                    for t1 in tied:
                        for t2 in tied:
                            if t1 == t2:
                                continue
                            rec = h2h[t1].get(t2, {})
                            if rec:
                                h2h_scores[t1]["pts"] += rec.get("pts", 0)
                                h2h_scores[t1]["gd"] += rec.get("gd", 0)
                                h2h_scores[t1]["gf"] += rec.get("gf", 0)
                    sorted_tied = sorted(
                        tied,
                        key=lambda t: (
                            -h2h_scores[t]["pts"],
                            -h2h_scores[t]["gd"],
                            -h2h_scores[t]["gf"],
                            -table[t]["gd"],
                            -table[t]["gf"],
                            t,
                        ),
                    )
                    ranked.extend(sorted_tied)
        return {team: pos + 1 for pos, team in enumerate(ranked)}

    for row in df.itertuples(index=False):
        home = row.HomeTeam
        away = row.AwayTeam

        home_points_before.append(float(table.get(home, {}).get("points", 0)))
        away_points_before.append(float(table.get(away, {}).get("points", 0)))
        home_pos_before.append(float(position_map.get(home, len(teams))))
        away_pos_before.append(float(position_map.get(away, len(teams))))

        if home not in table or away not in table:
            continue

        hg = row.FTHG
        ag = row.FTAG
        ftr = row.FTR
        if pd.isna(hg) or pd.isna(ag) or pd.isna(ftr):
            continue

        hg = int(hg)
        ag = int(ag)
        table[home]["played"] += 1
        table[away]["played"] += 1
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

        if use_h2h:
            h2h_rec = h2h[home][away]
            h2h_rev = h2h[away][home]
            if ftr == "H":
                h2h_rec["pts"] += 3
            elif ftr == "A":
                h2h_rev["pts"] += 3
            else:
                h2h_rec["pts"] += 1
                h2h_rev["pts"] += 1
            h2h_rec["gd"] += hg - ag
            h2h_rec["gf"] += hg
            h2h_rec["ga"] += ag
            h2h_rev["gd"] += ag - hg
            h2h_rev["gf"] += ag
            h2h_rev["ga"] += hg

        position_map = rank_positions()

    df["HomePointsBefore"] = home_points_before
    df["AwayPointsBefore"] = away_points_before
    df["HomeLeaguePosBefore"] = home_pos_before
    df["AwayLeaguePosBefore"] = away_pos_before
    return df


def process_one_file(rel_path, *, force_min_rows=None):
    file_path = os.path.join(RAW_FOLDER, rel_path)
    season_start_year = parse_season_start_year(os.path.basename(rel_path))
    if season_start_year is None:
        return False, rel_path, "skipped_invalid_name"

    fname = os.path.basename(rel_path)
    prefix_match = re.match(r"^([a-z0-9]+stat)\d{4}-\d{2}\.csv$", fname, re.IGNORECASE)
    competition_prefix = prefix_match.group(1).lower() if prefix_match else ""

    df = read_csv_fast(file_path)
    if not has_required_general_data(
        df,
        season_start_year,
        competition_prefix,
        file_name=fname,
        force_min_rows=force_min_rows,
    ):
        return False, rel_path, "skipped_insufficient_data"

    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, format="mixed", errors="coerce")

    available_columns = [col for col in columns if col in df.columns]
    df = df[available_columns]
    # Downstream Sort_Data.py accesses HS/HST/AS/AST unconditionally; leagues
    # without shot statistics (see NO_SHOT_STATS_PREFIXES) still need these
    # columns present, just left blank/NaN rather than omitted entirely.
    for shot_col in ("HS", "HST", "AS", "AST"):
        if shot_col not in df.columns:
            df[shot_col] = pd.NA

    if "Date" in df.columns:
        df = df.sort_values("Date")

    df = add_table_context_columns(df, competition=competition_prefix)

    if "FTR" in df.columns:
        df["ResultNum"] = df["FTR"].map(result_map)

    output_path = os.path.join(PROCESSED_FOLDER, rel_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    # Only rewrite the processed file if its content actually changed.
    # (This preserves the mtime — and downstream the model-cache fingerprint —
    # when re-processing already-current data.)
    new_csv = df.to_csv(index=False)
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
    _t0 = time.monotonic()
    if not os.path.isdir(RAW_FOLDER):
        raise FileNotFoundError(f"Raw data folder not found: {RAW_FOLDER}")

    os.makedirs(PROCESSED_FOLDER, exist_ok=True)

    target_files = get_target_season_files(RAW_FOLDER)
    if not target_files:
        raise ValueError("No valid files were found in Raw_Data.")

    workers = max(1, PROCESS_WORKERS)
    processed_comps = set()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_one_file, rel_path): rel_path for rel_path in target_files}
        for future in as_completed(futures):
            ok, rel_path, status = future.result()
            if status == "processed":
                print(f"Processed {rel_path}...")
                processed_comps.add(os.path.dirname(rel_path).replace("\\", "/"))
            elif status == "kept_content_unchanged":
                processed_comps.add(os.path.dirname(rel_path).replace("\\", "/"))
            elif status == "skipped_insufficient_data":
                print(f"Skipped {rel_path} (insufficient data)...")
            elif status == "skipped_invalid_name":
                print(f"Skipped {rel_path} (invalid season name)...")

    # If every season for a competition failed the normal bar, still keep the
    # latest raw file with at least one valid row so cup/projection fallbacks
    # and early-season tables have something to work with.
    latest_by_comp = {}
    for rel_path in target_files:
        comp = os.path.dirname(rel_path).replace("\\", "/") or "Unknown"
        start_year = parse_season_start_year(os.path.basename(rel_path)) or 0
        prev = latest_by_comp.get(comp)
        if prev is None or start_year > prev[0]:
            latest_by_comp[comp] = (start_year, rel_path)
    for comp, (_, rel_path) in sorted(latest_by_comp.items()):
        if comp in processed_comps:
            continue
        ok, _, status = process_one_file(rel_path, force_min_rows=CURRENT_SEASON_MIN_ROWS)
        if ok:
            print(f"Processed {rel_path} (fallback: only sparse raw data for {comp})...")
        else:
            print(f"Skipped {rel_path} (fallback still insufficient for {comp})...")

    print(f"All files processed. ({time.monotonic() - _t0:.1f}s)")


if __name__ == "__main__":
    main()
