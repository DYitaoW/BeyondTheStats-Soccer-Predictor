import unittest
import sys
import os

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_THIS_DIR)
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)
_WEBSITE_DIR = os.path.join(_PROJECT_DIR, "Website")
if _WEBSITE_DIR not in sys.path:
    sys.path.insert(0, _WEBSITE_DIR)

from Website import config, accuracy_tracker
from pipelines.europe.files import Process_National_Team_Data as national
from pipelines.europe.files import Predict_Upcoming_National_Team_Games as pred
from pipelines.europe.files import Update_Live_Prediction_Results as update_res
from pipelines.europe.files import fetch_league_teams as fetch_teams


class TestNationalCompetitionsConfig(unittest.TestCase):
    def test_live_score_competitions(self):
        # UEFA Nations League
        self.assertEqual(config.LIVE_SCORE_COMPETITIONS.get("International/Nations League"), "uefa.nations")
        # Euros
        self.assertEqual(config.LIVE_SCORE_COMPETITIONS.get("International/European Championship"), "uefa.euro")
        self.assertEqual(config.LIVE_SCORE_COMPETITIONS.get("Europe/European Championship"), "uefa.euro")
        # CONCACAF Nations League
        self.assertEqual(config.LIVE_SCORE_COMPETITIONS.get("North America/Concacaf Nations League"), "concacaf.nations.league")
        self.assertEqual(config.LIVE_SCORE_COMPETITIONS.get("North America/CONCACAF Nations League"), "concacaf.nations.league")

    def test_tournament_key_map(self):
        self.assertEqual(config.TOURNAMENT_KEY_MAP.get("nations-league"), "International/Nations League")
        self.assertEqual(config.TOURNAMENT_KEY_MAP.get("concacaf-nations-league"), "North America/Concacaf Nations League")
        self.assertEqual(config.TOURNAMENT_KEY_MAP.get("euros"), "International/European Championship")

    def test_is_national_team_competition(self):
        self.assertTrue(config.is_national_team_competition("International/Nations League"))
        self.assertTrue(config.is_national_team_competition("International/European Championship"))
        self.assertTrue(config.is_national_team_competition("North America/Concacaf Nations League"))
        self.assertTrue(config.is_national_team_competition("International/World Cup"))
        self.assertTrue(config.is_national_team_competition("South America/Copa America"))
        self.assertFalse(config.is_national_team_competition("England/Premier League"))
        self.assertFalse(config.is_national_team_competition("United States/MLS"))

    def test_process_national_team_data_competitions(self):
        self.assertIn("North America/Concacaf Nations League", national.UPCOMING_ESPN_COMPETITIONS)
        self.assertEqual(national.UPCOMING_ESPN_COMPETITIONS["North America/Concacaf Nations League"]["espn_id"], "concacaf.nations.league")
        self.assertIn("International/Nations League", national.UPCOMING_ESPN_COMPETITIONS)
        self.assertEqual(national.UPCOMING_ESPN_COMPETITIONS["International/Nations League"]["espn_id"], "uefa.nations")
        self.assertIn("International/European Championship", national.UPCOMING_ESPN_COMPETITIONS)
        self.assertEqual(national.UPCOMING_ESPN_COMPETITIONS["International/European Championship"]["espn_id"], "uefa.euro")

        # RECENT_ESPN_COMPETITIONS should also inherit CONCACAF Nations League
        self.assertIn("North America/Concacaf Nations League", national.RECENT_ESPN_COMPETITIONS)

    def test_predict_upcoming_national_team_games_configs(self):
        configs = pred.competition_configs(world_cup_only=False, friendlies_only=True)
        self.assertIn("North America/Concacaf Nations League", configs)
        self.assertIn("International/Nations League", configs)
        self.assertIn("International/Friendly", configs)
        self.assertNotIn("International/World Cup", configs)

        all_configs = pred.competition_configs(world_cup_only=False, friendlies_only=False)
        self.assertIn("North America/Concacaf Nations League", all_configs)
        self.assertIn("International/World Cup", all_configs)

    def test_accuracy_and_update_mappings(self):
        self.assertEqual(accuracy_tracker._UPCOMING_CSV_MODE_MAP.get("North America/Concacaf Nations League"), "national")
        self.assertEqual(update_res.ESPN_COMPETITION_KEYS.get("North America/Concacaf Nations League"), "concacaf.nations.league")
        self.assertEqual(update_res.ESPN_COMPETITION_KEYS.get("International/Nations League"), "uefa.nations")
        self.assertEqual(fetch_teams.LEAGUES.get("North America/Concacaf Nations League"), "concacaf.nations.league")


if __name__ == "__main__":
    unittest.main()
