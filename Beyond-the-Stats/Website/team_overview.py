"""
Unified Team Overview Module for Beyond the Stats.

Computes comprehensive profile information for a team:
- Standardized 3-tier names (full, short, abbreviation), id, logo, and league
- Primary league and all active competitions
- Recent 5 matches across all competitions (domestic league, cups, continental)
- Real computed season averages (shots, shots on target, corners, possession %, fouls, passes)
- Multi-competition predicted season finish & position odds
- Latest match lineup (formation, starting XI, substitutes)
- Relative ratings (attack, defence, overall)
- Team form streaks and momentum guide
- Fast in-memory caching and scheduled pre-caching for popular teams (Top 5 European Leagues & MLS)
"""

from __future__ import annotations

import logging
import os
import time
import threading
from collections import defaultdict
from typing import Any, Dict, List, Optional

import pandas as pd

from standardized_teams import resolve_team_info, decorate_game_team_names
from competition_rules import (
    canonical_team_name,
    _batch_load_all_games,
    _iter_current_club_season_csv_files,
)
from match_analytics import (
    get_team_comprehensive_form,
    compute_team_momentum,
)
from routes.leagues import _load_projected_competition_table
import config

logger = logging.getLogger(__name__)

# Cache for team overview payloads: { team_lower: (expires_at, payload) }
_TEAM_OVERVIEW_CACHE: Dict[str, tuple[float, dict]] = {}
_TEAM_OVERVIEW_CACHE_LOCK = threading.Lock()
_TEAM_OVERVIEW_TTL = 3600.0  # 1 hour

# Global precomputed indexes to make per-team lookups O(1)
_INDEX_LOCK = threading.Lock()
_INDEX_BUILT_TS: float = 0.0
_INDEX_TTL = 3600.0  # 1 hour

_ALL_TEAM_STATS: Dict[str, dict] = {}
_ALL_TEAM_LINEUPS: Dict[str, dict] = {}
_ALL_TEAM_COMPS: Dict[str, set[str]] = {}
_ALL_TEAM_PRIMARY_LEAGUE: Dict[str, str] = {}
_ALL_LEAGUE_RATINGS: Dict[str, Dict[str, dict]] = {}

POPULAR_LEAGUES = [
    "England/Premier League",
    "Spain/La Liga",
    "Germany/Bundesliga",
    "Italy/Serie A",
    "France/Ligue 1",
    "United States/MLS",
]


def _build_global_team_indexes(force: bool = False) -> None:
    """Build global lookup tables for season statistics, latest lineups, and active competitions."""
    global _INDEX_BUILT_TS, _ALL_TEAM_STATS, _ALL_TEAM_LINEUPS, _ALL_TEAM_COMPS, _ALL_TEAM_PRIMARY_LEAGUE, _ALL_LEAGUE_RATINGS

    now = time.time()
    if not force and _INDEX_BUILT_TS > 0 and (now - _INDEX_BUILT_TS) < _INDEX_TTL:
        return

    with _INDEX_LOCK:
        if not force and _INDEX_BUILT_TS > 0 and (now - _INDEX_BUILT_TS) < _INDEX_TTL:
            return

        team_stats_acc = defaultdict(lambda: {
            "shots": [], "sot": [], "corners": [], "possession": [],
            "fouls": [], "passes": [], "acc_passes": [], "goals_scored": [], "goals_conceded": []
        })
        team_lineups = {}
        team_comps = defaultdict(set)
        team_primary_league = {}
        league_goals = defaultdict(lambda: {"gf": defaultdict(float), "ga": defaultdict(float), "mp": defaultdict(int)})

        # 1. Inspect games across all competitions
        games_dict = _batch_load_all_games()
        for comp, glist in games_dict.items():
            is_domestic_league = (
                "/" in comp
                and not any(cup in comp.lower() for cup in ["cup", "cl", "champions", "europa", "conference", "trophy", "shield", "friendly"])
            )
            for g in glist:
                h = canonical_team_name(g.get("home_team", ""), comp)
                a = canonical_team_name(g.get("away_team", ""), comp)
                dt = str(g.get("match_date", ""))

                if h:
                    team_comps[h].add(comp)
                    if is_domestic_league and h not in team_primary_league:
                        team_primary_league[h] = comp
                if a:
                    team_comps[a].add(comp)
                    if is_domestic_league and a not in team_primary_league:
                        team_primary_league[a] = comp

                # Lineup tracking (pick latest date)
                lu = g.get("lineups")
                if isinstance(lu, dict):
                    h_lu = lu.get("home")
                    if h_lu and (h_lu.get("startXI") or h_lu.get("substitutes")):
                        if h not in team_lineups or dt > team_lineups[h]["date"]:
                            team_lineups[h] = {
                                "date": dt,
                                "competition": comp,
                                "opponent": a,
                                "venue": "Home",
                                "formation": str(h_lu.get("formation") or ""),
                                "startXI": h_lu.get("startXI") or [],
                                "substitutes": h_lu.get("substitutes") or [],
                            }
                    a_lu = lu.get("away")
                    if a_lu and (a_lu.get("startXI") or a_lu.get("substitutes")):
                        if a not in team_lineups or dt > team_lineups[a]["date"]:
                            team_lineups[a] = {
                                "date": dt,
                                "competition": comp,
                                "opponent": h,
                                "venue": "Away",
                                "formation": str(a_lu.get("formation") or ""),
                                "startXI": a_lu.get("startXI") or [],
                                "substitutes": a_lu.get("substitutes") or [],
                            }

                # ESPN Boxscore / In-Game Stats
                hs = g.get("home_stats")
                as_ = g.get("away_stats")
                if hs and isinstance(hs, dict):
                    try:
                        acc = team_stats_acc[h]
                        if "totalShots" in hs: acc["shots"].append(float(hs["totalShots"]))
                        if "shotsOnTarget" in hs: acc["sot"].append(float(hs["shotsOnTarget"]))
                        if "wonCorners" in hs: acc["corners"].append(float(hs["wonCorners"]))
                        if "possessionPct" in hs: acc["possession"].append(float(hs["possessionPct"]))
                        if "foulsCommitted" in hs: acc["fouls"].append(float(hs["foulsCommitted"]))
                        if "totalPasses" in hs: acc["passes"].append(float(hs["totalPasses"]))
                        if "accuratePasses" in hs: acc["acc_passes"].append(float(hs["accuratePasses"]))
                    except Exception:
                        pass
                if as_ and isinstance(as_, dict):
                    try:
                        acc = team_stats_acc[a]
                        if "totalShots" in as_: acc["shots"].append(float(as_["totalShots"]))
                        if "shotsOnTarget" in as_: acc["sot"].append(float(as_["shotsOnTarget"]))
                        if "wonCorners" in as_: acc["corners"].append(float(as_["wonCorners"]))
                        if "possessionPct" in as_: acc["possession"].append(float(as_["possessionPct"]))
                        if "foulsCommitted" in as_: acc["fouls"].append(float(as_["foulsCommitted"]))
                        if "totalPasses" in as_: acc["passes"].append(float(as_["totalPasses"]))
                        if "accuratePasses" in as_: acc["acc_passes"].append(float(as_["accuratePasses"]))
                    except Exception:
                        pass

                # Goals and relative rating tallies
                try:
                    h_score = g.get("home_score")
                    a_score = g.get("away_score")
                    if h_score is not None and a_score is not None:
                        h_val = float(h_score)
                        a_val = float(a_score)
                        team_stats_acc[h]["goals_scored"].append(h_val)
                        team_stats_acc[h]["goals_conceded"].append(a_val)
                        team_stats_acc[a]["goals_scored"].append(a_val)
                        team_stats_acc[a]["goals_conceded"].append(h_val)

                        if is_domestic_league:
                            league_goals[comp]["gf"][h] += h_val
                            league_goals[comp]["ga"][h] += a_val
                            league_goals[comp]["mp"][h] += 1
                            league_goals[comp]["gf"][a] += a_val
                            league_goals[comp]["ga"][a] += h_val
                            league_goals[comp]["mp"][a] += 1
                except Exception:
                    pass

        # 2. Ingest processed club season CSVs (for broader shots/corners/fouls coverage)
        for comp_name, season_path in _iter_current_club_season_csv_files():
            try:
                df = pd.read_csv(season_path)
            except Exception:
                continue
            cols = set(df.columns)
            if not {"HomeTeam", "AwayTeam"}.issubset(cols):
                continue
            for _, row in df.iterrows():
                h = canonical_team_name(row["HomeTeam"], comp_name)
                a = canonical_team_name(row["AwayTeam"], comp_name)
                hs = pd.to_numeric(row.get("HS"), errors="coerce")
                as_ = pd.to_numeric(row.get("AS"), errors="coerce")
                hst = pd.to_numeric(row.get("HST"), errors="coerce")
                ast = pd.to_numeric(row.get("AST"), errors="coerce")
                hc = pd.to_numeric(row.get("HC"), errors="coerce")
                ac = pd.to_numeric(row.get("AC"), errors="coerce")
                hf = pd.to_numeric(row.get("HF"), errors="coerce")
                af = pd.to_numeric(row.get("AF"), errors="coerce")

                if pd.notna(hs): team_stats_acc[h]["shots"].append(float(hs))
                if pd.notna(hst): team_stats_acc[h]["sot"].append(float(hst))
                if pd.notna(hc): team_stats_acc[h]["corners"].append(float(hc))
                if pd.notna(hf): team_stats_acc[h]["fouls"].append(float(hf))

                if pd.notna(as_): team_stats_acc[a]["shots"].append(float(as_))
                if pd.notna(ast): team_stats_acc[a]["sot"].append(float(ast))
                if pd.notna(ac): team_stats_acc[a]["corners"].append(float(ac))
                if pd.notna(af): team_stats_acc[a]["fouls"].append(float(af))

        # 3. Finalize Computed Season Averages
        final_team_stats = {}
        for team, acc in team_stats_acc.items():
            sh = round(sum(acc["shots"]) / len(acc["shots"]), 1) if acc["shots"] else None
            sot = round(sum(acc["sot"]) / len(acc["sot"]), 1) if acc["sot"] else None
            crn = round(sum(acc["corners"]) / len(acc["corners"]), 1) if acc["corners"] else None
            poss = round(sum(acc["possession"]) / len(acc["possession"]), 1) if acc["possession"] else None
            fls = round(sum(acc["fouls"]) / len(acc["fouls"]), 1) if acc["fouls"] else None
            pass_avg = round(sum(acc["passes"]) / len(acc["passes"]), 1) if acc["passes"] else None
            pass_acc_pct = (
                round((sum(acc["acc_passes"]) / max(sum(acc["passes"]), 1.0)) * 100.0, 1)
                if acc["passes"]
                else None
            )
            gf = round(sum(acc["goals_scored"]) / len(acc["goals_scored"]), 2) if acc["goals_scored"] else None
            ga = round(sum(acc["goals_conceded"]) / len(acc["goals_conceded"]), 2) if acc["goals_conceded"] else None

            final_team_stats[team] = {
                "matches_recorded": max(len(acc["shots"]), len(acc["goals_scored"])),
                "shots_per_game": sh,
                "shots_on_target_per_game": sot,
                "corners_per_game": crn,
                "possession_pct": poss,
                "fouls_per_game": fls,
                "passes_per_game": pass_avg,
                "pass_accuracy_pct": pass_acc_pct,
                "goals_scored_per_game": gf,
                "goals_conceded_per_game": ga,
            }

        # 4. Finalize League Ratings
        final_league_ratings = defaultdict(dict)
        for comp, stats in league_goals.items():
            tot_mp = sum(stats["mp"].values())
            if tot_mp == 0:
                continue
            lg_gf_pg = sum(stats["gf"].values()) / tot_mp
            lg_ga_pg = sum(stats["ga"].values()) / tot_mp
            for team, mp in stats["mp"].items():
                if mp <= 0:
                    continue
                gf_pg = stats["gf"][team] / mp
                ga_pg = stats["ga"][team] / mp
                att = round(max(40.0, min(99.0, 75.0 + (gf_pg - lg_gf_pg) * 15.0)), 1)
                defense = round(max(40.0, min(99.0, 75.0 + (lg_ga_pg - ga_pg) * 15.0)), 1)
                ovr = round((att + defense) / 2.0, 1)
                final_league_ratings[comp][team] = {
                    "attack_rating": att,
                    "defence_rating": defense,
                    "overall_rating": ovr,
                }

        _ALL_TEAM_STATS = final_team_stats
        _ALL_TEAM_LINEUPS = team_lineups
        _ALL_TEAM_COMPS = dict(team_comps)
        _ALL_TEAM_PRIMARY_LEAGUE = team_primary_league
        _ALL_LEAGUE_RATINGS = dict(final_league_ratings)
        _INDEX_BUILT_TS = now
        logger.info(
            "Built global team indexes: %d teams with stats, %d with lineups, %d with comps",
            len(_ALL_TEAM_STATS),
            len(_ALL_TEAM_LINEUPS),
            len(_ALL_TEAM_COMPS),
        )


def build_team_overview(team_name: str) -> dict:
    """Generate the comprehensive overview payload for a single team."""
    raw_name = str(team_name or "").strip()
    if not raw_name:
        return {"ok": False, "error": "Team name is required"}

    # Check memory cache
    now = time.time()
    cache_key = raw_name.lower()
    with _TEAM_OVERVIEW_CACHE_LOCK:
        if cache_key in _TEAM_OVERVIEW_CACHE:
            exp_ts, cached_payload = _TEAM_OVERVIEW_CACHE[cache_key]
            if now < exp_ts:
                return cached_payload

    # Ensure indexes are ready
    _build_global_team_indexes()

    canon_name = canonical_team_name(raw_name)
    team_info = resolve_team_info(canon_name or raw_name)

    # Effective team identity keys
    lookup_candidates = [
        canon_name,
        raw_name,
        team_info.get("full_name"),
        team_info.get("short_name"),
    ]
    primary_comp = None
    for cand in lookup_candidates:
        if cand and cand in _ALL_TEAM_PRIMARY_LEAGUE:
            primary_comp = _ALL_TEAM_PRIMARY_LEAGUE[cand]
            break
    if not primary_comp:
        primary_comp = team_info.get("league", "")

    # 1. Competitions
    active_competitions = set()
    for cand in lookup_candidates:
        if cand and cand in _ALL_TEAM_COMPS:
            active_competitions.update(_ALL_TEAM_COMPS[cand])
    if primary_comp and primary_comp not in active_competitions:
        active_competitions.add(primary_comp)
    competitions_list = sorted(list(active_competitions))

    # 2. Last 5 Matches (All Competitions)
    last_5_all = get_team_comprehensive_form(canon_name or raw_name, limit=5, venue=None, exclude_friendlies=False)
    for m in last_5_all:
        decorate_game_team_names(m, m.get("competition", ""))

    last_5_home = get_team_comprehensive_form(canon_name or raw_name, limit=5, venue="home", exclude_friendlies=False)
    for m in last_5_home:
        decorate_game_team_names(m, m.get("competition", ""))

    last_5_away = get_team_comprehensive_form(canon_name or raw_name, limit=5, venue="away", exclude_friendlies=False)
    for m in last_5_away:
        decorate_game_team_names(m, m.get("competition", ""))

    # 3. Streaks & Momentum
    momentum = compute_team_momentum(last_5_all)

    # 4. Computed Season Statistics Averages
    season_averages = {
        "matches_recorded": 0,
        "shots_per_game": None,
        "shots_on_target_per_game": None,
        "corners_per_game": None,
        "possession_pct": None,
        "fouls_per_game": None,
        "passes_per_game": None,
        "pass_accuracy_pct": None,
        "goals_scored_per_game": None,
        "goals_conceded_per_game": None,
    }
    for cand in lookup_candidates:
        if cand and cand in _ALL_TEAM_STATS:
            season_averages = _ALL_TEAM_STATS[cand]
            break

    # 5. Latest Game Lineup & Substitutes
    latest_lineup = None
    for cand in lookup_candidates:
        if cand and cand in _ALL_TEAM_LINEUPS:
            latest_lineup = _ALL_TEAM_LINEUPS[cand]
            break

    # 6. Projected Season Finish & Odds Across Competitions
    predicted_season_finishes = []
    for comp in competitions_list:
        try:
            table = _load_projected_competition_table(comp)
            for row in table:
                row_team = str(row.get("team", "")).lower()
                if any(c.lower() == row_team for c in lookup_candidates if c):
                    predicted_season_finishes.append({
                        "competition": comp,
                        "predicted_position": row.get("position"),
                        "most_likely_position": row.get("most_likely_position"),
                        "most_likely_position_pct": row.get("most_likely_position_pct"),
                        "win_league_pct": row.get("win_league_pct"),
                        "top4_pct": row.get("top4_pct"),
                        "bottom3_pct": row.get("bottom3_pct"),
                        "simulated_points": row.get("Pts"),
                        "played_real": row.get("PlayedReal"),
                        "played_predicted": row.get("PlayedPred"),
                        "position_odds": row.get("position_odds", {}),
                    })
                    break
        except Exception:
            pass

    # 7. Relative Ratings
    ratings = {
        "attack_rating": None,
        "defence_rating": None,
        "overall_rating": None,
    }
    if primary_comp and primary_comp in _ALL_LEAGUE_RATINGS:
        comp_ratings = _ALL_LEAGUE_RATINGS[primary_comp]
        for cand in lookup_candidates:
            if cand and cand in comp_ratings:
                ratings = comp_ratings[cand]
                break
    if ratings["attack_rating"] is None:
        # Fallback estimation based on season averages
        gf = season_averages.get("goals_scored_per_game") or 1.35
        ga = season_averages.get("goals_conceded_per_game") or 1.35
        att = round(max(40.0, min(99.0, 75.0 + (gf - 1.35) * 15.0)), 1)
        defn = round(max(40.0, min(99.0, 75.0 + (1.35 - ga) * 15.0)), 1)
        ratings = {
            "attack_rating": att,
            "defence_rating": defn,
            "overall_rating": round((att + defn) / 2.0, 1),
        }

    # 8. Next Game Payload Integration
    from routes.predictions import _get_team_next_game_payload
    next_game_res = _get_team_next_game_payload(canon_name or raw_name)
    next_game = next_game_res.get("game") if next_game_res.get("has_game") else None

    payload = {
        "ok": True,
        "team": canon_name or raw_name,
        "canonical_name": canon_name or raw_name,
        "team_full": team_info.get("full_name"),
        "team_short": team_info.get("short_name"),
        "team_abbr": team_info.get("abbreviation"),
        "team_id": team_info.get("id"),
        "team_logo": team_info.get("logo_file"),
        "team_info": team_info,
        "primary_league": primary_comp,
        "active_competitions": competitions_list,
        "ratings": ratings,
        "season_averages": season_averages,
        "momentum": momentum,
        "last_game_lineup": latest_lineup,
        "predicted_season_finishes": predicted_season_finishes,
        "last_5_matches": last_5_all,
        "last_5_home": last_5_home,
        "last_5_away": last_5_away,
        "next_game": next_game,
    }

    # Store in memory cache
    with _TEAM_OVERVIEW_CACHE_LOCK:
        _TEAM_OVERVIEW_CACHE[cache_key] = (now + _TEAM_OVERVIEW_TTL, payload)
        if canon_name and canon_name.lower() != cache_key:
            _TEAM_OVERVIEW_CACHE[canon_name.lower()] = (now + _TEAM_OVERVIEW_TTL, payload)

    return payload


_RESULT_REFRESH_LOCK = threading.Lock()
_RESULT_REFRESH_PENDING: set[str] = set()
_RESULT_REFRESH_RUNNING = False


def _team_identity_keys(name: str) -> set[str]:
    """Lower-cased spellings + team id that identify *name* in overview caches."""
    keys: set[str] = set()
    raw = str(name or "").strip()
    if not raw:
        return keys
    keys.add(raw.lower())
    try:
        canon = canonical_team_name(raw)
        if canon:
            keys.add(canon.lower())
        info = resolve_team_info(canon or raw) or {}
        for field in ("full_name", "short_name", "id"):
            val = str(info.get(field) or "").strip()
            if val:
                keys.add(val.lower() if field != "id" else f"id:{val}")
    except Exception:
        pass
    return keys


def invalidate_team_overviews(team_names) -> list[str]:
    """Drop cached overviews for teams whose match just finished.

    Matches on raw/canonical/full/short names and resolved team id so every
    alias key of the payload is removed. Returns the canonical team names
    whose payloads were dropped (used to re-warm them).
    """
    wanted: set[str] = set()
    for name in team_names or ():
        wanted |= _team_identity_keys(name)
    if not wanted:
        return []
    dropped: list[str] = []
    with _TEAM_OVERVIEW_CACHE_LOCK:
        for key in list(_TEAM_OVERVIEW_CACHE.keys()):
            _exp, payload = _TEAM_OVERVIEW_CACHE[key]
            ids = {key}
            if isinstance(payload, dict):
                for field in ("team", "canonical_name", "team_full", "team_short"):
                    val = str(payload.get(field) or "").strip()
                    if val:
                        ids.add(val.lower())
                if payload.get("team_id"):
                    ids.add(f"id:{payload.get('team_id')}")
            if ids & wanted:
                _TEAM_OVERVIEW_CACHE.pop(key, None)
                if isinstance(payload, dict) and payload.get("team"):
                    dropped.append(str(payload["team"]))
    return sorted(set(dropped))


def refresh_team_overviews_after_results(team_names) -> None:
    """Invalidate + asynchronously rebuild overviews after match results.

    Season averages / lineups come from the global index, so it is rebuilt
    (once per burst of results) before re-warming the dropped payloads.
    """
    global _RESULT_REFRESH_RUNNING
    dropped = invalidate_team_overviews(team_names)
    with _RESULT_REFRESH_LOCK:
        _RESULT_REFRESH_PENDING.update(dropped or [])
        _RESULT_REFRESH_PENDING.update(str(t) for t in (team_names or ()) if t)
        if _RESULT_REFRESH_RUNNING:
            return
        _RESULT_REFRESH_RUNNING = True

    def _worker():
        global _RESULT_REFRESH_RUNNING
        try:
            while True:
                with _RESULT_REFRESH_LOCK:
                    teams = sorted(_RESULT_REFRESH_PENDING)
                    _RESULT_REFRESH_PENDING.clear()
                    if not teams:
                        _RESULT_REFRESH_RUNNING = False
                        return
                try:
                    _build_global_team_indexes(force=True)
                except Exception as exc:
                    logger.warning("Team index rebuild after results failed: %s", exc)
                # Results in the same burst may have re-cached stale payloads
                # built from the old index while it was rebuilding.
                invalidate_team_overviews(teams)
                for team in teams:
                    try:
                        build_team_overview(team)
                    except Exception as exc:
                        logger.warning("Re-warming overview for %s failed: %s", team, exc)
        except Exception:
            with _RESULT_REFRESH_LOCK:
                _RESULT_REFRESH_RUNNING = False

    threading.Thread(target=_worker, name="team-overview-results", daemon=True).start()


def precache_popular_teams() -> int:
    """Pre-cache overview payloads for popular clubs (Top 5 European Leagues & MLS)."""
    _build_global_team_indexes(force=True)

    league_teams_path = getattr(config, "LEAGUE_TEAMS_FILE", None) or os.path.join(
        getattr(config, "PROJECT_DIR", ""), "Data", "Seeds", "league_teams.json"
    )
    teams_to_cache = set()
    if league_teams_path and os.path.exists(league_teams_path):
        try:
            import json
            with open(league_teams_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for comp in POPULAR_LEAGUES:
                teams = data.get(comp, [])
                for t in teams:
                    if t:
                        teams_to_cache.add(t)
        except Exception as e:
            logger.warning("Could not read league_teams.json for pre-caching: %s", e)

    count = 0
    start_time = time.time()
    for team in sorted(teams_to_cache):
        try:
            build_team_overview(team)
            count += 1
        except Exception as e:
            logger.warning("Failed pre-caching team overview for %s: %s", team, e)

    elapsed = time.time() - start_time
    logger.info("Pre-cached %d popular teams in %.2fs", count, elapsed)
    return count


def start_team_overview_precache_worker() -> None:
    """Start background daemon thread that pre-caches popular teams at startup and daily."""
    def _worker():
        # Delay slightly to allow server startup and other warmups
        time.sleep(2.0)
        logger.info("[precache] Starting initial popular teams pre-caching...")
        precache_popular_teams()
        while True:
            # Sleep 24 hours (86400s)
            time.sleep(86400.0)
            logger.info("[precache] Running daily popular teams pre-caching refresh...")
            precache_popular_teams()

    thread = threading.Thread(target=_worker, name="TeamOverviewPrecacheWorker", daemon=True)
    thread.start()
