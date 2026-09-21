"""Canonical filesystem paths for Beyond-the-Stats.

Import from here (or from Website.config which re-exports the same roots)
instead of hard-coding ``Data/Predictions`` / ``MLS/files`` / etc.

Layout (post-reorg)::

    Beyond-the-Stats/
      main/                 # pipeline + backend entry points
      pipelines/
        europe/files/       # European / global scripts
        mls/files/ + Data/  # MLS (+ Liga MX helpers)
        extra/files/ + Data/
      shared/               # cross-cutting libraries
      Website/              # Flask API + templates
      Output/               # ALL generated artifacts
        Predictions/{europe,mls,extra,cups,national,friendlies,shared}/
        LeagueData/ CupData/ Upcoming/ Status/ Logs/ Cache/
      Data/                 # shared input / seeds / national / info
      docs/ deploy/ public/ scripts/ tests/
"""
from __future__ import annotations

import os
from pathlib import Path

# Beyond-the-Stats/
SP_DIR = Path(__file__).resolve().parent.parent
# Repo root (parent of Beyond-the-Stats/)
REPO_ROOT = SP_DIR.parent

MAIN_DIR = SP_DIR / "main"
BACKEND_DIR = MAIN_DIR / "Backend"
SHARED_DIR = SP_DIR / "shared"
WEBSITE_DIR = SP_DIR / "Website"
PIPELINES_DIR = SP_DIR / "pipelines"
SCRIPTS_DIR = SP_DIR / "scripts"
DOCS_DIR = SP_DIR / "docs"
DEPLOY_DIR = SP_DIR / "deploy"
PUBLIC_DIR = SP_DIR / "public"
TESTS_DIR = SP_DIR / "tests"

# Per-region pipeline roots (scripts live in <region>/files/)
EUROPE_DIR = PIPELINES_DIR / "europe"
MLS_DIR = PIPELINES_DIR / "mls"
EXTRA_DIR = PIPELINES_DIR / "extra"

EUROPE_FILES_DIR = EUROPE_DIR / "files"
MLS_FILES_DIR = MLS_DIR / "files"
EXTRA_FILES_DIR = EXTRA_DIR / "files"

# Shared non-generated / seed data (+ Europe working Raw/Processed/Team_Data)
DATA_DIR = SP_DIR / "Data"
DATA_INFO_DIR = DATA_DIR / "Info"
DATA_NATIONAL_DIR = DATA_DIR / "National_Team_Data"
DATA_SEEDS_DIR = DATA_DIR / "Seeds"
DATA_TEAM_DIR = DATA_DIR / "Team_Data"  # legacy shared team artifacts

# Working data (raw / processed / team stats) per region
# Europe still uses the project-level ``Beyond-the-Stats/Data/`` tree (same as
# pre-reorg). MLS / Extra keep region-local Data under pipelines/<region>/.
EUROPE_DATA_DIR = DATA_DIR  # alias: not pipelines/europe/Data
MLS_DATA_DIR = MLS_DIR / "Data"
EXTRA_DATA_DIR = EXTRA_DIR / "Data"

# Repo-level shared mapping (outside Beyond-the-Stats/)
REPO_DATA_DIR = REPO_ROOT / "Data"
TEAM_NAME_MAPPING_MASTER = REPO_DATA_DIR / "team_name_mapping_master.json"
REDEEM_CODES_FILE = REPO_DATA_DIR / "redeem_codes.json"
REDEEM_CODES_EXAMPLE_FILE = REPO_DATA_DIR / "redeem_codes.example.json"

# ── Output (all generated artifacts) ───────────────────────────────
OUTPUT_DIR = SP_DIR / "Output"
OUTPUT_PREDICTIONS_DIR = OUTPUT_DIR / "Predictions"
OUTPUT_PRED_EUROPE = OUTPUT_PREDICTIONS_DIR / "europe"
OUTPUT_PRED_MLS = OUTPUT_PREDICTIONS_DIR / "mls"
OUTPUT_PRED_EXTRA = OUTPUT_PREDICTIONS_DIR / "extra"
OUTPUT_PRED_CUPS = OUTPUT_PREDICTIONS_DIR / "cups"
OUTPUT_PRED_NATIONAL = OUTPUT_PREDICTIONS_DIR / "national"
OUTPUT_PRED_FRIENDLIES = OUTPUT_PREDICTIONS_DIR / "friendlies"
OUTPUT_PRED_SHARED = OUTPUT_PREDICTIONS_DIR / "shared"

OUTPUT_LEAGUE_DATA_DIR = OUTPUT_DIR / "LeagueData"
OUTPUT_CUP_DATA_DIR = OUTPUT_DIR / "CupData"
OUTPUT_UPCOMING_DIR = OUTPUT_DIR / "Upcoming"
OUTPUT_STATUS_DIR = OUTPUT_DIR / "Status"
OUTPUT_LOGS_DIR = OUTPUT_DIR / "Logs"
OUTPUT_CACHE_DIR = OUTPUT_DIR / "Cache"

# Convenience aliases used by Daily_Pipeline / Run_All_Pipeline
FILES_DIR = EUROPE_FILES_DIR  # historical name: global/europe scripts
PREDICTIONS_DIR = OUTPUT_PRED_EUROPE
MLS_PREDICTIONS_DIR = OUTPUT_PRED_MLS
EXTRA_PREDICTIONS_DIR = OUTPUT_PRED_EXTRA

# Status / runtime state (was under Data/)
LAST_REFRESH_FILE = OUTPUT_STATUS_DIR / "last_refresh.json"
LAST_DATA_REFRESH_FILE = OUTPUT_STATUS_DIR / "last_data_refresh.json"
PIPELINE_STATUS_FILE = OUTPUT_STATUS_DIR / "pipeline_status.json"
BACKEND_RUN_STATUS_FILE = OUTPUT_STATUS_DIR / "backend_run_status.json"
PIPELINE_LOG_FILE = OUTPUT_LOGS_DIR / "pipeline_latest.log"
STANDINGS_CACHE_FILE = OUTPUT_STATUS_DIR / "standings_cache.json"
LIVE_SCORE_HISTORY_FILE = OUTPUT_STATUS_DIR / "live_score_history.json"
# Durable append/upsert store for past games + live-score history (APIs prefer this).
SQLITE_STORE_FILE = OUTPUT_STATUS_DIR / "bts_store.db"
PREDICTION_TRACKING_FILE = OUTPUT_STATUS_DIR / "prediction_tracking.json"

# Seed / roster files (tracked in git)
LEAGUE_TEAMS_FILE = DATA_SEEDS_DIR / "league_teams.json"
CURRENT_SEASON_TEAMS_FILE = DATA_SEEDS_DIR / "current_season_teams.json"
ESPN_TEAM_NAMES_FILE = DATA_SEEDS_DIR / "espn_team_names_seen.json"
ESPN_CUP_NAMES_FILE = DATA_SEEDS_DIR / "espn_cup_names_seen.json"

# Common prediction artifacts
GLOBAL_UPCOMING_FILE = OUTPUT_PRED_EUROPE / "upcoming_matchweek_predictions.csv"
MLS_UPCOMING_FILE = OUTPUT_PRED_MLS / "upcoming_matchweek_predictions.csv"
EXTRA_UPCOMING_FILE = OUTPUT_PRED_EXTRA / "upcoming_matchweek_predictions.csv"
CUP_UPCOMING_FILE = OUTPUT_PRED_CUPS / "upcoming_cup_predictions.csv"
CUP_COMPLETED_FILE = OUTPUT_PRED_CUPS / "completed_cup_predictions.csv"
NATIONAL_UPCOMING_FILE = OUTPUT_PRED_NATIONAL / "upcoming_national_team_predictions.csv"
FRIENDLIES_UPCOMING_FILE = OUTPUT_PRED_FRIENDLIES / "upcoming_club_friendlies.csv"
PAST_GAMES_FILE = OUTPUT_PRED_SHARED / "past_games.json"
# Never-pruned append-only journal + previous snapshot for recoverability.
PAST_GAMES_JOURNAL_FILE = OUTPUT_PRED_SHARED / "past_games_journal.jsonl"
PAST_GAMES_BACKUP_FILE = OUTPUT_PRED_SHARED / "past_games.prev.json"
# Alias kept for callers that want an explicit past-games DB path; both tables
# live in SQLITE_STORE_FILE.
PAST_GAMES_DB_FILE = SQLITE_STORE_FILE
LIVE_SCORE_HISTORY_DB_FILE = SQLITE_STORE_FILE

GLOBAL_PROJECTED_TABLE_FILE = OUTPUT_PRED_EUROPE / "projected_league_tables.csv"
GLOBAL_PROJECTED_MATCHES_FILE = OUTPUT_PRED_EUROPE / "projected_future_matches.csv"
MLS_PROJECTED_TABLE_FILE = OUTPUT_PRED_MLS / "projected_league_tables.csv"
MLS_PROJECTED_BRACKET_FILE = OUTPUT_PRED_MLS / "projected_mls_playoff_bracket.json"
EXTRA_PROJECTED_TABLE_FILE = OUTPUT_PRED_EXTRA / "projected_league_tables.csv"
EXTRA_PROJECTED_MATCHES_FILE = OUTPUT_PRED_EXTRA / "projected_future_matches.csv"
CUP_PROJECTED_TABLE_FILE = OUTPUT_PRED_CUPS / "projected_cup_tables.csv"
CUP_REAL_TABLE_FILE = OUTPUT_PRED_CUPS / "real_cup_tables.csv"
CUP_PROJECTED_BRACKET_FILE = OUTPUT_PRED_CUPS / "projected_cup_brackets.json"
WORLD_CUP_PROJECTION_FILE = OUTPUT_PRED_NATIONAL / "world_cup_projection.json"

ALL_UPCOMING_FILE = OUTPUT_UPCOMING_DIR / "all_upcoming.csv"
FOUR_WEEK_WINDOW_FILE = OUTPUT_UPCOMING_DIR / "four_week_window.csv"

# Region-local team-name mapping masters (still under pipeline Data for now)
EUROPE_MAPPING_FILE = OUTPUT_PRED_SHARED / "team_name_mapping_master.json"
MLS_MAPPING_FILE = MLS_DATA_DIR / "Predictions" / "team_name_mapping_master.json"
EXTRA_MAPPING_FILE = EXTRA_DATA_DIR / "Predictions" / "team_name_mapping_master.json"

# Entry scripts
RUN_ALL_PIPELINE = MAIN_DIR / "Run_All_Pipeline.py"
DAILY_PIPELINE = MAIN_DIR / "Daily_Pipeline.py"
RUN_BACKEND = MAIN_DIR / "run_backend.py"
LIVE_RESULTS_UPDATER = EUROPE_FILES_DIR / "Update_Live_Prediction_Results.py"


def ensure_output_dirs() -> None:
    """Create the Output tree (safe to call repeatedly)."""
    for path in (
        OUTPUT_PRED_EUROPE,
        OUTPUT_PRED_MLS,
        OUTPUT_PRED_EXTRA,
        OUTPUT_PRED_CUPS,
        OUTPUT_PRED_NATIONAL,
        OUTPUT_PRED_FRIENDLIES,
        OUTPUT_PRED_SHARED,
        OUTPUT_LEAGUE_DATA_DIR,
        OUTPUT_CUP_DATA_DIR,
        OUTPUT_UPCOMING_DIR,
        OUTPUT_STATUS_DIR,
        OUTPUT_LOGS_DIR,
        OUTPUT_CACHE_DIR,
        DATA_SEEDS_DIR,
        DATA_DIR / "Raw_Data",
        DATA_DIR / "Processed_Data",
        DATA_DIR / "Team_Data",
        MLS_DATA_DIR / "Raw_Data",
        MLS_DATA_DIR / "Processed_Data",
        MLS_DATA_DIR / "Team_Data",
        MLS_DATA_DIR / "Predictions",
        EXTRA_DATA_DIR / "Raw_Data",
        EXTRA_DATA_DIR / "Processed_Data",
        EXTRA_DATA_DIR / "Team_Data",
        EXTRA_DATA_DIR / "Predictions",
    ):
        os.makedirs(path, exist_ok=True)
    migrate_legacy_runtime_files()
    migrate_legacy_working_data()


def _tree_has_files(root: Path) -> bool:
    if not root.is_dir():
        return False
    try:
        for path in root.rglob("*"):
            if path.is_file():
                return True
    except OSError:
        return False
    return False


def _copy_tree_fill(src: Path, dst: Path) -> int:
    """Copy files from ``src`` into ``dst`` when the destination file is missing.

    Returns the number of files copied. Never overwrites an existing destination
    file so a partial new download is not clobbered by stale legacy copies.
    """
    import shutil

    if not src.is_dir():
        return 0
    copied = 0
    try:
        for src_path in src.rglob("*"):
            if not src_path.is_file():
                continue
            rel = src_path.relative_to(src)
            dst_path = dst / rel
            if dst_path.is_file():
                continue
            try:
                dst_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dst_path)
                copied += 1
            except OSError:
                continue
    except OSError:
        return copied
    return copied


def migrate_legacy_working_data() -> None:
    """Recover gitignored Raw/Processed/Team_Data left at pre-reorg paths.

    ``**/Data/Raw_Data/**`` and Processed_Data are gitignored, so a git-based
    folder reorg does not move downloaded CSVs. Production hosts may still have:

    - ``Beyond-the-Stats/MLS/Data/...``
    - ``Beyond-the-Stats/Extra-leagues/Data/...``
    - ``pipelines/europe/Data/...`` (mistaken region-local europe tree)

    while the code now reads ``pipelines/mls/Data``, ``pipelines/extra/Data``,
    and project-level ``Data/``. Copy missing files into the canonical trees so
    the pipeline does not cold-redownload (or fail) for hours.
    """
    working_subdirs = ("Raw_Data", "Processed_Data", "Team_Data")

    # Region Data roots that moved with the reorg (code paths updated; CSVs may not).
    region_legacy_roots = (
        (SP_DIR / "MLS" / "Data", MLS_DATA_DIR),
        (SP_DIR / "Extra-leagues" / "Data", EXTRA_DATA_DIR),
        (SP_DIR / "Extra" / "Data", EXTRA_DATA_DIR),
    )
    for legacy_root, new_root in region_legacy_roots:
        if legacy_root.resolve() == new_root.resolve():
            continue
        for sub in working_subdirs:
            src = legacy_root / sub
            dst = new_root / sub
            if not _tree_has_files(src):
                continue
            copied = _copy_tree_fill(src, dst)
            if copied:
                print(
                    f"[paths] migrated {copied} file(s) from {src} -> {dst}",
                    flush=True,
                )

    # Europe working data must live under project Data/, not pipelines/europe/Data.
    mistaken_europe = EUROPE_DIR / "Data"
    if mistaken_europe.resolve() != DATA_DIR.resolve():
        for sub in working_subdirs:
            src = mistaken_europe / sub
            dst = DATA_DIR / sub
            if not _tree_has_files(src):
                continue
            copied = _copy_tree_fill(src, dst)
            if copied:
                print(
                    f"[paths] migrated {copied} file(s) from {src} -> {dst}",
                    flush=True,
                )


def migrate_legacy_runtime_files() -> None:
    """Copy pre-reorg ``Data/*.json`` status files into ``Output/Status/``.

    Hosts that ran before the folder reorg may still have
    ``Data/backend_run_status.json``, ``Data/standings_cache.json``, etc.
    New code only reads ``Output/Status/``. Copy (not move) when the new
    path is missing so a partial deploy cannot lose the only copy.
    """
    legacy_pairs = (
        (DATA_DIR / "backend_run_status.json", BACKEND_RUN_STATUS_FILE),
        (DATA_DIR / "standings_cache.json", STANDINGS_CACHE_FILE),
        (DATA_DIR / "live_score_history.json", LIVE_SCORE_HISTORY_FILE),
        (DATA_DIR / "pipeline_status.json", PIPELINE_STATUS_FILE),
        (DATA_DIR / "last_refresh.json", LAST_REFRESH_FILE),
        (DATA_DIR / "last_data_refresh.json", LAST_DATA_REFRESH_FILE),
        (DATA_DIR / "prediction_tracking.json", PREDICTION_TRACKING_FILE),
        (
            DATA_DIR / "Predictions" / "world_cup_projection.json",
            WORLD_CUP_PROJECTION_FILE,
        ),
        (
            DATA_DIR / "Predictions" / "upcoming_matchweek_predictions.csv",
            GLOBAL_UPCOMING_FILE,
        ),
        (
            DATA_DIR / "Predictions" / "projected_league_tables.csv",
            GLOBAL_PROJECTED_TABLE_FILE,
        ),
        (
            DATA_DIR / "Predictions" / "past_games.json",
            PAST_GAMES_FILE,
        ),
    )
    import shutil

    for src, dst in legacy_pairs:
        try:
            if not src.is_file():
                continue
            if dst.is_file():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError:
            continue


def as_str(path: Path | str) -> str:
    return str(path)
