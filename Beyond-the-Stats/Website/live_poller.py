"""Background live score polling thread and ESPN scoreboard merging."""
import hashlib
import importlib.util
import json
import os
import queue
import re
import sys
import threading
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

import config
from competition_rules import canonical_team_name, make_match_sync_key
from accuracy_tracker import _track_prediction_results
from espn_api import _fetch_competition_scores, _fetch_event_summary, LIVE_SCORE_FETCH_TIMEOUT
from espn_parser import (
    _is_halftime_break,
    _parse_elapsed_minutes,
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
    _load_live_score_history,
    _live_history_cutoff,
    _upsert_live_score_history,
    refresh_real_standings_after_live_final,
)

_live_scores: dict[str, dict] = {}
_live_scores_lock = threading.RLock()

_live_summary_cache: dict[str, dict] = {}
_live_summary_cache_lock = threading.Lock()
# Per-match metadata for summary fetch attempts (match_id -> {"ts": float}).
# Kept OUT of _live_summary_cache because cache entries are g.update()d into
# API payloads and must not leak bookkeeping keys to the frontend.
_summary_fetch_meta: dict[str, dict] = {}
# Pre-game summaries re-attempt at most this often. h2h/last-five/injuries are
# static and lineups only appear near kickoff, so polling them every 60s cycle
# wasted dozens of ESPN calls per minute on busy matchdays.
_PRE_SUMMARY_RETRY_S = 600
_notified_events: dict[str, set] = {}
_notified_events_lock = threading.Lock()
_match_tracker: dict[str, dict] = {}
_match_tracker_lock = threading.Lock()
_last_friendlies_sync_ts = 0.0
_FRIENDLIES_SYNC_INTERVAL_S = 900

_last_deferred_poll_ts = 0.0
_DEFERRED_POLL_INTERVAL_S = 300


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


def _flush_completed_live_scores_to_history():
    """Persist finished in-memory games to live_score_history.json.

    Caller must already hold ``_live_scores_lock`` (or be okay with a nested
    RLock acquisition via ``_merge_completed_to_history``).
    """
    return _merge_completed_to_history()


# ── Result finalization bookkeeping ────────────────────────────────────
# Every caller of _merge_completed_to_history (poller loop, on-demand refresh,
# overnight rollover) records finished / corrected results here; the poller
# drains it once per cycle (_drain_finalized_results) so league tables, team
# form, overviews and response caches update no matter which path saw the
# final whistle first.
_past_synced_scores: dict[str, tuple] = {}
_result_pending_lock = threading.Lock()
_pending_result_comps: set[str] = set()
_pending_result_teams: set[str] = set()
_result_drain_lock = threading.Lock()

# Response-cache path prefixes whose payloads depend on match results.
_RESULT_DEPENDENT_ENDPOINTS = (
    "/api/league-data", "/api/league-tables", "/api/real-tables",
    "/api/competition-data", "/api/cup-data", "/api/real-cup-data",
    "/api/cup-bracket", "/api/tournament", "/api/world-cup", "/api/stats",
    "/api/scorers", "/api/team", "/api/past-games", "/api/past-live-scores",
    "/api/h2h", "/api/upcoming", "/api/home", "/api/mobile/widget",
)


def _is_synthetic_game(game: dict, comp_name: str = "") -> bool:
    mid = str(game.get("match_id", "") or "").strip().lower()
    comp = str(comp_name or game.get("competition", "") or "")
    return (
        mid.startswith("test-")
        or mid.startswith("sim-")
        or "test-past-games" in mid
        or comp.startswith("Test/")
    )


def _result_signature(game: dict) -> tuple:
    return (
        str(game.get("home_score", "")).strip(),
        str(game.get("away_score", "")).strip(),
    )


def _result_memo_key(game: dict) -> str:
    mid = str(game.get("match_id", "") or "").strip()
    if mid:
        return f"id:{mid}"
    return f"sync:{game.get('sync_key') or ''}|{game.get('home_team')}|{game.get('away_team')}"


def _sync_finished_games_to_past_games(games) -> int:
    """Write finished live games to SQLite ``past_games`` right away.

    Previously finals only reached ``past_games`` during the nightly full
    pipeline archive, so team last-5 / momentum / H2H (which read
    ``past_games``) lagged a day behind the live tables. Rows reuse the
    archive's converter (ET match date, canonical names, sync key) and merge
    the pre-match prediction so the stored row matches ``/api/past-games``.
    Upserts are idempotent with the nightly archive (same storage key).
    """
    games = [g for g in (games or []) if isinstance(g, dict)]
    if not games:
        return 0
    try:
        from predictions import (
            _build_past_game_prediction_lookup,
            _is_placeholder_game,
            _json_safe_row,
            _live_game_to_past_row,
            _merge_prediction_onto_past_row,
        )
        from shared import sqlite_store as _store
    except Exception as exc:
        print(f"[past-games] live result sync unavailable: {exc}")
        return 0

    rows: list[dict] = []
    lookup = None
    for g in games:
        if str(g.get("status", "")).strip().lower() != "post":
            continue
        if _is_synthetic_game(g):
            continue
        row = _live_game_to_past_row(g, g.get("competition"))
        if not row or _is_placeholder_game(row):
            continue
        if lookup is None:
            try:
                lookup = _build_past_game_prediction_lookup()
            except Exception:
                lookup = {}
        try:
            row = _merge_prediction_onto_past_row(dict(row), lookup)
        except Exception:
            pass
        rows.append(_json_safe_row(row))
    if not rows:
        return 0
    _store.upsert_past_games(rows)
    return len(rows)


def _merge_completed_to_history():
    """Move finished games from _live_scores into persistent history.

    Stores all game data including summary fields (lineups, h2h, key events,
    boxscore stats) that were merged onto game objects from ``_live_summary_cache``.

    Always upserts current ``post`` games (not only first-seen) so a later
    summary fetch can enrich the SQLite/JSON row with full stats. SQLite never
    prunes these rows.

    Newly finished games (and post-FT score corrections) are also written to
    ``past_games`` and queued for ``_drain_finalized_results`` (standings,
    team form, overview and response-cache refresh).

    Returns the set of competition names that received newly completed games.
    """
    history = _load_live_score_history()
    historic_ids = {g["match_id"] for g in history if g.get("match_id")}
    new_games = []
    all_post = []
    cleared_standings = set()
    with _live_scores_lock, _live_summary_cache_lock:
        for comp_name, comp_data in _live_scores.items():
            for g in comp_data.get("games", []):
                status = str(g.get("status", "")).strip().lower()
                if status != "post":
                    continue
                entry = dict(g)
                entry.setdefault("competition", comp_name)
                entry.setdefault("completed_at", datetime.now(timezone.utc).isoformat())
                mid = entry.get("match_id")
                if mid:
                    cached = _live_summary_cache.get(mid) or {}
                    if cached:
                        for key, value in cached.items():
                            if value not in (None, "", [], {}):
                                entry[key] = value
                _stamp_game_sync_fields(entry, comp_name)
                all_post.append(entry)
                if mid and mid not in historic_ids:
                    new_games.append(entry)
                    historic_ids.add(mid)
                    cleared_standings.add(comp_name)
                elif not mid:
                    new_games.append(entry)
                    cleared_standings.add(comp_name)

    # Decide which finals must be (re)written to past_games: first time this
    # process sees them, or the score changed after full time (VAR/admin fix).
    new_keys = {_result_memo_key(g) for g in new_games}
    to_sync: list[dict] = []
    finalized: list[dict] = []
    for entry in all_post:
        if _is_synthetic_game(entry):
            continue
        memo_key = _result_memo_key(entry)
        sig = _result_signature(entry)
        prev_sig = _past_synced_scores.get(memo_key)
        if prev_sig == sig:
            continue
        to_sync.append(entry)
        if memo_key in new_keys or prev_sig is not None:
            # Brand-new final, or a corrected score → tables/form must change.
            finalized.append(entry)
            cleared_standings.add(entry.get("competition", ""))

    cleared_standings.discard("")
    for comp in cleared_standings:
        _clear_standings_cache(comp)
        _clear_leaders_cache(comp)
    if all_post:
        _upsert_live_score_history(all_post)
    if to_sync:
        try:
            _sync_finished_games_to_past_games(to_sync)
            for entry in to_sync:
                _past_synced_scores[_result_memo_key(entry)] = _result_signature(entry)
        except Exception as exc:
            print(f"[past-games] live result sync failed: {exc}")
    if finalized or cleared_standings:
        with _result_pending_lock:
            _pending_result_comps.update(cleared_standings)
            for entry in finalized:
                comp = entry.get("competition", "")
                for side in ("home_team", "away_team"):
                    name = str(entry.get(side, "") or "").strip()
                    if name:
                        _pending_result_teams.add(name)
                        canon = canonical_team_name(name, comp)
                        if canon:
                            _pending_result_teams.add(canon)
    # Track predictions for newly completed games against our CSV predictions.
    if new_games:
        _track_prediction_results(new_games)
    return cleared_standings


def _invalidate_result_dependent_caches(teams) -> None:
    """Drop every cache whose payload embeds match results or team form."""
    steps = []

    def _step(label, fn):
        try:
            fn()
        except Exception as exc:
            steps.append(f"{label}: {exc}")

    def _analytics():
        from match_analytics import invalidate_match_history_cache
        invalidate_match_history_cache()

    def _form():
        from predictions import clear_team_form_caches
        clear_team_form_caches()

    def _next_game():
        from routes.predictions import _invalidate_team_next_game_index
        _invalidate_team_next_game_index()

    def _leaders():
        from routes.leagues import clear_league_leaders_cache
        clear_league_leaders_cache()

    def _responses():
        from cache import _cache_clear_endpoints
        _cache_clear_endpoints(_RESULT_DEPENDENT_ENDPOINTS)

    def _overviews():
        from team_overview import refresh_team_overviews_after_results
        refresh_team_overviews_after_results(sorted(teams))

    _step("analytics", _analytics)
    _step("form", _form)
    _step("next-game", _next_game)
    _step("leaders", _leaders)
    # Overviews last: their rebuild reads the freshly invalidated sources.
    _step("overviews", _overviews)
    _step("responses", _responses)
    if steps:
        print(f"[results] cache invalidation issues: {'; '.join(steps)}")


def _drain_finalized_results(extra_comps=()) -> list[str]:
    """Apply queued results: recompute real tables, then invalidate caches.

    Safe to call from any thread; concurrent calls coalesce (the running
    drain loops until the queue is empty). Returns refreshed competitions.
    """
    with _result_pending_lock:
        _pending_result_comps.update(c for c in (extra_comps or ()) if c)
    if not _result_drain_lock.acquire(blocking=False):
        return []
    refreshed: list[str] = []
    try:
        while True:
            with _result_pending_lock:
                comps = sorted(_pending_result_comps)
                teams = set(_pending_result_teams)
                _pending_result_comps.clear()
                _pending_result_teams.clear()
            if not comps and not teams:
                break
            for comp_name in comps:
                try:
                    refreshed.extend(refresh_real_standings_after_live_final(comp_name))
                except Exception:
                    import traceback
                    traceback.print_exc()
            _invalidate_result_dependent_caches(teams)
            if teams:
                print(
                    f"[results] finalized {len(teams)} team result(s); "
                    f"tables refreshed: {', '.join(comps) or 'none'}"
                )
    finally:
        _result_drain_lock.release()
    return refreshed


def _sync_backfilled_finals_to_past_games(games) -> None:
    """Startup catch-up: finals that never reached past_games (e.g. finished
    while the backend was down, or before live sync existed)."""
    try:
        n = _sync_finished_games_to_past_games(games)
        for g in games or []:
            if str(g.get("status", "")).strip().lower() == "post":
                _past_synced_scores.setdefault(_result_memo_key(g), _result_signature(g))
        if n:
            print(f"[past-games] startup sync: {n} recent final(s) upserted to past_games")
    except Exception as exc:
        print(f"[past-games] startup sync failed: {exc}")


def _persist_post_games_full_stats():
    """Re-upsert finished games after summary enrichment (lineups/boxscore/etc.)."""
    enriched = []
    with _live_scores_lock, _live_summary_cache_lock:
        for comp_name, comp_data in _live_scores.items():
            for g in comp_data.get("games", []):
                if str(g.get("status", "")).strip().lower() != "post":
                    continue
                entry = dict(g)
                entry.setdefault("competition", comp_name)
                entry.setdefault("completed_at", datetime.now(timezone.utc).isoformat())
                mid = entry.get("match_id")
                if mid and mid in _live_summary_cache:
                    entry.update(_live_summary_cache[mid])
                _stamp_game_sync_fields(entry, comp_name)
                # Only rewrite when we actually have richer detail.
                has_detail = any(
                    entry.get(key)
                    for key in (
                        "lineups",
                        "boxscore_stats",
                        "key_events",
                        "home_stats",
                        "away_stats",
                        "game_info",
                        "h2h",
                    )
                )
                if has_detail or mid:
                    enriched.append(entry)
    if enriched:
        _upsert_live_score_history(enriched)
    return len(enriched)


def _effective_poller_date():
    """Return the primary ET calendar date for live-score polling.

    The backend host runs in Eastern Time; all live-score day boundaries use
    ``America/New_York``. Before 2am ET, yesterday's slate can still be active,
    so we return yesterday. From 2am ET onward the primary date is today.
    """
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.hour < 2:
        return (now_et - timedelta(days=1)).date()
    return now_et.date()


def _poll_dates_for_cycle():
    """ESPN scoreboard dates (ET) for this poll cycle.

    - Before 2am ET: yesterday + today (late finishers + early kickoffs).
    - 2am–6am ET: today + yesterday (spillover finals only briefly).
    - After 6am ET: today only.

    Dual-date mornings used to leave yesterday's completed games stuck in
    ``_live_scores`` all afternoon because the day-boundary clear only fires
    when the *primary* date changes. Callers must prune memory to these dates.
    """
    now_et = datetime.now(ZoneInfo("America/New_York"))
    today = now_et.date()
    yesterday = today - timedelta(days=1)
    if now_et.hour < 2:
        return [yesterday, today]
    if now_et.hour < 6:
        return [today, yesterday]
    return [today]


def _stamp_scoreboard_date(games, date_str):
    """Tag each game with the ESPN scoreboard date it was fetched for (YYYYMMDD)."""
    stamped = []
    for game in games or []:
        entry = dict(game)
        entry["scoreboard_date"] = str(date_str)
        stamped.append(entry)
    return stamped


def _game_et_calendar_date(game):
    """Best-effort ET calendar date for a live-score game dict."""
    raw = game.get("scoreboard_date")
    if raw:
        text = str(raw).strip()
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text[:8], fmt).date()
            except ValueError:
                continue
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            pass
    for key in ("kickoff_et", "kickoff_utc", "match_date", "date", "start_time"):
        value = game.get(key)
        if not value:
            continue
        val_str = str(value).strip()
        if len(val_str) == 10 and val_str.count("-") == 2:
            try:
                return date.fromisoformat(val_str)
            except ValueError:
                pass
        try:
            dt = pd.to_datetime(value, utc=True, errors="coerce")
            if pd.notna(dt):
                if getattr(dt, "tzinfo", None) is None and hasattr(dt, "tz_localize"):
                    dt = dt.tz_localize("UTC")
                return dt.tz_convert(ZoneInfo("America/New_York")).date()
        except Exception:
            try:
                return date.fromisoformat(str(value)[:10])
            except Exception:
                continue
    return None


def _stamp_game_sync_fields(game: dict, comp_name: str) -> None:
    """Attach canonical team names and deterministic sync_key to a live game object."""
    if not isinstance(game, dict):
        return
    home_raw = game.get("home_team", "")
    away_raw = game.get("away_team", "")
    c_home = canonical_team_name(home_raw, comp_name) or str(home_raw or "")
    c_away = canonical_team_name(away_raw, comp_name) or str(away_raw or "")
    game["canonical_home_team"] = c_home
    game["canonical_away_team"] = c_away

    game_date = _game_et_calendar_date(game)
    date_val = game_date.isoformat() if game_date else (game.get("match_date") or game.get("scoreboard_date"))
    game["sync_key"] = make_match_sync_key(date_val, comp_name, c_home, c_away)


def _keep_game_for_poll_window(game, allowed_dates):
    """Whether a game belongs in the current ET poll window.

    In-progress games are always kept (can spill past midnight). Completed and
    upcoming games must fall on one of the allowed ET scoreboard dates.
    """
    if game.get("simulated"):
        return True
    status = str(game.get("status") or "").strip().lower()
    if status == "in":
        return True
    game_date = _game_et_calendar_date(game)
    if game_date is None:
        return False
    return game_date in allowed_dates


def _prune_live_scores_to_dates(allowed_dates):
    """Drop competitions/games outside the active ET poll window (in-place)."""
    allowed = set(allowed_dates or [])
    if not allowed:
        return
    stale_comps = []
    for comp_name, comp_data in list(_live_scores.items()):
        games = [
            g for g in (comp_data.get("games") or [])
            if _keep_game_for_poll_window(g, allowed)
        ]
        if not games:
            stale_comps.append(comp_name)
            continue
        comp_data["games"] = games
    for comp_name in stale_comps:
        _live_scores.pop(comp_name, None)


def _backfill_recent_live_score_history():
    """Recover the retained history window from ESPN at poller startup."""
    today = datetime.now(ZoneInfo("America/New_York")).date()
    date_range = f"{_live_history_cutoff(today).strftime('%Y%m%d')}-{today.strftime('%Y%m%d')}"
    competitions = {}
    for comp_name, espn_id in config.LIVE_SCORE_COMPETITIONS.items():
        if espn_id and espn_id not in competitions:
            competitions[espn_id] = comp_name

    completed = []
    if competitions:
        with ThreadPoolExecutor(max_workers=min(8, len(competitions))) as pool:
            futures = {
                pool.submit(_fetch_competition_scores, comp_name, espn_id, date_range): comp_name
                for espn_id, comp_name in competitions.items()
            }
            for future in as_completed(futures):
                comp_name = futures[future]
                try:
                    games = future.result()
                except Exception:
                    continue
                for game in games:
                    if str(game.get("status", "")).strip().lower() != "post":
                        continue
                    entry = dict(game)
                    entry.setdefault("competition", comp_name)
                    entry.setdefault("completed_at", datetime.now(timezone.utc).isoformat())
                    completed.append(entry)

    if completed:
        result = _upsert_live_score_history(completed, as_of=today)
        print(
            "[live-history] ESPN retained-window backfill: "
            f"{len(completed)} final(s), {result['inserted']} inserted, "
            f"{result['updated']} updated, {result['total']} total."
        )
        for entry in completed:
            _stamp_game_sync_fields(entry, entry.get("competition", ""))
        _sync_backfilled_finals_to_past_games(completed)
    return len(completed)


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


# ── Live Activity content-state (APNs) ────────────────────────────
# The iOS app's ActivityKit ``ContentState`` expects these exact keys.
# ``half_start_time`` / ``kickoff_date`` are ISO8601 UTC strings ("...Z").

_la_prematch_index_cache: dict = {}
_la_prematch_index_ts: float = 0.0


def _la_prematch_record(game, comp_name):
    """Pre-match record for a game, using a short TTL cache of the index."""
    global _la_prematch_index_cache, _la_prematch_index_ts
    now = time.time()
    if not _la_prematch_index_cache or now - _la_prematch_index_ts > 120:
        try:
            _la_prematch_index_cache = _build_live_prematch_index()
            _la_prematch_index_ts = now
        except Exception:
            pass
    if not _la_prematch_index_cache:
        return None
    try:
        return _match_prematch_record(
            game.get("home_team", ""),
            game.get("away_team", ""),
            comp_name,
            _la_prematch_index_cache,
        )
    except Exception:
        return None


def _la_kickoff_iso(game) -> str:
    raw = game.get("kickoff_utc") or game.get("match_datetime_utc") or ""
    try:
        dt = pd.to_datetime(str(raw), utc=True, errors="coerce")
        if pd.notna(dt):
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        pass
    return ""


def _la_half_start_iso(game, kickoff_iso: str) -> str:
    """ISO8601 UTC time the current half kicked off (drives the count-up timer)."""
    period = str(game.get("period") or "").strip().lower()
    now = datetime.now(timezone.utc)
    elapsed = _parse_elapsed_minutes(game.get("clock", ""), game.get("period", ""))
    if "extra" in period or period.startswith("et") or "overtime" in period:
        base = 105 if "2nd" in period else 90
        within = max(0, elapsed - base)
        return (now - timedelta(minutes=within)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if "2nd half" in period or "second half" in period:
        within = max(0, elapsed - 45)
        return (now - timedelta(minutes=within)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if kickoff_iso:
        return kickoff_iso
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _la_probabilities(game, comp_name, live_prediction):
    for src in (live_prediction, _la_prematch_record(game, comp_name)):
        if not src:
            continue
        try:
            ph = float(src.get("prob_home"))
            pdraw = float(src.get("prob_draw"))
            pa = float(src.get("prob_away"))
        except (TypeError, ValueError):
            continue
        total = ph + pdraw + pa
        if total > 0:
            return (round(ph / total, 4), round(pdraw / total, 4), round(pa / total, 4))
    third = round(1.0 / 3.0, 4)
    return (third, third, third)


def _la_score(value) -> int | None:
    """Coerce a scoreboard score to int (or None) so ContentState `Int?` decoding never fails on "-"."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


# ── Live Activity match clock (Apple's built-in timer) ───────────
# ActivityKit decodes content-state with Swift's default JSONDecoder, which
# reads ``Date`` as seconds since 2001-01-01 (timeIntervalSinceReferenceDate).
# Sending timer fields in that unit lets the widget declare them as ``Date``
# and drive ``Text(timerInterval:pauseTime:countsDown:)`` directly, so the
# system ticks the clock locally — no per-second pushes.
_SWIFT_REFERENCE_EPOCH = 978307200.0  # 2001-01-01T00:00:00Z as Unix seconds
_la_timer_anchors: dict[str, dict] = {}
_la_timer_lock = threading.Lock()
# Re-anchor only when the computed zero point drifts more than this (seconds),
# so minute-granularity ESPN clocks don't make the on-device timer jump.
_LA_TIMER_DRIFT_TOLERANCE_S = 75.0


def _swift_date(ts: float | None) -> float | None:
    """Unix seconds → Swift ``Date`` (reference-date seconds) for content-state."""
    return None if ts is None else round(float(ts) - _SWIFT_REFERENCE_EPOCH, 3)


def _la_period_base_minutes(period: str) -> int:
    """Match minute at which the current period starts (0 / 45 / 90 / 105)."""
    p = str(period or "").strip().lower()
    if p.startswith("et") or "extra" in p:
        return 105 if "2nd" in p or "second" in p else 90
    if "2nd" in p or "second" in p:
        return 45
    return 0


def _la_clock_state(game) -> str:
    """"pre" | "running" | "paused" (HT / ET HT / end of regular time) | "stopped" (FT / shootout)."""
    status = str(game.get("status", "")).strip().lower()
    status_type = str(game.get("status_type", "")).strip().upper()
    period = str(game.get("period", "")).strip().lower()
    if status == "pre":
        return "pre"
    if status == "post":
        return "stopped"
    if "pen" in period or "shootout" in period or status_type == "STATUS_SHOOTOUT":
        return "stopped"
    if status_type == "STATUS_END_REGULAR_TIME" or _is_halftime_break(
        game.get("period"), game.get("status_type"), game.get("clock")
    ):
        return "paused"
    return "running"


def _la_elapsed_seconds(game) -> float:
    """Elapsed match time in seconds (ESPN raw clock when available, else minute display)."""
    raw = game.get("clock_seconds")
    base = _la_period_base_minutes(game.get("period", "")) * 60
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return max(float(raw), float(base))
    minutes = _parse_elapsed_minutes(game.get("clock", ""), game.get("period", ""))
    if minutes <= 0:
        return float(base)
    # ESPN shows the minute in progress ("23'" ≈ 22:00–22:59) → aim mid-minute.
    return max(float(base), minutes * 60.0 - 30.0)


def _la_pause_elapsed_seconds(game) -> float:
    """Clock value to freeze at during a break (45:00 at HT, 90:00 end of regular time, 105:00 ET HT)."""
    status_type = str(game.get("status_type", "")).strip().upper()
    period = str(game.get("period", "")).strip().lower()
    if status_type == "STATUS_EXTRA_HALFTIME" or "et" in period.split() or period.startswith("et"):
        return 105 * 60.0
    if status_type == "STATUS_END_REGULAR_TIME":
        return 90 * 60.0
    return 45 * 60.0


def _la_timer_fields(game, polled_at: float | None = None) -> dict:
    """Timer fields for the Live Activity clock.

    - ``timer_start``     (Date): virtual 0:00 — ``now - timer_start`` = match time.
    - ``timer_paused_at`` (Date): when set, the clock is frozen at
      ``timer_paused_at - timer_start`` (e.g. 45:00 at halftime).
    - ``timer_running``   (Bool): True while the ball is in play.
    - ``kickoff_time``    (Date): scheduled kickoff (pre-match countdown).
    """
    mid = str(game.get("match_id") or "").strip()
    ref = float(polled_at) if polled_at else time.time()
    state = _la_clock_state(game)
    period = str(game.get("period") or "")
    fields: dict = {
        "timer_running": state == "running",
        "timer_start": None,
        "timer_paused_at": None,
        "kickoff_time": _swift_date(_la_kickoff_ts(game)),
    }
    if state == "pre" or not mid:
        return fields
    with _la_timer_lock:
        prev = _la_timer_anchors.get(mid)
        if state == "running":
            anchor = ref - _la_elapsed_seconds(game)
            if (
                prev
                and prev.get("period") == period
                and abs(float(prev["anchor"]) - anchor) < _LA_TIMER_DRIFT_TOLERANCE_S
            ):
                anchor = float(prev["anchor"])
            _la_timer_anchors[mid] = {"period": period, "anchor": anchor}
            fields["timer_start"] = _swift_date(anchor)
        else:
            frozen = (
                _la_pause_elapsed_seconds(game) if state == "paused"
                else max(_la_elapsed_seconds(game), 90 * 60.0)
            )
            anchor = float(prev["anchor"]) if prev else ref - frozen
            fields["timer_start"] = _swift_date(anchor)
            fields["timer_paused_at"] = _swift_date(anchor + frozen)
    return fields


def _la_forget_timer(match_id: str) -> None:
    with _la_timer_lock:
        _la_timer_anchors.pop(str(match_id or "").strip(), None)


def _la_content_state(game, comp_name, polled_at: float | None = None) -> dict:
    """Full Live Activity content-state in the iOS app's expected schema.

    Every update/end/start push carries this complete shape (plus ``event`` /
    ``event_text`` labels) so ActivityKit can always decode it into the app's
    ContentState type. Scores are int or null; ``half_start_time`` /
    ``kickoff_date`` are ISO-8601 strings; ``timer_start`` / ``timer_paused_at``
    / ``kickoff_time`` are Swift ``Date`` values (reference-date seconds).
    """
    kickoff_iso = _la_kickoff_iso(game)
    prob_home, prob_draw, prob_away = _la_probabilities(
        game, comp_name, game.get("live_prediction")
    )
    timer = _la_timer_fields(game, polled_at=polled_at)
    half_start_iso = _la_half_start_iso(game, kickoff_iso)
    if timer.get("timer_running") and timer.get("timer_start") is not None:
        half_start_ts = (
            timer["timer_start"] + _SWIFT_REFERENCE_EPOCH
            + _la_period_base_minutes(game.get("period", "")) * 60
        )
        half_start_iso = datetime.fromtimestamp(half_start_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        **timer,
        "match_id": str(game.get("match_id") or ""),
        "sync_key": game.get("sync_key", ""),
        "competition": comp_name,
        "home_team": game.get("home_team", ""),
        "away_team": game.get("away_team", ""),
        "home_score": _la_score(game.get("home_score")),
        "away_score": _la_score(game.get("away_score")),
        "status": game.get("status", ""),
        "clock": game.get("clock", ""),
        "period": game.get("period", ""),
        "home_team_logo": game.get("home_team_logo", ""),
        "away_team_logo": game.get("away_team_logo", ""),
        "match_minute": _parse_elapsed_minutes(game.get("clock", ""), game.get("period", "")),
        "prob_home": prob_home,
        "prob_draw": prob_draw,
        "prob_away": prob_away,
        "half_start_time": half_start_iso,
        "kickoff_date": kickoff_iso,
    }


def _la_attributes(game, comp_name) -> dict:
    """Static ActivityAttributes for push-to-start payloads (fixed for the activity's lifetime)."""
    return {
        "match_id": str(game.get("match_id") or ""),
        "sync_key": game.get("sync_key", ""),
        "competition": comp_name,
        "home_team": game.get("home_team", ""),
        "away_team": game.get("away_team", ""),
        "home_team_logo": game.get("home_team_logo", ""),
        "away_team_logo": game.get("away_team_logo", ""),
        "kickoff_date": _la_kickoff_iso(game),
    }


def _polled_ts(comp_data) -> float | None:
    """Epoch seconds of a competition's last ESPN poll (when its clock values were sampled)."""
    raw = comp_data.get("last_polled_utc") if isinstance(comp_data, dict) else None
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        value = ts.timestamp()
    except ValueError:
        return None
    # Ignore implausible values (clock skew / stale fixtures in tests).
    now = time.time()
    return value if now - 600 <= value <= now + 5 else None


def _la_kickoff_ts(game) -> float | None:
    """Scheduled kickoff as epoch seconds (None if unknown)."""
    iso = _la_kickoff_iso(game)
    if not iso:
        return None
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _la_stale_date(game) -> int:
    """stale-date: kickoff + configured window, so an activity whose "end" never arrives shows as outdated."""
    after = int(getattr(config, "APNS_LA_STALE_AFTER_KICKOFF_SECONDS", 10800))
    ko = _la_kickoff_ts(game)
    base = ko if ko is not None else time.time()
    return int(max(base + after, time.time() + 1800))


def _upcoming_csv_scan_paths():
    """All fixture CSV paths the poller should consult for today's competitions."""
    seen = set()
    paths = []
    for csv_path in config.UPCOMING_CSV_FILES.values():
        if csv_path and csv_path not in seen:
            seen.add(csv_path)
            paths.append(csv_path)
    for csv_path in (
        config.ALL_UPCOMING_FILE,
        config.FOUR_WEEK_WINDOW_FILE,
        os.path.join(config.PROJECT_DIR, "Output", "Europe", "Upcoming", "europe_upcoming.csv"),
        os.path.join(config.PROJECT_DIR, "Output", "National", "Upcoming", "national_upcoming.csv"),
    ):
        if csv_path and csv_path not in seen:
            seen.add(csv_path)
            paths.append(csv_path)
    return paths


def _scan_upcoming_csv_for_today(csv_path, today_date, todays):
    """Add today's kickoffs from one upcoming CSV into *todays*."""
    if not csv_path or not os.path.exists(csv_path):
        return
    try:
        frame = pd.read_csv(csv_path, dtype=str)
    except Exception:
        return
    for _, row in frame.iterrows():
        comp = str(row.get("competition", "") or "").strip()
        live_comp = config.resolve_live_competition(comp)
        if not live_comp:
            continue
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
        todays[live_comp].append(kickoff_et)


def _discover_todays_competitions_from_espn(today_date, date_str):
    """Fallback: ask ESPN which competitions have fixtures on *today_date*."""
    competitions = {}
    for comp_name, espn_id in config.LIVE_SCORE_COMPETITIONS.items():
        if espn_id and espn_id not in competitions:
            competitions[espn_id] = comp_name
    if not competitions:
        return {}

    discovered = defaultdict(list)
    with ThreadPoolExecutor(max_workers=min(8, len(competitions))) as pool:
        futures = {
            pool.submit(_fetch_competition_scores, comp_name, espn_id, date_str): comp_name
            for espn_id, comp_name in competitions.items()
        }
        for future in as_completed(futures):
            comp_name = futures[future]
            try:
                games = future.result()
            except Exception:
                continue
            if not games:
                continue
            for game in games:
                kickoff_et = None
                kickoff_raw = str(game.get("kickoff_utc") or game.get("match_datetime_utc") or "").strip()
                if kickoff_raw:
                    try:
                        dt_utc = pd.to_datetime(kickoff_raw, errors="coerce")
                        if not pd.isna(dt_utc):
                            if dt_utc.tz is None:
                                dt_utc = dt_utc.tz_localize("UTC")
                            kickoff_et = dt_utc.tz_convert(ZoneInfo("America/New_York"))
                    except Exception:
                        kickoff_et = None
                if kickoff_et is None:
                    match_date = str(game.get("match_date") or "").strip()
                    if match_date:
                        try:
                            dt = pd.to_datetime(match_date, errors="coerce", dayfirst=False)
                            if not pd.isna(dt):
                                if dt.tz is None:
                                    kickoff_et = dt.tz_localize(ZoneInfo("America/New_York"))
                                else:
                                    kickoff_et = dt.tz_convert(ZoneInfo("America/New_York"))
                        except Exception:
                            kickoff_et = None
                if kickoff_et is None or kickoff_et.date() == today_date:
                    discovered[comp_name].append(kickoff_et or datetime.now(ZoneInfo("America/New_York")))
    if discovered:
        print(
            "[live-poller] ESPN discovery fallback found "
            f"{len(discovered)} competition(s) for {today_date.isoformat()}."
        )
    return {k: sorted(v) for k, v in discovered.items()}


def _get_todays_competitions(today_date=None):
    """Return {competition: [kickoff_et, ...]} for competitions with games today.

    Checks all available data sources:
      1. Upcoming predictions CSVs (club, MLS, extra, cups, national team)
      2. Merged Output/Upcoming CSVs (all_upcoming, four_week_window, regional)
      3. World Cup projection JSON (group_fixtures + knockout rounds)
      4. Cup bracket JSON (knockout fixtures)

    Args:
        today_date: date override (defaults to today's date in Eastern Time).
    """
    if today_date is None:
        today_date = datetime.now(ZoneInfo("America/New_York")).date()
    now_et = datetime.now(ZoneInfo("America/New_York"))
    todays = defaultdict(list)

    # ── Source 1: upcoming predictions + merged fixture CSVs ─────
    for csv_path in _upcoming_csv_scan_paths():
        _scan_upcoming_csv_for_today(csv_path, today_date, todays)

    # ── Source 2: World Cup projection JSON ──────────────────────
    if "International/World Cup" not in todays and os.path.exists(config.WORLD_CUP_PROJECTION_FILE):
        try:
            with open(config.WORLD_CUP_PROJECTION_FILE, "r", encoding="utf-8") as fh:
                wc_data = json.load(fh)
        except Exception:
            wc_data = {}

        # group_fixtures — list of dicts with match_date, match_datetime_utc etc.
        for fixture in wc_data.get("group_fixtures") or []:
            if isinstance(fixture, dict):
                _add_if_today(fixture, "International/World Cup", today_date, now_et, todays)

        # knockout rounds
        for round_list in (wc_data.get("knockout") or {}).values():
            if isinstance(round_list, list):
                for match in round_list:
                    if isinstance(match, dict):
                        _add_if_today(match, "International/World Cup", today_date, now_et, todays)

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


def _compute_poll_interval(results, todays_comps):
    """Return the sleep interval in seconds before the next poll cycle.

    - 60 seconds  if any game is in-progress or just kicked off (past 90 min),
                  or within 3 minutes of a scheduled kickoff (catches pre→in
                  transition within seconds).
    - 900 seconds (15 min) when a game kicks off within the next 60 minutes
    - wakes up at nearest_future − 1h when games are 1-2h away (drops to
      15-min pre-match polling mode at the 1h mark)
    - 1800 seconds otherwise (no games or all finished)
    """
    try:
        # Check live results for in-progress games
        for comp_data in (results or {}).values():
            for g in (comp_data or {}).get("games", []) or []:
                if g.get("status") == "in":
                    return 60

        now_et = datetime.now(ZoneInfo("America/New_York"))
        nearest_future = None

        for _comp_name, kickoffs in (todays_comps or {}).items():
            for kt in kickoffs or []:
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
            return max(10, int(wake_at))

        return 1800
    except Exception:
        # Never let interval math kill the poller thread.
        return 60


_last_on_demand_refresh_ts = 0.0
_ON_DEMAND_REFRESH_MIN_INTERVAL_S = 45.0
_on_demand_refresh_lock = threading.Lock()
_poller_last_cycle_utc: str | None = None
_poller_last_cycle_games = 0
_poller_started = False
_poller_thread: threading.Thread | None = None


def _poller_thread_is_alive() -> bool:
    thread = _poller_thread
    return thread is not None and thread.is_alive()


def get_live_poller_status() -> dict:
    """Lightweight poller health snapshot for API responses.

    Also restarts the poller thread if it exited (e.g. after an uncaught
    NameError) while ``started`` was left True — that stuck state is exactly
    what left scores frozen at the first cycle in production.
    """
    if _poller_started and not _poller_thread_is_alive():
        start_live_score_poller(force=True)
    with _live_scores_lock:
        total_games = sum(len(v.get("games", [])) for v in _live_scores.values())
        last_polled = max(
            (v.get("last_polled_utc", "") for v in _live_scores.values()),
            default=None,
        )
        competitions = list(_live_scores.keys())
    return {
        "started": _poller_started,
        "thread_alive": _poller_thread_is_alive(),
        "last_cycle_utc": _poller_last_cycle_utc,
        "last_cycle_games": _poller_last_cycle_games,
        "competitions_in_memory": competitions,
        "total_games_in_memory": total_games,
        "last_polled_utc": last_polled,
        "effective_poll_date": _effective_poller_date().isoformat(),
        "poll_dates": [d.isoformat() for d in _poll_dates_for_cycle()],
    }


# ── Pre-serialized payload cache and Server-Sent Events (SSE) ────────

_COMPACT_LIVE_EXCLUDED_KEYS = frozenset({
    "lineups",
    "key_events",
    "events",
    "assists",
    "boxscore_stats",
    "shot_mapping",
    "shot_origins",
    "goal_locations",
    "_shot_origins_len",
    "_goal_locations_len",
    "injuries_availability",
    "head_to_head",
    "h2h",
    "last_five",
    "game_info",
    "situation",
    "team_stats",
    "home_stats",
    "away_stats",
    "cumulative_momentum",
    "momentum",
    "broadcasts",
    "weather",
    "officials",
})


def to_compact_live_game(game: dict) -> dict:
    """Return a compact representation of a live match with scores and status,

    omitting heavy fields such as lineups, key_events, boxscore stats, and shot maps.
    """
    if not isinstance(game, dict):
        return {}
    return {
        k: v
        for k, v in game.items()
        if k not in _COMPACT_LIVE_EXCLUDED_KEYS and v is not None
    }


def to_compact_live_scores_data(data: dict) -> dict:
    """Transform a full competitions live-scores dictionary into its compact variant."""
    if not isinstance(data, dict):
        return {}
    compact_comps = {}
    for comp_name, comp_data in data.items():
        if not isinstance(comp_data, dict):
            continue
        entry = dict(comp_data)
        games = comp_data.get("games") or []
        entry["games"] = [to_compact_live_game(g) for g in games]
        compact_comps[comp_name] = entry
    return compact_comps


_cached_live_scores_bytes: bytes = b""
_cached_live_scores_etag: str = ""
_cached_sse_frame: str = ""
_cached_compact_live_scores_bytes: bytes = b""
_cached_compact_live_scores_etag: str = ""
_cached_compact_sse_frame: str = ""
_cached_live_scores_lock = threading.Lock()

_sse_subscribers: dict[queue.Queue, bool] = {}
_sse_lock = threading.Lock()


def register_sse_subscriber(compact: bool = False) -> queue.Queue:
    """Register a new SSE client queue (optionally receiving compact payloads)."""
    q: queue.Queue = queue.Queue(maxsize=30)
    with _sse_lock:
        _sse_subscribers[q] = bool(compact)
    return q


def unregister_sse_subscriber(q: queue.Queue) -> None:
    """Unregister an SSE client queue."""
    with _sse_lock:
        _sse_subscribers.pop(q, None)


def _broadcast_sse_update(
    raw_bytes: bytes,
    compact_bytes: bytes | None = None,
    sse_frame: str | None = None,
    compact_sse_frame: str | None = None,
) -> None:
    """Broadcast pre-serialized live scores payload to all active SSE subscriber queues."""
    with _sse_lock:
        if not _sse_subscribers:
            return
        subs = list(_sse_subscribers.items())

    # Pre-compute frames if not provided
    if sse_frame is None and raw_bytes:
        sse_frame = f"data: {raw_bytes.decode('utf-8')}\n\n"
    if compact_sse_frame is None and compact_bytes is not None:
        compact_sse_frame = f"data: {compact_bytes.decode('utf-8')}\n\n"

    for q, is_compact in subs:
        msg = compact_sse_frame if (is_compact and compact_sse_frame is not None) else sse_frame
        if msg is None:
            msg = compact_bytes if (is_compact and compact_bytes is not None) else raw_bytes
        try:
            q.put_nowait(msg)
        except queue.Full:
            try:
                q.get_nowait()
                q.put_nowait(msg)
            except Exception:
                pass
        except Exception:
            pass


def _update_cached_live_scores_payload() -> None:
    """Serialize current live scores into UTF-8 JSON bytes (both full and compact), compute ETags, and notify SSE clients."""
    global _cached_live_scores_bytes, _cached_live_scores_etag
    global _cached_compact_live_scores_bytes, _cached_compact_live_scores_etag
    global _cached_sse_frame, _cached_compact_sse_frame
    status = get_live_poller_status()
    with _live_scores_lock:
        data = {}
        for cname, cdata in _live_scores.items():
            cd = dict(cdata)
            games_list = []
            for g in cdata.get("games", []):
                g_copy = dict(g)
                try:
                    from team_utils import decorate_game_team_names
                    decorate_game_team_names(g_copy, cname)
                except Exception:
                    pass
                games_list.append(g_copy)
            cd["games"] = games_list
            data[cname] = cd

    # 1. Full payload
    payload = {
        "ok": True,
        "competitions": data,
        "poller": status,
    }
    raw_str = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    raw_bytes = raw_str.encode("utf-8")
    etag = f'"{hashlib.md5(raw_bytes).hexdigest()}"'
    sse_frame = f"data: {raw_str}\n\n"

    # 2. Compact payload (scores & status only, omitting lineups/events/stats)
    compact_data = to_compact_live_scores_data(data)
    compact_payload = {
        "ok": True,
        "competitions": compact_data,
        "poller": status,
        "compact": True,
    }
    compact_str = json.dumps(compact_payload, ensure_ascii=False, separators=(",", ":"))
    compact_bytes = compact_str.encode("utf-8")
    compact_etag = f'"{hashlib.md5(compact_bytes).hexdigest()}"'
    compact_sse_frame = f"data: {compact_str}\n\n"

    with _cached_live_scores_lock:
        _cached_live_scores_bytes = raw_bytes
        _cached_live_scores_etag = etag
        _cached_sse_frame = sse_frame
        _cached_compact_live_scores_bytes = compact_bytes
        _cached_compact_live_scores_etag = compact_etag
        _cached_compact_sse_frame = compact_sse_frame

    _broadcast_sse_update(raw_bytes, compact_bytes, sse_frame, compact_sse_frame)


def get_cached_sse_frame() -> str:
    """Return pre-formatted SSE frame ('data: {...}\\n\\n') for full live scores."""
    with _cached_live_scores_lock:
        if _cached_sse_frame:
            return _cached_sse_frame
    raw_bytes, _ = get_cached_live_scores_payload()
    return f"data: {raw_bytes.decode('utf-8')}\n\n"


def get_cached_compact_sse_frame() -> str:
    """Return pre-formatted SSE frame ('data: {...}\\n\\n') for compact live scores."""
    with _cached_live_scores_lock:
        if _cached_compact_sse_frame:
            return _cached_compact_sse_frame
    compact_bytes, _ = get_cached_compact_live_scores_payload()
    return f"data: {compact_bytes.decode('utf-8')}\n\n"


def get_cached_live_scores_payload() -> tuple[bytes, str]:
    """Return pre-serialized (payload_bytes, etag) for ultra-fast, zero-re-serialization responses."""
    with _cached_live_scores_lock:
        if _cached_live_scores_bytes:
            return _cached_live_scores_bytes, _cached_live_scores_etag

    # Cold boot or empty cache: trigger background refresh without blocking calling thread
    with _live_scores_lock:
        is_empty = not _live_scores
    if is_empty:
        def _bg_refresh():
            try:
                refresh_live_scores_now()
            except Exception:
                pass
        threading.Thread(target=_bg_refresh, daemon=True, name="bg-live-scores-refresh").start()

    _update_cached_live_scores_payload()
    with _cached_live_scores_lock:
        if _cached_live_scores_bytes:
            return _cached_live_scores_bytes, _cached_live_scores_etag
        # Emergency fallback if serialization failed
        empty_payload = json.dumps({"ok": True, "competitions": {}, "poller": get_live_poller_status()}).encode("utf-8")
        return empty_payload, f'"{hashlib.md5(empty_payload).hexdigest()}"'


def get_cached_compact_live_scores_payload() -> tuple[bytes, str]:
    """Return pre-serialized compact (payload_bytes, etag) for ultra-fast, zero-re-serialization responses."""
    with _cached_live_scores_lock:
        if _cached_compact_live_scores_bytes:
            return _cached_compact_live_scores_bytes, _cached_compact_live_scores_etag

    # Cold boot or empty cache: trigger background refresh without blocking calling thread
    with _live_scores_lock:
        is_empty = not _live_scores
    if is_empty:
        def _bg_refresh():
            try:
                refresh_live_scores_now()
            except Exception:
                pass
        threading.Thread(target=_bg_refresh, daemon=True, name="bg-live-scores-refresh").start()

    _update_cached_live_scores_payload()
    with _cached_live_scores_lock:
        if _cached_compact_live_scores_bytes:
            return _cached_compact_live_scores_bytes, _cached_compact_live_scores_etag
        # Emergency fallback if serialization failed
        empty_compact = json.dumps({"ok": True, "competitions": {}, "poller": get_live_poller_status(), "compact": True}).encode("utf-8")
        return empty_compact, f'"{hashlib.md5(empty_compact).hexdigest()}"'


def get_live_scores_snapshot() -> dict:
    """Return a snapshot dict of all live scores."""
    with _live_scores_lock:
        return dict(_live_scores)


# ── Red-card dedupe ───────────────────────────────────────────────
# ESPN reports the same red card twice — in scoreboard ``details`` (parsed to
# ``red_cards``: full name, "67'") and in summary ``keyEvents`` (sometimes an
# abbreviated name like "B. Saka", sometimes no athlete at all, minute format
# can differ). Cards are fingerprinted as (player surname, side, minute) and a
# new card is a duplicate when the surname matches, or when the minute matches
# and the side matches/is unknown or either player name is unknown.

def _rc_minute_key(minute) -> str:
    """Normalize "67'", "67", "90'+2'" → "67" / "90+2"."""
    s = str(minute or "")
    nums = re.findall(r"\d+", s)
    if not nums:
        return ""
    return f"{nums[0]}+{nums[1]}" if "+" in s and len(nums) > 1 else nums[0]


def _rc_player_key(name) -> str:
    """Accent-folded lowercase surname ("B. Saka" / "Bukayo Saka" → "saka"); "" if unknown."""
    raw = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    tokens = [t for t in re.findall(r"[a-z]+", raw) if t not in {"jr", "junior", "sr", "ii", "iii"}]
    if not tokens or tokens == ["player"]:
        return ""
    return tokens[-1]


def _rc_fingerprints(notified: set, mid: str) -> list[tuple[str, str, str]]:
    prefix = f"rcseen|{mid}|"
    out = []
    for item in notified:
        if isinstance(item, str) and item.startswith(prefix):
            parts = item[len(prefix):].split("|")
            if len(parts) == 3:
                out.append((parts[0], parts[1], parts[2]))
    return out


def _rc_is_duplicate(notified: set, mid: str, pkey: str, side: str, mkey: str) -> bool:
    for seen_p, seen_side, seen_m in _rc_fingerprints(notified, mid):
        if pkey and seen_p and pkey == seen_p:
            return True
        if mkey and seen_m == mkey:
            sides_compatible = not side or not seen_side or side == seen_side
            names_compatible = not pkey or not seen_p
            if sides_compatible and (names_compatible or side == seen_side):
                return True
    return False


def _rc_mark(notified: set, mid: str, pkey: str, side: str, mkey: str) -> None:
    notified.add(f"rcseen|{mid}|{pkey}|{side}|{mkey}")


def _game_red_cards(game) -> list[dict]:
    """All red cards for a game from both ESPN sources, normalized.

    Returns dicts: player (display), pkey, side ("home"/"away"/""), minute (display), mkey.
    """
    cards = []
    home_id = str(game.get("home_team_id") or "")
    away_id = str(game.get("away_team_id") or "")
    for rc in (game.get("red_cards") or []):
        player = str(rc.get("player", "")).strip()
        minute = str(rc.get("minute", "")).strip()
        cards.append({
            "player": player or "Player",
            "pkey": _rc_player_key(player),
            "side": str(rc.get("team", "") or ""),
            "minute": minute,
            "mkey": _rc_minute_key(minute),
            "source": "scoreboard",
        })
    for ev in (game.get("key_events") or []):
        if "red card" not in str(ev.get("type", "")).lower():
            continue
        athlete = str(ev.get("athlete_name", "")).strip()
        minute = str(ev.get("clock", "")).strip()
        team_id = str(ev.get("team_id", "") or "")
        side = "home" if team_id and team_id == home_id else ("away" if team_id and team_id == away_id else "")
        cards.append({
            "player": athlete or str(ev.get("text", "")).strip() or "Player",
            "pkey": _rc_player_key(athlete),  # never fingerprint free text as a name
            "side": side,
            "minute": minute,
            "mkey": _rc_minute_key(minute),
            "source": "key_events",
        })
    return cards


def _dispatch_match_event_notifications() -> int:
    """Inspect current match states, detect kickoff, goals, halftime, fulltime, and red cards,
    and dispatch deduplicated push alerts and Live Activity updates.

    Guarantees:
    - Exactly ONE goal alert per (match_id, home_score, away_score) scoreline.
    - Scorer name and minute are resolved from scoreboard details or keyEvents.
    - Halftime detected reliably via _is_halftime_break / STATUS_HALFTIME / HT.
    - Full time detected reliably via status == "post" / STATUS_FULL_TIME.
    - Initial startup does not send backlogged notifications for finished/old games.
    """
    total_dispatched = 0
    try:
        from notifications import (
            clear_match_subscriptions,
            for_game_or_team_subscribers,
            record_notification_event,
            send_live_activity_end,
            send_live_activity_update,
        )

        _SCORING_EVENT_TYPES = frozenset({"goal", "own goal", "penalty"})
        _ALERT_EVENT_TYPES = _SCORING_EVENT_TYPES | frozenset({"red card"})

        with _live_scores_lock, _notified_events_lock, _match_tracker_lock:
            for comp_name, comp_data in _live_scores.items():
                polled_at = _polled_ts(comp_data)
                for g in comp_data.get("games", []):
                    mid = str(g.get("match_id", "")).strip()
                    if not mid:
                        continue
                    cur_status = str(g.get("status", "")).strip().lower()
                    if cur_status not in ("in", "post"):
                        if cur_status == "pre":
                            _match_tracker[mid] = {
                                "status": "pre",
                                "home_score": None,
                                "away_score": None,
                            }
                        continue

                    notified = _notified_events.setdefault(mid, set())
                    ht = g.get("home_team", "")
                    at = g.get("away_team", "")
                    hs = g.get("home_score")
                    as_ = g.get("away_score")
                    state_base = _la_content_state(g, comp_name, polled_at=polled_at)
                    cur_minute = state_base.get("match_minute") or 0

                    prev_meta = _match_tracker.get(mid)
                    is_first_observation = prev_meta is None

                    if is_first_observation:
                        # Pre-mark completed or mid-game milestones on initial observation
                        # to avoid spamming historical/past milestones.
                        for card in _game_red_cards(g):
                            if not _rc_is_duplicate(notified, mid, card["pkey"], card["side"], card["mkey"]):
                                _rc_mark(notified, mid, card["pkey"], card["side"], card["mkey"])
                        if cur_status == "post":
                            notified.add(f"start|{mid}")
                            notified.add(f"halftime|{mid}")
                            notified.add(f"ht|{mid}")
                            notified.add(f"fulltime|{mid}")
                            notified.add(f"ft|{mid}")
                            if hs is not None and as_ is not None and hs != "-" and as_ != "-":
                                notified.add(f"goal|{mid}|{hs}-{as_}")
                                notified.add(f"score_goal|{mid}|{hs}-{as_}")
                            # Match finished while we weren't watching (e.g. backend
                            # restart): silently end any Live Activities still
                            # registered so they don't linger, and drop match subs.
                            first_sync = str(g.get("sync_key", "")).strip()
                            if f"cleared|{mid}" not in notified:
                                notified.add(f"cleared|{mid}")
                                send_live_activity_end(
                                    mid, comp_name,
                                    {**state_base, "status": "FT", "event": "fulltime"},
                                    sync_key=first_sync,
                                )
                                clear_match_subscriptions(mid, comp_name, sync_key=first_sync)
                                _la_forget_timer(mid)
                        elif cur_status == "in":
                            if cur_minute > 5 or g.get("period") in ("2nd Half", "Halftime", "ET"):
                                notified.add(f"start|{mid}")
                            if g.get("period") in ("2nd Half", "ET") or _is_halftime_break(g.get("period"), g.get("status_type"), g.get("clock")):
                                notified.add(f"halftime|{mid}")
                                notified.add(f"ht|{mid}")
                            if hs is not None and as_ is not None and hs != "-" and as_ != "-" and (hs > 0 or as_ > 0):
                                notified.add(f"goal|{mid}|{hs}-{as_}")
                                notified.add(f"score_goal|{mid}|{hs}-{as_}")
                        _match_tracker[mid] = {
                            "status": cur_status,
                            "home_score": hs,
                            "away_score": as_,
                            "period": g.get("period", ""),
                            "status_type": g.get("status_type", ""),
                        }
                        continue

                    prev_status = prev_meta.get("status", "")
                    prev_h = prev_meta.get("home_score")
                    prev_a = prev_meta.get("away_score")
                    sync_k = str(g.get("sync_key", "")).strip()

                    la_stale = _la_stale_date(g)
                    la_pushed = False

                    def _queue_event_alert(event_type: str, title: str, body: str, collapse_id: str = ""):
                        """Record the feed event and queue regular pushes.

                        Devices that own an active Live Activity for this match are
                        skipped — the Live Activity alert already notifies them.
                        """
                        try:
                            record_notification_event(
                                title=title,
                                body=body,
                                match_id=mid,
                                competition=comp_name,
                                notif_type=event_type,
                                sync_key=sync_k,
                                home_team=ht,
                                away_team=at,
                                home_score=hs,
                                away_score=as_,
                            )
                        except Exception:
                            pass
                        tokens = for_game_or_team_subscribers(
                            mid, comp_name, home_team=ht, away_team=at, event=event_type, sync_key=sync_k,
                            exclude_live_activity_devices=True,
                        )
                        for token in tokens:
                            from notifications import _apns_notification_queue
                            _apns_notification_queue.append({
                                "token": token,
                                "title": title,
                                "body": body,
                                "badge": 1,
                                "match_id": mid,
                                "sync_key": sync_k,
                                "competition": comp_name,
                                "event": event_type,
                                "collapse_id": collapse_id,
                            })
                        from notifications import _notify_apns_queue
                        _notify_apns_queue()
                        return len(tokens)

                    # Handle VAR goal cancellation / reversal
                    if (
                        prev_h is not None
                        and prev_a is not None
                        and hs is not None
                        and as_ is not None
                        and hs != "-"
                        and as_ != "-"
                        and (hs < prev_h or as_ < prev_a)
                    ):
                        notified.discard(f"goal|{mid}|{prev_h}-{prev_a}")
                        notified.discard(f"score_goal|{mid}|{prev_h}-{prev_a}")

                    # 1. Match Kickoff (pre -> in)
                    start_id = f"start|{mid}"
                    if (prev_status == "pre" and cur_status == "in") or (
                        cur_status == "in" and start_id not in notified and cur_minute <= 5
                    ):
                        if start_id not in notified:
                            notified.add(start_id)
                            start_title = "Match Started"
                            start_body = f"{ht} vs {at} - Kickoff!"
                            _queue_event_alert("start", start_title, start_body, collapse_id=start_id)
                            la_state = {**state_base, "event": "kickoff"}
                            send_live_activity_update(
                                mid, comp_name, la_state,
                                event="kickoff",
                                alert={"title": start_title, "body": start_body},
                                sync_key=sync_k,
                                stale_date=la_stale,
                            )
                            la_pushed = True
                            total_dispatched += 1

                    # 2. Halftime detection (robust check across period, status_type, and clock)
                    is_ht = _is_halftime_break(g.get("period"), g.get("status_type"), g.get("clock"))
                    if is_ht and cur_status != "post":
                        ht_id = f"halftime|{mid}"
                        if ht_id not in notified:
                            notified.add(ht_id)
                            notified.add(f"ht|{mid}")
                            hs_disp = hs if hs is not None and hs != "-" else 0
                            as_disp = as_ if as_ is not None and as_ != "-" else 0
                            ht_title = "Halftime"
                            ht_body = f"{ht} {hs_disp}-{as_disp} {at} - Halftime"
                            _queue_event_alert("ht", ht_title, ht_body, collapse_id=ht_id)
                            la_state = {**state_base, "event": "halftime"}
                            send_live_activity_update(
                                mid, comp_name, la_state,
                                event="halftime",
                                alert={"title": ht_title, "body": ht_body},
                                sync_key=sync_k,
                                stale_date=la_stale,
                            )
                            la_pushed = True
                            total_dispatched += 1

                    # 3. Goal detection (strict deduplication by match ID and scoreline)
                    if (
                        hs is not None
                        and as_ is not None
                        and hs != "-"
                        and as_ != "-"
                        and (hs > 0 or as_ > 0)
                    ):
                        goal_id = f"goal|{mid}|{hs}-{as_}"
                        score_increased = (
                            prev_h is not None
                            and prev_a is not None
                            and (hs > prev_h or as_ > prev_a)
                        )
                        if goal_id not in notified and score_increased:
                            notified.add(goal_id)
                            notified.add(f"score_goal|{mid}|{hs}-{as_}")

                            scoring_side = "home" if (hs > (prev_h or 0)) else "away"
                            scorer_name = ""
                            scorer_min = ""
                            scorer_type = "goal"
                            goalscorers = g.get("goalscorers") or []
                            matching_goals = [goal for goal in goalscorers if goal.get("team") == scoring_side]
                            target_goal = matching_goals[-1] if matching_goals else (goalscorers[-1] if goalscorers else None)
                            if target_goal:
                                scorer_name = target_goal.get("scorer", "").strip()
                                scorer_min = target_goal.get("minute", "").strip()
                                scorer_type = target_goal.get("type", "goal").strip().lower()

                            # If scorer not in goalscorers, search key_events
                            if not scorer_name:
                                for ev in reversed(g.get("key_events") or []):
                                    ev_type = str(ev.get("type", "")).lower().strip()
                                    if ev_type in _SCORING_EVENT_TYPES or ev.get("scoring_play"):
                                        ath = ev.get("athlete_name", "").strip()
                                        clk = ev.get("clock", "").strip()
                                        txt = ev.get("text", "").strip() or ev.get("short_text", "").strip()
                                        if ath:
                                            scorer_name = ath
                                            scorer_min = clk
                                            scorer_type = ev_type
                                            break
                                        elif txt:
                                            scorer_name = txt
                                            scorer_min = clk
                                            scorer_type = ev_type
                                            break

                            goal_title = "Goal"
                            if scorer_name:
                                if "own" in scorer_type:
                                    tag = f"{scorer_name} ({scorer_min} OG)" if scorer_min else f"{scorer_name} (OG)"
                                elif "pen" in scorer_type:
                                    tag = f"{scorer_name} ({scorer_min} pen)" if scorer_min else f"{scorer_name} (pen)"
                                elif scorer_min:
                                    tag = f"{scorer_name} ({scorer_min})"
                                else:
                                    tag = scorer_name
                                goal_body = f"{ht} {hs}-{as_} {at} - {tag}"
                            else:
                                clk = g.get("clock", "")
                                goal_body = f"{ht} {hs}-{as_} {at} ({clk})" if clk else f"{ht} {hs}-{as_} {at}"

                            _queue_event_alert("goal", goal_title, goal_body, collapse_id=goal_id)

                            la_event_label = (
                                "own goal" if "own" in scorer_type
                                else "penalty" if "penalty" in scorer_type
                                else "goal"
                            )
                            la_state = {
                                **state_base,
                                "event": la_event_label,
                                "clock": scorer_min or g.get("clock", ""),
                            }
                            if scorer_name:
                                la_state["event_text"] = scorer_name
                            la_alert = {"title": goal_title, "body": goal_body}
                            send_live_activity_update(
                                mid, comp_name, la_state, event=la_event_label, alert=la_alert,
                                sync_key=sync_k, stale_date=la_stale,
                            )
                            la_pushed = True
                            total_dispatched += 1

                    # 4. Red Cards — merged from scoreboard details (red_cards) and
                    # summary key_events, deduplicated by (surname, side, minute) so
                    # the same card reported by both feeds alerts exactly once.
                    for card in _game_red_cards(g):
                        if _rc_is_duplicate(notified, mid, card["pkey"], card["side"], card["mkey"]):
                            continue
                        _rc_mark(notified, mid, card["pkey"], card["side"], card["mkey"])
                        rc_player = card["player"]
                        rc_min = card["minute"]
                        carded_team = ht if card["side"] == "home" else (at if card["side"] == "away" else "")
                        team_info = f" ({carded_team})" if carded_team else ""
                        min_info = f" ({rc_min})" if rc_min else ""
                        rc_body = f"{rc_player}{team_info} Red Card{min_info}"
                        rc_id = f"red_card|{mid}|{card['pkey'] or card['side']}|{card['mkey']}"
                        _queue_event_alert("red_card", "Red Card", rc_body, collapse_id=rc_id)
                        la_state = {**state_base, "event": "red card", "clock": rc_min, "event_text": rc_body}
                        send_live_activity_update(
                            mid, comp_name, la_state, event="red card",
                            alert={"title": "Red Card", "body": rc_body},
                            sync_key=sync_k, stale_date=la_stale,
                        )
                        la_pushed = True
                        total_dispatched += 1

                    # 5. Full time detection.
                    # STATUS_END_REGULAR_TIME is deliberately NOT full time: in cup
                    # ties it precedes extra time / penalties, and ending the Live
                    # Activity there would drop the rest of the match.
                    is_ft = (
                        cur_status == "post"
                        or str(g.get("status_type", "")).upper() in {
                            "STATUS_FULL_TIME", "STATUS_FINAL", "STATUS_FINAL_AET", "STATUS_FINAL_PEN",
                        }
                    )

                    # 4b. Period transitions. Halftime ending (2nd-half kickoff), extra
                    # time and a penalty shootout are first-class events: alert + regular
                    # push + Live Activity update carrying the re-anchored timer, so the
                    # on-device clock resumes from 45:00 / 90:00 / 105:00. Quieter
                    # transitions (ET 2nd half, end of regular time) update silently.
                    # Priority 10 (timer correctness); devices with frequent updates
                    # disabled are downgraded to 5 by the notifications priority policy.
                    prev_period = str(prev_meta.get("period") or "")
                    cur_period = str(g.get("period") or "")
                    if (
                        not is_ft
                        and cur_status == "in"
                        and cur_period
                        and prev_period
                        and cur_period != prev_period
                        and not _is_halftime_break(cur_period, g.get("status_type"), g.get("clock"))
                    ):
                        period_id = f"period|{mid}|{cur_period}"
                        if period_id not in notified:
                            notified.add(period_id)
                            low = cur_period.lower()
                            is_et = low.startswith("et") or "extra" in low or "et" in low.split()
                            score_txt = f"{_la_score(hs) or 0}-{_la_score(as_) or 0}"
                            alert_event = None  # regular-push event type
                            if "pen" in low or "shootout" in low:
                                period_label = "penalties"
                                alert_event = "penalties"
                                p_title, p_body = "Penalties", f"{ht} {score_txt} {at} - Penalty shootout"
                            elif low in ("ft", "full time") or "regular" in low:
                                # ESPN labels STATUS_END_REGULAR_TIME as "FT" while the
                                # tie is still live (extra time / penalties follow).
                                period_label = "end of regular time"
                            elif is_et and ("2nd" in low or "second" in low):
                                period_label = "extra time second half"
                            elif is_et:
                                period_label = "extra time"
                                alert_event = "extra_time"
                                p_title, p_body = "Extra Time", f"{ht} {score_txt} {at} - Extra time underway"
                            elif "2nd" in low or "second" in low:
                                period_label = "second half"
                                alert_event = "second_half"
                                p_title, p_body = "Second Half", f"{ht} {score_txt} {at} - Second half underway"
                            else:
                                period_label = "period"
                            if alert_event:
                                _queue_event_alert(alert_event, p_title, p_body, collapse_id=period_id)
                                send_live_activity_update(
                                    mid, comp_name, {**state_base, "event": period_label},
                                    event=period_label, alert={"title": p_title, "body": p_body},
                                    sync_key=sync_k, stale_date=la_stale,
                                )
                                total_dispatched += 1
                            else:
                                send_live_activity_update(
                                    mid, comp_name, {**state_base, "event": period_label},
                                    event=period_label, sync_key=sync_k, priority=10, stale_date=la_stale,
                                )
                            la_pushed = True

                    # 4c. Score changed without a goal alert (VAR reversal, missed
                    # cycle, corrected feed): keep the activity's scoreboard in sync.
                    if (
                        not is_ft
                        and not la_pushed
                        and cur_status == "in"
                        and prev_h is not None
                        and prev_a is not None
                        and (_la_score(hs), _la_score(as_)) != (_la_score(prev_h), _la_score(prev_a))
                    ):
                        send_live_activity_update(
                            mid, comp_name, {**state_base, "event": "score"},
                            event="score", sync_key=sync_k, priority=10, stale_date=la_stale,
                        )
                        la_pushed = True

                    if is_ft:
                        ft_id = f"fulltime|{mid}"
                        if ft_id not in notified:
                            notified.add(ft_id)
                            notified.add(f"ft|{mid}")
                            hs_disp = _la_score(hs)
                            as_disp = _la_score(as_)
                            hs_txt = hs_disp if hs_disp is not None else 0
                            as_txt = as_disp if as_disp is not None else 0
                            ft_title = "Full Time"
                            ft_body = f"{ht} {hs_txt}-{as_txt} {at} - Full Time"
                            _queue_event_alert("end", ft_title, ft_body, collapse_id=ft_id)
                            # Full final content-state (same schema as updates) so
                            # ActivityKit can decode the "end" push.
                            state = {
                                **state_base,
                                "home_score": hs_txt,
                                "away_score": as_txt,
                                "status": "FT",
                                "event": "fulltime",
                                "match_minute": state_base.get("match_minute") or 90,
                            }
                            send_live_activity_end(
                                mid, comp_name, state,
                                alert={"title": ft_title, "body": ft_body}, sync_key=sync_k,
                            )
                            total_dispatched += 1
                            _la_forget_timer(mid)
                        if f"cleared|{mid}" not in notified:
                            notified.add(f"cleared|{mid}")
                            clear_match_subscriptions(mid, comp_name, sync_key=sync_k)

                    # Update tracker for next cycle
                    _match_tracker[mid] = {
                        "status": cur_status,
                        "home_score": hs,
                        "away_score": as_,
                        "period": g.get("period", ""),
                        "status_type": g.get("status_type", ""),
                    }
    except Exception:
        import traceback
        traceback.print_exc()

    return total_dispatched


def _discover_competitions_for_poll(poll_date, today_str):
    """Return competitions expected to have games today.

    Prefer pipeline upcoming CSVs (written by Predict_Upcoming_*), and only
    fall back to an ESPN discovery sweep when those files have nothing for the
    date. Callers that collect live scores should invoke this **after** the
    primary ESPN fetch so an empty/stale CSV cannot gate core collection.
    """
    todays_comps = _get_todays_competitions(today_date=poll_date)
    if not todays_comps:
        todays_comps = _discover_todays_competitions_from_espn(poll_date, today_str)
    return todays_comps


def _todays_comps_from_results(results, poll_date):
    """Build a todays_comps map from games already collected this cycle."""
    now_et = datetime.now(ZoneInfo("America/New_York"))
    todays = defaultdict(list)
    for comp_name, comp_data in (results or {}).items():
        games = comp_data.get("games") or []
        if not games:
            continue
        for game in games:
            kickoff_et = None
            kickoff_raw = str(game.get("kickoff_utc") or game.get("match_datetime_utc") or "").strip()
            if kickoff_raw:
                try:
                    dt_utc = pd.to_datetime(kickoff_raw, errors="coerce")
                    if not pd.isna(dt_utc):
                        if getattr(dt_utc, "tz", None) is None:
                            dt_utc = dt_utc.tz_localize("UTC")
                        kickoff_et = dt_utc.tz_convert(ZoneInfo("America/New_York"))
                except Exception:
                    kickoff_et = None
            todays[comp_name].append(kickoff_et or now_et)
    return {k: sorted(v) for k, v in todays.items()}


def _merge_competition_result_dicts(*result_dicts):
    """Merge per-competition poll results; concatenate unique match_ids."""
    merged = {}
    for results in result_dicts:
        for comp_name, comp_data in (results or {}).items():
            existing = merged.get(comp_name)
            if existing is None:
                merged[comp_name] = {
                    "competition": comp_name,
                    "games": list(comp_data.get("games") or []),
                    "last_polled_utc": comp_data.get("last_polled_utc"),
                }
                continue
            by_id = {
                g.get("match_id"): g
                for g in existing.get("games") or []
                if g.get("match_id")
            }
            for g in comp_data.get("games") or []:
                mid = g.get("match_id")
                if not mid:
                    continue
                if mid in by_id:
                    by_id[mid].update(g)
                else:
                    by_id[mid] = g
            existing["games"] = list(by_id.values())
            existing["last_polled_utc"] = comp_data.get("last_polled_utc") or existing.get(
                "last_polled_utc"
            )
    return merged


def _poll_espn_for_dates(poll_dates, *, include_deferred: bool):
    """Fetch live (+ optional deferred) ESPN results across one or more dates.

    Order matters:
      1. Collect from the always-on CORE set first (never gated on CSV discovery).
      2. Only after that collection, check pipeline CSVs / ESPN discovery for
         additional competitions that have games today, then fetch those extras.
    That way an unfinished upcoming-prediction pipeline cannot block live
    score collection, and the "games for today?" check runs on top of
    already-collected data.
    """
    all_live = {}
    all_deferred = {}
    todays_comps_merged = {}
    for poll_date in poll_dates:
        date_str = poll_date.strftime("%Y%m%d")

        # ── 1. Collect CORE competitions first ─────────────────────
        core_available = _build_available_competitions({})
        live_comps, deferred_comps = _split_live_and_deferred(core_available)
        all_live = _merge_competition_result_dicts(
            all_live, _fetch_live_poll_results(live_comps, date_str)
        )
        if include_deferred and deferred_comps:
            all_deferred = _merge_competition_result_dicts(
                all_deferred, _fetch_deferred_poll_results(deferred_comps, date_str)
            )

        already = set(live_comps) | set(deferred_comps)

        # ── 2. AFTER collection: check which other comps have games today ──
        discovered = _discover_competitions_for_poll(poll_date, date_str)
        todays_comps_merged.update(discovered or {})
        # Also count competitions that already returned games this cycle.
        for comp_name, comps_kickoffs in _todays_comps_from_results(
            _merge_competition_result_dicts(all_live, all_deferred), poll_date
        ).items():
            todays_comps_merged.setdefault(comp_name, comps_kickoffs)

        extra_available = {}
        for comp in discovered or {}:
            if comp in already:
                continue
            eid = config.LIVE_SCORE_COMPETITIONS.get(comp)
            if eid:
                extra_available[comp] = eid
        if extra_available:
            extra_live, extra_deferred = _split_live_and_deferred(extra_available)
            all_live = _merge_competition_result_dicts(
                all_live, _fetch_live_poll_results(extra_live, date_str)
            )
            if include_deferred and extra_deferred:
                all_deferred = _merge_competition_result_dicts(
                    all_deferred, _fetch_deferred_poll_results(extra_deferred, date_str)
                )

    return (
        _merge_competition_result_dicts(all_live, all_deferred),
        todays_comps_merged,
    )


def _build_available_competitions(todays_comps):
    """Merge today's discovered competitions with the always-on core set."""
    available = {}
    for comp in config.CORE_LIVE_POLL_COMPETITIONS:
        eid = config.LIVE_SCORE_COMPETITIONS.get(comp)
        if eid:
            available[comp] = eid
    for comp in todays_comps or {}:
        eid = config.LIVE_SCORE_COMPETITIONS.get(comp)
        if eid:
            available[comp] = eid
    return available


def _split_live_and_deferred(available):
    live_comps = {}
    deferred_comps = {}
    for comp, eid in available.items():
        if comp in config.RESULT_ONLY_COMPETITIONS or comp in config.REDUCED_POLLING_COMPETITIONS:
            deferred_comps[comp] = eid
        else:
            live_comps[comp] = eid
    return live_comps, deferred_comps


def _fetch_live_poll_results(live_comps, today_str):
    results = {}
    if not live_comps:
        return results
    with ThreadPoolExecutor(max_workers=min(8, len(live_comps))) as pool:
        ft_to_name = {
            pool.submit(
                _fetch_competition_scores, name, eid, today_str, log_failures=True
            ): name
            for name, eid in live_comps.items()
        }
        for ft in as_completed(ft_to_name):
            name = ft_to_name[ft]
            try:
                games = ft.result()
                if name == config.CLUB_FRIENDLIES_COMPETITION:
                    chelsea = [g for g in games if _is_chelsea_live_game(g)]
                    non_chelsea = [g for g in games if not _is_chelsea_live_game(g) and g.get("status") == "post"]
                    games = chelsea + non_chelsea
                games = _filter_live_games_for_competition(name, games)
                games = _stamp_scoreboard_date(games, today_str)
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
    return results


def _fetch_deferred_poll_results(deferred_comps, today_str):
    results = {}
    for comp_name, espn_id in deferred_comps.items():
        try:
            games = _fetch_competition_scores(comp_name, espn_id, today_str, log_failures=True)
            if comp_name in config.REDUCED_POLLING_COMPETITIONS:
                filtered = []
                for g in games:
                    status = g.get("status", "")
                    if status == "post":
                        filtered.append(g)
                    elif status == "in":
                        minute = g.get("clock") or g.get("match_minute") or 0
                        try:
                            minute = int(minute)
                        except (ValueError, TypeError):
                            minute = 0
                        if 40 <= minute <= 50 or minute >= 85:
                            filtered.append(g)
                games = filtered
            else:
                games = [g for g in games if g.get("status") == "post"]
            games = _stamp_scoreboard_date(games, today_str)
            if games:
                results[comp_name] = {
                    "competition": comp_name,
                    "games": games,
                    "last_polled_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
                }
        except Exception:
            import traceback
            traceback.print_exc()
    return results


def _merge_poll_results_into_live_scores(results, poll_date, allowed_dates=None):
    global _poller_last_cycle_utc, _poller_last_cycle_games
    allowed = list(allowed_dates) if allowed_dates else [poll_date]
    with _live_scores_lock:
        _poller_day_str = poll_date.isoformat()
        if getattr(_live_score_poller_loop, "_poller_date", None) != _poller_day_str:
            _merge_completed_to_history()
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
                _live_scores.clear()
                with _live_summary_cache_lock:
                    _live_summary_cache.clear()
                    _summary_fetch_meta.clear()
                _live_score_poller_loop._poller_date = _poller_day_str
            except Exception:
                import traceback
                traceback.print_exc()

        for comp_name, comp_data in results.items():
            new_games = comp_data.get("games", [])
            for g in new_games:
                _stamp_game_sync_fields(g, comp_name)
            existing = _live_scores.get(comp_name, {"games": []})
            if not new_games and existing.get("games"):
                continue
            # Start from this cycle's games, then keep only in-window leftovers
            # from memory (e.g. still-live spillover). Never retain yesterday's
            # completed slate after the overnight window closes.
            games_by_id = {
                g["match_id"]: g
                for g in new_games
                if g.get("match_id")
            }
            for g in existing.get("games") or []:
                mid = g.get("match_id")
                if not mid or mid in games_by_id:
                    continue
                if _keep_game_for_poll_window(g, set(allowed)):
                    games_by_id[mid] = g
            _live_scores[comp_name] = {
                "competition": comp_name,
                "games": list(games_by_id.values()),
                "last_polled_utc": comp_data["last_polled_utc"],
                "cup_format": config._CUP_FORMATS.get(comp_name),
            }
        # Flush completed games to history BEFORE pruning the overnight
        # slate. Otherwise yesterday's finals leave memory and never reach
        # live_score_history.json /api/past-games.
        _flush_completed_live_scores_to_history()
        _prune_live_scores_to_dates(allowed)
        with _live_summary_cache_lock:
            for comp_name, comp_data in _live_scores.items():
                for g in comp_data.get("games", []):
                    mid = g.get("match_id")
                    if mid and mid in _live_summary_cache:
                        g.update(_live_summary_cache[mid])
                    _stamp_game_sync_fields(g, comp_name)
        _poller_last_cycle_utc = datetime.now(ZoneInfo("UTC")).isoformat()
        _poller_last_cycle_games = sum(len(v.get("games", [])) for v in _live_scores.values())


def refresh_live_scores_now(*, force: bool = False) -> dict:
    """On-demand ESPN poll used when the in-memory store is empty."""
    global _last_on_demand_refresh_ts
    now_ts = time.time()
    if not force and (now_ts - _last_on_demand_refresh_ts) < _ON_DEMAND_REFRESH_MIN_INTERVAL_S:
        with _live_scores_lock:
            return dict(_live_scores)

    with _on_demand_refresh_lock:
        if not force and (time.time() - _last_on_demand_refresh_ts) < _ON_DEMAND_REFRESH_MIN_INTERVAL_S:
            with _live_scores_lock:
                return dict(_live_scores)

        poll_dates = _poll_dates_for_cycle()
        primary_date = poll_dates[0]
        results, _todays = _poll_espn_for_dates(poll_dates, include_deferred=True)
        _merge_poll_results_into_live_scores(results, primary_date, allowed_dates=poll_dates)
        try:
            _dispatch_match_event_notifications()
        except Exception:
            pass
        # Finals observed here would otherwise only refresh tables/form on the
        # poller's next cycle (up to 30 min when nothing else is live).
        threading.Thread(
            target=_drain_finalized_results, name="results-drain", daemon=True
        ).start()
        _last_on_demand_refresh_ts = time.time()
        try:
            _update_cached_live_scores_payload()
        except Exception:
            pass

    with _live_scores_lock:
        return dict(_live_scores)


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
    try:
        _backfill_recent_live_score_history()
    except Exception:
        import traceback
        traceback.print_exc()
    while True:
        try:
            poll_dates = _poll_dates_for_cycle()
            poll_date = poll_dates[0]

            # Snapshot previous game statuses before merging (for detecting new completions).
            prev_statuses = {}
            prev_scores: dict[str, tuple[int | None, int | None]] = {}
            with _live_scores_lock:
                for comp_name, comp_data in _live_scores.items():
                    for g in comp_data.get("games", []):
                        mid = g.get("match_id")
                        if mid:
                            prev_statuses[mid] = (comp_name, g.get("status", ""))
                            prev_scores[mid] = (
                                g.get("home_score"),
                                g.get("away_score"),
                            )

            # Deferred comps are expensive; only include on the deferred cadence.
            global _last_deferred_poll_ts
            now_ts = time.time()
            include_deferred = (now_ts - _last_deferred_poll_ts) >= _DEFERRED_POLL_INTERVAL_S
            if include_deferred:
                _last_deferred_poll_ts = now_ts

            # Collect ESPN games first; CSV/discovery "games today?" runs inside
            # _poll_espn_for_dates only after the CORE fetch completes.
            results, todays_comps = _poll_espn_for_dates(
                poll_dates, include_deferred=include_deferred
            )

            _merge_poll_results_into_live_scores(
                results, poll_date, allowed_dates=poll_dates
            )
            # Persist any newly completed games to history file.
            merged_comps = _merge_completed_to_history()

            # ── Standings / form refresh on game completion ─────────
            # Recompute real tables as soon as a match finishes so standings
            # update before the nightly pipeline refresh. Finals seen by any
            # path (this cycle, on-demand refresh, overnight rollover) are
            # queued by _merge_completed_to_history; status transitions seen
            # here are added as a safety net.
            try:
                comps_to_refresh = set(merged_comps)
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
                _drain_finalized_results(comps_to_refresh)
            except Exception:
                import traceback
                traceback.print_exc()

            # ── Live Activity updates (iOS 16.1+) ─────────────────────
            # All Live Activity pushes (kickoff, goals, halftime, period changes,
            # red cards, score corrections, full time) are sent from
            # _dispatch_match_event_notifications() so each scoreboard change
            # produces exactly one ActivityKit push. Push-to-start ("start") is
            # sent by the separate _live_activity_start_loop() thread.

            # ── Fetch summary data for active games ──────────────────
            # - pre:   fetch once, then re-attempt at most every
            #          _PRE_SUMMARY_RETRY_S (lineups are the only thing that
            #          changes pre-kickoff; h2h/last-five/injuries are static).
            # - in:    fetch every cycle (key_events & boxscore change in real time)
            # - post:  fetch once if cache empty (final data capture)
            try:
                games_needing_summary = []
                now_s = time.time()
                for comp_name, comp_data in list(_live_scores.items()):
                    espn_id = config.LIVE_SCORE_COMPETITIONS.get(comp_name)
                    if not espn_id:
                        continue
                    for g in comp_data.get("games", []):
                        mid = g.get("match_id")
                        if not mid:
                            continue
                        status = g.get("status", "")
                        if status == "in":
                            games_needing_summary.append((comp_name, espn_id, mid))
                        elif status == "post" and mid not in _live_summary_cache:
                            games_needing_summary.append((comp_name, espn_id, mid))
                        elif status == "pre":
                            meta = _summary_fetch_meta.get(mid)
                            if meta is None or (now_s - float(meta.get("ts", 0))) >= _PRE_SUMMARY_RETRY_S:
                                games_needing_summary.append((comp_name, espn_id, mid))
                if games_needing_summary:
                    with _live_summary_cache_lock:
                        for _c, _e, mid in games_needing_summary:
                            _summary_fetch_meta[mid] = {"ts": now_s}
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

            # Persist finished games again once full summary stats are attached
            # so SQLite/JSON history keeps the same fields /api/live-scores exposes
            # for completed games (lineups, boxscore, key events, game_info, …).
            try:
                _persist_post_games_full_stats()
            except Exception:
                import traceback
                traceback.print_exc()

            # ── Event push notifications (goals, red cards, halftime, full time) ──
            try:
                _dispatch_match_event_notifications()
            except Exception:
                import traceback
                traceback.print_exc()

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
        try:
            _update_cached_live_scores_payload()
        except Exception:
            import traceback
            traceback.print_exc()

        # Exclude deferred comps from interval calc so they don't force 60s polling.
        # NOTE: do not reference undefined cycle locals here — a prior NameError on
        # ``live_comps`` killed the daemon after the first successful collect, which
        # froze scores at status=pre while poller.started stayed true.
        try:
            live_results = {
                k: v
                for k, v in (results or {}).items()
                if k not in config.RESULT_ONLY_COMPETITIONS
                and k not in config.REDUCED_POLLING_COMPETITIONS
            }
            live_todays = {
                k: v
                for k, v in (todays_comps or {}).items()
                if k not in config.RESULT_ONLY_COMPETITIONS
                and k not in config.REDUCED_POLLING_COMPETITIONS
            }
            interval = _compute_poll_interval(live_results, live_todays)
        except Exception:
            import traceback
            traceback.print_exc()
            interval = 60
        time.sleep(max(10, int(interval or 60)))


# ── Live Activity push-to-start scheduler ─────────────────────────
# ActivityKit "start" pushes are sent APNS_LA_START_LEAD_SECONDS (default 5 min)
# before scheduled kickoff to every device that (a) registered a push-to-start
# token and (b) follows the match or either team. A separate fast thread is used
# because the main poller sleeps up to 15 min during the pre-match window.

_LA_START_LOOP_INTERVAL_S = 20
_la_start_thread: threading.Thread | None = None


def _dispatch_live_activity_starts(now_ts: float | None = None) -> int:
    """Send push-to-start payloads for games entering the pre-kickoff window. Returns starts queued."""
    from notifications import (
        broadcast_enabled,
        for_game_or_team_subscribers,
        get_device_la_settings,
        get_or_create_match_channel,
        get_token_environment,
        live_activity_device_tokens,
        mark_push_started,
        push_to_start_devices,
        push_to_start_token_for_device,
        register_channel_activity,
        send_live_activity_start,
        was_push_started,
    )

    pts_devices = set(push_to_start_devices())
    if not pts_devices:
        return 0
    lead = int(getattr(config, "APNS_LA_START_LEAD_SECONDS", 300))
    grace = int(getattr(config, "APNS_LA_START_GRACE_SECONDS", 900))
    now = float(now_ts if now_ts is not None else time.time())

    candidates: list[tuple[str, dict, float]] = []
    with _live_scores_lock:
        for comp_name, comp_data in _live_scores.items():
            for g in comp_data.get("games", []):
                mid = str(g.get("match_id") or "").strip()
                status = str(g.get("status", "")).strip().lower()
                if not mid or status not in ("pre", "in"):
                    continue
                ko = _la_kickoff_ts(g)
                if ko is None or not (ko - lead <= now <= ko + grace):
                    continue
                candidates.append((comp_name, dict(g), ko))

    queued = 0
    for comp_name, g, ko in candidates:
        mid = str(g.get("match_id") or "").strip()
        sync_k = str(g.get("sync_key", "")).strip()
        home, away = g.get("home_team", ""), g.get("away_team", "")
        subscribers = for_game_or_team_subscribers(
            mid, comp_name, home_team=home, away_team=away, event=None, sync_key=sync_k,
        )
        targets = [d for d in subscribers if d in pts_devices and not was_push_started(d, mid)]
        if not targets:
            continue
        # Devices that already started an activity in-app for this match.
        already_active = live_activity_device_tokens(mid, comp_name, sync_key=sync_k)
        is_live = str(g.get("status", "")).strip().lower() == "in"
        state = {**_la_content_state(g, comp_name), "event": "live" if is_live else "prematch"}
        attributes = _la_attributes(g, comp_name)
        mins = int(round((ko - now) / 60.0))
        if is_live or mins <= 0:
            title = "Live now" if is_live else "Kicking off"
        else:
            title = f"Kickoff in {mins} min"
        alert = {"title": title, "body": f"{home} vs {away}"}
        stale = _la_stale_date(g)
        for device in targets:
            if device in already_active:
                mark_push_started(device, mid, competition=comp_name, sync_key=sync_k)
                continue
            settings = get_device_la_settings(device)
            if settings.get("activities_enabled") is False:
                continue  # user turned Live Activities off in iOS Settings
            # iOS 18+: subscribe the new activity to the match's broadcast channel.
            channel_id = ""
            if broadcast_enabled() and settings.get("supports_broadcast"):
                env = get_token_environment(push_to_start_token_for_device(device))
                if env is None:
                    env = get_token_environment(device)
                ch = get_or_create_match_channel(mid, sync_k, comp_name, environment=env)
                if ch.get("ok"):
                    channel_id = ch["channel_id"]
            if send_live_activity_start(
                device, mid, comp_name, state, attributes, alert,
                sync_key=sync_k, stale_date=stale, channel_id=channel_id,
            ):
                if channel_id:
                    # The device won't register a per-activity token, so record
                    # its activity here (duplicate-banner suppression).
                    register_channel_activity(device, mid, sync_key=sync_k, competition=comp_name)
                queued += 1
    return queued


_LA_CHANNEL_CLEANUP_INTERVAL_S = 600


def _live_activity_start_loop() -> None:
    last_cleanup = 0.0
    while True:
        try:
            _dispatch_live_activity_starts()
        except Exception:
            import traceback
            traceback.print_exc()
        # Delete broadcast channels of finished matches (10k-per-env cap).
        if time.time() - last_cleanup >= _LA_CHANNEL_CLEANUP_INTERVAL_S:
            last_cleanup = time.time()
            try:
                import notifications as _n
                if _n.broadcast_enabled():
                    _n.cleanup_expired_channels()
            except Exception:
                import traceback
                traceback.print_exc()
        time.sleep(_LA_START_LOOP_INTERVAL_S)


def start_live_activity_start_scheduler() -> None:
    """Start the push-to-start scheduler thread once per process."""
    global _la_start_thread
    if _la_start_thread is not None and _la_start_thread.is_alive():
        return
    _la_start_thread = threading.Thread(
        target=_live_activity_start_loop, daemon=True, name="live-activity-start",
    )
    _la_start_thread.start()


def start_live_score_poller(*, force: bool = False) -> None:
    """Start the live score poller thread if not already running.

    Called by BackendServer on Steam Deck (gunicorn) and by ``__main__``
    in dev mode.  Safe to call multiple times — only one live thread is kept.
    Pass ``force=True`` (or call via status) to revive a dead thread.
    """
    global _poller_started, _poller_thread
    start_live_activity_start_scheduler()
    if _poller_thread_is_alive() and not force:
        return
    if _poller_thread_is_alive() and force:
        return
    _poller_started = True
    start_live_score_poller._started = True
    thread = threading.Thread(
        target=_live_score_poller_loop,
        daemon=True,
        name="live-score-poller",
    )
    _poller_thread = thread
    thread.start()
    print(
        "[startup] Live score poller started "
        "(60s live / 60s 3min pre-kickoff / 15min pre-match / 30min idle)."
    )


# ── Testing & Simulation Helpers ─────────────────────────────────

def inject_simulated_live_game(game: dict, comp_name: str = "Test/Simulation") -> dict:
    """Inject a test/simulated match into _live_scores for end-to-end testing."""
    mid = str(game.get("match_id", "sim-999")).strip()
    with _live_scores_lock:
        comp_entry = _live_scores.setdefault(comp_name, {
            "competition": comp_name,
            "last_polled_utc": datetime.now(timezone.utc).isoformat(),
            "games": [],
        })
        games = comp_entry.setdefault("games", [])
        idx = next((i for i, g in enumerate(games) if g.get("match_id") == mid), None)
        entry = dict(game)
        entry["match_id"] = mid
        entry.setdefault("competition", comp_name)
        entry.setdefault("home_team", "Arsenal")
        entry.setdefault("away_team", "Chelsea")
        entry.setdefault("home_score", 0)
        entry.setdefault("away_score", 0)
        entry.setdefault("status", "in")
        entry.setdefault("clock", "0'")
        entry.setdefault("period", 1)
        entry.setdefault("kickoff_utc", datetime.now(timezone.utc).isoformat())
        entry.setdefault("goalscorers", [])
        entry.setdefault("red_cards", [])
        entry.setdefault("yellow_cards", [])
        entry.setdefault("live_prediction", {"prob_home": 52.0, "prob_draw": 26.0, "prob_away": 22.0})
        entry["simulated"] = True
        _stamp_game_sync_fields(entry, comp_name)

        if idx is not None:
            games[idx] = entry
        else:
            games.append(entry)
        comp_entry["last_polled_utc"] = datetime.now(timezone.utc).isoformat()

    try:
        _update_cached_live_scores_payload()
    except Exception:
        pass
    return entry


def get_simulated_live_game(match_id: str = "sim-999", comp_name: str = "Test/Simulation") -> dict | None:
    with _live_scores_lock:
        comp = _live_scores.get(comp_name)
        if not comp:
            return None
        for g in comp.get("games", []):
            if g.get("match_id") == match_id:
                return dict(g)
    return None


def update_simulated_live_game(match_id: str = "sim-999", updates: dict = None, comp_name: str = "Test/Simulation") -> dict | None:
    if updates is None:
        updates = {}
    with _live_scores_lock:
        comp = _live_scores.get(comp_name)
        if not comp:
            return None
        target = None
        for g in comp.get("games", []):
            if g.get("match_id") == match_id:
                target = g
                break
        if target is None:
            return None
        target.update(updates)
        comp["last_polled_utc"] = datetime.now(timezone.utc).isoformat()
        res = dict(target)

    try:
        _update_cached_live_scores_payload()
    except Exception:
        pass
    return res


def remove_simulated_live_game(match_id: str = "sim-999", comp_name: str = "Test/Simulation") -> bool:
    with _live_scores_lock:
        comp = _live_scores.get(comp_name)
        if not comp:
            return False
        games = comp.get("games", [])
        before = len(games)
        comp["games"] = [g for g in games if g.get("match_id") != match_id]
        after = len(comp["games"])
        if not comp["games"]:
            _live_scores.pop(comp_name, None)
    try:
        _update_cached_live_scores_payload()
    except Exception:
        pass
    return after < before

