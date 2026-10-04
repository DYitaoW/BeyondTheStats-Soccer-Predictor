"""
Model bundle and PredictorContext loading and cache management services.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any
import joblib
import pandas as pd

import config

@dataclass
class PredictorContext:
    pm: object
    clf: object
    result_label_encoder: object
    home_goal_reg: object
    away_goal_reg: object
    home_shot_reg: object
    away_shot_reg: object
    home_sot_reg: object
    away_sot_reg: object
    train_columns: pd.Index
    overall_teams: dict
    season_teams: dict
    head_to_head: dict
    current_form: dict
    league_strength: dict
    latest_season: str
    latest_start_year: int
    team_competition_map: dict
    available_teams: list
    market_value_data: dict


_ctx_lock = threading.Lock()
_ctx_global: PredictorContext | None = None
_ctx_mls: PredictorContext | None = None
_ctx_extra: PredictorContext | None = None


def _load_context(pm_mod) -> PredictorContext:
    """Load cached model bundle and supporting team data for one predictor mode."""
    matches, season_files = pm_mod.load_training_matches(pm_mod.PROCESSED_DIR)

    if not os.path.exists(pm_mod.MODEL_CACHE):
        raise FileNotFoundError(
            f"Model cache not found at {pm_mod.MODEL_CACHE}. Run Predict_Match.py once first."
        )

    bundle = joblib.load(pm_mod.MODEL_CACHE)
    fingerprint = pm_mod.data_fingerprint(season_files)
    if bundle.get("fingerprint") != fingerprint:
        print(f"[predictions] Model cache fingerprint mismatch; using cached models (full retrain runs Tue/Fri)")

    overall_teams = pm_mod.load_json_if_exists(os.path.join(pm_mod.TEAM_DATA_DIR, "overall_teams.json"))
    season_teams = pm_mod.load_json_if_exists(os.path.join(pm_mod.TEAM_DATA_DIR, "season_teams.json"))
    head_to_head = pm_mod.load_json_if_exists(os.path.join(pm_mod.TEAM_DATA_DIR, "head_to_head.json"))
    current_form = pm_mod.load_json_if_exists(os.path.join(pm_mod.TEAM_DATA_DIR, "current_form.json"))
    league_strength = pm_mod.load_json_if_exists(os.path.join(pm_mod.TEAM_DATA_DIR, "league_strength.json")) or {}
    market_value_data = pm_mod.load_json_if_exists(
        os.path.join(pm_mod.TEAM_DATA_DIR, "mls_squad_values.json")
    ) or {}
    if not market_value_data or not market_value_data.get("teams"):
        try:
            from shared import sqlite_store
            db_squad = sqlite_store.load_squad_values(competition="United States/MLS")
            if db_squad and db_squad.get("teams"):
                market_value_data = db_squad
        except Exception:
            pass
    dynamic_form = pm_mod.build_dynamic_form_from_matches(matches)

    if (
        overall_teams is None
        or season_teams is None
        or head_to_head is None
        or current_form is None
        or not isinstance(overall_teams, dict)
        or len(overall_teams) == 0
    ):
        overall_teams, season_teams, head_to_head, current_form = pm_mod.build_fallback_data(matches, season_files)

    overall_teams = pm_mod.replace_nan_with_sentinel(overall_teams)
    season_teams = pm_mod.replace_nan_with_sentinel(season_teams)
    head_to_head = pm_mod.replace_nan_with_sentinel(head_to_head)
    current_form = pm_mod.replace_nan_with_sentinel(current_form)
    league_strength = pm_mod.replace_nan_with_sentinel(league_strength)

    if not isinstance(current_form, dict):
        current_form = {"teams": {}}
    if "teams" not in current_form or not isinstance(current_form["teams"], dict):
        current_form["teams"] = {}
    current_form_teams = current_form["teams"]
    for team, stats in dynamic_form.items():
        if team not in current_form_teams or not isinstance(current_form_teams.get(team), dict):
            current_form_teams[team] = stats
            continue
        existing = current_form_teams[team]
        for key, value in stats.items():
            if key not in existing or existing.get(key) in (None, "", 0, 0.0):
                existing[key] = value

    team_competition_map = {}
    for _, row in matches.iterrows():
        team_competition_map[row["HomeTeam"]] = row["competition"]
        team_competition_map[row["AwayTeam"]] = row["competition"]

    latest_season = season_files[-1].replace(".csv", "")
    csv_latest_year = max(pm_mod.parse_start_year_from_key(key) for key in season_teams.keys())
    latest_start_year = max(csv_latest_year, pm_mod.expected_current_latest_start_year())
    available_teams = sorted(set(matches["HomeTeam"].dropna()) | set(matches["AwayTeam"].dropna()))

    return PredictorContext(
        pm=pm_mod,
        clf=bundle["clf"],
        result_label_encoder=bundle["result_label_encoder"],
        home_goal_reg=bundle["home_goal_reg"],
        away_goal_reg=bundle["away_goal_reg"],
        home_shot_reg=bundle["home_shot_reg"],
        away_shot_reg=bundle["away_shot_reg"],
        home_sot_reg=bundle["home_sot_reg"],
        away_sot_reg=bundle["away_sot_reg"],
        train_columns=bundle["train_columns"],
        overall_teams=overall_teams,
        season_teams=season_teams,
        head_to_head=head_to_head,
        current_form=current_form,
        league_strength=league_strength,
        latest_season=latest_season,
        latest_start_year=latest_start_year,
        team_competition_map=team_competition_map,
        available_teams=available_teams,
        market_value_data=market_value_data,
    )


def _latest_season_for_competition(season_teams, competition, fallback, parse_start_year):
    """Return latest season key for competition or fallback when unknown."""
    competition = str(competition or "").strip()
    if not competition:
        return fallback
    best_key = None
    best_year = -1
    prefix = f"{competition}/"
    for season_key in season_teams.keys():
        if not str(season_key).startswith(prefix):
            continue
        year = parse_start_year(season_key)
        if year > best_year:
            best_year = year
            best_key = season_key
    return best_key or fallback


def get_context(mode="global") -> PredictorContext:
    """Return lazily initialized prediction context for global, MLS, or extra mode."""
    global _ctx_global, _ctx_mls, _ctx_extra
    import predictions
    if mode == "mls":
        if _ctx_mls is None:
            with _ctx_lock:
                if _ctx_mls is None:
                    _ctx_mls = _load_context(predictions.pm_mls)
        return _ctx_mls
    if mode == "extra":
        if _ctx_extra is None:
            with _ctx_lock:
                if _ctx_extra is None:
                    _ctx_extra = _load_context(predictions.pm_extra)
        return _ctx_extra

    if _ctx_global is None:
        with _ctx_lock:
            if _ctx_global is None:
                _ctx_global = _load_context(predictions.pm_global)
    return _ctx_global


def _invalidate_prediction_caches(*, reload_contexts: bool = False) -> None:
    """Clear in-memory predictor state and caches after a pipeline run."""
    global _ctx_global, _ctx_mls, _ctx_extra
    with _ctx_lock:
        _ctx_global = None
        _ctx_mls = None
        _ctx_extra = None
    try:
        from cache import _cache_clear_pattern
        _cache_clear_pattern()
    except Exception:
        pass
    try:
        from standings import _clear_all_real_data_caches
        _clear_all_real_data_caches()
    except Exception:
        pass
    try:
        from league_data import clear_league_data_caches
        clear_league_data_caches()
    except Exception:
        pass
    if reload_contexts and not getattr(config, "STATIC_PREDICTIONS", False):
        try:
            get_context("global")
            get_context("mls")
            get_context("extra")
            print("[refresh] Model contexts reloaded successfully.")
        except Exception as exc:
            print(f"[refresh] Context reload warning: {exc}")

