"""Tests for comprehensive match analytics, cross-competition form, H2H, momentum, HT/FT, and xPTS."""
import os
import sys
import unittest
from pathlib import Path

# Ensure Beyond-the-Stats and Website are on sys.path
_TEST_DIR = Path(__file__).resolve().parent
_BTS_DIR = _TEST_DIR.parent
_WEBSITE_DIR = _BTS_DIR / "Website"
if str(_BTS_DIR) not in sys.path:
    sys.path.insert(0, str(_BTS_DIR))
if str(_WEBSITE_DIR) not in sys.path:
    sys.path.insert(0, str(_WEBSITE_DIR))

from starlette.testclient import TestClient
from app import app
import math_utils
import match_analytics


class TestMatchAnalyticsAndStats(unittest.TestCase):
    """Test comprehensive match analytics, form, H2H, streaks, and secondary markets."""

    def setUp(self):
        self.client = TestClient(app)

    def test_math_utils_expected_points(self):
        """Expected Points (xPTS) should correctly compute from 1X2 probabilities."""
        xpts = math_utils._compute_expected_points(60.0, 25.0, 15.0)
        # 3 * 0.60 + 1 * 0.25 = 1.80 + 0.25 = 2.05
        # 3 * 0.15 + 1 * 0.25 = 0.45 + 0.25 = 0.70
        self.assertAlmostEqual(xpts["xpts_home"], 2.05, delta=0.05)
        self.assertAlmostEqual(xpts["xpts_away"], 0.70, delta=0.05)

    def test_math_utils_halftime_and_htft(self):
        """Half-time distributions and HT/FT probabilities should be mathematically sound."""
        ht = math_utils._compute_halftime_distributions(2.0, 1.0)
        self.assertIn("pred_home_goals_ht", ht)
        self.assertIn("pred_away_goals_ht", ht)
        self.assertGreater(ht["prob_home_ht"], 0.0)
        self.assertGreater(ht["prob_draw_ht"], 0.0)
        self.assertEqual(len(ht["ht_correct_score_dist"]), 6)

        htft = math_utils._compute_htft_probabilities(2.0, 1.0)
        self.assertIn("prob_htft", htft)
        self.assertIn("predicted_htft", htft)
        self.assertIn("prob_highest_scoring_half", htft)
        # Sum of 9 outcomes should equal 100%
        self.assertAlmostEqual(sum(htft["prob_htft"].values()), 100.0, delta=0.5)

    def test_math_utils_combos_and_predicted_stats(self):
        """Combo markets and predicted match stats should be properly formatted."""
        combos = math_utils._compute_combo_markets(2.0, 1.0)
        self.assertIn("prob_home_win_and_over_2_5", combos)
        self.assertIn("prob_btts_and_over_2_5", combos)
        self.assertIn("prob_home_win_clean_sheet", combos)

        stats = math_utils._compute_predicted_team_stats(2.0, 1.0)
        self.assertGreater(stats["pred_home_shots"], stats["pred_away_shots"])
        self.assertAlmostEqual(stats["pred_home_possession"] + stats["pred_away_possession"], 100.0, delta=0.5)

    def test_comprehensive_form_includes_cups_and_continental(self):
        """get_team_comprehensive_form should return matches across domestic leagues, cups, and continental comps."""
        matches = match_analytics.get_team_comprehensive_form("Arsenal", limit=10)
        self.assertGreater(len(matches), 0)
        comps = {m["competition"] for m in matches}
        # Arsenal plays across Premier League, League Cup, and Champions League
        self.assertTrue(any("league" in c.lower() or "champions" in c.lower() for c in comps))

    def test_head_to_head_and_momentum(self):
        """Head-to-Head and momentum metrics should be generated accurately."""
        h2h_matches, h2h_summary = match_analytics.get_head_to_head("Arsenal", "Everton", limit=5)
        self.assertGreater(len(h2h_matches), 0)
        self.assertIn("total_meetings", h2h_summary)
        self.assertIn("home_team_wins", h2h_summary)
        self.assertIn("btts_pct", h2h_summary)

        momentum = match_analytics.compute_team_momentum(h2h_matches)
        self.assertIn("unbeaten_streak", momentum)
        self.assertIn("form_guide", momentum)
        self.assertIn("points_last_5", momentum)

    def test_api_match_detail_includes_all_new_stats(self):
        """GET /api/match should return all new analytics, HT/FT, xPTS, and momentum fields."""
        resp = self.client.get("/api/match?sync_key=2026-10-24|arsenal|everton")
        self.assertEqual(resp.status_code, 200)
        game = resp.json().get("game", {})

        # Assert cross-competition form
        self.assertEqual(len(game.get("last_5_home", [])), 5)
        self.assertEqual(len(game.get("last_5_away", [])), 5)
        self.assertGreater(len(game.get("last_5_home_at_home", [])), 0)

        # Assert H2H
        self.assertGreater(len(game.get("head_to_head", [])), 0)
        self.assertIn("total_meetings", game.get("h2h_summary", {}))

        # Assert Momentum & Streaks
        self.assertIn("unbeaten_streak", game.get("home_momentum", {}))
        self.assertIn("form_guide", game.get("home_momentum", {}))

        # Assert Expected Points & HT/FT
        self.assertIn("xpts_home", game)
        self.assertIn("xpts_away", game)
        self.assertIn("halftime_prediction", game)
        self.assertIn("htft_prediction", game)
        self.assertIn("predicted_htft", game)

        # Assert Combo Markets & Predicted Stats
        self.assertIn("combo_markets", game)
        self.assertIn("pred_home_corners", game)
        self.assertIn("pred_home_possession", game)


if __name__ == "__main__":
    unittest.main()

