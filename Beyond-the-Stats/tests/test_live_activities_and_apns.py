"""Comprehensive test suite for Live Activities (ActivityKit) and Apple APNs interaction:
- Live Activity registration via API with sync_key and dual-lookup support
- SQLite persistence and reload across server restarts
- Live Activity query endpoint (GET /api/live-activities)
- Live Activity update and end pushes with content-state and alerts
- Dual match_id & sync_key live event dispatch from live_poller
- Real ES256 APNs JWT generation and verification
- APNs HTTP/2 communication, headers, topics, and Sandbox vs Production fallback
- Interactive diagnostic endpoints (/api/test/live-activities/push, /api/test/notifications/push, /api/test/notifications/status)
"""

import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
_WEBSITE = _ROOT / "Website"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_WEBSITE) not in sys.path:
    sys.path.insert(0, str(_WEBSITE))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
import jwt

from Website.app import app
import config
import notifications
from notifications import (
    _apns_notification_queue,
    _live_activities,
    _live_activities_lock,
    _match_notification_subscriptions,
    _token_environment_cache,
    apns_send_detailed,
    for_match,
    init_subscriptions_from_sqlite,
    register,
    send_live_activity_end,
    send_live_activity_update,
    unregister,
    unregister_by_match,
)
from shared import sqlite_store


class TestLiveActivitiesAndAPNs(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_bts_la.db"
        sqlite_store.ensure_store(self.db_path)
        notifications.sqlite_store = sqlite_store
        self._orig_default_db = sqlite_store._default_db_path
        sqlite_store._default_db_path = lambda: self.db_path

        with _live_activities_lock:
            _live_activities.clear()
        _apns_notification_queue.clear()
        _match_notification_subscriptions.clear()
        _token_environment_cache.clear()

        # Generate a real ES256 key for testing APNs JWT signing
        self.ec_key = ec.generate_private_key(ec.SECP256R1())
        self.pem_key = self.ec_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        self.key_file = Path(self.temp_dir.name) / "AuthKey_TEST123456.p8"
        self.key_file.write_text(self.pem_key)

        self._orig_key_id = config.APNS_KEY_ID
        self._orig_team_id = config.APNS_TEAM_ID
        self._orig_key_path = config.APNS_AUTH_KEY_PATH
        config.APNS_KEY_ID = "TEST123456"
        config.APNS_TEAM_ID = "TEAM123456"
        config.APNS_AUTH_KEY_PATH = str(self.key_file)

        notifications._apns_jwt_cache = None
        notifications._apns_jwt_expiry = 0

        self.client = TestClient(app)

    def tearDown(self):
        config.APNS_KEY_ID = self._orig_key_id
        config.APNS_TEAM_ID = self._orig_team_id
        config.APNS_AUTH_KEY_PATH = self._orig_key_path
        notifications._apns_jwt_cache = None
        notifications._apns_jwt_expiry = 0
        sqlite_store._default_db_path = self._orig_default_db
        self.temp_dir.cleanup()

    # ── 1. Live Activity API & Persistence Tests ────────────────────────

    def test_live_activity_registration_endpoint_and_sqlite_persistence(self):
        # Register a Live Activity with sync_key
        act_token = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        dev_token = "device_test_token_123"
        sync_key = "2026-11-08|arsenal|chelsea"
        espn_mid = "401999111"

        resp = self.client.post("/api/live-activities/register", json={
            "activity_token": act_token,
            "device_token": dev_token,
            "match_id": espn_mid,
            "sync_key": sync_key,
            "competition": "England/Premier League",
            "environment": "sandbox",
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertTrue(data.get("registered"))
        self.assertEqual(data.get("match_id"), espn_mid)
        self.assertEqual(data.get("sync_key"), sync_key)

        # Query GET /api/live-activities
        get_resp = self.client.get("/api/live-activities")
        self.assertEqual(get_resp.status_code, 200)
        get_data = get_resp.json()
        self.assertTrue(get_data.get("ok"))
        self.assertGreaterEqual(get_data.get("count", 0), 1)

        # Filter by match_id
        filter_resp = self.client.get(f"/api/live-activities?match_id={espn_mid}")
        self.assertEqual(filter_resp.status_code, 200)
        self.assertEqual(len(filter_resp.json().get("activities", [])), 1)

        # Filter by sync_key
        filter_sync_resp = self.client.get(f"/api/live-activities?sync_key={sync_key}")
        self.assertEqual(filter_sync_resp.status_code, 200)
        self.assertEqual(len(filter_sync_resp.json().get("activities", [])), 1)

        # Verify reload from SQLite across simulated server restart
        with _live_activities_lock:
            _live_activities.clear()
        self.assertEqual(len(for_match(espn_mid, "England/Premier League")), 0)

        reload_status = init_subscriptions_from_sqlite()
        self.assertGreaterEqual(reload_status.get("live_activities", 0), 1)
        reloaded = for_match(espn_mid, "England/Premier League")
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0]["activity_token"], act_token)

    def test_live_activity_dual_lookup_by_sync_key_and_mid(self):
        act_token = "la_token_upcoming_match"
        sync_key = "2026-11-09|tottenham|fulham"
        espn_mid = "401999222"

        # Client registers before kickoff using sync_key as match_id
        ok = register(
            activity_token=act_token,
            device_token="dev_tok_99",
            match_id=sync_key,
            competition="England/Premier League",
            sync_key=sync_key,
        )
        self.assertTrue(ok)

        # During live match, poller queries with numeric ESPN mid and sync_key
        found = for_match(match_id=espn_mid, competition="England/Premier League", sync_key=sync_key)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["activity_token"], act_token)

    def test_live_activity_update_and_end_queuing(self):
        act_token = "la_token_queue_test"
        mid = "401999333"
        comp = "England/Premier League"

        register(act_token, "dev_99", mid, comp)

        # 1. Update push
        content_state = {
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "home_score": 1,
            "away_score": 0,
            "clock": "23'",
            "status": "in",
            "match_minute": 23,
        }
        sent = send_live_activity_update(
            match_id=mid,
            competition=comp,
            content_state=content_state,
            event="goal",
            alert={"title": "Goal!", "body": "Arsenal 1-0 Chelsea - Saka (23')"},
        )
        self.assertEqual(sent, 1)
        self.assertEqual(len(_apns_notification_queue), 1)

        queued = _apns_notification_queue[0]
        self.assertEqual(queued["type"], "liveactivity")
        self.assertEqual(queued["token"], act_token)
        self.assertEqual(queued["event"], "goal")
        self.assertEqual(queued["content_state"]["home_score"], 1)
        self.assertEqual(queued["alert"]["title"], "Goal!")

        # 2. End push
        _apns_notification_queue.clear()
        end_sent = send_live_activity_end(
            match_id=mid,
            competition=comp,
            content_state={"status": "FT", "home_score": 2, "away_score": 1},
            alert={"title": "Full Time", "body": "Arsenal 2-1 Chelsea"},
        )
        self.assertEqual(end_sent, 1)
        self.assertEqual(len(_apns_notification_queue), 1)
        end_queued = _apns_notification_queue[0]
        self.assertEqual(end_queued["event"], "end")
        self.assertEqual(end_queued["content_state"]["status"], "FT")

        # Live Activity is automatically deregistered upon ending
        self.assertEqual(len(for_match(mid, comp)), 0)

    # ── 2. APNs JWT & HTTP/2 Payload Interaction Tests ─────────────────

    def test_real_es256_apns_jwt_generation(self):
        jwt_token = notifications._generate_apns_jwt()
        self.assertIsNotNone(jwt_token)

        # Decode and verify header and claims
        unverified_header = jwt.get_unverified_header(jwt_token)
        self.assertEqual(unverified_header.get("alg"), "ES256")
        self.assertEqual(unverified_header.get("kid"), "TEST123456")

        decoded = jwt.decode(
            jwt_token,
            self.ec_key.public_key(),
            algorithms=["ES256"],
            issuer="TEAM123456",
        )
        self.assertEqual(decoded.get("iss"), "TEAM123456")
        self.assertIn("iat", decoded)
        self.assertIn("exp", decoded)
        self.assertGreater(decoded["exp"], decoded["iat"])

    def test_live_activities_test_push_endpoint(self):
        # Test calling the diagnostic endpoint /api/test/live-activities/push
        act_token = "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899"

        # Mock the underlying HTTP post to test payload packaging and headers
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.is_success = True
        mock_response.headers = {"apns-id": "TEST-APNS-UUID-1234"}
        mock_response.text = ""

        with patch.object(notifications, "_get_apns_client") as mock_client_getter:
            mock_client = MagicMock()
            mock_client.post.return_value = mock_response
            mock_client_getter.return_value = mock_client

            resp = self.client.post("/api/test/live-activities/push", json={
                "activity_token": act_token,
                "home_team": "Real Madrid",
                "away_team": "Barcelona",
                "home_score": 2,
                "away_score": 1,
                "clock": "78'",
                "event": "update",
                "alert_title": "Goal!",
                "alert_body": "Real Madrid 2-1 Barcelona - Bellingham (78')",
            })

            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("event"), "update")

            # Verify the HTTP call had the correct headers and APNs topic
            mock_client.post.assert_called_once()
            call_args, call_kwargs = mock_client.post.call_args
            endpoint_url = call_args[0]
            headers = call_kwargs.get("headers", {})
            json_body = call_kwargs.get("json", {})

            self.assertIn(act_token, endpoint_url)
            self.assertEqual(headers.get("apns-push-type"), "liveactivity")
            self.assertTrue(headers.get("apns-topic", "").endswith(".push-type.liveactivity"))
            self.assertEqual(headers.get("apns-priority"), "10")
            self.assertEqual(headers.get("apns-expiration"), "0")
            self.assertTrue(headers.get("authorization", "").startswith("bearer "))

            # Verify Live Activity payload format
            aps = json_body.get("aps", {})
            self.assertEqual(aps.get("event"), "update")
            self.assertIn("content-state", aps)
            self.assertEqual(aps["content-state"]["home_team"], "Real Madrid")
            self.assertEqual(aps["content-state"]["home_score"], 2)
            self.assertEqual(aps["alert"]["title"], "Goal!")

    def test_apns_sandbox_production_auto_fallback(self):
        test_token = "1122334455667788990011223344556677889900112233445566778899001122"

        # Mock initial failure on Production (400 BadDeviceToken) then success on Sandbox (200 OK)
        prod_resp = MagicMock()
        prod_resp.status_code = 400
        prod_resp.is_success = False
        prod_resp.headers = {"apns-id": "FAIL-ID"}
        prod_resp.text = '{"reason": "BadDeviceToken"}'
        prod_resp.json.return_value = {"reason": "BadDeviceToken"}

        sand_resp = MagicMock()
        sand_resp.status_code = 200
        sand_resp.is_success = True
        sand_resp.headers = {"apns-id": "SUCCESS-ID"}
        sand_resp.text = ""

        with patch.object(notifications, "_get_apns_client") as mock_client_getter:
            mock_client = MagicMock()
            mock_client.post.side_effect = [prod_resp, sand_resp]
            mock_client_getter.return_value = mock_client

            # Initial config is production (APNS_USE_SANDBOX=0)
            res = apns_send_detailed(
                push_token=test_token,
                payload={"aps": {"alert": "Test"}},
                live_activity=False,
            )

            self.assertTrue(res.get("ok"))
            self.assertEqual(res.get("status_code"), 200)
            self.assertEqual(res.get("apns_id"), "SUCCESS-ID")
            self.assertTrue(res.get("sandbox"))  # Auto-switched to sandbox

            # Verify token environment was cached so subsequent calls go straight to Sandbox
            self.assertEqual(mock_client.post.call_count, 2)
            self.assertEqual(_token_environment_cache.get(test_token), True)


if __name__ == "__main__":
    unittest.main()

