"""League tables and standings computation from live scores."""
import json
import os
import threading
from datetime import datetime, timezone

import pandas as pd

import config
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
            return json.load(f)
    except Exception:
        return []


def _save_live_score_history(games):
    os.makedirs(os.path.dirname(config.LIVE_SCORE_HISTORY_FILE), exist_ok=True)
    with open(config.LIVE_SCORE_HISTORY_FILE, "w") as f:
        json.dump(games, f, indent=2)


MLS_EASTERN_CONFERENCE_TEAMS = frozenset({
    "Atlanta Utd", "CF Montreal", "Charlotte", "Chicago Fire", "Columbus Crew",
    "DC United", "FC Cincinnati", "Inter Miami", "Nashville SC", "New England Revolution",
    "New York City", "New York Red Bulls", "Orlando City", "Philadelphia Union", "Toronto FC",
})

MLS_WESTERN_CONFERENCE_TEAMS = frozenset({
    "Austin FC", "Colorado Rapids", "FC Dallas", "Houston Dynamo", "Los Angeles Galaxy",
    "Los Angeles FC", "Minnesota United", "Portland Timbers", "Real Salt Lake", "San Diego FC",
    "San Jose Earthquakes", "Seattle Sounders", "Sporting Kansas City", "St. Louis City",
    "Vancouver Whitecaps",
})

BELGIAN_REGULAR_LIMIT = 30  # 16 teams × 2 rounds

_UEFA_COMPETITIONS = {
    "UEFA/Champions League", "UEFA/Europa League", "UEFA/Conference League",
    "Europe/Champions League", "Europe/Europa League", "Europe/Conference League",
}


def _mls_conference(team_name):
    n = str(team_name).replace(" FC", "").replace("United", "United").strip().lower()
    for t in MLS_EASTERN_CONFERENCE_TEAMS:
        if t.lower() in n or n in t.lower():
            return "east"
    for t in MLS_WESTERN_CONFERENCE_TEAMS:
        if t.lower() in n or n in t.lower():
            return "west"
    return None

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
    history = _load_live_score_history()
    # Also include any post games still in _live_scores (not yet merged)
    from live_poller import _live_scores, _live_scores_lock
    with _live_scores_lock:
        for comp_data in _live_scores.values():
            for g in comp_data.get("games", []):
                if g.get("status") == "post":
                    entry = dict(g)
                    entry.setdefault("competition", next(
                        (k for k, v in _live_scores.items() if v is comp_data), comp_name,
                    ))
                    history.append(entry)

    comp_games = [g for g in history
                  if g.get("competition") == comp_name and g.get("status") == "post"
                  and g.get("home_score") is not None and g.get("away_score") is not None]
    if not comp_games:
        return None

    def _finalize(response):
        with _real_tables_lock:
            _real_tables[comp_name] = response
        _persist_real_tables()
        return response

    # Detect group format: any game with "group" in round name
    has_groups = any("group" in str(g.get("round", "")).lower() for g in comp_games)

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
        use_h2h = comp_name in config.H2H_LEAGUES and all_matches is not None
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

    # ── League-phase format (UCL/UEL/UECL) ──────────────────────
    is_league_phase = comp_name in _UEFA_COMPETITIONS and not has_groups
    is_mls = comp_name == "United States/MLS"
    is_belgian = "belgium" in comp_name.lower() or "belgian" in comp_name.lower()
    is_scottish = "scotland" in comp_name.lower() or "scottish" in comp_name.lower()

    if is_league_phase:
        # UCL/UEL/UECL league phase: single table with all teams
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
        table = _init_table(teams)
        for g in comp_games:
            ht = str(g.get("home_team", ""))
            at = str(g.get("away_team", ""))
            hs = int(g.get("home_score", 0))
            as_ = int(g.get("away_score", 0))
            if ht and at:
                _apply_result(table, ht, at, hs, as_)
                match_records.append((ht, at, hs, as_))
        ranked = _rank_table(table, match_records)
        entries = [{"team": team, "rank": pos, **stats} for pos, (team, stats) in enumerate(ranked, 1)]
        from knockout import _build_knockout_framework
        knockout_rounds = _build_knockout_framework(comp_name)
        groups = [{"name": "League Phase", "entries": entries}]
        response = {
            "competition": comp_name,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "groups": groups,
            "source": "computed",
        }
        if knockout_rounds:
            response["knockout_rounds"] = knockout_rounds
        return _finalize(response)

    if has_groups:
        groups_data = {}
        group_games_map = {}
        for g in comp_games:
            group = _extract_group(str(g.get("round", "")))
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
            ranked = _rank_table(table, match_records if comp_name in config.H2H_LEAGUES else None)
            entries = []
            for pos, (team, stats) in enumerate(ranked, 1):
                entries.append({"team": team, "rank": pos, **stats})
            groups_data[group_name] = {"name": group_name, "entries": entries}

        if not groups_data:
            return None
        groups = [{"name": gn, "entries": gd["entries"]} for gn, gd in sorted(groups_data.items())]

        # Add knockout framework for UEFA group-stage competitions
        response = {
            "competition": comp_name,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "groups": groups,
            "source": "computed",
        }
        if comp_name in _UEFA_COMPETITIONS:
            from knockout import _build_knockout_framework
            ko = _build_knockout_framework(comp_name)
            if ko:
                response["knockout_rounds"] = ko
        return _finalize(response)


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

        # Split into conferences
        east_teams = [t for t in teams if _mls_conference(t) == "east"]
        west_teams = [t for t in teams if _mls_conference(t) == "west"]
        east_table = _init_table(east_teams)
        west_table = _init_table(west_teams)
        for g in comp_games:
            ht = str(g.get("home_team", ""))
            at = str(g.get("away_team", ""))
            hs = int(g.get("home_score", 0))
            as_ = int(g.get("away_score", 0))
            if ht in east_teams and at in east_teams:
                _apply_result(east_table, ht, at, hs, as_)
            if ht in west_teams and at in west_teams:
                _apply_result(west_table, ht, at, hs, as_)

        east_ranked = _rank_table(east_table)
        west_ranked = _rank_table(west_table)

        groups = [
            {"name": "Supporters Shield", "entries": all_entries},
            {"name": "Eastern Conference", "entries": [{"team": t, "rank": i+1, **s} for i, (t, s) in enumerate(east_ranked)]},
            {"name": "Western Conference", "entries": [{"team": t, "rank": i+1, **s} for i, (t, s) in enumerate(west_ranked)]},
        ]
        response = {
            "competition": comp_name,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "groups": groups,
            "source": "computed",
        }
        return _finalize(response)


    # ── Scottish Premiership split ───────────────────────────
    if is_scottish:
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
        table = _init_table(teams)
        for g in comp_games:
            ht = str(g.get("home_team", ""))
            at = str(g.get("away_team", ""))
            hs = int(g.get("home_score", 0))
            as_ = int(g.get("away_score", 0))
            if ht and at:
                _apply_result(table, ht, at, hs, as_)
                match_records.append((ht, at, hs, as_))
        ranked = _rank_table(table, match_records if comp_name in config.H2H_LEAGUES else None)
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

        response = {
            "competition": comp_name,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "groups": groups,
            "source": "computed",
        }
        return _finalize(response)


    # ── Belgian Pro League (2-phase detection) ───────────────
    if is_belgian:
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
        table = _init_table(teams)
        for g in comp_games:
            ht = str(g.get("home_team", ""))
            at = str(g.get("away_team", ""))
            hs = int(g.get("home_score", 0))
            as_ = int(g.get("away_score", 0))
            if ht and at:
                _apply_result(table, ht, at, hs, as_)
                match_records.append((ht, at, hs, as_))
        ranked = _rank_table(table, match_records if comp_name in config.H2H_LEAGUES else None)
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

        response = {
            "competition": comp_name,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "groups": groups,
            "source": "computed",
        }
        return _finalize(response)


    # ── Standard single table ────────────────────────────────
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
    table = _init_table(teams)
    for g in comp_games:
        ht = str(g.get("home_team", ""))
        at = str(g.get("away_team", ""))
        hs = int(g.get("home_score", 0))
        as_ = int(g.get("away_score", 0))
        if ht and at:
            _apply_result(table, ht, at, hs, as_)
            match_records.append((ht, at, hs, as_))
    ranked = _rank_table(table, match_records if comp_name in config.H2H_LEAGUES else None)
    entries = [{"team": team, "rank": pos, **stats} for pos, (team, stats) in enumerate(ranked, 1)]
    groups = [{"name": "Overall", "entries": entries}]

    response = {
        "competition": comp_name,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "groups": groups,
        "source": "computed",
    }
    return _finalize(response)


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
    ``{"competition": str, "groups": [{"name": "Overall", "entries": [...]}]}``
    """
    teams = set()

    # 1. Persisted league-team rosters (offseason fallback from fetch_league_teams.py)
    league_teams = _load_league_teams()
    cached = league_teams.get(comp_name)
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
        mask = df["competition"].astype(str).str.strip() == comp_name
        sub = df[mask]
        if sub.empty:
            continue
        if "team" in sub.columns:
            teams.update(sub["team"].dropna().astype(str).str.strip())
        elif "home_team" in sub.columns:
            teams.update(sub["home_team"].dropna().astype(str).str.strip())
        if "away_team" in sub.columns:
            teams.update(sub["away_team"].dropna().astype(str).str.strip())

    if not teams:
        return None

    now_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    entries = []
    for pos, team in enumerate(sorted(teams), start=1):
        entries.append({
            "position": pos, "team": team,
            "P": 0, "W": 0, "D": 0, "L": 0,
            "GF": 0, "GA": 0, "GD": 0, "Pts": 0,
        })
    return {
        "competition": comp_name,
        "updated_at": now_utc,
        "groups": [{"name": "Overall", "entries": entries}],
        "source": "placeholder",
    }
