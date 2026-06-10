import os
import urllib.request
from datetime import datetime
from io import StringIO

import pandas as pd
import Process_Data as process_data
import Sort_Data as sort_data


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DATA_DIR = os.path.join(BASE_DIR, "Data", "Raw_Data")

# Source format expected (football-data.co.uk "new" CSVs):
# Columns: Season, Date, Home, Away, HG, AG, Res, ...
SOURCES = [
    {
        "country": "Argentina",
        "league": "Primera Division",
        "url": "https://www.football-data.co.uk/new/ARG.csv",
        "file_prefix": "argstat",
    },
    {
        "country": "Brazil",
        "league": "Serie A",
        "url": "https://www.football-data.co.uk/new/BRA.csv",
        "file_prefix": "brastat",
    },
    {
        "country": "Japan",
        "league": "J1 League",
        "url": "https://www.football-data.co.uk/new/JPN.csv",
        "file_prefix": "jpnstat",
    },
    {
        "country": "Mexico",
        "league": "Liga MX",
        "url": "https://www.football-data.co.uk/new/MEX.csv",
        "file_prefix": "mexstat",
    },
]

RAW_REQUIRED_COLUMNS = ["Season", "Date", "Home", "Away", "HG", "AG", "Res"]
MIN_START_YEAR = 2002
REFRESH_RECENT_SEASONS = 2


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


def main():
    os.makedirs(RAW_DATA_DIR, exist_ok=True)
    current_year = datetime.now().year

    for source in SOURCES:
        country = source["country"]
        league = source["league"]
        url = source["url"]
        prefix = source["file_prefix"]

        target_dir = os.path.join(RAW_DATA_DIR, country, league)
        os.makedirs(target_dir, exist_ok=True)

        print(f"\nDownloading {country} - {league}")
        try:
            df = fetch_source_dataframe(url)
        except Exception as exc:
            print(f"Failed to download {url}: {exc}")
            continue

        if any(col not in df.columns for col in RAW_REQUIRED_COLUMNS):
            print("Source CSV missing required columns; skipping.")
            continue

        df = df.copy()
        df["SeasonInt"] = df["Season"].map(normalize_season)
        df = df[df["SeasonInt"].notna()]

        valid_years = sorted(
            {
                int(year)
                for year in df["SeasonInt"].unique().tolist()
                if MIN_START_YEAR <= int(year) <= current_year
            }
        )

        updated_count = 0
        skipped_existing_count = 0
        for start_year in valid_years:
            season_rows = df[df["SeasonInt"] == start_year].copy()
            if season_rows.empty:
                continue
            out_name = season_file_name(prefix, start_year)
            out_path = os.path.join(target_dir, out_name)

            refresh_cutoff = current_year - REFRESH_RECENT_SEASONS
            should_refresh = start_year >= refresh_cutoff
            if os.path.exists(out_path) and not should_refresh:
                print(f"Kept {out_name}")
                skipped_existing_count += 1
                continue

            season_rows.to_csv(out_path, index=False)
            updated_count += 1
            print(f"Downloaded/Updated {out_name} ({len(season_rows)} rows)")

        print(
            f"Done {country} - {league}: updated {updated_count} season files, "
            f"skipped {skipped_existing_count} historical files."
        )

    print("\nExtra leagues download complete.")
    print("\nProcessing extra-league files...")
    process_data.main()
    print("\nSorting extra-league team data...")
    sort_data.sort_all_seasons()
    sort_data.build_current_form_file()
    print("\nExtra leagues pipeline complete (download + process + sort).")


if __name__ == "__main__":
    main()
