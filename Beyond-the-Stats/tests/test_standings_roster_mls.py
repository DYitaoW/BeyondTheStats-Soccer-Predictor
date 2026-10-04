"""Tests for current-season roster preference and MLS regular-season filtering."""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WEBSITE_DIR = ROOT / "Website"
if str(WEBSITE_DIR) not in sys.path:
    sys.path.insert(0, str(WEBSITE_DIR))


class CurrentSeasonRosterPreferenceTests(unittest.TestCase):
    def test_canonical_roster_prefers_current_season_over_predicted(self):
        import standings

        current = {
            "England/Premier League": [
                "Arsenal",
                "Coventry",
                "Hull",
                "Ipswich",
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "current_season_teams.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(current, fh)
            with mock.patch.object(standings.config, "CURRENT_SEASON_TEAMS_FILE", path):
                with mock.patch.object(
                    standings,
                    "_teams_from_predicted_table",
                    return_value=["Arsenal", "Burnley", "West Ham", "Wolves"],
                ):
                    roster = standings._canonical_roster_teams("England/Premier League")
        self.assertIn("Coventry", roster)
        self.assertIn("Hull", roster)
        self.assertIn("Ipswich", roster)
        self.assertNotIn("Burnley", roster)
        self.assertNotIn("West Ham", roster)
        self.assertNotIn("Wolves", roster)

    def test_fallback_standings_does_not_union_csv_when_current_roster_exists(self):
        import standings

        current = {
            "England/Premier League": [
                "Arsenal",
                "Coventry",
                "Hull",
                "Ipswich",
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "current_season_teams.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(current, fh)

            real_exists = os.path.exists

            def _exists(p):
                if p == path:
                    return True
                # Pretend prediction/projected CSVs are absent so union cannot run.
                return False

            with mock.patch.object(standings.config, "CURRENT_SEASON_TEAMS_FILE", path):
                with mock.patch.object(standings, "_load_league_teams", return_value={}):
                    with mock.patch.object(standings.os.path, "exists", side_effect=_exists):
                        payload = standings._build_fallback_standings("England/Premier League")
        self.assertIsNotNone(payload)
        teams = []
        for group in payload.get("groups") or []:
            teams.extend(e.get("team") for e in (group.get("entries") or []))
        self.assertIn("Coventry", teams)
        self.assertNotIn("Burnley", teams)


class MlsRegularSeasonFilterTests(unittest.TestCase):
    def test_excludes_open_cup_leagues_cup_and_playoffs(self):
        import competition_rules as cr

        keep = {
            "competition": "United States/MLS",
            "home_team": "Inter Miami",
            "away_team": "Atlanta Utd",
            "round": "Regular Season",
            "home_score": 2,
            "away_score": 1,
        }
        open_cup = {
            **keep,
            "competition": "United States/US Open Cup",
            "away_team": "Louisville City",
        }
        leagues_cup = {
            **keep,
            "round": "Leagues Cup - Round of 32",
            "away_team": "Club America",
        }
        playoff = {
            **keep,
            "round": "MLS Cup Playoffs - Round One",
        }
        friendly = {
            **keep,
            "round": "Club Friendly",
        }
        self.assertTrue(cr.is_mls_regular_season_game(keep))
        self.assertFalse(cr.is_mls_regular_season_game(open_cup))
        self.assertFalse(cr.is_mls_regular_season_game(leagues_cup))
        self.assertFalse(cr.is_mls_regular_season_game(playoff))
        self.assertFalse(cr.is_mls_regular_season_game(friendly))
        filtered = cr.filter_mls_regular_season_games(
            [keep, open_cup, leagues_cup, playoff, friendly]
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["home_team"], "Inter Miami")


class MlsCupWinCountBugTests(unittest.TestCase):
    def test_cup_win_counts_uses_truthy_winner_not_membership(self):
        source = (WEBSITE_DIR.parent / "pipelines" / "mls" / "files" / "Project_League_Table.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("cup_win_counts[cup_winner]", source)
        self.assertNotIn("if cup_winner in cup_win_counts:", source)
        # Cups PR: reject NONE/TBD/Draw placeholders before counting a champion.
        self.assertIn("NONE", source)
        self.assertIn("accumulate_mls_playoff_outcome_counts", source)
        self.assertIn("make_playoffs_probabilities", source)
        self.assertIn("round_reach_probabilities", source)

    def test_accumulate_playoff_outcomes_counts_champion_and_rounds(self):
        # Load only the pure helper without sklearn/joblib module deps.
        path = WEBSITE_DIR.parent / "pipelines" / "mls" / "files" / "Project_League_Table.py"
        source = path.read_text(encoding="utf-8")
        start = source.index("def _series_participants(")
        end = source.index("\ndef counts_to_probability_map(")
        ns: dict = {}
        exec(source[start:end], ns)

        bracket = {
            "eastern_seeds": [{"seed": i, "team": f"E{i}"} for i in range(1, 10)],
            "western_seeds": [{"seed": i, "team": f"W{i}"} for i in range(1, 10)],
            "wildcard": {
                "east": {"home_team": "E8", "away_team": "E9", "winner": "E8"},
                "west": {"home_team": "W8", "away_team": "W9", "winner": "W8"},
            },
            "round_one": {
                "east": {
                    "A": {"high_seed_team": "E1", "low_seed_team": "E8", "winner": "E1"},
                    "B": {"high_seed_team": "E2", "low_seed_team": "E7", "winner": "E2"},
                    "C": {"high_seed_team": "E3", "low_seed_team": "E6", "winner": "E3"},
                    "D": {"high_seed_team": "E4", "low_seed_team": "E5", "winner": "E4"},
                },
                "west": {
                    "A": {"high_seed_team": "W1", "low_seed_team": "W8", "winner": "W1"},
                    "B": {"high_seed_team": "W2", "low_seed_team": "W7", "winner": "W2"},
                    "C": {"high_seed_team": "W3", "low_seed_team": "W6", "winner": "W3"},
                    "D": {"high_seed_team": "W4", "low_seed_team": "W5", "winner": "W4"},
                },
            },
            "conference_semifinals": {
                "east": [
                    {"home_team": "E1", "away_team": "E4", "winner": "E1"},
                    {"home_team": "E2", "away_team": "E3", "winner": "E2"},
                ],
                "west": [
                    {"home_team": "W1", "away_team": "W4", "winner": "W1"},
                    {"home_team": "W2", "away_team": "W3", "winner": "W2"},
                ],
            },
            "conference_finals": {
                "east": {"home_team": "E1", "away_team": "E2", "winner": "E1"},
                "west": {"home_team": "W1", "away_team": "W2", "winner": "W1"},
            },
            "mls_cup": {"home_team": "E1", "away_team": "W1", "winner": "E1"},
        }
        from collections import defaultdict

        make_playoffs = defaultdict(int)
        round_reach = defaultdict(lambda: defaultdict(int))
        elimination = defaultdict(lambda: defaultdict(int))
        canonical = {f"E{i}" for i in range(1, 11)} | {f"W{i}" for i in range(1, 11)}
        ns["accumulate_mls_playoff_outcome_counts"](
            bracket, make_playoffs, round_reach, elimination, canonical
        )
        self.assertEqual(make_playoffs["E1"], 1)
        self.assertEqual(make_playoffs["E10"], 0)
        self.assertEqual(elimination["E10"]["missed_playoffs"], 1)
        self.assertEqual(round_reach["made_playoffs"]["E1"], 1)
        self.assertEqual(round_reach["mls_cup"]["E1"], 1)
        self.assertEqual(elimination["E1"]["champion"], 1)
        self.assertEqual(elimination["W1"]["mls_cup"], 1)
        self.assertEqual(elimination["E9"]["wildcard"], 1)


if __name__ == "__main__":
    unittest.main()
