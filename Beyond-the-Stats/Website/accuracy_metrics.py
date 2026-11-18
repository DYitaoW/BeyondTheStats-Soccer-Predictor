"""Probabilistic accuracy of settled predictions (``GET /api/accuracy``).

The old tracker only counted hits (argmax == result). That hides badly
calibrated probabilities: MLS and CONCACAF Nations League picks hit 45-52%
while their probabilities were worse than a uniform 1/3 guess (log loss 1.91
and 3.62 vs 1.10). Metrics here, per competition and overall:

* ``hit_rate``   share of games where the most likely outcome happened
* ``brier``      sum over H/D/A of (p - outcome)^2  (0 best, 2 worst; 0.667 = uniform)
* ``log_loss``   -ln p(actual)  (uniform guess = ln 3 = 1.0986)
* ``rps``        ranked probability score over H < D < A (0 best; standard in football)
* ``skill``      1 - log_loss / ln 3  (> 0 = better than guessing)
* ``draw_pred`` / ``draw_actual``  mean predicted draw probability vs real draw rate
* ``calibration``  favourite probability buckets: predicted vs observed hit rate

Source: ``past_games`` (settled results), one row per fixture (deduplicated
by ESPN ``match_id``, ``sync_key`` or date + teams), using the pre-match
prediction (``prematch_prediction`` when stored, else the row's probabilities).
Snapshots are stored daily in ``prediction_accuracy_history``.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

LN3 = math.log(3.0)
EPS = 1e-4
WINDOWS = {"30d": 30, "90d": 90, "season": None}
CALIBRATION_BUCKETS = ((0.0, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 1.01))


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def prediction_probs(row: dict) -> Optional[tuple[float, float, float]]:
    """Pre-match (H, D, A) decimals for a settled row, or None."""
    sources = []
    pre = row.get("prematch_prediction")
    if isinstance(pre, dict):
        sources.append(pre)
    sources.append(row)
    for src in sources:
        vals = [_num(src.get(k)) for k in ("prob_home", "prob_draw", "prob_away")]
        if any(v is None for v in vals):
            continue
        total = sum(vals)
        if total <= 0:
            continue
        return tuple(max(0.0, v) / total for v in vals)  # type: ignore[return-value]
    return None


def _season_start(today) -> str:
    """Club-season start: 1 Jul (calendar-year leagues are covered by the window)."""
    year = today.year if today.month >= 7 else today.year - 1
    return f"{year}-07-01"


def _row_key(row: dict) -> str:
    mid = str(row.get("match_id") or "").strip()
    if mid and mid.lower() not in {"none", "nan"}:
        return f"id:{mid}"
    sk = str(row.get("sync_key") or "").strip().lower()
    if sk:
        return f"sk:{sk}"
    return "|".join(str(row.get(k) or "").strip().lower()
                    for k in ("match_date_iso", "competition", "home_team", "away_team"))


def settled_predictions(rows: Iterable[dict]) -> list[dict]:
    """Deduplicated ``{competition, date, probs, result}`` records."""
    out: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        result = str(row.get("actual_result") or "").strip().upper()
        if result not in {"H", "D", "A"}:
            continue
        if str(row.get("schedule_only") or "").strip().lower() in {"1", "true"}:
            continue
        if row.get("has_prediction") in (False, 0, "0", "false", "False"):
            continue
        probs = prediction_probs(row)
        if probs is None:
            continue
        key = _row_key(row)
        rec = {
            "competition": str(row.get("competition") or "").strip() or "Unknown",
            "date": str(row.get("match_date_iso") or row.get("match_date") or "")[:10],
            "probs": probs,
            "result": result,
        }
        # Prefer the row that carries a stored pre-match snapshot.
        if key not in out or isinstance(row.get("prematch_prediction"), dict):
            out[key] = rec
    return list(out.values())


def score(records: list[dict]) -> dict:
    """Aggregate metrics for a list of settled prediction records."""
    n = len(records)
    if not n:
        return {"n": 0}
    hits = brier = ll = rps = draw_pred = draw_act = 0.0
    for r in records:
        ph, pd_, pa = r["probs"]
        oh, od, oa = (r["result"] == "H"), (r["result"] == "D"), (r["result"] == "A")
        pick = max((ph, "H"), (pd_, "D"), (pa, "A"))[1]
        hits += pick == r["result"]
        brier += (ph - oh) ** 2 + (pd_ - od) ** 2 + (pa - oa) ** 2
        p_actual = ph if oh else (pd_ if od else pa)
        ll += -math.log(max(EPS, p_actual))
        rps += ((ph - oh) ** 2 + (ph + pd_ - oh - od) ** 2) / 2.0
        draw_pred += pd_
        draw_act += od
    log_loss = ll / n
    return {
        "n": n,
        "hit_rate": round(hits / n, 4),
        "brier": round(brier / n, 4),
        "log_loss": round(log_loss, 4),
        "rps": round(rps / n, 4),
        "skill": round(1.0 - log_loss / LN3, 4),
        "draw_pred": round(draw_pred / n, 4),
        "draw_actual": round(draw_act / n, 4),
        "baseline_log_loss": round(LN3, 4),
    }


def calibration(records: list[dict]) -> list[dict]:
    """Favourite-probability buckets: mean predicted vs observed hit rate."""
    buckets = []
    for lo, hi in CALIBRATION_BUCKETS:
        sel = []
        for r in records:
            fav = max(r["probs"])
            if lo <= fav < hi:
                outcome = "HDA"[list(r["probs"]).index(fav)]
                sel.append((fav, outcome == r["result"]))
        if not sel:
            continue
        buckets.append({
            "bucket": f"{int(lo * 100)}-{min(100, int(hi * 100))}%",
            "n": len(sel),
            "predicted": round(sum(p for p, _ in sel) / len(sel), 4),
            "observed": round(sum(1 for _, h in sel if h) / len(sel), 4),
        })
    return buckets


def _window_bounds(window: str, today=None) -> tuple[str, str]:
    today = today or datetime.now(ZoneInfo("America/New_York")).date()
    days = WINDOWS.get(window, 30)
    start = _season_start(today) if days is None else (today - timedelta(days=days)).isoformat()
    return start, today.isoformat()


def load_records(from_date: str = "", to_date: str = "", competition: str = "") -> list[dict]:
    from shared import sqlite_store

    rows = sqlite_store.load_past_games(competition_substr=competition or "", from_date=from_date, to_date=to_date)
    return settled_predictions(rows)


def build_accuracy_payload(window: str = "30d", competition: str = "", min_games: int = 1) -> dict:
    """``/api/accuracy`` body."""
    window = window if window in WINDOWS else "30d"
    start, end = _window_bounds(window)
    records = load_records(start, end, competition)
    by_comp: dict[str, list] = defaultdict(list)
    for r in records:
        by_comp[r["competition"]].append(r)
    comps = [
        {"competition": c, **score(recs)}
        for c, recs in by_comp.items() if len(recs) >= max(1, int(min_games))
    ]
    comps.sort(key=lambda x: (-x["n"], x["competition"]))
    history = []
    try:
        from shared import sqlite_store

        history = sqlite_store.load_accuracy_history(competition or "ALL", window)
    except Exception:
        history = []
    return {
        "ok": True,
        "window": window,
        "from": start,
        "to": end,
        "competition": competition or None,
        "overall": score(records),
        "competitions": comps,
        "calibration": calibration(records),
        "history": history,
        "metric_notes": {
            "hit_rate": "most likely outcome happened",
            "brier": "0 best; 0.667 = uniform guess",
            "log_loss": "lower is better; 1.0986 = uniform guess",
            "rps": "ranked probability score, lower is better",
            "skill": "1 - log_loss / ln 3; above 0 beats guessing",
        },
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def record_daily_snapshot(snapshot_date: Optional[str] = None) -> int:
    """Store today's metrics for every window (overall + per competition)."""
    from shared import sqlite_store

    today = datetime.now(ZoneInfo("America/New_York")).date()
    snapshot_date = snapshot_date or today.isoformat()
    rows = []
    for window in WINDOWS:
        start, end = _window_bounds(window, today)
        records = load_records(start, end)
        if not records:
            continue
        rows.append({"competition": "ALL", "window": window, **score(records)})
        by_comp: dict[str, list] = defaultdict(list)
        for r in records:
            by_comp[r["competition"]].append(r)
        for comp, recs in by_comp.items():
            rows.append({"competition": comp, "window": window, **score(recs)})
    return sqlite_store.save_accuracy_snapshot(snapshot_date, rows)
