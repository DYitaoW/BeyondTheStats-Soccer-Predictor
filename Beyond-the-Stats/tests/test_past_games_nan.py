"""Unit tests for JSON NaN sanitization and /api/past-games endpoint robustness."""

import math
import os
import sys
import unittest
from pathlib import Path

# Add Website and root to path
_ROOT = Path(__file__).resolve().parent.parent
_WEBSITE = _ROOT / "Website"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_WEBSITE) not in sys.path:
    sys.path.insert(0, str(_WEBSITE))

from fastapi.testclient import TestClient
from Website.app import app
from Website.routes.compat import _sanitize_nan_inf, SafeJSONResponse, jsonify


class TestJsonNanSanitization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app, raise_server_exceptions=False)

    def test_sanitize_nan_inf_primitives(self):
        """Verify individual float NaN and Inf values become None."""
        self.assertIsNone(_sanitize_nan_inf(float("nan")))
        self.assertIsNone(_sanitize_nan_inf(float("inf")))
        self.assertIsNone(_sanitize_nan_inf(float("-inf")))
        self.assertEqual(_sanitize_nan_inf(42.5), 42.5)
        self.assertEqual(_sanitize_nan_inf("hello"), "hello")
        self.assertEqual(_sanitize_nan_inf(True), True)
        self.assertEqual(_sanitize_nan_inf(100), 100)

    def test_sanitize_nan_inf_nested_structures(self):
        """Verify deeply nested dicts and lists with NaNs become JSON-safe nulls."""
        payload = {
            "match_id": "test-1",
            "prob_home": float("nan"),
            "prob_away": 0.45,
            "odds": {"home": float("inf"), "away": 2.1},
            "stats": [float("-inf"), 1.5, float("nan")],
        }
        sanitized = _sanitize_nan_inf(payload)
        self.assertIsNone(sanitized["prob_home"])
        self.assertEqual(sanitized["prob_away"], 0.45)
        self.assertIsNone(sanitized["odds"]["home"])
        self.assertEqual(sanitized["odds"]["away"], 2.1)
        self.assertIsNone(sanitized["stats"][0])
        self.assertEqual(sanitized["stats"][1], 1.5)
        self.assertIsNone(sanitized["stats"][2])

    def test_safe_json_response_does_not_crash_on_nan(self):
        """SafeJSONResponse must not raise ValueError: Out of range float values are not JSON compliant."""
        bad_payload = {
            "name": "Bad Match",
            "rating": float("nan"),
            "inf_value": float("inf"),
        }
        # Standard Starlette JSONResponse with allow_nan=False would crash here
        resp = SafeJSONResponse(content=bad_payload)
        raw_bytes = resp.render(bad_payload)
        self.assertIn(b'"rating":null', raw_bytes)
        self.assertIn(b'"inf_value":null', raw_bytes)

    def test_past_games_query_with_limit_and_typo_dates(self):
        """GET /api/past-games with query like ?from2-26-10-05&to=2026-10-18&limit=1000 succeeds with 200."""
        # Exact query reported by user
        res = cls = self.client.get("/api/past-games?from2-26-10-05&to=2026-10-18&limit=1000")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("rows", data)
        self.assertIn("total", data)

    def test_past_games_query_standard(self):
        """GET /api/past-games?from=2026-10-05&to=2026-10-18&limit=1000 succeeds with 200."""
        res = self.client.get("/api/past-games?from=2026-10-05&to=2026-10-18&limit=1000")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
    def test_past_games_single_date_fast(self):
        """GET /api/past-games?date=2026-10-15 responds with 200."""
        res = self.client.get("/api/past-games?date=2026-10-15")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))

    def test_upcoming_single_date_fast(self):
        """GET /api/upcoming/global?date=2026-10-15 responds with 200."""
        res = self.client.get("/api/upcoming/global?date=2026-10-15")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))


if __name__ == "__main__":
    unittest.main()

