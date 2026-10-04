"""
Standings projection, winner probabilities, and bracket loading services.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
import pandas as pd

import config
try:
    import competition_ids
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import competition_ids

_projected_tables_cache: dict[str, tuple[float, dict]] = {}
_JSON_PAYLOAD_CACHE: dict[str, tuple[float, object]] = {}
_JSON_PAYLOAD_CACHE_LOCK = threading.Lock()

PROJECTED_TABLE_SOURCES = (
    config.GLOBAL_PROJECTED_TABLE_FILE,
    config.MLS_PROJECTED_TABLE_FILE,
    config.EXTRA_PROJECTED_TABLE_FILE,
    config.CUP_PROJECTED_TABLE_FILE,
)

PROJECTED_WINNER_COMP_ALIASES = {
    "United States/MLS": "United States/MLS - Supporters Shield Table",
}

MLS_SHIELD_TABLE = "United States/MLS - Supporters Shield Table"
MLS_EAST_TABLE = "United States/MLS - Eastern Conference"
MLS_WEST_TABLE = "United States/MLS - Western Conference"
MLS_CONFERENCE_STAT_FIELDS = ("P", "W", "D", "L", "GF", "GA", "GD", "Pts", "PlayedReal", "PlayedPred")


def _copy_projected_result(data: dict) -> dict:
    """Defensive shallow copy: new top dict + tables/leagues/position_odds copies."""
    return {
        "leagues": list(data.get("leagues") or []),
        "tables": dict(data.get("tables") or {}),
        "position_odds": dict(data.get("position_odds") or {}),
    }


def _load_current_season_tables() -> dict | None:
    """Load projected tables from ``current_season_teams.json`` when no CSV exists."""
    if not os.path.exists(config.CURRENT_SEASON_TEAMS_FILE):
        return None
    try:
        with open(config.CURRENT_SEASON_TEAMS_FILE, "r", encoding="utf-8") as f:
            roster = json.load(f)
    except Exception:
        return None
    if not isinstance(roster, dict):
        return None
    leagues = []
    tables = {}
    for comp_name, teams in roster.items():
        if not teams:
            continue
        leagues.append(comp_name)
        entries = []
        for pos, team in enumerate(sorted(teams), start=1):
            entries.append({
                "position": pos, "team": team,
                "P": 0, "W": 0, "D": 0, "L": 0,
                "GF": 0, "GA": 0, "GD": 0, "Pts": 0,
                "PlayedReal": 0, "PlayedPred": 0,
                "win_league_pct": 0.0, "top4_pct": 0.0, "bottom3_pct": 0.0,
                "most_likely_position": 0, "most_likely_position_pct": 0.0,
                "position_odds": {}, "sim_runs": 0,
            })
        tables[comp_name] = entries
    return {"leagues": sorted(leagues), "tables": tables}


def _load_projected_tables(csv_path: str) -> dict:
    """Load projected table CSV into API-ready league/table structure."""
    try:
        mtime = os.path.getmtime(csv_path)
    except OSError:
        return {"leagues": [], "tables": {}}
    cached = _projected_tables_cache.get(os.path.normpath(csv_path))
    if cached is not None and cached[0] == mtime:
        return _copy_projected_result(cached[1])
    if not os.path.exists(csv_path):
        return {"leagues": [], "tables": {}}
    try:
        if getattr(config, "LOW_MEMORY_STATIC", False):
            allowed = {
                "competition", "position", "team", "P", "W", "D", "L", "GF", "GA", "GD", "Pts",
                "PlayedReal", "PlayedPred", "win_league_pct", "top4_pct", "bottom3_pct",
                "most_likely_position", "most_likely_position_pct", "position_odds_json",
                "sim_runs", "remaining_games",
            }
            frame = pd.read_csv(
                csv_path,
                usecols=lambda c: c in allowed,
                dtype={"competition": "string", "team": "string"},
            )
        else:
            frame = pd.read_csv(csv_path)
    except Exception:
        return {"leagues": [], "tables": {}}

    required = {
        "competition", "position", "team", "P", "W", "D", "L", "GF", "GA", "GD", "Pts",
    }
    if frame.empty or not required.issubset(frame.columns):
        return {"leagues": [], "tables": {}}

    frame = frame.copy()
    frame["competition"] = (
        frame["competition"].astype(str).str.strip().map(competition_ids.canonical_competition_id)
    )
    frame = frame[frame["competition"] != ""]
    if frame.empty:
        return {"leagues": [], "tables": {}}

    frame = frame[~frame["competition"].map(config.is_national_team_competition)]
    if frame.empty:
        return {"leagues": [], "tables": {}}

    frame["position"] = pd.to_numeric(frame["position"], errors="coerce")
    frame = frame.sort_values(["competition", "position", "team"], na_position="last")

    tables = {}
    position_odds_tables = {}
    for competition, comp_frame in frame.groupby("competition", dropna=False):
        rows = []
        pos_odds_rows = []
        for _, row in comp_frame.iterrows():
            win_league_pct_raw = pd.to_numeric(row.get("win_league_pct"), errors="coerce")
            top4_pct_raw = pd.to_numeric(row.get("top4_pct"), errors="coerce")
            bottom3_pct_raw = pd.to_numeric(row.get("bottom3_pct"), errors="coerce")
            most_likely_pos_raw = pd.to_numeric(row.get("most_likely_position"), errors="coerce")
            most_likely_pct_raw = pd.to_numeric(row.get("most_likely_position_pct"), errors="coerce")
            sim_runs_raw = pd.to_numeric(row.get("sim_runs"), errors="coerce")
            pos_odds = {}
            raw_odds = row.get("position_odds_json")
            if pd.notna(raw_odds) and str(raw_odds).strip():
                try:
                    parsed_odds = json.loads(str(raw_odds))
                    if isinstance(parsed_odds, dict):
                        pos_odds = {int(k): round(float(v), 2) for k, v in parsed_odds.items()}
                except Exception:
                    pass

            entry = {
                "position": int(row["position"]) if pd.notna(row["position"]) else None,
                "team": str(row["team"]).strip(),
                "P": int(row["P"]) if pd.notna(row["P"]) else 0,
                "W": int(row["W"]) if pd.notna(row["W"]) else 0,
                "D": int(row["D"]) if pd.notna(row["D"]) else 0,
                "L": int(row["L"]) if pd.notna(row["L"]) else 0,
                "GF": int(row["GF"]) if pd.notna(row["GF"]) else 0,
                "GA": int(row["GA"]) if pd.notna(row["GA"]) else 0,
                "GD": int(row["GD"]) if pd.notna(row["GD"]) else 0,
                "Pts": int(row["Pts"]) if pd.notna(row["Pts"]) else 0,
                "PlayedReal": int(row["PlayedReal"]) if pd.notna(row.get("PlayedReal")) else None,
                "PlayedPred": int(row["PlayedPred"]) if pd.notna(row.get("PlayedPred")) else None,
                "win_league_pct": round(float(win_league_pct_raw), 2) if pd.notna(win_league_pct_raw) else None,
                "top4_pct": round(float(top4_pct_raw), 2) if pd.notna(top4_pct_raw) else None,
                "bottom3_pct": round(float(bottom3_pct_raw), 2) if pd.notna(bottom3_pct_raw) else None,
                "most_likely_position": int(most_likely_pos_raw) if pd.notna(most_likely_pos_raw) else None,
                "most_likely_position_pct": round(float(most_likely_pct_raw), 2) if pd.notna(most_likely_pct_raw) else None,
                "position_odds": pos_odds,
                "sim_runs": int(sim_runs_raw) if pd.notna(sim_runs_raw) else None,
            }
            rows.append(entry)
            if pos_odds:
                pos_odds_rows.append({"team": entry["team"], "position_odds": pos_odds})
        tables[str(competition)] = rows
        if pos_odds_rows:
            position_odds_tables[str(competition)] = pos_odds_rows

    leagues = sorted(tables.keys(), key=lambda name: name.lower())
    result = {"leagues": leagues, "tables": tables, "position_odds": position_odds_tables}
    if os.path.normpath(csv_path) == os.path.normpath(config.MLS_PROJECTED_TABLE_FILE):
        _normalize_mls_conference_tables(result)
    _projected_tables_cache[os.path.normpath(csv_path)] = (mtime, result)
    return result


def _normalize_mls_conference_tables(data: dict) -> dict:
    """Ensure MLS conference projected rows reuse Supporters Shield season stats."""
    from competition_rules import mls_conference

    tables = data.get("tables") or {}
    shield_rows = tables.get(MLS_SHIELD_TABLE)
    if not shield_rows:
        return data

    shield_by_team = {str(row.get("team", "")).strip(): row for row in shield_rows if row.get("team")}
    for conf_name, target_conf in ((MLS_EAST_TABLE, "east"), (MLS_WEST_TABLE, "west")):
        conf_rows = tables.get(conf_name)
        if not conf_rows:
            continue
        synced_rows = []
        for row in conf_rows:
            team = str(row.get("team", "")).strip()
            base = shield_by_team.get(team)
            if not base or mls_conference(team) != target_conf:
                continue
            synced = dict(row)
            for field in MLS_CONFERENCE_STAT_FIELDS:
                if field in base:
                    synced[field] = base[field]
            synced_rows.append(synced)
        synced_rows.sort(
            key=lambda item: (
                -(int(item.get("Pts") or 0)),
                -(int(item.get("GD") or 0)),
                -(int(item.get("GF") or 0)),
                str(item.get("team", "")),
            )
        )
        for pos, row in enumerate(synced_rows, start=1):
            row["position"] = pos
        tables[conf_name] = synced_rows
    data["tables"] = tables
    return data


def _build_mls_winners_odds_bundle() -> dict:
    """Return separate winner odds for Shield, East, West, and MLS Cup."""
    bundle: dict = {}
    for key, comp_name in config.MLS_WINNER_VIEWS.items():
        table = _load_projected_competition_table(comp_name)
        if table:
            payload = _build_winner_probability_payload(table)
            if payload.get("winner_probabilities"):
                bundle[key] = {
                    "competition": comp_name,
                    "winner_probabilities": payload.get("winner_probabilities", {}),
                    "winners_odds": payload.get("winners_odds", []),
                    "champion": payload.get("champion"),
                    "simulations_run": payload.get("simulations_run"),
                }

    if "mls_cup" not in bundle:
        bracket = _load_json_payload(config.MLS_PROJECTED_BRACKET_FILE)
        if isinstance(bracket, dict):
            cup_probs = bracket.get("mls_cup_winner_probabilities") or {}
            if cup_probs:
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
                champion = winners_odds[0]["team"] if winners_odds else (bracket.get("mls_cup") or {}).get("winner")
                cup_view = {
                    "competition": config.MLS_CUP_COMPETITION,
                    "winner_probabilities": {k: round(float(v), 2) for k, v in cup_probs.items() if float(v or 0) > 0},
                    "winners_odds": winners_odds,
                    "champion": champion,
                    "simulations_run": bracket.get("simulations_run"),
                }
                make_playoffs = bracket.get("make_playoffs_probabilities") or {}
                if make_playoffs:
                    cup_view["make_playoffs_probabilities"] = {
                        k: round(float(v), 2) for k, v in make_playoffs.items() if float(v or 0) > 0
                    }
                round_reach = bracket.get("round_reach_probabilities") or {}
                if isinstance(round_reach, dict) and round_reach:
                    cup_view["round_reach_probabilities"] = {
                        rnd: {
                            team: round(float(pct), 2)
                            for team, pct in (team_map or {}).items()
                            if float(pct or 0) > 0
                        }
                        for rnd, team_map in round_reach.items()
                        if isinstance(team_map, dict)
                    }
                elim = bracket.get("elimination_round_probabilities") or {}
                if isinstance(elim, dict) and elim:
                    cup_view["elimination_round_probabilities"] = elim
                bundle["mls_cup"] = cup_view
    return bundle


def _slugify_competition_for_leagueresult(competition: str) -> str:
    out = (competition or "").strip().lower()
    out = out.replace("/", "_").replace(" ", "_").replace("-", "_")
    out = out.replace(".", "").replace(",", "").replace("'", "")
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_") or "unknown"


def _rows_from_leagueresult_json(comp_name: str) -> list[dict]:
    lookup_names = [str(comp_name or "").strip()]
    alias = PROJECTED_WINNER_COMP_ALIASES.get(lookup_names[0])
    if alias and alias not in lookup_names:
        lookup_names.append(alias)
    if lookup_names[0] == "United States/MLS":
        lookup_names.append(MLS_SHIELD_TABLE)

    slugs = []
    for name in lookup_names:
        if not name:
            continue
        slug = _slugify_competition_for_leagueresult(name)
        if slug and slug not in slugs:
            slugs.append(slug)

    for region in ("Other", "Europe", "National"):
        for slug in slugs:
            path = os.path.join(config.PROJECT_DIR, "Output", region, "LeagueResult", f"{slug}.json")
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            teams_raw = payload.get("teams")
            if not isinstance(teams_raw, list) or not teams_raw:
                continue
            rows = []
            for entry in teams_raw:
                if not isinstance(entry, dict):
                    continue
                team = str(entry.get("team") or "").strip()
                if not team:
                    continue
                row = dict(entry)
                row["team"] = team
                if isinstance(row.get("position_odds"), str):
                    try:
                        row["position_odds"] = json.loads(row["position_odds"])
                    except Exception:
                        row["position_odds"] = {}
                rows.append(row)
            if rows:
                return rows
    return []


def _load_projected_competition_table(comp_name: str) -> list[dict]:
    """Return projected table rows for a competition from any pipeline CSV."""
    lookup_names = [str(comp_name or "").strip()]
    alias = PROJECTED_WINNER_COMP_ALIASES.get(lookup_names[0])
    if alias and alias not in lookup_names:
        lookup_names.append(alias)
    for lookup in lookup_names:
        if not lookup:
            continue
        for csv_path in PROJECTED_TABLE_SOURCES:
            proj = _load_projected_tables(csv_path)
            table = (proj.get("tables") or {}).get(lookup)
            if table:
                return table
    return _rows_from_leagueresult_json(comp_name)


def _build_winner_probability_payload(comp_table: list[dict], competition: str = "") -> dict:
    """Build World Cup-style winner odds fields from projected table rows."""
    from competition_rules import canonical_team_name, leagues_cup_table_side
    from competition_rules import LEAGUES_CUP_TABLE_LIGA_MX, LEAGUES_CUP_TABLE_MLS
    from competition_rules import normalize_team_key

    comp = str(competition or "").strip()
    filtered_table = list(comp_table or [])
    if comp == "North America/Leagues Cup":
        filtered_table = [
            row for row in filtered_table
            if leagues_cup_table_side(str(row.get("team", "")).strip()) in {
                LEAGUES_CUP_TABLE_MLS, LEAGUES_CUP_TABLE_LIGA_MX,
            }
        ]

    winner_probabilities: dict[str, float] = {}
    winners_odds: list[dict] = []
    champion = None
    sim_runs = None
    best_pct = -1.0
    seen_keys: set[str] = set()

    for row in filtered_table:
        team = canonical_team_name(str(row.get("team", "")).strip(), comp) if comp else str(row.get("team", "")).strip()
        if not team:
            continue
        key = normalize_team_key(team)
        if key and key in seen_keys:
            continue
        if key:
            seen_keys.add(key)
        if sim_runs is None and row.get("sim_runs") is not None:
            sim_runs = row.get("sim_runs")
        try:
            pct_f = float(row.get("win_league_pct") or 0)
        except (TypeError, ValueError):
            pct_f = 0.0
        entry = {
            "team": team,
            "win_league_pct": round(pct_f, 2),
            "top4_pct": row.get("top4_pct"),
            "bottom3_pct": row.get("bottom3_pct"),
            "most_likely_position": row.get("most_likely_position"),
            "most_likely_position_pct": row.get("most_likely_position_pct"),
        }
        if pct_f > 0:
            winner_probabilities[team] = round(pct_f, 2)
            winners_odds.append(entry)
            if pct_f > best_pct:
                best_pct = pct_f
                champion = team

    total = sum(winner_probabilities.values())
    if total > 0 and abs(total - 100.0) > 0.05:
        scale = 100.0 / total
        winner_probabilities = {
            team: round(pct * scale, 2) for team, pct in winner_probabilities.items()
        }
        drift = round(100.0 - sum(winner_probabilities.values()), 2)
        if champion and champion in winner_probabilities and drift:
            winner_probabilities[champion] = round(winner_probabilities[champion] + drift, 2)
        for entry in winners_odds:
            team = entry.get("team")
            if team in winner_probabilities:
                entry["win_league_pct"] = winner_probabilities[team]

    winners_odds.sort(key=lambda x: x.get("win_league_pct") or 0, reverse=True)
    payload: dict = {"winners_odds": winners_odds}
    if winner_probabilities:
        payload["winner_probabilities"] = winner_probabilities
    if champion:
        payload["champion"] = champion
    if sim_runs is not None:
        payload["simulations_run"] = sim_runs
    return payload


def _load_json_payload(path: str) -> object | None:
    """Safely load JSON payload from disk, returning None on failure."""
    if not path or not os.path.exists(path):
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    key = os.path.normpath(path)
    with _JSON_PAYLOAD_CACHE_LOCK:
        cached = _JSON_PAYLOAD_CACHE.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            payload = json.load(fh)
    except Exception:
        return None
    with _JSON_PAYLOAD_CACHE_LOCK:
        _JSON_PAYLOAD_CACHE[key] = (mtime, payload)
    return payload


def clear_json_payload_cache() -> None:
    """Drop memoized JSON payloads."""
    with _JSON_PAYLOAD_CACHE_LOCK:
        _JSON_PAYLOAD_CACHE.clear()


def _load_all_fixtures_by_competition(csv_path: str) -> dict[str, list[dict]]:
    """Return all fixtures grouped by competition from an upcoming CSV."""
    from .static_predictions import _load_upcoming_rows

    if not csv_path or not os.path.exists(csv_path):
        return {}
    try:
        rows, _, _ = _load_upcoming_rows(csv_path, date_range="all", fixtures_only=True)
    except Exception:
        return {}
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        comp = str(r.get("competition", "")).strip()
        if not comp:
            continue
        grouped.setdefault(comp, []).append(r)
    return grouped

