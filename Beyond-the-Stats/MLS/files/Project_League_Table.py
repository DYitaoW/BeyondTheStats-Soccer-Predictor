import os
import json
import re
import random
import urllib.request
from collections import defaultdict
from datetime import datetime
import subprocess
import sys

import joblib
import pandas as pd

import Download_Latest_Data as download_latest
import Predict_Match as pm


class AveragedProbaClassifier:
    # Compatibility shim so old model_cache.pkl entries serialized from __main__
    # can be loaded when this script is the entrypoint.
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
MLS_FILES_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(BASE_DIR, "Data", "Raw_Data")
OUT_DIR = os.path.join(BASE_DIR, "Data", "Predictions")
OUT_TABLE = os.path.join(OUT_DIR, "projected_league_tables.csv")
OUT_MATCHES = os.path.join(OUT_DIR, "projected_future_matches.csv")
OUT_BRACKET = os.path.join(OUT_DIR, "projected_mls_playoff_bracket.json")
ESPN_SCOREBOARD_API = "https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/scoreboard"
RNG = random.Random()
SIMULATION_RUNS = 200

EASTERN_CONFERENCE_TEAMS = {
    "Atlanta Utd",
    "CF Montreal",
    "Charlotte",
    "Chicago Fire",
    "Columbus Crew",
    "DC United",
    "FC Cincinnati",
    "Inter Miami",
    "Nashville SC",
    "New England Revolution",
    "New York City",
    "New York Red Bulls",
    "Orlando City",
    "Philadelphia Union",
    "Toronto FC",
}

WESTERN_CONFERENCE_TEAMS = {
    "Austin FC",
    "Colorado Rapids",
    "FC Dallas",
    "Houston Dynamo",
    "Los Angeles Galaxy",
    "Los Angeles FC",
    "Minnesota United",
    "Portland Timbers",
    "Real Salt Lake",
    "San Diego FC",
    "San Jose Earthquakes",
    "Seattle Sounders",
    "Sporting Kansas City",
    "St. Louis City",
    "Vancouver Whitecaps",
}
MLS_TEAM_ALIASES = {
    "d.c.united": "DC United",
    "dcunited": "DC United",
    "newyorkcityfc": "New York City",
    "newyorkcity": "New York City",
    "newyorkredbulls": "New York Red Bulls",
    "lafc": "Los Angeles FC",
    "lagalaxy": "Los Angeles Galaxy",
    "stlouiscitysc": "St. Louis City",
    "stlouiscity": "St. Louis City",
    "sandiegofc": "San Diego FC",
    "sandiego": "San Diego FC",
}


def normalize_team_key(name):
    text = str(name or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return "".join(text.split())


def rebuild_model_cache_once():
    """Rebuild the MLS model cache in non-interactive mode for dtype/pickle compatibility."""
    predict_script = os.path.join(MLS_FILES_DIR, "Predict_Match.py")
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
        raise RuntimeError(f"Auto-rebuild of MLS model cache failed: {message}")


def build_conference_lookup():
    lookup = {}
    for team in EASTERN_CONFERENCE_TEAMS:
        lookup[normalize_team_key(team)] = "east"
    for team in WESTERN_CONFERENCE_TEAMS:
        lookup[normalize_team_key(team)] = "west"
    return lookup


def resolve_conference_teams(ctx):
    east = []
    west = []
    for team in sorted(EASTERN_CONFERENCE_TEAMS):
        resolved = pm.resolve_team_name(team, ctx["available_teams"]) or team
        east.append(resolved)
    for team in sorted(WESTERN_CONFERENCE_TEAMS):
        resolved = pm.resolve_team_name(team, ctx["available_teams"]) or team
        west.append(resolved)
    return east, west


def resolve_fixture_team(raw_name, ctx, by_key):
    raw = str(raw_name or "").strip()
    if not raw:
        return ""

    direct = pm.resolve_team_name(raw, ctx["available_teams"])
    if direct:
        return direct

    key = normalize_team_key(raw)
    alias_target = MLS_TEAM_ALIASES.get(key)
    if alias_target:
        mapped = pm.resolve_team_name(alias_target, ctx["available_teams"])
        if mapped:
            return mapped
        return alias_target

    mapped = by_key.get(key)
    if mapped:
        return mapped

    return raw


def build_synthetic_mls_schedule(ctx, target_year):
    east, west = resolve_conference_teams(ctx)
    rows = []

    # In-conference: home and away versus every other team in conference.
    for conference in (east, west):
        for i, home in enumerate(conference):
            for j, away in enumerate(conference):
                if i == j:
                    continue
                rows.append(
                    {
                        "Season": target_year,
                        "Date": "",
                        "Home": home,
                        "Away": away,
                        "HG": None,
                        "AG": None,
                        "Res": "",
                    }
                )

    # Inter-conference: 6 opponents per team (90 total fixtures across 30 teams).
    # Circular block mapping gives each team exactly 6 inter-conference matches.
    n = min(len(east), len(west))
    for i in range(n):
        for offset in range(6):
            j = (i + offset) % n
            e_team = east[i]
            w_team = west[j]
            # Deterministic home assignment for reproducible projections.
            if ((i + offset + target_year) % 2) == 0:
                home, away = e_team, w_team
            else:
                home, away = w_team, e_team
            rows.append(
                {
                    "Season": target_year,
                    "Date": "",
                    "Home": home,
                    "Away": away,
                    "HG": None,
                    "AG": None,
                    "Res": "",
                }
            )

    return pd.DataFrame(rows, columns=["Season", "Date", "Home", "Away", "HG", "AG", "Res"])


def fetch_json(url, timeout=30):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload)


def load_full_schedule_from_espn_season(target_year):
    start = pd.Timestamp(f"{target_year}-01-01")
    end = pd.Timestamp(f"{target_year}-12-31")
    rows = []
    seen = set()

    day = start
    while day <= end:
        url = f"{ESPN_SCOREBOARD_API}?dates={day.strftime('%Y%m%d')}"
        try:
            data = fetch_json(url, timeout=20)
        except Exception:
            day += pd.Timedelta(days=1)
            continue

        events = data.get("events", [])
        if not isinstance(events, list):
            day += pd.Timedelta(days=1)
            continue

        for event in events:
            dt = pd.to_datetime(event.get("date"), utc=True, errors="coerce")
            if pd.isna(dt):
                continue
            dt = dt.tz_convert("UTC").tz_localize(None)
            if dt.year != target_year:
                continue

            competitions = event.get("competitions", [])
            if not competitions:
                continue
            comp0 = competitions[0] or {}
            competitors = comp0.get("competitors", [])

            home_team = ""
            away_team = ""
            home_score = None
            away_score = None
            for competitor in competitors:
                side = str(competitor.get("homeAway", "")).strip().lower()
                team_name = ((competitor.get("team") or {}).get("displayName") or "").strip()
                score_val = pd.to_numeric(competitor.get("score"), errors="coerce")
                if side == "home":
                    home_team = team_name
                    home_score = int(score_val) if pd.notna(score_val) else None
                elif side == "away":
                    away_team = team_name
                    away_score = int(score_val) if pd.notna(score_val) else None

            if not home_team or not away_team:
                continue

            key = (dt.date().isoformat(), home_team, away_team)
            if key in seen:
                continue
            seen.add(key)

            status_type = ((comp0.get("status") or {}).get("type") or {})
            completed = bool(status_type.get("completed"))
            if completed and home_score is not None and away_score is not None:
                if home_score > away_score:
                    result = "H"
                elif away_score > home_score:
                    result = "A"
                else:
                    result = "D"
                hg = home_score
                ag = away_score
            else:
                result = ""
                hg = None
                ag = None

            rows.append(
                {
                    "Season": target_year,
                    "Date": dt.date().isoformat(),
                    "Home": home_team,
                    "Away": away_team,
                    "HG": hg,
                    "AG": ag,
                    "Res": result,
                }
            )

        day += pd.Timedelta(days=1)

    if not rows:
        return None
    return pd.DataFrame(rows, columns=["Season", "Date", "Home", "Away", "HG", "AG", "Res"])


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
        print(f"[warn] Failed to load MLS model cache ({exc.__class__.__name__}). Rebuilding cache once...")
        rebuild_model_cache_once()
        bundle = joblib.load(pm.MODEL_CACHE)
    if bundle.get("fingerprint") != pm.data_fingerprint(season_files):
        raise RuntimeError("Model cache is stale. Rebuild by running Predict_Match.py.")

    overall_teams = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "overall_teams.json"))
    season_teams = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "season_teams.json"))
    head_to_head = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "head_to_head.json"))
    current_form = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "current_form.json"))
    league_strength = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "league_strength.json")) or {}
    market_value_data = pm.load_json_if_exists(os.path.join(pm.TEAM_DATA_DIR, "mls_squad_values.json")) or {}
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
    market_value_data = pm.replace_nan_with_sentinel(market_value_data)
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
        "market_value_data": market_value_data,
        "team_comp_map": team_comp_map,
        "latest_start": latest_start,
        "latest_season": latest_season,
        "available_teams": available,
    }


def init_table(teams):
    table = {}
    for team in sorted(teams):
        table[team] = {
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


def run_monte_carlo_mls(canonical_teams, base_table, future_predictions, conference_lookup, runs):
    stat_sums = {team: defaultdict(float) for team in canonical_teams}
    league_pos_counts = {team: defaultdict(int) for team in canonical_teams}
    east_pos_counts = {team: defaultdict(int) for team in canonical_teams}
    west_pos_counts = {team: defaultdict(int) for team in canonical_teams}

    for _ in range(max(1, int(runs))):
        sim_table = clone_table(base_table)
        for fixture in future_predictions:
            result = sample_outcome(fixture["probs"])
            hg, ag = coerce_scoreline(result, fixture["pred_home_goals"], fixture["pred_away_goals"])
            apply_result(sim_table, fixture["home_team"], fixture["away_team"], hg, ag, is_real=False)

        ranked = rank_table(sim_table)
        for pos, (team, stats) in enumerate(ranked, start=1):
            league_pos_counts[team][pos] += 1
            for key, value in stats.items():
                stat_sums[team][key] += float(value)

        east_ranked = [item for item in ranked if conference_lookup.get(normalize_team_key(item[0])) == "east"]
        west_ranked = [item for item in ranked if conference_lookup.get(normalize_team_key(item[0])) == "west"]
        for pos, (team, _) in enumerate(east_ranked, start=1):
            east_pos_counts[team][pos] += 1
        for pos, (team, _) in enumerate(west_ranked, start=1):
            west_pos_counts[team][pos] += 1

    return stat_sums, league_pos_counts, east_pos_counts, west_pos_counts


def build_probability_columns(position_counts, team, total_teams):
    counts = position_counts.get(team, {})
    most_likely_pos, most_likely_count = max(counts.items(), key=lambda kv: kv[1], default=(1, 0))
    top_n = min(4, total_teams)
    bottom_cutoff = max(1, total_teams - 2)
    position_odds = {
        str(rank): round((counts.get(rank, 0) / SIMULATION_RUNS) * 100.0, 2)
        for rank in range(1, total_teams + 1)
    }
    return {
        "win_league_pct": round((counts.get(1, 0) / SIMULATION_RUNS) * 100.0, 2),
        "top4_pct": round((sum(v for k, v in counts.items() if k <= top_n) / SIMULATION_RUNS) * 100.0, 2),
        "bottom3_pct": round((sum(v for k, v in counts.items() if k >= bottom_cutoff) / SIMULATION_RUNS) * 100.0, 2),
        "most_likely_position": int(most_likely_pos),
        "most_likely_position_pct": round((most_likely_count / SIMULATION_RUNS) * 100.0, 2),
        "position_odds_json": json.dumps(position_odds, separators=(",", ":"), sort_keys=True),
        "sim_runs": int(SIMULATION_RUNS),
    }


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

    market_shift, _, _ = pm.market_value_probability_shift(home_team, away_team, ctx["market_value_data"])
    if market_shift != 0.0:
        if market_shift > 0:
            transfer = min(market_shift, probs.get("A", 0.0))
            probs["H"] += transfer
            probs["A"] -= transfer
        else:
            transfer = min(abs(market_shift), probs.get("H", 0.0))
            probs["A"] += transfer
            probs["H"] -= transfer
        total = probs["H"] + probs["D"] + probs["A"]
        if total > 0:
            probs["H"] /= total
            probs["D"] /= total
            probs["A"] /= total

    home_adv_shift = pm.mls_home_advantage_shift(home_team, prediction_season, ctx["season_teams"])
    transfer = min(home_adv_shift, probs.get("A", 0.0))
    probs["H"] = max(0.0, probs.get("H", 0.0) + transfer)
    probs["A"] = max(0.0, probs.get("A", 0.0) - transfer)
    probs = pm.apply_home_advantage_boost(probs)
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

    if pred_res == "H" and hg <= ag:
        hg = ag + 1
    elif pred_res == "A" and ag <= hg:
        ag = hg + 1
    elif pred_res == "D":
        ag = hg

    return pred_res, hg, ag, probs


def predict_single_winner_no_draw(ctx, home_team, away_team, competition_hint, fallback_winner):
    _, _, _, probs = predict_match(ctx, home_team, away_team, competition_hint)
    p_home = max(0.0, float(probs.get("H", 0.0)))
    p_away = max(0.0, float(probs.get("A", 0.0)))
    total = p_home + p_away
    if total <= 0:
        return fallback_winner
    return home_team if RNG.random() < (p_home / total) else away_team


def predict_best_of_three(ctx, high_seed_team, low_seed_team, competition_hint):
    high_wins = 0
    low_wins = 0
    games = []

    winner = predict_single_winner_no_draw(
        ctx, high_seed_team, low_seed_team, competition_hint, high_seed_team
    )
    games.append({"home": high_seed_team, "away": low_seed_team, "winner": winner})
    if winner == high_seed_team:
        high_wins += 1
    else:
        low_wins += 1

    if high_wins < 2 and low_wins < 2:
        winner = predict_single_winner_no_draw(
            ctx, low_seed_team, high_seed_team, competition_hint, high_seed_team
        )
        games.append({"home": low_seed_team, "away": high_seed_team, "winner": winner})
        if winner == high_seed_team:
            high_wins += 1
        else:
            low_wins += 1

    if high_wins < 2 and low_wins < 2:
        winner = predict_single_winner_no_draw(
            ctx, high_seed_team, low_seed_team, competition_hint, high_seed_team
        )
        games.append({"home": high_seed_team, "away": low_seed_team, "winner": winner})
        if winner == high_seed_team:
            high_wins += 1
        else:
            low_wins += 1

    return {
        "high_seed_team": high_seed_team,
        "low_seed_team": low_seed_team,
        "winner": high_seed_team if high_wins >= low_wins else low_seed_team,
        "games": games,
    }


def build_mls_playoff_bracket_prediction(ctx, east_ranked, west_ranked):
    def seed_at(items, seed_num):
        for pos, (team, _) in enumerate(items, start=1):
            if pos == seed_num:
                return team
        return f"Seed {seed_num}"

    e1, e2, e3, e4, e5, e6, e7, e8, e9 = [seed_at(east_ranked, s) for s in range(1, 10)]
    w1, w2, w3, w4, w5, w6, w7, w8, w9 = [seed_at(west_ranked, s) for s in range(1, 10)]

    e_wc_winner = predict_single_winner_no_draw(ctx, e8, e9, "United States/MLS", e8)
    w_wc_winner = predict_single_winner_no_draw(ctx, w8, w9, "United States/MLS", w8)

    e_r1a = predict_best_of_three(ctx, e1, e_wc_winner, "United States/MLS")
    e_r1b = predict_best_of_three(ctx, e2, e7, "United States/MLS")
    e_r1c = predict_best_of_three(ctx, e3, e6, "United States/MLS")
    e_r1d = predict_best_of_three(ctx, e4, e5, "United States/MLS")

    w_r1a = predict_best_of_three(ctx, w1, w_wc_winner, "United States/MLS")
    w_r1b = predict_best_of_three(ctx, w2, w7, "United States/MLS")
    w_r1c = predict_best_of_three(ctx, w3, w6, "United States/MLS")
    w_r1d = predict_best_of_three(ctx, w4, w5, "United States/MLS")

    e_sf1_home = e_r1a["winner"]
    e_sf1_away = e_r1d["winner"]
    e_sf1_winner = predict_single_winner_no_draw(ctx, e_sf1_home, e_sf1_away, "United States/MLS", e_sf1_home)
    e_sf2_home = e_r1b["winner"]
    e_sf2_away = e_r1c["winner"]
    e_sf2_winner = predict_single_winner_no_draw(ctx, e_sf2_home, e_sf2_away, "United States/MLS", e_sf2_home)
    e_cf_home = e_sf1_winner
    e_cf_away = e_sf2_winner
    e_champ = predict_single_winner_no_draw(ctx, e_cf_home, e_cf_away, "United States/MLS", e_cf_home)

    w_sf1_home = w_r1a["winner"]
    w_sf1_away = w_r1d["winner"]
    w_sf1_winner = predict_single_winner_no_draw(ctx, w_sf1_home, w_sf1_away, "United States/MLS", w_sf1_home)
    w_sf2_home = w_r1b["winner"]
    w_sf2_away = w_r1c["winner"]
    w_sf2_winner = predict_single_winner_no_draw(ctx, w_sf2_home, w_sf2_away, "United States/MLS", w_sf2_home)
    w_cf_home = w_sf1_winner
    w_cf_away = w_sf2_winner
    w_champ = predict_single_winner_no_draw(ctx, w_cf_home, w_cf_away, "United States/MLS", w_cf_home)

    cup_home = e_champ
    cup_away = w_champ
    cup_winner = predict_single_winner_no_draw(ctx, cup_home, cup_away, "United States/MLS", cup_home)

    return {
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "eastern_seeds": [{"seed": i + 1, "team": team} for i, team in enumerate([e1, e2, e3, e4, e5, e6, e7, e8, e9])],
        "western_seeds": [{"seed": i + 1, "team": team} for i, team in enumerate([w1, w2, w3, w4, w5, w6, w7, w8, w9])],
        "wildcard": {
            "east": {"home_team": e8, "away_team": e9, "winner": e_wc_winner},
            "west": {"home_team": w8, "away_team": w9, "winner": w_wc_winner},
        },
        "round_one": {
            "east": {"A": e_r1a, "B": e_r1b, "C": e_r1c, "D": e_r1d},
            "west": {"A": w_r1a, "B": w_r1b, "C": w_r1c, "D": w_r1d},
        },
        "conference_semifinals": {
            "east": [
                {"home_team": e_sf1_home, "away_team": e_sf1_away, "winner": e_sf1_winner},
                {"home_team": e_sf2_home, "away_team": e_sf2_away, "winner": e_sf2_winner},
            ],
            "west": [
                {"home_team": w_sf1_home, "away_team": w_sf1_away, "winner": w_sf1_winner},
                {"home_team": w_sf2_home, "away_team": w_sf2_away, "winner": w_sf2_winner},
            ],
        },
        "conference_finals": {
            "east": {"home_team": e_cf_home, "away_team": e_cf_away, "winner": e_champ},
            "west": {"home_team": w_cf_home, "away_team": w_cf_away, "winner": w_champ},
        },
        "mls_cup": {"home_team": cup_home, "away_team": cup_away, "winner": cup_winner},
    }


def project_competition(ctx, competition, raw_file):
    def load_full_schedule_for_season(year):
        try:
            source = download_latest.fetch_source_dataframe()
        except Exception:
            return None
        required = {"Season", "Date", "Home", "Away", "HG", "AG", "Res"}
        if not required.issubset(source.columns):
            return None

        source = source.copy()
        source["SeasonInt"] = pd.to_numeric(source["Season"], errors="coerce")
        source = source[source["SeasonInt"] == float(year)]
        if source.empty:
            return None
        return source

    current_year = datetime.now().year
    full_schedule = load_full_schedule_for_season(current_year)
    if full_schedule is None:
        full_schedule = load_full_schedule_from_espn_season(current_year)
    if full_schedule is not None:
        df = full_schedule.copy()
    else:
        # If upcoming-season schedule is unavailable from source, project the full
        # MLS calendar year using league format rules (34 matches per team).
        df = build_synthetic_mls_schedule(ctx, current_year)

    required = {"Date", "Home", "Away", "HG", "AG", "Res"}
    if not required.issubset(df.columns):
        return [], []

    df["DateParsed"] = pd.to_datetime(df["Date"], dayfirst=True, format="mixed", errors="coerce")
    df = df[df["Home"].notna() & df["Away"].notna()]
    df = df.sort_values(["DateParsed", "Home", "Away"], na_position="last").reset_index(drop=True)

    east_teams, west_teams = resolve_conference_teams(ctx)
    canonical_teams = sorted(set(east_teams + west_teams))
    canonical_team_set = set(canonical_teams)
    table = init_table(canonical_teams)
    future_rows = []
    future_predictions = []
    key_to_team = {normalize_team_key(team): team for team in ctx["available_teams"]}
    seen_fixtures = set()
    seen_pairs = set()
    synthetic_df = build_synthetic_mls_schedule(ctx, current_year)
    allowed_pairs = set()
    for _, synth_row in synthetic_df.iterrows():
        sh = resolve_fixture_team(synth_row.get("Home", ""), ctx, key_to_team)
        sa = resolve_fixture_team(synth_row.get("Away", ""), ctx, key_to_team)
        if sh and sa and sh != sa and sh in canonical_team_set and sa in canonical_team_set:
            allowed_pairs.add((sh, sa))

    def add_predicted_fixture(home, away, match_date):
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
        raw_home = str(row["Home"]).strip()
        raw_away = str(row["Away"]).strip()
        home = resolve_fixture_team(raw_home, ctx, key_to_team)
        away = resolve_fixture_team(raw_away, ctx, key_to_team)
        if not home or not away or home == away:
            continue
        if home not in canonical_team_set or away not in canonical_team_set:
            continue
        match_date = row["DateParsed"].date().isoformat() if pd.notna(row["DateParsed"]) else ""
        fixture_key = (match_date, home, away)
        if fixture_key in seen_fixtures:
            continue
        seen_fixtures.add(fixture_key)
        seen_pairs.add((home, away))
        if (home, away) not in allowed_pairs:
            continue
        ftr = str(row.get("Res", "")).strip()
        hg = pd.to_numeric(row.get("HG"), errors="coerce")
        ag = pd.to_numeric(row.get("AG"), errors="coerce")
        is_played = ftr in {"H", "D", "A"} and pd.notna(hg) and pd.notna(ag)

        if is_played:
            apply_result(table, home, away, int(hg), int(ag), is_real=True)
            continue

        add_predicted_fixture(home, away, match_date)

    # If online sources do not yet expose the complete regular season schedule,
    # fill missing MLS-format fixtures so every team reaches 34 games.
    expected_fixtures = (len(canonical_teams) * 34) // 2
    if len(seen_pairs) < expected_fixtures:
        for home, away in sorted(allowed_pairs):
            if (home, away) in seen_pairs:
                continue
            seen_pairs.add((home, away))
            add_predicted_fixture(home, away, "")

    conference_lookup = build_conference_lookup()
    stat_sums, league_pos_counts, east_pos_counts, west_pos_counts = run_monte_carlo_mls(
        canonical_teams, table, future_predictions, conference_lookup, SIMULATION_RUNS
    )
    averaged = {}
    for team in canonical_teams:
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

    def emit_rows(target_name, items, pos_counts):
        rows = []
        total_teams = len(items)
        for pos, (team, stats) in enumerate(items, start=1):
            rows.append(
                {
                    "competition": target_name,
                    "position": pos,
                    "team": team,
                    **stats,
                    **build_probability_columns(pos_counts, team, total_teams),
                }
            )
        return rows

    east_ranked = [item for item in ranked if conference_lookup.get(normalize_team_key(item[0])) == "east"]
    west_ranked = [item for item in ranked if conference_lookup.get(normalize_team_key(item[0])) == "west"]

    out_rows.extend(emit_rows("United States/MLS - Supporters Shield Table", ranked, league_pos_counts))
    if east_ranked:
        out_rows.extend(emit_rows("United States/MLS - Eastern Conference", east_ranked, east_pos_counts))
    if west_ranked:
        out_rows.extend(emit_rows("United States/MLS - Western Conference", west_ranked, west_pos_counts))

    bracket_payload = None
    if len(east_ranked) >= 9 and len(west_ranked) >= 9:
        bracket_payload = build_mls_playoff_bracket_prediction(ctx, east_ranked, west_ranked)

    return out_rows, future_rows, bracket_payload


def main():
    ctx = load_context()
    latest = latest_raw_file_per_competition(RAW_DIR)
    if not latest:
        raise ValueError(f"No raw season files found in {RAW_DIR}")

    all_tables = []
    all_future = []
    playoff_bracket = None
    for competition, path in sorted(latest.items()):
        table_rows, future_rows, bracket_payload = project_competition(ctx, competition, path)
        all_tables.extend(table_rows)
        all_future.extend(future_rows)
        if bracket_payload:
            playoff_bracket = bracket_payload

    os.makedirs(OUT_DIR, exist_ok=True)
    pd.DataFrame(all_tables).to_csv(OUT_TABLE, index=False)
    pd.DataFrame(all_future).to_csv(OUT_MATCHES, index=False)
    if playoff_bracket is not None:
        with open(OUT_BRACKET, "w", encoding="utf-8") as fh:
            json.dump(playoff_bracket, fh, indent=2)
    print(f"Projected league tables saved: {OUT_TABLE}")
    print(f"Predicted remaining matches saved: {OUT_MATCHES}")
    if playoff_bracket is not None:
        print(f"Predicted MLS playoff bracket saved: {OUT_BRACKET}")


if __name__ == "__main__":
    main()
