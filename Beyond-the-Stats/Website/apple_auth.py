"""Apple Sign-In authentication and user management.

Minimal personal-data storage:
  - sub (Apple opaque user ID) — primary key
  - tier, tier_expires_at, tier_product_id
  - credits, credits_monthly_cap, credits_granted_until
  - teams_followed, leagues_followed (string lists)
  - widget_settings, live_activity_settings (JSON objects)
  - processed_transactions (for receipt replay prevention)

No email, name, or any PII is stored.
"""
import json
import os
import threading
import time
from base64 import urlsafe_b64decode

import jwt
import requests

import config

_USERS_LOCK = threading.Lock()
_APPLE_KEYS_LOCK = threading.Lock()

# ── Constants ────────────────────────────────────────────────────

APPLE_PUBLIC_KEYS_URL = "https://appleid.apple.com/auth/keys"
APP_STORE_PRODUCTION_URL = "https://buy.itunes.apple.com/verifyReceipt"
APP_STORE_SANDBOX_URL = "https://sandbox.itunes.apple.com/verifyReceipt"
SESSION_TTL_DAYS = 30

_APPLE_KEYS_CACHE: list[dict] = []
_APPLE_KEYS_CACHE_AT: float = 0
_APPLE_KEYS_CACHE_TTL: float = 86400  # 24 h

USERS_FILE = os.path.join(config.PROJECT_DIR, "Data", "users.json")
ORIGINAL_TX_MAP_FILE = os.path.join(config.PROJECT_DIR, "Data", "original_tx_sub_map.json")

# Apple server-to-server notification V2: Apple root certificate URL
APPLE_ROOT_CA_URL = "https://www.apple.com/certificateauthority/AppleRootCA-G3.cer"

_ORIGINAL_TX_MAP_LOCK = threading.Lock()

_APPLE_NOTIFICATION_KEYS_LOCK = threading.Lock()
_APPLE_NOTIFICATION_KEYS_CACHE: list[dict] = []
_APPLE_NOTIFICATION_KEYS_CACHE_AT: float = 0
_APPLE_NOTIFICATION_KEYS_CACHE_TTL: float = 86400  # 24 h

# ── Product definitions ───────────────────────────────────────────
# Each product ID in your App Store Connect configuration.

# Maps product_id → {tier, credits_monthly_cap}
SUBSCRIPTION_PRODUCTS: dict[str, dict] = {
    "com.beyondthestats.plus.monthly": {
        "tier": "plus",
        "credits_monthly_cap": 3,
        "price": 2.99,
    },
    "com.beyondthestats.plus.yearly": {
        "tier": "plus",
        "credits_monthly_cap": 3,
        "price": 29.99,
    },
    "com.beyondthestats.prediction.monthly": {
        "tier": "prediction",
        "credits_monthly_cap": 15,
        "price": 6.99,
    },
    "com.beyondthestats.prediction.yearly": {
        "tier": "prediction",
        "credits_monthly_cap": 15,
        "price": 69.99,
    },
    "com.beyondthestats.premium.monthly": {
        "tier": "premium",
        "credits_monthly_cap": 1000,
        "price": 12.99,
    },
    "com.beyondthestats.premium.yearly": {
        "tier": "premium",
        "credits_monthly_cap": 1000,
        "price": 129.99,
    },
}

# Maps product_id → {credits, price}
CONSUMABLE_PRODUCTS: dict[str, dict] = {
    "com.beyondthestats.credit.1": {"credits": 1, "price": 1.99},
    "com.beyondthestats.credit.3": {"credits": 3, "price": 4.99},
    "com.beyondthestats.credit.10": {"credits": 10, "price": 9.99},
}

# Fields stored per user (no PII).
_USER_FIELDS = frozenset({
    "sub", "tier", "tier_expires_at", "tier_product_id",
    "credits", "credits_purchased", "credits_monthly_cap", "credits_granted_until",
    "total_credits_purchased",
    "teams_followed", "leagues_followed",
    "widget_settings", "live_activity_settings",
    "processed_transactions",
    "created_at", "updated_at",
})

# ═══════════════════════════════════════════════════════════════════
#  Apple identity token verification
# ═══════════════════════════════════════════════════════════════════


def _fetch_apple_public_keys() -> list[dict]:
    global _APPLE_KEYS_CACHE, _APPLE_KEYS_CACHE_AT
    now = time.time()
    with _APPLE_KEYS_LOCK:
        if _APPLE_KEYS_CACHE and now - _APPLE_KEYS_CACHE_AT < _APPLE_KEYS_CACHE_TTL:
            return _APPLE_KEYS_CACHE
        try:
            resp = requests.get(APPLE_PUBLIC_KEYS_URL, timeout=10)
            resp.raise_for_status()
            _APPLE_KEYS_CACHE = (resp.json().get("keys") or [])
            _APPLE_KEYS_CACHE_AT = now
        except Exception:
            pass
        return _APPLE_KEYS_CACHE


def _find_apple_key(kid: str) -> dict | None:
    for key in _fetch_apple_public_keys():
        if key.get("kid") == kid:
            return key
    return None


def verify_apple_identity_token(identity_token: str, client_id: str | None = None) -> dict | None:
    """Verify an Apple identity token, return the payload dict or None."""
    client_id = client_id or os.environ.get("APPLE_CLIENT_ID", "")
    if not client_id:
        return None
    try:
        header = jwt.get_unverified_header(identity_token)
        kid = header.get("kid")
        alg = header.get("alg", "RS256")
        if not kid:
            return None
        apple_key = _find_apple_key(kid)
        if not apple_key:
            return None
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(apple_key))
        payload = jwt.decode(
            identity_token,
            public_key,
            algorithms=[alg],
            audience=client_id,
            issuer="https://appleid.apple.com",
        )
        return payload
    except jwt.PyJWTError:
        return None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════
#  App Store receipt verification
# ═══════════════════════════════════════════════════════════════════


def _apple_store_shared_secret() -> str:
    return os.environ.get("APP_STORE_SHARED_SECRET", "").strip()


# ── Original transaction ID → sub mapping ──────────────────────


def _load_original_tx_map() -> dict[str, str]:
    if not os.path.exists(ORIGINAL_TX_MAP_FILE):
        return {}
    try:
        with open(ORIGINAL_TX_MAP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_original_tx_map(mapping: dict[str, str]) -> None:
    os.makedirs(os.path.dirname(ORIGINAL_TX_MAP_FILE), exist_ok=True)
    with open(ORIGINAL_TX_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)


# ── Server-to-server notification verification ─────────────────


def _fetch_apple_notification_keys() -> list[dict]:
    """Fetch Apple public keys used for signing server notifications.

    Apple uses the same JWKS endpoint as identity tokens, but
    notifications may also use x5c certificate chains.  This
    function caches the JWKS response for 24 h.
    """
    global _APPLE_NOTIFICATION_KEYS_CACHE, _APPLE_NOTIFICATION_KEYS_CACHE_AT
    now = time.time()
    with _APPLE_NOTIFICATION_KEYS_LOCK:
        if _APPLE_NOTIFICATION_KEYS_CACHE and now - _APPLE_NOTIFICATION_KEYS_CACHE_AT < _APPLE_NOTIFICATION_KEYS_CACHE_TTL:
            return _APPLE_NOTIFICATION_KEYS_CACHE
        try:
            resp = requests.get(APPLE_PUBLIC_KEYS_URL, timeout=10)
            resp.raise_for_status()
            _APPLE_NOTIFICATION_KEYS_CACHE = (resp.json().get("keys") or [])
            _APPLE_NOTIFICATION_KEYS_CACHE_AT = now
        except Exception:
            pass
        return _APPLE_NOTIFICATION_KEYS_CACHE


def verify_apple_signed_payload(signed_payload: str) -> dict | None:
    """Verify an Apple JWS signed payload (server-to-server V2).

    Supports verification via ``kid`` (JWKS) or ``x5c`` (certificate
    chain).  Returns the decoded payload dict, or None on failure.
    """
    try:
        parts = signed_payload.split(".")
        if len(parts) != 3:
            return None

        header_b64, payload_b64, signature_b64 = parts

        import base64 as _b64

        def _b64d(s: str) -> bytes:
            s = s + "=" * (4 - len(s) % 4) if len(s) % 4 else s
            return _b64.urlsafe_b64decode(s)

        header_json = _b64d(header_b64).decode("utf-8")
        header = json.loads(header_json)
        alg = header.get("alg", "ES256")

        # Try JWKS-based verification first (kid in header)
        kid = header.get("kid")
        if kid:
            from cryptography.hazmat.primitives.asymmetric import ec, rsa
            from cryptography.hazmat.primitives import serialization

            for jwk_key in _fetch_apple_notification_keys():
                if jwk_key.get("kid") != kid:
                    continue
                if alg.startswith("ES"):
                    crv = jwk_key.get("crv", "")
                    x_bytes = _b64d(jwk_key["x"])
                    y_bytes = _b64d(jwk_key["y"])
                    num_size = len(x_bytes)
                    x_int = int.from_bytes(x_bytes, "big")
                    y_int = int.from_bytes(y_bytes, "big")
                    if crv == "P-256":
                        from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicNumbers, SECP256R1
                        pub_key = EllipticCurvePublicNumbers(x_int, y_int, SECP256R1()).public_key()
                    elif crv == "P-384":
                        from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicNumbers, SECP384R1
                        pub_key = EllipticCurvePublicNumbers(x_int, y_int, SECP384R1()).public_key()
                    else:
                        continue
                elif alg.startswith("RS") or alg.startswith("PS"):
                    n_bytes = _b64d(jwk_key["n"])
                    e_bytes = _b64d(jwk_key["e"])
                    n_int = int.from_bytes(n_bytes, "big")
                    e_int = int.from_bytes(e_bytes, "big")
                    pub_key = rsa.RSAPublicNumbers(e_int, n_int).public_key(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
                else:
                    continue

                message = (header_b64 + "." + payload_b64).encode("utf-8")
                sig = _b64d(signature_b64)
                if alg == "ES256":
                    from cryptography.hazmat.primitives import hashes
                    pub_key.verify(sig, message, ec.ECDSA(hashes.SHA256()))
                elif alg == "ES384":
                    from cryptography.hazmat.primitives import hashes
                    pub_key.verify(sig, message, ec.ECDSA(hashes.SHA384()))
                elif alg in ("RS256", "PS256"):
                    from cryptography.hazmat.primitives import hashes, padding as _pad
                    pub_key.verify(sig, message, _pad.PKCS1v15(), hashes.SHA256())
                else:
                    continue

                payload = json.loads(_b64d(payload_b64).decode("utf-8"))
                return payload

        # Fallback: x5c certificate chain verification
        x5c = header.get("x5c", [])
        if x5c:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import ec

            cert_der = _b64.b64decode(x5c[0])
            cert = x509.load_der_x509_certificate(cert_der)
            pub_key = cert.public_key()

            message = (header_b64 + "." + payload_b64).encode("utf-8")
            sig = _b64d(signature_b64)
            hash_algo = hashes.SHA256() if "256" in alg else hashes.SHA384()
            pub_key.verify(sig, message, ec.ECDSA(hash_algo))

            payload = json.loads(_b64d(payload_b64).decode("utf-8"))
            return payload

        return None
    except Exception:
        return None


def _verify_notification_transaction(signed_tx: str) -> dict | None:
    """Verify and decode the nested signedTransactionInfo JWS."""
    return verify_apple_signed_payload(signed_tx)


# ── Notification processing ────────────────────────────────────


def process_apple_notification(payload: dict) -> dict:
    """Process an Apple server-to-server notification V2 payload.

    Returns a result dict with ``ok`` and ``action`` fields.
    """
    notification_type = payload.get("notificationType", "")
    subtype = payload.get("subtype", "")
    data = payload.get("data", {}) or {}

    # Decode the signed transaction info
    signed_tx = data.get("signedTransactionInfo", "")
    tx_info = None
    if signed_tx:
        tx_info = _verify_notification_transaction(signed_tx)

    if not tx_info:
        return {"ok": False, "error": "Missing or invalid transaction info"}

    original_tx_id = tx_info.get("originalTransactionId", "")
    product_id = tx_info.get("productId", "")
    expires_date_ms = _ms_to_float(tx_info.get("expiresDate", ""))

    if not original_tx_id or not product_id:
        return {"ok": False, "error": "Invalid transaction data"}

    # Look up the user by original_transaction_id
    sub = None
    with _ORIGINAL_TX_MAP_LOCK:
        tx_map = _load_original_tx_map()
        sub = tx_map.get(original_tx_id)

    if not sub:
        return {"ok": False, "error": "User not found for original_transaction_id"}

    # Process based on notification type
    with _USERS_LOCK:
        users = _load_users()
        user = users.get(sub)
        if not user:
            return {"ok": False, "error": "User not found"}

        product_map = _get_product_mapping()
        product = product_map.get(product_id)
        if not product:
            return {"ok": False, "error": f"Unknown product: {product_id}"}

        action = ""
        changed = False

        if notification_type in ("SUBSCRIBED", "DID_RENEW", "INTERACTIVE_RENEWAL"):
            # Apply or update subscription
            if expires_date_ms:
                user["tier"] = product.get("tier", "premium")
                user["tier_expires_at"] = expires_date_ms
                user["tier_product_id"] = product_id
                user["credits_monthly_cap"] = product.get("credits_monthly_cap", 0)
                action = "subscription_applied"
                changed = True

                # Deduplicate transaction_id
                tx_id = tx_info.get("transactionId", "")
                if tx_id:
                    processed = user.setdefault("processed_transactions", [])
                    if tx_id not in processed:
                        processed.append(tx_id)

        elif notification_type == "EXPIRED":
            subtype_lower = subtype.lower() if subtype else ""
            # Only downgrade if expired after a billing retry (not voluntary cancellation)
            # or if it's truly expired
            if subtype_lower == "voluntary":
                # User cancelled — keep the tier until expires, but flag
                action = "expired_voluntary"
            else:
                user["tier"] = "free"
                user["tier_expires_at"] = None
                user["tier_product_id"] = None
                user["credits_monthly_cap"] = 0
                purchased = user.get("credits_purchased", 0) or 0
                user["credits"] = purchased
                user["credits_granted_until"] = None
                action = "downgraded_to_free"
                changed = True

        elif notification_type == "REFUND":
            # Refund — remove credits associated with refunded purchase
            if product.get("type") == "consumable":
                credit_amount = product.get("credits", 0)
                user["credits"] = max(0, (user.get("credits", 0) or 0) - credit_amount)
                user["credits_purchased"] = max(0, (user.get("credits_purchased", 0) or 0) - credit_amount)
                action = "credits_removed"
                changed = True
            elif product.get("type") == "subscription":
                user["tier"] = "free"
                user["tier_expires_at"] = None
                user["tier_product_id"] = None
                user["credits_monthly_cap"] = 0
                purchased = user.get("credits_purchased", 0) or 0
                user["credits"] = purchased
                user["credits_granted_until"] = None
                action = "refund_downgraded"
                changed = True

        elif notification_type == "REVOKE":
            # Family sharing / transfer revoked
            user["tier"] = "free"
            user["tier_expires_at"] = None
            user["tier_product_id"] = None
            user["credits_monthly_cap"] = 0
            purchased = user.get("credits_purchased", 0) or 0
            user["credits"] = purchased
            user["credits_granted_until"] = None
            action = "revoke_downgraded"
            changed = True

        elif notification_type == "DID_CHANGE_RENEWAL_PREF":
            # User changed plan (e.g., monthly → yearly)
            if expires_date_ms and product.get("type") == "subscription":
                user["tier"] = product.get("tier", "premium")
                user["tier_product_id"] = product_id
                user["credits_monthly_cap"] = product.get("credits_monthly_cap", 0)
                action = "plan_changed"
                changed = True

        elif notification_type == "DID_CHANGE_RENEWAL_STATUS":
            action = "renewal_status_changed"

        elif notification_type in ("DID_FAIL_TO_RENEW", "GRACE_PERIOD_EXPIRED",
                                    "PRICE_INCREASE", "RENEWAL_EXTENDED"):
            action = f"logged_{notification_type.lower()}"

        elif notification_type == "TEST":
            action = "test_notification"

        if changed:
            user["updated_at"] = time.time()
            _save_users(users)

        return {"ok": True, "action": action, "notification_type": notification_type}


def _verify_receipt_with_apple(receipt_data: str, url: str) -> dict | None:
    """Send receipt data to Apple's verification endpoint."""
    shared_secret = _apple_store_shared_secret()
    if not shared_secret:
        return None
    try:
        resp = requests.post(
            url,
            json={"receipt-data": receipt_data, "password": shared_secret, "exclude-old-transactions": True},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def verify_app_store_receipt(receipt_data: str) -> dict | None:
    """Verify an App Store receipt.

    Returns parsed purchase info::

        {
            "status": 0,
            "environment": "Production" | "Sandbox",
            "transactions": [
                {
                    "product_id": "...",
                    "transaction_id": "...",
                    "original_transaction_id": "...",
                    "purchase_date_ms": 123,
                    "expires_date_ms": 456 | None,  (null for consumables)
                    "is_trial_period": False,
                },
                ...
            ],
        }

    Returns None on failure.
    """
    result = _verify_receipt_with_apple(receipt_data, APP_STORE_PRODUCTION_URL)
    # 21007 means the receipt is from sandbox environment
    if result is None:
        return None
    if result.get("status") == 21007:
        result = _verify_receipt_with_apple(receipt_data, APP_STORE_SANDBOX_URL)
        if result is None:
            return None

    status = result.get("status")
    if status != 0:
        return None

    transactions = []
    latest_info = result.get("latest_receipt_info") or result.get("receipt", {}).get("in_app") or []
    for entry in latest_info:
        transactions.append({
            "product_id": entry.get("product_id", ""),
            "transaction_id": entry.get("transaction_id", ""),
            "original_transaction_id": entry.get("original_transaction_id", ""),
            "purchase_date_ms": _ms_to_float(entry.get("purchase_date_ms")),
            "expires_date_ms": _ms_to_float(entry.get("expires_date_ms")),
            "is_trial_period": str(entry.get("is_trial_period", "false")).lower() == "true",
        })

    return {
        "status": 0,
        "environment": result.get("environment", "Unknown"),
        "transactions": transactions,
    }


def _ms_to_float(ms_str: str | None) -> float | None:
    if not ms_str:
        return None
    try:
        return float(ms_str) / 1000.0
    except (ValueError, TypeError):
        return None


# ═══════════════════════════════════════════════════════════════════
#  Purchase application logic
# ═══════════════════════════════════════════════════════════════════


def _get_product_mapping() -> dict[str, dict]:
    """Return flat map of all product IDs to their effects."""
    mapping: dict[str, dict] = {}
    for pid, info in SUBSCRIPTION_PRODUCTS.items():
        mapping[pid] = {"type": "subscription", **info}
    for pid, info in CONSUMABLE_PRODUCTS.items():
        mapping[pid] = {"type": "consumable", **info}
    return mapping


def _apply_transaction(sub: str, tx: dict, user: dict) -> bool:
    """Apply a single verified transaction to a user record. Returns True if changed."""
    product_id = tx.get("product_id", "")
    tx_id = tx.get("transaction_id", "")
    if not product_id or not tx_id:
        return False

    processed = user.setdefault("processed_transactions", [])
    if tx_id in processed:
        return False

    product_map = _get_product_mapping()
    product = product_map.get(product_id)
    if not product:
        return False

    product_type = product.get("type")

    if product_type == "subscription":
        expires_at = tx.get("expires_date_ms")
        if not expires_at:
            return False
        user["tier"] = product.get("tier", "premium")
        user["tier_expires_at"] = expires_at
        user["tier_product_id"] = product_id
        monthly_cap = product.get("credits_monthly_cap", 0)
        user["credits_monthly_cap"] = monthly_cap
        # Reset grant tracker so monthly drip starts from now
        user["credits_granted_until"] = tx.get("purchase_date_ms", time.time()) or time.time()

        # Map original_transaction_id → sub for server-to-server notifications
        original_tx_id = tx.get("original_transaction_id", "")
        if original_tx_id:
            with _ORIGINAL_TX_MAP_LOCK:
                tx_map = _load_original_tx_map()
                if tx_map.get(original_tx_id) != sub:
                    tx_map[original_tx_id] = sub
                    _save_original_tx_map(tx_map)

    elif product_type == "consumable":
        credit_amount = product.get("credits", 0)
        user["credits"] = (user.get("credits", 0) or 0) + credit_amount
        user["credits_purchased"] = (user.get("credits_purchased", 0) or 0) + credit_amount
        user["total_credits_purchased"] = (user.get("total_credits_purchased", 0) or 0) + credit_amount

    processed.append(tx_id)
    return True


def apply_purchases(sub: str, parsed_receipt: dict) -> dict | None:
    """Apply all new/unprocessed transactions from a parsed receipt.

    Returns updated user or None.
    """
    with _USERS_LOCK:
        users = _load_users()
        user = users.get(sub)
        if not user:
            return None
        changed = False
        for tx in (parsed_receipt.get("transactions") or []):
            if _apply_transaction(sub, tx, user):
                changed = True
        if changed:
            user["updated_at"] = time.time()
            _save_users(users)
        return _sanitize_user(user)


# ═══════════════════════════════════════════════════════════════════
#  User persistence  (JSON file, no DB)
# ═══════════════════════════════════════════════════════════════════


def _load_users() -> dict[str, dict]:
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_users(users: dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, default=str, ensure_ascii=False)


def _sanitize_user(user: dict) -> dict:
    return {k: v for k, v in user.items() if k in _USER_FIELDS}


def get_user(sub: str) -> dict | None:
    with _USERS_LOCK:
        users = _load_users()
        raw = users.get(sub)
        return _sanitize_user(raw) if raw else None


def get_or_create_user(sub: str) -> dict:
    """Return existing user or create a new one with free tier."""
    with _USERS_LOCK:
        users = _load_users()
        if sub not in users:
            users[sub] = {
                "sub": sub,
                "tier": "free",
                "tier_expires_at": None,
                "tier_product_id": None,
                "credits": 0,
                "credits_purchased": 0,
                "credits_monthly_cap": 0,
                "credits_granted_until": None,
                "total_credits_purchased": 0,
                "teams_followed": [],
                "leagues_followed": [],
                "widget_settings": {},
                "live_activity_settings": {},
                "processed_transactions": [],
                "created_at": time.time(),
                "updated_at": time.time(),
            }
            _save_users(users)
        return _sanitize_user(users[sub])


def update_user(sub: str, **kwargs) -> dict | None:
    """Update fields on a user record.

    Allowed keys: tier, tier_expires_at, tier_product_id,
    credits, credits_monthly_cap, credits_granted_until,
    teams_followed, leagues_followed,
    widget_settings, live_activity_settings.
    """
    allowed = {
        "tier", "tier_expires_at", "tier_product_id",
        "credits", "credits_purchased", "credits_monthly_cap", "credits_granted_until",
        "teams_followed", "leagues_followed",
        "widget_settings", "live_activity_settings",
    }
    with _USERS_LOCK:
        users = _load_users()
        if sub not in users:
            return None
        changed = False
        for k, v in kwargs.items():
            if k in allowed and v is not None:
                users[sub][k] = v
                changed = True
        if changed:
            users[sub]["updated_at"] = time.time()
            _save_users(users)
        return _sanitize_user(users[sub])


# ═══════════════════════════════════════════════════════════════════
#  Credit management
# ═══════════════════════════════════════════════════════════════════


def get_credits(sub: str) -> int:
    """Return the current credit balance for a user."""
    user = get_user(sub)
    return (user.get("credits", 0) or 0) if user else 0


def decrement_credits(sub: str, amount: int = 1) -> tuple[bool, int]:
    """Decrement credits atomically. Returns (success, new_balance).

    Spends subscription (drip) credits first, then purchased credits.
    ``amount`` must be positive.  Returns ``(False, current_balance)``
    if insufficient funds.
    """
    if amount < 1:
        return False, 0
    with _USERS_LOCK:
        users = _load_users()
        user = users.get(sub)
        if not user:
            return False, 0
        balance = user.get("credits", 0) or 0
        purchased = user.get("credits_purchased", 0) or 0
        if balance < amount:
            return False, balance
        # Spend subscription credits first, then purchased
        earned = balance - purchased
        amt_from_earned = min(amount, earned)
        amt_from_purchased = amount - amt_from_earned
        user["credits"] = balance - amount
        user["credits_purchased"] = purchased - amt_from_purchased
        user["updated_at"] = time.time()
        _save_users(users)
        return True, user["credits"]


def add_credits(sub: str, amount: int) -> int:
    """Add credits to a user's balance. Returns new balance."""
    if amount < 1:
        return 0
    with _USERS_LOCK:
        users = _load_users()
        user = users.get(sub)
        if not user:
            return 0
        user["credits"] = (user.get("credits", 0) or 0) + amount
        user["updated_at"] = time.time()
        _save_users(users)
        return user["credits"]


# ═══════════════════════════════════════════════════════════════════
#  Tier validation
# ═══════════════════════════════════════════════════════════════════

# Ordered from lowest to highest access.
TIER_HIERARCHY = ["free", "plus", "prediction", "premium"]


def _tier_rank(tier: str) -> int:
    try:
        return TIER_HIERARCHY.index(tier)
    except ValueError:
        return -1


def _grant_monthly_credits_if_due(user: dict) -> bool:
    """Grant monthly credits for any elapsed months since last grant.

    For active subscriptions with ``credits_monthly_cap > 0``, grants
    credits for each full calendar month that has passed since
    ``credits_granted_until``.  Returns True if credits were added.
    """
    now = time.time()
    tier = user.get("tier", "free")
    cap = user.get("credits_monthly_cap", 0) or 0
    expires_at = user.get("tier_expires_at")
    if tier == "free" or cap <= 0:
        return False
    if expires_at and now >= expires_at:
        return False

    granted_until = user.get("credits_granted_until", 0) or 0
    if granted_until <= 0:
        user["credits_granted_until"] = now
        user["credits"] = (user.get("credits", 0) or 0) + cap
        return True

    # Count full months elapsed since last grant
    import datetime as _dt
    start = _dt.datetime.fromtimestamp(granted_until, tz=_dt.timezone.utc)
    now_dt = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc)
    months = (now_dt.year - start.year) * 12 + (now_dt.month - start.month)
    if months > 0:
        user["credits"] = (user.get("credits", 0) or 0) + months * cap
        # Advance to the start of the current month
        advanced = _dt.datetime(now_dt.year, now_dt.month, 1, tzinfo=_dt.timezone.utc)
        user["credits_granted_until"] = advanced.timestamp()
        return True
    return False


def check_and_refresh_tier(sub: str) -> dict | None:
    """Check if the user's subscription has expired and auto-downgrade to free.

    Also grants monthly credits for active subscriptions on every check.
    Returns the (possibly updated) user dict, or None if the user doesn't exist.
    """
    now = time.time()
    with _USERS_LOCK:
        users = _load_users()
        user = users.get(sub)
        if not user:
            return None
        tier = user.get("tier", "free")
        expires_at = user.get("tier_expires_at")
        if tier != "free" and expires_at and now >= expires_at:
            user["tier"] = "free"
            user["tier_expires_at"] = None
            user["tier_product_id"] = None
            user["credits_monthly_cap"] = 0
            purchased = user.get("credits_purchased", 0) or 0
            user["credits"] = purchased
            user["credits_granted_until"] = None
            user["updated_at"] = now

        _grant_monthly_credits_if_due(user)
        _save_users(users)
        return _sanitize_user(user)


def meets_tier_requirement(user_tier: str, min_tier: str) -> bool:
    """Return True if *user_tier* meets or exceeds *min_tier*."""
    return _tier_rank(user_tier) >= _tier_rank(min_tier)


def require_tier(user: dict, min_tier: str) -> tuple[bool, str | None]:
    """Check if *user* meets the *min_tier* requirement.

    Returns ``(True, None)`` if allowed, or ``(False, error_message)`` if denied.
    """
    user_tier = user.get("tier", "free")
    if meets_tier_requirement(user_tier, min_tier):
        return True, None
    return False, f"Requires '{min_tier}' tier or higher (current: '{user_tier}')"


def require_credits(user: dict, amount: int = 1) -> tuple[bool, str | None]:
    """Check if *user* has at least *amount* credits.

    Returns ``(True, None)`` if sufficient, or ``(False, error_message)`` if not.
    """
    balance = user.get("credits", 0) or 0
    if balance >= amount:
        return True, None
    return False, f"Insufficient credits (have {balance}, need {amount})"


# ═══════════════════════════════════════════════════════════════════
#  Session JWT  (HS256, signed with FLASK_SECRET_KEY)
# ═══════════════════════════════════════════════════════════════════


def create_session_jwt(sub: str, tier: str = "free") -> str | None:
    if not config._SECRET_KEY:
        return None
    now = time.time()
    payload = {
        "sub": sub,
        "tier": tier,
        "iat": now,
        "exp": now + 86400 * SESSION_TTL_DAYS,
    }
    try:
        return jwt.encode(payload, config._SECRET_KEY, algorithm="HS256")
    except Exception:
        return None


def decode_session_jwt(token: str) -> dict | None:
    if not config._SECRET_KEY:
        return None
    try:
        return jwt.decode(token, config._SECRET_KEY, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    except Exception:
        return None
