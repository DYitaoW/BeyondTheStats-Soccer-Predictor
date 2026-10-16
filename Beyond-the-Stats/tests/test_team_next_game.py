"""Tests for dedicated GET /api/team/<team_name>/next-game and /api/team/next-game endpoints."""

import os
import sys
import time
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

from live_poller import _live_scores, _live_scores_lock
from routes.predictions import (
    _get_team_next_game_payload,
    _invalidate_team_next_game_index,
    _TEAM_NEXT_GAME_INDEX,
)


class TestTeamNextGame(unittest.TestCase):
    def setUp(self):
        _invalidate_team_next_game_index()
        with _live_scores_lock:
            _live_scores.clear()

    def tearDown(self):
        _invalidate_team_next_game_index()
        with _live_scores_lock:
            _live_scores.clear()

    def test_missing_team_returns_error(self):
        res = _get_team_next_game_payload("")
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("error"), "Missing team")

        res_none = _get_team_next_game_payload(None)
        self.assertFalse(res_none.get("ok"))

    def test_live_match_takes_priority(self):
        # Simulate active live match for Arsenal vs Chelsea
        live_game = {
            "match_id": "live-12345",
            "competition": "English Premier League",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "home_score": 2,
            "away_score": 1,
            "status": "in",
            "period": "2nd Half",
            "clock": "68'",
            "kickoff_utc": "2026-10-18T15:30:00Z",
            "match_date": "2026-10-18",
            "goalscorers": [
                {"team": "home", "scorer": "Saka", "minute": "23'"},
                {"team": "away", "scorer": "Palmer", "minute": "45'"},
                {"team": "home", "scorer": "Havertz", "minute": "55'"},
            ],
            "red_cards": [],
            "yellow_cards": [],
        }

        with _live_scores_lock:
            _live_scores["English Premier League"] = {"games": [live_game]}

        # Query for Arsenal (Home)
        res = _get_team_next_game_payload("Arsenal")
        self.assertTrue(res.get("ok"))
        self.assertTrue(res.get("has_game"))
        self.assertTrue(res.get("is_live"))
        self.assertEqual(res.get("status"), "live")
        game = res.get("game")
        self.assertIsNotNone(game)
        self.assertEqual(game.get("match_id"), "live-12345")
        self.assertTrue(game.get("is_home"))
        self.assertEqual(game.get("opponent"), "Chelsea")
        self.assertEqual(game.get("live_status"), "in")
        self.assertEqual(game.get("live_clock"), "68'")
        self.assertEqual(game.get("score"), {"home": 2, "away": 1})
        self.assertEqual(len(game.get("goalscorers")), 3)

        # Query for Chelsea (Away)
        res_away = _get_team_next_game_payload("chelsea")
        self.assertTrue(res_away.get("ok"))
        self.assertTrue(res_away.get("has_game"))
        self.assertTrue(res_away.get("is_live"))
        game_away = res_away.get("game")
        self.assertFalse(game_away.get("is_home"))
        self.assertEqual(game_away.get("opponent"), "Arsenal")

    def test_upcoming_match_when_not_live(self):
        # Mock upcoming game rows
        mock_upcoming = [
            {
                "match_id": "up-999",
                "competition": "English Premier League",
                "home_team": "Liverpool",
                "away_team": "Manchester City",
                "match_date_iso": "2026-10-25",
                "match_datetime_utc": "2026-10-25T16:30:00Z",
                "predicted_result": "H",
                "prob_home": 0.45,
                "prob_draw": 0.28,
                "prob_away": 0.27,
                "status": "upcoming",
            }
        ]

        with patch("routes.predictions._ALL_UPCOMING_SOURCES", []), \
             patch("shared.sqlite_store.load_upcoming_games", return_value=mock_upcoming):
            _invalidate_team_next_game_index()

            res = _get_team_next_game_payload("Liverpool")
            self.assertTrue(res.get("ok"))
            self.assertTrue(res.get("has_game"))
            self.assertFalse(res.get("is_live"))
            self.assertEqual(res.get("status"), "upcoming")
            game = res.get("game")
            self.assertIsNotNone(game)
            self.assertEqual(game.get("match_id"), "up-999")
            self.assertTrue(game.get("is_home"))
            self.assertEqual(game.get("opponent"), "Manchester City")
            self.assertEqual(game.get("predicted_result"), "H")
            self.assertEqual(game.get("prob_home"), 0.45)

            # Query away team
            res_away = _get_team_next_game_payload("Manchester City")
            self.assertTrue(res_away.get("ok"))
            self.assertTrue(res_away.get("has_game"))
            self.assertFalse(res_away.get("game")["is_home"])
            self.assertEqual(res_away.get("game")["opponent"], "Liverpool")

    def test_no_game_scheduled(self):
        with patch("routes.predictions._ALL_UPCOMING_SOURCES", []), \
             patch("shared.sqlite_store.load_upcoming_games", return_value=[]):
            _invalidate_team_next_game_index()
            res = _get_team_next_game_payload("NonExistentTeamFC")
            self.assertTrue(res.get("ok"))
            self.assertFalse(res.get("has_game"))
            self.assertFalse(res.get("is_live"))
            self.assertIsNone(res.get("game"))

    def test_performance_under_50ms_for_100_queries(self):
        mock_upcoming = [
            {
                "match_id": f"fixture-{i}",
                "competition": "English Premier League",
                "home_team": f"TeamHome{i}",
                "away_team": f"TeamAway{i}",
                "match_date_iso": "2026-10-25",
                "match_datetime_utc": "2026-10-25T16:30:00Z",
                "status": "upcoming",
            }
            for i in range(20)
        ]

        with patch("routes.predictions._ALL_UPCOMING_SOURCES", []), \
             patch("shared.sqlite_store.load_upcoming_games", return_value=mock_upcoming):
            _invalidate_team_next_game_index()

            start_t = time.perf_counter()
            for _ in range(100):
                res = _get_team_next_game_payload("TeamHome3")
                self.assertTrue(res.get("has_game"))
            elapsed_ms = (time.perf_counter() - start_t) * 1000

            # 100 in-memory queries should finish well under 50ms (<0.5ms each)
            self.assertLess(elapsed_ms, 50.0, f"Expected <50ms for 100 queries, got {elapsed_ms:.2f}ms")


if __name__ == "__main__":
    unittest.main()
