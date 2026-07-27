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

Runs continuously on the Steam Deck as a long-lived process.  On Tuesday and
Friday it executes a full model retrain; on other days it runs a light refresh
(data download, predictions, tables — ``--skip-model-train``). A missing or
broken cache file is still built automatically on light days.

Key differences from ``Run_All_Pipeline``:
- Scheduler loop with configurable window (``--window-days``)
- After each pipeline run, builds the mobile-app feed JSON
- Publishes output files to a deployment directory
- Decides full vs. light refresh based on backend ``weekly_model_refresh_days`` (Tue/Fri)
"""
import argparse
import csv
import hashlib
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pipeline_log

SP_DIR = Path(__file__).resolve().parent
ROOT_DIR = SP_DIR.parent
FILES_DIR = SP_DIR / "files"
MLS_FILES_DIR = SP_DIR / "MLS" / "files"
EXTRA_FILES_DIR = SP_DIR / "Extra-leagues" / "files"
PREDICTIONS_DIR = SP_DIR / "Data" / "Predictions"
MLS_PREDICTIONS_DIR = SP_DIR / "MLS" / "Data" / "Predictions"
EXTRA_PREDICTIONS_DIR = SP_DIR / "Extra-leagues" / "Data" / "Predictions"
OUTPUT_DIR = SP_DIR / "Output"
PAST_GAMES_FILE = PREDICTIONS_DIR / "past_games.json"
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

# Competitions with knockout stages (for randomized simulation & real brackets).
_CUP_COMPETITIONS = frozenset({
    "England/FA Cup", "England/League Cup",
    "Italy/Coppa Italia", "Spain/Copa del Rey",
    "Germany/DFB-Pokal", "France/Coupe de France",
    "United States/US Open Cup", "CONCACAF/Leagues Cup",
    "FIFA/World Cup",
})

_CUP_SIM_RUNS = {
    "UEFA/Champions League": 500,
    "United States/MLS": 500,
    "UEFA/Conference League": 150,
    "United States/US Open Cup": 150,
}

_ROUND_STAGE_MAP = {
    "final": "final", "finale": "final",
    "quarterfinals": "quarterfinals", "quarter-finals": "quarterfinals",
    "quarter_finals": "quarterfinals", "quarter": "quarterfinals",
    "semifinals": "semifinals", "semi-finals": "semifinals",
    "semi_finals": "semifinals", "semi": "semifinals",
    "round_of_16": "round_of_16", "round of 16": "round_of_16",
    "round_of_32": "round_of_32", "round of 32": "round_of_32",
    "group_stage": "group_stage", "group stage": "group_stage",
    "league_phase": "league_phase", "league phase": "league_phase",
    "third_place": "third_place", "third place": "third_place",
}


def _is_knockout_format(comp_name: str) -> bool:
    """Return True for cup/knockout competition names."""
    if not comp_name:
        return False
    if comp_name in _CUP_COMPETITIONS:
        return True
    if any(phrase in comp_name for phrase in
           ["Champions League", "Europa League", "Conference League"]):
        return True
    if "/Cup" in comp_name or " Cup" in comp_name or "World Cup" in comp_name:
        return True
    return False


def _round_to_stage_key(round_name: str) -> str:
    """Normalize a round label into a canonical stage key (e.g. 'quarterfinals')."""
    key = round_name.strip().lower()
    key = re.sub(r"['\u2019]", "", key)
    key = key.replace("-", "_").replace(" ", "_")
    if key in _ROUND_STAGE_MAP:
        return _ROUND_STAGE_MAP[key]
    m = re.match(r"round_?of_?(\d+)", key)
    if m:
        return f"round_of_{m.group(1)}"
    ordinal_map = {
        "first": "1", "second": "2", "third": "3", "fourth": "4",
        "fifth": "5", "sixth": "6", "seventh": "7", "eighth": "8",
        "ninth": "9", "tenth": "10",
    }
    m = re.match(r"(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)_round", key)
    if m and m.group(1) in ordinal_map:
        return f"round_{ordinal_map[m.group(1)]}"
    return key


def _discover_cup_teams(comp_name, upcoming_rows, completed_rows):
    """Extract unique team names for a cup comp from prediction CSVs."""
    teams = set()
    for row in list(upcoming_rows) + list(completed_rows):
        if row.get("competition", "").strip() == comp_name:
            ht = row.get("home_team", "").strip()
            at = row.get("away_team", "").strip()
            if ht:
                teams.add(ht)
            if at:
                teams.add(at)
    return sorted(teams)


def _run_randomized_cup_simulation(teams, num_simulations=250):
    """Monte Carlo knockout simulation — random pairings each round, 50/50 winners.

    Returns {team_name: win_pct} for teams that won at least once.
    """
    wins = {t: 0 for t in teams}
    team_list = list(teams)
    n = len(team_list)
    num_rounds = max(1, (n - 1).bit_length())
    total_slots = 1 << num_rounds

    for _ in range(num_simulations):
        shuffled = team_list.copy()
        random.shuffle(shuffled)
        current = shuffled + [None] * (total_slots - n)

        for _ in range(num_rounds):
            nxt = []
            for i in range(0, len(current), 2):
                t1 = current[i]
                t2 = current[i + 1] if i + 1 < len(current) else None
                if t1 is None and t2 is None:
                    continue
                if t1 is None:
                    nxt.append(t2)
                elif t2 is None:
                    nxt.append(t1)
                else:
                    nxt.append(t1 if random.random() < 0.5 else t2)
            if not nxt:
                break
            if len(nxt) == 1:
                w = nxt[0]
                if w:
                    wins[w] += 1
                break
            current = nxt

    return {t: round(c / num_simulations * 100, 2)
            for t, c in wins.items() if c > 0}


def _build_real_knockout_for_comp(live_games, comp_name):
    """Build real_knockout dict from completed matches in live_score_history."""
    comp_matches = [
        g for g in live_games
        if g.get("competition", "").strip().lower() == comp_name.lower()
        and g.get("status") == "post"
    ]
    if not comp_matches:
        return None

    rounds = {}
    for g in comp_matches:
        rnd = g.get("round", "").strip()
        if not rnd:
            continue
        rounds.setdefault(rnd, []).append(g)

    real_ko = {}
    for rnd, matches in rounds.items():
        stage_key = _round_to_stage_key(rnd)
        ko_matches = []
        for g in matches:
            hs = g.get("home_score")
            a_s = g.get("away_score")
            try:
                hs_i = int(hs) if hs is not None else None
                a_s_i = int(a_s) if a_s is not None else None
            except (ValueError, TypeError):
                hs_i, a_s_i = None, None

            if hs_i is not None and a_s_i is not None and hs_i != a_s_i:
                winner = g.get("home_team") if hs_i > a_s_i else g.get("away_team")
            else:
                winner = None

            ko_matches.append({
                "home_team": g.get("home_team", ""),
                "away_team": g.get("away_team", ""),
                "winner": winner,
                "home_score": hs_i,
                "away_score": a_s_i,
                "status": g.get("status", "post"),
                "match_date": g.get("match_date", ""),
                "from_live": True,
            })
        if ko_matches:
            real_ko[stage_key] = ko_matches

    return real_ko if real_ko else None


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
    parser.add_argument("--window-days", type=int, default=365, help="Legacy league window days (season-aware bounds apply in predictors).")
    parser.add_argument(
        "--cup-window-days",
        type=int,
        default=180,
        help="Rolling lookahead in days for cup upcoming fixture scripts.",
    )
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

def _condense_real_league_tables():
    """Load real standings for all tracked leagues (including MLS views)."""
    import config as website_config

    website_dir = SP_DIR / "Website"
    if str(website_dir) not in sys.path:
        sys.path.insert(0, str(website_dir))

    comp_names = list(website_config.LIVE_SCORE_COMPETITIONS) + sorted(
        website_config.MLS_TABLE_VIEW_ALIASES
    )
    standings_file = SP_DIR / "Data" / "standings_cache.json"
    if standings_file.exists():
        try:
            cache = json.loads(standings_file.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
        if isinstance(cache, dict):
            tables = {name: cache[name] for name in comp_names if name in cache}
            if tables:
                return tables

    try:
        from standings import _compute_standings_from_history
    except ImportError as exc:
        print(f"[WARN] Could not import standings for mobile feed: {exc}")
        return {}

    tables = {}
    for comp_name in comp_names:
        table = _compute_standings_from_history(comp_name)
        if table:
            tables[comp_name] = table
    return tables


def _merge_projected_brackets(cup_path, mls_path):
    """Combine domestic-cup and MLS playoff brackets into one object."""
    brackets = _condense_cup_brackets(cup_path)
    mls_bracket = _condense_cup_brackets(mls_path)
    if not isinstance(brackets, dict):
        brackets = {}
    if isinstance(mls_bracket, dict) and mls_bracket:
        brackets["United States/MLS"] = mls_bracket
    return brackets


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
        "mls_projected_bracket": MLS_PREDICTIONS_DIR / "projected_mls_playoff_bracket.json",
        "extra_upcoming_fixtures": EXTRA_PREDICTIONS_DIR / "upcoming_matchweek_predictions.csv",
        "extra_projected_league_tables": EXTRA_PREDICTIONS_DIR / "projected_league_tables.csv",
    }

    feed = {
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "schema_version": MOBILE_FEED_SCHEMA_VERSION,
        "pipeline_status": "ok" if pipeline_status else "degraded",
        "step_results": dict(step_results),
        "sources": {name: str(path) for name, path in sources.items()},
        "data": {
            "upcoming_fixtures": _condense_fixtures(sources["upcoming_fixtures_global"])
            + _condense_fixtures(sources["mls_upcoming_fixtures"])
            + _condense_fixtures(sources["extra_upcoming_fixtures"]),
            "upcoming_cup_fixtures": _condense_fixtures(sources["upcoming_fixtures_cups"]),
            "upcoming_national_fixtures": _condense_fixtures(sources["upcoming_fixtures_national"]),
            "projected_league_tables": _condense_tables(sources["projected_league_tables_global"])
            + _condense_tables(sources["mls_projected_league_tables"])
            + _condense_tables(sources["extra_projected_league_tables"]),
            "projected_cup_tables": _condense_tables(sources["projected_cup_tables"]),
            "projected_cup_brackets": _merge_projected_brackets(
                sources["projected_cup_brackets"],
                sources["mls_projected_bracket"],
            ),
            "completed_cup_predictions": _condense_fixtures(sources["completed_cup_predictions"]),
            "real_league_tables": _condense_real_league_tables(),
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
    # Past-game fields (populated only for settled games)
    "actual_result",
    "actual_home_goals",
    "actual_away_goals",
    "is_correct",
    "prediction_key",
    "settled_at_utc",
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


def _publish_windowed_upcoming(output_dir, source_paths):
    """Incrementally update Output/Upcoming/four_week_window.csv.

    Merges in new rows from source CSVs and past_games.json, removes games
    older than 14 days, and never clears the file — if the pipeline fails
    partway through, previously written data survives.
    """
    from zoneinfo import ZoneInfo

    def _row_key(r):
        pk = str(r.get("prediction_key", "") or "").strip()
        if pk:
            return ("pk", pk.lower())
        comp = r.get("competition", "").strip()
        home = r.get("home_team", "").strip()
        away = r.get("away_team", "").strip()
        md = str(r.get("match_date_iso") or r.get("match_date", "")).strip()[:10]
        return ("fixture", comp.lower(), home.lower(), away.lower(), md)

    def _row_date(r):
        raw = str(r.get("match_date_iso") or r.get("match_date", "")).strip()[:10]
        return datetime.strptime(raw, "%Y-%m-%d").date()

    out_dir = output_dir / "Upcoming"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "four_week_window.csv"

    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    window_start = today_et - timedelta(days=14)
    window_end = today_et + timedelta(days=21)

    # ── Load existing rows ──────────────────────────────────────────────
    existing = _read_csv(out_path)
    seen = {}
    for r in existing:
        k = _row_key(r)
        seen[k] = r

    # ── Merge source CSVs ───────────────────────────────────────────────
    for src in source_paths:
        if not src or not src.exists():
            continue
        for r in _read_csv(src):
            k = _row_key(r)
            if k not in seen:
                try:
                    d = _row_date(r)
                except (ValueError, TypeError):
                    continue
                if d < window_start or d > window_end:
                    continue
                seen[k] = r

    # ── Merge past_games.json ───────────────────────────────────────────
    past_games = _load_json(PAST_GAMES_FILE)
    if isinstance(past_games, list):
        for r in past_games:
            k = _row_key(r)
            if k in seen:
                continue
            try:
                d = _row_date(r)
            except (ValueError, TypeError):
                continue
            if d < window_start or d > window_end:
                continue
            out = {f: (r.get(f) or "") for f in _UPCOMING_CSV_FIELDS}
            if not out.get("match_date") and r.get("match_date_iso"):
                out["match_date"] = str(r["match_date_iso"]).strip()[:10]
            seen[k] = out

    # ── Prune rows outside window ───────────────────────────────────────
    rows = list(seen.values())
    kept = []
    for r in rows:
        try:
            d = _row_date(r)
        except (ValueError, TypeError):
            continue
        if d < window_start or d > window_end:
            continue
        kept.append(r)

    if not kept:
        return None

    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_UPCOMING_CSV_FIELDS)
        w.writeheader()
        for r in kept:
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


_MLS_EAST_TEAMS = {
    "Atlanta Utd", "CF Montreal", "Charlotte", "Chicago Fire",
    "Columbus Crew", "DC United", "FC Cincinnati", "Inter Miami",
    "Nashville SC", "New England Revolution", "New York City",
    "New York Red Bulls", "Orlando City", "Philadelphia Union", "Toronto FC",
}
_MLS_WEST_TEAMS = {
    "Austin FC", "Colorado Rapids", "FC Dallas", "Houston Dynamo",
    "Los Angeles Galaxy", "Los Angeles FC", "Minnesota United",
    "Portland Timbers", "Real Salt Lake", "San Diego FC",
    "San Jose Earthquakes", "Seattle Sounders", "Sporting Kansas City",
    "St. Louis City", "Vancouver Whitecaps",
}


def _load_json(path):
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _enrich_with_cup_data(payload, cup_brackets):
    comp = payload.get("competition", "")
    bracket = cup_brackets.get(comp, {}) if isinstance(cup_brackets, dict) else {}
    payload["has_knockout_data"] = bool(bracket.get("rounds") or bracket.get("knockout"))
    payload["has_winner_odds"] = bool(bracket.get("winner_probabilities"))
    if bracket.get("winner_probabilities"):
        payload["winner_probabilities"] = bracket["winner_probabilities"]
    if bracket.get("champion"):
        payload["champion"] = bracket["champion"]
    if bracket.get("simulations_run"):
        payload["simulations_run"] = bracket["simulations_run"]
    if bracket.get("rounds"):
        payload["rounds"] = bracket["rounds"]
    if bracket.get("knockout"):
        payload["knockout"] = bracket["knockout"]
    if bracket.get("groups"):
        payload["groups"] = bracket["groups"]
    return payload


def _enrich_mls_data(payload, mls_bracket, mls_table_path):
    if not mls_bracket:
        return payload
    comp = payload.get("competition", "")
    if comp == "United States/MLS":
        cup_probs = mls_bracket.get("mls_cup_winner_probabilities") or {}
        payload["has_knockout_data"] = True
        payload["has_winner_odds"] = True
        payload["winner_probabilities"] = cup_probs
        payload["champion"] = max(cup_probs, key=cup_probs.get) if cup_probs else None
        payload["simulations_run"] = mls_bracket.get("simulations_run", 0)
        payload["knockout"] = {
            "rounds": mls_bracket.get("rounds", []),
            "mls_cup": mls_bracket.get("mls_cup", {}),
            "wildcard": mls_bracket.get("wildcard", {}),
            "round_one": mls_bracket.get("round_one", []),
            "conference_semifinals": mls_bracket.get("conference_semifinals", []),
            "conference_finals": mls_bracket.get("conference_finals", []),
        }
        # Compute Supporters Shield, East/West winner probabilities from table rows
        teams = payload.get("teams", [])
        shield_odds = {}
        east_odds = {}
        west_odds = {}
        for t in teams:
            name = t.get("team", "")
            win_league = t.get("win_league_pct", 0)
            if win_league and win_league > 0:
                shield_odds[name] = round(win_league * 100, 2)
            pos_odds = t.get("position_odds", {})
            # Eastern Conference: positions 1-9 typically
            east_candidates = [name for name in shield_odds if name in _MLS_EAST_TEAMS]
            west_candidates = [name for name in shield_odds if name in _MLS_WEST_TEAMS]
        if shield_odds:
            payload["supporters_shield_odds"] = dict(sorted(shield_odds.items(), key=lambda x: -x[1]))
        # Re-read MLS table CSV for conference-specific odds
        if mls_table_path and mls_table_path.exists():
            mls_rows = _read_csv(mls_table_path)
            for r in mls_rows:
                tn = r.get("team", "").strip()
                wp = _row_to_float(r.get("win_league_pct"))
                if wp <= 0:
                    continue
                if tn in _MLS_EAST_TEAMS:
                    east_odds[tn] = round(wp * 100, 2)
                elif tn in _MLS_WEST_TEAMS:
                    west_odds[tn] = round(wp * 100, 2)
        if east_odds:
            payload["east_winner_odds"] = dict(sorted(east_odds.items(), key=lambda x: -x[1]))
        if west_odds:
            payload["west_winner_odds"] = dict(sorted(west_odds.items(), key=lambda x: -x[1]))
    elif comp in ("United States/MLS - Eastern Conference", "United States/MLS - Western Conference"):
        payload["has_knockout_data"] = True
        payload["has_winner_odds"] = True
        conf_key = "east" if "Eastern" in comp else "west"
        odds_key = f"{conf_key}_winner_odds"
        if mls_bracket.get(odds_key):
            payload["winner_probabilities"] = mls_bracket[odds_key]
            payload["champion"] = max(mls_bracket[odds_key], key=mls_bracket[odds_key].get) if mls_bracket[odds_key] else None
    elif comp == "United States/MLS - Supporters Shield Table":
        payload["has_winner_odds"] = True
        if mls_bracket.get("supporters_shield_odds"):
            payload["winner_probabilities"] = mls_bracket["supporters_shield_odds"]
            payload["champion"] = max(mls_bracket["supporters_shield_odds"], key=mls_bracket["supporters_shield_odds"].get) if mls_bracket["supporters_shield_odds"] else None
    return payload


# ---------------------------------------------------------------------------
# Player event tracking — goals, assists, yellow/red cards per comp per season
# ---------------------------------------------------------------------------

_PLAYER_STATS_DIR = SP_DIR / "Data" / "PlayerStats"
_ESPN_STATS_IDS = {
    "England/Premier League": "eng.1",
    "England/Championship": "eng.2",
    "Spain/La Liga": "esp.1",
    "Spain/La Liga 2": "esp.2",
    "Italy/Serie A": "ita.1",
    "Italy/Serie B": "ita.2",
    "Germany/Bundesliga": "ger.1",
    "Germany/Bundesliga 2": "ger.2",
    "France/Ligue 1": "fra.1",
    "France/Ligue 2": "fra.2",
    "Portugal/Liga Portugal": "por.1",
    "Netherlands/Eredivisie": "ned.1",
    "United States/MLS": "usa.1",
    "Mexico/Liga MX": "mex.1",
    "Belgium/First Division A": "bel.1",
    "Scotland/Premiership": "sco.1",
    "Turkey/Super Lig": "tur.1",
    "UEFA/Champions League": "uefa.champions",
    "UEFA/Europa League": "uefa.europa",
    "UEFA/Conference League": "uefa.europa.conf",
    "Europe/Champions League": "uefa.champions",
    "Europe/Europa League": "uefa.europa",
    "Europe/Conference League": "uefa.europa.conf",
    "England/FA Cup": "eng.fa",
    "England/League Cup": "eng.efl",
    "Germany/DFB-Pokal": "ger.dfb_pokal",
    "Spain/Copa del Rey": "esp.copa_del_rey",
    "Italy/Coppa Italia": "ita.coppa",
    "France/Coupe de France": "fra.coupe_de_france",
    "Argentina/Primera Division": "arg.1",
    "Brazil/Brasileirão": "bra.1",
    "Japan/J1 League": "jpn.1",
    "Austria/Bundesliga": "aut.1",
    "Switzerland/Super League": "sui.1",
    "Greece/Super League": "gre.1",
    "Denmark/Danish Superliga": "den.1",
    "Ukraine/Premier League": "ukr.1",
    "Norway/Eliteserien": "nor.1",
    "Croatia/HNL": "cro.1",
    "Romania/Liga I": "rou.1",
    "Sweden/Allsvenskan": "swe.1",
    "Hungary/NB I": "hun.1",
    "Israel/Premier League": "isr.1",
    "Czech Republic/First League": "cze.1",
    "Poland/Ekstraklasa": "pol.1",
    "Serbia/SuperLiga": "srb.1",
    "Cyprus/First Division": "cyp.1",
    "Slovakia/Super Liga": "svk.1",
    "Slovenia/PrvaLiga": "svn.1",
    "Bulgaria/First League": "bul.1",
}


def _comp_stats_path(comp_name):
    slug = comp_name.replace("/", "_").replace(" ", "_").lower()
    _PLAYER_STATS_DIR.mkdir(parents=True, exist_ok=True)
    return _PLAYER_STATS_DIR / f"{slug}.json"


def _load_comp_player_stats(comp_name):
    path = _comp_stats_path(comp_name)
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {"season": "", "players": {}, "processed_ids": [], "updated_at": ""}


def _save_comp_player_stats(comp_name, data):
    path = _comp_stats_path(comp_name)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, default=str)


def _current_season():
    now = datetime.now()
    return f"{now.year - 1}-{now.year}" if now.month < 8 else f"{now.year}-{now.year + 1}"


def _extract_match_player_events(match):
    """Return {player: {team, goals, assists, yellow_cards, red_cards}} from one match."""
    events = {}
    home = match.get("home_team", "")
    away = match.get("away_team", "")

    def _side_to_team(side):
        return home if side == "home" else away

    def _inc(player, team, key):
        if not player or not team:
            return
        if player not in events:
            events[player] = {"team": team, "goals": 0, "assists": 0, "yellow_cards": 0, "red_cards": 0}
        events[player][key] += 1

    for g in match.get("goalscorers") or []:
        _inc(g.get("scorer", ""), _side_to_team(g.get("team", "")), "goals")
    for a in match.get("assists") or []:
        _inc(a.get("assister", ""), _side_to_team(a.get("team", "")), "assists")
    for y in match.get("yellow_cards") or []:
        _inc(y.get("player", ""), _side_to_team(y.get("team", "")), "yellow_cards")
    for r in match.get("red_cards") or []:
        _inc(r.get("player", ""), _side_to_team(r.get("team", "")), "red_cards")

    return events


def _process_player_stats(live_history, existing_stats):
    """Process completed matches incrementally, return updated existing_stats in-place."""
    season = _current_season()
    for match in live_history:
        if match.get("status") != "post":
            continue
        mid = str(match.get("match_id", "")).strip()
        if not mid:
            mid = f"fixture:{match.get('match_date', '')}|{match.get('competition', '')}|{match.get('home_team', '')}|{match.get('away_team', '')}"
        comp = match.get("competition", "")
        if not comp:
            continue

        if comp not in existing_stats:
            existing_stats[comp] = {"season": "", "players": {}, "processed_ids": [], "updated_at": ""}
        st = existing_stats[comp]

        if mid in st.get("processed_ids", []):
            continue
        if st.get("season") != season:
            st["season"] = season
            st["players"] = {}
            st["processed_ids"] = []

        match_events = _extract_match_player_events(match)
        for player_name, stats in match_events.items():
            if player_name not in st["players"]:
                st["players"][player_name] = {"team": stats["team"], "goals": 0, "assists": 0, "yellow_cards": 0, "red_cards": 0}
            else:
                st["players"][player_name]["team"] = stats["team"]
            for key in ("goals", "assists", "yellow_cards", "red_cards"):
                st["players"][player_name][key] += stats[key]

        st.setdefault("processed_ids", []).append(mid)
        st["updated_at"] = datetime.now(timezone.utc).isoformat()

    return existing_stats


def _leaders_from_player_stats(comp_stats, top_n=30):
    """Build leaders categories dict from per-competition player stats."""
    players = comp_stats.get("players", {})
    buckets = {"goals": [], "assists": [], "yellow_cards": [], "red_cards": []}
    for pname, pstats in players.items():
        for cat in buckets:
            val = pstats.get(cat, 0)
            if val > 0:
                buckets[cat].append({"player": pname, "team": pstats.get("team", ""), "value": val})

    cat_labels = {"goals": "Goals", "assists": "Assists", "yellow_cards": "Yellow Cards", "red_cards": "Red Cards"}
    categories = {}
    for cat, label in cat_labels.items():
        entries = sorted(buckets[cat], key=lambda x: -x["value"])[:top_n]
        if entries:
            categories[cat] = {
                "label": label,
                "entries": [{"rank": i + 1, **e} for i, e in enumerate(entries)],
            }
    return categories


def _fetch_espn_leaders_for_comp(comp_name, espn_id, timeout=15, top_n=30):
    """Fetch ESPN season leaders and return categories dict (or None)."""
    base = "https://site.api.espn.com/apis/site/v2/sports/soccer"
    now = datetime.now()
    seasons = [str(now.year)]
    if now.month < 8:
        seasons.append(str(now.year - 1))
    data = None
    for s in seasons:
        url = f"{base}/{espn_id}/statistics/leaders?season={s}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            if data and data.get("leaders"):
                break
        except Exception:
            continue
    if not data or not data.get("leaders"):
        return None

    cat_map = {"goals": "goals", "assists": "assists", "yellowCards": "yellow_cards", "redCards": "red_cards"}
    categories = {}
    for cat in data.get("leaders", []):
        abbr = cat.get("abbreviation", "")
        our_cat = cat_map.get(abbr)
        if not our_cat:
            continue
        entries = []
        for rank, entry in enumerate((cat.get("leaders") or [])[:top_n], 1):
            athlete = entry.get("athlete") or {}
            team_info = athlete.get("team") or {}
            pname = str(athlete.get("displayName", "") or "")
            tname = str(team_info.get("displayName", "") or "")
            raw = entry.get("value", entry.get("displayValue", ""))
            try:
                val = int(float(raw))
            except (ValueError, TypeError):
                val = 0
            if pname:
                entries.append({"rank": rank, "player": pname, "team": tname, "value": val})
        if entries:
            label = {"goals": "Goals", "assists": "Assists", "yellow_cards": "Yellow Cards", "red_cards": "Red Cards"}.get(our_cat, our_cat)
            categories[our_cat] = {"label": label, "entries": entries}

    return categories if categories else None


def _merge_espn_leaders_into_stats(comp_stats, espn_categories):
    """Fill zero-value categories from ESPN leaders data (preserves incremental counts)."""
    players = comp_stats.setdefault("players", {})
    changed = False
    cat_reverse = {"goals": "goals", "assists": "assists", "yellow_cards": "yellow_cards", "red_cards": "red_cards"}
    for our_cat, espn_cat in cat_reverse.items():
        cat_data = espn_categories.get(espn_cat) if isinstance(espn_categories, dict) else None
        if not cat_data:
            continue
        for entry in cat_data.get("entries", []):
            pname = entry.get("player", "")
            tname = entry.get("team", "")
            val = entry.get("value", 0)
            if not pname or not isinstance(val, (int, float)) or val <= 0:
                continue
            if pname not in players:
                players[pname] = {"team": tname, "goals": 0, "assists": 0, "yellow_cards": 0, "red_cards": 0}
                players[pname][our_cat] = int(val)
                changed = True
            elif players[pname].get(our_cat, 0) == 0:
                players[pname][our_cat] = int(val)
                changed = True
    return comp_stats, changed


def build_player_standings():
    """Main entry: process live_score_history, optionally fetch ESPN leaders, save per-comp files.

    Returns dict of {comp_name: leaders_categories} for enrichment.
    """
    # ── 1. Load live history ──
    hist_file = SP_DIR / "Data" / "live_score_history.json"
    live_history = _load_json(hist_file) if hist_file.exists() else []
    live_history = live_history if isinstance(live_history, list) else []

    # ── 2. Load existing per-comp stats ──
    existing = {}
    hist_comps = set()
    for m in live_history:
        c = m.get("competition", "")
        if c:
            hist_comps.add(c)
    for comp in hist_comps:
        existing[comp] = _load_comp_player_stats(comp)

    # ── 3. Incremental processing ──
    _process_player_stats(live_history, existing)

    # ── 4. Try ESPN bulk fetch for each competition ──
    for comp in hist_comps:
        espn_id = _ESPN_STATS_IDS.get(comp)
        if not espn_id:
            continue
        st = existing.get(comp, {})
        espn_cats = _fetch_espn_leaders_for_comp(comp, espn_id)
        if espn_cats:
            merged, changed = _merge_espn_leaders_into_stats(st, espn_cats)
            if changed:
                existing[comp] = merged

    # ── 5. Persist and build leaders output ──
    leaders_by_comp = {}
    for comp in hist_comps:
        st = existing.get(comp, {})
        if st.get("players"):
            _save_comp_player_stats(comp, st)
            cats = _leaders_from_player_stats(st)
            if cats:
                leaders_by_comp[comp] = {
                    "updated_at": st.get("updated_at", ""),
                    "season": st.get("season", ""),
                    "categories": cats,
                }
    return leaders_by_comp


def _publish_enriched_competition_data(output_dir, cup_brackets_path, mls_bracket_path, mls_table_path, written):
    """Enrich per-competition JSON files with winner odds, knockout data, metadata."""
    cup_brackets = _load_json(cup_brackets_path) if cup_brackets_path and cup_brackets_path.exists() else {}
    mls_bracket = _load_json(mls_bracket_path) if mls_bracket_path and mls_bracket_path.exists() else {}

    # Sources for real knockout brackets and cup team discovery
    live_history_file = SP_DIR / "Data" / "live_score_history.json"
    live_history = _load_json(live_history_file) if live_history_file.exists() else []
    live_history = live_history if isinstance(live_history, list) else []
    cup_upcoming = _read_csv(PREDICTIONS_DIR / "upcoming_cup_predictions.csv")
    cup_completed = _read_csv(PREDICTIONS_DIR / "completed_cup_predictions.csv")

    # Player standings leaders (computed earlier if build_player_standings was called)
    player_leaders = written.get("_player_leaders", {})

    enriched_count = 0
    for region in ("Europe", "Other"):
        league_dir = output_dir / region / LEAGUE_RESULT_SUBDIR
        if not league_dir.exists():
            continue
        for json_path in sorted(league_dir.glob("*.json")):
            payload = _load_json(json_path)
            if not payload or not isinstance(payload, dict):
                continue
            payload["has_knockout_data"] = False
            payload["has_winner_odds"] = False
            comp = payload.get("competition", "")
            _enrich_with_cup_data(payload, cup_brackets)
            _enrich_mls_data(payload, mls_bracket, mls_table_path)
            # Compute win_league_pct-based winner odds if not already set
            if not payload.get("has_winner_odds") and payload.get("teams"):
                wp_odds = {}
                for t in payload["teams"]:
                    wp = t.get("win_league_pct", 0)
                    if wp and wp > 0:
                        wp_odds[t.get("team", "")] = round(wp * 100, 2)
                if wp_odds:
                    payload["has_winner_odds"] = True
                    payload["winner_probabilities"] = dict(sorted(wp_odds.items(), key=lambda x: -x[1]))
                    payload["champion"] = max(wp_odds, key=wp_odds.get) if wp_odds else None
            # Randomized Monte Carlo simulation for cups without bracket data
            if not payload.get("has_winner_odds") and _is_knockout_format(comp):
                teams = [t.get("team", "") for t in (payload.get("teams") or []) if t.get("team", "")]
                if len(teams) < 2:
                    teams = _discover_cup_teams(comp, cup_upcoming, cup_completed)
                if len(teams) >= 2:
                    cup_sims = _CUP_SIM_RUNS.get(comp, 250)
                    rnd_odds = _run_randomized_cup_simulation(teams, cup_sims)
                    if rnd_odds:
                        payload["has_winner_odds"] = True
                        payload["winner_probabilities"] = dict(
                            sorted(rnd_odds.items(), key=lambda x: -x[1])
                        )
                        payload["champion"] = max(rnd_odds, key=rnd_odds.get)
                        payload["simulations_run"] = cup_sims
            # Real knockout bracket from live score history
            real_ko = _build_real_knockout_for_comp(live_history, comp)
            if real_ko:
                payload["real_knockout"] = real_ko
            # Player leaders from incremental event tracking
            comp_leaders = player_leaders.get(comp)
            if comp_leaders:
                payload["leaders"] = comp_leaders
            with json_path.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
            enriched_count += 1
    return enriched_count


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
        PREDICTIONS_DIR / "projected_future_matches.csv",
        MLS_PREDICTIONS_DIR / "upcoming_matchweek_predictions.csv",
        EXTRA_PREDICTIONS_DIR / "upcoming_matchweek_predictions.csv",
        EXTRA_PREDICTIONS_DIR / "projected_future_matches.csv",
    ]
    for region in ("europe", "other"):
        path = _publish_upcoming_csv(output_dir, upcoming_sources, region)
        if path:
            written[region]["upcoming"] = str(path)
    nat_sources = [PREDICTIONS_DIR / "upcoming_national_team_predictions.csv"]
    friendlies_sources = [PREDICTIONS_DIR / "upcoming_club_friendlies.csv"]
    path = _publish_upcoming_csv(output_dir, nat_sources, "national")
    if path:
        written["national"]["upcoming"] = str(path)

    combined = _publish_all_upcoming(output_dir, upcoming_sources + nat_sources + friendlies_sources)
    if combined:
        written["combined"]["all_upcoming"] = str(combined)

    windowed = _publish_windowed_upcoming(output_dir, upcoming_sources + nat_sources + friendlies_sources)
    if windowed:
        written["combined"]["four_week_window"] = str(windowed)

    # Build per-competition player standings from live-score events + ESPN leaders
    player_leaders = build_player_standings()
    written["_player_leaders"] = player_leaders

    _publish_enriched_competition_data(
        output_dir,
        PREDICTIONS_DIR / "projected_cup_brackets.json",
        MLS_PREDICTIONS_DIR / "projected_mls_playoff_bracket.json",
        MLS_PREDICTIONS_DIR / "projected_league_tables.csv",
        written,
    )

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
        failed_steps = sorted(k for k, v in results.items() if not v)
        log_stats = pipeline_log.log_stats()
        log_snapshot = pipeline_log.read_log(tail=2000, level="notable", highlights_limit=80)
        pfile = SP_DIR / "Data" / "pipeline_status.json"
        pfile.parent.mkdir(parents=True, exist_ok=True)
        pfile.write_text(
            json.dumps({
                "finished_utc": now.isoformat(),
                "total_steps": len(results),
                "passed": passed,
                "failed": failed,
                "ok": failed == 0,
                "failed_steps": failed_steps,
                "steps": {k: bool(v) for k, v in sorted(results.items())},
                "log_file": log_stats.get("log_file"),
                "log_bytes": log_stats.get("bytes", 0),
                "log_lines": log_stats.get("lines", 0),
                "log_highlights": log_snapshot.get("highlights", []),
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
        tee = None
        if not os.environ.get("BTS_BACKEND_MANAGED"):
            tee = pipeline_log.activate_stdout_tee(trigger="daily")
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

        # Rebuild Output/Upcoming/all_upcoming.csv (and related Output trees)
        # so /api/upcoming/global prefers a fresh merged file. Mobile feed
        # generation remains disabled (not currently used).
        try:
            publish_to_output()
        except Exception as exc:
            print(f"[WARN] publish_to_output failed: {exc}")
        if tee is not None:
            pipeline_log.deactivate_stdout_tee()

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
