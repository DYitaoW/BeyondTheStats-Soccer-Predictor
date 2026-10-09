"""
Live scores, APNs push notifications, and Live Activities API routes.
"""

from __future__ import annotations

import os
import queue
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from starlette.responses import StreamingResponse
from .compat import Blueprint, Response, jsonify, request
import pandas as pd

import config
from live_poller import (
    _effective_poller_date,
    _get_todays_competitions,
    _live_score_poller_loop,
    _live_scores,
    _live_scores_lock,
    get_cached_compact_live_scores_payload,
    get_cached_live_scores_payload,
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

    with _live_scores_lock:
        empty = not _live_scores
    if empty or force_refresh:
        try:
            refresh_live_scores_now(force=force_refresh)
        except Exception:
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
        filtered = {k: v for k, v in _live_scores.items() if k in wanted}
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
            # Send initial snapshot immediately
            raw_bytes, _ = (
                get_cached_compact_live_scores_payload()
                if compact
                else get_cached_live_scores_payload()
            )
            if raw_bytes:
                yield f"data: {raw_bytes.decode('utf-8')}\n\n"
            while True:
                try:
                    update_bytes = q.get(timeout=25.0)
                    yield f"data: {update_bytes.decode('utf-8')}\n\n"
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


@live_scores_bp.get("/api/live-score-history")
@live_scores_bp.get("/api/past-live-scores")
def api_live_score_history():
    """Return historical completed games, grouped by competition."""
    compact = request.args.get("compact", "").strip().lower() in {"1", "true", "yes"}
    league = request.args.get("league", "").strip()
    from_date = request.args.get("from", "").strip()
    to_date = request.args.get("to", "").strip()
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
        from shared import sqlite_store as _sqlite_store

        _sqlite_store.ensure_store()
        if _sqlite_store.count_rows("live_score_history") > 0:
            games = _sqlite_store.load_live_score_history(
                competition_substr=league,
                from_date=from_date,
                to_date=to_date,
                limit=limit,
                offset=offset,
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
        if offset:
            games = games[offset:]
        if limit:
            games = games[:limit]

    competitions = {}
    for g in games:
        comp = g.get("competition", "Unknown")
        if comp not in competitions:
            competitions[comp] = {
                "competition": comp,
                "games": [],
                "last_polled_utc": datetime.now(timezone.utc).isoformat(),
            }
        competitions[comp]["games"].append(to_compact_live_game(g) if compact else g)

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
    token = str(payload.get("token", "")).strip()
    if not token:
        return jsonify({"ok": False, "error": "token required"}), 400
    if len(token) > 512:
        return jsonify({"ok": False, "error": "token too long"}), 400
    platform = str(payload.get("platform", "generic")).strip().lower()
    if platform == "ios":
        ios_device_tokens.add(token)
    else:
        device_tokens.add(token)
    return jsonify({"ok": True, "registered": True})


@live_scores_bp.post("/api/notifications/unregister")
def api_unregister_device():
    """Remove a device token."""
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "")).strip()
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
    token = str(payload.get("token", "")).strip()
    match_id = str(payload.get("match_id", "")).strip()
    team = str(payload.get("team", "") or payload.get("team_name", "")).strip()
    competition = str(payload.get("competition", "")).strip()
    events = payload.get("events")

    if not token:
        return jsonify({"ok": False, "error": "token required"}), 400
    if not match_id and not team:
        return jsonify({"ok": False, "error": "either match_id (with competition) or team required"}), 400
    if match_id and not competition:
        return jsonify({"ok": False, "error": "competition required when subscribing by match_id"}), 400
    if len(token) > 512 or len(match_id) > 256 or len(competition) > 256 or len(team) > 256:
        return jsonify({"ok": False, "error": "input too long"}), 400

    norm_events = normalize_events(events)
    subscribed_targets = []

    if match_id:
        ok_m = subscribe_match(token, match_id, competition, events=norm_events)
        if ok_m:
            subscribed_targets.append(f"match:{match_id}")

    if team:
        ok_t = subscribe_team(token, team, competition=competition, events=norm_events)
        if ok_t:
            subscribed_targets.append(f"team:{team}")

    return jsonify({
        "ok": True,
        "subscribed": bool(subscribed_targets),
        "targets": subscribed_targets,
        "events": sorted(list(norm_events)),
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
    token = str(payload.get("token", "")).strip()
    team = str(payload.get("team", "") or payload.get("team_name", "")).strip()
    competition = str(payload.get("competition", "")).strip()
    events = payload.get("events")

    if not token or not team:
        return jsonify({"ok": False, "error": "token and team required"}), 400
    if len(token) > 512 or len(team) > 256 or len(competition) > 256:
        return jsonify({"ok": False, "error": "input too long"}), 400

    norm_events = normalize_events(events)
    ok = subscribe_team(token, team, competition=competition, events=norm_events)
    return jsonify({
        "ok": True,
        "subscribed": ok,
        "team": team,
        "events": sorted(list(norm_events)),
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
    activity_token = str(payload.get("activity_token", "")).strip()
    match_id = str(payload.get("match_id", "")).strip()
    competition = str(payload.get("competition", "")).strip()
    events = payload.get("events")

    if not activity_token or not match_id or not competition:
        return jsonify({"ok": False, "error": "activity_token, match_id, and competition required"}), 400
    if len(activity_token) > 1024 or len(match_id) > 256 or len(competition) > 256:
        return jsonify({"ok": False, "error": "input too long"}), 400
    device_token = str(payload.get("device_token", "")).strip()
    if len(device_token) > 512:
        return jsonify({"ok": False, "error": "device_token too long"}), 400

    ok = live_activities.register(
        activity_token, device_token, match_id, competition, events=events
    )
    if ok and device_token:
        subscribe_match(device_token, match_id, competition, events=events)
    return jsonify({"ok": True, "registered": ok, "total": len(live_activities.all_activities())})


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
