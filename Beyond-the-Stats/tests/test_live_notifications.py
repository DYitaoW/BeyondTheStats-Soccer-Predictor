"""Comprehensive tests for live score event detection and notification dispatch:
- Kickoff / Match Start
- Halftime (STATUS_HALFTIME, HT, Halftime period)
- Goals with Goalscorer and Minute formatting
- Strict deduplication (no 5-10 duplicate storms)
- Full Time / End of Game detection
- Server restart / startup idempotency
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBSITE_DIR = ROOT / "Website"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(WEBSITE_DIR) not in sys.path:
    sys.path.insert(0, str(WEBSITE_DIR))

import notifications
notifications.sqlite_store = None

from notifications import (
    _apns_notification_queue,
    _live_activities,
    _match_notification_subscriptions,
    _match_subscription_events,
    _team_notification_subscriptions,
    _team_subscription_events,
    subscribe_match,
    register,
)
import live_poller
from live_poller import (
    _live_scores,
    _live_scores_lock,
    _notified_events,
    _notified_events_lock,
    _match_tracker,
    _match_tracker_lock,
    _dispatch_match_event_notifications,
)


class TestLivePollerNotifications(unittest.TestCase):
    def setUp(self):
        with _live_scores_lock:
            _live_scores.clear()
        with _notified_events_lock:
            _notified_events.clear()
        with _match_tracker_lock:
            _match_tracker.clear()
        _apns_notification_queue.clear()
        _match_notification_subscriptions.clear()
        _match_subscription_events.clear()
        _team_notification_subscriptions.clear()
        _team_subscription_events.clear()
        _live_activities.clear()

        # Set up a subscriber device and Live Activity for test match
        subscribe_match("device-tok-abc", "match-100", "England/Premier League")
        register("la-tok-xyz", "device-tok-abc", "match-100", "England/Premier League")

    def _set_game(self, game_dict):
        with _live_scores_lock:
            _live_scores["England/Premier League"] = {
                "competition": "England/Premier League",
                "last_polled_utc": "2026-10-24T20:00:00Z",
                "games": [game_dict],
            }

    def test_match_kickoff_dispatches_once(self):
        # 1. Match is scheduled (pre)
        self._set_game({
            "match_id": "match-100",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "home_score": None,
            "away_score": None,
            "status": "pre",
            "clock": "",
            "period": "Scheduled",
        })
        _dispatch_match_event_notifications()
        self.assertEqual(len(_apns_notification_queue), 0)

        # 2. Match kicks off (in)
        self._set_game({
            "match_id": "match-100",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "home_score": 0,
            "away_score": 0,
            "status": "in",
            "clock": "1'",
            "period": "1st Half",
            "status_type": "STATUS_FIRST_HALF",
        })
        dispatched = _dispatch_match_event_notifications()
        self.assertEqual(dispatched, 1)

        # Regular push and Live Activity alert should be queued
        regular_pushes = [e for e in _apns_notification_queue if e.get("type") != "liveactivity"]
        la_pushes = [e for e in _apns_notification_queue if e.get("type") == "liveactivity"]

        device_pushes = [e for e in regular_pushes if e["token"] == "device-tok-abc"]
        self.assertEqual(len(device_pushes), 1)
        self.assertEqual(device_pushes[0]["event"], "start")
        self.assertIn("Kickoff", device_pushes[0]["body"])

        # Every target device token must appear at most once (zero duplicates)
        reg_tokens = [e["token"] for e in regular_pushes]
        self.assertEqual(len(reg_tokens), len(set(reg_tokens)))

        self.assertEqual(len(la_pushes), 1)
        self.assertEqual(la_pushes[0]["event"], "kickoff")

        # 3. Next poll cycle: match is at 2' — must NOT re-alert kickoff
        _apns_notification_queue.clear()
        self._set_game({
            "match_id": "match-100",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "home_score": 0,
            "away_score": 0,
            "status": "in",
            "clock": "2'",
            "period": "1st Half",
            "status_type": "STATUS_FIRST_HALF",
        })
        dispatched_second = _dispatch_match_event_notifications()
        self.assertEqual(dispatched_second, 0)
        self.assertEqual(len(_apns_notification_queue), 0)

    def test_goal_with_goalscorer_formatting_and_deduplication(self):
        # 1. Match is 0-0
        self._set_game({
            "match_id": "match-100",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "home_score": 0,
            "away_score": 0,
            "status": "in",
            "clock": "20'",
            "period": "1st Half",
            "status_type": "STATUS_FIRST_HALF",
        })
        _dispatch_match_event_notifications()
        _apns_notification_queue.clear()

        # 2. Goal scored for Arsenal (1-0) with goalscorer from scoreboard details
        self._set_game({
            "match_id": "match-100",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "home_score": 1,
            "away_score": 0,
            "status": "in",
            "clock": "23'",
            "period": "1st Half",
            "status_type": "STATUS_FIRST_HALF",
            "goalscorers": [
                {"team": "home", "scorer": "B. Saka", "minute": "23'", "type": "Goal"}
            ],
        })
        dispatched = _dispatch_match_event_notifications()
        self.assertEqual(dispatched, 1)

        regular_pushes = [e for e in _apns_notification_queue if e.get("type") != "liveactivity"]
        device_pushes = [e for e in regular_pushes if e["token"] == "device-tok-abc"]
        self.assertEqual(len(device_pushes), 1)
        self.assertEqual(device_pushes[0]["event"], "goal")
        self.assertEqual(device_pushes[0]["title"], "Goal")
        # Ensure goalscorer name and minute are in the alert body
        self.assertIn("Arsenal 1-0 Chelsea", device_pushes[0]["body"])
        self.assertIn("B. Saka (23')", device_pushes[0]["body"])

        # Ensure no duplicates per token
        reg_tokens = [e["token"] for e in regular_pushes]
        self.assertEqual(len(reg_tokens), len(set(reg_tokens)))

        la_pushes = [e for e in _apns_notification_queue if e.get("type") == "liveactivity"]
        self.assertEqual(len(la_pushes), 1)
        self.assertEqual(la_pushes[0]["event"], "goal")
        self.assertEqual(la_pushes[0]["alert"]["body"], device_pushes[0]["body"])

        # 3. Subsequent poll cycles: ESPN enriches text with assist and description
        # MUST NOT send duplicate notifications for 1-0!
        _apns_notification_queue.clear()
        for clock_str in ("23'", "24'", "25'"):
            self._set_game({
                "match_id": "match-100",
                "home_team": "Arsenal",
                "away_team": "Chelsea",
                "home_score": 1,
                "away_score": 0,
                "status": "in",
                "clock": clock_str,
                "period": "1st Half",
                "status_type": "STATUS_FIRST_HALF",
                "goalscorers": [
                    {"team": "home", "scorer": "B. Saka", "minute": "23'", "type": "Goal"}
                ],
                "key_events": [
                    {
                        "type": "Goal",
                        "scoring_play": True,
                        "athlete_name": "B. Saka",
                        "clock": "23'",
                        "text": "Goal! Arsenal 1, Chelsea 0. Bukayo Saka left footed shot. Assisted by Martin Odegaard.",
                    }
                ],
            })
            d = _dispatch_match_event_notifications()
            self.assertEqual(d, 0)
            self.assertEqual(len(_apns_notification_queue), 0, f"Spurious alert on poll cycle {clock_str}")

        # 4. Away team scores (1-1)
        self._set_game({
            "match_id": "match-100",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "home_score": 1,
            "away_score": 1,
            "status": "in",
            "clock": "38'",
            "period": "1st Half",
            "status_type": "STATUS_FIRST_HALF",
            "goalscorers": [
                {"team": "home", "scorer": "B. Saka", "minute": "23'", "type": "Goal"},
                {"team": "away", "scorer": "C. Palmer", "minute": "38'", "type": "Penalty - Scored"},
            ],
        })
        dispatched_goal2 = _dispatch_match_event_notifications()
        self.assertEqual(dispatched_goal2, 1)

        reg_pushes_2 = [e for e in _apns_notification_queue if e.get("type") != "liveactivity"]
        dev_pushes_2 = [e for e in reg_pushes_2 if e["token"] == "device-tok-abc"]
        self.assertEqual(len(dev_pushes_2), 1)
        self.assertIn("Arsenal 1-1 Chelsea", dev_pushes_2[0]["body"])
        self.assertIn("C. Palmer (38' pen)", dev_pushes_2[0]["body"])

    def test_halftime_detection(self):
        # 1. Match in first half
        self._set_game({
            "match_id": "match-100",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "home_score": 1,
            "away_score": 0,
            "status": "in",
            "clock": "45'",
            "period": "1st Half",
            "status_type": "STATUS_FIRST_HALF",
        })
        _dispatch_match_event_notifications()
        _apns_notification_queue.clear()

        # 2. Halftime occurs with STATUS_HALFTIME and HT clock
        self._set_game({
            "match_id": "match-100",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "home_score": 1,
            "away_score": 0,
            "status": "in",
            "clock": "HT",
            "period": "Halftime",
            "status_type": "STATUS_HALFTIME",
        })
        dispatched = _dispatch_match_event_notifications()
        self.assertEqual(dispatched, 1)

        regular_pushes = [e for e in _apns_notification_queue if e.get("type") != "liveactivity"]
        device_pushes = [e for e in regular_pushes if e["token"] == "device-tok-abc"]
        self.assertEqual(len(device_pushes), 1)
        self.assertEqual(device_pushes[0]["event"], "ht")
        self.assertEqual(device_pushes[0]["title"], "Halftime")
        self.assertIn("Arsenal 1-0 Chelsea - Halftime", device_pushes[0]["body"])

        la_pushes = [e for e in _apns_notification_queue if e.get("type") == "liveactivity"]
        self.assertEqual(len(la_pushes), 1)
        self.assertEqual(la_pushes[0]["event"], "halftime")

        # 3. Next poll cycle: still Halftime — must NOT duplicate
        _apns_notification_queue.clear()
        _dispatch_match_event_notifications()
        self.assertEqual(len(_apns_notification_queue), 0)

    def test_fulltime_detection_and_subscription_cleanup(self):
        # 1. Match in second half (2-1)
        self._set_game({
            "match_id": "match-100",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "home_score": 2,
            "away_score": 1,
            "status": "in",
            "clock": "90'+4'",
            "period": "2nd Half",
            "status_type": "STATUS_SECOND_HALF",
        })
        _dispatch_match_event_notifications()
        _apns_notification_queue.clear()

        # 2. Match finishes (status: "post", STATUS_FULL_TIME)
        self._set_game({
            "match_id": "match-100",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "home_score": 2,
            "away_score": 1,
            "status": "post",
            "clock": "FT",
            "period": "FT",
            "status_type": "STATUS_FULL_TIME",
        })
        dispatched = _dispatch_match_event_notifications()
        self.assertEqual(dispatched, 1)

        regular_pushes = [e for e in _apns_notification_queue if e.get("type") != "liveactivity"]
        device_pushes = [e for e in regular_pushes if e["token"] == "device-tok-abc"]
        self.assertEqual(len(device_pushes), 1)
        self.assertEqual(device_pushes[0]["event"], "end")
        self.assertEqual(device_pushes[0]["title"], "Full Time")
        self.assertIn("Arsenal 2-1 Chelsea - Full Time", device_pushes[0]["body"])

        la_pushes = [e for e in _apns_notification_queue if e.get("type") == "liveactivity"]
        self.assertEqual(len(la_pushes), 1)
        self.assertEqual(la_pushes[0]["event"], "end")
        self.assertEqual(la_pushes[0]["content_state"]["status"], "FT")

        # 3. Match subscriptions must be cleared after FT
        subs = notifications.for_match_tokens("match-100", "England/Premier League")
        self.assertEqual(len(subs), 0)

    def test_startup_idempotency_does_not_spam_past_games(self):
        """When poller starts fresh, existing completed or live games must not spam subscribers."""
        # Setup finished match from 2 hours ago
        self._set_game({
            "match_id": "old-match-999",
            "home_team": "Liverpool",
            "away_team": "Man City",
            "home_score": 3,
            "away_score": 2,
            "status": "post",
            "clock": "FT",
            "period": "FT",
            "status_type": "STATUS_FULL_TIME",
            "goalscorers": [
                {"team": "home", "scorer": "Salah", "minute": "12'", "type": "Goal"},
                {"team": "away", "scorer": "Haaland", "minute": "34'", "type": "Goal"},
                {"team": "home", "scorer": "Diaz", "minute": "55'", "type": "Goal"},
                {"team": "away", "scorer": "Foden", "minute": "67'", "type": "Goal"},
                {"team": "home", "scorer": "Nunez", "minute": "89'", "type": "Goal"},
            ],
        })
        subscribe_match("device-tok-abc", "old-match-999", "England/Premier League")

        # First cycle after startup
        dispatched = _dispatch_match_event_notifications()
        self.assertEqual(dispatched, 0)
        self.assertEqual(len(_apns_notification_queue), 0, "Startup must not blast old finished games to subscribers")


if __name__ == "__main__":
    unittest.main()
