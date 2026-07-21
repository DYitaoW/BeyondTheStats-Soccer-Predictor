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
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import joblib
import pandas as pd

import Predict_Match as pm


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
import projection_schedule as proj_sched  # noqa: E402

FILES_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(BASE_DIR, "Data", "Raw_Data")
OUT_DIR = os.path.join(BASE_DIR, "Data", "Predictions")
OUT_TABLE = os.path.join(OUT_DIR, "projected_league_tables.csv")
OUT_MATCHES = os.path.join(OUT_DIR, "projected_future_matches.csv")
RNG = random.Random()
SIMULATION_RUNS = 1000
COMPETITION_SIM_RUNS = {
    "England/Premier League": 2500,
    "Spain/La Liga": 2500,
    "Italy/Serie A": 2500,
    "Germany/Bundesliga": 2500,
    "France/Ligue 1": 2500,
}
ESPN_SCOREBOARD_API = "https://site.api.espn.com/apis/site/v2/sports/soccer/{espn_id}/scoreboard"
EASTERN_TZ = ZoneInfo("America/New_York")

MAPPING_FILE = os.path.join(BASE_DIR, "..", "..", "Data", "team_name_mapping_master.json")
LEAGUE_TEAMS_FILE = os.path.join(OUT_DIR, "league_teams.json")
CURRENT_SEASON_TEAMS_FILE = os.path.join(OUT_DIR, "current_season_teams.json")


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
    if not unresolved_names or not os.path.exists(MAPPING_FILE):
        return
    try:
        with open(MAPPING_FILE, "r", encoding="utf-8") as fh:
            mapping = json.load(fh)
    except Exception:
        return
    if not isinstance(mapping, dict):
        return
    comp_section = mapping.setdefault(competition, {})
    siblings = _sibling_competitions(competition, mapping)
    # Pre-compute which sibling sections contain current-roster teams
    sibling_roster = {}
    if roster_teams:
        for sibling_comp in siblings:
            sibling_section = mapping.get(sibling_comp, {})
            if isinstance(sibling_section, dict):
                for raw, canon in sibling_section.items():
                    if canon and canon in roster_teams:
                        sibling_roster.setdefault(canon, []).append((sibling_comp, raw))
    added = 0
    for raw_name in sorted(set(unresolved_names)):
        existing = comp_section.get(raw_name)
        if isinstance(existing, str) and existing.strip():
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
                    if raw_name not in sibling_section or not str(sibling_section.get(raw_name) or "").strip():
                        sibling_section[raw_name] = candidate
                        added_to_sibling = True
                        break
                if not added_to_sibling:
                    comp_section[raw_name] = candidate
            else:
                comp_section[raw_name] = candidate
            added += 1
        # Do not write blank stubs — they block future auto-mapping.
    if added:
        with open(MAPPING_FILE, "w", encoding="utf-8") as fh:
            json.dump(mapping, fh, indent=2, ensure_ascii=False)
        if hasattr(pm, "clear_name_mapping_cache"):
            pm.clear_name_mapping_cache()
        print(f"  Mapping: auto-added {added} entry/ies to {MAPPING_FILE}")


def _load_roster_from_json(path, competition):
    """Return sorted team names for *competition* from a roster JSON, or None."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        roster = data.get(competition)
        if isinstance(roster, list) and len(roster) > 1:
            return sorted({str(r).strip() for r in roster if str(r).strip()})
    except Exception:
        pass
    return None


def _load_current_season_roster(competition):
    """Prefer ``current_season_teams.json`` (promotion/relegation updates)."""
    return _load_roster_from_json(CURRENT_SEASON_TEAMS_FILE, competition)


def _load_upcoming_roster(competition):
    """Load the upcoming-season roster.

    Prefers ``current_season_teams.json`` (manually / pipeline-updated for the
    active season) over the broader historical ``league_teams.json``.
    """
    return _load_current_season_roster(competition) or _load_roster_from_json(
        LEAGUE_TEAMS_FILE, competition
    )


ESPN_LEAGUE_IDS = {
    "England/Premier League": "eng.1",
    "England/Championship": "eng.2",
    "Spain/La Liga": "esp.1",
    "Spain/La Liga 2": "esp.2",
    "Italy/Serie A": "ita.1",
    "Italy/Serie B": "ita.2",
    "Germany/Bundesliga": "ger.1",
    "Germany/Bundesliga 2": "ger.2",
    "France/Ligue 1": "fra.1",
    "France/Ligue 2": "fra.2",
    "Portugal/Liga Portugal": "por.1",
}


def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_future_fixtures_from_espn(competition, year=None, max_days=45):
    espn_id = ESPN_LEAGUE_IDS.get(competition)
    if not espn_id:
        return pd.DataFrame()

    today = pd.Timestamp(datetime.now(UTC).date())
    # Prefer a short near-term crawl; remaining fixtures are filled synthetically.
    try:
        import season_calendar as sc
        _start, season_end = sc.fixture_search_bounds(competition, reference_date=today)
        season_end = max(season_end, today)
    except Exception:
        target_year = int(year or datetime.now(UTC).year)
        season_end = pd.Timestamp(f"{target_year}-12-31")
    end = min(season_end, today + pd.Timedelta(days=max(0, int(max_days))))
    start = today
    rows = []
    seen = set()
    empty_streak = 0
    day = start
    while day <= end:
        url = ESPN_SCOREBOARD_API.format(espn_id=espn_id) + f"?dates={day.strftime('%Y%m%d')}"
        try:
            data = fetch_json(url, timeout=20)
        except Exception:
            empty_streak += 1
            day += timedelta(days=7 if empty_streak >= 5 else 1)
            continue

        events = data.get("events", []) or []
        if not events:
            empty_streak += 1
            day += timedelta(days=7 if empty_streak >= 5 else 1)
            continue
        empty_streak = 0

        for event in events:
            event_date = pd.to_datetime(event.get("date"), utc=True, errors="coerce")
            if pd.isna(event_date):
                continue
            match_date = event_date.tz_convert(EASTERN_TZ).tz_localize(None).normalize()
            if match_date < today or match_date > end:
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

            key = (match_date.strftime("%Y-%m-%d"), home_team, away_team)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "Date": match_date.strftime("%Y-%m-%d"),
                "HomeTeam": home_team,
                "AwayTeam": away_team,
                "FTHG": None,
                "FTAG": None,
                "FTR": "",
            })

        day += timedelta(days=1)

    return pd.DataFrame(rows)


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


def load_context():
    matches, season_files = pm.load_training_matches(pm.PROCESSED_DIR)
    if not os.path.exists(pm.MODEL_CACHE):
        print("[model-cache] cache missing; rebuilding model cache...")
        rebuild_model_cache_once()

    try:
        # Caches pickled from Predict_Match.py as __main__ need this alias when present.
        cls = getattr(pm, "AveragedProbaClassifier", None)
        if cls is not None:
            setattr(sys.modules.get("__main__"), "AveragedProbaClassifier", cls)
    except Exception:
        pass

    try:
        bundle = joblib.load(pm.MODEL_CACHE)
    except Exception as exc:
        print(f"[model-cache] failed to load cache ({exc.__class__.__name__}: {exc}); rebuilding...")
        rebuild_model_cache_once()
        try:
            cls = getattr(pm, "AveragedProbaClassifier", None)
            if cls is not None:
                setattr(sys.modules.get("__main__"), "AveragedProbaClassifier", cls)
        except Exception:
            pass
        bundle = joblib.load(pm.MODEL_CACHE)
    if bundle.get("fingerprint") != pm.data_fingerprint(season_files):
        print("[model-cache] using cached models (data newer than cache; full retrain runs Tue/Fri)")

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
H2H_LEAGUES = set(proj_sched.H2H_TIEBREAKER_COMPETITIONS)


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
    stat_sums = defaultdict(lambda: defaultdict(float))
    position_counts = defaultdict(lambda: defaultdict(int))
    for team in teams:
        stat_sums[team]
        position_counts[team]

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


def _expected_current_season_year(competition):
    """Return the expected start year for the current season of *competition*."""
    try:
        import season_calendar as sc
        return sc.expected_season_start_year(competition)
    except Exception:
        now = datetime.now()
        # Mirror european_season_start_year when season_calendar is unavailable.
        if now.month > 7 or (now.month == 7 and now.day >= 15):
            return now.year
        return now.year - 1


def _peek_csv_team_and_played_counts(raw_file):
    """Return (unique_team_count, played_row_count) for a season CSV."""
    try:
        df = pd.read_csv(raw_file)
    except Exception:
        return 0, 0
    if "HomeTeam" not in df.columns or "AwayTeam" not in df.columns:
        return 0, 0
    homes = df["HomeTeam"].dropna().astype(str).str.strip()
    aways = df["AwayTeam"].dropna().astype(str).str.strip()
    teams = set(homes) | set(aways)
    teams.discard("")
    played = 0
    if "FTR" in df.columns:
        ftr = df["FTR"].astype(str).str.strip()
        played = int(ftr.isin(["H", "D", "A"]).sum())
    elif "FTHG" in df.columns and "FTAG" in df.columns:
        played = int(pd.to_numeric(df["FTHG"], errors="coerce").notna().sum())
    return len(teams), played


def _prefer_csv_path_a(competition, raw_file, csv_start_year, expected_year):
    """True only when the CSV is the expected current season (PATH A).

    Prior-season files always use PATH B so finished results cannot block a
    full synthetic remaining schedule.
    """
    return proj_sched.prefer_current_season_csv(csv_start_year, expected_year)


def _fetch_espn_teams(competition):
    """Fetch current team list from ESPN /teams endpoint.

    Returns a sorted list of team display names, or None on failure.
    """
    espn_id = ESPN_LEAGUE_IDS.get(competition)
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


def project_competition(ctx, competition, raw_file, sim_runs=None):
    if sim_runs is None:
        sim_runs = SIMULATION_RUNS
    sim_runs = COMPETITION_SIM_RUNS.get(competition, sim_runs)

    # Determine if this CSV represents the current season or a past season
    csv_start_year = pm.parse_season_start_year(os.path.basename(raw_file))
    expected_year = _expected_current_season_year(competition)
    use_path_a = _prefer_csv_path_a(competition, raw_file, csv_start_year, expected_year)

    if use_path_a:
        # ── PATH A: Current-season CSV exists ──────────────────────────
        df = pd.read_csv(raw_file).copy()
        required = {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"}
        if not required.issubset(df.columns):
            return [], []

        df["DateParsed"] = pd.to_datetime(df["Date"], dayfirst=True, format="mixed", errors="coerce")
        df = df[df["HomeTeam"].notna() & df["AwayTeam"].notna()]
        df = df.sort_values(["DateParsed", "HomeTeam", "AwayTeam"], na_position="last").reset_index(drop=True)

        raw_teams = set(df["HomeTeam"].astype(str).str.strip()) | set(df["AwayTeam"].astype(str).str.strip())
        teams = sorted({r for t in raw_teams if (r := pm.resolve_team_name(t, ctx["available_teams"]))})
        table = init_table(teams)
        real_matches = []
        future_pairs = []
        future_dates = []
        seen_pairs = set()

        for _, row in df.iterrows():
            raw_home = str(row["HomeTeam"]).strip()
            raw_away = str(row["AwayTeam"]).strip()
            home = pm.resolve_team_name(raw_home, ctx["available_teams"])
            away = pm.resolve_team_name(raw_away, ctx["available_teams"])
            # Skip unresolved names so ESPN/API aliases cannot create duplicate
            # table rows beside the canonical football-data names.
            if not home or not away or home not in table or away not in table:
                continue
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

        # ESPN upcoming fixtures supplement
        espn_fixtures = load_future_fixtures_from_espn(competition)
        if not espn_fixtures.empty:
            _merge_espn_fixtures(competition, espn_fixtures, future_pairs, future_dates, seen_pairs, ctx)

        # Fill remaining gaps with format-aware home/away (or Scottish 3-round) pairs
        _fill_remaining_fixtures(teams, seen_pairs, future_pairs, future_dates, ctx, competition=competition)

    else:
        # ── PATH B: No current-season CSV or CSV is from a past season ──
        # Roster priority: current_season_teams.json → ESPN → league_teams.json
        # (ESPN can lag or mix Championship clubs during the transition window.)
        teams = None

        def _resolve_roster(raw_names, label):
            resolved = set()
            unresolved = []
            for t in raw_names:
                r = pm.resolve_team_name(t, ctx["available_teams"])
                if r:
                    resolved.add(r)
                else:
                    unresolved.append(t)
            if unresolved:
                print(f"  Unresolved {label} teams in {competition}: {sorted(set(unresolved))}")
                _append_mapping_if_missing(
                    competition, unresolved, ctx["available_teams"], _load_upcoming_roster(competition)
                )
                for t in unresolved:
                    r = pm.resolve_team_name(t, ctx["available_teams"])
                    if r:
                        resolved.add(r)
            return sorted(resolved) if resolved else None

        current_roster = _load_current_season_roster(competition)
        if current_roster:
            teams = _resolve_roster(current_roster, "current_season")
            if teams:
                print(f"  PATH B roster from current_season_teams.json ({len(teams)} teams)")

        if not teams:
            espn_teams = _fetch_espn_teams(competition)
            if espn_teams:
                teams = _resolve_roster(espn_teams, "ESPN")
                if teams:
                    print(f"  PATH B roster from ESPN ({len(teams)} teams)")

        if not teams:
            static_roster = _load_roster_from_json(LEAGUE_TEAMS_FILE, competition)
            if not static_roster:
                print(f"  No current-season file and no ESPN/roster data for {competition}")
                return [], []
            teams = _resolve_roster(static_roster, "league_teams") or sorted(static_roster)
            print(f"  PATH B roster from league_teams.json ({len(teams)} teams)")

        games_each = proj_sched.expected_games_per_team(competition, len(teams))
        print(
            f"  No current-season CSV — PATH B ({len(teams)} teams, "
            f"~{games_each} games/team via format-aware round-robin)"
        )
        table = init_table(teams)
        real_matches = []
        future_pairs = []
        future_dates = []
        seen_pairs = set()

        # Do not rely on ESPN upcoming scoreboards for PATH B — synthesize
        # the full remaining slate from the competition format.
        _fill_remaining_fixtures(teams, seen_pairs, future_pairs, future_dates, ctx, competition=competition)

    if not future_pairs:
        print(f"  No fixtures to project for {competition}")
        return [], []

    # ── Common: batch-predict all future fixtures ──────────────────────
    batched = _predict_matches_batch(ctx, future_pairs, competition) if future_pairs else []

    future_predictions = []
    future_rows = []
    for i, (pred_res, phg, pag, probs) in enumerate(batched):
        home, away = future_pairs[i]
        match_date = future_dates[i] if i < len(future_dates) else ""
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

    stat_sums, position_counts = run_monte_carlo(
        teams, table, future_predictions, sim_runs,
        competition=competition, real_matches=real_matches,
    )
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
        }

    table_rows = [
        {
            "competition": competition,
            "team": team,
            **averaged[team],
        }
        for team in teams
    ]

    return table_rows, future_rows


def _merge_espn_fixtures(competition, espn_fixtures, future_pairs, future_dates, seen_pairs, ctx):
    """Resolve ESPN fixture names and add to future_pairs / future_dates."""
    espn_count = 0
    unresolved = []
    for _, row in espn_fixtures.iterrows():
        raw_home = str(row["HomeTeam"]).strip()
        raw_away = str(row["AwayTeam"]).strip()
        if not raw_home or not raw_away:
            continue
        home = pm.resolve_team_name(raw_home, ctx["available_teams"])
        away = pm.resolve_team_name(raw_away, ctx["available_teams"])
        if not home:
            unresolved.append(raw_home)
        if not away:
            unresolved.append(raw_away)
        if not home or not away:
            continue
        if (home, away) in seen_pairs:
            continue
        seen_pairs.add((home, away))
        future_pairs.append((home, away))
        future_dates.append(str(row.get("Date", "")))
        espn_count += 1
    if unresolved:
        msg = f"  ESPN: {len(unresolved)} team name(s) in {competition} could not be resolved — add to team_name_mapping_master.json: {sorted(set(unresolved))}"
        print(f"[WARN] {msg}")
        _append_mapping_if_missing(competition, unresolved, ctx["available_teams"], _load_upcoming_roster(competition))
    if espn_count:
        print(f"  ESPN: loaded {espn_count} future fixtures for {competition}")


def _fill_remaining_fixtures(teams, seen_pairs, future_pairs, future_dates, ctx, competition=None):
    """Generate remaining fixtures from the competition's schedule format.

    Default: double round-robin home/away = (n-1)*2 games per team.
    Special formats (Scottish 3-round, Liga MX single RR) via projection_schedule.
    """
    added = proj_sched.fill_missing_fixtures(
        competition or "",
        teams,
        seen_pairs,
        future_pairs,
        future_dates,
    )
    if added:
        games = proj_sched.expected_games_per_team(competition or "", len(teams))
        print(f"  Generated {added} fixture(s) (format target ~{games} games/team)")


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
        help="Number of parallel workers for per-competition processing (0 = sequential).",
    )
    return parser.parse_args()


def main():
    args = parse_cli_args()
    sim_runs = max(1, int(args.sim_runs))
    comp_workers = max(1, int(args.competition_workers))
    ctx = load_context()
    latest = latest_raw_file_per_competition(RAW_DIR)
    if not latest:
        raise ValueError(f"No raw season files found in {RAW_DIR}")

    # June through mid-July is the European club off-season — skip projections
    # until on/after Jul 15 when the next season (e.g. 26-27) becomes active.
    # Even then, Jul–most of Aug often has no next-season CSV yet; PATH B uses
    # ESPN/roster without requiring that file.
    try:
        import season_calendar as sc
        if sc.is_european_club_offseason():
            european = {
                comp: path for comp, path in latest.items()
                if not sc.competition_uses_calendar_year(comp)
            }
            calendar = {
                comp: path for comp, path in latest.items()
                if sc.competition_uses_calendar_year(comp)
            }
            if european:
                print(
                    f"[skip] European club off-season (Jun–Jul 14) — "
                    f"deferring {len(european)} league projection(s) until Jul 15+"
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
            table_rows, future_rows = project_competition(ctx, competition, path, sim_runs)
            all_tables.extend(table_rows)
            all_future.extend(future_rows)
    else:
        max_workers = min(comp_workers, len(comps))
        print(f"  Processing {len(comps)} competitions with {max_workers} workers")
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(project_competition, ctx, comp, path, sim_runs): comp
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
