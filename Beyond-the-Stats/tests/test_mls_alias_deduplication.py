"""Regression tests for MLS upcoming fixture deduplication across aliases and API endpoints."""

import sys
import tempfile
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
from competition_rules import canonical_team_name, make_match_sync_key
from routes.predictions import _deduplicate_and_merge_upcoming_rows, _fixture_dedup_key
from shared import sqlite_store


class TestMLSAliasDeduplication(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_mls_team_alias_canonicalization(self):
        """MLS aliases must resolve to identical canonical names and sync keys."""
        comp = "United States/MLS"
        test_pairs = [
            ("Seattle Sounders FC", "LAFC", "Seattle Sounders", "Los Angeles FC"),
            ("Real Salt Lake", "LA Galaxy", "Real Salt Lake", "Los Angeles Galaxy"),
            ("CF Montréal", "Vancouver Whitecaps", "CF Montreal", "Vancouver Whitecaps"),
            ("Chicago Fire FC", "Columbus Crew", "Chicago Fire", "Columbus Crew"),
            ("D.C. United", "New York City FC", "DC United", "New York City"),
            ("Inter Miami CF", "Charlotte FC", "Inter Miami", "Charlotte"),
            ("Red Bull New York", "Atlanta United FC", "New York Red Bulls", "Atlanta Utd"),
            ("Orlando City SC", "New England Revolution", "Orlando City", "New England Revolution"),
            ("Houston Dynamo FC", "St. Louis CITY SC", "Houston Dynamo", "St. Louis City"),
            ("Minnesota United FC", "San Jose Earthquakes", "Minnesota United", "San Jose Earthquakes"),
        ]

        test_date = "2026-11-07"
        for raw_home, raw_away, exp_home, exp_away in test_pairs:
            c_home = canonical_team_name(raw_home, comp)
            c_away = canonical_team_name(raw_away, comp)
            self.assertEqual(c_home, exp_home, f"Canonical home mismatch for {raw_home}")
            self.assertEqual(c_away, exp_away, f"Canonical away mismatch for {raw_away}")

            sk1 = make_match_sync_key(test_date, comp, raw_home, raw_away)
            sk2 = make_match_sync_key(test_date, comp, exp_home, exp_away)
            self.assertEqual(sk1, sk2, f"Sync keys should match for {raw_home} vs {raw_away}")

    def test_past_game_storage_key_alias_identical(self):
        """past_game_storage_key must return identical keys for alias variations."""
        row_raw = {
            "match_date_iso": "2026-11-07",
            "competition": "United States/MLS",
            "home_team": "Seattle Sounders FC",
            "away_team": "LAFC",
        }
        row_canon = {
            "match_date_iso": "2026-11-07",
            "competition": "United States/MLS",
            "home_team": "Seattle Sounders",
            "away_team": "Los Angeles FC",
        }
        key_raw = sqlite_store.past_game_storage_key(row_raw)
        key_canon = sqlite_store.past_game_storage_key(row_canon)
        self.assertEqual(key_raw, key_canon)
        self.assertEqual(key_canon, "fixture:2026-11-07|united states/mls|seattle sounders|los angeles fc")

    def test_sqlite_upsert_deduplicates_alias_fixtures(self):
        """upsert_upcoming_games must merge alias entries into a single row."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_db = Path(tmp_dir) / "test_store.db"
            sqlite_store.ensure_store(test_db)

            # Insert raw alias row first
            row1 = {
                "match_date_iso": "2026-11-07",
                "competition": "United States/MLS",
                "home_team": "Seattle Sounders FC",
                "away_team": "LAFC",
                "sync_key": "2026-11-07|seattlesounders|losangeles",
                "has_prediction": False,
                "schedule_only": True,
            }
            res1 = sqlite_store.upsert_upcoming_games([row1], db_path=test_db)
            self.assertEqual(res1["upserted"], 1)

            # Insert enriched canonical row
            row2 = {
                "match_date_iso": "2026-11-07",
                "competition": "United States/MLS",
                "home_team": "Seattle Sounders",
                "away_team": "Los Angeles FC",
                "sync_key": "2026-11-07|seattlesounders|losangeles",
                "has_prediction": True,
                "prob_home": 46.1,
                "match_id": "761948",
            }
            res2 = sqlite_store.upsert_upcoming_games([row2], db_path=test_db)
            self.assertEqual(res2["upserted"], 1)

            # Check that only 1 row exists
            loaded = sqlite_store.load_upcoming_games(competition_substr="MLS", db_path=test_db)
            self.assertEqual(len(loaded), 1, "Expected exactly 1 merged row in SQLite")
            self.assertEqual(loaded[0]["match_id"], "761948")
            self.assertTrue(loaded[0]["has_prediction"])

    def test_deduplicate_and_merge_upcoming_rows_merges_aliases(self):
        """_deduplicate_and_merge_upcoming_rows must merge duplicate rows by sync_key or aliases."""
        rows = [
            {
                "match_date_iso": "2026-11-07",
                "competition": "United States/MLS",
                "home_team": "Red Bull New York",
                "away_team": "Atlanta United FC",
                "sync_key": "2026-11-07|newyorkredbulls|atlantautd",
                "match_id": "761939",
            },
            {
                "match_date_iso": "2026-11-07",
                "competition": "United States/MLS",
                "home_team": "New York Red Bulls",
                "away_team": "Atlanta Utd",
                "sync_key": "2026-11-07|newyorkredbulls|atlantautd",
                "has_prediction": True,
                "prob_home": 52.0,
                "pred_home_goals": 2,
            },
        ]
        merged = _deduplicate_and_merge_upcoming_rows(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["match_id"], "761939")
        self.assertEqual(merged[0]["pred_home_goals"], 2)

    def test_api_home_upcoming_no_mls_duplicates(self):
        """GET /api/home/upcoming must never return duplicate MLS fixtures."""
        resp = self.client.get("/api/home/upcoming")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        mls_rows = [r for r in data.get("rows", []) if "mls" in str(r.get("competition", "")).lower()]

        sync_keys = [r.get("sync_key") for r in mls_rows if r.get("sync_key")]
        unique_sync_keys = set(sync_keys)
        self.assertEqual(
            len(sync_keys),
            len(unique_sync_keys),
            f"Found duplicate sync_keys in /api/home/upcoming: {[k for k in sync_keys if sync_keys.count(k) > 1]}",
        )

    def test_api_upcoming_global_no_mls_duplicates(self):
        """GET /api/upcoming/global must never return duplicate MLS fixtures."""
        resp = self.client.get("/api/upcoming/global?date=2026-11-07")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        mls_rows = [r for r in data.get("rows", []) if "mls" in str(r.get("competition", "")).lower()]

        sync_keys = [r.get("sync_key") for r in mls_rows if r.get("sync_key")]
        unique_sync_keys = set(sync_keys)
        self.assertEqual(
            len(sync_keys),
            len(unique_sync_keys),
            f"Found duplicate sync_keys in /api/upcoming/global: {[k for k in sync_keys if sync_keys.count(k) > 1]}",
        )


if __name__ == "__main__":
    unittest.main()

