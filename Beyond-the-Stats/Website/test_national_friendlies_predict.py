"""Tests for national-team friendlies-only prediction mode."""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd


WEBSITE_DIR = Path(__file__).resolve().parent
ROOT_DIR = WEBSITE_DIR.parent
FILES_DIR = ROOT_DIR / "pipelines" / "europe" / "files"
MAIN_DIR = ROOT_DIR / "main"
for path in (WEBSITE_DIR, FILES_DIR, MAIN_DIR, ROOT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


class NationalFriendliesPredictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pred = importlib.import_module("Predict_Upcoming_National_Team_Games")

    def test_competition_configs_friendlies_only(self):
        configs = self.pred.competition_configs(friendlies_only=True)
        self.assertEqual(set(configs.keys()), {"International/Friendly"})
        wc = self.pred.competition_configs(world_cup_only=True)
        self.assertEqual(set(wc.keys()), {"International/World Cup"})

    def test_pipeline_wires_friendlies_when_wc_inactive(self):
        src = (MAIN_DIR / "Run_All_Pipeline.py").read_text(encoding="utf-8")
        self.assertIn("--friendlies-only", src)
        self.assertIn("upcoming_international_friendlies", src)
        self.assertIn("World Cup inactive", src)
        self.assertNotIn(
            'print("[skip] No active World Cup window — skipping World Cup steps")',
            src,
        )

    def test_empty_friendlies_keeps_existing_store(self):
        with mock.patch.object(self.pred.national, "load_model_bundle", return_value={"ok": True}):
            with mock.patch.object(self.pred, "load_upcoming_fixtures", return_value=pd.DataFrame()):
                with mock.patch.object(self.pred, "_write_national_predictions") as write:
                    with mock.patch.object(
                        self.pred,
                        "parse_cli_args",
                        return_value=mock.Mock(
                            window_days=14,
                            world_cup_only=False,
                            friendlies_only=True,
                            api_token="",
                        ),
                    ):
                        self.pred.main()
        write.assert_not_called()

    def test_pipeline_preserves_training_when_cache_exists(self):
        src = (MAIN_DIR / "Run_All_Pipeline.py").read_text(encoding="utf-8")
        self.assertIn("processed model files left untouched", src)
        self.assertIn("national_team_recent_matches_raw.csv", src)
        self.assertIn("--archive-matches", src)
        self.assertIn("--skip-fetch", src)
        archive_at = src.index("--archive-matches")
        reuse_at = src.index("processed model files left untouched")
        self.assertLess(archive_at, reuse_at)

    def test_archive_appends_new_matches_and_mirrors_sqlite(self):
        import tempfile

        nat = importlib.import_module("Process_National_Team_Data")
        existing = {
            "match_id": "old-1",
            "match_datetime_utc": "2024-06-01T18:00:00+00:00",
            "match_date": "2024-06-01",
            "competition": "International/Friendly",
            "home_team": "Brazil",
            "away_team": "Argentina",
            "FTHG": 1,
            "FTAG": 1,
            "FTR": "D",
        }
        incoming_dup = dict(existing)
        incoming_dup["FTHG"] = 9
        incoming_new = {
            "match_id": "new-2",
            "match_datetime_utc": "2026-09-10T18:00:00+00:00",
            "match_date": "2026-09-10",
            "competition": "International/Friendly",
            "home_team": "Spain",
            "away_team": "France",
            "FTHG": 2,
            "FTAG": 0,
            "FTR": "H",
        }
        merged, added = nat.merge_national_match_rows([existing], [incoming_dup, incoming_new])
        self.assertEqual(added, 1)
        self.assertEqual(len(merged), 2)
        kept = next(row for row in merged if row["match_id"] == "old-1")
        self.assertEqual(kept["FTHG"], 1)

        with tempfile.TemporaryDirectory() as tmp:
            raw_path = Path(tmp) / "national_team_recent_matches_raw.csv"
            db_path = Path(tmp) / "bts_store.db"
            pd.DataFrame([existing]).to_csv(raw_path, index=False)
            with mock.patch.object(nat, "RAW_MATCHES_FILE", str(raw_path)):
                with mock.patch.object(nat, "ranked_national_teams", return_value=["Brazil", "Spain"]):
                    with mock.patch.object(
                        nat,
                        "fetch_completed_national_matches",
                        return_value=[incoming_dup, incoming_new],
                    ):
                        with mock.patch.dict(os.environ, {"BTS_SQLITE_STORE_PATH": str(db_path)}):
                            result = nat.archive_national_match_history(
                                mock.Mock(
                                    friendlies_only=True,
                                    world_cup_only=False,
                                    archive_lookback_days=45,
                                    lookback_days=900,
                                )
                            )
            self.assertEqual(result["added"], 1)
            self.assertEqual(result["total"], 2)
            saved = pd.read_csv(raw_path)
            self.assertEqual(len(saved), 2)
            old = saved[saved["match_id"] == "old-1"].iloc[0]
            self.assertEqual(int(old["FTHG"]), 1)
            from shared import sqlite_store

            loaded = sqlite_store.load_past_games(db_path=db_path)
            self.assertEqual(len(loaded), 2)
            sources = {row.get("archive_source") for row in loaded}
            self.assertEqual(sources, {"national_training"})
            brazil = next(row for row in loaded if row.get("home_team") == "Brazil")
            self.assertEqual(brazil.get("actual_result"), "D")
            self.assertEqual(int(brazil.get("actual_home_goals")), 1)


if __name__ == "__main__":
    unittest.main()
