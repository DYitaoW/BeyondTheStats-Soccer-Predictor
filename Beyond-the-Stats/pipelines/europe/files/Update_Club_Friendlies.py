"""Sync club and international friendlies from ESPN into the friendlies CSV.

Friendlies appear on the upcoming schedule as schedule-only entries
(no model predictions). Settled results are still tracked for display.
"""

import os as _os_paths_setup
import sys as _sys_paths_setup
_FILES_DIR = _os_paths_setup.path.dirname(_os_paths_setup.path.abspath(__file__))
_REGION_DIR = _os_paths_setup.path.dirname(_FILES_DIR)  # pipelines/europe
_SP_DIR = _os_paths_setup.path.dirname(_os_paths_setup.path.dirname(_REGION_DIR))  # Beyond-the-Stats
if _SP_DIR not in _sys_paths_setup.path:
    _sys_paths_setup.path.insert(0, _SP_DIR)
from shared import paths as _bts_paths
if str(_bts_paths.SHARED_DIR) not in _sys_paths_setup.path:
    _sys_paths_setup.path.insert(0, str(_bts_paths.SHARED_DIR))
BASE_DIR = str(_bts_paths.SP_DIR)  # europe uses project-level Data/
PREDICTIONS_DIR = str(_bts_paths.OUTPUT_PRED_FRIENDLIES)
PROJECT_DIR = str(_bts_paths.SP_DIR)
import os
import sys
import time
import unicodedata
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import Predict_Match as pm

# BASE_DIR set by shared.paths bootstrap above
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
import team_mapping_groups as tmg  # noqa: E402
import espn_api_cache  # noqa: E402
PREDICTIONS_FILE = os.path.join(PREDICTIONS_DIR, "upcoming_club_friendlies.csv")
TEAM_MAPPING_FILE = str(_bts_paths.TEAM_NAME_MAPPING_MASTER)

CLUB_FRIENDLIES_COMPETITION = "Club Friendlies"
INTERNATIONAL_FRIENDLIES_COMPETITION = "International/Friendly"
FRIENDLY_ESPN_SOURCES = (
    (CLUB_FRIENDLIES_COMPETITION, "club.friendly"),
    (INTERNATIONAL_FRIENDLIES_COMPETITION, "fifa.friendly"),
)
EASTERN_TZ = ZoneInfo("America/New_York")
LOOKAHEAD_DAYS = 365

RESULT_COLUMNS = [
    "prediction_key",
    "created_at_utc",
    "match_date",
    "match_datetime_utc",
    "match_datetime_et",
    "competition",
    "home_team",
    "away_team",
    "display_home_team",
    "display_away_team",
    "schedule_only",
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
    "actual_home_goals",
    "actual_away_goals",
    "actual_result",
    "is_correct",
    "settled_at_utc",
    "espn_event_id",
    "live_tracking",
]


def normalize_team_key(name):
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip().replace("&", " and ")
    text = text.replace("'", "").replace(".", " ")
    text = text.replace("-", " ")
    parts = [p for p in text.split() if p]
    stop_words = {"fc", "cf", "ac", "ca", "sc", "sv", "fk", "club", "de", "the"}
    parts = [p for p in parts if p not in stop_words]
    return "".join(parts)


def load_team_mapping():
    if not os.path.exists(TEAM_MAPPING_FILE):
        return {}
    try:
        return tmg.load_team_mapping(TEAM_MAPPING_FILE)
    except Exception:
        return {}


def resolve_team_name(raw_name, mapping, available_teams):
    raw_name = str(raw_name or "").strip()
    if not raw_name:
        return raw_name
    for competition in mapping.values():
        if not isinstance(competition, dict):
            continue
        mapped = str(competition.get(raw_name, "")).strip()
        if mapped:
            return mapped
    resolved = pm.resolve_team_name(raw_name, available_teams)
    return resolved or raw_name


def parse_event(event):
    event_id = str(event.get("id", "")).strip()
    event_date = pd.to_datetime(event.get("date"), utc=True, errors="coerce")
    if pd.isna(event_date):
        return None

    event_dt_et = event_date.tz_convert(EASTERN_TZ)
    match_date = event_dt_et.tz_localize(None).normalize()
    competitions = event.get("competitions", [])
    if not competitions:
        return None
    comp0 = competitions[0] or {}

    home_team = ""
    away_team = ""
    home_score = None
    away_score = None
    for competitor in comp0.get("competitors", []) or []:
        team_name = ((competitor.get("team") or {}).get("displayName") or "").strip()
        side = str(competitor.get("homeAway", "")).strip().lower()
        score_val = pd.to_numeric(competitor.get("score"), errors="coerce")
        if side == "home":
            home_team = team_name
            home_score = int(score_val) if pd.notna(score_val) else None
        elif side == "away":
            away_team = team_name
            away_score = int(score_val) if pd.notna(score_val) else None
    if not home_team or not away_team:
        return None

    status_type = ((comp0.get("status") or {}).get("type") or {})
    status_state = str(status_type.get("state", "")).strip().lower()
    completed = bool(status_type.get("completed")) or status_state in {"post", "final"}

    actual_result = ""
    if completed and home_score is not None and away_score is not None:
        if home_score > away_score:
            actual_result = "H"
        elif away_score > home_score:
            actual_result = "A"
        else:
            actual_result = "D"

    return {
        "espn_event_id": event_id,
        "match_date": match_date,
        "match_datetime_utc": event_date.tz_convert("UTC").isoformat(),
        "match_datetime_et": event_dt_et.isoformat(),
        "home_team": home_team,
        "away_team": away_team,
        "actual_home_goals": home_score if completed else "",
        "actual_away_goals": away_score if completed else "",
        "actual_result": actual_result,
        "status_state": status_state,
        "completed": completed,
    }


def load_fixtures_from_espn(lookahead_days=LOOKAHEAD_DAYS):
    """Load club + international friendly fixtures from ESPN scoreboards."""
    today = pd.Timestamp(datetime.now(UTC).date())
    rows = []
    seen = set()
    for competition, espn_id in FRIENDLY_ESPN_SOURCES:
        for offset in range(0, max(1, int(lookahead_days) + 1)):
            day = today + pd.Timedelta(days=offset)
            try:
                data = espn_api_cache.fetch_scoreboard(espn_id, day.strftime("%Y%m%d"))
            except Exception:
                continue
            for event in data.get("events", []) or []:
                parsed = parse_event(event)
                if parsed is None:
                    continue
                if parsed["match_date"] < today:
                    continue
                key = (
                    competition,
                    parsed["match_date"].strftime("%Y-%m-%d"),
                    parsed["home_team"],
                    parsed["away_team"],
                )
                if key in seen:
                    continue
                seen.add(key)
                parsed = dict(parsed)
                parsed["competition"] = competition
                rows.append(parsed)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(
        ["match_date", "competition", "home_team", "away_team"]
    ).reset_index(drop=True)


def make_prediction_key(match_date, competition, home_team, away_team):
    home_key = normalize_team_key(home_team) or str(home_team).strip().lower()
    away_key = normalize_team_key(away_team) or str(away_team).strip().lower()
    team_pair = sorted([home_key, away_key])
    competition = str(competition or CLUB_FRIENDLIES_COMPETITION).strip()
    return f"{match_date.strftime('%Y-%m-%d')}|{competition}|{team_pair[0]}|{team_pair[1]}"


def load_existing():
    if not os.path.exists(PREDICTIONS_FILE):
        return pd.DataFrame(columns=RESULT_COLUMNS)
    frame = pd.read_csv(PREDICTIONS_FILE, dtype="object")
    for col in RESULT_COLUMNS:
        if col not in frame.columns:
            frame[col] = ""
    return frame[RESULT_COLUMNS].astype("object")


def sync_friendlies():
    _t0 = time.monotonic()
    fixtures = load_fixtures_from_espn()
    if fixtures.empty:
        print("No club/international friendlies returned by ESPN.")
        existing = load_existing()
        if not existing.empty:
            os.makedirs(PREDICTIONS_DIR, exist_ok=True)
            existing.to_csv(PREDICTIONS_FILE, index=False)
        return

    mapping = load_team_mapping()
    available_teams = []
    created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing = load_existing()
    existing_by_key = {}
    if not existing.empty:
        existing_by_key = {
            str(row.get("prediction_key", "")).strip(): row.to_dict()
            for _, row in existing.iterrows()
            if str(row.get("prediction_key", "")).strip()
        }

    rows = []
    for _, fixture in fixtures.iterrows():
        raw_home = str(fixture["home_team"]).strip()
        raw_away = str(fixture["away_team"]).strip()
        home_team = resolve_team_name(raw_home, mapping, available_teams)
        away_team = resolve_team_name(raw_away, mapping, available_teams)
        match_date = pd.Timestamp(fixture["match_date"]).normalize()
        competition = str(fixture.get("competition") or CLUB_FRIENDLIES_COMPETITION).strip()
        prediction_key = make_prediction_key(match_date, competition, home_team, away_team)
        prior = existing_by_key.get(prediction_key, {})

        row = {col: "" for col in RESULT_COLUMNS}
        row.update(
            {
                "prediction_key": prediction_key,
                "created_at_utc": prior.get("created_at_utc") or created_at,
                "match_date": match_date.strftime("%Y-%m-%d"),
                "match_datetime_utc": str(fixture.get("match_datetime_utc", "")),
                "match_datetime_et": str(fixture.get("match_datetime_et", "")),
                "competition": competition,
                "home_team": home_team,
                "away_team": away_team,
                "display_home_team": raw_home,
                "display_away_team": raw_away,
                "schedule_only": "1",
                "live_tracking": "0",
                "espn_event_id": str(fixture.get("espn_event_id", "")),
                "actual_home_goals": fixture.get("actual_home_goals", ""),
                "actual_away_goals": fixture.get("actual_away_goals", ""),
                "actual_result": fixture.get("actual_result", ""),
            }
        )

        if str(row.get("actual_result", "")).strip().upper() in {"H", "D", "A"}:
            predicted = str(row.get("predicted_result", "")).strip().upper()
            actual = str(row.get("actual_result", "")).strip().upper()
            row["is_correct"] = "1" if predicted and predicted == actual else ("0" if predicted else "")
            row["settled_at_utc"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            row["is_correct"] = prior.get("is_correct", "")
            row["settled_at_utc"] = prior.get("settled_at_utc", "")

        rows.append(row)

    out = pd.DataFrame(rows, columns=RESULT_COLUMNS).astype("object")
    os.makedirs(PREDICTIONS_DIR, exist_ok=True)
    out.to_csv(PREDICTIONS_FILE, index=False)
    club_n = int((out["competition"] == CLUB_FRIENDLIES_COMPETITION).sum())
    intl_n = int((out["competition"] == INTERNATIONAL_FRIENDLIES_COMPETITION).sum())
    print(f"Saved friendlies: {PREDICTIONS_FILE}")
    print(
        f"Fixtures: {len(out)} | club={club_n} international={intl_n} | schedule-only (no predictions)"
    )
    print(f"Elapsed: {time.monotonic() - _t0:.1f}s")


def update_recent_friendlies_results(days_back=1, days_forward=1):
    """Lightweight ESPN refresh for recent friendlies used by the website poller."""
    today = pd.Timestamp(datetime.now(UTC).date())
    start = today - pd.Timedelta(days=max(0, int(days_back)))
    end = today + pd.Timedelta(days=max(0, int(days_forward)))
    rows = []
    day = start
    while day <= end:
        try:
            # Poller-grade freshness: the website result-sync calls this every
            # few minutes, so never serve a cached scoreboard older than 60s.
            data = espn_api_cache.fetch_scoreboard(
                "club.friendly", day.strftime("%Y%m%d"), max_age_seconds=60
            )
        except Exception:
            day += pd.Timedelta(days=1)
            continue
        for event in data.get("events", []) or []:
            parsed = parse_event(event)
            if parsed is not None:
                rows.append(parsed)
        day += pd.Timedelta(days=1)

    if not rows:
        return 0

    existing = load_existing()
    if existing.empty:
        sync_friendlies()
        return 0

    updates = 0
    frame = existing.copy().astype("object")
    for parsed in rows:
        match_date = pd.Timestamp(parsed["match_date"]).strftime("%Y-%m-%d")
        for idx, row in frame.iterrows():
            if str(row.get("match_date", "")).strip() != match_date:
                continue
            home_match = normalize_team_key(row.get("display_home_team", row.get("home_team", ""))) == normalize_team_key(parsed["home_team"])
            away_match = normalize_team_key(row.get("display_away_team", row.get("away_team", ""))) == normalize_team_key(parsed["away_team"])
            if not (home_match and away_match):
                continue
            actual = str(parsed.get("actual_result", "")).strip().upper()
            if actual not in {"H", "D", "A"}:
                continue
            frame.at[idx, "actual_home_goals"] = parsed.get("actual_home_goals", "")
            frame.at[idx, "actual_away_goals"] = parsed.get("actual_away_goals", "")
            frame.at[idx, "actual_result"] = actual
            predicted = str(row.get("predicted_result", "")).strip().upper()
            frame.at[idx, "is_correct"] = "1" if predicted and predicted == actual else ("0" if predicted else "")
            frame.at[idx, "settled_at_utc"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            updates += 1

    if updates:
        os.makedirs(PREDICTIONS_DIR, exist_ok=True)
        frame.to_csv(PREDICTIONS_FILE, index=False)
    return updates


def main():
    sync_friendlies()


if __name__ == "__main__":
    main()
