import argparse
import os
import random
import sys
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_THIS_DIR)
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

import football_data_api as fda
import Process_National_Team_Data as national


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREDICTIONS_DIR = os.path.join(BASE_DIR, "Data", "Predictions")
PREDICTIONS_FILE = os.path.join(PREDICTIONS_DIR, "upcoming_national_team_predictions.csv")

RESULT_COLUMNS = [
    "prediction_key",
    "created_at_utc",
    "match_date",
    "match_datetime_utc",
    "competition",
    "stage",
    "venue",
    "home_team",
    "away_team",
    "display_home_team",
    "display_away_team",
    "is_neutral_site",
    "source",
    "predicted_result",
    "prob_home",
    "prob_draw",
    "prob_away",
    "probability_reasoning",
    "pred_home_goals",
    "pred_away_goals",
    "actual_home_goals",
    "actual_away_goals",
    "actual_result",
    "is_correct",
    "settled_at_utc",
]


def parse_cli_args():
    parser = argparse.ArgumentParser(
        description="Fetch upcoming national-team fixtures and generate predictions from the national predictor."
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=90,
        help="Lookahead window for upcoming national-team fixtures.",
    )
    parser.add_argument(
        "--world-cup-only",
        action="store_true",
        help="Only predict FIFA World Cup fixtures.",
    )
    parser.add_argument(
        "--api-token",
        type=str,
        default=os.getenv("FOOTBALL_DATA_API_TOKEN", "").strip(),
        help="Optional football-data.org token for supplemental scheduled fixtures.",
    )
    return parser.parse_args()


def load_prediction_store(path):
    if not os.path.exists(path):
        return pd.DataFrame(columns=RESULT_COLUMNS)
    frame = pd.read_csv(path)
    for col in RESULT_COLUMNS:
        if col not in frame.columns:
            frame[col] = ""
    return frame[RESULT_COLUMNS].astype("object")


def competition_configs(world_cup_only=False):
    if world_cup_only:
        return {"FIFA/World Cup": national.UPCOMING_ESPN_COMPETITIONS["FIFA/World Cup"]}
    return dict(national.UPCOMING_ESPN_COMPETITIONS)


def iter_window_dates(window_days):
    today = datetime.now(UTC).date()
    end = today + timedelta(days=max(0, int(window_days)))
    current = today
    while current <= end:
        yield current
        current += timedelta(days=1)


def parse_espn_fixture(event, competition_name):
    parsed = national.parse_espn_event(event, competition_name, require_completed=False)
    if not parsed:
        return None
    if parsed.get("FTR"):
        return None
    match_dt = pd.to_datetime(parsed.get("match_datetime_utc"), utc=True, errors="coerce")
    if pd.isna(match_dt):
        return None
    if match_dt < pd.Timestamp(datetime.now(UTC)):
        return None
    # Ensure all required fields are present for fixture compatibility
    fixture = {
        "match_id": parsed.get("match_id", ""),
        "match_datetime_utc": parsed.get("match_datetime_utc", ""),
        "match_date": parsed.get("match_date", ""),
        "competition": parsed.get("competition", ""),
        "stage": parsed.get("stage", "unknown"),
        "home_team": parsed.get("home_team", ""),
        "away_team": parsed.get("away_team", ""),
        "is_neutral_site": parsed.get("is_neutral_site", True),
        "venue": parsed.get("venue", ""),
        "source": "espn",
    }
    return fixture


def fetch_espn_upcoming_fixtures(window_days, world_cup_only=False):
    rows = []
    seen_event_ids = set()
    rows_lock = threading.Lock()
    
    configs = competition_configs(world_cup_only=world_cup_only)
    
    def fetch_competition_day(espn_id, competition_name, date):
        """Fetch fixtures for a specific competition and date."""
        try:
            for _, payload in national.fetch_espn_scoreboard_days(espn_id, [date], timeout=30):
                for event in payload.get("events") or []:
                    event_id = str(event.get("id", "")).strip()
                    with rows_lock:
                        if event_id and event_id in seen_event_ids:
                            continue
                    fixture = parse_espn_fixture(event, competition_name)
                    if not fixture:
                        continue
                    with rows_lock:
                        if event_id and event_id not in seen_event_ids:
                            if event_id:
                                seen_event_ids.add(event_id)
                            rows.append(fixture)
        except Exception as e:
            # Log errors but don't fail the entire fetch
            print(f"[DEBUG] Error fetching {competition_name} on {date}: {e}")
    
    # Prepare all tasks
    tasks = []
    dates = list(iter_window_dates(window_days))
    for competition_name, config in sorted(configs.items(), key=lambda item: item[1]["priority"]):
        espn_id = config["espn_id"]
        print(f"Scheduling ESPN fixtures for {competition_name} ({espn_id})")
        for date in dates:
            tasks.append((espn_id, competition_name, date))
    
    # Execute tasks concurrently
    max_workers = min(8, len(tasks))  # Limit concurrent requests
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for espn_id, competition_name, date in tasks:
            future = executor.submit(fetch_competition_day, espn_id, competition_name, date)
            futures.append(future)
        
        # Wait for all tasks to complete
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"[DEBUG] Task failed: {e}")
    
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def parse_football_data_fixture(match, competition_name):
    parsed = national.parse_football_data_match(match, competition_name, completed_only=False)
    if not parsed:
        return None
    match_dt = pd.to_datetime(parsed.get("match_datetime_utc"), utc=True, errors="coerce")
    if pd.isna(match_dt) or match_dt < pd.Timestamp(datetime.now(UTC)):
        return None
    parsed["source"] = "football-data.org"
    return parsed


def fetch_football_data_upcoming_fixtures(api_token, window_days, world_cup_only=False):
    if not api_token:
        return pd.DataFrame()

    rows = []
    today = datetime.now(UTC).date()
    end = today + timedelta(days=max(0, int(window_days)))
    headers = {"X-Auth-Token": api_token}
    wanted_names = set(competition_configs(world_cup_only=world_cup_only).keys())
    api_competitions = [
        (code, name)
        for code, name in national.FOOTBALL_DATA_COMPETITIONS.items()
        if name in wanted_names
    ]
    for index, (competition_code, competition_name) in enumerate(api_competitions):
        fda.wait_between_competition_requests(competition_name, is_first=index == 0)
        query = urllib.parse.urlencode(
            {
                "dateFrom": today.strftime("%Y-%m-%d"),
                "dateTo": end.strftime("%Y-%m-%d"),
                "status": "SCHEDULED",
            }
        )
        url = f"{national.FOOTBALL_DATA_API_BASE}/competitions/{competition_code}/matches?{query}"
        try:
            payload = fda.fetch_json(url, headers=headers, timeout=45, competition_name=competition_name)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise RuntimeError("football-data.org API token is invalid.") from error
            continue
        except Exception:
            continue
        for match in payload.get("matches") or []:
            parsed = parse_football_data_fixture(match, competition_name)
            if parsed:
                rows.append(parsed)
    return pd.DataFrame(rows)


def dedupe_fixtures(fixtures):
    if fixtures.empty:
        return fixtures.copy()
    frame = fixtures.copy()
    
    # Ensure all required columns exist
    required_cols = ["match_date", "competition", "home_team", "away_team", "source", "match_datetime_utc"]
    for col in required_cols:
        if col not in frame.columns:
            frame[col] = "" if col != "match_datetime_utc" else pd.NaT
    
    # Validate and filter out invalid rows
    frame = frame.dropna(subset=["home_team", "away_team"], how="any")
    frame = frame[frame["home_team"].astype(str).str.strip() != ""]
    frame = frame[frame["away_team"].astype(str).str.strip() != ""]
    
    if frame.empty:
        return frame
    
    source_order = {"espn": 0, "football-data.org": 1}
    frame["source_order"] = frame["source"].astype(str).map(source_order).fillna(99)
    
    # Create prediction keys safely
    try:
        frame["prediction_key"] = frame.apply(
            lambda row: national.make_prediction_key(
                row.get("match_date", ""), 
                row.get("competition", ""), 
                row.get("home_team", ""), 
                row.get("away_team", "")
            ),
            axis=1,
        )
    except Exception as e:
        print(f"[ERROR] Failed to create prediction keys: {e}")
        return pd.DataFrame()
    
    frame = frame.sort_values(["source_order", "match_datetime_utc", "prediction_key"], na_position="last")
    frame = frame.drop_duplicates(subset=["prediction_key"], keep="first")
    frame = frame.drop(columns=["source_order"], errors="ignore")
    
    return frame.sort_values(["match_datetime_utc", "competition", "home_team"], na_position="last").reset_index(drop=True)


def load_upcoming_fixtures(api_token, window_days, world_cup_only=False):
    espn = fetch_espn_upcoming_fixtures(window_days, world_cup_only=world_cup_only)
    football_data = fetch_football_data_upcoming_fixtures(
        api_token,
        window_days,
        world_cup_only=world_cup_only,
    )
    frames = [frame for frame in [espn, football_data] if not frame.empty]
    if not frames:
        return pd.DataFrame()
    return dedupe_fixtures(pd.concat(frames, ignore_index=True))


def probabilities_from_model(feature_frame, bundle):
    probabilities = {"H": 0.0, "D": 0.0, "A": 0.0}
    proba_values = bundle["clf"].predict_proba(feature_frame)[0]
    for idx, encoded_label in enumerate(bundle["clf"].classes_):
        label = bundle["result_label_encoder"].inverse_transform([encoded_label])[0]
        probabilities[label] = float(proba_values[idx])
    return probabilities


def normalize_probabilities(probabilities):
    total = sum(max(0.0, float(probabilities.get(key, 0.0))) for key in ["H", "D", "A"])
    if total <= 0:
        return {"H": 1 / 3, "D": 1 / 3, "A": 1 / 3}
    return {key: max(0.0, float(probabilities.get(key, 0.0))) / total for key in ["H", "D", "A"]}


def adjust_for_knockout(probabilities, stage):
    probabilities = normalize_probabilities(probabilities)
    if not national.stage_is_knockout(stage):
        return probabilities
    draw_carry = probabilities["D"] * 0.35
    probabilities["D"] -= draw_carry
    non_draw = probabilities["H"] + probabilities["A"]
    if non_draw > 0:
        probabilities["H"] += draw_carry * probabilities["H"] / non_draw
        probabilities["A"] += draw_carry * probabilities["A"] / non_draw
    else:
        probabilities["H"] += draw_carry * 0.5
        probabilities["A"] += draw_carry * 0.5
    return normalize_probabilities(probabilities)


def predict_fixture(row, bundle):
    snapshot = bundle["snapshot"]
    raw_home = str(row.get("home_team", "")).strip()
    raw_away = str(row.get("away_team", "")).strip()
    competition = str(row.get("competition", "")).strip()
    stage = str(row.get("stage", "") or "unknown").strip().lower() or "unknown"
    is_neutral_site = bool(row.get("is_neutral_site", False))
    match_date = row.get("match_date")

    raw_features, home_team, away_team = national.build_prediction_feature_frame(
        raw_home,
        raw_away,
        competition,
        stage,
        is_neutral_site,
        snapshot,
    )
    if not home_team or not away_team or home_team == away_team:
        return None

    feature_frame = national.align_feature_frame(raw_features, bundle)
    probabilities = probabilities_from_model(feature_frame, bundle)
    probabilities = adjust_for_knockout(probabilities, stage)
    prediction_key = national.make_prediction_key(match_date, competition, home_team, away_team)
    jitter_delta = 0.018 if "world cup" in competition.lower() else 0.026
    probabilities = national.probability_jitter(probabilities, prediction_key, jitter_delta)

    pred_home_goals = max(0.0, float(bundle["home_goal_reg"].predict(feature_frame)[0]))
    pred_away_goals = max(0.0, float(bundle["away_goal_reg"].predict(feature_frame)[0]))
    # Use probabilistic sampling instead of always taking the max
    # This adds variation in group stage outcomes
    results = list(probabilities.keys())
    probs_list = [probabilities[r] for r in results]
    predicted_result = random.choices(results, weights=probs_list, k=1)[0]

    p_home = probabilities["H"] * 100
    p_draw = probabilities["D"] * 100
    p_away = probabilities["A"] * 100
    max_p = max(p_home, p_draw, p_away)
    conf = "High" if max_p >= 55 else ("Medium" if max_p >= 40 else "Low")
    result_label = {"H": "Home win", "D": "Draw", "A": "Away win"}.get(predicted_result, predicted_result)
    venue_str = "Neutral site" if is_neutral_site else "Standard venue"
    knockout_str = "Knockout" if "knockout" in stage else stage.replace("_", " ").title()

    parsed_dt = pd.to_datetime(row.get("match_datetime_utc"), utc=True, errors="coerce")
    match_date_text = (
        parsed_dt.strftime("%Y-%m-%d")
        if not pd.isna(parsed_dt)
        else str(match_date)[:10]
    )

    return {
        "prediction_key": prediction_key,
        "created_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "match_date": match_date_text,
        "match_datetime_utc": str(row.get("match_datetime_utc", "")).strip(),
        "competition": competition,
        "stage": stage,
        "venue": str(row.get("venue", "") or "").strip(),
        "home_team": home_team,
        "away_team": away_team,
        "display_home_team": raw_home,
        "display_away_team": raw_away,
        "is_neutral_site": "1" if is_neutral_site else "0",
        "source": str(row.get("source", "") or "").strip(),
        "predicted_result": predicted_result,
        "prob_home": round(probabilities["H"], 6),
        "prob_draw": round(probabilities["D"], 6),
        "prob_away": round(probabilities["A"], 6),
        "probability_reasoning": (
            f"Predicted: {result_label} ({p_home:.0f}%/{p_draw:.0f}%/{p_away:.0f}%) | "
            f"xG: {pred_home_goals:.1f}-{pred_away_goals:.1f} | "
            f"{venue_str} | {knockout_str} | "
            f"Confidence: {conf} | Jitter: {jitter_delta:.3f}"
        ),
        "pred_home_goals": round(pred_home_goals, 3),
        "pred_away_goals": round(pred_away_goals, 3),
        "actual_home_goals": None,
        "actual_away_goals": None,
        "actual_result": None,
        "is_correct": None,
        "settled_at_utc": None,
    }


def keep_only_current_fixtures(predictions_df, fixtures_df, bundle):
    if predictions_df.empty or fixtures_df.empty:
        return predictions_df.iloc[0:0].copy()
    fixture_keys = set()
    snapshot = bundle["snapshot"]
    for _, row in fixtures_df.iterrows():
        raw_features, home_team, away_team = national.build_prediction_feature_frame(
            row.get("home_team", ""),
            row.get("away_team", ""),
            row.get("competition", ""),
            row.get("stage", "unknown"),
            bool(row.get("is_neutral_site", False)),
            snapshot,
        )
        del raw_features
        if home_team and away_team and home_team != away_team:
            fixture_keys.add(
                national.make_prediction_key(
                    row.get("match_date"),
                    row.get("competition", ""),
                    home_team,
                    away_team,
                )
            )
    frame = predictions_df.copy()
    return frame[frame["prediction_key"].astype(str).isin(fixture_keys)].copy()


def merge_prediction_frames(existing_df, new_df):
    if existing_df.empty and new_df.empty:
        return pd.DataFrame(columns=RESULT_COLUMNS)
    if existing_df.empty:
        return new_df.copy()
    if new_df.empty:
        return existing_df.copy()
    
    # Ensure both DataFrames have the same columns before concat
    for col in RESULT_COLUMNS:
        if col not in existing_df.columns:
            existing_df[col] = None
        if col not in new_df.columns:
            new_df[col] = None
    
    # Append new rows last so duplicate prediction keys keep the freshest prediction.
    combined = pd.concat(
        [existing_df[RESULT_COLUMNS], new_df[RESULT_COLUMNS]], 
        ignore_index=True
    )
    combined = combined.drop_duplicates(subset=["prediction_key"], keep="last")
    return combined.reset_index(drop=True)


def main():
    args = parse_cli_args()
    bundle = national.load_model_bundle()
    fixtures = load_upcoming_fixtures(
        args.api_token,
        args.window_days,
        world_cup_only=args.world_cup_only,
    )
    if fixtures.empty:
        print("No upcoming national-team fixtures returned by ESPN or football-data.org.")
        return

    existing = load_prediction_store(PREDICTIONS_FILE)
    new_records = []
    skipped = 0
    for _, fixture in fixtures.iterrows():
        try:
            prediction = predict_fixture(fixture, bundle)
            if prediction is None:
                skipped += 1
                continue
            new_records.append(prediction)
        except Exception as e:
            skipped += 1
            print(f"[DEBUG] Prediction failed for {fixture.get('home_team')} vs {fixture.get('away_team')}: {e}")
            continue

    if not new_records:
        if existing.empty:
            print("No national-team predictions were generated.")
        else:
            print("No new predictions generated, keeping existing data.")
        return

    # Create new predictions DataFrame with correct columns
    new_df = pd.DataFrame(new_records)
    for col in RESULT_COLUMNS:
        if col not in new_df.columns:
            new_df[col] = None
    new_df = new_df[RESULT_COLUMNS].astype("object")

    # Merge with existing predictions
    combined = merge_prediction_frames(existing.astype("object"), new_df)

    # Filter to keep only current fixtures
    combined = keep_only_current_fixtures(combined, fixtures, bundle)
    combined = combined.drop_duplicates(subset=["prediction_key"], keep="last")
    
    # Ensure all columns exist and sort
    for col in RESULT_COLUMNS:
        if col not in combined.columns:
            combined[col] = None
    combined = combined[RESULT_COLUMNS].sort_values(
        ["match_date", "competition", "home_team", "away_team"], 
        na_position="last"
    )

    os.makedirs(PREDICTIONS_DIR, exist_ok=True)
    combined.to_csv(PREDICTIONS_FILE, index=False)

    world_cup_count = int(combined["competition"].astype(str).str.contains("World Cup", case=False, na=False).sum())
    print("\nUpcoming national-team predictions generated")
    print(f"Fixtures found: {len(fixtures)}")
    print(f"Predictions written: {len(new_df)}")
    print(f"World Cup predictions currently in file: {world_cup_count}")
    print(f"Skipped fixtures: {skipped}")
    print(f"Saved tracking file: {PREDICTIONS_FILE}")


if __name__ == "__main__":
    main()
