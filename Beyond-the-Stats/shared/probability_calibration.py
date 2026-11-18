"""Final 1X2 probability post-processing shared by every prediction pipeline.

Two steps, applied to each pipeline's output frame just before it is written:

1. **Floor** (``finalize_probabilities``): no outcome below ``PROB_FLOOR``
   (2%). Tree ensembles and the old post-model adjustments produced exact 0%
   (San Diego 97.8 / 2.2 / **0.0** v Sporting KC, who won), which is never
   true in football and makes log loss infinite.
2. **Draw calibration** (``calibrate_prediction_frame``): per competition, the
   mean predicted draw probability of this run's fixtures is scaled to the
   competition's historical draw rate (last ``DRAW_RATE_SEASONS`` seasons of
   the training data, shrunk towards the pooled rate). Each game keeps its
   relative draw likelihood; home/away keep their ratio. The scale is clamped
   to ``[DRAW_SCALE_MIN, DRAW_SCALE_MAX]``. Historical draw rates differ a lot
   (Serie B ~32%, Argentina ~31%, WSL ~18%), which a single global draw
   reduction could not capture.

The old per-fixture "randomizer" noise (up to +-16 points between home and
away) was removed from the model chain; this module never adds noise.
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
from functools import lru_cache
from typing import Iterable, Optional

PROB_FLOOR = 0.02
DRAW_RATE_SEASONS = 5
DRAW_RATE_PRIOR = 0.255          # pooled top-flight draw rate
DRAW_RATE_PRIOR_GAMES = 300      # shrinkage weight of the prior (in games)
DRAW_SCALE_MIN = 0.70
DRAW_SCALE_MAX = 1.30
DRAW_MAX = 0.45
MIN_ROWS_FOR_CALIBRATION = 6


def finalize_probabilities(probabilities: dict, floor: float = PROB_FLOOR) -> dict:
    """Normalise ``{H, D, A}`` and lift every outcome to at least ``floor``."""
    vals = []
    for k in ("H", "D", "A"):
        try:
            v = float(probabilities.get(k, 0.0) or 0.0)
        except (TypeError, ValueError):
            v = 0.0
        vals.append(v if math.isfinite(v) and v > 0 else 0.0)
    total = sum(vals)
    if total <= 0:
        return {"H": 1 / 3, "D": 1 / 3, "A": 1 / 3}
    vals = [v / total for v in vals]
    low = [v < floor for v in vals]
    if any(low):
        free = 1.0 - floor * sum(low)
        rest = sum(v for v, lo in zip(vals, low) if not lo)
        vals = [floor if lo else (v / rest * free if rest > 0 else free / (3 - sum(low)))
                for v, lo in zip(vals, low)]
    return {"H": vals[0], "D": vals[1], "A": vals[2]}


def rescale_draw(probabilities: dict, scale: float, max_draw: float = DRAW_MAX) -> dict:
    """Multiply the draw probability by ``scale``; home/away keep their ratio."""
    p = finalize_probabilities(probabilities, floor=0.0)
    d = min(max_draw, max(0.0, p["D"] * float(scale)))
    ha = p["H"] + p["A"]
    if ha <= 0:
        return {"H": (1 - d) / 2, "D": d, "A": (1 - d) / 2}
    return {"H": p["H"] / ha * (1 - d), "D": d, "A": p["A"] / ha * (1 - d)}


def _season_order(path: str) -> int:
    nums = re.findall(r"(\d{4})", os.path.basename(path))
    return int(nums[-1]) if nums else 0


def _processed_roots() -> list[str]:
    try:
        from shared import paths  # type: ignore
    except Exception:  # pragma: no cover - imported from pipeline dirs
        import paths  # type: ignore
    roots = []
    for name in ("PROCESSED_DIR", "PROCESSED_DATA_DIR"):
        if hasattr(paths, name):
            roots.append(str(getattr(paths, name)))
    sp = str(getattr(paths, "SP_DIR", ""))
    if sp:
        roots += [os.path.join(sp, "Data", "Processed_Data")]
        roots += glob.glob(os.path.join(sp, "pipelines", "*", "Data", "Processed_Data"))
    return list(dict.fromkeys(r for r in roots if os.path.isdir(r)))


# Processed-data folder -> app competition name, where they differ.
_FOLDER_ALIASES = {
    "England/WSL": "England/Women's Super League",
}


@lru_cache(maxsize=1)
def historical_draw_rates() -> dict:
    """``{competition: draw rate}`` from the last seasons of the training data.

    Rates are shrunk towards ``DRAW_RATE_PRIOR`` with weight
    ``DRAW_RATE_PRIOR_GAMES`` so small samples stay sensible.
    """
    import pandas as pd

    rates: dict = {}
    for root in _processed_roots():
        for league_dir in glob.glob(os.path.join(root, "*", "*")):
            if not os.path.isdir(league_dir):
                continue
            comp = os.path.relpath(league_dir, root).replace(os.sep, "/")
            files = sorted(glob.glob(os.path.join(league_dir, "*.csv")), key=_season_order)
            draws = games = 0
            for f in files[-DRAW_RATE_SEASONS:]:
                try:
                    ftr = pd.read_csv(f, usecols=["FTR"])["FTR"].astype(str).str.strip().str.upper()
                except Exception:
                    continue
                ftr = ftr[ftr.isin(["H", "D", "A"])]
                draws += int((ftr == "D").sum())
                games += int(len(ftr))
            if games < 30:
                continue
            rate = (draws + DRAW_RATE_PRIOR * DRAW_RATE_PRIOR_GAMES) / (games + DRAW_RATE_PRIOR_GAMES)
            rates[_FOLDER_ALIASES.get(comp, comp)] = round(rate, 4)
    # National-team competitions: one raw CSV with a competition column.
    for path in _national_raw_files():
        try:
            nat = pd.read_csv(path, usecols=["competition", "FTR"])
        except Exception:
            continue
        nat = nat[nat["FTR"].astype(str).str.upper().isin(["H", "D", "A"])]
        for comp, grp in nat.groupby(nat["competition"].astype(str).str.strip()):
            games = int(len(grp))
            if games < 30 or comp in rates:
                continue
            draws = int((grp["FTR"].astype(str).str.upper() == "D").sum())
            rate = (draws + DRAW_RATE_PRIOR * DRAW_RATE_PRIOR_GAMES) / (games + DRAW_RATE_PRIOR_GAMES)
            rates[comp] = round(rate, 4)
    return rates


def _national_raw_files() -> list[str]:
    try:
        from shared import paths  # type: ignore
    except Exception:  # pragma: no cover
        import paths  # type: ignore
    sp = str(getattr(paths, "SP_DIR", ""))
    path = os.path.join(sp, "Data", "National_Team_Data", "national_team_recent_matches_raw.csv")
    return [path] if os.path.exists(path) else []


def draw_rate_for(competition: str, rates: Optional[dict] = None) -> float:
    """Historical draw rate for ``competition`` (pooled prior when unknown)."""
    rates = historical_draw_rates() if rates is None else rates
    comp = str(competition or "").strip()
    if comp in rates:
        return float(rates[comp])
    low = comp.lower()
    for key, val in rates.items():
        if key.lower() == low:
            return float(val)
    return DRAW_RATE_PRIOR


def _num(value) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def calibrate_prediction_frame(
    df,
    *,
    competition_col: str = "competition",
    rates: Optional[dict] = None,
    floor: float = PROB_FLOOR,
    skip_competitions: Iterable[str] = (),
    add_scale_column: bool = False,
):
    """Draw-calibrate and floor a prediction DataFrame in place (and return it).

    Expects decimal ``prob_home/prob_draw/prob_away`` columns. Rows without
    probabilities (schedule-only) are left untouched. Recomputes
    ``predicted_result`` and, when present, ``odds_*`` and ``prob_*_text``.
    With ``add_scale_column`` adds ``draw_calibration_scale`` per row.
    """
    if df is None or len(df) == 0:
        return df
    cols = ("prob_home", "prob_draw", "prob_away")
    if not all(c in df.columns for c in cols):
        return df
    rates = historical_draw_rates() if rates is None else rates
    skip = {str(c).strip().lower() for c in skip_competitions}
    # Work on a positional index: frames built with ``pd.concat`` can repeat
    # index labels, and ``df.at[label, ...]`` would then write every row that
    # shares the label (rows were left unfloored / overwritten).
    original_index = df.index
    df = df.reset_index(drop=True)

    probs: dict = {}
    for idx, row in df.iterrows():
        vals = [_num(row.get(c)) for c in cols]
        if any(v is None for v in vals) or sum(vals) <= 0:
            continue
        scale = 100.0 if sum(vals) > 1.5 else 1.0
        probs[idx] = {"H": vals[0] / scale, "D": vals[1] / scale, "A": vals[2] / scale}

    comps = df[competition_col].astype(str).str.strip() if competition_col in df.columns else None
    scales: dict = {}
    if comps is not None:
        by_comp: dict = {}
        for idx in probs:
            by_comp.setdefault(comps.loc[idx], []).append(idx)
        for comp, idxs in by_comp.items():
            if comp.lower() in skip or len(idxs) < MIN_ROWS_FOR_CALIBRATION:
                continue
            mean_d = sum(finalize_probabilities(probs[i], 0.0)["D"] for i in idxs) / len(idxs)
            if mean_d <= 0:
                continue
            k = min(DRAW_SCALE_MAX, max(DRAW_SCALE_MIN, draw_rate_for(comp, rates) / mean_d))
            for i in idxs:
                scales[i] = k

    has_odds = all(c in df.columns for c in ("odds_home", "odds_draw", "odds_away"))
    text_cols = [c for c in ("prob_home_text", "prob_draw_text", "prob_away_text") if c in df.columns]
    if add_scale_column and "draw_calibration_scale" not in df.columns:
        df["draw_calibration_scale"] = 1.0
    for idx, p in probs.items():
        k = scales.get(idx, 1.0)
        out = rescale_draw(p, k) if k != 1.0 else p
        out = finalize_probabilities(out, floor)
        df.at[idx, "prob_home"] = round(out["H"], 6)
        df.at[idx, "prob_draw"] = round(out["D"], 6)
        df.at[idx, "prob_away"] = round(out["A"], 6)
        if add_scale_column:
            df.at[idx, "draw_calibration_scale"] = round(k, 4)
        if "predicted_result" in df.columns:
            df.at[idx, "predicted_result"] = max(out, key=out.get)
        if has_odds:
            df.at[idx, "odds_home"] = round(1.0 / out["H"], 2)
            df.at[idx, "odds_draw"] = round(1.0 / out["D"], 2)
            df.at[idx, "odds_away"] = round(1.0 / out["A"], 2)
        for col, key in zip(("prob_home_text", "prob_draw_text", "prob_away_text"), ("H", "D", "A")):
            if col in text_cols:
                df.at[idx, col] = f"{round(out[key] * 100, 1)}%"
    df.index = original_index
    return df


def describe_rates(rates: Optional[dict] = None) -> str:
    """Human-readable dump (pipeline logs)."""
    rates = historical_draw_rates() if rates is None else rates
    return json.dumps(dict(sorted(rates.items())), ensure_ascii=False)
