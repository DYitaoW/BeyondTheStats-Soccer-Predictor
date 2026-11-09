"""Test runs must never write to the production SQLite store.

Unisolated tests used to insert a fake "Liverpool 1-1 Everton" into the real
``past_games`` (giving both clubs an extra game in the live Premier League
table), fake device subscriptions and fake notification-feed events.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import paths, sqlite_store


class StoreIsolationTests(unittest.TestCase):
    def test_default_path_is_not_production_under_test_runner(self):
        self.assertTrue(sqlite_store._running_under_test_runner())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BTS_SQLITE_STORE_PATH", None)
            os.environ.pop("BTS_ALLOW_REAL_STORE_IN_TESTS", None)
            resolved = sqlite_store._db_path()
        self.assertNotEqual(Path(resolved).resolve(), Path(paths.SQLITE_STORE_FILE).resolve())
        self.assertEqual(resolved, sqlite_store._db_path())  # stable per process

    def test_explicit_override_still_wins(self):
        with mock.patch.dict(os.environ, {"BTS_SQLITE_STORE_PATH": "/tmp/explicit-bts.db"}):
            self.assertEqual(Path(sqlite_store._db_path()).resolve(), Path("/tmp/explicit-bts.db").resolve())

    def test_opt_out_uses_real_store(self):
        with mock.patch.dict(os.environ, {"BTS_ALLOW_REAL_STORE_IN_TESTS": "1"}):
            os.environ.pop("BTS_SQLITE_STORE_PATH", None)
            self.assertEqual(
                Path(sqlite_store._default_db_path()).resolve(),
                Path(paths.SQLITE_STORE_FILE).resolve(),
            )

    def test_writes_land_in_isolated_store(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BTS_SQLITE_STORE_PATH", None)
            sqlite_store.upsert_past_games([{
                "prediction_key": "isolation-probe", "match_date_iso": "2000-01-01",
                "competition": "Test/Isolation", "home_team": "A", "away_team": "B",
                "actual_home_goals": 1, "actual_away_goals": 0, "actual_result": "H",
            }])
            db = Path(sqlite_store._db_path()).resolve()
            self.assertEqual(db.parent, Path(sqlite_store._TEST_DB_DIR).resolve())
            self.assertNotEqual(db, Path(paths.SQLITE_STORE_FILE).resolve())


class PredictionKeyNaNTests(unittest.TestCase):
    """CSV rows without a prediction_key arrive as NaN; they must not all
    collapse into a single ``prediction:nan`` row."""

    def test_nan_prediction_keys_fall_back_to_fixture_keys(self):
        rows = [
            {"prediction_key": float("nan"), "match_date_iso": "2000-01-0%d" % i,
             "competition": "Test/Isolation", "home_team": f"H{i}", "away_team": f"A{i}",
             "actual_home_goals": 1, "actual_away_goals": 0, "actual_result": "H"}
            for i in (1, 2)
        ]
        keys = {sqlite_store.past_game_storage_key(r) for r in rows}
        self.assertEqual(len(keys), 2)
        self.assertTrue(all(k.startswith("fixture:") for k in keys))
        for value in ("nan", "None", "NULL", "", None):
            self.assertEqual(sqlite_store._clean_prediction_key(value), "")
        self.assertEqual(sqlite_store._clean_prediction_key(" real-key "), "real-key")


if __name__ == "__main__":
    unittest.main()
