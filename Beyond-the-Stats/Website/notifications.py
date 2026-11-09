"""Push notification and Live Activity delivery via APNs."""
from __future__ import annotations

import functools
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

ALL_EVENT_TYPES: frozenset[str] = frozenset({
    "goal", "start", "end", "ht", "red_card",
    # Period starts: second-half kickoff (halftime ends), extra time, shootout.
    "second_half", "extra_time", "penalties",
})


def normalize_event_type(event: str) -> str:
    """Normalize event name to a canonical event type (see ALL_EVENT_TYPES)."""
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
    if ev in {"second_half", "2h", "2nd_half", "ht_end", "halftime_end", "half_time_end"}:
        return "second_half"
    if ev in {"extra_time", "et", "extratime"}:
        return "extra_time"
    if ev in {"penalties", "shootout", "penalty_shootout"}:
        return "penalties"
    return ev


def normalize_events(events: Any = None) -> set[str]:
    """Return the event set for a subscription — always every event type.

    Notifications are all-or-nothing: following a match or team delivers every
    in-match event (kickoff, goals, red cards, halftime, second half, extra
    time, penalties, full time). Opting out means unsubscribing/unregistering.
    The ``events`` argument is accepted (and ignored) for backward compatibility
    with older app builds that still send per-event preferences.
    """
    return set(ALL_EVENT_TYPES)


# ── In-memory queues and registrations ────────────────────────────

_apns_notification_queue: deque = deque()
_notifications: list[dict] = []
_notifications_lock = threading.Lock()
device_tokens: set[str] = set()
ios_device_tokens: set[str] = set()

# Live Activity storage: key = "{match_id}|{competition}"
_live_activities: dict[str, list[dict]] = {}
_live_activities_lock = threading.RLock()

# Regular push subscription store: key = "{match_id}|{competition}",
# value = set of device tokens that asked to be notified about that match.
_match_notification_subscriptions: dict[str, set[str]] = {}
_match_subscription_events: dict[str, dict[str, set[str]]] = {}
_match_notification_subscriptions_lock = threading.RLock()

# Team push subscription store: key = normalized_team_name,
# value = set of device tokens that asked to be notified about that team.
_team_notification_subscriptions: dict[str, set[str]] = {}
_team_subscription_events: dict[str, dict[str, set[str]]] = {}
_team_notification_subscriptions_lock = threading.RLock()

# Token environment cache: maps clean token to bool (True = Sandbox, False = Production)
_token_environment_cache: dict[str, bool] = {}
_token_environment_lock = threading.Lock()

# Push-to-start (iOS 17.2+): device_token -> {"token": push_to_start_token, ...}.
# One push-to-start token per app install; used to start a Live Activity from
# the server shortly before kickoff for matches/teams the device follows.
_push_to_start_tokens: dict[str, dict] = {}
_push_to_start_lock = threading.RLock()
# Dedupe markers for push-to-start sends: "{device_token}|{match_id}".
_push_started: set[str] = set()

# The only values ActivityKit accepts for ``aps.event``.
LA_APS_EVENTS: frozenset[str] = frozenset({"start", "update", "end"})

# Per-device Live Activity settings reported by the app:
#   frequent_updates   — ActivityAuthorizationInfo().frequentPushesEnabled
#   activities_enabled — ActivityAuthorizationInfo().areActivitiesEnabled
#   supports_broadcast — iOS 18+ (can subscribe to broadcast channels)
_device_la_settings: dict[str, dict] = {}
_device_la_settings_lock = threading.RLock()

# Broadcast channels (iOS 18+): "{match_key}|{environment}" -> channel record.
_la_channels: dict[str, dict] = {}
_la_channels_lock = threading.RLock()

# Devices whose Live Activity listens on a broadcast channel (no per-activity
# token): match identifier (match_id or sync_key) -> set of device tokens.
# Used for duplicate-banner suppression and push-to-start skipping.
_channel_activity_devices: dict[str, set[str]] = {}


def _clean_token(token: Any) -> str:
    """Normalize an APNs hex token string (strip spaces and ``<>`` from Data descriptions)."""
    return str(token or "").strip().replace(" ", "").replace("<", "").replace(">", "")


def _env_label(env: Optional[str | bool]) -> str:
    """Map an environment hint to the persisted label ("sandbox"/"production"), or "" if unknown."""
    if env is None or env == "":
        return ""
    if isinstance(env, bool):
        return "sandbox" if env else "production"
    norm = str(env).strip().lower()
    if norm in {"sandbox", "dev", "development", "debug", "1", "true"}:
        return "sandbox"
    if norm in {"production", "prod", "release", "testflight", "appstore", "app_store", "0", "false"}:
        return "production"
    return ""


def set_token_environment(token: str, env: str | bool) -> bool:
    """Explicitly cache whether a device or Live Activity token is sandbox or production."""
    clean = _clean_token(token)
    if not clean:
        return False
    is_sandbox = _env_label(env) == "sandbox"
    with _token_environment_lock:
        _token_environment_cache[clean] = is_sandbox
    return is_sandbox


def get_token_environment(token: str) -> Optional[bool]:
    """Get the cached environment (True = Sandbox, False = Production) for a token."""
    clean = str(token or "").strip().replace(" ", "").replace("<", "").replace(">", "")
    with _token_environment_lock:
        return _token_environment_cache.get(clean)


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
    for extra in (f"{mid}|", mid):
        if extra not in seen:
            seen.add(extra)
            keys.append(extra)
    return keys


def subscribe_match(
    device_token: str,
    match_id: str,
    competition: str,
    events: Optional[Iterable[str] | str] = None,
    environment: Optional[str | bool] = None,
) -> bool:
    """Follow one match (ESPN match_id or sync_key): the device gets every event of that game."""
    match_id = normalize_match_ident(match_id)
    key = _match_key(match_id, competition)
    norm_events = normalize_events(events)
    clean_token = _clean_token(device_token)
    if not clean_token or not str(match_id or "").strip():
        return False
    if environment is not None:
        set_token_environment(clean_token, environment)
    with _match_notification_subscriptions_lock:
        subs = _match_notification_subscriptions.setdefault(key, set())
        subs.add(clean_token)
        _match_subscription_events.setdefault(key, {})[clean_token] = norm_events
        ios_device_tokens.add(clean_token)
    if sqlite_store is not None:
        try:
            sqlite_store.save_notification_sub(
                clean_token, "match", key, competition=competition, events=norm_events,
                environment=_env_label(environment),
            )
        except Exception as exc:
            logger.warning(f"[notifications] failed to save match sub to SQLite: {exc}")
    return True


def unsubscribe_match(device_token: str, match_id: str, competition: str = "") -> bool:
    """Remove a device token from one match's regular-push list.

    Removes the token from every registry key for the match (all competition
    aliases, including the empty-competition key), so callers do not need to
    send the exact competition string used at subscribe time.
    """
    clean_token = _clean_token(device_token)
    mid = normalize_match_ident(match_id)
    if not clean_token or not mid:
        return False
    deleted = False
    with _match_notification_subscriptions_lock:
        for key in list(_match_notification_subscriptions.keys()):
            if key != mid and not key.startswith(f"{mid}|"):
                continue
            subs = _match_notification_subscriptions.get(key)
            if subs and clean_token in subs:
                subs.discard(clean_token)
                if not subs:
                    del _match_notification_subscriptions[key]
                deleted = True
            ev_map = _match_subscription_events.get(key)
            if ev_map and clean_token in ev_map:
                del ev_map[clean_token]
                if not ev_map:
                    _match_subscription_events.pop(key, None)
    if sqlite_store is not None:
        try:
            sqlite_store.remove_token_match_subs(clean_token, mid)
        except Exception as exc:
            logger.warning(f"[notifications] failed to remove match sub from SQLite: {exc}")
    return deleted


def for_match_tokens(match_id: str, competition: str = "", event: Optional[str] = None) -> list[str]:
    """Device tokens subscribed to a specific match (by ESPN match_id or sync_key).

    Every registry key for the match is checked (all competition aliases and the
    empty-competition key): match identifiers are unique, so the competition
    suffix never decides who follows a match. ``event`` is accepted for
    backward compatibility; follows are all-events.
    """
    norm_event = normalize_event_type(event) if event else None
    tokens: list[str] = []
    seen: set[str] = set()
    mid = normalize_match_ident(match_id)
    if not mid:
        return tokens
    with _match_notification_subscriptions_lock:
        keys = list(_match_lookup_keys(mid, competition))
        keys.extend(k for k in _match_notification_subscriptions if (k == mid or k.startswith(f"{mid}|")) and k not in keys)
        for key in keys:
            ev_map = _match_subscription_events.get(key, {})
            for token in _match_notification_subscriptions.get(key, ()):
                if token in seen:
                    continue
                if norm_event and norm_event not in ev_map.get(token, ALL_EVENT_TYPES):
                    continue
                seen.add(token)
                tokens.append(token)
    return tokens


def normalize_match_ident(value: Any) -> str:
    """Normalize a client-supplied match identifier (ESPN id or sync_key).

    URL-decodes (``%7C`` → ``|``), trims, and lowercases sync keys so they equal
    the server-generated ``YYYY-MM-DD|home|away`` keys the poller uses.
    """
    from urllib.parse import unquote

    text = unquote(str(value or "")).strip()
    return text.lower() if "|" in text else text


def clear_match_subscriptions(match_id: str, competition: str, sync_key: str = "") -> int:
    """Drop all regular-push subscriptions (by match_id and sync_key) for a finished match."""
    removed = 0
    idents = [i for i in (str(match_id or "").strip(), str(sync_key or "").strip()) if i]
    with _match_notification_subscriptions_lock:
        for ident in idents:
            keys = set(_match_lookup_keys(ident, competition))
            keys.update(k for k in _match_notification_subscriptions if k == ident or k.startswith(f"{ident}|"))
            for key in keys:
                subs = _match_notification_subscriptions.pop(key, None)
                _match_subscription_events.pop(key, None)
                if subs:
                    removed += len(subs)
    if sqlite_store is not None:
        try:
            sqlite_store.remove_match_notification_subs(
                match_id, competition=competition, sync_key=sync_key, sub_types=("match",)
            )
        except Exception as exc:
            logger.warning(f"[notifications] failed to clear match subs from SQLite: {exc}")
    return removed


# ── Team follows (keyed by resolved team identity) ───────────────
# Team subscriptions are matched by the resolved team id from
# standardized_teams.resolve_team_info (the same id /api/teams/catalog exposes),
# never by name prefixes. That keeps aliases together ("Man United" /
# "Manchester United", "PSG" / "Paris Saint-Germain", "Celtic" / "Celtic FC")
# while keeping distinct clubs apart ("Inter" vs "Inter Miami", "Real Sociedad"
# vs "Real Sociedad B", "Newcastle" vs "Newcastle Jets"). Unmapped teams fall
# back to a slug of their exact name, so they only match the same name.

# token -> {team_id: {"team": display name, "competition": str}}
_team_follow_records: dict[str, dict[str, dict]] = {}


@functools.lru_cache(maxsize=8192)
def _resolve_team_id_cached(team_norm: str, competition: str) -> str:
    try:
        from standardized_teams import resolve_team_info
        info = resolve_team_info(team_norm, competition) or {}
        return str(info.get("id") or "").strip().lower()
    except Exception:
        return ""


def resolve_team_id(team: str, competition: str = "", team_id: str = "") -> str:
    """Stable team identity for subscription matching ("" if nothing usable)."""
    explicit = str(team_id or "").strip().lower()
    if explicit:
        return explicit
    name = str(team or "").strip()
    if not name:
        return ""
    tid = _resolve_team_id_cached(name, str(competition or "").strip())
    if tid:
        return tid
    # Last resort: exact-name identity (only ever matches the same name).
    return "name:" + normalize_team_name(name)


def _team_registry_keys(team: str, competition: str = "", team_id: str = "") -> list[str]:
    """The single registry key a team follow is stored and looked up under."""
    tid = resolve_team_id(team, competition, team_id)
    return [f"id:{tid}"] if tid else []


def subscribe_team(
    device_token: str,
    team: str,
    competition: str = "",
    events: Optional[Iterable[str] | str] = None,
    environment: Optional[str | bool] = None,
    team_id: str = "",
) -> bool:
    """Follow a team: the device gets every event of every game that team plays.

    ``team_id`` (from /api/teams/catalog) is the most reliable identifier; a
    team name (any alias) plus optional ``competition`` also works.
    """
    clean_token = _clean_token(device_token)
    tid = resolve_team_id(team, competition, team_id)
    if not clean_token or not tid:
        return False
    norm_events = normalize_events(events)
    if environment is not None:
        set_token_environment(clean_token, environment)
    key = f"id:{tid}"
    display = str(team or "").strip() or tid
    with _team_notification_subscriptions_lock:
        _team_notification_subscriptions.setdefault(key, set()).add(clean_token)
        _team_subscription_events.setdefault(key, {})[clean_token] = norm_events
        _team_follow_records.setdefault(clean_token, {})[tid] = {
            "team": display, "competition": str(competition or "").strip(),
        }
        ios_device_tokens.add(clean_token)
    if sqlite_store is not None:
        try:
            sqlite_store.save_notification_sub(
                clean_token, "team", normalize_team_name(display), competition=competition, events=norm_events,
                environment=_env_label(environment), team_id=tid,
            )
        except Exception as exc:
            logger.warning(f"[notifications] failed to save team sub to SQLite: {exc}")
    return True


def unsubscribe_team(device_token: str, team: str = "", competition: str = "", team_id: str = "") -> bool:
    """Unfollow a team (by any alias or team_id) from memory and SQLite."""
    clean_token = _clean_token(device_token)
    tid = resolve_team_id(team, competition, team_id)
    if not clean_token or not tid:
        return False
    key = f"id:{tid}"
    deleted = False
    with _team_notification_subscriptions_lock:
        subs = _team_notification_subscriptions.get(key)
        if subs and clean_token in subs:
            subs.discard(clean_token)
            deleted = True
            if not subs:
                del _team_notification_subscriptions[key]
        ev_map = _team_subscription_events.get(key)
        if ev_map:
            ev_map.pop(clean_token, None)
            if not ev_map:
                _team_subscription_events.pop(key, None)
        records = _team_follow_records.get(clean_token)
        if records:
            records.pop(tid, None)
            if not records:
                _team_follow_records.pop(clean_token, None)
    if sqlite_store is not None:
        try:
            # Delete every stored row for this token that resolves to the same
            # team, whatever alias it was saved under (incl. legacy rows that
            # predate the team_id column).
            for row in sqlite_store.load_all_notification_subs():
                if row.get("token") != clean_token or row.get("sub_type") != "team":
                    continue
                row_tid = resolve_team_id(row.get("sub_target", ""), row.get("competition", ""), row.get("team_id", ""))
                if row_tid == tid:
                    sqlite_store.remove_notification_sub(clean_token, "team", row["sub_target"])
        except Exception as exc:
            logger.warning(f"[notifications] failed to remove team sub from SQLite: {exc}")
    return deleted


def for_team_tokens(
    team: str,
    event: Optional[str] = None,
    competition: str = "",
    team_id: str = "",
) -> list[str]:
    """Device tokens following a team — exact team-identity match only (no fuzzy names).

    ``event`` is accepted for backward compatibility; follows are all-events.
    """
    norm_event = normalize_event_type(event) if event else None
    tokens: list[str] = []
    with _team_notification_subscriptions_lock:
        for key in _team_registry_keys(team, competition, team_id):
            ev_map = _team_subscription_events.get(key, {})
            for token in _team_notification_subscriptions.get(key, ()):
                if token in tokens:
                    continue
                if norm_event and norm_event not in ev_map.get(token, ALL_EVENT_TYPES):
                    continue
                tokens.append(token)
    return tokens


def followed_teams(device_token: str) -> list[dict]:
    """Teams a device follows: [{"team", "team_id", "competition"}]."""
    with _team_notification_subscriptions_lock:
        records = dict(_team_follow_records.get(_clean_token(device_token)) or {})
    return [
        {"team": rec.get("team", tid), "team_id": tid, "competition": rec.get("competition", "")}
        for tid, rec in sorted(records.items(), key=lambda kv: kv[1].get("team", kv[0]).lower())
    ]


def for_game_or_team_subscribers(
    match_id: str,
    competition: str,
    home_team: str = "",
    away_team: str = "",
    event: Optional[str] = None,
    sync_key: str = "",
    exclude_live_activity_devices: bool = False,
) -> list[str]:
    """Return unified, deduplicated list of tokens subscribed to match OR either team (filtered by event).

    With ``exclude_live_activity_devices``, devices that own an active Live
    Activity for this match are skipped: the Live Activity's own alert already
    lights up the screen, so a regular push would be a duplicate banner.
    """
    seen: set[str] = set()
    if exclude_live_activity_devices:
        seen.update(live_activity_device_tokens(match_id, competition, sync_key=sync_key))
    tokens: list[str] = []
    # 1. Match-level subscribers (match_id)
    for tok in for_match_tokens(match_id, competition, event=event):
        if tok not in seen:
            seen.add(tok)
            tokens.append(tok)
    # 1b. Match-level subscribers (sync_key if different)
    if sync_key and str(sync_key).strip() != str(match_id).strip():
        for tok in for_match_tokens(sync_key, competition, event=event):
            if tok not in seen:
                seen.add(tok)
                tokens.append(tok)
    # 2. Home team subscribers
    if home_team:
        for tok in for_team_tokens(home_team, event=event, competition=competition):
            if tok not in seen:
                seen.add(tok)
                tokens.append(tok)
    # 3. Away team subscribers
    if away_team:
        for tok in for_team_tokens(away_team, event=event, competition=competition):
            if tok not in seen:
                seen.add(tok)
                tokens.append(tok)
    return tokens


def live_activity_device_tokens(match_id: str, competition: str = "", sync_key: str = "") -> set[str]:
    """Device tokens that own an active Live Activity for a match (token-based or channel-based)."""
    devices = {
        str(e.get("device_token") or "").strip()
        for e in for_match(match_id, competition, sync_key=sync_key)
        if str(e.get("device_token") or "").strip()
    }
    with _live_activities_lock:
        for ident in (str(match_id or "").strip(), str(sync_key or "").strip()):
            if ident:
                devices.update(_channel_activity_devices.get(ident, ()))
    return devices


def get_device_subscriptions(token: str) -> dict:
    """Return all matches, teams, and subscribed events for a device token."""
    t = _clean_token(token)
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
    follows = followed_teams(t)
    for rec in follows:
        name = normalize_team_name(rec["team"])
        teams.append(name)
        team_events[name] = sorted(ALL_EVENT_TYPES)
    return {
        "matches": sorted(matches),
        "teams": sorted(set(teams)),
        "team_ids": sorted({rec["team_id"] for rec in follows}),
        "followed_teams": follows,
        "match_events": match_events,
        "team_events": team_events,
    }


def prune_dead_token(token: str, is_live_activity: bool = False) -> None:
    """Purge an invalid or unregistered token (APNs 410 or fatal BadDeviceToken) from all stores."""
    clean_token = str(token or "").strip().replace(" ", "").replace("<", "").replace(">", "")
    if not clean_token:
        return
    logger.info("Pruning dead APNs token: %s... (live_activity=%s)", clean_token[:12], is_live_activity)

    with _token_environment_lock:
        _token_environment_cache.pop(clean_token, None)

    if is_live_activity:
        unregister(clean_token)
    else:
        device_tokens.discard(clean_token)
        ios_device_tokens.discard(clean_token)

        with _match_notification_subscriptions_lock:
            for k in list(_match_notification_subscriptions.keys()):
                subs = _match_notification_subscriptions[k]
                subs.discard(clean_token)
                if not subs:
                    del _match_notification_subscriptions[k]
                ev_map = _match_subscription_events.get(k)
                if ev_map and clean_token in ev_map:
                    del ev_map[clean_token]
                    if not ev_map:
                        _match_subscription_events.pop(k, None)

        with _team_notification_subscriptions_lock:
            _team_follow_records.pop(clean_token, None)
            for team in list(_team_notification_subscriptions.keys()):
                subs = _team_notification_subscriptions[team]
                subs.discard(clean_token)
                if not subs:
                    del _team_notification_subscriptions[team]
                ev_map = _team_subscription_events.get(team)
                if ev_map and clean_token in ev_map:
                    del ev_map[clean_token]
                    if not ev_map:
                        _team_subscription_events.pop(team, None)

        # A dead device token means the app is gone; its push-to-start token,
        # channel-activity markers, and Live Activity settings are too.
        unregister_push_to_start(device_token=clean_token)
        with _live_activities_lock:
            for ident in list(_channel_activity_devices.keys()):
                _channel_activity_devices[ident].discard(clean_token)
                if not _channel_activity_devices[ident]:
                    del _channel_activity_devices[ident]
        with _device_la_settings_lock:
            _device_la_settings.pop(clean_token, None)

        if sqlite_store is not None:
            try:
                sqlite_store.remove_all_token_subscriptions(clean_token)
                if hasattr(sqlite_store, "delete_live_activity_device_settings"):
                    sqlite_store.delete_live_activity_device_settings(clean_token)
            except Exception as exc:
                logger.warning(f"[notifications] failed to remove dead token from SQLite: {exc}")


def unregister_device(token: str) -> bool:
    """Fully remove a device token: registration, match/team subscriptions, push-to-start, SQLite rows."""
    clean_token = _clean_token(token)
    if not clean_token:
        return False
    existed = clean_token in ios_device_tokens or clean_token in device_tokens
    with _push_to_start_lock:
        existed = existed or clean_token in _push_to_start_tokens
    prune_dead_token(clean_token, is_live_activity=False)
    return existed


def record_notification_event(
    title: str,
    body: str,
    match_id: str = "",
    competition: str = "",
    notif_type: str = "live_event",
    sync_key: str = "",
    home_team: str = "",
    away_team: str = "",
    home_score: Any = None,
    away_score: Any = None,
) -> dict:
    """Record an event notification in the in-memory feed and SQLite store for GET /api/notifications."""
    now_iso = datetime.now(timezone.utc).isoformat()
    entry = {
        "id": len(_notifications) + 1,
        "title": str(title or "").strip(),
        "body": str(body or "").strip(),
        "created_at": now_iso,
        "type": normalize_event_type(notif_type) if notif_type else "live_event",
        "match_id": str(match_id or "").strip(),
        "sync_key": str(sync_key or "").strip(),
        "competition": normalize_live_competition(competition),
        "home_team": str(home_team or "").strip(),
        "away_team": str(away_team or "").strip(),
        "home_score": home_score,
        "away_score": away_score,
    }
    with _notifications_lock:
        entry["id"] = len(_notifications) + 1
        _notifications.append(entry)
        if len(_notifications) > 500:
            del _notifications[:-500]

    if sqlite_store is not None and hasattr(sqlite_store, "save_notification_event"):
        try:
            sqlite_store.save_notification_event(
                title=entry["title"],
                body=entry["body"],
                notif_type=entry["type"],
                match_id=entry["match_id"],
                sync_key=entry["sync_key"],
                competition=entry["competition"],
                home_team=entry["home_team"],
                away_team=entry["away_team"],
                home_score=entry["home_score"],
                away_score=entry["away_score"],
                created_at=now_iso,
            )
        except Exception as exc:
            logger.warning(f"[notifications] failed to save notification event to SQLite: {exc}")

    return entry


def _load_la_settings_and_channels_from_sqlite() -> None:
    """Preload per-device Live Activity settings and broadcast channels."""
    if sqlite_store is None:
        return
    try:
        if hasattr(sqlite_store, "load_live_activity_device_settings"):
            with _device_la_settings_lock:
                for row in sqlite_store.load_live_activity_device_settings():
                    _device_la_settings[row["device_token"]] = {
                        k: row.get(k)
                        for k in ("frequent_updates", "activities_enabled", "supports_broadcast", "ios_version")
                    }
        if hasattr(sqlite_store, "load_live_activity_channels"):
            with _la_channels_lock:
                for row in sqlite_store.load_live_activity_channels():
                    _la_channels[f"{row['match_key']}|{row['environment']}"] = dict(row)
    except Exception as exc:
        logger.warning(f"[notifications] failed to preload LA settings/channels: {exc}")


def init_subscriptions_from_sqlite() -> dict:
    """Preload active subscriptions and event preferences from SQLite store into memory."""
    if sqlite_store is None:
        return {"loaded": 0, "matches": 0, "teams": 0}
    try:
        rows = sqlite_store.load_all_notification_subs()
        loaded_matches = 0
        loaded_teams = 0
        loaded_devices = 0
        loaded_live_activities = 0
        loaded_push_to_start = 0
        with (
            _match_notification_subscriptions_lock,
            _team_notification_subscriptions_lock,
            _live_activities_lock,
            _push_to_start_lock,
        ):
            for r in rows:
                token = r.get("token")
                sub_type = r.get("sub_type")
                target = r.get("sub_target")
                if not token or not target:
                    continue
                env = r.get("environment") or ""
                if env:
                    set_token_environment(token, env)
                # Only real device tokens join the device registry; Live Activity
                # and push-to-start tokens must never receive regular alert pushes.
                if sub_type in ("device", "match", "team"):
                    ios_device_tokens.add(token)
                if sub_type == "device":
                    loaded_devices += 1
                    continue
                ev_pref = normalize_events(r.get("events"))
                if sub_type == "team":
                    comp = r.get("competition") or ""
                    tid = resolve_team_id(target, comp, r.get("team_id") or "")
                    if tid:
                        k = f"id:{tid}"
                        _team_notification_subscriptions.setdefault(k, set()).add(token)
                        _team_subscription_events.setdefault(k, {})[token] = ev_pref
                        _team_follow_records.setdefault(token, {})[tid] = {"team": target, "competition": comp}
                    loaded_teams += 1
                elif sub_type == "match":
                    comp = r.get("competition") or ""
                    key = target if "|" in target else _match_key(target, comp)
                    _match_notification_subscriptions.setdefault(key, set()).add(token)
                    _match_subscription_events.setdefault(key, {})[token] = ev_pref
                    loaded_matches += 1
                elif sub_type == "live_activity":
                    comp = r.get("competition") or ""
                    key = _match_key(target, comp)
                    clean_act = str(token).strip()
                    existing = _live_activities.setdefault(key, [])
                    if not any(e.get("activity_token") == clean_act for e in existing):
                        existing.append({
                            "activity_token": clean_act,
                            "device_token": r.get("device_token") or "",
                            "match_id": target,
                            "sync_key": r.get("sync_key") or "",
                            "competition": normalize_live_competition(comp),
                            "events": ev_pref,
                            "registered_at": r.get("created_at") or datetime.now(timezone.utc).isoformat(),
                        })
                        loaded_live_activities += 1
                elif sub_type == "la_push_to_start":
                    _push_to_start_tokens[target] = {
                        "token": str(token).strip(),
                        "device_token": target,
                        "registered_at": r.get("created_at") or datetime.now(timezone.utc).isoformat(),
                    }
                    loaded_push_to_start += 1
                elif sub_type == "la_started":
                    _push_started.add(f"{token}|{target}")
                elif sub_type == "la_channel":
                    # token = device, target = match_id (sync_key in its own column)
                    _channel_activity_devices.setdefault(target, set()).add(token)
                    if r.get("sync_key"):
                        _channel_activity_devices.setdefault(r["sync_key"], set()).add(token)

        _load_la_settings_and_channels_from_sqlite()

        # Also preload recent notification history into memory if empty
        with _notifications_lock:
            if not _notifications and hasattr(sqlite_store, "load_recent_notification_events"):
                try:
                    recent = sqlite_store.load_recent_notification_events(50)
                    for item in reversed(recent):
                        _notifications.append(item)
                except Exception as exc:
                    logger.debug(f"[notifications] failed to preload history: {exc}")

        return {
            "loaded": len(rows),
            "matches": loaded_matches,
            "teams": loaded_teams,
            "devices": loaded_devices,
            "live_activities": loaded_live_activities,
            "push_to_start": loaded_push_to_start,
        }
    except Exception as exc:
        logger.warning(f"[notifications] failed to preload subscriptions from SQLite: {exc}")
        return {"loaded": 0, "error": str(exc)}


def send_match_notification(
    match_id: str,
    competition: str,
    title: str,
    body: str,
    event: Optional[str] = None,
    home_team: str = "",
    away_team: str = "",
    sync_key: str = "",
    collapse_id: str = "",
) -> int:
    """Queue a regular alert push only to subscribers of ``match_id`` / teams who requested this event.

    Devices with an active Live Activity for the match are skipped (the Live
    Activity alert covers them).
    """
    tokens = for_game_or_team_subscribers(
        match_id, competition, home_team=home_team, away_team=away_team, event=event, sync_key=sync_key,
        exclude_live_activity_devices=True,
    )
    for token in tokens:
        _apns_notification_queue.append({
            "token": token,
            "title": title,
            "body": body,
            "badge": 1,
            "match_id": match_id,
            "sync_key": sync_key,
            "competition": normalize_live_competition(competition),
            "event": normalize_event_type(event) if event else "alert",
            "collapse_id": collapse_id or "",
        })
    _notify_apns_queue()
    return len(tokens)


def register(
    activity_token: str,
    device_token: str,
    match_id: str,
    competition: str,
    events: Optional[Iterable[str] | str] = None,
    environment: Optional[str | bool] = None,
    sync_key: str = "",
) -> bool:
    """Register a Live Activity update push token for a match.

    Returns False when the token was already registered (its device_token /
    sync_key metadata is refreshed if newly supplied).
    """
    match_id = normalize_match_ident(match_id)
    key = _match_key(match_id, competition)
    norm_events = normalize_events(events)
    clean_activity = _clean_token(activity_token)
    clean_device = _clean_token(device_token)
    clean_sync = normalize_match_ident(sync_key)
    if not clean_activity or not str(match_id or "").strip():
        return False
    if environment is not None:
        if clean_activity:
            set_token_environment(clean_activity, environment)
        if clean_device:
            set_token_environment(clean_device, environment)

    entry = {
        "activity_token": clean_activity,
        "device_token": clean_device,
        "match_id": str(match_id or "").strip(),
        "sync_key": clean_sync,
        "competition": normalize_live_competition(competition),
        "events": norm_events,
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }

    created = True
    with _live_activities_lock:
        existing = _live_activities.setdefault(key, [])
        prior = next((e for e in existing if e["activity_token"] == clean_activity), None)
        if prior is not None:
            created = False
            if clean_device:
                prior["device_token"] = clean_device
            if clean_sync:
                prior["sync_key"] = clean_sync
        else:
            existing.append(entry)

        if created and clean_sync and clean_sync != match_id:
            s_key = _match_key(clean_sync, competition)
            if s_key != key:
                s_existing = _live_activities.setdefault(s_key, [])
                if not any(e["activity_token"] == clean_activity for e in s_existing):
                    s_existing.append(entry)

    if sqlite_store is not None:
        try:
            env_label = _env_label(environment)
            sqlite_store.save_notification_sub(
                clean_activity,
                "live_activity",
                match_id,
                competition=competition,
                events=norm_events,
                device_token=clean_device,
                sync_key=clean_sync,
                environment=env_label,
            )
            if clean_sync and clean_sync != match_id:
                sqlite_store.save_notification_sub(
                    clean_activity,
                    "live_activity",
                    clean_sync,
                    competition=competition,
                    events=norm_events,
                    device_token=clean_device,
                    sync_key=clean_sync,
                    environment=env_label,
                )
        except Exception as exc:
            logger.warning(f"[notifications] failed to save live activity to SQLite: {exc}")

    return created


def unregister(activity_token: str) -> bool:
    """Remove a Live Activity token (or a push-to-start token) from memory and SQLite."""
    clean_activity = _clean_token(activity_token)
    if not clean_activity:
        return False
    removed = unregister_push_to_start(push_to_start_token=clean_activity)
    with _live_activities_lock:
        for key in list(_live_activities):
            before = len(_live_activities[key])
            _live_activities[key] = [
                e for e in _live_activities[key] if e["activity_token"] != clean_activity
            ]
            if len(_live_activities[key]) < before:
                removed = True
            if not _live_activities[key]:
                del _live_activities[key]
    if sqlite_store is not None:
        try:
            sqlite_store.remove_all_token_subscriptions(clean_activity)
        except Exception as exc:
            logger.warning(f"[notifications] failed to remove live activity from SQLite: {exc}")
    return removed


def for_match(match_id: str, competition: str, sync_key: str = "") -> list[dict]:
    """Return Live Activity registrations for a match across competition aliases and sync keys."""
    out: list[dict] = []
    seen_tokens: set[str] = set()
    mid = normalize_match_ident(match_id)
    s_key = normalize_match_ident(sync_key)
    keys_to_search = _match_lookup_keys(mid, competition)
    if s_key and s_key != mid:
        for k in _match_lookup_keys(s_key, competition):
            if k not in keys_to_search:
                keys_to_search.append(k)

    with _live_activities_lock:
        for key in keys_to_search:
            for entry in _live_activities.get(key, []):
                token = entry.get("activity_token")
                if not token or token in seen_tokens:
                    continue
                seen_tokens.add(token)
                out.append(dict(entry))

        targets = [t for t in (mid, s_key) if t]
        if targets:
            for key, entries in _live_activities.items():
                for entry in entries:
                    token = entry.get("activity_token")
                    if not token or token in seen_tokens:
                        continue
                    m_val = str(entry.get("match_id", "")).strip()
                    s_val = str(entry.get("sync_key", "")).strip()
                    if any(t in (m_val, s_val) or key == t or key.startswith(f"{t}|") for t in targets):
                        seen_tokens.add(token)
                        out.append(dict(entry))
    return out


def unregister_by_match(match_id: str, competition: str, sync_key: str = "") -> bool:
    """Drop every Live Activity registration for a match (by match_id and sync_key), memory + SQLite."""
    removed = False
    mid = str(match_id or "").strip()
    s_key = str(sync_key or "").strip()
    targets = [t for t in (mid, s_key) if t]
    with _live_activities_lock:
        for ident in targets:
            for key in _match_lookup_keys(ident, competition):
                if _live_activities.pop(key, None) is not None:
                    removed = True
        for k in list(_live_activities.keys()):
            entries = _live_activities.get(k) or []
            key_hit = any(k == t or k.startswith(f"{t}|") for t in targets)
            kept = [
                e for e in entries
                if not key_hit
                and str(e.get("match_id", "")).strip() not in targets
                and str(e.get("sync_key", "")).strip() not in targets
            ]
            if len(kept) != len(entries):
                removed = True
            if kept:
                _live_activities[k] = kept
            else:
                _live_activities.pop(k, None)
        for ident in targets:
            if _channel_activity_devices.pop(ident, None):
                removed = True
    if sqlite_store is not None and targets:
        try:
            sqlite_store.remove_match_notification_subs(
                mid, competition=competition, sync_key=s_key,
                sub_types=("live_activity", "la_started", "la_channel"),
            )
        except Exception as exc:
            logger.warning(f"[notifications] failed to remove match live activities from SQLite: {exc}")
    return removed


def all_activities() -> list[dict]:
    with _live_activities_lock:
        return [
            {**entry, "key": key}
            for key, entries in _live_activities.items()
            for entry in entries
        ]


# ── Push-to-start (iOS 17.2+) ─────────────────────────────────────

def register_push_to_start(
    push_to_start_token: str,
    device_token: str,
    environment: Optional[str | bool] = None,
) -> bool:
    """Register a device's ActivityKit push-to-start token.

    The device_token links the push-to-start token to the device's match/team
    subscriptions, which decide which games get a server-started Live Activity.
    Re-registering a device replaces its previous push-to-start token.
    """
    pts = _clean_token(push_to_start_token)
    device = _clean_token(device_token)
    if not pts or not device:
        return False
    if environment is not None:
        set_token_environment(pts, environment)
        set_token_environment(device, environment)
    with _push_to_start_lock:
        prior = _push_to_start_tokens.get(device)
        _push_to_start_tokens[device] = {
            "token": pts,
            "device_token": device,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
    ios_device_tokens.add(device)
    if sqlite_store is not None:
        try:
            if prior and prior.get("token") != pts:
                sqlite_store.remove_push_to_start_for_device(device)
            sqlite_store.save_notification_sub(
                pts, "la_push_to_start", device, environment=_env_label(environment), device_token=device,
            )
        except Exception as exc:
            logger.warning(f"[notifications] failed to save push-to-start token to SQLite: {exc}")
    return True


def unregister_push_to_start(push_to_start_token: str = "", device_token: str = "") -> bool:
    """Remove a push-to-start token, looked up by the token itself or by its owning device."""
    pts = _clean_token(push_to_start_token)
    device = _clean_token(device_token)
    if not pts and not device:
        return False
    removed_devices: list[str] = []
    with _push_to_start_lock:
        for dev, info in list(_push_to_start_tokens.items()):
            if (device and dev == device) or (pts and info.get("token") == pts):
                _push_to_start_tokens.pop(dev, None)
                removed_devices.append(dev)
    if sqlite_store is not None and removed_devices:
        try:
            for dev in removed_devices:
                sqlite_store.remove_push_to_start_for_device(dev)
        except Exception as exc:
            logger.warning(f"[notifications] failed to remove push-to-start token from SQLite: {exc}")
    return bool(removed_devices)


def push_to_start_token_for_device(device_token: str) -> str:
    """Return the push-to-start token registered for a device ("" if none)."""
    with _push_to_start_lock:
        info = _push_to_start_tokens.get(_clean_token(device_token))
        return str(info.get("token") or "") if info else ""


def push_to_start_devices() -> list[str]:
    """Device tokens that have a push-to-start token registered."""
    with _push_to_start_lock:
        return list(_push_to_start_tokens.keys())


def was_push_started(device_token: str, match_id: str) -> bool:
    """True if a push-to-start 'start' was already sent to this device for this match."""
    with _push_to_start_lock:
        return f"{_clean_token(device_token)}|{str(match_id or '').strip()}" in _push_started


def mark_push_started(device_token: str, match_id: str, competition: str = "", sync_key: str = "") -> None:
    """Record (memory + SQLite) that a 'start' was sent, so restarts never start a duplicate activity."""
    device = _clean_token(device_token)
    mid = str(match_id or "").strip()
    if not device or not mid:
        return
    with _push_to_start_lock:
        _push_started.add(f"{device}|{mid}")
    if sqlite_store is not None:
        try:
            sqlite_store.save_notification_sub(
                device, "la_started", mid, competition=competition, sync_key=sync_key,
            )
        except Exception as exc:
            logger.warning(f"[notifications] failed to save push-start marker to SQLite: {exc}")


def send_live_activity_start(
    device_token: str,
    match_id: str,
    competition: str,
    content_state: dict,
    attributes: dict,
    alert: dict,
    sync_key: str = "",
    stale_date: Optional[int] = None,
    relevance_score: Optional[float] = None,
    channel_id: str = "",
) -> bool:
    """Queue an ActivityKit push-to-start ("event": "start") for one device.

    Requires the device to have registered a push-to-start token. Apple requires
    an ``alert`` on start payloads. Marks the (device, match) pair as started.
    With ``channel_id`` (iOS 18+), the started activity subscribes to that
    broadcast channel (``input-push-channel``) instead of requesting an update
    token (``input-push-token``).
    """
    pts = push_to_start_token_for_device(device_token)
    if not pts or not match_id:
        return False
    if was_push_started(device_token, match_id):
        return False
    state = dict(content_state or {})
    state.setdefault("match_id", str(match_id))
    if sync_key:
        state.setdefault("sync_key", sync_key)
    state.setdefault("competition", normalize_live_competition(competition))
    _apns_notification_queue.append({
        "type": "liveactivity",
        "token": pts,
        "event": "start",
        "content_state": state,
        "attributes": dict(attributes or {}),
        "alert": dict(alert or {}) or {"title": "Match starting", "body": ""},
        "stale_date": stale_date,
        "relevance_score": relevance_score,
        "input_push_channel": str(channel_id or ""),
        "priority": 10,
        "match_id": str(match_id),
        "sync_key": sync_key,
        "competition": normalize_live_competition(competition),
    })
    mark_push_started(device_token, match_id, competition=competition, sync_key=sync_key)
    _notify_apns_queue()
    return True


# ── Per-device Live Activity settings ─────────────────────────────

_LA_SETTINGS_DEFAULTS: dict[str, Any] = {
    "frequent_updates": True,
    "activities_enabled": True,
    "supports_broadcast": False,
    "ios_version": "",
}


def _ios_major(version: Any) -> Optional[int]:
    """Parse the major iOS version from strings like "18.1" / "17.4.1" (None if unknown)."""
    try:
        return int(str(version or "").strip().split(".")[0])
    except (TypeError, ValueError):
        return None


def _opt_bool(value: Any) -> Optional[bool]:
    """Coerce JSON-ish booleans ("1", "true", 0, ...) to bool; None stays None."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def set_device_la_settings(
    device_token: str,
    frequent_updates: Any = None,
    activities_enabled: Any = None,
    supports_broadcast: Any = None,
    ios_version: Any = None,
) -> dict:
    """Record a device's Live Activity settings (``None`` leaves a value unchanged).

    - frequent_updates:   ActivityAuthorizationInfo().frequentPushesEnabled. When
      False, non-alert updates are sent at priority 5 so they don't burn the
      (smaller) budget and get throttled.
    - activities_enabled: ActivityAuthorizationInfo().areActivitiesEnabled. When
      False, no push-to-start is sent to the device.
    - supports_broadcast: iOS 18+ (derived from ios_version when not given). When
      True and broadcast is enabled, push-to-start subscribes the new activity to
      the match's broadcast channel instead of issuing a per-activity token.
    """
    device = _clean_token(device_token)
    if not device:
        return {}
    fu = _opt_bool(frequent_updates)
    ae = _opt_bool(activities_enabled)
    sb = _opt_bool(supports_broadcast)
    ver = str(ios_version).strip() if ios_version not in (None, "") else None
    if sb is None and ver is not None:
        major = _ios_major(ver)
        if major is not None:
            sb = major >= 18
    with _device_la_settings_lock:
        current = dict(_device_la_settings.get(device) or {})
        for key, val in (("frequent_updates", fu), ("activities_enabled", ae),
                         ("supports_broadcast", sb), ("ios_version", ver)):
            if val is not None:
                current[key] = val
        _device_la_settings[device] = current
    if sqlite_store is not None and hasattr(sqlite_store, "save_live_activity_device_settings"):
        try:
            sqlite_store.save_live_activity_device_settings(
                device, frequent_updates=fu, activities_enabled=ae, supports_broadcast=sb, ios_version=ver,
            )
        except Exception as exc:
            logger.warning(f"[notifications] failed to save LA settings to SQLite: {exc}")
    return get_device_la_settings(device)


def get_device_la_settings(device_token: str) -> dict:
    """Effective Live Activity settings for a device (defaults for unknown values)."""
    device = _clean_token(device_token)
    with _device_la_settings_lock:
        stored = dict(_device_la_settings.get(device) or {})
    out = dict(_LA_SETTINGS_DEFAULTS)
    out.update({k: v for k, v in stored.items() if v is not None})
    return out


def _effective_la_priority(requested: Optional[int], has_alert: bool, device_token: str = "") -> Optional[int]:
    """Per-device ActivityKit priority policy.

    Alerts always go at 10. Otherwise the requested priority is used, except that
    devices with frequent updates disabled get 5 (budget-free) so routine
    refreshes aren't throttled. ``None`` lets the payload builder decide.
    """
    if has_alert:
        return 10
    if requested == 5:
        return 5
    if device_token and get_device_la_settings(device_token).get("frequent_updates") is False:
        return 5
    return requested


# ── Broadcast channels (iOS 18+) ──────────────────────────────────
# One channel per match and APNs environment. The app fetches the channel id
# (POST /api/live-activities/channel) and starts its activity with
# ``pushType: .channel(id)``; push-to-start can also subscribe new activities
# via ``input-push-channel``. Every update/end is then published once per
# channel instead of once per device.

_APNS_BROADCAST_HOSTS = {
    "production": "https://api.push.apple.com",
    "sandbox": "https://api.sandbox.push.apple.com",
}
_APNS_CHANNEL_HOSTS = {
    "production": "https://api-manage-broadcast.push.apple.com:2196",
    "sandbox": "https://api-manage-broadcast.sandbox.push.apple.com:2195",
}
_channel_create_lock = threading.Lock()


def broadcast_enabled() -> bool:
    import config
    return bool(getattr(config, "APNS_BROADCAST_ENABLED", False))


def _channel_env(environment: Optional[str | bool] = None) -> str:
    """Resolve "sandbox"/"production" for channel operations (server default when unknown)."""
    import config
    label = _env_label(environment)
    if label:
        return label
    return "sandbox" if getattr(config, "APNS_USE_SANDBOX", False) else "production"


def find_match_channels(match_id: str = "", sync_key: str = "", include_ended: bool = False) -> list[dict]:
    """Stored broadcast channels for a match (any environment), matched by match_id or sync_key."""
    idents = {i for i in (str(match_id or "").strip(), str(sync_key or "").strip()) if i}
    if not idents:
        return []
    with _la_channels_lock:
        out = []
        for rec in _la_channels.values():
            if not include_ended and rec.get("ended_at"):
                continue
            if idents & {rec.get("match_key", ""), rec.get("match_id", ""), rec.get("sync_key", "")}:
                out.append(dict(rec))
        return out


def _apns_channel_request(method: str, environment: str, channel_id: str = "", body: Optional[dict] = None) -> dict:
    """Call the APNs channel management API (create / read / delete)."""
    import config

    env = _channel_env(environment)
    result: dict[str, Any] = {"ok": False, "status_code": None, "channel_id": channel_id or None,
                              "reason": None, "environment": env, "error": None}
    jwt_token = _generate_apns_jwt()
    if not jwt_token:
        result["error"] = "Failed to generate APNs JWT"
        return result
    bundle = getattr(config, "APNS_BUNDLE_ID", "") or config.APNS_TOPIC
    url = f"{_APNS_CHANNEL_HOSTS[env]}/1/apps/{bundle}/channels"
    headers = {"authorization": f"bearer {jwt_token}"}
    if channel_id:
        headers["apns-channel-id"] = channel_id
    try:
        client = _get_apns_client()
        resp = client.request(method, url, headers=headers, json=body if body is not None else None)
        result["status_code"] = resp.status_code
        result["ok"] = resp.is_success
        if resp.headers.get("apns-channel-id"):
            result["channel_id"] = resp.headers.get("apns-channel-id")
        if resp.text:
            try:
                result["reason"] = resp.json().get("reason")
            except Exception:
                result["reason"] = resp.text[:100]
    except Exception as exc:
        result["error"] = str(exc)
    if not result["ok"]:
        logger.warning(
            "APNs channel %s failed: status=%s reason=%s env=%s error=%s",
            method, result["status_code"], result["reason"], env, result["error"],
        )
    return result


def get_or_create_match_channel(
    match_id: str = "",
    sync_key: str = "",
    competition: str = "",
    environment: Optional[str | bool] = None,
) -> dict:
    """Return the broadcast channel for a match in an environment, creating it on first use."""
    import config

    if not broadcast_enabled():
        return {"ok": False, "error": "broadcast channels disabled (APNS_BROADCAST_ENABLED=0)"}
    mid = str(match_id or "").strip()
    skey = str(sync_key or "").strip()
    if not mid and not skey:
        return {"ok": False, "error": "match_id or sync_key required"}
    env = _channel_env(environment)

    def _existing() -> Optional[dict]:
        for rec in find_match_channels(mid, skey, include_ended=True):
            if rec.get("environment") == env:
                return rec
        return None

    rec = _existing()
    if rec:
        if mid and not rec.get("match_id"):
            _attach_channel_match_id(rec, mid)
        return {"ok": not rec.get("ended_at"), "channel_id": rec["channel_id"], "environment": env,
                "created": False, "ended": bool(rec.get("ended_at"))}

    with _channel_create_lock:
        rec = _existing()
        if rec:
            return {"ok": not rec.get("ended_at"), "channel_id": rec["channel_id"], "environment": env,
                    "created": False, "ended": bool(rec.get("ended_at"))}
        with _la_channels_lock:
            active = sum(1 for r in _la_channels.values() if r.get("environment") == env and not r.get("ended_at"))
        if active >= int(getattr(config, "APNS_BROADCAST_MAX_ACTIVE_CHANNELS", 1000)):
            return {"ok": False, "error": "active channel limit reached", "environment": env}
        res = _apns_channel_request(
            "POST", env,
            body={"message-storage-policy": int(getattr(config, "APNS_BROADCAST_STORAGE_POLICY", 0)),
                  "push-type": "LiveActivity"},
        )
        if not res.get("ok") or not res.get("channel_id"):
            return {"ok": False, "error": res.get("error") or res.get("reason") or "channel create failed",
                    "status_code": res.get("status_code"), "environment": env}
        match_key = skey or mid
        rec = {
            "match_key": match_key, "environment": env, "channel_id": res["channel_id"],
            "match_id": mid, "sync_key": skey, "competition": normalize_live_competition(competition),
            "created_at": datetime.now(timezone.utc).isoformat(), "ended_at": "",
        }
        with _la_channels_lock:
            _la_channels[f"{match_key}|{env}"] = rec
        if sqlite_store is not None and hasattr(sqlite_store, "save_live_activity_channel"):
            try:
                sqlite_store.save_live_activity_channel(
                    match_key, env, rec["channel_id"], match_id=mid, sync_key=skey, competition=rec["competition"],
                )
            except Exception as exc:
                logger.warning(f"[notifications] failed to save channel to SQLite: {exc}")
        return {"ok": True, "channel_id": rec["channel_id"], "environment": env, "created": True, "ended": False}


def _attach_channel_match_id(rec: dict, match_id: str) -> None:
    with _la_channels_lock:
        live = _la_channels.get(f"{rec['match_key']}|{rec['environment']}")
        if live is not None:
            live["match_id"] = match_id
    if sqlite_store is not None and hasattr(sqlite_store, "update_live_activity_channel"):
        try:
            sqlite_store.update_live_activity_channel(rec["match_key"], rec["environment"], match_id=match_id)
        except Exception as exc:
            logger.debug(f"[notifications] failed to attach match_id to channel: {exc}")


def mark_match_channels_ended(match_id: str = "", sync_key: str = "") -> int:
    """Stamp ``ended_at`` on a match's channels (deleted later by cleanup_expired_channels)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    marked = 0
    for rec in find_match_channels(match_id, sync_key):
        with _la_channels_lock:
            live = _la_channels.get(f"{rec['match_key']}|{rec['environment']}")
            if live is not None and not live.get("ended_at"):
                live["ended_at"] = now_iso
                marked += 1
        if sqlite_store is not None and hasattr(sqlite_store, "update_live_activity_channel"):
            try:
                sqlite_store.update_live_activity_channel(rec["match_key"], rec["environment"], ended_at=now_iso)
            except Exception as exc:
                logger.debug(f"[notifications] failed to mark channel ended: {exc}")
    return marked


def _drop_channel_record(rec: dict) -> None:
    with _la_channels_lock:
        _la_channels.pop(f"{rec['match_key']}|{rec['environment']}", None)
    if sqlite_store is not None and hasattr(sqlite_store, "delete_live_activity_channel"):
        try:
            sqlite_store.delete_live_activity_channel(rec["match_key"], rec["environment"])
        except Exception as exc:
            logger.debug(f"[notifications] failed to delete channel row: {exc}")


def cleanup_expired_channels(now_ts: Optional[float] = None) -> int:
    """Delete APNs channels whose match ended long enough ago (or that are > 36 h old)."""
    import config

    now = float(now_ts if now_ts is not None else time.time())
    ttl = int(getattr(config, "APNS_BROADCAST_DELETE_AFTER_END_SECONDS", 3600))
    with _la_channels_lock:
        records = [dict(r) for r in _la_channels.values()]
    deleted = 0
    for rec in records:
        due = False
        for field, limit in (("ended_at", ttl), ("created_at", 36 * 3600)):
            raw = rec.get(field)
            if not raw:
                continue
            try:
                ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if now - ts >= limit:
                due = True
        if not due:
            continue
        res = _apns_channel_request("DELETE", rec["environment"], channel_id=rec["channel_id"])
        # 404/410: already gone on Apple's side — drop our record too.
        if res.get("ok") or res.get("status_code") in (404, 410):
            _drop_channel_record(rec)
            deleted += 1
    return deleted


def apns_broadcast_send(channel_id: str, payload: dict, environment: str, priority: int = 10) -> dict:
    """Publish one ActivityKit payload to every device subscribed to a broadcast channel."""
    import config

    env = _channel_env(environment)
    bundle = getattr(config, "APNS_BUNDLE_ID", "") or config.APNS_TOPIC
    endpoint = f"{_APNS_BROADCAST_HOSTS[env]}/4/broadcasts/apps/{bundle}"
    result: dict[str, Any] = {
        "ok": False, "status_code": None, "apns_id": None, "reason": None, "endpoint": endpoint,
        "push_type": "liveactivity-broadcast", "sandbox": env == "sandbox", "error": None,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(), "priority": priority,
    }
    jwt_token = _generate_apns_jwt()
    if not jwt_token:
        result["error"] = "Failed to generate APNs JWT"
        _record_apns_log(result, channel_id)
        return result
    # No Message Stored channels (policy 0) reject non-zero expirations.
    policy = int(getattr(config, "APNS_BROADCAST_STORAGE_POLICY", 0))
    expiration = "0" if policy == 0 else str(int(time.time()) + 3600)
    headers = {
        "apns-push-type": "liveactivity",
        "apns-channel-id": channel_id,
        "apns-expiration": expiration,
        "apns-priority": str(priority if priority in (1, 5, 10) else 10),
        "authorization": f"bearer {jwt_token}",
    }
    try:
        client = _get_apns_client()
        resp = client.post(endpoint, json=payload, headers=headers)
        result["status_code"] = resp.status_code
        result["ok"] = resp.is_success
        result["apns_id"] = resp.headers.get("apns-unique-id") or resp.headers.get("apns-request-id")
        if resp.text:
            try:
                result["reason"] = resp.json().get("reason")
            except Exception:
                result["reason"] = resp.text[:100]
        if not result["ok"]:
            logger.warning(
                "APNs broadcast failed: status=%s reason=%s env=%s channel=%s...",
                result["status_code"], result["reason"], env, channel_id[:12],
            )
            # The channel no longer exists on Apple's side — forget it.
            if result["status_code"] in (404, 410):
                for rec in list(_la_channels.values()):
                    if rec.get("channel_id") == channel_id:
                        _drop_channel_record(rec)
    except Exception as exc:
        result["error"] = str(exc)
        logger.warning("APNs broadcast error: %s", exc)
    _record_apns_log(result, channel_id)
    return result


def _queue_channel_broadcasts(
    match_id: str,
    sync_key: str,
    competition: str,
    content_state: dict,
    aps_event: str,
    alert: Optional[dict],
    priority: Optional[int],
    stale_date: Optional[int] = None,
    dismissal_date: Optional[int] = None,
) -> int:
    """Queue one broadcast per active channel of a match. Returns channels queued."""
    if not broadcast_enabled():
        return 0
    channels = find_match_channels(match_id, sync_key)
    for rec in channels:
        _apns_notification_queue.append({
            "type": "broadcast",
            "channel_id": rec["channel_id"],
            "environment": rec["environment"],
            "token": rec["channel_id"],
            "content_state": content_state,
            "event": aps_event,
            "alert": alert,
            "priority": 10 if alert else priority,
            "stale_date": stale_date,
            "dismissal_date": dismissal_date,
            "match_id": match_id,
            "sync_key": sync_key,
            "competition": normalize_live_competition(competition),
        })
    return len(channels)


def register_channel_activity(device_token: str, match_id: str, sync_key: str = "", competition: str = "") -> bool:
    """Record that a device runs a channel-based Live Activity for a match (no per-activity token).

    Needed so the device's regular pushes are suppressed during the activity and
    push-to-start doesn't start a second one.
    """
    device = _clean_token(device_token)
    mid = normalize_match_ident(match_id)
    skey = normalize_match_ident(sync_key)
    if not device or not (mid or skey):
        return False
    with _live_activities_lock:
        for ident in (mid, skey):
            if ident:
                _channel_activity_devices.setdefault(ident, set()).add(device)
    if sqlite_store is not None:
        try:
            sqlite_store.save_notification_sub(
                device, "la_channel", mid or skey, competition=competition, sync_key=skey, device_token=device,
            )
        except Exception as exc:
            logger.warning(f"[notifications] failed to save channel activity to SQLite: {exc}")
    return True


def unregister_channel_activity(device_token: str, match_id: str = "", sync_key: str = "") -> bool:
    """Remove a device's channel-based Live Activity marker for a match."""
    device = _clean_token(device_token)
    idents = [i for i in (normalize_match_ident(match_id), normalize_match_ident(sync_key)) if i]
    if not device or not idents:
        return False
    removed = False
    with _live_activities_lock:
        for ident in idents:
            devs = _channel_activity_devices.get(ident)
            if devs and device in devs:
                devs.discard(device)
                removed = True
                if not devs:
                    del _channel_activity_devices[ident]
    if sqlite_store is not None:
        try:
            for ident in idents:
                sqlite_store.remove_notification_sub(device, "la_channel", ident)
        except Exception as exc:
            logger.warning(f"[notifications] failed to remove channel activity from SQLite: {exc}")
    return removed


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

    if not kid or not iss or not key_path:
        return None

    try:
        if not os.path.exists(key_path):
            return None
        with open(key_path, "r") as f:
            private_key = f.read()
    except Exception as exc:
        logger.warning(f"Could not read APNs key file at {key_path}: {exc}")
        return None

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
    priority: Optional[int] = None,
    collapse_id: Optional[str] = None,
) -> dict:
    """Send an APNs push notification and return detailed status, headers, and diagnostic hints.
    Automatically detects and handles Sandbox (Xcode / Simulator) vs Production (TestFlight / App Store) tokens.

    ``priority``: 10 (immediate, counts toward the Live Activity budget) or 5
    (low priority, budget-free). Defaults to 10.
    ``collapse_id``: optional ``apns-collapse-id`` (<= 64 bytes; longer ids are hashed).
    """
    import config

    jwt_token = _generate_apns_jwt()
    clean_token = _clean_token(push_token)

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
    if live_activity and eff_topic and not eff_topic.endswith(".push-type.liveactivity"):
        eff_topic = f"{eff_topic}.push-type.liveactivity"

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

    eff_priority = 5 if priority == 5 else 10
    headers = {
        "apns-push-type": "liveactivity" if live_activity else "alert",
        "apns-topic": eff_topic,
        "apns-priority": str(eff_priority),
        "authorization": f"bearer {jwt_token}",
    }
    result["priority"] = eff_priority
    if live_activity:
        headers["apns-expiration"] = "0"
    if collapse_id:
        cid = str(collapse_id)
        if len(cid.encode("utf-8")) > 64:
            import hashlib
            cid = hashlib.sha256(cid.encode("utf-8")).hexdigest()[:64]
        headers["apns-collapse-id"] = cid

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
                    # Persist so restarts don't pay a failed request per token again.
                    if sqlite_store is not None and hasattr(sqlite_store, "update_notification_token_environment"):
                        try:
                            sqlite_store.update_notification_token_environment(
                                clean_token, "sandbox" if use_sandbox else "production"
                            )
                        except Exception as exc:
                            logger.debug(f"[notifications] failed to persist token environment: {exc}")
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
            # Auto-prune dead/expired tokens (APNs 410 Unregistered or fatal BadDeviceToken on real hex tokens)
            is_unregistered = result["status_code"] == 410
            is_fatal_bad_token = (
                result["status_code"] == 400
                and result.get("reason") == "BadDeviceToken"
                and len(clean_token) == 64
                and all(c in "0123456789abcdefABCDEF" for c in clean_token)
            )
            if is_unregistered or is_fatal_bad_token:
                prune_dead_token(clean_token, is_live_activity=live_activity)

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


def _apns_send(
    push_token: str,
    payload: dict,
    topic: str,
    live_activity: bool = False,
    priority: Optional[int] = None,
    collapse_id: Optional[str] = None,
) -> bool:
    res = apns_send_detailed(
        push_token, payload, topic=topic, live_activity=live_activity,
        priority=priority, collapse_id=collapse_id,
    )
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
    with _push_to_start_lock:
        pts_count = len(_push_to_start_tokens)
    with _la_channels_lock:
        channels_active = sum(1 for r in _la_channels.values() if not r.get("ended_at"))
        channels_total = len(_la_channels)
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
            "push_to_start_devices_count": pts_count,
            "broadcast_enabled": broadcast_enabled(),
            "broadcast_channels_active": channels_active,
            "broadcast_channels_total": channels_total,
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
            logger.exception("[notifications] APNs worker failed while draining queue")
        _apns_queue_event.wait(timeout=1.0)
        _apns_queue_event.clear()


def build_live_activity_aps(entry: dict) -> tuple[dict, int]:
    """Build the ActivityKit ``{"aps": ...}`` payload and APNs priority for a queued LA entry.

    - ``aps.event`` is always one of start/update/end; any other value (legacy
      callers passing UI labels like "goal") is sent as "update", with the label
      preserved in ``content-state.event``.
    - ``alert.sound`` lives inside the alert dict (Apple's documented shape).
    - start: adds ``attributes-type`` / ``attributes`` (+ ``input-push-token`` on iOS 18+).
    - end: adds ``dismissal-date`` (default: 4 hours, the system maximum).
    - Priority: 10 for start/end/alerts or when explicitly requested, else 5.
    """
    import config

    raw_event = str(entry.get("event") or "update").strip().lower()
    aps_event = raw_event if raw_event in LA_APS_EVENTS else "update"
    state = dict(entry.get("content_state") or {})
    if raw_event not in LA_APS_EVENTS:
        state.setdefault("event", raw_event)

    aps: dict[str, Any] = {
        "timestamp": int(time.time()),
        "event": aps_event,
        "content-state": state,
    }
    alert = entry.get("alert")
    if alert:
        alert_payload = dict(alert)
        alert_payload.setdefault("sound", entry.get("sound") or "default")
        aps["alert"] = alert_payload
    stale = entry.get("stale_date")
    if stale:
        aps["stale-date"] = int(stale)
    relevance = entry.get("relevance_score")
    if relevance is not None:
        aps["relevance-score"] = relevance
    if aps_event == "start":
        aps["attributes-type"] = getattr(config, "APNS_LA_ATTRIBUTES_TYPE", "MatchActivityAttributes")
        aps["attributes"] = dict(entry.get("attributes") or {})
        if entry.get("input_push_channel"):
            # iOS 18+: the new activity listens on the match's broadcast channel.
            aps["input-push-channel"] = entry["input_push_channel"]
        elif getattr(config, "APNS_LA_INPUT_PUSH_TOKEN", True):
            aps["input-push-token"] = 1
        if "alert" not in aps:
            aps["alert"] = {"title": "Match starting", "body": "", "sound": "default"}
    if aps_event == "end":
        # dismissal-date: when iOS removes the ended activity from the Lock Screen.
        # Default 4 hours (system maximum) so the final score stays glanceable.
        d_date = entry.get("dismissal_date")
        if d_date is None:
            d_date = int(time.time() + 14400)
        aps["dismissal-date"] = int(d_date)

    requested = entry.get("priority")
    if requested in (1, 5, 10) and not (requested != 10 and "alert" in aps):
        priority = int(requested)
    elif aps_event in ("start", "end") or "alert" in aps:
        priority = 10
    else:
        priority = 5
    return {"aps": aps}, priority


def build_alert_payload(entry: dict) -> dict:
    """Build a regular alert push payload with deep-link metadata and per-match threading."""
    mid = str(entry.get("match_id") or "").strip()
    sync_key = str(entry.get("sync_key") or "").strip()
    aps: dict[str, Any] = {
        "alert": {
            "title": entry.get("title", ""),
            "body": entry.get("body", ""),
        },
        "sound": "default",
        "badge": entry.get("badge", 1),
    }
    thread = mid or sync_key
    if thread:
        aps["thread-id"] = f"match-{thread}"
    return {
        "aps": aps,
        "match_id": mid,
        "sync_key": sync_key,
        "competition": entry.get("competition", ""),
        "event": entry.get("event", "alert"),
    }


def _drain_queue() -> None:
    import config

    while _apns_notification_queue:
        entry = _apns_notification_queue.popleft()
        push_token = entry.get("token", "")
        if not push_token:
            continue

        if entry.get("type") == "broadcast":
            try:
                payload, priority = build_live_activity_aps(entry)
                if payload["aps"].get("event") == "start":
                    continue  # Apple: broadcast pushes can't start activities.
                apns_broadcast_send(
                    entry.get("channel_id", ""), payload, entry.get("environment", ""), priority=priority,
                )
            except Exception:
                logger.exception("[notifications] APNs broadcast raised for channel %s...",
                                 str(entry.get("channel_id", ""))[:12])
            continue

        is_la = entry.get("type") == "liveactivity"

        if is_la:
            topic = config.APNS_LIVE_ACTIVITY_TOPIC or ""
            payload, priority = build_live_activity_aps(entry)
            collapse_id = None
        else:
            topic = config.APNS_TOPIC or ""
            payload = build_alert_payload(entry)
            priority = 10
            collapse_id = entry.get("collapse_id") or None

        try:
            _apns_send(
                push_token, payload, topic, live_activity=is_la,
                priority=priority, collapse_id=collapse_id,
            )
        except Exception:
            logger.exception(
                "[notifications] APNs send raised for %s token %s...",
                "live-activity" if is_la else "alert",
                str(push_token)[:10],
            )


def send_live_activity_update(
    match_id: str,
    competition: str,
    content_state: dict,
    event: str = "update",
    alert: Optional[dict] = None,
    sync_key: str = "",
    priority: Optional[int] = None,
    stale_date: Optional[int] = None,
) -> int:
    """Queue a Live Activity content-state update for every registration of a match.

    APNs always receives ``aps.event = "update"``. A non-"update" ``event``
    argument (e.g. "goal", "halftime", "red card") is treated as the UI label
    and stored in ``content-state.event`` (unless the state already sets one).

    Delivery: one push per token-based activity (iOS 16.1+) plus one broadcast
    per active channel of the match (iOS 18+, when broadcast is enabled).

    When ``alert`` is passed (e.g. for goals or halftime), ActivityKit presents
    an alert banner and sound on the Lock Screen / Dynamic Island. Priority:
    alerts are always 10; otherwise ``priority`` (default 5), downgraded to 5
    for devices that disabled frequent updates. Returns pushes queued.
    """
    activities = for_match(match_id, competition, sync_key=sync_key)
    has_channels = broadcast_enabled() and bool(find_match_channels(match_id, sync_key))
    if not activities and not has_channels:
        return 0
    state = dict(content_state or {})
    label = str(event or "").strip()
    if label and label.lower() not in LA_APS_EVENTS:
        state.setdefault("event", label)
    state.setdefault("match_id", match_id)
    if sync_key:
        state.setdefault("sync_key", sync_key)
    state.setdefault("competition", normalize_live_competition(competition))
    for entry in activities:
        _apns_notification_queue.append({
            "type": "liveactivity",
            "token": entry["activity_token"],
            "content_state": state,
            "event": "update",
            "alert": alert,
            "priority": _effective_la_priority(priority, bool(alert), entry.get("device_token") or ""),
            "stale_date": stale_date,
            "match_id": match_id,
            "sync_key": sync_key,
            "competition": normalize_live_competition(competition),
        })
    channels = _queue_channel_broadcasts(
        match_id, sync_key, competition, state, "update", alert, priority, stale_date=stale_date,
    )
    _notify_apns_queue()
    return len(activities) + channels


def send_live_activity_end(
    match_id: str,
    competition: str,
    content_state: dict | None = None,
    alert: Optional[dict] = None,
    dismissal_date: Optional[int] = None,
    sync_key: str = "",
) -> int:
    """Queue an 'end' Live Activity push for a match with optional alert and dismissal date.

    Callers should pass the FULL final content-state (same schema as updates);
    Apple decodes it into the app's ContentState type and drops the push if
    required fields are missing. Ends token-based activities and publishes the
    end on the match's broadcast channels (which are then marked ended and
    deleted later by ``cleanup_expired_channels``).

    dismissal_date: Epoch timestamp (seconds) when iOS removes the ended activity from Lock Screen.
    Defaults to 4 hours in the future to allow users to view the final score.
    """
    activities = for_match(match_id, competition, sync_key=sync_key)
    has_channels = broadcast_enabled() and bool(find_match_channels(match_id, sync_key))
    if not activities and not has_channels:
        unregister_by_match(match_id, competition, sync_key=sync_key)
        mark_match_channels_ended(match_id, sync_key)
        return 0
    state = dict(content_state or {})
    state.setdefault("status", "FT")
    state.setdefault("event", "fulltime")
    state.setdefault("match_id", match_id)
    if sync_key:
        state.setdefault("sync_key", sync_key)
    state.setdefault("competition", normalize_live_competition(competition))
    for entry in activities:
        _apns_notification_queue.append({
            "type": "liveactivity",
            "token": entry["activity_token"],
            "content_state": state,
            "event": "end",
            "alert": alert,
            "dismissal_date": dismissal_date,
            "match_id": match_id,
            "sync_key": sync_key,
            "competition": normalize_live_competition(competition),
        })
    channels = _queue_channel_broadcasts(
        match_id, sync_key, competition, state, "end", alert, 10, dismissal_date=dismissal_date,
    )
    _notify_apns_queue()
    unregister_by_match(match_id, competition, sync_key=sync_key)
    mark_match_channels_ended(match_id, sync_key)
    return len(activities) + channels


# Preload existing subscriptions from SQLite store on module initialization
try:
    init_subscriptions_from_sqlite()
except Exception:
    pass
