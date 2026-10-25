"""Tests for Admin Debug Points Adjustments API & Standings Integration.

Verifies:
- Authorization enforcement (401 without or with invalid MUTATION_API_TOKEN).
- Valid token acceptance via X-Admin-Token and Authorization: Bearer.
- CRUD lifecycle of team point deductions, baseline points, and manual overrides.
- Automatic standings table ranking & metadata reflection (Pts, deduction, deduction_reason).
- Standings recalculation and cache invalidation.
- Competitions and teams catalog endpoint.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Setup paths
_ROOT = Path(__file__).resolve().parent.parent
_WEBSITE = _ROOT / "Website"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_WEBSITE) not in sys.path:
    sys.path.insert(0, str(_WEBSITE))

from starlette.testclient import TestClient
from app import app
import config
from shared import sqlite_store
import standings


class TestAdminPointsAdjustments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.test_token = "bts-secret-test-admin-token-2026"
        cls.orig_token = config.MUTATION_API_TOKEN
        config.MUTATION_API_TOKEN = cls.test_token

    @classmethod
    def tearDownClass(cls):
        config.MUTATION_API_TOKEN = cls.orig_token

    def setUp(self):
        # Clean any test adjustments from store
        standings.clear_points_adjustments_cache()
        standings._clear_all_real_data_caches()
        sqlite_store.delete_points_adjustment("England/Premier League", "Everton")
        sqlite_store.delete_points_adjustment("England/Premier League", "Nottingham Forest")
        sqlite_store.delete_points_adjustment("United States/MLS", "Inter Miami")
        sqlite_store.delete_points_adjustment("Spain/La Liga", "Test FC")

    def tearDown(self):
        standings.clear_points_adjustments_cache()
        standings._clear_all_real_data_caches()
        sqlite_store.delete_points_adjustment("England/Premier League", "Everton")
        sqlite_store.delete_points_adjustment("England/Premier League", "Nottingham Forest")
        sqlite_store.delete_points_adjustment("United States/MLS", "Inter Miami")
        sqlite_store.delete_points_adjustment("Spain/La Liga", "Test FC")

    # ── Authorization Tests ──────────────────────────────────────────

    def test_unauthorized_access_rejected(self):
        """Endpoints must return 401 Unauthorized without a valid admin token."""
        # GET without token
        resp = self.client.get("/api/admin/points-adjustments")
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(resp.json().get("ok"))

        # GET with invalid token
        resp = self.client.get(
            "/api/admin/points-adjustments",
            headers={"X-Admin-Token": "wrong-token"},
        )
        self.assertEqual(resp.status_code, 401)

        # POST without token
        resp = self.client.post("/api/admin/points-adjustments", json={"competition": "eng.1", "team": "Everton"})
        self.assertEqual(resp.status_code, 401)

        # DELETE without token
        resp = self.client.delete("/api/admin/points-adjustments")
        self.assertEqual(resp.status_code, 401)

        # Recalculate without token
        resp = self.client.post("/api/admin/points-adjustments/recalculate")
        self.assertEqual(resp.status_code, 401)

        # Preview without token
        resp = self.client.get("/api/admin/points-adjustments/preview?competition=England/Premier+League")
        self.assertEqual(resp.status_code, 401)

        # Competitions without token
        resp = self.client.get("/api/admin/points-adjustments/competitions")
        self.assertEqual(resp.status_code, 401)

    def test_bearer_token_authorization(self):
        """Endpoints must accept 'Authorization: Bearer <token>' header."""
        resp = self.client.get(
            "/api/admin/points-adjustments",
            headers={"Authorization": f"Bearer {self.test_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("ok"))

    # ── CRUD Operations Tests ────────────────────────────────────────

    def test_points_adjustment_crud_lifecycle(self):
        """Test creating, reading, updating, and deleting a point deduction."""
        headers = {"X-Admin-Token": self.test_token}

        # 1. Create deduction
        create_payload = {
            "competition": "England/Premier League",
            "team": "Everton",
            "deduction_points": 8,
            "deduction_reason": "PSR breach",
            "notes": "2023/24 Premier League penalty",
            "recalculate": False,
        }
        res_create = self.client.post(
            "/api/admin/points-adjustments",
            json=create_payload,
            headers=headers,
        )
        self.assertEqual(res_create.status_code, 200)
        body = res_create.json()
        self.assertTrue(body.get("ok"))
        self.assertEqual(body["adjustment"]["team"], "Everton")
        self.assertEqual(body["adjustment"]["deduction_points"], 8)
        self.assertEqual(body["adjustment"]["deduction_reason"], "PSR breach")

        # 2. Read back
        res_get = self.client.get(
            "/api/admin/points-adjustments?competition=England/Premier+League",
            headers=headers,
        )
        self.assertEqual(res_get.status_code, 200)
        items = res_get.json().get("adjustments", [])
        self.assertTrue(any(a["team"] == "Everton" and a["deduction_points"] == 8 for a in items))

        # 3. Update (reduce deduction to 6 and add baseline points 3)
        update_payload = {
            "competition": "England/Premier League",
            "team": "Everton",
            "deduction_points": 6,
            "baseline_points": 3,
            "deduction_reason": "PSR breach reduced on appeal",
            "notes": "Appeal verdict April 2024",
            "recalculate": False,
        }
        res_update = self.client.post(
            "/api/admin/points-adjustments",
            json=update_payload,
            headers=headers,
        )
        self.assertEqual(res_update.status_code, 200)
        self.assertEqual(res_update.json()["adjustment"]["deduction_points"], 6)
        self.assertEqual(res_update.json()["adjustment"]["baseline_points"], 3)

        # 4. Delete via query params
        res_del = self.client.delete(
            "/api/admin/points-adjustments?competition=England/Premier+League&team=Everton&recalculate=false",
            headers=headers,
        )
        self.assertEqual(res_del.status_code, 200)
        self.assertTrue(res_del.json().get("ok"))

        # Verify deletion
        res_after = self.client.get(
            "/api/admin/points-adjustments?competition=England/Premier+League",
            headers=headers,
        )
        items_after = res_after.json().get("adjustments", [])
        self.assertFalse(any(a["team"] == "Everton" for a in items_after))

    # ── Standings Table Integration Tests ────────────────────────────

    def test_standings_deduction_applied_and_reranked(self):
        """Setting a point deduction must subtract points, attach metadata, and re-rank the table."""
        headers = {"X-Admin-Token": self.test_token}

        # Save an Everton -10 pts deduction
        resp = self.client.post(
            "/api/admin/points-adjustments",
            json={
                "competition": "England/Premier League",
                "team": "Everton",
                "deduction_points": 10,
                "deduction_reason": "Financial irregularities",
                "notes": "Test deduction",
                "recalculate": True,
            },
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200)

        # Preview standings via admin API
        preview_resp = self.client.get(
            "/api/admin/points-adjustments/preview?competition=England/Premier+League",
            headers=headers,
        )
        self.assertEqual(preview_resp.status_code, 200)
        preview_data = preview_resp.json()
        self.assertTrue(preview_data.get("ok"))
        self.assertIn("Everton", preview_data.get("active_adjustments", {}))

        # Check the table entries
        groups = preview_data["standings"].get("groups", [])
        self.assertTrue(len(groups) > 0)
        entries = groups[0].get("entries", [])
        everton_entry = next((e for e in entries if e["team"] == "Everton"), None)
        self.assertIsNotNone(everton_entry, "Everton must exist in Premier League table")
        self.assertEqual(everton_entry.get("deduction"), 10)
        self.assertEqual(everton_entry.get("deduction_reason"), "Financial irregularities")

        # Verify ranking order: table must be monotonically non-increasing by Pts
        for i in range(len(entries) - 1):
            cur_pts = int(entries[i].get("Pts", 0))
            next_pts = int(entries[i + 1].get("Pts", 0))
            self.assertGreaterEqual(cur_pts, next_pts, f"Rank {i+1} ({cur_pts} pts) must be >= Rank {i+2} ({next_pts} pts)")

    def test_baseline_points_and_stats_override(self):
        """Setting baseline points and match stats must increment baseline values for leagues without full history."""
        headers = {"X-Admin-Token": self.test_token}

        # Set baseline for a team
        resp = self.client.post(
            "/api/admin/points-adjustments",
            json={
                "competition": "Spain/La Liga",
                "team": "Test FC",
                "baseline_points": 15,
                "baseline_p": 5,
                "baseline_w": 5,
                "baseline_gf": 12,
                "baseline_ga": 2,
                "notes": "Early season baseline from external data",
                "recalculate": True,
            },
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200)

        # Fetch preview
        preview_resp = self.client.get(
            "/api/admin/points-adjustments/preview?competition=Spain/La+Liga",
            headers=headers,
        )
        self.assertEqual(preview_resp.status_code, 200)
        entries = preview_resp.json()["standings"]["groups"][0]["entries"]
        test_entry = next((e for e in entries if e["team"] == "Test FC"), None)
        self.assertIsNotNone(test_entry)
        self.assertGreaterEqual(test_entry["Pts"], 15)
        self.assertGreaterEqual(test_entry["P"], 5)
        self.assertEqual(test_entry.get("baseline_points"), 15)

    def test_recalculate_endpoint(self):
        """POST /api/admin/points-adjustments/recalculate flushes caches and recalculates standings."""
        headers = {"X-Admin-Token": self.test_token}
        resp = self.client.post(
            "/api/admin/points-adjustments/recalculate",
            json={"competition": "England/Premier League"},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("England/Premier League", data.get("competitions", []))

    def test_competitions_catalog_endpoint(self):
        """GET /api/admin/points-adjustments/competitions returns known competitions and teams."""
        headers = {"X-Admin-Token": self.test_token}
        resp = self.client.get(
            "/api/admin/points-adjustments/competitions",
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertGreater(data.get("count", 0), 0)
        comp_names = [c["competition"] for c in data.get("competitions", [])]
        self.assertIn("England/Premier League", comp_names)
        self.assertIn("Spain/La Liga", comp_names)


if __name__ == "__main__":
    unittest.main()
