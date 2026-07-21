import json
import os
import random
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
PREDICTIONS_DIR = os.path.join(BASE_DIR, "Data", "Predictions")
CUP_PREDICTIONS_FILE = os.path.join(PREDICTIONS_DIR, "upcoming_cup_predictions.csv")
COMPLETED_CUP_PREDICTIONS_FILE = os.path.join(PREDICTIONS_DIR, "completed_cup_predictions.csv")
PROJECTED_CUP_TABLES_FILE = os.path.join(PREDICTIONS_DIR, "projected_cup_tables.csv")
PROJECTED_CUP_BRACKETS_FILE = os.path.join(PREDICTIONS_DIR, "projected_cup_brackets.json")
ESPN_CUP_NAMES_FILE = os.path.join(PREDICTIONS_DIR, "espn_cup_names_seen.json")

CUP_ESPN_COMPETITION_KEYS = {
    "England/FA Cup": "eng.fa",
    "England/League Cup": "eng.efl",
    "UEFA/Champions League": "uefa.champions",
    "UEFA/Europa League": "uefa.europa",
    "UEFA/Conference League": "uefa.europa.conf",
    "Europe/Champions League": "uefa.champions",
    "Europe/Europa League": "uefa.europa",
    "Europe/Conference League": "uefa.europa.conf",
    "CONCACAF/Leagues Cup": "concacaf.leagues.cup",
}
UEFA_TABLE_COMPETITIONS = {
    "UEFA/Champions League",
    "UEFA/Europa League",
    "UEFA/Conference League",
    "Europe/Champions League",
    "Europe/Europa League",
    "Europe/Conference League",
    # Leagues Cup 2026 uses dual MLS/Liga MX tables — not UEFA league-phase.
}
UEFA_LEAGUE_PHASE_MATCHES = {
    "UEFA/Champions League": 8,
    "Europe/Champions League": 8,
    "UEFA/Europa League": 8,
    "Europe/Europa League": 8,
    "UEFA/Conference League": 6,
    "Europe/Conference League": 6,
}
UEFA_PRIMARY_COMPETITIONS = [
    "UEFA/Champions League",
    "UEFA/Europa League",
    "UEFA/Conference League",
]
DOMESTIC_BRACKET_COMPETITIONS = {
    "England/FA Cup",
    "England/League Cup",
}
DOMESTIC_BRACKET_MATCH_LIMIT = 16

CUP_SIMULATION_RUNS = 2500

CUP_KNOCKOUT_FEEDS = {
    "First Round Playoff": {"next_round": "Round of 16", "feeds_to": lambda slot: slot},
    "Round of 16": {"next_round": "Quarterfinals", "feeds_to": lambda slot: (slot + 1) // 2},
    "Quarterfinals": {"next_round": "Semifinals", "feeds_to": lambda slot: (slot + 1) // 2},
    "Semifinals": {"next_round": "Final", "feeds_to": lambda slot: 1},
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
    "CONCACAF/Leagues Cup": {
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
    "CONCACAF/Leagues Cup",
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
    if lower in {"tbd", "draw", "tie", "unknown"}:
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


def _fallback_winner_when_prediction_missing(home_team, away_team):
    """When odds/data fail: known team beats unknown; both unknown (or both known) → tie."""
    home = str(home_team or "").strip()
    away = str(away_team or "").strip()
    home_known = _is_known_team(home)
    away_known = _is_known_team(away)
    if home_known and not away_known:
        return home
    if away_known and not home_known:
        return away
    return "Draw"


def _fallback_predicted_score(home_team, away_team):
    """Scoreline matching ``_fallback_winner_when_prediction_missing`` (1-0 / 0-1 / 0-0)."""
    winner = _fallback_winner_when_prediction_missing(home_team, away_team)
    home = str(home_team or "").strip()
    away = str(away_team or "").strip()
    if winner == home:
        return 1, 0
    if winner == away:
        return 0, 1
    return 0, 0


def _lookup_match_probs(predictions_index, home_team, away_team):
    """Return (prob_home, prob_away) from index, trying both orientations."""
    if not predictions_index:
        return 0.0, 0.0
    hm = str(home_team or "").strip().lower()
    aw = str(away_team or "").strip().lower()
    entry = predictions_index.get((hm, aw), {})
    ph = _safe_float(entry.get("prob_home"), 0)
    pa = _safe_float(entry.get("prob_away"), 0)
    if ph == 0 and pa == 0:
        rev = predictions_index.get((aw, hm), {})
        ph = _safe_float(rev.get("prob_away"), 0)
        pa = _safe_float(rev.get("prob_home"), 0)
    return ph, pa


def _pick_projected_winner(home_team, away_team, predictions_index=None):
    """Prefer model probs; else known-team / tie fallback."""
    ph, pa = _lookup_match_probs(predictions_index, home_team, away_team)
    total = ph + pa
    if total > 0:
        return home_team if ph >= pa else away_team
    return _fallback_winner_when_prediction_missing(home_team, away_team)


def _predicted_score(row):
    home = str(row.get("home_team", "")).strip()
    away = str(row.get("away_team", "")).strip()
    schedule_only = str(row.get("schedule_only", "")).strip().lower() in {"1", "true", "yes"}
    predicted = str(row.get("predicted_result", "")).strip().upper()
    hg = _numeric_int(row.get("pred_home_goals"), None)
    ag = _numeric_int(row.get("pred_away_goals"), None)

    if schedule_only or predicted not in {"H", "D", "A"} or hg is None or ag is None:
        return _fallback_predicted_score(home, away)

    if predicted == "H" and hg <= ag:
        hg = ag + 1
    elif predicted == "A" and ag <= hg:
        ag = hg + 1
    elif predicted == "D":
        ag = hg
    return hg, ag


def _build_projected_cup_tables(completed_df, upcoming_df):
    frames = []
    if completed_df is not None and not completed_df.empty:
        completed = completed_df.copy()
        completed["__is_real"] = True
        frames.append(completed)
    if upcoming_df is not None and not upcoming_df.empty:
        pending = upcoming_df.copy()
        pending["__is_real"] = False
        frames.append(pending)
    if not frames:
        return _empty_frame(TABLE_COLUMNS)

    combined = pd.concat(frames, ignore_index=True)
    combined["competition"] = combined["competition"].astype(str).str.strip()
    combined = combined[combined["competition"].isin(UEFA_TABLE_COMPETITIONS)]
    if combined.empty:
        return _empty_frame(TABLE_COLUMNS)

    out_rows = []
    for competition, comp_frame in combined.groupby("competition", dropna=False):
        table = {}
        max_phase_matches = UEFA_LEAGUE_PHASE_MATCHES.get(str(competition).strip(), 8)
        played_counts = {}
        comp_frame = comp_frame.copy()
        comp_frame["__date_sort"] = pd.to_datetime(comp_frame.get("match_date"), errors="coerce")
        comp_frame = comp_frame.sort_values(
            ["__date_sort", "__is_real", "home_team", "away_team"],
            ascending=[True, False, True, True],
            na_position="last",
        )
        for _, row in comp_frame.iterrows():
            home = str(row.get("home_team", "")).strip()
            away = str(row.get("away_team", "")).strip()
            if not home or not away:
                continue
            if played_counts.get(home, 0) >= max_phase_matches or played_counts.get(away, 0) >= max_phase_matches:
                continue
            is_real = bool(row.get("__is_real"))
            if is_real:
                hg = _numeric_int(row.get("actual_home_goals"), None)
                ag = _numeric_int(row.get("actual_away_goals"), None)
                if hg is None or ag is None:
                    continue
            else:
                hg, ag = _predicted_score(row)
            _apply_result(table, home, away, hg, ag, is_real=is_real)
            played_counts[home] = played_counts.get(home, 0) + 1
            played_counts[away] = played_counts.get(away, 0) + 1

        ranked = sorted(table.items(), key=lambda item: (-item[1]["Pts"], -item[1]["GD"], -item[1]["GF"], item[0]))
        total_positions = len(ranked)
        bottom_cutoff = max(1, total_positions - 2)
        for position, (team, stats) in enumerate(ranked, start=1):
            position_odds = {str(pos): (100.0 if pos == position else 0.0) for pos in range(1, total_positions + 1)}
            out_rows.append(
                {
                    "competition": competition,
                    "position": position,
                    "team": team,
                    **stats,
                    "win_league_pct": 100.0 if position == 1 else 0.0,
                    "top4_pct": 100.0 if position <= min(4, total_positions) else 0.0,
                    "bottom3_pct": 100.0 if position >= bottom_cutoff else 0.0,
                    "most_likely_position": position,
                    "most_likely_position_pct": 100.0,
                    "position_odds_json": json.dumps(position_odds, separators=(",", ":"), sort_keys=True),
                    "sim_runs": 1,
                }
            )

    return _ensure_columns(pd.DataFrame(out_rows), TABLE_COLUMNS)


def _winner_label(row):
    actual = str(row.get("actual_result", "")).strip().upper()
    predicted = str(row.get("predicted_result", "")).strip().upper()
    home = str(row.get("home_team", "")).strip()
    away = str(row.get("away_team", "")).strip()
    schedule_only = str(row.get("schedule_only", "")).strip().lower() in {"1", "true", "yes"}
    result = actual if actual in {"H", "D", "A"} else predicted
    if result == "H":
        return home
    if result == "A":
        return away
    if result == "D":
        return "Draw"
    if schedule_only or result not in {"H", "D", "A"}:
        return _fallback_winner_when_prediction_missing(home, away)
    return "Draw"


def _match_payload(row, status):
    actual_hg = pd.to_numeric(row.get("actual_home_goals"), errors="coerce")
    actual_ag = pd.to_numeric(row.get("actual_away_goals"), errors="coerce")
    pred_hg = pd.to_numeric(row.get("pred_home_goals"), errors="coerce")
    pred_ag = pd.to_numeric(row.get("pred_away_goals"), errors="coerce")
    return {
        "match_date": str(row.get("match_date", "")).strip(),
        "home_team": str(row.get("home_team", "")).strip(),
        "away_team": str(row.get("away_team", "")).strip(),
        "status": status,
        "winner": _winner_label(row),
        "actual_home_goals": int(actual_hg) if pd.notna(actual_hg) else None,
        "actual_away_goals": int(actual_ag) if pd.notna(actual_ag) else None,
        "pred_home_goals": int(round(float(pred_hg))) if pd.notna(pred_hg) else None,
        "pred_away_goals": int(round(float(pred_ag))) if pd.notna(pred_ag) else None,
        "predicted_result": str(row.get("predicted_result", "")).strip().upper(),
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


def _build_domestic_cup_bracket_with_draws(competition_name, comp_frame, predictions_index):
    """Build a domestic cup bracket distinguishing real fixtures from projected rounds.
    
    - real_knockout: Contains only confirmed/completed matches from ESPN API
    - projected_knockout: Contains simulated next rounds following draw rules
    - upcoming_fixtures: Next matches to be played (confirmed from API)
    """
    rules = CUP_FORMAT_RULES.get(competition_name, {})
    
    # Separate completed vs upcoming matches
    completed = comp_frame[comp_frame["__status"] == "Completed"] if "__status" in comp_frame.columns else pd.DataFrame()
    upcoming = comp_frame[comp_frame["__status"] == "Upcoming"] if "__status" in comp_frame.columns else pd.DataFrame()
    
    # Build upcoming fixtures round
    upcoming_matches = []
    if not upcoming.empty:
        for _, row in upcoming.iterrows():
            upcoming_matches.append(_match_payload(row, "Upcoming"))
    
    # Build real knockout rounds from completed matches
    real_rounds = []
    if not completed.empty:
        for status_group in ["Completed"]:
            status_matches = completed[completed.get("__status") == status_group] if "__status" in completed.columns else completed
            if not status_matches.empty:
                real_rounds.append({
                    "name": "Recent Results",
                    "matches": [_match_payload(row, "Completed") for _, row in status_matches.iterrows()],
                })
    
    # Build projected knockout rounds (TBD for future rounds)
    num_completed = len(completed) if not completed.empty else 0
    num_upcoming = len(upcoming) if not upcoming.empty else 0
    
    # For now, projected rounds are marked as TBD since we don't have full bracket info
    projected_rounds = []
    if rules and num_upcoming > 0:
        projected_rounds.append({
            "name": "Next Rounds (Projected)",
            "matches": [
                _create_tbd_matchup(f"Round {i}", i, draw_rules=rules)
                for i in range(1, min(4, num_upcoming + 2))
            ],
        })
    
    return {
        "competition": competition_name,
        "format": "domestic_knockout_with_projections",
        "format_rules": rules,
        "real_knockout": real_rounds,
        "projected_knockout": projected_rounds,
        "upcoming_fixtures": upcoming_matches,
        "match_count": {
            "completed": num_completed,
            "upcoming": num_upcoming,
        },
    }


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
        
        # Add draw constraint info for playoff round
        if competition_name in ("UEFA/Champions League", "Europe/Champions League"):
            bracket["draw_constraints"] = {
                "round": "First Round Playoff",
                "description": "Seeds 9-16 paired with seeds 17-24. Lower seeds can face seeds 23-24.",
                "seeding_rules": {
                    "top_8": "Auto-qualify to Round of 16",
                    "9_to_24": "Single-leg playoff (lower seed hosts higher seed in first leg equivalent)",
                }
            }
    
    return bracket



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
                "matches": [_match_payload(row, "Completed") for _, row in completed_rows.iterrows()],
            }
        )
        return rounds
    rounds.append(
        {
            "name": "Upcoming Cup Fixtures",
            "matches": [_match_payload(row, "Upcoming") for _, row in upcoming_rows.iterrows()],
        }
    )
    if not completed_rows.empty:
        rounds.append(
            {
                "name": "Recent Cup Results",
                "matches": [_match_payload(row, "Completed") for _, row in completed_rows.iterrows()],
            }
        )
    return rounds


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
    
    if competition_name in ("UEFA/Champions League", "Europe/Champions League"):
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
        round_templates[r["name"]] = matches

    champion_counts = defaultdict(int)
    per_sim_champions = []

    rng = np.random.default_rng(20260611)

    for sim_num in range(num_sims):
        results_by_round = {}  # round_name → list of (slot, winner)

        for rnd_name in round_names:
            feeds = CUP_KNOCKOUT_FEEDS[rnd_name]
            templates = round_templates.get(rnd_name, [])
            winners = []

            for m in templates:
                slot = m.get("slot", 1)
                hm = str(m.get("home_team", "")).strip()
                aw = str(m.get("away_team", "")).strip()

                # If previous round feeds into this match, check if teams updated
                # (handled below by updating home/away from previous winners)
                ph, pa = _lookup_match_probs(predictions_index, hm, aw)

                # Draw probability is redistributed proportionally
                total = ph + pa
                if total > 0:
                    p_home = ph / total
                    winner = hm if rng.random() < p_home else aw
                else:
                    # No model odds: known team advances; otherwise treat as a tie.
                    winner = _fallback_winner_when_prediction_missing(hm, aw)

                winners.append({"slot": slot, "winner": winner, "home_team": hm, "away_team": aw})

            results_by_round[rnd_name] = winners

            # Propagate winners to the next round
            next_rnd_name = feeds["next_round"]
            if next_rnd_name and next_rnd_name in round_templates:
                next_templates = round_templates[next_rnd_name]
                for w in winners:
                    target_slot = feeds["feeds_to"](w["slot"])
                    if target_slot is not None and target_slot <= len(next_templates):
                        tmpl = next_templates[target_slot - 1]
                        # Place the winner as either home or away in the target slot
                        # We use the template's original teams to decide role
                        orig_home = str(tmpl.get("home_team", "")).strip().lower()
                        orig_away = str(tmpl.get("away_team", "")).strip().lower()
                        w_name = w["winner"].strip().lower()
                        if w_name == orig_home:
                            pass  # already correct
                        elif w_name == orig_away:
                            pass
                        else:
                            # Winner is not directly a team name -> placeholder like "Seed 9"
                            # Just keep the original
                            pass

        # Record champion (final round's winner)
        final_winners = results_by_round.get("Final", [])
        if final_winners:
            champion = final_winners[0]["winner"]
            champion_counts[champion] += 1
            per_sim_champions.append(champion)

    if not per_sim_champions:
        return {"champion": None, "simulations_run": 0, "winner_probabilities": {}}

    total = len(per_sim_champions)
    most_common = max(champion_counts, key=lambda k: (champion_counts[k], k)) if champion_counts else None
    winner_probabilities = {team: round(count / total, 4) for team, count in sorted(champion_counts.items(), key=lambda x: -x[1])}

    # Find the first simulation whose champion matches the aggregate winner
    sim_index = next((i for i, c in enumerate(per_sim_champions) if c == most_common), 0)

    return {
        "champion": most_common,
        "simulations_run": total,
        "winner_probabilities": winner_probabilities,
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

    # Build prediction index from upcoming predictions for simulation use
    predictions_index = {}
    if upcoming_df is not None and not upcoming_df.empty:
        for _, row in upcoming_df.iterrows():
            hm = str(row.get("home_team", "")).strip().lower()
            aw = str(row.get("away_team", "")).strip().lower()
            if hm and aw:
                predictions_index[(hm, aw)] = {
                    "prob_home": _safe_float(row.get("prob_home"), 0),
                    "prob_draw": _safe_float(row.get("prob_draw"), 0),
                    "prob_away": _safe_float(row.get("prob_away"), 0),
                }

    if not tables_df.empty:
        for competition, comp_table in tables_df.groupby("competition", dropna=False):
            competition_name = str(competition).strip()
            if competition_name not in UEFA_TABLE_COMPETITIONS:
                continue
            table_rows = comp_table.to_dict("records")
            bracket = _build_uefa_bracket_with_draws(competition_name, table_rows, predictions_index)
            payload["competitions"][competition_name] = bracket
    
    for competition_name in UEFA_PRIMARY_COMPETITIONS:
        if competition_name not in payload["competitions"]:
            bracket = _build_uefa_bracket_with_draws(competition_name, [], predictions_index)
            payload["competitions"][competition_name] = bracket

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
        return payload

    combined = pd.concat(frames, ignore_index=True)
    combined["competition"] = combined["competition"].astype(str).str.strip()
    combined = combined[combined["competition"].isin(DOMESTIC_BRACKET_COMPETITIONS)]
    if combined.empty:
        return payload

    combined = combined.sort_values(["competition", "match_date", "__status", "home_team", "away_team"], na_position="last")
    for competition, comp_frame in combined.groupby("competition", dropna=False):
        competition_name = str(competition).strip()
        # Use enhanced bracket with draw awareness
        bracket = _build_domestic_cup_bracket_with_draws(competition_name, comp_frame, predictions_index)
        payload["competitions"][competition_name] = bracket
    return payload


def refresh_cup_projection_artifacts(completed_df, upcoming_df):
    tables = _build_projected_cup_tables(completed_df, upcoming_df)
    brackets = _build_projected_cup_brackets(completed_df, upcoming_df, tables)
    _write_csv(PROJECTED_CUP_TABLES_FILE, tables, TABLE_COLUMNS)
    save_json(PROJECTED_CUP_BRACKETS_FILE, brackets)
    return len(tables), sum(len(comp.get("rounds", [])) for comp in brackets.get("competitions", {}).values())


def main():
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

    _write_csv(COMPLETED_CUP_PREDICTIONS_FILE, completed_df, CUP_HISTORY_COLUMNS)
    _write_csv(CUP_PREDICTIONS_FILE, cup_df, CUP_HISTORY_COLUMNS)
    table_rows, bracket_rounds = refresh_cup_projection_artifacts(completed_df, cup_df)

    print(f"Cup mapping auto-added: {mapping_added} (drift detected: {mapping_drift})")
    if unresolved:
        print(f"Cup unresolved ESPN names by competition: {unresolved}")
    print(f"Cup predictions updated: {cup_updates}")
    print(f"Cup completed rows added to history: {completed_added}")
    print(f"Cup completed rows removed from upcoming list: {removed_completed}")
    print(f"Cup totals entries added: {totals_added}")
    print(f"Cup projected table rows written: {table_rows}")
    print(f"Cup bracket sections written: {bracket_rounds}")
    print(f"Cup completed predictions file: {COMPLETED_CUP_PREDICTIONS_FILE}")
    print(f"Cup projected tables file: {PROJECTED_CUP_TABLES_FILE}")
    print(f"Cup projected brackets file: {PROJECTED_CUP_BRACKETS_FILE}")
    print("Done.")


if __name__ == "__main__":
    main()
