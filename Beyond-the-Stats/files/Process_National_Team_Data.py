"""
National-team data pipeline — download, process, train, and predict.

Independent sub-pipeline for all international football.  Sources:
- ESPN scoreboards for historical results
- FIFA rankings (from a seed file / cached)
- Transfermarkt for squad market values (weekly only)

Produces:
- ``national_team_predictions.csv`` (upcoming fixtures)
- ``national_team_model_cache.pkl`` (trained model)
- World Cup group / knockout projections (shared with ``Project_World_Cup``)
"""
import argparse
import hashlib
import json
import math
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
import requests
from bs4 import BeautifulSoup

import joblib
import pandas as pd
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.preprocessing import LabelEncoder

try:
    from xgboost import XGBClassifier, XGBRegressor
except ImportError:
    XGBClassifier = None
    XGBRegressor = None


if __name__ == "__main__":
    sys.modules.setdefault("Process_National_Team_Data", sys.modules[__name__])


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NATIONAL_DATA_DIR = os.path.join(BASE_DIR, "Data", "National_Team_Data")
ESPN_CACHE_DIR = os.path.join(NATIONAL_DATA_DIR, "espn_scoreboard_cache")
TRANSFERMARKT_CACHE_DIR = os.path.join(NATIONAL_DATA_DIR, "transfermarkt_cache")
RAW_MATCHES_FILE = os.path.join(NATIONAL_DATA_DIR, "national_team_recent_matches_raw.csv")
PROCESSED_MATCHES_FILE = os.path.join(NATIONAL_DATA_DIR, "national_team_recent_context.csv")
MODEL_CACHE = os.path.join(NATIONAL_DATA_DIR, "national_team_model_cache.pkl")
API_REPORT_FILE = os.path.join(NATIONAL_DATA_DIR, "national_team_api_sources.json")
FIFA_RANKINGS_FILE = os.path.join(NATIONAL_DATA_DIR, "all_team_rankings.json")
SQUAD_VALUES_FILE = os.path.join(NATIONAL_DATA_DIR, "national_team_squad_values.json")

ESPN_SCOREBOARD_API = "https://site.api.espn.com/apis/site/v2/sports/soccer/{espn_id}/scoreboard"
FOOTBALL_DATA_API_BASE = "https://api.football-data.org/v4"
FOOTBALLDATA_IO_BASE = "https://footballdata.io/api/v1"
SPORTRADAR_FIFA_RANKINGS_URL = "https://api.sportradar.com/soccer-extended/trial/v4/en/fifa_rankings.json"
TRANSFERMARKT_CACHE_HOURS = max(0.0, float(os.getenv("TRANSFERMARKT_CACHE_HOURS", "24") or "24"))  # 24-hour cache (squad values change less frequently)
TRANSFERMARKT_BASE_URL = "https://www.transfermarkt.com"
TRANSFERMARKT_SEARCH_URL = "https://www.transfermarkt.com/schnellsuche/ergebnis/schnellsuche?query={query}"
TRANSFERMARKT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Referer": "https://www.transfermarkt.com/",
}
TRANSFERMARKT_FETCH_WORKERS = max(1, int(os.getenv("NATIONAL_TRANSFERMARKT_FETCH_WORKERS", "4") or "4"))
# Squad market values change slowly; refresh the aggregated file at most
# every SQUAD_VALUES_MAX_AGE_DAYS days (overridden via --squad-cache-days).
SQUAD_VALUES_MAX_AGE_DAYS = max(0, int(float(os.getenv("NATIONAL_SQUAD_CACHE_DAYS", "30") or "30")))
NATIONAL_TEAM_QUERY_ALIASES = {
    "United States": "USA",
    "USA": "USA",
    "Czechia": "Czech Republic",
    "Turkey": "Turkiye",
    "Turkiye": "Turkiye",
    "South Korea": "Korea Republic",
    "Korea Republic": "Korea Republic",
    "Bosnia-Herzegovina": "Bosnia-Herzegovina",
    "Ivory Coast": "Cote d'Ivoire",
    "Cote d'Ivoire": "Cote d'Ivoire",
    "Curacao": "Curacao",
    "Curaçao": "Curacao",
    "Cape Verde": "Cape Verde",
    "Cabo Verde": "Cape Verde",
    "Republic of Ireland": "Ireland",
    "Iran": "IR Iran",
    "FYR Macedonia": "North Macedonia",
    "DR Congo": "DR Congo",
    "Congo DR": "DR Congo",
    "Democratic Republic of the Congo": "DR Congo",
    "Congo, Democratic Republic of the": "DR Congo",
    "UAE": "United Arab Emirates",
    "China PR": "China",
    "Hong Kong": "Hongkong",
    "China": "China",
}
LAST_N_MATCHES = 15
DEFAULT_LOOKBACK_DAYS = 900
RESULT_LABELS = {"H", "D", "A"}
ESPN_FETCH_WORKERS = max(1, int(os.getenv("NATIONAL_ESPN_FETCH_WORKERS", "8") or "8"))
ESPN_CACHE_HOURS = max(0.0, float(os.getenv("NATIONAL_ESPN_CACHE_HOURS", "6") or "6"))
# Sklearn training knobs (mirrors Predict_Match.py).
CPU_COUNT = max(1, (os.cpu_count() or 1))
NATIONAL_TRAIN_WORKERS = int(os.getenv("NATIONAL_TRAIN_WORKERS", str(max(1, min(4, CPU_COUNT // 2)))))
NATIONAL_MODEL_THREADS = int(os.getenv("NATIONAL_MODEL_THREADS", str(max(1, CPU_COUNT // NATIONAL_TRAIN_WORKERS))))
EU_RANDOMIZER_MAX_DELTA = 0.08
DRAW_REDUCTION_FACTOR = 0.08
HIGH_DRAW_THRESHOLD = 0.42
HIGH_DRAW_EXTRA_REDUCTION_MAX = 0.18
CATEGORICAL_FEATURE_COLUMNS = ["competition", "stage"]
# Below this many completed recent matches we fall back to the current-context
# heuristic instead of training a sklearn model (too little data to learn from).
MIN_TRAINING_ROWS = 24


def d(start, end):
    return (start, end)


UPCOMING_ESPN_COMPETITIONS = {
    "International/World Cup": {"espn_id": "fifa.world", "priority": 1, "ranges": []},
    "International/World Cup Qualifying - UEFA": {"espn_id": "fifa.worldq.uefa", "priority": 2, "ranges": []},
    "International/World Cup Qualifying - CONMEBOL": {"espn_id": "fifa.worldq.conmebol", "priority": 3, "ranges": []},
    "International/World Cup Qualifying - CONCACAF": {"espn_id": "fifa.worldq.concacaf", "priority": 4, "ranges": []},
    "International/Friendly": {"espn_id": "fifa.friendly", "priority": 5, "ranges": []},
    "International/European Championship": {"espn_id": "uefa.euro", "priority": 6, "ranges": []},
    "South America/Copa America": {"espn_id": "conmebol.america", "priority": 7, "ranges": []},
    "North America/Gold Cup": {"espn_id": "concacaf.gold", "priority": 8, "ranges": []},
    "Africa/Africa Cup of Nations": {"espn_id": "caf.nations", "priority": 9, "ranges": []},
    "International/Nations League": {"espn_id": "uefa.nations", "priority": 10, "ranges": []},
}

RECENT_ESPN_COMPETITIONS = {
    **UPCOMING_ESPN_COMPETITIONS,
    "International/World Cup Qualifying - AFC": {"espn_id": "fifa.worldq.afc", "priority": 11, "ranges": []},
    "International/World Cup Qualifying - CAF": {"espn_id": "fifa.worldq.caf", "priority": 12, "ranges": []},
    "Asia/Asian Cup": {"espn_id": "afc.cup", "priority": 13, "ranges": []},
}

FOOTBALL_DATA_COMPETITIONS = {
    "WC": "International/World Cup",
    "EC": "International/European Championship",
}


def parse_cli_args():
    parser = argparse.ArgumentParser(
        description="Build a current-context national-team predictor from rankings, squad values, and last-15 games."
    )
    parser.add_argument("--skip-fetch", action="store_true", help="Use existing recent-match CSV/context files.")
    parser.add_argument(
        "--world-cup-only",
        action="store_true",
        help="Build context for current World Cup teams only. This is the default pipeline use.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help="How many days of ESPN national-team scoreboards to scan for last-15 matches.",
    )
    parser.add_argument(
        "--rankings-file",
        default=FIFA_RANKINGS_FILE,
        help="JSON/CSV file with current FIFA rankings. JSON can be {team:{rank,points}} or a list of rows.",
    )
    parser.add_argument(
        "--squad-values-file",
        default=SQUAD_VALUES_FILE,
        help="JSON/CSV file with latest national squad market values in EUR millions.",
    )
    parser.add_argument(
        "--footballdata-io-token",
        default=os.getenv("FOOTBALLDATA_IO_TOKEN", "").strip(),
        help="Optional Footballdata.io token for current FIFA ranking refresh.",
    )
    parser.add_argument(
        "--sportradar-api-key",
        default=os.getenv("SPORTRADAR_API_KEY", "").strip(),
        help="Optional Sportradar API key for current FIFA ranking refresh.",
    )
    parser.add_argument(
        "--refresh-squad-values",
        action="store_true",
        help="Search Transfermarkt for each target team and refresh national_team_squad_values.json before processing.",
    )
    parser.add_argument(
        "--squad-cache-days",
        type=int,
        default=SQUAD_VALUES_MAX_AGE_DAYS,
        help="Force a Transfermarkt refresh when the squad-values file is older than this many days. Set to 0 to disable the staleness override (the file is only refreshed when --refresh-squad-values is passed).",
    )
    parser.add_argument(
        "--skip-squad-values",
        action="store_true",
        help="Skip Transfermarkt squad value refreshes (weekly-only operation).",
    )
    return parser.parse_args()


def normalize_team_key(name):
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    parts = [part for part in text.split() if part and part not in {"fc", "cf", "team", "national", "football", "soccer", "the"}]
    aliases = {
        "usa": "unitedstates",
        "us": "unitedstates",
        "u s": "unitedstates",
        "unitedstatesofamerica": "unitedstates",
        "czechrepublic": "czechia",
        "turkiye": "turkey",
        "ivorycoast": "cotedivoire",
        "cotedivoire": "cotedivoire",
        "bosniaherzegovina": "bosniaandherzegovina",
        "bosniaherz": "bosniaandherzegovina",
        "curacao": "curacao",
        "korea republic": "southkorea",
        "republickorea": "southkorea",
    }
    key = "".join(parts)
    return aliases.get(key, key)


def canonical_team_name(name):
    text = str(name or "").strip()
    aliases = {
        "USA": "United States",
        "US": "United States",
        "Czech Republic": "Czechia",
        "Türkiye": "Turkey",
        "Korea Republic": "South Korea",
        "Bosnia and Herzegovina": "Bosnia-Herzegovina",
        "Côte d'Ivoire": "Ivory Coast",
        "Cote d'Ivoire": "Ivory Coast",
    }
    return aliases.get(text, text)


def make_prediction_key(match_date, competition, home_team, away_team):
    parsed = pd.to_datetime(match_date, utc=True, errors="coerce")
    date_part = parsed.strftime("%Y-%m-%d") if pd.notna(parsed) else str(match_date)[:10]
    pair = sorted([normalize_team_key(home_team), normalize_team_key(away_team)])
    return f"{date_part}|{competition}|{pair[0]}|{pair[1]}"


def fetch_json(url, headers=None, timeout=30):
    request = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def cache_path_for_url(url):
    # Hash the URL so each ESPN scoreboard day gets a stable filesystem-safe cache file.
    digest = hashlib.sha256(str(url).encode("utf-8")).hexdigest()
    return os.path.join(ESPN_CACHE_DIR, f"{digest}.json")


def load_cached_json(url, max_age_hours=ESPN_CACHE_HOURS):
    # Reuse recent scoreboard responses to make repeated World Cup runs much faster.
    cache_path = cache_path_for_url(url)
    if max_age_hours <= 0 or not os.path.exists(cache_path):
        return None
    cache_age_hours = (datetime.now(UTC).timestamp() - os.path.getmtime(cache_path)) / 3600.0
    if cache_age_hours > max_age_hours:
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return None


def write_cached_json(url, payload):
    # Write through a temp file so partial cache writes never poison future reads.
    os.makedirs(ESPN_CACHE_DIR, exist_ok=True)
    cache_path = cache_path_for_url(url)
    temp_path = f"{cache_path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False)
    os.replace(temp_path, cache_path)


def fetch_json_cached(url, headers=None, timeout=30, max_age_hours=ESPN_CACHE_HOURS):
    # Cache only default-header ESPN style calls; authenticated APIs still use direct fetch_json.
    if headers is None:
        cached = load_cached_json(url, max_age_hours=max_age_hours)
        if cached is not None:
            return cached
    payload = fetch_json(url, headers=headers, timeout=timeout)
    if headers is None:
        write_cached_json(url, payload)
    return payload


def transfermarkt_cache_path_for_url(url):
    # Hash the URL so each Transfermarkt search/squad page gets a stable filesystem-safe cache file.
    digest = hashlib.sha256(str(url).encode("utf-8")).hexdigest()
    return os.path.join(TRANSFERMARKT_CACHE_DIR, f"{digest}.html")


def load_cached_transfermarkt_html(url, max_age_hours=TRANSFERMARKT_CACHE_HOURS):
    cache_path = transfermarkt_cache_path_for_url(url)
    if max_age_hours <= 0 or not os.path.exists(cache_path):
        return None
    cache_age_hours = (datetime.now(UTC).timestamp() - os.path.getmtime(cache_path)) / 3600.0
    if cache_age_hours > max_age_hours:
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as file:
            return file.read()
    except Exception:
        return None


def write_cached_transfermarkt_html(url, html):
    os.makedirs(TRANSFERMARKT_CACHE_DIR, exist_ok=True)
    cache_path = transfermarkt_cache_path_for_url(url)
    temp_path = f"{cache_path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        file.write(html)
    os.replace(temp_path, cache_path)


def fetch_transfermarkt_html(url, retries=2, pause_seconds=1.5, timeout=30):
    last_exc = None
    for attempt in range(max(1, int(retries))):
        try:
            request = urllib.request.Request(url, headers=TRANSFERMARKT_HEADERS)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="ignore")
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < retries:
                time.sleep(pause_seconds)
                continue
            break
    if last_exc is not None:
        print(f"  Transfermarkt fetch failed for {url}: {last_exc}")
    return ""


def fetch_cached_transfermarkt_html(url, max_age_hours=TRANSFERMARKT_CACHE_HOURS, retries=2, pause_seconds=1.5, timeout=30):
    if max_age_hours > 0:
        cached = load_cached_transfermarkt_html(url, max_age_hours=max_age_hours)
        if cached is not None:
            return cached
    html = fetch_transfermarkt_html(url, retries=retries, pause_seconds=pause_seconds, timeout=timeout)
    if html and max_age_hours > 0:
        write_cached_transfermarkt_html(url, html)
    return html


def fetch_espn_scoreboard_day(espn_id, day, timeout=30, max_age_hours=ESPN_CACHE_HOURS):
    # Centralize ESPN day URL building so callers collect the same data source consistently.
    url = ESPN_SCOREBOARD_API.format(espn_id=espn_id) + f"?dates={day.strftime('%Y%m%d')}"
    return fetch_json_cached(url, timeout=timeout, max_age_hours=max_age_hours)


def fetch_espn_scoreboard_days(espn_id, days, timeout=30, max_age_hours=ESPN_CACHE_HOURS, workers=ESPN_FETCH_WORKERS):
    # Fetch scoreboard days concurrently while returning rows in calendar order for deterministic parsing.
    ordered_days = list(days)
    if not ordered_days:
        return []
    max_workers = max(1, min(int(workers), len(ordered_days)))
    if max_workers == 1:
        return [(day, fetch_espn_scoreboard_day(espn_id, day, timeout=timeout, max_age_hours=max_age_hours)) for day in ordered_days]

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_day = {
            executor.submit(fetch_espn_scoreboard_day, espn_id, day, timeout, max_age_hours): day
            for day in ordered_days
        }
        for future in as_completed(future_to_day):
            day = future_to_day[future]
            try:
                results[day] = future.result()
            except Exception:
                results[day] = None
    return [(day, results.get(day)) for day in ordered_days if results.get(day) is not None]


def date_range(start_date, end_date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def parse_number(value, default=0.0):
    if value is None:
        return default
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    if not match:
        return default
    try:
        return float(match.group(0))
    except ValueError:
        return default


def _competitors_by_side(competitors):
    by_side = {}
    ordered = list(competitors or [])
    for competitor in ordered:
        side = str(competitor.get("homeAway", "")).strip().lower()
        if side in {"home", "away"}:
            by_side[side] = competitor
    if "home" not in by_side and ordered:
        by_side["home"] = ordered[0]
    if "away" not in by_side and len(ordered) > 1:
        by_side["away"] = ordered[1]
    return by_side


def parse_espn_event(event, competition_name, require_completed=True):
    competitions = event.get("competitions") or []
    if not competitions:
        return None
    competition = competitions[0] or {}
    status_type = ((competition.get("status") or {}).get("type") or {})
    state = str(status_type.get("state", "")).strip().lower()
    completed = bool(status_type.get("completed")) or state == "post"
    if require_completed and not completed:
        return None

    by_side = _competitors_by_side(competition.get("competitors") or [])
    home = by_side.get("home")
    away = by_side.get("away")
    if not home or not away:
        return None

    home_team = canonical_team_name(((home.get("team") or {}).get("displayName") or "").strip())
    away_team = canonical_team_name(((away.get("team") or {}).get("displayName") or "").strip())
    if not home_team or not away_team:
        return None

    event_dt = pd.to_datetime(event.get("date"), utc=True, errors="coerce")
    if pd.isna(event_dt):
        return None

    home_goals = parse_number(home.get("score"), default=None)
    away_goals = parse_number(away.get("score"), default=None)
    result = ""
    if completed and home_goals is not None and away_goals is not None:
        if home_goals > away_goals:
            result = "H"
        elif away_goals > home_goals:
            result = "A"
        elif bool(home.get("winner")) and not bool(away.get("winner")):
            result = "H"
        elif bool(away.get("winner")) and not bool(home.get("winner")):
            result = "A"
        else:
            result = "D"

    stage = str((event.get("season") or {}).get("slug", "") or "").strip().lower() or "unknown"
    venue = competition.get("venue") or {}
    return {
        "match_id": str(event.get("id", "")).strip(),
        "match_datetime_utc": event_dt.isoformat(),
        "match_date": event_dt.strftime("%Y-%m-%d"),
        "competition": competition_name,
        "stage": stage,
        "home_team": home_team,
        "away_team": away_team,
        "FTHG": int(home_goals) if home_goals is not None else None,
        "FTAG": int(away_goals) if away_goals is not None else None,
        "FTR": result,
        "status": str(status_type.get("name", "") or status_type.get("description", "")).strip(),
        "is_neutral_site": bool(competition.get("neutralSite")) if competition.get("neutralSite") is not None else True,
        "venue": str(venue.get("fullName", "") or venue.get("name", "") or "").strip(),
        "source": "espn",
    }


def parse_football_data_match(match, competition_name, completed_only=True):
    status = str(match.get("status", "")).strip().upper()
    if completed_only and status not in {"FINISHED", "FULL_TIME"}:
        return None
    home_team = canonical_team_name(((match.get("homeTeam") or {}).get("name") or "").strip())
    away_team = canonical_team_name(((match.get("awayTeam") or {}).get("name") or "").strip())
    parsed_dt = pd.to_datetime(match.get("utcDate"), utc=True, errors="coerce")
    if not home_team or not away_team or pd.isna(parsed_dt):
        return None

    score = match.get("score") or {}
    full_time = score.get("fullTime") or {}
    home_goals = full_time.get("home")
    away_goals = full_time.get("away")
    winner = str(score.get("winner", "")).strip().upper()
    result = ""
    if home_goals is not None and away_goals is not None:
        if home_goals > away_goals or winner == "HOME_TEAM":
            result = "H"
        elif away_goals > home_goals or winner == "AWAY_TEAM":
            result = "A"
        else:
            result = "D"

    return {
        "match_id": str(match.get("id", "")).strip(),
        "match_datetime_utc": parsed_dt.isoformat(),
        "match_date": parsed_dt.strftime("%Y-%m-%d"),
        "competition": competition_name,
        "stage": str(match.get("stage", "") or "unknown").strip().lower(),
        "home_team": home_team,
        "away_team": away_team,
        "FTHG": int(home_goals) if home_goals is not None else None,
        "FTAG": int(away_goals) if away_goals is not None else None,
        "FTR": result,
        "status": status,
        "is_neutral_site": True,
        "venue": str(match.get("venue", "") or "").strip(),
        "source": "football-data.org",
    }


def fetch_world_cup_team_names(start_date="2026-06-11", end_date="2026-06-27"):
    teams = set()
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    # Pull group-stage days concurrently; parsing stays ordered and unchanged.
    for _, payload in fetch_espn_scoreboard_days("fifa.world", date_range(start, end), timeout=30):
        for event in payload.get("events") or []:
            parsed = parse_espn_event(event, "International/World Cup", require_completed=False)
            if parsed:
                for side in ["home_team", "away_team"]:
                    name = parsed.get(side, "")
                    if name and not is_placeholder_team(name):
                        teams.add(name)
    return sorted(teams)


def is_placeholder_team(name):
    text = str(name or "").lower()
    return any(token in text for token in ["group ", "winner", "third place", "round of", "quarterfinal", "semifinal"])


def fetch_recent_espn_matches(target_teams, lookback_days):
    today = datetime.now(UTC).date()
    start = today - timedelta(days=max(30, int(lookback_days)))
    target_keys = {normalize_team_key(team) for team in target_teams}
    by_team = {team: [] for team in target_teams}
    target_lookup = defaultdict(list)
    for team in target_teams:
        target_lookup[normalize_team_key(team)].append(team)
    all_rows = []
    seen = set()

    for competition_name, config in sorted(RECENT_ESPN_COMPETITIONS.items(), key=lambda item: item[1]["priority"]):
        espn_id = config["espn_id"]
        print(f"Scanning ESPN {competition_name} for recent national-team matches...")
        # Fetch each competition's date window in parallel, then parse in calendar order.
        for _, payload in fetch_espn_scoreboard_days(espn_id, date_range(start, today), timeout=30):
            for event in payload.get("events") or []:
                event_id = str(event.get("id", "")).strip()
                if event_id and event_id in seen:
                    continue
                parsed = parse_espn_event(event, competition_name, require_completed=True)
                if not parsed or str(parsed.get("FTR", "")).strip() not in RESULT_LABELS:
                    continue
                home_key = normalize_team_key(parsed["home_team"])
                away_key = normalize_team_key(parsed["away_team"])
                if home_key not in target_keys and away_key not in target_keys:
                    continue
                if event_id:
                    seen.add(event_id)
                all_rows.append(parsed)
                # Store matches for only the teams involved; final last-15 selection happens after sorting.
                for team in target_lookup.get(home_key, []) + target_lookup.get(away_key, []):
                    by_team[team].append(parsed)
        for team, matches in by_team.items():
            # Keep the newest last-15 matches per team so extra fetched history cannot dilute form quality.
            by_team[team] = sorted(matches, key=lambda row: row["match_datetime_utc"], reverse=True)[:LAST_N_MATCHES]
        if all(len(matches) >= LAST_N_MATCHES for matches in by_team.values()):
            break
    all_rows.sort(key=lambda row: row["match_datetime_utc"])
    return all_rows, by_team


def load_json_or_csv_records(path):
    if not path or not os.path.exists(path):
        return None
    if path.endswith(".csv"):
        return pd.read_csv(path).to_dict("records")
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def normalize_rankings_payload(payload):
    rankings = {}
    if isinstance(payload, dict):
        if "rankings" in payload:
            for ranking_group in payload.get("rankings") or []:
                for row in ranking_group.get("competitor_rankings") or []:
                    competitor = row.get("competitor") or {}
                    name = canonical_team_name(competitor.get("name") or competitor.get("country") or "")
                    if name:
                        rankings[name] = {"rank": int(row.get("rank", 999)), "points": float(row.get("points", 0.0) or 0.0)}
            return rankings
        for team, value in payload.items():
            team_name = canonical_team_name(team)
            if isinstance(value, dict):
                rankings[team_name] = {
                    "rank": int(value.get("rank", value.get("position", 999)) or 999),
                    "points": float(value.get("points", value.get("rating", 0.0)) or 0.0),
                }
            else:
                rankings[team_name] = {"rank": int(value or 999), "points": 0.0}
        return rankings
    if isinstance(payload, list):
        for row in payload:
            if not isinstance(row, dict):
                continue
            name = canonical_team_name(row.get("team") or row.get("country") or row.get("name") or "")
            if not name:
                continue
            rankings[name] = {
                "rank": int(row.get("rank", row.get("position", 999)) or 999),
                "points": float(row.get("points", row.get("rating", 0.0)) or 0.0),
            }
    return rankings


def fetch_rankings_from_footballdata_io(token):
    if not token:
        return {}
    url = f"{FOOTBALLDATA_IO_BASE}/fifa-rankings/current?ranking_type=men"
    try:
        payload = fetch_json(url, headers={"Authorization": f"Bearer {token}"}, timeout=45)
    except Exception as exc:
        print(f"Footballdata.io FIFA rankings fetch failed: {exc}")
        return {}
    return normalize_rankings_payload(payload)


def fetch_rankings_from_sportradar(api_key):
    if not api_key:
        return {}
    query = urllib.parse.urlencode({"api_key": api_key})
    try:
        payload = fetch_json(f"{SPORTRADAR_FIFA_RANKINGS_URL}?{query}", timeout=45)
    except Exception as exc:
        print(f"Sportradar FIFA rankings fetch failed: {exc}")
        return {}
    return normalize_rankings_payload(payload)


""" def fetch_rankings_from_fifa_official():
    

    
    try:
        print("Fetching official FIFA rankings from inside.fifa.com...")
        url = "https://inside.fifa.com/fifa-world-ranking/men"
        
        # Try direct JSON API that the FIFA website uses
        api_url = "https://inside.fifa.com/api/rankings?ranking_type=men&current=true&limit=300"
        try:
            payload = fetch_json(api_url, timeout=45)
            if "rankings" in payload and isinstance(payload["rankings"], list):
                rankings = {}
                for entry in payload["rankings"]:
                    team_name = canonical_team_name(entry.get("team_name", ""))
                    if team_name:
                        rankings[team_name] = {
                            "rank": int(entry.get("rank", 999)),
                            "points": float(entry.get("rating", entry.get("points", 0.0)))
                        }
                if rankings:
                    print(f"✓ Fetched {len(rankings)} official FIFA rankings")
                    return rankings
        except Exception as e:
            print(f"  FIFA API attempt failed: {e}")
        
        # Fallback: Try common CDN/API endpoints
        endpoints = [
            "https://api.fifa.com/rankings?type=men",
            "https://cdn.fifa.com/api/rankings?ranking_type=men",
        ]
        
        for endpoint in endpoints:
            try:
                payload = fetch_json(endpoint, timeout=30)
                if isinstance(payload, dict) and ("rankings" in payload or "data" in payload):
                    items = payload.get("rankings", payload.get("data", []))
                    if items:
                        rankings = {}
                        for item in items:
                            team_name = canonical_team_name(item.get("team", item.get("team_name", "")))
                            if team_name:
                                rankings[team_name] = {
                                    "rank": int(item.get("rank", item.get("position", 999))),
                                    "points": float(item.get("points", item.get("rating", 0.0)))
                                }
                        if rankings:
                            print(f"✓ Fetched {len(rankings)} FIFA rankings from {endpoint}")
                            return rankings
            except Exception as e:
                continue
        
        return {}
    except Exception as e:
        print(f"ERROR fetching official FIFA rankings: {e}")
        return {}
    

    
    try:
        print("Computing team rankings from actual ESPN match data...")
        # We'll compute this from the match history we fetch
        # This is real, verifiable data based on actual results
        return compute_elo_rankings_from_matches()
    except Exception as e:
        print(f"Match-based ranking computation failed: {e}")
    return {} """

def fetch_rankings_from_fifa_official():
    """
    Scrape FIFA Men's World Rankings from the official FIFA page
    and return data in the format:

    {
        "Argentina": {
            "rank": 1,
            "points": 1886.16
        },
        ...
    }
    """

    try:
        print("Fetching FIFA rankings from official website...")

        url = "https://inside.fifa.com/fifa-world-ranking/men"

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            )
        }

        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        rankings = {}

        # Look for table rows
        rows = soup.select("table tbody tr")

        if not rows:
            print("No ranking rows found on page.")
            return {}

        for row in rows:
            cols = row.find_all(["td", "th"])

            if len(cols) < 3:
                continue

            try:
                rank_text = cols[0].get_text(strip=True)
                team_text = cols[1].get_text(strip=True)
                points_text = cols[2].get_text(strip=True)

                # Extract numeric values
                rank_match = re.search(r"\d+", rank_text)
                points_match = re.search(
                    r"[\d,.]+",
                    points_text.replace(",", "")
                )

                if not rank_match or not team_text:
                    continue

                rank = int(rank_match.group())

                points = (
                    float(points_match.group())
                    if points_match
                    else 0.0
                )

                team_name = canonical_team_name(team_text)

                rankings[team_name] = {
                    "rank": rank,
                    "points": points
                }

            except Exception as row_error:
                print(f"Skipping row: {row_error}")
                continue

        print(f"✓ Scraped {len(rankings)} FIFA rankings")

        return rankings

    except Exception as e:
        print(f"ERROR fetching FIFA rankings: {e}")
        return {}



def compute_elo_rankings_from_matches(start_days_back=730):
    """
    Compute live rankings using Elo rating system from actual match results.
    This is REAL data - derived from actual verified match outcomes.
    """
    try:
        today = datetime.now(UTC).date()
        start = today - timedelta(days=start_days_back)
        
        # Fetch recent international matches from ESPN
        elo_ratings = {}  # {team: rating}
        match_counts = {}  # Track games per team
        
        for competition_name, config in sorted(RECENT_ESPN_COMPETITIONS.items(), key=lambda item: item[1]["priority"])[:3]:
            espn_id = config["espn_id"]
            try:
                print(f"  Fetching {competition_name} matches for Elo calculation...")
                for _, payload in fetch_espn_scoreboard_days(espn_id, date_range(start, today), timeout=30):
                    for event in payload.get("events") or []:
                        parsed = parse_espn_event(event, competition_name, require_completed=True)
                        if not parsed or str(parsed.get("FTR", "")).strip() not in RESULT_LABELS:
                            continue
                        
                        home = canonical_team_name(parsed["home_team"])
                        away = canonical_team_name(parsed["away_team"])
                        result = parsed["FTR"]
                        
                        # Initialize ratings at 1600 (international average)
                        if home not in elo_ratings:
                            elo_ratings[home] = 1600.0
                            match_counts[home] = 0
                        if away not in elo_ratings:
                            elo_ratings[away] = 1600.0
                            match_counts[away] = 0
                        
                        # Simple Elo update
                        h_rating = elo_ratings[home]
                        a_rating = elo_ratings[away]
                        expected_h = 1.0 / (1.0 + 10.0 ** ((a_rating - h_rating) / 400.0))
                        expected_a = 1.0 - expected_h
                        
                        if result == "H":
                            h_score, a_score = 1.0, 0.0
                        elif result == "A":
                            h_score, a_score = 0.0, 1.0
                        else:
                            h_score, a_score = 0.5, 0.5
                        
                        k = 32  # K-factor for international matches
                        elo_ratings[home] = h_rating + k * (h_score - expected_h)
                        elo_ratings[away] = a_rating + k * (a_score - expected_a)
                        match_counts[home] += 1
                        match_counts[away] += 1
            except Exception as e:
                print(f"    Error fetching {competition_name}: {e}")
                continue
        
        # Convert Elo ratings to FIFA-style rankings
        if not elo_ratings:
            return {}
        
        sorted_teams = sorted(elo_ratings.items(), key=lambda x: -x[1])
        rankings = {}
        for idx, (team, rating) in enumerate(sorted_teams, 1):
            games = match_counts.get(team, 0)
            if games >= 3:  # Only include teams with at least 3 matches
                rankings[team] = {
                    "rank": idx,
                    "points": round(rating, 1)
                }
        
        print(f"✓ Computed live rankings for {len(rankings)} teams from {sum(match_counts.values())} matches")
        return rankings
    
    except Exception as e:
        print(f"ERROR computing Elo rankings: {e}")
        return {}


def load_fifa_rankings(path, footballdata_io_token="", sportradar_api_key=""):
    # Priority: pre-saved all_team_rankings.json > user-provided --rankings-file > API tokens
    rankings = {}
    ALL_RANKINGS_FILE = os.path.join(NATIONAL_DATA_DIR, "all_team_rankings.json")
    if os.path.exists(ALL_RANKINGS_FILE):
        try:
            with open(ALL_RANKINGS_FILE, "r", encoding="utf-8") as f:
                payload = json.load(f)
            file_rankings = normalize_rankings_payload(payload)
            if file_rankings:
                rankings.update(file_rankings)
                print(f"Loaded {len(file_rankings)} rankings from all_team_rankings.json")
        except Exception as e:
            print(f"Warning: Could not load rankings from all_team_rankings.json: {e}")

    if len(rankings) < 50 and path and os.path.exists(path) and os.path.abspath(path) != os.path.abspath(ALL_RANKINGS_FILE):
        try:
            payload = load_json_or_csv_records(path)
            if payload:
                file_rankings = normalize_rankings_payload(payload)
                if file_rankings:
                    rankings.update(file_rankings)
                    print(f"Loaded {len(file_rankings)} rankings from {path}")
        except Exception as e:
            print(f"Warning: Could not load rankings from {path}: {e}")

    return rankings


def save_complete_rankings(rankings):
    """should be removed when i have a chance*****"""
    print(f"REMOVE LATER")


def parse_value_to_eur_m(value):
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "")
    multiplier = 1.0
    lowered = text.lower()
    # Match the unit only when it's preceded by a digit or whitespace and
    # followed by a non-letter (or end of string). This avoids false matches
    # like the "k" in "market" or the "b" in "bolivia".
    if re.search(r"(?<=\d|\s)(bn|billion)\b", lowered):
        multiplier = 1000.0
    elif re.search(r"(?<=\d|\s)(thsd|thousand)\b", lowered):
        multiplier = 0.001
    elif re.search(r"(?<=\d|\s)k\b", lowered):
        multiplier = 0.001
    # NOTE: bare "m" is ambiguous (matches "m" in "market"/"million"/etc.),
    # so we don't treat it as a unit. Numbers without a recognised unit are
    # assumed to already be in EUR millions.
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return 0.0
    return float(match.group(0)) * multiplier


def normalize_squad_values_payload(payload):
    values = {}
    if isinstance(payload, dict):
        # New wrapped format: {"generated_at_utc": ..., "teams": {team: {...}}}
        if isinstance(payload.get("teams"), dict):
            payload = payload["teams"]
        for team, value in payload.items():
            if team in {"generated_at_utc", "source", "cache_hours", "season", "source_file", "total_teams", "summary"}:
                continue
            team_name = canonical_team_name(team)
            if isinstance(value, dict):
                raw = value.get("squad_value_eur_m", value.get("market_value_eur_m", value.get("value", 0.0)))
                updated = value.get("updated_at", "")
            else:
                raw = value
                updated = ""
            values[team_name] = {"squad_value_eur_m": parse_value_to_eur_m(raw), "updated_at": str(updated)}
    elif isinstance(payload, list):
        for row in payload:
            if not isinstance(row, dict):
                continue
            name = canonical_team_name(row.get("team") or row.get("country") or row.get("name") or "")
            if not name:
                continue
            raw = row.get("squad_value_eur_m", row.get("market_value_eur_m", row.get("value", row.get("market_value", 0.0))))
            values[name] = {"squad_value_eur_m": parse_value_to_eur_m(raw), "updated_at": str(row.get("updated_at", ""))}
    return values


def _read_squad_values_payload(path):
    """Read the raw JSON wrapper of the squad-values file (with `generated_at_utc`).

    Returns an empty dict if the file is missing, empty, or unreadable.
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as file:
            payload = json.load(file)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _squad_values_age_days(payload, now=None):
    """Return the age in days of the squad-values payload based on `generated_at_utc`.

    Returns math.inf if the timestamp is missing or unparseable.
    """
    if not isinstance(payload, dict):
        return math.inf
    stamp = payload.get("generated_at_utc")
    if not stamp:
        return math.inf
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except Exception:
        return math.inf
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return (reference - parsed).total_seconds() / 86400.0


def load_squad_values(path, refresh=False, target_teams=None, max_workers=TRANSFERMARKT_FETCH_WORKERS, max_age_days=None):
    """Load squad market values from external sources. Optionally refresh from Transfermarkt first.

    When `max_age_days` is set and the existing file is older than that window,
    `refresh` is forced on for the squad refresh step (the rest of the function
    still reads the freshly written file). Pass `max_age_days=0` to disable the
    staleness override.
    """
    if max_age_days is None:
        max_age_days = SQUAD_VALUES_MAX_AGE_DAYS
    effective_refresh = bool(refresh)
    if not effective_refresh and max_age_days and max_age_days > 0:
        existing = _read_squad_values_payload(path)
        age_days = _squad_values_age_days(existing)
        if age_days >= max_age_days:
            existing_teams = existing.get("teams") if isinstance(existing, dict) else None
            team_count = len(existing_teams) if isinstance(existing_teams, dict) else 0
            print(
                f"Squad values file is {age_days:.1f} days old (>{max_age_days}d threshold, "
                f"{team_count} teams) — forcing a refresh from Transfermarkt."
            )
            effective_refresh = True
    if effective_refresh and target_teams:
        try:
            build_squad_values_file(list(target_teams), refresh=True, max_workers=max_workers)
        except Exception as exc:
            print(f"WARNING: Failed to refresh squad values from Transfermarkt: {exc}")
    values = {}
    payload = load_json_or_csv_records(path)
    if payload:
        values.update(normalize_squad_values_payload(payload))
    if not values:
        print("WARNING: No squad values loaded. Provide --squad-values-file or ensure it exists.")
        print("         Squad values are optional but improve prediction accuracy.")
    return {canonical_team_name(team): value for team, value in values.items()}


_NATIONAL_TEAM_YOUTH_PATTERN = re.compile(r"\bU[\s\-]?(1[5-9]|2[0-3])\b", re.IGNORECASE)


def _is_youth_national_team(name):
    return bool(_NATIONAL_TEAM_YOUTH_PATTERN.search(str(name or "")))


def _extract_search_candidates(soup):
    # The Transfermarkt search results now live in <div id="club-grid"> wrapping
    # a <table class="items">. The previous layout used <table id="club-grid">
    # directly. Handle both.
    grid = soup.find(id="club-grid")
    table = None
    if grid is not None:
        table = grid.find("table") or (grid if grid.name == "table" else None)
    if table is None:
        table = soup.find("table", {"id": "club-grid"})
    if table is None:
        # Last-resort fallback: the items table that BOTH has flaggenrahmen
        # images (the national-team signal) AND links to /startseite/verein/<id>.
        for candidate in soup.find_all("table", class_="items"):
            if (
                candidate.find("img", class_="flaggenrahmen")
                and candidate.find("a", href=re.compile(r"/startseite/verein/\d+"))
            ):
                table = candidate
                break
    if table is None:
        return []

    candidates = []
    for row in table.find_all("tr"):
        # In the new layout each row has a club/team logo as the first <img>
        # and a separate <img class="flaggenrahmen"> for the country flag. We
        # must look for the flag specifically, not the first image.
        flag_img = row.find("img", class_="flaggenrahmen")
        if flag_img is None:
            continue
        flag_src = flag_img.get("src", "")
        if "flagge/" not in flag_src:
            continue

        link = row.find("a", href=re.compile(r"/startseite/verein/\d+"))
        if link is None:
            hauptlink_cell = row.find("td", class_="hauptlink")
            if hauptlink_cell is not None:
                link = hauptlink_cell.find("a", href=re.compile(r"/startseite/verein/\d+"))
        if link is None:
            continue

        href = link.get("href", "")
        name = link.get_text(strip=True) or flag_img.get("alt", "")
        match = re.search(r"/startseite/verein/(\d+)", href)
        if not match:
            continue
        team_id = match.group(1)
        candidates.append({"name": name, "href": href, "team_id": team_id, "is_youth": _is_youth_national_team(name)})
    return candidates


def find_transfermarkt_national_team_link(team_name):
    query = NATIONAL_TEAM_QUERY_ALIASES.get(team_name, team_name)
    if not query:
        return None, None
    encoded = urllib.parse.quote(str(query))
    url = TRANSFERMARKT_SEARCH_URL.format(query=encoded)
    html = fetch_cached_transfermarkt_html(url)
    if not html:
        return None, None
    soup = BeautifulSoup(html, "html.parser")
    candidates = _extract_search_candidates(soup)
    if not candidates:
        return None, None
    senior = [c for c in candidates if not c["is_youth"]]
    pool = senior or candidates
    target_key = normalize_team_key(query)
    for candidate in pool:
        if normalize_team_key(candidate["name"]) == target_key:
            return f"{TRANSFERMARKT_BASE_URL}{candidate['href']}", candidate["team_id"]
    name_key = normalize_team_key(team_name)
    for candidate in pool:
        if normalize_team_key(candidate["name"]) == name_key:
            return f"{TRANSFERMARKT_BASE_URL}{candidate['href']}", candidate["team_id"]
    chosen = pool[0]
    return f"{TRANSFERMARKT_BASE_URL}{chosen['href']}", chosen["team_id"]


def _parse_data_header_info_box(soup):
    info = {"squad_size": None, "average_age": None, "foreigners": None, "fifa_world_ranking": None}
    # New layout: each row is <li class="data-header__label"> containing the
    # label text and a sibling <span class="data-header__content">. Older
    # layouts had separate <span class="data-header__label">/content pairs;
    # accept both.
    for box in soup.find_all("div", class_="data-header__info-box"):
        for label_el in box.find_all("li", class_="data-header__label"):
            content_el = label_el.find("span", class_="data-header__content")
            if content_el is None:
                continue
            label = label_el.get_text(" ", strip=True).lower()
            value = content_el.get_text(" ", strip=True)
            if "squad" in label and "size" in label:
                match = re.search(r"\d+", value)
                if match:
                    info["squad_size"] = int(match.group(0))
            elif "average age" in label:
                try:
                    info["average_age"] = float(value.replace(",", "."))
                except ValueError:
                    pass
            elif "foreigners" in label:
                match = re.search(r"\d+", value)
                if match:
                    info["foreigners"] = int(match.group(0))
            elif "fifa" in label and "ranking" in label:
                match = re.search(r"\d+", value)
                if match:
                    info["fifa_world_ranking"] = int(match.group(0))
        if all(v is not None for v in info.values()):
            break
        for label_el in box.find_all("span", class_="data-header__label"):
            content_el = label_el.find_next("span", class_="data-header__content")
            if content_el is None:
                continue
            label = label_el.get_text(strip=True).lower()
            value = content_el.get_text(strip=True)
            if "squad" in label and "size" in label and info["squad_size"] is None:
                match = re.search(r"\d+", value)
                if match:
                    info["squad_size"] = int(match.group(0))
            elif "average age" in label and info["average_age"] is None:
                try:
                    info["average_age"] = float(value.replace(",", "."))
                except ValueError:
                    pass
            elif "foreigners" in label and info["foreigners"] is None:
                match = re.search(r"\d+", value)
                if match:
                    info["foreigners"] = int(match.group(0))
            elif "fifa" in label and "ranking" in label and info["fifa_world_ranking"] is None:
                match = re.search(r"\d+", value)
                if match:
                    info["fifa_world_ranking"] = int(match.group(0))
    return info


def fetch_national_team_squad_value(team_url, team_id=None):
    if not team_url:
        return None
    html = fetch_cached_transfermarkt_html(team_url)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    wrapper = soup.find("a", class_="data-header__market-value-wrapper")
    if wrapper is None:
        return None
    currency_spans = wrapper.find_all("span", class_="waehrung")
    raw_text = wrapper.get_text(" ", strip=True)
    squad_value_eur_m = 0.0
    if currency_spans:
        try:
            squad_value_eur_m = parse_value_to_eur_m(raw_text)
        except Exception:
            squad_value_eur_m = 0.0
    if squad_value_eur_m <= 0:
        return None
    info = _parse_data_header_info_box(soup)
    return {
        "squad_value_eur_m": round(squad_value_eur_m, 2),
        "raw_value_text": raw_text,
        "squad_size": info["squad_size"],
        "average_age": info["average_age"],
        "foreigners": info["foreigners"],
        "fifa_world_ranking_on_page": info["fifa_world_ranking"],
        "team_id": str(team_id) if team_id else "",
        "source_url": team_url,
        "updated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }


def _load_existing_squad_values():
    if not os.path.exists(SQUAD_VALUES_FILE):
        return {}
    try:
        with open(SQUAD_VALUES_FILE, "r", encoding="utf-8") as file:
            payload = json.load(file) or {}
    except Exception:
        return {}
    teams = payload.get("teams", {}) if isinstance(payload, dict) else {}
    if not isinstance(teams, dict):
        return {}
    return {canonical_team_name(name): value for name, value in teams.items() if isinstance(value, dict)}


def _build_single_team_record(team_name, refresh=True):
    if not refresh:
        return None
    team_url, team_id = find_transfermarkt_national_team_link(team_name)
    if not team_url:
        return None
    return fetch_national_team_squad_value(team_url, team_id)


def build_squad_values_file(target_teams, refresh=True, max_workers=TRANSFERMARKT_FETCH_WORKERS):
    target_teams = [canonical_team_name(team) for team in target_teams if team]
    target_teams = [team for team in target_teams if team]
    if not target_teams:
        print("WARNING: build_squad_values_file called with no teams.")
        return None
    os.makedirs(TRANSFERMARKT_CACHE_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(SQUAD_VALUES_FILE), exist_ok=True)
    previous_values = _load_existing_squad_values()
    results = {}
    if not refresh:
        output = {
            "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "source": "transfermarkt.com",
            "cache_hours": TRANSFERMARKT_CACHE_HOURS,
            "teams": results,
        }
        with open(SQUAD_VALUES_FILE, "w", encoding="utf-8") as file:
            json.dump(output, file, indent=2, ensure_ascii=False)
        return output
    print(f"Searching Transfermarkt for {len(target_teams)} national team squad values...")

    def _resolve(team_name):
        try:
            value_data = _build_single_team_record(team_name, refresh=True)
        except Exception as exc:
            print(f"  Error fetching squad value for {team_name}: {exc}")
            value_data = None
        return team_name, value_data

    max_workers = max(1, min(int(max_workers), max(1, len(target_teams))))
    if max_workers == 1:
        resolved = [_resolve(team) for team in target_teams]
    else:
        resolved = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_team = {executor.submit(_resolve, team): team for team in target_teams}
            for future in as_completed(future_to_team):
                try:
                    resolved.append(future.result())
                except Exception as exc:
                    team = future_to_team[future]
                    print(f"  Error fetching squad value for {team}: {exc}")
                    resolved.append((team, None))

    for team_name, value_data in resolved:
        if value_data and value_data.get("squad_value_eur_m", 0) > 0:
            results[team_name] = {**value_data, "status": "ok"}
            print(f"  {team_name}: €{value_data['squad_value_eur_m']:.2f}m")
        else:
            previous = previous_values.get(team_name) or {}
            if previous.get("squad_value_eur_m", 0) > 0:
                results[team_name] = {
                    "squad_value_eur_m": previous["squad_value_eur_m"],
                    "updated_at": previous.get("updated_at", datetime.now(UTC).replace(microsecond=0).isoformat()),
                    "status": "cached",
                    "source_url": previous.get("source_url", ""),
                    "team_id": previous.get("team_id", ""),
                }
                print(f"  {team_name}: cached (€{previous['squad_value_eur_m']:.2f}m)")
            else:
                results[team_name] = {
                    "squad_value_eur_m": 0.0,
                    "updated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
                    "status": "fetch_failed",
                }
                print(f"  {team_name}: fetch failed")

    output = {
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "source": "transfermarkt.com",
        "cache_hours": TRANSFERMARKT_CACHE_HOURS,
        "teams": results,
    }
    with open(SQUAD_VALUES_FILE, "w", encoding="utf-8") as file:
        json.dump(output, file, indent=2, ensure_ascii=False)
    print(f"Saved national team squad values to {SQUAD_VALUES_FILE}")
    return output


def result_for_team(row, team):
    is_home = normalize_team_key(row["home_team"]) == normalize_team_key(team)
    gf = int(row["FTHG"] if is_home else row["FTAG"])
    ga = int(row["FTAG"] if is_home else row["FTHG"])
    if gf > ga:
        result = "W"
        points = 3
    elif gf < ga:
        result = "L"
        points = 0
    else:
        result = "D"
        points = 1
    return result, points, gf, ga


def opponent_strength_multiplier(opponent_team, rankings):
    """Calculate a multiplier based on opponent's FIFA ranking quality."""
    opponent_rank = rankings.get(canonical_team_name(opponent_team), {}).get("rank", 999)
    # Scale from rank: better teams (lower rank) get higher multipliers
    # Rank 1 = 1.5x, Rank 50 = 1.0x, Rank 100+ = 0.8x
    if opponent_rank <= 20:
        multiplier = 1.5 - (opponent_rank - 1) * 0.015  # 1.5 to ~1.22
    elif opponent_rank <= 50:
        multiplier = 1.2 - (opponent_rank - 20) * 0.009  # 1.2 to ~1.07
    else:
        multiplier = max(0.7, 1.08 - (opponent_rank - 50) * 0.004)  # 1.08 down to 0.7
    return round(max(0.7, min(1.5, multiplier)), 4)


def summarize_last_matches(team, matches, rankings=None):
    if rankings is None:
        rankings = {}
    ordered = sorted(matches, key=lambda row: row["match_datetime_utc"], reverse=True)[:LAST_N_MATCHES]
    if not ordered:
        return {
            "games": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "points": 0,
            "points_per_game": 0.0,
            "goals_for": 0,
            "goals_against": 0,
            "goal_diff": 0,
            "goals_for_per_game": 0.0,
            "goals_against_per_game": 0.0,
            "clean_sheets": 0,
            "failed_to_score": 0,
            "form": "",
            "quality_adjusted_points": 0.0,
            "matches": [],
        }
    wins = draws = losses = points = gf_total = ga_total = clean_sheets = failed_to_score = quality_adj_points = 0.0
    form = []
    compact_matches = []
    for row in ordered:
        result, pts, gf, ga = result_for_team(row, team)
        opponent = row["away_team"] if normalize_team_key(row["home_team"]) == normalize_team_key(team) else row["home_team"]
        opponent_mult = opponent_strength_multiplier(opponent, rankings)
        quality_adjusted_pts = pts * opponent_mult
        wins += 1 if result == "W" else 0
        draws += 1 if result == "D" else 0
        losses += 1 if result == "L" else 0
        points += pts
        quality_adj_points += quality_adjusted_pts
        gf_total += gf
        ga_total += ga
        clean_sheets += 1 if ga == 0 else 0
        failed_to_score += 1 if gf == 0 else 0
        form.append(result)
        compact_matches.append(
            {
                "date": row["match_date"],
                "competition": row["competition"],
                "home_team": row["home_team"],
                "away_team": row["away_team"],
                "home_goals": int(row["FTHG"]),
                "away_goals": int(row["FTAG"]),
                "team_result": result,
                "opponent": opponent,
                "opponent_rank": rankings.get(canonical_team_name(opponent), {}).get("rank", 999),
                "opponent_strength_multiplier": opponent_mult,
                "quality_adjusted_points": round(quality_adjusted_pts, 2),
            }
        )
    games = len(ordered)
    return {
        "games": games,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "points": points,
        "points_per_game": round(points / games, 4),
        "quality_adjusted_points": round(quality_adj_points, 2),
        "quality_adjusted_points_per_game": round(quality_adj_points / games, 4),
        "goals_for": gf_total,
        "goals_against": ga_total,
        "goal_diff": gf_total - ga_total,
        "goals_for_per_game": round(gf_total / games, 4),
        "goals_against_per_game": round(ga_total / games, 4),
        "clean_sheets": clean_sheets,
        "failed_to_score": failed_to_score,
        "form": "".join(form),
        "matches": compact_matches,
    }


def build_team_context(target_teams, by_team_matches, rankings, squad_values):
    context = {}
    for team in target_teams:
        team_name = canonical_team_name(team)
        ranking = rankings.get(team_name, {"rank": 999, "points": 0.0})
        squad = squad_values.get(team_name, {"squad_value_eur_m": 0.0, "updated_at": ""})
        last15 = summarize_last_matches(team_name, by_team_matches.get(team, []), rankings=rankings)
        context[team_name] = {
            "fifa_rank": int(ranking.get("rank", 999) or 999),
            "fifa_points": float(ranking.get("points", 0.0) or 0.0),
            "squad_value_eur_m": float(squad.get("squad_value_eur_m", 0.0) or 0.0),
            "squad_value_updated_at": str(squad.get("updated_at", "")),
            "last15": last15,
        }
    return context


def load_existing_recent_matches():
    if not os.path.exists(RAW_MATCHES_FILE):
        return [], {}
    frame = pd.read_csv(RAW_MATCHES_FILE)
    rows = frame.to_dict("records")
    by_team = defaultdict(list)
    for row in rows:
        for side in ["home_team", "away_team"]:
            by_team[canonical_team_name(row.get(side, ""))].append(row)
    return rows, by_team


def strength_from_context(team_context):
    """
    Calculate overall team strength from multiple factors.
    CRITICAL FIX: Heavily weight FIFA official ranking to prevent weak teams from getting inflated chances.
    """
    rank = float(team_context.get("fifa_rank", 999) or 999)
    fifa_points = float(team_context.get("fifa_points", 0.0) or 0.0)
    value = float(team_context.get("squad_value_eur_m", 0.0) or 0.0)
    form = team_context.get("last15", {})
    # Use quality-adjusted points per game for form score (vs stronger opponents counts more)
    ppg = float(form.get("quality_adjusted_points_per_game", form.get("points_per_game", 0.0) or 0.0) or 0.0)
    gdpg = safe_div(form.get("goal_diff", 0.0), max(1, form.get("games", 0)))
    gfpg = float(form.get("goals_for_per_game", 0.0) or 0.0)
    gapg = float(form.get("goals_against_per_game", 0.0) or 0.0)
    
    ranking_score = max(0.0, min(1.0, (210.0 - rank) / 209.0))
    points_score = max(0.0, min(1.0, (fifa_points - 900.0) / 1000.0)) if fifa_points else ranking_score
    value_score = max(0.0, min(1.0, math.log1p(value) / math.log1p(1600.0))) if value > 0 else 0.35
    form_score = max(0.0, min(1.0, ppg / 3.5))  # Slightly higher denominator since quality-adjusted can exceed 3
    goal_score = max(0.0, min(1.0, 0.50 + gdpg / 5.0 + (gfpg - gapg) / 8.0))
    
    # CRITICAL: Much higher weight on FIFA ranking (50%) to prevent weak teams with lucky form
    # from beating strong teams. Official FIFA rank is more reliable than recent form.
    return (
        0.50 * ranking_score      # Increased from 0.34: FIFA rank is authoritative
        + 0.15 * points_score     # Unchanged: FIFA points confirm rank
        + 0.15 * value_score      # Decreased from 0.20: Squad value is secondary
        + 0.15 * form_score       # Decreased from 0.22: Recent form is volatile
        + 0.05 * goal_score       # Decreased from 0.08: Goals are less predictive
    )


def safe_div(numerator, denominator, default=0.0):
    try:
        denominator = float(denominator)
        if denominator == 0:
            return default
        return float(numerator) / denominator
    except Exception:
        return default


def build_feature_row(home_team, away_team, competition, stage, is_neutral_site, team_context):
    home = team_context.get(canonical_team_name(home_team), {})
    away = team_context.get(canonical_team_name(away_team), {})
    h_form = home.get("last15", {})
    a_form = away.get("last15", {})
    h_strength = strength_from_context(home)
    a_strength = strength_from_context(away)
    return {
        "home_strength": h_strength,
        "away_strength": a_strength,
        "strength_diff": h_strength - a_strength,
        "home_fifa_rank": float(home.get("fifa_rank", 999) or 999),
        "away_fifa_rank": float(away.get("fifa_rank", 999) or 999),
        "fifa_rank_diff": float(away.get("fifa_rank", 999) or 999) - float(home.get("fifa_rank", 999) or 999),
        "home_fifa_points": float(home.get("fifa_points", 0.0) or 0.0),
        "away_fifa_points": float(away.get("fifa_points", 0.0) or 0.0),
        "fifa_points_diff": float(home.get("fifa_points", 0.0) or 0.0) - float(away.get("fifa_points", 0.0) or 0.0),
        "home_squad_value_eur_m": float(home.get("squad_value_eur_m", 0.0) or 0.0),
        "away_squad_value_eur_m": float(away.get("squad_value_eur_m", 0.0) or 0.0),
        "squad_value_ratio": safe_div(float(home.get("squad_value_eur_m", 0.0) or 0.0) + 25.0, float(away.get("squad_value_eur_m", 0.0) or 0.0) + 25.0, 1.0),
        "home_last15_ppg": float(h_form.get("points_per_game", 0.0) or 0.0),
        "away_last15_ppg": float(a_form.get("points_per_game", 0.0) or 0.0),
        "last15_ppg_diff": float(h_form.get("points_per_game", 0.0) or 0.0) - float(a_form.get("points_per_game", 0.0) or 0.0),
        "home_last15_gfpg": float(h_form.get("goals_for_per_game", 0.0) or 0.0),
        "away_last15_gfpg": float(a_form.get("goals_for_per_game", 0.0) or 0.0),
        "home_last15_gapg": float(h_form.get("goals_against_per_game", 0.0) or 0.0),
        "away_last15_gapg": float(a_form.get("goals_against_per_game", 0.0) or 0.0),
        "last15_goal_diff_delta": safe_div(h_form.get("goal_diff", 0.0), max(1, h_form.get("games", 0))) - safe_div(a_form.get("goal_diff", 0.0), max(1, a_form.get("games", 0))),
        "is_world_cup": 1.0 if "world cup" in str(competition).lower() else 0.0,
        "is_knockout": 1.0 if stage_is_knockout(stage) else 0.0,
        "is_neutral_site": 1.0 if is_neutral_site else 0.0,
        "competition": competition,
        "stage": str(stage or "unknown").strip().lower() or "unknown",
    }


def stage_is_knockout(stage):
    text = str(stage or "").lower()
    if "group" in text or "qualifying" in text:
        return False
    return any(term in text for term in ["final", "semi", "quarter", "round", "knockout", "third"])


def build_prediction_feature_frame(home_team, away_team, competition, stage, is_neutral_site, snapshot):
    known_teams = snapshot.get("known_teams", [])
    home = resolve_team_name(home_team, known_teams)
    away = resolve_team_name(away_team, known_teams)
    row = build_feature_row(
        home,
        away,
        competition,
        stage,
        is_neutral_site,
        snapshot.get("team_context", {}),
    )
    frame = pd.DataFrame([row])
    frame = pd.get_dummies(frame, columns=CATEGORICAL_FEATURE_COLUMNS, dtype=float)
    frame = frame.fillna(0.0)
    return frame, home, away


def resolve_team_name(raw_name, known_teams):
    raw = canonical_team_name(str(raw_name or "").strip())
    if raw in known_teams:
        return raw
    key = normalize_team_key(raw)
    by_key = {normalize_team_key(team): team for team in known_teams}
    if key in by_key:
        return by_key[key]
    contains = [team for team in known_teams if key and key in normalize_team_key(team)]
    if len(contains) == 1:
        return contains[0]
    reverse_contains = [team for team in known_teams if normalize_team_key(team) and normalize_team_key(team) in key]
    if len(reverse_contains) == 1:
        return reverse_contains[0]
    return raw


def align_feature_frame(raw_feature_frame, bundle):
    return raw_feature_frame.reindex(columns=bundle.get("train_columns", list(raw_feature_frame.columns)), fill_value=0.0)


def probability_jitter(probabilities, key, max_delta):
    h = max(0.0, float(probabilities.get("H", 0.0)))
    d = max(0.0, float(probabilities.get("D", 0.0)))
    a = max(0.0, float(probabilities.get("A", 0.0)))
    total = h + d + a
    if total <= 0:
        return {"H": 1 / 3, "D": 1 / 3, "A": 1 / 3}
    h, d, a = h / total, d / total, a / total
    seed = int(hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    delta = rng.uniform(-max_delta, max_delta)
    h = max(0.0, min(1.0, h + delta))
    a = max(0.0, min(1.0, a - delta))
    total = h + d + a
    return {"H": h / total, "D": d / total, "A": a / total}


def context_probabilities(home_context, away_context, is_neutral_site=True):
    h_strength = strength_from_context(home_context)
    a_strength = strength_from_context(away_context)
    diff = h_strength - a_strength
    rank_gap = float(away_context.get("fifa_rank", 999) or 999) - float(home_context.get("fifa_rank", 999) or 999)
    value_ratio = safe_div(
        float(home_context.get("squad_value_eur_m", 0.0) or 0.0) + 25.0,
        float(away_context.get("squad_value_eur_m", 0.0) or 0.0) + 25.0,
        1.0,
    )
    logistic_input = (4.4 * diff) + (0.006 * rank_gap) + (0.16 * math.log(max(0.05, value_ratio)))
    if not is_neutral_site:
        logistic_input += 0.12
    home_away_split = 1.0 / (1.0 + math.exp(-logistic_input))
    strength_gap = abs(diff)
    draw = max(0.16, min(0.31, 0.29 - 0.20 * strength_gap))
    non_draw = 1.0 - draw
    home = non_draw * home_away_split
    away = non_draw * (1.0 - home_away_split)
    return {"H": home, "D": draw, "A": away}


def expected_goals(home_context, away_context):
    h_form = home_context.get("last15", {})
    a_form = away_context.get("last15", {})
    h_gf = float(h_form.get("goals_for_per_game", 1.15) or 1.15)
    h_ga = float(h_form.get("goals_against_per_game", 1.15) or 1.15)
    a_gf = float(a_form.get("goals_for_per_game", 1.15) or 1.15)
    a_ga = float(a_form.get("goals_against_per_game", 1.15) or 1.15)
    h_strength = strength_from_context(home_context)
    a_strength = strength_from_context(away_context)
    home_goals = max(0.25, min(3.75, 0.56 * h_gf + 0.44 * a_ga + 0.55 * (h_strength - a_strength)))
    away_goals = max(0.25, min(3.75, 0.56 * a_gf + 0.44 * h_ga + 0.55 * (a_strength - h_strength)))
    return home_goals, away_goals


class CurrentContextClassifier:
    def __init__(self, team_context=None):
        self.team_context = team_context or {}
        self.classes_ = [0, 1, 2]

    def predict_proba(self, rows):
        matrices = []
        for _, row in rows.iterrows():
            probs = self._probabilities_from_feature_row(row)
            matrices.append([probs["A"], probs["D"], probs["H"]])
        return matrices

    def predict(self, rows):
        return [max(range(3), key=lambda idx: probs[idx]) for probs in self.predict_proba(rows)]

    def _probabilities_from_feature_row(self, row):
        diff = float(row.get("strength_diff", 0.0) or 0.0)
        rank_gap = float(row.get("fifa_rank_diff", 0.0) or 0.0)
        value_ratio = float(row.get("squad_value_ratio", 1.0) or 1.0)
        logistic_input = (4.4 * diff) + (0.006 * rank_gap) + (0.16 * math.log(max(0.05, value_ratio)))
        home_away_split = 1.0 / (1.0 + math.exp(-logistic_input))
        draw = max(0.16, min(0.31, 0.29 - 0.20 * abs(diff)))
        non_draw = 1.0 - draw
        return {"H": non_draw * home_away_split, "D": draw, "A": non_draw * (1.0 - home_away_split)}


class ContextGoalRegressor:
    def __init__(self, side):
        self.side = side

    def predict(self, rows):
        out = []
        for _, row in rows.iterrows():
            if self.side == "home":
                gf = float(row.get("home_last15_gfpg", 1.15) or 1.15)
                opp_ga = float(row.get("away_last15_gapg", 1.15) or 1.15)
                diff = float(row.get("strength_diff", 0.0) or 0.0)
                out.append(max(0.25, min(3.75, 0.56 * gf + 0.44 * opp_ga + 0.55 * diff)))
            else:
                gf = float(row.get("away_last15_gfpg", 1.15) or 1.15)
                opp_ga = float(row.get("home_last15_gapg", 1.15) or 1.15)
                diff = -float(row.get("strength_diff", 0.0) or 0.0)
                out.append(max(0.25, min(3.75, 0.56 * gf + 0.44 * opp_ga + 0.55 * diff)))
        return out


class ResultLabelEncoder:
    def inverse_transform(self, values):
        mapping = {0: "A", 1: "D", 2: "H", "0": "A", "1": "D", "2": "H"}
        return [mapping.get(value, value) for value in values]


CurrentContextClassifier.__module__ = "Process_National_Team_Data"
ContextGoalRegressor.__module__ = "Process_National_Team_Data"
ResultLabelEncoder.__module__ = "Process_National_Team_Data"


def build_training_dataset(team_context, recent_rows):
    """Build a sklearn-ready feature frame + target vectors from completed recent matches.

    The current team context is reused for every historical match (a static-context
    approximation).  This matches the available data scale (the last-15 matches per
    team across competitions) and gives the model a real, learnable signal even
    without season-by-season history.
    """
    feature_rows = []
    y_result = []
    y_home_goals = []
    y_away_goals = []

    for match in recent_rows or []:
        result = str(match.get("FTR", "")).strip().upper()
        if result not in RESULT_LABELS:
            continue
        home_team = match.get("home_team")
        away_team = match.get("away_team")
        if not home_team or not away_team or home_team == away_team:
            continue
        try:
            fthg = float(match.get("FTHG"))
            ftag = float(match.get("FTAG"))
        except (TypeError, ValueError):
            continue

        feature_rows.append(
            build_feature_row(
                home_team,
                away_team,
                match.get("competition", ""),
                match.get("stage", "unknown"),
                bool(match.get("is_neutral_site", True)),
                team_context,
            )
        )
        y_result.append(result)
        y_home_goals.append(fthg)
        y_away_goals.append(ftag)

    if not feature_rows:
        return None

    frame = pd.DataFrame(feature_rows)
    frame = pd.get_dummies(frame, columns=CATEGORICAL_FEATURE_COLUMNS, dtype=float)
    frame = frame.fillna(0.0)
    return {
        "X": frame,
        "y_result": y_result,
        "y_home_goals": y_home_goals,
        "y_away_goals": y_away_goals,
    }


def train_national_result_model(X, y):
    label_encoder = LabelEncoder()
    y_encoded = label_encoder.fit_transform(y)

    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=12,
        min_samples_leaf=2,
        random_state=42,
        n_jobs=NATIONAL_MODEL_THREADS,
    )
    model.fit(X, y_encoded)
    return model, label_encoder, "random-forest-cpu"


def train_national_goal_regressor(X, y, random_state):
    model = RandomForestRegressor(
        n_estimators=200,
        max_depth=12,
        min_samples_leaf=2,
        random_state=random_state,
        n_jobs=NATIONAL_MODEL_THREADS,
    )
    model.fit(X, y)
    return model, "random-forest-cpu"


def _sample_train_columns(team_context, target_teams):
    """Build the one-hot encoded training column list used when no real training is run."""
    home = target_teams[0] if target_teams else "Home"
    away = target_teams[1] if len(target_teams) >= 2 else (target_teams[0] if target_teams else "Away")
    sample_row = build_feature_row(home, away, "International/World Cup", "group-stage", True, team_context)
    sample_frame = pd.DataFrame([sample_row])
    sample_frame = pd.get_dummies(sample_frame, columns=CATEGORICAL_FEATURE_COLUMNS, dtype=float)
    return list(sample_frame.columns)


def build_context_bundle(team_context, recent_rows, target_teams):
    snapshot = {
        "team_context": team_context,
        "known_teams": sorted(team_context.keys()),
        "recent_matches": recent_rows,
        "latest_match_datetime_utc": max(
            (row.get("match_datetime_utc", "") for row in (recent_rows or [])),
            default="",
        ),
    }
    fingerprint = hashlib.sha256(
        json.dumps(team_context, sort_keys=True).encode("utf-8")
    ).hexdigest()

    training = build_training_dataset(team_context, recent_rows)
    use_sklearn = training is not None and len(training["X"]) >= MIN_TRAINING_ROWS
    training_rows = 0
    backend = "current_context_heuristic"
    train_columns = []

    if use_sklearn:
        X = training["X"]
        train_columns = list(X.columns)
        clf, label_encoder, backend = train_national_result_model(X, training["y_result"])
        home_goal_reg, _ = train_national_goal_regressor(X, training["y_home_goals"], 42)
        away_goal_reg, _ = train_national_goal_regressor(X, training["y_away_goals"], 43)
        training_rows = len(X)
        model_type = "sklearn"
    else:
        clf = CurrentContextClassifier(team_context)
        label_encoder = ResultLabelEncoder()
        home_goal_reg = ContextGoalRegressor("home")
        away_goal_reg = ContextGoalRegressor("away")
        train_columns = _sample_train_columns(team_context, target_teams)
        model_type = "current_context"

    return {
        "model_version": 3,
        "model_type": model_type,
        "backend": backend,
        "created_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "build_time": time.time(),
        "fingerprint": fingerprint,
        "clf": clf,
        "result_label_encoder": label_encoder,
        "home_goal_reg": home_goal_reg,
        "away_goal_reg": away_goal_reg,
        "train_columns": train_columns,
        "categorical_feature_columns": list(CATEGORICAL_FEATURE_COLUMNS),
        "snapshot": snapshot,
        "training_rows": training_rows,
        "min_training_rows": MIN_TRAINING_ROWS,
        "data_basis": "current FIFA rankings, latest squad market values, ESPN last-15 matches across competitions and friendlies",
    }


def load_model_bundle(path=MODEL_CACHE):
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"National context cache not found at {path}. Run Process_National_Team_Data.py first."
        )
    return joblib.load(path)


def write_api_report(target_teams, ranking_source, value_source, recent_match_count, model_type, backend, training_rows, min_training_rows):
    report = {
        "created_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "summary": [
            f"National predictor trains a {backend} sklearn model on {training_rows} completed last-15 matches per team across competitions (World Cup, qualifiers, continental tournaments, Nations League, friendlies).",
            "When fewer than the minimum training rows are available, the model falls back to a current-context heuristic using FIFA rank, FIFA points, and squad market value.",
            "Current FIFA rankings are loaded from a provided file or optional paid API credentials; seed rankings are used when no refresh source is configured.",
            "Latest squad market values are scraped from Transfermarkt (schnellsuche + startseite) and stored in national_team_squad_values.json; cached HTML responses are reused for 24 hours between runs, and the aggregated squad file is auto-refreshed when older than the squad-cache-days window (default 30 days).",
        ],
        "target_team_count": len(target_teams),
        "recent_match_count": recent_match_count,
        "model_type": model_type,
        "model_backend": backend,
        "training_rows": training_rows,
        "min_training_rows": min_training_rows,
        "ranking_source": ranking_source,
        "squad_value_source": value_source,
        "outputs": {
            "raw_recent_matches": RAW_MATCHES_FILE,
            "processed_context": PROCESSED_MATCHES_FILE,
            "model_cache": MODEL_CACHE,
        },
    }
    os.makedirs(NATIONAL_DATA_DIR, exist_ok=True)
    with open(API_REPORT_FILE, "w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)


def run_pipeline(args):
    import time
    os.makedirs(NATIONAL_DATA_DIR, exist_ok=True)
    pipeline_t0 = time.monotonic()
    step_t0 = time.monotonic()
    target_teams = fetch_world_cup_team_names() if args.world_cup_only else fetch_world_cup_team_names()
    if not target_teams:
        try:
            with open(FIFA_RANKINGS_FILE, "r", encoding="utf-8") as f:
                target_teams = sorted(json.load(f).keys())
            print(f"[INFO] Using {len(target_teams)} teams from {FIFA_RANKINGS_FILE} as fallback.")
        except Exception:
            print("[ERROR] ESPN API unreachable and no rankings file available. Cannot determine World Cup teams.")
            raise
    print(f"[TIMING] fetch_world_cup_team_names: {time.monotonic() - step_t0:.1f}s")

    step_t0 = time.monotonic()
    rankings = load_fifa_rankings(
        args.rankings_file,
        footballdata_io_token=getattr(args, "footballdata_io_token", ""),
        sportradar_api_key=getattr(args, "sportradar_api_key", ""),
    )
    print(f"[TIMING] load_fifa_rankings: {time.monotonic() - step_t0:.1f}s")

    step_t0 = time.monotonic()
    skip_squad = args.skip_squad_values
    if not skip_squad:
        refresh_squad = bool(getattr(args, "refresh_squad_values", False)) or not os.path.exists(args.squad_values_file)
        squad_cache_days = getattr(args, "squad_cache_days", SQUAD_VALUES_MAX_AGE_DAYS)
        if squad_cache_days is None:
            squad_cache_days = SQUAD_VALUES_MAX_AGE_DAYS
    else:
        refresh_squad = False
        squad_cache_days = 0
    squad_values = load_squad_values(
        args.squad_values_file,
        refresh=refresh_squad,
        target_teams=target_teams,
        max_workers=TRANSFERMARKT_FETCH_WORKERS,
        max_age_days=squad_cache_days,
    )
    print(f"[TIMING] load_squad_values: {time.monotonic() - step_t0:.1f}s")

    step_t0 = time.monotonic()
    if args.skip_fetch:
        recent_rows, by_team = load_existing_recent_matches()
    else:
        recent_rows, by_team = fetch_recent_espn_matches(target_teams, args.lookback_days)
        pd.DataFrame(recent_rows).to_csv(RAW_MATCHES_FILE, index=False)
    print(f"[TIMING] fetch_recent_espn_matches: {time.monotonic() - step_t0:.1f}s ({len(recent_rows)} matches)")

    # Retry failed fetches: after both sources have run, check for missing data.
    if not args.skip_fetch:
        if not skip_squad:
            retry_squad_targets = [t for t in target_teams if squad_values.get(canonical_team_name(t), 0) == 0]
            if retry_squad_targets:
                print(f"[RETRY] {len(retry_squad_targets)} teams missing squad values; retrying Transfermarkt...")
                time.sleep(3)
                try:
                    build_squad_values_file(list(retry_squad_targets), refresh=True, max_workers=max(1, TRANSFERMARKT_FETCH_WORKERS // 2))
                    retry_payload = load_json_or_csv_records(args.squad_values_file)
                    if retry_payload:
                        squad_values.update(normalize_squad_values_payload(retry_payload))
                    print(f"  Transfermarkt retry complete for {len(retry_squad_targets)} teams.")
                except Exception as exc:
                    print(f"  Transfermarkt retry failed: {exc}")
        teams_with_few_matches = [t for t in target_teams if len(by_team.get(t, [])) < min(LAST_N_MATCHES, 5)]
        if teams_with_few_matches:
            print(f"[RETRY] {len(teams_with_few_matches)} teams have <{min(LAST_N_MATCHES,5)} recent matches; retrying ESPN fetch with delay...")
            time.sleep(3)
            extra_rows, extra_by = fetch_recent_espn_matches(teams_with_few_matches, args.lookback_days)
            if extra_rows:
                existing_ids = {r.get("match_id", "") for r in recent_rows if r.get("match_id")}
                new_rows = [r for r in extra_rows if r.get("match_id", "") not in existing_ids]
                recent_rows.extend(new_rows)
                for team in teams_with_few_matches:
                    team_key = normalize_team_key(team)
                    for row in extra_rows:
                        if normalize_team_key(row.get("home_team", "")) == team_key or normalize_team_key(row.get("away_team", "")) == team_key:
                            if row.get("match_id", "") not in existing_ids:
                                by_team[team].append(row)
                    by_team[team] = sorted(by_team[team], key=lambda r: r["match_datetime_utc"], reverse=True)[:LAST_N_MATCHES]
                print(f"  ESPN retry added {len(new_rows)} new matches.")
            pd.DataFrame(recent_rows).to_csv(RAW_MATCHES_FILE, index=False)

    step_t0 = time.monotonic()
    team_context = build_team_context(target_teams, by_team, rankings, squad_values)
    processed_rows = []
    for team, context in team_context.items():
        row = {
            "team": team,
            "fifa_rank": context["fifa_rank"],
            "fifa_points": context["fifa_points"],
            "squad_value_eur_m": context["squad_value_eur_m"],
            **{f"last15_{key}": value for key, value in context["last15"].items() if key != "matches"},
        }
        processed_rows.append(row)
    pd.DataFrame(processed_rows).to_csv(PROCESSED_MATCHES_FILE, index=False)
    print(f"[TIMING] build_team_context: {time.monotonic() - step_t0:.1f}s")

    step_t0 = time.monotonic()
    bundle = build_context_bundle(team_context, recent_rows, target_teams)
    print(f"[TIMING] build_context_bundle (incl model train): {time.monotonic() - step_t0:.1f}s")

    step_t0 = time.monotonic()
    joblib.dump(bundle, MODEL_CACHE)
    print(f"[TIMING] joblib.dump: {time.monotonic() - step_t0:.1f}s")
    print(f"[TIMING] run_pipeline total: {time.monotonic() - pipeline_t0:.1f}s")
    ranking_source = "api_or_file" if os.path.exists(args.rankings_file) or getattr(args, "footballdata_io_token", "") or getattr(args, "sportradar_api_key", "") else "seed_snapshot"
    if refresh_squad and os.path.exists(args.squad_values_file):
        value_source = "transfermarkt_live"
    elif os.path.exists(args.squad_values_file):
        value_source = "file"
    else:
        value_source = "seed_snapshot"
    if (
        value_source == "transfermarkt_live"
        and not getattr(args, "refresh_squad_values", False)
        and squad_cache_days
        and squad_cache_days > 0
    ):
        value_source = f"transfermarkt_live (stale>{squad_cache_days}d)"
    write_api_report(
        target_teams,
        ranking_source,
        value_source,
        len(recent_rows),
        bundle.get("model_type", "unknown"),
        bundle.get("backend", "unknown"),
        bundle.get("training_rows", 0),
        bundle.get("min_training_rows", MIN_TRAINING_ROWS),
    )

    print("\nNational-team current-context predictor generated")
    print(f"Teams loaded: {len(target_teams)}")
    print(f"Recent ESPN matches loaded: {len(recent_rows)}")
    print(f"Model type: {bundle.get('model_type', 'unknown')}")
    print(f"Model backend: {bundle.get('backend', 'unknown')}")
    print(f"Training rows: {bundle.get('training_rows', 0)} (min: {bundle.get('min_training_rows', MIN_TRAINING_ROWS)})")
    print(f"Train feature columns: {len(bundle.get('train_columns', []))}")
    print(f"Ranking source: {ranking_source}")
    print(f"Squad value source: {value_source}")
    print(f"Context data: {PROCESSED_MATCHES_FILE}")
    print(f"Model cache: {MODEL_CACHE}")
    return bundle


def main():
    args = parse_cli_args()
    run_pipeline(args)


if __name__ == "__main__":
    main()
