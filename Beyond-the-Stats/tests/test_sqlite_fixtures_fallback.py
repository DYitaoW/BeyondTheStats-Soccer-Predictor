import unittest
import tempfile
import sqlite3
from pathlib import Path
from datetime import date, datetime, timezone
import pandas as pd

import sys
repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root / "shared"))
sys.path.insert(0, str(repo_root / "pipelines" / "mls" / "files"))

import sqlite_store as store
import season_calendar


class SqliteFixtureFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_store.db"
        store.ensure_store(self.db_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_load_upcoming_fixtures_dataframe_extracts_and_never_deletes(self):
        test_rows = [
            {
                "prediction_key": "2026-10-01|United States/MLS|lafc|lagalaxy",
                "match_date": "2026-10-01",
                "match_datetime_utc": "2026-10-01T23:30:00Z",
                "competition": "United States/MLS",
                "home_team": "LAFC",
                "away_team": "LA Galaxy",
                "status": "upcoming",
            },
            {
                "prediction_key": "2026-10-05|United States/MLS|seattle|portland",
                "match_date": "2026-10-05",
                "match_datetime_utc": "2026-10-05T20:00:00Z",
                "competition": "United States/MLS",
                "home_team": "Seattle Sounders",
                "away_team": "Portland Timbers",
                "status": "upcoming",
            },
            {
                "prediction_key": "2026-09-10|United States/MLS|settled|game",
                "match_date": "2026-09-10",
                "match_datetime_utc": "2026-09-10T20:00:00Z",
                "competition": "United States/MLS",
                "home_team": "Team A",
                "away_team": "Team B",
                "actual_result": "H",
                "status": "settled",
            },
        ]
        store.upsert_upcoming_games(test_rows, source="mls", db_path=self.db_path)
        count_before = store.count_rows("upcoming_games", db_path=self.db_path)
        self.assertEqual(count_before, 3)

        df = store.load_upcoming_fixtures_dataframe(
            competitions=["United States/MLS"],
            reference_date=date(2026, 9, 21),
            window_days=30,
            db_path=self.db_path,
        )

        self.assertIsNotNone(df)
        self.assertEqual(len(df), 2)
        self.assertIn("home_team", df.columns)
        self.assertIn("away_team", df.columns)
        self.assertIn("match_date", df.columns)
        self.assertEqual(df.iloc[0]["home_team"], "LAFC")

        # STRICT VERIFICATION: Ensure SQLite row count is untouched (no deletions)
        count_after = store.count_rows("upcoming_games", db_path=self.db_path)
        self.assertEqual(count_before, count_after, "SQLite store was mutated or deleted from!")

    def test_international_window_detection(self):
        # FIFA October window is active around Oct 3-18
        self.assertTrue(season_calendar.is_near_international_window(date(2026, 10, 5), 7))
        self.assertTrue(season_calendar.is_near_international_window(date(2026, 9, 25), 14))

        # Off-break mid-January
        self.assertFalse(season_calendar.is_near_international_window(date(2026, 1, 15), 14))

    def test_mls_has_team_mapping_file_defined(self):
        import Predict_Upcoming_Matchweek as mls_predict
        self.assertTrue(hasattr(mls_predict, "TEAM_MAPPING_FILE"))
        self.assertTrue(isinstance(mls_predict.TEAM_MAPPING_FILE, str))
        self.assertTrue(len(mls_predict.TEAM_MAPPING_FILE) > 0)


if __name__ == "__main__":
    unittest.main()

