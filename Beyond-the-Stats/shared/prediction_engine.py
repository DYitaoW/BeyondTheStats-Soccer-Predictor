"""
Shared core prediction engine for Beyond-the-Stats.

Consolidates the ML prediction, feature engineering, and model training logic
previously duplicated across Europe, MLS, and Extra-league pipelines into a
single unified, highly-configurable engine.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesClassifier, RandomForestClassifier, RandomForestRegressor
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import LabelEncoder, StandardScaler

try:
    from xgboost import XGBClassifier, XGBRegressor
except ImportError:
    XGBClassifier = None
    XGBRegressor = None


# --- Shared Constants ---
EARLY_SEASON_GAMES_THRESHOLD = 19
EARLY_SEASON_FULL_PRIOR_GAMES = 7
EARLY_SEASON_FADE_END_GAMES = 19
PRIOR_BLEND_STRENGTH_AT_ZERO = 0.85
PROMOTED_TEAM_STRENGTH_CAP = 0.58
RELEGATED_TEAM_STRENGTH_FLOOR = 0.62

GOAL_PROB_TARGETS = [
    ("goal_prob_home_0", lambda hg, ag: (hg == 0).astype(int)),
    ("goal_prob_home_1plus", lambda hg, ag: (hg >= 1).astype(int)),
    ("goal_prob_home_2plus", lambda hg, ag: (hg >= 2).astype(int)),
    ("goal_prob_away_0", lambda hg, ag: (ag == 0).astype(int)),
    ("goal_prob_away_1plus", lambda hg, ag: (ag >= 1).astype(int)),
    ("goal_prob_away_2plus", lambda hg, ag: (ag >= 2).astype(int)),
    ("goal_prob_both_score", lambda hg, ag: ((hg > 0) & (ag > 0)).astype(int)),
    ("goal_prob_over_1_5", lambda hg, ag: ((hg + ag) > 1).astype(int)),
    ("goal_prob_over_2_5", lambda hg, ag: ((hg + ag) > 2).astype(int)),
    ("goal_prob_over_3_5", lambda hg, ag: ((hg + ag) > 3).astype(int)),
]

GOAL_PROB_RESULT_KEYS = [
    "prob_home_goals_0",
    "prob_home_goals_1plus",
    "prob_home_goals_2plus",
    "prob_away_goals_0",
    "prob_away_goals_1plus",
    "prob_away_goals_2plus",
    "prob_both_score",
    "prob_over_1_5",
    "prob_over_2_5",
    "prob_over_3_5",
]

CPU_COUNT = max(1, (os.cpu_count() or 2) - 1)
TRAIN_WORKERS = int(os.getenv("SOCCER_TRAIN_WORKERS", str(max(1, min(4, CPU_COUNT // 2)))))
MODEL_THREADS = int(os.getenv("SOCCER_MODEL_THREADS", str(max(1, CPU_COUNT // TRAIN_WORKERS))))


class AveragedProbaClassifier:
    """Ensemble classifier averaging predicted probability distributions."""

    def __init__(self, models):
        self.models = models
        self.classes_ = models[0].classes_

    def predict_proba(self, X):
        matrices = [model.predict_proba(X) for model in self.models]
        return sum(matrices) / len(matrices)

    def predict(self, X):
        avg = self.predict_proba(X)
        idx = avg.argmax(axis=1)
        return self.classes_[idx]


@dataclass
class RegionConfig:
    region_name: str
    base_dir: str
    predictions_dir: str
    project_dir: str
    files_dir: str
    processed_dir: str
    team_data_dir: str
    model_cache: str
    season_pattern: re.Pattern
    mapping_file: str
    min_start_year: int = 2002

    # Regional model toggles
    use_ensemble_classifier: bool = False
    enable_mls_home_advantage: bool = False
    enable_market_value_shift: bool = False
    disable_post_model_form_shift: bool = False

    # Tuning numbers
    randomizer_max_delta: float = 0.08
    draw_reduction_factor: float = 0.08
    high_draw_threshold: float = 0.42
    high_draw_extra_reduction_max: float = 0.18
    home_extra_boost: float = 0.012

    # MLS & Extra specific constants
    mls_home_edge_shift: float = 0.045
    mls_draw_target: float = 0.27
    mls_draw_blend: float = 0.20
    mls_adjustment_weight: float = 0.45
    mls_market_shift_max: float = 0.075
    mls_market_shift_scale: float = 0.10
    mls_home_adv_min_shift: float = 0.03
    mls_home_adv_max_shift: float = 0.13

    # Fallback paths for empty regional datasets
    shared_processed_dir: Optional[str] = None
    shared_team_data_dir: Optional[str] = None
    shared_model_cache: Optional[str] = None


class PredictionEngine:
    def __init__(self, config: RegionConfig):
        self.config = config
        self._name_mapping_cache: Optional[dict] = None
        self._historical_tables_cache: Optional[dict] = None

    # --- Directory & Cache Fallbacks ---
    def processed_dir_has_season_csvs(self, processed_dir: str) -> bool:
        if not processed_dir or not os.path.isdir(processed_dir):
            return False
        for root, _, files in os.walk(processed_dir):
            for name in files:
                if name.endswith(".csv") and self.config.season_pattern.match(name):
                    return True
        return False

    def resolve_processed_dir(self, preferred: Optional[str] = None) -> str:
        preferred = preferred or self.config.processed_dir
        if self.processed_dir_has_season_csvs(preferred):
            return preferred
        shared = self.config.shared_processed_dir
        if shared and preferred != shared and self.processed_dir_has_season_csvs(shared):
            print(
                f"[{self.config.region_name}] Processed_Data empty at {preferred}; "
                f"falling back to shared {shared}",
                flush=True,
            )
            return shared
        return preferred

    def resolve_team_data_dir(self, preferred: Optional[str] = None) -> str:
        preferred = preferred or self.config.team_data_dir
        marker = os.path.join(preferred, "overall_teams.json")
        if os.path.isfile(marker):
            return preferred
        shared = self.config.shared_team_data_dir
        if shared and preferred != shared:
            shared_marker = os.path.join(shared, "overall_teams.json")
            if os.path.isfile(shared_marker):
                print(
                    f"[{self.config.region_name}] Team_Data missing at {preferred}; "
                    f"falling back to shared {shared}",
                    flush=True,
                )
                return shared
        return preferred

    def resolve_model_cache_path(self, preferred: Optional[str] = None) -> str:
        preferred = preferred or self.config.model_cache
        if os.path.isfile(preferred):
            return preferred
        shared = self.config.shared_model_cache
        if shared and preferred != shared and os.path.isfile(shared):
            print(
                f"[{self.config.region_name}] model cache missing at {preferred}; "
                f"falling back to shared {shared}",
                flush=True,
            )
            return shared
        return preferred

    # --- Early-Season Priors ---
    def early_season_prior_weight(self, games_played: float | int) -> float:
        games = max(0.0, float(games_played))
        if games <= EARLY_SEASON_FULL_PRIOR_GAMES:
            return PRIOR_BLEND_STRENGTH_AT_ZERO
        if games >= EARLY_SEASON_FADE_END_GAMES:
            return 0.0
        span = float(EARLY_SEASON_FADE_END_GAMES - EARLY_SEASON_FULL_PRIOR_GAMES)
        fraction = (games - EARLY_SEASON_FULL_PRIOR_GAMES) / span
        return PRIOR_BLEND_STRENGTH_AT_ZERO * (1.0 - fraction)

    def early_season_prior_factor(self, games_played: float | int) -> float:
        games = max(0.0, float(games_played))
        if games <= EARLY_SEASON_FULL_PRIOR_GAMES:
            return 1.0
        if games >= EARLY_SEASON_FADE_END_GAMES:
            return 0.0
        span = float(EARLY_SEASON_FADE_END_GAMES - EARLY_SEASON_FULL_PRIOR_GAMES)
        return 1.0 - (games - EARLY_SEASON_FULL_PRIOR_GAMES) / span

    def _season_teams_signature(self) -> Optional[str]:
        team_data_dir = self.resolve_team_data_dir()
        path = os.path.join(team_data_dir, "season_teams.json")
        try:
            with open(path, encoding="utf-8-sig") as fh:
                season_teams = json.load(fh)
        except Exception:
            return None
        if not isinstance(season_teams, dict):
            return None
        digest = hashlib.sha1()
        for key in sorted(season_teams):
            digest.update(str(key).encode("utf-8", errors="replace"))
            digest.update(b"\x00")
        return digest.hexdigest()

    def _load_historical_tables(self) -> dict:
        if self._historical_tables_cache is not None:
            return self._historical_tables_cache

        team_data_dir = self.resolve_team_data_dir()
        path = os.path.join(team_data_dir, "historical_tables.json")
        current = self._season_teams_signature()
        cached = self.load_json_if_exists(path) or {}
        if not cached or (current and cached.get("generated_season_signature") != current):
            builder = os.path.join(self.config.files_dir, "Build_Historical_Tables.py")
            if os.path.exists(builder):
                try:
                    subprocess.run(
                        [sys.executable, builder],
                        cwd=self.config.base_dir,
                        capture_output=True,
                        text=True,
                        timeout=600,
                    )
                except Exception:
                    pass
            cached = self.load_json_if_exists(path) or {}
        self._historical_tables_cache = cached
        return cached

    def team_division_history(self, team: str, competition: Optional[str] = None, historical: Optional[dict] = None) -> Tuple[Optional[str], Optional[str], list]:
        if historical is None:
            historical = self._load_historical_tables()
        country = str(competition or "").replace("\\", "/").split("/", 1)[0]
        bucket = (historical.get("team_history") or {}).get(country)
        if not bucket:
            return None, None, []
        seasons = []
        for season in bucket.get("seasons") or []:
            rec = (season.get("records") or {}).get(team)
            entry = {"start_year": season.get("start_year", 0)}
            entry.update(rec or {})
            seasons.append(entry)
        return country, bucket.get("seed_season"), seasons

    def _season_games_played(self, team: str, season_lookup: dict) -> int:
        stats = self.clean_stats_dict(season_lookup.get(team, {})) if isinstance(season_lookup, dict) else {}
        if not stats:
            return 0
        try:
            return int(float(stats.get("games", 0.0) or 0.0))
        except (TypeError, ValueError):
            return 0

    def _prior_team_strength(self, team: str, team_prior: dict) -> Tuple[float, bool]:
        entry = team_prior.get(team)
        if not entry:
            return 0.45, False
        position = float(entry.get("position", 0.0) or 0.0)
        games = float(entry.get("games", 0.0) or 0.0)
        if position <= 0 or games <= 0:
            return 0.45, False
        strength = 1.0 / (1.0 + (position - 1.0) * 0.08)
        strength = max(0.05, min(1.0, strength))
        return strength, True

    def _prior_distribution(
        self,
        home_team: str,
        away_team: str,
        team_prior: dict,
        current_competition: Optional[str] = None,
        is_neutral: bool = False,
        league_strength: Optional[dict] = None,
    ) -> Optional[dict]:
        home_entry = team_prior.get(home_team)
        away_entry = team_prior.get(away_team)

        home_strength, home_found = self._prior_team_strength(home_team, team_prior)
        away_strength, away_found = self._prior_team_strength(away_team, team_prior)
        if not home_found and not away_found:
            return None

        if not home_found:
            home_strength = 0.45
        if not away_found:
            away_strength = 0.45

        if isinstance(league_strength, dict):
            current_key = str(current_competition or "").replace("\\", "/")
            current_ls = float(league_strength.get(current_key, 0.0))

            def _ls_lookup(competition):
                key = str(competition or "").replace("\\", "/")
                return float(league_strength.get(key, 0.60))

            def _scaled_strength(strength, entry):
                prior_ls = _ls_lookup(entry.get("competition")) if entry else 0.60
                if entry and current_ls > 0:
                    prior_key = str(entry.get("competition") or "").replace("\\", "/")
                    if prior_key and prior_key != current_key:
                        ratio = prior_ls / current_ls
                        if prior_ls > current_ls:
                            strength = max(strength * ratio, RELEGATED_TEAM_STRENGTH_FLOOR)
                        elif prior_ls < current_ls:
                            strength = min(strength * ratio, PROMOTED_TEAM_STRENGTH_CAP)
                        else:
                            strength = strength * ratio
                        return max(0.05, min(1.0, strength))
                return max(0.05, min(1.0, strength * prior_ls))

            home_strength = _scaled_strength(home_strength, home_entry)
            away_strength = _scaled_strength(away_strength, away_entry)

        if not is_neutral:
            home_strength += 0.06

        strength_gap = abs(home_strength - away_strength)
        draw_prior = max(0.24, min(0.34, 0.34 - 0.5 * strength_gap))
        if home_strength >= away_strength:
            home_share = 0.5 + 0.5 * strength_gap
        else:
            home_share = 0.5 - 0.5 * strength_gap
        home_share = max(0.15, min(0.85, home_share))

        away_share = 1.0 - home_share
        home_prob = (1.0 - draw_prior) * home_share
        away_prob = (1.0 - draw_prior) * away_share
        total = home_prob + draw_prior + away_prob
        return {
            "H": home_prob / total,
            "D": draw_prior / total,
            "A": away_prob / total,
        }

    def blend_historical_prior(
        self,
        probabilities: dict,
        home_team: str,
        away_team: str,
        season_lookup: dict,
        competition: Optional[str] = None,
        is_neutral: bool = False,
        league_strength: Optional[dict] = None,
    ) -> dict:
        if not isinstance(probabilities, dict):
            return probabilities
        home_games = self._season_games_played(home_team, season_lookup)
        away_games = self._season_games_played(away_team, season_lookup)
        if min(home_games, away_games) >= EARLY_SEASON_FADE_END_GAMES:
            return dict(probabilities)

        historical = self._load_historical_tables()
        team_prior = historical.get("team_prior") or {}
        prior = self._prior_distribution(
            home_team,
            away_team,
            team_prior,
            current_competition=competition,
            is_neutral=is_neutral,
            league_strength=league_strength,
        )
        if not prior:
            return dict(probabilities)

        least_played = min(home_games, away_games)
        prior_weight = self.early_season_prior_weight(least_played)
        if prior_weight <= 0.0:
            return dict(probabilities)

        blended = {}
        for key in ("H", "D", "A"):
            current = float(probabilities.get(key, 0.0))
            prior_val = float(prior.get(key, 0.0))
            blended[key] = prior_weight * prior_val + (1.0 - prior_weight) * current
        total = blended["H"] + blended["D"] + blended["A"]
        if total > 0:
            blended["H"] /= total
            blended["D"] /= total
            blended["A"] /= total
        return blended

    # --- Goal Poisson Sampling and Calculations ---
    def sample_score_from_probs(
        self,
        prob_home: float,
        prob_draw: float,
        prob_away: float,
        base_home_xg: float,
        base_away_xg: float,
        prediction: str,
        rng: np.random.Generator,
    ) -> Tuple[int, int]:
        eps = 1e-6
        ph = max(prob_home, eps)
        pa = max(prob_away, eps)
        pd = max(prob_draw, eps)

        base_total = base_home_xg + base_away_xg
        if base_total <= 0:
            base_total = 2.6

        strength_ratio = ph / pa if pa > 0 else 3.0
        strength_ratio = max(0.2, min(5.0, strength_ratio))

        draw_factor = 1.0 + pd * 0.5
        lambda_h = base_total * strength_ratio / (strength_ratio + 1.0) * draw_factor
        lambda_a = base_total / (strength_ratio + 1.0) * draw_factor

        lambda_h *= 1.05

        max_attempts = 50
        for _ in range(max_attempts):
            hg = int(rng.poisson(lambda_h))
            ag = int(rng.poisson(lambda_a))
            hg = max(0, min(hg, 6))
            ag = max(0, min(ag, 6))

            if (
                (prediction == "H" and hg > ag)
                or (prediction == "A" and ag > hg)
                or (prediction == "D" and hg == ag)
            ):
                return hg, ag

        hg, ag = int(round(base_home_xg)), int(round(base_away_xg))
        if prediction == "H" and hg <= ag:
            hg = ag + 1
        elif prediction == "A" and ag <= hg:
            ag = hg + 1
        elif prediction == "D" and hg != ag:
            ag = hg
        return max(0, hg), max(0, ag)

    def align_predicted_score(
        self,
        pred_home_goals: float,
        pred_away_goals: float,
        prediction: str,
        max_iter: int = 10,
    ) -> Tuple[int, int]:
        h = max(0.0, pred_home_goals)
        a = max(0.0, pred_away_goals)

        def score_matches(hi, ai):
            if hi > ai:
                return "H"
            if hi == ai:
                return "D"
            return "A"

        for _ in range(max_iter):
            hi = int(round(h))
            ai = int(round(a))
            if score_matches(hi, ai) == prediction:
                return hi, ai
            if prediction == "H" and hi <= ai:
                h += 0.15
                a = max(0.0, a - 0.05)
            elif prediction == "A" and hi >= ai:
                a += 0.15
                h = max(0.0, h - 0.05)
            elif prediction == "D":
                if hi > ai:
                    h -= 0.1
                    a += 0.1
                elif hi < ai:
                    h += 0.1
                    a -= 0.1
                else:
                    break
            else:
                break

        hi = int(round(h))
        ai = int(round(a))
        if prediction == "H" and hi <= ai:
            hi = ai + 1
        elif prediction == "A" and hi >= ai:
            ai = hi + 1
        elif prediction == "D" and hi != ai:
            ai = hi
        return max(0, hi), max(0, ai)

    def compute_goal_probabilities(self, lambda_home: float, lambda_away: float) -> dict:
        eps = 1e-9
        lh = max(lambda_home, eps)
        la = max(lambda_away, eps)

        def poisson_pmf(k, lam):
            return math.exp(-lam) * (lam ** k) / math.factorial(k)

        p_h0 = poisson_pmf(0, lh)
        p_h1 = poisson_pmf(1, lh)
        p_h2 = poisson_pmf(2, lh)
        p_a0 = poisson_pmf(0, la)
        p_a1 = poisson_pmf(1, la)
        p_a2 = poisson_pmf(2, la)

        prob_home_goals_0 = round(p_h0, 6)
        prob_home_goals_1plus = round(1.0 - p_h0, 6)
        prob_home_goals_2plus = round(1.0 - p_h0 - p_h1, 6)
        prob_away_goals_0 = round(p_a0, 6)
        prob_away_goals_1plus = round(1.0 - p_a0, 6)
        prob_away_goals_2plus = round(1.0 - p_a0 - p_a1, 6)
        prob_both_score = round((1.0 - p_h0) * (1.0 - p_a0), 6)

        p_over_1_5 = 1.0
        for h in range(0, 3):
            for a in range(0, 3):
                if h + a <= 1:
                    p_over_1_5 -= poisson_pmf(h, lh) * poisson_pmf(a, la)
        p_over_2_5 = 1.0
        for h in range(0, 4):
            for a in range(0, 4):
                if h + a <= 2:
                    p_over_2_5 -= poisson_pmf(h, lh) * poisson_pmf(a, la)
        p_over_3_5 = 1.0
        for h in range(0, 5):
            for a in range(0, 5):
                if h + a <= 3:
                    p_over_3_5 -= poisson_pmf(h, lh) * poisson_pmf(a, la)

        return {
            "prob_home_goals_0": prob_home_goals_0,
            "prob_home_goals_1plus": prob_home_goals_1plus,
            "prob_home_goals_2plus": prob_home_goals_2plus,
            "prob_away_goals_0": prob_away_goals_0,
            "prob_away_goals_1plus": prob_away_goals_1plus,
            "prob_away_goals_2plus": prob_away_goals_2plus,
            "prob_both_score": prob_both_score,
            "prob_over_1_5": round(p_over_1_5, 6),
            "prob_over_2_5": round(p_over_2_5, 6),
            "prob_over_3_5": round(p_over_3_5, 6),
        }

    # --- JSON Helpers ---
    def load_json(self, path: str) -> Any:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)

    def save_json(self, path: str, payload: Any) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, ensure_ascii=False)

    def load_json_if_exists(self, path: str) -> Optional[Any]:
        if not os.path.exists(path):
            return None
        try:
            return self.load_json(path)
        except Exception:
            return None

    def is_invalid_stat_value(self, value: Any) -> bool:
        if value is None:
            return True
        try:
            if pd.isna(value):
                return True
        except Exception:
            pass
        return value == -1

    def replace_nan_with_sentinel(self, value: Any, sentinel: int = -1) -> Any:
        if isinstance(value, dict):
            return {k: self.replace_nan_with_sentinel(v, sentinel=sentinel) for k, v in value.items()}
        if isinstance(value, list):
            return [self.replace_nan_with_sentinel(item, sentinel=sentinel) for item in value]
        try:
            if pd.isna(value):
                return sentinel
        except Exception:
            pass
        return value

    def clean_stats_dict(self, stats: Any) -> dict:
        if not isinstance(stats, dict):
            return {}
        cleaned = {}
        for key, value in stats.items():
            if self.is_invalid_stat_value(value):
                continue
            cleaned[key] = value
        return cleaned

    def coerce_feature_value(self, value: Any, default: float = 0.0) -> float:
        if self.is_invalid_stat_value(value):
            return float(default)
        return float(value)

    def data_fingerprint(self, season_files: list, processed_dir: Optional[str] = None) -> str:
        processed_dir = self.resolve_processed_dir(processed_dir)
        digest = hashlib.sha256()
        for rel_path in season_files:
            digest.update(rel_path.encode("utf-8"))
            full_path = os.path.join(processed_dir, rel_path)
            try:
                stat = os.stat(full_path)
                digest.update(str(stat.st_mtime_ns).encode("utf-8"))
                digest.update(str(stat.st_size).encode("utf-8"))
            except OSError:
                digest.update(b"missing")
        return digest.hexdigest()

    def per_game(self, stats: dict, total_key: str, games_key: str) -> float:
        games = stats.get(games_key, 0)
        total = stats.get(total_key, 0)
        if self.is_invalid_stat_value(games) or self.is_invalid_stat_value(total):
            return 0.0
        if not games:
            return 0.0
        return total / games

    def parse_season_start_year(self, file_name: str) -> Optional[int]:
        match = self.config.season_pattern.match(file_name)
        if not match:
            return None

        start_year = int(match.group(1))
        # If there is a group(2) for 2-digit end year (e.g. 2023-24)
        if match.lastindex and match.lastindex >= 2 and match.group(2):
            end_year_two_digits = int(match.group(2))
            if end_year_two_digits != (start_year + 1) % 100:
                return None

        if start_year < self.config.min_start_year:
            return None
        if start_year > datetime.now().year:
            return None
        return start_year

    def parse_start_year_from_key(self, season_key: str) -> int:
        file_name = os.path.basename(season_key) + ".csv"
        start = self.parse_season_start_year(file_name)
        return -1 if start is None else start

    def expected_current_latest_start_year(self, reference_date: Optional[datetime] = None) -> int:
        try:
            import season_calendar as sc
            return sc.european_season_start_year(reference_date)
        except Exception:
            ref = datetime.now() if reference_date is None else reference_date
            if ref.month > 7 or (ref.month == 7 and ref.day >= 15):
                return ref.year
            return ref.year - 1

    def season_recency_coefficient(self, latest_start_year: int, season_start_year: int) -> float:
        age = max(0, latest_start_year - season_start_year)
        if age <= 0:
            return 1.00
        if age == 1:
            return 0.92
        if age == 2:
            return 0.84
        if age == 3:
            return 0.76
        if age == 4:
            return 0.70
        return 0.60

    def choose_season_for_teams(self, home_team: str, away_team: str, season_teams: dict, fallback_season_key: str) -> str:
        candidates = []
        for season_key, teams in season_teams.items():
            if home_team in teams and away_team in teams:
                candidates.append(season_key)

        if not candidates:
            return fallback_season_key

        candidates.sort(key=self.parse_start_year_from_key)
        return candidates[-1]

    def find_latest_team_season_stats(self, team: str, season_teams: dict) -> dict:
        latest_key = None
        latest_year = -1
        for season_key, teams in season_teams.items():
            if team not in teams:
                continue
            year = self.parse_start_year_from_key(season_key)
            if year > latest_year:
                latest_year = year
                latest_key = season_key
        if latest_key is None:
            return {}
        return season_teams.get(latest_key, {}).get(team, {})

    def team_form(self, team: str, current_form: dict) -> Tuple[float, float, float, float]:
        stats = self.clean_stats_dict(current_form.get("teams", {}).get(team, {}))
        points_10 = float(stats.get("points_last_10", 0.0) or 0.0)
        wins_10 = float(stats.get("wins_last_10", 0.0) or 0.0)
        losses_10 = float(stats.get("losses_last_10", 0.0) or 0.0)
        avg_for_10 = float(stats.get("avg_goals_for_last_10", 0.0) or 0.0)
        avg_against_10 = float(stats.get("avg_goals_against_last_10", 0.0) or 0.0)
        ppg_10 = points_10 / 10.0
        form_index = (wins_10 - losses_10) / 10.0
        return ppg_10, form_index, avg_for_10, avg_against_10

    def build_season_position_map(self, season_lookup: dict) -> dict:
        if not isinstance(season_lookup, dict):
            return {}
        teams = list(season_lookup.keys())
        ranked = sorted(
            teams,
            key=lambda t: (
                -float(season_lookup.get(t, {}).get("points", 0.0) or 0.0),
                -float(season_lookup.get(t, {}).get("goal_difference", 0.0) or 0.0),
                -float(season_lookup.get(t, {}).get("goals_scored", 0.0) or 0.0),
                t,
            ),
        )
        return {team: idx + 1 for idx, team in enumerate(ranked)}

    def split_season_key(self, season_key: str) -> Tuple[str, str]:
        if "/" not in season_key:
            return "Unknown", season_key
        parts = season_key.split("/")
        return "/".join(parts[:-1]), parts[-1]

    def build_latest_competition_tables(self, season_teams: dict) -> dict:
        latest_by_comp = {}
        for season_key in season_teams.keys():
            competition, _ = self.split_season_key(season_key)
            year = self.parse_start_year_from_key(season_key)
            current = latest_by_comp.get(competition)
            if current is None or year > current[0]:
                latest_by_comp[competition] = (year, season_key)

        tables = {}
        for competition, (_, season_key) in latest_by_comp.items():
            season_lookup = season_teams.get(season_key, {})
            positions = self.build_season_position_map(season_lookup)
            tables[competition] = {
                "season_key": season_key,
                "positions": positions,
                "size": len(positions),
            }
        return tables

    def build_competition_offsets(self, league_strength: dict, competition_tables: dict) -> dict:
        competitions = list(competition_tables.keys())
        competitions.sort(
            key=lambda comp: (-float(league_strength.get(comp, 0.85)), comp)
        )
        offsets = {}
        running = 0
        for comp in competitions:
            offsets[comp] = running
            running += int(competition_tables.get(comp, {}).get("size", 0))
        return offsets

    def build_dynamic_form_from_matches(self, matches: pd.DataFrame) -> dict:
        ordered_rows = []
        for idx, row in matches.iterrows():
            season_year = self.parse_start_year_from_key(row["season_key"])
            ordered_rows.append((season_year, idx, row))
        ordered_rows.sort(key=lambda item: (item[0], item[1]))

        team_history = defaultdict(list)

        for _, _, row in ordered_rows:
            home = row["HomeTeam"]
            away = row["AwayTeam"]
            hg = float(row["FTHG"])
            ag = float(row["FTAG"])
            result = row["FTR"]

            avg_h = float(row.get("AvgH", 0.0) or 0.0)
            avg_d = float(row.get("AvgD", 0.0) or 0.0)
            avg_a = float(row.get("AvgA", 0.0) or 0.0)

            if result == "H":
                home_res, away_res = "W", "L"
            elif result == "A":
                home_res, away_res = "L", "W"
            else:
                home_res, away_res = "D", "D"

            team_history[home].append(
                {"result": home_res, "gf": hg, "ga": ag, "win_odds": avg_h, "draw_odds": avg_d, "lose_odds": avg_a}
            )
            team_history[away].append(
                {"result": away_res, "gf": ag, "ga": hg, "win_odds": avg_a, "draw_odds": avg_d, "lose_odds": avg_h}
            )

        dynamic_form = {}
        for team, history in team_history.items():
            recent = history[-10:]
            wins = sum(1 for match in recent if match["result"] == "W")
            draws = sum(1 for match in recent if match["result"] == "D")
            losses = sum(1 for match in recent if match["result"] == "L")
            points = wins * 3 + draws
            gf = sum(match["gf"] for match in recent)
            ga = sum(match["ga"] for match in recent)
            n = len(recent) if recent else 1
            last = history[-1]
            dynamic_form[team] = {
                "form_last_10": "".join(match["result"] for match in recent),
                "wins_last_10": wins,
                "draws_last_10": draws,
                "losses_last_10": losses,
                "points_last_10": points,
                "avg_goals_for_last_10": round(gf / n, 2),
                "avg_goals_against_last_10": round(ga / n, 2),
                "previous_match_win_odds": last["win_odds"],
                "previous_match_draw_odds": last["draw_odds"],
                "previous_match_lose_odds": last["lose_odds"],
            }

        return dynamic_form

    def build_fallback_data(self, matches: pd.DataFrame, season_files: list) -> Tuple[dict, dict, dict, dict]:
        overall_teams = defaultdict(
            lambda: {
                "games": 0,
                "goals_scored": 0,
                "goals_conceded": 0,
                "home_games": 0,
                "away_games": 0,
                "home_goals_scored": 0,
                "away_goals_scored": 0,
            }
        )
        season_teams = defaultdict(lambda: defaultdict(lambda: {"games": 0, "points": 0}))
        head_to_head = defaultdict(
            lambda: defaultdict(lambda: {"games": 0, "wins": 0, "goals_scored": 0, "goals_conceded": 0})
        )

        for _, row in matches.iterrows():
            season_key = row["season_key"]
            home = row["HomeTeam"]
            away = row["AwayTeam"]
            hg = float(row["FTHG"])
            ag = float(row["FTAG"])
            result = row["FTR"]

            overall_teams[home]["games"] += 1
            overall_teams[home]["goals_scored"] += hg
            overall_teams[home]["goals_conceded"] += ag
            overall_teams[home]["home_games"] += 1
            overall_teams[home]["home_goals_scored"] += hg

            overall_teams[away]["games"] += 1
            overall_teams[away]["goals_scored"] += ag
            overall_teams[away]["goals_conceded"] += hg
            overall_teams[away]["away_games"] += 1
            overall_teams[away]["away_goals_scored"] += ag

            season_teams[season_key][home]["games"] += 1
            season_teams[season_key][away]["games"] += 1

            head_to_head[home][away]["games"] += 1
            head_to_head[home][away]["goals_scored"] += hg
            head_to_head[home][away]["goals_conceded"] += ag
            head_to_head[away][home]["games"] += 1
            head_to_head[away][home]["goals_scored"] += ag
            head_to_head[away][home]["goals_conceded"] += hg

            if result == "H":
                season_teams[season_key][home]["points"] += 3
                head_to_head[home][away]["wins"] += 1
            elif result == "A":
                season_teams[season_key][away]["points"] += 3
                head_to_head[away][home]["wins"] += 1
            else:
                season_teams[season_key][home]["points"] += 1
                season_teams[season_key][away]["points"] += 1

        for _, stats in overall_teams.items():
            games = max(stats["games"], 1)
            stats["avg_goals_scored"] = stats["goals_scored"] / games
            stats["avg_goals_conceded"] = stats["goals_conceded"] / games

        latest_key = season_files[-1].replace(".csv", "")
        current_form = {"season": latest_key, "teams": {}}
        latest_matches = matches[matches["season_key"] == latest_key]
        last_odds = {}
        for _, row in latest_matches.iterrows():
            home = row["HomeTeam"]
            away = row["AwayTeam"]
            last_odds[home] = {
                "previous_match_win_odds": float(row.get("AvgH", 0.0) or 0.0),
                "previous_match_draw_odds": float(row.get("AvgD", 0.0) or 0.0),
                "previous_match_lose_odds": float(row.get("AvgA", 0.0) or 0.0),
            }
            last_odds[away] = {
                "previous_match_win_odds": float(row.get("AvgA", 0.0) or 0.0),
                "previous_match_draw_odds": float(row.get("AvgD", 0.0) or 0.0),
                "previous_match_lose_odds": float(row.get("AvgH", 0.0) or 0.0),
            }
        current_form["teams"] = last_odds

        return (
            dict(overall_teams),
            {k: dict(v) for k, v in season_teams.items()},
            {k: dict(v) for k, v in head_to_head.items()},
            current_form,
        )

    # --- Feature Engineering ---
    def build_features(
        self,
        match_df: pd.DataFrame,
        season_key: str,
        competition_key: str,
        season_coeff: float,
        overall_teams: dict,
        season_teams: dict,
        head_to_head: dict,
        current_form: dict,
        league_strength: dict,
        home_competition_override: Optional[str] = None,
        away_competition_override: Optional[str] = None,
    ) -> pd.DataFrame:
        season_lookup = season_teams.get(season_key, {})
        season_positions = self.build_season_position_map(season_lookup)
        form_lookup = current_form.get("teams", {})
        rows = []
        previous_odds = {}
        default_strength = float(league_strength.get(competition_key, 0.85))

        for _, match in match_df.iterrows():
            home = match["HomeTeam"]
            away = match["AwayTeam"]

            home_overall = self.clean_stats_dict(overall_teams.get(home, {}))
            away_overall = self.clean_stats_dict(overall_teams.get(away, {}))
            home_season = self.clean_stats_dict(season_lookup.get(home, {}))
            away_season = self.clean_stats_dict(season_lookup.get(away, {}))
            if not home_season:
                home_season = self.clean_stats_dict(self.find_latest_team_season_stats(home, season_teams))
            if not away_season:
                away_season = self.clean_stats_dict(self.find_latest_team_season_stats(away, season_teams))
            h2h_home = self.clean_stats_dict(head_to_head.get(home, {}).get(away, {}))
            home_competition = home_competition_override or competition_key
            away_competition = away_competition_override or competition_key
            home_strength = float(league_strength.get(home_competition, default_strength))
            away_strength = float(league_strength.get(away_competition, default_strength))
            home_points_before = match.get("HomePointsBefore", home_season.get("points", 0.0))
            away_points_before = match.get("AwayPointsBefore", away_season.get("points", 0.0))
            home_pos_before = match.get("HomeLeaguePosBefore", season_positions.get(home, 0.0))
            away_pos_before = match.get("AwayLeaguePosBefore", season_positions.get(away, 0.0))
            home_points_before = self.coerce_feature_value(home_points_before, 0.0)
            away_points_before = self.coerce_feature_value(away_points_before, 0.0)
            home_pos_before = self.coerce_feature_value(home_pos_before, 0.0)
            away_pos_before = self.coerce_feature_value(away_pos_before, 0.0)

            home_prev = previous_odds.get(home)
            away_prev = previous_odds.get(away)
            home_ppg10, home_form_idx, home_form_gf10, home_form_ga10 = self.team_form(home, current_form)
            away_ppg10, away_form_idx, away_form_gf10, away_form_ga10 = self.team_form(away, current_form)

            if home_prev is None:
                form_home = self.clean_stats_dict(form_lookup.get(home, {}))
                home_prev = {
                    "win": float(form_home.get("previous_match_win_odds") or 0.0),
                    "draw": float(form_home.get("previous_match_draw_odds") or 0.0),
                    "lose": float(form_home.get("previous_match_lose_odds") or 0.0),
                }

            if away_prev is None:
                form_away = self.clean_stats_dict(form_lookup.get(away, {}))
                away_prev = {
                    "win": float(form_away.get("previous_match_win_odds") or 0.0),
                    "draw": float(form_away.get("previous_match_draw_odds") or 0.0),
                    "lose": float(form_away.get("previous_match_lose_odds") or 0.0),
                }

            avg_h = match.get("AvgH", 0.0)
            avg_d = match.get("AvgD", 0.0)
            avg_a = match.get("AvgA", 0.0)

            avg_h = 0.0 if pd.isna(avg_h) else float(avg_h)
            avg_d = 0.0 if pd.isna(avg_d) else float(avg_d)
            avg_a = 0.0 if pd.isna(avg_a) else float(avg_a)

            rows.append(
                {
                    "home_overall_avg_goals": home_overall.get("avg_goals_scored", 0.0),
                    "away_overall_avg_goals": away_overall.get("avg_goals_scored", 0.0),
                    "home_overall_avg_conceded": home_overall.get("avg_goals_conceded", 0.0),
                    "away_overall_avg_conceded": away_overall.get("avg_goals_conceded", 0.0),
                    "home_home_goals_per_game": self.per_game(home_overall, "home_goals_scored", "home_games"),
                    "away_away_goals_per_game": self.per_game(away_overall, "away_goals_scored", "away_games"),
                    "home_avg_home_goals_scored": home_overall.get("avg_home_goals_scored", 0.0),
                    "home_avg_home_goals_conceded": home_overall.get("avg_home_goals_conceded", 0.0),
                    "away_avg_away_goals_scored": away_overall.get("avg_away_goals_scored", 0.0),
                    "away_avg_away_goals_conceded": away_overall.get("avg_away_goals_conceded", 0.0),
                    "home_avg_home_shots_for": home_overall.get("avg_home_shots_for", 0.0),
                    "home_avg_home_shots_against": home_overall.get("avg_home_shots_against", 0.0),
                    "away_avg_away_shots_for": away_overall.get("avg_away_shots_for", 0.0),
                    "away_avg_away_shots_against": away_overall.get("avg_away_shots_against", 0.0),
                    "home_season_points_per_game": self.per_game(home_season, "points", "games"),
                    "away_season_points_per_game": self.per_game(away_season, "points", "games"),
                    "home_season_avg_goals_scored": home_season.get("avg_goals_scored", 0.0),
                    "home_season_avg_goals_conceded": home_season.get("avg_goals_conceded", 0.0),
                    "away_season_avg_goals_scored": away_season.get("avg_goals_scored", 0.0),
                    "away_season_avg_goals_conceded": away_season.get("avg_goals_conceded", 0.0),
                    "home_season_avg_home_goals_scored": home_season.get("avg_home_goals_scored", 0.0),
                    "home_season_avg_home_goals_conceded": home_season.get("avg_home_goals_conceded", 0.0),
                    "away_season_avg_away_goals_scored": away_season.get("avg_away_goals_scored", 0.0),
                    "away_season_avg_away_goals_conceded": away_season.get("avg_away_goals_conceded", 0.0),
                    "home_season_avg_home_shots_for": home_season.get("avg_home_shots_for", 0.0),
                    "home_season_avg_home_shots_against": home_season.get("avg_home_shots_against", 0.0),
                    "away_season_avg_away_shots_for": away_season.get("avg_away_shots_for", 0.0),
                    "away_season_avg_away_shots_against": away_season.get("avg_away_shots_against", 0.0),
                    "h2h_home_win_rate": self.per_game(h2h_home, "wins", "games"),
                    "h2h_goal_diff_per_game": self.per_game(h2h_home, "goals_scored", "games")
                    - self.per_game(h2h_home, "goals_conceded", "games"),
                    "h2h_weighted_goal_diff": (
                        h2h_home.get("weighted_avg_goals_scored", 0.0) - h2h_home.get("weighted_avg_goals_conceded", 0.0)
                    ),
                    "h2h_weighted_shot_diff": (
                        h2h_home.get("weighted_avg_shots_for", 0.0) - h2h_home.get("weighted_avg_shots_against", 0.0)
                    ),
                    "home_prev_win_odds": home_prev["win"],
                    "home_prev_draw_odds": home_prev["draw"],
                    "home_prev_lose_odds": home_prev["lose"],
                    "away_prev_win_odds": away_prev["win"],
                    "away_prev_draw_odds": away_prev["draw"],
                    "away_prev_lose_odds": away_prev["lose"],
                    "home_league_strength": home_strength,
                    "away_league_strength": away_strength,
                    "league_strength_diff": home_strength - away_strength,
                    "home_points_before": home_points_before,
                    "away_points_before": away_points_before,
                    "points_before_diff": home_points_before - away_points_before,
                    "home_league_pos_before": home_pos_before,
                    "away_league_pos_before": away_pos_before,
                    "league_pos_before_diff": away_pos_before - home_pos_before,
                    "season_coeff": season_coeff,
                    "home_adj_points_per_game": self.per_game(home_season, "points", "games") * home_strength * season_coeff,
                    "away_adj_points_per_game": self.per_game(away_season, "points", "games") * away_strength * season_coeff,
                    "home_adj_avg_goals_scored": home_overall.get("weighted_avg_goals_scored", home_overall.get("avg_goals_scored", 0.0))
                    * home_strength,
                    "away_adj_avg_goals_scored": away_overall.get("weighted_avg_goals_scored", away_overall.get("avg_goals_scored", 0.0))
                    * away_strength,
                    "home_form_ppg_10": home_ppg10,
                    "away_form_ppg_10": away_ppg10,
                    "form_ppg_diff_10": home_ppg10 - away_ppg10,
                    "home_form_index_10": home_form_idx,
                    "away_form_index_10": away_form_idx,
                    "form_index_diff_10": home_form_idx - away_form_idx,
                    "home_form_avg_goals_for_10": home_form_gf10,
                    "away_form_avg_goals_for_10": away_form_gf10,
                    "home_form_avg_goals_against_10": home_form_ga10,
                    "away_form_avg_goals_against_10": away_form_ga10,
                    "competition": competition_key,
                }
            )

            previous_odds[home] = {"win": avg_h, "draw": avg_d, "lose": avg_a}
            previous_odds[away] = {"win": avg_a, "draw": avg_d, "lose": avg_h}

        return pd.DataFrame(rows)

    def load_training_matches(self, processed_dir: Optional[str] = None) -> Tuple[pd.DataFrame, list]:
        processed_dir = self.resolve_processed_dir(processed_dir)
        frames = []
        valid_files = []

        for root, _, files in os.walk(processed_dir):
            for name in files:
                if not name.endswith(".csv"):
                    continue
                start_year = self.parse_season_start_year(name)
                if start_year is not None:
                    rel_path = os.path.relpath(os.path.join(root, name), processed_dir)
                    valid_files.append((start_year, rel_path))

        valid_files.sort(key=lambda item: item[0])
        season_files = [name for _, name in valid_files]
        latest_start_year = valid_files[-1][0] if valid_files else self.config.min_start_year

        for start_year, rel_path in valid_files:
            path = os.path.join(processed_dir, rel_path)
            df = pd.read_csv(path)

            for col in ["AvgH", "AvgD", "AvgA"]:
                if col not in df.columns:
                    df[col] = 0.0
            for col in ["HomePointsBefore", "AwayPointsBefore", "HomeLeaguePosBefore", "AwayLeaguePosBefore"]:
                if col not in df.columns:
                    df[col] = 0.0
            for col in ["HS", "AS", "HST", "AST"]:
                if col not in df.columns:
                    df[col] = -1
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(-1)

            required_cols = ["HomeTeam", "AwayTeam", "FTR", "FTHG", "FTAG"]
            missing_required = [col for col in required_cols if col not in df.columns]
            if missing_required:
                continue

            clean_df = df[required_cols + ["AvgH", "AvgD", "AvgA", "HS", "AS", "HST", "AST", "HomePointsBefore", "AwayPointsBefore", "HomeLeaguePosBefore", "AwayLeaguePosBefore"]].dropna(subset=required_cols).copy()
            clean_df = clean_df[clean_df["FTR"].isin(["H", "D", "A"])].copy()

            season_key = rel_path.replace(".csv", "").replace("\\", "/")
            clean_df["season_key"] = season_key
            competition_name = os.path.dirname(rel_path).replace("\\", "/")
            clean_df["competition"] = competition_name if competition_name else "Unknown"
            clean_df["season_coeff"] = self.season_recency_coefficient(latest_start_year, start_year)

            frames.append(clean_df)

        if not frames:
            raise ValueError(f"No valid match data found in {processed_dir}")

        matches = pd.concat(frames, ignore_index=True)
        return matches, season_files

    # --- Team Name Resolution & Mapping ---
    def _load_name_mapping(self) -> dict:
        if self._name_mapping_cache is not None:
            return self._name_mapping_cache

        flat = {}
        mapping_file = self.config.mapping_file
        if not os.path.isfile(mapping_file):
            self._name_mapping_cache = flat
            return flat

        def _norm_key(text):
            t = str(text or "").lower().strip()
            t = t.replace("&", "and")
            return re.sub(r"[^a-z0-9]+", "", t)

        try:
            with open(mapping_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            self._name_mapping_cache = flat
            return flat

        for canon, aliases in data.items():
            flat[canon.lower()] = canon
            norm_c = _norm_key(canon)
            if norm_c:
                flat.setdefault(norm_c, canon)
            for alias in aliases:
                flat[alias.lower()] = canon
                norm_a = _norm_key(alias)
                if norm_a:
                    flat.setdefault(norm_a, canon)
        self._name_mapping_cache = flat
        return flat

    def resolve_team_name(self, raw_name: Any, valid_names: Any) -> Optional[str]:
        if not raw_name:
            return None

        def normalize(name):
            try:
                import team_mapping_groups as tmg
                return tmg.normalize_team_key(name)
            except Exception:
                name = str(name).lower().strip()
                name = name.replace("&", "and")
                return re.sub(r"[^a-z0-9]+", "", name)

        valid_list = [str(t) for t in (valid_names or []) if str(t).strip()]
        if not valid_list:
            return None
        valid_set = set(valid_list)
        alias_map = {normalize(team): team for team in valid_list}

        def from_candidate(candidate):
            if not candidate:
                return None
            text = str(candidate).strip()
            if text in valid_set:
                return text
            return alias_map.get(normalize(text))

        mapping = self._load_name_mapping()
        raw = str(raw_name).strip()
        lower = raw.lower()

        for key in (lower, re.sub(r"[^a-z0-9]+", "", lower.replace("&", "and"))):
            hit = from_candidate(mapping.get(key))
            if hit:
                return hit

        hit = from_candidate(raw)
        if hit:
            return hit

        direct = alias_map.get(normalize(raw))
        if direct:
            return direct

        stripped = re.sub(r"\s+(AFC|FC)\s*$", "", raw, flags=re.IGNORECASE).strip()
        if stripped and stripped.lower() != lower:
            for key in (stripped.lower(), re.sub(r"[^a-z0-9]+", "", stripped.lower().replace("&", "and"))):
                hit = from_candidate(mapping.get(key))
                if hit:
                    return hit
            hit = from_candidate(stripped) or alias_map.get(normalize(stripped))
            if hit:
                return hit

        key = normalize(raw)
        candidates = [team for team in valid_list if key and key in normalize(team)]
        if len(candidates) == 1:
            return candidates[0]

        return None

    def build_match_input(self, home_team: str, away_team: str) -> pd.DataFrame:
        return pd.DataFrame(
            [{"HomeTeam": home_team, "AwayTeam": away_team, "FTR": "D", "AvgH": 0.0, "AvgD": 0.0, "AvgA": 0.0}]
        )

    def probabilities_to_odds(self, probabilities: dict) -> dict:
        odds = {}
        for key in ["H", "D", "A"]:
            p = max(float(probabilities.get(key, 0.0)), 1e-6)
            odds[key] = round(1.0 / p, 2)
        return odds

    def format_percent_text(self, probability: float) -> str:
        pct = max(0.0, float(probability)) * 100.0
        if 0.0 < pct < 1.0:
            return "<1%"
        return f"{pct:.1f}%"

    def prediction_randomizer_seed(self, home_team: str, away_team: str, competition_key: str, season_key: str = "") -> int:
        payload = "||".join(
            [
                str(home_team).strip().lower(),
                str(away_team).strip().lower(),
                str(competition_key).strip().lower(),
                str(season_key).strip().lower(),
            ]
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return int(digest[:16], 16)

    def apply_probability_randomizer(self, probabilities: dict, max_delta: float, seed: Optional[int] = None) -> dict:
        h = max(0.0, float(probabilities.get("H", 0.0)))
        d = max(0.0, float(probabilities.get("D", 0.0)))
        a = max(0.0, float(probabilities.get("A", 0.0)))
        total = h + d + a
        if total <= 0:
            return {"H": 0.0, "D": 0.0, "A": 0.0}
        h /= total
        d /= total
        a /= total

        rng = random.Random(seed) if seed is not None else random
        delta = rng.uniform(-max_delta, max_delta)
        h = max(0.0, min(1.0, h + delta))
        a = max(0.0, min(1.0, a - delta))
        rem = max(0.0, 1.0 - (h + a))
        d_target = max(0.0, min(1.0, d + rng.uniform(-max_delta * 0.35, max_delta * 0.35)))
        d = min(rem, d_target)
        spill = max(0.0, rem - d)
        denom = h + a
        if denom > 0:
            h += spill * (h / denom)
            a += spill * (a / denom)
        else:
            h = spill * 0.5
            a = spill * 0.5
        norm = h + d + a
        if norm <= 0:
            return {"H": 1 / 3, "D": 1 / 3, "A": 1 / 3}
        return {"H": h / norm, "D": d / norm, "A": a / norm}

    def reduce_draw_probability(self, probabilities: dict, reduction_factor: Optional[float] = None) -> dict:
        reduction_factor = reduction_factor if reduction_factor is not None else self.config.draw_reduction_factor
        h = max(0.0, float(probabilities.get("H", 0.0)))
        d = max(0.0, float(probabilities.get("D", 0.0)))
        a = max(0.0, float(probabilities.get("A", 0.0)))
        total = h + d + a
        if total <= 0:
            return {"H": 1 / 3, "D": 1 / 3, "A": 1 / 3}
        h /= total
        d /= total
        a /= total

        r = max(0.0, min(0.6, float(reduction_factor)))
        if d > self.config.high_draw_threshold:
            extra_ratio = min(1.0, (d - self.config.high_draw_threshold) / max(1e-6, (1.0 - self.config.high_draw_threshold)))
            r += self.config.high_draw_extra_reduction_max * extra_ratio
        r = min(0.6, r)
        reduced_d = d * (1.0 - r)
        carry = d - reduced_d
        d = reduced_d
        h_a_total = h + a
        if h_a_total > 0:
            h += carry * (h / h_a_total)
            a += carry * (a / h_a_total)
        else:
            h += carry * 0.5
            a += carry * 0.5

        norm = h + d + a
        if norm <= 0:
            return {"H": 1 / 3, "D": 1 / 3, "A": 1 / 3}
        return {"H": h / norm, "D": d / norm, "A": a / norm}

    # --- Regional Specific Adjustments (MLS & Extra) ---
    def apply_home_advantage_boost(self, probabilities: dict, boost: Optional[float] = None) -> dict:
        boost = boost if boost is not None else self.config.home_extra_boost
        h = max(0.0, float(probabilities.get("H", 0.0)))
        d = max(0.0, float(probabilities.get("D", 0.0)))
        a = max(0.0, float(probabilities.get("A", 0.0)))
        total = h + d + a
        if total <= 0:
            return {"H": 1 / 3, "D": 1 / 3, "A": 1 / 3}
        h /= total
        d /= total
        a /= total

        transfer = min(max(0.0, float(boost)), a)
        h += transfer
        a -= transfer
        norm = h + d + a
        if norm <= 0:
            return {"H": 1 / 3, "D": 1 / 3, "A": 1 / 3}
        return {"H": h / norm, "D": d / norm, "A": a / norm}

    def mls_home_advantage_shift(self, home_team: str, prediction_season: str, season_teams: dict) -> float:
        season_lookup = season_teams.get(prediction_season, {}) if isinstance(season_teams, dict) else {}
        home_stats = self.clean_stats_dict(season_lookup.get(home_team, {})) or self.clean_stats_dict(
            self.find_latest_team_season_stats(home_team, season_teams)
        )
        home_gd = float(home_stats.get("avg_home_goals_scored", 0.0)) - float(home_stats.get("avg_home_goals_conceded", 0.0))
        shot_edge = float(home_stats.get("avg_home_shots_for", 0.0)) - float(home_stats.get("avg_home_shots_against", 0.0))
        home_strength_signal = home_gd + 0.03 * shot_edge
        form_factor = 0.5 + 0.5 * math.tanh(home_strength_signal)
        blend = 0.55 * random.random() + 0.45 * form_factor
        shift = self.config.mls_home_adv_min_shift + (self.config.mls_home_adv_max_shift - self.config.mls_home_adv_min_shift) * blend
        return max(self.config.mls_home_adv_min_shift, min(self.config.mls_home_adv_max_shift, shift))

    def market_value_team_score(self, team_name: str, market_value_data: dict) -> float:
        teams = (market_value_data or {}).get("teams", {})
        team_entry = teams.get(team_name, {})
        return float(team_entry.get("squad_value_eur_m", 0) or 0)

    def market_value_probability_shift(self, home_team: str, away_team: str, market_value_data: dict) -> Tuple[float, float, float]:
        home_score = self.market_value_team_score(home_team, market_value_data)
        away_score = self.market_value_team_score(away_team, market_value_data)
        total = home_score + away_score
        if total <= 0:
            return 0.0, home_score, away_score
        edge = (home_score - away_score) / total
        shift = max(-self.config.mls_market_shift_max, min(self.config.mls_market_shift_max, self.config.mls_market_shift_scale * edge))
        return shift, home_score, away_score

    def apply_league_strength_adjustment(self, probabilities: dict, home_strength: float, away_strength: float) -> Tuple[dict, float, float]:
        h = max(0.0, float(probabilities.get("H", 0.0)))
        d = max(0.0, float(probabilities.get("D", 0.0)))
        a = max(0.0, float(probabilities.get("A", 0.0)))
        total = h + d + a
        if total <= 0:
            return {"H": 1 / 3, "D": 1 / 3, "A": 1 / 3}, 1.0, 0.0
        h /= total
        d /= total
        a /= total

        strength_delta = float(home_strength) - float(away_strength)
        strength_gap = abs(strength_delta)
        draw_scale = max(0.82, 1.0 - 0.35 * strength_gap)
        old_draw = d
        d = old_draw * draw_scale
        carry = old_draw - d
        h_a_total = h + a
        if h_a_total > 0:
            h += carry * (h / h_a_total)
            a += carry * (a / h_a_total)
        else:
            h += carry * 0.5
            a += carry * 0.5

        league_direction_shift = max(-0.035, min(0.035, 0.18 * strength_delta * self.config.mls_adjustment_weight))
        if league_direction_shift > 0:
            transfer = min(league_direction_shift, a)
            h += transfer
            a -= transfer
        elif league_direction_shift < 0:
            transfer = min(abs(league_direction_shift), h)
            a += transfer
            h -= transfer

        norm = h + d + a
        if norm <= 0:
            return {"H": 1 / 3, "D": 1 / 3, "A": 1 / 3}, draw_scale, league_direction_shift
        return {"H": h / norm, "D": d / norm, "A": a / norm}, draw_scale, league_direction_shift

    # --- Model Training ---
    def train_result_model(self, X_train: pd.DataFrame, y_train: pd.Series):
        label_encoder = LabelEncoder()
        y_train_enc = label_encoder.fit_transform(y_train)

        if not self.config.use_ensemble_classifier:
            # European-style: XGBoost (GPU -> CPU) or RandomForestClassifier(220)
            if XGBClassifier is not None:
                try:
                    model = XGBClassifier(
                        n_estimators=500,
                        max_depth=8,
                        learning_rate=0.05,
                        subsample=0.9,
                        colsample_bytree=0.9,
                        objective="multi:softprob",
                        num_class=len(label_encoder.classes_),
                        eval_metric="mlogloss",
                        random_state=42,
                        tree_method="hist",
                        device="cuda",
                        n_jobs=MODEL_THREADS,
                    )
                    model.fit(X_train, y_train_enc)
                    return model, label_encoder, "xgboost-gpu"
                except Exception:
                    try:
                        model = XGBClassifier(
                            n_estimators=500,
                            max_depth=8,
                            learning_rate=0.05,
                            subsample=0.9,
                            colsample_bytree=0.9,
                            objective="multi:softprob",
                            num_class=len(label_encoder.classes_),
                            eval_metric="mlogloss",
                            random_state=42,
                            tree_method="hist",
                            device="cpu",
                            n_jobs=MODEL_THREADS,
                        )
                        model.fit(X_train, y_train_enc)
                        return model, label_encoder, "xgboost-cpu"
                    except Exception:
                        pass

            model = RandomForestClassifier(n_estimators=220, random_state=42, n_jobs=MODEL_THREADS)
            model.fit(X_train, y_train_enc)
            return model, label_encoder, "random-forest-cpu"

        # MLS & Extra: Ensemble of XGB (if present) + RandomForest(260) + ExtraTrees(320)
        trained_models = []
        backends = []

        if XGBClassifier is not None:
            try:
                model = XGBClassifier(
                    n_estimators=420,
                    max_depth=7,
                    learning_rate=0.05,
                    subsample=0.9,
                    colsample_bytree=0.9,
                    objective="multi:softprob",
                    num_class=len(label_encoder.classes_),
                    eval_metric="mlogloss",
                    random_state=42,
                    tree_method="hist",
                    device="cuda",
                    n_jobs=MODEL_THREADS,
                )
                model.fit(X_train, y_train_enc)
                trained_models.append(model)
                backends.append("xgboost-gpu")
            except Exception:
                try:
                    model = XGBClassifier(
                        n_estimators=420,
                        max_depth=7,
                        learning_rate=0.05,
                        subsample=0.9,
                        colsample_bytree=0.9,
                        objective="multi:softprob",
                        num_class=len(label_encoder.classes_),
                        eval_metric="mlogloss",
                        random_state=42,
                        tree_method="hist",
                        device="cpu",
                        n_jobs=MODEL_THREADS,
                    )
                    model.fit(X_train, y_train_enc)
                    trained_models.append(model)
                    backends.append("xgboost-cpu")
                except Exception:
                    pass

        rf_model = RandomForestClassifier(n_estimators=260, random_state=42, n_jobs=MODEL_THREADS)
        rf_model.fit(X_train, y_train_enc)
        trained_models.append(rf_model)
        backends.append("random-forest")

        et_model = ExtraTreesClassifier(n_estimators=320, random_state=42, n_jobs=MODEL_THREADS)
        et_model.fit(X_train, y_train_enc)
        trained_models.append(et_model)
        backends.append("extra-trees")

        if len(trained_models) == 1:
            return trained_models[0], label_encoder, backends[0]

        ensemble = AveragedProbaClassifier(trained_models)
        return ensemble, label_encoder, "ensemble(" + "+".join(backends) + ")"

    def train_regression_model(self, X_train: pd.DataFrame, y_train: pd.Series, random_state: int):
        if XGBRegressor is not None:
            try:
                model = XGBRegressor(
                    n_estimators=450,
                    max_depth=8,
                    learning_rate=0.05,
                    subsample=0.9,
                    colsample_bytree=0.9,
                    objective="reg:squarederror",
                    eval_metric="rmse",
                    random_state=random_state,
                    tree_method="hist",
                    device="cuda",
                    n_jobs=MODEL_THREADS,
                )
                model.fit(X_train, y_train)
                return model
            except Exception:
                try:
                    model = XGBRegressor(
                        n_estimators=450,
                        max_depth=8,
                        learning_rate=0.05,
                        subsample=0.9,
                        colsample_bytree=0.9,
                        objective="reg:squarederror",
                        eval_metric="rmse",
                        random_state=random_state,
                        tree_method="hist",
                        device="cpu",
                        n_jobs=MODEL_THREADS,
                    )
                    model.fit(X_train, y_train)
                    return model
                except Exception:
                    pass

        model = RandomForestRegressor(n_estimators=160, random_state=random_state, n_jobs=MODEL_THREADS)
        model.fit(X_train, y_train)
        return model

    def train_all_regressors(self, X: pd.DataFrame, targets: list) -> dict:
        results = {}
        with ThreadPoolExecutor(max_workers=TRAIN_WORKERS) as pool:
            future_map = {
                pool.submit(self.train_regression_model, X, series, seed): key
                for key, series, seed in targets
            }
            for future in as_completed(future_map):
                key = future_map[future]
                results[key] = future.result()
        return results

    def train_goal_prob_model(self, X_train: pd.DataFrame, y_train: pd.Series, random_state: int):
        model = make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=2000, random_state=random_state),
        )
        model.fit(X_train, y_train)
        return model

    def train_all_goal_prob_models(self, X: pd.DataFrame, matches: pd.DataFrame) -> dict:
        hg = matches["FTHG"]
        ag = matches["FTAG"]
        models = {}
        for name, fn in GOAL_PROB_TARGETS:
            models[name] = self.train_goal_prob_model(X, fn(hg, ag), hash(name) % (2**31))
        return models

    def predict_goal_probabilities(self, X_match: pd.DataFrame, goal_prob_models: dict) -> dict:
        result = {}
        for (name, _), result_key in zip(GOAL_PROB_TARGETS, GOAL_PROB_RESULT_KEYS):
            model = goal_prob_models[name]
            proba = model.predict_proba(X_match)[0]
            classes = list(model.named_steps["logisticregression"].classes_)
            pos_idx = classes.index(1) if 1 in classes else -1
            result[result_key] = round(float(proba[pos_idx]), 4) if pos_idx >= 0 else 0.0
        return result

    # --- Interactive and CLI Execution ---
    def main(self) -> None:
        processed_dir = self.resolve_processed_dir()
        team_data_dir = self.resolve_team_data_dir()
        model_cache = self.resolve_model_cache_path()

        matches, season_files = self.load_training_matches(processed_dir)
        dynamic_form = self.build_dynamic_form_from_matches(matches)

        overall_teams = self.load_json_if_exists(os.path.join(team_data_dir, "overall_teams.json"))
        season_teams = self.load_json_if_exists(os.path.join(team_data_dir, "season_teams.json"))
        head_to_head = self.load_json_if_exists(os.path.join(team_data_dir, "head_to_head.json"))
        current_form = self.load_json_if_exists(os.path.join(team_data_dir, "current_form.json"))
        league_strength = self.load_json_if_exists(os.path.join(team_data_dir, "league_strength.json")) or {}
        market_value_data = self.load_json_if_exists(os.path.join(team_data_dir, "mls_squad_values.json")) or {}

        if (
            overall_teams is None
            or season_teams is None
            or head_to_head is None
            or current_form is None
            or not isinstance(overall_teams, dict)
            or len(overall_teams) == 0
        ):
            fallback = self.build_fallback_data(matches, season_files)
            overall_teams, season_teams, head_to_head, current_form = fallback

        overall_teams = self.replace_nan_with_sentinel(overall_teams)
        season_teams = self.replace_nan_with_sentinel(season_teams)
        head_to_head = self.replace_nan_with_sentinel(head_to_head)
        current_form = self.replace_nan_with_sentinel(current_form)
        league_strength = self.replace_nan_with_sentinel(league_strength)

        if not isinstance(current_form, dict):
            current_form = {"teams": {}}
        if "teams" not in current_form or not isinstance(current_form["teams"], dict):
            current_form["teams"] = {}
        for team, stats in dynamic_form.items():
            if team not in current_form["teams"] or not isinstance(current_form["teams"].get(team), dict):
                current_form["teams"][team] = stats
                continue
            existing = current_form["teams"][team]
            for key, value in stats.items():
                if key not in existing or existing.get(key) in (None, "", 0, 0.0):
                    existing[key] = value

        feature_frames = []
        for season_key, season_matches in matches.groupby("season_key", sort=False):
            feature_frames.append(
                self.build_features(
                    season_matches,
                    season_key,
                    season_matches["competition"].iloc[0],
                    float(season_matches["season_coeff"].iloc[0]),
                    overall_teams,
                    season_teams,
                    head_to_head,
                    current_form,
                    league_strength,
                )
            )

        X = pd.concat(feature_frames, ignore_index=True)
        X = pd.get_dummies(X, columns=["competition"], dtype=float)
        train_columns = X.columns

        y_result = matches["FTR"].reset_index(drop=True)
        y_home_goals = matches["FTHG"].reset_index(drop=True)
        y_away_goals = matches["FTAG"].reset_index(drop=True)
        y_home_shots = matches["HS"].reset_index(drop=True)
        y_away_shots = matches["AS"].reset_index(drop=True)
        y_home_sot = matches["HST"].reset_index(drop=True)
        y_away_sot = matches["AST"].reset_index(drop=True)

        fingerprint = self.data_fingerprint(season_files, processed_dir)
        cache_bundle = None
        cache_valid = False
        if os.path.exists(model_cache):
            try:
                cache_bundle = joblib.load(model_cache)
                cache_valid = cache_bundle.get("fingerprint") == fingerprint
                if not cache_valid:
                    bt = cache_bundle.get("build_time")
                    age_h = (time.time() - bt) / 3600.0 if bt is not None else None
                    age_s = f" (cache age {age_h:.1f}h)" if age_h is not None else ""
                    if "--build-cache-only" in sys.argv:
                        print(f"[{self.config.region_name}] fingerprint mismatch{age_s}; retraining models...")
                    else:
                        print(f"[{self.config.region_name}] fingerprint mismatch{age_s}; using cached models (retrain runs Tue/Fri)")
                        cache_valid = True
            except Exception:
                cache_bundle = None
                cache_valid = False

        if cache_valid:
            clf = cache_bundle["clf"]
            result_label_encoder = cache_bundle["result_label_encoder"]
            home_goal_reg = cache_bundle["home_goal_reg"]
            away_goal_reg = cache_bundle["away_goal_reg"]
            home_shot_reg = cache_bundle["home_shot_reg"]
            away_shot_reg = cache_bundle["away_shot_reg"]
            home_sot_reg = cache_bundle["home_sot_reg"]
            away_sot_reg = cache_bundle["away_sot_reg"]
            train_columns = cache_bundle["train_columns"]
            goal_prob_models = cache_bundle.get("goal_prob_models")
            backend = cache_bundle.get("backend", "cached")
        else:
            clf, result_label_encoder, backend = self.train_result_model(X, y_result)
            regs = self.train_all_regressors(
                X,
                [
                    ("home_goal_reg", y_home_goals, 42),
                    ("away_goal_reg", y_away_goals, 43),
                    ("home_shot_reg", y_home_shots, 44),
                    ("away_shot_reg", y_away_shots, 45),
                    ("home_sot_reg", y_home_sot, 46),
                    ("away_sot_reg", y_away_sot, 47),
                ],
            )
            home_goal_reg = regs["home_goal_reg"]
            away_goal_reg = regs["away_goal_reg"]
            home_shot_reg = regs["home_shot_reg"]
            away_shot_reg = regs["away_shot_reg"]
            home_sot_reg = regs["home_sot_reg"]
            away_sot_reg = regs["away_sot_reg"]
            goal_prob_models = self.train_all_goal_prob_models(X, matches)
            try:
                os.makedirs(os.path.dirname(self.config.model_cache), exist_ok=True)
                joblib.dump(
                    {
                        "fingerprint": fingerprint,
                        "build_time": time.time(),
                        "clf": clf,
                        "result_label_encoder": result_label_encoder,
                        "home_goal_reg": home_goal_reg,
                        "away_goal_reg": away_goal_reg,
                        "home_shot_reg": home_shot_reg,
                        "away_shot_reg": away_shot_reg,
                        "home_sot_reg": home_sot_reg,
                        "away_sot_reg": away_sot_reg,
                        "goal_prob_models": goal_prob_models,
                        "train_columns": train_columns,
                        "backend": backend,
                    },
                    self.config.model_cache,
                )
            except Exception:
                import traceback
                traceback.print_exc()

        available_teams = sorted(set(matches["HomeTeam"].dropna()) | set(matches["AwayTeam"].dropna()))

        if "--build-cache-only" in sys.argv:
            print(f"Model cache ready: {self.config.model_cache} (backend={backend})")
            return

        print("\nMatch Predictor\n")
        print(f"Model backend: {backend}")
        print("Enter teams for prediction. Team 1 is always the home team.")
        print("Type 'q' to quit.\n")
        debug_input = input("Enable debug reasoning output? (y/n): ").strip().lower()
        debug_mode = debug_input in {"y", "yes", "1", "true"}
        print("")

        latest_season = season_files[-1].replace(".csv", "")
        team_competition_map = {}
        for _, row in matches.iterrows():
            team_competition_map[row["HomeTeam"]] = row["competition"]
            team_competition_map[row["AwayTeam"]] = row["competition"]
        competition_tables = self.build_latest_competition_tables(season_teams)
        competition_offsets = self.build_competition_offsets(league_strength, competition_tables)

        while True:
            team_1_raw = input("Team 1 (Home): ").strip()
            if team_1_raw.lower() in {"q", "quit", "exit"}:
                print("\nExiting predictor.")
                break

            team_2_raw = input("Team 2 (Away): ").strip()
            if team_2_raw.lower() in {"q", "quit", "exit"}:
                print("\nExiting predictor.")
                break

            home_team = self.resolve_team_name(team_1_raw, available_teams)
            away_team = self.resolve_team_name(team_2_raw, available_teams)

            if not home_team or not away_team:
                print("\nOne or both team names were not recognized.")
                continue
            if home_team == away_team:
                print("\nTeams must be different.\n")
                continue

            prediction_season = self.choose_season_for_teams(home_team, away_team, season_teams, latest_season)
            competition_key = os.path.dirname(prediction_season).replace("\\", "/") or "Unknown"
            prediction_start_year = self.parse_start_year_from_key(prediction_season)
            latest_start_year = max(self.parse_start_year_from_key(key) for key in season_teams.keys())
            effective_latest_year = max(latest_start_year, self.expected_current_latest_start_year())
            prediction_season_coeff = self.season_recency_coefficient(effective_latest_year, prediction_start_year)
            home_competition = team_competition_map.get(home_team, competition_key)
            away_competition = team_competition_map.get(away_team, competition_key)

            match_input = self.build_match_input(home_team, away_team)
            X_match = self.build_features(
                match_input,
                prediction_season,
                competition_key,
                prediction_season_coeff,
                overall_teams,
                season_teams,
                head_to_head,
                current_form,
                league_strength,
                home_competition_override=home_competition,
                away_competition_override=away_competition,
            )
            X_match = pd.get_dummies(X_match, columns=["competition"], dtype=float)
            X_match = X_match.reindex(columns=train_columns, fill_value=0.0)

            probabilities = {"H": 0.0, "D": 0.0, "A": 0.0}
            proba_values = clf.predict_proba(X_match)[0]
            for idx, encoded_label in enumerate(clf.classes_):
                label = result_label_encoder.inverse_transform([encoded_label])[0]
                probabilities[label] = float(proba_values[idx])
            raw_probabilities = dict(probabilities)

            home_league_strength = float(league_strength.get(home_competition, 0.85))
            away_league_strength = float(league_strength.get(away_competition, 0.85))
            strength_delta = home_league_strength - away_league_strength
            strength_gap = abs(strength_delta)

            draw_scale = 1.0
            league_direction_shift = 0.0

            if self.config.enable_mls_home_advantage:
                probabilities, draw_scale, league_direction_shift = self.apply_league_strength_adjustment(
                    probabilities, home_league_strength, away_league_strength
                )
            else:
                if strength_gap >= 0.08:
                    draw_scale = max(0.65, 1.0 - strength_gap)
                    old_draw = probabilities.get("D", 0.0)
                    probabilities["D"] = old_draw * draw_scale
                    carry = old_draw - probabilities["D"]
                    if carry > 0:
                        non_draw_total = probabilities.get("H", 0.0) + probabilities.get("A", 0.0)
                        if non_draw_total > 0:
                            probabilities["H"] += carry * (probabilities.get("H", 0.0) / non_draw_total)
                            probabilities["A"] += carry * (probabilities.get("A", 0.0) / non_draw_total)
                    total_prob = probabilities.get("H", 0.0) + probabilities.get("D", 0.0) + probabilities.get("A", 0.0)
                    if total_prob > 0:
                        probabilities["H"] /= total_prob
                        probabilities["D"] /= total_prob
                        probabilities["A"] /= total_prob

                league_direction_shift = max(-0.14, min(0.14, 0.30 * strength_delta))
                if league_direction_shift != 0.0:
                    if league_direction_shift > 0:
                        transfer = min(league_direction_shift, probabilities.get("A", 0.0))
                        probabilities["H"] += transfer
                        probabilities["A"] -= transfer
                    else:
                        transfer = min(abs(league_direction_shift), probabilities.get("H", 0.0))
                        probabilities["A"] += transfer
                        probabilities["H"] -= transfer

                    total_prob = probabilities.get("H", 0.0) + probabilities.get("D", 0.0) + probabilities.get("A", 0.0)
                    if total_prob > 0:
                        probabilities["H"] /= total_prob
                        probabilities["D"] /= total_prob
                        probabilities["A"] /= total_prob

            after_league_probabilities = dict(probabilities)

            early_season_lookup = season_teams.get(prediction_season, {})
            probabilities = self.blend_historical_prior(
                probabilities,
                home_team,
                away_team,
                early_season_lookup,
                competition=competition_key,
                league_strength=league_strength,
            )

            home_ppg10, home_form_idx, _, _ = self.team_form(home_team, current_form)
            away_ppg10, away_form_idx, _, _ = self.team_form(away_team, current_form)
            form_delta = (home_ppg10 - away_ppg10) + 0.5 * (home_form_idx - away_form_idx)

            if self.config.disable_post_model_form_shift:
                form_shift = 0.0
            else:
                form_shift = max(-0.10, min(0.10, 0.08 * form_delta))
                if form_shift != 0.0:
                    probabilities["H"] = max(0.0, probabilities.get("H", 0.0) + form_shift)
                    probabilities["A"] = max(0.0, probabilities.get("A", 0.0) - form_shift)
                    total_prob = probabilities.get("H", 0.0) + probabilities.get("D", 0.0) + probabilities.get("A", 0.0)
                    if total_prob > 0:
                        probabilities["H"] /= total_prob
                        probabilities["D"] /= total_prob
                        probabilities["A"] /= total_prob
            after_form_probabilities = dict(probabilities)

            home_cur_stats = self.clean_stats_dict(season_teams.get(prediction_season, {}).get(home_team, {}))
            away_cur_stats = self.clean_stats_dict(season_teams.get(prediction_season, {}).get(away_team, {}))
            home_stats = home_cur_stats or self.clean_stats_dict(self.find_latest_team_season_stats(home_team, season_teams))
            away_stats = away_cur_stats or self.clean_stats_dict(self.find_latest_team_season_stats(away_team, season_teams))
            fallback_past_season_used = bool(home_stats) and (not home_cur_stats or not away_cur_stats)
            early_past_factor = self.early_season_prior_factor(
                min(
                    self._season_games_played(home_team, early_season_lookup),
                    self._season_games_played(away_team, early_season_lookup),
                )
            )
            home_season_strength = (
                float(home_stats.get("avg_home_goals_scored", 0.0))
                - float(home_stats.get("avg_home_goals_conceded", 0.0))
                + 0.05
                * (
                    float(home_stats.get("avg_home_shots_for", 0.0))
                    - float(home_stats.get("avg_home_shots_against", 0.0))
                )
            )
            away_season_strength = (
                float(away_stats.get("avg_away_goals_scored", 0.0))
                - float(away_stats.get("avg_away_goals_conceded", 0.0))
                + 0.05
                * (
                    float(away_stats.get("avg_away_shots_for", 0.0))
                    - float(away_stats.get("avg_away_shots_against", 0.0))
                )
            )
            season_delta = home_season_strength - away_season_strength

            if self.config.enable_mls_home_advantage:
                season_shift = max(
                    -0.035,
                    min(0.035, 0.025 * season_delta * prediction_season_coeff * self.config.mls_adjustment_weight),
                )
            else:
                season_shift = max(-0.06, min(0.06, 0.04 * season_delta * prediction_season_coeff))

            if fallback_past_season_used:
                season_shift *= early_past_factor
            if season_shift != 0.0:
                probabilities["H"] = max(0.0, probabilities.get("H", 0.0) + season_shift)
                probabilities["A"] = max(0.0, probabilities.get("A", 0.0) - season_shift)
                total_prob = probabilities.get("H", 0.0) + probabilities.get("D", 0.0) + probabilities.get("A", 0.0)
                if total_prob > 0:
                    probabilities["H"] /= total_prob
                    probabilities["D"] /= total_prob
                    probabilities["A"] /= total_prob
            after_season_probabilities = dict(probabilities)

            season_lookup_for_pred = season_teams.get(prediction_season, {})
            season_positions = self.build_season_position_map(season_lookup_for_pred)
            home_table = self.clean_stats_dict(season_lookup_for_pred.get(home_team, {})) or self.clean_stats_dict(self.find_latest_team_season_stats(home_team, season_teams))
            away_table = self.clean_stats_dict(season_lookup_for_pred.get(away_team, {})) or self.clean_stats_dict(self.find_latest_team_season_stats(away_team, season_teams))
            home_table_cur = self.clean_stats_dict(season_lookup_for_pred.get(home_team, {}))
            away_table_cur = self.clean_stats_dict(season_lookup_for_pred.get(away_team, {}))
            table_used_past_fallback = bool(home_table) and (not home_table_cur or not away_table_cur)
            home_points = float(home_table.get("points", 0.0) or 0.0)
            away_points = float(away_table.get("points", 0.0) or 0.0)
            home_pos = float(season_positions.get(home_team, home_table.get("league_position", 0.0) or 0.0))
            away_pos = float(season_positions.get(away_team, away_table.get("league_position", 0.0) or 0.0))
            table_delta = ((home_points - away_points) / 30.0) + ((away_pos - home_pos) / 20.0)

            if self.config.enable_mls_home_advantage:
                division_weight = 0.35 + 0.25 * abs(strength_delta)
                table_shift = max(-0.04, min(0.04, 0.03 * table_delta * division_weight * self.config.mls_adjustment_weight))
            else:
                division_weight = 0.5 + 0.5 * abs(strength_delta)
                table_shift = max(-0.08, min(0.08, 0.05 * table_delta * division_weight))

            if table_used_past_fallback:
                table_shift *= early_past_factor

            home_comp_table = competition_tables.get(home_competition, {})
            away_comp_table = competition_tables.get(away_competition, {})
            home_comp_pos = float(home_comp_table.get("positions", {}).get(home_team, home_pos or 0.0) or 0.0)
            away_comp_pos = float(away_comp_table.get("positions", {}).get(away_team, away_pos or 0.0) or 0.0)
            home_abs_pos = float(competition_offsets.get(home_competition, 0)) + home_comp_pos
            away_abs_pos = float(competition_offsets.get(away_competition, 0)) + away_comp_pos
            interleague_pos_delta = away_abs_pos - home_abs_pos

            if self.config.enable_mls_home_advantage:
                interleague_shift = max(-0.015, min(0.015, 0.002 * interleague_pos_delta * self.config.mls_adjustment_weight))
            else:
                interleague_shift = max(-0.12, min(0.12, 0.0075 * interleague_pos_delta))

            boundary_bonus_shift = 0.0
            abs_gap = abs(interleague_pos_delta)
            if home_competition != away_competition and abs_gap <= 6.0:
                boundary_factor = (6.0 - abs_gap) / 6.0
                if self.config.enable_mls_home_advantage:
                    if home_league_strength > away_league_strength:
                        boundary_bonus_shift += 0.006 * boundary_factor * self.config.mls_adjustment_weight
                    elif away_league_strength > home_league_strength:
                        boundary_bonus_shift -= 0.006 * boundary_factor * self.config.mls_adjustment_weight
                else:
                    if home_league_strength > away_league_strength:
                        boundary_bonus_shift += 0.035 * boundary_factor
                    elif away_league_strength > home_league_strength:
                        boundary_bonus_shift -= 0.035 * boundary_factor

            table_shift = table_shift + interleague_shift + boundary_bonus_shift
            if self.config.enable_mls_home_advantage:
                table_shift = max(-0.045, min(0.045, table_shift))
            else:
                table_shift = max(-0.16, min(0.16, table_shift))

            if table_shift != 0.0:
                probabilities["H"] = max(0.0, probabilities.get("H", 0.0) + table_shift)
                probabilities["A"] = max(0.0, probabilities.get("A", 0.0) - table_shift)
                total_prob = probabilities.get("H", 0.0) + probabilities.get("D", 0.0) + probabilities.get("A", 0.0)
                if total_prob > 0:
                    probabilities["H"] /= total_prob
                    probabilities["D"] /= total_prob
                    probabilities["A"] /= total_prob

            home_adv_shift = 0.0
            if self.config.enable_mls_home_advantage:
                home_adv_shift = self.mls_home_advantage_shift(home_team, prediction_season, season_teams)
                transfer = min(home_adv_shift, probabilities.get("A", 0.0))
                probabilities["H"] = max(0.0, probabilities.get("H", 0.0) + transfer)
                probabilities["A"] = max(0.0, probabilities.get("A", 0.0) - transfer)
                total_prob = probabilities.get("H", 0.0) + probabilities.get("D", 0.0) + probabilities.get("A", 0.0)
                if total_prob > 0:
                    probabilities["H"] /= total_prob
                    probabilities["D"] /= total_prob
                    probabilities["A"] /= total_prob

            market_shift = 0.0
            home_market_score = 0.0
            away_market_score = 0.0
            if self.config.enable_market_value_shift:
                market_shift, home_market_score, away_market_score = self.market_value_probability_shift(
                    home_team, away_team, market_value_data
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

            if self.config.enable_mls_home_advantage:
                probabilities = self.apply_home_advantage_boost(probabilities)

            probabilities = self.reduce_draw_probability(probabilities)
            seed = self.prediction_randomizer_seed(home_team, away_team, competition_key, prediction_season)
            probabilities = self.apply_probability_randomizer(probabilities, self.config.randomizer_max_delta, seed=seed)
            final_probabilities = dict(probabilities)

            prediction = max(probabilities, key=probabilities.get)
            base_home_xg = max(0.0, float(home_goal_reg.predict(X_match)[0]))
            base_away_xg = max(0.0, float(away_goal_reg.predict(X_match)[0]))
            rng = np.random.default_rng(seed)
            predicted_score_home, predicted_score_away = self.sample_score_from_probs(
                probabilities.get("H", 0.0),
                probabilities.get("D", 0.0),
                probabilities.get("A", 0.0),
                base_home_xg,
                base_away_xg,
                prediction,
                rng,
            )
            predicted_home_shots = max(0.0, float(home_shot_reg.predict(X_match)[0]))
            predicted_away_shots = max(0.0, float(away_shot_reg.predict(X_match)[0]))
            predicted_home_sot = max(0.0, float(home_sot_reg.predict(X_match)[0]))
            predicted_away_sot = max(0.0, float(away_sot_reg.predict(X_match)[0]))

            result_map = {"H": f"{home_team} win", "D": "Draw", "A": f"{away_team} win"}
            print("\nPrediction")
            print(f"Home: {home_team}")
            print(f"Away: {away_team}")
            print(f"Most likely result: {result_map[prediction]}")
            print(
                "Win probabilities: "
                f"{home_team} {self.format_percent_text(probabilities['H'])} | "
                f"Draw {self.format_percent_text(probabilities['D'])} | "
                f"{away_team} {self.format_percent_text(probabilities['A'])}"
            )
            print(f"Predicted score: {home_team} {predicted_score_home} - {predicted_score_away} {away_team}")
            print(f"Predicted shots: {home_team} {predicted_home_shots:.1f} | {away_team} {predicted_away_shots:.1f}")
            print(f"Predicted shots on target: {home_team} {predicted_home_sot:.1f} | {away_team} {predicted_away_sot:.1f}\n")

    def get_exports(self) -> Dict[str, Any]:
        """Dictionary of all regional attributes and methods for backward compatibility shims."""
        return {
            "RegionConfig": RegionConfig,
            "PredictionEngine": PredictionEngine,
            "AveragedProbaClassifier": AveragedProbaClassifier,
            "BASE_DIR": self.config.base_dir,
            "PREDICTIONS_DIR": self.config.predictions_dir,
            "PROJECT_DIR": self.config.project_dir,
            "PROCESSED_DIR": self.config.processed_dir,
            "TEAM_DATA_DIR": self.config.team_data_dir,
            "MODEL_CACHE": self.config.model_cache,
            "SHARED_PROCESSED_DIR": self.config.shared_processed_dir,
            "SHARED_TEAM_DATA_DIR": self.config.shared_team_data_dir,
            "SHARED_MODEL_CACHE": self.config.shared_model_cache,
            "SEASON_PATTERN": self.config.season_pattern,
            "MAPPING_FILE": self.config.mapping_file,
            "MIN_START_YEAR": self.config.min_start_year,
            "DRAW_REDUCTION_FACTOR": self.config.draw_reduction_factor,
            "HIGH_DRAW_THRESHOLD": self.config.high_draw_threshold,
            "HIGH_DRAW_EXTRA_REDUCTION_MAX": self.config.high_draw_extra_reduction_max,
            "EU_RANDOMIZER_MAX_DELTA": self.config.randomizer_max_delta,
            "MLS_RANDOMIZER_MAX_DELTA": self.config.randomizer_max_delta,
            "MLS_DRAW_REDUCTION_FACTOR": self.config.draw_reduction_factor,
            "MLS_HIGH_DRAW_THRESHOLD": self.config.high_draw_threshold,
            "MLS_HIGH_DRAW_EXTRA_REDUCTION_MAX": self.config.high_draw_extra_reduction_max,
            "MLS_HOME_EDGE_SHIFT": self.config.mls_home_edge_shift,
            "MLS_DRAW_TARGET": self.config.mls_draw_target,
            "MLS_DRAW_BLEND": self.config.mls_draw_blend,
            "MLS_ADJUSTMENT_WEIGHT": self.config.mls_adjustment_weight,
            "MLS_MARKET_SHIFT_MAX": self.config.mls_market_shift_max,
            "MLS_MARKET_SHIFT_SCALE": self.config.mls_market_shift_scale,
            "MLS_HOME_ADV_MIN_SHIFT": self.config.mls_home_adv_min_shift,
            "MLS_HOME_ADV_MAX_SHIFT": self.config.mls_home_adv_max_shift,
            "MLS_HOME_EXTRA_BOOST": self.config.home_extra_boost,
            "EARLY_SEASON_GAMES_THRESHOLD": EARLY_SEASON_GAMES_THRESHOLD,
            "EARLY_SEASON_FULL_PRIOR_GAMES": EARLY_SEASON_FULL_PRIOR_GAMES,
            "EARLY_SEASON_FADE_END_GAMES": EARLY_SEASON_FADE_END_GAMES,
            "PRIOR_BLEND_STRENGTH_AT_ZERO": PRIOR_BLEND_STRENGTH_AT_ZERO,
            "PROMOTED_TEAM_STRENGTH_CAP": PROMOTED_TEAM_STRENGTH_CAP,
            "RELEGATED_TEAM_STRENGTH_FLOOR": RELEGATED_TEAM_STRENGTH_FLOOR,
            "GOAL_PROB_TARGETS": GOAL_PROB_TARGETS,
            "GOAL_PROB_RESULT_KEYS": GOAL_PROB_RESULT_KEYS,
            "CPU_COUNT": CPU_COUNT,
            "TRAIN_WORKERS": TRAIN_WORKERS,
            "MODEL_THREADS": MODEL_THREADS,
            # Functions
            "early_season_prior_weight": self.early_season_prior_weight,
            "early_season_prior_factor": self.early_season_prior_factor,
            "_season_teams_signature": self._season_teams_signature,
            "_load_historical_tables": self._load_historical_tables,
            "team_division_history": self.team_division_history,
            "_season_games_played": self._season_games_played,
            "_prior_team_strength": self._prior_team_strength,
            "_prior_distribution": self._prior_distribution,
            "blend_historical_prior": self.blend_historical_prior,
            "sample_score_from_probs": self.sample_score_from_probs,
            "align_predicted_score": self.align_predicted_score,
            "compute_goal_probabilities": self.compute_goal_probabilities,
            "load_json": self.load_json,
            "save_json": self.save_json,
            "load_json_if_exists": self.load_json_if_exists,
            "is_invalid_stat_value": self.is_invalid_stat_value,
            "replace_nan_with_sentinel": self.replace_nan_with_sentinel,
            "clean_stats_dict": self.clean_stats_dict,
            "coerce_feature_value": self.coerce_feature_value,
            "data_fingerprint": self.data_fingerprint,
            "per_game": self.per_game,
            "parse_season_start_year": self.parse_season_start_year,
            "parse_start_year_from_key": self.parse_start_year_from_key,
            "expected_current_latest_start_year": self.expected_current_latest_start_year,
            "season_recency_coefficient": self.season_recency_coefficient,
            "choose_season_for_teams": self.choose_season_for_teams,
            "find_latest_team_season_stats": self.find_latest_team_season_stats,
            "team_form": self.team_form,
            "build_season_position_map": self.build_season_position_map,
            "split_season_key": self.split_season_key,
            "build_latest_competition_tables": self.build_latest_competition_tables,
            "build_competition_offsets": self.build_competition_offsets,
            "build_dynamic_form_from_matches": self.build_dynamic_form_from_matches,
            "build_fallback_data": self.build_fallback_data,
            "build_features": self.build_features,
            "load_training_matches": self.load_training_matches,
            "_load_name_mapping": self._load_name_mapping,
            "resolve_team_name": self.resolve_team_name,
            "build_match_input": self.build_match_input,
            "probabilities_to_odds": self.probabilities_to_odds,
            "format_percent_text": self.format_percent_text,
            "prediction_randomizer_seed": self.prediction_randomizer_seed,
            "apply_probability_randomizer": self.apply_probability_randomizer,
            "reduce_draw_probability": self.reduce_draw_probability,
            "apply_home_advantage_boost": self.apply_home_advantage_boost,
            "mls_home_advantage_shift": self.mls_home_advantage_shift,
            "market_value_team_score": self.market_value_team_score,
            "market_value_probability_shift": self.market_value_probability_shift,
            "apply_league_strength_adjustment": self.apply_league_strength_adjustment,
            "train_result_model": self.train_result_model,
            "train_regression_model": self.train_regression_model,
            "train_all_regressors": self.train_all_regressors,
            "train_goal_prob_model": self.train_goal_prob_model,
            "train_all_goal_prob_models": self.train_all_goal_prob_models,
            "predict_goal_probabilities": self.predict_goal_probabilities,
            "processed_dir_has_season_csvs": self.processed_dir_has_season_csvs,
            "resolve_processed_dir": self.resolve_processed_dir,
            "resolve_team_data_dir": self.resolve_team_data_dir,
            "resolve_model_cache_path": self.resolve_model_cache_path,
            "main": self.main,
        }

    def export_to_globals(self, g: dict) -> None:
        """Inject all exported symbols directly into a calling module's globals."""
        g.update(self.get_exports())

