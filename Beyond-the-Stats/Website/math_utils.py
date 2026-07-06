"""Mathematical utilities for predictions."""
import math


def _poisson_pmf(k, lam):
    """Poisson probability mass function P(X=k) for mean λ."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def _safe_float(v, default=0.0):
    """Convert to float, returning *default* on None / NaN / error."""
    if v is None:
        return default
    try:
        f = float(v)
        if math.isnan(f):
            return default
        return f
    except (ValueError, TypeError, OverflowError):
        return default


def _compute_correct_score_dist(pred_home_goals, pred_away_goals, max_goals=5):
    """Full correct-score distribution via independent Poisson.

    Returns top-20 scorelines sorted by probability descending.
    """
    hg = max(0.01, _safe_float(pred_home_goals, 0.0))
    ag = max(0.01, _safe_float(pred_away_goals, 0.0))
    scores = []
    total = 0.0
    for h in range(max_goals + 1):
        ph = _poisson_pmf(h, hg)
        for a in range(max_goals + 1):
            p = ph * _poisson_pmf(a, ag)
            if p > 0.001:
                scores.append({"home": h, "away": a, "prob": round(p, 6)})
                total += p
    if total > 0:
        for s in scores:
            s["prob"] = round(s["prob"] / total, 6)
    scores.sort(key=lambda x: x["prob"], reverse=True)
    return scores[:20]


def _compute_double_chance(prob_home, prob_draw, prob_away):
    """Double-chance probabilities (1X / 12 / X2) from 0-1 probs."""
    ph = _safe_float(prob_home, 0.0)
    pdv = _safe_float(prob_draw, 0.0)
    pa = _safe_float(prob_away, 0.0)
    t = ph + pdv + pa
    if t <= 0:
        return {}
    return {
        "home_or_draw": round((ph + pdv) / t * 100, 2),
        "home_or_away": round((ph + pa) / t * 100, 2),
        "draw_or_away": round((pdv + pa) / t * 100, 2),
    }


def _compute_total_goals_dist(pred_home_goals, pred_away_goals, max_total=6):
    """Exact total goals distribution: probability per total goal count 0..max_total."""
    hg = max(0.01, _safe_float(pred_home_goals, 0.0))
    ag = max(0.01, _safe_float(pred_away_goals, 0.0))
    lam = hg + ag
    dist = {}
    total_p = 0.0
    for k in range(max_total):
        p = _poisson_pmf(k, lam)
        dist[str(k)] = round(p, 6)
        total_p += p
    dist["6plus"] = round(max(0.0, 1.0 - total_p), 6)
    return dist


def _compute_first_to_score(pred_home_goals, pred_away_goals):
    """First team to score probabilities."""
    hg = max(0.01, _safe_float(pred_home_goals, 0.0))
    ag = max(0.01, _safe_float(pred_away_goals, 0.0))
    lam = hg + ag
    p_no_goal = math.exp(-lam)
    if lam <= 0:
        return {"home": 0.0, "away": 0.0, "none": 1.0}
    return {
        "home": round((1.0 - p_no_goal) * hg / lam, 6),
        "away": round((1.0 - p_no_goal) * ag / lam, 6),
        "none": round(p_no_goal, 6),
    }


def _compute_clean_sheet(pred_home_goals, pred_away_goals):
    """Clean sheet probabilities."""
    hg = max(0.01, _safe_float(pred_home_goals, 0.0))
    ag = max(0.01, _safe_float(pred_away_goals, 0.0))
    return {
        "home": round(math.exp(-ag), 6),
        "away": round(math.exp(-hg), 6),
    }


def _compute_asian_handicap(pred_home_goals, pred_away_goals, prob_home=None, prob_draw=None, prob_away=None, max_goals=5):
    """Compute key Asian handicap lines."""
    lines = {}

    # Simple lines from match probabilities
    ph = _safe_float(prob_home, None)
    pdv = _safe_float(prob_draw, None)
    pa = _safe_float(prob_away, None)
    if ph is not None and pdv is not None and pa is not None:
        total = ph + pdv + pa
        if total > 0:
            lines["0"] = {"home": round((ph + pdv * 0.5) / total, 6), "away": round((pa + pdv * 0.5) / total, 6)}
            lines["-0.5"] = {"home": round(ph / total, 6), "away": round((pdv + pa) / total, 6)}
            lines["+0.5"] = {"home": round((ph + pdv) / total, 6), "away": round(pa / total, 6)}

    # Margin-dependent lines from Poisson distribution
    hg = max(0.01, _safe_float(pred_home_goals, 0.0))
    ag = max(0.01, _safe_float(pred_away_goals, 0.0))
    dist = {}
    total_p = 0.0
    for h in range(max_goals + 1):
        php = _poisson_pmf(h, hg)
        for a in range(max_goals + 1):
            p = php * _poisson_pmf(a, ag)
            gd = h - a
            dist[gd] = dist.get(gd, 0.0) + p
            total_p += p
    if total_p > 0:
        dist = {k: v / total_p for k, v in dist.items()}

    def _gd_prob(cmp):
        return sum(p for gd, p in dist.items() if cmp(gd))

    for line in [-2.0, -1.5, -1.25, -1.0, -0.75, -0.5, -0.25, 0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0]:
        if str(line) in lines:
            continue
        if line <= 0:
            need = abs(line)
            if need == int(need):
                p_h = _gd_prob(lambda g, n=need: g > n) + _gd_prob(lambda g, n=need: g == n) * 0.5
                p_a = _gd_prob(lambda g, n=need: g < n) + _gd_prob(lambda g, n=need: g == n) * 0.5
            else:
                p_h = _gd_prob(lambda g, n=need: g > n)
                p_a = _gd_prob(lambda g, n=need: g < n)
        else:
            if line == int(line):
                p_h = _gd_prob(lambda g, n=line: g > -n) + _gd_prob(lambda g, n=line: g == -n) * 0.5
                p_a = _gd_prob(lambda g, n=line: g < -n) + _gd_prob(lambda g, n=line: g == -n) * 0.5
            else:
                p_h = _gd_prob(lambda g, n=line: g > -n)
                p_a = _gd_prob(lambda g, n=line: g < -n)
        lines[str(line)] = {"home": round(p_h, 6), "away": round(p_a, 6)}
    return lines
