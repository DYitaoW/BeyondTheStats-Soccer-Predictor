"""Friendlies (one-off competitions): no tables / simulations / snapshots.

``/api/league-data/Club Friendlies`` and ``/api/league-data/International/Friendly``
used to build full league payloads (a 182-team predicted table, a "real" table,
form table, projection comparisons; up to 1 MB) with an empty fixtures list.
They now return only the upcoming games. Dates are relative to wall-clock time.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "Website", ROOT / "shared"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from starlette.testclient import TestClient  # noqa: E402

import config  # noqa: E402
from app import app  # noqa: E402
import routes.predictions as rp  # noqa: E402

TABLE_KEYS = {"real_table", "predicted_table", "winners_odds", "position_odds", "form_table",
              "comparison", "bracket", "predicted", "real", "qualification_zones"}


def _day(offset: int) -> str:
    return (datetime.now(ZoneInfo("America/New_York")) + timedelta(days=offset)).strftime("%Y-%m-%d")


def _row(comp, offset, home, away, **extra):
    d = _day(offset)
    row = {"competition": comp, "match_date": d, "match_date_iso": d, "home_team": home, "away_team": away,
           "match_datetime_utc": f"{d}T18:00:00Z", "prob_home": 0.5, "prob_draw": 0.25, "prob_away": 0.25}
    row.update(extra)
    return row


FEED = [
    _row("International/Friendly", 3, "Spain", "Japan"),
    _row("International/Friendly", 1, "Brazil", "Chile"),
    _row("International/Friendly", -2, "Old", "Game"),                         # past date
    _row("International/Friendly", 1, "Done", "Already", actual_result="H"),   # finished
    _row("International/Friendly", 2, "Live", "Final", live_status="post"),    # finished
    _row("Club Friendlies", 2, "Chelsea", "Ajax"),
    _row("England/Premier League", 1, "Arsenal", "Chelsea"),                   # other competition
]


class OneOffConfigTests(unittest.TestCase):
    def test_friendlies_are_one_off(self):
        for comp in ("Club Friendlies", "International/Friendly", "club friendlies"):
            self.assertTrue(config.is_one_off_competition(comp), comp)
        for comp in ("England/Premier League", "United States/NWSL", "England/FA Cup", ""):
            self.assertFalse(config.is_one_off_competition(comp), comp)

    def test_excluded_from_league_listings_and_tables(self):
        from routes.helpers import filter_league_tables_payload, is_league_api_competition

        self.assertFalse(is_league_api_competition("Club Friendlies"))
        self.assertTrue(is_league_api_competition("England/Premier League"))
        out = filter_league_tables_payload({"leagues": ["Club Friendlies", "England/Premier League"],
                                            "tables": {"Club Friendlies": {}, "England/Premier League": {}}})
        self.assertEqual(out["leagues"], ["England/Premier League"])
        self.assertEqual(list(out["tables"]), ["England/Premier League"])


class OneOffPayloadTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.patch = mock.patch.object(rp, "load_upcoming_mode_rows", return_value=([dict(r) for r in FEED], {}, []))
        self.loader = self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_league_data_is_upcoming_games_only(self):
        resp = self.client.get("/api/league-data/International/Friendly?no_cache=1")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["upcoming_only"])
        self.assertFalse(TABLE_KEYS & set(data), TABLE_KEYS & set(data))
        games = [(g["home_team"], g["away_team"]) for g in data["upcoming_games"]]
        self.assertEqual(games, [("Brazil", "Chile"), ("Spain", "Japan")])  # sorted, filtered
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["next_match_date"], _day(1))
        self.assertEqual(self.loader.call_args.args[0], "friendlies")

    def test_club_friendlies_and_compact(self):
        data = self.client.get("/api/league-data/Club Friendlies?compact=1&no_cache=1").json()
        self.assertEqual([(g["home_team"], g["away_team"]) for g in data["upcoming_games"]], [("Chelsea", "Ajax")])

    def test_backend_builders_never_produce_tables_or_cache(self):
        import league_data as ld
        import standings as st

        payload = ld.build_league_data_payload("International/Friendly")
        self.assertTrue(payload["upcoming_only"])
        self.assertTrue(ld._build_league_data_payload_uncached("Club Friendlies")["upcoming_only"])
        ld._write_league_data_cache("Club Friendlies", {"ok": True, "real_table": {"groups": [{}]}})
        self.assertFalse(os.path.exists(ld._league_data_cache_path("Club Friendlies")))
        self.assertIsNone(ld._load_league_data_from_cache("Club Friendlies"))
        self.assertIsNone(st._compute_standings_from_history("International/Friendly", force_recompute=True))
        self.assertEqual(st._live_standings_refresh_targets("Club Friendlies"), [])
        self.assertEqual(st.compute_league_form_table("International/Friendly")["overall"], [])
        self.assertEqual(st.refresh_real_standings_after_live_final("International/Friendly"), [])


if __name__ == "__main__":
    unittest.main()
