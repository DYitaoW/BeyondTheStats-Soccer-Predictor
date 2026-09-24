#!/usr/bin/env python3
"""Tests for /api/past-games Monday-Sunday weekly pagination."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "Website") not in sys.path:
    sys.path.insert(0, str(ROOT / "Website"))

import app as flask_app
from app import _week_bounds_from_iso


class PastGamesWeeklyPaginationTests(unittest.TestCase):
    def setUp(self):
        flask_app.app.config["TESTING"] = True
        self.client = flask_app.app.test_client()

    def test_week_bounds_calculation(self):
        # 2026-09-24 is a Thursday -> Monday is 2026-09-21, Sunday is 2026-09-27
        bounds = _week_bounds_from_iso("2026-09-24")
        self.assertIsNotNone(bounds)
        mon, sun, label = bounds
        self.assertEqual(mon, "2026-09-21")
        self.assertEqual(sun, "2026-09-27")
        self.assertIn("Sep 21", label)
        self.assertIn("Sep 27, 2026", label)

        # 2026-09-28 is a Monday -> Monday is 2026-09-28, Sunday is 2026-10-04
        bounds2 = _week_bounds_from_iso("2026-09-28")
        self.assertEqual(bounds2[0], "2026-09-28")
        self.assertEqual(bounds2[1], "2026-10-04")

        # 2026-09-20 is a Sunday -> Monday is 2026-09-14, Sunday is 2026-09-20
        bounds3 = _week_bounds_from_iso("2026-09-20")
        self.assertEqual(bounds3[0], "2026-09-14")
        self.assertEqual(bounds3[1], "2026-09-20")

    def test_api_past_games_weekly_pagination(self):
        mock_archive = [
            # Week 1 (most recent): 2026-09-28 (Monday)
            {
                "match_date": "2026-09-28",
                "competition": "International/Friendly",
                "home_team": "Turkmenistan",
                "away_team": "Palestine",
                "actual_result": "D",
                "actual_home_goals": 0,
                "actual_away_goals": 0,
            },
            # Week 2: 2026-09-24 (Thursday) and 2026-09-21 (Monday)
            {
                "match_date": "2026-09-24",
                "competition": "International/Friendly",
                "home_team": "Uzbekistan",
                "away_team": "Iran",
                "actual_result": "H",
                "actual_home_goals": 3,
                "actual_away_goals": 1,
            },
            {
                "match_date": "2026-09-21",
                "competition": "International/Friendly",
                "home_team": "Dominica",
                "away_team": "Anguilla",
                "actual_result": "H",
                "actual_home_goals": 1,
                "actual_away_goals": 0,
            },
            # Week 3: 2026-09-20 (Sunday)
            {
                "match_date": "2026-09-20",
                "competition": "England/Premier League",
                "home_team": "Bournemouth",
                "away_team": "Liverpool",
                "actual_result": "A",
                "actual_home_goals": 0,
                "actual_away_goals": 1,
            },
        ]

        with mock.patch("shared.sqlite_store.load_past_games", return_value=mock_archive), \
             mock.patch("shared.sqlite_store.count_rows", return_value=len(mock_archive)), \
             mock.patch("shared.sqlite_store.load_upcoming_games", return_value=[]), \
             mock.patch.object(flask_app, "_collect_live_past_game_rows", return_value=[]), \
             mock.patch.object(flask_app, "_load_upcoming_rows", return_value=([], None, None)):

            # Page 1 -> Week of 2026-09-28 to 2026-10-04
            resp1 = self.client.get("/api/past-games?page=1")
            self.assertEqual(resp1.status_code, 200)
            data1 = resp1.get_json()
            self.assertTrue(data1["ok"])
            self.assertEqual(data1["page"], 1)
            self.assertEqual(data1["total_weeks"], 3)
            self.assertEqual(data1["total"], 4)
            self.assertEqual(data1["week_start"], "2026-09-28")
            self.assertEqual(data1["week_end"], "2026-10-04")
            self.assertEqual(len(data1["rows"]), 1)
            self.assertEqual(data1["rows"][0]["home_team"], "Turkmenistan")

            # Page 2 -> Week of 2026-09-21 to 2026-09-27
            resp2 = self.client.get("/api/past-games?page=2")
            data2 = resp2.get_json()
            self.assertEqual(data2["page"], 2)
            self.assertEqual(data2["week_start"], "2026-09-21")
            self.assertEqual(data2["week_end"], "2026-09-27")
            self.assertEqual(len(data2["rows"]), 2)

            # Page 3 -> Week of 2026-09-14 to 2026-09-20
            resp3 = self.client.get("/api/past-games?page=3")
            data3 = resp3.get_json()
            self.assertEqual(data3["page"], 3)
            self.assertEqual(data3["week_start"], "2026-09-14")
            self.assertEqual(data3["week_end"], "2026-09-20")
            self.assertEqual(len(data3["rows"]), 1)
            self.assertEqual(data3["rows"][0]["home_team"], "Bournemouth")

            # Direct date query: ?date=2026-09-24 jumps to Page 2
            resp_date = self.client.get("/api/past-games?date=2026-09-24")
            data_date = resp_date.get_json()
            self.assertEqual(data_date["page"], 2)
            self.assertEqual(data_date["week_start"], "2026-09-21")
            self.assertEqual(len(data_date["rows"]), 2)

            # Available weeks metadata check
            self.assertEqual(len(data1["available_weeks"]), 3)
            self.assertEqual(data1["available_weeks"][0]["page"], 1)
            self.assertEqual(data1["available_weeks"][0]["week_start"], "2026-09-28")
            self.assertEqual(data1["available_weeks"][0]["total_games"], 1)
            self.assertEqual(data1["available_weeks"][1]["page"], 2)
            self.assertEqual(data1["available_weeks"][1]["week_start"], "2026-09-21")
            self.assertEqual(data1["available_weeks"][1]["total_games"], 2)


if __name__ == "__main__":
    unittest.main()