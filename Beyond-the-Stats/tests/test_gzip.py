"""Tests for FastAPI GZipMiddleware compression behavior."""

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


class TestGZipCompression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Register a test route to isolate size thresholds
        @app.get("/api/test-gzip-large")
        async def _test_gzip_large():
            return {"data": "X" * 2500, "status": "ok"}

        @app.get("/api/test-gzip-small")
        async def _test_gzip_small():
            return {"status": "ok"}

        cls.client = TestClient(app, raise_server_exceptions=False)

    def test_gzip_compression_on_large_payload(self):
        """Responses >= minimum_size (1000 bytes) with Accept-Encoding: gzip must be compressed."""
        response = self.client.get(
            "/api/test-gzip-large",
            headers={"Accept-Encoding": "gzip"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("content-encoding"), "gzip")
        # In Starlette GZipMiddleware, Vary header includes Accept-Encoding
        self.assertIn("Accept-Encoding", response.headers.get("vary", ""))

    def test_gzip_bypassed_without_accept_encoding(self):
        """Responses without Accept-Encoding: gzip must NOT be compressed."""
        response = self.client.get(
            "/api/test-gzip-large",
            headers={"Accept-Encoding": "identity"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get("content-encoding"))

    def test_gzip_bypassed_on_small_payload(self):
        """Responses smaller than minimum_size (1000 bytes) must NOT be compressed."""
        response = self.client.get(
            "/api/test-gzip-small",
            headers={"Accept-Encoding": "gzip"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get("content-encoding"))


if __name__ == "__main__":
    unittest.main()

