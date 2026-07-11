"""Live Activity registration and data persistence for iOS 16.1+ widgets.

Stores push-token ↔ match mappings in a JSON file so the server can
send real-time score updates to active Live Activities.
"""
import json
import os
import threading

import config

_live_activities: list[dict] = []
_live_activities_lock = threading.Lock()


def _load():
    global _live_activities
    if not os.path.exists(config.LIVE_ACTIVITIES_FILE):
        _live_activities = []
        return
    try:
        with open(config.LIVE_ACTIVITIES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _live_activities = data if isinstance(data, list) else []
    except Exception:
        _live_activities = []


def _save():
    dirpath = os.path.dirname(config.LIVE_ACTIVITIES_FILE)
    if not os.path.exists(dirpath):
        os.makedirs(dirpath, exist_ok=True)
    with open(config.LIVE_ACTIVITIES_FILE, "w", encoding="utf-8") as f:
        json.dump(_live_activities, f, indent=2)


def register(activity_token: str, device_token: str, match_id: str, competition: str) -> bool:
    """Register a Live Activity push token for a match.

    Returns True if a new registration was added (duplicates are skipped).
    """
    if not activity_token or not match_id:
        return False
    _load()
    with _live_activities_lock:
        for entry in _live_activities:
            if entry.get("activity_token") == activity_token:
                return False
        _live_activities.append({
            "activity_token": activity_token,
            "device_token": device_token,
            "match_id": match_id,
            "competition": competition,
        })
        _save()
        return True


def unregister(activity_token: str) -> bool:
    """Remove a Live Activity registration."""
    _load()
    with _live_activities_lock:
        before = len(_live_activities)
        _live_activities[:] = [e for e in _live_activities if e.get("activity_token") != activity_token]
        if len(_live_activities) < before:
            _save()
            return True
        return False


def unregister_by_match(match_id: str, competition: str) -> int:
    """Remove all Live Activity registrations for a given match.

    Returns the number of registrations removed.
    """
    _load()
    with _live_activities_lock:
        before = len(_live_activities)
        _live_activities[:] = [
            e for e in _live_activities
            if not (e.get("match_id") == match_id and e.get("competition") == competition)
        ]
        removed = before - len(_live_activities)
        if removed:
            _save()
        return removed


def for_match(match_id: str, competition: str) -> list[dict]:
    """Return all Live Activity registrations for a specific match."""
    _load()
    with _live_activities_lock:
        return [
            dict(e) for e in _live_activities
            if e.get("match_id") == match_id and e.get("competition") == competition
        ]


def all_activities() -> list[dict]:
    """Return all Live Activity registrations (snapshot)."""
    _load()
    with _live_activities_lock:
        return [dict(e) for e in _live_activities]


def competition_count(competition: str) -> int:
    """Number of active Live Activities for a given competition."""
    _load()
    with _live_activities_lock:
        return sum(1 for e in _live_activities if e.get("competition") == competition)


# Initialize on import
_load()
