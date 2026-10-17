"""Unit tests for GET /api/today and build_today_merged_payload."""

import os
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
_WEBSITE = _ROOT / "Website"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_WEBSITE) not in sys.path:
    sys.path.insert(0, str(_WEBSITE))

from competition_rules import canonical_team_name, make_match_sync_key
from routes.live_scores import (
    build_today_merged_payload,
    _TODAY_CACHE,
    _TODAY_CACHE_LOCK,
    _invalidate_today_cache,
)


class TestApiToday(unittest.TestCase):
    def setUp(self):
        _invalidate_today_cache()

    @patch("routes.live_scores.refresh_live_scores_now")
    @patch("predictions._load_upcoming_rows")
    @patch("routes.helpers.regional_espn_schedule_fallback")
    @patch("shared.sqlite_store.load_live_score_history")
    def test_merged_upcoming_and_live_game(
        self, mock_load_hist, mock_regional_fallback, mock_load_upcoming, mock_refresh_live
    ):
        mock_load_hist.return_value = []
        mock_regional_fallback.side_effect = lambda rows: rows

        target_date = "2026-10-18"

        # Mock an upcoming prediction fixture for target_date
        mock_load_upcoming.return_value = (
            [
                {
                    "competition": "English Premier League",
                    "match_date_iso": target_date,
                    "match_date": target_date,
                    "match_datetime_et": f"{target_date} 12:30:00",
                    "home_team": "Arsenal",
                    "away_team": "Chelsea",
                    "predicted_result": "H",
                    "prob_home": 0.55,
                    "prob_draw": 0.25,
                    "prob_away": 0.20,
                    "pred_home_goals": 2,
                    "pred_away_goals": 1,
                    "sync_key": f"{target_date}|arsenal|chelsea",
                }
            ],
            None,
            None,
        )

        # Mock active live scores in memory
        from live_poller import _live_scores, _live_scores_lock

        with _live_scores_lock:
            _live_scores.clear()
            _live_scores["English Premier League"] = {
                "competition": "English Premier League",
                "games": [
                    {
                        "match_id": "700101",
                        "status": "in",
                        "status_detail": "2nd Half - 65'",
                        "clock": 65,
                        "period": 2,
                        "home_team": "Arsenal",
                        "away_team": "Chelsea",
                        "home_score": 1,
                        "away_score": 1,
                        "kickoff_et": f"{target_date} 12:30:00",
                        "sync_key": f"{target_date}|arsenal|chelsea",
                        "goalscorers": [{"player": "Saka", "minute": 23, "team": "Arsenal"}],
                    }
                ],
            }

        payload = build_today_merged_payload(
            target_date_iso=target_date,
            compact=False,
            force_refresh=False,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["date"], target_date)
        self.assertEqual(payload["summary"]["total_games"], 1)
        self.assertEqual(payload["summary"]["live"], 1)
        self.assertEqual(payload["summary"]["scheduled"], 0)
        self.assertEqual(payload["summary"]["completed"], 0)
        self.assertEqual(payload["summary"]["matched"], 1)
        self.assertEqual(payload["summary"]["upcoming_only"], 0)
        self.assertEqual(payload["summary"]["live_only"], 0)

        match = payload["matches"][0]
        self.assertEqual(match["sync_status"], "matched")
        self.assertEqual(match["sources"], ["upcoming", "live_scores"])
        self.assertEqual(match["match_id"], "700101")
        self.assertEqual(match["status"], "in")
        self.assertEqual(match["home_score"], 1)
        self.assertEqual(match["away_score"], 1)
        self.assertEqual(match["predicted_result"], "H")
        self.assertEqual(match["pred_home_goals"], 2)
        self.assertIn("debug", match)
        self.assertEqual(match["debug"]["matched_by"], "sync_key")

    @patch("routes.live_scores.refresh_live_scores_now")
    @patch("predictions._load_upcoming_rows")
    @patch("routes.helpers.regional_espn_schedule_fallback")
    @patch("shared.sqlite_store.load_live_score_history")
    def test_upcoming_only_game(
        self, mock_load_hist, mock_regional_fallback, mock_load_upcoming, mock_refresh_live
    ):
        mock_load_hist.return_value = []
        mock_regional_fallback.side_effect = lambda rows: rows

        target_date = "2026-10-18"

        mock_load_upcoming.return_value = (
            [
                {
                    "competition": "Spanish La Liga",
                    "match_date_iso": target_date,
                    "match_date": target_date,
                    "match_datetime_et": f"{target_date} 15:00:00",
                    "home_team": "Real Madrid",
                    "away_team": "Barcelona",
                    "predicted_result": "D",
                    "prob_home": 0.38,
                    "prob_draw": 0.34,
                    "prob_away": 0.28,
                    "sync_key": f"{target_date}|real-madrid|barcelona",
                }
            ],
            None,
            None,
        )

        from live_poller import _live_scores, _live_scores_lock

        with _live_scores_lock:
            _live_scores.clear()

        payload = build_today_merged_payload(
            target_date_iso=target_date,
            compact=True,
            force_refresh=False,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["total_games"], 1)
        self.assertEqual(payload["summary"]["scheduled"], 1)
        self.assertEqual(payload["summary"]["upcoming_only"], 1)
        self.assertEqual(payload["summary"]["matched"], 0)

        match = payload["matches"][0]
        self.assertEqual(match["sync_status"], "upcoming_only")
        self.assertEqual(match["status"], "pre")
        self.assertEqual(match["home_team"], "Real Madrid")
        self.assertEqual(match["away_team"], "Barcelona")

    @patch("routes.live_scores.refresh_live_scores_now")
    @patch("predictions._load_upcoming_rows")
    @patch("routes.helpers.regional_espn_schedule_fallback")
    @patch("shared.sqlite_store.load_live_score_history")
    def test_live_only_game(
        self, mock_load_hist, mock_regional_fallback, mock_load_upcoming, mock_refresh_live
    ):
        mock_load_hist.return_value = []
        mock_regional_fallback.side_effect = lambda rows: rows
        mock_load_upcoming.return_value = ([], None, None)

        target_date = "2026-10-18"

        from live_poller import _live_scores, _live_scores_lock

        with _live_scores_lock:
            _live_scores.clear()
            _live_scores["French Ligue 1"] = {
                "competition": "French Ligue 1",
                "games": [
                    {
                        "match_id": "800202",
                        "status": "post",
                        "status_detail": "FT",
                        "home_team": "Paris Saint-Germain",
                        "away_team": "Marseille",
                        "home_score": 3,
                        "away_score": 1,
                        "kickoff_et": f"{target_date} 15:00:00",
                        "scoreboard_date": "20261018",
                    }
                ],
            }

        payload = build_today_merged_payload(
            target_date_iso=target_date,
            compact=False,
            force_refresh=False,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["total_games"], 1)
        self.assertEqual(payload["summary"]["completed"], 1)
        self.assertEqual(payload["summary"]["live_only"], 1)

        match = payload["matches"][0]
        self.assertEqual(match["sync_status"], "live_only")
        self.assertEqual(match["status"], "post")
        self.assertEqual(match["home_score"], 3)
        self.assertEqual(match["away_score"], 1)
        self.assertEqual(match["debug"]["live_match_id"], "800202")

    @patch("routes.live_scores.refresh_live_scores_now")
    @patch("predictions._load_upcoming_rows")
    @patch("routes.helpers.regional_espn_schedule_fallback")
    @patch("shared.sqlite_store.load_live_score_history")
    def test_filter_by_competition(
        self, mock_load_hist, mock_regional_fallback, mock_load_upcoming, mock_refresh_live
    ):
        mock_load_hist.return_value = []
        mock_regional_fallback.side_effect = lambda rows: rows

        target_date = "2026-10-18"

        mock_load_upcoming.return_value = (
            [
                {
                    "competition": "English Premier League",
                    "match_date_iso": target_date,
                    "match_date": target_date,
                    "home_team": "Liverpool",
                    "away_team": "Everton",
                    "sync_key": f"{target_date}|liverpool|everton",
                },
                {
                    "competition": "Italian Serie A",
                    "match_date_iso": target_date,
                    "match_date": target_date,
                    "home_team": "Juventus",
                    "away_team": "Inter Milan",
                    "sync_key": f"{target_date}|juventus|inter",
                },
            ],
            None,
            None,
        )

        from live_poller import _live_scores, _live_scores_lock

        with _live_scores_lock:
            _live_scores.clear()

        # Request only Italian Serie A
        payload = build_today_merged_payload(
            target_date_iso=target_date,
            comp_filter="Italian Serie A",
            compact=True,
            force_refresh=False,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["total_games"], 1)
        self.assertEqual(payload["matches"][0]["competition"], "Italian Serie A")
        self.assertEqual(payload["matches"][0]["home_team"], "Juventus")


if __name__ == "__main__":
    unittest.main()
