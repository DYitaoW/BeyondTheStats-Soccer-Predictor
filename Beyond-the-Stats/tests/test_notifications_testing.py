"""Unit tests for APNs push and Live Activity testing & simulation endpoints."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add Website and root to path
_ROOT = Path(__file__).resolve().parent.parent
_WEBSITE = _ROOT / "Website"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_WEBSITE) not in sys.path:
    sys.path.insert(0, str(_WEBSITE))

from fastapi.testclient import TestClient
from Website.app import app
import notifications


class TestNotificationsTestingSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        # Reset any simulated matches
        self.client.post("/api/test/simulate-game/reset", json={"match_id": "sim-999"})

    def test_status_endpoint(self):
        """GET /api/test/notifications/status returns system diagnostic info."""
        res = self.client.get("/api/test/notifications/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("credentials", data)
        self.assertIn("apns_settings", data)
        self.assertIn("jwt", data)
        self.assertIn("in_memory_state", data)

    def test_env_endpoint(self):
        """GET /api/test/env returns active backend environment variables masked."""
        res = self.client.get("/api/test/env")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("apns", data)
        self.assertIn("pipeline", data)
        self.assertIn("server", data)

    def test_simulate_game_lifecycle(self):
        """Test starting, scoring, and ending a fake live game."""
        # 1. Start simulated match
        start_res = self.client.post("/api/test/simulate-game/start", json={
            "match_id": "sim-999",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "device_token": "fake_device_token_12345",
        })
        self.assertEqual(start_res.status_code, 200)
        start_data = start_res.json()
        self.assertTrue(start_data.get("ok"))
        self.assertEqual(start_data["game"]["match_id"], "sim-999")
        self.assertEqual(start_data["game"]["home_score"], 0)

        # 2. Check game appears in startup endpoint
        boot_res = self.client.get("/api/startup")
        self.assertEqual(boot_res.status_code, 200)
        boot_data = boot_res.json()
        sim_comp = boot_data["live_scores"]["competitions"].get("Test/Simulation")
        self.assertIsNotNone(sim_comp)
        self.assertTrue(any(g["match_id"] == "sim-999" for g in sim_comp["games"]))

        # 3. Trigger a home goal event
        goal_res = self.client.post("/api/test/simulate-game/event", json={
            "match_id": "sim-999",
            "event": "goal_home",
            "player": "B. Saka",
            "minute": "23'",
        })
        self.assertEqual(goal_res.status_code, 200)
        goal_data = goal_res.json()
        self.assertTrue(goal_data.get("ok"))
        self.assertEqual(goal_data["canonical_event"], "goal")
        self.assertEqual(goal_data["game_current_state"]["home_score"], 1)
        self.assertIn("B. Saka", goal_data["notification"]["body"])
        # Device was auto-subscribed on start, so subscriber count must be >= 1
        self.assertGreaterEqual(goal_data["notification"]["subscribers_notified_count"], 1)

        # 4. Trigger Full-Time event
        ft_res = self.client.post("/api/test/simulate-game/event", json={
            "match_id": "sim-999",
            "event": "ft",
        })
        self.assertEqual(ft_res.status_code, 200)
        ft_data = ft_res.json()
        self.assertTrue(ft_data.get("ok"))
        self.assertEqual(ft_data["canonical_event"], "end")
        self.assertTrue(ft_data["live_activity"]["ended"])
        self.assertEqual(ft_data["game_current_state"]["status"], "post")

        # 5. Reset/clean up
        reset_res = self.client.post("/api/test/simulate-game/reset", json={"match_id": "sim-999"})
        self.assertEqual(reset_res.status_code, 200)
        self.assertTrue(reset_res.json().get("ok"))

    @patch("notifications.apns_send_detailed")
    def test_direct_push_endpoint(self, mock_send):
        """POST /api/test/notifications/push calls apns_send_detailed and returns result."""
        mock_send.return_value = {
            "ok": True,
            "status_code": 200,
            "apns_id": "test-uuid-1234",
            "reason": None,
            "endpoint": "https://api.development.push.apple.com/3/device/sample_token",
            "topic": "Yitao.BeyondTheStatsApp",
            "sandbox": True,
            "push_type": "alert",
            "jwt_generated": True,
            "error": None,
            "hint": "Success",
        }
        res = self.client.post("/api/test/notifications/push", json={
            "device_token": "sample_token",
            "title": "Test Title",
            "body": "Test Message",
            "sandbox": True,
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("successful_count"), 1)
        mock_send.assert_called_once()


if __name__ == "__main__":
    unittest.main()

