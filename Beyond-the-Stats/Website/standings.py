"""League tables and standings computation from live scores."""
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:
    fcntl = None

def _acquire_file_lock(lock_handle):
    if fcntl:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)

def _release_file_lock(lock_handle):
    if fcntl:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

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
    filter_games_to_active_season,
    load_wc_team_groups,
    mls_conference,
    normalize_team_key,
    package_real_standings,
    resolve_competition_query,
    resolve_mls_team_name,
    uses_h2h_tiebreaker,
)
from espn_api import _fetch_leaders, _fetch_standings, LIVE_SCORE_FETCH_TIMEOUT
from team_utils import _to_int

_real_tables: dict[str, dict] = {}
_team_display_mapping: dict[str, dict[str, str]] | None = None
_team_display_mapping_lock = threading.Lock()


def _load_team_display_mapping() -> dict[str, dict[str, str]]:
    global _team_display_mapping
    if _team_display_mapping is not None:
        return _team_display_mapping
    with _team_display_mapping_lock:
        if _team_display_mapping is not None:
            return _team_display_mapping
        path = config.TEAM_NAME_DISPLAY_MAPPING_FILE
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    _team_display_mapping = json.load(f)
            except Exception:
                _team_display_mapping = {}
        else:
            _team_display_mapping = {}
        return _team_display_mapping


def _normalize_team_name(name: str, competition: str) -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    # Prefer shared canonical resolver (key-aware mapping aliases).
    try:
        mapped = canonical_team_name(text, competition)
        if mapped:
            return mapped
    except Exception:
        pass
    mapping = _load_team_display_mapping()
    lower = text.lower()
    comp_map = mapping.get(competition, {})
    for raw, canon in comp_map.items():
        if str(raw).lower().strip() == lower and str(canon).strip():
            return str(canon).strip()
    for comp_entries in mapping.values():
        if not isinstance(comp_entries, dict):
            continue
        for raw, canon in comp_entries.items():
            if str(raw).lower().strip() == lower and str(canon).strip():
                return str(canon).strip()
    return text


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


_espn_roster_cache: dict[str, list[str]] = {}


def _fetch_espn_roster_for_competition(comp_name: str) -> list[str]:
    """Best-effort roster fetch from ESPN when local league_teams.json is missing."""
    if comp_name in _espn_roster_cache:
        return _espn_roster_cache[comp_name]
    espn_id = config.LIVE_SCORE_COMPETITIONS.get(comp_name)
    if not espn_id:
        _espn_roster_cache[comp_name] = []
        return []
    try:
        import urllib.request

        url = f"{config.LIVE_SCORE_ESPN_BASE}/{espn_id}/teams"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            payload = json.load(resp)
        teams: list[str] = []
        for league in (payload.get("sports") or [{}])[0].get("leagues") or []:
            for team_entry in league.get("teams") or []:
                entry = team_entry.get("team", team_entry)
                name = str(entry.get("displayName", "")).strip()
                if name:
                    teams.append(name)
        teams = sorted(set(teams))
        _espn_roster_cache[comp_name] = teams
        return teams
    except Exception:
        _espn_roster_cache[comp_name] = []
        return []


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


_live_history_write_lock = threading.Lock()


def _live_history_cutoff(as_of=None):
    """Start of the previous full week (Monday), in Eastern time."""
    today = as_of or datetime.now(ZoneInfo("America/New_York")).date()
    current_week_start = today - timedelta(days=today.weekday())
    return current_week_start - timedelta(days=7)


def _live_history_game_date(game):
    for field in ("kickoff_utc", "match_datetime_utc", "completed_at", "match_date_iso", "match_date"):
        raw = str(game.get(field, "") or "").strip()
        if not raw:
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            try:
                return datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                continue
        try:
            parsed = pd.to_datetime(raw, utc=True, errors="coerce")
            if pd.notna(parsed):
                return parsed.tz_convert("America/New_York").date()
        except Exception:
            continue
    return None


def _live_history_game_key(game):
    match_id = str(game.get("match_id", "") or "").strip()
    if match_id:
        return f"id:{match_id}"
    game_date = _live_history_game_date(game)
    competition = str(game.get("competition", "") or "").strip().lower()
    home = str(game.get("home_team", "") or "").strip().lower()
    away = str(game.get("away_team", "") or "").strip().lower()
    if game_date and home and away:
        return f"fixture:{game_date.isoformat()}|{competition}|{home}|{away}"
    return ""


def _read_live_history_strict():
    if not os.path.exists(config.LIVE_SCORE_HISTORY_FILE):
        return []
    with open(config.LIVE_SCORE_HISTORY_FILE, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError("live score history must be a JSON list")
    return [row for row in payload if isinstance(row, dict)]


def _atomic_write_live_history(games):
    directory = os.path.dirname(config.LIVE_SCORE_HISTORY_FILE)
    os.makedirs(directory, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=".live-score-history-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(games, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, config.LIVE_SCORE_HISTORY_FILE)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _upsert_live_score_history(games, as_of=None):
    """Append/update history without allowing an empty poll to erase the file.

    Writes are process- and cross-process locked, atomic, deduplicated, and
    retain the current week plus the previous full week. Rows with no parseable
    date are kept rather than silently discarded.
    """
    incoming = [dict(game) for game in (games or []) if isinstance(game, dict)]
    if not incoming:
        return {"inserted": 0, "updated": 0, "pruned": 0, "total": len(_load_live_score_history())}

    lock_path = f"{config.LIVE_SCORE_HISTORY_FILE}.lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with _live_history_write_lock:
        with open(lock_path, "a+", encoding="utf-8") as lock_handle:
            _acquire_file_lock(lock_handle)
            try:
                existing = _read_live_history_strict()
                by_key = {}
                keyless = []
                for row in existing:
                    key = _live_history_game_key(row)
                    if key:
                        by_key[key] = row
                    else:
                        keyless.append(row)

                inserted = 0
                updated = 0
                for row in incoming:
                    key = _live_history_game_key(row)
                    if not key:
                        keyless.append(row)
                        inserted += 1
                        continue
                    if key in by_key:
                        merged = dict(by_key[key])
                        merged.update({k: v for k, v in row.items() if v not in (None, "")})
                        by_key[key] = merged
                        updated += 1
                    else:
                        by_key[key] = row
                        inserted += 1

                cutoff = _live_history_cutoff(as_of)
                combined = list(by_key.values()) + keyless
                retained = [
                    row for row in combined
                    if _live_history_game_date(row) is None or _live_history_game_date(row) >= cutoff
                ]
                pruned = len(combined) - len(retained)
                retained.sort(
                    key=lambda row: str(
                        row.get("kickoff_utc")
                        or row.get("match_datetime_utc")
                        or row.get("completed_at")
                        or ""
                    ),
                    reverse=True,
                )
                _atomic_write_live_history(retained)
                return {
                    "inserted": inserted,
                    "updated": updated,
                    "pruned": pruned,
                    "total": len(retained),
                }
            finally:
                _release_file_lock(lock_handle)


def _save_live_score_history(games):
    """Compatibility wrapper: merge supplied rows instead of replacing history."""
    return _upsert_live_score_history(games)


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
    # Only count games from the active season window (drops prior May finales
    # during Jul–Aug preseason when no new-season CSV exists yet).
    comp_games = filter_games_to_active_season(
        collect_competition_games(comp_name),
        base_comp,
    )
    if base_comp == config.LIGA_MX_COMPETITION or base_comp.startswith("Mexico/"):
        # Liga MX already has tournament filtering; re-apply after season filter.
        try:
            comp_games = filter_games_to_liga_mx_tournament(comp_games, base_comp) or comp_games
        except Exception:
            pass
    if not comp_games:
        return None

    for g in comp_games:
        raw_ht = str(g.get("home_team", ""))
        raw_at = str(g.get("away_team", ""))
        g["home_team"] = _normalize_team_name(raw_ht, base_comp)
        g["away_team"] = _normalize_team_name(raw_at, base_comp)

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
            fallback = _build_fallback_standings(comp_name)
            if fallback is not None:
                with _real_tables_lock:
                    _real_tables[comp_name] = fallback
                _persist_real_tables()
                return fallback
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


def _clear_all_real_data_caches():
    """Drop all in-memory ESPN standings/leaders caches (e.g. after pipeline refresh)."""
    with _real_tables_lock:
        _real_tables.clear()
    with _real_leaders_lock:
        _real_leaders.clear()

def _fill_placeholder_tables(data):
    """Fill ``data["tables"]`` with zero-stat team entries from league_teams.json
    for any competition in ``data["leagues"]`` that has no projected table data.

    Falls back to team discovery from local prediction/projected CSVs when the
    persisted roster file is missing (common for Liga MX early in the season).

    Mutates ``data["tables"]`` in place.
    """
    league_teams = _load_league_teams()
    tables = data.get("tables", {})
    leagues = data.get("leagues", [])
    for comp_name in leagues:
        if comp_name in tables and tables[comp_name]:
            continue
        roster = league_teams.get(comp_name)
        if not roster:
            roster = _fetch_espn_roster_for_competition(comp_name)
        if roster:
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
            continue

        fallback = _build_fallback_standings(comp_name)
        if not fallback or not fallback.get("groups"):
            continue
        entries = []
        for group in fallback["groups"]:
            for entry in group.get("entries", []):
                team = str(entry.get("team", "")).strip()
                if not team:
                    continue
                rank = entry.get("rank") or entry.get("position") or (len(entries) + 1)
                entries.append({
                    "position": rank,
                    "team": team,
                    "P": int(entry.get("P") or 0),
                    "W": int(entry.get("W") or 0),
                    "D": int(entry.get("D") or 0),
                    "L": int(entry.get("L") or 0),
                    "GF": int(entry.get("GF") or 0),
                    "GA": int(entry.get("GA") or 0),
                    "GD": int(entry.get("GD") or 0),
                    "Pts": int(entry.get("Pts") or 0),
                    "PlayedReal": 0,
                    "PlayedPred": 0,
                    "win_league_pct": 0.0,
                    "top4_pct": 0.0,
                    "bottom3_pct": 0.0,
                    "most_likely_position": rank,
                    "most_likely_position_pct": 0.0,
                    "position_odds": {},
                    "sim_runs": 0,
                })
        if entries:
            entries.sort(key=lambda item: item.get("position") or 999)
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

    # 1. Prefer current-season roster, then broader league_teams.json
    try:
        if os.path.exists(config.CURRENT_SEASON_TEAMS_FILE):
            with open(config.CURRENT_SEASON_TEAMS_FILE, "r", encoding="utf-8") as fh:
                current = json.load(fh)
            if isinstance(current, dict):
                for lookup_name in lookup_names:
                    cached = current.get(lookup_name)
                    if cached:
                        teams.update(cached)
    except Exception:
        pass
    league_teams = _load_league_teams()
    if not teams:
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

    # Normalize all team names through the mapping file to prevent duplicates
    normalized = set()
    for team in teams:
        normed = _normalize_team_name(team, base_comp)
        if normed:
            normalized.add(normed)
    if base_comp == "United States/MLS":
        normalized = {resolve_mls_team_name(team) for team in normalized if resolve_mls_team_name(team)}
    teams = normalized

    if not teams:
        return None

    groups = build_structured_standings_groups(comp_name, sorted(teams))
    if not groups:
        return None
    return package_real_standings(comp_name, groups, "placeholder")
