import os
import json
import re
import urllib.request
import argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo
import random
import subprocess
import sys
import time

import joblib
import pandas as pd

import Predict_Match as pm

# Beyond-the-Stats root (sibling of Extra-leagues/) for shared season helpers.
_SP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SP_DIR not in sys.path:
    sys.path.insert(0, _SP_DIR)
import season_calendar


class AveragedProbaClassifier:
    # Cache compatibility shim for pickles created when Predict_Match was __main__.
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
SIMULATION_RUNS = 1000
COMPETITION_SIM_RUNS = {}
ESPN_SCOREBOARD_API = "https://site.api.espn.com/apis/site/v2/sports/soccer/{espn_id}/scoreboard"
EASTERN_TZ = ZoneInfo("America/New_York")
SHARED_MAPPING_FILE = os.path.join(BASE_DIR, "..", "..", "Data", "team_name_mapping_master.json")
LEAGUE_TEAMS_FILE = os.path.join(BASE_DIR, "..", "Data", "Predictions", "league_teams.json")


def _sibling_competitions(competition, mapping):
    """Return competition keys to search when ``competition`` has no mapping.

    For UEFA competitions, checks every competition in the mapping (since any
    European league's teams can appear in Champions/Europa/Conference League).
    For Leagues Cup, only checks United States/MLS and Mexico/Liga MX.
    For country-specific competitions, checks sibling leagues in the same
    country (e.g. ``England/Premier League`` → also check ``England/Championship``).
    """
    country = competition.split("/")[0] if "/" in competition else competition
    if country.upper().startswith("UEFA"):
        return [k for k in mapping if isinstance(mapping.get(k), dict)]
    if competition == "CONCACAF/Leagues Cup":
        return [k for k in mapping if k in ("United States/MLS", "Mexico/Liga MX") and isinstance(mapping.get(k), dict)]
    return [k for k in mapping if k != competition and isinstance(mapping.get(k), dict) and k.split("/")[0] == country]


def _append_mapping_if_missing(competition, unresolved_names, valid_names, roster_teams=None):
    if not unresolved_names or not os.path.exists(SHARED_MAPPING_FILE):
        return
    try:
        with open(SHARED_MAPPING_FILE, "r", encoding="utf-8") as fh:
            mapping = json.load(fh)
    except Exception:
        return
    if not isinstance(mapping, dict):
        return
    comp_section = mapping.setdefault(competition, {})
    siblings = _sibling_competitions(competition, mapping)
    added = 0
    for raw_name in sorted(set(unresolved_names)):
        if raw_name in comp_section:
            continue
        candidate = None
        for sibling_comp in siblings:
            sibling_section = mapping.get(sibling_comp, {})
            if isinstance(sibling_section, dict) and raw_name in sibling_section:
                mapped = sibling_section[raw_name]
                if mapped:
                    candidate = mapped
                    break
        if not candidate:
            stripped = re.sub(
                r"\s+(FC|AFC|United|City|CF|IF|SC|AG|AS|OFK|FK|NK|HNK|UD|CD|RC|CR|Club|Atl[eé]tico)\s*$",
                "", raw_name, flags=re.IGNORECASE
            ).strip()
            candidate = pm.resolve_team_name(stripped, valid_names) or pm.resolve_team_name(raw_name, valid_names)
        if candidate and candidate in valid_names:
            if roster_teams is not None and candidate not in roster_teams:
                # Team is valid but not in current competition roster — add to sibling section instead
                added_to_sibling = False
                for sibling_comp in siblings:
                    sibling_section = mapping.setdefault(sibling_comp, {})
                    if raw_name not in sibling_section:
                        sibling_section[raw_name] = candidate
                        added_to_sibling = True
                        break
                if not added_to_sibling:
                    comp_section[raw_name] = candidate
            else:
                comp_section[raw_name] = candidate
        else:
            comp_section[raw_name] = ""
        added += 1
    if added:
        with open(SHARED_MAPPING_FILE, "w", encoding="utf-8") as fh:
            json.dump(mapping, fh, indent=2, ensure_ascii=False)
        print(f"  Mapping: auto-added {added} entry/ies to {SHARED_MAPPING_FILE}")


def _load_upcoming_roster(competition):
    """Load team roster for the upcoming season from league_teams.json.

    Returns a sorted list of team names, or None if the competition is
    not found in the roster file.
    """
    if not os.path.exists(LEAGUE_TEAMS_FILE):
        return None
    try:
        with open(LEAGUE_TEAMS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        roster = data.get(competition)
        if isinstance(roster, list) and len(roster) > 1:
            return sorted(set(r.strip() for r in roster if r and r.strip()))
    except Exception:
        pass
    return None


EXTRA_ESPN_COMPETITIONS = {
    "Argentina/Primera Division": "arg.1",
    "Brazil/Serie A": "bra.1",
    "Japan/J1 League": "jpn.1",
    "Netherlands/Eredivisie": "ned.1",
    "Belgium/First Division A": "bel.1",
    "Scotland/Premiership": "sco.1",
    "Turkey/Super Lig": "tur.1",
    "Austria/Bundesliga": "aut.1",
    "Switzerland/Super League": "sui.1",
    "Greece/Super League": "gre.1",
    "Denmark/Danish Superliga": "den.1",
    "Ukraine/Premier League": "ukr.1",
    "Norway/Eliteserien": "nor.1",
    "Croatia/HNL": "cro.1",
    "Romania/Liga I": "rou.1",
    "Sweden/Allsvenskan": "swe.1",
    "Hungary/NB I": "hun.1",
    "Israel/Premier League": "isr.1",
    "Czech Republic/First League": "cze.1",
    "Poland/Ekstraklasa": "pol.1",
    "Serbia/SuperLiga": "srb.1",
    "Cyprus/First Division": "cyp.1",
    "Slovakia/Super Liga": "svk.1",
    "Slovenia/PrvaLiga": "svn.1",
    "Bulgaria/First League": "bul.1",
}


def rebuild_model_cache_once():
    """Rebuild the model cache in non-interactive mode for dtype/pickle compatibility."""
    predict_script = os.path.join(FILES_DIR, "Predict_Match.py")
    if not os.path.exists(predict_script):
        raise FileNotFoundError(f"Missing predictor script: {predict_script}")
    proc = subprocess.run(
        [sys.executable, predict_script, "--build-cache-only"],
        cwd=BASE_DIR,
        text=True,
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


def _load_espn_fixtures(competition, ctx):
    """Fetch upcoming ESPN fixtures, resolve team names, return a DataFrame or None."""
    espn_id = EXTRA_ESPN_COMPETITIONS.get(competition)
    if not espn_id:
        return None
    target_year = datetime.now(UTC).year
    today = pd.Timestamp(datetime.now(UTC).date())
    # Only crawl remaining days — past dates are irrelevant for upcoming fixtures.
    start = today
    end = pd.Timestamp(f"{target_year}-12-31")
    rows = []
    seen = set()
    unresolved = []
    empty_streak = 0
    day = start
    while day <= end:
        url = ESPN_SCOREBOARD_API.format(espn_id=espn_id) + f"?dates={day.strftime('%Y%m%d')}"
        try:
            data = fetch_json(url, timeout=20)
        except Exception:
            empty_streak += 1
            day += pd.Timedelta(days=7 if empty_streak >= 5 else 1)
            continue
        events = data.get("events", []) or []
        if not events:
            empty_streak += 1
            day += pd.Timedelta(days=7 if empty_streak >= 5 else 1)
            continue
        empty_streak = 0
        for event in events:
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
            resolved_home = pm.resolve_team_name(home_team, ctx["available_teams"])
            resolved_away = pm.resolve_team_name(away_team, ctx["available_teams"])
            if not resolved_home:
                unresolved.append(home_team)
            if not resolved_away:
                unresolved.append(away_team)
            if not resolved_home or not resolved_away:
                continue
            key = (match_date.date().isoformat(), resolved_home, resolved_away)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "Date": match_date.date().isoformat(),
                "HomeTeam": resolved_home,
                "AwayTeam": resolved_away,
                "FTHG": None,
                "FTAG": None,
                "FTR": "",
            })
        day += pd.Timedelta(days=1)
    if unresolved:
        msg = f"  ESPN: {len(unresolved)} team name(s) in {competition} could not be resolved: {sorted(set(unresolved))}"
        print(f"[WARN] {msg}")
        _append_mapping_if_missing(competition, unresolved, ctx["available_teams"], _load_upcoming_roster(competition))
    if not rows:
        return None
    frame = pd.DataFrame(rows)
    frame["DateParsed"] = pd.to_datetime(frame["Date"], errors="coerce")
    return frame


def load_context():
    matches, season_files = pm.load_training_matches(pm.PROCESSED_DIR)
    if not os.path.exists(pm.MODEL_CACHE):
        print("[model-cache] cache missing; rebuilding model cache...")
        rebuild_model_cache_once()

    try:
        # Caches pickled from Predict_Match.py as __main__ need this alias.
        setattr(sys.modules.get("__main__"), "AveragedProbaClassifier", pm.AveragedProbaClassifier)
    except Exception:
        pass

    try:
        bundle = joblib.load(pm.MODEL_CACHE)
    except Exception as exc:
        print(f"[model-cache] failed to load cache ({exc.__class__.__name__}: {exc}); rebuilding...")
        rebuild_model_cache_once()
        try:
            setattr(sys.modules.get("__main__"), "AveragedProbaClassifier", pm.AveragedProbaClassifier)
        except Exception:
            pass
        bundle = joblib.load(pm.MODEL_CACHE)
    if bundle.get("fingerprint") != pm.data_fingerprint(season_files):
        print("[model-cache] using cached models (data newer than cache; full retrain runs Tue/Fri)")

    required_keys = {
        "clf",
        "result_label_encoder",
        "home_goal_reg",
        "away_goal_reg",
        "home_shot_reg",
        "away_shot_reg",
        "home_sot_reg",
        "away_sot_reg",
        "train_columns",
        "goal_prob_models",
    }
    missing = sorted(k for k in required_keys if k not in bundle)
    if missing:
        print(f"[model-cache] cache missing required fields ({', '.join(missing)}); rebuilding...")
        if os.path.exists(pm.MODEL_CACHE):
            os.remove(pm.MODEL_CACHE)
        rebuild_model_cache_once()
        bundle = joblib.load(pm.MODEL_CACHE)

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
    "Mexico/Liga MX",
}


def _compute_h2h_scores(tied_teams, all_matches):
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


def run_monte_carlo(teams, base_table, future_predictions, runs, competition=None, all_matches=None):
    stat_sums = {team: defaultdict(float) for team in teams}
    position_counts = {team: defaultdict(int) for team in teams}

    real_matches = list(all_matches) if all_matches else []
    for _ in range(max(1, int(runs))):
        sim_table = clone_table(base_table)
        # Reset each iteration — appending forever made H2H ranking O(runs^2).
        sim_matches = list(real_matches)
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


def _expected_current_season_year(competition):
    """Return the expected start year for the current season of *competition*."""
    try:
        return season_calendar.expected_season_start_year(competition)
    except Exception:
        now = datetime.now()
        if now.month > 7 or (now.month == 7 and now.day >= 15):
            return now.year
        return now.year - 1


def _fetch_espn_teams(competition):
    """Fetch current team list from ESPN /teams endpoint."""
    espn_id = EXTRA_ESPN_COMPETITIONS.get(competition)
    if not espn_id:
        return None
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{espn_id}/teams"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        teams_list = data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
        if not teams_list:
            return None
        teams = []
        for entry in teams_list:
            team = entry.get("team") or {}
            name = (team.get("displayName") or team.get("name") or "").strip()
            if name:
                teams.append(name)
        return sorted(set(teams))
    except Exception:
        return None


def project_competition(ctx, competition, raw_file):
    sim_runs = COMPETITION_SIM_RUNS.get(competition, SIMULATION_RUNS)

    # Determine if this CSV represents the current season or a past season
    csv_start_year = pm.parse_season_start_year(os.path.basename(raw_file))
    expected_year = _expected_current_season_year(competition)
    is_current_season = csv_start_year is not None and csv_start_year == expected_year

    if is_current_season:
        # ── PATH A: Current-season CSV exists ──────────────────────────
        df = normalize_raw_df(pd.read_csv(raw_file))
        required = {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"}
        if not required.issubset(df.columns):
            return [], []

        # Try ESPN for any forthcoming fixtures in the current season window
        espn_future = _load_espn_fixtures(competition, ctx)
        if espn_future is not None:
            df = pd.concat([df, espn_future], ignore_index=True, sort=False)

        df["DateParsed"] = pd.to_datetime(df["Date"], dayfirst=True, format="mixed", errors="coerce")
        df = df[df["HomeTeam"].notna() & df["AwayTeam"].notna()]
        df = df.sort_values(["DateParsed", "HomeTeam", "AwayTeam"], na_position="last").reset_index(drop=True)

        raw_teams = set(df["HomeTeam"].astype(str).str.strip()) | set(df["AwayTeam"].astype(str).str.strip())
        teams = sorted({r for t in raw_teams if (r := pm.resolve_team_name(t, ctx["available_teams"]))})
    else:
        # ── PATH B: No current-season CSV — ESPN fallback ─────────────
        espn_teams = _fetch_espn_teams(competition)
        if espn_teams:
            resolved = set()
            unresolved = []
            for t in espn_teams:
                r = pm.resolve_team_name(t, ctx["available_teams"])
                if r:
                    resolved.add(r)
                else:
                    unresolved.append(t)
            if unresolved:
                print(f"  Unresolved ESPN teams in {competition}: {sorted(set(unresolved))}")
                _append_mapping_if_missing(competition, unresolved, ctx["available_teams"], _load_upcoming_roster(competition))
                for t in unresolved:
                    r = pm.resolve_team_name(t, ctx["available_teams"])
                    if r:
                        resolved.add(r)
            teams = sorted(resolved) if resolved else None
        if not teams:
            static_roster = _load_upcoming_roster(competition)
            if not static_roster:
                print(f"  No current-season file and no ESPN/roster data for {competition}")
                return [], []
            teams = sorted(static_roster)

        print(
            f"  No current-season CSV (typical Jul–Aug before games) — "
            f"using ESPN/roster PATH B ({len(teams)} teams)"
        )

        # Build a minimal df with resolved team names and no played results
        espn_future = _load_espn_fixtures(competition, ctx)
        rows = []
        if espn_future is not None:
            for _, r in espn_future.iterrows():
                rows.append({
                    "Date": str(r.get("Date", "")),
                    "HomeTeam": str(r.get("HomeTeam", "")),
                    "AwayTeam": str(r.get("AwayTeam", "")),
                    "FTHG": None, "FTAG": None, "FTR": "",
                })
        # Add synthetic pairs for any teams not covered by ESPN fixtures.
        # Prefer resolved model names only — never inject unresolved ESPN aliases.
        resolved_teams = []
        for team_name in teams:
            r = pm.resolve_team_name(team_name, ctx["available_teams"])
            if r:
                resolved_teams.append(r)
        if resolved_teams:
            teams = sorted(set(resolved_teams))
        seen = {(r["HomeTeam"], r["AwayTeam"]) for r in rows if r["HomeTeam"] and r["AwayTeam"]}
        for home in teams:
            for away in teams:
                if home == away:
                    continue
                if (home, away) in seen:
                    continue
                seen.add((home, away))
                rows.append({
                    "Date": "", "HomeTeam": home, "AwayTeam": away,
                    "FTHG": None, "FTAG": None, "FTR": "",
                })
        df = pd.DataFrame(rows)
        df["DateParsed"] = pd.to_datetime(df["Date"], errors="coerce")

    table = init_table(teams)
    future_rows = []
    future_predictions = []
    real_matches = []
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
                "match_datetime_utc": "",
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
        home = pm.resolve_team_name(raw_home, ctx["available_teams"])
        away = pm.resolve_team_name(raw_away, ctx["available_teams"])
        if not home or not away or home not in table or away not in table:
            continue
        seen_pairs.add((home, away))
        ftr = str(row.get("FTR", "")).strip()
        hg = pd.to_numeric(row.get("FTHG"), errors="coerce")
        ag = pd.to_numeric(row.get("FTAG"), errors="coerce")
        is_played = ftr in {"H", "D", "A"} and pd.notna(hg) and pd.notna(ag)

        # PATH B (no current-season CSV): never treat rows as already played.
        if is_played and is_current_season:
            apply_result(table, home, away, int(hg), int(ag), is_real=True)
            real_matches.append((home, away, int(hg), int(ag)))
            continue

        add_future_prediction(
            home,
            away,
            row["DateParsed"].date().isoformat() if pd.notna(row["DateParsed"]) else "",
        )

    # Fill remaining synthetic pairs for any teams not yet connected
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

    stat_sums, position_counts = run_monte_carlo(teams, table, future_predictions, sim_runs, competition=competition, all_matches=real_matches)
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


def main():
    parser = argparse.ArgumentParser(description="Project remaining fixtures onto a Monte-Carlo season.")
    parser.add_argument(
        "--competition-workers",
        type=int,
        default=0,
        help="Number of parallel workers for per-competition processing (0 = sequential).",
    )
    args = parser.parse_args()
    comp_workers = max(1, int(args.competition_workers))

    ctx = load_context()
    latest = latest_raw_file_per_competition(RAW_DIR)
    if not latest:
        raise ValueError(f"No raw season files found in {RAW_DIR}")

    # June through mid-July: skip European-style Extra leagues until Jul 15+.
    # Calendar-year Extra leagues (if any) keep projecting.
    try:
        if season_calendar.is_european_club_offseason():
            european = {
                comp: path for comp, path in latest.items()
                if not season_calendar.competition_uses_calendar_year(comp)
            }
            calendar = {
                comp: path for comp, path in latest.items()
                if season_calendar.competition_uses_calendar_year(comp)
            }
            if european:
                print(
                    f"[skip] European club off-season (Jun–Jul 14) — "
                    f"deferring {len(european)} Extra league projection(s) until Jul 15+"
                )
            latest = calendar
    except Exception:
        pass

    all_tables = []
    all_future = []
    comps = sorted(latest.items())
    if not comps:
        print("No competitions left to project.")
        os.makedirs(OUT_DIR, exist_ok=True)
        pd.DataFrame(all_tables).to_csv(OUT_TABLE, index=False)
        pd.DataFrame(all_future).to_csv(OUT_MATCHES, index=False)
        return

    if comp_workers <= 1 or len(comps) <= 1:
        for competition, path in comps:
            table_rows, future_rows = project_competition(ctx, competition, path)
            all_tables.extend(table_rows)
            all_future.extend(future_rows)
    else:
        max_workers = min(comp_workers, len(comps))
        print(f"  Processing {len(comps)} competitions with {max_workers} workers")
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(project_competition, ctx, comp, path): comp
                for comp, path in comps
            }
            for fut in as_completed(futures):
                comp = futures[fut]
                try:
                    table_rows, future_rows = fut.result()
                    all_tables.extend(table_rows)
                    all_future.extend(future_rows)
                    print(f"  [{comp}] done ({len(table_rows)} rows)")
                except Exception as e:
                    print(f"  [{comp}] ERROR: {e}")

    os.makedirs(OUT_DIR, exist_ok=True)
    pd.DataFrame(all_tables).to_csv(OUT_TABLE, index=False)
    pd.DataFrame(all_future).to_csv(OUT_MATCHES, index=False)
    print(f"Projected league tables saved: {OUT_TABLE}")
    print(f"Predicted remaining matches saved: {OUT_MATCHES}")


if __name__ == "__main__":
    main()
