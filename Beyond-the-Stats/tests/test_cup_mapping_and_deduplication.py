"""Tests for cup team mapping safeguards, qualifier isolation, and upcoming deduplication."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_WEBSITE = _ROOT / "Website"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_WEBSITE) not in sys.path:
    sys.path.insert(0, str(_WEBSITE))

from starlette.testclient import TestClient

from app import app
from competition_rules import canonical_team_name, make_match_sync_key, normalize_team_key
from shared import team_mapping_groups as tmg
from routes.predictions import _deduplicate_and_merge_upcoming_rows


class TestCupMappingAndDeduplication(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_canonical_team_name_cup_qualifiers(self):
        """Oxford City and Bristol Rovers must not be mapped to Oxford or Bristol City."""
        self.assertEqual(canonical_team_name("Oxford City", "England/FA Cup"), "Oxford City")
        self.assertEqual(canonical_team_name("Oxford City FC", "England/FA Cup"), "Oxford City")
        self.assertEqual(canonical_team_name("Oxford United", "England/FA Cup"), "Oxford")
        self.assertEqual(canonical_team_name("Oxford United FC", "England/FA Cup"), "Oxford")

        self.assertEqual(canonical_team_name("Bristol Rovers", "England/FA Cup"), "Bristol Rovers")
        self.assertEqual(canonical_team_name("Bristol Rovers FC", "England/FA Cup"), "Bristol Rovers")
        self.assertEqual(canonical_team_name("Bristol City", "England/FA Cup"), "Bristol City")
        self.assertEqual(canonical_team_name("Bristol City FC", "England/FA Cup"), "Bristol City")

    def test_normalize_team_key_preserves_qualifiers(self):
        """normalize_team_key must preserve club qualifiers like City, United, Rovers."""
        self.assertNotEqual(
            normalize_team_key("Oxford City"),
            normalize_team_key("Oxford"),
        )
        self.assertNotEqual(
            normalize_team_key("Oxford City"),
            normalize_team_key("Oxford United"),
        )
        self.assertNotEqual(
            normalize_team_key("Bristol Rovers"),
            normalize_team_key("Bristol City"),
        )
        self.assertNotEqual(
            normalize_team_key("Manchester City"),
            normalize_team_key("Manchester United"),
        )

    def test_fuzzy_resolve_qualifier_isolation(self):
        """Lower-tier or unmapped cup clubs must not be hijacked by senior clubs with different qualifiers."""
        senior_teams = ["Oxford", "Bristol City", "Leicester", "Arsenal", "Cambridge"]
        self.assertIsNone(tmg.fuzzy_resolve_team_name("Oxford City", senior_teams))
        self.assertIsNone(tmg.fuzzy_resolve_team_name("Bristol Rovers", senior_teams))
        self.assertIsNone(tmg.fuzzy_resolve_team_name("Manchester United", ["Manchester City"]))

    def test_deterministic_sync_keys(self):
        """Deterministic sync keys must be generated with canonical slugs."""
        key1 = make_match_sync_key("2026-11-06", "England/FA Cup", "Oxford City", "Leicester City")
        self.assertEqual(key1, "2026-11-06|oxfordcity|leicester")

        key2 = make_match_sync_key("2026-11-06", "England/FA Cup", "Bristol Rovers", "Cambridge United")
        self.assertEqual(key2, "2026-11-06|bristolrovers|cambridgeunited")

    def test_deduplicate_and_merge_upcoming_rows(self):
        """Rows with the same fixture must merge prediction stats and ESPN match_id without duplicating."""
        future_date = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")

        # Row 1: Model prediction without match_id
        row1 = {
            "match_date": future_date,
            "match_date_iso": future_date,
            "competition": "England/FA Cup",
            "home_team": "Oxford City",
            "away_team": "Leicester City",
            "has_prediction": True,
            "prediction_quality": "prediction",
            "prob_home": 15.0,
            "prob_draw": 25.0,
            "prob_away": 60.0,
            "pred_home_goals": 1,
            "pred_away_goals": 3,
            "sync_key": f"{future_date}|oxfordcity|leicester",
            "match_id": None,
        }

        # Row 2: ESPN schedule feed with match_id but schedule-only
        row2 = {
            "match_date": future_date,
            "match_date_iso": future_date,
            "competition": "England/FA Cup",
            "home_team": "Oxford City FC",
            "away_team": "Leicester",
            "has_prediction": False,
            "prediction_quality": "no_prediction",
            "schedule_only": True,
            "sync_key": f"{future_date}|oxfordcity|leicester",
            "match_id": "401999999",
            "live_status": "scheduled",
        }

        merged = _deduplicate_and_merge_upcoming_rows([row1, row2])
        self.assertEqual(len(merged), 1, "Expected duplicate rows to be merged into a single entry")
        res = merged[0]
        # Must retain prediction fields
        self.assertTrue(res["has_prediction"])
        self.assertEqual(res["pred_home_goals"], 1)
        self.assertEqual(res["pred_away_goals"], 3)
        # Must acquire ESPN match_id
        self.assertEqual(res["match_id"], "401999999")
        self.assertEqual(res["live_status"], "scheduled")

    def test_api_upcoming_deduplication_and_match_id(self):
        """GET /api/upcoming/global must return non-duplicated fixtures with match_id.

        Seeds its own fixtures (relative date) instead of relying on whatever
        happened to be in the production store on a hardcoded day.
        """
        from unittest import mock
        import routes.predictions as rp
        from shared import sqlite_store

        day = (datetime.now(timezone.utc) + timedelta(days=4)).strftime("%Y-%m-%d")
        prediction_rows = [
            {
                "match_date": day, "match_date_iso": day, "competition": "England/FA Cup",
                "home_team": home, "away_team": away, "has_prediction": True,
                "prediction_quality": "prediction", "prob_home": 20.0, "prob_draw": 25.0,
                "prob_away": 55.0, "pred_home_goals": 1, "pred_away_goals": 2,
                "sync_key": make_match_sync_key(day, "England/FA Cup", home, away), "match_id": None,
            }
            for home, away in (("Oxford City", "Leicester City"), ("Bristol Rovers", "Cambridge United"))
        ]
        espn_rows = [
            {
                "match_date": day, "match_date_iso": day, "competition": "England/FA Cup",
                "home_team": home, "away_team": away, "has_prediction": False,
                "prediction_quality": "no_prediction", "schedule_only": True,
                "sync_key": make_match_sync_key(day, "England/FA Cup", home, away),
                "match_id": mid, "live_status": "scheduled",
            }
            for home, away, mid in (
                ("Oxford City FC", "Leicester", "401999001"),
                ("Bristol Rovers FC", "Cambridge United", "401999002"),
            )
        ]
        with mock.patch.object(rp, "merged_upcoming_file_is_fresh", return_value=False), \
                mock.patch.object(sqlite_store, "count_rows", return_value=len(prediction_rows)), \
                mock.patch.object(sqlite_store, "load_upcoming_games", return_value=[dict(r) for r in prediction_rows]), \
                mock.patch.object(rp, "regional_espn_schedule_fallback",
                                  side_effect=lambda rows: list(rows) + [dict(r) for r in espn_rows]):
            resp = self.client.get(f"/api/upcoming/global?date={day}&no_cache=1")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))

        rows = data.get("rows", [])
        oxford_rows = [
            r for r in rows
            if "oxford city" in str(r.get("home_team", "")).lower() or "oxford city" in str(r.get("display_home_team", "")).lower()
        ]
        bristol_rows = [
            r for r in rows
            if "bristol rovers" in str(r.get("home_team", "")).lower() or "bristol rovers" in str(r.get("display_home_team", "")).lower()
        ]

        self.assertEqual(len(oxford_rows), 1, "Oxford City match should appear exactly once")
        self.assertEqual(len(bristol_rows), 1, "Bristol Rovers match should appear exactly once")

        ox_match = oxford_rows[0]
        self.assertEqual(ox_match.get("match_id"), "401999001")
        self.assertEqual(ox_match.get("sync_key"), f"{day}|oxfordcity|leicester")
        self.assertTrue(ox_match.get("has_prediction"))

        br_match = bristol_rows[0]
        self.assertEqual(br_match.get("match_id"), "401999002")
        self.assertEqual(br_match.get("sync_key"), f"{day}|bristolrovers|cambridgeunited")
        self.assertTrue(br_match.get("has_prediction"))


if __name__ == "__main__":
    unittest.main()

