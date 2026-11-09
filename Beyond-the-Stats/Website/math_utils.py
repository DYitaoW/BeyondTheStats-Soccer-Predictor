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


def _compute_goal_probabilities(pred_home_goals, pred_away_goals) -> dict:
    """Compute Poisson-based goal event probabilities from expected goals."""
    hg = max(0.01, _safe_float(pred_home_goals, 0.0))
    ag = max(0.01, _safe_float(pred_away_goals, 0.0))

    p_h0 = _poisson_pmf(0, hg)
    p_h1 = _poisson_pmf(1, hg)
    p_a0 = _poisson_pmf(0, ag)
    p_a1 = _poisson_pmf(1, ag)

    prob_home_goals_0 = round(p_h0, 6)
    prob_home_goals_1plus = round(1.0 - p_h0, 6)
    prob_home_goals_2plus = round(max(0.0, 1.0 - p_h0 - p_h1), 6)
    prob_away_goals_0 = round(p_a0, 6)
    prob_away_goals_1plus = round(1.0 - p_a0, 6)
    prob_away_goals_2plus = round(max(0.0, 1.0 - p_a0 - p_a1), 6)
    prob_both_score = round((1.0 - p_h0) * (1.0 - p_a0), 6)

    lam = hg + ag
    p_tot0 = _poisson_pmf(0, lam)
    p_tot1 = _poisson_pmf(1, lam)
    p_tot2 = _poisson_pmf(2, lam)
    p_tot3 = _poisson_pmf(3, lam)

    prob_over_1_5 = round(max(0.0, 1.0 - p_tot0 - p_tot1), 6)
    prob_over_2_5 = round(max(0.0, 1.0 - p_tot0 - p_tot1 - p_tot2), 6)
    prob_over_3_5 = round(max(0.0, 1.0 - p_tot0 - p_tot1 - p_tot2 - p_tot3), 6)

    return {
        "prob_home_goals_0": prob_home_goals_0,
        "prob_home_goals_1plus": prob_home_goals_1plus,
        "prob_home_goals_2plus": prob_home_goals_2plus,
        "prob_away_goals_0": prob_away_goals_0,
        "prob_away_goals_1plus": prob_away_goals_1plus,
        "prob_away_goals_2plus": prob_away_goals_2plus,
        "prob_both_score": prob_both_score,
        "prob_over_1_5": prob_over_1_5,
        "prob_over_2_5": prob_over_2_5,
        "prob_over_3_5": prob_over_3_5,
    }


def _compute_expected_points(prob_home, prob_draw, prob_away) -> dict:
    """Compute mathematical Expected Points (xPTS) from 1X2 probabilities (0-100 or 0-1)."""
    ph = _safe_float(prob_home, 0.0)
    pdv = _safe_float(prob_draw, 0.0)
    pa = _safe_float(prob_away, 0.0)
    if ph > 1.0 or pdv > 1.0 or pa > 1.0:
        tot = ph + pdv + pa
        if tot > 0:
            ph /= tot
            pdv /= tot
            pa /= tot
        else:
            ph, pdv, pa = 0.0, 0.0, 0.0
    xpts_h = round(3.0 * ph + 1.0 * pdv, 2)
    xpts_a = round(3.0 * pa + 1.0 * pdv, 2)
    return {
        "xpts_home": xpts_h,
        "xpts_away": xpts_a,
    }


def _compute_halftime_distributions(pred_home_goals, pred_away_goals, max_goals=4) -> dict:
    """Compute Half-Time (1st Half) expected goals, 1X2 probabilities, and scoreline distribution."""
    hg = max(0.01, _safe_float(pred_home_goals, 1.2))
    ag = max(0.01, _safe_float(pred_away_goals, 1.0))
    # In professional soccer, ~45% of expected goals occur in the first half
    hg_ht = hg * 0.45
    ag_ht = ag * 0.45

    scores = []
    p_home_win = 0.0
    p_draw = 0.0
    p_away_win = 0.0
    total = 0.0

    for h in range(max_goals + 1):
        ph = _poisson_pmf(h, hg_ht)
        for a in range(max_goals + 1):
            pa = _poisson_pmf(a, ag_ht)
            p = ph * pa
            total += p
            if h > a:
                p_home_win += p
            elif a > h:
                p_away_win += p
            else:
                p_draw += p
            scores.append({"home": h, "away": a, "prob": round(p, 6)})

    if total > 0:
        p_home_win /= total
        p_draw /= total
        p_away_win /= total

    scores.sort(key=lambda s: s["prob"], reverse=True)
    ht_correct_score = scores[:6]

    lam_ht = hg_ht + ag_ht
    p_ht_0 = _poisson_pmf(0, lam_ht)
    p_ht_1 = _poisson_pmf(1, lam_ht)
    prob_ht_over_0_5 = round(max(0.0, 1.0 - p_ht_0), 4)
    prob_ht_over_1_5 = round(max(0.0, 1.0 - p_ht_0 - p_ht_1), 4)

    return {
        "pred_home_goals_ht": round(hg_ht, 2),
        "pred_away_goals_ht": round(ag_ht, 2),
        "prob_home_ht": round(p_home_win * 100, 2),
        "prob_draw_ht": round(p_draw * 100, 2),
        "prob_away_ht": round(p_away_win * 100, 2),
        "prob_ht_over_0_5": prob_ht_over_0_5,
        "prob_ht_over_1_5": prob_ht_over_1_5,
        "ht_correct_score_dist": ht_correct_score,
        "ht_clean_sheet_home": round(math.exp(-ag_ht), 4),
        "ht_clean_sheet_away": round(math.exp(-hg_ht), 4),
    }


def _compute_htft_probabilities(pred_home_goals, pred_away_goals, prob_home=None, prob_draw=None, prob_away=None) -> dict:
    """Compute joint Half-Time / Full-Time (HT/FT) probabilities for all 9 outcomes."""
    hg = max(0.01, _safe_float(pred_home_goals, 1.2))
    ag = max(0.01, _safe_float(pred_away_goals, 1.0))
    hg_1h = hg * 0.45
    ag_1h = ag * 0.45
    hg_2h = hg * 0.55
    ag_2h = ag * 0.55

    outcomes = {
        "home_home": 0.0, "home_draw": 0.0, "home_away": 0.0,
        "draw_home": 0.0, "draw_draw": 0.0, "draw_away": 0.0,
        "away_home": 0.0, "away_draw": 0.0, "away_away": 0.0,
    }

    # First half vs Second half goal sums
    first_half_more = 0.0
    second_half_more = 0.0
    halves_equal = 0.0

    for h1 in range(5):
        ph1 = _poisson_pmf(h1, hg_1h)
        for a1 in range(5):
            pa1 = _poisson_pmf(a1, ag_1h)
            p1 = ph1 * pa1
            ht_res = "home" if h1 > a1 else ("away" if a1 > h1 else "draw")
            tot_1 = h1 + a1
            for h2 in range(5):
                ph2 = _poisson_pmf(h2, hg_2h)
                for a2 in range(5):
                    pa2 = _poisson_pmf(a2, ag_2h)
                    p2 = ph2 * pa2
                    tot_2 = h2 + a2
                    h_ft = h1 + h2
                    a_ft = a1 + a2
                    ft_res = "home" if h_ft > a_ft else ("away" if a_ft > h_ft else "draw")
                    prob_cell = p1 * p2
                    outcomes[f"{ht_res}_{ft_res}"] += prob_cell

                    if tot_1 > tot_2:
                        first_half_more += prob_cell
                    elif tot_2 > tot_1:
                        second_half_more += prob_cell
                    else:
                        halves_equal += prob_cell

    tot = sum(outcomes.values())
    if tot > 0:
        prob_htft = {k: round((v / tot) * 100, 2) for k, v in outcomes.items()}
    else:
        prob_htft = {k: 11.11 for k in outcomes}

    tot_half = first_half_more + second_half_more + halves_equal
    if tot_half > 0:
        half_distribution = {
            "first_half": round((first_half_more / tot_half) * 100, 2),
            "second_half": round((second_half_more / tot_half) * 100, 2),
            "equal": round((halves_equal / tot_half) * 100, 2),
        }
    else:
        half_distribution = {"first_half": 35.0, "second_half": 45.0, "equal": 20.0}

    best_key = max(prob_htft.items(), key=lambda x: x[1])[0]
    display_map = {
        "home_home": "Home / Home", "home_draw": "Home / Draw", "home_away": "Home / Away",
        "draw_home": "Draw / Home", "draw_draw": "Draw / Draw", "draw_away": "Draw / Away",
        "away_home": "Away / Home", "away_draw": "Away / Draw", "away_away": "Away / Away",
    }

    return {
        "prob_htft": prob_htft,
        "predicted_htft": display_map.get(best_key, best_key),
        "prob_highest_scoring_half": half_distribution,
    }


def _compute_combo_markets(pred_home_goals, pred_away_goals, prob_home=None, prob_draw=None, prob_away=None, max_goals=6) -> dict:
    """Compute popular combo markets (Win & Over 2.5, Win to Nil, BTTS & Over 2.5) via joint Poisson."""
    hg = max(0.01, _safe_float(pred_home_goals, 1.2))
    ag = max(0.01, _safe_float(pred_away_goals, 1.0))

    p_h_win_over_2_5 = 0.0
    p_a_win_over_2_5 = 0.0
    p_d_under_2_5 = 0.0
    p_h_win_clean_sheet = 0.0
    p_a_win_clean_sheet = 0.0
    p_btts_over_2_5 = 0.0
    p_btts_under_2_5 = 0.0
    total = 0.0

    for h in range(max_goals + 1):
        ph = _poisson_pmf(h, hg)
        for a in range(max_goals + 1):
            pa = _poisson_pmf(a, ag)
            p = ph * pa
            total += p
            tot_goals = h + a
            btts = (h >= 1 and a >= 1)

            if h > a and tot_goals > 2:
                p_h_win_over_2_5 += p
            if a > h and tot_goals > 2:
                p_a_win_over_2_5 += p
            if h == a and tot_goals < 3:
                p_d_under_2_5 += p
            if h > a and a == 0:
                p_h_win_clean_sheet += p
            if a > h and h == 0:
                p_a_win_clean_sheet += p
            if btts and tot_goals > 2:
                p_btts_over_2_5 += p
            if btts and tot_goals <= 2:
                p_btts_under_2_5 += p

    if total > 0:
        p_h_win_over_2_5 /= total
        p_a_win_over_2_5 /= total
        p_d_under_2_5 /= total
        p_h_win_clean_sheet /= total
        p_a_win_clean_sheet /= total
        p_btts_over_2_5 /= total
        p_btts_under_2_5 /= total

    return {
        "prob_home_win_and_over_2_5": round(p_h_win_over_2_5 * 100, 2),
        "prob_away_win_and_over_2_5": round(p_a_win_over_2_5 * 100, 2),
        "prob_draw_and_under_2_5": round(p_d_under_2_5 * 100, 2),
        "prob_home_win_clean_sheet": round(p_h_win_clean_sheet * 100, 2),
        "prob_away_win_clean_sheet": round(p_a_win_clean_sheet * 100, 2),
        "prob_btts_and_over_2_5": round(p_btts_over_2_5 * 100, 2),
        "prob_btts_and_under_2_5": round(p_btts_under_2_5 * 100, 2),
    }


def _compute_predicted_team_stats(pred_home_goals, pred_away_goals, home_attack=None, away_attack=None) -> dict:
    """Compute expected in-match team stats: shots, shots on target, corners, possession, fouls, and cards."""
    hg = max(0.01, _safe_float(pred_home_goals, 1.3))
    ag = max(0.01, _safe_float(pred_away_goals, 1.1))

    # Conversion benchmark: ~0.11 goals per shot, ~0.33 goals per SOT
    shots_h = max(5.0, round(hg / 0.115 + 2.0, 1))
    shots_a = max(4.0, round(ag / 0.115 + 1.0, 1))
    sot_h = max(2.0, round(hg / 0.33 + 0.8, 1))
    sot_a = max(1.5, round(ag / 0.33 + 0.5, 1))

    # Corner rates correlate with shots & attacking pressure (benchmark ~0.4 corners per shot)
    corners_h = max(2.5, round(shots_h * 0.38, 1))
    corners_a = max(2.0, round(shots_a * 0.38, 1))

    # Possession model based on relative goal expectations and home field bias (+3%)
    ratio_h = (hg + 0.3) / (hg + ag + 0.6)
    poss_h = round(min(78.0, max(22.0, ratio_h * 100)), 1)
    poss_a = round(100.0 - poss_h, 1)

    # Fouls & Cards benchmark: ~10.5 fouls per team, ~1.8 cards per team
    fouls_h = round(10.5 + (ag - hg) * 0.8, 1)
    fouls_a = round(11.2 + (hg - ag) * 0.8, 1)
    cards_h = round(max(0.5, 1.8 + (fouls_h - 10.0) * 0.12), 1)
    cards_a = round(max(0.5, 2.1 + (fouls_a - 10.0) * 0.12), 1)

    return {
        "pred_home_shots": shots_h,
        "pred_away_shots": shots_a,
        "pred_total_shots": round(shots_h + shots_a, 1),
        "pred_home_sot": sot_h,
        "pred_away_sot": sot_a,
        "pred_total_sot": round(sot_h + sot_a, 1),
        "pred_home_corners": corners_h,
        "pred_away_corners": corners_a,
        "pred_total_corners": round(corners_h + corners_a, 1),
        "pred_home_possession": poss_h,
        "pred_away_possession": poss_a,
        "pred_home_fouls": max(4.0, fouls_h),
        "pred_away_fouls": max(4.0, fouls_a),
        "pred_home_cards": cards_h,
        "pred_away_cards": cards_a,
    }
