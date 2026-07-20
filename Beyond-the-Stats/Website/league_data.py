"""Unified ``/api/league-data`` response builder.

Every competition returns the same top-level shape. Competition-specific
rules (MLS conferences, Liga MX tournaments, cup knockouts) are described in
``format`` and surfaced in ``predicted.groups`` / ``bracket`` / ``real.standings``.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import config
from competition_rules import (
    build_structured_standings_groups,
    competition_format_spec,
    resolve_competition_query,
    standings_layout_for,
    STANDINGS_LAYOUT_MLS,
)
from knockout import (
    _build_cup_knockout_payload,
    _build_knockout_framework,
    _enrich_league_data_cup_fields,
    _gather_competition_cup_matches,
)
from predictions import (
    _build_mls_winners_odds_bundle,
    _build_winner_probability_payload,
    _load_json_payload,
    _load_projected_competition_table,
    _load_upcoming_rows,
)
from standings import (
    _build_fallback_standings,
    _compute_standings_from_history,
    _dedupe_standings_groups,
    _load_league_teams,
    _normalize_team_name,
    _UEFA_COMPETITIONS,
)


_MLS_EAST_TEAMS = {
    "Atlanta Utd", "CF Montreal", "Charlotte", "Chicago Fire",
    "Columbus Crew", "DC United", "FC Cincinnati", "Inter Miami",
    "Nashville SC", "New England Revolution", "New York City",
    "New York Red Bulls", "Orlando City", "Philadelphia Union", "Toronto FC",
}
_MLS_WEST_TEAMS = {
    "Austin FC", "Colorado Rapids", "FC Dallas", "Houston Dynamo",
    "Los Angeles Galaxy", "Los Angeles FC", "Minnesota United",
    "Portland Timbers", "Real Salt Lake", "San Diego FC",
    "San Jose Earthquakes", "Seattle Sounders", "Sporting Kansas City",
    "St. Louis City", "Vancouver Whitecaps",
}


def _slugify_competition(competition):
    out = (competition or "").strip().lower()
    out = out.replace("/", "_").replace(" ", "_").replace("-", "_")
    out = out.replace(".", "").replace(",", "").replace("'", "")
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_") or "unknown"


def _league_data_cache_path(comp_name):
    slug = _slugify_competition(comp_name)
    return os.path.join(config.LEAGUE_DATA_DIR, f"{slug}.json")


def _load_league_data_from_cache(comp_name):
    path = _league_data_cache_path(comp_name)
    if os.path.exists(path):
        try:
            age = datetime.now(timezone.utc).timestamp() - os.path.getmtime(path)
            # Short TTL so preseason roster / PATH B fixes show up quickly.
            if age > float(getattr(config, "CACHE_TTL_LONG", 600)):
                return None
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if _mls_league_data_cache_missing_cup(comp_name, payload):
                return None
            return payload
        except Exception:
            pass
    return None


def _mls_league_data_cache_missing_cup(comp_name: str, payload: dict) -> bool:
    """Rebuild MLS cache entries that omit Cup bracket / winner odds."""
    if not str(comp_name or "").startswith("United States/MLS"):
        return False
    if not isinstance(payload, dict):
        return True
    winners = payload.get("mls_winners_odds") or {}
    cup_odds = winners.get("mls_cup") if isinstance(winners, dict) else None
    has_cup_odds = isinstance(cup_odds, dict) and bool(
        cup_odds.get("winner_probabilities") or cup_odds.get("winners_odds") or cup_odds.get("champion")
    )
    projected = ((payload.get("bracket") or {}).get("projected") or {})
    has_real_bracket = isinstance(projected, dict) and any(
        key in projected
        for key in ("mls_cup", "wildcard", "round_one", "eastern_seeds", "mls_cup_winner_probabilities")
    )
    bracket_path = getattr(config, "MLS_PROJECTED_BRACKET_FILE", "")
    if bracket_path and os.path.exists(bracket_path):
        return not (has_cup_odds and has_real_bracket)
    # Even without the pipeline file, keep a stable mls_cup slot in winners_odds.
    return "mls_cup" not in (winners or {})


def _write_league_data_cache(comp_name, payload):
    path = _league_data_cache_path(comp_name)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    except Exception:
        pass


MLS_PREDICTED_GROUP_NAMES = (
    ("Supporters Shield", "United States/MLS - Supporters Shield Table"),
    ("Eastern Conference", "United States/MLS - Eastern Conference"),
    ("Western Conference", "United States/MLS - Western Conference"),
)


def _build_position_odds(comp_table: list[dict]) -> dict:
    simple: dict[str, list] = {}
    detailed: list[dict] = []
    for row in comp_table:
        team = str(row.get("team", "")).strip()
        raw = row.get("position_odds")
        if not team or not raw or not isinstance(raw, dict):
            continue
        entry = {"team": team, "odds": {}}
        for pos_str, pct in raw.items():
            pct_f = float(pct) if pct is not None else 0.0
            entry["odds"][str(pos_str)] = pct_f
            simple.setdefault(str(pos_str), []).append({"team": team, "pct": pct_f})
        detailed.append(entry)
    for pos_rows in simple.values():
        pos_rows.sort(key=lambda item: item["pct"], reverse=True)
    simple = dict(
        sorted(simple.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 999)
    )
    return {
        "simple": simple,
        "detailed": detailed,
        "detailed_same_as_simple": _position_odds_views_equivalent(simple, detailed),
    }


def _position_odds_views_equivalent(simple: dict, detailed: list[dict]) -> bool:
    if not simple and not detailed:
        return True
    if not simple or not detailed:
        return False
    rebuilt: dict[str, list] = {}
    for entry in detailed:
        team = entry.get("team")
        odds = entry.get("odds") or {}
        if not team:
            continue
        for pos, pct in odds.items():
            rebuilt.setdefault(str(pos), []).append({"team": team, "pct": float(pct)})
    for pos_rows in rebuilt.values():
        pos_rows.sort(key=lambda item: item["pct"], reverse=True)
    return rebuilt == simple


def _rows_to_group_entries(rows: list[dict]) -> list[dict]:
    entries = []
    for row in rows:
        team = str(row.get("team", "")).strip()
        if not team:
            continue
        pos = row.get("position") or row.get("rank") or 0
        entries.append({
            "team": team,
            "rank": pos,
            "position": pos,
            "P": row.get("P", 0),
            "W": row.get("W", 0),
            "D": row.get("D", 0),
            "L": row.get("L", 0),
            "GF": row.get("GF", 0),
            "GA": row.get("GA", 0),
            "GD": row.get("GD", 0),
            "Pts": row.get("Pts", 0),
        })
    entries.sort(key=lambda item: item.get("rank") or item.get("position") or 999)
    return entries


def _load_mls_predicted_groups() -> list[dict]:
    groups = []
    for group_name, comp_key in MLS_PREDICTED_GROUP_NAMES:
        rows = _load_projected_competition_table(comp_key)
        if rows:
            groups.append({"name": group_name, "entries": _rows_to_group_entries(rows)})
    return groups


def _load_predicted_groups(comp_name: str, comp_table: list[dict]) -> list[dict] | None:
    base_comp, mls_view = resolve_competition_query(comp_name)
    layout = standings_layout_for(comp_name)

    if layout == STANDINGS_LAYOUT_MLS:
        if mls_view:
            view_map = {
                "east": "Eastern Conference",
                "west": "Western Conference",
                "shield": "Supporters Shield",
            }
            label = view_map.get(mls_view)
            if label and comp_table:
                return [{"name": label, "entries": _rows_to_group_entries(comp_table)}]
            real = _load_real_standings(comp_name)
            if real and real.get("groups"):
                target = label
                if target:
                    filtered = [g for g in real["groups"] if g.get("name") == target]
                    if filtered:
                        return filtered
            return None
        groups = _load_mls_predicted_groups()
        if groups:
            return groups
        real = _load_real_standings(comp_name)
        if real and real.get("groups"):
            return real["groups"]
        league_teams = _load_league_teams()
        roster = league_teams.get(base_comp) or league_teams.get(comp_name)
        if roster:
            return build_structured_standings_groups(comp_name, roster)
        return None

    fmt = competition_format_spec(comp_name)

    # For cup competitions with group stages (like Leagues Cup), try ESPN standings first
    if fmt and fmt.get("format") == "group_stage_then_knockout" and comp_table:
        from standings import _get_or_fetch_standings
        espn_standings = _get_or_fetch_standings(comp_name, computed=False)
        if espn_standings and isinstance(espn_standings.get("groups"), list) and len(espn_standings["groups"]) > 1:
            team_map = {}
            for grp in espn_standings["groups"]:
                for entry in grp.get("entries", []):
                    team_map[str(entry.get("team", "")).strip()] = grp.get("name", "Overall")
            if team_map:
                groups_dict = {}
                for row in _rows_to_group_entries(comp_table):
                    t = str(row.get("team", "")).strip()
                    gname = team_map.get(t, "Overall")
                    groups_dict.setdefault(gname, {"name": gname, "entries": []})["entries"].append(row)
                if groups_dict:
                    result = list(groups_dict.values())
                    for g in result:
                        g["entries"].sort(key=lambda e: e.get("rank") or e.get("position") or 999)
                    return result

    # Fallback: infer groups from actual fixture matchups (who plays whom).
    if fmt and fmt.get("format") == "group_stage_then_knockout" and comp_table:
        groups = _infer_groups_from_fixtures(comp_name, comp_table)
        if groups:
            return groups

    real = _load_real_standings(comp_name)
    if real and isinstance(real.get("groups"), list) and real["groups"]:
        if comp_table:
            group_name = str(real["groups"][0].get("name") or "Overall")
            return [{"name": group_name, "entries": _rows_to_group_entries(comp_table)}]
        return real["groups"]

    if comp_table:
        return [{"name": "Overall", "entries": _rows_to_group_entries(comp_table)}]

    league_teams = _load_league_teams()
    roster = league_teams.get(comp_name) or league_teams.get(base_comp)
    if roster:
        return build_structured_standings_groups(comp_name, roster)
    return None


def _load_real_standings(comp_name: str) -> dict | None:
    """Active-season real table, or a zeroed current roster in preseason.

    Prior-season history is excluded via ``filter_games_to_active_season``.
    When the active window has no games yet (typical Jul–mid-Aug), return the
    roster placeholder instead of a stale end-of-season table.

    Deduping / roster alignment happen inside standings helpers so real tables
    share the same football-data canonical team set as predicted tables.
    """
    result = _compute_standings_from_history(comp_name)
    if result and result.get("groups"):
        return result
    fallback = _build_fallback_standings(comp_name)
    if fallback:
        return fallback
    return None


def _infer_groups_from_fixtures(comp_name: str, comp_table: list[dict]) -> list[dict] | None:
    """Infer groups from the actual fixture matchups for group-stage cups.

    For competitions like Leagues Cup, each team plays 3 group-stage games.
    By building a graph from the fixtures (edges = matchups), connected
    components directly correspond to groups.
    """
    import csv

    base, _view = resolve_competition_query(comp_name)
    candidates = {comp_name, base}
    csv_path = config.CUP_UPCOMING_FILE
    if not os.path.exists(csv_path):
        return None
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fixtures = [r for r in reader if str(r.get("competition", "")).strip() in candidates]
    except Exception:
        return None
    if not fixtures:
        return None

    adj = {}
    for r in fixtures:
        h = str(r.get("home_team", "")).strip().lower()
        a = str(r.get("away_team", "")).strip().lower()
        if h and a:
            adj.setdefault(h, []).append(a)
            adj.setdefault(a, []).append(h)

    visited = set()
    groups_raw = []
    for team in adj:
        if team in visited:
            continue
        stack = [team]
        group = []
        while stack:
            t = stack.pop()
            if t in visited:
                continue
            visited.add(t)
            group.append(t)
            for neighbor in adj.get(t, []):
                if neighbor not in visited:
                    stack.append(neighbor)
        if group:
            groups_raw.append(sorted(group))

    if len(groups_raw) < 2:
        return None

    table_by_team = {}
    table_lower = {}
    for row in comp_table:
        t = str(row.get("team", "")).strip()
        if t:
            table_by_team[t] = row
            table_lower[t.lower()] = row

    labels = "ABCDEFGHIJKL"
    result = []
    for idx, grp_teams in enumerate(groups_raw):
        label = f"Group {labels[idx]}" if idx < len(labels) else f"Group {idx + 1}"
        entries = []
        for lt in grp_teams:
            row = table_by_team.get(lt, table_lower.get(lt))
            if row:
                entries.append(row)
        entries.sort(key=lambda e: e.get("rank") or e.get("position") or 999)
        result.append({"name": label, "entries": entries})

    return result if result else None


def _load_fixtures(comp_name: str) -> list[dict]:
    base_comp, _view = resolve_competition_query(comp_name)
    fixture_comps = {comp_name, base_comp}
    if base_comp == "United States/MLS":
        fixture_comps.add("United States/MLS")
    if base_comp == config.LIGA_MX_COMPETITION:
        fixture_comps.add(config.LIGA_MX_COMPETITION)
    for csv_path in (
        config.GLOBAL_UPCOMING_FILE,
        config.MLS_UPCOMING_FILE,
        config.EXTRA_UPCOMING_FILE,
        config.CUP_UPCOMING_FILE,
    ):
        try:
            rows, _, _ = _load_upcoming_rows(csv_path, date_range="all")
        except Exception:
            continue
        matches = [r for r in rows if r.get("competition") in fixture_comps]
        if matches:
            return matches
    return []


def _generate_mls_playoff_bracket():
    """Generate MLS playoff bracket from current projected conference positions.

    First round is 1v8, 2v7, 3v6, 4v5 per conference (static format).
    Later rounds use TBD placeholders. Topology (feeds_to) is included.
    """
    east_rows = _load_projected_competition_table("United States/MLS - Eastern Conference")
    west_rows = _load_projected_competition_table("United States/MLS - Western Conference")
    east_rows = [r for r in (east_rows or []) if r.get("team")]
    west_rows = [r for r in (west_rows or []) if r.get("team")]
    east_rows.sort(key=lambda r: int(r.get("position", 999)))
    west_rows.sort(key=lambda r: int(r.get("position", 999)))

    first_round = []
    for conf_name, conf_rows, offset in [
        ("East", east_rows, 0),
        ("West", west_rows, 4),
    ]:
        if len(conf_rows) < 8:
            continue
        pairings = [(0, 7), (1, 6), (2, 5), (3, 4)]
        for slot_idx, (hi, lo) in enumerate(pairings):
            home = conf_rows[hi].get("team", "")
            away = conf_rows[lo].get("team", "")
            if home and away:
                first_round.append({
                    "slot": offset + slot_idx + 1,
                    "home_team": home,
                    "away_team": away,
                    "home_seed": hi + 1,
                    "away_seed": lo + 1,
                    "conference": conf_name,
                })

    if not first_round:
        return {"knockout_rounds": []}

    rounds = [
        {
            "name": "First Round",
            "order": 1,
            "matches_count": len(first_round),
            "matchups": [
                {"slot": i + 1, "feeds_to": {
                    "round": "Conference Semifinals",
                    "slot": ((i) // 2) + 1,
                }}
                for i in range(len(first_round))
            ],
        },
        {
            "name": "Conference Semifinals",
            "order": 2,
            "matches_count": len(first_round) // 2,
            "matchups": [
                {"slot": i + 1, "feeds_to": {
                    "round": "Conference Finals",
                    "slot": (i // 2) + 1,
                }}
                for i in range(len(first_round) // 2)
            ],
        },
        {
            "name": "Conference Finals",
            "order": 3,
            "matches_count": 2,
            "matchups": [
                {"slot": 1, "feeds_to": {"round": "MLS Cup", "slot": 1}},
                {"slot": 2, "feeds_to": {"round": "MLS Cup", "slot": 1}},
            ],
        },
        {"name": "MLS Cup", "order": 4, "matches_count": 1},
    ]

    return {
        "rounds": [
            {"name": "First Round", "matches": first_round},
            {"name": "Conference Semifinals", "matches": [
                {"slot": i + 1, "home_team": "TBD", "away_team": "TBD"}
                for i in range(len(first_round) // 2)
            ]},
            {"name": "Conference Finals", "matches": [
                {"slot": 1, "home_team": "TBD", "away_team": "TBD"},
                {"slot": 2, "home_team": "TBD", "away_team": "TBD"},
            ]},
            {"name": "MLS Cup", "matches": [
                {"slot": 1, "home_team": "TBD", "away_team": "TBD"},
            ]},
        ],
        "knockout_rounds": rounds,
    }


def _load_mls_projected_bracket() -> dict | None:
    """Load the pipeline MLS Cup / playoff bracket JSON when present."""
    data = _load_json_payload(config.MLS_PROJECTED_BRACKET_FILE)
    return data if isinstance(data, dict) and data else None


def _mls_bracket_has_playoff_structure(bracket: dict | None) -> bool:
    if not isinstance(bracket, dict):
        return False
    return any(
        key in bracket
        for key in ("mls_cup", "wildcard", "round_one", "eastern_seeds", "mls_cup_winner_probabilities")
    )


def _build_bracket_section(comp_name: str) -> dict:
    bracket = {
        "projected": None,
        "knockout": None,
        "odds_knockout": None,
        "real_knockout": None,
        "knockout_rounds": None,
    }
    base_comp, _view = resolve_competition_query(comp_name)

    if base_comp == "United States/MLS" or comp_name.startswith("United States/MLS"):
        # Prefer the full simulated playoff bracket from Project_League_Table.
        projected = _load_mls_projected_bracket()
        stub = _generate_mls_playoff_bracket()
        if _mls_bracket_has_playoff_structure(projected):
            bracket["projected"] = projected
            # Keep round topology for clients that only understand knockout_rounds.
            bracket["knockout_rounds"] = stub.get("knockout_rounds")
        elif stub.get("rounds"):
            bracket["projected"] = stub
            bracket["knockout_rounds"] = stub.get("knockout_rounds")
        return bracket

    cup_fmt = config._CUP_FORMATS.get(base_comp) or config._CUP_FORMATS.get(comp_name)
    is_cup = bool(cup_fmt) or base_comp in _UEFA_COMPETITIONS or comp_name in _UEFA_COMPETITIONS
    if not is_cup:
        return bracket

    ko_framework = _build_knockout_framework(base_comp) or _build_knockout_framework(comp_name)
    if ko_framework:
        bracket["knockout_rounds"] = ko_framework

    projected_brackets = _load_json_payload(config.CUP_PROJECTED_BRACKET_FILE)
    if isinstance(projected_brackets, dict):
        comps = projected_brackets.get("competitions", projected_brackets)
        if isinstance(comps, dict):
            entry = comps.get(comp_name) or comps.get(base_comp)
            if entry:
                bracket["projected"] = entry

    matches = _gather_competition_cup_matches(comp_name)
    if matches:
        knockout, odds_knockout, real_knockout = _build_cup_knockout_payload(matches, comp_name)
        bracket["knockout"] = knockout
        bracket["odds_knockout"] = odds_knockout
        bracket["real_knockout"] = real_knockout
    return bracket


def _roster_predicted_table(comp_name: str) -> tuple[list[dict], list[dict]]:
    """Zeroed preseason table from the best available current roster.

    Prefers ``current_season_teams.json`` (updated for the active season) over
    the broader ``league_teams.json`` historical roster when available.
    """
    base_comp, _ = resolve_competition_query(comp_name)
    roster = None
    # Prefer current-season roster when present (preseason 26-27 lists, etc.)
    try:
        if os.path.exists(config.CURRENT_SEASON_TEAMS_FILE):
            with open(config.CURRENT_SEASON_TEAMS_FILE, "r", encoding="utf-8") as fh:
                current = json.load(fh)
            if isinstance(current, dict):
                roster = current.get(comp_name) or current.get(base_comp)
    except Exception:
        roster = None
    if not roster:
        league_teams = _load_league_teams()
        roster = league_teams.get(comp_name) or league_teams.get(base_comp)
    if not roster:
        return [], []
    # Map ESPN long names → football-data canonicals and drop blanks/dupes.
    seen = set()
    canonical_roster = []
    for team in roster:
        canon = _normalize_team_name(str(team).strip(), base_comp)
        if not canon:
            continue
        key = canon.lower()
        if key in seen:
            continue
        seen.add(key)
        canonical_roster.append(canon)
    if not canonical_roster:
        return [], []
    predicted_table = []
    winners_odds = []
    for pos, team in enumerate(sorted(canonical_roster), start=1):
        predicted_table.append({
            "position": pos,
            "team": team,
            "P": 0, "W": 0, "D": 0, "L": 0,
            "GF": 0, "GA": 0, "GD": 0, "Pts": 0,
            "PlayedReal": 0, "PlayedPred": 0,
            "win_league_pct": 0.0, "top4_pct": 0.0, "bottom3_pct": 0.0,
            "most_likely_position": 0, "most_likely_position_pct": 0.0,
            "position_odds": {},
            "sim_runs": 0,
        })
        winners_odds.append({
            "team": team,
            "win_league_pct": 0.0,
            "top4_pct": 0.0,
            "bottom3_pct": 0.0,
            "most_likely_position": 0,
            "most_likely_position_pct": 0.0,
        })
    return predicted_table, winners_odds


def _empty_mls_cup_odds() -> dict:
    return {
        "competition": config.MLS_CUP_COMPETITION,
        "winner_probabilities": {},
        "winners_odds": [],
        "champion": None,
        "simulations_run": None,
    }


def _enrich_mls_payload(comp: str, payload: dict) -> dict:
    """Attach MLS Cup playoff bracket + Shield/East/West/Cup winner odds."""
    if not str(comp or "").startswith("United States/MLS"):
        return payload

    mls_winners = _build_mls_winners_odds_bundle() or {}
    # Always expose an mls_cup slot so clients can rely on the key.
    if "mls_cup" not in mls_winners:
        mls_winners["mls_cup"] = _empty_mls_cup_odds()

    payload["mls_winners_odds"] = mls_winners
    payload.setdefault("format", {}).setdefault("extensions", {})
    payload["format"]["extensions"]["mls_winner_views"] = list(mls_winners.keys())
    payload.setdefault("predicted", {})
    payload["predicted"]["mls_winner_views"] = mls_winners

    _, view = resolve_competition_query(comp)
    view_key_map = {
        "shield": "supporters_shield",
        "east": "eastern_conference",
        "west": "western_conference",
    }
    if comp == config.MLS_CUP_COMPETITION:
        view_key = "mls_cup"
    elif view:
        view_key = view_key_map.get(view)
    else:
        view_key = None

    # Only remap predicted.winner for an explicit view / MLS Cup URL.
    # Default ``United States/MLS`` keeps Supporters Shield table odds and
    # exposes Cup odds under ``mls_winners_odds.mls_cup``.
    if view_key and view_key in mls_winners:
        view_payload = mls_winners[view_key]
        payload["predicted"].setdefault("winner", {})
        if view_payload.get("winner_probabilities") is not None:
            payload["predicted"]["winner"]["probabilities"] = view_payload["winner_probabilities"]
        if view_payload.get("winners_odds") is not None:
            payload["predicted"]["winners_odds"] = view_payload["winners_odds"]
        if view_payload.get("champion") is not None:
            payload["predicted"]["winner"]["champion"] = view_payload["champion"]
        if view_payload.get("simulations_run") is not None:
            payload["predicted"]["winner"]["simulations_run"] = view_payload["simulations_run"]

    bracket_section = payload.get("bracket") if isinstance(payload.get("bracket"), dict) else {}
    projected = bracket_section.get("projected") if isinstance(bracket_section.get("projected"), dict) else None
    bracket_file = _load_mls_projected_bracket()
    if _mls_bracket_has_playoff_structure(bracket_file):
        projected = dict(bracket_file)
        bracket_section["projected"] = projected
        payload["bracket"] = bracket_section

        cup_data = projected.get("mls_cup") or {}
        cup_probs = projected.get("mls_cup_winner_probabilities") or {}
        if cup_data.get("winner"):
            payload["mls_cup_winner"] = cup_data.get("winner")
        # Convenience mirrors for clients that expect Cup fields beside the bracket.
        payload["mls_cup"] = cup_data or None
        if cup_probs:
            payload["mls_cup_winner_probabilities"] = {
                k: round(float(v), 2) for k, v in cup_probs.items() if float(v or 0) > 0
            }
            # Refresh mls_cup odds from the live bracket file when the projected
            # table CSV lacks an ``United States/MLS - MLS Cup`` sheet.
            cup_view = mls_winners.get("mls_cup") or _empty_mls_cup_odds()
            if not cup_view.get("winner_probabilities"):
                winners_odds = [
                    {
                        "team": team,
                        "win_league_pct": round(float(pct), 2),
                        "top4_pct": None,
                        "bottom3_pct": None,
                        "most_likely_position": None,
                        "most_likely_position_pct": None,
                    }
                    for team, pct in sorted(cup_probs.items(), key=lambda x: -float(x[1] or 0))
                    if float(pct or 0) > 0
                ]
                cup_view = {
                    "competition": config.MLS_CUP_COMPETITION,
                    "winner_probabilities": {
                        k: round(float(v), 2) for k, v in cup_probs.items() if float(v or 0) > 0
                    },
                    "winners_odds": winners_odds,
                    "champion": (
                        winners_odds[0]["team"] if winners_odds else cup_data.get("winner")
                    ),
                    "simulations_run": projected.get("simulations_run"),
                }
                mls_winners["mls_cup"] = cup_view
                payload["mls_winners_odds"] = mls_winners
                payload["predicted"]["mls_winner_views"] = mls_winners

        if comp == config.MLS_CUP_COMPETITION or view_key == "mls_cup":
            payload["predicted"].setdefault("winner", {})
            if cup_probs:
                payload["predicted"]["winner"]["probabilities"] = {
                    k: round(float(v), 2) for k, v in cup_probs.items() if float(v or 0) > 0
                }
            if cup_data.get("winner"):
                payload["predicted"]["winner"]["champion"] = cup_data.get("winner")
            if projected.get("simulations_run"):
                payload["predicted"]["winner"]["simulations_run"] = projected.get("simulations_run")

    # Ensure Cup odds are also mirrored under predicted for the default MLS URL.
    cup_view = (payload.get("mls_winners_odds") or {}).get("mls_cup") or {}
    if cup_view:
        payload["predicted"]["mls_cup"] = {
            "champion": cup_view.get("champion"),
            "probabilities": cup_view.get("winner_probabilities") or {},
            "winners_odds": cup_view.get("winners_odds") or [],
            "simulations_run": cup_view.get("simulations_run"),
        }

    if not payload.get("fixtures"):
        payload["fixtures"] = _load_fixtures(comp)
    return payload


def _projected_table_looks_like_completed_prior_season(comp_name: str, rows: list[dict]) -> bool:
    """True when projected rows look like a finished prior season (preseason stale CSV).

    Fresh PATH B projections also have high total ``P`` (almost all games are
    *predicted*), so we key off ``PlayedReal`` instead. Prior-season leftovers
    typically have median PlayedReal ≥ 30; preseason PATH B has PlayedReal ≈ 0.
    """
    if not rows or len(rows) < 8:
        return False
    played_real = []
    played_total = []
    for row in rows:
        try:
            played_real.append(int(float(row.get("PlayedReal") or 0)))
        except (TypeError, ValueError):
            played_real.append(0)
        try:
            played_total.append(int(float(row.get("P") or 0)))
        except (TypeError, ValueError):
            played_total.append(0)
    if not played_real:
        return False
    played_real_sorted = sorted(played_real)
    median_real = played_real_sorted[len(played_real_sorted) // 2]
    # Fresh PATH B / early season: almost no real results yet.
    if median_real < 8:
        return False
    # Prior-season leftover: most games were already played for real.
    if median_real < 30:
        return False
    try:
        import sys as _sys
        import os as _os
        _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        import season_calendar as sc
        base_comp, _view = resolve_competition_query(comp_name)
        if sc.competition_uses_calendar_year(base_comp):
            return False
        now = datetime.now(timezone.utc)
        if now.month == 7 or (now.month == 8 and now.day < 15):
            return True
        if sc.is_european_club_offseason():
            return True
    except Exception:
        now = datetime.now(timezone.utc)
        if now.month == 7 or (now.month == 8 and now.day < 15):
            return True
    return False


def _load_usable_projected_table(comp_name: str) -> list[dict]:
    rows = _load_projected_competition_table(comp_name) or []
    if rows and _projected_table_looks_like_completed_prior_season(comp_name, rows):
        return []
    return rows


def build_league_data_payload(comp_name: str) -> dict:
    """Build the canonical league-data API payload for one competition."""
    comp = str(comp_name or "").strip()

    cached = _load_league_data_from_cache(comp)
    if cached is not None:
        return cached

    fmt = competition_format_spec(comp)

    comp_table = _load_usable_projected_table(comp)
    predicted_table = list(comp_table or [])
    winner_fields = _build_winner_probability_payload(comp_table) if comp_table else {}

    if not predicted_table:
        predicted_table, roster_winners = _roster_predicted_table(comp)
        if predicted_table:
            winner_fields = _build_winner_probability_payload(predicted_table)
        else:
            roster_winners = []

    # Dedupe predicted rows the same way as real tables
    if predicted_table:
        fake = {
            "groups": [{"name": "Overall", "entries": [
                {**row, "team": row.get("team"), "P": row.get("P") or row.get("played") or 0}
                for row in predicted_table
            ]}]
        }
        deduped = _dedupe_standings_groups(fake, comp)
        entries = (deduped.get("groups") or [{}])[0].get("entries") or []
        # Preserve original projected fields keyed by team
        by_team = {str(r.get("team", "")).strip(): r for r in predicted_table}
        predicted_table = []
        for entry in entries:
            team = str(entry.get("team", "")).strip()
            base = dict(by_team.get(team) or entry)
            base["team"] = team
            predicted_table.append(base)
        winner_fields = _build_winner_probability_payload(predicted_table) if predicted_table else winner_fields

    position_odds = _build_position_odds(predicted_table)
    predicted_groups = _load_predicted_groups(comp, predicted_table)
    real_standings = _load_real_standings(comp)
    bracket = _build_bracket_section(comp)
    fixtures = _load_fixtures(comp)

    winners_odds = winner_fields.get("winners_odds", [])
    predicted = {
        "table": predicted_table,
        "groups": predicted_groups,
        "winner": {
            "champion": winner_fields.get("champion"),
            "probabilities": winner_fields.get("winner_probabilities") or {},
            "simulations_run": winner_fields.get("simulations_run"),
        },
        "winners_odds": winners_odds,
        "position_odds": position_odds,
    }

    is_mls = str(comp or "").startswith("United States/MLS")
    payload: dict = {
        "ok": True,
        "competition": comp,
        "format": fmt,
        "predicted": predicted,
        "real": {"standings": real_standings},
        "bracket": bracket,
        "fixtures": fixtures,
    }

    if not is_mls:
        payload["predicted_table"] = predicted_table
        payload["position_odds"] = {
            "simple": position_odds["simple"],
            "detailed": position_odds["detailed"],
        }
        payload["winners_odds"] = winners_odds
        payload["real_table"] = real_standings
        for key in ("winner_probabilities", "champion", "simulations_run"):
            if winner_fields.get(key) is not None:
                payload[key] = winner_fields[key]

    payload = _enrich_league_data_cup_fields(comp, payload)
    if payload.get("knockout"):
        bracket["knockout"] = payload["knockout"]
    if payload.get("odds_knockout"):
        bracket["odds_knockout"] = payload["odds_knockout"]
    if payload.get("real_knockout"):
        bracket["real_knockout"] = payload["real_knockout"]
    if payload.get("winner_probabilities"):
        predicted["winner"]["probabilities"] = payload["winner_probabilities"]
    if payload.get("champion"):
        predicted["winner"]["champion"] = payload["champion"]
    if payload.get("winners_odds"):
        predicted["winners_odds"] = payload["winners_odds"]
        payload["winners_odds"] = payload["winners_odds"]
    payload["bracket"] = bracket
    payload["predicted"] = predicted

    payload = _enrich_mls_payload(comp, payload)

    _write_league_data_cache(comp, payload)

    return payload
