"""
Live scores, APNs push notifications, and Live Activities API routes.
"""

from __future__ import annotations

import hashlib
import os
import queue
import threading
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo
from starlette.responses import StreamingResponse
from .compat import Blueprint, Response, jsonify, request
import pandas as pd
from shared import sqlite_store as _sqlite_store

from cache import _cached_response
import config
from live_poller import (
    _effective_poller_date,
    _get_todays_competitions,
    _live_score_poller_loop,
    _live_scores,
    _live_scores_lock,
    _live_summary_cache,
    _live_summary_cache_lock,
    get_cached_compact_live_scores_payload,
    get_cached_compact_sse_frame,
    get_cached_live_scores_payload,
    get_cached_sse_frame,
    get_live_poller_status,
    refresh_live_scores_now,
    register_sse_subscriber,
    to_compact_live_game,
    to_compact_live_scores_data,
    unregister_sse_subscriber,
)
from espn_api import _fetch_competition_scores
from standings import _load_live_score_history
from predictions import _valid_date_iso
from notifications import (
    ALL_EVENT_TYPES,
    _apns_notification_queue,
    _notifications,
    device_tokens,
    get_device_subscriptions,
    ios_device_tokens,
    normalize_events,
    send_live_activity_end,
    send_live_activity_update,
    subscribe_match,
    subscribe_team,
    unsubscribe_match,
    unsubscribe_team,
)
import notifications as live_activities
from routes.helpers import mutation_authorized
from team_utils import decorate_game_team_names, resolve_team_info

live_scores_bp = Blueprint("live_scores", __name__)
live_scores_router = live_scores_bp


@live_scores_bp.get("/api/live-scores")
@live_scores_bp.get("/api/live-scores/compact")
def api_live_scores():
    """Return live scores for active competitions (polled from ESPN)."""
    comp_filter = request.args.get("competition", "").strip()
    force_refresh = request.args.get("refresh", "").strip().lower() in {"1", "true", "yes"}
    compact = (
        request.args.get("compact", "").strip().lower() in {"1", "true", "yes"}
        or request.path.endswith("/compact")
    )

    if force_refresh:
        try:
            refresh_live_scores_now(force=force_refresh)
        except Exception:
            pass
            import traceback
            traceback.print_exc()

    # Fast path: unfiltered requests serve pre-serialized JSON with ETag / 304 Not Modified
    if not comp_filter:
        raw_bytes, etag = (
            get_cached_compact_live_scores_payload()
            if compact
            else get_cached_live_scores_payload()
        )
        if_none_match = request.headers.get("if-none-match", "").strip()
        if if_none_match and (if_none_match == etag or if_none_match == etag.strip('"')):
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
        return Response(
            content=raw_bytes,
            media_type="application/json",
            headers={"ETag": etag, "Cache-Control": "no-cache"},
        )

    poller_status = get_live_poller_status()
    with _live_scores_lock:
        if not _live_scores:
            return jsonify({
                "ok": True,
                "competitions": {},
                "message": "No live games at this time.",
                "poller": poller_status,
                "compact": compact,
            })
        wanted = {c.strip() for c in comp_filter.split(",") if c.strip()}
        filtered = {}
        for k, v in _live_scores.items():
            if k in wanted:
                v_copy = dict(v)
                v_copy["games"] = [decorate_game_team_names(dict(g), k) for g in v.get("games", [])]
                filtered[k] = v_copy
        if compact:
            filtered = to_compact_live_scores_data(filtered)
        return jsonify({
            "ok": True,
            "competitions": filtered,
            "poller": poller_status,
            "compact": compact,
        })


@live_scores_bp.get("/api/live-scores/stream")
def api_live_scores_stream():
    """Real-time Server-Sent Events (SSE) stream broadcasting live score updates."""
    compact = request.args.get("compact", "").strip().lower() in {"1", "true", "yes"}
    q = register_sse_subscriber(compact=compact)

    def event_generator():
        try:
            # 1. Immediately yield initial connection confirmation so client finishes handshake in <1ms
            yield ": stream-connected\n\n"

            # 2. Yield current cached snapshot immediately without decoding/reformatting
            snapshot = (
                get_cached_compact_sse_frame()
                if compact
                else get_cached_sse_frame()
            )
            if snapshot:
                yield snapshot

            # 3. Stream ongoing frames from the queue
            while True:
                try:
                    update = q.get(timeout=20.0)
                    if isinstance(update, bytes):
                        update = update.decode("utf-8")
                    if not update.startswith("data:"):
                        yield f"data: {update}\n\n"
                    else:
                        yield update
                except queue.Empty:
                    # Heartbeat comment to keep persistent connection open
                    yield ": keep-alive\n\n"
        except (GeneratorExit, Exception):
            pass
        finally:
            unregister_sse_subscriber(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _enrich_with_upcoming_prediction(entry: dict, mid: str) -> dict:
    """Merge comprehensive upcoming prediction data (probabilities, distributions, secondary markets, reasoning, odds) into match payload."""
    clean_mid = str(mid or "").strip()
    unquoted_mid = urllib.parse.unquote_plus(clean_mid).strip()

    if not entry.get("sync_key") or not entry.get("canonical_home_team"):
        try:
            from live_poller import _stamp_game_sync_fields as _stamp_fields
            _stamp_fields(entry, entry.get("competition", ""))
        except Exception:
            pass

    pred_row = None
    try:
        from predictions import _build_past_game_prediction_lookup, _past_row_date_iso
        lookup = _build_past_game_prediction_lookup()

        if lookup:
            cand_keys = []
            if clean_mid:
                cand_keys.extend([f"mid:{clean_mid}", clean_mid])
            if unquoted_mid and unquoted_mid != clean_mid:
                cand_keys.extend([f"mid:{unquoted_mid}", unquoted_mid])
            if entry.get("sync_key"):
                cand_keys.append(entry["sync_key"])
            date_iso = _past_row_date_iso(entry)
            c_home = str(entry.get("canonical_home_team", "") or "").strip().lower()
            c_away = str(entry.get("canonical_away_team", "") or "").strip().lower()
            if date_iso and c_home and c_away:
                cand_keys.append(f"{date_iso}|{c_home}|{c_away}")
            comp = str(entry.get("competition", "")).strip().lower()
            home = str(entry.get("home_team", "")).strip().lower()
            away = str(entry.get("away_team", "")).strip().lower()
            if date_iso and comp and home and away:
                cand_keys.append(f"{date_iso}|{comp}|{home}|{away}")

            for k in cand_keys:
                if k in lookup:
                    pred_row = lookup[k]
                    break
    except Exception:
        pred_row = None

    if not pred_row:
        try:
            for cand in (clean_mid, unquoted_mid, entry.get("sync_key")):
                if cand:
                    pred_row = _sqlite_store.get_game_by_match_id(cand)
                    if pred_row:
                        break
        except Exception:
            pass

    prediction_fields = {
        "prob_home", "prob_draw", "prob_away",
        "prob_home_text", "prob_draw_text", "prob_away_text",
        "pred_home_goals", "pred_away_goals",
        "pred_home_shots", "pred_away_shots", "pred_home_sot", "pred_away_sot",
        "prob_both_score", "prob_over_1_5", "prob_over_2_5", "prob_over_3_5",
        "prob_home_goals_0", "prob_home_goals_1plus", "prob_home_goals_2plus",
        "prob_away_goals_0", "prob_away_goals_1plus", "prob_away_goals_2plus",
        "correct_score_dist", "double_chance", "asian_handicap", "total_goals_dist",
        "first_to_score", "clean_sheet",
        "home_attack_rating", "home_defence_rating", "away_attack_rating", "away_defence_rating",
        "last_5_home", "last_5_away",
        "winner_label", "predicted_result", "prediction_key", "prediction_quality",
        "has_prediction", "prediction_note", "reasoning", "probability_reasoning",
        "odds_home", "odds_draw", "odds_away", "schedule_only", "unmapped_teams",
        "match_datetime_et", "match_datetime_utc", "date_label", "time_label", "weekday",
    }

    preserve = {
        "match_id",
        "home_score",
        "away_score",
        "status",
        "clock",
        "period",
        "match_minute",
        "events",
        "key_events",
        "lineups",
        "stats",
        "live_status",
        "live_clock",
        "live_period",
        "live_prediction",
        "live_updates",
        "actual_home_goals",
        "actual_away_goals",
        "actual_result",
        "is_correct",
        "is_live",
    }

    if pred_row:
        for k, v in pred_row.items():
            if k in preserve:
                continue
            if k in prediction_fields:
                if entry.get(k) in (None, "", [], {}) or v not in (None, "", [], {}):
                    entry[k] = v
            elif entry.get(k) in (None, "", [], {}):
                entry[k] = v

    # Fallback to prematch/upcoming_prediction if present on entry
    for sub_key in ("upcoming_prediction", "prematch_prediction"):
        if isinstance(entry.get(sub_key), dict):
            for k, v in entry[sub_key].items():
                if k not in preserve and (entry.get(k) in (None, "", [], {}) or k in prediction_fields):
                    if entry.get(k) is None:
                        entry[k] = v

    # Fallback math calculations for missing secondary prediction markets
    hg = entry.get("pred_home_goals")
    ag = entry.get("pred_away_goals")
    ph = entry.get("prob_home")
    pdv = entry.get("prob_draw")
    pa = entry.get("prob_away")

    # Double chance
    if not entry.get("double_chance") and (ph is not None or pdv is not None or pa is not None):
        try:
            from math_utils import _compute_double_chance
            ph_val = float(ph or 0)
            pd_val = float(pdv or 0)
            pa_val = float(pa or 0)
            if ph_val > 1.0 or pd_val > 1.0 or pa_val > 1.0:
                ph_val /= 100.0
                pd_val /= 100.0
                pa_val /= 100.0
            dc = _compute_double_chance(ph_val, pd_val, pa_val)
            if dc:
                entry["double_chance"] = dc
        except Exception:
            pass

    # Correct score distribution
    if not entry.get("correct_score_dist") and (hg is not None and ag is not None):
        try:
            from math_utils import _compute_correct_score_dist
            entry["correct_score_dist"] = _compute_correct_score_dist(hg, ag)
        except Exception:
            pass

    # Asian handicap
    if not entry.get("asian_handicap") and (hg is not None and ag is not None):
        try:
            from math_utils import _compute_asian_handicap
            ph_val = (float(ph) / 100.0 if float(ph) > 1.0 else float(ph)) if ph is not None else None
            pd_val = (float(pdv) / 100.0 if float(pdv) > 1.0 else float(pdv)) if pdv is not None else None
            pa_val = (float(pa) / 100.0 if float(pa) > 1.0 else float(pa)) if pa is not None else None
            entry["asian_handicap"] = _compute_asian_handicap(hg, ag, prob_home=ph_val, prob_draw=pd_val, prob_away=pa_val)
        except Exception:
            pass

    # Total goals distribution
    if not entry.get("total_goals_dist") and (hg is not None and ag is not None):
        try:
            from math_utils import _compute_total_goals_dist
            entry["total_goals_dist"] = _compute_total_goals_dist(hg, ag)
        except Exception:
            pass

    # First to score
    if not entry.get("first_to_score") and (hg is not None and ag is not None):
        try:
            from math_utils import _compute_first_to_score
            entry["first_to_score"] = _compute_first_to_score(hg, ag)
        except Exception:
            pass

    # Clean sheet
    if not entry.get("clean_sheet") and (hg is not None and ag is not None):
        try:
            from math_utils import _compute_clean_sheet
            entry["clean_sheet"] = _compute_clean_sheet(hg, ag)
        except Exception:
            pass

    # Secondary goal probabilities
    if (entry.get("prob_both_score") is None or entry.get("prob_over_2_5") is None) and (hg is not None and ag is not None):
        try:
            from math_utils import _compute_goal_probabilities
            probs = _compute_goal_probabilities(float(hg), float(ag))
            for gp in (
                "prob_both_score", "prob_over_1_5", "prob_over_2_5", "prob_over_3_5",
                "prob_home_goals_0", "prob_home_goals_1plus", "prob_home_goals_2plus",
                "prob_away_goals_0", "prob_away_goals_1plus", "prob_away_goals_2plus",
            ):
                if entry.get(gp) is None and gp in probs:
                    entry[gp] = round(float(probs[gp]), 6)
        except Exception:
            pass

    # Prob text formatting
    if entry.get("prob_home") is not None:
        try:
            ph_val = float(entry["prob_home"])
            if ph_val <= 1.0 and ph_val > 0:
                ph_val *= 100.0
            raw_txt = str(entry.get("prob_home_text", "")).strip()
            if not raw_txt or raw_txt.endswith(".0") or float(raw_txt.rstrip("%") or 0) > 100:
                entry["prob_home_text"] = f"{ph_val:.1f}%"
        except Exception:
            pass
    if entry.get("prob_draw") is not None:
        try:
            pd_val = float(entry["prob_draw"])
            if pd_val <= 1.0 and pd_val > 0:
                pd_val *= 100.0
            raw_txt = str(entry.get("prob_draw_text", "")).strip()
            if not raw_txt or raw_txt.endswith(".0") or float(raw_txt.rstrip("%") or 0) > 100:
                entry["prob_draw_text"] = f"{pd_val:.1f}%"
        except Exception:
            pass
    if entry.get("prob_away") is not None:
        try:
            pa_val = float(entry["prob_away"])
            if pa_val <= 1.0 and pa_val > 0:
                pa_val *= 100.0
            raw_txt = str(entry.get("prob_away_text", "")).strip()
            if not raw_txt or raw_txt.endswith(".0") or float(raw_txt.rstrip("%") or 0) > 100:
                entry["prob_away_text"] = f"{pa_val:.1f}%"
        except Exception:
            pass

    # Last 5 form
    h_team = entry.get("home_team")
    if not entry.get("last_5_home") and h_team:
        try:
            entry["last_5_home"] = _sqlite_store.load_team_recent_matches(str(h_team), limit=5)
        except Exception:
            entry["last_5_home"] = []
    elif entry.get("last_5_home") is None:
        entry["last_5_home"] = []

    a_team = entry.get("away_team")
    if not entry.get("last_5_away") and a_team:
        try:
            entry["last_5_away"] = _sqlite_store.load_team_recent_matches(str(a_team), limit=5)
        except Exception:
            entry["last_5_away"] = []
    elif entry.get("last_5_away") is None:
        entry["last_5_away"] = []

    # Ratings
    for r_key in ("home_attack_rating", "home_defence_rating", "away_attack_rating", "away_defence_rating"):
        if r_key not in entry:
            entry[r_key] = None

    # Winner label
    if not entry.get("winner_label") and entry.get("predicted_result"):
        try:
            from predictions import _winner_label
            entry["winner_label"] = _winner_label(entry["predicted_result"], str(h_team or ""), str(a_team or ""))
        except Exception:
            pass

    # Ensure reasoning fields synced
    r_val = str(entry.get("reasoning") or entry.get("probability_reasoning") or "").strip()
    if r_val:
        entry["reasoning"] = r_val
        entry["probability_reasoning"] = r_val

    # Snapshot dictionary for upcoming_prediction and prematch_prediction
    pred_snapshot = {k: entry.get(k) for k in prediction_fields if k in entry}
    if not isinstance(entry.get("upcoming_prediction"), dict):
        entry["upcoming_prediction"] = {}
    entry["upcoming_prediction"].update(pred_snapshot)

    if not isinstance(entry.get("prematch_prediction"), dict):
        entry["prematch_prediction"] = {}
    entry["prematch_prediction"].update(pred_snapshot)

    return entry


_MATCH_DETAIL_CACHE: dict[str, tuple[float, dict, str]] = {}
_MATCH_DETAIL_CACHE_LOCK = threading.Lock()
_MATCH_DETAIL_LIVE_TTL = 3.0
_MATCH_DETAIL_PRE_TTL = 60.0
_MATCH_DETAIL_POST_TTL = 3600.0


def clear_match_detail_cache() -> None:
    with _MATCH_DETAIL_CACHE_LOCK:
        _MATCH_DETAIL_CACHE.clear()


@live_scores_bp.get("/api/match")
@live_scores_bp.get("/api/match/<path:match_id>")
def api_match_detail(match_id: str = ""):
    """Return complete detailed information for a single match with caching, ETags, and edge headers."""
    mid = str(
        match_id
        or request.args.get("match_id")
        or request.args.get("sync_key")
        or request.args.get("id")
        or request.args.get("game_id")
        or ""
    ).strip()
    if not mid:
        return jsonify({"ok": False, "error": "match_id required"}), 400

    unquoted_mid = urllib.parse.unquote_plus(mid).strip()
    candidate_ids = {mid, unquoted_mid}
    for prefix in ("fixture:", "prediction:", "id:"):
        for c in list(candidate_ids):
            if c.startswith(prefix):
                candidate_ids.add(c[len(prefix):])
            else:
                candidate_ids.add(f"{prefix}{c}")

    now = time.time()
    if_none_match = request.headers.get("if-none-match", "").strip('"').strip()

    # 0. Check in-memory fast cache
    with _MATCH_DETAIL_CACHE_LOCK:
        cached_item = _MATCH_DETAIL_CACHE.get(mid) or _MATCH_DETAIL_CACHE.get(unquoted_mid)
        if cached_item:
            exp_ts, cached_payload, cached_etag = cached_item
            if now < exp_ts:
                game_st = str(cached_payload.get("game", {}).get("status", "")).strip().lower()
                cc = (
                    "public, max-age=5, s-maxage=5, stale-while-revalidate=5" if game_st == "in"
                    else ("public, max-age=3600, s-maxage=86400, stale-while-revalidate=600" if game_st in ("post", "ft", "final")
                    else "public, max-age=60, s-maxage=300, stale-while-revalidate=60")
                )
                if if_none_match and if_none_match == cached_etag:
                    return Response("", status_code=304, headers={"ETag": f'"{cached_etag}"', "Cache-Control": cc, "Vary": "Accept-Encoding"})
                resp = jsonify(cached_payload)
                resp.headers["ETag"] = f'"{cached_etag}"'
                resp.headers["Cache-Control"] = cc
                resp.headers["Vary"] = "Accept-Encoding"
                resp.headers["X-Cache"] = "HIT"
                return resp

    # Look up match across sources
    def _apply_live_summary(game_dict: dict) -> dict:
        entry = dict(game_dict)
        with _live_summary_cache_lock:
            for cand in candidate_ids:
                cached = _live_summary_cache.get(cand)
                if cached:
                    for k, v in cached.items():
                        if v not in (None, "", [], {}):
                            entry[k] = v
                    break
        return _enrich_with_upcoming_prediction(entry, mid)

    def _ensure_game_sync(game: dict) -> dict:
        return _apply_live_summary(game)

    found_game = None
    found_source = ""

    # 1. Check live in-memory games first
    with _live_scores_lock:
        for comp_name, comp_data in _live_scores.items():
            for g in comp_data.get("games", []):
                g_mid = str(g.get("match_id", "")).strip()
                g_sync = str(g.get("sync_key", "")).strip()
                if (g_mid and g_mid in candidate_ids) or (g_sync and g_sync in candidate_ids):
                    game_dict = dict(g)
                    game_dict.setdefault("competition", comp_name)
                    found_game = _apply_live_summary(game_dict)
                    found_source = "live"
                    break
            if found_game:
                break

    # 2. Check targeted single-row lookup in SQLite (fast indexed query!)
    if not found_game:
        try:
            g = _sqlite_store.get_game_by_match_id(mid)
            if not g and unquoted_mid != mid:
                g = _sqlite_store.get_game_by_match_id(unquoted_mid)
            if g:
                found_game = _apply_live_summary(dict(g))
                st = str(found_game.get("status", "")).strip().lower()
                found_source = "upcoming" if st in ("upcoming", "pre") else "history"
        except Exception:
            pass

    # 3. Check upcoming predictions lookup index (in-memory O(1))
    if not found_game:
        try:
            from predictions import _build_past_game_prediction_lookup
            lookup = _build_past_game_prediction_lookup()
            for cand in (f"mid:{mid}", mid, unquoted_mid, f"mid:{unquoted_mid}"):
                if cand in lookup:
                    found_game = _apply_live_summary(dict(lookup[cand]))
                    found_source = "upcoming_prediction"
                    break
        except Exception:
            pass

    # 4. Fallback: check JSON history
    if not found_game:
        try:
            import json, os
            from Website import config
            hist_path = getattr(config, "LIVE_SCORE_HISTORY_PATH", None) or os.path.join(getattr(config, "STATUS_DIR", ""), "live_score_history.json")
            if hist_path and os.path.exists(hist_path):
                with open(hist_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                games = data if isinstance(data, list) else data.get("games", [])
                for g in games:
                    g_mid = str(g.get("match_id", "")).strip()
                    g_sync = str(g.get("sync_key", "")).strip()
                    if (g_mid and g_mid in candidate_ids) or (g_sync and g_sync in candidate_ids):
                        found_game = _apply_live_summary(dict(g))
                        found_source = "history_json"
                        break
        except Exception:
            pass

    # 5. Fallback: check CSV predictions
    if not found_game:
        try:
            from predictions import _load_upcoming_rows
            for source, csv_path in _ALL_UPCOMING_SOURCES:
                rows, _, _ = _load_upcoming_rows(csv_path, source, date_range="all")
                for g in rows:
                    g_mid = str(g.get("match_id", "")).strip()
                    g_sync = str(g.get("sync_key", "")).strip()
                    if (g_mid and g_mid in candidate_ids) or (g_sync and g_sync in candidate_ids):
                        found_game = _apply_live_summary(dict(g))
                        found_source = "upcoming_csv"
                        break
                if found_game:
                    break
        except Exception:
            pass

    if not found_game:
        return jsonify({"ok": False, "error": f"Match {mid} not found"}), 404

    found_game = decorate_game_team_names(found_game, found_game.get("competition", ""))
    if isinstance(found_game.get("upcoming_prediction"), dict):
        decorate_game_team_names(found_game["upcoming_prediction"], found_game.get("competition", ""))
    if isinstance(found_game.get("prematch_prediction"), dict):
        decorate_game_team_names(found_game["prematch_prediction"], found_game.get("competition", ""))
    payload = {"ok": True, "source": found_source, "game": found_game}

    # Compute ETag and caching headers
    st_val = str(found_game.get("status", "")).strip().lower()
    h_sc = str(found_game.get("home_score", ""))
    a_sc = str(found_game.get("away_score", ""))
    clk = str(found_game.get("clock", found_game.get("live_clock", "")))
    etag_raw = f"{mid}:{st_val}:{h_sc}:{a_sc}:{clk}"
    etag = hashlib.md5(etag_raw.encode("utf-8")).hexdigest()[:16]

    if st_val == "in":
        ttl = _MATCH_DETAIL_LIVE_TTL
        cc = "public, max-age=5, s-maxage=5, stale-while-revalidate=5"
    elif st_val in ("post", "ft", "final"):
        ttl = _MATCH_DETAIL_POST_TTL
        cc = "public, max-age=3600, s-maxage=86400, stale-while-revalidate=600"
    else:
        ttl = _MATCH_DETAIL_PRE_TTL
        cc = "public, max-age=60, s-maxage=300, stale-while-revalidate=60"

    with _MATCH_DETAIL_CACHE_LOCK:
        _MATCH_DETAIL_CACHE[mid] = (now + ttl, payload, etag)
        if unquoted_mid != mid:
            _MATCH_DETAIL_CACHE[unquoted_mid] = (now + ttl, payload, etag)

    if if_none_match and if_none_match == etag:
        return Response("", status_code=304, headers={"ETag": f'"{etag}"', "Cache-Control": cc, "Vary": "Accept-Encoding"})

    resp = jsonify(payload)
    resp.headers["ETag"] = f'"{etag}"'
    resp.headers["Cache-Control"] = cc
    resp.headers["Vary"] = "Accept-Encoding"
    resp.headers["X-Cache"] = "MISS"
    return resp


@live_scores_bp.get("/api/live-score-history")
@live_scores_bp.get("/api/past-live-scores")
@_cached_response(ttl=config.CACHE_TTL_DEFAULT)
def api_live_score_history():
    """Return historical completed games, grouped by competition."""
    compact = request.args.get("compact", "").strip().lower() in {"1", "true", "yes"}
    league = request.args.get("league", "").strip()
    match_id = request.args.get("match_id", "").strip()
    target_date = request.args.get("date", "").strip()
    from_date = request.args.get("from", "").strip()
    to_date = request.args.get("to", "").strip()

    today_iso = ""
    try:
        today_iso = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        today_iso = datetime.now(timezone.utc).date().isoformat()

    if target_date.lower() == "today":
        target_date = today_iso
    if from_date.lower() == "today":
        from_date = today_iso
    if to_date.lower() == "today":
        to_date = today_iso

    # If single exact date is provided (date=YYYY-MM-DD or date=today), scope both from and to
    if target_date and not from_date and not to_date:
        from_date = target_date
        to_date = target_date

    limit = None
    offset = 0
    raw_limit = request.args.get("limit", "").strip()
    raw_offset = request.args.get("offset", "").strip()
    if raw_limit.isdigit():
        limit = max(1, int(raw_limit))
    if raw_offset.isdigit():
        offset = max(0, int(raw_offset))

    if from_date and not _valid_date_iso(from_date):
        return jsonify({"ok": False, "error": "Invalid 'from' date format (use YYYY-MM-DD)"}), 400
    if to_date and not _valid_date_iso(to_date):
        return jsonify({"ok": False, "error": "Invalid 'to' date format (use YYYY-MM-DD)"}), 400

    games = None
    used_sqlite = False
    try:
        if _sqlite_store.count_rows("live_score_history") > 0:
            games = _sqlite_store.load_live_score_history(
                competition_substr=league,
                from_date=from_date,
                to_date=to_date,
                limit=limit if not match_id else None,
                offset=offset if not match_id else 0,
            )
            used_sqlite = True
    except Exception:
        games = None
        used_sqlite = False

    if not used_sqlite:
        games = _load_live_score_history()
        if league:
            league_lower = league.lower()
            games = [g for g in games if league_lower in g.get("competition", "").lower()]
        if from_date:
            games = [g for g in games if g.get("kickoff_utc", "") >= from_date]
        if to_date:
            games = [g for g in games if g.get("kickoff_utc", "") <= to_date]
        games.sort(key=lambda g: g.get("kickoff_utc", ""), reverse=True)

    if match_id and games:
        games = [g for g in games if str(g.get("match_id", "")).strip() == match_id]

    if not used_sqlite:
        if offset:
            games = games[offset:]
        if limit:
            games = games[:limit]
    elif match_id and games:
        if offset:
            games = games[offset:]
        if limit:
            games = games[:limit]

    competitions = {}
    for g in (games or []):
        comp = g.get("competition", "Unknown")
        if comp not in competitions:
            competitions[comp] = {
                "competition": comp,
                "games": [],
                "last_polled_utc": datetime.now(timezone.utc).isoformat(),
            }
        g_dec = decorate_game_team_names(dict(g), comp)
        competitions[comp]["games"].append(to_compact_live_game(g_dec) if compact else g_dec)

    res = {
        "ok": True,
        "competitions": competitions,
    }
    if compact:
        res["compact"] = True
    return jsonify(res)


@live_scores_bp.get("/api/debug/live-score-sources")
def api_debug_live_score_sources():
    """Debug endpoint: show what _get_todays_competitions() detects and which files exist/stale."""
    if not mutation_authorized():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    info = {}
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    info["today_date"] = today_et.isoformat()
    info["now_et"] = datetime.now(ZoneInfo("America/New_York")).isoformat()
    info["csv_files"] = {}
    for name, path in config.UPCOMING_CSV_FILES.items():
        entry = {"exists": os.path.exists(path)}
        if entry["exists"]:
            entry["size_bytes"] = os.path.getsize(path)
            entry["mtime_utc"] = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat()
            try:
                df = pd.read_csv(path, dtype=str)
                entry["rows"] = len(df)
                if "match_datetime_utc" in df.columns:
                    utc_dates = pd.to_datetime(df["match_datetime_utc"], errors="coerce")
                    if hasattr(utc_dates.dt, "tz") and utc_dates.dt.tz is not None:
                        et_dates = utc_dates.dt.tz_convert(ZoneInfo("America/New_York"))
                    else:
                        et_dates = utc_dates.dt.tz_localize("UTC", ambiguous="NaT").dt.tz_convert(ZoneInfo("America/New_York"))
                    entry["date_range"] = [et_dates.min().strftime("%Y-%m-%d") if pd.notna(et_dates.min()) else None,
                                           et_dates.max().strftime("%Y-%m-%d") if pd.notna(et_dates.max()) else None]
                    entry["today_count"] = int((et_dates.dt.date == today_et).sum())
                elif "match_date" in df.columns:
                    parsed = pd.to_datetime(df["match_date"], errors="coerce", dayfirst=False)
                    entry["date_range"] = [parsed.min().strftime("%Y-%m-%d") if pd.notna(parsed.min()) else None,
                                           parsed.max().strftime("%Y-%m-%d") if pd.notna(parsed.max()) else None]
                    entry["today_count"] = int((parsed.dt.date == today_et).sum())
                entry["competitions"] = sorted(df["competition"].dropna().unique().tolist()) if "competition" in df.columns else []
            except Exception as e:
                entry["read_error"] = str(e)
        info["csv_files"][name] = entry
    info["wc_projection"] = {"exists": os.path.exists(config.WORLD_CUP_PROJECTION_FILE)}
    if info["wc_projection"]["exists"]:
        info["wc_projection"]["size_bytes"] = os.path.getsize(config.WORLD_CUP_PROJECTION_FILE)
    info["cup_bracket"] = {"exists": os.path.exists(config.CUP_PROJECTED_BRACKET_FILE)}
    if info["cup_bracket"]["exists"]:
        info["cup_bracket"]["size_bytes"] = os.path.getsize(config.CUP_PROJECTED_BRACKET_FILE)
    todays_comps = _get_todays_competitions()
    info["todays_competitions"] = {k: [v.isoformat() for v in vs] for k, vs in todays_comps.items()}
    return jsonify({"ok": True, "debug": info})


@live_scores_bp.get("/api/debug/manual-poll")
def api_debug_manual_poll():
    """Manually run one ESPN poll cycle and return the results."""
    if not mutation_authorized():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    today_str = _effective_poller_date().strftime("%Y%m%d")
    all_results = {}
    for comp_name, espn_id in config.LIVE_SCORE_COMPETITIONS.items():
        try:
            games = _fetch_competition_scores(comp_name, espn_id, today_str)
        except Exception:
            continue
        if games:
            all_results[comp_name] = {
                "competition": comp_name,
                "games": games,
                "last_polled_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
            }
    return jsonify({
        "ok": True,
        "today_str": today_str,
        "competitions_found": len(all_results),
        "total_games": sum(len(v["games"]) for v in all_results.values()),
        "live_scores": all_results,
    })


@live_scores_bp.get("/api/debug/poller-state")
def api_debug_poller_state():
    """Show what the poller thread currently has stored and its last poll timing."""
    if not mutation_authorized():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    with _live_scores_lock:
        state = {
            "poll_competitions": list(_live_scores.keys()),
            "total_games": sum(len(v.get("games", [])) for v in _live_scores.values()),
            "last_polled_utc": max(
                (v.get("last_polled_utc", "") for v in _live_scores.values()),
                default=None,
            ),
            "poller_date": getattr(_live_score_poller_loop, "_poller_date", None),
            "live_scores": _live_scores,
        }
    return jsonify({"ok": True, "state": state})


@live_scores_bp.post("/api/notifications")
def api_push_notification():
    """Queue a push notification for delivery via APNs."""
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title", "")).strip()
    body = str(payload.get("body", "")).strip()
    if not title or not body:
        return jsonify({"ok": False, "error": "title and body required"}), 400
    badge = payload.get("badge", 0)
    try:
        badge = max(0, int(badge))
    except (TypeError, ValueError):
        badge = 0
    _notifications.append({
        "id": len(_notifications),
        "title": title,
        "body": body,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "type": payload.get("type", "info"),
    })
    for device_token in list(ios_device_tokens):
        _apns_notification_queue.append({
            "token": device_token,
            "title": title,
            "body": body,
            "badge": badge,
        })
    try:
        from notifications import _notify_apns_queue
        _notify_apns_queue()
    except Exception:
        pass
    return jsonify({"ok": True})


@live_scores_bp.get("/api/notifications")
def api_get_notifications():
    """Return recent notifications."""
    limit = min(int(request.args.get("limit", "20")), 100)
    items = list(_notifications)[-limit:]
    return jsonify({"ok": True, "notifications": items})


@live_scores_bp.post("/api/notifications/register")
def api_register_device():
    """Register a device token for push notifications."""
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "")).strip().replace(" ", "").replace("<", "").replace(">", "")
    if not token:
        return jsonify({"ok": False, "error": "token required"}), 400
    if len(token) > 512:
        return jsonify({"ok": False, "error": "token too long"}), 400
    platform = str(payload.get("platform", "generic")).strip().lower()
    env = payload.get("environment") or request.headers.get("X-Apns-Environment")
    if env:
        try:
            from notifications import set_token_environment
            set_token_environment(token, env)
        except Exception:
            pass

    if platform == "ios":
        ios_device_tokens.add(token)
    else:
        device_tokens.add(token)
    return jsonify({"ok": True, "registered": True, "environment": env or "auto"})


@live_scores_bp.post("/api/notifications/unregister")
def api_unregister_device():
    """Remove a device token."""
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "")).strip().replace(" ", "").replace("<", "").replace(">", "")
    if not token:
        return jsonify({"ok": False, "error": "token required"}), 400
    platform = str(payload.get("platform", "generic")).strip().lower()
    if platform == "ios":
        ios_device_tokens.discard(token)
    else:
        device_tokens.discard(token)
    return jsonify({"ok": True, "removed": True})


@live_scores_bp.post("/api/notifications/subscribe")
def api_subscribe_match_notifications():
    """Subscribe a device to live-event alerts for a specific match or team with event preferences."""
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "")).strip().replace(" ", "").replace("<", "").replace(">", "")
    match_id = str(payload.get("match_id", "")).strip()
    team = str(payload.get("team", "") or payload.get("team_name", "")).strip()
    competition = str(payload.get("competition", "")).strip()
    events = payload.get("events")
    env = payload.get("environment") or request.headers.get("X-Apns-Environment")

    if not token:
        return jsonify({"ok": False, "error": "token required"}), 400
    if not match_id and not team:
        return jsonify({"ok": False, "error": "either match_id or team required"}), 400
    if match_id and not competition:
        try:
            from live_poller import _live_scores, _live_scores_lock
            with _live_scores_lock:
                for c_name, comp_data in _live_scores.items():
                    game_list = comp_data.get("games", []) if isinstance(comp_data, dict) else comp_data
                    for g in (game_list or []):
                        if isinstance(g, dict) and (str(g.get("match_id", "")).strip() == match_id or str(g.get("id", "")).strip() == match_id):
                            competition = c_name
                            break
                    if competition:
                        break
        except Exception:
            pass

        competition = competition or ""
    if len(token) > 512 or len(match_id) > 256 or len(competition) > 256 or len(team) > 256:
        return jsonify({"ok": False, "error": "input too long"}), 400

    norm_events = normalize_events(events)
    subscribed_targets = []

    if match_id:
        ok_m = subscribe_match(token, match_id, competition, events=norm_events, environment=env)
        if ok_m:
            subscribed_targets.append(f"match:{match_id}")

    if team:
        ok_t = subscribe_team(token, team, competition=competition, events=norm_events, environment=env)
        if ok_t:
            subscribed_targets.append(f"team:{team}")

    return jsonify({
        "ok": True,
        "subscribed": bool(subscribed_targets),
        "targets": subscribed_targets,
        "events": sorted(list(norm_events)),
        "environment": env or "auto",
    })


@live_scores_bp.post("/api/notifications/unsubscribe")
def api_unsubscribe_match_notifications():
    """Remove a device from a match or team's live-event alert list."""
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "")).strip()
    match_id = str(payload.get("match_id", "")).strip()
    team = str(payload.get("team", "") or payload.get("team_name", "")).strip()
    competition = str(payload.get("competition", "")).strip()

    if not token:
        return jsonify({"ok": False, "error": "token required"}), 400
    if not match_id and not team:
        return jsonify({"ok": False, "error": "either match_id or team required"}), 400
    if len(token) > 512 or len(match_id) > 256 or len(competition) > 256 or len(team) > 256:
        return jsonify({"ok": False, "error": "input too long"}), 400

    unsub_count = 0
    if match_id:
        if unsubscribe_match(token, match_id, competition):
            unsub_count += 1
    if team:
        if unsubscribe_team(token, team):
            unsub_count += 1

    return jsonify({"ok": True, "unsubscribed": unsub_count > 0, "count": unsub_count})


@live_scores_bp.post("/api/notifications/subscribe/team")
def api_subscribe_team():
    """Subscribe a device token to all matches involving a specific team with optional event preferences."""
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "")).strip().replace(" ", "").replace("<", "").replace(">", "")
    team = str(payload.get("team", "") or payload.get("team_name", "")).strip()
    competition = str(payload.get("competition", "")).strip()
    events = payload.get("events")
    env = payload.get("environment") or request.headers.get("X-Apns-Environment")

    if not token or not team:
        return jsonify({"ok": False, "error": "token and team required"}), 400
    if len(token) > 512 or len(team) > 256 or len(competition) > 256:
        return jsonify({"ok": False, "error": "input too long"}), 400

    norm_events = normalize_events(events)
    ok = subscribe_team(token, team, competition=competition, events=norm_events, environment=env)
    return jsonify({
        "ok": True,
        "subscribed": ok,
        "team": team,
        "events": sorted(list(norm_events)),
        "environment": env or "auto",
    })


@live_scores_bp.post("/api/notifications/unsubscribe/team")
def api_unsubscribe_team():
    """Unsubscribe a device token from team notifications."""
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "")).strip()
    team = str(payload.get("team", "") or payload.get("team_name", "")).strip()

    if not token or not team:
        return jsonify({"ok": False, "error": "token and team required"}), 400
    if len(token) > 512 or len(team) > 256:
        return jsonify({"ok": False, "error": "input too long"}), 400

    ok = unsubscribe_team(token, team)
    return jsonify({"ok": True, "unsubscribed": ok, "team": team})


@live_scores_bp.get("/api/notifications/preferences")
def api_get_notification_preferences():
    """Return all active subscriptions and event preferences for a device token."""
    token = request.args.get("token", "").strip()
    if not token:
        return jsonify({"ok": False, "error": "token query parameter required"}), 400
    subs = get_device_subscriptions(token)
    return jsonify({
        "ok": True,
        "token": token,
        "matches": subs.get("matches", []),
        "teams": subs.get("teams", []),
        "match_events": subs.get("match_events", {}),
        "team_events": subs.get("team_events", {}),
        "available_events": sorted(list(ALL_EVENT_TYPES)),
    })


@live_scores_bp.post("/live-activities/register")
@live_scores_bp.post("/api/live-activities/register")
def api_register_live_activity():
    """Register a Live Activity push token for a specific match with optional event preferences."""
    payload = request.get_json(silent=True) or {}
    activity_token = str(payload.get("activity_token", "")).strip().replace(" ", "").replace("<", "").replace(">", "")
    match_id = str(payload.get("match_id", "")).strip()
    competition = str(payload.get("competition", "")).strip()
    events = payload.get("events")
    env = payload.get("environment") or request.headers.get("X-Apns-Environment")

    if not activity_token or not match_id:
        return jsonify({"ok": False, "error": "activity_token and match_id required"}), 400
    if not competition:
        try:
            from live_poller import _live_scores, _live_scores_lock
            with _live_scores_lock:
                for c_name, comp_data in _live_scores.items():
                    game_list = comp_data.get("games", []) if isinstance(comp_data, dict) else comp_data
                    for g in (game_list or []):
                        if isinstance(g, dict) and (str(g.get("match_id", "")).strip() == match_id or str(g.get("id", "")).strip() == match_id):
                            competition = c_name
                            break
                    if competition:
                        break
        except Exception:
            pass

        competition = competition or ""
    if len(activity_token) > 1024 or len(match_id) > 256 or len(competition) > 256:
        return jsonify({"ok": False, "error": "input too long"}), 400
    device_token = str(payload.get("device_token", "")).strip().replace(" ", "").replace("<", "").replace(">", "")
    if len(device_token) > 512:
        return jsonify({"ok": False, "error": "device_token too long"}), 400

    ok = live_activities.register(
        activity_token, device_token, match_id, competition, events=events, environment=env
    )
    if ok and device_token:
        subscribe_match(device_token, match_id, competition, events=events, environment=env)
    return jsonify({
        "ok": True,
        "registered": ok,
        "total": len(live_activities.all_activities()),
        "environment": env or "auto",
    })


@live_scores_bp.post("/live-activities/unregister")
@live_scores_bp.post("/api/live-activities/unregister")
def api_unregister_live_activity():
    """Remove a Live Activity registration."""
    payload = request.get_json(silent=True) or {}
    activity_token = str(payload.get("activity_token", "")).strip()
    if not activity_token:
        return jsonify({"ok": False, "error": "activity_token required"}), 400
    if len(activity_token) > 1024:
        return jsonify({"ok": False, "error": "activity_token too long"}), 400
    ok = live_activities.unregister(activity_token)
    if ok:
        match_id = str(payload.get("match_id", "")).strip()
        competition = str(payload.get("competition", "")).strip()
        device_token = str(payload.get("device_token", "")).strip()
        if match_id and competition and device_token:
            unsubscribe_match(device_token, match_id, competition)
    return jsonify({"ok": True, "removed": ok})


@live_scores_bp.post("/api/live-activities/update")
def api_update_live_activity():
    """Manually push a content-state update to all Live Activities for a match with optional alert."""
    payload = request.get_json(silent=True) or {}
    match_id = str(payload.get("match_id", "")).strip()
    competition = str(payload.get("competition", "")).strip()
    content_state = payload.get("content_state")
    alert = payload.get("alert")
    if not match_id or not competition or not isinstance(content_state, dict):
        return jsonify({"ok": False, "error": "match_id, competition, and content_state required"}), 400
    if len(match_id) > 256 or len(competition) > 256:
        return jsonify({"ok": False, "error": "input too long"}), 400
    sent = send_live_activity_update(match_id, competition, content_state, alert=alert)
    return jsonify({"ok": True, "sent": sent})


@live_scores_bp.post("/api/live-activities/end")
def api_end_live_activity():
    """End/dismiss Live Activities for a match with optional alert."""
    payload = request.get_json(silent=True) or {}
    match_id = str(payload.get("match_id", "")).strip()
    competition = str(payload.get("competition", "")).strip()
    alert = payload.get("alert")
    if not match_id or not competition:
        return jsonify({"ok": False, "error": "match_id and competition required"}), 400
    if len(match_id) > 256 or len(competition) > 256:
        return jsonify({"ok": False, "error": "input too long"}), 400
    content_state = payload.get("content_state", {})
    if not isinstance(content_state, dict):
        content_state = {}
    sent = send_live_activity_end(match_id, competition, content_state, alert=alert)
    return jsonify({"ok": True, "sent": sent, "deregistered": True})


@live_scores_bp.get("/api/startup")
@live_scores_bp.get("/api/bootstrap")
def api_startup():
    """Unified application launch endpoint.

    Bundles in a single call (< 25 KB):
      - today's compact live scores (including realtime live odds, goalscorers, cards)
      - today's compact upcoming predictions & scheduled matches
      - poller status and server timestamp
    """
    # 1. Today's live scores (compact)
    with _live_scores_lock:
        live_data = dict(_live_scores)
    compact_live = to_compact_live_scores_data(live_data)
    poller_status = get_live_poller_status()

    # 2. Today's upcoming games (compact)
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    today_str = today_et.isoformat()
    upcoming_today = []
    try:
        from predictions import _load_upcoming_rows
        from predictions import exclude_upcoming_only_rows, regional_espn_schedule_fallback
        from routes.predictions import _to_compact_upcoming_row

        feed = []
        seen_keys = set()
        for source, csv_path in _ALL_UPCOMING_SOURCES:
            rows, _, _ = _load_upcoming_rows(csv_path, source, date_range="all")
            for row in rows:
                key = "|".join(
                    str(row.get(f, "")).strip().lower()
                    for f in ("match_date_iso", "competition", "home_team", "away_team")
                )
                if key not in seen_keys:
                    seen_keys.add(key)
                    feed.append(row)

        if not feed:
            from shared import sqlite_store
            db_rows = sqlite_store.load_upcoming_games()
            if db_rows:
                feed = list(db_rows)

        feed = regional_espn_schedule_fallback(feed)
        for r in feed:
            d_iso = str(r.get("match_date_iso", "")).strip()
            if d_iso == today_str:
                upcoming_today.append(_to_compact_upcoming_row(r))

        # Ensure match_id from active live games is propagated onto upcoming_today
        live_id_by_sync = {}
        for c_data in compact_live.values():
            for lg in c_data.get("games", []):
                sk = lg.get("sync_key")
                mid = lg.get("match_id")
                if sk and mid:
                    live_id_by_sync[sk] = mid
        for ur in upcoming_today:
            if not ur.get("match_id") and ur.get("sync_key") in live_id_by_sync:
                ur["match_id"] = live_id_by_sync[ur["sync_key"]]
    except Exception:
        upcoming_today = []

    # 3. Count summary
    total_live_games = sum(len(c.get("games", [])) for c in compact_live.values())

    return jsonify({
        "ok": True,
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
        "today_date": today_str,
        "poller": poller_status,
        "live_scores": {
            "total_games": total_live_games,
            "competitions": compact_live,
        },
        "upcoming_today": {
            "total_games": len(upcoming_today),
            "games": upcoming_today,
        },
    })


# ── In-Memory Cache for /api/today ────────────────────────────────────
_TODAY_CACHE: dict[str, Any] = {}
_TODAY_CACHE_LOCK = threading.Lock()
_TODAY_CACHE_TTL = 10.0  # seconds, matching live-scores freshness


def _invalidate_today_cache() -> None:
    with _TODAY_CACHE_LOCK:
        _TODAY_CACHE.clear()


def build_today_merged_payload(
    target_date_iso: Optional[str] = None,
    comp_filter: Optional[str] = None,
    compact: bool = False,
    force_refresh: bool = False,
) -> dict:
    """Pre-merge upcoming predictions and live-scores for today (or target_date).

    Behaves with live-scores freshness:
      - Automatically triggers on-demand live poll if memory is empty or force_refresh is requested.
      - Pairs matches using sync_key, match_id, or canonical (home, away) pairs.
      - Includes clear sync_status ('matched', 'upcoming_only', 'live_only') and rich debug info.
    """
    now_et = datetime.now(ZoneInfo("America/New_York"))
    today_et_str = now_et.date().isoformat()
    target_date = str(target_date_iso or today_et_str).strip()
    is_today = (target_date == today_et_str)

    # 1. Refresh live scores if empty or explicitly forced
    if is_today:
        with _live_scores_lock:
            empty = not _live_scores
        if empty or force_refresh:
            try:
                refresh_live_scores_now(force=force_refresh)
            except Exception:
                pass

    from competition_rules import canonical_team_name, make_match_sync_key
    from predictions import _load_upcoming_rows
    from routes.helpers import (
        _ALL_UPCOMING_SOURCES,
        regional_espn_schedule_fallback,
    )
    from routes.predictions import _clean_float, _to_compact_upcoming_row

    # 2. Collect Live Scores & Completed History for target_date
    live_games_for_date = []
    seen_live_ids = set()

    with _live_scores_lock:
        for comp_name, comp_data in _live_scores.items():
            for g in comp_data.get("games", []):
                g_dict = dict(g)
                g_dict.setdefault("competition", comp_name)
                # Attach latest summary if available
                mid = g_dict.get("match_id")
                if mid:
                    with _live_summary_cache_lock:
                        cached = _live_summary_cache.get(mid)
                        if cached:
                            for k, v in cached.items():
                                if v not in (None, "", [], {}):
                                    g_dict[k] = v
                # Determine game date
                from live_poller import _game_et_calendar_date, _stamp_game_sync_fields
                _stamp_game_sync_fields(g_dict, comp_name)
                g_date = _game_et_calendar_date(g_dict)
                g_date_str = g_date.isoformat() if g_date else ""
                if g_date_str == target_date or (is_today and not g_date_str):
                    if mid:
                        seen_live_ids.add(str(mid))
                    live_games_for_date.append(g_dict)

    # Check history (SQLite + JSON fallback) for completed games on target_date
    try:
        from shared import sqlite_store
        hist_rows = sqlite_store.load_live_score_history() or []
        for hg in hist_rows:
            hg_mid = str(hg.get("match_id") or "")
            if hg_mid and hg_mid in seen_live_ids:
                continue
            hg_comp = hg.get("competition", "")
            from live_poller import _game_et_calendar_date, _stamp_game_sync_fields
            hg_dict = dict(hg)
            _stamp_game_sync_fields(hg_dict, hg_comp)
            hg_date = _game_et_calendar_date(hg_dict)
            hg_date_str = hg_date.isoformat() if hg_date else ""
            if hg_date_str == target_date:
                if hg_mid:
                    seen_live_ids.add(hg_mid)
                live_games_for_date.append(hg_dict)
    except Exception:
        pass

    # 3. Collect Upcoming Fixtures for target_date
    upcoming_for_date = []
    feed = []
    seen_upcoming_keys = set()
    try:
        for source, csv_path in _ALL_UPCOMING_SOURCES:
            rows, _, _ = _load_upcoming_rows(csv_path, source, date_range="all")
            for row in rows:
                key = "|".join(
                    str(row.get(f, "")).strip().lower()
                    for f in ("match_date_iso", "competition", "home_team", "away_team")
                )
                if key not in seen_upcoming_keys:
                    seen_upcoming_keys.add(key)
                    feed.append(row)
    except Exception:
        pass

    if not feed:
        try:
            from shared import sqlite_store
            db_rows = sqlite_store.load_upcoming_games()
            if db_rows:
                feed = list(db_rows)
        except Exception:
            pass

    try:
        feed = regional_espn_schedule_fallback(feed)
    except Exception:
        pass

    for r in feed:
        d_iso = str(r.get("match_date_iso") or r.get("match_date") or "").strip()
        if d_iso == target_date:
            upcoming_for_date.append(dict(r))

    # 4. Merge Upcoming and Live games using sync_key and fallbacks
    # Index upcoming by sync_key, match_id, and canonical team pair
    upcoming_by_sync: dict[str, dict] = {}
    upcoming_by_id: dict[str, dict] = {}
    upcoming_by_pair: dict[tuple[str, str], dict] = {}

    for up in upcoming_for_date:
        comp = up.get("competition", "")
        ht = up.get("home_team", "")
        at = up.get("away_team", "")
        c_home = up.get("canonical_home_team") or canonical_team_name(ht, comp) or ht
        c_away = up.get("canonical_away_team") or canonical_team_name(at, comp) or at
        up["canonical_home_team"] = c_home
        up["canonical_away_team"] = c_away
        sk = up.get("sync_key") or make_match_sync_key(
            up.get("match_datetime_et") or up.get("match_date_iso") or target_date,
            comp,
            c_home,
            c_away,
        )
        up["sync_key"] = sk
        if sk and sk not in upcoming_by_sync:
            upcoming_by_sync[sk] = up
        mid = str(up.get("match_id") or "").strip()
        if mid and mid not in upcoming_by_id:
            upcoming_by_id[mid] = up
        pair_key = (str(c_home or "").strip().lower(), str(c_away or "").strip().lower())
        if pair_key not in upcoming_by_pair:
            upcoming_by_pair[pair_key] = up

    matched_upcoming_keys = set()
    merged_items = []

    # First pass: Process all live games and match with upcoming
    for lg in live_games_for_date:
        comp = lg.get("competition", "")
        ht = lg.get("home_team", "")
        at = lg.get("away_team", "")
        c_home = lg.get("canonical_home_team") or canonical_team_name(ht, comp) or ht
        c_away = lg.get("canonical_away_team") or canonical_team_name(at, comp) or at
        lg_mid = str(lg.get("match_id") or "").strip()
        lg_sk = lg.get("sync_key") or make_match_sync_key(
            lg.get("kickoff_et") or lg.get("match_date") or target_date,
            comp,
            c_home,
            c_away,
        )

        matched_up = None
        matched_method = None

        if lg_sk and lg_sk in upcoming_by_sync:
            matched_up = upcoming_by_sync[lg_sk]
            matched_method = "sync_key"
        elif lg_mid and lg_mid in upcoming_by_id:
            matched_up = upcoming_by_id[lg_mid]
            matched_method = "match_id"
        else:
            pair_k = (str(c_home or "").strip().lower(), str(c_away or "").strip().lower())
            if pair_k in upcoming_by_pair:
                matched_up = upcoming_by_pair[pair_k]
                matched_method = "canonical_pair"

        # Build merged match record
        item = {}
        if matched_up:
            up_sk = matched_up.get("sync_key")
            if up_sk:
                matched_upcoming_keys.add(up_sk)
            # Base is upcoming, overlay live scores
            if compact:
                item = _to_compact_upcoming_row(matched_up)
            else:
                item = dict(matched_up)

            # Overlay live fields
            item["match_id"] = lg.get("match_id") or item.get("match_id")
            item["live_status"] = lg.get("status") or item.get("live_status")
            item["status"] = lg.get("status")
            item["status_detail"] = lg.get("status_detail")
            item["live_clock"] = lg.get("clock") or item.get("live_clock")
            item["live_period"] = lg.get("period") or item.get("live_period")
            item["live_updates"] = (lg.get("status") in ("in", "post"))
            item["home_score"] = lg.get("home_score")
            item["away_score"] = lg.get("away_score")
            item["goalscorers"] = lg.get("goalscorers") or item.get("goalscorers") or []
            item["red_cards"] = lg.get("red_cards") or item.get("red_cards") or []
            item["yellow_cards"] = lg.get("yellow_cards") or item.get("yellow_cards") or []
            item["shootout"] = lg.get("shootout")
            if not compact:
                for full_key in ("boxscore_stats", "key_events", "lineups", "game_info", "h2h", "last_five", "odds"):
                    if lg.get(full_key):
                        item[full_key] = lg[full_key]

            item["sync_status"] = "matched"
            item["sources"] = ["upcoming", "live_scores"]
            item["debug"] = {
                "matched_by": matched_method,
                "sync_key": lg_sk,
                "upcoming_sync_key": matched_up.get("sync_key"),
                "live_match_id": lg_mid,
                "upcoming_match_id": matched_up.get("match_id"),
                "live_teams": f"{ht} vs {at}",
                "upcoming_teams": f"{matched_up.get('home_team')} vs {matched_up.get('away_team')}",
                "canonical_teams": f"{c_home} vs {c_away}",
            }
        else:
            # Live only game (no model prediction found)
            if compact:
                from live_poller import to_compact_live_game
                item = to_compact_live_game(lg)
            else:
                item = dict(lg)

            item["sync_status"] = "live_only"
            item["sources"] = ["live_scores"]
            item["sync_key"] = lg_sk
            item["canonical_home_team"] = c_home
            item["canonical_away_team"] = c_away
            item["debug"] = {
                "matched_by": None,
                "sync_key": lg_sk,
                "live_match_id": lg_mid,
                "live_teams": f"{ht} vs {at}",
                "canonical_teams": f"{c_home} vs {c_away}",
            }

        item = decorate_game_team_names(item, item.get("competition", ""))
        merged_items.append(item)

    # Second pass: Include upcoming games that had no live counterpart (upcoming_only)
    for up in upcoming_for_date:
        up_sk = up.get("sync_key")
        if up_sk and up_sk in matched_upcoming_keys:
            continue
        if compact:
            item = _to_compact_upcoming_row(up)
        else:
            item = dict(up)

        ht = up.get("home_team", "")
        at = up.get("away_team", "")
        c_home = up.get("canonical_home_team") or canonical_team_name(ht, up.get("competition", "")) or ht
        c_away = up.get("canonical_away_team") or canonical_team_name(at, up.get("competition", "")) or at

        item["status"] = "pre"
        item["live_status"] = "pre"
        item["sync_status"] = "upcoming_only"
        item["sources"] = ["upcoming"]
        item["debug"] = {
            "matched_by": None,
            "sync_key": up_sk,
            "upcoming_match_id": up.get("match_id"),
            "upcoming_teams": f"{ht} vs {at}",
            "canonical_teams": f"{c_home} vs {c_away}",
        }
        item = decorate_game_team_names(item, item.get("competition", ""))
        merged_items.append(item)

    # Filter by competition if requested
    if comp_filter:
        wanted_comps = {c.strip().lower() for c in comp_filter.split(",") if c.strip()}
        merged_items = [
            it for it in merged_items
            if str(it.get("competition", "")).strip().lower() in wanted_comps
        ]

    # Sort merged matches:
    # 1. In-progress matches first (status == "in" or live_status == "in")
    # 2. Scheduled upcoming matches next (status == "pre" or live_status == "pre") ordered by kickoff
    # 3. Completed matches last (status == "post" or live_status == "post")
    def _sort_key(m: dict):
        status = str(m.get("status") or m.get("live_status") or "").strip().lower()
        if status == "in":
            prio = 0
        elif status == "pre":
            prio = 1
        else:
            prio = 2
        kickoff = str(
            m.get("match_datetime_et")
            or m.get("kickoff_et")
            or m.get("match_datetime_utc")
            or m.get("start_time")
            or ""
        )
        return (prio, kickoff)

    merged_items.sort(key=_sort_key)

    # Group by competition
    by_competition = defaultdict(list)
    for m in merged_items:
        c_name = m.get("competition") or "Other"
        by_competition[c_name].append(m)

    # Summary counts
    total_games = len(merged_items)
    in_progress_count = sum(1 for m in merged_items if str(m.get("status") or m.get("live_status")).strip().lower() == "in")
    completed_count = sum(1 for m in merged_items if str(m.get("status") or m.get("live_status")).strip().lower() == "post")
    scheduled_count = total_games - in_progress_count - completed_count
    matched_count = sum(1 for m in merged_items if m.get("sync_status") == "matched")
    upcoming_only_count = sum(1 for m in merged_items if m.get("sync_status") == "upcoming_only")
    live_only_count = sum(1 for m in merged_items if m.get("sync_status") == "live_only")

    return {
        "ok": True,
        "date": target_date,
        "is_today": is_today,
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_games": total_games,
            "live": in_progress_count,
            "scheduled": scheduled_count,
            "completed": completed_count,
            "matched": matched_count,
            "upcoming_only": upcoming_only_count,
            "live_only": live_only_count,
        },
        "matches": merged_items,
        "by_competition": dict(by_competition),
    }


@live_scores_bp.get("/api/today")
@live_scores_bp.get("/api/today/compact")
def api_today():
    """Unified pre-merged upcoming predictions and live-scores for today.

    Refreshes like /api/live-scores and provides deep synchronization debug metadata.
    Query parameters:
      - date=YYYY-MM-DD: override date (defaults to current date in America/New_York)
      - competition: comma-separated competition names to filter by
      - compact=1: lightweight rows without bulky rosters/boxscores
      - refresh=1: force an immediate on-demand live poll cycle from ESPN
    """
    comp_filter = request.args.get("competition", "").strip()
    force_refresh = request.args.get("refresh", "").strip().lower() in {"1", "true", "yes"}
    compact = (
        request.args.get("compact", "").strip().lower() in {"1", "true", "yes"}
        or request.path.endswith("/compact")
    )
    date_arg = request.args.get("date", "").strip()

    now_et = datetime.now(ZoneInfo("America/New_York"))
    target_date = now_et.date().isoformat()
    if date_arg:
        if date_arg.lower() != "today":
            if not _valid_date_iso(date_arg):
                return jsonify({"ok": False, "error": "Invalid date format (use YYYY-MM-DD)"}), 400
            target_date = date_arg

    # Cache lookup if not force refreshing
    cache_key = f"{target_date}:{comp_filter}:{compact}"
    now_ts = time.time()
    if not force_refresh:
        with _TODAY_CACHE_LOCK:
            entry = _TODAY_CACHE.get(cache_key)
            if entry and (now_ts - entry["ts"]) < _TODAY_CACHE_TTL:
                resp = jsonify(entry["payload"])
                resp.headers["Cache-Control"] = f"public, max-age={int(_TODAY_CACHE_TTL)}"
                return resp

    payload = build_today_merged_payload(
        target_date_iso=target_date,
        comp_filter=comp_filter,
        compact=compact,
        force_refresh=force_refresh,
    )

    with _TODAY_CACHE_LOCK:
        _TODAY_CACHE[cache_key] = {"ts": now_ts, "payload": payload}

    resp = jsonify(payload)
    resp.headers["Cache-Control"] = f"public, max-age={int(_TODAY_CACHE_TTL)}"
    return resp
