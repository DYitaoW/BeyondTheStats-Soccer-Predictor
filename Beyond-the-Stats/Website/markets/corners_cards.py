"""
Corners and Cards expectancy prediction market model.
Calculates expected team and total corners and bookings/cards using team shot volumes,
possession/tempo metrics, and bivariate Poisson distribution for spread and over/under lines.
"""
from __future__ import annotations

import math
from typing import Any

# Baseline parameters across top leagues:
# ~10.0 total corners per match (5.6 home, 4.4 away)
# ~4.0 total yellow cards per match (1.8 home, 2.2 away)
BASE_HOME_CORNERS = 5.5
BASE_AWAY_CORNERS = 4.3
BASE_HOME_CARDS = 1.85
BASE_AWAY_CARDS = 2.15


def _poisson_pmf(k: int, lam: float) -> float:
    """Poisson probability mass function P(X=k) for mean λ."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def _poisson_over(threshold: float, lam: float, max_k: int = 25) -> float:
    """Calculate P(X > threshold) for Poisson with parameter lam."""
    int_thresh = int(math.floor(threshold))
    prob_under_or_equal = sum(_poisson_pmf(k, lam) for k in range(int_thresh + 1))
    return max(0.0, min(1.0, 1.0 - prob_under_or_equal))


def compute_corners_and_cards_markets(
    home_shots: float | int | None = None,
    away_shots: float | int | None = None,
    home_attack_rating: float | int | None = None,
    away_attack_rating: float | int | None = None,
    home_defence_rating: float | int | None = None,
    away_defence_rating: float | int | None = None,
    competition: str | None = None,
) -> dict[str, Any]:
    """Compute expected corners and cards for home, away, and combined totals,
    with standard betting market lines (Over/Under corners, Over/Under cards).
    
    Parameters:
        home_shots: Predicted home shots.
        away_shots: Predicted away shots.
        home_attack_rating: Team attack rating relative to 1.0.
        away_attack_rating: Team attack rating relative to 1.0.
        home_defence_rating: Team defence rating relative to 1.0.
        away_defence_rating: Team defence rating relative to 1.0.
        competition: League name for baseline adjustments.
    """
    # 1. Corners Estimation based on shot volume and relative strength
    # Typically ~1 corner for every 2.4 - 2.8 shots
    hs = float(home_shots) if home_shots is not None and float(home_shots) > 0 else 13.5
    aws = float(away_shots) if away_shots is not None and float(away_shots) > 0 else 10.5

    h_att = float(home_attack_rating) if home_attack_rating is not None and float(home_attack_rating) > 0 else 1.0
    a_att = float(away_attack_rating) if away_attack_rating is not None and float(away_attack_rating) > 0 else 1.0
    h_def = float(home_defence_rating) if home_defence_rating is not None and float(home_defence_rating) > 0 else 1.0
    a_def = float(away_defence_rating) if away_defence_rating is not None and float(away_defence_rating) > 0 else 1.0

    exp_home_corners = BASE_HOME_CORNERS * (hs / 13.5) * 0.7 + (BASE_HOME_CORNERS * h_att / a_def) * 0.3
    exp_away_corners = BASE_AWAY_CORNERS * (aws / 10.5) * 0.7 + (BASE_AWAY_CORNERS * a_att / h_def) * 0.3

    exp_home_corners = round(max(2.0, min(12.0, exp_home_corners)), 2)
    exp_away_corners = round(max(1.5, min(10.0, exp_away_corners)), 2)
    total_corners_lam = exp_home_corners + exp_away_corners

    corners_markets = {
        "expected_home_corners": exp_home_corners,
        "expected_away_corners": exp_away_corners,
        "expected_total_corners": round(total_corners_lam, 2),
        "over_8_5": round(_poisson_over(8.5, total_corners_lam) * 100, 2),
        "over_9_5": round(_poisson_over(9.5, total_corners_lam) * 100, 2),
        "over_10_5": round(_poisson_over(10.5, total_corners_lam) * 100, 2),
        "over_11_5": round(_poisson_over(11.5, total_corners_lam) * 100, 2),
    }

    # 2. Cards / Bookings Estimation
    # Underdogs and teams under defensive pressure tend to commit more fouls and receive more cards
    exp_home_cards = BASE_HOME_CARDS * (aws / 11.0) * 0.5 + BASE_HOME_CARDS * 0.5
    exp_away_cards = BASE_AWAY_CARDS * (hs / 13.0) * 0.5 + BASE_AWAY_CARDS * 0.5

    exp_home_cards = round(max(0.5, min(5.0, exp_home_cards)), 2)
    exp_away_cards = round(max(0.8, min(6.0, exp_away_cards)), 2)
    total_cards_lam = exp_home_cards + exp_away_cards

    cards_markets = {
        "expected_home_cards": exp_home_cards,
        "expected_away_cards": exp_away_cards,
        "expected_total_cards": round(total_cards_lam, 2),
        "over_2_5": round(_poisson_over(2.5, total_cards_lam) * 100, 2),
        "over_3_5": round(_poisson_over(3.5, total_cards_lam) * 100, 2),
        "over_4_5": round(_poisson_over(4.5, total_cards_lam) * 100, 2),
        "over_5_5": round(_poisson_over(5.5, total_cards_lam) * 100, 2),
    }

    return {
        "corners": corners_markets,
        "cards": cards_markets,
    }
