import argparse
import json
import math
import os
import random
import re
from collections import Counter, defaultdict, deque
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd

import Predict_Upcoming_National_Team_Games as upcoming_national
import Process_National_Team_Data as national


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREDICTIONS_DIR = os.path.join(BASE_DIR, "Data", "Predictions")
OUT_FILE = os.path.join(PREDICTIONS_DIR, "world_cup_projection.json")
WORLD_CUP_COMPETITION = "FIFA/World Cup"
WORLD_CUP_ESPN_ID = "fifa.world"
GROUP_LABELS = list("ABCDEFGHIJKL")
STAGE_ORDER = {
    "round-of-32": 1,
    "round-of-16": 2,
    "quarterfinals": 3,
    "semifinals": 4,
    "third-place": 5,
    "final": 6,
}
STAGE_DISPLAY = {
    "round-of-32": "Round of 32",
    "round-of-16": "Round of 16",
    "quarterfinals": "Quarterfinal",
    "semifinals": "Semifinal",
    "third-place": "Third Place",
    "final": "Final",
}


def parse_cli_args():
    parser = argparse.ArgumentParser(
        description="Project 2026 FIFA World Cup groups and knockout bracket from national-team predictions."
    )
    parser.add_argument("--year", type=int, default=2026, help="World Cup year to project.")
    parser.add_argument("--start-date", default="2026-06-11", help="Tournament start date (YYYY-MM-DD).")
    parser.add_argument("--end-date", default="2026-07-19", help="Tournament final date (YYYY-MM-DD).")
    parser.add_argument(
        "--rebuild-national-model",
        action="store_true",
        help="Rebuild the national-team model before projecting the World Cup.",
    )
    parser.add_argument(
        "--api-token",
        type=str,
        default=os.getenv("FOOTBALL_DATA_API_TOKEN", "").strip(),
        help="Optional football-data.org token used if the national model needs rebuilding.",
    )
    return parser.parse_args()


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def iter_dates(start_date, end_date):
    current = parse_date(start_date) if isinstance(start_date, str) else start_date
    end = parse_date(end_date) if isinstance(end_date, str) else end_date
    while current <= end:
        yield current
        current += timedelta(days=1)


def ensure_model_bundle(rebuild, api_token):
    if rebuild or not os.path.exists(national.MODEL_CACHE):
        args = SimpleNamespace(
            skip_fetch=False,
            world_cup_only=True,
            lookback_days=national.DEFAULT_LOOKBACK_DAYS,
            rankings_file=national.FIFA_RANKINGS_FILE,
            squad_values_file=national.SQUAD_VALUES_FILE,
            footballdata_io_token=os.getenv("FOOTBALLDATA_IO_TOKEN", "").strip(),
            sportradar_api_key=os.getenv("SPORTRADAR_API_KEY", "").strip(),
        )
        national.run_pipeline(args)
    return national.load_model_bundle()


def fetch_world_cup_fixtures(start_date, end_date):
    rows = []
    seen_ids = set()
    # Fetch tournament days concurrently, then parse in date order to keep output deterministic.
    for _, payload in national.fetch_espn_scoreboard_days(WORLD_CUP_ESPN_ID, iter_dates(start_date, end_date), timeout=30):
        for event in payload.get("events") or []:
            event_id = str(event.get("id", "")).strip()
            if event_id and event_id in seen_ids:
                continue
            parsed = national.parse_espn_event(event, WORLD_CUP_COMPETITION, require_completed=False)
            if not parsed:
                continue
            if event_id:
                seen_ids.add(event_id)
            parsed["event_id"] = event_id
            parsed["espn_name"] = str(event.get("name", "") or "").strip()
            rows.append(parsed)
    rows.sort(key=lambda row: (row.get("match_datetime_utc", ""), row.get("event_id", "")))
    return rows


def is_placeholder_team(name):
    text = str(name or "").lower()
    return any(
        token in text
        for token in [
            "group ",
            "winner",
            "third place",
            "round of",
            "quarterfinal",
            "semifinal",
        ]
    )


def infer_groups(group_fixtures):
    graph = defaultdict(set)
    earliest = {}
    for fixture in group_fixtures:
        home = str(fixture["home_team"]).strip()
        away = str(fixture["away_team"]).strip()
        if not home or not away or is_placeholder_team(home) or is_placeholder_team(away):
            continue
        graph[home].add(away)
        graph[away].add(home)
        match_dt = str(fixture.get("match_datetime_utc", ""))
        earliest[home] = min(earliest.get(home, match_dt), match_dt)
        earliest[away] = min(earliest.get(away, match_dt), match_dt)

    components = []
    seen = set()
    for team in sorted(graph):
        if team in seen:
            continue
        queue = deque([team])
        seen.add(team)
        component = []
        while queue:
            current = queue.popleft()
            component.append(current)
            for other in graph[current]:
                if other not in seen:
                    seen.add(other)
                    queue.append(other)
        component.sort()
        first_match = min(earliest.get(team, "") for team in component)
        components.append((first_match, component))
    components.sort(key=lambda item: (item[0], item[1]))

    team_to_group = {}
    groups = {}
    for idx, (_, teams) in enumerate(components[: len(GROUP_LABELS)]):
        label = GROUP_LABELS[idx]
        groups[label] = teams
        for team in teams:
            team_to_group[team] = label
    return groups, team_to_group


def empty_table_row(team):
    return {
        "team": team,
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


def apply_result(table, home, away, hg, ag, source):
    home_row = table.setdefault(home, empty_table_row(home))
    away_row = table.setdefault(away, empty_table_row(away))
    for row, gf, ga in [(home_row, hg, ag), (away_row, ag, hg)]:
        row["P"] += 1
        row["GF"] += int(gf)
        row["GA"] += int(ga)
        row["GD"] = row["GF"] - row["GA"]
        if source == "real":
            row["PlayedReal"] += 1
        else:
            row["PlayedPred"] += 1
    if hg > ag:
        home_row["W"] += 1
        away_row["L"] += 1
        home_row["Pts"] += 3
        return "H"
    if ag > hg:
        away_row["W"] += 1
        home_row["L"] += 1
        away_row["Pts"] += 3
        return "A"
    home_row["D"] += 1
    away_row["D"] += 1
    home_row["Pts"] += 1
    away_row["Pts"] += 1
    return "D"


def coerce_scoreline(prediction, allow_draw=True):
    result = str(prediction.get("predicted_result", "")).strip().upper()
    hg = int(round(float(prediction.get("pred_home_goals", 0.0) or 0.0)))
    ag = int(round(float(prediction.get("pred_away_goals", 0.0) or 0.0)))
    hg = max(0, hg)
    ag = max(0, ag)
    if result == "H" and hg <= ag:
        hg = ag + 1
    elif result == "A" and ag <= hg:
        ag = hg + 1
    elif result == "D" and allow_draw:
        ag = hg
    elif result == "D" and not allow_draw:
        # For knockout rounds, resolve draws using higher non-draw probability
        prob_home = float(prediction.get("prob_home", 0.0) or 0.0)
        prob_away = float(prediction.get("prob_away", 0.0) or 0.0)
        if prob_home > prob_away:
            result = "H"
            hg = ag + 1
        elif prob_away > prob_home:
            result = "A"
            ag = hg + 1
        else:
            # Exact tie: use deterministic tie-breaker (home team advantage)
            result = "H"
            hg = ag + 1
    return result, hg, ag


def _adjust_scoreline_from_probs(prob_h, prob_d, prob_a, hg, ag, allow_draw):
    """Apply the same goal adjustments as coerce_scoreline but using the
    most-likely outcome from the prob tuple instead of a sampled result.
    """
    if prob_h >= prob_d and prob_h >= prob_a:
        result = "H"
    elif prob_a >= prob_h and prob_a >= prob_d:
        result = "A"
    else:
        result = "D"
    hg = max(0, int(hg))
    ag = max(0, int(ag))
    if result == "H" and hg <= ag:
        hg = ag + 1
    elif result == "A" and ag <= hg:
        ag = hg + 1
    elif result == "D" and allow_draw:
        ag = hg
    elif result == "D" and not allow_draw:
        if prob_h > prob_a:
            result = "H"
            hg = ag + 1
        elif prob_a > prob_h:
            result = "A"
            ag = hg + 1
        else:
            result = "H"
            hg = ag + 1
    return result, hg, ag


def actual_result_from_fixture(fixture):
    ftr = str(fixture.get("FTR", "")).strip().upper()
    hg = pd.to_numeric(fixture.get("FTHG"), errors="coerce")
    ag = pd.to_numeric(fixture.get("FTAG"), errors="coerce")
    if ftr in {"H", "D", "A"} and pd.notna(hg) and pd.notna(ag):
        return ftr, int(hg), int(ag)
    return None


def _split_fixtures_by_status(fixtures):
    """Partition group fixtures into (played, unplayed) using actual_result_from_fixture."""
    played = []
    unplayed = []
    for fixture in fixtures:
        if actual_result_from_fixture(fixture):
            played.append(fixture)
        else:
            unplayed.append(fixture)
    return played, unplayed


def _count_real_fixtures_per_team(fixtures):
    """Return {team: count} of real (already-played) fixtures per team across all given fixtures."""
    counts = defaultdict(int)
    for fixture in fixtures:
        if not actual_result_from_fixture(fixture):
            continue
        home = str(fixture.get("home_team", "")).strip()
        away = str(fixture.get("away_team", "")).strip()
        if home:
            counts[home] += 1
        if away:
            counts[away] += 1
    return counts


def predict_team_match(bundle, home, away, stage, match_datetime="", venue="", allow_draw=True):
    row = {
        "match_date": str(match_datetime)[:10] if match_datetime else "",
        "match_datetime_utc": match_datetime,
        "competition": WORLD_CUP_COMPETITION,
        "stage": stage,
        "venue": venue,
        "home_team": home,
        "away_team": away,
        "is_neutral_site": True,
        "source": "projection",
    }
    prediction = upcoming_national.predict_fixture(row, bundle)
    if not prediction:
        raise ValueError(f"Could not predict World Cup match: {home} vs {away}")
    result, hg, ag = coerce_scoreline(prediction, allow_draw=allow_draw)
    # Ensure no draws in knockout rounds (secondary safety check)
    if not allow_draw and result == "D":
        prob_home = float(prediction.get("prob_home", 0.0) or 0.0)
        prob_away = float(prediction.get("prob_away", 0.0) or 0.0)
        if prob_home > prob_away:
            result = "H"
            hg = max(hg, ag + 1)
        elif prob_away > prob_home:
            result = "A"
            ag = max(ag, hg + 1)
        else:
            # Exact tie: use home team advantage
            result = "H"
            hg = max(hg, ag + 1)
    prediction["predicted_result"] = result
    prediction["pred_home_goals"] = hg
    prediction["pred_away_goals"] = ag
    prediction["winner_team"] = home if result == "H" else away if result == "A" else "Draw"
    return prediction


def fixture_to_prediction(bundle, fixture):
    actual = actual_result_from_fixture(fixture)
    if actual:
        result, hg, ag = actual
        return {
            "prediction_key": national.make_prediction_key(
                fixture["match_date"], WORLD_CUP_COMPETITION, fixture["home_team"], fixture["away_team"]
            ),
            "match_date": fixture.get("match_date", ""),
            "match_datetime_utc": fixture.get("match_datetime_utc", ""),
            "competition": WORLD_CUP_COMPETITION,
            "stage": fixture.get("stage", ""),
            "venue": fixture.get("venue", ""),
            "home_team": fixture["home_team"],
            "away_team": fixture["away_team"],
            "predicted_result": result,
            "prob_home": 1.0 if result == "H" else 0.0,
            "prob_draw": 1.0 if result == "D" else 0.0,
            "prob_away": 1.0 if result == "A" else 0.0,
            "pred_home_goals": hg,
            "pred_away_goals": ag,
            "winner_team": fixture["home_team"] if result == "H" else fixture["away_team"] if result == "A" else "Draw",
            "source": "real",
        }
    prediction = predict_team_match(
        bundle,
        fixture["home_team"],
        fixture["away_team"],
        fixture.get("stage", "group-stage"),
        fixture.get("match_datetime_utc", ""),
        fixture.get("venue", ""),
        allow_draw=True,
    )
    prediction["source"] = "predicted"
    return prediction


def _resolve_pair_names(home, away, bundle):
    snapshot = bundle.get("snapshot", {}) if bundle else {}
    known_teams = snapshot.get("known_teams", [])
    return (
        national.resolve_team_name(home, known_teams),
        national.resolve_team_name(away, known_teams),
    )


def _build_pair_feature_rows(pairs, competition, stage, is_neutral_site, bundle):
    team_context = bundle.get("snapshot", {}).get("team_context", {}) if bundle else {}
    raw_rows = []
    for home, away in pairs:
        raw_rows.append(
            national.build_feature_row(home, away, competition, stage, is_neutral_site, team_context)
        )
    combined = pd.DataFrame(raw_rows)
    if not combined.empty:
        combined = pd.get_dummies(combined, columns=national.CATEGORICAL_FEATURE_COLUMNS, dtype=float)
        combined = combined.fillna(0.0)
    return combined


def precompute_knockout_pair_cache(bundle, teams, stage="quarterfinals"):
    """Batch-predict every ordered (home, away) pair for the knockout stage.

    Returns a dict keyed by (home, away) -> (p_home, p_draw, p_away, hg, ag) where
    probs already include adjust_for_knockout + probability_jitter, and hg/ag are
    the raw regressor scoreline values (rounded). The simulation loop applies
    the same coerce_scoreline-style consistency adjustment per sampled result.
    """
    if not teams or len(teams) < 2:
        return {}
    ordered_pairs = [(home, away) for home in teams for away in teams if home != away]
    resolved_pairs = [_resolve_pair_names(home, away, bundle) for home, away in ordered_pairs]
    dedup_pairs = []
    seen = set()
    for home, away in resolved_pairs:
        if not home or not away or home == away:
            continue
        key = (home, away)
        if key in seen:
            continue
        seen.add(key)
        dedup_pairs.append(key)
    resolved_pairs = dedup_pairs
    raw_frame = _build_pair_feature_rows(
        resolved_pairs, WORLD_CUP_COMPETITION, stage, True, bundle
    )
    if raw_frame.empty:
        return {}
    aligned = national.align_feature_frame(raw_frame, bundle)
    proba = bundle["clf"].predict_proba(aligned)
    hg_pred = np.asarray(bundle["home_goal_reg"].predict(aligned), dtype=float)
    ag_pred = np.asarray(bundle["away_goal_reg"].predict(aligned), dtype=float)
    classes = bundle["clf"].classes_
    labels = bundle["result_label_encoder"].inverse_transform(classes)
    proba_index = {label: proba[:, idx] for idx, label in enumerate(labels)}
    jitter_delta = 0.018 if "world cup" in WORLD_CUP_COMPETITION.lower() else 0.026
    cache = {}
    for idx, (home, away) in enumerate(resolved_pairs):
        probabilities = {
            "H": float(proba_index.get("H", np.zeros(len(resolved_pairs)))[idx]),
            "D": float(proba_index.get("D", np.zeros(len(resolved_pairs)))[idx]),
            "A": float(proba_index.get("A", np.zeros(len(resolved_pairs)))[idx]),
        }
        probabilities = upcoming_national.adjust_for_knockout(probabilities, stage)
        prediction_key = national.make_prediction_key("", WORLD_CUP_COMPETITION, home, away)
        probabilities = national.probability_jitter(probabilities, prediction_key, jitter_delta)
        hg = max(0, int(round(float(hg_pred[idx]))))
        ag = max(0, int(round(float(ag_pred[idx]))))
        cache[(home, away)] = (probabilities["H"], probabilities["D"], probabilities["A"], hg, ag)
    return cache


def precompute_group_fixture_cache(bundle, group_fixtures):
    """Batch-predict the deterministic group-stage fixtures so the simulation loop can
    do a dict lookup instead of running predict_team_match for every sim.
    """
    if not group_fixtures:
        return {}
    ordered_pairs = []
    resolved_pairs = []
    raw_features = []
    for fixture in group_fixtures:
        home = str(fixture.get("home_team", "")).strip()
        away = str(fixture.get("away_team", "")).strip()
        if not home or not away or is_placeholder_team(home) or is_placeholder_team(away):
            continue
        home_resolved, away_resolved = _resolve_pair_names(home, away, bundle)
        if not home_resolved or not away_resolved or home_resolved == away_resolved:
            continue
        stage = str(fixture.get("stage", "group-stage") or "group-stage").strip().lower() or "group-stage"
        ordered_pairs.append((home_resolved, away_resolved))
        raw_features.append(
            national.build_feature_row(
                home_resolved, away_resolved, WORLD_CUP_COMPETITION, stage, True,
                bundle.get("snapshot", {}).get("team_context", {}),
            )
        )
        resolved_pairs.append((home_resolved, away_resolved, stage, fixture))
    if not raw_features:
        return {}
    combined = pd.DataFrame(raw_features)
    combined = pd.get_dummies(combined, columns=national.CATEGORICAL_FEATURE_COLUMNS, dtype=float)
    combined = combined.fillna(0.0)
    aligned = national.align_feature_frame(combined, bundle)
    proba = bundle["clf"].predict_proba(aligned)
    hg_pred = np.asarray(bundle["home_goal_reg"].predict(aligned), dtype=float)
    ag_pred = np.asarray(bundle["away_goal_reg"].predict(aligned), dtype=float)
    classes = bundle["clf"].classes_
    labels = bundle["result_label_encoder"].inverse_transform(classes)
    proba_index = {label: proba[:, idx] for idx, label in enumerate(labels)}
    jitter_delta = 0.018 if "world cup" in WORLD_CUP_COMPETITION.lower() else 0.026
    cache = {}
    for idx, (home, away, stage, fixture) in enumerate(resolved_pairs):
        probabilities = {
            "H": float(proba_index.get("H", np.zeros(len(resolved_pairs)))[idx]),
            "D": float(proba_index.get("D", np.zeros(len(resolved_pairs)))[idx]),
            "A": float(proba_index.get("A", np.zeros(len(resolved_pairs)))[idx]),
        }
        probabilities = upcoming_national.adjust_for_knockout(probabilities, stage)
        prediction_key = national.make_prediction_key(
            fixture.get("match_date"), WORLD_CUP_COMPETITION, home, away
        )
        probabilities = national.probability_jitter(probabilities, prediction_key, jitter_delta)
        hg = max(0, int(round(float(hg_pred[idx]))))
        ag = max(0, int(round(float(ag_pred[idx]))))
        cache[(home, away)] = {
            "probs": (probabilities["H"], probabilities["D"], probabilities["A"]),
            "hg": hg,
            "ag": ag,
        }
    return cache


def head_to_head_stats(teams, fixture_predictions):
    team_set = set(teams)
    stats = {team: {"Pts": 0, "GD": 0, "GF": 0} for team in teams}
    for item in fixture_predictions:
        home = item["home_team"]
        away = item["away_team"]
        if home not in team_set or away not in team_set:
            continue
        hg = int(item["pred_home_goals"])
        ag = int(item["pred_away_goals"])
        stats[home]["GF"] += hg
        stats[home]["GD"] += hg - ag
        stats[away]["GF"] += ag
        stats[away]["GD"] += ag - hg
        if hg > ag:
            stats[home]["Pts"] += 3
        elif ag > hg:
            stats[away]["Pts"] += 3
        else:
            stats[home]["Pts"] += 1
            stats[away]["Pts"] += 1
    return stats


def rank_group_rows(rows, fixture_predictions):
    base_groups = defaultdict(list)
    for row in rows:
        base_groups[(row["Pts"], row["GD"], row["GF"])].append(row)

    ranked = []
    for key in sorted(base_groups.keys(), key=lambda item: (-item[0], -item[1], -item[2])):
        tied_rows = base_groups[key]
        if len(tied_rows) == 1:
            ranked.extend(tied_rows)
            continue
        teams = [row["team"] for row in tied_rows]
        h2h = head_to_head_stats(teams, fixture_predictions)
        ranked.extend(
            sorted(
                tied_rows,
                key=lambda row: (
                    -h2h[row["team"]]["Pts"],
                    -h2h[row["team"]]["GD"],
                    -h2h[row["team"]]["GF"],
                    row["team"],
                ),
            )
        )
    for idx, row in enumerate(ranked, start=1):
        row["position"] = idx
    return ranked


def project_groups(bundle, group_fixtures, groups, team_to_group):
    tables = {group: {team: empty_table_row(team) for team in teams} for group, teams in groups.items()}
    fixtures_by_group = {group: [] for group in groups}
    for fixture in group_fixtures:
        group = team_to_group.get(fixture["home_team"]) or team_to_group.get(fixture["away_team"])
        if not group:
            continue
        prediction = fixture_to_prediction(bundle, fixture)
        _, hg, ag = coerce_scoreline(prediction, allow_draw=True)
        source = "real" if prediction.get("source") == "real" else "predicted"
        result = apply_result(tables[group], fixture["home_team"], fixture["away_team"], hg, ag, source=source)
        prediction["predicted_result"] = result
        prediction["pred_home_goals"] = hg
        prediction["pred_away_goals"] = ag
        prediction["group"] = group
        fixtures_by_group[group].append(prediction)

    group_tables = []
    for group in GROUP_LABELS:
        if group not in tables:
            continue
        rows = [dict(row) for row in tables[group].values()]
        ranked = rank_group_rows(rows, fixtures_by_group.get(group, []))
        group_tables.append({"group": group, "teams": ranked})
    return group_tables, fixtures_by_group


def rank_third_place_teams(group_tables):
    thirds = []
    for group in group_tables:
        teams = group.get("teams", [])
        if len(teams) >= 3:
            third = dict(teams[2])
            third["group"] = group["group"]
            thirds.append(third)
    thirds.sort(key=lambda row: (-row["Pts"], -row["GD"], -row["GF"], row["group"], row["team"]))
    for idx, row in enumerate(thirds, start=1):
        row["third_rank"] = idx
        row["qualified"] = idx <= 8
    return thirds


def _round_group_stats(W_mean, D_mean, L_mean, P):
    """Round sim-averaged W/D/L into integers that sum to P.

    Rules:
      - W is rounded up first (ceil), capped at P ("if possible").
      - D and L are each rounded to the nearest integer (round-half-up).
      - When the rounded D + L doesn't match P - W, the side with the largest
        distance from its rounded value (the least certain) is adjusted: drop
        if the sum overshoots, add if it undershoots. Ties break D-first.
      - Pts = 3*W + D (self-consistent standard scoring).

    Returns: (W, D, L, Pts) as integers.
    """
    W_mean = max(0.0, float(W_mean))
    D_mean = max(0.0, float(D_mean))
    L_mean = max(0.0, float(L_mean))
    P = int(P)

    # 1. Wins: round up first, cap at P.
    W = min(int(math.ceil(W_mean)), P)
    remaining = P - W

    # 2. Round D and L to nearest (round-half-up).
    def _round_half_up(x):
        return int(math.floor(x + 0.5))

    D = _round_half_up(D_mean)
    L = _round_half_up(L_mean)
    target = remaining

    def _sorted_by_distance():
        # Distance from each side's mean to its current rounded value.
        # The "less certain" side is the one further from its rounded value.
        D_frac = round(abs(D_mean - D), 10)
        L_frac = round(abs(L_mean - L), 10)
        return sorted(
            [("D", D_frac), ("L", L_frac)],
            key=lambda item: (-item[1], item[0]),  # largest distance first; D-first on tie
        )

    # Cap D + L at target by dropping the least certain side first.
    while D + L > target:
        dropped = False
        for label, _ in _sorted_by_distance():
            if label == "D" and D > 0:
                D -= 1
                dropped = True
                break
            if label == "L" and L > 0:
                L -= 1
                dropped = True
                break
        if not dropped:
            break

    # Fill D + L up to target by adding to the least certain side first.
    while D + L < target:
        for label, _ in _sorted_by_distance():
            if label == "D":
                D += 1
                break
            else:
                L += 1
                break

    Pts = 3 * W + D
    return W, D, L, Pts


def aggregate_sim_group_tables(sims_group_tables, num_sims, real_fixture_counts=None):
    """Aggregate per-simulation group tables into one set of group tables.

    For each team across all sims, computes:
      - `position`: mode (most common finishing position); final 1-4 ordering
        breaks ties via rounded Pts/GD/GF, then team name.
      - `W`, `D`, `L`, `Pts`: integers. W is rounded up first (ceil), then D
        and L are allocated by the largest-remainder method so D + L = P - W,
        and Pts = 3*W + D. See `_round_group_stats`.
      - `GF`, `GA`, `GD`: integers (mean rounded to nearest).
      - `P`, `PlayedReal`, `PlayedPred`: deterministic. PlayedReal comes from
        the live ESPN fixture count; PlayedPred = 3 - PlayedReal (WC group
        stage has 3 games per team).

    Returns: list of {"group": g, "teams": [rows]} sorted by (position, -Pts, -GD, -GF, team).
    """
    real_fixture_counts = real_fixture_counts or {}
    per_team_history = defaultdict(lambda: defaultdict(list))

    for sim_tables in sims_group_tables:
        for group_entry in sim_tables:
            group = group_entry["group"]
            for team_row in group_entry["teams"]:
                team = team_row["team"]
                per_team_history[group][team].append({
                    "position": team_row.get("position", 99),
                    "W": team_row.get("W", 0),
                    "D": team_row.get("D", 0),
                    "L": team_row.get("L", 0),
                    "Pts": team_row.get("Pts", 0),
                    "GF": team_row.get("GF", 0),
                    "GA": team_row.get("GA", 0),
                    "GD": team_row.get("GD", 0),
                })

    aggregated = []
    for group in sorted(per_team_history.keys()):
        team_rows = []
        for team, history in per_team_history[group].items():
            positions = [h["position"] for h in history]
            position_counter = Counter(positions)
            mode_position = position_counter.most_common(1)[0][0]
            n = len(history)
            mean_W = sum(h["W"] for h in history) / n
            mean_D = sum(h["D"] for h in history) / n
            mean_L = sum(h["L"] for h in history) / n
            mean_GF = sum(h["GF"] for h in history) / n
            mean_GA = sum(h["GA"] for h in history) / n
            mean_GD = sum(h["GD"] for h in history) / n
            played_real = real_fixture_counts.get(team, 0)
            played_pred = 3 - played_real
            P = played_real + played_pred
            W, D, L, Pts = _round_group_stats(mean_W, mean_D, mean_L, P)
            GF = int(round(mean_GF))
            GA = int(round(mean_GA))
            GD = GF - GA
            team_rows.append({
                "team": team,
                "position": mode_position,
                "P": P,
                "W": W,
                "D": D,
                "L": L,
                "Pts": Pts,
                "GF": GF,
                "GA": GA,
                "GD": GD,
                "PlayedReal": played_real,
                "PlayedPred": played_pred,
            })
        # Break mode-position ties via rounded Pts/GD/GF (deterministic),
        # then re-assign final 1-4 positions by sorted order.
        team_rows.sort(key=lambda row: (row["position"], -row["Pts"], -row["GD"], -row["GF"], row["team"]))
        for idx, row in enumerate(team_rows, start=1):
            row["position"] = idx
        aggregated.append({"group": group, "teams": team_rows})
    return aggregated


def aggregate_sim_third_place(sims_third_place, num_sims):
    """Aggregate per-simulation 3rd-place tables.

    For each group, find the most common 3rd-place team (mode across sims) and
    report its sim-averaged Pts/GD/GF across all sims (regardless of whether
    it actually finished 3rd in each sim — the average represents the team's
    expected performance in that group slot). Rank all 12 across groups by
    sim-averaged Pts/GD/GF; mark top 8 as qualified.
    """
    third_place_counts = defaultdict(Counter)
    team_stats_by_group = defaultdict(lambda: defaultdict(list))

    for sim_third in sims_third_place:
        for row in sim_third:
            group = row["group"]
            team = row["team"]
            third_place_counts[group][team] += 1
            team_stats_by_group[group][team].append({
                "Pts": row.get("Pts", 0),
                "GD": row.get("GD", 0),
                "GF": row.get("GF", 0),
            })

    aggregated_thirds = []
    for group in sorted(third_place_counts.keys()):
        counter = third_place_counts[group]
        mode_team, _ = counter.most_common(1)[0]
        history = team_stats_by_group[group][mode_team]
        n = len(history)
        if n:
            mean_pts = sum(h["Pts"] for h in history) / n
            mean_gd = sum(h["GD"] for h in history) / n
            mean_gf = sum(h["GF"] for h in history) / n
        else:
            mean_pts = mean_gd = mean_gf = 0
        aggregated_thirds.append({
            "group": group,
            "team": mode_team,
            "Pts": round(mean_pts, 2),
            "GD": round(mean_gd, 2),
            "GF": round(mean_gf, 2),
        })

    aggregated_thirds.sort(key=lambda row: (-row["Pts"], -row["GD"], -row["GF"], row["group"], row["team"]))
    for idx, row in enumerate(aggregated_thirds, start=1):
        row["third_rank"] = idx
        row["qualified"] = idx <= 8
    return aggregated_thirds


def parse_group_slot(slot_name):
    text = str(slot_name or "").strip()
    match = re.match(r"Group ([A-L]) (Winner|2nd Place)$", text, flags=re.IGNORECASE)
    if not match:
        return None
    group = match.group(1).upper()
    position = 1 if match.group(2).lower() == "winner" else 2
    return group, position


def parse_third_slot(slot_name):
    text = str(slot_name or "").strip()
    match = re.match(r"Third Place Group ([A-L](?:/[A-L])*)$", text, flags=re.IGNORECASE)
    if not match:
        return None
    return [part.upper() for part in match.group(1).split("/")]


def parse_previous_winner_slot(slot_name):
    text = str(slot_name or "").strip()
    patterns = [
        (r"Round of 32 (\d+) Winner", "Round of 32"),
        (r"Round of 16 (\d+) Winner", "Round of 16"),
        (r"Quarterfinal (\d+) Winner", "Quarterfinal"),
        (r"Semifinal (\d+) Winner", "Semifinal"),
    ]
    for pattern, round_name in patterns:
        match = re.match(pattern + r"$", text, flags=re.IGNORECASE)
        if match:
            return f"{round_name} {int(match.group(1))}"
    return None


def extract_competitor_slots(fixture):
    home_slot = ""
    away_slot = ""
    for side in ["home_team", "away_team"]:
        value = str(fixture.get(side, "")).strip()
        if side == "home_team":
            home_slot = value
        else:
            away_slot = value
    return home_slot, away_slot


def resolve_third_place_slots(round_of_32_fixtures, third_place_table):
    qualified = {row["group"]: row for row in third_place_table if row.get("qualified")}
    third_slots = []
    for idx, fixture in enumerate(round_of_32_fixtures):
        for side in ["home_team", "away_team"]:
            candidates = parse_third_slot(fixture.get(side))
            if candidates:
                third_slots.append(
                    {
                        "id": f"{idx}:{side}",
                        "fixture_idx": idx,
                        "side": side,
                        "candidates": candidates,
                    }
                )

    rank_by_group = {row["group"]: int(row["third_rank"]) for row in third_place_table}
    slots_by_constraint = sorted(
        third_slots,
        key=lambda slot: (
            len([group for group in slot["candidates"] if group in qualified]),
            slot["fixture_idx"],
            slot["side"],
        ),
    )

    def feasible(remaining_slots, used_groups):
        for slot in remaining_slots:
            if not any(group in qualified and group not in used_groups for group in slot["candidates"]):
                return False
        return True

    def backtrack(slot_idx, used_groups, assignments):
        if slot_idx >= len(slots_by_constraint):
            return assignments
        slot = slots_by_constraint[slot_idx]
        choices = [group for group in slot["candidates"] if group in qualified and group not in used_groups]
        choices.sort(key=lambda group: rank_by_group.get(group, 999))
        for group in choices:
            next_used = set(used_groups)
            next_used.add(group)
            remaining = slots_by_constraint[slot_idx + 1 :]
            if not feasible(remaining, next_used):
                continue
            next_assignments = dict(assignments)
            next_assignments[slot["id"]] = group
            solved = backtrack(slot_idx + 1, next_used, next_assignments)
            if solved is not None:
                return solved
        return None

    return backtrack(0, set(), {}) or {}


def group_qualifier_lookup(group_tables):
    lookup = {}
    for group in group_tables:
        group_label = group["group"]
        teams = group.get("teams", [])
        for idx, row in enumerate(teams, start=1):
            lookup[(group_label, idx)] = row["team"]
    return lookup


def resolve_slot(slot_name, match_idx, side, group_lookup, third_assignments, third_place_table, winners):
    group_slot = parse_group_slot(slot_name)
    if group_slot:
        return group_lookup.get(group_slot)
    third_candidates = parse_third_slot(slot_name)
    if third_candidates:
        group = third_assignments.get(f"{match_idx}:{side}")
        if not group:
            return None
        by_group = {row["group"]: row["team"] for row in third_place_table if row.get("qualified")}
        return by_group.get(group)
    previous_key = parse_previous_winner_slot(slot_name)
    if previous_key:
        return winners.get(previous_key)
    if is_placeholder_team(slot_name):
        return None
    return slot_name


def knockout_round_key(stage):
    return {
        "round-of-32": "round_of_32",
        "round-of-16": "round_of_16",
        "quarterfinals": "quarterfinals",
        "semifinals": "semifinals",
        "third-place": "third_place",
        "final": "final",
    }.get(stage, stage.replace("-", "_"))


def project_knockout(bundle, knockout_fixtures, group_tables, third_place_table):
    fixtures_by_stage = defaultdict(list)
    for fixture in knockout_fixtures:
        stage = str(fixture.get("stage", "")).strip().lower()
        if stage in STAGE_ORDER:
            fixtures_by_stage[stage].append(fixture)
    for stage in fixtures_by_stage:
        fixtures_by_stage[stage].sort(key=lambda row: (row.get("match_datetime_utc", ""), row.get("event_id", "")))

    group_lookup = group_qualifier_lookup(group_tables)
    third_assignments = resolve_third_place_slots(fixtures_by_stage.get("round-of-32", []), third_place_table)
    winners = {}
    projected = {}

    for stage in sorted(fixtures_by_stage.keys(), key=lambda value: STAGE_ORDER[value]):
        round_rows = []
        round_name = STAGE_DISPLAY[stage]
        for idx, fixture in enumerate(fixtures_by_stage[stage], start=1):
            match_idx_zero = idx - 1
            home_slot, away_slot = extract_competitor_slots(fixture)
            home = resolve_slot(
                home_slot,
                match_idx_zero,
                "home_team",
                group_lookup,
                third_assignments,
                third_place_table,
                winners,
            )
            away = resolve_slot(
                away_slot,
                match_idx_zero,
                "away_team",
                group_lookup,
                third_assignments,
                third_place_table,
                winners,
            )
            label = f"{round_name} {idx}"
            if not home or not away:
                row = {
                    "label": label,
                    "stage": stage,
                    "match_date": fixture.get("match_date", ""),
                    "match_datetime_utc": fixture.get("match_datetime_utc", ""),
                    "venue": fixture.get("venue", ""),
                    "home_slot": home_slot,
                    "away_slot": away_slot,
                    "home_team": home or home_slot,
                    "away_team": away or away_slot,
                    "winner": "",
                    "predicted_result": "",
                    "prob_home": 0.0,
                    "prob_draw": 0.0,
                    "prob_away": 0.0,
                    "pred_home_goals": None,
                    "pred_away_goals": None,
                }
            else:
                prediction = predict_team_match(
                    bundle,
                    home,
                    away,
                    stage,
                    fixture.get("match_datetime_utc", ""),
                    fixture.get("venue", ""),
                    allow_draw=False,
                )
                # Determine winner: should never be a draw in knockout rounds
                if prediction["predicted_result"] == "H":
                    winner = home
                elif prediction["predicted_result"] == "A":
                    winner = away
                else:
                    # Safety check - should not happen
                    raise ValueError(f"Unexpected draw result in knockout match: {home} vs {away}")
                winners[label] = winner
                row = {
                    "label": label,
                    "stage": stage,
                    "match_date": fixture.get("match_date", ""),
                    "match_datetime_utc": fixture.get("match_datetime_utc", ""),
                    "venue": fixture.get("venue", ""),
                    "home_slot": home_slot,
                    "away_slot": away_slot,
                    "home_team": home,
                    "away_team": away,
                    "winner": winner,
                    "predicted_result": prediction["predicted_result"],
                    "prob_home": round(float(prediction.get("prob_home", 0.0)), 6),
                    "prob_draw": round(float(prediction.get("prob_draw", 0.0)), 6),
                    "prob_away": round(float(prediction.get("prob_away", 0.0)), 6),
                    "pred_home_goals": int(prediction["pred_home_goals"]),
                    "pred_away_goals": int(prediction["pred_away_goals"]),
                }
            round_rows.append(row)
        projected[knockout_round_key(stage)] = round_rows
    return projected, winners


def simulate_match_result(prob_tuple, rng=None):
    """Sample a match result (H/D/A) from a probability tuple.

    Accepts either a 3-tuple (p_home, p_draw, p_away) or a dict-like with
    `prob_home`/`prob_draw`/`prob_away` keys for backward compatibility.
    """
    if isinstance(prob_tuple, dict):
        p_home = float(prob_tuple.get("prob_home", 0.0))
        p_draw = float(prob_tuple.get("prob_draw", 0.0))
        p_away = float(prob_tuple.get("prob_away", 0.0))
    else:
        p_home, p_draw, p_away = float(prob_tuple[0]), float(prob_tuple[1]), float(prob_tuple[2])
    total = p_home + p_draw + p_away
    if total > 0:
        p_home, p_draw, p_away = p_home / total, p_draw / total, p_away / total
    else:
        p_home, p_draw, p_away = 1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0
    if rng is None:
        rng = random
    u = rng.random()
    if u < p_home:
        return "H"
    if u < p_home + p_draw:
        return "D"
    return "A"


def simulate_group_stage(group_fixtures, groups, team_to_group, group_fixture_cache=None, bundle=None, rng=None):
    """Simulate group stage and return final group tables. Returns dict of group -> list of teams ranked.

    For fixtures with a real result (live WC support via ESPN), the result is
    applied deterministically (PlayedReal++). For unplayed fixtures, the
    outcome is sampled from the precomputed cache (PlayedPred++). Sim-averaged
    stats naturally reflect both the deterministic real results and the
    sampled predictions.
    """
    tables = {}
    for group in groups:
        tables[group] = {team: empty_table_row(team) for team in groups[group]}

    for fixture in group_fixtures:
        home = str(fixture.get("home_team", "")).strip()
        away = str(fixture.get("away_team", "")).strip()
        group = team_to_group.get(home) or team_to_group.get(away)
        if not group:
            continue

        # Apply real result directly if the fixture has already been played
        # (live WC support — don't re-predict games that already happened).
        actual = actual_result_from_fixture(fixture)
        if actual is not None:
            result, hg, ag = actual
            apply_result(tables[group], home, away, hg, ag, source="real")
            continue

        probs = None
        raw_hg = None
        raw_ag = None
        if group_fixture_cache is not None:
            cache_entry = group_fixture_cache.get((home, away))
            if cache_entry is not None:
                probs = cache_entry["probs"]
                raw_hg = cache_entry["hg"]
                raw_ag = cache_entry["ag"]
        if (probs is None or raw_hg is None or raw_ag is None) and bundle is not None:
            prediction = predict_team_match(
                bundle, home, away, "group-stage",
                fixture.get("match_datetime_utc", ""), fixture.get("venue", ""), allow_draw=True,
            )
            probs = (prediction.get("prob_home", 0.0), prediction.get("prob_draw", 0.0), prediction.get("prob_away", 0.0))
            raw_hg = int(prediction.get("pred_home_goals", 0))
            raw_ag = int(prediction.get("pred_away_goals", 0))
        if probs is None:
            continue

        result = simulate_match_result(probs, rng=rng)
        # Apply coerce_scoreline-style adjustment to the raw regressor goals so the
        # sim standings stay consistent with the deterministic projection's
        # behavior (sampled result drives a consistent scoreline).
        _, hg, ag = _adjust_scoreline_from_probs(
            float(probs[0]), float(probs[1]), float(probs[2]),
            raw_hg, raw_ag, allow_draw=True,
        )
        # _adjust_scoreline_from_probs uses the argmax result; override with the
        # sampled result so per-sim randomness flows through to the scoreline.
        if result == "D":
            hg, ag = raw_hg, raw_ag
            ag = hg
        elif result == "H" and hg <= ag:
            hg = ag + 1
        elif result == "A" and ag <= hg:
            ag = hg + 1

        apply_result(tables[group], home, away, hg, ag, source="predicted")

    final_tables = {}
    for group, group_tables in tables.items():
        team_rows = list(group_tables.values())
        ranked = rank_group_rows(team_rows, [])
        final_tables[group] = ranked

    return final_tables


def simulate_knockout_match(probs, raw_hg, raw_ag, allow_draw=False, rng=None):
    """Simulate a knockout match using precomputed probs + raw goals. Returns (result, hg, ag)."""
    result = simulate_match_result(probs, rng=rng)
    if result == "D" and not allow_draw:
        p_home = float(probs[0])
        p_away = float(probs[2])
        if p_home > p_away:
            result = "H"
        elif p_away > p_home:
            result = "A"
        else:
            result = "H"
    hg, ag = int(raw_hg), int(raw_ag)
    if result == "H" and hg <= ag:
        hg = ag + 1
    elif result == "A" and ag <= hg:
        ag = hg + 1
    return result, hg, ag


def simulate_knockout_stage(knockout_fixtures, group_tables, third_place_table, pair_cache=None, rng=None, bundle=None):
    """Simulate knockout rounds and return winners advancing to each stage.

    `pair_cache` maps (home, away) -> (p_home, p_draw, p_away, hg, ag) so the
    inner loop is a dict lookup + sampling rather than a fresh model inference.
    The hg/ag are the raw regressor scoreline values; the per-match adjustment
    runs in simulate_knockout_match so the sampled result drives a consistent
    scoreline (mirroring coerce_scoreline in the deterministic projection).

    `winners` is keyed by the human-readable label (e.g. "Round of 32 1",
    "Quarterfinal 3", "Final 1") so that `parse_previous_winner_slot` can look
    up the team that won a given previous match when resolving the next
    round's slot.
    """
    fixtures_by_stage = defaultdict(list)
    for fixture in knockout_fixtures:
        stage = str(fixture.get("stage", "")).strip().lower()
        if stage in STAGE_ORDER:
            fixtures_by_stage[stage].append(fixture)

    for stage in fixtures_by_stage:
        fixtures_by_stage[stage].sort(key=lambda row: (row.get("match_datetime_utc", ""), row.get("event_id", "")))

    group_lookup = group_qualifier_lookup(group_tables)
    third_assignments = resolve_third_place_slots(fixtures_by_stage.get("round-of-32", []), third_place_table)
    winners = {}
    stage_participants = {stage: set() for stage in STAGE_ORDER}
    last_winner = ""
    # Per-fixture rows in the same shape as project_knockout so a single sim's
    # bracket can drive the website display when the sim's champion matches the
    # 1000-sim aggregate's highest-odds winner.
    projected = {}

    for stage in sorted(fixtures_by_stage.keys(), key=lambda value: STAGE_ORDER[value]):
        round_rows = []
        for idx, fixture in enumerate(fixtures_by_stage[stage], start=1):
            match_idx_zero = idx - 1
            home_slot, away_slot = extract_competitor_slots(fixture)
            home = resolve_slot(
                home_slot, match_idx_zero, "home_team", group_lookup, third_assignments, third_place_table, winners
            )
            away = resolve_slot(
                away_slot, match_idx_zero, "away_team", group_lookup, third_assignments, third_place_table, winners
            )
            label = f"{STAGE_DISPLAY[stage]} {idx}"
            if not home or not away:
                round_rows.append({
                    "label": label,
                    "stage": stage,
                    "match_date": fixture.get("match_date", ""),
                    "match_datetime_utc": fixture.get("match_datetime_utc", ""),
                    "venue": fixture.get("venue", ""),
                    "home_slot": home_slot,
                    "away_slot": away_slot,
                    "home_team": home or home_slot,
                    "away_team": away or away_slot,
                    "winner": "",
                    "predicted_result": "",
                    "prob_home": 0.0,
                    "prob_draw": 0.0,
                    "prob_away": 0.0,
                    "pred_home_goals": None,
                    "pred_away_goals": None,
                })
                continue

            cache_entry = None
            p_home = p_draw = p_away = 0.0
            raw_hg = raw_ag = 0
            if pair_cache is not None:
                cache_entry = pair_cache.get((home, away))
            if cache_entry is not None:
                p_home, p_draw, p_away, raw_hg, raw_ag = cache_entry
                probs = (p_home, p_draw, p_away)
                result, hg, ag = simulate_knockout_match(probs, raw_hg, raw_ag, allow_draw=False, rng=rng)
            elif bundle is not None:
                prediction = predict_team_match(
                    bundle, home, away, stage, fixture.get("match_datetime_utc", ""), fixture.get("venue", ""), allow_draw=False
                )
                probs = (prediction.get("prob_home", 0.0), prediction.get("prob_draw", 0.0), prediction.get("prob_away", 0.0))
                raw_hg = int(prediction.get("pred_home_goals", 0))
                raw_ag = int(prediction.get("pred_away_goals", 0))
                p_home, p_draw, p_away = probs
                result, hg, ag = simulate_knockout_match(probs, raw_hg, raw_ag, allow_draw=False, rng=rng)
            else:
                continue

            winner = home if result == "H" else away
            winners[label] = winner
            stage_participants[stage].add(home)
            stage_participants[stage].add(away)
            last_winner = winner
            round_rows.append({
                "label": label,
                "stage": stage,
                "match_date": fixture.get("match_date", ""),
                "match_datetime_utc": fixture.get("match_datetime_utc", ""),
                "venue": fixture.get("venue", ""),
                "home_slot": home_slot,
                "away_slot": away_slot,
                "home_team": home,
                "away_team": away,
                "winner": winner,
                "predicted_result": result,
                "prob_home": round(float(p_home), 6),
                "prob_draw": round(float(p_draw), 6),
                "prob_away": round(float(p_away), 6),
                "pred_home_goals": int(hg),
                "pred_away_goals": int(ag),
            })
        projected[knockout_round_key(stage)] = round_rows

    return {
        "round-of-32": set(),
        "round-of-16": set(),
        "quarterfinals": set(),
        "semifinals": set(),
        "final": set(),
        "winner": last_winner,
    }, winners, projected


def run_tournament_simulation(group_fixtures, knockout_fixtures, groups, team_to_group, group_fixture_cache=None, pair_cache=None, rng=None, bundle=None):
    """Run a single tournament simulation. Returns stats dict."""
    try:
        simulated_groups = simulate_group_stage(
            group_fixtures, groups, team_to_group, group_fixture_cache=group_fixture_cache, bundle=bundle
        )
    except Exception as e:
        print(f"Error simulating group stage: {e}")
        return None

    group_tables_for_ranking = []
    for group in sorted(simulated_groups.keys()):
        group_tables_for_ranking.append({
            "group": group,
            "teams": simulated_groups[group]
        })

    third_place_table = rank_third_place_teams(group_tables_for_ranking)

    try:
        stage_results, knockout_winners, knockout_rows = simulate_knockout_stage(
            knockout_fixtures, group_tables_for_ranking, third_place_table, pair_cache=pair_cache, rng=rng, bundle=bundle
        )
    except Exception as e:
        print(f"Error simulating knockout: {e}")
        return None

    stats = {
        "groups": {},
        "knockout_advancement": {
            "round_of_32": set(),
            "round_of_16": set(),
            "quarterfinals": set(),
            "semifinals": set(),
            "finals": set(),
        },
        "champion": stage_results.get("winner", ""),
        "group_tables": group_tables_for_ranking,
        "third_place_table": third_place_table,
        "knockout": knockout_rows,
    }

    for group, group_table in simulated_groups.items():
        stats["groups"][group] = {}
        for position, team_row in enumerate(group_table, start=1):
            team = team_row["team"]
            stats["groups"][group][team] = position

    return stats


def run_simulations(bundle, group_fixtures, knockout_fixtures, groups, team_to_group, num_simulations=1000):
    """Run multiple tournament simulations and aggregate results.

    Pre-computes every possible (home, away) knockout matchup so the inner
    loop is a dict lookup + sampling rather than a full model inference.

    Returns aggregated position/winner probabilities plus the raw per-sim
    group_tables and third_place_table lists so `build_projection` can
    produce sim-aggregated group standings.
    """
    print(f"Running {num_simulations} tournament simulations...")

    group_fixture_cache = precompute_group_fixture_cache(bundle, group_fixtures)
    print(f"  Pre-cached {len(group_fixture_cache)} group fixtures.")

    all_teams = set()
    for teams in groups.values():
        all_teams.update(teams)
    knockout_team_set = set(all_teams)
    for fixture in knockout_fixtures:
        for side in ("home_team", "away_team"):
            team = str(fixture.get(side, "")).strip()
            if team and not is_placeholder_team(team):
                knockout_team_set.add(team)
    pair_cache = precompute_knockout_pair_cache(bundle, sorted(knockout_team_set))
    print(f"  Pre-cached {len(pair_cache)} knockout pair predictions.")

    position_counts = defaultdict(lambda: defaultdict(int))
    winner_counts = defaultdict(int)
    sim_group_tables = []
    sim_third_place = []
    # Per-sim results (group_tables, third_place_table, knockout, champion) keyed
    # by the sim's RNG seed. The `display_sim` key stores the sim selected as the
    # website display (its champion matches the highest-odds winner from the
    # 1000-sim aggregate). Default -1 indicates none chosen yet.
    per_sim_results = []
    display_sim_index = -1

    successful_sims = 0
    base_seed = 20260611

    for sim_num in range(num_simulations):
        if (sim_num + 1) % 100 == 0:
            print(f"  Progress: {sim_num + 1}/{num_simulations} simulations...")

        sim_seed = base_seed + sim_num
        sim_rng = np.random.default_rng(sim_seed)

        stats = run_tournament_simulation(
            group_fixtures, knockout_fixtures, groups, team_to_group,
            group_fixture_cache=group_fixture_cache, pair_cache=pair_cache, rng=sim_rng, bundle=bundle,
        )
        if not stats:
            continue

        successful_sims += 1

        sim_group_tables.append(stats["group_tables"])
        sim_third_place.append(stats["third_place_table"])
        per_sim_results.append({
            "sim_index": sim_num,
            "seed": sim_seed,
            "champion": stats.get("champion", ""),
            "group_tables": stats["group_tables"],
            "third_place_table": stats["third_place_table"],
            "knockout": stats.get("knockout", {}),
        })

        for group, group_stats in stats.get("groups", {}).items():
            for team, position in group_stats.items():
                position_counts[team][f"group_position_{position}"] += 1

        if stats.get("champion"):
            winner_counts[stats["champion"]] += 1

    print(f"  Completed {successful_sims} successful simulations")

    position_probabilities = {}
    for team, positions in position_counts.items():
        position_probabilities[team] = {}
        for position_key, count in positions.items():
            percentage = (count / successful_sims * 100) if successful_sims > 0 else 0
            position_probabilities[team][position_key] = round(percentage, 2)

    winner_probabilities = {}
    for team, count in winner_counts.items():
        percentage = (count / successful_sims * 100) if successful_sims > 0 else 0
        winner_probabilities[team] = round(percentage, 2)

    # Pick the sim whose champion matches the highest-odds winner from the
    # 1000-sim aggregate. If no sim in the 1000 produces that champion (rare
    # when the top probability is low), run additional sims with new seeds
    # until one matches, with a max-attempts cap.
    target_champion = ""
    top_pct = -1.0
    for team, pct in winner_probabilities.items():
        if pct > top_pct:
            top_pct = pct
            target_champion = team

    display_sim = None
    if target_champion:
        for entry in per_sim_results:
            if entry["champion"] == target_champion:
                display_sim = entry
                display_sim_index = entry["sim_index"]
                break

    if display_sim is None and target_champion:
        print(f"  No sim in {num_simulations} produced highest-odds winner '{target_champion}'. Running extra sims...")
        max_extra_attempts = 500
        for attempt in range(max_extra_attempts):
            extra_seed = base_seed + num_simulations + attempt
            extra_rng = np.random.default_rng(extra_seed)
            stats = run_tournament_simulation(
                group_fixtures, knockout_fixtures, groups, team_to_group,
                group_fixture_cache=group_fixture_cache, pair_cache=pair_cache, rng=extra_rng, bundle=bundle,
            )
            if not stats:
                continue
            if stats.get("champion") == target_champion:
                display_sim = {
                    "sim_index": num_simulations + attempt,
                    "seed": extra_seed,
                    "champion": stats.get("champion", ""),
                    "group_tables": stats["group_tables"],
                    "third_place_table": stats["third_place_table"],
                    "knockout": stats.get("knockout", {}),
                }
                display_sim_index = display_sim["sim_index"]
                print(f"    Match found on attempt {attempt + 1} (seed {extra_seed}).")
                break
        if display_sim is None:
            print(f"    No match after {max_extra_attempts} extra attempts. Falling back to the 1000-sim aggregate for display.")

    return {
        "simulations_run": successful_sims,
        "position_probabilities": position_probabilities,
        "winner_probabilities": winner_probabilities,
        "sim_group_tables": sim_group_tables,
        "sim_third_place": sim_third_place,
        "per_sim_results": per_sim_results,
        "display_sim": display_sim,
        "display_sim_index": display_sim_index,
        "target_display_champion": target_champion,
    }


def build_projection(args):
    bundle = ensure_model_bundle(args.rebuild_national_model, args.api_token)
    fixtures = fetch_world_cup_fixtures(args.start_date, args.end_date)
    if not fixtures:
        raise ValueError("No FIFA World Cup fixtures returned by ESPN.")

    group_fixtures = [row for row in fixtures if row.get("stage") == "group-stage"]
    knockout_fixtures = [row for row in fixtures if row.get("stage") in STAGE_ORDER]
    groups, team_to_group = infer_groups(group_fixtures)

    # Run 1000 simulations FIRST so we can use sim-aggregated qualifiers for
    # the deterministic knockout projection. Group-stage + third-place
    # AGGREGATE output drives the winner/position probabilities (used by the
    # website summary cards). The DISPLAYED group tables, third-place table,
    # knockout bracket, and champion come from a SINGLE sim whose champion
    # matches the highest-odds winner from the 1000-sim aggregate — so the
    # website's "champion" card matches the displayed bracket end-to-end and
    # the third-place Pts/GD/GF reflect the actual group results, not sim
    # averages. Live WC results (if any) are applied deterministically inside
    # each sim's group stage.
    print("Generating tournament simulations...")
    simulation_stats = run_simulations(bundle, group_fixtures, knockout_fixtures, groups, team_to_group)

    real_fixture_counts = _count_real_fixtures_per_team(group_fixtures)
    aggregated_group_tables = aggregate_sim_group_tables(
        simulation_stats["sim_group_tables"],
        simulation_stats["simulations_run"],
        real_fixture_counts,
    )
    aggregated_third_place = aggregate_sim_third_place(
        simulation_stats["sim_third_place"],
        simulation_stats["simulations_run"],
    )

    display_sim = simulation_stats.get("display_sim")
    if display_sim is not None:
        # Use the single sim's outputs for the displayed group tables, third
        # place, and knockout bracket. The sim's champion == highest-odds
        # winner by construction.
        display_group_tables = display_sim["group_tables"]
        display_third_place = display_sim["third_place_table"]
        display_knockout = display_sim["knockout"]
        display_champion = display_sim["champion"]
    else:
        # No sim produced the highest-odds winner (rare, only when the top
        # probability is very low). Fall back to the 1000-sim aggregate for
        # the displayed groups/third place + a deterministic knockout fed
        # with the aggregate qualifiers.
        display_group_tables = aggregated_group_tables
        display_third_place = aggregated_third_place
        display_knockout, _winners = project_knockout(
            bundle, knockout_fixtures, aggregated_group_tables, aggregated_third_place
        )
        final_rows = display_knockout.get("final", [])
        display_champion = final_rows[0]["winner"] if final_rows else ""

    # Deterministic per-fixture predictions (for the website display) — these
    # include actual results for played fixtures (source: "real") and
    # deterministic predictions for unplayed ones.
    _, group_fixture_predictions = project_groups(bundle, group_fixtures, groups, team_to_group)

    payload = {
        "ok": True,
        "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "source": "ESPN scoreboard API + national-team predictor",
        "competition": WORLD_CUP_COMPETITION,
        "year": args.year,
        "rules_summary": [
            "Group ranking uses FIFA order available from match data: points, goal difference, goals scored, then head-to-head among tied teams.",
            "The top two teams from each of the 12 groups qualify for the Round of 32.",
            "The eight best third-place teams qualify using points, goal difference, goals scored, then deterministic fallback.",
            "Round-of-32 third-place slots follow ESPN/FIFA published candidate group constraints for the 2026 bracket.",
            "Knockout rounds are projected without draws; tied model outcomes advance the side with the higher non-draw probability.",
            "Winner/position probabilities are aggregated across 1000 tournament simulations (mode position + sim-averaged W/D/L/Pts/GD/GF/GA).",
            "The displayed group tables, third-place table, knockout bracket, and champion come from a SINGLE sim whose champion matches the highest-odds winner from the 1000-sim aggregate, so the bracket ends at the displayed champion and the third-place Pts/GD/GF reflect the actual group results (not sim averages). If no sim in the first 1000 produced that champion, additional sims are run (max 500 extra attempts) until one matches.",
        ],
        "groups_inferred_from_schedule": True,
        # Primary fields rendered by the website (world_cup.js) come from a
        # SINGLE sim whose champion matches the highest-odds winner from the
        # 1000-sim aggregate. Internally consistent end-to-end: third-place
        # Pts/GD/GF reflect the actual group results, and the displayed bracket
        # leads to the displayed champion.
        "group_tables": display_group_tables,
        "third_place_table": display_third_place,
        "knockout": display_knockout,
        "champion": display_champion,
        "display_sim_index": simulation_stats.get("display_sim_index", -1),
        "display_sim_seed": display_sim.get("seed") if display_sim else None,
        # 1000-sim aggregate (used for winner/position probability cards).
        "aggregate_group_tables": aggregated_group_tables,
        "aggregate_third_place_table": aggregated_third_place,
        # Deterministic per-fixture predictions (played fixtures have source:
        # "real" with actual goals; unplayed have source: "predicted" with the
        # model's point estimate).
        "group_fixtures": [
            item
            for group in GROUP_LABELS
            for item in group_fixture_predictions.get(group, [])
        ],
        "simulations": simulation_stats,
    }
    return payload


def main():
    args = parse_cli_args()
    projection = build_projection(args)
    os.makedirs(PREDICTIONS_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as file:
        json.dump(projection, file, indent=2, ensure_ascii=False)
    print(f"World Cup projection saved: {OUT_FILE}")
    print(f"Groups projected: {len(projection.get('group_tables', []))}")
    print(f"Champion projection: {projection.get('champion') or 'N/A'}")


if __name__ == "__main__":
    main()
