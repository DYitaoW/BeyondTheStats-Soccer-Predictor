"""Tests for /api/league-leaders caching & ETags, and /api/live-scores/stream SSE optimization."""

import json
import os
import queue
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

from starlette.testclient import TestClient
from app import app
from live_poller import (
    _cached_compact_sse_frame,
    _cached_live_scores_bytes,
    _cached_sse_frame,
    _live_scores,
    _live_scores_lock,
    _update_cached_live_scores_payload,
    get_cached_compact_sse_frame,
    get_cached_sse_frame,
    register_sse_subscriber,
    unregister_sse_subscriber,
)
from routes.leagues import (
    _LEAGUE_LEADERS_CACHE_FILE,
    _LEAGUE_LEADERS_MEMO_CACHE,
    _LEAGUE_LEADERS_MEMO_LOCK,
    _build_league_leaders_data,
    _load_persisted_league_leaders,
    _refresh_league_leaders_cache,
    clear_league_leaders_cache,
)


class TestLeagueLeadersCaching(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        with _LEAGUE_LEADERS_MEMO_LOCK:
            _LEAGUE_LEADERS_MEMO_CACHE["computing"] = False

    def test_build_league_leaders_data(self):
        data = _build_league_leaders_data()
        self.assertTrue(data.get("ok"))
        self.assertIn("leagues", data)
        self.assertIn("cups", data)
        self.assertIsInstance(data["leagues"], list)
        self.assertIsInstance(data["cups"], list)

    def test_refresh_league_leaders_cache_and_disk_persistence(self):
        data = _refresh_league_leaders_cache(force=True)
        self.assertTrue(data.get("ok"))

        # Verify in-memory memo cache
        with _LEAGUE_LEADERS_MEMO_LOCK:
            self.assertIsNotNone(_LEAGUE_LEADERS_MEMO_CACHE.get("bytes"))
            self.assertIsNotNone(_LEAGUE_LEADERS_MEMO_CACHE.get("etag"))
            etag = _LEAGUE_LEADERS_MEMO_CACHE["etag"]
            self.assertTrue(etag.startswith('"') and etag.endswith('"'))

        # Verify disk persistence file
        self.assertTrue(os.path.exists(_LEAGUE_LEADERS_CACHE_FILE))
        with open(_LEAGUE_LEADERS_CACHE_FILE, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
        self.assertTrue(disk_data.get("ok"))
        self.assertEqual(len(disk_data["leagues"]), len(data["leagues"]))

        # Verify reload on cold boot
        with _LEAGUE_LEADERS_MEMO_LOCK:
            _LEAGUE_LEADERS_MEMO_CACHE["bytes"] = None
            _LEAGUE_LEADERS_MEMO_CACHE["etag"] = None

        _load_persisted_league_leaders()
        with _LEAGUE_LEADERS_MEMO_LOCK:
            self.assertIsNotNone(_LEAGUE_LEADERS_MEMO_CACHE.get("bytes"))
            self.assertEqual(_LEAGUE_LEADERS_MEMO_CACHE.get("etag"), etag)

    def test_api_league_leaders_endpoint_etag_and_304(self):
        # 1. First request returns 200 with ETag
        resp = self.client.get("/api/league-leaders")
        self.assertEqual(resp.status_code, 200)
        etag = resp.headers.get("etag")
        self.assertIsNotNone(etag)
        body = resp.json()
        self.assertTrue(body.get("ok"))

        # 2. Subsequent request with matching If-None-Match returns 304 Not Modified
        resp_304 = self.client.get("/api/league-leaders", headers={"If-None-Match": etag})
        self.assertEqual(resp_304.status_code, 304)

        # 3. Request with mismatched If-None-Match returns 200
        resp_mismatch = self.client.get("/api/league-leaders", headers={"If-None-Match": '"different-hash"'})
        self.assertEqual(resp_mismatch.status_code, 200)

    def test_clear_league_leaders_cache(self):
        clear_league_leaders_cache()
        with _LEAGUE_LEADERS_MEMO_LOCK:
            self.assertEqual(_LEAGUE_LEADERS_MEMO_CACHE["expires_at"], 0.0)


class TestLiveScoresStream(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        with _LEAGUE_LEADERS_MEMO_LOCK:
            _LEAGUE_LEADERS_MEMO_CACHE["computing"] = False

    def test_sse_subscriber_registration(self):
        q = register_sse_subscriber(compact=True)
        self.assertIsInstance(q, queue.Queue)
        unregister_sse_subscriber(q)

    def test_cached_sse_frames(self):
        _update_cached_live_scores_payload()
        frame = get_cached_sse_frame()
        compact_frame = get_cached_compact_sse_frame()

        self.assertTrue(frame.startswith("data: {"))
        self.assertTrue(frame.endswith("\n\n"))
        self.assertTrue(compact_frame.startswith("data: {"))
        self.assertTrue(compact_frame.endswith("\n\n"))

    def test_sse_stream_initial_handshake_and_snapshot(self):
        # Set up dummy game to ensure payload
        with _live_scores_lock:
            _live_scores["Test League"] = {
                "competition": "Test League",
                "games": [{"match_id": "test-1", "home_team": "Team A", "away_team": "Team B"}],
            }
        _update_cached_live_scores_payload()

        # Connect to stream and read the first two chunks
        with self.client.stream("GET", "/api/live-scores/stream?compact=1") as stream:
            self.assertEqual(stream.status_code, 200)
            self.assertTrue(stream.headers.get("content-type", "").startswith("text/event-stream"))

            lines = []
            for line in stream.iter_lines():
                if line:
                    lines.append(line)
                if len(lines) >= 2:
                    break

            # The very first yielded item is ': stream-connected'
            self.assertEqual(lines[0], ": stream-connected")
            # The second item is the snapshot 'data: {...}'
            self.assertTrue(lines[1].startswith("data: {"))


class TestApiMatchDetailUnified(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        with _live_scores_lock:
            _live_scores.clear()

    def tearDown(self):
        with _live_scores_lock:
            _live_scores.clear()

    def test_api_match_detail_merges_live_and_full_precomputed_predictions(self):
        mid = "unified-test-match-123"
        live_entry = {
            "match_id": mid,
            "competition": "English Premier League",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "home_score": 2,
            "away_score": 1,
            "status": "in",
            "clock": "68'",
            "period": "2",
        }
        with _live_scores_lock:
            _live_scores["English Premier League"] = {
                "competition": "English Premier League",
                "games": [live_entry],
            }

        pred_payload = {
            "match_id": mid,
            "competition": "English Premier League",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "prob_home": 58.5,
            "prob_draw": 24.2,
            "prob_away": 17.3,
            "pred_home_goals": 2,
            "pred_away_goals": 1,
            "pred_home_shots": 15.2,
            "pred_away_shots": 8.7,
            "pred_home_sot": 5.4,
            "pred_away_sot": 3.1,
            "prob_both_score": 0.61,
            "prob_over_2_5": 0.55,
            "prob_home_goals_0": 0.12,
            "probability_reasoning": "Arsenal expected to dominate possession and shot volume.",
            "correct_score_dist": {"2-1": 0.14},
            "double_chance": {"1X": 0.82},
            "asian_handicap": {"home_-0.5": 0.58},
            "odds_home": 1.80,
            "odds_draw": 3.60,
            "odds_away": 4.50,
            "winner_label": "Pred: Arsenal",
        }

        with patch("predictions._build_past_game_prediction_lookup", return_value={f"mid:{mid}": pred_payload}):
            resp = self.client.get(f"/api/match/{mid}")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("source"), "live")
            game = data.get("game", {})

            # Live actual values preserved
            self.assertEqual(game.get("status"), "in")
            self.assertEqual(game.get("home_score"), 2)
            self.assertEqual(game.get("away_score"), 1)
            self.assertEqual(game.get("clock"), "68'")

            # Full precomputed prediction fields merged directly at root
            self.assertEqual(game.get("pred_home_shots"), 15.2)
            self.assertEqual(game.get("pred_away_shots"), 8.7)
            self.assertEqual(game.get("pred_home_sot"), 5.4)
            self.assertEqual(game.get("pred_away_sot"), 3.1)
            self.assertEqual(game.get("prob_both_score"), 0.61)
            self.assertEqual(game.get("prob_over_2_5"), 0.55)
            self.assertEqual(game.get("probability_reasoning"), "Arsenal expected to dominate possession and shot volume.")
            self.assertEqual(game.get("odds_home"), 1.80)
            self.assertEqual(game.get("winner_label"), "Pred: Arsenal")

            # Also present inside upcoming_prediction dict
            up = game.get("upcoming_prediction", {})
            self.assertIsInstance(up, dict)
            self.assertEqual(up.get("pred_home_shots"), 15.2)
            self.assertEqual(up.get("prob_over_2_5"), 0.55)

    def test_api_match_detail_upcoming_fixture(self):
        mid = "upcoming-future-789"
        pred_payload = {
            "match_id": mid,
            "competition": "Spanish La Liga",
            "home_team": "Real Madrid",
            "away_team": "Barcelona",
            "match_date_iso": "2026-11-01",
            "prob_home": 45.0,
            "prob_draw": 26.0,
            "prob_away": 29.0,
            "pred_home_goals": 2,
            "pred_away_goals": 1,
            "pred_home_shots": 16.0,
            "pred_away_shots": 13.0,
            "prob_both_score": 0.68,
            "prob_over_2_5": 0.64,
            "probability_reasoning": "High-scoring Clasico anticipated based on attacking form.",
        }

        with patch("predictions._build_past_game_prediction_lookup", return_value={f"mid:{mid}": pred_payload}):
            resp = self.client.get(f"/api/match/{mid}")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data.get("ok"))
            game = data.get("game", {})
            self.assertEqual(game.get("home_team"), "Real Madrid")
            self.assertEqual(game.get("away_team"), "Barcelona")
            self.assertEqual(game.get("pred_home_shots"), 16.0)
            self.assertEqual(game.get("prob_both_score"), 0.68)
            self.assertIn("upcoming", data.get("source"))

    def test_api_match_detail_etag_and_304(self):
        from routes.live_scores import clear_match_detail_cache
        clear_match_detail_cache()

        mid = "live-caching-test-101"
        live_entry = {
            "match_id": mid,
            "competition": "English Premier League",
            "home_team": "Liverpool",
            "away_team": "Manchester City",
            "home_score": 1,
            "away_score": 1,
            "status": "in",
            "clock": "45+2'",
        }
        with _live_scores_lock:
            _live_scores["English Premier League"] = {
                "competition": "English Premier League",
                "games": [live_entry],
            }

        with patch("predictions._build_past_game_prediction_lookup", return_value={}):
            # First fetch (cache MISS)
            resp = self.client.get(f"/api/match/{mid}")
            self.assertEqual(resp.status_code, 200)
            etag = resp.headers.get("etag")
            self.assertIsNotNone(etag)
            self.assertIn("s-maxage=5", resp.headers.get("cache-control", ""))

            # Subsequent fetch with matching If-None-Match (HTTP 304 Not Modified)
            resp_304 = self.client.get(f"/api/match/{mid}", headers={"If-None-Match": etag})
            self.assertEqual(resp_304.status_code, 304)

            # Cached fetch without If-None-Match (Cache HIT)
            resp_hit = self.client.get(f"/api/match/{mid}")
            self.assertEqual(resp_hit.status_code, 200)
            self.assertEqual(resp_hit.headers.get("x-cache"), "HIT")
            self.assertEqual(resp_hit.headers.get("etag"), etag)


if __name__ == "__main__":
    unittest.main()
