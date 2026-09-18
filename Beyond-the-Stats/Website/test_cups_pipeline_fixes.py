"""Regression tests for cup ESPN IDs, layouts, and Leagues Cup stage classify."""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


WEBSITE_DIR = Path(__file__).resolve().parent
ROOT_DIR = WEBSITE_DIR.parent
FILES_DIR = ROOT_DIR / "files"
for path in (WEBSITE_DIR, FILES_DIR, ROOT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


class CupLayoutAndStageTests(unittest.TestCase):
    def test_knockout_cups_use_bracket_layout_not_table(self):
        import competition_rules as cr

        for comp in (
            "England/FA Cup",
            "England/League Cup",
            "Spain/Copa del Rey",
            "Germany/DFB-Pokal",
            "France/Coupe de France",
            "Italy/Coppa Italia",
            "United States/US Open Cup",
        ):
            self.assertEqual(
                cr.standings_layout_for(comp),
                cr.STANDINGS_LAYOUT_KNOCKOUT,
                comp,
            )

    def test_uefa_and_leagues_cup_keep_table_layouts(self):
        import competition_rules as cr

        self.assertEqual(
            cr.standings_layout_for("Europe/Champions League"),
            cr.STANDINGS_LAYOUT_LEAGUE_PHASE,
        )
        self.assertEqual(
            cr.standings_layout_for("North America/Leagues Cup"),
            cr.STANDINGS_LAYOUT_LEAGUES_CUP,
        )

    def test_leagues_cup_phase_one_round_labels_are_group_not_knockout(self):
        import competition_rules as cr

        for label in ("Phase One", "Phase 1", "Round 1", "Round 2", "Round 3"):
            stage = cr.classify_match_stage(
                {"round": label},
                "North America/Leagues Cup",
            )
            self.assertEqual(stage, "group", label)

        for label in ("Quarter-finals", "Semi-finals", "Final", "Third Place"):
            stage = cr.classify_match_stage(
                {"round": label},
                "North America/Leagues Cup",
            )
            self.assertEqual(stage, "knockout", label)

    def test_knockout_regex_no_longer_matches_bare_round(self):
        import competition_rules as cr

        self.assertIsNone(cr.KNOCKOUT_ROUND_RE.search("Round 1"))
        self.assertIsNotNone(cr.KNOCKOUT_ROUND_RE.search("Round of 16"))


class CupEspnAndKeyMapTests(unittest.TestCase):
    def test_track_cup_espn_ids_cover_all_domestics(self):
        track = importlib.import_module("Track_Cup_Results")
        keys = track.CUP_ESPN_COMPETITION_KEYS
        self.assertEqual(keys["England/League Cup"], "eng.league_cup")
        self.assertEqual(keys["Italy/Coppa Italia"], "ita.coppa_italia")
        self.assertEqual(keys["United States/US Open Cup"], "usa.open")
        self.assertEqual(keys["Spain/Copa del Rey"], "esp.copa_del_rey")
        self.assertEqual(keys["Germany/DFB-Pokal"], "ger.dfb_pokal")
        self.assertEqual(keys["France/Coupe de France"], "fra.coupe_de_france")

    def test_predict_cups_includes_us_open(self):
        src = (FILES_DIR / "Predict_Upcoming_Cups.py").read_text(encoding="utf-8")
        self.assertIn('"name": "United States/US Open Cup"', src)
        self.assertIn('"espn_id": "usa.open"', src)
        self.assertIn('"United States/US Open Cup"', src.split("AUTO_RESOLVE_CUP_COMPETITIONS")[1].split("}")[0])

    def test_tournament_key_map_has_copa_del_rey_and_league_cup(self):
        import config

        self.assertEqual(config.TOURNAMENT_KEY_MAP["copa-del-rey"], "Spain/Copa del Rey")
        self.assertEqual(config.TOURNAMENT_KEY_MAP["league-cup"], "England/League Cup")
        self.assertEqual(config.TOURNAMENT_KEY_MAP["efl-cup"], "England/League Cup")

    def test_config_live_espn_ids_aligned(self):
        import config

        self.assertEqual(config.LIVE_SCORE_COMPETITIONS["Italy/Coppa Italia"], "ita.coppa_italia")
        self.assertEqual(config.LIVE_SCORE_COMPETITIONS["United States/US Open Cup"], "usa.open")
        self.assertEqual(config.LIVE_SCORE_COMPETITIONS["England/League Cup"], "eng.league_cup")


class DomesticBracketRoundsTests(unittest.TestCase):
    def test_domestic_bracket_emits_rounds(self):
        import pandas as pd
        track = importlib.import_module("Track_Cup_Results")

        frame = pd.DataFrame([
            {
                "match_date": "2026-09-20",
                "home_team": "Arsenal",
                "away_team": "Chelsea",
                "predicted_result": "H",
                "prob_home": 0.5,
                "prob_draw": 0.25,
                "prob_away": 0.25,
                "pred_home_goals": 2,
                "pred_away_goals": 1,
                "__status": "Upcoming",
            },
            {
                "match_date": "2026-09-10",
                "home_team": "Liverpool",
                "away_team": "Everton",
                "predicted_result": "H",
                "actual_result": "H",
                "actual_home_goals": 1,
                "actual_away_goals": 0,
                "__status": "Completed",
            },
        ])
        bracket = track._build_domestic_cup_bracket_with_draws(
            "England/FA Cup", frame, {}
        )
        self.assertIn("rounds", bracket)
        self.assertGreaterEqual(len(bracket["rounds"]), 1)
        self.assertEqual(bracket["rounds"][0]["name"], "Upcoming Cup Fixtures")
        self.assertEqual(len(bracket["rounds"][0]["matches"]), 1)


class BundesligaRosterTests(unittest.TestCase):
    def test_league_teams_bundesliga_26_27_short_names(self):
        import json

        path = ROOT_DIR / "Data" / "Predictions" / "league_teams.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        bl = set(data["Germany/Bundesliga"])
        self.assertEqual(len(bl), 18)
        self.assertIn("Hamburg", bl)
        self.assertIn("Schalke 04", bl)
        self.assertIn("Elversberg", bl)
        self.assertIn("Paderborn", bl)
        self.assertNotIn("Wolfsburg", bl)
        self.assertNotIn("Hamburg SV", bl)
        self.assertNotIn("Bayer Leverkusen", bl)


if __name__ == "__main__":
    unittest.main()
