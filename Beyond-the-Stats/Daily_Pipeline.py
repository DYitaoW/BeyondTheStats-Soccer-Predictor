"""
Daily pipeline runner + mobile-app feed generator.

Repeats the full data-gathering / processing / sorting / prediction pipeline
every day (covering global, MLS, extra leagues, cups, and the World Cup),
then writes a condensed JSON feed to the `Output/` directory for a future
mobile app.

Usage:
    python Daily_Pipeline.py                       # run once, then sleep 24h, repeat
    python Daily_Pipeline.py --once                # run once and exit
    python Daily_Pipeline.py --interval-hours 12   # custom cycle
    python Daily_Pipeline.py --max-iterations 3     # stop after N runs (testing)
    python Daily_Pipeline.py --skip-mls --skip-extra
    python Daily_Pipeline.py --output-file path/to/feed.json

The pipeline step list mirrors `Run_All_Pipeline.py`; this script is just the
scheduler + mobile-feed writer on top of it.
"""

"""
Scheduled daily pipeline runner — wraps ``Run_All_Pipeline`` in a scheduler loop.

Runs continuously on the Steam Deck as a long-lived process.  On Tuesday it
executes a full retrain (model + data); on other days it runs a light refresh
(data only, ``--skip-model-train`` / ``--skip-squad-values``).

Key differences from ``Run_All_Pipeline``:
- Scheduler loop with configurable window (``--window-days``)
- After each pipeline run, builds the mobile-app feed JSON
- Publishes output files to a deployment directory
- Decides full vs. light refresh based on ``weekly_model_refresh_day``
"""
import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

SP_DIR = Path(__file__).resolve().parent
ROOT_DIR = SP_DIR.parent
FILES_DIR = SP_DIR / "files"
MLS_FILES_DIR = SP_DIR / "MLS" / "files"
EXTRA_FILES_DIR = SP_DIR / "Extra-leagues" / "files"
PREDICTIONS_DIR = SP_DIR / "Data" / "Predictions"
MLS_PREDICTIONS_DIR = SP_DIR / "MLS" / "Data" / "Predictions"
EXTRA_PREDICTIONS_DIR = SP_DIR / "Extra-leagues" / "Data" / "Predictions"
OUTPUT_DIR = SP_DIR / "Output"
DEFAULT_FEED_FILE = OUTPUT_DIR / "mobile_app_feed.json"
LOCAL_KEYS_FILE = FILES_DIR / "local_api_keys.json"

# Output folder structure constants
REGION_DIRS = ("Europe", "Other", "National")
LEAGUE_RESULT_SUBDIR = "LeagueResult"
UPCOMING_SUBDIR = "Upcoming"
TEAMSTAT_SUBDIR = "TeamStat"

# Countries whose domestic leagues are classified as "europe" by default.
_EUROPEAN_COUNTRIES = frozenset({
    "England", "Spain", "Italy", "Portugal", "Netherlands", "France",
    "Germany", "Turkey", "Belgium", "Scotland", "Greece", "Austria",
    "Switzerland", "Denmark", "Sweden", "Norway", "Poland", "Czechia",
    "Czech Republic", "Croatia", "Serbia", "Ukraine", "Romania",
    "Hungary", "Bulgaria", "Cyprus", "Israel", "Slovakia", "Slovenia",
    "Bosnia", "Albania", "Montenegro", "Kosovo", "North Macedonia",
    "Finland", "Iceland", "Ireland", "Wales", "Northern Ireland",
    "Moldova", "Georgia", "Armenia", "Azerbaijan", "Kazakhstan",
    "Lithuania", "Latvia", "Estonia", "Belarus",
    "UEFA",
})

MOBILE_FEED_SCHEMA_VERSION = 1

_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    try:
        print("\n[INFO] Shutdown requested. Finishing current run, then exiting.")
    except Exception:
        pass


signal.signal(signal.SIGINT, _handle_shutdown)
signal.signal(signal.SIGTERM, _handle_shutdown)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Continuously run the full soccer pipeline and emit a mobile-app feed."
    )
    parser.add_argument("--once", action="store_true", help="Run the pipeline once and exit (no scheduler loop).")
    parser.add_argument(
        "--interval-hours",
        type=float,
        default=24.0,
        help=(
            "Hours to wait between runs when looping. Default 24. "
            "Ignored when --refresh-time is set (then the scheduler fires "
            "at the configured wall-clock time every day)."
        ),
    )
    parser.add_argument(
        "--refresh-time",
        type=str,
        default="",
        help=(
            "Daily wall-clock time to run the pipeline, format HH:MM (24h). "
            "When set, the scheduler aligns each run to this time in --timezone "
            "(default America/New_York) instead of every --interval-hours. "
            "Example: --refresh-time 02:00 --timezone America/New_York."
        ),
    )
    parser.add_argument(
        "--timezone",
        type=str,
        default="America/New_York",
        help=(
            "IANA timezone name used to interpret --refresh-time. "
            "Default America/New_York. The 2am ET target survives DST "
            "transitions automatically because the scheduler is timezone-aware."
        ),
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=0,
        help="Stop after this many runs (0 = unlimited). Useful for testing.",
    )
    # Pipeline toggles (passed through to Run_All_Pipeline).
    parser.add_argument("--skip-mls", action="store_true", help="Skip MLS pipeline steps.")
    parser.add_argument("--skip-extra", action="store_true", help="Skip extra-leagues pipeline steps.")
    parser.add_argument("--skip-global", action="store_true", help="Skip European/global pipeline steps.")
    parser.add_argument("--window-days", type=int, default=365, help="Fixture window days for upcoming matchweek scripts.")
    parser.add_argument(
        "--national-window-days",
        type=int,
        default=90,
        help="Fixture window days for national-team and World Cup prediction scripts.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue remaining pipeline steps if one step fails.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Number of sub-pipelines (global/MLS/extra) to run concurrently in Run_All_Pipeline. "
            "1 = sequential (default). Up to 3 = all sub-pipelines in parallel."
        ),
    )
    parser.add_argument(
        "--competition-workers",
        type=int,
        default=0,
        help=(
            "Per-competition worker count passed to Project_League_Table.py. "
            "0 = auto; 1 = serial. Currently the inner sim loop dominates, so this is a no-op stub."
        ),
    )
    parser.add_argument(
        "--skip-model-train",
        action="store_true",
        help="Skip model cache building (retraining) steps; only fetch data and update predictions.",
    )
    return parser.parse_args()


def load_api_token():
    import os
    env_token = os.getenv("FOOTBALL_DATA_API_TOKEN", "").strip()
    if env_token:
        return env_token
    if LOCAL_KEYS_FILE.exists():
        try:
            payload = json.loads(LOCAL_KEYS_FILE.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        token = str(payload.get("FOOTBALL_DATA_API_TOKEN", "")).strip()
        if token:
            return token
    return ""


# ---------------------------------------------------------------------------
# CSV condensing helpers
# ---------------------------------------------------------------------------

_FIXTURE_FIELDS = (
    "match_date",
    "match_datetime_utc",
    "competition",
    "home_team",
    "away_team",
    "predicted_result",
    "prob_home",
    "prob_draw",
    "prob_away",
    "pred_home_goals",
    "pred_away_goals",
)

_TABLE_FIELDS = (
    "competition",
    "position",
    "team",
    "P",
    "W",
    "D",
    "L",
    "GF",
    "GA",
    "GD",
    "Pts",
)


def _read_csv(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _condense_rows(rows, fields):
    """Trim each row to the named fields, dropping any that are missing/empty."""
    out = []
    for row in rows:
        item = {}
        for key in fields:
            value = row.get(key, "")
            if value in (None, ""):
                continue
            item[key] = value
        if item:
            out.append(item)
    return out


def _condense_fixtures(path):
    return _condense_rows(_read_csv(path), _FIXTURE_FIELDS)


def _condense_tables(path):
    return _condense_rows(_read_csv(path), _TABLE_FIELDS)


def _condense_world_cup(path):
    """Condense the world_cup_projection.json into the mobile-app essentials."""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            proj = json.load(f)
    except Exception as exc:
        print(f"[WARN] Failed to read {path}: {exc}")
        return {}

    simulations = proj.get("simulations", {}) or {}
    return {
        "year": proj.get("year"),
        "competition": proj.get("competition"),
        "champion": proj.get("champion"),
        "groups": proj.get("group_tables", []),
        "third_place_table": proj.get("third_place_table", []),
        "knockout": proj.get("knockout", {}),
        "winner_probabilities": simulations.get("winner_probabilities", {}),
        "simulations_run": simulations.get("simulations_run"),
    }


def _condense_cup_brackets(path):
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"[WARN] Failed to read {path}: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Mobile feed builder
# ---------------------------------------------------------------------------

def build_mobile_app_feed(pipeline_status, step_results, output_path):
    """Read the latest prediction outputs and write a condensed JSON feed.

    Schema is provisional (schema_version=1). The exact field set can be
    refined once the mobile-app spec is known — this is the basic framework.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    sources = {
        "upcoming_fixtures_global": PREDICTIONS_DIR / "upcoming_matchweek_predictions.csv",
        "upcoming_fixtures_cups": PREDICTIONS_DIR / "upcoming_cup_predictions.csv",
        "upcoming_fixtures_national": PREDICTIONS_DIR / "upcoming_national_team_predictions.csv",
        "projected_league_tables_global": PREDICTIONS_DIR / "projected_league_tables.csv",
        "projected_cup_tables": PREDICTIONS_DIR / "projected_cup_tables.csv",
        "projected_cup_brackets": PREDICTIONS_DIR / "projected_cup_brackets.json",
        "completed_cup_predictions": PREDICTIONS_DIR / "completed_cup_predictions.csv",
        "projected_future_matches": PREDICTIONS_DIR / "projected_future_matches.csv",
        "world_cup_projection": PREDICTIONS_DIR / "world_cup_projection.json",
        "mls_upcoming_fixtures": MLS_PREDICTIONS_DIR / "upcoming_matchweek_predictions.csv",
        "mls_projected_league_tables": MLS_PREDICTIONS_DIR / "projected_league_tables.csv",
    }

    feed = {
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "schema_version": MOBILE_FEED_SCHEMA_VERSION,
        "pipeline_status": "ok" if pipeline_status else "degraded",
        "step_results": dict(step_results),
        "sources": {name: str(path) for name, path in sources.items()},
        "data": {
            "upcoming_fixtures": _condense_fixtures(sources["upcoming_fixtures_global"]),
            "upcoming_cup_fixtures": _condense_fixtures(sources["upcoming_fixtures_cups"]),
            "upcoming_national_fixtures": _condense_fixtures(sources["upcoming_fixtures_national"]),
            "projected_league_tables": _condense_tables(sources["projected_league_tables_global"]),
            "projected_cup_tables": _condense_tables(sources["projected_cup_tables"]),
            "projected_cup_brackets": _condense_cup_brackets(sources["projected_cup_brackets"]),
            "completed_cup_predictions": _condense_fixtures(sources["completed_cup_predictions"]),
            "mls_upcoming_fixtures": _condense_fixtures(sources["mls_upcoming_fixtures"]),
            "mls_projected_league_tables": _condense_tables(sources["mls_projected_league_tables"]),
            "world_cup": _condense_world_cup(sources["world_cup_projection"]),
        },
    }

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2, default=str)

    print(f"[OK] Mobile-app feed written: {output_path}")
    return feed


# ---------------------------------------------------------------------------
# Output/ folder publisher
# ---------------------------------------------------------------------------
#
# Layout produced (per the user's spec):
#
#   Output/
#   ├── Europe/
#   │   ├── LeagueResult/<league_slug>.json  (per European league + UEFA cups)
#   │   ├── Upcoming/europe_upcoming.csv     (every upcoming fixture classified as Europe)
#   │   └── TeamStat/.gitkeep                 (placeholder, empty for now)
#   ├── Other/
#   │   ├── LeagueResult/<league_slug>.json  (American + non-European leagues)
#   │   ├── Upcoming/other_upcoming.csv
#   │   └── TeamStat/.gitkeep
#   ├── National/
#   │   ├── world_cup.json                    (full World Cup projection)
#   │   ├── Upcoming/national_upcoming.csv
#   │   └── TeamStat/.gitkeep
#   └── Upcoming/
#       └── all_upcoming.csv                  (single combined file, every region)
#
# Per-league JSON shape:
#   { competition, region, generated_at_utc, team_count, teams: [ ... ] }
#   where each team carries the full position_odds dict (one pct per table spot).
#
# Upcoming CSV columns (match projected_future_matches.csv layout + time):
#   competition, match_date, match_datetime_utc, home_team, away_team,
#   predicted_result, pred_home_goals, pred_away_goals,
#   prob_home, prob_draw, prob_away

_UPCOMING_CSV_FIELDS = (
    "competition",
    "match_date",
    "match_datetime_utc",
    "home_team",
    "away_team",
    "predicted_result",
    "pred_home_goals",
    "pred_away_goals",
    "prob_home",
    "prob_draw",
    "prob_away",
)


def _classify_competition(competition):
    """Map a competition string to 'europe', 'other', or 'national'."""
    comp = (competition or "").strip()
    if not comp:
        return "other"

    lower = comp.lower()
    if "world cup" in lower or lower.startswith("fifa/") or "/world cup" in lower:
        return "national"
    if "nations league" in lower or "euro " in lower or lower.startswith("euro/") or "copa america" in lower:
        return "national"
    if "friendly" in lower and "/" not in comp:
        return "national"

    if "/" in comp:
        country = comp.split("/", 1)[0].strip()
        if country in _EUROPEAN_COUNTRIES:
            return "europe"
        if country == "United States":
            return "other"
        return "other"
    return "other"


def _slugify_competition(competition):
    """Turn 'England/Premier League' into 'england_premier_league'."""
    out = (competition or "").strip().lower()
    out = out.replace("/", "_").replace(" ", "_").replace("-", "_")
    out = out.replace(".", "").replace(",", "").replace("'", "")
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_") or "unknown"


def _parse_position_odds(raw):
    """Parse the `position_odds_json` CSV cell into a {pos_str: pct} dict."""
    if not raw or not str(raw).strip():
        return {}
    try:
        parsed = json.loads(str(raw))
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out = {}
    for k, v in parsed.items():
        try:
            key = str(int(k))
        except (ValueError, TypeError):
            continue
        try:
            out[key] = float(v)
        except (ValueError, TypeError):
            continue
    return out


def _row_to_int(value, default=0):
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def _row_to_float(value, default=0.0):
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _publish_league_tables(output_dir, source_path, region):
    """Read a projected_league_tables CSV, filter by region, write per-league JSON."""
    if not source_path or not source_path.exists():
        return {}
    rows = _read_csv(source_path)
    region_rows = [r for r in rows if _classify_competition(r.get("competition", "")) == region]
    if not region_rows:
        return {}

    by_comp = {}
    for r in region_rows:
        comp = r.get("competition", "").strip()
        if not comp:
            continue
        by_comp.setdefault(comp, []).append(r)

    out_dir = output_dir / region.capitalize() / LEAGUE_RESULT_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()

    for comp, comp_rows in by_comp.items():
        teams = []
        for r in comp_rows:
            position_odds = _parse_position_odds(r.get("position_odds_json", ""))
            teams.append(
                {
                    "position": _row_to_int(r.get("position")),
                    "team": r.get("team", "").strip(),
                    "P": _row_to_int(r.get("P")),
                    "W": _row_to_int(r.get("W")),
                    "D": _row_to_int(r.get("D")),
                    "L": _row_to_int(r.get("L")),
                    "GF": _row_to_int(r.get("GF")),
                    "GA": _row_to_int(r.get("GA")),
                    "GD": _row_to_int(r.get("GD")),
                    "Pts": _row_to_int(r.get("Pts")),
                    "PlayedReal": _row_to_int(r.get("PlayedReal")),
                    "PlayedPred": _row_to_int(r.get("PlayedPred")),
                    "win_league_pct": round(_row_to_float(r.get("win_league_pct")), 3),
                    "top4_pct": round(_row_to_float(r.get("top4_pct")), 3),
                    "bottom3_pct": round(_row_to_float(r.get("bottom3_pct")), 3),
                    "most_likely_position": _row_to_int(r.get("most_likely_position")),
                    "most_likely_position_pct": round(_row_to_float(r.get("most_likely_position_pct")), 3),
                    "sim_runs": _row_to_int(r.get("sim_runs")),
                    "position_odds": position_odds,
                }
            )
        teams.sort(key=lambda t: (t["position"], t["team"]))
        payload = {
            "competition": comp,
            "region": region.lower(),
            "generated_at_utc": generated_at,
            "team_count": len(teams),
            "teams": teams,
        }
        slug = _slugify_competition(comp)
        out_path = out_dir / f"{slug}.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        written[comp] = str(out_path)

    return written


def _publish_upcoming_csv(output_dir, source_paths, region):
    """Read upcoming CSVs, filter by region, write a single per-region CSV."""
    all_rows = []
    for src in source_paths:
        if not src or not src.exists():
            continue
        all_rows.extend(_read_csv(src))
    region_rows = [r for r in all_rows if _classify_competition(r.get("competition", "")) == region]
    if not region_rows:
        return None

    out_dir = output_dir / region.capitalize() / UPCOMING_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{region.lower()}_upcoming.csv"
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_UPCOMING_CSV_FIELDS)
        w.writeheader()
        for r in region_rows:
            w.writerow({k: r.get(k, "") for k in _UPCOMING_CSV_FIELDS})
    return out_path


def _publish_all_upcoming(output_dir, source_paths):
    """Write the combined Output/Upcoming/all_upcoming.csv (deduped)."""
    all_rows = []
    for src in source_paths:
        if not src or not src.exists():
            continue
        all_rows.extend(_read_csv(src))
    if not all_rows:
        return None

    seen = set()
    deduped = []
    for r in all_rows:
        key = (
            r.get("competition", "").strip(),
            r.get("home_team", "").strip(),
            r.get("away_team", "").strip(),
            r.get("match_date", "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    out_dir = output_dir / "Upcoming"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "all_upcoming.csv"
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_UPCOMING_CSV_FIELDS)
        w.writeheader()
        for r in deduped:
            w.writerow({k: r.get(k, "") for k in _UPCOMING_CSV_FIELDS})
    return out_path


def _publish_world_cup(output_dir):
    """Copy world_cup_projection.json to Output/National/world_cup.json."""
    src = PREDICTIONS_DIR / "world_cup_projection.json"
    if not src.exists():
        return None
    out_dir = output_dir / "National"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "world_cup.json"
    with src.open("r", encoding="utf-8") as f:
        data = json.load(f)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    return out_path


def _create_empty_teamstat_dirs(output_dir):
    """Create empty <region>/TeamStat/.gitkeep directories."""
    for region in REGION_DIRS:
        teamstat_dir = output_dir / region / TEAMSTAT_SUBDIR
        teamstat_dir.mkdir(parents=True, exist_ok=True)
        keep = teamstat_dir / ".gitkeep"
        if not keep.exists():
            keep.touch()


def publish_to_output(output_dir=None):
    """Copy pipeline outputs into the Output/ folder structure.

    Returns a dict with lists of files written per region.
    """
    output_dir = Path(output_dir) if output_dir else OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    written = {"europe": {}, "other": {}, "national": {}, "combined": {}}

    global_tables = PREDICTIONS_DIR / "projected_league_tables.csv"
    cup_tables = PREDICTIONS_DIR / "projected_cup_tables.csv"
    mls_tables = MLS_PREDICTIONS_DIR / "projected_league_tables.csv"
    extra_tables = EXTRA_PREDICTIONS_DIR / "projected_league_tables.csv"

    for comp, path in _publish_league_tables(output_dir, global_tables, "europe").items():
        written["europe"][comp] = path
    for comp, path in _publish_league_tables(output_dir, cup_tables, "europe").items():
        written["europe"][comp] = path
    for src in (global_tables, mls_tables, extra_tables):
        for comp, path in _publish_league_tables(output_dir, src, "other").items():
            written["other"][comp] = path

    wc_path = _publish_world_cup(output_dir)
    if wc_path:
        written["national"]["world_cup"] = str(wc_path)

    upcoming_sources = [
        PREDICTIONS_DIR / "upcoming_matchweek_predictions.csv",
        PREDICTIONS_DIR / "upcoming_cup_predictions.csv",
        MLS_PREDICTIONS_DIR / "upcoming_matchweek_predictions.csv",
        EXTRA_PREDICTIONS_DIR / "upcoming_matchweek_predictions.csv",
    ]
    for region in ("europe", "other"):
        path = _publish_upcoming_csv(output_dir, upcoming_sources, region)
        if path:
            written[region]["upcoming"] = str(path)
    nat_sources = [PREDICTIONS_DIR / "upcoming_national_team_predictions.csv"]
    path = _publish_upcoming_csv(output_dir, nat_sources, "national")
    if path:
        written["national"]["upcoming"] = str(path)

    combined = _publish_all_upcoming(output_dir, upcoming_sources + nat_sources)
    if combined:
        written["combined"]["all_upcoming"] = str(combined)

    _create_empty_teamstat_dirs(output_dir)

    summary = {
        region: {
            "league_files": sum(1 for k in written[region] if k != "upcoming" and k != "world_cup"),
            "files": [str(p) for p in written[region].values()],
        }
        for region in ("europe", "other", "national")
    }
    summary["combined"] = {"files": [str(p) for p in written["combined"].values()]}
    return summary


# ---------------------------------------------------------------------------
# Main scheduler
# ---------------------------------------------------------------------------

LAST_REFRESH_FILE = SP_DIR / "Data" / "last_refresh.json"


def _write_pipeline_timestamp() -> None:
    """Write last_refresh.json so /api/last-refresh is current even if
    the parent process (gunicorn/BackendServer) crashed mid-pipeline."""
    try:
        LAST_REFRESH_FILE.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).replace(microsecond=0)
        LAST_REFRESH_FILE.write_text(
            json.dumps({"last_refresh_utc": now.isoformat()}), encoding="utf-8"
        )
    except Exception as exc:
        print(f"[WARN] Could not write {LAST_REFRESH_FILE}: {exc}")

def _import_pipeline_runner():
    """Import `run_full_pipeline` from the sibling Run_All_Pipeline module."""
    if str(SP_DIR) not in sys.path:
        sys.path.insert(0, str(SP_DIR))
    from Run_All_Pipeline import run_full_pipeline  # type: ignore
    return run_full_pipeline


def _write_pipeline_status(results: dict) -> None:
    """Write pipeline step results to Data/pipeline_status.json (local copy)."""
    try:
        now = datetime.now(UTC).replace(microsecond=0)
        passed = sum(1 for v in results.values() if v)
        failed = sum(1 for v in results.values() if not v)
        pfile = SP_DIR / "Data" / "pipeline_status.json"
        pfile.parent.mkdir(parents=True, exist_ok=True)
        pfile.write_text(
            json.dumps({
                "finished_utc": now.isoformat(),
                "total_steps": len(results),
                "passed": passed,
                "failed": failed,
                "steps": {k: bool(v) for k, v in sorted(results.items())},
            }, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[WARN] Could not write pipeline_status.json: {exc}")


def _sleep_with_shutdown(seconds):
    """Sleep for `seconds` in 1-second ticks; return early on shutdown signal."""
    end = time.monotonic() + seconds
    while not _shutdown_requested and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))


def _compute_seconds_until_next_run(refresh_time, tz_name):
    """Compute seconds from now until the next HH:MM tick in ``tz_name``.

    Returns None if ``refresh_time`` is empty / unparseable so the caller
    can fall back to the interval-hours path.
    """
    from zoneinfo import ZoneInfo

    if not refresh_time:
        return None
    try:
        hour_str, minute_str = refresh_time.split(":", 1)
        hour = int(hour_str)
        minute = int(minute_str)
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError("out of range")
    except (ValueError, AttributeError):
        print(f"[WARN] --refresh-time {refresh_time!r} is not HH:MM; falling back to --interval-hours")
        return None
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        print(f"[WARN] --timezone {tz_name!r} is unknown; falling back to UTC")
        tz = ZoneInfo("UTC")
    now_local = datetime.now(tz)
    target = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_local:
        target = target + timedelta(days=1)
    wait = (target - now_local).total_seconds()
    print(f"[INFO] Next run scheduled for {target.isoformat()} ({tz_name}) -- in {wait/3600:.2f}h")
    return wait


def main():
    args = parse_args()
    api_token = load_api_token()
    run_full_pipeline = _import_pipeline_runner()
    if hasattr(args, "workers"):
        setattr(args, "workers", max(1, int(getattr(args, "workers", 1) or 1)))
    if hasattr(args, "competition_workers"):
        setattr(args, "competition_workers", max(0, int(getattr(args, "competition_workers", 0) or 0)))

    print("\n" + "=" * 70)
    print("  Daily Pipeline starting")
    print(f"  Python: {sys.version.split()[0]} on {sys.platform}")
    print(f"  CWD: {Path.cwd()}")
    print(f"  Args: {sys.argv}")
    print(f"  API token set: {bool(api_token)}")
    print(f"  Workers: {getattr(args, 'workers', 'default')}")
    print(f"  Skip: global={getattr(args, 'skip_global', False)}"
          f" mls={getattr(args, 'skip_mls', False)}"
          f" extra={getattr(args, 'skip_extra', False)}")
    print(f"  continue_on_error: {getattr(args, 'continue_on_error', False)}")
    print("=" * 70 + "\n")

    iteration = 0
    while not _shutdown_requested:
        iteration += 1
        print("\n" + "#" * 80)
        print(f"  PIPELINE RUN #{iteration}  -  {datetime.now(UTC).replace(microsecond=0).isoformat()}")
        print("#" * 80)

        _iter_start = time.monotonic()
        step_results = {}
        pipeline_ok = False
        try:
            run_full_pipeline(args, api_token, step_results)
            pipeline_ok = bool(step_results) and all(step_results.values())
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            print(f"[ERROR] Pipeline failed: {exc}")
            import traceback
            traceback.print_exc()
        finally:
            _write_pipeline_status(step_results)

        _write_pipeline_timestamp()
        _iter_elapsed = time.monotonic() - _iter_start
        print(f"[DEBUG] Daily_Pipeline iteration #{iteration} took {_iter_elapsed:.0f}s total")

        if args.once:
            break
        if args.max_iterations and iteration >= args.max_iterations:
            print(f"[INFO] Reached max-iterations={args.max_iterations}; exiting.")
            break

        # Sleep until the next scheduled run. Two paths:
        #   1. --refresh-time HH:MM --timezone TZ  -> align to wall clock
        #   2. (default) --interval-hours N        -> sleep N hours from now
        # The --refresh-time path is preferred for the persistent backend: the
        # 2am ET target is preserved across DST because the scheduler uses
        # zoneinfo instead of naive UTC math.
        wait_seconds = _compute_seconds_until_next_run(args.refresh_time, args.timezone)
        if wait_seconds is None:
            wait_seconds = max(0.0, args.interval_hours) * 3600.0
        if wait_seconds <= 0:
            print("[INFO] wait <= 0; exiting after one run.")
            break

        print(f"\n[INFO] Next run in {wait_seconds/3600:.2f}h. Press Ctrl-C to stop.")
        _sleep_with_shutdown(wait_seconds)

    print("[INFO] Daily pipeline exiting.")


if __name__ == "__main__":
    main()
