"""Push notification and Live Activity delivery via APNs."""
from __future__ import annotations

import json
import os
import time
import threading
from collections import deque
from datetime import datetime, timezone

# ── In-memory queues and registrations ────────────────────────────

_apns_notification_queue: deque = deque()
_notifications: list[dict] = []
device_tokens: set[str] = set()
ios_device_tokens: set[str] = set()

# Live Activity storage: key = "{match_id}|{competition}"
_live_activities: dict[str, list[dict]] = {}


def register(activity_token: str, device_token: str, match_id: str, competition: str) -> bool:
    key = f"{match_id}|{competition}"
    existing = _live_activities.setdefault(key, [])
    if any(e["activity_token"] == activity_token for e in existing):
        return False
    existing.append({
        "activity_token": activity_token,
        "device_token": device_token,
        "registered_at": datetime.now(timezone.utc).isoformat(),
    })
    return True


def unregister(activity_token: str) -> bool:
    for key in list(_live_activities):
        _live_activities[key] = [e for e in _live_activities[key] if e["activity_token"] != activity_token]
        if not _live_activities[key]:
            del _live_activities[key]
            return True
    return False


def for_match(match_id: str, competition: str) -> list[dict]:
    return list(_live_activities.get(f"{match_id}|{competition}", []))


def unregister_by_match(match_id: str, competition: str) -> bool:
    return _live_activities.pop(f"{match_id}|{competition}", None) is not None


def all_activities() -> list[dict]:
    return [
        {**entry, "key": key}
        for key, entries in _live_activities.items()
        for entry in entries
    ]


# ── APNs JWT generation (ES256 with .p8 key) ─────────────────────

_apns_jwt_cache: str | None = None
_apns_jwt_expiry: float = 0


def _generate_apns_jwt() -> str | None:
    global _apns_jwt_cache, _apns_jwt_expiry
    now = time.time()
    if _apns_jwt_cache and now < _apns_jwt_expiry - 60:
        return _apns_jwt_cache

    import config
    import jwt as pyjwt

    kid = config.APNS_KEY_ID or ""
    iss = config.APNS_TEAM_ID or ""
    key_path = config.APNS_AUTH_KEY_PATH or ""

    if not kid or not iss or not key_path or not os.path.exists(key_path):
        return None

    with open(key_path, "r") as f:
        private_key = f.read()

    token = pyjwt.encode(
        {"iss": iss, "iat": int(now), "exp": int(now) + 3600},
        private_key,
        algorithm="ES256",
        headers={"alg": "ES256", "kid": kid},
    )
    _apns_jwt_cache = token
    _apns_jwt_expiry = int(now) + 3600
    return token


# ── APNs HTTP/2 request ──────────────────────────────────────────

_APNS_PRODUCTION = "https://api.push.apple.com"
_APNS_SANDBOX = "https://api.sandbox.push.apple.com"


def _apns_send(push_token: str, payload: dict, topic: str, live_activity: bool = False) -> bool:
    jwt_token = _generate_apns_jwt()
    if not jwt_token:
        return False

    import config
    import httpx

    base = _APNS_SANDBOX if config.APNS_USE_SANDBOX else _APNS_PRODUCTION
    endpoint = f"{base}/3/activity/{push_token}" if live_activity else f"{base}/3/device/{push_token}"

    headers = {
        "apns-push-type": "live-activity" if live_activity else "alert",
        "apns-topic": topic,
        "apns-priority": "10",
        "authorization": f"bearer {jwt_token}",
    }

    try:
        with httpx.Client(http2=True) as client:
            resp = client.post(endpoint, json=payload, headers=headers, timeout=10)
            return resp.is_success
    except Exception:
        return False


# ── Queue worker ─────────────────────────────────────────────────

def start_apns_worker() -> None:
    threading.Thread(target=_worker_loop, daemon=True).start()


def _worker_loop() -> None:
    while True:
        try:
            _drain_queue()
        except Exception:
            pass
        time.sleep(5)


def _drain_queue() -> None:
    import config

    while _apns_notification_queue:
        entry = _apns_notification_queue.popleft()
        push_token = entry.get("token", "")
        if not push_token:
            continue

        is_la = entry.get("type") == "liveactivity"

        if is_la:
            topic = config.APNS_LIVE_ACTIVITY_TOPIC or ""
            payload = {
                "aps": {
                    "content-state": entry.get("content_state", {}),
                    "timestamp": int(time.time()),
                    "event": entry.get("event", "update"),
                }
            }
        else:
            topic = config.APNS_TOPIC or ""
            payload = {
                "aps": {
                    "alert": {
                        "title": entry.get("title", ""),
                        "body": entry.get("body", ""),
                    },
                    "badge": entry.get("badge", 0),
                    "sound": "default",
                }
            }

        _apns_send(push_token, payload, topic, live_activity=is_la)


def send_live_activity_update(match_id: str, competition: str, content_state: dict) -> None:
    for entry in for_match(match_id, competition):
        _apns_notification_queue.append({
            "type": "liveactivity",
            "token": entry["activity_token"],
            "content_state": content_state,
            "event": "update",
        })


def send_live_activity_end(match_id: str, competition: str, content_state: dict | None = None) -> None:
    for entry in for_match(match_id, competition):
        _apns_notification_queue.append({
            "type": "liveactivity",
            "token": entry["activity_token"],
            "content_state": content_state or {},
            "event": "end",
        })
    unregister_by_match(match_id, competition)
