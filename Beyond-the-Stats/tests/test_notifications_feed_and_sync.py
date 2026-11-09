"""Tests for notification feed API (GET /api/notifications), persistent SQLite history,
device registration with initial favorites, and sync_key/canonical team notification matching.
"""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_ROOT = Path(__file__).resolve().parent.parent
_WEBSITE = _ROOT / "Website"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_WEBSITE) not in sys.path:
    sys.path.insert(0, str(_WEBSITE))

from fastapi.testclient import TestClient
from Website.app import app
import notifications
from notifications import (
    _notifications,
    _notifications_lock,
    _match_notification_subscriptions,
    _team_notification_subscriptions,
    _apns_notification_queue,
    ios_device_tokens,
    record_notification_event,
    subscribe_match,
    subscribe_team,
    for_game_or_team_subscribers,
    init_subscriptions_from_sqlite,
)
from shared import sqlite_store


class TestNotificationFeedAndSync(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_bts.db"
        sqlite_store.ensure_store(self.db_path)
        notifications.sqlite_store = sqlite_store
        self._orig_default_db = sqlite_store._default_db_path
        sqlite_store._default_db_path = lambda: self.db_path

        with _notifications_lock:
            _notifications.clear()
        _match_notification_subscriptions.clear()
        _team_notification_subscriptions.clear()
        _apns_notification_queue.clear()
        ios_device_tokens.clear()

        self.client = TestClient(app)

    def tearDown(self):
        sqlite_store._default_db_path = self._orig_default_db
        self.temp_dir.cleanup()

    def test_record_notification_event_and_get_feed(self):
        # 1. Record events
        ev1 = record_notification_event(
            title="Goal",
            body="Arsenal 1-0 Chelsea - Saka (24')",
            match_id="401878760",
            sync_key="2026-11-08|arsenal|chelsea",
            competition="England/Premier League",
            notif_type="goal",
            home_team="Arsenal",
            away_team="Chelsea",
            home_score=1,
            away_score=0,
        )
        self.assertTrue(ev1["id"] > 0)

        ev2 = record_notification_event(
            title="Halftime",
            body="Arsenal 1-0 Chelsea - Halftime",
            match_id="401878760",
            sync_key="2026-11-08|arsenal|chelsea",
            competition="England/Premier League",
            notif_type="ht",
            home_team="Arsenal",
            away_team="Chelsea",
            home_score=1,
            away_score=0,
        )

        # 2. Query GET /api/notifications
        resp = self.client.get("/api/notifications")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertGreaterEqual(data.get("count", 0), 2)
        notifs = data.get("notifications", [])
        self.assertEqual(notifs[0]["type"], "ht")
        self.assertEqual(notifs[1]["type"], "goal")

    def test_notification_feed_filtering(self):
        record_notification_event(
            title="Goal",
            body="Chelsea 1-0 Swansea - Palmer (15')",
            match_id="m101",
            sync_key="2026-11-08|chelsea|swansea",
            competition="England/FA Cup",
            notif_type="goal",
            home_team="Chelsea",
            away_team="Swansea City",
        )
        record_notification_event(
            title="Goal",
            body="Liverpool 1-0 Everton - Salah (33')",
            match_id="m102",
            sync_key="2026-11-08|liverpool|everton",
            competition="England/Premier League",
            notif_type="goal",
            home_team="Liverpool",
            away_team="Everton",
        )

        # Filter by team=chelsea
        resp = self.client.get("/api/notifications?team=chelsea")
        self.assertEqual(resp.status_code, 200)
        items = resp.json().get("notifications", [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["match_id"], "m101")

        # Filter by match_id=m102
        resp = self.client.get("/api/notifications?match_id=m102")
        self.assertEqual(resp.status_code, 200)
        items = resp.json().get("notifications", [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["home_team"], "Liverpool")

        # Filter by sync_key
        resp = self.client.get("/api/notifications?sync_key=2026-11-08|chelsea|swansea")
        self.assertEqual(resp.status_code, 200)
        items = resp.json().get("notifications", [])
        self.assertEqual(len(items), 1)

    def test_device_register_with_favorite_teams_and_persistence(self):
        # Register device with favorite teams
        token = "test-device-token-12345"
        resp = self.client.post("/api/notifications/register", json={
            "device_token": token,
            "platform": "ios",
            "favorite_teams": ["Chelsea", "Swansea"],
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertTrue(data.get("registered"))
        self.assertIn("Chelsea", data.get("teams_subscribed", []))
        self.assertIn("Swansea", data.get("teams_subscribed", []))

        # Ensure token is in ios_device_tokens
        self.assertIn(token, ios_device_tokens)

        # Clear memory and verify reload from SQLite via init_subscriptions_from_sqlite
        ios_device_tokens.clear()
        _team_notification_subscriptions.clear()
        init_subscriptions_from_sqlite()

        self.assertIn(token, ios_device_tokens)
        # Follows are keyed by resolved team id; ESPN's names must still match.
        import notifications as _n
        self.assertIn(token, _n.for_team_tokens("Chelsea", competition="England/Premier League"))
        self.assertIn(token, _n.for_team_tokens("Swansea City", competition="England/Championship"))

    def test_sync_key_and_team_matching_for_game_subscribers(self):
        tok_match = "tok-subscribed-by-sync-key"
        tok_team = "tok-subscribed-to-chelsea"

        # User 1 subscribed to upcoming match using sync_key before ESPN id was known
        sync_key = "2026-11-08|chelsea|arsenal"
        subscribe_match(tok_match, sync_key, competition="England/Premier League")

        # User 2 subscribed to team "Chelsea"
        subscribe_team(tok_team, "Chelsea")

        # During live match, ESPN mid is "401999999", home_team="Chelsea FC", away_team="Arsenal"
        espn_mid = "401999999"
        subscribers = for_game_or_team_subscribers(
            match_id=espn_mid,
            competition="England/Premier League",
            home_team="Chelsea FC",
            away_team="Arsenal",
            event="goal",
            sync_key=sync_key,
        )

        self.assertIn(tok_match, subscribers)
        self.assertIn(tok_team, subscribers)


if __name__ == "__main__":
    unittest.main()
