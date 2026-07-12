"""
Predict upcoming MLS and Liga MX fixtures — identical workflow to global version.

Reads fixture feeds for CONCACAF club leagues, loads the MLS model cache, and
writes ``upcoming_matchweek_predictions.csv`` with predicted scores and
probabilities.
"""
import argparse
import difflib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import joblib
import pandas as pd

import Download_Latest_Data as download_latest
import Predict_Match as pm


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_DIR = os.path.dirname(BASE_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import season_calendar
import team_mapping_groups as tmg
MLS_FILES_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DATA_DIR = os.path.join(BASE_DIR, "Data", "Raw_Data")
PREDICTIONS_DIR = os.path.join(BASE_DIR, "Data", "Predictions")
PREDICTIONS_FILE = os.path.join(PREDICTIONS_DIR, "upcoming_matchweek_predictions.csv")
SHARED_PREDICTIONS_DIR = PREDICTIONS_DIR
TEAM_MAPPING_FILE = os.path.join(SHARED_PREDICTIONS_DIR, "team_name_mapping_master.json")
FOOTBALL_DATA_API_BASE = "https://api.football-data.org/v4"
ESPN_SCOREBOARD_API = "https://site.api.espn.com/apis/site/v2/sports/soccer/{espn_id}/scoreboard"
EASTERN_TZ = ZoneInfo("America/New_York")

# CONCACAF domestic leagues served by this sub-pipeline.
REGIONAL_ESPN_COMPETITIONS = {
    "United States/MLS": "usa.1",
    "Mexico/Liga MX": "mex.1",
}


def rebuild_model_cache_once():
    """Rebuild the MLS model cache in non-interactive mode."""
    predict_script = os.path.join(MLS_FILES_DIR, "Predict_Match.py")
    proc = subprocess.run(
        [sys.executable, predict_script, "--build-cache-only"],
        cwd=BASE_DIR,
        text=True,
        capture_output=True,
        check=False,
        timeout=3600,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        message = stderr or stdout or f"exit code {proc.returncode}"
        raise RuntimeError(f"Auto-rebuild of MLS model cache failed: {message}")


class AveragedProbaClassifier:
    # Compatibility shim so old model_cache.pkl entries serialized from __main__
    # can be loaded when this script is the entrypoint.
    def __init__(self, models):
        self.models = models
        self.classes_ = models[0].classes_

    def predict_proba(self, X):
        matrices = [model.predict_proba(X) for model in self.models]
        return sum(matrices) / len(matrices)

    def predict(self, X):
        avg = self.predict_proba(X)
        idx = avg.argmax(axis=1)
        return self.classes_[idx]

MLS_COMPETITION_CODE = "MLS"
MLS_COMPETITION_NAME = "United States/MLS"
MANUAL_TEAM_OVERRIDES = {
    "Atlanta United FC": "Atlanta Utd",
    "Atlanta United": "Atlanta Utd",
    "Los Angeles FC": "Los Angeles FC",
    "LAFC": "Los Angeles FC",
    "St. Louis CITY SC": "St. Louis City",
    "St. Louis City SC": "St. Louis City",
    "New York City FC": "New York City",
    "New York Red Bulls": "New York RB",
    "Sporting Kansas City": "Kansas City",
    "Inter Miami CF": "Inter Miami",
    "CF Montréal": "Montreal",
    "CF Montreal": "Montreal",
}
TEAM_KEY_ALIASES = {
    "caosasuna": "osasuna",
    "uslecce": "lecce",
    "borussiadortmund": "dortmund",
    "lafc": "losangelesfc",
    "lagalaxy": "losangelesgalaxy",
    "losangelesfc": "losangelesfc",
    "losangelesgalaxy": "losangelesgalaxy",
    "stlouiscity": "stlouiscity",
    "stlouiscitysc": "stlouiscity",
    "dcunited": "dcunited",
    "newyorkcityfc": "newyorkcity",
    "newyorkredbulls": "newyorkredbulls",
    "sportingkansascity": "kansascity",
    "intermiamicf": "intermiami",
    "cfmontreal": "montreal",
    "atlantaunitedfc": "atlantaunited",
    "atlantaunited": "atlantaunited",
}

RESULT_COLUMNS = [
    "prediction_key",
    "created_at_utc",
    "match_date",
    "match_datetime_utc",
    "competition",
    "home_team",
    "away_team",
    "display_home_team",
    "display_away_team",
    "unmapped_teams",
    "is_neutral_site",
    "schedule_only",
    "prediction_quality",
    "predicted_result",
    "prob_home",
    "prob_draw",
    "prob_away",
    "pred_home_goals",
    "pred_away_goals",
    "pred_home_shots",
    "pred_away_shots",
    "pred_home_sot",
    "pred_away_sot",
    "probability_reasoning",
    "prob_home_goals_0",
    "prob_home_goals_1plus",
    "prob_home_goals_2plus",
    "prob_away_goals_0",
    "prob_away_goals_1plus",
    "prob_away_goals_2plus",
    "prob_both_score",
    "prob_over_1_5",
    "prob_over_2_5",
    "prob_over_3_5",
    "actual_home_goals",
    "actual_away_goals",
    "actual_result",
    "is_correct",
    "settled_at_utc",
]


def parse_cli_args():
    parser = argparse.ArgumentParser(
        description="Predict next matchweek fixtures and track outcomes for later comparison."
    )
    parser.add_argument(
        "--refresh-download",
        action="store_true",
        help="Download the latest raw CSV files first using Download_Latest_Data.py.",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=365,
        help="Lookahead window in days for upcoming fixtures (default: full season). Short windows (<90 days) extend through the next Tuesday.",
    )
    parser.add_argument(
        "--api-token",
        type=str,
        default=os.getenv("FOOTBALL_DATA_API_TOKEN", "").strip(),
        help="football-data.org API token. Defaults to FOOTBALL_DATA_API_TOKEN env var.",
    )
    return parser.parse_args()


def competition_from_rel_path(rel_path):
    return os.path.dirname(rel_path).replace("\\", "/") or "Unknown"


def parse_match_date(value):
    date_value = pd.to_datetime(value, dayfirst=True, format="mixed", errors="coerce")
    if pd.isna(date_value):
        return None
    return date_value.normalize()


def calculate_fixture_window_end(window_days, start_date=None):
    # Anchor the window to today so the pull reflects the current MLS slate.
    today = pd.Timestamp(start_date or datetime.now(UTC).date())
    today = today.normalize()
    window_days = max(0, int(window_days))

    # Full-season windows include every remaining scheduled fixture.
    if window_days >= 90:
        return today + pd.Timedelta(days=window_days)

    min_window_end = today + pd.Timedelta(days=window_days)
    # Short windows extend through the next Tuesday to keep Friday-to-Tuesday blocks together.
    days_to_tuesday = (1 - today.weekday()) % 7
    if days_to_tuesday == 0:
        days_to_tuesday = 7
    next_tuesday = today + pd.Timedelta(days=days_to_tuesday)
    return max(min_window_end, next_tuesday)


def make_prediction_key(match_date, competition, home_team, away_team):
    home_key = normalize_team_key(home_team) or str(home_team).strip().lower()
    away_key = normalize_team_key(away_team) or str(away_team).strip().lower()
    team_pair = sorted([home_key, away_key])
    return (
        f"{match_date.strftime('%Y-%m-%d')}|{competition}|"
        f"{team_pair[0]}|{team_pair[1]}"
    )


def normalize_team_key(name):
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip().replace("&", " and ")
    text = text.replace("'", "").replace(".", " ")
    text = text.replace("-", " ")
    parts = [p for p in text.split() if p]
    stop_words = {
        "fc",
        "cf",
        "ac",
        "ca",
        "afc",
        "us",
        "sc",
        "sv",
        "fk",
        "the",
        "club",
        "de",
        "calcio",
        "team",
        "football",
        "sociedad",
        "city",
        "united",
        "town",
        "athletic",
        "county",
        "albion",
        "wanderers",
        "hotspur",
    }
    parts = [p for p in parts if p not in stop_words]
    key = "".join(parts)
    return TEAM_KEY_ALIASES.get(key, key)


def load_team_mapping(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as file:
            mapping = json.load(file)
    except Exception:
        return {}
    if not isinstance(mapping, dict):
        return {}
    cleaned = {}
    for competition, names in mapping.items():
        if not isinstance(names, dict):
            continue
        cleaned[str(competition)] = {
            str(api_name): str(mapped_name)
            for api_name, mapped_name in names.items()
            if str(api_name).strip()
        }
    return cleaned


def save_team_mapping(path, mapping):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(mapping, file, indent=2, ensure_ascii=False)


def load_shared_mapping():
    return load_team_mapping(TEAM_MAPPING_FILE)


def canonical_names_by_competition(context):
    team_competition_map = context.get("team_competition_map", {})
    available_teams = context.get("available_teams", [])
    by_comp = {}
    for team in available_teams:
        team_name = str(team).strip()
        competition = str(team_competition_map.get(team_name, "")).strip()
        if not team_name or not competition:
            continue
        by_comp.setdefault(competition, set()).add(team_name)
    return by_comp


def ensure_canonical_self_mappings(mapping, context):
    updated = dict(mapping) if isinstance(mapping, dict) else {}
    added = 0
    by_comp = canonical_names_by_competition(context)
    for competition, names in by_comp.items():
        updated.setdefault(competition, {})
        for team_name in names:
            if team_name not in updated[competition]:
                updated[competition][team_name] = team_name
                added += 1
    return updated, added


def resolve_live_team_name(raw_name, competition, context, mapping=None):
    valid_names = tmg.candidate_teams_for_competition(competition, context)

    manual_override = MANUAL_TEAM_OVERRIDES.get(str(raw_name).strip())
    if manual_override:
        if manual_override in valid_names:
            return manual_override
        if manual_override in context["available_teams"]:
            return manual_override

    if mapping is not None:
        canonical, _source = tmg.lookup_mapped_name(raw_name, competition, mapping)
        if canonical and canonical in context["available_teams"]:
            return canonical

    direct = pm.resolve_team_name(raw_name, valid_names)
    if direct:
        return direct

    return tmg.fuzzy_resolve_team_name(raw_name, valid_names)


def update_team_mapping_from_fixtures(fixtures, context, mapping):
    updated = dict(mapping)
    new_entries = 0
    changed_entries = 0
    blanks_added = 0
    by_comp = canonical_names_by_competition(context)

    for _, row in fixtures.iterrows():
        competition = str(row.get("competition", "")).strip()
        if not competition:
            continue
        updated.setdefault(competition, {})
        canonical_names = by_comp.get(competition, set())

        for side_col in ["home_team", "away_team"]:
            api_name = str(row.get(side_col, "")).strip()
            if not api_name:
                continue
            resolved = resolve_live_team_name(api_name, competition, context, mapping=updated)
            target = resolved if resolved else ""
            existing = str(updated[competition].get(api_name, "")).strip()
            if not existing:
                tmg.store_team_mapping(
                    updated,
                    competition,
                    api_name,
                    target if target else (api_name if api_name in canonical_names else ""),
                    propagate_country=True,
                    propagate_international=tmg.is_international_competition(competition),
                )
                new_entries += 1
                if not target and api_name not in canonical_names:
                    blanks_added += 1
            elif existing != target and target:
                tmg.store_team_mapping(
                    updated,
                    competition,
                    api_name,
                    target,
                    propagate_country=True,
                    propagate_international=tmg.is_international_competition(competition),
                )
                changed_entries += 1
            elif not target and api_name in canonical_names:
                tmg.store_team_mapping(updated, competition, api_name, api_name, propagate_country=True)

    return updated, new_entries, changed_entries, blanks_added


def apply_team_mapping_to_fixtures(fixtures, mapping, context):
    mapped = fixtures.copy()
    known_teams = set(context.get("available_teams", []))

    def mapped_name(competition, api_name):
        competition = str(competition).strip()
        api_name = str(api_name).strip()
        canonical, _source = tmg.lookup_mapped_name(api_name, competition, mapping)
        if canonical and canonical in known_teams:
            return canonical
        resolved = resolve_live_team_name(api_name, competition, context, mapping=mapping)
        if resolved:
            return resolved
        if api_name in known_teams:
            return api_name
        return ""

    mapped["mapped_home_team"] = mapped.apply(
        lambda row: mapped_name(row.get("competition", ""), row.get("home_team", "")),
        axis=1,
    )
    mapped["mapped_away_team"] = mapped.apply(
        lambda row: mapped_name(row.get("competition", ""), row.get("away_team", "")),
        axis=1,
    )
    mapped["display_home_team"] = mapped["home_team"]
    mapped["display_away_team"] = mapped["away_team"]
    return mapped


def latest_season_for_competition(season_teams, competition, fallback):
    best_key = None
    best_year = -1
    for season_key in season_teams.keys():
        if not str(season_key).startswith(f"{competition}/"):
            continue
        year = pm.parse_start_year_from_key(season_key)
        if year > best_year:
            best_year = year
            best_key = season_key
    return best_key or fallback


def choose_prediction_season(home_team, away_team, competition, match_date, season_teams, fallback_season):
    """Prefer the latest competition season for upcoming fixtures, like European pre-season logic."""
    competition_latest = latest_season_for_competition(season_teams, competition, fallback_season)
    fixture_year = -1
    if match_date is not None:
        try:
            fixture_year = int(pd.Timestamp(match_date).year)
        except Exception:
            fixture_year = -1
    if fixture_year > 0:
        competition_latest = season_calendar.season_key_for_fixture_year(
            competition, competition_latest, fixture_year
        )
    if home_team in season_teams.get(competition_latest, {}) and away_team in season_teams.get(
        competition_latest, {}
    ):
        return competition_latest
    return pm.choose_season_for_teams(home_team, away_team, season_teams, competition_latest)


def _csv_fixture_is_played(row):
    res = str(row.get("Res", row.get("FTR", ""))).strip().upper()
    hg = row.get("HG", row.get("FTHG"))
    ag = row.get("AG", row.get("FTAG"))
    if res in {"H", "D", "A"}:
        return True
    try:
        return pd.notna(hg) and pd.notna(ag) and float(hg) >= 0 and float(ag) >= 0
    except (TypeError, ValueError):
        return False


def mean_from_dicts(rows, key, default=0.0):
    if not rows:
        return float(default)
    values = []
    for row in rows:
        value = row.get(key, default)
        if pm.is_invalid_stat_value(value):
            continue
        values.append(float(value))
    if not values:
        return float(default)
    return float(sum(values) / len(values))


def inject_fallback_team(team_name, competition, season_key, context):
    overall_teams = context["overall_teams"]
    season_teams = context["season_teams"]
    current_form = context["current_form"]
    team_competition_map = context["team_competition_map"]
    available_teams = context["available_teams"]

    if team_name in overall_teams:
        team_competition_map[team_name] = competition
        if team_name not in available_teams:
            available_teams.append(team_name)
        if team_name not in season_teams.get(season_key, {}):
            season_teams.setdefault(season_key, {})[team_name] = {"games": 0, "points": 0}
        return

    comp_teams = [t for t, comp in team_competition_map.items() if comp == competition]
    comp_overall_rows = [overall_teams[t] for t in comp_teams if t in overall_teams]
    season_rows = list(season_teams.get(season_key, {}).values()) if season_key in season_teams else []

    overall_teams[team_name] = {
        "games": max(1, int(round(mean_from_dicts(comp_overall_rows, "games", 30)))),
        "goals_scored": mean_from_dicts(comp_overall_rows, "goals_scored", 40.0),
        "goals_conceded": mean_from_dicts(comp_overall_rows, "goals_conceded", 40.0),
        "home_games": max(1, int(round(mean_from_dicts(comp_overall_rows, "home_games", 15)))),
        "away_games": max(1, int(round(mean_from_dicts(comp_overall_rows, "away_games", 15)))),
        "home_goals_scored": mean_from_dicts(comp_overall_rows, "home_goals_scored", 20.0),
        "away_goals_scored": mean_from_dicts(comp_overall_rows, "away_goals_scored", 20.0),
        "avg_goals_scored": mean_from_dicts(comp_overall_rows, "avg_goals_scored", 1.35),
        "avg_goals_conceded": mean_from_dicts(comp_overall_rows, "avg_goals_conceded", 1.35),
        "avg_home_goals_scored": mean_from_dicts(comp_overall_rows, "avg_home_goals_scored", 1.45),
        "avg_home_goals_conceded": mean_from_dicts(comp_overall_rows, "avg_home_goals_conceded", 1.20),
        "avg_away_goals_scored": mean_from_dicts(comp_overall_rows, "avg_away_goals_scored", 1.20),
        "avg_away_goals_conceded": mean_from_dicts(comp_overall_rows, "avg_away_goals_conceded", 1.45),
        "avg_home_shots_for": mean_from_dicts(comp_overall_rows, "avg_home_shots_for", 12.0),
        "avg_home_shots_against": mean_from_dicts(comp_overall_rows, "avg_home_shots_against", 12.0),
        "avg_away_shots_for": mean_from_dicts(comp_overall_rows, "avg_away_shots_for", 10.5),
        "avg_away_shots_against": mean_from_dicts(comp_overall_rows, "avg_away_shots_against", 12.5),
        "weighted_avg_goals_scored": mean_from_dicts(comp_overall_rows, "weighted_avg_goals_scored", 1.35),
    }

    season_teams.setdefault(season_key, {})
    season_teams[season_key][team_name] = {
        "games": max(1, int(round(mean_from_dicts(season_rows, "games", 20)))),
        "points": mean_from_dicts(season_rows, "points", 28.0),
        "avg_goals_scored": mean_from_dicts(season_rows, "avg_goals_scored", 1.30),
        "avg_goals_conceded": mean_from_dicts(season_rows, "avg_goals_conceded", 1.30),
        "avg_home_goals_scored": mean_from_dicts(season_rows, "avg_home_goals_scored", 1.45),
        "avg_home_goals_conceded": mean_from_dicts(season_rows, "avg_home_goals_conceded", 1.20),
        "avg_away_goals_scored": mean_from_dicts(season_rows, "avg_away_goals_scored", 1.15),
        "avg_away_goals_conceded": mean_from_dicts(season_rows, "avg_away_goals_conceded", 1.50),
        "avg_home_shots_for": mean_from_dicts(season_rows, "avg_home_shots_for", 12.0),
        "avg_home_shots_against": mean_from_dicts(season_rows, "avg_home_shots_against", 12.0),
        "avg_away_shots_for": mean_from_dicts(season_rows, "avg_away_shots_for", 10.5),
        "avg_away_shots_against": mean_from_dicts(season_rows, "avg_away_shots_against", 12.5),
    }

    current_form.setdefault("teams", {})
    current_form["teams"].setdefault(
        team_name,
        {
            "points_last_10": 12.0,
            "wins_last_10": 3.0,
            "losses_last_10": 3.0,
            "avg_goals_for_last_10": 1.2,
            "avg_goals_against_last_10": 1.2,
            "previous_match_win_odds": 2.8,
            "previous_match_draw_odds": 3.3,
            "previous_match_lose_odds": 2.8,
        },
    )

    team_competition_map[team_name] = competition
    if team_name not in available_teams:
        available_teams.append(team_name)


def find_latest_season_file_per_competition(raw_dir):
    latest = {}
    for root, _, files in os.walk(raw_dir):
        for name in files:
            if not name.endswith(".csv"):
                continue
            start_year = pm.parse_season_start_year(name)
            if start_year is None:
                continue
            full_path = os.path.join(root, name)
            rel_path = os.path.relpath(full_path, raw_dir)
            competition = competition_from_rel_path(rel_path)
            current = latest.get(competition)
            if current is None or start_year > current[0]:
                latest[competition] = (start_year, rel_path)
    return {competition: rel_path for competition, (_, rel_path) in latest.items()}


def fetch_json(url, headers=None, timeout=30):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload)


def load_upcoming_fixtures_from_csv_source(source_df, competition_name, window_days, today):
    needed = {"Date", "Home", "Away"}
    if not needed.issubset(source_df.columns):
        return pd.DataFrame()

    frame = source_df.copy()
    frame["match_date"] = pd.to_datetime(frame["Date"], dayfirst=False, format="mixed", errors="coerce").dt.normalize()
    frame = frame[frame["match_date"].notna()]
    frame = frame[frame["Home"].notna() & frame["Away"].notna()]
    frame = frame[~frame.apply(_csv_fixture_is_played, axis=1)]
    if frame.empty:
        return pd.DataFrame()

    future = frame[frame["match_date"] >= today]
    if future.empty:
        future = frame.copy()

    frame = future.rename(columns={"Home": "home_team", "Away": "away_team"})
    frame["competition"] = competition_name
    fixtures = frame[["match_date", "competition", "home_team", "away_team"]].copy()
    fixtures["match_datetime_et"] = None
    fixtures = fixtures.sort_values(["match_date", "home_team", "away_team"]).reset_index(drop=True)
    return season_calendar.filter_fixtures_to_bounds(
        fixtures,
        competition_name,
        reference_date=today,
    )


def load_upcoming_matchweek_fixtures_from_raw_season_files(raw_dir, window_days):
    """Load unplayed fixtures from the latest per-competition raw season CSV files."""
    latest = find_latest_season_file_per_competition(raw_dir)
    if not latest:
        return pd.DataFrame()

    today = pd.Timestamp(datetime.now(UTC).date())
    rows = []
    for competition, rel_path in sorted(latest.items()):
        full_path = os.path.join(raw_dir, rel_path)
        try:
            frame = pd.read_csv(full_path)
        except Exception:
            try:
                frame = pd.read_csv(full_path, encoding="latin-1", engine="python", on_bad_lines="skip")
            except Exception:
                continue
        fixtures = load_upcoming_fixtures_from_csv_source(frame, competition, window_days, today)
        if not fixtures.empty:
            rows.extend(fixtures.to_dict("records"))

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(
        ["match_date", "competition", "home_team", "away_team"]
    ).reset_index(drop=True)


def load_upcoming_matchweek_fixtures_from_csv_fallback(window_days):
    today = pd.Timestamp(datetime.now(UTC).date())
    frames = []

    try:
        mls_source = download_latest.fetch_source_dataframe()
        mls_fixtures = load_upcoming_fixtures_from_csv_source(
            mls_source, MLS_COMPETITION_NAME, window_days, today
        )
        if not mls_fixtures.empty:
            frames.append(mls_fixtures)
    except Exception:
        pass

    try:
        import Download_Mexico_Data as download_mexico

        mex_source = download_mexico.fetch_source_dataframe()
        mex_fixtures = load_upcoming_fixtures_from_csv_source(
            mex_source, "Mexico/Liga MX", window_days, today
        )
        if not mex_fixtures.empty:
            frames.append(mex_fixtures)
    except Exception:
        pass

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(
        ["match_date", "competition", "home_team", "away_team"]
    ).reset_index(drop=True)


def load_upcoming_matchweek_fixtures_from_espn(
    window_days, lookahead_days=365, competition_names=None
):
    today = pd.Timestamp(datetime.now(UTC).date())
    rows = []
    seen = set()

    for competition_name, espn_id in REGIONAL_ESPN_COMPETITIONS.items():
        if competition_names and competition_name not in set(competition_names):
            continue
        end = today + pd.Timedelta(days=max(1, min(int(lookahead_days), 365)))
        url = (
            ESPN_SCOREBOARD_API.format(espn_id=espn_id)
            + f"?dates={today.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}&limit=1000"
        )
        try:
            data = fetch_json(url, timeout=30)
        except Exception:
            continue

        events = data.get("events", [])
        if not isinstance(events, list):
            continue

        for event in events:
            event_date = pd.to_datetime(event.get("date"), utc=True, errors="coerce")
            if pd.isna(event_date):
                continue
            event_dt_et = event_date.tz_convert(EASTERN_TZ)
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

            competitors = comp0.get("competitors", [])
            home_team = ""
            away_team = ""
            for c in competitors:
                team_name = ((c.get("team") or {}).get("displayName") or "").strip()
                side = str(c.get("homeAway", "")).strip().lower()
                if side == "home":
                    home_team = team_name
                elif side == "away":
                    away_team = team_name
            if not home_team or not away_team:
                continue

            key = (competition_name, match_date.strftime("%Y-%m-%d"), home_team, away_team)
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

    fixtures = fixtures.sort_values(["match_date", "competition", "home_team", "away_team"]).reset_index(drop=True)
    bounded_frames = []
    for competition_name in fixtures["competition"].dropna().unique():
        comp_frame = fixtures[fixtures["competition"] == competition_name]
        bounded_frames.append(
            season_calendar.filter_fixtures_to_bounds(
                comp_frame,
                str(competition_name),
                reference_date=today,
            )
        )
    if not bounded_frames:
        return fixtures.iloc[0:0].copy()
    return pd.concat(bounded_frames, ignore_index=True).sort_values(
        ["match_date", "competition", "home_team", "away_team"]
    ).reset_index(drop=True)


def load_upcoming_matchweek_fixtures_from_api(api_token, window_days):
    today = pd.Timestamp(datetime.now(UTC).date())
    headers = {"X-Auth-Token": api_token}
    date_params = season_calendar.football_data_api_date_params(
        MLS_COMPETITION_NAME,
        reference_date=today,
    )
    query = urllib.parse.urlencode({"status": "SCHEDULED", **date_params}, doseq=True)
    url = f"{FOOTBALL_DATA_API_BASE}/competitions/{MLS_COMPETITION_CODE}/matches?{query}"

    try:
        data = fetch_json(url, headers=headers, timeout=45)
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            print(
                f"football-data.org returned {error.code} for MLS. "
                "Falling back to football-data.co.uk / ESPN sources."
            )
            return pd.DataFrame()
        raise RuntimeError(f"Could not fetch MLS fixtures from API (HTTP {error.code}).") from error
    except Exception as error:
        print(f"Could not fetch MLS fixtures from API ({error}). Falling back to football-data.co.uk source.")
        return load_upcoming_matchweek_fixtures_from_csv_fallback(window_days)

    matches = data.get("matches", [])
    if not isinstance(matches, list) or not matches:
        return pd.DataFrame()

    rows = []
    for match in matches:
        home_team = ((match.get("homeTeam") or {}).get("name") or "").strip()
        away_team = ((match.get("awayTeam") or {}).get("name") or "").strip()
        utc_date = match.get("utcDate")
        if not home_team or not away_team or not utc_date:
            continue

        parsed = pd.to_datetime(utc_date, utc=True, errors="coerce")
        if pd.isna(parsed):
            continue

        match_dt_et = parsed.tz_convert(EASTERN_TZ)
        match_date = match_dt_et.tz_localize(None).normalize()
        if match_date < today:
            continue

        rows.append(
            {
                "match_date": match_date,
                "match_datetime_et": match_dt_et.isoformat(),
                "competition": MLS_COMPETITION_NAME,
                "home_team": home_team,
                "away_team": away_team,
            }
        )

    fixtures = pd.DataFrame(rows)
    if fixtures.empty:
        return fixtures

    fixtures = fixtures.sort_values(["match_date", "home_team", "away_team"]).reset_index(drop=True)
    return season_calendar.filter_fixtures_to_bounds(
        fixtures,
        MLS_COMPETITION_NAME,
        reference_date=today,
    )


def dedupe_fixtures(fixtures: pd.DataFrame) -> pd.DataFrame:
    if fixtures.empty:
        return fixtures

    frame = fixtures.copy()
    frame["_fixture_key"] = frame.apply(
        lambda row: (
            str(row.get("competition", "")).strip(),
            pd.Timestamp(row.get("match_date")).strftime("%Y-%m-%d"),
            normalize_team_key(row.get("home_team", "")),
            normalize_team_key(row.get("away_team", "")),
        ),
        axis=1,
    )
    frame = frame.sort_values("match_datetime_et", na_position="last")
    frame = frame.drop_duplicates(subset=["_fixture_key"], keep="last")
    return frame.drop(columns=["_fixture_key"]).reset_index(drop=True)


def filter_liga_mx_active_tournament(fixtures: pd.DataFrame) -> pd.DataFrame:
    """Keep only Liga MX fixtures from the active Apertura/Clausura tournament."""
    if fixtures.empty:
        return fixtures

    active = season_calendar.active_liga_mx_tournament_label()
    if not active:
        return fixtures

    keep_mask = []
    for _, row in fixtures.iterrows():
        competition = str(row.get("competition", "")).strip()
        if competition != "Mexico/Liga MX":
            keep_mask.append(True)
            continue
        label = season_calendar.liga_mx_tournament_label_for_date(row.get("match_date"))
        keep_mask.append(label == active)

    return fixtures[keep_mask].reset_index(drop=True)


def load_upcoming_matchweek_fixtures(api_token, window_days):
    # football-data.org supplies MLS only. Always supplement it with ESPN
    # Liga MX fixtures instead of returning early and silently dropping Liga MX.
    if api_token:
        mls_fixtures = load_upcoming_matchweek_fixtures_from_api(api_token, window_days)
        if not mls_fixtures.empty:
            mexico_fixtures = load_upcoming_matchweek_fixtures_from_espn(
                window_days,
                lookahead_days=window_days,
                competition_names={"Mexico/Liga MX"},
            )
            if mexico_fixtures.empty:
                fallback = load_upcoming_matchweek_fixtures_from_csv_fallback(window_days)
                if not fallback.empty:
                    mexico_fixtures = fallback[
                        fallback["competition"].astype(str).eq("Mexico/Liga MX")
                    ].copy()
            frames = [mls_fixtures]
            if not mexico_fixtures.empty:
                frames.append(mexico_fixtures)
            fixtures = pd.concat(frames, ignore_index=True)
            fixtures = fixtures.drop_duplicates(
                subset=["match_date", "competition", "home_team", "away_team"],
                keep="first",
            ).sort_values(["match_date", "competition", "home_team", "away_team"])
            print("Fixture sources: football-data.org MLS + ESPN/CSV Liga MX")
            return fixtures.reset_index(drop=True)

    fixtures = load_upcoming_matchweek_fixtures_from_espn(
        window_days, lookahead_days=window_days
    )
    if not fixtures.empty:
        print("Fixture source: ESPN scoreboard API")
        return fixtures

    fixtures = pd.concat(frames, ignore_index=True)
    fixtures = dedupe_fixtures(fixtures)
    fixtures = filter_liga_mx_active_tournament(fixtures)
    for source in sources:
        print(f"Fixture source: {source}")
    return fixtures


def load_prediction_store(path):
    if not os.path.exists(path):
        return pd.DataFrame(columns=RESULT_COLUMNS)
    frame = pd.read_csv(path)
    for col in RESULT_COLUMNS:
        if col not in frame.columns:
            frame[col] = ""
    frame = frame[RESULT_COLUMNS].copy()
    return frame.astype("object")


def load_results_index(raw_dir):
    results = {}
    for root, _, files in os.walk(raw_dir):
        for name in files:
            if not name.endswith(".csv"):
                continue
            start_year = pm.parse_season_start_year(name)
            if start_year is None:
                continue

            full_path = os.path.join(root, name)
            rel_path = os.path.relpath(full_path, raw_dir)
            competition = competition_from_rel_path(rel_path)

            try:
                frame = pd.read_csv(full_path)
            except Exception:
                frame = pd.read_csv(full_path, encoding="latin-1", engine="python", on_bad_lines="skip")

            needed = {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"}
            if not needed.issubset(frame.columns):
                continue

            frame = frame[frame["FTR"].astype(str).str.strip().isin({"H", "D", "A"})]
            frame = frame[frame["HomeTeam"].notna() & frame["AwayTeam"].notna()]
            if frame.empty:
                continue

            date_parsed = pd.to_datetime(frame["Date"], dayfirst=True, format="mixed", errors="coerce").dt.normalize()
            frame = frame[date_parsed.notna()]
            date_parsed = date_parsed.loc[frame.index]

            for idx, row in frame.iterrows():
                match_date = date_parsed.loc[idx]
                key = make_prediction_key(match_date, competition, row["HomeTeam"], row["AwayTeam"])
                results[key] = {
                    "actual_home_goals": int(row["FTHG"]),
                    "actual_away_goals": int(row["FTAG"]),
                    "actual_result": str(row["FTR"]).strip(),
                }
    return results


def build_prediction_context():
    matches, season_files = pm.load_training_matches(pm.PROCESSED_DIR)

    if not os.path.exists(pm.MODEL_CACHE):
        print("[model-cache] cache missing; rebuilding model cache...")
        rebuild_model_cache_once()

    try:
        bundle = joblib.load(pm.MODEL_CACHE)
    except Exception as exc:
        print(f"[model-cache] failed to load cache ({exc.__class__.__name__}); rebuilding...")
        rebuild_model_cache_once()
        bundle = joblib.load(pm.MODEL_CACHE)

    fingerprint = pm.data_fingerprint(season_files)
    if bundle.get("fingerprint") != fingerprint:
        print("[model-cache] using cached models (data newer than cache; full retrain runs Tue/Fri)")

    required_keys = {
        "clf",
        "result_label_encoder",
        "home_goal_reg",
        "away_goal_reg",
        "home_shot_reg",
        "away_shot_reg",
        "home_sot_reg",
        "away_sot_reg",
        "train_columns",
        "goal_prob_models",
    }
    missing = sorted(k for k in required_keys if k not in bundle)
    if missing:
        print(f"[model-cache] cache missing required fields ({', '.join(missing)}); rebuilding...")
        if os.path.exists(pm.MODEL_CACHE):
            os.remove(pm.MODEL_CACHE)
        rebuild_model_cache_once()
        bundle = joblib.load(pm.MODEL_CACHE)

    overall_teams = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "overall_teams.json"))
    season_teams = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "season_teams.json"))
    head_to_head = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "head_to_head.json"))
    current_form = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "current_form.json"))
    league_strength = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "league_strength.json")) or {}
    market_value_data = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "mls_squad_values.json")) or {}
    dynamic_form = pm.build_dynamic_form_from_matches(matches)

    if (
        overall_teams is None
        or season_teams is None
        or head_to_head is None
        or current_form is None
        or not isinstance(overall_teams, dict)
        or len(overall_teams) == 0
    ):
        overall_teams, season_teams, head_to_head, current_form = pm.build_fallback_data(matches, season_files)

    overall_teams = pm.replace_nan_with_sentinel(overall_teams)
    season_teams = pm.replace_nan_with_sentinel(season_teams)
    head_to_head = pm.replace_nan_with_sentinel(head_to_head)
    current_form = pm.replace_nan_with_sentinel(current_form)
    league_strength = pm.replace_nan_with_sentinel(league_strength)
    market_value_data = pm.replace_nan_with_sentinel(market_value_data)

    if not isinstance(current_form, dict):
        current_form = {"teams": {}}
    if "teams" not in current_form or not isinstance(current_form["teams"], dict):
        current_form["teams"] = {}
    current_form["teams"].update(dynamic_form)
    clf = bundle["clf"]
    result_label_encoder = bundle["result_label_encoder"]
    home_goal_reg = bundle["home_goal_reg"]
    away_goal_reg = bundle["away_goal_reg"]
    home_shot_reg = bundle["home_shot_reg"]
    away_shot_reg = bundle["away_shot_reg"]
    home_sot_reg = bundle["home_sot_reg"]
    away_sot_reg = bundle["away_sot_reg"]
    goal_prob_models = bundle["goal_prob_models"]
    train_columns = bundle["train_columns"]

    team_competition_map = {}
    for _, row in matches.iterrows():
        team_competition_map[row["HomeTeam"]] = row["competition"]
        team_competition_map[row["AwayTeam"]] = row["competition"]

    available_teams = sorted(set(matches["HomeTeam"].dropna()) | set(matches["AwayTeam"].dropna()))
    latest_season = season_files[-1].replace(".csv", "")
    latest_start_year = max(pm.parse_start_year_from_key(key) for key in season_teams.keys())

    return {
        "clf": clf,
        "result_label_encoder": result_label_encoder,
        "home_goal_reg": home_goal_reg,
        "away_goal_reg": away_goal_reg,
        "home_shot_reg": home_shot_reg,
        "away_shot_reg": away_shot_reg,
        "home_sot_reg": home_sot_reg,
        "away_sot_reg": away_sot_reg,
        "goal_prob_models": goal_prob_models,
        "overall_teams": overall_teams,
        "season_teams": season_teams,
        "head_to_head": head_to_head,
        "current_form": current_form,
        "league_strength": league_strength,
        "market_value_data": market_value_data,
        "train_columns": train_columns,
        "team_competition_map": team_competition_map,
        "available_teams": available_teams,
        "latest_season": latest_season,
        "latest_start_year": latest_start_year,
    }


def predict_fixture(row, context):
    raw_home = row["home_team"]
    raw_away = row["away_team"]
    competition = row["competition"]
    match_date = row["match_date"]
    match_datetime_utc = str(row.get("match_datetime_utc", "")).strip()
    match_datetime_et = str(row.get("match_datetime_et", "")).strip()

    if pm.is_placeholder_team(raw_home) or pm.is_placeholder_team(raw_away):
        return None

    home_team = str(row.get("mapped_home_team", "")).strip()
    away_team = str(row.get("mapped_away_team", "")).strip()
    if not home_team or not away_team or home_team == away_team:
        return None

    season_teams = context["season_teams"]
    competition_season = latest_season_for_competition(season_teams, competition, context["latest_season"])
    prediction_season = choose_prediction_season(
        home_team,
        away_team,
        competition,
        match_date,
        season_teams,
        competition_season,
    )
    inject_fallback_team(home_team, competition, prediction_season, context)
    inject_fallback_team(away_team, competition, prediction_season, context)
    prediction_start_year = pm.parse_start_year_from_key(prediction_season)
    season_coeff = pm.season_recency_coefficient(context["latest_start_year"], prediction_start_year)
    home_comp = context["team_competition_map"].get(home_team, competition)
    away_comp = context["team_competition_map"].get(away_team, competition)

    match_input = pm.build_match_input(home_team, away_team)
    X_match = pm.build_features(
        match_input,
        prediction_season,
        competition,
        season_coeff,
        context["overall_teams"],
        context["season_teams"],
        context["head_to_head"],
        context["current_form"],
        context["league_strength"],
        home_competition_override=home_comp,
        away_competition_override=away_comp,
    )
    X_match = pd.get_dummies(X_match, columns=["competition"], dtype=float)
    X_match = X_match.reindex(columns=context["train_columns"], fill_value=0.0)

    probabilities = {"H": 0.0, "D": 0.0, "A": 0.0}
    proba_values = context["clf"].predict_proba(X_match)[0]
    for idx, encoded_label in enumerate(context["clf"].classes_):
        label = context["result_label_encoder"].inverse_transform([encoded_label])[0]
        probabilities[label] = float(proba_values[idx])

    home_league_strength = float(context.get("league_strength", {}).get(home_comp, 0.85))
    away_league_strength = float(context.get("league_strength", {}).get(away_comp, 0.85))
    probabilities, _, league_direction_shift = pm.apply_league_strength_adjustment(
        probabilities, home_league_strength, away_league_strength
    )

    home_adv_shift = pm.mls_home_advantage_shift(home_team, prediction_season, context["season_teams"])
    transfer = min(home_adv_shift, probabilities.get("A", 0.0))
    probabilities["H"] = max(0.0, probabilities.get("H", 0.0) + transfer)
    probabilities["A"] = max(0.0, probabilities.get("A", 0.0) - transfer)
    total_prob = probabilities.get("H", 0.0) + probabilities.get("D", 0.0) + probabilities.get("A", 0.0)
    if total_prob > 0:
        probabilities["H"] /= total_prob
        probabilities["D"] /= total_prob
        probabilities["A"] /= total_prob

    market_shift, _, _ = pm.market_value_probability_shift(
        home_team, away_team, context.get("market_value_data", {})
    )
    if market_shift != 0.0:
        if market_shift > 0:
            transfer = min(market_shift, probabilities.get("A", 0.0))
            probabilities["H"] += transfer
            probabilities["A"] -= transfer
        else:
            transfer = min(abs(market_shift), probabilities.get("H", 0.0))
            probabilities["A"] += transfer
            probabilities["H"] -= transfer
        total_prob = probabilities.get("H", 0.0) + probabilities.get("D", 0.0) + probabilities.get("A", 0.0)
        if total_prob > 0:
            probabilities["H"] /= total_prob
            probabilities["D"] /= total_prob
            probabilities["A"] /= total_prob
    probabilities = pm.apply_home_advantage_boost(probabilities)
    probabilities = pm.reduce_draw_probability(probabilities)
    seed = pm.prediction_randomizer_seed(home_team, away_team, competition, prediction_season)
    probabilities = pm.apply_probability_randomizer(
        probabilities,
        pm.MLS_RANDOMIZER_MAX_DELTA,
        seed=seed,
    )

    prediction = max(probabilities, key=probabilities.get)
    pred_home_goals = max(0.0, float(context["home_goal_reg"].predict(X_match)[0]))
    pred_away_goals = max(0.0, float(context["away_goal_reg"].predict(X_match)[0]))

    # Compute Poisson-based goal probabilities from raw expected goals
    goal_probs = pm.predict_goal_probabilities(X_match, context["goal_prob_models"])

    # Align predicted scores to match the most likely result
    aligned_home, aligned_away = pm.align_predicted_score(pred_home_goals, pred_away_goals, prediction)

    pred_home_shots = max(0.0, float(context["home_shot_reg"].predict(X_match)[0]))
    pred_away_shots = max(0.0, float(context["away_shot_reg"].predict(X_match)[0]))
    pred_home_sot = max(0.0, float(context["home_sot_reg"].predict(X_match)[0]))
    pred_away_sot = max(0.0, float(context["away_sot_reg"].predict(X_match)[0]))

    key = make_prediction_key(match_date, competition, home_team, away_team)
    display_home = str(row.get("display_home_team", row.get("home_team", ""))).strip()
    display_away = str(row.get("display_away_team", row.get("away_team", ""))).strip()
    return {
        "prediction_key": key,
        "created_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "match_date": match_date.strftime("%Y-%m-%d"),
        "match_datetime_utc": match_datetime_utc,
        "competition": competition,
        "home_team": home_team,
        "away_team": away_team,
        "display_home_team": display_home,
        "display_away_team": display_away,
        "unmapped_teams": "",
        "is_neutral_site": "0",
        "schedule_only": "0",
        "prediction_quality": "prediction",
        "predicted_result": prediction,
        "prob_home": round(probabilities["H"], 6),
        "prob_draw": round(probabilities["D"], 6),
        "prob_away": round(probabilities["A"], 6),
        "pred_home_goals": aligned_home,
        "pred_away_goals": aligned_away,
        "pred_home_shots": round(pred_home_shots, 3),
        "pred_away_shots": round(pred_away_shots, 3),
        "pred_home_sot": round(pred_home_sot, 3),
        "pred_away_sot": round(pred_away_sot, 3),
        "probability_reasoning": "",
        **goal_probs,
        "actual_home_goals": None,
        "actual_away_goals": None,
        "actual_result": None,
        "is_correct": None,
        "settled_at_utc": None,
    }


def build_schedule_only_row(row, context=None):
    raw_home = str(row.get("home_team", "")).strip()
    raw_away = str(row.get("away_team", "")).strip()
    competition = str(row.get("competition", "")).strip()
    match_date = row.get("match_date")
    if match_date is None or pd.isna(match_date):
        return None
    match_date = pd.Timestamp(match_date).normalize()
    mapped_home = str(row.get("mapped_home_team", "")).strip()
    mapped_away = str(row.get("mapped_away_team", "")).strip()
    home_team = mapped_home or raw_home
    away_team = mapped_away or raw_away
    if not home_team or not away_team:
        home_team = raw_home
        away_team = raw_away
    if not home_team or not away_team or home_team == away_team:
        return None
    key = make_prediction_key(match_date, competition, home_team, away_team)
    unmapped = []
    if not mapped_home and raw_home:
        unmapped.append(raw_home)
    if not mapped_away and raw_away:
        unmapped.append(raw_away)
    return {
        "prediction_key": key,
        "created_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "match_date": match_date.strftime("%Y-%m-%d"),
        "match_datetime_utc": str(row.get("match_datetime_utc", "")).strip(),
        "competition": competition,
        "home_team": home_team,
        "away_team": away_team,
        "display_home_team": raw_home,
        "display_away_team": raw_away,
        "is_neutral_site": "0",
        "unmapped_teams": ",".join(unmapped) if unmapped else "",
        "schedule_only": "1",
        "prediction_quality": "no_prediction",
        "predicted_result": "",
        "probability_reasoning": "Teams are not in the model database — fixture listed without odds.",
        "prob_home": 0.0,
        "prob_draw": 0.0,
        "prob_away": 0.0,
        "pred_home_goals": None,
        "pred_away_goals": None,
        "pred_home_shots": None,
        "pred_away_shots": None,
        "pred_home_sot": None,
        "pred_away_sot": None,
        "prob_home_goals_0": None,
        "prob_home_goals_1plus": None,
        "prob_home_goals_2plus": None,
        "prob_away_goals_0": None,
        "prob_away_goals_1plus": None,
        "prob_away_goals_2plus": None,
        "prob_both_score": None,
        "prob_over_1_5": None,
        "prob_over_2_5": None,
        "prob_over_3_5": None,
        "actual_home_goals": None,
        "actual_away_goals": None,
        "actual_result": None,
        "is_correct": None,
        "settled_at_utc": None,
    }


def settle_predictions(predictions_df, results_index):
    if predictions_df.empty:
        return predictions_df, 0

    settled_count = 0
    now_utc = datetime.now(UTC).replace(microsecond=0).isoformat()

    for idx, row in predictions_df.iterrows():
        key = row["prediction_key"]
        if not isinstance(key, str) or key not in results_index:
            continue
        if str(row.get("actual_result", "")).strip() in {"H", "D", "A"}:
            continue

        result = results_index[key]
        predictions_df.at[idx, "actual_home_goals"] = result["actual_home_goals"]
        predictions_df.at[idx, "actual_away_goals"] = result["actual_away_goals"]
        predictions_df.at[idx, "actual_result"] = result["actual_result"]
        predictions_df.at[idx, "is_correct"] = (
            "1" if str(row.get("predicted_result", "")).strip() == result["actual_result"] else "0"
        )
        predictions_df.at[idx, "settled_at_utc"] = now_utc
        settled_count += 1

    return predictions_df, settled_count


def drop_completed_predictions(predictions_df, results_index):
    if predictions_df.empty:
        return predictions_df, 0
    if not isinstance(results_index, dict) or not results_index:
        return predictions_df, 0
    frame = predictions_df.copy()
    keep_mask = ~frame["prediction_key"].astype(str).isin(set(results_index.keys()))
    dropped = int((~keep_mask).sum())
    frame = frame[keep_mask].copy()
    return frame, dropped


def dedupe_predictions(predictions_df):
    if predictions_df.empty:
        return predictions_df

    frame = predictions_df.copy()
    def canonical_fixture_key(row):
        parsed_date = parse_match_date(row.get("match_date"))
        if parsed_date is None:
            return str(row.get("prediction_key", ""))
        home_key = normalize_team_key(row.get("home_team", ""))
        away_key = normalize_team_key(row.get("away_team", ""))
        team_pair = sorted([home_key, away_key])
        return f"{parsed_date.strftime('%Y-%m-%d')}|{row.get('competition', '')}|{team_pair[0]}|{team_pair[1]}"

    frame["canonical_prediction_key"] = frame.apply(canonical_fixture_key, axis=1)
    frame = frame.sort_values(["created_at_utc", "prediction_key"], na_position="last")
    frame = frame.drop_duplicates(subset=["canonical_prediction_key"], keep="last")
    frame["prediction_key"] = frame["canonical_prediction_key"]
    frame = frame.drop(columns=["canonical_prediction_key"])
    return frame


def keep_only_current_fixtures(predictions_df, fixtures_df):
    if predictions_df.empty or fixtures_df.empty:
        return predictions_df.iloc[0:0].copy()

    fixture_keys = set()
    for _, row in fixtures_df.iterrows():
        match_date = parse_match_date(row.get("match_date"))
        competition = str(row.get("competition", "")).strip()
        home_team = str(row.get("mapped_home_team", "") or row.get("home_team", "")).strip()
        away_team = str(row.get("mapped_away_team", "") or row.get("away_team", "")).strip()
        if match_date is None or not competition or not home_team or not away_team:
            continue
        fixture_keys.add(make_prediction_key(match_date, competition, home_team, away_team))

    if not fixture_keys:
        return predictions_df.iloc[0:0].copy()

    # Keep only fixtures that still belong to the latest regional slate.
    frame = predictions_df.copy()
    keep_mask = frame["prediction_key"].astype(str).isin(fixture_keys)
    return frame[keep_mask].copy()


def main():
    args = parse_cli_args()

    if args.refresh_download:
        download_latest.main()

    fixtures = load_upcoming_matchweek_fixtures(args.api_token, args.window_days)
    if fixtures.empty:
        print(
            "No upcoming fixtures found from API, ESPN, CSV download, or raw season files. "
            "Run with --refresh-download or check Raw_Data for current-season mlsstat/mexstat files."
        )
        return

    context = build_prediction_context()
    team_mapping = load_shared_mapping()
    team_mapping, canonical_added = ensure_canonical_self_mappings(team_mapping, context)
    team_mapping, new_map_entries, mapping_drift, blanks_added = update_team_mapping_from_fixtures(fixtures, context, team_mapping)
    save_team_mapping(TEAM_MAPPING_FILE, team_mapping)
    fixtures = apply_team_mapping_to_fixtures(fixtures, team_mapping, context)
    existing = load_prediction_store(PREDICTIONS_FILE)
    existing = existing.set_index("prediction_key", drop=False) if not existing.empty else existing

    new_records = []
    skipped = 0
    for _, fixture in fixtures.iterrows():
        pred = predict_fixture(fixture, context)
        if pred is None:
            pred = build_schedule_only_row(fixture, context)
        if pred is None:
            skipped += 1
            continue
        new_records.append(pred)

    new_df = pd.DataFrame(new_records, columns=RESULT_COLUMNS).astype("object")
    if new_df.empty and existing.empty:
        print("No match predictions were generated.")
        return

    if existing.empty:
        combined = new_df.copy()
    else:
        combined = existing.copy().astype("object")
        for _, row in new_df.iterrows():
            combined.loc[row["prediction_key"]] = row
        combined = combined.reset_index(drop=True)

    # Remove stale rows so the website only receives fixtures from the active pull.
    combined = keep_only_current_fixtures(combined, fixtures)
    results_index = load_results_index(RAW_DATA_DIR)
    combined, settled_count = settle_predictions(combined, results_index)
    combined, removed_completed = drop_completed_predictions(combined, results_index)
    combined = dedupe_predictions(combined)
    combined = combined[RESULT_COLUMNS].sort_values(["match_date", "competition", "home_team", "away_team"])

    os.makedirs(PREDICTIONS_DIR, exist_ok=True)
    combined.to_csv(PREDICTIONS_FILE, index=False)

    print(f"Upcoming fixtures found: {len(fixtures)}")
    print(f"Team mappings file: {TEAM_MAPPING_FILE}")
    print(f"Canonical raw-data names added: {canonical_added}")
    print(f"Team mappings added from current pull: {new_map_entries}")
    print(f"New blank mappings needing manual edit: {blanks_added}")
    print(f"Potential mapping drifts detected (kept your existing map): {mapping_drift}")
    print(f"Predictions written: {len(new_df)}")
    print(f"Skipped (unmatched team names): {skipped}")
    print(f"Removed completed fixtures from upcoming list: {removed_completed}")
    print(f"Newly settled with real results: {settled_count}")
    print(f"Saved tracking file: {PREDICTIONS_FILE}")


if __name__ == "__main__":
    main()




