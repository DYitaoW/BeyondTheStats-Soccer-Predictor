"""Tests for in-depth match prediction statistics in single match API endpoint (/api/match/<match_id>)."""

import os
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

# Ensure project and Website paths
_ROOT = Path(__file__).resolve().parent.parent
_WEBSITE = _ROOT / "Website"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_WEBSITE) not in sys.path:
    sys.path.insert(0, str(_WEBSITE))

from starlette.testclient import TestClient
from Website.app import app
from routes.live_scores import clear_match_detail_cache
from shared import sqlite_store


class TestMatchDetailPredictionStats(unittest.TestCase):
    def setUp(self):
        clear_match_detail_cache()
        self.client = TestClient(app)

    def tearDown(self):
        clear_match_detail_cache()

    def test_match_detail_by_sync_key_returns_all_in_depth_stats(self):
        """Verify requesting single match by sync_key returns full in-depth prediction stats."""
        # Find an upcoming game from SQLite
        upcoming = sqlite_store.load_upcoming_games(limit=1)
        if not upcoming:
            self.skipTest("No upcoming games found in SQLite to test.")

        fixture = upcoming[0]
        sync_k = fixture.get("sync_key")
        self.assertTrue(sync_k, "Upcoming game must have sync_key")

        resp = self.client.get(f"/api/match/{sync_k}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        game = data.get("game", {})

        # Assert distributions are present and populated
        self.assertIn("correct_score_dist", game)
        self.assertIsInstance(game["correct_score_dist"], list)
        self.assertGreater(len(game["correct_score_dist"]), 0)

        self.assertIn("asian_handicap", game)
        self.assertIsInstance(game["asian_handicap"], dict)
        self.assertGreater(len(game["asian_handicap"]), 0)

        self.assertIn("total_goals_dist", game)
        self.assertIsInstance(game["total_goals_dist"], dict)
        self.assertGreater(len(game["total_goals_dist"]), 0)

        self.assertIn("first_to_score", game)
        self.assertIsInstance(game["first_to_score"], dict)
        self.assertIn("home", game["first_to_score"])
        self.assertIn("away", game["first_to_score"])

        self.assertIn("clean_sheet", game)
        self.assertIsInstance(game["clean_sheet"], dict)
        self.assertIn("home", game["clean_sheet"])
        self.assertIn("away", game["clean_sheet"])

        self.assertIn("double_chance", game)
        self.assertIsInstance(game["double_chance"], dict)

        # Assert probabilities and goals
        self.assertIn("prob_home", game)
        self.assertIn("prob_draw", game)
        self.assertIn("prob_away", game)
        self.assertIn("prob_home_text", game)
        self.assertIn("prob_draw_text", game)
        self.assertIn("prob_away_text", game)
        self.assertIn("pred_home_goals", game)
        self.assertIn("pred_away_goals", game)

        # Assert secondary goal probabilities
        self.assertIn("prob_both_score", game)
        self.assertIn("prob_over_1_5", game)
        self.assertIn("prob_over_2_5", game)
        self.assertIn("prob_over_3_5", game)

        # Assert nested predictions dicts
        self.assertIn("upcoming_prediction", game)
        self.assertIsInstance(game["upcoming_prediction"], dict)
        up = game["upcoming_prediction"]
        self.assertEqual(up.get("correct_score_dist"), game.get("correct_score_dist"))
        self.assertEqual(up.get("asian_handicap"), game.get("asian_handicap"))

    def test_match_detail_by_url_encoded_sync_key(self):
        """Verify URL-encoded sync_key path resolves correctly."""
        upcoming = sqlite_store.load_upcoming_games(limit=1)
        if not upcoming:
            self.skipTest("No upcoming games found in SQLite to test.")

        sync_k = upcoming[0]["sync_key"]
        encoded_k = urllib.parse.quote(sync_k)

        resp = self.client.get(f"/api/match/{encoded_k}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("game", {}).get("sync_key"), sync_k)

    def test_match_detail_by_query_params(self):
        """Verify query-based parameters match_id and sync_key return the match."""
        upcoming = sqlite_store.load_upcoming_games(limit=1)
        if not upcoming:
            self.skipTest("No upcoming games found in SQLite to test.")

        sync_k = upcoming[0]["sync_key"]

        # 1. Query by ?match_id=...
        resp1 = self.client.get(f"/api/match?match_id={sync_k}")
        self.assertEqual(resp1.status_code, 200)
        self.assertTrue(resp1.json().get("ok"))

        # 2. Query by ?sync_key=...
        resp2 = self.client.get(f"/api/match?sync_key={sync_k}")
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(resp2.json().get("ok"))

    def test_dynamic_distribution_fallbacks_when_raw_prediction_provided(self):
        """Verify that when raw model probabilities exist without pre-computed distributions, they are dynamically computed."""
        dummy_mid = "dynamic-math-test-999"
        dummy_payload = {
            "match_id": dummy_mid,
            "competition": "English Premier League",
            "home_team": "Newcastle",
            "away_team": "Aston Villa",
            "pred_home_goals": 2,
            "pred_away_goals": 1,
            "prob_home": 52.0,
            "prob_draw": 26.0,
            "prob_away": 22.0,
            "predicted_result": "H",
        }

        with patch("predictions._build_past_game_prediction_lookup", return_value={f"mid:{dummy_mid}": dummy_payload}):
            resp = self.client.get(f"/api/match/{dummy_mid}")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data.get("ok"))
            game = data.get("game", {})

            # Dynamically generated distributions
            self.assertIsInstance(game.get("correct_score_dist"), list)
            self.assertGreater(len(game["correct_score_dist"]), 0)
            self.assertIsInstance(game.get("asian_handicap"), dict)
            self.assertGreater(len(game["asian_handicap"]), 0)
            self.assertIsInstance(game.get("total_goals_dist"), dict)
            self.assertIn("6plus", game["total_goals_dist"])
            self.assertIsInstance(game.get("first_to_score"), dict)
            self.assertIn("home", game["first_to_score"])
            self.assertIsInstance(game.get("clean_sheet"), dict)
            self.assertIn("home", game["clean_sheet"])
            self.assertIsInstance(game.get("double_chance"), dict)
            self.assertIn("home_or_draw", game["double_chance"])

            # Dynamically generated goal markets
            self.assertIsNotNone(game.get("prob_both_score"))
            self.assertIsNotNone(game.get("prob_over_2_5"))
            self.assertEqual(game.get("prob_home_text"), "52.0%")
            self.assertIn("Newcastle", game.get("winner_label", ""))

    def test_parity_between_upcoming_global_and_single_match(self):
        """Verify exact field parity between /api/upcoming/global and /api/match/<sync_key>."""
        up_resp = self.client.get("/api/upcoming/global?limit=1")
        self.assertEqual(up_resp.status_code, 200)
        up_data = up_resp.json()
        if not up_data.get("rows"):
            self.skipTest("No global upcoming rows available.")

        up_row = up_data["rows"][0]
        sync_k = up_row.get("sync_key")

        match_resp = self.client.get(f"/api/match/{sync_k}")
        self.assertEqual(match_resp.status_code, 200)
        match_game = match_resp.json().get("game", {})

        missing_fields = set(up_row.keys()) - set(match_game.keys())
        self.assertEqual(missing_fields, set(), f"Single match is missing fields present in upcoming: {missing_fields}")


if __name__ == "__main__":
    unittest.main()

