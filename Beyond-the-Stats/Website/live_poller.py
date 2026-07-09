"""Background live score polling thread and ESPN scoreboard merging."""
import importlib.util
import json
import os
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

import config
from accuracy_tracker import _track_prediction_results
from espn_api import _fetch_competition_scores, _fetch_event_summary, LIVE_SCORE_FETCH_TIMEOUT
from espn_parser import (
    _parse_espn_boxscore_stats,
    _parse_espn_game_info,
    _parse_espn_head_to_head,
    _parse_espn_injuries_availability,
    _parse_espn_key_events,
    _parse_espn_last_five,
    _parse_espn_lineups,
    _parse_espn_shot_mapping,
    _parse_espn_situation,
    _parse_espn_team_stats,
)
from live_prediction import (
    _build_live_prematch_index,
    _compute_live_prediction,
    _extract_passes_to_stats,
    _match_prematch_record,
    _promote_team_stats_to_home_away,
    _update_cumulative_momentum,
)
from standings import (
    _clear_leaders_cache,
    _clear_standings_cache,
    _compute_standings_from_history,
    _load_live_score_history,
    _real_tables,
    _real_tables_lock,
    _upsert_live_score_history,
)

_live_scores: dict[str, dict] = {}
_live_scores_lock = threading.RLock()

_live_summary_cache: dict[str, dict] = {}
_live_summary_cache_lock = threading.Lock()
_last_friendlies_sync_ts = 0.0
_FRIENDLIES_SYNC_INTERVAL_S = 900


def _load_friendlies_sync_module():
    script_path = os.path.join(config.FILES_DIR, "Update_Club_Friendlies.py")
    if not os.path.exists(script_path):
        return None
    spec = importlib.util.spec_from_file_location("update_club_friendlies", script_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, config.FILES_DIR)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


def _chelsea_team_name(name):
    text = str(name or "").strip().lower().replace("fc", "").replace(" ", "")
    return text in {"chelsea", "chelseafc"}


def _is_chelsea_live_game(game):
    return _chelsea_team_name(game.get("home_team")) or _chelsea_team_name(game.get("away_team"))


def _sync_friendlies_results_if_due():
    global _last_friendlies_sync_ts
    now = time.time()
    if now - _last_friendlies_sync_ts < _FRIENDLIES_SYNC_INTERVAL_S:
        return
    module = _load_friendlies_sync_module()
    if module is None:
        return
    try:
        updated = module.update_recent_friendlies_results(days_back=1, days_forward=1)
        if updated:
            print(f"[friendlies] Updated {updated} final result(s) from ESPN.")
    except Exception:
        import traceback
        traceback.print_exc()
    _last_friendlies_sync_ts = now


def _merge_completed_to_history():
    """Move finished games from _live_scores into persistent history file.

    Stores all game data including summary fields (lineups, h2h, key events,
    boxscore stats) that were merged onto game objects from ``_live_summary_cache``.
    """
    history = _load_live_score_history()
    historic_ids = {g["match_id"] for g in history if g.get("match_id")}
    new_games = []
    cleared_standings = set()
    with _live_scores_lock:
        for comp_name, comp_data in _live_scores.items():
            for g in comp_data.get("games", []):
                if g.get("status") == "post" and g.get("match_id") not in historic_ids:
                    entry = dict(g)
                    entry.setdefault("competition", comp_name)
                    entry.setdefault("completed_at", datetime.now(timezone.utc).isoformat())
                    new_games.append(entry)
                    historic_ids.add(entry["match_id"])
                    cleared_standings.add(comp_name)
    for comp in cleared_standings:
        _clear_standings_cache(comp)
        _clear_leaders_cache(comp)
    if new_games:
        _upsert_live_score_history(new_games)
    # Track predictions for newly completed games against our CSV predictions.
    if new_games:
        _track_prediction_results(new_games)

def _effective_poller_date():
    """Return the effective date for live-score polling.

    Before 2am ET the previous day's games are still active, so we return
    yesterday's date.  After 2am ET we switch to today's date.
    """
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.hour < 2:
        return (now_et - timedelta(days=1)).date()
    return now_et.date()


def _uefa_live_scoring_allowed_for_comp(comp_name: str) -> bool:
    comp = str(comp_name or "").strip()
    if comp not in config.UEFA_LIVE_SCORE_COMPETITIONS:
        return True
    return config.uefa_live_scoring_allowed()


def _filter_live_games_for_competition(comp_name: str, games: list[dict]) -> list[dict]:
    """During UEFA qualifying, keep only final scores — no in-play live tracking."""
    if _uefa_live_scoring_allowed_for_comp(comp_name):
        return games
    filtered = []
    for game in games or []:
        status = str(game.get("status") or "").strip().lower()
        if status == "post":
            cleaned = dict(game)
            cleaned.pop("live_prediction", None)
            filtered.append(cleaned)
    return filtered


def _get_todays_competitions(today_date=None):
    """Return {competition: [kickoff_et, ...]} for competitions with games today.

    Checks all available data sources:
      1. Upcoming predictions CSVs (club, MLS, extra, cups, national team)
      2. World Cup projection JSON (group_fixtures + knockout rounds)
      3. Cup bracket JSON (knockout fixtures)

    Args:
        today_date: date override (defaults to ``date.today()``).
    """
    if today_date is None:
        today_date = date.today()
    now_et = datetime.now(ZoneInfo("America/New_York"))
    todays = defaultdict(list)

    # ── Source 1: upcoming predictions CSVs ──────────────────────
    for csv_path in config.UPCOMING_CSV_FILES.values():
        if not os.path.exists(csv_path):
            continue
        try:
            frame = pd.read_csv(csv_path, dtype=str)
        except Exception:
            continue
        for _, row in frame.iterrows():
            comp = str(row.get("competition", "") or "").strip()
            if comp not in config.LIVE_SCORE_COMPETITIONS:
                continue
            # Use match_datetime_utc converted to ET to determine if the
            # game falls on today in the LOCAL timezone (not UTC date).
            kickoff_utc_str = str(row.get("match_datetime_utc", "") or "").strip()
            if kickoff_utc_str:
                try:
                    dt_utc = pd.to_datetime(kickoff_utc_str, errors="coerce")
                    if pd.isna(dt_utc):
                        continue
                    if dt_utc.tz is None:
                        dt_utc = dt_utc.tz_localize("UTC")
                    kickoff_et = dt_utc.tz_convert(ZoneInfo("America/New_York"))
                    if kickoff_et.date() != today_date:
                        continue
                except Exception:
                    continue
            else:
                # Fallback to match_date column if no datetime available
                md = str(row.get("match_date", "") or "").strip()
                if not md:
                    continue
                try:
                    dt = pd.to_datetime(md, errors="coerce", dayfirst=False)
                    if pd.isna(dt):
                        continue
                    if dt.tz is None:
                        kickoff_et = dt.tz_localize(ZoneInfo("America/New_York"))
                    else:
                        kickoff_et = dt.tz_convert(ZoneInfo("America/New_York"))
                    if kickoff_et.date() != today_date:
                        continue
                except Exception:
                    continue
            todays[comp].append(kickoff_et)

    # ── Source 2: World Cup projection JSON ──────────────────────
    if "FIFA/World Cup" not in todays and os.path.exists(config.WORLD_CUP_PROJECTION_FILE):
        try:
            with open(config.WORLD_CUP_PROJECTION_FILE, "r", encoding="utf-8") as fh:
                wc_data = json.load(fh)
        except Exception:
            wc_data = {}

        # group_fixtures — list of dicts with match_date, match_datetime_utc etc.
        for fixture in wc_data.get("group_fixtures") or []:
            if isinstance(fixture, dict):
                _add_if_today(fixture, "FIFA/World Cup", today_date, now_et, todays)

        # knockout rounds
        for round_list in (wc_data.get("knockout") or {}).values():
            if isinstance(round_list, list):
                for match in round_list:
                    if isinstance(match, dict):
                        _add_if_today(match, "FIFA/World Cup", today_date, now_et, todays)

    # ── Source 3: cup bracket JSON ───────────────────────────────
    if os.path.exists(config.CUP_PROJECTED_BRACKET_FILE):
        try:
            with open(config.CUP_PROJECTED_BRACKET_FILE, "r", encoding="utf-8") as fh:
                cup_data = json.load(fh)
        except Exception:
            cup_data = {}

        if isinstance(cup_data, dict):
            for comp_name in list(config.LIVE_SCORE_COMPETITIONS.keys()):
                if comp_name in todays:
                    continue
                # Cup bracket has entries like {"England/FA Cup": {round_name: [...]}}
                comp_entry = cup_data.get(comp_name)
                if isinstance(comp_entry, dict):
                    for round_name, matches in comp_entry.items():
                        if isinstance(matches, list):
                            for match in matches:
                                if isinstance(match, dict):
                                    _add_if_today(match, comp_name, today_date, now_et, todays)

    # ── Source 4: club friendlies (Chelsea live only) ───────────
    if os.path.exists(config.FRIENDLIES_UPCOMING_FILE):
        try:
            frame = pd.read_csv(config.FRIENDLIES_UPCOMING_FILE, dtype=str)
        except Exception:
            frame = pd.DataFrame()
        for _, row in frame.iterrows():
            if str(row.get("live_tracking", "")).strip() != "1":
                continue
            kickoff_utc_str = str(row.get("match_datetime_utc", "") or "").strip()
            if not kickoff_utc_str:
                continue
            try:
                dt_utc = pd.to_datetime(kickoff_utc_str, errors="coerce")
                if pd.isna(dt_utc):
                    continue
                if dt_utc.tz is None:
                    dt_utc = dt_utc.tz_localize("UTC")
                kickoff_et = dt_utc.tz_convert(ZoneInfo("America/New_York"))
                if kickoff_et.date() == today_date:
                    todays[config.CLUB_FRIENDLIES_COMPETITION].append(kickoff_et)
            except Exception:
                continue

    return {k: sorted(v) for k, v in todays.items()}


def _add_if_today(entry, comp_name, today_date, now_et, out_dict):
    """If *entry* has a date field matching today in ET, add its kickoff."""
    # Prefer match_datetime_utc — the most reliable source for date-in-ET.
    dt_utc_str = str(entry.get("match_datetime_utc", "") or "").strip()
    if dt_utc_str and dt_utc_str not in ("", "nan", "None", "NaT"):
        try:
            dt_utc = pd.to_datetime(dt_utc_str, errors="coerce")
            if not pd.isna(dt_utc):
                if dt_utc.tz is None:
                    dt_utc = dt_utc.tz_localize("UTC")
                kickoff_et = dt_utc.tz_convert(ZoneInfo("America/New_York"))
                if kickoff_et.date() == today_date:
                    out_dict[comp_name].append(kickoff_et)
                return
        except Exception:
            pass
    # Fallback to match_date / date / kickoff (naive date — assume ET).
    raw = str(entry.get("match_date") or entry.get("date") or entry.get("kickoff") or "")
    if not raw or raw in ("", "nan", "None", "NaT"):
        return
    try:
        dt = pd.to_datetime(raw, errors="coerce", dayfirst=False)
        if pd.isna(dt):
            return
        if dt.tz is None:
            dt = dt.tz_localize(ZoneInfo("America/New_York"))
        else:
            dt = dt.tz_convert(ZoneInfo("America/New_York"))
        if dt.date() != today_date:
            return
        out_dict[comp_name].append(dt)
    except Exception:
        pass


def _compute_poll_interval(now, results, active_comps, todays_comps):
    """Return the sleep interval in seconds before the next poll cycle.

    - 60 seconds  if any game is in-progress or just kicked off (past 90 min),
                  or within 3 minutes of a scheduled kickoff (catches pre→in
                  transition within seconds).
    - 900 seconds (15 min) when a game kicks off within the next 60 minutes
    - wakes up at nearest_future − 1h when games are 1-2h away (drops to
      15-min pre-match polling mode at the 1h mark)
    - 1800 seconds otherwise (no games or all finished)
    """
    # Check live results for in-progress games
    for comp_data in results.values():
        for g in comp_data.get("games", []):
            if g.get("status") == "in":
                return 60

    now_et = datetime.now(ZoneInfo("America/New_York"))
    nearest_future = None

    for comp_name, kickoffs in todays_comps.items():
        for kt in kickoffs:
            try:
                if isinstance(kt, datetime):
                    diff = (kt - now_et).total_seconds()
                    # Game started within last 90 min -> poll at 60s
                    if -5400 <= diff <= 0:
                        return 60
                    # Within 3 minutes of kickoff -> poll every 60s so we catch
                    # the pre→in transition within seconds.
                    if 0 < diff <= 180:
                        return 60
                    # Future kickoff within 60 min -> poll every 15 min
                    if 0 < diff <= 3600:
                        return 900
                    # Future kickoff — track the nearest one
                    if diff > 0 and (nearest_future is None or diff < nearest_future):
                        nearest_future = diff
            except Exception:
                continue

    # Wake up at nearest future − 1h so pre-match 15-min polling starts on time.
    # If the nearest kickoff is within 3 min, we'd already have returned 60 above.
    if nearest_future is not None and nearest_future < 7200:
        wake_at = nearest_future - 3600
        return max(10, wake_at)

    return 1800

def _live_score_poller_loop():
    """Background thread: poll ESPN for live scores.

    Poll interval adapts dynamically:
      60s   during live games or games just kicked off (past 90 min),
      900s (15min) when a game kicks off within 60 minutes (pre-match),
      wakes at nearest_future - 1h when games are 1-2h away,
      1800s (30min) otherwise.
    Summaries (lineups, h2h, key events) fetched every cycle for ALL games.
    """
    # Init defaults so a crash in one cycle doesn't leave them undefined.
    todays_comps = {}
    active_comps = {}
    results = {}
    while True:
        try:
            poll_date = _effective_poller_date()
            today_str = poll_date.strftime("%Y%m%d")
            todays_comps = _get_todays_competitions(today_date=poll_date)
            active_comps = {}
            for comp in todays_comps:
                eid = config.LIVE_SCORE_COMPETITIONS.get(comp)
                if eid:
                    active_comps[comp] = eid

            results = {}
            if active_comps:
                with ThreadPoolExecutor(max_workers=min(8, len(active_comps))) as pool:
                    ft_to_name = {
                        pool.submit(_fetch_competition_scores, name, eid, today_str): name
                        for name, eid in active_comps.items()
                    }
                    for ft in as_completed(ft_to_name):
                        name = ft_to_name[ft]
                        try:
                            games = ft.result()
                            if name == config.CLUB_FRIENDLIES_COMPETITION:
                                games = [g for g in games if _is_chelsea_live_game(g)]
                            games = _filter_live_games_for_competition(name, games)
                            if not games:
                                continue
                            results[name] = {
                                "competition": name,
                                "games": games,
                                "last_polled_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
                            }
                        except Exception:
                            import traceback
                            traceback.print_exc()

            # Snapshot previous game statuses before merging (for detecting new completions).
            prev_statuses = {}
            with _live_scores_lock:
                for comp_name, comp_data in _live_scores.items():
                    for g in comp_data.get("games", []):
                        mid = g.get("match_id")
                        if mid:
                            prev_statuses[mid] = (comp_name, g.get("status", ""))

            with _live_scores_lock:
                # Day boundary: save ALL games (not just completed) then clear.
                _poller_day_str = poll_date.isoformat()
                if getattr(_live_score_poller_loop, "_poller_date", None) != _poller_day_str:
                    _merge_completed_to_history()
                    # Save any in-progress games that started yesterday but
                    # haven't finished yet so they persist in history.
                    now_utc = datetime.now(timezone.utc)
                    pending_history = []
                    for comp_name, comp_data in list(_live_scores.items()):
                        for g in comp_data.get("games", []):
                            if g.get("status") not in ("post", "in"):
                                continue
                            entry = dict(g)
                            entry.setdefault("competition", comp_name)
                            entry.setdefault("completed_at", now_utc.isoformat())
                            pending_history.append(entry)
                    try:
                        if pending_history:
                            _upsert_live_score_history(pending_history)
                        # An empty day performs no write. Only clear the
                        # in-memory day after any pending rows were persisted.
                        _live_scores.clear()
                        with _live_summary_cache_lock:
                            _live_summary_cache.clear()
                        _live_score_poller_loop._poller_date = _poller_day_str
                    except Exception:
                        # Keep yesterday's in-memory data and retry next cycle;
                        # never trade a persistence error for data loss.
                        import traceback
                        traceback.print_exc()
                # Merge new results into existing so finished games persist.
                for comp_name, comp_data in results.items():
                    new_games = comp_data.get("games", [])
                    existing = _live_scores.get(comp_name, {"games": []})
                    # Guard: if ESPN transiently returns 0 games but we already
                    # have data, keep the existing games to avoid "no live games"
                    # flickering across poll cycles.
                    if not new_games and existing.get("games"):
                        continue
                    games_by_id = {g["match_id"]: g for g in existing["games"] if g.get("match_id")}
                    for g in new_games:
                        if g.get("match_id"):
                            g["competition"] = comp_name
                            mid = g["match_id"]
                            if mid in games_by_id:
                                games_by_id[mid].update(g)
                            else:
                                games_by_id[mid] = g
                    _live_scores[comp_name] = {
                        "competition": comp_name,
                        "games": list(games_by_id.values()),
                        "last_polled_utc": comp_data["last_polled_utc"],
                        "cup_format": config._CUP_FORMATS.get(comp_name),
                    }
                # Re-apply summary cache to all games so summary data always survives.
                with _live_summary_cache_lock:
                    for comp_data in _live_scores.values():
                        for g in comp_data.get("games", []):
                            mid = g.get("match_id")
                            if mid and mid in _live_summary_cache:
                                g.update(_live_summary_cache[mid])
            # Persist any newly completed games to history file.
            _merge_completed_to_history()

            # ── Standings refresh on game completion ────────────────
            # Detect games that just finished this cycle and fetch fresh
            # standings for their competition, updating the cache.
            try:
                comps_to_refresh = set()
                with _live_scores_lock:
                    for comp_name, comp_data in _live_scores.items():
                        for g in comp_data.get("games", []):
                            mid = g.get("match_id")
                            if not mid:
                                continue
                            cur_status = g.get("status", "")
                            prev = prev_statuses.get(mid)
                            if prev and prev[1] != "post" and cur_status == "post":
                                comps_to_refresh.add(comp_name)
                for comp_name in comps_to_refresh:
                    table = _compute_standings_from_history(comp_name)
                    if table:
                        with _real_tables_lock:
                            _real_tables[comp_name] = table
            except Exception:
                import traceback
                traceback.print_exc()

            # ── Fetch summary data for active games ──────────────────
            # - pre:   fetch every cycle so lineup/formation changes are picked up
            # - in:    fetch every cycle (key_events & boxscore change in real time)
            # - post:  fetch once if cache empty (final data capture)
            try:
                games_needing_summary = []
                for comp_name, comp_data in list(_live_scores.items()):
                    espn_id = config.LIVE_SCORE_COMPETITIONS.get(comp_name)
                    if not espn_id:
                        continue
                    for g in comp_data.get("games", []):
                        mid = g.get("match_id")
                        if not mid:
                            continue
                        status = g.get("status", "")
                        if status in ("pre", "in"):
                            games_needing_summary.append((comp_name, espn_id, mid))
                        elif status == "post" and mid not in _live_summary_cache:
                            games_needing_summary.append((comp_name, espn_id, mid))
                if games_needing_summary:
                    def _fetch_summary(args):
                        comp_name, espn_id, match_id = args
                        data = _fetch_event_summary(comp_name, espn_id, match_id)
                        if not data:
                            return None
                        result = {"match_id": match_id, "comp_name": comp_name}
                        lineups = _parse_espn_lineups(data)
                        if lineups:
                            result["lineups"] = lineups
                        h2h = _parse_espn_head_to_head(data)
                        if h2h:
                            result["head_to_head"] = h2h
                        last5 = _parse_espn_last_five(data)
                        if last5:
                            result["last_five"] = last5
                        key_events = _parse_espn_key_events(data)
                        if key_events:
                            result["key_events"] = key_events
                        boxscore = _parse_espn_boxscore_stats(data)
                        if boxscore:
                            result["boxscore_stats"] = boxscore
                        game_info = _parse_espn_game_info(data)
                        if game_info:
                            result["game_info"] = game_info
                        shot_mapping = _parse_espn_shot_mapping(data)
                        if shot_mapping:
                            result["shot_mapping"] = shot_mapping
                        situation = _parse_espn_situation(data)
                        if situation:
                            result["situation"] = situation
                        injuries = _parse_espn_injuries_availability(data)
                        if injuries:
                            result["injuries_availability"] = injuries
                        team_stats = _parse_espn_team_stats(data)
                        if team_stats:
                            result["team_stats"] = team_stats
                        return result
                    with ThreadPoolExecutor(max_workers=min(6, len(games_needing_summary))) as pool:
                        summary_futures = [pool.submit(_fetch_summary, args) for args in games_needing_summary]
                        for ft in as_completed(summary_futures):
                            try:
                                sresult = ft.result()
                                if not sresult:
                                    continue
                                mid = sresult["match_id"]
                                comp_name = sresult["comp_name"]
                                with _live_summary_cache_lock:
                                    cache_entry = _live_summary_cache.get(mid, {})
                                    if "lineups" in sresult:
                                        cache_entry["lineups"] = sresult["lineups"]
                                    if "head_to_head" in sresult:
                                        cache_entry["head_to_head"] = sresult["head_to_head"]
                                    if "last_five" in sresult:
                                        cache_entry["last_five"] = sresult["last_five"]
                                    if "key_events" in sresult:
                                        cache_entry["key_events"] = sresult["key_events"]
                                    if "boxscore_stats" in sresult:
                                        cache_entry["boxscore_stats"] = sresult["boxscore_stats"]
                                    if "game_info" in sresult:
                                        cache_entry["game_info"] = sresult["game_info"]
                                    if "shot_mapping" in sresult:
                                        cache_entry["shot_mapping"] = sresult["shot_mapping"]
                                    if "situation" in sresult:
                                        cache_entry["situation"] = sresult["situation"]
                                    if "injuries_availability" in sresult:
                                        cache_entry["injuries_availability"] = sresult["injuries_availability"]
                                    if "team_stats" in sresult:
                                        cache_entry["team_stats"] = sresult["team_stats"]
                                    _live_summary_cache[mid] = cache_entry
                                # lineups always re-fetched for pre games on the next cycle
                            except Exception:
                                pass
            except Exception:
                import traceback
                traceback.print_exc()

            # Re-apply summary cache to live games so data is available
            # immediately (not delayed one cycle).
            with _live_scores_lock, _live_summary_cache_lock:
                for comp_data in _live_scores.values():
                    for g in comp_data.get("games", []):
                        mid = g.get("match_id")
                        if mid and mid in _live_summary_cache:
                            g.update(_live_summary_cache[mid])
                        # Promote passes from boxscore_stats to home_stats/away_stats
                        _extract_passes_to_stats(g)
                        # Promote granular team_stats (shotsInsideBox, interceptions,
                        # aerialsWon, etc.) into home_stats/away_stats.
                        _promote_team_stats_to_home_away(g)
                        # Append new shots to persistent arrays so the frontend
                        # gets an accumulating shot map / goal locations list.
                        sm = g.get("shot_mapping") or {}
                        for arr_key in ("shot_origins", "goal_locations"):
                            batch = sm.get(arr_key) or []
                            seen = g.get(f"_{arr_key}_len", 0)
                            if len(batch) > seen:
                                new_items = batch[seen:]
                                persistent = g.setdefault(arr_key, [])
                                persistent.extend(new_items)
                                g[f"_{arr_key}_len"] = len(batch)

            # Compute live in-play predictions for active games.
            try:
                prematch_index = _build_live_prematch_index()
                for comp_name in list(_live_scores.keys()):
                    for g in _live_scores[comp_name].get("games", []):
                        if g.get("status") == "in" and _uefa_live_scoring_allowed_for_comp(comp_name):
                            prematch = _match_prematch_record(
                                g.get("home_team", ""), g.get("away_team", ""),
                                comp_name, prematch_index,
                            )
                            lp = _compute_live_prediction(g, prematch)
                            if lp is not None:
                                g["live_prediction"] = lp
                        # Update cumulative momentum for in-progress games
                        if g.get("status") == "in" and _uefa_live_scoring_allowed_for_comp(comp_name):
                            _update_cumulative_momentum(g)
            except Exception:
                import traceback
                traceback.print_exc()
        except Exception:
            import traceback
            traceback.print_exc()

        _sync_friendlies_results_if_due()

        now = datetime.now()
        interval = _compute_poll_interval(now, results, active_comps, todays_comps)
        time.sleep(interval)


def start_live_score_poller() -> None:
    """Start the live score poller thread if not already running.

    Called by BackendServer on Steam Deck (gunicorn) and by ``__main__``
    in dev mode.  Safe to call multiple times — the ``_started`` guard
    ensures only one thread is created.
    """
    if not getattr(start_live_score_poller, "_started", False):
        start_live_score_poller._started = True
        threading.Thread(target=_live_score_poller_loop, daemon=True, name="live-score-poller").start()
        print("[startup] Live score poller started (60s live / 60s 3min pre-kickoff / 15min pre-match / 30min idle).")
