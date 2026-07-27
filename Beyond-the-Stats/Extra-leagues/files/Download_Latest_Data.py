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
    # Americas / Asia only — Liga MX moved to MLS pipeline; Austria/Romania/Poland
    # moved to the global European pipeline (see files/Download_Latest_Data.py).
    {"country": "Argentina",      "league": "Primera Division",    "type": "new",  "url": "https://www.football-data.co.uk/new/ARG.csv",  "file_prefix": "argstat"},
    {"country": "Brazil",         "league": "Brasileirão",         "type": "new",  "url": "https://www.football-data.co.uk/new/BRA.csv",  "file_prefix": "brastat"},
    {"country": "Japan",          "league": "J1 League",           "type": "new",  "url": "https://www.football-data.co.uk/new/JPN.csv",  "file_prefix": "jpnstat"},
]

# ---------------------------------------------------------------------------
# NOTE on leagues removed from this list (2026 audit):
#
# - Norway (Eliteserien) and Sweden (Allsvenskan) were moved to the regular
#   ``files/Download_Latest_Data.py`` pipeline: they have real, correctly
#   sourced "new"-format data and their clubs regularly qualify for UEFA
#   Champions/Europa/Conference League, so real domestic results should feed
#   the same database the cup predictor consults instead of always falling
#   back to synthetic UEFA-coefficient estimates.
# - Greece (Super League) was moved to the regular pipeline too, using the
#   real mmz4281 ``G1`` code (verified to return genuine Greek Super League
#   data with full match statistics).
# - Denmark ("DK1"), Czech Republic ("CE1"), Israel ("I1"), Serbia ("SE1"),
#   Bulgaria ("BU1"), Cyprus ("CP1"), Belarus ("BL1") and Moldova ("MD1")
#   were REMOVED entirely rather than moved: these mmz4281 league codes are
#   stale/incorrect and currently silently redirect to a completely
#   different country's data (verified: "DK1" -> Germany Bundesliga,
#   "CE1"/"SE1" -> England Championship, "I1" -> Italy Serie A,
#   "BU1" -> Belgium First Division A, "CP1" -> Portugal Liga Portugal,
#   "BL1" -> Belgium First Division A, "MD1" -> Germany Bundesliga).
#   football-data.co.uk does not currently publish a legitimate feed for
#   any of these five countries (confirmed via https://football-data.co.uk/
#   data.php and https://football-data.co.uk/all_new_data.php, and by
#   probing https://www.football-data.co.uk/new/{CODE}.csv, which 404s for
#   ISR/SRB/BUL/BGR/CYP/CZE). Rather than keep training on mislabeled data,
#   these five European leagues now rely entirely on the UEFA fallback path
#   (uefa_country_coefficients.json + team registry + ESPN domestic tables +
#   squad values, see UEFA_Data_Manager.py / inject_fallback_team()) for any
#   of their clubs that appear in European-cup fixtures — which is exactly
#   the "team not in database" fallback the fixture predictor already uses.
# ---------------------------------------------------------------------------

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

    # Legacy folder rename: Brazil/Serie A → Brazil/Brasileirão
    for base_name in ("Raw_Data", "Processed_Data"):
        legacy = os.path.join(BASE_DIR, "Data", base_name, "Brazil", "Serie A")
        modern = os.path.join(BASE_DIR, "Data", base_name, "Brazil", "Brasileirão")
        if os.path.isdir(legacy) and not os.path.isdir(modern):
            os.makedirs(os.path.dirname(modern), exist_ok=True)
            os.rename(legacy, modern)
            print(f"Renamed legacy folder: {legacy} → {modern}")

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
    if _sort_data_needed():
        sort_data.sort_all_seasons()
        sort_data.build_current_form_file()
        _touch_sort_tracker()
        print("Sort complete.")
    else:
        print("No processed data changes — skipping Sort_Data (team stats unchanged).")
    print("\nExtra leagues pipeline complete (download + process + sort).")


PROCESSED_DATA_DIR = os.path.join(BASE_DIR, "Data", "Processed_Data")
TEAM_DATA_DIR = os.path.join(BASE_DIR, "Data", "Team_Data")
SORT_TRACKER_FILE = os.path.join(TEAM_DATA_DIR, ".sort_tracker")


def _sort_data_needed():
    """Return True if any processed CSV has been modified since last sort."""
    if not os.path.exists(SORT_TRACKER_FILE):
        return True
    last_mtime = os.path.getmtime(SORT_TRACKER_FILE)
    for root, _, files in os.walk(PROCESSED_DATA_DIR):
        for fname in files:
            if not fname.endswith(".csv"):
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
