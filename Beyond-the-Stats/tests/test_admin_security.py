"""Admin auth: constant-time token check, and admin responses never cacheable."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "Website"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from starlette.testclient import TestClient  # noqa: E402

import config  # noqa: E402
from app import app  # noqa: E402
from routes.helpers import admin_token_matches  # noqa: E402

TOKEN = "unit-test-admin-token"


class AdminSecurityTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.patch = mock.patch.object(config, "MUTATION_API_TOKEN", TOKEN)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_token_matching(self):
        self.assertTrue(admin_token_matches(TOKEN))
        self.assertFalse(admin_token_matches(TOKEN + "x"))
        self.assertFalse(admin_token_matches(""))
        with mock.patch.object(config, "MUTATION_API_TOKEN", ""):
            self.assertFalse(admin_token_matches(""))  # unset token never authorizes

    def test_admin_routes_require_token_both_header_styles(self):
        url = "/api/admin/points-adjustments"
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.get(url, headers={"X-Admin-Token": "wrong"}).status_code, 401)
        self.assertEqual(self.client.get(url, headers={"X-Admin-Token": TOKEN}).status_code, 200)
        self.assertEqual(self.client.get(url, headers={"Authorization": f"Bearer {TOKEN}"}).status_code, 200)
        self.assertEqual(self.client.get("/api/test/env").status_code, 401)
        self.assertEqual(self.client.get("/api/test/env", headers={"X-Admin-Token": TOKEN}).status_code, 200)

    def test_admin_and_debug_responses_are_no_store(self):
        for url in ("/api/admin/points-adjustments", "/api/debug/poller-state"):
            for headers in ({}, {"X-Admin-Token": TOKEN}):
                resp = self.client.get(url, headers=headers)
                self.assertEqual(resp.headers.get("cache-control"), "no-store", (url, headers, resp.status_code))


if __name__ == "__main__":
    unittest.main()
