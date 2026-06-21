import json
import os
from datetime import UTC, datetime, timedelta

import pandas as pd

from Update_Live_Prediction_Results import (
    ACCURACY_TOTALS_FILE,
    ESPN_BASE,
    SHARED_MAPPING_FILE,
    apply_mapping_updates,
    fetch_json,
    infer_result_code,
    load_accuracy_totals,
    load_predictions,
    load_shared_mapping,
    normalize_team_key,
    resolve_espn_team_name,
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
}
UEFA_TABLE_COMPETITIONS = {
    "UEFA/Champions League",
    "UEFA/Europa League",
    "UEFA/Conference League",
    "Europe/Champions League",
    "Europe/Europa League",
    "Europe/Conference League",
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


def _predicted_score(row):
    hg = _numeric_int(row.get("pred_home_goals"), 0)
    ag = _numeric_int(row.get("pred_away_goals"), 0)
    predicted = str(row.get("predicted_result", "")).strip().upper()
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
    result = actual if actual in {"H", "D", "A"} else predicted
    if result == "H":
        return str(row.get("home_team", "")).strip()
    if result == "A":
        return str(row.get("away_team", "")).strip()
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


def _uefa_match(stage, slot, home_team, away_team, winner=None):
    return {
        "stage": stage,
        "slot": slot,
        "match_date": "",
        "home_team": home_team,
        "away_team": away_team,
        "status": "Projected",
        "winner": winner or home_team,
        "actual_home_goals": None,
        "actual_away_goals": None,
        "pred_home_goals": None,
        "pred_away_goals": None,
        "predicted_result": "",
    }


def _build_uefa_bracket_from_table(competition, table_rows):
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
        winner = high_team
        playoff_winners.append(winner)
        playoff_matches.append(_uefa_match("First Round Playoff", idx, high_team, low_team, winner))

    top_seed_order = [1, 8, 4, 5, 2, 7, 3, 6]
    round_of_16 = []
    round_of_16_winners = []
    for idx, seed in enumerate(top_seed_order, start=1):
        top_seed = _seed_name(ranked_rows, seed)
        playoff_winner = playoff_winners[idx - 1] if idx - 1 < len(playoff_winners) else f"Playoff Winner {idx}"
        winner = top_seed
        round_of_16_winners.append(winner)
        round_of_16.append(_uefa_match("Round of 16", idx, top_seed, playoff_winner, winner))

    quarterfinals = []
    semifinalists = []
    for idx in range(0, 8, 2):
        home = round_of_16_winners[idx] if idx < len(round_of_16_winners) else f"R16 Winner {idx + 1}"
        away = round_of_16_winners[idx + 1] if idx + 1 < len(round_of_16_winners) else f"R16 Winner {idx + 2}"
        winner = home
        semifinalists.append(winner)
        quarterfinals.append(_uefa_match("Quarterfinals", (idx // 2) + 1, home, away, winner))

    semifinals = []
    finalists = []
    for idx in range(0, 4, 2):
        home = semifinalists[idx] if idx < len(semifinalists) else f"Quarterfinal Winner {idx + 1}"
        away = semifinalists[idx + 1] if idx + 1 < len(semifinalists) else f"Quarterfinal Winner {idx + 2}"
        winner = home
        finalists.append(winner)
        semifinals.append(_uefa_match("Semifinals", (idx // 2) + 1, home, away, winner))

    final_home = finalists[0] if finalists else "Semifinal Winner 1"
    final_away = finalists[1] if len(finalists) > 1 else "Semifinal Winner 2"
    final = [_uefa_match("Final", 1, final_home, final_away, final_home)]

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


def _limited_domestic_rounds(comp_frame):
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


def _build_projected_cup_brackets(completed_df, upcoming_df, tables_df):
    payload = {
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "competitions": {},
    }
    tables_df = _ensure_columns(tables_df, TABLE_COLUMNS)
    if not tables_df.empty:
        for competition, comp_table in tables_df.groupby("competition", dropna=False):
            competition_name = str(competition).strip()
            if competition_name not in UEFA_TABLE_COMPETITIONS:
                continue
            table_rows = comp_table.to_dict("records")
            payload["competitions"][competition_name] = _build_uefa_bracket_from_table(competition_name, table_rows)
    for competition_name in UEFA_PRIMARY_COMPETITIONS:
        payload["competitions"].setdefault(
            competition_name,
            _build_uefa_bracket_from_table(competition_name, []),
        )

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
        rounds = _limited_domestic_rounds(comp_frame)
        payload["competitions"][competition] = {
            "competition": competition,
            "format": "domestic_knockout_snapshot",
            "match_limit_per_section": DOMESTIC_BRACKET_MATCH_LIMIT,
            "rounds": rounds,
        }
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
