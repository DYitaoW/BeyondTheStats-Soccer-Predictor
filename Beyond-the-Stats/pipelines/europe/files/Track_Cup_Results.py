
import os as _os_paths_setup
import sys as _sys_paths_setup
_FILES_DIR = _os_paths_setup.path.dirname(_os_paths_setup.path.abspath(__file__))
_REGION_DIR = _os_paths_setup.path.dirname(_FILES_DIR)  # pipelines/europe
_SP_DIR = _os_paths_setup.path.dirname(_os_paths_setup.path.dirname(_REGION_DIR))  # Beyond-the-Stats
if _SP_DIR not in _sys_paths_setup.path:
    _sys_paths_setup.path.insert(0, _SP_DIR)
from shared import paths as _bts_paths
import projection_cache as proj_cache
if str(_bts_paths.SHARED_DIR) not in _sys_paths_setup.path:
    _sys_paths_setup.path.insert(0, str(_bts_paths.SHARED_DIR))
BASE_DIR = str(_bts_paths.SP_DIR)  # europe uses project-level Data/
PREDICTIONS_DIR = str(_bts_paths.OUTPUT_PRED_CUPS)
PROJECT_DIR = str(_bts_paths.SP_DIR)
import json
from pathlib import Path
import os
import random
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from Update_Live_Prediction_Results import (
    ACCURACY_TOTALS_FILE,
    ESPN_BASE,
    PAST_GAMES_FILE,
    SHARED_MAPPING_FILE,
    apply_mapping_updates,
    fetch_json,
    infer_result_code,
    load_accuracy_totals,
    load_predictions,
    load_shared_mapping,
    normalize_team_key,
    resolve_espn_team_name,
    save_completed_rows_to_past_games,
    save_json,
    save_mapping,
    update_accuracy_totals_from_frame,
    update_frame_with_results,
)


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREDICTIONS_DIR = PREDICTIONS_DIR
CUP_PREDICTIONS_FILE = os.path.join(PREDICTIONS_DIR, "upcoming_cup_predictions.csv")
COMPLETED_CUP_PREDICTIONS_FILE = os.path.join(PREDICTIONS_DIR, "completed_cup_predictions.csv")
PROJECTED_CUP_TABLES_FILE = os.path.join(PREDICTIONS_DIR, "projected_cup_tables.csv")
REAL_CUP_TABLES_FILE = os.path.join(PREDICTIONS_DIR, "real_cup_tables.csv")
PROJECTED_CUP_BRACKETS_FILE = os.path.join(PREDICTIONS_DIR, "projected_cup_brackets.json")
ESPN_CUP_NAMES_FILE = os.path.join(PREDICTIONS_DIR, "espn_cup_names_seen.json")

# Sentinel when a projected knockout winner cannot be decided yet (TBD side or no odds).
NO_PREDICTION = "NONE"
CUP_TABLE_SIMULATION_RUNS = 250

# Import season bounds for in-season table backfill (Europe = Sept+).
try:
    import season_calendar as _season_calendar
except ImportError:
    sys.path.insert(0, BASE_DIR)
    import season_calendar as _season_calendar

CUP_ESPN_COMPETITION_KEYS = {
    "England/FA Cup": "eng.fa",
    "England/League Cup": "eng.league_cup",
    "Europe/Champions League": "uefa.champions",
    "Europe/Europa League": "uefa.europa",
    "Europe/Conference League": "uefa.europa.conf",
    "Italy/Coppa Italia": "ita.coppa_italia",
    "Spain/Copa del Rey": "esp.copa_del_rey",
    "Germany/DFB-Pokal": "ger.dfb_pokal",
    "France/Coupe de France": "fra.coupe_de_france",
    "United States/US Open Cup": "usa.open",
    "North America/Leagues Cup": "concacaf.leagues.cup",
}
UEFA_TABLE_COMPETITIONS = {
    "Europe/Champions League",
    "Europe/Europa League",
    "Europe/Conference League",
}
# Cups that publish Phase One / league-phase tables (not pure knockout).
CUP_TABLE_COMPETITIONS = UEFA_TABLE_COMPETITIONS | {
    "North America/Leagues Cup",
}
LEAGUES_CUP_COMPETITION = "North America/Leagues Cup"
LEAGUES_CUP_PHASE_MATCHES = 3
UEFA_LEAGUE_PHASE_MATCHES = {
    "Europe/Champions League": 8,
    "Europe/Europa League": 8,
    "Europe/Conference League": 6,
}
UEFA_PRIMARY_COMPETITIONS = [
    "Europe/Champions League",
    "Europe/Europa League",
    "Europe/Conference League",
]
DOMESTIC_BRACKET_COMPETITIONS = {
    "England/FA Cup",
    "England/League Cup",
}
DOMESTIC_BRACKET_MATCH_LIMIT = 16

CUP_SIMULATION_RUNS = 500

CUP_KNOCKOUT_FEEDS = {
    "First Round Playoff": {"next_round": "Round of 16", "feeds_to": lambda slot: slot},
    "Round of 16": {"next_round": "Quarterfinals", "feeds_to": lambda slot: (slot + 1) // 2},
    "Quarterfinals": {"next_round": "Semifinals", "feeds_to": lambda slot: (slot + 1) // 2},
    "Semifinals": {"next_round": "Final", "feeds_to": lambda slot: 1},
    "Quarter-finals": {"next_round": "Semi-finals", "feeds_to": lambda slot: (slot + 1) // 2},
    "Semi-finals": {"next_round": "Final", "feeds_to": lambda slot: 1},
    "Final": {"next_round": None, "feeds_to": lambda slot: None},
}

# ── Domestic Cup Format Rules ───────────────────────────────────
# Specifies draw rules, 2-leg info, and team eligibility for each cup

CUP_FORMAT_RULES = {
    "England/FA Cup": {
        "format": "domestic_knockout",
        "description": "Single-elimination. Most rounds single-leg. Replays in some early/quarter-final rounds if drawn.",
        "typical_rounds": ["First Round", "Second Round", "Third Round", "Fourth Round", "Fifth Round", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": [],  # Replays exist but treated as separate matches in records
        "final_neutral": True,
        "draw_type": "fully_randomized",
        "allows_lower_league": True,
    },
    "England/League Cup": {
        "format": "domestic_knockout",
        "description": "Single-elimination. Semi-finals are two-legged. Final at neutral venue.",
        "typical_rounds": ["First Round", "Second Round", "Third Round", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": ["Semi-finals"],
        "final_neutral": True,
        "draw_type": "fully_randomized",
        "allows_lower_league": True,
    },
    "Spain/Copa del Rey": {
        "format": "domestic_knockout",
        "description": "Single-elimination knockout. Semi-finals are two-legged. Final at neutral venue.",
        "typical_rounds": ["Preliminary", "First Round", "Second Round", "Third Round", "Round of 32", "Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": ["Semi-finals"],
        "final_neutral": True,
        "draw_type": "fully_randomized",
        "allows_lower_league": True,
    },
    "Germany/DFB-Pokal": {
        "format": "domestic_knockout",
        "description": "Single-elimination knockout. All rounds are single match. Final at neutral venue.",
        "typical_rounds": ["First Round", "Second Round", "Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": [],
        "final_neutral": True,
        "draw_type": "fully_randomized",
        "allows_lower_league": True,
    },
    "France/Coupe de France": {
        "format": "domestic_knockout",
        "description": "Single-elimination knockout. All rounds are single match. Final at neutral venue.",
        "typical_rounds": ["First Round", "Second Round", "Third Round", "Fourth Round", "Fifth Round", "Sixth Round", "Seventh Round", "Eighth Round", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": [],
        "final_neutral": True,
        "draw_type": "fully_randomized",
        "allows_lower_league": True,
    },
    "Italy/Coppa Italia": {
        "format": "domestic_knockout",
        "description": "Single-elimination knockout. Semi-finals are two-legged. Final at neutral venue.",
        "typical_rounds": ["First Round", "Second Round", "Third Round", "Fourth Round", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": ["Semi-finals"],
        "final_neutral": True,
        "draw_type": "fully_randomized",
        "allows_lower_league": True,
    },
    "United States/US Open Cup": {
        "format": "domestic_knockout",
        "description": "Single-elimination knockout. All rounds are single match.",
        "typical_rounds": ["First Round", "Second Round", "Third Round", "Fourth Round", "Round of 16", "Quarter-finals", "Semi-finals", "Final"],
        "two_leg_rounds": [],
        "final_neutral": False,
        "draw_type": "fully_randomized",
        "allows_lower_league": True,
    },
    "North America/Leagues Cup": {
        "format": "dual_league_phase_then_knockout",
        "description": (
            "Phase One: 3 MLS↔Liga MX matches per club; separate MLS and Liga MX "
            "tables; top 4 each advance to Quarter-finals. No draws (3/2/1 points)."
        ),
        "typical_rounds": ["Phase One", "Quarter-finals", "Semi-finals", "Third Place", "Final"],
        "two_leg_rounds": [],
        "final_neutral": True,
        "draw_type": "seeded_dual_tables",
        "allows_lower_league": False,
        "no_draws": True,
        "advance_per_table": 4,
    },
}

# Update DOMESTIC_BRACKET_COMPETITIONS to reflect all domestic cups with rules
DOMESTIC_BRACKET_COMPETITIONS = {
    "England/FA Cup",
    "England/League Cup",
    "Spain/Copa del Rey",
    "Germany/DFB-Pokal",
    "France/Coupe de France",
    "Italy/Coppa Italia",
    "United States/US Open Cup",
    "North America/Leagues Cup",
}

CUP_HISTORY_COLUMNS = [
    "prediction_key",
    "created_at_utc",
    "match_date",
    "match_datetime_utc",
    "match_datetime_et",
    "competition",
    "home_team",
    "away_team",
    "predicted_result",
    "probability_reasoning",
    "prob_home",
    "prob_draw",
    "prob_away",
    "pred_home_goals",
    "pred_away_goals",
    "pred_home_shots",
    "pred_away_shots",
    "pred_home_sot",
    "pred_away_sot",
    "actual_home_goals",
    "actual_away_goals",
    "actual_result",
    "is_correct",
    "settled_at_utc",
]

TABLE_COLUMNS = [
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
    "PlayedReal",
    "PlayedPred",
    "win_league_pct",
    "top4_pct",
    "bottom3_pct",
    "most_likely_position",
    "most_likely_position_pct",
    "position_odds_json",
    "sim_runs",
]


def _empty_frame(columns):
    return pd.DataFrame(columns=columns)


def _ensure_columns(frame, columns):
    out = frame.copy() if frame is not None else _empty_frame(columns)
    for col in columns:
        if col not in out.columns:
            out[col] = None
    return out


def _load_completed_cups():
    if not os.path.exists(COMPLETED_CUP_PREDICTIONS_FILE):
        return _empty_frame(CUP_HISTORY_COLUMNS)
    try:
        frame = pd.read_csv(COMPLETED_CUP_PREDICTIONS_FILE)
    except Exception:
        return _empty_frame(CUP_HISTORY_COLUMNS)
    return _ensure_columns(frame, CUP_HISTORY_COLUMNS)


def cup_table_season_bounds(competition_name, reference_date=None):
    """Inclusive (start, end) dates for games that may seed a cup phase table.

    UEFA club competitions: Sept 1 of the active European season → May 31.
    Leagues Cup: Jul 1 → Sep 30 of the current calendar year.

    Does **not** clamp to today — callers that fetch completed results should
    use ``min(end, today)`` themselves so upcoming in-season fixtures remain.
    """
    today = pd.Timestamp(datetime.now(UTC).date()).normalize()
    ref = pd.Timestamp(reference_date).normalize() if reference_date is not None else today
    comp = str(competition_name or "").strip()
    if comp in UEFA_TABLE_COMPETITIONS:
        start, end = _season_calendar.european_cup_table_season_bounds(ref)
    elif comp == LEAGUES_CUP_COMPETITION:
        start, end = _season_calendar.leagues_cup_season_bounds(ref)
    else:
        start, end = _season_calendar.european_cup_table_season_bounds(ref)
    return start.normalize(), end.normalize()


def _filter_frame_to_cup_table_season(frame, competition_name=None):
    """Keep only rows whose match_date falls in the competition's table season."""
    if frame is None or frame.empty:
        return _empty_frame(CUP_HISTORY_COLUMNS) if frame is None else frame.iloc[0:0].copy()
    out = frame.copy()
    out["__match_date"] = pd.to_datetime(out.get("match_date"), errors="coerce").dt.normalize()
    if competition_name:
        start, end = cup_table_season_bounds(competition_name)
        mask = (
            (out["competition"].astype(str).str.strip() == competition_name)
            & out["__match_date"].notna()
            & (out["__match_date"] >= start)
            & (out["__match_date"] <= end)
        )
        other = out["competition"].astype(str).str.strip() != competition_name
        kept = out[mask | other].drop(columns=["__match_date"], errors="ignore")
        return kept

    # Per-competition filter for all table cups; drop out-of-season table-cup rows.
    keep_masks = []
    for comp in out["competition"].astype(str).str.strip().unique():
        comp_mask = out["competition"].astype(str).str.strip() == comp
        if comp not in CUP_TABLE_COMPETITIONS:
            keep_masks.append(comp_mask)
            continue
        start, end = cup_table_season_bounds(comp)
        keep_masks.append(
            comp_mask
            & out["__match_date"].notna()
            & (out["__match_date"] >= start)
            & (out["__match_date"] <= end)
        )
    if not keep_masks:
        return out.drop(columns=["__match_date"], errors="ignore")
    combined_mask = keep_masks[0]
    for m in keep_masks[1:]:
        combined_mask = combined_mask | m
    return out.loc[combined_mask].drop(columns=["__match_date"], errors="ignore")


def _espn_events_to_completed_rows(competition, league_key, events, mapping_by_competition, season_start, season_end):
    """Parse ESPN scoreboard events into completed cup history rows (in season)."""
    rows = []
    mapping_updates = {}
    unresolved = set()
    seen_names = set()
    predicted_team_names = set()
    # Prefer mapping canons as known names when we have no upcoming frame.
    try:
        for name in (mapping_by_competition.get(competition) or {}).values():
            text = str(name or "").strip()
            if text:
                predicted_team_names.add(text)
    except Exception:
        pass

    for event in events or []:
        if not isinstance(event, dict):
            continue
        dt = pd.to_datetime(event.get("date"), utc=True, errors="coerce")
        if pd.isna(dt):
            continue
        try:
            match_date = dt.tz_convert("UTC").tz_localize(None).normalize()
        except Exception:
            match_date = pd.Timestamp(dt).tz_localize(None).normalize() if getattr(dt, "tzinfo", None) else pd.Timestamp(dt).normalize()
        if match_date < season_start or match_date > season_end:
            continue

        event_competitions = event.get("competitions", [])
        if not event_competitions:
            continue
        comp0 = event_competitions[0] or {}
        status_type = ((comp0.get("status") or {}).get("type") or {})
        if not bool(status_type.get("completed")):
            continue

        # Prefer league-phase / group / phase-one notes; skip explicit knockout labels.
        round_note = ""
        for note in (comp0.get("notes") or []):
            if isinstance(note, dict):
                round_note = str(note.get("headline") or note.get("text") or "").strip()
            else:
                round_note = str(note or "").strip()
            if round_note:
                break
        if not round_note:
            round_note = str(
                ((event.get("season") or {}).get("slug"))
                or (event.get("name") or "")
                or ""
            )
        lower = round_note.lower()
        knockout_tokens = (
            "round of", "quarter", "semi", "final", "playoff", "play-off",
            "knockout", "last 16", "last 32",
        )
        phase_ok = (
            "league phase" in lower
            or "matchday" in lower
            or "group" in lower
            or "phase one" in lower
            or "phase 1" in lower
            or not lower
        )
        if any(tok in lower for tok in knockout_tokens) and not phase_ok:
            continue

        competitors = comp0.get("competitors", [])
        home_name = ""
        away_name = ""
        home_score = None
        away_score = None
        for competitor in competitors:
            side = str(competitor.get("homeAway", "")).strip().lower()
            team_name = str((competitor.get("team") or {}).get("displayName") or "").strip()
            score_val = pd.to_numeric(competitor.get("score"), errors="coerce")
            if side == "home":
                if team_name:
                    seen_names.add(team_name)
                resolved, ok = resolve_cup_team_name(
                    team_name, competition, mapping_by_competition, predicted_team_names or {team_name}
                )
                home_name = resolved if ok else team_name
                if ok and team_name and team_name != home_name:
                    mapping_updates[team_name] = home_name
                if not ok and team_name:
                    unresolved.add(team_name)
                home_score = int(score_val) if pd.notna(score_val) else None
            elif side == "away":
                if team_name:
                    seen_names.add(team_name)
                resolved, ok = resolve_cup_team_name(
                    team_name, competition, mapping_by_competition, predicted_team_names or {team_name}
                )
                away_name = resolved if ok else team_name
                if ok and team_name and team_name != away_name:
                    mapping_updates[team_name] = away_name
                if not ok and team_name:
                    unresolved.add(team_name)
                away_score = int(score_val) if pd.notna(score_val) else None

        if not home_name or not away_name or home_score is None or away_score is None:
            continue
        if _is_unknown_team(home_name) or _is_unknown_team(away_name):
            continue

        rows.append({
            "competition": competition,
            "match_date": match_date.strftime("%Y-%m-%d"),
            "home_team": home_name,
            "away_team": away_name,
            "actual_home_goals": home_score,
            "actual_away_goals": away_score,
            "actual_result": infer_result_code(home_score, away_score),
            "predicted_result": "",
            "round": round_note or "League Phase",
            "settled_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "schedule_only": "0",
        })
    return rows, mapping_updates, unresolved, seen_names


def fetch_in_season_cup_table_results(mapping_by_competition=None):
    """Pull completed in-season league-phase games for UEFA / Leagues Cup tables.

    Backfills older matchdays that never appeared in upcoming_cup_predictions.csv
    so projected/real cup tables include the full current-season phase record.
    European competitions only accept September+ of the active season.
    """
    mapping_by_competition = mapping_by_competition or {}
    all_rows = []
    mapping_updates = {}
    unresolved = {}
    seen_names = {}

    try:
        import espn_api_cache
    except ImportError:
        espn_api_cache = None

    for competition in sorted(CUP_TABLE_COMPETITIONS):
        league_key = CUP_ESPN_COMPETITION_KEYS.get(competition)
        if not league_key:
            continue
        season_start, season_end = cup_table_season_bounds(competition)
        today = pd.Timestamp(datetime.now(UTC).date()).normalize()
        fetch_end = min(season_end, today)
        if season_start > fetch_end:
            continue
        print(
            f"[cup-tables] backfilling {competition} completed phase games "
            f"{season_start.date()} → {fetch_end.date()}",
            flush=True,
        )

        events = []
        if espn_api_cache is not None:
            # UEFA rejects multi-day dates= (HTTP 400); smart walk uses Tue/Wed only.
            events = espn_api_cache.fetch_scoreboard_range(
                league_key,
                season_start.date(),
                fetch_end.date(),
                include_default=True,
                progress_label=f"backfill {competition}",
            ) or []
        else:
            # Legacy fallback when shared cache module is unavailable.
            chunks = [(season_start, fetch_end)]
            span_days = int((fetch_end - season_start).days) + 1
            if span_days > 45:
                chunks = []
                cursor = season_start
                while cursor <= fetch_end:
                    chunk_end = min(cursor + pd.Timedelta(days=44), fetch_end)
                    chunks.append((cursor, chunk_end))
                    cursor = chunk_end + pd.Timedelta(days=1)
            for chunk_start, chunk_end in chunks:
                date_param = f"{chunk_start.strftime('%Y%m%d')}-{chunk_end.strftime('%Y%m%d')}"
                url = f"{ESPN_BASE}/{league_key}/scoreboard?dates={date_param}&limit=1000"
                try:
                    data = fetch_json(url, timeout=60)
                except Exception as error:
                    print(f"  [cup-tables] range fetch failed ({date_param}): {error}; trying daily", flush=True)
                    data = None
                chunk_events = (data or {}).get("events") if isinstance(data, dict) else None
                if isinstance(chunk_events, list) and chunk_events:
                    events.extend(chunk_events)
                    continue
                day = chunk_start
                while day <= chunk_end:
                    day_url = f"{ESPN_BASE}/{league_key}/scoreboard?dates={day.strftime('%Y%m%d')}"
                    try:
                        day_data = fetch_json(day_url, timeout=45)
                        day_events = (day_data or {}).get("events") if isinstance(day_data, dict) else None
                        if isinstance(day_events, list):
                            events.extend(day_events)
                    except Exception as error:
                        print(f"  [cup-tables] skip {competition} {day.date()}: {error}", flush=True)
                    day += pd.Timedelta(days=1)

        rows, maps, unresolved_names, seen = _espn_events_to_completed_rows(
            competition, league_key, events, mapping_by_competition, season_start, fetch_end,
        )
        print(f"  [cup-tables] {competition}: {len(rows)} in-season completed phase rows", flush=True)
        all_rows.extend(rows)
        if maps:
            mapping_updates.setdefault(competition, {}).update(maps)
        if unresolved_names:
            unresolved[competition] = sorted(unresolved_names)
        if seen:
            seen_names[competition] = sorted(seen)

    if not all_rows:
        return _empty_frame(CUP_HISTORY_COLUMNS), mapping_updates, unresolved, seen_names
    frame = _ensure_columns(pd.DataFrame(all_rows), CUP_HISTORY_COLUMNS)
    frame = _filter_frame_to_cup_table_season(frame)
    return frame, mapping_updates, unresolved, seen_names


def _espn_events_to_upcoming_phase_rows(competition, events, season_start, season_end, today):
    """Parse ESPN events into upcoming (pre) league-phase rows for table Monte Carlo."""
    rows = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        dt = pd.to_datetime(event.get("date"), utc=True, errors="coerce")
        if pd.isna(dt):
            continue
        try:
            match_date = dt.tz_convert("UTC").tz_localize(None).normalize()
        except Exception:
            match_date = pd.Timestamp(dt).tz_localize(None).normalize() if getattr(dt, "tzinfo", None) else pd.Timestamp(dt).normalize()
        if match_date < today or match_date < season_start or match_date > season_end:
            continue
        event_competitions = event.get("competitions", [])
        if not event_competitions:
            continue
        comp0 = event_competitions[0] or {}
        status_type = ((comp0.get("status") or {}).get("type") or {})
        state = str(status_type.get("state") or "").strip().lower()
        if state and state not in {"pre"}:
            continue
        if bool(status_type.get("completed")):
            continue

        round_note = ""
        for note in (comp0.get("notes") or []):
            if isinstance(note, dict):
                round_note = str(note.get("headline") or note.get("text") or "").strip()
            else:
                round_note = str(note or "").strip()
            if round_note:
                break
        lower = round_note.lower()
        knockout_tokens = (
            "round of", "quarter", "semi", "final", "playoff", "play-off",
            "knockout", "last 16", "last 32",
        )
        phase_ok = (
            "league phase" in lower
            or "matchday" in lower
            or "group" in lower
            or "phase one" in lower
            or "phase 1" in lower
            or not lower
        )
        if any(tok in lower for tok in knockout_tokens) and not phase_ok:
            continue

        competitors = comp0.get("competitors", [])
        home_name = ""
        away_name = ""
        for competitor in competitors:
            side = str(competitor.get("homeAway", "")).strip().lower()
            team_name = str((competitor.get("team") or {}).get("displayName") or "").strip()
            if side == "home":
                home_name = team_name
            elif side == "away":
                away_name = team_name
        if not home_name or not away_name:
            continue
        if not _is_known_team(home_name) or not _is_known_team(away_name):
            continue
        rows.append(
            {
                "prediction_key": f"{match_date.strftime('%Y-%m-%d')}|{competition}|{home_name}|{away_name}",
                "created_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
                "match_date": match_date.strftime("%Y-%m-%d"),
                "match_datetime_utc": str(event.get("date") or ""),
                "match_datetime_et": "",
                "competition": competition,
                "home_team": home_name,
                "away_team": away_name,
                "predicted_result": "",
                "probability_reasoning": "espn-table-pending",
                "prob_home": None,
                "prob_draw": None,
                "prob_away": None,
                "pred_home_goals": None,
                "pred_away_goals": None,
                "pred_home_shots": None,
                "pred_away_shots": None,
                "pred_home_sot": None,
                "pred_away_sot": None,
                "actual_home_goals": None,
                "actual_away_goals": None,
                "actual_result": "",
                "is_correct": None,
                "settled_at_utc": "",
                "schedule_only": 1,
                "round": round_note or "League Phase",
            }
        )
    return rows


def fetch_upcoming_cup_table_fixtures(mapping_by_competition=None):
    """Fetch remaining in-season league-phase fixtures for table Monte Carlo.

    When ``upcoming_cup_predictions.csv`` is empty (Predict hung or filtered
    everything out), table sims otherwise run with ``sim_runs=0``. Pull ESPN
    ``pre`` fixtures through season end so CL/EL/ECL still get position odds.
    """
    mapping_by_competition = mapping_by_competition or {}
    try:
        import espn_api_cache
    except ImportError:
        print("[cup-tables] espn_api_cache unavailable — cannot seed pending fixtures", flush=True)
        return _empty_frame(CUP_HISTORY_COLUMNS)

    today = pd.Timestamp(datetime.now(UTC).date()).normalize()
    all_rows = []
    for competition in sorted(CUP_TABLE_COMPETITIONS):
        league_key = CUP_ESPN_COMPETITION_KEYS.get(competition)
        if not league_key:
            continue
        season_start, season_end = cup_table_season_bounds(competition)
        fetch_start = max(season_start, today)
        if fetch_start > season_end:
            continue
        print(
            f"[cup-tables] START pending fixtures {competition} "
            f"{fetch_start.date()} → {season_end.date()}",
            flush=True,
        )
        events = espn_api_cache.fetch_scoreboard_range(
            league_key,
            fetch_start.date(),
            season_end.date(),
            include_default=True,
            progress_label=f"pending {competition}",
        ) or []
        # Map ESPN display names through shared mapping when available.
        mapped_events = events
        rows = _espn_events_to_upcoming_phase_rows(
            competition, mapped_events, season_start, season_end, today,
        )
        # Apply competition mapping to resolve ESPN → canonical names.
        if mapping_by_competition and rows:
            mapping = mapping_by_competition.get(competition) or {}
            for row in rows:
                for side in ("home_team", "away_team"):
                    raw = str(row.get(side) or "").strip()
                    canon = mapping.get(raw) or mapping.get(raw.lower())
                    if canon:
                        row[side] = str(canon).strip()
        print(
            f"[cup-tables] DONE  pending fixtures {competition} — {len(rows)} rows",
            flush=True,
        )
        all_rows.extend(rows)

    if not all_rows:
        return _empty_frame(CUP_HISTORY_COLUMNS)
    frame = _ensure_columns(pd.DataFrame(all_rows), CUP_HISTORY_COLUMNS)
    return _filter_frame_to_cup_table_season(frame)

def merge_completed_cup_frames(existing, extra):
    """Union completed cup history frames, preferring rows with actual scores."""
    frames = []
    for frame in (existing, extra):
        if frame is not None and not frame.empty:
            frames.append(_ensure_columns(frame, CUP_HISTORY_COLUMNS))
    if not frames:
        return _empty_frame(CUP_HISTORY_COLUMNS)
    merged = pd.concat(frames, ignore_index=True)
    if merged.empty:
        return _empty_frame(CUP_HISTORY_COLUMNS)
    merged["competition"] = merged["competition"].astype(str).str.strip()
    merged["home_team"] = merged["home_team"].astype(str).str.strip()
    merged["away_team"] = merged["away_team"].astype(str).str.strip()
    merged["match_date"] = merged["match_date"].astype(str).str.strip()
    # Prefer rows that have actual scores when deduping.
    merged["__has_actual"] = (
        pd.to_numeric(merged.get("actual_home_goals"), errors="coerce").notna()
        & pd.to_numeric(merged.get("actual_away_goals"), errors="coerce").notna()
    )
    merged = merged.sort_values("__has_actual", ascending=False)
    merged = merged.drop_duplicates(
        subset=["competition", "match_date", "home_team", "away_team"],
        keep="first",
    )
    return _ensure_columns(merged.drop(columns=["__has_actual"], errors="ignore"), CUP_HISTORY_COLUMNS)


def _write_csv(path, frame, columns=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    out = frame.copy()
    if columns:
        out = _ensure_columns(out, columns)
        ordered = columns + [col for col in out.columns if col not in columns]
        out = out[ordered]
    out.to_csv(path, index=False)


def _event_date_keys(dt_utc):
    keys = set()
    if pd.isna(dt_utc):
        return keys
    keys.add(dt_utc.tz_convert("UTC").strftime("%Y-%m-%d"))
    try:
        keys.add(dt_utc.tz_convert("America/New_York").strftime("%Y-%m-%d"))
    except Exception:
        pass
    return keys


def resolve_cup_team_name(raw_name, competition, mapping_by_competition, predicted_team_names):
    resolved, ok = resolve_espn_team_name(raw_name, competition, mapping_by_competition, predicted_team_names)
    if ok:
        return resolved, True

    raw_key = normalize_team_key(raw_name)
    suffix_matches = []
    for team in predicted_team_names:
        team_key = normalize_team_key(team)
        if raw_key and team_key and (raw_key.endswith(team_key) or team_key.endswith(raw_key)):
            suffix_matches.append(team)
    if len(suffix_matches) == 1:
        return suffix_matches[0], True
    return resolved, False


def build_cup_results_index_from_espn(predictions_df, mapping_by_competition):
    if predictions_df is None or predictions_df.empty:
        return {}, {}, {}, {}

    results = {}
    mapping_updates = {}
    unresolved = {}
    seen_names = {}
    competitions = sorted(set(predictions_df["competition"].astype(str).str.strip()))
    for competition in competitions:
        if not competition:
            continue
        league_key = CUP_ESPN_COMPETITION_KEYS.get(competition)
        if not league_key:
            print(f"Skipping cup {competition}: no ESPN cup mapping configured.")
            continue

        subset = predictions_df[predictions_df["competition"].astype(str).str.strip() == competition]
        if subset.empty:
            continue
        predicted_team_names = set(subset["home_team"].astype(str)) | set(subset["away_team"].astype(str))
        unresolved.setdefault(competition, set())
        seen_names.setdefault(competition, set())

        parsed_dates = pd.to_datetime(subset["match_date"], errors="coerce")
        parsed_dates = parsed_dates[parsed_dates.notna()]
        if parsed_dates.empty:
            continue

        query_days = set()
        for day in sorted(set(pd.Timestamp(dt).normalize() for dt in parsed_dates)):
            query_days.add(day)
            query_days.add(day - pd.Timedelta(days=1))
            query_days.add(day + pd.Timedelta(days=1))

        for day in sorted(query_days):
            url = f"{ESPN_BASE}/{league_key}/scoreboard?dates={day.strftime('%Y%m%d')}"
            try:
                data = fetch_json(url, timeout=45)
            except Exception as error:
                print(f"Skipping cup {competition} date {day.strftime('%Y-%m-%d')}: {error}")
                continue

            events = data.get("events", [])
            if not isinstance(events, list):
                continue

            for event in events:
                dt = pd.to_datetime(event.get("date"), utc=True, errors="coerce")
                if pd.isna(dt):
                    continue
                date_keys = _event_date_keys(dt)
                if not date_keys:
                    continue

                event_competitions = event.get("competitions", [])
                if not event_competitions:
                    continue
                comp0 = event_competitions[0] or {}
                status_type = ((comp0.get("status") or {}).get("type") or {})
                if not bool(status_type.get("completed")):
                    continue

                competitors = comp0.get("competitors", [])
                home_name = ""
                away_name = ""
                home_score = None
                away_score = None
                for competitor in competitors:
                    side = str(competitor.get("homeAway", "")).strip().lower()
                    team_name = str((competitor.get("team") or {}).get("displayName") or "").strip()
                    score_val = pd.to_numeric(competitor.get("score"), errors="coerce")
                    if side == "home":
                        if team_name:
                            seen_names[competition].add(team_name)
                        home_name, home_ok = resolve_cup_team_name(
                            team_name, competition, mapping_by_competition, predicted_team_names
                        )
                        if home_ok and home_name in predicted_team_names and team_name and team_name != home_name:
                            mapping_updates.setdefault(competition, {})
                            mapping_updates[competition].setdefault(team_name, home_name)
                        if not home_ok:
                            unresolved[competition].add(team_name)
                        home_score = int(score_val) if pd.notna(score_val) else None
                    elif side == "away":
                        if team_name:
                            seen_names[competition].add(team_name)
                        away_name, away_ok = resolve_cup_team_name(
                            team_name, competition, mapping_by_competition, predicted_team_names
                        )
                        if away_ok and away_name in predicted_team_names and team_name and team_name != away_name:
                            mapping_updates.setdefault(competition, {})
                            mapping_updates[competition].setdefault(team_name, away_name)
                        if not away_ok:
                            unresolved[competition].add(team_name)
                        away_score = int(score_val) if pd.notna(score_val) else None

                if not home_name or not away_name or home_score is None or away_score is None:
                    continue

                result = {
                    "actual_home_goals": home_score,
                    "actual_away_goals": away_score,
                    "actual_result": infer_result_code(home_score, away_score),
                    "completed": True,
                }
                for date_key in date_keys:
                    key = (date_key, competition, normalize_team_key(home_name), normalize_team_key(away_name))
                    results[key] = result

    unresolved = {k: sorted(v) for k, v in unresolved.items() if v}
    seen_names = {k: sorted(v) for k, v in seen_names.items()}
    return results, mapping_updates, unresolved, seen_names


def append_completed_predictions(existing_completed, settled_frame):
    if settled_frame is None or settled_frame.empty:
        return existing_completed, 0
    settled_mask = settled_frame["actual_result"].astype(str).str.strip().str.upper().isin({"H", "D", "A"})
    new_completed = settled_frame[settled_mask].copy()
    if new_completed.empty:
        return existing_completed, 0

    existing = _ensure_columns(existing_completed, CUP_HISTORY_COLUMNS)
    before_keys = set(existing["prediction_key"].astype(str).str.strip()) if not existing.empty else set()
    merged = pd.concat([existing, new_completed], ignore_index=True)
    merged = _ensure_columns(merged, CUP_HISTORY_COLUMNS)
    merged = merged.drop_duplicates(subset=["prediction_key"], keep="last")
    merged = merged.sort_values(["match_date", "competition", "home_team", "away_team"], na_position="last")
    after_keys = set(merged["prediction_key"].astype(str).str.strip()) if not merged.empty else set()
    added = len(after_keys - before_keys)
    return merged, added


def _drop_completed_rows(frame, today=None):
    """Drop completed (settled) rows, but only for dates *before* today.

    Games from today are kept even if settled so they still appear on
    the website — the frontend can show the actual result alongside
    the prediction.  Pass ``today=None`` (default) to drop all settled
    rows regardless of date (legacy / cleanup mode).
    """
    if frame is None or frame.empty or "actual_result" not in frame.columns:
        return frame, 0
    settled_mask = frame["actual_result"].astype(str).str.strip().str.upper().isin({"H", "D", "A"})
    if today is not None and "match_date" in frame.columns:
        parsed_dates = pd.to_datetime(frame["match_date"], errors="coerce").dt.normalize()
        today_ts = pd.Timestamp(today)
        drop_mask = settled_mask & (parsed_dates < today_ts)
    else:
        drop_mask = settled_mask
    removed = int(drop_mask.sum())
    if removed == 0:
        return frame, 0
    return frame[~drop_mask].copy(), removed


def _numeric_int(value, default=0):
    num = pd.to_numeric(value, errors="coerce")
    if pd.isna(num):
        return default
    return int(round(float(num)))


def _table_row():
    return {
        "P": 0,
        "W": 0,
        "D": 0,
        "L": 0,
        "GF": 0,
        "GA": 0,
        "GD": 0,
        "Pts": 0,
        "PlayedReal": 0,
        "PlayedPred": 0,
    }


def _apply_result(table, home, away, hg, ag, is_real):
    home_stats = table.setdefault(home, _table_row())
    away_stats = table.setdefault(away, _table_row())
    home_stats["P"] += 1
    away_stats["P"] += 1
    home_stats["GF"] += hg
    home_stats["GA"] += ag
    away_stats["GF"] += ag
    away_stats["GA"] += hg
    home_stats["GD"] = home_stats["GF"] - home_stats["GA"]
    away_stats["GD"] = away_stats["GF"] - away_stats["GA"]
    if is_real:
        home_stats["PlayedReal"] += 1
        away_stats["PlayedReal"] += 1
    else:
        home_stats["PlayedPred"] += 1
        away_stats["PlayedPred"] += 1
    if hg > ag:
        home_stats["W"] += 1
        away_stats["L"] += 1
        home_stats["Pts"] += 3
    elif ag > hg:
        away_stats["W"] += 1
        home_stats["L"] += 1
        away_stats["Pts"] += 3
    else:
        home_stats["D"] += 1
        away_stats["D"] += 1
        home_stats["Pts"] += 1
        away_stats["Pts"] += 1


def _is_unknown_team(name):
    """True for empty / placeholder / seed / TBD labels that are not real clubs."""
    text = str(name or "").strip()
    if not text:
        return True
    lower = text.lower()
    if lower in {"tbd", "draw", "tie", "unknown", "none", NO_PREDICTION.lower()}:
        return True
    if lower.startswith("seed "):
        return True
    tokens = (
        "group ",
        "winner",
        "runner",
        "third place",
        "round of",
        "quarterfinal",
        "quarter-final",
        "semifinal",
        "semi-final",
        "playoff ",
        "qualifier",
        "to be determined",
    )
    return any(token in lower for token in tokens)


def _is_known_team(name):
    return not _is_unknown_team(name)


def _is_no_prediction(name):
    text = str(name or "").strip()
    return (not text) or text.upper() == NO_PREDICTION or text.lower() in {"draw", "tie"}


def _lookup_match_probs(predictions_index, home_team, away_team):
    """Return (prob_home, prob_draw, prob_away) from index, trying both orientations."""
    if not predictions_index:
        return 0.0, 0.0, 0.0
    hm = str(home_team or "").strip().lower()
    aw = str(away_team or "").strip().lower()
    entry = predictions_index.get((hm, aw), {}) or {}
    ph = _safe_float(entry.get("prob_home"), 0)
    pd_ = _safe_float(entry.get("prob_draw"), 0)
    pa = _safe_float(entry.get("prob_away"), 0)
    if ph == 0 and pa == 0 and pd_ == 0:
        rev = predictions_index.get((aw, hm), {}) or {}
        ph = _safe_float(rev.get("prob_away"), 0)
        pd_ = _safe_float(rev.get("prob_draw"), 0)
        pa = _safe_float(rev.get("prob_home"), 0)
    return ph, pd_, pa


def _lookup_match_probs_ha(predictions_index, home_team, away_team):
    """Return (prob_home, prob_away) — draw mass ignored for knockout winner picks."""
    ph, _pd, pa = _lookup_match_probs(predictions_index, home_team, away_team)
    return ph, pa


def _pick_projected_winner(home_team, away_team, predictions_index=None):
    """Pick knockout winner from odds only. Never invent 'Draw' as a team.

    - Either side TBD / placeholder → ``NONE``
    - Both known with home/away odds → higher win probability
    - Both known but no usable odds → ``NONE``
    """
    home = str(home_team or "").strip()
    away = str(away_team or "").strip()
    if not _is_known_team(home) or not _is_known_team(away):
        return NO_PREDICTION
    ph, pa = _lookup_match_probs_ha(predictions_index, home, away)
    if ph + pa <= 0:
        return NO_PREDICTION
    return home if ph >= pa else away


def _fallback_winner_when_prediction_missing(home_team, away_team):
    """Legacy name — always prefer NONE over inventing Draw/known-team bias."""
    return _pick_projected_winner(home_team, away_team, predictions_index=None)


def _fallback_predicted_score(home_team, away_team, predictions_index=None):
    """Scoreline for table sims when goals missing: odds winner 1-0, else skip (None)."""
    winner = _pick_projected_winner(home_team, away_team, predictions_index)
    home = str(home_team or "").strip()
    away = str(away_team or "").strip()
    if winner == home:
        return 1, 0
    if winner == away:
        return 0, 1
    return None


def _predicted_score(row, predictions_index=None):
    """Return (hg, ag) for projecting a pending fixture, or None if undecidable."""
    home = str(row.get("home_team", "")).strip()
    away = str(row.get("away_team", "")).strip()
    schedule_only = str(row.get("schedule_only", "")).strip().lower() in {"1", "true", "yes"}
    predicted = str(row.get("predicted_result", "")).strip().upper()
    hg = _numeric_int(row.get("pred_home_goals"), None)
    ag = _numeric_int(row.get("pred_away_goals"), None)

    # Prefer odds over a hard H/D/A label — cups should not invent Draw winners.
    ph, pa = _lookup_match_probs_ha(predictions_index, home, away)
    if ph + pa > 0:
        if ph >= pa:
            if hg is not None and ag is not None and hg > ag:
                return int(hg), int(ag)
            return 1, 0
        if hg is not None and ag is not None and ag > hg:
            return int(hg), int(ag)
        return 0, 1

    if schedule_only or predicted not in {"H", "A"} or hg is None or ag is None:
        return _fallback_predicted_score(home, away, predictions_index)

    if predicted == "H":
        if hg <= ag:
            hg = ag + 1
        return hg, ag
    if predicted == "A":
        if ag <= hg:
            ag = hg + 1
        return hg, ag
    return _fallback_predicted_score(home, away, predictions_index)


def _sample_fixture_outcome(row, rng, predictions_index=None):
    """Sample H/D/A from model probs for league-phase Monte Carlo."""
    home = str(row.get("home_team", "")).strip()
    away = str(row.get("away_team", "")).strip()
    ph, pd_, pa = _lookup_match_probs(predictions_index, home, away)
    # Fall back to row-stored probs when index miss.
    if ph + pd_ + pa <= 0:
        ph = _safe_float(row.get("prob_home"), 0)
        pd_ = _safe_float(row.get("prob_draw"), 0)
        pa = _safe_float(row.get("prob_away"), 0)
    total = ph + pd_ + pa
    if total <= 0:
        # Schedule-only ESPN seeds have no model odds. Use a mild home-leaning
        # prior so Monte Carlo still runs (skipping every fixture left sim_runs=0
        # / 100% sticky live tables for CL/EL/ECL).
        score = _predicted_score(row, predictions_index)
        if score is not None:
            return score
        ph, pd_, pa = 0.46, 0.26, 0.28
        total = 1.0
    pick = rng.random() * total
    if pick < ph:
        result = "H"
    elif pick < ph + pd_:
        result = "D"
    else:
        result = "A"
    base_hg = _numeric_int(row.get("pred_home_goals"), 1)
    base_ag = _numeric_int(row.get("pred_away_goals"), 1)
    hg = max(0, base_hg if base_hg is not None else 1)
    ag = max(0, base_ag if base_ag is not None else 1)
    if result == "H" and hg <= ag:
        hg = ag + 1
    elif result == "A" and ag <= hg:
        ag = hg + 1
    elif result == "D":
        ag = hg
    return hg, ag


def _is_league_phase_cup_row(row, competition_name):
    """True when a fixture belongs on the phase table (not knockout)."""
    rnd = str(row.get("round") or row.get("matchday") or row.get("stage") or "").strip().lower()
    if not rnd:
        # UEFA / Leagues Cup upcoming rows often omit round — treat as phase.
        return competition_name in CUP_TABLE_COMPETITIONS
    knockout_tokens = (
        "knockout", "playoff", "play-off", "round of", "quarter", "semi",
        "final", "third place", "3rd place",
    )
    if any(tok in rnd for tok in knockout_tokens):
        # "Final" in "group stage final matchday" is rare; allow league/phase labels.
        if "league phase" in rnd or "group" in rnd or "phase one" in rnd or "matchday" in rnd:
            return True
        return False
    return True


def _rank_table_items(table):
    return sorted(
        table.items(),
        key=lambda item: (-item[1]["Pts"], -item[1]["GD"], -item[1]["GF"], item[0]),
    )


def _probability_columns_from_counts(pos_counts, team, total_positions, runs):
    runs = max(1, int(runs))
    counts = pos_counts.get(team) or {}
    position_odds = {
        str(pos): round((counts.get(pos, 0) / runs) * 100.0, 2)
        for pos in range(1, total_positions + 1)
    }
    win_league_pct = round((counts.get(1, 0) / runs) * 100.0, 2)
    top4_pct = round(sum(counts.get(pos, 0) for pos in range(1, min(4, total_positions) + 1)) / runs * 100.0, 2)
    bottom_cutoff = max(1, total_positions - 2)
    bottom3_pct = round(
        sum(counts.get(pos, 0) for pos in range(bottom_cutoff, total_positions + 1)) / runs * 100.0,
        2,
    )
    if counts:
        most_pos = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]
        most_pct = round((counts[most_pos] / runs) * 100.0, 2)
    else:
        most_pos, most_pct = 1, 0.0
    return {
        "win_league_pct": win_league_pct,
        "top4_pct": top4_pct,
        "bottom3_pct": bottom3_pct,
        "most_likely_position": most_pos,
        "most_likely_position_pct": most_pct,
        "position_odds_json": json.dumps(position_odds, separators=(",", ":"), sort_keys=True),
        "sim_runs": int(runs),
    }


def _rank_cup_table_rows(competition, table, pos_counts=None, runs=0):
    """Turn an in-memory W/D/L table into projected_cup_tables rows."""
    ranked = _rank_table_items(table)
    total_positions = len(ranked)
    out_rows = []
    for position, (team, stats) in enumerate(ranked, start=1):
        if pos_counts and runs > 0:
            odds = _probability_columns_from_counts(pos_counts, team, total_positions, runs)
        else:
            # Real/live table only — no invented 100% odds.
            odds = {
                "win_league_pct": None,
                "top4_pct": None,
                "bottom3_pct": None,
                "most_likely_position": position,
                "most_likely_position_pct": None,
                "position_odds_json": json.dumps({}, separators=(",", ":")),
                "sim_runs": 0,
            }
        out_rows.append(
            {
                "competition": competition,
                "position": position,
                "team": team,
                "P": stats["P"],
                "W": stats["W"],
                "D": stats["D"],
                "L": stats["L"],
                "GF": stats["GF"],
                "GA": stats["GA"],
                "GD": stats["GD"],
                "Pts": stats["Pts"],
                "PlayedReal": stats.get("PlayedReal", 0),
                "PlayedPred": stats.get("PlayedPred", 0),
                **odds,
            }
        )
    return out_rows


def _clone_table(table):
    return {team: dict(stats) for team, stats in table.items()}


def _build_predictions_index(upcoming_df):
    predictions_index = {}
    if upcoming_df is None or upcoming_df.empty:
        return predictions_index
    for _, row in upcoming_df.iterrows():
        hm = str(row.get("home_team", "")).strip().lower()
        aw = str(row.get("away_team", "")).strip().lower()
        if hm and aw:
            predictions_index[(hm, aw)] = {
                "prob_home": _safe_float(row.get("prob_home"), 0),
                "prob_draw": _safe_float(row.get("prob_draw"), 0),
                "prob_away": _safe_float(row.get("prob_away"), 0),
            }
    return predictions_index


def _progress(msg: str) -> None:
    """Flushed terminal progress so long cup sims show activity immediately."""
    print(msg, flush=True)


def _build_projected_cup_tables(completed_df, upcoming_df):
    """Build projected phase tables with Monte Carlo position odds + real base.

    Completed (real) league-phase games seed the live table. Remaining upcoming
    fixtures are sampled ``CUP_TABLE_SIMULATION_RUNS`` times so position odds
    reflect simulation mass — not a single deterministic projection.

    Only in-season games are used (UEFA: September+ of the active season).
    """
    frames = []
    # Drop prior-season history before seeding tables.
    completed_df = _filter_frame_to_cup_table_season(completed_df)
    upcoming_df = _filter_frame_to_cup_table_season(upcoming_df) if upcoming_df is not None else upcoming_df
    if completed_df is not None and not completed_df.empty:
        completed = completed_df.copy()
        completed["__is_real"] = True
        frames.append(completed)
    if upcoming_df is not None and not upcoming_df.empty:
        pending = upcoming_df.copy()
        pending["__is_real"] = False
        frames.append(pending)
    if not frames:
        return _empty_frame(TABLE_COLUMNS), _empty_frame(TABLE_COLUMNS)

    combined = pd.concat(frames, ignore_index=True)
    combined["competition"] = combined["competition"].astype(str).str.strip()
    combined = combined[combined["competition"].isin(CUP_TABLE_COMPETITIONS)]
    if combined.empty:
        return _empty_frame(TABLE_COLUMNS), _empty_frame(TABLE_COLUMNS)

    predictions_index = _build_predictions_index(upcoming_df)
    rng = np.random.default_rng(20260612)
    projected_rows = []
    real_rows = []

    table_comps = sorted(str(c).strip() for c in combined["competition"].unique())
    total_tables = len(table_comps)
    _progress(f"[cups] START cup table projections — {total_tables} competitions")
    finished_tables = 0

    for competition, comp_frame in combined.groupby("competition", dropna=False):
        competition_name = str(competition).strip()
        finished_tables += 1
        _progress(
            f"[cups] START table {competition_name} "
            f"({finished_tables}/{total_tables})"
        )
        if competition_name == LEAGUES_CUP_COMPETITION:
            max_phase_matches = LEAGUES_CUP_PHASE_MATCHES
        else:
            max_phase_matches = UEFA_LEAGUE_PHASE_MATCHES.get(competition_name, 8)

        phase = comp_frame[comp_frame.apply(
            lambda r: _is_league_phase_cup_row(r, competition_name), axis=1
        )].copy()
        if phase.empty:
            _progress(
                f"[cups] DONE  table {competition_name} "
                f"({finished_tables}/{total_tables}) — no phase rows"
            )
            continue

        phase["__date_sort"] = pd.to_datetime(phase.get("match_date"), errors="coerce")
        phase = phase.sort_values(
            ["__date_sort", "__is_real", "home_team", "away_team"],
            ascending=[True, False, True, True],
            na_position="last",
        )

        # --- Real / live table from completed phase games only ---
        real_table = {}
        played_real = {}
        for _, row in phase.iterrows():
            if not bool(row.get("__is_real")):
                continue
            home = str(row.get("home_team", "")).strip()
            away = str(row.get("away_team", "")).strip()
            if not _is_known_team(home) or not _is_known_team(away):
                continue
            if played_real.get(home, 0) >= max_phase_matches or played_real.get(away, 0) >= max_phase_matches:
                continue
            hg = _numeric_int(row.get("actual_home_goals"), None)
            ag = _numeric_int(row.get("actual_away_goals"), None)
            if hg is None or ag is None:
                continue
            _apply_result(real_table, home, away, hg, ag, is_real=True)
            played_real[home] = played_real.get(home, 0) + 1
            played_real[away] = played_real.get(away, 0) + 1

        # Pending fixtures for Monte Carlo (known teams, under phase-match cap).
        pending_rows = []
        played_base = dict(played_real)
        for _, row in phase.iterrows():
            if bool(row.get("__is_real")):
                continue
            home = str(row.get("home_team", "")).strip()
            away = str(row.get("away_team", "")).strip()
            if not _is_known_team(home) or not _is_known_team(away):
                continue
            if played_base.get(home, 0) >= max_phase_matches or played_base.get(away, 0) >= max_phase_matches:
                continue
            pending_rows.append(row)
            played_base[home] = played_base.get(home, 0) + 1
            played_base[away] = played_base.get(away, 0) + 1

        def _emit_sides(table, pos_counts, runs, into_projected=True):
            if competition_name == LEAGUES_CUP_COMPETITION:
                mls_table = {
                    team: stats for team, stats in table.items()
                    if _leagues_cup_table_side(team) == "MLS"
                }
                liga_table = {
                    team: stats for team, stats in table.items()
                    if _leagues_cup_table_side(team) == "Liga MX"
                }
                chunks = []
                for side_table in (mls_table, liga_table):
                    if not side_table:
                        continue
                    side_counts = {
                        t: (pos_counts or {}).get(t, {}) for t in side_table
                    } if pos_counts else None
                    chunks.extend(_rank_cup_table_rows(
                        competition_name, side_table, side_counts, runs,
                    ))
                return chunks
            return _rank_cup_table_rows(competition_name, table, pos_counts, runs)

        real_rows.extend(_emit_sides(real_table, None, 0, into_projected=False))

        runs = CUP_TABLE_SIMULATION_RUNS if pending_rows else 0
        if runs <= 0:
            # No remaining fixtures — projected == real live table, odds unknown.
            projected_rows.extend(_emit_sides(real_table, None, 0))
            _progress(
                f"[cups] DONE  table {competition_name} "
                f"({finished_tables}/{total_tables}) — live table only (0 sims)"
            )
            continue

        print(
            f"[cups] running {runs} table sims for {competition_name} "
            f"({len(pending_rows)} pending, {len(real_table)} teams)",
            flush=True,
        )
        pos_counts = defaultdict(lambda: defaultdict(int))
        stat_sums = defaultdict(lambda: defaultdict(float))
        for _ in range(runs):
            sim_table = _clone_table(real_table)
            played = dict(played_real)
            for row in pending_rows:
                home = str(row.get("home_team", "")).strip()
                away = str(row.get("away_team", "")).strip()
                if played.get(home, 0) >= max_phase_matches or played.get(away, 0) >= max_phase_matches:
                    continue
                score = _sample_fixture_outcome(row, rng, predictions_index)
                if score is None:
                    continue
                hg, ag = score
                _apply_result(sim_table, home, away, hg, ag, is_real=False)
                played[home] = played.get(home, 0) + 1
                played[away] = played.get(away, 0) + 1

            if competition_name == LEAGUES_CUP_COMPETITION:
                for side in ("MLS", "Liga MX"):
                    side_table = {
                        t: s for t, s in sim_table.items()
                        if _leagues_cup_table_side(t) == side
                    }
                    for pos, (team, stats) in enumerate(_rank_table_items(side_table), start=1):
                        pos_counts[team][pos] += 1
                        for key, value in stats.items():
                            try:
                                stat_sums[team][key] += float(value)
                            except (TypeError, ValueError):
                                pass
            else:
                for pos, (team, stats) in enumerate(_rank_table_items(sim_table), start=1):
                    pos_counts[team][pos] += 1
                    for key, value in stats.items():
                        try:
                            stat_sums[team][key] += float(value)
                        except (TypeError, ValueError):
                            pass

        # Averaged projected stats + sim position odds.
        avg_table = {}
        for team, sums in stat_sums.items():
            avg_table[team] = {
                key: int(round(val / runs)) for key, val in sums.items()
            }
            # Preserve real played counts from base when present.
            if team in real_table:
                avg_table[team]["PlayedReal"] = real_table[team].get("PlayedReal", 0)
        projected_rows.extend(_emit_sides(avg_table, pos_counts, runs))
        _progress(
            f"[cups] DONE  table {competition_name} "
            f"({finished_tables}/{total_tables}) — {runs} sims"
        )

    _progress(f"[cups] DONE with processing {finished_tables}/{total_tables} cup table competitions")
    return (
        _ensure_columns(pd.DataFrame(projected_rows), TABLE_COLUMNS),
        _ensure_columns(pd.DataFrame(real_rows), TABLE_COLUMNS),
    )


def _winner_label(row, predictions_index=None):
    """Resolved match winner for bracket display.

    Completed: actual H/A team (completed D → NONE — no advancing side).
    Upcoming: odds-based pick; otherwise NONE (never invent a team named Draw).
    """
    actual = str(row.get("actual_result", "")).strip().upper()
    home = str(row.get("home_team", "")).strip()
    away = str(row.get("away_team", "")).strip()
    if actual == "H":
        return home or NO_PREDICTION
    if actual == "A":
        return away or NO_PREDICTION
    if actual == "D":
        return NO_PREDICTION
    return _pick_projected_winner(home, away, predictions_index)


def _match_payload(row, status, predictions_index=None):
    actual_hg = pd.to_numeric(row.get("actual_home_goals"), errors="coerce")
    actual_ag = pd.to_numeric(row.get("actual_away_goals"), errors="coerce")
    pred_hg = pd.to_numeric(row.get("pred_home_goals"), errors="coerce")
    pred_ag = pd.to_numeric(row.get("pred_away_goals"), errors="coerce")
    winner = _winner_label(row, predictions_index)
    predicted = str(row.get("predicted_result", "")).strip().upper()
    ph, pa = _lookup_match_probs_ha(predictions_index, row.get("home_team"), row.get("away_team"))
    if ph + pa <= 0:
        ph = _safe_float(row.get("prob_home"), 0)
        pa = _safe_float(row.get("prob_away"), 0)
    # Expose NONE when we cannot pick a side yet (TBD or no odds).
    if _is_no_prediction(winner):
        predicted_out = NO_PREDICTION
    elif predicted in {"H", "A"} and status != "Completed":
        # Prefer odds-aligned code when available.
        home = str(row.get("home_team", "")).strip()
        away = str(row.get("away_team", "")).strip()
        if winner == home:
            predicted_out = "H"
        elif winner == away:
            predicted_out = "A"
        else:
            predicted_out = NO_PREDICTION
    elif status == "Completed" and str(row.get("actual_result", "")).strip().upper() in {"H", "D", "A"}:
        predicted_out = predicted if predicted in {"H", "D", "A"} else str(row.get("actual_result", "")).strip().upper()
    else:
        predicted_out = NO_PREDICTION if _is_no_prediction(winner) else predicted

    return {
        "match_date": str(row.get("match_date", "")).strip(),
        "home_team": str(row.get("home_team", "")).strip(),
        "away_team": str(row.get("away_team", "")).strip(),
        "status": status,
        "winner": winner,
        "actual_home_goals": int(actual_hg) if pd.notna(actual_hg) else None,
        "actual_away_goals": int(actual_ag) if pd.notna(actual_ag) else None,
        "pred_home_goals": int(round(float(pred_hg))) if pd.notna(pred_hg) else None,
        "pred_away_goals": int(round(float(pred_ag))) if pd.notna(pred_ag) else None,
        "predicted_result": predicted_out,
        "prob_home": round(ph, 4) if ph else _safe_float(row.get("prob_home"), None),
        "prob_draw": _safe_float(row.get("prob_draw"), None),
        "prob_away": round(pa, 4) if pa else _safe_float(row.get("prob_away"), None),
    }


def _seed_name(ranked_rows, seed):
    if seed <= len(ranked_rows):
        return str(ranked_rows[seed - 1].get("team", "")).strip() or f"Seed {seed}"
    return f"Seed {seed}"


def _uefa_match(stage, slot, home_team, away_team, winner=None, predictions_index=None):
    if winner is None:
        winner = _pick_projected_winner(home_team, away_team, predictions_index)
    return {
        "stage": stage,
        "slot": slot,
        "match_date": "",
        "home_team": home_team,
        "away_team": away_team,
        "status": "Projected",
        "winner": winner,
        "actual_home_goals": None,
        "actual_away_goals": None,
        "pred_home_goals": None,
        "pred_away_goals": None,
        "predicted_result": "",
    }


def _build_uefa_bracket_from_table(competition, table_rows, predictions_index=None):
    def position_value(row):
        pos = pd.to_numeric(row.get("position"), errors="coerce")
        return int(pos) if pd.notna(pos) else 999

    ranked_rows = sorted(
        table_rows,
        key=lambda row: (position_value(row), str(row.get("team", ""))),
    )
    playoff_pairs = [(9, 24), (10, 23), (11, 22), (12, 21), (13, 20), (14, 19), (15, 18), (16, 17)]
    playoff_matches = []
    playoff_winners = []
    for idx, (high_seed, low_seed) in enumerate(playoff_pairs, start=1):
        high_team = _seed_name(ranked_rows, high_seed)
        low_team = _seed_name(ranked_rows, low_seed)
        winner = _pick_projected_winner(high_team, low_team, predictions_index)
        playoff_winners.append(winner)
        playoff_matches.append(
            _uefa_match("First Round Playoff", idx, high_team, low_team, winner, predictions_index)
        )

    top_seed_order = [1, 8, 4, 5, 2, 7, 3, 6]
    round_of_16 = []
    round_of_16_winners = []
    for idx, seed in enumerate(top_seed_order, start=1):
        top_seed = _seed_name(ranked_rows, seed)
        playoff_winner = playoff_winners[idx - 1] if idx - 1 < len(playoff_winners) else f"Playoff Winner {idx}"
        winner = _pick_projected_winner(top_seed, playoff_winner, predictions_index)
        round_of_16_winners.append(winner)
        round_of_16.append(
            _uefa_match("Round of 16", idx, top_seed, playoff_winner, winner, predictions_index)
        )

    quarterfinals = []
    semifinalists = []
    for idx in range(0, 8, 2):
        home = round_of_16_winners[idx] if idx < len(round_of_16_winners) else f"R16 Winner {idx + 1}"
        away = round_of_16_winners[idx + 1] if idx + 1 < len(round_of_16_winners) else f"R16 Winner {idx + 2}"
        winner = _pick_projected_winner(home, away, predictions_index)
        semifinalists.append(winner)
        quarterfinals.append(
            _uefa_match("Quarterfinals", (idx // 2) + 1, home, away, winner, predictions_index)
        )

    semifinals = []
    finalists = []
    for idx in range(0, 4, 2):
        home = semifinalists[idx] if idx < len(semifinalists) else f"Quarterfinal Winner {idx + 1}"
        away = semifinalists[idx + 1] if idx + 1 < len(semifinalists) else f"Quarterfinal Winner {idx + 2}"
        winner = _pick_projected_winner(home, away, predictions_index)
        finalists.append(winner)
        semifinals.append(
            _uefa_match("Semifinals", (idx // 2) + 1, home, away, winner, predictions_index)
        )

    final_home = finalists[0] if finalists else "Semifinal Winner 1"
    final_away = finalists[1] if len(finalists) > 1 else "Semifinal Winner 2"
    final_winner = _pick_projected_winner(final_home, final_away, predictions_index)
    final = [_uefa_match("Final", 1, final_home, final_away, final_winner, predictions_index)]

    return {
        "competition": competition,
        "format": "uefa_league_phase_knockout",
        "league_phase_matches": UEFA_LEAGUE_PHASE_MATCHES.get(competition, 8),
        "qualification": {
            "round_of_16": "Positions 1-8",
            "first_round_playoff": "Positions 9-24",
        },
        "rounds": [
            {"name": "First Round Playoff", "matches": playoff_matches},
            {"name": "Round of 16", "matches": round_of_16},
            {"name": "Quarterfinals", "matches": quarterfinals},
            {"name": "Semifinals", "matches": semifinals},
            {"name": "Final", "matches": final},
        ],
    }


def _leagues_cup_table_side(team_name):
    """Classify a team into MLS or Liga MX for Leagues Cup seeding."""
    try:
        website_dir = os.path.join(BASE_DIR, "Website")
        if website_dir not in sys.path:
            sys.path.insert(0, website_dir)
        from competition_rules import leagues_cup_table_side
        return leagues_cup_table_side(team_name)
    except Exception:
        return None


def _build_leagues_cup_bracket_from_table(table_rows, predictions_index):
    """Seed Leagues Cup knockout from dual Phase One tables (top 4 each)."""
    def _ranked(side):
        rows = [
            r for r in (table_rows or [])
            if _leagues_cup_table_side(str(r.get("team", "")).strip()) == side
        ]
        rows.sort(key=lambda r: (
            int(pd.to_numeric(r.get("position"), errors="coerce") or 999),
            -int(pd.to_numeric(r.get("Pts"), errors="coerce") or 0),
            str(r.get("team", "")),
        ))
        return [str(r.get("team", "")).strip() for r in rows if str(r.get("team", "")).strip()]

    mls = _ranked("MLS")
    liga = _ranked("Liga MX")
    while len(mls) < 4:
        mls.append(f"MLS Seed {len(mls) + 1}")
    while len(liga) < 4:
        liga.append(f"Liga MX Seed {len(liga) + 1}")

    qf_pairs = [
        (mls[0], liga[3]),
        (mls[1], liga[2]),
        (mls[2], liga[1]),
        (mls[3], liga[0]),
    ]
    quarterfinals = []
    qf_winners = []
    for idx, (home, away) in enumerate(qf_pairs, start=1):
        winner = _pick_projected_winner(home, away, predictions_index)
        qf_winners.append(winner)
        quarterfinals.append(_uefa_match("Quarter-finals", idx, home, away, winner, predictions_index))

    semifinals = []
    sf_winners = []
    for idx in range(0, 4, 2):
        home = qf_winners[idx]
        away = qf_winners[idx + 1]
        winner = _pick_projected_winner(home, away, predictions_index)
        sf_winners.append(winner)
        semifinals.append(_uefa_match("Semi-finals", (idx // 2) + 1, home, away, winner, predictions_index))

    final_home = sf_winners[0] if sf_winners else "Semifinal Winner 1"
    final_away = sf_winners[1] if len(sf_winners) > 1 else "Semifinal Winner 2"
    final_winner = _pick_projected_winner(final_home, final_away, predictions_index)
    final = [_uefa_match("Final", 1, final_home, final_away, final_winner, predictions_index)]

    return {
        "competition": "North America/Leagues Cup",
        "format": "leagues_cup_dual_knockout",
        "rounds": [
            {"name": "Quarter-finals", "matches": quarterfinals},
            {"name": "Semi-finals", "matches": semifinals},
            {"name": "Final", "matches": final},
        ],
    }


def _attach_cup_simulation(competition_name, bracket, predictions_index):
    sim_info = _simulate_cup_tournament(
        competition_name, bracket.get("rounds", []), predictions_index,
    )
    if sim_info.get("simulations_run", 0) <= 0:
        return bracket
    bracket["champion"] = sim_info["champion"]
    bracket["simulations_run"] = sim_info["simulations_run"]
    bracket["winner_probabilities"] = sim_info["winner_probabilities"]
    bracket["sim_index"] = sim_info["sim_index"]
    if sim_info.get("elimination_round_probabilities"):
        bracket["elimination_round_probabilities"] = sim_info["elimination_round_probabilities"]
    if sim_info.get("round_reach_probabilities"):
        bracket["round_reach_probabilities"] = sim_info["round_reach_probabilities"]
    return bracket


def _build_domestic_cup_rounds(comp_frame, predictions_index=None):
    """Build the ``rounds`` list consumers read from projected_cup_brackets.json.

    Knockout APIs and cup simulations only look at ``bracket["rounds"]``.
    Upcoming fixtures come first when present; otherwise recent results.
    """
    rounds = []
    completed_rows = comp_frame[comp_frame["__status"] == "Completed"].sort_values(
        ["match_date", "home_team", "away_team"],
        ascending=[False, True, True],
        na_position="last",
    ).head(DOMESTIC_BRACKET_MATCH_LIMIT)
    upcoming_rows = comp_frame[comp_frame["__status"] == "Upcoming"].sort_values(
        ["match_date", "home_team", "away_team"],
        na_position="last",
    ).head(DOMESTIC_BRACKET_MATCH_LIMIT)
    if upcoming_rows.empty and completed_rows.empty:
        return rounds
    if upcoming_rows.empty:
        rounds.append(
            {
                "name": "Recent Cup Results",
                "matches": [
                    _match_payload(row, "Completed", predictions_index)
                    for _, row in completed_rows.iterrows()
                ],
            }
        )
        return rounds
    rounds.append(
        {
            "name": "Upcoming Cup Fixtures",
            "matches": [
                _match_payload(row, "Upcoming", predictions_index)
                for _, row in upcoming_rows.iterrows()
            ],
        }
    )
    if not completed_rows.empty:
        rounds.append(
            {
                "name": "Recent Cup Results",
                "matches": [
                    _match_payload(row, "Completed", predictions_index)
                    for _, row in completed_rows.iterrows()
                ],
            }
        )
    return rounds


def _build_domestic_cup_bracket_with_draws(competition_name, comp_frame, predictions_index):
    """Build a domestic cup bracket with a consumer-facing ``rounds`` list.

    Also keeps ``real_knockout`` / ``projected_knockout`` / ``upcoming_fixtures``
    as supplemental metadata. Winner-odds simulation still requires ``rounds``.
    """
    rules = CUP_FORMAT_RULES.get(competition_name, {})

    completed = (
        comp_frame[comp_frame["__status"] == "Completed"]
        if "__status" in comp_frame.columns
        else pd.DataFrame()
    )
    upcoming = (
        comp_frame[comp_frame["__status"] == "Upcoming"]
        if "__status" in comp_frame.columns
        else pd.DataFrame()
    )

    upcoming_matches = []
    if not upcoming.empty:
        for _, row in upcoming.iterrows():
            upcoming_matches.append(_match_payload(row, "Upcoming", predictions_index))

    real_rounds = []
    if not completed.empty:
        real_rounds.append({
            "name": "Recent Results",
            "matches": [
                _match_payload(row, "Completed", predictions_index)
                for _, row in completed.iterrows()
            ],
        })

    num_completed = len(completed) if not completed.empty else 0
    num_upcoming = len(upcoming) if not upcoming.empty else 0

    projected_rounds = []
    if rules and num_upcoming > 0:
        projected_rounds.append({
            "name": "Next Rounds (Projected)",
            "matches": [
                _create_tbd_matchup(f"Round {i}", i, draw_rules=rules)
                for i in range(1, min(4, num_upcoming + 2))
            ],
        })

    rounds = _build_domestic_cup_rounds(comp_frame, predictions_index)

    return _attach_cup_simulation(competition_name, {
        "competition": competition_name,
        "format": "domestic_knockout_with_projections",
        "format_rules": rules,
        "rounds": rounds,
        "real_knockout": real_rounds,
        "projected_knockout": projected_rounds,
        "upcoming_fixtures": upcoming_matches,
        "match_count": {
            "completed": num_completed,
            "upcoming": num_upcoming,
        },
    }, predictions_index)


def _build_uefa_bracket_with_draws(competition_name, table_rows, predictions_index):
    """Build UEFA bracket with draw-aware simulation.
    
    - Generates bracket from league phase standings (positions 1-8 auto-qualify, 9-24 play playoff)
    - Simulates playoff round with seeding constraints
    - Calculates probabilities for each possible opponent
    """
    bracket = _build_uefa_bracket_from_table(competition_name, table_rows, predictions_index)
    
    # Run simulation with draw constraints
    sim_info = _simulate_cup_tournament(
        competition_name, bracket.get("rounds", []), predictions_index,
    )
    
    if sim_info["simulations_run"] > 0:
        bracket["champion"] = sim_info["champion"]
        bracket["simulations_run"] = sim_info["simulations_run"]
        bracket["winner_probabilities"] = sim_info["winner_probabilities"]
        bracket["sim_index"] = sim_info["sim_index"]
        if sim_info.get("elimination_round_probabilities"):
            bracket["elimination_round_probabilities"] = sim_info["elimination_round_probabilities"]
        if sim_info.get("round_reach_probabilities"):
            bracket["round_reach_probabilities"] = sim_info["round_reach_probabilities"]
        
        # Add draw constraint info for playoff round
        if competition_name in ("Europe/Champions League", "Europe/Champions League"):
            bracket["draw_constraints"] = {
                "round": "First Round Playoff",
                "description": "Seeds 9-16 paired with seeds 17-24. Lower seeds can face seeds 23-24.",
                "seeding_rules": {
                    "top_8": "Auto-qualify to Round of 16",
                    "9_to_24": "Single-leg playoff (lower seed hosts higher seed in first leg equivalent)",
                }
            }
    
    return bracket


_MATCH_FIELDS = {"home_team", "away_team", "winner", "prob_home", "prob_draw", "prob_away"}


def _normalize_team_key(name):
    """Normalize a team name for comparison."""
    return str(name or "").strip().lower()


def _get_team_squad_value(team_name, squad_value_data=None):
    """Retrieve squad value for a team from available data sources."""
    if squad_value_data is None:
        squad_value_data = {}
    
    normalized = _normalize_team_key(team_name)
    
    # Check direct mapping
    if team_name in squad_value_data:
        return squad_value_data[team_name]
    if normalized in squad_value_data:
        return squad_value_data[normalized]
    
    return None


def _get_team_league_position(team_name, standings_data=None):
    """Retrieve team league position for a team from standings data."""
    if standings_data is None:
        standings_data = {}
    
    normalized = _normalize_team_key(team_name)
    
    # Search through standings for this team
    for competition, table in standings_data.items():
        if isinstance(table, list):
            for entry in table:
                if _normalize_team_key(entry.get("team", "")) == normalized:
                    return {
                        "position": entry.get("position"),
                        "competition": competition,
                        "points": entry.get("Pts"),
                        "played": entry.get("P"),
                    }
    
    return None


def _enrich_team_data(team_name, squad_values=None, standings=None):
    """Enrich team data with squad value, league position, and other metrics."""
    squad_value = _get_team_squad_value(team_name, squad_values)
    position_info = _get_team_league_position(team_name, standings)
    
    return {
        "team_name": team_name,
        "squad_value_millions": squad_value,
        "league_position": position_info.get("position") if position_info else None,
        "league_competition": position_info.get("competition") if position_info else None,
        "league_points": position_info.get("points") if position_info else None,
    }


def _detect_two_leg_tie(home_team, away_team, matches_for_pair):
    """Detect if a matchup has 2 legs (home and away matches)."""
    if len(matches_for_pair) < 2:
        return False
    
    # Check if we have both home and away legs with same teams
    home_leg = None
    away_leg = None
    
    for match in matches_for_pair:
        h = _normalize_team_key(match.get("home_team", ""))
        a = _normalize_team_key(match.get("away_team", ""))
        ht = _normalize_team_key(home_team)
        at = _normalize_team_key(away_team)
        
        if h == ht and a == at:
            home_leg = match
        elif h == at and a == ht:
            away_leg = match
    
    return home_leg is not None and away_leg is not None


def _create_tbd_matchup(round_name, slot, possible_opponents=None, draw_rules=None):
    """Create a TBD (To Be Determined) matchup placeholder."""
    matchup = {
        "slot": slot,
        "round": round_name,
        "status": "TBD",
        "home_team": "TBD",
        "away_team": "TBD",
        "winner": NO_PREDICTION,
        "predicted_result": NO_PREDICTION,
        "is_placeholder": True,
    }
    
    if possible_opponents:
        matchup["possible_opponents"] = possible_opponents
    
    if draw_rules:
        matchup["draw_info"] = draw_rules
    
    return matchup


def _create_conditional_matchup(team_or_source, opponent_or_source, round_name, slot, draw_constraints=None):
    """Create a conditional matchup like 'Winner of Match X vs Team Y'."""
    return {
        "slot": slot,
        "round": round_name,
        "status": "Conditional",
        "home_team": team_or_source,
        "away_team": opponent_or_source,
        "is_conditional": True,
        "draw_constraints": draw_constraints or {},
    }


def _generate_possible_opponents_for_slot(competition_name, round_name, slot, bracket_data, predictions_index):
    """Generate possible opponents for a TBD matchup slot based on draw rules.
    
    For example, in Champions League playoff, seed 9 can only face seed 23 or 24.
    Returns list of (opponent, probability) tuples.
    """
    possible_opponents = []
    
    # Get draw rules for this competition
    rules = CUP_FORMAT_RULES.get(competition_name, {})
    
    if competition_name in ("Europe/Champions League", "Europe/Champions League"):
        # Champions League playoff seeding constraints
        if round_name == "First Round Playoff":
            seed_map = {
                9: [23, 24], 10: [23, 24], 11: [22, 24], 12: [22, 24],
                13: [21, 24], 14: [21, 24], 15: [20, 24], 16: [20, 24],
                17: [19, 24], 18: [19, 24], 19: [18, 24], 20: [17, 24],
                21: [16, 24], 22: [15, 24], 23: [9, 10, 11, 12, 13, 14, 15, 16],
                24: [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22],
            }
            # Extract seed number from slot if available
            slot_seed = slot + 8  # Assuming slot 1-16 maps to seeds 9-24
            candidates = seed_map.get(slot_seed, [])
            for seed in candidates:
                possible_opponents.append({
                    "name": f"Seed {seed}",
                    "probability": 1.0 / len(candidates) if candidates else 0.0,
                })
    else:
        # For most domestic cups, draw is fully randomized among remaining teams
        # Just return "TBD - to be determined in draw"
        possible_opponents.append({
            "name": "TBD - to be determined",
            "probability": 1.0,
        })
    
    return possible_opponents


def _is_placeholder_team(name):
    text = str(name or "").strip().lower()
    if not text:
        return True
    return any(token in text for token in (
        "winner", "seed", "tbd", "playoff", "quarterfinal", "semifinal", "r16",
    ))


def _apply_round_winners_to_next(sim_templates, rnd_name, winners):
    feeds = CUP_KNOCKOUT_FEEDS.get(rnd_name)
    if not feeds:
        return
    next_rnd = feeds.get("next_round")
    if not next_rnd or next_rnd not in sim_templates:
        return
    next_matches = sim_templates[next_rnd]
    for w in winners:
        target_slot = feeds["feeds_to"](w.get("slot", 1))
        if target_slot is None or target_slot < 1 or target_slot > len(next_matches):
            continue
        tmpl = next_matches[target_slot - 1]
        winner = str(w.get("winner", "")).strip()
        if not winner or _is_placeholder_team(winner):
            continue
        home = str(tmpl.get("home_team", "")).strip()
        away = str(tmpl.get("away_team", "")).strip()
        if _is_placeholder_team(home) or home.lower() == winner.lower():
            tmpl["home_team"] = winner
        elif _is_placeholder_team(away) or away.lower() == winner.lower():
            tmpl["away_team"] = winner
        elif w.get("slot", 1) % 2 == 1:
            tmpl["home_team"] = winner
        else:
            tmpl["away_team"] = winner


def _simulate_cup_tournament(competition_name, rounds_data, predictions_index, num_sims=CUP_SIMULATION_RUNS):
    """Run Monte-Carlo tournament simulations for a cup competition.

    *rounds_data* is the list of round dicts from the bracket JSON
    (e.g. ``[{"name": "Round of 16", "matches": [...]}, ...]``).
    *predictions_index* maps ``(home_team, away_team) → {prob_home, prob_draw, prob_away}``.

    Returns a dict with ``champion``, ``simulations_run``, ``winner_probabilities``,
    and ``sim_index`` (which simulation's result to use as the display bracket).
    """
    if not rounds_data:
        return {"champion": None, "simulations_run": 0, "winner_probabilities": {}}

    # Collect round names and match templates
    round_names = [r["name"] for r in rounds_data if r.get("name") in CUP_KNOCKOUT_FEEDS]
    if len(round_names) < 2:
        return {"champion": None, "simulations_run": 0, "winner_probabilities": {}}

    # Build slot → match template for each round (original winner as fallback)
    round_templates = {}
    for r in rounds_data:
        if r["name"] not in CUP_KNOCKOUT_FEEDS:
            continue
        matches = sorted(r.get("matches", []), key=lambda m: m.get("slot", 0) or 0)
        round_templates[r["name"]] = [dict(m) for m in matches]

    champion_counts = defaultdict(int)
    per_sim_champions = []
    elimination_counts = defaultdict(lambda: defaultdict(int))
    round_reach_counts = {rnd: defaultdict(int) for rnd in round_names}

    rng = np.random.default_rng(20260611)

    for _sim_num in range(num_sims):
        sim_templates = {rnd: [dict(m) for m in matches] for rnd, matches in round_templates.items()}
        sim_participants = set()

        for rnd_name in round_names:
            templates = sim_templates.get(rnd_name, [])
            winners = []

            for m in templates:
                slot = m.get("slot", 1)
                hm = str(m.get("home_team", "")).strip()
                aw = str(m.get("away_team", "")).strip()
                if hm and not _is_placeholder_team(hm):
                    sim_participants.add(hm)
                    round_reach_counts[rnd_name][hm] += 1
                if aw and not _is_placeholder_team(aw):
                    sim_participants.add(aw)
                    round_reach_counts[rnd_name][aw] += 1

                ph, pa = _lookup_match_probs_ha(predictions_index, hm, aw)
                total = ph + pa
                if total > 0:
                    p_home = ph / total
                    winner = hm if rng.random() < p_home else aw
                else:
                    winner = _pick_projected_winner(hm, aw, predictions_index)

                if _is_no_prediction(winner) or _is_placeholder_team(winner):
                    # Cannot resolve this tie — leave slot unresolved; do not
                    # credit Draw/NONE as a champion or elimination.
                    winners.append({"slot": slot, "winner": NO_PREDICTION, "home_team": hm, "away_team": aw})
                    continue

                losers = [t for t in (hm, aw) if t and not _is_placeholder_team(t) and t != winner]
                for loser in losers:
                    elimination_counts[loser][rnd_name] += 1

                winners.append({"slot": slot, "winner": winner, "home_team": hm, "away_team": aw})

            _apply_round_winners_to_next(sim_templates, rnd_name, winners)

        final_winners = sim_templates.get("Final", [])
        if final_winners:
            champion = str(final_winners[0].get("winner", "")).strip()
            if champion and not _is_placeholder_team(champion) and not _is_no_prediction(champion):
                champion_counts[champion] += 1
                elimination_counts[champion]["Champion"] += 1
                round_reach_counts["Final"][champion] += 1
                per_sim_champions.append(champion)

    if not per_sim_champions:
        return {"champion": None, "simulations_run": 0, "winner_probabilities": {}}

    total = len(per_sim_champions)
    most_common = max(champion_counts, key=lambda k: (champion_counts[k], k)) if champion_counts else None
    winner_probabilities = {
        team: round(count / total, 4)
        for team, count in sorted(champion_counts.items(), key=lambda x: -x[1])
    }

    elimination_round_probabilities = {}
    for team, rounds in elimination_counts.items():
        team_total = sum(rounds.values())
        if team_total <= 0:
            continue
        elimination_round_probabilities[team] = {
            rnd: round(count / team_total, 4)
            for rnd, count in sorted(rounds.items(), key=lambda x: -x[1])
        }

    round_reach_probabilities = {
        rnd: {team: round(count / num_sims, 4) for team, count in sorted(team_map.items(), key=lambda x: -x[1])}
        for rnd, team_map in round_reach_counts.items()
        if team_map
    }

    sim_index = next((i for i, c in enumerate(per_sim_champions) if c == most_common), 0)

    return {
        "champion": most_common,
        "simulations_run": total,
        "winner_probabilities": winner_probabilities,
        "elimination_round_probabilities": elimination_round_probabilities,
        "round_reach_probabilities": round_reach_probabilities,
        "sim_index": sim_index,
    }


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _build_projected_cup_brackets(completed_df, upcoming_df, tables_df):
    payload = {
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "competitions": {},
    }
    tables_df = _ensure_columns(tables_df, TABLE_COLUMNS)
    _progress("[cups] START cup bracket projections")

    # Build prediction index from upcoming predictions for simulation use
    predictions_index = {}
    if upcoming_df is not None and not upcoming_df.empty:
        for _, row in upcoming_df.iterrows():
            hm = str(row.get("home_team", "")).strip().lower()
            aw = str(row.get("away_team", "")).strip().lower()
            if hm and aw:
                probs = {
                    "prob_home": _safe_float(row.get("prob_home"), 0),
                    "prob_draw": _safe_float(row.get("prob_draw"), 0),
                    "prob_away": _safe_float(row.get("prob_away"), 0),
                }
                predictions_index[(hm, aw)] = probs
                try:
                    comp = str(row.get("competition", "")).strip()
                    proj_cache.set_matchup_probs(
                        row.get("home_team"),
                        row.get("away_team"),
                        comp,
                        {"H": probs["prob_home"], "D": probs["prob_draw"], "A": probs["prob_away"]},
                    )
                except Exception:
                    pass

    bracket_done = 0
    if not tables_df.empty:
        for competition, comp_table in tables_df.groupby("competition", dropna=False):
            competition_name = str(competition).strip()
            if competition_name not in UEFA_TABLE_COMPETITIONS:
                continue
            _progress(f"[cups] START bracket {competition_name} (UEFA table→KO)")
            table_rows = comp_table.to_dict("records")
            bracket = _build_uefa_bracket_with_draws(competition_name, table_rows, predictions_index)
            payload["competitions"][competition_name] = bracket
            bracket_done += 1
            _progress(f"[cups] DONE  bracket {competition_name}")
    
    for competition_name in UEFA_PRIMARY_COMPETITIONS:
        if competition_name not in payload["competitions"]:
            _progress(f"[cups] START bracket {competition_name} (empty UEFA fallback)")
            bracket = _build_uefa_bracket_with_draws(competition_name, [], predictions_index)
            payload["competitions"][competition_name] = bracket
            bracket_done += 1
            _progress(f"[cups] DONE  bracket {competition_name}")

    frames = []
    if completed_df is not None and not completed_df.empty:
        completed = completed_df.copy()
        completed["__status"] = "Completed"
        frames.append(completed)
    if upcoming_df is not None and not upcoming_df.empty:
        pending = upcoming_df.copy()
        pending["__status"] = "Upcoming"
        frames.append(pending)
    if not frames:
        _progress(f"[cups] DONE with processing {bracket_done} cup bracket competitions")
        return payload

    combined = pd.concat(frames, ignore_index=True)
    combined["competition"] = combined["competition"].astype(str).str.strip()
    combined = combined[combined["competition"].isin(DOMESTIC_BRACKET_COMPETITIONS)]
    if combined.empty:
        _progress(f"[cups] DONE with processing {bracket_done} cup bracket competitions")
        return payload

    combined = combined.sort_values(["competition", "match_date", "__status", "home_team", "away_team"], na_position="last")
    table_lookup = {}
    if not tables_df.empty:
        for competition, comp_table in tables_df.groupby("competition", dropna=False):
            table_lookup[str(competition).strip()] = comp_table.to_dict("records")

    domestic_comps = sorted(str(c).strip() for c in combined["competition"].unique())
    total_domestic = len(domestic_comps)
    for idx, (competition, comp_frame) in enumerate(combined.groupby("competition", dropna=False), start=1):
        competition_name = str(competition).strip()
        _progress(
            f"[cups] START bracket {competition_name} "
            f"(domestic {idx}/{total_domestic})"
        )
        if competition_name == "North America/Leagues Cup":
            table_rows = table_lookup.get(competition_name, [])
            bracket = _build_leagues_cup_bracket_from_table(table_rows, predictions_index)
            bracket = _attach_cup_simulation(competition_name, bracket, predictions_index)
        else:
            bracket = _build_domestic_cup_bracket_with_draws(competition_name, comp_frame, predictions_index)
        payload["competitions"][competition_name] = bracket
        bracket_done += 1
        _progress(f"[cups] DONE  bracket {competition_name}")
    _progress(f"[cups] DONE with processing {bracket_done} cup bracket competitions")
    return payload


def _cup_style(competition_name: str) -> str:
    """Return knockout | table_knockout | group_knockout for routing."""
    name = str(competition_name or "").strip()
    if name in CUP_TABLE_COMPETITIONS:
        # UEFA league phase + Leagues Cup dual tables.
        if name == LEAGUES_CUP_COMPETITION:
            return "table_knockout"
        return "table_knockout"
    # Domestic cups in this module are pure knockout.
    if name in DOMESTIC_BRACKET_COMPETITIONS:
        return "knockout"
    return "knockout"


def refresh_cup_projection_artifacts(completed_df, upcoming_df):
    """Rebuild cup tables/brackets with format-aware work and optional skip.

    - ``table_knockout`` / group-style: run phase-table Monte Carlo, then brackets
    - ``knockout``: skip table sims; brackets only
    Skip gate (``BTS_CUP_SKIP_UNCHANGED=1``) is off by default.
    """
    _progress("[cups] START — refresh cup projection artifacts")
    sig = proj_cache.cup_fixture_signature(upcoming_df, completed_df)
    if proj_cache.cup_skip_unchanged_enabled():
        prev = proj_cache.get_cup_stamp()
        if prev and prev == sig and os.path.exists(PROJECTED_CUP_BRACKETS_FILE):
            _progress(f"[cups] skip projection rebuild (fixture signature unchanged; {sig[:10]}…)")
            try:
                tables = pd.read_csv(PROJECTED_CUP_TABLES_FILE) if os.path.exists(PROJECTED_CUP_TABLES_FILE) else _empty_frame(TABLE_COLUMNS)
                n_rounds = 0
                if os.path.exists(PROJECTED_CUP_BRACKETS_FILE):
                    existing = json.loads(Path(PROJECTED_CUP_BRACKETS_FILE).read_text(encoding="utf-8"))
                    n_rounds = sum(len(comp.get("rounds", [])) for comp in (existing.get("competitions") or {}).values())
                _progress("[cups] DONE — reused existing cup projections")
                return len(tables), n_rounds
            except Exception as exc:
                _progress(f"[cups] skip aborted, rebuilding ({exc})")

    # Table / group-phase cups only — pure knockout skips the table MC entirely.
    tables, real_tables = _build_projected_cup_tables(completed_df, upcoming_df)
    _progress(
        f"[cups] table sims={CUP_TABLE_SIMULATION_RUNS} for "
        f"{sorted(CUP_TABLE_COMPETITIONS)}; knockout cups skip tables"
    )
    brackets = _build_projected_cup_brackets(completed_df, upcoming_df, tables)
    _progress(f"[cups] bracket sims={CUP_SIMULATION_RUNS}")
    _write_csv(PROJECTED_CUP_TABLES_FILE, tables, TABLE_COLUMNS)
    _write_csv(REAL_CUP_TABLES_FILE, real_tables, TABLE_COLUMNS)
    save_json(PROJECTED_CUP_BRACKETS_FILE, brackets)
    try:
        proj_cache.set_cup_stamp(
            sig,
            {
                "table_sims": CUP_TABLE_SIMULATION_RUNS,
                "bracket_sims": CUP_SIMULATION_RUNS,
                "table_rows": len(tables),
            },
        )
        proj_cache.flush_matchup_probs()
    except Exception:
        pass
    n_rounds = sum(len(comp.get("rounds", [])) for comp in brackets.get("competitions", {}).values())
    _progress(
        f"[cups] DONE — refresh complete "
        f"({len(tables)} table rows, {n_rounds} bracket sections)"
    )
    return len(tables), n_rounds


def main():
    _t0 = time.monotonic()
    _progress("[cups] START — Track_Cup_Results")
    cup_df = load_predictions(CUP_PREDICTIONS_FILE)
    completed_df = _load_completed_cups()
    if cup_df is None:
        cup_df = _empty_frame(CUP_HISTORY_COLUMNS)

    shared_mapping = load_shared_mapping()
    results, mapping_updates, unresolved, seen_names = build_cup_results_index_from_espn(cup_df, shared_mapping)
    shared_mapping, mapping_added, mapping_drift = apply_mapping_updates(shared_mapping, mapping_updates)
    save_mapping(SHARED_MAPPING_FILE, shared_mapping)
    save_json(
        ESPN_CUP_NAMES_FILE,
        {
            "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "cups": seen_names,
        },
    )

    cup_updates = 0
    completed_added = 0
    removed_completed = 0
    totals_added = 0
    if cup_df is not None and not cup_df.empty:
        cup_df, cup_updates = update_frame_with_results(cup_df, results)
        completed_df, completed_added = append_completed_predictions(completed_df, cup_df)
        totals = load_accuracy_totals(ACCURACY_TOTALS_FILE)
        totals_added = update_accuracy_totals_from_frame(totals, cup_df)
        totals["updated_at_utc"] = datetime.now(UTC).replace(microsecond=0).isoformat()
        save_json(ACCURACY_TOTALS_FILE, totals)
        today_local = datetime.now().date()
        prev_thursday = today_local - timedelta(days=(today_local.weekday() - 3) % 7 + 7)
        save_completed_rows_to_past_games(cup_df, today=prev_thursday)
        cup_df, removed_completed = _drop_completed_rows(cup_df, today=prev_thursday)

    # Backfill older in-season league-phase results for table cups (UEFA Sept+,
    # Leagues Cup summer) so tables are not limited to recently predicted fixtures.
    backfill_added = 0
    try:
        backfill_df, backfill_maps, backfill_unresolved, backfill_seen = fetch_in_season_cup_table_results(
            shared_mapping
        )
        if backfill_maps:
            shared_mapping, bf_added, bf_drift = apply_mapping_updates(shared_mapping, backfill_maps)
            mapping_added += bf_added
            mapping_drift = mapping_drift or bf_drift
            save_mapping(SHARED_MAPPING_FILE, shared_mapping)
        if backfill_unresolved:
            for comp, names in backfill_unresolved.items():
                unresolved.setdefault(comp, [])
                unresolved[comp] = sorted(set(unresolved[comp]) | set(names))
        if backfill_seen:
            for comp, names in backfill_seen.items():
                existing = set(seen_names.get(comp) or [])
                seen_names[comp] = sorted(existing | set(names))
            save_json(
                ESPN_CUP_NAMES_FILE,
                {
                    "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
                    "cups": seen_names,
                },
            )
        before = len(completed_df) if completed_df is not None else 0
        completed_df = merge_completed_cup_frames(completed_df, backfill_df)
        # Persist only in-season table-cup history + any non-table cup rows.
        completed_df = _filter_frame_to_cup_table_season(completed_df)
        backfill_added = max(0, len(completed_df) - before)
    except Exception as exc:
        _progress(f"[WARN] in-season cup table backfill failed: {exc}")

    _write_csv(COMPLETED_CUP_PREDICTIONS_FILE, completed_df, CUP_HISTORY_COLUMNS)
    _write_csv(CUP_PREDICTIONS_FILE, cup_df, CUP_HISTORY_COLUMNS)
    print(
        f"[cups] START projection rebuild "
        f"(table_sims={CUP_TABLE_SIMULATION_RUNS}, bracket_sims={CUP_SIMULATION_RUNS})",
        flush=True,
    )
    table_rows, bracket_rounds = refresh_cup_projection_artifacts(completed_df, cup_df)
    print(f"[cups] DONE projection rebuild", flush=True)

    _progress(f"Cup mapping auto-added: {mapping_added} (drift detected: {mapping_drift})")
    if unresolved:
        _progress(f"Cup unresolved ESPN names by competition: {unresolved}")
    _progress(f"Cup predictions updated: {cup_updates}")
    _progress(f"Cup completed rows added to history: {completed_added}")
    _progress(f"Cup in-season table backfill rows merged: {backfill_added}")
    _progress(f"Cup completed rows removed from upcoming list: {removed_completed}")
    _progress(f"Cup totals entries added: {totals_added}")
    _progress(f"Cup projected table rows written: {table_rows}")
    _progress(f"Cup bracket sections written: {bracket_rounds}")
    _progress(f"Cup completed predictions file: {COMPLETED_CUP_PREDICTIONS_FILE}")
    _progress(f"Cup projected tables file: {PROJECTED_CUP_TABLES_FILE}")
    _progress(f"Cup real/live tables file: {REAL_CUP_TABLES_FILE}")
    _progress(f"Cup projected brackets file: {PROJECTED_CUP_BRACKETS_FILE}")
    _progress(f"[cups] DONE — Track_Cup_Results elapsed {time.monotonic() - _t0:.1f}s")
    _progress("Done.")


if __name__ == "__main__":
    main()
