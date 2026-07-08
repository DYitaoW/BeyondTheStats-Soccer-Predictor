import os
import urllib.request
from datetime import datetime
from io import StringIO

import pandas as pd


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DATA_DIR = os.path.join(BASE_DIR, "Data", "Raw_Data")
TARGET_DIR = os.path.join(RAW_DATA_DIR, "Mexico", "Liga MX")

# Mexico source page: https://www.football-data.co.uk/mexico.php
# Direct CSV contains many seasons in one file.
MEXICO_SOURCE_URL = "https://www.football-data.co.uk/new/MEX.csv"
FILE_PREFIX = "mexstat"
MIN_START_YEAR = 2002
REFRESH_RECENT_SEASONS = 2

RAW_REQUIRED_COLUMNS = ["Season", "Date", "Home", "Away", "HG", "AG", "Res"]


def season_file_name(start_year):
    return f"{FILE_PREFIX}{start_year}.csv"


def fetch_source_dataframe():
    with urllib.request.urlopen(MEXICO_SOURCE_URL, timeout=30) as response:
        raw = response.read()

    text = raw.decode("utf-8-sig", errors="replace")
    try:
        df = pd.read_csv(StringIO(text))
    except Exception:
        text = raw.decode("latin-1", errors="replace")
        df = pd.read_csv(StringIO(text), engine="python", on_bad_lines="skip")
    return df


def normalize_season(value):
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


def main():
    os.makedirs(TARGET_DIR, exist_ok=True)
    current_year = datetime.now().year

    print(f"\nDownloading Liga MX source: {MEXICO_SOURCE_URL}")
    source = fetch_source_dataframe()
    if any(col not in source.columns for col in RAW_REQUIRED_COLUMNS):
        raise ValueError("Mexico source CSV does not contain expected columns.")

    source = source.copy()
    source["SeasonInt"] = source["Season"].map(normalize_season)
    source = source[source["SeasonInt"].notna()]

    valid_years = sorted(
        {
            int(year)
            for year in source["SeasonInt"].unique().tolist()
            if MIN_START_YEAR <= int(year) <= current_year
        }
    )

    updated_count = 0
    skipped_existing_count = 0
    for start_year in valid_years:
        season_rows = source[source["SeasonInt"] == start_year].copy()
        if season_rows.empty:
            continue
        out_name = season_file_name(start_year)
        out_path = os.path.join(TARGET_DIR, out_name)

        refresh_cutoff = current_year - REFRESH_RECENT_SEASONS
        should_refresh = start_year >= refresh_cutoff
        if os.path.exists(out_path) and not should_refresh:
            skipped_existing_count += 1
            continue

        season_rows.to_csv(out_path, index=False)
        updated_count += 1
        print(f"Saved {out_name} ({len(season_rows)} rows)")

    print(
        f"\nDownload stage done. Updated {updated_count} season files, "
        f"skipped {skipped_existing_count} existing historical files."
    )


if __name__ == "__main__":
    main()
