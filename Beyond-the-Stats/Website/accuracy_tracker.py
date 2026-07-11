"""Prediction accuracy tracking and success metrics."""
import json
import os

import pandas as pd
from datetime import datetime, timedelta

import config
from config import CUP_COMPETITIONS, MLS_COMPETITION
from predictions import _load_json_payload, _utc_to_et

HISTORY_COLUMNS = [
    "prediction_key",
    "match_date",
    "competition",
    "home_team",
    "away_team",
    "predicted_result",
    "actual_result",
    "is_correct",
]

_UEFA_COMPETITIONS = {
    "UEFA/Champions League", "UEFA/Europa League", "UEFA/Conference League",
    "Europe/Champions League", "Europe/Europa League", "Europe/Conference League",
}

_UPCOMING_CSV_MODE_MAP = {
    "England/Premier League": "global",
    "England/Championship": "global",
    "Spain/La Liga": "global",
    "Spain/La Liga 2": "global",
    "Italy/Serie A": "global",
    "Italy/Serie B": "global",
    "Germany/Bundesliga": "global",
    "Germany/Bundesliga 2": "global",
    "France/Ligue 1": "global",
    "France/Ligue 2": "global",
    "Portugal/Liga Portugal": "global",
    "Netherlands/Eredivisie": "extra",
    "England/FA Cup": "cups",
    "England/League Cup": "cups",
    "UEFA/Champions League": "cups",
    "UEFA/Europa League": "cups",
    "UEFA/Conference League": "cups",
    "Europe/Champions League": "cups",
    "Europe/Europa League": "cups",
    "Europe/Conference League": "cups",
    "Italy/Coppa Italia": "cups",
    "Spain/Copa del Rey": "cups",
    "Germany/DFB-Pokal": "cups",
    "France/Coupe de France": "cups",
    "United States/US Open Cup": "cups",
    "FIFA/World Cup": "national",
    "FIFA/Friendly": "national",
    "Club Friendlies": "friendlies",
    "UEFA/European Championship": "national",
    "UEFA/Nations League": "national",
    "CONMEBOL/Copa America": "national",
    "United States/MLS": "mls",
    "Belgium/First Division A": "extra",
    "Scotland/Premiership": "extra",
    "Turkey/Super Lig": "extra",
    "Austria/Bundesliga": "extra",
    "Greece/Super League": "extra",
    "Norway/Eliteserien": "extra",
    "Romania/Liga I": "extra",
    "Sweden/Allsvenskan": "extra",
    "Poland/Ekstraklasa": "extra",
    "Mexico/Liga MX": "mls",
    "Argentina/Primera Division": "extra",
    "Brazil/Serie A": "extra",
    "Japan/J1 League": "extra",
    "CONCACAF/Leagues Cup": "cups",
    "Azerbaijan/Premier League": "extra",
    "Kazakhstan/Premier League": "extra",
    "Belarus/Premier League": "extra",
    "Moldova/Super Liga": "extra",
}


def _get_week_start(dt, competition):
    """Return the start of the current prediction week for a competition.

    Most leagues: Thursday through Wednesday (matches on weekends).
    UEFA competitions: Monday through Friday (UCL/UEL/UECL midweek).
    """
    if competition in _UEFA_COMPETITIONS:
        return dt - timedelta(days=dt.weekday())  # Monday = 0
    days_since_thursday = (dt.weekday() - 3) % 7
    return dt - timedelta(days=days_since_thursday)


def _load_prediction_tracking():
    if not os.path.exists(config.PREDICTION_TRACKING_FILE):
        return {"all_time": {"total": {"correct": 0, "incorrect": 0}, "by_league": {}},
                "weekly": {}, "per_team": {}}
    try:
        with open(config.PREDICTION_TRACKING_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"all_time": {"total": {"correct": 0, "incorrect": 0}, "by_league": {}},
                "weekly": {}, "per_team": {}}


def _save_prediction_tracking(data):
    os.makedirs(os.path.dirname(config.PREDICTION_TRACKING_FILE), exist_ok=True)
    with open(config.PREDICTION_TRACKING_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _load_predictions_for_competition(comp_name):
    """Load upcoming prediction rows from the CSV matching *comp_name*."""
    mode = _UPCOMING_CSV_MODE_MAP.get(comp_name)
    if not mode:
        return []
    csv_path = config.UPCOMING_CSV_FILES.get(mode)
    if not csv_path or not os.path.exists(csv_path):
        return []
    try:
        df = pd.read_csv(csv_path, dtype=str)
    except Exception:
        return []
    rows = []
    for _, row in df.iterrows():
        comp = str(row.get("competition", "") or "").strip()
        if comp != comp_name:
            continue
        rows.append({
            "home_team": str(row.get("home_team", "") or "").strip(),
            "away_team": str(row.get("away_team", "") or "").strip(),
            "match_datetime_utc": _utc_to_et(str(row.get("match_datetime_utc", "") or "").strip()),
            "predicted_result": str(row.get("predicted_result", "") or "").strip(),
            "prob_home": str(row.get("prob_home", "") or "").strip(),
            "prob_draw": str(row.get("prob_draw", "") or "").strip(),
            "prob_away": str(row.get("prob_away", "") or "").strip(),
        })
    return rows


def _compute_actual_result(home_score, away_score):
    if home_score is None or away_score is None:
        return None
    if home_score > away_score:
        return "H"
    if away_score > home_score:
        return "A"
    return "D"


def _ensure(d, *keys):
    for k in keys:
        d = d.setdefault(k, {"correct": 0, "incorrect": 0})
    return d


def _track_prediction_results(completed_games):
    """Match completed ESPN games against our CSV predictions and update tracking."""
    tracking = _load_prediction_tracking()
    now = datetime.now()
    for g in completed_games:
        comp = g.get("competition", "")
        home = g.get("home_team", "").strip().lower()
        away = g.get("away_team", "").strip().lower()
        hs = g.get("home_score")
        aws = g.get("away_score")
        actual = _compute_actual_result(hs, aws)
        if not actual:
            continue

        # Find matching prediction row
        predictions = _load_predictions_for_competition(comp)
        matched = None
        for p in predictions:
            if p["home_team"].strip().lower() == home and p["away_team"].strip().lower() == away:
                matched = p
                break
            # Try reversed (home/away might be swapped in CSV)
            if p["home_team"].strip().lower() == away and p["away_team"].strip().lower() == home:
                matched = p
                actual = {"H": "A", "A": "H", "D": "D"}[actual]
                break

        if not matched:
            continue

        pred = matched["predicted_result"]
        correct = pred == actual

        # -- All-time --
        _ensure(tracking, "all_time", "total")
        if correct:
            tracking["all_time"]["total"]["correct"] += 1
        else:
            tracking["all_time"]["total"]["incorrect"] += 1

        _ensure(tracking, "all_time", "by_league", comp)
        if correct:
            tracking["all_time"]["by_league"][comp]["correct"] += 1
        else:
            tracking["all_time"]["by_league"][comp]["incorrect"] += 1

        # -- Weekly --
        week_start = _get_week_start(now, comp)
        week_key = week_start.isoformat()
        weekly = tracking.setdefault("weekly", {})
        current_week = weekly.setdefault(week_key, {"week_start": week_key, "by_league": {}})
        wl = current_week["by_league"].setdefault(comp, {"correct": 0, "incorrect": 0})
        if correct:
            wl["correct"] += 1
        else:
            wl["incorrect"] += 1

        # -- Per-team tracking (last 10) --
        teams_to_update = [
            (g.get("home_team", "").strip(), g.get("away_team", "").strip()),
            (g.get("away_team", "").strip(), g.get("home_team", "").strip()),
        ]
        for team_name, opponent in teams_to_update:
            if not team_name:
                continue
            pentry = {
                "match_id": g.get("match_id", ""),
                "opponent": opponent,
                "competition": comp,
                "prediction": pred,
                "prob_home": matched.get("prob_home", ""),
                "prob_draw": matched.get("prob_draw", ""),
                "prob_away": matched.get("prob_away", ""),
                "actual_result": actual,
                "correct": correct,
                "kickoff_utc": _utc_to_et(g.get("kickoff_utc", "")),
            }
            pt = tracking.setdefault("per_team", {})
            tdata = pt.setdefault(team_name, {"predictions": []})
            tdata["predictions"].insert(0, pentry)
            tdata["predictions"] = tdata["predictions"][:10]
            total = len(tdata["predictions"])
            tdata["total"] = total
            tdata["accuracy"] = round(sum(1 for x in tdata["predictions"] if x["correct"]) / total, 3) if total else 0

    _save_prediction_tracking(tracking)

def _compute_accuracy_stats(frame):
    """Compute aggregate accuracy counters from a predictions dataframe."""
    if frame.empty:
        return {
            "total_predictions": 0,
            "settled_total": 0,
            "correct_total": 0,
            "pending_total": 0,
            "accuracy_pct": 0.0,
        }

    if "actual_result" in frame.columns:
        settled_mask = frame["actual_result"].astype(str).str.strip().isin({"H", "D", "A"})
    else:
        settled_mask = pd.Series([False] * len(frame), index=frame.index)
    settled = frame[settled_mask].copy()
    if settled.empty:
        return {
            "total_predictions": int(len(frame)),
            "settled_total": 0,
            "correct_total": 0,
            "pending_total": int(len(frame)),
            "accuracy_pct": 0.0,
        }

    correct = (
        settled["predicted_result"].astype(str).str.strip().str.upper()
        == settled["actual_result"].astype(str).str.strip().str.upper()
    ).sum()

    settled_total = int(len(settled))
    correct_total = int(correct)
    accuracy = round((100.0 * correct_total / settled_total), 1) if settled_total else 0.0
    return {
        "total_predictions": int(len(frame)),
        "settled_total": settled_total,
        "correct_total": correct_total,
        "pending_total": int(len(frame) - settled_total),
        "accuracy_pct": accuracy,
    }


def _compute_league_accuracy_stats(frame):
    """Compute accuracy counters grouped by competition."""
    if frame.empty or "competition" not in frame.columns:
        return []

    rows = []
    grouped = frame.groupby("competition", dropna=False)
    for competition, comp_frame in grouped:
        stats = _compute_accuracy_stats(comp_frame)
        rows.append(
            {
                "competition": str(competition),
                "correct_total": stats["correct_total"],
                "settled_total": stats["settled_total"],
                "pending_total": stats["pending_total"],
                "total_predictions": stats["total_predictions"],
                "accuracy_pct": stats["accuracy_pct"],
            }
        )
    rows.sort(key=lambda item: item["competition"].lower())
    return rows


def _load_accuracy_totals():
    """Load persistent all-time accuracy totals written by the live updater."""
    payload = _load_json_payload(config.ACCURACY_TOTALS_FILE)
    if not isinstance(payload, dict):
        return {"overall": {}, "by_league": {}}
    overall = payload.get("overall")
    by_league = payload.get("by_league")
    if not isinstance(overall, dict):
        overall = {}
    if not isinstance(by_league, dict):
        by_league = {}
    return {"overall": overall, "by_league": by_league}


def _build_persistent_accuracy_stats(mode, rows):
    """Build response stats by combining persistent settled totals with current pending rows."""
    totals = _load_accuracy_totals()
    by_league_all = totals.get("by_league", {})
    if mode == "mls":
        filtered = {
            str(k): v for k, v in by_league_all.items()
            if str(k).strip() == MLS_COMPETITION
        }
    elif mode == "extra":
        filtered = {}
    elif mode == "national":
        filtered = {}
    elif mode == "cups":
        filtered = {
            str(k): v for k, v in by_league_all.items()
            if str(k).strip() in CUP_COMPETITIONS
        }
    elif mode == "friendlies":
        filtered = {
            str(k): v for k, v in by_league_all.items()
            if str(k).strip() == config.CLUB_FRIENDLIES_COMPETITION
        }
    else:
        filtered = {
            str(k): v for k, v in by_league_all.items()
            if str(k).strip() != MLS_COMPETITION and str(k).strip() not in CUP_COMPETITIONS
        }

    pending_by_league = {}
    for row in rows:
        comp = str(row.get("competition", "")).strip() or "Unknown"
        pending_by_league[comp] = pending_by_league.get(comp, 0) + 1

    league_stats = []
    comps = sorted(set(filtered.keys()) | set(pending_by_league.keys()), key=lambda name: name.lower())
    correct_sum = 0
    settled_sum = 0
    for comp in comps:
        league_payload = filtered.get(comp, {}) if isinstance(filtered.get(comp), dict) else {}
        correct_total = int(league_payload.get("correct_total", 0) or 0)
        settled_total = int(league_payload.get("total_predictions", 0) or 0)
        pending_total = int(pending_by_league.get(comp, 0))
        accuracy_pct = round((100.0 * correct_total / settled_total), 1) if settled_total else 0.0
        league_stats.append(
            {
                "competition": comp,
                "correct_total": correct_total,
                "settled_total": settled_total,
                "pending_total": pending_total,
                "total_predictions": settled_total,
                "accuracy_pct": accuracy_pct,
            }
        )
        correct_sum += correct_total
        settled_sum += settled_total

    stats = {
        "total_predictions": settled_sum,
        "settled_total": settled_sum,
        "correct_total": correct_sum,
        "pending_total": int(len(rows)),
        "accuracy_pct": round((100.0 * correct_sum / settled_sum), 1) if settled_sum else 0.0,
    }
    return stats, league_stats

def _safe_filename(name):
    """Convert league names into filesystem-safe filenames."""
    text = "".join(ch if ch.isalnum() else "_" for ch in str(name or "").strip())
    text = "_".join(part for part in text.split("_") if part)
    return text[:120] or "unknown_league"

def _update_accuracy_history_from_csv(csv_path, source_key):
    """Append settled predictions into per-league accuracy history CSV files.

    Each per-league file stores only the singular values needed to compute
    accuracy (one row per match). Derived metrics (totals/percentages) and
    prediction detail columns (probabilities, predicted goals/shots, etc.)
    are intentionally excluded — they can be recomputed on demand from
    these rows.
    """
    if not os.path.exists(csv_path):
        return 0, 0
    try:
        frame = pd.read_csv(csv_path)
    except Exception:
        return 0, 0
    if frame.empty or "competition" not in frame.columns or "prediction_key" not in frame.columns:
        return 0, 0

    source_dir = os.path.join(config.ACCURACY_HISTORY_DIR, source_key)
    os.makedirs(source_dir, exist_ok=True)
    files_touched = 0
    rows_added = 0
    if "actual_result" in frame.columns:
        settled_mask = frame["actual_result"].astype(str).str.strip().isin({"H", "D", "A"})
        settled = frame[settled_mask].copy()
    else:
        settled = pd.DataFrame(columns=HISTORY_COLUMNS)

    all_competitions = sorted(set(frame["competition"].astype(str).str.strip()))
    for competition in all_competitions:
        league_name = str(competition).strip() or "Unknown"
        league_file = os.path.join(source_dir, f"{_safe_filename(league_name)}.csv")
        comp_data = settled[settled["competition"].astype(str).str.strip() == league_name].copy()
        if not comp_data.empty:
            comp_data = comp_data.reindex(columns=HISTORY_COLUMNS).copy()
            comp_data["competition"] = league_name
        else:
            comp_data = pd.DataFrame(columns=HISTORY_COLUMNS)

        if os.path.exists(league_file):
            try:
                existing = pd.read_csv(league_file)
            except Exception:
                existing = pd.DataFrame(columns=HISTORY_COLUMNS)
        else:
            existing = pd.DataFrame(columns=HISTORY_COLUMNS)

        # Existing files written by the old schema get re-projected to the
        # new singular-value schema on the next read; missing columns are
        # introduced as empty so the concat stays consistent.
        for col in HISTORY_COLUMNS:
            if col not in existing.columns:
                existing[col] = pd.Series(dtype="object")
        existing = existing.reindex(columns=HISTORY_COLUMNS)

        before = len(existing)
        merged = pd.concat([existing, comp_data], ignore_index=True) if not comp_data.empty else existing.copy()
        if not merged.empty:
            merged = merged.drop_duplicates(subset=["prediction_key"], keep="last")
        after = len(merged)
        merged.to_csv(league_file, index=False)
        files_touched += 1
        rows_added += max(0, after - before)

    return files_touched, rows_added


def update_accuracy_history_files():
    """Refresh global, MLS, extra-league, and cup accuracy history stores."""
    os.makedirs(config.ACCURACY_HISTORY_DIR, exist_ok=True)
    global_files, global_rows = _update_accuracy_history_from_csv(config.GLOBAL_UPCOMING_FILE, "global")
    mls_files, mls_rows = _update_accuracy_history_from_csv(config.MLS_UPCOMING_FILE, "mls")
    extra_files, extra_rows = _update_accuracy_history_from_csv(config.EXTRA_UPCOMING_FILE, "extra")
    cup_files, cup_rows = _update_accuracy_history_from_csv(config.CUP_COMPLETED_FILE, "cups")
    print(
        "[startup] Accuracy history updated: "
        f"global_files={global_files}, global_new_rows={global_rows}, "
        f"mls_files={mls_files}, mls_new_rows={mls_rows}, "
        f"extra_files={extra_files}, extra_new_rows={extra_rows}, "
        f"cup_files={cup_files}, cup_new_rows={cup_rows}"
    )
