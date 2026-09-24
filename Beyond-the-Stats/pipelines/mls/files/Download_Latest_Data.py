"""
Download MLS + Liga MX fixture data from football-data.co.uk.

Mirrors ``files/Download_Latest_Data.py`` but configured for CONCACAF club
competitions (MLS and Liga MX).  Fetches CSVs, then immediately processes
and sorts them (imports sibling ``Process_Data`` and ``Sort_Data``).
"""

import os as _os_paths_setup
import sys as _sys_paths_setup
_FILES_DIR = _os_paths_setup.path.dirname(_os_paths_setup.path.abspath(__file__))
_REGION_DIR = _os_paths_setup.path.dirname(_FILES_DIR)  # pipelines/mls
_SP_DIR = _os_paths_setup.path.dirname(_os_paths_setup.path.dirname(_REGION_DIR))
if _SP_DIR not in _sys_paths_setup.path:
    _sys_paths_setup.path.insert(0, _SP_DIR)
from shared import paths as _bts_paths
if str(_bts_paths.SHARED_DIR) not in _sys_paths_setup.path:
    _sys_paths_setup.path.insert(0, str(_bts_paths.SHARED_DIR))
BASE_DIR = str(_bts_paths.MLS_DIR)
PREDICTIONS_DIR = str(_bts_paths.OUTPUT_PRED_MLS)
PROJECT_DIR = str(_bts_paths.SP_DIR)
import argparse
import os
import sys
import time
from datetime import datetime
from io import StringIO
import urllib.error
import urllib.request
import re

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import Process_Data as process_data
import Sort_Data as sort_data
import Download_Mexico_Data as download_mexico


# BASE_DIR set by shared.paths bootstrap above
RAW_DATA_DIR = os.path.join(BASE_DIR, "Data", "Raw_Data")
TARGET_DIR = os.path.join(RAW_DATA_DIR, "United States", "MLS")

# MLS source page: https://www.football-data.co.uk/usa.php
# Direct CSV contains many seasons in one file.
MLS_SOURCE_URL = "https://www.football-data.co.uk/new/USA.csv"
FILE_PREFIX = "mlsstat"
MIN_START_YEAR = 2002
REFRESH_RECENT_SEASONS = 2
LEGACY_FILE_PATTERN = re.compile(rf"^{FILE_PREFIX}\d{{4}}-\d{{2}}\.csv$", re.IGNORECASE)

RAW_REQUIRED_COLUMNS = ["Season", "Date", "Home", "Away", "HG", "AG", "Res"]


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-squad-values",
        action="store_true",
        help="Skip Transfermarkt squad value scraping (weekly-only operation).",
    )
    return parser.parse_args()


def season_label(start_year):
    return f"{start_year}"


def season_file_name(start_year):
    return f"{FILE_PREFIX}{season_label(start_year)}.csv"


def _urlopen_with_retry(url, timeout=30, attempts=4, backoff_base=2.0):
    """GET a URL with bounded retries.

    A single transient network error used to abort this step, which tripped the
    pipeline's fail-fast barrier and skipped the cup steps. Retry with
    exponential backoff so brief outages self-heal. Permanent 4xx responses
    (e.g. 404) still raise immediately.
    """
    delay = backoff_base
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                raise
            last_exc = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_exc = exc
        if attempt < attempts:
            print(f"  [retry] {url} failed ({last_exc}); retrying in {delay:.0f}s ({attempt}/{attempts - 1})...")
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise last_exc


def fetch_source_dataframe():
    raw = _urlopen_with_retry(MLS_SOURCE_URL, timeout=30)
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


def main():
    _t0 = time.monotonic()
    args = parse_args()
    os.makedirs(TARGET_DIR, exist_ok=True)
    current_year = datetime.now().year

    print(f"\nDownloading MLS source: {MLS_SOURCE_URL}")
    source = None
    try:
        source = fetch_source_dataframe()
    except Exception as exc:
        existing_raw = [
            f for f in os.listdir(TARGET_DIR)
            if f.lower().startswith(FILE_PREFIX.lower())
        ]
        if not existing_raw:
            raise
        print(
            f"[warn] MLS download failed ({exc}); falling back to "
            f"{len(existing_raw)} existing raw file(s) in {TARGET_DIR}"
        )

    valid_years = []
    updated_count = 0
    skipped_existing_count = 0
    if source is not None:
        if any(col not in source.columns for col in RAW_REQUIRED_COLUMNS):
            raise ValueError("MLS source CSV does not contain expected columns.")

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

    for start_year in valid_years:
        season_rows = source[source["SeasonInt"] == start_year].copy()
        if season_rows.empty:
            continue
        out_name = season_file_name(start_year)
        out_path = os.path.join(TARGET_DIR, out_name)
        legacy_name = f"{FILE_PREFIX}{start_year}-{str(start_year + 1)[-2:]}.csv"
        legacy_path = os.path.join(TARGET_DIR, legacy_name)

        # Older historical files are stable; refresh only recent seasons.
        refresh_cutoff = current_year - REFRESH_RECENT_SEASONS
        should_refresh = start_year >= refresh_cutoff
        if os.path.exists(out_path) and not should_refresh:
            skipped_existing_count += 1
            continue

        season_rows.to_csv(out_path, index=False)
        updated_count += 1
        print(f"Downloaded/Updated {out_name} ({len(season_rows)} rows)")
        if os.path.exists(legacy_path):
            os.remove(legacy_path)

    # Remove any remaining legacy season files with YYYY-YY naming.
    for file_name in os.listdir(TARGET_DIR):
        if LEGACY_FILE_PATTERN.match(file_name):
            os.remove(os.path.join(TARGET_DIR, file_name))

    processed_target_dir = os.path.join(BASE_DIR, "Data", "Processed_Data", "United States", "MLS")
    if os.path.isdir(processed_target_dir):
        for file_name in os.listdir(processed_target_dir):
            if LEGACY_FILE_PATTERN.match(file_name):
                os.remove(os.path.join(processed_target_dir, file_name))

    print(
        f"\nDownload stage done. Updated {updated_count} season files, "
        f"skipped {skipped_existing_count} existing historical files. ({time.monotonic() - _t0:.1f}s)"
    )

    _t1 = time.monotonic()
    print("\nDownloading Liga MX source data...")
    try:
        download_mexico.main()
        print(f"Liga MX download done. ({time.monotonic() - _t1:.1f}s)")
    except Exception as exc:
        print(f"[warn] Liga MX download failed ({exc}); continuing with existing raw data.")

    _t2 = time.monotonic()
    print("\nProcessing MLS + Liga MX files...")
    process_data.main()
    print(f"Processing done. ({time.monotonic() - _t2:.1f}s)")

    _t3 = time.monotonic()
    print("\nSorting MLS + Liga MX team data...")
    if _sort_data_needed():
        sort_data.sort_all_seasons()
        sort_data.build_current_form_file()
        if not args.skip_squad_values:
            try:
                sort_data.build_squad_values_file()
            except Exception as exc:
                print(f"[warn] Squad values update failed ({exc}); continuing with existing squad values.")
        _touch_sort_tracker()
        print(f"Sort complete. ({time.monotonic() - _t3:.1f}s)")
    else:
        print(f"No processed data changes — skipping Sort_Data (team stats unchanged). ({time.monotonic() - _t3:.1f}s)")
    print(f"\nMLS + Liga MX pipeline complete (download + process + sort). ({time.monotonic() - _t0:.1f}s)")


PROCESSED_DATA_DIR = os.path.join(BASE_DIR, "Data", "Processed_Data")
TEAM_DATA_DIR = os.path.join(BASE_DIR, "Data", "Team_Data")
SORT_TRACKER_FILE = os.path.join(TEAM_DATA_DIR, ".sort_tracker")


def _sort_data_needed():
    """Return True if any processed CSV (matching MLS pattern *statYYYY.csv) has been modified since last sort."""
    if not os.path.exists(SORT_TRACKER_FILE):
        return True
    last_mtime = os.path.getmtime(SORT_TRACKER_FILE)
    for root, _, files in os.walk(PROCESSED_DATA_DIR):
        for fname in files:
            if not fname.endswith(".csv"):
                continue
            if not re.search(r"[a-z0-9]+stat\d{4}\.csv$", fname, re.I):
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
