"""Sync club preseason friendlies from ESPN into the upcoming friendlies CSV.

- All friendlies appear on the upcoming schedule.
- Non-Chelsea friendlies are schedule + final result only (no model predictions).
- Chelsea FC friendlies also receive global-model predictions.
"""
import json
import os
import sys
import unicodedata
import urllib.request
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import Predict_Match as pm

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREDICTIONS_DIR = os.path.join(BASE_DIR, "Data", "Predictions")
PREDICTIONS_FILE = os.path.join(PREDICTIONS_DIR, "upcoming_club_friendlies.csv")
TEAM_MAPPING_FILE = os.path.join(PREDICTIONS_DIR, "team_name_mapping_master.json")

CLUB_FRIENDLIES_COMPETITION = "Club Friendlies"
ESPN_SCOREBOARD_API = "https://site.api.espn.com/apis/site/v2/sports/soccer/club.friendly/scoreboard"
EASTERN_TZ = ZoneInfo("America/New_York")
LOOKAHEAD_DAYS = 365
CHELSEA_KEYS = {"chelsea", "chelseafc"}

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


def is_chelsea_team(name):
    return normalize_team_key(name) in CHELSEA_KEYS


def is_chelsea_fixture(home_team, away_team):
    return is_chelsea_team(home_team) or is_chelsea_team(away_team)


def load_team_mapping():
    if not os.path.exists(TEAM_MAPPING_FILE):
        return {}
    try:
        with open(TEAM_MAPPING_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


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


def fetch_json(url, timeout=30):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


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
    today = pd.Timestamp(datetime.now(UTC).date())
    rows = []
    seen = set()
    for offset in range(0, max(1, int(lookahead_days) + 1)):
        day = today + pd.Timedelta(days=offset)
        url = f"{ESPN_SCOREBOARD_API}?dates={day.strftime('%Y%m%d')}"
        try:
            data = fetch_json(url, timeout=30)
        except Exception:
            continue
        for event in data.get("events", []) or []:
            parsed = parse_event(event)
            if parsed is None:
                continue
            if parsed["match_date"] < today:
                continue
            key = (
                parsed["match_date"].strftime("%Y-%m-%d"),
                parsed["home_team"],
                parsed["away_team"],
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(parsed)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["match_date", "home_team", "away_team"]).reset_index(drop=True)


def make_prediction_key(match_date, home_team, away_team):
    home_key = normalize_team_key(home_team) or str(home_team).strip().lower()
    away_key = normalize_team_key(away_team) or str(away_team).strip().lower()
    team_pair = sorted([home_key, away_key])
    return f"{match_date.strftime('%Y-%m-%d')}|{CLUB_FRIENDLIES_COMPETITION}|{team_pair[0]}|{team_pair[1]}"


def build_prediction_context():
    matches, season_files = pm.load_training_matches(pm.PROCESSED_DIR)
    if not os.path.exists(pm.MODEL_CACHE):
        raise FileNotFoundError(f"Missing model cache: {pm.MODEL_CACHE}")

    bundle = __import__("joblib").load(pm.MODEL_CACHE)
    if bundle.get("fingerprint") != pm.data_fingerprint(season_files):
        bt = bundle.get("build_time")
        if bt is None or (__import__("time").time() - bt) >= 604800:
            raise RuntimeError("Model cache is stale. Rebuild by running Predict_Match.py.")

    overall_teams = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "overall_teams.json")) or {}
    season_teams = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "season_teams.json")) or {}
    head_to_head = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "head_to_head.json")) or {}
    current_form = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "current_form.json")) or {}
    league_strength = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "league_strength.json")) or {}
    market_value = pm.load_json_if_exists(
        os.path.join(pm.TEAM_DATA_DIR, "team_top_market_value_players.json")
    ) or {}

    latest_season = max(season_files.keys()) if season_files else ""
    latest_start_year = pm.parse_start_year_from_key(latest_season) if latest_season else datetime.now().year
    available_teams = sorted(overall_teams.keys()) if isinstance(overall_teams, dict) else []
    team_comp_map = {}
    for team_name, seasons in season_teams.items():
        if isinstance(seasons, dict) and seasons:
            team_comp_map[team_name] = os.path.dirname(next(iter(seasons.keys()))).replace("\\", "/")

    return {
        "clf": bundle["model"],
        "home_goal_reg": bundle["home_goal_reg"],
        "away_goal_reg": bundle["away_goal_reg"],
        "home_shot_reg": bundle.get("home_shot_reg"),
        "away_shot_reg": bundle.get("away_shot_reg"),
        "home_sot_reg": bundle.get("home_sot_reg"),
        "away_sot_reg": bundle.get("away_sot_reg"),
        "result_le": bundle["result_le"],
        "train_columns": bundle["train_columns"],
        "overall_teams": overall_teams,
        "season_teams": season_teams,
        "head_to_head": head_to_head,
        "current_form": current_form,
        "league_strength": league_strength,
        "market_value_data": market_value,
        "available_teams": available_teams,
        "team_comp_map": team_comp_map,
        "latest_season": latest_season,
        "latest_start_year": latest_start_year,
    }


def predict_chelsea_fixture(home_team, away_team, context):
    prediction_season = pm.choose_season_for_teams(
        home_team, away_team, context["season_teams"], context["latest_season"]
    )
    competition_key = "England/Premier League"
    start_year = pm.parse_start_year_from_key(prediction_season)
    season_coeff = pm.season_recency_coefficient(context["latest_start_year"], start_year)
    home_comp = context["team_comp_map"].get(home_team, competition_key)
    away_comp = context["team_comp_map"].get(away_team, competition_key)

    X = pm.build_features(
        pm.build_match_input(home_team, away_team),
        prediction_season,
        competition_key,
        season_coeff,
        context["overall_teams"],
        context["season_teams"],
        context["head_to_head"],
        context["current_form"],
        context["league_strength"],
        home_competition_override=home_comp,
        away_competition_override=away_comp,
    )
    X = pd.get_dummies(X, columns=["competition"], dtype=float)
    X = X.reindex(columns=context["train_columns"], fill_value=0.0)

    probs = {"H": 0.0, "D": 0.0, "A": 0.0}
    pvals = context["clf"].predict_proba(X)[0]
    for idx, enc in enumerate(context["clf"].classes_):
        label = context["result_le"].inverse_transform([enc])[0]
        probs[label] = float(pvals[idx])
    probs = pm.reduce_draw_probability(probs)
    predicted = max(probs, key=probs.get)
    phg = max(0.0, float(context["home_goal_reg"].predict(X)[0]))
    pag = max(0.0, float(context["away_goal_reg"].predict(X)[0]))
    if predicted == "H" and phg <= pag:
        phg = pag + 1
    elif predicted == "A" and pag <= phg:
        pag = phg + 1
    elif predicted == "D":
        pag = phg

    return {
        "predicted_result": predicted,
        "prob_home": round(probs["H"], 6),
        "prob_draw": round(probs["D"], 6),
        "prob_away": round(probs["A"], 6),
        "pred_home_goals": int(round(phg)),
        "pred_away_goals": int(round(pag)),
        "pred_home_shots": round(float(context["home_shot_reg"].predict(X)[0]), 3) if context.get("home_shot_reg") else "",
        "pred_away_shots": round(float(context["away_shot_reg"].predict(X)[0]), 3) if context.get("away_shot_reg") else "",
        "pred_home_sot": round(float(context["home_sot_reg"].predict(X)[0]), 3) if context.get("home_sot_reg") else "",
        "pred_away_sot": round(float(context["away_sot_reg"].predict(X)[0]), 3) if context.get("away_sot_reg") else "",
        "probability_reasoning": pm.build_reasoning_string(
            home_team,
            away_team,
            CLUB_FRIENDLIES_COMPETITION,
            probs,
            float(phg),
            float(pag),
            season_coeff=season_coeff,
        ),
    }


def load_existing():
    if not os.path.exists(PREDICTIONS_FILE):
        return pd.DataFrame(columns=RESULT_COLUMNS)
    frame = pd.read_csv(PREDICTIONS_FILE, dtype="object")
    for col in RESULT_COLUMNS:
        if col not in frame.columns:
            frame[col] = ""
    return frame[RESULT_COLUMNS].astype("object")


def sync_friendlies():
    fixtures = load_fixtures_from_espn()
    if fixtures.empty:
        print("No club friendlies returned by ESPN.")
        existing = load_existing()
        if not existing.empty:
            os.makedirs(PREDICTIONS_DIR, exist_ok=True)
            existing.to_csv(PREDICTIONS_FILE, index=False)
        return

    mapping = load_team_mapping()
    try:
        context = build_prediction_context()
    except Exception as exc:
        print(f"Could not build prediction context ({exc}); schedule-only sync will continue.")
        context = None

    available_teams = context["available_teams"] if context else []
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
    chelsea_predictions = 0
    for _, fixture in fixtures.iterrows():
        raw_home = str(fixture["home_team"]).strip()
        raw_away = str(fixture["away_team"]).strip()
        home_team = resolve_team_name(raw_home, mapping, available_teams)
        away_team = resolve_team_name(raw_away, mapping, available_teams)
        match_date = pd.Timestamp(fixture["match_date"]).normalize()
        prediction_key = make_prediction_key(match_date, home_team, away_team)
        prior = existing_by_key.get(prediction_key, {})

        chelsea_match = is_chelsea_fixture(home_team, away_team) or is_chelsea_fixture(raw_home, raw_away)
        row = {col: "" for col in RESULT_COLUMNS}
        row.update(
            {
                "prediction_key": prediction_key,
                "created_at_utc": prior.get("created_at_utc") or created_at,
                "match_date": match_date.strftime("%Y-%m-%d"),
                "match_datetime_utc": str(fixture.get("match_datetime_utc", "")),
                "match_datetime_et": str(fixture.get("match_datetime_et", "")),
                "competition": CLUB_FRIENDLIES_COMPETITION,
                "home_team": home_team,
                "away_team": away_team,
                "display_home_team": raw_home,
                "display_away_team": raw_away,
                "schedule_only": "0" if chelsea_match else "1",
                "live_tracking": "1" if chelsea_match else "0",
                "espn_event_id": str(fixture.get("espn_event_id", "")),
                "actual_home_goals": fixture.get("actual_home_goals", ""),
                "actual_away_goals": fixture.get("actual_away_goals", ""),
                "actual_result": fixture.get("actual_result", ""),
            }
        )

        if chelsea_match and context is not None:
            try:
                pred = predict_chelsea_fixture(home_team, away_team, context)
                row.update(pred)
                chelsea_predictions += 1
            except Exception as exc:
                print(f"Skipped Chelsea prediction for {raw_home} vs {raw_away}: {exc}")
                row["schedule_only"] = "1"
                row["live_tracking"] = "0"

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
    print(f"Saved club friendlies: {PREDICTIONS_FILE}")
    print(f"Fixtures: {len(out)} | Chelsea predictions: {chelsea_predictions}")


def update_recent_friendlies_results(days_back=1, days_forward=1):
    """Lightweight ESPN refresh for recent friendlies used by the website poller."""
    today = pd.Timestamp(datetime.now(UTC).date())
    start = today - pd.Timedelta(days=max(0, int(days_back)))
    end = today + pd.Timedelta(days=max(0, int(days_forward)))
    rows = []
    day = start
    while day <= end:
        url = f"{ESPN_SCOREBOARD_API}?dates={day.strftime('%Y%m%d')}"
        try:
            data = fetch_json(url, timeout=30)
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
