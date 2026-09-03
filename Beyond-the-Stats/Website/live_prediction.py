"""In-game live match probability updates."""
import math
import os
import re

import pandas as pd

import config
from espn_parser import (
    _is_halftime_break,
    _key_event_match_minute,
    _parse_elapsed_minutes,
)
from math_utils import _safe_float
from predictions import _to_float_or_none
from team_utils import _normalize_team_key, _team_name_for_db, _to_float

def _poisson_match_probs(lambda_h, lambda_a):
    """Compute P(H), P(D), P(A) from Poisson final-score distribution."""
    max_g = 10
    home_pmf = [math.exp(-lambda_h) * (lambda_h ** h) / math.factorial(h) for h in range(max_g + 1)]
    away_pmf = [math.exp(-lambda_a) * (lambda_a ** a) / math.factorial(a) for a in range(max_g + 1)]
    p_h = p_d = p_a = 0.0
    for h in range(max_g + 1):
        for a in range(max_g + 1):
            prob = home_pmf[h] * away_pmf[a]
            if h > a:
                p_h += prob
            elif h == a:
                p_d += prob
            else:
                p_a += prob
    total = p_h + p_d + p_a
    if total > 0:
        return {"prob_home": round(p_h / total, 4), "prob_draw": round(p_d / total, 4), "prob_away": round(p_a / total, 4)}
    return {"prob_home": 0.34, "prob_draw": 0.33, "prob_away": 0.33}

def _normalize_team_for_live(name):
    """Normalize team name for matching between ESPN API and prediction CSV."""
    n = str(name or "").strip().lower()
    n = re.sub(r"\s+", " ", n)
    n = n.replace("&", "and")
    n = re.sub(r"[^a-z0-9 ]", "", n)
    return n.strip()

def _upcoming_csv_paths_for_live():
    """Fixture CSV paths used to seed live prematch lookups."""
    seen = set()
    paths = []
    for csv_path in config.UPCOMING_CSV_FILES.values():
        if csv_path and csv_path not in seen:
            seen.add(csv_path)
            paths.append(csv_path)
    for csv_path in (
        config.ALL_UPCOMING_FILE,
        config.FOUR_WEEK_WINDOW_FILE,
        os.path.join(config.PROJECT_DIR, "Output", "Europe", "Upcoming", "europe_upcoming.csv"),
        os.path.join(config.PROJECT_DIR, "Output", "National", "Upcoming", "national_upcoming.csv"),
    ):
        if csv_path and csv_path not in seen:
            seen.add(csv_path)
            paths.append(csv_path)
    return paths


def _build_live_prematch_index():
    """Build {(norm_home, norm_away, comp): record} from today's upcoming CSVs.

    Stores both expected goals and pre-match win/draw/away probabilities
    so ``_compute_live_prediction`` can blend them with live Poisson odds.
    """
    index = {}
    for csv_path in _upcoming_csv_paths_for_live():
        if not os.path.exists(csv_path):
            continue
        try:
            frame = pd.read_csv(csv_path, dtype=str)
        except Exception:
            continue
        for _, row in frame.iterrows():
            comp = str(row.get("competition", "")).strip()
            home = str(row.get("home_team", "")).strip()
            away = str(row.get("away_team", "")).strip()
            if not comp or not home or not away:
                continue
            key = (_normalize_team_for_live(home), _normalize_team_for_live(away), comp)
            try:
                fhg = float(row.get("pred_home_goals", 0))
                fag = float(row.get("pred_away_goals", 0))
            except Exception:
                fhg, fag = 1.4, 1.2
            try:
                ph = float(row.get("prob_home", 0))
                pd_ = float(row.get("prob_draw", 0))
                pa = float(row.get("prob_away", 0))
            except Exception:
                ph = pd_ = pa = 0.0
            total = ph + pd_ + pa
            if total > 0:
                ph /= total
                pd_ /= total
                pa /= total
            index[key] = {
                "pred_home_goals": max(0.1, fhg),
                "pred_away_goals": max(0.1, fag),
                "predicted_result": str(row.get("predicted_result", "")).strip(),
                "prob_home": ph,
                "prob_draw": pd_,
                "prob_away": pa,
            }
    return index

def _match_prematch_record(home_team, away_team, comp_name, prematch_index):
    """Try to find a matching pre-match record using multiple strategies."""
    nh = _normalize_team_for_live(home_team)
    na = _normalize_team_for_live(away_team)
    key = (nh, na, comp_name)
    rec = prematch_index.get(key)
    if rec is not None:
        return rec
    db_home = _team_name_for_db(home_team)
    db_away = _team_name_for_db(away_team)
    if db_home != home_team or db_away != away_team:
        key2 = (_normalize_team_for_live(db_home), _normalize_team_for_live(db_away), comp_name)
        rec = prematch_index.get(key2)
        if rec is not None:
            return rec
    for csv_key, csv_rec in prematch_index.items():
        csv_nh, csv_na, csv_comp = csv_key
        if csv_comp != comp_name:
            continue
        if (nh in csv_nh or csv_nh in nh) and (na in csv_na or csv_na in na):
            return csv_rec
    return None

def _compute_live_next_to_score(game, effective_gd, home_stats, away_stats, time_frac):
    """Probability the next goal is scored by home, away, or no more goals.

    Uses effective GD, stat imbalance, and time remaining to estimate.
    """
    if time_frac <= 0.05:
        return {"home": 0.02, "away": 0.02, "none": 0.96}
    shots_h = _to_float_or_none(home_stats.get("totalShots")) or 0
    shots_a = _to_float_or_none(away_stats.get("totalShots")) or 0
    sot_h = _to_float_or_none(home_stats.get("shotsOnTarget")) or 0
    sot_a = _to_float_or_none(away_stats.get("shotsOnTarget")) or 0
    corners_h = _to_float_or_none(home_stats.get("corners")) or 0
    corners_a = _to_float_or_none(away_stats.get("corners")) or 0
    total_shots = max(shots_h + shots_a, 1)
    total_sot = max(sot_h + sot_a, 1)
    total_corners = max(corners_h + corners_a, 1)

    # Base threat from stats
    home_edge = 0.15 * (shots_h - shots_a) / total_shots + 0.25 * (sot_h - sot_a) / total_sot + 0.10 * (corners_h - corners_a) / total_corners
    home_edge = max(-0.5, min(0.5, home_edge))

    # Effective GD shifts threat: trailing team pushes harder
    gd_shift = -effective_gd * 0.15
    home_threat = 0.5 + home_edge + gd_shift
    home_threat = max(0.1, min(0.9, home_threat))

    p_any_goal = 0.55 * time_frac + 0.10
    p_any_goal = max(0.02, min(0.90, p_any_goal))
    p_home_next = home_threat * p_any_goal
    p_away_next = (1.0 - home_threat) * p_any_goal
    p_none = 1.0 - p_home_next - p_away_next

    return {
        "home": round(p_home_next, 4),
        "away": round(p_away_next, 4),
        "none": round(p_none, 4),
    }

def _compute_live_comeback_prob(live_probs, home_score, away_score):
    """Probability the trailing team avoids defeat (draws or wins).

    Returns 0 when tied; otherwise the draw + trailing-team-win probability.
    """
    if home_score == away_score:
        return 0.0
    if home_score < away_score:
        return round(live_probs.get("prob_home", 0) + live_probs.get("prob_draw", 0), 4)
    return round(live_probs.get("prob_away", 0) + live_probs.get("prob_draw", 0), 4)

def _compute_live_momentum(game, elapsed):
    """Recent territorial pressure (last 15 min of *match* time).

    Goals are not treated as momentum — a counter can score against the run
    of play. Uses shots/SOT/corners plus non-goal key events.
    """
    key_events = game.get("key_events") or []
    cutoff_min = max(0, elapsed - 15)
    recent = []
    for ev in key_events:
        ev_min = _key_event_match_minute(ev)
        if cutoff_min <= ev_min <= elapsed:
            recent.append(ev)

    # Pressure from boxscore imbalance (current half snapshot).
    home_stats = game.get("home_stats") or {}
    away_stats = game.get("away_stats") or {}

    def _f(side, key):
        src = home_stats if side == "home" else away_stats
        try:
            return float(src.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    shots_h, shots_a = _f("home", "totalShots"), _f("away", "totalShots")
    sot_h, sot_a = _f("home", "shotsOnTarget"), _f("away", "shotsOnTarget")
    cor_h, cor_a = _f("home", "wonCorners") or _f("home", "corners"), _f("away", "wonCorners") or _f("away", "corners")
    tot_shots = max(shots_h + shots_a, 1.0)
    tot_sot = max(sot_h + sot_a, 1.0)
    tot_cor = max(cor_h + cor_a, 1.0)
    score = (
        2.0 * (shots_h - shots_a) / tot_shots
        + 3.0 * (sot_h - sot_a) / tot_sot
        + 1.5 * (cor_h - cor_a) / tot_cor
    )

    # Cards / penalties can shift control; goals do not.
    weights = {
        "yellow card": 0.4,
        "red card": 1.8,
        "substitution": 0.15,
        "missed penalty": 0.6,
        "penalty": 0.5,
    }
    for ev in recent:
        ev_type = str(ev.get("type", ev.get("short_text", ev.get("text", "")))).lower()
        if "goal" in ev_type:
            continue
        w = 0.0
        for kw, weight in weights.items():
            if kw in ev_type:
                w = weight
                break
        team_id = ev.get("team_id", "")
        if not team_id or w == 0:
            continue
        is_home = team_id == str(game.get("home_team_id", ""))
        score += w if is_home else -w

    trend = max(-1.0, min(1.0, score / 6.0))
    label = "home" if trend > 0.15 else ("away" if trend < -0.15 else "neutral")
    return {"trend": round(trend, 3), "label": label, "events_recent": len(recent)}

def _extract_passes_to_stats(game):
    """Promote passes data from ``boxscore_stats`` into ``home_stats`` / ``away_stats``.

    ESPN often provides detailed passes at the boxscore level but not in the
    scoreboard competitor-statistics block.  This copies them up so they are
    available at the top-level ``home_stats``/``away_stats`` dict.
    """
    boxscore = game.get("boxscore_stats")
    if not boxscore or not isinstance(boxscore, dict):
        return
    for side in ("home", "away"):
        team_box = boxscore.get(side)
        if not team_box or not isinstance(team_box, list):
            continue
        stats_dict = game.get(f"{side}_stats")
        if stats_dict is None:
            stats_dict = {}
            game[f"{side}_stats"] = stats_dict
        for stat in team_box:
            name = stat.get("name", "")
            if name in ("passes", "totalPasses", "accuratePasses", "passAccuracy", "longPasses", "crosses"):
                if name not in stats_dict or not stats_dict[name]:
                    stats_dict[name] = str(stat.get("value", ""))

def _promote_team_stats_to_home_away(game):
    """Promote granular stats from ``team_stats`` into ``home_stats``/``away_stats``.

    The ``teamStats`` block contains richer categories (offensive, defensive,
    passing) than the scoreboard-level ``statistics``.  This flattens them
    so fields like shotsInsideBox, interceptions, aerialsWon, etc. are
    accessible at the top level.
    """
    ts = game.get("team_stats")
    if not ts or not isinstance(ts, dict):
        return
    for side in ("home", "away"):
        side_data = ts.get(side)
        if not isinstance(side_data, dict):
            continue
        stats_dict = game.get(f"{side}_stats")
        if stats_dict is None:
            stats_dict = {}
            game[f"{side}_stats"] = stats_dict
        for cat_name, cat_stats in side_data.items():
            if not isinstance(cat_stats, list):
                continue
            for s in cat_stats:
                name = s.get("name", "")
                if not name:
                    continue
                value = s.get("value", "")
                if name not in stats_dict or not stats_dict.get(name):
                    stats_dict[name] = value

def _update_cumulative_momentum(game):
    """Update per-match momentum timeline keyed by elapsed match minute.

    Scale (same as before):
        -100  = home territorial pressure
           0  = balanced
        +100  = away territorial pressure

    Built from **per-cycle pressure deltas** (shots, SOT, corners, territory,
    passing). Goals are **not** momentum — a counter can score against the
    run of play.

    Halftime: freeze; do not keep appending during the interval. Second half
    restarts at minute 45 with a fresh 0 so the chart lines up with the clock.
    """
    period = str(game.get("period") or "")
    status_type = str(game.get("status_type") or "")
    minute = int(_parse_elapsed_minutes(game.get("clock", "0'"), period))
    history = game.setdefault("momentum_history", [])

    def _append_point(min_, value, phase="play"):
        point = {"minute": int(min_), "value": value, "phase": phase}
        if history:
            last = history[-1]
            if isinstance(last, dict) and last.get("minute") == point["minute"] and last.get("phase") == phase:
                if phase == "play":
                    last["value"] = value
                return
        history.append(point)

    # ── Halftime: freeze, then restart at 45' ─────────────────
    if _is_halftime_break(period, status_type):
        if not game.get("_momentum_ht_frozen"):
            _append_point(45, None, phase="ht")
            game["_momentum_ht_frozen"] = True
            is_et_ht = "et" in period.lower() or "EXTRA_HALFTIME" in status_type.upper()
            game["_momentum_restart_2h"] = not is_et_ht
            _seed_momentum_stat_baselines(game)
        return

    if game.get("_momentum_restart_2h"):
        game["_momentum_restart_2h"] = False
        game["_momentum_ht_frozen"] = False
        game["_momentum_value"] = 0.0
        _seed_momentum_stat_baselines(game)
        _append_point(45, 0.0, phase="play")
        minute = max(minute, 45)

    if str(game.get("status") or "").lower() != "in":
        return

    cycle_delta = _pressure_cycle_delta(game)
    prev = game.get("_momentum_value")
    if prev is None:
        # First sample of the match: seed baselines, start the chart at 0.
        game["_momentum_value"] = 0.0
        _seed_momentum_stat_baselines(game)
        _append_point(minute, 0.0, phase="play")
        return

    alpha = 0.55
    raw = (1.0 - alpha) * float(prev) + alpha * cycle_delta
    val = round(max(-100.0, min(100.0, raw)), 2)
    game["_momentum_value"] = val
    _append_point(minute, val, phase="play")


def _seed_momentum_stat_baselines(game):
    """Record current cumulative stats so the next cycle only uses new events."""
    _pressure_cycle_delta(game, seed_only=True)


def _pressure_cycle_delta(game, seed_only=False):
    """Away-minus-home pressure this poll cycle. Positive = away momentum.

    Intentionally ignores the scoreline and goal events.
    """
    SHOT_WEIGHT = 12.0
    SOT_WEIGHT = 18.0
    CORNER_WEIGHT = 8.0
    YELLOW_WEIGHT = 3.0
    RED_CARD_WEIGHT = 28.0
    POSSESSION_WEIGHT = 50.0
    PASS_WEIGHT = 1.2
    CROSS_WEIGHT = 6.0
    KEY_PASS_WEIGHT = 8.0
    ATTACKING_THIRD_WEIGHT = 40.0

    home_stats = game.get("home_stats") or {}
    away_stats = game.get("away_stats") or {}

    def _stat(side, *keys):
        stats = home_stats if side == "home" else away_stats
        for key in keys:
            if key in stats and stats.get(key) not in (None, ""):
                return stats.get(key)
        if game.get("boxscore_stats"):
            for s in game["boxscore_stats"].get(side, []):
                if s.get("name") in keys:
                    return s.get("value")
        return None

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    def _delta(keys, prev_key):
        cur_h = _f(_stat("home", *keys))
        cur_a = _f(_stat("away", *keys))
        stored = game.get(f"_prev_{prev_key}")
        game[f"_prev_{prev_key}"] = (cur_h, cur_a)
        if stored is None or seed_only:
            return 0.0, 0.0
        prev_h, prev_a = stored
        return (cur_h - prev_h, cur_a - prev_a)

    if seed_only:
        _delta(("totalShots",), "shots")
        _delta(("shotsOnTarget",), "sot")
        _delta(("wonCorners", "corners"), "corners")
        _delta(("yellowCards",), "yellows")
        _delta(("redCards",), "reds")
        _delta(("totalPasses", "passes", "accuratePasses"), "passes")
        _delta(("totalCrosses", "crosses"), "crosses")
        _delta(("keyPasses",), "keypasses")
        return 0.0

    dh_shots, da_shots = _delta(("totalShots",), "shots")
    dh_sot, da_sot = _delta(("shotsOnTarget",), "sot")
    dh_corners, da_corners = _delta(("wonCorners", "corners"), "corners")
    dh_yellows, da_yellows = _delta(("yellowCards",), "yellows")
    dh_reds, da_reds = _delta(("redCards",), "reds")
    dh_passes, da_passes = _delta(("totalPasses", "passes", "accuratePasses"), "passes")
    dh_crosses, da_crosses = _delta(("totalCrosses", "crosses"), "crosses")
    dh_keypass, da_keypass = _delta(("keyPasses",), "keypasses")

    shot_delta = (da_shots - dh_shots) * SHOT_WEIGHT
    sot_delta = (da_sot - dh_sot) * SOT_WEIGHT
    corner_delta = (da_corners - dh_corners) * CORNER_WEIGHT
    yellow_delta = (da_yellows - dh_yellows) * YELLOW_WEIGHT
    red_delta = (da_reds - dh_reds) * RED_CARD_WEIGHT
    passes_delta = (da_passes - dh_passes) * PASS_WEIGHT
    crosses_delta = (da_crosses - dh_crosses) * CROSS_WEIGHT
    keypass_delta = (da_keypass - dh_keypass) * KEY_PASS_WEIGHT

    sit = game.get("situation") or {}
    poss = sit.get("possession")
    if isinstance(poss, dict):
        home_poss = _f(poss.get("home"))
        away_poss = _f(poss.get("away"))
    else:
        home_poss = _f(_stat("home", "possessionPct", "possession"))
        away_poss = _f(_stat("away", "possessionPct", "possession"))
    if home_poss or away_poss:
        total_poss = max(home_poss + away_poss, 1.0)
        poss_ratio = (home_poss - away_poss) / total_poss
        poss_delta = -poss_ratio * POSSESSION_WEIGHT
    else:
        poss_delta = 0.0

    zones = sit.get("possession_zones") or {}
    hz = zones.get("home") if isinstance(zones, dict) else None
    az = zones.get("away") if isinstance(zones, dict) else None
    if isinstance(hz, dict) or isinstance(az, dict):
        home_att = _f((hz or {}).get("attacking"))
        away_att = _f((az or {}).get("attacking"))
        tot_att = max(home_att + away_att, 1.0)
        att_delta = -((home_att - away_att) / tot_att) * ATTACKING_THIRD_WEIGHT
    else:
        att_delta = 0.0

    cycle_delta = (
        shot_delta
        + sot_delta
        + corner_delta
        + yellow_delta
        + red_delta
        + poss_delta
        + passes_delta
        + crosses_delta
        + keypass_delta
        + att_delta
    )
    return max(-100.0, min(100.0, cycle_delta))

def _compute_live_prediction(game, prematch):
    """Compute in-play prediction from live game state and pre-match data.

    **Logit scoreline model with pre-match blend**:

    1. A small pre-match nudge (``0.2 × time_frac × prem_xg_diff``) is
       added to the model's goal-difference estimate so 0-0 games still
       reflect pre-match strength and avoid the crude tied formula.
    2. **Pre-match blend** mixes the model's output with the full pre-match
       probabilities.  The blend decays with both elapsed time and goals
       scored: ``blend = 1 - (elapsed + 15 × |goal_diff|) / 90``.  Goals
       accelerate the transition to pure scoreline; a scoreless game lets
       pre-match weight persist longer.
    3. A stronger pre-match pull (``0.3 × time_frac × prem_xg_diff``) is
       added to ``effective_gd`` for ancillary metrics only (``next_to_score``,
       ``comeback_prob``, ``momentum``).
    """
    if game.get("status") != "in":
        return None
    home_score = game.get("home_score") or 0
    away_score = game.get("away_score") or 0
    goal_diff = home_score - away_score
    elapsed = _parse_elapsed_minutes(game.get("clock", "0'"), game.get("period", ""))
    period = str(game.get("period", "")).lower()
    is_extra_time = "extra" in period or "et" in period or "overtime" in period
    total_minutes = 120 if is_extra_time else 90
    time_frac = max(0.05, min(1.0, (total_minutes + 5 - elapsed) / total_minutes))

    # ── Pre-match data ─────────────────────────────────────────
    if prematch:
        prem_home = prematch.get("prob_home", 0.34)
        prem_draw = prematch.get("prob_draw", 0.33)
        prem_away = prematch.get("prob_away", 0.33)
        prem_xg_diff = prematch.get("pred_home_goals", 1.4) - prematch.get("pred_away_goals", 1.2)
    else:
        prem_home = prem_draw = prem_away = 1.0 / 3.0
        prem_xg_diff = 0.0

    # ── Pre-match pull (affects effective_gd only, not probs) ──
    prem_pull = prem_xg_diff * time_frac * 0.3

    # ── In-game statistics adjustment ──────────────────────────
    home_stats = game.get("home_stats", {}) or {}
    away_stats = game.get("away_stats", {}) or {}

    shots_h = _to_float_or_none(home_stats.get("totalShots")) or 0
    shots_a = _to_float_or_none(away_stats.get("totalShots")) or 0
    sot_h = _to_float_or_none(home_stats.get("shotsOnTarget")) or 0
    sot_a = _to_float_or_none(away_stats.get("shotsOnTarget")) or 0
    corners_h = _to_float_or_none(home_stats.get("corners")) or 0
    corners_a = _to_float_or_none(away_stats.get("corners")) or 0

    total_shots = max(shots_h + shots_a, 1)
    total_sot = max(sot_h + sot_a, 1)
    total_corners = max(corners_h + corners_a, 1)

    shot_diff = (shots_h - shots_a) / total_shots
    sot_diff = (sot_h - sot_a) / total_sot
    corner_diff = (corners_h - corners_a) / total_corners

    stat_adjustment = 0.15 * shot_diff + 0.25 * sot_diff + 0.10 * corner_diff

    # Red cards: each red card against the opponent is worth 0.5 goals
    reds_h = sum(1 for rc in game.get("red_cards", []) if rc.get("team") == "home")
    reds_a = sum(1 for rc in game.get("red_cards", []) if rc.get("team") == "away")
    red_adjustment = (reds_a - reds_h) * 0.5

    # ── Model probabilities ────────────────────────────────────
    # Small pre-match nudge keeps 0-0 games out of the tied formula
    # so the model still knows which team is better:
    prem_nudge = prem_xg_diff * time_frac * 0.2
    model_gd = goal_diff + stat_adjustment + red_adjustment + prem_nudge
    abs_model_gd = abs(model_gd)
    DRAW_RATIO = 0.75

    if abs_model_gd < 0.01:
        # Effectively tied
        w = 1.0 - time_frac
        m_home = max(0.001, 0.33 - 0.28 * w)
        m_draw = max(0.001, 0.34 + 0.56 * w)
        m_away = max(0.001, 0.33 - 0.28 * w)
    else:
        base_p = min(0.99, 0.50 + abs_model_gd * 0.12)
        base_logit = math.log(base_p / (1.0 - base_p))
        logit_boost = abs_model_gd * 2.0 * (1.0 - time_frac)
        final_logit = base_logit + logit_boost
        p_lead = 1.0 / (1.0 + math.exp(-final_logit))
        m_draw = max(0.001, (1.0 - p_lead) * DRAW_RATIO)
        p_lose = (1.0 - p_lead) - m_draw
        if model_gd > 0:
            m_home, m_away = p_lead, p_lose
        else:
            m_home, m_away = p_lose, p_lead
        # Late-game: cap losing team's win probability at 3 %,
        # redistribute excess to draw (realistic ceiling for a comeback)
        if time_frac < 0.15:  # last ~13 min
            if model_gd > 0:
                if m_away > 0.03:
                    m_draw += m_away - 0.03
                    m_away = 0.03
            else:
                if m_home > 0.03:
                    m_draw += m_home - 0.03
                    m_home = 0.03

    # ── Pre-match blend ────────────────────────────────────────
    # Blend decays with time AND with goals scored.  Goals are strong
    # evidence so they accelerate the transition toward the pure model.
    # At 0-0 the blend decays slowly so pre-match weight persists longer.
    blend = max(0.0, min(1.0, 1.0 - (elapsed + 15 * abs(goal_diff)) / total_minutes))

    p_home = blend * prem_home + (1.0 - blend) * m_home
    p_draw = blend * prem_draw + (1.0 - blend) * m_draw
    p_away = blend * prem_away + (1.0 - blend) * m_away

    # Clamp & normalise
    p_home = max(0.001, min(0.999, p_home))
    p_draw = max(0.001, min(0.999, p_draw))
    p_away = max(0.001, min(0.999, p_away))
    total = p_home + p_draw + p_away
    live_probs = {
        "prob_home": round(p_home / total, 4),
        "prob_draw": round(p_draw / total, 4),
        "prob_away": round(p_away / total, 4),
    }

    live_probs["home_score"] = home_score
    live_probs["away_score"] = away_score
    live_probs["elapsed"] = elapsed
    live_probs["time_remaining_frac"] = round(time_frac, 3)

    # ── Effective GD (with pre-match pull) for ancillary metrics ─
    effective_gd = model_gd + prem_pull

    # ── Next team to score ─────────────────────────────────────
    live_probs["next_to_score"] = _compute_live_next_to_score(
        game, effective_gd, home_stats, away_stats, time_frac,
    )

    # ── Comeback probability ───────────────────────────────────
    live_probs["comeback_prob"] = _compute_live_comeback_prob(
        live_probs, home_score, away_score,
    )

    # ── Momentum ───────────────────────────────────────────────
    live_probs["momentum"] = _compute_live_momentum(game, elapsed)
    if game.get("momentum_history"):
        live_probs["momentum_history"] = game["momentum_history"]

    return live_probs
