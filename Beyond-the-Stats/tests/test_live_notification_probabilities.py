import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
WEBSITE_DIR = ROOT / "Website"
for p in (ROOT, WEBSITE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from shared import sqlite_store
from Website import config
import notifications as n
import live_poller as lp


class TestLiveNotificationProbabilities(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "notif_prob.db"
        sqlite_store.ensure_store(self.db_path)
        self._orig_default_db = sqlite_store._default_db_path
        sqlite_store._default_db_path = lambda: self.db_path
        self._orig_store = n.sqlite_store
        n.sqlite_store = sqlite_store

        # Clear memory queues & trackers
        n._apns_notification_queue.clear()
        n._notifications.clear()
        with lp._live_scores_lock:
            lp._live_scores.clear()
        with lp._notified_events_lock:
            lp._notified_events.clear()
        with lp._match_tracker_lock:
            lp._match_tracker.clear()

    def tearDown(self):
        n._apns_notification_queue.clear()
        n._notifications.clear()
        n.sqlite_store = self._orig_store
        sqlite_store._default_db_path = self._orig_default_db
        self.temp_dir.cleanup()

    def test_la_probabilities_calculates_in_play_when_missing(self):
        """When live_prediction is missing on an in-progress game, it calculates live odds on the fly."""
        game = {
            "match_id": "test-101",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "status": "in",
            "home_score": 2,
            "away_score": 0,
            "clock": "60'",
            "period": "2nd Half",
            "sync_key": "2026-11-14|arsenal|chelsea",
        }
        # Pre-match mock: Arsenal was a mild favorite (e.g. 45% / 28% / 27%)
        with mock.patch("live_poller._la_prematch_record", return_value={"prob_home": 0.45, "prob_draw": 0.28, "prob_away": 0.27, "pred_home_goals": 1.5, "pred_away_goals": 1.1}):
            p = lp._la_probabilities(game, "England/Premier League")
            # In a 2-0 game at 60', live in-play prob_home should be significantly higher than pre-match 45%
            self.assertGreater(p[0], 0.70)
            self.assertLess(p[2], 0.15)
            # game['live_prediction'] should now be cached on the game
            self.assertIsNotNone(game.get("live_prediction"))

    def test_build_alert_payload_includes_probabilities(self):
        """Regular APNs alert payloads include prob_home, prob_draw, prob_away."""
        entry = {
            "token": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
            "title": "Goal!",
            "body": "Arsenal 1-0 Chelsea - Saka (34')",
            "match_id": "999",
            "sync_key": "2026-11-14|arsenal|chelsea",
            "competition": "England/Premier League",
            "event": "goal",
            "prob_home": 0.7321,
            "prob_draw": 0.1852,
            "prob_away": 0.0827,
        }
        payload = n.build_alert_payload(entry)
        self.assertEqual(payload["match_id"], "999")
        self.assertEqual(payload["sync_key"], "2026-11-14|arsenal|chelsea")
        self.assertEqual(payload["prob_home"], 0.7321)
        self.assertEqual(payload["prob_draw"], 0.1852)
        self.assertEqual(payload["prob_away"], 0.0827)

    def test_sqlite_store_persists_and_loads_notification_probabilities(self):
        """SQLite notification_history saves and returns live probabilities."""
        event_id = sqlite_store.save_notification_event(
            title="Goal!",
            body="Arsenal 2-0 Chelsea",
            notif_type="goal",
            match_id="m-555",
            sync_key="2026-11-14|arsenal|chelsea",
            competition="England/Premier League",
            home_team="Arsenal",
            away_team="Chelsea",
            home_score=2,
            away_score=0,
            prob_home=0.8654,
            prob_draw=0.1012,
            prob_away=0.0334,
            db_path=self.db_path,
        )
        self.assertGreater(event_id, 0)

        events = sqlite_store.load_recent_notification_events(match_id="m-555", db_path=self.db_path)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["match_id"], "m-555")
        self.assertEqual(ev["home_score"], 2)
        self.assertAlmostEqual(ev["prob_home"], 0.8654, places=4)
        self.assertAlmostEqual(ev["prob_draw"], 0.1012, places=4)
        self.assertAlmostEqual(ev["prob_away"], 0.0334, places=4)

    def test_dispatch_notifications_sends_live_probabilities(self):
        """Live event notifications and Live Activity pushes send newly calculated live probabilities."""
        token = "1111111111111111111111111111111111111111111111111111111111111111"
        n.subscribe_match(token, "match-live-1", "England/Premier League", events=["goal"])

        game = {
            "match_id": "match-live-1",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "status": "in",
            "home_score": 1,
            "away_score": 0,
            "clock": "34'",
            "period": "1st Half",
            "sync_key": "2026-11-14|arsenal|chelsea",
            "goalscorers": [{"player": "Saka", "team": "home", "clock": "34'"}],
        }

        # Initialize match tracker with 0-0 so 1-0 triggers a goal event
        lp._match_tracker["match-live-1"] = {
            "status": "in",
            "home_score": 0,
            "away_score": 0,
            "period": "1st Half",
            "status_type": "STATUS_IN_PROGRESS",
        }

        with lp._live_scores_lock:
            lp._live_scores["England/Premier League"] = {"games": [game]}

        with mock.patch("live_poller._match_prematch_record", return_value={"prob_home": 0.40, "prob_draw": 0.30, "prob_away": 0.30, "pred_home_goals": 1.4, "pred_away_goals": 1.2}):
            lp._dispatch_match_event_notifications()

        # Regular notification queue should contain the goal with in-play probabilities
        regular_pushes = [e for e in n._apns_notification_queue if e.get("type") != "liveactivity"]
        self.assertGreaterEqual(len(regular_pushes), 1)
        goal_push = regular_pushes[0]
        self.assertEqual(goal_push["match_id"], "match-live-1")
        # In a 1-0 game at 34', live prob_home should be higher than pre-match 40%
        self.assertGreater(goal_push["prob_home"], 0.55)
        self.assertLess(goal_push["prob_away"], 0.25)


if __name__ == "__main__":
    unittest.main()
