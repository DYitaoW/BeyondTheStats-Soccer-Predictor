"""
Upcoming matches CSV caching, past games lookup, and live game enrichment services.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd

import config
from team_utils import _normalize_team_key, _team_name_for_display, _to_float

_static_predictions_cache: dict[str, dict] = {}
_PAST_GAME_PREDICTION_LOOKUP_CACHE: dict | None = None
_PAST_GAME_PREDICTION_LOOKUP_LOCK = threading.Lock()


def _valid_date_iso(s: str) -> bool:
    """Return True if s matches YYYY-MM-DD (ISO 8601 date)."""
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(s or "")))


def _utc_to_et(utc_iso: str) -> str:
    """Convert UTC ISO timestamp to America/New_York ISO timestamp string."""
    if not utc_iso:
        return ""
    try:
        dt = pd.to_datetime(utc_iso, utc=True, errors="coerce")
        if pd.notna(dt):
            return dt.tz_convert("America/New_York").isoformat()
    except Exception:
        pass
    return ""


def _parse_iso_datetime(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _format_percent_value(value: object) -> str:
    """Format percent values and clamp tiny non-zero values to '<1'."""
    try:
        v = float(value)
    except Exception:
        return "0"
    if 0.0 < v < 1.0:
        return "<1"
    return f"{v:.1f}"


def _winner_label(code: str, home_team: str, away_team: str) -> str:
    """Convert H/D/A code to a display winner label."""
    code = str(code or "").strip().upper()
    if code == "H":
        return f"{home_team}"
    if code == "A":
        return f"{away_team}"
    return "Draw"


def _to_float_or_none(value: object) -> float | None:
    try:
        num = float(value)
    except Exception:
        return None
    if pd.isna(num):
        return None
    return float(num)


def _to_int(value: object) -> int:
    try:
        num = float(value)
    except Exception:
        return 0
    if pd.isna(num):
        return 0
    return int(round(num))


def _is_test_live_game(r: dict) -> bool:
    match_id = str(r.get("match_id", "")).strip().lower()
    if match_id.startswith("test-") or "test-past-games" in match_id:
        return True
    source = str(r.get("source", "")).strip().lower()
    return source == "test"


def _is_placeholder_game(r: dict) -> bool:
    """Return True if a game dict is a placeholder (not a real match)."""
    if _is_test_live_game(r):
        return True
    for key in ("home_team", "away_team"):
        val = str(r.get(key, "")).lower()
        if "group" in val or "third place" in val or "winner" in val or "runner" in val:
            return True
    return False


def _past_row_date_iso(row: dict) -> str:
    """Normalize any past-game row to an ISO date (YYYY-MM-DD) in US/Eastern."""
    for key in (
        "match_date_iso", "match_date", "kickoff_utc", "kickoff_et",
        "match_datetime_utc", "completed_at", "scoreboard_date",
    ):
        raw = str(row.get(key, "") or "").strip()
        if not raw:
            continue
        if len(raw) == 10 and _valid_date_iso(raw):
            return raw
        if len(raw) == 8 and raw.isdigit():
            try:
                return datetime.strptime(raw, "%Y%m%d").date().isoformat()
            except ValueError:
                pass
        try:
            text = raw.replace("Z", "+00:00")
            if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
                text = text[:-2] + ":" + text[-2:]
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        except Exception:
            pass
        try:
            parsed = pd.to_datetime(raw, errors="coerce", utc=True)
            if pd.notna(parsed):
                if parsed.tzinfo is None:
                    parsed = parsed.tz_localize("UTC")
                return parsed.tz_convert("America/New_York").date().isoformat()
        except Exception:
            continue
    return ""


def _past_row_looks_api_complete(row: dict) -> bool:
    """True if row contains actual goals and result."""
    actual = str(row.get("actual_result", "")).strip().upper()
    if actual not in {"H", "D", "A"}:
        return False
    return (
        row.get("actual_home_goals") is not None
        and row.get("actual_away_goals") is not None
    )


def _enrich_json_past_row(r: dict) -> None:
    """Add display fields to a raw past_games.json row."""
    pred = str(r.get("predicted_result", "")).strip().upper()
    home = str(r.get("home_team", "")).strip()
    away = str(r.get("away_team", "")).strip()
    r["winner_label"] = (
        f"Pred: {home}" if pred == "H" else
        f"Pred: {away}" if pred == "A" else
        "Pred: Draw" if pred == "D" else ""
    )
    actual = str(r.get("actual_result", "")).strip().upper()
    if actual in {"H", "D", "A"}:
        r["is_correct"] = "1" if pred == actual else "0"
    else:
        r["is_correct"] = ""

    try:
        ph = float(r.get("prob_home", 0) or 0) * 100
        pdv = float(r.get("prob_draw", 0) or 0) * 100
        pa = float(r.get("prob_away", 0) or 0) * 100
    except Exception:
        ph = pdv = pa = 0.0
    r["prob_home_text"] = _format_percent_value(ph)
    r["prob_draw_text"] = _format_percent_value(pdv)
    r["prob_away_text"] = _format_percent_value(pa)

    md = str(r.get("match_date", "")).strip()
    if md:
        try:
            dt = pd.to_datetime(md, errors="coerce")
            if pd.notna(dt):
                r["weekday"] = dt.strftime("%A")
                r["date_label"] = dt.strftime("%B %d, %Y")
        except Exception:
            r["weekday"] = ""
            r["date_label"] = md
    else:
        r["weekday"] = ""
        r["date_label"] = ""

    utc_dt = str(r.get("match_datetime_utc", "")).strip()
    if utc_dt:
        try:
            dt = pd.to_datetime(utc_dt, utc=True, errors="coerce")
            if pd.notna(dt):
                dt = dt.tz_convert("America/New_York")
                r["match_datetime_et"] = dt.strftime("%Y-%m-%dT%H:%M:%S%z")
                r["time_label"] = dt.strftime("%I:%M %p ET").lstrip("0")
        except Exception:
            pass
    if "time_label" not in r:
        r["time_label"] = ""

    for score_key in ("actual_home_goals", "actual_away_goals", "home_score", "away_score"):
        v = r.get(score_key)
        if v is not None:
            try:
                r[score_key] = int(float(v))
            except (ValueError, TypeError):
                pass

    if r.get("home_score") is not None and r.get("actual_home_goals") is None:
        r["actual_home_goals"] = r["home_score"]
    if r.get("away_score") is not None and r.get("actual_away_goals") is None:
        r["actual_away_goals"] = r["away_score"]
    if r.get("actual_home_goals") is not None and r.get("home_score") is None:
        r["home_score"] = r["actual_home_goals"]
    if r.get("actual_away_goals") is not None and r.get("away_score") is None:
        r["away_score"] = r["actual_away_goals"]


def _week_based_cutoff() -> str:
    """Return ISO date string for retention cutoff (30 days ago)."""
    today_local = datetime.now(ZoneInfo("America/New_York")).date()
    return (today_local - timedelta(days=30)).isoformat()


def _load_static_predictions(path: str) -> tuple[dict, set]:
    if not path or not os.path.exists(path):
        return {}, set()
    try:
        df = pd.read_csv(path)
    except Exception:
        return {}, set()
    if df.empty:
        return {}, set()

    lower_cols = {str(col).strip().lower(): col for col in df.columns}
    def find_col(*names):
        for name in names:
            col = lower_cols.get(name)
            if col is not None:
                return col
        return None

    home_col = find_col("home_team", "hometeam", "home")
    away_col = find_col("away_team", "awayteam", "away")
    if home_col is None or away_col is None:
        return {}, set()

    comp_col = find_col("competition", "league")
    result_col = find_col("predicted_result", "prediction", "result")
    ph_col = find_col("prob_home", "prob_h", "home_prob")
    pd_col = find_col("prob_draw", "prob_d", "draw_prob")
    pa_col = find_col("prob_away", "prob_a", "away_prob")
    hg_col = find_col("pred_home_goals", "home_goals")
    ag_col = find_col("pred_away_goals", "away_goals")
    hs_col = find_col("pred_home_shots", "home_shots")
    as_col = find_col("pred_away_shots", "away_shots")
    hsot_col = find_col("pred_home_sot", "home_sot")
    asot_col = find_col("pred_away_sot", "away_sot")

    lookup = {}
    teams = set()
    for _, r in df.iterrows():
        h = str(r[home_col]).strip()
        a = str(r[away_col]).strip()
        if not h or not a:
            continue
        hk = _normalize_team_key(h)
        ak = _normalize_team_key(a)
        teams.add(hk)
        teams.add(ak)
        lookup[(hk, ak)] = {
            "home_team": h,
            "away_team": a,
            "competition": str(r[comp_col]).strip() if comp_col else "",
            "predicted_result": str(r[result_col]).strip() if result_col else "",
            "prob_home": _to_float(r.get(ph_col, 0.0)),
            "prob_draw": _to_float(r.get(pd_col, 0.0)),
            "prob_away": _to_float(r.get(pa_col, 0.0)),
            "pred_home_goals": _to_float(r.get(hg_col, 0.0)),
            "pred_away_goals": _to_float(r.get(ag_col, 0.0)),
            "pred_home_shots": _to_float(r.get(hs_col, 0.0)),
            "pred_away_shots": _to_float(r.get(as_col, 0.0)),
            "pred_home_sot": _to_float(r.get(hsot_col, 0.0)),
            "pred_away_sot": _to_float(r.get(asot_col, 0.0)),
        }
    return lookup, teams


def _get_static_predictions(mode: str = "global") -> tuple[dict, set]:
    """Retrieve pre-built static prediction lookup table."""
    if mode == "mls":
        path = config.STATIC_PREDICTIONS_MLS_FILE
    elif mode == "extra":
        path = config.STATIC_PREDICTIONS_EXTRA_FILE
    else:
        path = config.STATIC_PREDICTIONS_GLOBAL_FILE
    if not path or not os.path.exists(path):
        return {}, set()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}, set()

    cached = _static_predictions_cache.get(mode)
    if cached and cached.get("path") == path and cached.get("mtime") == mtime:
        return cached["lookup"], cached["teams"]

    lookup, teams = _load_static_predictions(path)
    _static_predictions_cache[mode] = {"path": path, "mtime": mtime, "lookup": lookup, "teams": teams}
    return lookup, teams


def _build_past_game_prediction_lookup() -> dict:
    """Build a lookup mapping (home_team, away_team, date) to model prediction dict."""
    global _PAST_GAME_PREDICTION_LOOKUP_CACHE
    with _PAST_GAME_PREDICTION_LOOKUP_LOCK:
        if _PAST_GAME_PREDICTION_LOOKUP_CACHE is not None:
            return _PAST_GAME_PREDICTION_LOOKUP_CACHE

        lookup: dict = {}
        csv_sources = [
            config.GLOBAL_UPCOMING_FILE,
            config.MLS_UPCOMING_FILE,
            config.EXTRA_UPCOMING_FILE,
            config.CUP_UPCOMING_FILE,
            config.NATIONAL_UPCOMING_FILE,
            config.FRIENDLIES_UPCOMING_FILE,
        ]
        for path in csv_sources:
            if not path or not os.path.exists(path):
                continue
            try:
                df = pd.read_csv(path, dtype=str)
            except Exception:
                continue
            if df.empty or "home_team" not in df.columns or "away_team" not in df.columns:
                continue
            date_col = "match_date" if "match_date" in df.columns else None
            for _, r in df.iterrows():
                h = _normalize_team_key(str(r["home_team"]))
                a = _normalize_team_key(str(r["away_team"]))
                if not h or not a:
                    continue
                d = str(r[date_col]).strip() if date_col and pd.notna(r.get(date_col)) else ""
                key = (h, a, d)
                if key not in lookup and r.get("predicted_result"):
                    lookup[key] = {
                        "predicted_result": str(r.get("predicted_result", "")).strip(),
                        "prob_home": _to_float(r.get("prob_home")),
                        "prob_draw": _to_float(r.get("prob_draw")),
                        "prob_away": _to_float(r.get("prob_away")),
                    }
        _PAST_GAME_PREDICTION_LOOKUP_CACHE = lookup
        return lookup


def _merge_prediction_onto_past_row(row: dict, lookup: dict) -> dict:
    """Attach model prediction to a live completed game row if available."""
    h = _normalize_team_key(str(row.get("home_team", "")))
    a = _normalize_team_key(str(row.get("away_team", "")))
    d = _past_row_date_iso(row)
    pred = lookup.get((h, a, d)) or lookup.get((h, a, ""))
    if pred:
        for k, v in pred.items():
            if v is not None:
                row[k] = v
    return row


def _collect_live_past_game_rows(cutoff: str) -> list[dict]:
    """Return completed games from live score history."""
    try:
        from standings import _load_live_score_history
        history = _load_live_score_history()
    except Exception:
        return []
    completed = []
    for g in history:
        if str(g.get("status", "")).lower() == "post":
            date_iso = _past_row_date_iso(g)
            if not date_iso or date_iso < cutoff:
                continue
            hs = g.get("home_score")
            aws = g.get("away_score")
            if hs is None or aws is None:
                continue
            actual = "H" if hs > aws else ("A" if aws > hs else "D")
            row = {
                "match_date": date_iso,
                "match_date_iso": date_iso,
                "competition": str(g.get("competition", "")).strip(),
                "home_team": str(g.get("home_team", "")).strip(),
                "away_team": str(g.get("away_team", "")).strip(),
                "actual_home_goals": int(hs),
                "actual_away_goals": int(aws),
                "actual_result": actual,
                "home_score": int(hs),
                "away_score": int(aws),
                "match_datetime_utc": str(g.get("kickoff_utc", "")).strip(),
                "source": "live_score_history",
            }
            _enrich_json_past_row(row)
            completed.append(row)
    return completed


def _load_upcoming_rows(
    csv_path: str,
    mode: str = None,
    date_range: str = "upcoming",
    window_days: int = None,
    fixtures_only: bool = False,
    competition_filter: str = None,
) -> tuple[list[dict], dict, dict]:
    """Load prediction rows from CSV filtered by date range."""
    from accuracy_tracker import _compute_accuracy_stats, _compute_league_accuracy_stats

    if not csv_path or not os.path.exists(csv_path):
        empty = pd.DataFrame()
        return [], _compute_accuracy_stats(empty), _compute_league_accuracy_stats(empty)
    try:
        df = pd.read_csv(csv_path)
    except Exception:
        empty = pd.DataFrame()
        return [], _compute_accuracy_stats(empty), _compute_league_accuracy_stats(empty)
    if df.empty or "home_team" not in df.columns or "away_team" not in df.columns:
        empty = pd.DataFrame()
        return [], _compute_accuracy_stats(empty), _compute_league_accuracy_stats(empty)

    today = datetime.now(ZoneInfo("America/New_York")).date()
    rows = []
    for _, r in df.iterrows():
        comp = str(r.get("competition", "")).strip()
        if competition_filter and comp != competition_filter:
            continue
        home = _team_name_for_display(str(r.get("display_home_team", r["home_team"])).strip())
        away = _team_name_for_display(str(r.get("display_away_team", r["away_team"])).strip())
        date_str = str(r.get("match_date", "")).strip()
        date_iso = _past_row_date_iso(r.to_dict()) or date_str

        # Filter by date range
        if date_range == "completed":
            if not date_iso or date_iso > today.isoformat():
                continue
        elif date_range == "upcoming":
            if date_iso and date_iso < today.isoformat():
                continue

        entry = {
            "match_date": date_str,
            "match_date_iso": date_iso,
            "competition": comp,
            "home_team": home,
            "away_team": away,
            "predicted_result": str(r.get("predicted_result", "")).strip(),
            "prob_home": _to_float(r.get("prob_home")),
            "prob_draw": _to_float(r.get("prob_draw")),
            "prob_away": _to_float(r.get("prob_away")),
            "pred_home_goals": _to_float_or_none(r.get("pred_home_goals")),
            "pred_away_goals": _to_float_or_none(r.get("pred_away_goals")),
            "actual_home_goals": _to_int(r.get("actual_home_goals")) if pd.notna(r.get("actual_home_goals")) else None,
            "actual_away_goals": _to_int(r.get("actual_away_goals")) if pd.notna(r.get("actual_away_goals")) else None,
            "actual_result": str(r.get("actual_result", "")).strip().upper() if pd.notna(r.get("actual_result")) else None,
        }
        _enrich_json_past_row(entry)
        rows.append(entry)

    return rows, _compute_accuracy_stats(df), _compute_league_accuracy_stats(df)
