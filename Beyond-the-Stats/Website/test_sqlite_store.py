#!/usr/bin/env python3
"""SQLite store for past games + live-score history."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class SqliteStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "bts_store.db"
        self.past_json = Path(self.tmp.name) / "past_games.json"
        self.live_json = Path(self.tmp.name) / "live_score_history.json"
        self.journal = Path(self.tmp.name) / "past_games_journal.jsonl"

        from shared import sqlite_store as store

        self.store = store
        self._path_patches = [
            mock.patch.object(store, "_default_db_path", return_value=self.db),
            mock.patch.object(store, "_past_games_json", return_value=self.past_json),
            mock.patch.object(store, "_past_games_journal", return_value=self.journal),
            mock.patch.object(store, "_live_history_json", return_value=self.live_json),
            mock.patch.dict(os.environ, {"BTS_SQLITE_STORE_PATH": str(self.db)}),
        ]
        for patch in self._path_patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_upsert_and_load_past_games(self):
        rows = [
            {
                "prediction_key": "pk-1",
                "match_date": "2026-09-18",
                "competition": "England/Premier League",
                "home_team": "Arsenal",
                "away_team": "Chelsea",
                "actual_result": "H",
            },
            {
                "prediction_key": "pk-1",
                "match_date": "2026-09-18",
                "competition": "England/Premier League",
                "home_team": "Arsenal",
                "away_team": "Chelsea",
                "actual_result": "H",
                "actual_home_goals": 2,
                "actual_away_goals": 0,
            },
        ]
        result = self.store.upsert_past_games(rows)
        self.assertEqual(result["upserted"], 2)
        loaded = self.store.load_past_games()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["actual_home_goals"], 2)
        filtered = self.store.load_past_games(competition_substr="premier")
        self.assertEqual(len(filtered), 1)
        none = self.store.load_past_games(competition_substr="Serie A")
        self.assertEqual(none, [])

    def test_upsert_and_load_live_history(self):
        rows = [
            {
                "match_id": "espn-1",
                "kickoff_utc": "2026-09-18T19:00:00Z",
                "competition": "USA/Major League Soccer",
                "home_team": "Inter Miami",
                "away_team": "Atlanta United",
                "home_score": 1,
                "away_score": 0,
            }
        ]
        result = self.store.upsert_live_score_history(rows)
        self.assertEqual(result["upserted"], 1)
        loaded = self.store.load_live_score_history(competition_substr="Major League")
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["match_id"], "espn-1")

    def test_migrate_from_json(self):
        self.past_json.write_text(
            json.dumps(
                [
                    {
                        "prediction_key": "old",
                        "match_date": "2020-01-01",
                        "competition": "Spain/La Liga",
                        "home_team": "Barcelona",
                        "away_team": "Sevilla",
                        "actual_result": "D",
                    }
                ]
            ),
            encoding="utf-8",
        )
        self.live_json.write_text(
            json.dumps(
                [
                    {
                        "match_id": "legacy-1",
                        "kickoff_utc": "2026-01-01T12:00:00Z",
                        "competition": "Germany/Bundesliga",
                        "home_team": "Bayern Munich",
                        "away_team": "Dortmund",
                    }
                ]
            ),
            encoding="utf-8",
        )
        migrated = self.store.migrate_json_archives(force=True)
        self.assertGreaterEqual(migrated["past_games"], 1)
        self.assertGreaterEqual(migrated["live_score_history"], 1)
        self.assertEqual(self.store.count_rows("past_games"), 1)
        self.assertEqual(self.store.count_rows("live_score_history"), 1)
        # Second migrate is a no-op unless forced.
        again = self.store.migrate_json_archives()
        self.assertTrue(again["skipped"])

    def test_json_prune_does_not_erase_sqlite(self):
        # Simulate long-term SQLite retention while JSON keeps a shorter window.
        self.store.upsert_live_score_history(
            [
                {
                    "match_id": "old-match",
                    "kickoff_utc": "2020-01-01T12:00:00Z",
                    "competition": "England/Premier League",
                    "home_team": "Arsenal",
                    "away_team": "Chelsea",
                },
                {
                    "match_id": "new-match",
                    "kickoff_utc": "2026-09-18T12:00:00Z",
                    "competition": "England/Premier League",
                    "home_team": "Liverpool",
                    "away_team": "Everton",
                },
            ]
        )
        # Partial JSON snapshot (pruned) must not delete SQLite rows.
        self.store.replace_live_score_history_snapshot(
            [
                {
                    "match_id": "new-match",
                    "kickoff_utc": "2026-09-18T12:00:00Z",
                    "competition": "England/Premier League",
                    "home_team": "Liverpool",
                    "away_team": "Everton",
                }
            ]
        )
        ids = {r["match_id"] for r in self.store.load_live_score_history()}
        self.assertIn("old-match", ids)
        self.assertIn("new-match", ids)

    def test_sync_upcoming_csvs_and_settled_to_past(self):
        import pandas as pd

        csv_path = Path(self.tmp.name) / "upcoming.csv"
        pd.DataFrame(
            [
                {
                    "prediction_key": "up-1",
                    "match_date": "2026-09-21",
                    "competition": "England/Premier League",
                    "home_team": "Arsenal",
                    "away_team": "Chelsea",
                    "predicted_result": "H",
                    "actual_result": "",
                },
                {
                    "prediction_key": "up-2",
                    "match_date": "2026-09-18",
                    "competition": "England/Premier League",
                    "home_team": "Liverpool",
                    "away_team": "Everton",
                    "predicted_result": "D",
                    "actual_result": "D",
                    "actual_home_goals": 1,
                    "actual_away_goals": 1,
                },
            ]
        ).to_csv(csv_path, index=False)

        result = self.store.sync_upcoming_predictions_from_csvs(
            sources=[("global", csv_path)],
            db_path=self.db,
        )
        self.assertEqual(result["upserted"], 2)
        upcoming = self.store.load_upcoming_games(status="upcoming")
        settled = self.store.load_upcoming_games(status="settled")
        self.assertEqual(len(upcoming), 1)
        self.assertEqual(len(settled), 1)
        past = self.store.load_past_games()
        self.assertEqual({r.get("prediction_key") for r in past}, {"up-2"})
        info = self.store.store_info(self.db)
        self.assertTrue(info["never_deletes"])
        self.assertEqual(info["tables"]["upcoming_games"], 2)

    def test_live_history_keeps_full_stats_on_update(self):
        self.store.upsert_live_score_history(
            [
                {
                    "match_id": "stats-1",
                    "kickoff_utc": "2026-09-18T19:00:00Z",
                    "competition": "USA/Major League Soccer",
                    "home_team": "Inter Miami",
                    "away_team": "Atlanta United",
                    "home_score": 2,
                    "away_score": 1,
                    "status": "post",
                }
            ]
        )
        self.store.upsert_live_score_history(
            [
                {
                    "match_id": "stats-1",
                    "kickoff_utc": "2026-09-18T19:00:00Z",
                    "competition": "USA/Major League Soccer",
                    "home_team": "Inter Miami",
                    "away_team": "Atlanta United",
                    "home_score": 2,
                    "away_score": 1,
                    "status": "post",
                    "lineups": {"home": ["A"], "away": ["B"]},
                    "boxscore_stats": {"possession": {"home": 55, "away": 45}},
                    "key_events": [{"type": "goal", "team": "home"}],
                }
            ]
        )
        loaded = self.store.load_live_score_history()
        self.assertEqual(len(loaded), 1)
        self.assertIn("lineups", loaded[0])
        self.assertIn("boxscore_stats", loaded[0])
        self.assertEqual(loaded[0]["home_score"], 2)


class PastGamesSqliteWriterTests(unittest.TestCase):
    def test_save_completed_dual_writes_sqlite(self):
        sys.path.insert(0, str(ROOT / "pipelines" / "europe" / "files"))
        import importlib

        import Update_Live_Prediction_Results as live
        import pandas as pd

        importlib.reload(live)
        with tempfile.TemporaryDirectory() as tmp:
            past = Path(tmp) / "past_games.json"
            journal = Path(tmp) / "past_games_journal.jsonl"
            backup = Path(tmp) / "past_games.prev.json"
            db = Path(tmp) / "bts_store.db"
            frame = pd.DataFrame(
                [
                    {
                        "prediction_key": "sqlite-key",
                        "match_date": "2026-09-18",
                        "competition": "England/Premier League",
                        "home_team": "Liverpool",
                        "away_team": "Everton",
                        "actual_result": "D",
                        "actual_home_goals": 1,
                        "actual_away_goals": 1,
                    }
                ]
            )
            with mock.patch.dict(
                os.environ,
                {
                    "BTS_PAST_GAMES_RETENTION_DAYS": "0",
                    "BTS_SQLITE_STORE_PATH": str(db),
                },
            ):
                with mock.patch.object(live, "PAST_GAMES_FILE", str(past)), mock.patch.object(
                    live, "PAST_GAMES_JOURNAL_FILE", str(journal)
                ), mock.patch.object(live, "PAST_GAMES_BACKUP_FILE", str(backup)):
                    added = live.save_completed_rows_to_past_games(frame)

            self.assertGreaterEqual(added, 1)
            from shared import sqlite_store as store

            with mock.patch.dict(os.environ, {"BTS_SQLITE_STORE_PATH": str(db)}):
                loaded = store.load_past_games()
            keys = {str(r.get("prediction_key")) for r in loaded}
            self.assertIn("sqlite-key", keys)


class PathsSqliteConstantTests(unittest.TestCase):
    def test_sqlite_path_under_output_status(self):
        from shared import paths as paths_mod

        self.assertTrue(
            str(paths_mod.SQLITE_STORE_FILE).startswith(str(paths_mod.OUTPUT_STATUS_DIR))
        )
        self.assertEqual(paths_mod.PAST_GAMES_DB_FILE, paths_mod.SQLITE_STORE_FILE)
        self.assertEqual(paths_mod.LIVE_SCORE_HISTORY_DB_FILE, paths_mod.SQLITE_STORE_FILE)


if __name__ == "__main__":
    unittest.main()
