"""Push notification and Live Activity delivery via APNs."""
from __future__ import annotations

import json
import logging
import os
import time
import threading
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

try:
    from shared import sqlite_store
except Exception:
    try:
        import sqlite_store  # type: ignore
    except Exception:
        sqlite_store = None


# ── In-memory queues and registrations ────────────────────────────

_apns_notification_queue: deque = deque()
_notifications: list[dict] = []
device_tokens: set[str] = set()
ios_device_tokens: set[str] = set()

# Live Activity storage: key = "{match_id}|{competition}"
_live_activities: dict[str, list[dict]] = {}
_live_activities_lock = threading.Lock()

# Regular push subscription store: key = "{match_id}|{competition}",
# value = set of device tokens that asked to be notified about that match.
_match_notification_subscriptions: dict[str, set[str]] = {}
_match_notification_subscriptions_lock = threading.Lock()

# Team push subscription store: key = normalized_team_name (str),
# value = set of device tokens that asked to be notified about any game for that team.
_team_notification_subscriptions: dict[str, set[str]] = {}
_team_notification_subscriptions_lock = threading.Lock()


def normalize_team_name(team: str) -> str:
    """Canonical team key for subscription lookups (lowercase, stripped)."""
    return str(team or "").strip().lower()


def normalize_live_competition(competition: str) -> str:
    """Canonical competition key shared by the live poller and LA registry."""
    import config

    raw = str(competition or "").strip()
    if not raw:
        return ""
    resolved = config.resolve_live_competition(raw)
    return resolved or raw


def _match_key(match_id: str, competition: str) -> str:
    return f"{match_id}|{normalize_live_competition(competition)}"


def _match_lookup_keys(match_id: str, competition: str) -> list[str]:
    """Return possible registry keys for a match (canonical + aliases)."""
    import config

    mid = str(match_id or "").strip()
    if not mid:
        return []
    keys: list[str] = []
    seen: set[str] = set()
    for alias in config.competition_live_aliases(competition):
        key = f"{mid}|{alias}"
        if key not in seen:
            seen.add(key)
            keys.append(key)
    canonical = _match_key(mid, competition)
    if canonical and canonical not in seen:
        keys.insert(0, canonical)
    return keys


def subscribe_match(device_token: str, match_id: str, competition: str) -> bool:
    """Register a device token to receive regular push alerts for one match."""
    key = _match_key(match_id, competition)
    with _match_notification_subscriptions_lock:
        subs = _match_notification_subscriptions.setdefault(key, set())
        subs.add(device_token)
        ios_device_tokens.add(device_token)
    if sqlite_store is not None:
        try:
            sqlite_store.save_notification_sub(device_token, "match", key, competition=competition)
        except Exception as exc:
            logger.warning(f"[notifications] failed to save match sub to SQLite: {exc}")
    return True


def unsubscribe_match(device_token: str, match_id: str, competition: str) -> bool:
    """Remove a device token from one match's regular-push list."""
    key = _match_key(match_id, competition)
    deleted = False
    with _match_notification_subscriptions_lock:
        subs = _match_notification_subscriptions.get(key)
        if subs and device_token in subs:
            subs.discard(device_token)
            if not subs:
                del _match_notification_subscriptions[key]
            deleted = True
    if sqlite_store is not None:
        try:
            sqlite_store.remove_notification_sub(device_token, "match", key)
        except Exception as exc:
            logger.warning(f"[notifications] failed to remove match sub from SQLite: {exc}")
    return deleted


def for_match_tokens(match_id: str, competition: str) -> list[str]:
    """Device tokens subscribed to regular pushes for a match."""
    tokens: list[str] = []
    seen: set[str] = set()
    with _match_notification_subscriptions_lock:
        for key in _match_lookup_keys(match_id, competition):
            for token in _match_notification_subscriptions.get(key, ()):
                if token not in seen:
                    seen.add(token)
                    tokens.append(token)
    return tokens


def clear_match_subscriptions(match_id: str, competition: str) -> int:
    """Drop all regular-push subscriptions for a finished match."""
    removed = 0
    with _match_notification_subscriptions_lock:
        for key in _match_lookup_keys(match_id, competition):
            subs = _match_notification_subscriptions.pop(key, None)
            if subs:
                removed += len(subs)
    if sqlite_store is not None:
        try:
            sqlite_store.remove_match_notification_subs(match_id, competition=competition)
        except Exception as exc:
            logger.warning(f"[notifications] failed to clear match subs from SQLite: {exc}")
    return removed


def subscribe_team(device_token: str, team: str, competition: str = "") -> bool:
    """Register a device token to receive push alerts for any game involving team."""
    norm = normalize_team_name(team)
    if not norm or not device_token:
        return False
    with _team_notification_subscriptions_lock:
        subs = _team_notification_subscriptions.setdefault(norm, set())
        subs.add(device_token)
        ios_device_tokens.add(device_token)
    if sqlite_store is not None:
        try:
            sqlite_store.save_notification_sub(device_token, "team", norm, competition=competition)
        except Exception as exc:
            logger.warning(f"[notifications] failed to save team sub to SQLite: {exc}")
    return True


def unsubscribe_team(device_token: str, team: str) -> bool:
    """Remove a device token from team notifications."""
    norm = normalize_team_name(team)
    if not norm or not device_token:
        return False
    deleted = False
    with _team_notification_subscriptions_lock:
        subs = _team_notification_subscriptions.get(norm)
        if subs and device_token in subs:
            subs.discard(device_token)
            if not subs:
                del _team_notification_subscriptions[norm]
            deleted = True
    if sqlite_store is not None:
        try:
            sqlite_store.remove_notification_sub(device_token, "team", norm)
        except Exception as exc:
            logger.warning(f"[notifications] failed to remove team sub from SQLite: {exc}")
    return deleted


def for_team_tokens(team: str) -> list[str]:
    """Return all device tokens subscribed to a specific team."""
    norm = normalize_team_name(team)
    if not norm:
        return []
    with _team_notification_subscriptions_lock:
        return list(_team_notification_subscriptions.get(norm, ()))


def for_game_or_team_subscribers(
    match_id: str,
    competition: str,
    home_team: str = "",
    away_team: str = "",
) -> list[str]:
    """Return unified, deduplicated list of tokens subscribed to match OR either team."""
    seen: set[str] = set()
    tokens: list[str] = []
    # 1. Match-level subscribers
    for tok in for_match_tokens(match_id, competition):
        if tok not in seen:
            seen.add(tok)
            tokens.append(tok)
    # 2. Home team subscribers
    if home_team:
        for tok in for_team_tokens(home_team):
            if tok not in seen:
                seen.add(tok)
                tokens.append(tok)
    # 3. Away team subscribers
    if away_team:
        for tok in for_team_tokens(away_team):
            if tok not in seen:
                seen.add(tok)
                tokens.append(tok)
    return tokens


def get_device_subscriptions(token: str) -> dict:
    """Return all matches and teams a device token is subscribed to."""
    t = str(token or "").strip()
    if not t:
        return {"matches": [], "teams": []}
    matches = []
    teams = []
    with _match_notification_subscriptions_lock:
        for k, tokens in _match_notification_subscriptions.items():
            if t in tokens:
                matches.append(k)
    with _team_notification_subscriptions_lock:
        for team_norm, tokens in _team_notification_subscriptions.items():
            if t in tokens:
                teams.append(team_norm)
    return {"matches": sorted(matches), "teams": sorted(teams)}


def record_notification_event(
    title: str,
    body: str,
    match_id: str = "",
    competition: str = "",
    notif_type: str = "live_event",
) -> None:
    """Record an event notification in the in-memory feed for GET /api/notifications."""
    _notifications.append({
        "id": len(_notifications),
        "title": title,
        "body": body,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "type": notif_type,
        "match_id": match_id,
        "competition": normalize_live_competition(competition),
    })
    if len(_notifications) > 500:
        del _notifications[:-500]


def init_subscriptions_from_sqlite() -> dict:
    """Preload active subscriptions from SQLite store into memory."""
    if sqlite_store is None:
        return {"loaded": 0, "matches": 0, "teams": 0}
    try:
        rows = sqlite_store.load_all_notification_subs()
        loaded_matches = 0
        loaded_teams = 0
        with _match_notification_subscriptions_lock, _team_notification_subscriptions_lock:
            for r in rows:
                token = r.get("token")
                sub_type = r.get("sub_type")
                target = r.get("sub_target")
                if not token or not target:
                    continue
                ios_device_tokens.add(token)
                if sub_type == "team":
                    k = normalize_team_name(target)
                    _team_notification_subscriptions.setdefault(k, set()).add(token)
                    loaded_teams += 1
                elif sub_type == "match":
                    comp = r.get("competition") or ""
                    key = target if "|" in target else _match_key(target, comp)
                    _match_notification_subscriptions.setdefault(key, set()).add(token)
                    loaded_matches += 1
        return {"loaded": len(rows), "matches": loaded_matches, "teams": loaded_teams}
    except Exception as exc:
        logger.warning(f"[notifications] failed to preload subscriptions from SQLite: {exc}")
        return {"loaded": 0, "error": str(exc)}



def send_match_notification(match_id: str, competition: str, title: str, body: str) -> int:
    """Queue a regular alert push only to subscribers of ``match_id``."""
    tokens = for_match_tokens(match_id, competition)
    for token in tokens:
        _apns_notification_queue.append({
            "token": token,
            "title": title,
            "body": body,
            "badge": 0,
            "match_id": match_id,
            "competition": normalize_live_competition(competition),
        })
    return len(tokens)


def register(activity_token: str, device_token: str, match_id: str, competition: str) -> bool:
    key = _match_key(match_id, competition)
    with _live_activities_lock:
        existing = _live_activities.setdefault(key, [])
        if any(e["activity_token"] == activity_token for e in existing):
            return False
        existing.append({
            "activity_token": activity_token,
            "device_token": device_token,
            "match_id": str(match_id or "").strip(),
            "competition": normalize_live_competition(competition),
            "registered_at": datetime.now(timezone.utc).isoformat(),
        })
        return True


def unregister(activity_token: str) -> bool:
    removed = False
    with _live_activities_lock:
        for key in list(_live_activities):
            before = len(_live_activities[key])
            _live_activities[key] = [
                e for e in _live_activities[key] if e["activity_token"] != activity_token
            ]
            if len(_live_activities[key]) < before:
                removed = True
            if not _live_activities[key]:
                del _live_activities[key]
    return removed


def for_match(match_id: str, competition: str) -> list[dict]:
    """Return Live Activity registrations for a match across competition aliases."""
    out: list[dict] = []
    seen_tokens: set[str] = set()
    with _live_activities_lock:
        for key in _match_lookup_keys(match_id, competition):
            for entry in _live_activities.get(key, []):
                token = entry.get("activity_token")
                if not token or token in seen_tokens:
                    continue
                seen_tokens.add(token)
                out.append(dict(entry))
    return out


def unregister_by_match(match_id: str, competition: str) -> bool:
    removed = False
    with _live_activities_lock:
        for key in _match_lookup_keys(match_id, competition):
            if _live_activities.pop(key, None) is not None:
                removed = True
    return removed


def all_activities() -> list[dict]:
    with _live_activities_lock:
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
_APNS_SANDBOX = "https://api.development.push.apple.com"


def _apns_send(push_token: str, payload: dict, topic: str, live_activity: bool = False) -> bool:
    jwt_token = _generate_apns_jwt()
    if not jwt_token:
        return False

    import config
    import httpx

    base = _APNS_SANDBOX if config.APNS_USE_SANDBOX else _APNS_PRODUCTION
    # APNs uses the same /3/device/ endpoint for regular alerts and Live
    # Activity updates; the apns-push-type header distinguishes them.
    endpoint = f"{base}/3/device/{push_token}"

    headers = {
        "apns-push-type": "liveactivity" if live_activity else "alert",
        "apns-topic": topic,
        "apns-priority": "10",
        "authorization": f"bearer {jwt_token}",
    }
    if live_activity:
        headers["apns-expiration"] = "0"

    try:
        with httpx.Client(http2=True) as client:
            resp = client.post(endpoint, json=payload, headers=headers, timeout=10)
        if not resp.is_success:
            logger.warning(
                "APNs %s push failed: status=%s body=%s token_prefix=%s topic=%s",
                "live-activity" if live_activity else "alert",
                resp.status_code,
                resp.text[:300],
                push_token[:10],
                topic,
            )
        return resp.is_success
    except Exception as exc:
        logger.warning(
            "APNs %s push error: %s token_prefix=%s",
            "live-activity" if live_activity else "alert",
            exc,
            push_token[:10],
        )
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
            event = entry.get("event", "update")
            aps = {
                "content-state": entry.get("content_state", {}),
                "timestamp": int(time.time()),
                "event": event,
            }
            if event == "end":
                aps["dismissal-date"] = int(entry.get("dismissal_date") or time.time())
            payload = {"aps": aps}
        else:
            topic = config.APNS_TOPIC or ""
            payload = {
                "aps": {
                    "alert": {
                        "title": entry.get("title", ""),
                        "body": entry.get("body", ""),
                    },
                    "sound": "default",
                    "badge": entry.get("badge", 1),
                },
                "match_id": entry.get("match_id", ""),
            }

        ok = _apns_send(push_token, payload, topic, live_activity=is_la)
        if not ok and entry.get("match_id"):
            # A permanent registration failure (bad token) shouldn't poison the
            # queue, but temporary errors are fine to retry next cycle.
            pass


def send_live_activity_update(
    match_id: str,
    competition: str,
    content_state: dict,
    event: str = "update",
) -> int:
    """Queue a Live Activity content-state update for every registration of a match.

    Always includes scores from ``content_state`` so goal pushes refresh the
    Live Activity scoreboard (ActivityKit ``content-state``).
    """
    activities = for_match(match_id, competition)
    if not activities:
        return 0
    state = dict(content_state or {})
    state.setdefault("match_id", match_id)
    state.setdefault("competition", normalize_live_competition(competition))
    for entry in activities:
        _apns_notification_queue.append({
            "type": "liveactivity",
            "token": entry["activity_token"],
            "content_state": state,
            "event": event or "update",
            "match_id": match_id,
            "competition": normalize_live_competition(competition),
        })
    return len(activities)


def send_live_activity_end(match_id: str, competition: str, content_state: dict | None = None) -> int:
    """Queue an 'end' Live Activity push (dismisses the card) for a match."""
    activities = for_match(match_id, competition)
    if not activities:
        unregister_by_match(match_id, competition)
        return 0
    state = dict(content_state or {})
    state.setdefault("status", "FT")
    for entry in activities:
        _apns_notification_queue.append({
            "type": "liveactivity",
            "token": entry["activity_token"],
            "content_state": state,
            "event": "end",
            "match_id": match_id,
            "competition": normalize_live_competition(competition),
        })
    unregister_by_match(match_id, competition)
    return len(activities)

# Preload existing subscriptions from SQLite store on module initialization
try:
    init_subscriptions_from_sqlite()
except Exception:
    pass

