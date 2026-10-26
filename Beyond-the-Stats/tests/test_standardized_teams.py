import os
import sys
import unittest

# Ensure project directories are in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEBSITE_DIR = os.path.join(BASE_DIR, "Website")
for p in (BASE_DIR, WEBSITE_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from Website.standardized_teams import (
    StandardizedTeamManager,
    resolve_team_info,
    decorate_game_team_names,
    decorate_standings_entry,
    decorate_standings_table,
    generate_fallback_team_info,
    get_standardized_teams_list,
    get_standardized_team_manager,
)
from Website.team_utils import resolve_team_info as tu_resolve_team_info


class TestStandardizedTeams(unittest.TestCase):
    def setUp(self):
        self.mgr = get_standardized_team_manager()

    def test_manager_loaded_teams(self):
        teams = get_standardized_teams_list()
        self.assertGreater(len(teams), 100, "Should have loaded 100+ standardized teams from mappings")

    def test_known_team_resolution(self):
        # 1. Arsenal
        info = resolve_team_info("Arsenal", "English Premier League")
        self.assertEqual(info["id"], "arsenal")
        self.assertEqual(info["abbreviation"], "ARS")
        self.assertEqual(info["short_name"], "Arsenal")
        self.assertEqual(info["full_name"], "Arsenal")
        self.assertEqual(info["logo_file"], "arsenal.png")

        # 2. Brighton
        info_bha = resolve_team_info("Brighton & Hove Albion")
        self.assertEqual(info_bha["id"], "brighton-hove-albion")
        self.assertEqual(info_bha["full_name"], "Brighton & Hove Albion")
        self.assertEqual(info_bha["short_name"], "Brighton")
        self.assertEqual(info_bha["abbreviation"], "BHA")

        # 3. Manchester City
        info_mci = resolve_team_info("Man City")
        self.assertEqual(info_mci["full_name"], "Manchester City")
        self.assertEqual(info_mci["short_name"], "Man City")
        self.assertEqual(info_mci["abbreviation"], "MCI")

        # 4. Spurs
        info_spurs = resolve_team_info("Tottenham Hotspur")
        self.assertEqual(info_spurs["abbreviation"], "TOT")

        # 5. Inter Milan
        info_inter = resolve_team_info("Inter Milan", "Italian Serie A")
        self.assertEqual(info_inter["id"], "fc-internazionale-milano")
        self.assertEqual(info_inter["short_name"], "Inter")
        self.assertEqual(info_inter["abbreviation"], "INT")

        # 6. Bayern Munich
        info_bayern = resolve_team_info("Bayern Munich", "German Bundesliga")
        self.assertEqual(info_bayern["id"], "fc-bayern-munich")
        self.assertEqual(info_bayern["short_name"], "Bayern")
        self.assertEqual(info_bayern["abbreviation"], "BAY")

        # 7. Real Madrid
        info_rma = resolve_team_info("Real Madrid", "Spanish La Liga")
        self.assertEqual(info_rma["id"], "real-madrid")
        self.assertEqual(info_rma["abbreviation"], "RMA")

    def test_team_utils_export(self):
        # Verify team_utils correctly proxies to standardized_teams
        info = tu_resolve_team_info("Arsenal")
        self.assertEqual(info["id"], "arsenal")
        self.assertEqual(info["abbreviation"], "ARS")

    def test_unmapped_fallback_heuristic(self):
        # Obscure or new team not in the JSON mapping
        raw_name = "FC Super Nova Utd 2026"
        info = generate_fallback_team_info(raw_name, "Latvian Higher League")

        self.assertIsNotNone(info)
        self.assertIn("id", info)
        self.assertIn("full_name", info)
        self.assertIn("short_name", info)
        self.assertIn("abbreviation", info)
        self.assertIn("logo_file", info)

        # Abbreviation should be 3 uppercase characters
        self.assertEqual(len(info["abbreviation"]), 3)
        self.assertTrue(info["abbreviation"].isupper())

        # Utd should expand to United in full_name
        self.assertIn("United", info["full_name"])

        # Logo file should match id
        self.assertEqual(info["logo_file"], f"{info['id']}.png")

    def test_special_characters_fallback(self):
        # Accented characters and slashes
        info = resolve_team_info("Bodø / Glimt", "Norway Eliteserien")
        self.assertIsNotNone(info)
        self.assertEqual(len(info["abbreviation"]), 3)
        self.assertTrue(info["abbreviation"].isalpha())
        self.assertTrue(info["abbreviation"].isupper())

    def test_decorate_game_team_names_preserves_backend_data(self):
        game = {
            "sync_key": "2026-04-15|england_premier_league|arsenal|chelsea",
            "match_id": "123456",
            "competition": "English Premier League",
            "home_team": "Brighton & Hove Albion",
            "away_team": "Manchester City",
            "prob_home": 0.25,
            "prob_draw": 0.25,
            "prob_away": 0.50,
            "predicted_result": "A",
            "actual_home_goals": 1,
            "actual_away_goals": 3,
        }

        decorated = decorate_game_team_names(dict(game), "English Premier League")

        # Core prediction & training keys must remain 100% intact
        self.assertEqual(decorated["home_team"], "Brighton & Hove Albion")
        self.assertEqual(decorated["away_team"], "Manchester City")
        self.assertEqual(decorated["prob_home"], 0.25)
        self.assertEqual(decorated["prob_draw"], 0.25)
        self.assertEqual(decorated["prob_away"], 0.50)
        self.assertEqual(decorated["predicted_result"], "A")
        self.assertEqual(decorated["sync_key"], "2026-04-15|england_premier_league|arsenal|chelsea")
        self.assertEqual(decorated["match_id"], "123456")

        # Standardized 3-tier names and metadata must be added
        self.assertEqual(decorated["home_team_full"], "Brighton & Hove Albion")
        self.assertEqual(decorated["home_team_short"], "Brighton")
        self.assertEqual(decorated["home_team_abbr"], "BHA")
        self.assertEqual(decorated["home_team_id"], "brighton-hove-albion")
        self.assertIsInstance(decorated["home_team_info"], dict)

        self.assertEqual(decorated["away_team_full"], "Manchester City")
        self.assertEqual(decorated["away_team_short"], "Man City")
        self.assertEqual(decorated["away_team_abbr"], "MCI")
        self.assertEqual(decorated["away_team_id"], "manchester-city")
        self.assertIsInstance(decorated["away_team_info"], dict)

    def test_decorate_standings_entry(self):
        entry = {
            "team": "AFC Bournemouth",
            "P": 30,
            "W": 12,
            "D": 8,
            "L": 10,
            "GF": 44,
            "GA": 42,
            "GD": 2,
            "Pts": 44,
            "rank": 8,
            "position": 8,
        }

        decorated = decorate_standings_entry(dict(entry), "English Premier League")

        # Original standings fields must remain intact
        self.assertEqual(decorated["team"], "AFC Bournemouth")
        self.assertEqual(decorated["Pts"], 44)
        self.assertEqual(decorated["GD"], 2)
        self.assertEqual(decorated["W"], 12)
        self.assertEqual(decorated["position"], 8)

        # Standardized additions
        self.assertEqual(decorated["team_full"], "AFC Bournemouth")
        self.assertEqual(decorated["team_short"], "Bournemouth")
        self.assertEqual(decorated["team_abbr"], "BOU")
        self.assertEqual(decorated["team_id"], "afc-bournemouth")
        self.assertEqual(decorated["team_logo"], "bournemouth.png")

    def test_decorate_standings_table_groups(self):
        table_payload = {
            "competition": "English Premier League",
            "groups": [
                {
                    "name": "Overall",
                    "entries": [
                        {"team": "Arsenal", "Pts": 65, "position": 1},
                        {"team": "Manchester City", "Pts": 64, "position": 2},
                    ],
                }
            ],
        }

        decorated = decorate_standings_table(table_payload)
        entries = decorated["groups"][0]["entries"]
        self.assertEqual(entries[0]["team_abbr"], "ARS")
        self.assertEqual(entries[1]["team_abbr"], "MCI")

    def test_decorate_standings_table_dict_of_tables(self):
        tables_dict = {
            "English Premier League": [
                {"team": "Arsenal", "Pts": 65},
            ],
            "Spanish La Liga": [
                {"team": "Real Madrid", "Pts": 70},
            ],
        }

        decorate_standings_table(tables_dict)
        self.assertEqual(tables_dict["English Premier League"][0]["team_abbr"], "ARS")
        self.assertEqual(tables_dict["Spanish La Liga"][0]["team_abbr"], "RMA")


if __name__ == "__main__":
    unittest.main()
