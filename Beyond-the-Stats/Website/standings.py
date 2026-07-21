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
        if mapped and mapped != text:
            return mapped
        if mapped:
            text = mapped
    except Exception:
        pass
    mapping = _load_team_display_mapping()
    lower = text.lower()
    base_comp = competition
    try:
        from competition_rules import resolve_competition_query
        base_comp, _view = resolve_competition_query(competition)
    except Exception:
        pass
    # Competition-scoped lookup first (and Leagues Cup sibling maps).
    maps_to_try = []
    for key in (competition, base_comp):
        comp_map = mapping.get(key) if key else None
        if isinstance(comp_map, dict) and comp_map not in maps_to_try:
            maps_to_try.append(comp_map)
    if base_comp == "CONCACAF/Leagues Cup":
        for sibling in ("United States/MLS", "Mexico/Liga MX"):
            sibling_map = mapping.get(sibling)
            if isinstance(sibling_map, dict) and sibling_map not in maps_to_try:
                maps_to_try.append(sibling_map)
    for comp_map in maps_to_try:
        for raw, canon in comp_map.items():
            if str(raw).lower().strip() == lower and str(canon).strip():
                return str(canon).strip()
    # Do NOT fall through to unrelated competitions for short / ambiguous
    # names like "Inter" (Serie A) vs "Inter Miami" (MLS / Leagues Cup).
    if lower in {"inter", "miami"} and base_comp in {
        "CONCACAF/Leagues Cup", "United States/MLS",
    }:
        return "Inter Miami"
    if len(lower) <= 5:
        return text
    for comp_key, comp_entries in mapping.items():
        if not isinstance(comp_entries, dict):
            continue
        if comp_key in {competition, base_comp}:
            continue
        # Skip cross-country collisions for CONCACAF/MLS contexts.
        if base_comp.startswith(("CONCACAF/", "United States/", "Mexico/")) and not str(comp_key).startswith(
            ("CONCACAF/", "United States/", "Mexico/")
        ):
            continue
        for raw, canon in comp_entries.items():
            if str(raw).lower().strip() == lower and str(canon).strip():
                return str(canon).strip()
    return text


def _filter_games_to_known_roster(games: list, competition: str) -> list:
    """Keep only matches where both sides resolve into the competition roster.

    Used for Denmark/Danish Superliga where historical football-data ``DK1``
    feeds sometimes mixed in German Bundesliga clubs.
    """
    if not games:
        return []
    roster_keys: set[str] = set()
    try:
        league_teams = _load_league_teams()
        for team in league_teams.get(competition) or []:
            k = normalize_team_key(_normalize_team_name(str(team), competition) or team)
            if k:
                roster_keys.add(k)
    except Exception:
        pass
    try:
        if os.path.exists(config.CURRENT_SEASON_TEAMS_FILE):
            with open(config.CURRENT_SEASON_TEAMS_FILE, "r", encoding="utf-8") as fh:
                current = json.load(fh)
            for team in (current.get(competition) or []):
                k = normalize_team_key(_normalize_team_name(str(team), competition) or team)
                if k:
                    roster_keys.add(k)
    except Exception:
        pass
    # Always include football-data short names from the mapping file.
    try:
        mapping = _load_team_display_mapping().get(competition) or {}
        for raw, canon in mapping.items():
            for candidate in (raw, canon):
                k = normalize_team_key(candidate)
                if k:
                    roster_keys.add(k)
    except Exception:
        pass
    if not roster_keys:
        return list(games)

    german_block = {
        "bayern", "dortmund", "leverkusen", "stuttgart", "frankfurt", "wolfsburg",
        "gladbach", "leipzig", "hoffenheim", "augsburg", "heidenheim", "bochum",
        "mainz", "unionberlin", "koln", "koeln", "werder", "freiburg", "stpauli",
        "hamburger", "schalke",
    }

    def _ok(team: str) -> bool:
        canon = _normalize_team_name(team, competition) or team
        key = normalize_team_key(canon)
        if not key:
            return False
        if any(g in key for g in german_block):
            return False
        if key in roster_keys:
            return True
        # Allow close roster containment for accent/spelling variants.
        return any(key in rk or rk in key for rk in roster_keys if len(rk) >= 4 and len(key) >= 4)

    out = []
    for game in games:
        ht = str(game.get("home_team", "")).strip()
        at = str(game.get("away_team", "")).strip()
        if ht and at and _ok(ht) and _ok(at):
            out.append(game)
    return out


def _dedupe_standings_groups(standings: dict, comp_name: str) -> dict:
    """Collapse duplicate team rows that survived incomplete name mapping.

    Uses the same football-data canonicals as predicted tables so
    ``Manchester City`` / ``Man City`` (etc.) become one row.
    """
    if not standings or not isinstance(standings.get("groups"), list):
        return standings
    base_comp, _view = resolve_competition_query(comp_name)
    new_groups = []
    for group in standings["groups"]:
        entries = group.get("entries") or []
        by_key: dict[str, dict] = {}
        order: list[str] = []
        for entry in entries:
            team = str(entry.get("team", "")).strip()
            if not team:
                continue
            canon = _normalize_team_name(team, base_comp)
            key = canon.lower()
            if key not in by_key:
                cloned = dict(entry)
                cloned["team"] = canon
                by_key[key] = cloned
                order.append(key)
            else:
                existing = by_key[key]
                try:
                    if int(entry.get("P") or 0) > int(existing.get("P") or 0):
                        cloned = dict(entry)
                        cloned["team"] = canon
                        by_key[key] = cloned
                except Exception:
                    pass
        deduped = [by_key[k] for k in order]
        for idx, row in enumerate(deduped, start=1):
            row["rank"] = idx
            row["position"] = idx
        new_groups.append({**group, "entries": deduped})
    out = dict(standings)
    out["groups"] = new_groups
    return out


def _slugify_competition_for_output(competition: str) -> str:
    """Match Daily_Pipeline slugify: England/Premier League → england_premier_league."""
    out = (competition or "").strip().lower()
    out = out.replace("/", "_").replace(" ", "_").replace("-", "_")
    out = out.replace(".", "").replace(",", "").replace("'", "")
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_") or "unknown"


def _teams_from_predicted_table(comp_name: str) -> list[str]:
    """Team names exactly as shown on the predictions tab (source of truth)."""
    base_comp, _view = resolve_competition_query(comp_name)
    lookup_names = [comp_name, base_comp]
    # 1) Projected CSV rows used by /api/league-data predicted tables.
    try:
        from predictions import _load_projected_competition_table

        for name in lookup_names:
            if not name:
                continue
            rows = _load_projected_competition_table(name) or []
            teams = [str(r.get("team", "")).strip() for r in rows if str(r.get("team", "")).strip()]
            if teams:
                return teams
    except Exception:
        pass
    # 2) Published LeagueResult JSON (same teams the site ships after pipeline).
    slug_names = []
    for name in lookup_names:
        if name:
            slug_names.append(_slugify_competition_for_output(name))
    for region in ("Europe", "Other", "National"):
        for slug in slug_names:
            path = os.path.join(config.PROJECT_DIR, "Output", region, "LeagueResult", f"{slug}.json")
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                teams_raw = payload.get("teams") if isinstance(payload, dict) else None
                if not isinstance(teams_raw, list):
                    continue
                teams = []
                for row in teams_raw:
                    if isinstance(row, dict):
                        t = str(row.get("team", "")).strip()
                    else:
                        t = str(row or "").strip()
                    if t:
                        teams.append(t)
                if teams:
                    return teams
            except Exception:
                continue
    return []


def _canonical_roster_teams(comp_name: str) -> list[str]:
    """Return the team set real tables must share with predicted tables.

    Order of preference:
      1. Predicted/projected table teams (identical names & count as predictions tab)
      2. ``current_season_teams.json``
      3. ``league_teams.json``

    All names are normalized through the shared ESPN→football-data mapping.
    """
    base_comp, _view = resolve_competition_query(comp_name)
    lookup_names = {comp_name, base_comp}
    raw_teams: list[str] = _teams_from_predicted_table(comp_name)
    if not raw_teams:
        try:
            if os.path.exists(config.CURRENT_SEASON_TEAMS_FILE):
                with open(config.CURRENT_SEASON_TEAMS_FILE, "r", encoding="utf-8") as fh:
                    current = json.load(fh)
                if isinstance(current, dict):
                    for name in lookup_names:
                        cached = current.get(name)
                        if cached:
                            raw_teams = list(cached)
                            break
        except Exception:
            raw_teams = []
    if not raw_teams:
        league_teams = _load_league_teams()
        for name in lookup_names:
            cached = league_teams.get(name)
            if cached:
                raw_teams = list(cached)
                break
    seen: set[str] = set()
    out: list[str] = []
    for team in raw_teams:
        canon = _normalize_team_name(str(team).strip(), base_comp)
        if not canon:
            continue
        key = canon.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(canon)
    return out


def _align_standings_to_canonical_roster(standings: dict, comp_name: str) -> dict:
    """Re-key real rows onto mapped canons; drop alias orphans when roster known.

    Runs for every standings layout (single table, Liga MX tournament, MLS
    conferences, etc.). Every team name is passed through the shared mapping
    so ESPN/football-data dual labels cannot inflate the table.
    """
    if not standings or not isinstance(standings.get("groups"), list):
        return standings
    roster = _canonical_roster_teams(comp_name)
    base_comp, _view = resolve_competition_query(comp_name)
    layout = str(standings.get("standings_layout") or "")
    if layout and layout not in {"single_table", "", "leagues_cup_dual"}:
        # Still rename entries to canonicals via dedupe; keep structure.
        return standings
    new_groups = []
    for group in standings["groups"]:
        entries = group.get("entries") or []
        # Skip alignment for multi-group cups / conferences — except Leagues Cup
        # dual MLS / Liga MX tables, which share the predicted roster per side.
        group_name = str(group.get("name") or "")
        if layout == "leagues_cup_dual":
            try:
                from competition_rules import leagues_cup_table_side
                side_roster = [t for t in roster if leagues_cup_table_side(t) == group_name]
            except Exception:
                side_roster = list(roster)
            if not side_roster:
                new_groups.append(group)
                continue
            by_team: dict[str, dict] = {t: {
                "team": t, "rank": 0, "position": 0,
                "P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "GD": 0, "Pts": 0,
            } for t in side_roster}
            side_set = set(side_roster)
            for entry in entries:
                team = _normalize_team_name(str(entry.get("team", "")).strip(), base_comp)
                if not team or team not in side_set:
                    continue
                existing = by_team[team]
                try:
                    if int(entry.get("P") or 0) >= int(existing.get("P") or 0):
                        merged = dict(entry)
                        merged["team"] = team
                        by_team[team] = merged
                except Exception:
                    merged = dict(entry)
                    merged["team"] = team
                    by_team[team] = merged
            ranked = sorted(
                by_team.values(),
                key=lambda r: (
                    -int(r.get("Pts") or 0),
                    -int(r.get("GD") or 0),
                    -int(r.get("GF") or 0),
                    str(r.get("team") or ""),
                ),
            )
            for idx, row in enumerate(ranked, start=1):
                row["rank"] = idx
                row["position"] = idx
            new_groups.append({**group, "entries": ranked})
            continue
        if len(standings["groups"]) > 1 and group_name not in {
            "Overall", "Regular Season", "League Phase", "Supporters Shield",
        }:
            new_groups.append(group)
            continue
        by_team: dict[str, dict] = {t: {
            "team": t, "rank": 0, "position": 0,
            "P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "GD": 0, "Pts": 0,
        } for t in roster}
        for entry in entries:
            raw = str(entry.get("team", "")).strip()
            if not raw:
                continue
            canon = _normalize_team_name(raw, base_comp)
            if not canon:
                continue
            key = canon.lower()
            if key not in by_key:
                cloned = dict(entry)
                cloned["team"] = canon
                by_key[key] = cloned
                order.append(key)
            else:
                by_key[key] = _merge_entry(by_key[key], entry, canon)

        group_roster = _roster_for_group(group_name)
        if group_roster:
            roster_set = set(group_roster)
            by_team = {
                t: {
                    "team": t, "rank": 0, "position": 0,
                    "P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "GD": 0, "Pts": 0,
                }
                for t in group_roster
            }
            for key in order:
                row = by_key[key]
                team = row["team"]
                if team not in roster_set:
                    continue
                by_team[team] = _merge_entry(by_team[team], row, team)
            ranked_rows = list(by_team.values())
        else:
            ranked_rows = [by_key[k] for k in order]

        ranked = sorted(
            ranked_rows,
            key=lambda r: (
                -int(r.get("Pts") or 0),
                -int(r.get("GD") or 0),
                -int(r.get("GF") or 0),
                str(r.get("team") or ""),
            ),
        )
        for idx, row in enumerate(ranked, start=1):
            row["rank"] = idx
            row["position"] = idx
        new_groups.append({**group, "entries": ranked})
    out = dict(standings)
    out["groups"] = new_groups
    return out


def _sanitize_real_standings(standings: dict | None, comp_name: str) -> dict | None:
    """Dedupe + align so real tables always match the predicted team set.

    Safe to call on persisted cache rows that predate mapping fixes.
    """
    if not standings or not isinstance(standings, dict):
        return standings
    cleaned = _dedupe_standings_groups(standings, comp_name)
    cleaned = _align_standings_to_canonical_roster(cleaned, comp_name)
    return cleaned


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
                        # Re-sanitize so older caches cannot serve ESPN/football-data
                        # duplicate rows or a roster that diverges from predictions.
                        cleaned = _sanitize_real_standings(table, comp_name)
                        _real_tables[comp_name] = cleaned if cleaned is not None else table
    except Exception:
        pass


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


# Warm after ``_load_league_teams`` exists — sanitize-on-load may need it.
_load_persisted_standings()


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
    competition = str(game.get("competition", "") or "").strip()
    home_raw = str(game.get("home_team", "") or "").strip()
    away_raw = str(game.get("away_team", "") or "").strip()
    try:
        home = (canonical_team_name(home_raw, competition) or home_raw).lower()
        away = (canonical_team_name(away_raw, competition) or away_raw).lower()
    except Exception:
        home = home_raw.lower()
        away = away_raw.lower()
    if game_date and home and away:
        return f"fixture:{game_date.isoformat()}|{competition.lower()}|{home}|{away}"
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
                    # Always map team names through the master alias file so
                    # ESPN display names cannot create duplicate standings rows.
                    comp = str(row.get("competition", "")).strip()
                    if comp:
                        try:
                            ht = canonical_team_name(row.get("home_team"), comp)
                            at = canonical_team_name(row.get("away_team"), comp)
                            if ht:
                                row["home_team"] = ht
                            if at:
                                row["away_team"] = at
                            winner = str(row.get("winner") or "").strip()
                            if winner:
                                mapped_w = canonical_team_name(winner, comp)
                                if mapped_w:
                                    row["winner"] = mapped_w
                        except Exception:
                            pass
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
    # Prefer a fresh in-memory / persisted table before re-scanning history files.
    with _real_tables_lock:
        cached = _real_tables.get(comp_name)
    if isinstance(cached, dict) and cached.get("groups"):
        updated = cached.get("updated_at", "")
        if not updated:
            return cached
        try:
            age = (datetime.now() - datetime.fromisoformat(str(updated))).total_seconds()
        except Exception:
            return cached
        if age < float(getattr(config, "REAL_TABLES_CACHE_TTL", 300)):
            return cached

    base_comp, mls_view = resolve_competition_query(comp_name)
    # Only count games from the active season window (drops prior May finales
    # during Jul–Aug preseason when no new-season CSV exists yet).
    comp_games = filter_games_to_active_season(
        collect_competition_games(comp_name),
        base_comp,
    )
    # Denmark: drop any non-roster (e.g. historical DK1→Bundesliga) contamination.
    if base_comp == "Denmark/Danish Superliga":
        comp_games = _filter_games_to_known_roster(comp_games, base_comp)
    if base_comp == config.LIGA_MX_COMPETITION or base_comp.startswith("Mexico/"):
        # Liga MX already has tournament filtering; re-apply after season filter.
        try:
            comp_games = filter_games_to_liga_mx_tournament(
                comp_games, active_liga_mx_tournament_label()
            ) or comp_games
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
        # Always collapse ESPN/football-data aliases and align to the same
        # canonical roster predicted tables use (Premier League etc.).
        response = _sanitize_real_standings(response, comp_name) or response
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
    if fmt and fmt.get("format") in {
        "group_stage_then_knockout",
        "league_phase_then_knockout",
        "dual_league_phase_then_knockout",
    }:
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

    def _apply_result(table, home, away, hg, ag, game=None):
        hs = table.setdefault(home, {"P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "GD": 0, "Pts": 0})
        at = table.setdefault(away, {"P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "GD": 0, "Pts": 0})
        hs["P"] += 1; at["P"] += 1
        hs["GF"] += int(hg); hs["GA"] += int(ag)
        at["GF"] += int(ag); at["GA"] += int(hg)
        hs["GD"] = hs["GF"] - hs["GA"]
        at["GD"] = at["GF"] - at["GA"]
        no_draws = bool((fmt or {}).get("no_draws"))
        decided_pk = bool(game and (
            game.get("decided_by_penalties")
            or "pen" in str(game.get("detail") or game.get("status_detail") or "").lower()
        ))
        winner = str((game or {}).get("winner") or "").strip()
        if hg > ag:
            hs["W"] += 1; at["L"] += 1; hs["Pts"] += 3
        elif ag > hg:
            at["W"] += 1; hs["L"] += 1; at["Pts"] += 3
        elif no_draws and (decided_pk or winner):
            # Leagues Cup style: regulation draw → shootout awards 2 / 1.
            win_team = winner
            if not win_team:
                win_team = home  # last resort; prefer explicit winner
            if win_team == away or (not winner and False):
                at["W"] += 1; hs["L"] += 1
                at["Pts"] += 2; hs["Pts"] += 1
            else:
                hs["W"] += 1; at["L"] += 1
                hs["Pts"] += 2; at["Pts"] += 1
            hs["D"] = hs.get("D", 0)
            at["D"] = at.get("D", 0)
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

    # ── Leagues Cup 2026: dual MLS / Liga MX Phase One tables ─────
    if base_comp == "CONCACAF/Leagues Cup" or (
        fmt and fmt.get("format") == "dual_league_phase_then_knockout"
    ):
        from competition_rules import (
            LEAGUES_CUP_TABLE_LIGA_MX,
            LEAGUES_CUP_TABLE_MLS,
            leagues_cup_table_side,
        )

        def _outcome_points(home, away, hg, ag, game):
            """Return (home_pts, away_pts, home_wdl, away_wdl) for one match."""
            no_draws = bool((fmt or {}).get("no_draws"))
            decided_pk = bool(game and (
                game.get("decided_by_penalties")
                or "pen" in str(game.get("detail") or game.get("status_detail") or "").lower()
            ))
            winner = str((game or {}).get("winner") or "").strip()
            if hg > ag:
                return 3, 0, "W", "L"
            if ag > hg:
                return 0, 3, "L", "W"
            if no_draws and (decided_pk or winner):
                if winner == away:
                    return 1, 2, "L", "W"
                return 2, 1, "W", "L"
            return 1, 1, "D", "D"

        def _credit_team(table, team, gf, ga, pts, wdl):
            row = table.setdefault(
                team, {"P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "GD": 0, "Pts": 0}
            )
            row["P"] += 1
            row["GF"] += int(gf)
            row["GA"] += int(ga)
            row["GD"] = row["GF"] - row["GA"]
            row["Pts"] += int(pts)
            if wdl == "W":
                row["W"] += 1
            elif wdl == "L":
                row["L"] += 1
            else:
                row["D"] += 1

        phase_games = group_stage_games or [
            g for g in comp_games
            if classify_match_stage(g, base_comp, team_to_group) != "knockout"
        ]
        mls_table = {}
        liga_table = {}
        for g in phase_games:
            ht = str(g.get("home_team", "")).strip()
            at = str(g.get("away_team", "")).strip()
            if not ht or not at:
                continue
            try:
                hs = int(g.get("home_score", 0))
                as_ = int(g.get("away_score", 0))
            except (TypeError, ValueError):
                continue
            hp, ap, hwdl, awdl = _outcome_points(ht, at, hs, as_, g)
            side_map = {
                LEAGUES_CUP_TABLE_MLS: mls_table,
                LEAGUES_CUP_TABLE_LIGA_MX: liga_table,
            }
            home_side = leagues_cup_table_side(ht)
            away_side = leagues_cup_table_side(at)
            if home_side in side_map:
                _credit_team(side_map[home_side], ht, hs, as_, hp, hwdl)
            if away_side in side_map:
                _credit_team(side_map[away_side], at, as_, hs, ap, awdl)

        def _entries(table):
            ranked = _rank_table(table)
            return [{"team": team, "rank": pos, **stats} for pos, (team, stats) in enumerate(ranked, 1)]

        groups = [
            {"name": LEAGUES_CUP_TABLE_MLS, "entries": _entries(mls_table)},
            {"name": LEAGUES_CUP_TABLE_LIGA_MX, "entries": _entries(liga_table)},
        ]
        return _finalize(
            groups,
            source="computed",
            current_phase=current_competition_phase(comp_games, base_comp),
        )

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
    response = package_real_standings(comp_name, groups, "placeholder")
    return _sanitize_real_standings(response, comp_name) or response
