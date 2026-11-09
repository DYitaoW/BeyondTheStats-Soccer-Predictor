"""
Unit and integration tests for Team Overview API and Match Name Formatting.
Asserts:
1. Match API returns 3-tier team names (full, short, abbreviation), id, logo, and canonical names.
2. Team Overview API returns full profile (3-tier names, primary league, active competitions,
   season averages, ratings, last 5 matches across all competitions, last match lineup,
   predicted finish across competitions).
3. Pre-caching mechanism runs and populates memory cache for top European and MLS teams.
"""

import unittest
from starlette.testclient import TestClient

from Website.app import app
from Website.team_overview import build_team_overview, precache_popular_teams, _TEAM_OVERVIEW_CACHE


class TestTeamOverviewAndMatchNames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_match_api_contains_short_and_abbreviated_names(self):
        """Verify GET /api/match returns standardized full, short, and abbreviated names."""
        res = self.client.get("/api/match?sync_key=2026-10-24|arsenal|everton")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        game = data.get("game", {})

        # Assert primary home & away names
        self.assertEqual(game.get("home_team"), "Arsenal")
        self.assertEqual(game.get("canonical_home_team"), "Arsenal")
        self.assertEqual(game.get("home_team_full"), "Arsenal")
        self.assertEqual(game.get("home_team_short"), "Arsenal")
        self.assertEqual(game.get("home_team_abbr"), "ARS")

        self.assertEqual(game.get("away_team"), "Everton")
        self.assertEqual(game.get("canonical_away_team"), "Everton")
        self.assertEqual(game.get("away_team_full"), "Everton")
        self.assertEqual(game.get("away_team_short"), "Everton")
        self.assertEqual(game.get("away_team_abbr"), "EVE")

        # Assert upcoming_prediction snapshot also decorated
        up = game.get("upcoming_prediction", {})
        self.assertEqual(up.get("home_team_abbr"), "ARS")
        self.assertEqual(up.get("away_team_abbr"), "EVE")

    def test_team_overview_structure_and_fields(self):
        """Verify build_team_overview returns complete 3-tier names, stats, lineup, and projections."""
        payload = build_team_overview("Arsenal")
        self.assertTrue(payload.get("ok"))

        # 3-tier names
        self.assertEqual(payload.get("team_full"), "Arsenal")
        self.assertEqual(payload.get("team_short"), "Arsenal")
        self.assertEqual(payload.get("team_abbr"), "ARS")
        self.assertEqual(payload.get("team_id"), "arsenal")

        # League and active competitions
        self.assertEqual(payload.get("primary_league"), "England/Premier League")
        self.assertIn("England/Premier League", payload.get("active_competitions", []))
        self.assertIn("Europe/Champions League", payload.get("active_competitions", []))

        # Season averages computed from real match data
        averages = payload.get("season_averages", {})
        self.assertGreater(averages.get("matches_recorded", 0), 0)
        self.assertIsNotNone(averages.get("shots_per_game"))
        self.assertIsNotNone(averages.get("corners_per_game"))
        self.assertIsNotNone(averages.get("possession_pct"))

        # Ratings
        ratings = payload.get("ratings", {})
        self.assertIsNotNone(ratings.get("attack_rating"))
        self.assertIsNotNone(ratings.get("defence_rating"))
        self.assertIsNotNone(ratings.get("overall_rating"))

        # Last game lineup
        lineup = payload.get("last_game_lineup")
        self.assertIsNotNone(lineup)
        self.assertTrue(len(lineup.get("startXI", [])) > 0)
        self.assertTrue(len(lineup.get("substitutes", [])) > 0)

        # Predicted finishes
        finishes = payload.get("predicted_season_finishes", [])
        self.assertTrue(len(finishes) > 0)
        pl_finish = next((f for f in finishes if f["competition"] == "England/Premier League"), None)
        self.assertIsNotNone(pl_finish)
        self.assertIn("predicted_position", pl_finish)
        self.assertIn("win_league_pct", pl_finish)

        # Last 5 matches & momentum
        self.assertEqual(len(payload.get("last_5_matches", [])), 5)
        momentum = payload.get("momentum", {})
        self.assertIn("unbeaten_streak", momentum)
        self.assertIn("form_guide", momentum)

    def test_team_overview_endpoints(self):
        """Verify /api/team/<team>/overview, /api/team?team=...&overview=true, and /api/team-overview."""
        # 1. Path style
        res1 = self.client.get("/api/team/Arsenal/overview")
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.json().get("team_abbr"), "ARS")

        # 2. Query overview=true style
        res2 = self.client.get("/api/team/Chelsea?overview=true")
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.json().get("team_abbr"), "CHE")

        # 3. Dedicated endpoint
        res3 = self.client.get("/api/team-overview?team=Real Madrid")
        self.assertEqual(res3.status_code, 200)
        self.assertEqual(res3.json().get("primary_league"), "Spain/La Liga")

    def test_popular_teams_precaching(self):
        """Verify pre-caching populates memory cache for popular teams."""
        count = precache_popular_teams()
        self.assertGreater(count, 50)
        self.assertIn("arsenal", _TEAM_OVERVIEW_CACHE)
        self.assertIn("chelsea", _TEAM_OVERVIEW_CACHE)


if __name__ == "__main__":
    unittest.main()

