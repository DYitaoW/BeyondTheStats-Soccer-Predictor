"""
Interactive testing and diagnostic routes for APNs push notifications and Live Activities.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from .compat import Blueprint, jsonify, request

import config
from live_poller import (
    get_simulated_live_game,
    inject_simulated_live_game,
    remove_simulated_live_game,
    update_simulated_live_game,
)
import notifications
from notifications import (
    ALL_EVENT_TYPES,
    _apns_notification_queue,
    apns_send_detailed,
    clear_match_subscriptions,
    for_game_or_team_subscribers,
    get_apns_diagnostic_status,
    ios_device_tokens,
    send_live_activity_end,
    send_live_activity_update,
    send_match_notification,
    subscribe_match,
    subscribe_team,
)

testing_bp = Blueprint("testing", __name__)
testing_router = testing_bp


@testing_bp.get("/api/test/env")
def api_test_env():
    """Inspect all backend environment variables with sensitive tokens masked."""
    def _mask(val: str, show_chars: int = 3) -> str | None:
        if not val:
            return None
        if len(val) <= show_chars * 2:
            return "***"
        return f"{val[:show_chars]}***{val[-show_chars:]}"

    key_path = getattr(config, "APNS_AUTH_KEY_PATH", "") or ""
    env_report = {
        "ok": True,
        "apns": {
            "APNS_KEY_ID": _mask(getattr(config, "APNS_KEY_ID", "")),
            "APNS_TEAM_ID": _mask(getattr(config, "APNS_TEAM_ID", "")),
            "APNS_AUTH_KEY_PATH": key_path or None,
            "APNS_AUTH_KEY_EXISTS": os.path.exists(key_path) if key_path else False,
            "APNS_TOPIC": getattr(config, "APNS_TOPIC", None),
            "APNS_LIVE_ACTIVITY_TOPIC": getattr(config, "APNS_LIVE_ACTIVITY_TOPIC", None),
            "APNS_USE_SANDBOX": getattr(config, "APNS_USE_SANDBOX", False),
        },
        "pipeline": {
            "PIPELINE_RUN_ON_START": os.environ.get("PIPELINE_RUN_ON_START", "0"),
            "PIPELINE_ENABLED": os.environ.get("PIPELINE_ENABLED", "1"),
            "PIPELINE_FULL_RETRAIN_ON_START": os.environ.get("PIPELINE_FULL_RETRAIN_ON_START", "0"),
        },
        "server": {
            "MUTATION_API_TOKEN": "set" if getattr(config, "MUTATION_API_TOKEN", "") else "unset",
            "ALLOWED_ORIGINS": getattr(config, "ALLOWED_ORIGINS", []),
            "GZIP_MINIMUM_SIZE": getattr(config, "GZIP_MINIMUM_SIZE", 1000),
            "PORT": os.environ.get("PORT", "5000"),
            "HOST": os.environ.get("HOST", "0.0.0.0"),
        },
    }
    return jsonify(env_report)


@testing_bp.get("/api/test/notifications/status")
def api_test_notifications_status():
    """Return comprehensive diagnostics on APNs credentials, keys, topics, and subscribers."""
    status = get_apns_diagnostic_status()
    return jsonify(status)


@testing_bp.get("/api/test/notifications/logs")
def api_test_notifications_logs():
    """Return recent APNs dispatch history with Apple response reasons and headers."""
    status = get_apns_diagnostic_status()
    return jsonify({
        "ok": True,
        "recent_dispatches": status.get("recent_dispatches", []),
        "in_memory_state": status.get("in_memory_state", {}),
    })


@testing_bp.post("/api/test/notifications/push")
def api_test_notifications_push():
    """Directly send a real APNs test push alert to a device token with instant Apple feedback."""
    payload = request.get_json(silent=True) or {}
    device_token = str(payload.get("device_token", "")).strip().replace(" ", "")
    title = str(payload.get("title", "Beyond the Stats Test Alert")).strip()
    body = str(payload.get("body", "Test notification sent from Beyond the Stats backend.")).strip()
    sound = str(payload.get("sound", "default")).strip()
    badge = int(payload.get("badge", 1))
    sandbox_override = payload.get("sandbox")
    topic = payload.get("topic")

    targets = [device_token] if device_token else list(ios_device_tokens)
    if not targets:
        return jsonify({
            "ok": False,
            "error": "No device_token provided in request, and no device tokens are registered in memory.",
            "hint": "Pass 'device_token' in JSON body, or have your iOS app subscribe via /api/notifications/subscribe first.",
        }), 400

    apns_payload = {
        "aps": {
            "alert": {
                "title": title,
                "body": body,
            },
            "sound": sound,
            "badge": badge,
        },
        "test": True,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }

    results = []
    success_count = 0
    for tok in targets:
        res = notifications.apns_send_detailed(
            push_token=tok,
            payload=apns_payload,
            topic=topic,
            live_activity=False,
            sandbox=sandbox_override,
        )
        if res.get("ok"):
            success_count += 1
        results.append(res)

    all_ok = success_count == len(targets)
    return jsonify({
        "ok": all_ok,
        "total_targets": len(targets),
        "successful_count": success_count,
        "results": results,
    })


@testing_bp.post("/api/test/live-activities/push")
def api_test_live_activities_push():
    """Directly send a real Live Activity update or end push to an activity_token."""
    payload = request.get_json(silent=True) or {}
    activity_token = str(payload.get("activity_token", "")).strip().replace(" ", "")
    if not activity_token:
        return jsonify({
            "ok": False,
            "error": "activity_token required in request body",
            "hint": "Obtain activity.pushToken from ActivityKit on your iOS device and pass it here.",
        }), 400

    event = str(payload.get("event", "update")).strip().lower()
    sandbox_override = payload.get("sandbox")
    topic = payload.get("topic")

    content_state = payload.get("content_state")
    if not isinstance(content_state, dict):
        content_state = {
            "home_team": str(payload.get("home_team", "Arsenal")),
            "away_team": str(payload.get("away_team", "Chelsea")),
            "home_score": int(payload.get("home_score", 1)),
            "away_score": int(payload.get("away_score", 0)),
            "clock": str(payload.get("clock", "25'")),
            "status": str(payload.get("status", "in")),
            "match_id": str(payload.get("match_id", "sim-999")),
            "competition": str(payload.get("competition", "Test/Simulation")),
            "event": event,
        }

    alert = payload.get("alert")
    if alert is None and payload.get("alert_title"):
        alert = {
            "title": str(payload.get("alert_title", "")),
            "body": str(payload.get("alert_body", "")),
        }

    aps: dict = {
        "content-state": content_state,
        "timestamp": int(time.time()),
        "event": event,
    }
    if alert:
        aps["alert"] = alert
        aps["sound"] = payload.get("sound", "default")
    if event == "end":
        aps["dismissal-date"] = int(time.time() + 60)

    apns_payload = {"aps": aps}

    res = apns_send_detailed(
        push_token=activity_token,
        payload=apns_payload,
        topic=topic,
        live_activity=True,
        sandbox=sandbox_override,
    )
    return jsonify({
        "ok": res.get("ok", False),
        "event": event,
        "apns_result": res,
    })


@testing_bp.post("/api/test/simulate-game/start")
def api_test_simulate_game_start():
    """Inject a fake live match into memory so the iOS app can view it, subscribe, or run Live Activities."""
    payload = request.get_json(silent=True) or {}
    match_id = str(payload.get("match_id", "sim-999")).strip()
    comp_name = str(payload.get("competition", "Test/Simulation")).strip()
    home_team = str(payload.get("home_team", "Arsenal")).strip()
    away_team = str(payload.get("away_team", "Chelsea")).strip()
    home_score = int(payload.get("home_score", 0))
    away_score = int(payload.get("away_score", 0))
    clock = str(payload.get("clock", "0'")).strip()
    status = str(payload.get("status", "in")).strip()

    fake_game = {
        "match_id": match_id,
        "competition": comp_name,
        "home_team": home_team,
        "away_team": away_team,
        "home_score": home_score,
        "away_score": away_score,
        "clock": clock,
        "status": status,
        "period": 1,
        "kickoff_utc": datetime.now(timezone.utc).isoformat(),
        "goalscorers": [],
        "red_cards": [],
        "yellow_cards": [],
        "live_prediction": {
            "prob_home": 54.0,
            "prob_draw": 26.0,
            "prob_away": 20.0,
        },
    }

    injected = inject_simulated_live_game(fake_game, comp_name=comp_name)

    # Auto-subscribe requesting device if passed
    device_token = str(payload.get("device_token", "")).strip()
    if device_token:
        subscribe_match(device_token, match_id, comp_name, events=ALL_EVENT_TYPES)

    return jsonify({
        "ok": True,
        "message": f"Simulated game '{home_team} vs {away_team}' (match_id: {match_id}) is now live in memory.",
        "game": injected,
        "endpoints_to_verify": {
            "live_scores": "/api/live-scores",
            "startup": "/api/startup",
            "match_detail": f"/api/match/{match_id}",
        },
    })


@testing_bp.post("/api/test/simulate-game/event")
def api_test_simulate_game_event():
    """Trigger an in-game event (kickoff, goal, halftime, red card, full time) for the simulated game.

    Updates memory, pushes notifications to all subscribers, and dispatches Live Activity alerts.
    """
    payload = request.get_json(silent=True) or {}
    match_id = str(payload.get("match_id", "sim-999")).strip()
    comp_name = str(payload.get("competition", "Test/Simulation")).strip()
    event = str(payload.get("event", "goal_home")).strip().lower()
    player = str(payload.get("player", "")).strip()
    minute = str(payload.get("minute", "")).strip()

    game = get_simulated_live_game(match_id, comp_name=comp_name)
    if not game:
        return jsonify({
            "ok": False,
            "error": f"Simulated match '{match_id}' not found. Start it first via POST /api/test/simulate-game/start",
        }), 404

    ht = game.get("home_team", "Arsenal")
    at = game.get("away_team", "Chelsea")
    hs = int(game.get("home_score", 0))
    as_ = int(game.get("away_score", 0))
    scorers = list(game.get("goalscorers", []))
    red_cards = list(game.get("red_cards", []))

    updates: dict = {}
    notif_title = ""
    notif_body = ""
    canonical_event = "alert"
    la_alert = None
    is_ft = False

    if event in ("kickoff", "start"):
        canonical_event = "start"
        updates["status"] = "in"
        updates["clock"] = "1'"
        updates["period"] = 1
        notif_title = "Match Started"
        notif_body = f"{ht} vs {at} — Kickoff!"

    elif event in ("goal_home", "home_goal"):
        canonical_event = "goal"
        hs += 1
        minute = minute or f"{max(hs * 22, 10)}'"
        scorer_name = player or "B. Saka"
        scorers.append({"team": "home", "scorer": scorer_name, "minute": minute, "type": "Goal"})
        updates["home_score"] = hs
        updates["clock"] = minute
        updates["goalscorers"] = scorers
        notif_title = f"GOAL! {ht}"
        notif_body = f"{ht} {hs} - {as_} {at} ({minute} {scorer_name})"
        la_alert = {"title": notif_title, "body": notif_body}

    elif event in ("goal_away", "away_goal"):
        canonical_event = "goal"
        as_ += 1
        minute = minute or f"{max(as_ * 33, 18)}'"
        scorer_name = player or "C. Palmer"
        scorers.append({"team": "away", "scorer": scorer_name, "minute": minute, "type": "Goal"})
        updates["away_score"] = as_
        updates["clock"] = minute
        updates["goalscorers"] = scorers
        notif_title = f"GOAL! {at}"
        notif_body = f"{ht} {hs} - {as_} {at} ({minute} {scorer_name})"
        la_alert = {"title": notif_title, "body": notif_body}

    elif event in ("ht", "halftime", "half_time"):
        canonical_event = "ht"
        updates["status"] = "ht"
        updates["clock"] = "HT"
        updates["period"] = 2
        notif_title = "Half Time"
        notif_body = f"{ht} {hs} - {as_} {at} — Half Time"
        la_alert = {"title": notif_title, "body": notif_body}

    elif event in ("red_card", "red_card_home", "red_card_away"):
        canonical_event = "red_card"
        team_side = "away" if "away" in event else "home"
        carded_team = at if team_side == "away" else ht
        carded_player = player or ("M. Caicedo" if team_side == "away" else "D. Rice")
        minute = minute or "58'"
        red_cards.append({"team": team_side, "player": carded_player, "minute": minute})
        updates["red_cards"] = red_cards
        updates["clock"] = minute
        notif_title = f"RED CARD ({carded_team})"
        notif_body = f"{carded_player} sent off! ({minute}) {ht} {hs} - {as_} {at}"
        la_alert = {"title": notif_title, "body": notif_body}

    elif event in ("ft", "fulltime", "full_time", "end"):
        canonical_event = "end"
        is_ft = True
        updates["status"] = "post"
        updates["clock"] = "FT"
        notif_title = "Full Time"
        notif_body = f"{ht} {hs} - {as_} {at} — Final Result"
        la_alert = {"title": notif_title, "body": notif_body}

    else:
        return jsonify({
            "ok": False,
            "error": f"Unknown event '{event}'. Choose from: kickoff, goal_home, goal_away, ht, red_card_home, red_card_away, ft",
        }), 400

    updated_game = update_simulated_live_game(match_id, updates, comp_name=comp_name)

    # 1. Dispatch regular push notifications to match & team subscribers
    subscribers = for_game_or_team_subscribers(
        match_id, comp_name, home_team=ht, away_team=at, event=canonical_event
    )
    regular_pushes_queued = send_match_notification(
        match_id, comp_name, notif_title, notif_body, event=canonical_event
    )

    # 2. Dispatch Live Activity updates (with alert banner)
    la_state = {
        "home_team": ht,
        "away_team": at,
        "home_score": hs,
        "away_score": as_,
        "clock": updates.get("clock", game.get("clock", "")),
        "status": updates.get("status", game.get("status", "in")),
        "match_id": match_id,
        "competition": comp_name,
        "event": canonical_event,
    }

    if is_ft:
        la_sent = send_live_activity_end(match_id, comp_name, content_state=la_state, alert=la_alert)
    else:
        la_sent = send_live_activity_update(
            match_id, comp_name, content_state=la_state, event=canonical_event, alert=la_alert
        )

    return jsonify({
        "ok": True,
        "event_triggered": event,
        "canonical_event": canonical_event,
        "notification": {
            "title": notif_title,
            "body": notif_body,
            "subscribers_notified_count": regular_pushes_queued,
            "subscriber_tokens": subscribers,
        },
        "live_activity": {
            "sent_to_activities_count": la_sent,
            "content_state": la_state,
            "ended": is_ft,
        },
        "game_current_state": updated_game,
    })


@testing_bp.post("/api/test/simulate-game/reset")
def api_test_simulate_game_reset():
    """Dismiss any active Live Activities for the fake game and remove it from memory."""
    payload = request.get_json(silent=True) or {}
    match_id = str(payload.get("match_id", "sim-999")).strip()
    comp_name = str(payload.get("competition", "Test/Simulation")).strip()

    # Dismiss Live Activities
    send_live_activity_end(match_id, comp_name, content_state={"status": "FT"})
    clear_match_subscriptions(match_id, comp_name)
    removed = remove_simulated_live_game(match_id, comp_name=comp_name)

    return jsonify({
        "ok": True,
        "message": f"Simulated game '{match_id}' removed and Live Activities dismissed.",
        "removed": removed,
    })

