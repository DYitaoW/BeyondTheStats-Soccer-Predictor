"""End-to-end checks for past_games, live_score_history, and international rows."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FILES_DIR = ROOT / "pipelines" / "europe" / "files"
for path in (ROOT, FILES_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


class SqliteEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "bts_store.db"
        self.past_json = Path(self.tmp.name) / "past_games.json"
        self.live_json = Path(self.tmp.name) / "live_score_history.json"
        self.journal = Path(self.tmp.name) / "past_games_journal.jsonl"
        self.national_csv = Path(self.tmp.name) / "national_team_recent_matches_raw.csv"
        self.national_csv.write_text(
            "match_id,match_date,match_datetime_utc,competition,home_team,away_team,FTHG,FTAG,FTR\n"
            "intl-1,2025-10-10,2025-10-10T18:00:00+00:00,International/Friendly,"
            "Brazil,Chile,2,0,H\n",
            encoding="utf-8",
        )

        from shared import sqlite_store as store

        self.store = store
        patches = [
            mock.patch.object(store, "_default_db_path", return_value=self.db),
            mock.patch.object(store, "_past_games_json", return_value=self.past_json),
            mock.patch.object(store, "_past_games_journal", return_value=self.journal),
            mock.patch.object(store, "_live_history_json", return_value=self.live_json),
            mock.patch.object(store, "_national_raw_matches_csv", return_value=self.national_csv),
            mock.patch.dict(os.environ, {"BTS_SQLITE_STORE_PATH": str(self.db)}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_past_games_live_history_and_international(self):
        path = self.store.ensure_store()
        self.assertTrue(path.is_file())
        info = self.store.store_info()
        self.assertEqual(
            set(info["tables"]),
            {"past_games", "live_score_history", "upcoming_games", "squad_values"},
        )

        # National training CSV migrates into past_games on first ensure_store.
        past = self.store.load_past_games(competition_substr="Friendly")
        self.assertEqual(len(past), 1)
        self.assertEqual(past[0]["archive_source"], "national_training")
        self.assertEqual(past[0]["home_team"], "Brazil")
        self.assertEqual(str(past[0].get("actual_result")), "H")

        # Settled club past game dual-write shape.
        club = {
            "prediction_key": "club-1",
            "match_date": "2026-09-18",
            "match_date_iso": "2026-09-18",
            "competition": "England/Premier League",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "actual_result": "H",
            "actual_home_goals": 3,
            "actual_away_goals": 1,
            "prob_home": 0.55,
        }
        self.assertEqual(self.store.upsert_past_games([club])["upserted"], 1)
        club_loaded = self.store.load_past_games(competition_substr="Premier")
        self.assertEqual(len(club_loaded), 1)
        self.assertEqual(club_loaded[0]["actual_home_goals"], 3)

        # Live / past-score history keeps full payload.
        live_row = {
            "match_id": "live-intl-1",
            "kickoff_utc": "2026-09-10T19:00:00Z",
            "game_date": "2026-09-10",
            "competition": "International/Friendly",
            "home_team": "Spain",
            "away_team": "France",
            "home_score": 1,
            "away_score": 1,
            "status": "FT",
            "boxscore": {"shots": 10},
            "key_events": [{"type": "goal", "team": "Spain"}],
        }
        self.assertEqual(self.store.upsert_live_score_history([live_row])["upserted"], 1)
        history = self.store.load_live_score_history(competition_substr="Friendly")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["match_id"], "live-intl-1")
        self.assertEqual(history[0]["boxscore"]["shots"], 10)
        self.assertEqual(history[0]["key_events"][0]["type"], "goal")

        # International upcoming (friendlies predictions) lands in upcoming_games.
        upcoming = {
            "prediction_key": "nat-up-1",
            "match_date": "2026-09-25",
            "match_date_iso": "2026-09-25",
            "competition": "International/Friendly",
            "home_team": "Germany",
            "away_team": "Italy",
            "prob_home": 0.41,
            "prob_draw": 0.28,
            "prob_away": 0.31,
            "status": "upcoming",
        }
        self.assertEqual(
            self.store.upsert_upcoming_games([upcoming], source="national")["upserted"],
            1,
        )
        upcoming_loaded = self.store.load_upcoming_games(competition_substr="Friendly")
        self.assertEqual(len(upcoming_loaded), 1)
        self.assertEqual(upcoming_loaded[0]["home_team"], "Germany")
        self.assertEqual(upcoming_loaded[0].get("source"), "national")

        # UEFA / cups settled + upcoming also share the same tables.
        cup_settled = {
            "prediction_key": "ucl-1",
            "match_date": "2026-09-17",
            "competition": "Europe/Champions League",
            "home_team": "Bayern Munich",
            "away_team": "Inter",
            "actual_result": "H",
            "actual_home_goals": 2,
            "actual_away_goals": 0,
        }
        cup_upcoming = {
            "prediction_key": "ucl-2",
            "match_date": "2026-10-01",
            "competition": "Europe/Champions League",
            "home_team": "Real Madrid",
            "away_team": "Liverpool",
            "prob_home": 0.48,
        }
        self.store.upsert_past_games([cup_settled])
        self.store.upsert_upcoming_games([cup_upcoming], source="cups")
        self.assertEqual(
            len(self.store.load_past_games(competition_substr="Champions League")),
            1,
        )
        cups_upcoming = self.store.load_upcoming_games(competition_substr="Champions")
        self.assertEqual(len(cups_upcoming), 1)
        self.assertEqual(cups_upcoming[0].get("source"), "cups")

        final = self.store.store_info()
        self.assertGreaterEqual(final["tables"]["past_games"], 3)
        self.assertEqual(final["tables"]["live_score_history"], 1)
        self.assertGreaterEqual(final["tables"]["upcoming_games"], 2)

    def test_national_archive_writer_uses_same_store(self):
        nat = __import__("Process_National_Team_Data")
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
        incoming = {
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
        pd.DataFrame([existing]).to_csv(self.national_csv, index=False)
        with mock.patch.object(nat, "RAW_MATCHES_FILE", str(self.national_csv)):
            with mock.patch.object(nat, "ranked_national_teams", return_value=["Brazil", "Spain"]):
                with mock.patch.object(
                    nat, "fetch_completed_national_matches", return_value=[incoming]
                ):
                    result = nat.archive_national_match_history(
                        mock.Mock(
                            friendlies_only=True,
                            world_cup_only=False,
                            archive_lookback_days=45,
                            lookback_days=900,
                        )
                    )
        self.assertEqual(result["added"], 1)
        past = self.store.load_past_games(competition_substr="Friendly")
        ids = {str(row.get("match_id")) for row in past}
        self.assertIn("old-1", ids)
        self.assertIn("new-2", ids)
        for row in past:
            self.assertEqual(row.get("archive_source"), "national_training")


if __name__ == "__main__":
    unittest.main()
