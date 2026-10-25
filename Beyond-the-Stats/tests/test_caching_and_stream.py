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


if __name__ == "__main__":
    unittest.main()
