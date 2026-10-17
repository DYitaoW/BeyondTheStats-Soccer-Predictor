"""Unit tests for unified match sync key, canonical team names, and cross-endpoint syncing."""

import os
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
_WEBSITE = _ROOT / "Website"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_WEBSITE) not in sys.path:
    sys.path.insert(0, str(_WEBSITE))

from competition_rules import canonical_team_name, make_match_sync_key
from live_poller import _stamp_game_sync_fields
from predictions import (
    _enrich_json_past_row,
    _live_game_to_past_row,
    _build_past_game_prediction_lookup,
    _merge_prediction_onto_past_row,
)
from routes.predictions import (
    _format_next_game_payload,
    _to_compact_upcoming_row,
)


class TestMatchSyncUnification(unittest.TestCase):
    def test_sync_key_deterministic_across_date_formats(self):
        comp = "English Premier League"
        # Match played on 2026-10-18 at 21:00 ET (which is 2026-10-19 01:00 UTC)
        key_from_et_date = make_match_sync_key("2026-10-18", comp, "Manchester United", "Brentford")
        key_from_utc_iso = make_match_sync_key("2026-10-19T01:00:00Z", comp, "Manchester United", "Brentford")
        key_from_dt_obj = make_match_sync_key(
            datetime(2026, 10, 19, 1, 0, 0, tzinfo=timezone.utc),
            comp, "Manchester United", "Brentford"
        )
        key_from_date_obj = make_match_sync_key(date(2026, 10, 18), comp, "Manchester United", "Brentford")

        self.assertEqual(key_from_et_date, "2026-10-18|man-united|brentford")
        self.assertEqual(key_from_utc_iso, "2026-10-18|man-united|brentford")
        self.assertEqual(key_from_dt_obj, "2026-10-18|man-united|brentford")
        self.assertEqual(key_from_date_obj, "2026-10-18|man-united|brentford")

    def test_sync_key_canonical_aliases(self):
        comp = "English Premier League"
        k1 = make_match_sync_key("2026-10-18", comp, "Manchester United", "Wolverhampton Wanderers")
        k2 = make_match_sync_key("2026-10-18", comp, "Man United", "Wolves")
        self.assertEqual(k1, k2)
        self.assertEqual(k1, "2026-10-18|man-united|wolves")

        ucl = "Europe/Champions League"
        m1 = make_match_sync_key("2026-10-20", ucl, "Milan", "Benfica")
        m2 = make_match_sync_key("2026-10-20", ucl, "AC Milan", "SL Benfica")
        self.assertEqual(m1, m2)
        self.assertEqual(m1, "2026-10-20|milan|benfica")

    def test_real_sociedad_vs_b_team_separation(self):
        la_liga = "Spain/La Liga"
        la_liga_2 = "Spain/La Liga 2"
        sociedad = canonical_team_name("Real Sociedad", la_liga)
        sociedad_b = canonical_team_name("Real Sociedad B", la_liga_2)
        self.assertEqual(sociedad, "Sociedad")
        self.assertEqual(sociedad_b, "Real Sociedad B")

        k_first = make_match_sync_key("2026-10-18", la_liga, "Real Sociedad", "Getafe")
        k_reserve = make_match_sync_key("2026-10-18", la_liga_2, "Real Sociedad B", "Getafe")
        self.assertNotEqual(k_first, k_reserve)
        self.assertIn("sociedad", k_first)
        self.assertIn("real-sociedad-b", k_reserve)

    def test_stamp_game_sync_fields(self):
        game = {
            "match_id": "401569876",
            "home_team": "Manchester United",
            "away_team": "Brentford",
            "match_date": "2026-10-18",
            "kickoff_utc": "2026-10-18T14:00:00Z",
            "scoreboard_date": "20261018",
        }
        _stamp_game_sync_fields(game, "English Premier League")
        self.assertEqual(game.get("canonical_home_team"), "Man United")
        self.assertEqual(game.get("canonical_away_team"), "Brentford")
        self.assertEqual(game.get("sync_key"), "2026-10-18|man-united|brentford")

    def test_enrich_json_past_row_attaches_sync_fields(self):
        row = {
            "match_date": "2026-10-18",
            "competition": "English Premier League",
            "home_team": "Manchester United",
            "away_team": "Brentford",
            "home_score": 2,
            "away_score": 1,
            "actual_result": "H",
        }
        _enrich_json_past_row(row)
        self.assertEqual(row.get("canonical_home_team"), "Man United")
        self.assertEqual(row.get("canonical_away_team"), "Brentford")
        self.assertEqual(row.get("sync_key"), "2026-10-18|man-united|brentford")

    def test_live_game_to_past_row_attaches_sync_fields(self):
        live_game = {
            "match_id": "espn-987",
            "competition": "English Premier League",
            "home_team": "Manchester United",
            "away_team": "Brentford",
            "home_score": 2,
            "away_score": 1,
            "status": "post",
            "match_date": "2026-10-18",
            "kickoff_utc": "2026-10-18T14:00:00Z",
        }
        past_row = _live_game_to_past_row(live_game)
        self.assertIsNotNone(past_row)
        self.assertEqual(past_row.get("canonical_home_team"), "Man United")
        self.assertEqual(past_row.get("canonical_away_team"), "Brentford")
        self.assertEqual(past_row.get("sync_key"), "2026-10-18|man-united|brentford")
        self.assertEqual(past_row.get("match_id"), "espn-987")

    def test_format_next_game_payload_includes_sync_key(self):
        game = {
            "match_id": "espn-123",
            "competition": "English Premier League",
            "home_team": "Manchester United",
            "away_team": "Brentford",
            "match_date": "2026-10-18",
            "match_datetime_utc": "2026-10-18T14:00:00Z",
            "prob_home": 0.55,
            "prob_draw": 0.25,
            "prob_away": 0.20,
        }
        payload = _format_next_game_payload(game, is_home=True, target_team="Manchester United", status="upcoming")
        self.assertIn("sync_key", payload)
        self.assertEqual(payload["sync_key"], "2026-10-18|man-united|brentford")
        self.assertEqual(payload["canonical_home_team"], "Man United")
        self.assertEqual(payload["canonical_away_team"], "Brentford")

    def test_cross_source_prediction_merge_by_sync_key(self):
        # Model predicted row from upcoming CSV/store with predictor naming
        pred_row = {
            "match_date": "2026-10-18",
            "match_date_iso": "2026-10-18",
            "competition": "England/Premier League",
            "home_team": "Man United",
            "away_team": "Brentford",
            "canonical_home_team": "Man United",
            "canonical_away_team": "Brentford",
            "sync_key": "2026-10-18|man-united|brentford",
            "predicted_result": "H",
            "prob_home": 62.5,
            "prob_draw": 22.0,
            "prob_away": 15.5,
            "pred_home_goals": 2,
            "pred_away_goals": 1,
        }

        # Completed live game from ESPN with different competition and team naming
        live_game = {
            "match_id": "espn-456",
            "competition": "English Premier League",
            "home_team": "Manchester United",
            "away_team": "Brentford",
            "home_score": 2,
            "away_score": 1,
            "status": "post",
            "match_date": "2026-10-18",
            "kickoff_utc": "2026-10-18T14:00:00Z",
        }
        past_row = _live_game_to_past_row(live_game)

        # Lookup indexed with predictor row
        lookup = {
            pred_row["sync_key"]: pred_row,
            f"2026-10-18|man united|brentford": pred_row,
        }

        merged = _merge_prediction_onto_past_row(past_row, lookup)
        # Verify prediction fields were successfully merged across naming differences!
        self.assertEqual(merged.get("predicted_result"), "H")
        self.assertEqual(merged.get("prob_home"), 62.5)
        self.assertEqual(merged.get("sync_key"), "2026-10-18|man-united|brentford")
        self.assertEqual(merged.get("match_id"), "espn-456")
        self.assertEqual(merged.get("actual_home_goals"), 2)


if __name__ == "__main__":
    unittest.main()
