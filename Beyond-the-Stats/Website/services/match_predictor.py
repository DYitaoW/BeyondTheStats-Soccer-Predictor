"""
Single match prediction service, integrating ML models, MLS adjustments, and odds distributions.
"""

from __future__ import annotations

import os
import pandas as pd

import config
from math_utils import (
    _compute_asian_handicap,
    _compute_correct_score_dist,
    _compute_double_chance,
)
from team_utils import (
    _normalize_team_key,
    _team_name_for_db,
    _team_name_for_display,
    _to_float,
)
from .static_predictions import _get_static_predictions
from .context_service import get_context, _latest_season_for_competition


def _get_goal_prob(row: dict, col_name: str, pm_global: object = None) -> float | None:
    """Return goal probability from CSV column or compute from predicted goals."""
    val = row.get(col_name)
    if val is not None:
        try:
            f = float(val)
            if not pd.isna(f):
                return round(f, 6)
        except Exception:
            pass
    try:
        hg = float(row.get("pred_home_goals", 0))
        ag = float(row.get("pred_away_goals", 0))
    except Exception:
        return None
    try:
        if pm_global is None:
            import predictions
            pm_global = predictions.pm_global
        probs = pm_global.compute_goal_probabilities(hg, ag)
        return round(float(probs.get(col_name, 0)), 6)
    except Exception:
        return None


def _predict(home_raw: str, away_raw: str, mode: str = "global") -> dict:
    """Run a single match prediction and return probabilities plus stat projections."""
    if getattr(config, "STATIC_PREDICTIONS", False):
        lookup, _ = _get_static_predictions(mode)
        key = (_normalize_team_key(home_raw), _normalize_team_key(away_raw))
        record = lookup.get(key)
        if not record:
            raise ValueError("Prediction not available in static data.")
        prediction = record.get("predicted_result") or ""
        if prediction not in {"H", "D", "A"}:
            probs = {
                "H": record.get("prob_home", 0.0),
                "D": record.get("prob_draw", 0.0),
                "A": record.get("prob_away", 0.0),
            }
            prediction = max(probs, key=probs.get)
        home_display = _team_name_for_display(record["home_team"])
        away_display = _team_name_for_display(record["away_team"])
        return {
            "home_team": home_display,
            "away_team": away_display,
            "competition": record.get("competition") or "",
            "predicted_result": prediction,
            "winner_label": {"H": f"{home_display} win", "D": "Draw", "A": f"{away_display} win"}[prediction],
            "prob_home": round(_to_float(record.get("prob_home", 0.0)) * 100, 3),
            "prob_draw": round(_to_float(record.get("prob_draw", 0.0)) * 100, 3),
            "prob_away": round(_to_float(record.get("prob_away", 0.0)) * 100, 3),
            "pred_home_goals": int(round(_to_float(record.get("pred_home_goals", 0.0)))),
            "pred_away_goals": int(round(_to_float(record.get("pred_away_goals", 0.0)))),
            "pred_home_shots": round(_to_float(record.get("pred_home_shots", 0.0)), 2),
            "pred_away_shots": round(_to_float(record.get("pred_away_shots", 0.0)), 2),
            "pred_home_sot": round(_to_float(record.get("pred_home_sot", 0.0)), 2),
            "pred_away_sot": round(_to_float(record.get("pred_away_sot", 0.0)), 2),
            "prob_home_goals_0": _get_goal_prob(record, "prob_home_goals_0"),
            "prob_home_goals_1plus": _get_goal_prob(record, "prob_home_goals_1plus"),
            "prob_home_goals_2plus": _get_goal_prob(record, "prob_home_goals_2plus"),
            "prob_away_goals_0": _get_goal_prob(record, "prob_away_goals_0"),
            "prob_away_goals_1plus": _get_goal_prob(record, "prob_away_goals_1plus"),
            "prob_away_goals_2plus": _get_goal_prob(record, "prob_away_goals_2plus"),
            "prob_both_score": _get_goal_prob(record, "prob_both_score"),
            "prob_over_1_5": _get_goal_prob(record, "prob_over_1_5"),
            "prob_over_2_5": _get_goal_prob(record, "prob_over_2_5"),
            "prob_over_3_5": _get_goal_prob(record, "prob_over_3_5"),
            "correct_score_dist": _compute_correct_score_dist(
                record.get("pred_home_goals"), record.get("pred_away_goals"),
            ),
            "double_chance": _compute_double_chance(
                _to_float(record.get("prob_home", 0.0)),
                _to_float(record.get("prob_draw", 0.0)),
                _to_float(record.get("prob_away", 0.0)),
            ),
            "asian_handicap": _compute_asian_handicap(
                record.get("pred_home_goals"), record.get("pred_away_goals"),
                prob_home=_to_float(record.get("prob_home", 0.0)),
                prob_draw=_to_float(record.get("prob_draw", 0.0)),
                prob_away=_to_float(record.get("prob_away", 0.0)),
            ),
        }

    ctx = get_context(mode)
    pm = ctx.pm
    home_input = _team_name_for_db(home_raw)
    away_input = _team_name_for_db(away_raw)
    home_team = pm.resolve_team_name(home_input, ctx.available_teams)
    away_team = pm.resolve_team_name(away_input, ctx.available_teams)
    if not home_team or not away_team:
        raise ValueError("One or both team names were not recognized.")
    if home_team == away_team:
        raise ValueError("Home and away teams must be different.")

    home_comp = str(ctx.team_competition_map.get(home_team, "")).strip()
    away_comp = str(ctx.team_competition_map.get(away_team, "")).strip()
    competition_hint = home_comp if home_comp and home_comp == away_comp else (home_comp or away_comp)
    competition_fallback = _latest_season_for_competition(
        ctx.season_teams,
        competition_hint,
        ctx.latest_season,
        pm.parse_start_year_from_key,
    )
    prediction_season = pm.choose_season_for_teams(home_team, away_team, ctx.season_teams, competition_fallback)
    competition_key = os.path.dirname(prediction_season).replace("\\", "/") or "Unknown"
    feature_competition = competition_hint or competition_key
    prediction_start_year = pm.parse_start_year_from_key(prediction_season)
    season_coeff = pm.season_recency_coefficient(ctx.latest_start_year, prediction_start_year)
    home_comp = ctx.team_competition_map.get(home_team, feature_competition)
    away_comp = ctx.team_competition_map.get(away_team, feature_competition)

    match_input = pm.build_match_input(home_team, away_team)
    X_match = pm.build_features(
        match_input,
        prediction_season,
        feature_competition,
        season_coeff,
        ctx.overall_teams,
        ctx.season_teams,
        ctx.head_to_head,
        ctx.current_form,
        ctx.league_strength,
        home_competition_override=home_comp,
        away_competition_override=away_comp,
    )
    X_match = pd.get_dummies(X_match, columns=["competition"], dtype=float)
    X_match = X_match.reindex(columns=ctx.train_columns, fill_value=0.0)

    probabilities = {"H": 0.0, "D": 0.0, "A": 0.0}
    proba_values = ctx.clf.predict_proba(X_match)[0]
    for idx, encoded_label in enumerate(ctx.clf.classes_):
        label = ctx.result_label_encoder.inverse_transform([encoded_label])[0]
        probabilities[label] = float(proba_values[idx])
    if mode == "mls":
        home_league_strength = float(ctx.league_strength.get(home_comp, 0.85))
        away_league_strength = float(ctx.league_strength.get(away_comp, 0.85))
        probabilities, _, _ = pm.apply_league_strength_adjustment(
            probabilities, home_league_strength, away_league_strength
        )

        home_adv_shift = pm.mls_home_advantage_shift(home_team, prediction_season, ctx.season_teams)
        transfer = min(home_adv_shift, probabilities.get("A", 0.0))
        probabilities["H"] = max(0.0, probabilities.get("H", 0.0) + transfer)
        probabilities["A"] = max(0.0, probabilities.get("A", 0.0) - transfer)
        total_prob = probabilities.get("H", 0.0) + probabilities.get("D", 0.0) + probabilities.get("A", 0.0)
        if total_prob > 0:
            probabilities["H"] /= total_prob
            probabilities["D"] /= total_prob
            probabilities["A"] /= total_prob

        market_shift, _, _ = pm.market_value_probability_shift(
            home_team, away_team, ctx.market_value_data
        )
        if market_shift != 0.0:
            if market_shift > 0:
                transfer = min(market_shift, probabilities.get("A", 0.0))
                probabilities["H"] += transfer
                probabilities["A"] -= transfer
            else:
                transfer = min(abs(market_shift), probabilities.get("H", 0.0))
                probabilities["A"] += transfer
                probabilities["H"] -= transfer
            total_prob = probabilities.get("H", 0.0) + probabilities.get("D", 0.0) + probabilities.get("A", 0.0)
            if total_prob > 0:
                probabilities["H"] /= total_prob
                probabilities["D"] /= total_prob
                probabilities["A"] /= total_prob

        probabilities = pm.apply_home_advantage_boost(probabilities)
        probabilities = pm.reduce_draw_probability(probabilities)
        seed = pm.prediction_randomizer_seed(home_team, away_team, feature_competition, prediction_season)
        probabilities = pm.apply_probability_randomizer(
            probabilities,
            pm.MLS_RANDOMIZER_MAX_DELTA,
            seed=seed,
        )
    else:
        probabilities = pm.reduce_draw_probability(probabilities)
        seed = pm.prediction_randomizer_seed(home_team, away_team, feature_competition, prediction_season)
        max_delta = getattr(pm, "EU_RANDOMIZER_MAX_DELTA", None)
        if max_delta is None:
            max_delta = getattr(pm, "MLS_RANDOMIZER_MAX_DELTA", 0.12)
        probabilities = pm.apply_probability_randomizer(
            probabilities,
            max_delta,
            seed=seed,
        )

    prediction = max(probabilities, key=probabilities.get)
    home_goals = max(0.0, float(ctx.home_goal_reg.predict(X_match)[0]))
    away_goals = max(0.0, float(ctx.away_goal_reg.predict(X_match)[0]))
    home_shots = max(0.0, float(ctx.home_shot_reg.predict(X_match)[0]))
    away_shots = max(0.0, float(ctx.away_shot_reg.predict(X_match)[0]))
    home_sot = max(0.0, float(ctx.home_sot_reg.predict(X_match)[0]))
    away_sot = max(0.0, float(ctx.away_sot_reg.predict(X_match)[0]))

    home_display = _team_name_for_display(home_team)
    away_display = _team_name_for_display(away_team)

    goal_probs = pm.compute_goal_probabilities(home_goals, away_goals)
    aligned_home, aligned_away = pm.align_predicted_score(home_goals, away_goals, prediction)

    return {
        "home_team": home_display,
        "away_team": away_display,
        "competition": home_comp if home_comp == away_comp else f"{home_comp} vs {away_comp}",
        "predicted_result": prediction,
        "winner_label": {"H": f"{home_display} win", "D": "Draw", "A": f"{away_display} win"}[prediction],
        "prob_home": round(probabilities["H"] * 100, 3),
        "prob_draw": round(probabilities["D"] * 100, 3),
        "prob_away": round(probabilities["A"] * 100, 3),
        "pred_home_goals": int(round(home_goals)),
        "pred_away_goals": int(round(away_goals)),
        "pred_home_shots": round(home_shots, 2),
        "pred_away_shots": round(away_shots, 2),
        "pred_home_sot": round(home_sot, 2),
        "pred_away_sot": round(away_sot, 2),
        "prob_home_goals_0": round(goal_probs.get("prob_home_goals_0", 0), 6),
        "prob_home_goals_1plus": round(goal_probs.get("prob_home_goals_1plus", 0), 6),
        "prob_home_goals_2plus": round(goal_probs.get("prob_home_goals_2plus", 0), 6),
        "prob_away_goals_0": round(goal_probs.get("prob_away_goals_0", 0), 6),
        "prob_away_goals_1plus": round(goal_probs.get("prob_away_goals_1plus", 0), 6),
        "prob_away_goals_2plus": round(goal_probs.get("prob_away_goals_2plus", 0), 6),
        "prob_both_score": round(goal_probs.get("prob_both_score", 0), 6),
        "prob_over_1_5": round(goal_probs.get("prob_over_1_5", 0), 6),
        "prob_over_2_5": round(goal_probs.get("prob_over_2_5", 0), 6),
        "prob_over_3_5": round(goal_probs.get("prob_over_3_5", 0), 6),
        "correct_score_dist": _compute_correct_score_dist(home_goals, away_goals),
        "double_chance": _compute_double_chance(
            probabilities.get("H", 0), probabilities.get("D", 0), probabilities.get("A", 0),
        ),
        "asian_handicap": _compute_asian_handicap(
            home_goals, away_goals,
            prob_home=probabilities.get("H", 0),
            prob_draw=probabilities.get("D", 0),
            prob_away=probabilities.get("A", 0),
        ),
    }

