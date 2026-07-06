"""Apple Push Notification Service (APNs) worker and token generation."""
import time
import threading
from collections import deque

import httpx

import config

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
        return None
    try:
        with open(config.APNS_KEY_FILE, "rb") as f:
            key_data = f.read()
        import jwt as pyjwt
        issued_at = int(now)
        expiry = issued_at + 3600  # tokens valid for 1 hour
        headers = {"kid": config.APNS_KEY_ID}
        payload = {
            "iss": config.APNS_TEAM_ID,
            "iat": issued_at,
        }
        token = pyjwt.encode(payload, key_data, algorithm="ES256", headers=headers)
        _apns_token_cache = token
        _apns_token_expires = expiry - 60  # refresh 1 min early
        return token
    except Exception:
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
            return resp.status_code == 200
    except Exception:
        return False


def _apns_worker():
    """Background thread that drains the notification queue to APNs."""
    while True:
        try:
            item = _apns_notification_queue.popleft() if _apns_notification_queue else None
        except IndexError:
            item = None
        if item:
            _send_apns_notification(
                device_token=item["token"],
                title=item["title"],
                body=item["body"],
                badge=item.get("badge", 0),
            )
        else:
            time.sleep(2.0)


def start_apns_worker():
    """Start the APNs background worker once."""
    global _apns_worker_started
    if _apns_worker_started:
        return
    if not config.APNS_KEY_FILE or not config.APNS_KEY_ID or not config.APNS_TEAM_ID or not config.APNS_BUNDLE_ID:
        return
    _apns_worker_started = True
    t = threading.Thread(target=_apns_worker, daemon=True, name="apns-worker")
    t.start()
