"""League tables and standings computation from live scores."""
import json
import os
import re
import threading
from datetime import datetime, timezone

import pandas as pd

import config
from competition_rules import (
    MLS_EASTERN_CONFERENCE_TEAMS,
    MLS_WESTERN_CONFERENCE_TEAMS,
    active_liga_mx_tournament_label,
    build_structured_standings_groups,
    canonical_team_name,
    classify_match_stage,
    collect_competition_games,
    competition_format_spec,
    cup_format,
    current_competition_phase,
    extract_group_label,
    filter_games_to_liga_mx_tournament,
    load_wc_team_groups,
    mls_conference,
    package_real_standings,
    resolve_competition_query,
    resolve_mls_team_name,
    uses_h2h_tiebreaker,
)
from espn_api import _fetch_leaders, _fetch_standings, LIVE_SCORE_FETCH_TIMEOUT
from team_utils import _to_int

_real_tables: dict[str, dict] = {}
_real_tables_lock = threading.Lock()

_real_leaders: dict[str, dict] = {}
_real_leaders_lock = threading.Lock()

LEADER_CATEGORY_LABELS = {
    "goals": "Goals",
    "assists": "Assists",
    "yellowCards": "Yellow Cards",
    "yellowCard": "Yellow Cards",
    "redCards": "Red Cards",
    "redCard": "Red Cards",
    "shotsOnGoal": "Shots on Goal",
    "shotsOnGoalPerGame": "Shots/Game",
    "passes": "Passes",
    "passAccuracy": "Pass Accuracy",
    "tackles": "Tackles",
    "interceptions": "Interceptions",
    "fouls": "Fouls",
    "offsides": "Offsides",
    "saves": "Saves",
    "cleanSheet": "Clean Sheets",
    "cleanSheets": "Clean Sheets",
    "minutesPlayed": "Minutes",
    "appearances": "Appearances",
    "gameStarted": "Starts",
    "gameWinningGoals": "GWG",
    "hatTricks": "Hat Tricks",
    "penaltyKickGoals": "PK Goals",
    "penaltyKickAttempts": "PK Att",
    "ownGoals": "Own Goals",
    "crosses": "Crosses",
    "corners": "Corners",
    "blocks": "Blocks",
    "clearances": "Clearances",
    "aerialsWon": "Aerials Won",
    "duelsWon": "Duels Won",
}

def _load_persisted_standings():
    """Load persisted standings from disk into ``_real_tables`` on startup."""
    if not os.path.exists(config.REAL_TABLES_PERSIST_FILE):
        return
    try:
        with open(config.REAL_TABLES_PERSIST_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            with _real_tables_lock:
                for comp_name, table in data.items():
                    if table is not None:
                        _real_tables[comp_name] = table
    except Exception:
        pass


# Warm the standings cache from disk on import so the API never starts cold.
_load_persisted_standings()


def _persist_real_tables():
    """Write the entire ``_real_tables`` cache to disk so it survives restarts."""
    try:
        with _real_tables_lock:
            data = dict(_real_tables)
        os.makedirs(os.path.dirname(config.REAL_TABLES_PERSIST_FILE), exist_ok=True)
        with open(config.REAL_TABLES_PERSIST_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _load_league_teams():
    """Load persisted league-team rosters from league_teams.json.

    Returns ``{competition_name: [team_name, ...]}`` or ``{}``.
    """
    if not os.path.exists(config.LEAGUE_TEAMS_FILE):
        return {}
    try:
        with open(config.LEAGUE_TEAMS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _load_live_score_history():
    if not os.path.exists(config.LIVE_SCORE_HISTORY_FILE):
        return []
    try:
        with open(config.LIVE_SCORE_HISTORY_FILE, "r") as f:
            games = json.load(f)
    except Exception:
        return []
    if not isinstance(games, list):
        return []
    filtered = []
    for game in games:
        if not isinstance(game, dict):
            continue
        match_id = str(game.get("match_id", "")).strip().lower()
        if match_id.startswith("test-") or "test-past-games" in match_id:
            continue
        filtered.append(game)
    return filtered


def _save_live_score_history(games):
    os.makedirs(os.path.dirname(config.LIVE_SCORE_HISTORY_FILE), exist_ok=True)
    with open(config.LIVE_SCORE_HISTORY_FILE, "w") as f:
        json.dump(games, f, indent=2)


BELGIAN_REGULAR_LIMIT = 30  # 16 teams × 2 rounds

_UEFA_COMPETITIONS = {
    "UEFA/Champions League", "UEFA/Europa League", "UEFA/Conference League",
    "Europe/Champions League", "Europe/Europa League", "Europe/Conference League",
}


def _competition_names_for_lookup(comp_name):
    """Return competition keys to scan when discovering teams for standings."""
    base_comp, _view = resolve_competition_query(comp_name)
    names = [comp_name]
    if base_comp and base_comp not in names:
        names.append(base_comp)
    return names


def _mls_conference(team_name):
    return mls_conference(team_name)

def _compute_standings_from_history(comp_name):
    """Compute league / group standings purely from completed live-score results.

    Handles:
      - single-table (standard league)
      - group-stage (World Cup, UCL groups)
      - MLS conferences (east / west split)
      - Belgian Pro League 2-phase detection (regular table until phase 2 starts)
      - Scottish Premiership split (top 6 / bottom 6 after 33 games)
      - UEFA league-phase format (single table, then knockout)
    Applies correct tiebreakers per league (GD-first vs H2H-first).
    Returns ``None`` if no completed games are available.
    """
    base_comp, mls_view = resolve_competition_query(comp_name)
    comp_games = collect_competition_games(comp_name)
    if not comp_games:
        return None

    def _finalize(groups, source="computed", current_phase=None):
        if mls_view and groups:
            view_map = {
                "east": "Eastern Conference",
                "west": "Western Conference",
                "shield": "Supporters Shield",
            }
            target = view_map.get(mls_view)
            if target:
                filtered = [g for g in groups if g.get("name") == target]
                if filtered:
                    groups = filtered
        response = package_real_standings(
            comp_name,
            groups,
            source,
            current_phase=current_phase,
        )
        with _real_tables_lock:
            _real_tables[comp_name] = response
        _persist_real_tables()
        return response

    fmt = cup_format(base_comp)
    team_to_group = load_wc_team_groups(comp_games) if base_comp == "FIFA/World Cup" else {}
    group_stage_games = [
        g for g in comp_games
        if classify_match_stage(g, base_comp, team_to_group) == "group"
    ]
    league_games = [
        g for g in comp_games
        if classify_match_stage(g, base_comp, team_to_group) == "league"
    ]
    has_groups = bool(group_stage_games) or any(
        "group" in str(g.get("round", "")).lower() for g in comp_games
    )
    if fmt and fmt.get("format") in {"group_stage_then_knockout", "league_phase_then_knockout"}:
        has_groups = bool(group_stage_games)

    def _extract_group(round_name):
        """Extract group label from ESPN round name e.g. 'Group Stage - Group A' → 'A'."""
        m = re.search(r'Group\s+([A-Z0-9]+)', round_name)
        if m:
            return m.group(1)
        parts = round_name.replace("Group Stage", "").replace("-", "").split()
        for p in parts:
            if p.strip():
                return p.strip()[:3]
        return ""

    def _h2h_scores(tied_teams, all_matches):
        """Head-to-head points, GD, GF among a set of tied teams."""
        scores = {t: {"pts": 0, "gd": 0, "gf": 0} for t in tied_teams}
        ts = set(tied_teams)
        for home, away, hg, ag in all_matches:
            if home in ts and away in ts:
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

    def _init_table(teams):
        table = {}
        for t in sorted(teams):
            table[t] = {"P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "GD": 0, "Pts": 0}
        return table

    def _apply_result(table, home, away, hg, ag):
        hs = table.setdefault(home, {"P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "GD": 0, "Pts": 0})
        at = table.setdefault(away, {"P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "GD": 0, "Pts": 0})
        hs["P"] += 1; at["P"] += 1
        hs["GF"] += int(hg); hs["GA"] += int(ag)
        at["GF"] += int(ag); at["GA"] += int(hg)
        hs["GD"] = hs["GF"] - hs["GA"]
        at["GD"] = at["GF"] - at["GA"]
        if hg > ag:
            hs["W"] += 1; at["L"] += 1; hs["Pts"] += 3
        elif ag > hg:
            at["W"] += 1; hs["L"] += 1; at["Pts"] += 3
        else:
            hs["D"] += 1; at["D"] += 1; hs["Pts"] += 1; at["Pts"] += 1

    def _rank_table(table, all_matches=None):
        use_h2h = uses_h2h_tiebreaker(comp_name) and all_matches is not None
        if not use_h2h:
            return sorted(table.items(), key=lambda kv: (-kv[1]["Pts"], -kv[1]["GD"], -kv[1]["GF"], kv[0]))
        from collections import defaultdict
        pts_groups = defaultdict(list)
        for team in table:
            pts_groups[table[team]["Pts"]].append(team)
        result = []
        for pts in sorted(pts_groups, reverse=True):
            tied = pts_groups[pts]
            if len(tied) == 1:
                result.append((tied[0], table[tied[0]]))
            else:
                h2h = _h2h_scores(tied, all_matches)
                sorted_tied = sorted(
                    tied,
                    key=lambda t: (
                        -h2h[t]["pts"], -h2h[t]["gd"], -h2h[t]["gf"],
                        -table[t]["GD"], -table[t]["GF"], t,
                    ),
                )
                for team in sorted_tied:
                    result.append((team, table[team]))
        return result

    # ── World Cup / group-stage cups ─────────────────────────────
    if base_comp == "FIFA/World Cup" or (
        fmt and fmt.get("format") == "group_stage_then_knockout" and group_stage_games
    ):
        groups_data = {}
        games_for_groups = group_stage_games or [
            g for g in comp_games if "group" in str(g.get("round", "")).lower()
        ]
        for g in games_for_groups:
            group = extract_group_label(g, team_to_group)
            if not group:
                continue
            groups_data.setdefault(group, []).append(g)

        groups = []
        for group_name in sorted(groups_data):
            games = groups_data[group_name]
            teams = set()
            for g in games:
                teams.add(str(g.get("home_team", "")))
                teams.add(str(g.get("away_team", "")))
            teams.discard("")
            if not teams:
                continue
            table = _init_table(teams)
            match_records = []
            for g in games:
                ht = str(g.get("home_team", ""))
                at = str(g.get("away_team", ""))
                hs = int(g.get("home_score", 0))
                as_ = int(g.get("away_score", 0))
                if ht and at:
                    _apply_result(table, ht, at, hs, as_)
                    match_records.append((ht, at, hs, as_))
            ranked = _rank_table(table, match_records if uses_h2h_tiebreaker(base_comp) else None)
            entries = [{"team": team, "rank": pos, **stats} for pos, (team, stats) in enumerate(ranked, 1)]
            groups.append({"name": f"Group {group_name}", "entries": entries})

        if groups:
            return _finalize(
                groups,
                source="computed",
                current_phase=current_competition_phase(comp_games, base_comp),
            )

        if base_comp == "FIFA/World Cup" or (
            fmt and fmt.get("format") == "group_stage_then_knockout"
        ):
            return _finalize(
                [],
                source="computed",
                current_phase=current_competition_phase(comp_games, base_comp),
            )

    # ── League-phase format (UCL/UEL/UECL) ──────────────────────
    is_league_phase = base_comp in _UEFA_COMPETITIONS and not has_groups
    is_mls = base_comp == "United States/MLS"
    is_belgian = "belgium" in comp_name.lower() or "belgian" in comp_name.lower()
    is_scottish = "scotland" in comp_name.lower() or "scottish" in comp_name.lower()
    is_liga_mx = base_comp == config.LIGA_MX_COMPETITION

    if is_league_phase:
        # UCL/UEL/UECL league phase: single table with all teams
        teams = set()
        match_records = []
        phase_games = group_stage_games or comp_games
        for g in phase_games:
            ht = str(g.get("home_team", ""))
            at = str(g.get("away_team", ""))
            if ht and at:
                teams.add(ht)
                teams.add(at)
        if not teams:
            return None
        table = _init_table(teams)
        for g in phase_games:
            ht = str(g.get("home_team", ""))
            at = str(g.get("away_team", ""))
            hs = int(g.get("home_score", 0))
            as_ = int(g.get("away_score", 0))
            if ht and at:
                _apply_result(table, ht, at, hs, as_)
                match_records.append((ht, at, hs, as_))
        ranked = _rank_table(table, match_records)
        entries = [{"team": team, "rank": pos, **stats} for pos, (team, stats) in enumerate(ranked, 1)]
        groups = [{"name": "League Phase", "entries": entries}]
        return _finalize(groups, source="computed")

    if has_groups:
        groups_data = {}
        group_games_map = {}
        games_for_groups = group_stage_games or comp_games
        for g in games_for_groups:
            if classify_match_stage(g, base_comp, team_to_group) == "knockout":
                continue
            group = extract_group_label(g, team_to_group) or _extract_group(str(g.get("round", "")))
            if not group:
                continue
            group_games_map.setdefault(group, []).append(g)

        for group_name in sorted(group_games_map):
            games = group_games_map[group_name]
            teams = set()
            for g in games:
                teams.add(str(g.get("home_team", "")))
                teams.add(str(g.get("away_team", "")))
            teams.discard("")
            if not teams:
                continue
            table = _init_table(teams)
            match_records = []
            for g in games:
                ht = str(g.get("home_team", ""))
                at = str(g.get("away_team", ""))
                hs = int(g.get("home_score", 0))
                as_ = int(g.get("away_score", 0))
                if ht and at:
                    _apply_result(table, ht, at, hs, as_)
                    match_records.append((ht, at, hs, as_))
            ranked = _rank_table(table, match_records if uses_h2h_tiebreaker(comp_name) else None)
            entries = []
            for pos, (team, stats) in enumerate(ranked, 1):
                entries.append({"team": team, "rank": pos, **stats})
            groups_data[group_name] = {"name": f"Group {group_name}", "entries": entries}

        if not groups_data:
            return None
        groups = [{"name": gd["name"], "entries": gd["entries"]} for _, gd in sorted(groups_data.items())]
        return _finalize(groups, source="computed")


    # ── MLS conference split ─────────────────────────────────
    if is_mls:
        teams = set()
        match_records = []
        for g in comp_games:
            ht = str(g.get("home_team", ""))
            at = str(g.get("away_team", ""))
            if ht and at:
                teams.add(ht)
                teams.add(at)
        if not teams:
            return None
        # Build full MLS table
        full_table = _init_table(teams)
        for g in comp_games:
            ht = str(g.get("home_team", ""))
            at = str(g.get("away_team", ""))
            hs = int(g.get("home_score", 0))
            as_ = int(g.get("away_score", 0))
            if ht and at:
                full_table[ht] = full_table.get(ht, _init_table([ht])[ht])
                full_table[at] = full_table.get(at, _init_table([at])[at])
                _apply_result(full_table, ht, at, hs, as_)
                match_records.append((ht, at, hs, as_))
        ranked = _rank_table(full_table, match_records)
        all_entries = [{"team": team, "rank": pos, **stats} for pos, (team, stats) in enumerate(ranked, 1)]

        # Conference tables use full-season stats (all 34 games), same as Supporters Shield.
        east_teams = {t for t in teams if _mls_conference(t) == "east"}
        west_teams = {t for t in teams if _mls_conference(t) == "west"}
        east_table = {team: full_table[team] for team in east_teams if team in full_table}
        west_table = {team: full_table[team] for team in west_teams if team in full_table}
        east_ranked = _rank_table(east_table, match_records)
        west_ranked = _rank_table(west_table, match_records)

        groups = [
            {"name": "Supporters Shield", "entries": all_entries},
            {"name": "Eastern Conference", "entries": [{"team": t, "rank": i+1, **s} for i, (t, s) in enumerate(east_ranked)]},
            {"name": "Western Conference", "entries": [{"team": t, "rank": i+1, **s} for i, (t, s) in enumerate(west_ranked)]},
        ]
        return _finalize(groups, source="computed")


    # ── Scottish Premiership split ───────────────────────────
    if is_scottish:
        teams = set()
        match_records = []
        phase_games = group_stage_games or comp_games
        for g in phase_games:
            ht = str(g.get("home_team", ""))
            at = str(g.get("away_team", ""))
            if ht and at:
                teams.add(ht)
                teams.add(at)
        if not teams:
            return None
        table = _init_table(teams)
        for g in phase_games:
            ht = str(g.get("home_team", ""))
            at = str(g.get("away_team", ""))
            hs = int(g.get("home_score", 0))
            as_ = int(g.get("away_score", 0))
            if ht and at:
                _apply_result(table, ht, at, hs, as_)
                match_records.append((ht, at, hs, as_))
        ranked = _rank_table(table, match_records if uses_h2h_tiebreaker(comp_name) else None)
        total_games = sum(1 for t, s in ranked if s["P"] > 0) and max(s["P"] for _, s in ranked) if ranked else 0
        # Split into top 6 / bottom 6 after 33 games
        if total_games >= 33:
            top6 = [t for t, s in ranked[:6]]
            bottom6 = [t for t, s in ranked[6:]]
            top_table = {t: table[t] for t in top6 if t in table}
            bottom_table = {t: table[t] for t in bottom6 if t in table}
            top_ranked = _rank_table(top_table)
            bottom_ranked = _rank_table(bottom_table)
            groups = [
                {"name": "Championship Group", "entries": [{"team": t, "rank": i+1, **s} for i, (t, s) in enumerate(top_ranked)]},
                {"name": "Relegation Group", "entries": [{"team": t, "rank": i+1, **s} for i, (t, s) in enumerate(bottom_ranked)]},
            ]
        else:
            entries = [{"team": team, "rank": pos, **stats} for pos, (team, stats) in enumerate(ranked, 1)]
            groups = [{"name": "Overall", "entries": entries}]

        return _finalize(groups, source="computed")
    if is_belgian:
        teams = set()
        match_records = []
        phase_games = group_stage_games or comp_games
        for g in phase_games:
            ht = str(g.get("home_team", ""))
            at = str(g.get("away_team", ""))
            if ht and at:
                teams.add(ht)
                teams.add(at)
        if not teams:
            return None
        table = _init_table(teams)
        for g in phase_games:
            ht = str(g.get("home_team", ""))
            at = str(g.get("away_team", ""))
            hs = int(g.get("home_score", 0))
            as_ = int(g.get("away_score", 0))
            if ht and at:
                _apply_result(table, ht, at, hs, as_)
                match_records.append((ht, at, hs, as_))
        ranked = _rank_table(table, match_records if uses_h2h_tiebreaker(comp_name) else None)
        max_gp = max(s["P"] for _, s in ranked) if ranked else 0
        entries = [{"team": team, "rank": pos, **stats} for pos, (team, stats) in enumerate(ranked, 1)]

        if max_gp >= BELGIAN_REGULAR_LIMIT:
            # Phase 2: split into championship / europe / relegation groups
            championship = entries[:6]
            europe = entries[6:12]
            relegation = entries[12:]
            groups = [
                {"name": "Championship Play-off", "entries": championship},
                {"name": "Europe Play-off", "entries": europe},
                {"name": "Relegation Play-off", "entries": relegation},
            ]
        else:
            groups = [{"name": "Regular Season", "entries": entries}]

        return _finalize(groups, source="computed")


    # ── Liga MX active tournament (Apertura / Clausura) ────────
    if is_liga_mx:
        tournament_label = active_liga_mx_tournament_label()
        tournament_games = filter_games_to_liga_mx_tournament(comp_games, tournament_label)
        if not tournament_games:
            return None
        teams = set()
        match_records = []
        for g in tournament_games:
            ht = str(g.get("home_team", ""))
            at = str(g.get("away_team", ""))
            if ht and at:
                teams.add(ht)
                teams.add(at)
        if not teams:
            return None
        table = _init_table(teams)
        for g in tournament_games:
            ht = str(g.get("home_team", ""))
            at = str(g.get("away_team", ""))
            hs = int(g.get("home_score", 0))
            as_ = int(g.get("away_score", 0))
            if ht and at:
                _apply_result(table, ht, at, hs, as_)
                match_records.append((ht, at, hs, as_))
        ranked = _rank_table(table, match_records if uses_h2h_tiebreaker(comp_name) else None)
        entries = [{"team": team, "rank": pos, **stats} for pos, (team, stats) in enumerate(ranked, 1)]
        groups = [{"name": tournament_label, "entries": entries}]
        return _finalize(groups, source="computed")


    # ── Standard single table ────────────────────────────────
    if fmt and fmt.get("format") == "group_stage_then_knockout":
        return None
    table_games = league_games or comp_games
    teams = set()
    match_records = []
    for g in table_games:
        ht = str(g.get("home_team", ""))
        at = str(g.get("away_team", ""))
        if ht and at:
            teams.add(ht)
            teams.add(at)
    if not teams:
        return None
    table = _init_table(teams)
    for g in table_games:
        ht = str(g.get("home_team", ""))
        at = str(g.get("away_team", ""))
        hs = int(g.get("home_score", 0))
        as_ = int(g.get("away_score", 0))
        if ht and at:
            _apply_result(table, ht, at, hs, as_)
            match_records.append((ht, at, hs, as_))
    ranked = _rank_table(table, match_records if uses_h2h_tiebreaker(comp_name) else None)
    entries = [{"team": team, "rank": pos, **stats} for pos, (team, stats) in enumerate(ranked, 1)]
    groups = [{"name": "Overall", "entries": entries}]
    return _finalize(groups, source="computed")


def _get_or_fetch_standings(comp_name, computed=True):
    """Return cached standings for *comp_name*.

    Defaults to *computed* (from ``live_score_history.json``).
    Set *computed* = ``False`` to fetch from ESPN instead (legacy).
    """
    if not computed:
        espn_id = config.LIVE_SCORE_COMPETITIONS.get(comp_name)
        if espn_id:
            now = datetime.now()
            with _real_tables_lock:
                cached = _real_tables.get(comp_name)
                if cached:
                    updated = cached.get("updated_at", "")
                    try:
                        age = (now - datetime.fromisoformat(updated)).total_seconds()
                    except Exception:
                        age = config.REAL_TABLES_CACHE_TTL + 1
                    if age < config.REAL_TABLES_CACHE_TTL:
                        return cached
            data = _fetch_standings(comp_name, espn_id)
            if data:
                with _real_tables_lock:
                    _real_tables[comp_name] = data
                _persist_real_tables()
            return data
    return _compute_standings_from_history(comp_name)


def _clear_standings_cache(comp_name):
    """Force next fetch of *comp_name* standings to hit ESPN."""
    with _real_tables_lock:
        _real_tables.pop(comp_name, None)

def _get_or_fetch_leaders(comp_name):
    """Return cached leaders for *comp_name*, or fetch+store if stale/missing."""
    espn_id = config.LIVE_SCORE_COMPETITIONS.get(comp_name)
    if not espn_id:
        return None
    now = datetime.now()
    with _real_leaders_lock:
        cached = _real_leaders.get(comp_name)
        if cached:
            updated = cached.get("updated_at", "")
            try:
                age = (now - datetime.fromisoformat(updated)).total_seconds()
            except Exception:
                age = config.REAL_LEADERS_CACHE_TTL + 1
            if age < config.REAL_LEADERS_CACHE_TTL:
                return cached
    data = _fetch_leaders(comp_name, espn_id)
    if data:
        with _real_leaders_lock:
            _real_leaders[comp_name] = data
    return data


def _clear_leaders_cache(comp_name):
    """Force next fetch of *comp_name* leaders to hit ESPN."""
    with _real_leaders_lock:
        _real_leaders.pop(comp_name, None)

def _fill_placeholder_tables(data):
    """Fill ``data["tables"]`` with zero-stat team entries from league_teams.json
    for any competition in ``data["leagues"]`` that has no projected table data.

    Mutates ``data["tables"]`` in place.
    """
    league_teams = _load_league_teams()
    tables = data.get("tables", {})
    leagues = data.get("leagues", [])
    now_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for comp_name in leagues:
        if comp_name in tables and tables[comp_name]:
            continue
        roster = league_teams.get(comp_name)
        if not roster:
            continue
        entries = []
        for pos, team in enumerate(sorted(roster), start=1):
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
    data["tables"] = tables

def _build_fallback_standings(comp_name):
    """Return a placeholder standings dict with all known teams on 0 points.

    Reads every upcoming prediction CSV source (including ``Output/`` dirs)
    and projected table CSVs for *comp_name* and returns teams sorted
    alphabetically.  If no teams are found, returns ``None``.

    Does NOT call ESPN API — teams are discovered from local CSV data only.

    Response shape (matches ``_compute_standings_from_history``):
    ``{"competition": str, "groups": [{"name": "...", "entries": [...]}, ...]}``
    where group names follow each competition's real table rules (MLS conferences,
    Belgian regular season, UEFA league phase, World Cup groups, etc.).
    """
    base_comp, _view = resolve_competition_query(comp_name)
    lookup_names = _competition_names_for_lookup(comp_name)
    teams = set()

    # 1. Persisted league-team rosters (offseason fallback from fetch_league_teams.py)
    league_teams = _load_league_teams()
    for lookup_name in lookup_names:
        cached = league_teams.get(lookup_name)
        if cached:
            teams.update(cached)

    # 2. Upcoming prediction CSVs and projected table CSVs
    csv_sources = [
        config.GLOBAL_UPCOMING_FILE,
        config.MLS_UPCOMING_FILE,
        config.EXTRA_UPCOMING_FILE,
        config.CUP_UPCOMING_FILE,
        config.NATIONAL_UPCOMING_FILE,
        os.path.join(config.PROJECT_DIR, "Output", "Upcoming", "all_upcoming.csv"),
        os.path.join(config.PROJECT_DIR, "Output", "Europe", "Upcoming", "europe_upcoming.csv"),
        os.path.join(config.PROJECT_DIR, "Output", "National", "Upcoming", "national_upcoming.csv"),
        config.GLOBAL_PROJECTED_TABLE_FILE,
        config.MLS_PROJECTED_TABLE_FILE,
        config.EXTRA_PROJECTED_TABLE_FILE,
        config.CUP_PROJECTED_TABLE_FILE,
    ]
    for path in csv_sources:
        if not os.path.exists(path):
            continue
        try:
            df = pd.read_csv(path, dtype=str, encoding="utf-8")
        except Exception:
            continue
        if "competition" not in df.columns:
            continue
        mask = df["competition"].astype(str).str.strip().isin(lookup_names)
        sub = df[mask]
        if sub.empty:
            continue
        if "team" in sub.columns:
            teams.update(sub["team"].dropna().astype(str).str.strip())
        elif "home_team" in sub.columns:
            teams.update(sub["home_team"].dropna().astype(str).str.strip())
        if "away_team" in sub.columns:
            teams.update(sub["away_team"].dropna().astype(str).str.strip())

    if base_comp == "United States/MLS":
        teams = {resolve_mls_team_name(team) for team in teams if resolve_mls_team_name(team)}

    if not teams:
        return None

    groups = build_structured_standings_groups(comp_name, sorted(teams))
    if not groups:
        return None
    return package_real_standings(comp_name, groups, "placeholder")
