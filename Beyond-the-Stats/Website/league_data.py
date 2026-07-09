"""Unified ``/api/league-data`` response builder.

Every competition returns the same top-level shape. Competition-specific
rules (MLS conferences, Liga MX tournaments, cup knockouts) are described in
``format`` and surfaced in ``predicted.groups`` / ``bracket`` / ``real.standings``.
"""
from __future__ import annotations

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
    _load_league_teams,
    _UEFA_COMPETITIONS,
)


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
    return _compute_standings_from_history(comp_name) or _build_fallback_standings(comp_name)


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


def _build_bracket_section(comp_name: str) -> dict:
    bracket = {
        "projected": None,
        "knockout": None,
        "odds_knockout": None,
        "real_knockout": None,
    }
    base_comp, _view = resolve_competition_query(comp_name)

    if base_comp == "United States/MLS" or comp_name.startswith("United States/MLS"):
        projected = _load_json_payload(config.MLS_PROJECTED_BRACKET_FILE)
        if isinstance(projected, dict) and projected:
            bracket["projected"] = projected
        return bracket

    cup_fmt = config._CUP_FORMATS.get(base_comp) or config._CUP_FORMATS.get(comp_name)
    is_cup = bool(cup_fmt) or base_comp in _UEFA_COMPETITIONS or comp_name in _UEFA_COMPETITIONS
    if not is_cup:
        return bracket

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
    league_teams = _load_league_teams()
    roster = league_teams.get(comp_name)
    if not roster:
        base_comp, _ = resolve_competition_query(comp_name)
        roster = league_teams.get(base_comp)
    if not roster:
        return [], []
    predicted_table = []
    winners_odds = []
    for pos, team in enumerate(sorted(roster), start=1):
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


def _enrich_mls_payload(comp: str, payload: dict) -> dict:
    if not str(comp or "").startswith("United States/MLS"):
        return payload

    mls_winners = _build_mls_winners_odds_bundle()
    if mls_winners:
        payload["mls_winners_odds"] = mls_winners
        payload["format"]["extensions"]["mls_winner_views"] = list(mls_winners.keys())
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

    if view_key and view_key in mls_winners:
        view_payload = mls_winners[view_key]
        for key in ("winner_probabilities", "winners_odds", "champion", "simulations_run"):
            if view_payload.get(key) is not None:
                payload[key] = view_payload[key]
                if key == "winner_probabilities":
                    payload["predicted"]["winner"]["probabilities"] = view_payload[key]
                elif key == "winners_odds":
                    payload["predicted"]["winners_odds"] = view_payload[key]
                    payload["winners_odds"] = view_payload[key]
                elif key == "champion":
                    payload["predicted"]["winner"]["champion"] = view_payload[key]
                elif key == "simulations_run":
                    payload["predicted"]["winner"]["simulations_run"] = view_payload[key]

    bracket_file = _load_json_payload(config.MLS_PROJECTED_BRACKET_FILE)
    if isinstance(bracket_file, dict) and bracket_file:
        payload["bracket"]["projected"] = bracket_file
        cup_data = bracket_file.get("mls_cup") or {}
        if cup_data.get("winner"):
            payload["mls_cup_winner"] = cup_data.get("winner")

    if not payload.get("fixtures"):
        payload["fixtures"] = _load_fixtures(comp)
    return payload


def build_league_data_payload(comp_name: str) -> dict:
    """Build the canonical league-data API payload for one competition."""
    comp = str(comp_name or "").strip()
    fmt = competition_format_spec(comp)

    comp_table = _load_projected_competition_table(comp)
    predicted_table = comp_table or []
    winner_fields = _build_winner_probability_payload(comp_table) if comp_table else {}

    if not predicted_table:
        predicted_table, roster_winners = _roster_predicted_table(comp)
        if predicted_table:
            winner_fields = _build_winner_probability_payload(predicted_table)
        else:
            roster_winners = []

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

    payload: dict = {
        "ok": True,
        "competition": comp,
        "format": fmt,
        "predicted": predicted,
        "real": {"standings": real_standings},
        "bracket": bracket,
        "fixtures": fixtures,
        # Legacy mirrors
        "predicted_table": predicted_table,
        "position_odds": {
            "simple": position_odds["simple"],
            "detailed": position_odds["detailed"],
        },
        "winners_odds": winners_odds,
        "real_table": real_standings,
    }

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
    return payload
