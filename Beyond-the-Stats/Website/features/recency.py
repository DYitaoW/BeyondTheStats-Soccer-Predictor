"""
Exponential recency weighting module for team form and performance metrics.
Applies time-decay factor w(t) = exp(-lambda * delta_days) to historical games.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Sequence
import pandas as pd


# Default half-life in days (e.g., a match 45 days ago has half the weight of today's match)
DEFAULT_HALF_LIFE_DAYS = 45.0
DEFAULT_LAMBDA = math.log(2.0) / DEFAULT_HALF_LIFE_DAYS


def calculate_recency_weight(
    match_date: datetime | str | pd.Timestamp,
    reference_date: datetime | str | pd.Timestamp | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """Calculate exponential recency weight exp(-lambda * delta_days).
    
    Parameters:
        match_date: The date of the historical match.
        reference_date: The date from which recency is evaluated (defaults to now).
        half_life_days: Number of days over which weight halves.
    """
    if reference_date is None:
        ref_dt = pd.Timestamp.now(tz="UTC")
    else:
        ref_dt = pd.to_datetime(reference_date, utc=True)

    m_dt = pd.to_datetime(match_date, utc=True)
    delta_days = max(0.0, (ref_dt - m_dt).total_seconds() / 86400.0)

    lam = math.log(2.0) / max(1.0, half_life_days)
    weight = math.exp(-lam * delta_days)
    return round(weight, 6)


def compute_weighted_team_form(
    matches: Sequence[dict[str, Any]],
    team_name: str,
    reference_date: datetime | str | pd.Timestamp | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> dict[str, float]:
    """Compute exponentially weighted form metrics for a team from a list of matches.
    
    Each match dict should ideally contain:
        - 'date' / 'match_date'
        - 'home_team', 'away_team'
        - 'home_score', 'away_score' (or 'actual_home_goals', 'actual_away_goals')
        - 'result' / 'actual_result' ('H', 'D', 'A')
        
    Returns:
        weighted_points_per_game: Float between 0.0 and 3.0
        weighted_goals_scored: Weighted average goals scored per game
        weighted_goals_conceded: Weighted average goals conceded per game
        effective_sample_size: Sum of weights (total effective games)
    """
    if not matches:
        return {
            "weighted_points_per_game": 1.35,  # league baseline neutral
            "weighted_goals_scored": 1.25,
            "weighted_goals_conceded": 1.25,
            "effective_sample_size": 0.0,
        }

    total_weight = 0.0
    weighted_pts = 0.0
    weighted_gf = 0.0
    weighted_ga = 0.0

    for m in matches:
        dt_val = m.get("date") or m.get("match_date") or m.get("match_date_iso")
        if not dt_val:
            continue
        w = calculate_recency_weight(dt_val, reference_date=reference_date, half_life_days=half_life_days)

        is_home = m.get("is_home")
        home = str(m.get("home_team", "")).strip().lower()
        away = str(m.get("away_team", "")).strip().lower()
        t_lower = team_name.strip().lower()

        if is_home is None:
            if home and t_lower in home:
                is_home = True
            elif away and t_lower in away:
                is_home = False
            else:
                is_home = True

        hg = m.get("home_score")
        if hg is None:
            hg = m.get("actual_home_goals", 0)
        ag = m.get("away_score")
        if ag is None:
            ag = m.get("actual_away_goals", 0)

        try:
            hg = float(hg)
            ag = float(ag)
        except (ValueError, TypeError):
            continue

        gf = hg if is_home else ag
        ga = ag if is_home else hg

        # Determine points
        res = m.get("result") or m.get("actual_result")
        if res:
            res = str(res).upper()
            if (is_home and res == "H") or (not is_home and res == "A"):
                pts = 3.0
            elif res == "D":
                pts = 1.0
            else:
                pts = 0.0
        else:
            if gf > ga:
                pts = 3.0
            elif gf == ga:
                pts = 1.0
            else:
                pts = 0.0

        weighted_pts += pts * w
        weighted_gf += gf * w
        weighted_ga += ga * w
        total_weight += w

    if total_weight <= 0.001:
        return {
            "weighted_points_per_game": 1.35,
            "weighted_goals_scored": 1.25,
            "weighted_goals_conceded": 1.25,
            "effective_sample_size": 0.0,
        }

    return {
        "weighted_points_per_game": round(weighted_pts / total_weight, 3),
        "weighted_goals_scored": round(weighted_gf / total_weight, 3),
        "weighted_goals_conceded": round(weighted_ga / total_weight, 3),
        "effective_sample_size": round(total_weight, 2),
    }
