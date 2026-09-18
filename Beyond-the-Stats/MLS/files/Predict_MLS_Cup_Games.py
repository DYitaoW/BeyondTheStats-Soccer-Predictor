import argparse
import os
from datetime import UTC, datetime

import pandas as pd

import Predict_Upcoming_Matchweek as upcoming


PREDICTIONS_FILE = os.path.join(upcoming.PREDICTIONS_DIR, "upcoming_mls_cup_predictions.csv")
ESPN_SCOREBOARD_API = "https://site.api.espn.com/apis/site/v2/sports/soccer/{espn_id}/scoreboard"
THESPORTSDB_API_BASE = "https://www.thesportsdb.com/api/v1/json/123"
DEFAULT_MLS_CUP_FIXTURE_WINDOW_DAYS = 7

MLS_CUP_COMPETITIONS = {
    "US Open Cup": {
        "name": "United States/US Open Cup",
        "sportsdb_id": "5199",
        "espn_id": "usa.open",
        "priority": 1,
        "fixture_window_days": 7,
    },
}


def parse_cli_args():
    parser = argparse.ArgumentParser(
        description="Predict upcoming MLS cup fixtures with the MLS model."
    )
    parser.add_argument(
        "--refresh-download",
        action="store_true",
        help="Download the latest MLS raw CSV files first using Download_Latest_Data.py.",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=DEFAULT_MLS_CUP_FIXTURE_WINDOW_DAYS,
        help="Fallback days after an MLS cup's first upcoming fixture for cups without a configured fixture window.",
    )
    return parser.parse_args()


def cup_fixture_window_days(cup_data, fallback_window_days):
    try:
        return int(cup_data.get("fixture_window_days", fallback_window_days))
    except (TypeError, ValueError):
        return int(fallback_window_days)


def filter_to_next_fixture_window(fixtures, window_days, label):
    if fixtures.empty:
        return fixtures

    frame = fixtures.copy()
    frame["match_date"] = pd.to_datetime(frame["match_date"], errors="coerce").dt.normalize()
    frame = frame[frame["match_date"].notna()].copy()
    if frame.empty:
        return frame

    first_fixture_date = frame["match_date"].min()
    cutoff_date = upcoming.calculate_fixture_window_end(window_days, start_date=first_fixture_date)
    filtered = frame[frame["match_date"] <= cutoff_date].reset_index(drop=True)
    print(
        f"{label}: First fixture {first_fixture_date.date()} anchors "
        f"{window_days}-day cutoff at {cutoff_date.date()}."
    )
    return filtered


def load_upcoming_mls_cup_fixtures_from_thesportsdb(window_days, cup_data):
    competition_name = cup_data["name"]
    league_id = cup_data.get("sportsdb_id")
    if not league_id:
        return pd.DataFrame()

    url = f"{THESPORTSDB_API_BASE}/eventsnextleague.php?id={league_id}"
    try:
        data = upcoming.fetch_json(url, timeout=30)
    except Exception as exc:
        print(f"TheSportsDB API error for {competition_name}: {exc}")
        return pd.DataFrame()

    events = data.get("events") or []
    if not isinstance(events, list) or not events:
        print(f"TheSportsDB: No events found for {competition_name}")
        return pd.DataFrame()

    today = pd.Timestamp(datetime.now(UTC).date()).normalize()
    rows = []
    for event in events:
        date_str = event.get("dateEvent")
        if not date_str:
            continue
        match_date = pd.to_datetime(date_str, utc=True, errors="coerce")
        if pd.isna(match_date):
            continue
        match_date = match_date.tz_localize(None)
        if match_date < today:
            continue

        home_team = str(event.get("strHomeTeam", "")).strip()
        away_team = str(event.get("strAwayTeam", "")).strip()
        if not home_team or not away_team:
            continue

        rows.append(
            {
                "match_date": match_date,
                "match_datetime_et": event.get("strTime") or "",
                "competition": competition_name,
                "home_team": home_team,
                "away_team": away_team,
            }
        )

    fixtures = pd.DataFrame(rows)
    if fixtures.empty:
        print(f"TheSportsDB: No valid fixtures for {competition_name}")
        return fixtures
    fixtures = fixtures.sort_values(["match_date", "home_team", "away_team"]).reset_index(drop=True)
    fixtures = filter_to_next_fixture_window(fixtures, window_days, f"TheSportsDB {competition_name}")
    if fixtures.empty:
        print(f"TheSportsDB: No fixtures within anchored window for {competition_name}")
    return fixtures


def load_upcoming_mls_cup_fixtures_from_espn(window_days, cup_data, lookahead_days=30):
    competition_name = cup_data["name"]
    espn_id = cup_data.get("espn_id")
    if not espn_id:
        return pd.DataFrame()

    today = pd.Timestamp(datetime.now(UTC).date()).normalize()
    rows = []
    seen = set()

    for offset in range(0, max(1, lookahead_days + 1)):
        day = today + pd.Timedelta(days=offset)

        url = ESPN_SCOREBOARD_API.format(espn_id=espn_id) + f"?dates={day.strftime('%Y%m%d')}"
        try:
            data = upcoming.fetch_json(url, timeout=30)
        except Exception as exc:
            print(f"ESPN API error for {competition_name} on {day.date()}: {exc}")
            continue

        events = data.get("events", [])
        if not isinstance(events, list):
            continue

        for event in events:
            event_date = pd.to_datetime(event.get("date"), utc=True, errors="coerce")
            if pd.isna(event_date):
                continue
            event_dt_et = event_date.tz_convert(upcoming.EASTERN_TZ)
            match_date = event_dt_et.tz_localize(None).normalize()
            if match_date < today:
                continue

            competitions = event.get("competitions", [])
            if not competitions:
                continue
            comp0 = competitions[0] or {}
            status_state = (
                ((comp0.get("status") or {}).get("type") or {}).get("state", "")
            ).strip().lower()
            if status_state and status_state not in {"pre"}:
                continue

            home_team = ""
            away_team = ""
            for competitor in comp0.get("competitors", []):
                team_name = ((competitor.get("team") or {}).get("displayName") or "").strip()
                side = str(competitor.get("homeAway", "")).strip().lower()
                if side == "home":
                    home_team = team_name
                elif side == "away":
                    away_team = team_name
            if not home_team or not away_team:
                continue

            key = (match_date.strftime("%Y-%m-%d"), competition_name, home_team, away_team)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "match_date": match_date,
                    "match_datetime_et": event_dt_et.isoformat(),
                    "competition": competition_name,
                    "home_team": home_team,
                    "away_team": away_team,
                }
            )

    fixtures = pd.DataFrame(rows)
    if fixtures.empty:
        return fixtures
    fixtures = fixtures.sort_values(["match_date", "home_team", "away_team"]).reset_index(drop=True)
    return filter_to_next_fixture_window(fixtures, window_days, f"ESPN {competition_name}")


def load_upcoming_mls_cup_fixtures(window_days):
    all_fixtures = []
    sorted_cups = sorted(MLS_CUP_COMPETITIONS.items(), key=lambda item: item[1]["priority"])
    for cup_name, cup_data in sorted_cups:
        cup_window_days = cup_fixture_window_days(cup_data, window_days)
        print(f"\n--- Checking MLS cup source for: {cup_name} ({cup_window_days}-day fixture window) ---")

        fixtures = load_upcoming_mls_cup_fixtures_from_thesportsdb(cup_window_days, cup_data)
        if not fixtures.empty:
            all_fixtures.append(fixtures)
            print(f"SUCCESS: Loaded {len(fixtures)} fixtures for {cup_name} from TheSportsDB.")
            continue

        fixtures = load_upcoming_mls_cup_fixtures_from_espn(cup_window_days, cup_data)
        if not fixtures.empty:
            all_fixtures.append(fixtures)
            print(f"SUCCESS: Loaded {len(fixtures)} fixtures for {cup_name} from ESPN.")
            continue

    if not all_fixtures:
        return pd.DataFrame()
    return pd.concat(all_fixtures, ignore_index=True)


def resolve_mls_cup_team_name(raw_name, competition, mapping, context):
    known_teams = set(context.get("available_teams", []))
    api_name = str(raw_name).strip()
    if not api_name:
        return ""

    for mapping_competition in [competition, upcoming.MLS_COMPETITION_NAME]:
        mapped_name = str(mapping.get(mapping_competition, {}).get(api_name, "")).strip()
        if mapped_name in known_teams:
            return mapped_name

    if api_name in known_teams:
        return api_name

    resolved = upcoming.resolve_live_team_name(api_name, upcoming.MLS_COMPETITION_NAME, context)
    return resolved or ""


def update_mls_cup_team_mapping(fixtures, context, mapping):
    updated = dict(mapping)
    new_entries = 0
    blanks_added = 0

    for _, row in fixtures.iterrows():
        competition = str(row.get("competition", "")).strip()
        if not competition:
            continue
        updated.setdefault(competition, {})

        for side_col in ["home_team", "away_team"]:
            api_name = str(row.get(side_col, "")).strip()
            if not api_name or api_name in updated[competition]:
                continue
            resolved = resolve_mls_cup_team_name(api_name, competition, updated, context)
            updated[competition][api_name] = resolved
            new_entries += 1
            if not resolved:
                blanks_added += 1

    return updated, new_entries, blanks_added


def apply_mls_cup_team_mapping(fixtures, mapping, context):
    mapped = fixtures.copy()
    mapped["mapped_home_team"] = mapped.apply(
        lambda row: resolve_mls_cup_team_name(
            row.get("home_team", ""), row.get("competition", ""), mapping, context
        ),
        axis=1,
    )
    mapped["mapped_away_team"] = mapped.apply(
        lambda row: resolve_mls_cup_team_name(
            row.get("away_team", ""), row.get("competition", ""), mapping, context
        ),
        axis=1,
    )
    return mapped


def predict_mls_cup_fixture(row, context):
    model_row = row.copy()
    cup_competition = str(row.get("competition", "")).strip()
    model_row["competition"] = upcoming.MLS_COMPETITION_NAME
    pred = upcoming.predict_fixture(model_row, context)
    if pred is None:
        return None

    match_date = upcoming.parse_match_date(row.get("match_date"))
    if match_date is None:
        return None
    pred["competition"] = cup_competition
    pred["prediction_key"] = upcoming.make_prediction_key(
        match_date,
        cup_competition,
        pred["home_team"],
        pred["away_team"],
    )
    pred["probability_reasoning"] = pred["probability_reasoning"].replace(
        "trained MLS model", "trained MLS model for MLS cup fixture"
    )
    return pred


def main():
    args = parse_cli_args()
    if args.refresh_download:
        upcoming.download_latest.main()

    fixtures = load_upcoming_mls_cup_fixtures(args.window_days)
    if fixtures.empty:
        print("No upcoming MLS cup fixtures returned by available sources.")
        return

    context = upcoming.build_prediction_context()
    team_mapping = upcoming.load_shared_mapping()
    team_mapping, canonical_added = upcoming.ensure_canonical_self_mappings(team_mapping, context)
    team_mapping, new_map_entries, blanks_added = update_mls_cup_team_mapping(
        fixtures, context, team_mapping
    )
    upcoming.save_team_mapping(upcoming.TEAM_MAPPING_FILE, team_mapping)
    fixtures = apply_mls_cup_team_mapping(fixtures, team_mapping, context)

    existing = upcoming.load_prediction_store(PREDICTIONS_FILE)
    existing = existing.set_index("prediction_key", drop=False) if not existing.empty else existing

    new_records = []
    skipped = 0
    for _, fixture in fixtures.iterrows():
        pred = predict_mls_cup_fixture(fixture, context)
        if pred is None:
            skipped += 1
            continue
        new_records.append(pred)

    new_df = pd.DataFrame(new_records, columns=upcoming.RESULT_COLUMNS).astype("object")
    if new_df.empty and existing.empty:
        print("No MLS cup predictions were generated.")
        return

    if existing.empty:
        combined = new_df.copy()
    else:
        combined = existing.copy().astype("object")
        for _, row in new_df.iterrows():
            combined.loc[row["prediction_key"]] = row
        combined = combined.reset_index(drop=True)

    combined = upcoming.keep_only_current_fixtures(combined, fixtures)
    results_index = upcoming.load_results_index(upcoming.RAW_DATA_DIR)
    combined, settled_count = upcoming.settle_predictions(combined, results_index)
    combined, removed_completed = upcoming.drop_completed_predictions(combined, results_index)
    combined = upcoming.dedupe_predictions(combined)
    combined = combined[upcoming.RESULT_COLUMNS].sort_values(
        ["match_date", "competition", "home_team", "away_team"]
    )

    os.makedirs(upcoming.PREDICTIONS_DIR, exist_ok=True)
    combined.to_csv(PREDICTIONS_FILE, index=False)

    print(f"Upcoming MLS cup fixtures found: {len(fixtures)}")
    print(f"Team mappings file: {upcoming.TEAM_MAPPING_FILE}")
    print(f"Canonical MLS names added: {canonical_added}")
    print(f"MLS cup mappings added from current pull: {new_map_entries}")
    print(f"New blank mappings needing manual edit: {blanks_added}")
    print(f"Predictions written: {len(new_df)}")
    print(f"Skipped (unmatched team names): {skipped}")
    print(f"Removed completed fixtures from upcoming list: {removed_completed}")
    print(f"Newly settled with real results: {settled_count}")
    print(f"Saved tracking file: {PREDICTIONS_FILE}")


if __name__ == "__main__":
    main()
