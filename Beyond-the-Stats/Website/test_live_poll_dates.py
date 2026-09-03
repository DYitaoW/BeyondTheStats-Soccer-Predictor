#!/usr/bin/env python3
"""Tests for live-score ET poll-date window and yesterday prune (no sklearn)."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import unittest

import pandas as pd


def _effective_poller_date(now_et):
    if now_et.hour < 2:
        return (now_et - timedelta(days=1)).date()
    return now_et.date()


def _poll_dates_for_cycle(now_et):
    today = now_et.date()
    yesterday = today - timedelta(days=1)
    if now_et.hour < 2:
        return [yesterday, today]
    if now_et.hour < 6:
        return [today, yesterday]
    return [today]


def _game_et_calendar_date(game):
    raw = game.get("scoreboard_date")
    if raw:
        text = str(raw).strip()
        for fmt, n in (("%Y%m%d", 8), ("%Y-%m-%d", 10)):
            try:
                return datetime.strptime(text[:n], fmt).date()
            except ValueError:
                continue
    for key in ("kickoff_et", "kickoff_utc", "match_date", "date", "start_time"):
        value = game.get(key)
        if not value:
            continue
        try:
            dt = pd.to_datetime(value, utc=True, errors="coerce")
            if pd.notna(dt):
                return dt.tz_convert(ZoneInfo("America/New_York")).date()
        except Exception:
            try:
                return date.fromisoformat(str(value)[:10])
            except Exception:
                continue
    return None


def _keep_game_for_poll_window(game, allowed_dates):
    status = str(game.get("status") or "").strip().lower()
    if status == "in":
        return True
    game_date = _game_et_calendar_date(game)
    if game_date is None:
        return False
    return game_date in allowed_dates


def _prune_live_scores_to_dates(live_scores, allowed_dates):
    allowed = set(allowed_dates or [])
    stale = []
    for comp_name, comp_data in list(live_scores.items()):
        games = [
            g for g in (comp_data.get("games") or [])
            if _keep_game_for_poll_window(g, allowed)
        ]
        if not games:
            stale.append(comp_name)
            continue
        comp_data["games"] = games
    for comp_name in stale:
        live_scores.pop(comp_name, None)


class PollDateWindowTests(unittest.TestCase):
    def test_overnight_before_2am_includes_yesterday_first(self):
        now = datetime(2026, 9, 3, 1, 15, tzinfo=ZoneInfo("America/New_York"))
        self.assertEqual(_poll_dates_for_cycle(now), [date(2026, 9, 2), date(2026, 9, 3)])
        self.assertEqual(_effective_poller_date(now), date(2026, 9, 2))

    def test_early_morning_keeps_spillover_yesterday(self):
        now = datetime(2026, 9, 3, 5, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertEqual(_poll_dates_for_cycle(now), [date(2026, 9, 3), date(2026, 9, 2)])
        self.assertEqual(_effective_poller_date(now), date(2026, 9, 3))

    def test_afternoon_et_is_today_only(self):
        now = datetime(2026, 9, 3, 18, 15, tzinfo=ZoneInfo("America/New_York"))
        self.assertEqual(_poll_dates_for_cycle(now), [date(2026, 9, 3)])

    def test_prune_drops_yesterdays_completed_keeps_live_spillover(self):
        live = {
            "England/Championship": {
                "games": [
                    {"match_id": "y1", "status": "post", "scoreboard_date": "20260902"},
                    {"match_id": "y2", "status": "in", "scoreboard_date": "20260902"},
                    {"match_id": "t1", "status": "post", "scoreboard_date": "20260903"},
                    {"match_id": "t2", "status": "pre", "scoreboard_date": "20260903"},
                ]
            }
        }
        _prune_live_scores_to_dates(live, [date(2026, 9, 3)])
        ids = sorted(g["match_id"] for g in live["England/Championship"]["games"])
        self.assertEqual(ids, ["t1", "t2", "y2"])

    def test_prune_removes_competition_when_only_yesterday_finals(self):
        live = {
            "North America/Leagues Cup": {
                "games": [
                    {"match_id": "lc1", "status": "post", "scoreboard_date": "20260902"},
                ]
            }
        }
        _prune_live_scores_to_dates(live, [date(2026, 9, 3)])
        self.assertNotIn("North America/Leagues Cup", live)


if __name__ == "__main__":
    unittest.main()
