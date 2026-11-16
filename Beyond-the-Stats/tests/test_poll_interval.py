"""Live poller sleep interval must never sleep through a kickoff.

Regression for 2026-11-15: the WSL game Arsenal v London City Lionesses kicked
off at 11:30 ET but the poller first saw it live at 11:45 ET, so the "start"
alert was 15 min late and both goals (10', 11') never alerted. Two causes:
1. ``_compute_poll_interval`` returned on the FIRST kickoff it examined, so a
   game 30-60 min away in another competition gave a 15-min sleep straight
   through a kickoff happening now;
2. the kickoff map came only from pipeline fixtures (women's ones are
   date-only), never from the real ESPN ``kickoff_utc`` on the scoreboard.

All times are relative to wall-clock time.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "Website"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import live_poller as lp

ET = ZoneInfo("America/New_York")


def _et(minutes: float) -> datetime:
    return datetime.now(ET) + timedelta(minutes=minutes)


def _iso_utc(minutes: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


class PollIntervalTests(unittest.TestCase):
    def test_imminent_kickoff_wins_regardless_of_competition_order(self):
        todays = {
            "International/Friendly": [_et(30)],          # listed first, 30 min away
            "England/Women's Super League": [_et(-0.5)],  # kicked off 30 s ago
        }
        self.assertEqual(lp._compute_poll_interval({}, todays), 60)
        todays = {
            "International/Friendly": [_et(45)],
            "United States/NWSL": [_et(2)],
        }
        self.assertEqual(lp._compute_poll_interval({}, todays), 60)

    def test_pre_match_sleep_ends_before_kickoff(self):
        # Kickoff in 10 min: the old code slept 15 min (5 min past kickoff).
        interval = lp._compute_poll_interval({}, {"England/Premier League": [_et(10)]})
        self.assertLessEqual(interval, 10 * 60 - 60)
        self.assertGreaterEqual(interval, 60)
        # Kickoff in 50 min: normal 15-min pre-match polling.
        self.assertEqual(lp._compute_poll_interval({}, {"England/Premier League": [_et(50)]}), 900)

    def test_espn_scoreboard_kickoff_used_when_pipeline_schedule_is_wrong(self):
        # Pipeline thinks the WSL games are at a later default time; ESPN's
        # scoreboard says one kicks off in 1 minute.
        todays = {"England/WSL": [_et(150)]}
        results = {
            "England/Women's Super League": {"games": [
                {"match_id": "401902864", "status": "pre", "kickoff_utc": _iso_utc(1)},
            ]},
        }
        self.assertEqual(lp._compute_poll_interval(results, todays), 60)
        # Same with no pipeline schedule at all.
        self.assertEqual(lp._compute_poll_interval(results, {}), 60)

    def test_live_game_and_idle(self):
        self.assertEqual(lp._compute_poll_interval({"X": {"games": [{"status": "in"}]}}, {}), 60)
        self.assertEqual(lp._compute_poll_interval({}, {}), 1800)
        # Finished games on the scoreboard don't keep the poller awake.
        results = {"X": {"games": [{"status": "post", "kickoff_utc": _iso_utc(-200)}]}}
        self.assertEqual(lp._compute_poll_interval(results, {}), 1800)

    def test_wakes_an_hour_before_distant_kickoff(self):
        # 80 min out: wake at the 1-hour mark (20 min), never later.
        interval = lp._compute_poll_interval({}, {"Spain/La Liga": [_et(80)]})
        self.assertAlmostEqual(interval, 20 * 60, delta=5)
        # 100 min out: never sleeps longer than the 30-min idle cap.
        self.assertLessEqual(lp._compute_poll_interval({}, {"Spain/La Liga": [_et(100)]}), 1800)


if __name__ == "__main__":
    unittest.main()
