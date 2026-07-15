"""Apple Push Notification Service (APNs) worker and token generation."""
import logging
import time
import threading
from collections import deque
from datetime import datetime, timezone

import httpx

import config

LOG = logging.getLogger("notifications")

_notifications = deque(maxlen=100)
device_tokens: set = set()
ios_device_tokens: set[str] = set()

_apns_client_lock = threading.Lock()
_apns_token_cache: str | None = None
_apns_token_expires: float = 0.0
_apns_notification_queue: deque[dict] = deque(maxlen=500)
_apns_worker_started = False


def _generate_apns_token() -> str | None:
    """Generate a JWT token for APNs token-based authentication.

    Returns the encoded JWT string or None if config is incomplete.
    """
    global _apns_token_cache, _apns_token_expires
    now = time.time()
    if _apns_token_cache and now < _apns_token_expires:
        return _apns_token_cache
    if not config.APNS_KEY_FILE or not config.APNS_KEY_ID or not config.APNS_TEAM_ID:
        LOG.warning("APNs config incomplete: KEY_FILE=%s KEY_ID=%s TEAM_ID=%s",
                     config.APNS_KEY_FILE, config.APNS_KEY_ID, config.APNS_TEAM_ID)
        return None
    try:
        with open(config.APNS_KEY_FILE, "rb") as f:
            key_data = f.read()
    except Exception as e:
        LOG.error("Failed to read APNs key file %s: %s", config.APNS_KEY_FILE, e)
        return None
    try:
        import jwt as pyjwt
        issued_at = int(now)
        expiry = issued_at + 3600
        headers = {"kid": config.APNS_KEY_ID}
        payload = {
            "iss": config.APNS_TEAM_ID,
            "iat": issued_at,
        }
        token = pyjwt.encode(payload, key_data, algorithm="ES256", headers=headers)
        _apns_token_cache = token
        _apns_token_expires = expiry - 60
        return token
    except Exception as e:
        LOG.error("APNs JWT generation failed: %s", e)
        return None


def _send_apns_notification(device_token: str, title: str, body: str, badge: int = 0) -> bool:
    """Send a push notification to a single iOS device via APNs.

    Uses HTTP/2 (required by Apple). Returns True on success.
    """
    token = _generate_apns_token()
    if not token:
        return False
    apns_host = "api.sandbox.push.apple.com" if config.APNS_USE_SANDBOX else "api.push.apple.com"
    url = f"https://{apns_host}/3/device/{device_token}"
    notification = {
        "aps": {
            "alert": {"title": title, "body": body},
            "badge": badge,
            "sound": "default",
        }
    }
    try:
        with httpx.Client(http2=True, timeout=10.0) as client:
            resp = client.post(
                url,
                json=notification,
                headers={
                    "authorization": f"bearer {token}",
                    "apns-topic": config.APNS_BUNDLE_ID,
                    "apns-push-type": "alert",
                },
            )
            if resp.status_code != 200:
                LOG.warning("APNs alert push failed: HTTP %s %s", resp.status_code, resp.text[:200])
            return resp.status_code == 200
    except Exception as e:
        LOG.error("APNs alert push error: %s", e)
        return False


# ── Live Activity pushes (iOS 16.1+) ─────────────────────────────────


def _send_live_activity_push(
    activity_token: str,
    event: str,
    content_state: dict,
    timestamp: int | None = None,
) -> bool:
    """Send a Live Activity push (update or end) to a single activity token.

    Parameters
    ----------
    activity_token : str
        The push token received from the Live Activity instance.
    event : ``"update"`` or ``"end"``
    content_state : dict
        The widget's ``ContentState`` fields (home_score, away_score, …).
    timestamp : int, optional
        Epoch seconds for ``aps.timestamp``. Defaults to current time.

    ``event="end"`` signals Apple to dismiss the Live Activity after the
    content state is displayed.
    """
    token = _generate_apns_token()
    if not token:
        return False
    apns_host = "api.sandbox.push.apple.com" if config.APNS_USE_SANDBOX else "api.push.apple.com"
    url = f"https://{apns_host}/3/device/{activity_token}"
    if timestamp is None:
        timestamp = int(time.time())
    payload = {
        "aps": {
            "timestamp": timestamp,
            "event": event,
            "content-state": content_state,
        }
    }
    try:
        with httpx.Client(http2=True, timeout=10.0) as client:
            resp = client.post(
                url,
                json=payload,
                headers={
                    "authorization": f"bearer {token}",
                    "apns-topic": f"{config.APNS_BUNDLE_ID}.push-type.liveactivity",
                    "apns-push-type": "liveactivity",
                },
            )
            if resp.status_code != 200:
                LOG.warning("APNs liveactivity push failed: HTTP %s %s", resp.status_code, resp.text[:200])
            return resp.status_code == 200
    except Exception as e:
        LOG.error("APNs liveactivity push error: %s", e)
        return False


def send_live_activity_update(activity_token: str, content_state: dict) -> bool:
    """Update a Live Activity with new content state data."""
    return _send_live_activity_push(activity_token, "update", content_state)


def send_live_activity_end(activity_token: str, content_state: dict | None = None) -> bool:
    """Dismiss a Live Activity (iOS removes the widget)."""
    return _send_live_activity_push(activity_token, "end", content_state or {})


# ── Worker ───────────────────────────────────────────────────────────


def _apns_worker():
    """Background thread that drains the notification queue to APNs."""
    while True:
        try:
            item = _apns_notification_queue.popleft() if _apns_notification_queue else None
        except IndexError:
            item = None
        if item:
            ptype = item.get("type", "alert")
            if ptype == "liveactivity":
                ok = _send_live_activity_push(
                    activity_token=item["token"],
                    event=item.get("event", "update"),
                    content_state=item.get("content_state", {}),
                    timestamp=item.get("timestamp"),
                )
            else:
                ok = _send_apns_notification(
                    device_token=item["token"],
                    title=item["title"],
                    body=item["body"],
                    badge=item.get("badge", 0),
                )
            if not ok:
                LOG.warning("APNs worker: failed to send %s push", ptype)
        else:
            time.sleep(2.0)


def start_apns_worker():
    """Start the APNs background worker once.

    The worker drains the notification queue for Live Activities even when
    APNs credentials are missing — items simply log a warning instead of
    failing silently.
    """
    global _apns_worker_started
    if _apns_worker_started:
        return
    has_creds = bool(config.APNS_KEY_FILE and config.APNS_KEY_ID and config.APNS_TEAM_ID and config.APNS_BUNDLE_ID)
    if not has_creds:
        pass
    _apns_worker_started = True
    t = threading.Thread(target=_apns_worker, daemon=True, name="apns-worker")
    t.start()
