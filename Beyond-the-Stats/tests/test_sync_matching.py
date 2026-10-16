"""
Tests for canonical match sync_key generation, upcoming vs live-score matching,
and league-leaders memo caching.
"""
import os
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BTS_DIR = os.path.dirname(_TESTS_DIR)
_WEBSITE_DIR = os.path.join(_BTS_DIR, "Website")
for p in (_BTS_DIR, _WEBSITE_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from competition_rules import canonical_team_name, make_match_sync_key
from live_poller import _stamp_game_sync_fields, to_compact_live_game
from routes.predictions import _to_compact_upcoming_row
from routes.leagues import _LEAGUE_LEADERS_MEMO_CACHE, clear_league_leaders_cache


class TestSyncMatching(unittest.TestCase):

    def test_sync_key_man_united_vs_wolves(self):
        # Model / dataset upcoming format
        key_pred = make_match_sync_key(
            "2026-10-18",
            "England/Premier League",
            "Manchester United",
            "Wolverhampton Wanderers",
        )
        # ESPN live-scores format
        key_live = make_match_sync_key(
            "2026-10-18",
            "England/Premier League",
            "Man United",
            "Wolves",
        )
        self.assertEqual(key_pred, key_live)
        self.assertEqual(key_pred, "2026-10-18|manunited|wolves")

    def test_sync_key_inter_milan(self):
        key_canon = make_match_sync_key(
            "2026-10-19",
            "Italy/Serie A",
            "Internazionale",
            "Juventus",
        )
        key_espn = make_match_sync_key(
            "2026-10-19",
            "Italy/Serie A",
            "Inter Milan",
            "Juventus",
        )
        self.assertEqual(key_canon, key_espn)
        self.assertEqual(key_canon, "2026-10-19|inter|juventus")

    def test_sync_key_spurs_tottenham(self):
        key1 = make_match_sync_key(
            "2026-10-24",
            "England/Premier League",
            "Tottenham Hotspur",
            "Arsenal",
        )
        key2 = make_match_sync_key(
            "2026-10-24",
            "England/Premier League",
            "Spurs",
            "Arsenal",
        )
        key3 = make_match_sync_key(
            "2026-10-24",
            "England/Premier League",
            "Tottenham",
            "Arsenal",
        )
        self.assertEqual(key1, key2)
        self.assertEqual(key2, key3)
        self.assertEqual(key1, "2026-10-24|tottenham|arsenal")

    def test_sync_key_date_variations(self):
        # 8-digit date string, 10-digit ISO string, and UTC timestamp in evening ET
        k_dash = make_match_sync_key("2026-10-18", "England/Premier League", "Arsenal", "Chelsea")
        k_compact = make_match_sync_key("20261018", "England/Premier League", "Arsenal", "Chelsea")
        # 2026-10-18 20:00 ET = 2026-10-19 00:00 UTC
        k_utc = make_match_sync_key("2026-10-19T00:00:00Z", "England/Premier League", "Arsenal", "Chelsea")
        self.assertEqual(k_dash, "2026-10-18|arsenal|chelsea")
        self.assertEqual(k_compact, "2026-10-18|arsenal|chelsea")
        self.assertEqual(k_utc, "2026-10-18|arsenal|chelsea")

    def test_stamp_game_sync_fields(self):
        live_game = {
            "match_id": "700999",
            "competition": "England/Premier League",
            "home_team": "Man United",
            "away_team": "Wolves",
            "match_date": "2026-10-18",
            "home_score": 2,
            "away_score": 1,
            "status": "in",
            "clock": "72'",
        }
        _stamp_game_sync_fields(live_game, "England/Premier League")
        self.assertEqual(live_game.get("canonical_home_team"), "Man United")
        self.assertEqual(live_game.get("canonical_away_team"), "Wolves")
        self.assertEqual(live_game.get("sync_key"), "2026-10-18|manunited|wolves")

        # Compact live game retains these fields
        compact = to_compact_live_game(live_game)
        self.assertEqual(compact.get("sync_key"), "2026-10-18|manunited|wolves")
        self.assertEqual(compact.get("canonical_home_team"), "Man United")
        self.assertEqual(compact.get("canonical_away_team"), "Wolves")
        self.assertEqual(compact.get("match_id"), "700999")

    def test_compact_upcoming_row_sync_fields(self):
        pred_row = {
            "competition": "England/Premier League",
            "home_team": "Manchester United",
            "away_team": "Wolverhampton Wanderers",
            "match_date": "2026-10-18",
            "match_date_iso": "2026-10-18",
            "predicted_result": "H",
            "prob_home": 62.5,
            "prob_draw": 22.1,
            "prob_away": 15.4,
        }
        compact_pred = _to_compact_upcoming_row(pred_row)
        self.assertEqual(compact_pred.get("canonical_home_team"), "Man United")
        self.assertEqual(compact_pred.get("canonical_away_team"), "Wolves")
        self.assertEqual(compact_pred.get("sync_key"), "2026-10-18|manunited|wolves")

    def test_matching_dict_by_sync_key(self):
        # Simulate frontend dictionary matching
        live_game = {
            "match_id": "800111",
            "competition": "England/Premier League",
            "home_team": "Man United",
            "away_team": "Wolves",
            "match_date": "2026-10-18",
            "status": "in",
        }
        _stamp_game_sync_fields(live_game, "England/Premier League")

        live_by_sync_key = {live_game["sync_key"]: live_game}

        pred_row = {
            "competition": "England/Premier League",
            "home_team": "Manchester United",
            "away_team": "Wolverhampton Wanderers",
            "match_date_iso": "2026-10-18",
        }
        compact_pred = _to_compact_upcoming_row(pred_row)

        matched_live = live_by_sync_key.get(compact_pred["sync_key"])
        self.assertIsNotNone(matched_live)
        self.assertEqual(matched_live["match_id"], "800111")

    def test_league_leaders_memo_cache(self):
        clear_league_leaders_cache()
        self.assertIsNone(_LEAGUE_LEADERS_MEMO_CACHE["data"])
        self.assertEqual(_LEAGUE_LEADERS_MEMO_CACHE["expires_at"], 0.0)

        # Manually populate memo cache to verify isolation
        fake_payload = {"ok": True, "leagues": [{"competition": "Test League"}], "cups": []}
        _LEAGUE_LEADERS_MEMO_CACHE["data"] = fake_payload
        _LEAGUE_LEADERS_MEMO_CACHE["expires_at"] = 9999999999.0

        # Invalidate
        clear_league_leaders_cache()
        self.assertIsNone(_LEAGUE_LEADERS_MEMO_CACHE["data"])
        self.assertEqual(_LEAGUE_LEADERS_MEMO_CACHE["expires_at"], 0.0)


if __name__ == "__main__":
    unittest.main()

