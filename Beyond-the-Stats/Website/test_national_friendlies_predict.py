"""Tests for national-team friendlies-only prediction mode."""
from __future__ import annotations

import importlib
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
        self.assertIn("training files left untouched", src)
        self.assertIn("national_team_recent_matches_raw.csv", src)
        self.assertIn("--skip-fetch", src)


if __name__ == "__main__":
    unittest.main()
