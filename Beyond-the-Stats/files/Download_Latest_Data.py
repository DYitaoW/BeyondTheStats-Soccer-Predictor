"""
Download raw fixture data from football-data.co.uk for European club leagues.

Part of the global sub-pipeline (global).  Fetches CSVs for each configured
competition + past / current season, saved under ``Data/Raw_Data/``.

Season completeness: once past June 1 of a season's end year, that season is
marked complete and never re-downloaded.  The 2025-26 / 2026-27 seasons use
unique filenames so they coexist in the same directory.
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from io import StringIO

import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
import season_calendar

sys.path.insert(0, os.path.dirname(__file__))
import Process_Data as process_data
import Sort_Data as sort_data

RAW_DATA_DIR = os.path.join(BASE_DIR, "Data", "Raw_Data")
PROCESSED_DATA_DIR = os.path.join(BASE_DIR, "Data", "Processed_Data")
TEAM_DATA_DIR = os.path.join(BASE_DIR, "Data", "Team_Data")
SORT_TRACKER_FILE = os.path.join(TEAM_DATA_DIR, ".sort_tracker")


# Data source format expected:
# https://www.football-data.co.uk/mmz4281/{season_code}/{league_code}.csv
# Example season_code: 2526 (for 2025-26)
# Example league_code: E0 (England Premier League), SP1 (Spain La Liga), D1 (Germany Bundesliga)
URL_TEMPLATE = "https://www.football-data.co.uk/mmz4281/{season_code}/{league_code}.csv"

# Add/remove competitions here.
# NOTE: Greece was moved here from Extra-leagues (real, correctly-coded mmz4281
# data with full match statistics) since its clubs regularly appear in UEFA
# Champions/Europa/Conference League qualifiers.
COMPETITIONS = [
    {"country": "England", "league": "Premier League", "league_code": "E0", "file_prefix": "premstat"},
    {"country": "England", "league": "Championship", "league_code": "E1", "file_prefix": "champstat"},
    {"country": "Spain", "league": "La Liga", "league_code": "SP1", "file_prefix": "laligastat"},
    {"country": "Spain", "league": "La Liga 2", "league_code": "SP2", "file_prefix": "laliga2stat"},
    {"country": "Italy", "league": "Serie A", "league_code": "I1", "file_prefix": "seriaastat"},
    {"country": "Italy", "league": "Serie B", "league_code": "I2", "file_prefix": "seriabstat"},
    {"country": "Germany", "league": "Bundesliga", "league_code": "D1", "file_prefix": "bundstat"},
    {"country": "Germany", "league": "Bundesliga 2", "league_code": "D2", "file_prefix": "bund2stat"},
    {"country": "France", "league": "Ligue 1", "league_code": "F1", "file_prefix": "ligue1stat"},
    {"country": "France", "league": "Ligue 2", "league_code": "F2", "file_prefix": "ligue2stat"},
    {"country": "Portugal", "league": "Liga Portugal", "league_code": "P1", "file_prefix": "portstat"},
    {"country": "Netherlands", "league": "Eredivisie", "league_code": "N1", "file_prefix": "eredivisiestat"},
    {"country": "Belgium", "league": "First Division A", "league_code": "B1", "file_prefix": "belgiestat"},
    {"country": "Scotland", "league": "Premiership", "league_code": "SC0", "file_prefix": "scotpremstat"},
    {"country": "Turkey", "league": "Super Lig", "league_code": "T1", "file_prefix": "turkstat"},
    {"country": "Greece", "league": "Super League", "league_code": "G1", "file_prefix": "grecstat"},
]

# ---------------------------------------------------------------------------
# "New" single-CSV-per-country sources (football-data.co.uk/new/{code}.csv).
#
# Norway and Sweden were moved here from Extra-leagues: they don't have
# per-season mmz4281 codes (their whole history ships as one CSV without
# shot statistics), but their clubs are regular UEFA Champions/Europa/
# Conference League qualifiers, so their real domestic results should feed
# the same database used for European-cup fallback lookups instead of always
# relying on synthetic estimates. Missing HS/HST/AS/AST are left blank and
# treated as -1 sentinels downstream (see Process_Data.py / Predict_Match.py).
# ---------------------------------------------------------------------------
NEW_FORMAT_BASE_URL = "https://www.football-data.co.uk/new/{code}.csv"
NEW_FORMAT_COMPETITIONS = [
    {"country": "Norway", "league": "Eliteserien", "code": "NOR", "file_prefix": "norstat"},
    {"country": "Sweden", "league": "Allsvenskan", "code": "SWE", "file_prefix": "swestat"},
    # Moved from Extra-leagues (2026): UEFA-regular domestic leagues with real
    # football-data.co.uk "new" format feeds.
    {"country": "Austria", "league": "Bundesliga", "code": "AUT", "file_prefix": "autstat"},
    {"country": "Romania", "league": "Liga I", "code": "ROU", "file_prefix": "roustat"},
    {"country": "Poland", "league": "Ekstraklasa", "code": "POL", "file_prefix": "polstat"},
]
NEW_FORMAT_REQUIRED_COLUMNS = ["Season", "Date", "Home", "Away", "HG", "AG", "Res"]
NEW_FORMAT_MIN_ROWS = 100
NEW_FORMAT_CURRENT_SEASON_MIN_ROWS = season_calendar.CURRENT_SEASON_MIN_ROWS

GENERAL_REQUIRED_COLUMNS = ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR", "HS", "HST", "AS", "AST"]
MIN_COMPLETENESS_RATIO = 0.90
MIN_ROWS = 200
CURRENT_SEASON_MIN_ROWS = season_calendar.CURRENT_SEASON_MIN_ROWS
MIN_START_YEAR = 2002
REFRESH_RECENT_SEASONS = 2


def make_season_code(start_year):
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def season_label(start_year):
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def download_bytes(url):
    with urllib.request.urlopen(url, timeout=15) as response:
        return response.read()


def has_required_general_data(csv_bytes, start_year, current_year, file_prefix=""):
    try:
        text = csv_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = csv_bytes.decode("latin-1", errors="replace")

    try:
        df = pd.read_csv(StringIO(text))
    except Exception:
        try:
            df = pd.read_csv(StringIO(text), engine="python", on_bad_lines="skip")
        except Exception:
            return False

    if any(col not in df.columns for col in GENERAL_REQUIRED_COLUMNS):
        return False

    # Keep the active season (e.g. 2026-27 from Jul 15 2026) with a 1-row floor.
    # Before the flip / early Jul–Aug the next-season file often does not exist yet.
    file_name = f"{file_prefix}{start_year}-{(start_year + 1) % 100:02d}.csv" if file_prefix else ""
    in_progress_season = season_calendar.is_in_progress_season(
        start_year, file_name, current_year=current_year
    )
    if in_progress_season:
        return len(df) >= CURRENT_SEASON_MIN_ROWS

    if len(df) < MIN_ROWS:
        return False

    complete_rows = df[GENERAL_REQUIRED_COLUMNS].notna().all(axis=1).mean()
    return complete_rows >= MIN_COMPLETENESS_RATIO


def write_bytes(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as file:
        file.write(content)


def fetch_new_format_dataframe(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        raw = response.read()
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        return pd.read_csv(StringIO(text))
    except Exception:
        text = raw.decode("latin-1", errors="replace")
        return pd.read_csv(StringIO(text), engine="python", on_bad_lines="skip")


def normalize_new_format_season(value):
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none"}:
        return None
    if "/" in text:
        try:
            return int(text.split("/")[0])
        except Exception:
            return None
    try:
        return int(float(value))
    except Exception:
        return None


def download_new_format_competition(source, current_year):
    """Download+split a "new" single-CSV-per-country source (Norway, Sweden).

    Writes one file per season using the same ``{prefix}{start}-{end2}.csv``
    naming as the mmz4281 competitions so Process_Data.py / Predict_Match.py
    pick them up transparently. Shot columns (HS/HST/AS/AST) are absent from
    this source and are simply left out of the written CSV; downstream code
    treats missing shot stats as -1 sentinels rather than dropping the match.
    """
    country = source["country"]
    league = source["league"]
    prefix = source["file_prefix"]
    target_dir = os.path.join(RAW_DATA_DIR, country, league)
    os.makedirs(target_dir, exist_ok=True)
    print(f"\nDownloading {country} - {league} ({source['code']}, new format)")

    url = NEW_FORMAT_BASE_URL.format(code=source["code"])
    try:
        df = fetch_new_format_dataframe(url)
    except Exception as exc:
        print(f"  Failed to download {url}: {exc}")
        return 0
    if any(col not in df.columns for col in NEW_FORMAT_REQUIRED_COLUMNS):
        print("  Source CSV missing required columns; skipping.")
        return 0

    df = df.copy()
    df["SeasonInt"] = df["Season"].map(normalize_new_format_season)
    df = df[df["SeasonInt"].notna()]
    valid_years = sorted(
        int(y) for y in df["SeasonInt"].unique().tolist()
        if MIN_START_YEAR <= int(y) <= current_year
    )

    updated_count = 0
    refresh_cutoff = current_year - REFRESH_RECENT_SEASONS
    for start_year in valid_years:
        out_name = f"{prefix}{season_label(start_year)}.csv"
        out_path = os.path.join(target_dir, out_name)

        season_complete = start_year < refresh_cutoff
        if os.path.exists(out_path) and season_complete:
            continue

        season_rows = df[df["SeasonInt"] == start_year].drop(columns=["SeasonInt"]).copy()
        if season_rows.empty:
            continue
        in_progress_season = season_calendar.is_in_progress_season(
            start_year, f"{prefix}{season_label(start_year)}.csv", current_year=current_year
        )
        min_rows = NEW_FORMAT_CURRENT_SEASON_MIN_ROWS if in_progress_season else NEW_FORMAT_MIN_ROWS
        if len(season_rows) < min_rows:
            continue

        season_rows = season_rows.rename(
            columns={"Home": "HomeTeam", "Away": "AwayTeam", "HG": "FTHG", "AG": "FTAG", "Res": "FTR"}
        )
        for col in ["HS", "HST", "AS", "AST"]:
            if col not in season_rows.columns:
                season_rows[col] = ""

        season_rows.to_csv(out_path, index=False)
        updated_count += 1
        print(f"  {out_name} ({len(season_rows)} rows)")

    return updated_count


def main():
    _t0 = time.monotonic()
    os.makedirs(RAW_DATA_DIR, exist_ok=True)
    current_year = datetime.now().year
    kept_count = 0
    skipped_existing_count = 0

    for comp in COMPETITIONS:
        country = comp["country"]
        league = comp["league"]
        league_code = comp["league_code"]
        prefix = comp["file_prefix"]

        target_dir = os.path.join(RAW_DATA_DIR, country, league)
        os.makedirs(target_dir, exist_ok=True)
        print(f"\nDownloading {country} - {league} ({league_code})")

        for start_year in range(MIN_START_YEAR, current_year + 1):
            season_code = make_season_code(start_year)
            url = URL_TEMPLATE.format(season_code=season_code, league_code=league_code)
            name = f"{prefix}{season_label(start_year)}.csv"
            out_path = os.path.join(target_dir, name)

            # Older historical files are stable; only refresh recent seasons.
            # European seasons (Aug-May) are considered complete once past June 1 of end_year.
            now = datetime.now()
            season_end_year = start_year + 1
            season_complete = (now.year > season_end_year or
                               (now.year == season_end_year and now.month >= 6))
            refresh_cutoff = current_year - REFRESH_RECENT_SEASONS
            should_refresh = start_year >= refresh_cutoff and not season_complete
            if os.path.exists(out_path) and not should_refresh:
                skipped_existing_count += 1
                continue

            try:
                csv_bytes = download_bytes(url)
            except Exception:
                continue

            if not has_required_general_data(csv_bytes, start_year, current_year, file_prefix=prefix):
                continue

            write_bytes(out_path, csv_bytes)
            kept_count += 1
            print(f"Downloaded/Updated {name}")

    for source in NEW_FORMAT_COMPETITIONS:
        kept_count += download_new_format_competition(source, current_year)

    print(f"\nDone. Updated {kept_count} CSV files, kept {skipped_existing_count} existing unchanged, across all configured competitions. ({time.monotonic() - _t0:.1f}s)")

    _t1 = time.monotonic()
    print("\nProcessing raw data files...")
    process_data.main()

    if _sort_data_needed():
        print("Sorting team data...")
        sort_data.sort_all_seasons()
        sort_data.build_current_form_file()
        _touch_sort_tracker()
        print(f"Sort complete. ({time.monotonic() - _t1:.1f}s)")
    else:
        print(f"No processed data changes — skipping Sort_Data (team stats unchanged). ({time.monotonic() - _t1:.1f}s)")


def _sort_data_needed():
    """Return True if any processed CSV (matching this pipeline's pattern) has been modified since last sort."""
    if not os.path.exists(SORT_TRACKER_FILE):
        return True
    last_mtime = os.path.getmtime(SORT_TRACKER_FILE)
    for root, _, files in os.walk(PROCESSED_DATA_DIR):
        for fname in files:
            if not fname.endswith(".csv"):
                continue
            if not re.search(r"[a-z0-9]+stat\d{4}-\d{2}\.csv$", fname, re.I):
                continue
            fpath = os.path.join(root, fname)
            if os.path.getmtime(fpath) > last_mtime + 1:
                return True
    return False


def _touch_sort_tracker():
    """Write (or touch) the tracker file after a successful Sort_Data run."""
    os.makedirs(TEAM_DATA_DIR, exist_ok=True)
    with open(SORT_TRACKER_FILE, "w") as f:
        f.write(datetime.now().isoformat())


if __name__ == "__main__":
    main()
