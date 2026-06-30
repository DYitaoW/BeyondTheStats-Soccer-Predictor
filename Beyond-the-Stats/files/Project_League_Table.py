"""
Monte Carlo league table projections using the trained match model.

Sub-pipeline step after ``Predict_Upcoming_Matchweek``.  For each competition:
1. Loads the processed match history (past results) + upcoming fixture list
2. Uses the model cache to simulate remaining fixtures (1000+ iterations)
3. Aggregates simulation results into projected final tables (mean pts / GD / etc.)
4. Outputs ``{competition}_projected_league_table.json``

The same tiebreaker logic from ``Process_Data`` is applied within simulations
so projected standings correctly break ties per-league rules.
"""
import argparse
import os
import json
import multiprocessing as mp
from collections import defaultdict
from datetime import datetime
import random
import subprocess
import sys
import time

import joblib
import pandas as pd

import Predict_Match as pm


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(BASE_DIR, "Data", "Raw_Data")
OUT_DIR = os.path.join(BASE_DIR, "Data", "Predictions")
OUT_TABLE = os.path.join(OUT_DIR, "projected_league_tables.csv")
OUT_MATCHES = os.path.join(OUT_DIR, "projected_future_matches.csv")
RNG = random.Random()
SIMULATION_RUNS = 2500


def rebuild_model_cache_once():
    """Rebuild the model cache in non-interactive mode for dtype/pickle compatibility."""
    predict_script = os.path.join(FILES_DIR, "Predict_Match.py")
    if not os.path.exists(predict_script):
        raise FileNotFoundError(f"Missing predictor script: {predict_script}")
    proc = subprocess.run(
        [sys.executable, predict_script],
        cwd=BASE_DIR,
        text=True,
        input="n\nq\n",
        capture_output=True,
        check=False,
        timeout=3600,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        message = stderr or stdout or f"exit code {proc.returncode}"
        raise RuntimeError(f"Auto-rebuild of model cache failed: {message}")


def latest_raw_file_per_competition(raw_root):
    latest = {}
    for root, _, files in os.walk(raw_root):
        for name in files:
            if not name.endswith(".csv"):
                continue
            start_year = pm.parse_season_start_year(name)
            if start_year is None:
                continue
            full_path = os.path.join(root, name)
            rel_path = os.path.relpath(full_path, raw_root)
            competition = os.path.dirname(rel_path).replace("\\", "/") or "Unknown"
            current = latest.get(competition)
            if current is None or start_year > current[0]:
                latest[competition] = (start_year, full_path)
    return {comp: path for comp, (_, path) in latest.items()}


def load_context():
    matches, season_files = pm.load_training_matches(pm.PROCESSED_DIR)
    if not os.path.exists(pm.MODEL_CACHE):
        raise FileNotFoundError(f"Missing model cache: {pm.MODEL_CACHE}. Run Predict_Match.py first.")

    try:
        bundle = joblib.load(pm.MODEL_CACHE)
    except Exception as exc:
        print(f"[warn] Failed to load model cache ({exc.__class__.__name__}). Rebuilding cache once...")
        rebuild_model_cache_once()
        bundle = joblib.load(pm.MODEL_CACHE)
    if bundle.get("fingerprint") != pm.data_fingerprint(season_files):
        bt = bundle.get("build_time")
        if bt is None or (time.time() - bt) >= 604800:
            raise RuntimeError("Model cache is stale. Rebuild by running Predict_Match.py.")

    overall_teams = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "overall_teams.json"))
    season_teams = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "season_teams.json"))
    head_to_head = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "head_to_head.json"))
    current_form = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "current_form.json"))
    league_strength = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "league_strength.json")) or {}
    dynamic_form = pm.build_dynamic_form_from_matches(matches)

    if (
        overall_teams is None
        or season_teams is None
        or head_to_head is None
        or current_form is None
        or not isinstance(overall_teams, dict)
        or len(overall_teams) == 0
    ):
        overall_teams, season_teams, head_to_head, current_form = pm.build_fallback_data(matches, season_files)

    overall_teams = pm.replace_nan_with_sentinel(overall_teams)
    season_teams = pm.replace_nan_with_sentinel(season_teams)
    head_to_head = pm.replace_nan_with_sentinel(head_to_head)
    current_form = pm.replace_nan_with_sentinel(current_form)
    league_strength = pm.replace_nan_with_sentinel(league_strength)
    current_form.setdefault("teams", {})
    for team, stats in dynamic_form.items():
        if team not in current_form["teams"] or not isinstance(current_form["teams"].get(team), dict):
            current_form["teams"][team] = stats
            continue
        existing = current_form["teams"][team]
        for key, value in stats.items():
            if key not in existing or existing.get(key) in (None, "", 0, 0.0):
                existing[key] = value

    team_comp_map = {}
    for _, row in matches.iterrows():
        team_comp_map[row["HomeTeam"]] = row["competition"]
        team_comp_map[row["AwayTeam"]] = row["competition"]

    latest_start = max(pm.parse_start_year_from_key(k) for k in season_teams.keys())
    latest_season = season_files[-1].replace(".csv", "")
    available = sorted(set(matches["HomeTeam"].dropna()) | set(matches["AwayTeam"].dropna()))

    return {
        "clf": bundle["clf"],
        "result_le": bundle["result_label_encoder"],
        "home_goal_reg": bundle["home_goal_reg"],
        "away_goal_reg": bundle["away_goal_reg"],
        "train_columns": bundle["train_columns"],
        "overall_teams": overall_teams,
        "season_teams": season_teams,
        "head_to_head": head_to_head,
        "current_form": current_form,
        "league_strength": league_strength,
        "team_comp_map": team_comp_map,
        "latest_start": latest_start,
        "latest_season": latest_season,
        "available_teams": available,
    }


def init_table(teams):
    table = {}
    for t in sorted(teams):
        table[t] = {
            "P": 0,
            "W": 0,
            "D": 0,
            "L": 0,
            "GF": 0,
            "GA": 0,
            "GD": 0,
            "Pts": 0,
            "PlayedReal": 0,
            "PlayedPred": 0,
        }
    return table


def apply_result(table, home, away, hg, ag, is_real):
    hs = table.setdefault(home, {"P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "GD": 0, "Pts": 0, "PlayedReal": 0, "PlayedPred": 0})
    as_ = table.setdefault(away, {"P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "GD": 0, "Pts": 0, "PlayedReal": 0, "PlayedPred": 0})

    hs["P"] += 1
    as_["P"] += 1
    hs["GF"] += int(hg)
    hs["GA"] += int(ag)
    as_["GF"] += int(ag)
    as_["GA"] += int(hg)
    hs["GD"] = hs["GF"] - hs["GA"]
    as_["GD"] = as_["GF"] - as_["GA"]
    if is_real:
        hs["PlayedReal"] += 1
        as_["PlayedReal"] += 1
    else:
        hs["PlayedPred"] += 1
        as_["PlayedPred"] += 1

    if hg > ag:
        hs["W"] += 1
        as_["L"] += 1
        hs["Pts"] += 3
    elif ag > hg:
        as_["W"] += 1
        hs["L"] += 1
        as_["Pts"] += 3
    else:
        hs["D"] += 1
        as_["D"] += 1
        hs["Pts"] += 1
        as_["Pts"] += 1


def clone_table(table):
    return {
        team: {
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
        }
        for team, stats in table.items()
    }


# Leagues where the first tiebreaker is head-to-head (points > GD > GF) then overall GD > GF > name.
H2H_LEAGUES = {
    "Spain/La Liga", "Spain/La Liga 2",
    "Italy/Serie A", "Italy/Serie B",
    "Portugal/Liga Portugal",
    "Belgium/First Division A",
    "Turkey/Super Lig",
}


def _compute_h2h_scores(tied_teams, all_matches):
    """Return per-team {pts, gd, gf} among the tied group from all_matches."""
    scores = {t: {"pts": 0, "gd": 0, "gf": 0} for t in tied_teams}
    team_set = set(tied_teams)
    for home, away, hg, ag in all_matches:
        if home in team_set and away in team_set:
            if hg > ag:
                scores[home]["pts"] += 3
            elif ag > hg:
                scores[away]["pts"] += 3
            else:
                scores[home]["pts"] += 1
                scores[away]["pts"] += 1
            scores[home]["gd"] += hg - ag
            scores[home]["gf"] += hg
            scores[away]["gd"] += ag - hg
            scores[away]["gf"] += ag
    return scores


def rank_table(table, competition=None, all_matches=None):
    use_h2h = competition in H2H_LEAGUES and all_matches is not None
    if not use_h2h:
        return sorted(table.items(), key=lambda kv: (-kv[1]["Pts"], -kv[1]["GD"], -kv[1]["GF"], kv[0]))

    pts_groups = defaultdict(list)
    for team, stats in table.items():
        pts_groups[stats["Pts"]].append(team)

    result = []
    for pts in sorted(pts_groups, reverse=True):
        tied = pts_groups[pts]
        if len(tied) == 1:
            result.append((tied[0], table[tied[0]]))
        else:
            h2h = _compute_h2h_scores(tied, all_matches)
            sorted_tied = sorted(
                tied,
                key=lambda t: (
                    -h2h[t]["pts"],
                    -h2h[t]["gd"],
                    -h2h[t]["gf"],
                    -table[t]["GD"],
                    -table[t]["GF"],
                    t,
                ),
            )
            for team in sorted_tied:
                result.append((team, table[team]))
    return result


def sample_outcome(probs):
    labels = ["H", "D", "A"]
    weights = [max(0.0, float(probs.get(label, 0.0))) for label in labels]
    total = sum(weights)
    if total <= 0:
        return max(probs, key=probs.get)
    return RNG.choices(labels, weights=weights, k=1)[0]


def coerce_scoreline(pred_result, base_hg, base_ag):
    hg = int(round(float(base_hg)))
    ag = int(round(float(base_ag)))
    hg = max(0, hg)
    ag = max(0, ag)
    if pred_result == "H" and hg <= ag:
        hg = ag + 1
    elif pred_result == "A" and ag <= hg:
        ag = hg + 1
    elif pred_result == "D":
        ag = hg
    return hg, ag


def run_monte_carlo(teams, base_table, future_predictions, runs, competition=None, real_matches=None):
    stat_sums = {team: defaultdict(float) for team in teams}
    position_counts = {team: defaultdict(int) for team in teams}

    for _ in range(max(1, int(runs))):
        sim_table = clone_table(base_table)
        sim_matches = list(real_matches) if real_matches else []
        for fixture in future_predictions:
            result = sample_outcome(fixture["probs"])
            hg, ag = coerce_scoreline(result, fixture["pred_home_goals"], fixture["pred_away_goals"])
            apply_result(sim_table, fixture["home_team"], fixture["away_team"], hg, ag, is_real=False)
            sim_matches.append((fixture["home_team"], fixture["away_team"], hg, ag))

        ranked = rank_table(sim_table, competition=competition, all_matches=sim_matches if competition in H2H_LEAGUES else None)
        for pos, (team, stats) in enumerate(ranked, start=1):
            position_counts[team][pos] += 1
            for key, value in stats.items():
                stat_sums[team][key] += float(value)

    return stat_sums, position_counts


def predict_match(ctx, home_team, away_team, competition_hint):
    return _predict_matches_batch(ctx, [(home_team, away_team)], competition_hint)[0]


def _predict_matches_batch(ctx, fixture_pairs, competition_hint):
    """Predict a batch of (home, away) fixtures sharing the same competition hint.

    Returns a list of (pred_res, hg, ag, probs) tuples in input order.
    """
    if not fixture_pairs:
        return []

    n = len(fixture_pairs)
    batch_input = pd.concat(
        [pm.build_match_input(h, a) for h, a in fixture_pairs], ignore_index=True
    )

    prediction_season = pm.choose_season_for_teams(
        fixture_pairs[0][0], fixture_pairs[0][1], ctx["season_teams"], ctx["latest_season"]
    )
    competition_key = os.path.dirname(prediction_season).replace("\\", "/") or competition_hint
    start_year = pm.parse_start_year_from_key(prediction_season)
    season_coeff = pm.season_recency_coefficient(ctx["latest_start"], start_year)
    home_comp = ctx["team_comp_map"].get(fixture_pairs[0][0], competition_key)
    away_comp = ctx["team_comp_map"].get(fixture_pairs[0][1], competition_key)

    X = pm.build_features(
        batch_input,
        prediction_season,
        competition_key,
        season_coeff,
        ctx["overall_teams"],
        ctx["season_teams"],
        ctx["head_to_head"],
        ctx["current_form"],
        ctx["league_strength"],
        home_competition_override=home_comp,
        away_competition_override=away_comp,
    )
    X = pd.get_dummies(X, columns=["competition"], dtype=float)
    X = X.reindex(columns=ctx["train_columns"], fill_value=0.0)

    pvals = ctx["clf"].predict_proba(X)
    hg_batch = ctx["home_goal_reg"].predict(X)
    ag_batch = ctx["away_goal_reg"].predict(X)

    classes = list(ctx["clf"].classes_)
    class_labels = [ctx["result_le"].inverse_transform([enc])[0] for enc in classes]

    results = []
    for i in range(n):
        probs = {"H": 0.0, "D": 0.0, "A": 0.0}
        for c_idx, lbl in enumerate(class_labels):
            probs[lbl] = float(pvals[i, c_idx])
        probs = pm.reduce_draw_probability(probs)

        labels = ["H", "D", "A"]
        weights = [max(0.0, float(probs.get(label, 0.0))) for label in labels]
        total = sum(weights)
        if total <= 0:
            pred_res = max(probs, key=probs.get)
        else:
            pred_res = RNG.choices(labels, weights=weights, k=1)[0]
        hg = int(round(max(0.0, float(hg_batch[i]))))
        ag = int(round(max(0.0, float(ag_batch[i]))))

        if pred_res == "H" and hg <= ag:
            hg = ag + 1
        elif pred_res == "A" and ag <= hg:
            ag = hg + 1
        elif pred_res == "D":
            ag = hg

        results.append((pred_res, hg, ag, probs))
    return results


def project_competition(ctx, competition, raw_file, sim_runs=None):
    if sim_runs is None:
        sim_runs = SIMULATION_RUNS
    df = pd.read_csv(raw_file).copy()
    required = {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"}
    if not required.issubset(df.columns):
        return [], []

    df["DateParsed"] = pd.to_datetime(df["Date"], dayfirst=True, format="mixed", errors="coerce")
    df = df[df["HomeTeam"].notna() & df["AwayTeam"].notna()]
    df = df.sort_values(["DateParsed", "HomeTeam", "AwayTeam"], na_position="last").reset_index(drop=True)

    teams = sorted(set(df["HomeTeam"].astype(str).str.strip()) | set(df["AwayTeam"].astype(str).str.strip()))
    table = init_table(teams)
    real_matches = []
    future_pairs = []
    future_dates = []
    seen_pairs = set()

    for _, row in df.iterrows():
        raw_home = str(row["HomeTeam"]).strip()
        raw_away = str(row["AwayTeam"]).strip()
        home = pm.resolve_team_name(raw_home, ctx["available_teams"]) or raw_home
        away = pm.resolve_team_name(raw_away, ctx["available_teams"]) or raw_away
        seen_pairs.add((home, away))
        ftr = str(row.get("FTR", "")).strip()
        hg = pd.to_numeric(row.get("FTHG"), errors="coerce")
        ag = pd.to_numeric(row.get("FTAG"), errors="coerce")
        is_played = ftr in {"H", "D", "A"} and pd.notna(hg) and pd.notna(ag)

        if is_played:
            apply_result(table, home, away, int(hg), int(ag), is_real=True)
            real_matches.append((home, away, int(hg), int(ag)))
            continue

        future_pairs.append((home, away))
        future_dates.append(row["DateParsed"].date().isoformat() if pd.notna(row["DateParsed"]) else "")

    # If the feed only has played matches, synthesize missing league fixtures so
    # the projection represents a full double round-robin season.
    for home in teams:
        resolved_home = pm.resolve_team_name(home, ctx["available_teams"]) or home
        for away in teams:
            if home == away:
                continue
            resolved_away = pm.resolve_team_name(away, ctx["available_teams"]) or away
            if (resolved_home, resolved_away) in seen_pairs:
                continue
            seen_pairs.add((resolved_home, resolved_away))
            future_pairs.append((resolved_home, resolved_away))
            future_dates.append("")

    # Batch all future-fixture predictions into one vectorized call instead of N single-row
    # calls — this avoids per-fixture pd.get_dummies/reindex/predict_proba overhead.
    batched = _predict_matches_batch(ctx, future_pairs, competition) if future_pairs else []

    future_predictions = []
    future_rows = []
    for i, (pred_res, phg, pag, probs) in enumerate(batched):
        home, away = future_pairs[i]
        match_date = future_dates[i]
        future_predictions.append(
            {
                "home_team": home,
                "away_team": away,
                "pred_home_goals": phg,
                "pred_away_goals": pag,
                "probs": probs,
            }
        )
        future_rows.append(
            {
                "competition": competition,
                "match_date": match_date,
                "home_team": home,
                "away_team": away,
                "predicted_result": pred_res,
                "pred_home_goals": phg,
                "pred_away_goals": pag,
                "prob_home": round(probs["H"], 6),
                "prob_draw": round(probs["D"], 6),
                "prob_away": round(probs["A"], 6),
            }
        )

    stat_sums, position_counts = run_monte_carlo(teams, table, future_predictions, sim_runs, competition=competition, real_matches=real_matches)
    averaged = {}
    for team in teams:
        sums = stat_sums.get(team, {})
        averaged[team] = {
            "P": int(round(sums.get("P", 0.0) / sim_runs)),
            "W": int(round(sums.get("W", 0.0) / sim_runs)),
            "D": int(round(sums.get("D", 0.0) / sim_runs)),
            "L": int(round(sums.get("L", 0.0) / sim_runs)),
            "GF": int(round(sums.get("GF", 0.0) / sim_runs)),
            "GA": int(round(sums.get("GA", 0.0) / sim_runs)),
            "GD": int(round(sums.get("GD", 0.0) / sim_runs)),
            "Pts": int(round(sums.get("Pts", 0.0) / sim_runs)),
            "PlayedReal": int(round(sums.get("PlayedReal", 0.0) / sim_runs)),
            "PlayedPred": int(round(sums.get("PlayedPred", 0.0) / sim_runs)),
        }

    out_rows = []
    ranked = sorted(averaged.items(), key=lambda kv: (-kv[1]["Pts"], -kv[1]["GD"], -kv[1]["GF"], kv[0]))
    n_teams = len(teams)
    top_n = min(4, n_teams)
    bottom_cutoff = max(1, n_teams - 2)
    for pos, (team, s) in enumerate(ranked, start=1):
        team_positions = position_counts.get(team, {})
        most_likely_pos, most_likely_count = max(team_positions.items(), key=lambda kv: kv[1], default=(pos, 0))
        win_league_pct = (team_positions.get(1, 0) / sim_runs) * 100.0
        top4_pct = (sum(v for k, v in team_positions.items() if k <= top_n) / sim_runs) * 100.0
        bottom3_pct = (sum(v for k, v in team_positions.items() if k >= bottom_cutoff) / sim_runs) * 100.0
        position_odds = {
            str(rank): round((team_positions.get(rank, 0) / sim_runs) * 100.0, 2)
            for rank in range(1, n_teams + 1)
        }
        out_rows.append(
            {
                "competition": competition,
                "position": pos,
                "team": team,
                **s,
                "win_league_pct": round(win_league_pct, 2),
                "top4_pct": round(top4_pct, 2),
                "bottom3_pct": round(bottom3_pct, 2),
                "most_likely_position": int(most_likely_pos),
                "most_likely_position_pct": round((most_likely_count / sim_runs) * 100.0, 2),
                "position_odds_json": json.dumps(position_odds, separators=(",", ":"), sort_keys=True),
                "sim_runs": int(sim_runs),
            }
        )
    return out_rows, future_rows


def parse_cli_args():
    parser = argparse.ArgumentParser(description="Project remaining fixtures onto a Monte-Carlo season.")
    parser.add_argument(
        "--sim-runs",
        type=int,
        default=SIMULATION_RUNS,
        help=f"Number of Monte-Carlo simulations per competition (default: {SIMULATION_RUNS}).",
    )
    parser.add_argument(
        "--competition-workers",
        type=int,
        default=0,
        help=(
            "Reserved for future per-competition parallelism. "
            "Currently the inner sim loop is the bottleneck; batching predict_match "
            "already covers the easier win."
        ),
    )
    return parser.parse_args()


def main():
    args = parse_cli_args()
    sim_runs = max(1, int(args.sim_runs))
    ctx = load_context()
    latest = latest_raw_file_per_competition(RAW_DIR)
    if not latest:
        raise ValueError(f"No raw season files found in {RAW_DIR}")

    all_tables = []
    all_future = []
    for competition, path in sorted(latest.items()):
        table_rows, future_rows = project_competition(ctx, competition, path, sim_runs)
        all_tables.extend(table_rows)
        all_future.extend(future_rows)

    os.makedirs(OUT_DIR, exist_ok=True)
    pd.DataFrame(all_tables).to_csv(OUT_TABLE, index=False)
    pd.DataFrame(all_future).to_csv(OUT_MATCHES, index=False)
    print(f"Projected league tables saved: {OUT_TABLE}")
    print(f"Predicted remaining matches saved: {OUT_MATCHES}")


if __name__ == "__main__":
    main()
