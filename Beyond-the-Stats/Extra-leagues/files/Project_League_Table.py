import os
import json
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo
import random
import subprocess
import sys
import time

import joblib
import pandas as pd

import Predict_Match as pm


class AveragedProbaClassifier:
    # Cache compatibility shim for previously pickled wrappers.
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


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(BASE_DIR, "Data", "Raw_Data")
OUT_DIR = os.path.join(BASE_DIR, "Data", "Predictions")
OUT_TABLE = os.path.join(OUT_DIR, "projected_league_tables.csv")
OUT_MATCHES = os.path.join(OUT_DIR, "projected_future_matches.csv")
RNG = random.Random()
SIMULATION_RUNS = 2500
ESPN_SCOREBOARD_API = "https://site.api.espn.com/apis/site/v2/sports/soccer/{espn_id}/scoreboard"
EASTERN_TZ = ZoneInfo("America/New_York")
EXTRA_ESPN_COMPETITIONS = {
    "Mexico/Liga MX": "mex.1",
    "Argentina/Primera Division": "arg.1",
    "Brazil/Serie A": "bra.1",
    "Japan/J1 League": "jpn.1",
    "Austria/Bundesliga": "aut.1",
    "Romania/Liga I": "rou.1",
    "Poland/Ekstraklasa": "pol.1",
}


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


def fetch_json(url, timeout=30):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_raw_df(df):
    frame = df.copy()
    if {"HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"}.issubset(frame.columns):
        return frame
    if {"Home", "Away", "HG", "AG", "Res"}.issubset(frame.columns):
        out = frame.copy()
        out["HomeTeam"] = out["Home"].astype(str).str.strip()
        out["AwayTeam"] = out["Away"].astype(str).str.strip()
        out["FTHG"] = pd.to_numeric(out["HG"], errors="coerce")
        out["FTAG"] = pd.to_numeric(out["AG"], errors="coerce")
        out["FTR"] = out["Res"].astype(str).str.strip().str.upper()
        if "Date" in out.columns:
            out["Date"] = out["Date"]
        return out
    return frame


def load_future_fixtures_from_espn(competition, year=None):
    espn_id = EXTRA_ESPN_COMPETITIONS.get(competition)
    if not espn_id:
        return pd.DataFrame()

    target_year = int(year or datetime.now(UTC).year)
    start = pd.Timestamp(f"{target_year}-01-01")
    end = pd.Timestamp(f"{target_year}-12-31")
    today = pd.Timestamp(datetime.now(UTC).date())
    rows = []
    seen = set()
    day = start
    while day <= end:
        url = ESPN_SCOREBOARD_API.format(espn_id=espn_id) + f"?dates={day.strftime('%Y%m%d')}"
        try:
            data = fetch_json(url, timeout=20)
        except Exception:
            day += pd.Timedelta(days=1)
            continue

        for event in data.get("events", []) or []:
            event_date = pd.to_datetime(event.get("date"), utc=True, errors="coerce")
            if pd.isna(event_date):
                continue
            match_date = event_date.tz_convert(EASTERN_TZ).tz_localize(None).normalize()
            if match_date.year != target_year or match_date < today:
                continue

            competitions = event.get("competitions", [])
            if not competitions:
                continue
            comp0 = competitions[0] or {}
            status_state = (
                ((comp0.get("status") or {}).get("type") or {}).get("state", "")
            ).strip().lower()
            if status_state and status_state not in {"pre"}:
                continue

            home_team = ""
            away_team = ""
            for competitor in comp0.get("competitors", []) or []:
                team_name = ((competitor.get("team") or {}).get("displayName") or "").strip()
                side = str(competitor.get("homeAway", "")).strip().lower()
                if side == "home":
                    home_team = team_name
                elif side == "away":
                    away_team = team_name
            if not home_team or not away_team:
                continue

            key = (match_date.date().isoformat(), home_team, away_team)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "Date": match_date.date().isoformat(),
                    "HomeTeam": home_team,
                    "AwayTeam": away_team,
                    "FTHG": None,
                    "FTAG": None,
                    "FTR": "",
                }
            )

        day += pd.Timedelta(days=1)

    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame["DateParsed"] = pd.to_datetime(frame["Date"], errors="coerce")
    return frame


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


def rank_table(table):
    return sorted(table.items(), key=lambda kv: (-kv[1]["Pts"], -kv[1]["GD"], -kv[1]["GF"], kv[0]))


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


def run_monte_carlo(teams, base_table, future_predictions, runs):
    stat_sums = {team: defaultdict(float) for team in teams}
    position_counts = {team: defaultdict(int) for team in teams}

    for _ in range(max(1, int(runs))):
        sim_table = clone_table(base_table)
        for fixture in future_predictions:
            result = sample_outcome(fixture["probs"])
            hg, ag = coerce_scoreline(result, fixture["pred_home_goals"], fixture["pred_away_goals"])
            apply_result(sim_table, fixture["home_team"], fixture["away_team"], hg, ag, is_real=False)

        ranked = rank_table(sim_table)
        for pos, (team, stats) in enumerate(ranked, start=1):
            position_counts[team][pos] += 1
            for key, value in stats.items():
                stat_sums[team][key] += float(value)

    return stat_sums, position_counts


def predict_match(ctx, home_team, away_team, competition_hint):
    prediction_season = pm.choose_season_for_teams(home_team, away_team, ctx["season_teams"], ctx["latest_season"])
    competition_key = os.path.dirname(prediction_season).replace("\\", "/") or competition_hint
    start_year = pm.parse_start_year_from_key(prediction_season)
    season_coeff = pm.season_recency_coefficient(ctx["latest_start"], start_year)
    home_comp = ctx["team_comp_map"].get(home_team, competition_key)
    away_comp = ctx["team_comp_map"].get(away_team, competition_key)

    X = pm.build_features(
        pm.build_match_input(home_team, away_team),
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

    probs = {"H": 0.0, "D": 0.0, "A": 0.0}
    pvals = ctx["clf"].predict_proba(X)[0]
    for idx, enc in enumerate(ctx["clf"].classes_):
        lbl = ctx["result_le"].inverse_transform([enc])[0]
        probs[lbl] = float(pvals[idx])
    probs = pm.reduce_draw_probability(probs)

    labels = ["H", "D", "A"]
    weights = [max(0.0, float(probs.get(label, 0.0))) for label in labels]
    total = sum(weights)
    if total <= 0:
        pred_res = max(probs, key=probs.get)
    else:
        pred_res = RNG.choices(labels, weights=weights, k=1)[0]
    phg = max(0.0, float(ctx["home_goal_reg"].predict(X)[0]))
    pag = max(0.0, float(ctx["away_goal_reg"].predict(X)[0]))
    hg = int(round(phg))
    ag = int(round(pag))

    # Keep scoreline direction consistent with predicted result label.
    if pred_res == "H" and hg <= ag:
        hg = ag + 1
    elif pred_res == "A" and ag <= hg:
        ag = hg + 1
    elif pred_res == "D":
        ag = hg

    return pred_res, hg, ag, probs


def project_competition(ctx, competition, raw_file):
    df = normalize_raw_df(pd.read_csv(raw_file))
    required = {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"}
    if not required.issubset(df.columns):
        return [], []

    espn_future = load_future_fixtures_from_espn(competition)
    if not espn_future.empty:
        espn_future = espn_future.copy()
        espn_future["DateParsed"] = pd.to_datetime(espn_future["Date"], errors="coerce")
        df["DateParsed"] = pd.to_datetime(df["Date"], dayfirst=True, format="mixed", errors="coerce")
        played_keys = set()
        for _, row in df.iterrows():
            home = str(row.get("HomeTeam", "")).strip()
            away = str(row.get("AwayTeam", "")).strip()
            ftr = str(row.get("FTR", "")).strip().upper()
            hg = pd.to_numeric(row.get("FTHG"), errors="coerce")
            ag = pd.to_numeric(row.get("FTAG"), errors="coerce")
            if home and away and ftr in {"H", "D", "A"} and pd.notna(hg) and pd.notna(ag):
                played_keys.add(tuple(sorted([home, away])))
        supplemental = []
        for _, row in espn_future.iterrows():
            home = str(row.get("HomeTeam", "")).strip()
            away = str(row.get("AwayTeam", "")).strip()
            if not home or not away:
                continue
            if tuple(sorted([home, away])) in played_keys:
                continue
            supplemental.append(row)
        if supplemental:
            df = pd.concat([df, pd.DataFrame(supplemental)], ignore_index=True, sort=False)

    df["DateParsed"] = pd.to_datetime(df["Date"], dayfirst=True, format="mixed", errors="coerce")
    df = df[df["HomeTeam"].notna() & df["AwayTeam"].notna()]
    df = df.sort_values(["DateParsed", "HomeTeam", "AwayTeam"], na_position="last").reset_index(drop=True)

    teams = sorted(set(df["HomeTeam"].astype(str).str.strip()) | set(df["AwayTeam"].astype(str).str.strip()))
    table = init_table(teams)
    future_rows = []
    future_predictions = []
    seen_pairs = set()

    def add_future_prediction(home, away, match_date=""):
        pred_res, phg, pag, probs = predict_match(ctx, home, away, competition)
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
            continue

        add_future_prediction(
            home,
            away,
            row["DateParsed"].date().isoformat() if pd.notna(row["DateParsed"]) else "",
        )

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
            add_future_prediction(resolved_home, resolved_away, "")

    stat_sums, position_counts = run_monte_carlo(teams, table, future_predictions, SIMULATION_RUNS)
    averaged = {}
    for team in teams:
        sums = stat_sums.get(team, {})
        averaged[team] = {
            "P": int(round(sums.get("P", 0.0) / SIMULATION_RUNS)),
            "W": int(round(sums.get("W", 0.0) / SIMULATION_RUNS)),
            "D": int(round(sums.get("D", 0.0) / SIMULATION_RUNS)),
            "L": int(round(sums.get("L", 0.0) / SIMULATION_RUNS)),
            "GF": int(round(sums.get("GF", 0.0) / SIMULATION_RUNS)),
            "GA": int(round(sums.get("GA", 0.0) / SIMULATION_RUNS)),
            "GD": int(round(sums.get("GD", 0.0) / SIMULATION_RUNS)),
            "Pts": int(round(sums.get("Pts", 0.0) / SIMULATION_RUNS)),
            "PlayedReal": int(round(sums.get("PlayedReal", 0.0) / SIMULATION_RUNS)),
            "PlayedPred": int(round(sums.get("PlayedPred", 0.0) / SIMULATION_RUNS)),
        }

    out_rows = []
    ranked = sorted(averaged.items(), key=lambda kv: (-kv[1]["Pts"], -kv[1]["GD"], -kv[1]["GF"], kv[0]))
    n_teams = len(teams)
    top_n = min(4, n_teams)
    bottom_cutoff = max(1, n_teams - 2)
    for pos, (team, s) in enumerate(ranked, start=1):
        team_positions = position_counts.get(team, {})
        most_likely_pos, most_likely_count = max(team_positions.items(), key=lambda kv: kv[1], default=(pos, 0))
        win_league_pct = (team_positions.get(1, 0) / SIMULATION_RUNS) * 100.0
        top4_pct = (sum(v for k, v in team_positions.items() if k <= top_n) / SIMULATION_RUNS) * 100.0
        bottom3_pct = (sum(v for k, v in team_positions.items() if k >= bottom_cutoff) / SIMULATION_RUNS) * 100.0
        position_odds = {
            str(rank): round((team_positions.get(rank, 0) / SIMULATION_RUNS) * 100.0, 2)
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
                "most_likely_position_pct": round((most_likely_count / SIMULATION_RUNS) * 100.0, 2),
                "position_odds_json": json.dumps(position_odds, separators=(",", ":"), sort_keys=True),
                "sim_runs": int(SIMULATION_RUNS),
            }
        )
    return out_rows, future_rows


def main():
    ctx = load_context()
    latest = latest_raw_file_per_competition(RAW_DIR)
    if not latest:
        raise ValueError(f"No raw season files found in {RAW_DIR}")

    all_tables = []
    all_future = []
    for competition, path in sorted(latest.items()):
        table_rows, future_rows = project_competition(ctx, competition, path)
        all_tables.extend(table_rows)
        all_future.extend(future_rows)

    os.makedirs(OUT_DIR, exist_ok=True)
    pd.DataFrame(all_tables).to_csv(OUT_TABLE, index=False)
    pd.DataFrame(all_future).to_csv(OUT_MATCHES, index=False)
    print(f"Projected league tables saved: {OUT_TABLE}")
    print(f"Predicted remaining matches saved: {OUT_MATCHES}")


if __name__ == "__main__":
    main()
