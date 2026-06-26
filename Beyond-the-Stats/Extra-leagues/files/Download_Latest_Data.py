"""
Download Extra Leagues fixture data from football-data.co.uk.

Same pattern as the MLS downloader — fetches lower-tier European leagues,
processes and sorts them.  These leagues share the same ``Process_Data``
and ``Sort_Data`` logic as the global pipeline.

Supports two source formats:
- ``"new"`` — single CSV with "Season" column (ARG, BRA, etc.)
- ``"mmz4281"`` — per-season CSV with ``{season_code}/{league_code}.csv``
"""
import os
import sys
import urllib.request
from datetime import datetime
from io import StringIO

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import Process_Data as process_data
import Sort_Data as sort_data


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DATA_DIR = os.path.join(BASE_DIR, "Data", "Raw_Data")

# ---------------------------------------------------------------------------
# Source definitions
# ---------------------------------------------------------------------------
# "new" format — single CSV from football-data.co.uk/new/{code}.csv
#   Columns: Season, Date, Home, Away, HG, AG, Res, ...
#
# "mmz4281" format — per-season CSVs
#   Columns: Div, Date, HomeTeam, AwayTeam, FTHG, FTAG, FTR, ...
#   url_template receives {season_code} (e.g. "2526") and {league_code}

SOURCES = [
    # ---- Original "new" format sources (keep) ----
    {"country": "Argentina",      "league": "Primera Division",    "type": "new",  "url": "https://www.football-data.co.uk/new/ARG.csv",  "file_prefix": "argstat"},
    {"country": "Brazil",         "league": "Serie A",             "type": "new",  "url": "https://www.football-data.co.uk/new/BRA.csv",  "file_prefix": "brastat"},
    {"country": "Japan",          "league": "J1 League",           "type": "new",  "url": "https://www.football-data.co.uk/new/JPN.csv",  "file_prefix": "jpnstat"},
    {"country": "Mexico",         "league": "Liga MX",             "type": "new",  "url": "https://www.football-data.co.uk/new/MEX.csv",  "file_prefix": "mexstat"},
    # ---- Additional "new" format sources ----
    {"country": "Austria",        "league": "Bundesliga",          "type": "new",  "url": "https://www.football-data.co.uk/new/AUT.csv",  "file_prefix": "autstat"},
    {"country": "Norway",         "league": "Eliteserien",         "type": "new",  "url": "https://www.football-data.co.uk/new/NOR.csv",  "file_prefix": "norstat"},
    {"country": "Romania",        "league": "Liga I",              "type": "new",  "url": "https://www.football-data.co.uk/new/ROU.csv",  "file_prefix": "roustat"},
    {"country": "Sweden",         "league": "Allsvenskan",         "type": "new",  "url": "https://www.football-data.co.uk/new/SWE.csv",  "file_prefix": "swestat"},
    {"country": "Poland",         "league": "Ekstraklasa",         "type": "new",  "url": "https://www.football-data.co.uk/new/POL.csv",  "file_prefix": "polstat"},
    # ---- mmz4281 format sources (seasonal CSVs) ----
    # Greece  (G1)
    {"country": "Greece",         "league": "Super League",        "type": "mmz4281", "league_code": "G1",   "file_prefix": "grecstat"},
    # Denmark (DK1)
    {"country": "Denmark",        "league": "Superliga",           "type": "mmz4281", "league_code": "DK1",  "file_prefix": "denstat"},
    # Czech Republic (CE1)
    {"country": "Czech Republic", "league": "First League",        "type": "mmz4281", "league_code": "CE1",  "file_prefix": "czestat"},
    # Israel (I1)
    {"country": "Israel",         "league": "Premier League",      "type": "mmz4281", "league_code": "I1",   "file_prefix": "isrstat"},
    # Serbia (SE1)
    {"country": "Serbia",         "league": "SuperLiga",           "type": "mmz4281", "league_code": "SE1",  "file_prefix": "srbstat"},
    # Bulgaria (BU1)
    {"country": "Bulgaria",       "league": "First League",        "type": "mmz4281", "league_code": "BU1",  "file_prefix": "bulstat"},
    # Cyprus (CP1)
    {"country": "Cyprus",         "league": "First Division",      "type": "mmz4281", "league_code": "CP1",  "file_prefix": "cyprusstat"},
    # Belarus (BL1)
    {"country": "Belarus",        "league": "Premier League",      "type": "mmz4281", "league_code": "BL1",  "file_prefix": "belstat"},
    # Moldova (MD1)
    {"country": "Moldova",        "league": "Super Liga",          "type": "mmz4281", "league_code": "MD1",  "file_prefix": "moldstat"},
]

NEW_REQUIRED_COLUMNS = ["Season", "Date", "Home", "Away", "HG", "AG", "Res"]
MMZ4281_REQUIRED_COLUMNS = ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"]
MIN_START_YEAR = 2002
REFRESH_RECENT_SEASONS = 2
MMZ4281_BASE_URL = "https://www.football-data.co.uk/mmz4281/{season_code}/{league_code}.csv"


def fetch_source_dataframe(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        raw = response.read()
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        df = pd.read_csv(StringIO(text))
    except Exception:
        text = raw.decode("latin-1", errors="replace")
        df = pd.read_csv(StringIO(text), engine="python", on_bad_lines="skip")
    return df


def normalize_season(value):
    try:
        return int(float(value))
    except Exception:
        return None


def season_file_name(prefix, start_year):
    return f"{prefix}{start_year}.csv"


def _download_mmz4281_season(source, start_year, current_year):
    """Download a single season CSV for an mmz4281 source.

    Returns a DataFrame with columns normalised to the "new" format
    (Season, Date, Home, Away, HG, AG, Res) or None on failure.
    """
    season_code = f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"
    url = MMZ4281_BASE_URL.format(season_code=season_code, league_code=source["league_code"])
    try:
        df = fetch_source_dataframe(url)
    except Exception:
        return None
    if any(col not in df.columns for col in MMZ4281_REQUIRED_COLUMNS):
        return None
    df = df.rename(columns={"HomeTeam": "Home", "AwayTeam": "Away",
                            "FTHG": "HG", "FTAG": "AG", "FTR": "Res"})
    df["Season"] = start_year
    return df


def main():
    os.makedirs(RAW_DATA_DIR, exist_ok=True)
    current_year = datetime.now().year

    for source in SOURCES:
        country = source["country"]
        league = source["league"]
        prefix = source["file_prefix"]
        src_type = source.get("type", "new")
        target_dir = os.path.join(RAW_DATA_DIR, country, league)
        os.makedirs(target_dir, exist_ok=True)
        print(f"\nDownloading {country} - {league} ({src_type})")

        if src_type == "new":
            # Single CSV — all seasons in one file with a Season column
            url = source["url"]
            try:
                df = fetch_source_dataframe(url)
            except Exception as exc:
                print(f"  Failed to download {url}: {exc}")
                continue
            if any(col not in df.columns for col in NEW_REQUIRED_COLUMNS):
                print("  Source CSV missing required columns; skipping.")
                continue
            df = df.copy()
            df["SeasonInt"] = df["Season"].map(normalize_season)
            df = df[df["SeasonInt"].notna()]
            valid_years = sorted(
                int(y) for y in df["SeasonInt"].unique().tolist()
                if MIN_START_YEAR <= int(y) <= current_year
            )
        else:
            # mmz4281 — one CSV per season, downloaded individually
            valid_years = list(range(MIN_START_YEAR, current_year + 1))

        updated_count = 0
        skipped_existing_count = 0
        refresh_cutoff = current_year - REFRESH_RECENT_SEASONS

        for start_year in valid_years:
            out_name = season_file_name(prefix, start_year)
            out_path = os.path.join(target_dir, out_name)

            should_refresh = start_year >= refresh_cutoff
            if os.path.exists(out_path) and not should_refresh:
                skipped_existing_count += 1
                continue

            if src_type == "new":
                season_rows = df[df["SeasonInt"] == start_year].drop(columns=["SeasonInt"]).copy()
                if season_rows.empty:
                    continue
            else:
                season_df = _download_mmz4281_season(source, start_year, current_year)
                if season_df is None or season_df.empty:
                    print(f"  No data for {out_name} (HTTP 404 / empty)")
                    continue
                season_rows = season_df

            season_rows.to_csv(out_path, index=False)
            updated_count += 1
            print(f"  {out_name} ({len(season_rows)} rows)")

        print(f"  Done: updated {updated_count}, skipped {skipped_existing_count} historical.")

    print("\nExtra leagues download complete.")
    print("\nProcessing extra-league files...")
    process_data.main()
    print("\nSorting extra-league team data...")
    sort_data.sort_all_seasons()
    sort_data.build_current_form_file()
    print("\nExtra leagues pipeline complete (download + process + sort).")


if __name__ == "__main__":
    main()
