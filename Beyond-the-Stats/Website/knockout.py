"""Knockout bracket structures (World Cup, playoff formats)."""
import json
import re

import pandas as pd

import config
from math_utils import _safe_float
from predictions import (
    _load_all_fixtures_by_competition,
    _load_json_payload,
    _load_projected_tables,
    _utc_to_et,
)

_RN_RE = re.compile(r"[^a-z0-9]+")

# Canonical knockout JSON keys shared by World Cup projection, cup APIs,
# mobile feed, and the iOS app (underscore form, no hyphenated round keys).
_KNOCKOUT_STAGE_KEY_ALIASES = {
    "round-of-32": "round_of_32",
    "round-of-16": "round_of_16",
    "quarter-finals": "quarterfinals",
    "quarterfinals": "quarterfinals",
    "semi-finals": "semifinals",
    "semifinals": "semifinals",
    "third-place": "third_place",
    "knockout-round-play-offs": "knockout_round_playoffs",
    "knockout-round-playoff": "knockout_round_playoffs",
    "first-round-playoff": "knockout_round_playoffs",
    "final": "final",
}


def _build_knockout_framework(comp_name):
    """Return bracket topology for knockout competitions.

    Returns a list of round descriptors with match slots, feeding
    information (``feeds_to``), and ordering for bracket rendering.
    For unknown competitions returns an empty list.
    """
    uefa_rounds = {
        "UEFA/Champions League": [
            {"name": "Knockout Round Play-offs", "order": 1, "matches_count": 8},
            {"name": "Round of 16", "order": 2, "matches_count": 8},
            {"name": "Quarter-finals", "order": 3, "matches_count": 4,
             "matchups": [
                 {"slot": 1, "feeds_to": {"round": "Semi-finals", "slot": 1}},
                 {"slot": 2, "feeds_to": {"round": "Semi-finals", "slot": 1}},
                 {"slot": 3, "feeds_to": {"round": "Semi-finals", "slot": 2}},
                 {"slot": 4, "feeds_to": {"round": "Semi-finals", "slot": 2}},
             ]},
            {"name": "Semi-finals", "order": 4, "matches_count": 2},
            {"name": "Final", "order": 5, "matches_count": 1},
        ],
        "UEFA/Europa League": [
            {"name": "Knockout Round Play-offs", "order": 1, "matches_count": 8},
            {"name": "Round of 16", "order": 2, "matches_count": 8},
            {"name": "Quarter-finals", "order": 3, "matches_count": 4,
             "matchups": [
                 {"slot": 1, "feeds_to": {"round": "Semi-finals", "slot": 1}},
                 {"slot": 2, "feeds_to": {"round": "Semi-finals", "slot": 1}},
                 {"slot": 3, "feeds_to": {"round": "Semi-finals", "slot": 2}},
                 {"slot": 4, "feeds_to": {"round": "Semi-finals", "slot": 2}},
             ]},
            {"name": "Semi-finals", "order": 4, "matches_count": 2},
            {"name": "Final", "order": 5, "matches_count": 1},
        ],
        "UEFA/Conference League": [
            {"name": "Knockout Round Play-offs", "order": 1, "matches_count": 8},
            {"name": "Round of 16", "order": 2, "matches_count": 8},
            {"name": "Quarter-finals", "order": 3, "matches_count": 4,
             "matchups": [
                 {"slot": 1, "feeds_to": {"round": "Semi-finals", "slot": 1}},
                 {"slot": 2, "feeds_to": {"round": "Semi-finals", "slot": 1}},
                 {"slot": 3, "feeds_to": {"round": "Semi-finals", "slot": 2}},
                 {"slot": 4, "feeds_to": {"round": "Semi-finals", "slot": 2}},
             ]},
            {"name": "Semi-finals", "order": 4, "matches_count": 2},
            {"name": "Final", "order": 5, "matches_count": 1},
        ],
        "FIFA/World Cup": [
            {"name": "Round of 32", "order": 1, "matches_count": 16},
            {"name": "Round of 16", "order": 2, "matches_count": 8},
            {"name": "Quarter-finals", "order": 3, "matches_count": 4,
             "matchups": [
                 {"slot": 1, "feeds_to": {"round": "Semi-finals", "slot": 1}},
                 {"slot": 2, "feeds_to": {"round": "Semi-finals", "slot": 1}},
                 {"slot": 3, "feeds_to": {"round": "Semi-finals", "slot": 2}},
                 {"slot": 4, "feeds_to": {"round": "Semi-finals", "slot": 2}},
             ]},
            {"name": "Semi-finals", "order": 4, "matches_count": 2},
            {"name": "Third Place", "order": 5, "matches_count": 1},
            {"name": "Final", "order": 6, "matches_count": 1},
        ],
        "United States/MLS": [
            {"name": "First Round", "order": 1, "matches_count": 8,
             "matchups": [
                 {"slot": 1, "feeds_to": {"round": "Conference Semifinals", "slot": 1}},
                 {"slot": 2, "feeds_to": {"round": "Conference Semifinals", "slot": 1}},
                 {"slot": 3, "feeds_to": {"round": "Conference Semifinals", "slot": 2}},
                 {"slot": 4, "feeds_to": {"round": "Conference Semifinals", "slot": 2}},
                 {"slot": 5, "feeds_to": {"round": "Conference Semifinals", "slot": 3}},
                 {"slot": 6, "feeds_to": {"round": "Conference Semifinals", "slot": 3}},
                 {"slot": 7, "feeds_to": {"round": "Conference Semifinals", "slot": 4}},
                 {"slot": 8, "feeds_to": {"round": "Conference Semifinals", "slot": 4}},
             ]},
            {"name": "Conference Semifinals", "order": 2, "matches_count": 4,
             "matchups": [
                 {"slot": 1, "feeds_to": {"round": "Conference Finals", "slot": 1}},
                 {"slot": 2, "feeds_to": {"round": "Conference Finals", "slot": 1}},
                 {"slot": 3, "feeds_to": {"round": "Conference Finals", "slot": 2}},
                 {"slot": 4, "feeds_to": {"round": "Conference Finals", "slot": 2}},
             ]},
            {"name": "Conference Finals", "order": 3, "matches_count": 2,
             "matchups": [
                 {"slot": 1, "feeds_to": {"round": "MLS Cup", "slot": 1}},
                 {"slot": 2, "feeds_to": {"round": "MLS Cup", "slot": 1}},
             ]},
            {"name": "MLS Cup", "order": 4, "matches_count": 1},
        ],
        "CONCACAF/Leagues Cup": [
            {"name": "Quarter-finals", "order": 1, "matches_count": 4,
             "matchups": [
                 {"slot": 1, "label": "MLS 1 vs Liga MX 4",
                  "feeds_to": {"round": "Semi-finals", "slot": 1}},
                 {"slot": 2, "label": "MLS 2 vs Liga MX 3",
                  "feeds_to": {"round": "Semi-finals", "slot": 1}},
                 {"slot": 3, "label": "MLS 3 vs Liga MX 2",
                  "feeds_to": {"round": "Semi-finals", "slot": 2}},
                 {"slot": 4, "label": "MLS 4 vs Liga MX 1",
                  "feeds_to": {"round": "Semi-finals", "slot": 2}},
             ]},
            {"name": "Semi-finals", "order": 2, "matches_count": 2,
             "matchups": [
                 {"slot": 1, "feeds_to": {"round": "Final", "slot": 1}},
                 {"slot": 2, "feeds_to": {"round": "Final", "slot": 1}},
             ]},
            {"name": "Third Place", "order": 3, "matches_count": 1},
            {"name": "Final", "order": 4, "matches_count": 1},
        ],
    }

    def _domestic_cup_framework(stages, two_leg_rounds=None):
        """Build framework for a domestic cup from its stage list.

        Computes match counts assuming single-elimination: the round k
        positions before the final has 2^k matches.
        """
        result = []
        total_stages = len(stages)
        for order, stage in enumerate(stages, 1):
            positions_after = total_stages - order
            matches = 2 ** positions_after if positions_after >= 0 else 1
            entry = {"name": stage, "order": order, "matches_count": matches}
            result.append(entry)
        return result

    frameworks = {}
    for c in ("UEFA/Champions League", "UEFA/Europa League", "UEFA/Conference League",
              "Europe/Champions League", "Europe/Europa League", "Europe/Conference League",
              "FIFA/World Cup", "CONCACAF/Leagues Cup"):
        if c in uefa_rounds:
            frameworks[c] = uefa_rounds[c]
        elif c.startswith("Europe/"):
            uefa_key = "UEFA/" + c.split("/", 1)[1]
            if uefa_key in uefa_rounds:
                frameworks[c] = uefa_rounds[uefa_key]
    for c, fmt in config._CUP_FORMATS.items():
        # Prefer knockout_rounds so Phase One / group stages are not treated as KO.
        stages = fmt.get("knockout_rounds") or fmt.get("stages") or []
        if stages and c not in frameworks:
            frameworks[c] = _domestic_cup_framework(stages, fmt.get("two_leg_rounds"))

    mls_views = {"United States/MLS", "United States/MLS - Eastern Conference",
                 "United States/MLS - Western Conference", "United States/MLS - Supporters Shield Table"}
    if comp_name in mls_views:
        return uefa_rounds.get("United States/MLS", [])
    for mv in mls_views:
        if mv not in frameworks:
            frameworks[mv] = uefa_rounds.get("United States/MLS", [])
    return frameworks.get(comp_name, [])


def _normalize_round_label(round_name):
    """Return a safe string round label for knockout grouping."""
    if round_name is None:
        return "Match"
    label = str(round_name).strip()
    return label or "Match"


def _round_to_stage_key(round_name):
    """Convert a round label to the canonical underscore knockout JSON key."""
    label = _normalize_round_label(round_name)
    slug = _RN_RE.sub("-", label.lower()).strip("-")
    if not slug:
        return "match"
    if slug in _KNOCKOUT_STAGE_KEY_ALIASES:
        return _KNOCKOUT_STAGE_KEY_ALIASES[slug]
    return slug.replace("-", "_")


def _normalize_round_token(name):
    return _RN_RE.sub("-", str(name or "").strip().lower()).strip("-")


def _expand_two_leg_knockout(knockout_dict, two_leg_rounds):
    """Expand two-legged knockout rounds into separate Leg 1 / Leg 2 match entries."""
    if not knockout_dict or not two_leg_rounds:
        return knockout_dict
    two_leg_keys = {_normalize_round_token(r) for r in two_leg_rounds}
    expanded = {}
    for stage_key, matches in knockout_dict.items():
        round_name = str(matches[0].get("round", "") or "") if matches else ""
        stage_norm = _normalize_round_token(stage_key)
        round_norm = _normalize_round_token(round_name)
        is_two_leg = (
            stage_norm in two_leg_keys
            or round_norm in two_leg_keys
            or any(tl in stage_norm or tl in round_norm for tl in two_leg_keys)
        )
        if not is_two_leg:
            expanded[stage_key] = matches
            continue
        new_matches = []
        for idx, m in enumerate(matches, start=1):
            tie_id = m.get("slot") or m.get("tie_id") or idx
            rnd_label = round_name or stage_key.replace("-", " ").title()
            leg1 = dict(m)
            leg1["leg"] = 1
            leg1["tie_id"] = tie_id
            leg1["label"] = f"{rnd_label} — Leg 1"
            new_matches.append(leg1)
            leg2 = dict(m)
            leg2["leg"] = 2
            leg2["tie_id"] = tie_id
            leg2["home_team"] = m.get("away_team", "")
            leg2["away_team"] = m.get("home_team", "")
            leg2["label"] = f"{rnd_label} — Leg 2"
            leg2["prob_home"] = m.get("prob_away")
            leg2["prob_away"] = m.get("prob_home")
            new_matches.append(leg2)
        expanded[stage_key] = new_matches
    return expanded


def _append_projected_cup_matches(matches, comp, bracket_data):
    """Append projected cup bracket matchups for *comp* into *matches*."""
    if not isinstance(bracket_data, dict):
        return

    entry = None
    comps = bracket_data.get("competitions")
    if isinstance(comps, dict):
        entry = comps.get(comp)

    # Legacy flat format: {competition: {round_name: [match, ...]}}
    if entry is None:
        legacy = bracket_data.get(comp)
        if isinstance(legacy, dict):
            if isinstance(legacy.get("rounds"), list):
                entry = legacy
            else:
                for round_name, round_matches in legacy.items():
                    if not isinstance(round_matches, list):
                        continue
                    for m in round_matches:
                        if not isinstance(m, dict):
                            continue
                        hm = str(m.get("home_team", "") or "")
                        aw = str(m.get("away_team", "") or "")
                        if not hm or not aw:
                            continue
                        matches.append({
                            "home_team": hm,
                            "away_team": aw,
                            "home_score": None,
                            "away_score": None,
                            "status": "pre",
                            "kickoff_utc": _utc_to_et(str(m.get("match_datetime_utc", "") or "")),
                            "round": _normalize_round_label(round_name),
                            "competition": comp,
                            "match_id": str(m.get("match_id", "") or ""),
                        })
                return

    if not isinstance(entry, dict):
        return

    for rnd in entry.get("rounds") or []:
        if not isinstance(rnd, dict):
            continue
        rnd_name = _normalize_round_label(rnd.get("name", ""))
        for m in rnd.get("matches", []):
            if not isinstance(m, dict):
                continue
            hm = str(m.get("home_team", "") or "")
            aw = str(m.get("away_team", "") or "")
            if not hm or not aw:
                continue
            matches.append({
                "home_team": hm,
                "away_team": aw,
                "home_score": None,
                "away_score": None,
                "status": "pre",
                "kickoff_utc": _utc_to_et(str(m.get("match_datetime_utc", "") or "")),
                "round": rnd_name,
                "competition": comp,
                "match_id": str(m.get("match_id", "") or ""),
            })


def _gather_competition_cup_matches(comp):
    """Collect completed, live, and projected cup matches for knockout building."""
    from competition_rules import collect_competition_games
    from live_poller import _live_scores, _live_scores_lock
    from standings import _load_live_score_history

    matches = []
    seen_ids = set()

    for g in collect_competition_games(comp):
        mid = g.get("match_id", "")
        if mid and mid in seen_ids:
            continue
        if mid:
            seen_ids.add(mid)
        matches.append(g)

    history = _load_live_score_history()
    for g in history:
        if g.get("competition") != comp:
            continue
        mid = g.get("match_id", "")
        if mid and mid in seen_ids:
            continue
        if mid:
            seen_ids.add(mid)
        matches.append(g)

    with _live_scores_lock:
        current = _live_scores.get(comp, {}).get("games", [])
    for g in current:
        mid = g.get("match_id", "")
        if mid not in seen_ids:
            if mid:
                seen_ids.add(mid)
            matches.append(g)

    bracket_data = _load_json_payload(config.CUP_PROJECTED_BRACKET_FILE)
    if isinstance(bracket_data, dict):
        comps = bracket_data.get("competitions", bracket_data)
        if isinstance(comps, dict) and comp in comps:
            entry = comps[comp]
            if isinstance(entry, dict):
                for rnd in entry.get("rounds") or []:
                    rnd_name = rnd.get("name", "")
                    for m in rnd.get("matches", []):
                        hm = str(m.get("home_team", "") or "")
                        aw = str(m.get("away_team", "") or "")
                        if not hm or not aw:
                            continue
                        matches.append({
                            "home_team": hm,
                            "away_team": aw,
                            "home_score": m.get("actual_home_goals"),
                            "away_score": m.get("actual_away_goals"),
                            "status": "post" if str(m.get("status", "")).lower() in ("completed", "post") else "pre",
                            "kickoff_utc": _utc_to_et(str(m.get("match_datetime_utc", "") or "")),
                            "round": rnd_name,
                            "competition": comp,
                            "match_id": str(m.get("match_id", "") or ""),
                            "pred_home_goals": m.get("pred_home_goals"),
                            "pred_away_goals": m.get("pred_away_goals"),
                            "prob_home": m.get("prob_home"),
                            "prob_draw": m.get("prob_draw"),
                            "prob_away": m.get("prob_away"),
                        })

    odds_index = {}
    try:
        odds_df = pd.read_csv(config.CUP_UPCOMING_FILE)
        if not odds_df.empty and all(c in odds_df.columns for c in ("home_team", "away_team", "prob_home", "prob_draw", "prob_away")):
            for _, row in odds_df.iterrows():
                key = (str(row["home_team"]).strip().lower(), str(row["away_team"]).strip().lower())
                odds_index[key] = {
                    "prob_home": _safe_float(row["prob_home"], None),
                    "prob_draw": _safe_float(row["prob_draw"], None),
                    "prob_away": _safe_float(row["prob_away"], None),
                }
    except Exception:
        pass

    from competition_rules import annotate_knockout_rounds

    matches = annotate_knockout_rounds(matches, comp)

    for g in matches:
        rnd = _normalize_round_label(g.get("round"))
        order = g.get("round_order", 0)
        if not isinstance(order, (int, float)):
            try:
                order = int(order)
            except (ValueError, TypeError):
                order = 0
        g["round_order"] = order
        g["round"] = rnd

        if g.get("status") == "post":
            hs = g.get("home_score")
            aws = g.get("away_score")
            winner = str(g.get("winner") or "").strip()
            if winner:
                g["winner"] = winner
            elif hs is not None and aws is not None:
                if hs > aws:
                    g["winner"] = g.get("home_team", "")
                elif aws > hs:
                    g["winner"] = g.get("away_team", "")

        if g.get("prob_home") is None:
            hm_name = str(g.get("home_team", "")).strip().lower()
            aw_name = str(g.get("away_team", "")).strip().lower()
            odds = odds_index.get((hm_name, aw_name)) or odds_index.get((aw_name, hm_name), {})
            g["prob_home"] = odds.get("prob_home")
            g["prob_draw"] = odds.get("prob_draw")
            g["prob_away"] = odds.get("prob_away")

    return matches


def _build_cup_knockout_payload(matches, comp):
    """Build knockout / odds_knockout / real_knockout with optional two-leg expansion."""
    from competition_rules import classify_match_stage, cup_format, load_wc_team_groups

    cup_format_meta = cup_format(comp)
    team_to_group = load_wc_team_groups() if comp == "FIFA/World Cup" else {}
    bracket_matches = matches
    if cup_format_meta and cup_format_meta.get("format") in {"group_stage_then_knockout", "league_phase_then_knockout"}:
        bracket_matches = [
            g for g in matches
            if classify_match_stage(g, comp, team_to_group) == "knockout"
        ]
    elif cup_format_meta and cup_format_meta.get("format") == "knockout":
        bracket_matches = [
            g for g in matches
            if classify_match_stage(g, comp, team_to_group) in {"knockout", "league"}
        ]

    knockout, odds_knockout, real_knockout = _build_knockout_wc_format(bracket_matches)
    if cup_format_meta and cup_format_meta.get("two_leg_rounds"):
        knockout = _expand_two_leg_knockout(knockout, cup_format_meta["two_leg_rounds"])
        odds_knockout = _expand_two_leg_knockout(odds_knockout, cup_format_meta["two_leg_rounds"])
        real_knockout = _expand_two_leg_knockout(real_knockout, cup_format_meta["two_leg_rounds"])
    return knockout, odds_knockout, real_knockout


def _enrich_league_data_cup_fields(comp, payload):
    """Add tournament winner odds and knockout bracket data for cup competitions."""
    from standings import _UEFA_COMPETITIONS

    cup_format = config._CUP_FORMATS.get(comp)
    is_cup = comp in config._CUP_FORMATS or comp in _UEFA_COMPETITIONS
    if not is_cup:
        return payload

    if cup_format:
        payload["cup_format"] = cup_format

    bracket_data = _load_json_payload(config.CUP_PROJECTED_BRACKET_FILE)
    if isinstance(bracket_data, dict):
        comps = bracket_data.get("competitions", bracket_data)
        if isinstance(comps, dict) and comp in comps:
            entry = comps[comp]
            if isinstance(entry, dict):
                for key in ("champion", "simulations_run", "winner_probabilities"):
                    if key in entry:
                        payload[key] = entry[key]
                probs = entry.get("winner_probabilities") or {}
                if probs:
                    winners = []
                    for team, pct in sorted(probs.items(), key=lambda x: -(x[1] or 0)):
                        pct_f = float(pct or 0)
                        display_pct = round(pct_f * 100, 2) if pct_f <= 1 else round(pct_f, 2)
                        winners.append({
                            "team": team,
                            "win_league_pct": display_pct,
                            "top4_pct": None,
                            "bottom3_pct": None,
                            "most_likely_position": None,
                            "most_likely_position_pct": None,
                        })
                    payload["winners_odds"] = winners

    matches = _gather_competition_cup_matches(comp)
    if matches:
        knockout, odds_knockout, real_knockout = _build_cup_knockout_payload(matches, comp)
        payload["knockout"] = knockout
        payload["odds_knockout"] = odds_knockout
        payload["real_knockout"] = real_knockout

    return payload


def _enrich_tournament_payload(comp_name, data):
    """Add projected tables and fixtures so cup pages match World Cup format."""
    if not isinstance(data, dict):
        return data

    projected = _load_projected_tables(config.CUP_PROJECTED_TABLE_FILE)
    rows = (projected.get("tables") or {}).get(comp_name) or []
    if rows and not data.get("group_tables"):
        data["group_tables"] = [{
            "group": "League Phase",
            "teams": [{
                "team": row.get("team"),
                "P": row.get("P"),
                "W": row.get("W"),
                "D": row.get("D"),
                "L": row.get("L"),
                "GF": row.get("GF"),
                "GA": row.get("GA"),
                "GD": row.get("GD"),
                "Pts": row.get("Pts"),
                "position": row.get("position"),
                "PlayedPred": row.get("PlayedPred"),
                "PlayedReal": row.get("PlayedReal"),
            } for row in rows if row.get("team")],
        }]

    fixtures_by_comp = _load_all_fixtures_by_competition(config.CUP_UPCOMING_FILE)
    comp_fixtures = fixtures_by_comp.get(comp_name) or []
    if comp_fixtures and not data.get("group_fixtures"):
        data["group_fixtures"] = comp_fixtures

    if rows:
        pos_probs = {}
        for row in rows:
            team = row.get("team")
            if not team:
                continue
            odds = {}
            raw_odds = row.get("position_odds_json")
            if raw_odds:
                try:
                    odds = json.loads(raw_odds) if isinstance(raw_odds, str) else raw_odds
                except Exception:
                    odds = {}
            if odds:
                pos_probs[team] = {
                    f"group_position_{key}": value
                    for key, value in odds.items()
                }
        if pos_probs:
            simulations = data.get("simulations") or {}
            simulations.setdefault("position_probabilities", pos_probs)
            data["simulations"] = simulations

    return data


def _compute_odds_bracket():
    """Build odds-weighted bracket for all cup competitions.

    For each match in the projected cup bracket JSON, picks the winner
    with the higher win probability from the cup predictions CSV.
    Returns ``{competition_name: {round_name: [match_dict, …], …}, …}``.
    """
    bracket_data = _load_json_payload(config.CUP_PROJECTED_BRACKET_FILE)
    if not isinstance(bracket_data, dict):
        return {}
    comps = bracket_data.get("competitions", bracket_data)
    if not isinstance(comps, dict):
        return {}

    # Build odds index from cup predictions CSV
    odds_index = {}
    try:
        odds_df = pd.read_csv(config.CUP_UPCOMING_FILE)
        if not odds_df.empty and all(c in odds_df.columns for c in ("home_team", "away_team", "prob_home")):
            for _, row in odds_df.iterrows():
                key = (str(row["home_team"]).strip().lower(), str(row["away_team"]).strip().lower())
                odds_index[key] = _safe_float(row["prob_home"], 0)
    except Exception:
        pass

    result = {}
    for comp_name, entry in comps.items():
        if not isinstance(entry, dict):
            continue
        rounds = entry.get("rounds") or []
        comp_result = {}
        for rnd in rounds:
            rname = rnd.get("name", "")
            matches = rnd.get("matches") or []
            rnd_matches = []
            for m in matches:
                hm = str(m.get("home_team", "")).strip()
                aw = str(m.get("away_team", "")).strip()
                if not hm or not aw:
                    continue
                key = (hm.lower(), aw.lower())
                ph = odds_index.get(key, 0)
                pa = odds_index.get((aw.lower(), hm.lower()), 0)
                winner = hm if ph > pa else (aw if pa > ph else "")
                rnd_matches.append({
                    "home_team": hm,
                    "away_team": aw,
                    "prob_home": ph,
                    "prob_away": pa,
                    "winner": winner,
                    "slot": m.get("slot", 0),
                    "stage": rname,
                })
            if rnd_matches:
                comp_result[rname] = rnd_matches
        if comp_result:
            result[comp_name] = comp_result
    return result


def _build_knockout_wc_format(matches):
    """Convert a flat list of cup matches into WC-style knockout/odds_knockout/real_knockout.

    Returns ``(knockout, odds_knockout, real_knockout)`` where each is a
    ``{stage_key: [match_dict, ...]}`` object.
    """
    by_round = {}
    round_orders = {}
    for g in matches:
        rnd = _normalize_round_label(g.get("round"))
        by_round.setdefault(rnd, []).append(g)
        order = g.get("round_order", 999)
        try:
            order = int(order)
        except (ValueError, TypeError):
            order = 999
        if rnd not in round_orders or order < round_orders[rnd]:
            round_orders[rnd] = order

    knockout = {}
    odds_knockout = {}
    real_knockout = {}

    for rnd_name in sorted(by_round.keys(), key=lambda r: (round_orders.get(r, 999), r)):
        stage_key = _round_to_stage_key(rnd_name)
        round_matches = sorted(by_round[rnd_name], key=lambda m: m.get("kickoff_utc", "") or "")

        ko_list, odds_list, real_list = [], [], []
        for idx, g in enumerate(round_matches, start=1):
            dt_utc = g.get("kickoff_utc", "") or ""
            match_date = ""
            try:
                mdt = pd.to_datetime(dt_utc, utc=True)
                match_date = mdt.strftime("%Y-%m-%d")
            except Exception:
                pass

            winner = None
            if g.get("status") == "post":
                winner = str(g.get("winner") or "").strip() or None
                if not winner:
                    hs, aws = g.get("home_score"), g.get("away_score")
                    if hs is not None and aws is not None:
                        winner = g.get("home_team", "") if hs > aws else (g.get("away_team", "") if aws > hs else None)

            prob_home = g.get("prob_home")
            prob_draw = g.get("prob_draw")
            prob_away = g.get("prob_away")

            base = {
                "label": f"{rnd_name} {idx}",
                "stage": stage_key,
                "round": rnd_name,
                "match_date": match_date,
                "match_datetime_utc": dt_utc,
                "home_team": g.get("home_team", ""),
                "away_team": g.get("away_team", ""),
                "home_score": g.get("home_score"),
                "away_score": g.get("away_score"),
                "status": g.get("status", "pre"),
                "winner": winner,
                "slot": idx,
                "match_id": g.get("match_id", ""),
                "prob_home": prob_home,
                "prob_draw": prob_draw,
                "prob_away": prob_away,
                "pred_home_goals": g.get("pred_home_goals"),
                "pred_away_goals": g.get("pred_away_goals"),
                "venue": g.get("venue", ""),
            }
            ko_list.append(base)

            odds_entry = dict(base)
            ph = _safe_float(prob_home, 0)
            pa = _safe_float(prob_away, 0)
            hm = str(base["home_team"]).strip()
            aw = str(base["away_team"]).strip()
            if ph > pa and hm:
                odds_entry["winner"] = hm
            elif pa > ph and aw:
                odds_entry["winner"] = aw
            odds_entry["odds_weighted"] = True
            odds_list.append(odds_entry)

            real_entry = dict(base)
            if g.get("status") != "post":
                real_entry["winner"] = None
            real_entry["from_live"] = g.get("status") in ("post", "in")
            real_list.append(real_entry)

        knockout[stage_key] = ko_list
        odds_knockout[stage_key] = odds_list
        real_knockout[stage_key] = real_list

    return knockout, odds_knockout, real_knockout
