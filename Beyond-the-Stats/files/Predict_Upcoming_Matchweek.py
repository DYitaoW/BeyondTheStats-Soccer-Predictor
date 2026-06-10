import argparse
import difflib
import json
import os
import urllib.error
import urllib.request
import unicodedata
from datetime import UTC, datetime

import joblib
import pandas as pd

import Download_Latest_Data as download_latest
import Predict_Match as pm


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DATA_DIR = os.path.join(BASE_DIR, "Data", "Raw_Data")
PREDICTIONS_DIR = os.path.join(BASE_DIR, "Data", "Predictions")
PREDICTIONS_FILE = os.path.join(PREDICTIONS_DIR, "upcoming_matchweek_predictions.csv")
TEAM_MAPPING_FILE = os.path.join(PREDICTIONS_DIR, "team_name_mapping_master.json")
LEGACY_GLOBAL_MAPPING_FILE = os.path.join(PREDICTIONS_DIR, "upcoming_fixture_team_mapping.json")
LEGACY_MLS_MAPPING_FILE = os.path.join(BASE_DIR, "MLS", "Data", "Predictions", "upcoming_fixture_team_mapping.json")
TEAM_DATA_DIR = os.path.join(BASE_DIR, "Data", "Team_Data")
SCORERS_FILE = os.path.join(TEAM_DATA_DIR, "current_season_top_scorers.json")
FOOTBALL_DATA_API_BASE = "https://api.football-data.org/v4"

# football-data.org competition codes mapped to your project competition naming.
API_COMPETITIONS = {
    "PL": "England/Premier League",
    "ELC": "England/Championship",
    "PD": "Spain/La Liga",
    "SD": "Spain/La Liga 2",
    "SA": "Italy/Serie A",
    "BL1": "Germany/Bundesliga",
    "BL2": "Germany/Bundesliga 2",
    "FL1": "France/Ligue 1",
    "PPL": "Portugal/Liga Portugal",
}

# Cup fixtures come from football-data.org only and share the global upcoming feed.
CUP_API_COMPETITIONS = {
    "FAC": "England/FA Cup",
    "FLC": "England/League Cup",
    "CL": "Europe/Champions League",
    "EL": "Europe/Europa League",
    "UCL": "Europe/Conference League",
}
CUP_COMPETITIONS = set(CUP_API_COMPETITIONS.values())
PROVISIONAL_LEAGUE_KEY = "__provisional__"
PROVISIONAL_STRENGTH_COEFF = 0.50
CUP_RANDOMIZER_MAX_DELTA = 0.11
NEUTRAL_RANDOMIZER_BONUS = 0.03
PROVISIONAL_RANDOMIZER_BONUS = 0.02

MANUAL_TEAM_OVERRIDES = {
    "Manchester City FC": "Man City",
    "Wolverhampton Wanderers FC": "Wolves",
    "1. FC Köln": "FC Koln",
    "AC Pisa 1909": "Pisa",
    "Borussia Mönchengladbach": "M'gladbach",
    "Deportivo Alavés": "Alaves",
    "FC Internazionale Milano": "Inter",
    "FC Lorient": "Lorient",
    "FC Metz": "Metz",
    "Paris Saint-Germain FC": "Paris SG",
    "Queens Park Rangers FC": "QPR",
    "RC Celta de Vigo": "Celta",
    "RCD Espanyol de Barcelona": "Espanol",
    "Rayo Vallecano de Madrid": "Vallecano",
    "Real Betis Balompié": "Betis",
    "Sporting Clube de Braga": "Sp Braga",
    "Sporting Clube de Portugal": "Sp Lisbon",
    "Stade Rennais FC 1901": "Rennes",
    "US Cremonese": "Cremonese",
    "Vitória SC": "Guimaraes",
}

TEAM_KEY_ALIASES = {
    "caosasuna": "osasuna",
    "uslecce": "lecce",
    "borussiadortmund": "dortmund",
    "brightonandhove": "brighton",
    "brightonhove": "brighton",
    "como1907": "como",
    "sheffieldwednesday": "sheffieldweds",
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
    "is_neutral_site",
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
        default=3,
        help="Minimum lookahead window in days, extended through the next Tuesday when that is farther out.",
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


def is_cup_competition(competition_name):
    return str(competition_name).strip() in CUP_COMPETITIONS


def is_likely_neutral_site(competition_name, stage_name, venue_name=""):
    competition_name = str(competition_name).strip()
    stage_name = str(stage_name).strip().upper()
    # Only finals are treated as neutral; venue name is no longer parsed.
    if stage_name == "FINAL":
        return True
    return False


def display_team_name(team_name, is_provisional=False):
    team_name = str(team_name).strip()
    return f"{team_name} (P)" if is_provisional and team_name else team_name


def calculate_fixture_window_end(window_days, start_date=None):
    # Anchor the window to today so the pull covers the current scheduling block.
    today = pd.Timestamp(start_date or datetime.now(UTC).date())
    today = today.normalize()
    min_window_end = today + pd.Timedelta(days=max(0, int(window_days)))

    # Extend through the next Tuesday when that keeps the Friday-to-Tuesday slate intact.
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
    token_aliases = {
        "weds": "wednesday",
        "utd": "united",
        "st": "saint",
    }
    parts = [token_aliases.get(p, p) for p in parts]
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
        "and",
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
    shared = load_team_mapping(TEAM_MAPPING_FILE)
    if shared:
        return shared
    # One-time migration path so existing manual work is preserved.
    merged = {}
    for legacy in [LEGACY_GLOBAL_MAPPING_FILE, LEGACY_MLS_MAPPING_FILE]:
        legacy_map = load_team_mapping(legacy)
        for competition, names in legacy_map.items():
            merged.setdefault(competition, {})
            for api_name, mapped_name in names.items():
                if api_name not in merged[competition]:
                    merged[competition][api_name] = mapped_name
    if merged:
        save_team_mapping(TEAM_MAPPING_FILE, merged)
    return merged


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


def append_only_mapping_from_fixtures(fixtures, context, mapping):
    updated = dict(mapping) if isinstance(mapping, dict) else {}
    added = 0
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
            if api_name in updated[competition]:
                continue
            if api_name in canonical_names:
                updated[competition][api_name] = api_name
            else:
                # Unknown mapping stays blank for manual assignment.
                updated[competition][api_name] = ""
                blanks_added += 1
            added += 1

    normalized = {}
    for competition, names in sorted(updated.items(), key=lambda item: item[0].lower()):
        if not isinstance(names, dict):
            continue
        normalized[competition] = dict(
            sorted(
                ((str(k).strip(), str(v).strip()) for k, v in names.items() if str(k).strip()),
                key=lambda item: item[0].lower(),
            )
        )
    return normalized, added, blanks_added


def resolve_live_team_name(raw_name, competition, context):
    team_competition_map = context["team_competition_map"]
    comp_candidates = [team for team in context["available_teams"] if team_competition_map.get(team) == competition]
    valid_names = comp_candidates if comp_candidates else context["available_teams"]

    manual_override = MANUAL_TEAM_OVERRIDES.get(str(raw_name).strip())
    if manual_override:
        if manual_override in valid_names:
            return manual_override
        if manual_override in context["available_teams"]:
            return manual_override

    direct = pm.resolve_team_name(raw_name, valid_names)
    if direct:
        return direct

    key = normalize_team_key(raw_name)
    if not key:
        return None

    by_key = {normalize_team_key(team): team for team in valid_names}
    if key in by_key:
        return by_key[key]

    # Catch API names like "Newcastle United FC" when the canonical project name is just "Newcastle".
    contained_by_raw = [team for team in valid_names if normalize_team_key(team) and normalize_team_key(team) in key]
    if len(contained_by_raw) == 1:
        return contained_by_raw[0]

    contains = [team for team in valid_names if key in normalize_team_key(team)]
    if len(contains) == 1:
        return contains[0]

    candidates = list(by_key.keys())
    close = difflib.get_close_matches(key, candidates, n=1, cutoff=0.88)
    if close:
        return by_key[close[0]]
    return None


def update_team_mapping_from_fixtures(fixtures, context, mapping):
    updated = dict(mapping)
    new_entries = 0
    changed_entries = 0
    valid_names = set(context.get("available_teams", []))

    # Keep only API names that appear in the current fixture pull for each competition.
    api_names_by_comp = {}
    for _, row in fixtures.iterrows():
        competition = str(row.get("competition", "")).strip()
        if not competition:
            continue
        api_names_by_comp.setdefault(competition, set())
        for side_col in ["home_team", "away_team"]:
            api_name = str(row.get(side_col, "")).strip()
            if api_name:
                api_names_by_comp[competition].add(api_name)

    for competition in list(updated.keys()):
        if competition not in api_names_by_comp:
            del updated[competition]
            continue
        names = updated.get(competition, {})
        if not isinstance(names, dict):
            updated[competition] = {}
            continue
        allowed_api_names = api_names_by_comp[competition]
        cleaned = {}
        for api_name, mapped_name in names.items():
            api_key = str(api_name).strip()
            mapped_value = str(mapped_name).strip()
            if api_key not in allowed_api_names:
                continue
            if mapped_value in valid_names:
                cleaned[api_key] = mapped_value
            else:
                resolved = resolve_live_team_name(api_key, competition, context)
                cleaned[api_key] = resolved if resolved else ""
        updated[competition] = cleaned

    for _, row in fixtures.iterrows():
        competition = str(row.get("competition", "")).strip()
        if not competition:
            continue
        updated.setdefault(competition, {})

        for side_col in ["home_team", "away_team"]:
            api_name = str(row.get(side_col, "")).strip()
            if not api_name:
                continue
            resolved = resolve_live_team_name(api_name, competition, context)
            target = resolved if resolved else ""

            existing = str(updated[competition].get(api_name, "")).strip()
            if not existing:
                updated[competition][api_name] = target
                new_entries += 1
            elif existing != target:
                changed_entries += 1

    return updated, new_entries, changed_entries


def apply_team_mapping_to_fixtures(fixtures, mapping, context):
    mapped = fixtures.copy()
    known_teams = set(context.get("available_teams", []))

    def mapped_name(competition, api_name):
        competition = str(competition)
        api_name = str(api_name)
        comp_map = mapping.get(competition, {})

        direct = str(comp_map.get(api_name, "")).strip()
        if direct:
            if direct in known_teams:
                return direct
            return ""

        # If API name already matches canonical raw-data team name, use it.
        if api_name in known_teams:
            return api_name
        return ""

    # Preserve raw cup teams as provisional rows so they can still be predicted.
    mapped["mapped_home_team"] = mapped.apply(
        lambda row: mapped_name(row.get("competition", ""), row.get("home_team", "")),
        axis=1,
    )
    mapped["mapped_away_team"] = mapped.apply(
        lambda row: mapped_name(row.get("competition", ""), row.get("away_team", "")),
        axis=1,
    )
    mapped["home_is_provisional"] = mapped.apply(
        lambda row: bool(
            is_cup_competition(row.get("competition", ""))
            and not str(row.get("mapped_home_team", "")).strip()
            and str(row.get("home_team", "")).strip()
        ),
        axis=1,
    )
    mapped["away_is_provisional"] = mapped.apply(
        lambda row: bool(
            is_cup_competition(row.get("competition", ""))
            and not str(row.get("mapped_away_team", "")).strip()
            and str(row.get("away_team", "")).strip()
        ),
        axis=1,
    )
    mapped.loc[mapped["home_is_provisional"], "mapped_home_team"] = mapped.loc[mapped["home_is_provisional"], "home_team"]
    mapped.loc[mapped["away_is_provisional"], "mapped_away_team"] = mapped.loc[mapped["away_is_provisional"], "away_team"]
    mapped["display_home_team"] = mapped.apply(
        lambda row: display_team_name(
            row.get("mapped_home_team", "") or row.get("home_team", ""),
            bool(row.get("home_is_provisional", False)),
        ),
        axis=1,
    )
    mapped["display_away_team"] = mapped.apply(
        lambda row: display_team_name(
            row.get("mapped_away_team", "") or row.get("away_team", ""),
            bool(row.get("away_is_provisional", False)),
        ),
        axis=1,
    )
    mapped["home_competition_override"] = mapped.apply(
        lambda row: PROVISIONAL_LEAGUE_KEY if bool(row.get("home_is_provisional", False)) else "",
        axis=1,
    )
    mapped["away_competition_override"] = mapped.apply(
        lambda row: PROVISIONAL_LEAGUE_KEY if bool(row.get("away_is_provisional", False)) else "",
        axis=1,
    )
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


def load_upcoming_matchweek_fixtures_from_api(api_token, window_days):
    today = pd.Timestamp(datetime.now(UTC).date())
    rows = []
    headers = {"X-Auth-Token": api_token}
    accessible_competitions = 0

    for competition_code, competition_name in {**API_COMPETITIONS, **CUP_API_COMPETITIONS}.items():
        url = f"{FOOTBALL_DATA_API_BASE}/competitions/{competition_code}/matches?status=SCHEDULED"
        try:
            data = fetch_json(url, headers=headers, timeout=45)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise RuntimeError("football-data.org API token is invalid or missing permission.") from error
            if error.code in {403, 404, 429}:
                continue
            continue
        except Exception:
            continue

        accessible_competitions += 1

        matches = data.get("matches", [])
        if not isinstance(matches, list) or not matches:
            continue

        comp_rows = []
        for match in matches:
            home_team = ((match.get("homeTeam") or {}).get("name") or "").strip()
            away_team = ((match.get("awayTeam") or {}).get("name") or "").strip()
            utc_date = match.get("utcDate")
            if not home_team or not away_team or not utc_date:
                continue

            parsed = pd.to_datetime(utc_date, utc=True, errors="coerce")
            if pd.isna(parsed):
                continue

            match_date = parsed.tz_convert("UTC").tz_localize(None).normalize()
            if match_date < today:
                continue

            stage_name = str(match.get("stage", "")).strip()
            venue_name = str(match.get("venue", "")).strip()
            is_neutral_site = is_likely_neutral_site(competition_name, stage_name, venue_name)

            comp_rows.append(
                {
                    "match_date": match_date,
                    "match_datetime_utc": str(parsed.tz_convert("UTC").isoformat()),
                    "competition": competition_name,
                    "home_team": home_team,
                    "away_team": away_team,
                    "stage": stage_name,
                    "venue": venue_name,
                    "is_neutral_site": is_neutral_site,
                }
            )

        if not comp_rows:
            continue

        comp_df = pd.DataFrame(comp_rows).sort_values(["match_date", "home_team", "away_team"])
        
        # Use a current-date window so late-week pulls keep the full Friday-to-Tuesday block.
        cutoff_date = calculate_fixture_window_end(window_days)
        comp_df = comp_df[comp_df["match_date"] <= cutoff_date]
        rows.extend(comp_df.to_dict("records"))

    fixtures = pd.DataFrame(rows)
    if fixtures.empty:
        return fixtures
    fixtures = fixtures.sort_values(["match_date", "competition", "home_team", "away_team"]).reset_index(drop=True)
    return fixtures


def _raw_is_played(row):
    res = str(row.get("FTR", "")).strip().upper()
    hg = row.get("FTHG")
    ag = row.get("FTAG")
    return res in {"H", "D", "A"} and pd.notna(hg) and pd.notna(ag)


def load_upcoming_matchweek_fixtures_from_raw(raw_dir, window_days):
    latest = find_latest_season_file_per_competition(raw_dir)
    if not latest:
        return pd.DataFrame()

    today = pd.Timestamp(datetime.now(UTC).date())
    rows = []
    for competition, rel_path in sorted(latest.items()):
        # Skip Ligue 2 from the global upcoming slate.
        if competition == "France/Ligue 2":
            continue
        full_path = os.path.join(raw_dir, rel_path)
        try:
            frame = pd.read_csv(full_path)
        except Exception:
            frame = pd.read_csv(full_path, encoding="latin-1", engine="python", on_bad_lines="skip")

        needed = {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"}
        if not needed.issubset(frame.columns):
            continue

        frame = frame.copy()
        frame["DateParsed"] = pd.to_datetime(frame["Date"], dayfirst=True, format="mixed", errors="coerce")
        frame = frame[frame["HomeTeam"].notna() & frame["AwayTeam"].notna()]
        frame = frame[frame["DateParsed"].notna()]
        if frame.empty:
            continue

        frame = frame[~frame.apply(_raw_is_played, axis=1)]
        if frame.empty:
            continue

        future = frame[frame["DateParsed"] >= today]
        if future.empty:
            future = frame.copy()

        # Reuse the same current-date window instead of anchoring to the first fixture date.
        cutoff_date = calculate_fixture_window_end(window_days, start_date=today)
        window = future[future["DateParsed"] <= cutoff_date]
        for _, row in window.iterrows():
            rows.append(
                {
                    "match_date": row["DateParsed"].normalize(),
                    "competition": competition,
                    "home_team": str(row["HomeTeam"]).strip(),
                    "away_team": str(row["AwayTeam"]).strip(),
                }
            )

    if not rows:
        return pd.DataFrame()
    fixtures = pd.DataFrame(rows)
    fixtures = fixtures.sort_values(["match_date", "competition", "home_team", "away_team"]).reset_index(drop=True)
    return fixtures


def _dedupe_fixtures(fixtures):
    if fixtures.empty:
        return fixtures
    work = fixtures.copy()
    work["home_key"] = work["home_team"].map(normalize_team_key)
    work["away_key"] = work["away_team"].map(normalize_team_key)
    work["pair_key"] = work.apply(
        lambda r: "|".join(sorted([r["home_key"], r["away_key"]])), axis=1
    )
    work["dedupe_key"] = work["match_date"].astype(str) + "|" + work["competition"] + "|" + work["pair_key"]
    work = work.drop_duplicates(subset=["dedupe_key"], keep="first")
    work = work.drop(columns=["home_key", "away_key", "pair_key", "dedupe_key"])
    return work


def load_prediction_store(path):
    if not os.path.exists(path):
        return pd.DataFrame(columns=RESULT_COLUMNS)
    frame = pd.read_csv(path)
    for col in RESULT_COLUMNS:
        if col not in frame.columns:
            frame[col] = ""
    frame = frame[RESULT_COLUMNS].copy()
    return frame.astype("object")


def load_finished_matches_from_api(api_token):
    """Fetch finished matches from football-data.org API for all configured competitions."""
    results = {}
    headers = {"X-Auth-Token": api_token}

    for competition_code, competition_name in {**API_COMPETITIONS, **CUP_API_COMPETITIONS}.items():
        url = f"{FOOTBALL_DATA_API_BASE}/competitions/{competition_code}/matches?status=FINISHED"
        try:
            data = fetch_json(url, headers=headers, timeout=45)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                print("Warning: API token invalid or missing permission. Falling back to CSV results.")
                return results
            if error.code in {403, 404, 429}:
                continue
            continue
        except Exception as exc:
            print(f"Warning: Could not fetch finished matches for {competition_name}: {exc}")
            continue

        matches = data.get("matches", [])
        if not isinstance(matches, list) or not matches:
            continue

        for match in matches:
            home_team = ((match.get("homeTeam") or {}).get("name") or "").strip()
            away_team = ((match.get("awayTeam") or {}).get("name") or "").strip()
            utc_date = match.get("utcDate")
            
            # Check if match has finished (both score and status indicate completion).
            status = str(match.get("status", "")).strip()
            score = match.get("score") or {}
            full_time = score.get("fullTime") or {}
            home_goals = full_time.get("home")
            away_goals = full_time.get("away")
            
            if not home_team or not away_team or not utc_date:
                continue
            if home_goals is None or away_goals is None:
                continue
            if status not in {"FINISHED", "FULL_TIME"}:
                continue

            parsed = pd.to_datetime(utc_date, utc=True, errors="coerce")
            if pd.isna(parsed):
                continue

            match_date = parsed.tz_convert("UTC").tz_localize(None).normalize()
            
            # Determine result: H (home win), D (draw), A (away win).
            if home_goals > away_goals:
                result = "H"
            elif away_goals > home_goals:
                result = "A"
            else:
                result = "D"
            
            key = make_prediction_key(match_date, competition_name, home_team, away_team)
            results[key] = {
                "actual_home_goals": int(home_goals),
                "actual_away_goals": int(away_goals),
                "actual_result": result,
            }

    return results


def load_top_scorers_from_api(api_token):
    """Fetch current season top scorers from football-data.org API for all configured competitions."""
    scorers_by_competition = {}
    headers = {"X-Auth-Token": api_token}

    for competition_code, competition_name in API_COMPETITIONS.items():
        url = f"{FOOTBALL_DATA_API_BASE}/competitions/{competition_code}/scorers"
        try:
            data = fetch_json(url, headers=headers, timeout=45)
        except urllib.error.HTTPError as error:
            if error.code in {401, 403, 404, 429}:
                continue
            continue
        except Exception as exc:
            print(f"Warning: Could not fetch scorers for {competition_name}: {exc}")
            continue

        scorers_list = data.get("scorers", [])
        if not isinstance(scorers_list, list) or not scorers_list:
            continue

        competition_scorers = []
        for scorer in scorers_list:
            player = scorer.get("player") or {}
            team = scorer.get("team") or {}
            goals = scorer.get("goals")
            assists = scorer.get("assists")
            
            player_name = str(player.get("name", "")).strip()
            team_name = str(team.get("name", "")).strip()
            
            if not player_name or goals is None:
                continue
            
            competition_scorers.append({
                "rank": len(competition_scorers) + 1,
                "player_name": player_name,
                "team_name": team_name,
                "goals": int(goals),
                "assists": int(assists) if assists is not None else 0,
                "player_id": player.get("id"),
                "team_id": team.get("id"),
            })

        if competition_scorers:
            scorers_by_competition[competition_name] = competition_scorers

    return scorers_by_competition


def save_top_scorers(scorers_by_competition):
    """Save fetched scorers data to JSON file."""
    os.makedirs(TEAM_DATA_DIR, exist_ok=True)
    
    output = {
        "last_updated_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "competitions": scorers_by_competition,
    }
    
    with open(SCORERS_FILE, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)
    
    return len(scorers_by_competition)


def load_results_index(raw_dir, api_token=None):
    """Load match results from API first (if token provided), then fall back to CSV files."""
    results = {}
    
    # Try to fetch from API first if token is provided.
    if api_token:
        print("Fetching finished matches from football-data.org API...")
        api_results = load_finished_matches_from_api(api_token)
        results.update(api_results)
        if api_results:
            print(f"  Loaded {len(api_results)} finished matches from API")
    
    # Fall back to CSV files for any missing results or if no API token.
    csv_count = 0
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
                # Only add from CSV if not already fetched from API.
                if key not in results:
                    results[key] = {
                        "actual_home_goals": int(row["FTHG"]),
                        "actual_away_goals": int(row["FTAG"]),
                        "actual_result": str(row["FTR"]).strip(),
                    }
                    csv_count += 1
    
    if csv_count > 0:
        print(f"  Loaded {csv_count} additional finished matches from CSV files")
    
    return results


def build_prediction_context():
    matches, season_files = pm.load_training_matches(pm.PROCESSED_DIR)

    if not os.path.exists(pm.MODEL_CACHE):
        raise FileNotFoundError(
            f"Model cache not found at {pm.MODEL_CACHE}. Run Predict_Match.py once first to build it."
        )

    try:
        bundle = joblib.load(pm.MODEL_CACHE)
    except Exception as exc:
        raise RuntimeError(
            f"Could not load model cache at {pm.MODEL_CACHE}. Rebuild it by running Predict_Match.py."
        ) from exc

    fingerprint = pm.data_fingerprint(season_files)
    if bundle.get("fingerprint") != fingerprint:
        raise RuntimeError(
            "Model cache is stale for current processed data. Run Predict_Match.py to rebuild before predicting matchweek."
        )

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
    }
    missing = sorted(k for k in required_keys if k not in bundle)
    if missing:
        raise RuntimeError(
            "Model cache is missing required fields: " + ", ".join(missing) + ". Re-run Predict_Match.py."
        )

    overall_teams = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "overall_teams.json"))
    season_teams = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "season_teams.json"))
    head_to_head = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "head_to_head.json"))
    current_form = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "current_form.json"))
    league_strength = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "league_strength.json")) or {}
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
    # Cup provisional teams should resolve to a neutral mid-strength prior.
    league_strength[PROVISIONAL_LEAGUE_KEY] = PROVISIONAL_STRENGTH_COEFF

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
        "overall_teams": overall_teams,
        "season_teams": season_teams,
        "head_to_head": head_to_head,
        "current_form": current_form,
        "league_strength": league_strength,
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

    home_team = str(row.get("mapped_home_team", "")).strip()
    away_team = str(row.get("mapped_away_team", "")).strip()
    home_display = str(row.get("display_home_team", "")).strip() or home_team or str(raw_home).strip()
    away_display = str(row.get("display_away_team", "")).strip() or away_team or str(raw_away).strip()
    home_is_provisional = bool(row.get("home_is_provisional", False))
    away_is_provisional = bool(row.get("away_is_provisional", False))
    is_neutral_site = bool(row.get("is_neutral_site", False))
    if not home_team or not away_team or home_team == away_team:
        return None

    season_teams = context["season_teams"]
    competition_season = latest_season_for_competition(season_teams, competition, context["latest_season"])
    prediction_season = pm.choose_season_for_teams(home_team, away_team, season_teams, competition_season)
    inject_fallback_team(home_team, competition, prediction_season, context)
    inject_fallback_team(away_team, competition, prediction_season, context)
    prediction_start_year = pm.parse_start_year_from_key(prediction_season)
    season_coeff = pm.season_recency_coefficient(context["latest_start_year"], prediction_start_year)
    if home_is_provisional or away_is_provisional:
        # Provisional cup teams should lean on a softer, lower-confidence prior.
        season_coeff = min(season_coeff, PROVISIONAL_STRENGTH_COEFF)

    home_comp = str(row.get("home_competition_override", "")).strip() or context["team_competition_map"].get(home_team, competition)
    away_comp = str(row.get("away_competition_override", "")).strip() or context["team_competition_map"].get(away_team, competition)

    randomizer_delta = pm.EU_RANDOMIZER_MAX_DELTA
    if is_cup_competition(competition):
        randomizer_delta = CUP_RANDOMIZER_MAX_DELTA
    if is_neutral_site:
        randomizer_delta += NEUTRAL_RANDOMIZER_BONUS
    if home_is_provisional:
        randomizer_delta += PROVISIONAL_RANDOMIZER_BONUS
    if away_is_provisional:
        randomizer_delta += PROVISIONAL_RANDOMIZER_BONUS
    randomizer_delta = min(randomizer_delta, 0.18)

    def build_feature_frame(local_home, local_away, local_home_comp, local_away_comp):
        match_input = pm.build_match_input(local_home, local_away)
        feature_frame = pm.build_features(
            match_input,
            prediction_season,
            competition,
            season_coeff,
            context["overall_teams"],
            context["season_teams"],
            context["head_to_head"],
            context["current_form"],
            context["league_strength"],
            home_competition_override=local_home_comp,
            away_competition_override=local_away_comp,
        )
        feature_frame = pd.get_dummies(feature_frame, columns=["competition"], dtype=float)
        return feature_frame.reindex(columns=context["train_columns"], fill_value=0.0)

    def predict_probabilities(feature_frame, local_home, local_away):
        probabilities = {"H": 0.0, "D": 0.0, "A": 0.0}
        proba_values = context["clf"].predict_proba(feature_frame)[0]
        for idx, encoded_label in enumerate(context["clf"].classes_):
            label = context["result_label_encoder"].inverse_transform([encoded_label])[0]
            probabilities[label] = float(proba_values[idx])
        probabilities = pm.reduce_draw_probability(probabilities)
        seed = pm.prediction_randomizer_seed(local_home, local_away, competition, prediction_season)
        return pm.apply_probability_randomizer(probabilities, randomizer_delta, seed=seed)

    X_match = build_feature_frame(home_team, away_team, home_comp, away_comp)
    probabilities = predict_probabilities(X_match, home_team, away_team)
    pred_home_goals = max(0.0, float(context["home_goal_reg"].predict(X_match)[0]))
    pred_away_goals = max(0.0, float(context["away_goal_reg"].predict(X_match)[0]))
    pred_home_shots = max(0.0, float(context["home_shot_reg"].predict(X_match)[0]))
    pred_away_shots = max(0.0, float(context["away_shot_reg"].predict(X_match)[0]))
    pred_home_sot = max(0.0, float(context["home_sot_reg"].predict(X_match)[0]))
    pred_away_sot = max(0.0, float(context["away_sot_reg"].predict(X_match)[0]))

    if is_neutral_site:
        # Average both team orientations so home advantage becomes part of the uncertainty instead of a fixed edge.
        X_swapped = build_feature_frame(away_team, home_team, away_comp, home_comp)
        swapped_probabilities = predict_probabilities(X_swapped, away_team, home_team)
        probabilities = {
            "H": (probabilities["H"] + swapped_probabilities["A"]) / 2.0,
            "D": (probabilities["D"] + swapped_probabilities["D"]) / 2.0,
            "A": (probabilities["A"] + swapped_probabilities["H"]) / 2.0,
        }
        pred_home_goals = max(
            0.0,
            (
                pred_home_goals
                + float(context["away_goal_reg"].predict(X_swapped)[0])
            )
            / 2.0,
        )
        pred_away_goals = max(
            0.0,
            (
                pred_away_goals
                + float(context["home_goal_reg"].predict(X_swapped)[0])
            )
            / 2.0,
        )
        pred_home_shots = max(
            0.0,
            (
                pred_home_shots
                + float(context["away_shot_reg"].predict(X_swapped)[0])
            )
            / 2.0,
        )
        pred_away_shots = max(
            0.0,
            (
                pred_away_shots
                + float(context["home_shot_reg"].predict(X_swapped)[0])
            )
            / 2.0,
        )
        pred_home_sot = max(
            0.0,
            (
                pred_home_sot
                + float(context["away_sot_reg"].predict(X_swapped)[0])
            )
            / 2.0,
        )
        pred_away_sot = max(
            0.0,
            (
                pred_away_sot
                + float(context["home_sot_reg"].predict(X_swapped)[0])
            )
            / 2.0,
        )

    prediction = max(probabilities, key=probabilities.get)

    key = make_prediction_key(match_date, competition, home_team, away_team)
    match_datetime_utc = str(row.get("match_datetime_utc", "")).strip()
    return {
        "prediction_key": key,
        "created_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "match_date": match_date.strftime("%Y-%m-%d"),
        "match_datetime_utc": match_datetime_utc,
        "competition": competition,
        "home_team": home_team,
        "away_team": away_team,
        "display_home_team": home_display,
        "display_away_team": away_display,
        "is_neutral_site": "1" if is_neutral_site else "0",
        "predicted_result": prediction,
        "prob_home": round(probabilities["H"], 6),
        "prob_draw": round(probabilities["D"], 6),
        "prob_away": round(probabilities["A"], 6),
        "pred_home_goals": round(pred_home_goals, 3),
        "pred_away_goals": round(pred_away_goals, 3),
        "pred_home_shots": round(pred_home_shots, 3),
        "pred_away_shots": round(pred_away_shots, 3),
        "pred_home_sot": round(pred_home_sot, 3),
        "pred_away_sot": round(pred_away_sot, 3),
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

    # Keep only fixtures that still belong to the latest upcoming slate.
    frame = predictions_df.copy()
    keep_mask = frame["prediction_key"].astype(str).isin(fixture_keys)
    return frame[keep_mask].copy()


def enforce_single_match_per_team_day(predictions_df):
    if predictions_df.empty:
        return predictions_df, 0

    frame = predictions_df.copy()
    frame = frame.sort_values(["created_at_utc", "prediction_key"], na_position="last")

    kept_rows = []
    team_day_seen = set()

    for _, row in frame.iterrows():
        match_date = str(row.get("match_date", "")).strip()
        competition = str(row.get("competition", "")).strip()
        home_team = normalize_team_key(row.get("home_team", ""))
        away_team = normalize_team_key(row.get("away_team", ""))
        if not match_date or not competition or not home_team or not away_team:
            continue

        home_key = (match_date, competition, home_team)
        away_key = (match_date, competition, away_team)
        if home_key in team_day_seen or away_key in team_day_seen:
            continue

        team_day_seen.add(home_key)
        team_day_seen.add(away_key)
        kept_rows.append(row)

    kept = pd.DataFrame(kept_rows, columns=frame.columns).astype("object")
    dropped = len(frame) - len(kept)
    return kept, dropped


def main():
    args = parse_cli_args()

    if args.refresh_download:
        download_latest.main()

    fixtures_api = pd.DataFrame()
    if args.api_token:
        try:
            fixtures_api = load_upcoming_matchweek_fixtures_from_api(args.api_token, args.window_days)
        except Exception as exc:
            print(f"API fixtures load failed: {exc}")
            fixtures_api = pd.DataFrame()
    else:
        print("No API token provided; using raw CSV fixtures only.")

    fixtures_raw = load_upcoming_matchweek_fixtures_from_raw(RAW_DATA_DIR, args.window_days)
    fixtures = pd.concat([fixtures_api, fixtures_raw], ignore_index=True) if not fixtures_raw.empty or not fixtures_api.empty else pd.DataFrame()
    fixtures = _dedupe_fixtures(fixtures)
    if fixtures.empty:
        print("No upcoming matchweek fixtures returned by API or raw CSVs.")
        return

    context = build_prediction_context()
    team_mapping = load_shared_mapping()
    team_mapping, canonical_added = ensure_canonical_self_mappings(team_mapping, context)
    team_mapping, added_api_names, mapping_changes = update_team_mapping_from_fixtures(fixtures, context, team_mapping)
    save_team_mapping(TEAM_MAPPING_FILE, team_mapping)
    fixtures = apply_team_mapping_to_fixtures(fixtures, team_mapping, context)
    existing = load_prediction_store(PREDICTIONS_FILE)
    existing = existing.set_index("prediction_key", drop=False) if not existing.empty else existing

    new_records = []
    skipped = 0
    for _, fixture in fixtures.iterrows():
        pred = predict_fixture(fixture, context)
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

    # Remove stale rows so old leagues and fixtures do not linger in the upcoming file.
    combined = keep_only_current_fixtures(combined, fixtures)
    results_index = load_results_index(RAW_DATA_DIR, api_token=args.api_token)
    combined, settled_count = settle_predictions(combined, results_index)
    combined, removed_completed = drop_completed_predictions(combined, results_index)
    combined = dedupe_predictions(combined)
    combined, single_match_dropped = enforce_single_match_per_team_day(combined)
    combined = combined[RESULT_COLUMNS].sort_values(["match_date", "competition", "home_team", "away_team"])

    os.makedirs(PREDICTIONS_DIR, exist_ok=True)
    combined.to_csv(PREDICTIONS_FILE, index=False)

    # Fetch and save current season top scorers if API token is available
    scorers_saved = 0
    if args.api_token:
        try:
            print("\nFetching current season top scorers from API...")
            scorers_by_comp = load_top_scorers_from_api(args.api_token)
            scorers_saved = save_top_scorers(scorers_by_comp)
            print(f"  Saved top scorers for {scorers_saved} competitions")
        except Exception as exc:
            print(f"  Warning: Could not fetch/save scorers: {exc}")

    print(f"\nUpcoming fixtures found: {len(fixtures)}")
    print(f"Team mappings file: {TEAM_MAPPING_FILE}")
    print(f"Canonical raw-data names added: {canonical_added}")
    print(f"API names added from current fixtures: {added_api_names}")
    print(f"API names remapped/changed from resolver: {mapping_changes}")
    print(f"Predictions written: {len(new_df)}")
    print(f"Skipped (unmatched team names): {skipped}")
    print(f"Dropped by one-match-per-team-per-day rule: {single_match_dropped}")
    print(f"Removed completed fixtures from upcoming list: {removed_completed}")
    print(f"Newly settled with real results: {settled_count}")
    print(f"Saved tracking file: {PREDICTIONS_FILE}")


if __name__ == "__main__":
    main()
