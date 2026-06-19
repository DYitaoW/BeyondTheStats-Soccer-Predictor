import os
import urllib.request
from datetime import datetime
from io import StringIO

import pandas as pd


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DATA_DIR = os.path.join(BASE_DIR, "Data", "Raw_Data")


# Data source format expected:
# https://www.football-data.co.uk/mmz4281/{season_code}/{league_code}.csv
# Example season_code: 2526 (for 2025-26)
# Example league_code: E0 (England Premier League), SP1 (Spain La Liga), D1 (Germany Bundesliga)
URL_TEMPLATE = "https://www.football-data.co.uk/mmz4281/{season_code}/{league_code}.csv"

# Add/remove competitions here.
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
]

GENERAL_REQUIRED_COLUMNS = ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR", "HS", "HST", "AS", "AST"]
MIN_COMPLETENESS_RATIO = 0.90
MIN_ROWS = 200
CURRENT_SEASON_MIN_ROWS = 20
MIN_START_YEAR = 2002
REFRESH_RECENT_SEASONS = 2


def make_season_code(start_year):
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def season_label(start_year):
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def download_bytes(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read()


def has_required_general_data(csv_bytes, start_year, current_year):
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

    # Keep current in-progress season (e.g. 2025-26 during 2026) with relaxed row/completeness checks.
    in_progress_season = start_year == (current_year - 1)
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


def main():
    os.makedirs(RAW_DATA_DIR, exist_ok=True)
    current_year = datetime.now().year
    kept_count = 0

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
                print(f"Kept {name}")
                continue

            try:
                csv_bytes = download_bytes(url)
            except Exception:
                continue

            if not has_required_general_data(csv_bytes, start_year, current_year):
                continue

            write_bytes(out_path, csv_bytes)
            kept_count += 1
            print(f"Downloaded/Updated {name}")

    print(f"\nDone. Updated {kept_count} CSV files across all configured competitions.")


if __name__ == "__main__":
    main()
