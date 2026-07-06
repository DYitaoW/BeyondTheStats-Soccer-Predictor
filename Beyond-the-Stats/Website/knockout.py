"""Knockout bracket structures (World Cup, playoff formats)."""
import re

import pandas as pd

import config
from math_utils import _safe_float
from predictions import _load_json_payload

_RN_RE = re.compile(r"[^a-z0-9]+")

def _build_knockout_framework(comp_name):
    """Return bracket topology for knockout competitions.

    Returns a dict with ``knockout_rounds`` (round descriptors with match
    slots and feeding information) and optionally ``bracket_map`` (how
    round slots connect to subsequent rounds), or an empty dict for
    unknown competitions.

    The ``feeds_to`` field in round-level matchups tells the frontend
    which round-name + slot the winners feed into, enabling bracket
    rendering without hardcoded knowledge of the competition format.
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
    }
    frameworks = {}
    for c in ("UEFA/Champions League", "UEFA/Europa League", "UEFA/Conference League",
              "Europe/Champions League", "Europe/Europa League", "Europe/Conference League",
              "FIFA/World Cup"):
        if c in uefa_rounds:
            frameworks[c] = uefa_rounds[c]
        elif c.startswith("Europe/"):
            uefa_key = "UEFA/" + c.split("/", 1)[1]
            if uefa_key in uefa_rounds:
                frameworks[c] = uefa_rounds[uefa_key]
    return frameworks.get(comp_name, [])

def _round_to_stage_key(round_name):
    """Convert 'Quarter-finals' -> 'quarter-finals', 'Round of 16' -> 'round-of-16'."""
    return _RN_RE.sub("-", round_name.strip().lower()).strip("-")

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
        rnd = g.get("round", "") or "Match"
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
