#!/usr/bin/env python3
"""Tests for live match-minute parsing and momentum (no sklearn)."""
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from espn_parser import (  # noqa: E402
    _is_halftime_break,
    _key_event_match_minute,
    _parse_elapsed_minutes,
    _parse_espn_live_event,
    _period_label_from_espn_status,
)

if "predictions" not in sys.modules:
    predictions_stub = types.ModuleType("predictions")
    predictions_stub._to_float_or_none = lambda v: None
    sys.modules["predictions"] = predictions_stub

from live_prediction import _compute_live_momentum, _update_cumulative_momentum  # noqa: E402


def _game(**kwargs):
    base = {
        "status": "in",
        "period": "1st Half",
        "status_type": "STATUS_FIRST_HALF",
        "clock": "20'",
        "home_team_id": "1",
        "away_team_id": "2",
        "home_score": 0,
        "away_score": 0,
        "home_stats": {"totalShots": 2, "shotsOnTarget": 1, "wonCorners": 1},
        "away_stats": {"totalShots": 2, "shotsOnTarget": 1, "wonCorners": 1},
    }
    base.update(kwargs)
    return base


class PeriodAndClockTests(unittest.TestCase):
    def test_period_labels(self):
        self.assertEqual(_period_label_from_espn_status("STATUS_FIRST_HALF", "15'", 1), "1st Half")
        self.assertEqual(_period_label_from_espn_status("STATUS_HALFTIME", "HT", 1), "Halftime")
        self.assertEqual(_period_label_from_espn_status("STATUS_SECOND_HALF", "52'", 2), "2nd Half")

    def test_elapsed_minutes(self):
        self.assertEqual(_parse_elapsed_minutes("15'", "1st Half"), 15)
        self.assertEqual(_parse_elapsed_minutes("52'", "2nd Half"), 52)
        self.assertEqual(_parse_elapsed_minutes("90'+6'", "FT"), 96)
        self.assertEqual(_parse_elapsed_minutes("HT", "Halftime"), 45)
        self.assertEqual(_parse_elapsed_minutes("45'+3'", "1st Half"), 48)

    def test_key_event_minutes_not_shifted_by_period(self):
        first = {"clock": "28'", "period": 1}
        second = {"clock": "52'", "period": 2}
        self.assertEqual(_key_event_match_minute(first), 28)
        self.assertEqual(_key_event_match_minute(second), 52)

    def test_live_event_uses_display_clock_not_seconds(self):
        event = {
            "id": "1",
            "date": "2026-09-03T18:00Z",
            "competitions": [{
                "status": {
                    "clock": 900.0,
                    "displayClock": "15'",
                    "period": 1,
                    "type": {
                        "state": "in",
                        "name": "STATUS_FIRST_HALF",
                        "detail": "15'",
                    },
                },
                "competitors": [
                    {"homeAway": "home", "id": "10", "team": {"displayName": "Home"}, "score": "0"},
                    {"homeAway": "away", "id": "20", "team": {"displayName": "Away"}, "score": "0"},
                ],
            }],
        }
        parsed = _parse_espn_live_event(event)
        self.assertEqual(parsed["clock"], "15'")
        self.assertEqual(parsed["period"], "1st Half")
        self.assertEqual(parsed["status_type"], "STATUS_FIRST_HALF")
        self.assertEqual(_parse_elapsed_minutes(parsed["clock"], parsed["period"]), 15)


class MomentumTests(unittest.TestCase):
    def test_goal_does_not_create_momentum(self):
        game = _game(
            clock="30'",
            home_score=0,
            away_score=1,
            key_events=[{
                "type": "Goal",
                "clock": "29'",
                "period": 1,
                "team_id": "2",
                "scoring_play": True,
            }],
        )
        _update_cumulative_momentum(game)
        _update_cumulative_momentum(game)
        # Second tick with a goal but no extra pressure should stay near 0.
        self.assertEqual(game["_momentum_value"], 0.0)
        trend = _compute_live_momentum(game, 30)
        self.assertEqual(trend["label"], "neutral")

    def test_shots_create_momentum_not_scoreline(self):
        game = _game(
            clock="25'",
            home_stats={"totalShots": 3, "shotsOnTarget": 1, "wonCorners": 1},
            away_stats={"totalShots": 3, "shotsOnTarget": 1, "wonCorners": 1},
        )
        _update_cumulative_momentum(game)
        game["clock"] = "27'"
        game["home_stats"] = {"totalShots": 6, "shotsOnTarget": 3, "wonCorners": 3}
        game["home_score"] = 0
        game["away_score"] = 1  # counter-goal for away, home still pressing
        _update_cumulative_momentum(game)
        self.assertLess(game["_momentum_value"], 0)  # negative = home pressure

    def test_halftime_freezes_then_restarts_at_45(self):
        game = _game(clock="44'", period="1st Half", status_type="STATUS_FIRST_HALF")
        _update_cumulative_momentum(game)
        game["clock"] = "45'+1'"
        game["home_stats"] = {"totalShots": 8, "shotsOnTarget": 4, "wonCorners": 4}
        _update_cumulative_momentum(game)
        pre_ht = game["_momentum_value"]
        history_len = len(game["momentum_history"])

        game["period"] = "Halftime"
        game["status_type"] = "STATUS_HALFTIME"
        game["clock"] = "HT"
        for _ in range(8):
            _update_cumulative_momentum(game)
        self.assertEqual(len(game["momentum_history"]), history_len + 1)
        ht_points = [p for p in game["momentum_history"] if isinstance(p, dict) and p.get("phase") == "ht"]
        self.assertEqual(len(ht_points), 1)
        self.assertEqual(game["_momentum_value"], pre_ht)

        game["period"] = "2nd Half"
        game["status_type"] = "STATUS_SECOND_HALF"
        game["clock"] = "46'"
        _update_cumulative_momentum(game)
        self.assertEqual(game["_momentum_value"], 0.0)
        restart = [p for p in game["momentum_history"] if p.get("phase") == "play" and p.get("minute") == 45]
        self.assertTrue(restart)
        self.assertEqual(restart[-1]["value"], 0.0)

    def test_history_uses_match_minutes(self):
        game = _game(clock="10'")
        _update_cumulative_momentum(game)
        game["clock"] = "12'"
        game["home_stats"] = {"totalShots": 4, "shotsOnTarget": 2, "wonCorners": 2}
        _update_cumulative_momentum(game)
        minutes = [p["minute"] for p in game["momentum_history"] if p.get("phase") == "play"]
        self.assertEqual(minutes, [10, 12])


if __name__ == "__main__":
    unittest.main()
