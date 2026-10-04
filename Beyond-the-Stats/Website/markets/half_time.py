"""
Half-Time (HT) and Half-Time / Full-Time (HT/FT) prediction market model.
Computes 1st half goal expectations, HT result probabilities (1, X, 2),
and the 9-way HT/FT joint outcome distribution.
"""
from __future__ import annotations

import math
from typing import Any

# In professional association football, ~45% of total match goals occur in the first half
FIRST_HALF_GOAL_RATIO = 0.45


def _poisson_pmf(k: int, lam: float) -> float:
    """Poisson probability mass function P(X=k) for mean λ."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def compute_half_time_markets(
    pred_home_goals: float | int | None,
    pred_away_goals: float | int | None,
    prob_home_ft: float | None = None,
    prob_draw_ft: float | None = None,
    prob_away_ft: float | None = None,
    max_ht_goals: int = 4,
) -> dict[str, Any]:
    """Compute Half-Time outcome probabilities and 9-way HT/FT matrix.
    
    Parameters:
        pred_home_goals: Full-time expected home goals (e.g. 1.65).
        pred_away_goals: Full-time expected away goals (e.g. 1.10).
        prob_home_ft: Full-time home win probability (0-1 or 0-100).
        prob_draw_ft: Full-time draw probability (0-1 or 0-100).
        prob_away_ft: Full-time away win probability (0-1 or 0-100).
        max_ht_goals: Goal cutoff for bivariate Poisson grid.
    """
    try:
        hg_ft = max(0.05, float(pred_home_goals or 1.35))
    except (ValueError, TypeError):
        hg_ft = 1.35
    try:
        ag_ft = max(0.05, float(pred_away_goals or 1.10))
    except (ValueError, TypeError):
        ag_ft = 1.10

    # First half expected goals
    hg_ht = hg_ft * FIRST_HALF_GOAL_RATIO
    ag_ht = ag_ft * FIRST_HALF_GOAL_RATIO

    # Calculate HT bivariate distribution
    ht_prob_h = 0.0
    ht_prob_d = 0.0
    ht_prob_a = 0.0
    ht_scores = []
    total_ht_p = 0.0

    for h in range(max_ht_goals + 1):
        ph = _poisson_pmf(h, hg_ht)
        for a in range(max_ht_goals + 1):
            pa = _poisson_pmf(a, ag_ht)
            p = ph * pa
            total_ht_p += p
            if h > a:
                ht_prob_h += p
            elif h == a:
                ht_prob_d += p
            else:
                ht_prob_a += p

            if p > 0.005:
                ht_scores.append({"home": h, "away": a, "prob": round(p, 4)})

    if total_ht_p > 0:
        ht_prob_h /= total_ht_p
        ht_prob_d /= total_ht_p
        ht_prob_a /= total_ht_a = total_ht_p
        for s in ht_scores:
            s["prob"] = round(s["prob"] / total_ht_p, 4)

    ht_scores.sort(key=lambda x: x["prob"], reverse=True)

    # Calculate 2nd half expected goals
    hg_sh = hg_ft * (1.0 - FIRST_HALF_GOAL_RATIO)
    ag_sh = ag_ft * (1.0 - FIRST_HALF_GOAL_RATIO)

    sh_prob_h = 0.0
    sh_prob_d = 0.0
    sh_prob_a = 0.0
    total_sh_p = 0.0

    for h in range(max_ht_goals + 1):
        ph = _poisson_pmf(h, hg_sh)
        for a in range(max_ht_goals + 1):
            p = ph * pa
            total_sh_p += p
            if h > a:
                sh_prob_h += p
            elif h == a:
                sh_prob_d += p
            else:
                sh_prob_a += p

    if total_sh_p > 0:
        sh_prob_h /= total_sh_p
        sh_prob_d /= total_sh_p
        sh_prob_a /= total_sh_p

    # Compute joint HT/FT probabilities
    # 9 outcomes: H/H, H/D, H/A, D/H, D/D, D/A, A/H, A/D, A/A
    ht_ft_distribution = {
        "home_home": round(ht_prob_h * (sh_prob_h * 0.70 + sh_prob_d * 0.30), 4),
        "home_draw": round(ht_prob_h * (sh_prob_a * 0.65), 4),
        "home_away": round(ht_prob_h * (sh_prob_a * 0.35), 4),
        "draw_home": round(ht_prob_d * sh_prob_h, 4),
        "draw_draw": round(ht_prob_d * sh_prob_d, 4),
        "draw_away": round(ht_prob_d * sh_prob_a, 4),
        "away_home": round(ht_prob_a * (sh_prob_h * 0.35), 4),
        "away_draw": round(ht_prob_a * (sh_prob_h * 0.65), 4),
        "away_away": round(ht_prob_a * (sh_prob_a * 0.70 + sh_prob_d * 0.30), 4),
    }

    # Normalize HT/FT simplex
    total_joint = sum(ht_ft_distribution.values())
    if total_joint > 0:
        ht_ft_distribution = {
            k: round(v / total_joint, 4) for k, v in ht_ft_distribution.items()
        }

    # Over / Under 0.5 and 1.5 in the first half
    ht_over_0_5 = round(1.0 - (_poisson_pmf(0, hg_ht) * _poisson_pmf(0, ag_ht)), 4)
    ht_over_1_5 = round(
        1.0 - (
            _poisson_pmf(0, hg_ht) * _poisson_pmf(0, ag_ht) +
            _poisson_pmf(1, hg_ht) * _poisson_pmf(0, ag_ht) +
            _poisson_pmf(0, hg_ht) * _poisson_pmf(1, ag_ht)
        ),
        4
    )

    return {
        "ht_expected_home_goals": round(hg_ht, 2),
        "ht_expected_away_goals": round(ag_ht, 2),
        "ht_prob_home": round(ht_prob_h * 100, 2),
        "ht_prob_draw": round(ht_prob_d * 100, 2),
        "ht_prob_away": round(ht_prob_a * 100, 2),
        "ht_over_0_5": round(ht_over_0_5 * 100, 2),
        "ht_over_1_5": round(ht_over_1_5 * 100, 2),
        "ht_correct_scores": ht_scores[:8],
        "ht_ft_matrix": ht_ft_distribution,
    }
