"""Push notification and Live Activity delivery via APNs."""
from __future__ import annotations

import json
import logging
import os
import time
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

try:
    from shared import sqlite_store
except Exception:
    try:
        import sqlite_store  # type: ignore
    except Exception:
        sqlite_store = None


# ── Canonical Event Types & Normalization ─────────────────────────

ALL_EVENT_TYPES: frozenset[str] = frozenset({"goal", "start", "end", "ht", "red_card"})


def normalize_event_type(event: str) -> str:
    """Normalize event name to canonical event type (goal, start, end, ht, red_card)."""
    ev = str(event or "").strip().lower().replace("-", "_").replace(" ", "_")
    if ev in {"goal", "goals", "scoring", "score", "penalty", "pen", "own_goal"}:
        return "goal"
    if ev in {"start", "kickoff", "match_start", "game_start", "first_half", "1h"}:
        return "start"
    if ev in {"end", "ft", "fulltime", "full_time", "match_end", "game_end", "finished"}:
        return "end"
    if ev in {"ht", "halftime", "half_time", "half"}:
        return "ht"
    if ev in {"red_card", "red_cards", "red", "card", "redcard"}:
        return "red_card"
    return ev


def normalize_events(events: Any) -> set[str]:
    """Normalize input event list or comma-separated string to canonical event types.

    If empty or None, defaults to ALL_EVENT_TYPES.
    """
    if not events:
        return set(ALL_EVENT_TYPES)
    if isinstance(events, str):
        parts = [p.strip() for p in events.split(",") if p.strip()]
    elif isinstance(events, (list, tuple, set, frozenset)):
        parts = [str(p).strip() for p in events if str(p).strip()]
    else:
        return set(ALL_EVENT_TYPES)
    normalized = set()
    for p in parts:
        norm = normalize_event_type(p)
        if norm in ALL_EVENT_TYPES:
            normalized.add(norm)
    return normalized or set(ALL_EVENT_TYPES)


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
_match_subscription_events: dict[str, dict[str, set[str]]] = {}
_match_notification_subscriptions_lock = threading.Lock()

# Team push subscription store: key = normalized_team_name (str),
# value = set of device tokens that asked to be notified about any game for that team.
_team_notification_subscriptions: dict[str, set[str]] = {}
_team_subscription_events: dict[str, dict[str, set[str]]] = {}
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


def subscribe_match(
    device_token: str,
    match_id: str,
    competition: str,
    events: Optional[Iterable[str] | str] = None,
) -> bool:
    """Register a device token to receive regular push alerts for one match with event preferences."""
    key = _match_key(match_id, competition)
    norm_events = normalize_events(events)
    with _match_notification_subscriptions_lock:
        subs = _match_notification_subscriptions.setdefault(key, set())
        subs.add(device_token)
        _match_subscription_events.setdefault(key, {})[device_token] = norm_events
        ios_device_tokens.add(device_token)
    if sqlite_store is not None:
        try:
            sqlite_store.save_notification_sub(
                device_token, "match", key, competition=competition, events=norm_events
            )
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
        ev_map = _match_subscription_events.get(key)
        if ev_map and device_token in ev_map:
            del ev_map[device_token]
            if not ev_map:
                _match_subscription_events.pop(key, None)
    if sqlite_store is not None:
        try:
            sqlite_store.remove_notification_sub(device_token, "match", key)
        except Exception as exc:
            logger.warning(f"[notifications] failed to remove match sub from SQLite: {exc}")
    return deleted


def for_match_tokens(match_id: str, competition: str, event: Optional[str] = None) -> list[str]:
    """Device tokens subscribed to regular pushes for a match (optionally filtered by event)."""
    norm_event = normalize_event_type(event) if event else None
    tokens: list[str] = []
    seen: set[str] = set()
    with _match_notification_subscriptions_lock:
        for key in _match_lookup_keys(match_id, competition):
            ev_map = _match_subscription_events.get(key, {})
            for token in _match_notification_subscriptions.get(key, ()):
                if token in seen:
                    continue
                if norm_event:
                    token_events = ev_map.get(token, ALL_EVENT_TYPES)
                    if norm_event not in token_events:
                        continue
                seen.add(token)
                tokens.append(token)
    return tokens


def clear_match_subscriptions(match_id: str, competition: str) -> int:
    """Drop all regular-push subscriptions for a finished match."""
    removed = 0
    with _match_notification_subscriptions_lock:
        for key in _match_lookup_keys(match_id, competition):
            subs = _match_notification_subscriptions.pop(key, None)
            _match_subscription_events.pop(key, None)
            if subs:
                removed += len(subs)
    if sqlite_store is not None:
        try:
            sqlite_store.remove_match_notification_subs(match_id, competition=competition)
        except Exception as exc:
            logger.warning(f"[notifications] failed to clear match subs from SQLite: {exc}")
    return removed


def subscribe_team(
    device_token: str,
    team: str,
    competition: str = "",
    events: Optional[Iterable[str] | str] = None,
) -> bool:
    """Register a device token to receive push alerts for any game involving team with event preferences."""
    norm = normalize_team_name(team)
    if not norm or not device_token:
        return False
    norm_events = normalize_events(events)
    with _team_notification_subscriptions_lock:
        subs = _team_notification_subscriptions.setdefault(norm, set())
        subs.add(device_token)
        _team_subscription_events.setdefault(norm, {})[device_token] = norm_events
        ios_device_tokens.add(device_token)
    if sqlite_store is not None:
        try:
            sqlite_store.save_notification_sub(
                device_token, "team", norm, competition=competition, events=norm_events
            )
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
        ev_map = _team_subscription_events.get(norm)
        if ev_map and device_token in ev_map:
            del ev_map[device_token]
            if not ev_map:
                _team_subscription_events.pop(norm, None)
    if sqlite_store is not None:
        try:
            sqlite_store.remove_notification_sub(device_token, "team", norm)
        except Exception as exc:
            logger.warning(f"[notifications] failed to remove team sub from SQLite: {exc}")
    return deleted


def for_team_tokens(team: str, event: Optional[str] = None) -> list[str]:
    """Return all device tokens subscribed to a specific team (optionally filtered by event)."""
    norm = normalize_team_name(team)
    if not norm:
        return []
    norm_event = normalize_event_type(event) if event else None
    tokens: list[str] = []
    with _team_notification_subscriptions_lock:
        ev_map = _team_subscription_events.get(norm, {})
        for token in _team_notification_subscriptions.get(norm, ()):
            if norm_event:
                token_events = ev_map.get(token, ALL_EVENT_TYPES)
                if norm_event not in token_events:
                    continue
            tokens.append(token)
    return tokens


def for_game_or_team_subscribers(
    match_id: str,
    competition: str,
    home_team: str = "",
    away_team: str = "",
    event: Optional[str] = None,
) -> list[str]:
    """Return unified, deduplicated list of tokens subscribed to match OR either team (filtered by event)."""
    seen: set[str] = set()
    tokens: list[str] = []
    # 1. Match-level subscribers
    for tok in for_match_tokens(match_id, competition, event=event):
        if tok not in seen:
            seen.add(tok)
            tokens.append(tok)
    # 2. Home team subscribers
    if home_team:
        for tok in for_team_tokens(home_team, event=event):
            if tok not in seen:
                seen.add(tok)
                tokens.append(tok)
    # 3. Away team subscribers
    if away_team:
        for tok in for_team_tokens(away_team, event=event):
            if tok not in seen:
                seen.add(tok)
                tokens.append(tok)
    return tokens


def get_device_subscriptions(token: str) -> dict:
    """Return all matches, teams, and subscribed events for a device token."""
    t = str(token or "").strip()
    if not t:
        return {"matches": [], "teams": [], "match_events": {}, "team_events": {}}
    matches = []
    teams = []
    match_events = {}
    team_events = {}
    with _match_notification_subscriptions_lock:
        for k, tokens in _match_notification_subscriptions.items():
            if t in tokens:
                matches.append(k)
                evs = _match_subscription_events.get(k, {}).get(t, ALL_EVENT_TYPES)
                match_events[k] = sorted(list(evs))
    with _team_notification_subscriptions_lock:
        for team_norm, tokens in _team_notification_subscriptions.items():
            if t in tokens:
                teams.append(team_norm)
                evs = _team_subscription_events.get(team_norm, {}).get(t, ALL_EVENT_TYPES)
                team_events[team_norm] = sorted(list(evs))
    return {
        "matches": sorted(matches),
        "teams": sorted(teams),
        "match_events": match_events,
        "team_events": team_events,
    }


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
    """Preload active subscriptions and event preferences from SQLite store into memory."""
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
                ev_pref = normalize_events(r.get("events"))
                if sub_type == "team":
                    k = normalize_team_name(target)
                    _team_notification_subscriptions.setdefault(k, set()).add(token)
                    _team_subscription_events.setdefault(k, {})[token] = ev_pref
                    loaded_teams += 1
                elif sub_type == "match":
                    comp = r.get("competition") or ""
                    key = target if "|" in target else _match_key(target, comp)
                    _match_notification_subscriptions.setdefault(key, set()).add(token)
                    _match_subscription_events.setdefault(key, {})[token] = ev_pref
                    loaded_matches += 1
        return {"loaded": len(rows), "matches": loaded_matches, "teams": loaded_teams}
    except Exception as exc:
        logger.warning(f"[notifications] failed to preload subscriptions from SQLite: {exc}")
        return {"loaded": 0, "error": str(exc)}


def send_match_notification(
    match_id: str,
    competition: str,
    title: str,
    body: str,
    event: Optional[str] = None,
) -> int:
    """Queue a regular alert push only to subscribers of ``match_id`` who requested this event."""
    tokens = for_match_tokens(match_id, competition, event=event)
    for token in tokens:
        _apns_notification_queue.append({
            "token": token,
            "title": title,
            "body": body,
            "badge": 0,
            "match_id": match_id,
            "competition": normalize_live_competition(competition),
            "event": normalize_event_type(event) if event else "alert",
        })
    return len(tokens)


def register(
    activity_token: str,
    device_token: str,
    match_id: str,
    competition: str,
    events: Optional[Iterable[str] | str] = None,
) -> bool:
    key = _match_key(match_id, competition)
    norm_events = normalize_events(events)
    with _live_activities_lock:
        existing = _live_activities.setdefault(key, [])
        if any(e["activity_token"] == activity_token for e in existing):
            return False
        existing.append({
            "activity_token": activity_token,
            "device_token": device_token,
            "match_id": str(match_id or "").strip(),
            "competition": normalize_live_competition(competition),
            "events": norm_events,
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
_apns_http2_client = None
_apns_http2_client_lock = threading.Lock()
_apns_recent_logs: deque[dict] = deque(maxlen=50)
_apns_recent_logs_lock = threading.Lock()
_token_environment_cache: dict[str, bool] = {}
_token_environment_lock = threading.Lock()


def _get_apns_client():
    global _apns_http2_client
    if _apns_http2_client is not None:
        return _apns_http2_client
    with _apns_http2_client_lock:
        if _apns_http2_client is None:
            import httpx
            _apns_http2_client = httpx.Client(
                http2=True,
                timeout=10.0,
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            )
        return _apns_http2_client


def _derive_apns_hint(status_code: int | None, reason: str | None, is_la: bool, sandbox: bool) -> str:
    """Provide human-actionable guidance for APNs error responses."""
    import config

    if status_code == 200:
        return (
            "APNs successfully accepted the push notification. If not visible on the device, check: "
            "(1) App notification permissions in iOS Settings -> Notifications. "
            "(2) Do Not Disturb / Focus filter status. "
            "(3) If app is in foreground, UNUserNotificationCenterDelegate willPresent must be implemented to show banners."
        )
    if status_code == 400:
        if reason == "BadDeviceToken":
            env_name = "Sandbox (Development)" if sandbox else "Production"
            return (
                f"Apple rejected this token as invalid for the {env_name} APNs server. "
                "Common causes: "
                "(1) If your app was installed directly from Xcode to your phone or simulator, it generates a Sandbox token. "
                "(2) If installed via TestFlight or App Store, it generates a Production token. "
                "(3) The backend automatically tests both environments when BadDeviceToken occurs; if both fail, the token string may be truncated or belongs to another Team ID / Bundle ID."
            )
        if reason == "DeviceTokenNotForTopic":
            if is_la:
                return (
                    f"DeviceTokenNotForTopic: For Live Activities, apns-topic MUST be '{config.APNS_LIVE_ACTIVITY_TOPIC}'. "
                    "Ensure your widget extension and app bundle identifier match."
                )
            return (
                f"DeviceTokenNotForTopic: The device token does not match topic '{config.APNS_TOPIC}'. "
                "Check your Xcode project Bundle Identifier."
            )
        if reason == "TopicDisallowed":
            return (
                "TopicDisallowed: Pushing to this topic is not permitted for your APNs authentication key. "
                "Verify in Apple Developer Portal that Push Notifications capability is enabled for your App ID."
            )
        return f"APNs returned HTTP 400 with reason: {reason or 'Unknown'}. Verify token format and topic."
    if status_code == 403:
        if reason in ("InvalidProviderToken", "ExpiredProviderToken"):
            return (
                "InvalidProviderToken: The APNs authentication JWT was rejected. "
                "Verify APNS_KEY_ID (10-char Key ID), APNS_TEAM_ID (10-char Team ID), "
                "and ensure the .p8 file at APNS_AUTH_KEY_PATH is correct and downloaded from Apple Developer -> Keys."
            )
        return f"Forbidden (403): {reason or 'Authentication failure'}."
    if status_code == 410:
        return "Unregistered (410): The device has uninstalled the app or this Live Activity has expired."
    if status_code == 413:
        return "PayloadTooLarge (413): Payload exceeds APNs maximum size limit (4096 bytes)."
    if status_code is None:
        return "Network or client failure connecting to Apple APNs server."
    return f"APNs responded with HTTP status {status_code}: {reason or ''}"


def _record_apns_log(result: dict, token: str) -> None:
    entry = {
        "timestamp_utc": result.get("timestamp_utc"),
        "push_type": result.get("push_type"),
        "token_preview": (token[:8] + "..." + token[-6:]) if len(token) > 14 else token,
        "status_code": result.get("status_code"),
        "apns_id": result.get("apns_id"),
        "reason": result.get("reason"),
        "sandbox": result.get("sandbox"),
        "ok": result.get("ok"),
        "error": result.get("error"),
    }
    with _apns_recent_logs_lock:
        _apns_recent_logs.append(entry)


def apns_send_detailed(
    push_token: str,
    payload: dict,
    topic: Optional[str] = None,
    live_activity: bool = False,
    sandbox: Optional[bool] = None,
) -> dict:
    """Send an APNs push notification and return detailed status, headers, and diagnostic hints.
    Automatically detects and handles Sandbox (Xcode / Simulator) vs Production (TestFlight / App Store) tokens.
    """
    import config

    jwt_token = _generate_apns_jwt()
    clean_token = str(push_token or "").strip().replace(" ", "").replace("<", "").replace(">", "")

    explicit_sandbox = sandbox is not None
    if explicit_sandbox:
        use_sandbox = bool(sandbox)
    else:
        with _token_environment_lock:
            cached_env = _token_environment_cache.get(clean_token)
        if cached_env is not None:
            use_sandbox = cached_env
        else:
            use_sandbox = config.APNS_USE_SANDBOX

    base = _APNS_SANDBOX if use_sandbox else _APNS_PRODUCTION
    endpoint = f"{base}/3/device/{clean_token}"

    eff_topic = topic or (config.APNS_LIVE_ACTIVITY_TOPIC if live_activity else config.APNS_TOPIC) or ""

    result: dict[str, Any] = {
        "ok": False,
        "status_code": None,
        "apns_id": None,
        "reason": None,
        "raw_response": None,
        "endpoint": endpoint,
        "topic": eff_topic,
        "sandbox": use_sandbox,
        "push_type": "liveactivity" if live_activity else "alert",
        "jwt_generated": bool(jwt_token),
        "error": None,
        "hint": None,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }

    if not jwt_token:
        result["error"] = "Failed to generate APNs JWT (check APNS_KEY_ID, APNS_TEAM_ID, and APNS_AUTH_KEY_PATH)"
        result["hint"] = "JWT signing key is missing or invalid. Check server status at /api/test/notifications/status"
        _record_apns_log(result, clean_token)
        return result

    headers = {
        "apns-push-type": "liveactivity" if live_activity else "alert",
        "apns-topic": eff_topic,
        "apns-priority": "10",
        "authorization": f"bearer {jwt_token}",
    }
    if live_activity:
        headers["apns-expiration"] = "0"

    try:
        client = _get_apns_client()
        resp = client.post(endpoint, json=payload, headers=headers)
        result["status_code"] = resp.status_code
        result["apns_id"] = resp.headers.get("apns-id")
        result["raw_response"] = resp.text[:500] if resp.text else None
        result["ok"] = resp.is_success

        reason = None
        if resp.text:
            try:
                data = resp.json()
                reason = data.get("reason")
            except Exception:
                reason = resp.text[:100]
        result["reason"] = reason

        # Automatic fallback between Sandbox and Production:
        # If Apple returns BadDeviceToken and caller didn't explicitly force sandbox,
        # retry against the opposite environment (Xcode Simulator/Debug vs TestFlight Beta)
        if not resp.is_success and resp.status_code == 400 and reason == "BadDeviceToken" and not explicit_sandbox:
            alt_sandbox = not use_sandbox
            alt_base = _APNS_SANDBOX if alt_sandbox else _APNS_PRODUCTION
            alt_endpoint = f"{alt_base}/3/device/{clean_token}"
            try:
                alt_resp = client.post(alt_endpoint, json=payload, headers=headers)
                if alt_resp.is_success:
                    resp = alt_resp
                    use_sandbox = alt_sandbox
                    endpoint = alt_endpoint
                    result["status_code"] = resp.status_code
                    result["apns_id"] = resp.headers.get("apns-id")
                    result["raw_response"] = resp.text[:500] if resp.text else None
                    result["ok"] = True
                    result["reason"] = None
                    result["sandbox"] = use_sandbox
                    result["endpoint"] = endpoint
                    with _token_environment_lock:
                        _token_environment_cache[clean_token] = use_sandbox
                    logger.info(
                        "APNs auto-detected %s environment for token %s... (switched from %s)",
                        "Sandbox" if use_sandbox else "Production",
                        clean_token[:10],
                        "Sandbox" if not use_sandbox else "Production",
                    )
            except Exception as alt_exc:
                logger.debug(f"APNs fallback attempt failed: {alt_exc}")

        if resp.is_success:
            with _token_environment_lock:
                _token_environment_cache[clean_token] = use_sandbox

        result["hint"] = _derive_apns_hint(result["status_code"], result["reason"], live_activity, use_sandbox)

        if not result["ok"]:
            logger.warning(
                "APNs %s push failed: status=%s reason=%s token_prefix=%s topic=%s sandbox=%s",
                "live-activity" if live_activity else "alert",
                result["status_code"],
                result["reason"],
                clean_token[:10],
                eff_topic,
                use_sandbox,
            )
    except Exception as exc:
        result["error"] = str(exc)
        result["hint"] = f"Connection error to Apple APNs: {exc}"
        logger.warning(
            "APNs %s push error: %s token_prefix=%s",
            "live-activity" if live_activity else "alert",
            exc,
            clean_token[:10],
        )

    _record_apns_log(result, clean_token)
    return result


def _apns_send(push_token: str, payload: dict, topic: str, live_activity: bool = False) -> bool:
    res = apns_send_detailed(push_token, payload, topic=topic, live_activity=live_activity)
    return bool(res.get("ok"))


def get_apns_diagnostic_status() -> dict:
    """Return comprehensive health and configuration status for APNs delivery."""
    import config

    kid = config.APNS_KEY_ID or ""
    iss = config.APNS_TEAM_ID or ""
    key_path = config.APNS_AUTH_KEY_PATH or ""
    key_exists = bool(key_path and os.path.exists(key_path))
    key_readable = False
    key_size = 0
    if key_exists:
        try:
            key_size = os.path.getsize(key_path)
            with open(key_path, "r") as f:
                head = f.read(27)
                key_readable = "BEGIN PRIVATE KEY" in head or "BEGIN EC PRIVATE KEY" in head
        except Exception:
            key_readable = False

    jwt_test_ok = False
    jwt_error = None
    try:
        jwt_token = _generate_apns_jwt()
        jwt_test_ok = bool(jwt_token)
        if not jwt_token:
            jwt_error = "Missing APNS_KEY_ID, APNS_TEAM_ID, or invalid .p8 file"
    except Exception as exc:
        jwt_error = str(exc)

    with _match_notification_subscriptions_lock:
        match_subs_count = sum(len(toks) for toks in _match_notification_subscriptions.values())
        match_keys = list(_match_notification_subscriptions.keys())
    with _team_notification_subscriptions_lock:
        team_subs_count = sum(len(toks) for toks in _team_notification_subscriptions.values())
        team_keys = list(_team_notification_subscriptions.keys())
    with _live_activities_lock:
        la_count = sum(len(acts) for acts in _live_activities.values())
        la_matches = list(_live_activities.keys())
    with _apns_recent_logs_lock:
        logs_copy = list(_apns_recent_logs)

    return {
        "ok": True,
        "configured": bool(kid and iss and key_exists and jwt_test_ok),
        "credentials": {
            "key_id": (kid[:3] + "***" + kid[-2:]) if len(kid) > 5 else (kid or None),
            "key_id_set": bool(kid),
            "team_id": (iss[:3] + "***" + iss[-2:]) if len(iss) > 5 else (iss or None),
            "team_id_set": bool(iss),
            "key_path": key_path or None,
            "key_path_exists": key_exists,
            "key_file_readable": key_readable,
            "key_file_size_bytes": key_size,
        },
        "apns_settings": {
            "topic": config.APNS_TOPIC or None,
            "live_activity_topic": config.APNS_LIVE_ACTIVITY_TOPIC or None,
            "sandbox": bool(config.APNS_USE_SANDBOX),
            "target_server": _APNS_SANDBOX if config.APNS_USE_SANDBOX else _APNS_PRODUCTION,
        },
        "jwt": {
            "signing_successful": jwt_test_ok,
            "error": jwt_error,
        },
        "in_memory_state": {
            "total_registered_device_tokens": len(ios_device_tokens),
            "match_subscriptions_count": match_subs_count,
            "active_matches": match_keys,
            "team_subscriptions_count": team_subs_count,
            "active_teams": team_keys,
            "active_live_activities_count": la_count,
            "live_activity_matches": la_matches,
            "pending_queue_size": len(_apns_notification_queue),
        },
        "recent_dispatches": logs_copy[-15:],
    }


# ── Queue worker ─────────────────────────────────────────────────

_apns_queue_event = threading.Event()


def _notify_apns_queue() -> None:
    _apns_queue_event.set()


def start_apns_worker() -> None:
    threading.Thread(target=_worker_loop, daemon=True, name="apns-worker").start()


def _worker_loop() -> None:
    while True:
        try:
            _drain_queue()
        except Exception:
            pass
        _apns_queue_event.wait(timeout=1.0)
        _apns_queue_event.clear()


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
            aps: dict[str, Any] = {
                "content-state": entry.get("content_state", {}),
                "timestamp": int(time.time()),
                "event": event,
            }
            # When an alert dict is present, ActivityKit shows a system notification
            # banner and plays the notification sound for this Live Activity update.
            if entry.get("alert"):
                aps["alert"] = entry["alert"]
                aps["sound"] = entry.get("sound", "default")
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
            pass


def send_live_activity_update(
    match_id: str,
    competition: str,
    content_state: dict,
    event: str = "update",
    alert: Optional[dict] = None,
) -> int:
    """Queue a Live Activity content-state update for every registration of a match.

    When ``alert`` is passed (e.g. for goals or halftime), ActivityKit will present
    an alert banner and sound on the user's Lock Screen and Dynamic Island.
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
            "alert": alert,
            "match_id": match_id,
            "competition": normalize_live_competition(competition),
        })
    _notify_apns_queue()
    return len(activities)


def send_live_activity_end(
    match_id: str,
    competition: str,
    content_state: dict | None = None,
    alert: Optional[dict] = None,
) -> int:
    """Queue an 'end' Live Activity push (dismisses the card) for a match with optional alert."""
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
            "alert": alert,
            "match_id": match_id,
            "competition": normalize_live_competition(competition),
        })
    _notify_apns_queue()
    unregister_by_match(match_id, competition)
    return len(activities)


# Preload existing subscriptions from SQLite store on module initialization
try:
    init_subscriptions_from_sqlite()
except Exception:
    pass
