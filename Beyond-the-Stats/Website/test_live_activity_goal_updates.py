"""Tests for Live Activity goal → score update pushes."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


WEBSITE_DIR = Path(__file__).resolve().parent
if str(WEBSITE_DIR) not in sys.path:
    sys.path.insert(0, str(WEBSITE_DIR))


class LiveActivityGoalUpdateTests(unittest.TestCase):
    def setUp(self):
        import notifications as n

        n._live_activities.clear()
        n._match_notification_subscriptions.clear()
        n._apns_notification_queue.clear()
        self.n = n

    def test_register_normalizes_competition_alias(self):
        ok = self.n.register("act-token-1", "dev-token-1", "12345", "FIFA/World Cup")
        self.assertTrue(ok)
        # Poller uses International/World Cup — lookup must still find the token.
        found = self.n.for_match("12345", "International/World Cup")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["activity_token"], "act-token-1")

    def test_send_live_activity_update_queues_content_state_with_scores(self):
        self.n.register("act-token-2", "dev-token-2", "999", "England/Premier League")
        sent = self.n.send_live_activity_update(
            "999",
            "England/Premier League",
            {
                "home_team": "Arsenal",
                "away_team": "Chelsea",
                "home_score": 2,
                "away_score": 1,
                "status": "in",
                "event": "goal",
                "clock": "67'",
            },
        )
        self.assertEqual(sent, 1)
        self.assertEqual(len(self.n._apns_notification_queue), 1)
        entry = self.n._apns_notification_queue[0]
        self.assertEqual(entry["type"], "liveactivity")
        self.assertEqual(entry["event"], "update")  # APNs aps.event
        self.assertEqual(entry["token"], "act-token-2")
        state = entry["content_state"]
        self.assertEqual(state["home_score"], 2)
        self.assertEqual(state["away_score"], 1)
        self.assertEqual(state["event"], "goal")  # UI event label

    def test_scoring_event_types_push_live_activity(self):
        """Own goals and penalties must also refresh Live Activity scores."""
        source = (WEBSITE_DIR / "live_poller.py").read_text(encoding="utf-8")
        self.assertIn('_SCORING_EVENT_TYPES = frozenset({"goal", "own goal", "penalty"})', source)
        self.assertIn("send_live_activity_update(mid, comp_name, la_state)", source)
        self.assertIn('score_state["event"] = "score"', source)
        self.assertIn("send_live_activity_end(mid, comp_name, state)", source)

    def test_score_change_helper_used_by_poller(self):
        source = (WEBSITE_DIR / "live_poller.py").read_text(encoding="utf-8")
        # Raw queue appends for LA updates should not remain in the poller paths.
        self.assertNotIn('"type": "liveactivity"', source.replace(" ", ""))
        # Prefer the shared helper so goal + score paths stay consistent.
        self.assertIn("send_live_activity_update", source)
        self.assertIn("send_live_activity_end", source)

    def test_drain_queue_builds_liveactivity_payload(self):
        self.n.register("act-token-3", "", "555", "England/Premier League")
        self.n.send_live_activity_update(
            "555",
            "England/Premier League",
            {"home_score": 1, "away_score": 0, "event": "goal"},
        )
        sent_payloads = []

        def _fake_send(token, payload, topic, live_activity=False):
            sent_payloads.append((token, payload, topic, live_activity))
            return True

        with mock.patch.object(self.n, "_apns_send", side_effect=_fake_send):
            with mock.patch.object(self.n, "_generate_apns_jwt", return_value="jwt"):
                with mock.patch.dict(
                    "os.environ",
                    {},
                    clear=False,
                ):
                    import config

                    with mock.patch.object(config, "APNS_LIVE_ACTIVITY_TOPIC", "bundle.push-type.liveactivity"):
                        with mock.patch.object(config, "APNS_TOPIC", "bundle"):
                            self.n._drain_queue()

        self.assertEqual(len(sent_payloads), 1)
        token, payload, topic, is_la = sent_payloads[0]
        self.assertTrue(is_la)
        self.assertEqual(token, "act-token-3")
        self.assertEqual(payload["aps"]["event"], "update")
        self.assertEqual(payload["aps"]["content-state"]["home_score"], 1)
        self.assertEqual(payload["aps"]["content-state"]["event"], "goal")


if __name__ == "__main__":
    unittest.main()
