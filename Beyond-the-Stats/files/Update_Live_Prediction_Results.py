"""
Settle predictions by matching CSV predictions against real ESPN results.

Called as a post-pipeline step by ``Run_All_Pipeline._run_shared_post_steps``.
Reads the ``upcoming_matchweek_predictions.csv`` files (global + MLS), fetches
completed match results from ESPN scoreboards, then:
- Updates ``actual_home_goals`` / ``actual_away_goals`` / ``actual_result``
- Marks ``is_correct`` (1/0)
- Tracks accuracy totals in ``accuracy_totals.json``
- Drops settled rows from the upcoming CSVs (they are done)

Team-name mapping is stored in ``team_name_mapping_master.json`` and auto-learned
from ESPN display names on each run.
"""
import argparse
import difflib
import json
import os
import urllib.request
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GLOBAL_PREDICTIONS_FILE = os.path.join(BASE_DIR, "Data", "Predictions", "upcoming_matchweek_predictions.csv")
MLS_PREDICTIONS_FILE = os.path.join(BASE_DIR, "MLS", "Data", "Predictions", "upcoming_matchweek_predictions.csv")
SHARED_MAPPING_FILE = os.path.join(BASE_DIR, "Data", "Predictions", "team_name_mapping_master.json")
LEGACY_GLOBAL_MAPPING_FILE = os.path.join(BASE_DIR, "Data", "Predictions", "upcoming_fixture_team_mapping.json")
LEGACY_MLS_MAPPING_FILE = os.path.join(BASE_DIR, "MLS", "Data", "Predictions", "upcoming_fixture_team_mapping.json")
ESPN_NAMES_FILE = os.path.join(BASE_DIR, "Data", "Predictions", "espn_team_names_seen.json")
ACCURACY_TOTALS_FILE = os.path.join(BASE_DIR, "Website", "files", "accuracy_totals.json")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
ESPN_COMPETITION_KEYS = {
    "England/Premier League": "eng.1",
    "England/Championship": "eng.2",
    "Spain/La Liga": "esp.1",
    "Spain/La Liga 2": "esp.2",
    "Italy/Serie A": "ita.1",
    "Italy/Serie B": "ita.2",
    "Germany/Bundesliga": "ger.1",
    "Germany/Bundesliga 2": "ger.2",
    "France/Ligue 1": "fra.1",
    "France/Ligue 2": "fra.2",
    "Portugal/Liga Portugal": "por.1",
    "United States/MLS": "usa.1",
}
DEFAULT_ESPN_FETCH_WORKERS = 8
MLS_COMPETITION = "United States/MLS"
EASTERN_TZ = ZoneInfo("America/New_York")

REQUIRED_COLUMNS = [
    "prediction_key",
    "created_at_utc",
    "match_date",
    "competition",
    "home_team",
    "away_team",
    "predicted_result",
    "actual_home_goals",
    "actual_away_goals",
    "actual_result",
    "is_correct",
    "settled_at_utc",
]
TEAM_KEY_ALIASES = {
    "lafc": "losangelesfc",
    "lagalaxy": "losangelesgalaxy",
    "stlouiscity": "stlouiscitysc",
    "stlouiscitysc": "stlouiscitysc",
    "dcunited": "dcunited",
    "newyorkcityfc": "newyorkcity",
    "newyorkredbulls": "newyorkredbulls",
}


def parse_cli_args():
    parser = argparse.ArgumentParser(
        description="Update upcoming predictions with live/final real results and correctness flags."
    )
    parser.add_argument(
        "--cleanup-all",
        action="store_true",
        help="Clear all settled result fields for both European and MLS prediction CSVs.",
    )
    parser.add_argument(
        "--resettle-after-cleanup",
        action="store_true",
        help="When used with --cleanup-all, immediately resettle from ESPN in the same run.",
    )
    parser.add_argument(
        "--cleanup-mls",
        action="store_true",
        help="One-time cleanup: clear MLS settled rows that do not match completed ESPN results.",
    )
    parser.add_argument(
        "--espn-fetch-workers",
        type=int,
        default=DEFAULT_ESPN_FETCH_WORKERS,
        help=f"Concurrent ESPN scoreboard fetches (default: {DEFAULT_ESPN_FETCH_WORKERS}).",
    )
    return parser.parse_args()


def normalize_team_key(name):
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii")
    text = text.strip().lower()
    text = text.replace("&", " and ")
    for ch in ("'", ".", "-", "_", "/", ","):
        text = text.replace(ch, " ")
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
    }
    parts = [p for p in parts if p not in stop_words]
    key = "".join(parts)
    return TEAM_KEY_ALIASES.get(key, key)


def fetch_json(url, headers=None, timeout=30):
    request = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def load_mapping(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_mapping(path, mapping):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    normalized = {}
    for competition, values in (mapping or {}).items():
        if not isinstance(values, dict):
            continue
        normalized[str(competition)] = dict(
            sorted(
                ((str(k).strip(), str(v).strip()) for k, v in values.items() if str(k).strip()),
                key=lambda item: item[0].lower(),
            )
        )
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(normalized, fh, indent=2, ensure_ascii=False)


def save_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)


def load_accuracy_totals(path):
    if not os.path.exists(path):
        return {
            "updated_at_utc": "",
            "overall": {"correct_total": 0, "total_predictions": 0, "accuracy_pct": 0.0},
            "by_league": {},
            "counted_prediction_keys": {},
        }
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return {
            "updated_at_utc": "",
            "overall": {"correct_total": 0, "total_predictions": 0, "accuracy_pct": 0.0},
            "by_league": {},
            "counted_prediction_keys": {},
        }
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("updated_at_utc", "")
    payload.setdefault("overall", {"correct_total": 0, "total_predictions": 0, "accuracy_pct": 0.0})
    payload.setdefault("by_league", {})
    payload.setdefault("counted_prediction_keys", {})
    return payload


def update_accuracy_totals_from_frame(totals, frame):
    if frame is None or frame.empty:
        return 0
    counted = totals.setdefault("counted_prediction_keys", {})
    by_league = totals.setdefault("by_league", {})
    overall = totals.setdefault("overall", {"correct_total": 0, "total_predictions": 0, "accuracy_pct": 0.0})

    added = 0
    for _, row in frame.iterrows():
        actual = str(row.get("actual_result", "")).strip().upper()
        if actual not in {"H", "D", "A"}:
            continue
        key = str(row.get("prediction_key", "")).strip()
        if not key or key in counted:
            continue
        competition = str(row.get("competition", "Unknown")).strip() or "Unknown"
        predicted = str(row.get("predicted_result", "")).strip().upper()
        is_correct = int(predicted == actual)

        league = by_league.setdefault(
            competition,
            {"correct_total": 0, "total_predictions": 0, "accuracy_pct": 0.0},
        )
        league["total_predictions"] = int(league.get("total_predictions", 0)) + 1
        league["correct_total"] = int(league.get("correct_total", 0)) + is_correct
        league["accuracy_pct"] = round(
            100.0 * league["correct_total"] / max(1, league["total_predictions"]), 1
        )

        overall["total_predictions"] = int(overall.get("total_predictions", 0)) + 1
        overall["correct_total"] = int(overall.get("correct_total", 0)) + is_correct
        overall["accuracy_pct"] = round(
            100.0 * overall["correct_total"] / max(1, overall["total_predictions"]), 1
        )

        counted[key] = {
            "competition": competition,
            "predicted_result": predicted,
            "actual_result": actual,
            "is_correct": is_correct,
        }
        added += 1
    return added


def load_shared_mapping():
    shared = load_mapping(SHARED_MAPPING_FILE)
    if shared:
        return shared
    merged = {}
    for legacy in [LEGACY_GLOBAL_MAPPING_FILE, LEGACY_MLS_MAPPING_FILE]:
        legacy_map = load_mapping(legacy)
        for competition, values in legacy_map.items():
            merged.setdefault(competition, {})
            for api_name, mapped_name in (values or {}).items():
                if api_name not in merged[competition]:
                    merged[competition][api_name] = mapped_name
    if merged:
        save_mapping(SHARED_MAPPING_FILE, merged)
    return merged


def load_predictions(path):
    if not os.path.exists(path):
        return None
    frame = pd.read_csv(path)
    for col in REQUIRED_COLUMNS:
        if col not in frame.columns:
            frame[col] = None
    # Settlement columns must accept mixed text/number values.
    object_cols = [
        "prediction_key",
        "match_date",
        "competition",
        "home_team",
        "away_team",
        "predicted_result",
        "actual_result",
        "is_correct",
        "settled_at_utc",
    ]
    for col in object_cols:
        frame[col] = frame[col].astype("object")
    return frame


def infer_result_code(home_goals, away_goals):
    if home_goals > away_goals:
        return "H"
    if away_goals > home_goals:
        return "A"
    return "D"


def choose_score(score_obj):
    if not isinstance(score_obj, dict):
        return None, None
    for bucket in ("fullTime", "regularTime", "halfTime"):
        section = score_obj.get(bucket)
        if not isinstance(section, dict):
            continue
        hg = pd.to_numeric(section.get("home"), errors="coerce")
        ag = pd.to_numeric(section.get("away"), errors="coerce")
        if pd.notna(hg) and pd.notna(ag):
            return int(hg), int(ag)
    return None, None


def event_date_key_for_competition(dt_utc, competition):
    if competition == MLS_COMPETITION:
        return dt_utc.tz_convert(EASTERN_TZ).strftime("%Y-%m-%d")
    return dt_utc.tz_convert("UTC").strftime("%Y-%m-%d")


def update_frame_with_results(frame, results_index):
    if frame is None or frame.empty:
        return frame, 0
    updates = 0
    now_utc = datetime.now(UTC).replace(microsecond=0).isoformat()
    for idx, row in frame.iterrows():
        date_key = str(row.get("match_date", "")).strip()
        comp_key = str(row.get("competition", "")).strip()
        home_key = normalize_team_key(row.get("home_team", ""))
        away_key = normalize_team_key(row.get("away_team", ""))
        if not date_key or not comp_key or not home_key or not away_key:
            continue
        key = (date_key, comp_key, home_key, away_key)
        result = results_index.get(key)
        if result is None:
            key_rev = (date_key, comp_key, away_key, home_key)
            result = results_index.get(key_rev)
            if result is None:
                continue
            result = {
                "actual_home_goals": result["actual_away_goals"],
                "actual_away_goals": result["actual_home_goals"],
                "actual_result": "A" if result["actual_result"] == "H" else "H" if result["actual_result"] == "A" else "D",
                "completed": bool(result.get("completed")),
            }
        if not bool(result.get("completed")):
            continue

        prev_hg = pd.to_numeric(row.get("actual_home_goals"), errors="coerce")
        prev_ag = pd.to_numeric(row.get("actual_away_goals"), errors="coerce")
        prev_res = str(row.get("actual_result", "")).strip().upper()
        prev_correct = str(row.get("is_correct", "")).strip()
        new_correct = "1" if str(row.get("predicted_result", "")).strip().upper() == result["actual_result"] else "0"

        changed = (
            pd.isna(prev_hg) or int(prev_hg) != int(result["actual_home_goals"]) or
            pd.isna(prev_ag) or int(prev_ag) != int(result["actual_away_goals"]) or
            prev_res != result["actual_result"] or
            prev_correct != new_correct
        )
        if not changed:
            continue

        frame.at[idx, "actual_home_goals"] = result["actual_home_goals"]
        frame.at[idx, "actual_away_goals"] = result["actual_away_goals"]
        frame.at[idx, "actual_result"] = result["actual_result"]
        frame.at[idx, "is_correct"] = new_correct
        frame.at[idx, "settled_at_utc"] = now_utc
        updates += 1
    return frame, updates


def clear_future_settled_rows(frame, today_date):
    if frame is None or frame.empty:
        return frame, 0
    cleared = 0
    for idx, row in frame.iterrows():
        existing_result = str(row.get("actual_result", "")).strip().upper()
        if existing_result not in {"H", "D", "A"}:
            continue
        match_date = pd.to_datetime(row.get("match_date"), errors="coerce")
        if pd.isna(match_date):
            continue
        if match_date.date() <= today_date:
            continue
        frame.at[idx, "actual_home_goals"] = None
        frame.at[idx, "actual_away_goals"] = None
        frame.at[idx, "actual_result"] = None
        frame.at[idx, "is_correct"] = None
        frame.at[idx, "settled_at_utc"] = None
        cleared += 1
    return frame, cleared


def cleanup_settled_rows_not_in_results(frame, results_index):
    if frame is None or frame.empty:
        return frame, 0
    cleaned = 0
    for idx, row in frame.iterrows():
        existing_result = str(row.get("actual_result", "")).strip().upper()
        if existing_result not in {"H", "D", "A"}:
            continue

        date_key = str(row.get("match_date", "")).strip()
        comp_key = str(row.get("competition", "")).strip()
        home_key = normalize_team_key(row.get("home_team", ""))
        away_key = normalize_team_key(row.get("away_team", ""))
        if not date_key or not comp_key or not home_key or not away_key:
            continue

        key = (date_key, comp_key, home_key, away_key)
        key_rev = (date_key, comp_key, away_key, home_key)
        if key in results_index or key_rev in results_index:
            continue

        frame.at[idx, "actual_home_goals"] = None
        frame.at[idx, "actual_away_goals"] = None
        frame.at[idx, "actual_result"] = None
        frame.at[idx, "is_correct"] = None
        frame.at[idx, "settled_at_utc"] = None
        cleaned += 1
    return frame, cleaned


def cleanup_all_settled_rows(frame):
    if frame is None or frame.empty:
        return frame, 0
    cleaned = 0
    for idx, row in frame.iterrows():
        existing_result = str(row.get("actual_result", "")).strip().upper()
        if existing_result not in {"H", "D", "A"}:
            continue
        frame.at[idx, "actual_home_goals"] = None
        frame.at[idx, "actual_away_goals"] = None
        frame.at[idx, "actual_result"] = None
        frame.at[idx, "is_correct"] = None
        frame.at[idx, "settled_at_utc"] = None
        cleaned += 1
    return frame, cleaned


def drop_completed_rows(frame, today=None):
    """Drop completed (settled) rows, but only for dates *before* today.

    Games from today are kept even if settled so they still appear in
    ``/api/upcoming/global`` — the frontend can show the actual result
    alongside the prediction.  Pass ``today=None`` (default) to drop all
    settled rows regardless of date (legacy / cleanup mode).
    """
    if frame is None or frame.empty or "actual_result" not in frame.columns:
        return frame, 0
    settled_mask = frame["actual_result"].astype(str).str.strip().str.upper().isin({"H", "D", "A"})
    if today is not None and "match_date" in frame.columns:
        parsed_dates = pd.to_datetime(frame["match_date"], errors="coerce").dt.normalize()
        today_ts = pd.Timestamp(today)
        # Only drop settled rows whose match date is strictly before today
        drop_mask = settled_mask & (parsed_dates < today_ts)
    else:
        drop_mask = settled_mask
    removed = int(drop_mask.sum())
    if removed == 0:
        return frame, 0
    return frame[~drop_mask].copy(), removed


def resolve_espn_team_name(raw_name, competition, mapping_by_competition, predicted_team_names):
    raw = str(raw_name or "").strip()
    if not raw:
        return ""
    comp_map = mapping_by_competition.get(competition, {}) if isinstance(mapping_by_competition, dict) else {}

    direct = str(comp_map.get(raw, "")).strip()
    if direct:
        return direct, True

    raw_key = normalize_team_key(raw)

    # Try normalized lookup against mapping keys.
    for map_key, map_val in comp_map.items():
        if normalize_team_key(map_key) == raw_key:
            mapped = str(map_val).strip()
            if mapped:
                return mapped, True

    # Direct match against prediction team names.
    if raw in predicted_team_names:
        return raw, True

    by_key = {}
    for team in predicted_team_names:
        by_key.setdefault(normalize_team_key(team), []).append(team)
    candidates = by_key.get(raw_key, [])
    if len(candidates) == 1:
        return candidates[0], True

    close = difflib.get_close_matches(raw_key, list(by_key.keys()), n=1, cutoff=0.9)
    if close:
        teams = by_key.get(close[0], [])
        if len(teams) == 1:
            return teams[0], True

    return raw, False


def build_results_index_from_espn(predictions_df, mapping_by_competition, fetch_workers=8):
    if predictions_df is None or predictions_df.empty:
        return {}, {}, {}, {}
    results = {}
    mapping_updates = {}
    unresolved = {}
    seen_names = {}

    # Pre-compute (competition, league_key, query_days) plans for the subset of competitions
    # that have an ESPN mapping. Scoreboard fetches are independent across (competition, day)
    # so we can fire them concurrently with a thread pool.
    plans = []
    for competition in sorted(set(predictions_df["competition"].astype(str).str.strip())):
        if not competition:
            continue
        league_key = ESPN_COMPETITION_KEYS.get(competition)
        if not league_key:
            print(f"Skipping {competition}: no ESPN league mapping configured.")
            continue
        subset = predictions_df[predictions_df["competition"].astype(str).str.strip() == competition]
        if subset.empty:
            continue
        predicted_team_names = set(subset["home_team"].astype(str)) | set(subset["away_team"].astype(str))
        date_series = pd.to_datetime(subset["match_date"], errors="coerce")
        date_series = date_series[date_series.notna()]
        if date_series.empty:
            continue
        base_days = sorted(set(pd.Timestamp(dt).normalize() for dt in date_series))
        query_days = set(base_days)
        if competition == MLS_COMPETITION:
            for day in base_days:
                query_days.add(day - pd.Timedelta(days=1))
                query_days.add(day + pd.Timedelta(days=1))
        date_codes = sorted(day.strftime("%Y%m%d") for day in query_days)
        plans.append(
            {
                "competition": competition,
                "league_key": league_key,
                "date_codes": date_codes,
                "predicted_team_names": predicted_team_names,
            }
        )

    if not plans:
        return {}, {}, {}, {}

    def _fetch_one(competition, league_key, yyyymmdd):
        url = f"{ESPN_BASE}/{league_key}/scoreboard?dates={yyyymmdd}"
        try:
            data = fetch_json(url, timeout=45)
            return (competition, yyyymmdd, data, None)
        except Exception as error:
            return (competition, yyyymmdd, None, error)

    tasks = []
    for plan in plans:
        for yyyymmdd in plan["date_codes"]:
            tasks.append((plan["competition"], plan["league_key"], yyyymmdd))

    fetched = {}
    if fetch_workers <= 1 or len(tasks) <= 1:
        for comp, yyyymmdd, data, err in (_fetch_one(c, k, d) for c, k, d in tasks):
            if err is not None:
                print(f"Skipping {comp} date {yyyymmdd}: {err}")
                continue
            fetched.setdefault(comp, []).append((yyyymmdd, data))
    else:
        max_workers = max(1, min(int(fetch_workers), len(tasks)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_meta = {
                executor.submit(_fetch_one, comp, league_key, yyyymmdd): (comp, yyyymmdd)
                for comp, league_key, yyyymmdd in tasks
            }
            for fut in as_completed(future_to_meta):
                comp, yyyymmdd = future_to_meta[fut]
                _, _, data, err = fut.result()
                if err is not None:
                    print(f"Skipping {comp} date {yyyymmdd}: {err}")
                    continue
                fetched.setdefault(comp, []).append((yyyymmdd, data))

    for plan in plans:
        competition = plan["competition"]
        predicted_team_names = plan["predicted_team_names"]
        unresolved.setdefault(competition, set())
        seen_names.setdefault(competition, set())
        for _, data in sorted(fetched.get(competition, []), key=lambda item: item[0]):
            events = data.get("events", [])
            if not isinstance(events, list):
                continue
            for event in events:
                dt = pd.to_datetime(event.get("date"), utc=True, errors="coerce")
                if pd.isna(dt):
                    continue
                date_key = event_date_key_for_competition(dt, competition)
                comps = event.get("competitions", [])
                if not comps:
                    continue
                comp0 = comps[0] or {}
                status_type = ((comp0.get("status") or {}).get("type") or {})
                if not bool(status_type.get("completed")):
                    continue
                competitors = comp0.get("competitors", [])
                home_name = ""
                away_name = ""
                home_score = None
                away_score = None
                for c in competitors:
                    side = str(c.get("homeAway", "")).strip().lower()
                    team_name = str((c.get("team") or {}).get("displayName") or "").strip()
                    score_val = pd.to_numeric(c.get("score"), errors="coerce")
                    if side == "home":
                        if team_name:
                            seen_names[competition].add(team_name)
                        home_name, home_ok = resolve_espn_team_name(
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
                        away_name, away_ok = resolve_espn_team_name(
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
                key = (date_key, competition, normalize_team_key(home_name), normalize_team_key(away_name))
                results[key] = {
                    "actual_home_goals": home_score,
                    "actual_away_goals": away_score,
                    "actual_result": infer_result_code(home_score, away_score),
                    "completed": True,
                }

    unresolved = {k: sorted(v) for k, v in unresolved.items() if v}
    seen_names = {k: sorted(v) for k, v in seen_names.items()}
    return results, mapping_updates, unresolved, seen_names


def apply_mapping_updates(base_mapping, updates):
    merged = dict(base_mapping) if isinstance(base_mapping, dict) else {}
    added = 0
    changed = 0
    for competition, values in (updates or {}).items():
        merged.setdefault(competition, {})
        if not isinstance(merged[competition], dict):
            merged[competition] = {}
        for api_name, mapped_name in values.items():
            api = str(api_name).strip()
            mapped = str(mapped_name).strip()
            if not api or not mapped:
                continue
            existing = str(merged[competition].get(api, "")).strip()
            if not existing:
                merged[competition][api] = mapped
                added += 1
            elif existing != mapped:
                # Keep existing user mapping, but count drift.
                changed += 1
    return merged, added, changed


def main():
    args = parse_cli_args()

    global_df = load_predictions(GLOBAL_PREDICTIONS_FILE)
    mls_df = load_predictions(MLS_PREDICTIONS_FILE)
    totals = load_accuracy_totals(ACCURACY_TOTALS_FILE)

    if global_df is None and mls_df is None:
        raise ValueError("No predictions CSV files found to update.")

    shared_mapping = load_shared_mapping()

    needs_resettle = (not args.cleanup_all) or args.resettle_after_cleanup
    if needs_resettle:
        global_results, global_mapping_updates, global_unresolved, global_seen_names = build_results_index_from_espn(
            global_df, shared_mapping, fetch_workers=args.espn_fetch_workers
        )
        mls_results, mls_mapping_updates, mls_unresolved, mls_seen_names = build_results_index_from_espn(
            mls_df, shared_mapping, fetch_workers=args.espn_fetch_workers
        )
    else:
        global_results, global_mapping_updates, global_unresolved, global_seen_names = {}, {}, {}, {}
        mls_results, mls_mapping_updates, mls_unresolved, mls_seen_names = {}, {}, {}, {}

    shared_mapping, global_added, global_drift = apply_mapping_updates(shared_mapping, global_mapping_updates)
    shared_mapping, mls_added, mls_drift = apply_mapping_updates(shared_mapping, mls_mapping_updates)
    save_mapping(SHARED_MAPPING_FILE, shared_mapping)
    save_json(
        ESPN_NAMES_FILE,
        {
            "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "global": global_seen_names,
            "mls": mls_seen_names,
        },
    )

    global_updates = 0
    global_cleaned = 0
    global_removed_completed = 0
    if global_df is not None:
        if args.cleanup_all:
            global_df, global_cleaned = cleanup_all_settled_rows(global_df)
        if needs_resettle:
            global_df, global_updates = update_frame_with_results(global_df, global_results)
        global_totals_added = update_accuracy_totals_from_frame(totals, global_df)
        today_et = datetime.now(EASTERN_TZ).date()
        prev_monday = today_et - timedelta(days=today_et.weekday() + 7)
        global_df, global_removed_completed = drop_completed_rows(global_df, today=prev_monday)
        global_df.to_csv(GLOBAL_PREDICTIONS_FILE, index=False)
    else:
        global_totals_added = 0

    mls_updates = 0
    mls_cleaned = 0
    mls_future_cleared = 0
    mls_removed_completed = 0
    if mls_df is not None:
        today_et = datetime.now(EASTERN_TZ).date()
        mls_df, mls_future_cleared = clear_future_settled_rows(mls_df, today_et)
        if args.cleanup_all:
            mls_df, mls_cleaned = cleanup_all_settled_rows(mls_df)
        elif args.cleanup_mls:
            mls_df, mls_cleaned = cleanup_settled_rows_not_in_results(mls_df, mls_results)
        if needs_resettle:
            mls_df, mls_updates = update_frame_with_results(mls_df, mls_results)
        mls_totals_added = update_accuracy_totals_from_frame(totals, mls_df)
        mls_df, mls_removed_completed = drop_completed_rows(mls_df, today=prev_monday)
        mls_df.to_csv(MLS_PREDICTIONS_FILE, index=False)
    else:
        mls_totals_added = 0

    totals["updated_at_utc"] = datetime.now(UTC).replace(microsecond=0).isoformat()
    save_json(ACCURACY_TOTALS_FILE, totals)

    if args.cleanup_all:
        print(f"Global predictions cleanup cleared: {global_cleaned}")
        print(f"MLS predictions cleanup cleared: {mls_cleaned}")
    print(f"Global mapping auto-added: {global_added} (drift detected: {global_drift})")
    print(f"MLS mapping auto-added: {mls_added} (drift detected: {mls_drift})")
    if global_unresolved:
        print(f"Global unresolved ESPN names by league: {global_unresolved}")
    if mls_unresolved:
        print(f"MLS unresolved ESPN names by league: {mls_unresolved}")
    print(f"MLS future-settled rows cleared: {mls_future_cleared}")
    print(f"Global predictions updated: {global_updates}")
    print(f"Global completed rows removed from upcoming list: {global_removed_completed}")
    print(f"Global totals entries added: {global_totals_added}")
    if args.cleanup_mls and not args.cleanup_all:
        print(f"MLS predictions cleanup cleared: {mls_cleaned}")
    print(f"MLS predictions updated: {mls_updates}")
    print(f"MLS completed rows removed from upcoming list: {mls_removed_completed}")
    print(f"MLS totals entries added: {mls_totals_added}")
    print(f"ESPN names seen file: {ESPN_NAMES_FILE}")
    print(f"Accuracy totals file: {ACCURACY_TOTALS_FILE}")
    print("Done.")


if __name__ == "__main__":
    main()
