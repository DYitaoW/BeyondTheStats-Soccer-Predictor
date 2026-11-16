"""Predict upcoming matchweek fixtures for NWSL and WSL with 79+ statistical parity fields."""

import os as _os_paths_setup
import sys as _sys_paths_setup
_FILES_DIR = _os_paths_setup.path.dirname(_os_paths_setup.path.abspath(__file__))
_REGION_DIR = _os_paths_setup.path.dirname(_FILES_DIR)
_SP_DIR = _os_paths_setup.path.dirname(_os_paths_setup.path.dirname(_REGION_DIR))
if _SP_DIR not in _sys_paths_setup.path:
    _sys_paths_setup.path.insert(0, _SP_DIR)
from shared import paths as _bts_paths
if str(_bts_paths.SHARED_DIR) not in _sys_paths_setup.path:
    _sys_paths_setup.path.insert(0, str(_bts_paths.SHARED_DIR))
_WEBSITE_DIR = str(_bts_paths.SP_DIR / "Website")
if _WEBSITE_DIR not in _sys_paths_setup.path:
    _sys_paths_setup.path.insert(0, _WEBSITE_DIR)

BASE_DIR = str(_bts_paths.WOMENS_DIR)
PREDICTIONS_DIR = str(_bts_paths.OUTPUT_PRED_WOMENS)
PROJECT_DIR = str(_bts_paths.SP_DIR)

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import joblib
import pandas as pd

import Predict_Match as pm
from shared import sqlite_store as store
from competition_rules import canonical_team_name, make_match_sync_key
from math_utils import (
    _compute_correct_score_dist,
    _compute_double_chance,
    _compute_asian_handicap,
    _compute_total_goals_dist,
    _compute_first_to_score,
    _compute_clean_sheet,
    _compute_goal_probabilities,
)


class AveragedProbaClassifier:
    """Compatibility shim for unpickling ensemble model cache."""
    def __init__(self, models):
        self.models = models
        self.classes_ = models[0].classes_

    def predict_proba(self, X):
        matrices = [model.predict_proba(X) for model in self.models]
        return sum(matrices) / len(matrices)

RAW_DATA_DIR = os.path.join(BASE_DIR, "Data", "Raw_Data")
PREDICTIONS_FILE = str(_bts_paths.WOMENS_UPCOMING_FILE)
MODEL_CACHE = os.path.join(BASE_DIR, "Data", "Team_Data", "model_cache.pkl")


def load_context():
    if not os.path.exists(MODEL_CACHE):
        print(f"Model cache missing at {MODEL_CACHE}. Building...")
        import subprocess
        proc = subprocess.run([sys.executable, os.path.join(_FILES_DIR, "Predict_Match.py"), "--build-cache-only"], check=True)

    bundle = joblib.load(MODEL_CACHE)
    team_data_dir = os.path.join(BASE_DIR, "Data", "Team_Data")
    overall_teams = pm.load_json(os.path.join(team_data_dir, "overall_teams.json"))
    season_teams = pm.load_json(os.path.join(team_data_dir, "season_teams.json"))
    head_to_head = pm.load_json(os.path.join(team_data_dir, "head_to_head.json"))
    current_form = pm.load_json(os.path.join(team_data_dir, "current_form.json"))
    league_strength = pm.load_json(os.path.join(team_data_dir, "league_strength.json"))

    return {
        "clf": bundle["clf"],
        "result_le": bundle["result_label_encoder"],
        "home_goal_reg": bundle["home_goal_reg"],
        "away_goal_reg": bundle["away_goal_reg"],
        "home_shot_reg": bundle["home_shot_reg"],
        "away_shot_reg": bundle["away_shot_reg"],
        "home_sot_reg": bundle["home_sot_reg"],
        "away_sot_reg": bundle["away_sot_reg"],
        "train_columns": bundle["train_columns"],
        "overall_teams": overall_teams,
        "season_teams": season_teams,
        "head_to_head": head_to_head,
        "current_form": current_form,
        "league_strength": league_strength,
        "available_teams": sorted(set(overall_teams.keys())),
    }


def find_upcoming_unplayed_fixtures(window_days: int = 365):
    """Real unplayed fixtures of the current season (from Womens_Data_Manager's ESPN CSVs).

    Kickoff time and ESPN match id come from the CSV, so the app, the live
    poller and push-to-start all see the real kickoff (no invented 19:00 UTC).
    """
    import Womens_Data_Manager as wdm

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=max(1, int(window_days)))
    upcoming = []
    for comp, cfg in wdm.LEAGUES.items():
        season = wdm.current_season(comp)
        path = os.path.join(str(cfg["raw_dir"]), f"{cfg['prefix']}stat{season}.csv")
        if not os.path.exists(path):
            continue
        df = pd.read_csv(path, dtype=str).fillna("")
        for _, row in df.iterrows():
            if str(row.get("Res", "")).strip() in ("H", "D", "A"):
                continue  # already played
            kickoff_raw = str(row.get("KickoffUTC", "")).strip()
            try:
                kickoff = datetime.strptime(kickoff_raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            # Skip fixtures that should already be over (ESPN marks them post
            # once played) and fixtures beyond the prediction window.
            if kickoff < now - timedelta(hours=3) or kickoff > horizon:
                continue
            ht = str(row.get("Home", "")).strip()
            at = str(row.get("Away", "")).strip()
            if not ht or not at:
                continue
            kickoff_et = kickoff.astimezone(ZoneInfo("America/New_York"))
            league_dir = os.path.relpath(str(cfg["raw_dir"]), RAW_DATA_DIR).replace(os.sep, "/")
            upcoming.append({
                # Model keys (as trained): processed folder + season file stem.
                "model_competition": league_dir,
                "season_key": f"{league_dir}/{cfg['prefix']}stat{season}",
                "date": kickoff_et.strftime("%Y-%m-%d"),
                "kickoff_utc": kickoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "kickoff_et": kickoff_et.strftime("%Y-%m-%d %H:%M"),
                "match_time": kickoff_et.strftime("%H:%M"),
                "match_id": str(row.get("MatchId", "")).strip(),
                "stage": str(row.get("Stage", "")).strip() or "regular",
                "season": str(season),
                "competition": comp,
                "home_team": ht,
                "away_team": at,
            })
    upcoming.sort(key=lambda f: f["kickoff_utc"])
    return upcoming


def _replace_womens_upcoming_rows(rows: list[dict]) -> dict:
    """Upsert this run's predictions and drop earlier womens rows not in it.

    Fixtures that were rescheduled, cancelled or already played must not stay
    behind in upcoming_games (the old generator's rows also lived there).
    """
    # Compare on prediction_key: the upsert may re-key rows that merge by sync_key.
    keep = {str(r.get("prediction_key")) for r in rows}
    res = store.upsert_upcoming_games(rows, source="womens")
    removed = 0
    with store._WRITE_LOCK:
        conn = store._open_ready(None)
        try:
            stale = [
                r["storage_key"]
                for r in conn.execute(
                    "SELECT storage_key, json_extract(payload_json, '$.prediction_key') AS pk "
                    "FROM upcoming_games WHERE json_extract(payload_json, '$.source') = 'womens'"
                ).fetchall()
                if str(r["pk"]) not in keep
            ]
            if stale:
                conn.executemany("DELETE FROM upcoming_games WHERE storage_key = ?", [(k,) for k in stale])
                conn.commit()
                removed = len(stale)
        finally:
            conn.close()
    res["removed_stale"] = removed
    return res


def predict_upcoming(window_days: int = 365):
    print("=== Predicting Upcoming Womens Fixtures ===")
    ctx = load_context()
    fixtures = find_upcoming_unplayed_fixtures(window_days)
    print(f"Found {len(fixtures)} unplayed fixtures.")

    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()
    rows = []

    for fix in fixtures:
        m_date = fix["date"]
        comp = fix["competition"]
        ht = fix["home_team"]
        at = fix["away_team"]

        c_home = canonical_team_name(ht, comp) or ht
        c_away = canonical_team_name(at, comp) or at
        skey = make_match_sync_key(m_date, comp, c_home, c_away)
        pkey = f"pred:{skey}"

        # Baseline ratings & expectations. The model was trained with the
        # processed-folder competition ("England/WSL") and path season keys;
        # passing the app label made every WSL competition dummy zero.
        season_key = fix["season_key"]
        model_comp = fix["model_competition"]
        ls = ctx["league_strength"].get(model_comp, ctx["league_strength"].get(comp, 0.85))

        X_match = pm.build_features(
            pm.build_match_input(ht, at),
            season_key,
            model_comp,
            ls,
            ctx["overall_teams"],
            ctx["season_teams"],
            ctx["head_to_head"],
            ctx["current_form"],
            ctx["league_strength"],
        )
        X_match = pd.get_dummies(X_match, columns=["competition"], dtype=float)
        X_match = X_match.reindex(columns=ctx["train_columns"], fill_value=0.0)

        # Classifier
        proba_values = ctx["clf"].predict_proba(X_match)[0]
        probabilities = {"H": 0.0, "D": 0.0, "A": 0.0}
        for idx, enc in enumerate(ctx["clf"].classes_):
            label = ctx["result_le"].inverse_transform([enc])[0]
            probabilities[label] = float(proba_values[idx])

        ph = round(probabilities["H"], 3)
        pd_prob = round(probabilities["D"], 3)
        pa = round(probabilities["A"], 3)

        # Regressors
        pred_hg = round(float(ctx["home_goal_reg"].predict(X_match)[0]), 2)
        pred_ag = round(float(ctx["away_goal_reg"].predict(X_match)[0]), 2)
        pred_hs = round(float(ctx["home_shot_reg"].predict(X_match)[0]), 1)
        pred_as = round(float(ctx["away_shot_reg"].predict(X_match)[0]), 1)
        pred_hst = round(float(ctx["home_sot_reg"].predict(X_match)[0]), 1)
        pred_ast = round(float(ctx["away_sot_reg"].predict(X_match)[0]), 1)

        # Secondary markets
        cs_dist = _compute_correct_score_dist(pred_hg, pred_ag)
        dc = _compute_double_chance(ph, pd_prob, pa)
        ah = _compute_asian_handicap(pred_hg, pred_ag, ph, pd_prob, pa)
        tg_dist = _compute_total_goals_dist(pred_hg, pred_ag)
        fts = _compute_first_to_score(pred_hg, pred_ag)
        clean = _compute_clean_sheet(pred_hg, pred_ag)
        goal_probs = _compute_goal_probabilities(pred_hg, pred_ag)

        pred_res = "H" if ph >= max(pd_prob, pa) else ("A" if pa >= max(ph, pd_prob) else "D")
        winner_lbl = f"Pred: {ht}" if pred_res == "H" else (f"Pred: {at}" if pred_res == "A" else "Pred: Draw")

        # Half markets
        pred_hg_ht = round(pred_hg * 0.45, 2)
        pred_ag_ht = round(pred_ag * 0.45, 2)
        ph_ht = round(ph * 0.75 + pd_prob * 0.15, 3)
        pa_ht = round(pa * 0.75 + pd_prob * 0.15, 3)
        pd_ht = round(max(0.05, 1.0 - (ph_ht + pa_ht)), 3)

        # Expected points
        xpts_h = round(ph * 3.0 + pd_prob * 1.0, 2)
        xpts_a = round(pa * 3.0 + pd_prob * 1.0, 2)

        # Form info
        h_form = ctx["current_form"].get(ht, {}).get("last_5", [])
        a_form = ctx["current_form"].get(at, {}).get("last_5", [])

        # Odds approximation from model prob
        odds_h = round(1.05 / max(0.05, ph), 2)
        odds_d = round(1.05 / max(0.05, pd_prob), 2)
        odds_a = round(1.05 / max(0.05, pa), 2)

        row = {
            "prediction_key": pkey,
            "created_at_utc": now_iso,
            "match_date": m_date,
            "match_date_iso": m_date,
            "match_datetime_utc": fix["kickoff_utc"],
            "match_datetime_et": fix["kickoff_et"],
            "kickoff_utc": fix["kickoff_utc"],
            "match_time": fix["match_time"],
            "match_id": fix["match_id"] or None,
            "stage": fix["stage"],
            "competition": comp,
            "home_team": ht,
            "away_team": at,
            "canonical_home_team": c_home,
            "canonical_away_team": c_away,
            "display_home_team": ht,
            "display_away_team": at,
            "sync_key": skey,
            "unmapped_teams": 0,
            "is_neutral_site": 0,
            "schedule_only": 0,
            "prediction_quality": "prediction",
            "has_prediction": True,
            "predicted_result": pred_res,
            "winner_label": winner_lbl,
            "prob_home": ph,
            "prob_draw": pd_prob,
            "prob_away": pa,
            "prob_home_text": f"{round(ph * 100, 1)}%",
            "prob_draw_text": f"{round(pd_prob * 100, 1)}%",
            "prob_away_text": f"{round(pa * 100, 1)}%",
            "pred_home_goals": pred_hg,
            "pred_away_goals": pred_ag,
            "pred_home_shots": pred_hs,
            "pred_away_shots": pred_as,
            "pred_home_sot": pred_hst,
            "pred_away_sot": pred_ast,
            "pred_home_possession": round(50.0 + (pred_hg - pred_ag) * 5.0, 1),
            "pred_away_possession": round(50.0 - (pred_hg - pred_ag) * 5.0, 1),
            "pred_home_corners": round(pred_hs * 0.4, 1),
            "pred_away_corners": round(pred_as * 0.4, 1),
            "pred_home_fouls": 11.5,
            "pred_away_fouls": 11.8,
            "pred_home_cards": 1.2,
            "pred_away_cards": 1.4,
            "correct_score_dist": cs_dist,
            "double_chance": dc,
            "asian_handicap": ah,
            "total_goals_dist": tg_dist,
            "first_to_score": fts,
            "clean_sheet": clean,
            "xpts_home": xpts_h,
            "xpts_away": xpts_a,
            "expected_points": {"home": xpts_h, "away": xpts_a},
            "pred_home_goals_ht": pred_hg_ht,
            "pred_away_goals_ht": pred_ag_ht,
            "prob_home_ht": ph_ht,
            "prob_draw_ht": pd_ht,
            "prob_away_ht": pa_ht,
            "last_5_home": h_form,
            "last_5_away": a_form,
            "odds_home": odds_h,
            "odds_draw": odds_d,
            "odds_away": odds_a,
            "prob_both_score": goal_probs.get("prob_both_score", 0.5),
            "prob_over_1_5": goal_probs.get("prob_over_1_5", 0.75),
            "prob_over_2_5": goal_probs.get("prob_over_2_5", 0.52),
            "prob_over_3_5": goal_probs.get("prob_over_3_5", 0.30),
            "prob_home_goals_0": goal_probs.get("prob_home_goals_0", 0.25),
            "prob_home_goals_1plus": goal_probs.get("prob_home_goals_1plus", 0.75),
            "prob_home_goals_2plus": goal_probs.get("prob_home_goals_2plus", 0.45),
            "prob_away_goals_0": goal_probs.get("prob_away_goals_0", 0.30),
            "prob_away_goals_1plus": goal_probs.get("prob_away_goals_1plus", 0.70),
            "prob_away_goals_2plus": goal_probs.get("prob_away_goals_2plus", 0.38),
            "probability_reasoning": f"Model favors {winner_lbl} based on rolling form and attacking efficiency.",
            "source": "womens",
        }
        rows.append(row)

    # Save to CSV
    os.makedirs(os.path.dirname(PREDICTIONS_FILE), exist_ok=True)
    df_out = pd.DataFrame(rows)
    df_out.to_csv(PREDICTIONS_FILE, index=False)
    print(f"Saved {len(rows)} predictions to {PREDICTIONS_FILE}")

    # Upsert to SQLite
    res = _replace_womens_upcoming_rows(rows)
    print(f"Upserted {res.get('upserted', 0)} predictions to SQLite upcoming_games "
          f"(removed {res.get('removed_stale', 0)} stale womens rows).")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-days", type=int, default=365)
    args = parser.parse_args()
    predict_upcoming(window_days=args.window_days)


if __name__ == "__main__":
    main()
