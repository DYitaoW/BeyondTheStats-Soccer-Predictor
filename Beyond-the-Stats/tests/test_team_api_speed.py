"""Tests for individual team API performance, caching, and payload schema integrity."""

import sys
import time
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_WEBSITE = _ROOT / "Website"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_WEBSITE) not in sys.path:
    sys.path.insert(0, str(_WEBSITE))

from fastapi.testclient import TestClient
from Website.app import app
from Website.routes.predictions import (
    _get_team_next_game_payload,
    _load_team_upcoming_games,
    _invalidate_team_next_game_index,
    _resolve_team_api_payload,
    _TEAM_PAYLOAD_CACHE,
    _TEAM_UPCOMING_GAMES_INDEX,
)
from Website.predictions import (
    _load_upcoming_rows,
    clear_upcoming_rows_cache,
    config,
)


class TestTeamAPISpeed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        _invalidate_team_next_game_index()

    def tearDown(self):
        _invalidate_team_next_game_index()

    def test_team_upcoming_games_latency_and_schema(self):
        """_load_team_upcoming_games retrieves full upcoming fixture list with low latency."""
        # First call builds index if cold
        games = _load_team_upcoming_games("Arsenal")
        self.assertIsInstance(games, list)
        self.assertGreater(len(games), 0)

        # Check required schema keys on fixtures
        first = games[0]
        for key in (
            "sync_key",
            "match_date",
            "match_date_iso",
            "competition",
            "home_team",
            "away_team",
            "canonical_home_team",
            "canonical_away_team",
            "is_home",
            "opponent",
            "opponent_display",
            "predicted_result",
            "prob_home",
            "prob_draw",
            "prob_away",
        ):
            self.assertIn(key, first, f"Missing key {key} in upcoming fixture")

        # Second call must be instant (< 15ms)
        t0 = time.perf_counter()
        cached_games = _load_team_upcoming_games("Arsenal")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self.assertEqual(len(cached_games), len(games))
        self.assertLess(elapsed_ms, 15.0, f"Expected < 15ms, took {elapsed_ms:.2f}ms")

    def test_team_next_game_latency_and_schema(self):
        """_get_team_next_game_payload returns valid payload with sub-5ms lookup."""
        res = _get_team_next_game_payload("Arsenal")
        self.assertTrue(res.get("ok"))
        self.assertTrue(res.get("has_game"))
        self.assertIsNotNone(res.get("game"))
        self.assertEqual(res.get("team"), "Arsenal")

        # Fast repeat lookup
        t0 = time.perf_counter()
        res2 = _get_team_next_game_payload("Arsenal")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self.assertTrue(res2.get("ok"))
        self.assertLess(elapsed_ms, 15.0, f"Expected < 15ms, took {elapsed_ms:.2f}ms")

    def test_team_api_alias_sharing_and_parity(self):
        """Query-style and path-style /api/team endpoints share cached payloads for team aliases."""
        resp1 = self.client.get("/api/team?team=Arsenal")
        self.assertEqual(resp1.status_code, 200)
        data1 = resp1.json()
        self.assertTrue(data1.get("ok"))
        self.assertEqual(data1.get("team"), "Arsenal")

        # Path style alias lookup
        t0 = time.perf_counter()
        resp2 = self.client.get("/api/team/Arsenal")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()

        self.assertEqual(data1["team"], data2["team"])
        self.assertEqual(len(data1["upcoming_games"]), len(data2["upcoming_games"]))
        self.assertEqual(len(data1["recent_matches"]), len(data2["recent_matches"]))
        self.assertLess(elapsed_ms, 50.0, f"Alias lookup took too long: {elapsed_ms:.2f}ms")

    def test_upcoming_rows_cache_mtime_invalidation(self):
        """_load_upcoming_rows caches parsed rows and invalidates on clear_upcoming_rows_cache."""
        csv_path = config.GLOBAL_UPCOMING_FILE
        rows1, _, _ = _load_upcoming_rows(csv_path, "global", date_range="upcoming", fixtures_only=True)
        self.assertGreater(len(rows1), 0)

        # Immediate repeat should be instant
        t0 = time.perf_counter()
        rows2, _, _ = _load_upcoming_rows(csv_path, "global", date_range="upcoming", fixtures_only=True)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self.assertEqual(len(rows1), len(rows2))
        self.assertLess(elapsed_ms, 15.0)

        # Invalidate
        clear_upcoming_rows_cache()

    def test_cache_invalidation_clears_team_payload_cache(self):
        """_invalidate_team_next_game_index clears _TEAM_PAYLOAD_CACHE."""
        _resolve_team_api_payload("Arsenal", "global")
        self.assertGreater(len(_TEAM_PAYLOAD_CACHE), 0)

        _invalidate_team_next_game_index()
        self.assertEqual(len(_TEAM_PAYLOAD_CACHE), 0)


if __name__ == "__main__":
    unittest.main()

