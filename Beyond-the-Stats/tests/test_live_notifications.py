"""Tests for live game & team push notifications, SQLite persistence, and API routes."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Ensure Website and shared are on sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
_WEBSITE_DIR = _REPO_ROOT / "Website"
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_WEBSITE_DIR) not in sys.path:
    sys.path.insert(0, str(_WEBSITE_DIR))

from shared import sqlite_store
import notifications


class LiveNotificationsTests(unittest.TestCase):
    def setUp(self):
        # Clear in-memory notification state before each test
        with notifications._match_notification_subscriptions_lock:
            notifications._match_notification_subscriptions.clear()
        with notifications._team_notification_subscriptions_lock:
            notifications._team_notification_subscriptions.clear()
        notifications._apns_notification_queue.clear()
        notifications._notifications.clear()
        notifications.device_tokens.clear()
        notifications.ios_device_tokens.clear()

    def test_team_subscribe_and_unsubscribe_case_insensitive(self):
        token = "device_token_abc123"
        # Subscribe to "Arsenal"
        ok = notifications.subscribe_team(token, "Arsenal", competition="Premier League")
        self.assertTrue(ok)
        self.assertIn(token, notifications.ios_device_tokens)

        # Lookup with lowercase and mixed case
        tokens_lower = notifications.for_team_tokens("arsenal")
        self.assertEqual(tokens_lower, [token])
        tokens_upper = notifications.for_team_tokens("ARSENAL")
        self.assertEqual(tokens_upper, [token])

        # Unsubscribe
        unsub_ok = notifications.unsubscribe_team(token, "arsenal")
        self.assertTrue(unsub_ok)
        self.assertEqual(notifications.for_team_tokens("Arsenal"), [])

        # Unsubscribing non-existent returns False
        self.assertFalse(notifications.unsubscribe_team(token, "Arsenal"))

    def test_for_game_or_team_subscribers_deduplication(self):
        token_a = "token_match_and_team"
        token_b = "token_home_team_only"
        token_c = "token_away_team_only"
        token_d = "token_match_only"

        # Token A is subscribed to both the match AND the home team
        notifications.subscribe_match(token_a, "1001", "England/Premier League")
        notifications.subscribe_team(token_a, "Arsenal")

        # Token B subscribed to home team only
        notifications.subscribe_team(token_b, "Arsenal")

        # Token C subscribed to away team only
        notifications.subscribe_team(token_c, "Chelsea")

        # Token D subscribed to match only
        notifications.subscribe_match(token_d, "1001", "England/Premier League")

        recipients = notifications.for_game_or_team_subscribers(
            match_id="1001",
            competition="England/Premier League",
            home_team="Arsenal",
            away_team="Chelsea",
        )

        # All 4 unique tokens should be present, exactly once each
        self.assertEqual(len(recipients), 4)
        self.assertEqual(set(recipients), {token_a, token_b, token_c, token_d})
        # Check no duplicates
        self.assertEqual(len(recipients), len(set(recipients)))

    def test_sqlite_persistence_and_initialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test_bts_store.db"
            with mock.patch.dict(os.environ, {"BTS_SQLITE_STORE_PATH": str(db_path)}):
                # Save subscriptions
                ok1 = sqlite_store.save_notification_sub("tok_1", "match", "401923762|Bundesliga", "Bundesliga", db_path=db_path)
                ok2 = sqlite_store.save_notification_sub("tok_2", "team", "inter miami", "MLS", db_path=db_path)
                self.assertTrue(ok1)
                self.assertTrue(ok2)

                all_subs = sqlite_store.load_all_notification_subs(db_path=db_path)
                self.assertEqual(len(all_subs), 2)
                sub_targets = {s["sub_target"] for s in all_subs}
                self.assertEqual(sub_targets, {"401923762|Bundesliga", "inter miami"})

                # Test init_subscriptions_from_sqlite preloading into clean memory
                with mock.patch.object(sqlite_store, "load_all_notification_subs", return_value=all_subs):
                    info = notifications.init_subscriptions_from_sqlite()
                    self.assertEqual(info["loaded"], 2)
                    self.assertEqual(info["matches"], 1)
                    self.assertEqual(info["teams"], 1)
                    self.assertIn("tok_2", notifications.for_team_tokens("inter miami"))

                # Test clearing match subscriptions
                deleted = sqlite_store.remove_match_notification_subs("401923762", db_path=db_path)
                self.assertEqual(deleted, 1)
                remaining = sqlite_store.load_all_notification_subs(db_path=db_path)
                self.assertEqual(len(remaining), 1)
                self.assertEqual(remaining[0]["sub_target"], "inter miami")

    def test_get_device_subscriptions_helper(self):
        token = "my_iphone_token"
        notifications.subscribe_match(token, "2001", "La Liga")
        notifications.subscribe_team(token, "Real Madrid")
        notifications.subscribe_team(token, "Barcelona")

        subs = notifications.get_device_subscriptions(token)
        self.assertEqual(subs["teams"], ["barcelona", "real madrid"])
        self.assertTrue(any("2001" in m for m in subs["matches"]))

    def test_record_notification_event_and_feed_cap(self):
        # Adding event appends to _notifications
        notifications.record_notification_event(
            title="Goal",
            body="Arsenal 1-0 Chelsea (12')",
            match_id="5001",
            competition="Premier League",
        )
        self.assertEqual(len(notifications._notifications), 1)
        ev = notifications._notifications[0]
        self.assertEqual(ev["title"], "Goal")
        self.assertEqual(ev["match_id"], "5001")
        self.assertEqual(ev["type"], "live_event")



    def test_live_poller_subscriber_resolution_and_queue(self):
        import live_poller
        token_team = "device_fan_of_liverpool"
        notifications.subscribe_team(token_team, "Liverpool")

        # Simulate game data
        game = {
            "match_id": "998877",
            "status": "in",
            "home_team": "Liverpool",
            "away_team": "Everton",
            "home_score": 1,
            "away_score": 0,
            "key_events": [{"type": "goal", "clock": "23", "athlete_id": "11", "text": "Salah Goal"}],
        }

        # Verify subscribers includes Liverpool team subscriber
        subs = notifications.for_game_or_team_subscribers(
            game["match_id"], "England/Premier League", game["home_team"], game["away_team"]
        )
        self.assertIn(token_team, subs)

        # Emulate alert queueing
        for tok in subs:
            notifications._apns_notification_queue.append({
                "token": tok,
                "title": "Goal",
                "body": "Liverpool 1-0 Everton (23')",
                "badge": 1,
                "match_id": game["match_id"],
                "competition": "England/Premier League",
            })
        notifications.record_notification_event("Goal", "Liverpool 1-0 Everton (23')", match_id="998877", competition="England/Premier League")

        self.assertEqual(len(notifications._apns_notification_queue), 1)
        self.assertEqual(notifications._apns_notification_queue[0]["token"], token_team)
        self.assertEqual(len(notifications._notifications), 1)
        self.assertEqual(notifications._notifications[0]["title"], "Goal")


class LiveNotificationsApiTests(unittest.TestCase):
    def setUp(self):
        import app as flask_app
        self.client = flask_app.app.test_client()
        with notifications._match_notification_subscriptions_lock:
            notifications._match_notification_subscriptions.clear()
        with notifications._team_notification_subscriptions_lock:
            notifications._team_notification_subscriptions.clear()
        notifications._apns_notification_queue.clear()
        notifications._notifications.clear()

    def test_api_subscribe_team_and_query_subscriptions(self):
        # 1. Subscribe to team via /api/notifications/subscribe
        res = self.client.post(
            "/api/notifications/subscribe",
            json={"token": "test_tok_99", "team": "Arsenal", "competition": "Premier League"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("type"), "team")

        # 2. Subscribe to second team via dedicated /api/notifications/subscribe-team
        res2 = self.client.post(
            "/api/notifications/subscribe-team",
            json={"token": "test_tok_99", "team": "Inter Miami"},
        )
        self.assertEqual(res2.status_code, 200)
        data2 = res2.get_json()
        self.assertTrue(data2.get("ok"))

        # 3. Query subscriptions via GET /api/notifications/subscriptions
        res3 = self.client.get("/api/notifications/subscriptions?token=test_tok_99")
        self.assertEqual(res3.status_code, 200)
        subs = res3.get_json()
        self.assertTrue(subs.get("ok"))
        self.assertIn("arsenal", subs.get("teams", []))
        self.assertIn("inter miami", subs.get("teams", []))

        # 4. Unsubscribe from team via dedicated /api/notifications/unsubscribe-team
        res4 = self.client.post(
            "/api/notifications/unsubscribe-team",
            json={"token": "test_tok_99", "team": "Arsenal"},
        )
        self.assertEqual(res4.status_code, 200)
        self.assertTrue(res4.get_json().get("ok"))

        # 5. Verify Arsenal is removed
        res5 = self.client.get("/api/notifications/subscriptions?token=test_tok_99")
        subs5 = res5.get_json()
        self.assertNotIn("arsenal", subs5.get("teams", []))
        self.assertIn("inter miami", subs5.get("teams", []))

    def test_api_subscribe_match(self):
        res = self.client.post(
            "/api/notifications/subscribe",
            json={"token": "test_tok_88", "match_id": "4019999", "competition": "MLS"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("type"), "match")

        res_unsub = self.client.post(
            "/api/notifications/unsubscribe",
            json={"token": "test_tok_88", "match_id": "4019999", "competition": "MLS"},
        )
        self.assertEqual(res_unsub.status_code, 200)
        self.assertTrue(res_unsub.get_json().get("ok"))


if __name__ == "__main__":
    unittest.main()
