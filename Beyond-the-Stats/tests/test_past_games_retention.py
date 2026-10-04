#!/usr/bin/env python3
"""Past-games archive must not hard-prune history by default."""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class PastGamesRetentionTests(unittest.TestCase):
    def test_save_completed_keeps_old_rows_by_default(self):
        sys.path.insert(0, str(ROOT / "pipelines" / "europe" / "files"))
        import Update_Live_Prediction_Results as live

        importlib.reload(live)
        with tempfile.TemporaryDirectory() as tmp:
            past = Path(tmp) / "past_games.json"
            journal = Path(tmp) / "past_games_journal.jsonl"
            backup = Path(tmp) / "past_games.prev.json"
            old = {
                "prediction_key": "old-key",
                "match_date": "2020-01-15",
                "competition": "England/Premier League",
                "home_team": "Arsenal",
                "away_team": "Chelsea",
                "actual_result": "H",
                "actual_home_goals": 1,
                "actual_away_goals": 0,
            }
            past.write_text(json.dumps([old]), encoding="utf-8")
            frame = pd.DataFrame(
                [
                    {
                        "prediction_key": "new-key",
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
            with mock.patch.dict(os.environ, {"BTS_PAST_GAMES_RETENTION_DAYS": "0"}):
                with mock.patch.object(live, "PAST_GAMES_FILE", str(past)), mock.patch.object(
                    live, "PAST_GAMES_JOURNAL_FILE", str(journal)
                ), mock.patch.object(live, "PAST_GAMES_BACKUP_FILE", str(backup)):
                    added = live.save_completed_rows_to_past_games(frame)

            self.assertGreaterEqual(added, 1)
            saved = json.loads(past.read_text(encoding="utf-8"))
            keys = {str(r.get("prediction_key")) for r in saved}
            self.assertIn("old-key", keys)
            self.assertIn("new-key", keys)
            self.assertTrue(journal.is_file())
            self.assertTrue(backup.is_file())

    def test_source_mentions_journal_and_zero_default(self):
        source = (
            ROOT / "pipelines" / "europe" / "files" / "Update_Live_Prediction_Results.py"
        ).read_text(encoding="utf-8")
        self.assertIn("past_games_journal.jsonl", source)
        self.assertIn('BTS_PAST_GAMES_RETENTION_DAYS", "0"', source)
        self.assertNotIn("Prune rows older than 30 days", source)


if __name__ == "__main__":
    unittest.main()
