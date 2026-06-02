import argparse
import json
import os
import random
import re
from collections import defaultdict, deque
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

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


def actual_result_from_fixture(fixture):
    ftr = str(fixture.get("FTR", "")).strip().upper()
    hg = pd.to_numeric(fixture.get("FTHG"), errors="coerce")
    ag = pd.to_numeric(fixture.get("FTAG"), errors="coerce")
    if ftr in {"H", "D", "A"} and pd.notna(hg) and pd.notna(ag):
        return ftr, int(hg), int(ag)
    return None


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


def simulate_match_result(prediction):
    """Simulate a match result based on probabilities. Returns 'H', 'D', or 'A'."""
    outcomes = ['H', 'D', 'A']
    probabilities = [
        float(prediction.get("prob_home", 0.0)),
        float(prediction.get("prob_draw", 0.0)),
        float(prediction.get("prob_away", 0.0)),
    ]
    # Normalize probabilities in case they don't sum to 1.0
    total = sum(probabilities)
    if total > 0:
        probabilities = [p / total for p in probabilities]
    else:
        probabilities = [0.33, 0.34, 0.33]
    return random.choices(outcomes, weights=probabilities, k=1)[0]


def simulate_group_stage(group_fixture_predictions, groups, team_to_group):
    """Simulate group stage and return final group tables. Returns dict of group -> list of teams ranked."""
    tables = {}
    for group in groups:
        tables[group] = {team: empty_table_row(team) for team in groups[group]}
    
    # Simulate each fixture
    for fixture in group_fixture_predictions:
        home = fixture["home_team"]
        away = fixture["away_team"]
        group = team_to_group.get(home) or team_to_group.get(away)
        if not group:
            continue
        
        # Simulate the match
        result = simulate_match_result(fixture)
        hg = int(fixture.get("pred_home_goals", 0))
        ag = int(fixture.get("pred_away_goals", 0))
        
        # Update tables
        tables[group][home]["P"] += 1
        tables[group][away]["P"] += 1
        tables[group][home]["GF"] += hg
        tables[group][home]["GA"] += ag
        tables[group][away]["GF"] += ag
        tables[group][away]["GA"] += hg
        
        if result == "H":
            tables[group][home]["W"] += 1
            tables[group][home]["Pts"] += 3
            tables[group][away]["L"] += 1
        elif result == "A":
            tables[group][away]["W"] += 1
            tables[group][away]["Pts"] += 3
            tables[group][home]["L"] += 1
        else:  # Draw
            tables[group][home]["D"] += 1
            tables[group][away]["D"] += 1
            tables[group][home]["Pts"] += 1
            tables[group][away]["Pts"] += 1
    
    # Rank teams in each group
    final_tables = {}
    for group, group_tables in tables.items():
        team_rows = list(group_tables.values())
        ranked = rank_group_rows(team_rows, [])  # Empty fixture list for h2h (not needed for simulation)
        final_tables[group] = ranked
    
    return final_tables


def simulate_knockout_match(match_prediction, allow_draw=False):
    """Simulate a knockout match. Return winner ('H' or 'A') and goals."""
    result = simulate_match_result(match_prediction)
    
    # If draw in knockout, resolve it
    if result == "D" and not allow_draw:
        prob_home = float(match_prediction.get("prob_home", 0.0))
        prob_away = float(match_prediction.get("prob_away", 0.0))
        if prob_home > prob_away:
            result = "H"
        elif prob_away > prob_home:
            result = "A"
        else:
            result = "H"  # Default to home on tie
    
    hg = int(match_prediction.get("pred_home_goals", 0))
    ag = int(match_prediction.get("pred_away_goals", 0))
    
    # Ensure goals differ for knockout
    if result == "H" and hg <= ag:
        hg = ag + 1
    elif result == "A" and ag <= hg:
        ag = hg + 1
    
    return result, hg, ag


def simulate_knockout_stage(bundle, knockout_fixtures, group_tables, third_place_table):
    """Simulate knockout rounds and return winners advancing to each stage."""
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
    
    for stage in sorted(fixtures_by_stage.keys(), key=lambda value: STAGE_ORDER[value]):
        for idx, fixture in enumerate(fixtures_by_stage[stage], start=1):
            match_idx_zero = idx - 1
            home_slot, away_slot = extract_competitor_slots(fixture)
            home = resolve_slot(
                home_slot, match_idx_zero, "home_team", group_lookup, third_assignments, third_place_table, winners
            )
            away = resolve_slot(
                away_slot, match_idx_zero, "away_team", group_lookup, third_assignments, third_place_table, winners
            )
            
            if not home or not away:
                continue
            
            # Predict the match
            prediction = predict_team_match(
                bundle, home, away, stage, fixture.get("match_datetime_utc", ""), fixture.get("venue", ""), allow_draw=False
            )
            
            # Simulate result
            result, hg, ag = simulate_knockout_match(prediction, allow_draw=False)
            winner = home if result == "H" else away
            winners[f"{stage}_{idx}"] = winner
            stage_participants[stage].add(home)
            stage_participants[stage].add(away)
    
    # Return only the winners at each stage
    return {
        "round-of-32": set(),
        "round-of-16": set(),
        "quarterfinals": set(),
        "semifinals": set(),
        "final": set(),
        "winner": list(winners.values())[-1] if winners else ""
    }, winners


def run_tournament_simulation(bundle, group_fixtures, knockout_fixtures, groups, team_to_group):
    """Run a single tournament simulation. Returns stats dict."""
    # Simulate group stage
    try:
        simulated_groups = simulate_group_stage(group_fixtures, groups, team_to_group)
    except Exception as e:
        print(f"Error simulating group stage: {e}")
        return None
    
    # Convert simulated_groups to the format expected by rank_third_place_teams
    # Format: [{group: str, teams: [team_rows]}, ...]
    group_tables_for_ranking = []
    for group in sorted(simulated_groups.keys()):
        group_tables_for_ranking.append({
            "group": group,
            "teams": simulated_groups[group]
        })
    
    # Get properly ranked third-place teams with "group" and "qualified" fields
    third_place_table = rank_third_place_teams(group_tables_for_ranking)
    
    # Simulate knockout
    try:
        stage_results, knockout_winners = simulate_knockout_stage(
            bundle, knockout_fixtures, group_tables_for_ranking, third_place_table
        )
    except Exception as e:
        print(f"Error simulating knockout: {e}")
        return None
    
    # Compile stats for this simulation
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
    }
    
    # Track group positions
    for group, group_table in simulated_groups.items():
        stats["groups"][group] = {}
        for position, team_row in enumerate(group_table, start=1):
            team = team_row["team"]
            stats["groups"][group][team] = position
    
    return stats


def run_simulations(bundle, group_fixtures, knockout_fixtures, groups, team_to_group, num_simulations=500):
    """Run multiple tournament simulations and aggregate results."""
    print(f"Running {num_simulations} tournament simulations...")
    
    # Aggregation structures
    position_counts = defaultdict(lambda: defaultdict(int))  # team -> position -> count
    stage_counts = defaultdict(lambda: defaultdict(int))  # team -> stage -> count
    winner_counts = defaultdict(int)  # team -> count
    
    successful_sims = 0
    
    for sim_num in range(num_simulations):
        if (sim_num + 1) % 100 == 0:
            print(f"  Progress: {sim_num + 1}/{num_simulations} simulations...")
        
        stats = run_tournament_simulation(bundle, group_fixtures, knockout_fixtures, groups, team_to_group)
        if not stats:
            continue
        
        successful_sims += 1
        
        # Count group positions
        for group, group_stats in stats.get("groups", {}).items():
            for team, position in group_stats.items():
                position_counts[team][f"group_position_{position}"] += 1
        
        # Count champion
        if stats.get("champion"):
            winner_counts[stats["champion"]] += 1
    
    print(f"  Completed {successful_sims} successful simulations")
    
    # Convert to percentages
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
    
    return {
        "simulations_run": successful_sims,
        "position_probabilities": position_probabilities,
        "winner_probabilities": winner_probabilities,
    }


def build_projection(args):
    bundle = ensure_model_bundle(args.rebuild_national_model, args.api_token)
    fixtures = fetch_world_cup_fixtures(args.start_date, args.end_date)
    if not fixtures:
        raise ValueError("No FIFA World Cup fixtures returned by ESPN.")

    group_fixtures = [row for row in fixtures if row.get("stage") == "group-stage"]
    knockout_fixtures = [row for row in fixtures if row.get("stage") in STAGE_ORDER]
    groups, team_to_group = infer_groups(group_fixtures)
    group_tables, group_fixture_predictions = project_groups(bundle, group_fixtures, groups, team_to_group)
    third_place_table = rank_third_place_teams(group_tables)
    knockout, winners = project_knockout(bundle, knockout_fixtures, group_tables, third_place_table)

    final_rows = knockout.get("final", [])
    champion = final_rows[0]["winner"] if final_rows else ""
    
    # Run 500 simulations to calculate probabilities
    print("Generating tournament simulations...")
    simulation_stats = run_simulations(bundle, group_fixtures, knockout_fixtures, groups, team_to_group, num_simulations=500)
    
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
        ],
        "groups_inferred_from_schedule": True,
        "group_tables": group_tables,
        "third_place_table": third_place_table,
        "group_fixtures": [
            item
            for group in GROUP_LABELS
            for item in group_fixture_predictions.get(group, [])
        ],
        "knockout": knockout,
        "champion": champion,
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
