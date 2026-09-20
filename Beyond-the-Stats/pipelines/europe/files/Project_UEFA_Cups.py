#!/usr/bin/env python3
"""Project UEFA CL / EL / ECL league-phase tables + knockout finish odds.

Focus competitions only:
  - Europe/Champions League (8 league-phase matches)
  - Europe/Europa League (8)
  - Europe/Conference League (6)

Pipeline:
  1. Pull completed + upcoming league-phase fixtures from ESPN
  2. Attach model (or home-prior) predictions for every upcoming fixture
  3. Monte Carlo the remaining league phase → full position odds (like a league table)
  4. After each table sim, run the UEFA knockout draw/sim (see UEFA_Cup_Knockout.py)
  5. Aggregate winner / runner-up / SF / QF / R16 / playoff odds per team

Writes into Output/Predictions/cups/ (merges with existing cup artifacts).
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

_FILES_DIR = Path(__file__).resolve().parent
_SP_DIR = _FILES_DIR.parents[2]
if str(_SP_DIR) not in sys.path:
    sys.path.insert(0, str(_SP_DIR))
if str(_FILES_DIR) not in sys.path:
    sys.path.insert(0, str(_FILES_DIR))

from shared import paths as _bts_paths  # noqa: E402

import UEFA_Cup_Knockout as uefa_ko  # noqa: E402
import Track_Cup_Results as track  # noqa: E402

PREDICTIONS_DIR = Path(_bts_paths.OUTPUT_PRED_CUPS)
UPCOMING_FILE = PREDICTIONS_DIR / "upcoming_cup_predictions.csv"
COMPLETED_FILE = PREDICTIONS_DIR / "completed_cup_predictions.csv"
TABLES_FILE = PREDICTIONS_DIR / "projected_cup_tables.csv"
REAL_TABLES_FILE = PREDICTIONS_DIR / "real_cup_tables.csv"
BRACKETS_FILE = PREDICTIONS_DIR / "projected_cup_brackets.json"

TABLE_SIMS = int(os.environ.get("BTS_UEFA_TABLE_SIMS", "200") or "200")
RNG_SEED = int(os.environ.get("BTS_UEFA_SIM_SEED", "20260920") or "20260920")


def _progress(msg: str) -> None:
    print(msg, flush=True)


def _empty_history():
    return track._empty_frame(track.CUP_HISTORY_COLUMNS)


def _load_csv(path: Path):
    if not path.is_file():
        return _empty_history()
    try:
        return track._ensure_columns(pd.read_csv(path), track.CUP_HISTORY_COLUMNS)
    except Exception:
        return _empty_history()


def _uefa_only(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return _empty_history()
    out = frame.copy()
    out["competition"] = out["competition"].astype(str).str.strip()
    return out[out["competition"].isin(uefa_ko.UEFA_COMPETITIONS)].reset_index(drop=True)


def _non_uefa(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return _empty_history()
    out = frame.copy()
    out["competition"] = out["competition"].astype(str).str.strip()
    return out[~out["competition"].isin(uefa_ko.UEFA_COMPETITIONS)].reset_index(drop=True)


def _attach_simple_predictions(frame: pd.DataFrame) -> pd.DataFrame:
    """Fill missing probs / predicted scores so Monte Carlo + APIs have predictions."""
    if frame is None or frame.empty:
        return _empty_history()
    out = frame.copy()
    for col, default in (
        ("prob_home", 0.46),
        ("prob_draw", 0.26),
        ("prob_away", 0.28),
        ("pred_home_goals", 1.4),
        ("pred_away_goals", 1.1),
    ):
        if col not in out.columns:
            out[col] = default
        out[col] = pd.to_numeric(out[col], errors="coerce")
        out[col] = out[col].fillna(default)
    # predicted_result from probs when blank
    def _pred_code(row):
        existing = str(row.get("predicted_result") or "").strip().upper()
        if existing in {"H", "D", "A"}:
            return existing
        ph, pd_, pa = float(row["prob_home"]), float(row["prob_draw"]), float(row["prob_away"])
        best = max(ph, pd_, pa)
        if best == ph:
            return "H"
        if best == pa:
            return "A"
        return "D"

    out["predicted_result"] = out.apply(_pred_code, axis=1)
    out["probability_reasoning"] = out.apply(
        lambda r: str(r.get("probability_reasoning") or "").strip()
        or f"UEFA prior/model H={float(r['prob_home']):.3f} D={float(r['prob_draw']):.3f} A={float(r['prob_away']):.3f}",
        axis=1,
    )
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    if "created_at_utc" not in out.columns:
        out["created_at_utc"] = now
    else:
        out["created_at_utc"] = out["created_at_utc"].fillna(now)
    return track._ensure_columns(out, track.CUP_HISTORY_COLUMNS)


def _try_model_predict_pending(pending: pd.DataFrame) -> pd.DataFrame:
    """Best-effort: enrich pending rows via Predict_Match when a cache exists."""
    if pending is None or pending.empty:
        return pending
    try:
        import Predict_Match as pm
    except Exception as exc:
        _progress(f"[uefa] Predict_Match unavailable ({exc}); using home priors")
        return _attach_simple_predictions(pending)

    try:
        if hasattr(pm, "ensure_model_cache"):
            pm.ensure_model_cache()
        ctx = None
        if hasattr(pm, "load_prediction_context"):
            ctx = pm.load_prediction_context()
        elif hasattr(pm, "build_context"):
            ctx = pm.build_context()
        if not ctx:
            return _attach_simple_predictions(pending)
    except Exception as exc:
        _progress(f"[uefa] model context failed ({exc}); using home priors")
        return _attach_simple_predictions(pending)

    rows = []
    for _, row in pending.iterrows():
        rec = dict(row)
        home = str(rec.get("home_team") or "").strip()
        away = str(rec.get("away_team") or "").strip()
        comp = str(rec.get("competition") or "").strip()
        try:
            if hasattr(pm, "predict_match_proba"):
                probs = pm.predict_match_proba(ctx, home, away, comp)
                ph, pd_, pa = float(probs["H"]), float(probs["D"]), float(probs["A"])
            elif hasattr(pm, "predict_match"):
                pred = pm.predict_match(ctx, home, away, comp)
                # Various return shapes across versions
                if isinstance(pred, dict):
                    ph = float(pred.get("prob_home") or pred.get("H") or 0.46)
                    pd_ = float(pred.get("prob_draw") or pred.get("D") or 0.26)
                    pa = float(pred.get("prob_away") or pred.get("A") or 0.28)
                    rec["pred_home_goals"] = pred.get("pred_home_goals", rec.get("pred_home_goals", 1.4))
                    rec["pred_away_goals"] = pred.get("pred_away_goals", rec.get("pred_away_goals", 1.1))
                else:
                    ph, pd_, pa = 0.46, 0.26, 0.28
            else:
                ph, pd_, pa = 0.46, 0.26, 0.28
            rec["prob_home"] = round(ph, 6)
            rec["prob_draw"] = round(pd_, 6)
            rec["prob_away"] = round(pa, 6)
            if ph >= pd_ and ph >= pa:
                rec["predicted_result"] = "H"
            elif pa >= pd_ and pa >= ph:
                rec["predicted_result"] = "A"
            else:
                rec["predicted_result"] = "D"
            rec["probability_reasoning"] = f"UEFA model H={ph:.3f} D={pd_:.3f} A={pa:.3f}"
        except Exception:
            rec.setdefault("prob_home", 0.46)
            rec.setdefault("prob_draw", 0.26)
            rec.setdefault("prob_away", 0.28)
            rec.setdefault("predicted_result", "H")
        rows.append(rec)
    return _attach_simple_predictions(pd.DataFrame(rows))


def _init_table_stats():
    return {"P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "GD": 0, "Pts": 0, "PlayedReal": 0, "PlayedPred": 0}


def _apply_result(table, home, away, hg, ag, is_real=False):
    for team in (home, away):
        table.setdefault(team, _init_table_stats())
    table[home]["P"] += 1
    table[away]["P"] += 1
    table[home]["GF"] += hg
    table[home]["GA"] += ag
    table[away]["GF"] += ag
    table[away]["GA"] += hg
    table[home]["GD"] = table[home]["GF"] - table[home]["GA"]
    table[away]["GD"] = table[away]["GF"] - table[away]["GA"]
    flag = "PlayedReal" if is_real else "PlayedPred"
    table[home][flag] += 1
    table[away][flag] += 1
    if hg > ag:
        table[home]["W"] += 1
        table[away]["L"] += 1
        table[home]["Pts"] += 3
    elif ag > hg:
        table[away]["W"] += 1
        table[home]["L"] += 1
        table[away]["Pts"] += 3
    else:
        table[home]["D"] += 1
        table[away]["D"] += 1
        table[home]["Pts"] += 1
        table[away]["Pts"] += 1


def _rank_items(table: dict):
    return sorted(
        table.items(),
        key=lambda kv: (-kv[1]["Pts"], -kv[1]["GD"], -kv[1]["GF"], kv[0]),
    )


def _build_real_and_pending(competition: str, completed: pd.DataFrame, upcoming: pd.DataFrame):
    max_matches = uefa_ko.UEFA_PHASE_MATCHES.get(competition, 8)
    real_table = {}
    played_real = {}
    comp_completed = completed[completed["competition"] == competition] if not completed.empty else completed
    for _, row in (comp_completed or pd.DataFrame()).iterrows():
        if not track._is_league_phase_cup_row(row, competition):
            continue
        home = str(row.get("home_team") or "").strip()
        away = str(row.get("away_team") or "").strip()
        if not track._is_known_team(home) or not track._is_known_team(away):
            continue
        if played_real.get(home, 0) >= max_matches or played_real.get(away, 0) >= max_matches:
            continue
        hg = track._numeric_int(row.get("actual_home_goals"), None)
        ag = track._numeric_int(row.get("actual_away_goals"), None)
        if hg is None or ag is None:
            continue
        _apply_result(real_table, home, away, hg, ag, is_real=True)
        played_real[home] = played_real.get(home, 0) + 1
        played_real[away] = played_real.get(away, 0) + 1

    pending = []
    played = dict(played_real)
    comp_upcoming = upcoming[upcoming["competition"] == competition] if not upcoming.empty else upcoming
    for _, row in (comp_upcoming or pd.DataFrame()).iterrows():
        if not track._is_league_phase_cup_row(row, competition):
            continue
        home = str(row.get("home_team") or "").strip()
        away = str(row.get("away_team") or "").strip()
        if not track._is_known_team(home) or not track._is_known_team(away):
            continue
        if played.get(home, 0) >= max_matches or played.get(away, 0) >= max_matches:
            continue
        pending.append(row)
        played[home] = played.get(home, 0) + 1
        played[away] = played.get(away, 0) + 1
    return real_table, pending, max_matches


def _make_predict_fn(predictions_index, rng_template=None):
    def predict_fn(home, away, rng):
        ph, pd_, pa = track._lookup_match_probs(predictions_index, home, away)
        if ph + pd_ + pa <= 0:
            ph, pd_, pa = 0.46, 0.26, 0.28
        total = ph + pd_ + pa
        pick = float(rng.random()) * total
        if pick < ph:
            result = "H"
            hg, ag = 2, 1
        elif pick < ph + pd_:
            result = "D"
            hg = ag = 1
        else:
            result = "A"
            hg, ag = 1, 2
        return hg, ag, {
            "predicted_result": result,
            "prob_home": ph,
            "prob_draw": pd_,
            "prob_away": pa,
            "pred_home_goals": hg,
            "pred_away_goals": ag,
        }

    return predict_fn


def _table_rows_from_stats(competition, table, pos_counts, runs):
    ranked = _rank_items(table)
    n = max(len(ranked), 1)
    rows = []
    for pos, (team, stats) in enumerate(ranked, start=1):
        probs = track._probability_columns_from_counts(pos_counts, team, n, runs) if pos_counts else {
            "win_league_pct": 0.0,
            "top4_pct": 0.0,
            "bottom3_pct": 0.0,
            "most_likely_position": pos,
            "most_likely_position_pct": 0.0,
            "position_odds_json": json.dumps({str(i): 0.0 for i in range(1, n + 1)}),
        }
        rows.append(
            {
                "competition": competition,
                "position": pos,
                "team": team,
                "P": int(stats.get("P", 0)),
                "W": int(stats.get("W", 0)),
                "D": int(stats.get("D", 0)),
                "L": int(stats.get("L", 0)),
                "GF": int(stats.get("GF", 0)),
                "GA": int(stats.get("GA", 0)),
                "GD": int(stats.get("GD", 0)),
                "Pts": int(stats.get("Pts", 0)),
                "PlayedReal": int(stats.get("PlayedReal", 0)),
                "PlayedPred": int(stats.get("PlayedPred", 0)),
                "sim_runs": int(runs),
                **probs,
            }
        )
    return rows


def project_competition(competition: str, completed: pd.DataFrame, upcoming: pd.DataFrame, rng: np.random.Generator):
    _progress(f"[uefa] START {competition}")
    real_table, pending_rows, max_matches = _build_real_and_pending(competition, completed, upcoming)
    predictions_index = track._build_predictions_index(upcoming)
    predict_fn = _make_predict_fn(predictions_index)

    real_rows = _table_rows_from_stats(competition, real_table, None, 0)
    if not pending_rows:
        _progress(f"[uefa] {competition}: live table only ({len(real_table)} teams, 0 pending)")
        # Still run knockout odds from the live table alone (deterministic ranking).
        finish_counts = uefa_ko.empty_finish_counts(list(real_table.keys()))
        for _ in range(max(1, TABLE_SIMS // 5)):
            ranked = [t for t, _ in _rank_items(real_table)]
            sim = uefa_ko.simulate_knockout_from_table(ranked, rng, predict_fn)
            uefa_ko.accumulate_finish_counts(finish_counts, sim, list(real_table.keys()))
        finish_probs = uefa_ko.finish_counts_to_probabilities(finish_counts, max(1, TABLE_SIMS // 5))
        illustrative = uefa_ko.simulate_knockout_from_table(
            [t for t, _ in _rank_items(real_table)], rng, predict_fn
        )
        return real_rows, real_rows, finish_probs, illustrative, 0

    runs = max(1, TABLE_SIMS)
    _progress(
        f"[uefa] {competition}: {runs} sims, {len(pending_rows)} pending, "
        f"{len(real_table)} teams (cap {max_matches})"
    )
    pos_counts = defaultdict(lambda: defaultdict(int))
    stat_sums = defaultdict(lambda: defaultdict(float))
    finish_counts = uefa_ko.empty_finish_counts(list(real_table.keys()))
    last_sim = None

    for _ in range(runs):
        sim_table = {t: dict(s) for t, s in real_table.items()}
        played = {t: int(s.get("PlayedReal", 0)) for t, s in real_table.items()}
        for row in pending_rows:
            home = str(row.get("home_team") or "").strip()
            away = str(row.get("away_team") or "").strip()
            if played.get(home, 0) >= max_matches or played.get(away, 0) >= max_matches:
                continue
            score = track._sample_fixture_outcome(row, rng, predictions_index)
            if score is None:
                continue
            hg, ag = score
            _apply_result(sim_table, home, away, hg, ag, is_real=False)
            played[home] = played.get(home, 0) + 1
            played[away] = played.get(away, 0) + 1

        ranked_items = _rank_items(sim_table)
        for pos, (team, stats) in enumerate(ranked_items, start=1):
            pos_counts[team][pos] += 1
            for key, value in stats.items():
                try:
                    stat_sums[team][key] += float(value)
                except (TypeError, ValueError):
                    pass

        ranked_names = [t for t, _ in ranked_items]
        for team in ranked_names:
            finish_counts.setdefault(
                team,
                {s: 0 for s in (
                    "winner", "runner_up", "final", "semifinal",
                    "quarterfinal", "round_of_16", "playoff", "missed_knockout",
                )},
            )
        last_sim = uefa_ko.simulate_knockout_from_table(ranked_names, rng, predict_fn)
        uefa_ko.accumulate_finish_counts(finish_counts, last_sim, ranked_names)

    avg_table = {}
    for team, sums in stat_sums.items():
        avg_table[team] = {k: int(round(v / runs)) for k, v in sums.items()}
        if team in real_table:
            avg_table[team]["PlayedReal"] = real_table[team].get("PlayedReal", 0)
    projected_rows = _table_rows_from_stats(competition, avg_table, pos_counts, runs)
    finish_probs = uefa_ko.finish_counts_to_probabilities(finish_counts, runs)
    _progress(f"[uefa] DONE  {competition}")
    return projected_rows, real_rows, finish_probs, last_sim, runs


def _bracket_payload(competition, finish_probs, illustrative, sims, phase_matches):
    teams_odds = [
        {
            "team": team,
            **stages,
        }
        for team, stages in sorted(
            (finish_probs or {}).items(),
            key=lambda kv: (-kv[1].get("winner", 0), -kv[1].get("final", 0), kv[0]),
        )
    ]
    champ = None
    if teams_odds:
        champ = teams_odds[0]["team"]
    round_reach = uefa_ko.finish_probs_to_round_reach(finish_probs or {})
    elimination = uefa_ko.finish_probs_to_elimination(finish_probs or {})
    return {
        "competition": competition,
        "format": "uefa_league_phase_knockout",
        "league_phase_matches": phase_matches,
        "simulations_run": int(sims),
        "champion": champ or (illustrative or {}).get("champion"),
        "display": "knockout_finish_odds",
        "qualification": {
            "round_of_16": "Positions 1-8 (skip playoff)",
            "knockout_playoff": "Positions 9-24 (two-legged)",
        },
        "draw_rules": {
            "playoff_bands": [
                "9-10 vs 23-24 (random pairing)",
                "11-12 vs 21-22",
                "13-14 vs 19-20",
                "15-16 vs 17-18",
            ],
            "second_leg_home": "Higher league-phase seed",
            "aggregate_tie": "Random (penalties)",
            "round_of_16": (
                "Seeds 1-2 opposite halves; face winners of 9/10 vs 23/24 ties. "
                "Seeds 3-4 cannot meet 1-2 before SF."
            ),
        },
        "knockout_finish_probabilities": finish_probs,
        "teams": teams_odds,
        "winner_probabilities": {
            team: stages.get("winner", 0.0) for team, stages in (finish_probs or {}).items()
        },
        "round_reach_probabilities": round_reach,
        "elimination_round_probabilities": elimination,
        "illustrative_simulation": {
            "champion": (illustrative or {}).get("champion"),
            "runner_up": (illustrative or {}).get("runner_up"),
            "final": (illustrative or {}).get("final"),
        },
        # Keep a lightweight rounds stub so older UI code does not crash.
        "rounds": [],
    }


def refresh_uefa_cup_projections() -> dict:
    """Fetch ESPN data, predict, project tables + knockout odds for CL/EL/ECL."""
    t0 = time.monotonic()
    _progress("[uefa] START Project_UEFA_Cups")
    PREDICTIONS_DIR.mkdir(parents=True, exist_ok=True)

    mapping = track.load_shared_mapping()

    # 1) Completed league-phase backfill
    try:
        backfill_df, maps, unresolved, seen = track.fetch_in_season_cup_table_results(mapping)
        if maps:
            mapping, _, _ = track.apply_mapping_updates(mapping, maps)
            track.save_mapping(track.SHARED_MAPPING_FILE, mapping)
    except Exception as exc:
        _progress(f"[uefa] completed backfill failed: {exc}")
        backfill_df = _empty_history()

    existing_completed = _load_csv(COMPLETED_FILE)
    completed = track.merge_completed_cup_frames(existing_completed, backfill_df)
    completed = track._filter_frame_to_cup_table_season(completed)
    uefa_completed = _uefa_only(completed)
    _progress(f"[uefa] completed UEFA phase rows: {len(uefa_completed)}")

    # 2) Pending fixtures
    try:
        pending_df = track.fetch_upcoming_cup_table_fixtures(mapping)
    except Exception as exc:
        _progress(f"[uefa] pending fetch failed: {exc}")
        pending_df = _empty_history()
    pending_df = _uefa_only(pending_df)
    pending_df = _try_model_predict_pending(pending_df)
    _progress(f"[uefa] pending UEFA fixtures with predictions: {len(pending_df)}")

    # Merge upcoming: keep non-UEFA rows from disk, replace UEFA rows.
    existing_upcoming = _load_csv(UPCOMING_FILE)
    other_upcoming = _non_uefa(existing_upcoming)
    upcoming = pd.concat([other_upcoming, pending_df], ignore_index=True) if not pending_df.empty else other_upcoming
    upcoming = track._ensure_columns(upcoming, track.CUP_HISTORY_COLUMNS)

    # Persist history files
    other_completed = _non_uefa(existing_completed)
    completed_out = pd.concat([other_completed, uefa_completed], ignore_index=True)
    completed_out = track.merge_completed_cup_frames(completed_out, uefa_completed)
    track._write_csv(str(COMPLETED_FILE), completed_out, track.CUP_HISTORY_COLUMNS)
    track._write_csv(str(UPCOMING_FILE), upcoming, track.CUP_HISTORY_COLUMNS)

    rng = np.random.default_rng(RNG_SEED)
    all_projected = []
    all_real = []
    brackets = {
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "competitions": {},
        "uefa_engine": "Project_UEFA_Cups",
    }

    for competition in uefa_ko.UEFA_COMPETITIONS:
        projected, real, finish_probs, illustrative, sims = project_competition(
            competition, uefa_completed, pending_df, rng
        )
        all_projected.extend(projected)
        all_real.extend(real)
        brackets["competitions"][competition] = _bracket_payload(
            competition,
            finish_probs,
            illustrative,
            sims if sims > 0 else max(1, TABLE_SIMS // 5),
            uefa_ko.UEFA_PHASE_MATCHES.get(competition, 8),
        )

    # Merge tables: replace UEFA comps, keep other cups.
    existing_tables = (
        pd.read_csv(TABLES_FILE) if TABLES_FILE.is_file() else track._empty_frame(track.TABLE_COLUMNS)
    )
    existing_real = (
        pd.read_csv(REAL_TABLES_FILE) if REAL_TABLES_FILE.is_file() else track._empty_frame(track.TABLE_COLUMNS)
    )
    for frame_name, existing, new_rows, path in (
        ("projected", existing_tables, all_projected, TABLES_FILE),
        ("real", existing_real, all_real, REAL_TABLES_FILE),
    ):
        keep = existing[~existing["competition"].astype(str).isin(uefa_ko.UEFA_COMPETITIONS)] if not existing.empty and "competition" in existing.columns else existing
        merged = pd.concat(
            [keep, track._ensure_columns(pd.DataFrame(new_rows), track.TABLE_COLUMNS)],
            ignore_index=True,
        )
        track._write_csv(str(path), merged, track.TABLE_COLUMNS)
        _progress(f"[uefa] wrote {frame_name} tables → {path} ({len(new_rows)} UEFA rows)")

    # Merge brackets JSON
    existing_brackets = {}
    if BRACKETS_FILE.is_file():
        try:
            existing_brackets = json.loads(BRACKETS_FILE.read_text(encoding="utf-8"))
        except Exception:
            existing_brackets = {}
    comps = dict(existing_brackets.get("competitions") or {})
    comps.update(brackets["competitions"])
    payload = {
        "generated_at_utc": brackets["generated_at_utc"],
        "competitions": comps,
        "uefa_engine": "Project_UEFA_Cups",
        "table_sims": TABLE_SIMS,
    }
    BRACKETS_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    _progress(f"[uefa] wrote brackets → {BRACKETS_FILE}")
    _progress(f"[uefa] DONE in {time.monotonic() - t0:.1f}s")
    return {
        "upcoming": len(pending_df),
        "completed": len(uefa_completed),
        "projected_rows": len(all_projected),
        "competitions": list(uefa_ko.UEFA_COMPETITIONS),
    }


def main():
    summary = refresh_uefa_cup_projections()
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
