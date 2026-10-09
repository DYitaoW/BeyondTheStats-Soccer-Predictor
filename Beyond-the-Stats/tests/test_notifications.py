"""Tests for push notifications, Live Activities, event filtering, and SQLite persistence."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Add Website and root to path
_ROOT = Path(__file__).resolve().parent.parent
_WEBSITE = _ROOT / "Website"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_WEBSITE) not in sys.path:
    sys.path.insert(0, str(_WEBSITE))

from notifications import (
    ALL_EVENT_TYPES,
    _apns_notification_queue,
    _drain_queue,
    _match_notification_subscriptions,
    _match_subscription_events,
    _team_notification_subscriptions,
    _team_subscription_events,
    clear_match_subscriptions,
    for_game_or_team_subscribers,
    for_match_tokens,
    for_team_tokens,
    get_device_subscriptions,
    init_subscriptions_from_sqlite,
    normalize_event_type,
    normalize_events,
    register,
    send_live_activity_end,
    send_live_activity_update,
    send_match_notification,
    subscribe_match,
    subscribe_team,
    unregister,
    unsubscribe_match,
    unsubscribe_team,
)
from shared import sqlite_store


class TestEventNormalization(unittest.TestCase):
    def test_normalize_event_type(self):
        self.assertEqual(normalize_event_type("goal"), "goal")
        self.assertEqual(normalize_event_type("goals"), "goal")
        self.assertEqual(normalize_event_type("scoring"), "goal")
        self.assertEqual(normalize_event_type("penalty"), "goal")
        self.assertEqual(normalize_event_type("kickoff"), "start")
        self.assertEqual(normalize_event_type("match_start"), "start")
        self.assertEqual(normalize_event_type("start"), "start")
        self.assertEqual(normalize_event_type("ht"), "ht")
        self.assertEqual(normalize_event_type("halftime"), "ht")
        self.assertEqual(normalize_event_type("half_time"), "ht")
        self.assertEqual(normalize_event_type("end"), "end")
        self.assertEqual(normalize_event_type("ft"), "end")
        self.assertEqual(normalize_event_type("fulltime"), "end")
        self.assertEqual(normalize_event_type("red_card"), "red_card")
        self.assertEqual(normalize_event_type("red_cards"), "red_card")
        self.assertEqual(normalize_event_type("card"), "red_card")

    def test_normalize_events(self):
        # Empty or None defaults to ALL_EVENT_TYPES
        self.assertEqual(normalize_events(None), set(ALL_EVENT_TYPES))
        self.assertEqual(normalize_events([]), set(ALL_EVENT_TYPES))
        self.assertEqual(normalize_events(""), set(ALL_EVENT_TYPES))

        # Comma-separated strings
        self.assertEqual(normalize_events("goal, ht"), {"goal", "ht"})
        self.assertEqual(normalize_events("goals, kickoff, ft"), {"goal", "start", "end"})

        # Lists / Iterables
        self.assertEqual(normalize_events(["goal", "red_card"]), {"goal", "red_card"})


class TestSubscriptionFiltering(unittest.TestCase):
    def setUp(self):
        _match_notification_subscriptions.clear()
        _match_subscription_events.clear()
        _team_notification_subscriptions.clear()
        _team_subscription_events.clear()
        _apns_notification_queue.clear()

    def test_match_subscription_filtering(self):
        # Device 1 wants only goals
        subscribe_match("dev-1", "m123", "EPL", events=["goal"])
        # Device 2 wants only red cards and ht
        subscribe_match("dev-2", "m123", "EPL", events=["red_card", "ht"])
        # Device 3 wants everything
        subscribe_match("dev-3", "m123", "EPL", events=None)

        # Query for goal: dev-1 and dev-3
        goal_tokens = for_match_tokens("m123", "EPL", event="goal")
        self.assertIn("dev-1", goal_tokens)
        self.assertNotIn("dev-2", goal_tokens)
        self.assertIn("dev-3", goal_tokens)

        # Query for ht: dev-2 and dev-3
        ht_tokens = for_match_tokens("m123", "EPL", event="ht")
        self.assertNotIn("dev-1", ht_tokens)
        self.assertIn("dev-2", ht_tokens)
        self.assertIn("dev-3", ht_tokens)

        # Query for start: only dev-3
        start_tokens = for_match_tokens("m123", "EPL", event="start")
        self.assertNotIn("dev-1", start_tokens)
        self.assertNotIn("dev-2", start_tokens)
        self.assertIn("dev-3", start_tokens)

        # Unsubscribe dev-1
        unsubscribe_match("dev-1", "m123", "EPL")
        self.assertNotIn("dev-1", for_match_tokens("m123", "EPL", event="goal"))

    def test_team_subscription_filtering(self):
        subscribe_team("dev-arsenal-goals", "Arsenal", events="goal")
        subscribe_team("dev-arsenal-all", "Arsenal", events=None)

        goal_tokens = for_team_tokens("Arsenal", event="goal")
        self.assertIn("dev-arsenal-goals", goal_tokens)
        self.assertIn("dev-arsenal-all", goal_tokens)

        card_tokens = for_team_tokens("Arsenal", event="red_card")
        self.assertNotIn("dev-arsenal-goals", card_tokens)
        self.assertIn("dev-arsenal-all", card_tokens)

    def test_for_game_or_team_subscribers_union(self):
        subscribe_match("dev-match", "m1", "EPL", events=["goal"])
        subscribe_team("dev-home", "Arsenal", events=["goal", "ht"])
        subscribe_team("dev-away", "Chelsea", events=["red_card"])

        # For a goal event: dev-match and dev-home should receive, dev-away should not
        goal_subs = for_game_or_team_subscribers("m1", "EPL", home_team="Arsenal", away_team="Chelsea", event="goal")
        self.assertIn("dev-match", goal_subs)
        self.assertIn("dev-home", goal_subs)
        self.assertNotIn("dev-away", goal_subs)

        # For a red card: only dev-away
        card_subs = for_game_or_team_subscribers("m1", "EPL", home_team="Arsenal", away_team="Chelsea", event="red_card")
        self.assertNotIn("dev-match", card_subs)
        self.assertNotIn("dev-home", card_subs)
        self.assertIn("dev-away", card_subs)

    def test_get_device_subscriptions_preferences(self):
        subscribe_match("tok-abc", "m1", "EPL", events=["goal", "ht"])
        subscribe_team("tok-abc", "Liverpool", events=["goal"])

        prefs = get_device_subscriptions("tok-abc")
        self.assertIn("m1|epl", prefs["matches"])
        self.assertIn("liverpool", prefs["teams"])
        self.assertEqual(prefs["match_events"]["m1|epl"], ["goal", "ht"])
        self.assertEqual(prefs["team_events"]["liverpool"], ["goal"])


class TestLiveActivityAlerts(unittest.TestCase):
    def setUp(self):
        _apns_notification_queue.clear()
        import notifications
        notifications._live_activities.clear()

    def test_live_activity_alert_payloads(self):
        register("act-token-1", "dev-tok-1", "m99", "EPL")

        # Send a goal update with alert
        goal_alert = {"title": "Goal! Arsenal 1 - 0 Chelsea", "body": "Saka 23'"}
        count = send_live_activity_update(
            "m99", "EPL",
            content_state={"home_score": "1", "away_score": "0"},
            event="goal",
            alert=goal_alert,
        )
        self.assertEqual(count, 1)
        self.assertEqual(len(_apns_notification_queue), 1)

        # Inspect the queued item
        queued = _apns_notification_queue[0]
        self.assertEqual(queued["type"], "liveactivity")
        self.assertEqual(queued["alert"], goal_alert)

        # Mock _apns_send to verify the final APNs payload structure
        sent_payloads = []
        with patch("notifications._apns_send", side_effect=lambda token, payload, topic, live_activity: sent_payloads.append(payload) or True):
            _drain_queue()

        self.assertEqual(len(sent_payloads), 1)
        aps = sent_payloads[0]["aps"]
        self.assertEqual(aps["event"], "goal")
        self.assertEqual(aps["alert"], goal_alert)
        self.assertEqual(aps["sound"], "default")

    def test_live_activity_ht_and_ft_alerts(self):
        register("act-token-2", "dev-tok-2", "m100", "EPL")

        # Halftime alert
        ht_alert = {"title": "Halftime", "body": "Arsenal 1 - 0 Chelsea"}
        send_live_activity_update(
            "m100", "EPL",
            content_state={"status": "HT"},
            event="halftime",
            alert=ht_alert,
        )

        # Full time alert and end
        ft_alert = {"title": "Full Time", "body": "Arsenal 2 - 0 Chelsea"}
        send_live_activity_end(
            "m100", "EPL",
            content_state={"status": "FT"},
            alert=ft_alert,
        )

        sent_payloads = []
        with patch("notifications._apns_send", side_effect=lambda token, payload, topic, live_activity: sent_payloads.append(payload) or True):
            _drain_queue()

        self.assertEqual(len(sent_payloads), 2)
        # 1. HT payload
        self.assertEqual(sent_payloads[0]["aps"]["event"], "halftime")
        self.assertEqual(sent_payloads[0]["aps"]["alert"], ht_alert)
        # 2. FT payload
        self.assertEqual(sent_payloads[1]["aps"]["event"], "end")
        self.assertEqual(sent_payloads[1]["aps"]["alert"], ft_alert)


class TestSQLiteSubscriptionPersistence(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp_db = Path(tempfile.gettempdir()) / "test_bts_notifs.db"
        if self.temp_db.exists():
            self.temp_db.unlink()
        sqlite_store.ensure_store(self.temp_db)

    def tearDown(self):
        if self.temp_db.exists():
            try:
                self.temp_db.unlink()
            except Exception:
                pass

    def test_sqlite_save_and_load_events(self):
        # Save subscriptions with events
        sqlite_store.save_notification_sub(
            "tok-1", "match", "m123|epl", competition="EPL", events=["goal", "ht"], db_path=self.temp_db
        )
        sqlite_store.save_notification_sub(
            "tok-2", "team", "arsenal", events="goal,red_card", db_path=self.temp_db
        )

        subs = sqlite_store.load_all_notification_subs(db_path=self.temp_db)
        self.assertEqual(len(subs), 2)

        sub_m = next(s for s in subs if s["token"] == "tok-1")
        self.assertEqual(sub_m["sub_type"], "match")
        self.assertEqual(sorted(sub_m["events"]), ["goal", "ht"])

        sub_t = next(s for s in subs if s["token"] == "tok-2")
        self.assertEqual(sub_t["sub_type"], "team")
        self.assertEqual(sorted(sub_t["events"]), ["goal", "red_card"])


class TestCompactLiveScores(unittest.TestCase):
    def test_to_compact_live_game_strips_heavy_fields(self):
        from live_poller import to_compact_live_game

        full_game = {
            "match_id": "700123",
            "competition": "England/Premier League",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "home_score": 2,
            "away_score": 1,
            "status": "in",
            "status_type": "in",
            "period": "2nd Half",
            "period_number": 2,
            "clock": "68'",
            "kickoff_utc": "2026-10-08T19:00:00Z",
            "match_date": "2026-10-08",
            "round": "Matchweek 8",
            # Heavy fields to be stripped:
            "lineups": {"home": {"startXI": [{"name": "Saka"}]}, "away": {}},
            "key_events": [{"type": "goal", "text": "Goal by Saka"}],
            "goalscorers": [{"scorer": "Saka", "minute": "23'"}],
            "boxscore_stats": {"passes": 340},
            "shot_mapping": {"shot_origins": [{"x": 10, "y": 20}]},
            "injuries_availability": [{"player": "Odegaard"}],
            "head_to_head": [{"date": "2025-01-01"}],
            "last_five": [{"result": "W"}],
            "team_stats": {"possession": "55%"},
            "home_stats": {"shots": 12},
            "away_stats": {"shots": 8},
        }

        compact = to_compact_live_game(full_game)

        # Retained fields
        self.assertEqual(compact["match_id"], "700123")
        self.assertEqual(compact["competition"], "England/Premier League")
        self.assertEqual(compact["home_team"], "Arsenal")
        self.assertEqual(compact["away_team"], "Chelsea")
        self.assertEqual(compact["home_score"], 2)
        self.assertEqual(compact["away_score"], 1)
        self.assertEqual(compact["status"], "in")
        self.assertEqual(compact["clock"], "68'")

        # Excluded heavy fields
        self.assertNotIn("lineups", compact)
        self.assertNotIn("key_events", compact)
        self.assertNotIn("goalscorers", compact)
        self.assertNotIn("boxscore_stats", compact)
        self.assertNotIn("shot_mapping", compact)
        self.assertNotIn("injuries_availability", compact)
        self.assertNotIn("head_to_head", compact)
        self.assertNotIn("last_five", compact)
        self.assertNotIn("team_stats", compact)
        self.assertNotIn("home_stats", compact)
        self.assertNotIn("away_stats", compact)

    def test_compact_cache_payload(self):
        import json
        from live_poller import get_cached_compact_live_scores_payload

        raw_bytes, etag = get_cached_compact_live_scores_payload()
        self.assertIsInstance(raw_bytes, bytes)
        self.assertTrue(len(raw_bytes) > 0)
        self.assertTrue(etag.startswith('"') and etag.endswith('"'))

        payload = json.loads(raw_bytes.decode("utf-8"))
        self.assertTrue(payload.get("ok"))
        self.assertTrue(payload.get("compact"))


if __name__ == "__main__":
    unittest.main()

